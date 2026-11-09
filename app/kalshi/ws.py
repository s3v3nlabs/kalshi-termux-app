"""Kalshi WebSocket market-data + fill streams (push instead of polling).

Two connections, both signed with the same key (GET /trade-api/ws/v2):

* **Market data** (``settings.ws_url``, e.g. wss://external-api-ws.kalshi.com/
  trade-api/ws/v2): ``orderbook_delta`` (local books, seq gap -> get_snapshot),
  ``ticker``, ``trade`` for the watched markets, and ``market_lifecycle_v2``
  for all markets (open/close/determined events).
* **Fills** (``wss://warp-ws.kalshi.com/trade-api/ws/v2``): the low-latency
  host that serves ONLY ``fill`` and ``user_orders``.

Streams do not spend REST tokens. Kalshi allows 200 concurrent connections per
user on Basic; this app opens two. Reconnects back off exponentially with
jitter (a handshake 429 means the connection cap; we wait longer). If keys are
missing or the stream is down, the app falls back to REST polling.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Any, Callable, Iterable, Optional

from app.config import settings
from app.kalshi.books import BookStore, SeqTracker
from app.kalshi.signing import WS_SIGN_PATH, Signer

log = logging.getLogger(__name__)

WARP_WS_HOSTS = {
    "external": "wss://warp-ws.kalshi.com/trade-api/ws/v2",
    "elections": "wss://warp-ws.kalshi.com/trade-api/ws/v2",
    "trading": "wss://warp-ws.kalshi.com/trade-api/ws/v2",
    "demo": "wss://warp-ws.demo.kalshi.co/trade-api/ws/v2",
}
FALLBACK_WS = {
    "wss://external-api-ws.kalshi.com/trade-api/ws/v2": "wss://api.elections.kalshi.com/trade-api/ws/v2",
}
MARKET_CHANNELS = ("orderbook_delta", "ticker", "trade")
WS_WATCH_MAX = 200
BACKOFF_MIN, BACKOFF_MAX = 1.0, 60.0


def _default_connect(url: str, headers: dict):
    import websockets  # lazy: the app still runs without it (REST polling)

    kw = dict(ping_interval=20, ping_timeout=20, open_timeout=10, max_size=2**22, close_timeout=5)
    try:
        return websockets.connect(url, additional_headers=headers, **kw)
    except TypeError:  # websockets < 14 (legacy client)
        return websockets.connect(url, extra_headers=headers, **kw)


def _handshake_status(exc: BaseException) -> Optional[int]:
    for attr in ("status_code",):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            return v
    resp = getattr(exc, "response", None)
    v = getattr(resp, "status_code", None)
    return v if isinstance(v, int) else None


class _Conn:
    """State for one socket: command ids and channel -> sid map."""

    def __init__(self, ws):
        self.ws = ws
        self.next_id = 1
        self.sids: dict[str, int] = {}
        self.pending: set[str] = set()  # subscribed, waiting for "subscribed"
        self.seq = SeqTracker()

    async def send(self, cmd: str, params: dict) -> int:
        i = self.next_id
        self.next_id += 1
        await self.ws.send(json.dumps({"id": i, "cmd": cmd, "params": params}))
        return i


class MarketStream:
    def __init__(
        self,
        books: Optional[BookStore] = None,
        signer_fn: Optional[Callable[[], Signer]] = None,
        connect: Callable[[str, dict], Any] = _default_connect,
        url_fn: Optional[Callable[[], str]] = None,
        warp_url_fn: Optional[Callable[[], str]] = None,
    ):
        self.books = books or BookStore()
        self.signer_fn = signer_fn or (lambda: Signer(settings.key_id, settings.key_path))
        self._connect = connect
        self.url_fn = url_fn or (lambda: settings.ws_url)
        self.warp_url_fn = warp_url_fn or (lambda: WARP_WS_HOSTS.get(settings.host_key, WARP_WS_HOSTS["external"]))
        self.enabled = True  # WS_ENABLED=0 turns streaming off
        self.connected = False
        self.warp_connected = False
        self.url_in_use: Optional[str] = None
        self.reason = "starting"
        self.warp_reason = "starting"
        self.msgs = 0
        self.gaps = 0
        self.resyncs = 0
        self.reconnects = 0
        self.errors: list[str] = []
        self.last_msg_at: Optional[float] = None
        self.fills = 0
        self.user_orders = 0
        self._groups: dict[str, set[str]] = {}
        self._subscribed: set[str] = set()
        self._conn: Optional[_Conn] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._listeners: list[Callable[[str, dict], None]] = []
        self._rng = random.random

    # ----- watch list (called from any thread) -----

    def watch(self) -> list[str]:
        out: list[str] = []
        seen = set()
        # Priority order: held/sniper first, scanner rows last.
        for name in sorted(self._groups, key=lambda g: (g == "scan", g)):
            for t in self._groups[name]:
                if t not in seen:
                    seen.add(t)
                    out.append(t)
        return out[:WS_WATCH_MAX]

    def set_watch(self, group: str, tickers: Iterable[str]) -> None:
        self._groups[group] = {str(t) for t in tickers if t}
        if self._loop is not None and self._conn is not None:
            try:
                self._loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._sync_watch()))
            except RuntimeError:
                pass

    def on_event(self, fn: Callable[[str, dict], None]) -> None:
        self._listeners.append(fn)

    def _emit(self, kind: str, msg: dict) -> None:
        for fn in list(self._listeners):
            try:
                fn(kind, msg)
            except Exception:  # listeners never break the stream
                log.exception("ws listener")

    # ----- status -----

    @property
    def live(self) -> bool:
        """True when scanner/auto-bet can use pushed prices instead of polling."""
        return bool(self.connected and self.last_msg_at is not None and time.monotonic() - self.last_msg_at < 90)

    def status(self) -> dict:
        age = None if self.last_msg_at is None else round(time.monotonic() - self.last_msg_at, 1)
        return {
            "enabled": self.enabled,
            "connected": self.connected,
            "live": self.live,
            "mode": "live stream" if self.live else "REST polling",
            "reason": self.reason,
            "url": self.url_in_use,
            "fills_stream": {"connected": self.warp_connected, "reason": self.warp_reason, "url": self.warp_url_fn(),
                             "fills": self.fills, "user_orders": self.user_orders},
            "watching": len(self._subscribed),
            "messages": self.msgs,
            "seq_gaps": self.gaps,
            "resyncs": self.resyncs,
            "reconnects": self.reconnects,
            "last_message_age_s": age,
            "errors": self.errors[-5:],
            **self.books.stats(),
        }

    # ----- message handling (market socket) -----

    async def handle(self, conn: _Conn, data: dict) -> None:
        self.msgs += 1
        self.last_msg_at = time.monotonic()
        typ = data.get("type")
        msg = data.get("msg") if isinstance(data.get("msg"), (dict, list)) else {}
        sid = data.get("sid")
        if typ == "subscribed" and isinstance(msg, dict):
            if msg.get("channel"):
                conn.sids[msg["channel"]] = int(msg.get("sid") or 0)
                conn.pending.discard(msg["channel"])
            return
        if typ == "error":
            self.errors.append(f"{(msg or {}).get('code')}: {(msg or {}).get('msg')}")
            conn.pending.clear()  # a failed subscribe must not block later updates
            return
        # Contiguous seq per subscription; only orderbook needs a resync.
        if sid is not None and data.get("seq") is not None:
            ok = conn.seq.check(sid, data["seq"])
            if not ok and sid == conn.sids.get("orderbook_delta"):
                self.gaps += 1
                await self._resync(conn)
                if typ == "orderbook_delta":
                    return
        if typ == "orderbook_snapshot":
            t = self.books.snapshot(msg)
            if t:
                self._emit("book", {"ticker": t})
        elif typ == "orderbook_delta":
            t = self.books.delta(msg)
            if t:
                self._emit("book", {"ticker": t})
        elif typ == "ticker":
            t = self.books.ticker(msg)
            if t:
                self._emit("ticker", {"ticker": t, **msg})
        elif typ == "trade":
            self.books.trade(msg)
            self._emit("trade", msg)
        elif typ == "market_lifecycle_v2":
            t = self.books.lifecycle_event(msg)
            if t:
                self._emit("lifecycle", msg)

    async def _resync(self, conn: _Conn) -> None:
        """Seq gap: books may be wrong. Ask for fresh snapshots of every market."""
        sid = conn.sids.get("orderbook_delta")
        tickers = sorted(self._subscribed)
        self.books.mark_unsynced(set(tickers))
        if sid is None or not tickers:
            return
        self.resyncs += 1
        await conn.send("update_subscription", {"sids": [sid], "market_tickers": tickers, "action": "get_snapshot"})

    async def _sync_watch(self) -> None:
        conn = self._conn
        if conn is None:
            return
        if conn.pending:
            # Wait for the server to confirm sids, then apply the latest watch list.
            asyncio.get_running_loop().call_later(1.0, lambda: asyncio.ensure_future(self._sync_watch()))
            return
        want = set(self.watch())
        add = sorted(want - self._subscribed)
        rem = sorted(self._subscribed - want)
        if add:
            missing = [c for c in MARKET_CHANNELS if c not in conn.sids]
            if missing:
                conn.pending.update(missing)
                await conn.send("subscribe", {"channels": missing, "market_tickers": add})
            for ch in MARKET_CHANNELS:
                if ch in conn.sids and ch not in missing:
                    await conn.send("update_subscription", {"sids": [conn.sids[ch]], "market_tickers": add, "action": "add_markets"})
        if rem:
            for ch in MARKET_CHANNELS:
                if ch in conn.sids:
                    await conn.send("update_subscription", {"sids": [conn.sids[ch]], "market_tickers": rem, "action": "delete_markets"})
            self.books.drop(rem)
        self._subscribed = want

    async def session(self, ws) -> None:
        """Run one connected market socket until it closes."""
        conn = _Conn(ws)
        self._conn = conn
        self._subscribed = set()
        await conn.send("subscribe", {"channels": ["market_lifecycle_v2"]})
        await self._sync_watch()
        async for raw in ws:
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(data, dict):
                await self.handle(conn, data)

    # ----- fills socket (warp) -----

    async def handle_fill(self, data: dict) -> None:
        typ = data.get("type")
        msg = data.get("msg") if isinstance(data.get("msg"), dict) else {}
        if typ == "fill":
            self.fills += 1
            self._emit("fill", msg)
        elif typ == "user_order":
            self.user_orders += 1
            self._emit("user_order", msg)
        elif typ == "error":
            self.errors.append(f"fills {(msg or {}).get('code')}: {(msg or {}).get('msg')}")

    async def fill_session(self, ws) -> None:
        conn = _Conn(ws)
        await conn.send("subscribe", {"channels": ["fill", "user_orders"]})
        async for raw in ws:
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(data, dict):
                await self.handle_fill(data)

    # ----- connection loops -----

    def _delay(self, attempt: int, throttled: bool) -> float:
        base = min(BACKOFF_MAX, BACKOFF_MIN * (2 ** attempt)) * (4 if throttled else 1)
        base = min(base, BACKOFF_MAX * 2)
        return base / 2 + self._rng() * base / 2

    async def _loop_forever(self, which: str) -> None:
        attempt = 0
        primary_failures = 0
        while True:
            signer = self.signer_fn()
            if not self.enabled:
                self._set_reason(which, "off (WS_ENABLED=0)")
                await asyncio.sleep(30)
                continue
            if not signer.available:
                # Kalshi requires a signed handshake even for public channels.
                self._set_reason(which, "needs Kalshi keys — using REST polling")
                await asyncio.sleep(30)
                continue
            url = self.url_fn() if which == "market" else self.warp_url_fn()
            if which == "market" and primary_failures >= 3 and url in FALLBACK_WS:
                url = FALLBACK_WS[url]
            throttled = False
            try:
                headers = signer.headers("GET", WS_SIGN_PATH)
                async with self._connect(url, headers) as ws:
                    attempt = 0
                    primary_failures = 0
                    self._set_connected(which, True, url)
                    if which == "market":
                        await self.session(ws)
                    else:
                        await self.fill_session(ws)
                self._set_reason(which, "closed by server — reconnecting")
            except asyncio.CancelledError:
                self._set_connected(which, False, None)
                raise
            except Exception as exc:
                status = _handshake_status(exc)
                throttled = status == 429
                primary_failures += 1
                self.errors.append(f"{which}: {type(exc).__name__}: {str(exc)[:160]}")
                self._set_reason(which, "connection limit (429) — backing off" if throttled else f"reconnecting after {type(exc).__name__}")
            finally:
                if which == "market":
                    self._conn = None
                self._set_connected(which, False, None, keep_reason=True)
            self.reconnects += 1
            await asyncio.sleep(self._delay(attempt, throttled))
            attempt = min(attempt + 1, 8)

    def _set_reason(self, which: str, reason: str) -> None:
        if which == "market":
            self.reason = reason
        else:
            self.warp_reason = reason

    def _set_connected(self, which: str, on: bool, url: Optional[str], keep_reason: bool = False) -> None:
        if which == "market":
            self.connected = on
            if on:
                self.url_in_use = url
                self.reason = "connected"
        else:
            self.warp_connected = on
            if on:
                self.warp_reason = "connected"

    async def run_forever(self) -> None:
        self._loop = asyncio.get_running_loop()
        await asyncio.gather(self._loop_forever("market"), self._loop_forever("fills"))

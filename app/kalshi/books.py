"""Local order books + quotes built from Kalshi WebSocket messages.

docs.kalshi.com/websockets/orderbook-updates (read Nov 9 2026):
* ``orderbook_snapshot`` first (``yes_dollars_fp`` / ``no_dollars_fp`` as
  ``[price_dollars, count_fp]`` bid levels), then ``orderbook_delta`` with
  ``price_dollars``, ``delta_fp`` and ``side``. A snapshot always replaces the
  local book for that market.
* ``seq`` is contiguous per subscription (sid). A gap means a missed message:
  request ``get_snapshot`` for all subscribed markets and rebuild.
* Kalshi books are bids only. YES ask = 1 - best NO bid; NO ask = 1 - best YES bid.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Callable, Optional


def _f(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class LocalBook:
    def __init__(self, ticker: str, clock: Callable[[], float] = time.monotonic):
        self.ticker = ticker
        self._clock = clock
        self.yes: dict[float, float] = {}
        self.no: dict[float, float] = {}
        self.updated = 0.0
        self.synced = False  # True after a snapshot; False after a seq gap
        self.clock_value: Optional[int] = None

    def apply_snapshot(self, msg: dict) -> None:
        self.yes, self.no = {}, {}
        for side, key in ((self.yes, "yes_dollars_fp"), (self.no, "no_dollars_fp")):
            for level in msg.get(key) or msg.get(key.replace("_fp", "")) or []:
                try:
                    price, qty = float(level[0]), float(level[1])
                except (TypeError, ValueError, IndexError):
                    continue
                if qty > 0:
                    side[round(price, 4)] = qty
        self.clock_value = msg.get("clock")
        self.synced = True
        self.updated = self._clock()

    def apply_delta(self, msg: dict) -> None:
        price, delta = _f(msg.get("price_dollars")), _f(msg.get("delta_fp"))
        side = str(msg.get("side") or "").lower()
        if price is None or delta is None or side not in {"yes", "no"}:
            return
        book = self.yes if side == "yes" else self.no
        key = round(price, 4)
        qty = book.get(key, 0.0) + delta
        if qty > 1e-9:
            book[key] = qty
        else:
            book.pop(key, None)
        self.clock_value = msg.get("clock", self.clock_value)
        self.updated = self._clock()

    def quote(self) -> dict:
        by = max(self.yes) if self.yes else None
        bn = max(self.no) if self.no else None
        return {
            "yes_bid": by,
            "no_bid": bn,
            "yes_ask": round(1.0 - bn, 4) if bn is not None else None,
            "no_ask": round(1.0 - by, 4) if by is not None else None,
            "yes_ask_size": self.no.get(bn) if bn is not None else None,
            "no_ask_size": self.yes.get(by) if by is not None else None,
        }


class SeqTracker:
    """Per-subscription contiguous ``seq`` check."""

    def __init__(self):
        self.last: dict[int, int] = {}

    def check(self, sid: Any, seq: Any) -> bool:
        if sid is None or seq is None:
            return True
        sid, seq = int(sid), int(seq)
        prev = self.last.get(sid)
        self.last[sid] = seq
        return prev is None or seq == prev + 1

    def reset(self, sid: Optional[int] = None) -> None:
        if sid is None:
            self.last.clear()
        else:
            self.last.pop(int(sid), None)


class BookStore:
    """Thread-safe store read by the scanner/auto-bet threads, written by the WS loop."""

    def __init__(self, fresh_s: float = 30.0, clock: Callable[[], float] = time.monotonic):
        self._lock = threading.Lock()
        self._clock = clock
        self.fresh_s = fresh_s
        self.books: dict[str, LocalBook] = {}
        self.tickers: dict[str, dict] = {}  # ticker channel
        self.lifecycle: dict[str, dict] = {}  # market_lifecycle_v2
        self.trades: deque = deque(maxlen=500)

    def snapshot(self, msg: dict) -> Optional[str]:
        t = msg.get("market_ticker")
        if not t:
            return None
        with self._lock:
            self.books.setdefault(t, LocalBook(t, self._clock)).apply_snapshot(msg)
        return t

    def delta(self, msg: dict) -> Optional[str]:
        t = msg.get("market_ticker")
        with self._lock:
            book = self.books.get(t) if t else None
            if book is None or not book.synced:
                return None  # wait for the snapshot
            book.apply_delta(msg)
        return t

    def mark_unsynced(self, tickers=None) -> None:
        with self._lock:
            for t, b in self.books.items():
                if tickers is None or t in tickers:
                    b.synced = False

    def ticker(self, msg: dict) -> Optional[str]:
        t = msg.get("market_ticker")
        if t:
            with self._lock:
                self.tickers[t] = {**msg, "_at": self._clock()}
        return t

    def trade(self, msg: dict) -> None:
        with self._lock:
            self.trades.append({**msg, "_at": self._clock()})

    def lifecycle_event(self, msg: dict) -> Optional[str]:
        t = msg.get("market_ticker")
        if t:
            with self._lock:
                prev = self.lifecycle.get(t, {})
                self.lifecycle[t] = {**prev, **msg, "_at": self._clock()}
        return t

    def drop(self, tickers) -> None:
        with self._lock:
            for t in tickers:
                self.books.pop(t, None)
                self.tickers.pop(t, None)

    def quote(self, ticker: str) -> Optional[dict]:
        """Freshest live quote: synced book first, then the ticker channel."""
        now = self._clock()
        with self._lock:
            b = self.books.get(ticker)
            if b is not None and b.synced and now - b.updated <= self.fresh_s * 20:
                # Books only change on deltas; a quiet synced book is still current.
                q = b.quote()
                if q["yes_ask"] is not None or q["no_ask"] is not None:
                    return {**q, "source": "ws_book", "age_s": round(now - b.updated, 2)}
            tk = self.tickers.get(ticker)
            if tk is not None and now - tk["_at"] <= self.fresh_s:
                ya, yb = _f(tk.get("yes_ask_dollars")), _f(tk.get("yes_bid_dollars"))
                return {
                    "yes_bid": yb,
                    "yes_ask": ya,
                    "no_bid": round(1 - ya, 4) if ya is not None else None,
                    "no_ask": round(1 - yb, 4) if yb is not None else None,
                    "source": "ws_ticker",
                    "age_s": round(now - tk["_at"], 2),
                }
        return None

    def overlay(self, market: dict) -> dict:
        """Copy of a REST market dict with live WS prices, when we have them."""
        q = self.quote(str(market.get("ticker") or ""))
        if not q:
            return market
        out = dict(market)
        for side in ("yes", "no"):
            for kind in ("ask", "bid"):
                v = q.get(f"{side}_{kind}")
                if v is not None:
                    out[f"{side}_{kind}_dollars"] = f"{v:.4f}"
                    out.pop(f"{side}_{kind}", None)  # never mix stale cents fields
        out["_quote_source"] = q["source"]
        out["_quote_age_s"] = q["age_s"]
        return out

    def stats(self) -> dict:
        with self._lock:
            synced = sum(1 for b in self.books.values() if b.synced)
            return {"books": len(self.books), "synced_books": synced, "ticker_quotes": len(self.tickers),
                    "lifecycle_markets": len(self.lifecycle), "trades_buffered": len(self.trades)}

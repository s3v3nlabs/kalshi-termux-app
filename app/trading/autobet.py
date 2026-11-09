"""Auto-bet engine: an opt-in background loop that places orders from the scanner.

OFF by default. The on/off choice is saved in the Store (``autobet_enabled``).
Each cycle (when ON):

1. size against the bankroll (live Kalshi balance when ARM LIVE is set and
   paper trading is off; otherwise ``AUTOBET_PAPER_BANKROLL``),
2. run the normal scan (mispricing / Nash ranking, Brier-based Kelly halving),
3. keep rows where the model has a positive edge on a side and Kelly sizes at
   least one contract,
4. drop anything the geo gate blocks (sports fail closed, same ``geo.check``
   as manual orders), and any market or event already held, resting, or
   ordered this cycle,
5. send the top ``AUTOBET_TOP_N`` through the existing ``OrderGate.place``
   (Create Order V2). The gate decides paper vs live exactly as for a tapped
   order: real money only when live trading is allowed AND ARM LIVE is set.

Every auto order is written to ``auto_orders`` and its fill to ``bets`` with
``source='auto'`` so it feeds calibration and training like any other bet.
Turning Auto-bet OFF takes effect before the next order is sent, even in the
middle of a cycle.

Model output is an estimate; any auto bet can lose.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

from app import geo
from app.config import settings
from app.kalshi.client import MAX_CONTRACTS_PER_ORDER
from app.strategy.kelly import kelly_size

log = logging.getLogger(__name__)

SETTING_KEY = "autobet_enabled"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def event_of(row_or_ticker: Any) -> str:
    if isinstance(row_or_ticker, dict):
        ev = row_or_ticker.get("event_ticker")
        if ev:
            return str(ev).upper()
        row_or_ticker = row_or_ticker.get("ticker") or ""
    t = str(row_or_ticker or "").upper()
    return t.rsplit("-", 1)[0] if "-" in t else t


def _local_midnight_utc_iso(now: Optional[datetime] = None) -> str:
    local = (now or datetime.now(timezone.utc)).astimezone()
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc).isoformat()


def best_auto_side(row: dict, bankroll: float) -> Optional[dict]:
    """Pick the side with the larger positive expected profit, sized by the
    scanner's own Kelly settings (``kelly_scale`` carries the Brier halving)."""
    p_yes = (row.get("nash") or {}).get("model_prob_yes")
    if p_yes is None:
        p_yes = (row.get("model") or {}).get("p_yes")
    if p_yes is None:
        return None
    scale = float(row.get("kelly_scale") or 1.0)
    best = None
    for side, price, p in (
        ("yes", row.get("yes_ask"), float(p_yes)),
        ("no", row.get("no_ask"), 1.0 - float(p_yes)),
    ):
        try:
            price = float(price)
        except (TypeError, ValueError):
            continue
        if not 0.0 < price < 1.0:
            continue
        k = kelly_size(
            model_prob=p,
            contract_price=price,
            bankroll=bankroll,
            fraction=settings.kelly_fraction * scale,
            max_fraction=settings.kelly_max_fraction * scale,
        )
        if k.edge <= 0 or k.contracts < 1:
            continue
        contracts = min(int(k.contracts), MAX_CONTRACTS_PER_ORDER)
        cents = max(1, min(99, int(round(price * 100))))
        pick = {
            "side": side,
            "price": price,
            "price_cents": cents,
            "count": contracts,
            "model_p": round(p, 6),
            "edge": float(k.edge),
            "ev_total": round(float(k.edge) * contracts, 4),
            "kelly_pct": k.kelly_pct_of_bankroll,
        }
        if best is None or pick["ev_total"] > best["ev_total"]:
            best = pick
    return best


def select_candidates(
    rows: Iterable[dict],
    *,
    bankroll: float,
    state: Optional[str],
    held_tickers: Iterable[str] = (),
    held_events: Iterable[str] = (),
    top_n: int = 5,
    geo_check: Callable[[str, Optional[str]], dict] = geo.check,
) -> tuple[list[dict], dict]:
    """Rows arrive in scanner order (mispricing / Nash first). Returns
    (picks, skip_counts). One pick per event; held / resting markets skipped."""
    held_t = {str(t).upper() for t in held_tickers}
    held_e = {str(e).upper() for e in held_events}
    picks: list[dict] = []
    skipped = {"no_edge": 0, "geo_blocked": 0, "held": 0, "same_event": 0}
    for row in rows:
        if len(picks) >= max(0, int(top_n)):
            break
        ticker = str(row.get("ticker") or "").upper()
        if not ticker:
            continue
        g = geo_check(ticker, state)
        if not g.get("allowed"):
            skipped["geo_blocked"] += 1
            continue
        ev = event_of(row)
        if ticker in held_t or ev in held_e:
            skipped["held"] += 1
            continue
        pick = best_auto_side(row, bankroll)
        if pick is None:
            skipped["no_edge"] += 1
            continue
        if any(p["event_ticker"] == ev for p in picks):
            skipped["same_event"] += 1
            continue
        picks.append(
            {
                **pick,
                "ticker": row.get("ticker"),
                "event_ticker": ev,
                "title": row.get("title"),
                "mispricing": row.get("mispricing"),
                "nash_payoff": row.get("nash_payoff_gemini"),
                "uncalibrated": bool(row.get("model_uncalibrated")),
            }
        )
    return picks, skipped


def _position_tickers(data: Any) -> set[str]:
    out = set()
    for p in (data or {}).get("market_positions") or []:
        try:
            pos = float(p.get("position_fp") if p.get("position_fp") is not None else p.get("position") or 0)
        except (TypeError, ValueError):
            pos = 0.0
        if pos and p.get("ticker"):
            out.add(str(p["ticker"]).upper())
    return out


def _order_tickers(data: Any) -> set[str]:
    return {str(o["ticker"]).upper() for o in (data or {}).get("orders") or [] if o.get("ticker")}


class AutoBetEngine:
    def __init__(
        self,
        *,
        store,
        ledger,
        orders,
        scan_fn: Callable[[float], dict],
        client_fn: Callable[[], Any],
        state_fn: Optional[Callable[[], Optional[str]]] = None,
        balance_fn: Optional[Callable[[], Optional[float]]] = None,
        interval_s: Optional[float] = None,
        top_n: Optional[int] = None,
        paper_bankroll: Optional[float] = None,
    ):
        self.store = store
        self.ledger = ledger
        self.orders = orders
        self.scan_fn = scan_fn
        self.client_fn = client_fn
        self.state_fn = state_fn or (lambda: geo.resolve_state("127.0.0.1"))
        self.balance_fn = balance_fn
        self.interval_s = float(interval_s if interval_s is not None else _env_float("AUTOBET_INTERVAL_S", 30.0))
        self.top_n = int(top_n if top_n is not None else _env_int("AUTOBET_TOP_N", 5))
        self.paper_bankroll = float(
            paper_bankroll if paper_bankroll is not None else _env_float("AUTOBET_PAPER_BANKROLL", 1000.0)
        )
        self._enabled = (store.get_setting(SETTING_KEY, "0") or "0") == "1"
        self._cycle_lock = threading.Lock()
        self._wake: Optional[asyncio.Event] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.last_cycle: Optional[dict] = None

    # ----- toggle -----

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, on: bool) -> dict:
        self._enabled = bool(on)
        self.store.set_setting(SETTING_KEY, "1" if on else "0")
        self.store.log_activity("autobet", "Auto-bet ON" if on else "Auto-bet OFF")
        if self._wake is not None and self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._wake.set)
            except RuntimeError:  # loop closed
                pass
        return self.status()

    def mode(self) -> str:
        if not self._enabled:
            return "off"
        return "live" if self.orders.live_armed else "paper"

    # ----- one cycle -----

    def _bankroll(self, live: bool) -> Optional[float]:
        if not live:
            return self.paper_bankroll
        if self.balance_fn is not None:
            return self.balance_fn()
        data = self.client_fn().get_balance()
        fp = data.get("balance_dollars")
        if fp not in (None, ""):
            return float(fp)
        bal = data.get("balance")
        return float(bal) / 100.0 if isinstance(bal, (int, float)) else None

    def _held(self, live: bool) -> tuple[set[str], set[str], list[str]]:
        tickers: set[str] = set()
        notes: list[str] = []
        for b in self.ledger.bets_sync(settled=False):
            tickers.add(str(b["ticker"]).upper())
        if live:
            client = self.client_fn()
            try:
                tickers |= _position_tickers(client.get_positions(limit=200))
            except Exception as exc:
                notes.append(f"positions: {exc}")
            try:
                tickers |= _order_tickers(client.get_orders(status="resting", limit=200))
            except Exception as exc:
                notes.append(f"resting orders: {exc}")
        # Resting auto orders from this app (live, unfilled) also count as pending.
        for o in self.ledger.auto_orders_sync(limit=500):
            if o["status"] == "RESTING":
                tickers.add(str(o["ticker"]).upper())
        return tickers, {event_of(t) for t in tickers}, notes

    def run_cycle(self) -> dict:
        if not self._enabled:
            return {"ran": False, "reason": "off"}
        if not self._cycle_lock.acquire(blocking=False):
            return {"ran": False, "reason": "cycle already running"}
        started = datetime.now(timezone.utc)
        summary: dict[str, Any] = {"ts": started.isoformat(), "ran": True, "placed": 0, "picked": 0, "orders": []}
        try:
            live = bool(self.orders.live_armed)
            summary["mode"] = "live" if live else "paper"
            bankroll = self._bankroll(live)
            if not bankroll or bankroll <= 0:
                summary["error"] = "No bankroll to size against (balance 0 or unreadable)."
                return summary
            summary["bankroll"] = round(bankroll, 2)
            scan = self.scan_fn(bankroll)
            rows = scan.get("markets") or []
            state = self.state_fn()
            held_t, held_e, notes = self._held(live)
            if notes:
                summary["notes"] = notes
            picks, skipped = select_candidates(
                rows, bankroll=bankroll, state=state, held_tickers=held_t, held_events=held_e, top_n=self.top_n
            )
            summary.update({"scanned": len(rows), "picked": len(picks), "skipped": skipped, "state": state})
            remaining = bankroll
            for pick in picks:
                if not self._enabled:  # one-tap OFF stops before the next order
                    summary["stopped"] = "Auto-bet turned off mid-cycle"
                    break
                # Re-check the gate at send time: a blocked sports order never leaves here.
                if not geo.check(str(pick["ticker"]), self.state_fn()).get("allowed"):
                    continue
                cost = pick["count"] * pick["price_cents"] / 100.0
                if cost > remaining:
                    pick["count"] = int(remaining // (pick["price_cents"] / 100.0))
                    if pick["count"] < 1:
                        continue
                    cost = pick["count"] * pick["price_cents"] / 100.0
                summary["orders"].append(self._place(pick, live))
                if summary["orders"][-1]["ok"]:
                    summary["placed"] += 1
                    remaining -= cost
        except Exception as exc:
            log.exception("auto-bet cycle failed")
            summary["error"] = str(exc)
            self.store.log_activity("error", f"Auto-bet cycle failed: {exc}")
        finally:
            summary["duration_s"] = round((datetime.now(timezone.utc) - started).total_seconds(), 2)
            self.last_cycle = summary
            self._cycle_lock.release()
        return summary

    def _place(self, pick: dict, live: bool) -> dict:
        try:  # bet snapshot so the bets row gets the model probability
            self.ledger.add_model_runs_sync(
                [{"ticker": pick["ticker"], "model_prob": pick["model_p"] if pick["side"] == "yes" else 1 - pick["model_p"],
                  "features": {"source": "auto", "edge": pick["edge"]}, "kind": "bet"}]
            )
        except Exception:
            pass
        res = self.orders.place(
            ticker=str(pick["ticker"]),
            side=pick["side"],
            count=int(pick["count"]),
            price_cents=int(pick["price_cents"]),
            order_type="limit",
            source="auto",
        )
        ok = bool(res.get("ok"))
        if not ok:
            status = "ERROR"
        elif res.get("dry_run"):
            status = "PAPER"
        else:
            ack = res.get("response") or {}
            try:
                remaining = float(ack.get("remaining_count") or 0)
            except (TypeError, ValueError):
                remaining = 0.0
            status = "RESTING" if remaining > 0 else "FILLED"
        row = {
            "ticker": pick["ticker"],
            "event_ticker": pick["event_ticker"],
            "side": pick["side"],
            "count": pick["count"],
            "price_cents": pick["price_cents"],
            "mode": "paper" if res.get("dry_run", True) else "live",
            "status": status,
            "ev": pick["ev_total"],
            "order_log_id": res.get("order_log_id"),
            "bet_id": res.get("bet_id"),
            "detail": {"edge": pick["edge"], "model_p": pick["model_p"], "mispricing": pick.get("mispricing"),
                       "error": None if ok else res.get("error")},
        }
        try:
            self.ledger.add_auto_order_sync(row)
        except Exception as exc:
            self.store.log_activity("error", f"Could not log auto order: {exc}")
        self.store.log_activity(
            "autobet",
            f"[{row['mode'].upper()}] Auto {pick['side'].upper()} {pick['count']}x {pick['ticker']} @ {pick['price_cents']}¢ "
            f"· est. edge {pick['edge'] * 100:.1f}¢/contract · {status}",
        )
        return {"ok": ok, **{k: row[k] for k in ("ticker", "side", "count", "price_cents", "mode", "status")},
                "error": None if ok else res.get("error")}

    # ----- status -----

    def status(self) -> dict:
        since = _local_midnight_utc_iso()
        today = [o for o in self.ledger.auto_orders_sync(since=since, limit=1000)]
        auto_bets = [b for b in self.ledger.bets_sync() if (b.get("source") or "manual") == "auto"]
        settled = [b for b in auto_bets if b["outcome"] is not None]
        settled_today = [b for b in settled if str(b["ts"]) >= since]
        return {
            "enabled": self._enabled,
            "mode": self.mode(),
            "trades_real_money": self._enabled and bool(self.orders.live_armed),
            "interval_s": self.interval_s,
            "top_n": self.top_n,
            "paper_bankroll": self.paper_bankroll,
            "last_cycle": self.last_cycle,
            "orders_today": sum(1 for o in today if o["status"] != "ERROR"),
            "errors_today": sum(1 for o in today if o["status"] == "ERROR"),
            "recent": today[:10],
            "pnl": {
                "realized_total": round(sum(float(b["pnl"] or 0) for b in settled), 2),
                "realized_today": round(sum(float(b["pnl"] or 0) for b in settled_today), 2),
                "open_stake": round(sum(float(b["stake"] or 0) for b in auto_bets if b["outcome"] is None), 2),
                "settled_n": len(settled),
                "open_n": sum(1 for b in auto_bets if b["outcome"] is None),
                "wins": sum(1 for b in settled if b["outcome"] == 1),
            },
            "note": "Model estimates only. Auto-bet can lose money; settled results feed calibration.",
        }

    # ----- background loop -----

    async def run_forever(self) -> None:
        self._wake = asyncio.Event()
        self._loop = asyncio.get_running_loop()
        while True:
            if self._enabled:
                try:
                    await asyncio.to_thread(self.run_cycle)
                except Exception:  # pragma: no cover - run_cycle already catches
                    log.exception("auto-bet loop")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.interval_s if self._enabled else 3600)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

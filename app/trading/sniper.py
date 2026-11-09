"""Near-finish sniper: late-game sports markets where the live game state says
the ask is too cheap after Kalshi's fee.

OFF by default (Store ``sniper_enabled``). When ON, each cycle:

1. stops unless Kalshi says trading is active (exchange gate, fail closed),
2. lists open full-game markets (NFL/NBA/MLB/NHL/NCAAF/NCAAB) whose expected
   finish is near now,
3. reads the game state from Kalshi ``live_data`` (milestone -> live_data/batch),
   falling back to the ESPN scoreboard,
4. keeps games in progress inside the last ``SNIPER_NEAR_FRAC`` of regulation
   (default 25%: 4th quarter / 3rd period / 7th inning on),
5. builds a model probability from score, time left and (when available)
   ESPN's live win %: ``p = Phi(lead / (sigma * sqrt(time_left)))``,
   clamped to [0.02, 0.98] — never 0 or 1,
6. prices both sides at the ask (live WebSocket book when watched, else
   REST), with the series' taker fee, and fires a limit order when net EV
   per contract >= ``SNIPER_EV_MIN`` (default 3c), sized by fee-aware Kelly
   with the normal Kelly fraction and max-fraction caps,
7. re-checks the sports geo gate and the exchange gate before every order,
   one order per event, skips anything already held or resting,
8. cancels any live sniper order still resting after ``SNIPER_CANCEL_S``.

Orders go through ``OrderGate.place(source="sniper")`` so PAPER vs LIVE works
exactly like a tapped order. Bets are logged with ``source='sniper'``.
A late lead is not a sure thing; overtime, reviews and comebacks happen.
Every sniper bet is an estimate and can lose.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional

from app import geo
from app.config import settings
from app.kalshi.client import MAX_CONTRACTS_PER_ORDER
from app.strategy.fees import fee_aware_kelly, series_of

log = logging.getLogger(__name__)

SETTING_KEY = "sniper_enabled"

GAME_SERIES: dict[str, str] = {
    "KXNFLGAME": "nfl",
    "KXNBAGAME": "nba",
    "KXMLBGAME": "mlb",
    "KXNHLGAME": "nhl",
    "KXNCAAFGAME": "ncaaf",
    "KXNCAABGAME": "ncaab",
}

# Regulation length (periods, seconds per period) and the final-margin
# standard deviation used by the normal win-probability model.
LEAGUE_SHAPE: dict[str, dict[str, float]] = {
    "nfl": {"periods": 4, "period_s": 900, "sigma": 13.5},
    "ncaaf": {"periods": 4, "period_s": 900, "sigma": 16.0},
    "nba": {"periods": 4, "period_s": 720, "sigma": 12.0},
    "ncaab": {"periods": 2, "period_s": 1200, "sigma": 11.0},
    "nhl": {"periods": 3, "period_s": 1200, "sigma": 1.8},
    "mlb": {"periods": 9, "period_s": 0, "sigma": 4.0},
}


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


# ----- pure helpers (picklable; used by the compute pool) -----

def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _phi_inv(p: float) -> float:
    p = min(0.999, max(0.001, p))
    lo, hi = -8.0, 8.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if _phi(mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def clock_seconds(value: Any) -> Optional[float]:
    """'05:32' -> 332, '0:07.4' -> 7.4, 95 -> 95."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.match(r"^\s*(\d+):(\d+(?:\.\d+)?)\s*$", str(value))
    if m:
        return int(m.group(1)) * 60 + float(m.group(2))
    try:
        return float(value)
    except ValueError:
        return None


def remaining_fraction(league: str, details: dict) -> Optional[float]:
    """Share of regulation still to play (0..1), or None when unknown."""
    shape = LEAGUE_SHAPE.get(league)
    if not shape:
        return None
    try:
        period = int(details.get("period") or details.get("quarter") or 0)
    except (TypeError, ValueError):
        return None
    if period <= 0:
        return 1.0
    n = int(shape["periods"])
    if league == "mlb":
        # Innings: after the start of the k-th inning ~ (n - k + 0.5) innings left.
        return max(0.0, min(1.0, (n - period + 0.5) / n))
    left = clock_seconds(details.get("period_remaining_time") or details.get("clock"))
    if left is None:
        return None
    if period > n:  # overtime
        return max(0.0, left) / (n * shape["period_s"])
    total = n * shape["period_s"]
    return max(0.0, min(1.0, ((n - period) * shape["period_s"] + max(0.0, left)) / total))


def espn_remaining_fraction(league: str, detail: str) -> Optional[float]:
    """ESPN shortDetail ('2:31 - 4th', '5:00 - 2nd Half', 'Bot 8th', 'End of 3rd',
    'Final') -> share of regulation left, None when it can't be read."""
    d = str(detail or "")
    if re.search(r"\bfinal\b", d, re.I):
        return 0.0
    m = re.search(r"(\d+:\d+(?:\.\d+)?)\s*-\s*(\d+)(?:st|nd|rd|th)", d)
    if m:
        return remaining_fraction(league, {"period": int(m.group(2)), "period_remaining_time": m.group(1)})
    m = re.search(r"\b(?:top|bot|bottom|mid|middle|end)\w*\s+(?:of\s+)?(?:the\s+)?(\d+)", d, re.I)
    if m and league == "mlb":
        return remaining_fraction(league, {"period": int(m.group(1))})
    m = re.search(r"\bend of (?:the )?(\d+)(?:st|nd|rd|th)", d, re.I)
    if m:
        return remaining_fraction(league, {"period": int(m.group(1)), "period_remaining_time": "0:00"})
    return None


def win_probability(league: str, lead: float, frac_left: float, prior_p: Optional[float] = None,
                    live_win_p: Optional[float] = None) -> float:
    """P(team leading by ``lead`` wins). Normal model on the final margin,
    an optional pregame prior that fades with time, blended 50/50 with a
    provider's live win % when one exists. Clamped to [0.02, 0.98]."""
    sigma = LEAGUE_SHAPE.get(league, {}).get("sigma", 10.0)
    f = max(0.0025, float(frac_left))
    z = float(lead) / (sigma * math.sqrt(f))
    if prior_p is not None and 0.0 < prior_p < 1.0:
        z += _phi_inv(prior_p) * math.sqrt(f)
    p = _phi(z)
    if lead == 0 and frac_left <= 0:
        p = 0.5  # tied at the end of regulation: overtime coin-flip-ish
    if live_win_p is not None and 0.0 <= live_win_p <= 1.0:
        p = 0.5 * p + 0.5 * float(live_win_p)
    return max(0.02, min(0.98, p))


def yes_is_home(ticker: str, title: Optional[str] = None) -> Optional[bool]:
    """KXNBAGAME-26NOV09WASDET-DET -> True (event code is AWAY+HOME)."""
    parts = str(ticker or "").upper().split("-")
    if len(parts) < 3:
        return None
    suffix = parts[-1]
    code = re.sub(r"^\d{2}[A-Z]{3}\d{2}(\d{4})?", "", parts[1])
    if suffix in {"TIE", "DRAW"} or not code:
        return None
    starts, ends = code.startswith(suffix), code.endswith(suffix)
    if starts and not ends:
        return False
    if ends and not starts:
        return True
    if title and " at " in title:
        away, home = title.split(" at ", 1)
        if home.upper().startswith(suffix):
            return True
        if away.upper().startswith(suffix):
            return False
    return None


def game_state_from_live(league: str, live: dict, home: bool) -> Optional[dict]:
    """Kalshi live_data details -> {status, lead (YES view), frac_left, ...}."""
    d = (live or {}).get("details") or {}
    status = str(d.get("status") or "").lower()
    try:
        hp, ap = float(d.get("home_points") or 0), float(d.get("away_points") or 0)
    except (TypeError, ValueError):
        return None
    lead = (hp - ap) if home else (ap - hp)
    out = {"source": "kalshi_live_data", "status": status, "lead": lead,
           "period": d.get("period"), "clock": d.get("period_remaining_time") or d.get("clock"),
           "result_pending": bool(d.get("result_pending")), "winner": d.get("winner") or ""}
    out["frac_left"] = 0.0 if status == "closed" else remaining_fraction(league, d)
    return out


def score_candidate(c: dict) -> dict:
    """Model probability + both sides' fee-aware sizing for one market.

    Picklable on purpose: the compute pool can run it in another process.
    ``c``: league, lead, frac_left, live_win_p, prior_p, yes_ask, no_ask,
    bankroll, fraction, max_fraction, fee_multiplier, ev_min.
    """
    if c.get("status") == "closed":
        p_yes = 0.98 if c["lead"] > 0 else 0.02 if c["lead"] < 0 else 0.5
    else:
        p_yes = win_probability(c["league"], c["lead"], c["frac_left"], c.get("prior_p"), c.get("live_win_p"))
    best = None
    for side, ask, p in (("yes", c.get("yes_ask"), p_yes), ("no", c.get("no_ask"), 1.0 - p_yes)):
        try:
            ask = float(ask)
        except (TypeError, ValueError):
            continue
        if not 0.0 < ask < 1.0:
            continue
        cents = max(1, min(99, int(round(ask * 100))))
        price = cents / 100.0
        k = fee_aware_kelly(p, price, float(c["bankroll"]), fraction=float(c["fraction"]),
                            max_fraction=float(c["max_fraction"]), multiplier=float(c.get("fee_multiplier") or 1.0),
                            max_contracts=int(c.get("max_contracts") or MAX_CONTRACTS_PER_ORDER))
        if k.contracts < 1 or k.net_edge < float(c["ev_min"]):
            continue
        pick = {"side": side, "price_cents": cents, "count": k.contracts, "model_p": round(p, 4),
                "edge": k.gross_edge, "edge_net": k.net_edge, "fee_total": k.fee_total, "ev_total": k.ev_total_net}
        if best is None or pick["ev_total"] > best["ev_total"]:
            best = pick
    return {"ticker": c.get("ticker"), "p_yes": round(p_yes, 4), "pick": best}


def _iso_ts(v: Any) -> Optional[float]:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _ask(m: dict, side: str) -> Optional[float]:
    v = m.get(f"{side}_ask_dollars")
    if v in (None, ""):
        c = m.get(f"{side}_ask")
        return float(c) / 100.0 if isinstance(c, (int, float)) and c > 0 else None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


class Sniper:
    def __init__(self, *, store, ledger, orders, client_fn: Callable[[], Any],
                 state_fn: Optional[Callable[[], Optional[str]]] = None,
                 bankroll_fn: Optional[Callable[[bool], Optional[float]]] = None,
                 held_fn: Optional[Callable[[bool], set]] = None,
                 exchange_gate: Optional[Any] = None,
                 fee_multiplier_fn: Optional[Callable[[str], float]] = None,
                 quotes_fn: Optional[Callable[[list], dict]] = None,
                 watch_fn: Optional[Callable[[list], None]] = None,
                 espn_fn: Optional[Callable[[dict], Any]] = None,
                 score_fn: Optional[Callable[[list], list]] = None,
                 clock: Callable[[], float] = time.time):
        self.store, self.ledger, self.orders = store, ledger, orders
        self.client_fn = client_fn
        self.state_fn = state_fn or (lambda: geo.resolve_state("127.0.0.1"))
        self.bankroll_fn = bankroll_fn or (lambda live: _env_float("AUTOBET_PAPER_BANKROLL", 1000.0))
        self.held_fn = held_fn or (lambda live: set())
        self.exchange_gate = exchange_gate
        self.fee_multiplier_fn = fee_multiplier_fn
        self.quotes_fn = quotes_fn
        self.watch_fn = watch_fn
        self.espn_fn = espn_fn
        self.score_fn = score_fn or (lambda cands: [score_candidate(c) for c in cands])
        self._clock = clock
        self.ev_min = _env_float("SNIPER_EV_MIN", 0.03)
        self.near_frac = _env_float("SNIPER_NEAR_FRAC", 0.25)
        self.window_min = _env_float("SNIPER_WINDOW_MIN", 30.0)
        self.cancel_s = _env_float("SNIPER_CANCEL_S", 20.0)
        self.max_per_cycle = _env_int("SNIPER_MAX_ORDERS", 3)
        self.base_interval_s = _env_float("SNIPER_INTERVAL_S", 15.0)
        self.fast_interval_s = _env_float("SNIPER_FAST_INTERVAL_S", 3.0)
        self.event_cooldown_s = _env_float("SNIPER_EVENT_COOLDOWN_S", 300.0)
        self.min_gap_s = _env_float("SNIPER_MIN_GAP_S", 2.0)
        self._enabled = (store.get_setting(SETTING_KEY, "0") or "0") == "1"
        self._milestones: dict[str, Optional[dict]] = {}
        self._fired_events: dict[str, float] = {}
        self._pending: dict[str, dict] = {}  # order_id -> {ticker, deadline}
        self._lock = threading.Lock()
        self._wake: Optional[asyncio.Event] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._last_start = 0.0
        self.hot = False  # a watched game is near the finish -> fast cadence
        self.watched: list[str] = []
        self.pokes = 0
        self.last_cycle: Optional[dict] = None

    # ----- toggle -----

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, on: bool) -> None:
        self._enabled = bool(on)
        self.store.set_setting(SETTING_KEY, "1" if on else "0")
        self.store.log_activity("sniper", "Near-finish sniper ON" if on else "Near-finish sniper OFF")
        if not on and self.watch_fn:
            try:
                self.watch_fn([])
            except Exception:
                pass
        self._wake_now()

    def _wake_now(self) -> None:
        if self._wake is not None and self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._wake.set)
            except RuntimeError:
                pass

    def poke(self, ticker: str = "") -> bool:
        """WebSocket event on a watched market (price, trade, lifecycle)."""
        if not self._enabled or (ticker and ticker.upper() not in self.watched):
            return False
        if time.monotonic() - self._last_start < self.min_gap_s:
            return False
        self.pokes += 1
        self._wake_now()
        return True

    @property
    def interval_s(self) -> float:
        return self.fast_interval_s if self.hot else self.base_interval_s

    # ----- data -----

    def _candidates(self) -> list[dict]:
        """Open full-game markets whose expected finish is within the window
        (or which are already past it: game running long / overtime)."""
        now = self._clock()
        client = self.client_fn()
        out = []
        for series, league in GAME_SERIES.items():
            try:
                data = client.get_markets(status="open", limit=200, series_ticker=series)
            except Exception as exc:
                log.debug("sniper list %s: %s", series, exc)
                continue
            for m in data.get("markets") or []:
                exp = _iso_ts(m.get("expected_expiration_time")) or _iso_ts(m.get("close_time"))
                if exp is None:
                    continue
                # expected_expiration ~ start + 3h; keep games that could be live now.
                if exp - 5 * 3600 <= now <= exp + 2 * 3600 or exp - now <= self.window_min * 60:
                    out.append({**m, "_league": league})
        return out

    def _milestone(self, event_ticker: str) -> Optional[dict]:
        ev = event_ticker.upper()
        if ev not in self._milestones:
            try:
                ms = self.client_fn().get_milestones(ev)
            except Exception:
                return None  # retry next cycle
            pick = None
            for m in ms:
                d = m.get("details") or {}
                if d.get("main_game_event_ticker", "").upper() == ev or ev in [str(x).upper() for x in m.get("primary_event_tickers") or []]:
                    pick = m
                    break
            self._milestones[ev] = pick or (ms[0] if ms else None)
        return self._milestones[ev]

    def _states(self, markets: list[dict]) -> dict[str, dict]:
        """event_ticker -> live_data dict (Kalshi), one batch call per cycle."""
        by_ms: dict[str, str] = {}
        for m in markets:
            ev = str(m.get("event_ticker") or "").upper()
            ms = self._milestone(ev)
            if ms and ms.get("id"):
                by_ms[ms["id"]] = ev
        if not by_ms:
            return {}
        try:
            lives = self.client_fn().get_live_data_batch(list(by_ms))
        except Exception as exc:
            log.debug("sniper live_data: %s", exc)
            return {}
        return {by_ms[x["milestone_id"]]: x for x in lives if x.get("milestone_id") in by_ms}

    def _state_for(self, m: dict, live: Optional[dict]) -> Optional[dict]:
        league = m["_league"]
        ev = str(m.get("event_ticker") or "").upper()
        ms = self._milestones.get(ev) or {}
        home = yes_is_home(m.get("ticker"), ms.get("title"))
        st = None
        if live is not None and home is not None:
            st = game_state_from_live(league, live, home)
        espn_p = None
        if self.espn_fn is not None:
            try:
                est = self.espn_fn(m)
            except Exception:
                est = None
            if est is not None and getattr(est, "source", "") == "espn_live":
                espn_p = float(est.p_yes)
                if (st is None or st.get("frac_left") is None) and est.state == "in":
                    # ESPN fallback: time left from its status line; its live
                    # probability (shrunk halfway to 50%) is the model.
                    st = {"source": "espn", "status": "inprogress",
                          "lead": 0.0, "frac_left": espn_remaining_fraction(league, est.detail),
                          "espn_only": True, "summary": est.summary, "detail": est.detail}
        if st is None:
            return None
        st["live_win_p"] = espn_p
        return st

    # ----- cycle -----

    def cancel_stale(self) -> list[dict]:
        now = self._clock()
        out = []
        with self._lock:
            due = [(oid, e) for oid, e in self._pending.items() if e["deadline"] <= now]
        for oid, e in due:
            still = oid in getattr(self.orders, "resting", {})
            if still:
                res = self.orders.cancel(oid, e["ticker"])
                out.append({"order_id": oid, "ticker": e["ticker"], "ok": bool(res.get("ok")), "error": res.get("error")})
                if res.get("ok"):
                    self.store.log_activity("sniper", f"Canceled unfilled sniper order {e['ticker']} after {self.cancel_s:g}s")
                elif e.get("tries", 0) < 3:
                    e["tries"] = e.get("tries", 0) + 1
                    e["deadline"] = now + 5
                    continue
            with self._lock:
                self._pending.pop(oid, None)
        return out

    def run_cycle(self) -> dict:
        self._last_start = time.monotonic()
        summary: dict[str, Any] = {"ts": datetime.now(timezone.utc).isoformat(), "ran": False, "placed": 0, "orders": []}
        canceled = self.cancel_stale()
        if canceled:
            summary["canceled"] = canceled
        if not self._enabled:
            summary["reason"] = "off"
            self.hot = bool(self._pending)
            return summary
        summary["ran"] = True
        try:
            live = bool(self.orders.live_armed)
            summary["mode"] = "live" if live else "paper"
            if self.exchange_gate is not None:
                ex = self.exchange_gate.check()
                summary["exchange"] = {k: ex.get(k) for k in ("ok", "trading_active", "reason")}
                if not ex.get("ok"):
                    summary["error"] = ex.get("reason") or "Kalshi trading is not active"
                    return self._done(summary)
            if live and not settings.has_keys:
                summary["error"] = "Live — add Kalshi keys in Settings"
                return self._done(summary)
            state = self.state_fn()
            markets = [m for m in self._candidates() if geo.check(str(m.get("ticker")), state).get("allowed")]
            summary["state"] = state
            summary["candidates"] = len(markets)
            if not markets:
                self.hot = False
                self._watch([])
                return self._done(summary)
            states = self._states(markets)
            bankroll = self.bankroll_fn(live)
            if not bankroll or bankroll <= 0:
                summary["error"] = "No bankroll to size against (balance 0 or unreadable)."
                return self._done(summary)
            quotes = {}
            if self.quotes_fn is not None:
                try:
                    quotes = self.quotes_fn([m["ticker"] for m in markets]) or {}
                except Exception:
                    quotes = {}
            near, scored_in = [], []
            for m in markets:
                ev = str(m.get("event_ticker") or "").upper()
                st = self._state_for(m, states.get(ev))
                if not st or st.get("result_pending"):
                    continue
                if st.get("status") not in {"inprogress", "in", "closed"}:
                    continue
                frac = st.get("frac_left")
                if frac is None or frac > self.near_frac:
                    continue
                near.append(m["ticker"].upper())
                q = quotes.get(m["ticker"]) or {}
                mult = 1.0
                if self.fee_multiplier_fn is not None:
                    try:
                        mult = float(self.fee_multiplier_fn(series_of(m)))
                    except Exception:
                        mult = 1.0
                scale = 1.0
                scored_in.append({
                    "ticker": m["ticker"], "event_ticker": ev, "league": m["_league"],
                    "status": "closed" if st.get("status") == "closed" else "inprogress",
                    "lead": float(st.get("lead") or 0.0), "frac_left": float(frac),
                    "live_win_p": st.get("live_win_p") if not st.get("espn_only") else None,
                    "espn_only_p": st.get("live_win_p") if st.get("espn_only") else None,
                    "yes_ask": q.get("yes_ask") if q.get("yes_ask") is not None else _ask(m, "yes"),
                    "no_ask": q.get("no_ask") if q.get("no_ask") is not None else _ask(m, "no"),
                    "quote_source": "ws" if q else "rest",
                    "bankroll": bankroll, "fraction": settings.kelly_fraction * scale,
                    "max_fraction": settings.kelly_max_fraction * scale, "fee_multiplier": mult,
                    "ev_min": self.ev_min, "state": st,
                })
            self.hot = bool(near)
            self._watch(near)
            summary["near_finish"] = len(near)
            # ESPN-only rows: the provider's probability is the model (no score model).
            for c in scored_in:
                if c.get("espn_only_p") is not None:
                    c["live_win_p"] = c["espn_only_p"]
                    c["lead"], c["frac_left"] = 0.0, 1.0
            results = self.score_fn([{k: v for k, v in c.items() if k != "state"} for c in scored_in])
            held = {str(t).upper() for t in self.held_fn(live)}
            held_events = {t.rsplit("-", 1)[0] for t in held}
            now = self._clock()
            fired = 0
            for c, r in zip(scored_in, results):
                pick = r.get("pick")
                if not pick:
                    continue
                if fired >= self.max_per_cycle:
                    break
                ev = c["event_ticker"]
                if c["ticker"].upper() in held or ev in held_events:
                    continue
                if now - self._fired_events.get(ev, 0.0) < self.event_cooldown_s:
                    continue
                if not self._enabled:
                    summary["stopped"] = "Sniper turned off mid-cycle"
                    break
                if not geo.check(c["ticker"], self.state_fn()).get("allowed"):
                    continue
                if self.exchange_gate is not None and not self.exchange_gate.check().get("ok"):
                    summary["stopped"] = "Kalshi trading stopped mid-cycle"
                    break
                o = self._place(c, r, pick)
                summary["orders"].append(o)
                if o["ok"]:
                    fired += 1
                    summary["placed"] += 1
                    self._fired_events[ev] = now
        except Exception as exc:
            log.exception("sniper cycle failed")
            summary["error"] = str(exc)
            self.store.log_activity("error", f"Sniper cycle failed: {exc}")
        return self._done(summary)

    def _done(self, summary: dict) -> dict:
        self.last_cycle = summary
        return summary

    def _watch(self, tickers: list[str]) -> None:
        self.watched = list(tickers)
        if self.watch_fn is not None:
            try:
                self.watch_fn(tickers)
            except Exception:
                pass

    def _place(self, c: dict, r: dict, pick: dict) -> dict:
        st = c.get("state") or {}
        try:
            self.ledger.add_model_runs_sync([{
                "ticker": c["ticker"], "model_prob": pick["model_p"],
                "features": {"source": "sniper", "lead": c["lead"], "frac_left": c["frac_left"],
                             "league": c["league"], "data": st.get("source")}, "kind": "bet"}])
        except Exception:
            pass
        res = self.orders.place(ticker=c["ticker"], side=pick["side"], count=int(pick["count"]),
                                price_cents=int(pick["price_cents"]), order_type="limit", source="sniper")
        ok = bool(res.get("ok"))
        status = "ERROR" if not ok else "PAPER" if res.get("dry_run") else "SUBMITTED"
        oid = res.get("order_id")
        if ok and not res.get("dry_run") and oid and str(oid) in getattr(self.orders, "resting", {}):
            status = "RESTING"
            with self._lock:
                self._pending[str(oid)] = {"ticker": c["ticker"], "deadline": self._clock() + self.cancel_s}
        elif ok and not res.get("dry_run"):
            status = "FILLED"
        row = {
            "ticker": c["ticker"], "event_ticker": c["event_ticker"], "side": pick["side"], "count": pick["count"],
            "price_cents": pick["price_cents"], "mode": "paper" if res.get("dry_run", True) else "live",
            "status": status, "ev": pick["ev_total"], "order_log_id": res.get("order_log_id"), "bet_id": res.get("bet_id"),
            "detail": {"source": "sniper", "model_p": pick["model_p"], "edge_net": pick["edge_net"],
                       "fee": pick["fee_total"], "lead": c["lead"], "frac_left": round(c["frac_left"], 4),
                       "data": st.get("source"), "quote": c.get("quote_source"),
                       "error": None if ok else res.get("error")},
        }
        try:
            self.ledger.add_auto_order_sync(row)
        except Exception as exc:
            self.store.log_activity("error", f"Could not log sniper order: {exc}")
        self.store.log_activity(
            "sniper",
            f"Sniper {'paper ' if res.get('dry_run') else ''}{pick['side'].upper()} {pick['count']}x {c['ticker']} "
            f"@ {pick['price_cents']}¢ · model {pick['model_p']:.0%} · net edge {pick['edge_net'] * 100:+.1f}¢ "
            "(estimate, can lose)" if ok else f"Sniper order failed: {res.get('error')}",
        )
        return {"ok": ok, "ticker": c["ticker"], "side": pick["side"], "count": pick["count"],
                "price_cents": pick["price_cents"], "status": status, "model_p": pick["model_p"],
                "edge_net": pick["edge_net"], "error": None if ok else res.get("error")}

    def status(self) -> dict:
        return {
            "enabled": self._enabled,
            "mode": ("live" if self.orders.live_armed else "paper") if self._enabled else "off",
            "trades_real_money": self._enabled and bool(self.orders.live_armed),
            "ev_min": self.ev_min,
            "near_frac": self.near_frac,
            "cancel_s": self.cancel_s,
            "max_orders_per_cycle": self.max_per_cycle,
            "interval_s": self.interval_s,
            "hot": self.hot,
            "watching": len(self.watched),
            "pending_cancels": len(self._pending),
            "stream_wakes": self.pokes,
            "last_cycle": self.last_cycle,
            "note": "Late leads can still be lost. Every sniper bet is an estimate.",
        }

    async def run_forever(self) -> None:
        self._wake = asyncio.Event()
        self._loop = asyncio.get_running_loop()
        while True:
            if self._enabled or self._pending:
                try:
                    await asyncio.to_thread(self.run_cycle)
                except Exception:  # pragma: no cover
                    log.exception("sniper loop")
            timeout = self.interval_s if (self._enabled or self._pending) else 3600
            if self._pending:
                timeout = min(timeout, max(1.0, self.cancel_s / 2))
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

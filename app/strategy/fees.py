"""Kalshi trading fees and fee-aware EV / Kelly sizing.

Formula (Kalshi fee schedule, effective Jul 7 2026; docs "Fee Rounding" and
series ``fee_type`` / ``fee_multiplier``, read Nov 9 2026):

    taker fee = round_up( M_t * 0.07   * C * P * (1 - P) )   M_t default 1
    maker fee = round_up( M_m * 0.0175 * C * P * (1 - P) )   M_m default 0
                                                            (1 on series with
                                                            fee_type *_with_maker_fees)

P = price in dollars, C = contracts in the order. Kalshi rounds fees UP; the
schedule's table rounds to the cent (1 contract @ 50c -> $0.02). Direct
members can be charged at centicent precision, so the default here is the
conservative whole cent (``FEE_ROUND``=0.01; set 0.0001 for centicents).

The app buys at the ask with limit orders that usually take liquidity, so EV
and Kelly use the taker fee. Unknown series use multiplier 1 (full fee,
conservative); auto-bet and the sniper look up the series' real multiplier.
A positive net EV is still only an estimate; any bet can lose.
"""
from __future__ import annotations

import math
import os
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Optional

from app.strategy.kelly import kelly_size

TAKER_RATE = 0.07
MAKER_RATE = 0.0175


def _round_unit() -> float:
    try:
        r = float(os.getenv("FEE_ROUND", "0.01"))
    except ValueError:
        r = 0.01
    return r if r > 0 else 0.01


def _ceil_to(x: float, unit: float) -> float:
    if x <= 0:
        return 0.0
    return round(math.ceil(x / unit - 1e-9) * unit, 6)


def fee_total(price: float, count: int, *, rate: float = TAKER_RATE, multiplier: float = 1.0,
              round_to: Optional[float] = None) -> float:
    """Fee in dollars for one order of ``count`` contracts at ``price`` dollars."""
    p = float(price)
    c = int(count)
    if c <= 0 or not 0.0 < p < 1.0 or multiplier <= 0:
        return 0.0
    return _ceil_to(multiplier * rate * c * p * (1.0 - p), round_to or _round_unit())


def taker_fee(price: float, count: int, multiplier: float = 1.0, round_to: Optional[float] = None) -> float:
    return fee_total(price, count, rate=TAKER_RATE, multiplier=multiplier, round_to=round_to)


def maker_fee(price: float, count: int, multiplier: float = 1.0, round_to: Optional[float] = None) -> float:
    return fee_total(price, count, rate=MAKER_RATE, multiplier=multiplier, round_to=round_to)


def fee_per_contract_raw(price: float, multiplier: float = 1.0) -> float:
    """Unrounded taker fee per contract (the large-order limit)."""
    p = float(price)
    if not 0.0 < p < 1.0:
        return 0.0
    return multiplier * TAKER_RATE * p * (1.0 - p)


def taker_multiplier(fee_type: Optional[str], fee_multiplier: Any) -> float:
    """Series fee settings -> taker multiplier. Unknown -> 1.0 (conservative)."""
    try:
        m = float(fee_multiplier) if fee_multiplier not in (None, "") else 1.0
    except (TypeError, ValueError):
        m = 1.0
    if m < 0:
        m = 1.0
    return m


@dataclass
class NetSizing:
    contracts: int
    price: float
    fee_total: float
    fee_per_contract: float
    gross_edge: float  # p - price, per contract
    net_edge: float  # p - price - fee/contract
    ev_total_net: float  # contracts * net_edge
    cost_total: float  # contracts * price + fee
    kelly_pct: float
    multiplier: float
    reason: str

    def as_dict(self) -> dict:
        return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in asdict(self).items()}


def fee_aware_kelly(model_prob: float, price: float, bankroll: float, *, fraction: float, max_fraction: float,
                    multiplier: float = 1.0, max_contracts: Optional[int] = None,
                    round_to: Optional[float] = None) -> NetSizing:
    """Kelly on the fee-inclusive cost, then shrink until the rounded order fee
    still leaves positive net EV and the spend fits the Kelly allocation."""
    p = max(0.01, min(0.99, float(model_prob)))
    price = float(price)
    gross = p - price
    raw = fee_per_contract_raw(price, multiplier)
    eff = min(0.99, price + raw)
    k = kelly_size(model_prob=p, contract_price=eff, bankroll=bankroll, fraction=fraction, max_fraction=max_fraction)
    n = int(k.contracts)
    if max_contracts is not None:
        n = min(n, int(max_contracts))
    cap = bankroll * k.fractional_kelly
    reason = "Sized after fees"
    while n > 0:
        fee = fee_total(price, n, multiplier=multiplier, round_to=round_to)
        if n * price + fee <= cap + 1e-9 and n * gross - fee > 0:
            break
        n -= 1
    if n <= 0:
        fee1 = fee_total(price, 1, multiplier=multiplier, round_to=round_to)
        reason = "Negative edge after fees — PASS" if gross - fee1 <= 0 or k.full_kelly <= 0 else \
            "Edge after fees rounds to 0 contracts"
        return NetSizing(0, price, 0.0, round(fee1, 6), round(gross, 6), round(gross - fee1, 6), 0.0, 0.0, 0.0,
                         multiplier, reason)
    fee = fee_total(price, n, multiplier=multiplier, round_to=round_to)
    fpc = fee / n
    return NetSizing(n, price, round(fee, 6), round(fpc, 6), round(gross, 6), round(gross - fpc, 6),
                     round(n * gross - fee, 6), round(n * price + fee, 6), round(k.fractional_kelly * 100, 2),
                     multiplier, reason)


class SeriesFees:
    """series_ticker -> taker multiplier from GET /series/{t} (cached 6 h).

    ``fetch(series_ticker)`` returns the series payload ({"series": {...}} or
    the series dict). Failures fall back to 1.0 and are cached briefly.
    """

    def __init__(self, fetch: Callable[[str], Any], ttl_s: float = 6 * 3600, err_ttl_s: float = 300,
                 clock=time.monotonic):
        self.fetch = fetch
        self.ttl_s = ttl_s
        self.err_ttl_s = err_ttl_s
        self._clock = clock
        self._cache: dict[str, tuple[float, float, Optional[str]]] = {}
        self._lock = threading.Lock()

    def info(self, series_ticker: str) -> dict:
        s = str(series_ticker or "").upper()
        if not s:
            return {"multiplier": 1.0, "fee_type": None, "source": "default"}
        now = self._clock()
        with self._lock:
            hit = self._cache.get(s)
        if hit and hit[0] > now:
            return {"multiplier": hit[1], "fee_type": hit[2], "source": "cache"}
        try:
            data = self.fetch(s) or {}
            ser = data.get("series") if isinstance(data, dict) and "series" in data else data
            ft = (ser or {}).get("fee_type")
            m = taker_multiplier(ft, (ser or {}).get("fee_multiplier"))
            exp = now + self.ttl_s
            src = "series"
        except Exception:
            ft, m, exp, src = None, 1.0, now + self.err_ttl_s, "default"
        with self._lock:
            self._cache[s] = (exp, m, ft)
        return {"multiplier": m, "fee_type": ft, "source": src}

    def multiplier(self, series_ticker: str) -> float:
        return float(self.info(series_ticker)["multiplier"])


def series_of(row_or_ticker: Any) -> str:
    """KXNFLGAME-26NOV09BUFMIN-BUF -> KXNFLGAME (series_ticker field wins)."""
    if isinstance(row_or_ticker, dict):
        if row_or_ticker.get("series_ticker"):
            return str(row_or_ticker["series_ticker"]).upper()
        row_or_ticker = row_or_ticker.get("event_ticker") or row_or_ticker.get("ticker") or ""
    return str(row_or_ticker or "").upper().split("-", 1)[0]

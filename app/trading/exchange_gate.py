"""Exchange status gate for automated orders (auto-bet and sniper).

GET /trade-api/v2/exchange/status (public; docs.kalshi.com/api-reference/
exchange/get-exchange-status): ``exchange_active`` is false during maintenance;
``trading_active`` is false outside trading hours or when Kalshi pauses
trading. Automated orders run only when both are true. The answer is cached
for ``EXCHANGE_STATUS_TTL_S`` (15 s) so the gate costs at most ~4 read
requests a minute. Unreadable status fails closed (no automated orders).
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable, Optional


class ExchangeGate:
    def __init__(self, status_fn: Callable[[], dict], ttl_s: Optional[float] = None,
                 error_ttl_s: float = 5.0, clock: Callable[[], float] = time.monotonic):
        self.status_fn = status_fn
        self.ttl_s = float(ttl_s if ttl_s is not None else os.getenv("EXCHANGE_STATUS_TTL_S", 15) or 15)
        self.error_ttl_s = error_ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: Optional[dict] = None
        self._at = -1e18
        self.checks = 0

    def check(self, force: bool = False) -> dict:
        with self._lock:
            age = self._clock() - self._at
            ttl = self.ttl_s if (self._cached or {}).get("readable") else self.error_ttl_s
            if not force and self._cached is not None and age <= ttl:
                return {**self._cached, "age_s": round(age, 1)}
        try:
            raw = self.status_fn() or {}
            exchange_active = bool(raw.get("exchange_active"))
            trading_active = bool(raw.get("trading_active"))
            if not exchange_active:
                reason = "Kalshi exchange is under maintenance"
                resume = raw.get("exchange_estimated_resume_time")
                if resume:
                    reason += f" (estimated resume {resume})"
            elif not trading_active:
                reason = "Kalshi trading is paused or outside trading hours"
            else:
                reason = "trading active"
            out = {
                "ok": exchange_active and trading_active,
                "readable": True,
                "exchange_active": exchange_active,
                "trading_active": trading_active,
                "resume_time": raw.get("exchange_estimated_resume_time"),
                "reason": reason,
            }
        except Exception as exc:
            out = {"ok": False, "readable": False, "exchange_active": None, "trading_active": None,
                   "resume_time": None, "reason": f"Could not read Kalshi exchange status ({str(exc)[:120]}) — automated orders paused"}
        with self._lock:
            self._cached = out
            self._at = self._clock()
            self.checks += 1
        return {**out, "age_s": 0.0}

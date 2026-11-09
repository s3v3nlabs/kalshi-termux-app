"""One shared Kalshi request budget for the whole app (sync + async + WS setup).

Kalshi meters every request in tokens (docs.kalshi.com/getting_started/rate_limits):

* Two independent buckets: **Read** (GETs and anything not a write) and
  **Write** (order create / amend / cancel, order groups, RFQ quotes, and the
  API-tier upgrade POST). Each refills continuously at ``refill_rate`` tokens/s
  up to ``bucket_capacity``; both come from ``GET /account/limits``.
* ``GET /portfolio/balance`` has its own bucket: 1 call/s on Basic/Advanced,
  5/s above that, no burst.
* Most requests cost 10 tokens. ``GET /account/endpoint_costs`` lists the
  endpoints that cost something else (cancels 2, tier upgrade 30, ...).
* A 429 means the bucket was empty. No penalty, no Retry-After header; back off
  exponentially and try again.

Ban-safety rules this module enforces by construction:

* There is exactly ONE budget per process (``get_budget()``), shared by every
  client, thread and coroutine. All Kalshi traffic uses one API key.
* The budget runs at ``RATE_SAFETY`` (default 85%) of the published limits, so
  the app stays under Kalshi's own bucket even with clock skew.
* A 429 drains the matching local bucket to zero so every caller waits.
* No IP rotation, proxies, extra keys or extra accounts exist anywhere in the
  app. More compute never means more Kalshi calls.

Waits are *reservations*: a caller takes its tokens now (the balance can go
negative) and sleeps until the balance would have covered them. That keeps
callers in arrival order and never busy-loops.
"""
from __future__ import annotations

import asyncio
import os
import re
import threading
import time
from typing import Any, Callable, Optional

from app.kalshi.rate_limit import GlobalRateLimiter

DEFAULT_COST = 10

# Published budgets (tokens/s) used until GET /account/limits answers.
# Basic read holds 3 s of budget; Basic write holds 1 s.
TIER_DEFAULTS = {
    "basic": {"read": (200, 600), "write": (100, 100)},
    "advanced": {"read": (300, 900), "write": (300, 900)},
    "expert": {"read": (600, 600), "write": (600, 1800)},
    "premier": {"read": (1200, 1200), "write": (1200, 3600)},
}
# Balance endpoint: calls per second (own bucket, no burst).
BALANCE_RATE = {"basic": 1.0, "advanced": 1.0}
BALANCE_RATE_HIGH = 5.0

WRITE_METHODS = {"POST", "PUT", "DELETE", "PATCH"}
BALANCE_PATH = "/trade-api/v2/portfolio/balance"


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        val = float(os.getenv(name, default))
    except (TypeError, ValueError):
        val = default
    return max(lo, min(hi, val))


class TokenBucket:
    """Thread-safe token bucket with reservation semantics."""

    def __init__(self, refill_rate: float, capacity: float, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self.refill_rate = max(0.001, float(refill_rate))
        self.capacity = max(1.0, float(capacity))
        self.tokens = self.capacity
        self._last = clock()
        self.spent = 0.0
        self.waited_s = 0.0
        self.throttled = 0  # 429s seen

    def _refill(self) -> None:
        now = self._clock()
        self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.refill_rate)
        self._last = now

    def configure(self, refill_rate: float, capacity: float) -> None:
        with self._lock:
            self._refill()
            self.refill_rate = max(0.001, float(refill_rate))
            self.capacity = max(1.0, float(capacity))
            self.tokens = min(self.tokens, self.capacity)

    def reserve(self, cost: float) -> float:
        """Take ``cost`` tokens now; return seconds to wait before sending."""
        cost = float(cost)
        with self._lock:
            self._refill()
            # A single request bigger than the bucket could never pass; clamp.
            cost = min(cost, self.capacity)
            self.tokens -= cost
            self.spent += cost
            wait = 0.0 if self.tokens >= 0 else -self.tokens / self.refill_rate
            self.waited_s += wait
            return wait

    def drain(self) -> None:
        """Kalshi said 429: assume our view was optimistic and start from empty."""
        with self._lock:
            self._refill()
            self.tokens = min(self.tokens, 0.0)
            self.throttled += 1

    def status(self) -> dict:
        with self._lock:
            self._refill()
            return {
                "tokens": round(self.tokens, 1),
                "capacity": round(self.capacity, 1),
                "refill_per_s": round(self.refill_rate, 1),
                "spent": round(self.spent, 1),
                "waited_s": round(self.waited_s, 3),
                "throttled_429": self.throttled,
            }


def _route_regex(path: str) -> re.Pattern:
    """'/trade-api/v2/portfolio/events/orders/:order_id' -> regex; '*rest' -> anything."""
    parts = []
    for seg in path.strip("/").split("/"):
        if seg.startswith(":"):
            parts.append(r"[^/]+")
        elif seg.startswith("*"):
            parts.append(r".*")
        else:
            parts.append(re.escape(seg))
    return re.compile("^/" + "/".join(parts) + "$")


class KalshiBudget:
    def __init__(self, clock: Callable[[], float] = time.monotonic, safety: Optional[float] = None):
        self.safety = safety if safety is not None else _env_float("RATE_SAFETY", 0.85, 0.1, 1.0)
        self.tier = "basic"
        self.limits_source = "defaults (basic)"
        self.limits_raw: Optional[dict] = None
        self.default_cost = DEFAULT_COST
        self._costs: list[tuple[str, re.Pattern, int, str]] = []
        self.costs_loaded_at: Optional[float] = None
        r_rate, r_cap = TIER_DEFAULTS["basic"]["read"]
        w_rate, w_cap = TIER_DEFAULTS["basic"]["write"]
        s = self.safety
        self.read = TokenBucket(r_rate * s, r_cap * s, clock)
        self.write = TokenBucket(w_rate * s, w_cap * s, clock)
        # Balance bucket counts calls, not tokens (1 token == 1 call).
        self.balance = TokenBucket(BALANCE_RATE["basic"] * s, 1.0, clock)
        # Raw request-count pacer: a last-resort cap against a runaway loop.
        self.pacer = GlobalRateLimiter(rate=_env_float("KALSHI_MAX_RPS", 25.0, 1.0, 500.0))

    # ----- configuration -----

    def apply_limits(self, data: dict, source: str = "GET /account/limits") -> None:
        """Size the buckets from GET /account/limits ({usage_tier, read, write})."""
        if not isinstance(data, dict):
            return
        s = self.safety
        tier = str(data.get("usage_tier") or self.tier).lower()
        for name, bucket in (("read", self.read), ("write", self.write)):
            lim = data.get(name) or {}
            try:
                rate = float(lim["refill_rate"])
                cap = float(lim.get("bucket_capacity") or rate)
            except (KeyError, TypeError, ValueError):
                rate, cap = TIER_DEFAULTS.get(tier, TIER_DEFAULTS["basic"])[name]
            bucket.configure(rate * s, max(cap * s, self.default_cost))
        self.balance.configure(BALANCE_RATE.get(tier, BALANCE_RATE_HIGH) * s, 1.0)
        self.tier = tier
        self.limits_source = source
        self.limits_raw = {k: data.get(k) for k in ("usage_tier", "read", "write", "grants")}

    def apply_endpoint_costs(self, data: dict) -> int:
        """Load GET /account/endpoint_costs ({default_cost, endpoint_costs[]})."""
        if not isinstance(data, dict):
            return 0
        try:
            self.default_cost = int(data.get("default_cost") or DEFAULT_COST)
        except (TypeError, ValueError):
            self.default_cost = DEFAULT_COST
        rows = []
        for item in data.get("endpoint_costs") or []:
            try:
                method = str(item["method"]).upper()
                path = str(item["path"])
                cost = int(item["cost"])
            except (KeyError, TypeError, ValueError):
                continue
            rows.append((method, _route_regex(path), cost, path))
        # Literal routes before wildcard routes.
        rows.sort(key=lambda r: (r[3].count(":") + r[3].count("*"), -len(r[3])))
        self._costs = rows
        self.costs_loaded_at = time.time()
        return len(rows)

    # ----- accounting -----

    @staticmethod
    def kind(method: str, path: str) -> str:
        path = path.split("?")[0]
        if method.upper() == "GET" and path.rstrip("/") == BALANCE_PATH:
            return "balance"
        return "write" if method.upper() in WRITE_METHODS else "read"

    def cost(self, method: str, path: str) -> int:
        path = path.split("?")[0].rstrip("/") or "/"
        m = method.upper()
        for meth, rx, cost, _ in self._costs:
            if meth == m and rx.match(path):
                return cost
        return self.default_cost

    def _bucket(self, kind: str) -> TokenBucket:
        return {"read": self.read, "write": self.write, "balance": self.balance}[kind]

    def reserve(self, method: str, path: str, units: int = 1, pace: bool = True) -> float:
        """Reserve tokens for one request (``units`` > 1 for a batch). Returns wait seconds.

        ``pace=False`` skips the request-count pacer (the sync client paces
        through its own ``bucket.total``, which defaults to the same pacer)."""
        kind = self.kind(method, path)
        cost = 1 if kind == "balance" else self.cost(method, path) * max(1, int(units))
        wait = self._bucket(kind).reserve(cost)
        if pace:
            wait = max(wait, self.pacer.reserve())
        return wait

    def acquire_sync(self, method: str, path: str, units: int = 1, sleep=time.sleep, pace: bool = True) -> float:
        wait = self.reserve(method, path, units, pace=pace)
        if wait > 0:
            sleep(wait)
        return wait

    async def acquire_async(self, method: str, path: str, units: int = 1) -> float:
        wait = self.reserve(method, path, units)
        if wait > 0:
            await asyncio.sleep(wait)
        return wait

    def note_429(self, method: str, path: str) -> None:
        self._bucket(self.kind(method, path)).drain()

    def status(self) -> dict:
        return {
            "tier": self.tier,
            "limits_source": self.limits_source,
            "safety": self.safety,
            "default_cost": self.default_cost,
            "endpoint_costs_loaded": len(self._costs),
            "read": self.read.status(),
            "write": self.write.status(),
            "balance": self.balance.status(),
            "max_requests_per_s": self.pacer.rate,
            "rules": "one key · one shared budget · no proxies / IP rotation / extra keys",
        }


_BUDGET: Optional[KalshiBudget] = None
_BUDGET_LOCK = threading.Lock()


def get_budget() -> KalshiBudget:
    """The process-wide budget. Every Kalshi request in the app draws from it."""
    global _BUDGET
    with _BUDGET_LOCK:
        if _BUDGET is None:
            _BUDGET = KalshiBudget()
        return _BUDGET


def reset_budget_for_tests(budget: Optional[KalshiBudget] = None) -> KalshiBudget:
    global _BUDGET
    with _BUDGET_LOCK:
        _BUDGET = budget or KalshiBudget()
        return _BUDGET


def summarize_limits(data: Any) -> dict:
    """Plain view of GET /account/limits for the Settings card."""
    data = data if isinstance(data, dict) else {}
    out = {"usage_tier": data.get("usage_tier"), "grants": data.get("grants") or []}
    for name in ("read", "write"):
        lim = data.get(name) or {}
        out[name] = {"refill_rate": lim.get("refill_rate"), "bucket_capacity": lim.get("bucket_capacity")}
    return out

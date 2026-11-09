"""API usage tier: read it, and (optionally) claim the free Advanced upgrade.

POST /account/api_usage_level/upgrade (docs: api-reference/account/upgrade-
account-api-usage-level, read Nov 9 2026) grants a permanent Advanced grant
(read 300/s, write 300/s instead of Basic's 200/100) when at least one of your
last 100 Predictions orders was created via the API. 403 = no API order yet.
It costs 30 Write tokens. This is Kalshi's own sanctioned path to more
throughput; higher tiers come only from trading volume.

The app calls it at most once per ``UPGRADE_RETRY_S`` (6 h; the button has a
10-minute cooldown), only with keys, and never once the tier is above Basic.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from app.kalshi.budget import KalshiBudget, get_budget, summarize_limits
from app.kalshi.client import KalshiAPIError, friendly_order_error

AUTO_KEY = "auto_tier_upgrade"
LAST_TRY_KEY = "tier_upgrade_last_try"
DONE_KEY = "tier_upgrade_done"
UPGRADE_RETRY_S = 6 * 3600
BUTTON_COOLDOWN_S = 600
LIMITS_TTL_S = 60


class TierManager:
    def __init__(self, store, client_fn, has_keys_fn, budget: Optional[KalshiBudget] = None, clock=time.time):
        self.store = store
        self.client_fn = client_fn
        self.has_keys_fn = has_keys_fn
        self.budget = budget or get_budget()
        self._clock = clock
        self._limits: Optional[dict] = None
        self._limits_at = 0.0
        self.last_result: Optional[dict] = None

    @property
    def auto(self) -> bool:
        return (self.store.get_setting(AUTO_KEY, "0") or "0") == "1"

    def set_auto(self, on: bool) -> None:
        self.store.set_setting(AUTO_KEY, "1" if on else "0")

    def limits(self, refresh: bool = False) -> dict:
        """Current tier from GET /account/limits (keys) or the budget defaults."""
        if not self.has_keys_fn():
            return {"ok": True, "has_keys": False, "usage_tier": self.budget.tier, "source": "defaults (no keys)",
                    "budget": self.budget.status()}
        now = self._clock()
        if refresh or self._limits is None or now - self._limits_at > LIMITS_TTL_S:
            try:
                data = self.client_fn().get_limits()
                self.budget.apply_limits(data)
                self._limits, self._limits_at = summarize_limits(data), now
            except Exception as exc:
                return {"ok": False, "has_keys": True, "usage_tier": self.budget.tier,
                        "error": friendly_order_error(exc)["error"], "budget": self.budget.status()}
        return {"ok": True, "has_keys": True, **self._limits, "source": "GET /account/limits", "budget": self.budget.status()}

    def status(self) -> dict:
        last = self.store.get_setting(LAST_TRY_KEY, "") or ""
        return {
            "auto_upgrade": self.auto,
            "tier": self.budget.tier,
            "upgraded": bool(self.store.get_setting(DONE_KEY, "") or ""),
            "last_try": float(last) if last else None,
            "last_result": self.last_result,
        }

    def upgrade(self, *, cooldown_s: float = BUTTON_COOLDOWN_S) -> dict:
        if not self.has_keys_fn():
            return {"ok": False, "error": "Add your Kalshi key in Settings first. The upgrade is tied to your account.",
                    "code": "keys_required"}
        tier = (self.limits().get("usage_tier") or self.budget.tier or "basic").lower()
        if tier != "basic":
            self.store.set_setting(DONE_KEY, str(self._clock()))
            return {"ok": True, "already": True, "usage_tier": tier, "message": f"Already on {tier.title()}."}
        last = self.store.get_setting(LAST_TRY_KEY, "") or ""
        if last and self._clock() - float(last) < cooldown_s:
            wait = int(cooldown_s - (self._clock() - float(last)))
            return {"ok": False, "error": f"Tried recently. Try again in about {max(1, wait // 60)} min.", "code": "cooldown"}
        self.store.set_setting(LAST_TRY_KEY, str(self._clock()))
        try:
            self.client_fn().upgrade_usage_level()
        except KalshiAPIError as exc:
            if exc.status == 403:
                res = {"ok": False, "code": "needs_api_order",
                       "error": "Kalshi grants Advanced once one of your last 100 orders was placed through the API. "
                                "Place one order from this app (LIVE), then try again."}
            else:
                res = {"ok": False, "code": f"http_{exc.status}", "error": friendly_order_error(exc)["error"]}
            self.last_result = res
            return res
        except Exception as exc:
            res = {"ok": False, "code": "error", "error": friendly_order_error(exc)["error"]}
            self.last_result = res
            return res
        self.store.set_setting(DONE_KEY, str(self._clock()))
        lim = self.limits(refresh=True)
        res = {"ok": True, "usage_tier": lim.get("usage_tier"), "message": "Advanced API tier granted (free, permanent)."}
        self.store.log_activity("settings", "API tier upgraded to Advanced")
        self.last_result = res
        return res

    def maybe_auto_upgrade(self, cooldown_s: float = UPGRADE_RETRY_S) -> Optional[dict]:
        """Background: only when the toggle is ON, keys exist and tier is Basic."""
        if not self.auto or not self.has_keys_fn() or self.budget.tier != "basic":
            return None
        return self.upgrade(cooldown_s=cooldown_s)


def tier_note(data: Any) -> str:
    t = str((data or {}).get("usage_tier") or "basic").title()
    return f"{t} tier"

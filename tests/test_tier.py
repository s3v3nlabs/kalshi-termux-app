"""Speed pack item 4: free Advanced tier upgrade (once, keys only) + tier display."""
from __future__ import annotations

from app.db.store import Store
from app.kalshi.budget import KalshiBudget
from app.kalshi.client import KalshiAPIError
from app.kalshi.tier import TierManager

BASIC = {"usage_tier": "basic", "read": {"refill_rate": 200, "bucket_capacity": 600},
         "write": {"refill_rate": 100, "bucket_capacity": 100}, "grants": []}
ADV = {"usage_tier": "advanced", "read": {"refill_rate": 300, "bucket_capacity": 900},
       "write": {"refill_rate": 300, "bucket_capacity": 900},
       "grants": [{"exchange_instance": "event_contract", "level": "advanced", "source": "manual"}]}


class Cl:
    def __init__(self, upgrade_status=201):
        self.limits_doc = dict(BASIC)
        self.upgrades = 0
        self.upgrade_status = upgrade_status

    def get_limits(self):
        return self.limits_doc

    def upgrade_usage_level(self):
        self.upgrades += 1
        if self.upgrade_status != 201:
            raise KalshiAPIError(self.upgrade_status, "forbidden", "no api order")
        self.limits_doc = dict(ADV)
        return {}


class T:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def mk(tmp_path, client, keys=True):
    clock = T()
    bud = KalshiBudget(safety=1.0)
    return TierManager(Store(tmp_path / "s.db"), lambda: client, lambda: keys, budget=bud, clock=clock), bud, clock


def test_no_keys_never_calls_kalshi(tmp_path):
    cl = Cl()
    tm, bud, _ = mk(tmp_path, cl, keys=False)
    assert tm.upgrade()["code"] == "keys_required" and cl.upgrades == 0
    lim = tm.limits()
    assert lim["has_keys"] is False and lim["usage_tier"] == "basic"


def test_upgrade_once_then_already(tmp_path):
    cl = Cl()
    tm, bud, _ = mk(tmp_path, cl)
    r = tm.upgrade()
    assert r["ok"] and r["usage_tier"] == "advanced" and cl.upgrades == 1
    assert bud.tier == "advanced" and bud.write.refill_rate == 300
    r2 = tm.upgrade()
    assert r2["already"] and cl.upgrades == 1  # never posts again above Basic
    assert tm.status()["upgraded"] is True


def test_403_explains_and_cooldown(tmp_path):
    cl = Cl(upgrade_status=403)
    tm, _, clock = mk(tmp_path, cl)
    r = tm.upgrade()
    assert r["code"] == "needs_api_order" and "last 100 orders" in r["error"]
    assert tm.upgrade()["code"] == "cooldown" and cl.upgrades == 1
    clock.t += 601
    tm.upgrade()
    assert cl.upgrades == 2


def test_auto_upgrade_only_when_toggled(tmp_path):
    cl = Cl()
    tm, bud, _ = mk(tmp_path, cl)
    assert tm.maybe_auto_upgrade() is None and cl.upgrades == 0
    tm.set_auto(True)
    assert tm.maybe_auto_upgrade()["ok"] and cl.upgrades == 1
    assert tm.maybe_auto_upgrade() is None  # tier now advanced


def test_api_limits_endpoints_without_keys(monkeypatch):
    from fastapi.testclient import TestClient

    import app.main as M

    monkeypatch.setattr(type(M.settings), "has_keys", property(lambda self: False))
    with TestClient(M.app) as c:
        lim = c.get("/api/limits").json()
        assert lim["has_keys"] is False and "budget" in lim
        assert c.post("/api/limits/upgrade").json()["code"] == "keys_required"
        r = c.post("/api/limits/auto", json={"enabled": False}).json()
        assert r["auto_upgrade"] is False

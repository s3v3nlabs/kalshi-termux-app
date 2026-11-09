"""Speed pack item 3: trading_active gate for auto-bet / sniper."""
from __future__ import annotations

import pytest

from app.config import settings
from app.db.ledger import Ledger
from app.db.store import Store
from app.trading.autobet import AutoBetEngine
from app.trading.exchange_gate import ExchangeGate
from app.trading.orders import OrderGate
from tests.test_autobet import ROWS, FakeClient


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def test_gate_states_and_cache():
    calls = []
    state = {"exchange_active": True, "trading_active": True}
    c = Clock()
    g = ExchangeGate(lambda: calls.append(1) or dict(state), ttl_s=15, clock=c)
    assert g.check()["ok"] is True
    state["trading_active"] = False
    assert g.check()["ok"] is True and len(calls) == 1  # cached
    c.t = 16
    r = g.check()
    assert r["ok"] is False and "paused" in r["reason"] and len(calls) == 2
    state.update(exchange_active=False, exchange_estimated_resume_time="2026-11-09T12:00:00Z")
    r = g.check(force=True)
    assert r["ok"] is False and "maintenance" in r["reason"] and "12:00" in r["reason"]


def test_gate_fails_closed_on_error():
    def boom():
        raise RuntimeError("network down")

    r = ExchangeGate(boom).check()
    assert r["ok"] is False and r["readable"] is False and "paused" in r["reason"]


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "dry_run", True)
    monkeypatch.setenv("GEO_LOCAL_STATE", "CA")
    store, ledger = Store(tmp_path / "s.db"), Ledger(tmp_path / "l.db")
    client = FakeClient()
    state = {"exchange_active": True, "trading_active": False}
    gate = ExchangeGate(lambda: dict(state), ttl_s=0)
    eng = AutoBetEngine(store=store, ledger=ledger, orders=OrderGate(store, client, ledger),
                        scan_fn=lambda b: {"markets": [dict(r) for r in ROWS]}, client_fn=lambda: client,
                        balance_fn=lambda: 500.0, interval_s=1, top_n=5, paper_bankroll=1000.0, exchange_gate=gate)
    eng.set_enabled(True)
    return eng, state, ledger


def test_autobet_waits_while_trading_paused(engine):
    eng, state, ledger = engine
    out = eng.run_cycle()
    assert out["placed"] == 0 and "paused" in out["error"] and ledger.auto_orders_sync() == []
    state["trading_active"] = True
    out = eng.run_cycle()
    assert out["placed"] > 0 and out["exchange"]["ok"] is True


def test_api_exchange_endpoint(monkeypatch):
    from fastapi.testclient import TestClient

    import app.main as M

    monkeypatch.setattr(M.exchange_gate, "status_fn", lambda: {"exchange_active": True, "trading_active": False})
    with TestClient(M.app) as c:
        r = c.get("/api/exchange?refresh=1").json()
        assert r["ok"] is False and r["trading_active"] is False
        s = c.get("/api/status").json()
        assert s["exchange"]["trading_active"] is False

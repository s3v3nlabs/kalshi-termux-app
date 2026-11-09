"""Item 4: offline backtest + Brier gate. Uses the seeded fixture (no live calls)."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import backtest as bt
from app.db.ledger import Ledger
from app.strategy import model as M

FIXTURE = Path(__file__).parent / "fixtures" / "backtest_fixture.json"


def test_report_shape_on_fixture():
    rows = bt.load_fixture(FIXTURE)
    rep = bt.run_backtest(rows, {k: 0.1 for k in M.WEIGHT_KEYS})
    assert rep["n"] == len(rows) == 40
    assert 0 <= rep["brier"] <= 1
    assert len(rep["buckets"]) == 10 and sum(b["n"] for b in rep["buckets"]) == 40
    assert rep["pnl"]["kelly_fraction"] == 0.5 and len(rep["pnl"]["curve"]) == 40


def test_gate():
    assert bt.gate(0.20, 0.21) and bt.gate(0.21, 0.21) and not bt.gate(0.22, 0.21)
    assert bt.gate(0.3, None) and not bt.gate(None, 0.2)


def test_proposed_weights_pass_gate_on_fixture():
    result = bt.compare(bt.load_fixture(FIXTURE), {k: 0.1 for k in M.WEIGHT_KEYS})
    assert result["gate_pass"] is True
    assert result["proposed"]["report"]["brier"] <= result["current"]["report"]["brier"]


def test_bad_weights_fail_gate():
    rows = bt.load_fixture(FIXTURE)
    good = bt.run_backtest(rows, {"w0": 0, "w1": 1, "w2": 0, "w3": 0, "w4": 0, "w5": 0})
    bad = bt.run_backtest(rows, {"w0": 0, "w1": -1, "w2": 0, "w3": 0, "w4": 0, "w5": 0})
    assert not bt.gate(bad["brier"], good["brier"])


def test_ledger_dataset_replays_model_runs(tmp_path):
    led = Ledger(tmp_path / "b.db")
    led.add_model_runs_sync([{"ticker": "A", "model_prob": 0.7, "features": M.build_features(0.7), "ts": "2026-01-01T00:00:00+00:00"}])
    bid = led.add_bet_sync(ticker="A", side="no", price=0.3, stake=0.3, model_prob=0.3, market_prob=0.3)
    led.settle_bet_sync(bid, "yes")
    rows = bt.load_ledger_dataset(led)
    assert rows == [{"ticker": "A", "features": M.build_features(0.7), "yes_outcome": 1, "stored_p": 0.7}]


def test_cli_on_fixture(capsys):
    assert bt.main(["--fixture", str(FIXTURE), "--propose"]) == 0
    assert '"gate_pass": true' in capsys.readouterr().out


def test_backtest_endpoints_use_fixture_without_history(monkeypatch, tmp_path):
    import app.main as main

    monkeypatch.setattr(main, "ledger", Ledger(tmp_path / "empty.db"))
    http = TestClient(main.app)
    data = http.get("/backtest").json()
    assert data["source"].startswith("fixture")
    assert set(data) >= {"current", "proposed", "gate_pass"}
    assert http.post("/backtest/promote").status_code == 400


def test_promote_refuses_fixture_source():
    """UI screen 6: Promote must not write weights when only the fixture is available."""
    from fastapi.testclient import TestClient

    import app.main as main

    http = TestClient(main.app)
    report = http.get("/backtest").json()
    if report["source"] == "ledger":
        return  # a real ledger exists on this box; the fixture guard is not exercised
    assert http.post("/backtest/promote").status_code == 400

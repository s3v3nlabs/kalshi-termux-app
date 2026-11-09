"""Item 3: logistic scorer, weights JSON, refit (GD, log-loss, L2), weekly cadence."""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

import pytest

from app.db.ledger import Ledger
from app.db.store import Store
from app.strategy import model as M


def test_default_weights_all_point_one():
    data = json.loads(M.DEFAULT_WEIGHTS_PATH.read_text())
    assert data["weights"] == {k: 0.1 for k in M.WEIGHT_KEYS}


def test_formula_matches_spec():
    w = {k: 0.1 for k in M.WEIGHT_KEYS}
    x = {"market_edge": 1.0, "time_to_close": 0.5, "volume_z": -1.0, "espn_delta": 2.0, "spread_z": 0.0}
    z = 0.1 + 0.1 * (1.0 + 0.5 - 1.0 + 2.0 + 0.0)
    assert M.predict(x, w) == pytest.approx(1 / (1 + math.exp(-z)), abs=1e-6)


def test_features():
    now = datetime(2026, 11, 8, tzinfo=timezone.utc)
    f = M.build_features(0.5, close_time="2026-11-23T00:00:00Z", volume=math.e**6 - 1, spread_cents=6, espn_p_yes=0.5, now=now)
    assert f["market_edge"] == pytest.approx(0.0)
    assert f["time_to_close"] == pytest.approx(0.5)
    assert f["volume_z"] == pytest.approx(0.0, abs=1e-6)
    assert f["spread_z"] == pytest.approx(1.0)
    assert f["espn_delta"] == pytest.approx(0.0)
    assert M.build_features(0.3)["espn_delta"] == 0.0


def test_fit_reduces_log_loss():
    samples = [({"market_edge": 2.0}, 1)] * 8 + [({"market_edge": -2.0}, 0)] * 8
    w0 = {k: 0.1 for k in M.WEIGHT_KEYS}
    w1 = M.fit(samples, w0)
    assert M.log_loss(samples, w1) < M.log_loss(samples, w0)
    assert w1["w1"] > w0["w1"]


def _seed(led: Ledger, n: int, ts: str):
    for i in range(n):
        p_mkt = 0.8 if i % 2 == 0 else 0.2
        feats = M.build_features(p_mkt)
        led.add_model_runs_sync([{"ticker": f"T{i}", "model_prob": 0.5, "features": feats, "ts": ts}])
        bid = led.add_bet_sync(ticker=f"T{i}", side="yes", price=p_mkt, stake=p_mkt, model_prob=0.5, market_prob=p_mkt)
        led.settle_bet_sync(bid, "yes" if i % 2 == 0 else "no")


def test_refit_persists_logs_and_is_weekly(tmp_path):
    led = Ledger(tmp_path / "m.db")
    store = Store(tmp_path / "s.db")
    path = tmp_path / "w.json"
    now = datetime(2026, 11, 8, tzinfo=timezone.utc)
    skipped = M.maybe_refit(led, store, now=now, path=path)
    assert skipped["skipped"]
    assert not path.exists()  # nothing persisted, so the next settle retries
    _seed(led, 10, "2000-01-01T00:00:00+00:00")
    rec = M.maybe_refit(led, store, now=now, path=path)
    assert rec["n"] == 10 and rec["log_loss_after"] < rec["log_loss_before"]
    saved = json.loads(path.read_text())
    assert saved["version"] == 1 and saved["last_refit"] == now.isoformat()
    assert saved["refit_log"][-1]["version"] == 1
    assert any(a["kind"] == "model" for a in store.recent_activity(10))
    assert M.maybe_refit(led, store, now=now + timedelta(days=3), path=path) is None
    assert M.maybe_refit(led, store, now=now + timedelta(days=7), path=path)["version"] == 2

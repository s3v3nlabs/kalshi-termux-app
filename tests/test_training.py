"""Phase 1: settled-market ingest, feature reconstruction, chronological
holdout, the Brier gate, and the end of per-poll model_runs bloat."""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db.ledger import MIN_CALIBRATION_N, Ledger, is_uncalibrated
from app.strategy import model as M
from app.strategy import training as T
from app.strategy.scanner import enrich_market

FIXTURE = Path(__file__).parent / "fixtures" / "settled_markets_fixture.json"


def _fixture():
    return json.loads(FIXTURE.read_text())


class FakeClient:
    """Serves the fixture; any other call is a test failure (no network)."""

    def __init__(self, fx):
        self.fx = fx
        self.calls = []

    def get_markets(self, **kw):
        self.calls.append(("markets", kw))
        lo, hi = kw.get("min_close_ts"), kw.get("max_close_ts")
        out = [m for m in self.fx["markets"] if lo <= T._ts(m["close_time"]) <= hi]
        return {"markets": out, "cursor": ""}

    def get_candlesticks(self, series, ticker, start, end, period):
        self.calls.append(("candles", ticker))
        return {"candlesticks": [c for c in self.fx["candles"].get(ticker, []) if start <= c["end_period_ts"] <= end]}

    def get_series(self, series):
        return {"series": {"category": "Sports" if series.startswith("KXN") else "Science"}}


def test_parse_settled_market_filters_unusable():
    fx = _fixture()
    parsed = {m["ticker"]: T.parse_settled_market(m) for m in fx["markets"]}
    usable = [t for t, p in parsed.items() if p]
    assert len(usable) == 3
    assert parsed["KXNBAREB-26NOV08MIASAC-SACDSABONIS11-18"] is None  # scalar result
    assert parsed["KXPALLADIUMH-26NOV0900-T1048.99"] is None  # no volume
    assert parsed["KXNBATOTAL-26NOV08NOPLAC-266"] is None  # lived < 2h
    p = parsed["KXNCAAMBTOTAL-26NOV08UTAHAW-126"]
    assert p["result"] == "yes" and p["series"] == "KXNCAAMBTOTAL" and p["volume"] > 1000
    assert p["close_time"].endswith("+00:00")


def test_snapshots_use_pre_close_book_not_settlement_price():
    fx = _fixture()
    m = T.parse_settled_market(next(x for x in fx["markets"] if x["ticker"].startswith("KXNCAAMBTOTAL")))
    snaps = T.snapshots_from_candles(m, fx["candles"][m["ticker"]])
    assert snaps and snaps[0]["horizon_h"] == 1
    close = T._ts(m["close_time"])
    for s in snaps:
        assert T._ts(s["snapshot_ts"]) <= close - s["horizon_h"] * 3600
        assert 0 < s["yes_ask"] < 1 and 0 <= s["yes_bid"] < s["yes_ask"]
        assert 0 <= s["volume"] <= m["volume"]
        f = s["features"]
        assert f["espn_delta"] is None  # live-only feature: null, contributes 0
        assert f["market_p_yes"] == pytest.approx((s["yes_ask"] + s["yes_bid"]) / 2, abs=1e-6)
        assert f["market_edge"] == pytest.approx(M.logit(f["market_p_yes"]), abs=1e-5)


def test_reconstructed_features_match_live_scanner_schema():
    snap = {"horizon_h": 1, "snapshot_ts": "2026-11-08T10:00:00+00:00", "yes_ask": 0.2, "yes_bid": 0.18, "volume": 5000.0, "open_interest": 900.0}
    market = {"close_time": "2026-11-08T12:00:00+00:00"}
    hist = T.snapshot_features(market, snap)
    live = enrich_market(
        {"ticker": "KXT-1", "yes_ask_dollars": "0.20", "yes_bid_dollars": "0.18", "no_ask_dollars": "0.82",
         "volume_fp": "5000", "open_interest_fp": "900", "close_time": "2099-01-01T00:00:00Z"}
    )["model_features"]
    for k in ("market_edge", "volume_z", "spread_z", "market_p_yes", "yes_ask", "no_ask", "spread_cents"):
        assert hist[k] == pytest.approx(live[k], abs=1e-6), k
    assert set(M.FEATURES) <= set(hist)
    assert hist["time_to_close"] == pytest.approx(2 / 24 / 30, abs=1e-6)
    # null terms score as 0
    w = {k: 0.1 for k in M.WEIGHT_KEYS}
    assert M.predict(hist, w) == M.predict({**hist, "espn_delta": 0.0}, w)


def test_ingest_from_fixture_stores_markets_and_skips_known(tmp_path):
    fx = _fixture()
    led = Ledger(tmp_path / "t.db")
    client = FakeClient(fx)
    now = datetime(2026, 11, 9, 6, 0, tzinfo=timezone.utc)
    rep = T.ingest_settled(client, led, days=2, per_day=10, now=now)
    assert rep["stored"] == 3 and rep["errors"] == []
    counts = led.settled_counts_sync()
    assert counts["markets"] == 3 and counts["yes"] == 2 and counts["snapshots"] >= 3
    rows = led.settled_dataset_sync(horizon_h=1)
    assert {r["category"] for r in rows} <= {"Sports", "Science"}
    again = T.ingest_settled(client, led, days=2, per_day=10, now=now)
    assert again["stored"] == 0 and again["skipped_known"] == 3


def _rows(n, start=datetime(2026, 10, 1, tzinfo=timezone.utc), seed=7):
    import random

    rng = random.Random(seed)
    rows = []
    for i in range(n):
        p = rng.choice([0.03, 0.1, 0.3, 0.5, 0.7, 0.9, 0.97])
        y = 1 if rng.random() < p else 0
        f = M.build_features(p, volume=1000, spread_cents=2)
        f["market_p_yes"] = p
        ct = (start + timedelta(hours=i)).isoformat()
        rows.append({"ticker": f"M{i:03d}", "close_time": ct, "features": f, "yes_outcome": y, "source": "settled_market", "horizon_h": 1})
        rows.append({"ticker": f"M{i:03d}", "close_time": ct, "features": f, "yes_outcome": y, "source": "settled_market", "horizon_h": 6})
    return rows


def test_holdout_split_is_chronological_and_never_straddles():
    rows = _rows(50)
    shuffled = list(reversed(rows))
    train, hold = T.chronological_split(shuffled, 0.2)
    assert len({r["ticker"] for r in hold}) == 10
    assert max(r["close_time"] for r in train) < min(r["close_time"] for r in hold)
    assert not ({r["ticker"] for r in train} & {r["ticker"] for r in hold})
    assert T.chronological_split(rows, 0.2) == (sorted(train, key=lambda r: (r["ticker"], r["horizon_h"])), sorted(hold, key=lambda r: (r["ticker"], r["horizon_h"])))


def test_gate_promotes_better_fit(tmp_path):
    path = tmp_path / "w.json"
    rec = T.train_and_gate(Ledger(tmp_path / "x.db"), path=path, rows=_rows(200))
    assert rec["gate_pass"] and rec["promoted"]
    assert rec["holdout_brier_proposed"] < rec["holdout_brier_current"]
    doc = json.loads(path.read_text())
    assert doc["weights"] == rec["weights_proposed"] and doc["version"] == 1
    assert doc["last_training"]["promoted"] is True and doc["refit_log"][-1]["kind"] == "settled_train"


def test_gate_refuses_worse_brier(tmp_path, monkeypatch):
    path = tmp_path / "w.json"
    good = {"w0": 0.0, "w1": 1.0, "w2": 0.0, "w3": 0.0, "w4": 0.0, "w5": 0.0}
    M.save_model({"version": 3, "weights": dict(good), "last_refit": None, "refit_log": []}, path)
    monkeypatch.setattr(M, "fit", lambda samples, w, **kw: {k: (-1.0 if k == "w1" else 0.0) for k in M.WEIGHT_KEYS})
    rec = T.train_and_gate(Ledger(tmp_path / "x.db"), path=path, rows=_rows(200))
    assert rec["gate_pass"] is False and rec["promoted"] is False
    doc = json.loads(path.read_text())
    assert doc["version"] == 3 and doc["weights"]["w1"] == 1.0
    assert "did not improve" in doc["last_training"]["reason"]


def test_too_little_history_keeps_weights(tmp_path):
    path = tmp_path / "w.json"
    rec = T.train_and_gate(Ledger(tmp_path / "x.db"), path=path, rows=_rows(20))
    assert rec["promoted"] is False and "Not enough settled history" in rec["reason"]
    assert M.load_model(path)["weights"]["w1"] == 0.1


def test_snapshot_throttle_one_per_ticker_per_hour(tmp_path):
    led = Ledger(tmp_path / "s.db")
    t0 = datetime(2026, 11, 9, 1, 0, tzinfo=timezone.utc)
    rows = [{"ticker": f"K{i}", "model_prob": 0.4, "features": {}} for i in range(150)]
    assert led.add_model_snapshots_sync(rows, now=t0) == 150
    for k in range(1, 12):  # eleven more 5-second polls
        assert led.add_model_snapshots_sync(rows, now=t0 + timedelta(seconds=5 * k)) == 0
    assert led.count_model_runs_sync() == 150
    assert led.add_model_snapshots_sync(rows, now=t0 + timedelta(hours=1, seconds=1)) == 150


def test_scan_endpoint_does_not_write_per_poll(tmp_path, monkeypatch):
    from app import main

    led = Ledger(tmp_path / "scan.db")
    monkeypatch.setattr(main, "ledger", led)
    monkeypatch.setattr(main.orders, "ledger", led)
    rows = [{"ticker": f"KXTEST-{i}", "nash": {"model_prob_yes": 0.3}, "model_features": {"market_edge": -1.0}} for i in range(150)]

    def fake_scan(*a, **kw):
        return {"markets": [dict(r) for r in rows], "scanned": 150, "count": 150, "host": "fake"}

    monkeypatch.setattr(main, "scan_opportunities", fake_scan)
    http = TestClient(main.app)
    for _ in range(5):
        assert http.get("/api/markets/scan?refresh=true").status_code == 200
    assert led.count_model_runs_sync() == 150  # not 750

    # A bet forces a fresh snapshot for its ticker even inside the hour.
    res = http.post("/api/trading/order", json={"ticker": "KXTEST-7", "side": "yes", "count": 1, "price_cents": 30})
    assert res.status_code == 200, res.text
    runs = [r for r in led.model_runs_sync() if r["ticker"] == "KXTEST-7"]
    assert len(runs) == 2 and runs[-1]["kind"] == "bet"


def test_migration_compacts_old_poll_rows(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE model_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, model_prob REAL NOT NULL, features_json TEXT NOT NULL, ts TEXT NOT NULL)")
    for i in range(30):  # 30 polls inside one hour + 1 in the next hour
        con.execute("INSERT INTO model_runs(ticker, model_prob, features_json, ts) VALUES('A', 0.3, '{}', ?)", (f"2026-11-09T01:{i:02d}:00+00:00",))
    con.execute("INSERT INTO model_runs(ticker, model_prob, features_json, ts) VALUES('A', 0.4, '{}', '2026-11-09T02:00:00+00:00')")
    con.commit()
    con.close()
    led = Ledger(db)
    runs = led.model_runs_sync()
    assert len(runs) == 2 and runs[0]["ts"].startswith("2026-11-09T01:29")
    assert led.latest_model_run_sync("A", before_ts="2026-11-09T01:59:00+00:00")["model_prob"] == 0.3


def test_calibration_needs_minimum_sample():
    assert is_uncalibrated(0.1, MIN_CALIBRATION_N - 1)
    assert not is_uncalibrated(0.1, MIN_CALIBRATION_N)
    assert is_uncalibrated(0.3, 500)


def test_training_status_endpoint(tmp_path, monkeypatch):
    from app import main

    monkeypatch.setattr(main, "ledger", Ledger(tmp_path / "st.db"))
    data = TestClient(main.app).get("/api/training").json()
    assert {"settled", "last_training", "calibration", "gems_verified", "model_runs"} <= set(data)
    assert data["gems_verified"] is False

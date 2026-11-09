"""Item 2: calibration buckets, Brier, and the uncalibrated Kelly haircut."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db.ledger import Ledger, brier_score, bucket_of, is_uncalibrated
from app.strategy.scanner import enrich_market


def test_buckets_and_brier():
    assert bucket_of(0.0) == "0-10" and bucket_of(0.15) == "10-20" and bucket_of(1.0) == "90-100"
    assert brier_score([]) is None
    assert brier_score([(0.8, 1), (0.2, 0)]) == pytest.approx(0.04)
    assert is_uncalibrated(None) and is_uncalibrated(0.26) and not is_uncalibrated(0.2)


def test_settle_recomputes_calibration(tmp_path):
    led = Ledger(tmp_path / "c.db")
    ids = [
        led.add_bet_sync(ticker=f"T{i}", side="yes", price=0.5, stake=0.5, model_prob=p, market_prob=0.5)
        for i, p in enumerate([0.15, 0.12, 0.85])
    ]
    led.settle_bet_sync(ids[0], "no")
    led.settle_bet_sync(ids[1], "yes")
    res = led.settle_bet_sync(ids[2], "yes")
    cal = res["calibration"]
    b = {x["bucket"]: x for x in cal["buckets"]}
    assert b["10-20"]["n"] == 2 and b["10-20"]["actual"] == pytest.approx(0.5)
    assert b["10-20"]["predicted"] == pytest.approx(0.135)
    assert b["80-90"]["n"] == 1 and b["0-10"]["n"] == 0
    expected = ((0.15 - 0) ** 2 + (0.12 - 1) ** 2 + (0.85 - 1) ** 2) / 3
    assert cal["brier"] == pytest.approx(expected, abs=1e-6)
    assert cal["uncalibrated"] is True  # 0.27 > 0.25
    assert len(cal["buckets"]) == 10


def _market():
    return {
        "ticker": "KXT-1", "title": "t", "yes_ask_dollars": "0.2000", "no_ask_dollars": "0.8100",
        "yes_bid_dollars": "0.1900", "volume_fp": "5000", "open_interest_fp": "5000", "status": "open",
    }


def test_uncalibrated_halves_kelly():
    full = enrich_market(_market(), bankroll=1000, uncalibrated=False)
    half = enrich_market(_market(), bankroll=1000, uncalibrated=True)
    assert half["model_uncalibrated"] is True and half["kelly_scale"] == 0.5
    assert half["kelly"]["fractional_kelly"] == pytest.approx(full["kelly"]["fractional_kelly"] / 2, abs=1e-4)
    assert half["gemini_kelly"]["allocation"] == pytest.approx(full["gemini_kelly"]["allocation"] / 2, abs=0.02)


def test_calibration_endpoint():
    from app.main import app

    data = TestClient(app).get("/calibration").json()
    assert {"buckets", "brier", "n", "uncalibrated", "threshold"} <= set(data)
    assert data["threshold"] == 0.25

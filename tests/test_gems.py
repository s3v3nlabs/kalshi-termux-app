"""Gemini payout-anomaly ("hidden gem") layer: feature math, tiers, gem flag."""
from __future__ import annotations

import math

import pytest

from app.strategy.gems import (
    gem_check,
    payout_odds,
    payout_tier,
    spread_compression,
    vol_oi_ratio,
)
from app.strategy.scanner import enrich_market


def test_feature_math():
    assert spread_compression(0) == 1.0
    assert spread_compression(1) == pytest.approx(0.5)
    assert spread_compression(3) == pytest.approx(0.25)
    assert vol_oi_ratio(500, 99) == pytest.approx(5.0)
    assert vol_oi_ratio(10, 0) == pytest.approx(10.0)
    assert payout_odds(0.08) == pytest.approx(12.5)
    assert payout_odds(0.5) == pytest.approx(2.0)
    assert payout_odds(0) == 2.0 and payout_odds(None) == 2.0


def test_payout_tiers():
    assert payout_tier(1.25) == "2x" and payout_tier(3.0) == "2x"
    assert payout_tier(3.01) == "mid" and payout_tier(9.99) == "mid"
    assert payout_tier(10.0) == "13x" and payout_tier(12.5) == "13x" and payout_tier(50) == "13x"


def test_fed_corridor_scrap_is_a_gem():
    # Scrap example: "Federal Reserve Rate Corridor", odds 12.5, base_prob 0.12.
    g = gem_check(p=0.12, implied_prob=0.08, spread_cents=1, volume=500, open_interest=400, bankroll=1000)
    assert g["payout_tier"] == "13x" and g["payout_odds"] == pytest.approx(12.5)
    assert g["nash_payoff"] == pytest.approx(0.12 * 12.5 - 0.88)  # 0.62
    kelly_f = (0.12 * 12.5 - 0.88) / 12.5
    assert g["half_kelly_fraction"] == pytest.approx(kelly_f * 0.5, abs=1e-6)
    assert g["half_kelly_allocation"] == pytest.approx(1000 * kelly_f * 0.5, abs=0.01)
    assert g["edge_vs_implied"] == pytest.approx(0.04)
    assert all(g["checks"].values()) and g["gem"] is True


@pytest.mark.parametrize(
    "kw, failed",
    [
        (dict(p=0.60, implied_prob=0.50, spread_cents=1), "high_payout_tier"),  # 2x tier
        (dict(p=0.12, implied_prob=0.08, spread_cents=5), "tight_spread"),
        (dict(p=0.09, implied_prob=0.08, spread_cents=1), "nash_go"),  # 0.09*12.5-0.91 = 0.215
        (dict(p=0.115, implied_prob=0.10, spread_cents=1), "beats_implied"),  # +1.5pp only
    ],
)
def test_each_check_can_block_the_gem(kw, failed):
    g = gem_check(**kw)
    assert g["checks"][failed] is False and g["gem"] is False


def test_half_kelly_sign_matches_nash_sign():
    # Gemini kelly_f = (p*odds - q)/odds, so half-Kelly > 0 exactly when nash_payoff > 0.
    for p, implied in [(0.05, 0.08), (0.07, 0.08), (0.08, 0.08), (0.3, 0.5), (0.6, 0.5)]:
        g = gem_check(p=p, implied_prob=implied, spread_cents=1)
        assert g["checks"]["positive_half_kelly"] is (g["nash_payoff"] > 0)
    g = gem_check(p=0.05, implied_prob=0.08, spread_cents=1)  # 0.05*12.5 - 0.95 < 0
    assert g["half_kelly_fraction"] == 0 and g["gem"] is False


def _tail_market(spread_cents=1):
    bid = 0.08 - spread_cents / 100
    return {
        "ticker": "KXFEDCORRIDOR-26DEC-T5", "title": "Federal Reserve Rate Corridor",
        "yes_ask_dollars": "0.0800", "yes_bid_dollars": f"{bid:.4f}", "no_ask_dollars": "0.9300",
        "volume_fp": "800", "open_interest_fp": "400", "status": "open",
    }


def _weights_for(p):
    return {"w0": math.log(p / (1 - p)), "w1": 0.0, "w2": 0.0, "w3": 0.0, "w4": 0.0, "w5": 0.0}


def test_scanner_row_carries_gem_fields():
    row = enrich_market(_tail_market(), bankroll=1000, weights=_weights_for(0.14))
    g = row["gemini"]
    assert row["best_side"] == "yes" and g["implied_prob"] == pytest.approx(0.08)
    assert g["payout_odds"] == pytest.approx(12.5) and row["payout_tier"] == "13x"
    assert g["spread_cents"] == pytest.approx(1.0) and g["spread_compression"] == pytest.approx(0.5)
    assert g["vol_oi_ratio"] == pytest.approx(800 / 401, abs=1e-3)
    assert g["nash_payoff"] == pytest.approx(row["nash_payoff_gemini"], abs=1e-3)
    assert row["gem"] is True


def test_scanner_wide_spread_or_no_edge_is_not_gem():
    assert enrich_market(_tail_market(spread_cents=6), weights=_weights_for(0.14))["gem"] is False
    assert enrich_market(_tail_market(), weights=_weights_for(0.08))["gem"] is False


def test_uncalibrated_halves_gem_kelly_but_keeps_flag_math():
    full = enrich_market(_tail_market(), weights=_weights_for(0.14), uncalibrated=False)["gemini"]
    half = enrich_market(_tail_market(), weights=_weights_for(0.14), uncalibrated=True)["gemini"]
    assert half["half_kelly_fraction"] == pytest.approx(full["half_kelly_fraction"] / 2, abs=1e-6)


def test_no_guaranteed_language():
    import pathlib

    src = pathlib.Path("app/strategy/gems.py").read_text().lower()
    js = pathlib.Path("static/js/app.js").read_text()
    gem_js = js[js.index("function gemBadge") : js.index("function scanRowHtml")].lower()
    for word in ("guaranteed", "lock", "sure win", "can't lose"):
        assert word not in src.replace("not a prediction", "")
        assert word not in gem_js


def test_gem_status_unverified_when_uncalibrated():
    assert enrich_market(_tail_market(), weights=_weights_for(0.14), uncalibrated=True)["gem_status"] == "unverified"
    assert enrich_market(_tail_market(), weights=_weights_for(0.14), uncalibrated=False)["gem_status"] == "candidate"
    assert enrich_market(_tail_market(spread_cents=6), weights=_weights_for(0.14))["gem_status"] is None

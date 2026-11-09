"""Gemini payout-anomaly ("hidden gem") layer, from the ai_agent.py scrap.

Per market, for the side we would buy (the higher-payout side):

    spread             = yes_ask - yes_bid            (cents)
    spread_compression = 1 / (spread + 1)
    vol_oi_ratio       = volume / (open_interest + 1)
    implied_prob       = ask price of the side we buy (dollars)
    payout_odds        = 1 / implied_prob, or 2.0 when implied_prob <= 0

Payout tier: "2x" (<= 3x), "13x" (>= 10x), otherwise "mid".

A row is flagged as a gem candidate only when every check passes:
  tier == "13x", spread_compression >= 0.25 (spread <= 3c), Nash payoff > 0.4,
  model p beats implied_prob by >= 2 points, and Gemini half-Kelly > 0.
The flag is a screen for unusual prices, not a prediction that the bet wins.
"""
from __future__ import annotations

from typing import Optional

from app.strategy.kelly import gemini_nash_payoff, kelly_from_payout_odds

TIER_LOW_MAX = 3.0  # <= 3x  -> "2x"
TIER_HIGH_MIN = 10.0  # >= 10x -> "13x"
GEM_MIN_COMPRESSION = 0.25  # spread <= 3 cents
GEM_NASH_THRESHOLD = 0.4
GEM_MIN_EDGE = 0.02  # model p - implied_prob
DEFAULT_ODDS = 2.0


def spread_compression(spread_cents: float) -> float:
    return 1.0 / (max(0.0, float(spread_cents)) + 1.0)


def vol_oi_ratio(volume: float, open_interest: float) -> float:
    return float(volume or 0.0) / (float(open_interest or 0.0) + 1.0)


def payout_odds(implied_prob: Optional[float]) -> float:
    if implied_prob is None or implied_prob <= 0:
        return DEFAULT_ODDS
    return 1.0 / float(implied_prob)


def payout_tier(odds: float) -> str:
    if odds <= TIER_LOW_MAX:
        return "2x"
    if odds >= TIER_HIGH_MIN:
        return "13x"
    return "mid"


def gem_check(
    *,
    p: float,
    implied_prob: Optional[float],
    spread_cents: float,
    volume: float = 0.0,
    open_interest: float = 0.0,
    bankroll: float = 1000.0,
    kelly_scale: float = 1.0,
) -> dict:
    """All Gemini features + the gem flag for one side of one market."""
    odds = payout_odds(implied_prob)
    comp = spread_compression(spread_cents)
    nash_pay = gemini_nash_payoff(p, odds)
    gk = kelly_from_payout_odds(p=p, payout_odds=odds, bankroll=bankroll, fraction=0.5 * kelly_scale)
    tier = payout_tier(odds)
    edge = float(p) - float(implied_prob or 0.0)
    checks = {
        "high_payout_tier": tier == "13x",
        "tight_spread": comp >= GEM_MIN_COMPRESSION,
        "nash_go": nash_pay > GEM_NASH_THRESHOLD,
        "beats_implied": edge >= GEM_MIN_EDGE,
        "positive_half_kelly": gk.fractional > 0,
    }
    return {
        "spread_cents": round(float(spread_cents), 2),
        "spread_compression": round(comp, 4),
        "vol_oi_ratio": round(vol_oi_ratio(volume, open_interest), 4),
        "implied_prob": None if implied_prob is None else round(float(implied_prob), 4),
        "payout_odds": round(odds, 4),
        "payout_tier": tier,
        "p": round(float(p), 4),
        "edge_vs_implied": round(edge, 4),
        "nash_payoff": round(nash_pay, 4),
        "half_kelly_fraction": round(gk.fractional, 6),
        "half_kelly_allocation": gk.allocation,
        "checks": checks,
        "gem": all(checks.values()),
    }

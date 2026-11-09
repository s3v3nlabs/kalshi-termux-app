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
from app.strategy.model import flow_z as _flow_z

TIER_LOW_MAX = 3.0  # <= 3x  -> "2x"
TIER_HIGH_MIN = 10.0  # >= 10x -> "13x"
GEM_MIN_COMPRESSION = 0.25  # spread <= 3 cents
GEM_NASH_THRESHOLD = 0.4
GEM_MIN_EDGE = 0.02  # model p - implied_prob
DEFAULT_ODDS = 2.0
# Informational only (not a gem check): volume at least 3x open interest.
UNUSUAL_FLOW_RATIO = 3.0


def spread_compression(spread_cents: float) -> float:
    return 1.0 / (max(0.0, float(spread_cents)) + 1.0)


def vol_oi_ratio(volume: float, open_interest: float) -> float:
    return float(volume or 0.0) / (float(open_interest or 0.0) + 1.0)


def unusual_flow(ratio: float) -> bool:
    return float(ratio) >= UNUSUAL_FLOW_RATIO


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
    ratio = vol_oi_ratio(volume, open_interest)
    return {
        "spread_cents": round(float(spread_cents), 2),
        "spread_compression": round(comp, 4),
        "vol_oi_ratio": round(ratio, 4),
        "flow_z": _flow_z(volume, open_interest),
        "unusual_flow": unusual_flow(ratio),
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


def suppress_paired_gems(rows: list[dict]) -> int:
    """YES+NO sanity: at most one gem per event.

    The untrained model flagged both sides of one (probably decided) NFL game:
    team A's YES and team B's YES are two markets of the same event, i.e. the
    two sides of one outcome. Within an event (``event_ticker``; the ticker
    itself when absent) only the gem with the highest Nash payoff stays; ties go
    to the higher payout, then the ticker. The rest lose the flag and say why.
    Returns how many were suppressed.
    """
    groups: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("gem"):
            groups.setdefault(str(r.get("event_ticker") or r.get("ticker")), []).append(r)
    dropped = 0
    for event, members in groups.items():
        if len(members) < 2:
            continue
        members.sort(
            key=lambda r: (
                float((r.get("gemini") or {}).get("nash_payoff") or 0.0),
                float(r.get("best_payout_multiple") or 0.0),
                str(r.get("ticker")),
            ),
            reverse=True,
        )
        keep = members[0]
        for r in members[1:]:
            r["gem"] = False
            r["gem_status"] = None
            r["gem_suppressed"] = {
                "kept": keep.get("ticker"),
                "event": event,
                "reason": f"Same event as {keep.get('ticker')}, which has the higher Nash payoff. Only one side of an event is flagged.",
            }
            if isinstance(r.get("gemini"), dict):
                r["gemini"]["gem"] = False
            dropped += 1
    return dropped

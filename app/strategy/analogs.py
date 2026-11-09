"""Analog library: how did similar *settled* setups resolve?

A setup is the side the scanner would buy (the higher-payout side) at a
pre-close snapshot. For each settled market we use its 1-hour-before-close
snapshot (``settled_snapshots.horizon_h = 1``):

    side         = cheaper of YES ask and NO ask (= 1 - YES bid)
    implied_prob = that ask
    payout tier  = 2x / mid / 13x (same cut-offs as the gem layer)
    spread_compression = 1 / (spread_cents + 1)
    vol_oi_ratio = volume / (open_interest + 1)
    hit          = the market settled in favour of that side

Match rules (all must hold):
    same payout tier
    |implied - implied0|          <= max(1c, 35% of implied0)
    |compression - compression0| <= 0.20
    |log1p(ratio) - log1p(ratio0)| <= 1.0

Fewer than ``MIN_ANALOGS`` matches -> "not enough analogs" and no rate. A high
rate is history, not a promise, and never promotes ◆ GEM? to ◆ GEM.
"""
from __future__ import annotations

import math
from typing import Optional

from app.strategy.gems import payout_odds, payout_tier, spread_compression, vol_oi_ratio

MIN_ANALOGS = 10
IMPLIED_REL_TOL = 0.35
IMPLIED_ABS_TOL = 0.01
COMPRESSION_TOL = 0.20
FLOW_LOG_TOL = 1.0
ANALOG_HORIZON_H = 1


def setup_from_prices(yes_ask: float, yes_bid: Optional[float], volume: float, open_interest: float, no_ask: Optional[float] = None) -> Optional[dict]:
    if yes_ask is None or not 0 < float(yes_ask) < 1:
        return None
    if no_ask is None:
        if yes_bid is None:
            return None
        no_ask = 1.0 - float(yes_bid)
    yes_ask, no_ask = float(yes_ask), float(no_ask)
    if not 0 < no_ask <= 1:
        return None
    side, implied = ("yes", yes_ask) if yes_ask <= no_ask else ("no", no_ask)
    spread = (yes_ask - float(yes_bid)) * 100.0 if yes_bid is not None else abs(yes_ask + no_ask - 1.0) * 100.0
    ratio = vol_oi_ratio(volume, open_interest)
    return {
        "side": side,
        "implied_prob": implied,
        "tier": payout_tier(payout_odds(implied)),
        "spread_compression": spread_compression(spread),
        "vol_oi_ratio": ratio,
    }


def build_pool(settled_rows: list[dict]) -> list[dict]:
    """settled_rows: ``Ledger.settled_dataset(horizon_h=1)`` rows."""
    pool = []
    for r in settled_rows:
        s = setup_from_prices(r.get("yes_ask"), r.get("yes_bid"), r.get("snap_volume") or 0.0, r.get("snap_oi") or 0.0)
        if s is None:
            continue
        s["ticker"] = r["ticker"]
        s["hit"] = 1 if str(r.get("result")) == s["side"] else 0
        pool.append(s)
    return pool


def load_pool(ledger) -> list[dict]:
    try:
        return build_pool(ledger.settled_dataset_sync(horizon_h=ANALOG_HORIZON_H))
    except Exception:
        return []


def matches(a: dict, b: dict) -> bool:
    if a["tier"] != b["tier"]:
        return False
    tol = max(IMPLIED_ABS_TOL, IMPLIED_REL_TOL * a["implied_prob"])
    if abs(a["implied_prob"] - b["implied_prob"]) > tol:
        return False
    if abs(a["spread_compression"] - b["spread_compression"]) > COMPRESSION_TOL:
        return False
    return abs(math.log1p(a["vol_oi_ratio"]) - math.log1p(b["vol_oi_ratio"])) <= FLOW_LOG_TOL


def analog_stats(setup: dict, pool: list[dict], min_n: int = MIN_ANALOGS, exclude: Optional[str] = None) -> dict:
    hits = n = 0
    implied_sum = 0.0
    for b in pool:
        if exclude and b.get("ticker") == exclude:
            continue
        if matches(setup, b):
            n += 1
            hits += b["hit"]
            implied_sum += b["implied_prob"]
    enough = n >= min_n
    return {
        "analog_n": n,
        "analog_hits": hits,
        "analog_hit_rate": round(hits / n, 4) if enough else None,
        "analog_avg_implied": round(implied_sum / n, 4) if n else None,
        "analog_min_n": min_n,
        "analog_status": "ok" if enough else "insufficient",
        "analog_text": (
            f"Similar settled setups: {hits} of {n} hit (avg implied {implied_sum / n * 100:.1f}%). History, not a promise."
            if enough
            else f"Not enough analogs ({n} similar settled setups; need {min_n})."
        ),
    }


def row_setup(row: dict) -> Optional[dict]:
    """Setup for a live scan row, using the gem layer's buy-side numbers."""
    g = row.get("gemini") or {}
    if g.get("implied_prob") is None:
        return None
    return {
        "side": row.get("best_side"),
        "implied_prob": float(g["implied_prob"]),
        "tier": g.get("payout_tier"),
        "spread_compression": float(g.get("spread_compression") or 0.0),
        "vol_oi_ratio": float(g.get("vol_oi_ratio") or 0.0),
    }


def annotate_rows(rows: list[dict], pool: list[dict], min_n: int = MIN_ANALOGS) -> None:
    for row in rows:
        s = row_setup(row)
        row["analogs"] = analog_stats(s, pool, min_n, exclude=row.get("ticker")) if s else None

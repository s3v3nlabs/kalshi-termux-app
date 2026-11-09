"""Simple 2-player Nash / Socratic stress-test vs market maker / crowd.

Deterministic game-theory layer — no LLM required.
Treats YOU vs MARKET as a zero-sum-ish payoff over BUY YES / BUY NO / PASS.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

Action = Literal["BUY_YES", "BUY_NO", "PASS"]


@dataclass
class NashResult:
    action: Action
    coherence: float
    payoff_matrix: dict
    your_best_payoff: float
    market_implied_yes: float
    model_prob_yes: float
    socratic_notes: list[str]
    edge_yes: float
    edge_no: float


def _clamp(x: float, lo: float = 0.01, hi: float = 0.99) -> float:
    return max(lo, min(hi, x))


def nash_decision(
    yes_ask: float,
    no_ask: float,
    model_prob_yes: float | None = None,
    spread_cents: float | None = None,
    volume: float = 0.0,
    open_interest: float = 0.0,
) -> NashResult:
    """
    Prices in dollars (0–1). If model_prob not given, derive a mild prior from
    the logistic scorer in app.strategy.model (no fake certainty).
    """
    yes_ask = _clamp(yes_ask)
    no_ask = _clamp(no_ask)
    mid = yes_ask  # ask as conservative entry
    # Implied from book (yes_ask + no_ask often ~1 + spread)
    market_implied = yes_ask

    if model_prob_yes is None:
        # Logistic scorer (app.strategy.model) replaces the old shrink prior.
        from app.strategy.model import build_features, predict

        spread_for_model = spread_cents if spread_cents is not None else abs((yes_ask + no_ask - 1.0) * 100)
        market_p = _clamp((yes_ask + (1.0 - no_ask)) / 2.0)
        model_p = predict(build_features(market_p, volume=volume, spread_cents=spread_for_model))
    else:
        model_p = _clamp(model_prob_yes)

    edge_yes = model_p * 1.0 - yes_ask
    edge_no = (1.0 - model_p) * 1.0 - no_ask

    # Payoff matrix rows = your action, cols = nature (YES settles / NO settles)
    # Values = expected utility approx (edge adjusted by coherence penalty for spread)
    spread = spread_cents if spread_cents is not None else abs((yes_ask + no_ask - 1.0) * 100)
    liquidity_penalty = min(0.15, spread / 200.0)

    uy_yes = edge_yes - liquidity_penalty
    uy_no_settle = -yes_ask  # lose stake if wrong
    un_no = edge_no - liquidity_penalty
    un_yes_settle = -no_ask
    upass = 0.0

    matrix = {
        "BUY_YES": {"YES_settles": round(1.0 - yes_ask, 4), "NO_settles": round(-yes_ask, 4), "EV": round(edge_yes, 4)},
        "BUY_NO": {"YES_settles": round(-no_ask, 4), "NO_settles": round(1.0 - no_ask, 4), "EV": round(edge_no, 4)},
        "PASS": {"YES_settles": 0.0, "NO_settles": 0.0, "EV": 0.0},
    }

    # Socratic stress questions → coherence score
    notes: list[str] = []
    coherence = 0.55

    if edge_yes > 0.05 or edge_no > 0.05:
        coherence += 0.12
        notes.append("Positive model edge vs ask price.")
    else:
        notes.append("No meaningful edge vs ask — prefer PASS.")
        coherence -= 0.08

    if spread > 8:
        coherence -= 0.1
        notes.append(f"Wide spread (~{spread:.1f}¢) — market maker extracts more.")
    else:
        coherence += 0.05
        notes.append("Tight-ish spread supports cleaner entry.")

    if volume + open_interest < 100:
        coherence -= 0.12
        notes.append("Thin liquidity / OI — adverse selection risk vs informed flow.")
    else:
        coherence += 0.05
        notes.append("Adequate volume/OI for size discipline.")

    # Information asymmetry stress-test
    if abs(model_p - market_implied) > 0.2:
        coherence -= 0.08
        notes.append("Large model–market disagreement. What does the crowd know that you don't?")
    else:
        notes.append("Model close to market; edge is incremental, not heroic.")

    # Mean-reversion honesty
    if yes_ask < 0.15 or yes_ask > 0.85:
        notes.append("Tail contract: high payout multiple but low base rate — Kelly will shrink hard.")
        coherence -= 0.05

    coherence = _clamp(coherence, 0.05, 0.95)

    # Equilibrium action: choose best EV among actions if coherence above floor
    candidates = [
        ("BUY_YES", edge_yes),
        ("BUY_NO", edge_no),
        ("PASS", 0.0),
    ]
    candidates.sort(key=lambda x: x[1], reverse=True)
    best_action, best_payoff = candidates[0]

    if best_payoff < 0.02 or coherence < 0.45:
        best_action, best_payoff = "PASS", 0.0
        notes.append("Equilibrium: PASS (edge or coherence below threshold).")
    else:
        notes.append(f"Equilibrium: {best_action} with EV≈{best_payoff:.4f}.")

    return NashResult(
        action=best_action,  # type: ignore[arg-type]
        coherence=round(coherence, 4),
        payoff_matrix=matrix,
        your_best_payoff=round(best_payoff, 4),
        market_implied_yes=round(market_implied, 4),
        model_prob_yes=round(model_p, 4),
        socratic_notes=notes,
        edge_yes=round(edge_yes, 4),
        edge_no=round(edge_no, 4),
    )


def nash_to_dict(result: NashResult) -> dict:
    return asdict(result)

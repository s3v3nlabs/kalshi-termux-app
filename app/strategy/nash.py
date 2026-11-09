"""Simple 2-player Nash / Socratic stress-test vs market maker / crowd.

Deterministic game-theory layer — no LLM required.
Treats YOU vs MARKET as a zero-sum-ish payoff over BUY YES / BUY NO / PASS.
"""
from __future__ import annotations

import math
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


# ---------------------------------------------------------------------------
# Second solver: multi-agent softmax (logit quantal-response) equilibrium.
# ---------------------------------------------------------------------------

def softmax_equilibrium(
    markets: list[dict],
    temperature: float = 0.1,
    steps: int = 2000,
    eta: float = 0.05,
    tol: float = 1e-7,
) -> dict[str, float]:
    """Equilibrium YES price per market for two kinds of agents.

    Each market dict needs ``ticker``, ``model_p`` (model P(YES)),
    ``market_p`` (book P(YES)) and ``volume``.

    * One **informed** agent with 1 unit of capital spread over every
      market: it picks among BUY_YES_i, BUY_NO_i and PASS with softmax
      probabilities over expected value / temperature, using model_p.
    * One **book** agent per market with capital equal to that market's
      volume share: same softmax choice over its own market, using market_p.

    Net YES demand moves each price: q_i += eta * (yes_i - no_i). At the
    fixed point the informed and book flows cancel, so liquid markets stay
    near the book and thin ones lean toward the model. Returns
    ``{ticker: nash_prob}``. Deterministic; no randomness.
    """
    if not markets:
        return {}
    t = max(1e-6, float(temperature))
    p = [_clamp(float(m["model_p"])) for m in markets]
    b = [_clamp(float(m["market_p"])) for m in markets]
    vol = [max(0.0, float(m.get("volume") or 0.0)) for m in markets]
    total = sum(vol)
    share = [v / total if total > 0 else 1.0 / len(markets) for v in vol]
    q = list(b)
    n = len(markets)
    for _ in range(steps):
        # Informed agent: one softmax over 2n + 1 actions (PASS has EV 0).
        logits = []
        for i in range(n):
            logits.append((p[i] - q[i]) / t)  # BUY_YES_i
            logits.append((q[i] - p[i]) / t)  # BUY_NO_i
        logits.append(0.0)  # PASS
        mx = max(logits)
        ex = [math.exp(x - mx) for x in logits]
        z = sum(ex)
        moved = 0.0
        for i in range(n):
            inf_yes, inf_no = ex[2 * i] / z, ex[2 * i + 1] / z
            # Book agent for market i: softmax over its own YES/NO/PASS.
            by, bn = (b[i] - q[i]) / t, (q[i] - b[i]) / t
            m2 = max(by, bn, 0.0)
            ey, en, ep = math.exp(by - m2), math.exp(bn - m2), math.exp(-m2)
            zb = ey + en + ep
            demand = (inf_yes - inf_no) + share[i] * n * (ey - en) / zb
            new_q = _clamp(q[i] + eta * demand)
            moved = max(moved, abs(new_q - q[i]))
            q[i] = new_q
        if moved < tol:
            break
    return {m["ticker"]: round(q[i], 6) for i, m in enumerate(markets)}


def mispricing_scores(rows: list[dict], top_n: int = 10, temperature: float = 0.1) -> dict[str, dict]:
    """Run the equilibrium over the top ``top_n`` rows by volume.

    Each row needs ``ticker``, ``volume``, ``model_p`` and ``market_p``.
    Returns ``{ticker: {nash_prob, mispricing}}`` where
    mispricing = |nash_prob - market_prob|.
    """
    top = sorted(rows, key=lambda r: float(r.get("volume") or 0.0), reverse=True)[:top_n]
    eq = softmax_equilibrium(top, temperature=temperature)
    out = {}
    for r in top:
        q = eq[r["ticker"]]
        out[r["ticker"]] = {"nash_prob": q, "mispricing": round(abs(q - float(r["market_p"])), 6)}
    return out

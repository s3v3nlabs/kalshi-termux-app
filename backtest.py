#!/usr/bin/env python3
"""Offline backtest: replay stored model_runs against stored outcomes.

No live calls. Reads the local ledger (data/kalshi_portal.db) or a JSON
fixture, re-scores every model run with a weight set, and reports:

  * Brier score
  * hit rate by probability bucket (0-10%, ..., 90-100%)
  * simulated P&L at 0.5 Kelly (fixed $1,000 bankroll, 5% cap per bet)

Gate: a change ships only if Brier does not get worse (``gate()``).

Usage:
  python backtest.py                    # ledger + current weights
  python backtest.py --fixture tests/fixtures/backtest_fixture.json
  python backtest.py --propose          # also fit proposed weights and compare
  python backtest.py --weights path.json
Exit code 1 when --propose is given and the proposed weights fail the gate.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from app.db.ledger import BUCKETS, Ledger, brier_score, bucket_of
from app.strategy import model as M

BANKROLL = 1000.0
KELLY_FRACTION = 0.5
MAX_FRACTION = 0.05


def load_ledger_dataset(ledger: Ledger) -> list[dict]:
    """One row per ticker with a known result: its latest model run before the first settled bet."""
    outcomes: dict[str, tuple[int, str]] = {}
    for bet in ledger.bets_sync(settled=True):
        yes_won = bet["outcome"] if bet["side"] == "yes" else 1 - bet["outcome"]
        outcomes.setdefault(bet["ticker"], (int(yes_won), bet["ts"]))
    rows = []
    for ticker, (yes_won, ts) in outcomes.items():
        run = ledger.latest_model_run_sync(ticker, before_ts=ts) or ledger.latest_model_run_sync(ticker)
        if run:
            rows.append({"ticker": ticker, "features": run["features"], "yes_outcome": yes_won, "stored_p": run["model_prob"]})
    return rows


def load_fixture(path: Path) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))["rows"]


def _side_prices(feats: dict) -> tuple[float, float]:
    mp = float(feats.get("market_p_yes") or M.sigmoid(float(feats.get("market_edge", 0.0))))
    yes = float(feats.get("yes_ask") or mp)
    no = float(feats.get("no_ask") or (1.0 - mp))
    return min(0.99, max(0.01, yes)), min(0.99, max(0.01, no))


def run_backtest(rows: list[dict], weights: dict) -> dict:
    pairs = []
    buckets = {b: {"bucket": b, "n": 0, "hits": 0, "predicted_sum": 0.0, "actual_sum": 0} for b in BUCKETS}
    pnl = 0.0
    bets = wins = 0
    curve = []
    for r in rows:
        feats = {k: M.feature_value(r["features"], k) for k in M.FEATURES}
        p = M.predict(feats, weights)
        y = int(r["yes_outcome"])
        pairs.append((p, y))
        b = buckets[bucket_of(p)]
        b["n"] += 1
        b["hits"] += int((p >= 0.5) == (y == 1))
        b["predicted_sum"] += p
        b["actual_sum"] += y
        yes_px, no_px = _side_prices(r["features"])
        edge_yes, edge_no = p - yes_px, (1 - p) - no_px
        side, prob, px = ("yes", p, yes_px) if edge_yes >= edge_no else ("no", 1 - p, no_px)
        if prob - px > 0:
            f = min(MAX_FRACTION, KELLY_FRACTION * (prob - px) / (1 - px))
            stake = BANKROLL * f
            won = (y == 1) == (side == "yes")
            pnl += stake * (1 - px) / px if won else -stake
            bets += 1
            wins += int(won)
        curve.append(round(pnl, 4))
    out_buckets = []
    for b in buckets.values():
        n = b["n"]
        out_buckets.append(
            {
                "bucket": b["bucket"],
                "n": n,
                "hit_rate": round(b["hits"] / n, 4) if n else None,
                "predicted": round(b["predicted_sum"] / n, 4) if n else None,
                "actual": round(b["actual_sum"] / n, 4) if n else None,
            }
        )
    hits = sum(int((p >= 0.5) == (y == 1)) for p, y in pairs)
    return {
        "n": len(pairs),
        "brier": brier_score(pairs),
        "hit_rate": round(hits / len(pairs), 4) if pairs else None,
        "buckets": out_buckets,
        "pnl": {"kelly_fraction": KELLY_FRACTION, "bankroll": BANKROLL, "bets": bets, "wins": wins, "total": round(pnl, 2), "curve": curve},
    }


def gate(candidate_brier: Optional[float], baseline_brier: Optional[float]) -> bool:
    """True when the candidate may ship: Brier did not get worse."""
    if candidate_brier is None:
        return False
    if baseline_brier is None:
        return True
    return candidate_brier <= baseline_brier + 1e-9


def propose_weights(rows: list[dict], weights: dict) -> dict:
    samples = [({k: M.feature_value(r["features"], k) for k in M.FEATURES}, int(r["yes_outcome"])) for r in rows]
    return M.fit(samples, weights)


def compare(rows: list[dict], current: dict) -> dict:
    proposed = propose_weights(rows, current)
    cur = run_backtest(rows, current)
    new = run_backtest(rows, proposed)
    return {
        "current": {"weights": current, "report": cur},
        "proposed": {"weights": proposed, "report": new},
        "gate_pass": gate(new["brier"], cur["brier"]),
    }


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fixture", type=Path)
    ap.add_argument("--db", type=Path)
    ap.add_argument("--weights", type=Path)
    ap.add_argument("--propose", action="store_true")
    args = ap.parse_args(argv)
    rows = load_fixture(args.fixture) if args.fixture else load_ledger_dataset(Ledger(args.db) if args.db else Ledger())
    weights = M.load_model(args.weights)["weights"]
    if not rows:
        print(json.dumps({"n": 0, "note": "No settled history yet. Try --fixture tests/fixtures/backtest_fixture.json"}))
        return 0
    if args.propose:
        result = compare(rows, weights)
        for side in ("current", "proposed"):
            result[side]["report"]["pnl"].pop("curve", None)
        print(json.dumps(result, indent=2))
        return 0 if result["gate_pass"] else 1
    report = run_backtest(rows, weights)
    report["pnl"].pop("curve", None)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

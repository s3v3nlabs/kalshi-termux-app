#!/usr/bin/env python3
"""Train the logistic model on Kalshi settled markets (public API, no keys).

  python train.py --ingest --days 21 --per-day 20 --max-markets 400   # pull history, then refit
  python train.py                       # refit on what is already stored
  python train.py --status              # sample size, last gate result
  python train.py --compact             # trim old per-poll model_runs (1 per ticker per hour)
  python train.py --no-promote          # report only; never writes weights

Promotion only happens when holdout Brier (last 20% of markets by close time)
improves on the current weights. Gems stay unverified until calibration on
settled bets passes; this script does not change that.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from app.db.ledger import Ledger
from app.strategy import training as T


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path)
    ap.add_argument("--weights", type=Path, help="weights file (default data/model_weights.json)")
    ap.add_argument("--ingest", action="store_true")
    ap.add_argument("--days", type=int, default=T.DEFAULT_DAYS)
    ap.add_argument("--per-day", type=int, default=T.DEFAULT_PER_DAY)
    ap.add_argument("--max-markets", type=int, default=T.DEFAULT_MAX_MARKETS)
    ap.add_argument("--epochs", type=int, default=T.SETTLED_EPOCHS)
    ap.add_argument("--no-promote", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--compact", action="store_true")
    args = ap.parse_args(argv)
    ledger = Ledger(args.db) if args.db else Ledger()
    if args.compact:
        print(json.dumps({"model_runs_deleted": ledger.compact_model_runs_sync(), "model_runs": ledger.count_model_runs_sync()}))
    if args.status:
        print(json.dumps(T.training_status(ledger, args.weights), indent=2, default=str))
        return 0
    if args.ingest:
        from app.kalshi.client import KalshiClient

        report = T.ingest_settled(
            KalshiClient(), ledger, days=args.days, per_day=args.per_day, max_markets=args.max_markets,
            progress=lambda msg: print(msg, file=sys.stderr),
        )
        print(json.dumps({"ingest": report}, indent=2, default=str))
    record = T.train_and_gate(ledger, path=args.weights, epochs=args.epochs, promote=not args.no_promote)
    print(json.dumps({"training": record}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Logistic scorer that replaces the old shrink-toward-50% prior.

    z = w0 + w1*market_edge + w2*time_to_close + w3*volume_z + w4*espn_delta + w5*spread_z
    p = 1 / (1 + e^-z)          (P(YES), clamped to [0.01, 0.99])

Feature definitions (simplest reading of the spec; see BUILD_NOTES):
  market_edge   = logit(market-implied P(YES))  — the book's lean away from 50/50, in log-odds
  time_to_close = days until close / 30, clipped to [0, 1]
  volume_z      = (log1p(volume) - 6) / 3      — fixed reference, no batch stats needed
  espn_delta    = logit(ESPN P(YES)) - logit(market P(YES)), 0 when no ESPN match
  spread_z      = (spread_cents - 3) / 3

Weights start at 0.1 (``model_weights.json`` in this folder). Refits are
written to ``data/model_weights.json`` (gitignored) and win over the default.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from app.config import settings

log = logging.getLogger("kalshi.model")

FEATURES = ("market_edge", "time_to_close", "volume_z", "espn_delta", "spread_z")
WEIGHT_KEYS = ("w0", "w1", "w2", "w3", "w4", "w5")
DEFAULT_WEIGHTS_PATH = Path(__file__).with_name("model_weights.json")
REFIT_INTERVAL = timedelta(days=7)
MIN_REFIT_SAMPLES = 5
LR, EPOCHS, L2 = 0.01, 200, 0.001


def runtime_weights_path() -> Path:
    return settings.data_dir / "model_weights.json"


def _clamp(p: float, lo: float = 0.01, hi: float = 0.99) -> float:
    return max(lo, min(hi, p))


def logit(p: float) -> float:
    p = _clamp(float(p))
    return math.log(p / (1.0 - p))


def sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def _days_to_close(close_time: Any, now: Optional[datetime] = None) -> float:
    if not close_time:
        return 30.0
    try:
        ct = datetime.fromisoformat(str(close_time).replace("Z", "+00:00"))
    except ValueError:
        return 30.0
    if ct.tzinfo is None:
        ct = ct.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return max(0.0, (ct - now).total_seconds() / 86400.0)


def build_features(
    market_prob: float,
    *,
    close_time: Any = None,
    volume: float = 0.0,
    spread_cents: float = 0.0,
    espn_p_yes: Optional[float] = None,
    now: Optional[datetime] = None,
) -> dict[str, float]:
    m = logit(market_prob)
    return {
        "market_edge": round(m, 6),
        "time_to_close": round(min(1.0, _days_to_close(close_time, now) / 30.0), 6),
        "volume_z": round((math.log1p(max(0.0, float(volume or 0))) - 6.0) / 3.0, 6),
        "espn_delta": round(logit(espn_p_yes) - m, 6) if espn_p_yes is not None else 0.0,
        "spread_z": round((float(spread_cents or 0) - 3.0) / 3.0, 6),
    }


def load_model(path: Optional[Path] = None) -> dict:
    for candidate in ([path] if path else [runtime_weights_path(), DEFAULT_WEIGHTS_PATH]):
        if candidate and Path(candidate).is_file():
            data = json.loads(Path(candidate).read_text(encoding="utf-8"))
            w = data.get("weights") or {}
            if all(k in w for k in WEIGHT_KEYS):
                return data
    return json.loads(DEFAULT_WEIGHTS_PATH.read_text(encoding="utf-8"))


def save_model(model: dict, path: Optional[Path] = None) -> Path:
    path = Path(path) if path else runtime_weights_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(model, indent=2) + "\n", encoding="utf-8")
    return path


def score(features: dict, weights: Optional[dict] = None) -> float:
    """Raw z."""
    w = weights or load_model()["weights"]
    z = float(w["w0"])
    for i, name in enumerate(FEATURES, start=1):
        z += float(w[f"w{i}"]) * float(features.get(name, 0.0))
    return z


def predict(features: dict, weights: Optional[dict] = None) -> float:
    return round(_clamp(sigmoid(score(features, weights))), 6)


def log_loss(samples: list[tuple[dict, int]], weights: dict) -> float:
    if not samples:
        return 0.0
    total = 0.0
    for x, y in samples:
        p = min(1 - 1e-9, max(1e-9, sigmoid(score(x, weights))))
        total -= y * math.log(p) + (1 - y) * math.log(1 - p)
    return total / len(samples)


def fit(
    samples: list[tuple[dict, int]],
    weights: dict,
    lr: float = LR,
    epochs: int = EPOCHS,
    l2: float = L2,
) -> dict:
    """Full-batch gradient descent on mean log-loss + L2 (bias not penalized)."""
    w = {k: float(weights[k]) for k in WEIGHT_KEYS}
    n = len(samples)
    if n == 0:
        return w
    for _ in range(epochs):
        grad = {k: 0.0 for k in WEIGHT_KEYS}
        for x, y in samples:
            err = sigmoid(score(x, w)) - y
            grad["w0"] += err
            for i, name in enumerate(FEATURES, start=1):
                grad[f"w{i}"] += err * float(x.get(name, 0.0))
        for k in WEIGHT_KEYS:
            g = grad[k] / n
            if k != "w0":
                g += l2 * w[k]
            w[k] -= lr * g
    return {k: round(v, 8) for k, v in w.items()}


def training_samples(ledger) -> list[tuple[dict, int]]:
    """Settled bets joined to the latest model run for that ticker at bet time.

    Label is the YES outcome: side yes & won -> 1; side no & won -> 0, etc.
    """
    samples = []
    for bet in ledger.bets_sync(settled=True):
        run = ledger.latest_model_run_sync(bet["ticker"], before_ts=bet["ts"]) or ledger.latest_model_run_sync(bet["ticker"])
        if not run:
            continue
        feats = {k: float(run["features"].get(k, 0.0) or 0.0) for k in FEATURES}
        yes_won = bet["outcome"] if bet["side"] == "yes" else 1 - bet["outcome"]
        samples.append((feats, int(yes_won)))
    return samples


def refit(ledger, store=None, now: Optional[datetime] = None, path: Optional[Path] = None) -> dict:
    """Refit on settled bets, persist, and log. Returns the refit record."""
    now = now or datetime.now(timezone.utc)
    model = load_model(path)
    samples = training_samples(ledger)
    before = log_loss(samples, model["weights"])
    if len(samples) < MIN_REFIT_SAMPLES:
        # Not enough data: log it, but leave last_refit alone so the next
        # settle tries again instead of waiting a week.
        record = {"ts": now.isoformat(), "n": len(samples), "skipped": "not enough settled bets"}
        log.info("model refit skipped: %s", record)
        if store is not None:
            store.log_activity("model", f"Refit skipped: {len(samples)} settled bets (< {MIN_REFIT_SAMPLES})", record)
        return record
    else:
        new_w = fit(samples, model["weights"])
        after = log_loss(samples, new_w)
        model["weights"] = new_w
        model["version"] = int(model.get("version") or 0) + 1
        record = {
            "ts": now.isoformat(),
            "n": len(samples),
            "log_loss_before": round(before, 6),
            "log_loss_after": round(after, 6),
            "weights": new_w,
            "version": model["version"],
        }
    model["last_refit"] = now.isoformat()
    model.setdefault("refit_log", []).append(record)
    model["refit_log"] = model["refit_log"][-52:]
    save_model(model, path)
    log.info("model refit: %s", record)
    if store is not None:
        store.log_activity("model", f"Refit on {record['n']} settled bets", record)
    return record


def refit_due(model: dict, now: Optional[datetime] = None) -> bool:
    last = model.get("last_refit")
    if not last:
        return True
    now = now or datetime.now(timezone.utc)
    try:
        return now - datetime.fromisoformat(last) >= REFIT_INTERVAL
    except ValueError:
        return True


def maybe_refit(ledger, store=None, now: Optional[datetime] = None, path: Optional[Path] = None) -> Optional[dict]:
    """Weekly refit: runs at most once per 7 days."""
    if not refit_due(load_model(path), now):
        return None
    return refit(ledger, store=store, now=now, path=path)

"""Train the logistic model on Kalshi's settled markets (public API, no keys).

Pipeline
--------
1. ``ingest_settled``: page ``GET /trade-api/v2/markets?status=settled`` one
   close-day at a time (``min_close_ts`` / ``max_close_ts``), keep binary
   markets with a YES/NO result, enough volume and a life of at least
   ``MIN_LIFETIME_H``. A settled market's own prices are post-settlement
   (last price ~1c/99c), so they would leak the answer. Instead each market's
   pre-close book is rebuilt from public hourly candlesticks
   (``/series/{s}/markets/{t}/candlesticks``) at ``SNAPSHOT_HORIZONS_H`` hours
   before close.
2. ``snapshot_features``: the same ``build_features`` the live scanner uses,
   with ``now`` = the snapshot time. ``espn_delta`` (and every later context
   term) is stored as null: the scoreboard at that time is not recoverable,
   and null contributes 0 to z.
3. ``train_and_gate``: rows = settled snapshots + real settled bets. Split
   chronologically by market close time (last ``HOLDOUT_FRAC`` of markets is
   the holdout; a market's snapshots never straddle the split). Fit on the
   train part (same GD as the weekly refit), score both weight sets on the
   holdout, and promote only if holdout Brier improves (``backtest.gate`` and
   strictly lower). Otherwise the current weights stay.
"""
from __future__ import annotations

import hashlib
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from app.db.ledger import brier_score
from app.strategy import model as M

log = logging.getLogger("kalshi.training")

SNAPSHOT_HORIZONS_H = (1, 6, 24)
CANDLE_PERIOD_MIN = 60
MIN_VOLUME = 50.0
MIN_LIFETIME_H = 2.0
DEFAULT_DAYS = 21
DEFAULT_PER_DAY = 20
MAX_PER_SERIES_PER_DAY = 3
DEFAULT_MAX_MARKETS = 400
HOLDOUT_FRAC = 0.2
MIN_TRAIN_MARKETS = 50  # markets (not snapshots) needed before a gated refit
MIN_HOLDOUT_MARKETS = 10
# Same lr / L2 as the weekly refit, more epochs: on 397 live settled markets
# (Nov 2026) 200 epochs left w1 at ~0.56 (holdout Brier 0.149, worse than the
# book's own 0.137) while 2000 reached ~0.93 and beat the book slightly.
SETTLED_EPOCHS = 2000


def _f(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ts(iso: Any) -> Optional[int]:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def series_of(market: dict) -> str:
    ev = market.get("event_ticker") or market.get("ticker") or ""
    return str(ev).split("-")[0]


def parse_settled_market(m: dict, min_volume: float = MIN_VOLUME, min_lifetime_h: float = MIN_LIFETIME_H) -> Optional[dict]:
    """Normalize one settled market, or None if it is not usable for training."""
    ticker = m.get("ticker") or ""
    if not ticker or "CROSSCATEGORY" in ticker.upper():
        return None
    if (m.get("market_type") or "binary") != "binary":
        return None
    result = str(m.get("result") or "").lower()
    if result not in {"yes", "no"}:
        return None
    close_ts, open_ts = _ts(m.get("close_time")), _ts(m.get("open_time"))
    if close_ts is None:
        return None
    if open_ts is not None and (close_ts - open_ts) < min_lifetime_h * 3600:
        return None
    volume = _f(m.get("volume_fp")) if m.get("volume_fp") is not None else _f(m.get("volume"))
    if (volume or 0.0) < min_volume:
        return None
    oi = _f(m.get("open_interest_fp")) if m.get("open_interest_fp") is not None else _f(m.get("open_interest"))
    return {
        "ticker": ticker,
        "event_ticker": m.get("event_ticker"),
        "series": series_of(m),
        "category": m.get("category"),
        "title": m.get("title"),
        "open_time": m.get("open_time"),
        "close_time": _iso(close_ts),
        "settlement_ts": m.get("settlement_ts"),
        "result": result,
        "settlement_value": _f(m.get("settlement_value_dollars")),
        "last_price": _f(m.get("last_price_dollars")),
        "volume": volume or 0.0,
        "open_interest": oi or 0.0,
    }


def _candle_price(c: dict, side: str) -> Optional[float]:
    blk = c.get(side) or {}
    v = _f(blk.get("close_dollars"))
    if v is None and blk.get("close") is not None:
        v = _f(blk.get("close"))
        if v is not None and v > 1:
            v /= 100.0
    return v


def snapshots_from_candles(market: dict, candles: list[dict], horizons=SNAPSHOT_HORIZONS_H) -> list[dict]:
    """Pre-close book at each horizon: the last hourly candle ending at least
    ``h`` hours before close (walking back at most 2 candles for a quoted
    book). Volume at the snapshot = final volume minus volume traded after it."""
    close_ts = _ts(market["close_time"])
    open_ts = _ts(market.get("open_time")) or 0
    cs = sorted((c for c in candles if c.get("end_period_ts")), key=lambda c: int(c["end_period_ts"]))
    final_vol = float(market.get("volume") or 0.0)
    out, used = [], set()
    for h in horizons:
        target = close_ts - int(h) * 3600
        idx = [i for i, c in enumerate(cs) if int(c["end_period_ts"]) <= target and int(c["end_period_ts"]) > open_ts]
        pick = None
        for i in reversed(idx[-3:]):
            ya, yb = _candle_price(cs[i], "yes_ask"), _candle_price(cs[i], "yes_bid")
            if ya is not None and yb is not None and 0 < ya < 1 and 0 <= yb < ya:
                pick = (i, ya, yb)
                break
        if pick is None:
            continue
        i, ya, yb = pick
        end = int(cs[i]["end_period_ts"])
        if end in used:
            continue
        used.add(end)
        after = sum(float(_f(c.get("volume_fp") if c.get("volume_fp") is not None else c.get("volume")) or 0.0) for c in cs[i + 1 :])
        oi = _f(cs[i].get("open_interest_fp") if cs[i].get("open_interest_fp") is not None else cs[i].get("open_interest"))
        snap = {
            "horizon_h": int(h),
            "snapshot_ts": _iso(end),
            "yes_ask": round(ya, 4),
            "yes_bid": round(yb, 4),
            "volume": max(0.0, final_vol - after),
            "open_interest": oi or 0.0,
        }
        snap["features"] = snapshot_features(market, snap)
        out.append(snap)
    return out


def snapshot_features(market: dict, snap: dict) -> dict:
    """Same feature schema as the live scanner's ``model_features``."""
    ya, yb = float(snap["yes_ask"]), float(snap["yes_bid"])
    no_ask = 1.0 - yb
    market_p = max(0.01, min(0.99, (ya + yb) / 2.0))  # = mid of YES ask and 1 - NO ask
    spread = (ya - yb) * 100.0
    now = datetime.fromisoformat(snap["snapshot_ts"])
    feats: dict[str, Any] = M.build_features(
        market_p, close_time=market["close_time"], volume=snap["volume"], spread_cents=spread, now=now,
        open_interest=snap.get("open_interest"),
    )
    for name in M.FEATURES:
        feats.setdefault(name, None)
    feats["espn_delta"] = None  # not recoverable historically; null -> 0 in z
    feats.update(
        {
            "market_p_yes": round(market_p, 6),
            "yes_ask": round(ya, 4),
            "yes_bid": round(yb, 4),
            "no_ask": round(no_ask, 4),
            "volume": snap["volume"],
            "open_interest": snap["open_interest"],
            "spread_cents": round(spread, 2),
            "close_time": market["close_time"],
            "prob_source": "settled_history",
        }
    )
    return feats


def _stable_key(ticker: str) -> str:
    return hashlib.sha1(ticker.encode()).hexdigest()


def select_markets(parsed: list[dict], per_day: int = DEFAULT_PER_DAY, per_series: int = MAX_PER_SERIES_PER_DAY) -> list[dict]:
    """Deterministic, diverse sample: hash order, at most ``per_series`` per series."""
    picked, by_series = [], defaultdict(int)
    for m in sorted(parsed, key=lambda m: _stable_key(m["ticker"])):
        if by_series[m["series"]] >= per_series:
            continue
        by_series[m["series"]] += 1
        picked.append(m)
        if len(picked) >= per_day:
            break
    return picked


def ingest_settled(
    client,
    ledger,
    *,
    days: int = DEFAULT_DAYS,
    per_day: int = DEFAULT_PER_DAY,
    max_markets: int = DEFAULT_MAX_MARKETS,
    min_volume: float = MIN_VOLUME,
    now: Optional[datetime] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> dict:
    """Pull settled markets day by day and store them with pre-close snapshots."""
    now_ts = int((now or datetime.now(timezone.utc)).timestamp())
    have = ledger.settled_tickers_sync()
    report = {"days": days, "listed": 0, "eligible": 0, "fetched": 0, "stored": 0, "no_snapshot": 0, "errors": [], "skipped_known": 0}
    categories: dict[str, Optional[str]] = {}
    for d in range(days):
        if report["fetched"] >= max_markets:
            break
        hi = now_ts - d * 86400
        lo = hi - 86400
        try:
            page = client.get_markets(status="settled", limit=1000, mve_filter="exclude", min_close_ts=lo, max_close_ts=hi)
        except Exception as exc:  # network / 4xx: report, keep going
            report["errors"].append(f"markets day -{d}: {exc}")
            continue
        raw = page.get("markets") or []
        report["listed"] += len(raw)
        parsed = [p for p in (parse_settled_market(m, min_volume=min_volume) for m in raw) if p]
        report["eligible"] += len(parsed)
        fresh = [p for p in parsed if p["ticker"] not in have]
        report["skipped_known"] += len(parsed) - len(fresh)
        for mk in select_markets(fresh, per_day=per_day):
            if report["fetched"] >= max_markets:
                break
            close_ts = _ts(mk["close_time"])
            start = max(_ts(mk.get("open_time")) or 0, close_ts - (max(SNAPSHOT_HORIZONS_H) + 2) * 3600)
            try:
                candles = (client.get_candlesticks(mk["series"], mk["ticker"], start, close_ts, CANDLE_PERIOD_MIN) or {}).get("candlesticks") or []
            except Exception as exc:
                report["errors"].append(f"candles {mk['ticker']}: {exc}")
                continue
            report["fetched"] += 1
            if mk["series"] not in categories:
                try:
                    categories[mk["series"]] = ((client.get_series(mk["series"]) or {}).get("series") or {}).get("category")
                except Exception:
                    categories[mk["series"]] = None
            mk["category"] = mk.get("category") or categories.get(mk["series"])
            snaps = snapshots_from_candles(mk, candles)
            if not snaps:
                report["no_snapshot"] += 1
                continue
            ledger.upsert_settled_sync(mk, snaps)
            have.add(mk["ticker"])
            report["stored"] += 1
        if progress:
            progress(f"day -{d}: stored {report['stored']} / fetched {report['fetched']}")
    report["errors"] = report["errors"][:20]
    report["totals"] = ledger.settled_counts_sync()
    ledger.set_meta_sync("last_ingest", _iso(now_ts))
    return report


# ----- training rows + chronological holdout -----


def _backfill(r: dict) -> dict:
    """Snapshots stored before a feature existed: rebuild what the snapshot
    columns allow (flow_z from volume / OI). Context terms stay null."""
    f = dict(r["features"])
    if "flow_z" not in f:
        f["flow_z"] = M.flow_z(r.get("snap_volume"), r.get("snap_oi"))
    for name in M.FEATURES:
        f.setdefault(name, None)
    return f


def training_rows(ledger, include_bets: bool = True) -> list[dict]:
    rows = [
        {
            "ticker": r["ticker"],
            "close_time": r["close_time"],
            "features": _backfill(r),
            "yes_outcome": 1 if r["result"] == "yes" else 0,
            "source": "settled_market",
            "horizon_h": r["horizon_h"],
        }
        for r in ledger.settled_dataset_sync()
    ]
    if include_bets:
        import backtest as bt

        bet_ts = {}
        for b in ledger.bets_sync(settled=True):
            bet_ts.setdefault(b["ticker"], b["ts"])
        for r in bt.load_ledger_dataset(ledger):
            rows.append(
                {
                    "ticker": r["ticker"],
                    "close_time": bet_ts.get(r["ticker"]) or "",
                    "features": r["features"],
                    "yes_outcome": int(r["yes_outcome"]),
                    "source": "settled_bet",
                    "horizon_h": None,
                }
            )
    return rows


def chronological_split(rows: list[dict], holdout_frac: float = HOLDOUT_FRAC) -> tuple[list[dict], list[dict]]:
    """Last ``holdout_frac`` of *markets* by close time is the holdout. All
    snapshots of one market stay on the same side. No shuffling."""
    first_close: dict[str, str] = {}
    for r in rows:
        key = r["ticker"]
        ct = str(r.get("close_time") or "")
        if key not in first_close or ct > first_close[key]:
            first_close[key] = ct
    order = sorted(first_close, key=lambda t: (first_close[t], t))
    n_hold = int(round(len(order) * holdout_frac))
    hold = set(order[len(order) - n_hold :]) if n_hold else set()
    train = [r for r in rows if r["ticker"] not in hold]
    holdout = [r for r in rows if r["ticker"] in hold]
    return train, holdout


def _samples(rows: list[dict]) -> list[tuple[dict, int]]:
    return [({k: M.feature_value(r["features"], k) for k in M.FEATURES}, int(r["yes_outcome"])) for r in rows]


def train_and_gate(
    ledger,
    *,
    path: Optional[Path] = None,
    store=None,
    epochs: int = SETTLED_EPOCHS,
    holdout_frac: float = HOLDOUT_FRAC,
    promote: bool = True,
    now: Optional[datetime] = None,
    rows: Optional[list[dict]] = None,
) -> dict:
    """Fit on the chronological train split; promote only if holdout Brier improves."""
    import backtest as bt

    now = now or datetime.now(timezone.utc)
    doc = M.load_model(path)
    current = {k: float(doc["weights"].get(k, M.START_WEIGHT)) for k in M.WEIGHT_KEYS}
    rows = training_rows(ledger) if rows is None else rows
    train, holdout = chronological_split(rows, holdout_frac)
    n_markets = len({r["ticker"] for r in rows})
    n_hold_m = len({r["ticker"] for r in holdout})
    record: dict[str, Any] = {
        "ts": now.isoformat(),
        "kind": "settled_train",
        "n_rows": len(rows),
        "n_markets": n_markets,
        "n_train": len(train),
        "n_holdout": len(holdout),
        "n_holdout_markets": n_hold_m,
        "sources": {s: sum(1 for r in rows if r["source"] == s) for s in ("settled_market", "settled_bet")},
        "holdout_from": min((str(r["close_time"]) for r in holdout), default=None),
        "epochs": epochs,
        "promoted": False,
    }
    if n_markets < MIN_TRAIN_MARKETS or n_hold_m < MIN_HOLDOUT_MARKETS:
        record["reason"] = (
            f"Not enough settled history ({n_markets} markets, {n_hold_m} in holdout; need "
            f"{MIN_TRAIN_MARKETS} / {MIN_HOLDOUT_MARKETS}). Kept current weights."
        )
    else:
        proposed = M.fit(_samples(train), current, epochs=epochs)
        cur_tr, new_tr = bt.run_backtest(train, current), bt.run_backtest(train, proposed)
        cur_ho, new_ho = bt.run_backtest(holdout, current), bt.run_backtest(holdout, proposed)
        passed = bt.gate(new_ho["brier"], cur_ho["brier"]) and new_ho["brier"] < cur_ho["brier"]
        record.update(
            {
                "train_brier_current": cur_tr["brier"],
                "train_brier_proposed": new_tr["brier"],
                "holdout_brier_current": cur_ho["brier"],
                "holdout_brier_proposed": new_ho["brier"],
                # Reference only: the book's own mid as the forecast.
                "holdout_brier_market": brier_score(
                    [(float(r["features"].get("market_p_yes") or 0.5), int(r["yes_outcome"])) for r in holdout]
                ),
                "holdout_hit_current": cur_ho["hit_rate"],
                "holdout_hit_proposed": new_ho["hit_rate"],
                "holdout_pnl_current": cur_ho["pnl"]["total"],
                "holdout_pnl_proposed": new_ho["pnl"]["total"],
                "gate_pass": bool(passed),
                "weights_current": current,
                "weights_proposed": proposed,
            }
        )
        if passed and promote:
            doc["weights"] = proposed
            doc["version"] = int(doc.get("version") or 0) + 1
            doc["last_refit"] = now.isoformat()
            record["promoted"] = True
            record["version"] = doc["version"]
            record["reason"] = "Holdout Brier improved; promoted."
        elif passed:
            record["reason"] = "Holdout Brier improved, but promote was off."
        else:
            record["reason"] = "Holdout Brier did not improve. Kept current weights."
    if promote:
        doc["last_training"] = record
        if record["promoted"]:
            doc.setdefault("refit_log", []).append({k: v for k, v in record.items() if k != "weights_current"})
            doc["refit_log"] = doc["refit_log"][-52:]
        M.save_model(doc, path)
    log.info("settled training: %s", {k: record.get(k) for k in ("n_markets", "holdout_brier_current", "holdout_brier_proposed", "promoted")})
    if store is not None:
        store.log_activity("model", f"Settled-market training: {record['reason']}", record)
    return record


def training_status(ledger, path: Optional[Path] = None, calibration: Optional[dict] = None) -> dict:
    doc = M.load_model(path)
    calib = calibration if calibration is not None else ledger.calibration_sync()
    return {
        "settled": ledger.settled_counts_sync(),
        "last_ingest": ledger.get_meta_sync("last_ingest"),
        "model_runs": ledger.count_model_runs_sync(),
        "model_version": doc.get("version", 0),
        "weights": doc["weights"],
        "last_refit": doc.get("last_refit"),
        "last_training": doc.get("last_training"),
        "calibration": {k: calib.get(k) for k in ("brier", "n", "uncalibrated", "threshold", "min_n")},
        "gems_verified": not bool(calib.get("uncalibrated", True)),
        "note": (
            "Gems stay ◆ GEM? (unverified) until settled bets pass calibration "
            "(Brier ≤ threshold on enough bets). Holdout Brier on settled markets "
            "gates weight changes; it does not verify gems."
        ),
    }

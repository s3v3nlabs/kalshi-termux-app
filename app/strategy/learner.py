"""Continuous learning: a background job that keeps the model current.

Every ``LEARN_INTERVAL_H`` hours (default 6) it:

1. ingests newly settled Kalshi markets (``training.ingest_settled``; known
   tickers are skipped, so each run only adds what rolled since the last one),
2. settles the app's own open bets from Kalshi results (manual and auto),
3. refits the logistic model on settled markets + settled bets and promotes
   the new weights ONLY through the existing chronological-holdout gate
   (``training.train_and_gate``): if holdout Brier does not improve, the
   current weights stay. That is what makes it learn rather than drift,
4. refreshes research knowledge: drops expired briefs and briefs for markets
   that have settled, and rebuilds briefs for markets the app still holds.

The last run is saved in ledger meta ``last_learn`` and surfaced on the
Calibration screen. Set ``LEARN_ENABLED=0`` to stop the schedule.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from app.strategy import model as M
from app.strategy import training as T

log = logging.getLogger(__name__)

META_KEY = "last_learn"
RESEARCH_PREWARM_MAX = 10


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _parse(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def ingest_days_since(last_ingest: Optional[str], now: datetime, default: int = T.DEFAULT_DAYS) -> int:
    """Days to look back: since the last ingest (+1 overlap), first run = default."""
    last = _parse(last_ingest)
    if last is None:
        return default
    return max(1, min(default, int(math.ceil((now - last).total_seconds() / 86400.0)) + 1))


class Learner:
    def __init__(
        self,
        *,
        ledger,
        store=None,
        client_fn: Callable[[], Any],
        interval_h: Optional[float] = None,
        weights_path: Optional[Path] = None,
        ingest_fn: Optional[Callable[..., dict]] = None,
        settle_fn: Optional[Callable[..., list]] = None,
        train_fn: Optional[Callable[..., dict]] = None,
        research_fn: Optional[Callable[..., dict]] = None,
        per_day: int = T.DEFAULT_PER_DAY,
        max_markets: int = T.DEFAULT_MAX_MARKETS,
    ):
        from app.db.ledger import settle_open_bets

        self.ledger = ledger
        self.store = store
        self.client_fn = client_fn
        self.interval_h = float(interval_h if interval_h is not None else _env_float("LEARN_INTERVAL_H", 6.0))
        self.enabled = (os.getenv("LEARN_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"})
        self.weights_path = weights_path
        self.ingest_fn = ingest_fn or T.ingest_settled
        self.settle_fn = settle_fn or settle_open_bets
        self.train_fn = train_fn or (lambda led, **kw: T.train_and_gate(led, **kw))
        self.research_fn = research_fn
        self.per_day = per_day
        self.max_markets = max_markets
        self._lock = threading.Lock()
        self.running = False

    # ----- state -----

    def last_run(self) -> Optional[dict]:
        raw = self.ledger.get_meta_sync(META_KEY)
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return None

    def due(self, now: Optional[datetime] = None) -> bool:
        last = _parse((self.last_run() or {}).get("ts"))
        now = now or datetime.now(timezone.utc)
        return last is None or (now - last).total_seconds() >= self.interval_h * 3600

    def seconds_until_due(self, now: Optional[datetime] = None) -> float:
        last = _parse((self.last_run() or {}).get("ts"))
        if last is None:
            return 0.0
        now = now or datetime.now(timezone.utc)
        return max(0.0, self.interval_h * 3600 - (now - last).total_seconds())

    def status(self) -> dict:
        doc = M.load_model(self.weights_path)
        last = self.last_run()
        nxt = None
        if last and _parse(last.get("ts")):
            nxt = datetime.fromtimestamp(_parse(last["ts"]).timestamp() + self.interval_h * 3600, timezone.utc).isoformat()
        lt = doc.get("last_training") or {}
        return {
            "enabled": self.enabled,
            "running": self.running,
            "interval_h": self.interval_h,
            "last_run": last,
            "next_run": nxt,
            "model_version": doc.get("version", 0),
            "last_refit": doc.get("last_refit"),
            "last_training_ts": lt.get("ts"),
            "holdout_brier": lt.get("holdout_brier_proposed") if lt.get("promoted") else lt.get("holdout_brier_current"),
            "gate_pass": lt.get("gate_pass"),
            "promoted": lt.get("promoted"),
        }

    # ----- one learning pass -----

    def run_once(self, now: Optional[datetime] = None) -> dict:
        if not self._lock.acquire(blocking=False):
            return {"ran": False, "reason": "already running"}
        self.running = True
        now = now or datetime.now(timezone.utc)
        rec: dict[str, Any] = {"ts": now.isoformat(), "ran": True, "errors": []}
        try:
            client = self.client_fn()
            days = ingest_days_since(self.ledger.get_meta_sync("last_ingest"), now)
            rec["ingest_days"] = days
            try:
                rep = self.ingest_fn(client, self.ledger, days=days, per_day=self.per_day, max_markets=self.max_markets, now=now)
                rec["ingest"] = {k: rep.get(k) for k in ("listed", "eligible", "fetched", "stored", "skipped_known")}
                rec["errors"] += list(rep.get("errors") or [])[:5]
            except Exception as exc:
                rec["errors"].append(f"ingest: {exc}")
            try:
                settled = self.settle_fn(self.ledger, client) or []
                rec["bets_settled"] = len(settled)
            except Exception as exc:
                rec["errors"].append(f"settle bets: {exc}")
            try:
                tr = self.train_fn(self.ledger, store=self.store, path=self.weights_path)
                rec["train"] = {
                    k: tr.get(k)
                    for k in ("n_markets", "holdout_brier_current", "holdout_brier_proposed", "gate_pass", "promoted", "version", "reason")
                }
            except Exception as exc:
                rec["errors"].append(f"train: {exc}")
            rec["research"] = self._refresh_research(client)
        finally:
            rec["model_version"] = M.load_model(self.weights_path).get("version", 0)
            try:
                self.ledger.set_meta_sync(META_KEY, json.dumps(rec, default=str))
            except Exception as exc:
                log.warning("could not save learn record: %s", exc)
            if self.store is not None:
                tr = rec.get("train") or {}
                self.store.log_activity(
                    "model",
                    f"Continuous learning: +{(rec.get('ingest') or {}).get('stored', 0)} settled markets, "
                    f"{rec.get('bets_settled', 0)} bets settled, {tr.get('reason') or 'no refit'}",
                    rec,
                )
            self.running = False
            self._lock.release()
        return rec

    def _refresh_research(self, client) -> dict:
        out: dict[str, Any] = {"purged": 0, "rebuilt": 0}
        try:
            out["purged"] = self.ledger.purge_research_sync(time.time(), self.ledger.settled_tickers_sync())
        except Exception as exc:
            out["error"] = str(exc)
            return out
        open_tickers = []
        try:
            for b in self.ledger.bets_sync(settled=False):
                if b["ticker"] not in open_tickers:
                    open_tickers.append(b["ticker"])
        except Exception:
            pass
        fn = self.research_fn
        if fn is None:
            from app.strategy.research import get_research as fn  # noqa: N813
        for t in open_tickers[:RESEARCH_PREWARM_MAX]:
            try:
                fn(t, client=client, ledger=self.ledger, refresh=True)
                out["rebuilt"] += 1
            except Exception:
                continue
        return out

    # ----- background loop -----

    async def run_forever(self, startup_delay_s: float = 60.0) -> None:
        await asyncio.sleep(startup_delay_s)  # let the server come up first
        while True:
            if self.enabled and self.due():
                try:
                    await asyncio.to_thread(self.run_once)
                except Exception:  # pragma: no cover - run_once catches
                    log.exception("continuous learning")
            await asyncio.sleep(max(60.0, min(3600.0, self.seconds_until_due() or 60.0)))

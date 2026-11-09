"""Prediction ledger on SQLite via aiosqlite.

Tables (spec item 1):
  bets(id, ticker, side, price, stake, model_prob, market_prob, outcome, pnl, ts)
  model_runs(id, ticker, model_prob, features_json, ts)
  calibration(bucket, predicted, actual, n)

Conventions:
  * bets.price / market_prob = price paid per contract for the chosen side (0-1).
  * bets.model_prob = model probability that the chosen side wins.
  * bets.outcome = 1 if the chosen side won, 0 if it lost, NULL while open.
  * bets.pnl = stake-relative dollars: win -> contracts*(1-price), lose -> -stake.
  * model_runs.model_prob = model P(YES).

The app's HTTP handlers are sync (FastAPI threadpool), so each async method
has a ``*_sync`` twin via :func:`run_sync`.
"""
from __future__ import annotations

import asyncio
import json
import math
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import aiosqlite

from app.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS bets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    stake REAL NOT NULL,
    model_prob REAL,
    market_prob REAL,
    outcome INTEGER,
    pnl REAL,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    model_prob REAL NOT NULL,
    features_json TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_model_runs_ticker_ts ON model_runs(ticker, ts);
CREATE TABLE IF NOT EXISTS calibration (
    bucket TEXT PRIMARY KEY,
    predicted REAL NOT NULL,
    actual REAL NOT NULL,
    n INTEGER NOT NULL
);
"""

BUCKETS = [f"{i * 10}-{(i + 1) * 10}" for i in range(10)]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def bucket_of(p: float) -> str:
    return BUCKETS[min(9, max(0, int(math.floor(float(p) * 10))))]


def run_sync(coro):
    """Run a coroutine from sync code, even if a loop is already running."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    box: dict[str, Any] = {}

    def _target():
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:  # pragma: no cover - re-raised below
            box["error"] = exc

    t = threading.Thread(target=_target)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


class Ledger:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else settings.db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ready = False

    async def _db(self) -> aiosqlite.Connection:
        conn = await aiosqlite.connect(str(self.path))
        conn.row_factory = aiosqlite.Row
        if not self._ready:
            await conn.executescript(SCHEMA)
            await conn.commit()
            self._ready = True
        return conn

    # ----- model runs -----

    async def add_model_runs(self, rows: Iterable[dict]) -> int:
        """rows: {ticker, model_prob, features}. Returns rows written."""
        ts = _now()
        values = [
            (str(r["ticker"]), float(r["model_prob"]), json.dumps(r.get("features") or {}, sort_keys=True), r.get("ts") or ts)
            for r in rows
            if r.get("ticker") and r.get("model_prob") is not None
        ]
        if not values:
            return 0
        conn = await self._db()
        try:
            await conn.executemany(
                "INSERT INTO model_runs(ticker, model_prob, features_json, ts) VALUES(?,?,?,?)", values
            )
            await conn.commit()
        finally:
            await conn.close()
        return len(values)

    async def latest_model_run(self, ticker: str, before_ts: Optional[str] = None) -> Optional[dict]:
        conn = await self._db()
        try:
            if before_ts:
                cur = await conn.execute(
                    "SELECT * FROM model_runs WHERE ticker=? AND ts<=? ORDER BY ts DESC, id DESC LIMIT 1",
                    (ticker, before_ts),
                )
            else:
                cur = await conn.execute(
                    "SELECT * FROM model_runs WHERE ticker=? ORDER BY ts DESC, id DESC LIMIT 1", (ticker,)
                )
            row = await cur.fetchone()
        finally:
            await conn.close()
        return _run_row(row) if row else None

    async def model_runs(self) -> list[dict]:
        conn = await self._db()
        try:
            cur = await conn.execute("SELECT * FROM model_runs ORDER BY ts, id")
            rows = await cur.fetchall()
        finally:
            await conn.close()
        return [_run_row(r) for r in rows]

    # ----- bets -----

    async def add_bet(
        self,
        *,
        ticker: str,
        side: str,
        price: float,
        stake: float,
        model_prob: Optional[float],
        market_prob: Optional[float],
        ts: Optional[str] = None,
    ) -> int:
        side = side.lower()
        if side not in {"yes", "no"}:
            raise ValueError("side must be yes or no")
        if not 0 < float(price) < 1:
            raise ValueError("price must be between 0 and 1")
        conn = await self._db()
        try:
            cur = await conn.execute(
                "INSERT INTO bets(ticker, side, price, stake, model_prob, market_prob, outcome, pnl, ts) "
                "VALUES(?,?,?,?,?,?,NULL,NULL,?)",
                (ticker, side, float(price), float(stake), model_prob, market_prob, ts or _now()),
            )
            await conn.commit()
            return int(cur.lastrowid)
        finally:
            await conn.close()

    async def get_bet(self, bet_id: int) -> Optional[dict]:
        conn = await self._db()
        try:
            cur = await conn.execute("SELECT * FROM bets WHERE id=?", (bet_id,))
            row = await cur.fetchone()
        finally:
            await conn.close()
        return dict(row) if row else None

    async def bets(self, settled: Optional[bool] = None) -> list[dict]:
        sql = "SELECT * FROM bets"
        if settled is True:
            sql += " WHERE outcome IS NOT NULL"
        elif settled is False:
            sql += " WHERE outcome IS NULL"
        conn = await self._db()
        try:
            cur = await conn.execute(sql + " ORDER BY ts, id")
            rows = await cur.fetchall()
        finally:
            await conn.close()
        return [dict(r) for r in rows]

    async def settle_bet(self, bet_id: int, winning_side: str) -> dict:
        """Record the market result ('yes'/'no') for a bet, then recalibrate."""
        winning_side = winning_side.lower()
        if winning_side not in {"yes", "no"}:
            raise ValueError("winning_side must be yes or no")
        bet = await self.get_bet(bet_id)
        if bet is None:
            raise KeyError(f"bet {bet_id} not found")
        if bet["outcome"] is not None:
            return {"bet": bet, "calibration": await self.calibration(), "already_settled": True}
        outcome = 1 if bet["side"] == winning_side else 0
        contracts = bet["stake"] / bet["price"] if bet["price"] else 0.0
        pnl = round(contracts * (1.0 - bet["price"]), 6) if outcome else round(-bet["stake"], 6)
        conn = await self._db()
        try:
            await conn.execute("UPDATE bets SET outcome=?, pnl=? WHERE id=?", (outcome, pnl, bet_id))
            await conn.commit()
        finally:
            await conn.close()
        calib = await self.recompute_calibration()
        return {"bet": await self.get_bet(bet_id), "calibration": calib, "already_settled": False}

    # ----- calibration (item 2) -----

    async def recompute_calibration(self) -> dict:
        settled = [b for b in await self.bets(settled=True) if b["model_prob"] is not None]
        stats: dict[str, list[float]] = {b: [0.0, 0.0, 0] for b in BUCKETS}  # type: ignore[misc]
        for b in settled:
            s = stats[bucket_of(b["model_prob"])]
            s[0] += b["model_prob"]
            s[1] += b["outcome"]
            s[2] += 1
        conn = await self._db()
        try:
            await conn.execute("DELETE FROM calibration")
            await conn.executemany(
                "INSERT INTO calibration(bucket, predicted, actual, n) VALUES(?,?,?,?)",
                [
                    (bucket, s[0] / s[2], s[1] / s[2], int(s[2]))
                    for bucket, s in stats.items()
                    if s[2]
                ],
            )
            await conn.commit()
        finally:
            await conn.close()
        return await self.calibration()

    async def calibration(self) -> dict:
        conn = await self._db()
        try:
            cur = await conn.execute("SELECT bucket, predicted, actual, n FROM calibration")
            rows = {r["bucket"]: dict(r) for r in await cur.fetchall()}
        finally:
            await conn.close()
        settled = [b for b in await self.bets(settled=True) if b["model_prob"] is not None]
        brier = brier_score([(b["model_prob"], b["outcome"]) for b in settled])
        buckets = [
            rows.get(b, {"bucket": b, "predicted": None, "actual": None, "n": 0}) for b in BUCKETS
        ]
        return {
            "buckets": buckets,
            "brier": brier,
            "n": len(settled),
            "uncalibrated": is_uncalibrated(brier),
            "threshold": BRIER_THRESHOLD,
        }

    # ----- sync twins for the sync FastAPI handlers -----

    def add_model_runs_sync(self, rows):
        return run_sync(self.add_model_runs(list(rows)))

    def latest_model_run_sync(self, ticker, before_ts=None):
        return run_sync(self.latest_model_run(ticker, before_ts))

    def add_bet_sync(self, **kw):
        return run_sync(self.add_bet(**kw))

    def bets_sync(self, settled=None):
        return run_sync(self.bets(settled))

    def settle_bet_sync(self, bet_id, winning_side):
        return run_sync(self.settle_bet(bet_id, winning_side))

    def calibration_sync(self):
        return run_sync(self.calibration())

    def model_runs_sync(self):
        return run_sync(self.model_runs())


BRIER_THRESHOLD = 0.25


def brier_score(pairs: list[tuple[float, int]]) -> Optional[float]:
    if not pairs:
        return None
    return round(sum((p - y) ** 2 for p, y in pairs) / len(pairs), 6)


def is_uncalibrated(brier: Optional[float]) -> bool:
    """No settled history counts as uncalibrated (fail safe: half Kelly)."""
    return brier is None or brier > BRIER_THRESHOLD


def _run_row(row) -> dict:
    d = dict(row)
    d["features"] = json.loads(d.pop("features_json") or "{}")
    return d


def settle_open_bets(ledger: Ledger, client) -> list[dict]:
    """Settle open bets whose markets Kalshi reports as settled with a result."""
    done = []
    for bet in ledger.bets_sync(settled=False):
        try:
            market = (client.get_market(bet["ticker"]) or {}).get("market") or {}
        except Exception:
            continue
        result = str(market.get("result") or "").lower()
        status = str(market.get("status") or "").lower()
        if result in {"yes", "no"} and status in {"settled", "finalized", "determined", "closed"}:
            done.append(ledger.settle_bet_sync(bet["id"], result))
    return done

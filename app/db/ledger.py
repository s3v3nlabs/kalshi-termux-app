"""Prediction ledger on SQLite via aiosqlite.

Tables (spec item 1):
  bets(id, ticker, side, price, stake, model_prob, market_prob, outcome, pnl, ts)
  model_runs(id, ticker, model_prob, features_json, ts)
  calibration(bucket, predicted, actual, n)
  settled_markets(ticker, ..., result, close_time, ...)    -- public Kalshi history
  settled_snapshots(ticker, horizon_h, snapshot_ts, features_json, ...)
  research_cache(ticker, brief_json, created_at, expires_at)  -- on-demand research briefs (TTL)

model_runs are training snapshots, not a poll log: the scanner keeps at most
one row per ticker per hour (``add_model_snapshots``) and a bet forces one.

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
from datetime import datetime, timedelta, timezone
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
CREATE TABLE IF NOT EXISTS settled_markets (
    ticker TEXT PRIMARY KEY,
    event_ticker TEXT,
    series TEXT,
    category TEXT,
    title TEXT,
    open_time TEXT,
    close_time TEXT NOT NULL,
    settlement_ts TEXT,
    result TEXT NOT NULL,
    settlement_value REAL,
    last_price REAL,
    volume REAL,
    open_interest REAL,
    ingested_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_settled_close ON settled_markets(close_time);
CREATE TABLE IF NOT EXISTS settled_snapshots (
    ticker TEXT NOT NULL,
    horizon_h INTEGER NOT NULL,
    snapshot_ts TEXT NOT NULL,
    yes_bid REAL,
    yes_ask REAL,
    volume REAL,
    open_interest REAL,
    features_json TEXT NOT NULL,
    PRIMARY KEY (ticker, horizon_h)
);
CREATE TABLE IF NOT EXISTS ledger_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS research_cache (
    ticker TEXT PRIMARY KEY,
    brief_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS calibration (
    bucket TEXT PRIMARY KEY,
    predicted REAL NOT NULL,
    actual REAL NOT NULL,
    n INTEGER NOT NULL
);
"""

BUCKETS = [f"{i * 10}-{(i + 1) * 10}" for i in range(10)]
# Scanner snapshots: at most one model_runs row per ticker per this many seconds.
SNAPSHOT_INTERVAL_S = 3600


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
            await self._migrate(conn)
            await conn.commit()
            self._ready = True
        return conn

    async def _migrate(self, conn: aiosqlite.Connection) -> None:
        """model_runs.kind ('scan' | 'bet'), then a one-time compaction of the old
        per-poll rows down to the last row per ticker per hour (keeps the
        "latest run before a bet" lookup working)."""
        cur = await conn.execute("PRAGMA table_info(model_runs)")
        cols = {r[1] for r in await cur.fetchall()}
        if "kind" not in cols:
            await conn.execute("ALTER TABLE model_runs ADD COLUMN kind TEXT DEFAULT 'scan'")
        cur = await conn.execute("SELECT value FROM ledger_meta WHERE key='model_runs_compacted_v1'")
        if await cur.fetchone() is None:
            await conn.execute(COMPACT_SQL)
            await conn.execute(
                "INSERT OR REPLACE INTO ledger_meta(key, value) VALUES('model_runs_compacted_v1', ?)", (_now(),)
            )

    # ----- model runs -----

    async def add_model_runs(self, rows: Iterable[dict]) -> int:
        """rows: {ticker, model_prob, features}. Returns rows written."""
        ts = _now()
        values = [
            (
                str(r["ticker"]),
                float(r["model_prob"]),
                json.dumps(r.get("features") or {}, sort_keys=True),
                r.get("ts") or ts,
                r.get("kind") or "scan",
            )
            for r in rows
            if r.get("ticker") and r.get("model_prob") is not None
        ]
        if not values:
            return 0
        conn = await self._db()
        try:
            await conn.executemany(
                "INSERT INTO model_runs(ticker, model_prob, features_json, ts, kind) VALUES(?,?,?,?,?)", values
            )
            await conn.commit()
        finally:
            await conn.close()
        return len(values)

    async def add_model_snapshots(
        self, rows: Iterable[dict], min_interval_s: int = SNAPSHOT_INTERVAL_S, now: Optional[datetime] = None
    ) -> int:
        """Throttled scanner write: skip a ticker that already has a run in the
        last ``min_interval_s`` seconds. Returns rows written."""
        rows = [r for r in rows if r.get("ticker") and r.get("model_prob") is not None]
        if not rows:
            return 0
        now = now or datetime.now(timezone.utc)
        cutoff = (now - timedelta(seconds=min_interval_s)).isoformat()
        conn = await self._db()
        try:
            recent: set[str] = set()
            tickers = sorted({str(r["ticker"]) for r in rows})
            for i in range(0, len(tickers), 500):
                chunk = tickers[i : i + 500]
                cur = await conn.execute(
                    f"SELECT DISTINCT ticker FROM model_runs WHERE ts > ? AND ticker IN ({','.join('?' * len(chunk))})",
                    (cutoff, *chunk),
                )
                recent.update(r[0] for r in await cur.fetchall())
        finally:
            await conn.close()
        seen: set[str] = set()
        keep = []
        for r in rows:
            t = str(r["ticker"])
            if t in recent or t in seen:
                continue
            seen.add(t)
            keep.append({**r, "ts": r.get("ts") or now.isoformat(), "kind": r.get("kind") or "scan"})
        return await self.add_model_runs(keep)

    async def count_model_runs(self) -> int:
        conn = await self._db()
        try:
            cur = await conn.execute("SELECT COUNT(*) FROM model_runs")
            return int((await cur.fetchone())[0])
        finally:
            await conn.close()

    async def compact_model_runs(self) -> int:
        """Keep the last model run per ticker per hour. Returns rows deleted."""
        conn = await self._db()
        try:
            before = (await (await conn.execute("SELECT COUNT(*) FROM model_runs")).fetchone())[0]
            await conn.execute(COMPACT_SQL)
            await conn.commit()
            after = (await (await conn.execute("SELECT COUNT(*) FROM model_runs")).fetchone())[0]
        finally:
            await conn.close()
        return int(before - after)

    # ----- settled market history (training) -----

    async def upsert_settled(self, market: dict, snapshots: Iterable[dict]) -> None:
        conn = await self._db()
        try:
            await conn.execute(
                "INSERT OR REPLACE INTO settled_markets(ticker, event_ticker, series, category, title, open_time, "
                "close_time, settlement_ts, result, settlement_value, last_price, volume, open_interest, ingested_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    market["ticker"], market.get("event_ticker"), market.get("series"), market.get("category"),
                    market.get("title"), market.get("open_time"), market["close_time"], market.get("settlement_ts"),
                    market["result"], market.get("settlement_value"), market.get("last_price"),
                    market.get("volume"), market.get("open_interest"), _now(),
                ),
            )
            await conn.execute("DELETE FROM settled_snapshots WHERE ticker=?", (market["ticker"],))
            await conn.executemany(
                "INSERT INTO settled_snapshots(ticker, horizon_h, snapshot_ts, yes_bid, yes_ask, volume, open_interest, features_json) "
                "VALUES(?,?,?,?,?,?,?,?)",
                [
                    (
                        market["ticker"], int(s["horizon_h"]), s["snapshot_ts"], s.get("yes_bid"), s.get("yes_ask"),
                        s.get("volume"), s.get("open_interest"), json.dumps(s.get("features") or {}, sort_keys=True),
                    )
                    for s in snapshots
                ],
            )
            await conn.commit()
        finally:
            await conn.close()

    async def settled_tickers(self) -> set[str]:
        conn = await self._db()
        try:
            cur = await conn.execute("SELECT ticker FROM settled_markets")
            return {r[0] for r in await cur.fetchall()}
        finally:
            await conn.close()

    async def settled_dataset(self, horizon_h: Optional[int] = None) -> list[dict]:
        """Settled markets joined to their pre-close snapshots, oldest close first."""
        sql = (
            "SELECT m.*, s.horizon_h, s.snapshot_ts, s.yes_bid, s.yes_ask, s.volume AS snap_volume, "
            "s.open_interest AS snap_oi, s.features_json FROM settled_markets m "
            "JOIN settled_snapshots s ON s.ticker = m.ticker"
        )
        args: tuple = ()
        if horizon_h is not None:
            sql += " WHERE s.horizon_h = ?"
            args = (int(horizon_h),)
        conn = await self._db()
        try:
            cur = await conn.execute(sql + " ORDER BY m.close_time, m.ticker, s.horizon_h", args)
            rows = await cur.fetchall()
        finally:
            await conn.close()
        out = []
        for r in rows:
            d = dict(r)
            d["features"] = json.loads(d.pop("features_json") or "{}")
            out.append(d)
        return out

    async def settled_counts(self) -> dict:
        conn = await self._db()
        try:
            m = (await (await conn.execute("SELECT COUNT(*), MIN(close_time), MAX(close_time) FROM settled_markets")).fetchone())
            s = (await (await conn.execute("SELECT COUNT(*) FROM settled_snapshots")).fetchone())[0]
            y = (await (await conn.execute("SELECT COUNT(*) FROM settled_markets WHERE result='yes'")).fetchone())[0]
        finally:
            await conn.close()
        return {"markets": int(m[0]), "snapshots": int(s), "yes": int(y), "first_close": m[1], "last_close": m[2]}

    async def get_meta(self, key: str) -> Optional[str]:
        conn = await self._db()
        try:
            row = await (await conn.execute("SELECT value FROM ledger_meta WHERE key=?", (key,))).fetchone()
        finally:
            await conn.close()
        return row[0] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        conn = await self._db()
        try:
            await conn.execute("INSERT OR REPLACE INTO ledger_meta(key, value) VALUES(?,?)", (key, value))
            await conn.commit()
        finally:
            await conn.close()

    # ----- research briefs (on-demand cache; see app/strategy/research.py) -----

    async def get_research(self, ticker: str, now_epoch: float) -> Optional[dict]:
        conn = await self._db()
        try:
            row = await (
                await conn.execute(
                    "SELECT brief_json, created_at, expires_at FROM research_cache WHERE ticker=? AND expires_at > ?",
                    (ticker, float(now_epoch)),
                )
            ).fetchone()
        finally:
            await conn.close()
        if row is None:
            return None
        try:
            return {"brief": json.loads(row[0]), "created_at": row[1], "expires_at": float(row[2])}
        except ValueError:
            return None

    async def put_research(self, ticker: str, brief: dict, created_at: str, expires_at: float) -> None:
        conn = await self._db()
        try:
            await conn.execute(
                "INSERT OR REPLACE INTO research_cache(ticker, brief_json, created_at, expires_at) VALUES(?,?,?,?)",
                (ticker, json.dumps(brief, default=str), created_at, float(expires_at)),
            )
            await conn.execute("DELETE FROM research_cache WHERE expires_at < ?", (float(expires_at) - 86400.0,))
            await conn.commit()
        finally:
            await conn.close()

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
            "uncalibrated": is_uncalibrated(brier, len(settled)),
            "threshold": BRIER_THRESHOLD,
            "min_n": MIN_CALIBRATION_N,
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

    def add_model_snapshots_sync(self, rows, min_interval_s=SNAPSHOT_INTERVAL_S, now=None):
        return run_sync(self.add_model_snapshots(list(rows), min_interval_s, now))

    def count_model_runs_sync(self):
        return run_sync(self.count_model_runs())

    def compact_model_runs_sync(self):
        return run_sync(self.compact_model_runs())

    def upsert_settled_sync(self, market, snapshots):
        return run_sync(self.upsert_settled(market, list(snapshots)))

    def settled_tickers_sync(self):
        return run_sync(self.settled_tickers())

    def settled_dataset_sync(self, horizon_h=None):
        return run_sync(self.settled_dataset(horizon_h))

    def settled_counts_sync(self):
        return run_sync(self.settled_counts())

    def get_meta_sync(self, key):
        return run_sync(self.get_meta(key))

    def set_meta_sync(self, key, value):
        return run_sync(self.set_meta(key, value))

    def get_research_sync(self, ticker, now_epoch):
        return run_sync(self.get_research(ticker, now_epoch))

    def put_research_sync(self, ticker, brief, created_at, expires_at):
        return run_sync(self.put_research(ticker, brief, created_at, expires_at))


BRIER_THRESHOLD = 0.25

# Bets (kind='bet') are never compacted; scan rows keep the last per ticker per hour.
COMPACT_SQL = (
    "DELETE FROM model_runs WHERE COALESCE(kind,'scan') != 'bet' AND id NOT IN ("
    "SELECT MAX(id) FROM model_runs WHERE COALESCE(kind,'scan') != 'bet' GROUP BY ticker, substr(ts, 1, 13))"
)


def brier_score(pairs: list[tuple[float, int]]) -> Optional[float]:
    if not pairs:
        return None
    return round(sum((p - y) ** 2 for p, y in pairs) / len(pairs), 6)


# Fewer settled bets than this is an "insufficient sample": still uncalibrated.
MIN_CALIBRATION_N = 20


def is_uncalibrated(brier: Optional[float], n: Optional[int] = None) -> bool:
    """No settled history, Brier > 0.25, or (when ``n`` is given) fewer than
    MIN_CALIBRATION_N settled bets counts as uncalibrated (fail safe: half Kelly)."""
    if n is not None and n < MIN_CALIBRATION_N:
        return True
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

"""Speed pack item 2: WebSocket books, seq-gap resync, fills, scanner overlay. No network."""
from __future__ import annotations

import asyncio
import json

import pytest

from app.config import settings
from app.db.ledger import Ledger
from app.db.store import Store
from app.kalshi.books import BookStore, LocalBook, SeqTracker
from app.kalshi.ws import MarketStream
from app.strategy.scanner import scan_opportunities
from app.trading.orders import OrderGate

SNAP = {"market_ticker": "M-1", "clock": 10, "yes_dollars_fp": [["0.0800", "300.00"], ["0.2200", "333.00"]],
        "no_dollars_fp": [["0.5400", "20.00"], ["0.5600", "146.00"]]}


def test_local_book_snapshot_delta_and_implied_asks():
    b = LocalBook("M-1")
    b.apply_snapshot(SNAP)
    q = b.quote()
    assert q["yes_bid"] == 0.22 and q["no_bid"] == 0.56
    assert q["yes_ask"] == pytest.approx(0.44) and q["no_ask"] == pytest.approx(0.78)
    b.apply_delta({"price_dollars": "0.5600", "delta_fp": "-146.00", "side": "no"})
    assert b.quote()["yes_ask"] == pytest.approx(0.46)  # best NO bid fell to 0.54
    b.apply_delta({"price_dollars": "0.3000", "delta_fp": "5.00", "side": "yes"})
    assert b.quote()["no_ask"] == pytest.approx(0.70)


def test_seq_tracker_detects_gaps():
    s = SeqTracker()
    assert s.check(2, 1) and s.check(2, 2) and not s.check(2, 4) and s.check(2, 5)
    assert s.check(None, 9) and s.check(3, 1)


def test_bookstore_ignores_delta_before_snapshot_and_overlays():
    bs = BookStore()
    assert bs.delta({"market_ticker": "M-1", "price_dollars": "0.5", "delta_fp": "1", "side": "yes"}) is None
    bs.snapshot(SNAP)
    m = bs.overlay({"ticker": "M-1", "yes_ask_dollars": "0.9900", "no_ask_dollars": "0.9900", "yes_ask": 99})
    assert m["yes_ask_dollars"] == "0.4400" and m["no_ask_dollars"] == "0.7800" and "yes_ask" not in m
    assert m["_quote_source"] == "ws_book"
    assert bs.overlay({"ticker": "OTHER", "yes_ask_dollars": "0.3"}) == {"ticker": "OTHER", "yes_ask_dollars": "0.3"}
    bs.ticker({"market_ticker": "T-2", "yes_bid_dollars": "0.4000", "yes_ask_dollars": "0.4500"})
    q = bs.quote("T-2")
    assert q["source"] == "ws_ticker" and q["no_ask"] == pytest.approx(0.60)


class FakeWS:
    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent = []

    async def send(self, text):
        self.sent.append(json.loads(text))

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0)
        if not self.incoming:
            raise StopAsyncIteration
        return json.dumps(self.incoming.pop(0))


def test_session_subscribes_tracks_books_and_resyncs_on_seq_gap():
    st = MarketStream(BookStore())
    st.set_watch("scan", ["M-1", "M-2"])
    events = []
    st.on_event(lambda k, m: events.append(k))
    msgs = [
        {"id": 1, "type": "subscribed", "msg": {"channel": "market_lifecycle_v2", "sid": 1}},
        {"id": 2, "type": "subscribed", "msg": {"channel": "orderbook_delta", "sid": 2}},
        {"id": 2, "type": "subscribed", "msg": {"channel": "ticker", "sid": 3}},
        {"type": "orderbook_snapshot", "sid": 2, "seq": 1, "msg": SNAP},
        {"type": "orderbook_delta", "sid": 2, "seq": 2, "msg": {"market_ticker": "M-1", "price_dollars": "0.5600", "delta_fp": "-146.00", "side": "no"}},
        {"type": "orderbook_delta", "sid": 2, "seq": 5, "msg": {"market_ticker": "M-1", "price_dollars": "0.5400", "delta_fp": "-20.00", "side": "no"}},
        {"type": "market_lifecycle_v2", "sid": 1, "seq": 1, "msg": {"market_ticker": "M-1", "event_type": "determined"}},
    ]
    ws = FakeWS(msgs)
    asyncio.run(st.session(ws))
    cmds = ws.sent
    assert cmds[0] == {"id": 1, "cmd": "subscribe", "params": {"channels": ["market_lifecycle_v2"]}}
    assert cmds[1]["cmd"] == "subscribe" and set(cmds[1]["params"]["channels"]) == {"orderbook_delta", "ticker", "trade"}
    assert cmds[1]["params"]["market_tickers"] == ["M-1", "M-2"]
    resync = [c for c in cmds if c["cmd"] == "update_subscription"]
    assert resync and resync[-1]["params"] == {"sids": [2], "market_tickers": ["M-1", "M-2"], "action": "get_snapshot"}
    assert st.gaps == 1 and st.resyncs == 1
    # Gap delta was NOT applied; book is waiting for a fresh snapshot.
    assert st.books.books["M-1"].synced is False
    assert st.books.lifecycle["M-1"]["event_type"] == "determined"
    assert "book" in events and "lifecycle" in events


def test_watch_changes_use_update_subscription():
    st = MarketStream(BookStore())
    st.set_watch("scan", ["A"])
    ws = FakeWS([
        {"id": 2, "type": "subscribed", "msg": {"channel": "orderbook_delta", "sid": 7}},
        {"id": 2, "type": "subscribed", "msg": {"channel": "ticker", "sid": 8}},
        {"id": 2, "type": "subscribed", "msg": {"channel": "trade", "sid": 9}},
    ])
    asyncio.run(st.session(ws))
    st._conn = __import__("app.kalshi.ws", fromlist=["_Conn"])._Conn(ws)
    st._conn.sids = {"orderbook_delta": 7, "ticker": 8, "trade": 9}
    st.set_watch("scan", ["B"])
    asyncio.run(st._sync_watch())
    ups = [c["params"] for c in ws.sent if c["cmd"] == "update_subscription"]
    assert {"sids": [7], "market_tickers": ["B"], "action": "add_markets"} in ups
    assert {"sids": [8], "market_tickers": ["A"], "action": "delete_markets"} in ups


def test_no_keys_means_rest_polling(monkeypatch):
    class NoKey:
        available = False

    st = MarketStream(BookStore(), signer_fn=lambda: NoKey(), connect=lambda *a: pytest.fail("must not connect"))

    async def run():
        task = asyncio.create_task(st._loop_forever("market"))
        await asyncio.sleep(0.01)
        task.cancel()

    asyncio.run(run())
    s = st.status()
    assert s["connected"] is False and s["mode"] == "REST polling" and "needs Kalshi keys" in s["reason"]


def test_fill_stream_records_resting_order_fill(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "dry_run", False)
    store, ledger = Store(tmp_path / "s.db"), Ledger(tmp_path / "l.db")

    class Cl:
        host = "x"

        def place_order(self, body):
            return {"order_id": "ord-1", "fill_count": "0.00", "remaining_count": "5.00"}

    monkeypatch.setattr(type(settings), "has_keys", property(lambda self: True))
    gate = OrderGate(store, Cl(), ledger)
    res = gate.place(ticker="M-1", side="no", count=5, price_cents=30, source="sniper")
    assert res["ok"] and "ord-1" in gate.resting
    st = MarketStream(BookStore())
    st.on_event(lambda k, m: gate.on_fill(m) if k == "fill" else gate.on_user_order(m))
    ws = FakeWS([
        {"type": "fill", "sid": 13, "msg": {"order_id": "ord-1", "market_ticker": "M-1", "yes_price_dollars": "0.7000", "count_fp": "5.00", "outcome_side": "no"}},
    ])
    asyncio.run(st.fill_session(ws))
    assert ws.sent[0]["params"] == {"channels": ["fill", "user_orders"]}
    bets = ledger.bets_sync()
    assert len(bets) == 1 and bets[0]["source"] == "sniper" and bets[0]["price"] == pytest.approx(0.30)
    assert "ord-1" not in gate.resting and st.fills == 1


def test_scan_uses_live_quotes_and_reuses_market_list():
    calls = []

    class Client:
        host = "https://stream.test"

        def get_markets(self, **kw):
            calls.append(kw)
            return {"markets": [{"ticker": "M-1", "title": "m", "yes_ask_dollars": "0.9000", "no_ask_dollars": "0.1500",
                                 "volume_fp": "100", "status": "open"}], "cursor": None}

    class Board:
        fetch_ok = fetch_errors = 0

        def estimate(self, m):
            return None

    bs = BookStore()
    bs.snapshot(SNAP)
    r1 = scan_opportunities(Client(), limit=5, espn=Board(), include_game_series=False, use_context=False, quotes=bs, raw_ttl_s=60)
    r2 = scan_opportunities(Client(), limit=5, espn=Board(), include_game_series=False, use_context=False, quotes=bs, raw_ttl_s=60)
    assert len(calls) == 1 and r2["quotes"]["market_list_cached"] is True
    row = r1["markets"][0]
    assert row["yes_ask"] == pytest.approx(0.44) and row["quote_source"] == "ws_book"
    r3 = scan_opportunities(Client(), limit=5, espn=Board(), include_game_series=False, use_context=False)
    assert len(calls) == 2 and r3["markets"][0]["quote_source"] == "rest"


def test_status_endpoint_exposes_stream():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        s = c.get("/api/stream").json()
        assert s["ok"] and s["mode"] in {"live stream", "REST polling"}

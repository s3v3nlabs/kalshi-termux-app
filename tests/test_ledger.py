"""Item 1: SQLite ledger (aiosqlite) — model runs, bets, settlement."""
from __future__ import annotations

import asyncio

import pytest

from app.config import settings
from app.db.ledger import Ledger, run_sync, settle_open_bets
from app.db.store import Store
from app.strategy.scanner import model_run_rows
from app.trading.orders import OrderGate


def test_schema_and_round_trip(tmp_path):
    led = Ledger(tmp_path / "l.db")
    assert led.add_model_runs_sync([{"ticker": "A", "model_prob": 0.3, "features": {"x": 1}}]) == 1
    run = led.latest_model_run_sync("A")
    assert run["model_prob"] == 0.3 and run["features"] == {"x": 1}
    bid = led.add_bet_sync(ticker="A", side="no", price=0.6, stake=1.2, model_prob=0.7, market_prob=0.6)
    open_bets = led.bets_sync(settled=False)
    assert [b["id"] for b in open_bets] == [bid]
    # NO won -> outcome 1, pnl = 2 contracts * 0.40
    res = led.settle_bet_sync(bid, "no")
    assert res["bet"]["outcome"] == 1 and res["bet"]["pnl"] == pytest.approx(0.8)
    assert led.settle_bet_sync(bid, "yes")["already_settled"] is True
    lose = led.add_bet_sync(ticker="A", side="yes", price=0.25, stake=0.5, model_prob=0.3, market_prob=0.25)
    assert led.settle_bet_sync(lose, "no")["bet"]["pnl"] == pytest.approx(-0.5)
    assert len(led.bets_sync(settled=True)) == 2


def test_run_sync_inside_running_loop(tmp_path):
    led = Ledger(tmp_path / "l.db")

    async def inner():
        return led.add_model_runs_sync([{"ticker": "B", "model_prob": 0.5}])

    assert asyncio.run(inner()) == 1
    assert run_sync(led.model_runs())[0]["ticker"] == "B"


def test_bad_bet_rejected(tmp_path):
    led = Ledger(tmp_path / "l.db")
    with pytest.raises(ValueError):
        led.add_bet_sync(ticker="A", side="maybe", price=0.5, stake=1, model_prob=None, market_prob=None)
    with pytest.raises(ValueError):
        led.add_bet_sync(ticker="A", side="yes", price=1.0, stake=1, model_prob=None, market_prob=None)


def test_model_run_rows_from_scan():
    rows = [{"ticker": "T", "nash": {"model_prob_yes": 0.4}, "model_features": {"a": 1}}, {"ticker": "U"}]
    assert model_run_rows(rows) == [{"ticker": "T", "model_prob": 0.4, "features": {"a": 1}}]


def test_paper_fill_writes_bet_with_model_prob(tmp_path):
    old = settings.dry_run
    try:
        settings.dry_run = True
        led = Ledger(tmp_path / "l.db")
        led.add_model_runs_sync([{"ticker": "KX-1", "model_prob": 0.3}])
        gate = OrderGate(Store(tmp_path / "s.db"), client=object(), ledger=led)
        res = gate.place("KX-1", "no", 2, price_cents=40)
        assert res["ok"] and res["bet_id"]
        bet = led.bets_sync()[0]
        assert bet["side"] == "no" and bet["price"] == 0.4 and bet["stake"] == pytest.approx(0.8)
        assert bet["model_prob"] == pytest.approx(0.7)  # P(NO) = 1 - P(YES)
    finally:
        settings.dry_run = old


def test_live_fill_writes_bet_only_when_filled(tmp_path):
    old = (settings.dry_run, settings.key_id, settings.key_path)
    key = tmp_path / "k"
    key.write_text("placeholder")
    acks = [
        {"order_id": "a", "fill_count": "0.00", "remaining_count": "1.00", "ts_ms": 1},
        {"order_id": "b", "fill_count": "2.00", "remaining_count": "0.00", "average_fill_price": "0.5800", "ts_ms": 2},
    ]

    class Fake:
        def place_order(self, body):
            return acks.pop(0)

    try:
        settings.dry_run, settings.key_id, settings.key_path = False, "unit", key
        led = Ledger(tmp_path / "l.db")
        gate = OrderGate(Store(tmp_path / "s.db"), Fake(), led)
        gate.arm_live("ARM LIVE")
        assert gate.place("KX-2", "yes", 1, price_cents=30)["bet_id"] is None
        res = gate.place("KX-2", "no", 2, price_cents=45)
        bet = led.bets_sync()[0]
        assert res["bet_id"] == bet["id"]
        assert bet["price"] == pytest.approx(0.42)  # NO paid 1 - 0.58 YES-book price
    finally:
        settings.dry_run, settings.key_id, settings.key_path = old


def test_settle_open_bets_from_market_result(tmp_path):
    led = Ledger(tmp_path / "l.db")
    a = led.add_bet_sync(ticker="DONE", side="yes", price=0.2, stake=0.2, model_prob=0.3, market_prob=0.2)
    led.add_bet_sync(ticker="OPEN", side="yes", price=0.2, stake=0.2, model_prob=0.3, market_prob=0.2)

    class C:
        def get_market(self, t):
            if t == "DONE":
                return {"market": {"status": "finalized", "result": "yes"}}
            return {"market": {"status": "active", "result": ""}}

    done = settle_open_bets(led, C())
    assert [d["bet"]["id"] for d in done] == [a]
    assert done[0]["bet"]["pnl"] == pytest.approx(0.8)

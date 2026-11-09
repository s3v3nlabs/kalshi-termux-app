"""Speed pack item 7: near-finish sniper. No network: fake Kalshi client."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.db.ledger import Ledger
from app.db.store import Store
from app.trading import sniper as S
from app.trading.orders import OrderGate

NOW = datetime(2026, 11, 10, 2, 30, tzinfo=timezone.utc)
T_YES = "KXNBAGAME-26NOV09WASDET-DET"
T_NO = "KXNBAGAME-26NOV09WASDET-WAS"
EV = "KXNBAGAME-26NOV09WASDET"


def mkt(ticker, yes_ask, no_ask):
    return {"ticker": ticker, "event_ticker": EV, "status": "active",
            "expected_expiration_time": (NOW + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
            "yes_ask_dollars": f"{yes_ask:.4f}", "no_ask_dollars": f"{no_ask:.4f}"}


class FakeKalshi:
    def __init__(self, period=4, clock="1:30", home=110, away=100, status="inprogress", det_ask=0.88):
        self.details = {"status": status, "home_points": home, "away_points": away, "period": period,
                        "period_remaining_time": clock, "period_type": "quarter", "result_pending": False, "winner": ""}
        self.det_ask = det_ask
        self.sent, self.canceled = [], []
        self.calls = {"markets": 0, "milestones": 0, "live": 0}

    def get_markets(self, status="open", limit=200, series_ticker=None, **kw):
        self.calls["markets"] += 1
        if series_ticker != "KXNBAGAME":
            return {"markets": []}
        return {"markets": [mkt(T_YES, self.det_ask, 0.14), mkt(T_NO, 0.14, self.det_ask)]}

    def get_milestones(self, ev, limit=5):
        self.calls["milestones"] += 1
        return [{"id": "ms1", "title": "WAS Wizards at DET Pistons", "primary_event_tickers": [EV],
                 "details": {"main_game_event_ticker": EV}}]

    def get_live_data_batch(self, ids):
        self.calls["live"] += 1
        return [{"milestone_id": "ms1", "type": "basketball_game", "details": dict(self.details)}]

    def place_order(self, order):
        self.sent.append(order)
        return {"order_id": f"o{len(self.sent)}", "fill_count": 0, "remaining_count": order.get("count", 1)}

    def cancel_order(self, order_id, market_ticker=None):
        self.canceled.append((order_id, market_ticker))
        return {"order": {"order_id": order_id}}

    def get_positions(self, limit=100):
        return {"market_positions": []}

    def get_orders(self, status=None, limit=20):
        return {"orders": []}


class Gate:
    def __init__(self, ok=True):
        self.ok = ok

    def check(self, force=False):
        return {"ok": self.ok, "trading_active": self.ok, "reason": None if self.ok else "Kalshi trading is paused"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "dry_run", True)
    monkeypatch.setenv("GEO_LOCAL_STATE", "CA")
    store, ledger = Store(tmp_path / "s.db"), Ledger(tmp_path / "l.db")
    clock = {"t": NOW.timestamp()}

    def make(client, gate=None, on=True):
        orders = OrderGate(store, client, ledger)
        sn = S.Sniper(store=store, ledger=ledger, orders=orders, client_fn=lambda: client,
                      state_fn=lambda: "CA", bankroll_fn=lambda live: 1000.0, exchange_gate=gate or Gate(),
                      clock=lambda: clock["t"])
        if on:
            sn.set_enabled(True)
        return sn, orders

    return {"store": store, "ledger": ledger, "clock": clock, "make": make}


# ----- model -----

def test_remaining_fraction_and_model():
    assert S.remaining_fraction("nba", {"period": 4, "period_remaining_time": "6:00"}) == pytest.approx(360 / 2880)
    assert S.remaining_fraction("nhl", {"period": 3, "period_remaining_time": "10:00"}) == pytest.approx(600 / 3600)
    assert S.remaining_fraction("mlb", {"period": 9}) == pytest.approx(0.5 / 9)
    assert S.remaining_fraction("nba", {"period": 0}) == 1.0
    p_early = S.win_probability("nba", 10, 0.9)
    p_late = S.win_probability("nba", 10, 0.03)
    assert 0.5 < p_early < p_late <= 0.98  # never 1.0
    assert S.win_probability("nba", -30, 0.0) >= 0.02
    assert S.win_probability("nfl", 0, 0.5) == pytest.approx(0.5)


def test_yes_side_home_away():
    assert S.yes_is_home(T_YES) is True and S.yes_is_home(T_NO) is False
    assert S.yes_is_home("KXNHLGAME-26NOV09NYREDM-EDM") is True
    assert S.yes_is_home("KXEPLGAME-26NOV09ARSCHE-TIE") is None


def test_espn_status_line_parse():
    assert S.espn_remaining_fraction("nba", "2:24 - 4th") == pytest.approx(144 / 2880)
    assert S.espn_remaining_fraction("ncaab", "5:00 - 2nd Half") == pytest.approx(300 / 2400)
    assert S.espn_remaining_fraction("nfl", "Halftime") is None


def test_score_candidate_needs_net_edge():
    base = {"ticker": T_YES, "league": "nba", "status": "inprogress", "lead": 10, "frac_left": 0.03,
            "yes_ask": 0.88, "no_ask": 0.14, "bankroll": 1000, "fraction": 0.5, "max_fraction": 0.05,
            "fee_multiplier": 1.0, "ev_min": 0.03}
    r = S.score_candidate(base)
    assert r["pick"]["side"] == "yes" and r["pick"]["edge_net"] >= 0.03 and r["p_yes"] <= 0.98
    assert S.score_candidate({**base, "yes_ask": 0.97})["pick"] is None  # ask ~ model: no fire


# ----- engine -----

def test_off_by_default_never_trades(env):
    cl = FakeKalshi()
    sn, _ = env["make"](cl, on=False)
    assert sn.enabled is False
    out = sn.run_cycle()
    assert out["ran"] is False and cl.calls["markets"] == 0 and cl.sent == []


def test_paper_fire_logs_source_sniper(env):
    cl = FakeKalshi()
    sn, orders = env["make"](cl)
    out = sn.run_cycle()
    assert out["placed"] == 1 and out["mode"] == "paper" and cl.sent == []  # paper never sends
    o = out["orders"][0]
    assert o["ticker"] == T_YES and o["side"] == "yes" and o["edge_net"] >= sn.ev_min
    bets = env["ledger"].bets_sync()
    assert bets and bets[0]["source"] == "sniper"
    auto = env["ledger"].auto_orders_sync()
    assert auto[0]["status"] == "PAPER" and auto[0]["detail"]["source"] == "sniper"
    # one per event + cooldown: the NO leg / same event is not fired again
    assert sn.run_cycle()["placed"] == 0
    assert sn.hot and sn.interval_s == sn.fast_interval_s and T_YES in sn.watched


def test_not_near_finish_or_closed_exchange_or_geo(env, monkeypatch):
    cl = FakeKalshi(period=2, clock="6:00")
    sn, _ = env["make"](cl)
    assert sn.run_cycle()["placed"] == 0 and not sn.hot
    cl2 = FakeKalshi()
    sn2, _ = env["make"](cl2, gate=Gate(ok=False))
    out = sn2.run_cycle()
    assert out["placed"] == 0 and "paused" in out["error"] and cl2.calls["markets"] == 0
    cl3 = FakeKalshi()
    sn3, _ = env["make"](cl3)
    sn3.state_fn = lambda: "NV"  # sports blocked: fail closed
    assert sn3.run_cycle()["placed"] == 0


def test_result_pending_skipped(env):
    cl = FakeKalshi()
    cl.details["result_pending"] = True
    sn, _ = env["make"](cl)
    assert sn.run_cycle()["placed"] == 0


def test_live_resting_order_canceled_after_n_seconds(env, monkeypatch):
    monkeypatch.setattr(settings, "dry_run", False)
    monkeypatch.setattr(type(settings), "has_keys", property(lambda self: True))
    cl = FakeKalshi()
    sn, orders = env["make"](cl)
    out = sn.run_cycle()
    assert out["placed"] == 1 and len(cl.sent) == 1 and out["orders"][0]["status"] == "RESTING"
    oid = next(iter(orders.resting))
    env["clock"]["t"] += sn.cancel_s - 1
    assert sn.cancel_stale() == [] and cl.canceled == []
    env["clock"]["t"] += 2
    res = sn.cancel_stale()
    assert res and res[0]["ok"] and cl.canceled == [(oid, T_YES)] and oid not in orders.resting
    assert env["ledger"].auto_orders_sync()[0]["status"] == "CANCELED"


def test_wording_never_claims_certainty():
    import inspect

    src = inspect.getsource(S).lower()
    for bad in ("guaranteed", "almost certain", "sure win", "can't lose"):
        assert bad not in src


def test_sniper_endpoints_toggle():
    from fastapi.testclient import TestClient

    import app.main as M

    with TestClient(M.app) as c:
        r = c.post("/api/sniper", json={"enabled": True}).json()
        assert r["enabled"] is True and r["mode"] in {"paper", "live"}
        r = c.post("/api/sniper", json={"enabled": False}).json()
        assert r["enabled"] is False and r["mode"] == "off"
        assert c.post("/api/sniper/run").status_code == 409
        assert c.get("/api/status").json()["sniper"]["enabled"] is False

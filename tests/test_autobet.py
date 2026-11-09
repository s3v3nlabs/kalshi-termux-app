"""Auto-bet engine + continuous learning. No network: fake scan, fake client."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import geo
from app.config import settings
from app.db.ledger import Ledger
from app.db.store import Store
from app.strategy import model as M
from app.strategy import training as T
from app.strategy.learner import Learner, ingest_days_since
from app.trading.autobet import AutoBetEngine, best_auto_side, event_of, select_candidates
from app.trading.orders import OrderGate


def row(ticker, p_yes, yes_ask, no_ask=None, event=None, mispricing=0.1, scale=0.5):
    return {
        "ticker": ticker,
        "event_ticker": event or ticker.rsplit("-", 1)[0],
        "title": ticker,
        "yes_ask": yes_ask,
        "no_ask": no_ask if no_ask is not None else round(1 - yes_ask + 0.02, 4),
        "nash": {"model_prob_yes": p_yes},
        "model": {"p_yes": p_yes},
        "kelly_scale": scale,
        "mispricing": mispricing,
        "model_uncalibrated": scale < 1,
    }


ROWS = [
    row("KXCPI-26NOV-T3.0", 0.60, 0.40),  # +edge YES
    row("KXCPI-26NOV-T3.2", 0.62, 0.40),  # same event -> skipped
    row("KXFLAT-26NOV-A", 0.50, 0.51, 0.51),  # no edge either side
    row("KXRAIN-26NOV10-B1", 0.20, 0.60, 0.45),  # +edge NO (p_no 0.8 at 45c)
    row("KXGDP-26Q4-T2", 0.70, 0.50),
]


class FakeClient:
    host = "https://external-api.kalshi.com"

    def __init__(self):
        self.sent = []

    def place_order(self, order):
        self.sent.append(order)
        return {"order_id": f"o{len(self.sent)}", "fill_count": order.get("count", 1), "remaining_count": 0,
                "average_fill_price": None}

    def get_positions(self, limit=100):
        return {"market_positions": []}

    def get_orders(self, status=None, limit=20):
        return {"orders": []}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "dry_run", True)
    monkeypatch.setenv("GEO_LOCAL_STATE", "CA")
    store = Store(tmp_path / "s.db")
    ledger = Ledger(tmp_path / "l.db")
    client = FakeClient()
    gate = OrderGate(store, client, ledger)
    rows = {"markets": [dict(r) for r in ROWS]}

    def make(**kw):
        return AutoBetEngine(
            store=store, ledger=ledger, orders=gate, scan_fn=lambda bankroll: rows, client_fn=lambda: client,
            balance_fn=lambda: 500.0, interval_s=1, top_n=kw.pop("top_n", 5), paper_bankroll=1000.0, **kw
        )

    return {"store": store, "ledger": ledger, "client": client, "gate": gate, "rows": rows, "make": make}


# ----- selection -----

def test_best_side_uses_positive_edge_and_existing_kelly():
    pick = best_auto_side(ROWS[0], 1000.0)
    assert pick["side"] == "yes" and pick["price_cents"] == 40 and pick["edge"] > 0
    # half-Kelly (uncalibrated scale 0.5) of a 5% max fraction = 2.5% of 1000 -> $25 / 0.40 = 62
    assert pick["count"] == 62
    no_pick = best_auto_side(ROWS[3], 1000.0)
    assert no_pick["side"] == "no" and no_pick["price_cents"] == 45
    assert best_auto_side(ROWS[2], 1000.0) is None


def test_selection_keeps_scan_order_top_n_one_per_event():
    picks, skipped = select_candidates(ROWS, bankroll=1000.0, state="CA", top_n=5)
    assert [p["ticker"] for p in picks] == ["KXCPI-26NOV-T3.0", "KXRAIN-26NOV10-B1", "KXGDP-26Q4-T2"]
    assert skipped["same_event"] == 1 and skipped["no_edge"] == 1
    picks2, _ = select_candidates(ROWS, bankroll=1000.0, state="CA", top_n=2)
    assert len(picks2) == 2


def test_dedupe_skips_held_market_and_event():
    picks, skipped = select_candidates(
        ROWS, bankroll=1000.0, state="CA", held_tickers={"KXGDP-26Q4-T2"}, held_events={event_of("KXCPI-26NOV-T9")}
    )
    assert [p["ticker"] for p in picks] == ["KXRAIN-26NOV10-B1"]
    assert skipped["held"] == 3  # both CPI rows + GDP


@pytest.mark.parametrize("state", ["UT", "MI", "NV", "OH", "NY", "AZ", "MD", "MT", None, "ZZ"])
def test_geo_gate_blocks_sports_in_blocked_or_unknown_state(state):
    sports = [row("KXNFLGAME-26NOV09BUFKC-KC", 0.7, 0.4), row("KXNBAPTS-26NOV09-LEBRON25", 0.7, 0.4)]
    picks, skipped = select_candidates(sports + [ROWS[0]], bankroll=1000.0, state=state)
    assert [p["ticker"] for p in picks] == ["KXCPI-26NOV-T3.0"]
    assert skipped["geo_blocked"] == 2


def test_engine_never_sends_blocked_sports_order(env, monkeypatch):
    monkeypatch.setenv("GEO_LOCAL_STATE", "NY")
    env["rows"]["markets"] = [row("KXNFLGAME-26NOV09BUFKC-KC", 0.8, 0.3)]
    eng = env["make"]()
    eng.set_enabled(True)
    out = eng.run_cycle()
    assert out["placed"] == 0 and out["skipped"]["geo_blocked"] == 1
    assert env["ledger"].bets_sync() == [] and env["ledger"].auto_orders_sync() == []


# ----- toggle -----

def test_off_by_default_and_persisted(env):
    eng = env["make"]()
    assert eng.enabled is False and eng.status()["mode"] == "off"
    assert eng.run_cycle() == {"ran": False, "reason": "off"}
    eng.set_enabled(True)
    assert env["make"]().enabled is True  # survives a restart
    eng.set_enabled(False)
    assert env["make"]().enabled is False


def test_toggle_off_mid_cycle_stops_before_next_order(env):
    eng = env["make"]()
    real_place = env["gate"].place
    calls = []

    def place(**kw):
        calls.append(kw["ticker"])
        res = real_place(**kw)
        eng.set_enabled(False)  # user taps OFF while the cycle is sending
        return res

    env["gate"].place = place
    eng.set_enabled(True)
    out = eng.run_cycle()
    assert calls == ["KXCPI-26NOV-T3.0"] and out["placed"] == 1
    assert "turned off" in out["stopped"]
    assert eng.run_cycle()["ran"] is False


# ----- dry-run simulation + ledger -----

def test_dry_run_simulates_and_logs_auto_bets(env):
    eng = env["make"]()
    eng.set_enabled(True)
    out = eng.run_cycle()
    assert out["mode"] == "paper" and out["placed"] == 3
    assert env["client"].sent == []  # nothing left the phone
    bets = env["ledger"].bets_sync()
    assert len(bets) == 3 and {b["source"] for b in bets} == {"auto"}
    assert all(b["model_prob"] is not None for b in bets)  # feeds calibration
    log = env["ledger"].auto_orders_sync()
    assert {o["status"] for o in log} == {"PAPER"} and {o["mode"] for o in log} == {"paper"}
    st = eng.status()
    assert st["orders_today"] == 3 and st["pnl"]["open_n"] == 3 and st["pnl"]["realized_total"] == 0
    # next cycle: everything is held -> no duplicates
    again = eng.run_cycle()
    assert again["placed"] == 0 and len(env["ledger"].bets_sync()) == 3


def test_pnl_from_settled_auto_bets(env):
    eng = env["make"]()
    eng.set_enabled(True)
    eng.run_cycle()
    first = env["ledger"].bets_sync()[0]
    env["ledger"].settle_bet_sync(first["id"], first["side"])
    p = eng.status()["pnl"]
    assert p["settled_n"] == 1 and p["wins"] == 1 and p["realized_total"] > 0
    manual = env["gate"].place(ticker="KXX-1-A", side="yes", count=1, price_cents=50)
    assert env["ledger"].bets_sync()[-1]["source"] == "manual" and manual["ok"]


def test_live_only_when_armed_and_allowed(env, monkeypatch):
    eng = env["make"](top_n=1)
    eng.set_enabled(True)
    monkeypatch.setattr(settings, "dry_run", False)
    out = eng.run_cycle()  # allowed but not armed -> still paper
    assert out["mode"] == "paper" and env["client"].sent == []
    env["gate"]._live_armed = True
    out = eng.run_cycle()
    assert out["mode"] == "live" and out["bankroll"] == 500.0 and out["placed"] == 1
    sent = env["client"].sent[0]
    assert sent["ticker"] != "KXCPI-26NOV-T3.0"  # already bet -> deduped
    auto = env["ledger"].auto_orders_sync()[0]
    assert auto["mode"] == "live" and auto["status"] == "FILLED"
    assert env["ledger"].bets_sync()[-1]["source"] == "auto"


def test_live_dedupes_against_kalshi_positions_and_resting(env, monkeypatch):
    monkeypatch.setattr(settings, "dry_run", False)
    env["gate"]._live_armed = True
    env["client"].get_positions = lambda limit=100: {"market_positions": [{"ticker": "KXCPI-26NOV-T3.0", "position": 5}]}
    env["client"].get_orders = lambda status=None, limit=20: {"orders": [{"ticker": "KXRAIN-26NOV10-B1"}]}
    eng = env["make"]()
    eng.set_enabled(True)
    out = eng.run_cycle()
    assert [o["ticker"] for o in out["orders"]] == ["KXGDP-26Q4-T2"]


def test_api_toggle(env, monkeypatch):
    from app import main

    eng = env["make"]()
    monkeypatch.setattr(main, "autobet", eng)
    http = TestClient(main.app)
    assert http.get("/api/autobet").json()["enabled"] is False
    assert http.post("/api/autobet/run").status_code == 409
    r = http.post("/api/autobet", json={"enabled": True}).json()
    assert r["enabled"] is True and r["mode"] == "paper"
    r = http.post("/api/autobet", json={"enabled": False}).json()
    assert r["enabled"] is False and eng.run_cycle()["ran"] is False


# ----- continuous learning -----

def _rows(n, seed=7):
    import random

    rng = random.Random(seed)
    out = []
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    for i in range(n):
        p = rng.choice([0.03, 0.1, 0.3, 0.5, 0.7, 0.9, 0.97])
        y = 1 if rng.random() < p else 0
        f = M.build_features(p, volume=1000, spread_cents=2)
        f["market_p_yes"] = p
        out.append({"ticker": f"M{i:03d}", "close_time": (start + timedelta(hours=i)).isoformat(), "features": f,
                    "yes_outcome": y, "source": "settled_market", "horizon_h": 1})
    return out


def _learner(tmp_path, ledger, train_rows, **kw):
    calls = []

    def ingest(client, led, **k):
        calls.append(("ingest", k["days"]))
        led.set_meta_sync("last_ingest", k["now"].isoformat())
        return {"listed": 5, "eligible": 4, "fetched": 4, "stored": 4, "errors": []}

    def settle(led, client):
        calls.append(("settle",))
        return []

    return Learner(
        ledger=ledger, client_fn=lambda: None, interval_h=6, weights_path=tmp_path / "w.json",
        ingest_fn=ingest, settle_fn=settle, research_fn=lambda *a, **k: {},
        train_fn=lambda led, **k: T.train_and_gate(led, rows=train_rows, **k), **kw
    ), calls


def test_learner_promotes_only_when_holdout_brier_improves(tmp_path):
    led = Ledger(tmp_path / "l.db")
    lrn, calls = _learner(tmp_path, led, _rows(200))
    rec = lrn.run_once()
    assert calls[0][0] == "ingest" and calls[1] == ("settle",)
    assert rec["train"]["promoted"] is True
    assert rec["train"]["holdout_brier_proposed"] < rec["train"]["holdout_brier_current"]
    st = lrn.status()
    assert st["model_version"] == 1 and st["last_run"]["ingest"]["stored"] == 4 and st["holdout_brier"] is not None
    assert st["next_run"] and not lrn.due()


def test_learner_never_promotes_worse_weights(tmp_path, monkeypatch):
    led = Ledger(tmp_path / "l.db")
    good = {"w0": 0.0, "w1": 1.0, "w2": 0.0, "w3": 0.0, "w4": 0.0, "w5": 0.0}
    M.save_model({"version": 4, "weights": dict(good), "refit_log": []}, tmp_path / "w.json")
    monkeypatch.setattr(M, "fit", lambda samples, w, **kw: {k: (-1.0 if k == "w1" else 0.0) for k in M.WEIGHT_KEYS})
    lrn, _ = _learner(tmp_path, led, _rows(200))
    rec = lrn.run_once()
    assert rec["train"]["promoted"] is False and rec["train"]["gate_pass"] is False
    doc = M.load_model(tmp_path / "w.json")
    assert doc["version"] == 4 and doc["weights"]["w1"] == 1.0


def test_learner_schedule_and_incremental_ingest(tmp_path):
    now = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
    assert ingest_days_since(None, now) == T.DEFAULT_DAYS
    assert ingest_days_since((now - timedelta(hours=6)).isoformat(), now) == 2
    led = Ledger(tmp_path / "l.db")
    lrn, calls = _learner(tmp_path, led, _rows(10))
    assert lrn.due(now)
    lrn.run_once(now=now)
    assert not lrn.due(now + timedelta(hours=5)) and lrn.due(now + timedelta(hours=6, minutes=1))
    lrn.run_once(now=now + timedelta(hours=6))
    assert [c for c in calls if c[0] == "ingest"] == [("ingest", T.DEFAULT_DAYS), ("ingest", 2)]


def test_learner_refreshes_research_cache(tmp_path):
    led = Ledger(tmp_path / "l.db")
    import time

    led.put_research_sync("KEEP-1", {"x": 1}, "2026-01-01", time.time() + 1000)
    led.put_research_sync("OLD-1", {"x": 1}, "2026-01-01", time.time() - 10)  # expired
    led.add_bet_sync(ticker="HELD-1", side="yes", price=0.4, stake=4.0, model_prob=0.5, market_prob=0.4)
    rebuilt = []
    lrn, _ = _learner(tmp_path, led, _rows(10))
    lrn.research_fn = lambda t, **k: rebuilt.append(t)
    rec = lrn.run_once()
    assert rec["research"]["purged"] == 1 and rebuilt == ["HELD-1"]
    assert led.get_research_sync("KEEP-1", time.time()) is not None


def test_training_endpoint_reports_learner(tmp_path, monkeypatch):
    from app import main

    monkeypatch.setattr(main, "ledger", Ledger(tmp_path / "st.db"))
    data = TestClient(main.app).get("/api/training").json()
    assert {"interval_h", "model_version", "last_run", "holdout_brier"} <= set(data["learner"])

"""Phase 2: unusual-flow feature, one gem per event, settled analog library."""
from __future__ import annotations

import math
import pathlib

import pytest

from app.db.ledger import Ledger
from app.strategy import analogs as A
from app.strategy import model as M
from app.strategy.gems import UNUSUAL_FLOW_RATIO, suppress_paired_gems, unusual_flow
from app.strategy.scanner import enrich_market, scan_opportunities


def test_flow_z_math():
    assert M.flow_z(1, 0) == pytest.approx(0.0)  # ratio 1 -> 0
    assert M.flow_z(0, 100) == pytest.approx(-math.log(2), abs=1e-6)
    assert M.flow_z(800, 399) == pytest.approx(math.log1p(2.0) - math.log(2), abs=1e-6)
    assert M.flow_z(10**9, 0) == 3.0  # clipped
    assert M.flow_z(500, None) == 0.0  # OI unknown -> term drops out
    f = M.build_features(0.3, volume=800, open_interest=399, spread_cents=2)
    assert f["flow_z"] == pytest.approx(M.flow_z(800, 399))


def test_flow_is_a_logistic_term_with_start_weight():
    assert "flow_z" in M.FEATURES and M.WEIGHT_KEYS[-1] == f"w{len(M.FEATURES)}"
    i = M.FEATURES.index("flow_z") + 1
    w = {k: 0.0 for k in M.WEIGHT_KEYS}
    w[f"w{i}"] = 0.5
    assert M.score({"flow_z": 2.0}, w) == pytest.approx(1.0)
    assert M.load_model(M.DEFAULT_WEIGHTS_PATH)["weights"][f"w{i}"] == 0.1


def test_old_weights_file_gets_new_terms_at_start_weight(tmp_path):
    p = tmp_path / "w.json"
    M.save_model({"version": 1, "weights": {f"w{i}": 0.2 for i in range(6)}}, p)
    w = M.load_model(p)["weights"]
    assert w["w5"] == 0.2 and w["w6"] == M.START_WEIGHT


def test_scan_row_exposes_flow():
    row = enrich_market({"ticker": "KXF-1", "yes_ask_dollars": "0.08", "yes_bid_dollars": "0.07", "no_ask_dollars": "0.93",
                         "volume_fp": "4000", "open_interest_fp": "999"})
    g = row["gemini"]
    assert g["vol_oi_ratio"] == pytest.approx(4.0) and g["unusual_flow"] is True
    assert g["flow_z"] == pytest.approx(row["model"]["features"]["flow_z"])


def test_unusual_flow_threshold_in_isolation():
    assert UNUSUAL_FLOW_RATIO == 3.0
    assert unusual_flow(3.0) and not unusual_flow(2.99)


def _gem_row(ticker, event, nash, mult=20.0):
    return {"ticker": ticker, "event_ticker": event, "gem": True, "gem_status": "unverified",
            "best_payout_multiple": mult, "gemini": {"gem": True, "nash_payoff": nash}}


def test_both_sides_of_one_event_never_both_gems():
    # The NFL case: team A YES and team B YES are the two sides of one game.
    rows = [
        _gem_row("KXNFLGAME-26NOV08TBCHI-TB", "KXNFLGAME-26NOV08TBCHI", 0.9),
        _gem_row("KXNFLGAME-26NOV08TBCHI-CHI", "KXNFLGAME-26NOV08TBCHI", 1.4),
        _gem_row("KXGOLD-26NOV-T5000", "KXGOLD-26NOV", 0.6),
    ]
    assert suppress_paired_gems(rows) == 1
    kept = [r["ticker"] for r in rows if r["gem"]]
    assert kept == ["KXNFLGAME-26NOV08TBCHI-CHI", "KXGOLD-26NOV-T5000"]
    dropped = rows[0]
    assert dropped["gem_status"] is None and dropped["gemini"]["gem"] is False
    assert dropped["gem_suppressed"]["kept"] == "KXNFLGAME-26NOV08TBCHI-CHI"


class _Client:
    host = "fake"

    def __init__(self, markets):
        self.markets = markets

    def get_markets(self, **kw):
        return {"markets": self.markets, "cursor": ""}


class _Board:
    fetch_ok = True
    fetch_errors = 0

    def estimate(self, m):
        return None


def test_scanner_suppresses_pair_end_to_end(monkeypatch, tmp_path):
    w = {k: 0.0 for k in M.WEIGHT_KEYS}
    w["w0"] = math.log(0.14 / 0.86)
    path = tmp_path / "w.json"
    M.save_model({"version": 9, "weights": w}, path)
    monkeypatch.setattr(M, "runtime_weights_path", lambda: path)

    def tail(t, ask):
        return {"ticker": t, "event_ticker": "KXEVT-1", "title": t, "yes_ask_dollars": f"{ask:.4f}",
                "yes_bid_dollars": f"{ask - 0.01:.4f}", "no_ask_dollars": f"{1 - ask + 0.01:.4f}",
                "volume_fp": "800", "open_interest_fp": "400", "status": "open"}

    res = scan_opportunities(_Client([tail("KXEVT-1-A", 0.08), tail("KXEVT-1-B", 0.07)]), limit=5,
                             espn=_Board(), include_game_series=False, analog_pool=[])
    gems = [r for r in res["markets"] if r["gem"]]
    assert len(gems) == 1 and res["gems_suppressed"] == 1
    assert all(r["analogs"]["analog_status"] == "insufficient" for r in res["markets"])


def _pool(n_hit, n_miss, implied=0.05):
    rows = []
    for i in range(n_hit + n_miss):
        rows.append({"ticker": f"S{i}", "yes_ask": implied, "yes_bid": implied - 0.01, "snap_volume": 1000.0,
                     "snap_oi": 500.0, "result": "yes" if i < n_hit else "no"})
    return A.build_pool(rows)


def test_analog_matching_known_hit_rate():
    pool = _pool(3, 17)  # 3 of 20 cheap-YES tails settled YES
    # Non-matching noise: other tier, wide spread, very different flow.
    pool += A.build_pool([
        {"ticker": "X1", "yes_ask": 0.5, "yes_bid": 0.49, "snap_volume": 1000.0, "snap_oi": 500.0, "result": "yes"},
        {"ticker": "X2", "yes_ask": 0.05, "yes_bid": 0.0, "snap_volume": 1000.0, "snap_oi": 500.0, "result": "yes"},
        {"ticker": "X3", "yes_ask": 0.05, "yes_bid": 0.04, "snap_volume": 100000.0, "snap_oi": 10.0, "result": "yes"},
    ])
    setup = A.setup_from_prices(0.055, 0.045, 900, 400)
    assert setup["side"] == "yes" and setup["tier"] == "13x"
    st = A.analog_stats(setup, pool)
    assert (st["analog_hits"], st["analog_n"]) == (3, 20)
    assert st["analog_hit_rate"] == pytest.approx(0.15) and st["analog_status"] == "ok"
    assert "3 of 20" in st["analog_text"]


def test_analog_side_follows_cheaper_side():
    s = A.setup_from_prices(0.96, 0.95, 10, 10)  # NO ask = 0.05
    assert s["side"] == "no" and s["implied_prob"] == pytest.approx(0.05)
    pool = A.build_pool([{"ticker": "N", "yes_ask": 0.96, "yes_bid": 0.95, "snap_volume": 10.0, "snap_oi": 10.0, "result": "no"}])
    assert pool[0]["hit"] == 1


def test_low_n_shows_insufficient_not_a_rate():
    st = A.analog_stats(A.setup_from_prices(0.055, 0.045, 900, 400), _pool(4, 2))
    assert st["analog_n"] == 6 and st["analog_hit_rate"] is None and st["analog_status"] == "insufficient"
    assert "Not enough analogs" in st["analog_text"] and "%" not in st["analog_text"]


def test_analogs_do_not_promote_gem_status():
    row = enrich_market({"ticker": "KXT-9", "yes_ask_dollars": "0.08", "yes_bid_dollars": "0.07", "no_ask_dollars": "0.93",
                         "volume_fp": "800", "open_interest_fp": "400"},
                        weights={**{k: 0.0 for k in M.WEIGHT_KEYS}, "w0": math.log(0.14 / 0.86)}, uncalibrated=True)
    A.annotate_rows([row], _pool(20, 0, implied=0.08))
    assert row["analogs"]["analog_hit_rate"] == 1.0
    assert row["gem_status"] == "unverified"


def test_analog_pool_from_ledger(tmp_path):
    led = Ledger(tmp_path / "a.db")
    m = {"ticker": "KXA-1", "close_time": "2026-11-01T00:00:00+00:00", "result": "no"}
    led.upsert_settled_sync(m, [{"horizon_h": 1, "snapshot_ts": "2026-10-31T23:00:00+00:00", "yes_ask": 0.04, "yes_bid": 0.03,
                                 "volume": 100.0, "open_interest": 50.0, "features": {}}])
    pool = A.load_pool(led)
    assert len(pool) == 1 and pool[0]["side"] == "yes" and pool[0]["hit"] == 0


def test_no_guaranteed_or_lock_copy_in_new_code():
    for path in ("app/strategy/analogs.py", "app/strategy/gems.py", "app/strategy/training.py"):
        src = pathlib.Path(path).read_text().lower()
        for word in ("guaranteed", "lock", "sure win", "can't lose", "almost-certain", "almost certain"):
            assert word not in src, (path, word)
    js = pathlib.Path("static/js/app.js").read_text().lower()
    new_js = js[js.index("function analoglines") if "function analoglines" in js else js.index("function analogline"): js.index("function scanrowhtml")]
    for word in ("guaranteed", "lock", "sure win", "almost certain"):
        assert word not in new_js

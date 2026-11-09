"""Item 5: softmax equilibrium + mispricing ranking."""
from __future__ import annotations

from app.strategy.nash import mispricing_scores, nash_decision, softmax_equilibrium
from app.strategy.scanner import scan_opportunities, sort_scan_rows


def test_agree_means_no_mispricing():
    eq = softmax_equilibrium([{"ticker": "A", "model_p": 0.4, "market_p": 0.4, "volume": 10}])
    assert abs(eq["A"] - 0.4) < 1e-3


def test_thin_market_leans_to_model_liquid_stays_near_book():
    rows = [
        {"ticker": "THIN", "model_p": 0.7, "market_p": 0.5, "volume": 1},
        {"ticker": "DEEP", "model_p": 0.7, "market_p": 0.5, "volume": 10_000},
    ]
    eq = softmax_equilibrium(rows, temperature=0.1)
    assert 0.5 < eq["DEEP"] < eq["THIN"] <= 0.7 + 1e-6
    scores = mispricing_scores(rows)
    assert scores["THIN"]["mispricing"] > scores["DEEP"]["mispricing"]
    assert scores["THIN"]["mispricing"] == round(abs(eq["THIN"] - 0.5), 6)


def test_top_ten_by_volume_only_and_deterministic():
    rows = [{"ticker": f"T{i}", "model_p": 0.3, "market_p": 0.5, "volume": i} for i in range(15)]
    s1, s2 = mispricing_scores(rows), mispricing_scores(rows)
    assert set(s1) == {f"T{i}" for i in range(5, 15)}
    assert s1 == s2


def test_matrix_kept():
    r = nash_decision(0.3, 0.72, model_prob_yes=0.4)
    assert set(r.payoff_matrix) == {"BUY_YES", "BUY_NO", "PASS"}


def test_sort_mispricing_puts_scored_rows_first():
    rows = [
        {"ticker": "a", "mispricing": None, "best_payout_multiple": 50},
        {"ticker": "b", "mispricing": 0.02, "best_payout_multiple": 2},
        {"ticker": "c", "mispricing": 0.10, "best_payout_multiple": 1.5},
    ]
    assert [r["ticker"] for r in sort_scan_rows(rows, "mispricing")] == ["c", "b", "a"]


def test_scan_defaults_to_mispricing():
    def mk(t, ya, na, vol):
        return {"ticker": t, "title": t, "yes_ask_dollars": ya, "no_ask_dollars": na, "volume_fp": vol, "status": "open"}

    class Client:
        host = "https://example.test"

        def get_markets(self, **kw):
            return {"markets": [mk("A", "0.1000", "0.9100", "50"), mk("B", "0.5000", "0.5100", "90000"), mk("C", "0.8000", "0.2100", "10")], "cursor": None}

    class Board:
        fetch_ok = fetch_errors = 0

        def estimate(self, m):
            return None

    res = scan_opportunities(client=Client(), limit=10, espn=Board(), include_game_series=False)
    assert res["sort"] == "mispricing"
    scores = [r["mispricing"] for r in res["markets"]]
    assert all(s is not None for s in scores) and scores == sorted(scores, reverse=True)
    assert all(r["nash_prob"] is not None for r in res["markets"])

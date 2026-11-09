"""Speed pack item 6: Kalshi fee formula, fee-aware Kelly, series multiplier lookup."""
from __future__ import annotations

from app.strategy.fees import (
    SeriesFees,
    fee_aware_kelly,
    maker_fee,
    series_of,
    taker_fee,
    taker_multiplier,
)
from app.trading.autobet import best_auto_side


def test_fee_schedule_examples():
    # Fee schedule table: 1 contract @ 50c -> $0.02 (0.07*0.25 = 0.0175 rounded up)
    assert taker_fee(0.50, 1) == 0.02
    # 100 contracts @ 50c -> 0.07*100*0.25 = $1.75
    assert taker_fee(0.50, 100) == 1.75
    # 100 @ 10c -> 0.07*100*0.09 = 0.63
    assert taker_fee(0.10, 100) == 0.63
    # maker 0.0175*100*0.25 = 0.4375 -> 0.44
    assert maker_fee(0.50, 100) == 0.44
    # multiplier 0.5 halves before rounding: 0.875 -> 0.88
    assert taker_fee(0.50, 100, multiplier=0.5) == 0.88
    # centicent rounding option
    assert taker_fee(0.50, 1, round_to=0.0001) == 0.0175
    assert taker_fee(0.0, 10) == 0.0 and taker_fee(0.5, 0) == 0.0


def test_fee_can_kill_a_thin_edge():
    # p=0.52 at 50c: gross 2c/contract but 1-contract fee is 2c -> PASS
    k = fee_aware_kelly(0.52, 0.50, 20.0, fraction=0.5, max_fraction=0.05)
    assert k.contracts == 0 and "fees" in k.reason
    # Bigger edge sizes, spend incl. fee within the Kelly cap, net EV positive
    k2 = fee_aware_kelly(0.70, 0.50, 1000.0, fraction=0.5, max_fraction=0.05)
    assert k2.contracts > 0 and k2.ev_total_net > 0
    assert k2.cost_total <= 1000.0 * 0.05 * 0.5 * 2 + 1e-9  # never above max fraction
    assert abs(k2.ev_total_net - (k2.contracts * 0.20 - k2.fee_total)) < 1e-6


def test_series_lookup_cached_and_fallback():
    calls = []

    def fetch(s):
        calls.append(s)
        if s == "BAD":
            raise RuntimeError("down")
        return {"series": {"ticker": s, "fee_type": "quadratic_with_maker_fees", "fee_multiplier": 0.5}}

    sf = SeriesFees(fetch)
    assert sf.multiplier("KXMLBGAME") == 0.5
    assert sf.multiplier("kxmlbgame") == 0.5 and calls == ["KXMLBGAME"]
    assert sf.multiplier("BAD") == 1.0  # conservative default
    assert taker_multiplier(None, None) == 1.0
    assert series_of({"ticker": "KXMLBGAME-26NOV09NYYBOS-NYY"}) == "KXMLBGAME"
    assert series_of({"series_ticker": "kxnba", "ticker": "X-Y"}) == "KXNBA"


def test_autobet_pick_uses_series_multiplier():
    row = {"ticker": "KXMLBGAME-26NOV09NYYBOS-NYY", "yes_ask": 0.40, "no_ask": 0.62,
           "nash": {"model_prob_yes": 0.60}, "kelly_scale": 1.0}
    full = best_auto_side(row, 1000.0)
    half = best_auto_side(row, 1000.0, fee_multiplier=0.5)
    assert full["fee_multiplier"] == 1.0 and half["fee_multiplier"] == 0.5
    assert half["fee_total"] < full["fee_total"] or half["count"] > full["count"]
    assert full["ev_total"] == round(full["count"] * full["edge"] - full["fee_total"], 4)


def test_scanner_row_reports_fee_and_net_ev():
    from app.strategy.scanner import enrich_market

    m = {"ticker": "KXCPI-26NOV-T3.0", "event_ticker": "KXCPI-26NOV", "title": "CPI above 3.0%",
         "yes_ask_dollars": "0.3000", "no_ask_dollars": "0.7200", "volume_fp": "500", "open_interest_fp": "200",
         "status": "active"}
    row = enrich_market(m, bankroll=1000.0)
    k, f = row["kelly"], row["fee"]
    assert f["type"] == "taker" and f["multiplier"] == 1.0 and f["per_contract_1"] > 0
    assert "edge_net" in k and k["edge_net"] <= k["edge"]
    assert k["contracts"] <= k["contracts_before_fees"]

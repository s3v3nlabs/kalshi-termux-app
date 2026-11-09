"""Phase 3: ESPN / NOAA weather / crypto context adapters (canned JSON, no network)."""
from __future__ import annotations

import json
import math
import pathlib
from datetime import datetime, timezone

import pytest

from app import geo
from app.strategy import context as C
from app.strategy import model as M
from app.strategy.espn import EspnBoard, _pregame_probability
from app.strategy.scanner import enrich_market, scan_opportunities
from app.strategy.training import snapshot_features

FX = json.loads((pathlib.Path(__file__).parent / "fixtures" / "context_fixture.json").read_text())
NOW = datetime(2026, 11, 9, 6, 0, tzinfo=timezone.utc)  # 1:00 AM ET on Nov 9


def _obs(*temps_c):
    return {"features": [{"properties": {"timestamp": f"2026-11-09T0{i}:00:00+00:00",
                                          "temperature": {"unitCode": "wmoUnit:degC", "value": t}}} for i, t in enumerate(temps_c)]}


def nws_fetch(obs=None):
    def fetch(url):
        if "/points/" in url:
            return FX["nws_points"]
        if "forecast/hourly" in url:
            return FX["nws_hourly"]
        if "/observations" in url:
            return obs if obs is not None else {"features": []}
        if "coinbase" in url:
            return FX["coinbase_btc"]
        raise AssertionError(url)
    return fetch


def boom(url):
    raise RuntimeError("network down")


def _wx(ticker="KXHIGHNY-26NOV09-T57", st="less", floor=None, cap=57, yes="0.1900", no="0.8300"):
    return {"ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "strike_type": st, "floor_strike": floor,
            "cap_strike": cap, "yes_ask_dollars": yes, "no_ask_dollars": no, "yes_bid_dollars": f"{float(yes) - 0.01:.4f}",
            "volume_fp": "3000", "open_interest_fp": "1500", "close_time": "2026-11-10T06:00:00Z"}


def _hourly_nov9():
    return [p["temperature"] for p in FX["nws_hourly"]["properties"]["periods"] if p["startTime"].startswith("2026-11-09")]


def test_parse_weather_market():
    p = C.parse_weather_market(_wx())
    assert p["metric"] == "high" and p["station"] == "KNYC" and p["date"] == "2026-11-09"
    assert p["strike"] == {"type": "less", "floor": None, "cap": 57.0}
    low = C.parse_weather_market(_wx("KXLOWTNYC-26NOV09-T49", "greater", 49, None))
    assert low["metric"] == "low" and low["city"] == "NYC"
    assert C.parse_weather_market(_wx("KXHIGHZZZ-26NOV09-T50")) is None  # unknown city
    assert C.parse_weather_market({"ticker": "KXFED-26DEC-T5", "strike_type": "greater", "floor_strike": 5}) is None


def test_whole_degree_strike_semantics():
    assert C.p_yes_from_temp(64.5, {"type": "greater", "floor": 64, "cap": None}, 2.5) == pytest.approx(0.5)
    assert C.p_yes_from_temp(56.5, {"type": "less", "floor": None, "cap": 57}, 2.5) == pytest.approx(0.5)
    between = C.p_yes_from_temp(58.5, {"type": "between", "floor": 58, "cap": 59}, 2.5)
    assert between == pytest.approx(C._norm_cdf(1 / 2.5) - C._norm_cdf(-1 / 2.5))
    assert C.weather_sigma(0) == 2.5 and C.weather_sigma(1) == 3.0 and C.weather_sigma(20) == 6.0


def test_weather_estimate_from_canned_nws():
    est = C.WeatherAdapter(fetch=nws_fetch(), now=lambda: NOW).estimate(_wx())
    hi = max(_hourly_nov9())
    assert est["estimate_f"] == pytest.approx(hi) and est["lead_days"] == 0 and est["sigma_f"] == 2.5
    assert est["p_yes"] == pytest.approx(max(0.01, C._norm_cdf((56.5 - hi) / 2.5)), abs=1e-4)
    assert "forecast, not a result" in est["hint"]


def test_observed_high_counts_same_day():
    # 20 °C = 68 °F already observed at the station today -> a ">64" high is near-certain YES.
    m = _wx("KXHIGHNY-26NOV09-T64", "greater", 64, None, yes="0.0500", no="0.9600")
    est = C.WeatherAdapter(fetch=nws_fetch(_obs(10.0, 20.0)), now=lambda: NOW).estimate(m)
    assert est["observed_extreme_f"] == pytest.approx(68.0) and est["estimate_f"] == pytest.approx(68.0)
    assert est["p_yes"] == 0.99
    assert C.observed_bound("high", [68.0], {"type": "less", "floor": None, "cap": 57}) == 0.01
    assert C.observed_bound("low", [40.0], {"type": "less", "floor": None, "cap": 42}) == 0.99
    assert C.observed_bound("low", [45.0], {"type": "greater", "floor": 49, "cap": None}) == 0.01
    assert C.observed_bound("high", [55.0], {"type": "greater", "floor": 64, "cap": None}) is None


def test_weather_delta_flows_into_model():
    row = enrich_market(_wx(), context=C.ContextAdapters(C.WeatherAdapter(nws_fetch(), now=lambda: NOW), C.CryptoAdapter(boom)))
    f = row["model"]["features"]
    p = row["context"]["weather"]["p_yes"]
    assert f["weather_delta"] == pytest.approx(M.logit(p) - M.logit(row["model"]["market_p_yes"]), abs=1e-5)
    assert row["context"]["crypto"] is None and f["crypto_vol_z"] == 0.0


def test_weather_fails_soft():
    est = C.WeatherAdapter(fetch=boom, now=lambda: NOW).estimate(_wx())
    assert est["p_yes"] is None and "failed" in est["error"]
    row = enrich_market(_wx(), context=C.ContextAdapters(C.WeatherAdapter(boom, now=lambda: NOW), C.CryptoAdapter(boom)))
    assert row["model"]["features"]["weather_delta"] == 0.0 and row["model"]["p_yes"] is not None
    past = C.WeatherAdapter(fetch=nws_fetch(), now=lambda: datetime(2026, 11, 12, tzinfo=timezone.utc)).estimate(_wx())
    assert past["p_yes"] is None and "passed" in past["error"]


def _btc(st="greater", floor=84799.99, cap=None):
    return {"ticker": "KXBTCD-26NOV0902-T84799.99", "event_ticker": "KXBTCD-26NOV0902", "strike_type": st,
            "floor_strike": floor, "cap_strike": cap, "yes_ask_dollars": "0.0200", "no_ask_dollars": "0.9900",
            "yes_bid_dollars": "0.0100", "volume_fp": "900", "open_interest_fp": "800"}


def test_crypto_vol_math():
    assert C.realized_vol([100, 101, 100, 101, 100]) == pytest.approx(
        math.sqrt(sum((r - 0) ** 2 for r in [math.log(1.01), math.log(100 / 101)] * 2) / 3), rel=1e-3)
    assert C.vol_direction(70000, {"type": "greater", "floor": 80000}) == 1
    assert C.vol_direction(90000, {"type": "greater", "floor": 80000}) == -1
    assert C.vol_direction(90000, {"type": "less", "cap": 80000}) == 1
    assert C.vol_direction(75000, {"type": "between", "floor": 74000, "cap": 76000}) == -1
    assert C.vol_direction(70000, {"type": "between", "floor": 74000, "cap": 76000}) == 1


def test_crypto_context_from_canned_candles():
    ad = C.CryptoAdapter(fetch=nws_fetch())
    ctx = ad.context(_btc())
    rows = sorted(FX["coinbase_btc"], key=lambda r: r[0])
    closes = [r[4] for r in rows]
    z = max(-3, min(3, math.log(C.realized_vol(closes[-25:]) / C.realized_vol(closes[:-24])) / 0.5))
    assert ctx["spot"] == closes[-1] and ctx["vol_z"] == pytest.approx(z, abs=1e-3)
    assert ctx["direction"] == 1 and ctx["crypto_vol_z"] == pytest.approx(ctx["vol_z"], abs=1e-3)
    flipped = ad.context(_btc("less", None, 84799.99))  # YES = stays below a strike far above spot
    assert flipped["crypto_vol_z"] == pytest.approx(-ctx["crypto_vol_z"], abs=1e-6)
    assert "Context, not a forecast" in ctx["hint"]


def test_crypto_only_applies_to_crypto_and_fails_soft():
    ad = C.CryptoAdapter(fetch=boom)
    assert ad.context({"ticker": "KXHIGHNY-26NOV09-T57", "strike_type": "less", "cap_strike": 57}) is None
    bad = ad.context(_btc())
    assert bad["crypto_vol_z"] is None
    row = enrich_market(_btc(), context=C.ContextAdapters(C.WeatherAdapter(boom), ad))
    assert row["model"]["features"]["crypto_vol_z"] == 0.0


def test_context_terms_in_z_with_start_weight():
    assert M.FEATURES[-2:] == ("weather_delta", "crypto_vol_z")
    w = M.load_model(M.DEFAULT_WEIGHTS_PATH)["weights"]
    assert w["w7"] == 0.1 and w["w8"] == 0.1
    f = M.build_features(0.3, weather_p_yes=0.6, crypto_vol_z=1.5)
    assert f["weather_delta"] == pytest.approx(M.logit(0.6) - M.logit(0.3)) and f["crypto_vol_z"] == 1.5
    zero = {k: 0.0 for k in M.WEIGHT_KEYS}
    assert M.score(f, {**zero, "w7": 1.0, "w8": 1.0}) == pytest.approx(f["weather_delta"] + 1.5)
    # Historical rows keep live-only context as null (0 in z).
    snap = {"horizon_h": 1, "snapshot_ts": "2026-11-08T10:00:00+00:00", "yes_ask": 0.2, "yes_bid": 0.18, "volume": 10.0, "open_interest": 5.0}
    hist = snapshot_features({"close_time": "2026-11-08T12:00:00+00:00"}, snap)
    assert all(hist[k] is None for k in M.LIVE_ONLY_FEATURES)


def test_espn_moneyline_v2_shape_is_used():
    comp = {"odds": [FX["espn_odds_v2"]]}
    ph = 378 / 478
    pa = 100 / 400
    assert _pregame_probability(comp, "home") == pytest.approx(ph / (ph + pa), abs=1e-6)
    assert _pregame_probability(comp, "away") == pytest.approx(pa / (ph + pa), abs=1e-6)
    no_ml = {"odds": [{"spread": -8.5, "moneyline": {"home": {"close": {"odds": "OFF"}}}}]}
    assert 0.7 < _pregame_probability(no_ml, "home") < 0.85  # falls back to the spread map


def test_espn_match_status():
    board = EspnBoard(fetcher=lambda league, day: [])
    assert board.match_status({"ticker": "KXNFLGAME-26SEP24ATLGB-GB", "rules_primary": "nothing useful"}) == "unparsed"
    rules = "If Green Bay wins the Atlanta vs Green Bay Pro Football game originally scheduled for Sep 24, 2026, then the market resolves to Yes."
    assert board.match_status({"ticker": "KXNFLGAME-26SEP24ATLGB-GB", "rules_primary": rules}) == "no_game"


def test_scan_reports_context_and_geo_unchanged():
    class Cl:
        host = "fake"

        def get_markets(self, **kw):
            return {"markets": [_wx(), _btc()], "cursor": ""}

    class Board:
        fetch_ok = fetch_errors = 0

        def estimate(self, m):
            return None

    ctx = C.ContextAdapters(C.WeatherAdapter(nws_fetch(), now=lambda: NOW), C.CryptoAdapter(nws_fetch()))
    res = scan_opportunities(Cl(), limit=5, espn=Board(), include_game_series=False, context=ctx, analog_pool=[])
    st = res["context"]
    assert st["weather_used"] == 1 and st["crypto_used"] == 1
    # Geo gate is untouched: non-sports allowed anywhere, sports fail closed when state is unknown.
    assert geo.check("KXHIGHNY-26NOV09-T57", None)["allowed"] is True
    for state in (None, "UT", "MI", "NV", "OH", "NY", "AZ", "MD", "MT"):
        assert geo.check("KXNFLGAME-26NOV08TBCHI-CHI", state)["allowed"] is False


def test_no_guaranteed_or_lock_copy_in_context():
    src = pathlib.Path("app/strategy/context.py").read_text().lower()
    for word in ("guaranteed", "lock", "sure win", "almost certain", "almost-certain"):
        assert word not in src.replace("threading.lock", "").replace("_lock", "")


def test_context_box_js_has_no_certainty_copy():
    js = pathlib.Path("static/js/app.js").read_text()
    box = js[js.index("function ctxText") : js.index("function featureBreakdown")].lower()
    for word in ("guaranteed", "sure win", "almost certain", "can't lose"):
        assert word not in box
    assert " lock" not in box.replace("l[o]ck", "")

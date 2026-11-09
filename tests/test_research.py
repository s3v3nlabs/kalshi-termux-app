"""Per-row research companion: structured brief, cache, wording, no poll use. No network."""
from __future__ import annotations

import json
import pathlib
from datetime import datetime, timezone

import pytest

from app import geo
from app.db.ledger import Ledger
from app.strategy import context as C
from app.strategy import research as R
from app.strategy.espn import EspnBoard
from app.strategy.scanner import scan_opportunities

FIX = pathlib.Path(__file__).parent / "fixtures"
RX = json.loads((FIX / "research_fixture.json").read_text())
CX = json.loads((FIX / "context_fixture.json").read_text())
NOW = datetime(2026, 11, 9, 6, 0, tzinfo=timezone.utc)


def ctx_fetch(url):
    if "/points/" in url:
        return CX["nws_points"]
    if "forecast/hourly" in url:
        return CX["nws_hourly"]
    if "/observations" in url:
        return {"features": []}
    if "coinbase" in url:
        return CX["coinbase_btc"]
    raise AssertionError(f"unexpected fetch {url}")


def boom(url):
    raise RuntimeError("network down")


def ctx(fetch=ctx_fetch):
    return C.ContextAdapters(C.WeatherAdapter(fetch, now=lambda: NOW), C.CryptoAdapter(fetch))


def _team(loc, nick, abbr):
    return {"location": loc, "name": nick, "abbreviation": abbr, "displayName": f"{loc} {nick}", "shortDisplayName": nick}


def gb_event():
    return {
        "id": "401",
        "date": "2026-11-09T18:00Z",
        "competitions": [{
            "competitors": [
                {"homeAway": "home", "team": _team("Green Bay", "Packers", "GB")},
                {"homeAway": "away", "team": _team("Atlanta", "Falcons", "ATL")},
            ],
            "status": {"type": {"state": "pre", "completed": False, "description": "Scheduled", "shortDetail": "Sun 1:00 PM", "name": "STATUS_SCHEDULED"}},
            "odds": [CX["espn_odds_v2"]],
        }],
    }


def board(events=None):
    return EspnBoard(fetcher=lambda league, day: (events if events is not None else [gb_event()]) if day == "20261109" else [])


class FakeClient:
    host = "https://api.elections.kalshi.com"

    def __init__(self, markets, series):
        self.markets, self.series = markets, series
        self.calls = 0

    def get_market(self, ticker):
        self.calls += 1
        if ticker not in self.markets:
            raise RuntimeError("404 market not found")
        return {"market": self.markets[ticker]}

    def get_series(self, st):
        if st not in self.series:
            raise RuntimeError("404")
        return {"series": self.series[st]}


def all_client():
    ms = {RX[k]["ticker"]: RX[k] for k in ("weather_market", "crypto_market", "sports_market", "econ_market", "unknown_market")}
    ss = {RX[k]["ticker"]: RX[k] for k in ("weather_series", "crypto_series", "sports_series", "econ_series")}
    return FakeClient(ms, ss)


SECTIONS = ("settles_yes", "would_flip", "priced_in", "still_thin")


def _shape(b):
    assert set(b["sections"]) == set(SECTIONS)
    assert all(b["sections"][s] for s in SECTIONS)
    assert b["coverage"] in {"enough", "thin", "conflict"}
    assert b["needs_human_check"] == (b["coverage"] != "enough")
    assert b["generator"] == "structured"
    assert not R.has_forbidden_wording(b)


def test_sports_brief_uses_espn_and_cites_sources():
    b = R.build_brief(RX["sports_market"], RX["sports_series"], espn=board(), context=ctx(boom), api_base=FakeClient.host)
    _shape(b)
    assert b["category"] == "sports"
    espn = b["sources"][0]
    assert espn["ok"] and espn["name"] == "ESPN scoreboard" and 0.7 < espn["p_yes"] < 0.8
    assert b["coverage"] == "enough"  # ESPN ~75% vs book ~76.5%
    txt = " ".join(b["sections"]["priced_in"])
    assert "largely priced in" in txt and "Book implies YES" in txt
    assert "Green Bay wins" in b["sections"]["settles_yes"][0]
    urls = {c["url"] for c in b["citations"]}
    assert "https://www.nfl.com/" in urls
    assert any(u and u.startswith("https://site.api.espn.com/") and "dates=20261109" in u for u in urls)
    assert f"{FakeClient.host}/trade-api/v2/markets/KXNFLGAME-26NOV09ATLGB-GB" in urls


def test_sports_no_espn_game_is_thin_and_says_why():
    b = R.build_brief(RX["sports_market"], RX["sports_series"], espn=board(events=[]), context=ctx(boom))
    _shape(b)
    assert b["coverage"] == "thin" and b["needs_human_check"]
    assert any("No ESPN game found" in t for t in b["sections"]["still_thin"])


def test_sports_conflict_when_espn_far_from_book():
    m = {**RX["sports_market"], "yes_ask_dollars": "0.3000", "yes_bid_dollars": "0.2900", "no_ask_dollars": "0.7100"}
    b = R.build_brief(m, RX["sports_series"], espn=board(), context=ctx(boom))
    _shape(b)
    assert b["coverage"] == "conflict" and b["coverage_label"].endswith("needs human check")


def test_weather_brief_uses_nws_and_names_weather_company():
    b = R.build_brief(RX["weather_market"], RX["weather_series"], espn=board(), context=ctx())
    _shape(b)
    assert b["category"] == "weather"
    nws = b["sources"][0]
    assert nws["ok"] and nws["p_yes"] is not None and nws["station"] == "KNYC"
    assert "The Weather Company" in b["sections"]["settles_yes"][1]
    assert any("Weather Company figure can differ" in t for t in b["sections"]["would_flip"])
    urls = [c["url"] for c in b["citations"]]
    assert "https://weather.com/kalshi" in urls and any("forecast.weather.gov" in (u or "") for u in urls)
    # Kalshi's own fine print says "does not guarantee data..." — that sentence is dropped, not echoed.
    assert "guarantee" not in json.dumps(b).lower()


def test_weather_source_down_fails_soft_to_thin():
    b = R.build_brief(RX["weather_market"], RX["weather_series"], espn=board(), context=ctx(boom))
    _shape(b)
    assert b["coverage"] == "thin"
    assert any(t.startswith("NWS / NOAA forecast + observations unavailable") for t in b["sections"]["still_thin"])


def test_crypto_brief_uses_coinbase_and_cf_benchmarks():
    b = R.build_brief(RX["crypto_market"], RX["crypto_series"], espn=board(), context=ctx())
    _shape(b)
    assert b["category"] == "crypto" and b["sources"][0]["ok"]
    assert any("CF Benchmarks, not Coinbase spot" in t for t in b["sections"]["would_flip"])
    assert any("api.exchange.coinbase.com" in (c["url"] or "") for c in b["citations"])


@pytest.mark.parametrize("mkey,skey,cat", [("econ_market", "econ_series", "economics"), ("unknown_market", None, "other")])
def test_thin_for_categories_without_a_primary_adapter(mkey, skey, cat):
    b = R.build_brief(RX[mkey], RX[skey] if skey else None, espn=board(), context=ctx(boom))
    _shape(b)
    assert b["category"] == cat and b["coverage"] == "thin" and b["sources"] == []
    thin = " ".join(b["sections"]["still_thin"])
    assert "No news is pulled or invented" in thin
    if skey:
        assert "Federal Reserve" in thin and "not read automatically" in thin
        assert "https://www.federalreserve.gov" in [c["url"] for c in b["citations"]]
    else:
        assert "names no settlement source" in thin
        assert "No rules text" in b["sections"]["settles_yes"][0]


def test_cache_hit_then_refresh(tmp_path):
    led = Ledger(tmp_path / "r.db")
    cl = all_client()
    t = [1_000_000.0]
    kw = dict(client=cl, ledger=led, espn=board(), context=ctx(), clock=lambda: t[0], now=NOW)
    first = R.get_research("KXHIGHNY-26NOV09-T57", **kw)
    assert first["cached"] is False and cl.calls == 1 and first["ttl_s"] == R.RESEARCH_TTL_S == 1200
    t[0] += 300
    second = R.get_research("kxhighny-26nov09-t57", **kw)
    assert second["cached"] is True and cl.calls == 1 and second["cache_age_s"] == 300
    assert second["sections"] == first["sections"]
    third = R.get_research("KXHIGHNY-26NOV09-T57", refresh=True, **kw)
    assert third["cached"] is False and cl.calls == 2
    t[0] += R.RESEARCH_TTL_S + 1  # expired
    assert R.get_research("KXHIGHNY-26NOV09-T57", **kw)["cached"] is False and cl.calls == 3


def test_unknown_ticker_raises_lookup(tmp_path):
    with pytest.raises(LookupError):
        R.get_research("NOPE", client=all_client(), ledger=None, espn=board(), context=ctx(boom))


def test_series_lookup_failure_is_reported_not_fatal():
    cl = all_client()
    cl.series = {}
    b = R.get_research("KXBTCD-26NOV0902-T74999.99", client=cl, espn=board(), context=ctx())
    assert any("series lookup failed" in t for t in b["sections"]["still_thin"])


def test_clean_copy_strips_certainty_phrasing():
    assert R.clean_copy("Scoreboard win chance for GB is ~70%. Not a lock.") == "Scoreboard win chance for GB is ~70%."
    assert R.clean_copy("That is an estimate, not a lock.") == "That is an estimate."
    assert R.clean_copy("Kalshi settlement can still differ. Not a guaranteed win.") == "Kalshi settlement can still differ."
    assert not R.has_forbidden_wording(R.clean_copy("It is almost certain. Guaranteed almost. OK."))
    for bad in ("guaranteed", "a lock", "almost certain", "almost-certain", "guaranteed almost", "sure thing", "risk-free"):
        assert R.has_forbidden_wording(f"This is {bad}.")
    assert not R.has_forbidden_wording("BLOCKED in UT · unlock vault")


def test_research_copy_in_source_has_no_certainty_wording():
    py = pathlib.Path("app/strategy/research.py").read_text()
    body = py[py.index("COVERAGE_LABEL") :]  # skip the regex + docstring that define the banned words
    assert not R.FORBIDDEN_WORDING.search(body.replace("_SOFTEN", ""))
    js = pathlib.Path("static/js/app.js").read_text()
    box = js[js.index("// ---- research companion") : js.index("// ---- end research companion")]
    assert not R.FORBIDDEN_WORDING.search(box)
    assert "needs human check" in py


def test_scanner_poll_never_builds_research(monkeypatch):
    R.STATS["built"] = 0

    def explode(*a, **k):
        raise AssertionError("research must not run on a scan")

    monkeypatch.setattr(R, "build_brief", explode)
    monkeypatch.setattr(R, "get_research", explode)

    class Cl:
        host = "fake"

        def get_markets(self, **kw):
            return {"markets": [RX["weather_market"], RX["crypto_market"], RX["sports_market"]], "cursor": ""}

    res = scan_opportunities(Cl(), limit=5, espn=board(), include_game_series=False, context=ctx(), analog_pool=[])
    assert res["count"] >= 1 and R.STATS["built"] == 0
    assert all("research" not in r for r in res["markets"])
    # The 5 s poll in the UI calls runScan only; /api/research is fetched by loadResearch alone.
    js = pathlib.Path("static/js/app.js").read_text()
    assert js.count("/api/research/") == 1
    i = js.index("/api/research/")
    assert js.rfind("async function loadResearch", 0, i) > js.rfind("function runScan", 0, i)
    poll = [ln for ln in js.splitlines() if "setInterval" in ln and "runScan" in ln]
    assert poll and all("research" not in ln.lower() for ln in poll)


def test_api_research_endpoint_cache_and_geo_unchanged(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import app.main as main

    monkeypatch.setattr(main, "client", all_client())
    monkeypatch.setattr(main, "ledger", Ledger(tmp_path / "api.db"))
    monkeypatch.setattr(R, "get_board", lambda: board())
    monkeypatch.setattr(R, "get_context", lambda: ctx())
    http = TestClient(main.app)
    r = http.get("/api/research/KXNFLGAME-26NOV09ATLGB-GB")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] and d["cached"] is False and d["category"] == "sports"
    # Test client has no resolvable state: sports stay blocked (fail closed); research does not change that.
    assert d["geo"]["allowed"] is False
    assert not any(k in d for k in ("order", "orders", "buy", "allocation_usd", "contracts"))
    assert http.get("/api/research/KXNFLGAME-26NOV09ATLGB-GB").json()["cached"] is True
    assert http.get("/research/KXNFLGAME-26NOV09ATLGB-GB?refresh=1").json()["cached"] is False
    assert http.get("/api/research/NOPE-1").status_code == 404
    for state in ("UT", "MI", "NV", "OH", "NY", "AZ", "MD", "MT", None):
        assert geo.check("KXNFLGAME-26NOV09ATLGB-GB", state)["allowed"] is False
    assert not R.has_forbidden_wording(d)


def test_research_module_cannot_place_orders():
    src = pathlib.Path("app/strategy/research.py").read_text()
    for needle in ("place_order", "OrderGate", "app.trading", "auth_post", "create_order"):
        assert needle not in src

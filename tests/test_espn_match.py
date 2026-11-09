"""ESPN team/date matching and model_p source. No network."""
from datetime import date

from app.main import app
from app.strategy.espn import (
    EspnBoard,
    ProbEstimate,
    estimate_from_events,
    parse_kalshi_game,
    select_nearby_games,
)
from app.strategy.scanner import enrich_market, scan_opportunities, sort_scan_rows


def test_import_app():
    assert app.title


def _team(loc, nick, abbr):
    return {
        "location": loc,
        "name": nick,
        "abbreviation": abbr,
        "displayName": f"{loc} {nick}",
        "shortDisplayName": nick,
    }


def _comp(loc, nick, abbr, side, score=None, winner=None):
    return {
        "homeAway": side,
        "score": score,
        "winner": winner,
        "team": _team(loc, nick, abbr),
    }


def _event(when, home, away, *, state="pre", detail="", prob=None, spread=None, moneyline=None, name=None):
    situation = {}
    if prob:
        situation = {"lastPlay": {"probability": prob}}
    odds = []
    if spread is not None or moneyline:
        home_ml, away_ml = (moneyline or (None, None))
        odds = [{
            "details": "LINE",
            "spread": spread,
            "homeTeamOdds": {"moneyLine": home_ml} if home_ml is not None else {},
            "awayTeamOdds": {"moneyLine": away_ml} if away_ml is not None else {},
        }]
    status_name = name or ("STATUS_FINAL" if state == "post" else "STATUS_IN_PROGRESS" if state == "in" else "STATUS_SCHEDULED")
    return {
        "id": when + home["team"]["abbreviation"],
        "date": when,
        "competitions": [{
            "competitors": [home, away],
            "status": {
                "type": {
                    "state": state,
                    "completed": state == "post",
                    "description": detail or state,
                    "shortDetail": detail,
                    "name": status_name,
                }
            },
            "situation": situation,
            "odds": odds,
        }],
    }


def _market(ticker, rules, yes="0.3300", no="0.6800", **extra):
    row = {
        "ticker": ticker,
        "event_ticker": ticker.rsplit("-", 1)[0],
        "title": extra.pop("title", "Game"),
        "yes_sub_title": extra.pop("yes_sub_title", "Yes"),
        "no_sub_title": extra.pop("no_sub_title", "Yes"),
        "rules_primary": rules,
        "yes_ask_dollars": yes,
        "no_ask_dollars": no,
        "yes_bid_dollars": yes,
        "volume": 800,
        "open_interest": 800,
        "status": "open",
    }
    row.update(extra)
    return row


GB_RULES = (
    "If Green Bay wins the Atlanta vs Green Bay Pro Football game originally "
    "scheduled for Sep 24, 2026, then the market resolves to Yes."
)
NYJ_RULES = (
    "If New York J wins the NY Jets vs CHI Bears Pro Football game originally "
    "scheduled for Oct 4, 2026, then the market resolves to Yes."
)
NYY_RULES = (
    "If Tampa Bay wins the Tampa Bay vs New York Y professional baseball game originally "
    "scheduled for Sep 24, 2026 at 7:05 PM EDT, then the market resolves to Yes."
)
ND_RULES = (
    "If North Dakota wins the Murray St. vs North Dakota college football game originally "
    "scheduled for Oct 3, 2026, then the market resolves to Yes."
)


def test_parse_rules_and_ignore_props():
    parsed = parse_kalshi_game(_market("KXNFLGAME-26SEP24ATLGB-GB", GB_RULES))
    assert parsed is not None
    assert parsed.league == "nfl"
    assert parsed.yes_team == "Green Bay"
    assert parsed.team_a == "Atlanta"
    assert parsed.team_b == "Green Bay"
    assert parsed.scheduled == date(2026, 9, 24)

    jets = parse_kalshi_game(_market("KXNFLGAME-26OCT04NYJCHI-NYJ", NYJ_RULES))
    assert jets.yes_team == "New York J"
    assert jets.team_a == "NY Jets"
    assert jets.scheduled == date(2026, 10, 4)

    rays = parse_kalshi_game(_market("KXMLBGAME-26SEP241905TBNYY-TB", NYY_RULES))
    assert rays.team_b == "New York Y"
    assert rays.start_utc is not None
    assert rays.start_utc.hour == 23 and rays.start_utc.minute == 5

    nd = parse_kalshi_game(_market("KXNCAAFGAME-26OCT03MUSTND-ND", ND_RULES))
    assert nd.team_a == "Murray St."
    assert nd.league == "ncaaf"

    prop = _market(
        "KXNFLREC-26SEP24ATLGB-ATLBROBINSON7-2",
        GB_RULES,
    )
    assert parse_kalshi_game(prop) is None
    futures = _market("KXNBA-27-ATL", "If Atlanta wins the 2027 Pro Basketball Finals, then the market resolves to Yes.")
    assert parse_kalshi_game(futures) is None


def test_live_match_uses_win_probability_across_utc_date():
    # Kalshi says Sep 24. ESPN kickoff is 00:15Z on Sep 25 (8:15pm ET Sep 24).
    home = _comp("Green Bay", "Packers", "GB", "home", score="7")
    away = _comp("Atlanta", "Falcons", "ATL", "away", score="17")
    event = _event(
        "2026-09-25T00:15:00Z",
        home,
        away,
        state="in",
        detail="0:59 - 2nd",
        prob={"homeWinPercentage": 0.3594, "awayWinPercentage": 0.6406, "tiePercentage": 0.0},
    )
    est = estimate_from_events(_market("KXNFLGAME-26SEP24ATLGB-GB", GB_RULES), [event])
    assert est is not None
    assert est.source == "espn_live"
    assert abs(est.p_yes - 0.3594) < 1e-6
    assert "not a lock" in est.hint.lower()
    assert "100%" not in est.hint
    assert "guaranteed win" in est.hint.lower() or "not a lock" in est.hint.lower()

    # Same clubs a week earlier must not match.
    stale = _event("2026-09-18T00:15:00Z", home, away, state="post", detail="Final")
    assert estimate_from_events(_market("KXNFLGAME-26SEP24ATLGB-GB", GB_RULES), [stale]) is None


def test_final_and_abbreviation_discriminators():
    phi = _comp("Philadelphia", "Flyers", "PHI", "home", score="2", winner=True)
    bos = _comp("Boston", "Bruins", "BOS", "away", score="0", winner=False)
    final = _event("2026-09-25T03:00:00Z", phi, bos, state="post", detail="Final")
    rules = (
        "If Philadelphia wins the Boston vs Philadelphia NHL game originally "
        "scheduled for Sep 24, 2026, then the market resolves to Yes."
    )
    est = estimate_from_events(_market("KXNHLGAME-26SEP24BOSPHI-PHI", rules), [final])
    assert est.source == "espn_live"
    assert est.p_yes == 0.98
    assert "final" in est.hint.lower()
    assert "guaranteed" in est.hint.lower()

    chargers = _comp("Los Angeles", "Chargers", "LAC", "away", score="0")
    broncos = _comp("Denver", "Broncos", "DEN", "home", score="0")
    rams = _comp("Los Angeles", "Rams", "LAR", "away", score="0")
    lac_rules = (
        "If Los Angeles C wins the Los Angeles C vs Denver Pro Football game originally "
        "scheduled for Sep 27, 2026, then the market resolves to Yes."
    )
    market = _market("KXNFLGAME-26SEP27LACDEN-LAC", lac_rules)
    pre = _event("2026-09-27T17:00:00Z", broncos, chargers, state="pre", spread=3.5)
    assert estimate_from_events(market, [pre]).source == "espn_pre"
    assert estimate_from_events(market, [_event("2026-09-27T17:00:00Z", broncos, rams, state="pre", spread=-3.0)]) is None

    sox = _comp("Chicago", "White Sox", "CWS", "home")
    cubs = _comp("Chicago", "Cubs", "CHC", "home")
    reds = _comp("Cincinnati", "Reds", "CIN", "away")
    ws_rules = (
        "If Chicago WS wins the Chicago WS vs Cincinnati professional baseball game originally "
        "scheduled for Sep 25, 2026, then the market resolves to Yes."
    )
    ws_market = _market("KXMLBGAME-26SEP25CHWCIN-CHW", ws_rules)
    assert estimate_from_events(
        ws_market,
        [_event("2026-09-25T23:10:00Z", sox, reds, state="pre", spread=-1.5)],
    ).source == "espn_pre"
    assert estimate_from_events(
        ws_market,
        [_event("2026-09-25T23:10:00Z", cubs, reds, state="pre", spread=-1.5)],
    ) is None

    yankees = _comp("New York", "Yankees", "NYY", "away")
    mets = _comp("New York", "Mets", "NYM", "home")
    ambiguous = (
        "If New York wins the New York vs New York professional baseball game originally "
        "scheduled for Sep 25, 2026, then the market resolves to Yes."
    )
    assert estimate_from_events(
        _market("KXMLBGAME-26SEP25NYYNYM-NYY", ambiguous),
        [_event("2026-09-25T23:10:00Z", mets, yankees, state="pre", spread=-1.5)],
    ) is None


def test_end_of_ot_follows_the_score_when_it_is_not_tied():
    det = _comp("Detroit", "Red Wings", "DET", "home", score="3", winner=None)
    buf = _comp("Buffalo", "Sabres", "BUF", "away", score="2", winner=None)
    event = _event("2026-09-25T02:00:00Z", det, buf, state="in", detail="End of OT", name="STATUS_END_PERIOD")
    rules = (
        "If Detroit wins the Buffalo vs Detroit NHL game originally "
        "scheduled for Sep 24, 2026, then the market resolves to Yes."
    )
    est = estimate_from_events(_market("KXNHLGAME-26SEP24BUFDET-DET", rules), [event])
    assert est is not None
    assert est.source == "espn_live"
    assert est.p_yes == 0.98
    assert "not a lock" in est.hint.lower()
    assert "official final" in est.hint.lower()

    tied = _event(
        "2026-09-25T02:00:00Z",
        _comp("Detroit", "Red Wings", "DET", "home", score="2"),
        _comp("Buffalo", "Sabres", "BUF", "away", score="2"),
        state="in",
        detail="End of OT",
        name="STATUS_END_PERIOD",
    )
    tied_est = estimate_from_events(_market("KXNHLGAME-26SEP24BUFDET-DET", rules), [tied])
    assert tied_est is not None
    assert tied_est.p_yes == 0.5


def test_pregame_spread_moneyline_and_pickem_fallback():
    home = _comp("Buffalo", "Bills", "BUF", "home")
    away = _comp("Los Angeles", "Chargers", "LAC", "away")
    rules = (
        "If Buffalo wins the Los Angeles C vs Buffalo Pro Football game originally "
        "scheduled for Sep 27, 2026, then the market resolves to Yes."
    )
    market = _market("KXNFLGAME-26SEP27LACBUF-BUF", rules)
    spread_ev = _event("2026-09-27T17:00:00Z", home, away, state="pre", spread=-7.0, detail="Scheduled")
    est = estimate_from_events(market, [spread_ev])
    assert est.source == "espn_pre"
    assert est.p_yes > 0.7
    assert "pregame" in est.hint.lower()
    assert "not a lock" in est.hint.lower()

    pickem = _event("2026-09-27T17:00:00Z", home, away, state="pre", spread=0.0)
    assert estimate_from_events(market, [pickem]) is None

    ml = _event(
        "2026-09-27T17:00:00Z",
        home,
        away,
        state="pre",
        moneyline=(-150, 130),
    )
    ml_est = estimate_from_events(market, [ml])
    assert ml_est.source == "espn_pre"
    # -150 vs +130, vig removed, home is favorite but well below a sure thing.
    assert 0.55 < ml_est.p_yes < 0.65

    postponed = _event(
        "2026-09-27T17:00:00Z",
        home,
        away,
        state="pre",
        spread=-7,
        name="STATUS_POSTPONED",
        detail="Postponed",
    )
    assert estimate_from_events(market, [postponed]) is None


def test_doubleheader_uses_start_time_and_college_names():
    rays_h = _comp("Tampa Bay", "Rays", "TB", "home", score="1", winner=False)
    yanks = _comp("New York", "Yankees", "NYY", "away", score="4", winner=True)
    early = _event("2026-09-24T16:05:00Z", rays_h, yanks, state="post", detail="Final")
    late_home = _comp("Tampa Bay", "Rays", "TB", "home", score="6", winner=True)
    late_away = _comp("New York", "Yankees", "NYY", "away", score="2", winner=False)
    late = _event("2026-09-24T23:05:00Z", late_home, late_away, state="post", detail="Final")
    est = estimate_from_events(_market("KXMLBGAME-26SEP241905TBNYY-TB", NYY_RULES), [early, late])
    assert est.source == "espn_live"
    assert est.p_yes == 0.98

    murray = _comp("Murray State", "Racers", "MUR", "away")
    dakota = _comp("North Dakota", "Fighting Hawks", "UND", "home")
    ev = _event("2026-10-03T22:00:00Z", dakota, murray, state="in", detail="2nd")
    ev["competitions"][0]["competitors"][0]["score"] = "21"
    ev["competitions"][0]["competitors"][1]["score"] = "14"
    est = estimate_from_events(_market("KXNCAAFGAME-26OCT03MUSTND-ND", ND_RULES), [ev])
    assert est is not None
    assert est.source == "espn_live"
    # No ESPN win% — damped lead, not a blowout certainty.
    assert 0.55 < est.p_yes < 0.85


def test_select_nearby_keeps_a_mix_and_drops_far_games():
    today = date(2026, 9, 25)
    def row(ticker, rules):
        return _market(ticker, rules)

    near_nfl = row("KXNFLGAME-26SEP24ATLGB-GB", GB_RULES)
    far_nfl = row(
        "KXNFLGAME-26OCT20ATLGB-GB",
        GB_RULES.replace("Sep 24, 2026", "Oct 20, 2026"),
    )
    mlb = row("KXMLBGAME-26SEP241905TBNYY-TB", NYY_RULES)
    college = row("KXNCAAFGAME-26OCT03MUSTND-ND", ND_RULES)
    picked = select_nearby_games([far_nfl, college, near_nfl, mlb], today, per_league=10, limit=10)
    tickers = [m["ticker"] for m in picked]
    assert near_nfl["ticker"] in tickers
    assert mlb["ticker"] in tickers
    assert far_nfl["ticker"] not in tickers
    # Oct 3 is 8 days out.
    assert college["ticker"] not in tickers

    flood = []
    for i in range(5):
        flood.append(row(
            f"KXNFLGAME-26SEP25A{i}-A",
            f"If Atlanta wins the Atlanta vs Green Bay Pro Football game originally scheduled for Sep 25, 2026, then yes.",
        ))
        flood.append(row(
            f"KXNCAAFGAME-26SEP25B{i}-B",
            f"If North Dakota wins the Murray St. vs North Dakota college football game originally scheduled for Sep 25, 2026, then yes.",
        ))
    mixed = select_nearby_games(flood, today, per_league=2, limit=3)
    leagues = {m["ticker"].split("-")[0] for m in mixed}
    assert "KXNFLGAME" in leagues
    assert len(mixed) == 3


def test_enrich_uses_espn_or_shrink():
    market = _market("KXNFLGAME-26SEP24ATLGB-GB", GB_RULES, yes="0.4000", no="0.6200")
    live = ProbEstimate(
        p_yes=0.22,
        source="espn_live",
        hint="ESPN live NFL: GB 7, ATL 17. Scoreboard win chance for Green Bay is ~22%. Not a lock.",
        league="NFL",
        state="in",
        summary="GB 7, ATL 17",
        detail="Q2",
    )

    class Board:
        fetch_ok = 1
        fetch_errors = 0

        def estimate(self, _market):
            return live

    row = enrich_market(market, espn=Board())
    assert row["prob_source"] == "espn_live"
    assert row["model_p_source"] == "espn_live"
    assert row["sports_game"] is True
    assert row["espn"]["state"] == "in"
    # The logistic model is the probability; ESPN enters as espn_delta.
    assert abs(row["nash"]["model_prob_yes"] - row["model"]["p_yes"]) < 1e-4
    assert row["model"]["features"]["espn_delta"] < 0  # ESPN 22% vs book ~39%
    assert "espn_live" in row["explainer"]["why_odds"]
    assert "not a likely win" in row["explainer"]["why_odds"].lower()
    assert "not a lock" in row["explainer"]["edge_plain"].lower()
    assert "guaranteed" in row["explainer"]["risk_plain"].lower() or "not a likely" in row["explainer"]["risk_plain"].lower()

    class Empty:
        def estimate(self, _market):
            return None

    fallback = enrich_market(market, espn=Empty())
    assert fallback["prob_source"] == "market_shrink"
    assert fallback["model_p_source"] == "market_shrink"
    assert fallback["espn"] is None
    assert "market_shrink" in fallback["explainer"]["why_odds"]


def test_board_caches_primary_slate():
    calls = []
    home = _comp("Green Bay", "Packers", "GB", "home", score="7")
    away = _comp("Atlanta", "Falcons", "ATL", "away", score="17")
    event = _event(
        "2026-09-25T00:15:00Z",
        home,
        away,
        state="in",
        prob={"homeWinPercentage": 0.4, "awayWinPercentage": 0.6},
    )

    def fetcher(league, yyyymmdd):
        calls.append((league, yyyymmdd))
        if yyyymmdd == "20260924":
            return [event]
        return []

    board = EspnBoard(fetcher=fetcher, ttl=60)
    market = _market("KXNFLGAME-26SEP24ATLGB-GB", GB_RULES)
    first = board.estimate(market)
    second = board.estimate(market)
    assert first.source == "espn_live"
    assert second.p_yes == first.p_yes
    assert calls.count(("nfl", "20260924")) == 1


def test_scan_merges_game_series_without_network(monkeypatch):
    # Pin "today" so the Sep 24 fixture stays inside the nearby-games window.
    import app.strategy.scanner as scanner_mod

    monkeypatch.setattr(scanner_mod, "us_today", lambda: date(2026, 9, 24))
    nfl = _market("KXNFLGAME-26SEP24ATLGB-GB", GB_RULES, yes="0.2000", no="0.8200")
    cheap = {
        "ticker": "KXTEST-CHEAP",
        "title": "Will the cheap side hit?",
        "yes_sub_title": "cheap side hits",
        "no_sub_title": "cheap side misses",
        "yes_ask_dollars": 0.08,
        "no_ask_dollars": 0.93,
        "volume": 500,
        "open_interest": 500,
        "status": "open",
    }

    class Client:
        host = "https://example.test"

        def get_markets(self, **kwargs):
            return {"markets": [cheap], "cursor": None}

        def get_events(self, **kwargs):
            series = kwargs.get("series_ticker")
            if series == "KXNFLGAME":
                return {"events": [{"markets": [nfl]}], "cursor": None}
            return {"events": [], "cursor": None}

    class Board:
        fetch_ok = 0
        fetch_errors = 0

        def estimate(self, market):
            if str(market.get("ticker") or "").startswith("KXNFLGAME"):
                return ProbEstimate(
                    p_yes=0.3,
                    source="espn_live",
                    hint="ESPN live NFL: test. Not a lock.",
                    league="NFL",
                    state="in",
                    summary="GB 7, ATL 17",
                    detail="Q2",
                )
            return None

    result = scan_opportunities(client=Client(), limit=10, espn=Board(), bankroll=1000, sort="payout")
    by_ticker = {row["ticker"]: row for row in result["markets"]}
    assert by_ticker["KXNFLGAME-26SEP24ATLGB-GB"]["prob_source"] == "espn_live"
    assert by_ticker["KXTEST-CHEAP"]["prob_source"] == "market_shrink"
    assert result["espn"]["live"] == 1
    assert result["espn"]["matched"] == 1
    assert result["sort"] == "payout"
    assert "not a sure outcome" in result["disclaimer"]
    multiples = [row["best_payout_multiple"] for row in result["markets"]]
    assert multiples == sorted(multiples, reverse=True)
    # Payout order is independent of the ESPN tag.
    tagged = [dict(row) for row in result["markets"]]
    sort_scan_rows(tagged, "payout")
    assert [row["ticker"] for row in tagged] == [row["ticker"] for row in result["markets"]]

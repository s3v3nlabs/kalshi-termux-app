"""Public ESPN scoreboard context for full-game Kalshi markets.

No API key. NFL, NBA, MLB, NHL, NCAAF, and NCAAB scoreboards are fetched from
site.api.espn.com, cached briefly, and fuzzy-matched on team names plus the
scheduled date. A match sets model probability to ``espn_live`` (in progress
or final) or ``espn_pre`` (pregame line when it says something useful).
Anything else stays on the caller's market-shrink fallback.

These probabilities are estimates. A final scoreboard can still disagree with
Kalshi settlement, and a high payout multiple is not a likely win.
"""
from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard"
ESPN_CACHE_TTL_SECONDS = 60.0
ESPN_TIMEOUT_SECONDS = 8.0
NEARBY_DAYS = 3

# Point-spread → win probability. Home spread is negative when home is favored.
# 0.16 per point is a rough logistic (~ -7 ≈ 75%). It is not a book moneyline.
_SPREAD_K = 0.16
_ET: Optional[ZoneInfo] = None


def _eastern() -> ZoneInfo:
    """America/New_York, loaded on first use.

    Termux Python often has no system zoneinfo. PyPI ``tzdata`` (in
    requirements.txt) supplies the IANA database ``ZoneInfo`` needs.
    """
    global _ET
    if _ET is None:
        try:
            _ET = ZoneInfo("America/New_York")
        except ZoneInfoNotFoundError as exc:
            raise ZoneInfoNotFoundError(
                "No time zone found with key America/New_York. "
                "Install IANA tzdata: pip install tzdata"
            ) from exc
    return _ET


# Longer prefixes first so college series are not shadowed.
_SERIES: tuple[tuple[str, str], ...] = (
    ("KXNCAAFGAME", "ncaaf"),
    ("KXNCAABGAME", "ncaab"),
    ("KXNFLGAME", "nfl"),
    ("KXNBAGAME", "nba"),
    ("KXMLBGAME", "mlb"),
    ("KXNHLGAME", "nhl"),
)

LEAGUES: dict[str, dict[str, Any]] = {
    "nfl": {"sport": "football", "league": "nfl", "label": "NFL", "groups": (None,)},
    "nba": {"sport": "basketball", "league": "nba", "label": "NBA", "groups": (None,)},
    "mlb": {"sport": "baseball", "league": "mlb", "label": "MLB", "groups": (None,)},
    "nhl": {"sport": "hockey", "league": "nhl", "label": "NHL", "groups": (None,)},
    # FBS + FCS. The ungrouped board is only a featured slice.
    "ncaaf": {"sport": "football", "league": "college-football", "label": "NCAAF", "groups": ("80", "81")},
    "ncaab": {
        "sport": "basketball",
        "league": "mens-college-basketball",
        "label": "NCAAB",
        "groups": (None,),
    },
}

_LEAGUE_PRIORITY = {"nfl": 0, "nba": 1, "mlb": 2, "nhl": 3, "ncaaf": 4, "ncaab": 5}

_MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

# Leading token in Kalshi rules ("NY Jets", "CHI Bears") → ESPN location.
_CITY_ABBREV = {
    "ari": "arizona",
    "atl": "atlanta",
    "bal": "baltimore",
    "bos": "boston",
    "buf": "buffalo",
    "car": "carolina",
    "chi": "chicago",
    "cin": "cincinnati",
    "cle": "cleveland",
    "dal": "dallas",
    "den": "denver",
    "det": "detroit",
    "gb": "green bay",
    "hou": "houston",
    "ind": "indianapolis",
    "jax": "jacksonville",
    "kc": "kansas city",
    "la": "los angeles",
    "lv": "las vegas",
    "mia": "miami",
    "min": "minnesota",
    "ne": "new england",
    "no": "new orleans",
    "ny": "new york",
    "phi": "philadelphia",
    "pit": "pittsburgh",
    "sea": "seattle",
    "sf": "san francisco",
    "stl": "st louis",
    "tb": "tampa bay",
    "ten": "tennessee",
    "was": "washington",
    "wsh": "washington",
}

_TZ_HOURS = {
    "edt": -4, "est": -5, "et": -4,
    "cdt": -5, "cst": -6, "ct": -5,
    "mdt": -6, "mst": -7, "mt": -6,
    "pdt": -7, "pst": -8, "pt": -7,
}

_SPORT_PHRASE = (
    r"(?:Pro Football|Pro Basketball|professional baseball|"
    r"college football|college basketball|NHL|NBA|MLB|NFL|NCAAF|NCAAB)"
)
_RULE_RE = re.compile(
    rf"If (?P<yes>.+?) wins the (?P<a>.+?) vs (?P<b>.+?) {_SPORT_PHRASE} game "
    r"originally scheduled for "
    r"(?P<mon>[A-Za-z]+) (?P<day>\d{1,2}), (?P<year>\d{4})"
    r"(?: at (?P<hh>\d{1,2}):(?P<mm>\d{2})\s*(?P<ampm>AM|PM)\s*(?P<tz>[A-Za-z]{2,4}))?",
    re.IGNORECASE,
)
_TICKER_DATE_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})")


@dataclass(frozen=True)
class ParsedGame:
    league: str
    yes_team: str
    team_a: str
    team_b: str
    scheduled: date
    start_utc: Optional[datetime] = None


@dataclass(frozen=True)
class ProbEstimate:
    """YES-side probability for the matched team. Never 0 or 1."""

    p_yes: float
    source: str  # espn_live | espn_pre
    hint: str
    league: str
    state: str
    summary: str
    detail: str

    def as_espn(self) -> dict[str, str]:
        return {
            "league": self.league,
            "state": self.state,
            "summary": self.summary,
            "detail": self.detail,
        }


def league_of(market: dict) -> Optional[str]:
    """Return nfl/nba/mlb/nhl/ncaaf/ncaab when the ticker is a full-game series."""
    for field in ("ticker", "event_ticker"):
        val = str(market.get(field) or "")
        for prefix, league in _SERIES:
            if val.startswith(prefix):
                return league
    return None


def is_full_game_market(market: dict) -> bool:
    return league_of(market) is not None


def us_today() -> date:
    return datetime.now(_eastern()).date()


def parse_kalshi_game(market: dict) -> Optional[ParsedGame]:
    """Pull league, both clubs, the YES club, and the scheduled date.

    Full-game winner markets look like ``Green Bay wins`` with rules
    ``If Green Bay wins the Atlanta vs Green Bay Pro Football game originally
    scheduled for Sep 24, 2026``. Props and season-long futures return None.
    """
    league = league_of(market)
    if not league:
        return None
    rules = market.get("rules_primary") or ""
    m = _RULE_RE.search(rules)
    if not m:
        return None
    month = _MONTHS.get(m.group("mon").lower())
    if not month:
        return None
    try:
        scheduled = date(int(m.group("year")), month, int(m.group("day")))
    except ValueError:
        return None
    start = _start_utc(scheduled, m.group("hh"), m.group("mm"), m.group("ampm"), m.group("tz"))
    yes = (m.group("yes") or "").strip()
    if not yes:
        yes = (market.get("yes_sub_title") or "").strip()
    team_a = (m.group("a") or "").strip()
    team_b = (m.group("b") or "").strip()
    if not yes or not team_a or not team_b:
        return None
    return ParsedGame(
        league=league,
        yes_team=yes,
        team_a=team_a,
        team_b=team_b,
        scheduled=scheduled,
        start_utc=start,
    )


def select_nearby_games(
    markets: list[dict],
    today: date,
    *,
    window_days: int = NEARBY_DAYS,
    per_league: int = 40,
    limit: int = 160,
) -> list[dict]:
    """Keep full-game markets scheduled near ``today``, spread across leagues."""
    buckets: dict[str, list[tuple]] = {k: [] for k in LEAGUES}
    for market in markets:
        parsed = parse_kalshi_game(market)
        if not parsed:
            continue
        delta = abs((parsed.scheduled - today).days)
        if delta > window_days:
            continue
        buckets[parsed.league].append((delta, market.get("ticker") or "", market))
    picked: list[tuple] = []
    for league, rows in buckets.items():
        rows.sort(key=lambda row: row[:2])
        for delta, ticker, market in rows[:per_league]:
            picked.append((_LEAGUE_PRIORITY[league], delta, ticker, market))
    picked.sort(key=lambda row: (row[1], row[0], row[2]))
    return [row[3] for row in picked[:limit]]


def estimate_from_events(market: dict, events: list[dict]) -> Optional[ProbEstimate]:
    """Match ``market`` against already-fetched scoreboard events. No network."""
    parsed = parse_kalshi_game(market)
    if not parsed:
        return None
    matched = match_game(parsed, events)
    if not matched:
        return None
    return probability_from_match(parsed, matched)


def match_game(parsed: ParsedGame, events: list[dict]) -> Optional[dict]:
    best: Optional[dict] = None
    best_key: Optional[tuple] = None
    for event in events:
        got = _try_event(parsed, event)
        if not got:
            continue
        key = (got["assign_score"], got["date_rank"], -got["time_dist"])
        if best_key is None or key > best_key:
            best_key = key
            best = got
    return best


def probability_from_match(parsed: ParsedGame, matched: dict) -> Optional[ProbEstimate]:
    comp = matched["comp"]
    status = ((comp.get("status") or {}).get("type") or {})
    state = (status.get("state") or "").lower()
    name = (status.get("name") or "").upper()
    desc = (status.get("description") or "").lower()
    if "POSTPON" in name or "CANCEL" in name or "postpon" in desc:
        return None
    if state not in {"pre", "in", "post"}:
        return None

    yes_c = matched["yes"]
    opp_c = matched["opp"]
    side = (yes_c.get("homeAway") or "").lower()
    detail = status.get("shortDetail") or status.get("description") or ""
    label = LEAGUES.get(parsed.league, {}).get("label", parsed.league.upper())

    if state in {"in", "post"}:
        official_final = state == "post" or _is_final(comp)
        ot_decided = _overtime_decided(comp, yes_c, opp_c)
        if official_final or ot_decided:
            p = _final_probability(yes_c, opp_c)
            phase = "post" if official_final else "ot"
        else:
            p = _live_probability(comp, yes_c, opp_c, side, state)
            phase = "in"
        if p is None:
            return None
        p = _clamp(p)
        summary = _score_summary(yes_c, opp_c)
        hint = _live_hint(parsed, phase, summary, detail, p)
        return ProbEstimate(
            p_yes=round(p, 4),
            source="espn_live",
            hint=hint,
            league=label,
            state="post" if phase == "post" else "in",
            summary=summary,
            detail=detail,
        )

    p = _pregame_probability(comp, side)
    if p is None:
        return None
    p = _clamp(p)
    odds = _first_odds(comp) or {}
    line = odds.get("details") or ""
    summary = f"pregame {line}".strip()
    hint = (
        f"ESPN pregame {label}"
        + (f" ({line})" if line else "")
        + f": rough win chance for {parsed.yes_team} is ~{p:.0%}. "
        "That is a pregame line, not a live result, and not a lock."
    )
    return ProbEstimate(
        p_yes=round(p, 4),
        source="espn_pre",
        hint=hint,
        league=label,
        state="pre",
        summary=summary,
        detail=detail or "Scheduled",
    )


class EspnBoard:
    """Short-TTL cache in front of the public scoreboard JSON."""

    def __init__(
        self,
        fetcher=None,
        ttl: float = ESPN_CACHE_TTL_SECONDS,
        session: Optional[requests.Session] = None,
    ):
        self.ttl = ttl
        self._fetcher = fetcher
        self._session = session
        self._cache: dict[tuple[str, str], tuple[float, list]] = {}
        self._lock = threading.Lock()
        self.fetch_ok = 0
        self.fetch_errors = 0

    def estimate(self, market: dict) -> Optional[ProbEstimate]:
        parsed = parse_kalshi_game(market)
        if parsed is None:
            return None
        primary = self._events(parsed.league, parsed.scheduled)
        matched = match_game(parsed, primary)
        if matched is None:
            extra: list[dict] = []
            for delta in (-1, 1):
                extra.extend(self._events(parsed.league, parsed.scheduled + timedelta(days=delta)))
            matched = match_game(parsed, extra)
        if matched is None:
            return None
        return probability_from_match(parsed, matched)

    def _events(self, league: str, day: date) -> list[dict]:
        key = (league, day.strftime("%Y%m%d"))
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and now - hit[0] < self.ttl:
                return hit[1]
        try:
            events = self._load(league, key[1])
            self.fetch_ok += 1
        except Exception:
            self.fetch_errors += 1
            events = []
        with self._lock:
            self._cache[key] = (time.monotonic(), events)
        return events

    def _load(self, league: str, yyyymmdd: str) -> list[dict]:
        if self._fetcher is not None:
            return list(self._fetcher(league, yyyymmdd) or [])
        return fetch_scoreboard(league, yyyymmdd, session=self._session)


_default_board: Optional[EspnBoard] = None
_default_lock = threading.Lock()


def get_board() -> EspnBoard:
    global _default_board
    with _default_lock:
        if _default_board is None:
            _default_board = EspnBoard()
        return _default_board


def fetch_scoreboard(
    league: str,
    yyyymmdd: str,
    session: Optional[requests.Session] = None,
) -> list[dict]:
    """GET one slate. College football merges FBS and FCS. No API key."""
    meta = LEAGUES[league]
    url = ESPN_SCOREBOARD.format(sport=meta["sport"], league=meta["league"])
    sess = session or requests
    # "scanner" in the User-Agent is rejected with 403. A plain client name is enough.
    headers = {"User-Agent": "kalshi-termux/1.0", "Accept": "application/json"}
    events: list[dict] = []
    seen: set[str] = set()
    last_error: Optional[Exception] = None
    for group in meta["groups"]:
        params: dict[str, Any] = {"dates": yyyymmdd, "limit": 400}
        if group:
            params["groups"] = group
        try:
            resp = sess.get(url, params=params, headers=headers, timeout=ESPN_TIMEOUT_SECONDS)
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            continue
        for event in payload.get("events") or []:
            eid = str(event.get("id") or "")
            if eid and eid in seen:
                continue
            if eid:
                seen.add(eid)
            events.append(event)
    if not events and last_error is not None:
        raise last_error
    return events


def _start_utc(scheduled: date, hh, mm, ampm, tz) -> Optional[datetime]:
    if not (hh and mm and ampm and tz):
        return None
    offset = _TZ_HOURS.get(tz.lower())
    if offset is None:
        return None
    hour = int(hh) % 12
    if ampm.lower() == "pm":
        hour += 12
    local = datetime(
        scheduled.year,
        scheduled.month,
        scheduled.day,
        hour,
        int(mm),
        tzinfo=timezone(timedelta(hours=offset)),
    )
    return local.astimezone(timezone.utc)


def _try_event(parsed: ParsedGame, event: dict) -> Optional[dict]:
    date_rank, time_dist = _date_rank(parsed, event)
    if date_rank <= 0:
        return None
    comps = _competitors(event)
    if len(comps) < 2:
        return None
    assign = _assign_teams(parsed, comps)
    if assign is None:
        return None
    yes_c, opp_c, assign_score = assign
    return {
        "assign_score": assign_score,
        "date_rank": date_rank,
        "time_dist": time_dist,
        "event": event,
        "comp": (event.get("competitions") or [{}])[0],
        "yes": yes_c,
        "opp": opp_c,
    }


def _competitors(event: dict) -> list[dict]:
    comp = (event.get("competitions") or [{}])[0]
    return list(comp.get("competitors") or [])


def _date_rank(parsed: ParsedGame, event: dict) -> tuple[int, float]:
    raw = event.get("date")
    if not raw:
        return 0, 1e9
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return 0, 1e9
    eastern = dt.astimezone(_eastern()).date()
    utc_day = dt.astimezone(timezone.utc).date()
    if parsed.scheduled == eastern:
        rank = 2
    elif abs((parsed.scheduled - eastern).days) <= 1 or abs((parsed.scheduled - utc_day).days) <= 1:
        rank = 1
    else:
        return 0, 1e9
    if parsed.start_utc is None:
        return rank, 0.0
    return rank, abs((dt - parsed.start_utc).total_seconds())


def _assign_teams(parsed: ParsedGame, comps: list[dict]) -> Optional[tuple[dict, dict, float]]:
    """Map the two Kalshi clubs onto the two ESPN competitors, then the YES club."""
    pair = comps[:2]
    scores_a = [score_team(parsed.team_a, c.get("team") or {}) for c in pair]
    scores_b = [score_team(parsed.team_b, c.get("team") or {}) for c in pair]
    straight = scores_a[0] + scores_b[1]
    crossed = scores_a[1] + scores_b[0]
    if straight >= crossed:
        mapping = (0, 1, straight)
    else:
        mapping = (1, 0, crossed)
    ia, ib, total = mapping
    if scores_a[ia] < 0.75 or scores_b[ib] < 0.75 or total < 1.6:
        return None
    yes_scores = [score_team(parsed.yes_team, c.get("team") or {}) for c in pair]
    yes_i = 0 if yes_scores[0] >= yes_scores[1] else 1
    if yes_scores[yes_i] < 0.75:
        return None
    other = yes_scores[1 - yes_i]
    if other >= 0.75 and yes_scores[yes_i] - other < 0.12:
        return None  # shared city, no discriminator
    opp_i = 1 - yes_i
    return pair[yes_i], pair[opp_i], total


def score_team(label: str, team: dict) -> float:
    """1.0 is an exact club match. Trailing letters split LA C / LA R and NY Y / NY M."""
    base, disc = _split_discriminator(label)
    if not base:
        return 0.0
    base = _expand_city(base)
    if base in {"as", "a's"}:
        base = "athletics"
    nickname = _norm(team.get("name") or "")
    abbr = _norm(team.get("abbreviation") or "").replace(" ", "")
    if disc and not _disc_ok(disc, nickname, abbr):
        return 0.0

    location = _norm(team.get("location") or "")
    display = _norm(team.get("displayName") or "")
    short = _norm(team.get("shortDisplayName") or "")
    full = _norm(f"{team.get('location') or ''} {team.get('name') or ''}")
    candidates = [display, short, location, nickname, abbr, full]
    best = 0.0
    q_tokens = set(base.split())
    for cand in candidates:
        if not cand:
            continue
        if base == cand:
            best = max(best, 1.0)
            continue
        c_tokens = set(cand.split())
        if len(q_tokens) >= 2 and q_tokens <= c_tokens:
            best = max(best, 0.95)
            continue
        if len(c_tokens) >= 2 and c_tokens <= q_tokens:
            best = max(best, 0.9)
        elif q_tokens and c_tokens:
            inter = len(q_tokens & c_tokens)
            if inter and len(q_tokens) >= 2:
                best = max(best, inter / len(q_tokens | c_tokens))
    if abbr and base.replace(" ", "") == abbr:
        best = 1.0
    if base == "athletics" and (abbr in {"ath", "oak"} or "athletic" in display or "athletic" in location):
        best = max(best, 0.95)
    return best


def _split_discriminator(label: str) -> tuple[str, str]:
    raw = (label or "").strip()
    if re.search(r"\([A-Za-z]", raw):
        return _norm(raw), ""
    m = re.match(r"^(.*\S)\s+([A-Za-z]{1,2})$", raw)
    if not m:
        return _norm(raw), ""
    return _norm(m.group(1)), m.group(2).lower()


def _expand_city(base: str) -> str:
    tokens = base.split()
    if len(tokens) >= 2 and tokens[0] in _CITY_ABBREV:
        return " ".join(_CITY_ABBREV[tokens[0]].split() + tokens[1:])
    return base


def _disc_ok(disc: str, nickname: str, abbr: str) -> bool:
    words = [w for w in nickname.split() if w]
    initials = "".join(w[0] for w in words)
    if len(disc) == 1:
        if nickname.startswith(disc):
            return True
        return bool(abbr) and abbr.endswith(disc)
    if initials.startswith(disc) or disc in initials:
        return True
    return bool(abbr) and abbr.endswith(disc)


def _norm(text: str) -> str:
    s = (text or "").lower().replace("\u2019", "").replace("'", "").replace(".", " ")
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    parts = s.split()
    if len(parts) >= 2 and parts[-1] == "st" and parts[0] != "st":
        parts[-1] = "state"
    if parts and parts[0] == "st":
        parts[0] = "saint"
    return " ".join(parts)


def _live_probability(comp: dict, yes_c: dict, opp_c: dict, side: str, state: str) -> Optional[float]:
    if state == "post" or _is_final(comp):
        decided = _final_probability(yes_c, opp_c)
        if decided is not None:
            return decided
    win_p = _win_pct(comp, side)
    if win_p is not None and state in {"in", "post"}:
        return win_p
    if state == "post":
        return _final_probability(yes_c, opp_c)
    if state == "in":
        return _score_heuristic(yes_c, opp_c)
    return None


def _overtime_decided(comp: dict, yes_c: dict, opp_c: dict) -> bool:
    """Sudden-death OT/SO with a leader is over even if ESPN still says in progress.

    A tied 'End of OT' can still be heading to a shootout, so that stays live.
    """
    status = (comp.get("status") or {}).get("type") or {}
    detail = f"{status.get('shortDetail') or ''} {status.get('detail') or ''}".lower()
    if not any(token in detail for token in ("end of ot", "end of so", "final/ot", "final/so")):
        return False
    ys, os_ = _score_num(yes_c), _score_num(opp_c)
    return ys is not None and os_ is not None and ys != os_


def _is_final(comp: dict) -> bool:
    status = (comp.get("status") or {}).get("type") or {}
    if status.get("completed") is True:
        return True
    name = (status.get("name") or "").upper()
    return "FINAL" in name and "SCHEDULED" not in name


def _final_probability(yes_c: dict, opp_c: dict) -> Optional[float]:
    winner = yes_c.get("winner")
    ys, os_ = _score_num(yes_c), _score_num(opp_c)
    if winner is True:
        return 0.98
    if winner is False:
        if ys is not None and os_ is not None and ys == os_:
            return 0.50
        return 0.02
    if ys is None or os_ is None:
        return None
    if ys > os_:
        return 0.98
    if ys < os_:
        return 0.02
    return 0.50


def _win_pct(comp: dict, side: str) -> Optional[float]:
    prob = ((comp.get("situation") or {}).get("lastPlay") or {}).get("probability") or {}
    if not isinstance(prob, dict):
        return None
    key = "homeWinPercentage" if side == "home" else "awayWinPercentage"
    raw = prob.get(key)
    if raw is None:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if val > 1.0:
        val = val / 100.0
    if val < 0.0 or val > 1.0:
        return None
    return val


def _score_heuristic(yes_c: dict, opp_c: dict) -> Optional[float]:
    ys, os_ = _score_num(yes_c), _score_num(opp_c)
    if ys is None or os_ is None:
        return None
    diff = ys - os_
    # Damped: a lead moves the estimate, it does not decide the game.
    shift = max(-0.30, min(0.30, diff * 0.015))
    return 0.5 + shift


def _pregame_probability(comp: dict, side: str) -> Optional[float]:
    odds = _first_odds(comp)
    if not odds or side not in {"home", "away"}:
        return None
    home_ml = _moneyline(odds.get("homeTeamOdds") or {})
    away_ml = _moneyline(odds.get("awayTeamOdds") or {})
    if home_ml is not None and away_ml is not None:
        ph = _american_implied(home_ml)
        pa = _american_implied(away_ml)
        total = ph + pa
        if total > 0:
            ph, pa = ph / total, pa / total
            return ph if side == "home" else pa
    spread = odds.get("spread")
    try:
        spread_f = float(spread) if spread is not None else None
    except (TypeError, ValueError):
        spread_f = None
    if spread_f is None or abs(spread_f) < 0.5:
        return None
    p_home = 1.0 / (1.0 + math.exp(_SPREAD_K * spread_f))
    if abs(p_home - 0.5) < 0.04:
        return None
    return p_home if side == "home" else 1.0 - p_home


def _first_odds(comp: dict) -> Optional[dict]:
    odds = comp.get("odds") or []
    if not odds:
        return None
    return odds[0] if isinstance(odds[0], dict) else None


def _moneyline(side_odds: dict) -> Optional[float]:
    for key in ("moneyLine", "moneyline", "ml"):
        if side_odds.get(key) is None:
            continue
        try:
            val = float(side_odds[key])
        except (TypeError, ValueError):
            continue
        if val == 0:
            continue
        return val
    return None


def _american_implied(ml: float) -> float:
    if ml < 0:
        return (-ml) / ((-ml) + 100.0)
    return 100.0 / (ml + 100.0)


def _score_num(comp: dict) -> Optional[float]:
    raw = comp.get("score")
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _score_summary(yes_c: dict, opp_c: dict) -> str:
    yname = ((yes_c.get("team") or {}).get("abbreviation")) or "YES"
    oname = ((opp_c.get("team") or {}).get("abbreviation")) or "OPP"
    ys, os_ = yes_c.get("score"), opp_c.get("score")
    if ys is None or os_ is None or ys == "" or os_ == "":
        return f"{yname} vs {oname}"
    return f"{yname} {ys}, {oname} {os_}"


def _live_hint(parsed: ParsedGame, state: str, summary: str, detail: str, p: float) -> str:
    label = LEAGUES.get(parsed.league, {}).get("label", parsed.league.upper())
    clock = f" ({detail})" if detail else ""
    if state == "post":
        return (
            f"ESPN final {label}{clock}: {summary}. "
            f"Model follows that scoreboard (~{p:.0%} for {parsed.yes_team} to win). "
            "Kalshi settlement can still differ. Not a guaranteed win."
        )
    if state == "ot":
        return (
            f"ESPN {label}{clock}: {summary}. "
            f"Overtime/shootout is over on the scoreboard, so the model follows that score "
            f"(~{p:.0%} for {parsed.yes_team}). Not an official final yet, and not a lock."
        )
    return (
        f"ESPN live {label}{clock}: {summary}. "
        f"Scoreboard win chance for {parsed.yes_team} is ~{p:.0%}. Not a lock."
    )


def _clamp(p: float) -> float:
    return max(0.02, min(0.98, float(p)))

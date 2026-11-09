"""Per-market research brief for the scanner's expanded row.

On demand only: ``GET /api/research/{ticker}`` builds a brief when the operator
taps Research on an expanded row. The 5-second scanner poll never calls this
module (``STATS["built"]`` stays 0 during scans; a test pins that).

What goes in
------------
Primary / official sources first, reusing the adapters already in the app:

* Always: the public Kalshi market (title, ticker, close time, ``rules_primary``,
  ``rules_secondary``) and its series (category, ``settlement_sources``,
  ``contract_url``). Public reads, no keys.
* Sports full-game markets: ESPN scoreboard (``app.strategy.espn``).
* Daily temperature markets: NWS hourly forecast + station observations
  (``app.strategy.context.WeatherAdapter``). NWS is context; the rules name the
  settlement source (currently The Weather Company), which is cited, not read.
* Crypto strike markets: Coinbase public candles (spot + realized vol).
* Politics / economics / everything else: no news is invented. The named
  settlement source is cited with its URL; its content is not read
  automatically, so the brief is marked thin ("needs human check").

The brief is a deterministic template over those facts: what settles YES, what
would flip it, what looks already priced in, what still looks thin, plus
citations and a coverage flag (``enough`` / ``thin`` / ``conflict``). No cloud
LLM. No local LLM is configured in this app, so there is no rewrite step.

Cache: one row per ticker in the ledger table ``research_cache`` for
``RESEARCH_TTL_S`` (20 minutes). ``refresh=True`` rebuilds. A source that is
down fails soft: the section says so and the coverage drops to thin.

This module never places, sizes, or enables orders, and it does not touch the
geo gate. The API attaches the caller's geo result for display only.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from app.strategy.context import (
    COINBASE_CANDLES,
    ContextAdapters,
    get_context,
    parse_crypto_market,
    parse_weather_market,
    strike_of,
)
from app.strategy.espn import (
    ESPN_SCOREBOARD,
    LEAGUES,
    EspnBoard,
    get_board,
    is_full_game_market,
    parse_kalshi_game,
)

RESEARCH_TTL_S = 20 * 60
CONFLICT_PP = 15.0  # an outside source this far from the book = "conflict"
THIN_VOLUME = 500.0
WIDE_SPREAD_C = 5.0
EXCERPT_CHARS = 420

STATS = {"built": 0, "cache_hits": 0}

# Copy that must never appear in a brief (a test enforces this on every brief).
FORBIDDEN_WORDING = re.compile(
    r"guarantee|\blocks?\b|almost[\s-]*certain|sure[\s-]+thing|can'?t\s+lose|risk[\s-]*free|guaranteed\s+almost",
    re.I,
)
_SOFTEN = re.compile(r",?\s*(and\s+)?not\s+an?\s+(lock|guaranteed\s+win)\b\.?", re.I)

COVERAGE_LABEL = {
    "enough": "Enough sources — still your call",
    "thin": "Thin — needs human check",
    "conflict": "Sources disagree — needs human check",
}


# ---------------- wording ----------------


def clean_copy(text: Any) -> str:
    """Drop certainty phrasing from quoted source text (ESPN hints, Kalshi
    rules boilerplate). Sentences that still carry a forbidden word are removed."""
    s = _SOFTEN.sub(".", str(text or "")).replace("..", ".")
    parts = re.split(r"(?<=[.!?])\s+", s.strip())
    return " ".join(p for p in parts if p and not FORBIDDEN_WORDING.search(p)).strip()


def _scrub(obj: Any) -> Any:
    if isinstance(obj, str):
        return obj if obj.startswith("http") else clean_copy(obj) if FORBIDDEN_WORDING.search(obj) else obj
    if isinstance(obj, list):
        return [_scrub(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items()}
    return obj


def has_forbidden_wording(obj: Any) -> bool:
    if isinstance(obj, str):
        return not obj.startswith("http") and bool(FORBIDDEN_WORDING.search(obj))
    if isinstance(obj, list):
        return any(has_forbidden_wording(x) for x in obj)
    if isinstance(obj, dict):
        return any(has_forbidden_wording(v) for v in obj.values())
    return False


# ---------------- helpers ----------------


def _f(v: Any) -> Optional[float]:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def _excerpt(text: Any, n: int = EXCERPT_CHARS) -> str:
    s = clean_copy(re.sub(r"\s+", " ", str(text or "")).strip())
    return s if len(s) <= n else s[: n - 1].rsplit(" ", 1)[0] + "…"


def _pct(p: Optional[float]) -> str:
    return "—" if p is None else f"{p * 100:.0f}%"


def _series_ticker(market: dict) -> str:
    return str(market.get("event_ticker") or market.get("ticker") or "").split("-")[0].upper()


def market_quotes(market: dict) -> dict:
    ya, na = _f(market.get("yes_ask_dollars")), _f(market.get("no_ask_dollars"))
    yb = _f(market.get("yes_bid_dollars"))
    if ya is None and _f(market.get("yes_ask")) is not None:
        ya = _f(market.get("yes_ask")) / 100.0
    if na is None and _f(market.get("no_ask")) is not None:
        na = _f(market.get("no_ask")) / 100.0
    ya = ya if ya and 0 < ya < 1 else None
    na = na if na and 0 < na < 1 else None
    p = None
    if ya is not None and na is not None:
        p = max(0.01, min(0.99, (ya + (1 - na)) / 2))
    elif ya is not None:
        p = ya
    elif na is not None:
        p = 1 - na
    spread = None
    if ya is not None and yb is not None:
        spread = round(abs(ya - yb) * 100, 1)
    elif ya is not None and na is not None:
        spread = round(abs(ya + na - 1) * 100, 1)
    vol = _f(market.get("volume_fp")) or _f(market.get("volume")) or 0.0
    oi = _f(market.get("open_interest_fp")) or _f(market.get("open_interest")) or 0.0
    return {"yes_ask": ya, "no_ask": na, "market_p_yes": None if p is None else round(p, 4), "spread_cents": spread, "volume": vol, "open_interest": oi}


def category_of(market: dict, series: Optional[dict]) -> str:
    if is_full_game_market(market):
        return "sports"
    if parse_weather_market(market) is not None:
        return "weather"
    if parse_crypto_market(market) is not None:
        return "crypto"
    cat = str((series or {}).get("category") or market.get("category") or "").lower()
    if "sport" in cat:
        return "sports"
    if "weather" in cat or "climate" in cat:
        return "weather"
    if "crypto" in cat:
        return "crypto"
    if "politic" in cat or "election" in cat:
        return "politics"
    if "econ" in cat or "financ" in cat:
        return "economics"
    return "other"


def _strike_text(market: dict) -> str:
    s = strike_of(market)
    if not s:
        return ""
    if s["type"] == "greater":
        return f"above {s['floor']:g}"
    if s["type"] == "less":
        return f"below {s['cap']:g}"
    return f"between {s['floor']:g} and {s['cap']:g}"


# ---------------- adapters (fail soft) ----------------


def _sports(market: dict, board: EspnBoard) -> dict:
    out: dict = {"name": "ESPN scoreboard", "ok": False}
    parsed = parse_kalshi_game(market)
    if parsed is not None:
        meta = LEAGUES.get(parsed.league, {})
        if meta:
            out["url"] = ESPN_SCOREBOARD.format(sport=meta["sport"], league=meta["league"]) + f"?dates={parsed.scheduled:%Y%m%d}"
        out["game"] = f"{parsed.team_a} vs {parsed.team_b} ({parsed.scheduled.isoformat()}), YES = {parsed.yes_team}"
        out["yes_team"] = parsed.yes_team
    try:
        est = board.estimate(market)
    except Exception as exc:
        est = None
        out["error"] = f"ESPN request failed ({type(exc).__name__})"
    if est is not None:
        out.update(ok=True, p_yes=est.p_yes, state=est.state, summary=est.summary, detail=est.detail,
                   source=est.source, hint=clean_copy(est.hint))
    elif "error" not in out:
        try:
            status = board.match_status(market)
        except Exception:
            status = "error"
        out["status"] = status
        out["error"] = {
            "unparsed": "Could not read the two teams and date from the Kalshi rules",
            "no_game": "No ESPN game found on that date (±1 day)",
            "matched_no_line": "ESPN game found, but no moneyline or usable spread yet",
        }.get(status, "ESPN unavailable")
    return out


def _weather(market: dict, ctx: ContextAdapters) -> dict:
    w = parse_weather_market(market)
    out: dict = {"name": "NWS / NOAA forecast + observations", "ok": False}
    if w is not None:
        out["url"] = f"https://forecast.weather.gov/MapClick.php?lat={w['lat']:.4f}&lon={w['lon']:.4f}"
        out["station"] = w["station"]
        out["obs_url"] = f"https://api.weather.gov/stations/{w['station']}/observations/latest"
    try:
        est = ctx.weather.estimate(market)
    except Exception as exc:
        est = {"error": f"NWS request failed ({type(exc).__name__})", "p_yes": None}
    if not est:
        out["error"] = "Not a recognised daily temperature market"
    elif est.get("p_yes") is None:
        out["error"] = est.get("error") or "NWS returned no usable forecast"
    else:
        out.update(ok=True, p_yes=est["p_yes"], estimate_f=est.get("estimate_f"), sigma_f=est.get("sigma_f"),
                   observed_extreme_f=est.get("observed_extreme_f"), metric=est.get("metric"), date=est.get("date"),
                   city=est.get("name"), hint=clean_copy(est.get("hint")))
    return out


def _crypto(market: dict, ctx: ContextAdapters) -> dict:
    c = parse_crypto_market(market)
    out: dict = {"name": "Coinbase Exchange public candles", "ok": False}
    if c is not None:
        out["url"] = COINBASE_CANDLES.format(product=c["product"])
        out["asset"] = c["asset"]
    try:
        cx = ctx.crypto.context(market)
    except Exception as exc:
        cx = {"error": f"Coinbase request failed ({type(exc).__name__})", "crypto_vol_z": None}
    if not cx:
        out["error"] = "Not a recognised crypto strike market"
    elif cx.get("crypto_vol_z") is None:
        out["error"] = cx.get("error") or "Coinbase returned no candles"
    else:
        out.update(ok=True, spot=cx.get("spot"), vol_z=cx.get("vol_z"), crypto_vol_z=cx.get("crypto_vol_z"),
                   direction=cx.get("direction"), hint=clean_copy(cx.get("hint")))
    return out


# ---------------- brief ----------------


def build_brief(
    market: dict,
    series: Optional[dict] = None,
    *,
    espn: Optional[EspnBoard] = None,
    context: Optional[ContextAdapters] = None,
    model_p_yes: Optional[float] = None,
    api_base: str = "",
    source_errors: Optional[list[str]] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Deterministic research brief. ``model_p_yes`` is the scanner's logistic
    P(YES) when the caller has it (display only)."""
    STATS["built"] += 1
    now = now or datetime.now(timezone.utc)
    series = series or {}
    ticker = str(market.get("ticker") or "")
    cat = category_of(market, series)
    q = market_quotes(market)
    mp = q["market_p_yes"]
    rules1 = _excerpt(market.get("rules_primary"))
    rules2 = _excerpt(market.get("rules_secondary"), 300)
    settle_srcs = [s for s in (series.get("settlement_sources") or []) if isinstance(s, dict) and s.get("name")]
    src_names = ", ".join(str(s["name"]) for s in settle_srcs) or "not listed in the public payload"

    adapters: list[dict] = []
    if cat == "sports" and is_full_game_market(market):
        adapters.append(_sports(market, espn if espn is not None else get_board()))
    if cat == "weather" and parse_weather_market(market) is not None:
        adapters.append(_weather(market, context if context is not None else get_context()))
    if cat == "crypto" and parse_crypto_market(market) is not None:
        adapters.append(_crypto(market, context if context is not None else get_context()))
    ok_sources = [a for a in adapters if a.get("ok")]

    # --- what settles YES
    settle_yes: list[str] = []
    if rules1:
        settle_yes.append(f"Kalshi rules: {rules1}")
    else:
        settle_yes.append(f"No rules text in the public payload. Title: {_excerpt(market.get('title') or ticker, 200)}")
    settle_yes.append(f"Settlement source named by Kalshi: {src_names}.")
    if market.get("close_time"):
        settle_yes.append(f"Trading closes {market.get('close_time')} (UTC).")

    # --- what would flip it
    flip: list[str] = []
    strike = _strike_text(market)
    for a in adapters:
        if a["name"].startswith("ESPN"):
            team = a.get("yes_team") or "the YES side"
            if a.get("ok") and a.get("state") == "in":
                flip.append(f"Live game ({a.get('summary')}, {a.get('detail')}): a scoring swing against {team} moves this fast.")
            elif a.get("ok") and a.get("state") == "post":
                flip.append("ESPN shows a final. Only an official correction or a Kalshi settlement difference would change it.")
            else:
                flip.append(f"{team} losing, or late injury / lineup news before the game starts.")
        elif a["name"].startswith("NWS"):
            word = "high" if (a.get("metric") or "high") == "high" else "low"
            flip.append(f"The official daily {word} landing on the other side of the strike ({strike or 'see rules'}). "
                        "Forecast misses of a few °F are common, and the Weather Company figure can differ from NWS rounding.")
        elif a["name"].startswith("Coinbase"):
            flip.append(f"Price crossing the strike ({strike or 'see rules'}) before the settlement window; "
                        "the settlement index is CF Benchmarks, not Coinbase spot.")
    if not adapters:
        flip.append("Anything in the rules that resolves NO; no outside adapter covers this market, so check the named source directly.")
    if rules2:
        flip.append(f"Fine print: {rules2}")

    # --- priced in
    priced: list[str] = []
    if mp is not None:
        ya = "—" if q["yes_ask"] is None else f"{q['yes_ask'] * 100:.0f}¢"
        na = "—" if q["no_ask"] is None else f"{q['no_ask'] * 100:.0f}¢"
        priced.append(f"Book implies YES ≈ {_pct(mp)} (YES ask {ya}, NO ask {na}, spread {q['spread_cents'] if q['spread_cents'] is not None else '—'}¢).")
    else:
        priced.append("No live quotes on either side, so nothing is priced yet.")
    if model_p_yes is not None:
        priced.append(f"Scanner model P(YES) {_pct(model_p_yes)} (logistic estimate, not an outside source).")
    gaps: list[float] = []
    for a in ok_sources:
        if a.get("p_yes") is not None and mp is not None:
            gap = (float(a["p_yes"]) - mp) * 100
            gaps.append(gap)
            verdict = "close to the book — largely priced in" if abs(gap) < 5 else (
                f"{gap:+.0f} pp vs the book — either the book knows something or the source is stale")
            priced.append(f"{a['name']}: P(YES) ≈ {_pct(a['p_yes'])}, {verdict}.")
        elif a.get("hint"):
            priced.append(f"{a['name']}: {a['hint']}")

    # --- thin
    thin: list[str] = []
    for a in adapters:
        if not a.get("ok"):
            thin.append(f"{a['name']} unavailable: {a.get('error') or 'no data'}.")
    if not adapters:
        thin.append(f"No primary-data adapter for {cat} markets here (ESPN / NWS / Coinbase only). "
                    "No news is pulled or invented.")
    if settle_srcs:
        thin.append(f"Named settlement source ({src_names}) is cited but not read automatically.")
    else:
        thin.append("Kalshi's public payload names no settlement source URL.")
    if q["volume"] < THIN_VOLUME:
        thin.append(f"Low volume ({q['volume']:.0f} contracts): the price may not reflect much information.")
    if q["spread_cents"] is not None and q["spread_cents"] > WIDE_SPREAD_C:
        thin.append(f"Wide spread ({q['spread_cents']}¢).")
    for e in source_errors or []:
        thin.append(e)

    # --- coverage
    conflict = any(abs(g) >= CONFLICT_PP for g in gaps)
    if conflict:
        coverage = "conflict"
    elif ok_sources and rules1 and mp is not None:
        coverage = "enough"
    else:
        coverage = "thin"

    # --- citations
    cites: list[dict] = []
    if ticker:
        cites.append({"name": f"Kalshi market {ticker} (public API)",
                      "url": f"{api_base.rstrip('/')}/trade-api/v2/markets/{ticker}" if api_base else None, "kind": "market"})
    if series.get("contract_url"):
        cites.append({"name": "Kalshi contract terms", "url": series["contract_url"], "kind": "rules"})
    for s in settle_srcs:
        cites.append({"name": f"Settlement source: {s['name']}", "url": s.get("url"), "kind": "settlement"})
    for a in adapters:
        if a.get("url"):
            cites.append({"name": a["name"] + ("" if a.get("ok") else " (no usable data)"), "url": a["url"], "kind": "data"})
        if a.get("obs_url"):
            cites.append({"name": f"NWS station {a.get('station')} observations", "url": a["obs_url"], "kind": "data"})

    brief = {
        "ticker": ticker,
        "title": _excerpt(market.get("title") or market.get("yes_sub_title") or ticker, 200),
        "category": cat,
        "series_category": series.get("category"),
        "close_time": market.get("close_time"),
        "status": market.get("status"),
        "quotes": q,
        "model_p_yes": model_p_yes,
        "coverage": coverage,
        "coverage_label": COVERAGE_LABEL[coverage],
        "needs_human_check": coverage != "enough",
        "sections": {
            "settles_yes": settle_yes,
            "would_flip": flip,
            "priced_in": priced,
            "still_thin": thin,
        },
        "sources": [{k: v for k, v in a.items() if k not in {"obs_url"}} for a in adapters],
        "citations": cites,
        "generator": "structured",
        "built_at": now.isoformat(),
        "note": "Research brief from public sources. It places no orders and does not change the geo gate.",
    }
    return _scrub(brief)


# ---------------- fetch + cache ----------------


def fetch_market(client, ticker: str) -> tuple[Optional[dict], Optional[dict], list[str]]:
    errors: list[str] = []
    try:
        raw = client.get_market(ticker)
        market = raw.get("market") or raw
    except Exception as exc:
        return None, None, [f"Kalshi market lookup failed: {exc}"]
    series = None
    st = _series_ticker(market)
    if st:
        try:
            raw = client.get_series(st)
            series = raw.get("series") or raw
        except Exception as exc:
            errors.append(f"Kalshi series lookup failed ({type(exc).__name__}); category and settlement source unknown.")
    return market, series, errors


def _model_p(market: dict, espn, context) -> Optional[float]:
    try:
        from app.strategy.scanner import enrich_market

        row = enrich_market(market, espn=espn, context=context)
        return None if row is None else (row.get("model") or {}).get("p_yes")
    except Exception:
        return None


def get_research(
    ticker: str,
    *,
    client,
    ledger=None,
    refresh: bool = False,
    espn: Optional[EspnBoard] = None,
    context: Optional[ContextAdapters] = None,
    clock: Callable[[], float] = time.time,
    now: Optional[datetime] = None,
) -> dict:
    """Cached brief for ``ticker``. Raises ``LookupError`` if Kalshi has no such market."""
    ticker = ticker.strip().upper()
    t = clock()
    if ledger is not None and not refresh:
        try:
            hit = ledger.get_research_sync(ticker, t)
        except Exception:
            hit = None
        if hit:
            STATS["cache_hits"] += 1
            return {**hit["brief"], "cached": True, "cached_at": hit["created_at"],
                    "cache_age_s": max(0, int(t - (hit["expires_at"] - RESEARCH_TTL_S))), "ttl_s": RESEARCH_TTL_S}
    market, series, errors = fetch_market(client, ticker)
    if market is None:
        raise LookupError(errors[0] if errors else "market not found")
    board = espn if espn is not None else get_board()
    ctx = context if context is not None else get_context()
    brief = build_brief(
        market, series, espn=board, context=ctx, model_p_yes=_model_p(market, board, ctx),
        api_base=getattr(client, "host", "") or "", source_errors=errors, now=now,
    )
    created = (now or datetime.now(timezone.utc)).isoformat()
    if ledger is not None:
        try:
            ledger.put_research_sync(ticker, brief, created, t + RESEARCH_TTL_S)
        except Exception:
            pass
    return {**brief, "cached": False, "cached_at": created, "cache_age_s": 0, "ttl_s": RESEARCH_TTL_S}

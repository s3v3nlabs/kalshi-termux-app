"""Scan open Kalshi markets for high-payout / mispriced single contracts."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from app.config import settings
from app.kalshi.client import KalshiClient
from app.strategy.espn import (
    EspnBoard,
    get_board,
    is_full_game_market,
    select_nearby_games,
    us_today,
)
from app.strategy.kelly import gemini_nash_payoff, kelly_from_payout_odds, kelly_size
from app.strategy.nash import nash_decision, nash_to_dict

# GO if Gemini Nash payoff clears this. Heuristic only — not a win guarantee.
NASH_GO_THRESHOLD = 0.4
# Full-game NFL/NBA/MLB/NHL/NCAAF/NCAAB series merged into the scan so a
# scoreboard match can set espn_live / espn_pre. Unmatched rows stay market_shrink.
_GAME_SERIES = ("KXNFLGAME", "KXNBAGAME", "KXMLBGAME", "KXNHLGAME", "KXNCAAFGAME", "KXNCAABGAME")
_SOURCE_PLAIN = {
    "espn_live": "ESPN live scoreboard",
    "espn_pre": "ESPN pregame line",
    "market_shrink": "market shrink",
}


def _cents_to_dollars(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        v = float(val)
    except (TypeError, ValueError):
        return None
    # Heuristic: values > 1 are likely cents (or dollars*100 integer)
    if v > 1.0:
        return v / 100.0
    return v


def _price_dollars(market: dict, side: str) -> Optional[float]:
    """Prefer *_dollars fields; fall back to integer cents fields."""
    side = side.lower()
    for key in (f"{side}_ask_dollars", f"{side}_bid_dollars"):
        if market.get(key) is not None:
            try:
                return float(market[key])
            except (TypeError, ValueError):
                pass
    for key in (f"{side}_ask", f"{side}_bid"):
        d = _cents_to_dollars(market.get(key))
        if d is not None:
            return d
    return None


def _payout_multiple(price: float) -> float:
    if price <= 0:
        return 0.0
    return 1.0 / price


def _fmt_cents(price: float) -> str:
    cents = price * 100.0
    if abs(cents - round(cents)) < 0.05:
        return str(int(round(cents)))
    return f"{cents:.1f}"


def _fmt_pct(price: float) -> str:
    pct = price * 100.0
    if abs(pct - round(pct)) < 0.05:
        return str(int(round(pct)))
    return f"{pct:.1f}"


def _build_explainer(
    market: dict,
    *,
    side: str,
    price: float,
    payout_odds: float,
    model_p: float,
    allocation: float,
    binary_contracts: int,
    prob_source: str = "market_shrink",
    espn_hint: str = "",
) -> dict:
    """Plain-English odds card. Estimates only — never a guaranteed win."""
    side_u = side.upper()
    cents = _fmt_cents(price)
    implied = _fmt_pct(price)
    mult = round(payout_odds, 2)
    title = (market.get("title") or market.get("subtitle") or "").strip()
    yes_sub = (market.get("yes_sub_title") or "").strip()
    no_sub = (market.get("no_sub_title") or "").strip()
    rules = (market.get("rules_primary") or market.get("rules_secondary") or "").strip()

    if side == "yes":
        win_outcome = yes_sub or title or "YES settles"
        lose_outcome = no_sub or "NO settles"
    else:
        win_outcome = no_sub or (f"the NO side of: {title}" if title else "NO settles")
        lose_outcome = yes_sub or title or "YES settles"

    win_if = f"You win if {win_outcome}."
    lose_if = f"You lose if {lose_outcome}."
    if title and title not in win_if:
        win_if = f"{win_if} Market: {title}."
    if rules:
        win_if = f"{win_if} {rules[:500]}"

    price_plain = (
        f"{side_u} at {cents}¢ ≈ market thinks ~{implied}% chance. "
        f"If you buy {side_u} for {cents}¢ and you're right, Kalshi pays $1 → about {mult:g}x "
        f"(1/{price:.4g}). If wrong, you lose the {cents}¢."
    )
    source_plain = _SOURCE_PLAIN.get(prob_source, prob_source)
    if prob_source == "market_shrink":
        why_odds = (
            f"The {side_u} ask ({cents}¢) is the market-implied chance (~{implied}%). "
            "No ESPN scoreboard match, so the model uses a conservative shrink from that "
            "price toward 50% (source: market_shrink). That is an estimate, not a lock."
        )
    else:
        why_odds = (
            f"{espn_hint} The {side_u} ask ({cents}¢) is still the market-implied chance "
            f"(~{implied}%). Source: {prob_source}. A high payout is not a likely win."
        )
    edge = model_p - price
    gk_contracts = int(allocation // price) if price > 0 and allocation > 0 else 0
    contracts = gk_contracts or binary_contracts
    edge_plain = (
        f"Book says {_fmt_pct(price)}%. Our model says {_fmt_pct(model_p)}% "
        f"({source_plain}, not a lock). Edge ≈ {edge:+.3f}. "
        f"Half-Kelly suggests ${allocation:.2f}"
        + (f" / {contracts} contracts." if contracts else " (size rounds to 0 contracts).")
    )
    profit = max(0.0, 1.0 - price)
    stake_k = gk_contracts * price
    payout_plain = (
        f"Per contract: stake ${price:.2f}, payout if win $1.00, "
        f"profit if win ${profit:.2f}, loss if lose ${price:.2f}. "
        f"At Gemini half-Kelly size ({gk_contracts} contracts): stake ${stake_k:.2f}, "
        f"payout if win ${gk_contracts * 1.0:.2f}, profit if win ${gk_contracts * profit:.2f}, "
        f"loss if lose ${stake_k:.2f}."
    )
    size_plain = (
        f"Gemini half-Kelly puts ${allocation:.2f} of bankroll on this {side_u} side "
        f"({mult:g}x). Binary Kelly (capped) suggests {binary_contracts} contracts. "
        "Both are sizing math, not a prediction that the side wins."
    )
    risk_plain = (
        "A high payout multiple means the market implies a low chance. "
        "It is not a likely win, and nothing here is guaranteed."
    )
    return {
        "win_if": win_if,
        "lose_if": lose_if,
        "price_plain": price_plain,
        "payout_plain": payout_plain,
        "why_odds": why_odds,
        "edge_plain": edge_plain,
        "size_plain": size_plain,
        "risk_plain": risk_plain,
    }


def sort_scan_rows(rows: list[dict], sort: str = "payout") -> list[dict]:
    """Highest payout multiple first, then EV / Nash payoff / Kelly dollars.

    `sort=score` keeps the money-seeking score as the primary key for callers
    that ask for it. Default is payout.
    """
    mode = (sort or "payout").strip().lower()

    def _ev(row: dict) -> float:
        return float((row.get("kelly") or {}).get("expected_value_per_contract") or 0.0)

    def _alloc(row: dict) -> float:
        return float((row.get("gemini_kelly") or {}).get("allocation") or 0.0)

    def _nash(row: dict) -> float:
        return float(row.get("nash_payoff_gemini") or 0.0)

    if mode == "score":
        rows.sort(
            key=lambda r: (float(r.get("score") or 0.0), float(r.get("best_payout_multiple") or 0.0), _nash(r)),
            reverse=True,
        )
    else:
        rows.sort(
            key=lambda r: (
                float(r.get("best_payout_multiple") or 0.0),
                _ev(r),
                _nash(r),
                _alloc(r),
            ),
            reverse=True,
        )
    return rows


def enrich_market(
    m: dict,
    bankroll: float = 1000.0,
    espn: Optional[EspnBoard] = None,
    uncalibrated: bool = False,
) -> Optional[dict]:
    """``uncalibrated=True`` (Brier > 0.25 or no settled history) halves Kelly."""
    ticker = m.get("ticker") or ""
    if "CROSSCATEGORY" in ticker.upper():
        return None

    yes_ask = _price_dollars(m, "yes")
    no_ask = _price_dollars(m, "no")
    # Treat zero / missing asks as absent (common on empty combo books)
    if yes_ask is not None and yes_ask <= 0:
        yes_ask = None
    if no_ask is not None and no_ask <= 0:
        no_ask = None
    if yes_ask is None and no_ask is None:
        return None

    # Fill missing side from parity approx
    if yes_ask is None and no_ask is not None:
        yes_ask = max(0.01, min(0.99, 1.0 - no_ask))
    if no_ask is None and yes_ask is not None:
        no_ask = max(0.01, min(0.99, 1.0 - yes_ask))

    yes_ask = float(yes_ask)
    no_ask = float(no_ask)

    vol = float(m.get("volume") or m.get("volume_fp") or 0) or 0.0
    try:
        vol = float(vol)
    except (TypeError, ValueError):
        vol = 0.0
    oi = float(m.get("open_interest") or m.get("open_interest_fp") or 0) or 0.0
    try:
        oi = float(oi)
    except (TypeError, ValueError):
        oi = 0.0

    yes_bid = _price_dollars(m, "yes")  # approx; better: use bid fields
    # Try explicit bids
    yb = m.get("yes_bid_dollars")
    ya = m.get("yes_ask_dollars")
    try:
        if yb is not None and ya is not None:
            spread_cents = abs(float(ya) - float(yb)) * 100
        else:
            yb_c = m.get("yes_bid")
            ya_c = m.get("yes_ask")
            if yb_c is not None and ya_c is not None:
                spread_cents = abs(float(ya_c) - float(yb_c))
                if spread_cents < 1 and float(ya_c) <= 1:
                    spread_cents *= 100
            else:
                spread_cents = abs(yes_ask + no_ask - 1.0) * 100
    except (TypeError, ValueError):
        spread_cents = abs(yes_ask + no_ask - 1.0) * 100

    sports_game = is_full_game_market(m)
    estimate = None
    if sports_game:
        try:
            estimate = (espn if espn is not None else get_board()).estimate(m)
        except Exception:
            estimate = None
    model_p_yes = estimate.p_yes if estimate is not None else None
    prob_source = estimate.source if estimate is not None else "market_shrink"
    espn_hint = estimate.hint if estimate is not None else ""

    nash = nash_decision(
        yes_ask=yes_ask,
        no_ask=no_ask,
        model_prob_yes=model_p_yes,
        spread_cents=spread_cents,
        volume=vol,
        open_interest=oi,
    )

    yes_mult = _payout_multiple(yes_ask)
    no_mult = _payout_multiple(no_ask)
    # Lead with the cheap high-multiple side (2x–13x+ tails).
    if yes_mult >= no_mult:
        best_side = "yes"
        best_price = yes_ask
        best_p = float(nash.model_prob_yes)
        best_mult = yes_mult
    else:
        best_side = "no"
        best_price = no_ask
        best_p = 1.0 - float(nash.model_prob_yes)
        best_mult = no_mult

    # Binary-contract Kelly: edge = p * $1 - price (capped fraction).
    kelly_scale = 0.5 if uncalibrated else 1.0
    kelly = kelly_size(
        model_prob=best_p,
        contract_price=best_price,
        bankroll=bankroll,
        fraction=settings.kelly_fraction * kelly_scale,
        max_fraction=settings.kelly_max_fraction * kelly_scale,
    )
    # Gemini half-Kelly on gross payout odds. fraction 0.5 matches the scrap.
    gk = kelly_from_payout_odds(
        p=best_p,
        payout_odds=best_mult,
        bankroll=bankroll,
        fraction=0.5 * kelly_scale,
    )
    nash_pay = gemini_nash_payoff(best_p, best_mult)
    signal = "GO" if nash_pay > NASH_GO_THRESHOLD else "NO-GO"
    model_p_source = prob_source

    cheap = (yes_ask * 100 <= settings.max_contract_price_cents) or (
        no_ask * 100 <= settings.max_contract_price_cents
    )
    high_payout = best_mult >= settings.min_payout_multiple
    thin_penalty = 0.5 if (vol + oi) < 100 else 0.0
    pos_ev = max(0.0, float(kelly.expected_value_per_contract))
    # Money-seeking rank: payout multiple + positive EV + Nash payoff + half-Kelly.
    score = (
        best_mult
        + (pos_ev * 10.0)
        + (max(0.0, nash_pay) * 2.0)
        + (gk.fractional * 10.0)
        - thin_penalty
    )
    explainer = _build_explainer(
        m,
        side=best_side,
        price=best_price,
        payout_odds=best_mult,
        model_p=best_p,
        allocation=gk.allocation,
        binary_contracts=kelly.contracts,
        prob_source=prob_source,
        espn_hint=espn_hint,
    )

    model_features = {
        "yes_ask": round(yes_ask, 4),
        "no_ask": round(no_ask, 4),
        "volume": vol,
        "open_interest": oi,
        "spread_cents": round(spread_cents, 2),
        "close_time": m.get("close_time"),
        "prob_source": prob_source,
        "espn_p_yes": model_p_yes,
    }

    return {
        "ticker": ticker,
        "model_features": model_features,
        "model_uncalibrated": bool(uncalibrated),
        "kelly_scale": kelly_scale,
        "title": m.get("title") or m.get("subtitle") or m.get("yes_sub_title") or ticker,
        "event_ticker": m.get("event_ticker"),
        "yes_ask": round(yes_ask, 4),
        "no_ask": round(no_ask, 4),
        "yes_ask_cents": round(yes_ask * 100, 1),
        "no_ask_cents": round(no_ask * 100, 1),
        "yes_payout_multiple": round(yes_mult, 2),
        "no_payout_multiple": round(no_mult, 2),
        "best_payout_multiple": round(best_mult, 2),
        "best_side": best_side,
        "payout_odds": round(best_mult, 4),
        "model_p_source": model_p_source,
        "prob_source": prob_source,
        "sports_game": sports_game,
        "espn_hint": espn_hint,
        "espn": estimate.as_espn() if estimate is not None else None,
        "spread_cents": round(spread_cents, 2),
        "volume": vol,
        "open_interest": oi,
        "status": m.get("status"),
        "close_time": m.get("close_time"),
        "cheap_contract": cheap,
        "high_payout": high_payout,
        "nash": nash_to_dict(nash),
        "nash_payoff_gemini": round(nash_pay, 4),
        "signal": signal,
        "gemini_kelly": {
            "allocation": gk.allocation,
            "kelly_pct": gk.kelly_pct,
            "payout_odds": gk.payout_odds,
            "p": gk.p,
        },
        "kelly": {
            "side": best_side,
            "full_kelly": kelly.full_kelly,
            "fractional_kelly": kelly.fractional_kelly,
            "allocation_usd": kelly.allocation_usd,
            "kelly_pct_of_bankroll": kelly.kelly_pct_of_bankroll,
            "contracts": kelly.contracts,
            "edge": kelly.edge,
            "expected_value_per_contract": kelly.expected_value_per_contract,
            "reason": kelly.reason,
        },
        "explainer": explainer,
        "score": round(score, 4),
    }


def _merge_nearby_game_markets(client: KalshiClient, markets: list[dict]) -> list[dict]:
    """Add open full-game winner markets near today. Failures stay on the book scan.

    Events are soonest-first. The plain markets feed is not, so a college
    weekend can sit behind later dates.
    """
    extra: list[dict] = []
    for series in _GAME_SERIES:
        try:
            if hasattr(client, "get_events"):
                raw = client.get_events(
                    series_ticker=series,
                    status="open",
                    limit=30,
                    with_nested_markets=True,
                )
                for event in raw.get("events") or []:
                    extra.extend(event.get("markets") or [])
            else:
                raw = client.get_markets(status="open", limit=100, series_ticker=series)
                extra.extend(raw.get("markets") or [])
        except Exception:
            continue
    seen = {m.get("ticker") for m in markets}
    merged = list(markets)
    for market in select_nearby_games(extra, us_today()):
        ticker = market.get("ticker")
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        merged.append(market)
    return merged


def model_run_rows(rows: list[dict]) -> list[dict]:
    """One model_runs row per enriched market: model P(YES) + its inputs."""
    out = []
    for row in rows:
        p = (row.get("nash") or {}).get("model_prob_yes")
        if row.get("ticker") and p is not None:
            out.append({"ticker": row["ticker"], "model_prob": float(p), "features": row.get("model_features") or {}})
    return out


def scan_opportunities(
    client: Optional[KalshiClient] = None,
    limit: Optional[int] = None,
    bankroll: float = 1000.0,
    only_actionable: bool = False,
    sort: str = "payout",
    espn: Optional[EspnBoard] = None,
    include_game_series: bool = True,
    calibration: Optional[dict] = None,
) -> dict:
    """``calibration`` is the ledger's /calibration payload. When it says the
    model is uncalibrated (Brier > 0.25, or no settled bets yet) every row is
    flagged and Kelly is halved."""
    client = client or KalshiClient()
    limit = limit or settings.scan_limit
    markets = []
    cursor = None
    pages = 0
    target = max(limit, 20)
    while len(markets) < target and pages < 5:
        raw = client.get_markets(
            status="open",
            limit=min(100, max(limit, 50)),
            cursor=cursor,
            mve_filter="exclude",
        )
        batch = raw.get("markets") or []
        markets.extend(batch)
        cursor = raw.get("cursor") or None
        pages += 1
        if not batch or not cursor:
            break
    markets = markets[: max(limit * 2, limit)]  # allow filter headroom
    if include_game_series:
        markets = _merge_nearby_game_markets(client, markets)
    board = espn if espn is not None else get_board()
    uncalibrated = True if calibration is None else bool(calibration.get("uncalibrated", True))
    enriched = []
    espn_stats = {"matched": 0, "live": 0, "pre": 0, "sports_unmatched": 0, "fallback": 0}
    for m in markets:
        row = enrich_market(m, bankroll=bankroll, espn=board, uncalibrated=uncalibrated)
        if not row:
            continue
        if only_actionable and row["nash"]["action"] == "PASS":
            # still keep high-payout cheap contracts for the scanner board
            if not (row["cheap_contract"] and row["high_payout"]):
                continue
        src = row.get("prob_source") or "market_shrink"
        if src == "espn_live":
            espn_stats["matched"] += 1
            espn_stats["live"] += 1
        elif src == "espn_pre":
            espn_stats["matched"] += 1
            espn_stats["pre"] += 1
        elif row.get("sports_game"):
            espn_stats["sports_unmatched"] += 1
            espn_stats["fallback"] += 1
        else:
            espn_stats["fallback"] += 1
        enriched.append(row)
    espn_stats["fetch_ok"] = board.fetch_ok
    espn_stats["fetch_errors"] = board.fetch_errors

    sort_mode = (sort or "payout").strip().lower()
    if sort_mode not in {"payout", "score"}:
        sort_mode = "payout"
    sort_scan_rows(enriched, sort_mode)
    now = datetime.now(timezone.utc).isoformat()
    return {
        "count": len(enriched),
        "scanned": len(markets),
        "sort": sort_mode,
        "host": client.host,
        "host_key": settings.host_key,
        "updated_at": now,
        "model_uncalibrated": uncalibrated,
        "calibration_brier": (calibration or {}).get("brier"),
        "calibration_n": (calibration or {}).get("n", 0),
        "cache_ttl_seconds": settings.cache_ttl,
        "disclaimer": (
            "Edge, payout multiples, and half-Kelly size are estimates, not guarantees. "
            "A high multiple means a low market-implied chance. "
            "ESPN scoreboard matches (espn_live / espn_pre) are context, not a sure outcome. "
            "No 100% win-rate claim. Risk of loss."
        ),
        "espn": espn_stats,
        "markets": enriched,
    }

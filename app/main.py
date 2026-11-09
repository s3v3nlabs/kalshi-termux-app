"""FastAPI entry — Termux-friendly Kalshi betting portal."""
from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app import __version__
from app import geo
from app.config import HOSTS, settings
from app.db.ledger import Ledger, settle_open_bets
from app.db.store import Store
from app.kalshi.books import BookStore
from app.kalshi.ws import MarketStream
from app.kalshi.client import (
    MAX_CONTRACTS_PER_ORDER,
    KalshiClient,
    detect_key_type,
    key_type_of_file,
    friendly_order_error,
    summarize_positions,
)
from app.security.mfa import TOTPManager
from app.security.vault import Vault
from app.strategy.kelly import kelly_size
from app.strategy.model import maybe_refit
from app.strategy.monte_carlo import double_slit_simulation, sim_to_dict
from app.strategy.nash import nash_decision, nash_to_dict
from app.strategy.scanner import enrich_market, model_run_rows, scan_opportunities
from app.trading.orders import OrderGate
from app.ui_prefs import (
    apply_saved_mode,
    load_skip_live_confirm,
    save_dry_run_override,
    save_skip_live_confirm,
)

ROOT = Path(__file__).resolve().parent.parent


def _background_enabled() -> bool:
    """Background loops (auto-bet, continuous learning) run in the server, not in tests."""
    if os.getenv("BACKGROUND_TASKS", "1").strip().lower() in {"0", "false", "no", "off"}:
        return False
    return "pytest" not in sys.modules


BUDGET_REFRESH_S = 6 * 3600
_BUDGET_LOAD: dict = {}


async def _budget_loop() -> None:
    """Size the shared request budget from Kalshi (endpoint costs, and with
    keys GET /account/limits), then refresh every 6 hours."""
    from app.kalshi.async_client import AsyncKalshiClient

    while True:
        ac = AsyncKalshiClient()
        try:
            _BUDGET_LOAD.clear()
            _BUDGET_LOAD.update(await ac.load_budget())
            # Optional free Advanced upgrade (Settings toggle, OFF by default).
            _BUDGET_LOAD["auto_upgrade"] = await asyncio.to_thread(tier.maybe_auto_upgrade)
        except Exception as exc:  # pragma: no cover - load_budget catches
            _BUDGET_LOAD["errors"] = [str(exc)]
        finally:
            await ac.aclose()
        await asyncio.sleep(BUDGET_REFRESH_S)


@asynccontextmanager
async def _lifespan(_app):
    tasks = []
    if _background_enabled():
        tasks.append(asyncio.create_task(_budget_loop(), name="budget"))
        tasks.append(asyncio.create_task(stream.run_forever(), name="stream"))
        tasks.append(asyncio.create_task(autobet.run_forever(), name="autobet"))
        tasks.append(asyncio.create_task(sniper.run_forever(), name="sniper"))
        tasks.append(asyncio.create_task(learner.run_forever(), name="learner"))
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()


app = FastAPI(
    title="Kalshi Termux Portal",
    version=__version__,
    docs_url="/api/docs",
    redoc_url=None,
    lifespan=_lifespan,
)

app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
templates = Jinja2Templates(directory=str(ROOT / "templates"))

store = Store()
ledger = Ledger()
apply_saved_mode(store)
vault = Vault()
# Startup unlock: derive the scrypt Fernet key from VAULT_PASSPHRASE if set.
if os.getenv("VAULT_PASSPHRASE"):
    if vault.unlock(os.environ["VAULT_PASSPHRASE"]):
        _saved_key_id = vault.get("kalshi_key_id")
        if _saved_key_id and not settings.key_id:
            settings.key_id = _saved_key_id
    else:
        store.log_activity("security", "VAULT_PASSPHRASE did not unlock the vault")
totp_mgr = TOTPManager(vault)
client = KalshiClient()
orders = OrderGate(store, client, ledger)

from app.trading.exchange_gate import ExchangeGate  # noqa: E402
from app.kalshi.tier import TierManager  # noqa: E402
from app.strategy.fees import SeriesFees  # noqa: E402

tier = TierManager(store, lambda: client, lambda: settings.has_keys)

# trading_active gate for automated orders (cached 15 s, fails closed).
exchange_gate = ExchangeGate(lambda: client.exchange_status())

# Live market data (WebSocket). Falls back to REST polling when not connected.
books = BookStore()
stream = MarketStream(books)
stream.enabled = os.getenv("WS_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
WS_WATCH_SCAN_N = int(os.getenv("WS_WATCH_SCAN_N", "120") or 120)
RAW_LIST_TTL_S = float(os.getenv("STREAM_LIST_TTL_S", "45") or 45)


def _scan_feed_kwargs() -> dict:
    """With the stream live, scans read pushed prices and reuse the market list."""
    if stream.live:
        return {"quotes": books, "raw_ttl_s": RAW_LIST_TTL_S}
    return {}


def _watch_scan_rows(rows: list) -> None:
    stream.set_watch("scan", [r.get("ticker") for r in rows[:WS_WATCH_SCAN_N] if r.get("ticker")])


def _auto_scan(bankroll: float) -> dict:
    """Scan for the auto-bet loop: same ranking, calibration and Kelly halving as /scanner."""
    from app.strategy.analogs import load_pool

    try:
        calib = ledger.calibration_sync()
    except Exception:
        calib = None
    result = scan_opportunities(
        client, limit=settings.scan_limit, bankroll=bankroll, sort="mispricing", calibration=calib,
        analog_pool=load_pool(ledger), **_scan_feed_kwargs()
    )
    _watch_scan_rows(result.get("markets") or [])
    try:
        ledger.add_model_snapshots_sync(model_run_rows(result.get("markets") or []))
    except Exception:
        pass
    return result


from app.strategy.learner import Learner  # noqa: E402
from app.trading.autobet import AutoBetEngine  # noqa: E402

series_fees = SeriesFees(lambda s: client.get_series(s))
autobet = AutoBetEngine(store=store, ledger=ledger, orders=orders, scan_fn=_auto_scan, client_fn=lambda: client,
                        exchange_gate=exchange_gate, fee_multiplier_fn=series_fees.multiplier)
learner = Learner(ledger=ledger, store=store, client_fn=lambda: client)

from app.strategy.espn import get_board  # noqa: E402
from app.trading.sniper import Sniper  # noqa: E402


def _ws_quotes(tickers: list) -> dict:
    out = {}
    for t in tickers:
        q = books.quote(t)
        if q:
            out[t] = q
    return out


sniper = Sniper(
    store=store, ledger=ledger, orders=orders, client_fn=lambda: client,
    bankroll_fn=lambda live: autobet._bankroll(live),
    held_fn=lambda live: autobet._held(live)[0],
    exchange_gate=exchange_gate, fee_multiplier_fn=series_fees.multiplier,
    quotes_fn=_ws_quotes, watch_fn=lambda ts: stream.set_watch("sniper", ts),
    espn_fn=lambda m: get_board().estimate(m),
)

_LAST_ASK: dict[str, tuple] = {}


def _on_stream_event(kind: str, msg: dict) -> None:
    """Fills update the ledger; a watched market's price move wakes auto-bet
    early (rate-limited)."""
    if kind in {"fill", "user_order"}:
        fn = orders.on_fill if kind == "fill" else orders.on_user_order
        try:  # sqlite work off the event loop
            asyncio.get_running_loop().run_in_executor(None, fn, msg)
        except RuntimeError:
            fn(msg)
        return
    if kind in {"trade", "lifecycle", "ticker", "book"}:
        st = msg.get("ticker") or msg.get("market_ticker")
        if st and st.upper() in sniper.watched:
            sniper.poke(st)  # near-finish game moved: re-score now (rate-limited)
    if kind not in {"ticker", "book"}:
        return
    t = msg.get("ticker")
    q = books.quote(t) if t else None
    if not q:
        return
    key = (q.get("yes_ask"), q.get("no_ask"))
    if _LAST_ASK.get(t) == key:
        return
    _LAST_ASK[t] = key
    autobet.poke("price move")


stream.on_event(_on_stream_event)

# Session-ish flags (local process only)
_app_unlocked = False


def _refresh_client() -> KalshiClient:
    global client, orders
    client = KalshiClient()
    orders.client = client
    return client


# ---------- pages ----------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "version": __version__,
            "host": settings.api_base,
            "dry_run": settings.dry_run,
        },
    )


# ---------- installable web app (PWA) ----------

@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(ROOT / "static" / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    # Served from / so its scope covers the whole app.
    return FileResponse(
        ROOT / "static" / "js" / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


def _balance_dollars(data: dict) -> Any:
    """Prefer Kalshi's fixed-point balance_dollars; fall back to integer cents."""
    fp = data.get("balance_dollars")
    if fp not in (None, ""):
        try:
            return round(float(fp), 6)
        except (TypeError, ValueError):
            pass
    bal = data.get("balance", 0)
    return bal / 100.0 if isinstance(bal, (int, float)) else bal


# ---------- health / status ----------

_KEY_TYPE_CACHE: dict = {}


def _key_type() -> Optional[str]:
    """"ed25519" / "rsa" / None for the configured key file (cached by mtime)."""
    p = settings.key_path
    try:
        mt = p.stat().st_mtime if settings.has_keys and p.is_file() else None
    except OSError:
        mt = None
    if mt is None:
        return None
    if _KEY_TYPE_CACHE.get("k") != (str(p), mt):
        _KEY_TYPE_CACHE.update(k=(str(p), mt), t=key_type_of_file(p))
    return _KEY_TYPE_CACHE.get("t")


@app.get("/api/status")
def api_status():
    # Cached exchange status (shared with the auto-bet/sniper gate) instead of
    # a fresh REST call on every status poll.
    ex = exchange_gate.check()
    ping = None
    err = None
    if ex.get("readable"):
        ping = {"ok": True, "host": client.host, "host_key": settings.host_key,
                "exchange_active": ex["exchange_active"], "trading_active": ex["trading_active"],
                "authenticated": settings.has_keys, "age_s": ex.get("age_s")}
    else:
        err = ex.get("reason")
    gate = orders.status()
    return {
        "version": __version__,
        "host": settings.api_base,
        "host_key": settings.host_key,
        "hosts_available": list(HOSTS.keys()),
        "has_keys": settings.has_keys,
        "key_path_exists": settings.key_path.is_file(),
        "key_type": _key_type(),
        "dry_run": bool(settings.dry_run),
        "dry_run_env": bool(settings.env_dry_run),
        "order_gate": gate,
        "mode_label": gate["mode_label"],
        "vault_unlocked": vault.unlocked,
        "app_unlocked": _app_unlocked,
        "totp_configured": bool(vault.unlocked and totp_mgr.has_seed()),
        "skip_live_confirm": load_skip_live_confirm(store),
        "bucket": client.bucket.status(),
        "stream": stream.status(),
        "sniper": {k: sniper.status()[k] for k in ("enabled", "mode", "hot", "watching")},
        "exchange": ping,
        "exchange_error": err,
        "disclaimer": "Not financial advice. No guaranteed win rate. Risk of loss.",
    }


class AutoTierBody(BaseModel):
    enabled: bool


@app.get("/api/limits")
def api_limits(refresh: bool = False):
    """Usage tier + read/write token buckets (GET /account/limits with keys)."""
    return {**tier.limits(refresh=bool(refresh)), **tier.status(), "budget_load": {k: v for k, v in _BUDGET_LOAD.items() if k != "limits"}}


@app.post("/api/limits/upgrade")
def api_limits_upgrade():
    """One call to POST /account/api_usage_level/upgrade (free Advanced tier)."""
    return tier.upgrade()


@app.post("/api/limits/auto")
def api_limits_auto(body: AutoTierBody):
    tier.set_auto(body.enabled)
    store.log_activity("settings", "Auto tier upgrade ON" if body.enabled else "Auto tier upgrade OFF")
    out = {"ok": True, **tier.status()}
    if body.enabled:
        out["attempt"] = tier.maybe_auto_upgrade()
    return out


@app.get("/api/exchange")
def api_exchange(refresh: bool = False):
    """trading_active gate used by auto-bet and the sniper (cached 15 s)."""
    return exchange_gate.check(force=bool(refresh))


@app.get("/api/stream")
def api_stream():
    """WebSocket feed status: live stream vs REST polling, books, seq gaps."""
    return {"ok": True, **stream.status(), "watch_sample": stream.watch()[:20]}


@app.get("/api/ping")
def api_ping():
    try:
        return client.ping()
    except Exception as e:
        raise HTTPException(502, str(e))


# ---------- markets ----------

def _apply_geo(result: dict, state: Optional[str]) -> dict:
    """Annotate rows (not cached: depends on the caller's state)."""
    out = dict(result)
    rows = []
    blocked = 0
    for row in result.get("markets") or []:
        g = geo.check(row.get("ticker") or "", state)
        blocked += 0 if g["allowed"] else 1
        rows.append({**row, "geo": g})
    out["markets"] = rows
    out["geo"] = {"state": state, "blocked": blocked}
    return out


# Latest scan's model-run rows by ticker, so a bet can snapshot its inputs
# without a network call (see trading_order).
_LAST_SCAN_RUNS: dict[str, dict] = {}


@app.get("/api/markets/scan")
def api_scan(
    request: Request,
    limit: int = 100,
    bankroll: float = 1000.0,
    refresh: bool = False,
    sort: str = "mispricing",
):
    sort_mode = (sort or "mispricing").strip().lower()
    if sort_mode not in {"mispricing", "payout", "score"}:
        sort_mode = "mispricing"
    cache_key = f"scan:{limit}:{bankroll}:{sort_mode}"
    if not refresh:
        cached = store.cache_get(cache_key)
        if cached:
            cached["from_cache"] = True
            return _apply_geo(cached, geo.state_for_request(request))
    try:
        try:
            calib = ledger.calibration_sync()
        except Exception:
            calib = None  # treated as uncalibrated: Kelly halved
        from app.strategy.analogs import load_pool

        result = scan_opportunities(
            client, limit=limit, bankroll=bankroll, sort=sort_mode, calibration=calib, analog_pool=load_pool(ledger),
            **_scan_feed_kwargs()
        )
        _watch_scan_rows(result.get("markets") or [])
    except Exception as e:
        store.log_activity("error", f"Scan failed: {e}")
        raise HTTPException(502, str(e))
    result["from_cache"] = False
    run_rows = model_run_rows(result.get("markets") or [])
    _LAST_SCAN_RUNS.clear()
    _LAST_SCAN_RUNS.update({r["ticker"]: r for r in run_rows})
    try:
        # Training snapshots, not a poll log: at most one row per ticker per hour.
        result["model_runs_written"] = ledger.add_model_snapshots_sync(run_rows)
    except Exception as e:
        store.log_activity("error", f"Could not write model runs: {e}")
    store.cache_set(cache_key, result, ttl=settings.cache_ttl)
    store.log_activity(
        "scan",
        f"Scanned {result['scanned']} markets → {result['count']} enriched via {result['host']}",
    )
    return _apply_geo(result, geo.state_for_request(request))


@app.get("/scanner")
def scanner(request: Request, limit: int = 100, bankroll: float = 1000.0, refresh: bool = False):
    """Scanner ranked by softmax-equilibrium mispricing score."""
    return api_scan(request, limit=limit, bankroll=bankroll, refresh=refresh, sort="mispricing")


@app.get("/api/markets/{ticker}")
def api_market(ticker: str, request: Request, bankroll: float = 1000.0):
    gate_geo = geo.check(ticker, geo.state_for_request(request))
    try:
        raw = client.get_market(ticker)
        market = raw.get("market") or raw
        enriched = enrich_market(market, bankroll=bankroll)
        ob = None
        try:
            ob = client.get_orderbook(ticker)
        except Exception:
            ob = None
        return {
            "ticker": ticker,
            "host": client.host,
            "raw": market,
            "enriched": enriched,
            "orderbook": ob,
            "geo": gate_geo,
        }
    except Exception as e:
        raise HTTPException(502, str(e))


# ---------- research (on demand; never on the scanner poll) ----------

@app.get("/api/research/{ticker}")
@app.get("/research/{ticker}")
def api_research(ticker: str, request: Request, refresh: bool = False):
    """Research brief for one market, opened from the expanded scanner row.

    Cached in the ledger for ``RESEARCH_TTL_S`` (20 min); ``?refresh=1`` rebuilds.
    Read-only: places no orders and does not alter the geo gate. The caller's
    geo result is attached for display so blocked sports still say BLOCKED."""
    from app.strategy.research import get_research

    t = (ticker or "").strip().upper()
    if not t or len(t) > 120:
        raise HTTPException(400, "Ticker required")
    try:
        brief = get_research(t, client=client, ledger=ledger, refresh=bool(refresh))
    except LookupError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        store.log_activity("error", f"Research failed for {t}: {e}")
        raise HTTPException(502, f"Research failed: {e}")
    return {"ok": True, **brief, "geo": geo.check(t, geo.state_for_request(request))}


# ---------- strategy ----------

class AnalyzeBody(BaseModel):
    yes_ask: float = Field(..., gt=0, lt=1.5)
    no_ask: float = Field(..., gt=0, lt=1.5)
    model_prob_yes: Optional[float] = None
    bankroll: float = 1000.0
    volume: float = 0
    open_interest: float = 0
    run_simulation: bool = True


@app.post("/api/strategy/analyze")
def api_analyze(body: AnalyzeBody):
    # Accept cents accidentally passed as >1
    yes = body.yes_ask / 100.0 if body.yes_ask > 1 else body.yes_ask
    no = body.no_ask / 100.0 if body.no_ask > 1 else body.no_ask
    nash = nash_decision(
        yes_ask=yes,
        no_ask=no,
        model_prob_yes=body.model_prob_yes,
        volume=body.volume,
        open_interest=body.open_interest,
    )
    if nash.action == "BUY_YES":
        k = kelly_size(nash.model_prob_yes, yes, body.bankroll, settings.kelly_fraction, settings.kelly_max_fraction)
        side = "yes"
        price = yes
    elif nash.action == "BUY_NO":
        k = kelly_size(1 - nash.model_prob_yes, no, body.bankroll, settings.kelly_fraction, settings.kelly_max_fraction)
        side = "no"
        price = no
    else:
        k = kelly_size(0.5, min(yes, no), body.bankroll, settings.kelly_fraction, settings.kelly_max_fraction)
        side = "pass"
        price = min(yes, no)

    sim = None
    if body.run_simulation:
        odds = 1.0 / price if price > 0 else 2.0
        sim = sim_to_dict(
            double_slit_simulation(
                base_prob=nash.model_prob_yes if side != "no" else 1 - nash.model_prob_yes,
                payout_odds=odds,
            )
        )

    return {
        "nash": nash_to_dict(nash),
        "kelly": {
            "side": side,
            "full_kelly": k.full_kelly,
            "fractional_kelly": k.fractional_kelly,
            "allocation_usd": k.allocation_usd,
            "kelly_pct_of_bankroll": k.kelly_pct_of_bankroll,
            "contracts": k.contracts,
            "edge": k.edge,
            "expected_value_per_contract": k.expected_value_per_contract,
            "reason": k.reason,
        },
        "simulation": sim,
        "disclaimer": "Model estimates only. Not financial advice. No 100% win rate.",
    }


# ---------- account ----------

@app.get("/api/account/balance")
def api_balance():
    if not settings.has_keys:
        return {"ok": False, "error": "Keys not configured", "authenticated": False}
    try:
        data = client.get_balance()
        dollars = _balance_dollars(data)
        portfolio = data.get("portfolio_value")
        portfolio_dollars = portfolio / 100.0 if isinstance(portfolio, (int, float)) else None
        store.log_activity("account", f"Balance fetched: ${dollars}")
        return {
            "ok": True,
            "raw": data,
            "balance_dollars": dollars,
            "portfolio_value_dollars": portfolio_dollars,
            "host": client.host,
        }
    except Exception as e:
        raise HTTPException(502, friendly_order_error(e)["error"])


@app.get("/api/account/positions")
def api_positions():
    if not settings.has_keys:
        return {"ok": False, "error": "Keys not configured", "authenticated": False}
    try:
        data = client.get_positions()
        return {"ok": True, "data": data, "summary": summarize_positions(data), "host": client.host}
    except Exception as e:
        raise HTTPException(502, friendly_order_error(e)["error"])


@app.get("/api/account/orders")
def api_acct_orders(status: Optional[str] = None):
    if not settings.has_keys:
        return {"ok": False, "error": "Keys not configured", "authenticated": False}
    try:
        return {"ok": True, "data": client.get_orders(status=status), "host": client.host}
    except Exception as e:
        raise HTTPException(502, friendly_order_error(e)["error"])


# ---------- calibration ----------

@app.get("/calibration")
def calibration():
    """Reliability buckets (0-10%, ..., 90-100%) + Brier over settled bets."""
    return ledger.calibration_sync()


# ---------- backtest (offline replay, no live calls) ----------

def _backtest_rows():
    import backtest as bt

    rows = bt.load_ledger_dataset(ledger)
    source = "ledger"
    if not rows:
        rows = bt.load_fixture(ROOT / "tests" / "fixtures" / "backtest_fixture.json")
        source = "fixture (no settled history yet)"
    return bt, rows, source


@app.get("/backtest")
def backtest_report():
    """Current vs proposed (refit) weights on stored history; fixture if none."""
    from app.strategy.model import load_model

    bt, rows, source = _backtest_rows()
    result = bt.compare(rows, load_model()["weights"])
    result["source"] = source
    return result


@app.post("/backtest/promote")
def backtest_promote():
    """Write the proposed weights to data/model_weights.json if the gate passes."""
    from datetime import datetime, timezone

    from app.strategy.model import load_model, save_model

    bt, rows, source = _backtest_rows()
    if source != "ledger":
        raise HTTPException(400, "No settled history yet. The fixture is for testing the gate, not for promoting weights.")
    doc = load_model()
    result = bt.compare(rows, doc["weights"])
    if not result["gate_pass"]:
        raise HTTPException(409, "Proposed weights make the backtest Brier worse. Not promoted.")
    doc["weights"] = result["proposed"]["weights"]
    doc["version"] = int(doc.get("version") or 0) + 1
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "promoted": True,
        "brier_before": result["current"]["report"]["brier"],
        "brier_after": result["proposed"]["report"]["brier"],
        "version": doc["version"],
        "weights": doc["weights"],
    }
    doc.setdefault("refit_log", []).append(record)
    path = save_model(doc)
    store.log_activity("model", f"Promoted weights v{doc['version']} from backtest", record)
    return {"ok": True, "path": str(path.relative_to(ROOT)), **record}


# ---------- settled-market training (phase 1) ----------

@app.get("/api/training")
def api_training_status():
    """Settled sample size, last refit/gate result, and whether gems are verified."""
    from app.strategy.training import training_status

    return {**training_status(ledger), "learner": learner.status()}


# ---------- auto-bet + continuous learning ----------

class AutoBetBody(BaseModel):
    enabled: bool


@app.get("/api/autobet")
def autobet_status():
    return {"ok": True, **autobet.status(), "order_gate": orders.status()}


@app.post("/api/autobet")
def autobet_toggle(body: AutoBetBody):
    """ON/OFF. OFF takes effect before the next order, even mid-cycle."""
    autobet.set_enabled(body.enabled)
    return {"ok": True, **autobet.status(), "order_gate": orders.status()}


class SniperBody(BaseModel):
    enabled: bool


@app.get("/api/sniper")
def sniper_status():
    return {"ok": True, **sniper.status(), "order_gate": orders.status()}


@app.post("/api/sniper")
def sniper_toggle(body: SniperBody):
    """Near-finish sniper ON/OFF (OFF by default). Respects PAPER/LIVE."""
    sniper.set_enabled(body.enabled)
    return {"ok": True, **sniper.status(), "order_gate": orders.status()}


@app.post("/api/sniper/run")
def sniper_run_now():
    if not sniper.enabled:
        raise HTTPException(409, "Sniper is OFF. Turn it on first.")
    return {"ok": True, "cycle": sniper.run_cycle(), **sniper.status()}


@app.post("/api/autobet/run")
def autobet_run_now():
    """Run one cycle now (only if Auto-bet is ON)."""
    if not autobet.enabled:
        raise HTTPException(409, "Auto-bet is OFF. Turn it on first.")
    return {"ok": True, "cycle": autobet.run_cycle(), **autobet.status()}


@app.get("/api/learn")
def learn_status():
    return {"ok": True, **learner.status()}


@app.post("/api/learn/run")
def learn_run_now():
    """One continuous-learning pass now: ingest, settle, gated refit, research refresh."""
    return {"ok": True, "record": learner.run_once(), **learner.status()}


@app.post("/api/training/refit")
def api_training_refit():
    """Refit on stored settled markets + settled bets; chronological holdout gate."""
    from app.strategy.training import train_and_gate, training_status

    record = train_and_gate(ledger, store=store)
    return {"ok": True, "record": record, "status": training_status(ledger)}


class IngestBody(BaseModel):
    days: int = Field(7, ge=1, le=60)
    per_day: int = Field(15, ge=1, le=50)
    max_markets: int = Field(100, ge=1, le=200)
    refit: bool = True


@app.post("/api/training/ingest")
def api_training_ingest(body: IngestBody):
    """Pull settled markets from Kalshi's public API (no keys), then refit."""
    from app.strategy.training import ingest_settled, train_and_gate, training_status

    try:
        report = ingest_settled(client, ledger, days=body.days, per_day=body.per_day, max_markets=body.max_markets)
    except Exception as e:
        store.log_activity("error", f"Settled ingest failed: {e}")
        raise HTTPException(502, f"Settled ingest failed: {e}")
    store.log_activity("model", f"Ingested {report['stored']} settled markets", {k: report[k] for k in ("listed", "eligible", "fetched", "stored")})
    record = train_and_gate(ledger, store=store) if body.refit else None
    return {"ok": True, "ingest": report, "record": record, "status": training_status(ledger)}


# ---------- bets ledger ----------

class SettleBody(BaseModel):
    result: str  # winning side: "yes" or "no"


@app.get("/bets")
@app.get("/api/bets")
def api_bets(settled: Optional[bool] = None):
    return {"ok": True, "bets": ledger.bets_sync(settled)}


@app.post("/api/bets/settle")
def api_bets_settle_auto():
    """Settle open bets whose markets Kalshi reports as settled (public reads)."""
    done = settle_open_bets(ledger, client)
    refit = None
    if done:
        store.log_activity("settle", f"Settled {len(done)} bet(s) from Kalshi results")
        refit = maybe_refit(ledger, store)
    return {"ok": True, "settled": done, "refit": refit}


@app.post("/api/bets/{bet_id}/settle")
def api_bet_settle(bet_id: int, body: SettleBody):
    try:
        res = ledger.settle_bet_sync(bet_id, body.result)
    except KeyError:
        raise HTTPException(404, "Bet not found")
    except ValueError as e:
        raise HTTPException(400, str(e))
    store.log_activity("settle", f"Bet {bet_id} settled: {body.result.upper()} won")
    refit = None if res.get("already_settled") else maybe_refit(ledger, store)
    return {"ok": True, **res, "refit": refit}


# ---------- trading ----------

class ArmBody(BaseModel):
    confirm_phrase: Optional[str] = None  # ignored; LIVE no longer needs typed ARM LIVE
    totp_code: Optional[str] = None


class OrderBody(BaseModel):
    ticker: str
    side: str
    count: int = Field(..., ge=1, le=MAX_CONTRACTS_PER_ORDER)
    # Max price in whole cents. Required: the gate refuses orders without one.
    price_cents: Optional[int] = None
    order_type: str = "limit"
    coherence: Optional[float] = None
    confirm_live: Optional[str] = None


@app.get("/api/trading/status")
def trading_status():
    return orders.status()


@app.post("/api/trading/arm")
def trading_arm(body: ArmBody):
    """Switch to LIVE (persisted). Typed phrase is ignored; optional TOTP still checked."""
    totp_ok = True
    if vault.unlocked and totp_mgr.has_seed():
        secret = totp_mgr.load_seed()
        totp_ok = totp_mgr.verify(secret or "", body.totp_code or "")
    result = orders.set_live(totp_ok=totp_ok)
    if result.get("ok"):
        save_dry_run_override(False, store)
    return result


@app.post("/api/trading/disarm")
def trading_disarm():
    """Switch to PAPER (persisted)."""
    result = orders.set_paper()
    save_dry_run_override(True, store)
    return result


@app.post("/api/trading/order")
def trading_order(body: OrderBody, request: Request):
    gate_geo = geo.check(body.ticker.strip(), geo.state_for_request(request))
    if not gate_geo["allowed"]:
        store.log_activity("geo", gate_geo["reason"], {"ticker": body.ticker})
        raise HTTPException(403, gate_geo["reason"])
    if not orders.dry_run:
        if body.confirm_live != "PLACE LIVE ORDER":
            raise HTTPException(
                400,
                "Real orders are armed. Confirm the real bet on screen before it is sent.",
            )
    snap = _LAST_SCAN_RUNS.get(body.ticker.strip())
    if snap is not None:
        try:  # a bet always gets a training snapshot (bypasses the hourly throttle)
            ledger.add_model_runs_sync([{**snap, "kind": "bet"}])
        except Exception as e:
            store.log_activity("error", f"Could not snapshot model run for bet: {e}")
    result = orders.place(
        ticker=body.ticker.strip(),
        side=body.side,
        count=body.count,
        price_cents=body.price_cents,
        coherence=body.coherence,
        order_type=body.order_type,
    )
    if result.get("ok") and not result.get("dry_run"):
        try:  # an API order now exists: the free Advanced upgrade can succeed
            tier.maybe_auto_upgrade(cooldown_s=60)
        except Exception:
            pass
    return result


# ---------- settings / vault / MFA ----------

class UnlockBody(BaseModel):
    passphrase: str
    totp_code: Optional[str] = None


class KeysBody(BaseModel):
    key_id: str
    pem: str
    passphrase: Optional[str] = None


class ClearKeysBody(BaseModel):
    passphrase: Optional[str] = None


class HostBody(BaseModel):
    host_key: str


class ModeBody(BaseModel):
    dry_run: Optional[bool] = None
    allow_live: Optional[bool] = None
    mode: Optional[str] = None  # "paper" | "live"
    totp_code: Optional[str] = None
    skip_live_confirm: Optional[bool] = None


class TotpSetupBody(BaseModel):
    passphrase: str


class TotpConfirmBody(BaseModel):
    secret: str
    code: str


@app.post("/api/vault/unlock")
def vault_unlock(body: UnlockBody):
    global _app_unlocked
    ok = vault.unlock(body.passphrase)
    if not ok:
        raise HTTPException(401, "Invalid passphrase")
    # If TOTP configured, require code
    if totp_mgr.has_seed():
        if not totp_mgr.verify(totp_mgr.load_seed() or "", body.totp_code or ""):
            vault.lock()
            raise HTTPException(401, "Invalid TOTP code")
    # Restore key id from vault if present
    kid = vault.get("kalshi_key_id")
    if kid:
        settings.key_id = kid
        os.environ["KALSHI_KEY_ID"] = kid
        _refresh_client()
    _app_unlocked = True
    store.log_activity("security", "Vault unlocked")
    return {"ok": True, "totp_configured": totp_mgr.has_seed()}


@app.post("/api/vault/lock")
def vault_lock():
    global _app_unlocked
    vault.lock()
    _app_unlocked = False
    store.log_activity("security", "Vault locked")
    return {"ok": True}


@app.post("/api/settings/keys")
def settings_keys(body: KeysBody):
    if not vault.unlocked:
        if body.passphrase:
            if not vault.unlock(body.passphrase):
                raise HTTPException(401, "Could not unlock vault")
        else:
            raise HTTPException(401, "Unlock vault first (or provide passphrase)")
    key_id = body.key_id.strip()
    pem = body.pem.strip()
    if not key_id:
        raise HTTPException(400, "Key ID is required")
    if not any(header in pem for header in ("BEGIN RSA PRIVATE KEY", "BEGIN PRIVATE KEY")):
        raise HTTPException(400, "PEM must contain an RSA PRIVATE KEY or PRIVATE KEY header")
    key_type = detect_key_type(pem)
    if key_type is None:
        raise HTTPException(400, "That private key could not be read. Kalshi keys are RSA or Ed25519.")
    path = vault.store_api_credentials(key_id, pem)
    _refresh_client()
    store.log_activity("security", "API credentials stored (encrypted id + chmod 600 PEM)")
    return {"ok": True, "key_path": str(path), "has_keys": settings.has_keys, "key_type": key_type}


@app.post("/api/settings/keys/clear")
def clear_settings_keys(body: ClearKeysBody):
    """Delete only local credentials; this never calls Kalshi's remote API."""
    if vault.exists() and not vault.unlocked:
        if body.passphrase:
            if not vault.unlock(body.passphrase):
                raise HTTPException(401, "Could not unlock vault")
        else:
            raise HTTPException(401, "Unlock vault first (or provide passphrase)")

    key_path = settings.key_path
    deleted = False
    try:
        if key_path.is_file():
            key_path.unlink()
            deleted = True
    except OSError as exc:
        raise HTTPException(500, f"Could not delete local key file: {exc}")

    if vault.unlocked:
        vault.delete("kalshi_key_id")
    settings.key_id = ""
    os.environ.pop("KALSHI_KEY_ID", None)
    _refresh_client()
    store.log_activity("security", "Local API credentials cleared (Kalshi key not remotely revoked)")
    return {
        "ok": True,
        "local_key_deleted": deleted,
        "has_keys": settings.has_keys,
        "remote_revocation_required": True,
    }


@app.post("/api/settings/mode")
def settings_mode(body: ModeBody):
    """Persist PAPER vs LIVE. LIVE trades for real once Kalshi keys exist."""
    if body.skip_live_confirm is not None and (
        body.dry_run is None and body.allow_live is None and body.mode is None
    ):
        save_skip_live_confirm(bool(body.skip_live_confirm), store)
        store.log_activity(
            "settings",
            "Skip LIVE confirm ON" if body.skip_live_confirm else "Skip LIVE confirm OFF",
        )
        return {"ok": True, "skip_live_confirm": bool(body.skip_live_confirm), **orders.status()}

    mode = (body.mode or "").strip().lower() or None
    if body.dry_run is None and body.allow_live is None and mode is None:
        raise HTTPException(400, "Send mode, dry_run, or allow_live")
    if body.dry_run is not None and body.allow_live is not None and body.dry_run == body.allow_live:
        raise HTTPException(400, "dry_run and allow_live disagree")
    if mode is not None:
        if mode not in {"paper", "live"}:
            raise HTTPException(400, 'mode must be "paper" or "live"')
        dry_run = mode == "paper"
    elif body.dry_run is not None:
        dry_run = bool(body.dry_run)
    else:
        dry_run = not bool(body.allow_live)

    if not dry_run:
        totp_ok = True
        if vault.unlocked and totp_mgr.has_seed():
            secret = totp_mgr.load_seed()
            totp_ok = totp_mgr.verify(secret or "", body.totp_code or "")
        if not totp_ok:
            raise HTTPException(401, "Extra lock code did not match.")
        result = orders.set_live(totp_ok=True)
    else:
        result = orders.set_paper()
    save_dry_run_override(dry_run, store)
    if body.skip_live_confirm is not None:
        save_skip_live_confirm(bool(body.skip_live_confirm), store)
    note = "PAPER mode" if dry_run else "LIVE mode"
    store.log_activity("settings", note)
    out = {"ok": True, **orders.status(), "skip_live_confirm": load_skip_live_confirm(store)}
    out.update({k: result[k] for k in ("ok",) if k in result})
    return out


@app.post("/api/settings/host")
def settings_host(body: HostBody):
    key = body.host_key.strip().lower()
    if key not in HOSTS:
        raise HTTPException(400, f"Unknown host_key. Choose from {list(HOSTS)}")
    settings.host_key = key
    os.environ["KALSHI_HOST"] = key
    store.set_setting("host_key", key)
    _refresh_client()
    store.log_activity("settings", f"API host switched to {key} → {settings.api_base}")
    return {"ok": True, "host_key": key, "host": settings.api_base}


@app.post("/api/mfa/setup")
def mfa_setup(body: TotpSetupBody):
    if not vault.unlocked:
        if not vault.unlock(body.passphrase):
            raise HTTPException(401, "Invalid passphrase")
    secret = totp_mgr.generate_secret()
    uri = totp_mgr.provisioning_uri(secret)
    # Do not save until confirm
    return {"ok": True, "secret": secret, "otpauth_uri": uri, "note": "Confirm with a code to save."}


@app.post("/api/mfa/confirm")
def mfa_confirm(body: TotpConfirmBody):
    if not vault.unlocked:
        raise HTTPException(401, "Unlock vault first")
    if not totp_mgr.verify(body.secret, body.code):
        raise HTTPException(400, "Code does not match secret")
    totp_mgr.save_seed(body.secret)
    store.log_activity("security", "TOTP seed saved to encrypted vault")
    return {"ok": True}


# ---------- activity ----------

@app.get("/api/activity")
def api_activity(limit: int = 50):
    return {
        "activity": store.recent_activity(limit),
        "orders": store.recent_orders(limit),
    }


@app.get("/api/health")
def health():
    return {"ok": True, "version": __version__}

# BUILD_NOTES — Kalshi Termux Portal

Built 2026-09-24 from Operator Gemini thread scraps + engine fragments + v0 portal look.

## Ported from Gemini / engine scraps

| Source | What we kept (sanitized) |
|--------|---------------------------|
| `kalshi_signed_client*.py` | RSA-PSS SHA-256 signing, header names, timestamp-ms payload layout |
| `ping_exchange.py` | Public `/trade-api/v2/exchange/status` probe |
| `live_engine_fragment.py` | Order payload shape (`action/buy`, `client_order_id`, `count`, `side`, `ticker`, `type`); **removed** `__main__` auto-fire test order |
| `ai_agent_ws.py` | Token-bucket rate limiter idea; Kelly fractional sizing; Nash + Monte Carlo structure; WS URL constants (WS client optional — REST poll primary for Termux) |
| `fragment_9e0cd94a.py` / markets fetch | Open markets scan, skip `CROSSCATEGORY`, prefer `*_dollars` price fields |
| `nash_blueprint.md` | Nash–Socratic stress questions, double-slit as **labeled simulation**, Kelly, coherence score concept |
| `engine_tree.md` | Capability map: auth paths, dry-run gap → closed with OrderGate |
| v0 `black_aurora` / `secure_client_portal` labels | Dark aurora visual language (cyan/purple) only — **no** Black Aurora / GOST / military copy in UI |

## Intentionally NOT ported into user-facing UI

- Ghostlight / Heimdall / Glyphstream / Hz carrier / Translilucense / SLHDS naming
- Fake “100% win” or guaranteed coherence claims
- Auto-live order on startup
- Real secrets from the Gemini `.env` paste (KEY_ID / GEMINI key) — **not** committed; Operator must supply keys locally

## New in this package

- FastAPI + single-page mobile UI (`templates/index.html` + `static/`)
- SQLite cache + activity/order log (`app/db/store.py`)
- Fernet vault + pyotp MFA (`app/security/`)
- Hard dry-run / arm-live gate with typed confirm (`app/trading/orders.py`)
- Host switch: `external` | `elections` | `demo` | `trading`
- `start.sh` binds `0.0.0.0:8787` and calls `termux-open-url` when available
- Honest DISCLAIMER + MIT license + `.gitignore` for secrets/data

## API host quirks (verified 2026-09-24)

- Both `external-api.kalshi.com` and `api.elections.kalshi.com` return live `exchange_active` and open markets.
- Docs recommend **external-api** for new integrations; elections host remains supported.
- Public `GET /trade-api/v2/markets?status=open` works without auth.
- Signature path should exclude query string (implemented in client).
- Balance field is in **cents** (divide by 100 for dollars) — matching engine scraps.

## Payout-first scan (2026-09-24)

Scan sorts by `best_payout_multiple` descending, then expected value / Gemini Nash payoff / half-Kelly allocation. Sizing exposes binary-contract edge (`p − price`) and Gemini half-Kelly on payout odds (`f* = (p·b − q) / b`, half of that, allocation = bankroll × fraction). Nash payoff `(p·b) − (1−p)` is labeled GO only above ~0.4. That is a heuristic, not a guaranteed win. Each enriched row includes an `explainer` object (win/lose, price, edge, payout math).

## ESPN scoreboard (shipped)

Full-game Kalshi series `KXNFLGAME`, `KXNBAGAME`, `KXMLBGAME`, `KXNHLGAME`, `KXNCAAFGAME`, and `KXNCAABGAME` can take model probability from the public ESPN scoreboard (`site.api.espn.com`, no API key). The matcher reads both clubs and the scheduled date out of `rules_primary`, allows a one-day timezone slip, and fuzzy-matches names (including `New York Y` / `Los Angeles C` / `NY Jets`).

- `espn_live` — game in progress (ESPN win probability when the scoreboard has one, otherwise a damped score lead) or final (scoreboard result, clamped so it is never 0 or 1).
- `espn_pre` — pregame only when a moneyline or a non-pick'em spread is on the board. The spread map is a rough logistic, not a book win probability.
- `market_shrink` — no game match, empty slate, postponed/canceled, or ESPN unreachable. Props, futures, and non-sports markets stay here.

Rows expose `prob_source` and `model_p_source` (`espn_live` | `espn_pre` | `market_shrink`) plus a short `espn_hint`. The scan merges nearby full-game markets into the existing book and still sorts payout-first. Scoreboard JSON is cached for 60 seconds. A final on ESPN can still settle differently at Kalshi. High payout is not a likely win.

## Live create-order (V2)

`POST /trade-api/v2/portfolio/orders` now returns HTTP 410 `deprecated_v1_order_endpoint`. Live placement uses `POST /trade-api/v2/portfolio/events/orders`. The app still thinks in buy YES/NO plus integer cents; the HTTP client maps that onto V2 `bid`/`ask` and a fixed-point YES price. Listing orders stays on `GET /trade-api/v2/portfolio/orders`. Paper bets do not call create-order.

### Rebuild hardening (Nov 2026, docs rechecked against OpenAPI 3.36.0)

- Folded PR #9 (V2 create-order) and PR #8 (tzdata) onto main.
- **Signing fix:** Kalshi's pre-sign text is `timestamp + METHOD + path` with no body. The old client appended the JSON body, which breaks signed POSTs. Fixed, plus Ed25519 support (Kalshi's default key type); key type is detected from the parsed key, not the PEM header.
- Max price is required; the implicit 99¢/1¢ IOC "market" order was removed. `type` must be `limit`, `ioc`, or `fok` (`market` is an alias for `ioc`, still needs a price).
- V2 passthrough bodies are validated: required fields, enums for `side` / `time_in_force` / `self_trade_prevention_type`, whole-cent price strings, whole-contract counts, `reduce_only` needs IOC, `expiration_time` needs GTC, unknown keys rejected.
- `KalshiAPIError` keeps status + Kalshi `code`/`message`; `friendly_order_error` maps them for the UI.
- Fixed-point reads: balance prefers `balance_dollars`; positions summarize `position_fp` / `market_exposure_dollars`; orders list reads `outcome_side`, `yes_price_dollars` / `no_price_dollars`, `fill_count_fp`, `remaining_count_fp`. Market scan already used `*_dollars` and `volume_fp` / `open_interest_fp` (integer cent fields were removed in the March 2026 migration).
- Nov 5 2026 fixed-point changes affect Perps / Clearing Member (`_fp` 6-decimal dollars) and fractional contracts on margin markets; event-market create-order is unchanged. This app sends whole cents / whole contracts, which are valid on every price grid.
- Default bind is now `127.0.0.1`.

## Remaining gaps

1. **Authenticated endpoints need Operator keys** (`KALSHI_KEY_ID` + `secrets/kalshi.key`). Without them: public scan/status work; balance/positions/live orders do not.
2. WebSocket orderbook listener is **not** wired into the UI (Termux-hostile); REST poll + short TTL cache used instead. WS URL constants remain in config for a future toggle.
3. Model probability uses an ESPN full-game match when team and date line up. Otherwise it stays a **conservative shrink-to-0.5** (`market_shrink`). No LLM dependency. Pregame spreads are a rough map, not a promise.
4. Demo host (`demo-api.kalshi.co`) not smoke-tested in this build environment. Kalshi now lists `external-api.demo.kalshi.co` as the primary demo host; `demo-api.kalshi.co` is still documented as supported.
6. Live create-order has only been tested against mocked HTTP. First real use: start with 1 contract at a low max price and check Orders.
5. Do not push secrets; coordinator handles GitHub publish.

## Smoke checklist

```bash
cd /workspace/kalshi-termux-app
python -c "from app.main import app; print('import ok', app.title)"
./start.sh   # then curl http://127.0.0.1:8787/api/ping
curl -s 'http://127.0.0.1:8787/api/markets/scan?limit=5' | head
```

## Post-boot fix (2026-09-24 afternoon)

Live open-market pages were dominated by multivariate `CROSSCATEGORY` combo contracts with `yes_ask_dollars=0`. Scanner initially enriched **0** rows.

**Fix:** default `mve_filter=exclude` on `GET /markets`, skip zero asks, paginate a few pages for headroom. Verified live: 100 enriched markets, many ≥2x payout singles (including extreme 1¢ tails).


## Prediction system (Architect plan, items 1–7)

Decisions below are the simplest option that meets each item; revisit if needed.

### 1. Persistence (`app/db/ledger.py`, aiosqlite)

- Tables `bets`, `model_runs`, `calibration` live in the existing `data/kalshi_portal.db` (gitignored). The old sync `Store` (cache/activity/orders/settings) is unchanged.
- HTTP handlers are sync, so each async ledger method has a `*_sync` twin (`run_sync`, safe inside or outside a running loop).
- `bets.model_prob` / `bets.price` are for the **chosen side**; `outcome` is 1 if that side won. `model_runs.model_prob` is P(YES).
- Every non-cached scan (`/api/markets/scan`) writes one `model_runs` row per enriched market. Cache hits do not re-write.
- "Every fill writes a bet": live acks with `fill_count > 0` write a bet (NO fill price = 1 − V2 `average_fill_price`, which is quoted on the YES book). **Paper bets are recorded as fills at the max price** so calibration can collect data without real money. `bets` has no paper/live column (spec schema); the orders log still has `dry_run`.
- Gap: a resting live order that fills later is not picked up (no fills poller yet).
- Settlement: `POST /api/bets/settle` checks open bets' markets via public `GET /markets/{ticker}` and settles those with `result` yes/no; `POST /api/bets/{id}/settle {"result": "yes"|"no"}` settles manually.

### 2. Calibration loop

- Every `settle_bet` recomputes the `calibration` table (10 buckets: `0-10` … `90-100`, by the bet's `model_prob`) and the Brier score over all settled bets.
- `GET /calibration` returns `{buckets, brier, n, uncalibrated, threshold}`.
- **Uncalibrated = Brier > 0.25 or no settled bets yet** (fail safe). The scanner flags `model_uncalibrated` and halves Kelly: both the fraction and the 5% cap are halved, so the final stake is exactly half; Gemini half-Kelly becomes quarter-Kelly.

### 3. Logistic model (`app/strategy/model.py`, `app/strategy/model_weights.json`)

- `z = w0 + w1*market_edge + w2*time_to_close + w3*volume_z + w4*espn_delta + w5*spread_z`, `p = sigmoid(z)` clamped to [0.01, 0.99]. This P(YES) now feeds Nash/Kelly for **every** market; the old shrink-toward-50% prior is gone (also from `nash_decision`'s no-model fallback).
- Feature choices: `market_edge` = logit of the book's P(YES) (mid of YES ask and 1 − NO ask) so a fitted model can track the market; `time_to_close` = days/30 clipped to [0,1]; `volume_z` = (log1p(volume) − 6)/3 and `spread_z` = (spread¢ − 3)/3 use fixed reference points (no batch statistics, so single-market lookups score the same as scans); `espn_delta` = logit(ESPN P) − logit(book P), 0 without a match. ESPN labels (`espn_live`/`espn_pre`/`market_shrink`) are kept as names; `market_shrink` now means "logistic model, no ESPN match".
- **Warning:** with every weight at 0.1 the starting model is nearly flat (a 90% book scores ≈ 58%). It disagrees hard with the market until it is refit on real outcomes, so Kelly will suggest fading favourites. Combined with item 2 (no history = uncalibrated = half Kelly) this is flagged, but do not trust live sizing until a refit and a passing backtest.
- Weights: defaults ship in `app/strategy/model_weights.json`; refits write `data/model_weights.json` (gitignored), which wins when present.
- Refit: full-batch gradient descent on mean log-loss, lr 0.01, 200 epochs, L2 0.001 (bias not penalised). Training rows = settled bets joined to the latest `model_runs` row for that ticker at bet time; label = YES outcome. Runs at most weekly, triggered after a settle. Needs ≥ 5 settled samples; a skipped refit is logged but does not start the 7-day clock. Every refit is logged (Python logger `kalshi.model`, activity log, and `refit_log` in the weights file, last 52 kept).
- lr 0.01 × 200 epochs moves weights slowly per refit; that is the spec's setting.

### 4. Backtest (`backtest.py`, `tests/fixtures/backtest_fixture.json`)

- Replays stored `model_runs` against stored outcomes: one row per ticker with a settled bet, using the latest model run at or before the first settled bet on it (so each market counts once). Each run is **re-scored with the weight set under test** from its stored features, so different weights can be compared on the same history. No live calls.
- Reports Brier, overall hit rate, hit rate/predicted/actual per bucket, and simulated P&L at 0.5 Kelly (fixed $1,000 bankroll, no compounding, 5% cap, buys whichever side has positive model edge vs the stored ask).
- Gate: `gate(candidate, baseline)` passes only if Brier is not worse. `python backtest.py --propose` fits proposed weights and exits 1 if they fail the gate. `POST /backtest/promote` writes `data/model_weights.json` only when the gate passes and real ledger history exists.
- **No real history yet**, so a 40-row synthetic fixture (seeded RNG, book-implied outcomes) lets the gate run in tests and `GET /backtest` falls back to it, labelled `source: fixture`. Promote refuses to write fixture-fitted weights.
- Fixture result (Nov 8 2026): current weights (all 0.1) Brier 0.2239, hit 70.0%, sim P&L −$900.80 over 39 bets; one proposed refit Brier 0.1735, hit 77.5%, sim P&L −$488.61; gate passes. The negative P&L is the honest result of a model still flatter than the market on data where the market is right; it is not tuned.

### 5. Nash fix (`app/strategy/nash.py`)

- The 2-player payoff matrix (`nash_decision`) is kept. New `softmax_equilibrium` / `mispricing_scores`: a logit quantal-response equilibrium at temperature 0.1 over the **top 10 scanned markets by volume**. Agents: one informed agent (model P) with one softmax over every BUY_YES_i / BUY_NO_i / PASS, plus one book agent per market (book P) weighted by that market's volume share. Net YES demand moves each price until flows cancel, so liquid markets stay near the book and thin ones lean to the model. Deterministic.
- `mispricing = |nash_prob − market_prob|` (market_prob = book mid). Rows outside the top 10 get `nash_prob`/`mispricing` = null.
- **Scanner default sort is now `mispricing`** (scored rows first, biggest mispricing first, the rest in payout order). This replaces the earlier "highest payout first" default because the Architect plan says to rank by mispricing; `sort=payout` and `sort=score` still work. `GET /scanner` = scan ranked by mispricing.
- Backtest gate: item 5 does not change model probabilities, so fixture Brier is unchanged (0.223942).

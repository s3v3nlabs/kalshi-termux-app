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

### 6. Geo-gate (`app/geo.py`, `app/geo_config.json`)

- Config maps all 50 states + DC to `{sports: bool}`; `UT, MI, NV, OH, NY, AZ, MD, MT` are `false`.
- Client IP = first `X-Forwarded-For` entry, else the socket peer. **No online IP lookup** (privacy, no phone-home): state comes from `ip_ranges` (CIDR → state, empty by default), or for loopback/private IPs from `GEO_LOCAL_STATE` env / `local_state`.
- **Fail closed:** unresolved IP, unknown state, missing/bad config → sports tickers blocked. Out of the box `local_state` is null, so a phone on `127.0.0.1` sees sports rows blocked until you set `GEO_LOCAL_STATE=PA` (your state) in `.env` or the config. Non-sports markets are never blocked.
- Sports ticker = ESPN full-game series (`KXNFLGAME`…) or a `sports_prefixes` match.
- Scan rows get a `geo` object (`allowed`, `state`, `reason`) and stay visible so the UI can dim them; orders on a blocked sports ticker return HTTP 403 with the reason (paper and live alike). `/api/markets/{ticker}` returns `geo` too.
- XFF is client-controlled without a trusted proxy; this is a compliance guard, not anti-spoofing.

### 7. Hardening

- **Vault KDF:** new vaults derive the Fernet key with scrypt (n=2^15, r=8, p=1, 16-byte salt); params are stored in the vault file. Old PBKDF2 vaults still open and are re-encrypted with scrypt + a fresh salt on the first successful unlock. If `VAULT_PASSPHRASE` is set, the vault is unlocked at startup (and the saved Key ID loaded). Otherwise unlock stays manual in Settings.
- **Retry wrapper** in `KalshiClient.request`: up to 5 tries, exponential backoff (0.5 s × 2^n, capped at 30 s) with jitter in [base/2, base], `Retry-After` honored (seconds or HTTP date, capped at 30 s). Each retry re-checks the rate limiters and re-signs with a fresh timestamp. GETs retry on network errors, 429, 500/502/503/504. **Writes (create-order) retry only on 429**, never on a network error or 5xx, so a bet cannot be sent twice when the first attempt's result is unknown.
- **Rate limiter:** new `GlobalRateLimiter` caps every Kalshi request at 8 req/s (burst 8), on top of the existing read/write buckets. Those older buckets are unchanged (not renamed); their sustained read rate (2/s after the initial burst) can still be the tighter limit on long scans.

## Operator console (frontend screens 1–7)

All in the existing `templates/index.html`, `static/js/app.js`, `static/css/app.css`. Vanilla JS, no CDN, npm or build step; existing dark palette variables. Polling only (no websockets): scanner every 5 s, calibration every 30 s, and only while that panel is on screen and the tab is visible. The ARM strip refreshes bankroll and today's P&L every 30 s.

1. **Scanner** fetches `GET /scanner` (already ranked by mispricing on the server; the filter chips only hide rows). Columns: ticker, market P(YES) (book mid), model P(YES), Nash P, edge = model − market in pp (green +, red −), Kelly stake (`kelly.allocation_usd`, already halved when uncalibrated), geo, plus a Buy button that opens the Bet panel prefilled. A row click expands the z breakdown (x, w, w·x per input, bias, z, p, mispricing) using the `model_weights` now returned with each scan. The header is sticky (below the ARM strip). Past 50 rows the list is windowed: absolutely positioned rows, prefix-sum offsets, 6 rows overscan, fixed row heights (48 px desktop, 128 px card on phones, plus a fixed detail height when expanded). Decision: fixed heights over measuring, because it is the simplest correct windowing.
2. **Calibration** reads `GET /calibration`: big Brier, inline SVG reliability curve (bucket midpoint on X; predicted and actual on Y; dashed diagonal = perfect), dot size by n. The red banner appears when Brier > 0.25; an amber note appears when there is no settled history yet (that case is also treated as uncalibrated).
3. **Bet log** reads the new `GET /bets` alias of `/api/bets`. It filters by outcome (open/won/lost) and ticker, shows an SVG running-P&L sparkline over settled bets, and shows model_prob and market_prob per bet. Settle buttons ("YES won" / "NO won", with a confirm) call `POST /api/bets/{id}/settle`, which writes outcome and pnl and then refits if due. "Check Kalshi results" calls the auto-settle route.
4. **ARM strip** is a sticky top bar showing mode (DRY RUN / LIVE READY / LIVE), bankroll (only when a key exists and the balance call works), today's settled P&L (local date), and the ARM LIVE input (plus a code field if TOTP is set). It posts to the same `/api/trading/arm` / `/disarm` endpoints, so the gate is unchanged: live must be allowed in Settings first (the strip sends you there), the phrase is checked server side, and the session-only arm and per-order confirm stay. When armed, `body.live-armed` draws a red border around the whole viewport. The Settings arm card is kept.
5. **Geo** shows a green OK / red BLOCKED XX badge per row. Blocked rows are dimmed and Buy is disabled, with a tooltip giving the server's reason (which names the state). The Bet panel also disables Place and shows the reason, using the `geo` from `/api/markets/{ticker}`. The server 403 stays the real gate.
6. **Backtest** runs `GET /backtest` and shows current vs proposed side by side: Brier, hit rate, P&L at ½ Kelly, bets/wins, P&L sparkline, weights with Δ, and hit rate per bucket. Promote is enabled only when `gate_pass` is true and the source is the ledger (the fixture can't be promoted; the server also refuses with 400/409).
7. **Mobile**: under 600 px the scanner table becomes cards (labelled cells, 3-column grid), tap targets are ≥ 44 px everywhere, and filter chips wrap instead of side-scrolling. The bottom bar is Markets / Calib / Bets / Backtest / More, and More opens Bet / Kelly / Account / Settings / Log. A Playwright check at 360 and 412 px (Termux phone widths) and 1280 px found, on every panel: document scrollWidth = clientWidth, no element past the right edge, no control under 44 px, and no JS errors.

- Tests: `/bets` + settle route (`tests/test_ledger.py`); Promote refuses the fixture (`tests/test_backtest.py`).

## Gemini payout-anomaly ("hidden gem") layer (`app/strategy/gems.py`)

From the ai_agent.py scrap. Computed per scan row for the side the scanner leads with, the higher-payout side (`best_side`):

- `spread = yes_ask − yes_bid` in cents (from `yes_*_dollars` or the cent fields; the existing YES+NO−1 parity spread when no bid is published). `spread_compression = 1 / (spread + 1)`; `vol_oi_ratio = volume / (open_interest + 1)`.
- **implied_prob decision:** the scrap used `yes_bid / 100`. The rebuild uses the **ask of the side you would buy** (YES ask, or NO ask for a NO buy). You pay the ask, so that is the price the payout is based on. The bid is what you could sell at, and using it would overstate the payout. It also means only an ask is ever needed. `payout_odds = 1 / implied_prob`, or 2.0 when implied_prob ≤ 0 (as in the scrap). This equals the existing `best_payout_multiple`.
- Payout tier: `2x` ≤ 3x, `13x` ≥ 10x, `mid` otherwise.
- Gemini half-Kelly is kept as is: p clipped to [0.01, 0.99], `kelly_f = (p·odds − q) / odds`, `fractional = max(0, 0.5·kelly_f)`, also halved again when the model is uncalibrated. Nash `p·odds − (1−p)` with GO > 0.4 is also kept.
- **Gem rule** (every check must pass): tier `13x` (≥ 10x) **and** spread_compression ≥ 0.25 (spread ≤ 3¢) **and** nash_payoff > 0.4 **and** model p ≥ implied_prob + 2 points **and** half-Kelly > 0. p is the logistic model's probability for that side, which includes the ESPN delta.
  - The thresholds are the simplest round numbers matching the scrap's "13x" and "tight book" intent; they live as constants in `gems.py`.
  - Half-Kelly > 0 is already implied by Nash > 0.4, but it is kept as a separate check because the spec lists it.
- `gem_status` is `"unverified"` while the model is uncalibrated (Brier > 0.25 or no settled bets), otherwise `"candidate"`. The UI shows **◆ GEM?** (dashed amber) for unverified and **◆ GEM** for candidate. Copy says it is a screen for odd prices, not a sure thing. There is no "guaranteed" or "lock" wording, and a test checks for it.
- The default sort stays mispricing. The new "Gems (13x anomalies)" filter chip only hides rows. The row expand adds the Gemini block (spread, spread_compression, vol_oi_ratio, implied_prob, payout_odds, payout_tier, model p, edge vs implied, nash_payoff, half-Kelly) and a ✓/✗ per check under the z-input table.
- The geo gate is unchanged and fails closed; a gem on a blocked sports ticker still has Buy disabled. There is no location spoofing or bypass.
- **Live smoke (Nov 8 2026, 11:37 PM ET):** 288 rows; 12 in the 13x tier; 8 flagged (4 of them in the 168-row UI view), all "unverified". They come from the flat starting weights: 1–5¢ tails get model p 0.34–0.56, so almost every tight-spread 13x tail passes. Examples are both sides of a likely-decided NFL game at 20x, and 100x metals and props. **Until the model is refit on settled bets and calibrated, treat gems as leads to check by hand, not signals.**

## Upgrade phase 1 — training on settled markets (Nov 9 2026)

Goal: stop scoring 1–5¢ tails at 34–56% with flat 0.1 weights.

- **Ingest (`app/strategy/training.py`, `train.py`)**: public `GET /trade-api/v2/markets?status=settled&mve_filter=exclude`, one close-day at a time via `min_close_ts`/`max_close_ts` (limit 1000 per day). `external-api.kalshi.com` returns 403 to this box, so the client's existing public fallback to `api.elections.kalshi.com` serves it. No keys. Kept: binary, result yes/no (scalar/void dropped), volume ≥ 50, lived ≥ 2 h, not CROSSCATEGORY. Sample per day is deterministic (sha1 of ticker), max 3 per series per day for diversity. Category comes from `GET /series/{s}` (cached per run).
- **Ingest decision (leakage):** a settled market's own prices are post-settlement (last 1¢/99¢, bid 0/ask 1), so they would leak the outcome. The pre-close book is rebuilt from public hourly candlesticks (`/series/{s}/markets/{t}/candlesticks`): a snapshot at **1 h, 6 h and 24 h before close** = the last candle ending at least that long before close (walk back ≤ 2 candles for a quoted book; needs 0 < ask < 1 and 0 ≤ bid < ask). Volume at snapshot = final volume − volume in later candles; OI = the candle's OI.
- **Features:** same `build_features` as the live scanner with `now` = snapshot time. `market_p_yes` = (YES ask + YES bid)/2, the same mid the scanner uses (NO ask = 1 − YES bid). `espn_delta` (and later context terms) are stored as **null**; `model.feature_value` turns null into 0, so that term drops out of z for those rows. The gem layer's `implied_prob` is untouched (ask of the side you buy).
- **Storage:** new tables `settled_markets` (ticker, event, series, category, title, open/close/settlement time, result, settlement value, last price, volume, OI) and `settled_snapshots` (ticker, horizon, snapshot time, bid/ask, volume, OI, features) in the same `data/kalshi_portal.db` via the existing aiosqlite `Ledger`. `ledger_meta` holds migration flags and `last_ingest`.
- **Holdout:** chronological by market close time; the last 20% of markets is the holdout, and all snapshots of one market stay on the same side. Real settled bets are added as rows (close time = bet time).
- **Gate:** fit on train (lr 0.01, L2 0.001, same GD), score current and proposed on the holdout with `backtest.run_backtest`, promote only if `backtest.gate` passes **and** holdout Brier is strictly lower. Needs ≥ 50 markets and ≥ 10 in holdout; otherwise weights are kept and the reason is logged. Result goes into the weights file (`last_training`, and `refit_log` when promoted) and the activity log.
- **Epochs decision:** the settled path defaults to **2000 epochs** (weekly bet refit stays at 200). On the live sample, 200 epochs left w1 at 0.56 and holdout Brier at 0.149 (worse than the book's own 0.137); 2000 reached w1 ≈ 0.93 and 0.134.
- **Calibration:** now also needs ≥ 20 settled bets (`MIN_CALIBRATION_N`); fewer counts as "insufficient sample" = uncalibrated, Kelly halved, gems ◆ GEM?. Holdout Brier on settled markets gates weights but does **not** verify gems.
- **model_runs bloat fixed:** the scanner no longer logs a row per market per poll. `/api/markets/scan` calls `Ledger.add_model_snapshots`, which writes at most **one row per ticker per hour** (`SNAPSHOT_INTERVAL_S`). A placed order (paper or live) forces a `kind='bet'` snapshot from the latest scan rows (no network call). A one-time migration adds `model_runs.kind` and compacts old rows to the last per ticker per hour (bet rows are never compacted); `python train.py --compact` reruns it.
- **Operator path:** `python train.py --ingest` (CLI), `GET /api/training`, `POST /api/training/refit`, `POST /api/training/ingest` (≤ 200 markets). The Backtest screen has a "Settled-market training" card; the Calibration screen shows a one-line training summary.
- **Live run (Nov 9 2026, ~1:00 AM ET):** 21 days listed 20,000 settled markets → 8,085 eligible → 400 fetched → **397 stored, 871 snapshots** (129 YES). Holdout 79 markets / 179 rows (closes from Nov 6). Holdout Brier **0.2924 → 0.1337** (book mid alone 0.1375), hit 60.9% → 81.0%, sim P&L at ½ Kelly −$3,866 → +$227. Gate passed; promoted as v1 to `data/model_weights.json`: w0 −0.144, w1 0.927, w2 0.095, w3 −0.026, w4 0.098 (no ESPN history, so unchanged by the fit), w5 −0.012. A 3¢ tail now scores ≈ 3.6–4%, not 34–56%. The ingest took ~9 min because of the existing read rate limiter.
- Fixture: `tests/fixtures/settled_markets_fixture.json` holds 6 real public markets (3 usable, 1 scalar, 1 zero-volume, 1 short-lived) and their candles, trimmed to parsed fields.

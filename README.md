# Kalshi Termux Portal

Mobile-first **Kalshi prediction-market** web app for Termux → browser.

- **Public** market scan + exchange status (no keys)
- **Authenticated** balance / positions / orders via RSA-PSS (`KALSHI_KEY_ID` + PEM)
- **Strategy**: high-payout scanner, fractional **Kelly**, **Nash** payoff matrix, optional Monte Carlo sim
- **Safety**: fresh install uses **paper bets** until you tap **Allow live trading**. Real orders still need typed **ARM LIVE** (+ optional extra lock code). No `.env` edit required.
- **Local security**: Fernet vault, `chmod 600` key file, Aegis-style TOTP — no phone-home

> Not financial advice. Prediction markets involve risk of loss. **No 100% win-rate claims.**

## Termux one-shot install

PyPI `cryptography` crashes on Termux (`PyBaseObject_Type` / `_rust.abi3.so`). Install Termux `python-cryptography`, create the venv with `--system-site-packages`, then uninstall the pip overlay that `requirements.txt` installs. Full notes: [docs/TERMUX.md](docs/TERMUX.md).

```bash
pkg update -y
pkg install -y python git python-cryptography
git clone https://github.com/singular-ty/kalshi-termux-app.git
cd kalshi-termux-app
python -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -r requirements.txt
pip uninstall -y cryptography
python -c "from cryptography.hazmat.primitives import hashes; print('ok')"
cp .env.example .env
chmod +x start.sh && ./start.sh
```

If the import check still fails, preload Termux libpython (matches current `python` 3.13):

```bash
LD_PRELOAD=$PREFIX/lib/libpython3.13.so ./start.sh
```

Then open the printed URL (or run `termux-open-url http://127.0.0.1:8787`).
If the browser shows connection refused, wait until Uvicorn says it is running, then refresh.

`requirements.txt` includes PyPI `tzdata` because Termux often has no system time-zone data. If you see `ZoneInfoNotFoundError: 'No time zone found with key America/New_York'`, run `pip install tzdata` (or reinstall from `requirements.txt`). See [docs/TERMUX.md](docs/TERMUX.md).

Optional: put the RSA PEM at `secrets/kalshi.key` (`chmod 600`) and set `KALSHI_KEY_ID` in `.env`.

### Without venv

Install `python-cryptography` from `pkg` and remove any pip-installed `cryptography` so the system module stays in place:

```bash
pkg install -y python-cryptography
pip install --user -r requirements.txt
pip uninstall -y cryptography
PYTHONPATH=. python -m uvicorn app.main:app --host 0.0.0.0 --port 8787
```

## Desktop / this machine

```bash
cd /workspace/kalshi-termux-app
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
./start.sh
```

If port `8787` is busy: `PORT=8788 ./start.sh`.

## Config

| Variable | Meaning |
|----------|---------|
| `KALSHI_HOST` | `external` (default) · `elections` · `demo` · `trading` |
| `KALSHI_KEY_ID` | API key id |
| `KALSHI_KEY_PATH` | PEM path (default `secrets/kalshi.key`) |
| `DRY_RUN` | Boot default only. `1` means paper bets until Settings → **Allow live trading** saves a local override |
| `KELLY_FRACTION` | fraction of full Kelly (default `0.5`) |
| `KELLY_MAX_FRACTION` | hard cap of bankroll per trade (default `0.05`) |
| `CACHE_TTL_SECONDS` | scanner cache (default `10`) |

API hosts (Trade API v2):

- Recommended: `https://external-api.kalshi.com`
- Also supported: `https://api.elections.kalshi.com`
- Demo: `https://demo-api.kalshi.co`

## Trading mode

A new install uses **paper bets (no money)** until you tap **Allow live trading** in Settings and confirm. That choice is saved on the phone (`data/ui_prefs.json`, gitignored). Real orders still require typing **ARM LIVE** once per session. **Disarm** pauses real orders. **Paper bets only** turns live trading back off.

## API keys (required for Account / live)

A GitHub clone contains no Kalshi credentials. Account data and live trading require a Kalshi **Key ID** plus its private PEM.

1. Log in at [Kalshi](https://kalshi.com/account/profile), open **profile/account → API Keys**, and select **Create New API Key**.
2. Save the Key ID and download the PEM immediately. Kalshi does not let you retrieve the private key later.
3. In **Settings**, paste the Kalshi Key ID and private key text, then choose **Save on this phone**. Alternatively, set `KALSHI_KEY_ID` in your local `.env` and put the PEM at `secrets/kalshi.key` with restrictive permissions.
4. Confirm the Settings badge changes from **No keys** to **Saved on this phone** or **Unlocked**, then test Account.
5. After the replacement key works, return to the same **API Keys** list and **delete/revoke** the old key.

The **Clear local keys** button only removes the local PEM and runtime/vault Key ID; it does **not** revoke a key at Kalshi. Revoke it separately on Kalshi.com. See [docs/KALSHI_API_KEYS.md](docs/KALSHI_API_KEYS.md) for create, rotate, and delete details. Never commit `.env`, PEM files, or real key material.

## Panels

1. **Markets** — scan sorts highest payout multiple first; sizing uses Gemini half-Kelly on payout odds plus Nash payoff, with a plain-English odds explainer (not a guaranteed win)  
2. **Contract** — detail + paper/live order gate  
3. **Nash / Kelly** — payoff matrix + Monte Carlo simulation  
4. **Account** — balance / positions / orders (keys required)  
5. **Settings** — Allow live trading, app lock password, key on this phone, then **ARM LIVE**  
6. **Activity** — SQLite activity + order log  

## Auth signing (Kalshi v2)

```
timestamp_ms + METHOD + path_without_query + optional_body
→ RSA-PSS SHA-256 (salt = DIGEST_LENGTH) → base64
Headers: KALSHI-ACCESS-KEY / -SIGNATURE / -TIMESTAMP
```

## License

MIT — see `LICENSE`. Read `DISCLAIMER.md` before trading.

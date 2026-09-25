# Termux install

PyPI `cryptography` crashes on Termux (`PyBaseObject_Type` / `_rust.abi3.so`). Install Termux `python-cryptography`, create the venv with `--system-site-packages` so that package is visible, then uninstall the pip overlay that `requirements.txt` installs.

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

The import check should print `ok`. Then open the printed URL (or run `termux-open-url http://127.0.0.1:8787`).

Optional: put the RSA PEM at `secrets/kalshi.key` (`chmod 600`) and set `KALSHI_KEY_ID` in `.env`. See [KALSHI_API_KEYS.md](KALSHI_API_KEYS.md).

## Trading mode

A fresh install uses **paper bets (no money)** until you choose otherwise. You do not edit `.env`.

1. Open **Settings** and tap **Allow live trading**. Confirm the warning.
2. Set the **app lock password**, paste your Kalshi Key ID and private key text, then **Save on this phone**.
3. Type **ARM LIVE** before any real order. That step is what can spend your Kalshi balance.
4. **Disarm** stops real orders and leaves live trading allowed. **Paper bets only** turns real trading off.

`DRY_RUN=1` is only the boot default when no saved choice exists. The app stores the choice under local `data/` (not committed).

## If the import still fails

Preload Termux libpython. The filename matches current Termux `python` (3.13):

```bash
LD_PRELOAD=$PREFIX/lib/libpython3.13.so ./start.sh
```

## Without venv

Install `python-cryptography` from `pkg` and remove any pip-installed `cryptography` so the system module stays in place:

```bash
pkg install -y python-cryptography
pip install --user -r requirements.txt
pip uninstall -y cryptography
PYTHONPATH=. python -m uvicorn app.main:app --host 0.0.0.0 --port 8787
```

If `GET /` returns 500 with `TypeError: unhashable type: 'dict'`, update `app/main.py` or `git pull` so `TemplateResponse` passes `request` first (Starlette 1.x).

## Time zones

Android/Termux Python often has no system IANA time-zone database. `app/strategy/espn.py` needs `America/New_York`. `requirements.txt` includes PyPI `tzdata`, so `pip install -r requirements.txt` (the steps above) installs it.

If startup or an ESPN scan fails with `ZoneInfoNotFoundError: 'No time zone found with key America/New_York'`, install the package and start again:

```bash
pip install tzdata
```

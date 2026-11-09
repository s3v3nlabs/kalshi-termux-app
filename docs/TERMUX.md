# Termux install

PyPI `cryptography` crashes on Termux (`PyBaseObject_Type` / `_rust.abi3.so`). Install Termux `python-cryptography`, create the venv with `--system-site-packages` so that package is visible, then uninstall the pip overlay that `requirements.txt` installs.

On Python 3.14 (current Termux) `pydantic-core` and the `uvicorn[standard]` extras have no prebuilt Android wheels, so pip compiles them with Rust. Install Termux's Rust toolchain and set the Android API level **before** `pip install`, or the build fails with `Target triple not supported by rustup: aarch64-unknown-linux-android`. The first build takes about 5–15 minutes; keep Termux open.

```bash
pkg update -y
pkg install -y python git python-cryptography
pkg install -y rust maturin binutils
export ANDROID_API_LEVEL=24
git clone https://github.com/s3v3nlabs/kalshi-termux-app.git
cd kalshi-termux-app
python -m venv --system-site-packages .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip uninstall -y cryptography
python -c "from cryptography.hazmat.primitives import hashes; print('ok')"
cp .env.example .env
chmod +x start.sh && ./start.sh
```

The import check should print `ok`. Then open the printed URL (or run `termux-open-url http://127.0.0.1:8787`).

Optional: put the RSA PEM at `secrets/kalshi.key` (`chmod 600`) and set `KALSHI_KEY_ID` in `.env`. See [KALSHI_API_KEYS.md](KALSHI_API_KEYS.md).

## Trading mode

A fresh install starts on **LIVE**. You do not edit `.env` to change mode.

1. Top strip **PAPER | LIVE** — PAPER is instant; LIVE confirms once (or instantly if Skip LIVE confirm is on).
2. Set the **app lock password**, paste your Kalshi Key ID and private key text, then **Save on this phone**.
3. Without keys, LIVE shows **Live — add Kalshi keys in Settings** and will not silently paper-trade.
4. Optional extra lock code (TOTP) still applies when switching to LIVE if you set one up.

`DRY_RUN=0` is the boot default when no saved choice exists. The app stores PAPER|LIVE under local `data/` (not committed).

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
PYTHONPATH=. python -m uvicorn app.main:app --host 127.0.0.1 --port 8787
```

If `GET /` returns 500 with `TypeError: unhashable type: 'dict'`, update `app/main.py` or `git pull` so `TemplateResponse` passes `request` first (Starlette 1.x).

## Time zones

Android/Termux Python often has no system IANA time-zone database. `app/strategy/espn.py` needs `America/New_York`. `requirements.txt` includes PyPI `tzdata`, so `pip install -r requirements.txt` (the steps above) installs it.

If startup or an ESPN scan fails with `ZoneInfoNotFoundError: 'No time zone found with key America/New_York'`, install the package and start again:

```bash
pip install tzdata
```

## pydantic-core / Rust build fails

If `pip install -r requirements.txt` stops at `Failed to build 'pydantic-core'` (or uvicorn is missing afterwards), the Rust toolchain was not set up. Run, with the venv active:

```bash
pkg install -y rust maturin binutils
export ANDROID_API_LEVEL=24
pip install --upgrade pip
pip install -r requirements.txt
```

`export ANDROID_API_LEVEL=24` only lasts for the current Termux session; set it again in a new session before reinstalling. If the build still fails, this skips the Rust-built `uvicorn[standard]` extras (the app runs on plain uvicorn):

```bash
pip install fastapi uvicorn requests python-dotenv pyotp jinja2 pydantic tzdata aiosqlite
```

## Auto-bet and continuous learning on the phone

Both run inside the server process, so they only work while Termux is running `./start.sh`. Run `termux-wake-lock` first so Android does not suspend Termux with the screen off. Auto-bet is OFF until you tap **AUTO** in the top strip; it places real orders only while mode is LIVE and Kalshi keys exist. Set `GEO_LOCAL_STATE` in `.env` or every sports market stays blocked (also for Auto-bet).

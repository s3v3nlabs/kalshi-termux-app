#!/usr/bin/env bash
# Kalshi Termux Portal launcher
set -euo pipefail
cd "$(dirname "$0")"

export PYTHONPATH="${PYTHONPATH:-}:$(pwd)"
# 127.0.0.1 keeps the portal (and LIVE mode) private to this phone.
# HOST=0.0.0.0 ./start.sh exposes it to everyone on your Wi-Fi; only do that on a trusted network.
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8787}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[-] python3 not found. On Termux: pkg install python"
  exit 1
fi

# Prefer venv if present
if [ -d .venv ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

echo "[*] Kalshi Termux Portal"
echo "[*] Binding http://${HOST}:${PORT}"
echo "[*] Open on this device after Uvicorn says it is running:"
echo "    http://127.0.0.1:${PORT}"

# Fail clearly if port busy
if command -v ss >/dev/null 2>&1; then
  if ss -ltn 2>/dev/null | grep -q ":${PORT} "; then
    echo "[!] Port ${PORT} appears busy. Set PORT=8788 ./start.sh or kill the other process."
  fi
fi

# Open the browser AFTER the port accepts connections (fixes Termux race:
# termux-open-url used to fire before uvicorn listened → connection refused).
open_when_ready() {
  local url="http://127.0.0.1:${PORT}"
  local i=0
  while [ "$i" -lt 60 ]; do
    if command -v python3 >/dev/null 2>&1; then
      if python3 -c "import socket; s=socket.create_connection(('127.0.0.1', ${PORT}), 0.4); s.close()" 2>/dev/null; then
        if command -v termux-open-url >/dev/null 2>&1; then
          echo "[*] Server is up — opening ${url}"
          termux-open-url "$url" || true
        else
          echo "[*] Server is up — open ${url}"
        fi
        return 0
      fi
    fi
    i=$((i + 1))
    sleep 0.25
  done
  echo "[!] Timed out waiting for port ${PORT}. Check the Uvicorn log above."
}

open_when_ready &

# Prefer asyncio loop on Android/Termux (uvloop from uvicorn[standard] can fail there)
exec python3 -m uvicorn app.main:app --host "$HOST" --port "$PORT" --loop asyncio --log-level info

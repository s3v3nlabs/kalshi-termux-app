#!/usr/bin/env python3
"""Optional remote math box for the Kalshi Termux app (REMOTE_COMPUTE_URL).

Run it on a computer you own (laptop/desktop/home server):

    python tools/remote_compute_server.py --host 0.0.0.0 --port 8765

then set REMOTE_COMPUTE_URL=http://<that-computer>:8765/compute in the app's
.env. It ONLY turns game-state numbers into model probabilities. It holds no
keys, makes no network calls (Kalshi or otherwise) and places no orders; all
Kalshi traffic stays on the phone's single client and rate budget.
Standard library only (plus this repo's app.trading.sniper math).
"""
from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.trading.sniper import PROB_FIELDS, sniper_prob  # noqa: E402

MAX_ITEMS = 5000


def compute(body: dict) -> dict:
    if body.get("kind") != "sniper_prob":
        raise ValueError("unknown kind")
    items = body.get("items") or []
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise ValueError("items must be a list (max 5000)")
    return {"results": [sniper_prob({k: it.get(k) for k in PROB_FIELDS}) for it in items]}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        try:
            n = int(self.headers.get("Content-Length") or 0)
            out = compute(json.loads(self.rfile.read(min(n, 5_000_000)) or b"{}"))
            code = 200
        except Exception as exc:
            out, code = {"error": str(exc)}, 400
        data = json.dumps(out).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    print(f"remote compute on http://{a.host}:{a.port}/compute (math only, no Kalshi calls)")
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()

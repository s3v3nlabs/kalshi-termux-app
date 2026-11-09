"""Geo-gate for sports contracts (spec item 6). Fails closed.

Client IP = first address in X-Forwarded-For, else the socket peer.
State resolution (no online lookups):
  1. ``ip_ranges`` CIDR -> state in ``app/geo_config.json``
  2. loopback / private / link-local IP -> ``GEO_LOCAL_STATE`` env or ``local_state``
  3. otherwise unresolved
A sports ticker is allowed only when the resolved state exists in the config
with ``sports: true``. Unresolved IP, unknown state, or bad config -> blocked.

X-Forwarded-For is set by the client unless a trusted proxy rewrites it, so
this is a compliance guard for an honest setup, not anti-spoofing.
"""
from __future__ import annotations

import ipaddress
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

from app.strategy.espn import is_full_game_market

CONFIG_PATH = Path(__file__).with_name("geo_config.json")


@lru_cache(maxsize=4)
def load_config(path: Optional[str] = None) -> dict:
    try:
        return json.loads(Path(path or CONFIG_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}  # fail closed: no states -> sports blocked everywhere


def client_ip(headers, peer: Optional[str]) -> Optional[str]:
    xff = ""
    if headers is not None:
        xff = headers.get("x-forwarded-for") or headers.get("X-Forwarded-For") or ""
    first = xff.split(",")[0].strip() if xff else ""
    candidate = first or (peer or "")
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def resolve_state(ip: Optional[str], config: Optional[dict] = None) -> Optional[str]:
    cfg = config if config is not None else load_config()
    if not ip:
        return None
    addr = ipaddress.ip_address(ip)
    for cidr, state in (cfg.get("ip_ranges") or {}).items():
        try:
            if addr in ipaddress.ip_network(cidr, strict=False):
                return str(state).upper()
        except ValueError:
            continue
    if addr.is_loopback or addr.is_private or addr.is_link_local:
        local = os.getenv("GEO_LOCAL_STATE") or cfg.get("local_state")
        return str(local).strip().upper() if local else None
    return None


def is_sports_ticker(ticker: str, config: Optional[dict] = None) -> bool:
    cfg = config if config is not None else load_config()
    t = (ticker or "").upper()
    if is_full_game_market({"ticker": t}):
        return True
    return any(t.startswith(p.upper()) for p in cfg.get("sports_prefixes") or [])


def sports_allowed(state: Optional[str], config: Optional[dict] = None) -> bool:
    cfg = config if config is not None else load_config()
    entry = (cfg.get("states") or {}).get((state or "").upper())
    return bool(entry and entry.get("sports") is True)


def check(ticker: str, state: Optional[str], config: Optional[dict] = None) -> dict:
    """{allowed, state, sports, reason} for one ticker."""
    cfg = config if config is not None else load_config()
    sports = is_sports_ticker(ticker, cfg)
    if not sports:
        return {"allowed": True, "state": state, "sports": False, "reason": None}
    if sports_allowed(state, cfg):
        return {"allowed": True, "state": state, "sports": True, "reason": None}
    if not state:
        reason = "Sports blocked: your state could not be determined (set GEO_LOCAL_STATE or local_state)."
    else:
        reason = f"Sports contracts are blocked in {state}."
    return {"allowed": False, "state": state, "sports": True, "reason": reason}


def state_for_request(request) -> Optional[str]:
    peer = request.client.host if getattr(request, "client", None) else None
    return resolve_state(client_ip(request.headers, peer))

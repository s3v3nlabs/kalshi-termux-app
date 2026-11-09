"""Item 6: geo-gate for sports tickers. Fail closed."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import geo

BLOCKED = ["UT", "MI", "NV", "OH", "NY", "AZ", "MD", "MT"]


def test_config_blocks_exact_states():
    cfg = json.loads(geo.CONFIG_PATH.read_text())
    off = sorted(s for s, v in cfg["states"].items() if not v["sports"])
    assert off == sorted(BLOCKED)
    assert len(cfg["states"]) == 51


def test_client_ip_prefers_first_xff():
    assert geo.client_ip({"x-forwarded-for": "8.8.8.8, 10.0.0.1"}, "127.0.0.1") == "8.8.8.8"
    assert geo.client_ip({}, "127.0.0.1") == "127.0.0.1"
    assert geo.client_ip({"x-forwarded-for": "garbage"}, None) is None


def test_resolution_and_fail_closed(monkeypatch):
    monkeypatch.delenv("GEO_LOCAL_STATE", raising=False)
    cfg = {"local_state": None, "ip_ranges": {"203.0.113.0/24": "ny", "198.51.100.0/24": "PA"},
           "states": {"NY": {"sports": False}, "PA": {"sports": True}}, "sports_prefixes": ["KXUFC"]}
    assert geo.resolve_state("203.0.113.9", cfg) == "NY"
    assert geo.resolve_state("8.8.8.8", cfg) is None
    assert geo.resolve_state("127.0.0.1", cfg) is None
    monkeypatch.setenv("GEO_LOCAL_STATE", "pa")
    assert geo.resolve_state("192.168.1.5", cfg) == "PA"
    sports = "KXNFLGAME-26NOV08TBCHI-CHI"
    assert geo.check(sports, "PA", cfg)["allowed"] is True
    ny = geo.check(sports, "NY", cfg)
    assert ny["allowed"] is False and "NY" in ny["reason"]
    assert geo.check(sports, None, cfg)["allowed"] is False   # unresolved -> blocked
    assert geo.check(sports, "ZZ", cfg)["allowed"] is False   # unknown -> blocked
    assert geo.check("KXUFCFIGHT-X", "NY", cfg)["allowed"] is False
    assert geo.check("KXHIGHNY-26NOV09-T60", "NY", cfg)["allowed"] is True  # not sports
    assert geo.check(sports, "PA", {})["allowed"] is False   # empty/bad config -> blocked


@pytest.mark.parametrize("state", BLOCKED)
def test_each_blocked_state(state):
    assert geo.check("KXNBAGAME-26NOV09LALBOS-LAL", state)["allowed"] is False


def test_order_endpoint_blocks_sports(monkeypatch):
    from app.main import app

    monkeypatch.delenv("GEO_LOCAL_STATE", raising=False)
    http = TestClient(app)
    assert http.post("/api/settings/mode", json={"mode": "paper"}).status_code == 200
    r = http.post(
        "/api/trading/order",
        json={"ticker": "KXNFLGAME-26NOV08TBCHI-CHI", "side": "yes", "count": 1, "price_cents": 50},
        headers={"X-Forwarded-For": "8.8.8.8"},
    )
    assert r.status_code == 403
    monkeypatch.setenv("GEO_LOCAL_STATE", "PA")
    ok = http.post(
        "/api/trading/order",
        json={"ticker": "KXNFLGAME-26NOV08TBCHI-CHI", "side": "yes", "count": 1, "price_cents": 50},
        headers={"X-Forwarded-For": "127.0.0.1"},
    )
    assert ok.status_code == 200 and ok.json()["dry_run"] is True

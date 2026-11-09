"""Speed pack item 1: shared token budget + async V2 client (no network)."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.kalshi import budget as B
from app.kalshi.async_client import AsyncKalshiClient, gather_limited
from app.kalshi.client import CREATE_ORDER_V2_PATH, KalshiAPIError, KalshiClient

COSTS = {
    "default_cost": 10,
    "endpoint_costs": [
        {"cost": 30, "method": "POST", "path": "/trade-api/v2/account/api_usage_level/upgrade"},
        {"cost": 2, "method": "DELETE", "path": "/trade-api/v2/portfolio/events/orders/:order_id"},
        {"cost": 2, "method": "DELETE", "path": "/trade-api/v2/portfolio/events/orders"},
        {"cost": 50, "method": "GET", "path": "/trade-api/v2/cfbenchmarks/*endpoint"},
    ],
}
LIMITS = {
    "usage_tier": "advanced",
    "read": {"refill_rate": 300, "bucket_capacity": 900},
    "write": {"refill_rate": 300, "bucket_capacity": 900},
    "grants": [{"exchange_instance": "event_contract", "level": "advanced", "source": "manual"}],
}


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_bucket_reservation_waits_and_refills():
    c = Clock()
    b = B.TokenBucket(100, 100, clock=c)
    assert b.reserve(100) == 0.0
    assert b.reserve(10) == pytest.approx(0.1)  # 10 tokens at 100/s
    c.t = 1.0
    assert b.reserve(10) == 0.0  # refilled
    b.drain()
    assert b.status()["tokens"] <= 0 and b.status()["throttled_429"] == 1


def test_budget_sized_from_account_limits_with_safety():
    bud = B.KalshiBudget(safety=0.5)
    assert bud.tier == "basic" and bud.write.refill_rate == pytest.approx(50)
    bud.apply_limits(LIMITS)
    assert bud.tier == "advanced"
    assert bud.read.refill_rate == pytest.approx(150) and bud.read.capacity == pytest.approx(450)
    assert bud.write.refill_rate == pytest.approx(150)
    assert bud.balance.refill_rate == pytest.approx(0.5)  # 1/s * safety, own bucket
    bud.apply_limits({"usage_tier": "premier", "read": {"refill_rate": 1200, "bucket_capacity": 1200},
                      "write": {"refill_rate": 1200, "bucket_capacity": 3600}})
    assert bud.balance.refill_rate == pytest.approx(2.5)  # 5/s above Advanced


def test_endpoint_costs_and_bucket_routing():
    bud = B.KalshiBudget(safety=1.0)
    assert bud.apply_endpoint_costs(COSTS) == 4
    assert bud.cost("DELETE", "/trade-api/v2/portfolio/events/orders/abc-123") == 2
    assert bud.cost("POST", "/trade-api/v2/account/api_usage_level/upgrade") == 30
    assert bud.cost("GET", "/trade-api/v2/cfbenchmarks/x/y") == 50
    assert bud.cost("GET", "/trade-api/v2/markets?limit=5") == 10
    assert bud.kind("GET", "/trade-api/v2/portfolio/balance") == "balance"
    assert bud.kind("POST", CREATE_ORDER_V2_PATH) == "write"
    assert bud.kind("GET", "/trade-api/v2/markets") == "read"
    w0 = bud.write.status()["spent"]
    bud.reserve("DELETE", "/trade-api/v2/portfolio/events/orders/o1")
    assert bud.write.status()["spent"] - w0 == 2
    r0 = bud.read.status()["spent"]
    bud.reserve("GET", "/trade-api/v2/portfolio/balance")
    assert bud.read.status()["spent"] == r0  # balance does not touch Read


def test_one_shared_budget_for_sync_and_async_clients():
    s1 = KalshiClient(host="https://x.test", key_id="", key_path=None)
    s2 = KalshiClient(host="https://x.test", key_id="", key_path=None)
    a = AsyncKalshiClient(host="https://x.test", key_id="", key_path=None)
    assert s1.budget is s2.budget is a.budget is B.get_budget()
    assert s1.bucket.total is B.get_budget().pacer


def _client(handler, **kw):
    bud = B.KalshiBudget(safety=1.0)
    cl = AsyncKalshiClient(host="https://api.test", key_id=kw.get("key_id", ""), key_path=kw.get("key_path"),
                           budget=bud, transport=httpx.MockTransport(handler))
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    cl._sleep = fake_sleep
    cl._rng = lambda: 0.5
    return cl, bud, sleeps


def test_async_429_backoff_drains_shared_bucket():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if len(calls) < 3:
            return httpx.Response(429, json={"error": "too many requests"})
        return httpx.Response(200, json={"exchange_active": True, "trading_active": True})

    cl, bud, sleeps = _client(handler)
    out = asyncio.run(cl.exchange_status())
    assert out["trading_active"] is True and len(calls) == 3
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0]  # exponential
    assert bud.read.status()["throttled_429"] == 2


def test_async_writes_never_retry_network_or_5xx(tmp_path):
    key = Ed25519PrivateKey.generate()
    kp = tmp_path / "k.pem"
    kp.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()))
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(503, json={"error": {"code": "x"}})

    cl, _, _ = _client(handler, key_id="kid", key_path=kp)
    with pytest.raises(KalshiAPIError):
        asyncio.run(cl.create_order_v2({"ticker": "T-1", "side": "yes", "count": 1, "yes_price": 40}))
    assert len(seen) == 1
    req = seen[0]
    assert req.url.path == CREATE_ORDER_V2_PATH  # V2 only
    body = json.loads(req.content)
    assert body["side"] == "bid" and body["price"] == "0.4000"
    assert req.headers["KALSHI-ACCESS-KEY"] == "kid" and req.headers["KALSHI-ACCESS-SIGNATURE"]

    def handler2(req):
        raise httpx.ConnectError("down")

    cl2, _, _ = _client(handler2, key_id="kid", key_path=kp)
    with pytest.raises(RuntimeError):
        asyncio.run(cl2.cancel_order_v2("o1", "T-1"))


def test_async_write_retries_429_then_succeeds(tmp_path):
    key = Ed25519PrivateKey.generate()
    kp = tmp_path / "k.pem"
    kp.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()))
    n = []

    def handler(req):
        n.append(req.headers["KALSHI-ACCESS-TIMESTAMP"])
        return httpx.Response(429) if len(n) == 1 else httpx.Response(200, json={"order_id": "o", "client_order_id": "c", "reduced_by": "1.00"})

    cl, bud, _ = _client(handler, key_id="kid", key_path=kp)
    out = asyncio.run(cl.cancel_order_v2("o", "T-1"))
    assert out["order_id"] == "o" and len(n) == 2
    assert bud.write.status()["throttled_429"] == 1


def test_pagination_follows_cursor_and_403_fallback():
    def handler(req):
        if req.url.host == "external-api.kalshi.com":
            return httpx.Response(403, text="<html>403</html>")
        cur = req.url.params.get("cursor")
        if not cur:
            return httpx.Response(200, json={"markets": [{"ticker": "A"}, {"ticker": "B"}], "cursor": "c2"})
        if cur == "c2":
            return httpx.Response(200, json={"markets": [{"ticker": "C"}], "cursor": ""})
        raise AssertionError(cur)

    bud = B.KalshiBudget(safety=1.0)
    cl = AsyncKalshiClient(host="https://external-api.kalshi.com", key_id="", key_path=None, budget=bud,
                           transport=httpx.MockTransport(handler))
    rows = asyncio.run(cl.collect("/trade-api/v2/markets", "markets", {"status": "open"}))
    assert [r["ticker"] for r in rows] == ["A", "B", "C"]


def test_live_data_batch_chunks_at_100():
    sizes = []

    def handler(req):
        ids = req.url.params.get_list("milestone_ids")
        sizes.append(len(ids))
        return httpx.Response(200, json={"live_datas": [{"milestone_id": i, "details": {}} for i in ids]})

    cl, _, _ = _client(handler)
    out = asyncio.run(cl.live_data_batch([f"m{i}" for i in range(230)]))
    assert sizes == [100, 100, 30] and len(out) == 230


def test_load_budget_without_keys_reads_public_costs_only():
    paths = []

    def handler(req):
        paths.append(req.url.path)
        return httpx.Response(200, json=COSTS)

    cl, bud, _ = _client(handler)
    out = asyncio.run(cl.load_budget())
    assert out["costs"] == 4 and out["limits"] is None
    assert paths == ["/trade-api/v2/account/endpoint_costs"]  # no signed call without keys
    assert bud.tier == "basic"


def test_gather_limited_caps_concurrency():
    live, peak = [0], [0]

    async def job(i):
        live[0] += 1
        peak[0] = max(peak[0], live[0])
        await asyncio.sleep(0.001)
        live[0] -= 1
        if i == 3:
            raise ValueError("x")
        return i

    res = asyncio.run(gather_limited((job(i) for i in range(20)), concurrency=4))
    assert peak[0] <= 4 and res[0] == 0 and isinstance(res[3], ValueError)


def test_sync_client_429_drains_shared_budget():
    class R:
        def __init__(self, s):
            self.status_code, self.text, self.content, self.headers = s, "{}", b"{}", {}

        def json(self):
            return {}

    bud = B.KalshiBudget(safety=1.0)
    cl = KalshiClient(host="https://x.test", key_id="", key_path=None, budget=bud)
    seq = [R(429), R(200)]
    cl._session.request = lambda *a, **k: seq.pop(0)
    cl._sleep = lambda s: None
    cl.public_get("/trade-api/v2/markets")
    assert bud.read.status()["throttled_429"] == 1


def test_no_proxy_or_rotation_code_paths():
    import pathlib

    src = "".join(p.read_text() for p in pathlib.Path("app").rglob("*.py"))
    for bad in ("proxies=", "socks5", "rotate_ip", "X-Forwarded-For\": ", "fake_ip"):
        assert bad not in src

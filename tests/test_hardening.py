"""Item 7: scrypt vault KDF, retry/backoff with Retry-After, 8 req/s limiter."""
from __future__ import annotations

import base64
import json

import pytest
import requests
from cryptography.fernet import Fernet

from app.kalshi import client as C
from app.kalshi.rate_limit import GLOBAL_RATE_PER_SEC, GlobalRateLimiter
from app.security import vault as V


# ----- scrypt vault -----

def test_new_vault_uses_scrypt(tmp_path):
    v = V.Vault(tmp_path / "v.enc")
    assert v.unlock("correct horse")
    v.set("k", "v")
    blob = json.loads((tmp_path / "v.enc").read_text())
    assert blob["kdf"] == "scrypt" and blob["scrypt"]["n"] == V.SCRYPT_N
    v2 = V.Vault(tmp_path / "v.enc")
    assert not v2.unlock("wrong")
    assert v2.unlock("correct horse") and v2.get("k") == "v"


def test_legacy_pbkdf2_vault_migrates(tmp_path):
    salt = b"0123456789abcdef"
    f = Fernet(V._derive_key("pw", salt))
    path = tmp_path / "old.enc"
    path.write_text(json.dumps({"salt": base64.b64encode(salt).decode(), "payload": f.encrypt(b'{"a": 1}').decode()}))
    v = V.Vault(path)
    assert v.unlock("pw") and v.get("a") == 1
    assert json.loads(path.read_text())["kdf"] == "scrypt"
    again = V.Vault(path)
    assert again.unlock("pw") and again.get("a") == 1


# ----- retry -----

class R:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self.text = json.dumps(body) if body is not None else ""
        self.content = self.text.encode()
        self.headers = headers or {}

    def json(self):
        return json.loads(self.text)


def _client(responses):
    cl = C.KalshiClient(host="https://api.elections.kalshi.com", key_id="", key_path=None)
    calls, sleeps = [], []

    def fake(method, url, **kw):
        calls.append(method)
        nxt = responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    cl._session.request = fake
    cl._sleep = sleeps.append
    cl._rng = lambda: 0.5
    cl.bucket.total = GlobalRateLimiter(rate=1000)
    return cl, calls, sleeps


def test_get_retries_5xx_and_network_then_succeeds():
    cl, calls, sleeps = _client([R(503), requests.ConnectionError("x"), R(200, {"ok": 1})])
    assert cl.public_get("/trade-api/v2/exchange/status") == {"ok": 1}
    assert len(calls) == 3
    assert sleeps == [C.backoff_delay(0, rng=lambda: 0.5), C.backoff_delay(1, rng=lambda: 0.5)]
    assert sleeps[1] > sleeps[0]


def test_retry_after_honored_and_cap_five_tries():
    cl, calls, sleeps = _client([R(429, headers={"Retry-After": "2"})] * 5)
    with pytest.raises(C.KalshiAPIError) as e:
        cl.public_get("/x")
    assert e.value.status == 429
    assert len(calls) == C.MAX_TRIES == 5
    assert sleeps == [2.0] * 4


def test_writes_do_not_retry_on_network_or_5xx():
    cl, calls, _ = _client([requests.ConnectionError("boom")])
    cl._auth_headers = lambda *a, **k: {}
    with pytest.raises(RuntimeError):
        cl.request("POST", C.CREATE_ORDER_V2_PATH, json_body={"a": 1}, auth=True)
    assert len(calls) == 1
    cl, calls, _ = _client([R(503)])
    cl._auth_headers = lambda *a, **k: {}
    with pytest.raises(C.KalshiAPIError):
        cl.request("POST", C.CREATE_ORDER_V2_PATH, json_body={"a": 1}, auth=True)
    assert len(calls) == 1


def test_writes_retry_429_and_resign():
    cl, calls, _ = _client([R(429), R(201, {"order_id": "o"})])
    signs = []
    cl._auth_headers = lambda *a, **k: signs.append(1) or {}
    assert cl.request("POST", C.CREATE_ORDER_V2_PATH, json_body={"a": 1}, auth=True)["order_id"] == "o"
    assert len(calls) == 2 and len(signs) == 2


def test_backoff_and_retry_after_parsing():
    assert C.backoff_delay(0, rng=lambda: 0.0) == pytest.approx(0.25)
    assert C.backoff_delay(3, rng=lambda: 1.0) == pytest.approx(4.0)
    assert C.backoff_delay(20, rng=lambda: 1.0) == C.BACKOFF_CAP
    assert C.backoff_delay(0, retry_after=99) == C.BACKOFF_CAP
    assert C.retry_after_seconds("3") == 3.0
    assert C.retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT", now=1445412470.0) == pytest.approx(10.0)
    assert C.retry_after_seconds("junk") is None


# ----- 8 req/s -----

def test_global_limiter_caps_at_8_per_second():
    assert GLOBAL_RATE_PER_SEC == 8.0
    t = [0.0]
    lim = GlobalRateLimiter(clock=lambda: t[0], sleep=lambda s: t.__setitem__(0, t[0] + s))
    for _ in range(8 + 16):
        lim.acquire()
    # 8 burst + 16 more at 8/s -> ~2 seconds of simulated time
    assert t[0] == pytest.approx(2.0, abs=1e-6)


def test_client_uses_global_limiter():
    cl = C.KalshiClient(host="https://x.test", key_id="", key_path=None)
    assert isinstance(cl.bucket.total, GlobalRateLimiter)

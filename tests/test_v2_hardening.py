"""Create Order V2 hardening: signing, validation, error mapping, paper vs armed live.

All network calls are mocked. Signing keys are generated per test run in a
temp dir and thrown away; no real key material is used or stored.
"""
from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from fastapi.testclient import TestClient

from app.config import settings
from app.db.store import Store
from app.kalshi.client import (
    CREATE_ORDER_V2_PATH,
    KalshiAPIError,
    KalshiClient,
    detect_key_type,
    friendly_order_error,
    presign_text,
    summarize_positions,
    to_create_order_v2,
)
from app.main import _balance_dollars, app
from app.trading.orders import OrderGate


def _write_key(tmp_path, key, name):
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    path = tmp_path / name
    path.write_bytes(pem)
    return path, pem.decode()


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self.text = json.dumps(payload) if payload is not None else ""
        self.content = self.text.encode()

    def json(self):
        return json.loads(self.text)


def _client_with(tmp_path, responses, captured):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path, _ = _write_key(tmp_path, key, "throwaway-rsa.pem")
    client = KalshiClient(host="https://external-api.kalshi.com", key_id="unit-test-only", key_path=path)

    def fake_request(method, url, headers=None, params=None, data=None, timeout=None):
        captured.append({"method": method, "url": url, "headers": headers, "data": data})
        return responses.pop(0)

    client._session.request = fake_request
    return client


# ----- signing -----

def test_presign_text_strips_query_and_excludes_body():
    assert presign_text("123", "post", "/trade-api/v2/portfolio/orders?limit=5") == (
        "123POST/trade-api/v2/portfolio/orders"
    )


def test_rsa_pss_signature_over_timestamp_method_path_only(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path, pem = _write_key(tmp_path, key, "rsa.pem")
    assert detect_key_type(pem) == "rsa"
    client = KalshiClient(host="https://x.test", key_id="kid", key_path=path)
    sig = client.generate_signature("POST", CREATE_ORDER_V2_PATH, "1700000000000", '{"a":1}')
    key.public_key().verify(
        base64.b64decode(sig),
        f"1700000000000POST{CREATE_ORDER_V2_PATH}".encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_ed25519_signature(tmp_path):
    key = ed25519.Ed25519PrivateKey.generate()
    path, pem = _write_key(tmp_path, key, "ed.pem")
    assert detect_key_type(pem) == "ed25519"
    client = KalshiClient(host="https://x.test", key_id="kid", key_path=path)
    headers = client._auth_headers("GET", "/trade-api/v2/portfolio/balance?subaccount=0")
    assert headers["KALSHI-ACCESS-KEY"] == "kid"
    ts = headers["KALSHI-ACCESS-TIMESTAMP"]
    key.public_key().verify(
        base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
        f"{ts}GET/trade-api/v2/portfolio/balance".encode(),
    )


def test_detect_key_type_rejects_garbage():
    assert detect_key_type("-----BEGIN PRIVATE KEY-----\nnope\n-----END PRIVATE KEY-----") is None


# ----- mapping and validation -----

def _order(**kw):
    base = {"ticker": "KXT-1", "side": "yes", "count": 3, "yes_price": 22, "type": "limit"}
    base.update(kw)
    return base


def test_complement_mapping_examples():
    assert to_create_order_v2(_order())["side"] == "bid"
    assert to_create_order_v2(_order())["price"] == "0.2200"
    assert to_create_order_v2(_order())["count"] == "3.00"
    no = to_create_order_v2(_order(side="no", yes_price=None, no_price=40))
    assert (no["side"], no["price"]) == ("ask", "0.6000")
    edge = to_create_order_v2(_order(side="no", yes_price=None, no_price=1))
    assert edge["price"] == "0.9900"


@pytest.mark.parametrize(
    "bad",
    [
        _order(type="stop"),
        _order(type="market_on_close"),
        _order(yes_price=22.5),
        _order(yes_price="22"),
        _order(yes_price=True),
        _order(count=0),
        _order(count=2.5),
        _order(count=10_001),
        _order(action="sell"),
        _order(ticker=" "),
    ],
)
def test_rejects_bad_app_orders(bad):
    with pytest.raises(ValueError):
        to_create_order_v2(bad)


def _v2(**kw):
    base = {
        "ticker": "KXT-1",
        "side": "bid",
        "count": "3",
        "price": "0.22",
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
    }
    base.update(kw)
    return base


def test_passthrough_normalizes_whole_cents_and_contracts():
    body = to_create_order_v2(_v2(client_order_id="c1", post_only=False, subaccount=0))
    assert body["count"] == "3.00"
    assert body["price"] == "0.2200"
    assert body["client_order_id"] == "c1"
    assert body["post_only"] is False
    assert body["subaccount"] == 0


@pytest.mark.parametrize(
    "bad",
    [
        _v2(price="0.225"),  # sub-cent: Kalshi may allow it, this app does not
        _v2(price="1.00"),
        _v2(price="0.00"),
        _v2(price=0.22),
        _v2(count="2.50"),  # fractional contracts are not sent
        _v2(count="-1"),
        _v2(side="buy"),
        _v2(time_in_force="GTT"),
        _v2(self_trade_prevention_type="none"),
        _v2(action="buy"),  # unknown V1 field
        _v2(yes_price=22),
        _v2(reduce_only=True),  # needs IOC
        _v2(time_in_force="immediate_or_cancel", expiration_time=1900000000),
        _v2(post_only="yes"),
        _v2(subaccount=64),
    ],
)
def test_passthrough_rejects_invalid_fields(bad):
    with pytest.raises(ValueError):
        to_create_order_v2(bad)


# ----- error mapping -----

def test_friendly_errors():
    gone = friendly_order_error(KalshiAPIError(410, "deprecated_v1_order_endpoint", "gone"))
    assert "Create Order V2" in gone["error"] and gone["http_status"] == 410
    plain_410 = friendly_order_error(KalshiAPIError(410))
    assert "retired the old order endpoint" in plain_410["error"]
    broke = friendly_order_error(KalshiAPIError(400, "insufficient_balance", "insufficient balance"))
    assert broke["error"].startswith("Not enough Kalshi balance")
    assert broke["error_code"] == "insufficient_balance"
    assert "key" in friendly_order_error(KalshiAPIError(401))["error"].lower()
    assert "trade" in friendly_order_error(KalshiAPIError(403))["error"].lower()
    assert "Wait" in friendly_order_error(KalshiAPIError(429))["error"]
    assert "check Orders" in friendly_order_error(KalshiAPIError(503))["error"]
    assert friendly_order_error(RuntimeError("Network error contacting x"))["error_code"] == "network"
    assert friendly_order_error(ValueError("bad"))["error_code"] == "invalid_order"


def test_api_error_parses_kalshi_body():
    err = KalshiAPIError.from_response(_Resp(400, {"error": {"code": "invalid_price", "message": "bad tick"}}))
    assert err.status == 400 and err.code == "invalid_price" and err.api_message == "bad tick"
    flat = KalshiAPIError.from_response(_Resp(409, {"code": "order_already_exists", "message": "dup"}))
    assert flat.code == "order_already_exists"


# ----- paper vs armed live through the real HTTP client (mocked transport) -----

@pytest.fixture
def live_env(tmp_path):
    old = (settings.dry_run, settings.key_id, settings.key_path)
    key_path = tmp_path / "placeholder.key"
    key_path.write_text("placeholder\n", encoding="utf-8")
    settings.key_id = "unit-test-only"
    settings.key_path = key_path
    yield tmp_path
    settings.dry_run, settings.key_id, settings.key_path = old


def test_paper_builds_same_v2_body_but_never_sends(live_env):
    captured = []
    client = _client_with(live_env, [], captured)
    gate = OrderGate(Store(live_env / "o.db"), client)
    settings.dry_run = True
    res = gate.place("KXT-1", "no", 2, price_cents=40)
    assert res["ok"] and res["dry_run"]
    assert res["v2_request"]["side"] == "ask"
    assert res["v2_request"]["price"] == "0.6000"
    assert res["v2_request"]["count"] == "2.00"
    assert "$0.80" in res["message"]
    assert captured == []


def test_armed_live_posts_signed_v2_and_reports_fill(live_env):
    captured = []
    ack = {"order_id": "o-1", "fill_count": "1.00", "remaining_count": "0.00", "ts_ms": 5}
    client = _client_with(live_env, [_Resp(201, ack)], captured)
    gate = OrderGate(Store(live_env / "o.db"), client)
    settings.dry_run = False
    res = gate.place("KXT-1", "yes", 1, price_cents=22, order_type="ioc")
    assert res["ok"] and res["dry_run"] is False
    assert "filled 1.00" in res["message"]
    assert len(captured) == 1
    call = captured[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://external-api.kalshi.com" + CREATE_ORDER_V2_PATH
    for h in ("KALSHI-ACCESS-KEY", "KALSHI-ACCESS-SIGNATURE", "KALSHI-ACCESS-TIMESTAMP"):
        assert call["headers"][h]
    sent = json.loads(call["data"])
    assert sent["side"] == "bid" and sent["price"] == "0.2200"
    assert sent["time_in_force"] == "immediate_or_cancel"
    # Verify the signature really covers timestamp+POST+path (no body).
    key = serialization.load_pem_private_key(client.key_path.read_bytes(), password=None)
    key.public_key().verify(
        base64.b64decode(call["headers"]["KALSHI-ACCESS-SIGNATURE"]),
        f"{call['headers']['KALSHI-ACCESS-TIMESTAMP']}POST{CREATE_ORDER_V2_PATH}".encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_armed_live_410_is_friendly_and_logged(live_env):
    captured = []
    body = {"error": {"code": "deprecated_v1_order_endpoint", "message": "use v2"}}
    client = _client_with(live_env, [_Resp(410, body)], captured)
    store = Store(live_env / "o.db")
    gate = OrderGate(store, client)
    settings.dry_run = False
    res = gate.place("KXT-1", "yes", 1, price_cents=22)
    assert res["ok"] is False
    assert res["http_status"] == 410
    assert "Create Order V2" in res["error"]
    assert store.recent_orders(1)[0]["status"] == "ERROR"


def test_gate_refuses_missing_price_and_unknown_type_before_network(live_env):
    captured = []
    client = _client_with(live_env, [], captured)
    gate = OrderGate(Store(live_env / "o.db"), client)
    settings.dry_run = False
    assert gate.place("KXT-1", "yes", 1)["error_code"] == "price_required"
    assert gate.place("KXT-1", "yes", 1, price_cents=22, order_type="stop")["ok"] is False
    assert gate.place("KXT-1", "yes", 1, price_cents=100)["ok"] is False
    assert gate.place("KXT-1", "yes", 0, price_cents=22)["ok"] is False
    assert captured == []


# ----- portfolio helpers and PWA -----

def test_balance_prefers_fixed_point_dollars():
    assert _balance_dollars({"balance": 1234, "balance_dollars": "12.345600"}) == 12.3456
    assert _balance_dollars({"balance": 1234}) == 12.34


def test_summarize_positions_fixed_point():
    rows = summarize_positions(
        {
            "market_positions": [
                {"ticker": "A", "position_fp": "3.00", "market_exposure_dollars": "0.6600"},
                {"ticker": "B", "position_fp": "-2.00", "realized_pnl_dollars": "1.50"},
                {"ticker": "C", "position_fp": "0.00"},
            ]
        }
    )
    assert [(r["ticker"], r["side"], r["contracts"]) for r in rows] == [("A", "YES", 3.0), ("B", "NO", 2.0)]
    assert rows[0]["exposure_dollars"] == 0.66


def test_pwa_routes_served():
    http = TestClient(app)
    man = http.get("/manifest.webmanifest")
    assert man.status_code == 200
    data = man.json()
    assert data["display"] == "standalone"
    assert any(icon["sizes"] == "512x512" for icon in data["icons"])
    sw = http.get("/sw.js")
    assert sw.status_code == 200
    assert "/api/" in sw.text  # API is never served from cache
    page = http.get("/").text
    assert 'rel="manifest"' in page
    assert "How to use" in page


def test_public_get_falls_back_on_403_but_signed_calls_do_not(tmp_path):
    captured = []
    client = _client_with(
        tmp_path,
        [_Resp(403, None), _Resp(200, {"markets": []}), _Resp(403, None)],
        captured,
    )
    assert client.public_get("/trade-api/v2/markets") == {"markets": []}
    assert captured[0]["url"].startswith("https://external-api.kalshi.com")
    assert captured[1]["url"].startswith("https://api.elections.kalshi.com")
    assert client.host == "https://external-api.kalshi.com"
    with pytest.raises(KalshiAPIError):
        client.auth_get("/trade-api/v2/portfolio/balance")
    assert len(captured) == 3

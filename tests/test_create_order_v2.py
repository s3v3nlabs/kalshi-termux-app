"""Live create-order uses Kalshi Create Order V2, not the deprecated portfolio path."""
from __future__ import annotations

import json

import pytest

from app.config import settings
from app.db.store import Store
from app.kalshi.client import CREATE_ORDER_V2_PATH, KalshiClient, to_create_order_v2
from app.trading.orders import OrderGate


def _legacy(**overrides):
    order = {
        "action": "buy",
        "client_order_id": "cid-1",
        "count": 1,
        "side": "yes",
        "ticker": "KXNHLGAME-TEST",
        "type": "limit",
        "yes_price": 22,
    }
    order.update(overrides)
    return order


def test_yes_limit_maps_to_bid():
    body = to_create_order_v2(_legacy())
    assert body == {
        "ticker": "KXNHLGAME-TEST",
        "client_order_id": "cid-1",
        "side": "bid",
        "count": "1.00",
        "price": "0.2200",
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
    }


def test_no_limit_maps_to_yes_book_ask():
    body = to_create_order_v2(
        _legacy(side="no", yes_price=None, no_price=40, count=2)
    )
    assert body["side"] == "ask"
    assert body["price"] == "0.6000"
    assert body["count"] == "2.00"
    assert body["time_in_force"] == "good_till_canceled"


def test_blank_price_is_market_style_ioc():
    yes = to_create_order_v2(_legacy(type="market", yes_price=None))
    assert yes["side"] == "bid"
    assert yes["price"] == "0.9900"
    assert yes["time_in_force"] == "immediate_or_cancel"

    no = to_create_order_v2(_legacy(side="no", type="market", yes_price=None))
    assert no["side"] == "ask"
    assert no["price"] == "0.0100"
    assert no["time_in_force"] == "immediate_or_cancel"


def test_rejects_price_outside_cents():
    with pytest.raises(ValueError):
        to_create_order_v2(_legacy(yes_price=0))
    with pytest.raises(ValueError):
        to_create_order_v2(_legacy(yes_price=100))


def test_place_order_posts_v2_path_and_body(tmp_path):
    client = KalshiClient(
        host="https://external-api.kalshi.com",
        key_id="unit-test-only",
        key_path=tmp_path / "not-a-real.key",
    )
    captured = {}

    def fake_headers(method, path, body_str=""):
        captured["method"] = method
        captured["signed_path"] = path
        captured["signed_body"] = body_str
        return {"Content-Type": "application/json"}

    class Resp:
        status_code = 201
        content = (
            b'{"order_id":"ord-1","client_order_id":"cid-1",'
            b'"fill_count":"0.00","remaining_count":"1.00","ts_ms":1715793600123}'
        )
        text = content.decode()

        def json(self):
            return json.loads(self.content)

    def fake_request(method, url, headers=None, params=None, data=None, timeout=None):
        captured["http_method"] = method
        captured["url"] = url
        captured["data"] = data
        return Resp()

    client._auth_headers = fake_headers
    client._session.request = fake_request

    ack = client.place_order(_legacy(side="no", yes_price=None, no_price=40))

    assert captured["http_method"] == "POST"
    assert captured["url"] == "https://external-api.kalshi.com" + CREATE_ORDER_V2_PATH
    assert captured["url"].rstrip("/").endswith("/portfolio/events/orders")
    assert not captured["url"].rstrip("/").endswith("/portfolio/orders")
    assert captured["signed_path"] == CREATE_ORDER_V2_PATH
    assert captured["signed_body"] == captured["data"]
    body = json.loads(captured["data"])
    assert body["ticker"] == "KXNHLGAME-TEST"
    assert body["side"] == "ask"
    assert body["count"] == "1.00"
    assert body["price"] == "0.6000"
    assert body["time_in_force"] == "good_till_canceled"
    assert body["self_trade_prevention_type"] == "taker_at_cross"
    assert body["client_order_id"] == "cid-1"
    assert "yes_price" not in body
    assert "no_price" not in body
    assert "action" not in body
    assert ack["order_id"] == "ord-1"
    assert "order" not in ack


def test_dry_run_does_not_call_create_order_and_live_does(tmp_path):
    old = (settings.dry_run, settings.key_id, settings.key_path)
    calls = []

    class Resp:
        status_code = 201
        content = b'{"order_id":"ord-live","fill_count":"1.00","remaining_count":"0.00","ts_ms":2}'
        text = content.decode()

        def json(self):
            return json.loads(self.content)

    try:
        settings.key_id = "unit-test-only"
        key_path = tmp_path / "not-a-real.key"
        key_path.write_text("unit-test-placeholder\n", encoding="utf-8")
        settings.key_path = key_path
        client = KalshiClient(host="https://external-api.kalshi.com", key_id=settings.key_id, key_path=key_path)
        client._auth_headers = lambda method, path, body_str="": {"Content-Type": "application/json"}

        def fake_request(method, url, headers=None, params=None, data=None, timeout=None):
            calls.append({"method": method, "url": url, "data": data})
            return Resp()

        client._session.request = fake_request
        gate = OrderGate(Store(tmp_path / "orders.db"), client)

        settings.dry_run = True
        paper = gate.place("KXNHLGAME-TEST", "yes", 1, price_cents=22)
        assert paper["dry_run"] is True
        assert paper["ok"] is True
        assert "Not sent" in paper["message"]
        assert calls == []

        settings.dry_run = False
        assert gate.place("KXNHLGAME-TEST", "yes", 1, price_cents=22)["dry_run"] is True
        assert calls == []

        armed = gate.arm_live("ARM LIVE")
        assert armed["ok"] is True
        live = gate.place("KXNHLGAME-TEST", "no", 1, price_cents=40)
        assert live["ok"] is True
        assert live["dry_run"] is False
        assert live["response"]["order_id"] == "ord-live"
        assert len(calls) == 1
        assert calls[0]["url"].endswith(CREATE_ORDER_V2_PATH)
        sent = json.loads(calls[0]["data"])
        assert sent["side"] == "ask"
        assert sent["price"] == "0.6000"
    finally:
        settings.dry_run, settings.key_id, settings.key_path = old

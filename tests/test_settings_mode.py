"""Smoke: homepage copy, persisted Allow live, ARM LIVE still required."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import settings
from app.db.store import Store
from app.main import app, orders, store
from app.trading.orders import OrderGate
from app.ui_prefs import apply_saved_mode, clear_dry_run_override, load_dry_run_override, prefs_path


def test_homepage_mode_and_arm_gate():
    saved_dry = bool(settings.dry_run)
    path = prefs_path()
    saved_file = path.read_text(encoding="utf-8") if path.is_file() else None
    saved_kv = store.get_setting("dry_run")
    saved_place = orders.client.place_order
    calls = {"n": 0}

    def blocked(payload):
        calls["n"] += 1
        raise AssertionError("live place_order must not run in the mode smoke test")

    orders.client.place_order = blocked
    orders.disarm()
    http = TestClient(app)
    try:
        home = http.get("/")
        assert home.status_code == 200
        page = home.text
        assert "Allow live trading" in page
        assert "App lock password" in page
        assert "Save on this phone" in page
        assert "ARM LIVE" in page
        assert "Paper bets" in page

        paper = http.post("/api/settings/mode", json={"dry_run": True})
        assert paper.status_code == 200
        assert paper.json()["dry_run"] is True
        assert paper.json()["live_armed"] is False

        status = http.get("/api/trading/status")
        assert status.status_code == 200
        assert status.json()["dry_run"] is True

        full = http.get("/api/status")
        assert full.status_code == 200
        assert "dry_run" in full.json()
        assert full.json()["dry_run"] is True

        allowed = http.post("/api/settings/mode", json={"allow_live": True})
        assert allowed.status_code == 200
        body = allowed.json()
        assert body["dry_run"] is False
        assert body["live_allowed"] is True
        assert body["live_armed"] is False
        assert load_dry_run_override(store) is False

        placed = http.post(
            "/api/trading/order",
            json={"ticker": "TEST-SMOKE", "side": "yes", "count": 1},
        )
        assert placed.status_code == 200
        placed_body = placed.json()
        assert placed_body["dry_run"] is True
        assert "Not sent" in placed_body["message"]
        assert calls["n"] == 0

        bad_arm = http.post("/api/trading/arm", json={"confirm_phrase": "nope"})
        assert bad_arm.json()["ok"] is False
        assert http.get("/api/trading/status").json()["live_armed"] is False

        arm = http.post("/api/trading/arm", json={"confirm_phrase": "ARM LIVE"})
        if arm.json().get("ok"):
            assert http.get("/api/trading/status").json()["live_armed"] is True
            http.post("/api/trading/disarm")
        assert http.get("/api/trading/status").json()["live_armed"] is False
        held = http.post(
            "/api/trading/order",
            json={"ticker": "TEST-SMOKE", "side": "yes", "count": 1},
        )
        assert held.json()["dry_run"] is True
        assert calls["n"] == 0

        back = http.post("/api/settings/mode", json={"dry_run": True})
        assert back.status_code == 200
        assert back.json()["dry_run"] is True
        assert back.json()["live_allowed"] is False
        blocked_arm = http.post("/api/trading/arm", json={"confirm_phrase": "ARM LIVE"})
        assert blocked_arm.json()["ok"] is False
        again = http.post(
            "/api/trading/order",
            json={"ticker": "TEST-SMOKE", "side": "no", "count": 1},
        )
        assert again.json()["dry_run"] is True
        assert calls["n"] == 0
        assert load_dry_run_override(store) is True
    finally:
        orders.client.place_order = saved_place
        orders.disarm()
        if saved_file is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(saved_file, encoding="utf-8")
        if saved_kv is None:
            store.delete_setting("dry_run")
        else:
            store.set_setting("dry_run", saved_kv)
        if saved_file is None and saved_kv is None:
            clear_dry_run_override(store)
            settings.dry_run = bool(settings.env_dry_run)
        else:
            settings.dry_run = saved_dry
            apply_saved_mode(store)


def test_real_order_only_when_live_allowed_and_armed(tmp_path):
    old = (settings.dry_run, settings.key_id, settings.key_path)
    try:
        settings.key_id = "unit-test-only"
        key_path = tmp_path / "not-a-real.key"
        key_path.write_text("unit-test-placeholder\n", encoding="utf-8")
        settings.key_path = key_path
        calls = []

        class Fake:
            def place_order(self, payload):
                calls.append(payload)
                return {
                    "order_id": "test-order",
                    "fill_count": "0.00",
                    "remaining_count": "1.00",
                    "ts_ms": 1,
                }

        gate = OrderGate(Store(tmp_path / "orders.db"), Fake())
        settings.dry_run = True
        assert gate.arm_live("ARM LIVE")["ok"] is False
        assert gate.place("T", "yes", 1)["dry_run"] is True
        assert calls == []

        settings.dry_run = False
        assert gate.place("T", "yes", 1)["dry_run"] is True
        assert calls == []
        armed = gate.arm_live("ARM LIVE")
        assert armed["ok"] is True
        sent = gate.place("T", "yes", 1, price_cents=22)
        assert sent["dry_run"] is False
        assert sent["response"]["order_id"] == "test-order"
        assert "test-order" in sent["message"]
        assert len(calls) == 1
        assert calls[0]["side"] == "yes"
        assert calls[0]["yes_price"] == 22
        gate.disarm()
        status = gate.status()
        assert status["live_allowed"] is True
        assert status["live_armed"] is False
        assert gate.place("T", "no", 1)["dry_run"] is True
        assert len(calls) == 1
    finally:
        settings.dry_run, settings.key_id, settings.key_path = old

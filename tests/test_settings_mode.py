"""Smoke: homepage PAPER|LIVE switch, LIVE default, persist, keys gate."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import settings
from app.db.store import Store
from app.main import app, orders, store
from app.trading.orders import OrderGate
from app.ui_prefs import (
    apply_saved_mode,
    clear_dry_run_override,
    load_dry_run_override,
    load_skip_live_confirm,
    prefs_path,
    save_skip_live_confirm,
)


def test_homepage_mode_switch_and_live_default():
    saved_dry = bool(settings.dry_run)
    path = prefs_path()
    saved_file = path.read_text(encoding="utf-8") if path.is_file() else None
    saved_kv = store.get_setting("dry_run")
    saved_skip = load_skip_live_confirm(store)
    saved_place = orders.client.place_order
    calls = {"n": 0}

    def blocked(payload):
        calls["n"] += 1
        raise AssertionError("live place_order must not run in the mode smoke test")

    orders.client.place_order = blocked
    http = TestClient(app)
    try:
        # Force a known PAPER state first so the rest of the assertions are stable.
        paper = http.post("/api/settings/mode", json={"mode": "paper"})
        assert paper.status_code == 200
        assert paper.json()["dry_run"] is True
        assert paper.json()["effective_mode"] == "PAPER"
        assert paper.json()["live_armed"] is False

        home = http.get("/")
        assert home.status_code == 200
        page = home.text
        assert "mode-switch" in page
        assert "strip-paper" in page and "strip-live" in page
        assert "PAPER" in page and "LIVE" in page
        assert "ARM LIVE" not in page
        assert "type ARM LIVE" not in page.lower()
        assert "App lock password" in page
        assert "Save on this phone" in page
        assert "skip-live-confirm" in page

        status = http.get("/api/trading/status")
        assert status.status_code == 200
        assert status.json()["dry_run"] is True

        full = http.get("/api/status")
        assert full.status_code == 200
        assert "dry_run" in full.json()
        assert full.json()["dry_run"] is True
        assert "skip_live_confirm" in full.json()

        # Switch to LIVE — no typed phrase. Without keys, orders refuse (no silent paper).
        live = http.post("/api/settings/mode", json={"mode": "live"})
        assert live.status_code == 200
        body = live.json()
        assert body["dry_run"] is False
        assert body["live_allowed"] is True
        assert body["live_armed"] is True
        assert body["effective_mode"] == "LIVE"
        assert load_dry_run_override(store) is False

        placed = http.post(
            "/api/trading/order",
            json={"ticker": "TEST-SMOKE", "side": "yes", "count": 1, "price_cents": 22, "confirm_live": "PLACE LIVE ORDER"},
        )
        assert placed.status_code == 200
        placed_body = placed.json()
        # No keys in this smoke → refuse, do not paper-trade, do not call exchange.
        if not settings.has_keys:
            assert placed_body["ok"] is False
            assert placed_body["error_code"] == "keys_required"
            assert "add Kalshi keys" in placed_body["error"]
            assert placed_body["dry_run"] is False
        assert calls["n"] == 0

        # PAPER is instant
        back = http.post("/api/settings/mode", json={"mode": "paper"})
        assert back.status_code == 200
        assert back.json()["dry_run"] is True
        assert back.json()["effective_mode"] == "PAPER"
        again = http.post(
            "/api/trading/order",
            json={"ticker": "TEST-SMOKE", "side": "no", "count": 1, "price_cents": 40},
        )
        assert again.json()["dry_run"] is True
        assert again.json()["ok"] is True
        assert "Not sent" in again.json()["message"]

        no_price = http.post(
            "/api/trading/order",
            json={"ticker": "TEST-SMOKE", "side": "no", "count": 1},
        )
        assert no_price.status_code == 200
        assert no_price.json()["ok"] is False
        assert no_price.json()["error_code"] == "price_required"
        assert calls["n"] == 0

        skip = http.post("/api/settings/mode", json={"skip_live_confirm": True})
        assert skip.status_code == 200
        assert skip.json()["skip_live_confirm"] is True
        assert load_skip_live_confirm(store) is True
        assert load_dry_run_override(store) is True  # still paper; skip toggle alone
    finally:
        orders.client.place_order = saved_place
        if saved_file is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(saved_file, encoding="utf-8")
        if saved_kv is None:
            store.delete_setting("dry_run")
        else:
            store.set_setting("dry_run", saved_kv)
        save_skip_live_confirm(saved_skip, store)
        if saved_file is None and saved_kv is None:
            clear_dry_run_override(store)
            settings.dry_run = bool(settings.env_dry_run)
        else:
            settings.dry_run = saved_dry
            apply_saved_mode(store)


def test_fresh_env_defaults_to_live():
    """Code default for missing DRY_RUN is LIVE (False)."""
    from app.config import _bool

    assert _bool(None, False) is False
    assert _bool("0", False) is False
    assert _bool("1", False) is True
    # Settings class default matches LIVE-first boot when env unset.
    src = Path(__file__).resolve().parents[1] / "app" / "config.py"
    assert '_bool(os.getenv("DRY_RUN"), False)' in src.read_text(encoding="utf-8")


def test_real_order_when_live_and_keys(tmp_path):
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
        assert gate.place("T", "yes", 1, price_cents=22)["dry_run"] is True
        assert calls == []

        # LIVE without keys → refuse (do not paper)
        settings.key_id = ""
        settings.dry_run = False
        refused = gate.place("T", "yes", 1, price_cents=22)
        assert refused["ok"] is False
        assert refused["error_code"] == "keys_required"
        assert calls == []

        settings.key_id = "unit-test-only"
        settings.dry_run = False
        assert gate.live_armed is True
        sent = gate.place("T", "yes", 1, price_cents=22)
        assert sent["dry_run"] is False
        assert sent["response"]["order_id"] == "test-order"
        assert "test-order" in sent["message"]
        assert len(calls) == 1
        assert calls[0]["side"] == "bid"
        assert calls[0]["price"] == "0.2200"
        assert calls[0]["count"] == "1.00"
        assert "yes_price" not in calls[0]

        gate.set_paper()
        status = gate.status()
        assert status["live_allowed"] is False
        assert status["live_armed"] is False
        assert status["effective_mode"] == "PAPER"
        assert gate.place("T", "no", 1, price_cents=40)["dry_run"] is True
        assert len(calls) == 1
    finally:
        settings.dry_run, settings.key_id, settings.key_path = old

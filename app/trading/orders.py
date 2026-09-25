"""Order gate. Real orders only when live trading is allowed AND ARM LIVE is set."""
from __future__ import annotations

import uuid
from typing import Any, Optional

from app.config import settings
from app.db.store import Store
from app.kalshi.client import KalshiClient


LIVE_CONFIRM_PHRASE = "ARM LIVE"


class OrderGate:
    def __init__(self, store: Store, client: Optional[KalshiClient] = None):
        self.store = store
        self.client = client or KalshiClient()
        self._live_armed = False

    @property
    def dry_run(self) -> bool:
        # Real place_order only when live is allowed AND this session is armed.
        if settings.dry_run:
            return True
        return not self._live_armed

    @property
    def live_armed(self) -> bool:
        return self._live_armed and not settings.dry_run

    def status(self) -> dict:
        paper_only = bool(settings.dry_run)
        armed = bool(self._live_armed) and not paper_only
        if armed:
            label = "Live armed"
        elif not paper_only:
            label = "Live ready"
        else:
            label = "Paper bets"
        return {
            "dry_run": paper_only,
            "env_dry_run": bool(settings.env_dry_run),
            "live_allowed": not paper_only,
            "live_armed": armed,
            "orders_are_paper": not armed,
            "effective_mode": "LIVE" if armed else "PAPER",
            "mode_label": label,
            "confirm_phrase": LIVE_CONFIRM_PHRASE,
            "has_keys": settings.has_keys,
        }

    def arm_live(self, confirm_phrase: str, totp_ok: bool = True) -> dict:
        if settings.dry_run:
            return {
                "ok": False,
                "error": "Paper bets are on. Tap Allow live trading first. You still type ARM LIVE before any real bet.",
            }
        if confirm_phrase.strip() != LIVE_CONFIRM_PHRASE:
            return {"ok": False, "error": 'Type ARM LIVE exactly to turn on real orders.'}
        if not totp_ok:
            return {"ok": False, "error": "Extra lock code did not match."}
        if not settings.has_keys:
            return {"ok": False, "error": "Add your Kalshi key in Settings before real orders."}
        self._live_armed = True
        self.store.log_activity("safety", "Real orders armed")
        return {"ok": True, "mode": "LIVE", **self.status()}

    def disarm(self) -> dict:
        self._live_armed = False
        if settings.dry_run:
            message = "Real orders off — paper bets only"
        else:
            message = "Real orders off — live trading still allowed until you arm again"
        self.store.log_activity("safety", message)
        return {"ok": True, **self.status()}

    def place(
        self,
        ticker: str,
        side: str,
        count: int,
        price_cents: Optional[int] = None,
        coherence: Optional[float] = None,
        order_type: str = "limit",
    ) -> dict:
        side = side.lower().strip()
        if side not in {"yes", "no"}:
            return {"ok": False, "error": "side must be yes or no"}
        if count < 1:
            return {"ok": False, "error": "count must be >= 1"}

        client_order_id = str(uuid.uuid4())
        payload: dict[str, Any] = {
            "action": "buy",
            "client_order_id": client_order_id,
            "count": int(count),
            "side": side,
            "ticker": ticker,
            "type": order_type if price_cents is not None else "market",
        }
        if price_cents is not None and order_type == "limit":
            payload["yes_price" if side == "yes" else "no_price"] = int(price_cents)

        price = (price_cents / 100.0) if price_cents is not None else None

        if self.dry_run:
            oid = self.store.log_order(
                dry_run=True,
                ticker=ticker,
                side=side,
                count=count,
                price=price,
                status="PAPER",
                coherence=coherence,
                payload=payload,
                response={"note": "Order NOT sent to Kalshi"},
            )
            msg = f"Paper bet: would BUY {count}x {side.upper()} {ticker}. Not sent to Kalshi."
            self.store.log_activity("order", msg, payload)
            return {
                "ok": True,
                "dry_run": True,
                "order_log_id": oid,
                "message": msg,
                "payload": payload,
            }

        # LIVE
        try:
            resp = self.client.place_order(payload)
            oid = self.store.log_order(
                dry_run=False,
                ticker=ticker,
                side=side,
                count=count,
                price=price,
                status="SUBMITTED",
                coherence=coherence,
                payload=payload,
                response=resp,
            )
            order_id = resp.get("order_id") if isinstance(resp, dict) else None
            msg = f"[LIVE] Submitted BUY {count}x {side.upper()} {ticker}"
            if order_id:
                msg = f"{msg} ({order_id})"
            self.store.log_activity("order", msg, {"payload": payload, "response": resp})
            return {"ok": True, "dry_run": False, "order_log_id": oid, "response": resp, "message": msg}
        except Exception as e:
            oid = self.store.log_order(
                dry_run=False,
                ticker=ticker,
                side=side,
                count=count,
                price=price,
                status="ERROR",
                coherence=coherence,
                payload=payload,
                response={"error": str(e)},
            )
            self.store.log_activity("error", f"Live order failed: {e}", payload)
            return {"ok": False, "error": str(e), "order_log_id": oid}

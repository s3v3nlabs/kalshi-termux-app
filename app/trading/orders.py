"""Order gate. Real orders only when live trading is allowed AND ARM LIVE is set."""
from __future__ import annotations

import uuid
from typing import Any, Optional

from app.config import settings
from app.db.store import Store
from app.kalshi.client import ORDER_TYPES, KalshiClient, friendly_order_error, to_create_order_v2


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
        """Buy YES or NO. ``price_cents`` is the max you will pay (required).

        The same Create Order V2 body is built in paper and live mode, so a
        paper bet proves the live request would be valid.
        """
        side = str(side or "").lower().strip()
        ticker = str(ticker or "").strip()
        if side not in {"yes", "no"}:
            return {"ok": False, "error": "Pick YES or NO.", "error_code": "invalid_order"}
        if not ticker:
            return {"ok": False, "error": "Pick a market first.", "error_code": "invalid_order"}
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            return {"ok": False, "error": "Contracts must be a whole number, 1 or more.", "error_code": "invalid_order"}
        if price_cents is None:
            return {
                "ok": False,
                "error": "Set a max price in cents (1-99). Orders without a price are not sent.",
                "error_code": "price_required",
            }
        order_type = str(order_type or "limit").strip().lower()
        if order_type not in ORDER_TYPES:
            return {"ok": False, "error": "Order type must be limit, ioc, or fok.", "error_code": "invalid_order"}

        client_order_id = str(uuid.uuid4())
        payload: dict[str, Any] = {
            "action": "buy",
            "client_order_id": client_order_id,
            "count": count,
            "side": side,
            "ticker": ticker,
            "type": order_type,
            ("yes_price" if side == "yes" else "no_price"): price_cents,
        }
        try:
            v2_body = to_create_order_v2(payload)
        except ValueError as e:
            return {"ok": False, **friendly_order_error(e)}

        price = price_cents / 100.0
        cost = round(price * count, 2)

        if self.dry_run:
            oid = self.store.log_order(
                dry_run=True,
                ticker=ticker,
                side=side,
                count=count,
                price=price,
                status="PAPER",
                coherence=coherence,
                payload=v2_body,
                response={"note": "Order NOT sent to Kalshi"},
            )
            msg = (
                f"Paper bet: would BUY {count}x {side.upper()} {ticker} at up to {price_cents}¢ "
                f"(max ${cost:.2f}). Not sent to Kalshi."
            )
            self.store.log_activity("order", msg, v2_body)
            return {
                "ok": True,
                "dry_run": True,
                "order_log_id": oid,
                "message": msg,
                "payload": payload,
                "v2_request": v2_body,
            }

        # LIVE
        try:
            resp = self.client.place_order(v2_body)
        except Exception as e:
            info = friendly_order_error(e)
            oid = self.store.log_order(
                dry_run=False,
                ticker=ticker,
                side=side,
                count=count,
                price=price,
                status="ERROR",
                coherence=coherence,
                payload=v2_body,
                response={"error": str(e), **info},
            )
            self.store.log_activity("error", f"Live order failed: {info['error']}", v2_body)
            return {"ok": False, "dry_run": False, "order_log_id": oid, **info}

        ack = resp if isinstance(resp, dict) else {}
        order_id = ack.get("order_id")
        filled = ack.get("fill_count")
        remaining = ack.get("remaining_count")
        oid = self.store.log_order(
            dry_run=False,
            ticker=ticker,
            side=side,
            count=count,
            price=price,
            status="SUBMITTED",
            coherence=coherence,
            payload=v2_body,
            response=resp,
        )
        msg = f"[LIVE] Sent BUY {count}x {side.upper()} {ticker} at up to {price_cents}¢"
        if filled is not None:
            msg += f" · filled {filled}, resting {remaining}"
        if order_id:
            msg += f" ({order_id})"
        self.store.log_activity("order", msg, {"request": v2_body, "response": resp})
        return {
            "ok": True,
            "dry_run": False,
            "order_log_id": oid,
            "response": resp,
            "message": msg,
            "v2_request": v2_body,
        }

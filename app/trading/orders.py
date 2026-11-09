"""Order gate. PAPER vs LIVE mode. LIVE sends real orders when Kalshi keys exist."""
from __future__ import annotations

import threading
import uuid
from typing import Any, Optional

from app.config import settings
from app.db.ledger import Ledger
from app.db.store import Store
from app.kalshi.client import ORDER_TYPES, KalshiClient, friendly_order_error, to_create_order_v2


class OrderGate:
    def __init__(self, store: Store, client: Optional[KalshiClient] = None, ledger: Optional[Ledger] = None):
        self.store = store
        self.client = client or KalshiClient()
        self.ledger = ledger
        # Live orders still resting after the ack, by Kalshi order_id. Fills
        # from the WebSocket fill stream are recorded against these.
        self.resting: dict[str, dict] = {}
        self._resting_lock = threading.Lock()

    def _record_fill(self, ticker: str, side: str, contracts: float, price: float, source: str = "manual") -> Optional[int]:
        """Write a bets row for a fill. Model prob = latest model run for the ticker."""
        if self.ledger is None or contracts <= 0 or not 0 < price < 1:
            return None
        model_prob = None
        try:
            run = self.ledger.latest_model_run_sync(ticker)
            if run is not None:
                p_yes = float(run["model_prob"])
                model_prob = p_yes if side == "yes" else 1.0 - p_yes
            return self.ledger.add_bet_sync(
                ticker=ticker,
                side=side,
                price=price,
                stake=round(contracts * price, 6),
                model_prob=model_prob,
                market_prob=price,
                source=source,
            )
        except Exception as exc:  # ledger trouble must never block an order result
            self.store.log_activity("error", f"Could not record bet: {exc}")
            return None

    @property
    def dry_run(self) -> bool:
        """True only in PAPER mode. LIVE without keys refuses the order (does not paper)."""
        return bool(settings.dry_run)

    @property
    def live_armed(self) -> bool:
        """True when mode is LIVE (persisted). Real sends still need Kalshi keys."""
        return not bool(settings.dry_run)

    def status(self) -> dict:
        paper = bool(settings.dry_run)
        live = not paper
        has_keys = bool(settings.has_keys)
        if paper:
            label = "PAPER"
            detail = "Paper bets — no money is sent"
        elif not has_keys:
            label = "Live — add Kalshi keys in Settings"
            detail = "Live mode on. Add Kalshi keys in Settings before orders spend money."
        else:
            label = "LIVE"
            detail = "Live — real orders use your Kalshi balance"
        return {
            "dry_run": paper,
            "env_dry_run": bool(settings.env_dry_run),
            "live_allowed": live,
            "live_armed": live,
            "orders_are_paper": paper,
            "effective_mode": "PAPER" if paper else "LIVE",
            "mode_label": label,
            "mode_detail": detail,
            "has_keys": has_keys,
            "can_trade_live": live and has_keys,
            "confirm_phrase": "",
        }

    def set_live(self, totp_ok: bool = True) -> dict:
        """Switch to LIVE mode. No typed phrase. Optional TOTP if the user enabled it."""
        if not totp_ok:
            return {"ok": False, "error": "Extra lock code did not match."}
        settings.dry_run = False
        self.store.log_activity("safety", "LIVE mode on")
        return {"ok": True, "mode": "LIVE", **self.status()}

    def set_paper(self) -> dict:
        """Switch to PAPER mode instantly."""
        settings.dry_run = True
        self.store.log_activity("safety", "PAPER mode on — no money is sent")
        return {"ok": True, "mode": "PAPER", **self.status()}

    def arm_live(self, confirm_phrase: str = "", totp_ok: bool = True) -> dict:
        """Compat alias: switch to LIVE. Typed ARM LIVE is no longer required."""
        return self.set_live(totp_ok=totp_ok)

    def disarm(self) -> dict:
        """Compat alias: switch to PAPER."""
        return self.set_paper()

    def place(
        self,
        ticker: str,
        side: str,
        count: int,
        price_cents: Optional[int] = None,
        coherence: Optional[float] = None,
        order_type: str = "limit",
        source: str = "manual",
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
            # Paper bets are recorded as filled at the max price so the model
            # can be calibrated without spending money (see BUILD_NOTES).
            bet_id = self._record_fill(ticker, side, float(count), price, source=source)
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
                "bet_id": bet_id,
            }

        # LIVE without keys: refuse — never silently paper-trade
        if not settings.has_keys:
            msg = "Live — add Kalshi keys in Settings"
            self.store.log_activity("safety", msg, {"ticker": ticker})
            return {
                "ok": False,
                "dry_run": False,
                "error": msg,
                "error_code": "keys_required",
                "mode_label": msg,
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
        bet_id = None
        try:
            filled_n = float(filled or 0)
        except (TypeError, ValueError):
            filled_n = 0.0
        if filled_n > 0:
            try:
                fill_px = float(ack.get("average_fill_price") or 0) or None
            except (TypeError, ValueError):
                fill_px = None
            if fill_px is None:
                fill_px = price
            elif side == "no":
                # V2 reports the YES-book price; a NO buyer paid 1 - that.
                fill_px = round(1.0 - fill_px, 6)
            bet_id = self._record_fill(ticker, side, filled_n, fill_px, source=source)
        try:
            remaining_n = float(remaining or 0)
        except (TypeError, ValueError):
            remaining_n = 0.0
        if order_id and remaining_n > 0:
            with self._resting_lock:
                self.resting[str(order_id)] = {
                    "order_id": str(order_id), "ticker": ticker, "side": side, "source": source,
                    "remaining": remaining_n, "filled": filled_n, "order_log_id": oid,
                    "client_order_id": client_order_id, "price_cents": price_cents,
                }
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
            "bet_id": bet_id,
            "order_id": order_id,
        }

    # ----- fill stream (WebSocket warp-ws: fill / user_orders) -----

    def on_fill(self, msg: dict) -> Optional[int]:
        """A later fill on one of our resting live orders: record it as a bet."""
        oid = str(msg.get("order_id") or "")
        with self._resting_lock:
            entry = self.resting.get(oid)
            if entry is None:
                return None
            try:
                n = float(msg.get("count_fp") or msg.get("count") or 0)
                yes_px = float(msg.get("yes_price_dollars"))
            except (TypeError, ValueError):
                return None
            entry["remaining"] = max(0.0, entry["remaining"] - n)
            entry["filled"] = entry.get("filled", 0.0) + n
            done = entry["remaining"] <= 0
            if done:
                self.resting.pop(oid, None)
        px = yes_px if entry["side"] == "yes" else round(1.0 - yes_px, 6)
        bet_id = self._record_fill(entry["ticker"], entry["side"], n, px, source=entry["source"])
        self.store.log_activity("order", f"[LIVE] Fill {n:g}x {entry['side'].upper()} {entry['ticker']} @ {px * 100:.0f}¢ ({entry['source']})")
        if done and self.ledger is not None and entry.get("order_log_id"):
            try:
                self.ledger.set_auto_order_status_sync(entry["order_log_id"], "FILLED")
            except Exception:
                pass
        return bet_id

    def on_user_order(self, msg: dict) -> None:
        """Order status push: drop canceled/executed orders from the resting map."""
        oid = str(msg.get("order_id") or "")
        status = str(msg.get("status") or "").lower()
        if status not in {"canceled", "cancelled", "executed"}:
            return
        with self._resting_lock:
            entry = self.resting.pop(oid, None)
        if entry is None or self.ledger is None or not entry.get("order_log_id"):
            return
        final = "FILLED" if status == "executed" else ("PARTIAL" if entry.get("filled") else "CANCELED")
        try:
            self.ledger.set_auto_order_status_sync(entry["order_log_id"], final)
        except Exception:
            pass

    def cancel(self, order_id: str, ticker: Optional[str] = None) -> dict:
        """Cancel a resting live order (Cancel Order V2)."""
        try:
            resp = self.client.cancel_order(order_id, market_ticker=ticker)
        except Exception as e:
            return {"ok": False, **friendly_order_error(e)}
        with self._resting_lock:
            entry = self.resting.pop(str(order_id), None)
        if entry and self.ledger is not None and entry.get("order_log_id"):
            try:
                self.ledger.set_auto_order_status_sync(entry["order_log_id"], "PARTIAL" if entry.get("filled") else "CANCELED")
            except Exception:
                pass
        self.store.log_activity("order", f"[LIVE] Canceled resting order {order_id} ({ticker or ''})")
        return {"ok": True, "response": resp}

"""Kalshi Trade API v2 client — RSA-PSS auth + public endpoints.

Signature scheme ported from Operator's Gemini engine scraps:
  payload = f"{timestamp_ms}{METHOD}{path}{optional_body}"
  RSA-PSS SHA-256, salt_length=DIGEST_LENGTH, base64 signature.
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from app.config import settings
from app.kalshi.rate_limit import TokenBucketManager

# Create Order (V2). The legacy POST /portfolio/orders path returns HTTP 410
# deprecated_v1_order_endpoint. Reads stay on /portfolio/orders.
# https://docs.kalshi.com/api-reference/orders/create-order-v2
CREATE_ORDER_V2_PATH = "/trade-api/v2/portfolio/events/orders"


def cents_to_fixed_dollars(cents: int) -> str:
    """Integer cents (1–99) → fixed-point dollar string, e.g. 22 → \"0.2200\"."""
    if isinstance(cents, bool) or not isinstance(cents, int) or not 1 <= cents <= 99:
        raise ValueError("price must be an integer number of cents from 1 to 99")
    return f"0.{cents:02d}00"


def fixed_point_count(count: int) -> str:
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("count must be an integer >= 1")
    return f"{count:.2f}"


def to_create_order_v2(order: dict) -> dict:
    """Map this app's buy yes/no payload onto Kalshi Create Order V2.

    V2 quotes a single YES book: ``bid`` buys YES, ``ask`` buys NO at
    ``1 - price``. ``price`` is fixed-point dollars. A blank price (the UI's
    market-style order) becomes an immediate-or-cancel at 99 cents on that
    outcome, because V2 has no separate market type.
    """
    side = str(order.get("side", "")).lower()
    if side in {"bid", "ask"}:
        return _passthrough_create_order_v2(order)
    if side not in {"yes", "no"}:
        raise ValueError("side must be yes or no")
    action = str(order.get("action") or "buy").lower()
    if action != "buy":
        raise ValueError("only buy orders are supported")
    ticker = str(order.get("ticker") or "").strip()
    if not ticker:
        raise ValueError("ticker is required")

    count = fixed_point_count(order.get("count"))
    order_type = str(order.get("type") or "limit").lower()
    price_key = "yes_price" if side == "yes" else "no_price"
    raw_price = order.get(price_key)
    if raw_price is None and order_type != "market":
        order_type = "market"

    if order_type == "market" and raw_price is None:
        # Pay up to 99¢ for the chosen outcome, then cancel any remainder.
        if side == "yes":
            book_side = "bid"
            price = cents_to_fixed_dollars(99)
        else:
            book_side = "ask"
            price = cents_to_fixed_dollars(1)
        time_in_force = "immediate_or_cancel"
    else:
        if raw_price is None:
            raise ValueError("limit order requires a price")
        if isinstance(raw_price, bool) or not isinstance(raw_price, int):
            raise ValueError("price must be an integer number of cents from 1 to 99")
        if side == "yes":
            book_side = "bid"
            price = cents_to_fixed_dollars(raw_price)
        else:
            # Buying NO at N cents rests as an ask at (100 − N) cents on the YES book.
            book_side = "ask"
            price = cents_to_fixed_dollars(100 - raw_price)
        time_in_force = "immediate_or_cancel" if order_type == "market" else "good_till_canceled"

    body: dict[str, Any] = {"ticker": ticker}
    client_order_id = order.get("client_order_id")
    if client_order_id:
        body["client_order_id"] = str(client_order_id)
    body.update(
        {
            "side": book_side,
            "count": count,
            "price": price,
            "time_in_force": time_in_force,
            "self_trade_prevention_type": "taker_at_cross",
        }
    )
    return body


def _passthrough_create_order_v2(order: dict) -> dict:
    """Accept an already-built V2 body without translating yes/no prices."""
    required = ("ticker", "side", "count", "price", "time_in_force", "self_trade_prevention_type")
    missing = [key for key in required if order.get(key) in (None, "")]
    if missing:
        raise ValueError("V2 order missing " + ", ".join(missing))
    count = order["count"]
    if isinstance(count, str):
        count_fp = count
    else:
        count_fp = fixed_point_count(count)
    price = order["price"]
    if not isinstance(price, str):
        raise ValueError("V2 price must be a fixed-point dollar string")
    body: dict[str, Any] = {
        "ticker": str(order["ticker"]),
        "side": str(order["side"]),
        "count": count_fp,
        "price": price,
        "time_in_force": str(order["time_in_force"]),
        "self_trade_prevention_type": str(order["self_trade_prevention_type"]),
    }
    if order.get("client_order_id"):
        body = {
            "ticker": body["ticker"],
            "client_order_id": str(order["client_order_id"]),
            **{k: v for k, v in body.items() if k != "ticker"},
        }
    return body


class KalshiClient:
    def __init__(
        self,
        host: Optional[str] = None,
        key_id: Optional[str] = None,
        key_path: Optional[Path] = None,
        bucket: Optional[TokenBucketManager] = None,
    ):
        self.host = (host or settings.api_base).rstrip("/")
        self.key_id = key_id if key_id is not None else settings.key_id
        self.key_path = Path(key_path) if key_path else settings.key_path
        self.bucket = bucket or TokenBucketManager(tier="basic")
        self._session = requests.Session()
        self._private_key = None
        self.last_host_used = self.host
        self.last_error: Optional[str] = None

    # ----- crypto -----

    def _load_key(self):
        if self._private_key is not None:
            return self._private_key
        if not self.key_path.is_file():
            raise FileNotFoundError(f"API key file not found: {self.key_path}")
        with open(self.key_path, "rb") as f:
            self._private_key = serialization.load_pem_private_key(f.read(), password=None)
        return self._private_key

    def generate_signature(self, method: str, path: str, timestamp: str, body_str: str = "") -> str:
        private_key = self._load_key()
        # Kalshi signs path without query string for some endpoints;
        # use full path as passed (including query) — matches engine scraps.
        path_for_sig = path.split("?")[0]
        payload = f"{timestamp}{method.upper()}{path_for_sig}{body_str}"
        signature = private_key.sign(
            payload.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _auth_headers(self, method: str, path: str, body_str: str = "") -> dict:
        if not self.key_id:
            raise ValueError("KALSHI_KEY_ID not configured")
        ts = str(int(time.time() * 1000))
        sig = self.generate_signature(method, path, ts, body_str)
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ----- transport -----

    def request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        auth: bool = False,
        timeout: float = 12.0,
    ) -> dict[str, Any]:
        """Return parsed JSON or raise with useful context."""
        is_write = method.upper() in {"POST", "PUT", "DELETE", "PATCH"}
        if is_write:
            self.bucket.consume_write(1.0)
        else:
            self.bucket.consume_read(1.0)

        if not path.startswith("/"):
            path = "/" + path
        url = f"{self.host}{path}"
        body_str = json.dumps(json_body) if json_body is not None else ""
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if auth:
            # Sign path with query stripped (Kalshi RSA-PSS convention)
            headers = self._auth_headers(method, path, body_str)

        self.last_host_used = self.host
        self.last_error = None
        try:
            resp = self._session.request(
                method.upper(),
                url,
                headers=headers,
                params=params,
                data=body_str if body_str else None,
                timeout=timeout,
            )
        except requests.RequestException as e:
            self.last_error = str(e)
            raise RuntimeError(f"Network error contacting {self.host}: {e}") from e

        if resp.status_code >= 400:
            self.last_error = f"HTTP {resp.status_code}: {resp.text[:400]}"
            raise RuntimeError(self.last_error)

        if not resp.content:
            return {}
        return resp.json()

    def public_get(self, path: str, params: Optional[dict] = None) -> dict:
        return self.request("GET", path, params=params, auth=False)

    def auth_get(self, path: str, params: Optional[dict] = None) -> dict:
        return self.request("GET", path, params=params, auth=True)

    def auth_post(self, path: str, body: dict) -> dict:
        return self.request("POST", path, json_body=body, auth=True)

    # ----- public endpoints -----

    def exchange_status(self) -> dict:
        return self.public_get("/trade-api/v2/exchange/status")

    def get_markets(
        self,
        status: str = "open",
        limit: int = 100,
        cursor: Optional[str] = None,
        series_ticker: Optional[str] = None,
        ticker: Optional[str] = None,
        mve_filter: Optional[str] = "exclude",
    ) -> dict:
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        if series_ticker:
            params["series_ticker"] = series_ticker
        if ticker:
            params["ticker"] = ticker
        # Exclude multivariate/combo (CROSSCATEGORY) by default — otherwise the
        # open book is dominated by unpriced MVE legs.
        if mve_filter:
            params["mve_filter"] = mve_filter
        # Prefer public; fall back to auth if needed
        try:
            return self.public_get("/trade-api/v2/markets", params=params)
        except RuntimeError:
            if self.key_id and self.key_path.is_file():
                return self.auth_get("/trade-api/v2/markets", params=params)
            raise

    def get_events(
        self,
        series_ticker: str,
        status: str = "open",
        limit: int = 30,
        cursor: Optional[str] = None,
        with_nested_markets: bool = True,
    ) -> dict:
        """Open events for one series. Nested markets follow soonest-first."""
        params: dict[str, Any] = {
            "series_ticker": series_ticker,
            "limit": limit,
            "with_nested_markets": "true" if with_nested_markets else "false",
        }
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        return self.public_get("/trade-api/v2/events", params=params)

    def get_market(self, ticker: str) -> dict:
        return self.public_get(f"/trade-api/v2/markets/{ticker}")

    def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        return self.public_get(
            f"/trade-api/v2/markets/{ticker}/orderbook",
            params={"depth": depth},
        )

    # ----- authenticated -----

    def get_balance(self) -> dict:
        return self.auth_get("/trade-api/v2/portfolio/balance")

    def get_positions(self, limit: int = 100) -> dict:
        return self.auth_get("/trade-api/v2/portfolio/positions", params={"limit": limit})

    def get_orders(self, status: Optional[str] = None, limit: int = 20) -> dict:
        # Reads stay on /portfolio/orders. The events/orders path is write-only.
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        return self.auth_get("/trade-api/v2/portfolio/orders", params=params)

    def get_fills(self, limit: int = 20) -> dict:
        return self.auth_get("/trade-api/v2/portfolio/fills", params={"limit": limit})

    def place_order(self, order: dict) -> dict:
        """Submit a live order via Create Order V2. Returns the V2 ack."""
        body = to_create_order_v2(order)
        return self.auth_post(CREATE_ORDER_V2_PATH, body)

    def ping(self) -> dict:
        """Lightweight connectivity check with host metadata."""
        data = self.exchange_status()
        return {
            "ok": True,
            "host": self.host,
            "host_key": settings.host_key,
            "exchange_active": data.get("exchange_active"),
            "trading_active": data.get("trading_active", data.get("exchange_active")),
            "raw": data,
            "authenticated": bool(self.key_id and self.key_path.is_file()),
        }

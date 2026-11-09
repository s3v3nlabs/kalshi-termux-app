"""Kalshi Trade API v2 client — signed auth + public endpoints.

Signature scheme (docs.kalshi.com/getting_started/api_keys, rechecked Nov 2026):
  pre-sign text = f"{timestamp_ms}{METHOD}{path_without_query}"   (no body)
  RSA keys:     RSA-PSS SHA-256, MGF1 SHA-256, salt_length=DIGEST_LENGTH
  Ed25519 keys: Ed25519 over the pre-sign text
  base64 signature in KALSHI-ACCESS-SIGNATURE.
"""
from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.config import settings
from app.kalshi.rate_limit import TokenBucketManager

# Create Order (V2). The legacy POST /portfolio/orders path returns HTTP 410
# deprecated_v1_order_endpoint. Reads stay on /portfolio/orders.
# https://docs.kalshi.com/api-reference/orders/create-order-v2
CREATE_ORDER_V2_PATH = "/trade-api/v2/portfolio/events/orders"

# Limits this app enforces on top of Kalshi's own validation.
#  * Prices are whole cents, 1-99. Whole cents are valid on every Kalshi
#    price_ranges grid (linear, deci-cent, tapered, ...), so we never need to
#    read the per-market grid to stay valid. Sub-cent prices are rejected here.
#  * Counts are whole contracts. Kalshi accepts fractional counts ("2.50") on
#    some markets; this app does not send them.
MIN_PRICE_CENTS = 1
MAX_PRICE_CENTS = 99
MAX_CONTRACTS_PER_ORDER = 10_000

ORDER_TYPES = {"limit", "ioc", "fok"}
TIME_IN_FORCE = {"good_till_canceled", "immediate_or_cancel", "fill_or_kill"}
SELF_TRADE_PREVENTION = {"taker_at_cross", "maker"}
BOOK_SIDES = {"bid", "ask"}
_ORDER_TYPE_TIF = {
    "limit": "good_till_canceled",
    "ioc": "immediate_or_cancel",
    "fok": "fill_or_kill",
}
_V2_ALLOWED_KEYS = {
    "ticker",
    "client_order_id",
    "side",
    "count",
    "price",
    "time_in_force",
    "self_trade_prevention_type",
    "expiration_time",
    "post_only",
    "cancel_order_on_pause",
    "reduce_only",
    "subaccount",
    "order_group_id",
    "exchange_index",
}
_V2_BOOL_KEYS = ("post_only", "cancel_order_on_pause", "reduce_only")
_COUNT_RE = re.compile(r"^\d+(?:\.0{1,2})?$")
_PRICE_RE = re.compile(r"^0?\.(\d{2})(?:0{1,2})?$")


def _whole_cents(value: Any, label: str = "price") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be a whole number of cents from 1 to 99")
    if not MIN_PRICE_CENTS <= value <= MAX_PRICE_CENTS:
        raise ValueError(f"{label} must be a whole number of cents from 1 to 99")
    return value


def cents_to_fixed_dollars(cents: int) -> str:
    """Integer cents (1-99) -> fixed-point dollar string, e.g. 22 -> "0.2200"."""
    return f"0.{_whole_cents(cents):02d}00"


def fixed_point_count(count: int) -> str:
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("count must be a whole number of contracts (1 or more)")
    if count > MAX_CONTRACTS_PER_ORDER:
        raise ValueError(f"count must be at most {MAX_CONTRACTS_PER_ORDER} contracts per order")
    return f"{count}.00"


def _parse_fixed_count(value: Any) -> str:
    if isinstance(value, str):
        text = value.strip()
        if not _COUNT_RE.match(text):
            raise ValueError("V2 count must be whole contracts, e.g. \"3\" or \"3.00\"")
        return fixed_point_count(int(text.split(".")[0]))
    return fixed_point_count(value)


def _parse_fixed_price(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("V2 price must be a fixed-point dollar string like \"0.2200\"")
    m = _PRICE_RE.match(value.strip())
    if not m:
        raise ValueError("V2 price must be whole cents between \"0.01\" and \"0.99\"")
    return cents_to_fixed_dollars(int(m.group(1)))


def to_create_order_v2(order: dict) -> dict:
    """Map this app's buy yes/no payload onto Kalshi Create Order V2.

    V2 quotes a single YES book: ``bid`` buys YES at ``price``; ``ask`` sells
    YES, which is the same as buying NO at ``1 - price``. So buy NO at 40c
    becomes ``side=ask, price="0.6000"``.

    Every order needs an explicit max price (``yes_price``/``no_price`` in
    whole cents). There is no implicit 99c "market" order. Order ``type``:
      * ``limit`` -> good_till_canceled (rests on the book)
      * ``ioc``   -> immediate_or_cancel (fill what you can now, cancel rest)
      * ``fok``   -> fill_or_kill (all now or nothing)
    ``market`` is accepted as a legacy alias for ``ioc`` but still needs a
    max price. Anything else is rejected.
    """
    if not isinstance(order, dict):
        raise ValueError("order must be an object")
    side = str(order.get("side", "")).strip().lower()
    if side in BOOK_SIDES:
        return _passthrough_create_order_v2(order)
    if side not in {"yes", "no"}:
        raise ValueError("side must be yes or no")
    action = str(order.get("action") or "buy").strip().lower()
    if action != "buy":
        raise ValueError("only buy orders are supported")
    ticker = str(order.get("ticker") or "").strip()
    if not ticker:
        raise ValueError("ticker is required")

    count = fixed_point_count(order.get("count"))
    order_type = str(order.get("type") or "limit").strip().lower()
    if order_type == "market":
        order_type = "ioc"
    if order_type not in ORDER_TYPES:
        raise ValueError("order type must be limit, ioc, or fok")

    price_key = "yes_price" if side == "yes" else "no_price"
    raw_price = order.get(price_key)
    if raw_price is None:
        raise ValueError("Set a max price in cents (1-99). Orders without a price are not sent.")
    cents = _whole_cents(raw_price)
    if side == "yes":
        book_side, price = "bid", cents_to_fixed_dollars(cents)
    else:
        # Buying NO at N cents rests as an ask at (100 - N) cents on the YES book.
        book_side, price = "ask", cents_to_fixed_dollars(100 - cents)

    body: dict[str, Any] = {"ticker": ticker}
    client_order_id = order.get("client_order_id")
    if client_order_id:
        body["client_order_id"] = str(client_order_id)
    body.update(
        {
            "side": book_side,
            "count": count,
            "price": price,
            "time_in_force": _ORDER_TYPE_TIF[order_type],
            "self_trade_prevention_type": "taker_at_cross",
        }
    )
    return body


def _passthrough_create_order_v2(order: dict) -> dict:
    """Validate an already-built V2 body. Unknown keys are rejected."""
    unknown = sorted(set(order) - _V2_ALLOWED_KEYS)
    if unknown:
        raise ValueError("V2 order has unsupported fields: " + ", ".join(unknown))
    required = ("ticker", "side", "count", "price", "time_in_force", "self_trade_prevention_type")
    missing = [key for key in required if order.get(key) in (None, "")]
    if missing:
        raise ValueError("V2 order missing " + ", ".join(missing))
    ticker = str(order["ticker"]).strip()
    if not ticker:
        raise ValueError("ticker is required")
    side = str(order["side"]).strip().lower()
    if side not in BOOK_SIDES:
        raise ValueError("V2 side must be bid or ask")
    tif = str(order["time_in_force"]).strip().lower()
    if tif not in TIME_IN_FORCE:
        raise ValueError("time_in_force must be one of " + ", ".join(sorted(TIME_IN_FORCE)))
    stp = str(order["self_trade_prevention_type"]).strip().lower()
    if stp not in SELF_TRADE_PREVENTION:
        raise ValueError("self_trade_prevention_type must be taker_at_cross or maker")

    body: dict[str, Any] = {"ticker": ticker}
    if order.get("client_order_id"):
        body["client_order_id"] = str(order["client_order_id"])
    body.update(
        {
            "side": side,
            "count": _parse_fixed_count(order["count"]),
            "price": _parse_fixed_price(order["price"]),
            "time_in_force": tif,
            "self_trade_prevention_type": stp,
        }
    )
    for key in _V2_BOOL_KEYS:
        if key in order and order[key] is not None:
            if not isinstance(order[key], bool):
                raise ValueError(f"{key} must be true or false")
            body[key] = order[key]
    if body.get("reduce_only") and tif != "immediate_or_cancel":
        raise ValueError("reduce_only orders must use immediate_or_cancel")
    if order.get("expiration_time") is not None:
        exp = order["expiration_time"]
        if isinstance(exp, bool) or not isinstance(exp, int) or exp <= 0:
            raise ValueError("expiration_time must be a Unix timestamp in seconds")
        if tif != "good_till_canceled":
            raise ValueError("expiration_time needs time_in_force good_till_canceled")
        body["expiration_time"] = exp
    for key in ("subaccount",):
        if order.get(key) is not None:
            val = order[key]
            if isinstance(val, bool) or not isinstance(val, int) or not 0 <= val <= 63:
                raise ValueError("subaccount must be an integer from 0 to 63")
            body[key] = val
    if order.get("exchange_index") is not None:
        val = order["exchange_index"]
        if isinstance(val, bool) or not isinstance(val, int) or val < -1:
            raise ValueError("exchange_index must be an integer >= -1")
        body["exchange_index"] = val
    if order.get("order_group_id"):
        body["order_group_id"] = str(order["order_group_id"])
    return body


class KalshiAPIError(RuntimeError):
    """HTTP error from Kalshi with the parsed error body, if any."""

    def __init__(self, status: int, code: str = "", message: str = "", details: str = "", raw: str = ""):
        self.status = int(status)
        self.code = code or ""
        self.api_message = message or ""
        self.details = details or ""
        self.raw = raw or ""
        text = f"HTTP {self.status}"
        if self.code:
            text += f" {self.code}"
        if self.api_message:
            text += f": {self.api_message}"
        elif self.raw:
            text += f": {self.raw[:300]}"
        super().__init__(text)

    @classmethod
    def from_response(cls, resp) -> "KalshiAPIError":
        raw = getattr(resp, "text", "") or ""
        code = message = details = ""
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        if isinstance(data, dict):
            err = data.get("error") if isinstance(data.get("error"), dict) else data
            code = str(err.get("code") or "")
            message = str(err.get("message") or "")
            details = str(err.get("details") or "")
        return cls(resp.status_code, code, message, details, raw[:400])


_CODE_HINTS = {
    "insufficient_balance": "Not enough Kalshi balance for this order.",
    "market_closed": "This market is closed for trading.",
    "market_not_found": "Kalshi could not find that market ticker.",
    "invalid_price": "Kalshi rejected the price. Use whole cents from 1 to 99.",
    "trading_is_paused": "Kalshi trading is paused right now. Try again later.",
    "exchange_closed": "The Kalshi exchange is closed right now.",
    "order_already_exists": "That order was already sent (duplicate client_order_id).",
    "deprecated_v1_order_endpoint": (
        "Kalshi retired the old order endpoint. This app now uses Create Order V2; "
        "update the app if you still see this."
    ),
}


def friendly_order_error(exc: BaseException) -> dict:
    """Turn an order exception into {error, error_code, http_status} for the UI."""
    if isinstance(exc, ValueError):
        return {"error": str(exc), "error_code": "invalid_order", "http_status": None}
    if isinstance(exc, KalshiAPIError):
        hint = _CODE_HINTS.get(exc.code)
        status = exc.status
        if hint is None:
            if status == 410:
                hint = _CODE_HINTS["deprecated_v1_order_endpoint"]
            elif status == 400:
                hint = "Kalshi rejected the order"
            elif status == 401:
                hint = "Kalshi did not accept your key. Check the Key ID and private key in Settings."
            elif status == 403:
                hint = "This Kalshi key is not allowed to trade. Create a key with trading permission."
            elif status == 404:
                hint = "Kalshi could not find that market or endpoint."
            elif status == 409:
                hint = "Kalshi says this order conflicts with an existing one."
            elif status == 429:
                hint = "Too many requests. Wait a few seconds and try again."
            elif status >= 500:
                hint = "Kalshi is having trouble right now. Nothing was confirmed; check Orders before retrying."
            else:
                hint = "Kalshi returned an error"
        detail = exc.api_message or exc.details
        text = hint if not detail or detail in hint else f"{hint} ({detail})"
        return {"error": text, "error_code": exc.code or f"http_{status}", "http_status": status}
    msg = str(exc)
    if "Network error" in msg:
        return {
            "error": "Could not reach Kalshi. Check your connection; nothing was confirmed.",
            "error_code": "network",
            "http_status": None,
        }
    if isinstance(exc, FileNotFoundError):
        return {"error": "Kalshi private key file is missing. Add your key in Settings.", "error_code": "no_key", "http_status": None}
    return {"error": msg, "error_code": "error", "http_status": None}


def detect_key_type(pem: str) -> Optional[str]:
    """Return "ed25519", "rsa", or None. The PEM header alone does not tell."""
    try:
        key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    except (ValueError, TypeError):
        return None
    if isinstance(key, Ed25519PrivateKey):
        return "ed25519"
    if isinstance(key, rsa.RSAPrivateKey):
        return "rsa"
    return None


def _fp(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def summarize_positions(data: dict) -> list[dict]:
    """Plain rows from GET /portfolio/positions (fixed-point _fp/_dollars fields).

    position_fp > 0 means YES contracts held, < 0 means NO contracts held.
    """
    rows = []
    for pos in (data or {}).get("market_positions") or []:
        qty = _fp(pos.get("position_fp"))
        if qty is None:
            qty = _fp(pos.get("position"))
        if not qty:
            continue
        rows.append(
            {
                "ticker": pos.get("ticker"),
                "side": "YES" if qty > 0 else "NO",
                "contracts": abs(qty),
                "exposure_dollars": _fp(pos.get("market_exposure_dollars")),
                "realized_pnl_dollars": _fp(pos.get("realized_pnl_dollars")),
                "fees_paid_dollars": _fp(pos.get("fees_paid_dollars")),
            }
        )
    return rows


# Public-read fallback when a network gets 403 from external-api.
PUBLIC_FALLBACK_HOSTS = {"https://external-api.kalshi.com": "https://api.elections.kalshi.com"}


def presign_text(timestamp: str, method: str, path: str) -> str:
    """Kalshi pre-sign text: timestamp + METHOD + path without the query string."""
    return f"{timestamp}{method.upper()}{path.split('?')[0]}"


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
        """Sign ``timestamp + METHOD + path`` (query stripped). The body is NOT signed.

        Kalshi picks the algorithm from the registered public key, so we sign
        with whatever key type was loaded: Ed25519 signs the text directly,
        RSA uses PSS with SHA-256 / MGF1-SHA-256 / salt = digest length.
        ``body_str`` is accepted for backwards compatibility and ignored.
        """
        private_key = self._load_key()
        payload = presign_text(timestamp, method, path).encode("utf-8")
        if isinstance(private_key, Ed25519PrivateKey):
            signature = private_key.sign(payload)
        elif isinstance(private_key, rsa.RSAPrivateKey):
            signature = private_key.sign(
                payload,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH,
                ),
                hashes.SHA256(),
            )
        else:
            raise ValueError("Kalshi keys must be RSA or Ed25519 private keys")
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
        host: Optional[str] = None,
    ) -> dict[str, Any]:
        """Return parsed JSON or raise with useful context."""
        is_write = method.upper() in {"POST", "PUT", "DELETE", "PATCH"}
        if is_write:
            self.bucket.consume_write(1.0)
        else:
            self.bucket.consume_read(1.0)

        if not path.startswith("/"):
            path = "/" + path
        base = (host or self.host).rstrip("/")
        url = f"{base}{path}"
        body_str = json.dumps(json_body) if json_body is not None else ""
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if auth:
            # Sign path with query stripped (Kalshi RSA-PSS convention)
            headers = self._auth_headers(method, path, body_str)

        self.last_host_used = base
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
            raise RuntimeError(f"Network error contacting {base}: {e}") from e

        if resp.status_code >= 400:
            err = KalshiAPIError.from_response(resp)
            self.last_error = str(err)
            raise err

        if not resp.content:
            return {}
        return resp.json()

    def public_get(self, path: str, params: Optional[dict] = None) -> dict:
        """Unauthenticated GET. If external-api answers 403 (some networks are
        blocked at the edge), retry once on api.elections.kalshi.com, which
        serves the same public data. Signed calls never switch hosts."""
        try:
            return self.request("GET", path, params=params, auth=False)
        except KalshiAPIError as err:
            fallback = PUBLIC_FALLBACK_HOSTS.get(self.host)
            if err.status != 403 or not fallback:
                raise
            # Pass the host per call; never mutate self.host (requests run on
            # several threads at once).
            return self.request("GET", path, params=params, auth=False, host=fallback)

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

"""Async Kalshi REST client: asyncio + httpx, one shared budget, V2 orders only.

This is the Python counterpart of Kotlin coroutines: many requests can be in
flight at once on one thread (``asyncio.gather`` / ``gather_limited``), but
every request still waits on the same token budget as the sync client
(``app.kalshi.budget.get_budget()``), so concurrency makes the app faster
without ever exceeding Kalshi's per-account limits.

Retry policy matches the sync client: GETs retry on network errors, 429 and
5xx; writes retry only on 429 (Kalshi rejected them before processing), never
on a network error or 5xx, so an order is never sent twice. Exponential backoff
with jitter; a 429 also drains the shared bucket so every caller slows down.
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Iterable, Optional

import httpx

from app.config import settings
from app.kalshi.budget import KalshiBudget, get_budget
from app.kalshi.client import (
    BACKOFF_CAP,
    CREATE_ORDER_V2_PATH,
    MAX_TRIES,
    PUBLIC_FALLBACK_HOSTS,
    RETRY_STATUSES_READ,
    RETRY_STATUSES_WRITE,
    KalshiAPIError,
    backoff_delay,
    to_create_order_v2,
)
from app.kalshi.signing import Signer

API = "/trade-api/v2"
LIMITS_PATH = f"{API}/account/limits"
ENDPOINT_COSTS_PATH = f"{API}/account/endpoint_costs"
UPGRADE_PATH = f"{API}/account/api_usage_level/upgrade"
LIVE_DATA_BATCH_MAX = 100  # milestone_ids per /live_data/batch call


class _Resp:
    """Adapter so KalshiAPIError.from_response works with httpx responses."""

    def __init__(self, r: httpx.Response):
        self.status_code = r.status_code
        self.text = r.text


async def gather_limited(coros: Iterable[Awaitable], concurrency: int = 8) -> list:
    """Run coroutines with at most ``concurrency`` in flight; results in order.
    Exceptions are returned in place (never raised) so one failure does not
    cancel the batch."""
    sem = asyncio.Semaphore(max(1, int(concurrency)))

    async def _one(c):
        async with sem:
            try:
                return await c
            except Exception as exc:  # noqa: BLE001 - returned to caller
                return exc

    return await asyncio.gather(*(_one(c) for c in coros))


class AsyncKalshiClient:
    def __init__(
        self,
        host: Optional[str] = None,
        key_id: Optional[str] = None,
        key_path: Optional[Path] = None,
        budget: Optional[KalshiBudget] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        timeout: float = 12.0,
    ):
        self.host = (host or settings.api_base).rstrip("/")
        self.signer = Signer(
            key_id if key_id is not None else settings.key_id,
            Path(key_path) if key_path else settings.key_path,
        )
        self.budget = budget or get_budget()
        self._transport = transport
        self._timeout = timeout
        self._http: Optional[httpx.AsyncClient] = None
        self._sleep = asyncio.sleep
        self._rng = random.random
        self.requests = 0
        self.retries = 0
        self.last_error: Optional[str] = None

    @property
    def has_keys(self) -> bool:
        return self.signer.available

    def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            kw: dict[str, Any] = {
                "timeout": self._timeout,
                "headers": {"Accept": "application/json"},
                # One connection pool to one host: keep-alive cuts TLS setup per call.
                "limits": httpx.Limits(max_connections=16, max_keepalive_connections=8),
            }
            if self._transport is not None:
                kw["transport"] = self._transport
            self._http = httpx.AsyncClient(**kw)
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ----- transport -----

    async def request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        auth: bool = False,
        host: Optional[str] = None,
        units: int = 1,
    ) -> dict:
        method = method.upper()
        if not path.startswith("/"):
            path = "/" + path
        base = (host or self.host).rstrip("/")
        is_write = method in {"POST", "PUT", "DELETE", "PATCH"}
        retry_statuses = RETRY_STATUSES_WRITE if is_write else RETRY_STATUSES_READ
        body = json.dumps(json_body) if json_body is not None else None
        resp: Optional[httpx.Response] = None
        for attempt in range(MAX_TRIES):
            await self.budget.acquire_async(method, path, units=units)
            headers = {"Content-Type": "application/json"} if body is not None else {}
            if auth:
                headers.update(self.signer.headers(method, path))  # fresh timestamp per try
            self.requests += 1
            try:
                resp = await self._client().request(method, base + path, params=params, content=body, headers=headers)
            except httpx.HTTPError as exc:
                self.last_error = str(exc)
                if is_write or attempt == MAX_TRIES - 1:
                    raise RuntimeError(f"Network error contacting {base}: {exc}") from exc
                self.retries += 1
                await self._sleep(backoff_delay(attempt, rng=self._rng))
                continue
            if resp.status_code == 429:
                self.budget.note_429(method, path)
            if resp.status_code in retry_statuses and attempt < MAX_TRIES - 1:
                self.retries += 1
                await self._sleep(min(BACKOFF_CAP, backoff_delay(attempt, rng=self._rng)))
                continue
            break
        assert resp is not None
        if resp.status_code >= 400:
            err = KalshiAPIError.from_response(_Resp(resp))
            self.last_error = str(err)
            raise err
        if not resp.content:
            return {}
        return resp.json()

    async def public_get(self, path: str, params: Optional[dict] = None) -> dict:
        try:
            return await self.request("GET", path, params=params)
        except KalshiAPIError as err:
            fallback = PUBLIC_FALLBACK_HOSTS.get(self.host)
            if err.status != 403 or not fallback:
                raise
            return await self.request("GET", path, params=params, host=fallback)

    async def auth_get(self, path: str, params: Optional[dict] = None) -> dict:
        return await self.request("GET", path, params=params, auth=True)

    async def paginate(
        self,
        path: str,
        items_key: str,
        params: Optional[dict] = None,
        *,
        auth: bool = False,
        max_pages: int = 10,
        page_limit: int = 100,
    ) -> AsyncIterator[dict]:
        """Cursor pagination (docs: getting_started/pagination). Yields items
        until ``cursor`` is empty or ``max_pages`` is reached."""
        q = dict(params or {})
        q.setdefault("limit", page_limit)
        cursor = None
        for _ in range(max(1, max_pages)):
            if cursor:
                q["cursor"] = cursor
            data = await (self.auth_get(path, q) if auth else self.public_get(path, q))
            for item in data.get(items_key) or []:
                yield item
            cursor = data.get("cursor") or None
            if not cursor:
                return

    async def collect(self, path: str, items_key: str, params: Optional[dict] = None, **kw) -> list[dict]:
        return [item async for item in self.paginate(path, items_key, params, **kw)]

    # ----- public reads -----

    async def exchange_status(self) -> dict:
        return await self.public_get(f"{API}/exchange/status")

    async def endpoint_costs(self) -> dict:
        return await self.public_get(ENDPOINT_COSTS_PATH)

    async def get_markets(self, **params) -> dict:
        params.setdefault("status", "open")
        params.setdefault("mve_filter", "exclude")
        return await self.public_get(f"{API}/markets", {k: v for k, v in params.items() if v is not None})

    async def markets_closing_between(self, min_close_ts: int, max_close_ts: int, max_pages: int = 3) -> list[dict]:
        return await self.collect(
            f"{API}/markets",
            "markets",
            {"status": "open", "mve_filter": "exclude", "min_close_ts": int(min_close_ts), "max_close_ts": int(max_close_ts)},
            max_pages=max_pages,
        )

    async def get_market(self, ticker: str) -> dict:
        return await self.public_get(f"{API}/markets/{ticker}")

    async def get_series(self, series_ticker: str) -> dict:
        return await self.public_get(f"{API}/series/{series_ticker}")

    async def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        return await self.public_get(f"{API}/markets/{ticker}/orderbook", {"depth": depth})

    async def get_orderbooks(self, tickers: list[str], concurrency: int = 8) -> dict[str, Any]:
        """Many books at once, still metered by the shared budget."""
        res = await gather_limited((self.get_orderbook(t) for t in tickers), concurrency)
        return dict(zip(tickers, res))

    async def milestones_for_event(self, event_ticker: str) -> list[dict]:
        data = await self.public_get(f"{API}/milestones", {"related_event_ticker": event_ticker, "limit": 5})
        return data.get("milestones") or []

    async def live_data_batch(self, milestone_ids: list[str]) -> list[dict]:
        """GET /live_data/batch (<=100 ids per call). ``details.status`` here is
        fresher than the milestone's own status; prefer it."""
        out: list[dict] = []
        ids = [m for m in dict.fromkeys(milestone_ids) if m]
        for i in range(0, len(ids), LIVE_DATA_BATCH_MAX):
            chunk = ids[i : i + LIVE_DATA_BATCH_MAX]
            data = await self.public_get(f"{API}/live_data/batch", {"milestone_ids": chunk})
            out.extend(data.get("live_datas") or [])
        return out

    # ----- account (keys) -----

    async def get_limits(self) -> dict:
        return await self.auth_get(LIMITS_PATH)

    async def upgrade_usage_level(self) -> dict:
        """POST /account/api_usage_level/upgrade: permanent Advanced grant once
        one of your last 100 orders came from the API. Costs 30 write tokens."""
        return await self.request("POST", UPGRADE_PATH, auth=True)

    async def get_balance(self) -> dict:
        return await self.auth_get(f"{API}/portfolio/balance")

    async def get_positions(self, limit: int = 200) -> dict:
        return await self.auth_get(f"{API}/portfolio/positions", {"limit": limit})

    async def get_orders(self, status: Optional[str] = None, limit: int = 200) -> dict:
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        return await self.auth_get(f"{API}/portfolio/orders", params)

    # ----- V2 orders only -----

    async def create_order_v2(self, order: dict) -> dict:
        """POST /portfolio/events/orders (Create Order V2). Same mapping and
        validation as the sync client; legacy V1 create is never used."""
        return await self.request("POST", CREATE_ORDER_V2_PATH, json_body=to_create_order_v2(order), auth=True)

    async def cancel_order_v2(self, order_id: str, market_ticker: Optional[str] = None) -> dict:
        """DELETE /portfolio/events/orders/{order_id} (Cancel Order V2, 2 tokens).
        ``market_ticker`` lets Kalshi auto-route to the right shard."""
        params = {"market_ticker": market_ticker} if market_ticker else None
        return await self.request("DELETE", f"{CREATE_ORDER_V2_PATH}/{order_id}", params=params, auth=True)

    # ----- budget bootstrap -----

    async def load_budget(self, *, costs: bool = True, limits: bool = True) -> dict:
        """Size the shared budget from Kalshi: endpoint costs (public) and, with
        keys, GET /account/limits. Failures keep the conservative defaults."""
        out: dict[str, Any] = {"costs": None, "limits": None, "errors": []}
        if costs:
            try:
                out["costs"] = self.budget.apply_endpoint_costs(await self.endpoint_costs())
            except Exception as exc:
                out["errors"].append(f"endpoint_costs: {exc}")
        if limits and self.has_keys:
            try:
                data = await self.get_limits()
                self.budget.apply_limits(data)
                out["limits"] = data
            except Exception as exc:
                out["errors"].append(f"limits: {exc}")
        out["at"] = time.time()
        return out

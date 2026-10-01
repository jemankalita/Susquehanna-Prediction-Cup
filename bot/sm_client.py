"""Thin client for the official Super Market API (/api/v1) with rate limiting and retries."""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque

import requests

from .config import SM_BASE_URL

log = logging.getLogger(__name__)

# Account budget is 100 reads + 30 writes per minute; stay below it.
READS_PER_MIN = 85
WRITES_PER_MIN = 25
TIMEOUT_S = 20
MAX_RETRIES = 4
PRICE_IDS_PER_CALL = 100
MARKETS_PAGE_LIMIT = 100   # endpoint max
ORDERS_PAGE_LIMIT = 200


class ApiError(RuntimeError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code = status, code


class _RateLimiter:
    """Sliding 60-second window."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self.calls: deque[float] = deque()
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            while self.calls and now - self.calls[0] > 60:
                self.calls.popleft()
            if len(self.calls) >= self.per_minute:
                sleep_for = 60 - (now - self.calls[0]) + 0.05
                time.sleep(max(sleep_for, 0))
            self.calls.append(time.monotonic())


class SuperMarketClient:
    def __init__(self, api_key: str, base_url: str = SM_BASE_URL):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        self.reads = _RateLimiter(READS_PER_MIN)
        self.writes = _RateLimiter(WRITES_PER_MIN)

    # ---------- low level ----------
    def _request(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        limiter = self.reads if method == "GET" else self.writes
        for attempt in range(MAX_RETRIES):
            limiter.wait()
            try:
                resp = self.session.request(method, self.base_url + path, params=params, json=body, timeout=TIMEOUT_S)
            except requests.RequestException as exc:
                log.warning("%s %s network error (%s), retry %d", method, path, exc, attempt + 1)
                time.sleep(2 ** attempt)
                continue
            if resp.status_code < 400:
                return resp.json() if resp.content else {}
            code, message = _error_fields(resp)
            retryable = resp.status_code in (429, 502, 503) or code == "REQUEST_IN_FLIGHT"
            if not retryable or attempt == MAX_RETRIES - 1:
                raise ApiError(resp.status_code, code, message)
            wait_s = float(resp.headers.get("Retry-After") or 2 ** attempt)
            log.warning("%s %s -> %s %s, retrying in %.0fs", method, path, resp.status_code, code, wait_s)
            time.sleep(wait_s)
        raise ApiError(0, "RETRIES_EXHAUSTED", f"{method} {path}")

    def _paginate(self, path: str, params: dict, page_limit: int, key: str = "data") -> list[dict]:
        items, cursor = [], None
        while True:
            page_params = {**params, "limit": page_limit, **({"cursor": cursor} if cursor else {})}
            page = self._request("GET", path, params=page_params)
            items.extend(page.get(key, []))
            pagination = page.get("pagination") or {}
            cursor = pagination.get("nextCursor")
            if not pagination.get("hasMore") or not cursor:
                return items

    # ---------- reads ----------
    def account(self) -> dict:
        return self._request("GET", "/account")

    def tournament(self, slug: str) -> dict:
        return self._request("GET", f"/tournaments/{slug}")

    def tournament_markets(self, slug: str) -> list[dict]:
        return self._paginate(f"/tournaments/{slug}/markets", {"status": "open"}, MARKETS_PAGE_LIMIT)

    def prices(self, exchange_ids: list[str], tournament_id: str) -> dict[str, dict]:
        """exchangeId -> {bestBid, bestAsk, latestPrice, ...}; up to 100 ids per call."""
        out: dict[str, dict] = {}
        for i in range(0, len(exchange_ids), PRICE_IDS_PER_CALL):
            chunk = exchange_ids[i : i + PRICE_IDS_PER_CALL]
            page = self._request("GET", "/exchanges/prices", params={"ids": ",".join(chunk), "tournamentId": tournament_id})
            for row in page.get("data", []):
                out[str(row["exchangeId"])] = row
        return out

    def orderbook(self, exchange_id: str, tournament_id: str, depth: int = 20) -> dict:
        return self._request("GET", f"/exchanges/{exchange_id}/orderbook", params={"tournamentId": tournament_id, "depth": depth})

    def positions(self, slug: str) -> list[dict]:
        return self._request("GET", f"/tournaments/{slug}/portfolio/positions").get("positions", [])

    def open_orders(self, tournament_id: str) -> list[dict]:
        return self._paginate("/orders", {"status": "open", "tournamentId": tournament_id}, ORDERS_PAGE_LIMIT)

    def recent_fills(self, slug: str, limit: int = 100) -> list[dict]:
        """Newest fills first; price is the price of the side bought."""
        return self._request("GET", f"/tournaments/{slug}/portfolio/fills", params={"limit": limit}).get("data", [])

    def leaderboard(self, slug: str) -> dict:
        return self._request("GET", f"/tournaments/{slug}/leaderboard", params={"period": "all"})

    # ---------- writes ----------
    def place_order(self, exchange_id: str, side: str, action: str, quantity: int, price: float,
                    tournament_id: str, expiration_iso: str | None = None) -> dict:
        body = {
            "idempotencyKey": str(uuid.uuid4()),
            "exchangeId": str(exchange_id),
            "side": side,
            "action": action,
            "quantity": int(quantity),
            "price": round(price, 3),
            "tournamentId": tournament_id,
        }
        if expiration_iso:
            body["expirationDate"] = expiration_iso
        return self._request("POST", "/orders", body=body)

    def cancel_all(self, tournament_id: str, exchange_id: str | None = None) -> dict:
        body = {"tournamentId": tournament_id, **({"exchangeId": str(exchange_id)} if exchange_id else {})}
        return self._request("POST", "/orders/cancel-all", body=body)


def _error_fields(resp: requests.Response) -> tuple[str, str]:
    try:
        err = resp.json().get("error", {})
        return err.get("code", "UNKNOWN"), err.get("message", resp.text[:200])
    except ValueError:
        return "UNKNOWN", resp.text[:200]

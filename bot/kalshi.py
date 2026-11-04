"""Public (no-login) Kalshi quotes, used as the fair-value reference."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import KALSHI_BASE_URL

log = logging.getLogger(__name__)
CHUNK = 50
TIMEOUT_S = 15
RETRIES = 3
BACKOFF_S = 0.5          # waits 0.5s, 1s, 2s between retries
CHUNK_PAUSE_S = 0.3      # spread chunk requests out instead of bursting
CACHE_TTL_S = 180        # reuse a quote this long after a failed fetch


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_markets(payload: dict) -> dict[str, Quote]:
    quotes: dict[str, Quote] = {}
    for m in payload.get("markets", []):
        bid, ask = _to_float(m.get("yes_bid_dollars")), _to_float(m.get("yes_ask_dollars"))
        if bid is None or ask is None or ask <= 0 or ask < bid:
            continue
        quotes[m["ticker"]] = Quote(bid=bid, ask=ask)
    return quotes


def _make_session() -> requests.Session:
    """Session that retries dropped connections / 429 / 5xx with exponential backoff."""
    retry = Retry(total=RETRIES, connect=RETRIES, read=RETRIES, status=RETRIES,
                  backoff_factor=BACKOFF_S, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset({"GET"}), respect_retry_after_header=True)
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


_SESSION = _make_session()
_last_good: dict[str, tuple[Quote, float]] = {}


def _with_cache(fresh: dict[str, Quote], failed: set[str]) -> dict[str, Quote]:
    """Record fresh quotes; for tickers whose fetch failed, reuse a recent good quote."""
    now = time.monotonic()
    _last_good.update({t: (q, now) for t, q in fresh.items()})
    stale = {t: _last_good[t][0] for t in failed
             if t in _last_good and now - _last_good[t][1] <= CACHE_TTL_S}
    if stale:
        log.info("Using cached Kalshi quotes for %d tickers after fetch failure", len(stale))
    return {**stale, **fresh}


def fetch_quotes(tickers: list[str], session: requests.Session | None = None) -> dict[str, Quote]:
    """Returns quotes for the tickers that exist; unknown tickers are silently absent."""
    session = session or _SESSION
    quotes: dict[str, Quote] = {}
    failed: set[str] = set()
    unique = sorted(set(tickers))
    for i in range(0, len(unique), CHUNK):
        chunk = unique[i : i + CHUNK]
        if i:
            time.sleep(CHUNK_PAUSE_S)
        try:
            resp = session.get(
                f"{KALSHI_BASE_URL}/markets",
                params={"tickers": ",".join(chunk), "limit": len(chunk)},
                timeout=TIMEOUT_S,
            )
            resp.raise_for_status()
            quotes.update(parse_markets(resp.json()))
        except (requests.RequestException, ValueError) as exc:
            log.warning("Kalshi fetch failed for %d tickers: %s", len(chunk), exc)
            failed.update(chunk)
    return _with_cache(quotes, failed)

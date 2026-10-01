"""Public (no-login) Kalshi quotes, used as the fair-value reference."""
from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

from .config import KALSHI_BASE_URL

log = logging.getLogger(__name__)
CHUNK = 50
TIMEOUT_S = 15


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


def fetch_quotes(tickers: list[str], session: requests.Session | None = None) -> dict[str, Quote]:
    """Returns quotes for the tickers that exist; unknown tickers are silently absent."""
    session = session or requests.Session()
    quotes: dict[str, Quote] = {}
    unique = sorted(set(tickers))
    for i in range(0, len(unique), CHUNK):
        chunk = unique[i : i + CHUNK]
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
    return quotes

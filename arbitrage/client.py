from __future__ import annotations

import json
import ssl
from functools import lru_cache
from threading import Lock
from time import monotonic, sleep
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
POLYMARKET_US_GATEWAY_URL = "https://gateway.polymarket.us"
NOVIG_BASE_URL = "https://api.novig.com"
KALSHI_MIN_REQUEST_INTERVAL_SECONDS = 0.05
HTTP_RETRY_ATTEMPTS = 4
_kalshi_rate_lock = Lock()
_next_kalshi_request_at = 0.0


def _pace_kalshi_request(url: str) -> None:
    """Serialize public Kalshi reads into a sustainable request rate."""
    if not url.startswith(KALSHI_BASE_URL):
        return
    global _next_kalshi_request_at
    with _kalshi_rate_lock:
        now = monotonic()
        scheduled_at = max(now, _next_kalshi_request_at)
        _next_kalshi_request_at = scheduled_at + KALSHI_MIN_REQUEST_INTERVAL_SECONDS
    if scheduled_at > now:
        sleep(scheduled_at - now)


def _retry_delay(error: HTTPError, attempt: int) -> float:
    """Honor a server retry hint when available, otherwise back off gently."""
    try:
        return max(0.1, float(error.headers.get("Retry-After", "")))
    except (AttributeError, TypeError, ValueError):
        return 0.6 * (2 ** attempt)


def _get_json(url: str) -> Any:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "cross-venue-arbitrage-scanner/0.1"})
    import certifi
    context = ssl.create_default_context(cafile=certifi.where())
    for attempt in range(HTTP_RETRY_ATTEMPTS):
        _pace_kalshi_request(url)
        try:
            with urlopen(request, timeout=20, context=context) as response:
                return json.load(response)
        except HTTPError as error:
            # Public feeds can temporarily throttle a refresh burst. Retry
            # only rate/server errors; invalid requests should surface at once.
            retryable = error.code == 429 or 500 <= error.code < 600
            if not retryable or attempt == HTTP_RETRY_ATTEMPTS - 1:
                raise
            sleep(_retry_delay(error, attempt))
    raise RuntimeError("unreachable HTTP retry state")


def kalshi_market(ticker: str) -> dict[str, Any]:
    return _get_json(f"{KALSHI_BASE_URL}/markets/{ticker}")["market"]


def kalshi_event(event_ticker: str) -> dict[str, Any]:
    payload = _get_json(f"{KALSHI_BASE_URL}/events/{event_ticker}")
    event = payload["event"]
    # Kalshi returns the parent event and its contracts as sibling fields.
    return {**event, "markets": payload.get("markets", [])}


@lru_cache(maxsize=256)
def kalshi_series(series_ticker: str) -> dict[str, Any]:
    return _get_json(f"{KALSHI_BASE_URL}/series/{series_ticker}")["series"]


def kalshi_open_events(max_events: int = 500) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    cursor: str | None = None
    while len(events) < max_events:
        query = {"status": "open", "limit": min(200, max_events - len(events))}
        if cursor:
            query["cursor"] = cursor
        page = _get_json(f"{KALSHI_BASE_URL}/events?{urlencode(query)}")
        batch = page.get("events", [])
        events.extend(batch)
        cursor = page.get("cursor")
        if not cursor or not batch:
            break
    return events[:max_events]


def kalshi_open_events_for_series(series_ticker: str, max_events: int = 1000) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    cursor: str | None = None
    while len(events) < max_events:
        query = {
            "series_ticker": series_ticker, "status": "open",
            "limit": min(200, max_events - len(events)),
        }
        if cursor:
            query["cursor"] = cursor
        page = _get_json(f"{KALSHI_BASE_URL}/events?{urlencode(query)}")
        batch = page.get("events", [])
        events.extend(batch)
        cursor = page.get("cursor")
        if not cursor or not batch:
            break
    return events[:max_events]


def polymarket_us_open_events(max_events: int = 500) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    while len(events) < max_events:
        query = {
            "active": "true", "closed": "false", "archived": "false",
            "limit": min(200, max_events - len(events)), "offset": len(events),
        }
        page = _get_json(f"{POLYMARKET_US_GATEWAY_URL}/v1/events?{urlencode(query)}")
        batch = page.get("events", [])
        events.extend(batch)
        if not batch:
            break
    return events[:max_events]


def polymarket_us_league_events(league: str, max_events: int = 500) -> list[dict[str, Any]]:
    """Retrieve all available pages from a Polymarket US league feed."""
    events: list[dict[str, Any]] = []
    seen: set[str] = set()
    offset = 0
    while len(events) < max_events:
        limit = min(100, max_events - len(events))
        payload = _get_json(
            f"{POLYMARKET_US_GATEWAY_URL}/v2/leagues/{league}/events?"
            f"{urlencode({'limit': limit, 'offset': offset})}"
        )
        batch = payload.get("events", [])
        for event in batch:
            identifier = str(event.get("id") or event.get("slug") or offset)
            if identifier not in seen:
                seen.add(identifier)
                events.append(event)
        offset += len(batch)
        if len(batch) < limit or not batch:
            break
    return events[:max_events]


def polymarket_us_event(slug: str) -> dict[str, Any]:
    return _get_json(f"{POLYMARKET_US_GATEWAY_URL}/v1/events/slug/{slug}")["event"]


def novig_public_events(league: str, max_events: int = 500) -> list[dict[str, Any]]:
    """Retrieve open Novig event catalog pages; no credentials are required."""
    events: list[dict[str, Any]] = []
    after: str | None = None
    while len(events) < max_events:
        query = {"league": league, "limit": min(5000, max_events - len(events))}
        if after:
            query["after"] = after
        page = _get_json(f"{NOVIG_BASE_URL}/v3/public/catalog/events?{urlencode(query)}")
        batch = page.get("items", [])
        events.extend(batch)
        after = page.get("next")
        if not after or not batch:
            break
    return events[:max_events]


def novig_public_moneyline_markets(league: str, max_markets: int = 5000) -> list[dict[str, Any]]:
    """Retrieve Novig full-game two-way moneyline markets for one league."""
    markets: list[dict[str, Any]] = []
    after: str | None = None
    while len(markets) < max_markets:
        query = {"league": league, "marketType": "MONEY", "limit": min(5000, max_markets - len(markets))}
        if after:
            query["after"] = after
        page = _get_json(f"{NOVIG_BASE_URL}/v3/public/catalog/markets?{urlencode(query)}")
        batch = page.get("items", [])
        markets.extend(batch)
        after = page.get("next")
        if not after or not batch:
            break
    return markets[:max_markets]


def novig_public_book(market_id: str) -> dict[str, Any]:
    """Retrieve the public bid ladder for a Novig market."""
    return _get_json(f"{NOVIG_BASE_URL}/v3/public/catalog/markets/{market_id}/book")

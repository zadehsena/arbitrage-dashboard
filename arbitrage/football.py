from __future__ import annotations

import argparse
import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Callable

from .client import kalshi_event, kalshi_open_events_for_series, kalshi_series, polymarket_us_event, polymarket_us_league_events
from .discover import similarity


def amount(value: object | None) -> str | None:
    if value is None:
        return None
    return f"{Decimal(str(value)):.4f}"


def url_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


KALSHI_SERIES_BY_LEAGUE = {"cfb": "KXNCAAFGAME", "nfl": "KXNFLGAME"}

# Kalshi groups a game's moneyline, spread, and total into separate event
# series. The dashboard originally queried only KXMLBGAME, so it could never
# display the contracts visible under a KXMLBSPREAD market URL.
KALSHI_COMPANION_SERIES = {
    "KXMLBGAME": ("KXMLBSPREAD", "KXMLBTOTAL"),
    # College football uses the same game suffix across its separate winner,
    # spread, and total event series. Fetching only KXNCAAFGAME left the
    # detail page's Kalshi spread/total sections empty even though those
    # contracts were listed on Kalshi.
    "KXNCAAFGAME": ("KXNCAAFSPREAD", "KXNCAAFTOTAL"),
}
MARKET_BREAKDOWN_VERSION = 4
VENUES = ("kalshi", "polymarket_us", "novig", "prophetx")


def moneyline_markets(event: dict) -> list[dict]:
    markets = [market for market in event.get("markets", [])
               if market.get("sportsMarketType") in {"moneyline", "soccer_team_full_time_winner"}]
    return markets or [market for market in event.get("markets", [])
                       if "who will win in the upcoming" in market.get("question", "").lower()]


def market_category(market: dict) -> str:
    """Normalize venue-specific sports market labels for the detail view."""
    value = " ".join(str(market.get(field, "")) for field in ("sportsMarketType", "title", "question")).lower()
    # Prefer the wording of the market question over a venue's broad internal
    # type: Polymarket US can label a spread as a moneyline instrument.
    if "cover" in value or "spread" in value or "handicap" in value or "wins by" in value:
        return "spread"
    if "total bases" in value or "record at least" in value:
        return "props"
    if "total" in value or "over/under" in value or "over under" in value:
        return "total"
    if ("moneyline" in value or "full_time_winner" in value or "will win" in value
            or re.search(r"\bwins?\b", value)):
        return "moneyline"
    return "props"


def companion_event_tickers(event_ticker: str, series_ticker: str) -> list[str]:
    """Return companion spread/total event tickers for a known game series."""
    suffix = event_ticker.removeprefix(series_ticker)
    if suffix == event_ticker:
        return []
    return [series + suffix for series in KALSHI_COMPANION_SERIES.get(series_ticker, ())]


def market_period(label: object) -> str:
    text = str(label or "").lower()
    if "first 5" in text:
        return "first_5_innings"
    if "inning" in text:
        return "inning"
    if "half" in text or "quarter" in text or "period" in text:
        return "partial_game"
    return "full_game"


def market_catalog(kalshi_markets: list[dict], polymarket_markets: list[dict]) -> list[dict]:
    """Adapt venue payloads to one extensible, review-first market schema."""
    catalog = []
    for venue, markets in (("kalshi", kalshi_markets), ("polymarket_us", polymarket_markets)):
        for market in markets:
            label = market.get("market") or market.get("contract") or "Market"
            quote = ({"yes_ask": market.get("yes_ask"), "no_ask": market.get("no_ask")}
                     if venue == "kalshi" else {"displayed_quote": market.get("displayed_quote")})
            catalog.append({
                "market_type": market.get("category", "props"),
                "period": market_period(label),
                "label": label,
                "selection": market.get("outcome"),
                "match_status": "review",
                "quotes": {**{name: None for name in VENUES}, venue: quote},
            })
    return catalog


def match_events(kalshi_events: list[dict], poly_events: list[dict], minimum_score: float,
                 title_normalizer: Callable[[str], str] | None = None) -> list[tuple[dict, dict, float]]:
    matches: list[tuple[dict, dict, float]] = []
    normalizer = title_normalizer or (lambda title: title)
    remaining = list(kalshi_events)
    for poly in poly_events:
        scored = [(similarity(normalizer(poly.get("title", "")), normalizer(kalshi.get("title", ""))), kalshi)
                  for kalshi in remaining]
        score, kalshi = max(scored, default=(0.0, None), key=lambda item: item[0])
        if kalshi is not None and score >= minimum_score:
            matches.append((kalshi, poly, score))
            remaining.remove(kalshi)
    return sorted(matches, key=lambda item: item[1].get("startDate", ""))


def report_record(kalshi_summary: dict, poly: dict, score: float) -> dict:
    kalshi = kalshi_event(kalshi_summary["event_ticker"])
    series = kalshi_series(kalshi["series_ticker"])
    kalshi_markets = list(kalshi.get("markets", []))
    for ticker in companion_event_tickers(kalshi["event_ticker"], kalshi["series_ticker"]):
        try:
            kalshi_markets.extend(kalshi_event(ticker).get("markets", []))
        except Exception:
            # A companion event is not guaranteed to be listed; preserve the
            # available moneyline data rather than failing the whole matchup.
            continue
    # League feeds can omit teams. The public event endpoint provides the
    # venue-supplied team names and logos used by the dashboard.
    poly_detail = polymarket_us_event(poly["slug"])
    kalshi_odds = [{"contract": market.get("title"), "ticker": market.get("ticker"),
                    "yes_ask": amount(market.get("yes_ask_dollars")),
                    "no_ask": amount(market.get("no_ask_dollars"))}
                   for market in kalshi_markets]
    kalshi_expected_expiration = min(
        (str(market["expected_expiration_time"]) for market in kalshi.get("markets", [])
         if market.get("expected_expiration_time")),
        default=None,
    )
    poly_odds = []
    for market in moneyline_markets(poly):
        sides = market.get("marketSides", [])
        if market.get("sportsMarketType") == "soccer_team_full_time_winner":
            # Soccer uses one YES/NO instrument per home team, away team, and
            # draw. Only the YES side represents the named match outcome.
            sides = [side for side in sides if side.get("description") == "Yes"]
        poly_odds.extend({"outcome": (side.get("team", {}).get("safeName")
                                      or side.get("team", {}).get("name")
                                      or ("Draw" if "draw" in market.get("question", "").lower()
                                          else side.get("description"))),
                          "nickname": side.get("team", {}).get("alias"),
                          "displayed_quote": amount(side.get("quote", {}).get("value")),
                          "market_slug": market.get("slug")}
                         for side in sides)
    kalshi_breakdown = [{
        "category": market_category(market),
        "market": market.get("title") or market.get("subtitle") or market.get("ticker"),
        "yes_ask": amount(market.get("yes_ask_dollars")),
        "no_ask": amount(market.get("no_ask_dollars")),
    } for market in kalshi_markets]
    polymarket_breakdown = []
    for market in poly_detail.get("markets") or poly.get("markets", []):
        for side in market.get("marketSides", []):
            outcome = (side.get("team", {}).get("safeName") or side.get("team", {}).get("name")
                       or side.get("description") or "Outcome")
            polymarket_breakdown.append({
                "category": market_category(market),
                "market": market.get("question") or market.get("title") or market.get("slug"),
                "outcome": outcome,
                "displayed_quote": amount(side.get("quote", {}).get("value")),
            })
    normalized_catalog = market_catalog(kalshi_breakdown, polymarket_breakdown)
    return {
        "similarity": score,
        "kalshi_event_ticker": kalshi["event_ticker"],
        "kalshi_title": kalshi["title"],
        "kalshi_url": "https://kalshi.com/markets/"
                      f"{kalshi['series_ticker'].lower()}/{url_slug(series['title'])}/"
                      f"{kalshi['event_ticker'].lower()}",
        "polymarket_us_event_slug": poly["slug"],
        "polymarket_us_title": poly["title"],
        "polymarket_us_url": "https://polymarket.us/sports/"
                            f"{poly_detail.get('primaryTag', {}).get('slug') or poly.get('primaryTag', {}).get('slug', '')}/"
                            f"{poly['slug']}",
        "start_time": poly.get("startDate"),
        "kalshi_expected_expiration_time": kalshi_expected_expiration,
        "kalshi_moneyline_asks": kalshi_odds,
        "polymarket_us_displayed_moneyline_quotes": poly_odds,
        "kalshi_market_breakdown": kalshi_breakdown,
        "polymarket_us_market_breakdown": polymarket_breakdown,
        "market_catalog": normalized_catalog,
        "market_breakdown_version": MARKET_BREAKDOWN_VERSION,
        "teams": [{
            "name": team.get("safeName") or team.get("name"),
            "nickname": team.get("alias"),
            "record": team.get("record"),
            "logo": team.get("shortIcon") or team.get("awayIcon") or team.get("logo"),
        } for team in poly_detail.get("teams", []) if team.get("safeName") or team.get("name")],
        "warning": "Kalshi values are top-of-book asks. Polymarket US values are displayed quotes, not verified executable asks.",
    }


def build_report(leagues: list[str], max_kalshi_events: int = 2000,
                 minimum_score: float = 0.72) -> tuple[list[dict], int, int]:
    """Fetch and match the supported football leagues without placing orders."""
    unknown_leagues = set(leagues) - KALSHI_SERIES_BY_LEAGUE.keys()
    if unknown_leagues:
        raise ValueError(f"no Kalshi series mapping yet for: {', '.join(sorted(unknown_leagues))}")
    kalshi = [event for league in leagues
              for event in kalshi_open_events_for_series(
                  KALSHI_SERIES_BY_LEAGUE[league], max_kalshi_events)]
    polymarket = [event for league in leagues for event in polymarket_us_league_events(league)
                  if event.get("active") and not event.get("closed")]
    matches = match_events(kalshi, polymarket, minimum_score)
    return ([report_record(kalshi_event, poly_event, score)
             for kalshi_event, poly_event, score in matches], len(kalshi), len(polymarket))


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Kalshi / Polymarket US football match and odds report")
    parser.add_argument("--leagues", default="cfb,nfl", help="Polymarket US leagues, comma-separated (default: cfb,nfl)")
    parser.add_argument("--max-kalshi-events", type=int, default=2000)
    parser.add_argument("--min-score", type=float, default=0.72)
    parser.add_argument("--json", type=Path, default=Path("reports/football_matches.json"))
    args = parser.parse_args()
    leagues = [league.strip() for league in args.leagues.split(",") if league.strip()]
    if not leagues or args.max_kalshi_events < 1 or not 0 <= args.min_score <= 1:
        parser.error("provide leagues, positive --max-kalshi-events, and --min-score from 0 to 1")

    try:
        records, kalshi_count, polymarket_count = build_report(
            leagues, args.max_kalshi_events, args.min_score)
    except ValueError as error:
        parser.error(str(error))
    print(f"Compared {kalshi_count} Kalshi football events with {polymarket_count} Polymarket US football events.")
    print(f"Found {len(records)} probable event matches.\n")
    for record in records:
        print(f"{record['kalshi_title']} | similarity {record['similarity']:.3f} | {record['start_time']}")
        print("  Kalshi asks: " + "; ".join(
            f"{item['contract']} YES {item['yes_ask'] or '—'}" for item in record["kalshi_moneyline_asks"]))
        print("  Polymarket US displayed quotes: " + "; ".join(
            f"{item['outcome']} {item['displayed_quote'] or '—'}" for item in record["polymarket_us_displayed_moneyline_quotes"]))
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(records, indent=2) + "\n")
    print(f"\nWrote {len(records)} matches to {args.json}")


if __name__ == "__main__":
    main()

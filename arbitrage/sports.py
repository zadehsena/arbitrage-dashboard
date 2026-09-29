"""Validated sport/league mappings and read-only cross-venue reports."""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Callable

from .client import (
    kalshi_open_events_for_series,
    novig_public_book,
    novig_public_events,
    novig_public_moneyline_markets,
    polymarket_us_league_events,
)
from .discover import similarity
from .football import match_events, report_record


# These pairs were checked against the public venue catalogs. A mapping is only
# a discovery aid; individual event rules and outcome definitions still require review.
SPORT_LEAGUE_MAPPINGS: dict[str, tuple[tuple[str, str], ...]] = {
    "football": (("cfb", "KXNCAAFGAME"), ("nfl", "KXNFLGAME")),
    "soccer": (("epl", "KXEPLGAME"), ("mls", "KXMLSGAME"), ("ucl", "KXUCLGAME")),
    "hockey": (("nhl", "KXNHLGAME"),),
    "basketball": (("nba", "KXNBAGAME"), ("wnba", "KXWNBAGAME"), ("cbb", "KXNCAABGAME")),
    "baseball": (("mlb", "KXMLBGAME"),),
    "mma": (("ufc", "KXUFCFIGHT"),),
    "esports": (
        ("cs2", "KXCS2GAME"), ("lol", "KXLOLGAME"),
        ("valorant", "KXVALORANTGAME"), ("dota2", "KXDOTA2GAME"),
        ("r6", "KXR6GAME"),
    ),
    "tennis": (
        ("atp", "KXATPMATCH"), ("atp", "KXATPCHALLENGERMATCH"),
        ("wta", "KXWTAMATCH"), ("wta", "KXWTACHALLENGERMATCH"),
    ),
}
SPORT_MATCHING_VERSION = 3
# Each record can require Kalshi's game, spread, and total event endpoints.
# Keep this deliberately small so a report refresh does not burst past the
# public API limit before the client-side pacing/retry logic can respond.
DETAIL_FETCH_WORKERS = 2
NOVIG_BOOK_FETCH_WORKERS = 6
NOVIG_LEAGUE_BY_POLYMARKET_LEAGUE = {
    "cfb": "NCAAF", "nfl": "NFL", "epl": "EPL", "mls": "MLS", "ucl": "UEFA_CHAMPIONS_LEAGUE",
    "nhl": "NHL", "nba": "NBA", "wnba": "WNBA", "cbb": "NCAAB", "mlb": "MLB",
    "ufc": "UFC", "atp": "ATP", "wta": "WTA",
}

# Kalshi shortens many MLB club names in event titles. Expand only complete
# known names, keeping Chicago and New York clubs distinct before title scoring.
MLB_TEAM_ALIASES = {
    "Arizona": "Arizona Diamondbacks", "Atlanta": "Atlanta Braves",
    "Baltimore": "Baltimore Orioles", "Boston": "Boston Red Sox",
    "Chicago C": "Chicago Cubs", "Chicago WS": "Chicago White Sox",
    "Cincinnati": "Cincinnati Reds", "Cleveland": "Cleveland Guardians",
    "Colorado": "Colorado Rockies", "Detroit": "Detroit Tigers",
    "Houston": "Houston Astros", "Kansas City": "Kansas City Royals",
    "LA Angels": "Los Angeles Angels", "LA Dodgers": "Los Angeles Dodgers",
    "Miami": "Miami Marlins", "Milwaukee": "Milwaukee Brewers",
    "Minnesota": "Minnesota Twins", "New York M": "New York Mets",
    "New York Y": "New York Yankees", "Oakland": "Athletics",
    "Philadelphia": "Philadelphia Phillies", "Pittsburgh": "Pittsburgh Pirates",
    "San Diego": "San Diego Padres", "San Francisco": "San Francisco Giants",
    "Seattle": "Seattle Mariners", "St Louis": "St. Louis Cardinals",
    "Tampa Bay": "Tampa Bay Rays", "Texas": "Texas Rangers",
    "Toronto": "Toronto Blue Jays", "Washington": "Washington Nationals",
}

NFL_CITY_ALIASES = {
    "ARI": "Arizona", "ATL": "Atlanta", "BAL": "Baltimore", "BUF": "Buffalo",
    "CAR": "Carolina", "CHI": "Chicago", "CIN": "Cincinnati", "CLE": "Cleveland",
    "DAL": "Dallas", "DEN": "Denver", "DET": "Detroit", "GB": "Green Bay",
    "HOU": "Houston", "IND": "Indianapolis", "JAX": "Jacksonville", "KC": "Kansas City",
    "LAC": "Los Angeles Chargers", "LAR": "Los Angeles Rams", "LV": "Las Vegas",
    "MIA": "Miami", "MIN": "Minnesota", "NE": "New England", "NO": "New Orleans",
    "NY": "New York", "PHI": "Philadelphia", "PIT": "Pittsburgh", "SEA": "Seattle",
    "SF": "San Francisco", "TB": "Tampa Bay", "TEN": "Tennessee", "WAS": "Washington",
}

# Public league feeds can retain an event as active after it has finished.
# These windows comfortably include an in-progress event, then remove a stale
# quote once a game could no longer plausibly be live.
LIVE_WINDOWS = {
    "football": timedelta(hours=5),
    "soccer": timedelta(hours=3),
    "hockey": timedelta(hours=4),
    "basketball": timedelta(hours=4),
    "baseball": timedelta(hours=6),
    "mma": timedelta(hours=6),
    "esports": timedelta(hours=6),
    "tennis": timedelta(hours=8),
}


def normalize_mlb_title(title: str) -> str:
    """Expand Kalshi's MLB title abbreviations for matching only."""
    normalized = title
    for short_name, full_name in sorted(MLB_TEAM_ALIASES.items(), key=lambda pair: len(pair[0]), reverse=True):
        suffix = full_name[len(short_name):]

        def expand(match: re.Match[str]) -> str:
            # Do not turn an already complete name such as "Boston Red Sox"
            # into "Boston Red Sox Red Sox" when normalizing Polymarket.
            if suffix and match.string[match.end():].lower().startswith(suffix.lower()):
                return match.group(0)
            return full_name

        normalized = re.sub(rf"(?<![A-Za-z]){re.escape(short_name)}(?![A-Za-z])", expand, normalized,
                            flags=re.IGNORECASE)
    return normalized


def normalize_cfb_title(title: str) -> str:
    """Align common college-football team abbreviations before title scoring."""
    normalized = re.sub(r"\bUAlbany\b", "University at Albany", title, flags=re.IGNORECASE)
    # Kalshi commonly abbreviates the university suffix (for example, "NC
    # St."), while Polymarket spells out "State". This only affects matching;
    # venue titles remain unchanged in the dashboard.
    normalized = re.sub(r"\bSt\.?(?=\s|$)", "State", normalized, flags=re.IGNORECASE)
    # "Southern" is the conventional short form for Southern University in
    # the CFB feeds. Avoid duplicating the full venue label when it is present.
    return re.sub(r"\bSouthern\b(?!\s+University\b)", "Southern University", normalized,
                  flags=re.IGNORECASE)


def normalize_nfl_title(title: str) -> str:
    """Expand feed-style NFL city abbreviations for cross-venue matching."""
    normalized = title
    for abbreviation, name in NFL_CITY_ALIASES.items():
        normalized = re.sub(rf"\b{abbreviation}\b", name, normalized, flags=re.IGNORECASE)
    return normalized


def supported_sports() -> tuple[str, ...]:
    return tuple(SPORT_LEAGUE_MAPPINGS)


def supported_leagues(sport: str) -> tuple[str, ...]:
    """Return the public league identifiers available for one sport."""
    try:
        return tuple(dict.fromkeys(league for league, _ in SPORT_LEAGUE_MAPPINGS[sport]))
    except KeyError as error:
        raise ValueError(f"unsupported sport: {sport}") from error


def is_current_sport_record(record: dict, sport: str, now: datetime | None = None) -> bool:
    """Keep upcoming and plausibly live events; drop completed stale records."""
    current_time = now or datetime.now(UTC)
    # Kalshi provides an event-specific expected expiration that is a better
    # completed-game cutoff than a generic sport-duration estimate.
    expected_expiration = record.get("kalshi_expected_expiration_time")
    if expected_expiration:
        try:
            expected = datetime.fromisoformat(str(expected_expiration).replace("Z", "+00:00"))
            if expected.tzinfo is None:
                expected = expected.replace(tzinfo=UTC)
            return expected > current_time
        except ValueError:
            pass
    start_time = record.get("start_time")
    if not start_time:
        return True
    try:
        start = datetime.fromisoformat(str(start_time).replace("Z", "+00:00"))
    except ValueError:
        return True
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    # Older CFB cache records may not yet contain Kalshi's expected expiration.
    # Three hours mirrors that venue-provided cutoff without forcing a full
    # report rebuild whenever the College Football tab opens.
    live_window = timedelta(hours=3) if sport == "football" and record.get("league") == "cfb" else LIVE_WINDOWS[sport]
    return start + live_window > current_time


def _novig_teams(description: object) -> list[str]:
    """Return Novig's away/home names from its public event description."""
    parts = re.split(r"\s+@\s+", str(description or ""), maxsplit=1)
    return [part.strip() for part in parts] if len(parts) == 2 and all(parts) else []


def _novig_quotes(event: dict, market: dict, book: dict) -> list[dict]:
    """Turn Novig's bid-only binary book into executable outcome asks.

    Buying one outcome is equivalent to selling its complement, so the best
    ask is 1 minus the complement outcome's highest bid. Leave it empty when
    the complementary ladder has no liquidity.
    """
    outcomes = list(market.get("outcomes", []))
    teams = _novig_teams(event.get("description"))
    quotes = []
    for index, outcome in enumerate(outcomes):
        complement = next((item for item in outcomes if item.get("outcomeId") != outcome.get("outcomeId")), None)
        orders = list(book.get("orders", {}).get((complement or {}).get("outcomeId"), []))
        try:
            ask = 1 - max(float(order["price"]) for order in orders)
            displayed_ask = f"{ask:.4f}"
        except (KeyError, TypeError, ValueError):
            displayed_ask = None
        # Novig's MONEY outcome order is home then away, while event text is
        # away @ home. Use the venue's full team name for safe cross-book
        # alignment, falling back to the outcome code if it is unavailable.
        label = teams[1 - index] if len(teams) == 2 and index < 2 else outcome.get("name")
        quotes.append({"outcome": label, "displayed_ask": displayed_ask,
                       "market_id": market.get("marketId")})
    return quotes


def _add_novig_moneylines(records: list[dict], polymarket_league: str,
                          normalizer: Callable[[str], str] | None) -> list[dict]:
    """Attach review-only Novig moneyline asks to already validated game rows."""
    novig_league = NOVIG_LEAGUE_BY_POLYMARKET_LEAGUE.get(polymarket_league)
    if not records or not novig_league:
        return records
    try:
        events = novig_public_events(novig_league)
        markets_by_event = {market.get("eventId"): market for market in novig_public_moneyline_markets(novig_league)
                            if market.get("status") == "OPEN"}
    except Exception:
        # Novig availability must not prevent the existing two-venue report
        # from rendering; missing quotes stay explicitly unavailable.
        return records
    normalize = normalize_nfl_title if polymarket_league == "nfl" else (normalizer or (lambda value: value))
    event_by_record = {}
    remaining = list(events)
    for record in records:
        title = normalize(str(record.get("polymarket_us_title", "")))
        score, event = max(((similarity(title, normalize(str(item.get("description", "")))), item)
                            for item in remaining), default=(0.0, None), key=lambda item: item[0])
        if event is not None and score >= 0.72 and event.get("eventId") in markets_by_event:
            event_by_record[id(record)] = event
            remaining.remove(event)
    selected = [(record, event_by_record[id(record)], markets_by_event[event_by_record[id(record)].get("eventId")])
                for record in records if id(record) in event_by_record]
    with ThreadPoolExecutor(max_workers=NOVIG_BOOK_FETCH_WORKERS) as executor:
        books = list(executor.map(lambda item: novig_public_book(str(item[2]["marketId"])), selected))
    for (record, event, market), book in zip(selected, books):
        record["novig_event_id"] = event.get("eventId")
        quotes = _novig_quotes(event, market, book)
        # Preserve the dashboard's established team labels. Novig often uses
        # a full city name while the other feeds use an abbreviation (PIT vs
        # Pittsburgh); mapping the matched quote back to the record team keeps
        # every venue in the same visible outcome slot.
        record_teams = [str(team.get("name")) for team in record.get("teams", []) if team.get("name")]
        for quote in quotes:
            score, team = max(((similarity(str(quote.get("outcome", "")), candidate), candidate)
                               for candidate in record_teams), default=(0.0, None), key=lambda item: item[0])
            if team is not None and score >= 0.25:
                quote["outcome"] = team
        record["novig_moneyline_quotes"] = quotes
    return records


def build_sport_report(sport: str, leagues: tuple[str, ...] | None = None,
                       max_kalshi_events: int = 500,
                       minimum_score: float = 0.72) -> tuple[list[dict], int, int]:
    """Return likely matched open game events for one dashboard sport.

    Requests public market data only. It never authenticates or places orders.
    """
    try:
        mappings = SPORT_LEAGUE_MAPPINGS[sport]
    except KeyError as error:
        raise ValueError(f"no cross-venue mapping configured for sport: {sport}") from error
    if max_kalshi_events < 1 or not 0 <= minimum_score <= 1:
        raise ValueError("max_kalshi_events must be positive and minimum_score must be from 0 to 1")

    if leagues:
        available_leagues = {league for league, _ in mappings}
        unknown_leagues = set(leagues) - available_leagues
        if unknown_leagues:
            raise ValueError(
                f"no {sport} mapping configured for: {', '.join(sorted(unknown_leagues))}")
        mappings = tuple(mapping for mapping in mappings if mapping[0] in leagues)

    normalizer = {
        "baseball": normalize_mlb_title,
        "football": normalize_cfb_title if leagues == ("cfb",) else None,
    }.get(sport)
    # Tennis feeds often omit players' given names on Kalshi ("Halys") while
    # Polymarket US includes them ("Quentin Halys"). A lower candidate score
    # still requires both opponent names to align and remains review-only.
    # Kalshi commonly uses fighter surnames while Polymarket US shows full
    # names (for example, "Fight Night: Jackson vs Simon" versus "Montel
    # Jackson vs. Ricky Simon"). Requiring both surnames still leaves these
    # matches in review-only territory while avoiding a zero-result UFC page.
    effective_minimum_score = min(minimum_score, 0.45) if sport == "mma" else (
        min(minimum_score, 0.50) if sport == "tennis" else minimum_score
    )
    # Match each league independently. This prevents a selected category such
    # as College Football from ever receiving records from the NFL feed.
    records = []
    kalshi_count = 0
    polymarket_count = 0
    for polymarket_league in dict.fromkeys(league for league, _ in mappings):
        kalshi_events = [event for league, series in mappings if league == polymarket_league
                         for event in kalshi_open_events_for_series(series, max_kalshi_events)]
        polymarket_events = [
            event for event in polymarket_us_league_events(polymarket_league, max_kalshi_events)
            if event.get("active") and not event.get("closed")
        ]
        # Tennis has multiple Kalshi match series for one ATP/WTA feed. Retain
        # each Polymarket US event once before attempting title matching.
        polymarket_events = list({
            event.get("id") or event.get("slug"): event for event in polymarket_events
        }.values())
        matches = [match for match in match_events(
            kalshi_events, polymarket_events, effective_minimum_score, normalizer
        ) if is_current_sport_record({"start_time": match[1].get("startDate")}, sport)]

        # Each matched event needs venue-specific detail requests for its
        # market breakdown, URLs, and team metadata. Fetch a small bounded
        # batch concurrently instead of waiting for every pair in sequence.
        with ThreadPoolExecutor(max_workers=DETAIL_FETCH_WORKERS) as executor:
            details = executor.map(lambda match: report_record(*match), matches)
            league_records = [
                {**record, "league": polymarket_league, "matching_version": SPORT_MATCHING_VERSION}
                for record in details
            ]
        records.extend(_add_novig_moneylines(league_records, polymarket_league, normalizer))
        kalshi_count += len(kalshi_events)
        polymarket_count += len(polymarket_events)
    return records, kalshi_count, polymarket_count

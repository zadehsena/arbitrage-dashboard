"""Local, read-only web dashboard for cross-venue market research."""
from __future__ import annotations

import argparse
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .accounts import (
    kalshi_balance,
    load_dotenv,
    novig_balance,
    novig_positions,
    polymarket_us_balances,
)
from .football import MARKET_BREAKDOWN_VERSION
from .sports import (
    build_sport_report,
    is_current_sport_record,
    supported_leagues,
    supported_sports,
)


ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = ROOT / "web"
REPORTS_DIR = ROOT / "reports"
ACCOUNT_SUMMARY_TIMEOUT_SECONDS = 5
SPORT_PAGE_SIZE = 25


def _report_metadata_path(report_path: Path) -> Path:
    """Return the small sidecar file containing a cached report's source totals."""
    return report_path.with_name(f"{report_path.stem}_metadata.json")


def _write_sport_report_cache(report_path: Path, records: list[dict],
                              kalshi_count: int, polymarket_count: int) -> None:
    """Cache report rows and the upstream event totals used to produce them."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(records, indent=2) + "\n")
    _report_metadata_path(report_path).write_text(json.dumps({
        "kalshi_events_compared": kalshi_count,
        "polymarket_events_compared": polymarket_count,
    }, indent=2) + "\n")


def _cached_source_counts(report_path: Path) -> tuple[int | None, int | None]:
    """Read upstream event totals without treating matched rows as source rows."""
    try:
        metadata = json.loads(_report_metadata_path(report_path).read_text())
        return metadata.get("kalshi_events_compared"), metadata.get("polymarket_events_compared")
    except (OSError, json.JSONDecodeError):
        return None, None


def _normalized_outcome_label(value: object) -> str:
    """Normalize a team/outcome label without relying on API item order."""
    text = str(value or "").lower()
    # Kalshi labels often read "Team wins" while the other venue provides
    # simply "Team". Remove the surrounding contract wording, then compare
    # the meaningful team names.
    text = re.sub(r"\b(will|the|win|wins|match|game)\b", " ", text)
    return re.sub(r"[^a-z0-9]+", "", text)


def _is_draw_outcome(value: object) -> bool:
    """Return whether a venue's outcome label represents a drawn game."""
    return bool(re.search(r"\b(draw|tie)\b", str(value or ""), re.IGNORECASE))


def _ordered_moneyline_quotes(record: dict, items_key: str, label_key: str,
                              quote_key: str, include_draw: bool = False) -> list[object | None]:
    """Return venue quotes in the displayed team order.

    Event feeds need not list the two winner contracts in the same order. A
    positional comparison could therefore pair a team's price with the other
    venue's opponent price and fabricate an arbitrage signal. Missing or
    unrecognizable labels deliberately remain unavailable rather than being
    guessed from their position.
    """
    items = list(record.get(items_key, []))
    teams = [team.get("name") for team in record.get("teams", []) if team.get("name")]
    if len(teams) < 2:
        # Legacy cached reports lack team metadata. Preserve their display,
        # but do not use this fallback for reports that can be aligned.
        return [item.get(quote_key) for item in items[:3 if include_draw else 2]]

    unused = list(items)
    quotes: list[object | None] = []
    for team in teams[:2]:
        team_label = _normalized_outcome_label(team)
        index = None
        for candidate_index, item in enumerate(unused):
            item_label = _normalized_outcome_label(item.get(label_key))
            if team_label and item_label and (team_label == item_label
                                              or team_label in item_label
                                              or item_label in team_label):
                index = candidate_index
                break
        quotes.append(unused.pop(index).get(quote_key) if index is not None else None)
    draw_index = next((index for index, item in enumerate(unused)
                       if _is_draw_outcome(item.get(label_key))), None)
    draw_quote = unused.pop(draw_index).get(quote_key) if draw_index is not None else None
    # Preserve a shared slot for the draw in both books. If only one venue
    # supplied it, the other is deliberately unavailable and cannot create an
    # apparent three-way arbitrage.
    return [quotes[0], draw_quote, quotes[1]] if include_draw else quotes


def _requested_leagues(query: str) -> tuple[str, ...] | None:
    """Read a comma-separated league filter from an API query string."""
    values = parse_qs(query).get("leagues", [])
    leagues = tuple(league.strip() for value in values for league in value.split(",") if league.strip())
    return leagues or None


def _pagination(query: str) -> tuple[int, int]:
    """Return a bounded, non-negative API page offset and size."""
    values = parse_qs(query)
    try:
        offset = max(0, int(values.get("offset", ["0"])[0]))
        limit = min(SPORT_PAGE_SIZE, max(1, int(values.get("limit", [str(SPORT_PAGE_SIZE)])[0])))
    except ValueError:
        return 0, SPORT_PAGE_SIZE
    return offset, limit


def _dollars(value: object) -> str | None:
    """Format cent-denominated venue values without sending raw API payloads."""
    try:
        return f"{Decimal(str(value)) / 100:.2f}"
    except (InvalidOperation, ValueError):
        return None


def account_summary() -> dict:
    """Return read-only wallet totals, never API credentials or raw responses."""
    load_dotenv(str(ROOT / ".env"))

    # Do not let one slow venue delay the whole dashboard. Account requests
    # remain read-only, but this view only needs a brief best-effort snapshot.
    with ThreadPoolExecutor(max_workers=3) as executor:
        kalshi_future = executor.submit(_kalshi_wallet)
        polymarket_future = executor.submit(_polymarket_wallet)
        novig_future = executor.submit(_novig_wallet)
        wallets = [kalshi_future.result(), polymarket_future.result(), novig_future.result()]

    # ProphetX does not have an account integration yet. Keep its card in the
    # dashboard so the wallet layout reflects every venue being compared.
    wallets.append({"venue": "ProphetX", "placeholder": True})

    return {"wallets": wallets, "updated_at": datetime.now(UTC).isoformat()}


def _kalshi_wallet() -> dict:
    try:
        payload = kalshi_balance(timeout=ACCOUNT_SUMMARY_TIMEOUT_SECONDS)
        return {
            "venue": "Kalshi",
            "balance": payload.get("balance_dollars") or _dollars(payload.get("balance")),
            "portfolio_value": _dollars(payload.get("portfolio_value")),
            "connected": True,
        }
    except Exception:
        return {"venue": "Kalshi", "balance": None, "portfolio_value": None, "connected": False}


def _polymarket_wallet() -> dict:
    try:
        balances = polymarket_us_balances(timeout=ACCOUNT_SUMMARY_TIMEOUT_SECONDS).get("balances", [])
        usd = next((item for item in balances if item.get("currency") == "USD"), balances[0] if balances else {})
        return {
            "venue": "Polymarket US",
            "balance": usd.get("displayedCash", usd.get("currentBalance")),
            "portfolio_value": usd.get("currentBalance"),
            "connected": bool(usd),
        }
    except Exception:
        return {"venue": "Polymarket US", "balance": None, "portfolio_value": None, "connected": False}


def _novig_wallet() -> dict:
    """Read the configured Novig trading key's cash balance and positions."""
    try:
        balance = novig_balance(os.environ["NOVIG_KEY_ID"], timeout=ACCOUNT_SUMMARY_TIMEOUT_SECONDS)
        # Validate the trading-read key each refresh, but do not invent a
        # marked portfolio value from the position quantities.
        novig_positions(timeout=ACCOUNT_SUMMARY_TIMEOUT_SECONDS)
        return {
            "venue": "Novig",
            "balance": f"{Decimal(str(balance['balance'])):.2f}",
            "portfolio_value": None,
            "connected": True,
        }
    except Exception:
        return {"venue": "Novig", "balance": None, "portfolio_value": None, "connected": False}


def opportunities_payload() -> dict:
    """Summarize cached reports for the home dashboard without new API calls."""
    rows = []
    counts: dict[str, int] = {}
    for sport in supported_sports():
        path = REPORTS_DIR / f"{sport}_matches.json"
        if not path.exists():
            continue
        try:
            records = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        current = [record for record in records if is_current_sport_record(record, sport)]
        counts[sport] = len(current)
        for record in current:
            # Both quote arrays must use the visible team order. Kalshi and
            # Polymarket US are free to return their contracts in opposite
            # orders for the same game.
            has_draw = any(_is_draw_outcome(item.get("contract"))
                           for item in record.get("kalshi_moneyline_asks", [])) or any(
                _is_draw_outcome(item.get("outcome"))
                for item in record.get("polymarket_us_displayed_moneyline_quotes", []))
            kalshi = _ordered_moneyline_quotes(
                record, "kalshi_moneyline_asks", "contract", "yes_ask", has_draw)
            poly = _ordered_moneyline_quotes(
                record, "polymarket_us_displayed_moneyline_quotes", "outcome", "displayed_quote", has_draw)
            novig = _ordered_moneyline_quotes(
                record, "novig_moneyline_quotes", "outcome", "displayed_ask", has_draw)
            try:
                if len(kalshi) != len(poly):
                    raise ValueError("venue outcome counts differ")
                # Choose the lower displayed quote for each mutually
                # exclusive outcome, including draw when it is offered.
                total = sum(min(float(kalshi[index]), float(poly[index]))
                            for index in range(len(kalshi)))
                edge = max(0, 1 - total)
            except (IndexError, TypeError, ValueError):
                edge = 0
            rows.append({"sport": sport, "title": record.get("polymarket_us_title") or record.get("kalshi_title"),
                         "start_time": record.get("start_time"), "kalshi": kalshi, "polymarket_us": poly,
                         "novig": novig,
                         "teams": record.get("teams", []),
                         "edge": edge, "kalshi_ticker": record.get("kalshi_event_ticker"),
                         "polymarket_slug": record.get("polymarket_us_event_slug"),
                         "kalshi_url": record.get("kalshi_url"),
                         "polymarket_us_url": record.get("polymarket_us_url")})
    return {"opportunities": sorted(rows, key=lambda row: row["edge"], reverse=True)[:8], "sport_counts": counts}


def game_payload(sport: str, kalshi_ticker: str, polymarket_slug: str) -> dict:
    """Find one current game across the sport's cached league reports."""
    if sport not in supported_sports():
        raise ValueError(f"unsupported sport: {sport}")
    if not kalshi_ticker or not polymarket_slug:
        raise ValueError("both venue event identifiers are required")

    # A Home opportunity can originate in an all-league cache while a sidebar
    # tab uses a league-specific cache. Search both so a click always opens
    # the selected event instead of assuming it falls within a particular page.
    paths = sorted(REPORTS_DIR.glob(f"{sport}*_matches.json"),
                   key=lambda path: (path.name != f"{sport}_matches.json", path.name))
    for path in paths:
        try:
            records = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        record = next((item for item in records
                       if item.get("kalshi_event_ticker") == kalshi_ticker
                       and item.get("polymarket_us_event_slug") == polymarket_slug
                       and is_current_sport_record(item, sport)), None)
        if record is not None:
            return {"record": record}
    raise LookupError("This game is no longer active or is unavailable in the latest report.")


def sport_payload(sport: str, leagues: tuple[str, ...] | None = None,
                  refresh: bool = False, offset: int = 0,
                  limit: int = SPORT_PAGE_SIZE) -> dict:
    if sport not in supported_sports():
        raise ValueError(f"unsupported sport: {sport}")
    if leagues:
        unknown_leagues = set(leagues) - set(supported_leagues(sport))
        if unknown_leagues:
            raise ValueError(f"unsupported {sport} league: {', '.join(sorted(unknown_leagues))}")
    # Each sidebar choice gets its own cache so a college/pro selection never
    # displays records fetched for the other category.
    cache_suffix = f"_{'-'.join(leagues)}" if leagues else ""
    report_path = REPORTS_DIR / f"{sport}{cache_suffix}_matches.json"
    try:
        if refresh or not report_path.exists():
            records, kalshi_count, polymarket_count = build_sport_report(sport, leagues)
            _write_sport_report_cache(report_path, records, kalshi_count, polymarket_count)
        else:
            records = json.loads(report_path.read_text())
            kalshi_count, polymarket_count = _cached_source_counts(report_path)
            # Reports written before logo support lack the `teams` field. Refresh
            # them automatically instead of showing permanent initials badges.
            if any(not record.get("teams") or not record.get("kalshi_url") or not record.get("polymarket_us_url")
                   or not record.get("kalshi_market_breakdown") or not record.get("polymarket_us_market_breakdown")
                   or "market_catalog" not in record
                   or "novig_moneyline_quotes" not in record
                   or (leagues and record.get("league") not in leagues)
                   or record.get("market_breakdown_version") != MARKET_BREAKDOWN_VERSION
                   for record in records):
                records, kalshi_count, polymarket_count = build_sport_report(sport, leagues)
                _write_sport_report_cache(report_path, records, kalshi_count, polymarket_count)
    except Exception:
        # A manual/automatic refresh must not turn a temporary venue throttle
        # into a blank dashboard when a prior usable report is available.
        if not report_path.exists():
            raise
        records = json.loads(report_path.read_text())
        kalshi_count, polymarket_count = _cached_source_counts(report_path)
    # Older cached reports may predate the stale-event filter. Apply it at
    # read time too, so completed games disappear without needing a refresh.
    records = [record for record in records
               if is_current_sport_record(record, sport)
               and (not leagues or record.get("league") in leagues)]
    total_records = len(records)
    page = records[offset:offset + limit]
    next_offset = offset + len(page)
    return {
        "sport": sport,
        "leagues": leagues or (),
        "records": page,
        "total_records": total_records,
        "next_offset": next_offset if next_offset < total_records else None,
        "updated_at": datetime.now(UTC).isoformat(),
        "kalshi_events_compared": kalshi_count,
        "polymarket_events_compared": polymarket_count,
    }


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def end_headers(self) -> None:
        # This local development dashboard must reflect edited JS and CSS
        # immediately; cached assets can otherwise keep an older layout alive.
        self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        if path == "/api/account-summary":
            self.send_json(account_summary())
            return
        if path == "/api/opportunities":
            self.send_json(opportunities_payload())
            return
        if path == "/api/game":
            query = parse_qs(parsed_url.query)
            try:
                self.send_json(game_payload(
                    query.get("sport", [""])[0],
                    query.get("kalshi", [""])[0],
                    query.get("polymarket", [""])[0],
                ))
            except (ValueError, LookupError) as error:
                self.send_json({"error": str(error)}, HTTPStatus.NOT_FOUND)
            return
        sport = path.removeprefix("/api/sports/")
        if sport in supported_sports():
            try:
                leagues = _requested_leagues(parsed_url.query)
                offset, limit = _pagination(parsed_url.query)
                self.send_json(sport_payload(sport, leagues, offset=offset, limit=limit))
            except Exception as error:  # makes API/network errors visible in the UI
                self.send_json({"error": str(error)}, HTTPStatus.BAD_GATEWAY)
            return
        if path == "/":
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        prefix = "/api/sports/"
        suffix = "/refresh"
        if not path.startswith(prefix) or not path.endswith(suffix):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        sport = path[len(prefix):-len(suffix)]
        if sport not in supported_sports():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            leagues = _requested_leagues(parsed_url.query)
            offset, limit = _pagination(parsed_url.query)
            self.send_json(sport_payload(sport, leagues, refresh=True, offset=offset, limit=limit))
        except Exception as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_GATEWAY)

    def send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # The browser can cancel an in-flight auto-refresh when navigating
            # away. The response is no longer needed, so do not log a server
            # error or attempt a second response on the closed socket.
            return


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local read-only arbitrage research dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"Dashboard: http://{args.host}:{args.port}")
    print("Read-only: no orders, transfers, or withdrawals are available.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

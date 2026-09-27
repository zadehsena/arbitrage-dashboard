import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from arbitrage.dashboard import (
    ACCOUNT_SUMMARY_TIMEOUT_SECONDS,
    SPORT_PAGE_SIZE,
    _cached_source_counts,
    _pagination,
    _write_sport_report_cache,
    account_summary,
    game_payload,
    opportunities_payload,
)


class DashboardAccountSummaryTest(unittest.TestCase):
    def test_sport_cache_persists_source_event_totals(self):
        with TemporaryDirectory() as directory:
            report_path = Path(directory) / "football_nfl_matches.json"
            with patch("arbitrage.dashboard.REPORTS_DIR", Path(directory)):
                _write_sport_report_cache(report_path, [{"title": "Example"}], 31, 32)

            self.assertEqual(_cached_source_counts(report_path), (31, 32))

    def test_pagination_bounds_requested_page_size_and_offset(self):
        self.assertEqual(_pagination("offset=25&limit=25"), (25, 25))
        self.assertEqual(_pagination("offset=-1&limit=1000"), (0, SPORT_PAGE_SIZE))
        self.assertEqual(_pagination("offset=invalid&limit=invalid"), (0, SPORT_PAGE_SIZE))

    def test_opportunities_aligns_opposite_venue_contract_order(self):
        record = {
            "teams": [{"name": "Houston Christian"}, {"name": "North Texas"}],
            # The real feeds for this matchup return the venue arrays in
            # opposite orders. Pairing their first entries would invent 98%.
            "kalshi_moneyline_asks": [
                {"contract": "North Texas wins", "yes_ask": "1.0000"},
                {"contract": "Houston Christian wins", "yes_ask": "0.0100"},
                {"contract": "Tie is the result", "yes_ask": "0.0100"},
            ],
            "polymarket_us_displayed_moneyline_quotes": [
                {"outcome": "Houston Christian", "displayed_quote": "0.0100"},
                {"outcome": "North Texas", "displayed_quote": "0.9950"},
                {"outcome": "Draw", "displayed_quote": "0.0050"},
            ],
        }
        with TemporaryDirectory() as directory:
            with patch("arbitrage.dashboard.REPORTS_DIR", Path(directory)), \
                 patch("arbitrage.dashboard.supported_sports", return_value=("football",)), \
                 patch("arbitrage.dashboard.is_current_sport_record", return_value=True):
                (Path(directory) / "football_matches.json").write_text(json.dumps([record]))
                row = opportunities_payload()["opportunities"][0]

        self.assertEqual(row["kalshi"], ["0.0100", "0.0100", "1.0000"])
        self.assertEqual(row["polymarket_us"], ["0.0100", "0.0050", "0.9950"])
        self.assertEqual(row["edge"], 0)

    def test_game_payload_finds_event_in_league_specific_report(self):
        record = {
            "kalshi_event_ticker": "KX-EXAMPLE",
            "polymarket_us_event_slug": "example-game",
        }
        with TemporaryDirectory() as directory:
            with patch("arbitrage.dashboard.REPORTS_DIR", Path(directory)), \
                 patch("arbitrage.dashboard.supported_sports", return_value=("football",)), \
                 patch("arbitrage.dashboard.is_current_sport_record", return_value=True):
                (Path(directory) / "football_cfb_matches.json").write_text(json.dumps([record]))
                payload = game_payload("football", "KX-EXAMPLE", "example-game")

        self.assertEqual(payload["record"], record)

    def test_sport_payload_uses_last_good_cache_when_refresh_fails(self):
        record = {"start_time": "2030-01-01T00:00:00Z", "league": "nfl", "teams": [{"name": "Example"}]}
        with TemporaryDirectory() as directory:
            report_path = Path(directory) / "football_nfl_matches.json"
            report_path.write_text(json.dumps([record]))
            with patch("arbitrage.dashboard.REPORTS_DIR", Path(directory)), \
                 patch("arbitrage.dashboard.supported_sports", return_value=("football",)), \
                 patch("arbitrage.dashboard.supported_leagues", return_value=("nfl",)), \
                 patch("arbitrage.dashboard.build_sport_report", side_effect=RuntimeError("429")), \
                 patch("arbitrage.dashboard.is_current_sport_record", return_value=True):
                from arbitrage.dashboard import sport_payload
                payload = sport_payload("football", ("nfl",), refresh=True)

        self.assertEqual(payload["records"], [record])

    @patch("arbitrage.dashboard.polymarket_us_balances")
    @patch("arbitrage.dashboard.kalshi_balance")
    def test_account_summary_uses_short_dashboard_timeouts(self, kalshi_balance, polymarket_balances):
        kalshi_balance.return_value = {"balance": 1250, "portfolio_value": 3000}
        polymarket_balances.return_value = {"balances": [{"currency": "USD", "displayedCash": "7.50"}]}

        summary = account_summary()

        kalshi_balance.assert_called_once_with(timeout=ACCOUNT_SUMMARY_TIMEOUT_SECONDS)
        polymarket_balances.assert_called_once_with(timeout=ACCOUNT_SUMMARY_TIMEOUT_SECONDS)
        self.assertEqual([wallet["venue"] for wallet in summary["wallets"]],
                         ["Kalshi", "Polymarket US", "Novig", "ProphetX"])

import unittest

from arbitrage.football import (
    KALSHI_SERIES_BY_LEAGUE,
    companion_event_tickers,
    market_catalog,
    market_category,
    match_events,
)


class FootballTest(unittest.TestCase):
    def test_has_kalshi_series_for_supported_leagues(self):
        self.assertEqual(KALSHI_SERIES_BY_LEAGUE["cfb"], "KXNCAAFGAME")
        self.assertEqual(KALSHI_SERIES_BY_LEAGUE["nfl"], "KXNFLGAME")

    def test_matches_reordered_team_title(self):
        kalshi = [{"event_ticker": "KX", "title": "Army vs Temple"}]
        poly = [{"slug": "game", "title": "Temple vs. Army"}]
        result = match_events(kalshi, poly, 0.7)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][2], 1.0)

    def test_classifies_kalshi_team_wins_contract_as_moneyline(self):
        self.assertEqual(market_category({"title": "Pittsburgh wins"}), "moneyline")

    def test_classifies_cover_and_player_total_base_markets(self):
        self.assertEqual(market_category({"sportsMarketType": "moneyline", "question": "Will Pittsburgh cover -1.5?"}), "spread")
        self.assertEqual(market_category({"title": "Washington wins by over 1.5 runs?"}), "spread")
        self.assertEqual(market_category({"question": "Will a player record at least 2 total bases?"}), "props")

    def test_derives_mlb_spread_and_total_companion_tickers(self):
        self.assertEqual(
            companion_event_tickers("KXMLBGAME-26SEP261605NYMWSH", "KXMLBGAME"),
            ["KXMLBSPREAD-26SEP261605NYMWSH", "KXMLBTOTAL-26SEP261605NYMWSH"],
        )

    def test_derives_college_football_spread_and_total_companion_tickers(self):
        self.assertEqual(
            companion_event_tickers("KXNCAAFGAME-26SEP26ALBYPRIN", "KXNCAAFGAME"),
            ["KXNCAAFSPREAD-26SEP26ALBYPRIN", "KXNCAAFTOTAL-26SEP26ALBYPRIN"],
        )

    def test_normalized_catalog_has_fixed_venue_slots(self):
        catalog = market_catalog([{"category": "moneyline", "market": "Mets win", "yes_ask": "0.51", "no_ask": "0.50"}], [])
        self.assertEqual(set(catalog[0]["quotes"]), {"kalshi", "polymarket_us", "novig", "prophetx"})
        self.assertEqual(catalog[0]["match_status"], "review")

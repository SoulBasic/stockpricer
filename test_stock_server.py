import unittest

from stock_server import build_markdown, compact_quote


class CompactQuoteTests(unittest.TestCase):
    def quote(self, state, price):
        return {
            "ok": True,
            "symbol": "AAPL",
            "name": "Apple Inc.",
            "market": "US",
            "currency": "USD",
            "marketState": state,
            "price": price,
            "previousClose": 95.0,
            "timestamp": "2026-07-14 04:00:00",
            "volume": 900,
            "amount": 9000,
            "session": {
                "regular": {
                    "price": 100.0,
                    "time": "2026-07-14 04:00:00",
                    "volume": 900,
                    "amount": 9000,
                },
                "pre": {
                    "price": 101.0,
                    "time": "2026-07-14 20:00:00",
                    "volume": 10,
                    "amount": 1010,
                },
                "post": {
                    "price": 102.0,
                    "time": "2026-07-15 05:00:00",
                    "volume": 20,
                    "amount": 2040,
                },
            },
        }

    def test_pre_market_contains_only_pre_market_values(self):
        result = compact_quote(self.quote("PRE", 101.0))

        self.assertEqual(result["previousClose"], 100.0)
        self.assertEqual(result["change"], 1.0)
        self.assertEqual(result["changePercent"], 1.0)
        self.assertEqual(result["timestamp"], "2026-07-14 20:00:00")
        self.assertEqual(result["volume"], 10)
        self.assertEqual(result["amount"], 1010)
        self.assertEqual(set(result), {
            "ok", "symbol", "name", "market", "currency", "marketState",
            "price", "previousClose", "change", "changePercent", "timestamp",
            "volume", "amount",
        })
        self.assertNotIn("session", result)
        self.assertNotIn("open", result)

    def test_post_market_uses_just_closed_regular_price(self):
        result = compact_quote(self.quote("POST", 102.0))

        self.assertEqual(result["previousClose"], 100.0)
        self.assertEqual(result["change"], 2.0)
        self.assertEqual(result["changePercent"], 2.0)
        self.assertEqual(result["volume"], 20)
        self.assertEqual(result["amount"], 2040)

    def test_regular_market_uses_previous_trading_day_close(self):
        result = compact_quote(self.quote("REGULAR", 100.0))

        self.assertEqual(result["previousClose"], 95.0)
        self.assertEqual(result["change"], 5.0)
        self.assertAlmostEqual(result["changePercent"], 5.2632)
        self.assertEqual(result["volume"], 900)
        self.assertEqual(result["amount"], 9000)

    def test_missing_extended_volume_does_not_fall_back_to_regular(self):
        quote = self.quote("PRE", 101.0)
        quote["session"]["pre"]["volume"] = None
        quote["session"]["pre"]["amount"] = None

        result = compact_quote(quote)

        self.assertIsNone(result["volume"])
        self.assertIsNone(result["amount"])

    def test_markdown_is_single_session_and_uses_red_green_markers(self):
        rising = build_markdown(compact_quote(self.quote("PRE", 101.0)))
        falling_quote = self.quote("POST", 98.0)
        falling_quote["session"]["post"]["price"] = 98.0
        falling = build_markdown(compact_quote(falling_quote))

        self.assertIn("🔴 +1.00（+1.00%）", rising)
        self.assertIn("🟢 -2.00（-2.00%）", falling)
        self.assertIn("上个收盘价", rising)
        self.assertNotIn("| 时段 |", rising)
        self.assertNotIn("盘后", rising)


if __name__ == "__main__":
    unittest.main()

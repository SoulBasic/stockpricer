import unittest
import unittest.mock

from stock_server import (build_markdown, compact_quote, eastmoney_quote,
                          _fmt_market_cap, normalize_yahoo_quote, tencent_quote)


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
            "marketCap": 3000000000000,
            "pe": 30.25,
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
            "volume", "amount", "marketCap", "pe",
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

    def test_markdown_uses_only_supported_red_green_color_tags(self):
        rising = build_markdown(compact_quote(self.quote("PRE", 101.0)))
        falling_quote = self.quote("POST", 98.0)
        falling_quote["session"]["post"]["price"] = 98.0
        falling = build_markdown(compact_quote(falling_quote))
        flat_quote = self.quote("REGULAR", 95.0)
        flat = build_markdown(compact_quote(flat_quote))

        self.assertIn('<font color="info">▲ +1.00 (+1.00%)</font>', rising)
        self.assertIn('<font color="warning">▼ -2.00 (-2.00%)</font>', falling)
        self.assertIn('<font color="comment">— +0.00 (+0.00%)</font>', flat)
        self.assertIn('# <font color="info">101.00 USD</font>', rising)
        self.assertIn('# <font color="warning">98.00 USD</font>', falling)
        self.assertIn('# <font color="comment">95.00 USD</font>', flat)
        self.assertNotIn("🔴", rising)
        self.assertNotIn("🟢", falling)
        self.assertIn("上个收盘价", rising)
        self.assertIn("**市值** 3万亿USD", rising)
        self.assertIn("**市盈率（PE）** 30.25", rising)
        self.assertNotIn("| 时段 |", rising)
        self.assertNotIn("盘后", rising)

    def test_market_cap_uses_chinese_units(self):
        self.assertEqual(_fmt_market_cap(13840000000, "USD"), "138.4亿USD")
        self.assertEqual(_fmt_market_cap(1380000000000, "USD"), "1.38万亿USD")


class ValuationSourceTests(unittest.TestCase):
    def test_yahoo_uses_market_cap_and_trailing_pe(self):
        result = normalize_yahoo_quote("us", "AAPL", {
            "symbol": "AAPL", "regularMarketPrice": 100,
            "regularMarketPreviousClose": 99, "marketState": "REGULAR",
            "marketCap": 3000000000000, "trailingPE": 30.25,
        })

        self.assertEqual(result["marketCap"], 3000000000000)
        self.assertEqual(result["pe"], 30.25)

    def test_tencent_scales_market_cap_from_hundred_millions(self):
        fields = [""] * 46
        fields[1], fields[2], fields[3], fields[4], fields[5] = (
            "测试公司", "00700", "500", "490", "495")
        fields[30], fields[39], fields[45] = "20260805120000", "18.5", "45000"
        payload = 'v_hk00700="%s";' % "~".join(fields)
        with unittest.mock.patch("stock_server.fetch_resilient", return_value=payload):
            result = tencent_quote("hk", "00700")

        self.assertEqual(result["marketCap"], 4500000000000)
        self.assertEqual(result["pe"], 18.5)

    def test_eastmoney_uses_total_market_cap_and_ttm_pe(self):
        payload = ('{"data":{"f43":1000,"f57":"600000","f58":"测试公司",'
                   '"f59":2,"f60":900,"f116":123456789,"f164":1875}}')
        with unittest.mock.patch("stock_server.fetch_resilient", return_value=payload):
            result = eastmoney_quote("cn", "600000")

        self.assertEqual(result["marketCap"], 123456789)
        self.assertEqual(result["pe"], 18.75)


if __name__ == "__main__":
    unittest.main()

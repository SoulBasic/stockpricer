import unittest
import unittest.mock

from stock_server import (build_markdown, compact_quote, eastmoney_quote,
                          _fmt_market_cap, fill_yahoo_extended_from_robinhood,
                          normalize_yahoo_quote, tencent_quote)


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

    def test_pre_market_without_pre_print_quotes_regular_day_move(self):
        # Yahoo says PRE but has no pre-market trade: the headline is still the
        # regular close, so its move is vs the previous day, not 0 vs itself.
        quote = self.quote("PRE", 100.0)
        quote["session"]["pre"] = None
        quote["session"]["post"] = None

        result = compact_quote(quote)

        self.assertEqual(result["marketState"], "PRE")
        self.assertEqual(result["previousClose"], 95.0)
        self.assertEqual(result["change"], 5.0)
        self.assertAlmostEqual(result["changePercent"], 5.2632)
        self.assertEqual(result["timestamp"], "2026-07-14 04:00:00")

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


class RobinhoodExtendedFillTests(unittest.TestCase):
    # 2026-11-10 16:00 EST regular close; Yahoo PRE with no preMarketPrice
    # (what it returned for Nasdaq symbols on 2026-11-11).
    YAHOO = {"symbol": "AAPL", "marketState": "PRE",
             "regularMarketPrice": 348.51, "regularMarketPreviousClose": 350.69,
             "regularMarketTime": 1794344400}

    def rh(self, price, when):
        return {"last_extended_hours_trade_price": "%.6f" % price,
                "venue_last_non_reg_trade_time": when}

    def test_missing_pre_market_is_filled_from_robinhood(self):
        yq = fill_yahoo_extended_from_robinhood(
            self.YAHOO, self.rh(346.5, "2026-11-11T10:05:41.702847013Z"))
        result = compact_quote(normalize_yahoo_quote("us", "AAPL", yq))

        self.assertEqual(result["marketState"], "PRE")
        self.assertEqual(result["price"], 346.5)
        self.assertEqual(result["previousClose"], 348.51)
        self.assertEqual(result["change"], -2.01)
        self.assertAlmostEqual(result["changePercent"], -0.5767)
        self.assertNotIn("preMarketPrice", self.YAHOO)

    def test_newer_yahoo_pre_market_is_kept(self):
        yahoo = dict(self.YAHOO, preMarketPrice=347.0,
                     preMarketTime=1794391600)       # 10:06:40Z
        yq = fill_yahoo_extended_from_robinhood(
            yahoo, self.rh(346.5, "2026-11-11T10:05:41Z"))

        self.assertIs(yq, yahoo)

    def test_after_hours_print_fills_post_market_only(self):
        yq = fill_yahoo_extended_from_robinhood(
            self.YAHOO, self.rh(349.0, "2026-11-11T00:59:58Z"))  # 19:59:58 EST

        self.assertEqual(yq["postMarketPrice"], 349.0)
        self.assertNotIn("preMarketPrice", yq)

    def test_print_before_regular_close_is_ignored(self):
        yq = fill_yahoo_extended_from_robinhood(
            self.YAHOO, self.rh(350.0, "2026-11-10T14:00:00Z"))  # 09:00 EST

        self.assertIs(yq, self.YAHOO)


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

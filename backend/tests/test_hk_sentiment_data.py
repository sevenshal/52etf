import unittest

import pandas as pd

from src.core.services.hk_sentiment_data_service import (
    normalize_southbound_net,
    parse_short_selling_summary,
)


class NormalizeSouthboundTest(unittest.TestCase):
    def test_daily_regime_is_converted_from_million_to_100mn(self):
        frame = pd.DataFrame(
            {
                "trade_date": ["20230103", "20230104", "20230105"],
                "south_money": [1000.0, -500.0, 250.0],
            }
        )
        net = normalize_southbound_net(frame)
        self.assertAlmostEqual(10.0, float(net.iloc[0]))
        self.assertAlmostEqual(-5.0, float(net.iloc[1]))
        self.assertAlmostEqual(2.5, float(net.iloc[2]))

    def test_cumulative_regime_uses_diff(self):
        frame = pd.DataFrame(
            {
                "trade_date": ["20260105", "20260106", "20260107"],
                "south_money": [50000.0, 50030.0, 50010.0],
            }
        )
        net = normalize_southbound_net(frame)
        self.assertTrue(pd.isna(net.iloc[0]))
        self.assertAlmostEqual(30.0, float(net.iloc[1]))
        self.assertAlmostEqual(-20.0, float(net.iloc[2]))

    def test_switch_day_gap_is_dropped(self):
        frame = pd.DataFrame(
            {
                "trade_date": ["20231122", "20231123", "20231124", "20231127", "20231215"],
                "south_money": [1200.0, -800.0, 28800.0, 28830.0, 29000.0],
            }
        )
        net = normalize_southbound_net(frame)
        # 换口径前后单位不同，差分没有意义，直接留空。
        self.assertTrue(pd.isna(net.iloc[2]))
        self.assertTrue(pd.isna(net.iloc[3]))
        self.assertAlmostEqual(170.0, float(net.iloc[4]))

    def test_empty_frame(self):
        self.assertTrue(normalize_southbound_net(pd.DataFrame()).empty)


class ParseShortSellingTest(unittest.TestCase):
    SAMPLE = """
    <table>
    Short Selling Turnover Total Value ($)          : HKD 12,345,678,900
    Total market turnover                           : HKD 98,765,432,100
    Short Selling of Designated Securities (excluding ETP) as % total turnover   : 12.50%
    </table>
    """

    def test_parses_summary(self):
        summary = parse_short_selling_summary(self.SAMPLE)
        self.assertIsNotNone(summary)
        self.assertAlmostEqual(0.125, summary["short_ratio"])
        self.assertAlmostEqual(12345678900.0, summary["short_turnover"])
        self.assertAlmostEqual(98765432100.0, summary["market_turnover"])

    def test_returns_none_without_footer(self):
        self.assertIsNone(parse_short_selling_summary("<html>not a trading day</html>"))


if __name__ == "__main__":
    unittest.main()

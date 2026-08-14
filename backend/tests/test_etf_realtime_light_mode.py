"""ETF 实时贪恐轻量模式：不取成分股行情时强度/宽度沿用日线值。"""
from unittest import TestCase
from unittest.mock import patch

import numpy as np
import pandas as pd

from src.core.services.etf_fear_greed_clone_service import ETFFearGreedCloneCalculator


def _price_frame(days=600):
    index = pd.bdate_range("2024-01-02", periods=days)
    return pd.DataFrame(
        {"close": np.linspace(100.0, 200.0, days), "volume": np.full(days, 1e6)},
        index=index,
    )


class ETFRealtimeLightModeTest(TestCase):
    def _calculator(self):
        calc = ETFFearGreedCloneCalculator.__new__(ETFFearGreedCloneCalculator)
        return calc

    def _build(self, fetch_holdings_quotes):
        calc = self._calculator()
        frame = _price_frame()
        with patch.object(calc, "_fetch_recent_price_history", return_value=frame), \
             patch.object(calc, "_append_realtime_quote", side_effect=lambda f, d, q: f), \
             patch.object(calc, "_latest_stored_component_raw", return_value={}), \
             patch.object(
                 calc,
                 "_fetch_realtime_barchart_put_call_raw",
                 return_value=(np.nan, None, None),
             ):
            return calc._build_realtime_raw_row(
                etf_symbol="SPY.US",
                holdings=[],
                quote_map={"SPY.US": {"price": 600.0}, "TLT.US": {"price": 90.0}},
                current_date=frame.index[-1].date(),
                previous_trading_day=frame.index[-2].date(),
                price_history_count=320,
                fetch_holdings_quotes=fetch_holdings_quotes,
            )

    def test_light_mode_without_holdings_quotes_does_not_raise(self):
        raw_values, price_payload, raw_freshness = self._build(fetch_holdings_quotes=False)
        # 强度/宽度留空，由调用方沿用最近日线值
        self.assertTrue(np.isnan(raw_values["stock_price_strength"]))
        self.assertTrue(np.isnan(raw_values["stock_price_breadth"]))
        # 纯价格分量（动量/波动/避险）有实时值
        self.assertTrue(np.isfinite(raw_values["market_momentum"]))
        self.assertTrue(np.isfinite(raw_values["market_volatility"]))
        self.assertTrue(np.isfinite(raw_values["safe_haven_demand"]))
        self.assertEqual(
            "latest stored constituent strength component",
            raw_freshness["stock_price_strength"],
        )

    def test_full_mode_without_holding_history_raises(self):
        with self.assertRaises(Exception):
            self._build(fetch_holdings_quotes=True)

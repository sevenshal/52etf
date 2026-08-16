from unittest import TestCase
from unittest.mock import patch

import pandas as pd

from src.app.api.soxl_fear_backtest import (
    SOXLFearStrategyParams,
    _prepare_base_dataframe,
    _run_backtest,
    _run_seesaw_backtest,
)


def price_frame(n=40):
    dates = pd.bdate_range("2024-01-01", periods=n).date
    return pd.DataFrame(
        {
            "date": dates,
            "open": [10.0] * n,
            "high": [12.0] * n,
            "low": [9.0] * n,
            "close": [11.0] * n,
            "volume": [100.0] * n,
            "turnover": [1000.0] * n,
        }
    )


def _base_df(price_df, fear_df):
    with patch(
        "src.app.api.soxl_fear_backtest._fetch_price_history",
        return_value=price_df,
    ), patch(
        "src.app.api.soxl_fear_backtest._fetch_fear_history",
        return_value=(fear_df, {"fear_source": "cnn", "fear_source_label": "CNN 恐贪", "fear_points": len(fear_df)}),
    ):
        base_df, _meta = _prepare_base_dataframe(
            "000015.SH", price_df.iloc[0]["date"], price_df.iloc[-1]["date"], "cnn"
        )
    return base_df


class IdleFieldTest(TestCase):
    def test_seesaw_idle_fields(self):
        price_df = price_frame()
        fear_df = pd.DataFrame({"date": price_df["date"], "fear_greed": [50.0] * len(price_df)})
        base_df = _base_df(price_df, fear_df)
        params = SOXLFearStrategyParams(
            symbol="000015.SH",
            buy_threshold=30.0,
            greed_threshold=70.0,
            volume_ratio_threshold=1.3,
            execute_next_open=True,
            sub_symbol="000688.SH",
            sub_buy_threshold=25.0,
            sub_volume_ratio_threshold=1.3,
            sub2_symbol="QQQ.US",
            sub2_buy_threshold=20.0,
            sub2_volume_ratio_threshold=1.3,
            swap_threshold=45.0,
        )
        result = _run_seesaw_backtest(base_df, base_df.copy(), params, 1_000_000.0, detailed=True,
                                      sub2_base_df=base_df.copy())
        self.assertIn("idle_days", result)
        self.assertIn("idle_ratio", result)
        self.assertIn("total_trading_days", result)
        self.assertEqual(result["total_trading_days"], len(base_df))
        self.assertTrue(0.0 <= result["idle_ratio"] <= 100.0)
        print("seesaw idle_days:", result["idle_days"], "idle_ratio:", result["idle_ratio"],
              "buys:", result["buy_count"])

    def test_single_idle_fields(self):
        price_df = price_frame()
        fear_df = pd.DataFrame({"date": price_df["date"], "fear_greed": [50.0] * len(price_df)})
        base_df = _base_df(price_df, fear_df)
        params = SOXLFearStrategyParams(symbol="000015.SH", buy_threshold=30.0, greed_threshold=70.0,
                                        volume_ratio_threshold=1.3, execute_next_open=True)
        result = _run_backtest(base_df, params, 1_000_000.0, detailed=True)
        self.assertIn("idle_days", result)
        self.assertIn("idle_ratio", result)
        print("single idle_days:", result["idle_days"], "idle_ratio:", result["idle_ratio"])

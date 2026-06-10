from unittest import TestCase
from unittest.mock import patch

import pandas as pd

from src.app.api.soxl_fear_backtest import (
    SOXLFearStrategyParams,
    _prepare_base_dataframe,
    _run_backtest,
)


class SoxlFearBacktestVolumeSignalTest(TestCase):
    def _price_frame(self):
        dates = pd.bdate_range("2024-01-01", periods=40).date
        volumes = [100.0] * len(dates)
        volumes[22] = 150.0
        volumes[23] = 160.0
        volumes[24] = 140.0
        return pd.DataFrame(
            {
                "date": dates,
                "open": [10.0] * len(dates),
                "high": [12.0] * len(dates),
                "low": [9.0] * len(dates),
                "close": [11.0] * len(dates),
                "volume": volumes,
                "turnover": [1000.0] * len(dates),
            }
        )

    def test_three_day_volume_signal_excludes_latest_three_days_from_baseline(self):
        price_df = self._price_frame()
        fear_df = pd.DataFrame(
            {
                "date": price_df["date"],
                "fear_greed": [20.0] * len(price_df),
            }
        )
        fear_meta = {"fear_source": "cnn", "fear_source_label": "CNN", "fear_points": len(fear_df)}

        with patch(
            "src.app.api.soxl_fear_backtest._fetch_price_history",
            return_value=price_df,
        ), patch(
            "src.app.api.soxl_fear_backtest._fetch_fear_history",
            return_value=(fear_df, fear_meta),
        ):
            base_df, _meta = _prepare_base_dataframe(
                "SOXL.US",
                price_df.iloc[24]["date"],
                price_df.iloc[-1]["date"],
                "cnn",
            )

        first_signal = base_df.iloc[0]
        self.assertEqual(price_df.iloc[24]["date"], first_signal["signal_date"])
        self.assertEqual(price_df.iloc[24]["date"], first_signal["date"])
        self.assertAlmostEqual(100.0, first_signal["volume_ma20_excluding_recent_3"])
        self.assertAlmostEqual(1.4, first_signal["volume_ratio_consecutive_3"])
        self.assertAlmostEqual(11.0, first_signal["execution_price"])
        self.assertEqual("same_day_close", _meta["execution_price_type"])

    def test_backtest_uses_configured_consecutive_volume_signal_for_buy(self):
        price_df = self._price_frame()
        fear_df = pd.DataFrame(
            {
                "date": price_df["date"],
                "fear_greed": [20.0] * len(price_df),
            }
        )
        fear_meta = {"fear_source": "cnn", "fear_source_label": "CNN", "fear_points": len(fear_df)}

        with patch(
            "src.app.api.soxl_fear_backtest._fetch_price_history",
            return_value=price_df,
        ), patch(
            "src.app.api.soxl_fear_backtest._fetch_fear_history",
            return_value=(fear_df, fear_meta),
        ):
            base_df, _meta = _prepare_base_dataframe(
                "SOXL.US",
                price_df.iloc[24]["date"],
                price_df.iloc[-1]["date"],
                "cnn",
            )

        result = _run_backtest(
            base_df,
            SOXLFearStrategyParams(
                buy_threshold=40.0,
                greed_threshold=90.0,
                volume_ratio_threshold=1.39,
                volume_ratio_consecutive_days=3,
                buy_position_pct=100.0,
                cooldown_days=0,
                trailing_stop_pct=5.0,
                sell_position_pct=50.0,
                rebalance_threshold_pct=0.0,
            ),
            10000.0,
            detailed=True,
        )

        buy_trades = [item for item in result["trades"] if item["action"] == "BUY"]
        self.assertGreaterEqual(len(buy_trades), 1)
        self.assertEqual(price_df.iloc[24]["date"].isoformat(), buy_trades[0]["date"])
        self.assertEqual(price_df.iloc[24]["date"].isoformat(), buy_trades[0]["signal_date"])
        self.assertAlmostEqual(11.0, buy_trades[0]["execution_price"])
        self.assertAlmostEqual(1.4, buy_trades[0]["buy_volume_ratio"])
        self.assertEqual(3, buy_trades[0]["volume_ratio_consecutive_days"])
        self.assertIn("连续 3 天", buy_trades[0]["reason"])

    def test_trailing_take_profit_uses_greed_zone_intraday_high_as_peak(self):
        dates = pd.bdate_range("2024-01-01", periods=3).date
        base_df = pd.DataFrame(
            {
                "date": dates,
                "open": [100.0, 118.0, 107.0],
                "high": [101.0, 120.0, 108.0],
                "low": [99.0, 106.0, 104.0],
                "close": [100.0, 118.0, 107.0],
                "execution_price": [100.0, 118.0, 107.0],
                "volume": [1000.0, 1000.0, 1000.0],
                "ma20": [100.0, 100.0, 100.0],
                "volume_ma20": [100.0, 100.0, 100.0],
                "volume_ratio": [2.0, 1.0, 1.0],
                "volume_ma20_excluding_recent_1": [100.0, 100.0, 100.0],
                "volume_ratio_consecutive_1": [2.0, 1.0, 1.0],
                "fear_greed": [20.0, 90.0, 90.0],
                "signal_volume": [200.0, 100.0, 100.0],
                "signal_date": dates,
                "fear_date": dates,
            }
        )

        result = _run_backtest(
            base_df,
            SOXLFearStrategyParams(
                buy_threshold=40.0,
                greed_threshold=80.0,
                volume_ratio_threshold=1.5,
                volume_ratio_consecutive_days=1,
                buy_position_pct=100.0,
                cooldown_days=0,
                trailing_stop_pct=5.0,
                sell_position_pct=50.0,
                sell_reduction_basis="holdings",
                sell_price_above_avg_cost=True,
                rebalance_threshold_pct=0.0,
            ),
            10000.0,
            detailed=True,
        )

        sells = [item for item in result["trades"] if item["action"] == "SELL"]
        self.assertEqual(1, len(sells))
        self.assertEqual(dates[2].isoformat(), sells[0]["date"])
        self.assertAlmostEqual(107.0, sells[0]["execution_price"])
        self.assertIn("回撤 10.83%", sells[0]["reason"])

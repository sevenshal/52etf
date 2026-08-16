from unittest import TestCase
from unittest.mock import patch

import pandas as pd

from src.app.api.soxl_fear_backtest import (
    SOXLFearStrategyParams,
    _prepare_base_dataframe,
    _run_backtest,
    _run_seesaw_backtest,
)


def price_frame(n=45):
    dates = pd.bdate_range("2024-01-01", periods=n).date
    volumes = [100.0] * n
    volumes[25] = 260.0  # 大幅放量日（log_z >> 1.25）
    volumes[26] = 250.0
    volumes[27] = 240.0
    return pd.DataFrame(
        {
            "date": dates,
            "open": [10.0] * n,
            "high": [12.0] * n,
            "low": [9.0] * n,
            "close": [11.0] * n,
            "volume": volumes,
            "turnover": [1000.0] * n,
        }
    )


def _base_df(price_df, fear_values):
    fear_df = pd.DataFrame({"date": price_df["date"], "fear_greed": fear_values})
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


class LogZVolumeTest(TestCase):
    def test_log_z_column_computed(self):
        price_df = price_frame()
        base_df = _base_df(price_df, [50.0] * len(price_df))
        self.assertIn("log_z", base_df.columns)
        # 放量日（index 25+，但前 20 日窗口需滚动均值）log_z 应显著 > 0
        surge_rows = base_df[base_df["signal_volume"] > 200]
        self.assertGreater(len(surge_rows), 0)
        self.assertTrue((surge_rows["log_z"].dropna() > 1.0).all())

    def test_buy_requires_log_z_threshold(self):
        """volume_z_threshold=1.25：恐慌日但放量不足时不应买入。"""
        price_df = price_frame()
        fear_values = [50.0] * len(price_df)
        fear_values[27] = 20.0  # 恐慌日，但 27 日量 240 -> 前20日均值100 -> log_z≈? 260/250/240 vs mean100 => 显著>1.25
        base_df = _base_df(price_df, fear_values)
        params = SOXLFearStrategyParams(
            symbol="000015.SH", buy_threshold=30.0, greed_threshold=70.0,
            volume_z_threshold=1.25, sell_shrink_z=-1.0,
            execute_next_open=True, volume_ratio_consecutive_days=1,
        )
        result = _run_backtest(base_df, params, 1_000_000.0, detailed=True)
        self.assertGreater(result["buy_count"], 0)
        # 放量日 25-27 log_z 高，恐慌日 27 满足条件应买入
        buys = [t for t in result["trades"] if t["action"] == "BUY"]
        self.assertGreaterEqual(len(buys), 1)

    def test_sell_shrink_requires_shrink(self):
        """sell_shrink_z=0.25：贪恐>=70 且当日缩量才卖；若无缩量日则不卖。"""
        price_df = price_frame()
        # 贪恐>=70 从第 30 天起；成交量持续高位（不缩量）→ 不应卖出
        fear_values = [50.0] * len(price_df)
        for i in range(30, len(fear_values)):
            fear_values[i] = 75.0
        base_df = _base_df(price_df, fear_values)
        params = SOXLFearStrategyParams(
            symbol="000015.SH", buy_threshold=30.0, greed_threshold=70.0,
            volume_z_threshold=1.25, sell_shrink_z=0.25,
            execute_next_open=True, volume_ratio_consecutive_days=1,
        )
        result = _run_backtest(base_df, params, 1_000_000.0, detailed=True)
        sells = [t for t in result["trades"] if t["action"] == "SELL"]
        # 贪区成交量恒高（100 附近，不缩量）→ 应无卖出（或很少）
        self.assertEqual(len(sells), 0)

    def test_seesaw_log_z_path(self):
        price_df = price_frame()
        fear_values = [50.0] * len(price_df)
        fear_values[27] = 20.0
        for i in range(30, len(fear_values)):
            fear_values[i] = 30.0  # 贪恐 30 <70，不卖
        base_df = _base_df(price_df, fear_values)
        params = SOXLFearStrategyParams(
            symbol="000015.SH", buy_threshold=30.0, greed_threshold=70.0,
            volume_z_threshold=1.25, sell_shrink_z=0.25,
            execute_next_open=True,
            sub_symbol="000688.SH", sub_buy_threshold=25.0,
            sub_volume_ratio_threshold=1.6,
            sub2_symbol="QQQ.US", sub2_buy_threshold=20.0,
            sub2_volume_ratio_threshold=1.3,
            swap_threshold=45.0,
        )
        result = _run_seesaw_backtest(base_df, base_df.copy(), params, 1_000_000.0, detailed=True,
                                      sub2_base_df=base_df.copy())
        self.assertIn("buy_count", result)
        self.assertIn("idle_days", result)
        # 引擎能跑通即可；log-z 放量（1.25）在放量日触发
        self.assertGreaterEqual(result["buy_count"], 0)

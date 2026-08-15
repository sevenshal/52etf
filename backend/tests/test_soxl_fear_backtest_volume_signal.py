from unittest import TestCase
from unittest.mock import patch

import pandas as pd

from src.app.api.soxl_fear_backtest import (
    SOXLFearStrategyParams,
    _prepare_base_dataframe,
    _run_backtest,
    _run_seesaw_backtest,
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

    def test_trailing_stop_zero_sells_immediately_when_greedy(self):
        """移动止盈=0：贪恐到达阈值当天即卖出，不再等回撤。"""
        dates = pd.bdate_range("2024-01-01", periods=3).date
        base_df = pd.DataFrame(
            {
                "date": dates,
                "open": [100.0, 102.0, 103.0],
                "high": [101.0, 102.0, 103.0],
                "low": [99.0, 100.0, 102.0],
                "close": [100.0, 101.0, 103.0],
                "execution_price": [100.0, 101.0, 103.0],
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

        params = SOXLFearStrategyParams(
            buy_threshold=40.0,
            greed_threshold=80.0,
            volume_ratio_threshold=1.5,
            volume_ratio_consecutive_days=1,
            buy_position_pct=100.0,
            cooldown_days=0,
            trailing_stop_pct=0.0,  # 到达贪恐即卖
            sell_position_pct=100.0,
            sell_reduction_basis="holdings",
            sell_price_above_avg_cost=True,
            min_position_pct_after_take_profit=0.0,
            rebalance_threshold_pct=0.0,
        )
        result = _run_backtest(base_df, params, 10000.0, detailed=True)

        sells = [item for item in result["trades"] if item["action"] == "SELL"]
        self.assertEqual(1, len(sells))
        # 贪恐 90 >= 80 的当天（第 2 个交易日）即卖出，没有等待回撤
        self.assertEqual(dates[1].isoformat(), sells[0]["date"])
        self.assertAlmostEqual(101.0, sells[0]["execution_price"])
        self.assertIn("到达贪恐阈值即卖", sells[0]["reason"])

    def test_trailing_stop_positive_waits_for_drawdown_even_when_greedy(self):
        """对照组：同样的数据，移动止盈>0 时不满足回撤条件则不卖。"""
        dates = pd.bdate_range("2024-01-01", periods=3).date
        base_df = pd.DataFrame(
            {
                "date": dates,
                "open": [100.0, 102.0, 103.0],
                "high": [101.0, 102.0, 103.0],
                "low": [99.0, 100.0, 102.0],
                "close": [100.0, 101.0, 103.0],
                "execution_price": [100.0, 101.0, 103.0],
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

        params = SOXLFearStrategyParams(
            buy_threshold=40.0,
            greed_threshold=80.0,
            volume_ratio_threshold=1.5,
            volume_ratio_consecutive_days=1,
            buy_position_pct=100.0,
            cooldown_days=0,
            trailing_stop_pct=5.0,
            sell_position_pct=100.0,
            sell_reduction_basis="holdings",
            sell_price_above_avg_cost=True,
            min_position_pct_after_take_profit=0.0,
            rebalance_threshold_pct=0.0,
        )
        result = _run_backtest(base_df, params, 10000.0, detailed=True)

        sells = [item for item in result["trades"] if item["action"] == "SELL"]
        self.assertEqual(0, len(sells))

    def test_execute_next_open_buy_fills_at_next_trading_day_open(self):
        """次日开盘成交：信号日收盘决策，下一交易日开盘价成交，交易日期为成交日。"""
        dates = pd.bdate_range("2024-01-01", periods=3).date
        base_df = pd.DataFrame(
            {
                "date": dates,
                "open": [100.0, 103.0, 104.0],
                "high": [101.0, 105.0, 106.0],
                "low": [99.0, 102.0, 103.0],
                "close": [100.0, 104.0, 105.0],
                "execution_price": [100.0, 104.0, 105.0],
                "volume": [1000.0, 1000.0, 1000.0],
                "ma20": [100.0, 100.0, 100.0],
                "volume_ma20": [100.0, 100.0, 100.0],
                "volume_ratio": [2.0, 1.0, 1.0],
                "volume_ma20_excluding_recent_1": [100.0, 100.0, 100.0],
                "volume_ratio_consecutive_1": [2.0, 1.0, 1.0],
                "fear_greed": [20.0, 60.0, 60.0],
                "signal_volume": [200.0, 100.0, 100.0],
                "signal_date": dates,
                "fear_date": dates,
            }
        )

        params = SOXLFearStrategyParams(
            buy_threshold=40.0,
            greed_threshold=90.0,
            volume_ratio_threshold=1.5,
            volume_ratio_consecutive_days=1,
            buy_position_pct=100.0,
            cooldown_days=0,
            trailing_stop_pct=5.0,
            sell_position_pct=100.0,
            sell_reduction_basis="holdings",
            sell_price_above_avg_cost=True,
            min_position_pct_after_take_profit=0.0,
            rebalance_threshold_pct=0.0,
            execute_next_open=True,
        )
        result = _run_backtest(base_df, params, 10000.0, detailed=True)

        buys = [item for item in result["trades"] if item["action"] == "BUY"]
        self.assertEqual(1, len(buys))
        # 信号在第 1 个交易日收盘形成（fear=20），成交在第 2 个交易日开盘价 103
        self.assertEqual(dates[1].isoformat(), buys[0]["date"])
        self.assertEqual(dates[0].isoformat(), buys[0]["signal_date"])
        self.assertAlmostEqual(103.0, buys[0]["execution_price"])
        self.assertEqual("next_day_open", result["execution_price_type"])

    def test_execute_next_open_sell_fills_at_next_trading_day_open(self):
        """次日开盘成交 + 移动止盈=0：贪恐到达阈值次日开盘卖出。"""
        dates = pd.bdate_range("2024-01-01", periods=4).date
        base_df = pd.DataFrame(
            {
                "date": dates,
                "open": [100.0, 103.0, 104.0, 106.0],
                "high": [101.0, 105.0, 106.0, 108.0],
                "low": [99.0, 102.0, 103.0, 105.0],
                "close": [100.0, 104.0, 105.0, 107.0],
                "execution_price": [100.0, 104.0, 105.0, 107.0],
                "volume": [1000.0, 1000.0, 1000.0, 1000.0],
                "ma20": [100.0, 100.0, 100.0, 100.0],
                "volume_ma20": [100.0, 100.0, 100.0, 100.0],
                "volume_ratio": [2.0, 1.0, 1.0, 1.0],
                "volume_ma20_excluding_recent_1": [100.0, 100.0, 100.0, 100.0],
                "volume_ratio_consecutive_1": [2.0, 1.0, 1.0, 1.0],
                "fear_greed": [20.0, 85.0, 85.0, 60.0],
                "signal_volume": [200.0, 100.0, 100.0, 100.0],
                "signal_date": dates,
                "fear_date": dates,
            }
        )

        params = SOXLFearStrategyParams(
            buy_threshold=40.0,
            greed_threshold=80.0,
            volume_ratio_threshold=1.5,
            volume_ratio_consecutive_days=1,
            buy_position_pct=100.0,
            cooldown_days=0,
            trailing_stop_pct=0.0,
            sell_position_pct=100.0,
            sell_reduction_basis="holdings",
            sell_price_above_avg_cost=True,
            min_position_pct_after_take_profit=0.0,
            rebalance_threshold_pct=0.0,
            execute_next_open=True,
        )
        result = _run_backtest(base_df, params, 10000.0, detailed=True)

        buys = [item for item in result["trades"] if item["action"] == "BUY"]
        sells = [item for item in result["trades"] if item["action"] == "SELL"]
        self.assertEqual(1, len(buys))
        self.assertEqual(1, len(sells))
        # 第 2 个交易日贪恐 85 >= 80 → 第 3 个交易日开盘价 104 卖出
        self.assertEqual(dates[2].isoformat(), sells[0]["date"])
        self.assertEqual(dates[1].isoformat(), sells[0]["signal_date"])
        self.assertAlmostEqual(104.0, sells[0]["execution_price"])
        self.assertIn("到达贪恐阈值即卖", sells[0]["reason"])

    def test_execute_next_open_default_keeps_same_day_close(self):
        """默认 execute_next_open=False：维持信号日收盘价成交。"""
        dates = pd.bdate_range("2024-01-01", periods=3).date
        base_df = pd.DataFrame(
            {
                "date": dates,
                "open": [100.0, 103.0, 104.0],
                "high": [101.0, 105.0, 106.0],
                "low": [99.0, 102.0, 103.0],
                "close": [100.0, 104.0, 105.0],
                "execution_price": [100.0, 104.0, 105.0],
                "volume": [1000.0, 1000.0, 1000.0],
                "ma20": [100.0, 100.0, 100.0],
                "volume_ma20": [100.0, 100.0, 100.0],
                "volume_ratio": [2.0, 1.0, 1.0],
                "volume_ma20_excluding_recent_1": [100.0, 100.0, 100.0],
                "volume_ratio_consecutive_1": [2.0, 1.0, 1.0],
                "fear_greed": [20.0, 60.0, 60.0],
                "signal_volume": [200.0, 100.0, 100.0],
                "signal_date": dates,
                "fear_date": dates,
            }
        )
        params = SOXLFearStrategyParams(
            buy_threshold=40.0,
            greed_threshold=90.0,
            volume_ratio_threshold=1.5,
            volume_ratio_consecutive_days=1,
            buy_position_pct=100.0,
            cooldown_days=0,
            trailing_stop_pct=5.0,
            sell_position_pct=100.0,
            sell_reduction_basis="holdings",
            sell_price_above_avg_cost=True,
            min_position_pct_after_take_profit=0.0,
            rebalance_threshold_pct=0.0,
        )
        result = _run_backtest(base_df, params, 10000.0, detailed=True)

        buys = [item for item in result["trades"] if item["action"] == "BUY"]
        self.assertEqual(1, len(buys))
        self.assertEqual(dates[0].isoformat(), buys[0]["date"])
        self.assertEqual(dates[0].isoformat(), buys[0]["signal_date"])
        self.assertAlmostEqual(100.0, buys[0]["execution_price"])
        self.assertEqual("same_day_close", result["execution_price_type"])

    def _seesaw_frames(self):
        """主标的无信号 + 候补恐慌放量（次开成交）"""
        dates = pd.bdate_range("2024-01-01", periods=4).date

        def frame(fear, vr, attrs):
            df = pd.DataFrame({
                "date": dates, "open": [100.0, 101.0, 102.0, 103.0],
                "high": [101.0, 102.0, 103.0, 104.0],
                "low": [99.0, 100.0, 101.0, 102.0],
                "close": [100.5, 101.5, 102.5, 103.5],
                "execution_price": [100.0, 101.0, 102.0, 103.0],
                "volume": [1000.0] * 4, "ma20": [100.0] * 4, "volume_ma20": [100.0] * 4,
                "volume_ratio": vr, "volume_ma20_excluding_recent_1": [100.0] * 4,
                "volume_ratio_consecutive_1": vr, "fear_greed": fear,
                "signal_volume": [100.0] * 4, "signal_date": dates, "fear_date": dates,
            })
            for key, value in attrs.items():
                df.attrs[key] = value
            return df

        main_df = frame([45.0, 45.0, 45.0, 45.0], [1.0, 1.0, 1.0, 1.0], {"symbol": "510880.SH", "fear_source_label": "上证红利"})
        sub_df = frame([20.0, 60.0, 60.0, 60.0], [2.0, 1.0, 1.0, 1.0], {"symbol": "588000.SH", "fear_source_label": "科创50"})
        return main_df, sub_df, dates

    def _seesaw_params(self, **overrides):
        base = dict(
            buy_threshold=40.0, greed_threshold=80.0, volume_ratio_threshold=1.5,
            volume_ratio_consecutive_days=1, buy_position_pct=100.0, cooldown_days=0,
            trailing_stop_pct=0.0, sell_position_pct=100.0, sell_reduction_basis="holdings",
            sell_price_above_avg_cost=True, max_take_profit_sells_per_cycle=2,
            min_position_pct_after_take_profit=0.0, rebalance_threshold_pct=0.0,
            execute_next_open=True,
            sub_symbol="588000.SH", sub_fear_source="a_stock_000688_sh",
            sub_buy_threshold=25.0, sub_volume_ratio_threshold=1.5,
        )
        base.update(overrides)
        return SOXLFearStrategyParams(**base)

    def test_seesaw_buys_sub_when_main_flat(self):
        """跷跷板：主标的无信号、空仓时，候补极恐放量 → 买入候补"""
        main_df, sub_df, dates = self._seesaw_frames()
        result = _run_seesaw_backtest(main_df, sub_df, self._seesaw_params(), 10000.0, detailed=True)
        buys = [item for item in result["trades"] if item["action"] == "BUY"]
        self.assertEqual(1, len(buys))
        self.assertEqual("588000.SH", buys[0]["symbol"])
        # 信号日 day0（候补 fear=20 < 25, 量比 2.0）→ 次日 day1 开盘成交
        self.assertEqual(dates[1].isoformat(), buys[0]["date"])
        self.assertAlmostEqual(101.0, buys[0]["price"])

    def test_seesaw_main_signal_switches_back_from_sub(self):
        """跷跷板：持有候补时主标的出信号 → 卖出候补换回主标的"""
        main_df, sub_df, dates = self._seesaw_frames()
        # 主标的 day1 出信号（fear=20 量比=2.0）
        main_df.loc[1, "fear_greed"] = 20.0
        main_df.loc[1, "volume_ratio"] = 2.0
        main_df.loc[1, "volume_ratio_consecutive_1"] = 2.0
        result = _run_seesaw_backtest(main_df, sub_df, self._seesaw_params(), 10000.0, detailed=True)
        actions = [(t["date"], t["action"], t["symbol"]) for t in result["trades"]]
        # day1(信号day0): 候补信号买入候补；day2(信号day1): 主信号 → 卖候补买主
        self.assertEqual(dates[1].isoformat(), actions[0][0])
        self.assertEqual("BUY", actions[0][1])
        self.assertEqual("588000.SH", actions[0][2])
        self.assertEqual(dates[2].isoformat(), actions[1][0])
        self.assertEqual("SELL", actions[1][1])
        self.assertEqual("588000.SH", actions[1][2])
        self.assertEqual(dates[2].isoformat(), actions[2][0])
        self.assertEqual("BUY", actions[2][1])
        self.assertEqual("510880.SH", actions[2][2])

    def test_seesaw_sub_greedy_sells_to_flat(self):
        """跷跷板：持有候补且候补到达贪恐阈值 → 卖出保持空仓"""
        main_df, sub_df, dates = self._seesaw_frames()
        # 候补 day1 贪恐 >= 80 → 信号 day1 → day2 卖出
        sub_df.loc[1, "fear_greed"] = 90.0
        result = _run_seesaw_backtest(main_df, sub_df, self._seesaw_params(), 10000.0, detailed=True)
        sells = [item for item in result["trades"] if item["action"] == "SELL"]
        self.assertEqual(1, len(sells))
        self.assertEqual("588000.SH", sells[0]["symbol"])
        self.assertEqual(dates[2].isoformat(), sells[0]["date"])

    def test_seesaw_daily_data_includes_sub_kline_fields(self):
        """跷跷板 daily_data 输出候补 OHLCV 字段供前端叠加展示"""
        main_df, sub_df, dates = self._seesaw_frames()
        result = _run_seesaw_backtest(main_df, sub_df, self._seesaw_params(), 10000.0, detailed=True)
        daily = result["daily_data"]
        self.assertGreaterEqual(len(daily), 1)
        first = daily[0]
        self.assertEqual("588000.SH", first["sub_symbol"])
        self.assertIsNotNone(first["sub_open"])
        self.assertIsNotNone(first["sub_high"])
        self.assertIsNotNone(first["sub_low"])
        self.assertIsNotNone(first["sub_close"])
        self.assertIsNotNone(first["sub_volume"])

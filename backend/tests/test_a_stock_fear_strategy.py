from datetime import date
from types import SimpleNamespace
from unittest import TestCase

import pandas as pd

from src.app.api.a_stock_fear_strategy import AStockFearStrategyConfigPayload
from src.robot.a_stock_fear_strategy_trader import (
    AStockFearStrategyTrader,
    _fear_source_index_symbol,
    _fear_source_label,
)


class AStockFearStrategyTraderTest(TestCase):
    def setUp(self):
        self.trader = AStockFearStrategyTrader()

    def test_fear_source_key_to_index_symbol(self):
        self.assertEqual("000015.SH", _fear_source_index_symbol("a_stock_000015_sh"))
        self.assertEqual("H30269.CSI", _fear_source_index_symbol("a_stock_h30269_csi"))
        self.assertEqual("INNO100.CN", _fear_source_index_symbol("a_stock_inno100_cn"))
        self.assertIsNone(_fear_source_index_symbol("cnn"))
        self.assertIsNone(_fear_source_index_symbol(None))

    def test_fear_source_label(self):
        self.assertIn("上证红利", _fear_source_label("a_stock_000015_sh"))
        self.assertIn("红利低波", _fear_source_label("a_stock_h30269_csi"))

    def test_prev_china_trading_day_skips_weekends(self):
        # 2026-08-15 是周六 → 上一交易日 2026-08-14（周五）
        self.assertEqual(date(2026, 8, 14), self.trader._prev_china_trading_day(date(2026, 8, 15)))
        # 2026-08-14（周五）→ 2026-08-13（周四）
        self.assertEqual(date(2026, 8, 13), self.trader._prev_china_trading_day(date(2026, 8, 14)))

    def test_parse_run_time(self):
        self.assertEqual("09:30", self.trader._parse_run_time("09:30").strftime("%H:%M"))
        self.assertEqual("09:30", self.trader._parse_run_time("bad").strftime("%H:%M"))
        self.assertEqual("14:58", self.trader._parse_run_time("14:58").strftime("%H:%M"))

    def test_volume_ratio_at_matches_backtest_shift_one_semantics(self):
        # 前 20 日均量（shift(1)）作为基准，与回测 prepare_market_features 一致
        dates = pd.bdate_range("2024-01-01", periods=25).date
        volumes = [100.0] * 25
        volumes[23] = 200.0  # 第 24 个交易日放量
        bars = pd.DataFrame({
            "trade_date": dates,
            "open": [10.0] * 25,
            "high": [12.0] * 25,
            "low": [9.0] * 25,
            "close": [11.0] * 25,
            "volume": volumes,
        })
        signal_date = dates[23]
        ratio = self.trader._volume_ratio_at(bars, signal_date)
        # 前 20 日均量 = 100 → 量比 = 2.0
        self.assertAlmostEqual(2.0, ratio)
        # 历史不足 20 日时返回 None
        self.assertIsNone(self.trader._volume_ratio_at(bars, dates[10]))

    def test_config_payload_defaults_match_backtest_style_params(self):
        payload = AStockFearStrategyConfigPayload()
        self.assertEqual("510880.SH", payload.symbol)
        self.assertEqual("a_stock_000015_sh", payload.fear_source)
        self.assertEqual("09:30", payload.run_time)
        self.assertEqual(30.0, payload.buy_threshold)
        self.assertEqual(70.0, payload.greed_threshold)
        self.assertEqual(1.3, payload.volume_ratio_threshold)
        self.assertEqual(100.0, payload.buy_position_pct)
        self.assertEqual(100.0, payload.sell_position_pct)
        self.assertEqual(0.0, payload.trailing_stop_pct)
        self.assertEqual(0, payload.cooldown_days)

    def test_config_payload_rejects_non_a_stock_symbol(self):
        with self.assertRaises(Exception):
            AStockFearStrategyConfigPayload(symbol="SOXL.US")

    def test_config_payload_rejects_non_a_stock_fear_source(self):
        with self.assertRaises(Exception):
            AStockFearStrategyConfigPayload(fear_source="cnn")

    def test_config_payload_validates_run_time_format(self):
        with self.assertRaises(Exception):
            AStockFearStrategyConfigPayload(run_time="9:30am")

    def test_config_payload_trailing_stop_zero_allowed(self):
        payload = AStockFearStrategyConfigPayload(trailing_stop_pct=0.0)
        self.assertEqual(0.0, payload.trailing_stop_pct)
        payload2 = AStockFearStrategyConfigPayload(trailing_stop_pct=7.0)
        self.assertEqual(7.0, payload2.trailing_stop_pct)
        with self.assertRaises(Exception):
            AStockFearStrategyConfigPayload(trailing_stop_pct=-1.0)
        with self.assertRaises(Exception):
            AStockFearStrategyConfigPayload(trailing_stop_pct=101.0)

    def test_position_shares_for_symbol_distinguishes_sub2(self):
        """下单持仓判断按 symbol 区分主/sub/sub2，换仓到第二候补不误读 sub 持仓。"""
        trader = AStockFearStrategyTrader()
        config = SimpleNamespace(symbol="510880.SH", sub_symbol="588000.SH", sub2_symbol="159941.SZ")
        snapshot = SimpleNamespace(shares=111, sub_shares=222, sub2_shares=333)
        self.assertEqual(111, trader._position_shares_for_symbol(snapshot, "510880.SH", config))
        self.assertEqual(222, trader._position_shares_for_symbol(snapshot, "588000.SH", config))
        # 修复前会把 159941.SZ 误读成 sub(588000) 的 222
        self.assertEqual(333, trader._position_shares_for_symbol(snapshot, "159941.SZ", config))

    def test_position_shares_for_symbol_no_sub2_falls_back_to_sub(self):
        """无 sub2 时非主标的走 sub 持仓。"""
        trader = AStockFearStrategyTrader()
        config = SimpleNamespace(symbol="510880.SH", sub_symbol="588000.SH", sub2_symbol=None)
        snapshot = SimpleNamespace(shares=111, sub_shares=222, sub2_shares=333)
        self.assertEqual(222, trader._position_shares_for_symbol(snapshot, "588000.SH", config))

    def test_config_payload_allows_us_volume_source_symbol(self):
        """量比来源允许美股标的（QQQ.US），支持三标的中纳指用 QQQ 成交量。"""
        payload = AStockFearStrategyConfigPayload(
            symbol="510880.SH",
            fear_source="a_stock_000015_sh",
            sub_symbol="588000.SH",
            sub_fear_source="a_stock_000688_sh",
            sub2_symbol="159941.SZ",
            sub2_fear_source="qqq_clone",
            sub2_volume_signal_symbol="QQQ.US",
        )
        self.assertEqual("QQQ.US", payload.sub2_volume_signal_symbol)
        # 非法量比来源仍拒绝
        with self.assertRaises(Exception):
            AStockFearStrategyConfigPayload(volume_signal_symbol="BOGUS.XY")

"""CNN 自算恐贪摘要：美股/港股盘中实时附加与失败降级。"""
import asyncio
from unittest import TestCase
from unittest.mock import patch

from src.app.api import cnn


def _summary_item(symbol):
    return {
        "symbol": symbol,
        "latest": {
            "date": "2026-08-13",
            "score": 50.0,
            "etf_price": {"close": 100.0},
        },
    }


def _realtime_payload(symbol, score):
    return {
        "fear_and_greed_clone": {
            "symbol": symbol,
            "score": score,
            "rating": "中性",
            "date": "2026-08-14",
            "timestamp": "2026-08-14T21:00:00",
            "market_open": False,
        },
        "etf_price": {
            "close": 251.3,
            "quote": {"price": 251.3, "timestamp": "2026-08-14T21:00:00", "source": "longport"},
        },
        "warnings": ["realtime price-driven"],
    }


class CnnFearGreedSummariesRealtimeTest(TestCase):
    def test_serialize_realtime_snapshot_maps_fields(self):
        snapshot = cnn._serialize_realtime_snapshot(_realtime_payload("SOXX.US", 62.5))
        self.assertEqual(62.5, snapshot["score"])
        self.assertEqual("SOXX.US", snapshot["symbol"])
        self.assertEqual(251.3, snapshot["index_level"])
        self.assertEqual("realtime", snapshot["mode"])
        self.assertEqual("2026-08-14", snapshot["trade_date"])
        self.assertEqual("2026-08-14T21:00:00", snapshot["snapshot_time"])
        self.assertFalse(snapshot["market_open"])

    def test_serialize_realtime_snapshot_none_without_score(self):
        self.assertIsNone(cnn._serialize_realtime_snapshot({}))
        self.assertIsNone(cnn._serialize_realtime_snapshot({"fear_and_greed_clone": {"score": None}}))

    def test_summaries_attaches_us_realtime_and_skips_failures(self):
        async def run():
            with patch.object(cnn, "ETFFearGreedCloneCalculator") as mock_calc_cls, \
                 patch.object(cnn, "_load_intraday_snapshot_map", return_value={"000300.SH": {"score": 45.0}}), \
                 patch.object(cnn, "load_a_stock_index_valuation", return_value={"status": "available"}):
                calculator = mock_calc_cls.return_value
                calculator.load_summaries_from_db.return_value = {
                    "data": [
                        _summary_item("SOXX.US"),
                        _summary_item("HSI.HK"),
                        _summary_item("000300.SH"),
                    ]
                }

                def fake_realtime(symbol, **kwargs):
                    # 只有港股会请求实时版；美股已去掉
                    if symbol == "HSI.HK":
                        return _realtime_payload(symbol, 58.0)
                    raise AssertionError(f"美股 {symbol} 不应计算实时贪恐")

                calculator.calculate_realtime_cached.side_effect = fake_realtime
                result = await cnn.get_etf_fear_greed_clone_summaries(
                    symbols="SOXX.US,HSI.HK,000300.SH"
                )

            by_symbol = {item["symbol"]: item for item in result["data"]}
            # 美股：不叠加盘中，仍显示收盘
            self.assertNotIn("intraday", by_symbol["SOXX.US"])
            self.assertEqual(50.0, by_symbol["SOXX.US"]["latest"]["score"])
            # 港股：轻量实时
            self.assertEqual(58.0, by_symbol["HSI.HK"]["intraday"]["score"])
            self.assertEqual("realtime", by_symbol["HSI.HK"]["intraday"]["mode"])
            # A股：仍走盘中快照 + 估值
            self.assertEqual(45.0, by_symbol["000300.SH"]["intraday"]["score"])
            self.assertIn("valuation", by_symbol["000300.SH"])
            # 实时计算调用参数：不含持仓行情，也不批量拉成分股实时价（轻量模式）
            _, kwargs = calculator.calculate_realtime_cached.call_args
            self.assertFalse(kwargs.get("include_holdings_quotes"))
            self.assertFalse(kwargs.get("fetch_holdings_quotes"))

        asyncio.run(run())

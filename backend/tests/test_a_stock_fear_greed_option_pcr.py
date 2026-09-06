"""put/call 分项的期权成交量口径。

中证全指走全市场口径（不过滤 opt_code），其余指数只统计自己配置的期权标的。
"""
from datetime import date, datetime
from unittest import TestCase

from src.core.analytics_database import (
    AStockOptionBasic,
    AStockOptionDaily,
    AnalyticsSession,
)
from src.core.services.a_stock_fear_greed_clone_service import (
    ALL_A_STOCK_OPTIONS,
    AStockInnovation100FearGreedCloneCalculator,
)


TRADE_DATE = date(2026, 9, 1)
# (合约, 标的, C/P, 成交量)
CONTRACTS = [
    ("MO2609-C-7800.CFX", "OP000852.SH", "C", 100.0),
    ("MO2609-P-7800.CFX", "OP000852.SH", "P", 50.0),
    ("10007000.SH", "OP510300.SH", "C", 400.0),
    ("10007001.SH", "OP510300.SH", "P", 200.0),
    ("90000123.SZ", "OP159915.SZ", "C", 500.0),
    ("90000124.SZ", "OP159915.SZ", "P", 750.0),
]


class OptionVolumePcrScopeTest(TestCase):
    def setUp(self):
        analytics_db = AnalyticsSession()
        try:
            analytics_db.query(AStockOptionDaily).delete(synchronize_session=False)
            analytics_db.query(AStockOptionBasic).delete(synchronize_session=False)
            now = datetime.now()
            for ts_code, opt_code, call_put, vol in CONTRACTS:
                analytics_db.add(AStockOptionBasic(
                    ts_code=ts_code,
                    opt_code=opt_code,
                    call_put=call_put,
                    name=ts_code,
                    updated_at=now,
                ))
                analytics_db.add(AStockOptionDaily(
                    trade_date=TRADE_DATE,
                    ts_code=ts_code,
                    vol=vol,
                    created_at=now,
                    updated_at=now,
                ))
            analytics_db.commit()
        finally:
            AnalyticsSession.remove()
        self.addCleanup(self._clear)

    def _clear(self):
        analytics_db = AnalyticsSession()
        try:
            analytics_db.query(AStockOptionDaily).delete(synchronize_session=False)
            analytics_db.query(AStockOptionBasic).delete(synchronize_session=False)
            analytics_db.commit()
        finally:
            AnalyticsSession.remove()

    def _pcr(self, symbol):
        series = AStockInnovation100FearGreedCloneCalculator(symbol)._load_option_volume_pcr(
            TRADE_DATE, TRADE_DATE
        )
        return float(series.iloc[0])

    def test_csi_all_share_aggregates_every_option_by_volume(self):
        """中证全指不过滤标的：(50+200+750) / (100+400+500)。"""
        calculator = AStockInnovation100FearGreedCloneCalculator("000985.SH")
        self.assertEqual(calculator.option_underlyings, (ALL_A_STOCK_OPTIONS,))
        self.assertAlmostEqual(self._pcr("000985.SH"), 1000.0 / 1000.0, places=9)

    def test_narrow_index_only_counts_its_own_option_underlying(self):
        """中证1000 只统计 OP000852.SH：50 / 100。"""
        self.assertAlmostEqual(self._pcr("000852.SH"), 50.0 / 100.0, places=9)

    def test_index_without_options_returns_empty_series(self):
        """行业指数没有配期权，直接返回空序列，put_call 分项缺失。"""
        series = AStockInnovation100FearGreedCloneCalculator(
            "399986.SZ"
        )._load_option_volume_pcr(TRADE_DATE, TRADE_DATE)
        self.assertTrue(series.empty)

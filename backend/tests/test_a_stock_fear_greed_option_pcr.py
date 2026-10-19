"""put/call 分项的期权成交量口径。

中证全指走全市场口径（不过滤 opt_code），其余指数只统计自己配置的期权标的。
"""
from datetime import date, datetime

import pandas as pd
from unittest import TestCase

from src.core.analytics_database import (
    AStockIndexWeight,
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
    ("10009000.SH", "OP510500.SH", "C", 500.0),
    ("10009001.SH", "OP510500.SH", "P", 750.0),
    ("90000123.SZ", "OP159915.SZ", "C", 500.0),
    ("90000124.SZ", "OP159915.SZ", "P", 750.0),
]
# 代理指数的成分快照：600000 在沪深300、000002 在中证500。
INDEX_MEMBERS = [
    ("000300.SH", "600000.SH", 100.0),
    ("000905.SH", "000002.SZ", 100.0),
]


class OptionVolumePcrScopeTest(TestCase):
    def setUp(self):
        analytics_db = AnalyticsSession()
        try:
            analytics_db.query(AStockOptionDaily).delete(synchronize_session=False)
            analytics_db.query(AStockOptionBasic).delete(synchronize_session=False)
            analytics_db.query(AStockIndexWeight).delete(synchronize_session=False)
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
            for index_code, con_code, weight in INDEX_MEMBERS:
                analytics_db.add(AStockIndexWeight(
                    index_code=index_code, trade_date=TRADE_DATE, con_code=con_code,
                    weight=weight, created_at=now, updated_at=now,
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
            analytics_db.query(AStockIndexWeight).delete(synchronize_session=False)
            analytics_db.commit()
        finally:
            AnalyticsSession.remove()

    def _pcr(self, symbol):
        series = AStockInnovation100FearGreedCloneCalculator(symbol)._load_option_volume_pcr(
            TRADE_DATE, TRADE_DATE
        )
        return float(series.iloc[0])

    def test_csi_all_share_aggregates_every_option_by_volume(self):
        """中证全指不过滤标的、也不加权：全市场 put 量除以全市场 call 量。"""
        calculator = AStockInnovation100FearGreedCloneCalculator("000985.SH")
        self.assertEqual(calculator.option_underlyings, (ALL_A_STOCK_OPTIONS,))
        puts = sum(vol for _, _, cp, vol in CONTRACTS if cp == "P")
        calls = sum(vol for _, _, cp, vol in CONTRACTS if cp == "C")
        self.assertAlmostEqual(self._pcr("000985.SH"), puts / calls, places=9)

    def test_narrow_index_only_counts_its_own_option_underlying(self):
        """中证1000 只统计 OP000852.SH：50 / 100。"""
        self.assertAlmostEqual(self._pcr("000852.SH"), 50.0 / 100.0, places=9)

    def _proxied_pcr(self, symbol, holdings):
        """代理指数的 PCR：按成分重叠度加权，而不是把成交量一股脑加总。"""
        timestamp = pd.Timestamp(TRADE_DATE)
        calculator = AStockInnovation100FearGreedCloneCalculator(symbol)
        series = calculator._load_option_volume_pcr(
            TRADE_DATE,
            TRADE_DATE,
            {timestamp: holdings},
            {timestamp: TRADE_DATE},
        )
        return series

    def test_proxied_index_blends_by_constituent_overlap_not_volume(self):
        """成分 80% 在沪深300、20% 在中证500 时，混合结果要贴近 80/20。

        成交量加总口径下 300ETF 只有 400+200=600 张、500ETF 有 500+750=1250 张，
        中证500 的权重会翻倍压过沪深300，方向正好和成分结构相反。
        """
        holdings = [
            {"symbol": "600000.SH", "weight": 0.8},   # 只在沪深300 里
            {"symbol": "000002.SZ", "weight": 0.2},   # 只在中证500 里
        ]
        blended = float(self._proxied_pcr("399986.SZ", holdings).iloc[0])
        pcr_300 = 200.0 / 400.0
        pcr_500 = 750.0 / 500.0
        self.assertAlmostEqual(blended, 0.8 * pcr_300 + 0.2 * pcr_500, places=9)
        # 成交量加总口径会得到 (200+750)/(400+500)≈1.056，明显偏向中证500。
        self.assertNotAlmostEqual(blended, 950.0 / 900.0, places=3)

    def test_bse_50_uses_the_market_wide_put_call_component(self):
        """北证50与中证全指共享全市场期权口径，不因成分零交集而缺失。"""
        holdings = [{"symbol": "830001.BJ", "weight": 1.0}]
        series = self._proxied_pcr("899050.BJ", holdings)
        puts = sum(vol for _, _, cp, vol in CONTRACTS if cp == "P")
        calls = sum(vol for _, _, cp, vol in CONTRACTS if cp == "C")
        self.assertAlmostEqual(float(series.iloc[0]), puts / calls, places=9)

    def test_missing_holdings_falls_back_to_equal_weight(self):
        """拿不到成分快照时退回等权，至少不会被成交量最大的标的绑架。"""
        blended = float(
            AStockInnovation100FearGreedCloneCalculator("399986.SZ")
            ._load_option_volume_pcr(TRADE_DATE, TRADE_DATE)
            .iloc[0]
        )
        self.assertAlmostEqual(blended, (200.0 / 400.0 + 750.0 / 500.0) / 2, places=9)

    def test_bse_50_uses_market_wide_iv_proxy(self):
        """全市场口径的指数不按成分重叠度筛 IV，应共享同一市场级 IV 序列。"""
        from unittest.mock import patch

        calculator = AStockInnovation100FearGreedCloneCalculator("899050.BJ")
        iv_by_tracked = pd.DataFrame(
            {
                "000300.SH": [0.20, 0.22],
                "000852.SH": [0.30, 0.34],
            },
            index=pd.DatetimeIndex(["2026-09-01", "2026-09-02"]),
        )
        with patch.object(
            calculator, "_load_option_implied_volatility_by_tracked_index",
            return_value=iv_by_tracked,
        ), patch.object(calculator, "_option_proxy_weights") as proxy_weights:
            result = calculator._load_option_implied_volatility(
                date(2026, 9, 1), date(2026, 9, 2), None, None
            )

        self.assertEqual(calculator._implied_volatility_mode, "market")
        self.assertFalse(proxy_weights.called)
        self.assertAlmostEqual(float(result.iloc[0]), 0.25)
        self.assertAlmostEqual(float(result.iloc[1]), 0.28)


class OptionDailyCoverageTest(TestCase):
    """期权日行情的覆盖判定：缺哪个交易所补哪天，不缺就一天都不重拉。"""

    @staticmethod
    def _service(first_listing, exchanges_by_date):
        from src.robot.a_stock_base_data_sync import AStockBaseDataSyncService

        service = object.__new__(AStockBaseDataSyncService)
        service._option_first_listing_by_exchange = lambda: first_listing
        service._existing_option_day_exchanges = lambda start, end: exchanges_by_date
        return service

    # 中金所第一只股指期权 2019-12-23 上市，在那之前只该有沪深两市。
    FIRST_LISTING = {
        "SSE": date(2015, 7, 23),
        "SZSE": date(2019, 12, 23),
        "CFFEX": date(2019, 12, 23),
    }
    DATES = [date(2019, 6, 20), date(2022, 3, 10), date(2026, 9, 4)]

    def test_days_before_an_exchange_listed_are_not_treated_as_missing(self):
        """2019-06 只有上交所有期权，不能因为凑不齐三个交易所就每次都重拉。"""
        service = self._service(self.FIRST_LISTING, {
            date(2019, 6, 20): {"SSE"},
            date(2022, 3, 10): {"SSE", "SZSE", "CFFEX"},
            date(2026, 9, 4): {"SSE", "SZSE", "CFFEX"},
        })
        self.assertEqual(service._option_daily_dates_needing_refresh(self.DATES), [])

    def test_newly_added_exchange_backfills_its_whole_history(self):
        """刚接入中金所、只拉到最近几天时，它上市以来的历史都要判成待补。

        这正是生产上踩到的状态：CFFEX 已进同步范围但只有尾部几天数据，
        用「各交易所最新日期」判断会算出最新是今天，历史永远补不上。
        """
        service = self._service(self.FIRST_LISTING, {
            date(2019, 6, 20): {"SSE"},
            date(2022, 3, 10): {"SSE", "SZSE"},          # 缺 CFFEX
            date(2026, 9, 4): {"SSE", "SZSE", "CFFEX"},  # 尾部已经补上
        })
        self.assertEqual(
            service._option_daily_dates_needing_refresh(self.DATES),
            [date(2022, 3, 10)],
        )

    def test_day_with_no_data_at_all_needs_refresh(self):
        service = self._service(self.FIRST_LISTING, {})
        self.assertEqual(
            service._option_daily_dates_needing_refresh(self.DATES),
            self.DATES,
        )


class OpenInterestPcrComponentTest(TestCase):
    """持仓量口径 P/C：只作诊断，且方向与成交量口径相反。"""

    def test_component_is_registered_as_display_only(self):
        from src.core.services.a_stock_fear_greed_clone_service import (
            A_STOCK_ALL_COMPONENTS,
            A_STOCK_COMPONENTS,
            A_STOCK_DISPLAY_ONLY_COMPONENTS,
        )

        self.assertIn("put_call_oi_options", A_STOCK_ALL_COMPONENTS)
        self.assertIn("put_call_oi_options", A_STOCK_DISPLAY_ONLY_COMPONENTS)
        self.assertNotIn("put_call_oi_options", A_STOCK_COMPONENTS)
        # 原来的成交量口径仍在总分里，两者的 key 不能混用。
        self.assertIn("put_call_options", A_STOCK_COMPONENTS)

    def test_loader_requests_open_interest_column(self):
        from unittest.mock import patch

        calculator = AStockInnovation100FearGreedCloneCalculator("INNO100.CN")
        expected = pd.Series([1.2], index=pd.DatetimeIndex(["2026-09-01"]))
        with patch.object(
            calculator, "_load_option_pcr", return_value=expected
        ) as mocked:
            calculator._load_option_open_interest_pcr(
                date(2026, 8, 1), date(2026, 9, 1)
            )
        self.assertEqual("oi", mocked.call_args.kwargs["value_column"])

    def test_raw_signal_keeps_positive_sign(self):
        """持仓量口径取正号：高 P/C 对应偏贪婪（局部顶部）。"""
        from unittest.mock import patch

        calculator = AStockInnovation100FearGreedCloneCalculator("INNO100.CN")
        index = pd.date_range("2026-08-03", periods=6, freq="B")
        levels = pd.DataFrame(
            {"level": 100.0 + index.month + index.day, "close": 100.0},
            index=index,
        )
        ratio = pd.Series(1.6, index=index)
        with patch.object(calculator, "_load_levels", return_value=levels), \
             patch.object(calculator, "_build_holdings_by_date", return_value=({}, {})), \
             patch.object(
                 calculator, "_load_constituent_market_frames", return_value={}
             ), \
             patch.object(
                 calculator, "_load_index_close",
                 return_value=pd.Series(1.0, index=index),
             ), \
             patch.object(
                 calculator, "_load_option_volume_pcr",
                 return_value=pd.Series(0.9, index=index),
             ), \
             patch.object(
                 calculator, "_load_option_open_interest_pcr",
                 return_value=ratio,
             ), \
             patch.object(
                 calculator, "_load_option_implied_volatility",
                 return_value=pd.Series(0.2, index=index),
             ), \
             patch.object(
                 calculator, "_load_credit_spread",
                 return_value=pd.Series(0.01, index=index),
             ), \
             patch.object(
                 calculator, "_weighted_range_position",
                 return_value=pd.Series(0.5, index=index),
             ), \
             patch.object(
                 calculator, "_weighted_advancing_amount_ratio",
                 return_value=pd.Series(0.5, index=index),
             ):
            raw = calculator._build_raw_signals(
                levels, {}, date(2026, 8, 1), date(2026, 9, 1)
            )

        self.assertAlmostEqual(1.6, float(raw["put_call_oi_options"].iloc[-1]))
        self.assertAlmostEqual(-0.9, float(raw["put_call_options"].iloc[-1]))

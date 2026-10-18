import logging
import unittest
from datetime import date
from unittest.mock import patch

import pandas as pd

from src.core.services import hk_option_service as service
from src.core.services.hk_option_service import (
    HK_FEAR_TARGET_OPTION_PRODUCT,
    HK_OPTION_PRODUCTS,
    HKOptionDailySync,
    OptionDaySummary,
    OptionSeries,
    atm_iv,
    black76_price,
    dtop_atm_iv,
    implied_vol,
    load_option_pcr_series,
    parse_dtop_report,
    parse_market_report,
)
from src.core.services.hk_stock_fear_greed_service import HKStockFearGreedCalculator
from src.robot.hk_stock_base_data_config import HK_INDEX_FEAR_GREED_TARGETS


MARKET_REPORT = """<HTML><HEAD><TITLE>HANG SENG INDEX OPTIONS DAILY MARKET REPORT</TITLE></HEAD><BODY><PRE>
HANG SENG INDEX OPTIONS DAILY MARKET REPORT (Final)
TOP 10 TRADED (BY VOLUME)    VOLUME    O.Q.P.  IV%     DAILY     DAILY      OPEN    O.Q.P.
C OCT-26      24000            1919       112   15       196       100       993        +3

OCT-26  23400 C        0       0       0       0       0  |     950     990     845     830      +59   17        909  |      2515       575        909       260      -169
OCT-26  23800 C        0       0       0       0       0  |       0       0       0     508      +37   16        117  |       922       334        117       229      -394
OCT-26  24000 C        0       0       0       0       0  |     485     540     345     372      +26   15        117  |      2253       247        141       492       -30
OCT-26  24000 P        0       0       0       0       0  |     375     406     240     262      +17   14        200  |      1336       168        194       375       -39

                              TOTAL PUT                0  |                               TOTAL PUT              200  | TOTAL PUT                  200       569       -39

                                                                                                                      | MARKET PUT/CALL RATIO     0.17
                                                                                                                      | MARKET TOTAL             1361      1356     +18
</PRE></BODY></HTML>
"""

DTOP_REPORT = """DTOP                                                       HKEX - DCASS
REPORT DATE     : 16OCT26
CLEARINGHOUSE   : HKCC

MARKET          : HSI   - HANG SENG FUTURES & OPTIONS
UNDERLYING      : HSI
EXPIRATION DATE : 29 OCT 26
 STRIKE    GROSS    NET     CHANGE    T/O     DEAL    PRICE    CHANGE    GROSS     NET     CHANGE     T/O    DEAL     PRICE    CHANGE
-------- -------- -------- --------- ------- ------- --------- -------- -------- -------- --------- ------- ------- --------- --------
  23,800        0        0         0       0       0     5,956       93      613      577        70      74      10         1        0
  24,000      120       90         0       6       2       500       93      613      577        70      74      10       480        1
MARKET          : HHI   - HSCEI FUTURES & OPTIONS
UNDERLYING      : HHI
EXPIRATION DATE : 29 OCT 26
  9,000      100       80         0       4       1     1,200       10      200      180        40      30     300         1        0
"""


class MarketReportParseTest(unittest.TestCase):
    def test_parses_totals_pcr_and_iv(self):
        summary = parse_market_report(MARKET_REPORT, "hsio")

        self.assertIsNotNone(summary)
        # 行内成交量：call 909+117+141=1167，put 194；与页脚 MARKET TOTAL 一致
        self.assertEqual(1167, summary.call_volume)
        self.assertEqual(194, summary.put_volume)
        self.assertAlmostEqual(194 / 1167, summary.put_call_volume_ratio, places=6)
        self.assertAlmostEqual(0.17, summary.reported_put_call_ratio)
        self.assertEqual(260 + 229 + 492, summary.call_open_interest)
        atm, strike, month, source = atm_iv(summary, spot=24000.0, trade_date=date(2026, 10, 16))
        self.assertEqual("OCT-26", month)
        self.assertEqual(24000.0, strike)
        self.assertEqual("report", source)
        self.assertAlmostEqual(14.5, atm, places=3)

    def test_missing_report_returns_none(self):
        self.assertIsNone(parse_market_report("", "hsio"))
        self.assertIsNone(parse_market_report("<HTML>no rows</HTML>", "hsio"))


class ImpliedVolTest(unittest.TestCase):
    def test_round_trip(self):
        spot, strike, years = 24000.0, 24000.0, 30 / 365
        price = black76_price(spot, strike, years, 0.25, True)
        self.assertAlmostEqual(0.25, implied_vol(price, spot, strike, years, True), places=4)
        put_price = black76_price(spot, strike, years, 0.25, False)
        self.assertAlmostEqual(0.25, implied_vol(put_price, spot, strike, years, False), places=4)

    def test_price_below_intrinsic_is_rejected(self):
        self.assertIsNone(implied_vol(5.0, 24000.0, 23000.0, 30 / 365, True))
        self.assertIsNone(implied_vol(None, 24000.0, 23000.0, 30 / 365, True))

    def test_atm_iv_inverts_settle_when_report_has_no_iv(self):
        summary = OptionDaySummary(
            product="hsio",
            trade_date=date(2026, 10, 16),
            call_volume=10,
            put_volume=10,
            call_open_interest=100,
            put_open_interest=100,
            series=[
                OptionSeries("OCT-26", 24000.0, "C", volume=10, open_interest=100, iv=0, settle=480.0),
                OptionSeries("OCT-26", 24000.0, "P", volume=10, open_interest=100, iv=0, settle=470.0),
                OptionSeries("OCT-26", 24600.0, "C", volume=1, open_interest=10, iv=0, settle=100.0),
            ],
        )

        value, strike, month, source = atm_iv(summary, spot=24000.0, trade_date=date(2026, 10, 16))

        self.assertEqual("inverted", source)
        self.assertEqual(24000.0, strike)
        self.assertEqual("OCT-26", month)
        # 13 天到期、平值 480 点大约对应 26% 上下的年化波动率
        self.assertTrue(15 < value < 40, value)


class DtopParseTest(unittest.TestCase):
    def test_splits_underlyings_and_volumes(self):
        buckets = parse_dtop_report(DTOP_REPORT)

        self.assertEqual({120, 100}, {buckets["HSI"]["call_open_interest"], buckets["HHI"]["call_open_interest"]})
        self.assertEqual(6, buckets["HSI"]["call_volume"])
        self.assertEqual(148, buckets["HSI"]["put_volume"])
        self.assertEqual(30, buckets["HHI"]["put_volume"])
        self.assertEqual(2, len(buckets["HSI"]["series"]))

    def test_atm_iv_inverts_dtop_settle(self):
        buckets = parse_dtop_report(DTOP_REPORT)

        value, strike, source = dtop_atm_iv(buckets["HSI"], 24000.0, date(2026, 10, 16))

        self.assertEqual("inverted", source)
        self.assertEqual(24000.0, strike)
        self.assertTrue(10 < value < 60, value)


class FearComponentTest(unittest.TestCase):
    def test_every_hk_target_maps_to_an_option_product(self):
        for target in HK_INDEX_FEAR_GREED_TARGETS:
            product = HK_FEAR_TARGET_OPTION_PRODUCT.get(target["symbol"])
            self.assertIn(product, HK_OPTION_PRODUCTS, target["symbol"])

    def test_put_call_component_is_positive_five_day_mean(self):
        index = pd.date_range("2026-09-01", periods=10, freq="B")
        ratio = pd.Series(
            [0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7],
            index=index,
            dtype=float,
        )
        calculator = object.__new__(HKStockFearGreedCalculator)
        calculator.target_symbol = "HSI.HK"
        with patch.object(
            service, "load_option_oi_pcr_series", return_value=ratio
        ), patch(
            "src.core.services.hk_stock_fear_greed_service.load_option_oi_pcr_series",
            return_value=ratio,
        ):
            raw = calculator._option_open_interest_ratio(index, index[0].date(), index[-1].date())

        # 持仓量口径取正号：恒指 put 持仓随多头仓位堆积，高 P/C 对应偏贪婪（局部顶部）。
        self.assertAlmostEqual(1.5, raw.iloc[-1], places=6)
        self.assertTrue(raw.iloc[0] != raw.iloc[0])  # 前三天不足 min_periods，应为空

    def test_missing_option_history_keeps_component_empty(self):
        index = pd.date_range("2026-09-01", periods=5, freq="B")
        calculator = object.__new__(HKStockFearGreedCalculator)
        calculator.target_symbol = "HSI.HK"
        with patch(
            "src.core.services.hk_stock_fear_greed_service.load_option_oi_pcr_series",
            return_value=pd.Series(dtype=float),
        ):
            raw = calculator._option_open_interest_ratio(index, index[0].date(), index[-1].date())

        self.assertTrue(raw.isna().all())


class SyncWriteTest(unittest.TestCase):
    def test_sync_date_writes_one_row_per_index_option_product(self):
        sync = HKOptionDailySync(include_etf=False, allow_dtop_backfill=False)
        trade_date = date(2026, 10, 16)
        try:
            sync._fetch_text = lambda url: MARKET_REPORT if url.endswith("hsio261016.htm") else None
            result = sync.sync_date(trade_date)
            rows = (
                sync.db.query(service.HKOptionDaily)
                .filter(service.HKOptionDaily.date == trade_date)
                .all()
            )
        finally:
            sync.close()

        self.assertEqual(1, result["saved"])
        self.assertEqual(["hsio"], [row.product for row in rows])
        self.assertAlmostEqual(194 / 1167, rows[0].put_call_volume_ratio, places=4)
        self.assertEqual(1167, rows[0].call_volume)
        self.assertEqual(194, rows[0].put_volume)

    def test_pcr_series_reads_back(self):
        sync = HKOptionDailySync(include_etf=False, allow_dtop_backfill=False)
        trade_date = date(2026, 10, 16)
        try:
            sync._fetch_text = lambda url: MARKET_REPORT if url.endswith("hsio261016.htm") else None
            sync.sync_date(trade_date)
        finally:
            sync.close()

        series = load_option_pcr_series("hsio", trade_date, trade_date)

        self.assertEqual(1, len(series))
        self.assertAlmostEqual(194 / 1167, float(series.iloc[0]), places=4)

    def test_dtop_backfill_inverts_iv_and_reports_it(self):
        """htm 过期时走 DTOP：成交量入库，并用结算价反解平值 IV 回传。"""
        sync = HKOptionDailySync(include_etf=False, allow_dtop_backfill=True)
        # 用另一个交易日，避免和 htm 报告的用例共用同一行
        trade_date = date(2026, 10, 15)
        try:
            sync._fetch_text = lambda url: None
            sync._index_closes = lambda day: {"HSI": 24000.0, "HSCEI": 9000.0, "HSTECH": 5000.0}
            sync._fetch_zip_texts = lambda url, suffixes: {"opt_dtl_all.rpt": DTOP_REPORT}
            result = sync.sync_date(trade_date)
            rows = {
                row.product: row
                for row in sync.db.query(service.HKOptionDaily)
                .filter(service.HKOptionDaily.date == trade_date)
                .all()
            }
        finally:
            sync.close()

        self.assertEqual({"hsio": "dtop", "hhio": "dtop"}, result["products"])
        self.assertEqual({"hsio", "hhio"}, set(rows))
        self.assertEqual("inverted", rows["hsio"].atm_iv_source)
        self.assertEqual(24000.0, rows["hsio"].atm_strike)
        self.assertTrue(10 < result["atm_iv"]["hsio"] < 60, result["atm_iv"])
        # DTOP 里没有的标的（这里是 HTI）不会硬塞 0，直接不入库
        self.assertNotIn("htio", rows)


if __name__ == "__main__":
    unittest.main()


class SyncRangeResumeAndConcurrencyTest(unittest.TestCase):
    """sync_range 的断点续传与多线程分片。"""

    def _make_sync(self):
        sync = object.__new__(HKOptionDailySync)
        sync.logger = logging.getLogger("test.hk_option_sync")
        sync.timeout = 1.0
        sync.include_etf = True
        sync.allow_dtop_backfill = True
        sync._external_session = True
        return sync

    def test_resume_skips_covered_dates_but_keeps_recent(self):
        sync = self._make_sync()
        covered = {
            date(2026, 9, 1),
            date(2026, 9, 2),
            date(2026, 9, 14),
            date(2026, 9, 15),
        }
        synced_dates = []

        def fake_sync_day(trade_date):
            synced_dates.append(trade_date)
            return {"saved": 1, "trading_day": True}

        with patch.object(sync, "_covered_dates", return_value=covered), patch.object(
            sync, "sync_date", side_effect=fake_sync_day
        ):
            result = sync.sync_range(
                date(2026, 9, 1), date(2026, 9, 16), max_workers=1, keep_recent_days=3
            )

        # keep_until = 9/16 - 3 = 9/13：9/1、9/2 被跳过；9/14、9/15 虽在 covered
        # 里但属于近 3 天的幂等窗口，仍照常重抓。
        self.assertEqual(
            synced_dates,
            [
                date(2026, 9, 3),
                date(2026, 9, 4),
                date(2026, 9, 7),
                date(2026, 9, 8),
                date(2026, 9, 9),
                date(2026, 9, 10),
                date(2026, 9, 11),
                date(2026, 9, 14),
                date(2026, 9, 15),
                date(2026, 9, 16),
            ],
        )
        self.assertEqual(result["skipped_synced_days"], 2)
        self.assertEqual(result["weekday_count"], 10)
        self.assertEqual(result["workers"], 1)
        self.assertEqual(result["errors"], [])

    def test_parallel_chunks_cover_all_dates_in_worker_threads(self):
        import threading

        sync = self._make_sync()
        seen = []
        threads = set()

        def fake_sync_day(trade_date):
            threads.add(threading.current_thread().name)
            seen.append(trade_date)
            return {"saved": 1, "trading_day": True}

        # 2026-08-03 是周一：8/3~8/7、8/10~8/13 共 9 个工作日（8/8、8/9 为周末）。
        dates = [
            date(2026, 8, 3),
            date(2026, 8, 4),
            date(2026, 8, 5),
            date(2026, 8, 6),
            date(2026, 8, 7),
            date(2026, 8, 10),
            date(2026, 8, 11),
            date(2026, 8, 12),
            date(2026, 8, 13),
        ]
        # 线程内会新建实例，必须 patch 类级别的方法才能覆盖到 worker。
        with patch.object(sync, "_covered_dates", return_value=set()), patch.object(
            HKOptionDailySync, "sync_date", side_effect=fake_sync_day
        ):
            result = sync.sync_range(dates[0], dates[-1], max_workers=3)

        self.assertEqual(sorted(seen), dates)
        # 分片任务极快时线程池可能来不及起满 max_workers 个线程，只断言上限。
        self.assertLessEqual(len(threads), 3)
        self.assertEqual(result["workers"], 3)
        self.assertEqual(result["weekday_count"], len(dates))
        self.assertTrue(all(name.startswith("hkopt") for name in threads))

    def test_thread_workers_use_independent_sessions(self):
        sync = self._make_sync()

        def fake_sync_day(trade_date):
            return {"saved": 1, "trading_day": True}

        dates = [date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5)]
        # 线程内会新建实例并各自 close；用类级 mock 统计创建数量。
        with patch.object(sync, "_covered_dates", return_value=set()), patch.object(
            HKOptionDailySync, "sync_date", side_effect=fake_sync_day
        ), patch.object(HKOptionDailySync, "__init__", return_value=None) as mock_init, patch.object(
            HKOptionDailySync, "close", return_value=None
        ) as mock_close:
            result = sync.sync_range(dates[0], dates[-1], max_workers=2)

        self.assertEqual(result["weekday_count"], 3)
        # max_workers=2 → 除主实例外再建 2 个线程实例（每个线程一个会话）。
        self.assertEqual(mock_init.call_count, 2)
        self.assertEqual(mock_close.call_count, 2)

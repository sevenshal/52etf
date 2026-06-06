import os
import subprocess
import sys
import tempfile
import textwrap
from unittest import TestCase


class AStockBaseDataSyncTest(TestCase):
    def test_module_import_smoke(self):
        fd, path = tempfile.mkstemp(suffix=".duckdb")
        os.close(fd)
        os.unlink(path)
        try:
            code = textwrap.dedent(
                """
                from src.robot.a_stock_base_data_sync import AStockBaseDataSyncService

                assert AStockBaseDataSyncService is not None
                """
            )
            env = os.environ.copy()
            env["ANALYTICS_DB_PATH"] = path

            subprocess.run([sys.executable, "-c", code], env=env, check=True)
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def test_sync_fund_basic_handles_duplicate_provider_symbols(self):
        fd, path = tempfile.mkstemp(suffix=".duckdb")
        os.close(fd)
        os.unlink(path)
        try:
            code = textwrap.dedent(
                """
                import pandas as pd
                from sqlalchemy import text

                from src.core.analytics_database import AnalyticsSession
                from src.robot.a_stock_base_data_sync import AStockBaseDataSyncService

                class FakeTushare:
                    def get_a_stock_etf_basic_frame(self, list_status="L"):
                        return pd.DataFrame(
                            [
                                {
                                    "ts_code": "159001.SZ",
                                    "extname": "old",
                                    "csname": None,
                                    "name": None,
                                    "exchange": "SZ",
                                    "list_date": "20141020",
                                    "list_status": "L",
                                },
                                {
                                    "ts_code": "159001.SZ",
                                    "extname": "new",
                                    "csname": None,
                                    "name": None,
                                    "exchange": "SZ",
                                    "list_date": "20141020",
                                    "list_status": "L",
                                },
                                {
                                    "ts_code": "510500.SH",
                                    "extname": "中证500ETF",
                                    "csname": None,
                                    "name": None,
                                    "exchange": "SH",
                                    "list_date": "20130206",
                                    "list_status": "L",
                                },
                            ]
                        )

                session = AnalyticsSession()
                service = AStockBaseDataSyncService(analytics_db=session, tushare_service=FakeTushare())
                try:
                    saved = service.sync_fund_basic()
                    rows = session.execute(
                        text("SELECT ts_code, name FROM a_stock_fund_basic ORDER BY ts_code")
                    ).fetchall()
                finally:
                    service.close()
                    AnalyticsSession.remove()

                assert saved == 2
                assert rows == [("159001.SZ", "new"), ("510500.SH", "中证500ETF")]
                """
            )
            env = os.environ.copy()
            env["ANALYTICS_DB_PATH"] = path

            subprocess.run([sys.executable, "-c", code], env=env, check=True)
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def test_analytics_mapping_insert_is_idempotent_for_duplicate_keys(self):
        fd, path = tempfile.mkstemp(suffix=".duckdb")
        os.close(fd)
        os.unlink(path)
        try:
            code = textwrap.dedent(
                """
                from datetime import datetime
                from sqlalchemy import text

                from src.core.analytics_database import AStockFundBasic, AnalyticsSession
                from src.robot.a_stock_base_data_sync import AStockBaseDataSyncService

                now = datetime(2026, 5, 29, 19, 20, 0)
                session = AnalyticsSession()
                service = AStockBaseDataSyncService(analytics_db=session, tushare_service=object())
                try:
                    service._insert_analytics_mappings(
                        AStockFundBasic,
                        [
                            {"ts_code": "159001.SZ", "name": "old", "market": "SZ", "list_date": None, "updated_at": now},
                            {"ts_code": "159001.SZ", "name": "new", "market": "SZ", "list_date": None, "updated_at": now},
                            {"ts_code": "510500.SH", "name": "中证500ETF", "market": "SH", "list_date": None, "updated_at": now},
                        ],
                    )
                    service._insert_analytics_mappings(
                        AStockFundBasic,
                        [
                            {"ts_code": "159001.SZ", "name": "newer", "market": "SZ", "list_date": None, "updated_at": now},
                        ],
                    )
                    rows = session.execute(
                        text("SELECT ts_code, name FROM a_stock_fund_basic ORDER BY ts_code")
                    ).fetchall()
                finally:
                    service.close()
                    AnalyticsSession.remove()

                assert rows == [("159001.SZ", "newer"), ("510500.SH", "中证500ETF")]
                """
            )
            env = os.environ.copy()
            env["ANALYTICS_DB_PATH"] = path

            subprocess.run([sys.executable, "-c", code], env=env, check=True)
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def test_basic_reference_upserts_preserve_existing_rows(self):
        fd, path = tempfile.mkstemp(suffix=".duckdb")
        os.close(fd)
        os.unlink(path)
        try:
            code = textwrap.dedent(
                """
                import pandas as pd
                from datetime import datetime
                from sqlalchemy import text

                from src.core.analytics_database import AStockBasic, AStockFundBasic, AnalyticsSession
                from src.robot.a_stock_base_data_sync import AStockBaseDataSyncService

                now = datetime(2026, 5, 29, 20, 0, 0)
                session = AnalyticsSession()
                service = AStockBaseDataSyncService(analytics_db=session, tushare_service=object())
                try:
                    service._insert_analytics_mappings(
                        AStockBasic,
                        [
                            {
                                "ts_code": "000001.SZ",
                                "symbol": "000001",
                                "name": "old",
                                "area": "深圳",
                                "industry": "银行",
                                "market": "主板",
                                "exchange": "SZSE",
                                "list_date": None,
                                "delist_date": None,
                                "list_status": "L",
                                "updated_at": now,
                            },
                            {
                                "ts_code": "999999.SZ",
                                "symbol": "999999",
                                "name": "keep",
                                "area": None,
                                "industry": None,
                                "market": None,
                                "exchange": None,
                                "list_date": None,
                                "delist_date": None,
                                "list_status": "D",
                                "updated_at": now,
                            },
                        ],
                    )
                    service._upsert_stock_basic(
                        pd.DataFrame(
                            [
                                {
                                    "ts_code": "000001.SZ",
                                    "symbol": "000001",
                                    "name": "new",
                                    "area": "深圳",
                                    "industry": "银行",
                                    "market": "主板",
                                    "exchange": "SZSE",
                                    "list_date": "",
                                    "delist_date": "",
                                    "list_status": "L",
                                }
                            ]
                        )
                    )

                    service._insert_analytics_mappings(
                        AStockFundBasic,
                        [
                            {"ts_code": "159001.SZ", "name": "old fund", "market": "SZ", "list_date": None, "updated_at": now},
                            {"ts_code": "159999.SZ", "name": "keep fund", "market": "SZ", "list_date": None, "updated_at": now},
                        ],
                    )
                    service._upsert_fund_basic(
                        pd.DataFrame(
                            [
                                {
                                    "ts_code": "159001.SZ",
                                    "extname": "new fund",
                                    "csname": None,
                                    "name": None,
                                    "exchange": "SZ",
                                    "list_date": "",
                                }
                            ]
                        ),
                        ["159001.SZ"],
                    )

                    stock_rows = session.execute(
                        text("SELECT ts_code, name FROM a_stock_basic ORDER BY ts_code")
                    ).fetchall()
                    fund_rows = session.execute(
                        text("SELECT ts_code, name FROM a_stock_fund_basic ORDER BY ts_code")
                    ).fetchall()
                finally:
                    service.close()
                    AnalyticsSession.remove()

                assert stock_rows == [("000001.SZ", "new"), ("999999.SZ", "keep")]
                assert fund_rows == [("159001.SZ", "new fund"), ("159999.SZ", "keep fund")]
                """
            )
            env = os.environ.copy()
            env["ANALYTICS_DB_PATH"] = path

            subprocess.run([sys.executable, "-c", code], env=env, check=True)
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def test_name_change_range_reinsert_replaces_existing_primary_key(self):
        fd, path = tempfile.mkstemp(suffix=".duckdb")
        os.close(fd)
        os.unlink(path)
        try:
            code = textwrap.dedent(
                """
                import pandas as pd
                from datetime import date
                from sqlalchemy import text

                from src.core.analytics_database import AnalyticsSession
                from src.robot.a_stock_base_data_sync import AStockBaseDataSyncService

                session = AnalyticsSession()
                service = AStockBaseDataSyncService(analytics_db=session, tushare_service=object())
                try:
                    frame = pd.DataFrame(
                        [
                            {
                                "ts_code": "000001.SZ",
                                "name": "平安银行",
                                "start_date": "20250101",
                                "end_date": "",
                                "change_reason": "更名",
                            }
                        ]
                    )
                    service._insert_name_changes(frame)
                    service._replace_name_changes_range(
                        frame,
                        date(2026, 2, 1),
                        date(2026, 6, 1),
                    )
                    rows = session.execute(
                        text(
                            "SELECT ts_code, name, start_date, COUNT(*) OVER () AS total "
                            "FROM a_stock_name_changes"
                        )
                    ).fetchall()
                finally:
                    service.close()
                    AnalyticsSession.remove()

                assert rows == [("000001.SZ", "平安银行", date(2025, 1, 1), 1)]
                """
            )
            env = os.environ.copy()
            env["ANALYTICS_DB_PATH"] = path

            subprocess.run([sys.executable, "-c", code], env=env, check=True)
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def test_incremental_adj_factor_uses_repair_window_not_default_start(self):
        fd, path = tempfile.mkstemp(suffix=".duckdb")
        os.close(fd)
        os.unlink(path)
        try:
            code = textwrap.dedent(
                """
                import pandas as pd
                from datetime import date, datetime

                from src.core.analytics_database import AStockAdjFactor, AnalyticsSession
                from src.robot.a_stock_base_data_sync import AStockBaseDataSyncService

                class FakeTushare:
                    def __init__(self):
                        self.fetch_calls = []

                    def get_trade_calendar_frame(self, start_date, end_date):
                        assert start_date == date(2026, 5, 22)
                        assert end_date == date(2026, 5, 30)
                        return pd.DataFrame([{"cal_date": date(2026, 5, 29), "is_open": 1}])

                    def get_a_stock_adj_factor_range_frame(self, *args, **kwargs):
                        self.fetch_calls.append(args)
                        return pd.DataFrame()

                fake_tushare = FakeTushare()
                session = AnalyticsSession()
                service = AStockBaseDataSyncService(analytics_db=session, tushare_service=fake_tushare)
                try:
                    service._insert_analytics_mappings(
                        AStockAdjFactor,
                        [
                            {
                                "ts_code": "000001.SZ",
                                "trade_date": date(2018, 7, 1),
                                "adj_factor": 1.0,
                                "created_at": datetime(2026, 5, 29, 20, 0, 0),
                                "updated_at": datetime(2026, 5, 29, 20, 0, 0),
                            },
                            {
                                "ts_code": "000001.SZ",
                                "trade_date": date(2026, 5, 29),
                                "adj_factor": 1.0,
                                "created_at": datetime(2026, 5, 29, 20, 0, 0),
                                "updated_at": datetime(2026, 5, 29, 20, 0, 0),
                            }
                        ],
                    )
                    result = service.sync_market_adj_factor(
                        date(2018, 7, 1),
                        date(2026, 5, 30),
                        incremental=True,
                    )
                finally:
                    service.close()
                    AnalyticsSession.remove()

                assert result["start_date"] == "2026-05-22"
                assert result["trading_days"] == 1
                assert result["chunks"] == 1
                assert fake_tushare.fetch_calls == [(date(2026, 5, 29), date(2026, 5, 29))]
                """
            )
            env = os.environ.copy()
            env["ANALYTICS_DB_PATH"] = path

            subprocess.run([sys.executable, "-c", code], env=env, check=True)
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def test_market_repair_window_force_refreshes_existing_dates(self):
        fd, path = tempfile.mkstemp(suffix=".duckdb")
        os.close(fd)
        os.unlink(path)
        try:
            code = textwrap.dedent(
                """
                import pandas as pd
                from datetime import date, datetime

                import src.robot.a_stock_base_data_sync as sync_module
                from src.core.analytics_database import AStockMarketDaily, AnalyticsSession
                from src.robot.a_stock_base_data_sync import AStockBaseDataSyncService

                sync_module.MIN_MARKET_DAILY_ROWS = 1

                class FakeTushare:
                    def __init__(self):
                        self.fetch_calls = []

                    def get_a_stock_market_daily_range_frame(self, start_date, end_date):
                        self.fetch_calls.append((start_date, end_date))
                        return pd.DataFrame()

                now = datetime(2026, 5, 29, 20, 0, 0)
                fake_tushare = FakeTushare()
                session = AnalyticsSession()
                service = AStockBaseDataSyncService(analytics_db=session, tushare_service=fake_tushare)
                try:
                    service._insert_analytics_mappings(
                        AStockMarketDaily,
                        [
                            {
                                "trade_date": date(2026, 5, 29),
                                "ts_code": "000001.SZ",
                                "open": 1.0,
                                "high": 1.0,
                                "low": 1.0,
                                "close": 1.0,
                                "pre_close": 1.0,
                                "change": 0.0,
                                "pct_chg": 0.0,
                                "vol": 0.0,
                                "amount": 0.0,
                                "total_mv": 1.0,
                                "circ_mv": 1.0,
                                "float_share": 1.0,
                                "total_share": 1.0,
                                "turnover_rate": 0.0,
                                "created_at": now,
                                "updated_at": now,
                            }
                        ],
                    )
                    service._ensure_market_days([date(2026, 5, 29)], force_refresh=False)
                    skipped_calls = list(fake_tushare.fetch_calls)
                    service._ensure_market_days([date(2026, 5, 29)], force_refresh=True)
                finally:
                    service.close()
                    AnalyticsSession.remove()

                assert skipped_calls == []
                assert fake_tushare.fetch_calls == [(date(2026, 5, 29), date(2026, 5, 29))]
                """
            )
            env = os.environ.copy()
            env["ANALYTICS_DB_PATH"] = path

            subprocess.run([sys.executable, "-c", code], env=env, check=True)
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def test_incremental_fund_daily_skips_when_no_new_trading_days(self):
        fd, path = tempfile.mkstemp(suffix=".duckdb")
        os.close(fd)
        os.unlink(path)
        try:
            code = textwrap.dedent(
                """
                import pandas as pd
                from datetime import date, datetime

                from src.core.analytics_database import AStockFundBasic, AStockFundDaily, AnalyticsSession
                from src.robot.a_stock_base_data_sync import AStockBaseDataSyncService

                class FakeTushare:
                    def get_a_stock_etf_basic_frame(self, list_status="L"):
                        return pd.DataFrame(
                            [
                                {
                                    "ts_code": "159001.SZ",
                                    "extname": "ETF",
                                    "csname": None,
                                    "name": None,
                                    "exchange": "SZ",
                                    "list_date": "20200101",
                                    "list_status": "L",
                                }
                            ]
                        )

                    def get_trade_calendar_frame(self, start_date, end_date):
                        assert start_date == date(2026, 5, 30)
                        assert end_date == date(2026, 5, 30)
                        return pd.DataFrame([{"cal_date": date(2026, 5, 30), "is_open": 0}])

                    def get_a_stock_fund_daily_range_frame(self, *args, **kwargs):
                        raise AssertionError("fund daily fetch should be skipped without new trading days")

                now = datetime(2026, 5, 29, 20, 0, 0)
                session = AnalyticsSession()
                service = AStockBaseDataSyncService(analytics_db=session, tushare_service=FakeTushare())
                try:
                    service._insert_analytics_mappings(
                        AStockFundBasic,
                        [{"ts_code": "159001.SZ", "name": "ETF", "market": "SZ", "list_date": None, "updated_at": now}],
                    )
                    service._insert_analytics_mappings(
                        AStockFundDaily,
                        [
                            {
                                "ts_code": "159001.SZ",
                                "trade_date": date(2026, 5, 29),
                                "open": 1.0,
                                "high": 1.0,
                                "low": 1.0,
                                "close": 1.0,
                                "pre_close": 1.0,
                                "change": 0.0,
                                "pct_chg": 0.0,
                                "vol": 0.0,
                                "amount": 0.0,
                                "created_at": now,
                                "updated_at": now,
                            }
                        ],
                    )
                    result = service.sync_fund_daily(
                        date(2019, 5, 26),
                        date(2026, 5, 30),
                        incremental=True,
                    )
                finally:
                    service.close()
                    AnalyticsSession.remove()

                assert result["start_date"] is None
                assert result["trading_days"] == 0
                assert result["jobs"] == 0
                """
            )
            env = os.environ.copy()
            env["ANALYTICS_DB_PATH"] = path

            subprocess.run([sys.executable, "-c", code], env=env, check=True)
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def test_incremental_fund_adj_factor_batches_recent_trade_dates(self):
        fd, path = tempfile.mkstemp(suffix=".duckdb")
        os.close(fd)
        os.unlink(path)
        try:
            code = textwrap.dedent(
                """
                import pandas as pd
                from datetime import date, datetime

                from src.core.analytics_database import AStockFundAdjFactor, AnalyticsSession
                from src.robot.a_stock_base_data_sync import AStockBaseDataSyncService

                class FakeTushare:
                    def __init__(self):
                        self.date_calls = []

                    def get_a_stock_etf_basic_frame(self, list_status="L"):
                        return pd.DataFrame(
                            [
                                {"ts_code": "159001.SZ", "name": "ETF1", "exchange": "SZ", "list_status": "L"},
                                {"ts_code": "159002.SZ", "name": "ETF2", "exchange": "SZ", "list_status": "L"},
                            ]
                        )

                    def get_trade_calendar_frame(self, start_date, end_date):
                        assert start_date == date(2026, 5, 30)
                        assert end_date == date(2026, 6, 1)
                        return pd.DataFrame(
                            [
                                {"cal_date": date(2026, 5, 30), "is_open": 0},
                                {"cal_date": date(2026, 5, 31), "is_open": 0},
                                {"cal_date": date(2026, 6, 1), "is_open": 1},
                            ]
                        )

                    def get_a_stock_fund_adj_factor_trade_date_frame(self, trade_date, **kwargs):
                        self.date_calls.append(trade_date)
                        return pd.DataFrame(
                            [
                                {"ts_code": "159001.SZ", "trade_date": "20260601", "adj_factor": 1.1},
                                {"ts_code": "159002.SZ", "trade_date": "20260601", "adj_factor": 1.2},
                            ]
                        )

                    def get_a_stock_fund_adj_factor_range_frame(self, *args, **kwargs):
                        raise AssertionError("fund_adj incremental should batch by trade_date")

                now = datetime(2026, 5, 29, 20, 0, 0)
                fake_tushare = FakeTushare()
                session = AnalyticsSession()
                service = AStockBaseDataSyncService(analytics_db=session, tushare_service=fake_tushare)
                try:
                    service._insert_analytics_mappings(
                        AStockFundAdjFactor,
                        [
                            {
                                "ts_code": "159001.SZ",
                                "trade_date": date(2026, 5, 29),
                                "adj_factor": 1.0,
                                "created_at": now,
                                "updated_at": now,
                            },
                            {
                                "ts_code": "159002.SZ",
                                "trade_date": date(2026, 5, 29),
                                "adj_factor": 1.0,
                                "created_at": now,
                                "updated_at": now,
                            },
                        ],
                    )
                    result = service.sync_fund_adj_factor(
                        date(2019, 5, 26),
                        date(2026, 6, 1),
                        incremental=True,
                    )
                finally:
                    service.close()
                    AnalyticsSession.remove()

                assert fake_tushare.date_calls == [date(2026, 6, 1)]
                assert result["jobs"] == 2
                assert result["date_batches"] == 1
                assert result["symbol_jobs"] == 0
                assert result["trading_days"] == 1
                assert result["saved_rows"] == 2
                """
            )
            env = os.environ.copy()
            env["ANALYTICS_DB_PATH"] = path

            subprocess.run([sys.executable, "-c", code], env=env, check=True)
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

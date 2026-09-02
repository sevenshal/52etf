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

    def test_sync_ths_board_data_updates_main_catalog_and_duckdb_caches(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            analytics_path = os.path.join(tmpdir, "analytics.duckdb")
            sqlite_path = os.path.join(tmpdir, "main.db")
            code = textwrap.dedent(
                """
                from datetime import date
                import pandas as pd
                from sqlalchemy import text

                from src.core.analytics_database import AnalyticsSession
                from src.core.database import Session
                import src.robot.a_stock_base_data_sync as sync_module
                from src.robot.a_stock_base_data_sync import AStockBaseDataSyncService

                sync_module.THS_DAILY_MIN_ROWS = 1

                class FakeTushare:
                    member_calls = []
                    daily_calls = []

                    def get_ths_index_frame(self, index_type):
                        suffix = {"N": "1", "TH": "2", "I": "3"}[index_type]
                        return pd.DataFrame([{
                            "ts_code": f"88500{suffix}.TI",
                            "name": f"板块{index_type}", "count": 1,
                            "exchange": "A", "list_date": "20200101",
                        }])

                    def get_ths_member_frame(self, ths_code):
                        self.member_calls.append(ths_code)
                        return pd.DataFrame([{
                            "ts_code": ths_code, "con_code": "600001.SH",
                            "con_name": "测试股", "weight": 1.0,
                            "in_date": "20200101", "out_date": None, "is_new": "Y",
                        }])

                    def get_trade_calendar_frame(self, start_date, end_date):
                        return pd.DataFrame([
                            {"cal_date": value, "is_open": 1}
                            for value in [
                                date(2026, 8, 18), date(2026, 8, 19), date(2026, 8, 20),
                                date(2026, 8, 21), date(2026, 8, 24),
                            ]
                        ])

                    def get_ths_daily_frame(self, trade_date):
                        self.daily_calls.append(trade_date)
                        return pd.DataFrame([{
                            "ts_code": "885001.N", "trade_date": trade_date,
                            "open": 99, "close": 100, "high": 101, "low": 98,
                            "pre_close": 99, "pct_change": 1.01,
                        }])

                db = AnalyticsSession()
                db.execute(text(
                    "CREATE TABLE xueqiu_cube_holdings_snapshots (snapshot_date DATE)"
                ))
                db.execute(text(
                    "INSERT INTO xueqiu_cube_holdings_snapshots VALUES ('2026-08-18')"
                ))
                db.execute(text(
                    "INSERT INTO a_stock_ths_daily (ths_code, trade_date, close, updated_at) "
                    "VALUES ('885001.N', '2026-08-24', 99, CURRENT_TIMESTAMP)"
                ))
                db.commit()
                service = AStockBaseDataSyncService(analytics_db=db, tushare_service=FakeTushare())
                try:
                    result = service.sync_ths_board_data(date(2026, 8, 24))
                    second = service.sync_ths_board_data(date(2026, 8, 24))
                    member_count = db.execute(text("SELECT COUNT(*) FROM a_stock_ths_member")).scalar()
                    daily_count = db.execute(text("SELECT COUNT(*) FROM a_stock_ths_daily")).scalar()
                    catalog_count = Session().execute(text("SELECT COUNT(*) FROM ai_stock_ths_index_cache")).scalar()
                finally:
                    service.close()
                    AnalyticsSession.remove()
                    Session.remove()

                assert result["catalog_rows"] == 3
                assert result["member_refreshed"] is True
                assert second["member_refreshed"] is True
                assert member_count == 3
                assert daily_count == 5
                assert catalog_count == 3
                assert len(FakeTushare.member_calls) == 6
                assert FakeTushare.daily_calls == [
                    date(2026, 8, 18), date(2026, 8, 19), date(2026, 8, 20),
                    date(2026, 8, 21), date(2026, 8, 24),
                    date(2026, 8, 20), date(2026, 8, 21), date(2026, 8, 24),
                ]
                """
            )
            env = os.environ.copy()
            env["ANALYTICS_DB_PATH"] = analytics_path
            env["QUANT_SQLITE_PATH"] = sqlite_path
            subprocess.run([sys.executable, "-c", code], env=env, check=True)

    def test_ths_bulk_replace_preserves_boards_and_dates_not_in_refresh(self):
        fd, path = tempfile.mkstemp(suffix=".duckdb")
        os.close(fd)
        os.unlink(path)
        try:
            code = textwrap.dedent(
                """
                import pandas as pd
                import src.robot.a_stock_base_data_sync as sync_module
                from src.core.analytics_database import AnalyticsSession
                from src.robot.a_stock_base_data_sync import (
                    _bulk_replace_ths_daily_frame,
                    _bulk_replace_ths_member_frame,
                )

                sync_module.THS_DAILY_MIN_ROWS = 1
                db = AnalyticsSession()
                try:
                    _bulk_replace_ths_member_frame(db, pd.DataFrame([
                        {"ts_code": "A.TI", "con_code": "000001.SZ", "con_name": "旧A"},
                        {"ts_code": "B.TI", "con_code": "000002.SZ", "con_name": "保留B"},
                    ]))
                    _bulk_replace_ths_member_frame(db, pd.DataFrame([
                        {"ts_code": "A.TI", "con_code": "000003.SZ", "con_name": "新A"},
                    ]))
                    members = db.execute(sync_module.text(
                        "SELECT ths_code, con_code FROM a_stock_ths_member ORDER BY ths_code"
                    )).fetchall()

                    _bulk_replace_ths_daily_frame(db, pd.DataFrame([
                        {"ts_code": "A.TI", "trade_date": "20260820", "close": 10},
                        {"ts_code": "A.TI", "trade_date": "20260821", "close": 11},
                    ]))
                    _bulk_replace_ths_daily_frame(db, pd.DataFrame([
                        {"ts_code": "A.TI", "trade_date": "20260821", "close": 12},
                    ]))
                    daily = db.execute(sync_module.text(
                        "SELECT trade_date, close FROM a_stock_ths_daily ORDER BY trade_date"
                    )).fetchall()
                finally:
                    AnalyticsSession.remove()

                assert members == [("A.TI", "000003.SZ"), ("B.TI", "000002.SZ")]
                assert [(str(row[0]), row[1]) for row in daily] == [
                    ("2026-08-20", 10.0), ("2026-08-21", 12.0)
                ]
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
                                "pb": 1.0,
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

    def test_incremental_fund_daily_batches_recent_trade_dates(self):
        fd, path = tempfile.mkstemp(suffix=".duckdb")
        os.close(fd)
        os.unlink(path)
        try:
            code = textwrap.dedent(
                """
                import pandas as pd
                from datetime import date, datetime
                from sqlalchemy import text

                from src.core.analytics_database import AStockFundDaily, AnalyticsSession
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

                    def get_a_stock_fund_daily_trade_date_frame(self, trade_date, **kwargs):
                        self.date_calls.append((trade_date, kwargs))
                        return pd.DataFrame(
                            [
                                {"ts_code": "159001.SZ", "trade_date": "20260601", "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "pre_close": 0.99, "change": 0.01, "pct_chg": 1.0, "vol": 100, "amount": 1000},
                                {"ts_code": "159002.SZ", "trade_date": "20260601", "open": 2.0, "high": 2.1, "low": 1.9, "close": 2.0, "pre_close": 1.98, "change": 0.02, "pct_chg": 1.0, "vol": 200, "amount": 2000},
                                {"ts_code": "159999.SZ", "trade_date": "20260601", "open": 9.0, "high": 9.1, "low": 8.9, "close": 9.0, "pre_close": 8.99, "change": 0.01, "pct_chg": 0.1, "vol": 900, "amount": 9000},
                            ]
                        )

                    def get_a_stock_fund_daily_range_frame(self, *args, **kwargs):
                        raise AssertionError("fund_daily incremental should batch by trade_date")

                now = datetime(2026, 5, 29, 20, 0, 0)
                fake_tushare = FakeTushare()
                session = AnalyticsSession()
                service = AStockBaseDataSyncService(analytics_db=session, tushare_service=fake_tushare)
                try:
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
                            },
                            {
                                "ts_code": "159002.SZ",
                                "trade_date": date(2026, 5, 29),
                                "open": 2.0,
                                "high": 2.0,
                                "low": 2.0,
                                "close": 2.0,
                                "pre_close": 2.0,
                                "change": 0.0,
                                "pct_chg": 0.0,
                                "vol": 0.0,
                                "amount": 0.0,
                                "created_at": now,
                                "updated_at": now,
                            },
                        ],
                    )
                    result = service.sync_fund_daily(
                        date(2019, 5, 26),
                        date(2026, 6, 1),
                        incremental=True,
                    )
                    rows = session.execute(
                        text("SELECT ts_code, trade_date FROM a_stock_fund_daily WHERE trade_date = DATE '2026-06-01' ORDER BY ts_code")
                    ).fetchall()
                finally:
                    service.close()
                    AnalyticsSession.remove()

                assert fake_tushare.date_calls == [(date(2026, 6, 1), {"raise_on_error": True})]
                assert result["jobs"] == 2
                assert result["date_batches"] == 1
                assert result["symbol_jobs"] == 0
                assert result["trading_days"] == 1
                assert result["saved_rows"] == 2
                assert rows == [("159001.SZ", date(2026, 6, 1)), ("159002.SZ", date(2026, 6, 1))]
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

    def test_incremental_fund_daily_empty_date_batch_does_not_fallback_to_symbol_jobs(self):
        fd, path = tempfile.mkstemp(suffix=".duckdb")
        os.close(fd)
        os.unlink(path)
        try:
            code = textwrap.dedent(
                """
                import pandas as pd
                from datetime import date, datetime

                from src.core.analytics_database import AStockFundDaily, AnalyticsSession
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
                        return pd.DataFrame([{"cal_date": date(2026, 6, 1), "is_open": 1}])

                    def get_a_stock_fund_daily_trade_date_frame(self, trade_date, **kwargs):
                        self.date_calls.append((trade_date, kwargs))
                        return pd.DataFrame(columns=["ts_code", "trade_date"])

                    def get_a_stock_fund_daily_range_frame(self, *args, **kwargs):
                        raise AssertionError("empty date batch should not fallback to symbol jobs")

                now = datetime(2026, 5, 29, 20, 0, 0)
                fake_tushare = FakeTushare()
                session = AnalyticsSession()
                service = AStockBaseDataSyncService(analytics_db=session, tushare_service=fake_tushare)
                try:
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
                            },
                            {
                                "ts_code": "159002.SZ",
                                "trade_date": date(2026, 5, 29),
                                "open": 2.0,
                                "high": 2.0,
                                "low": 2.0,
                                "close": 2.0,
                                "pre_close": 2.0,
                                "change": 0.0,
                                "pct_chg": 0.0,
                                "vol": 0.0,
                                "amount": 0.0,
                                "created_at": now,
                                "updated_at": now,
                            },
                        ],
                    )
                    result = service.sync_fund_daily(
                        date(2019, 5, 26),
                        date(2026, 6, 1),
                        incremental=True,
                    )
                finally:
                    service.close()
                    AnalyticsSession.remove()

                assert fake_tushare.date_calls == [(date(2026, 6, 1), {"raise_on_error": True})]
                assert result["jobs"] == 2
                assert result["date_batches"] == 1
                assert result["symbol_jobs"] == 0
                assert result["saved_rows"] == 0
                assert result["errors"] == []
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

    def test_incremental_income_plan_skips_fresh_symbols(self):
        fd, path = tempfile.mkstemp(suffix=".duckdb")
        os.close(fd)
        os.unlink(path)
        try:
            code = textwrap.dedent(
                """
                from datetime import date

                from src.robot.a_stock_base_data_sync import _plan_income_symbol_ranges

                jobs, stats = _plan_income_symbol_ranges(
                    ["000001.SZ", "000002.SZ", "000003.SZ"],
                    None,
                    date(2026, 6, 8),
                    incremental=True,
                    symbol_bounds={
                        "000001.SZ": (date(2014, 1, 2), date(2026, 4, 30)),
                        "000002.SZ": (date(2014, 1, 2), date(2026, 4, 1)),
                        "000003.SZ": (None, None),
                    },
                )

                assert stats == {
                    "skipped": 1,
                    "backfill": 1,
                    "incremental": 1,
                    "full": 0,
                }
                assert jobs == [
                    ("000002.SZ", date(2026, 2, 15), date(2026, 6, 8), "incremental"),
                    ("000003.SZ", date(2014, 1, 2), date(2026, 6, 8), "backfill"),
                ]
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

    def test_report_rc_upsert_normalizes_duplicate_research_rows(self):
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
                from src.robot.a_stock_base_data_sync import _bulk_upsert_report_rc_frame

                session = AnalyticsSession()
                try:
                    saved = _bulk_upsert_report_rc_frame(
                        session,
                        pd.DataFrame(
                            [
                                {
                                    "ts_code": "000001.SZ",
                                    "name": "平安银行",
                                    "report_date": "20260529",
                                    "report_title": "一季报点评",
                                    "report_type": "公司点评",
                                    "classify": "一般报告",
                                    "org_name": "测试证券",
                                    "author_name": "张三",
                                    "quarter": "2026Q4",
                                    "eps": "2.10",
                                    "pe": "8.5",
                                    "rating": "买入",
                                    "max_price": "18.5",
                                    "min_price": "16.0",
                                    "create_time": "2026-05-29 20:10:00",
                                },
                                {
                                    "ts_code": "000001.SZ",
                                    "name": "平安银行",
                                    "report_date": "20260529",
                                    "report_title": "一季报点评",
                                    "report_type": "公司点评",
                                    "classify": "一般报告",
                                    "org_name": "测试证券",
                                    "author_name": "张三",
                                    "quarter": "2026Q4",
                                    "eps": "2.20",
                                    "pe": "8.1",
                                    "rating": "增持",
                                    "max_price": "19.0",
                                    "min_price": "16.5",
                                    "create_time": "2026-05-29 21:10:00",
                                },
                            ]
                        ),
                    )
                    rows = session.execute(
                        text(
                            "SELECT ts_code, report_date, org_name, author_name, quarter, "
                            "eps, pe, rating, max_price, min_price "
                            "FROM a_stock_report_rc"
                        )
                    ).fetchall()
                finally:
                    AnalyticsSession.remove()

                assert saved == 1
                assert len(rows) == 1
                row = rows[0]
                assert row[:5] == ("000001.SZ", date(2026, 5, 29), "测试证券", "张三", "2026Q4")
                assert round(row[5], 4) == 2.2
                assert round(row[6], 4) == 8.1
                assert row[7:] == ("增持", 19.0, 16.5)
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

    def test_incremental_report_rc_uses_repair_window(self):
        fd, path = tempfile.mkstemp(suffix=".duckdb")
        os.close(fd)
        os.unlink(path)
        try:
            code = textwrap.dedent(
                """
                import pandas as pd
                from datetime import date, datetime

                from src.core.analytics_database import AStockReportRc, AnalyticsSession
                from src.robot.a_stock_base_data_sync import AStockBaseDataSyncService

                class FakeTushare:
                    def __init__(self):
                        self.fetch_calls = []

                    def get_a_stock_report_rc_range_frame(self, start_date, end_date, **kwargs):
                        self.fetch_calls.append((start_date, end_date, kwargs))
                        return pd.DataFrame(
                            [
                                {
                                    "ts_code": "000001.SZ",
                                    "name": "平安银行",
                                    "report_date": "20260530",
                                    "report_title": "最新点评",
                                    "report_type": "公司点评",
                                    "classify": "一般报告",
                                    "org_name": "测试证券",
                                    "author_name": "张三",
                                    "quarter": "2026Q4",
                                    "eps": 2.3,
                                    "pe": 8.0,
                                    "rating": "买入",
                                    "max_price": 20.0,
                                    "min_price": 17.0,
                                }
                            ]
                        )

                now = datetime(2026, 5, 29, 20, 0, 0)
                fake_tushare = FakeTushare()
                session = AnalyticsSession()
                service = AStockBaseDataSyncService(analytics_db=session, tushare_service=fake_tushare)
                try:
                    service._insert_analytics_mappings(
                        AStockReportRc,
                        [
                            {
                                "id": "seed",
                                "ts_code": "000001.SZ",
                                "name": "平安银行",
                                "report_date": date(2026, 5, 29),
                                "report_title": "旧点评",
                                "report_type": "公司点评",
                                "classify": "一般报告",
                                "org_name": "测试证券",
                                "author_name": "张三",
                                "quarter": "2026Q4",
                                "eps": 2.1,
                                "pe": 8.5,
                                "rating": "买入",
                                "max_price": 18.5,
                                "min_price": 16.0,
                                "created_at": now,
                                "updated_at": now,
                            }
                        ],
                    )
                    result = service.sync_report_rc(
                        date(2020, 1, 1),
                        date(2026, 5, 30),
                        incremental=True,
                    )
                finally:
                    service.close()
                    AnalyticsSession.remove()

                assert result["start_date"] == "2026-05-22"
                assert result["chunks"] == 1
                assert result["saved_rows"] == 1
                assert fake_tushare.fetch_calls == [
                    (date(2026, 5, 22), date(2026, 5, 30), {"raise_on_error": True})
                ]
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

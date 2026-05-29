import os
import subprocess
import sys
import tempfile
import textwrap
from unittest import TestCase


class AStockBaseDataSyncTest(TestCase):
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

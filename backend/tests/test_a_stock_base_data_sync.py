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

import os
import subprocess
import sys
import tempfile
import textwrap


def test_a_stock_fund_flow_daily_upsert_uses_duckdb_analytics_schema():
    fd, path = tempfile.mkstemp(suffix=".duckdb")
    os.close(fd)
    os.unlink(path)
    try:
        code = textwrap.dedent(
            """
            from datetime import datetime

            from src.core.duckdb_utils import connect_duckdb
            from src.robot.a_stock_fund_flow_sync import (
                ANALYTICS_DB_PATH,
                _insert_or_replace_rows,
                rank_item_to_daily_record,
            )

            row = rank_item_to_daily_record(
                {
                    "code": "600487",
                    "name": "亨通光电",
                    "price": 77.12,
                    "change_pct": 9.42,
                    "main_net": 3976260608.0,
                    "main_net_pct": 16.35,
                    "super_net": 4493251840.0,
                    "super_net_pct": 18.48,
                    "large_net": -516991232.0,
                    "large_net_pct": -2.13,
                    "mid_net": -2096092224.0,
                    "mid_net_pct": -8.62,
                    "small_net": -1880168480.0,
                    "small_net_pct": -7.73,
                    "updated_at": "2026-05-29T15:00:00",
                },
                now=datetime(2026, 5, 31, 18, 0, 0),
            )
            assert _insert_or_replace_rows([row]) == 1

            connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=False)
            try:
                rows = connection.execute(
                    '''
                    SELECT CAST(trade_date AS VARCHAR), ts_code, symbol, name, main_net, source
                    FROM a_stock_fund_flow_daily
                    '''
                ).fetchall()
            finally:
                connection.close()

            assert rows == [
                (
                    "2026-05-29",
                    "600487.SH",
                    "600487",
                    "亨通光电",
                    3976260608.0,
                    "eastmoney_push2",
                )
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

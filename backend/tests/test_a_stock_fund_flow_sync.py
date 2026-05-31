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


def test_tushare_moneyflow_dc_sync_converts_wan_to_yuan():
    fd, path = tempfile.mkstemp(suffix=".duckdb")
    os.close(fd)
    os.unlink(path)
    try:
        code = textwrap.dedent(
            """
            from datetime import date

            import pandas as pd

            from src.core.duckdb_utils import connect_duckdb
            from src.robot.a_stock_fund_flow_sync import (
                ANALYTICS_DB_PATH,
                sync_tushare_moneyflow_dc_trade_date,
                tushare_moneyflow_dc_row_to_record,
            )

            row = {
                "trade_date": "20260529",
                "ts_code": "600487.SH",
                "name": "亨通光电",
                "pct_change": 9.42,
                "close": 77.12,
                "net_amount": 397626.06,
                "net_amount_rate": 16.35,
                "buy_elg_amount": 449325.18,
                "buy_elg_amount_rate": 18.48,
                "buy_lg_amount": -51699.12,
                "buy_lg_amount_rate": -2.13,
                "buy_md_amount": -209609.22,
                "buy_md_amount_rate": -8.62,
                "buy_sm_amount": -188016.85,
                "buy_sm_amount_rate": -7.73,
            }
            record = tushare_moneyflow_dc_row_to_record(row)
            assert record["trade_date"].isoformat() == "2026-05-29"
            assert record["symbol"] == "600487"
            assert record["main_net"] == 3976260600.0
            assert record["source"] == "tushare_moneyflow_dc"

            class FakePro:
                def moneyflow_dc(self, trade_date):
                    assert trade_date == "20260529"
                    return pd.DataFrame([row])

            class FakeTushare:
                pro = FakePro()

            result = sync_tushare_moneyflow_dc_trade_date(
                date(2026, 5, 29),
                tushare_service=FakeTushare(),
            )
            assert result["source"] == "tushare_moneyflow_dc"
            assert result["saved_rows"] == 1

            connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=False)
            try:
                rows = connection.execute(
                    '''
                    SELECT CAST(trade_date AS VARCHAR), ts_code, main_net, super_net, source
                    FROM a_stock_fund_flow_daily
                    '''
                ).fetchall()
            finally:
                connection.close()

            assert len(rows) == 1
            trade_date, ts_code, main_net, super_net, source = rows[0]
            assert trade_date == "2026-05-29"
            assert ts_code == "600487.SH"
            assert abs(main_net - 3976260600.0) < 1000
            assert abs(super_net - 4493251800.0) < 1000
            assert source == "tushare_moneyflow_dc"
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


def test_tushare_moneyflow_sync_rebuilds_main_net_from_large_orders():
    code = textwrap.dedent(
        """
        from src.robot.a_stock_fund_flow_sync import tushare_moneyflow_row_to_record

        row = {
            "trade_date": "20200102",
            "ts_code": "600487.SH",
            "buy_sm_amount": 563357.37,
            "sell_sm_amount": 610062.48,
            "buy_md_amount": 657503.80,
            "sell_md_amount": 715604.93,
            "buy_lg_amount": 584618.26,
            "sell_lg_amount": 625850.80,
            "buy_elg_amount": 626334.55,
            "sell_elg_amount": 480295.78,
        }
        record = tushare_moneyflow_row_to_record(
            row,
            name_map={"600487.SH": "亨通光电"},
        )
        assert record["trade_date"].isoformat() == "2020-01-02"
        assert record["name"] == "亨通光电"
        assert abs(record["super_net"] - 1460387700.0) < 1
        assert abs(record["large_net"] + 412325400.0) < 1
        assert abs(record["main_net"] - 1048062300.0) < 1
        assert record["source"] == "tushare_moneyflow"
        """
    )
    subprocess.run([sys.executable, "-c", code], check=True)

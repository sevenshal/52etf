import os
import subprocess
import sys
import tempfile

import duckdb


def test_existing_a_stock_market_table_gets_valuation_columns_and_view_is_rebuilt():
    fd, path = tempfile.mkstemp(suffix=".duckdb")
    os.close(fd)
    os.unlink(path)
    try:
        connection = duckdb.connect(path)
        connection.execute("""
            CREATE TABLE a_stock_market_daily (
                trade_date DATE,
                ts_code VARCHAR,
                open FLOAT,
                high FLOAT,
                low FLOAT,
                close FLOAT,
                pre_close FLOAT,
                change FLOAT,
                pct_chg FLOAT,
                vol FLOAT,
                amount FLOAT,
                total_mv FLOAT,
                circ_mv FLOAT,
                float_share FLOAT,
                total_share FLOAT,
                turnover_rate FLOAT,
                created_at TIMESTAMP,
                updated_at TIMESTAMP,
                PRIMARY KEY (trade_date, ts_code)
            )
        """)
        connection.execute("CREATE VIEW a_stock_market_daily_qfq AS SELECT * FROM a_stock_market_daily")
        connection.close()

        env = os.environ.copy()
        env["ANALYTICS_DB_PATH"] = path
        subprocess.run(
            [sys.executable, "-c", "import src.core.analytics_database"],
            env=env,
            check=True,
        )

        connection = duckdb.connect(path, read_only=True)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(a_stock_market_daily)").fetchall()}
        views = {
            row[0]
            for row in connection.execute(
                "SELECT table_name FROM information_schema.views WHERE table_name = 'a_stock_market_daily_qfq'"
            ).fetchall()
        }
        connection.close()

        assert {"volume_ratio", "pe", "pe_ttm", "pb", "dv_ratio", "dv_ttm"} <= columns
        assert "a_stock_market_daily_qfq" in views
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_existing_financial_statement_tables_get_the_full_tushare_field_set():
    """存量的窄版财务报表要能自动补齐 tushare 官方字段全集，不需要人工执行 ALTER。

    这几张表原来只存了扫描器当时用得到的十几列。现在改成"接口有什么就存什么"，
    生产库里已经建好的窄表必须在启动时自动补列——少数股东权益(minority_int)就是
    因为当初没同步，导致 DCF 拿全口径 FCFF 去比归母市值时当场没法修。
    """
    fd, path = tempfile.mkstemp(suffix=".duckdb")
    os.close(fd)
    os.unlink(path)
    try:
        connection = duckdb.connect(path)
        connection.execute("""
            CREATE TABLE a_stock_balancesheet (
                id VARCHAR PRIMARY KEY,
                ts_code VARCHAR,
                end_date DATE,
                ann_date DATE,
                report_type VARCHAR,
                comp_type VARCHAR,
                total_assets FLOAT,
                total_liab FLOAT,
                money_cap FLOAT,
                created_at TIMESTAMP,
                updated_at TIMESTAMP
            )
        """)
        connection.execute("""
            CREATE TABLE a_stock_fina_indicator (
                id VARCHAR PRIMARY KEY,
                ts_code VARCHAR,
                end_date DATE,
                ann_date DATE,
                roe FLOAT,
                roic FLOAT,
                created_at TIMESTAMP,
                updated_at TIMESTAMP
            )
        """)
        connection.execute(
            "INSERT INTO a_stock_balancesheet VALUES "
            "('id-1', '000915.SZ', DATE '2025-12-31', DATE '2026-04-24', '1', '1', 100, 20, 30, NULL, NULL)"
        )
        connection.close()

        env = os.environ.copy()
        env["ANALYTICS_DB_PATH"] = path
        subprocess.run(
            [sys.executable, "-c", "import src.core.analytics_database"],
            env=env,
            check=True,
        )

        connection = duckdb.connect(path, read_only=True)
        balancesheet_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(a_stock_balancesheet)").fetchall()
        }
        fina_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(a_stock_fina_indicator)").fetchall()
        }
        preserved = connection.execute(
            "SELECT total_assets, minority_int FROM a_stock_balancesheet WHERE id = 'id-1'"
        ).fetchone()
        connection.close()

        assert {"minority_int", "total_hldr_eqy_inc_min_int", "f_ann_date", "update_flag"} <= balancesheet_columns
        assert {"fcff", "netdebt", "ocf_to_or", "interestdebt"} <= fina_columns
        # 补列是 ALTER TABLE ADD COLUMN，已有行必须原样保留、新列为 NULL
        assert preserved == (100.0, None)
    finally:
        if os.path.exists(path):
            os.unlink(path)

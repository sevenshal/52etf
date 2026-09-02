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

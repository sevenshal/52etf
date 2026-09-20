import os
import subprocess
import sys
import tempfile
import textwrap


def test_forecast_sync_fetches_by_period_and_upserts():
    """业绩预告按报告期整市场拉取（非 VIP 的 forecast 必须带 ts_code/ann_date，整市场只能走 period），
    重复同步是 upsert 不会产生重复行。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        code = textwrap.dedent(
            """
            from datetime import date
            import pandas as pd
            from sqlalchemy import text

            from src.core.analytics_database import AnalyticsSession
            from src.robot.a_stock_base_data_sync import sync_a_stock_forecast_data

            class FakeTushare:
                calls = []

                def get_a_stock_forecast_period_frame(self, period):
                    FakeTushare.calls.append(period)
                    if period != date(2026, 6, 30):
                        return pd.DataFrame()
                    return pd.DataFrame([{
                        "ts_code": "600001.SH", "end_date": date(2026, 6, 30), "ann_date": date(2026, 8, 21),
                        "type": "预增", "p_change_min": 45.0, "p_change_max": 80.0,
                        "net_profit_min": 1000.0, "net_profit_max": 1500.0, "last_parent_net": 700.0,
                        "first_ann_date": date(2026, 8, 21), "summary": "净利大增", "change_reason": "订单增加",
                    }])

            db = AnalyticsSession()
            try:
                first = sync_a_stock_forecast_data(
                    start_date=date(2026, 7, 1), end_date=date(2026, 9, 18),
                    tushare_service=FakeTushare(), analytics_db=db,
                )
                second = sync_a_stock_forecast_data(
                    start_date=date(2026, 7, 1), end_date=date(2026, 9, 18),
                    tushare_service=FakeTushare(), analytics_db=db,
                )
                rows = db.execute(text(
                    "SELECT ts_code, p_change_min, p_change_max, type, summary FROM a_stock_forecast"
                )).fetchall()
            finally:
                AnalyticsSession.remove()

            # 按报告期整市场拉，不按股票逐只请求
            assert date(2026, 6, 30) in FakeTushare.calls
            assert all(isinstance(call, date) for call in FakeTushare.calls)
            assert first["saved_rows"] == 1
            assert first["fetched_rows"] == 1
            # 重复同步是 upsert，不会变成两行
            assert second["saved_rows"] == 1
            assert len(rows) == 1
            assert rows[0][1] == 45.0 and rows[0][2] == 80.0
            assert rows[0][3] == "预增" and rows[0][4] == "净利大增"
            """
        )
        env = os.environ.copy()
        env["ANALYTICS_DB_PATH"] = os.path.join(tmpdir, "analytics.duckdb")
        env["QUANT_SQLITE_PATH"] = os.path.join(tmpdir, "main.db")
        subprocess.run([sys.executable, "-c", code], env=env, check=True)


def test_express_sync_fetches_by_period():
    """业绩快报同样按报告期整市场拉（express_vip），不需要逐只股票请求。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        code = textwrap.dedent(
            """
            from datetime import date
            import pandas as pd
            from sqlalchemy import text

            from src.core.analytics_database import AnalyticsSession
            from src.robot.a_stock_base_data_sync import sync_a_stock_express_data

            class FakeTushare:
                periods = []

                def get_a_stock_express_period_frame(self, period):
                    FakeTushare.periods.append(period)
                    if period != date(2026, 6, 30):
                        return pd.DataFrame()
                    return pd.DataFrame([{
                        "ts_code": code, "end_date": date(2026, 6, 30), "ann_date": date(2026, 8, 21),
                        "revenue": 2000.0, "n_income": 150.0, "yoy_net_profit": 100.0,
                        "yoy_sales": 22.0, "yoy_dedu_np": 48.0, "diluted_roe": 9.5, "is_audit": "0",
                        "perf_summary": "增长", "remark": "",
                    } for code in ("600001.SH", "600002.SH")])

            db = AnalyticsSession()
            try:
                result = sync_a_stock_express_data(
                    start_date=date(2026, 7, 1), end_date=date(2026, 9, 18),
                    tushare_service=FakeTushare(), analytics_db=db,
                )
                rows = db.execute(text(
                    "SELECT ts_code, n_income, yoy_net_profit, yoy_sales FROM a_stock_express ORDER BY ts_code"
                )).fetchall()
            finally:
                AnalyticsSession.remove()

            assert date(2026, 6, 30) in FakeTushare.periods
            assert result["saved_rows"] == 2
            assert [row[0] for row in rows] == ["600001.SH", "600002.SH"]
            assert rows[0][1] == 150.0 and rows[0][2] == 100.0 and rows[0][3] == 22.0
            """
        )
        env = os.environ.copy()
        env["ANALYTICS_DB_PATH"] = os.path.join(tmpdir, "analytics.duckdb")
        env["QUANT_SQLITE_PATH"] = os.path.join(tmpdir, "main.db")
        subprocess.run([sys.executable, "-c", code], env=env, check=True)

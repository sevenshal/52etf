#!/usr/bin/env python3
"""只读诊断：确认 a_stock_income.operate_income 是否一直是空的。

背景：tushare 官方文档(https://tushare.pro/document/2?doc_id=33)确认利润表接口
根本没有 operate_income 这个字段，但现有的研发动量因子(a_stock_innovation_momentum_virtual.py)
一直把它当"营业收入"用来算研发费用占比和三年营收复合增速。这个脚本只做统计查询，
不写入任何数据，用来在修复前确认历史数据确实受影响、以及新增的 revenue 列是否已经
有足够的历史可以直接顶替使用。

用法(需要在能访问真实 ANALYTICS_DB_PATH 的环境里跑，即生产环境本身)：
    cd backend && python scripts/inspect_a_stock_income_revenue_gap.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.duckdb_utils import ANALYTICS_DB_PATH, connect_duckdb  # noqa: E402


def main():
    connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
    try:
        overall = connection.execute(
            """
            SELECT
                COUNT(*) AS total_rows,
                COUNT(operate_income) AS non_null_operate_income,
                COUNT(revenue) AS non_null_revenue,
                COUNT(rd_exp) AS non_null_rd_exp,
                MIN(end_date) AS min_end_date,
                MAX(end_date) AS max_end_date
            FROM a_stock_income
            """
        ).fetchdf().to_dict(orient="records")[0]

        annual_coverage = connection.execute(
            """
            SELECT
                COUNT(*) AS annual_rows,
                COUNT(operate_income) AS annual_non_null_operate_income,
                COUNT(revenue) AS annual_non_null_revenue,
                COUNT(DISTINCT ts_code) AS symbols_with_annual_rows,
                COUNT(DISTINCT CASE WHEN revenue IS NOT NULL THEN ts_code END) AS symbols_with_annual_revenue
            FROM a_stock_income
            WHERE strftime(end_date, '%m-%d') = '12-31'
            """
        ).fetchdf().to_dict(orient="records")[0]

        sample_rows = connection.execute(
            """
            SELECT ts_code, end_date, ann_date, operate_income, revenue, rd_exp
            FROM a_stock_income
            WHERE revenue IS NOT NULL
            ORDER BY ann_date DESC
            LIMIT 5
            """
        ).fetchdf().to_dict(orient="records")
    finally:
        connection.close()

    print(json.dumps(
        {
            "overall": overall,
            "annual_report_rows_only": annual_coverage,
            "sample_rows_with_revenue": sample_rows,
        },
        ensure_ascii=False,
        indent=2,
        default=str,
    ))


if __name__ == "__main__":
    main()

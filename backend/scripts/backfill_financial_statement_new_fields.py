#!/usr/bin/env python3
"""一次性回填：把新增字段补进已经同步过的历史年份。

现在这4张表按 tushare 官方文档的输出参数**全集**建表(利润表97列、资产负债表161列、
现金流量表100列、财务指标170列，见 `src/core/tushare_statement_fields.py`)，所以这次
要补的不只是当初那几个字段，而是几百列历史数据。

背景：正常的"A股基础数据同步"任务是增量/回补逻辑——只有当某只股票完全没有历史
记录、或者最新公告已经过期(超过45天)时才会重新抓取。如果这4张财务报表缓存表
在这批新字段加进 tushare.py 的 fields 列表之前就已经跑过一次，那些已经落库的
历史年份不会因为 schema 新增了列就自动被刷新——正常点"立即执行一次"只会把
"最新一期"附近的窗口补上新字段，2021~2023这种更早的年份仍然是 NULL。

这个脚本对4张表分别调用同步函数并显式传 force_full_refresh=True：假装每只股票
都没有历史数据，逼着重新抓取完整的6年回溯窗口，把新字段补齐到所有历史年份。
只需要跑一次；之后的日常增量同步(不传这个参数)行为不变。

用法(需要在能访问真实 tushare token 配置和 ANALYTICS_DB_PATH 的环境里跑，
即生产环境本身)：
    cd backend && python scripts/backfill_financial_statement_new_fields.py
    # 先在少量股票上验证：
    python scripts/backfill_financial_statement_new_fields.py --symbols 600519.SH,000001.SZ
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.analytics_database import AnalyticsSession  # noqa: E402
from src.robot.a_stock_base_data_sync import (  # noqa: E402
    _load_income_symbols,
    sync_a_stock_balancesheet_data,
    sync_a_stock_cashflow_data,
    sync_a_stock_fina_indicator_data,
    sync_a_stock_income_data,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--symbols",
        default=None,
        help="逗号分隔的ts_code列表，仅回填这些股票(先在少量股票上验证再跑全量时用)",
    )
    args = parser.parse_args()

    analytics_db = AnalyticsSession()
    try:
        symbols = (
            [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
            if args.symbols
            else _load_income_symbols(analytics_db)
        )
        print(f"回填股票数: {len(symbols)}", flush=True)

        for label, fn in (
            ("income", sync_a_stock_income_data),
            ("balancesheet", sync_a_stock_balancesheet_data),
            ("cashflow", sync_a_stock_cashflow_data),
            ("fina_indicator", sync_a_stock_fina_indicator_data),
        ):
            print(f"=== 开始回填 {label} ===", flush=True)
            result = fn(
                end_date=date.today(),
                incremental=True,
                symbols=symbols,
                force_full_refresh=True,
                analytics_db=analytics_db,
            )
            print(
                json.dumps(
                    {
                        "table": label,
                        "status": result.get("status"),
                        "fetched_rows": result.get("fetched_rows"),
                        "saved_rows": result.get("saved_rows"),
                        "processed_symbols": result.get("processed_symbols"),
                        "empty_symbols": result.get("empty_symbols"),
                        "total_seconds": result.get("total_seconds"),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    finally:
        analytics_db.close()
        AnalyticsSession.remove()


if __name__ == "__main__":
    main()

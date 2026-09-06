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
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core import duckdb_utils  # noqa: E402
from src.core.analytics_database import AnalyticsSession  # noqa: E402
from src.robot import a_stock_base_data_sync as sync_module  # noqa: E402
from src.robot.a_stock_base_data_sync import (  # noqa: E402
    _load_income_symbols,
    sync_a_stock_balancesheet_data,
    sync_a_stock_cashflow_data,
    sync_a_stock_fina_indicator_data,
    sync_a_stock_income_data,
)

# --- DuckDB 写锁重试 ---------------------------------------------------------
# DuckDB 是单写者。生产后端的定时任务网格很密(整点/:15/:30/:45 各有任务，
# evc_static_info_sync、soxx_fear_greed_backfill 这类要跑 4~6 分钟)，而一张表的
# 回填要 13 分钟，必然会撞上。撞上时 `_insert_or_replace_analytics_frame()` 打开
# 写连接会直接抛 IOException，把整轮回填打断——实测两轮都死在这里。
#
# 正确的修法是在 `duckdb_utils.connect_duckdb()` 里加有界退避重试，但那是全应用
# 分析库写入的公共路径，改动影响面大，需要单独评估。这个脚本是一次性运维工具、
# 不进 pyz 打包产物，所以在这里就地把它包一层：只对"拿不到锁"这一种错误重试，
# 其它异常照常抛出。等公共路径那版修好之后，这段可以直接删掉。
LOCK_RETRY_ATTEMPTS = 60
LOCK_RETRY_SLEEP_SECONDS = 10
_original_connect_duckdb = duckdb_utils.connect_duckdb


def _connect_duckdb_waiting_for_lock(*args, **kwargs):
    for attempt in range(1, LOCK_RETRY_ATTEMPTS + 1):
        try:
            return _original_connect_duckdb(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            if "Could not set lock" not in str(exc):
                raise
            if attempt == 1 or attempt % 6 == 0:
                print(
                    f"    [锁等待] 第{attempt}次: 写锁被后端定时任务占用，"
                    f"{LOCK_RETRY_SLEEP_SECONDS}秒后重试",
                    flush=True,
                )
            if attempt >= LOCK_RETRY_ATTEMPTS:
                raise
            time.sleep(LOCK_RETRY_SLEEP_SECONDS)


# 同步模块 import 时就把名字绑走了(`from ..core.duckdb_utils import connect_duckdb`)，
# 所以两个位置都要换。
duckdb_utils.connect_duckdb = _connect_duckdb_waiting_for_lock
sync_module.connect_duckdb = _connect_duckdb_waiting_for_lock


# 表级重试是上面那层锁等待之外的兜底：锁等满 10 分钟仍拿不到(比如撞上手动触发的
# a_stock_base_data_sync，那个能跑 40 多分钟)时，整张表重来一次。
TABLE_RETRIES = 4
RETRY_SLEEP_SECONDS = 300

SYNC_FUNCTIONS = (
    ("income", sync_a_stock_income_data),
    ("balancesheet", sync_a_stock_balancesheet_data),
    ("cashflow", sync_a_stock_cashflow_data),
    ("fina_indicator", sync_a_stock_fina_indicator_data),
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--symbols",
        default=None,
        help="逗号分隔的ts_code列表，仅回填这些股票(先在少量股票上验证再跑全量时用)",
    )
    parser.add_argument(
        "--tables",
        default=None,
        help=(
            "逗号分隔的表名，只回填这几张(可选: "
            + ",".join(label for label, _ in SYNC_FUNCTIONS)
            + ")。中途失败后续跑剩下的表时用，避免把已经跑完的表再跑一遍"
        ),
    )
    args = parser.parse_args()

    selected = (
        {item.strip() for item in args.tables.split(",") if item.strip()}
        if args.tables
        else {label for label, _ in SYNC_FUNCTIONS}
    )
    unknown = selected - {label for label, _ in SYNC_FUNCTIONS}
    if unknown:
        parser.error(f"未知的表名: {', '.join(sorted(unknown))}")

    analytics_db = AnalyticsSession()
    try:
        symbols = (
            [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
            if args.symbols
            else _load_income_symbols(analytics_db)
        )
        print(f"回填股票数: {len(symbols)}", flush=True)

        for label, fn in SYNC_FUNCTIONS:
            if label not in selected:
                continue
            result = None
            for attempt in range(1, TABLE_RETRIES + 1):
                print(f"=== 开始回填 {label} (第{attempt}次) ===", flush=True)
                try:
                    result = fn(
                        end_date=date.today(),
                        incremental=True,
                        symbols=symbols,
                        force_full_refresh=True,
                        analytics_db=analytics_db,
                    )
                    break
                except Exception as exc:  # noqa: BLE001
                    print(f"!!! {label} 第{attempt}次失败: {exc}", flush=True)
                    if attempt >= TABLE_RETRIES:
                        raise
                    print(f"    {RETRY_SLEEP_SECONDS}秒后重试", flush=True)
                    time.sleep(RETRY_SLEEP_SECONDS)
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

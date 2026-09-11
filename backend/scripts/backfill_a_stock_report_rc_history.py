#!/usr/bin/env python3
"""一次性回填：补齐 a_stock_report_rc(卖方研报盈利预测/评级/目标价)的历史缺口。

背景：tushare report_rc 的 offset 超过 100000 直接报「查询数据失败，请确认参数」。
旧的同步按自然年切块，一年 35~45 万行只能翻到最新的 ~10 万行，撞上限那次报错又被
当成"已经翻完"静默吞掉，于是 2020~2025 每年都只剩 6~8 月以后的数据(每年恰好 ~10.15
万行)。同步已改成按月切块 + 撞上限自动拆区间，但已经落库的年份不会自己补回来——
增量同步只回补最近 7 天——所以要用这个脚本把历史区间按月重拉一遍。

写入按研报主键哈希 id INSERT OR REPLACE，已有的行只会被覆盖，不会重复。

必须用本分支(含 `_month_chunks`)的代码跑：脚本强制使用它所在 backend 目录下的 src，
site-packages 只提供第三方依赖。analytics.duckdb 是 640，只能以 quantd 身份写，而
quantd 读不了 /home/sevenshal(750)，所以先把 backend/src 和本脚本拷到 quantd 能读的地方：
    D=/home/quantd/quant_prod/backend/scripts/report_rc_backfill
    mkdir -p $D/scripts && cp -r src $D/ && cp scripts/backfill_a_stock_report_rc_history.py $D/scripts/
    cd $D && sudo -u quantd env \\
      ANALYTICS_DB_PATH=/home/quantd/quant_prod/quant_robot/analytics.duckdb \\
      QUANT_SQLITE_PATH=/home/quantd/quant_prod/quant_robot/evc_stocks.db \\
      PYTHONPATH=/home/quantd/quant_prod/backend/site-packages \\
      python3.12 scripts/backfill_a_stock_report_rc_history.py
    # 只看月度行数、不拉数据(只读打开，quantd 组成员即可，不需要 sudo)：
    ANALYTICS_DB_PATH=... PYTHONPATH=... python3.12 scripts/backfill_a_stock_report_rc_history.py --verify-only
"""
import argparse
import json
import sys
import time
import types
from datetime import date
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))
# 仓库里的 src 没有 __init__.py(命名空间包)，部署的 site-packages/src 是常规包；两者同在
# sys.path 上时常规包总是赢，sys.path 顺序救不了——直接把 src 钉到本仓库这份代码上，
# 否则会悄悄跑成部署的旧版(按年切块)同步逻辑。
_src_package = types.ModuleType("src")
_src_package.__path__ = [str(BACKEND_DIR / "src")]
sys.modules["src"] = _src_package

import pandas as pd  # noqa: E402

from src.core import duckdb_utils  # noqa: E402
from src.core.duckdb_utils import ANALYTICS_DB_PATH  # noqa: E402

# DuckDB 单写者，会撞上后端定时任务的写锁；只对"拿不到锁"重试(同
# backfill_financial_statement_new_fields.py 的做法)。
LOCK_RETRY_ATTEMPTS = 60
LOCK_RETRY_SLEEP_SECONDS = 10
_original_connect_duckdb = duckdb_utils.connect_duckdb


def _retry_on_lock(connect):
    for attempt in range(1, LOCK_RETRY_ATTEMPTS + 1):
        try:
            return connect()
        except Exception as exc:  # noqa: BLE001
            if "Could not set lock" not in str(exc) or attempt >= LOCK_RETRY_ATTEMPTS:
                raise
            if attempt == 1 or attempt % 6 == 0:
                print(f"    [锁等待] 第{attempt}次，{LOCK_RETRY_SLEEP_SECONDS}秒后重试", flush=True)
            time.sleep(LOCK_RETRY_SLEEP_SECONDS)


def _connect_duckdb_waiting_for_lock(*args, **kwargs):
    return _retry_on_lock(lambda: _original_connect_duckdb(*args, **kwargs))


duckdb_utils.connect_duckdb = _connect_duckdb_waiting_for_lock

MONTH_RETRIES = 3
MONTH_RETRY_SLEEP_SECONDS = 60
# 2020 年以来任何一个自然月都有几千条研报，低于这个数基本就是没拉全。
MIN_MONTH_ROWS = 1000


def _month_counts(start: date, end: date, read_only: bool):
    if read_only:
        import duckdb

        # 只读核对不能走 connect_duckdb：它先按读写打开，对 640 的库只会报 Permission denied。
        connection = _retry_on_lock(lambda: duckdb.connect(ANALYTICS_DB_PATH, read_only=True))
    else:
        # 回填后同进程里已有读写连接，DuckDB 要求同文件配置一致，只能沿用读写配置。
        connection = duckdb_utils.connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
    try:
        rows = connection.execute(
            """
            SELECT CAST(date_trunc('month', report_date) AS DATE) AS month, count(*)
            FROM a_stock_report_rc
            WHERE report_date BETWEEN ? AND ?
            GROUP BY 1
            """,
            [start, end],
        ).fetchall()
    finally:
        connection.close()
    return {month: count for month, count in rows}


def _verify(start: date, end: date, read_only: bool) -> bool:
    counts = _month_counts(start, end, read_only)
    gaps = []
    for period in pd.period_range(start, end, freq="M"):
        month = period.start_time.date()
        count = counts.get(month, 0)
        flag = "" if count >= MIN_MONTH_ROWS else "  <-- 缺口"
        if flag:
            gaps.append(f"{month:%Y-%m}")
        print(f"{month:%Y-%m} {count:>8}{flag}")
    print(f"合计 {sum(counts.values())} 行；缺口月份: {gaps or '无'}", flush=True)
    return not gaps


def _backfill(start: date, end: date):
    # 这几个模块 import 时就会建表(ensure_analytics_schema 读写打开分析库)，只在真正回填时导入。
    from src.core.analytics_database import AnalyticsSession
    from src.robot import a_stock_base_data_sync as sync_module
    from src.robot.a_stock_base_data_sync import AStockBaseDataSyncService, _month_chunks

    # 同步模块 import 时就把名字绑走了(`from ..core.duckdb_utils import connect_duckdb`)。
    sync_module.connect_duckdb = _connect_duckdb_waiting_for_lock

    failed_months = []
    total_fetched = 0
    started = time.time()
    for month_start, month_end in _month_chunks(start, end):
        for attempt in range(1, MONTH_RETRIES + 1):
            service = AStockBaseDataSyncService(analytics_db=AnalyticsSession())
            try:
                result = service.sync_report_rc(month_start, month_end, incremental=False)
            finally:
                service.close()
            if not result["errors"]:
                break
            print(f"    {month_start:%Y-%m} 第{attempt}次失败: {result['errors']}", flush=True)
            if attempt < MONTH_RETRIES:
                time.sleep(MONTH_RETRY_SLEEP_SECONDS)
        if result["errors"]:
            failed_months.append(f"{month_start:%Y-%m}")
        total_fetched += result["fetched_rows"]
        print(
            json.dumps(
                {
                    "month": f"{month_start:%Y-%m}",
                    "status": result["status"],
                    "fetched_rows": result["fetched_rows"],
                    "saved_rows": result["saved_rows"],
                    "elapsed_seconds": round(time.time() - started, 1),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    print(f"拉取完成：共 {total_fetched} 行，失败月份: {failed_months or '无'}", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default="2025-12-31")
    parser.add_argument("--verify-only", action="store_true", help="只读打印月度行数并检查缺口，不拉数据")
    args = parser.parse_args()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    print(f"ANALYTICS_DB_PATH={ANALYTICS_DB_PATH}", flush=True)

    if not args.verify_only:
        _backfill(start, end)
    ok = _verify(start, end, read_only=args.verify_only)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

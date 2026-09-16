"""回测数据工作区：把回测要读的表和日期范围从生产分析库复制到一个独立的 DuckDB 文件。

DuckDB 是单写者：回测子进程如果直接连生产库，逐月回放第一层要跑近一个小时的查询，这段时间里
主进程的写入（数据同步）和部分读取都会撞锁。所以回测开始时一张表一张表地 ATTACH 只读、复制、
DETACH（每张表只占几秒锁），之后整个回测只读写工作区文件。

调用前提（由回测子进程入口保证）：``ANALYTICS_DB_PATH`` 已指向工作区文件，并且已经 import 过
``core.analytics_database``——表结构和前复权视图按正式定义在工作区里建好，这里只灌数据。
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ...duckdb_utils import ANALYTICS_DB_PATH, connect_duckdb, is_duckdb_lock_conflict

logger = logging.getLogger(__name__)

WORKSPACE_FILENAME = "stock_system_backtest_workspace.duckdb"
ATTACH_ATTEMPTS = 120
ATTACH_SLEEP_SECONDS = 5.0


def workspace_path(source_path: str, filename: str = WORKSPACE_FILENAME) -> str:
    """工作区文件和生产分析库放同一个目录；不同回测用不同文件名，互不干扰。"""
    return os.path.join(os.path.dirname(os.path.abspath(source_path)), filename)


def _in(values: Sequence[str]) -> str:
    return ", ".join("?" for _ in values)


def table_plan(start: date, end: date) -> List[Tuple[str, str, List[Any], bool]]:
    """(表, WHERE, 参数, 是否 CREATE AS)：只复制回测会读到的日期范围。"""
    from ....robot.a_stock_base_data_config import A_STOCK_INDEX_FEAR_GREED_TARGETS

    targets = list(dict.fromkeys(str(t["symbol"]).upper() for t in A_STOCK_INDEX_FEAR_GREED_TARGETS))
    proxies = sorted(
        {str(t["proxy_etf"]).upper() for t in A_STOCK_INDEX_FEAR_GREED_TARGETS if t.get("proxy_etf")}
        | {"510300.SH", "510500.SH", "512100.SH", "563300.SH"}
    )
    market_from = start - timedelta(days=800)          # beta 两年 + K 线 400 天
    statements_from = date(start.year - 7, 1, 1)       # 5 期年报 + TTM 需要的中期报告
    return [
        ("a_stock_basic", "TRUE", [], False),
        ("a_stock_name_changes", "TRUE", [], False),
        ("a_stock_market_daily", "trade_date BETWEEN ? AND ?", [market_from, end], False),
        # 复权因子不截尾：前复权视图以每只股票最新一个因子为锚，保持与生产一致
        ("a_stock_adj_factor", "trade_date >= ?", [market_from], False),
        ("a_stock_index_daily", "TRUE", [], False),
        ("a_stock_index_weight", "trade_date BETWEEN ? AND ?", [start - timedelta(days=70), end], False),
        ("a_stock_income", "end_date >= ?", [statements_from], False),
        ("a_stock_balancesheet", "end_date >= ?", [statements_from], False),
        ("a_stock_cashflow", "end_date >= ?", [statements_from], False),
        ("a_stock_fina_indicator", "end_date >= ?", [statements_from], False),
        ("a_stock_report_rc", "report_date BETWEEN ? AND ?", [start - timedelta(days=900), end], False),
        ("a_stock_chinabond_yield_curve_defs", "TRUE", [], False),
        ("a_stock_chinabond_yield_curve_daily", "trade_date <= ?", [end], False),
        ("a_stock_fund_daily", f"ts_code IN ({_in(proxies)})", proxies, False),
        ("a_stock_fund_adj_factor", f"ts_code IN ({_in(proxies)})", proxies, False),
        ("xueqiu_cube_holdings_snapshots", "snapshot_date BETWEEN ? AND ?", [date(2026, 5, 1), end], True),
    ]


def _attach(connection, source_path: str, sleep: Callable[[float], None]) -> None:
    for attempt in range(1, ATTACH_ATTEMPTS + 1):
        try:
            connection.execute(f"ATTACH '{source_path}' AS source_db (READ_ONLY)")
            return
        except Exception as exc:  # noqa: BLE001
            if not is_duckdb_lock_conflict(exc) or attempt >= ATTACH_ATTEMPTS:
                raise
            sleep(ATTACH_SLEEP_SECONDS)


def build_workspace(
    source_path: str,
    start: date,
    end: date,
    *,
    log: Callable[[str], None] = lambda message: None,
    sleep: Callable[[float], None] = time.sleep,
    plan: Optional[Sequence[Tuple[str, str, List[Any], bool]]] = None,
) -> Dict[str, int]:
    """``plan`` 默认是选股系统那份；别的回测（如板块九转）传自己的表清单，只复制它要读的表。"""
    workspace = ANALYTICS_DB_PATH
    if os.path.abspath(workspace) == os.path.abspath(source_path):
        raise RuntimeError("回测工作区不能是生产分析库本身")
    connection = connect_duckdb(workspace, prefer_read_only=False)
    main_db = connection.execute("SELECT current_database()").fetchone()[0]
    counts: Dict[str, int] = {}
    try:
        for table, where, params, create in (plan if plan is not None else table_plan(start, end)):
            _attach(connection, source_path, sleep)
            try:
                exists = connection.execute(
                    "SELECT COUNT(*) FROM duckdb_tables() WHERE database_name = 'source_db' AND table_name = ?", [table]
                ).fetchone()[0]
                if not exists:
                    log(f"生产库没有 {table}，跳过")
                    continue
                if create:
                    connection.execute(f'DROP TABLE IF EXISTS "{main_db}".main.{table}')
                    connection.execute(
                        f'CREATE TABLE "{main_db}".main.{table} AS SELECT * FROM source_db.main.{table} WHERE {where}', params
                    )
                else:
                    def columns(database: str) -> List[str]:
                        return [row[0] for row in connection.execute(
                            "SELECT column_name FROM duckdb_columns() WHERE database_name = ? AND table_name = ? "
                            "ORDER BY column_index", [database, table]).fetchall()]
                    source_columns = set(columns("source_db"))
                    shared = ", ".join(f'"{c}"' for c in columns(main_db) if c in source_columns)
                    connection.execute(f'DELETE FROM "{main_db}".main.{table}')
                    connection.execute(
                        f'INSERT INTO "{main_db}".main.{table} ({shared}) '
                        f'SELECT {shared} FROM source_db.main.{table} WHERE {where}', params,
                    )
                counts[table] = connection.execute(f'SELECT COUNT(*) FROM "{main_db}".main.{table}').fetchone()[0]
                log(f"复制 {table}：{counts[table]} 行")
            finally:
                connection.execute("DETACH source_db")
    finally:
        connection.close()
    return counts

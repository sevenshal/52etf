"""申万一/二级行业指数分钟线同步（tushare sw_mins），并入「分钟行情同步」任务。

与个股分钟线同一滚动窗口（ROLLING_TRADING_DAYS 个交易日），用于：
- 盘前算申万一/二级的同时段量能基准（口径与个股一致）；
- 行业分时小图的历史部分。

sw_mins 单次最多返回 5000 行，超出部分**静默截断、不报错**。所以分批必须按
「指数数 × 天数 × 241」不超过上限来切，并且每批回来的行数达到上限时视为可能被截断，
自动对半拆开重拉，绝不把截断的数据当成完整数据入库。
"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from ..duckdb_utils import ANALYTICS_DB_PATH, connect_duckdb
from .tushare import TushareService


logger = logging.getLogger(__name__)

BARS_PER_DAY = 241
ROW_LIMIT = 5000
# 每批 指数数 × 天数 × 241 ≤ 4820，留出余量
CODES_PER_BATCH = 4
DAYS_PER_BATCH = 5
SW_MINUTE_LEVELS = ("L1", "L2")     # 三级 tushare 没有分钟数据

_WRITE_LOCK = threading.Lock()


def sw_minute_codes(connection) -> List[str]:
    rows = connection.execute(
        "SELECT index_code FROM a_stock_sw_industry WHERE level IN ('L1', 'L2') ORDER BY index_code"
    ).fetchall()
    return [str(row[0]).strip().upper() for row in rows if row and row[0]]


def recent_trading_dates(connection, days: int, end_date: Optional[date] = None) -> List[date]:
    """最近 days 个交易日（取自全市场日线，保证与个股分钟线窗口一致）。"""
    end_date = end_date or date.today()
    rows = connection.execute(
        "SELECT DISTINCT trade_date FROM a_stock_market_daily WHERE trade_date <= ? "
        "ORDER BY trade_date DESC LIMIT ?",
        [end_date, max(1, int(days))],
    ).fetchall()
    return sorted(row[0] for row in rows if row and row[0])


def plan_batches(codes: List[str], dates: List[date]) -> List[Tuple[List[str], date, date]]:
    """切成 (指数列表, 起始日, 结束日) 批次，每批行数上限 CODES_PER_BATCH×DAYS_PER_BATCH×241。"""
    batches: List[Tuple[List[str], date, date]] = []
    for day_offset in range(0, len(dates), DAYS_PER_BATCH):
        day_chunk = dates[day_offset:day_offset + DAYS_PER_BATCH]
        for code_offset in range(0, len(codes), CODES_PER_BATCH):
            batches.append((codes[code_offset:code_offset + CODES_PER_BATCH], day_chunk[0], day_chunk[-1]))
    return batches


def fetch_batch(service: Any, codes: List[str], start: date, end: date) -> pd.DataFrame:
    """拉一批；行数触顶视为可能截断，按指数或日期对半拆开递归重拉。"""
    frame = service.get_sw_minute_frame(
        codes,
        datetime.combine(start, dtime(9, 0)),
        datetime.combine(end, dtime(15, 30)),
    )
    if frame is None or frame.empty or len(frame) < ROW_LIMIT:
        return frame if frame is not None else pd.DataFrame()

    if len(codes) > 1:
        middle = len(codes) // 2
        parts = [fetch_batch(service, codes[:middle], start, end), fetch_batch(service, codes[middle:], start, end)]
    elif start < end:
        middle_date = start + (end - start) / 2
        parts = [
            fetch_batch(service, codes, start, middle_date),
            fetch_batch(service, codes, middle_date + timedelta(days=1), end),
        ]
    else:
        # 单指数单日不可能超过 241 行，到这里说明接口行为变了，宁可报错也不入库截断数据
        raise RuntimeError(f"sw_mins 单指数单日返回 {len(frame)} 行，超出预期，疑似接口变更")
    parts = [part for part in parts if part is not None and not part.empty]
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def upsert_sw_minutes(frame: pd.DataFrame) -> int:
    if frame is None or frame.empty:
        return 0
    rows = frame[["ts_code", "trade_time", "open", "high", "low", "close", "vol", "amount"]].copy()
    rows = rows.drop_duplicates(["ts_code", "trade_time"], keep="last")
    rows["updated_at"] = datetime.now()
    with _WRITE_LOCK:
        connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=False)
        try:
            connection.register("sw_minute_upsert", rows)
            connection.execute(
                """
                INSERT OR REPLACE INTO a_stock_sw_minute_bar
                SELECT ts_code, trade_time, open, high, low, close, vol, amount, updated_at
                FROM sw_minute_upsert
                """
            )
        finally:
            connection.close()
    return int(len(rows))


def prune_sw_minutes(keep_from: date) -> int:
    with _WRITE_LOCK:
        connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=False)
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM a_stock_sw_minute_bar WHERE CAST(trade_time AS DATE) < ?", [keep_from]
            ).fetchone()[0]
            connection.execute("DELETE FROM a_stock_sw_minute_bar WHERE CAST(trade_time AS DATE) < ?", [keep_from])
        finally:
            connection.close()
    return int(count or 0)


def sync_sw_minute_bars(trading_days: int, full: bool = False, service: Optional[Any] = None) -> Dict[str, Any]:
    """同步申万一/二级分钟线：全量回补 trading_days 个交易日，或只补缺失交易日（额外重叠 1 天）。"""
    service = service or TushareService.get_instance()
    connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
    try:
        codes = sw_minute_codes(connection)
        window = recent_trading_dates(connection, trading_days)
        latest = connection.execute(
            "SELECT max(CAST(trade_time AS DATE)) FROM a_stock_sw_minute_bar"
        ).fetchone()[0]
    finally:
        connection.close()
    if not codes:
        return {"saved_rows": 0, "requests": 0, "errors": ["没有申万行业目录，先跑「A股基础数据同步」"]}
    if not window:
        return {"saved_rows": 0, "requests": 0, "errors": ["分析库没有交易日历"]}

    if full or latest is None:
        dates = window
    else:
        # 增量：从库里最新一天开始（重叠 1 天，覆盖盘中不完整的那天）
        dates = [day for day in window if day >= latest]
    batches = plan_batches(codes, dates)

    saved = 0
    errors: List[str] = []
    for batch_codes, start, end in batches:
        try:
            saved += upsert_sw_minutes(fetch_batch(service, batch_codes, start, end))
        except Exception as exc:  # noqa: BLE001  单批失败不影响其余批次
            if len(errors) < 50:
                errors.append(f"{','.join(batch_codes)} {start}~{end}: {exc}")
            logger.warning("申万分钟线同步失败 %s %s~%s: %s", batch_codes, start, end, exc)

    pruned = 0
    if window:
        try:
            pruned = prune_sw_minutes(window[0])
        except Exception as exc:  # noqa: BLE001
            errors.append(f"清理过期申万分钟线失败: {exc}")
    return {
        "codes": len(codes),
        "days": len(dates),
        "requests": len(batches),
        "saved_rows": saved,
        "pruned_rows": pruned,
        "mode": "full" if (full or latest is None) else "incremental",
        "errors": errors,
    }

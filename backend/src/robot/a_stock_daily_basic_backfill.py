"""回填 a_stock_market_daily 的 daily_basic 列（pe / pe_ttm / pb / ps / ps_ttm / 股息率 / 量比 /
自由流通换手率 / 自由流通股本 / 收盘涨跌停状态）。

这些列是分批后加的，日线同步只写新的一天、从未回填：生产库里 pe_ttm/pb 最早只到
2026-08-25，ps/ps_ttm 等更晚，价值投资的估值分位、选股系统的盈利收益率因子在历史快照上全是空的。

按交易日逐天拉 tushare daily_basic，只 UPDATE 这几列，不动行情和市值等其它列；
默认只补"整天 ps_ttm 都为空"的交易日（最后加上的一列，它有值说明整组都补过），可以中断后重跑续上。
"""

from __future__ import annotations

import logging
import time
from datetime import date
from typing import Dict, List, Optional

import pandas as pd

from ..core.duckdb_utils import ANALYTICS_DB_PATH, connect_duckdb, connect_duckdb_for_write
from ..core.services.tushare import TushareService

logger = logging.getLogger(__name__)

VALUATION_COLUMNS = (
    "pe", "pe_ttm", "pb", "ps", "ps_ttm", "dv_ratio", "dv_ttm", "volume_ratio", "turnover_rate_f", "free_share",
    "limit_status",
)
INTEGER_COLUMNS = {"limit_status"}
DEFAULT_BACKFILL_START = date(2019, 1, 1)
DEFAULT_BATCH_DAYS = 20


def _trade_dates_to_fill(start_date: date, end_date: date, only_missing: bool) -> List[date]:
    connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
    try:
        having = " HAVING COUNT(ps_ttm) = 0" if only_missing else ""
        rows = connection.execute(
            f"""
            SELECT trade_date
            FROM a_stock_market_daily
            WHERE trade_date BETWEEN ? AND ?
            GROUP BY trade_date{having}
            ORDER BY trade_date
            """,
            [start_date, end_date],
        ).fetchall()
    finally:
        connection.close()
    return [row[0] if isinstance(row[0], date) else pd.Timestamp(row[0]).date() for row in rows]


def _write_batch(frame: pd.DataFrame) -> int:
    """只更新估值列；缺失值写成 NULL（不能写 NaN：DuckDB 里 NaN 比任何数都大）。"""
    batch = frame.reindex(columns=["ts_code", "trade_date", *VALUATION_COLUMNS]).copy()
    for column in VALUATION_COLUMNS:
        values = pd.to_numeric(batch[column], errors="coerce")
        batch[column] = values.round().astype("Int64") if column in INTEGER_COLUMNS else values.astype("Float64")
    assignments = ", ".join(f"{column} = f.{column}" for column in VALUATION_COLUMNS)
    connection = connect_duckdb_for_write(ANALYTICS_DB_PATH)
    try:
        connection.execute("BEGIN TRANSACTION")
        connection.register("daily_basic_backfill_frame", batch)
        before = connection.execute("SELECT COUNT(*) FROM daily_basic_backfill_frame").fetchone()[0]
        connection.execute(
            f"""
            UPDATE a_stock_market_daily
            SET {assignments}
            FROM daily_basic_backfill_frame AS f
            WHERE a_stock_market_daily.ts_code = f.ts_code
              AND a_stock_market_daily.trade_date = f.trade_date
            """
        )
        connection.execute("COMMIT")
        return int(before)
    except Exception:
        try:
            connection.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        connection.close()


def backfill_a_stock_daily_basic_valuation(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    *,
    only_missing: bool = True,
    batch_days: int = DEFAULT_BATCH_DAYS,
    tushare_service: Optional[TushareService] = None,
) -> Dict[str, object]:
    start = start_date or DEFAULT_BACKFILL_START
    end = end_date or date.today()
    trade_dates = _trade_dates_to_fill(start, end, only_missing)
    tushare = tushare_service or TushareService.getInstance()
    started = time.monotonic()
    filled_dates = 0
    updated_rows = 0
    empty_dates: List[str] = []

    for offset in range(0, len(trade_dates), max(1, int(batch_days))):
        chunk = trade_dates[offset:offset + max(1, int(batch_days))]
        frames = []
        for trade_date in chunk:
            frame = tushare.get_a_stock_daily_basic_frame(trade_date)
            if frame is None or frame.empty:
                empty_dates.append(trade_date.isoformat())
                continue
            frames.append(frame)
        if not frames:
            continue
        updated_rows += _write_batch(pd.concat(frames, ignore_index=True))
        filled_dates += len(frames)
        logger.info(
            "daily_basic valuation backfill progress: %s/%s dates, last=%s",
            offset + len(chunk), len(trade_dates), chunk[-1],
        )

    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "candidate_dates": len(trade_dates),
        "filled_dates": filled_dates,
        "updated_rows": updated_rows,
        "empty_dates": empty_dates[:20],
        "empty_date_count": len(empty_dates),
        "seconds": round(time.monotonic() - started, 1),
    }

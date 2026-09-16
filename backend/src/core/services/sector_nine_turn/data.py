"""板块九转策略的数据读取：板块日线、成分股日线、指数成分快照、贪恐分数。

全部走生产已有的入口，不另起一套：

- 个股日 K 用 ``a_stock_consensus.load_a_stock_klines_batch``——和个股详情页 K 线图同一张表、
  同一组字段（前复权），所以九转红点和页面完全一致；
- 贪恐分数用 ``stock_system.sentiment.CalculatorHistoryLoader``——和贪恐曲线页面同一个入口，
  ``end_date`` 截到 as_of 就是 point-in-time 的；
- 指数日线和成分权重直接读分析库（指数不复权，没有 qfq 表）。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from ..duckdb_analytics import connect_analytics_db

logger = logging.getLogger(__name__)

# 九转要 4 根前收盘、ATR14 要 14 根，留足预热
WARMUP_DAYS = 400


def load_index_bars(codes: Sequence[str], start: date, end: date,
                    *, connect=connect_analytics_db) -> Dict[str, List[Dict[str, Any]]]:
    """板块（指数）日线。字段名与个股 K 线保持一致，方便喂给同一套指标函数。"""
    codes = [str(code).upper() for code in dict.fromkeys(codes)]
    if not codes:
        return {}
    placeholders = ", ".join("?" for _ in codes)
    connection = connect()
    try:
        rows = connection.execute(
            f"""
            SELECT ts_code, trade_date, open, high, low, close, vol, amount
            FROM a_stock_index_daily
            WHERE ts_code IN ({placeholders}) AND trade_date BETWEEN ? AND ?
              AND open > 0 AND high > 0 AND low > 0 AND close > 0
            ORDER BY ts_code, trade_date
            """,
            [*codes, start, end],
        ).fetchall()
    finally:
        connection.close()
    result: Dict[str, List[Dict[str, Any]]] = {code: [] for code in codes}
    for ts_code, trade_date, open_price, high, low, close, volume, amount in rows:
        day = trade_date if isinstance(trade_date, date) else trade_date.date()
        result.setdefault(str(ts_code).upper(), []).append({
            "timestamp": datetime.combine(day, time(hour=15)),
            "open": float(open_price), "high": float(high), "low": float(low), "close": float(close),
            "volume": float(volume) if volume is not None else 0.0,
            "turnover": float(amount) if amount is not None else 0.0,
            "turnover_rate": None,
        })
    return result


def load_stock_bars(symbols: Sequence[str], start: date, end: date) -> Dict[str, List[Dict[str, Any]]]:
    """成分股前复权日 K（与个股详情页 K 线图同一入口）。"""
    from ...analytics_database import get_analytics_db_ctx
    from ..a_stock_consensus import load_a_stock_klines_batch

    symbols = [str(symbol).upper() for symbol in dict.fromkeys(symbols)]
    if not symbols:
        return {}
    result: Dict[str, List[Dict[str, Any]]] = {}
    with get_analytics_db_ctx() as db:
        for offset in range(0, len(symbols), 400):
            chunk = symbols[offset:offset + 400]
            result.update(load_a_stock_klines_batch(db, chunk, start_date=start, end_date=end))
    return result


def load_stock_names(symbols: Sequence[str], *, connect=connect_analytics_db) -> Dict[str, str]:
    symbols = [str(symbol).upper() for symbol in dict.fromkeys(symbols)]
    if not symbols:
        return {}
    placeholders = ", ".join("?" for _ in symbols)
    connection = connect()
    try:
        rows = connection.execute(
            f"SELECT ts_code, name FROM a_stock_basic WHERE ts_code IN ({placeholders})",
            list(symbols),
        ).fetchall()
    finally:
        connection.close()
    return {str(code).upper(): str(name or "") for code, name in rows}


def load_index_members(codes: Sequence[str], *, connect=connect_analytics_db) -> Dict[str, List[Dict[str, Any]]]:
    """每个指数的全部权重快照（按日期升序），调用方用 ``members_as_of`` 取 point-in-time 成分。"""
    codes = [str(code).upper() for code in dict.fromkeys(codes)]
    if not codes:
        return {}
    placeholders = ", ".join("?" for _ in codes)
    connection = connect()
    try:
        rows = connection.execute(
            f"""
            SELECT index_code, trade_date, con_code
            FROM a_stock_index_weight
            WHERE index_code IN ({placeholders})
            ORDER BY index_code, trade_date, con_code
            """,
            list(codes),
        ).fetchall()
    finally:
        connection.close()
    grouped: Dict[str, Dict[date, List[str]]] = {}
    for index_code, trade_date, con_code in rows:
        day = trade_date if isinstance(trade_date, date) else trade_date.date()
        grouped.setdefault(str(index_code).upper(), {}).setdefault(day, []).append(str(con_code).upper())
    return {
        code: [{"trade_date": day, "members": members} for day, members in sorted(snapshots.items())]
        for code, snapshots in grouped.items()
    }


def members_as_of(snapshots: Sequence[Mapping[str, Any]], day: date) -> List[str]:
    """信号日之前最近一期权重快照的成分；没有任何快照时返回空。"""
    chosen: List[str] = []
    for snapshot in snapshots or []:
        if snapshot["trade_date"] <= day:
            chosen = snapshot["members"]
        else:
            break
    return chosen


def load_fear_scores(codes: Sequence[str], as_of: date) -> Dict[str, Dict[date, float]]:
    """各板块截至 as_of 的自算贪恐分数（与贪恐曲线页面同一入口）。"""
    from ..stock_system.sentiment import CalculatorHistoryLoader

    loader = CalculatorHistoryLoader()
    result: Dict[str, Dict[date, float]] = {}
    for code in dict.fromkeys(str(item).upper() for item in codes):
        series: Dict[date, float] = {}
        try:
            rows = loader(code, as_of)
        except Exception as exc:  # 单个板块缺贪恐数据不应拖垮整次计算
            logger.warning("sector nine turn: fear history failed for %s: %s", code, exc)
            rows = []
        for row in rows:
            raw_date = row.get("date")
            score = row.get("score")
            if raw_date is None or score is None:
                continue
            day = raw_date if isinstance(raw_date, date) else _parse_date(raw_date)
            if day is None:
                continue
            try:
                series[day] = float(score)
            except (TypeError, ValueError):
                continue
        result[code] = series
    return result


def _parse_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "")[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def latest_trade_date(as_of: Optional[date] = None, *, connect=connect_analytics_db) -> Optional[date]:
    """不晚于 as_of 的最近一个 A 股交易日。"""
    connection = connect()
    try:
        row = connection.execute(
            "SELECT max(trade_date) FROM a_stock_market_daily WHERE trade_date <= ?",
            [as_of or date.today()],
        ).fetchone()
    finally:
        connection.close()
    if not row or row[0] is None:
        return None
    return row[0] if isinstance(row[0], date) else row[0].date()


def bar_dates(bars: Iterable[Mapping[str, Any]]) -> List[date]:
    return [bar["timestamp"].date() for bar in bars]


def warmup_start(start: date) -> date:
    return start - timedelta(days=WARMUP_DAYS)

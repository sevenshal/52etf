"""页面读取用的快照查询：板块状态、个股信号、最近有信号的交易日。"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from ..duckdb_analytics import connect_analytics_db, duckdb_table_exists
from ..stock_system.storage import clean_value

SECTOR_TABLE = "sector_nine_turn_sector_snapshot"
SIGNAL_TABLE = "sector_nine_turn_signal_snapshot"


def _rows(connection, sql: str, params: List[Any]) -> List[Dict[str, Any]]:
    cursor = connection.execute(sql, params)
    columns = [item[0] for item in cursor.description]
    return [{column: clean_value(value) for column, value in zip(columns, row)} for row in cursor.fetchall()]


def load_daily_view(trade_date: Optional[date] = None) -> Dict[str, Any]:
    """某个交易日的板块状态和个股信号；不传日期时取最新有快照的那天。"""
    connection = connect_analytics_db()
    try:
        if not duckdb_table_exists(connection, SECTOR_TABLE):
            return {"trade_date": None, "sectors": [], "signals": [], "available_dates": []}
        if trade_date is None:
            row = connection.execute(f"SELECT max(trade_date) FROM {SECTOR_TABLE}").fetchone()
            trade_date = row[0] if row and row[0] else None
            if isinstance(trade_date, str):
                trade_date = date.fromisoformat(trade_date)
        if trade_date is None:
            return {"trade_date": None, "sectors": [], "signals": [], "available_dates": []}
        sectors = _rows(connection, f"""
            SELECT * FROM {SECTOR_TABLE} WHERE trade_date = ?
            ORDER BY armed DESC, turn_signal DESC, fear_score NULLS LAST, index_code
        """, [trade_date])
        signals = []
        if duckdb_table_exists(connection, SIGNAL_TABLE):
            signals = _rows(connection, f"""
                SELECT * FROM {SIGNAL_TABLE} WHERE trade_date = ?
                ORDER BY CASE action WHEN 'buy' THEN 0 WHEN 'sell' THEN 1 WHEN 'hold' THEN 2 ELSE 3 END,
                         rank NULLS LAST, ts_code
            """, [trade_date])
        available = [
            item["trade_date"]
            for item in _rows(connection, f"""
                SELECT DISTINCT trade_date FROM {SECTOR_TABLE} ORDER BY trade_date DESC LIMIT 120
            """, [])
        ]
    finally:
        connection.close()
    return {
        "trade_date": trade_date.isoformat(),
        "sectors": sectors,
        "signals": signals,
        "available_dates": available,
    }


def load_signal_history(limit: int = 200) -> List[Dict[str, Any]]:
    """最近的买卖信号流水（跨交易日），页面上用来看策略最近在做什么。"""
    connection = connect_analytics_db()
    try:
        if not duckdb_table_exists(connection, SIGNAL_TABLE):
            return []
        return _rows(connection, f"""
            SELECT trade_date, ts_code, name, role, sector_code, sector_name, sector_fear_score,
                   close, atr, rising_drawdown_atr, action, rank, note
            FROM {SIGNAL_TABLE}
            WHERE action IN ('buy', 'sell')
            ORDER BY trade_date DESC, CASE action WHEN 'buy' THEN 0 ELSE 1 END, rank NULLS LAST, ts_code
            LIMIT ?
        """, [int(limit)])
    finally:
        connection.close()

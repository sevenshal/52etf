"""股票池范围：剔除 ST、近 N 日平均市值/成交额不达标的股票。

全部按 as_of 当时的状态判断（point-in-time）：
- 行情取 as_of 及之前最近 N 个交易日，停牌日没有行情就不参与平均；
- ST 按 as_of 当时的股票简称判断（优先用曾用名表，缺失时退回当前简称）；
- 在这 N 个交易日里一天都没有行情的股票（未上市、已退市、长期停牌）不入池。

单位换算：tushare 的 total_mv 是万元、amount 是千元，这里统一成 亿元 / 万元。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Mapping, Optional

import pandas as pd

from ..duckdb_analytics import duckdb_table_exists


def _recent_trade_dates(connection, as_of: date, count: int) -> List[date]:
    # 给日历天留足余量（长假 + 停市），避免扫全表
    lower_bound = as_of - timedelta(days=count * 3 + 20)
    rows = connection.execute(
        """
        SELECT DISTINCT trade_date
        FROM a_stock_market_daily
        WHERE trade_date <= ? AND trade_date >= ?
        ORDER BY trade_date DESC
        LIMIT ?
        """,
        [as_of, lower_bound, int(count)],
    ).fetchall()
    return [row[0] if isinstance(row[0], date) else pd.Timestamp(row[0]).date() for row in rows]


def _names_as_of(connection, as_of: date) -> Dict[str, str]:
    """as_of 当天的股票简称（曾用名表），用来判断当时是否 ST。"""
    if not duckdb_table_exists(connection, "a_stock_name_changes"):
        return {}
    frame = connection.execute(
        """
        SELECT ts_code, name
        FROM a_stock_name_changes
        WHERE start_date <= ? AND (end_date IS NULL OR end_date >= ?) AND name IS NOT NULL
        QUALIFY ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY start_date DESC) = 1
        """,
        [as_of, as_of],
    ).fetchdf()
    return {str(row.ts_code): str(row.name) for row in frame.itertuples()}


def is_st_name(name: Optional[str]) -> bool:
    """ST / *ST，以及退市整理期（简称带"退"）。"""
    text = str(name or "").strip().upper()
    return "ST" in text or text.endswith("退")


def load_universe(connection, as_of: date, config: Mapping[str, Any]) -> Dict[str, Any]:
    """返回 ``{"trade_date", "trade_dates", "frame"}``。

    frame 每行一只在窗口内有行情的股票，带 ``in_universe`` 和未入池原因
    ``universe_reason``，未入池的也保留，方便页面解释"为什么没有它"。
    """
    universe_config = config["universe"]
    window = int(universe_config["avg_window_days"])
    trade_dates = _recent_trade_dates(connection, as_of, window)
    empty = pd.DataFrame(
        columns=[
            "ts_code", "name", "industry", "is_st", "window_days", "close",
            "avg_total_mv_100m", "avg_amount_10k", "in_universe", "universe_reason",
        ]
    )
    if not trade_dates:
        return {"trade_date": None, "trade_dates": [], "frame": empty}

    placeholders = ", ".join("?" for _ in trade_dates)
    market = connection.execute(
        f"""
        SELECT ts_code,
               COUNT(*) AS window_days,
               AVG(total_mv) AS avg_total_mv,
               AVG(amount) AS avg_amount,
               arg_max(close, trade_date) AS close
        FROM a_stock_market_daily
        WHERE trade_date IN ({placeholders})
        GROUP BY ts_code
        """,
        list(trade_dates),
    ).fetchdf()
    if market.empty:
        return {"trade_date": trade_dates[0], "trade_dates": trade_dates, "frame": empty}

    basic = connection.execute("SELECT ts_code, name, industry FROM a_stock_basic").fetchdf()
    basic_by_symbol = basic.set_index("ts_code").to_dict("index") if not basic.empty else {}
    names_then = _names_as_of(connection, trade_dates[0])

    rows: List[Dict[str, Any]] = []
    for item in market.itertuples():
        ts_code = str(item.ts_code)
        info = basic_by_symbol.get(ts_code, {})
        name = names_then.get(ts_code) or info.get("name")
        avg_total_mv = float(item.avg_total_mv) / 1e4 if pd.notna(item.avg_total_mv) else None
        avg_amount = float(item.avg_amount) / 10.0 if pd.notna(item.avg_amount) else None
        st = is_st_name(name)

        reason = None
        if universe_config["exclude_st"] and st:
            reason = "ST/退市整理"
        elif avg_total_mv is None or avg_total_mv < universe_config["min_avg_total_mv_100m"]:
            reason = f"近{window}日平均市值不足{universe_config['min_avg_total_mv_100m']:g}亿"
        elif avg_amount is None or avg_amount < universe_config["min_avg_amount_10k"]:
            reason = f"近{window}日平均成交额不足{universe_config['min_avg_amount_10k']:g}万"

        rows.append({
            "ts_code": ts_code,
            "name": name,
            "industry": info.get("industry"),
            "is_st": st,
            "window_days": int(item.window_days),
            "close": float(item.close) if pd.notna(item.close) else None,
            "avg_total_mv_100m": avg_total_mv,
            "avg_amount_10k": avg_amount,
            "in_universe": reason is None,
            "universe_reason": reason,
        })
    return {"trade_date": trade_dates[0], "trade_dates": trade_dates, "frame": pd.DataFrame(rows)}

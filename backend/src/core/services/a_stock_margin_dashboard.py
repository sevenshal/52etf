"""全 A 两融杠杆看板的数据读取与指标计算。"""
from __future__ import annotations

from bisect import bisect_left, bisect_right, insort
from collections import deque
from typing import Any, Dict, List

from ..analytics_database import AStockMarginDaily  # noqa: F401 - import 阶段确保分析表升级完成
from .duckdb_analytics import connect_analytics_db, duckdb_query_dicts, safe_float


class MarginDashboardDataError(RuntimeError):
    pass


YI = 100_000_000.0
PERCENTILE_WINDOW = 1250  # 约五个交易年


def _rolling_percentile(rows: List[Dict[str, Any]], field: str) -> None:
    """原始值在过去 1250 个有效交易日中的百分位（含当日）。"""
    values: List[float] = []
    window = deque()
    for row in rows:
        value = safe_float(row.get(field))
        if value is None:
            row[f"{field}_percentile_5y"] = None
            continue
        insort(values, value)
        window.append(value)
        if len(window) > PERCENTILE_WINDOW:
            oldest = window.popleft()
            values.pop(bisect_left(values, oldest))
        row[f"{field}_percentile_5y"] = round(bisect_right(values, value) / len(values) * 100, 1)


def _load_rows() -> List[Dict[str, Any]]:
    connection = connect_analytics_db()
    try:
        rows = duckdb_query_dicts(connection, """
            WITH margin AS (
                SELECT
                    trade_date,
                    SUM(rzye) AS financing_balance_yuan,
                    SUM(rqye) AS short_balance_yuan,
                    SUM(rzrqye) AS margin_balance_yuan,
                    SUM(rzmre - rzche) AS financing_net_buy_yuan,
                    MAX(CASE WHEN exchange_id = 'SSE' THEN 1 ELSE 0 END) AS has_sse,
                    MAX(CASE WHEN exchange_id = 'SZSE' THEN 1 ELSE 0 END) AS has_szse
                FROM a_stock_margin_daily
                GROUP BY trade_date
            ), market AS (
                SELECT trade_date, SUM(circ_mv) * 10000.0 AS float_market_value_yuan
                FROM a_stock_market_daily
                WHERE circ_mv IS NOT NULL AND circ_mv > 0
                GROUP BY trade_date
            )
            SELECT
                margin.trade_date,
                financing_balance_yuan / ? AS financing_balance_yi,
                short_balance_yuan / ? AS short_balance_yi,
                margin_balance_yuan / ? AS margin_balance_yi,
                financing_net_buy_yuan / ? AS financing_net_buy_yi,
                float_market_value_yuan / ? AS float_market_value_yi,
                financing_balance_yuan / NULLIF(float_market_value_yuan, 0) * 100 AS financing_ratio_pct,
                short_balance_yuan / NULLIF(float_market_value_yuan, 0) * 100 AS short_ratio_pct,
                margin_balance_yuan / NULLIF(float_market_value_yuan, 0) * 100 AS leverage_ratio_pct
            FROM margin
            INNER JOIN market USING (trade_date)
            WHERE has_sse = 1 AND has_szse = 1
            ORDER BY margin.trade_date
        """, [YI, YI, YI, YI, YI])
    except Exception as exc:  # 表首次随代码启动创建；创建前给页面清晰提示
        raise MarginDashboardDataError("两融数据尚未同步，请先运行“A股基础数据同步”任务") from exc
    finally:
        connection.close()
    return rows


def _load_incomplete_margin_dates() -> List[Dict[str, Any]]:
    """识别沪深任一交易所缺失的日期，绝不让它们参与比例/分位计算。"""
    connection = connect_analytics_db()
    try:
        return duckdb_query_dicts(connection, """
            SELECT
                trade_date,
                STRING_AGG(exchange_id, ', ' ORDER BY exchange_id) AS available_exchanges
            FROM a_stock_margin_daily
            GROUP BY trade_date
            HAVING SUM(CASE WHEN exchange_id = 'SSE' THEN 1 ELSE 0 END) = 0
                OR SUM(CASE WHEN exchange_id = 'SZSE' THEN 1 ELSE 0 END) = 0
            ORDER BY trade_date DESC
            LIMIT 20
        """)
    finally:
        connection.close()


def _load_industry_rows() -> Dict[str, Any]:
    """取有明细的最新交易日，按申万一级行业聚合两融与流通市值。"""
    connection = connect_analytics_db()
    try:
        latest_row = connection.execute("SELECT MAX(trade_date) FROM a_stock_margin_detail_daily").fetchone()
        latest_date = latest_row[0] if latest_row else None
        if latest_date is None:
            return {"trade_date": None, "items": []}
        items = duckdb_query_dicts(connection, """
            WITH stocks AS (
                SELECT
                    d.ts_code, d.rzye, d.rqye, d.rzmre, d.rzche,
                    m.circ_mv * 10000.0 AS float_market_value_yuan,
                    COALESCE(NULLIF(TRIM(sw.l1_name), ''), '未分类') AS industry
                FROM a_stock_margin_detail_daily d
                INNER JOIN a_stock_market_daily m
                    ON m.trade_date = d.trade_date AND m.ts_code = d.ts_code
                LEFT JOIN a_stock_sw_member sw ON sw.ts_code = d.ts_code
                WHERE d.trade_date = ?
            )
            SELECT
                industry,
                COUNT(*) AS stock_count,
                SUM(rzye) / ? AS financing_balance_yi,
                SUM(rqye) / ? AS short_balance_yi,
                SUM(rzmre - rzche) / ? AS financing_net_buy_yi,
                SUM(float_market_value_yuan) / ? AS float_market_value_yi,
                SUM(rzye) / NULLIF(SUM(float_market_value_yuan), 0) * 100 AS financing_ratio_pct,
                SUM(rqye) / NULLIF(SUM(float_market_value_yuan), 0) * 100 AS short_ratio_pct,
                SUM(rzye + rqye) / NULLIF(SUM(float_market_value_yuan), 0) * 100 AS leverage_ratio_pct
            FROM stocks
            GROUP BY industry
            HAVING SUM(float_market_value_yuan) > 0
            ORDER BY financing_ratio_pct DESC NULLS LAST, financing_balance_yi DESC
        """, [latest_date, YI, YI, YI, YI])
        return {"trade_date": latest_date.isoformat(), "items": items}
    except Exception as exc:
        raise MarginDashboardDataError("行业两融明细尚未同步，请运行“A股两融明细同步”任务") from exc
    finally:
        connection.close()


def fetch_margin_dashboard(days: int = 1250) -> Dict[str, Any]:
    rows = _load_rows()
    if not rows:
        raise MarginDashboardDataError("两融数据尚未同步，请先运行“A股基础数据同步”任务")
    for field in ("financing_ratio_pct", "leverage_ratio_pct"):
        _rolling_percentile(rows, field)
    latest = dict(rows[-1])
    visible = rows[-max(20, int(days)):]
    return {
        "latest": latest,
        "history": visible,
        "available_start_date": rows[0]["trade_date"],
        "available_end_date": latest["trade_date"],
        "data_quality": {
            "required_exchanges": ["SSE", "SZSE"],
            "excluded_incomplete_dates": _load_incomplete_margin_dates(),
        },
        "methodology": {
            "financing_ratio": "融资余额 ÷ 全A股流通市值",
            "leverage_ratio": "融资融券余额 ÷ 全A股流通市值",
            "percentile": "当前值在过去1250个有效交易日（约5年）中的百分位，含当日",
            "note": "两融余额来自交易所汇总，可能包含少量ETF等非股票融资融券标的；分母为Tushare A股日度流通市值之和。",
        },
        "industry": _load_industry_rows(),
    }

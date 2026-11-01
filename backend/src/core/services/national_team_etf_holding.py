"""沪深300核心 ETF 资产规模代理看板。

基金份额乘二级市场收盘价是可复核的 ETF 市值近似，并非任何主体的实际持仓披露；
这里仅将规模最大的四只、精确跟踪沪深300的场内 ETF 合并，作为观察政策资金
可能通过宽基 ETF 配置大盘蓝筹的一个代理指标。
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right, insort
from collections import deque
from typing import Any, Dict, List

from .duckdb_analytics import connect_analytics_db, duckdb_query_dicts, safe_float


YI = 100_000_000.0
PERCENTILE_WINDOW = 1250
HS300_ETFS = (
    ("510300.SH", "华泰柏瑞沪深300ETF"),
    ("510310.SH", "易方达沪深300ETF"),
    ("510330.SH", "华夏沪深300ETF"),
    ("159919.SZ", "嘉实沪深300ETF"),
)


class NationalTeamHoldingDataError(RuntimeError):
    pass


def _rolling_percentile(rows: List[Dict[str, Any]], field: str) -> None:
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
            values.pop(bisect_left(values, window.popleft()))
        row[f"{field}_percentile_5y"] = round(bisect_right(values, value) / len(values) * 100, 1)


def _load_history() -> List[Dict[str, Any]]:
    symbols = [symbol for symbol, _ in HS300_ETFS]
    placeholders = ", ".join("?" for _ in symbols)
    connection = connect_analytics_db()
    try:
        # 成分股采用该交易日可获得的最近一期指数成分表；分母是成分股未调整流通市值之和。
        return duckdb_query_dicts(connection, f"""
            WITH etf_daily AS (
                SELECT s.trade_date, s.ts_code, s.fd_share * 10000.0 * d.close AS fund_market_value_yuan
                FROM a_stock_fund_share s
                INNER JOIN a_stock_fund_daily d USING (ts_code, trade_date)
                WHERE s.ts_code IN ({placeholders})
                    AND s.fd_share > 0 AND d.close > 0
            ), etf AS (
                SELECT trade_date, COUNT(DISTINCT ts_code) AS etf_count,
                    SUM(fund_market_value_yuan) AS etf_market_value_yuan
                FROM etf_daily
                GROUP BY trade_date
                HAVING COUNT(DISTINCT ts_code) = {len(symbols)}
            ), component_market AS (
                SELECT e.trade_date, SUM(m.circ_mv) * 10000.0 AS hs300_float_market_value_yuan,
                    COUNT(DISTINCT m.ts_code) AS component_count
                FROM etf e
                JOIN LATERAL (
                    SELECT MAX(trade_date) AS weight_date
                    FROM a_stock_index_weight
                    WHERE index_code = '000300.SH' AND trade_date <= e.trade_date
                ) wd ON wd.weight_date IS NOT NULL
                JOIN a_stock_index_weight w
                    ON w.index_code = '000300.SH' AND w.trade_date = wd.weight_date
                JOIN a_stock_market_daily m
                    ON m.trade_date = e.trade_date AND m.ts_code = w.con_code
                WHERE m.circ_mv > 0
                GROUP BY e.trade_date
            )
            SELECT e.trade_date,
                e.etf_count,
                c.component_count,
                idx.close AS hs300_close,
                e.etf_market_value_yuan / ? AS etf_market_value_yi,
                c.hs300_float_market_value_yuan / ? AS hs300_float_market_value_yi,
                e.etf_market_value_yuan / NULLIF(c.hs300_float_market_value_yuan, 0) * 100 AS holding_ratio_pct
            FROM etf e
            INNER JOIN component_market c USING (trade_date)
            LEFT JOIN a_stock_index_daily idx
                ON idx.trade_date = e.trade_date AND idx.ts_code = '000300.SH'
            WHERE c.component_count >= 290
            ORDER BY e.trade_date
        """, [*symbols, YI, YI])
    except Exception as exc:  # noqa: BLE001
        raise NationalTeamHoldingDataError("核心沪深300ETF或指数成分数据尚未同步") from exc
    finally:
        connection.close()


def _load_latest_funds(trade_date: Any, total_yi: float) -> List[Dict[str, Any]]:
    symbols = [symbol for symbol, _ in HS300_ETFS]
    names = dict(HS300_ETFS)
    placeholders = ", ".join("?" for _ in symbols)
    connection = connect_analytics_db()
    try:
        rows = duckdb_query_dicts(connection, f"""
            SELECT s.ts_code, s.fd_share, d.close,
                s.fd_share * 10000.0 * d.close / ? AS market_value_yi
            FROM a_stock_fund_share s
            INNER JOIN a_stock_fund_daily d USING (ts_code, trade_date)
            WHERE s.trade_date = ? AND s.ts_code IN ({placeholders})
            ORDER BY market_value_yi DESC
        """, [YI, trade_date, *symbols])
    finally:
        connection.close()
    for row in rows:
        row["name"] = names.get(row["ts_code"], row["ts_code"])
        value = safe_float(row.get("market_value_yi")) or 0
        row["weight_pct"] = round(value / total_yi * 100, 2) if total_yi else None
    return rows


def fetch_national_team_etf_holding(days: int = 1250) -> Dict[str, Any]:
    rows = _load_history()
    if not rows:
        raise NationalTeamHoldingDataError("暂无完整的四只核心沪深300ETF份额、行情及成分股流通市值数据")
    _rolling_percentile(rows, "holding_ratio_pct")
    latest = dict(rows[-1])
    latest["funds"] = _load_latest_funds(latest["trade_date"], latest["etf_market_value_yi"])
    visible = rows[-max(20, int(days)):]
    return {
        "latest": latest,
        "history": visible,
        "basket": [{"symbol": symbol, "name": name} for symbol, name in HS300_ETFS],
        "methodology": {
            "name": "国家队ETF持仓占比",
            "formula": "四只核心沪深300ETF份额 × 收盘价之和 ÷ 沪深300成分股流通市值之和",
            "percentile": "当前占比在过去1250个有效交易日（约5年）中的百分位，含当日",
            "note": "这是核心宽基ETF资产规模代理，不是国家队或任何机构的实际持仓披露；ETF二级市场价格用于近似基金市值，分母为最近一期成分表对应成分股的未调整流通市值。",
        },
    }

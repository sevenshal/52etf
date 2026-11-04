"""A 股拥挤度看板的本地 DuckDB 计算。"""
from __future__ import annotations

from bisect import bisect_left, bisect_right, insort
from collections import deque
from typing import Any, Dict, List, Tuple

from .duckdb_analytics import connect_analytics_db, duckdb_query_dicts, safe_float


class CrowdingDataError(RuntimeError):
    pass


LOOKBACK_DAYS = 1250
COMPONENTS: Tuple[Tuple[str, str, float], ...] = (
    ("amount_to_float_mv_pct", "成交额/流通市值", 0.18),
    ("turnover_rate_pct", "市值加权换手率", 0.13),
    ("leverage_ratio_pct", "两融杠杆率", 0.14),
    ("main_flow_ratio_pct", "主力净流入/流通市值", 0.13),
    ("up_ratio_pct", "上涨家数占比", 0.10),
    ("pb", "市值加权PB", 0.12),
    ("amount_hhi", "成交集中度(HHI)", 0.10),
    ("return_dispersion_pct", "收益离散度（波动）", 0.10),
)


def _rolling_percentiles(rows: List[Dict[str, Any]]) -> None:
    for field, _, _ in COMPONENTS:
        ordered: List[float] = []
        window = deque()
        for row in rows:
            value = safe_float(row.get(field))
            if value is None:
                row[f"{field}_percentile"] = None
                continue
            insort(ordered, value)
            window.append(value)
            if len(window) > LOOKBACK_DAYS:
                ordered.pop(bisect_left(ordered, window.popleft()))
            row[f"{field}_percentile"] = round(bisect_right(ordered, value) / len(ordered) * 100, 1)
    for row in rows:
        score, weight_total = 0.0, 0.0
        for field, _, weight in COMPONENTS:
            percentile = safe_float(row.get(f"{field}_percentile"))
            if percentile is not None:
                score += percentile * weight
                weight_total += weight
        row["crowding_score"] = round(score / weight_total, 1) if weight_total else None


def _scope_clause(scope: str, entity: str) -> Tuple[str, List[Any], str, str]:
    if scope == "market":
        return "", [], "", "全市场"
    if scope == "industry":
        if not entity:
            raise ValueError("请选择申万一级行业")
        return "INNER JOIN a_stock_sw_member sw ON sw.ts_code = m.ts_code", [entity], "AND COALESCE(NULLIF(TRIM(sw.l1_name), ''), '未分类') = ?", entity
    if scope == "index":
        if not entity:
            raise ValueError("请选择指数")
        return (
            "INNER JOIN (SELECT DISTINCT con_code FROM a_stock_index_weight WHERE index_code = ? "
            "AND trade_date = (SELECT MAX(trade_date) FROM a_stock_index_weight WHERE index_code = ?)) iw ON iw.con_code = m.ts_code",
            [entity, entity], "", entity,
        )
    raise ValueError("scope 仅支持 market、index 或 industry")


def _load_rows(scope: str, entity: str) -> List[Dict[str, Any]]:
    join_sql, params, where_sql, _ = _scope_clause(scope, entity)
    query = f"""
        WITH entity AS (
            SELECT m.trade_date, m.ts_code, m.amount, m.circ_mv, m.turnover_rate, m.pct_chg, m.pb,
                   f.main_net, d.rzrqye, d.rzmre, d.rzche
            FROM a_stock_market_daily m {join_sql}
            LEFT JOIN a_stock_fund_flow_daily f ON f.trade_date = m.trade_date AND f.ts_code = m.ts_code
            LEFT JOIN a_stock_margin_detail_daily d ON d.trade_date = m.trade_date AND d.ts_code = m.ts_code
            WHERE m.circ_mv IS NOT NULL AND m.circ_mv > 0 {where_sql}
        ), shares AS (
            SELECT *, amount / NULLIF(SUM(amount) OVER (PARTITION BY trade_date), 0) AS amount_share
            FROM entity WHERE amount IS NOT NULL AND amount > 0
        ), daily AS (
            SELECT trade_date, COUNT(*) AS stock_count,
                SUM(amount) / NULLIF(SUM(circ_mv), 0) * 10.0 AS amount_to_float_mv_pct,
                SUM(turnover_rate * circ_mv) / NULLIF(SUM(CASE WHEN turnover_rate IS NOT NULL THEN circ_mv END), 0) AS turnover_rate_pct,
                SUM(CASE WHEN pct_chg > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) AS up_ratio_pct,
                SUM(CASE WHEN pct_chg >= 9.5 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) AS limit_up_ratio_pct,
                STDDEV_POP(pct_chg) AS return_dispersion_pct,
                SUM(circ_mv) / NULLIF(SUM(CASE WHEN pb > 0 THEN circ_mv / pb END), 0) AS pb,
                SUM(main_net) / NULLIF(SUM(circ_mv) * 10000.0, 0) * 100.0 AS main_flow_ratio_pct,
                SUM(rzrqye) / NULLIF(SUM(circ_mv) * 10000.0, 0) * 100.0 AS leverage_ratio_pct,
                SUM(rzmre - rzche) / NULLIF(SUM(circ_mv) * 10000.0, 0) * 100.0 AS financing_net_buy_ratio_pct
            FROM entity GROUP BY trade_date
        ), hhi AS (SELECT trade_date, SUM(amount_share * amount_share) * 10000.0 AS amount_hhi FROM shares GROUP BY trade_date)
        SELECT daily.*, hhi.amount_hhi FROM daily LEFT JOIN hhi USING (trade_date) ORDER BY trade_date
    """
    connection = connect_analytics_db()
    try:
        return duckdb_query_dicts(connection, query, params)
    except Exception as exc:  # noqa: BLE001
        raise CrowdingDataError("拥挤度所需的日线、资金流或两融数据尚未完整同步") from exc
    finally:
        connection.close()


def _scope_options() -> Dict[str, List[Dict[str, str]]]:
    connection = connect_analytics_db()
    try:
        return {
            "industries": duckdb_query_dicts(connection, "SELECT DISTINCT TRIM(l1_name) AS value, TRIM(l1_name) AS label FROM a_stock_sw_member WHERE TRIM(COALESCE(l1_name, '')) <> '' ORDER BY label"),
            # a_stock_index_daily 的既有表结构只有行情字段，没有指数名称；名称由前端
            # 代码映射或用户选择时补充，不能让可选项查询依赖一个不存在的列。
            "indexes": duckdb_query_dicts(connection, "SELECT DISTINCT index_code AS value, index_code AS label FROM a_stock_index_weight ORDER BY label"),
        }
    finally:
        connection.close()


def _load_option_sentiment() -> Dict[str, Any]:
    """全市场期权成交量/持仓量 PCR；缺表时只返回空值，不影响主看板。"""
    connection = connect_analytics_db()
    try:
        rows = duckdb_query_dicts(connection, """
            SELECT d.trade_date,
                SUM(CASE WHEN b.call_put = 'P' THEN COALESCE(d.vol, 0) ELSE 0 END) / NULLIF(SUM(CASE WHEN b.call_put = 'C' THEN COALESCE(d.vol, 0) ELSE 0 END), 0) AS volume_pcr,
                SUM(CASE WHEN b.call_put = 'P' THEN COALESCE(d.oi, 0) ELSE 0 END) / NULLIF(SUM(CASE WHEN b.call_put = 'C' THEN COALESCE(d.oi, 0) ELSE 0 END), 0) AS oi_pcr
            FROM a_stock_option_daily d JOIN a_stock_option_basic b ON b.ts_code = d.ts_code
            WHERE b.call_put IN ('C', 'P') GROUP BY d.trade_date ORDER BY d.trade_date DESC LIMIT 1
        """)
        return rows[0] if rows else {"trade_date": None, "volume_pcr": None, "oi_pcr": None}
    except Exception:  # 期权历史尚未同步时的可选项目
        return {"trade_date": None, "volume_pcr": None, "oi_pcr": None}
    finally:
        connection.close()


def fetch_crowding_dashboard(scope: str = "market", entity: str = "", days: int = 250) -> Dict[str, Any]:
    scope, entity = str(scope or "market").strip().lower(), str(entity or "").strip()
    rows = _load_rows(scope, entity)
    if not rows:
        raise CrowdingDataError("当前范围没有可计算的拥挤度数据，请先完成 A 股基础数据同步")
    _rolling_percentiles(rows)
    _, _, _, scope_label = _scope_clause(scope, entity)
    latest = dict(rows[-1])
    return {
        "scope": scope, "entity": entity, "scope_label": scope_label, "latest": latest,
        "history": rows[-max(20, min(int(days), LOOKBACK_DAYS)):],
        "components": [{"key": key, "label": label, "weight": weight, "value": latest.get(key), "percentile": latest.get(f"{key}_percentile")} for key, label, weight in COMPONENTS],
        "options": _scope_options(),
        "option_sentiment": _load_option_sentiment(),
        "methodology": {
            "lookback_days": LOOKBACK_DAYS,
            "score": "各可用分项在过去1250个有效交易日（约5年、含当日）的分位，按权重归一后加总；缺失分项不以零分替代。",
            "index_membership": "指数按本地已同步的最近一期成分权重确定成分，历史序列使用这套成分计算。",
            "scope": "行业按当前申万一级归属聚合；两融指标仅覆盖可融资融券标的。",
        },
    }

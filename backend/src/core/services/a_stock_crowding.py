"""A 股成交额集中度看板。

唯一口径：每个交易日按股票成交额降序，取成交额最高的前 5% 股票，
其成交额之和除以全市场成交额。该比例超过 45% 定义为极端拥挤。
"""
from __future__ import annotations

from typing import Any, Dict, List

from .duckdb_analytics import connect_analytics_db, duckdb_query_dicts, safe_float


class CrowdingDataError(RuntimeError):
    pass


TOP_RATIO = 0.05
EXTREME_THRESHOLD_PCT = 45.0


def classify_concentration(value: Any) -> str:
    ratio = safe_float(value)
    return "极端拥挤" if ratio is not None and ratio > EXTREME_THRESHOLD_PCT else "常态"


def _load_history() -> List[Dict[str, Any]]:
    connection = connect_analytics_db()
    try:
        return duckdb_query_dicts(connection, """
            WITH ranked AS (
                SELECT trade_date, ts_code, amount,
                    ROW_NUMBER() OVER (PARTITION BY trade_date ORDER BY amount DESC, ts_code) AS rank,
                    COUNT(*) OVER (PARTITION BY trade_date) AS stock_count,
                    SUM(amount) OVER (PARTITION BY trade_date) AS total_amount
                FROM a_stock_market_daily
                WHERE amount IS NOT NULL AND amount > 0
            ), daily AS (
                SELECT trade_date, MAX(stock_count) AS stock_count, MAX(total_amount) AS total_amount,
                    CEIL(MAX(stock_count) * ?) AS top_stock_count,
                    SUM(CASE WHEN rank <= CEIL(stock_count * ?) THEN amount ELSE 0 END) AS top_amount
                FROM ranked GROUP BY trade_date
            )
            SELECT trade_date, stock_count, top_stock_count,
                total_amount / 100000.0 AS total_amount_yi,
                top_amount / 100000.0 AS top_amount_yi,
                top_amount / NULLIF(total_amount, 0) * 100.0 AS concentration_pct
            FROM daily ORDER BY trade_date
        """, [TOP_RATIO, TOP_RATIO])
    except Exception as exc:  # noqa: BLE001
        raise CrowdingDataError("成交额集中度所需的全市场日行情尚未同步") from exc
    finally:
        connection.close()


def _load_latest_constituents(trade_date: str) -> List[Dict[str, Any]]:
    connection = connect_analytics_db()
    try:
        return duckdb_query_dicts(connection, """
            WITH ranked AS (
                SELECT m.ts_code, COALESCE(b.name, m.ts_code) AS name, m.amount,
                    ROW_NUMBER() OVER (ORDER BY m.amount DESC, m.ts_code) AS rank,
                    COUNT(*) OVER () AS stock_count,
                    SUM(m.amount) OVER () AS total_amount
                FROM a_stock_market_daily m
                LEFT JOIN a_stock_basic b ON b.ts_code = m.ts_code
                WHERE m.trade_date = ? AND m.amount IS NOT NULL AND m.amount > 0
            )
            SELECT ts_code, name, rank, amount / 100000.0 AS amount_yi,
                amount / NULLIF(total_amount, 0) * 100.0 AS market_amount_share_pct,
                SUM(amount) OVER (ORDER BY rank) / NULLIF(total_amount, 0) * 100.0 AS cumulative_market_share_pct
            FROM ranked
            WHERE rank <= CEIL(stock_count * ?)
            ORDER BY rank
        """, [trade_date, TOP_RATIO])
    except Exception as exc:  # noqa: BLE001
        raise CrowdingDataError("无法读取最新交易日的成交额前5%成分股") from exc
    finally:
        connection.close()


def _load_industry_composition(trade_date: str) -> List[Dict[str, Any]]:
    connection = connect_analytics_db()
    try:
        return duckdb_query_dicts(connection, """
            WITH ranked AS (
                SELECT m.ts_code, m.amount, ROW_NUMBER() OVER (ORDER BY m.amount DESC, m.ts_code) AS rank,
                    COUNT(*) OVER () AS stock_count
                FROM a_stock_market_daily m
                WHERE m.trade_date = ? AND m.amount IS NOT NULL AND m.amount > 0
            ), top_basket AS (
                SELECT r.amount, COALESCE(NULLIF(TRIM(sw.l1_name), ''), '未分类') AS industry
                FROM ranked r LEFT JOIN a_stock_sw_member sw ON sw.ts_code = r.ts_code
                WHERE r.rank <= CEIL(r.stock_count * ?)
            )
            SELECT industry, COUNT(*) AS stock_count, SUM(amount) / 100000.0 AS amount_yi,
                SUM(amount) / NULLIF(SUM(SUM(amount)) OVER (), 0) * 100.0 AS top_basket_share_pct
            FROM top_basket GROUP BY industry ORDER BY amount_yi DESC
        """, [trade_date, TOP_RATIO])
    except Exception:  # 行业表可能尚未同步，不影响核心指标
        return []
    finally:
        connection.close()


def _extreme_streak(rows: List[Dict[str, Any]]) -> int:
    streak = 0
    for row in reversed(rows):
        if safe_float(row.get("concentration_pct")) is None or row["concentration_pct"] <= EXTREME_THRESHOLD_PCT:
            break
        streak += 1
    return streak


def fetch_crowding_dashboard(days: int = 250) -> Dict[str, Any]:
    all_rows = _load_history()
    if not all_rows:
        raise CrowdingDataError("全市场日行情尚未同步，无法计算成交额集中度")
    latest = dict(all_rows[-1])
    latest["level"] = classify_concentration(latest.get("concentration_pct"))
    history = all_rows[-max(20, min(int(days), len(all_rows))):]
    return {
        "latest": latest,
        "history": history,
        "constituents": _load_latest_constituents(str(latest["trade_date"])),
        "industry_composition": _load_industry_composition(str(latest["trade_date"])),
        "extreme_streak_days": _extreme_streak(all_rows),
        "methodology": {
            "top_ratio_pct": TOP_RATIO * 100,
            "extreme_threshold_pct": EXTREME_THRESHOLD_PCT,
            "formula": "每日将全市场股票按成交额降序排序，取成交额最高的前5%股票；集中度 = 该篮子成交额之和 ÷ 全市场总成交额。",
            "threshold": "集中度严格高于45%定义为极端拥挤；等于45%仍按常态显示。",
            "unit": "Tushare daily.amount 的单位为千元，展示时换算为亿元；比值计算使用原始同单位成交额。",
        },
    }

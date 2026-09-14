"""美股指数（ETF）估值点位：复用「美股ETF 估值分析」任务每天写入 etf_analysis 的成分估值结果。

etf_analysis 每行在纽约收盘后（17:30 America/New_York，即上海次日清晨；早期为上海 09:00）
按当天 EVC 个股公允价值和持仓行情计算，行日期是上海日期 D，反映的是美股 D-1 的收盘：
- 周末/美股休市日对应的行只是重复前一收盘，丢弃；
- 上海 21:00 之后手动重跑的行已进入美股当日交易时段，价格不再是 D-1 收盘，丢弃，避免未来函数。

估值偏离 = 有估值成分的加权公允价值中枢 ÷ ETF 持仓净值 − 1，与 A股指数「成分一致预期估值中枢
相对指数点位」同口径（都是成分 公允价值/价格 的加权均值 − 1）；估值点位用 A股同一个分位函数逐日计算。
"""
from __future__ import annotations

import statistics
from datetime import date, time as dtime, timedelta
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional

from ..database import ETFAnalysis, Session
from .a_stock_index_valuation import (
    VALUATION_POSITION_MAX_WINDOW,
    VALUATION_POSITION_SHORT_WINDOW,
    _build_valuation_position_fields,
    _gap_pct,
    _positive_number,
    _valuation_ratio,
    build_valuation_position_history,
)
from .external_trading_market import _is_us_trading_day

INTRADAY_RERUN_CUTOFF = dtime(21, 0)
# 当天有估值成分的市值覆盖率低于近 20 个有效日中位数的 80%：说明那天 EVC 个股估值同步不完整
# （如 2026-07-23 只同步到 360 只股票，QQQ 覆盖率从 0.96 掉到 0.11），用剩下少数成分算出的偏离没有代表性，
# 丢弃当天值、沿用上一个有效值（ffill）。
COVERAGE_DROP_RATIO = 0.8
COVERAGE_BASELINE_DAYS = 20
# 同步不完整都是单日事件；连续这么多天覆盖率都低，说明是覆盖率的新常态，接受并以此重建基准，避免一直 ffill
MAX_CONSECUTIVE_FFILL_DAYS = 3
_ANALYSIS_COLUMNS = (
    "date",
    "current_price",
    "market_price",
    "forward_stocks_value_lo",
    "forward_stocks_value_hi",
    "forward_stocks_weight",
    "min_fair_value_date",
    "max_fair_value_date",
    "created_at",
    "updated_at",
)


def _analysis_us_trading_date(row: Any) -> Optional[date]:
    """etf_analysis 行（上海日期 D）→ 它反映的美股收盘日 D-1；不是收盘快照的行返回 None。"""
    analysis_date = getattr(row, "date", None)
    if analysis_date is None:
        return None
    computed_at = getattr(row, "updated_at", None) or getattr(row, "created_at", None)
    if computed_at is not None and computed_at.date() == analysis_date and computed_at.time() >= INTRADAY_RERUN_CUTOFF:
        return None
    us_date = analysis_date - timedelta(days=1)
    if not _is_us_trading_day(us_date):
        return None
    return us_date


def _is_coverage_collapse(coverage: Optional[float], recent_coverages: List[float]) -> bool:
    if not recent_coverages:
        return False
    if coverage is None:
        return True
    return coverage < COVERAGE_DROP_RATIO * statistics.median(recent_coverages)


def build_us_valuation_rows(analysis_rows: Iterable[Any]) -> List[Dict[str, Any]]:
    """按日期升序的 etf_analysis 行 → 按美股交易日的估值偏离序列（同一交易日保留最早的收盘快照）。

    覆盖率骤降（EVC 同步不完整）的日子沿用上一个有效值，并标记 is_ffilled。
    """
    rows: List[Dict[str, Any]] = []
    seen = set()
    recent_coverages: List[float] = []
    last_valid: Optional[Dict[str, Any]] = None
    consecutive_ffill = 0
    for row in analysis_rows:
        us_date = _analysis_us_trading_date(row)
        if us_date is None or us_date in seen:
            continue
        nav = _positive_number(getattr(row, "current_price", None))
        fair_lo = _positive_number(getattr(row, "forward_stocks_value_lo", None))
        fair_hi = _positive_number(getattr(row, "forward_stocks_value_hi", None))
        if nav is None or fair_lo is None or fair_hi is None:
            continue
        seen.add(us_date)
        coverage = _positive_number(getattr(row, "forward_stocks_weight", None))
        if consecutive_ffill < MAX_CONSECUTIVE_FFILL_DAYS and _is_coverage_collapse(coverage, recent_coverages):
            if last_valid is not None:
                rows.append({
                    **last_valid,
                    "date": us_date,
                    "analysis_date": row.date,
                    "coverage_ratio": coverage,
                    "is_ffilled": True,
                })
                consecutive_ffill += 1
            continue
        if consecutive_ffill >= MAX_CONSECUTIVE_FFILL_DAYS:
            recent_coverages = []
        consecutive_ffill = 0
        if coverage is not None:
            recent_coverages = (recent_coverages + [coverage])[-COVERAGE_BASELINE_DAYS:]
        fair_mid = (fair_lo + fair_hi) / 2.0
        last_valid = {
            "date": us_date,
            "analysis_date": row.date,
            "index_level": nav,
            "market_price": getattr(row, "market_price", None),
            "fair_value_lo": round(fair_lo, 4),
            "fair_value_mid": round(fair_mid, 4),
            "fair_value_hi": round(fair_hi, 4),
            "current_gap_pct": _gap_pct(fair_mid, nav),
            "coverage_ratio": coverage,
            "valuation_date_min": getattr(row, "min_fair_value_date", None),
            "valuation_date_max": getattr(row, "max_fair_value_date", None),
            "is_ffilled": False,
        }
        rows.append(last_valid)
    return rows


def _load_us_valuation_rows(symbol: str, end_date: Optional[date] = None) -> List[Dict[str, Any]]:
    normalized_symbol = str(symbol or "").strip().upper()
    db = Session()
    try:
        query = db.query(*(getattr(ETFAnalysis, column) for column in _ANALYSIS_COLUMNS)).filter(
            ETFAnalysis.symbol == normalized_symbol
        )
        if end_date is not None:
            # 美股 T 日的估值在上海 T+1 那一行
            query = query.filter(ETFAnalysis.date <= end_date + timedelta(days=1))
        analysis_rows = [
            SimpleNamespace(**dict(zip(_ANALYSIS_COLUMNS, values)))
            for values in query.order_by(ETFAnalysis.date.asc()).all()
        ]
    finally:
        Session.remove()
    rows = build_us_valuation_rows(analysis_rows)
    if end_date is not None:
        rows = [row for row in rows if row["date"] <= end_date]
    return rows


def load_us_index_valuation_position_history(
    symbol: str,
    *,
    end_date: Optional[date] = None,
) -> Dict[date, Dict[int, Optional[float]]]:
    """逐美股交易日的估值点位（按窗口 252/504），每天只用截至当天的估值偏离历史。"""
    rows = _load_us_valuation_rows(symbol, end_date)
    return build_valuation_position_history((row["date"], row["current_gap_pct"]) for row in rows)


def load_us_index_valuation_history(
    symbol: str,
    *,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """贪恐历史曲线用：与 A股 valuation_history 同结构（估值系数）。"""
    return [
        {
            "date": row["date"].isoformat(),
            "valuation_ratio": _valuation_ratio(row["current_gap_pct"]),
            "current_gap_pct": row["current_gap_pct"],
            "is_ffilled": row["is_ffilled"],
        }
        for row in _load_us_valuation_rows(symbol, end_date)
        if start_date is None or row["date"] >= start_date
    ]


def load_us_index_valuation(symbol: str) -> Dict[str, Any]:
    """最新一个美股交易日的估值与估值点位（贪恐摘要卡片用，字段与 A股估值快照对齐）。"""
    rows = _load_us_valuation_rows(symbol)
    if not rows:
        return {"status": "unavailable", "reason": "valuation_history_missing"}
    latest = rows[-1]
    gaps = [row["current_gap_pct"] for row in rows]
    return {
        "status": "available",
        "index_date": latest["date"].isoformat(),
        "analysis_date": latest["analysis_date"].isoformat(),
        "index_level": latest["index_level"],
        "market_price": latest["market_price"],
        "fair_value_lo": latest["fair_value_lo"],
        "fair_value_mid": latest["fair_value_mid"],
        "fair_value_hi": latest["fair_value_hi"],
        "current_gap_pct": latest["current_gap_pct"],
        "is_ffilled": latest["is_ffilled"],
        "coverage_ratio": latest["coverage_ratio"],
        "effective_coverage_ratio": latest["coverage_ratio"],
        "valuation_date_min": latest["valuation_date_min"].isoformat() if latest["valuation_date_min"] else None,
        "valuation_date_max": latest["valuation_date_max"].isoformat() if latest["valuation_date_max"] else None,
        **_build_valuation_position_fields(gaps, latest["current_gap_pct"]),
        "method": "us_etf_analysis_evc_fair_value_vs_nav",
    }

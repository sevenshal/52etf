"""价值投资选股扫描：质量闸门 + 估值偏离度 + 潜在回报率(return%)。

数据来源均为 DuckDB 分析库中的 tushare 财务报表缓存
(a_stock_income / a_stock_balancesheet / a_stock_cashflow / a_stock_fina_indicator)
以及历史估值截面(a_stock_market_daily)。方法论对应三层漏斗：

1. 质量闸门(quality gate)：用现金流验证利润、检查资产负债健康度，
   剔除"便宜是因为要出问题"的价值陷阱。金融类公司(银行/保险/证券)
   用不同的杠杆/现金流判断口径，因为其资产负债表结构与工商业公司完全不同。
2. 估值偏离度：当前 PE_TTM / PB 在自身历史分布中的分位数，分位数越低
   说明相对自己的历史越便宜。
3. 潜在回报率 return%：取三种独立测算方法的中位数(至少两种可用才输出)，
   避免单一模型的乐观假设主导结果：
   - 估值均值回归：假设估值向自身历史中位数修复
   - FCF收益率 + 历史净利润复合增速：企业自身创造价值的部分，不依赖重估
   - 市盈率倒数(E/P)：类似"股权债券"的保底收益率

质量闸门与估值历史只使用年报(end_date 为 12-31)，不使用季报/半年报：
季报是累计但未审计的口径，跨报告类型混用会污染"近5年平均"这类长期质量判断。
本模块只做只读查询，不修改任何数据表。
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd

from .duckdb_analytics import connect_analytics_db, duckdb_table_exists, safe_float

VALUATION_HISTORY_YEARS = 5
ANNUAL_HISTORY_PERIODS = 5
MIN_ANNUAL_PERIODS_FOR_QUALITY = 3

DEFAULT_QUALITY_THRESHOLDS = {
    # 一般工商业
    "min_avg_roe": 10.0,
    "min_ocf_to_np": 0.6,
    "min_fcf_positive_years": 3,
    "max_debt_to_assets": 70.0,
    # 银行/保险/证券：杠杆天然更高，不适用上面的负债率/OCF口径
    "min_avg_roe_financial": 8.0,
}

# tushare comp_type: 1=一般工商业 2=银行 3=保险 4=证券
FINANCIAL_COMP_TYPES = {"2", "3", "4"}


def _annual_rows(connection, table: str, periods: int = ANNUAL_HISTORY_PERIODS) -> pd.DataFrame:
    """只取年报(end_date为12-31)，按 end_date 取最近 periods 期。

    价值投资看的是年度经营质量，季报(3/6/9月末)口径不一、且多为未审计数据，
    一律不参与质量闸门/估值历史计算。同一 end_date 若因更正/追溯调整存在多条记录
    (report_type 不同)，只保留公告时间最新的一条，避免同一财年被重复计入期数。
    """
    if not duckdb_table_exists(connection, table):
        return pd.DataFrame()
    query = f"""
        WITH deduped AS (
            SELECT *,
                   ROW_NUMBER() OVER (PARTITION BY ts_code, end_date ORDER BY ann_date DESC) AS _dedup_rn
            FROM {table}
            WHERE end_date IS NOT NULL AND strftime(end_date, '%m-%d') = '12-31'
        )
        SELECT * EXCLUDE (_dedup_rn)
        FROM deduped
        WHERE _dedup_rn = 1
        QUALIFY ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY end_date DESC) <= {int(periods)}
    """
    return connection.execute(query).fetchdf()


def _latest_market_row(connection) -> pd.DataFrame:
    if not duckdb_table_exists(connection, "a_stock_market_daily"):
        return pd.DataFrame()
    query = """
        SELECT ts_code, trade_date, close, total_mv, circ_mv, pe, pe_ttm, pb, dv_ttm
        FROM a_stock_market_daily
        QUALIFY ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date DESC) = 1
    """
    return connection.execute(query).fetchdf()


def _valuation_history(connection, start_date: date) -> pd.DataFrame:
    if not duckdb_table_exists(connection, "a_stock_market_daily"):
        return pd.DataFrame()
    query = """
        SELECT ts_code, pe_ttm, pb
        FROM a_stock_market_daily
        WHERE trade_date >= ?
    """
    return connection.execute(query, [start_date]).fetchdf()


def _percentile_rank(current: Optional[float], history: pd.Series) -> Optional[float]:
    """当前值在历史序列中的分位数(0=历史最低，100=历史最高)。"""
    if current is None or not math.isfinite(current):
        return None
    values = history.dropna()
    values = values[values > 0]
    if len(values) < 20:
        return None
    return float((values <= current).mean() * 100.0)


def _cagr_pct(latest: Optional[float], base: Optional[float], years: float) -> Optional[float]:
    if latest is None or base is None or base <= 0 or latest <= 0 or years <= 0:
        return None
    try:
        return (math.pow(latest / base, 1.0 / years) - 1.0) * 100.0
    except (ValueError, ZeroDivisionError):
        return None


def _median(values: List[Optional[float]], min_count: int = 2) -> Optional[float]:
    usable = sorted(v for v in values if v is not None and math.isfinite(v))
    if len(usable) < min_count:
        return None
    mid = len(usable) // 2
    if len(usable) % 2:
        return usable[mid]
    return (usable[mid - 1] + usable[mid]) / 2.0


def _quality_assessment(
    *,
    is_financial: bool,
    avg_roe: Optional[float],
    years_available: int,
    ocf_to_np: Optional[float],
    fcf_positive_years: int,
    debt_to_assets: Optional[float],
    thresholds: Dict[str, float],
) -> Dict[str, Any]:
    reasons: List[str] = []
    if years_available < MIN_ANNUAL_PERIODS_FOR_QUALITY:
        reasons.append(f"年报数据不足{MIN_ANNUAL_PERIODS_FOR_QUALITY}期，无法判断质量")
        return {"passes": False, "reasons": reasons}

    min_roe = thresholds["min_avg_roe_financial"] if is_financial else thresholds["min_avg_roe"]
    if avg_roe is None or avg_roe < min_roe:
        reasons.append(f"近{years_available}年平均ROE {avg_roe if avg_roe is not None else 'NA'} 低于阈值 {min_roe}")

    if not is_financial:
        if ocf_to_np is None or ocf_to_np < thresholds["min_ocf_to_np"]:
            reasons.append(f"经营现金流/净利润比 {ocf_to_np if ocf_to_np is not None else 'NA'} 低于阈值 {thresholds['min_ocf_to_np']}，盈利质量存疑")
        if fcf_positive_years < thresholds["min_fcf_positive_years"]:
            reasons.append(f"近{years_available}年自由现金流为正的年份仅 {fcf_positive_years} 年，低于阈值 {thresholds['min_fcf_positive_years']}")
        if debt_to_assets is not None and debt_to_assets > thresholds["max_debt_to_assets"]:
            reasons.append(f"资产负债率 {debt_to_assets} 高于阈值 {thresholds['max_debt_to_assets']}")

    return {"passes": len(reasons) == 0, "reasons": reasons}


def screen_value_investing_candidates(
    *,
    min_total_mv: Optional[float] = None,
    exclude_st: bool = True,
    quality_overrides: Optional[Dict[str, float]] = None,
    top_n: int = 100,
    as_of: Optional[date] = None,
) -> Dict[str, Any]:
    """跑一次全市场价值投资扫描，返回按潜在 return% 排序的候选列表。

    只读查询 DuckDB 分析库；金融类公司(银行/保险/证券)使用单独的质量口径。
    """
    thresholds = dict(DEFAULT_QUALITY_THRESHOLDS)
    if quality_overrides:
        thresholds.update({k: v for k, v in quality_overrides.items() if k in thresholds})

    as_of_value = as_of or date.today()
    history_start = as_of_value - timedelta(days=365 * VALUATION_HISTORY_YEARS)

    connection = connect_analytics_db()
    try:
        basic = connection.execute(
            "SELECT ts_code, name, industry, list_date, list_status FROM a_stock_basic"
        ).fetchdf()
        income_annual = _annual_rows(connection, "a_stock_income")
        balancesheet_annual = _annual_rows(connection, "a_stock_balancesheet", periods=1)
        cashflow_annual = _annual_rows(connection, "a_stock_cashflow")
        fina_annual = _annual_rows(connection, "a_stock_fina_indicator")
        market_latest = _latest_market_row(connection)
        valuation_history = _valuation_history(connection, history_start)
    finally:
        connection.close()

    if market_latest.empty or basic.empty:
        return {
            "as_of": as_of_value.isoformat(),
            "status": "no_data",
            "message": "分析库中暂无市场行情或股票基础信息，无法扫描",
            "candidates": [],
            "universe_size": 0,
            "quality_passed": 0,
        }

    has_fundamentals = not (income_annual.empty and cashflow_annual.empty and fina_annual.empty and balancesheet_annual.empty)
    if not has_fundamentals:
        return {
            "as_of": as_of_value.isoformat(),
            "status": "fundamentals_not_synced",
            "message": (
                "资产负债表/现金流量表/财务指标/利润表财务数据尚未同步到分析库，"
                "需要先跑一次 A 股基础数据同步(含新增的4张财务报表缓存表)才能扫描"
            ),
            "candidates": [],
            "universe_size": int(len(basic)),
            "quality_passed": 0,
        }

    basic_by_symbol = basic.set_index("ts_code").to_dict("index")
    valuation_by_symbol = {
        symbol: group[["pe_ttm", "pb"]] for symbol, group in valuation_history.groupby("ts_code")
    } if not valuation_history.empty else {}

    def _annual_group(frame: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        if frame.empty:
            return {}
        return {symbol: group.sort_values("end_date") for symbol, group in frame.groupby("ts_code")}

    income_by_symbol = _annual_group(income_annual)
    cashflow_by_symbol = _annual_group(cashflow_annual)
    fina_by_symbol = _annual_group(fina_annual)
    balancesheet_by_symbol = (
        balancesheet_annual.set_index("ts_code").to_dict("index") if not balancesheet_annual.empty else {}
    )

    candidates: List[Dict[str, Any]] = []
    universe_size = 0
    quality_passed = 0

    for _, market_row in market_latest.iterrows():
        ts_code = market_row["ts_code"]
        basic_row = basic_by_symbol.get(ts_code)
        if not basic_row:
            continue
        if str(basic_row.get("list_status") or "") != "L":
            continue
        name = str(basic_row.get("name") or "")
        if exclude_st and "ST" in name.upper():
            continue

        total_mv = safe_float(market_row.get("total_mv"))
        if min_total_mv is not None and (total_mv is None or total_mv < min_total_mv):
            continue

        universe_size += 1

        bs_row = balancesheet_by_symbol.get(ts_code, {})
        comp_type = str(bs_row.get("comp_type") or "")
        is_financial = comp_type in FINANCIAL_COMP_TYPES

        fina_history = fina_by_symbol.get(ts_code)
        avg_roe = safe_float(fina_history["roe"].mean()) if fina_history is not None and "roe" in fina_history else None
        years_available = 0 if fina_history is None else int(fina_history["end_date"].nunique())

        cashflow_history = cashflow_by_symbol.get(ts_code)
        ocf_to_np_values: List[float] = []
        fcf_positive_years = 0
        latest_free_cashflow = None
        if cashflow_history is not None:
            for _, row in cashflow_history.iterrows():
                net_profit = safe_float(row.get("net_profit"))
                ocf = safe_float(row.get("n_cashflow_act"))
                if net_profit and net_profit > 0 and ocf is not None:
                    ocf_to_np_values.append(ocf / net_profit)
                free_cashflow = safe_float(row.get("free_cashflow"))
                if free_cashflow is None and ocf is not None:
                    capex = safe_float(row.get("c_pay_acq_const_fiolta")) or 0.0
                    free_cashflow = ocf - capex
                if free_cashflow is not None and free_cashflow > 0:
                    fcf_positive_years += 1
                latest_free_cashflow = free_cashflow if free_cashflow is not None else latest_free_cashflow
            if not cashflow_history.empty:
                last_row = cashflow_history.iloc[-1]
                fcf = safe_float(last_row.get("free_cashflow"))
                if fcf is None:
                    ocf_last = safe_float(last_row.get("n_cashflow_act"))
                    capex_last = safe_float(last_row.get("c_pay_acq_const_fiolta")) or 0.0
                    fcf = None if ocf_last is None else ocf_last - capex_last
                latest_free_cashflow = fcf
        ocf_to_np = safe_float(sum(ocf_to_np_values) / len(ocf_to_np_values)) if ocf_to_np_values else None

        fina_latest_row = fina_history.iloc[-1].to_dict() if fina_history is not None and not fina_history.empty else {}
        debt_to_assets = safe_float(fina_latest_row.get("debt_to_assets"))
        if debt_to_assets is None:
            total_assets = safe_float(bs_row.get("total_assets"))
            total_liab = safe_float(bs_row.get("total_liab"))
            if total_assets and total_assets > 0 and total_liab is not None:
                debt_to_assets = total_liab / total_assets * 100.0

        quality = _quality_assessment(
            is_financial=is_financial,
            avg_roe=avg_roe,
            years_available=years_available,
            ocf_to_np=ocf_to_np,
            fcf_positive_years=fcf_positive_years,
            debt_to_assets=debt_to_assets,
            thresholds=thresholds,
        )
        if not quality["passes"]:
            candidates.append(
                {
                    "ts_code": ts_code,
                    "name": name,
                    "industry": basic_row.get("industry"),
                    "is_financial": is_financial,
                    "quality_passed": False,
                    "quality_reasons": quality["reasons"],
                    "avg_roe_pct": avg_roe,
                    "expected_return_pct": None,
                }
            )
            continue
        quality_passed += 1

        pe_ttm = safe_float(market_row.get("pe_ttm"))
        pb = safe_float(market_row.get("pb"))
        history = valuation_by_symbol.get(ts_code)
        pe_percentile = _percentile_rank(pe_ttm, history["pe_ttm"]) if history is not None else None
        pb_percentile = _percentile_rank(pb, history["pb"]) if history is not None else None
        valuation_percentile = _median([pe_percentile, pb_percentile], min_count=1)

        reversion_pct = None
        if history is not None:
            if pe_ttm and pe_ttm > 0:
                median_pe = safe_float(history.loc[history["pe_ttm"] > 0, "pe_ttm"].median())
                if median_pe:
                    reversion_pct = (median_pe / pe_ttm - 1.0) * 100.0
            if reversion_pct is None and pb and pb > 0:
                median_pb = safe_float(history.loc[history["pb"] > 0, "pb"].median())
                if median_pb:
                    reversion_pct = (median_pb / pb - 1.0) * 100.0

        income_history = income_by_symbol.get(ts_code)
        profit_cagr_pct = None
        if income_history is not None and len(income_history) >= 2 and "n_income_attr_p" in income_history:
            oldest = safe_float(income_history.iloc[0]["n_income_attr_p"])
            latest_profit = safe_float(income_history.iloc[-1]["n_income_attr_p"])
            years_span = income_history.iloc[-1]["end_date"].year - income_history.iloc[0]["end_date"].year
            profit_cagr_pct = _cagr_pct(latest_profit, oldest, years_span)

        fcf_yield_pct = None
        if latest_free_cashflow is not None and total_mv and total_mv > 0:
            fcf_yield_pct = latest_free_cashflow / (total_mv * 10000.0) * 100.0

        growth_return_pct = None
        if fcf_yield_pct is not None or profit_cagr_pct is not None:
            growth_return_pct = sum(v for v in (fcf_yield_pct, profit_cagr_pct) if v is not None)

        earnings_yield_pct = (100.0 / pe_ttm) if pe_ttm and pe_ttm > 0 else None

        expected_return_pct = _median([reversion_pct, growth_return_pct, earnings_yield_pct], min_count=2)

        candidates.append(
            {
                "ts_code": ts_code,
                "name": name,
                "industry": basic_row.get("industry"),
                "is_financial": is_financial,
                "quality_passed": True,
                "quality_reasons": [],
                "avg_roe_pct": avg_roe,
                "ocf_to_net_profit": ocf_to_np,
                "fcf_positive_years": fcf_positive_years,
                "debt_to_assets_pct": debt_to_assets,
                "close": safe_float(market_row.get("close")),
                "pe_ttm": pe_ttm,
                "pb": pb,
                "pe_percentile_5y": pe_percentile,
                "pb_percentile_5y": pb_percentile,
                "valuation_percentile_5y": valuation_percentile,
                "reversion_return_pct": safe_float(reversion_pct, 1),
                "fcf_yield_pct": safe_float(fcf_yield_pct, 2),
                "profit_cagr_pct": safe_float(profit_cagr_pct, 1),
                "growth_return_pct": safe_float(growth_return_pct, 1),
                "earnings_yield_pct": safe_float(earnings_yield_pct, 1),
                "expected_return_pct": safe_float(expected_return_pct, 1),
            }
        )

    ranked = sorted(
        (c for c in candidates if c["quality_passed"] and c["expected_return_pct"] is not None),
        key=lambda c: c["expected_return_pct"],
        reverse=True,
    )
    excluded = [c for c in candidates if not c["quality_passed"]]

    return {
        "as_of": as_of_value.isoformat(),
        "status": "completed",
        "universe_size": universe_size,
        "quality_passed": quality_passed,
        "insufficient_return_data": sum(
            1 for c in candidates if c["quality_passed"] and c["expected_return_pct"] is None
        ),
        "thresholds": thresholds,
        "candidates": ranked[: max(0, int(top_n))],
        "excluded_sample": excluded[: max(0, int(top_n))],
    }

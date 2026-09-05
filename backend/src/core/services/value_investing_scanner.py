"""价值投资选股扫描：ROIC vs WACC 质量闸门 + 两阶段 FCFF 现金流折现(DCF)内在价值。

数据来源均为 DuckDB 分析库中的 tushare 财务报表缓存
(a_stock_income / a_stock_balancesheet / a_stock_cashflow / a_stock_fina_indicator)
以及历史行情/估值截面(a_stock_market_daily / a_stock_index_daily)。

质量闸门与估值历史只使用年报(end_date 为 12-31)，不使用季报/半年报：
季报是累计但未审计的口径，跨报告类型混用会污染"近5年平均"这类长期质量判断。

方法论(比旧版"跟自己历史估值比"更接近专业分析师的真实流程):

1. 质量闸门 —— 核心判据是 ROIC 能否持续跑赢 WACC(资本加权成本)，而不是 ROE：
   ROE 加杠杆就能做高，掩盖不了"生意本身值不值得投钱"这个问题；ROIC 是资本结构
   中性的，ROIC-WACC 价差为负意味着公司每多投一块钱资本都在毁灭价值，增长越快越
   糟糕。金融类公司(银行/保险/证券)的资产负债表结构完全不同("投入资本"概念不
   适用)，退回到 ROE 闸门。同时保留经营现金流/净利润比、FCFF为正年数、资产负债率
   作为盈利质量与安全边际的辅助检验。
2. WACC 现场估算：
   - beta：用该股票近2年日收益率对中证全指(000985.SH)日收益率做回归斜率
   - 股权成本：CAPM = 无风险利率 + beta × 股权风险溢价
   - 债权成本：财务费用中的利息费用 / 有息负债，乘以 (1-实际税率) 得税后成本
   - 按市值(股权)与有息负债(债权)的权重加权
3. 内在价值：
   - 非金融公司：两阶段 FCFF DCF —— 显式预测期(5年)增长率从近端 FCFF/利润复合
     增速线性衰减到永续增长率，终值用永续增长模型，按 WACC 折现回企业价值，
     减净债务得股权价值，`return% = 股权价值/当前市值 − 1`
   - 金融公司：FCFF DCF 不适用(存贷款不是资本开支/营运资金)，改用银行/险资分析师
     常用的"合理市净率"模型：fair P/B = (ROE − g) / (股权成本 − g)，
     `return% = fair P/B / 当前PB − 1`
   - 同时给出 WACC/股权成本 ±150bp 的悲观/乐观区间，而不是单点估计
   - 估值均值回归、市盈率倒数(E/P)、trailing FCFF收益率仍作为交叉验证字段返回，
     但不再用于计算 return%，避免"跟自己历史比"这种不依赖基本面的逻辑主导结果

无风险利率与股权风险溢价目前是可配置的静态假设(本系统未同步国债收益率曲线，
只有信用债曲线)，不是实时值；需要更精确的口径可以通过接口参数覆盖。

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

# --- WACC / DCF 假设，均可通过 API 参数覆盖 ---
MARKET_INDEX_CODE = "000985.SH"  # 中证全指：覆盖面最广的宽基指数，用于估算 beta
BETA_LOOKBACK_DAYS = 730
MIN_BETA_OBSERVATIONS = 200
DEFAULT_BETA = 1.0
DEFAULT_RISK_FREE_RATE = 0.025
DEFAULT_EQUITY_RISK_PREMIUM = 0.06
DEFAULT_COST_OF_DEBT_PRETAX = 0.045
DEFAULT_EFFECTIVE_TAX_RATE = 0.25
DEFAULT_TERMINAL_GROWTH_RATE = 0.03
DCF_EXPLICIT_YEARS = 5
MIN_WACC_TERMINAL_SPREAD = 0.01
NEAR_TERM_GROWTH_BOUNDS = (-0.15, 0.30)
SCENARIO_DISCOUNT_RATE_SPREAD = 0.015
SCENARIO_GROWTH_MULTIPLIER = (0.5, 1.3)  # (悲观, 乐观) 相对近端增速的倍数

DEFAULT_QUALITY_THRESHOLDS = {
    # 非金融：ROIC-WACC 价差(百分点)下限，>=0 表示至少不能是"增长越快越毁灭价值"
    "min_roic_wacc_spread_pct": 0.0,
    "min_ocf_to_np": 0.6,
    "min_fcf_positive_years": 3,
    "max_debt_to_assets": 70.0,
    # 银行/保险/证券：资本结构与工商业完全不同，退回 ROE 闸门
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


def _beta_by_symbol(connection, lookback_start: date, market_index_code: str) -> Dict[str, float]:
    """用近 BETA_LOOKBACK_DAYS 天的日收益率对基准指数做回归斜率估算 beta。

    一次 SQL 对全市场做 REGR_SLOPE 分组聚合，避免逐个股票 Python 循环回归。
    """
    if not duckdb_table_exists(connection, "a_stock_index_daily") or not duckdb_table_exists(connection, "a_stock_market_daily"):
        return {}
    query = """
        WITH index_returns AS (
            SELECT trade_date, pct_chg AS index_pct_chg
            FROM a_stock_index_daily
            WHERE ts_code = ? AND trade_date >= ? AND pct_chg IS NOT NULL
        )
        SELECT
            m.ts_code AS ts_code,
            REGR_SLOPE(m.pct_chg, i.index_pct_chg) AS beta,
            COUNT(*) AS obs
        FROM a_stock_market_daily m
        JOIN index_returns i ON m.trade_date = i.trade_date
        WHERE m.pct_chg IS NOT NULL AND m.trade_date >= ?
        GROUP BY m.ts_code
        HAVING COUNT(*) >= ? AND REGR_SLOPE(m.pct_chg, i.index_pct_chg) IS NOT NULL
    """
    frame = connection.execute(
        query,
        [market_index_code, lookback_start, lookback_start, MIN_BETA_OBSERVATIONS],
    ).fetchdf()
    if frame.empty:
        return {}
    return {row["ts_code"]: float(row["beta"]) for _, row in frame.iterrows()}


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


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _wacc_components(
    *,
    beta: Optional[float],
    market_cap: Optional[float],
    interest_bearing_debt: Optional[float],
    interest_expense: Optional[float],
    income_tax: Optional[float],
    total_profit: Optional[float],
    risk_free_rate: float,
    equity_risk_premium: float,
) -> Optional[Dict[str, float]]:
    """估算 CAPM 股权成本 + 税后债权成本，按市值/有息负债加权得 WACC。

    市值(equity)与有息负债(debt)权重全为0时(缺市值数据)返回 None，
    调用方应把该股票标为"WACC 数据不足"而不是假装算出了一个值。
    """
    equity = market_cap if market_cap and market_cap > 0 else 0.0
    debt = interest_bearing_debt if interest_bearing_debt and interest_bearing_debt > 0 else 0.0
    total_capital = equity + debt
    if total_capital <= 0:
        return None

    effective_beta = beta if beta is not None and math.isfinite(beta) and beta > 0 else DEFAULT_BETA
    cost_of_equity = risk_free_rate + effective_beta * equity_risk_premium

    if debt > 0 and interest_expense and interest_expense > 0:
        cost_of_debt_pretax = _clip(interest_expense / debt, 0.005, 0.15)
    else:
        cost_of_debt_pretax = DEFAULT_COST_OF_DEBT_PRETAX

    if income_tax is not None and total_profit and total_profit > 0:
        effective_tax_rate = _clip(income_tax / total_profit, 0.05, 0.33)
    else:
        effective_tax_rate = DEFAULT_EFFECTIVE_TAX_RATE

    cost_of_debt_after_tax = cost_of_debt_pretax * (1.0 - effective_tax_rate)
    equity_weight = equity / total_capital
    debt_weight = debt / total_capital
    wacc = equity_weight * cost_of_equity + debt_weight * cost_of_debt_after_tax

    return {
        "beta": effective_beta,
        "cost_of_equity": cost_of_equity,
        "cost_of_debt_pretax": cost_of_debt_pretax,
        "effective_tax_rate": effective_tax_rate,
        "cost_of_debt_after_tax": cost_of_debt_after_tax,
        "equity_weight": equity_weight,
        "debt_weight": debt_weight,
        "wacc": wacc,
    }


def _two_stage_fcff_value(
    base_fcff: Optional[float],
    near_term_growth: float,
    terminal_growth: float,
    wacc: Optional[float],
    years: int = DCF_EXPLICIT_YEARS,
) -> Optional[float]:
    """两阶段 FCFF 折现，返回企业价值(EV，元)。

    显式预测期内增长率从 near_term_growth 线性衰减到 terminal_growth，
    终值用永续增长模型。WACC 与永续增长率利差过窄(<MIN_WACC_TERMINAL_SPREAD)时
    终值会爆炸式失真，直接判定不可用而不是返回一个虚假的精确数字。
    """
    if base_fcff is None or base_fcff <= 0 or wacc is None:
        return None
    if wacc - terminal_growth < MIN_WACC_TERMINAL_SPREAD:
        return None

    pv = 0.0
    fcff_t = base_fcff
    for t in range(1, years + 1):
        weight = (t - 1) / (years - 1) if years > 1 else 1.0
        g_t = near_term_growth + (terminal_growth - near_term_growth) * weight
        fcff_t = fcff_t * (1.0 + g_t)
        pv += fcff_t / ((1.0 + wacc) ** t)
    terminal_value = fcff_t * (1.0 + terminal_growth) / (wacc - terminal_growth)
    pv_terminal = terminal_value / ((1.0 + wacc) ** years)
    return pv + pv_terminal


def _justified_pb(avg_roe_frac: Optional[float], cost_of_equity: Optional[float], terminal_growth: float) -> Optional[float]:
    """银行/保险/证券的"合理市净率"模型：fair P/B = (ROE-g)/(r-g)。

    FCFF DCF 对金融类公司不适用(存贷款不是资本开支/营运资金变动)，
    这是分析师覆盖银行/险资时的标准替代框架，直接用 ROE 和股权成本算合理估值。
    """
    if avg_roe_frac is None or cost_of_equity is None:
        return None
    if cost_of_equity - terminal_growth < MIN_WACC_TERMINAL_SPREAD:
        return None
    return (avg_roe_frac - terminal_growth) / (cost_of_equity - terminal_growth)


def _estimate_near_term_growth(fcff_cagr_pct: Optional[float], profit_cagr_pct: Optional[float]) -> float:
    source_pct = fcff_cagr_pct if fcff_cagr_pct is not None else profit_cagr_pct
    growth = (source_pct / 100.0) if source_pct is not None else 0.0
    return _clip(growth, *NEAR_TERM_GROWTH_BOUNDS)


def _quality_assessment(
    *,
    is_financial: bool,
    avg_roe: Optional[float],
    avg_roic: Optional[float],
    wacc_pct: Optional[float],
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

    if is_financial:
        min_roe = thresholds["min_avg_roe_financial"]
        if avg_roe is None or avg_roe < min_roe:
            reasons.append(
                f"近{years_available}年平均ROE {avg_roe if avg_roe is not None else 'NA'} 低于阈值 {min_roe}"
                "(金融类资本结构特殊，不适用ROIC-WACC口径)"
            )
        return {"passes": len(reasons) == 0, "reasons": reasons}

    if avg_roic is None or wacc_pct is None:
        reasons.append("ROIC或WACC数据不足，无法判断是否创造价值")
    else:
        spread = avg_roic - wacc_pct
        if spread < thresholds["min_roic_wacc_spread_pct"]:
            reasons.append(
                f"近{years_available}年平均ROIC({avg_roic:.1f}%)未跑赢WACC({wacc_pct:.1f}%)，"
                f"价差{spread:.1f}个百分点，增长越快可能越在毁灭价值"
            )
    if ocf_to_np is None or ocf_to_np < thresholds["min_ocf_to_np"]:
        reasons.append(
            f"经营现金流/净利润比 {ocf_to_np if ocf_to_np is not None else 'NA'} "
            f"低于阈值 {thresholds['min_ocf_to_np']}，盈利质量存疑"
        )
    if fcf_positive_years < thresholds["min_fcf_positive_years"]:
        reasons.append(
            f"近{years_available}年FCFF为正的年份仅 {fcf_positive_years} 年，"
            f"低于阈值 {thresholds['min_fcf_positive_years']}"
        )
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
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
    equity_risk_premium: float = DEFAULT_EQUITY_RISK_PREMIUM,
    terminal_growth_rate: float = DEFAULT_TERMINAL_GROWTH_RATE,
) -> Dict[str, Any]:
    """跑一次全市场价值投资扫描，返回按 DCF/合理估值测算的潜在 return% 排序的候选列表。

    只读查询 DuckDB 分析库；金融类公司(银行/保险/证券)使用单独的质量与估值口径。
    """
    thresholds = dict(DEFAULT_QUALITY_THRESHOLDS)
    if quality_overrides:
        thresholds.update({k: v for k, v in quality_overrides.items() if k in thresholds})

    as_of_value = as_of or date.today()
    history_start = as_of_value - timedelta(days=365 * VALUATION_HISTORY_YEARS)
    beta_lookback_start = as_of_value - timedelta(days=BETA_LOOKBACK_DAYS)

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
        beta_by_symbol = _beta_by_symbol(connection, beta_lookback_start, MARKET_INDEX_CODE)
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
        market_cap_yuan = total_mv * 10000.0 if total_mv else None

        bs_row = balancesheet_by_symbol.get(ts_code, {})
        comp_type = str(bs_row.get("comp_type") or "")
        is_financial = comp_type in FINANCIAL_COMP_TYPES

        fina_history = fina_by_symbol.get(ts_code)
        avg_roe = safe_float(fina_history["roe"].mean()) if fina_history is not None and "roe" in fina_history else None
        avg_roic = safe_float(fina_history["roic"].mean()) if fina_history is not None and "roic" in fina_history else None
        years_available = 0 if fina_history is None else int(fina_history["end_date"].nunique())
        fina_latest_row = fina_history.iloc[-1].to_dict() if fina_history is not None and not fina_history.empty else {}

        fcf_positive_years = 0
        if fina_history is not None and "fcff" in fina_history:
            fcff_series = fina_history["fcff"].dropna()
            fcf_positive_years = int((fcff_series > 0).sum())

        cashflow_history = cashflow_by_symbol.get(ts_code)
        ocf_to_np_values: List[float] = []
        if cashflow_history is not None:
            for _, row in cashflow_history.iterrows():
                net_profit = safe_float(row.get("net_profit"))
                ocf = safe_float(row.get("n_cashflow_act"))
                if net_profit and net_profit > 0 and ocf is not None:
                    ocf_to_np_values.append(ocf / net_profit)
        ocf_to_np = safe_float(sum(ocf_to_np_values) / len(ocf_to_np_values)) if ocf_to_np_values else None

        debt_to_assets = safe_float(fina_latest_row.get("debt_to_assets"))
        if debt_to_assets is None:
            total_assets = safe_float(bs_row.get("total_assets"))
            total_liab = safe_float(bs_row.get("total_liab"))
            if total_assets and total_assets > 0 and total_liab is not None:
                debt_to_assets = total_liab / total_assets * 100.0

        quality = _quality_assessment(
            is_financial=is_financial,
            avg_roe=avg_roe,
            avg_roic=avg_roic,
            wacc_pct=None,  # 先占位，WACC 算出来后如果不通过闸门也不影响已经失败的判断
            years_available=years_available,
            ocf_to_np=ocf_to_np,
            fcf_positive_years=fcf_positive_years,
            debt_to_assets=debt_to_assets,
            thresholds=thresholds,
        ) if is_financial else None  # 非金融要先算出 WACC 才能判断闸门，见下方

        # --- WACC（金融、非金融都要用到股权成本，非金融还要用债权成本） ---
        income_history = income_by_symbol.get(ts_code)
        income_latest_row = income_history.iloc[-1].to_dict() if income_history is not None and not income_history.empty else {}
        beta = beta_by_symbol.get(ts_code)
        wacc_info = _wacc_components(
            beta=beta,
            market_cap=market_cap_yuan,
            interest_bearing_debt=safe_float(fina_latest_row.get("interestdebt")),
            interest_expense=safe_float(income_latest_row.get("fin_exp_int_exp")),
            income_tax=safe_float(income_latest_row.get("income_tax")),
            total_profit=safe_float(income_latest_row.get("total_profit")),
            risk_free_rate=risk_free_rate,
            equity_risk_premium=equity_risk_premium,
        )
        wacc_pct = safe_float(wacc_info["wacc"] * 100.0) if wacc_info else None

        if not is_financial:
            quality = _quality_assessment(
                is_financial=False,
                avg_roe=avg_roe,
                avg_roic=avg_roic,
                wacc_pct=wacc_pct,
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
                    "avg_roic_pct": avg_roic,
                    "wacc_pct": wacc_pct,
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
        earnings_yield_pct = (100.0 / pe_ttm) if pe_ttm and pe_ttm > 0 else None

        profit_cagr_pct = None
        if income_history is not None and len(income_history) >= 2 and "n_income_attr_p" in income_history:
            oldest_profit = safe_float(income_history.iloc[0]["n_income_attr_p"])
            latest_profit = safe_float(income_history.iloc[-1]["n_income_attr_p"])
            years_span = income_history.iloc[-1]["end_date"].year - income_history.iloc[0]["end_date"].year
            profit_cagr_pct = _cagr_pct(latest_profit, oldest_profit, years_span)

        expected_return_pct = None
        expected_return_pct_bear = None
        expected_return_pct_bull = None
        dcf_unavailable_reason = None
        justified_pb = None
        dcf_enterprise_value = None
        dcf_equity_value = None
        dcf_net_debt = None
        dcf_base_fcff = None
        fcff_cagr_pct = None
        fcf_yield_pct = None

        cost_of_equity = wacc_info["cost_of_equity"] if wacc_info else None

        if is_financial:
            avg_roe_frac = (avg_roe / 100.0) if avg_roe is not None else None
            justified_pb = _justified_pb(avg_roe_frac, cost_of_equity, terminal_growth_rate)
            if justified_pb is not None and pb and pb > 0:
                expected_return_pct = (justified_pb / pb - 1.0) * 100.0
                if cost_of_equity is not None:
                    bear_pb = _justified_pb(avg_roe_frac, cost_of_equity + SCENARIO_DISCOUNT_RATE_SPREAD, terminal_growth_rate)
                    bull_pb = _justified_pb(avg_roe_frac, cost_of_equity - SCENARIO_DISCOUNT_RATE_SPREAD, terminal_growth_rate)
                    expected_return_pct_bear = (bear_pb / pb - 1.0) * 100.0 if bear_pb is not None else None
                    expected_return_pct_bull = (bull_pb / pb - 1.0) * 100.0 if bull_pb is not None else None
            else:
                dcf_unavailable_reason = "股权成本或ROE数据不足，无法估算合理市净率"
        else:
            if fina_history is not None and "fcff" in fina_history:
                fcff_series = fina_history[["end_date", "fcff"]].dropna()
                recent_fcff = fcff_series["fcff"].tolist()[-3:]
                dcf_base_fcff = safe_float(sum(recent_fcff) / len(recent_fcff)) if recent_fcff else None
                if len(fcff_series) >= 2:
                    oldest_fcff = safe_float(fcff_series.iloc[0]["fcff"])
                    latest_fcff = safe_float(fcff_series.iloc[-1]["fcff"])
                    fcff_years_span = fcff_series.iloc[-1]["end_date"].year - fcff_series.iloc[0]["end_date"].year
                    fcff_cagr_pct = _cagr_pct(latest_fcff, oldest_fcff, fcff_years_span)
            near_term_growth = _estimate_near_term_growth(fcff_cagr_pct, profit_cagr_pct)

            latest_fcff_value = safe_float(fina_latest_row.get("fcff"))
            if latest_fcff_value is not None and market_cap_yuan:
                fcf_yield_pct = latest_fcff_value / market_cap_yuan * 100.0

            if wacc_info is None:
                dcf_unavailable_reason = "市值或有息负债数据不足，无法估算WACC"
            elif dcf_base_fcff is None or dcf_base_fcff <= 0:
                dcf_unavailable_reason = "近年FCFF为负或缺失，DCF不适用"
            else:
                wacc = wacc_info["wacc"]
                dcf_ev = _two_stage_fcff_value(dcf_base_fcff, near_term_growth, terminal_growth_rate, wacc)
                if dcf_ev is None:
                    dcf_unavailable_reason = "WACC与永续增长率利差过窄，DCF结果不稳定"
                else:
                    dcf_enterprise_value = dcf_ev
                    dcf_net_debt = safe_float(fina_latest_row.get("netdebt"))
                    if dcf_net_debt is None:
                        interest_debt = safe_float(fina_latest_row.get("interestdebt")) or 0.0
                        money_cap = safe_float(bs_row.get("money_cap")) or 0.0
                        dcf_net_debt = interest_debt - money_cap
                    dcf_equity_value = dcf_ev - dcf_net_debt
                    if market_cap_yuan and market_cap_yuan > 0:
                        expected_return_pct = (dcf_equity_value / market_cap_yuan - 1.0) * 100.0

                        bear_wacc = wacc + SCENARIO_DISCOUNT_RATE_SPREAD
                        bull_wacc = max(terminal_growth_rate + MIN_WACC_TERMINAL_SPREAD * 1.5, wacc - SCENARIO_DISCOUNT_RATE_SPREAD)
                        bear_growth = near_term_growth * SCENARIO_GROWTH_MULTIPLIER[0]
                        bull_growth = near_term_growth * SCENARIO_GROWTH_MULTIPLIER[1]
                        bear_ev = _two_stage_fcff_value(dcf_base_fcff, bear_growth, terminal_growth_rate, bear_wacc)
                        bull_ev = _two_stage_fcff_value(dcf_base_fcff, bull_growth, terminal_growth_rate, bull_wacc)
                        if bear_ev is not None:
                            expected_return_pct_bear = ((bear_ev - dcf_net_debt) / market_cap_yuan - 1.0) * 100.0
                        if bull_ev is not None:
                            expected_return_pct_bull = ((bull_ev - dcf_net_debt) / market_cap_yuan - 1.0) * 100.0

        candidates.append(
            {
                "ts_code": ts_code,
                "name": name,
                "industry": basic_row.get("industry"),
                "is_financial": is_financial,
                "quality_passed": True,
                "quality_reasons": [],
                "avg_roe_pct": avg_roe,
                "avg_roic_pct": avg_roic,
                "ocf_to_net_profit": ocf_to_np,
                "fcf_positive_years": fcf_positive_years,
                "debt_to_assets_pct": debt_to_assets,
                "close": safe_float(market_row.get("close")),
                "pe_ttm": pe_ttm,
                "pb": pb,
                "pe_percentile_5y": pe_percentile,
                "pb_percentile_5y": pb_percentile,
                "valuation_percentile_5y": valuation_percentile,
                "beta": safe_float(wacc_info["beta"], 2) if wacc_info else safe_float(beta, 2),
                "cost_of_equity_pct": safe_float(cost_of_equity * 100.0, 2) if cost_of_equity is not None else None,
                "cost_of_debt_after_tax_pct": (
                    safe_float(wacc_info["cost_of_debt_after_tax"] * 100.0, 2) if wacc_info else None
                ),
                "wacc_pct": safe_float(wacc_pct, 2),
                "roic_wacc_spread_pct": (
                    safe_float(avg_roic - wacc_pct, 2) if avg_roic is not None and wacc_pct is not None else None
                ),
                "dcf_base_fcff_yi": safe_float(dcf_base_fcff / 1e8, 2) if dcf_base_fcff else None,
                "fcff_cagr_pct": safe_float(fcff_cagr_pct, 1),
                "profit_cagr_pct": safe_float(profit_cagr_pct, 1),
                "terminal_growth_pct": terminal_growth_rate * 100.0,
                "dcf_enterprise_value_yi": safe_float(dcf_enterprise_value / 1e8, 2) if dcf_enterprise_value else None,
                "dcf_net_debt_yi": safe_float(dcf_net_debt / 1e8, 2) if dcf_net_debt is not None else None,
                "dcf_equity_value_yi": safe_float(dcf_equity_value / 1e8, 2) if dcf_equity_value else None,
                "justified_pb": safe_float(justified_pb, 2),
                "market_cap_yi": safe_float(market_cap_yuan / 1e8, 2) if market_cap_yuan else None,
                "dcf_unavailable_reason": dcf_unavailable_reason,
                "expected_return_pct": safe_float(expected_return_pct, 1),
                "expected_return_pct_bear": safe_float(expected_return_pct_bear, 1),
                "expected_return_pct_bull": safe_float(expected_return_pct_bull, 1),
                "reversion_return_pct": safe_float(reversion_pct, 1),
                "earnings_yield_pct": safe_float(earnings_yield_pct, 1),
                "fcf_yield_pct": safe_float(fcf_yield_pct, 2),
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
        "assumptions": {
            "risk_free_rate_pct": risk_free_rate * 100.0,
            "equity_risk_premium_pct": equity_risk_premium * 100.0,
            "terminal_growth_rate_pct": terminal_growth_rate * 100.0,
            "market_index_code": MARKET_INDEX_CODE,
            "dcf_explicit_years": DCF_EXPLICIT_YEARS,
        },
        "candidates": ranked[: max(0, int(top_n))],
        "excluded_sample": excluded[: max(0, int(top_n))],
    }

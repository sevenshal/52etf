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
   - 股权成本：CAPM = 无风险利率 + beta × 股权风险溢价，再套 MIN_COST_OF_EQUITY
     下限(无风险利率处在1.7%这种低位时，低beta股票的CAPM结果会低到不合常理，
     直接当贴现率用会把DCF终值吹爆)
   - 债权成本：财务费用中的利息费用 / 有息负债，乘以 (1-实际税率) 得税后成本
   - 按市值(股权)与有息负债(债权)的权重加权
3. 内在价值：
   - 非金融公司：两阶段 FCFF DCF —— 显式预测期(5年)增长率从近端 FCFF/利润复合
     增速线性衰减到永续增长率，终值用永续增长模型，按 WACC 折现回企业价值，
     减净债务、再减少数股东权益得**归母**股权价值，`return% = 归母股权价值/当前市值 − 1`。
     少数股东这一减不能省：FCFF 是"全体股东+债权人"口径，而 total_mv 只是母公司
     上市股本，利润主要来自非全资子公司的公司不减就等于把别人的那份也算进了回报。
     基准 FCFF 走现金流量表口径(经营现金流 − 资本开支 + 税后利息)，tushare 的
     `fina_indicator.fcff` 只留作交叉验证——它的"营运资金增加"项在不少公司上会算出
     巨额的营运资金释放，把 FCFF 顶到净利润的好几倍。
     永续增长率取 min(入参, 无风险利率)：谁也不能永远比长期国债增长得更快。
   - 金融公司：FCFF DCF 不适用(存贷款不是资本开支/营运资金)，改用银行/险资分析师
     常用的"合理市净率"模型：fair P/B = (ROE − g) / (股权成本 − g)，
     `return% = fair P/B / 当前PB − 1`
   - 终值现值占企业价值超过 MAX_TERMINAL_VALUE_SHARE 时判定 DCF 不可用：
     WACC-g 利差闸门只卡住一个入口，"估值几乎全部来自第6年以后的假设"才是失真的
     真正信号
   - 同时给出 WACC/股权成本 ±150bp 的悲观/乐观区间，而不是单点估计；乐观情形的
     贴现率只允许比基准低，利差地板挡住时宁可不给乐观值，也不能给出一个比基准
     还差的"乐观"数字
   - 估值均值回归、市盈率倒数(E/P)、trailing FCFF收益率仍作为交叉验证字段返回，
     但不再用于计算 return%，避免"跟自己历史比"这种不依赖基本面的逻辑主导结果
4. 内在价值同比增长闸门 —— 用"截止最新年报"和"截止去年年报"(去掉最新一期，
   WACC/股权成本假设保持不变，只让基本面输入随年报切片变化)分别按上面同一套
   公式各算一次内在价值，要求同比不能倒退。这是为了防止"当前静态快照看着便宜，
   但基本面已经在恶化"的价值陷阱：一家公司哪怕5年平均ROIC还压得住WACC，如果
   最新一年相比上一年内在价值在缩水，也不该现在就选进来。数据不够算出去年那次
   快照时，按"无法确认价值是否增长"处理，直接不通过，而不是放行。

无风险利率默认现取中债国债收益率曲线(到期)的10年期利率(同一套已有的chinabond
爬取基础设施，只是多同步一条曲线定义)；曲线还没同步到时退回静态假设。股权风险
溢价目前仍是可配置的静态假设(A股没有公开、权威的实时ERP口径)。

本模块只做只读查询，不修改任何数据表。
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

from ...robot.a_stock_base_data_config import (
    CHINABOND_GOVERNMENT_BOND_CURVE_ID,
    RISK_FREE_RATE_TERM_YEARS,
)
from .duckdb_analytics import connect_analytics_db, duckdb_table_exists, safe_float

VALUATION_HISTORY_YEARS = 5
ANNUAL_HISTORY_PERIODS = 5
MIN_ANNUAL_PERIODS_FOR_QUALITY = 3

# --- WACC / DCF 假设，均可通过 API 参数覆盖 ---
MARKET_INDEX_CODE = "000985.SH"  # 中证全指：覆盖面最广的宽基指数，用于估算 beta
BETA_LOOKBACK_DAYS = 730
MIN_BETA_OBSERVATIONS = 200
DEFAULT_BETA = 1.0
BETA_CLIP_BOUNDS = (0.2, 3.0)  # 单股票2年日收益率回归的合理范围，防止噪声估计值失真
BLUME_ADJUSTMENT_WEIGHT = 2.0 / 3.0  # adjusted_beta = w*raw_beta + (1-w)*1.0，业界标准做法
DEFAULT_RISK_FREE_RATE = 0.025
DEFAULT_EQUITY_RISK_PREMIUM = 0.06
DEFAULT_COST_OF_DEBT_PRETAX = 0.045
DEFAULT_EFFECTIVE_TAX_RATE = 0.25
DEFAULT_TERMINAL_GROWTH_RATE = 0.03
DCF_EXPLICIT_YEARS = 5
# 股权成本(CAPM)下限：无风险利率现取中债国债10年期(当前只有1.7%左右)，低beta股票
# 算出来的股权成本会低到6%上下——那不是"这家公司风险低"，而是"A股投资者不可能只要
# 6%的年化回报还承担股票风险"。贴现率一低，终值倍数立刻膨胀，DCF 结果就失真了。
MIN_COST_OF_EQUITY = 0.08
# 终值(永续增长模型)在企业价值里的占比上限。利差闸门只卡住了 WACC-g 这一个入口，
# 但真正的失真信号是"这个估值几乎全部来自第6年以后的假设"。占比越界时宁可判定
# DCF 不可用，也不要输出一个 85% 以上都建立在永续假设上的精确数字。
MAX_TERMINAL_VALUE_SHARE = 0.85
# WACC 与永续增长率的利差下限：终值 = 第N年FCFF×(1+g)/(WACC-g)，利差越窄终值
# 倍数越夸张(利差1pp对应~100倍，3pp对应~34倍，5pp对应~21倍)。1pp 太松，
# 实际跑数据时出现过 WACC-g 只有2.1pp、终值炸到年FCFF 48倍的案例，改成3pp。
MIN_WACC_TERMINAL_SPREAD = 0.03
NEAR_TERM_GROWTH_BOUNDS = (-0.15, 0.30)
# 复合增速的可信上限(百分点)。超过这个值说明序列基期接近0、首尾比值失真，
# 这时该判定"这个增速不可用"，而不是 clip 到 NEAR_TERM_GROWTH_BOUNDS 的上界——
# 后者等于把一个明显的垃圾值直接翻译成"允许范围内最乐观的增长假设"。
MAX_PLAUSIBLE_CAGR_PCT = 100.0
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
    # 用最新年报 vs 去年年报分别按同一套公式算出的内在价值(非金融:股权价值，
    # 金融:合理市净率)必须同比增长；0.0 表示至少不能倒退，防止"现在看着便宜、
    # 但基本面已经在恶化"的价值陷阱只看静态快照选不出来。
    "min_value_growth_pct": 0.0,
}

# tushare comp_type: 1=一般工商业 2=银行 3=保险 4=证券
FINANCIAL_COMP_TYPES = {"2", "3", "4"}

# 4 张财务报表按 tushare 官方字段全集建表(97~170列)，扫描器只需要下面这十几列。
# `_annual_rows` 必须显式点名，否则 SELECT * 会把全市场×5期年报的所有列拉进内存。
INCOME_SCAN_COLUMNS = (
    "n_income_attr_p",   # 归母净利润：算利润复合增速(和市值同为归母口径)
    "n_income",          # 净利润(含少数股东损益)：和上一项相除得归母利润占比
    "minority_gain",     # 少数股东损益：n_income 缺失时反推归母利润占比
    "total_profit",      # 利润总额：算实际税率
    "income_tax",        # 所得税费用：算实际税率
    "fin_exp_int_exp",   # 财务费用中的利息费用：算债权成本、FCFF 的税后利息加回
)
BALANCESHEET_SCAN_COLUMNS = (
    "comp_type",                    # 1一般工商业 2银行 3保险 4证券
    "total_assets",
    "total_liab",
    "money_cap",                    # 货币资金：netdebt 缺失时的净债务兜底
    "minority_int",                 # 少数股东权益：把全口径股权价值折回归母口径
    "total_hldr_eqy_exc_min_int",   # 归母权益
    "total_hldr_eqy_inc_min_int",   # 全部权益(minority_int 缺失时相减兜底)
)
CASHFLOW_SCAN_COLUMNS = (
    "net_profit",              # 合并净利润：算经营现金流/净利润
    "n_cashflow_act",          # 经营活动现金流净额
    "c_pay_acq_const_fiolta",  # 购建固定/无形/其他长期资产支付的现金(资本开支)
)
FINA_INDICATOR_SCAN_COLUMNS = (
    "roe",
    "roic",
    "debt_to_assets",
    "interestdebt",   # 有息负债
    "fcff",           # tushare 口径 FCFF：只做交叉验证，不再直接当 DCF 基准
    "netdebt",
)


def _latest_risk_free_rate(connection, curve_id: str, term_years: float) -> Optional[float]:
    """取中债国债收益率曲线最新一个交易日、最接近 term_years 期限的利率(转成小数)。

    曲线还没同步到(或那一天缺这条曲线的数据)时返回 None，调用方应退回静态假设，
    而不是假装取到了一个精确值。
    """
    if not duckdb_table_exists(connection, "a_stock_chinabond_yield_curve_daily"):
        return None
    query = """
        WITH latest_date AS (
            SELECT MAX(trade_date) AS trade_date
            FROM a_stock_chinabond_yield_curve_daily
            WHERE curve_id = ?
        )
        SELECT c.term, c.yield_rate
        FROM a_stock_chinabond_yield_curve_daily AS c, latest_date
        WHERE c.curve_id = ? AND c.trade_date = latest_date.trade_date AND c.yield_rate IS NOT NULL
        ORDER BY ABS(c.term - ?)
        LIMIT 1
    """
    row = connection.execute(query, [curve_id, curve_id, term_years]).fetchone()
    if not row:
        return None
    yield_rate_pct = safe_float(row[1])
    return (yield_rate_pct / 100.0) if yield_rate_pct is not None else None


def _annual_rows(
    connection,
    table: str,
    columns: Sequence[str],
    periods: int = ANNUAL_HISTORY_PERIODS,
) -> pd.DataFrame:
    """只取年报(end_date为12-31)，按 end_date 取最近 periods 期。

    价值投资看的是年度经营质量，季报(3/6/9月末)口径不一、且多为未审计数据，
    一律不参与质量闸门/估值历史计算。同一 end_date 若因更正/追溯调整存在多条记录
    (report_type 不同)，只保留公告时间最新的一条，避免同一财年被重复计入期数。

    `columns` 必须显式点名：这几张财务报表按 tushare 官方字段全集建表(利润表97列、
    资产负债表161列、财务指标170列)，`SELECT *` 会把全市场每只股票5期年报的所有列
    都拉进内存，而扫描器实际只用到其中十几列。
    """
    if not duckdb_table_exists(connection, table):
        return pd.DataFrame()
    selected = ["ts_code", "end_date", "ann_date", *columns]
    available = {
        row[0]
        for row in connection.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
            [table],
        ).fetchall()
    }
    projection = ", ".join(f'"{name}"' for name in dict.fromkeys(selected) if name in available)
    query = f"""
        WITH deduped AS (
            SELECT {projection},
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
    """两点复合增速(%)。基期或末期非正、跨度非正时返回 None(增速无意义)。

    算出来的绝对值超过 MAX_PLAUSIBLE_CAGR_PCT 时同样返回 None：这种量级的复合增速
    只会出现在"基期恰好接近0"的序列上(见 _series_cagr_pct 的说明)，是序列不可用的
    信号，不是一个可以拿去做预测的增长率。
    """
    if latest is None or base is None or base <= 0 or latest <= 0 or years <= 0:
        return None
    try:
        cagr_pct = (math.pow(latest / base, 1.0 / years) - 1.0) * 100.0
    except (ValueError, ZeroDivisionError):
        return None
    if not math.isfinite(cagr_pct) or abs(cagr_pct) > MAX_PLAUSIBLE_CAGR_PCT:
        return None
    return cagr_pct


def _series_cagr_pct(values: List[Optional[float]], years: List[int]) -> Optional[float]:
    """用首尾各取2期均值算复合增速，而不是直接拿第一期和最后一期两个单点。

    单点首尾对波动大的现金流序列非常危险：华特达因 2021 年 FCFF 只有几百万、2025 年
    12 亿，单点算出来是 339% 的"复合增速"，本质上是基期噪声被开方放大，而不是这家
    公司真的在以那个速度增长。取两期均值做端点、跨度按两个窗口的中点计算，能把这类
    基期噪声压下去；不足4期时退回单点口径(样本本来就短，再平滑没有意义)。
    """
    pairs = [
        (year, value)
        for year, value in zip(years, values)
        if value is not None and math.isfinite(value)
    ]
    if len(pairs) < 2:
        return None
    if len(pairs) < 4:
        return _cagr_pct(pairs[-1][1], pairs[0][1], pairs[-1][0] - pairs[0][0])

    head, tail = pairs[:2], pairs[-2:]
    base = sum(value for _, value in head) / len(head)
    latest = sum(value for _, value in tail) / len(tail)
    years_span = (sum(year for year, _ in tail) - sum(year for year, _ in head)) / 2.0
    return _cagr_pct(latest, base, years_span)


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

    raw_beta = beta if beta is not None and math.isfinite(beta) and beta > 0 else DEFAULT_BETA
    raw_beta = _clip(raw_beta, BETA_CLIP_BOUNDS[0], BETA_CLIP_BOUNDS[1])
    # Blume调整：把单只股票2年日收益率回归出来的beta往1.0收缩，这是业界standard做法
    # (Bloomberg终端的"adjusted beta"就是这个公式)——单股票回归噪声很大，尤其是
    # 交易不活跃、流动性差的股票，原始beta经常出现0.3、0.4这种不合理的低值，
    # 直接拖低股权成本/WACC，进而让DCF终值因为利差过窄而爆炸。
    effective_beta = BLUME_ADJUSTMENT_WEIGHT * raw_beta + (1.0 - BLUME_ADJUSTMENT_WEIGHT) * 1.0
    # CAPM 算出来的股权成本再套一个绝对下限：无风险利率处在1.7%这种历史低位时，
    # 低beta股票的 CAPM 结果会跌到6%上下，那个数字当贴现率用会直接把 DCF 终值吹爆。
    cost_of_equity = max(MIN_COST_OF_EQUITY, risk_free_rate + effective_beta * equity_risk_premium)

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
) -> Optional[Dict[str, float]]:
    """两阶段 FCFF 折现，返回 {企业价值, 显式期现值, 终值现值, 终值占比}(单位:元)。

    显式预测期内增长率从 near_term_growth 线性衰减到 terminal_growth，
    终值用永续增长模型。两道可用性闸门，任一不过直接返回 None 而不是返回一个
    虚假的精确数字：

    1. WACC 与永续增长率利差 < MIN_WACC_TERMINAL_SPREAD —— 终值倍数会爆炸；
    2. 终值现值占企业价值 > MAX_TERMINAL_VALUE_SHARE —— 估值几乎全部来自第6年
       以后的永续假设，利差闸门放行了也不代表这个数字可信。
    """
    if base_fcff is None or base_fcff <= 0 or wacc is None:
        return None
    if wacc - terminal_growth < MIN_WACC_TERMINAL_SPREAD:
        return None

    pv_explicit = 0.0
    fcff_t = base_fcff
    for t in range(1, years + 1):
        weight = (t - 1) / (years - 1) if years > 1 else 1.0
        g_t = near_term_growth + (terminal_growth - near_term_growth) * weight
        fcff_t = fcff_t * (1.0 + g_t)
        pv_explicit += fcff_t / ((1.0 + wacc) ** t)
    terminal_value = fcff_t * (1.0 + terminal_growth) / (wacc - terminal_growth)
    pv_terminal = terminal_value / ((1.0 + wacc) ** years)
    enterprise_value = pv_explicit + pv_terminal
    if enterprise_value <= 0:
        return None
    terminal_share = pv_terminal / enterprise_value
    if terminal_share > MAX_TERMINAL_VALUE_SHARE:
        return None
    return {
        "enterprise_value": enterprise_value,
        "pv_explicit": pv_explicit,
        "pv_terminal": pv_terminal,
        "terminal_share": terminal_share,
    }


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


def _annual_values_by_year(frame: Optional[pd.DataFrame], column: str) -> Dict[int, float]:
    """把一段年报切片压成 {财年: 数值}，缺列/缺值的年份直接不出现。"""
    if frame is None or frame.empty or column not in frame.columns:
        return {}
    values: Dict[int, float] = {}
    for _, row in frame.iterrows():
        end_date = row.get("end_date")
        value = safe_float(row.get(column))
        if end_date is None or value is None:
            continue
        values[int(end_date.year)] = value
    return values


def _fcff_history(
    *,
    fina_slice: Optional[pd.DataFrame],
    cashflow_slice: Optional[pd.DataFrame],
    income_slice: Optional[pd.DataFrame],
    effective_tax_rate: float,
) -> Dict[str, Any]:
    """给出逐年 FCFF 序列，以现金流量表口径为准、tushare 口径只做交叉验证。

    以前直接采信 tushare `fina_indicator.fcff`，它的公式是
    `EBIT×(1-税率) + 折旧摊销 - 营运资金增加 - 资本支出`，其中"营运资金增加"这一项
    在不少公司上会算出巨额的营运资金释放，把 FCFF 顶到净利润的好几倍——华特达因就是
    这样算出"基准FCFF 14.68亿 vs 归母净利4.2亿"，再经过终值放大，最后变成 1410% 的
    潜在回报率。

    现金流量表口径 `经营活动现金流 - 购建固定/无形/其他长期资产支付的现金 + 税后利息`
    每一项都能在报表上对上，所以拿它当主口径；tushare 的值只用来算一个偏离倍数
    (`cross_check_ratio`)放进结果里，方便人工判断分歧有多大。现金流量表口径完全算不
    出来时(缺表/缺列)才退回 tushare 口径，并把 source 标成 tushare 以示未经交叉验证。
    """
    reported = _annual_values_by_year(fina_slice, "fcff")
    operating_cash = _annual_values_by_year(cashflow_slice, "n_cashflow_act")
    capex = _annual_values_by_year(cashflow_slice, "c_pay_acq_const_fiolta")
    interest_expense = _annual_values_by_year(income_slice, "fin_exp_int_exp")

    cash_based: Dict[int, float] = {}
    for year, ocf in operating_cash.items():
        if year not in capex:
            continue
        after_tax_interest = interest_expense.get(year, 0.0) * (1.0 - effective_tax_rate)
        cash_based[year] = ocf - capex[year] + after_tax_interest

    # 交叉验证的窗口和 DCF 基准取值的窗口保持一致(都是最近3期)，这样
    # cross_check_ratio 就能直接读成"tushare 的基准 FCFF 是报表口径的几倍"。
    overlap = sorted(set(reported) & set(cash_based))[-3:]
    cross_check_ratio = None
    if overlap:
        cash_mean = sum(cash_based[year] for year in overlap) / len(overlap)
        reported_mean = sum(reported[year] for year in overlap) / len(overlap)
        if cash_mean > 0:
            cross_check_ratio = reported_mean / cash_mean

    if cash_based:
        source = "cashflow_statement"
        series = cash_based
    else:
        source = "tushare_fina_indicator"
        series = reported

    years = sorted(series)
    return {
        "years": years,
        "values": [series[year] for year in years],
        "source": source,
        "cross_check_ratio": cross_check_ratio,
    }


def _parent_profit_share(
    income_slice: Optional[pd.DataFrame],
    balancesheet_slice: Optional[pd.DataFrame],
    years: int = 3,
) -> Optional[float]:
    """归母利润占合并净利润的比例，用来把全口径现金流折算成归母那一份。

    用最近 `years` 期的**合计**而不是单年比值：单年的合并净利润可能接近0甚至为负，
    比值会失真。三档数据源依次兜底，都拿不到时返回 None(调用方退回账面口径)：

    1. `n_income_attr_p / n_income`(归母净利 / 含少数股东损益的净利)——最直接；
    2. `n_income_attr_p / (n_income_attr_p + minority_gain)`——`n_income` 缺失时用
       少数股东损益反推；
    3. `total_hldr_eqy_exc_min_int / total_hldr_eqy_inc_min_int`(归母权益/全部权益)
       ——利润表两个字段都缺时退回资产负债表的存量口径。
    """
    def _window_sum(frame: Optional[pd.DataFrame], column: str) -> Optional[float]:
        values = list(_annual_values_by_year(frame, column).items())
        if not values:
            return None
        recent = [value for _, value in sorted(values)[-years:]]
        return sum(recent) if recent else None

    parent_profit = _window_sum(income_slice, "n_income_attr_p")
    total_profit = _window_sum(income_slice, "n_income")
    if total_profit is None:
        minority_gain = _window_sum(income_slice, "minority_gain")
        if parent_profit is not None and minority_gain is not None:
            total_profit = parent_profit + minority_gain

    if parent_profit is not None and total_profit is not None and total_profit > 0:
        return _clip(parent_profit / total_profit, 0.0, 1.0)

    parent_equity = _window_sum(balancesheet_slice, "total_hldr_eqy_exc_min_int")
    all_equity = _window_sum(balancesheet_slice, "total_hldr_eqy_inc_min_int")
    if parent_equity is not None and all_equity is not None and all_equity > 0:
        return _clip(parent_equity / all_equity, 0.0, 1.0)
    return None


def _parent_equity_value(
    enterprise_value: float,
    net_debt: float,
    book_minority: float,
    parent_profit_share: Optional[float],
) -> Dict[str, Any]:
    """把企业价值折成**归母**股权价值，返回扣除额与所用口径。

    只减账面少数股东权益是不够的：那是拿一个账面数去减一个 DCF 数。华特达因的控股
    子公司 ROIC 24%，账面少数股东权益 24 亿，但少数股东真正拥有的是这家子公司未来
    现金流的近一半，按 DCF 折出来是 70 多亿。账面口径会系统性低估少数股东的索取权，
    进而系统性高估归母价值——上一版把回报率从 1403% 压到 149%，剩下的这一层就是
    这个原因。

    所以按归母利润占比把全体股东的股权价值切一刀，再和账面少数股东权益取**较大**的
    那个作为扣除额：两种口径各有失效场景(比例法在子公司亏损时失真、账面法在子公司
    高回报时失真)，取大是保守的那一侧。拿不到利润占比时退回纯账面口径。
    """
    gross_equity = enterprise_value - net_debt
    if parent_profit_share is None or gross_equity <= 0:
        return {
            "minority_claim": book_minority,
            "minority_basis": "book",
            "equity_value": gross_equity - book_minority,
        }
    proportionate = gross_equity * (1.0 - parent_profit_share)
    if proportionate > book_minority:
        return {
            "minority_claim": proportionate,
            "minority_basis": "proportionate",
            "equity_value": gross_equity - proportionate,
        }
    return {
        "minority_claim": book_minority,
        "minority_basis": "book",
        "equity_value": gross_equity - book_minority,
    }


def _estimate_near_term_growth(fcff_cagr_pct: Optional[float], profit_cagr_pct: Optional[float]) -> float:
    """近端增速：优先用 FCFF 复合增速，其次净利润复合增速，都不可用时按 0 处理。

    `_cagr_pct` 已经把"基期接近0导致的天文数字增速"归成 None，所以这里拿到的一定
    是一个还算可信的增速，clip 只负责收掉尾部极端值——而不再承担"把垃圾值压进
    允许范围"的职责(那样等于把不可用的输入翻译成最乐观的假设)。
    """
    source_pct = fcff_cagr_pct if fcff_cagr_pct is not None else profit_cagr_pct
    growth = (source_pct / 100.0) if source_pct is not None else 0.0
    return _clip(growth, *NEAR_TERM_GROWTH_BOUNDS)


def _intrinsic_value_snapshot(
    *,
    is_financial: bool,
    fina_slice: Optional[pd.DataFrame],
    income_slice: Optional[pd.DataFrame],
    cashflow_slice: Optional[pd.DataFrame],
    balancesheet_slice: Optional[pd.DataFrame],
    cost_of_equity: Optional[float],
    wacc: Optional[float],
    effective_tax_rate: float,
    terminal_growth_rate: float,
) -> Dict[str, Any]:
    """给定一段年报切片(截止到某一年)算出当期的基准情形内在价值。

    抽成独立函数是为了让"用截止到今年的年报算一次、用截止到去年的年报(去掉最新
    一期)再算一次"完全走同一套公式——只有这样两次结果的差才代表"公司本身创造的
    价值是否在增长"，而不是两套口径打架出来的噪声。非金融返回归母股权价值(元)，
    金融返回合理市净率(fair P/B)。
    """
    result: Dict[str, Any] = {
        "equity_value": None,
        "enterprise_value": None,
        "net_debt": None,
        "minority_interest": None,
        "minority_basis": None,
        "book_minority": None,
        "parent_profit_share": None,
        "terminal_value_share": None,
        "justified_pb": None,
        "base_fcff": None,
        "fcff_source": None,
        "fcff_cross_check_ratio": None,
        "near_term_growth": None,
        "fcff_cagr_pct": None,
        "profit_cagr_pct": None,
        "unavailable_reason": None,
    }

    if is_financial:
        avg_roe = (
            safe_float(fina_slice["roe"].mean())
            if fina_slice is not None and "roe" in fina_slice and not fina_slice.empty
            else None
        )
        avg_roe_frac = (avg_roe / 100.0) if avg_roe is not None else None
        result["justified_pb"] = _justified_pb(avg_roe_frac, cost_of_equity, terminal_growth_rate)
        if result["justified_pb"] is None:
            result["unavailable_reason"] = "股权成本或ROE数据不足，无法估算合理市净率"
        return result

    fcff_history = _fcff_history(
        fina_slice=fina_slice,
        cashflow_slice=cashflow_slice,
        income_slice=income_slice,
        effective_tax_rate=effective_tax_rate,
    )
    result["fcff_source"] = fcff_history["source"]
    result["fcff_cross_check_ratio"] = safe_float(fcff_history["cross_check_ratio"], 2)
    recent_fcff = fcff_history["values"][-3:]
    if recent_fcff:
        result["base_fcff"] = safe_float(sum(recent_fcff) / len(recent_fcff))
    result["fcff_cagr_pct"] = _series_cagr_pct(fcff_history["values"], fcff_history["years"])

    if income_slice is not None and not income_slice.empty and "n_income_attr_p" in income_slice:
        profit_by_year = _annual_values_by_year(income_slice, "n_income_attr_p")
        profit_years = sorted(profit_by_year)
        result["profit_cagr_pct"] = _series_cagr_pct(
            [profit_by_year[year] for year in profit_years], profit_years
        )

    result["near_term_growth"] = _estimate_near_term_growth(
        result["fcff_cagr_pct"], result["profit_cagr_pct"]
    )

    if wacc is None:
        result["unavailable_reason"] = "市值或有息负债数据不足，无法估算WACC"
        return result
    if result["base_fcff"] is None or result["base_fcff"] <= 0:
        result["unavailable_reason"] = "近年FCFF为负或缺失，DCF不适用"
        return result

    valuation = _two_stage_fcff_value(
        result["base_fcff"], result["near_term_growth"], terminal_growth_rate, wacc
    )
    if valuation is None:
        result["unavailable_reason"] = "WACC与永续增长率利差过窄、或估值几乎全部来自终值，DCF结果不稳定"
        return result

    latest_fina = fina_slice.iloc[-1] if fina_slice is not None and not fina_slice.empty else None
    latest_bs = (
        balancesheet_slice.iloc[-1]
        if balancesheet_slice is not None and not balancesheet_slice.empty
        else None
    )

    net_debt = safe_float(latest_fina.get("netdebt")) if latest_fina is not None else None
    if net_debt is None:
        interest_debt = safe_float(latest_fina.get("interestdebt")) if latest_fina is not None else None
        money_cap = safe_float(latest_bs.get("money_cap")) if latest_bs is not None else None
        net_debt = (interest_debt or 0.0) - (money_cap or 0.0)

    # FCFF 是"全体股东+债权人"口径的现金流，算出来的股权价值同样含少数股东那一份，
    # 但拿来比的市值(total_mv)只是母公司上市股本。子公司持股比例低的公司(华特达因
    # 的利润几乎全部来自持股约五成的控股子公司)不折成归母口径，回报率会凭空翻倍。
    book_minority = safe_float(latest_bs.get("minority_int")) if latest_bs is not None else None
    if book_minority is None and latest_bs is not None:
        total_equity = safe_float(latest_bs.get("total_hldr_eqy_inc_min_int"))
        parent_equity = safe_float(latest_bs.get("total_hldr_eqy_exc_min_int"))
        if total_equity is not None and parent_equity is not None:
            book_minority = total_equity - parent_equity
    if book_minority is None or book_minority < 0:
        book_minority = 0.0

    parent_profit_share = _parent_profit_share(income_slice, balancesheet_slice)
    attribution = _parent_equity_value(
        valuation["enterprise_value"], net_debt, book_minority, parent_profit_share
    )

    result["enterprise_value"] = valuation["enterprise_value"]
    result["terminal_value_share"] = valuation["terminal_share"]
    result["net_debt"] = net_debt
    result["parent_profit_share"] = parent_profit_share
    result["book_minority"] = book_minority
    result["minority_interest"] = attribution["minority_claim"]
    result["minority_basis"] = attribution["minority_basis"]
    result["equity_value"] = attribution["equity_value"]
    return result


def _value_growth_pct(is_financial: bool, current: Dict[str, Any], prior: Dict[str, Any]) -> Optional[float]:
    """比较"今年"与"去年"两次内在价值快照，算出计算出来的价值同比增长了多少。"""
    key = "justified_pb" if is_financial else "equity_value"
    current_value = current.get(key)
    prior_value = prior.get(key)
    if current_value is None or prior_value is None or prior_value <= 0:
        return None
    return (current_value / prior_value - 1.0) * 100.0


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
    value_growth_pct: Optional[float],
    thresholds: Dict[str, float],
) -> Dict[str, Any]:
    reasons: List[str] = []
    if years_available < MIN_ANNUAL_PERIODS_FOR_QUALITY:
        reasons.append(f"年报数据不足{MIN_ANNUAL_PERIODS_FOR_QUALITY}期，无法判断质量")
        return {"passes": False, "reasons": reasons}

    min_value_growth = thresholds["min_value_growth_pct"]
    if value_growth_pct is None:
        reasons.append("年报数据不足以对比去年同期计算出的内在价值，无法判断价值是否在增长")
    elif value_growth_pct < min_value_growth:
        reasons.append(
            f"按最新年报算出的内在价值比去年同期下降了{-value_growth_pct:.1f}%"
            if value_growth_pct < 0
            else f"按最新年报算出的内在价值同比只增长{value_growth_pct:.1f}%，低于阈值{min_value_growth}%"
        )

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
    risk_free_rate: Optional[float] = None,
    equity_risk_premium: float = DEFAULT_EQUITY_RISK_PREMIUM,
    terminal_growth_rate: float = DEFAULT_TERMINAL_GROWTH_RATE,
) -> Dict[str, Any]:
    """跑一次全市场价值投资扫描，返回按 DCF/合理估值测算的潜在 return% 排序的候选列表。

    只读查询 DuckDB 分析库；金融类公司(银行/保险/证券)使用单独的质量与估值口径。
    risk_free_rate 留空(None)时现取中债国债收益率曲线10年期利率，曲线还没同步到
    时才退回静态假设；显式传值则始终使用调用方指定的值。
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
        income_annual = _annual_rows(connection, "a_stock_income", INCOME_SCAN_COLUMNS)
        # 资产负债表以前只取最新1期。现在"去年那次估值快照"也要用去年的少数股东权益
        # 和货币资金，否则两次快照的净债务/少数股东口径会打架，同比差值就成了噪声。
        balancesheet_annual = _annual_rows(connection, "a_stock_balancesheet", BALANCESHEET_SCAN_COLUMNS)
        cashflow_annual = _annual_rows(connection, "a_stock_cashflow", CASHFLOW_SCAN_COLUMNS)
        fina_annual = _annual_rows(connection, "a_stock_fina_indicator", FINA_INDICATOR_SCAN_COLUMNS)
        market_latest = _latest_market_row(connection)
        valuation_history = _valuation_history(connection, history_start)
        beta_by_symbol = _beta_by_symbol(connection, beta_lookback_start, MARKET_INDEX_CODE)
        risk_free_rate_source = "explicit_override"
        if risk_free_rate is None:
            risk_free_rate = _latest_risk_free_rate(connection, CHINABOND_GOVERNMENT_BOND_CURVE_ID, RISK_FREE_RATE_TERM_YEARS)
            risk_free_rate_source = "chinabond_10y" if risk_free_rate is not None else "default_fallback"
            if risk_free_rate is None:
                risk_free_rate = DEFAULT_RISK_FREE_RATE
    finally:
        connection.close()

    # 永续增长率不能高于无风险利率：一家公司如果能永远以高于长期国债的名义速度增长，
    # 终局就是它大于整个经济体。这是 DCF 的标准约束，也是这次把"1.7%的无风险利率配
    # 3%的永续增长"这种自相矛盾的假设堵掉的地方——利差被人为压窄，终值就会失真。
    effective_terminal_growth = min(terminal_growth_rate, risk_free_rate)

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
    balancesheet_by_symbol = _annual_group(balancesheet_annual)

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

        balancesheet_history = balancesheet_by_symbol.get(ts_code)
        bs_row = (
            balancesheet_history.iloc[-1].to_dict()
            if balancesheet_history is not None and not balancesheet_history.empty
            else {}
        )
        comp_type = str(bs_row.get("comp_type") or "")
        is_financial = comp_type in FINANCIAL_COMP_TYPES

        fina_history = fina_by_symbol.get(ts_code)
        avg_roe = safe_float(fina_history["roe"].mean()) if fina_history is not None and "roe" in fina_history else None
        avg_roic = safe_float(fina_history["roic"].mean()) if fina_history is not None and "roic" in fina_history else None
        years_available = 0 if fina_history is None else int(fina_history["end_date"].nunique())
        fina_latest_row = fina_history.iloc[-1].to_dict() if fina_history is not None and not fina_history.empty else {}

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

        # --- WACC（金融、非金融都要用到股权成本，非金融还要用债权成本）——放在质量
        # 闸门判断之前，因为闸门本身现在也要用到"内在价值同比是否增长"这个判据，
        # 而算内在价值需要先有WACC/股权成本。
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
        cost_of_equity = wacc_info["cost_of_equity"] if wacc_info else None
        effective_tax_rate = (
            wacc_info["effective_tax_rate"] if wacc_info else DEFAULT_EFFECTIVE_TAX_RATE
        )

        # --- 内在价值：分别用"截止最新年报"和"截止去年年报(去掉最新一期)"两个
        # 切片走同一套公式各算一次，差值就是基本面本身是在变好还是变差，不依赖
        # 估值倍数是否重估。
        def _snapshot(drop_latest: bool) -> Dict[str, Any]:
            def _slice(frame: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
                if frame is None or frame.empty:
                    return frame
                return frame.iloc[:-1] if drop_latest and len(frame) > 1 else frame

            return _intrinsic_value_snapshot(
                is_financial=is_financial,
                fina_slice=_slice(fina_history),
                income_slice=_slice(income_history),
                cashflow_slice=_slice(cashflow_history),
                balancesheet_slice=_slice(balancesheet_history),
                cost_of_equity=cost_of_equity,
                wacc=wacc_info["wacc"] if wacc_info else None,
                effective_tax_rate=effective_tax_rate,
                terminal_growth_rate=effective_terminal_growth,
            )

        current_snapshot = _snapshot(drop_latest=False)
        prior_snapshot = _snapshot(drop_latest=True)
        value_growth_pct = _value_growth_pct(is_financial, current_snapshot, prior_snapshot)

        # 质量闸门的"FCFF为正年数"和交叉验证的"FCFF收益率"都必须走 DCF 采用的同一条
        # 序列（现在默认是现金流量表口径），否则闸门看的是 tushare 那条数、估值用的是
        # 另一条，两者结论可以完全相反。
        canonical_fcff = _fcff_history(
            fina_slice=fina_history,
            cashflow_slice=cashflow_history,
            income_slice=income_history,
            effective_tax_rate=effective_tax_rate,
        )["values"]
        fcf_positive_years = int(sum(1 for value in canonical_fcff if value > 0))

        quality = _quality_assessment(
            is_financial=is_financial,
            avg_roe=avg_roe,
            avg_roic=avg_roic,
            wacc_pct=wacc_pct,
            years_available=years_available,
            ocf_to_np=ocf_to_np,
            fcf_positive_years=fcf_positive_years,
            debt_to_assets=debt_to_assets,
            value_growth_pct=value_growth_pct,
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
                    "value_growth_pct": safe_float(value_growth_pct, 1),
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
        dcf_unavailable_reason = current_snapshot["unavailable_reason"]
        justified_pb = current_snapshot["justified_pb"]
        dcf_enterprise_value = current_snapshot["enterprise_value"]
        dcf_equity_value = current_snapshot["equity_value"]
        dcf_net_debt = current_snapshot["net_debt"]
        dcf_book_minority = current_snapshot["book_minority"] or 0.0
        dcf_base_fcff = current_snapshot["base_fcff"]
        fcff_cagr_pct = current_snapshot["fcff_cagr_pct"]
        fcf_yield_pct = None

        if is_financial:
            avg_roe_frac = (avg_roe / 100.0) if avg_roe is not None else None
            if justified_pb is not None and pb and pb > 0:
                expected_return_pct = (justified_pb / pb - 1.0) * 100.0
                if cost_of_equity is not None:
                    bear_pb = _justified_pb(
                        avg_roe_frac, cost_of_equity + SCENARIO_DISCOUNT_RATE_SPREAD, effective_terminal_growth
                    )
                    bull_pb = _justified_pb(
                        avg_roe_frac, cost_of_equity - SCENARIO_DISCOUNT_RATE_SPREAD, effective_terminal_growth
                    )
                    expected_return_pct_bear = (bear_pb / pb - 1.0) * 100.0 if bear_pb is not None else None
                    expected_return_pct_bull = (bull_pb / pb - 1.0) * 100.0 if bull_pb is not None else None
        else:
            near_term_growth = current_snapshot["near_term_growth"]
            if canonical_fcff and market_cap_yuan:
                fcf_yield_pct = canonical_fcff[-1] / market_cap_yuan * 100.0

            if wacc_info is not None and dcf_equity_value is not None and market_cap_yuan and market_cap_yuan > 0:
                wacc = wacc_info["wacc"]
                expected_return_pct = (dcf_equity_value / market_cap_yuan - 1.0) * 100.0

                # 乐观情形的贴现率只能比基准低。之前这里写的是
                # `max(g + MIN_WACC_TERMINAL_SPREAD*1.5, wacc - spread)`，基准 WACC
                # 低于那个地板时(低beta股票很常见)，"乐观"用的贴现率反而比基准还高，
                # 于是出现过乐观回报率 1128% < 基准 1410% 这种基准值落在自己区间之外
                # 的结果。现在改成：地板挡住时直接不给乐观值，而不是给一个更差的数。
                bear_wacc = wacc + SCENARIO_DISCOUNT_RATE_SPREAD
                bull_wacc = wacc - SCENARIO_DISCOUNT_RATE_SPREAD
                # 情景增速也要重新 clip：乘数施加在已经 clip 过的近端增速上，
                # 30% × 1.3 = 39% 会突破 NEAR_TERM_GROWTH_BOUNDS 的上界。
                bear_growth = _clip(near_term_growth * SCENARIO_GROWTH_MULTIPLIER[0], *NEAR_TERM_GROWTH_BOUNDS)
                bull_growth = _clip(near_term_growth * SCENARIO_GROWTH_MULTIPLIER[1], *NEAR_TERM_GROWTH_BOUNDS)
                bear_valuation = _two_stage_fcff_value(
                    dcf_base_fcff, bear_growth, effective_terminal_growth, bear_wacc
                )
                bull_valuation = (
                    _two_stage_fcff_value(dcf_base_fcff, bull_growth, effective_terminal_growth, bull_wacc)
                    if bull_wacc - effective_terminal_growth >= MIN_WACC_TERMINAL_SPREAD
                    else None
                )
                # 少数股东索取权按比例法时会随企业价值一起变，所以悲观/乐观情形要各自
                # 重新折算一次归母价值，不能沿用基准情形算出来的那个固定扣除额。
                def _scenario_return_pct(scenario_valuation) -> Optional[float]:
                    if scenario_valuation is None:
                        return None
                    scenario_equity = _parent_equity_value(
                        scenario_valuation["enterprise_value"],
                        dcf_net_debt,
                        dcf_book_minority,
                        current_snapshot["parent_profit_share"],
                    )["equity_value"]
                    return (scenario_equity / market_cap_yuan - 1.0) * 100.0

                expected_return_pct_bear = _scenario_return_pct(bear_valuation)
                expected_return_pct_bull = _scenario_return_pct(bull_valuation)

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
                "value_growth_pct": safe_float(value_growth_pct, 1),
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
                "dcf_base_fcff_source": current_snapshot["fcff_source"],
                "fcff_cross_check_ratio": current_snapshot["fcff_cross_check_ratio"],
                "fcff_cagr_pct": safe_float(fcff_cagr_pct, 1),
                "profit_cagr_pct": safe_float(profit_cagr_pct, 1),
                "near_term_growth_pct": (
                    safe_float(current_snapshot["near_term_growth"] * 100.0, 1)
                    if current_snapshot["near_term_growth"] is not None
                    else None
                ),
                "terminal_growth_pct": effective_terminal_growth * 100.0,
                "dcf_enterprise_value_yi": safe_float(dcf_enterprise_value / 1e8, 2) if dcf_enterprise_value else None,
                "dcf_terminal_value_share_pct": (
                    safe_float(current_snapshot["terminal_value_share"] * 100.0, 1)
                    if current_snapshot["terminal_value_share"] is not None
                    else None
                ),
                "dcf_net_debt_yi": safe_float(dcf_net_debt / 1e8, 2) if dcf_net_debt is not None else None,
                "dcf_minority_interest_yi": (
                    safe_float(current_snapshot["minority_interest"] / 1e8, 2)
                    if current_snapshot["minority_interest"] is not None
                    else None
                ),
                "dcf_minority_book_yi": (
                    safe_float(current_snapshot["book_minority"] / 1e8, 2)
                    if current_snapshot["book_minority"] is not None
                    else None
                ),
                "dcf_minority_basis": current_snapshot["minority_basis"],
                "parent_profit_share_pct": (
                    safe_float(current_snapshot["parent_profit_share"] * 100.0, 1)
                    if current_snapshot["parent_profit_share"] is not None
                    else None
                ),
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
            "risk_free_rate_source": risk_free_rate_source,
            "equity_risk_premium_pct": equity_risk_premium * 100.0,
            "terminal_growth_rate_pct": effective_terminal_growth * 100.0,
            "terminal_growth_rate_requested_pct": terminal_growth_rate * 100.0,
            "min_cost_of_equity_pct": MIN_COST_OF_EQUITY * 100.0,
            "max_terminal_value_share_pct": MAX_TERMINAL_VALUE_SHARE * 100.0,
            "market_index_code": MARKET_INDEX_CODE,
            "dcf_explicit_years": DCF_EXPLICIT_YEARS,
        },
        "candidates": ranked[: max(0, int(top_n))],
        "excluded_sample": excluded[: max(0, int(top_n))],
    }

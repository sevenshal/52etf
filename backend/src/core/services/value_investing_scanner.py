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
# Gordon 永续增长模型在 g 趋近 WACC 时会发散，必须留一道数值下限。这是**数值稳定性
# 要求**，不是用来把估值压低的旋钮：真正约束增长假设合理性的是下面的再投资口径
# (g 越高，为支撑它而必须投回去的资本越多，自由现金流越少)，而不是这个阈值。
MIN_WACC_TERMINAL_SPREAD = 0.015
# 永续增长率的经济上限：一家公司不可能永远比整个经济体长得快。用长期名义GDP增速
# 做上限，而不是用无风险利率——中国当前的10年期国债利率(1.7%)是政策压低的结果，
# 拿它当全市场的长期名义增长率会系统性低估。
MAX_TERMINAL_GROWTH_RATE = 0.05
NEAR_TERM_GROWTH_BOUNDS = (-0.15, 0.30)
# 终值期的 ROIC：竞争会侵蚀超额回报，永续期继续用公司当前的高 ROIC 等于假设护城河
# 永不失效。按"当前 ROIC 向 WACC 收敛一半"处理，并且不低于 WACC(否则永续期每投一
# 块钱都在毁灭价值，那种公司也不该按永续增长估)。
TERMINAL_ROIC_CONVERGENCE = 0.5
# 再投资率上限：g/ROIC 超过 1 意味着再投资超过 NOPAT、自由现金流永远为负，属于
# 增长假设和回报率假设互相矛盾，此时把增长率压到 ROIC 允许的范围内。
MAX_REINVESTMENT_RATE = 0.9
# 估值分位/均值回归的最小观测数。a_stock_market_daily 的 pe_ttm/pb 是后加的列、
# 日线同步只写新的一天从未回填，实测全表最早只到 2026-08-25——9 个交易日。没有这道
# 保护时 reversion_return_pct 会拿 9 天算出所谓"5年中位PE"照常输出，是个会误导人的
# 数字，比缺失更糟。
VALUATION_HISTORY_MIN_OBSERVATIONS = 20
# 复合增速的可信上限(百分点)。超过这个值说明序列基期接近0、首尾比值失真，
# 这时该判定"这个增速不可用"，而不是 clip 到 NEAR_TERM_GROWTH_BOUNDS 的上界——
# 后者等于把一个明显的垃圾值直接翻译成"允许范围内最乐观的增长假设"。
MAX_PLAUSIBLE_CAGR_PCT = 100.0
# 金融股剩余收益模型里 ROE 向股权成本衰减的年数(超额回报的竞争存续期)。
FINANCIAL_EXCESS_RETURN_FADE_YEARS = 10
# ROIC 交叉验证：tushare 的 roic 与自算 EBIT×(1-税率)/全部投入资本，实测相关系数
# 只有 0.710(中位 5.30% vs 5.16%)。质量闸门和再投资口径都建立在 ROIC 上，两者分歧
# 过大时把倍数输出到结果里供人工判断。
ROIC_CROSS_CHECK_MAX_RATIO = 2.0
ROIC_BOUNDS = (0.01, 0.60)  # 投入资本接近0时比值会失真，夹住
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
    "revenue",           # 营业收入：增长率的首选来源(比FCFF/利润稳定得多)
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
    # 有息负债的构成项：fina_indicator.interestdebt 缺失时自己加出来(见 _interest_bearing_debt)
    "st_borr",                      # 短期借款
    "non_cur_liab_due_1y",          # 一年内到期的非流动负债
    "lt_borr",                      # 长期借款
    "bond_payable",                 # 应付债券
    "st_bonds_payable",             # 应付短期债券
    "lease_liab",                   # 租赁负债(IFRS16 口径按债务处理)
)

# 有息负债的资产负债表构成项。不含"向中央银行借款"(cb_borr，银行专用，金融股走
# 另一套估值口径)和"质押借款"(pledge_borr，是短期借款的其中项，加进来会重复计算)。
INTEREST_BEARING_DEBT_COMPONENTS = (
    "st_borr",
    "non_cur_liab_due_1y",
    "lt_borr",
    "bond_payable",
    "st_bonds_payable",
    "lease_liab",
)
CASHFLOW_SCAN_COLUMNS = (
    "net_profit",              # 合并净利润：算经营现金流/净利润
    "n_cashflow_act",          # 经营活动现金流净额
    "c_pay_acq_const_fiolta",  # 购建固定/无形/其他长期资产支付的现金(资本开支)
)
FINA_INDICATOR_SCAN_COLUMNS = (
    "roe",
    "roe_waa",        # 加权平均ROE：金融股合理市净率用它比期末ROE合适
    "roic",
    "ebit",           # 息税前利润：NOPAT = EBIT×(1-实际税率)
    "invest_capital", # 全部投入资本：自算ROIC做交叉验证
    "daa",            # 折旧摊销：判断资本开支是否覆盖资产损耗
    "tax_to_ebt",
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
    if len(values) < VALUATION_HISTORY_MIN_OBSERVATIONS:
        return None
    return float((values <= current).mean() * 100.0)


def _history_median(history: Optional[pd.Series]) -> Optional[float]:
    """历史序列的中位数，样本不足 VALUATION_HISTORY_MIN_OBSERVATIONS 时返回 None。

    这道保护以前只加在分位数上，没加在均值回归上，后果是：`a_stock_market_daily` 的
    pe_ttm/pb 是后加的列、日线同步只写新的一天从未回填，实测全表最早只到 2026-08-25
    (9个交易日)——分位数因为样本不足全市场恒为 null，而均值回归照样拿这 9 天算出一个
    "5年中位PE"输出到页面上。缺失只是没信息，输出一个假的"5年"才是误导。
    """
    if history is None:
        return None
    values = history.dropna()
    values = values[values > 0]
    if len(values) < VALUATION_HISTORY_MIN_OBSERVATIONS:
        return None
    return safe_float(values.median())


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


def _effective_tax_rate(
    fina_slice: Optional[pd.DataFrame],
    income_slice: Optional[pd.DataFrame],
) -> Dict[str, Any]:
    """实际税率(小数)，附带它是从哪个口径来的。

    实际税率同时进入两个地方：WACC 的税后债权成本，和现金流量表口径 FCFF 的税后
    利息加回。以前只看最新一期年报的 `income_tax / total_profit`，`total_profit<=0`
    (亏损年)就退回 `DEFAULT_EFFECTIVE_TAX_RATE` 猜 25%——全市场有 28.3% 的股票最新
    一期正好踩在这个条件上，等于四分之一以上的样本用的是拍脑袋的税率。

    改成三档兜底：

    1. **最新年报单年**：口径最贴近当前，优先用；
    2. **五年窗口合计**(`sum(income_tax)/sum(total_profit)`)：单个亏损年不至于让整
       家公司退回默认值，实测能救回 9.5% 的股票；
    3. **`fina_indicator.tax_to_ebt`**：注意这一项**几乎没有增量信息**——实测它和
       `income_tax/total_profit` 在 99.09% 的行上数值完全相同(差<0.001)，只在 227 行
       上是"只有它有值"。留着是因为不要白不要，但别指望它解决覆盖率问题。

    三档都拿不到才用 DEFAULT_EFFECTIVE_TAX_RATE。结果 clip 到 [5%, 33%]：A股法定
    税率 25%，高新技术企业 15%，加计扣除后还能更低，但负税率或超过 33% 一定是
    利润总额接近 0 导致的比值失真(实测有 tax_to_ebt 高达 422% 的样本)。
    """
    def _window_sum(frame: Optional[pd.DataFrame], column: str) -> Optional[float]:
        values = _annual_values_by_year(frame, column)
        return sum(values.values()) if values else None

    latest_income = (
        income_slice.iloc[-1] if income_slice is not None and not income_slice.empty else None
    )
    if latest_income is not None:
        tax = safe_float(latest_income.get("income_tax"))
        profit = safe_float(latest_income.get("total_profit"))
        if tax is not None and profit is not None and profit > 0:
            return {"rate": _clip(tax / profit, 0.05, 0.33), "source": "latest_annual"}

    tax_sum = _window_sum(income_slice, "income_tax")
    profit_sum = _window_sum(income_slice, "total_profit")
    if tax_sum is not None and profit_sum is not None and profit_sum > 0:
        return {"rate": _clip(tax_sum / profit_sum, 0.05, 0.33), "source": "five_year_window"}

    reported = _annual_values_by_year(fina_slice, "tax_to_ebt")
    usable = sorted(value / 100.0 for value in reported.values() if 0.0 < value < 100.0)
    if usable:
        return {"rate": _clip(usable[len(usable) // 2], 0.05, 0.33), "source": "tushare_tax_to_ebt"}

    return {"rate": DEFAULT_EFFECTIVE_TAX_RATE, "source": "default_fallback"}


def _interest_bearing_debt(
    fina_slice: Optional[pd.DataFrame],
    balancesheet_slice: Optional[pd.DataFrame],
) -> Dict[str, Any]:
    """有息负债(元)，附带口径和与资产负债表加总的交叉验证倍数。

    有息负债决定 WACC 里债权的权重。以前只读 `fina_indicator.interestdebt`，缺失就
    当 0——而 debt=0 意味着 **WACC 直接退化成股权成本**，是个悄无声息就让贴现率失真
    的口径错误。

    实测 `interestdebt` 的覆盖率是 98.3%，缺失的 105 只股票里有 104 只能从资产负债表
    把构成项加出来，所以这里加一层兜底。两者同时有值时算一个 `cross_check_ratio`
    (报表加总 / tushare 聚合值)放进结果：实测 97.7% 的公司落在 0.9~1.1，只有 0.5%
    分歧超过 2 倍——聚合值总体可信，但那 0.5% 值得人工看一眼。
    """
    latest_fina = fina_slice.iloc[-1] if fina_slice is not None and not fina_slice.empty else None
    latest_bs = (
        balancesheet_slice.iloc[-1]
        if balancesheet_slice is not None and not balancesheet_slice.empty
        else None
    )

    reported = safe_float(latest_fina.get("interestdebt")) if latest_fina is not None else None

    components: List[float] = []
    if latest_bs is not None:
        for column in INTEREST_BEARING_DEBT_COMPONENTS:
            value = safe_float(latest_bs.get(column))
            if value is not None:
                components.append(value)
    # 构成项全为空说明这一期的资产负债表没同步到，而不是"真的没有有息负债"，
    # 所以要区分 0.0 和 None。
    balance_sheet_sum = sum(components) if components else None

    cross_check_ratio = None
    if reported is not None and reported > 0 and balance_sheet_sum is not None:
        cross_check_ratio = balance_sheet_sum / reported

    if reported is not None:
        return {
            "value": reported,
            "source": "tushare_interestdebt",
            "cross_check_ratio": cross_check_ratio,
        }
    if balance_sheet_sum is not None:
        return {"value": balance_sheet_sum, "source": "balancesheet_components", "cross_check_ratio": None}
    return {"value": None, "source": None, "cross_check_ratio": None}


def _wacc_components(
    *,
    beta: Optional[float],
    market_cap: Optional[float],
    interest_bearing_debt: Optional[float],
    interest_expense: Optional[float],
    effective_tax_rate: float,
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
    # 纯 CAPM，不加输出端的地板。曾经加过 8% 的股权成本下限来压住 DCF 终值，实测的
    # 后果是全市场超过一半的股票股权成本被压成同一个 8.00%(中位数和P25都等于地板)，
    # beta 白算了、截面信息全丢——那是在压数字而不是算准。终值失真的真正原因是增长
    # 不需要再投资(见 _two_stage_reinvestment_value)，在那里修才是对的。
    # 如果结果整体偏乐观，该调的是 equity_risk_premium 这个真实的经济假设。
    cost_of_equity = risk_free_rate + effective_beta * equity_risk_premium

    if debt > 0 and interest_expense and interest_expense > 0:
        cost_of_debt_pretax = _clip(interest_expense / debt, 0.005, 0.15)
    else:
        cost_of_debt_pretax = DEFAULT_COST_OF_DEBT_PRETAX

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


def _terminal_roic(roic: Optional[float], wacc: float) -> Optional[float]:
    """终值期的投入资本回报率：当前 ROIC 向 WACC 收敛一半。

    永续期继续套用公司当前的高 ROIC，等于假设护城河永不失效、竞争永远进不来。反过来
    直接用 WACC(完全竞争)又抹掉了确实存在的护城河。取中间：收敛一半，并且不低于
    WACC——ROIC<WACC 时每多投一块钱都在毁灭价值，那种公司本来也不该按永续增长估。
    """
    if roic is None or not math.isfinite(roic):
        return None
    converged = roic + (wacc - roic) * TERMINAL_ROIC_CONVERGENCE
    return max(converged, wacc)


def _two_stage_reinvestment_value(
    base_nopat: Optional[float],
    roic: Optional[float],
    near_term_growth: float,
    terminal_growth: float,
    wacc: Optional[float],
    years: int = DCF_EXPLICIT_YEARS,
) -> Optional[Dict[str, float]]:
    """再投资口径的两阶段 FCFF 折现，返回企业价值(元)及构成明细。

    **和旧版的本质区别**：旧版拿历史 FCFF 直接按 g 增长，等于假设增长是免费的。
    实测全市场有 49.9% 的公司资本开支低于折旧摊销——把它们的历史自由现金流按永续
    增长外推，等于假设资产永远不用更新还能一直长大。这是估值被系统性高估的主因，
    也是之前不得不靠股权成本地板、终值占比闸门去硬压结果的原因。

    这一版走 Damodaran 的基本关系 `g = 再投资率 × ROIC`：想长得快就得把更多 NOPAT
    投回去，自由现金流相应减少。

        NOPAT_t     = NOPAT_{t-1} × (1 + g_t)
        再投资_t    = NOPAT_t × (g_{t+1} / ROIC)     # 本年投入支撑下一年的增长
        FCFF_t      = NOPAT_t − 再投资_t
        终值        = NOPAT_N × (1+g∞) × (1 − g∞/ROIC∞) / (WACC − g∞)

    这样一来 ROIC 低的公司想增长会付出极高的代价(博汇纸业 ROIC 5.4%，长 3% 就要把
    56% 的 NOPAT 投回去)，而 ROIC 高的公司增长几乎不花钱——这正是"ROIC 与 WACC 的
    价差决定增长是否创造价值"这句话在估值公式里的体现，不再需要额外的闸门去表达。
    """
    if base_nopat is None or base_nopat <= 0 or wacc is None or roic is None:
        return None
    if roic <= 0:
        return None
    if wacc - terminal_growth < MIN_WACC_TERMINAL_SPREAD:
        return None

    terminal_roic = _terminal_roic(roic, wacc)
    if terminal_roic is None or terminal_roic <= 0:
        return None

    # 增长率和 ROIC 必须自洽：再投资率 g/ROIC 不能超过 1(那意味着自由现金流永远为负)。
    near_term_growth = min(near_term_growth, roic * MAX_REINVESTMENT_RATE)
    terminal_growth = min(terminal_growth, terminal_roic * MAX_REINVESTMENT_RATE)
    if wacc - terminal_growth < MIN_WACC_TERMINAL_SPREAD:
        return None

    def _growth_at(step: int) -> float:
        """显式预测期内增长率从近端线性衰减到永续增长率。"""
        weight = (step - 1) / (years - 1) if years > 1 else 1.0
        return near_term_growth + (terminal_growth - near_term_growth) * weight

    pv_explicit = 0.0
    nopat_t = base_nopat
    total_reinvestment = 0.0
    for t in range(1, years + 1):
        nopat_t = nopat_t * (1.0 + _growth_at(t))
        # 本年的再投资要支撑的是下一年的增长；最后一年之后进入永续期。
        next_growth = _growth_at(t + 1) if t < years else terminal_growth
        reinvestment_rate = _clip(next_growth / (roic if t < years else terminal_roic), 0.0, 1.0)
        fcff_t = nopat_t * (1.0 - reinvestment_rate)
        total_reinvestment += nopat_t * reinvestment_rate
        pv_explicit += fcff_t / ((1.0 + wacc) ** t)

    terminal_reinvestment_rate = _clip(terminal_growth / terminal_roic, 0.0, 1.0)
    terminal_fcff = nopat_t * (1.0 + terminal_growth) * (1.0 - terminal_reinvestment_rate)
    terminal_value = terminal_fcff / (wacc - terminal_growth)
    pv_terminal = terminal_value / ((1.0 + wacc) ** years)

    enterprise_value = pv_explicit + pv_terminal
    if enterprise_value <= 0:
        return None
    return {
        "enterprise_value": enterprise_value,
        "pv_explicit": pv_explicit,
        "pv_terminal": pv_terminal,
        # 终值占比不再是可用性闸门(5年显式期的DCF终值占70~80%本来就是常态，拿它当
        # 失真信号是找错了靶子)，只作为诊断字段输出，让人能一眼看出估值有多依赖永续假设。
        "terminal_share": pv_terminal / enterprise_value,
        "terminal_roic": terminal_roic,
        "terminal_reinvestment_rate": terminal_reinvestment_rate,
        "explicit_reinvestment": total_reinvestment,
        "applied_near_term_growth": near_term_growth,
        "applied_terminal_growth": terminal_growth,
    }


def _excess_return_discount_factor(cost_of_equity: float, growth: float, years: int) -> float:
    """Σ_{t=1..N} (1+g)^(t-1) / (1+r)^t —— 剩余收益的折现因子之和。

    账面价值按 g 增长，超额收益按 r 折现。抽出来是因为它同时被"算合理市净率"和
    "从当前股价反推市场隐含ROE"两个方向用到，必须是同一个数。
    """
    factor = 0.0
    for step in range(1, years + 1):
        factor += ((1.0 + growth) ** (step - 1)) / ((1.0 + cost_of_equity) ** step)
    return factor


def _residual_income_pb(
    roe_frac: Optional[float],
    cost_of_equity: Optional[float],
    growth: float,
    fade_years: int = FINANCIAL_EXCESS_RETURN_FADE_YEARS,
) -> Optional[float]:
    """金融类公司的合理市净率：剩余收益(超额收益)模型。

        P/B = 1 + Σ_{t=1..N} (ROE_t − r) × (1+g)^(t-1) / (1+r)^t

    **为什么不再用 `fair P/B = (ROE−g)/(r−g)`**：那个 Gordon 形式本身没错，错在它对
    银行完全没有分辨力。实测银行 beta 低，CAPM 股权成本只有 5~7%，配 3% 的永续增长，
    分母 `r−g` 只剩 1.4~2.4 个百分点——r 或 g 差 50bp，合理市净率就动 25%。用一个
    对输入如此敏感的公式给全市场银行排序，输出的精度是假的(实测算出 3~4 倍市净率，
    而中国银行股长期在 0.6 倍交易，一度让三只银行占据扫描榜前列)。

    剩余收益模型锚定在账面价值上：`P/B = 1 + 超额收益的现值`。ROE 等于股权成本时
    P/B 正好是 1，不存在会爆炸的分母，对输入的敏感度是线性而非双曲的。

    ROE 在 `fade_years` 年内从当前水平**线性衰减到股权成本**：竞争会侵蚀超额回报，
    假设一家银行永远赚 r 以上是没有依据的。衰减完成后超额收益为零，所以没有终值项
    ——这也是这个模型比 Gordon 形式稳健的地方，价值不依赖于对第 N 年之后的假设。
    """
    if roe_frac is None or cost_of_equity is None or fade_years <= 0:
        return None
    if cost_of_equity <= 0:
        return None

    book_value = 1.0
    present_value = 0.0
    for step in range(1, fade_years + 1):
        # 第 step 年的 ROE：从当前水平线性衰减到股权成本
        weight = step / fade_years
        roe_t = roe_frac + (cost_of_equity - roe_frac) * weight
        present_value += (roe_t - cost_of_equity) * book_value / ((1.0 + cost_of_equity) ** step)
        book_value *= (1.0 + growth)
    return 1.0 + present_value


def _implied_sustainable_roe(
    current_pb: Optional[float],
    cost_of_equity: Optional[float],
    growth: float,
    fade_years: int = FINANCIAL_EXCESS_RETURN_FADE_YEARS,
) -> Optional[float]:
    """从当前市净率反推市场隐含的可持续 ROE(小数)。

    把剩余收益公式倒过来解(假设 ROE 在整个窗口内保持不变)：
        P/B = 1 + (ROE − r) × Σ(1+g)^(t-1)/(1+r)^t   ⟹   ROE = r + (P/B − 1) / Σ

    这是这一版对金融股最有用的输出。模型和市场分歧巨大时，与其硬给一个"合理市净率
    是账面的 3 倍"的结论，不如告诉使用者**市场到底在假设什么**：中国银行股在 0.6 倍
    市净率交易，隐含的可持续 ROE 往往只有个位数甚至更低，远低于财报上报出来的十几个
    点。差距就是市场对资产质量/隐性风险的定价——那部分是纯 CAPM+ROE 框架看不见的，
    需要人工判断，而不是让模型假装它不存在。
    """
    if current_pb is None or current_pb <= 0 or cost_of_equity is None or cost_of_equity <= 0:
        return None
    factor = _excess_return_discount_factor(cost_of_equity, growth, fade_years)
    if factor <= 0:
        return None
    return cost_of_equity + (current_pb - 1.0) / factor


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
        # 中国会计准则(CAS 31)把"偿付利息支付的现金"归在**筹资活动**
        # (c_pay_dist_dpcp_int_exp 位于筹资分节)，间接法是把财务费用加回净利润的，
        # 所以经营活动现金流本身已经是**付息前**口径：
        #     CFO_CAS ≈ 净利 + D&A + 财务费用 − ΔWC
        #             = EBIT(1−t) + I·t + D&A − ΔWC
        # 于是 `CFO − capex` 已经等于 FCFF 再加上一笔利息税盾，要**减掉** I·t 才对。
        # 美国准则下利息付现在经营活动里，才是教科书上那个 `CFO + I(1−t) − capex`；
        # 直接套用会多算大约一整笔利息费用。实测 44.2% 的公司利息超过
        # (OCF−capex) 的 10%，23.6% 超过 30%，不是可以忽略的量级。
        interest_tax_shield = interest_expense.get(year, 0.0) * effective_tax_rate
        cash_based[year] = ocf - capex[year] - interest_tax_shield

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


def _roic_estimate(
    fina_slice: Optional[pd.DataFrame],
    effective_tax_rate: float,
) -> Dict[str, Any]:
    """投入资本回报率(小数)，附口径与交叉验证倍数。

    ROIC 是这个模型里最吃重的一个数：质量闸门用它判断"增长是否创造价值"，再投资
    口径的 DCF 用它决定"长一个百分点要投多少钱"。以前完全采信 tushare 的 `roic`
    一个聚合值——和当初完全采信 `fcff` 是同一类风险。

    实测自算的 `EBIT×(1−实际税率) / 全部投入资本` 与 tushare `roic` 的相关系数只有
    0.710(中位数 5.16% vs 5.30%)，不算高。这里以 tushare 的值为主(它的口径更完整，
    考虑了商誉、在建工程等调整)，缺失时用自算值兜底，两者都有时输出
    `cross_check_ratio` 供人工判断。取近5年均值而不是最新一年，避免单年波动。
    """
    reported = _annual_values_by_year(fina_slice, "roic")
    reported_mean = (
        sum(reported.values()) / len(reported) / 100.0 if reported else None
    )

    ebit = _annual_values_by_year(fina_slice, "ebit")
    invest_capital = _annual_values_by_year(fina_slice, "invest_capital")
    computed_values = [
        ebit[year] * (1.0 - effective_tax_rate) / invest_capital[year]
        for year in sorted(set(ebit) & set(invest_capital))
        if invest_capital[year] > 0
    ]
    computed_mean = sum(computed_values) / len(computed_values) if computed_values else None

    cross_check_ratio = None
    if reported_mean is not None and computed_mean is not None and reported_mean > 0:
        cross_check_ratio = computed_mean / reported_mean

    if reported_mean is not None and reported_mean > 0:
        return {
            "value": _clip(reported_mean, *ROIC_BOUNDS),
            "source": "tushare_roic",
            "cross_check_ratio": cross_check_ratio,
        }
    if computed_mean is not None and computed_mean > 0:
        return {
            "value": _clip(computed_mean, *ROIC_BOUNDS),
            "source": "computed_nopat_over_invested_capital",
            "cross_check_ratio": None,
        }
    return {"value": None, "source": None, "cross_check_ratio": cross_check_ratio}


def _base_nopat(
    fina_slice: Optional[pd.DataFrame],
    effective_tax_rate: float,
    years: int = 3,
) -> Optional[float]:
    """基准 NOPAT = 近3年 `EBIT × (1 − 实际税率)` 的均值。

    用 NOPAT 而不是历史自由现金流当 DCF 的起点，是这一版的核心改动：自由现金流里
    混着营运资金的一次性释放和"资本开支低于折旧"的资产损耗，把它当可持续现金流
    外推是错的。NOPAT 是经营层面的税后利润，再投资在 DCF 里显式扣除。
    """
    ebit = _annual_values_by_year(fina_slice, "ebit")
    if not ebit:
        return None
    recent = [ebit[year] for year in sorted(ebit)[-years:]]
    if not recent:
        return None
    return sum(recent) / len(recent) * (1.0 - effective_tax_rate)


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

    所以只要能算出归母利润占比，就按比例把全体股东的股权价值切一刀——少数股东分到的
    是子公司未来现金流的那一份，不是它的账面净资产。曾经写成"和账面取较大者"，那是
    保守化而不是准确化：取大等于在两个估计之间系统性偏向低估归母价值，方向性偏差和
    高估一样是错的。算不出利润占比时才退回纯账面口径。
    """
    gross_equity = enterprise_value - net_debt
    if parent_profit_share is None or gross_equity <= 0:
        return {
            "minority_claim": book_minority,
            "minority_basis": "book",
            "equity_value": gross_equity - book_minority,
        }
    proportionate = gross_equity * (1.0 - parent_profit_share)
    return {
        "minority_claim": proportionate,
        "minority_basis": "proportionate",
        "equity_value": gross_equity - proportionate,
    }


def _fundamental_growth(income_slice: Optional[pd.DataFrame], fina_slice: Optional[pd.DataFrame]) -> Dict[str, Any]:
    """近端增速：营业收入/EBIT/净利润三条复合增速取中位数。

    以前优先用 FCFF 复合增速——那是所有序列里噪声最大的一条(华特达因那次 339.6% 的
    离谱增速就出自它)。收入是最稳定的增长代理，EBIT 次之，净利润再次之，三者取中位
    能压掉单条序列的异常。全部不可用时按 0 处理，而不是猜一个正增长。

    注意这里用的都是**合并口径**(revenue / ebit / n_income)，和 NOPAT、FCFF 保持一致；
    归母那一刀在最后按利润占比切(见 _parent_equity_value)。
    """
    def _cagr(frame, column) -> Optional[float]:
        values = _annual_values_by_year(frame, column)
        years = sorted(values)
        return _series_cagr_pct([values[year] for year in years], years) if years else None

    revenue_cagr = _cagr(income_slice, "revenue")
    ebit_cagr = _cagr(fina_slice, "ebit")
    profit_cagr = _cagr(income_slice, "n_income")
    if profit_cagr is None:
        profit_cagr = _cagr(income_slice, "n_income_attr_p")

    median_pct = _median([revenue_cagr, ebit_cagr, profit_cagr], min_count=1)
    growth = (median_pct / 100.0) if median_pct is not None else 0.0
    return {
        "growth": _clip(growth, *NEAR_TERM_GROWTH_BOUNDS),
        "revenue_cagr_pct": revenue_cagr,
        "ebit_cagr_pct": ebit_cagr,
        "profit_cagr_pct": profit_cagr,
    }


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
        "terminal_roic": None,
        "terminal_reinvestment_rate": None,
        "justified_pb": None,
        "financial_roe": None,
        "base_nopat": None,
        "roic": None,
        "roic_source": None,
        "roic_cross_check_ratio": None,
        "near_term_growth": None,
        "applied_terminal_growth": None,
        "revenue_cagr_pct": None,
        "ebit_cagr_pct": None,
        "profit_cagr_pct": None,
        "cash_fcff": None,
        "cash_fcff_source": None,
        "fcff_cross_check_ratio": None,
        "unavailable_reason": None,
    }

    if is_financial:
        # 金融股用加权平均ROE(roe_waa)而不是期末ROE：增发/回购的年份两者能差出几个
        # 百分点，而合理市净率对 ROE 极其敏感。roe_waa 缺失时才退回 roe。
        avg_roe = None
        for column in ("roe_waa", "roe"):
            values = _annual_values_by_year(fina_slice, column)
            if values:
                avg_roe = sum(values.values()) / len(values)
                break
        avg_roe_frac = (avg_roe / 100.0) if avg_roe is not None else None
        result["financial_roe"] = avg_roe_frac
        result["justified_pb"] = _residual_income_pb(
            avg_roe_frac, cost_of_equity, terminal_growth_rate
        )
        if result["justified_pb"] is None:
            result["unavailable_reason"] = "股权成本或ROE数据不足，无法估算合理市净率"
        return result

    # --- 增长与回报率：DCF 的两个核心输入 ---
    growth_info = _fundamental_growth(income_slice, fina_slice)
    result["near_term_growth"] = growth_info["growth"]
    result["revenue_cagr_pct"] = growth_info["revenue_cagr_pct"]
    result["ebit_cagr_pct"] = growth_info["ebit_cagr_pct"]
    result["profit_cagr_pct"] = growth_info["profit_cagr_pct"]

    roic_info = _roic_estimate(fina_slice, effective_tax_rate)
    result["roic"] = roic_info["value"]
    result["roic_source"] = roic_info["source"]
    result["roic_cross_check_ratio"] = safe_float(roic_info["cross_check_ratio"], 2)

    result["base_nopat"] = _base_nopat(fina_slice, effective_tax_rate)

    # 现金流量表口径的自由现金流不再进 DCF，但仍作为交叉验证保留：NOPAT 口径算出来的
    # 价值如果和实打实收到的现金差太远，是个值得人工看一眼的信号。
    fcff_history = _fcff_history(
        fina_slice=fina_slice,
        cashflow_slice=cashflow_slice,
        income_slice=income_slice,
        effective_tax_rate=effective_tax_rate,
    )
    result["cash_fcff_source"] = fcff_history["source"]
    result["fcff_cross_check_ratio"] = safe_float(fcff_history["cross_check_ratio"], 2)
    recent_cash_fcff = fcff_history["values"][-3:]
    if recent_cash_fcff:
        result["cash_fcff"] = safe_float(sum(recent_cash_fcff) / len(recent_cash_fcff))

    if wacc is None:
        result["unavailable_reason"] = "市值或有息负债数据不足，无法估算WACC"
        return result
    if result["base_nopat"] is None or result["base_nopat"] <= 0:
        result["unavailable_reason"] = "EBIT缺失或为负，NOPAT口径DCF不适用"
        return result
    if result["roic"] is None:
        result["unavailable_reason"] = "ROIC数据不足，无法确定增长需要多少再投资"
        return result

    valuation = _two_stage_reinvestment_value(
        result["base_nopat"], result["roic"], result["near_term_growth"], terminal_growth_rate, wacc
    )
    if valuation is None:
        result["unavailable_reason"] = "WACC与永续增长率利差过窄，Gordon终值不稳定"
        return result

    latest_fina = fina_slice.iloc[-1] if fina_slice is not None and not fina_slice.empty else None
    latest_bs = (
        balancesheet_slice.iloc[-1]
        if balancesheet_slice is not None and not balancesheet_slice.empty
        else None
    )

    net_debt = safe_float(latest_fina.get("netdebt")) if latest_fina is not None else None
    if net_debt is None:
        debt_info = _interest_bearing_debt(fina_slice, balancesheet_slice)
        money_cap = safe_float(latest_bs.get("money_cap")) if latest_bs is not None else None
        net_debt = (debt_info["value"] or 0.0) - (money_cap or 0.0)

    # FCFF/NOPAT 都是"全体股东+债权人"口径，而拿来比的市值(total_mv)只是母公司上市
    # 股本。子公司持股比例低的公司不折成归母口径，回报率会凭空翻倍。
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
    result["terminal_roic"] = valuation["terminal_roic"]
    result["terminal_reinvestment_rate"] = valuation["terminal_reinvestment_rate"]
    result["applied_terminal_growth"] = valuation["applied_terminal_growth"]
    result["near_term_growth"] = valuation["applied_near_term_growth"]
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
    notes: List[str] = []
    if years_available < MIN_ANNUAL_PERIODS_FOR_QUALITY:
        reasons.append(f"年报数据不足{MIN_ANNUAL_PERIODS_FOR_QUALITY}期，无法判断质量")
        return {"passes": False, "reasons": reasons, "notes": notes}

    min_value_growth = thresholds["min_value_growth_pct"]
    if value_growth_pct is None:
        # 算不出"去年那次快照"不等于基本面在恶化，只是这一项没有信息。以前把它并进
        # 不通过的理由里，实测成了第一大拦截原因(全市场 1225 只)——那是把数据可得性
        # 当成了负面信号，制造假阴性。现在只记一条提示，不影响是否通过。
        notes.append("年报数据不足以对比去年同期计算出的内在价值，这一项未参与判断")
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
        return {"passes": len(reasons) == 0, "reasons": reasons, "notes": notes}

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

    return {"passes": len(reasons) == 0, "reasons": reasons, "notes": notes}


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

    # 永续增长率的上限取 MAX_TERMINAL_GROWTH_RATE(长期名义GDP增速的代理)，而不是
    # 当期无风险利率。教科书的 g <= rf 成立的前提是 rf 反映长期名义增长；中国当前
    # 10 年期国债只有 1.7%，那是政策压低的结果，拿它当全市场的长期名义增长率会
    # 系统性低估。
    effective_terminal_growth = min(terminal_growth_rate, MAX_TERMINAL_GROWTH_RATE)

    # --- 归一化无风险利率 ---
    # 贴现率和永续增长率必须建立在**同一个**对长期名义增长的判断上，否则模型会从
    # 自相矛盾里凭空造出价值：用 1.68% 的当期国债利率折现，同时假设现金流永远以 3%
    # 增长，等于说"这家公司能永远跑赢无风险利率 1.3 个百分点还不承担风险"——终值
    # 会被这个缺口撑爆。实测拆掉股权成本地板后，Top15 的 WACC 全部落在 4.1%~6.6%，
    # 最高分那只只有 4.54%，低于它自己的债权成本，这不是"算准"是算错。
    #
    # 修法不是在输出端加地板(那会把全市场过半股票的股权成本压成同一个数，beta 白算)，
    # 而是在输入端归一化：利率处在历史低位时用"长期正常水平"代替当期观测值，这是
    # Damodaran 的 normalized risk-free rate 做法。取 max 之后 g <= rf 自动成立，
    # 截面上的 beta 差异也完整保留。
    normalized_risk_free_rate = max(risk_free_rate, effective_terminal_growth)

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
        # 实际税率和有息负债都先独立算好再喂给 WACC：这两个量在 WACC 之外还要被
        # FCFF 的税后利息加回用到，绑在 _wacc_components 里就会出现"市值缺失导致
        # WACC 算不出来、连带税率也退回默认值"这种没道理的连锁。
        tax_info = _effective_tax_rate(fina_history, income_history)
        effective_tax_rate = tax_info["rate"]
        debt_info = _interest_bearing_debt(fina_history, balancesheet_history)
        wacc_info = _wacc_components(
            beta=beta,
            market_cap=market_cap_yuan,
            interest_bearing_debt=debt_info["value"],
            interest_expense=safe_float(income_latest_row.get("fin_exp_int_exp")),
            effective_tax_rate=effective_tax_rate,
            risk_free_rate=normalized_risk_free_rate,
            equity_risk_premium=equity_risk_premium,
        )
        wacc_pct = safe_float(wacc_info["wacc"] * 100.0) if wacc_info else None
        cost_of_equity = wacc_info["cost_of_equity"] if wacc_info else None

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
                    "quality_notes": quality["notes"],
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
                median_pe = _history_median(history["pe_ttm"])
                if median_pe:
                    reversion_pct = (median_pe / pe_ttm - 1.0) * 100.0
            if reversion_pct is None and pb and pb > 0:
                median_pb = _history_median(history["pb"])
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
        # 从当前市净率反推市场隐含的可持续 ROE：模型和市场分歧大时，这个数比"合理
        # 市净率是账面的几倍"有用得多——它直接说出市场在假设什么。
        implied_roe = (
            _implied_sustainable_roe(pb, cost_of_equity, effective_terminal_growth)
            if is_financial
            else None
        )
        dcf_unavailable_reason = current_snapshot["unavailable_reason"]
        justified_pb = current_snapshot["justified_pb"]
        dcf_enterprise_value = current_snapshot["enterprise_value"]
        dcf_equity_value = current_snapshot["equity_value"]
        dcf_net_debt = current_snapshot["net_debt"]
        dcf_book_minority = current_snapshot["book_minority"] or 0.0
        dcf_base_nopat = current_snapshot["base_nopat"]
        dcf_roic = current_snapshot["roic"]
        fcf_yield_pct = None

        if is_financial:
            avg_roe_frac = current_snapshot["financial_roe"]
            if justified_pb is not None and pb and pb > 0:
                expected_return_pct = (justified_pb / pb - 1.0) * 100.0
                if cost_of_equity is not None:
                    bear_pb = _residual_income_pb(
                        avg_roe_frac, cost_of_equity + SCENARIO_DISCOUNT_RATE_SPREAD, effective_terminal_growth
                    )
                    bull_pb = _residual_income_pb(
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
                bear_valuation = _two_stage_reinvestment_value(
                    dcf_base_nopat, dcf_roic, bear_growth, effective_terminal_growth, bear_wacc
                )
                bull_valuation = (
                    _two_stage_reinvestment_value(
                        dcf_base_nopat, dcf_roic, bull_growth, effective_terminal_growth, bull_wacc
                    )
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
                "quality_notes": quality["notes"],
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
                "effective_tax_rate_pct": safe_float(effective_tax_rate * 100.0, 2),
                "effective_tax_rate_source": tax_info["source"],
                "interest_bearing_debt_yi": (
                    safe_float(debt_info["value"] / 1e8, 2) if debt_info["value"] is not None else None
                ),
                "interest_bearing_debt_source": debt_info["source"],
                "interest_bearing_debt_cross_check_ratio": safe_float(debt_info["cross_check_ratio"], 2),
                "roic_wacc_spread_pct": (
                    safe_float(avg_roic - wacc_pct, 2) if avg_roic is not None and wacc_pct is not None else None
                ),
                "dcf_base_nopat_yi": safe_float(dcf_base_nopat / 1e8, 2) if dcf_base_nopat else None,
                "dcf_roic_pct": safe_float(dcf_roic * 100.0, 2) if dcf_roic is not None else None,
                "dcf_roic_source": current_snapshot["roic_source"],
                "roic_cross_check_ratio": current_snapshot["roic_cross_check_ratio"],
                "dcf_terminal_roic_pct": (
                    safe_float(current_snapshot["terminal_roic"] * 100.0, 2)
                    if current_snapshot["terminal_roic"] is not None
                    else None
                ),
                "dcf_terminal_reinvestment_rate_pct": (
                    safe_float(current_snapshot["terminal_reinvestment_rate"] * 100.0, 1)
                    if current_snapshot["terminal_reinvestment_rate"] is not None
                    else None
                ),
                "cash_fcff_yi": (
                    safe_float(current_snapshot["cash_fcff"] / 1e8, 2)
                    if current_snapshot["cash_fcff"]
                    else None
                ),
                "cash_fcff_source": current_snapshot["cash_fcff_source"],
                "fcff_cross_check_ratio": current_snapshot["fcff_cross_check_ratio"],
                "revenue_cagr_pct": safe_float(current_snapshot["revenue_cagr_pct"], 1),
                "ebit_cagr_pct": safe_float(current_snapshot["ebit_cagr_pct"], 1),
                "profit_cagr_pct": safe_float(profit_cagr_pct, 1),
                "near_term_growth_pct": (
                    safe_float(current_snapshot["near_term_growth"] * 100.0, 1)
                    if current_snapshot["near_term_growth"] is not None
                    else None
                ),
                "terminal_growth_pct": (
                    safe_float(current_snapshot["applied_terminal_growth"] * 100.0, 2)
                    if current_snapshot["applied_terminal_growth"] is not None
                    else effective_terminal_growth * 100.0
                ),
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
                "financial_roe_pct": (
                    safe_float(current_snapshot["financial_roe"] * 100.0, 2)
                    if current_snapshot["financial_roe"] is not None
                    else None
                ),
                "implied_sustainable_roe_pct": (
                    safe_float(implied_roe * 100.0, 2) if implied_roe is not None else None
                ),
                "roe_vs_implied_gap_pct": (
                    safe_float((current_snapshot["financial_roe"] - implied_roe) * 100.0, 2)
                    if implied_roe is not None and current_snapshot["financial_roe"] is not None
                    else None
                ),
                "excess_return_fade_years": FINANCIAL_EXCESS_RETURN_FADE_YEARS if is_financial else None,
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
            "normalized_risk_free_rate_pct": normalized_risk_free_rate * 100.0,
            "valuation_model": "two_stage_reinvestment_fcff",
            "risk_free_rate_source": risk_free_rate_source,
            "equity_risk_premium_pct": equity_risk_premium * 100.0,
            "terminal_growth_rate_pct": effective_terminal_growth * 100.0,
            "terminal_growth_rate_requested_pct": terminal_growth_rate * 100.0,
            "max_terminal_growth_rate_pct": MAX_TERMINAL_GROWTH_RATE * 100.0,
            "min_wacc_terminal_spread_pct": MIN_WACC_TERMINAL_SPREAD * 100.0,
            "terminal_roic_convergence": TERMINAL_ROIC_CONVERGENCE,
            "market_index_code": MARKET_INDEX_CODE,
            "dcf_explicit_years": DCF_EXPLICIT_YEARS,
        },
        "candidates": ranked[: max(0, int(top_n))],
        "excluded_sample": excluded[: max(0, int(top_n))],
    }

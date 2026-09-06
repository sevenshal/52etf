"""价值投资扫描器：DCF 口径与失真闸门的回归测试。

这些用例都围绕生产环境跑出来的一个真实案例展开：华特达因(000915.SZ)算出了
1410% 的"潜在回报率"。当时的输入是 基准FCFF 14.68亿 / 归母净利约4.2亿 / 市值
59.5亿 / WACC 6.1% / FCFF复合增速 339.6%，四层问题叠乘的结果。下面按每一层
各锁一条：口径(少数股东权益)、数据源(FCFF 交叉验证)、增速(异常CAGR判废)、
贴现率与终值(股权成本地板 + 终值占比闸门)，外加悲观/乐观区间的自洽性。
"""

from datetime import date

import pandas as pd
import pytest

from src.core.services import value_investing_scanner as scanner


def _annual_frame(rows):
    frame = pd.DataFrame(rows)
    frame["end_date"] = [date(year, 12, 31) for year in frame["year"]]
    return frame.drop(columns=["year"]).sort_values("end_date").reset_index(drop=True)


@pytest.fixture
def huate_slices():
    """华特达因式的输入：全口径现金流很大，但一半属于少数股东，且FCFF基期接近0。"""
    years = [2021, 2022, 2023, 2024, 2025]
    # tushare 口径 FCFF：基期只有 330 万，近3年均值 14.68 亿——生产环境真实取到的
    # 那组数，量级是同期合并净利润(8.2亿)的近两倍
    reported_fcff = [3.3e6, 4.0e8, 1.35e9, 1.47e9, 1.585e9]
    # 现金流量表口径：经营现金流 - 资本开支，量级和合并净利润一致
    operating_cash = [7.0e8, 7.6e8, 8.2e8, 8.8e8, 9.2e8]
    capex = [5.0e7] * 5
    return {
        "fina": _annual_frame(
            [
                {
                    "year": year,
                    "roe": 16.0,
                    "roic": 23.8,
                    "debt_to_assets": 13.0,
                    "interestdebt": 0.0,
                    "fcff": fcff,
                    "netdebt": -2.014e9,
                }
                for year, fcff in zip(years, reported_fcff)
            ]
        ),
        "income": _annual_frame(
            [
                {
                    "year": year,
                    "n_income_attr_p": 4.2e8,   # 归母净利
                    "n_income": 8.2e8,          # 合并净利：归母只占 51.2%
                    "minority_gain": 4.0e8,
                    "total_profit": 1.0e9,
                    "income_tax": 1.5e8,
                    "fin_exp_int_exp": 0.0,
                }
                for year in years
            ]
        ),
        "cashflow": _annual_frame(
            [
                {"year": year, "net_profit": 8.2e8, "n_cashflow_act": ocf, "c_pay_acq_const_fiolta": capex_value}
                for year, ocf, capex_value in zip(years, operating_cash, capex)
            ]
        ),
        # 归母权益 26 亿、少数股东权益 24 亿：利润几乎都来自持股约五成的控股子公司
        "balancesheet": _annual_frame(
            [
                {
                    "year": year,
                    "comp_type": "1",
                    "money_cap": 2.5e9,
                    "minority_int": 2.4e9,
                    "total_hldr_eqy_exc_min_int": 2.6e9,
                    "total_hldr_eqy_inc_min_int": 5.0e9,
                }
                for year in years
            ]
        ),
    }


def _snapshot(slices, **overrides):
    kwargs = {
        "is_financial": False,
        "fina_slice": slices["fina"],
        "income_slice": slices["income"],
        "cashflow_slice": slices["cashflow"],
        "balancesheet_slice": slices["balancesheet"],
        "cost_of_equity": 0.08,
        "wacc": 0.08,
        "effective_tax_rate": 0.15,
        "terminal_growth_rate": 0.0168,
    }
    kwargs.update(overrides)
    return scanner._intrinsic_value_snapshot(**kwargs)


def test_fcff_base_comes_from_cash_flow_statement_not_tushare(huate_slices):
    """tushare 的 fcff 与现金流量表口径差一倍以上时，DCF 基准取后者。"""
    snapshot = _snapshot(huate_slices)

    assert snapshot["fcff_source"] == "cashflow_statement"
    # 现金流量表口径近3年均值 = (8.2+8.8+9.2)/3 - 0.5 = 8.23亿，而不是 tushare 的 14.68亿
    assert snapshot["base_fcff"] == pytest.approx(8.23e8, rel=0.01)
    # 交叉验证倍数落在结果里，读作"tushare 的基准 FCFF 是报表口径的 1.78 倍"
    assert snapshot["fcff_cross_check_ratio"] == pytest.approx(1.78, rel=0.02)


def test_absurd_fcff_cagr_is_discarded_instead_of_clipped_to_the_bullish_bound(huate_slices):
    """基期接近0算出的天文数字增速要判废，不能被 clip 成"允许范围内最乐观"的假设。"""
    reported_years = [2021, 2022, 2023, 2024, 2025]
    reported_values = [3.3e6, 4.0e8, 1.35e9, 1.47e9, 1.585e9]

    # 旧口径：直接拿首尾两个单点，算出 339% 这种只反映基期噪声的"复合增速"
    assert scanner._cagr_pct(reported_values[-1], reported_values[0], 4) is None
    # 新口径：首尾各取2期均值，把基期噪声压下去
    smoothed = scanner._series_cagr_pct(reported_values, reported_years)
    assert smoothed is not None and smoothed < 120.0

    # 而 DCF 实际用的是现金流量表口径那条序列，近端增速远低于 clip 上界——
    # 关键是它不再是"垃圾输入被翻译成允许范围内最乐观的假设"
    snapshot = _snapshot(huate_slices)
    assert snapshot["near_term_growth"] < scanner.NEAR_TERM_GROWTH_BOUNDS[1]


def test_equity_value_deducts_the_proportionate_minority_claim_not_just_book(huate_slices):
    """子公司高回报时，账面少数股东权益远小于少数股东真正的 DCF 索取权。"""
    snapshot = _snapshot(huate_slices)

    # 归母利润占比 4.2/8.2 = 51.2%
    assert snapshot["parent_profit_share"] == pytest.approx(0.5122, rel=0.01)
    assert snapshot["book_minority"] == pytest.approx(2.4e9)
    assert snapshot["minority_basis"] == "proportionate"
    # 账面 24 亿，但按比例折出来的索取权是全体股东股权价值的 48.8%，远大于账面
    assert snapshot["minority_interest"] > snapshot["book_minority"]
    gross_equity = snapshot["enterprise_value"] - snapshot["net_debt"]
    assert snapshot["equity_value"] == pytest.approx(gross_equity * 0.5122, rel=0.01)


def test_book_minority_wins_when_it_is_larger_than_the_proportionate_claim(huate_slices):
    """两种口径取较大的那个：子公司不赚钱时，比例法会低估少数股东的索取权。"""
    balancesheet = huate_slices["balancesheet"].copy()
    balancesheet["minority_int"] = 2.0e10  # 账面少数股东权益远大于按比例折出来的

    snapshot = _snapshot(huate_slices, balancesheet_slice=balancesheet)
    assert snapshot["minority_basis"] == "book"
    assert snapshot["minority_interest"] == pytest.approx(2.0e10)


def test_parent_profit_share_falls_back_through_three_sources(huate_slices):
    """n_income → minority_gain 反推 → 归母权益/全部权益，逐级兜底。"""
    income = huate_slices["income"]
    balancesheet = huate_slices["balancesheet"]

    assert scanner._parent_profit_share(income, balancesheet) == pytest.approx(0.5122, rel=0.01)

    without_n_income = income.copy()
    without_n_income["n_income"] = None
    assert scanner._parent_profit_share(without_n_income, balancesheet) == pytest.approx(0.5122, rel=0.01)

    without_income_fields = without_n_income.copy()
    without_income_fields["minority_gain"] = None
    # 退回资产负债表：归母权益 26 亿 / 全部权益 50 亿 = 52%
    assert scanner._parent_profit_share(without_income_fields, balancesheet) == pytest.approx(0.52)


def test_missing_minority_data_entirely_falls_back_to_book_only(huate_slices):
    """利润表和资产负债表的归属口径都拿不到时，退回纯账面扣除，不能当成没有少数股东。"""
    income = huate_slices["income"].copy()
    income["n_income"] = None
    income["minority_gain"] = None
    balancesheet = huate_slices["balancesheet"].copy()
    balancesheet["total_hldr_eqy_exc_min_int"] = None
    balancesheet["total_hldr_eqy_inc_min_int"] = None

    snapshot = _snapshot(huate_slices, income_slice=income, balancesheet_slice=balancesheet)
    assert snapshot["parent_profit_share"] is None
    assert snapshot["minority_basis"] == "book"
    assert snapshot["minority_interest"] == pytest.approx(2.4e9)


def test_cost_of_equity_has_a_floor_when_risk_free_rate_is_very_low():
    """1.7% 的无风险利率 + 低 beta 会让 CAPM 跌到 6%，那不是可用的贴现率。"""
    components = scanner._wacc_components(
        beta=0.6,
        market_cap=5.95e9,
        interest_bearing_debt=0.0,
        interest_expense=None,
        effective_tax_rate=0.15,
        risk_free_rate=0.0168,
        equity_risk_premium=0.06,
    )

    assert components["cost_of_equity"] == pytest.approx(scanner.MIN_COST_OF_EQUITY)
    assert components["wacc"] == pytest.approx(scanner.MIN_COST_OF_EQUITY)


def test_dcf_rejects_valuations_that_are_almost_entirely_terminal_value():
    """WACC-g 利差刚过闸门、但 88% 的价值来自终值时，判定不可用而不是硬给一个数。"""
    wide_spread = scanner._two_stage_fcff_value(1.0e9, 0.05, 0.02, 0.12)
    assert wide_spread is not None
    assert wide_spread["terminal_share"] <= scanner.MAX_TERMINAL_VALUE_SHARE

    # WACC 6.1% / g 3%：利差 3.1pp 刚好越过 MIN_WACC_TERMINAL_SPREAD，正是老口径放行
    # 华特达因的那组参数，终值占比 88%。
    narrow_spread = scanner._two_stage_fcff_value(1.468e9, 0.30, 0.03, 0.061)
    assert narrow_spread is None


def test_huate_style_case_no_longer_produces_a_four_digit_return(huate_slices):
    """把所有修复叠在一起：同样的公司，回报率必须回到可讨论的量级。"""
    snapshot = _snapshot(huate_slices)
    market_cap = 5.95e9

    expected_return_pct = (snapshot["equity_value"] / market_cap - 1.0) * 100.0
    assert expected_return_pct < 100.0
    assert snapshot["terminal_value_share"] <= scanner.MAX_TERMINAL_VALUE_SHARE


def test_bull_case_never_discounts_at_a_higher_rate_than_the_base_case():
    """乐观情形的贴现率必须低于基准，否则"乐观"会算出比基准还差的回报率。"""
    base_wacc = 0.061
    bull_wacc = base_wacc - scanner.SCENARIO_DISCOUNT_RATE_SPREAD
    assert bull_wacc < base_wacc

    base = scanner._two_stage_fcff_value(1.0e9, 0.05, 0.0168, 0.09)
    bull = scanner._two_stage_fcff_value(
        1.0e9, 0.05, 0.0168, 0.09 - scanner.SCENARIO_DISCOUNT_RATE_SPREAD
    )
    bear = scanner._two_stage_fcff_value(
        1.0e9, 0.05, 0.0168, 0.09 + scanner.SCENARIO_DISCOUNT_RATE_SPREAD
    )
    assert bear["enterprise_value"] < base["enterprise_value"] < bull["enterprise_value"]


def test_scenario_growth_stays_inside_the_near_term_growth_bounds():
    """乐观乘数施加在已经 clip 过的增速上，要重新 clip 才不会突破上界。"""
    near_term_growth = scanner.NEAR_TERM_GROWTH_BOUNDS[1]
    bull_growth = scanner._clip(
        near_term_growth * scanner.SCENARIO_GROWTH_MULTIPLIER[1], *scanner.NEAR_TERM_GROWTH_BOUNDS
    )
    assert bull_growth == scanner.NEAR_TERM_GROWTH_BOUNDS[1]


# --- 实际税率 / 有息负债的口径与兜底 -----------------------------------------


def test_effective_tax_rate_prefers_latest_annual_report(huate_slices):
    info = scanner._effective_tax_rate(huate_slices["fina"], huate_slices["income"])
    assert info["source"] == "latest_annual"
    assert info["rate"] == pytest.approx(0.15)  # 1.5亿 / 10亿


def test_effective_tax_rate_falls_back_to_five_year_window_on_a_loss_year(huate_slices):
    """最新一年亏损时不该退回猜 25%，五年窗口合计仍然算得出真实税率。

    全市场有 28.3% 的股票最新一期 total_profit<=0，改窗口口径能救回其中 9.5%。
    """
    income = huate_slices["income"].copy()
    income.loc[income.index[-1], "total_profit"] = -5.0e8
    income.loc[income.index[-1], "income_tax"] = 0.0

    info = scanner._effective_tax_rate(huate_slices["fina"], income)
    assert info["source"] == "five_year_window"
    # 前4年各 1.5亿/10亿，最新一年 0/-5亿 → 6.0亿 / 35亿 = 17.1%
    assert info["rate"] == pytest.approx(6.0e8 / 3.5e9, rel=0.01)


def test_effective_tax_rate_falls_back_to_tushare_then_default(huate_slices):
    """利润表整段不可用时依次退到 tax_to_ebt、再退到默认值。"""
    income = huate_slices["income"].copy()
    income["total_profit"] = None
    income["income_tax"] = None

    fina = huate_slices["fina"].copy()
    fina["tax_to_ebt"] = 12.5  # tushare 给的是百分数
    info = scanner._effective_tax_rate(fina, income)
    assert info["source"] == "tushare_tax_to_ebt"
    assert info["rate"] == pytest.approx(0.125)

    info = scanner._effective_tax_rate(huate_slices["fina"], income)
    assert info["source"] == "default_fallback"
    assert info["rate"] == pytest.approx(scanner.DEFAULT_EFFECTIVE_TAX_RATE)


def test_effective_tax_rate_clips_distorted_ratios(huate_slices):
    """利润总额接近0会让比值失真(实测有 422% 的样本)，必须 clip 住。"""
    income = huate_slices["income"].copy()
    income.loc[income.index[-1], "total_profit"] = 1.0e5
    income.loc[income.index[-1], "income_tax"] = 5.0e5  # 500%

    info = scanner._effective_tax_rate(huate_slices["fina"], income)
    assert info["rate"] == pytest.approx(0.33)


def test_interest_bearing_debt_prefers_tushare_and_reports_cross_check(huate_slices):
    fina = huate_slices["fina"].copy()
    fina["interestdebt"] = 1.0e9
    balancesheet = huate_slices["balancesheet"].copy()
    for column, value in (("st_borr", 4.0e8), ("lt_borr", 5.0e8), ("lease_liab", 1.0e8)):
        balancesheet[column] = value

    info = scanner._interest_bearing_debt(fina, balancesheet)
    assert info["source"] == "tushare_interestdebt"
    assert info["value"] == pytest.approx(1.0e9)
    assert info["cross_check_ratio"] == pytest.approx(1.0)


def test_interest_bearing_debt_falls_back_to_balance_sheet_components(huate_slices):
    """interestdebt 缺失时从资产负债表加出来，而不是当成 0(那会让 WACC 退化成股权成本)。

    实测全市场 105 只缺 interestdebt 的股票里，104 只能这样补回来。
    """
    fina = huate_slices["fina"].copy()
    fina["interestdebt"] = None
    balancesheet = huate_slices["balancesheet"].copy()
    balancesheet["st_borr"] = 3.0e8
    balancesheet["non_cur_liab_due_1y"] = 1.0e8
    balancesheet["lease_liab"] = 5.0e7

    info = scanner._interest_bearing_debt(fina, balancesheet)
    assert info["source"] == "balancesheet_components"
    assert info["value"] == pytest.approx(4.5e8)
    assert info["cross_check_ratio"] is None


def test_interest_bearing_debt_missing_everywhere_stays_none(huate_slices):
    """构成项全为空是"这期资产负债表没同步到"，不能和"真的没有有息负债"混为一谈。"""
    fina = huate_slices["fina"].copy()
    fina["interestdebt"] = None

    info = scanner._interest_bearing_debt(fina, huate_slices["balancesheet"])
    assert info["value"] is None
    assert info["source"] is None


def test_zero_interest_bearing_debt_is_distinct_from_missing(huate_slices):
    """真的没有有息负债(全部构成项=0)要返回 0.0，不是 None。"""
    fina = huate_slices["fina"].copy()
    fina["interestdebt"] = None
    balancesheet = huate_slices["balancesheet"].copy()
    for column in scanner.INTEREST_BEARING_DEBT_COMPONENTS:
        balancesheet[column] = 0.0

    info = scanner._interest_bearing_debt(fina, balancesheet)
    assert info["value"] == 0.0
    assert info["source"] == "balancesheet_components"

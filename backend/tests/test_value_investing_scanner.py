"""价值投资扫描器：估值口径的回归测试。

这些用例围绕生产环境跑出来的真实案例：华特达因(000915.SZ)最初算出 1410% 的"潜在
回报率"。fixture 用的是它 2025 年报的真实数量级(归母净利 5.35亿 / 合并净利 10.29亿 /
少数股东损益 4.93亿 / 经营现金流 12.72亿 / 资本开支 0.48亿 / 少数股东权益 14.90亿 /
归母权益 28.61亿 / 全部权益 43.51亿 / ROIC 23.8% / 市值 59.5亿)。

核心的方法论主张只有一条：**增长不是免费的**。要长得快就得把 NOPAT 投回去，
自由现金流相应减少(`g = 再投资率 × ROIC`)。下面大部分用例是在锁这一条，以及它
替换掉的那些"把数字压下去"的补丁。
"""

from datetime import date

import pandas as pd
import pytest

from src.core.services import value_investing_scanner as scanner

MARKET_CAP = 5.95e9
YEARS = [2021, 2022, 2023, 2024, 2025]


def _annual_frame(rows):
    frame = pd.DataFrame(rows)
    frame["end_date"] = [date(year, 12, 31) for year in frame["year"]]
    return frame.drop(columns=["year"]).sort_values("end_date").reset_index(drop=True)


@pytest.fixture
def huate_slices():
    """华特达因式的输入：ROIC 高、少数股东占了近一半、经营现金流略高于净利。"""
    revenue = [2.027e9, 2.341e9, 2.484e9, 2.134e9, 2.228e9]
    ebit = [8.3e8, 1.17e9, 1.33e9, 1.18e9, 1.21e9]
    consolidated_profit = [7.04e8, 9.98e8, 1.127e9, 1.006e9, 1.029e9]
    parent_profit = [3.80e8, 5.27e8, 5.85e8, 5.16e8, 5.35e8]
    minority_gain = [3.24e8, 4.71e8, 5.42e8, 4.90e8, 4.93e8]
    operating_cash = [9.5e8, 1.02e9, 1.175e9, 9.30e8, 1.272e9]
    invested_capital = [3.6e9, 3.9e9, 4.2e9, 4.25e9, 4.32e9]
    return {
        "fina": _annual_frame(
            [
                {
                    "year": year,
                    "roe": 19.4,
                    "roe_waa": 19.0,
                    "roic": 23.8,
                    "ebit": ebit_value,
                    "invest_capital": capital,
                    "daa": 6.0e7,
                    "tax_to_ebt": 15.0,
                    "debt_to_assets": 12.95,
                    "interestdebt": 0.0,
                    "netdebt": -2.014e9,
                }
                for year, ebit_value, capital in zip(YEARS, ebit, invested_capital)
            ]
        ),
        "income": _annual_frame(
            [
                {
                    "year": year,
                    "revenue": rev,
                    "n_income_attr_p": parent,
                    "n_income": consolidated,
                    "minority_gain": minority,
                    "total_profit": consolidated / 0.85,
                    "income_tax": consolidated / 0.85 * 0.15,
                    "fin_exp_int_exp": 0.0,
                }
                for year, rev, parent, consolidated, minority in zip(
                    YEARS, revenue, parent_profit, consolidated_profit, minority_gain
                )
            ]
        ),
        "cashflow": _annual_frame(
            [
                {
                    "year": year,
                    "net_profit": profit,
                    "n_cashflow_act": ocf,
                    "c_pay_acq_const_fiolta": 4.8e7,
                }
                for year, profit, ocf in zip(YEARS, consolidated_profit, operating_cash)
            ]
        ),
        "balancesheet": _annual_frame(
            [
                {
                    "year": year,
                    "comp_type": "1",
                    "money_cap": 2.5e9,
                    "minority_int": 1.49e9,
                    "total_hldr_eqy_exc_min_int": 2.861e9,
                    "total_hldr_eqy_inc_min_int": 4.351e9,
                }
                for year in YEARS
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
        "terminal_growth_rate": 0.03,
    }
    kwargs.update(overrides)
    return scanner._intrinsic_value_snapshot(**kwargs)


# --- 核心：增长必须付再投资的代价 -------------------------------------------


def test_growth_is_paid_for_out_of_nopat():
    """同样的 NOPAT 和 WACC，增长越快再投资越多——增长不是免费的。"""
    slow = scanner._two_stage_reinvestment_value(1.0e9, 0.20, 0.02, 0.02, 0.09)
    fast = scanner._two_stage_reinvestment_value(1.0e9, 0.20, 0.15, 0.02, 0.09)
    assert slow is not None and fast is not None
    # 增长仍然是加分的(ROIC 20% > WACC 9%)，但没有旧模型那种"白拿"的放大
    assert fast["enterprise_value"] > slow["enterprise_value"]
    assert fast["explicit_reinvestment"] > slow["explicit_reinvestment"]


def test_low_roic_makes_growth_destroy_value():
    """ROIC 低于 WACC 时增长越快企业价值越低——"增长毁灭价值"的正确表达。

    旧模型靠一道单独的 ROIC-WACC 闸门表达这件事，估值公式本身照样把低 ROIC 公司的
    历史现金流按高增长外推。现在它内生在公式里。
    """
    no_growth = scanner._two_stage_reinvestment_value(1.0e9, 0.05, 0.0, 0.0, 0.09)
    with_growth = scanner._two_stage_reinvestment_value(1.0e9, 0.05, 0.04, 0.03, 0.09)
    assert no_growth is not None and with_growth is not None
    assert with_growth["enterprise_value"] < no_growth["enterprise_value"]


def test_capex_below_depreciation_no_longer_inflates_value(huate_slices):
    """历史资本开支低于折旧不再能抬高估值——DCF 起点是 NOPAT，不是历史自由现金流。

    实测全市场 49.9% 的公司 capex < D&A，旧模型把它们的历史自由现金流按永续增长
    外推，等于假设资产永远不用更新还能一直长大。
    """
    cashflow = huate_slices["cashflow"].copy()
    cashflow["c_pay_acq_const_fiolta"] = 1.0e6  # 资本开支几乎为零

    baseline = _snapshot(huate_slices)
    starved = _snapshot(huate_slices, cashflow_slice=cashflow)
    assert baseline["enterprise_value"] == pytest.approx(starved["enterprise_value"])


def test_terminal_roic_converges_halfway_towards_wacc():
    """永续期继续用当前高 ROIC 等于假设护城河永不失效，收敛一半更合理。"""
    assert scanner._terminal_roic(0.24, 0.08) == pytest.approx(0.16)
    # ROIC 低于 WACC 时不再往下收敛：那种公司本来就不该按永续增长估
    assert scanner._terminal_roic(0.05, 0.09) == pytest.approx(0.09)


def test_growth_cannot_exceed_what_roic_can_fund():
    """g/ROIC > 1 意味着再投资超过 NOPAT、自由现金流永远为负，是自相矛盾的假设。"""
    result = scanner._two_stage_reinvestment_value(1.0e9, 0.06, 0.30, 0.05, 0.09)
    assert result is not None
    assert result["applied_near_term_growth"] <= 0.06 * scanner.MAX_REINVESTMENT_RATE
    assert result["terminal_reinvestment_rate"] <= 1.0


# --- 现金流量表口径 FCFF 的中国准则修正 ---------------------------------------


def test_cash_fcff_subtracts_the_interest_tax_shield_not_adds_back_interest(huate_slices):
    """中国准则下利息付现在筹资活动里，经营现金流已经是付息前口径。

    美国准则的 `CFO + I(1−t) − capex` 直接套用会多算大约一整笔利息费用。CAS 下
    `CFO − capex` 已经等于 FCFF 加一笔利息税盾，要减掉 I·t。
    """
    income = huate_slices["income"].copy()
    income["fin_exp_int_exp"] = 1.0e8

    history = scanner._fcff_history(
        fina_slice=huate_slices["fina"],
        cashflow_slice=huate_slices["cashflow"],
        income_slice=income,
        effective_tax_rate=0.15,
    )
    # 2025: OCF 12.72亿 − capex 0.48亿 − 利息税盾 1亿×15% = 12.09亿
    assert history["values"][-1] == pytest.approx(1.272e9 - 4.8e7 - 1.5e7)


# --- 归母口径 ---------------------------------------------------------------


def test_equity_value_uses_the_proportionate_minority_claim(huate_slices):
    """能算出归母利润占比时就按比例切，不再和账面取较大者。

    取较大者是保守化不是准确化：那等于在两个估计之间系统性偏向低估归母价值，
    方向性偏差和高估一样是错的。
    """
    snapshot = _snapshot(huate_slices)

    # 近3年归母 (5.85+5.16+5.35) / 合并 (11.27+10.06+10.29) = 51.9%
    assert snapshot["parent_profit_share"] == pytest.approx(0.519, abs=0.01)
    assert snapshot["minority_basis"] == "proportionate"
    gross_equity = snapshot["enterprise_value"] - snapshot["net_debt"]
    assert snapshot["equity_value"] == pytest.approx(
        gross_equity * snapshot["parent_profit_share"]
    )


def test_parent_profit_share_falls_back_through_three_sources(huate_slices):
    """n_income → minority_gain 反推 → 归母权益/全部权益，逐级兜底。"""
    income = huate_slices["income"]
    balancesheet = huate_slices["balancesheet"]
    assert scanner._parent_profit_share(income, balancesheet) == pytest.approx(0.519, abs=0.01)

    without_n_income = income.copy()
    without_n_income["n_income"] = None
    assert scanner._parent_profit_share(without_n_income, balancesheet) == pytest.approx(
        0.519, abs=0.01
    )

    without_income_fields = without_n_income.copy()
    without_income_fields["minority_gain"] = None
    # 退回资产负债表：归母权益 28.61亿 / 全部权益 43.51亿 = 65.8%
    assert scanner._parent_profit_share(without_income_fields, balancesheet) == pytest.approx(
        0.658, abs=0.01
    )


def test_missing_minority_data_falls_back_to_book(huate_slices):
    """归属口径完全拿不到时退回账面扣除，不能当成没有少数股东。"""
    income = huate_slices["income"].copy()
    income["n_income"] = None
    income["minority_gain"] = None
    balancesheet = huate_slices["balancesheet"].copy()
    balancesheet["total_hldr_eqy_exc_min_int"] = None
    balancesheet["total_hldr_eqy_inc_min_int"] = None

    snapshot = _snapshot(huate_slices, income_slice=income, balancesheet_slice=balancesheet)
    assert snapshot["parent_profit_share"] is None
    assert snapshot["minority_basis"] == "book"
    assert snapshot["minority_interest"] == pytest.approx(1.49e9)


# --- 拆掉的那些"压数字"的补丁 -------------------------------------------------


def test_cost_of_equity_is_plain_capm_without_an_output_floor():
    """股权成本不再套 8% 地板：那让全市场过半股票的贴现率变成同一个数，beta 白算。"""
    components = scanner._wacc_components(
        beta=0.6,
        market_cap=MARKET_CAP,
        interest_bearing_debt=0.0,
        interest_expense=None,
        effective_tax_rate=0.15,
        risk_free_rate=0.0168,
        equity_risk_premium=0.06,
    )
    # Blume 调整后 beta = 2/3×0.6 + 1/3 = 0.7333
    assert components["cost_of_equity"] == pytest.approx(0.0168 + 0.7333 * 0.06, abs=1e-4)
    assert components["cost_of_equity"] < 0.08


def test_terminal_share_is_reported_but_never_blocks_a_valuation():
    """终值占比只是诊断字段：5年显式期的DCF终值占70~80%本来就是常态。"""
    result = scanner._two_stage_reinvestment_value(1.0e9, 0.20, 0.10, 0.03, 0.07)
    assert result is not None
    assert 0.0 < result["terminal_share"] < 1.0


# --- 增速来源 ---------------------------------------------------------------


def test_growth_uses_the_median_of_revenue_ebit_and_profit(huate_slices):
    """不再用 FCFF 复合增速——那是所有序列里噪声最大的一条(曾算出 339.6%)。"""
    info = scanner._fundamental_growth(huate_slices["income"], huate_slices["fina"])
    assert info["revenue_cagr_pct"] is not None
    assert info["ebit_cagr_pct"] is not None
    assert info["profit_cagr_pct"] is not None
    ordered = sorted([info["revenue_cagr_pct"], info["ebit_cagr_pct"], info["profit_cagr_pct"]])
    assert info["growth"] == pytest.approx(ordered[1] / 100.0)


def test_absurd_cagr_is_discarded_not_clipped_to_the_bullish_bound():
    """基期接近0算出的天文数字增速要判废，不能被 clip 成允许范围内最乐观的假设。"""
    assert scanner._cagr_pct(1.226e9, 3.3e6, 4) is None


# --- ROIC 交叉验证 -----------------------------------------------------------


def test_roic_prefers_tushare_and_reports_the_cross_check(huate_slices):
    """ROIC 同时决定质量闸门和再投资率，不能只信单一厂商聚合值(实测相关系数 0.71)。"""
    info = scanner._roic_estimate(huate_slices["fina"], 0.15)
    assert info["source"] == "tushare_roic"
    assert info["value"] == pytest.approx(0.238)
    assert info["cross_check_ratio"] is not None


def test_roic_falls_back_to_self_computed_when_tushare_is_missing(huate_slices):
    fina = huate_slices["fina"].copy()
    fina["roic"] = None

    info = scanner._roic_estimate(fina, 0.15)
    assert info["source"] == "computed_nopat_over_invested_capital"
    assert 0.15 < info["value"] < 0.30


# --- 估值历史的最小样本保护 ---------------------------------------------------


def test_reversion_needs_the_same_minimum_sample_as_the_percentile():
    """pe_ttm 在生产库里只有 9 个交易日历史，拿它算"5年中位PE"是误导而不是缺失。"""
    short_history = pd.Series([12.0] * 9)
    assert scanner._history_median(short_history) is None
    assert scanner._percentile_rank(11.0, short_history) is None

    long_history = pd.Series([12.0] * scanner.VALUATION_HISTORY_MIN_OBSERVATIONS)
    assert scanner._history_median(long_history) == pytest.approx(12.0)


# --- 质量闸门 ---------------------------------------------------------------


def _quality(**overrides):
    kwargs = {
        "is_financial": False,
        "avg_roe": 19.4,
        "avg_roic": 23.8,
        "wacc_pct": 8.0,
        "years_available": 5,
        "ocf_to_np": 1.07,
        "fcf_positive_years": 5,
        "debt_to_assets": 12.95,
        "value_growth_pct": 5.0,
        "thresholds": dict(scanner.DEFAULT_QUALITY_THRESHOLDS),
    }
    kwargs.update(overrides)
    return scanner._quality_assessment(**kwargs)


def test_unknown_value_growth_is_a_note_not_a_rejection():
    """算不出去年快照 ≠ 基本面恶化。以前这是第一大拦截原因(全市场 1225 只)。"""
    quality = _quality(value_growth_pct=None)
    assert quality["passes"] is True
    assert any("未参与判断" in note for note in quality["notes"])


def test_declining_intrinsic_value_still_fails():
    """真的算出来在倒退，仍然要拦。"""
    assert _quality(value_growth_pct=-12.0)["passes"] is False


# --- 端到端 ------------------------------------------------------------------


def test_huate_case_lands_in_a_defensible_range(huate_slices):
    """把所有修复叠起来跑真实数量级的输入，结果要落在可讨论的区间。"""
    snapshot = _snapshot(huate_slices)
    expected_return_pct = (snapshot["equity_value"] / MARKET_CAP - 1.0) * 100.0

    assert -50.0 < expected_return_pct < 200.0
    assert snapshot["minority_basis"] == "proportionate"
    assert snapshot["roic"] == pytest.approx(0.238)
    assert snapshot["base_nopat"] > 0


def test_bear_base_bull_are_monotonic(huate_slices):
    """基准值必须落在自己的悲观~乐观区间之内。"""
    snapshot = _snapshot(huate_slices)
    roic, nopat = snapshot["roic"], snapshot["base_nopat"]
    growth = snapshot["near_term_growth"]

    bear = scanner._two_stage_reinvestment_value(nopat, roic, growth * 0.5, 0.03, 0.095)
    base = scanner._two_stage_reinvestment_value(nopat, roic, growth, 0.03, 0.08)
    bull = scanner._two_stage_reinvestment_value(nopat, roic, growth * 1.3, 0.03, 0.065)
    assert bear["enterprise_value"] < base["enterprise_value"] < bull["enterprise_value"]


# --- 金融股：剩余收益模型 -----------------------------------------------------


def test_residual_income_pb_is_exactly_one_when_roe_equals_cost_of_equity():
    """不赚超额收益的公司就值账面价值——模型必须在这一点上精确。"""
    assert scanner._residual_income_pb(0.066, 0.066, 0.03) == pytest.approx(1.0)


def test_residual_income_pb_is_far_less_sensitive_than_the_gordon_form():
    """换模型的理由就是这个：Gordon 形式对银行没有分辨力。

    银行 beta 低导致股权成本只有 5~7%，配 3% 永续增长，`(ROE−g)/(r−g)` 的分母只剩
    1.4~2.4 个百分点，r 差 50bp 结果就动 25%——输出的精度是假的。
    """
    def gordon(roe, r, g):
        return (roe - g) / (r - g)

    roe, g = 0.135, 0.03
    gordon_swing = abs(gordon(roe, 0.071, g) / gordon(roe, 0.061, g) - 1)
    residual_swing = abs(
        scanner._residual_income_pb(roe, 0.071, g) / scanner._residual_income_pb(roe, 0.061, g) - 1
    )
    assert gordon_swing > 0.20
    assert residual_swing < 0.06


def test_residual_income_pb_rises_with_roe_but_stays_bounded():
    """ROE 越高市净率越高，但不会像 Gordon 形式那样冲到 3~4 倍。"""
    low = scanner._residual_income_pb(0.08, 0.066, 0.03)
    high = scanner._residual_income_pb(0.16, 0.066, 0.03)
    assert low < high < 2.0


def test_roe_below_cost_of_equity_gives_pb_under_one():
    """赚不回股权成本的银行应该低于账面价值。"""
    assert scanner._residual_income_pb(0.04, 0.066, 0.03) < 1.0


def test_implied_roe_inverts_the_residual_income_formula():
    """从市净率反推的隐含ROE，代回公式要能还原出同一个市净率。"""
    implied = scanner._implied_sustainable_roe(0.6, 0.066, 0.03)
    assert implied is not None
    # 反推假设 ROE 在窗口内保持不变，所以代回时不能走衰减路径，直接用折现因子还原
    factor = scanner._excess_return_discount_factor(0.066, 0.03, scanner.FINANCIAL_EXCESS_RETURN_FADE_YEARS)
    assert 1.0 + (implied - 0.066) * factor == pytest.approx(0.6)


def test_implied_roe_exposes_how_much_the_market_disbelieves_reported_roe():
    """中国银行股 0.6 倍市净率隐含的可持续ROE只有个位数，远低于财报上的十几个点。

    这个差距就是市场对资产质量/隐性风险的定价——纯 CAPM+ROE 框架看不见的部分。
    与其硬给一个"合理市净率是账面3倍"的结论，不如把市场的假设摆出来。
    """
    implied = scanner._implied_sustainable_roe(0.6, 0.066, 0.03)
    assert implied < 0.05

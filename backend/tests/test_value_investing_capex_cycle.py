"""扩产期通道：自由现金流为负，到底是在铺产能还是在失血。

这条通道存在的理由是模型内部的一致性：DCF 用 `再投资率 = g/ROIC` 给增长计价，
本来就没有减实际资本开支；质量闸门再用实际 FCFF 卡一次，等于为同一笔增长罚两次。
所以扩产期公司的 FCFF 为负从淘汰理由降级成提示——**但也仅限这一条**，其余闸门照旧。
"""

from datetime import date

import pandas as pd
import pytest

from src.core.services import value_investing_scanner as scanner

YEARS = [2021, 2022, 2023, 2024, 2025]


def _frame(rows):
    return pd.DataFrame(rows).sort_values("end_date").reset_index(drop=True)


def _cashflow(ocf_values, capex_values):
    return _frame([
        {"ts_code": "T", "end_date": date(year, 12, 31), "ann_date": date(year + 1, 4, 20),
         "n_cashflow_act": ocf, "c_pay_acq_const_fiolta": capex}
        for year, ocf, capex in zip(YEARS, ocf_values, capex_values)
    ])


def _fina(daa_values):
    return _frame([
        {"ts_code": "T", "end_date": date(year, 12, 31), "ann_date": date(year + 1, 4, 20), "daa": daa}
        for year, daa in zip(YEARS, daa_values)
    ])


def _assess(ocf, capex, daa, revenue_cagr_pct=8.0):
    return scanner._capex_cycle_assessment(
        cashflow_slice=_cashflow(ocf, capex),
        fina_slice=_fina(daa),
        revenue_cagr_pct=revenue_cagr_pct,
    )


BUILDING_OUT = dict(ocf=[30e8] * 5, capex=[60e8] * 5, daa=[20e8] * 5)   # 经营造血、产能猛铺
BLEEDING = dict(ocf=[-5e8] * 5, capex=[60e8] * 5, daa=[20e8] * 5)       # 经营就是负的


def test_building_out_capacity_is_recognised_as_a_capex_cycle():
    result = _assess(**BUILDING_OUT)

    assert result["in_capex_cycle"] is True
    assert result["capex_to_daa"] == pytest.approx(3.0)
    assert result["ocf_positive_years"] == 5


def test_negative_operating_cash_flow_is_bleeding_not_building():
    """经营现金流都是负的，那不是在扩产，是在烧钱——这条通道绝不能放它过去。"""
    result = _assess(**BLEEDING)

    assert result["in_capex_cycle"] is False
    assert result["ocf_positive_years"] == 0


def test_shrinking_revenue_is_not_a_capex_cycle():
    """收入在萎缩还猛投，是给一门衰退的生意加杠杆。"""
    assert _assess(**BUILDING_OUT, revenue_cagr_pct=-4.0)["in_capex_cycle"] is False
    assert _assess(**BUILDING_OUT, revenue_cagr_pct=None)["in_capex_cycle"] is False


def test_maintenance_level_capex_is_not_a_capex_cycle():
    """资本开支只够维持现有资产，就不存在"被在建产能吃掉现金"这回事。"""
    result = _assess(ocf=[30e8] * 5, capex=[21e8] * 5, daa=[20e8] * 5)

    assert result["in_capex_cycle"] is False
    assert result["capex_to_daa"] == pytest.approx(1.05)


def test_missing_depreciation_data_does_not_assume_a_capex_cycle():
    result = scanner._capex_cycle_assessment(
        cashflow_slice=_cashflow([30e8] * 5, [60e8] * 5), fina_slice=None, revenue_cagr_pct=8.0
    )

    assert result["capex_to_daa"] is None
    assert result["in_capex_cycle"] is False


# --- 闸门行为 ---

def _gate(fcf_positive_years, capex_cycle):
    return scanner._quality_assessment(
        is_financial=False, avg_roe=15.0, avg_roic=14.0, wacc_pct=8.0, years_available=5,
        ocf_to_np=1.2, fcf_positive_years=fcf_positive_years, debt_to_assets=45.0,
        value_growth_pct=6.0, thresholds=dict(scanner.DEFAULT_QUALITY_THRESHOLDS),
        capex_cycle=capex_cycle,
    )


def test_capex_cycle_turns_the_fcff_rejection_into_a_note():
    blocked = _gate(0, _assess(**BLEEDING))
    rescued = _gate(0, _assess(**BUILDING_OUT))

    assert blocked["passes"] is False
    assert any("FCFF为正的年份" in reason for reason in blocked["reasons"])

    assert rescued["passes"] is True
    assert rescued["reasons"] == []
    assert any("判定为扩产期" in note for note in rescued["notes"])


def test_the_capex_cycle_channel_only_forgives_the_fcff_gate():
    """其余闸门一条都不放过——这是"并一条通道"和"放宽闸门"的区别。"""
    capex_cycle = _assess(**BUILDING_OUT)
    thresholds = dict(scanner.DEFAULT_QUALITY_THRESHOLDS)

    def _gate_with(**overrides):
        params = dict(
            is_financial=False, avg_roe=15.0, avg_roic=14.0, wacc_pct=8.0, years_available=5,
            ocf_to_np=1.2, fcf_positive_years=0, debt_to_assets=45.0, value_growth_pct=6.0,
            thresholds=thresholds, capex_cycle=capex_cycle,
        )
        params.update(overrides)
        return scanner._quality_assessment(**params)

    assert _gate_with(avg_roic=5.0)["passes"] is False          # ROIC 跑不赢 WACC
    assert _gate_with(ocf_to_np=0.2)["passes"] is False         # 盈利质量
    assert _gate_with(debt_to_assets=90.0)["passes"] is False   # 资产负债率
    assert _gate_with(value_growth_pct=-8.0)["passes"] is False # 内在价值倒退


def test_a_passing_company_is_unaffected_by_the_new_channel():
    assert _gate(5, None)["passes"] is True
    assert _gate(5, _assess(**BUILDING_OUT))["notes"] == []

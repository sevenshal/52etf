"""反推市场隐含假设：把 DCF 倒过来解，得到"市场在假设什么"。

正向的 return% 只回答"按我们的假设值多少"；这些用例锁的是另一半——从当前市值反解出
市场隐含的增速和贴现率，让模型的假设和市场的假设能被直接对照、可以被行业常识证伪。
"""

import pytest

from src.core.services import value_investing_scanner as scanner

TERMINAL_GROWTH = 0.03


def _snapshot(roic=0.18, near_term_growth=0.10, base_nopat=1.0e9, net_debt=2.0e9,
              parent_profit_share=1.0, financial_roe=None):
    return {
        "base_nopat": base_nopat,
        "roic": roic,
        "net_debt": net_debt,
        "book_minority": 0.0,
        "parent_profit_share": parent_profit_share,
        "applied_terminal_growth": TERMINAL_GROWTH,
        "near_term_growth": near_term_growth,
        "financial_roe": financial_roe,
    }


def _forward_equity(roic, growth, wacc, base_nopat=1.0e9, net_debt=2.0e9):
    valuation = scanner._two_stage_reinvestment_value(base_nopat, roic, growth, TERMINAL_GROWTH, wacc)
    return valuation["enterprise_value"] - net_debt


def _implied(market_cap, snapshot, wacc=0.09):
    return scanner._market_implied_assumptions(
        is_financial=False, market_cap=market_cap, pb=None, snapshot=snapshot,
        wacc=wacc, cost_of_equity=None, terminal_growth=TERMINAL_GROWTH,
    )


def test_reverse_solving_the_models_own_value_returns_the_models_own_inputs():
    """最强的正确性判据：市值恰好等于模型算出的价值时，隐含假设必须等于模型的假设。"""
    snapshot = _snapshot(roic=0.18, near_term_growth=0.10)
    fair_value = _forward_equity(0.18, 0.10, 0.09)

    implied = _implied(fair_value, snapshot, wacc=0.09)

    assert implied["implied_near_term_growth"] == pytest.approx(0.10, abs=1e-4)
    assert implied["implied_roic"] == pytest.approx(0.18, abs=1e-3)
    assert implied["implied_discount_rate"] == pytest.approx(0.09, abs=1e-4)
    assert implied["implied_growth_status"] == "solved"
    assert implied["implied_roic_status"] == "solved"
    assert implied["implied_discount_rate_status"] == "solved"


def test_a_richer_price_implies_faster_growth_and_a_lower_required_return():
    snapshot = _snapshot(roic=0.18, near_term_growth=0.10)
    fair_value = _forward_equity(0.18, 0.10, 0.09)

    rich = _implied(fair_value * 1.1, snapshot, wacc=0.09)
    cheap = _implied(fair_value * 0.6, snapshot, wacc=0.09)

    assert rich["implied_near_term_growth"] > 0.10 > cheap["implied_near_term_growth"]
    assert rich["implied_roic"] > 0.18 > cheap["implied_roic"]
    # 贴现率就是"按这套现金流假设、以当前价买入的年化回报"：买贵了回报低
    assert rich["implied_discount_rate"] < 0.09 < cheap["implied_discount_rate"]


def test_growth_saturates_because_reinvestment_is_capped_by_roic():
    """本模型里 g <= ROIC×0.9，所以隐含增速只在公允价值附近的一个窄带里有解。

    ROIC 18% 的公司顶格长到 16.2%，价值也只比基准高一成出头；再贵一点，"市场在假设
    更快的增长"就不再是一个可行的解释，该问的是"市场在假设更高的资本回报率"。
    这条性质是隐含 ROIC 存在的理由，锁住它，免得以后有人把上界从 1.00 调大就以为
    修好了——瓶颈不在求解区间，在模型的再投资约束。
    """
    snapshot = _snapshot(roic=0.18, near_term_growth=0.10)
    fair_value = _forward_equity(0.18, 0.10, 0.09)
    ceiling_value = _forward_equity(0.18, 0.18 * 0.9, 0.09)

    assert ceiling_value / fair_value < 1.2  # 顶格增速也只多出不到两成

    # 1.2 倍：增速已经解释不了，但更高的资本回报率还能
    too_rich = _implied(fair_value * 1.2, snapshot, wacc=0.09)
    assert too_rich["implied_near_term_growth"] is None
    assert too_rich["implied_growth_status"] == "above_range"
    assert too_rich["implied_roic"] > 0.18
    assert too_rich["implied_discount_rate"] < 0.09


def test_only_the_discount_rate_still_solves_for_a_genuinely_expensive_stock():
    """增速和 ROIC 都有天花板(约公允价值的 1.1 / 1.3 倍)，贴现率没有。

    所以真正贵的股票——也就是最想问"市场在假设什么"的那一批——只有隐含贴现率给得出
    数字。这不是求解器不行，是模型在说"没有任何可行的经营假设能解释这个价格"，
    那句话本身就是答案；而隐含贴现率把它翻译成一句能直接判断的话：
    按这套现金流假设，以当前价买入的年化回报率只有百分之几。
    """
    snapshot = _snapshot(roic=0.18, near_term_growth=0.10)
    fair_value = _forward_equity(0.18, 0.10, 0.09)

    expensive = _implied(fair_value * 2.0, snapshot, wacc=0.09)

    assert expensive["implied_near_term_growth"] is None
    assert expensive["implied_roic"] is None
    assert expensive["implied_discount_rate"] == pytest.approx(0.0617, abs=0.005)
    assert expensive["implied_discount_rate"] < 0.09


def test_an_unjustifiable_price_is_reported_as_out_of_range_not_clipped():
    """解不出来时给状态，不给一个夹到端点的假数。"""
    snapshot = _snapshot(roic=0.18, near_term_growth=0.10)
    absurd = _implied(_forward_equity(0.18, 0.10, 0.09) * 500, snapshot, wacc=0.09)

    assert absurd["implied_near_term_growth"] is None
    assert absurd["implied_growth_status"] == "above_range"
    assert absurd["implied_roic"] is None
    assert absurd["implied_roic_status"] == "above_range"


def test_missing_inputs_return_empty_rather_than_guessing():
    assert _implied(None, _snapshot())["implied_near_term_growth"] is None
    assert _implied(1e10, _snapshot(base_nopat=None))["implied_discount_rate"] is None

    # 隐含贴现率不需要 WACC(它解的就是贴现率本身)，但隐含增速/ROIC 需要
    no_wacc = scanner._market_implied_assumptions(
        is_financial=False, market_cap=1e10, pb=None, snapshot=_snapshot(),
        wacc=None, cost_of_equity=None, terminal_growth=TERMINAL_GROWTH,
    )
    assert no_wacc["implied_near_term_growth"] is None
    assert no_wacc["implied_roic"] is None
    assert no_wacc["implied_discount_rate"] is not None


# --- 金融股：同一个公式的两种解法 ---

def test_financial_implied_cost_of_equity_round_trips_against_justified_pb():
    roe, cost_of_equity = 0.13, 0.10
    fair_pb = scanner._residual_income_pb(roe, cost_of_equity, TERMINAL_GROWTH)

    implied = scanner._market_implied_assumptions(
        is_financial=True, market_cap=None, pb=fair_pb,
        snapshot=_snapshot(financial_roe=roe), wacc=None,
        cost_of_equity=cost_of_equity, terminal_growth=TERMINAL_GROWTH,
    )

    assert implied["implied_cost_of_equity"] == pytest.approx(cost_of_equity, abs=1e-4)


def test_a_bank_below_book_implies_a_cost_of_equity_above_its_reported_roe():
    """0.6 倍市净率 + 财报 ROE 13%：市场要么不信这个 ROE，要么要更高的风险补偿。"""
    implied = scanner._market_implied_assumptions(
        is_financial=True, market_cap=None, pb=0.6,
        snapshot=_snapshot(financial_roe=0.13), wacc=None,
        cost_of_equity=0.10, terminal_growth=TERMINAL_GROWTH,
    )

    assert implied["implied_cost_of_equity"] > 0.13


def test_solver_reports_a_flat_function_instead_of_dividing_by_zero():
    assert scanner._solve_monotone(lambda x: 5.0, 0.0, 1.0, 5.0)["status"] == "flat"
    assert scanner._solve_monotone(lambda x: None, 0.0, 1.0, 5.0)["status"] == "unsolvable"


# --- 端到端：反推字段要真的从扫描器一路流到接口返回值 ---

def test_implied_fields_reach_the_stock_profile_payload(tmp_path, monkeypatch):
    import duckdb
    from test_value_investing_ttm import AS_OF, _build_db

    path = tmp_path / "implied.duckdb"
    _build_db(path)
    monkeypatch.setattr(scanner, "connect_analytics_db",
                        lambda: duckdb.connect(str(path), read_only=True))

    candidate = scanner.evaluate_value_investing_stock("600584.SH", as_of=AS_OF)["candidate"]

    # 隐含贴现率是唯一在任何价位都解得出来的那一项，必须始终有值
    assert candidate["implied_discount_rate_pct"] is not None
    assert candidate["implied_discount_rate_status"] == "solved"
    # 和模型 WACC 的差值就是分歧：正数=市场比模型悲观，负数=市场比模型乐观
    assert candidate["implied_discount_rate_gap_pct"] == pytest.approx(
        candidate["implied_discount_rate_pct"] - candidate["wacc_pct"], abs=0.02
    )
    # 增速/ROIC 两项要么有值要么带状态，不能既没值又没状态
    for value_key, status_key in (
        ("implied_near_term_growth_pct", "implied_growth_status"),
        ("implied_roic_pct", "implied_roic_status"),
    ):
        assert candidate[value_key] is not None or candidate[status_key] is not None

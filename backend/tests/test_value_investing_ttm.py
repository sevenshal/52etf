"""TTM 口径：A股定期报告是年初至今累计，滚动12个月只能靠"最新累计 + 去年年报 − 去年同期"。

数字取自长电科技(600584.SH)三份真实定期报告，因为这只股票恰好把纯年报口径的滞后
暴露得最清楚：2025年报归母净利 15.65 亿，而截至 2026-06-30 的 TTM 是 19.39 亿，
差 24%；2026 上半年归母净利同比 +79% 的拐点，纯年报口径要到 2027 年 4 月才看得见。
"""

from datetime import date

import duckdb
import pandas as pd
import pytest

from src.core.services import value_investing_scanner as scanner

# 长电科技真实披露值（元）
FY2025_REV, FY2025_NP = 38_871_348_331.23, 1_565_238_036.45
H1_2025_REV, H1_2025_NP = 18_605_224_056.33, 470_785_267.86
H1_2026_REV, H1_2026_NP = 19_527_223_323.13, 844_644_275.04
TTM_REV = FY2025_REV - H1_2025_REV + H1_2026_REV      # 397.93 亿
TTM_NP = FY2025_NP - H1_2025_NP + H1_2026_NP          # 19.39 亿


def _income_frame(rows):
    """rows: [(end_date, ann_date, revenue, n_income_attr_p)]"""
    return pd.DataFrame(
        [
            {"ts_code": "600584.SH", "end_date": e, "ann_date": a, "revenue": r, "n_income_attr_p": p}
            for e, a, r, p in rows
        ]
    ).sort_values("end_date").reset_index(drop=True)


ANNUAL = _income_frame([
    (date(2024, 12, 31), date(2025, 4, 10), 35_961_679_888.59, 1_609_575_410.92),
    (date(2025, 12, 31), date(2026, 4, 10), FY2025_REV, FY2025_NP),
])
PERIODS = _income_frame([
    (date(2024, 12, 31), date(2025, 4, 10), 35_961_679_888.59, 1_609_575_410.92),
    (date(2025, 6, 30), date(2025, 8, 21), H1_2025_REV, H1_2025_NP),
    (date(2025, 12, 31), date(2026, 4, 10), FY2025_REV, FY2025_NP),
    (date(2026, 6, 30), date(2026, 8, 21), H1_2026_REV, H1_2026_NP),
])


def test_ttm_rolls_the_cumulative_statements_not_just_doubles_the_half_year():
    overlay = scanner._ttm_overlay(ANNUAL, PERIODS, "a_stock_income")
    row = overlay["frame"].iloc[-1]

    assert overlay["applied"] is True
    assert overlay["end_date"] == date(2026, 6, 30)
    assert row["revenue"] == pytest.approx(TTM_REV)
    assert row["n_income_attr_p"] == pytest.approx(TTM_NP)
    # 半年报直接×2 会得到 16.89 亿，和真实 TTM 差着 2.5 亿——这正是要避免的错法
    assert row["n_income_attr_p"] != pytest.approx(H1_2026_NP * 2)
    # 最新一期取代了年报，但前面的年报历史原样保留
    assert len(overlay["frame"]) == len(ANNUAL)
    assert overlay["frame"].iloc[0]["end_date"] == date(2024, 12, 31)


def test_ttm_beats_the_annual_only_view_by_the_amount_the_filings_say():
    annual_only = ANNUAL.iloc[-1]["n_income_attr_p"]
    ttm = scanner._ttm_overlay(ANNUAL, PERIODS, "a_stock_income")["frame"].iloc[-1]["n_income_attr_p"]

    assert annual_only == pytest.approx(1_565_238_036.45)
    assert ttm == pytest.approx(1_939_097_043.63)
    assert ttm / annual_only - 1 == pytest.approx(0.2389, abs=1e-3)


def test_latest_report_being_the_annual_one_needs_no_rolling():
    periods = PERIODS[PERIODS["end_date"] <= date(2025, 12, 31)]
    overlay = scanner._ttm_overlay(ANNUAL, periods, "a_stock_income")

    assert overlay["applied"] is False
    assert overlay["frame"] is ANNUAL


def test_missing_prior_year_same_period_falls_back_to_the_annual_view():
    # 只有今年半年报、没有去年同期：拼不出完整12个月，宁可退回年报也不能拿半年当一年
    periods = PERIODS[PERIODS["end_date"] != date(2025, 6, 30)]
    overlay = scanner._ttm_overlay(ANNUAL, periods, "a_stock_income")

    assert overlay["applied"] is False
    assert "去年同期" in overlay["reason"]
    assert overlay["frame"] is ANNUAL


def test_balance_sheet_takes_the_latest_point_in_time_not_a_difference():
    annual = pd.DataFrame([
        {"ts_code": "600584.SH", "end_date": date(2025, 12, 31), "ann_date": date(2026, 4, 10),
         "total_assets": 55_516_503_601.44, "total_liab": 24_228_765_583.91, "comp_type": "1"},
    ])
    periods = pd.concat([annual, pd.DataFrame([
        {"ts_code": "600584.SH", "end_date": date(2026, 6, 30), "ann_date": date(2026, 8, 21),
         "total_assets": 58_642_830_358.19, "total_liab": 26_103_000_000.00, "comp_type": "1"},
    ])], ignore_index=True)

    row = scanner._ttm_overlay(annual, periods, "a_stock_balancesheet")["frame"].iloc[-1]

    # 时点数直接取最新，绝不能做 58.6 + 55.5 − ... 这种差值
    assert row["total_assets"] == pytest.approx(58_642_830_358.19)
    assert row["comp_type"] == "1"


def _fina_row(end, ann, roe, roic, dta, ebit, capital):
    return {"ts_code": "600584.SH", "end_date": end, "ann_date": ann, "roe": roe, "roic": roic,
            "debt_to_assets": dta, "ebit": ebit, "invest_capital": capital}


def test_ratio_columns_are_blanked_rather_than_carried_over_from_the_annual_row():
    annual = pd.DataFrame([_fina_row(date(2025, 12, 31), date(2026, 4, 10),
                                     5.5, 4.2, 43.64, 2_000_000_000.0, 40_000_000_000.0)])
    periods = pd.DataFrame([
        _fina_row(date(2025, 6, 30), date(2025, 8, 21), 1.69, 1.3, 43.10, 900_000_000.0, 39_000_000_000.0),
        _fina_row(date(2025, 12, 31), date(2026, 4, 10), 5.5, 4.2, 43.64, 2_000_000_000.0, 40_000_000_000.0),
        _fina_row(date(2026, 6, 30), date(2026, 8, 21), 2.92, 2.1, 44.50, 1_100_000_000.0, 42_000_000_000.0),
    ])

    row = scanner._ttm_overlay(annual, periods, "a_stock_fina_indicator")["frame"].iloc[-1]

    # 比率既不能加也不能减：半年的 ROE 2.92% 不是年化值，年报的 5.5% 也不是最新值，
    # 两个都不能放进 TTM 行，只能置空让下游按分量重算
    # 置空后是 float 列里的 NaN（safe_float 对非有限值返回 None，下游会跳过）
    assert pd.isna(row["roe"])
    assert pd.isna(row["roic"])
    assert pd.isna(row["debt_to_assets"])
    assert scanner.safe_float(row["roe"]) is None
    # 流量滚动，时点数取最新
    assert row["ebit"] == pytest.approx(1_100_000_000.0 + 2_000_000_000.0 - 900_000_000.0)
    assert row["invest_capital"] == pytest.approx(42_000_000_000.0)


def test_flow_columns_that_cannot_be_rolled_block_the_whole_overlay():
    """流量滚不出来时不能只换时点数：年报的 EBIT 配最新的投入资本会算出一个假 ROIC。"""
    annual = pd.DataFrame([_fina_row(date(2025, 12, 31), date(2026, 4, 10),
                                     5.5, 4.2, 43.64, 2_000_000_000.0, 40_000_000_000.0)])
    periods = pd.DataFrame([
        _fina_row(date(2025, 12, 31), date(2026, 4, 10), 5.5, 4.2, 43.64, 2_000_000_000.0, 40_000_000_000.0),
        _fina_row(date(2026, 6, 30), date(2026, 8, 21), 2.92, 2.1, 44.50, 1_100_000_000.0, 42_000_000_000.0),
    ])

    overlay = scanner._ttm_overlay(annual, periods, "a_stock_fina_indicator")

    assert overlay["applied"] is False
    assert overlay["frame"].iloc[-1]["invest_capital"] == pytest.approx(40_000_000_000.0)


# --------------------------------------------------------------------------
# 端到端：扫描器整条链路是否真的用上了 TTM，以及闸门是否仍然只看年报
# --------------------------------------------------------------------------
AS_OF = date(2026, 9, 10)
ANNUAL_YEARS = [2021, 2022, 2023, 2024, 2025]


def _periods(profit_jump: bool):
    """年报 5 期 + 去年同期/今年半年报。今年半年报的利润可选择性地跳升。"""
    rows = []
    for index, year in enumerate(ANNUAL_YEARS):
        scale = 1.05 ** index
        rows.append(dict(end=date(year, 12, 31), ann=date(year + 1, 4, 20), frac=1.0, scale=scale))
    rows.append(dict(end=date(2025, 6, 30), ann=date(2025, 8, 21), frac=0.5, scale=1.05 ** 4))
    rows.append(dict(end=date(2026, 6, 30), ann=date(2026, 8, 21),
                     frac=0.9 if profit_jump else 0.5, scale=1.05 ** 4))
    return rows


def _build_db(path, profit_jump=True):
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE a_stock_basic (ts_code VARCHAR, name VARCHAR, industry VARCHAR,"
                " list_date DATE, list_status VARCHAR)")
    con.execute("INSERT INTO a_stock_basic VALUES ('600584.SH','测试封测','半导体',DATE '2003-06-03','L')")

    con.execute("CREATE TABLE a_stock_market_daily (ts_code VARCHAR, trade_date DATE, close DOUBLE,"
                " total_mv DOUBLE, circ_mv DOUBLE, pe DOUBLE, pe_ttm DOUBLE, pb DOUBLE, dv_ttm DOUBLE,"
                " pct_chg DOUBLE)")
    con.executemany("INSERT INTO a_stock_market_daily VALUES (?,?,?,?,?,?,?,?,?,?)", [
        ("600584.SH", AS_OF - pd.Timedelta(days=offset), 20.0, 400000.0, 300000.0,
         12.0, 11.0 + (offset % 7) * 0.1, 1.4 + (offset % 5) * 0.02, 2.0, ((offset % 5) - 2) * 0.9)
        for offset in range(600)
    ])
    con.execute("CREATE TABLE a_stock_index_daily (ts_code VARCHAR, trade_date DATE, pct_chg DOUBLE)")
    con.executemany("INSERT INTO a_stock_index_daily VALUES (?,?,?)", [
        (scanner.MARKET_INDEX_CODE, AS_OF - pd.Timedelta(days=offset), ((offset % 5) - 2) * 1.0)
        for offset in range(600)
    ])

    con.execute("CREATE TABLE a_stock_income (ts_code VARCHAR, end_date DATE, ann_date DATE,"
                " revenue DOUBLE, n_income_attr_p DOUBLE, n_income DOUBLE, minority_gain DOUBLE,"
                " total_profit DOUBLE, income_tax DOUBLE, fin_exp_int_exp DOUBLE)")
    con.execute("CREATE TABLE a_stock_cashflow (ts_code VARCHAR, end_date DATE, ann_date DATE,"
                " net_profit DOUBLE, n_cashflow_act DOUBLE, c_pay_acq_const_fiolta DOUBLE)")
    con.execute("CREATE TABLE a_stock_fina_indicator (ts_code VARCHAR, end_date DATE, ann_date DATE,"
                " roe DOUBLE, roe_waa DOUBLE, roic DOUBLE, ebit DOUBLE, invest_capital DOUBLE,"
                " daa DOUBLE, tax_to_ebt DOUBLE, debt_to_assets DOUBLE, interestdebt DOUBLE,"
                " fcff DOUBLE, netdebt DOUBLE)")
    con.execute("CREATE TABLE a_stock_balancesheet (ts_code VARCHAR, end_date DATE, ann_date DATE,"
                " comp_type VARCHAR, total_assets DOUBLE, total_liab DOUBLE, money_cap DOUBLE,"
                " minority_int DOUBLE, total_hldr_eqy_exc_min_int DOUBLE, total_hldr_eqy_inc_min_int DOUBLE,"
                " st_borr DOUBLE, non_cur_liab_due_1y DOUBLE, lt_borr DOUBLE, bond_payable DOUBLE,"
                " st_bonds_payable DOUBLE, lease_liab DOUBLE)")

    for row in _periods(profit_jump):
        e, a, f, s = row["end"], row["ann"], row["frac"], row["scale"]
        con.execute("INSERT INTO a_stock_income VALUES (?,?,?,?,?,?,?,?,?,?)",
                    ["600584.SH", e, a, 2.0e9*s*f, 3.6e8*s*f, 4.0e8*s*f, 4.0e7*s*f,
                     4.8e8*s*f, 0.8e8*s*f, 2.0e7*f])
        con.execute("INSERT INTO a_stock_cashflow VALUES (?,?,?,?,?,?)",
                    ["600584.SH", e, a, 4.0e8*s*f, 5.2e8*s*f, 1.2e8*f])
        con.execute("INSERT INTO a_stock_fina_indicator VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ["600584.SH", e, a, 14.0*f, 14.0*f, 18.0*f, 4.8e8*s*f, 3.0e9, 1.5e8*f,
                     10.0, 40.0, 8.0e8, 3.0e8*s*f, 2.0e8])
        con.execute("INSERT INTO a_stock_balancesheet VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ["600584.SH", e, a, "1", 5.0e9, 2.0e9, 6.0e8, 3.0e8, 2.7e9, 3.0e9,
                     5.0e8, 0.0, 3.0e8, 0.0, 0.0, 0.0])
    con.close()


@pytest.fixture
def ttm_db(tmp_path, monkeypatch):
    path = tmp_path / "ttm.duckdb"
    _build_db(path)
    monkeypatch.setattr(scanner, "connect_analytics_db",
                        lambda: duckdb.connect(str(path), read_only=True))
    return path


def _candidate(**kwargs):
    result = scanner.evaluate_value_investing_stock("600584.SH", as_of=AS_OF, **kwargs)
    return result["candidate"]


def test_scanner_reports_the_ttm_basis_and_period(ttm_db):
    candidate = _candidate()

    assert candidate["valuation_basis"] == "ttm"
    assert candidate["valuation_period_end"] == "2026-06-30"


def test_ttm_picks_up_the_interim_jump_that_the_annual_view_cannot_see(ttm_db):
    ttm = _candidate()
    annual = scanner.screen_value_investing_candidates(
        as_of=AS_OF, symbols=["600584.SH"], force_valuation=True, top_n=1, exclude_st=False,
        use_ttm=False,
    )
    annual_candidate = next(iter(annual["candidates"] + annual["excluded_sample"]))

    assert annual_candidate["valuation_basis"] == "annual"
    assert annual_candidate["valuation_period_end"] is None
    # 上半年利润跳升(半年就做到往年全年的九成)，TTM 看得见、纯年报口径看不见
    assert ttm["dcf_base_nopat_yi"] > annual_candidate["dcf_base_nopat_yi"]
    assert ttm["expected_return_pct"] > annual_candidate["expected_return_pct"]


def test_quality_gates_stay_on_the_annual_series(ttm_db):
    ttm = _candidate()
    annual = scanner.screen_value_investing_candidates(
        as_of=AS_OF, symbols=["600584.SH"], force_valuation=True, top_n=1, exclude_st=False,
        use_ttm=False,
    )
    annual_candidate = next(iter(annual["candidates"] + annual["excluded_sample"]))

    # 近5年平均ROIC/ROE、FCFF为正年数、内在价值同比都是多年期判断，不能被滚动窗口改写
    for field in ("avg_roic_pct", "avg_roe_pct", "fcf_positive_years", "value_growth_pct"):
        assert ttm[field] == annual_candidate[field], field

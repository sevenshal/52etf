"""三张表核心数据读取：口径、对齐、去重。

数字取自长电科技(600584.SH)2025年报与2026半年报，这样断言的不是"某个数被原样搬运"，
而是"搬运出来的确实是报表上那个数"。
"""

from datetime import date

import duckdb
import pytest

from src.core.services import a_stock_financials as financials

FY2025 = dict(end=date(2025, 12, 31), ann=date(2026, 4, 10),
              revenue=38_871_348_331.23, cost=33_372_064_524.59,
              np_parent=1_565_238_036.45, ocf=4_652_212_168.20, capex=6_298_299_227.33,
              assets=55_516_503_601.44, liab=24_228_765_583.91, equity=28_671_164_117.07)
H1_2026 = dict(end=date(2026, 6, 30), ann=date(2026, 8, 21),
               revenue=19_527_223_323.13, cost=16_800_000_000.00,
               np_parent=844_644_275.04, ocf=2_964_426_853.22, capex=4_700_145_248.84,
               assets=58_642_830_358.19, liab=26_103_000_000.00, equity=28_944_631_725.69)


def _build(path, rows=(FY2025, H1_2026), duplicate_restatement=False):
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE a_stock_income (ts_code VARCHAR, end_date DATE, ann_date DATE,"
                " total_revenue DOUBLE, revenue DOUBLE, oper_cost DOUBLE, n_income_attr_p DOUBLE,"
                " rd_exp DOUBLE)")
    con.execute("CREATE TABLE a_stock_balancesheet (ts_code VARCHAR, end_date DATE, ann_date DATE,"
                " total_assets DOUBLE, total_liab DOUBLE, total_hldr_eqy_exc_min_int DOUBLE)")
    con.execute("CREATE TABLE a_stock_cashflow (ts_code VARCHAR, end_date DATE, ann_date DATE,"
                " n_cashflow_act DOUBLE, c_pay_acq_const_fiolta DOUBLE)")
    con.execute("CREATE TABLE a_stock_fina_indicator (ts_code VARCHAR, end_date DATE, ann_date DATE,"
                " grossprofit_margin DOUBLE, netprofit_margin DOUBLE, roe DOUBLE, roe_waa DOUBLE,"
                " roe_dt DOUBLE, debt_to_assets DOUBLE)")
    for row in rows:
        con.execute("INSERT INTO a_stock_income VALUES ('600584.SH',?,?,?,?,?,?,?)",
                    [row["end"], row["ann"], row["revenue"], row["revenue"], row["cost"],
                     row["np_parent"], 2_085_525_811.35])
        con.execute("INSERT INTO a_stock_balancesheet VALUES ('600584.SH',?,?,?,?,?)",
                    [row["end"], row["ann"], row["assets"], row["liab"], row["equity"]])
        con.execute("INSERT INTO a_stock_cashflow VALUES ('600584.SH',?,?,?,?)",
                    [row["end"], row["ann"], row["ocf"], row["capex"]])
    # 财务指标只给年报，用来验证"某一张表缺这一期"时该期不会整个消失
    con.execute("INSERT INTO a_stock_fina_indicator VALUES ('600584.SH',?,?,?,?,?,?,?,?)",
                [FY2025["end"], FY2025["ann"], 14.15, 4.03, 5.46, 5.50, 5.30, 43.64])
    if duplicate_restatement:
        # 同一报告期的更正稿：公告更晚、数字不同，只应保留最新那条
        con.execute("INSERT INTO a_stock_income VALUES ('600584.SH',?,?,?,?,?,?,?)",
                    [FY2025["end"], date(2026, 6, 15), 1.0, 1.0, 1.0, 1.0, 1.0])
    con.close()


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "fin.duckdb"
    _build(path)
    monkeypatch.setattr(financials, "connect_analytics_db",
                        lambda: duckdb.connect(str(path), read_only=True))
    return path


def test_periods_come_back_newest_first_with_readable_labels(db):
    result = financials.load_a_stock_financials("600584.sh")

    assert result["ts_code"] == "600584.SH"
    assert [p["period_label"] for p in result["periods"]] == ["2026中报", "2025年报"]
    assert [p["is_annual"] for p in result["periods"]] == [False, True]
    assert result["periods"][0]["end_date"] == "2026-06-30"
    assert result["periods"][0]["ann_date"] == "2026-08-21"


def test_the_three_statements_carry_the_filed_numbers(db):
    latest = financials.load_a_stock_financials("600584.SH")["periods"][0]

    assert latest["income"]["revenue"] == pytest.approx(19_527_223_323.13)
    assert latest["income"]["n_income_attr_p"] == pytest.approx(844_644_275.04)
    assert latest["balancesheet"]["total_assets"] == pytest.approx(58_642_830_358.19)
    assert latest["cashflow"]["n_cashflow_act"] == pytest.approx(2_964_426_853.22)
    assert latest["cashflow"]["c_pay_acq_const_fiolta"] == pytest.approx(4_700_145_248.84)


def test_gross_profit_and_margins_fall_back_to_the_income_statement(db):
    """财务指标表缺这一期时，毛利率/净利率按利润表自己算——最常看的两个数不该空着。"""
    interim, annual = financials.load_a_stock_financials("600584.SH")["periods"]

    assert interim["indicator"]["grossprofit_margin"] == pytest.approx(
        (19_527_223_323.13 - 16_800_000_000.00) / 19_527_223_323.13 * 100, abs=0.01
    )
    assert interim["indicator"]["netprofit_margin"] == pytest.approx(
        844_644_275.04 / 19_527_223_323.13 * 100, abs=0.01
    )
    # 年报那一期有财务指标表，就用报表自己的值，不去覆盖它
    assert annual["indicator"]["grossprofit_margin"] == pytest.approx(14.15)
    assert annual["income"]["gross_profit"] == pytest.approx(38_871_348_331.23 - 33_372_064_524.59)


def test_three_flavours_of_roe_are_kept_apart(db):
    """摊薄、加权、扣非摊薄是三个不同的数，必须分开列，不能混成一个"ROE"。"""
    annual = financials.load_a_stock_financials("600584.SH")["periods"][1]

    assert annual["indicator"]["roe"] == pytest.approx(5.46)
    assert annual["indicator"]["roe_waa"] == pytest.approx(5.50)
    assert annual["indicator"]["roe_dt"] == pytest.approx(5.30)


def test_a_period_missing_from_one_table_still_returns_the_other_tables(db):
    """财务指标只有年报，中报那一期照样要出来，缺的格子给 None。"""
    interim = financials.load_a_stock_financials("600584.SH")["periods"][0]

    assert interim["indicator"]["roe"] is None
    assert interim["indicator"]["debt_to_assets"] is None
    assert interim["income"]["revenue"] is not None


def test_annual_only_filters_out_the_interim_periods(db):
    result = financials.load_a_stock_financials("600584.SH", annual_only=True)

    assert [p["period_label"] for p in result["periods"]] == ["2025年报"]


def test_a_restated_period_keeps_only_the_latest_announcement(tmp_path, monkeypatch):
    path = tmp_path / "restated.duckdb"
    _build(path, duplicate_restatement=True)
    monkeypatch.setattr(financials, "connect_analytics_db",
                        lambda: duckdb.connect(str(path), read_only=True))

    result = financials.load_a_stock_financials("600584.SH")
    annual = [p for p in result["periods"] if p["period_label"] == "2025年报"]

    assert len(annual) == 1  # 同一报告期不能出现两列
    assert annual[0]["income"]["revenue"] == pytest.approx(1.0)  # 更正稿(公告更晚)胜出


def test_unknown_symbol_and_blank_input_are_handled(db):
    assert financials.load_a_stock_financials("000001.SZ")["periods"] == []
    assert financials.load_a_stock_financials("")["periods"] == []

"""价值投资扫描的 as_of 必须是 point-in-time：历史回放不能看到当时还没披露的财报、
还没发生的行情，也不能因为股票后来退市就把它从历史里抹掉。"""

from datetime import date, timedelta

import duckdb
import pytest

from src.core.services import value_investing_scanner as scanner

YEARS = [2020, 2021, 2022, 2023, 2024, 2025]
BEFORE_ANNUAL_REPORT = date(2026, 3, 31)   # 2025 年报还没披露
AFTER_ANNUAL_REPORT = date(2026, 5, 15)    # 2025 年报 4 月 20 日已披露
DELIST_DATE = date(2026, 5, 1)


@pytest.fixture
def analytics_db(tmp_path, monkeypatch):
    path = tmp_path / "analytics.duckdb"
    connection = duckdb.connect(str(path))
    connection.execute(
        "CREATE TABLE a_stock_basic (ts_code VARCHAR, name VARCHAR, industry VARCHAR,"
        " list_date DATE, list_status VARCHAR, delist_date DATE)"
    )
    connection.executemany(
        "INSERT INTO a_stock_basic VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("600001.SH", "在市公司", "机械设备", date(2010, 1, 1), "L", None),
            ("600002.SH", "后来退市公司", "机械设备", date(2010, 1, 1), "D", DELIST_DATE),
        ],
    )

    connection.execute(
        "CREATE TABLE a_stock_market_daily (ts_code VARCHAR, trade_date DATE, close DOUBLE,"
        " total_mv DOUBLE, circ_mv DOUBLE, pe DOUBLE, pe_ttm DOUBLE, pb DOUBLE, dv_ttm DOUBLE, pct_chg DOUBLE)"
    )
    last_day = date(2026, 6, 30)
    market_rows = []
    for offset in range(800):
        trade_date = last_day - timedelta(days=offset)
        # as_of 之后价格翻倍：用来确认历史回放取的是当时的价格
        close = 20.0 if trade_date <= BEFORE_ANNUAL_REPORT else 40.0
        for ts_code in ("600001.SH", "600002.SH"):
            if ts_code == "600002.SH" and trade_date >= DELIST_DATE:
                continue
            market_rows.append(
                (ts_code, trade_date, close, 400000.0 * close / 20.0, 300000.0, 12.0, 11.0, 1.4, 2.0,
                 ((offset % 5) - 2) * 0.9)
            )
    connection.executemany("INSERT INTO a_stock_market_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", market_rows)

    connection.execute("CREATE TABLE a_stock_index_daily (ts_code VARCHAR, trade_date DATE, pct_chg DOUBLE)")
    connection.executemany(
        "INSERT INTO a_stock_index_daily VALUES (?, ?, ?)",
        [(scanner.MARKET_INDEX_CODE, last_day - timedelta(days=offset), ((offset % 5) - 2) * 1.0) for offset in range(800)],
    )

    connection.execute(
        "CREATE TABLE a_stock_income (ts_code VARCHAR, end_date DATE, ann_date DATE, revenue DOUBLE,"
        " n_income_attr_p DOUBLE, n_income DOUBLE, minority_gain DOUBLE, total_profit DOUBLE,"
        " income_tax DOUBLE, fin_exp_int_exp DOUBLE)"
    )
    connection.execute(
        "CREATE TABLE a_stock_balancesheet (ts_code VARCHAR, end_date DATE, ann_date DATE,"
        " comp_type VARCHAR, total_assets DOUBLE, total_liab DOUBLE, money_cap DOUBLE,"
        " minority_int DOUBLE, total_hldr_eqy_exc_min_int DOUBLE, total_hldr_eqy_inc_min_int DOUBLE,"
        " st_borr DOUBLE, non_cur_liab_due_1y DOUBLE, lt_borr DOUBLE, bond_payable DOUBLE,"
        " st_bonds_payable DOUBLE, lease_liab DOUBLE)"
    )
    connection.execute(
        "CREATE TABLE a_stock_cashflow (ts_code VARCHAR, end_date DATE, ann_date DATE,"
        " net_profit DOUBLE, n_cashflow_act DOUBLE, c_pay_acq_const_fiolta DOUBLE)"
    )
    connection.execute(
        "CREATE TABLE a_stock_fina_indicator (ts_code VARCHAR, end_date DATE, ann_date DATE,"
        " roe DOUBLE, roe_waa DOUBLE, roic DOUBLE, ebit DOUBLE, invest_capital DOUBLE, daa DOUBLE,"
        " tax_to_ebt DOUBLE, debt_to_assets DOUBLE, interestdebt DOUBLE, fcff DOUBLE, netdebt DOUBLE)"
    )
    for ts_code in ("600001.SH", "600002.SH"):
        for index, year in enumerate(YEARS):
            end_date, ann_date = date(year, 12, 31), date(year + 1, 4, 20)
            growth = 1.05 ** index
            connection.execute(
                "INSERT INTO a_stock_income VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [ts_code, end_date, ann_date, 2.0e9 * growth, 3.6e8 * growth, 4.0e8 * growth,
                 4.0e7 * growth, 4.8e8 * growth, 0.8e8 * growth, 2.0e7],
            )
            connection.execute(
                "INSERT INTO a_stock_balancesheet VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [ts_code, end_date, ann_date, "1", 5.0e9, 2.0e9, 6.0e8, 3.0e8, 2.7e9, 3.0e9,
                 5.0e8, 0.0, 3.0e8, 0.0, 0.0, 0.0],
            )
            connection.execute(
                "INSERT INTO a_stock_cashflow VALUES (?, ?, ?, ?, ?, ?)",
                [ts_code, end_date, ann_date, 4.0e8 * growth, 5.2e8 * growth, 1.2e8],
            )
            # 2025 年报的资产负债率和前几年明显不同，用来判断读到的是哪一期
            debt_to_assets = 60.0 if year == 2025 else 40.0
            connection.execute(
                "INSERT INTO a_stock_fina_indicator VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [ts_code, end_date, ann_date, 14.0, 14.0, 18.0, 4.8e8 * growth, 3.0e9, 1.5e8, 10.0,
                 debt_to_assets, 8.0e8, 3.0e8 * growth, 2.0e8],
            )
    connection.close()
    monkeypatch.setattr(scanner, "connect_analytics_db", lambda: duckdb.connect(str(path), read_only=True))
    return path


def _candidate(result, ts_code):
    return next(
        (item for item in [*result["candidates"], *result["excluded_sample"]] if item["ts_code"] == ts_code),
        None,
    )


def test_reports_disclosed_after_as_of_are_invisible(analytics_db):
    before = scanner.screen_value_investing_candidates(as_of=BEFORE_ANNUAL_REPORT, force_valuation=True, top_n=10)
    after = scanner.screen_value_investing_candidates(as_of=AFTER_ANNUAL_REPORT, force_valuation=True, top_n=10)

    assert _candidate(before, "600001.SH")["debt_to_assets_pct"] == 40.0
    assert _candidate(after, "600001.SH")["debt_to_assets_pct"] == 60.0


def test_market_data_after_as_of_is_invisible(analytics_db):
    before = scanner.screen_value_investing_candidates(as_of=BEFORE_ANNUAL_REPORT, force_valuation=True, top_n=10)
    assert _candidate(before, "600001.SH")["close"] == 20.0


def test_stock_delisted_later_is_still_in_the_historical_scan(analytics_db):
    before = scanner.screen_value_investing_candidates(as_of=BEFORE_ANNUAL_REPORT, force_valuation=True, top_n=10)
    after = scanner.screen_value_investing_candidates(as_of=AFTER_ANNUAL_REPORT, force_valuation=True, top_n=10)

    assert _candidate(before, "600002.SH") is not None
    assert _candidate(after, "600002.SH") is None


def test_latest_twelve_month_metrics_are_reported(analytics_db):
    candidate = _candidate(
        scanner.screen_value_investing_candidates(as_of=AFTER_ANNUAL_REPORT, force_valuation=True, top_n=10),
        "600001.SH",
    )
    # 只有年报时"最近12个月"就是最新年报：EBIT×(1−税率)/投入资本
    tax_rate = candidate["effective_tax_rate_pct"] / 100.0
    expected_roic = 4.8e8 * 1.05 ** 5 * (1 - tax_rate) / 3.0e9 * 100
    assert candidate["latest_roic_pct"] == pytest.approx(expected_roic, abs=0.01)
    assert candidate["latest_roic_source"] == "ttm_nopat_over_invested_capital"
    assert candidate["latest_roe_pct"] == pytest.approx(3.6e8 * 1.05 ** 5 / 2.7e9 * 100, abs=0.01)
    assert candidate["ocf_to_net_profit_ttm"] == pytest.approx(1.3, abs=0.01)

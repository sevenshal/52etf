"""个股价值投资画像：必须和全市场扫描算出完全一样的数字。

详情页上的 ROIC/WACC、DCF 内在价值和潜在回报率，与「价值投资扫描」页上同一只股票的
那一行是同一套口径。这里用一个最小的 DuckDB 分析库把两条路径都跑一遍做逐字段比对，
防止以后有人为了详情页另写一份"差不多"的算法。
"""

from datetime import date, timedelta

import duckdb
import pytest

from src.core.services import value_investing_scanner as scanner

AS_OF = date(2026, 3, 31)
YEARS = [2021, 2022, 2023, 2024, 2025]


def _income_rows(ts_code, scale):
    return [
        (
            ts_code,
            date(year, 12, 31),
            date(year + 1, 4, 20),
            2.0e9 * scale * (1.05 ** index),
            3.6e8 * scale * (1.05 ** index),
            4.0e8 * scale * (1.05 ** index),
            4.0e7 * scale * (1.05 ** index),
            4.8e8 * scale * (1.05 ** index),
            0.8e8 * scale * (1.05 ** index),
            2.0e7 * scale,
        )
        for index, year in enumerate(YEARS)
    ]


def _balancesheet_rows(ts_code, scale):
    return [
        (
            ts_code,
            date(year, 12, 31),
            date(year + 1, 4, 20),
            "1",
            5.0e9 * scale,
            2.0e9 * scale,
            6.0e8 * scale,
            3.0e8 * scale,
            2.7e9 * scale,
            3.0e9 * scale,
            5.0e8 * scale,
            0.0,
            3.0e8 * scale,
            0.0,
            0.0,
            0.0,
        )
        for year in YEARS
    ]


def _cashflow_rows(ts_code, scale, ocf_multiple):
    return [
        (
            ts_code,
            date(year, 12, 31),
            date(year + 1, 4, 20),
            4.0e8 * scale * (1.05 ** index),
            4.0e8 * scale * ocf_multiple * (1.05 ** index),
            1.2e8 * scale,
        )
        for index, year in enumerate(YEARS)
    ]


def _fina_rows(ts_code, scale, roic_pct):
    return [
        (
            ts_code,
            date(year, 12, 31),
            date(year + 1, 4, 20),
            14.0,
            14.0,
            roic_pct,
            4.8e8 * scale * (1.05 ** index),
            3.0e9 * scale,
            1.5e8 * scale,
            10.0,
            40.0,
            8.0e8 * scale,
            3.0e8 * scale * (1.05 ** index),
            2.0e8 * scale,
        )
        for index, year in enumerate(YEARS)
    ]


@pytest.fixture
def analytics_db(tmp_path, monkeypatch):
    """两只一般工商业股票：一只轻松过闸门，一只经营现金流太差过不了。"""
    path = tmp_path / "analytics.duckdb"
    connection = duckdb.connect(str(path))

    connection.execute(
        "CREATE TABLE a_stock_basic (ts_code VARCHAR, name VARCHAR, industry VARCHAR,"
        " list_date DATE, list_status VARCHAR)"
    )
    connection.executemany(
        "INSERT INTO a_stock_basic VALUES (?, ?, ?, ?, ?)",
        [
            ("600001.SH", "优质公司", "机械设备", date(2010, 1, 1), "L"),
            ("600002.SH", "现金流很差公司", "机械设备", date(2010, 1, 1), "L"),
        ],
    )

    connection.execute(
        "CREATE TABLE a_stock_market_daily (ts_code VARCHAR, trade_date DATE, close DOUBLE,"
        " total_mv DOUBLE, circ_mv DOUBLE, pe DOUBLE, pe_ttm DOUBLE, pb DOUBLE, dv_ttm DOUBLE,"
        " pct_chg DOUBLE)"
    )
    market_rows = []
    for offset in range(600):
        trade_date = AS_OF - timedelta(days=offset)
        for index, ts_code in enumerate(("600001.SH", "600002.SH")):
            market_rows.append(
                (
                    ts_code,
                    trade_date,
                    20.0,
                    400000.0,  # 万元 → 40 亿市值
                    300000.0,
                    12.0 + index,
                    11.0 + index + (offset % 7) * 0.1,
                    1.4 + index * 0.1 + (offset % 5) * 0.02,
                    2.0,
                    ((offset % 5) - 2) * 0.9,
                )
            )
    connection.executemany(
        "INSERT INTO a_stock_market_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", market_rows
    )

    connection.execute("CREATE TABLE a_stock_index_daily (ts_code VARCHAR, trade_date DATE, pct_chg DOUBLE)")
    connection.executemany(
        "INSERT INTO a_stock_index_daily VALUES (?, ?, ?)",
        [
            (scanner.MARKET_INDEX_CODE, AS_OF - timedelta(days=offset), ((offset % 5) - 2) * 1.0)
            for offset in range(600)
        ],
    )

    connection.execute(
        "CREATE TABLE a_stock_income (ts_code VARCHAR, end_date DATE, ann_date DATE, revenue DOUBLE,"
        " n_income_attr_p DOUBLE, n_income DOUBLE, minority_gain DOUBLE, total_profit DOUBLE,"
        " income_tax DOUBLE, fin_exp_int_exp DOUBLE)"
    )
    connection.executemany(
        "INSERT INTO a_stock_income VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        _income_rows("600001.SH", 1.0) + _income_rows("600002.SH", 1.0),
    )

    connection.execute(
        "CREATE TABLE a_stock_balancesheet (ts_code VARCHAR, end_date DATE, ann_date DATE,"
        " comp_type VARCHAR, total_assets DOUBLE, total_liab DOUBLE, money_cap DOUBLE,"
        " minority_int DOUBLE, total_hldr_eqy_exc_min_int DOUBLE, total_hldr_eqy_inc_min_int DOUBLE,"
        " st_borr DOUBLE, non_cur_liab_due_1y DOUBLE, lt_borr DOUBLE, bond_payable DOUBLE,"
        " st_bonds_payable DOUBLE, lease_liab DOUBLE)"
    )
    connection.executemany(
        "INSERT INTO a_stock_balancesheet VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        _balancesheet_rows("600001.SH", 1.0) + _balancesheet_rows("600002.SH", 1.0),
    )

    connection.execute(
        "CREATE TABLE a_stock_cashflow (ts_code VARCHAR, end_date DATE, ann_date DATE,"
        " net_profit DOUBLE, n_cashflow_act DOUBLE, c_pay_acq_const_fiolta DOUBLE)"
    )
    connection.executemany(
        "INSERT INTO a_stock_cashflow VALUES (?, ?, ?, ?, ?, ?)",
        _cashflow_rows("600001.SH", 1.0, 1.3) + _cashflow_rows("600002.SH", 1.0, 0.1),
    )

    connection.execute(
        "CREATE TABLE a_stock_fina_indicator (ts_code VARCHAR, end_date DATE, ann_date DATE,"
        " roe DOUBLE, roe_waa DOUBLE, roic DOUBLE, ebit DOUBLE, invest_capital DOUBLE, daa DOUBLE,"
        " tax_to_ebt DOUBLE, debt_to_assets DOUBLE, interestdebt DOUBLE, fcff DOUBLE, netdebt DOUBLE)"
    )
    connection.executemany(
        "INSERT INTO a_stock_fina_indicator VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        _fina_rows("600001.SH", 1.0, 18.0) + _fina_rows("600002.SH", 1.0, 18.0),
    )
    connection.close()

    monkeypatch.setattr(scanner, "connect_analytics_db", lambda: duckdb.connect(str(path), read_only=True))
    return path


def test_single_stock_profile_matches_the_full_market_scan(analytics_db):
    full = scanner.screen_value_investing_candidates(as_of=AS_OF, top_n=100)
    scanned = next(item for item in full["candidates"] if item["ts_code"] == "600001.SH")

    profile = scanner.evaluate_value_investing_stock("600001.SH", as_of=AS_OF)

    assert profile["status"] == "completed"
    assert profile["candidate"] == scanned
    assert profile["assumptions"] == full["assumptions"]
    assert profile["thresholds"] == full["thresholds"]


def test_gate_failure_still_reports_the_full_valuation(analytics_db):
    full = scanner.screen_value_investing_candidates(as_of=AS_OF, top_n=100)
    excluded = next(item for item in full["excluded_sample"] if item["ts_code"] == "600002.SH")
    # 全市场扫描对没过闸门的股票只给一行摘要，估值根本没算
    assert "expected_return_pct" in excluded and excluded["expected_return_pct"] is None
    assert "dcf_equity_value_yi" not in excluded

    profile = scanner.evaluate_value_investing_stock("600002.SH", as_of=AS_OF)
    candidate = profile["candidate"]

    assert candidate["quality_passed"] is False
    assert candidate["quality_reasons"]  # 未通过原因照样要带出来
    assert candidate["expected_return_pct"] is not None
    assert candidate["dcf_equity_value_yi"] is not None


def test_lowercase_symbol_is_normalised(analytics_db):
    assert scanner.evaluate_value_investing_stock("600001.sz")["status"] == "not_found"
    profile = scanner.evaluate_value_investing_stock("600001.sh", as_of=AS_OF)
    assert profile["ts_code"] == "600001.SH"
    assert profile["candidate"]["ts_code"] == "600001.SH"


def test_unknown_symbol_reports_not_found(analytics_db):
    profile = scanner.evaluate_value_investing_stock("000001.SZ", as_of=AS_OF)
    assert profile["status"] == "not_found"
    assert profile["candidate"] is None

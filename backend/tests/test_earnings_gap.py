from datetime import date, datetime, timedelta

import duckdb
import pandas as pd
import pytest

from src.core.services import earnings_gap as eg


def _calendar(start: date, end: date):
    days = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def test_pick_latest_reports_uses_first_announcement_row():
    frame = pd.DataFrame([
        {"ts_code": "000001.SZ", "end_date": date(2026, 3, 31), "ann_date": date(2026, 4, 20), "netprofit_yoy": 10.0},
        {"ts_code": "000001.SZ", "end_date": date(2026, 6, 30), "ann_date": date(2026, 9, 1), "netprofit_yoy": 99.0},
        {"ts_code": "000001.SZ", "end_date": date(2026, 6, 30), "ann_date": date(2026, 8, 20), "netprofit_yoy": 50.0},
    ])
    latest = eg.pick_latest_reports(frame)
    assert len(latest) == 1
    row = latest.iloc[0]
    assert row["ann_date"] == date(2026, 8, 20)
    assert row["netprofit_yoy"] == 50.0


def test_next_trade_date_and_listed_days():
    calendar = _calendar(date(2026, 1, 1), date(2026, 12, 31))
    # 周五公告 → 下周一
    assert eg.next_trade_date(calendar, date(2026, 8, 21)) == date(2026, 8, 24)
    # 周六公告 → 周一
    assert eg.next_trade_date(calendar, date(2026, 8, 22)) == date(2026, 8, 24)
    assert eg.listed_trade_days(calendar, date(2026, 8, 20), date(2026, 8, 24)) == 3
    assert eg.listed_trade_days(calendar, date(2020, 1, 1), date(2026, 8, 24)) > 120


def test_evaluate_t1_bar():
    base = {"open": 10.3, "close": 10.6, "pre_close": 10.0, "amount_yuan": 5e7}
    assert eg.evaluate_t1_bar(base, 11.0)["passed"]
    # 高开不足 2%
    assert not eg.evaluate_t1_bar({**base, "open": 10.15}, 11.0)["passed"]
    # 收阴
    assert not eg.evaluate_t1_bar({**base, "close": 10.2}, 11.0)["passed"]
    # 封板
    assert not eg.evaluate_t1_bar({**base, "close": 11.0}, 11.0)["passed"]
    # 成交额不足
    assert not eg.evaluate_t1_bar({**base, "amount_yuan": 2e7}, 11.0)["passed"]


def test_ttm_from_cumulative_and_roe():
    values = {date(2026, 3, 31): 30.0, date(2025, 12, 31): 100.0, date(2025, 3, 31): 20.0}
    assert eg.ttm_from_cumulative(values, date(2026, 3, 31)) == 110.0
    assert eg.ttm_from_cumulative(values, date(2025, 12, 31)) == 100.0
    # 缺上年同期
    assert eg.ttm_from_cumulative({date(2026, 3, 31): 30.0, date(2025, 12, 31): 100.0}, date(2026, 3, 31)) is None
    assert eg.dedt_roe_ttm(values, {date(2026, 3, 31): 1100.0}) == 10.0
    assert eg.dedt_roe_ttm(values, {date(2026, 3, 31): -5.0}) is None


def test_fallback_up_limit():
    assert eg.fallback_up_limit("600000.SH", 10.0) == 11.0
    assert eg.fallback_up_limit("300750.SZ", 10.0) == 12.0
    assert eg.fallback_up_limit("830000.BJ", 10.0) == 13.0


class FakeTushare:
    def __init__(self, limits=None):
        self.limits = limits or {}

    def get_a_stock_stk_limit_frame(self, trade_date):
        rows = self.limits.get(trade_date, {})
        return pd.DataFrame([{"ts_code": code, "up_limit": value} for code, value in rows.items()])


def _build_db(bars, reports, basic, adj=None, calendar=(), equity=None):
    connection = duckdb.connect(":memory:")
    connection.execute(
        "CREATE TABLE a_stock_market_daily (ts_code VARCHAR, trade_date DATE, open DOUBLE, high DOUBLE,"
        " close DOUBLE, pre_close DOUBLE, amount DOUBLE, pe_ttm DOUBLE, pb DOUBLE, ps_ttm DOUBLE)"
    )
    connection.execute(
        "CREATE TABLE a_stock_balancesheet (ts_code VARCHAR, end_date DATE, ann_date DATE, report_type VARCHAR,"
        " total_hldr_eqy_exc_min_int DOUBLE)"
    )
    connection.execute("CREATE TABLE a_stock_adj_factor (ts_code VARCHAR, trade_date DATE, adj_factor DOUBLE)")
    connection.execute(
        "CREATE TABLE a_stock_fina_indicator (ts_code VARCHAR, end_date DATE, ann_date DATE, netprofit_yoy DOUBLE,"
        " profit_dedt DOUBLE)"
    )
    connection.execute(
        "CREATE TABLE a_stock_basic (ts_code VARCHAR, name VARCHAR, industry VARCHAR, list_date DATE, list_status VARCHAR)"
    )
    # 交易日历取自分析库日K，用一只不在股票列表里的占位股票铺满已同步的交易日
    for trade_date in calendar:
        connection.execute(
            "INSERT INTO a_stock_market_daily (ts_code, trade_date, open, high, close, pre_close, amount)"
            " VALUES ('999999.SZ', ?, 1, 1, 1, 1, 1)",
            [trade_date],
        )
    for row in bars:
        row = tuple(row) + (None,) * (10 - len(row))
        connection.execute("INSERT INTO a_stock_market_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row)
    for row in adj or []:
        connection.execute("INSERT INTO a_stock_adj_factor VALUES (?, ?, ?)", row)
    for row in reports:
        row = tuple(row) + (None,) * (5 - len(row))
        connection.execute("INSERT INTO a_stock_fina_indicator VALUES (?, ?, ?, ?, ?)", row)
    for row in equity or []:
        connection.execute("INSERT INTO a_stock_balancesheet VALUES (?, ?, ?, '1', ?)", row)
    for row in basic:
        connection.execute("INSERT INTO a_stock_basic VALUES (?, ?, ?, ?, 'L')", row)
    return connection


@pytest.fixture(autouse=True)
def _clear_limit_cache():
    eg._stk_limit_cache.clear()
    yield
    eg._stk_limit_cache.clear()


def test_compute_earnings_gap_end_to_end():
    calendar = _calendar(date(2024, 1, 1), date(2026, 9, 18))
    t1 = date(2026, 8, 24)  # 公告日 8/21(周五) 的 T+1
    last = date(2026, 9, 18)
    old = date(2018, 1, 2)
    basic = [
        ("000001.SZ", "命中", "银行", old),
        ("000002.SZ", "增速不足", "地产", old),
        ("000003.SZ", "封板", "电子", old),
        ("000004.SZ", "次新", "电子", date(2026, 6, 1)),
        ("000005.SZ", "增速过高", "电子", old),
    ]
    reports = [(code, date(2026, 6, 30), date(2026, 8, 21), yoy, 60.0) for code, yoy in (
        ("000001.SZ", 80.0),
        ("000002.SZ", 20.0),
        ("000003.SZ", 80.0),
        ("000004.SZ", 80.0),
        ("000005.SZ", 5000.0),
    )]
    # 旧一期财报不影响"最近财报"
    reports.append(("000001.SZ", date(2026, 3, 31), date(2026, 4, 20), 1.0))
    # 扣非 TTM = 60(26H1) + 100(25年报) − 40(25H1) = 120；归母净资产 1000 → ROE 12%
    reports.append(("000001.SZ", date(2025, 12, 31), date(2026, 3, 20), 10.0, 100.0))
    reports.append(("000001.SZ", date(2025, 6, 30), date(2025, 8, 20), 10.0, 40.0))
    equity = [("000001.SZ", date(2026, 6, 30), date(2026, 8, 21), 1000.0)]
    bars = []
    for code in ("000001.SZ", "000002.SZ", "000004.SZ", "000005.SZ"):
        bars.append((code, t1, 10.3, 10.8, 10.6, 10.0, 80000.0))
    bars.append(("000003.SZ", t1, 10.3, 11.0, 11.0, 10.0, 80000.0))
    bars.append(("000001.SZ", last, 12.0, 12.2, 12.0, 11.9, 90000.0, 25.5, 2.0, 3.1))
    adj = [("000001.SZ", t1, 1.0), ("000001.SZ", last, 1.1)]
    connection = _build_db(bars, reports, basic, adj, calendar, equity)
    service = FakeTushare(limits={t1: {"000001.SZ": 11.0, "000003.SZ": 11.0}})

    payload = eg.compute_earnings_gap(now=datetime(2026, 9, 19, 10, 0), service=service, connection=connection)

    assert [item["symbol"] for item in payload["items"]] == ["000001.SZ"]
    item = payload["items"][0]
    assert item["ann_date"] == "2026-08-21"
    assert item["signal_date"] == "2026-08-24"
    assert item["netprofit_yoy"] == 80.0
    assert item["t1_open_gap_pct"] == 3.0
    assert item["t1_pct_chg"] == 6.0
    # 前复权：12.0 * 1.1 / (10.6 * 1.0) - 1
    assert item["since_pct"] == pytest.approx((12.0 * 1.1 / 10.6 - 1) * 100, abs=0.01)
    assert item["latest_date"] == "2026-09-18"
    assert (item["pe_ttm"], item["pb"], item["ps_ttm"]) == (25.5, 2.0, 3.1)
    assert item["roe_dedt_ttm"] == 12.0
    assert item["roe_pb"] == 6.0
    assert payload["stats"]["growth_passed"] == 3
    assert payload["trade_date"] == "2026-09-18"
    assert payload["warnings"] == []


def test_announcement_on_latest_synced_day_is_pending():
    # 分析库最新交易日 9/18 公告的财报，T+1 还没有日K，不出信号也不报错
    calendar = _calendar(date(2024, 1, 1), date(2026, 9, 18))
    connection = _build_db(
        [],
        [("600001.SH", date(2026, 6, 30), date(2026, 9, 18), 45.0)],
        [("600001.SH", "待确认", "电子", date(2018, 1, 2))],
        calendar=calendar,
    )
    payload = eg.compute_earnings_gap(now=datetime(2026, 9, 18, 18, 25), service=FakeTushare(), connection=connection)
    assert payload["items"] == []
    assert payload["stats"]["pending"] == 1


def test_missing_stk_limit_falls_back_to_board_rule():
    calendar = _calendar(date(2024, 1, 1), date(2026, 9, 18))
    t1 = date(2026, 8, 24)
    connection = _build_db(
        [("300001.SZ", t1, 10.3, 11.5, 11.5, 10.0, 80000.0)],
        [("300001.SZ", date(2026, 6, 30), date(2026, 8, 21), 60.0)],
        [("300001.SZ", "创业板", "电子", date(2018, 1, 2))],
        calendar=calendar,
    )
    payload = eg.compute_earnings_gap(now=datetime(2026, 9, 18, 18, 25), service=FakeTushare(), connection=connection)
    # 创业板涨停 12.0，收 11.5 未封板
    assert [item["symbol"] for item in payload["items"]] == ["300001.SZ"]
    assert payload["items"][0]["t1_up_limit"] == 12.0
    assert any("stk_limit" in warning for warning in payload["warnings"])


def test_get_earnings_gap_reads_snapshot_until_refresh(monkeypatch):
    calls = []

    def fake_compute(now=None):
        calls.append(now)
        return {
            "computed_at": f"2026-09-21 15:01:0{len(calls)}",
            "trade_date": "2026-09-21",
            "items": [{"symbol": "600001.SH", "signal_date": "2026-09-21"}],
            "warnings": [],
        }

    monkeypatch.setattr(eg, "compute_earnings_gap", fake_compute)
    with eg.get_db_ctx() as db:
        db.query(eg.MarketSignalSnapshot).delete()

    first = eg.get_earnings_gap()
    assert first["computed_at"] == "2026-09-21 15:01:01"
    assert eg.get_earnings_gap()["computed_at"] == "2026-09-21 15:01:01"
    assert len(calls) == 1
    assert eg.get_earnings_gap(refresh=True)["computed_at"] == "2026-09-21 15:01:02"
    assert eg.load_earnings_gap_snapshot()["computed_at"] == "2026-09-21 15:01:02"

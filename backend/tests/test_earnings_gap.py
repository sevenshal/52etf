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


def test_next_trade_date_and_listed_days():
    calendar = _calendar(date(2026, 1, 1), date(2026, 12, 31))
    # 周五公告 → 下周一
    assert eg.next_trade_date(calendar, date(2026, 8, 21)) == date(2026, 8, 24)
    # 周六公告 → 周一
    assert eg.next_trade_date(calendar, date(2026, 8, 22)) == date(2026, 8, 24)
    assert eg.listed_trade_days(calendar, date(2026, 8, 20), date(2026, 8, 24)) == 3
    assert eg.listed_trade_days(calendar, date(2020, 1, 1), date(2026, 8, 24)) > 120


def test_evaluate_t1_bar():
    config = eg.normalize_config(None)
    base = {"open": 10.3, "close": 10.6, "low": 10.25, "pre_close": 10.0, "amount_yuan": 5e7}
    assert eg.evaluate_t1_bar(base, 11.0, config)["passed"]
    # 高开不足 2%
    assert not eg.evaluate_t1_bar({**base, "open": 10.15}, 11.0, config)["passed"]
    # 收阴
    assert not eg.evaluate_t1_bar({**base, "close": 10.2}, 11.0, config)["passed"]
    # 封板
    assert not eg.evaluate_t1_bar({**base, "close": 11.0}, 11.0, config)["passed"]
    # 成交额不足
    assert not eg.evaluate_t1_bar({**base, "amount_yuan": 2e7}, 11.0, config)["passed"]
    # 收阴/封板的要求可以关掉
    relaxed = eg.normalize_config({"require_bullish_close": False, "require_unsealed": False})
    assert eg.evaluate_t1_bar({**base, "close": 11.0}, 11.0, relaxed)["passed"]


def test_evaluate_t1_bar_true_gap_and_amount_ratio():
    base = {"open": 10.3, "close": 10.6, "low": 10.25, "pre_close": 10.0, "amount_yuan": 5e7}
    need_gap = eg.normalize_config({"require_true_gap": True})
    # T 日最高 10.2 < T+1 最低 10.25 → 真缺口
    assert eg.evaluate_t1_bar(base, 11.0, need_gap, prev_high=10.2)["true_gap"] is True
    assert eg.evaluate_t1_bar(base, 11.0, need_gap, prev_high=10.2)["passed"]
    # T 日最高 10.4 > T+1 最低 → 当天就把缺口补掉了
    filled = eg.evaluate_t1_bar(base, 11.0, need_gap, prev_high=10.4)
    assert filled["true_gap"] is False and not filled["passed"]
    # 不要求真缺口时照样出信号
    assert eg.evaluate_t1_bar(base, 11.0, eg.normalize_config(None), prev_high=10.4)["passed"]

    ratio_config = eg.normalize_config({"min_amount_ratio": 2.0})
    assert not eg.evaluate_t1_bar(base, 11.0, ratio_config, amount_ratio=1.5)["passed"]
    assert eg.evaluate_t1_bar(base, 11.0, ratio_config, amount_ratio=2.5)["passed"]


def test_gap_fill_status():
    from datetime import date as _date
    lows = [(_date(2026, 9, 1), 10.5), (_date(2026, 9, 2), 10.1)]
    assert eg.gap_fill_status(10.2, True, lows) == {
        "has_true_gap": True, "gap_filled": True, "gap_filled_date": _date(2026, 9, 2),
    }
    assert eg.gap_fill_status(10.0, True, lows)["gap_filled"] is False
    # 本来就没有缺口
    assert eg.gap_fill_status(10.4, False, lows) == {
        "has_true_gap": False, "gap_filled": None, "gap_filled_date": None,
    }


def test_pick_latest_events_prefers_report_on_same_day():
    frame = pd.DataFrame([
        {"ts_code": "000001.SZ", "source": "forecast", "end_date": date(2026, 6, 30),
         "ann_date": date(2026, 8, 21), "np_yoy": 40.0},
        {"ts_code": "000001.SZ", "source": "report", "end_date": date(2026, 6, 30),
         "ann_date": date(2026, 8, 21), "np_yoy": 55.0},
        {"ts_code": "000001.SZ", "source": "express", "end_date": date(2026, 6, 30),
         "ann_date": date(2026, 7, 20), "np_yoy": 50.0},
    ])
    latest = eg.pick_latest_events(frame)
    assert len(latest) == 1
    assert latest.iloc[0]["source"] == "report" and latest.iloc[0]["np_yoy"] == 55.0


def test_express_np_yoy():
    # 净利 120 / 去年同期 80 − 1 = 50%
    assert eg.express_np_yoy(120.0, 80.0) == pytest.approx(50.0)
    # 去年同期亏损，退回接口给的扣非同比
    assert eg.express_np_yoy(120.0, -80.0, fallback=66.0) == 66.0
    assert eg.express_np_yoy(None, None) is None


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


def _build_db(bars, reports, basic, adj=None, calendar=(), equity=None, forecasts=None, express=None):
    connection = duckdb.connect(":memory:")
    connection.execute(
        "CREATE TABLE a_stock_market_daily (ts_code VARCHAR, trade_date DATE, open DOUBLE, high DOUBLE,"
        " low DOUBLE, close DOUBLE, pre_close DOUBLE, amount DOUBLE, pe_ttm DOUBLE, pb DOUBLE, ps_ttm DOUBLE)"
    )
    connection.execute(
        "CREATE TABLE a_stock_forecast (ts_code VARCHAR, end_date DATE, ann_date DATE, type VARCHAR,"
        " p_change_min DOUBLE, p_change_max DOUBLE)"
    )
    connection.execute(
        "CREATE TABLE a_stock_express (ts_code VARCHAR, end_date DATE, ann_date DATE, n_income DOUBLE,"
        " yoy_net_profit DOUBLE, yoy_dedu_np DOUBLE, yoy_sales DOUBLE)"
    )
    connection.execute(
        "CREATE TABLE a_stock_balancesheet (ts_code VARCHAR, end_date DATE, ann_date DATE, report_type VARCHAR,"
        " total_hldr_eqy_exc_min_int DOUBLE)"
    )
    connection.execute("CREATE TABLE a_stock_adj_factor (ts_code VARCHAR, trade_date DATE, adj_factor DOUBLE)")
    connection.execute(
        "CREATE TABLE a_stock_fina_indicator (ts_code VARCHAR, end_date DATE, ann_date DATE, netprofit_yoy DOUBLE,"
        " profit_dedt DOUBLE, or_yoy DOUBLE, q_netprofit_qoq DOUBLE, q_sales_qoq DOUBLE)"
    )
    connection.execute(
        "CREATE TABLE a_stock_basic (ts_code VARCHAR, name VARCHAR, industry VARCHAR, list_date DATE, list_status VARCHAR)"
    )
    # 交易日历取自分析库日K，用一只不在股票列表里的占位股票铺满已同步的交易日
    for trade_date in calendar:
        connection.execute(
            "INSERT INTO a_stock_market_daily (ts_code, trade_date, open, high, low, close, pre_close, amount)"
            " VALUES ('999999.SZ', ?, 1, 1, 1, 1, 1, 1)",
            [trade_date],
        )
    for row in bars:
        row = tuple(row) + (None,) * (11 - len(row))
        connection.execute("INSERT INTO a_stock_market_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row)
    for row in adj or []:
        connection.execute("INSERT INTO a_stock_adj_factor VALUES (?, ?, ?)", row)
    for row in reports:
        row = tuple(row) + (None,) * (8 - len(row))
        connection.execute("INSERT INTO a_stock_fina_indicator VALUES (?, ?, ?, ?, ?, ?, ?, ?)", row)
    for row in forecasts or []:
        connection.execute("INSERT INTO a_stock_forecast VALUES (?, ?, ?, ?, ?, ?)", row)
    for row in express or []:
        connection.execute("INSERT INTO a_stock_express VALUES (?, ?, ?, ?, ?, ?, ?)", row)
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
        bars.append((code, t1, 10.3, 10.8, 10.25, 10.6, 10.0, 80000.0))
    bars.append(("000003.SZ", t1, 10.3, 11.0, 10.3, 11.0, 10.0, 80000.0))
    bars.append(("000001.SZ", last, 12.0, 12.4, 11.8, 12.0, 11.9, 90000.0, 25.5, 2.0, 3.1))
    adj = [("000001.SZ", t1, 1.0), ("000001.SZ", last, 1.1)]
    connection = _build_db(bars, reports, basic, adj, calendar, equity)
    service = FakeTushare(limits={t1: {"000001.SZ": 11.0, "000003.SZ": 11.0}})

    payload = eg.compute_earnings_gap(now=datetime(2026, 9, 19, 10, 0), service=service, connection=connection)

    assert [item["symbol"] for item in payload["items"]] == ["000001.SZ"]
    item = payload["items"][0]
    assert item["ann_date"] == "2026-08-21"
    assert item["signal_date"] == "2026-08-24"
    assert item["np_yoy"] == 80.0
    assert item["source"] == "report" and item["source_label"] == "财报"
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
        [("300001.SZ", t1, 10.3, 11.5, 10.3, 11.5, 10.0, 80000.0)],
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
            "payload_version": eg.PAYLOAD_VERSION,
            "criteria": eg.load_config(),
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


def test_old_payload_version_snapshot_is_recomputed(monkeypatch):
    """加了新字段（如估值列）后，旧结构的快照必须自动重算，不能一直显示空列。"""
    calls = []

    def fake_compute(now=None):
        calls.append(now)
        return {"payload_version": eg.PAYLOAD_VERSION, "criteria": eg.load_config(),
                "computed_at": "2026-09-20 18:25:00", "trade_date": "2026-09-18", "items": [], "warnings": []}

    monkeypatch.setattr(eg, "compute_earnings_gap", fake_compute)
    eg.save_earnings_gap_snapshot({
        "computed_at": "2026-09-19 18:29:41", "trade_date": "2026-09-18",
        "items": [{"symbol": "603353.SH", "signal_date": "2026-08-31"}], "warnings": [],
    })
    assert eg.load_earnings_gap_snapshot().get("payload_version") is None

    payload = eg.get_earnings_gap()
    assert payload["computed_at"] == "2026-09-20 18:25:00"
    assert len(calls) == 1
    # 重算后的新快照直接复用，不再反复重算
    assert eg.get_earnings_gap()["computed_at"] == "2026-09-20 18:25:00"
    assert len(calls) == 1


def test_forecast_and_express_events_drive_signals():
    """预告用下限、快报自己算同比，两者都能出信号；公告日更新的事件覆盖更早的财报。"""
    calendar = _calendar(date(2024, 1, 1), date(2026, 9, 18))
    t1 = date(2026, 8, 24)
    old = date(2018, 1, 2)
    basic = [("600001.SH", "预告股", "电子", old), ("600002.SH", "快报股", "机械", old)]
    # 两只股票都有一份更早、增速不达标的财报，最近一次公告分别是预告和快报
    reports = [
        ("600001.SH", date(2026, 3, 31), date(2026, 4, 20), 5.0),
        ("600002.SH", date(2026, 3, 31), date(2026, 4, 20), 5.0),
    ]
    forecasts = [("600001.SH", date(2026, 6, 30), date(2026, 8, 21), "预增", 45.0, 80.0)]
    express = [("600002.SH", date(2026, 6, 30), date(2026, 8, 21), 150.0, 100.0, 48.0, 22.0)]
    bars = [
        ("600001.SH", t1, 10.3, 10.8, 10.25, 10.6, 10.0, 80000.0),
        ("600002.SH", t1, 10.3, 10.8, 10.25, 10.6, 10.0, 80000.0),
    ]
    connection = _build_db(bars, reports, basic, calendar=calendar, forecasts=forecasts, express=express)
    payload = eg.compute_earnings_gap(
        now=datetime(2026, 9, 18, 18, 25), service=FakeTushare(limits={t1: {}}), connection=connection,
        config={"min_amount_yuan": 0},
    )

    by_symbol = {item["symbol"]: item for item in payload["items"]}
    assert set(by_symbol) == {"600001.SH", "600002.SH"}
    forecast_item = by_symbol["600001.SH"]
    assert forecast_item["source"] == "forecast" and forecast_item["source_label"] == "预告"
    assert forecast_item["np_yoy"] == 45.0  # 下限
    assert forecast_item["np_yoy_max"] == 80.0
    assert forecast_item["forecast_type"] == "预增"
    express_item = by_symbol["600002.SH"]
    assert express_item["source"] == "express"
    assert express_item["np_yoy"] == 50.0  # 150 / 100 − 1
    assert express_item["or_yoy"] == 22.0

    # 只留财报事件源时两只都不出信号（财报增速只有 5%）
    only_report = eg.compute_earnings_gap(
        now=datetime(2026, 9, 18, 18, 25), service=FakeTushare(), connection=connection,
        config={"sources": ["report"], "min_amount_yuan": 0},
    )
    assert only_report["items"] == []


def test_signal_columns_amount_ratio_max_gain_and_gap_fill():
    calendar = _calendar(date(2024, 1, 1), date(2026, 9, 4))
    t0, t1 = date(2026, 8, 21), date(2026, 8, 24)
    basic = [("600001.SH", "有缺口", "电子", date(2018, 1, 2)), ("600002.SH", "缺口已补", "电子", date(2018, 1, 2))]
    reports = [
        ("600001.SH", date(2026, 6, 30), date(2026, 8, 21), 60.0, None, 12.0, 8.0, 5.0),
        ("600002.SH", date(2026, 6, 30), date(2026, 8, 21), 60.0),
    ]
    bars = []
    for code in ("600001.SH", "600002.SH"):
        # T 日最高 10.2，T+1 最低 10.25 → 真缺口
        bars.append((code, t0, 10.0, 10.2, 9.9, 10.0, 9.9, 20000.0))
        bars.append((code, t1, 10.3, 10.8, 10.25, 10.6, 10.0, 80000.0))
    # 600001 之后冲高不回补；600002 回落到 10.1 补掉缺口
    bars.append(("600001.SH", date(2026, 8, 25), 10.7, 12.0, 10.6, 11.9, 10.6, 50000.0))
    bars.append(("600001.SH", date(2026, 9, 4), 11.9, 12.1, 11.5, 11.7, 11.9, 50000.0, 30.0, 3.0, 4.0))
    bars.append(("600002.SH", date(2026, 8, 25), 10.5, 10.6, 10.1, 10.2, 10.6, 50000.0))
    bars.append(("600002.SH", date(2026, 9, 4), 10.2, 10.3, 10.0, 10.1, 10.2, 50000.0, 20.0, 2.0, 3.0))
    connection = _build_db(bars, reports, basic, calendar=calendar)
    payload = eg.compute_earnings_gap(
        now=datetime(2026, 9, 4, 18, 25), service=FakeTushare(limits={t1: {}}), connection=connection,
    )

    by_symbol = {item["symbol"]: item for item in payload["items"]}
    kept, filled = by_symbol["600001.SH"], by_symbol["600002.SH"]
    # T+1 成交额 8000万 ÷ 之前均额（T 日 2000万）= 4 倍
    assert kept["amount_ratio"] == 4.0
    assert (kept["prev_high"], kept["t1_open"], kept["t1_close"]) == (10.2, 10.3, 10.6)
    assert kept["latest_price"] == 11.7
    # 最大涨幅按 T+1 之后的最高价 12.1 / 10.6 − 1
    assert kept["max_gain_pct"] == pytest.approx((12.1 / 10.6 - 1) * 100, abs=0.01)
    assert kept["since_pct"] == pytest.approx((11.7 / 10.6 - 1) * 100, abs=0.01)
    assert kept["has_true_gap"] is True and kept["gap_filled"] is False
    assert kept["or_yoy"] == 12.0 and kept["np_qoq"] == 8.0 and kept["or_qoq"] == 5.0

    assert filled["has_true_gap"] is True and filled["gap_filled"] is True
    assert filled["gap_filled_date"] == "2026-08-25"

    # 要求必须留缺口且不能回补时，只能靠 require_true_gap 过滤当天就补掉的；已回补的仍在列表里，由"缺口回补"列标注
    need_gap = eg.compute_earnings_gap(
        now=datetime(2026, 9, 4, 18, 25), service=FakeTushare(limits={t1: {}}), connection=connection,
        config={"require_true_gap": True},
    )
    assert {item["symbol"] for item in need_gap["items"]} == {"600001.SH", "600002.SH"}


def test_config_save_and_load_round_trip():
    eg.save_config({"min_gap_pct": 3.5, "sources": ["report", "forecast"], "require_true_gap": True}, updated_by="tester")
    config = eg.load_config()
    assert config["min_gap_pct"] == 3.5
    assert config["sources"] == ["report", "forecast"]
    assert config["require_true_gap"] is True
    # 未提交的键回落到默认值
    assert config["min_amount_yuan"] == eg.DEFAULT_CONFIG["min_amount_yuan"]
    eg.save_config(eg.DEFAULT_CONFIG)
    assert eg.load_config() == eg.DEFAULT_CONFIG

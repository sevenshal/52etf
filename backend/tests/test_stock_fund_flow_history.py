from datetime import date, timedelta

from sqlalchemy import text

from src.app.api import stock as stock_api
from src.app.api.stock import get_a_stock_fund_flow_history
from src.core.analytics_database import AStockFundFlowDaily, AnalyticsSession, ensure_analytics_schema


TEST_SYMBOL = "900021.SH"


def _delete_test_rows():
    db = AnalyticsSession()
    try:
        db.execute(text("DELETE FROM a_stock_fund_flow_daily WHERE ts_code = :symbol"), {"symbol": TEST_SYMBOL})
        db.commit()
    finally:
        db.close()
        AnalyticsSession.remove()


def test_fund_flow_history_returns_rows_in_range_sorted(monkeypatch):
    monkeypatch.setattr(stock_api, "fetch_stock_fund_flow_daily", lambda *args, **kwargs: {"daily": []})
    ensure_analytics_schema()
    _delete_test_rows()
    db = AnalyticsSession()
    try:
        db.add_all(
            [
                AStockFundFlowDaily(
                    trade_date=trade_date, ts_code=TEST_SYMBOL, symbol="900021",
                    main_net=main_net, super_net=main_net / 2, large_net=main_net / 2,
                    source="tushare_moneyflow_dc",
                )
                for trade_date, main_net in (
                    (date(2026, 9, 16), -2_000_000.0),
                    (date(2026, 9, 14), 1_500_000.0),
                    (date(2020, 1, 2), 9.0),
                )
            ]
        )
        db.commit()
    finally:
        db.close()
        AnalyticsSession.remove()

    try:
        rows = get_a_stock_fund_flow_history(
            symbol=TEST_SYMBOL.lower(), start_date=date(2026, 1, 1), end_date=date(2026, 9, 18), _="account",
        )
        assert [row["trade_date"] for row in rows] == ["2026-09-14", "2026-09-16"]
        assert rows[0]["main_net"] == 1_500_000.0
        assert rows[1]["main_net"] == -2_000_000.0
        assert rows[1]["source"] == "tushare_moneyflow_dc"
        assert rows[1]["live"] is False
    finally:
        _delete_test_rows()


def _seed_rows(rows):
    ensure_analytics_schema()
    _delete_test_rows()
    db = AnalyticsSession()
    try:
        db.add_all(
            [
                AStockFundFlowDaily(
                    trade_date=trade_date, ts_code=TEST_SYMBOL, symbol="900021",
                    main_net=main_net, source="tushare_moneyflow_dc",
                )
                for trade_date, main_net in rows
            ]
        )
        db.commit()
    finally:
        db.close()
        AnalyticsSession.remove()


def test_fund_flow_history_appends_live_rows_after_latest_synced_date(monkeypatch):
    today = date.today()
    synced_day = today - timedelta(days=2)
    missing_day = today - timedelta(days=1)
    calls = []

    def fake_fetch(code, daily_limit):
        calls.append((code, daily_limit))
        return {
            "daily": [
                # 已同步的那天以库里为准，不能被东财覆盖或重复
                {"date": synced_day.isoformat(), "main_net": 999.0},
                {"date": missing_day.isoformat(), "main_net": 300.0, "super_net": 200.0, "large_net": 100.0},
                {"date": today.isoformat(), "main_net": -400.0, "main_net_pct": -1.5},
            ]
        }

    monkeypatch.setattr(stock_api, "fetch_stock_fund_flow_daily", fake_fetch)
    _seed_rows([(synced_day, 100.0)])
    try:
        rows = get_a_stock_fund_flow_history(symbol=TEST_SYMBOL, start_date=None, end_date=None, _="account")
        assert calls == [(TEST_SYMBOL, stock_api.LIVE_FUND_FLOW_DAYS)]
        assert [(row["trade_date"], row["main_net"], row["live"]) for row in rows] == [
            (synced_day.isoformat(), 100.0, False),
            (missing_day.isoformat(), 300.0, True),
            (today.isoformat(), -400.0, True),
        ]
        assert rows[-1]["main_net_pct"] == -1.5
        assert rows[-1]["source"] == "eastmoney_push2"
    finally:
        _delete_test_rows()


def test_fund_flow_history_keeps_synced_rows_when_live_fetch_fails(monkeypatch):
    def failing_fetch(*args, **kwargs):
        raise RuntimeError("eastmoney down")

    monkeypatch.setattr(stock_api, "fetch_stock_fund_flow_daily", failing_fetch)
    synced_day = date.today() - timedelta(days=1)
    _seed_rows([(synced_day, 100.0)])
    try:
        rows = get_a_stock_fund_flow_history(symbol=TEST_SYMBOL, start_date=None, end_date=None, _="account")
        assert [row["trade_date"] for row in rows] == [synced_day.isoformat()]
    finally:
        _delete_test_rows()


def test_fund_flow_history_skips_live_fetch_for_past_range(monkeypatch):
    def unexpected_fetch(*args, **kwargs):
        raise AssertionError("past range must not hit the live source")

    monkeypatch.setattr(stock_api, "fetch_stock_fund_flow_daily", unexpected_fetch)
    _seed_rows([(date(2026, 1, 5), 100.0)])
    try:
        rows = get_a_stock_fund_flow_history(
            symbol=TEST_SYMBOL, start_date=date(2026, 1, 1), end_date=date(2026, 1, 31), _="account",
        )
        assert len(rows) == 1
    finally:
        _delete_test_rows()

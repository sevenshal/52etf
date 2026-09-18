from datetime import date

from sqlalchemy import text

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


def test_fund_flow_history_returns_rows_in_range_sorted():
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
    finally:
        _delete_test_rows()

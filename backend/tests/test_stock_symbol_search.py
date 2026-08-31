from sqlalchemy import text

from src.app.api.stock import search_a_stock_symbols
from src.core.analytics_database import AStockBasic, AnalyticsSession, ensure_analytics_schema


TEST_SYMBOLS = ("900011.SH", "900012.SH")


def _delete_test_symbols():
    db = AnalyticsSession()
    try:
        db.execute(
            text("DELETE FROM a_stock_basic WHERE ts_code IN (:first, :second)"),
            {"first": TEST_SYMBOLS[0], "second": TEST_SYMBOLS[1]},
        )
        db.commit()
    finally:
        db.close()
        AnalyticsSession.remove()


def test_search_a_stock_symbols_matches_name_and_code():
    ensure_analytics_schema()
    _delete_test_symbols()
    db = AnalyticsSession()
    try:
        db.add_all(
            [
                AStockBasic(
                    ts_code=TEST_SYMBOLS[0], symbol="900011", name="详情搜索甲",
                    market="主板", exchange="SSE", list_status="L",
                ),
                AStockBasic(
                    ts_code=TEST_SYMBOLS[1], symbol="900012", name="详情搜索乙",
                    market="主板", exchange="SSE", list_status="L",
                ),
            ]
        )
        db.commit()
    finally:
        db.close()
        AnalyticsSession.remove()

    try:
        by_name = search_a_stock_symbols(q="搜索乙", limit=10, _="account")
        by_code = search_a_stock_symbols(q="900011", limit=10, _="account")
        assert [item["value"] for item in by_name] == [TEST_SYMBOLS[1]]
        assert by_name[0]["label"] == "详情搜索乙 · 900012.SH"
        assert by_code[0]["name"] == "详情搜索甲"
    finally:
        _delete_test_symbols()

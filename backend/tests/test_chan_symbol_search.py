from sqlalchemy import text

from src.app.api.chan_analysis import search_stock_symbols
from src.core.analytics_database import AStockBasic, AnalyticsSession, ensure_analytics_schema


TEST_SYMBOLS = ("900001.SH", "900002.SH")


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


def test_search_stock_symbols_matches_name_and_code():
    ensure_analytics_schema()
    _delete_test_symbols()
    db = AnalyticsSession()
    try:
        db.add_all(
            [
                AStockBasic(
                    ts_code=TEST_SYMBOLS[0],
                    symbol="900001",
                    name="搜索样本甲",
                    industry="测试行业",
                    market="主板",
                    exchange="SSE",
                    list_status="L",
                ),
                AStockBasic(
                    ts_code=TEST_SYMBOLS[1],
                    symbol="900002",
                    name="搜索样本乙",
                    industry="测试行业",
                    market="主板",
                    exchange="SSE",
                    list_status="L",
                ),
            ]
        )
        db.commit()
    finally:
        db.close()
        AnalyticsSession.remove()

    try:
        by_name = search_stock_symbols(q="样本乙", limit=10, _="admin")
        by_code = search_stock_symbols(q="900001", limit=10, _="admin")
        assert [item["value"] for item in by_name] == [TEST_SYMBOLS[1]]
        assert by_name[0]["label"] == "搜索样本乙 · 900002.SH"
        assert by_code[0]["value"] == TEST_SYMBOLS[0]
    finally:
        _delete_test_symbols()

"""回测持仓与实盘持仓对比合并的单元测试（含“实盘独有”持仓）。"""

from src.core.services.szdt_position_diff import (
    a_stock_backtest_code,
    a_stock_live_symbol,
    merge_backtest_live_positions,
    us_backtest_code,
    us_live_symbol,
)


def test_a_stock_symbol_round_trip():
    assert a_stock_live_symbol("SH.510300") == "510300.SH"
    assert a_stock_backtest_code("510300.SH") == "SH.510300"
    assert a_stock_live_symbol("510300.SH") == "510300.SH"
    assert a_stock_backtest_code("SH.510300") == "SH.510300"


def test_us_symbol_round_trip():
    assert us_live_symbol("US.TQQQ") == "TQQQ"
    assert us_backtest_code("TQQQ") == "US.TQQQ"
    assert us_live_symbol("TQQQ") == "TQQQ"
    assert us_backtest_code("US.TQQQ") == "US.TQQQ"


def test_merge_keeps_backtest_rows_and_appends_live_only():
    latest_positions = [{
        "code": "SH.510300", "name": "300ETF", "quantity": 1000,
        "price": 4.0, "score": -20, "market_value": 4000.0, "position_pct": 10.0,
    }]
    live_positions = {"510300.SH": 600, "159915.SZ": 2000, "588000.SH": 0}

    rows = merge_backtest_live_positions(
        latest_positions,
        live_positions,
        names={"159915.SZ": "创业板ETF"},
        prices={"159915.SZ": 2.5},
    )

    assert [row["code"] for row in rows] == ["SH.510300", "SZ.159915"]
    matched, extra = rows
    # 回测侧原有字段不受影响，只补实盘数量与差额。
    assert matched["quantity"] == 1000
    assert matched["market_value"] == 4000.0
    assert matched["live_quantity"] == 600
    assert matched["live_quantity_diff"] == 400
    assert matched["live_only"] is False
    # 实盘独有：回测股数 0、差额为负，仓位/市值不适用。
    assert extra["live_only"] is True
    assert extra["name"] == "创业板ETF"
    assert extra["quantity"] == 0
    assert extra["price"] == 2.5
    assert extra["score"] is None
    assert extra["position_pct"] is None
    assert extra["market_value"] is None
    assert extra["live_quantity"] == 2000
    assert extra["live_quantity_diff"] == -2000


def test_merge_without_live_data_marks_zero_and_keeps_backtest_only():
    latest_positions = [{"code": "SH.510500", "name": "500ETF", "quantity": 300}]
    rows = merge_backtest_live_positions(latest_positions, {})
    assert len(rows) == 1
    assert rows[0]["live_quantity"] == 0
    assert rows[0]["live_quantity_diff"] == 300
    assert rows[0]["live_only"] is False


def test_merge_supports_nested_live_payload_and_us_symbols():
    latest_positions = [{"code": "US.TQQQ", "name": "TQQQ", "quantity": 10}]
    rows = merge_backtest_live_positions(
        latest_positions,
        {"TQQQ": {"qty": 4}, "SOXL": {"qty": 7}},
        to_live_symbol=us_live_symbol,
        to_backtest_code=us_backtest_code,
        names={"SOXL": "三倍做多半导体"},
        prices={"SOXL": 30.0},
    )
    assert [row["code"] for row in rows] == ["US.TQQQ", "US.SOXL"]
    assert rows[0]["live_quantity_diff"] == 6
    assert rows[1]["live_only"] is True
    assert rows[1]["live_quantity"] == 7
    assert rows[1]["market_value"] is None


def test_merge_ignores_duplicate_and_non_positive_live_rows():
    latest_positions = [{"code": "SH.510300", "quantity": 100}]
    rows = merge_backtest_live_positions(
        latest_positions,
        {"510300.SH": 100, "510500.SH": 0, "510050.SH": -5},
    )
    assert len(rows) == 1
    assert rows[0]["live_quantity_diff"] == 0

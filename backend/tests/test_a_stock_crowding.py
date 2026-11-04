import duckdb
import pytest

from src.core.services.a_stock_crowding import classify_concentration, fetch_crowding_dashboard


def test_extreme_threshold_is_strictly_greater_than_45_percent():
    assert classify_concentration(45.0) == "常态"
    assert classify_concentration(45.0001) == "极端拥挤"


def test_dashboard_uses_top_five_percent_turnover_concentration(tmp_path, monkeypatch):
    database_path = tmp_path / "analytics.duckdb"
    connection = duckdb.connect(str(database_path))
    connection.execute("CREATE TABLE a_stock_market_daily(trade_date DATE, ts_code VARCHAR, amount DOUBLE)")
    connection.execute("CREATE TABLE a_stock_basic(ts_code VARCHAR, name VARCHAR)")
    connection.execute("CREATE TABLE a_stock_sw_member(ts_code VARCHAR, l1_name VARCHAR)")
    rows = [("2026-11-03", "000001.SZ", 100.0)] + [
        ("2026-11-03", f"{index:06d}.SZ", 5.0) for index in range(2, 21)
    ]
    connection.executemany("INSERT INTO a_stock_market_daily VALUES (?, ?, ?)", rows)
    connection.execute("INSERT INTO a_stock_basic VALUES ('000001.SZ', '头部股票')")
    connection.execute("INSERT INTO a_stock_sw_member VALUES ('000001.SZ', '银行')")
    connection.close()
    monkeypatch.setattr(
        "src.core.services.a_stock_crowding.connect_analytics_db",
        lambda: duckdb.connect(str(database_path)),
    )

    result = fetch_crowding_dashboard(days=20)

    # 20 只股票的前 5% 即 1 只；100 / (100 + 19 * 5) = 51.282...
    assert result["latest"]["stock_count"] == 20
    assert result["latest"]["top_stock_count"] == 1
    assert result["latest"]["concentration_pct"] == pytest.approx(100 / 195 * 100)
    assert result["latest"]["level"] == "极端拥挤"
    assert result["constituents"] == [
        {
            "ts_code": "000001.SZ",
            "name": "头部股票",
            "rank": 1,
            "amount_yi": pytest.approx(0.001),
            "market_amount_share_pct": pytest.approx(100 / 195 * 100),
            "cumulative_market_share_pct": pytest.approx(100 / 195 * 100),
        }
    ]

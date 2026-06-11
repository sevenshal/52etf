from datetime import date

from src.core.services.a_stock_consensus import (
    build_a_stock_consensus_history,
    build_a_stock_consensus_candidates,
    normalize_a_stock_symbol,
    search_a_stock_consensus_candidates,
)


def test_normalize_a_stock_symbol_infers_exchange_suffix():
    assert normalize_a_stock_symbol("600519") == "600519.SH"
    assert normalize_a_stock_symbol("000001") == "000001.SZ"
    assert normalize_a_stock_symbol("300750") == "300750.SZ"
    assert normalize_a_stock_symbol("920001") == "920001.BJ"
    assert normalize_a_stock_symbol("600519.SH") == "600519.SH"


def test_consensus_candidates_filter_by_market_cap_undervalue_and_growth():
    rows = [
        {
            "ts_code": "000001.SZ",
            "stock_name": "平安银行",
            "industry": "银行",
            "market": "主板",
            "trade_date": date(2026, 6, 8),
            "close": 10.0,
            "total_mv": 2_000_000.0,
            "circ_mv": 1_500_000.0,
            "report_date": date(2026, 6, 1),
            "report_title": "深度报告A",
            "org_name": "机构A",
            "author_name": "分析师A",
            "quarter": "2026",
            "eps": 1.0,
            "np": 10_000.0,
            "pe": 10.0,
            "rating": "买入",
            "min_price": 14.0,
            "max_price": 16.0,
        },
        {
            "ts_code": "000001.SZ",
            "stock_name": "平安银行",
            "industry": "银行",
            "market": "主板",
            "trade_date": date(2026, 6, 8),
            "close": 10.0,
            "total_mv": 2_000_000.0,
            "circ_mv": 1_500_000.0,
            "report_date": date(2026, 6, 1),
            "report_title": "深度报告A",
            "org_name": "机构A",
            "author_name": "分析师A",
            "quarter": "2027",
            "eps": 1.25,
            "np": 12_500.0,
            "pe": 8.0,
            "rating": "买入",
            "min_price": 14.0,
            "max_price": 16.0,
        },
        {
            "ts_code": "000001.SZ",
            "stock_name": "平安银行",
            "industry": "银行",
            "market": "主板",
            "trade_date": date(2026, 6, 8),
            "close": 10.0,
            "total_mv": 2_000_000.0,
            "circ_mv": 1_500_000.0,
            "report_date": date(2026, 6, 2),
            "report_title": "深度报告B",
            "org_name": "机构B",
            "author_name": "分析师B",
            "quarter": "2026",
            "eps": 1.1,
            "np": 11_000.0,
            "pe": 9.5,
            "rating": "增持",
            "min_price": 15.0,
            "max_price": 17.0,
        },
        {
            "ts_code": "000001.SZ",
            "stock_name": "平安银行",
            "industry": "银行",
            "market": "主板",
            "trade_date": date(2026, 6, 8),
            "close": 10.0,
            "total_mv": 2_000_000.0,
            "circ_mv": 1_500_000.0,
            "report_date": date(2026, 6, 2),
            "report_title": "深度报告B",
            "org_name": "机构B",
            "author_name": "分析师B",
            "quarter": "2027",
            "eps": 1.4,
            "np": 14_000.0,
            "pe": 7.5,
            "rating": "增持",
            "min_price": 15.0,
            "max_price": 17.0,
        },
        {
            "ts_code": "300001.SZ",
            "stock_name": "小市值样本",
            "industry": "软件",
            "market": "创业板",
            "trade_date": date(2026, 6, 8),
            "close": 10.0,
            "total_mv": 200_000.0,
            "circ_mv": 180_000.0,
            "report_date": date(2026, 6, 1),
            "report_title": "深度报告C",
            "org_name": "机构C",
            "author_name": "分析师C",
            "quarter": "2026",
            "eps": 1.0,
            "np": 5_000.0,
            "pe": 10.0,
            "min_price": 20.0,
            "max_price": 22.0,
        },
        {
            "ts_code": "300001.SZ",
            "stock_name": "小市值样本",
            "industry": "软件",
            "market": "创业板",
            "trade_date": date(2026, 6, 8),
            "close": 10.0,
            "total_mv": 200_000.0,
            "circ_mv": 180_000.0,
            "report_date": date(2026, 6, 1),
            "report_title": "深度报告C",
            "org_name": "机构C",
            "author_name": "分析师C",
            "quarter": "2027",
            "eps": 1.4,
            "np": 7_000.0,
            "pe": 7.0,
            "min_price": 20.0,
            "max_price": 22.0,
        },
    ]

    result = build_a_stock_consensus_candidates(
        rows,
        date(2026, 6, 8),
        min_market_cap_100m=100.0,
        min_undervalue_pct=40.0,
        min_growth_pct=20.0,
        min_report_count=2,
    )

    assert [item["symbol"] for item in result] == ["000001.SZ"]
    candidate = result[0]
    assert candidate["target_report_count"] == 2
    assert candidate["organization_count"] == 2
    assert round(candidate["target_price_avg"], 4) == 15.5
    assert round(candidate["undervalue_pct"], 4) == 40.0
    assert round(candidate["growth_pct"], 4) == 26.1905
    assert candidate["growth_source"] == "eps"
    assert candidate["market_cap_100m"] == 200.0
    assert candidate["forecast_year"] == 2026
    assert candidate["next_forecast_year"] == 2027


def test_symbol_search_skips_screening_thresholds():
    rows = [
        {
            "ts_code": "000001.SZ",
            "stock_name": "平安银行",
            "trade_date": date(2026, 6, 8),
            "close": 10.0,
            "total_mv": 10_000.0,
            "report_date": date(2026, 6, 1),
            "report_title": "报告",
            "org_name": "机构",
            "author_name": "分析师",
            "quarter": "2026",
            "eps": 1.0,
            "min_price": 10.2,
            "max_price": 10.4,
        },
        {
            "ts_code": "000001.SZ",
            "stock_name": "平安银行",
            "trade_date": date(2026, 6, 8),
            "close": 10.0,
            "total_mv": 10_000.0,
            "report_date": date(2026, 6, 1),
            "report_title": "报告",
            "org_name": "机构",
            "author_name": "分析师",
            "quarter": "2027",
            "eps": 1.01,
            "min_price": 10.2,
            "max_price": 10.4,
        },
    ]

    result = build_a_stock_consensus_candidates(
        rows,
        date(2026, 6, 8),
        search_symbol="000001",
        min_market_cap_100m=100.0,
        min_undervalue_pct=50.0,
        min_growth_pct=20.0,
        min_report_count=5,
    )

    assert len(result) == 1
    assert result[0]["symbol"] == "000001.SZ"


def test_consensus_history_maps_target_price_range_to_valuation_fields():
    rows = [
        {
            "ts_code": "000001.SZ",
            "report_date": date(2026, 6, 1),
            "report_title": "报告A",
            "org_name": "机构A",
            "author_name": "分析师A",
            "quarter": "2026",
            "eps": 1.0,
            "pe": 10.0,
            "min_price": 14.0,
            "max_price": 16.0,
        },
        {
            "ts_code": "000001.SZ",
            "report_date": date(2026, 6, 1),
            "report_title": "报告A",
            "org_name": "机构A",
            "author_name": "分析师A",
            "quarter": "2027",
            "eps": 1.2,
            "pe": 8.0,
            "min_price": 14.0,
            "max_price": 16.0,
        },
    ]

    history = build_a_stock_consensus_history(rows, date(2026, 6, 8))

    assert len(history) == 1
    assert history[0]["date"] == "2026-06-01"
    assert history[0]["fair_value_lo"] == 14.0
    assert history[0]["fair_value_hi"] == 16.0
    assert history[0]["target_price_avg"] == 15.0
    assert history[0]["forward_next_fy_lo"] is None
    assert history[0]["forward_next_fy_hi"] is None
    assert round(history[0]["growth_pct"], 4) == 20.0


def test_search_consensus_candidates_supports_name_query():
    class FakeDb:
        def execute(self, statement, params=None):
            sql = str(statement)
            if "MAX(trade_date)" in sql:
                return type("ScalarResult", (), {"scalar": lambda self: date(2026, 6, 8)})()
            assert "LIKE :name_pattern" in sql
            assert "r.report_date >= :start_date" not in sql
            assert params["name_pattern"] == "%茅台%"
            rows = [
                {
                    "ts_code": "600519.SH",
                    "report_name": "贵州茅台",
                    "stock_name": "贵州茅台",
                    "industry": "白酒",
                    "market": "主板",
                    "trade_date": date(2026, 6, 8),
                    "close": 1000.0,
                    "total_mv": 20_000_000.0,
                    "circ_mv": 20_000_000.0,
                    "report_date": date(2026, 6, 1),
                    "report_title": "报告A",
                    "org_name": "机构A",
                    "author_name": "分析师A",
                    "quarter": "2026",
                    "eps": 50.0,
                    "min_price": 1200.0,
                    "max_price": 1300.0,
                },
                {
                    "ts_code": "600519.SH",
                    "report_name": "贵州茅台",
                    "stock_name": "贵州茅台",
                    "industry": "白酒",
                    "market": "主板",
                    "trade_date": date(2026, 6, 8),
                    "close": 1000.0,
                    "total_mv": 20_000_000.0,
                    "circ_mv": 20_000_000.0,
                    "report_date": date(2026, 6, 1),
                    "report_title": "报告A",
                    "org_name": "机构A",
                    "author_name": "分析师A",
                    "quarter": "2027",
                    "eps": 60.0,
                    "min_price": 1200.0,
                    "max_price": 1300.0,
                },
            ]
            return type("RowsResult", (), {"mappings": lambda self: type("Mappings", (), {"all": lambda self: rows})()})()

    result = search_a_stock_consensus_candidates(
        FakeDb(),
        symbol="茅台",
        min_report_count=5,
        min_undervalue_pct=50.0,
    )

    assert [item["symbol"] for item in result] == ["600519.SH"]


def test_search_consensus_candidates_symbol_query_ignores_report_window():
    class FakeDb:
        def execute(self, statement, params=None):
            sql = str(statement)
            if "MAX(trade_date)" in sql:
                return type("ScalarResult", (), {"scalar": lambda self: date(2026, 6, 8)})()
            assert "r.ts_code = :symbol" in sql
            assert "r.report_date >= :start_date" not in sql
            assert params["symbol"] == "300721.SZ"
            rows = [
                {
                    "ts_code": "300721.SZ",
                    "report_name": "怡达股份",
                    "stock_name": "怡达股份",
                    "industry": "化工",
                    "market": "创业板",
                    "trade_date": date(2026, 6, 8),
                    "close": 35.0,
                    "total_mv": 590_000.0,
                    "circ_mv": 490_000.0,
                    "report_date": date(2022, 12, 20),
                    "report_title": "旧研报",
                    "org_name": "机构A",
                    "author_name": "分析师A",
                    "quarter": "2022",
                    "eps": 1.0,
                    "min_price": 65.0,
                    "max_price": None,
                },
                {
                    "ts_code": "300721.SZ",
                    "report_name": "怡达股份",
                    "stock_name": "怡达股份",
                    "industry": "化工",
                    "market": "创业板",
                    "trade_date": date(2026, 6, 8),
                    "close": 35.0,
                    "total_mv": 590_000.0,
                    "circ_mv": 490_000.0,
                    "report_date": date(2022, 12, 20),
                    "report_title": "旧研报",
                    "org_name": "机构A",
                    "author_name": "分析师A",
                    "quarter": "2023",
                    "eps": 1.2,
                    "min_price": 65.0,
                    "max_price": None,
                },
            ]
            return type("RowsResult", (), {"mappings": lambda self: type("Mappings", (), {"all": lambda self: rows})()})()

    result = search_a_stock_consensus_candidates(
        FakeDb(),
        symbol="300721",
        report_lookback_days=60,
        min_report_count=5,
        min_undervalue_pct=200.0,
        min_growth_pct=200.0,
    )

    assert [item["symbol"] for item in result] == ["300721.SZ"]
    assert result[0]["latest_report_date"] == "2022-12-20"

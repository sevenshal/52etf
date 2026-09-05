from datetime import date

from src.core.services.a_stock_consensus import (
    _aggregate_report_rows,
    build_a_stock_consensus_history,
    build_a_stock_rolling_consensus_history,
    build_a_stock_consensus_candidates,
    normalize_a_stock_symbol,
    search_a_stock_consensus_candidates,
)


def test_consensus_robust_target_range_limits_outlier_influence():
    rows = [
        {
            "report_date": date(2026, 6, day),
            "report_title": f"报告{day}",
            "org_name": f"机构{day}",
            "author_name": f"分析师{day}",
            "min_price": target,
            "max_price": target,
        }
        for day, target in enumerate([100.0, 102.0, 104.0, 106.0, 1000.0], start=1)
    ]

    result = _aggregate_report_rows(rows, date(2026, 6, 8))

    assert result["target_price_min"] == 100.0
    assert result["target_price_max"] == 1000.0
    assert result["target_price_q25"] == 102.0
    assert result["target_price_median"] == 104.0
    assert result["target_price_q75"] == 106.0


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


def test_rolling_consensus_history_uses_point_in_time_percentile_range():
    rows = [
        {
            "report_date": date(2026, 1, day),
            "report_title": f"报告{day}",
            "org_name": f"机构{day}",
            "author_name": f"分析师{day}",
            "min_price": target,
            "max_price": target,
        }
        for day, target in ((1, 100.0), (2, 110.0), (3, 120.0), (20, 1000.0))
    ]

    history = build_a_stock_rolling_consensus_history(
        rows,
        [date(2026, 1, 2), date(2026, 1, 3)],
        report_lookback_days=180,
    )

    assert [item["date"] for item in history] == ["2026-01-02", "2026-01-03"]
    assert history[0]["fair_value_lo"] == 102.5
    assert history[0]["fair_value_hi"] == 107.5
    assert history[0]["target_report_count"] == 2
    assert history[1]["fair_value_lo"] == 105.0
    assert history[1]["fair_value_hi"] == 115.0
    assert history[1]["target_report_count"] == 3


def test_rolling_consensus_history_expires_reports_outside_window():
    rows = [
        {
            "report_date": date(2026, 1, 1),
            "report_title": "旧报告",
            "org_name": "旧机构",
            "author_name": "旧分析师",
            "min_price": 100.0,
            "max_price": 100.0,
        },
        {
            "report_date": date(2026, 1, 10),
            "report_title": "新报告",
            "org_name": "新机构",
            "author_name": "新分析师",
            "min_price": 120.0,
            "max_price": 120.0,
        },
    ]

    history = build_a_stock_rolling_consensus_history(
        rows,
        [date(2026, 1, 15)],
        report_lookback_days=7,
    )

    assert history[0]["fair_value_lo"] == 120.0
    assert history[0]["fair_value_hi"] == 120.0
    assert history[0]["target_report_count"] == 1


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


def _consensus_row(day, *, min_price=None, max_price=None, report_date=None, quarter="2026", eps=1.0):
    return {
        "ts_code": "600612.SH",
        "report_name": "老凤祥",
        "stock_name": "老凤祥",
        "industry": "服饰",
        "market": "主板",
        "trade_date": date(2026, 9, 4),
        "close": 33.80,
        "total_mv": 1_768_000.0,
        "circ_mv": 1_072_000.0,
        "report_date": report_date or date(2026, 8, day),
        "report_title": f"报告{day}",
        "org_name": f"机构{day}",
        "author_name": f"分析师{day}",
        "quarter": quarter,
        "eps": eps,
        "min_price": min_price,
        "max_price": max_price,
    }


def test_consensus_target_range_covers_average_when_bounds_are_one_sided():
    """单边填充 min_price/max_price 时，区间必须仍然包住均值。

    真实数据里多数研报只填 min_price(max_price 为空)，只有个别研报两边都填。
    旧实现把 min/max 分别取自不同的研报子集，会算出 42.45 ~ 42.45 的"点区间"
    却配上 60+ 的均值。
    """
    rows = [_consensus_row(day, min_price=price) for day, price in
            enumerate([70.0, 68.0, 65.0, 62.0, 60.0, 58.0], start=1)]
    rows.append(_consensus_row(7, min_price=42.45, max_price=42.45))

    result = _aggregate_report_rows(rows, date(2026, 9, 4))

    assert result["target_price_min"] == 42.45
    assert result["target_price_max"] == 70.0
    assert result["target_price_min"] <= result["target_price_avg"] <= result["target_price_max"]


def test_consensus_target_bounds_tolerate_inverted_min_max():
    rows = [_consensus_row(1, min_price=80.0, max_price=60.0)]

    result = _aggregate_report_rows(rows, date(2026, 9, 4))

    assert result["target_price_min"] == 60.0
    assert result["target_price_max"] == 80.0
    assert result["target_price_avg"] == 70.0


def test_consensus_only_uses_reports_published_after_latest_annual_report():
    rows = [
        _consensus_row(1, min_price=90.0, max_price=90.0, report_date=date(2026, 3, 10)),
        _consensus_row(2, min_price=60.0, max_price=60.0, report_date=date(2026, 5, 20)),
    ]

    result = _aggregate_report_rows(
        rows,
        date(2026, 9, 4),
        latest_annual_ann_date=date(2026, 4, 25),
        prev_annual_ann_date=date(2025, 4, 26),
    )

    assert result["target_report_count"] == 1
    assert result["target_price_avg"] == 60.0
    assert result["consensus_window"] == "post_latest_annual"
    assert result["is_stale"] is False


def test_consensus_falls_back_to_previous_annual_window_and_marks_stale():
    rows = [
        _consensus_row(1, min_price=90.0, max_price=90.0, report_date=date(2024, 12, 1)),
        _consensus_row(2, min_price=60.0, max_price=60.0, report_date=date(2026, 3, 10)),
    ]

    result = _aggregate_report_rows(
        rows,
        date(2026, 9, 4),
        latest_annual_ann_date=date(2026, 4, 25),
        prev_annual_ann_date=date(2025, 4, 26),
    )

    assert result["target_report_count"] == 1
    assert result["target_price_avg"] == 60.0
    assert result["consensus_window"] == "post_prev_annual"
    assert result["is_stale"] is True


def test_consensus_window_unfiltered_without_annual_announcement_date():
    rows = [_consensus_row(1, min_price=60.0, max_price=60.0, report_date=date(2024, 12, 1))]

    result = _aggregate_report_rows(rows, date(2026, 9, 4))

    assert result["target_report_count"] == 1
    assert result["consensus_window"] == "unfiltered"
    assert result["is_stale"] is True


def test_consensus_candidates_expose_annual_report_window_flags():
    rows = [
        dict(_consensus_row(1, min_price=90.0, max_price=90.0, report_date=date(2026, 3, 10)),
             latest_annual_ann_date=date(2026, 4, 25), prev_annual_ann_date=date(2025, 4, 26)),
        dict(_consensus_row(2, min_price=60.0, max_price=60.0, report_date=date(2026, 5, 20)),
             latest_annual_ann_date=date(2026, 4, 25), prev_annual_ann_date=date(2025, 4, 26)),
    ]

    candidates = build_a_stock_consensus_candidates(rows, date(2026, 9, 4), has_search=True)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["target_report_count"] == 1
    assert candidate["target_price_min"] == 60.0
    assert candidate["consensus_window"] == "post_latest_annual"
    assert candidate["is_stale"] is False
    assert candidate["latest_annual_ann_date"] == "2026-04-25"
    # 低估率仍取最悲观的单家下沿
    assert round(candidate["undervalue_pct"], 2) == round((60.0 / 33.80 - 1) * 100, 2)

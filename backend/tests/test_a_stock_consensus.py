from datetime import date

import pytest

from src.core.services.a_stock_consensus import (
    CONSENSUS_POOL_T,
    CONSENSUS_POOL_T1,
    DisclosureCutoffs,
    _PriceFactors,
    _aggregate_report_rows,
    _disclosure_cutoffs_as_of,
    _normalize_org_name,
    _parse_forecast_fiscal_year,
    build_a_stock_consensus_candidates,
    build_a_stock_rolling_consensus_history,
    load_a_stock_consensus_detail,
    load_a_stock_consensus_valuation_history_map,
    load_a_stock_price_factors,
    normalize_a_stock_symbol,
    search_a_stock_consensus_candidates,
)


AS_OF = date(2026, 9, 10)
# 老凤祥式的财报截面：2025 年报与 2026 一季报同一天(04-24)披露，2026 半年报 08-27 披露。
DISCLOSURES = [
    (date(2025, 8, 29), date(2025, 6, 30)),
    (date(2025, 10, 30), date(2025, 9, 30)),
    (date(2026, 4, 24), date(2025, 12, 31)),
    (date(2026, 4, 24), date(2026, 3, 31)),
    (date(2026, 8, 27), date(2026, 6, 30)),
]


def _cutoffs(as_of=AS_OF, disclosures=DISCLOSURES):
    return _disclosure_cutoffs_as_of(disclosures, as_of)


def _report(org, report_date, target, eps_by_year, *, max_price=None, np_by_year=None,
            title=None, symbol="600612.SH", rating="买入", market=None):
    """一篇研报：每个预测 quarter 一行，目标价在每行重复(与数据源一致)。"""
    rows = []
    for quarter, eps in eps_by_year.items():
        rows.append({
            "ts_code": symbol,
            "report_name": "老凤祥",
            "report_date": report_date,
            "report_title": title or f"{org}{report_date.isoformat()}",
            "org_name": org,
            "author_name": f"{org}分析师",
            "quarter": f"{quarter}Q4" if isinstance(quarter, int) else quarter,
            "eps": eps,
            "np": (np_by_year or {}).get(quarter),
            "pe": None,
            "rating": rating,
            "min_price": target,
            "max_price": max_price,
            **(market or {}),
        })
    return rows


def _laofengxiang_rows(market=None):
    rows = []
    rows += _report("中金公司", date(2026, 8, 30), 42.45, {2026: 2.45, 2027: 2.65}, max_price=42.45, market=market)
    rows += _report("华创证券", date(2026, 4, 28), 51.67, {2026: 3.04, 2027: 3.23, 2028: 3.46}, market=market)
    rows += _report("国泰海通", date(2026, 4, 27), 44.74, {2026: 2.80, 2027: 2.91, 2028: 3.03}, market=market)
    rows += _report("广发证券", date(2026, 4, 27), 44.86, {2026: 2.80, 2027: 2.99, 2028: 3.15}, market=market)
    rows += _report("中金", date(2026, 4, 26), 49.12, {2026: 2.46, 2027: 2.65}, market=market)
    rows += _report("华泰证券", date(2026, 4, 24), 50.00, {2026: 2.94, 2027: 3.26, 2028: 3.59}, market=market)
    return rows


class _Result:
    def __init__(self, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def scalar(self):
        return self._scalar

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


def _disclosure_rows(symbol="600612.SH", disclosures=DISCLOSURES):
    return [
        {"ts_code": symbol, "end_date": period, "ann_date": disclosed}
        for disclosed, period in disclosures
    ]


def test_normalize_a_stock_symbol_infers_exchange_suffix():
    assert normalize_a_stock_symbol("600519") == "600519.SH"
    assert normalize_a_stock_symbol("000001") == "000001.SZ"
    assert normalize_a_stock_symbol("300750") == "300750.SZ"
    assert normalize_a_stock_symbol("920001") == "920001.BJ"
    assert normalize_a_stock_symbol("600519.SH") == "600519.SH"


def test_forecast_quarter_only_accepts_full_year_q4():
    assert _parse_forecast_fiscal_year("2026Q4") == 2026
    assert _parse_forecast_fiscal_year(" 2027q4 ") == 2027
    # 中报/季报期预测和全年不可比；其余是脏数据。
    for value in ("2026Q2", "2026Q1", "Q", None, "", "2026Q5", "2026-Q4", "2026"):
        assert _parse_forecast_fiscal_year(value) is None


def test_org_name_aliases_merge_the_same_broker():
    assert _normalize_org_name("中金公司") == "中金"
    assert _normalize_org_name("国泰海通证券") == "国泰海通"
    assert _normalize_org_name("中信建投证券") == "中信建投"
    assert _normalize_org_name("申万宏源研究") == "申万宏源证券"
    # 合并前的券商仍是两家独立机构。
    assert _normalize_org_name("国泰君安") == "国泰君安"
    assert _normalize_org_name("海通证券") == "海通证券"


def test_disclosure_cutoffs_take_t1_as_the_last_strictly_earlier_disclosure():
    assert _cutoffs(date(2026, 9, 10)) == DisclosureCutoffs(
        date(2026, 6, 30), date(2026, 8, 27), date(2026, 3, 31), date(2026, 4, 24),
    )
    # 年报与一季报同日披露：T 取报告期最新的一季报，T-1 必须跳过同一天，退到上一年三季报。
    assert _cutoffs(date(2026, 5, 1)) == DisclosureCutoffs(
        date(2026, 3, 31), date(2026, 4, 24), date(2025, 9, 30), date(2025, 10, 30),
    )
    # 只能看到当天已披露的财报，不能用未来的。
    assert _cutoffs(date(2026, 4, 23)) == DisclosureCutoffs(
        date(2025, 9, 30), date(2025, 10, 30), date(2025, 6, 30), date(2025, 8, 29),
    )
    assert _cutoffs(date(2025, 1, 1)) == DisclosureCutoffs()


def test_laofengxiang_falls_back_to_t1_pool_and_merges_broker_aliases():
    result = _aggregate_report_rows(_laofengxiang_rows(), AS_OF, _cutoffs(), include_details=True)

    # T 池(08-27 之后)只有中金公司一家 → 退到 T-1 池(04-24 之后)。
    assert result["pool"] == CONSENSUS_POOL_T1
    assert result["is_stale"] is True
    assert result["pool_start_date"] == date(2026, 4, 24)
    # 中金 / 中金公司 是同一家，取它最新的 08-30 那篇。
    assert result["organization_count"] == 5
    zhongjin = next(view for view in result["organizations"] if view["org_name"] == "中金")
    assert zhongjin["report_date"] == date(2026, 8, 30)
    assert zhongjin["raw_org_names"] == ["中金公司"]

    current, following, after_next = result["horizons"]
    assert (current["fiscal_year"], following["fiscal_year"], after_next["fiscal_year"]) == (2026, 2027, 2028)
    assert current["lo"] == pytest.approx(42.45)
    assert current["lo_org"] == "中金"
    assert current["hi"] == pytest.approx(51.67)
    assert current["hi_org"] == "华创证券"
    assert current["lo"] <= current["avg"] <= current["hi"]
    # 下财年 = 各机构自己的 目标价 × EPS2027 / EPS2026。
    assert following["lo"] == pytest.approx(42.45 * 2.65 / 2.45)
    assert following["hi"] == pytest.approx(50.00 * 3.26 / 2.94)
    # 中金没给 2028 预测，下下财年只有 4 家。
    assert after_next["organization_count"] == 4
    assert after_next["lo"] == pytest.approx(44.74 * 3.03 / 2.80)
    assert after_next["hi"] == pytest.approx(50.00 * 3.59 / 2.94)
    # 低估率口径：target_price_min 仍是最悲观的单家下沿。
    assert result["target_price_min"] == pytest.approx(42.45)


def test_t_pool_is_used_when_two_organizations_publish_after_t():
    rows = _laofengxiang_rows()
    rows += _report("华泰证券", date(2026, 9, 1), 47.0, {2026: 2.90, 2027: 3.10})

    result = _aggregate_report_rows(rows, AS_OF, _cutoffs())

    assert result["pool"] == CONSENSUS_POOL_T
    assert result["is_stale"] is False
    assert result["organization_count"] == 2
    assert result["horizons"][0]["lo"] == pytest.approx(42.45)
    assert result["horizons"][0]["hi"] == pytest.approx(47.0)


def test_broker_aliases_do_not_pass_the_two_organization_threshold():
    rows = []
    rows += _report("中金", date(2026, 9, 1), 40.0, {2026: 2.0})
    rows += _report("中金公司", date(2026, 9, 5), 41.0, {2026: 2.1})
    rows += _report("华泰证券", date(2026, 5, 1), 50.0, {2026: 2.5})

    result = _aggregate_report_rows(rows, AS_OF, _cutoffs())

    assert result["pool"] == CONSENSUS_POOL_T1
    assert result["organization_count"] == 2


def test_no_valuation_when_t1_pool_has_no_target_price():
    rows = _report("华泰证券", date(2026, 3, 1), 50.0, {2026: 2.5})
    # T 期之后只有不带目标价的研报。
    rows += _report("中金", date(2026, 9, 1), None, {2026: 2.0})

    assert _aggregate_report_rows(rows, AS_OF, _cutoffs()) is None


def test_no_valuation_without_disclosure_dates():
    assert _aggregate_report_rows(_laofengxiang_rows(), AS_OF, DisclosureCutoffs()) is None


def test_same_organization_and_quarter_only_use_the_latest_forecast():
    rows = []
    rows += _report("机构A", date(2026, 5, 10), 30.0, {2026: 1.0, 2027: 1.5})
    # 同一机构更新了盈利预测，但这篇没给目标价。
    rows += _report("机构A", date(2026, 6, 10), None, {2026: 1.2, 2027: 1.3})
    rows += _report("机构B", date(2026, 5, 20), 40.0, {2026: 2.0, 2027: 2.2})

    result = _aggregate_report_rows(rows, AS_OF, _cutoffs())

    # 估值用每家机构最新一篇带目标价研报自己的盈利预测。
    assert result["horizons"][1]["lo"] == pytest.approx(40.0 * 2.2 / 2.0)
    assert result["horizons"][1]["hi"] == pytest.approx(30.0 * 1.5 / 1.0)
    # 共识 EPS 按 机构 + quarter 取最新。
    assert result["consensus_eps"] == pytest.approx((1.2 + 2.0) / 2)
    assert result["next_consensus_eps"] == pytest.approx((1.3 + 2.2) / 2)
    assert result["report_count"] == 3
    assert result["organization_count"] == 2


def test_past_fiscal_years_interim_quarters_and_dirty_quarters_are_ignored():
    rows = _report("机构A", date(2026, 5, 10), 30.0, {
        2025: 0.8, "2026Q2": 0.4, "Q": 9.9, 2026: 1.0, 2027: 1.1,
    })
    # 只预测过去年份的研报不进池子，也不算机构。
    rows += _report("机构B", date(2026, 5, 11), 99.0, {2025: 3.0})

    result = _aggregate_report_rows(rows, AS_OF, _cutoffs(), include_details=True)

    assert result["organization_count"] == 1
    view = result["organizations"][0]
    assert [item["quarter"] for item in view["forecasts"]] == ["2026Q4", "2027Q4"]
    assert result["horizons"][1]["lo"] == pytest.approx(33.0)


def test_one_sided_and_inverted_target_price_bounds_stay_consistent():
    rows = []
    rows += _report("机构A", date(2026, 5, 10), 60.0, {2026: 1.0})
    rows += _report("机构B", date(2026, 5, 11), 42.45, {2026: 1.0}, max_price=42.45)
    rows += _report("机构C", date(2026, 5, 12), 80.0, {2026: 1.0}, max_price=70.0)

    current = _aggregate_report_rows(rows, AS_OF, _cutoffs())["horizons"][0]

    assert current["lo"] == pytest.approx(42.45)
    assert current["hi"] == pytest.approx(80.0)
    assert current["lo"] <= current["avg"] <= current["hi"]


def test_forward_values_fall_back_to_net_profit_ratio_without_eps():
    rows = _report("机构A", date(2026, 5, 10), 30.0, {2026: None, 2027: None},
                   np_by_year={2026: 10.0, 2027: 12.0})

    result = _aggregate_report_rows(rows, AS_OF, _cutoffs(), include_details=True)

    value = result["organizations"][0]["values"][1]
    assert value["basis"] == "np"
    assert value["lo"] == pytest.approx(36.0)
    assert value["base_quarter"] == "2026Q4"
    assert value["quarter"] == "2027Q4"


def _candidate_market(symbol, close, total_mv, name="测试股"):
    return {
        "ts_code": symbol,
        "stock_name": name,
        "industry": "测试",
        "market": "主板",
        "trade_date": AS_OF,
        "close": close,
        "total_mv": total_mv,
        "circ_mv": total_mv,
    }


def _candidate_rows():
    rows = []
    # 低估且增长的中盘股。
    rows += _report("机构A", date(2026, 9, 1), 15.0, {2026: 1.0, 2027: 1.2}, symbol="000001.SZ",
                    market=_candidate_market("000001.SZ", 10.0, 2_000_000.0))
    rows += _report("机构B", date(2026, 9, 2), 16.0, {2026: 1.0, 2027: 1.2}, symbol="000001.SZ",
                    market=_candidate_market("000001.SZ", 10.0, 2_000_000.0))
    # 市值太小。
    rows += _report("机构A", date(2026, 9, 1), 30.0, {2026: 1.0, 2027: 1.5}, symbol="000002.SZ",
                    market=_candidate_market("000002.SZ", 10.0, 500_000.0))
    # 不低估。
    rows += _report("机构A", date(2026, 9, 1), 9.0, {2026: 1.0, 2027: 1.5}, symbol="000003.SZ",
                    market=_candidate_market("000003.SZ", 10.0, 2_000_000.0))
    return rows


def _candidate_disclosures():
    return {symbol: DISCLOSURES for symbol in ("000001.SZ", "000002.SZ", "000003.SZ")}


def test_consensus_candidates_filter_by_market_cap_undervalue_and_growth():
    result = build_a_stock_consensus_candidates(
        _candidate_rows(),
        AS_OF,
        disclosures=_candidate_disclosures(),
        min_market_cap_100m=100.0,
        min_undervalue_pct=10.0,
        min_growth_pct=10.0,
    )

    assert [item["symbol"] for item in result] == ["000001.SZ"]
    candidate = result[0]
    # 低估率取最悲观的单家下沿 15 / 10 - 1。
    assert candidate["undervalue_pct"] == pytest.approx(50.0)
    assert candidate["target_price_min"] == pytest.approx(15.0)
    assert candidate["target_price_max"] == pytest.approx(16.0)
    assert candidate["growth_pct"] == pytest.approx(20.0)
    assert candidate["pool"] == CONSENSUS_POOL_T
    assert candidate["is_stale"] is False
    assert candidate["t_period_label"] == "2026半年报"
    assert candidate["t_disclosure_date"] == "2026-08-27"
    assert candidate["t1_period_label"] == "2026一季报"
    assert candidate["organization_count"] == 2


def test_consensus_candidates_respect_minimum_organization_count():
    result = build_a_stock_consensus_candidates(
        _candidate_rows(),
        AS_OF,
        disclosures=_candidate_disclosures(),
        min_market_cap_100m=None,
        min_undervalue_pct=None,
        min_growth_pct=None,
        min_organization_count=2,
    )

    assert [item["symbol"] for item in result] == ["000001.SZ"]


def test_symbol_search_skips_screening_thresholds():
    result = build_a_stock_consensus_candidates(
        _candidate_rows(),
        AS_OF,
        disclosures=_candidate_disclosures(),
        search_symbol="000003",
        min_undervalue_pct=200.0,
        min_growth_pct=200.0,
        min_organization_count=5,
    )

    assert "000003.SZ" in [item["symbol"] for item in result]


def _search_fake_db(report_rows, captured):
    class FakeDb:
        def execute(self, statement, params=None):
            sql = str(statement)
            if "MAX(trade_date)" in sql:
                return _Result(scalar=AS_OF)
            if "a_stock_adj_factor" in sql:
                return _Result([])
            if "a_stock_income" in sql:
                captured.setdefault("income_params", params)
                return _Result(_disclosure_rows(report_rows[0]["ts_code"]))
            captured["sql"] = sql
            captured["params"] = params
            return _Result(report_rows)

    return FakeDb()


def test_search_consensus_candidates_supports_symbol_query():
    captured = {}
    rows = _laofengxiang_rows(market=_candidate_market("600612.SH", 33.80, 1_768_000.0, "老凤祥"))

    result = search_a_stock_consensus_candidates(
        _search_fake_db(rows, captured),
        symbol="600612",
        min_undervalue_pct=200.0,
        min_growth_pct=200.0,
        min_organization_count=10,
    )

    assert "r.ts_code = :symbol" in captured["sql"]
    assert captured["params"]["symbol"] == "600612.SH"
    assert captured["params"]["report_start"] < date(2026, 4, 24)
    assert captured["income_params"]["symbol_0"] == "600612.SH"
    assert [item["symbol"] for item in result] == ["600612.SH"]
    assert result[0]["pool"] == CONSENSUS_POOL_T1
    assert result[0]["undervalue_pct"] == pytest.approx((42.45 / 33.80 - 1) * 100)


def test_search_consensus_candidates_supports_name_query():
    captured = {}
    rows = _laofengxiang_rows(market=_candidate_market("600612.SH", 33.80, 1_768_000.0, "老凤祥"))

    result = search_a_stock_consensus_candidates(_search_fake_db(rows, captured), symbol="老凤祥")

    assert "b.name LIKE :name_pattern" in captured["sql"]
    assert captured["params"]["name_pattern"] == "%老凤祥%"
    assert [item["name"] for item in result] == ["老凤祥"]


def test_rolling_history_replays_pools_point_in_time():
    rows = []
    rows += _report("机构A", date(2026, 5, 10), 30.0, {2026: 1.0, 2027: 1.1})
    rows += _report("机构B", date(2026, 5, 20), 40.0, {2026: 1.0, 2027: 1.2})
    rows += _report("机构C", date(2026, 8, 30), 50.0, {2026: 1.0, 2027: 1.3})

    history = build_a_stock_rolling_consensus_history(
        rows,
        [date(2026, 4, 30), date(2026, 8, 26), date(2026, 8, 28), date(2026, 8, 31)],
        disclosures=DISCLOSURES,
    )
    by_date = {item["date"]: item for item in history}

    # 04-30：T=一季报(04-24)之后还没有研报，T-1(去年三季报 10-30)之后也没有 → 不出点，前端沿用前值。
    assert "2026-04-30" not in by_date
    # 08-26：T=一季报(04-24)，A、B 两家 → T 池。
    assert by_date["2026-08-26"]["pool"] == CONSENSUS_POOL_T
    assert (by_date["2026-08-26"]["fair_value_lo"], by_date["2026-08-26"]["fair_value_hi"]) == (30.0, 40.0)
    # 08-28：半年报(08-27)已披露，但 C 的研报 08-30 才发，当天看不到 → 退 T-1 池。
    assert by_date["2026-08-28"]["pool"] == CONSENSUS_POOL_T1
    assert by_date["2026-08-28"]["fair_value_hi"] == 40.0
    # 08-31：T 池只有 C 一家 → 仍退 T-1 池，三家都在。
    assert by_date["2026-08-31"]["pool"] == CONSENSUS_POOL_T1
    assert (by_date["2026-08-31"]["fair_value_lo"], by_date["2026-08-31"]["fair_value_hi"]) == (30.0, 50.0)
    assert by_date["2026-08-31"]["forward_next_fy_hi"] == pytest.approx(65.0)
    assert by_date["2026-08-31"]["forward_next2_fy_hi"] is None


def test_valuation_history_map_forward_fills_missing_days_up_to_365_days():
    disclosures = [
        (date(2024, 10, 30), date(2024, 9, 30)),
        (date(2025, 4, 25), date(2024, 12, 31)),
        (date(2025, 4, 25), date(2025, 3, 31)),
        (date(2025, 8, 28), date(2025, 6, 30)),
        (date(2025, 10, 30), date(2025, 9, 30)),
        (date(2026, 4, 24), date(2025, 12, 31)),
        (date(2026, 4, 24), date(2026, 3, 31)),
        (date(2026, 8, 27), date(2026, 6, 30)),
    ]
    report_rows = _report("机构A", date(2025, 5, 10), 30.0, {2025: 1.0, 2026: 1.2}, symbol="600519.SH")
    requested = [date(2025, 6, 2), date(2025, 9, 1), date(2025, 11, 3), date(2026, 10, 29), date(2026, 11, 2)]
    market_rows = [
        {"ts_code": "600519.SH", "trade_date": day, "close": 20.0}
        for day in requested + [date(2025, 10, 29)]
    ]

    class FakeDb:
        def execute(self, statement, params=None):
            sql = str(statement)
            if "a_stock_income" in sql:
                return _Result(_disclosure_rows("600519.SH", disclosures))
            if "a_stock_report_rc" in sql:
                return _Result(report_rows)
            return _Result(market_rows)

    history = load_a_stock_consensus_valuation_history_map(FakeDb(), ["600519.SH"], requested)

    first = history[date(2025, 6, 2)]["600519.SH"]
    assert first["fair_value_lo"] == 30.0
    assert first["is_ffilled"] is False
    assert first["is_stale"] is True  # 只有一家机构，走的 T-1 池
    assert history[date(2025, 9, 1)]["600519.SH"]["is_ffilled"] is False
    # 三季报(10-30)披露后 T-1 池(半年报 08-28 之后)没有研报 → 沿用 10-29 的估值。
    ffilled = history[date(2025, 11, 3)]["600519.SH"]
    assert ffilled["is_ffilled"] is True
    assert ffilled["is_stale"] is True
    assert ffilled["fair_value_lo"] == 30.0
    # 距最后一次有估值(2025-10-29)正好 365 天仍沿用，超过就不再计入。
    assert history[date(2026, 10, 29)]["600519.SH"]["is_ffilled"] is True
    assert date(2026, 11, 2) not in history


def _detail_fake_db(report_rows, disclosures=DISCLOSURES, factor_rows=()):
    class FakeDb:
        def execute(self, statement, params=None):
            sql = str(statement)
            if "a_stock_adj_factor" in sql:
                return _Result(list(factor_rows))
            if "a_stock_income" in sql:
                return _Result(_disclosure_rows("600612.SH", disclosures))
            if "a_stock_report_rc" in sql:
                return _Result(report_rows)
            return _Result([{"trade_date": AS_OF, "close": 33.80}])

    return FakeDb()


def test_consensus_detail_exposes_each_organization_and_horizon():
    detail = load_a_stock_consensus_detail(_detail_fake_db(_laofengxiang_rows()), "600612")

    assert detail["status"] == "available"
    assert detail["symbol"] == "600612.SH"
    assert detail["pool"] == CONSENSUS_POOL_T1
    assert detail["t_period_label"] == "2026半年报"
    assert detail["t_disclosure_date"] == "2026-08-27"
    assert detail["t1_period_label"] == "2026一季报"
    assert detail["pool_start_date"] == "2026-04-24"
    assert [item["fiscal_year"] for item in detail["horizons"]] == [2026, 2027, 2028]
    assert detail["horizons"][2]["hi_org"] == "华泰证券"
    huatai = next(item for item in detail["organizations"] if item["org_name"] == "华泰证券")
    assert huatai["report_date"] == "2026-04-24"
    assert [item["quarter"] for item in huatai["forecasts"]] == ["2026Q4", "2027Q4", "2028Q4"]
    assert huatai["values"][2]["base_quarter"] == "2026Q4"
    assert huatai["values"][2]["quarter"] == "2028Q4"


def test_consensus_detail_reports_why_valuation_is_unavailable():
    rows = _report("华泰证券", date(2026, 3, 1), 50.0, {2026: 2.5})

    detail = load_a_stock_consensus_detail(_detail_fake_db(rows), "600612.SH")

    assert detail["status"] == "unavailable"
    assert detail["reason"] == "no_target_price_in_pool"
    assert detail["t1_disclosure_date"] == "2026-04-24"


def _factors(*points):
    return _PriceFactors([day for day, _ in points], [value for _, value in points])


# 2026-06-20 除权：10 送 10，复权因子翻倍。
TEN_FOR_TEN = _factors((date(2025, 1, 2), 1.0), (date(2026, 6, 20), 2.0))


def test_price_factors_step_function():
    assert TEN_FOR_TEN.on_or_before(date(2026, 6, 19)) == 1.0
    assert TEN_FOR_TEN.on_or_before(date(2026, 6, 20)) == 2.0
    assert TEN_FOR_TEN.on_or_before(date(2024, 12, 31)) is None
    assert TEN_FOR_TEN.latest == 2.0


def test_load_price_factors_keeps_change_points_per_symbol():
    captured = {}

    class FakeDb:
        def execute(self, statement, params=None):
            captured["sql"] = str(statement)
            return _Result([
                {"ts_code": "600612.SH", "trade_date": date(2026, 6, 20), "adj_factor": 2.0},
                {"ts_code": "600612.SH", "trade_date": date(2025, 1, 2), "adj_factor": 1.0},
                {"ts_code": "000001.SZ", "trade_date": date(2025, 1, 2), "adj_factor": None},
            ])

    result = load_a_stock_price_factors(FakeDb(), ["600612.SH", "000001.SZ"], start=date(2025, 1, 1))

    assert "LAG(adj_factor)" in captured["sql"]
    assert result["600612.SH"] == TEN_FOR_TEN
    assert "000001.SZ" not in result


def test_target_prices_written_before_ex_rights_are_restated_to_current_prices():
    rows = _report("机构A", date(2026, 5, 10), 40.0, {2026: 2.0, 2027: 2.4})
    rows += _report("机构B", date(2026, 7, 1), 22.0, {2026: 1.0, 2027: 1.2})

    result = _aggregate_report_rows(rows, AS_OF, _cutoffs(), price_factors=TEN_FOR_TEN, include_details=True)

    current, following, _ = result["horizons"]
    # 机构 A 的 40 元写在 10 送 10 之前，换算到除权后是 20 元。
    assert current["lo"] == pytest.approx(20.0)
    assert current["lo_org"] == "机构A"
    assert current["hi"] == pytest.approx(22.0)
    assert following["lo"] == pytest.approx(20.0 * 2.4 / 2.0)
    assert result["target_price_min"] == pytest.approx(20.0)
    # 共识 EPS 也换到同一股本口径：A 的 2.0 元对应除权后 1.0 元。
    assert result["consensus_eps"] == pytest.approx(1.0)
    view = next(item for item in result["organizations"] if item["org_name"] == "机构A")
    assert view["target_price_low_raw"] == 40.0
    assert view["price_adjustment"] == pytest.approx(0.5)
    assert view["forecasts"][0]["eps"] == 2.0
    assert view["forecasts"][0]["eps_adjusted"] == pytest.approx(1.0)


def test_report_published_on_ex_rights_day_uses_previous_close_basis():
    rows = _report("机构A", date(2026, 6, 20), 40.0, {2026: 2.0})

    result = _aggregate_report_rows(rows, AS_OF, _cutoffs(), price_factors=TEN_FOR_TEN)

    assert result["target_price_min"] == pytest.approx(20.0)


def test_rolling_history_is_expressed_in_forward_adjusted_prices():
    rows = _report("机构A", date(2026, 5, 10), 40.0, {2026: 2.0, 2027: 2.4})

    history = build_a_stock_rolling_consensus_history(
        rows,
        [date(2026, 6, 1), date(2026, 7, 2)],
        disclosures=DISCLOSURES,
        price_factors=TEN_FOR_TEN,
    )

    # K 线是以最新复权因子为锚的前复权价，除权前后估值线都应落在同一口径 20 元上。
    assert [point["fair_value_lo"] for point in history] == pytest.approx([20.0, 20.0])


def test_valuation_history_map_restates_forward_filled_values_across_ex_rights():
    disclosures = [
        (date(2024, 10, 30), date(2024, 9, 30)),
        (date(2025, 4, 25), date(2025, 3, 31)),
        (date(2025, 8, 28), date(2025, 6, 30)),
        (date(2025, 10, 30), date(2025, 9, 30)),
    ]
    report_rows = _report("机构A", date(2025, 5, 10), 30.0, {2025: 1.0, 2026: 1.2}, symbol="600519.SH")
    requested = [date(2025, 6, 2), date(2025, 10, 29), date(2025, 11, 3)]
    market_rows = [{"ts_code": "600519.SH", "trade_date": day, "close": 20.0} for day in requested]
    factor_rows = [
        {"ts_code": "600519.SH", "trade_date": date(2024, 1, 2), "adj_factor": 1.0},
        {"ts_code": "600519.SH", "trade_date": date(2025, 11, 3), "adj_factor": 2.0},
    ]

    class FakeDb:
        def execute(self, statement, params=None):
            sql = str(statement)
            if "a_stock_adj_factor" in sql:
                return _Result(factor_rows)
            if "a_stock_income" in sql:
                return _Result(_disclosure_rows("600519.SH", disclosures))
            if "a_stock_report_rc" in sql:
                return _Result(report_rows)
            return _Result(market_rows)

    history = load_a_stock_consensus_valuation_history_map(FakeDb(), ["600519.SH"], requested)

    assert history[date(2025, 10, 29)]["600519.SH"]["fair_value_lo"] == pytest.approx(30.0)
    # 11-03 沿用 10-29 的估值，但当天 10 送 10 除权，旧估值要换到除权后口径。
    ffilled = history[date(2025, 11, 3)]["600519.SH"]
    assert ffilled["is_ffilled"] is True
    assert ffilled["fair_value_lo"] == pytest.approx(15.0)


def test_consensus_detail_shows_raw_and_restated_target_prices():
    rows = _report("机构A", date(2026, 5, 10), 40.0, {2026: 2.0})
    factor_rows = [
        {"ts_code": "600612.SH", "trade_date": date(2025, 1, 2), "adj_factor": 1.0},
        {"ts_code": "600612.SH", "trade_date": date(2026, 6, 20), "adj_factor": 2.0},
    ]

    detail = load_a_stock_consensus_detail(_detail_fake_db(rows, factor_rows=factor_rows), "600612.SH")

    organization = detail["organizations"][0]
    assert organization["target_price_low"] == pytest.approx(20.0)
    assert organization["target_price_low_raw"] == 40.0
    assert organization["price_adjustment"] == pytest.approx(0.5)
    assert detail["horizons"][0]["lo"] == pytest.approx(20.0)

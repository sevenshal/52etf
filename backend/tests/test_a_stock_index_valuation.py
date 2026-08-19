from datetime import date
from types import SimpleNamespace

import pytest

from src.core.services.a_stock_fear_greed_clone_service import AStockInnovation100FearGreedCloneCalculator
from src.core.services.a_stock_consensus import load_a_stock_consensus_valuation_map
from src.core.services.a_stock_index_valuation import (
    _build_valuation_position_fields,
    _percentile_rank,
    _valuation_position_label,
    calculate_weighted_index_valuation,
)


def _holding(symbol, weight):
    return SimpleNamespace(holding_symbol=symbol, weight=weight)


def _valuation(symbol, price, current_low, current_high, forward_low, forward_high, report_count=3):
    return SimpleNamespace(
        symbol=symbol,
        date=date(2026, 7, 30),
        last_price=price,
        fair_value_lo=current_low,
        fair_value_mid=(current_low + current_high) / 2,
        fair_value_hi=current_high,
        forward_next_fy_lo=forward_low,
        forward_next_fy_mid=(forward_low + forward_high) / 2,
        forward_next_fy_hi=forward_high,
        target_report_count=report_count,
    )


def test_calculate_weighted_index_valuation_normalizes_covered_weight():
    holdings = [_holding("AAA.SH", 0.6), _holding("BBB.SZ", 0.4)]
    valuations = {
        "AAA.SH": _valuation("AAA.SH", 10, 8, 12, 10, 14),
        "BBB.SZ": _valuation("BBB.SZ", 20, 10, 30, 20, 40),
    }

    result = calculate_weighted_index_valuation(
        index_level=1000,
        holdings=holdings,
        valuations=valuations,
    )

    assert result["fair_value_lo"] == pytest.approx(680)
    assert result["fair_value_hi"] == pytest.approx(1320)
    assert result["forward_next_fy_lo"] == pytest.approx(1000)
    assert result["forward_next_fy_hi"] == pytest.approx(1640)
    assert result["coverage_ratio"] == pytest.approx(1)
    assert result["rating"] == "合理"
    assert result["forward_rating"] == "合理"


def test_calculate_weighted_index_valuation_tracks_current_and_forward_coverage_separately():
    holdings = [_holding("AAA.SH", 0.7), _holding("BBB.SZ", 0.3)]
    incomplete = _valuation("BBB.SZ", 20, 10, 30, 20, 40)
    incomplete.forward_next_fy_hi = None

    result = calculate_weighted_index_valuation(
        index_level=1000,
        holdings=holdings,
        valuations={
            "AAA.SH": _valuation("AAA.SH", 10, 12, 14, 15, 17),
            "BBB.SZ": incomplete,
        },
    )

    assert result["coverage_ratio"] == pytest.approx(1)
    assert result["forward_coverage_ratio"] == pytest.approx(0.7)
    assert result["covered_count"] == 2
    assert result["forward_covered_count"] == 1
    assert result["fair_value_lo"] == pytest.approx(990)
    assert result["fair_value_hi"] == pytest.approx(1430)
    assert result["rating"] == "合理"
    assert result["forward_rating"] == "低估"


def test_calculate_weighted_index_valuation_downweights_thin_consensus():
    result = calculate_weighted_index_valuation(
        index_level=1000,
        holdings=[_holding("AAA.SH", 0.5), _holding("BBB.SZ", 0.5)],
        valuations={
            "AAA.SH": _valuation("AAA.SH", 10, 20, 20, 20, 20, report_count=1),
            "BBB.SZ": _valuation("BBB.SZ", 10, 10, 10, 10, 10, report_count=3),
        },
    )

    assert result["covered_weight"] == pytest.approx(1)
    assert result["effective_covered_weight"] == pytest.approx(2 / 3)
    assert result["fair_value_mid"] == pytest.approx(1250)


@pytest.mark.parametrize(
    ("position", "label"),
    [(90, "极度低估"), (70, "低估"), (50, "合理"), (30, "高估"), (10, "极度高估")],
)
def test_valuation_position_labels(position, label):
    assert _valuation_position_label(position) == label


def test_percentile_rank_uses_midrank_for_current_value():
    assert _percentile_rank([10, 20, 30, 40], 30) == pytest.approx(62.5)


def test_valuation_position_uses_504_days_and_keeps_252_day_comparison():
    result = _build_valuation_position_fields(range(1, 601), 600)

    assert result["valuation_history_days"] == 504
    assert result["valuation_history_252_days"] == 252
    assert result["valuation_position_is_full_window"] is True
    assert result["valuation_position_label"] == "极度低估"
    assert result["valuation_position_252_label"] == "极度低估"


def test_valuation_position_does_not_rate_fewer_than_120_days():
    result = _build_valuation_position_fields(range(1, 101), 100)

    assert result["valuation_history_days"] == 100
    assert result["valuation_position_pct"] is None
    assert result["valuation_position_label"] == "样本不足"
    assert result["valuation_position_252_pct"] is None
    assert result["valuation_position_252_label"] == "样本不足"


def test_load_holdings_on_or_before_uses_effective_snapshot(monkeypatch):
    calculator = AStockInnovation100FearGreedCloneCalculator("000985.SH")
    requested_day = date(2026, 7, 31)
    snapshot_day = date(2026, 6, 30)

    def fake_build(index):
        timestamp = index[0]
        return (
            {timestamp: [{"symbol": "600519.SH", "name": "贵州茅台", "weight": 0.05}]},
            {timestamp: snapshot_day},
        )

    monkeypatch.setattr(calculator, "_build_holdings_by_date", fake_build)

    holdings, holdings_as_of = calculator.load_holdings_on_or_before(requested_day)

    assert holdings == [{"symbol": "600519.SH", "name": "贵州茅台", "weight": 0.05}]
    assert holdings_as_of == snapshot_day


def test_load_a_stock_consensus_valuation_map_builds_forward_range():
    report_rows = [{
        "ts_code": "600519.SH",
        "report_date": date(2026, 7, 20),
        "report_title": "贵州茅台研报",
        "org_name": "测试券商",
        "author_name": "分析师",
        "quarter": "2026",
        "eps": 10,
        "pe": 20,
        "np": 100,
        "rating": "买入",
        "min_price": 120,
        "max_price": 140,
        "trade_date": date(2026, 7, 31),
        "close": 100,
    }, {
        "ts_code": "600519.SH",
        "report_date": date(2026, 7, 20),
        "report_title": "贵州茅台研报",
        "org_name": "测试券商",
        "author_name": "分析师",
        "quarter": "2027",
        "eps": 12,
        "pe": 18,
        "np": 120,
        "rating": "买入",
        "min_price": 120,
        "max_price": 140,
        "trade_date": date(2026, 7, 31),
        "close": 100,
    }]

    class Result:
        def __init__(self, rows=None, scalar=None):
            self._rows = rows or []
            self._scalar = scalar

        def scalar(self):
            return self._scalar

        def mappings(self):
            return self

        def all(self):
            return self._rows

    class FakeDb:
        def execute(self, statement, params=None):
            if params is None:
                return Result(scalar=date(2026, 7, 31))
            return Result(rows=report_rows)

    result = load_a_stock_consensus_valuation_map(FakeDb(), ["600519.SH"])

    assert result["600519.SH"]["fair_value_lo"] == pytest.approx(130)
    assert result["600519.SH"]["fair_value_mid"] == pytest.approx(130)
    assert result["600519.SH"]["fair_value_hi"] == pytest.approx(130)
    assert result["600519.SH"]["forward_next_fy_lo"] == pytest.approx(156)
    assert result["600519.SH"]["forward_next_fy_hi"] == pytest.approx(156)

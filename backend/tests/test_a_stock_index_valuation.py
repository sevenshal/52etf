from datetime import date
from types import SimpleNamespace

import pytest

from src.core.services.a_stock_index_valuation import calculate_weighted_index_valuation


def _holding(symbol, weight):
    return SimpleNamespace(holding_symbol=symbol, weight=weight)


def _valuation(symbol, price, current_low, current_high, forward_low, forward_high):
    return SimpleNamespace(
        symbol=symbol,
        date=date(2026, 7, 30),
        last_price=price,
        fair_value_lo=current_low,
        fair_value_hi=current_high,
        forward_next_fy_lo=forward_low,
        forward_next_fy_hi=forward_high,
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


def test_calculate_weighted_index_valuation_excludes_incomplete_rows_and_reports_coverage():
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

    assert result["coverage_ratio"] == pytest.approx(0.7)
    assert result["covered_count"] == 1
    assert result["fair_value_lo"] == pytest.approx(1200)
    assert result["fair_value_hi"] == pytest.approx(1400)
    assert result["rating"] == "低估"
    assert result["forward_rating"] == "低估"

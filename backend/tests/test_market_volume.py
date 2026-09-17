import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.services.market_volume import build_volume_compare, parse_trends_lines


def _lines(date, rows):
    return [f"{date} {minute},0.00,{close},{close},{close},100,{amount},{close}" for minute, close, amount in rows]


def test_parse_trends_lines_groups_by_date():
    parsed = parse_trends_lines(
        _lines("2026-09-16", [("09:30", 10.0, 1e8), ("09:31", 10.1, 2e8)])
        + _lines("2026-09-17", [("09:30", 10.2, 3e8)])
        + ["bad line"]
    )
    assert parsed["2026-09-16"] == [("09:30", 10.0, 1e8), ("09:31", 10.1, 2e8)]
    assert parsed["2026-09-17"] == [("09:30", 10.2, 3e8)]


def test_build_volume_compare_intraday_against_previous_day():
    sh = parse_trends_lines(
        _lines("2026-09-15", [("09:30", 99.0, 1e8), ("15:00", 100.0, 1e8)])
        + _lines("2026-09-16", [("09:30", 100.0, 4e8), ("09:31", 100.0, 6e8), ("09:32", 100.0, 2e8)])
        + _lines("2026-09-17", [("09:30", 101.0, 5e8), ("09:31", 99.0, 3e8)])
    )
    sz = parse_trends_lines(
        _lines("2026-09-15", [("09:30", 49.0, 1e8), ("15:00", 50.0, 1e8)])
        + _lines("2026-09-16", [("09:30", 50.0, 1e8), ("09:31", 50.0, 4e8), ("09:32", 50.0, 3e8)])
        + _lines("2026-09-17", [("09:30", 51.0, 5e8), ("09:31", 50.0, 1e8)])
    )

    result = build_volume_compare(sh, sz)

    assert result["target_date"] == "2026-09-17"
    assert result["compare_date"] == "2026-09-16"
    assert result["selectable_dates"] == ["2026-09-17", "2026-09-16"]
    assert result["is_intraday"] is True
    assert result["last_time"] == "09:31"
    assert result["target_total"] == 14.0
    assert result["compare_same_time_total"] == 15.0
    assert result["compare_full_total"] == 20.0
    assert result["diff"] == -1.0
    first, second, third = result["points"]
    assert first["deviation_pct"] == 100.0  # 10 亿 vs 5 亿
    assert first["sh_pct"] == 1.0
    assert first["sz_pct"] == 2.0
    assert second["target_cum"] == 14.0 and second["compare_cum"] == 15.0 and second["diff_cum"] == -1.0
    assert second["deviation_pct"] == -60.0
    assert third["target_cum"] is None and third["compare_cum"] == 20.0 and third["deviation_pct"] is None


def test_build_volume_compare_rejects_earliest_day_as_target():
    sh = parse_trends_lines(_lines("2026-09-16", [("09:30", 1, 1)]) + _lines("2026-09-17", [("09:30", 1, 1)]))
    with pytest.raises(ValueError):
        build_volume_compare(sh, sh, target_date="2026-09-16")

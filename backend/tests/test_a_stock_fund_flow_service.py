import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.services.a_stock_fund_flow import (
    _parse_hsgt_payload,
    _parse_rank_item,
    _parse_stock_flow_lines,
    normalize_stock_code,
)


def test_normalize_stock_code_accepts_common_formats():
    assert normalize_stock_code("600519") == "600519"
    assert normalize_stock_code("600519.SH") == "600519"
    assert normalize_stock_code("sz000001") == "000001"
    assert normalize_stock_code("BJ832000") == "832000"


def test_parse_rank_item_maps_main_order_flow_fields():
    row = {
        "f12": "600487",
        "f14": "亨通光电",
        "f2": 77.12,
        "f3": 9.42,
        "f62": 3976260608.0,
        "f184": 16.35,
        "f66": 4493251840.0,
        "f69": 18.48,
        "f72": -516991232.0,
        "f75": -2.13,
        "f78": -2096092224.0,
        "f81": -8.62,
        "f84": -1880168480.0,
        "f87": -7.73,
    }

    parsed = _parse_rank_item(row, 1)

    assert parsed["rank"] == 1
    assert parsed["code"] == "600487"
    assert parsed["main_net"] == 3976260608.0
    assert parsed["super_net_pct"] == 18.48
    assert parsed["large_net"] == -516991232.0
    assert parsed["small_net_pct"] == -7.73


def test_parse_hsgt_payload_aligns_arrays_and_totals():
    parsed = _parse_hsgt_payload({
        "time": ["09:30", "09:31", "09:32"],
        "hgt": [1.2, 1.5],
        "sgt": [-0.2, 0.3, 0.4],
    })

    assert parsed["points"] == [
        {"time": "09:30", "hgt_yi": 1.2, "sgt_yi": -0.2, "total_yi": 1.0},
        {"time": "09:31", "hgt_yi": 1.5, "sgt_yi": 0.3, "total_yi": 1.8},
        {"time": "09:32", "hgt_yi": None, "sgt_yi": 0.4, "total_yi": None},
    ]
    assert parsed["latest"]["time"] == "09:31"


def test_parse_stock_flow_daily_lines_maps_optional_percent_fields():
    rows = _parse_stock_flow_lines(
        [
            "2026-05-29,918724624.0,-2466552.0,-916258048.0,-19285088.0,"
            "938009712.0,9.15,-0.02,-9.13,-0.19,9.35,1326.00,3.92,0.00,0.00"
        ],
        daily=True,
    )

    assert rows == [
        {
            "date": "2026-05-29",
            "main_net": 918724624.0,
            "small_net": -2466552.0,
            "mid_net": -916258048.0,
            "large_net": -19285088.0,
            "super_net": 938009712.0,
            "main_net_pct": 9.15,
            "small_net_pct": -0.02,
            "mid_net_pct": -9.13,
            "large_net_pct": -0.19,
            "super_net_pct": 9.35,
            "close": 1326.0,
            "change_pct": 3.92,
        }
    ]

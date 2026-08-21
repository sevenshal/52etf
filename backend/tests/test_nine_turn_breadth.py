from datetime import date, timedelta

import pandas as pd

from src.core.services import nine_turn_breadth as service
from src.app.api.account import valid_admin_account
from src.app.api.nine_turn_breadth import router


def test_compute_stages_counts_consecutive_close_vs_four_days_ago():
    start = date(2026, 1, 1)
    frame = pd.DataFrame(
        {
            "ts_code": ["UP.SH"] * 13 + ["DOWN.SZ"] * 13,
            "trade_date": [start + timedelta(days=index) for index in range(13)] * 2,
            "close": list(range(1, 14)) + list(range(20, 7, -1)),
        }
    )

    result = service._compute_stages(frame)
    up = result[result["ts_code"] == "UP.SH"]
    down = result[result["ts_code"] == "DOWN.SZ"]

    assert up["high_stage"].tolist() == [0, 0, 0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert up["low_stage"].max() == 0
    assert down["low_stage"].tolist() == [0, 0, 0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert down["high_stage"].max() == 0


def test_classify_board_uses_selected_percentile_and_handles_both_signals():
    board = {
        "index_code": "TEST.CSI",
        "name": "测试板块",
        "high_share": 0.30,
        "low_share": 0.25,
        "high_history": [0.05, 0.10, 0.20],
        "low_history": [0.02, 0.08, 0.15],
    }

    result = service._classify_board(board, 90)

    assert result["signal"] == "both"
    assert result["high_triggered"] is True
    assert result["low_triggered"] is True
    assert "high_history" not in result
    assert "low_history" not in result


def test_overview_rejects_percentile_outside_supported_range():
    try:
        service.get_nine_turn_breadth_overview(percentile=49)
    except ValueError as exc:
        assert "50到99" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_detail_sorts_stage_two_or_above_before_neutral_members(monkeypatch):
    base = {
        "as_of_date": "2026-08-20",
        "boards": [{
            "index_code": "TEST.CSI", "name": "测试", "category": "行业主题",
            "eligible_members": 2, "high_count": 0, "low_count": 0,
            "high_share": 0.0, "low_share": 0.0,
            "high_history": [0.0] * 20, "low_history": [0.0] * 20, "history_days": 20,
        }],
        "details": {"TEST.CSI": [
            {"ts_code": "A.SH", "high_stage": 0, "low_stage": 0},
            {"ts_code": "B.SH", "high_stage": 3, "low_stage": 0},
        ]},
    }
    monkeypatch.setattr(service, "_get_base_snapshot", lambda force_refresh=False: base)

    result = service.get_nine_turn_breadth_detail("test.csi", 90)

    assert [item["ts_code"] for item in result["members"]] == ["B.SH", "A.SH"]


def test_all_nine_turn_routes_require_admin_account():
    routes = [route for route in router.routes if getattr(route, "path", "").startswith("/api/research/nine-turn-breadth")]

    assert {route.path for route in routes} == {
        "/api/research/nine-turn-breadth/overview",
        "/api/research/nine-turn-breadth/boards/{index_code}/detail",
    }
    for route in routes:
        dependency_calls = {dependency.call for dependency in route.dependant.dependencies}
        assert valid_admin_account in dependency_calls

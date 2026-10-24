"""A 股指数估值基础数据回补任务测试。"""

from datetime import date
from unittest.mock import patch

from src.robot import scheduled_tasks
from src.robot.a_stock_base_data_config import A_STOCK_INDEX_FEAR_GREED_TARGETS


def test_a_stock_etf_valuation_index_backfill_covers_all_configured_indexes():
    assert len(A_STOCK_INDEX_FEAR_GREED_TARGETS) > 8
    assert "a_stock_etf_valuation_index_backfill" in scheduled_tasks.START_DATE_RUN_TASK_KEYS
    assert "a_stock_etf_valuation_index_backfill" in scheduled_tasks.SYMBOLS_RUN_TASK_KEYS
    task = scheduled_tasks.ScheduledTaskManager().task_definitions[
        "a_stock_etf_valuation_index_backfill"
    ]
    assert task.default_enabled is False
    assert {item.key for item in task.parameter_schema} == {"start_date", "end_date", "symbols"}


def test_a_stock_etf_valuation_index_backfill_syncs_indexes_then_refreshes_valuation():
    result = {
        "start_date": "2024-08-01",
        "end_date": "2026-10-21",
        "index_daily": {"saved_rows": 1200, "errors": []},
        "index_weight": {"saved_rows": 3000, "errors": []},
    }
    with patch(
        "src.robot.a_stock_base_data_sync.sync_a_stock_index_history",
        return_value=result,
    ) as sync, patch(
        "src.core.services.a_stock_index_valuation.refresh_a_stock_index_valuations",
        return_value={"saved": len(A_STOCK_INDEX_FEAR_GREED_TARGETS), "errors": []},
    ) as refresh:
        message = scheduled_tasks._run_a_stock_etf_valuation_index_backfill(
            start_date="2024-08-01",
            end_date="2026-10-21",
        )

    sync.assert_called_once_with(
        [item["symbol"] for item in A_STOCK_INDEX_FEAR_GREED_TARGETS if not item["symbol"].endswith(".CN")],
        date(2024, 8, 1),
        date(2026, 10, 21),
    )
    refresh.assert_called_once_with([item["symbol"] for item in A_STOCK_INDEX_FEAR_GREED_TARGETS])
    assert "daily_saved=1200" in message
    assert "weight_saved=3000" in message
    assert f"valuation_saved={len(A_STOCK_INDEX_FEAR_GREED_TARGETS)}" in message

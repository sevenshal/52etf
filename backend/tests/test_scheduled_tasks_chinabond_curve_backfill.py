"""中债曲线定向回补任务测试。"""

from datetime import date
from unittest.mock import patch

from src.robot import scheduled_tasks


def test_chinabond_curve_backfill_is_manual_date_task():
    assert "a_stock_chinabond_curve_backfill" in scheduled_tasks.START_DATE_RUN_TASK_KEYS
    task = scheduled_tasks.ScheduledTaskManager().task_definitions[
        "a_stock_chinabond_curve_backfill"
    ]
    assert task.default_enabled is False
    assert {item.key for item in task.parameter_schema} == {"start_date", "end_date"}


def test_chinabond_curve_backfill_only_invokes_curve_sync():
    result = {
        "start_date": "2019-01-01",
        "end_date": "2026-10-17",
        "trading_days": 1900,
        "refresh_dates": 1850,
        "saved_rows": 130000,
        "errors": [],
    }
    with patch(
        "src.robot.a_stock_base_data_sync.sync_a_stock_chinabond_yield_curves",
        return_value=result,
    ) as sync:
        message = scheduled_tasks._run_a_stock_chinabond_curve_backfill(
            start_date="2019-01-01",
            end_date="2026-10-17",
        )

    sync.assert_called_once_with(start_date=date(2019, 1, 1), end_date=date(2026, 10, 17))
    assert "saved_rows=130000" in message
    assert "errors=0" in message

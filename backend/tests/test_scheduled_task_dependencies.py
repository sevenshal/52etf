from dataclasses import replace
from unittest.mock import patch

from src.core.database import ScheduledTaskConfig, get_db_ctx
from src.robot.scheduled_tasks import ScheduledTaskManager


def _prepare_manager() -> ScheduledTaskManager:
    manager = ScheduledTaskManager()
    manager.ensure_task_configs()
    return manager


def test_task_response_includes_declared_dependencies_and_their_latest_status():
    manager = _prepare_manager()

    with get_db_ctx() as db:
        upstream = db.query(ScheduledTaskConfig).filter(
            ScheduledTaskConfig.task_key == "a_stock_base_data_sync"
        ).one()
        upstream.last_run_status = "SUCCESS"

    task = manager.get_task("a_stock_innovation100_rebuild")

    assert task["dependencies"] == [{
        "task_key": "a_stock_base_data_sync",
        "name": "A股基础数据同步",
        "last_run_status": "SUCCESS",
        "last_run_finished_at": None,
    }]


def test_failed_dependency_marks_downstream_failed_without_running_runner():
    manager = _prepare_manager()
    task_key = "a_stock_innovation100_rebuild"
    runner_called = False

    def runner_should_not_run(**_kwargs):
        nonlocal runner_called
        runner_called = True

    manager.task_definitions[task_key] = replace(
        manager.task_definitions[task_key],
        runner=runner_should_not_run,
    )
    with get_db_ctx() as db:
        upstream = db.query(ScheduledTaskConfig).filter(
            ScheduledTaskConfig.task_key == "a_stock_base_data_sync"
        ).one()
        upstream.last_run_status = "FAILED"

    with patch("src.robot.scheduled_tasks.send_alert_email"), patch(
        "src.robot.scheduled_tasks.publish_event"
    ):
        manager._execute_task(
            manager.task_definitions[task_key],
            trigger_source="manual",
            triggered_by="test-user",
            runner_kwargs={},
        )

    assert runner_called is False
    with get_db_ctx() as db:
        downstream = db.query(ScheduledTaskConfig).filter(
            ScheduledTaskConfig.task_key == task_key
        ).one()
        assert downstream.last_run_status == "FAILED"
        assert "依赖任务最新执行失败" in downstream.last_run_message
        assert "a_stock_base_data_sync" in downstream.last_run_message

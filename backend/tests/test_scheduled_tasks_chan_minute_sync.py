from datetime import date

from src.app.api import scheduled_tasks as scheduled_tasks_api
from src.robot import scheduled_tasks


def test_chan_minute_sync_exposes_manual_start_date_option():
    assert "chan_minute_sync" in scheduled_tasks.START_DATE_RUN_TASK_KEYS
    assert "sw_minute_sync" in scheduled_tasks.START_DATE_RUN_TASK_KEYS
    task = scheduled_tasks.ScheduledTaskManager().task_definitions["chan_minute_sync"]
    assert "手动增量" in task.description
    assert "指定开始日期" in task.description


def test_chan_minute_runner_forwards_selected_start_date_as_full_sync(monkeypatch):
    captured = {}

    def fake_start(cls, **kwargs):
        captured.update(kwargs)
        return {"status": "SUCCESS", "saved_rows": 0, "errors": []}

    monkeypatch.setattr(
        "src.core.services.chan_minute_sync.ChanMinuteSyncManager.start",
        classmethod(fake_start),
    )
    monkeypatch.setattr(
        "src.core.services.szdt_etf_volume.sync_registered_etf_minutes",
        lambda: {"saved_rows": 0},
    )

    scheduled_tasks._run_chan_minute_sync(start_date="2020-01-02")

    assert captured == {
        "full": True,
        "trading_days": 128,
        "start_date": date(2020, 1, 2),
    }


def test_manual_minute_task_without_date_uses_incremental_mode(monkeypatch):
    calls = []

    class FakeManager:
        def trigger_task(self, **kwargs):
            calls.append(kwargs)

        def get_task(self, task_key):
            return {"task_key": task_key}

    monkeypatch.setattr(scheduled_tasks_api, "scheduled_task_manager", FakeManager())

    scheduled_tasks_api.run_scheduled_task_now(
        "chan_minute_sync",
        scheduled_tasks_api.ScheduledTaskRunRequest(),
        account_id="admin",
    )
    scheduled_tasks_api.run_scheduled_task_now(
        "chan_minute_sync",
        scheduled_tasks_api.ScheduledTaskRunRequest(start_date="2020-01-02"),
        account_id="admin",
    )
    scheduled_tasks_api.run_scheduled_task_now(
        "sw_minute_sync",
        scheduled_tasks_api.ScheduledTaskRunRequest(),
        account_id="admin",
    )
    scheduled_tasks_api.run_scheduled_task_now(
        "sw_minute_sync",
        scheduled_tasks_api.ScheduledTaskRunRequest(start_date="2020-01-02"),
        account_id="admin",
    )

    assert calls == [
        {
            "task_key": "chan_minute_sync",
            "trigger_source": "manual",
            "triggered_by": "admin",
            "background": True,
            "full": False,
        },
        {
            "task_key": "chan_minute_sync",
            "trigger_source": "manual",
            "triggered_by": "admin",
            "background": True,
            "start_date": "2020-01-02",
            "full": True,
        },
        {
            "task_key": "sw_minute_sync",
            "trigger_source": "manual",
            "triggered_by": "admin",
            "background": True,
            "full": False,
        },
        {
            "task_key": "sw_minute_sync",
            "trigger_source": "manual",
            "triggered_by": "admin",
            "background": True,
            "start_date": "2020-01-02",
            "full": True,
        },
    ]

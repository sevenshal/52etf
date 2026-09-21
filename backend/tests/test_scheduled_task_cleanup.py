import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.database import ScheduledTaskConfig, get_db_ctx
from src.robot.scheduled_tasks import DEPRECATED_TASK_KEYS, ScheduledTaskManager


def test_removed_sw_industry_sync_config_is_cleaned_up():
    """申万行业分类同步已并入 A股基础数据同步：存量库里残留的配置行要在启动时删掉，
    否则前端任务列表（直接读配置表）会一直显示这个已经不存在的任务。"""
    assert "sw_industry_sync" in DEPRECATED_TASK_KEYS

    with get_db_ctx() as db:
        db.query(ScheduledTaskConfig).filter(ScheduledTaskConfig.task_key == "sw_industry_sync").delete()
        db.add(ScheduledTaskConfig(
            task_key="sw_industry_sync", name="申万行业分类同步", enabled=True, schedule_time="08:40",
        ))

    manager = ScheduledTaskManager()
    manager.ensure_task_configs()

    with get_db_ctx() as db:
        assert db.query(ScheduledTaskConfig).filter(ScheduledTaskConfig.task_key == "sw_industry_sync").count() == 0
    assert all(task["task_key"] != "sw_industry_sync" for task in manager.list_tasks())


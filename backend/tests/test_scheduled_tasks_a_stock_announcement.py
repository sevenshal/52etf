"""`_run_a_stock_announcement_sync` 的 tushare 增量分支回归测试。

背景：东财公告落库后顺手拉的 tushare 预告/快报是「尽力而为」的，失败只应该降级成
结果消息里的一句 failed，绝不能把整条任务带崩。曾经因为分支里用了未定义的 ``logger``，
tushare 抛异常时会被 ``NameError: name 'logger' is not defined`` 顶掉，任务结果只剩这句
无用的报错，真实失败原因（鉴权/权限/接口变更）全被吞掉。
"""

from unittest.mock import patch

from src.robot import scheduled_tasks


def _fake_announcement_result():
    return {
        "start_date": "2026-10-08",
        "end_date": "2026-10-15",
        "fetched_rows": 128,
        "saved_rows": 120,
        "kinds": {"预告": 20, "快报": 5, "定期报告": 95},
        "seconds": 3.2,
    }


def _run_announcement_sync(tushare_runners):
    with patch.object(
        scheduled_tasks, "_is_china_trading_day", return_value=True
    ), patch(
        "src.core.services.a_stock_announcement.sync_a_stock_announcements",
        return_value=_fake_announcement_result(),
    ), patch(
        "src.robot.a_stock_base_data_sync.sync_a_stock_forecast_data",
        side_effect=tushare_runners["forecast"],
    ), patch(
        "src.robot.a_stock_base_data_sync.sync_a_stock_express_data",
        side_effect=tushare_runners["express"],
    ):
        return scheduled_tasks._run_a_stock_announcement_sync()


def test_tushare_failure_is_reported_without_breaking_task():
    """tushare 增量失败时任务仍返回消息，且带上真实异常类型与原因。"""

    def boom_forecast():
        raise RuntimeError("抱歉，您没有访问该接口的权限")

    message = _run_announcement_sync(
        {"forecast": boom_forecast, "express": lambda: {"saved_rows": 5, "fetched_rows": 5}}
    )

    assert message.startswith("业绩公告流同步完成: window=2026-10-08~2026-10-15")
    assert "tushare预告 failed=RuntimeError: 抱歉，您没有访问该接口的权限" in message
    assert "tushare快报 saved=5 fetched=5" in message
    assert "logger" not in message


def test_both_tushare_steps_failing_still_returns_message():
    def boom():
        raise ValueError("tushare token 未配置")

    message = _run_announcement_sync({"forecast": boom, "express": boom})

    assert message.count("failed=ValueError: tushare token 未配置") == 2
    assert "saved=120" in message


def test_tushare_success_path_reports_saved_and_fetched():
    message = _run_announcement_sync(
        {
            "forecast": lambda: {"saved_rows": 11, "fetched_rows": 12},
            "express": lambda: {"saved_rows": 3, "fetched_rows": 3},
        }
    )

    assert "tushare预告 saved=11 fetched=12" in message
    assert "tushare快报 saved=3 fetched=3" in message

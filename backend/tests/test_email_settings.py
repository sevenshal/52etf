from src.core.services.email_settings import EMAIL_SCENARIOS, EMAIL_SCENARIO_BY_KEY
from src.core import utils


def test_system_startup_scenario_is_registered():
    assert "system_startup" in EMAIL_SCENARIO_BY_KEY
    scenario = EMAIL_SCENARIO_BY_KEY["system_startup"]
    assert scenario["name"] == "系统启动通知"
    assert scenario["category"] == "系统"
    # 场景列表也要包含它，邮箱管理页会按此渲染
    keys = [item["key"] for item in EMAIL_SCENARIOS]
    assert "system_startup" in keys


def test_startup_notification_uses_system_startup_scenario(monkeypatch):
    # 回归：后端启动通知必须走 system_startup 场景，这样邮箱管理页里
    # 配置的专用邮箱 / 默认邮箱才会生效。
    calls = []

    def fake_send_configured_email(scenario_key, subject, body, **kwargs):
        calls.append({"scenario_key": scenario_key, "subject": subject, "body": body})
        return True

    monkeypatch.setattr(utils, "send_configured_email", fake_send_configured_email)

    assert utils.send_system_startup_email() is True

    assert len(calls) == 1
    assert calls[0]["scenario_key"] == "system_startup"
    assert "52etf" in calls[0]["subject"]

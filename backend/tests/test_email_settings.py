from datetime import date

from src.core.services.email_settings import EMAIL_SCENARIOS, EMAIL_SCENARIO_BY_KEY
from src.core import utils
from src.robot.a_stock_fear_strategy_trader import AStockFearStrategyTrader


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


def test_a_stock_fear_rebalance_notification_is_registered_and_includes_signal_details(monkeypatch):
    scenario = EMAIL_SCENARIO_BY_KEY["a_stock_fear_strategy_rebalance_signal"]
    assert scenario["name"] == "A股 ETF 情绪量能策略调仓提醒"
    assert scenario["category"] == "交易"
    assert "a_stock_fear_strategy_rebalance_signal" in {
        item["key"] for item in EMAIL_SCENARIOS
    }

    calls = []

    def fake_send_configured_email(scenario_key, subject, body, **kwargs):
        calls.append({"scenario_key": scenario_key, "subject": subject, "body": body})
        return True

    monkeypatch.setattr(
        "src.robot.a_stock_fear_strategy_trader.send_configured_email",
        fake_send_configured_email,
    )
    trader = AStockFearStrategyTrader()
    trader._send_rebalance_notification(
        trigger_source="auto",
        config_id=7,
        masked_account_id="***1234",
        symbol="510880.SH",
        action="BUY",
        trade_message="订单已同步",
        signal_date=date(2026, 10, 22),
        fear_score=22.5,
        fear_label="上证红利 指数贪恐",
        volume_ratio=1.8,
        price=1.2345,
        quantity=1000,
        position_ratio_before=0,
        position_ratio_after=99.9,
        fear_volume_date=date(2026, 10, 20),
        fear_volume_symbol="510880.SH",
        signal_close=1.2,
    )

    assert len(calls) == 1
    assert calls[0]["scenario_key"] == "a_stock_fear_strategy_rebalance_signal"
    assert "买入" in calls[0]["subject"]
    assert "订单已同步" in calls[0]["body"]
    assert "恐贪分数: 22.50" in calls[0]["body"]
    assert "恐慌放量日: 2026-10-20" in calls[0]["body"]
    assert "恐慌放量标的: 510880.SH" in calls[0]["body"]
    assert "恐慌放量日量能（量比）: 1.8000" in calls[0]["body"]
    assert "信号日: 2026-10-22" in calls[0]["body"]
    assert "信号日收盘价: 1.2000" in calls[0]["body"]
    assert "调仓原因: 订单已同步" in calls[0]["body"]

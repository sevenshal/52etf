import re
from datetime import datetime
from typing import Dict, List, Optional

from ..database import EmailRecipientConfig, SessionLocal


DEFAULT_EMAIL_SCENARIO_KEY = "__default__"
GENERIC_ALERT_SCENARIO_KEY = "system_alert"

EMAIL_SCENARIOS = [
    {
        "key": GENERIC_ALERT_SCENARIO_KEY,
        "name": "通用系统告警",
        "category": "系统",
        "description": "未显式归类的后端告警邮件。",
    },
    {
        "key": "system_startup",
        "name": "系统启动通知",
        "category": "系统",
        "description": "后端服务每次启动成功时发送的通知。",
    },
    {
        "key": "api_service_error",
        "name": "API 服务异常",
        "category": "系统",
        "description": "FastAPI 全局异常处理捕获到接口报错。",
    },
    {
        "key": "robot_main_loop_error",
        "name": "自动化主循环异常",
        "category": "系统",
        "description": "机器人主循环执行系统任务时发生异常。",
    },
    {
        "key": "scheduled_task_failure",
        "name": "定时任务失败",
        "category": "系统",
        "description": "系统级定时任务执行失败。",
    },
    {
        "key": "portfolio_copy_trading_error",
        "name": "自动化跟单交易异常",
        "category": "交易",
        "description": "组合跟单调仓、队列或 Cron 检查异常。",
    },
    {
        "key": "szdt_us_trading_error",
        "name": "SZDT 美股自动交易异常",
        "category": "交易",
        "description": "SZDT 美股自动交易账号处理或主循环异常。",
    },
    {
        "key": "lev_etf_trading_error",
        "name": "杠杆 ETF 策略异常",
        "category": "交易",
        "description": "杠杆 ETF 自动交易主循环异常。",
    },
    {
        "key": "soxl_fear_strategy_error",
        "name": "SOXL 情绪量能策略异常",
        "category": "交易",
        "description": "SOXL 情绪量能自动交易执行或主循环异常。",
    },
    {
        "key": "soxl_fear_strategy_rebalance_signal",
        "name": "SOXL 情绪量能策略调仓提醒",
        "category": "交易",
        "description": "SOXL 情绪量能策略产生买入或卖出调仓动作。",
    },
    {
        "key": "factor_live_trading_error",
        "name": "因子线上交易异常",
        "category": "交易",
        "description": "因子线上交易信号生成或执行失败。",
    },
    {
        "key": "external_trading_connection_alert",
        "name": "外部交易连接告警",
        "category": "外部交易",
        "description": "PTrade/外部交易账户长连接异常。",
    },
    {
        "key": "external_trading_fee_reconcile_alert",
        "name": "外部交易费用对账告警",
        "category": "外部交易",
        "description": "外部交易账号未收到交割单费用对账推送。",
    },
    {
        "key": "xueqiu_token_freshness_alert",
        "name": "雪球Token更新告警",
        "category": "外部交易",
        "description": "雪球 xq_a_token 超过24小时未更新或未配置。",
    },
    {
        "key": "xueqiu_top_holdings_report",
        "name": "雪球持仓报告",
        "category": "报告",
        "description": "雪球年榜组合持仓权重或自动调仓报告。",
    },
    {
        "key": "xueqiu_top_holdings_failure",
        "name": "雪球持仓任务失败",
        "category": "报告",
        "description": "雪球年榜组合持仓任务执行失败。",
    },
]

EMAIL_SCENARIO_BY_KEY = {item["key"]: item for item in EMAIL_SCENARIOS}
EMAIL_ADDRESS_PATTERN = re.compile(r"^[^@\s,;，；]+@[^@\s,;，；]+\.[^@\s,;，；]+$")


def normalize_email_recipients(value: Optional[str]) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None

    parts = [
        item.strip()
        for item in re.split(r"[,;，；\s]+", text)
        if item.strip()
    ]
    normalized = []
    seen = set()
    for item in parts:
        if not EMAIL_ADDRESS_PATTERN.fullmatch(item):
            raise ValueError(f"邮箱格式无效: {item}")
        dedupe_key = item.lower()
        if dedupe_key not in seen:
            normalized.append(item)
            seen.add(dedupe_key)
    return ", ".join(normalized) if normalized else None


def get_email_scenarios() -> List[Dict[str, str]]:
    return [dict(item) for item in EMAIL_SCENARIOS]


def _get_config_map(db) -> Dict[str, EmailRecipientConfig]:
    rows = db.query(EmailRecipientConfig).all()
    return {row.scenario_key: row for row in rows}


def _upsert_config(db, scenario_key: str, recipient_email: str, updated_by: Optional[str]):
    now = datetime.now()
    config = (
        db.query(EmailRecipientConfig)
        .filter(EmailRecipientConfig.scenario_key == scenario_key)
        .first()
    )
    if config:
        config.recipient_email = recipient_email
        config.updated_by = updated_by
        config.updated_at = now
        return config

    config = EmailRecipientConfig(
        scenario_key=scenario_key,
        recipient_email=recipient_email,
        updated_by=updated_by,
        created_at=now,
        updated_at=now,
    )
    db.add(config)
    return config


def _delete_config(db, scenario_key: str):
    config = (
        db.query(EmailRecipientConfig)
        .filter(EmailRecipientConfig.scenario_key == scenario_key)
        .first()
    )
    if config:
        db.delete(config)


def build_email_settings_response(config_map: Dict[str, EmailRecipientConfig]) -> Dict:
    default_email = None
    default_config = config_map.get(DEFAULT_EMAIL_SCENARIO_KEY)
    if default_config and default_config.recipient_email:
        default_email = default_config.recipient_email

    scenarios = []
    for scenario in EMAIL_SCENARIOS:
        config = config_map.get(scenario["key"])
        recipient_email = config.recipient_email if config and config.recipient_email else None
        effective_email = recipient_email or default_email
        scenarios.append(
            {
                **scenario,
                "recipient_email": recipient_email,
                "effective_email": effective_email,
                "uses_default": not bool(recipient_email) and bool(default_email),
                "updated_by": config.updated_by if config else None,
                "updated_at": config.updated_at.isoformat() if config and config.updated_at else None,
            }
        )

    return {
        "default_email": default_email,
        "scenarios": scenarios,
    }


def get_email_settings() -> Dict:
    db = SessionLocal()
    try:
        return build_email_settings_response(_get_config_map(db))
    finally:
        db.close()


def update_email_settings(
    *,
    default_email: Optional[str],
    scenario_emails: Dict[str, Optional[str]],
    updated_by: Optional[str],
) -> Dict:
    unknown_keys = set(scenario_emails.keys()) - set(EMAIL_SCENARIO_BY_KEY.keys())
    if unknown_keys:
        raise ValueError(f"未知邮件场景: {', '.join(sorted(unknown_keys))}")

    normalized_default = normalize_email_recipients(default_email)
    normalized_scenarios = {
        key: normalize_email_recipients(value)
        for key, value in scenario_emails.items()
    }

    db = SessionLocal()
    try:
        if normalized_default:
            _upsert_config(db, DEFAULT_EMAIL_SCENARIO_KEY, normalized_default, updated_by)
        else:
            _delete_config(db, DEFAULT_EMAIL_SCENARIO_KEY)

        for key, value in normalized_scenarios.items():
            if value:
                _upsert_config(db, key, value, updated_by)
            else:
                _delete_config(db, key)

        db.commit()
        return build_email_settings_response(_get_config_map(db))
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def resolve_email_recipient(scenario_key: Optional[str]) -> Optional[str]:
    db = SessionLocal()
    try:
        config_map = _get_config_map(db)
        key = scenario_key or GENERIC_ALERT_SCENARIO_KEY
        scenario_config = config_map.get(key)
        if scenario_config and scenario_config.recipient_email:
            return scenario_config.recipient_email

        default_config = config_map.get(DEFAULT_EMAIL_SCENARIO_KEY)
        if default_config and default_config.recipient_email:
            return default_config.recipient_email
        return None
    finally:
        db.close()

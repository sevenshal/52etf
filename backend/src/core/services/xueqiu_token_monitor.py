from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from ..database import SystemServiceCredential, get_db_ctx
from ..utils import mask_account_id, send_alert_email


CHINA_TZ = ZoneInfo("Asia/Shanghai")
XUEQIU_TOKEN_MAX_AGE_HOURS = 24
XUEQIU_TOKEN_FRESHNESS_ALERT_SCENARIO = "xueqiu_token_freshness_alert"


@dataclass(frozen=True)
class XueqiuTokenConfigSnapshot:
    account_id: Optional[str]
    xueqiu_cookie: Optional[str]
    updated_at: Optional[datetime]


def _to_naive_china_datetime(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(CHINA_TZ).replace(tzinfo=None)
    return value


def _select_latest_xueqiu_token_config(configs: List[Any]) -> Optional[Any]:
    candidates = [
        config
        for config in configs
        if "xq_a_token=" in str(getattr(config, "xueqiu_cookie", "") or "")
    ]
    if not candidates:
        return None

    return sorted(
        candidates,
        key=lambda config: _to_naive_china_datetime(getattr(config, "updated_at", None)) or datetime.min,
        reverse=True,
    )[0]


def evaluate_xueqiu_token_freshness(
    configs: List[Any],
    *,
    now: Optional[datetime] = None,
    max_age_hours: int = XUEQIU_TOKEN_MAX_AGE_HOURS,
) -> Dict[str, Any]:
    check_at = _to_naive_china_datetime(now or datetime.now(CHINA_TZ)) or datetime.now()
    config = _select_latest_xueqiu_token_config(configs)
    if not config:
        return {
            "status": "MISSING",
            "checked_at": check_at,
            "account_id": None,
            "updated_at": None,
            "age_hours": None,
            "max_age_hours": max_age_hours,
        }

    updated_at = _to_naive_china_datetime(getattr(config, "updated_at", None))
    age_hours = None
    if updated_at is not None:
        age_hours = round((check_at - updated_at).total_seconds() / 3600.0, 2)
    status = "OK" if age_hours is not None and age_hours <= max_age_hours else "STALE"
    return {
        "status": status,
        "checked_at": check_at,
        "account_id": getattr(config, "account_id", None),
        "updated_at": updated_at,
        "age_hours": age_hours,
        "max_age_hours": max_age_hours,
    }


def format_xueqiu_token_freshness_message(result: Dict[str, Any]) -> str:
    updated_at = result.get("updated_at")
    updated_at_text = updated_at.strftime("%Y-%m-%d %H:%M:%S") if updated_at else "无"
    age_hours = result.get("age_hours")
    age_text = f"{age_hours:.2f}h" if isinstance(age_hours, (int, float)) else "未知"
    account_text = mask_account_id(str(result.get("account_id") or ""))
    return (
        f"status={result.get('status')} "
        f"account={account_text or '-'} "
        f"updated_at={updated_at_text} "
        f"age={age_text} "
        f"max_age={result.get('max_age_hours')}h"
    )


def send_xueqiu_token_freshness_alert(result: Dict[str, Any]) -> bool:
    status = result.get("status")
    subject = (
        "雪球 token 未配置告警"
        if status == "MISSING"
        else f"雪球 token 超过{result.get('max_age_hours')}小时未更新"
    )
    body = (
        "雪球 token 新鲜度检查发现异常。\n\n"
        f"{format_xueqiu_token_freshness_message(result)}\n\n"
        "请检查 android_monitor 的雪球登录态和 token 上报链路，确认手机端已重新登录雪球并成功上报。"
    )
    return send_alert_email(
        subject,
        body,
        scenario_key=XUEQIU_TOKEN_FRESHNESS_ALERT_SCENARIO,
    )


def send_xueqiu_token_login_missing_alert(
    *,
    account_id: Optional[str],
    source: Optional[str] = None,
    status_message: Optional[str] = None,
) -> bool:
    account_text = mask_account_id(str(account_id or ""))
    body = (
        "android_monitor 上报雪球登录态异常，未读取到可用 xq_a_token。\n\n"
        f"account={account_text or '-'}\n"
        f"source={source or '-'}\n"
        f"message={status_message or '-'}\n\n"
        "请检查手机端雪球是否仍保持登录，并确认 android_monitor 能打开雪球页面读取 Cookie。"
    )
    return send_alert_email(
        "雪球登录态失效告警",
        body,
        scenario_key=XUEQIU_TOKEN_FRESHNESS_ALERT_SCENARIO,
    )


def process_xueqiu_token_freshness_check_for_robot(max_age_hours: int = XUEQIU_TOKEN_MAX_AGE_HOURS) -> str:
    with get_db_ctx() as db:
        row = db.get(SystemServiceCredential, "snowball")
        configs = []
        if row and row.cookie:
            configs.append(
                XueqiuTokenConfigSnapshot(
                    account_id="system:snowball",
                    xueqiu_cookie=row.cookie,
                    updated_at=row.updated_at,
                )
            )

    result = evaluate_xueqiu_token_freshness(configs, max_age_hours=max_age_hours)
    message = format_xueqiu_token_freshness_message(result)
    if result["status"] != "OK":
        sent = send_xueqiu_token_freshness_alert(result)
        return f"雪球token更新检查 {message} alert_sent={sent}"
    return f"雪球token更新检查 {message}"

import logging
from datetime import date, datetime, time as dtime
from typing import Dict, Optional, Set, Tuple
from zoneinfo import ZoneInfo

from ..database import ExternalTradingAccount, get_db_ctx
from ..utils import send_alert_email
from .external_trading import external_trading_hub

logger = logging.getLogger(__name__)

CHINA_TZ = ZoneInfo("Asia/Shanghai")
A_SHARE_PREOPEN_CHECK_START = dtime(8, 30)
A_SHARE_OPEN = dtime(9, 30)
A_SHARE_MORNING_CLOSE = dtime(11, 30)
A_SHARE_AFTERNOON_OPEN = dtime(13, 0)
A_SHARE_CLOSE = dtime(15, 0)
EXTERNAL_TRADING_STALE_SECONDS = 90

_preopen_alerted_keys: Set[Tuple[str, int]] = set()
_market_alerted_keys: Set[Tuple[str, str, int]] = set()
_last_health_by_account: Dict[int, bool] = {}
_trading_day_cache: Dict[date, bool] = {}
_state_date: Optional[date] = None


def _china_now() -> datetime:
    return datetime.now(CHINA_TZ)


def _to_china_datetime(value) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        if value.tzinfo:
            return value.astimezone(CHINA_TZ)
        return value.replace(tzinfo=CHINA_TZ)
    return None


def _is_china_trading_day(check_date: date) -> bool:
    if check_date in _trading_day_cache:
        return _trading_day_cache[check_date]
    if check_date.weekday() >= 5:
        _trading_day_cache[check_date] = False
        return False
    try:
        from .tushare import TushareService

        calendar = TushareService.get_instance().get_trade_calendar_frame(check_date, check_date)
        if not calendar.empty:
            row = calendar.iloc[0]
            is_open = int(row.get("is_open") or 0) == 1
            _trading_day_cache[check_date] = is_open
            return is_open
    except Exception as exc:
        logger.warning("A-share trading calendar check failed for %s: %s", check_date, exc)
    _trading_day_cache[check_date] = True
    return True


def _get_market_session(now: datetime) -> Optional[str]:
    current_time = now.astimezone(CHINA_TZ).time() if now.tzinfo else now.time()
    if A_SHARE_OPEN <= current_time <= A_SHARE_MORNING_CLOSE:
        return "morning"
    if A_SHARE_AFTERNOON_OPEN <= current_time <= A_SHARE_CLOSE:
        return "afternoon"
    return None


def _is_preopen_check_window(now: datetime) -> bool:
    current_time = now.astimezone(CHINA_TZ).time() if now.tzinfo else now.time()
    return A_SHARE_PREOPEN_CHECK_START <= current_time < A_SHARE_OPEN


def _reset_daily_state(today: date):
    global _state_date
    if _state_date == today:
        return
    _state_date = today
    _preopen_alerted_keys.clear()
    _market_alerted_keys.clear()
    _last_health_by_account.clear()


def _format_dt(value) -> str:
    local_value = _to_china_datetime(value)
    return local_value.strftime("%Y-%m-%d %H:%M:%S") if local_value else "-"


def _get_connection_health(account_id: int) -> Dict:
    status = external_trading_hub.get_status(account_id)
    connected = bool(status.get("connected"))
    last_seen_at = _to_china_datetime(status.get("last_seen_at"))
    now = _china_now()
    stale_seconds = None
    stale = False
    if connected:
        if last_seen_at:
            stale_seconds = max((now - last_seen_at).total_seconds(), 0)
            stale = stale_seconds > EXTERNAL_TRADING_STALE_SECONDS
        else:
            stale = True

    healthy = connected and not stale
    reason = "连接正常"
    if not connected:
        reason = "长连接未连接"
    elif stale:
        reason = f"心跳超时 {int(stale_seconds or 0)} 秒"

    return {
        "healthy": healthy,
        "connected": connected,
        "stale": stale,
        "stale_seconds": stale_seconds,
        "reason": reason,
        "status": status,
    }


def _send_external_trading_connection_alert(
    account: Dict,
    reason: str,
    scene: str,
    now: datetime,
    runtime_status: Dict,
):
    subject = f"外部交易账户连接告警: {account.get('name')}"
    body = "\n".join([
        "外部交易账户长连接状态异常，请检查 PTrade 端脚本和网络连接。",
        "",
        f"告警场景: {scene}",
        f"告警时间: {now.strftime('%Y-%m-%d %H:%M:%S')}",
        f"账户名: {account.get('name')}",
        f"唯一标识: {account.get('identifier')}",
        f"备注: {account.get('remark') or '-'}",
        f"异常原因: {reason}",
        "",
        f"运行时连接: {'是' if runtime_status.get('connected') else '否'}",
        f"运行时连接时间: {_format_dt(runtime_status.get('connected_at'))}",
        f"运行时最后心跳: {_format_dt(runtime_status.get('last_seen_at'))}",
        f"数据库最后连接: {_format_dt(account.get('last_connected_at'))}",
        f"数据库最后断开: {_format_dt(account.get('last_disconnected_at'))}",
        f"数据库最后心跳: {_format_dt(account.get('last_seen_at'))}",
        f"最后断开原因: {account.get('last_disconnect_reason') or '-'}",
    ])
    send_alert_email(subject, body)


def process_external_trading_connection_monitor_for_robot() -> Dict:
    now = _china_now()
    today = now.date()
    _reset_daily_state(today)

    if not _is_china_trading_day(today):
        return {"checked": 0, "alerts": 0, "skipped": "not_trading_day"}

    market_session = _get_market_session(now)
    preopen_window = _is_preopen_check_window(now)
    if not market_session and not preopen_window:
        return {"checked": 0, "alerts": 0, "skipped": "outside_monitor_window"}

    with get_db_ctx() as db:
        account_rows = (
            db.query(ExternalTradingAccount)
            .filter(ExternalTradingAccount.enabled == True)
            .order_by(ExternalTradingAccount.id.asc())
            .all()
        )
        accounts = [
            {
                "id": item.id,
                "name": item.name,
                "identifier": item.identifier,
                "remark": item.remark,
                "last_connected_at": item.last_connected_at,
                "last_disconnected_at": item.last_disconnected_at,
                "last_seen_at": item.last_seen_at,
                "last_disconnect_reason": item.last_disconnect_reason,
            }
            for item in account_rows
        ]

    checked = 0
    alerts = 0
    date_key = today.isoformat()
    for account in accounts:
        checked += 1
        account_pk = int(account["id"])
        health = _get_connection_health(account_pk)
        healthy = bool(health.get("healthy"))
        reason = str(health.get("reason") or "连接异常")
        runtime_status = health.get("status") or {}

        if preopen_window and not healthy:
            key = (date_key, account_pk)
            if key not in _preopen_alerted_keys:
                _send_external_trading_connection_alert(
                    account,
                    reason,
                    "A股开盘前1小时连接检查",
                    now,
                    runtime_status,
                )
                _preopen_alerted_keys.add(key)
                alerts += 1

        if market_session and not healthy:
            key = (date_key, market_session, account_pk)
            previous_health = _last_health_by_account.get(account_pk)
            should_alert = key not in _market_alerted_keys or previous_health is True
            if should_alert:
                scene = "A股交易时段长连接断开" if not health.get("stale") else "A股交易时段长连接心跳超时"
                _send_external_trading_connection_alert(
                    account,
                    reason,
                    scene,
                    now,
                    runtime_status,
                )
                _market_alerted_keys.add(key)
                alerts += 1

        _last_health_by_account[account_pk] = healthy

    return {
        "checked": checked,
        "alerts": alerts,
        "market_session": market_session,
        "preopen_window": preopen_window,
    }

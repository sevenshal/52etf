import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from ..external_trading_database import (
    ExternalTradingAccount,
    ExternalTradingSessionLocal,
)
from .external_trading import external_trading_hub
from .external_trading_ledger import reconcile_deliver_records
from .external_trading_market import (
    EXTERNAL_TRADING_MARKET_A_STOCK,
    normalize_external_trading_market_type,
)

logger = logging.getLogger(__name__)

CHINA_TZ = ZoneInfo("Asia/Shanghai")


def _is_china_trading_day(check_date: date) -> bool:
    if check_date.weekday() >= 5:
        return False
    try:
        from .tushare import TushareService

        calendar = TushareService.get_instance().get_trade_calendar_frame(check_date, check_date)
        if not calendar.empty:
            row = calendar.iloc[0]
            return int(row.get("is_open") or 0) == 1
    except Exception as exc:
        logger.warning("A-share trading calendar check failed for %s: %s", check_date, exc)
    return True


def previous_a_share_trading_day(current_date: Optional[date] = None) -> date:
    check_date = (current_date or datetime.now(CHINA_TZ).date()) - timedelta(days=1)
    while not _is_china_trading_day(check_date):
        check_date -= timedelta(days=1)
    return check_date


def _extract_deliver_records(deliver_response: Dict[str, Any]) -> List[Dict[str, Any]]:
    records = (
        deliver_response.get("records")
        or deliver_response.get("deliver_records")
        or deliver_response.get("delivers")
        or deliver_response.get("deliveries")
        or deliver_response.get("data")
        or []
    )
    if isinstance(records, dict):
        records = list(records.values())
    if not isinstance(records, list):
        raise ValueError("PTrade get_deliver returned invalid records")
    return records


def process_deliver_event(
    db: Session,
    *,
    external_trading_account_id: int,
    deliver_data: Dict[str, Any],
) -> Dict[str, Any]:
    """Handle deliver_event pushed by PTrade client in before_trading_start."""
    records = _extract_deliver_records(deliver_data)
    start_date_str = deliver_data.get("start_date")
    if start_date_str:
        try:
            # normalize_deliver_date returns YYYYMMDD format
            if len(start_date_str) == 8:
                trade_date = date(int(start_date_str[:4]), int(start_date_str[4:6]), int(start_date_str[6:8]))
            else:
                trade_date = date.fromisoformat(start_date_str[:10])
        except Exception:
            trade_date = previous_a_share_trading_day()
    else:
        trade_date = previous_a_share_trading_day()

    account = db.query(ExternalTradingAccount).get(external_trading_account_id)
    if not account:
        raise ValueError(f"Account {external_trading_account_id} not found")

    result = reconcile_deliver_records(
        db,
        account=account,
        records=records,
        default_trade_date=trade_date,
    )
    db.commit()
    logger.info(
        "process_deliver_event: account=%s trade_date=%s received=%s matched=%s unmatched=%s ignored=%s",
        account.id,
        trade_date.isoformat(),
        result.get("received"),
        result.get("matched"),
        result.get("unmatched"),
        result.get("ignored"),
    )
    return {
        "account_id": account.id,
        "trade_date": trade_date.isoformat(),
        **result,
    }


def check_and_alert_missing_deliver_records(today: Optional[date] = None) -> Dict[str, Any]:
    """Check if PTrade deliver_event was received and reconciled.

    PTrade now proactively pushes deliver_event in before_trading_start.
    This task runs after market open to verify all accounts have been reconciled.
    If any account is missing, it returns the missing accounts.
    """
    from datetime import time as dtime
    from ..external_trading_database import ExternalTradingOrder

    today_date = today or datetime.now(CHINA_TZ).date()
    if not _is_china_trading_day(today_date):
        return {
            "status": "SKIPPED",
            "reason": "not_trading_day",
            "today": today_date.isoformat(),
        }

    target_date = previous_a_share_trading_day(today_date)
    db = ExternalTradingSessionLocal()
    try:
        enabled_accounts = (
            db.query(ExternalTradingAccount)
            .filter(ExternalTradingAccount.enabled == True)  # noqa: E712
            .order_by(ExternalTradingAccount.id.asc())
            .all()
        )
        accounts = [
            account
            for account in enabled_accounts
            if normalize_external_trading_market_type(getattr(account, "market_type", None))
            == EXTERNAL_TRADING_MARKET_A_STOCK
        ]
        checked = len(accounts)
        skipped_non_a_stock = len(enabled_accounts) - checked
        reconciled = 0
        missing_accounts = []

        for account in accounts:
            reconciled_count = (
                db.query(ExternalTradingOrder)
                .filter(
                    ExternalTradingOrder.external_trading_account_id == account.id,
                    ExternalTradingOrder.fee_reconciled_at != None,  # noqa: E711
                    ExternalTradingOrder.fee_reconciled_at >= datetime.combine(today_date, dtime(0, 0)),
                )
                .count()
            )
            if reconciled_count > 0:
                reconciled += 1
            else:
                missing_accounts.append(account.name or str(account.id))
    finally:
        db.close()

    result = {
        "status": "OK" if not missing_accounts else "MISSING",
        "trade_date": target_date.isoformat(),
        "checked": checked,
        "reconciled": reconciled,
        "missing": len(missing_accounts),
        "missing_accounts": missing_accounts,
        "skipped_non_a_stock": skipped_non_a_stock,
    }

    if missing_accounts:
        from ..utils import send_alert_email
        now_shanghai = datetime.now(CHINA_TZ)
        missing_text = ", ".join(missing_accounts)
        logger.warning(
            "External trading fee reconcile missing for accounts: %s (trade_date=%s)",
            missing_text,
            target_date.isoformat(),
        )
        subject = "外部交易费用对账缺失告警"
        body = (
            f"以下外部交易账号未收到 PTrade 推送的交割单 (deliver_event):\n\n"
            f"交易日期: {target_date.isoformat()}\n"
            f"检查时间: {now_shanghai.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"缺失账号: {missing_text}\n\n"
            f"请检查 PTrade 策略 before_trading_start 是否正常执行，以及 WebSocket 连接是否正常。"
        )
        try:
            send_alert_email(
                subject,
                body,
                scenario_key="external_trading_fee_reconcile_alert",
            )
            result["alert_sent"] = True
        except Exception as exc:
            logger.error("Failed to send fee reconcile alert email: %s", exc)

    return result

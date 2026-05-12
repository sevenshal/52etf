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
from .external_trading import ExternalTradingConnectionError, external_trading_hub
from .external_trading_ledger import reconcile_deliver_records

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


async def reconcile_external_trading_account_fees(
    db: Session,
    *,
    account: ExternalTradingAccount,
    start_date: date,
    end_date: date,
    timeout_seconds: float = 30.0,
) -> Dict[str, Any]:
    deliver_response = await external_trading_hub.get_deliver(
        account.id,
        start_date.isoformat(),
        end_date.isoformat(),
        timeout=timeout_seconds,
    )
    records = _extract_deliver_records(deliver_response)
    default_trade_date = start_date if start_date == end_date else None
    result = reconcile_deliver_records(
        db,
        account=account,
        records=records,
        default_trade_date=default_trade_date,
    )
    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "deliver_response": deliver_response,
        "result": result,
    }


def _format_failure_preview(items: List[Dict[str, Any]], limit: int = 8) -> str:
    failures = [item for item in items if item.get("status") == "FAILED"]
    preview = "; ".join(
        f"{item.get('account_name') or item.get('external_trading_account_id')}: {item.get('error')}"
        for item in failures[:limit]
    )
    remaining = len(failures) - min(len(failures), limit)
    if remaining > 0:
        preview = f"{preview}; ... and {remaining} more"
    return preview


async def reconcile_enabled_external_trading_account_fees(
    *,
    trade_date: Optional[date] = None,
    timeout_seconds: float = 30.0,
    strict: bool = False,
    skip_non_trading_day: bool = True,
) -> Dict[str, Any]:
    today = datetime.now(CHINA_TZ).date()
    if skip_non_trading_day and not _is_china_trading_day(today):
        return {
            "status": "SKIPPED",
            "reason": "not_trading_day",
            "today": today.isoformat(),
            "items": [],
        }

    target_date = trade_date or previous_a_share_trading_day(today)
    result: Dict[str, Any] = {
        "status": "OK",
        "trade_date": target_date.isoformat(),
        "checked": 0,
        "processed": 0,
        "failed": 0,
        "matched": 0,
        "unmatched": 0,
        "applied_order_count": 0,
        "items": [],
    }

    db = ExternalTradingSessionLocal()
    try:
        accounts = (
            db.query(ExternalTradingAccount)
            .filter(ExternalTradingAccount.enabled == True)  # noqa: E712
            .order_by(ExternalTradingAccount.id.asc())
            .all()
        )
        result["checked"] = len(accounts)
        for account in accounts:
            item = {
                "external_trading_account_id": account.id,
                "account_id": account.account_id,
                "account_name": account.name,
                "trade_date": target_date.isoformat(),
            }
            try:
                if not external_trading_hub.get_status(account.id).get("connected"):
                    raise ExternalTradingConnectionError("外部交易账号未连接")
                reconciled = await reconcile_external_trading_account_fees(
                    db,
                    account=account,
                    start_date=target_date,
                    end_date=target_date,
                    timeout_seconds=timeout_seconds,
                )
                db.commit()
                account_result = reconciled.get("result") or {}
                item.update({
                    "status": "RECONCILED",
                    "received": account_result.get("received", 0),
                    "matched": account_result.get("matched", 0),
                    "unmatched": account_result.get("unmatched", 0),
                    "applied_order_count": account_result.get("applied_order_count", 0),
                })
                result["processed"] += 1
                result["matched"] += int(account_result.get("matched") or 0)
                result["unmatched"] += int(account_result.get("unmatched") or 0)
                result["applied_order_count"] += int(account_result.get("applied_order_count") or 0)
            except ExternalTradingConnectionError as exc:
                db.rollback()
                logger.warning(
                    "External trading fee reconcile skipped unavailable account: account=%s trade_date=%s error=%s",
                    account.id,
                    target_date,
                    exc,
                )
                item.update({"status": "FAILED", "error": str(exc)})
                result["failed"] += 1
            except Exception as exc:
                db.rollback()
                logger.exception("External trading fee reconcile failed: account=%s trade_date=%s", account.id, target_date)
                item.update({"status": "FAILED", "error": str(exc)})
                result["failed"] += 1
            result["items"].append(item)
    finally:
        db.close()

    if result["failed"]:
        result["status"] = "PARTIAL_FAILED" if result["processed"] else "FAILED"
        if strict:
            raise RuntimeError(
                "外部交易费用对账失败 "
                f"trade_date={target_date.isoformat()} failed={result['failed']} "
                f"{_format_failure_preview(result['items'])}"
            )
    return result


def process_external_trading_fee_reconcile_for_robot(*, strict: bool = False) -> Dict[str, Any]:
    return asyncio.run(reconcile_enabled_external_trading_account_fees(strict=strict))

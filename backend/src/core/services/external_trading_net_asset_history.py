import asyncio
import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from ..external_trading_database import (
    ExternalTradingAccount,
    ExternalTradingLedgerPosition,
    ExternalTradingSubAccount,
    ExternalTradingSubAccountNetAssetHistory,
    get_external_trading_db_ctx,
)
from .external_trading_ledger import safe_float, safe_int
from .external_trading_valuation import ExternalTradingValuationError, calculate_sub_account_net_asset

logger = logging.getLogger(__name__)


def _today_shanghai() -> date:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def _serializable_price_details(price_details: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for symbol, detail in (price_details or {}).items():
        result[str(symbol)] = {
            "price": safe_float(detail.get("price")),
            "source": detail.get("source"),
        }
    return result


def _history_row_for_update(db, sub_account_id: int, trading_date: date) -> ExternalTradingSubAccountNetAssetHistory:
    row = (
        db.query(ExternalTradingSubAccountNetAssetHistory)
        .filter(
            ExternalTradingSubAccountNetAssetHistory.sub_account_id == sub_account_id,
            ExternalTradingSubAccountNetAssetHistory.trading_date == trading_date,
        )
        .first()
    )
    if row:
        return row
    row = ExternalTradingSubAccountNetAssetHistory(
        sub_account_id=sub_account_id,
        trading_date=trading_date,
        created_at=datetime.now(),
    )
    db.add(row)
    return row


async def record_sub_account_net_asset_history(
    db,
    sub_account: ExternalTradingSubAccount,
    *,
    trading_date: Optional[date] = None,
    source: str = "scheduled_close",
    timeout: float = 10.0,
) -> Dict[str, Any]:
    trading_date = trading_date or _today_shanghai()
    positions: List[ExternalTradingLedgerPosition] = (
        db.query(ExternalTradingLedgerPosition)
        .filter(ExternalTradingLedgerPosition.sub_account_id == sub_account.id)
        .order_by(ExternalTradingLedgerPosition.symbol.asc())
        .all()
    )
    row = _history_row_for_update(db, sub_account.id, trading_date)
    now = datetime.now()

    row.account_id = sub_account.account_id
    row.external_trading_account_id = sub_account.external_trading_account_id
    row.strategy_type = sub_account.strategy_type
    row.strategy_config_id = sub_account.strategy_config_id
    row.cash_allocated = round(safe_float(sub_account.cash_allocated), 2)
    row.source = source
    row.updated_at = now

    try:
        valuation = await calculate_sub_account_net_asset(
            db,
            sub_account,
            positions=positions,
            timeout=timeout,
            update_positions=True,
        )
        row.cash_available = round(safe_float(valuation.get("cash_available")), 2)
        row.position_market_value = round(safe_float(valuation.get("position_market_value")), 2)
        row.net_asset = round(safe_float(valuation.get("net_asset")), 2)
        row.position_count = len(valuation.get("positions") or [])
        row.positions = valuation.get("positions") or []
        row.price_details = _serializable_price_details(valuation.get("price_details") or {})
        row.status = "SUCCESS"
        row.message = None
        row.valued_at = now
        return {
            "sub_account_id": sub_account.id,
            "sub_account_name": sub_account.name,
            "status": row.status,
            "trading_date": trading_date.isoformat(),
            "cash_available": row.cash_available,
            "position_market_value": row.position_market_value,
            "net_asset": row.net_asset,
            "position_count": row.position_count,
        }
    except ExternalTradingValuationError as exc:
        position_market_value = round(sum(safe_float(item.market_value) for item in positions if safe_int(item.quantity) > 0), 2)
        row.cash_available = round(safe_float(sub_account.cash_available), 2)
        row.position_market_value = position_market_value
        row.net_asset = round(row.cash_available + row.position_market_value, 2)
        row.position_count = len([item for item in positions if safe_int(item.quantity) > 0])
        row.positions = [
            {
                "symbol": item.symbol,
                "quantity": safe_int(item.quantity),
                "price": safe_float(item.market_price),
                "market_value": safe_float(item.market_value),
            }
            for item in positions
            if safe_int(item.quantity) > 0
        ]
        row.price_details = {}
        row.status = "FAILED"
        row.message = str(exc)[:1000]
        row.valued_at = now
        logger.warning("Sub-account net asset snapshot failed: sub_account_id=%s error=%s", sub_account.id, exc)
        return {
            "sub_account_id": sub_account.id,
            "sub_account_name": sub_account.name,
            "status": row.status,
            "trading_date": trading_date.isoformat(),
            "cash_available": row.cash_available,
            "position_market_value": row.position_market_value,
            "net_asset": row.net_asset,
            "position_count": row.position_count,
            "message": row.message,
        }


async def record_all_sub_account_net_asset_history(
    *,
    trading_date: Optional[date] = None,
    source: str = "scheduled_close",
    timeout: float = 10.0,
) -> Dict[str, Any]:
    trading_date = trading_date or _today_shanghai()
    with get_external_trading_db_ctx() as db:
        sub_account_ids = [
            row[0]
            for row in (
                db.query(ExternalTradingSubAccount.id)
                .join(
                    ExternalTradingAccount,
                    ExternalTradingSubAccount.external_trading_account_id == ExternalTradingAccount.id,
                )
                .filter(ExternalTradingAccount.enabled == True)  # noqa: E712
                .order_by(ExternalTradingSubAccount.external_trading_account_id.asc(), ExternalTradingSubAccount.id.asc())
                .all()
            )
        ]

    records = []
    for sub_account_id in sub_account_ids:
        with get_external_trading_db_ctx() as db:
            sub_account = db.query(ExternalTradingSubAccount).filter(ExternalTradingSubAccount.id == sub_account_id).first()
            if not sub_account:
                continue
            records.append(await record_sub_account_net_asset_history(
                db,
                sub_account,
                trading_date=trading_date,
                source=source,
                timeout=timeout,
            ))

    succeeded = [item for item in records if item.get("status") == "SUCCESS"]
    failed = [item for item in records if item.get("status") != "SUCCESS"]
    return {
        "status": "OK" if not failed else "PARTIAL_FAILED",
        "trading_date": trading_date.isoformat(),
        "checked": len(sub_account_ids),
        "recorded": len(succeeded),
        "failed": len(failed),
        "records": records,
    }


def process_external_trading_sub_account_net_asset_snapshot_for_robot() -> Dict[str, Any]:
    return asyncio.run(record_all_sub_account_net_asset_history(source="scheduled_close"))

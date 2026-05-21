import asyncio
import logging
import threading
from datetime import date, datetime, time as dtime, timedelta
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

from ..external_trading_database import (
    ExternalTradingAccount,
    ExternalTradingOrder,
    ExternalTradingTargetPosition,
    get_external_trading_db_ctx,
)
from .external_trading import ExternalTradingConnectionError, external_trading_hub
from .external_trading_execution_policy import (
    DEFAULT_EXECUTOR_LOT_SIZE,
    DEFAULT_EXECUTOR_MAX_REPLACE_COUNT,
    DEFAULT_EXECUTOR_MAX_SLIPPAGE_PCT,
    DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS,
    DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS_SEQUENCE,
    DEFAULT_EXECUTOR_PRICE_LEVEL,
    DEFAULT_EXECUTOR_PRICE_LEVEL_SEQUENCE,
    normalize_clip_sell_to_available,
    normalize_lot_size,
    normalize_max_replace_count,
    normalize_max_slippage_pct,
    normalize_price_level,
    normalize_price_level_sequence,
    normalize_timeout_seconds,
    normalize_timeout_seconds_sequence,
    resolve_execution_policy,
)
from .external_trading_ledger import (
    ACTIVE_ORDER_STATUSES,
    STRATEGY_NETTED_EXECUTOR,
    apply_internal_crosses,
    build_netted_target_execution_plan,
    collect_internal_cross_reference_symbols,
    create_netted_execution_orders,
    expire_insufficient_sellable_blocks,
    normalize_symbol,
    record_cancel_result,
    record_submission_result,
    safe_float,
    safe_int,
    serialize_order,
)
from .external_trading_valuation import ExternalTradingValuationError, get_realtime_reference_prices

logger = logging.getLogger(__name__)

CHINA_TZ = ZoneInfo("Asia/Shanghai")
A_SHARE_OPEN = dtime(9, 30)
A_SHARE_MORNING_CLOSE = dtime(11, 30)
A_SHARE_AFTERNOON_OPEN = dtime(13, 0)
A_SHARE_CLOSE = dtime(15, 0)

_running_lock = threading.Lock()
_running_account_ids: Set[int] = set()
_trading_day_cache: Dict[date, bool] = {}


def _china_now() -> datetime:
    return datetime.now(CHINA_TZ)


def _naive_now() -> datetime:
    return _china_now().replace(tzinfo=None)


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


def is_a_share_trading_window(now: Optional[datetime] = None) -> bool:
    current = now or _china_now()
    if current.tzinfo:
        current = current.astimezone(CHINA_TZ)
    if not _is_china_trading_day(current.date()):
        return False
    current_time = current.time()
    return (
        A_SHARE_OPEN <= current_time <= A_SHARE_MORNING_CLOSE
        or A_SHARE_AFTERNOON_OPEN <= current_time <= A_SHARE_CLOSE
    )


def next_a_share_trading_time(now: Optional[datetime] = None) -> datetime:
    current = now or _china_now()
    if current.tzinfo:
        current = current.astimezone(CHINA_TZ)

    if _is_china_trading_day(current.date()):
        current_time = current.time()
        if current_time < A_SHARE_OPEN:
            return current.replace(hour=A_SHARE_OPEN.hour, minute=A_SHARE_OPEN.minute, second=0, microsecond=0)
        if A_SHARE_MORNING_CLOSE < current_time < A_SHARE_AFTERNOON_OPEN:
            return current.replace(
                hour=A_SHARE_AFTERNOON_OPEN.hour,
                minute=A_SHARE_AFTERNOON_OPEN.minute,
                second=0,
                microsecond=0,
            )

    next_day = current.date() + timedelta(days=1)
    while not _is_china_trading_day(next_day):
        next_day += timedelta(days=1)
    return datetime.combine(next_day, A_SHARE_OPEN, tzinfo=CHINA_TZ)


def next_a_share_trading_day_open(now: Optional[datetime] = None) -> datetime:
    current = now or _china_now()
    if current.tzinfo:
        current = current.astimezone(CHINA_TZ)
    next_day = current.date() + timedelta(days=1)
    while not _is_china_trading_day(next_day):
        next_day += timedelta(days=1)
    return datetime.combine(next_day, A_SHARE_OPEN, tzinfo=CHINA_TZ)


def _try_mark_running(account_pk: int) -> bool:
    with _running_lock:
        if account_pk in _running_account_ids:
            return False
        _running_account_ids.add(account_pk)
        return True


def _clear_running(account_pk: int) -> None:
    with _running_lock:
        _running_account_ids.discard(account_pk)


def _load_accounts(
    *,
    account_id: Optional[str] = None,
    external_account_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    with get_external_trading_db_ctx() as db:
        query = db.query(ExternalTradingAccount).filter(ExternalTradingAccount.enabled == True)  # noqa: E712
        if account_id:
            query = query.filter(ExternalTradingAccount.account_id == account_id)
        if external_account_id:
            query = query.filter(ExternalTradingAccount.id == external_account_id)
        rows = query.order_by(ExternalTradingAccount.id.asc()).all()
        return [
            {
                "id": row.id,
                "account_id": row.account_id,
                "name": row.name,
                "identifier": row.identifier,
                "enabled": row.enabled,
                "executor_enabled": getattr(row, "executor_enabled", True),
                "executor_price_level": row.executor_price_level,
                "executor_lot_size": row.executor_lot_size,
                "executor_order_timeout_seconds": row.executor_order_timeout_seconds,
                "executor_order_timeout_seconds_sequence": getattr(row, "executor_order_timeout_seconds_sequence", None),
                "executor_max_replace_count": row.executor_max_replace_count,
                "executor_max_slippage_pct": getattr(row, "executor_max_slippage_pct", DEFAULT_EXECUTOR_MAX_SLIPPAGE_PCT),
                "executor_clip_sell_to_available": getattr(row, "executor_clip_sell_to_available", True),
                "executor_price_level_sequence": row.executor_price_level_sequence,
            }
            for row in rows
        ]


def _current_target_versions(db, external_account_id: int) -> Dict[Tuple[int, str], Optional[str]]:
    rows = (
        db.query(ExternalTradingTargetPosition)
        .filter(
            ExternalTradingTargetPosition.external_trading_account_id == external_account_id,
            ExternalTradingTargetPosition.status == "ACTIVE",
        )
        .all()
    )
    return {
        (safe_int(row.sub_account_id), normalize_symbol(row.symbol)): row.signal_version
        for row in rows
        if row.sub_account_id and row.symbol
    }


def _children_for_parent(db, parent_order_id: Optional[int]) -> List[ExternalTradingOrder]:
    if not parent_order_id:
        return []
    return (
        db.query(ExternalTradingOrder)
        .filter(ExternalTradingOrder.parent_order_id == parent_order_id)
        .order_by(ExternalTradingOrder.id.asc())
        .all()
    )


def _child_signal_version(child: ExternalTradingOrder) -> Optional[str]:
    if child.signal_version:
        return child.signal_version
    raw = child.raw_request or {}
    return raw.get("signal_version") if isinstance(raw, dict) else None


def _parent_order_obsolete(
    db,
    parent: ExternalTradingOrder,
    target_versions: Dict[Tuple[int, str], Optional[str]],
) -> bool:
    children = _children_for_parent(db, parent.id)
    active_children = [child for child in children if child.status in ACTIVE_ORDER_STATUSES]
    if not active_children:
        return False
    for child in active_children:
        if not child.sub_account_id:
            continue
        key = (safe_int(child.sub_account_id), normalize_symbol(child.symbol))
        current_version = target_versions.get(key)
        child_version = _child_signal_version(child)
        if key not in target_versions:
            return True
        if child_version and current_version != child_version:
            return True
    return False


def _propagate_parent_status_for_executor(db, parent: ExternalTradingOrder) -> None:
    for child in _children_for_parent(db, parent.id):
        child.broker_order_id = parent.broker_order_id or child.broker_order_id
        child.entrust_no = parent.entrust_no or child.entrust_no
        child.ptrade_status = parent.ptrade_status or child.ptrade_status
        child.message = parent.message or child.message
        child.status = parent.status
        child.last_event_at = parent.last_event_at
        child.updated_at = datetime.now()


def _mark_parent_failed_without_broker_id(db, parent: ExternalTradingOrder, reason: str) -> None:
    now = datetime.now()
    parent.status = "FAILED"
    parent.cancel_reason = reason
    parent.message = "订单没有券商订单号，无法撤单，已标记失败"
    parent.last_event_at = now
    parent.updated_at = now
    _propagate_parent_status_for_executor(db, parent)


async def _cancel_stale_or_timed_out_orders(account: Dict[str, Any]) -> Dict[str, Any]:
    account_pk = int(account["id"])
    now = _naive_now()
    cancel_items = []
    cancel_reason_by_client_id: Dict[str, str] = {}
    failed_without_broker = 0

    with get_external_trading_db_ctx() as db:
        target_versions = _current_target_versions(db, account_pk)
        active_parents = (
            db.query(ExternalTradingOrder)
            .filter(
                ExternalTradingOrder.external_trading_account_id == account_pk,
                ExternalTradingOrder.allocation_role == "PARENT",
                ExternalTradingOrder.status.in_(list(ACTIVE_ORDER_STATUSES)),
            )
            .order_by(ExternalTradingOrder.created_at.asc(), ExternalTradingOrder.id.asc())
            .all()
        )
        for parent in active_parents:
            if parent.status == "CANCEL_PENDING":
                continue

            stale_signal = _parent_order_obsolete(db, parent, target_versions)
            timed_out = bool(parent.deadline_at and parent.deadline_at <= now)
            if not stale_signal and not timed_out:
                continue

            reason = "signal_changed" if stale_signal else "timeout_reprice"
            order_id = parent.broker_order_id or parent.entrust_no
            if not order_id:
                failed_without_broker += 1
                _mark_parent_failed_without_broker_id(db, parent, reason)
                continue

            parent.cancel_reason = reason
            parent.message = "信号变化，等待撤单" if stale_signal else "订单超时，等待撤单后重定价"
            parent.updated_at = now
            cancel_items.append({
                "order_id": order_id,
                "client_order_id": parent.client_order_id,
            })
            cancel_reason_by_client_id[parent.client_order_id] = reason

    if not cancel_items:
        return {
            "requested": 0,
            "failed_without_broker": failed_without_broker,
            "orders": [],
        }

    response = await external_trading_hub.cancel_orders(account_pk, cancel_items, timeout=15.0)
    with get_external_trading_db_ctx() as db:
        for item in response.get("orders") or []:
            client_order_id = item.get("client_order_id")
            if not client_order_id:
                continue
            row = db.query(ExternalTradingOrder).filter(ExternalTradingOrder.client_order_id == client_order_id).first()
            if row:
                row.cancel_reason = cancel_reason_by_client_id.get(client_order_id) or row.cancel_reason
                row.updated_at = datetime.now()
        record_cancel_result(
            db,
            external_trading_account_id=account_pk,
            response_orders=response.get("orders") or [],
        )

    return {
        "requested": len(cancel_items),
        "failed_without_broker": failed_without_broker,
        "orders": response.get("orders") or [],
    }


async def _reference_prices_for_plan(account_pk: int, symbols: List[str]) -> Dict[str, float]:
    if not symbols:
        return {}
    try:
        return await get_realtime_reference_prices(account_pk, symbols, timeout=10.0)
    except ExternalTradingValuationError as exc:
        logger.warning("External trading executor reference price lookup failed: %s", exc)
        return {}
    except Exception as exc:
        logger.warning("External trading executor reference price lookup failed: %s", exc)
        return {}


def _order_signal_version(order: Dict[str, Any]) -> Optional[str]:
    versions = sorted({
        str(allocation.get("signal_version"))
        for allocation in (order.get("allocations") or [])
        if allocation.get("signal_version")
    })
    if versions:
        return ",".join(versions)[:64]
    return order.get("signal_version")


def _reference_protection_limit_price(order: Dict[str, Any], side: str) -> Optional[float]:
    reference_prices = []
    for item in order.get("allocations") or []:
        reference_price = safe_float(item.get("reference_price"))
        if safe_int(item.get("quantity")) > 0 and reference_price > 0:
            reference_prices.append(reference_price)
    if not reference_prices:
        return None

    policy = order.get("execution_policy") or {}
    price_level = normalize_price_level(order.get("price_level"), DEFAULT_EXECUTOR_PRICE_LEVEL)
    slippage_pct = 0.0 if price_level == 0 else normalize_max_slippage_pct(
        policy.get("max_slippage_pct"),
        DEFAULT_EXECUTOR_MAX_SLIPPAGE_PCT,
    )
    slippage_rate = max(slippage_pct, 0.0) / 100.0
    if side == "BUY":
        prices = [price * (1.0 + slippage_rate) for price in reference_prices]
        return round(min(prices), 4)
    if side == "SELL":
        prices = [price * (1.0 - slippage_rate) for price in reference_prices]
        return round(max(prices), 4)
    return None


def _max_replace_count_today(
    db,
    *,
    account_pk: int,
    symbol: str,
    side: str,
    signal_version: Optional[str] = None,
) -> int:
    start = _naive_now().replace(hour=0, minute=0, second=0, microsecond=0)
    query = db.query(ExternalTradingOrder.replace_count).filter(
        ExternalTradingOrder.external_trading_account_id == account_pk,
        ExternalTradingOrder.allocation_role == "PARENT",
        ExternalTradingOrder.strategy_type == STRATEGY_NETTED_EXECUTOR,
        ExternalTradingOrder.symbol == normalize_symbol(symbol),
        ExternalTradingOrder.side == side,
        ExternalTradingOrder.created_at >= start,
    )
    if signal_version:
        query = query.filter(ExternalTradingOrder.signal_version == signal_version)
    rows = query.all()
    if not rows:
        return -1
    return max(safe_int(row[0]) for row in rows)


def _apply_execution_metadata(
    db,
    *,
    account_pk: int,
    plan: Dict[str, Any],
    price_level: int,
) -> List[Dict[str, Any]]:
    executable_orders = []
    skipped = plan.setdefault("skipped", [])
    for order in plan.get("external_orders") or []:
        side = str(order.get("side") or "").upper()
        symbol = normalize_symbol(order.get("symbol"))
        policy = order.get("execution_policy") or {}
        signal_version = _order_signal_version(order)
        max_replace_count = normalize_max_replace_count(
            policy.get("max_replace_count"),
            DEFAULT_EXECUTOR_MAX_REPLACE_COUNT,
        )
        previous_replace_count = _max_replace_count_today(
            db,
            account_pk=account_pk,
            symbol=symbol,
            side=side,
            signal_version=signal_version,
        )
        replace_count = previous_replace_count + 1
        if replace_count > max_replace_count:
            skipped.append({
                "symbol": symbol,
                "side": side,
                "quantity": order.get("quantity"),
                "message": f"已达到最大重定价次数 {max_replace_count}",
            })
            continue

        sequence = normalize_price_level_sequence(
            policy.get("price_level_sequence"),
            default=DEFAULT_EXECUTOR_PRICE_LEVEL_SEQUENCE,
        )
        level = sequence[min(replace_count, len(sequence) - 1)]
        timeout_sequence = normalize_timeout_seconds_sequence(
            policy.get("order_timeout_seconds_sequence"),
            default=[
                normalize_timeout_seconds(
                    policy.get("order_timeout_seconds"),
                    DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS,
                )
            ] * len(DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS_SEQUENCE),
        )
        timeout_seconds = timeout_sequence[min(replace_count, len(timeout_sequence) - 1)]
        enriched = dict(order)
        enriched["price_level"] = level
        enriched["replace_count"] = replace_count
        enriched["signal_version"] = signal_version
        enriched["deadline_at"] = (datetime.now() + timedelta(seconds=timeout_seconds)).isoformat()
        enriched["execution_policy"] = {
            **policy,
            "price_level": sequence[0],
            "price_level_sequence": sequence,
            "order_timeout_seconds": timeout_seconds,
            "order_timeout_seconds_sequence": timeout_sequence,
            "max_replace_count": max_replace_count,
            "max_slippage_pct": normalize_max_slippage_pct(
                policy.get("max_slippage_pct"),
                DEFAULT_EXECUTOR_MAX_SLIPPAGE_PCT,
            ),
        }
        protection_limit_price = _reference_protection_limit_price(enriched, side)
        if protection_limit_price:
            enriched["protection_limit_price"] = protection_limit_price
            enriched["protection_limit_source"] = (
                "reference_price_limit" if normalize_price_level(enriched.get("price_level"), price_level) == 0
                else "reference_price_with_executor_slippage"
            )
        enriched["execution_pricing"] = "PTRADE_SNAPSHOT_AT_ORDER_TIME"
        executable_orders.append(enriched)
    plan["external_orders"] = executable_orders
    return executable_orders


def _mark_submission_error(parent_client_order_ids: List[str], message: str) -> None:
    if not parent_client_order_ids:
        return
    with get_external_trading_db_ctx() as db:
        rows = (
            db.query(ExternalTradingOrder)
            .filter(ExternalTradingOrder.client_order_id.in_(parent_client_order_ids))
            .all()
        )
        now = datetime.now()
        uncertain = "响应超时" in message or "timeout" in message.lower()
        for row in rows:
            row.status = "SUBMITTED" if uncertain else "FAILED"
            row.message = message[:1000]
            row.last_event_at = now
            row.updated_at = now
            _propagate_parent_status_for_executor(db, row)


async def _submit_current_targets(
    account: Dict[str, Any],
    *,
    trigger_source: str,
    price_level: int,
    lot_size: int,
    order_timeout_seconds: int,
) -> Dict[str, Any]:
    account_pk = int(account["id"])
    reference_prices: Dict[str, float] = {}
    account_policy = resolve_execution_policy(SimpleNamespace(**account), fallback={
        "price_level": price_level,
        "lot_size": lot_size,
        "order_timeout_seconds": order_timeout_seconds,
        "clip_sell_to_available": normalize_clip_sell_to_available(
            account.get("executor_clip_sell_to_available"),
        ),
    })

    with get_external_trading_db_ctx() as db:
        plan = build_netted_target_execution_plan(
            db,
            account_id=account["account_id"],
            external_trading_account_id=account_pk,
            price_level=account_policy.get("price_level"),
            lot_size=account_policy.get("lot_size"),
            order_timeout_seconds=account_policy.get("order_timeout_seconds"),
            max_replace_count=account_policy.get("max_replace_count"),
            clip_sell_to_available=account_policy.get("clip_sell_to_available"),
            price_level_sequence=account_policy.get("price_level_sequence"),
            order_timeout_seconds_sequence=account_policy.get("order_timeout_seconds_sequence"),
        )
        symbols = collect_internal_cross_reference_symbols(plan)

    if symbols:
        reference_prices = await _reference_prices_for_plan(account_pk, symbols)

    with get_external_trading_db_ctx() as db:
        plan = build_netted_target_execution_plan(
            db,
            account_id=account["account_id"],
            external_trading_account_id=account_pk,
            price_level=account_policy.get("price_level"),
            lot_size=account_policy.get("lot_size"),
            order_timeout_seconds=account_policy.get("order_timeout_seconds"),
            max_replace_count=account_policy.get("max_replace_count"),
            clip_sell_to_available=account_policy.get("clip_sell_to_available"),
            price_level_sequence=account_policy.get("price_level_sequence"),
            order_timeout_seconds_sequence=account_policy.get("order_timeout_seconds_sequence"),
            reference_prices=reference_prices,
        )
        plan["reference_prices"] = reference_prices
        plan["account_executor_policy"] = account_policy
        _apply_execution_metadata(
            db,
            account_pk=account_pk,
            plan=plan,
            price_level=account_policy.get("price_level"),
        )
        internal_orders = apply_internal_crosses(db, plan)
        execution_orders, parent_rows = create_netted_execution_orders(
            db,
            account_id=account["account_id"],
            external_trading_account_id=account_pk,
            orders=plan.get("external_orders") or [],
            deadline_at=datetime.now() + timedelta(seconds=account_policy.get("order_timeout_seconds")),
            executor_trigger=trigger_source,
        )
        parent_client_order_ids = [row.client_order_id for row in parent_rows]
        parent_orders = [serialize_order(row) for row in parent_rows]

    result = {"orders": [], "message": "无需要提交到券商的净额订单"}
    if execution_orders:
        try:
            result = await external_trading_hub.place_orders(
                account_pk,
                execution_orders,
                timeout=60.0,
            )
            with get_external_trading_db_ctx() as db:
                record_submission_result(
                    db,
                    external_trading_account_id=account_pk,
                    response_orders=result.get("orders") or [],
                    insufficient_sellable_block_until=next_a_share_trading_day_open().replace(tzinfo=None),
                )
        except ExternalTradingConnectionError as exc:
            _mark_submission_error(parent_client_order_ids, str(exc))
            raise

    return {
        "plan": plan,
        "internal_orders": internal_orders,
        "execution_orders": execution_orders,
        "parent_orders": parent_orders,
        "result": result,
    }


async def _run_account_executor(
    account: Dict[str, Any],
    *,
    trigger_source: str,
    price_level: int,
    lot_size: int,
    order_timeout_seconds: int,
) -> Dict[str, Any]:
    account_pk = int(account["id"])
    if trigger_source == "robot_timer" and not account.get("executor_enabled", True):
        return {
            "account_id": account_pk,
            "account_name": account.get("name"),
            "status": "SKIPPED",
            "reason": "executor_timer_disabled",
        }
    if not external_trading_hub.get_status(account_pk).get("connected"):
        return {
            "account_id": account_pk,
            "account_name": account.get("name"),
            "status": "SKIPPED",
            "reason": "external_account_disconnected",
        }

    with get_external_trading_db_ctx() as db:
        expire_insufficient_sellable_blocks(db, external_trading_account_id=account_pk, now=_naive_now())

    cancel_result = await _cancel_stale_or_timed_out_orders(account)
    if cancel_result.get("requested"):
        return {
            "account_id": account_pk,
            "account_name": account.get("name"),
            "status": "CANCEL_REQUESTED",
            "cancel": cancel_result,
            "message": "已提交撤单，等待 PTrade 回报后再重新撮合下单",
        }

    submission = await _submit_current_targets(
        account,
        trigger_source=trigger_source,
        price_level=price_level,
        lot_size=lot_size,
        order_timeout_seconds=order_timeout_seconds,
    )
    external_order_count = len(submission.get("execution_orders") or [])
    internal_order_count = len(submission.get("internal_orders") or [])
    return {
        "account_id": account_pk,
        "account_name": account.get("name"),
        "status": "SUBMITTED" if external_order_count else "IDLE",
        "external_order_count": external_order_count,
        "internal_order_count": internal_order_count,
        **submission,
    }


async def trigger_external_trading_executor(
    *,
    account_id: Optional[str] = None,
    external_account_id: Optional[int] = None,
    trigger_source: str = "manual",
    force: bool = False,
    price_level: int = DEFAULT_EXECUTOR_PRICE_LEVEL,
    lot_size: int = DEFAULT_EXECUTOR_LOT_SIZE,
    order_timeout_seconds: int = DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    now = _china_now()
    if not force and not is_a_share_trading_window(now):
        next_run_at = next_a_share_trading_time(now)
        return {
            "status": "SKIPPED",
            "reason": "market_closed",
            "trigger_source": trigger_source,
            "next_run_at": next_run_at.isoformat(),
            "accounts": [],
        }

    accounts = _load_accounts(account_id=account_id, external_account_id=external_account_id)
    result = {
        "status": "OK",
        "trigger_source": trigger_source,
        "checked": len(accounts),
        "processed": 0,
        "skipped": 0,
        "failed": 0,
        "accounts": [],
    }
    for account in accounts:
        account_pk = int(account["id"])
        if not _try_mark_running(account_pk):
            result["skipped"] += 1
            result["accounts"].append({
                "account_id": account_pk,
                "account_name": account.get("name"),
                "status": "SKIPPED",
                "reason": "executor_already_running",
            })
            continue
        try:
            account_result = await _run_account_executor(
                account,
                trigger_source=trigger_source,
                price_level=price_level,
                lot_size=lot_size,
                order_timeout_seconds=normalize_timeout_seconds(order_timeout_seconds),
            )
            result["accounts"].append(account_result)
            if account_result.get("status") == "SKIPPED":
                result["skipped"] += 1
            else:
                result["processed"] += 1
        except Exception as exc:
            logger.exception("External trading executor failed for account %s", account_pk)
            result["failed"] += 1
            result["accounts"].append({
                "account_id": account_pk,
                "account_name": account.get("name"),
                "status": "FAILED",
                "error": str(exc),
            })
        finally:
            _clear_running(account_pk)
    if result["failed"]:
        result["status"] = "PARTIAL_FAILED" if result["processed"] else "FAILED"
    return result


def process_external_trading_executor_for_robot() -> Dict[str, Any]:
    if not is_a_share_trading_window():
        return {
            "status": "SKIPPED",
            "reason": "market_closed",
            "accounts": [],
        }
    return asyncio.run(trigger_external_trading_executor(trigger_source="robot_timer"))

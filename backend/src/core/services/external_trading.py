import asyncio
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from decimal import (
    Decimal,
    InvalidOperation,
    ROUND_CEILING,
    ROUND_FLOOR,
    ROUND_HALF_UP,
)
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from fastapi import WebSocket

from ..external_trading_database import (
    EXTERNAL_TRADING_DB_PATH,
    ExternalTradingAccount,
    get_external_trading_db_ctx,
)
from .external_trading_ledger import (
    persist_broker_position_snapshot,
    process_order_events,
    process_trade_events,
)
from .external_trading_market import (
    EXTERNAL_TRADING_MARKET_A_STOCK,
    EXTERNAL_TRADING_MARKET_US_STOCK,
    external_trading_market_label,
    external_trading_market_timezone,
    is_external_trading_market_open,
    normalize_external_trading_market_type,
)
from .external_trading_crypto import decrypt_message, encrypt_message

logger = logging.getLogger(__name__)

CHINA_TZ = ZoneInfo("Asia/Shanghai")
A_SHARE_OPEN = dtime(9, 30)
A_SHARE_MORNING_CLOSE = dtime(11, 30)
A_SHARE_AFTERNOON_OPEN = dtime(13, 0)
A_SHARE_CLOSE = dtime(15, 0)
PTRADE_MARKET_HOURS_ENFORCED_ACTIONS = {
    "get_quotes",
    "get_snapshots",
    "place_orders",
    "cancel_orders",
    "get_account_snapshot",
    "get_positions",
    "get_assets",
    "get_today_orders",
    "get_deliver",
}
_trading_day_cache: Dict[date, bool] = {}

PENNY_PRICE_TICK = Decimal("0.01")
PENNY_PRICE_MARKET_TYPES = {
    EXTERNAL_TRADING_MARKET_A_STOCK,
    EXTERNAL_TRADING_MARKET_US_STOCK,
}
PENNY_ORDER_PRICE_FIELDS = (
    "price",
    "limit_price",
    "protection_limit_price",
    "market_limit_price",
    "max_buy_price",
    "min_sell_price",
)


class ExternalTradingConnectionError(Exception):
    """Raised when an external trading account is unavailable or rejects a command."""


def _as_positive_decimal(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if parsed <= 0:
        return None
    return parsed


def _round_penny_order_price(value: Any, side: str) -> Any:
    price = _as_positive_decimal(value)
    if price is None:
        return value

    upper_side = str(side or "").upper()
    if upper_side == "BUY":
        rounding = ROUND_FLOOR
    elif upper_side == "SELL":
        rounding = ROUND_CEILING
    else:
        rounding = ROUND_HALF_UP

    adjusted = (
        (price / PENNY_PRICE_TICK).to_integral_value(rounding=rounding)
        * PENNY_PRICE_TICK
    )
    adjusted = adjusted.quantize(PENNY_PRICE_TICK)
    if adjusted <= 0:
        return value
    return float(adjusted)


def _normalize_penny_order_prices(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    normalized_payload = dict(payload or {})
    orders = normalized_payload.get("orders")
    if not isinstance(orders, list):
        return normalized_payload

    normalized_orders = []
    for order in orders:
        if not isinstance(order, dict):
            normalized_orders.append(order)
            continue

        normalized_order = dict(order)
        side = str(normalized_order.get("side") or "").upper()
        for field in PENNY_ORDER_PRICE_FIELDS:
            if field in normalized_order:
                normalized_order[field] = _round_penny_order_price(
                    normalized_order.get(field),
                    side,
                )
        normalized_orders.append(normalized_order)

    normalized_payload["orders"] = normalized_orders
    return normalized_payload


def _prepare_command_payload(
    action: str,
    payload: Optional[Dict[str, Any]],
    market_type: Optional[str],
) -> Dict[str, Any]:
    prepared = payload or {}
    if action != "place_orders":
        return prepared
    if normalize_external_trading_market_type(market_type) not in PENNY_PRICE_MARKET_TYPES:
        return prepared
    return _normalize_penny_order_prices(prepared)


def _china_now() -> datetime:
    return datetime.now(CHINA_TZ)


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


def _is_a_share_trading_window(now: Optional[datetime] = None) -> bool:
    return is_external_trading_market_open(EXTERNAL_TRADING_MARKET_A_STOCK, now)


def is_a_share_trading_window(now: Optional[datetime] = None) -> bool:
    return _is_a_share_trading_window(now)


def _ensure_ptrade_command_window(action: str, market_type: Optional[str] = None) -> None:
    if action not in PTRADE_MARKET_HOURS_ENFORCED_ACTIONS:
        return
    normalized_market_type = normalize_external_trading_market_type(market_type)
    if normalized_market_type == EXTERNAL_TRADING_MARKET_US_STOCK:
        return
    if is_external_trading_market_open(normalized_market_type):
        return
    timezone = external_trading_market_timezone(normalized_market_type)
    now_text = datetime.now(timezone).strftime("%Y-%m-%d %H:%M:%S")
    raise ExternalTradingConnectionError(
        f"当前非{external_trading_market_label(normalized_market_type)}开盘时段（{timezone.key} {now_text}），拒绝发送 {action} 指令到外部交易客户端"
    )


@dataclass
class ExternalTradingConnection:
    account_pk: int
    account_id: str
    name: str
    identifier: str
    market_type: str
    websocket: WebSocket
    connection_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    connected_at: datetime = field(default_factory=datetime.now)
    last_seen_at: datetime = field(default_factory=datetime.now)
    pending: Dict[str, asyncio.Future] = field(default_factory=dict)


class ExternalTradingHub:
    """Runtime registry and request/response bridge for external trading clients."""

    def __init__(self):
        self._connections: Dict[int, ExternalTradingConnection] = {}
        self._lock = asyncio.Lock()
        self._last_seen_persisted_at: Dict[int, datetime] = {}
        self._seen_persist_interval = timedelta(seconds=30)

    async def connect(self, websocket: WebSocket, account: ExternalTradingAccount) -> ExternalTradingConnection:
        account_pk = int(account.id)
        account_id = account.account_id
        account_name = account.name
        account_identifier = account.identifier
        market_type = normalize_external_trading_market_type(getattr(account, "market_type", None))

        await websocket.accept()
        conn = ExternalTradingConnection(
            account_pk=account_pk,
            account_id=account_id,
            name=account_name,
            identifier=account_identifier,
            market_type=market_type,
            websocket=websocket,
        )

        async with self._lock:
            old_conn = self._connections.get(account_pk)
            self._connections[account_pk] = conn

        if old_conn:
            await self._finish_connection(
                old_conn,
                reason="replaced by a new WebSocket connection",
                close_code=4000,
            )

        self._mark_connected(account_pk)
        await websocket.send_text(encrypt_message({
            "type": "connected",
            "account_id": account_id,
            "name": account_name,
            "identifier": account_identifier,
            "market_type": market_type,
            "connected_at": conn.connected_at.isoformat(),
        }))
        logger.info("External trading account connected: %s/%s", account_id, account_name)
        return conn

    async def disconnect(self, account_pk: int, connection_id: str, reason: str = "disconnected"):
        async with self._lock:
            conn = self._connections.get(account_pk)
            if not conn or conn.connection_id != connection_id:
                return
            self._connections.pop(account_pk, None)

        await self._finish_connection(conn, reason=reason, close_code=None)
        logger.info("External trading account disconnected: %s/%s (%s)", conn.account_id, conn.name, reason)

    async def disconnect_account(self, account_pk: int, reason: str = "disconnected"):
        async with self._lock:
            conn = self._connections.pop(account_pk, None)
        if not conn:
            return
        await self._finish_connection(conn, reason=reason, close_code=4001)
        logger.info("External trading account disconnected by server: %s/%s (%s)", conn.account_id, conn.name, reason)

    async def _finish_connection(
        self,
        conn: ExternalTradingConnection,
        reason: str,
        close_code: Optional[int],
    ):
        for future in list(conn.pending.values()):
            if not future.done():
                future.set_exception(ExternalTradingConnectionError(reason))
        conn.pending.clear()

        if close_code is not None:
            try:
                await conn.websocket.close(code=close_code, reason=reason)
            except Exception:
                pass

        self._mark_disconnected(conn.account_pk, reason)

    async def handle_client_message(self, account_pk: int, connection_id: str, raw_message: str):
        conn = self._connections.get(account_pk)
        if not conn or conn.connection_id != connection_id:
            return

        now = datetime.now()
        conn.last_seen_at = now
        self._mark_seen_if_due(account_pk, now)

        message = decrypt_message(raw_message)

        message_type = message.get("type")
        if message_type in ("heartbeat", "ping"):
            await conn.websocket.send_text(encrypt_message({"type": "pong", "ts": now.isoformat()}))
            return

        if message_type == "result":
            request_id = message.get("id")
            future = conn.pending.pop(request_id, None) if request_id else None
            if future and not future.done():
                future.set_result(message)
            return

        if message_type == "order_event":
            with get_external_trading_db_ctx() as db:
                updated = process_order_events(
                    db,
                    external_trading_account_id=conn.account_pk,
                    orders=message.get("orders") or [],
                )
            logger.info("Processed external order events from %s: %s", conn.name, updated)
            return

        if message_type == "trade_event":
            with get_external_trading_db_ctx() as db:
                inserted = process_trade_events(
                    db,
                    external_trading_account_id=conn.account_pk,
                    trades=message.get("trades") or [],
                )
            logger.info("Processed external trade events from %s: %s", conn.name, inserted)
            return

        if message_type == "deliver_event":
            try:
                from .external_trading_fee_reconcile import process_deliver_event

                with get_external_trading_db_ctx() as db:
                    result = process_deliver_event(
                        db,
                        external_trading_account_id=conn.account_pk,
                        deliver_data=message.get("data") or {},
                    )
                logger.info("Processed deliver event from %s: %s", conn.name, result)
            except Exception as exc:
                logger.exception("Failed to process deliver event from %s: %s", conn.name, exc)
            return

        if message_type == "broker_positions_event":
            try:
                payload = message.get("data") or {}
                position_count = 0
                with get_external_trading_db_ctx() as db:
                    snapshot = persist_broker_position_snapshot(
                        db,
                        account=SimpleNamespace(id=conn.account_pk, account_id=conn.account_id),
                        payload=payload,
                        snapshot_source="push",
                        snapshot_kind=str(payload.get("snapshot_kind") or "close"),
                        market_window_open=False,
                    )
                    position_count = int(snapshot.position_count or 0)
                logger.info(
                    "Processed broker positions snapshot from %s: %s positions",
                    conn.name,
                    position_count,
                )
            except Exception as exc:
                logger.exception("Failed to process broker positions snapshot from %s: %s", conn.name, exc)
            return

        logger.debug("Ignored external trading message from %s: %s", conn.name, message)

    async def send_command(
        self,
        account_pk: int,
        action: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout: float = 15.0,
    ) -> Dict[str, Any]:
        send_error = None
        async with self._lock:
            conn = self._connections.get(account_pk)
            if not conn:
                raise ExternalTradingConnectionError("外部交易账号未连接")
            _ensure_ptrade_command_window(action, conn.market_type)

            request_id = uuid.uuid4().hex
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            conn.pending[request_id] = future
            prepared_payload = _prepare_command_payload(action, payload, conn.market_type)
            message = {
                "type": "command",
                "id": request_id,
                "action": action,
                "payload": prepared_payload,
                "ts": datetime.now().isoformat(),
            }

            try:
                await conn.websocket.send_text(encrypt_message(message))
            except Exception as exc:
                conn.pending.pop(request_id, None)
                send_error = exc

        if send_error is not None:
            await self.disconnect(conn.account_pk, conn.connection_id, reason=f"send failed: {send_error}")
            raise ExternalTradingConnectionError(f"发送指令失败: {send_error}") from send_error

        try:
            response = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            conn.pending.pop(request_id, None)
            raise ExternalTradingConnectionError("等待外部交易账号响应超时") from exc

        if not response.get("ok", False):
            error = response.get("error") or response.get("message") or "外部交易账号返回失败"
            raise ExternalTradingConnectionError(str(error))
        return response.get("data") or {}

    async def get_quotes(self, account_pk: int, symbols: List[str], timeout: float = 10.0) -> Dict[str, Any]:
        return await self.send_command(
            account_pk,
            "get_quotes",
            {"symbols": symbols},
            timeout=timeout,
        )

    async def get_snapshots(self, account_pk: int, symbols: List[str], timeout: float = 10.0) -> Dict[str, Any]:
        return await self.send_command(
            account_pk,
            "get_snapshots",
            {"symbols": symbols},
            timeout=timeout,
        )

    async def place_orders(self, account_pk: int, orders: List[Dict[str, Any]], timeout: float = 30.0) -> Dict[str, Any]:
        return await self.send_command(
            account_pk,
            "place_orders",
            {"orders": orders},
            timeout=timeout,
        )

    async def cancel_orders(self, account_pk: int, orders: List[Dict[str, Any]], timeout: float = 15.0) -> Dict[str, Any]:
        return await self.send_command(
            account_pk,
            "cancel_orders",
            {"orders": orders},
            timeout=timeout,
        )

    async def get_account_snapshot(self, account_pk: int, timeout: float = 10.0) -> Dict[str, Any]:
        return await self.send_command(account_pk, "get_account_snapshot", {}, timeout=timeout)

    async def get_positions(self, account_pk: int, timeout: float = 10.0) -> Dict[str, Any]:
        return await self.send_command(account_pk, "get_positions", {}, timeout=timeout)

    async def get_assets(self, account_pk: int, timeout: float = 10.0) -> Dict[str, Any]:
        return await self.send_command(account_pk, "get_assets", {}, timeout=timeout)

    async def get_today_orders(self, account_pk: int, timeout: float = 10.0) -> Dict[str, Any]:
        return await self.send_command(account_pk, "get_today_orders", {}, timeout=timeout)

    async def get_deliver(
        self,
        account_pk: int,
        start_date: str,
        end_date: str,
        timeout: float = 30.0,
    ) -> Dict[str, Any]:
        return await self.send_command(
            account_pk,
            "get_deliver",
            {"start_date": start_date, "end_date": end_date},
            timeout=timeout,
        )

    def get_status(self, account_pk: int) -> Dict[str, Any]:
        conn = self._connections.get(account_pk)
        if not conn:
            return {"connected": False, "pending_count": 0}
        return {
            "connected": True,
            "connected_at": conn.connected_at,
            "last_seen_at": conn.last_seen_at,
            "pending_count": len(conn.pending),
            "connection_id": conn.connection_id,
        }

    def _execute_status_update(self, description: str, sql: str, params: Dict[str, Any]):
        conn = None
        try:
            conn = sqlite3.connect(EXTERNAL_TRADING_DB_PATH, timeout=0.1)
            conn.execute(sql, params)
            conn.commit()
        except sqlite3.OperationalError as exc:
            logger.warning("Skipped external trading status persistence (%s): %s", description, exc)
        except Exception:
            logger.exception("Failed to persist external trading status (%s)", description)
        finally:
            if conn is not None:
                conn.close()

    def _mark_connected(self, account_pk: int):
        now = datetime.now()
        self._last_seen_persisted_at[account_pk] = now
        self._execute_status_update(
            "connected",
            """
            UPDATE external_trading_accounts
            SET last_connected_at = :now,
                last_seen_at = :now,
                last_disconnect_reason = NULL,
                updated_at = :now
            WHERE id = :account_pk
            """,
            {"now": now, "account_pk": account_pk},
        )

    def _mark_seen_if_due(self, account_pk: int, seen_at: datetime):
        last_persisted = self._last_seen_persisted_at.get(account_pk)
        if last_persisted and seen_at - last_persisted < self._seen_persist_interval:
            return
        self._last_seen_persisted_at[account_pk] = seen_at
        self._execute_status_update(
            "seen",
            """
            UPDATE external_trading_accounts
            SET last_seen_at = :seen_at,
                updated_at = :seen_at
            WHERE id = :account_pk
            """,
            {"seen_at": seen_at, "account_pk": account_pk},
        )

    def _mark_disconnected(self, account_pk: int, reason: str):
        now = datetime.now()
        self._last_seen_persisted_at.pop(account_pk, None)
        self._execute_status_update(
            "disconnected",
            """
            UPDATE external_trading_accounts
            SET last_disconnected_at = :now,
                last_disconnect_reason = :reason,
                updated_at = :now
            WHERE id = :account_pk
            """,
            {"now": now, "reason": reason[:500] if reason else None, "account_pk": account_pk},
        )


external_trading_hub = ExternalTradingHub()


def resolve_external_trading_account_pk(
    account_id: str,
    identifier: Optional[str] = None,
    name: Optional[str] = None,
) -> int:
    with get_external_trading_db_ctx() as db:
        query = db.query(ExternalTradingAccount).filter(
            ExternalTradingAccount.account_id == account_id,
            ExternalTradingAccount.enabled == True,
        )
        if identifier:
            query = query.filter(ExternalTradingAccount.identifier == identifier)
        if name:
            query = query.filter(ExternalTradingAccount.name == name)
        account = query.first()
        if not account:
            raise ExternalTradingConnectionError("外部交易账号不存在或未启用")
        return account.id


async def get_external_snapshots(
    account_id: str,
    identifier: str,
    symbols: List[str],
    name: Optional[str] = None,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    account_pk = resolve_external_trading_account_pk(account_id, identifier=identifier, name=name)
    return await external_trading_hub.get_snapshots(account_pk, symbols, timeout=timeout)


async def place_external_orders(
    account_id: str,
    identifier: str,
    orders: List[Dict[str, Any]],
    name: Optional[str] = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    account_pk = resolve_external_trading_account_pk(account_id, identifier=identifier, name=name)
    return await external_trading_hub.place_orders(account_pk, orders, timeout=timeout)


async def cancel_external_orders(
    account_id: str,
    identifier: str,
    orders: List[Dict[str, Any]],
    name: Optional[str] = None,
    timeout: float = 15.0,
) -> Dict[str, Any]:
    account_pk = resolve_external_trading_account_pk(account_id, identifier=identifier, name=name)
    return await external_trading_hub.cancel_orders(account_pk, orders, timeout=timeout)


async def get_external_positions(
    account_id: str,
    identifier: str,
    name: Optional[str] = None,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    account_pk = resolve_external_trading_account_pk(account_id, identifier=identifier, name=name)
    return await external_trading_hub.get_positions(account_pk, timeout=timeout)


async def get_external_assets(
    account_id: str,
    identifier: str,
    name: Optional[str] = None,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    account_pk = resolve_external_trading_account_pk(account_id, identifier=identifier, name=name)
    return await external_trading_hub.get_assets(account_pk, timeout=timeout)


async def get_external_today_orders(
    account_id: str,
    identifier: str,
    name: Optional[str] = None,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    account_pk = resolve_external_trading_account_pk(account_id, identifier=identifier, name=name)
    return await external_trading_hub.get_today_orders(account_pk, timeout=timeout)


async def get_external_deliver(
    account_id: str,
    identifier: str,
    start_date: str,
    end_date: str,
    name: Optional[str] = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    account_pk = resolve_external_trading_account_pk(account_id, identifier=identifier, name=name)
    return await external_trading_hub.get_deliver(account_pk, start_date, end_date, timeout=timeout)

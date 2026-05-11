import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import WebSocket

from ..database import ExternalTradingAccount, get_db_ctx
from .external_trading_crypto import decrypt_message, encrypt_message

logger = logging.getLogger(__name__)


class ExternalTradingConnectionError(Exception):
    """Raised when an external trading account is unavailable or rejects a command."""


@dataclass
class ExternalTradingConnection:
    account_pk: int
    account_id: str
    name: str
    identifier: str
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

    async def connect(self, websocket: WebSocket, account: ExternalTradingAccount) -> ExternalTradingConnection:
        await websocket.accept()
        conn = ExternalTradingConnection(
            account_pk=account.id,
            account_id=account.account_id,
            name=account.name,
            identifier=account.identifier,
            websocket=websocket,
        )

        async with self._lock:
            old_conn = self._connections.get(account.id)
            self._connections[account.id] = conn

        if old_conn:
            await self._finish_connection(
                old_conn,
                reason="replaced by a new WebSocket connection",
                close_code=4000,
            )

        self._mark_connected(account.id)
        await websocket.send_text(encrypt_message({
            "type": "connected",
            "account_id": account.account_id,
            "name": account.name,
            "identifier": account.identifier,
            "connected_at": conn.connected_at.isoformat(),
        }))
        logger.info("External trading account connected: %s/%s", account.account_id, account.name)
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
        self._mark_seen(account_pk, now)

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

            request_id = uuid.uuid4().hex
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            conn.pending[request_id] = future
            message = {
                "type": "command",
                "id": request_id,
                "action": action,
                "payload": payload or {},
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

    async def place_orders(self, account_pk: int, orders: List[Dict[str, Any]], timeout: float = 30.0) -> Dict[str, Any]:
        return await self.send_command(
            account_pk,
            "place_orders",
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

    def _mark_connected(self, account_pk: int):
        now = datetime.now()
        with get_db_ctx() as db:
            account = db.query(ExternalTradingAccount).filter(ExternalTradingAccount.id == account_pk).first()
            if account:
                account.last_connected_at = now
                account.last_seen_at = now
                account.last_disconnect_reason = None

    def _mark_seen(self, account_pk: int, seen_at: datetime):
        with get_db_ctx() as db:
            account = db.query(ExternalTradingAccount).filter(ExternalTradingAccount.id == account_pk).first()
            if account:
                account.last_seen_at = seen_at

    def _mark_disconnected(self, account_pk: int, reason: str):
        now = datetime.now()
        with get_db_ctx() as db:
            account = db.query(ExternalTradingAccount).filter(ExternalTradingAccount.id == account_pk).first()
            if account:
                account.last_disconnected_at = now
                account.last_disconnect_reason = reason[:500] if reason else None


external_trading_hub = ExternalTradingHub()


def resolve_external_trading_account_pk(
    account_id: str,
    identifier: Optional[str] = None,
    name: Optional[str] = None,
) -> int:
    with get_db_ctx() as db:
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


async def get_external_quotes(
    account_id: str,
    identifier: str,
    symbols: List[str],
    name: Optional[str] = None,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    account_pk = resolve_external_trading_account_pk(account_id, identifier=identifier, name=name)
    return await external_trading_hub.get_quotes(account_pk, symbols, timeout=timeout)


async def place_external_orders(
    account_id: str,
    identifier: str,
    orders: List[Dict[str, Any]],
    name: Optional[str] = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    account_pk = resolve_external_trading_account_pk(account_id, identifier=identifier, name=name)
    return await external_trading_hub.place_orders(account_pk, orders, timeout=timeout)


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

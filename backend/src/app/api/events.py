import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder

from ...core.event_stream import EventConnection, event_broker
from ...core.realtime_quotes import realtime_quotes
from .account import is_valid_account


router = APIRouter(prefix="/api/events", tags=["events"])
logger = logging.getLogger(__name__)


async def _handle_control_message(connection: EventConnection, raw: str) -> None:
    """处理前端发来的控制消息（注册/清理实时行情股票池）。

    注册与长连接会话绑定：同一连接下同一 source 全量替换；连接断开时
    backend_events_websocket 的 finally 会 clear_session 整体清理。
    """
    try:
        message: Dict[str, Any] = json.loads(raw)
    except (ValueError, TypeError):
        return
    if not isinstance(message, dict):
        return

    msg_type = message.get("type")
    source = message.get("source")
    if not isinstance(source, str) or not source:
        return

    if msg_type == "watch_register":
        codes = message.get("codes")
        if isinstance(codes, list):
            realtime_quotes.register(
                connection.connection_id,
                source,
                [str(code) for code in codes],
            )
    elif msg_type == "watch_unregister":
        codes = message.get("codes")
        if isinstance(codes, list):
            realtime_quotes.unregister(
                connection.connection_id,
                source,
                [str(code) for code in codes],
            )
        else:
            realtime_quotes.unregister(connection.connection_id, source)


async def _reader(websocket: WebSocket, connection: EventConnection) -> None:
    """下行推送与控制消息并存的 reader：只等客户端控制消息。"""
    while True:
        raw = await websocket.receive_text()
        await _handle_control_message(connection, raw)


async def _writer(websocket: WebSocket, connection: EventConnection) -> None:
    """下行推送：从事件队列取事件推给前端，空 25s 发心跳。"""
    while True:
        try:
            event = await asyncio.wait_for(connection.queue.get(), timeout=25)
        except asyncio.TimeoutError:
            event = {
                "type": "heartbeat",
                "pushed_at": datetime.now().isoformat(),
            }
        await websocket.send_json(jsonable_encoder(event))


@router.websocket("/ws")
async def backend_events_websocket(websocket: WebSocket):
    account_id = websocket.query_params.get("account_id")
    if not account_id or not is_valid_account(account_id):
        await websocket.accept()
        await websocket.close(code=1008, reason="invalid account_id")
        return

    await websocket.accept()
    connection = await event_broker.connect(account_id)
    reader_task: Optional[asyncio.Task] = None
    writer_task: Optional[asyncio.Task] = None
    try:
        await websocket.send_json({
            "type": "connected",
            "pushed_at": datetime.now().isoformat(),
        })
        reader_task = asyncio.create_task(_reader(websocket, connection))
        writer_task = asyncio.create_task(_writer(websocket, connection))
        done, pending = await asyncio.wait(
            {reader_task, writer_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        # 一侧结束（断线/异常）则取消另一侧，避免任务泄漏。
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, (asyncio.CancelledError, WebSocketDisconnect)):
                raise exc
    except WebSocketDisconnect:
        return
    except Exception as exc:
        logger.exception("Backend events WebSocket failed: %s", exc)
        try:
            await websocket.close(code=1011, reason="events websocket error")
        except Exception:
            pass
    finally:
        # 长连接断开：清理该会话注册的实时行情股票（与池绑定）
        realtime_quotes.clear_session(connection.connection_id)
        await event_broker.disconnect(connection)

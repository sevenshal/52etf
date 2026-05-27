import asyncio
import logging
from datetime import datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder

from ...core.event_stream import event_broker
from .account import is_valid_account


router = APIRouter(prefix="/api/events", tags=["events"])
logger = logging.getLogger(__name__)


@router.websocket("/ws")
async def backend_events_websocket(websocket: WebSocket):
    account_id = websocket.query_params.get("account_id")
    if not account_id or not is_valid_account(account_id):
        await websocket.accept()
        await websocket.close(code=1008, reason="invalid account_id")
        return

    await websocket.accept()
    connection = await event_broker.connect(account_id)
    try:
        await websocket.send_json({
            "type": "connected",
            "pushed_at": datetime.now().isoformat(),
        })
        while True:
            try:
                event = await asyncio.wait_for(connection.queue.get(), timeout=25)
            except asyncio.TimeoutError:
                event = {
                    "type": "heartbeat",
                    "pushed_at": datetime.now().isoformat(),
                }
            await websocket.send_json(jsonable_encoder(event))
    except WebSocketDisconnect:
        return
    except Exception as exc:
        logger.exception("Backend events WebSocket failed: %s", exc)
        try:
            await websocket.close(code=1011, reason="events websocket error")
        except Exception:
            pass
    finally:
        await event_broker.disconnect(connection)

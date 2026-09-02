import asyncio
import json
import logging
import math
from datetime import datetime
from typing import Any, Dict, Optional

import pandas as pd
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder

from ...core.event_stream import EventConnection, event_broker
from ...core.realtime_quotes import realtime_quotes
from ...core.services.tushare import TushareService
from .account import is_valid_account


router = APIRouter(prefix="/api/events", tags=["events"])
logger = logging.getLogger(__name__)


def _quote_number(value: Any) -> Optional[float]:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


async def _fetch_missing_tushare_quotes(codes: list[str]) -> Dict[str, Dict[str, Any]]:
    """用 Tushare 实时日线补齐注册时内存中缺失的展示行情。

    不过滤非当日数据：这里用于页面展示，休市/未开盘时最近交易日的收盘价和
    开盘价比空值更有用；交易执行场景仍由 valuation 模块按日期严格过滤。
    """
    if not codes:
        return {}

    def _fetch() -> Dict[str, Dict[str, Any]]:
        service = TushareService.get_instance()
        frames = []
        for frame in (
            service.get_a_stock_realtime_rt_k_frame(codes),
            service.get_a_stock_realtime_etf_rt_k_frame(codes),
        ):
            if isinstance(frame, pd.DataFrame) and not frame.empty:
                frames.append(frame)
        if not frames:
            return {}

        result: Dict[str, Dict[str, Any]] = {}
        merged = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["ts_code"], keep="first")
        for _, row in merged.iterrows():
            code = str(row.get("ts_code") or "").strip().upper()
            last_px = _quote_number(row.get("close"))
            if not code or not last_px or last_px <= 0:
                continue
            trade_time = row.get("trade_time")
            parsed_time = pd.to_datetime(trade_time, errors="coerce")
            result[code] = {
                "last_px": last_px,
                "preclose_px": _quote_number(row.get("pre_close")),
                "open_px": _quote_number(row.get("open")),
                "high_px": _quote_number(row.get("high")),
                "low_px": _quote_number(row.get("low")),
                "volume": _quote_number(row.get("vol")),
                "amount": _quote_number(row.get("amount")),
                "hs_time": None if pd.isna(parsed_time) else parsed_time.isoformat(),
                "source": "tushare_rt",
            }
        return result

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warning("Tushare quote backfill failed for realtime registration: %s", exc)
        return {}


def _enqueue_quote_snapshot(connection: EventConnection, quotes: Dict[str, Dict[str, Any]]) -> None:
    if not quotes:
        return
    event_broker._enqueue(connection.queue, {
        "type": "realtime_quotes",
        "pushed_at": datetime.now().isoformat(),
        "ts": datetime.now().isoformat(),
        "quotes": quotes,
    })


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
            normalized_codes = [str(code) for code in codes]
            realtime_quotes.register(
                connection.connection_id,
                source,
                normalized_codes,
            )
            # 注册后先立即恢复已有内存快照，再仅为缺失标的请求 Tushare。
            cached = realtime_quotes.snapshot(normalized_codes)
            _enqueue_quote_snapshot(connection, cached)
            missing = [code for code in normalized_codes if not realtime_quotes.quote(code)]
            if missing:
                fetched = await _fetch_missing_tushare_quotes(missing)
                filled = realtime_quotes.update_missing_quotes(fetched)
                _enqueue_quote_snapshot(connection, filled)
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

"""实时行情 API：PTrade 桥接上报 + 调试状态。

- POST /api/realtime/pool：PTrade 唯一接口。请求携带 tick 报价（可空，用于引导
  拉取最新池），响应返回最新股票池 + pool_version，PTrade 据此 set_universe 跟随。
- GET /api/realtime/status：管理员调试用，查看当前池/版本/缓存大小。

注册/清理不走 REST，走 /api/events/ws 长连接控制消息（watch_register / watch_unregister），
与 WebSocket 会话绑定，断线自动清理。
"""

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from ...core.event_stream import publish_event
from ...core.realtime_quotes import PTRADE_BRIDGE_ACCOUNT_ID, normalize_code, realtime_quotes
from .account import valid_admin_account

router = APIRouter(prefix="/api/realtime", tags=["realtime"])
logger = logging.getLogger(__name__)


class QuotePayload(BaseModel):
    ts: Optional[str] = None
    quotes: Dict[str, Dict[str, Any]] = {}


async def require_ptrade_bridge(x_account_id: Optional[str] = Header(None)) -> str:
    """PTrade 桥接专用账号校验。

    账号 id 写死且由后端控制，纯内存比较，零 DB 开销（PTrade 每 3s 上报一次，
    热路径不应碰 SQLite）。要吊销/更换时改 PTRADE_BRIDGE_ACCOUNT_ID 并重新部署。
    """
    if not x_account_id:
        raise HTTPException(status_code=401, detail="Missing account ID")
    if x_account_id != PTRADE_BRIDGE_ACCOUNT_ID:
        raise HTTPException(status_code=403, detail="仅限 PTrade 桥接账号")
    return x_account_id


@router.post("/pool")
def report_pool(payload: QuotePayload, _: str = Depends(require_ptrade_bridge)):
    now_ts = payload.ts or datetime.now().isoformat()
    quotes = payload.quotes or {}
    normalized: Dict[str, Dict[str, Any]] = {}
    for code, quote in quotes.items():
        ncode = normalize_code(code)
        if ncode:
            normalized[ncode] = dict(quote or {})
    if normalized:
        realtime_quotes.update_quotes(normalized)
        publish_event(None, "realtime_quotes", {"ts": now_ts, "quotes": normalized})
    return {
        "ts": now_ts,
        "pool": realtime_quotes.pool(),
        "pool_version": realtime_quotes.pool_version,
    }


@router.get("/status")
def realtime_status(_: str = Depends(valid_admin_account)):
    return realtime_quotes.status()

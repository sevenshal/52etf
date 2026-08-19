import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.app.api import realtime as realtime_api
from src.app.api import events as events_api
from src.app.api.account import ADMIN_ACCOUNT_ID
from src.core.realtime_quotes import realtime_quotes, PTRADE_BRIDGE_ACCOUNT_ID, MAX_POOL_SIZE


def _clear_sessions():
    for session_id in list(realtime_quotes.status()["sessions"]):
        realtime_quotes.clear_session(session_id)


def test_report_pool_auth_and_response():
    app = FastAPI()
    app.include_router(realtime_api.router)
    client = TestClient(app)

    # 无账号头 → 401
    assert client.post("/api/realtime/pool", json={"quotes": {}}).status_code == 401

    # 任意非桥接账号（含不存在/普通账号）→ 403（纯内存比较，不查 DB）
    resp = client.post(
        "/api/realtime/pool",
        json={"quotes": {}},
        headers={"X-Account-ID": "no-such-account"},
    )
    assert resp.status_code == 403

    resp = client.post(
        "/api/realtime/pool",
        json={"quotes": {}},
        headers={"X-Account-ID": ADMIN_ACCOUNT_ID},
    )
    assert resp.status_code == 403

    # 专用桥接账号 → 200，返回池与版本
    resp = client.post(
        "/api/realtime/pool",
        json={"ts": "2026-08-13 10:00:00", "quotes": {"600000.SS": {"last_px": 10.5}}},
        headers={"X-Account-ID": PTRADE_BRIDGE_ACCOUNT_ID},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["pool"], list)
    assert isinstance(body["pool_version"], int)
    # 上报的 .SS 被归一化成 .SH 并进入行情缓存
    assert realtime_quotes.quote("600000.SH")["last_px"] == 10.5


def test_ws_watch_register_and_disconnect_cleanup():
    app = FastAPI()
    app.include_router(events_api.router)
    client = TestClient(app)
    _clear_sessions()

    with client.websocket_connect(
        f"/api/events/ws?account_id={ADMIN_ACCOUNT_ID}"
    ) as ws:
        assert ws.receive_json()["type"] == "connected"
        ws.send_json({
            "type": "watch_register",
            "source": "ai_stock_page",
            "codes": ["600000.SS", "000001.SZ"],
        })
        time.sleep(0.3)
        assert "600000.SH" in realtime_quotes.pool()
        assert "000001.SZ" in realtime_quotes.pool()

        # 同 source 全量替换
        ws.send_json({
            "type": "watch_register",
            "source": "ai_stock_page",
            "codes": ["600001.SH"],
        })
        time.sleep(0.3)
        assert realtime_quotes.pool() == ["600001.SH"]

    # 连接断开 → 该会话注册被清理
    time.sleep(0.3)
    assert realtime_quotes.pool() == []


def test_max_pool_size_constant_is_sane():
    assert MAX_POOL_SIZE == 300

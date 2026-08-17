#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""52ETF WS 连接探测: 验证握手签名 + connected + 心跳 pong, 不依赖 xtquant"""
import json, time, sys
sys.path.insert(0, r"C:\qmt")
from qmt_client import (
    build_ws_url, decrypt_message, encrypt_message, log,
    DEFAULT_ACCOUNT_ID, DEFAULT_IDENTIFIER,
)
from tornado import ioloop, websocket

def main():
    log.info("WS 探测: account=%s identifier=%s", DEFAULT_ACCOUNT_ID, DEFAULT_IDENTIFIER)
    loop = ioloop.IOLoop()
    loop.make_current()
    url = build_ws_url(DEFAULT_ACCOUNT_ID, DEFAULT_IDENTIFIER)
    log.info("连接: %s", url)
    future = websocket.websocket_connect(url, connect_timeout=10)
    conn = loop.run_sync(lambda: future)
    log.info("已连接, 等待 connected...")

    # 等 connected
    msg = loop.run_sync(lambda: conn.read_message())
    m = decrypt_message(msg)
    log.info("收到消息: type=%s", m.get("type"))
    if m.get("type") == "connected":
        log.info("✅ 后端接受连接: name=%s identifier=%s market_type=%s",
                 m.get("name"), m.get("identifier"), m.get("market_type"))
    else:
        log.info("非预期: %s", m)
        return 1

    # 心跳
    conn.write_message(encrypt_message({"type": "heartbeat", "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}))
    msg2 = loop.run_sync(lambda: conn.read_message())
    m2 = decrypt_message(msg2)
    log.info("收到消息: type=%s", m2.get("type"))
    if m2.get("type") == "pong":
        log.info("✅ 心跳正常 (pong)")
    else:
        log.info("非预期: %s", m2)
        return 1
    conn.close()
    log.info("WS 探测完成 ✅")
    return 0

if __name__ == "__main__":
    sys.exit(main())

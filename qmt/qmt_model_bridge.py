#!/usr/bin/env python3
"""Network bridge for the SQLite-backed QMT model client."""

import hashlib
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qmt_client as protocol
import websocket


IPC_ROOT = r"C:\Users\sevenshal\qmt\ipc"
DB_PATH = os.path.join(IPC_ROOT, "qmt_queue.sqlite3")
HOST = "192.168.71.3:8080"
EXTERNAL_ACCOUNT_ID = "amNDUzNWU4OTA4NjcyZWYwMGYyZDMyYzQzNDFjYjAwOTUK"


def log(message):
    print("%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message), flush=True)


def connect_db():
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.execute("PRAGMA busy_timeout=10000")
    return db


def init_db():
    os.makedirs(IPC_ROOT, exist_ok=True)
    with connect_db() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS commands (
                request_id TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                received_at REAL NOT NULL,
                started_at REAL,
                completed_at REAL,
                last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS responses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedupe_key TEXT NOT NULL UNIQUE,
                request_id TEXT,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at REAL NOT NULL,
                sent_at REAL
            );
            CREATE TABLE IF NOT EXISTS order_intents (
                client_order_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL,
                order_index INTEGER NOT NULL,
                request_json TEXT NOT NULL,
                state TEXT NOT NULL,
                broker_order_id TEXT,
                result_json TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_commands_status_received
                ON commands(status, received_at);
            CREATE INDEX IF NOT EXISTS ix_responses_status_id
                ON responses(status, id);
        """)


def _client_order_id(request_id, index):
    raw = (str(request_id) + ":" + str(index)).encode("utf-8")
    return "52" + hashlib.sha256(raw).hexdigest()[:30]


def normalize_command(message):
    request_id = str(message.get("id") or "")
    if not request_id:
        raise ValueError("command id is required")
    payload = message.get("payload") or {}
    if message.get("action") in ("place_orders", "order.batch"):
        for index, order in enumerate(payload.get("orders") or []):
            if not order.get("client_order_id"):
                order["client_order_id"] = _client_order_id(request_id, index)
            order["_qmt_remark"] = "52" + hashlib.sha256(
                str(order["client_order_id"]).encode("utf-8")
            ).hexdigest()[:30]
    return request_id, str(message.get("action") or ""), payload


def enqueue_command(message):
    request_id, action, payload = normalize_command(message)
    with connect_db() as db:
        cursor = db.execute(
            "INSERT OR IGNORE INTO commands "
            "(request_id, action, payload_json, status, received_at) "
            "VALUES (?, ?, ?, 'pending', ?)",
            (request_id, action, json.dumps(payload, ensure_ascii=False), time.time()),
        )
        if cursor.rowcount:
            log("queued command id=%s action=%s" % (request_id, action))
        else:
            db.execute(
                "UPDATE responses SET status='pending', sent_at=NULL "
                "WHERE dedupe_key=?", ("result:" + request_id,)
            )
            log("ignored duplicate command id=%s" % request_id)


def flush_outgoing(ws):
    with connect_db() as db:
        rows = db.execute(
            "SELECT id, payload_json FROM responses "
            "WHERE status='pending' ORDER BY id LIMIT 100"
        ).fetchall()
    for row_id, payload_json in rows:
        ws.send(protocol.encrypt_message(json.loads(payload_json)))
        with connect_db() as db:
            db.execute(
                "UPDATE responses SET status='sent', sent_at=? "
                "WHERE id=? AND status='pending'", (time.time(), row_id)
            )


def run_once():
    protocol.USE_HTTPS = False
    url = protocol.build_ws_url(EXTERNAL_ACCOUNT_ID, protocol.DEFAULT_IDENTIFIER, HOST)
    log("connecting %s" % HOST)
    ws = websocket.create_connection(url, timeout=10)
    ws.settimeout(1)
    log("connected")
    last_heartbeat = 0
    try:
        while True:
            flush_outgoing(ws)
            now = time.time()
            if now - last_heartbeat >= 10:
                ws.send(protocol.encrypt_message({"type": "heartbeat", "ts": now}))
                last_heartbeat = now
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not raw:
                raise RuntimeError("connection closed")
            message = protocol.decrypt_message(raw)
            if message.get("type") == "command":
                enqueue_command(message)
    finally:
        ws.close()


def main():
    init_db()
    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            return
        except Exception as exc:
            log("disconnected: %s" % exc)
            time.sleep(5)


if __name__ == "__main__":
    main()

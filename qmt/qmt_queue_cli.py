#!/usr/bin/env python3
"""Small SQLite RPC used by QMT's restricted embedded Python runtime."""

import json
import os
import sqlite3
import sys
import time


IPC_ROOT = r"C:\Users\sevenshal\qmt\ipc"
DB_PATH = os.path.join(IPC_ROOT, "qmt_queue.sqlite3")


def connect_db():
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.execute("PRAGMA busy_timeout=10000")
    return db


def read_json(path):
    with open(path, "r", encoding="utf-8") as stream:
        return json.load(stream)


def write_json(path, payload):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False)
    os.replace(tmp_path, path)


def claim(output_path):
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT request_id, action, payload_json FROM commands "
            "WHERE status='pending' ORDER BY received_at LIMIT 1"
        ).fetchone()
        if not row:
            db.commit()
            write_json(output_path, {})
            return
        changed = db.execute(
            "UPDATE commands SET status='processing', attempts=attempts+1, started_at=? "
            "WHERE request_id=? AND status='pending'", (time.time(), row[0])
        ).rowcount
        db.commit()
    if changed:
        write_json(output_path, {"id": row[0], "action": row[1],
                                 "payload": json.loads(row[2])})
    else:
        write_json(output_path, {})


def complete(input_path):
    item = read_json(input_path)
    request_id = str(item["id"])
    payload = item["response"]
    ok = bool(payload.get("ok"))
    with connect_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO responses "
            "(dedupe_key, request_id, payload_json, status, created_at) "
            "VALUES (?, ?, ?, 'pending', ?)",
            ("result:" + request_id, request_id,
             json.dumps(payload, ensure_ascii=False), time.time()),
        )
        db.execute(
            "UPDATE commands SET status=?, completed_at=?, last_error=? "
            "WHERE request_id=?",
            ("done" if ok else "failed", time.time(),
             None if ok else str(payload.get("error") or "unknown error"), request_id),
        )


def prepare_order(input_path, output_path):
    item = read_json(input_path)
    client_order_id = str(item["client_order_id"])
    now = time.time()
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT state, broker_order_id, result_json FROM order_intents "
            "WHERE client_order_id=?", (client_order_id,)
        ).fetchone()
        if not row:
            db.execute(
                "INSERT INTO order_intents "
                "(client_order_id, request_id, order_index, request_json, state, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, 'submitting', ?, ?)",
                (client_order_id, str(item["request_id"]), int(item["order_index"]),
                 json.dumps(item["request"], ensure_ascii=False), now, now),
            )
            db.commit()
            result = {"state": "new"}
        else:
            db.commit()
            result = {"state": row[0], "broker_order_id": row[1],
                      "result": json.loads(row[2]) if row[2] else None}
    write_json(output_path, result)


def finish_order(input_path):
    item = read_json(input_path)
    with connect_db() as db:
        db.execute(
            "UPDATE order_intents SET state=?, broker_order_id=?, result_json=?, "
            "updated_at=? WHERE client_order_id=?",
            (str(item.get("state") or "submitted"), item.get("broker_order_id"),
             json.dumps(item.get("result"), ensure_ascii=False), time.time(),
             str(item["client_order_id"])),
        )


def event(input_path):
    item = read_json(input_path)
    with connect_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO responses "
            "(dedupe_key, request_id, payload_json, status, created_at) "
            "VALUES (?, NULL, ?, 'pending', ?)",
            (str(item["dedupe_key"]),
             json.dumps(item["payload"], ensure_ascii=False), time.time()),
        )


def recover():
    # Called when the QMT model starts. At that point no previous model worker
    # can still be executing, so abandoned claims are safe to replay.
    with connect_db() as db:
        db.execute("UPDATE commands SET status='pending', started_at=NULL "
                   "WHERE status='processing'")


def main():
    operation = sys.argv[1]
    if operation == "claim":
        claim(sys.argv[2])
    elif operation == "complete":
        complete(sys.argv[2])
    elif operation == "prepare-order":
        prepare_order(sys.argv[2], sys.argv[3])
    elif operation == "finish-order":
        finish_order(sys.argv[2])
    elif operation == "event":
        event(sys.argv[2])
    elif operation == "recover":
        recover()
    else:
        raise SystemExit("unknown operation: " + operation)


if __name__ == "__main__":
    main()

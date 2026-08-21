# -*- coding: utf-8 -*-
"""52ETF QMT普通模式策略客户端。

使用方法：将本文件作为QMT Python策略导入，以实盘模式运行，并选择资金账号。
网络与加密由外部桥接进程负责；本策略仅通过SQLite队列调用QMT交易API。
"""

import json
import os
import sys
import time
import traceback

ACCOUNT_ID = "40600083"
ACCOUNT_TYPE = "STOCK"
STRATEGY_NAME = "52etf-model"
TIMER_PERIOD = "1nSecond"
IPC_ROOT = r"C:\Users\sevenshal\qmt\ipc"
DB_PATH = os.path.join(IPC_ROOT, "qmt_queue.sqlite3")
RPC_ROOT = os.path.join(IPC_ROOT, "rpc")
BRIDGE_PYTHON = r"C:\Users\sevenshal\qmt\python310\pythonw.exe"
QUEUE_CLI = r"C:\Users\sevenshal\qmt\qmt_queue_cli.py"

_started = False
_last_order_snapshot = {}


def _log(message):
    print("[52ETF] %s" % message)


def _attr(obj, *names):
    for name in names:
        try:
            value = getattr(obj, name)
            if value is not None:
                return value
        except Exception:
            pass
    return None


def _num(value, default=0):
    try:
        return float(value)
    except Exception:
        return default


def _symbol(obj):
    code = str(_attr(obj, "m_strInstrumentID", "stock_code") or "")
    exchange = str(_attr(obj, "m_strExchangeID") or "").upper()
    if "." not in code and exchange in ("SH", "SZ", "BJ"):
        code += "." + exchange
    return code


def _side(obj):
    value = _attr(obj, "m_nDirection", "m_eEntrustBS", "m_nOffsetFlag")
    return "BUY" if value in (0, 23, "0", "23", "BUY") else "SELL"


def _query(kind, strategy_name=None):
    if strategy_name:
        return get_trade_detail_data(ACCOUNT_ID, ACCOUNT_TYPE, kind, strategy_name) or []
    return get_trade_detail_data(ACCOUNT_ID, ACCOUNT_TYPE, kind) or []


def _positions_payload():
    items = []
    for pos in _query("POSITION"):
        quantity = _num(_attr(pos, "m_nVolume", "m_nPosition"))
        available = _num(_attr(pos, "m_nCanUseVolume", "m_nAvailableVolume", "m_nCanUseVol"), quantity)
        items.append({
            "symbol": _symbol(pos),
            "client_symbol": _symbol(pos),
            "quantity": quantity,
            "available_quantity": available,
            "sellable_quantity": available,
            "avg_cost": _num(_attr(pos, "m_dOpenPrice", "m_dCostPrice")),
            "market_value": _num(_attr(pos, "m_dMarketValue", "m_dInstrumentValue")),
        })
    return {"current_time": time.strftime("%Y-%m-%d %H:%M:%S"), "positions": items}


def _assets_payload():
    accounts = _query("ACCOUNT")
    obj = accounts[0] if accounts else None
    available = _num(_attr(obj, "m_dAvailable", "m_dAvailableCash")) if obj else 0
    frozen = _num(_attr(obj, "m_dFrozenCash", "m_dFrozen")) if obj else 0
    total = _num(_attr(obj, "m_dBalance", "m_dAsset", "m_dTotalAsset")) if obj else 0
    market_value = _num(_attr(obj, "m_dMarketValue", "m_dStockValue")) if obj else 0
    return {"current_time": time.strftime("%Y-%m-%d %H:%M:%S"), "assets": {
        "portfolio_value": total,
        "available_cash": available,
        "locked_cash": frozen,
        "total_cash": available + frozen,
        "total_positions_value": market_value,
    }}


def _normalize_order(order):
    order_id = _attr(order, "m_strOrderSysID", "m_strEntrustNo", "m_nOrderID")
    status = _attr(order, "m_nOrderStatus", "m_eOrderStatus", "m_eEntrustStatus")
    client_order_id = str(_attr(order, "m_strRemark", "order_remark") or "")
    return {
        "client_order_id": client_order_id,
        "order_id": str(order_id or ""),
        "entrust_no": str(order_id or ""),
        "symbol": _symbol(order),
        "client_symbol": _symbol(order),
        "side": _side(order),
        "quantity": _num(_attr(order, "m_nVolume", "m_nEntrustVol")),
        "price": _num(_attr(order, "m_dPrice", "m_dEntrustPrice")),
        "filled_quantity": _num(_attr(order, "m_nTradedVolume", "m_nBusinessVol")),
        "avg_fill_price": _num(_attr(order, "m_dTradedPrice", "m_dBusinessPrice")),
        "status": str(status if status is not None else ""),
        "raw_status": str(status if status is not None else ""),
        "submitted_at": str(_attr(order, "m_strInsertTime", "m_strEntrustTime") or ""),
    }


def _orders_payload():
    return {"current_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "orders": [_normalize_order(item) for item in _query("ORDER")]}


def _quotes_payload(context, symbols):
    ticks = context.get_full_tick(symbols) or {}
    quotes = []
    for symbol in symbols:
        tick = ticks.get(symbol) or {}
        bids = tick.get("bidPrice") or []
        asks = tick.get("askPrice") or []
        bid_vol = tick.get("bidVol") or []
        ask_vol = tick.get("askVol") or []
        quotes.append({
            "ok": bool(tick), "symbol": symbol, "client_symbol": symbol,
            "price": tick.get("lastPrice"),
            "bid": bids[0] if bids else None,
            "bid_size": bid_vol[0] if bid_vol else None,
            "ask": asks[0] if asks else None,
            "ask_size": ask_vol[0] if ask_vol else None,
            "trade_status": str(tick.get("stockStatus", "")),
            "timestamp": str(int(time.time() * 1000)),
        })
    return {"quotes": quotes}


def _find_order_by_client_id(client_order_id, qmt_remark=None):
    target = str(qmt_remark or client_order_id or "")[:32]
    if not target:
        return None
    for order in _query("ORDER"):
        remark = str(_attr(order, "m_strRemark", "order_remark") or "")
        if remark == target:
            return _normalize_order(order)
    return None


def _write_json(path, payload):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as stream:
        json.dump(payload, stream, ensure_ascii=False)
    os.replace(tmp_path, path)


def _run_cli(operation, input_path=None, output_path=None):
    parts = [BRIDGE_PYTHON, QUEUE_CLI, operation]
    if input_path:
        parts.append(input_path)
    if output_path:
        parts.append(output_path)
    code = os.spawnv(os.P_WAIT, BRIDGE_PYTHON, parts)
    if code != 0:
        raise Exception("SQLite queue helper failed: %s code=%s" % (operation, code))


def _prepare_order(request_id, order_index, request):
    token = str(request.get("client_order_id"))[:32]
    input_path = os.path.join(RPC_ROOT, "prepare-%s-in.json" % token)
    output_path = os.path.join(RPC_ROOT, "prepare-%s-out.json" % token)
    _write_json(input_path, {
        "client_order_id": request.get("client_order_id"),
        "request_id": request_id,
        "order_index": order_index,
        "request": request,
    })
    if os.path.exists(output_path):
        os.remove(output_path)
    _run_cli("prepare-order", input_path, output_path)
    with open(output_path, "r") as stream:
        return json.load(stream)


def _finish_order(client_order_id, broker_order_id, result, state="submitted"):
    token = str(client_order_id)[:32]
    input_path = os.path.join(RPC_ROOT, "finish-%s.json" % token)
    _write_json(input_path, {
        "client_order_id": client_order_id,
        "broker_order_id": broker_order_id,
        "state": state,
        "result": result,
    })
    _run_cli("finish-order", input_path)


def _place_order(context, request, request_id, order_index):
    symbol = str(request.get("symbol") or "")
    side = str(request.get("side") or "").upper()
    quantity = int(request.get("quantity") or 0)
    client_order_id = str(request.get("client_order_id") or "")
    qmt_remark = str(request.get("_qmt_remark") or client_order_id)[:32]
    price = request.get("price") or request.get("limit_price")
    if not client_order_id:
        raise Exception("client_order_id is required")
    if not symbol or side not in ("BUY", "SELL") or quantity <= 0:
        raise Exception("invalid order")
    if not price:
        quote = _quotes_payload(context, [symbol])["quotes"][0]
        price = quote.get("ask") if side == "BUY" else quote.get("bid")
    if not price:
        raise Exception("quote unavailable")

    intent = _prepare_order(request_id, order_index, request)
    if intent.get("result"):
        return intent["result"]

    existing = _find_order_by_client_id(client_order_id, qmt_remark)
    if existing:
        result = {"ok": True, "status": "DEDUPLICATED", "deduplicated": True,
                  "client_order_id": client_order_id,
                  "order_id": existing.get("order_id"),
                  "entrust_no": existing.get("entrust_no"),
                  "symbol": existing.get("symbol") or symbol,
                  "side": existing.get("side") or side,
                  "quantity": existing.get("quantity") or quantity,
                  "submitted_quantity": existing.get("quantity") or quantity,
                  "calculated_price": existing.get("price")}
        _finish_order(client_order_id, existing.get("order_id"), result)
        return result

    if intent.get("state") != "new":
        return {"ok": False, "status": "AMBIGUOUS_NOT_RESUBMITTED",
                "deduplicated": True, "client_order_id": client_order_id,
                "symbol": symbol, "side": side, "quantity": quantity,
                "message": "订单意图已执行过但暂未查到委托，已阻止重复下单"}
    op_type = 23 if side == "BUY" else 24
    # 1101=按股数，11=指定价；定时器仅在实盘触发，quickTrade=2立即发单。
    passorder(op_type, 1101, ACCOUNT_ID, symbol, 11, float(price), quantity,
              STRATEGY_NAME, 2, qmt_remark, context)
    submitted = _find_order_by_client_id(client_order_id, qmt_remark)
    order_id = (submitted or {}).get("order_id")
    if not order_id:
        order_id = get_last_order_id(ACCOUNT_ID, ACCOUNT_TYPE, "ORDER", STRATEGY_NAME)
    result = {"ok": True, "status": "SUBMITTED", "client_order_id": client_order_id,
              "order_id": str(order_id), "entrust_no": str(order_id), "symbol": symbol,
              "side": side, "quantity": quantity, "submitted_quantity": quantity,
              "calculated_price": float(price), "price_source": "qmt_full_tick"}
    _finish_order(client_order_id, str(order_id), result)
    return result


def _cancel_order(context, item):
    if isinstance(item, dict):
        order_id = item.get("order_id")
        client_order_id = item.get("client_order_id")
    else:
        order_id, client_order_id = item, None
    ok = cancel(str(order_id), ACCOUNT_ID, ACCOUNT_TYPE, context)
    return {"ok": bool(ok), "order_id": str(order_id),
            "client_order_id": client_order_id, "status": "CANCEL_REQUESTED" if ok else "FAILED"}


def _execute(context, action, payload, request_id):
    if action in ("get_quotes", "get_bid_ask", "quote.batch", "get_snapshots", "snapshot.batch", "quote.snapshot"):
        return _quotes_payload(context, payload.get("symbols") or [])
    if action in ("get_positions", "positions"):
        return _positions_payload()
    if action in ("get_assets", "assets"):
        return _assets_payload()
    if action in ("get_today_orders", "today_orders", "orders.today"):
        return _orders_payload()
    if action in ("get_account_snapshot", "account.snapshot"):
        result = _positions_payload()
        result.update(_assets_payload())
        result.update(_orders_payload())
        return result
    if action in ("place_orders", "order.batch"):
        return {"orders": [_place_order(context, item, request_id, index)
                           for index, item in enumerate(payload.get("orders") or [])]}
    if action in ("cancel_orders", "order.cancel"):
        items = payload.get("orders") or payload.get("order_ids") or []
        return {"orders": [_cancel_order(context, item) for item in items]}
    raise Exception("unsupported action: %s" % action)


def _claim_command():
    output_path = os.path.join(RPC_ROOT, "claim.json")
    if os.path.exists(output_path):
        os.remove(output_path)
    _run_cli("claim", output_path)
    with open(output_path, "r") as stream:
        command = json.load(stream)
    return command or None


def _complete_command(request_id, response):
    input_path = os.path.join(RPC_ROOT, "complete-%s.json" % str(request_id))
    _write_json(input_path, {"id": request_id, "response": response})
    _run_cli("complete", input_path)


def _queue_event(dedupe_key, payload):
    input_path = os.path.join(RPC_ROOT, "event.json")
    _write_json(input_path, {"dedupe_key": dedupe_key, "payload": payload})
    _run_cli("event", input_path)


def init(ContextInfo):
    global _started
    for path in (IPC_ROOT, RPC_ROOT):
        if not os.path.isdir(path):
            os.makedirs(path)
    _run_cli("recover")
    ContextInfo.set_account(ACCOUNT_ID)
    ContextInfo.run_time("poll_52etf", TIMER_PERIOD, "2000-01-01 00:00:00", "SH")
    _started = True
    _log("普通模式SQLite客户端初始化完成 account=%s" % ACCOUNT_ID)


def poll_52etf(ContextInfo):
    global _last_order_snapshot
    if not _started:
        return
    for unused in range(20):
        command = _claim_command()
        if not command:
            break
        request_id = command.get("id")
        try:
            data = _execute(ContextInfo, command.get("action"),
                            command.get("payload") or {}, request_id)
            result = {"type": "result", "id": request_id, "ok": True,
                      "data": data, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
        except Exception as exc:
            traceback.print_exc()
            result = {"type": "result", "id": request_id, "ok": False,
                      "error": str(exc), "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
        _complete_command(request_id, result)
    try:
        orders = _orders_payload()["orders"]
        current = {item["order_id"]: (item["status"], item["filled_quantity"]) for item in orders}
        if current != _last_order_snapshot:
            event = {"type": "order_event", "source": "model_status_sync",
                     "orders": orders, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
            _queue_event("orders:" + repr(sorted(current.items())), event)
            _last_order_snapshot = current
    except Exception:
        traceback.print_exc()


def handlebar(ContextInfo):
    # 定时器在实盘模式驱动；保留handlebar满足QMT策略模型接口要求。
    pass


def stop(ContextInfo):
    _log("普通模式客户端已停止")

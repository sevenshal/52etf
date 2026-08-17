#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
52ETF 国金 QMT 外部交易客户端 (Windows)

架构:
  52ETF 后端 (api.52etf.vip)
      ↑ WebSocket 长连接 (RSA 握手签名 + ChaCha20 加密信封, 客户端主动连出)
  [本机 MiniQMT 客户端 (极简模式, 58610)]
      ↑ xtquant (localhost)
  qmt_client.py 运行在 QMT 所在 Windows 机器上

依赖: pip install xtquant tornado
运行: python qmt_client.py [--config qmt_config.json]
"""

import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import queue
import struct
import sys
import threading
import time
from datetime import datetime, date, timedelta
from urllib.parse import quote

# ---------------------------------------------------------------------------
# 配置（可用 --config JSON 覆盖）
# ---------------------------------------------------------------------------
USE_HTTPS = False
API_HOST = "api.52etf.vip"

# 52ETF 外部交易账号（管理端"外部交易账号"页创建）
DEFAULT_ACCOUNT_ID = "vNKpHJkLMnBQRSTUVWXYZabcdefghijkl"
DEFAULT_IDENTIFIER = "GJ40600083"

# QMT / xtquant 连接
QMT_DATA_PATH = r"C:\qjzq_qmt_trader\userdata"  # miniQMT userdata 目录
QMT_ACCOUNT_ID = "40600083"                     # 资金账号
QMT_SESSION_ID = 9501

HEARTBEAT_INTERVAL_SECONDS = 10
RECONNECT_DELAY_SECONDS = 5
COMMAND_QUEUE_TIMEOUT_SECONDS = 120
ORDER_ACK_WAIT_SECONDS = 5.0     # 下单后等待异步回报(拿 order_id)的秒数
QUERY_WAIT_SECONDS = 10.0        # 查询类命令等待结果秒数
ORDER_STATUS_SYNC_INTERVAL = 30  # 盘中订单状态轮询同步秒数
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qmt_client.log")

# RSA 私钥 + 共享密钥（与 ptrade_client.py 一致，52ETF 提供）
RSA_N_HEX = (
    "d10e83e0f75ddef1fa41d524bbf4ff76dc9f28a1d1d376f09a9920b0e66362503b5fba39003215f68a911bb33d160745f9f452bfa775c73ca9a3741509b1e5f0e74f35fe2f7e09e4da3bd0eefdea5765322b62a90c080e0ab500853ce8147d7e837dd3cda9c089fe47934065a0da0f3e00cb9de406bd254e0e585d5c67f7af3e0d0729847ca04e69b9ce81e598cdde04e50305e7ecdd0fbeba18a30f307ac795f8145bb149e8a855eaff687077f95305b6419fbf3878dca91edef4666f51fdcdd1c70495fa94f74bdd2733261e04cffaa24a8b040d46897e940ad25756093538d85b321b115cd29970cd51fba8b18c48b2b6e406a71d72a9b58b402d0025854b"
)
RSA_D_HEX = (
    "50a13bf9be2542eb05b2853f1dc3b1a3fc15bd906bf516c4ea3702cf131ef64b06f6c8443614d213bc92740ffe7e4acd9148f013ab33e9d2ecf175e53e2b6dc0bd63ae7bf780b1b27cb4979fa7e83b4f2b4b8992fe1fcf78589052d322e1f1f7362219a21320f53c9a09eb5d9036aed12328d2fba0b499c2301634be3968e3d54067e300fe5649a64b5fbe49cdc20e944c8265c5523628777c0802fd86784a6ce007ddb48cb9e3db14061ccd3e28331ac04fed8289395d553308ee90bbbf17d5cd84889caeb11520a3e238783133aa84c9c0da9bf5cef9982325c2ae13be182cb4d58e2d59ea7def87f93874759e7360218f0df56d7556547f0163fba1e93a1"
)
SHARED_KEY_B64 = "W4YVL+KUu7gcLlAuMGf/oD5T4y0RXjNvxVgAWrHVNe4="

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
log = logging.getLogger("qmt_client")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s")
_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
log.addHandler(_sh)
try:
    _fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    _fh.setFormatter(_fmt)
    log.addHandler(_fh)
except Exception as e:
    log.warning("无法创建日志文件 %s: %s", LOG_FILE, e)


def log_warn(message):
    log.warning(message)


# ---------------------------------------------------------------------------
# 加密层（与 ptrade_client.py 一致的 RSA-SHA256 握手 + ChaCha20 信封）
# ---------------------------------------------------------------------------
_SHA256_DIGESTINFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")
_SHARED_KEY = base64.b64decode(SHARED_KEY_B64)
_ENC_KEY = hashlib.sha256(b"external-trading-enc:" + _SHARED_KEY).digest()
_MAC_KEY = hashlib.sha256(b"external-trading-mac:" + _SHARED_KEY).digest()
_NONCE_COUNTER = 0


def b64url_encode(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(data):
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + padding).encode("ascii"))


def pseudo_random_bytes(length):
    global _NONCE_COUNTER
    chunks = []
    total = 0
    while total < length:
        _NONCE_COUNTER += 1
        seed = "%s|%s|%s|%s" % (
            time.time(), datetime.now().isoformat(), _NONCE_COUNTER, id(chunks),
        )
        chunk = hashlib.sha256(_SHARED_KEY + seed.encode("utf-8")).digest()
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)[:length]


def canonical_handshake_payload(account_id, identifier, ts, nonce):
    payload = {
        "account_id": account_id,
        "identifier": identifier,
        "nonce": nonce,
        "ts": str(ts),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def rsa_sha256_sign(message):
    n = int(RSA_N_HEX, 16)
    d = int(RSA_D_HEX, 16)
    key_size = (n.bit_length() + 7) // 8
    digest_info = _SHA256_DIGESTINFO_PREFIX + hashlib.sha256(message).digest()
    padding_len = key_size - len(digest_info) - 3
    if padding_len < 8:
        raise Exception("invalid rsa key size")
    encoded = b"\x00\x01" + (b"\xff" * padding_len) + b"\x00" + digest_info
    signature_int = pow(int.from_bytes(encoded, "big"), d, n)
    return b64url_encode(signature_int.to_bytes(key_size, "big"))


def sign_handshake(account_id, identifier, ts, nonce):
    return rsa_sha256_sign(canonical_handshake_payload(account_id, identifier, ts, nonce))


def rotl32(value, shift):
    return ((value << shift) & 0xffffffff) | (value >> (32 - shift))


def quarter_round(state, a, b, c, d):
    state[a] = (state[a] + state[b]) & 0xffffffff
    state[d] ^= state[a]
    state[d] = rotl32(state[d], 16)
    state[c] = (state[c] + state[d]) & 0xffffffff
    state[b] ^= state[c]
    state[b] = rotl32(state[b], 12)
    state[a] = (state[a] + state[b]) & 0xffffffff
    state[d] ^= state[a]
    state[d] = rotl32(state[d], 8)
    state[c] = (state[c] + state[d]) & 0xffffffff
    state[b] ^= state[c]
    state[b] = rotl32(state[b], 7)


def chacha20_block(key, counter, nonce):
    constants = b"expand 32-byte k"
    state = list(struct.unpack("<4I", constants))
    state.extend(struct.unpack("<8I", key))
    state.append(counter & 0xffffffff)
    state.extend(struct.unpack("<3I", nonce))
    working = state[:]
    for _ in range(10):
        quarter_round(working, 0, 4, 8, 12)
        quarter_round(working, 1, 5, 9, 13)
        quarter_round(working, 2, 6, 10, 14)
        quarter_round(working, 3, 7, 11, 15)
        quarter_round(working, 0, 5, 10, 15)
        quarter_round(working, 1, 6, 11, 12)
        quarter_round(working, 2, 7, 8, 13)
        quarter_round(working, 3, 4, 9, 14)
    output = [(working[i] + state[i]) & 0xffffffff for i in range(16)]
    return struct.pack("<16I", *output)


def chacha20_xor(data, nonce):
    result = bytearray()
    counter = 1
    for offset in range(0, len(data), 64):
        block = chacha20_block(_ENC_KEY, counter, nonce)
        chunk = data[offset:offset + 64]
        result.extend(bytes([chunk[i] ^ block[i] for i in range(len(chunk))]))
        counter += 1
    return bytes(result)


def encrypt_message(message):
    nonce = pseudo_random_bytes(12)
    plaintext = json.dumps(message, ensure_ascii=False, separators=(",", ":"),
                           default=str).encode("utf-8")
    ciphertext = chacha20_xor(plaintext, nonce)
    mac = hmac.new(_MAC_KEY, nonce + ciphertext, hashlib.sha256).digest()
    envelope = {
        "type": "secure",
        "alg": "CHACHA20-HMAC-SHA256",
        "nonce": b64url_encode(nonce),
        "ciphertext": b64url_encode(ciphertext),
        "mac": b64url_encode(mac),
    }
    return json.dumps(envelope, separators=(",", ":"))


def decrypt_message(raw_message):
    envelope = json.loads(raw_message)
    if envelope.get("type") != "secure":
        raise Exception("message is not encrypted")
    nonce = b64url_decode(envelope.get("nonce", ""))
    ciphertext = b64url_decode(envelope.get("ciphertext", ""))
    mac = b64url_decode(envelope.get("mac", ""))
    expected_mac = hmac.new(_MAC_KEY, nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected_mac):
        raise Exception("message authentication failed")
    plaintext = chacha20_xor(ciphertext, nonce)
    return json.loads(plaintext.decode("utf-8"))


# ---------------------------------------------------------------------------
# 状态码与常量
# ---------------------------------------------------------------------------
# QMT 委托状态 -> PTrade 兼容状态码 (52ETF 后端按 PTrade 表解释)
QMT_STATUS_TO_PTRADE = {
    48: "0",   # 未报
    49: "1",   # 待报
    50: "2",   # 已报
    51: "3",   # 已报待撤
    52: "4",   # 部成待撤
    53: "5",   # 部撤
    54: "6",   # 已撤
    55: "7",   # 部成
    56: "8",   # 已成
    57: "9",   # 废单
}
FILLED_STATUSES = {56}
PARTIAL_FILL_STATUSES = {55}


def ptr_status(qmt_status):
    try:
        return QMT_STATUS_TO_PTRADE.get(int(qmt_status), str(qmt_status))
    except (TypeError, ValueError):
        return str(qmt_status)


def price_type_fixed():
    from xtquant.xtconstant import FIX_PRICE
    return FIX_PRICE  # 11 指定价(限价)


def price_type_market(client_symbol):
    from xtquant.xtconstant import MARKET_SZ_INSTBUSI_RESTCANCEL, MARKET_SH_CONVERT_5_CANCEL
    if str(client_symbol).endswith(".SH"):
        return MARKET_SH_CONVERT_5_CANCEL      # 沪市 最优五档即成剩撤
    return MARKET_SZ_INSTBUSI_RESTCANCEL        # 深市 即时成交剩余撤销


def order_type_buy_sell(side):
    from xtquant.xtconstant import STOCK_BUY, STOCK_SELL
    return STOCK_BUY if str(side).upper() == "BUY" else STOCK_SELL


def symbol_is_sh(symbol):
    return str(symbol).upper().endswith((".SH", ".XSHG"))


# ---------------------------------------------------------------------------
# xtquant 网关
# ---------------------------------------------------------------------------
class XtGateway(object):
    """封装 XtQuantTrader 连接、下单、查询、回调事件。"""

    def __init__(self, data_path, account_id, session_id):
        self.data_path = data_path
        self.account_id = account_id
        self.session_id = session_id
        self.trader = None
        self.account = None
        self.connected = False
        self.account_ok = False
        self._seq_orders = {}       # seq -> order ctx
        self._order_ctx = {}        # order_id -> order ctx
        self._query_waits = {}      # seq -> (data_slot, event)
        self._cancel_waits = {}     # seq -> event
        self.lock = threading.Lock()
        self.event_queue = queue.Queue()
        self.known_order_status = {}  # order_id -> (status, filled) 用于轮询同步

    # -- 连接 ------------------------------------------------------------
    def connect(self):
        from xtquant.xttrader import XtQuantTrader
        from xtquant.xttype import StockAccount
        trader = XtQuantTrader(self.data_path, self.session_id, self)
        trader.start()
        result = trader.connect()
        log.info("xtquant connect() = %s", result)
        if result != 0:
            try:
                trader.stop()
            except Exception:
                pass
            return False
        self.trader = trader
        self.account = StockAccount(self.account_id)
        trader.subscribe(self.account)
        self.connected = True
        log.info("xtquant connected, account=%s", self.account_id)
        return True

    def disconnect(self):
        try:
            if self.trader:
                self.trader.stop()
        except Exception as e:
            log_warn("xtquant stop error: %s" % e)
        self.trader = None
        self.connected = False

    # -- 回调: 连接状态 ------------------------------------------------
    def on_connected(self):
        log.info("xt callback on_connected")

    def on_disconnected(self):
        log.info("xt callback on_disconnected")
        self.connected = False
        self.account_ok = False

    def on_account_status(self, account_status):
        log.info("xt callback account_status account=%s status=%s",
                 getattr(account_status, "account_id", "?"), getattr(account_status, "status", "?"))
        if getattr(account_status, "account_id", None) == self.account_id:
            self.account_ok = (getattr(account_status, "status", None) == 0)

    # -- 回调: 委托/成交事件 -> 业务事件队列 ---------------------------
    def on_stock_order(self, order):
        ctx = self._find_ctx(order.order_id)
        payload = {
            "type": "order_event",
            "source": "on_stock_order",
            "orders": [self._normalize_order(order, ctx)],
            "ts": datetime.now().isoformat(),
        }
        self.event_queue.put(payload)
        if ctx:
            with self.lock:
                self.known_order_status[order.order_id] = (
                    order.order_status, order.traded_volume)

    def on_stock_trade(self, trade):
        ctx = self._find_ctx(trade.order_id)
        payload = {
            "type": "trade_event",
            "source": "on_stock_trade",
            "trades": [self._normalize_trade(trade, ctx)],
            "ts": datetime.now().isoformat(),
        }
        self.event_queue.put(payload)

    def on_order_stock_async_response(self, resp):
        """下单异步回报: seq -> order_id 关联"""
        seq = getattr(resp, "seq", None)
        order_id = getattr(resp, "order_id", None)
        error_msg = getattr(resp, "error_msg", None) or ""
        ctx = self._seq_orders.pop(seq, None)
        if ctx is None:
            return
        with self.lock:
            ctx["order_id"] = order_id
            if order_id and order_id not in self._order_ctx:
                self._order_ctx[order_id] = ctx
            ack = ctx.get("ack_event")
        if error_msg:
            ctx["error_msg"] = error_msg
            log.warning("下单回报异常 seq=%s order_id=%s: %s", seq, order_id, error_msg)
        if ack:
            ack.set()
        # 上报确认事件
        event = {
            "type": "order_event",
            "source": "on_order_response",
            "orders": [{
                "client_order_id": ctx.get("client_order_id"),
                "order_id": order_id,
                "entrust_no": order_id,
                "symbol": ctx.get("symbol"),
                "side": ctx.get("side"),
                "quantity": ctx.get("quantity"),
                "price": ctx.get("submitted_price"),
                "status": "2",
                "raw_status": "50",
                "filled_quantity": 0,
                "avg_fill_price": None,
                "raw": {"seq": seq, "error_msg": error_msg},
            }],
            "ts": datetime.now().isoformat(),
        }
        self.event_queue.put(event)

    def on_order_error(self, error):
        ctx = self._find_ctx(error.order_id) or self._seq_orders.pop(getattr(error, "seq", None), None)
        event = {
            "type": "order_event",
            "source": "on_order_error",
            "orders": [{
                "client_order_id": (ctx or {}).get("client_order_id"),
                "order_id": getattr(error, "order_id", None),
                "symbol": (ctx or {}).get("symbol"),
                "side": (ctx or {}).get("side"),
                "quantity": (ctx or {}).get("quantity"),
                "status": "9",
                "raw_status": "57",
                "raw": {
                    "error_id": getattr(error, "error_id", None),
                    "error_msg": getattr(error, "error_msg", None),
                },
            }],
            "ts": datetime.now().isoformat(),
        }
        self.event_queue.put(event)

    def on_cancel_order_stock_async_response(self, resp):
        seq = getattr(resp, "seq", None)
        ev = self._cancel_waits.pop(seq, None)
        if ev:
            ev["data"] = resp
            ev["event"].set()
        event = {
            "type": "order_event",
            "source": "on_cancel_response",
            "orders": [{
                "client_order_id": None,
                "order_id": getattr(resp, "order_id", None),
                "status": "3",
                "raw_status": "51",
                "raw": {"cancel_result": getattr(resp, "cancel_result", None),
                        "error_msg": getattr(resp, "error_msg", None)},
            }],
            "ts": datetime.now().isoformat(),
        }
        self.event_queue.put(event)

    def on_cancel_error(self, error):
        event = {
            "type": "order_event",
            "source": "on_cancel_error",
            "orders": [{
                "client_order_id": None,
                "order_id": getattr(error, "order_id", None),
                "status": "9",
                "raw": {"error_id": getattr(error, "error_id", None),
                        "error_msg": getattr(error, "error_msg", None)},
            }],
            "ts": datetime.now().isoformat(),
        }
        self.event_queue.put(event)

    # -- 关联查询 ------------------------------------------------------
    def _find_ctx(self, order_id):
        if not order_id:
            return None
        return self._order_ctx.get(order_id)

    def _normalize_order(self, order, ctx):
        qmt_status = getattr(order, "order_status", None)
        return {
            "client_order_id": (ctx or {}).get("client_order_id"),
            "order_id": getattr(order, "order_id", None),
            "entrust_no": getattr(order, "order_id", None),
            "symbol": ctx.get("symbol") if ctx else None,
            "client_symbol": getattr(order, "stock_code", None),
            "side": ctx.get("side") if ctx else None,
            "quantity": getattr(order, "order_volume", None),
            "price": getattr(order, "price", None),
            "status": ptr_status(qmt_status),
            "raw_status": str(qmt_status) if qmt_status is not None else None,
            "filled_quantity": getattr(order, "traded_volume", 0),
            "avg_fill_price": getattr(order, "traded_price", None),
            "order_type": getattr(order, "order_type", None),
            "submitted_at": str(getattr(order, "order_time", "")),
            "event_time": datetime.now().isoformat(),
            "raw": {
                "status_msg": getattr(order, "status_msg", None),
                "order_sysid": getattr(order, "order_sysid", None),
                "order_remark": getattr(order, "order_remark", None),
            },
        }

    def _normalize_trade(self, trade, ctx):
        return {
            "client_order_id": (ctx or {}).get("client_order_id"),
            "order_id": getattr(trade, "order_id", None),
            "entrust_no": getattr(trade, "order_id", None),
            "symbol": ctx.get("symbol") if ctx else None,
            "client_symbol": getattr(trade, "stock_code", None),
            "side": ctx.get("side") if ctx else None,
            "quantity": getattr(trade, "traded_volume", 0),
            "price": getattr(trade, "traded_price", 0),
            "amount": getattr(trade, "traded_amount", 0),
            "status": "8",
            "business_no": getattr(trade, "traded_id", None),
            "business_time": str(getattr(trade, "traded_time", "")),
            "traded_at": str(getattr(trade, "traded_time", "")),
            "commission": getattr(trade, "commission", None),
            "raw": {
                "order_remark": getattr(trade, "order_remark", None),
                "order_sysid": getattr(trade, "order_sysid", None),
            },
        }

    # -- 下单 ----------------------------------------------------------
    def place_order(self, order_request):
        """下单单笔, 返回提交结果 dict。order_request 含 52ETF 字段。"""
        from xtquant.xtconstant import STOCK_BUY, STOCK_SELL
        client_order_id = order_request.get("client_order_id")
        symbol = order_request.get("symbol")
        side = str(order_request.get("side") or "").upper()
        quantity = int(order_request.get("quantity") or 0)
        order_type = (order_request.get("order_type") or "LIMIT").upper()
        if order_type == "MKT":
            order_type = "MARKET"

        base = {
            "client_order_id": client_order_id,
            "symbol": symbol,
            "client_symbol": symbol,
            "side": side,
            "quantity": quantity,
            "requested_quantity": quantity,
            "clip_sell_to_available": True,
            "raw_order_info": {},
        }

        if quantity <= 0 or not symbol or side not in ("BUY", "SELL"):
            base.update({"ok": False, "status": "FAILED", "error_code": "INVALID_ORDER",
                         "message": "无效订单: symbol=%s side=%s quantity=%s" % (symbol, side, quantity),
                         "retryable": False})
            return base

        # 价格计算
        price_info = self._calc_price(order_request, symbol, side, quantity, order_type)

        # 卖出数量裁剪到可卖
        clipped_info = self._clip_sell(order_request, symbol, side, quantity)
        quantity = clipped_info["submitted_quantity"]
        base.update({
            "submitted_quantity": quantity,
            "quantity_clipped": clipped_info["clipped"],
            "sellable_quantity": clipped_info["sellable_quantity"],
            "position_quantity": clipped_info["position_quantity"],
        })

        if not price_info["ok"]:
            base.update({"ok": False, "status": "REJECTED", "retryable": False,
                         "error_code": "PRICE_UNAVAILABLE",
                         "message": price_info["message"]})
            return base

        submitted_price = price_info["price"]
        base.update({
            "protection_limit_price": price_info.get("protection_limit_price"),
            "calculated_price": submitted_price,
            "price_source": price_info.get("price_source"),
            "price_level": price_info.get("price_level"),
            "snapshot_time": price_info.get("snapshot_time"),
            "order_type": order_type,
        })

        # 组装并下单
        qmt_order_type = STOCK_BUY if side == "BUY" else STOCK_SELL
        pt = price_type_fixed() if order_type == "LIMIT" else price_type_market(symbol)
        strategy_name = "52etf"
        remark = str(client_order_id or "")[:32]

        ctx = {
            "client_order_id": client_order_id,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "submitted_price": submitted_price,
            "ack_event": threading.Event(),
            "order_id": None,
        }
        seq = self.trader.order_stock_async(
            self.account, symbol, qmt_order_type, quantity, pt, submitted_price,
            strategy_name=strategy_name, order_remark=remark)
        if seq <= 0:
            base.update({"ok": False, "status": "FAILED", "error_code": "BROKER_REJECTED",
                         "message": "下单请求失败 seq=%s" % seq, "retryable": True})
            return base

        with self.lock:
            self._seq_orders[seq] = ctx

        # 等待异步回报拿 order_id
        ack_wait = ctx.get("ack_event")
        if ack_wait.wait(ORDER_ACK_WAIT_SECONDS):
            order_id = ctx.get("order_id")
            base.update({
                "ok": True,
                "status": "SUCCESS",
                "order_id": order_id,
                "entrust_no": order_id,
                "raw_status": "50",
                "filled_quantity": 0,
                "avg_fill_price": None,
                "submitted_price": submitted_price,
                "message": "%s %s, 数量: %s, 价格: %s" % (side, symbol, quantity, submitted_price),
            })
        else:
            base.update({
                "ok": True,
                "status": "SUCCESS",
                "order_id": None,
                "raw_status": "0",
                "message": "%s %s, 数量: %s, 价格: %s (等待回报中)" % (side, symbol, quantity, submitted_price),
            })
        return base

    # -- 价格计算 ------------------------------------------------------
    def _calc_price(self, order_request, symbol, side, quantity, order_type):
        explicit = order_request.get("price")
        limit_price = order_request.get("limit_price")
        price_level = order_request.get("price_level")
        if price_level is None:
            price_level = 0
        protection = (order_request.get("protection_limit_price")
                      or order_request.get("max_buy_price")
                      or order_request.get("min_sell_price")
                      or order_request.get("market_limit_price"))
        tick = self._get_tick(symbol)
        snapshot_time = None
        if tick:
            t = tick.get("time") or tick.get("timetag") or ""
            snapshot_time = str(t)
        limit_up = None
        limit_down = None
        if tick:
            limit_up = tick.get("limitUp")
            limit_down = tick.get("limitDown")
            if limit_up is None:
                pre = tick.get("preClose") or tick.get("lastClose")
                if pre:
                    limit_up = round(pre * 1.1, 3)
            if limit_down is None:
                pre = tick.get("preClose") or tick.get("lastClose")
                if pre:
                    limit_down = round(pre * 0.9, 3)

        bid1 = ask1 = None
        bid_levels = []
        ask_levels = []
        if tick:
            bp = tick.get("bidPrice") or []
            bv = tick.get("bidVol") or []
            ap = tick.get("askPrice") or []
            av = tick.get("askVol") or []
            for i in range(min(5, len(bp))):
                if bp[i] > 0:
                    bid_levels.append({"level": i + 1, "price": bp[i], "volume": bv[i] if i < len(bv) else 0})
            for i in range(min(5, len(ap))):
                if ap[i] > 0:
                    ask_levels.append({"level": i + 1, "price": ap[i], "volume": av[i] if i < len(av) else 0})
            if bid_levels:
                bid1 = bid_levels[0]["price"]
            if ask_levels:
                ask1 = ask_levels[0]["price"]

        # 显式价格优先
        if explicit:
            return {"ok": True, "price": float(explicit), "price_source": "explicit_price",
                    "price_level": price_level, "protection_limit_price": protection,
                    "snapshot_time": snapshot_time}
        if limit_price:
            return {"ok": True, "price": float(limit_price), "price_source": "explicit_limit_price",
                    "price_level": price_level, "protection_limit_price": protection,
                    "snapshot_time": snapshot_time}

        if order_type == "MARKET":
            p = None
            src = "market"
            if side == "BUY":
                p = ask1
            else:
                p = bid1
            if p is None:
                p = limit_up if side == "BUY" else limit_down
                src = "limit_fallback"
            return {"ok": True, "price": float(p), "price_source": src,
                    "price_level": price_level, "protection_limit_price": protection,
                    "snapshot_time": snapshot_time}

        # 档位定价
        if side == "BUY":
            target = ask_levels
            fallback = limit_down
        else:
            target = bid_levels
            fallback = limit_up

        if price_level == -1:
            # 被动价
            p = bid1 if side == "BUY" else ask1
            src = "bid_level_1" if side == "BUY" else "ask_level_1"
            if p is None:
                p = fallback
                src = "limit_fallback"
            return {"ok": True, "price": float(p), "price_source": src,
                    "price_level": -1, "protection_limit_price": protection,
                    "snapshot_time": snapshot_time}

        p = None
        src = "best_price"
        if price_level == 0:
            p = ask1 if side == "BUY" else bid1
            src = "ask_level_1" if side == "BUY" else "bid_level_1"
        else:
            # 1..N 档中能覆盖数量的最后一档
            depth = int(price_level) if int(price_level) >= 1 else 1
            if int(price_level) > 5:
                depth = 5
            if not target:
                return {"ok": False, "message": "无盘口档位可定价 symbol=%s" % symbol}
            cum = 0
            for lv in target:
                cum += lv["volume"]
                if cum >= quantity:
                    p = lv["price"]
                    src = "depth_level_%s" % lv["level"]
                    break
            if p is None:
                p = target[-1]["price"]
                src = "depth_level_last"

        if p is None:
            return {"ok": False, "message": "盘口不可用 symbol=%s" % symbol}

        # 保护限价风控边界
        if protection is not None:
            if side == "BUY":
                p = min(p, float(protection))
            else:
                p = max(p, float(protection))
        if limit_up is not None:
            p = min(p, limit_up)
        if limit_down is not None:
            p = max(p, limit_down)

        return {"ok": True, "price": float(p), "price_source": src,
                "price_level": price_level, "protection_limit_price": protection,
                "snapshot_time": snapshot_time}

    def _get_tick(self, symbol):
        try:
            from xtquant import xtdata
            ticks = xtdata.get_full_tick([symbol])
            t = ticks.get(symbol)
            if not t and ticks:
                # 兼容某些版本嵌套 data 字段
                t = list(ticks.values())[0]
            return t or {}
        except Exception as e:
            log_warn("get_full_tick 失败 %s: %s" % (symbol, e))
            return {}

    def _clip_sell(self, order_request, symbol, side, quantity):
        """卖出裁剪到可卖数量"""
        result = {"submitted_quantity": quantity, "clipped": False,
                  "sellable_quantity": None, "position_quantity": None}
        if side != "SELL" or not order_request.get("clip_sell_to_available", True):
            return result
        positions = self.query_positions()
        for pos in positions:
            if pos.get("client_symbol") == symbol:
                result["position_quantity"] = pos.get("quantity")
                result["sellable_quantity"] = pos.get("available_quantity")
                avail = int(pos.get("available_quantity") or 0)
                if quantity > avail:
                    result["submitted_quantity"] = avail
                    result["clipped"] = True
                break
        return result

    # -- 撤单 ----------------------------------------------------------
    def cancel_order(self, order_id, client_order_id=None):
        result = {"client_order_id": client_order_id, "order_id": order_id}
        if not order_id:
            result.update({"ok": False, "status": "FAILED", "message": "缺少 order_id"})
            return result
        ev = {"data": None, "event": threading.Event()}
        seq = self.trader.cancel_order_stock_async(self.account, order_id, None)
        if seq <= 0:
            result.update({"ok": False, "status": "FAILED", "message": "撤单请求失败 seq=%s" % seq})
            return result
        with self.lock:
            self._cancel_waits[seq] = ev
        ev["event"].wait(ORDER_ACK_WAIT_SECONDS)
        resp = ev["data"]
        if resp is not None and getattr(resp, "cancel_result", None) in (0, 1, True):
            result.update({"ok": True, "status": "CANCEL_REQUESTED", "message": "撤单指令已提交"})
        else:
            msg = getattr(resp, "error_msg", None) if resp else None
            result.update({"ok": True, "status": "CANCEL_REQUESTED",
                           "message": "撤单指令已提交(%s)" % (msg or "等待回报")})
        return result

    # -- 查询 ----------------------------------------------------------
    def _query(self, fn, account, timeout=None):
        """通用异步查询等待"""
        timeout = timeout or QUERY_WAIT_SECONDS
        slot = {"data": None, "event": threading.Event()}
        result = {}
        def _cb(data):
            slot["data"] = data
            slot["event"].set()
        try:
            seq = fn(account, _cb)
            slot["seq"] = seq
        except Exception as e:
            log_warn("查询失败: %s" % e)
            return None
        slot["event"].wait(timeout)
        return slot["data"]

    def query_asset(self):
        if not self.trader:
            return None
        return self._query(lambda a, cb: self.trader.query_stock_asset_async(a, cb), self.account)

    def query_positions(self):
        if not self.trader:
            return []
        data = self._query(lambda a, cb: self.trader.query_stock_positions_async(a, cb), self.account)
        return data or []

    def query_orders(self, cancelable_only=False):
        if not self.trader:
            return []
        data = self._query(lambda a, cb: self.trader.query_stock_orders_async(a, cb, cancelable_only), self.account)
        return data or []

    def query_trades(self):
        if not self.trader:
            return []
        data = self._query(lambda a, cb: self.trader.query_stock_trades_async(a, cb), self.account)
        return data or []

    # -- 盘中状态轮询(补漏) -------------------------------------------
    def sync_order_statuses(self):
        """周期查询当日委托, 对比已知状态, 上报变化。"""
        try:
            orders = self.query_orders()
        except Exception as e:
            log_warn("状态同步查询失败: %s" % e)
            return
        changed = []
        for o in orders:
            oid = getattr(o, "order_id", None)
            st = getattr(o, "order_status", None)
            filled = getattr(o, "traded_volume", 0)
            if oid is None:
                continue
            key = (st, filled)
            if oid not in self.known_order_status:
                self.known_order_status[oid] = key
                ctx = self._find_ctx(oid)
                if ctx is None and getattr(o, "order_remark", None):
                    # 尝试用 remark 关联 (备用)
                    ctx = {"client_order_id": getattr(o, "order_remark", None) or None,
                           "symbol": getattr(o, "stock_code", None),
                           "side": "BUY" if getattr(o, "order_type", 0) == 23 else "SELL",
                           "quantity": getattr(o, "order_volume", None)}
                changed.append(self._normalize_order(o, ctx))
            elif self.known_order_status[oid] != key:
                self.known_order_status[oid] = key
                ctx = self._find_ctx(oid)
                changed.append(self._normalize_order(o, ctx))
        if changed:
            self.event_queue.put({
                "type": "order_event",
                "source": "status_sync",
                "orders": changed,
                "ts": datetime.now().isoformat(),
            })


# ---------------------------------------------------------------------------
# 行情快照 (xtdata)
# ---------------------------------------------------------------------------
def get_snapshot_map(symbols):
    """返回 {symbol: snapshot dict}"""
    try:
        from xtquant import xtdata
        ticks = xtdata.get_full_tick(list(symbols))
    except Exception as e:
        log_warn("xtdata.get_full_tick 失败: %s" % e)
        return {}
    out = {}
    for sym in symbols:
        t = (ticks or {}).get(sym)
        if not t:
            out[sym] = None
            continue
        def v(k, d=None):
            val = t.get(k)
            return d if val is None else val
        bp = v("bidPrice") or []
        bv = v("bidVol") or []
        ap = v("askPrice") or []
        av = v("askVol") or []
        bid_levels = [{"level": i + 1, "price": bp[i], "volume": bv[i] if i < len(bv) else 0}
                      for i in range(min(5, len(bp))) if bp[i] > 0]
        ask_levels = [{"level": i + 1, "price": ap[i], "volume": av[i] if i < len(av) else 0}
                      for i in range(min(5, len(ap))) if ap[i] > 0]
        ts = v("time", "")
        try:
            ts = str(int(ts))
        except (TypeError, ValueError):
            pass
        out[sym] = {
            "symbol": sym,
            "client_symbol": sym,
            "price": v("lastPrice"),
            "bid": bid_levels[0]["price"] if bid_levels else None,
            "bid_size": bid_levels[0]["volume"] if bid_levels else None,
            "ask": ask_levels[0]["price"] if ask_levels else None,
            "ask_size": ask_levels[0]["volume"] if ask_levels else None,
            "bid_levels": bid_levels,
            "ask_levels": ask_levels,
            "open": v("open"),
            "high": v("high"),
            "low": v("low"),
            "pre_close": v("preClose", v("lastClose")),
            "limit_up": v("limitUp"),
            "limit_down": v("limitDown"),
            "trade_status": str(v("status", "")),
            "timestamp": ts,
            "raw": {k: t.get(k) for k in t if not isinstance(t[k], (list, dict))},
        }
    return out


def normalize_snapshot(snap):
    if snap is None:
        return None
    out = {k: snap[k] for k in (
        "symbol", "client_symbol", "price", "bid", "bid_size", "ask", "ask_size",
        "bid_levels", "ask_levels", "trade_status", "timestamp",
    )}
    out["ok"] = True
    return out


# ---------------------------------------------------------------------------
# WebSocket 客户端 (tornado)
# ---------------------------------------------------------------------------
_WS_LOOP = None
_WS_CONN = None
_PENDING_COMMANDS = []
_PENDING_COMMANDS_LOCK = threading.Lock()


def build_ws_url(account_id, identifier):
    ws_scheme = "wss" if USE_HTTPS else "ws"
    ts = str(int(time.time()))
    nonce = b64url_encode(pseudo_random_bytes(16))
    signature = sign_handshake(account_id, identifier, ts, nonce)
    query = "account_id=%s&identifier=%s&ts=%s&nonce=%s&signature=%s" % (
        quote(str(account_id), safe=""),
        quote(str(identifier), safe=""),
        quote(ts, safe=""),
        quote(nonce, safe=""),
        quote(signature, safe=""),
    )
    return "%s://%s/api/external-trading-accounts/ws?%s" % (ws_scheme, API_HOST, query)


def send_ws_json(loop, conn, payload):
    text = encrypt_message(payload)
    loop.add_callback(conn.write_message, text)


def send_ws_event(payload):
    loop = _WS_LOOP
    conn = _WS_CONN
    if not loop or not conn:
        log_warn("WebSocket 不可用, 丢弃事件: %s" % payload.get("type"))
        return
    try:
        loop.add_callback(conn.write_message, encrypt_message(payload))
    except Exception as e:
        log_warn("事件入队失败: %s" % e)


def handle_ws_message(loop, conn, raw_message, gateway):
    try:
        message = decrypt_message(raw_message)
    except Exception:
        log_warn("忽略无效加密消息")
        return
    mtype = message.get("type")
    if mtype == "connected":
        log.info("后端接受连接: %s (%s)", message.get("name"), message.get("identifier"))
        return
    if mtype == "pong":
        return
    if mtype != "command":
        log.info("忽略消息: %s", raw_message)
        return
    command = {
        "loop": loop,
        "conn": conn,
        "id": message.get("id"),
        "action": message.get("action"),
        "payload": message.get("payload") or {},
        "received_at": time.time(),
        "gateway": gateway,
    }
    with _PENDING_COMMANDS_LOCK:
        _PENDING_COMMANDS.append(command)
    log.info("入队指令: %s id=%s", command["action"], command["id"])


def pop_pending_commands():
    with _PENDING_COMMANDS_LOCK:
        items = _PENDING_COMMANDS[:]
        del _PENDING_COMMANDS[:]
    return items


def discard_pending_commands_for_conn(conn):
    with _PENDING_COMMANDS_LOCK:
        keep = []
        for c in _PENDING_COMMANDS:
            if c.get("conn") is conn:
                # 连接失效, 返回失败
                try:
                    loop = c.get("loop")
                    if loop:
                        loop.add_callback(c["conn"].write_message, encrypt_message({
                            "type": "result", "id": c["id"], "ok": False,
                            "error": "connection closed before command processed",
                            "ts": datetime.now().isoformat(),
                        }))
                except Exception:
                    pass
            else:
                keep.append(c)
        _PENDING_COMMANDS[:] = keep


def run_ws_client(account_id, identifier, gateway, stop_event):
    """WebSocket 连接循环 (独立线程)"""
    global _WS_LOOP, _WS_CONN
    from tornado import ioloop, websocket
    while not stop_event.is_set():
        conn = None
        loop = None
        heartbeat = None
        try:
            loop = ioloop.IOLoop()
            loop.make_current()
            ws_url = build_ws_url(account_id, identifier)
            log.info("连接 52ETF WebSocket: %s", ws_url)
            future = websocket.websocket_connect(ws_url, connect_timeout=10)
            conn = loop.run_sync(lambda: future)
            _WS_LOOP = loop
            _WS_CONN = conn
            log.info("52ETF WebSocket 已连接")

            def send_heartbeat():
                try:
                    conn.write_message(encrypt_message({
                        "type": "heartbeat",
                        "ts": datetime.now().isoformat(),
                    }))
                except Exception as e:
                    log_warn("心跳失败: %s: %s" % (e.__class__.__name__, e))

            heartbeat = ioloop.PeriodicCallback(
                send_heartbeat, HEARTBEAT_INTERVAL_SECONDS * 1000)
            heartbeat.start()

            while True:
                msg = loop.run_sync(lambda: conn.read_message())
                if msg is None:
                    log_warn("服务端关闭连接")
                    break
                handle_ws_message(loop, conn, msg, gateway)
        except Exception as e:
            log.error("WebSocket 错误: %s: %s", e.__class__.__name__, e)
        finally:
            if heartbeat:
                try:
                    heartbeat.stop()
                except Exception:
                    pass
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            if _WS_CONN is conn:
                _WS_CONN = None
                _WS_LOOP = None
            discard_pending_commands_for_conn(conn)
            if loop:
                try:
                    loop.close()
                except Exception:
                    pass
        if not stop_event.is_set():
            time.sleep(RECONNECT_DELAY_SECONDS)


# ---------------------------------------------------------------------------
# 指令执行
# ---------------------------------------------------------------------------
def _send_result(loop, conn, request_id, ok, data=None, error=None, message=None):
    payload = {
        "type": "result",
        "id": request_id,
        "ok": ok,
        "data": data if data is not None else {},
        "ts": datetime.now().isoformat(),
    }
    if error:
        payload["error"] = error
    if message and not error:
        payload["message"] = message
    try:
        loop.add_callback(conn.write_message, encrypt_message(payload))
    except Exception as e:
        log_warn("发送 result 失败: %s" % e)


def execute_command(gateway, action, payload):
    """返回 (ok, data, error)"""
    if action in ("get_quotes", "get_bid_ask", "quote.batch"):
        symbols = payload.get("symbols") or []
        snaps = get_snapshot_map(symbols)
        quotes = []
        for s in symbols:
            snap = snaps.get(s)
            if snap is None:
                quotes.append({"symbol": s, "client_symbol": s, "ok": False,
                               "error": "行情不可用"})
            else:
                q = normalize_snapshot(snap)
                quotes.append(q)
        return True, {"quotes": quotes}, None

    if action in ("get_snapshots", "snapshot.batch", "quote.snapshot"):
        symbols = payload.get("symbols") or []
        snaps = get_snapshot_map(symbols)
        out = []
        for s in symbols:
            snap = snaps.get(s)
            if snap is None:
                out.append({"symbol": s, "client_symbol": s, "ok": False,
                            "error": "行情不可用"})
            else:
                item = normalize_snapshot(snap)
                item["raw"] = snap["raw"]
                out.append(item)
        return True, {"snapshots": out}, None

    if action in ("place_orders", "order.batch"):
        orders = payload.get("orders") or []
        results = []
        for o in orders:
            try:
                results.append(gateway.place_order(o))
            except Exception as e:
                results.append({"ok": False, "status": "FAILED",
                                "client_order_id": o.get("client_order_id"),
                                "symbol": o.get("symbol"),
                                "message": "下单异常: %s" % e, "retryable": True})
        return True, {"orders": results}, None

    if action in ("cancel_orders", "order.cancel"):
        orders = payload.get("orders") or payload.get("order_ids") or []
        results = []
        for o in orders:
            if isinstance(o, dict):
                results.append(gateway.cancel_order(o.get("order_id"), o.get("client_order_id")))
            else:
                results.append(gateway.cancel_order(o, None))
        return True, {"orders": results}, None

    if action in ("get_account_snapshot", "account.snapshot"):
        return True, _account_snapshot(gateway), None

    if action in ("get_positions", "positions"):
        return True, _positions_payload(gateway), None

    if action in ("get_assets", "assets"):
        return True, _assets_payload(gateway), None

    if action in ("get_today_orders", "today_orders", "orders.today"):
        return True, _today_orders_payload(gateway), None

    if action in ("get_deliver", "deliver"):
        # xtquant 当前无交割单接口, 返回空记录 (费用对账暂缓)
        return True, {
            "start_date": payload.get("start_date"),
            "end_date": payload.get("end_date"),
            "records": [],
            "message": "QMT xtquant 暂不支持交割单查询, 费用对账由人工核对",
        }, None

    raise Exception("Unsupported command action: %s" % action)


def _positions_payload(gateway):
    positions = gateway.query_positions()
    items = []
    for p in positions:
        items.append({
            "symbol": getattr(p, "stock_code", None),
            "client_symbol": getattr(p, "stock_code", None),
            "quantity": getattr(p, "volume", None),
            "available_quantity": getattr(p, "can_use_volume", None),
            "cost_price": getattr(p, "avg_price", None),
            "last_price": getattr(p, "last_price", None),
            "market_value": getattr(p, "market_value", None),
            "profit": None,
            "profit_ratio": getattr(p, "profit_rate", None),
        })
    return {"current_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "positions": items}


def _assets_payload(gateway):
    asset = gateway.query_asset()
    if asset is None:
        return {"current_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "assets": {}}
    cash = getattr(asset, "cash", None)
    frozen = getattr(asset, "frozen_cash", None)
    mv = getattr(asset, "market_value", None)
    total = getattr(asset, "total_asset", None)
    def n(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0
    return {"current_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "assets": {
        "portfolio_value": n(total),
        "available_cash": n(cash),
        "locked_cash": n(frozen),
        "total_cash": n(cash) + n(frozen),
        "total_positions_value": n(mv),
    }}


def _account_snapshot(gateway):
    pos = _positions_payload(gateway)
    assets = _assets_payload(gateway)
    orders = _today_orders_payload(gateway)
    return {
        "account_id": DEFAULT_ACCOUNT_ID,
        "identifier": DEFAULT_IDENTIFIER,
        "backtest": False,
        "current_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "orders": orders.get("orders", []),
        "positions": pos.get("positions", []),
        "portfolio": {
            "portfolio_value": assets["assets"].get("portfolio_value"),
            "available_cash": assets["assets"].get("available_cash"),
            "locked_cash": assets["assets"].get("locked_cash"),
            "total_cash": assets["assets"].get("total_cash"),
            "total_positions_value": assets["assets"].get("total_positions_value"),
            "returns": None,
            "starting_cash": None,
        },
    }


def _today_orders_payload(gateway):
    orders = gateway.query_orders()
    items = []
    for o in orders:
        ctx = gateway._find_ctx(getattr(o, "order_id", None))
        items.append({
            "client_order_id": (ctx or {}).get("client_order_id"),
            "order_id": getattr(o, "order_id", None),
            "entrust_no": getattr(o, "order_id", None),
            "symbol": ctx.get("symbol") if ctx else None,
            "client_symbol": getattr(o, "stock_code", None),
            "side": "BUY" if getattr(o, "order_type", 0) == 23 else "SELL",
            "quantity": getattr(o, "order_volume", None),
            "price": getattr(o, "price", None),
            "status": ptr_status(getattr(o, "order_status", None)),
            "raw_status": str(getattr(o, "order_status", None)),
            "filled_quantity": getattr(o, "traded_volume", None),
            "avg_fill_price": getattr(o, "traded_price", None),
            "submitted_at": str(getattr(o, "order_time", "")),
            "event_time": datetime.now().isoformat(),
            "raw": {"status_msg": getattr(o, "status_msg", None),
                    "order_sysid": getattr(o, "order_sysid", None)},
        })
    return {"current_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "orders": items}


# ---------------------------------------------------------------------------
# 事件上报线程
# ---------------------------------------------------------------------------
def event_reporter(gateway, stop_event):
    while not stop_event.is_set():
        try:
            item = gateway.event_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        try:
            send_ws_event(item)
        except Exception as e:
            log_warn("上报事件失败: %s" % e)


# ---------------------------------------------------------------------------
# 指令处理线程
# ---------------------------------------------------------------------------
def command_worker(gateway, stop_event):
    while not stop_event.is_set():
        commands = pop_pending_commands()
        if not commands:
            time.sleep(0.2)
            continue
        for command in commands:
            loop = command["loop"]
            conn = command["conn"]
            rid = command["id"]
            action = command["action"]
            payload = command["payload"]
            try:
                ok, data, error = execute_command(gateway, action, payload)
                _send_result(loop, conn, rid, ok, data, error)
            except Exception as e:
                log.error("指令执行异常 %s id=%s: %s", action, rid, e)
                _send_result(loop, conn, rid, False, {}, str(e))


# ---------------------------------------------------------------------------
# 收盘持仓快照
# ---------------------------------------------------------------------------
def _is_trading_day():
    return date.today().weekday() < 5


def close_snapshot_scheduler(gateway, stop_event):
    last_sent = None
    while not stop_event.is_set():
        now = datetime.now()
        if (_is_trading_day() and now.hour >= 15 and now.minute >= 5
                and last_sent != now.date()):
            try:
                positions = gateway.query_positions()
                items = []
                for p in positions:
                    items.append({
                        "symbol": getattr(p, "stock_code", None),
                        "client_symbol": getattr(p, "stock_code", None),
                        "quantity": getattr(p, "volume", None),
                        "available_quantity": getattr(p, "can_use_volume", None),
                        "cost_price": getattr(p, "avg_price", None),
                        "last_price": getattr(p, "last_price", None),
                        "market_value": getattr(p, "market_value", None),
                        "profit_ratio": getattr(p, "profit_rate", None),
                    })
                send_ws_event({
                    "type": "broker_positions_event",
                    "data": {
                        "current_time": now.strftime("%Y-%m-%d %H:%M:%S"),
                        "snapshot_kind": "close",
                        "positions": items,
                    },
                    "ts": now.isoformat(),
                })
                log.info("已推送收盘持仓快照, %s 条", len(items))
                last_sent = now.date()
            except Exception as e:
                log_warn("收盘快照失败: %s" % e)
        time.sleep(60)


def order_status_sync_loop(gateway, stop_event):
    while not stop_event.is_set():
        time.sleep(ORDER_STATUS_SYNC_INTERVAL)
        if stop_event.is_set():
            break
        if not gateway.connected:
            continue
        try:
            gateway.sync_order_statuses()
        except Exception as e:
            log_warn("订单状态同步异常: %s" % e)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def load_config(args):
    cfg = {}
    if args.config and os.path.exists(args.config):
        with open(args.config, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    g = {
        "account_id": cfg.get("account_id", DEFAULT_ACCOUNT_ID),
        "identifier": cfg.get("identifier", DEFAULT_IDENTIFIER),
        "api_host": cfg.get("api_host", API_HOST),
        "use_https": bool(cfg.get("use_https", USE_HTTPS)),
        "qmt_data_path": cfg.get("qmt_data_path", QMT_DATA_PATH),
        "qmt_account_id": cfg.get("qmt_account_id", QMT_ACCOUNT_ID),
        "qmt_session_id": int(cfg.get("qmt_session_id", QMT_SESSION_ID)),
    }
    if args.account_id:
        g["account_id"] = args.account_id
    if args.identifier:
        g["identifier"] = args.identifier
    if args.api_host:
        g["api_host"] = args.api_host
    if args.qmt_path:
        g["qmt_data_path"] = args.qmt_path
    if args.qmt_account:
        g["qmt_account_id"] = args.qmt_account
    return g


def main():
    global USE_HTTPS, API_HOST, DEFAULT_ACCOUNT_ID, DEFAULT_IDENTIFIER
    parser = argparse.ArgumentParser(description="52ETF 国金 QMT 外部交易客户端")
    parser.add_argument("--config", help="JSON 配置文件路径")
    parser.add_argument("--account-id", help="52ETF account_id")
    parser.add_argument("--identifier", help="52ETF identifier (券商资金账号)")
    parser.add_argument("--api-host", help="52ETF API host")
    parser.add_argument("--use-https", action="store_true", help="使用 wss")
    parser.add_argument("--qmt-path", help="QMT userdata 目录")
    parser.add_argument("--qmt-account", help="QMT 资金账号")
    parser.add_argument("--once", action="store_true", help="连上后端后跑完即退出(联调用)")
    args = parser.parse_args()

    g = load_config(args)
    USE_HTTPS = g["use_https"]
    API_HOST = g["api_host"]
    DEFAULT_ACCOUNT_ID = g["account_id"]
    DEFAULT_IDENTIFIER = g["identifier"]
    QMT_DATA_PATH = g["qmt_data_path"]
    QMT_ACCOUNT_ID = g["qmt_account_id"]

    log.info("=" * 60)
    log.info("52ETF QMT 客户端启动")
    log.info("  52ETF 账号: %s / identifier=%s", g["account_id"], g["identifier"])
    log.info("  API: %s://%s", "wss" if USE_HTTPS else "ws", API_HOST)
    log.info("  QMT: %s 资金账号=%s", QMT_DATA_PATH, QMT_ACCOUNT_ID)
    log.info("  QMT 客户端版本: 2.1.19.0 (xtquant 250807.1.2)")

    # xtquant 网关 (主线程创建, 保证事件循环归属)
    gateway = XtGateway(QMT_DATA_PATH, QMT_ACCOUNT_ID, g["qmt_session_id"])
    if not gateway.connect():
        log.error("xtquant 连接失败: 请确认 QMT 极简模式客户端已登录并保持运行")
        if args.once:
            return 2
        # 不退出: 等待 MiniQMT 登录后重连
    else:
        log.info("xtquant 连接成功, 订阅账号 %s", QMT_ACCOUNT_ID)

    stop_event = threading.Event()
    threads = []

    ws_thread = threading.Thread(target=run_ws_client,
                                 args=(g["account_id"], g["identifier"], gateway, stop_event),
                                 name="ws-client", daemon=True)
    threads.append(ws_thread)

    cmd_thread = threading.Thread(target=command_worker, args=(gateway, stop_event),
                                  name="cmd-worker", daemon=True)
    threads.append(cmd_thread)

    rep_thread = threading.Thread(target=event_reporter, args=(gateway, stop_event),
                                  name="event-reporter", daemon=True)
    threads.append(rep_thread)

    sync_thread = threading.Thread(target=order_status_sync_loop, args=(gateway, stop_event),
                                   name="status-sync", daemon=True)
    threads.append(sync_thread)

    close_thread = threading.Thread(target=close_snapshot_scheduler, args=(gateway, stop_event),
                                    name="close-snapshot", daemon=True)
    threads.append(close_thread)

    for t in threads:
        t.start()

    log.info("各线程已启动, 进入主循环")
    if args.once:
        time.sleep(30)
        stop_event.set()
        log.info("--once 模式结束")
        return 0

    retry_counter = 0
    try:
        while True:
            time.sleep(5)
            # 保活: xtquant 未连接时周期性重连(账号审批后 miniQMT 登录即可自动连上)
            if not gateway.connected:
                retry_counter += 1
                if retry_counter % 6 != 0:
                    continue
                try:
                    if gateway.connect():
                        retry_counter = 0
                        log.info("xtquant 已重连")
                except Exception as e:
                    log_warn("xtquant 重连失败: %s" % e)
    except KeyboardInterrupt:
        log.info("收到中断, 退出")
    finally:
        stop_event.set()
        gateway.disconnect()


if __name__ == "__main__":
    main()

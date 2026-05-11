from datetime import datetime, timedelta
import base64
import hashlib
import hmac
import json
import os
import struct
import threading
import time

try:
    from urllib.parse import quote
except ImportError:
    from urllib import quote

# HTTP mode: set to False to use ws and avoid TLS certificate validation.
USE_HTTPS = os.getenv("PTRADE_USE_HTTPS", "0").strip().lower() in ("1", "true", "yes", "wss")
API_HOST = os.getenv("PTRADE_API_HOST", "api.52etf.vip").strip() or "api.52etf.vip"

# The backend validates account_id + account name + unique identifier.
# Create the same account in the web "外部交易账号" page before starting this script.
DEFAULT_ACCOUNT_ID = (
    os.getenv("PTRADE_ACCOUNT_ID", "vNKpHJkLMnBQRSTUVWXYZabcdefghijkl").strip()
    or "vNKpHJkLMnBQRSTUVWXYZabcdefghijkl"
)
DEFAULT_IDENTIFIER = os.getenv("PTRADE_IDENTIFIER", "GS66301027527").strip() or "GS66301027527"

HEARTBEAT_INTERVAL_SECONDS = 20
RECONNECT_DELAY_SECONDS = 5
DISABLE_AUTO_WEBSOCKET = os.getenv("PTRADE_DISABLE_AUTO_WS", "0").strip().lower() in ("1", "true", "yes")
RSA_N_HEX = (
    "d10e83e0f75ddef1fa41d524bbf4ff76dc9f28a1d1d376f09a9920b0e66362503b5fba39003215f68a911bb33d160745f9f452bfa775c73ca9a3741509b1e5f0e74f35fe2f7e09e4da3bd0eefdea5765322b62a90c080e0ab500853ce8147d7e837dd3cda9c089fe47934065a0da0f3e00cb9de406bd254e0e585d5c67f7af3e0d0729847ca04e69b9ce81e598cdde04e50305e7ecdd0fbeba18a30f307ac795f8145bb149e8a855eaff687077f95305b6419fbf3878dca91edef4666f51fdcdd1c70495fa94f74bdd2733261e04cffaa24a8b040d46897e940ad25756093538d85b321b115cd29970cd51fba8b18c48b2b6e406a71d72a9b58b402d0025854b"
)
RSA_D_HEX = (
    "50a13bf9be2542eb05b2853f1dc3b1a3fc15bd906bf516c4ea3702cf131ef64b06f6c8443614d213bc92740ffe7e4acd9148f013ab33e9d2ecf175e53e2b6dc0bd63ae7bf780b1b27cb4979fa7e83b4f2b4b8992fe1fcf78589052d322e1f1f7362219a21320f53c9a09eb5d9036aed12328d2fba0b499c2301634be3968e3d54067e300fe5649a64b5fbe49cdc20e944c8265c5523628777c0802fd86784a6ce007ddb48cb9e3db14061ccd3e28331ac04fed8289395d553308ee90bbbf17d5cd84889caeb11520a3e238783133aa84c9c0da9bf5cef9982325c2ae13be182cb4d58e2d59ea7def87f93874759e7360218f0df56d7556547f0163fba1e93a1"
)
SHARED_KEY_B64 = "W4YVL+KUu7gcLlAuMGf/oD5T4y0RXjNvxVgAWrHVNe4="
_SHA256_DIGESTINFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")
_SHARED_KEY = base64.b64decode(SHARED_KEY_B64)
_ENC_KEY = hashlib.sha256(b"external-trading-enc:" + _SHARED_KEY).digest()
_MAC_KEY = hashlib.sha256(b"external-trading-mac:" + _SHARED_KEY).digest()

try:
    from tornado import gen, ioloop, websocket
    HAS_WEBSOCKET = True
except ImportError:
    HAS_WEBSOCKET = False


def log_warn(message):
    if hasattr(log, "warning"):
        log.warning(message)
    else:
        log.warn(message)


def is_backtest_mode():
    try:
        return not is_trade()
    except Exception:
        return False


def initialize(context):
    g.account_id = DEFAULT_ACCOUNT_ID
    backtest = is_backtest_mode()
    g.external_account_identifier = DEFAULT_IDENTIFIER + ("B" if backtest else "")
    g.current_context = context

    if DISABLE_AUTO_WEBSOCKET:
        log.info("External trading WebSocket autostart disabled.")
    elif HAS_WEBSOCKET:
        log.info("Starting external trading WebSocket client...")
        thread = threading.Thread(target=run_ws_client)
        thread.daemon = True
        thread.start()
    else:
        log_warn("Tornado library not found, external trading WebSocket disabled.")


def handle_data(context, data):
    g.current_context = context


def tick_data(context, data):
    g.current_context = context


def b64url_encode(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(data):
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + padding).encode("ascii"))


def canonical_handshake_payload(account_id, identifier, ts, nonce):
    payload = {
        "account_id": account_id,
        "identifier": identifier,
        "nonce": nonce,
        "ts": str(ts),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


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
    message = canonical_handshake_payload(account_id, identifier, ts, nonce)
    return rsa_sha256_sign(message)


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
    nonce = os.urandom(12)
    plaintext = json.dumps(message, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
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


def build_ws_url():
    ws_scheme = "wss" if USE_HTTPS else "ws"
    ts = str(int(time.time()))
    nonce = b64url_encode(os.urandom(16))
    signature = sign_handshake(g.account_id, g.external_account_identifier, ts, nonce)
    query = "account_id=%s&identifier=%s&ts=%s&nonce=%s&signature=%s" % (
        quote(str(g.account_id), safe=""),
        quote(str(g.external_account_identifier), safe=""),
        quote(ts, safe=""),
        quote(nonce, safe=""),
        quote(signature, safe=""),
    )
    return "%s://%s/api/external-trading-accounts/ws?%s" % (ws_scheme, API_HOST, query)


def run_ws_client():
    if not HAS_WEBSOCKET:
        return

    while True:
        conn = None
        loop = None
        try:
            loop = ioloop.IOLoop()
            loop.make_current()
            ws_url = build_ws_url()
            log.info("Connecting external trading WebSocket: %s" % ws_url)
            future = websocket.websocket_connect(ws_url, connect_timeout=10)
            conn = loop.run_sync(lambda: future)
            log.info("External trading WebSocket connected.")

            while True:
                try:
                    msg = loop.run_sync(
                        lambda: gen.with_timeout(
                            timedelta(seconds=HEARTBEAT_INTERVAL_SECONDS),
                            conn.read_message(),
                        )
                    )
                except gen.TimeoutError:
                    send_ws_json(loop, conn, {
                        "type": "heartbeat",
                        "ts": datetime.now().isoformat(),
                    })
                    continue

                if msg is None:
                    log_warn("External trading WebSocket closed by server.")
                    break
                handle_ws_message(loop, conn, msg)
        except Exception as e:
            log.error("External trading WebSocket error: %s" % str(e))
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            if loop:
                try:
                    loop.close()
                except Exception:
                    pass

        time.sleep(RECONNECT_DELAY_SECONDS)


def send_ws_json(loop, conn, payload):
    text = encrypt_message(payload)
    loop.run_sync(lambda: conn.write_message(text))


def handle_ws_message(loop, conn, raw_message):
    try:
        message = decrypt_message(raw_message)
    except Exception:
        log_warn("Ignored invalid secure WebSocket message")
        return

    message_type = message.get("type")
    if message_type == "connected":
        log.info("External trading account accepted by backend: %s" % message.get("name"))
        return
    if message_type == "pong":
        return
    if message_type != "command":
        log.info("Ignored WebSocket message: %s" % raw_message)
        return

    request_id = message.get("id")
    action = message.get("action")
    payload = message.get("payload") or {}
    response = {
        "type": "result",
        "id": request_id,
        "ok": True,
        "data": {},
        "ts": datetime.now().isoformat(),
    }

    try:
        log.info("Executing external command: %s id=%s" % (action, request_id))
        response["data"] = execute_command(action, payload)
    except Exception as e:
        log.error("External command failed: %s id=%s error=%s" % (action, request_id, str(e)))
        response["ok"] = False
        response["error"] = str(e)

    send_ws_json(loop, conn, response)


def execute_command(action, payload):
    if action in ("get_quotes", "get_bid_ask", "quote.batch"):
        return get_quotes(payload.get("symbols") or [])
    if action in ("get_snapshots", "snapshot.batch", "quote.snapshot"):
        return get_snapshots_payload(payload.get("symbols") or [])
    if action in ("place_orders", "order.batch"):
        return place_order_batch(payload.get("orders") or [])
    if action in ("get_account_snapshot", "account.snapshot"):
        return get_account_snapshot()
    if action in ("get_positions", "positions"):
        return get_positions_payload()
    if action in ("get_assets", "assets"):
        return get_assets_payload()
    if action in ("get_today_orders", "today_orders", "orders.today"):
        return get_today_orders_payload()
    raise Exception("Unsupported command action: %s" % action)


def convert_to_api_code(symbol):
    """Convert PTrade client code (600000.SS) to backend code (SH.600000)."""
    if not symbol:
        return None
    parts = str(symbol).split(".")
    if len(parts) != 2:
        return symbol

    first = parts[0].upper()
    second = parts[1].upper()
    if first in ("SH", "SS", "SZ", "BJ"):
        market = "SH" if first == "SS" else first
        return "%s.%s" % (market, parts[1])

    market = "SH" if second == "SS" else second
    return "%s.%s" % (market, parts[0])


def convert_to_client_code(symbol):
    """Convert backend code (SH.600000) to PTrade client code (600000.SS)."""
    if not symbol:
        return None
    parts = str(symbol).split(".")
    if len(parts) != 2:
        return symbol

    first = parts[0].upper()
    second = parts[1].upper()
    if first not in ("SH", "SS", "SZ", "BJ"):
        return "%s.%s" % (parts[0], "SS" if second == "SH" else second)

    market = "SS" if first in ("SH", "SS") else first
    return "%s.%s" % (parts[1], market)


def value_of(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def get_current_dt():
    context = getattr(g, "current_context", None)
    if context is not None and hasattr(context, "current_dt"):
        return context.current_dt
    return datetime.now()


def normalize_depth_group(group):
    levels = []
    if not group:
        return levels

    if isinstance(group, list):
        iterator = enumerate(group, 1)
    else:
        iterator = [(level, group.get(level) or group.get(str(level))) for level in range(1, 6)]

    for level, price_volume in iterator:
        if not price_volume:
            continue
        try:
            price = float(price_volume[0])
            volume = int(price_volume[1])
        except Exception:
            continue
        levels.append({
            "level": int(level),
            "price": price,
            "volume": volume,
        })
    return levels


def first_numeric_value(obj, keys):
    for key in keys:
        value = value_of(obj, key)
        try:
            if value is not None and float(value) > 0:
                return float(value)
        except Exception:
            continue
    return None


def get_snapshot_map(client_symbols):
    if not client_symbols:
        return {}

    query = client_symbols if len(client_symbols) > 1 else client_symbols[0]
    snapshot = get_snapshot(query) or {}
    if not snapshot:
        return {}

    if value_of(snapshot, "bid_grp") is not None or value_of(snapshot, "offer_grp") is not None:
        return {client_symbols[0]: snapshot}

    return snapshot


def get_single_snapshot(client_symbol):
    snapshots = get_snapshot_map([client_symbol])
    return (
        snapshots.get(client_symbol)
        or snapshots.get(convert_to_api_code(client_symbol))
        or snapshots.get(str(client_symbol).upper())
        or {}
    )


def normalize_quote_from_snapshot(client_symbol, snapshot):
    bid_levels = normalize_depth_group(value_of(snapshot, "bid_grp"))
    ask_levels = normalize_depth_group(value_of(snapshot, "offer_grp"))
    best_bid = bid_levels[0] if bid_levels else {}
    best_ask = ask_levels[0] if ask_levels else {}
    return {
        "symbol": convert_to_api_code(client_symbol),
        "client_symbol": client_symbol,
        "ok": True,
        "price": first_numeric_value(snapshot, ("last_px",)),
        "bid": best_bid.get("price"),
        "bid_size": best_bid.get("volume"),
        "ask": best_ask.get("price"),
        "ask_size": best_ask.get("volume"),
        "bid_levels": bid_levels,
        "ask_levels": ask_levels,
        "trade_status": value_of(snapshot, "trade_status"),
        "timestamp": value_of(snapshot, "hsTimeStamp", datetime.now().isoformat()),
    }


def get_snapshots_payload(symbols):
    client_symbols = [convert_to_client_code(symbol) for symbol in symbols if symbol]
    snapshots = get_snapshot_map(client_symbols)
    results = []
    for client_symbol in client_symbols:
        api_symbol = convert_to_api_code(client_symbol)
        snapshot = (
            snapshots.get(client_symbol)
            or snapshots.get(api_symbol)
            or snapshots.get(str(client_symbol).upper())
        )
        if not snapshot:
            results.append({
                "symbol": api_symbol,
                "client_symbol": client_symbol,
                "ok": False,
                "error": "snapshot not found",
            })
            continue
        item = normalize_quote_from_snapshot(client_symbol, snapshot)
        item["raw"] = stringify_unknown_fields(snapshot)
        results.append(item)
    return {"snapshots": results}


def get_quotes(symbols):
    client_symbols = [convert_to_client_code(symbol) for symbol in symbols if symbol]
    snapshots = get_snapshot_map(client_symbols)
    results = []
    for client_symbol in client_symbols:
        api_symbol = convert_to_api_code(client_symbol)
        item = {
            "symbol": api_symbol,
            "client_symbol": client_symbol,
            "ok": True,
        }
        try:
            snapshot = (
                snapshots.get(client_symbol)
                or snapshots.get(api_symbol)
                or snapshots.get(str(client_symbol).upper())
            )
            if not snapshot:
                raise Exception("snapshot not found")
            item.update(normalize_quote_from_snapshot(client_symbol, snapshot))
        except Exception as e:
            item["ok"] = False
            item["error"] = str(e)
        results.append(item)
    return {"quotes": results}


def get_limit_price_from_snapshot(symbol, side, quantity, snapshot):
    """Use the order book to pick a limit price that covers the requested quantity."""
    if not snapshot or not value_of(snapshot, "bid_grp") or not value_of(snapshot, "offer_grp"):
        raise Exception("获取 %s 档位价格失败: 数据为空" % symbol)

    group = value_of(snapshot, "offer_grp") if side == "BUY" else value_of(snapshot, "bid_grp")
    levels = normalize_depth_group(group)
    if not levels:
        raise Exception("获取 %s 档位价格失败: 档位数据为空" % symbol)

    accumulated_volume = 0
    target_price = levels[0]["price"]
    for level in levels:
        accumulated_volume += level["volume"]
        target_price = level["price"]
        if accumulated_volume >= quantity:
            log.info("%s %s 数量%d, 档位%d满足 (累积%d), 价格%s" % (
                symbol,
                side,
                quantity,
                level["level"],
                accumulated_volume,
                target_price,
            ))
            return target_price

    log.info("%s 档位深度不足以覆盖数量%d (累积%d), 使用最终价格 %s" % (
        symbol,
        quantity,
        accumulated_volume,
        target_price,
    ))
    return target_price


def get_limit_price(symbol, side, quantity):
    return get_limit_price_from_snapshot(symbol, side, quantity, get_single_snapshot(symbol))


def get_int_or_none(value):
    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        return None


def calculate_order_price(symbol, side, quantity, price_level):
    snapshot = get_single_snapshot(symbol)
    if not snapshot:
        raise Exception("获取 %s 快照失败: 数据为空" % symbol)

    quote = normalize_quote_from_snapshot(symbol, snapshot)
    level = get_int_or_none(price_level)
    if level is None or level == -1:
        price = get_limit_price_from_snapshot(symbol, side, quantity, snapshot)
        source = "ptrade_depth_fallback"
    elif level == 0:
        price = first_numeric_value(snapshot, ("last_px",))
        source = "last_px"
    elif 1 <= level <= 5:
        levels = quote.get("ask_levels") if side == "BUY" else quote.get("bid_levels")
        price = None
        for item in levels or []:
            if int(item.get("level") or 0) == level:
                price = item.get("price")
                break
        source = "%s_level_%d" % ("ask" if side == "BUY" else "bid", level)
    else:
        raise Exception("不支持的价格档位: %s" % price_level)

    if price is None or float(price) <= 0:
        raise Exception("获取 %s 执行价格失败: %s 无可用价格" % (symbol, source))

    return {
        "price": float(price),
        "price_source": source,
        "price_level": level,
        "snapshot_time": quote.get("timestamp"),
        "last_price": quote.get("price"),
        "bid": quote.get("bid"),
        "ask": quote.get("ask"),
    }


def place_order_batch(orders):
    results = []
    for index, order_request in enumerate(orders):
        try:
            results.append(place_single_order(order_request, index))
        except Exception as e:
            results.append({
                "ok": False,
                "status": "FAILED",
                "symbol": order_request.get("symbol") if isinstance(order_request, dict) else None,
                "message": str(e),
            })
    return {"orders": results}


def normalize_order_type(order_request):
    order_type = str(order_request.get("order_type") or "LIMIT").upper()
    if order_type in ("MARKET", "MKT"):
        return "MARKET"
    return "LIMIT"


def is_sh_market_symbol(client_symbol):
    return str(client_symbol or "").upper().endswith((".SS", ".SH"))


def validate_market_order_type(client_symbol, market_type):
    market_type = int(market_type)
    allowed = (0, 1, 2, 4) if is_sh_market_symbol(client_symbol) else (0, 2, 3, 4, 5)
    if market_type not in allowed:
        raise Exception("%s 不支持市价委托类型 %s" % (client_symbol, market_type))
    return market_type


def get_market_protection_price(order_request, client_symbol, side, quantity):
    price = (
        order_request.get("protection_limit_price")
        or order_request.get("market_limit_price")
        or order_request.get("limit_price")
        or order_request.get("price")
    )
    if price is not None:
        return {
            "price": float(price),
            "price_source": "explicit_protection_limit_price",
            "price_level": None,
            "snapshot_time": None,
        }

    if "price_level" not in order_request and not is_sh_market_symbol(client_symbol):
        return None

    price_level = order_request.get("price_level", -1)
    calculated = calculate_order_price(client_symbol, side, quantity, price_level)
    calculated["price_source"] = "protection_%s" % calculated["price_source"]
    return calculated


def place_market_order(order_request, client_symbol, api_symbol, side, quantity):
    market_type_value = order_request.get("market_type")
    market_type = validate_market_order_type(client_symbol, 0 if market_type_value is None else market_type_value)
    protection = get_market_protection_price(order_request, client_symbol, side, quantity)
    protection_price = protection.get("price") if protection else None
    signed_quantity = quantity if side == "BUY" else -quantity

    if protection_price is None:
        order_sn = order_market(client_symbol, signed_quantity, market_type)
    else:
        order_sn = order_market(client_symbol, signed_quantity, market_type, protection_price)

    message = "%s %s 市价单, 数量: %d, 市价类型: %s" % (side, client_symbol, quantity, market_type)
    if protection_price is not None:
        message += ", 保护限价: %s" % protection_price
    log.info("交易指令已提交: %s" % message)

    raw_status = None
    if order_sn:
        order_info = get_first_order(order_sn)
        raw_status = str(value_of(order_info, "status", ""))
        if raw_status == "9":
            log.error("交易失败: %s失败(被拒绝)" % message)
            return {
                "ok": False,
                "status": "FAILED",
                "symbol": api_symbol,
                "client_symbol": client_symbol,
                "side": side,
                "quantity": quantity,
                "order_type": "MARKET",
                "market_type": market_type,
                "protection_limit_price": protection_price,
                "calculated_price": protection_price,
                "price_source": protection.get("price_source") if protection else None,
                "price_level": protection.get("price_level") if protection else order_request.get("price_level"),
                "snapshot_time": protection.get("snapshot_time") if protection else None,
                "submitted_price": protection_price,
                "order_id": order_sn,
                "raw_status": raw_status,
                "message": "%s失败(被拒绝)" % message,
            }

    return {
        "ok": True,
        "status": "SUCCESS",
        "symbol": api_symbol,
        "client_symbol": client_symbol,
        "side": side,
        "quantity": quantity,
        "order_type": "MARKET",
        "market_type": market_type,
        "protection_limit_price": protection_price,
        "calculated_price": protection_price,
        "price_source": protection.get("price_source") if protection else None,
        "price_level": protection.get("price_level") if protection else order_request.get("price_level"),
        "snapshot_time": protection.get("snapshot_time") if protection else None,
        "submitted_price": protection_price,
        "order_id": order_sn,
        "raw_status": raw_status,
        "message": message,
    }


def place_limit_order(order_request, client_symbol, api_symbol, side, quantity):
    limit_price = order_request.get("limit_price") or order_request.get("price")
    if limit_price is not None:
        calculated = {
            "price": float(limit_price),
            "price_source": "explicit_limit_price",
            "price_level": None,
            "snapshot_time": None,
        }
    else:
        calculated = calculate_order_price(
            client_symbol,
            side,
            quantity,
            order_request.get("price_level", -1),
        )
    limit_price = float(calculated["price"])

    signed_quantity = quantity if side == "BUY" else -quantity
    order_sn = order(client_symbol, signed_quantity, limit_price=limit_price)

    status = "FAILED"
    message = ""
    raw_status = None
    if order_sn:
        order_info = get_first_order(order_sn)
        raw_status = str(value_of(order_info, "status", ""))
        if raw_status == "9":
            message = "%s %s失败(被拒绝)" % (side, client_symbol)
        else:
            status = "SUCCESS"
            message = "%s %s, 数量: %d, 价格: %s" % (side, client_symbol, quantity, limit_price)
    else:
        message = "%s %s失败(无订单号)" % (side, client_symbol)

    if status == "SUCCESS":
        log.info("交易成功: %s" % message)
    else:
        log.error("交易失败: %s" % message)

    return {
        "ok": status == "SUCCESS",
        "status": status,
        "symbol": api_symbol,
        "client_symbol": client_symbol,
        "side": side,
        "quantity": quantity,
        "order_type": "LIMIT",
        "calculated_price": limit_price,
        "price_source": calculated.get("price_source"),
        "price_level": calculated.get("price_level"),
        "snapshot_time": calculated.get("snapshot_time"),
        "submitted_price": limit_price,
        "order_id": order_sn,
        "raw_status": raw_status,
        "message": message,
    }


def place_single_order(order_request, index):
    symbol = order_request.get("symbol")
    client_symbol = convert_to_client_code(symbol)
    api_symbol = convert_to_api_code(client_symbol)
    side = str(order_request.get("side") or "").upper()
    quantity = int(order_request.get("quantity") or 0)

    if side not in ("BUY", "SELL"):
        raise Exception("orders[%d].side must be BUY or SELL" % index)
    if quantity <= 0:
        raise Exception("orders[%d].quantity must be greater than 0" % index)

    if normalize_order_type(order_request) == "MARKET":
        return place_market_order(order_request, client_symbol, api_symbol, side, quantity)
    return place_limit_order(order_request, client_symbol, api_symbol, side, quantity)


def get_first_order(order_sn):
    try:
        orders = get_order(order_sn)
        if isinstance(orders, list):
            return orders[0] if orders else None
        return orders
    except Exception:
        return None


def stringify_unknown_fields(obj):
    if isinstance(obj, dict):
        result = {}
        for key, value in obj.items():
            try:
                json.dumps(value)
                result[key] = value
            except Exception:
                result[key] = str(value)
        return result
    return {}


def get_order_side(order_item):
    entrust_bs = value_of(order_item, "entrust_bs")
    if str(entrust_bs) == "1":
        return "BUY"
    if str(entrust_bs) == "2":
        return "SELL"

    quantity = value_of(order_item, "amount", value_of(order_item, "quantity", 0))
    try:
        return "BUY" if float(quantity) >= 0 else "SELL"
    except Exception:
        return None


def normalize_order(order_item, current_dt):
    quantity = value_of(order_item, "amount", value_of(order_item, "quantity", 0)) or 0
    try:
        quantity = abs(quantity)
    except Exception:
        pass

    return {
        "symbol": convert_to_api_code(value_of(order_item, "symbol")),
        "client_symbol": value_of(order_item, "symbol"),
        "side": get_order_side(order_item),
        "quantity": quantity,
        "price": value_of(order_item, "price", value_of(order_item, "business_price")),
        "status": value_of(order_item, "status"),
        "entrust_no": value_of(order_item, "entrust_no", value_of(order_item, "order_id")),
        "entrust_bs": value_of(order_item, "entrust_bs"),
        "filled_quantity": value_of(order_item, "business_amount", value_of(order_item, "filled_quantity")),
        "submitted_at": value_of(order_item, "entrust_time", current_dt.isoformat()),
        "raw": stringify_unknown_fields(order_item),
    }


def get_today_orders_payload():
    current_dt = get_current_dt()
    today_orders = get_all_orders() or []
    return {
        "current_time": current_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "orders": [normalize_order(item, current_dt) for item in today_orders],
    }


def normalize_position(pos):
    symbol = value_of(pos, "sid", value_of(pos, "symbol"))
    return {
        "symbol": convert_to_api_code(symbol),
        "client_symbol": symbol,
        "quantity": value_of(pos, "amount", value_of(pos, "quantity", 0)),
        "available_quantity": value_of(pos, "enable_amount", value_of(pos, "available_quantity")),
        "cost_price": value_of(pos, "cost_basis", value_of(pos, "cost_price", 0)),
        "last_price": value_of(pos, "last_sale_price", value_of(pos, "price")),
        "market_value": value_of(pos, "market_value"),
        "profit": value_of(pos, "profit"),
        "profit_ratio": value_of(pos, "profit_ratio"),
    }


def get_positions_payload():
    positions = get_positions() or {}
    position_values = positions.values() if isinstance(positions, dict) else positions
    return {
        "current_time": get_current_dt().strftime("%Y-%m-%d %H:%M:%S"),
        "positions": [
            normalize_position(pos)
            for pos in position_values
            if value_of(pos, "amount", value_of(pos, "quantity", 0)) != 0
        ],
    }


def get_assets_payload():
    context = getattr(g, "current_context", None)
    portfolio = {}
    if context is not None and hasattr(context, "portfolio"):
        portfolio_value = value_of(context.portfolio, "portfolio_value", 0)
        positions_value = value_of(context.portfolio, "positions_value", 0)
        cash = value_of(context.portfolio, "cash", 0)
        portfolio = {
            "portfolio_value": portfolio_value,
            "available_cash": cash,
            "locked_cash": portfolio_value - positions_value - cash,
            "total_cash": cash,
            "total_positions_value": positions_value,
            "returns": value_of(context.portfolio, "returns"),
            "starting_cash": value_of(context.portfolio, "starting_cash"),
        }

    return {
        "current_time": get_current_dt().strftime("%Y-%m-%d %H:%M:%S"),
        "assets": portfolio,
    }


def get_account_snapshot():
    current_dt = get_current_dt()
    orders_payload = get_today_orders_payload()
    positions_payload = get_positions_payload()
    assets_payload = get_assets_payload()

    return {
        "account_id": getattr(g, "account_id", None),
        "identifier": getattr(g, "external_account_identifier", None),
        "backtest": is_backtest_mode(),
        "current_time": current_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "orders": orders_payload["orders"],
        "positions": positions_payload["positions"],
        "portfolio": assets_payload["assets"],
    }

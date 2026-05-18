from datetime import datetime, timedelta
import base64
import hashlib
import hmac
import json
import struct
import threading
import time

try:
    from urllib.parse import quote
except ImportError:
    from urllib import quote

# PTrade sandbox forbids environment module access, so settings are constants.
# Edit these values directly before uploading this file to the broker server.
USE_HTTPS = False
API_HOST = "api.52etf.vip"

# The backend validates account_id + account name + unique identifier.
# Create the same account in the web "外部交易账号" page before starting this script.
DEFAULT_ACCOUNT_ID = "vNKpHJkLMnBQRSTUVWXYZabcdefghijkl" #poiuytrewqLKJHGFDSAMNBVCXZasdfgh
DEFAULT_IDENTIFIER = "GS66301027527" #GS66010000018

HEARTBEAT_INTERVAL_SECONDS = 10
RECONNECT_DELAY_SECONDS = 5
DISABLE_AUTO_WEBSOCKET = False
COMMAND_QUEUE_TIMEOUT_SECONDS = 120
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
_NONCE_COUNTER = 0
_CURRENT_CONTEXT = None
_WS_LOOP = None
_WS_CONN = None
_PENDING_COMMANDS = []
_PENDING_COMMANDS_LOCK = threading.Lock()

try:
    from tornado import ioloop, websocket
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


def clear_legacy_runtime_g_attrs():
    for name in ("current_context", "ws_loop", "ws_conn"):
        try:
            if hasattr(g, name):
                try:
                    delattr(g, name)
                except Exception:
                    setattr(g, name, None)
        except Exception:
            pass


def update_current_context(context):
    global _CURRENT_CONTEXT
    _CURRENT_CONTEXT = context
    clear_legacy_runtime_g_attrs()


def initialize(context):
    g.account_id = DEFAULT_ACCOUNT_ID
    backtest = is_backtest_mode()
    g.external_account_identifier = DEFAULT_IDENTIFIER + ("B" if backtest else "")
    update_current_context(context)
    g.order_client_id_by_order_id = {}
    g.order_side_by_order_id = {}
    g.order_last_known_status = {}

    if DISABLE_AUTO_WEBSOCKET:
        log.info("External trading WebSocket autostart disabled.")
    elif HAS_WEBSOCKET:
        log.info("Starting external trading WebSocket client...")
        thread = threading.Thread(target=run_ws_client)
        thread.daemon = True
        thread.start()
    else:
        log_warn("Tornado library not found, external trading WebSocket disabled.")


def before_trading_start(context, data):
    update_current_context(context)
    process_pending_commands()
    push_deliver_records()


def after_trading_end(context, data):
    update_current_context(context)
    process_pending_commands()


def handle_data(context, data):
    update_current_context(context)
    process_pending_commands()
    sync_tracked_order_statuses()


def tick_data(context, data):
    update_current_context(context)
    process_pending_commands()
    sync_tracked_order_statuses()


def b64url_encode(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(data):
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + padding).encode("ascii"))


def pseudo_random_bytes(length):
    """Return nonce bytes without blocked platform entropy APIs."""
    global _NONCE_COUNTER
    chunks = []
    total = 0
    while total < length:
        _NONCE_COUNTER += 1
        seed = "%s|%s|%s|%s" % (
            time.time(),
            datetime.now().isoformat(),
            _NONCE_COUNTER,
            id(chunks),
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
    nonce = pseudo_random_bytes(12)
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
    nonce = b64url_encode(pseudo_random_bytes(16))
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
    global _WS_LOOP, _WS_CONN
    if not HAS_WEBSOCKET:
        return

    while True:
        conn = None
        loop = None
        heartbeat = None
        try:
            loop = ioloop.IOLoop()
            loop.make_current()
            ws_url = build_ws_url()
            log.info("Connecting external trading WebSocket: %s" % ws_url)
            future = websocket.websocket_connect(ws_url, connect_timeout=10)
            conn = loop.run_sync(lambda: future)
            _WS_LOOP = loop
            _WS_CONN = conn
            log.info("External trading WebSocket connected.")

            def send_heartbeat():
                try:
                    conn.write_message(encrypt_message({
                        "type": "heartbeat",
                        "ts": datetime.now().isoformat(),
                    }))
                except Exception as heartbeat_error:
                    log_warn(
                        "Failed to send external trading heartbeat: %s: %s"
                        % (heartbeat_error.__class__.__name__, str(heartbeat_error))
                    )

            heartbeat = ioloop.PeriodicCallback(
                send_heartbeat,
                HEARTBEAT_INTERVAL_SECONDS * 1000,
            )
            heartbeat.start()

            while True:
                msg = loop.run_sync(lambda: conn.read_message())

                if msg is None:
                    log_warn("External trading WebSocket closed by server.")
                    break
                handle_ws_message(loop, conn, msg)
        except Exception as e:
            log.error("External trading WebSocket error: %s: %s" % (e.__class__.__name__, str(e)))
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

        time.sleep(RECONNECT_DELAY_SECONDS)


def send_ws_json(loop, conn, payload):
    text = encrypt_message(payload)
    loop.add_callback(conn.write_message, text)


def send_ws_event(payload):
    loop = _WS_LOOP
    conn = _WS_CONN
    if not loop or not conn:
        log_warn("External trading WebSocket unavailable, dropped event: %s" % payload.get("type"))
        return
    try:
        loop.add_callback(conn.write_message, encrypt_message(payload))
    except Exception as e:
        log_warn("Failed to enqueue external trading event: %s" % str(e))


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
    enqueue_external_command(loop, conn, request_id, action, payload)


def enqueue_external_command(loop, conn, request_id, action, payload):
    command = {
        "loop": loop,
        "conn": conn,
        "id": request_id,
        "action": action,
        "payload": payload or {},
        "received_at": time.time(),
    }
    with _PENDING_COMMANDS_LOCK:
        _PENDING_COMMANDS.append(command)
    log.info("Queued external command for PTrade callback: %s id=%s" % (action, request_id))


def discard_pending_commands_for_conn(conn):
    if conn is None:
        return
    with _PENDING_COMMANDS_LOCK:
        keep = []
        discarded = 0
        for command in _PENDING_COMMANDS:
            if command.get("conn") is conn:
                discarded += 1
            else:
                keep.append(command)
        if discarded:
            _PENDING_COMMANDS[:] = keep
    if discarded:
        log_warn("Discarded %d pending external commands after WebSocket disconnect." % discarded)


def pop_pending_commands():
    with _PENDING_COMMANDS_LOCK:
        commands = _PENDING_COMMANDS[:]
        _PENDING_COMMANDS[:] = []
    return commands


def is_command_connection_active(command):
    return command.get("loop") is _WS_LOOP and command.get("conn") is _WS_CONN


def process_pending_commands():
    commands = pop_pending_commands()
    for command in commands:
        process_pending_command(command)


def process_pending_command(command):
    request_id = command.get("id")
    action = command.get("action")
    payload = command.get("payload") or {}
    response = {
        "type": "result",
        "id": request_id,
        "ok": True,
        "data": {},
        "ts": datetime.now().isoformat(),
    }

    if not is_command_connection_active(command):
        log_warn("Dropped external command from inactive WebSocket: %s id=%s" % (action, request_id))
        return

    try:
        age = time.time() - float(command.get("received_at") or time.time())
        if age > COMMAND_QUEUE_TIMEOUT_SECONDS:
            raise Exception("External command expired before PTrade callback processed it")
        log.info("Executing external command on PTrade callback: %s id=%s" % (action, request_id))
        response["data"] = execute_command(action, payload)
    except Exception as e:
        log.error("External command failed: %s id=%s error=%s" % (action, request_id, str(e)))
        response["ok"] = False
        response["error"] = str(e)

    if not is_command_connection_active(command):
        log_warn("Dropped external command response after WebSocket disconnect: %s id=%s" % (action, request_id))
        return

    try:
        send_ws_json(command.get("loop"), command.get("conn"), response)
    except Exception as e:
        log_warn("Failed to enqueue external command response: %s id=%s error=%s" % (action, request_id, str(e)))


def execute_command(action, payload):
    if action in ("get_quotes", "get_bid_ask", "quote.batch"):
        return get_quotes(payload.get("symbols") or [])
    if action in ("get_snapshots", "snapshot.batch", "quote.snapshot"):
        return get_snapshots_payload(payload.get("symbols") or [])
    if action in ("place_orders", "order.batch"):
        return place_order_batch(payload.get("orders") or [])
    if action in ("cancel_orders", "order.cancel"):
        return cancel_order_batch(payload.get("orders") or payload.get("order_ids") or [])
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
    """Convert PTrade client code to backend code, e.g. 600000.XSHG -> 600000.SH."""
    if not symbol:
        return None
    parts = str(symbol).strip().upper().split(".")
    if len(parts) != 2:
        return str(symbol).strip().upper()

    first = parts[0]
    second = parts[1]
    market_aliases = {
        "XSHG": "SH",
        "SH": "SH",
        "SS": "SH",
        "XSHE": "SZ",
        "SZ": "SZ",
        "XBSE": "BJ",
        "BJ": "BJ",
    }
    if first in market_aliases:
        return "%s.%s" % (second, market_aliases[first])

    return "%s.%s" % (first, market_aliases.get(second, second))


def convert_to_client_code(symbol):
    """Convert backend code to official PTrade code, e.g. 600000.SH -> 600000.XSHG."""
    if not symbol:
        return None
    text = str(symbol).strip().upper()
    parts = text.split(".")
    if len(parts) == 1 and len(text) == 6 and text.isdigit():
        if text.startswith(("60", "68", "51", "52", "56", "58", "50", "11")):
            return "%s.XSHG" % text
        if text.startswith(("00", "30", "20", "15", "12", "13")):
            return "%s.XSHE" % text
        if text.startswith(("43", "83", "87", "88", "92")):
            return "%s.XBSE" % text
    if len(parts) != 2:
        return text

    first = parts[0]
    second = parts[1]
    market_aliases = {
        "XSHG": "XSHG",
        "SH": "XSHG",
        "SS": "XSHG",
        "XSHE": "XSHE",
        "SZ": "XSHE",
        "XBSE": "XBSE",
        "BJ": "XBSE",
    }
    if first in market_aliases:
        return "%s.%s" % (second, market_aliases[first])

    return "%s.%s" % (first, market_aliases.get(second, second))


def value_of(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def first_value(obj, keys, default=None):
    for key in keys:
        value = value_of(obj, key, None)
        if value is not None:
            return value
    return default


def symbol_lookup_candidates(symbol):
    candidates = []

    def add(value):
        if value is None:
            return
        text = str(value).strip().upper()
        if text and text not in candidates:
            candidates.append(text)

    add(symbol)
    api_symbol = convert_to_api_code(symbol)
    add(api_symbol)
    add(convert_to_client_code(api_symbol))
    parts = str(api_symbol or "").split(".")
    if len(parts) == 2:
        code, market = parts
        add("%s.%s" % (market, code))
        if market == "SH":
            add("%s.SS" % code)
            add("SS.%s" % code)
            add("%s.XSHG" % code)
            add("XSHG.%s" % code)
        elif market == "SZ":
            add("%s.XSHE" % code)
            add("XSHE.%s" % code)
        elif market == "BJ":
            add("%s.XBSE" % code)
            add("XBSE.%s" % code)
    return candidates


def get_current_dt():
    context = _CURRENT_CONTEXT
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


def get_gear_price_for_symbol(client_symbol):
    """Use get_gear_price to fetch bid/ask depth for a single symbol.

    Returns a dict with at least ``bid_grp`` and ``offer_grp`` keys,
    or an empty dict when data is unavailable.
    """
    try:
        data = get_gear_price(client_symbol)
    except Exception as exc:
        log_warn("get_gear_price(%s) failed: %s" % (client_symbol, exc))
        return {}
    if not data:
        return {}
    # get_gear_price returns {bid_grp: {1:[p,v,c],...}, offer_grp: {1:[p,v,c],...}}
    if value_of(data, "bid_grp") is not None or value_of(data, "offer_grp") is not None:
        return data
    for key in symbol_lookup_candidates(client_symbol):
        item = value_of(data, key)
        if item:
            return item
    return {}


def normalize_quote_from_gear_price(client_symbol, gear_data):
    """Build a normalized quote dict from get_gear_price output."""
    bid_levels = normalize_depth_group(value_of(gear_data, "bid_grp"))
    ask_levels = normalize_depth_group(value_of(gear_data, "offer_grp"))
    best_bid = bid_levels[0] if bid_levels else {}
    best_ask = ask_levels[0] if ask_levels else {}
    return {
        "symbol": convert_to_api_code(client_symbol),
        "client_symbol": client_symbol,
        "ok": True,
        "price": best_bid.get("price") or best_ask.get("price"),
        "bid": best_bid.get("price"),
        "bid_size": best_bid.get("volume"),
        "ask": best_ask.get("price"),
        "ask_size": best_ask.get("volume"),
        "bid_levels": bid_levels,
        "ask_levels": ask_levels,
        "timestamp": datetime.now().isoformat(),
    }


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
    for key in symbol_lookup_candidates(client_symbol):
        item = snapshots.get(key)
        if item:
            return item
    return {}


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
        snapshot = None
        for key in symbol_lookup_candidates(client_symbol):
            snapshot = snapshots.get(key)
            if snapshot:
                break
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
            snapshot = None
            for key in symbol_lookup_candidates(client_symbol):
                snapshot = snapshots.get(key)
                if snapshot:
                    break
            if not snapshot:
                raise Exception("snapshot not found")
            item.update(normalize_quote_from_snapshot(client_symbol, snapshot))
        except Exception as e:
            item["ok"] = False
            item["error"] = str(e)
        results.append(item)
    return {"quotes": results}


def get_limit_price_from_gear_data(symbol, side, quantity, gear_data):
    """Use the order book from get_gear_price to pick a limit price that covers the requested quantity."""
    if not gear_data or not value_of(gear_data, "bid_grp") or not value_of(gear_data, "offer_grp"):
        raise Exception("获取 %s 档位价格失败: 数据为空" % symbol)

    group = value_of(gear_data, "offer_grp") if side == "BUY" else value_of(gear_data, "bid_grp")
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
    return get_limit_price_from_gear_data(symbol, side, quantity, get_gear_price_for_symbol(symbol))


def get_int_or_none(value):
    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        return None


def calculate_order_price(symbol, side, quantity, price_level):
    gear_data = get_gear_price_for_symbol(symbol)
    if not gear_data:
        raise Exception("获取 %s 盘口数据失败: get_gear_price 返回空" % symbol)

    quote = normalize_quote_from_gear_price(symbol, gear_data)
    level = get_int_or_none(price_level)
    if level is None or level == -1:
        price = get_limit_price_from_gear_data(symbol, side, quantity, gear_data)
        source = "ptrade_depth_fallback"
    elif level == 0:
        # level 0 = use best bid/ask mid or best opposite side price
        if side == "BUY":
            price = quote.get("ask") or quote.get("bid")
        else:
            price = quote.get("bid") or quote.get("ask")
        source = "best_price"
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

    price = float(price)

    log.info(
        "%s 定价结果: price=%.4f source=%s level=%s bid=%s ask=%s"
        % (symbol, price, source, level, quote.get("bid"), quote.get("ask"))
    )

    return {
        "price": price,
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


def cancel_order_batch(orders):
    results = []
    for item in orders:
        order_id = item.get("order_id") if isinstance(item, dict) else item
        client_order_id = item.get("client_order_id") if isinstance(item, dict) else None
        result = {
            "client_order_id": client_order_id,
            "order_id": order_id,
            "ok": False,
            "status": "FAILED",
        }
        try:
            cancel_order(order_id)
            result["ok"] = True
            result["status"] = "CANCEL_REQUESTED"
            result["message"] = "撤单指令已提交"
        except Exception as e:
            result["message"] = str(e)
        results.append(result)
    return {"orders": results}


def normalize_order_type(order_request):
    order_type = str(order_request.get("order_type") or "LIMIT").upper()
    if order_type in ("MARKET", "MKT"):
        return "MARKET"
    return "LIMIT"


def is_sh_market_symbol(client_symbol):
    return str(client_symbol or "").upper().endswith((".XSHG", ".SS", ".SH"))


def is_star_market_symbol(symbol):
    api_symbol = convert_to_api_code(symbol)
    if not api_symbol:
        return False
    parts = str(api_symbol).upper().split(".")
    return len(parts) == 2 and parts[1] == "SH" and parts[0].startswith(("688", "689"))


def is_bj_market_symbol(symbol):
    api_symbol = convert_to_api_code(symbol)
    if not api_symbol:
        return False
    parts = str(api_symbol).upper().split(".")
    return len(parts) == 2 and parts[1] == "BJ"


def validate_market_order_type(client_symbol, market_type):
    market_type = int(market_type)
    allowed = (0, 1, 2, 4) if is_sh_market_symbol(client_symbol) else (0, 2, 3, 4, 5)
    if market_type not in allowed:
        raise Exception("%s 不支持市价委托类型 %s" % (client_symbol, market_type))
    return market_type


def bool_value(value, default=False):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y", "on"):
        return True
    if text in ("0", "false", "no", "n", "off"):
        return False
    return default


def get_position_candidates(client_symbol, api_symbol):
    candidates = [
        client_symbol,
        api_symbol,
        convert_to_api_code(client_symbol),
        convert_to_client_code(api_symbol),
    ]
    normalized = []
    for item in candidates:
        if not item:
            continue
        text = str(item).upper()
        if text not in normalized:
            normalized.append(text)
    return normalized


def get_position_quantities(client_symbol, api_symbol):
    positions = get_positions() or {}
    position_items = positions.items() if isinstance(positions, dict) else [(None, pos) for pos in positions]
    candidates = get_position_candidates(client_symbol, api_symbol)
    for key, pos in position_items:
        symbol = first_value(pos, ("sid", "symbol", "stock_code"), key)
        pos_candidates = get_position_candidates(symbol, convert_to_api_code(symbol))
        if not set(candidates).intersection(set(pos_candidates)):
            continue
        sellable_quantity = value_of(
            pos,
            "enable_amount",
            value_of(
                pos,
                "available_quantity",
                value_of(pos, "sellable_quantity", first_value(pos, ("amount", "current_amount"), 0)),
            ),
        )
        position_quantity = first_value(pos, ("amount", "current_amount", "quantity"), 0)
        try:
            sellable_quantity = max(int(float(sellable_quantity or 0)), 0)
        except Exception:
            sellable_quantity = 0
        try:
            position_quantity = max(int(float(position_quantity or 0)), 0)
        except Exception:
            position_quantity = 0
        return {
            "sellable_quantity": sellable_quantity,
            "position_quantity": position_quantity,
        }
    return {
        "sellable_quantity": 0,
        "position_quantity": 0,
    }


def apply_sell_quantity_clip(order_request, client_symbol, api_symbol, side, quantity):
    clip_enabled = bool_value(
        order_request.get(
            "clip_sell_to_available",
            order_request.get("clip_sell_quantity_to_available", False),
        ),
        False,
    )
    meta = {
        "requested_quantity": quantity,
        "submitted_quantity": quantity,
        "quantity_clipped": False,
        "clip_sell_to_available": clip_enabled,
        "sellable_quantity": None,
        "position_quantity": None,
        "block_reason": None,
        "block_message": None,
    }
    star_market = is_star_market_symbol(api_symbol or client_symbol)
    if side != "SELL" or (not clip_enabled and not star_market):
        return quantity, meta

    quantities = get_position_quantities(client_symbol, api_symbol)
    sellable_quantity = quantities.get("sellable_quantity") or 0
    position_quantity = quantities.get("position_quantity") or 0
    clipped_quantity = min(quantity, sellable_quantity) if clip_enabled else quantity
    meta.update({
        "submitted_quantity": clipped_quantity,
        "quantity_clipped": clipped_quantity != quantity,
        "sellable_quantity": sellable_quantity,
        "position_quantity": position_quantity,
    })
    if clipped_quantity != quantity:
        log_warn(
            "SELL %s 数量按可卖数量裁剪: 请求=%d, 持仓=%d, 可卖=%d, 提交=%d"
            % (client_symbol, quantity, position_quantity, sellable_quantity, clipped_quantity)
        )
    return clipped_quantity, meta


def order_clip_fields(order_request, quantity):
    requested_quantity = int(order_request.get("_requested_quantity") or quantity)
    submitted_quantity = int(order_request.get("_submitted_quantity") or quantity)
    quantity_clipped = bool_value(order_request.get("_quantity_clipped"), False)
    return {
        "requested_quantity": requested_quantity,
        "submitted_quantity": submitted_quantity,
        "quantity_clipped": quantity_clipped,
        "clip_sell_to_available": bool_value(order_request.get("clip_sell_to_available"), False),
        "sellable_quantity": order_request.get("_sellable_quantity"),
        "position_quantity": order_request.get("_position_quantity"),
        "block_reason": order_request.get("_block_reason"),
        "block_message": order_request.get("_block_message"),
    }


def make_preflight_rejection(
    order_request,
    client_symbol,
    api_symbol,
    side,
    requested_quantity,
    status,
    error_code,
    message,
):
    order_request["_requested_quantity"] = requested_quantity
    order_request["_submitted_quantity"] = 0
    order_request["_quantity_clipped"] = False
    order_request.setdefault("_sellable_quantity", None)
    order_request.setdefault("_position_quantity", None)
    order_request.setdefault("_block_reason", None)
    order_request.setdefault("_block_message", None)
    log.error("交易规则校验失败: %s" % message)
    return {
        "ok": False,
        "client_order_id": order_request.get("client_order_id"),
        "status": status,
        "error_code": error_code,
        "retryable": False,
        "symbol": api_symbol,
        "client_symbol": client_symbol,
        "side": side,
        "quantity": 0,
        **order_clip_fields(order_request, 0),
        "order_type": normalize_order_type(order_request),
        "calculated_price": None,
        "price_source": None,
        "price_level": order_request.get("price_level"),
        "snapshot_time": None,
        "submitted_price": None,
        "order_id": None,
        "raw_status": None,
        "message": message,
    }


def validate_order_rules(order_request, client_symbol, api_symbol, side, quantity, requested_quantity):
    if is_bj_market_symbol(api_symbol or client_symbol):
        return make_preflight_rejection(
            order_request,
            client_symbol,
            api_symbol,
            side,
            requested_quantity,
            "NOT_SUPPORTED",
            "UNSUPPORTED_MARKET",
            "PTrade执行器暂不支持北交所标的 %s，未提交订单" % api_symbol,
        )

    if not is_star_market_symbol(api_symbol or client_symbol):
        return None

    if side == "BUY" and 0 < quantity < 200:
        return make_preflight_rejection(
            order_request,
            client_symbol,
            api_symbol,
            side,
            requested_quantity,
            "REJECTED",
            "INVALID_LOT_SIZE",
            "科创板最低买入申报200股，计划买入%d股，未提交订单" % quantity,
        )

    if side == "SELL" and 0 < quantity < 200:
        sellable_quantity = order_request.get("_sellable_quantity")
        position_quantity = order_request.get("_position_quantity")
        if sellable_quantity is None or position_quantity is None:
            quantities = get_position_quantities(client_symbol, api_symbol)
            sellable_quantity = quantities.get("sellable_quantity") or 0
            position_quantity = quantities.get("position_quantity") or 0
            order_request["_sellable_quantity"] = sellable_quantity
            order_request["_position_quantity"] = position_quantity
        sellable_quantity = int(sellable_quantity or 0)
        position_quantity = int(position_quantity or 0)
        if quantity != sellable_quantity:
            return make_preflight_rejection(
                order_request,
                client_symbol,
                api_symbol,
                side,
                requested_quantity,
                "REJECTED",
                "INVALID_LOT_SIZE",
                (
                    "科创板最低卖出申报200股，当前持仓%d股，可卖%d股，计划卖出%d股，未提交订单"
                    % (position_quantity, sellable_quantity, quantity)
                ),
            )
    return None


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
    try:
        calculated = calculate_order_price(client_symbol, side, quantity, price_level)
    except Exception as e:
        log_warn("Market order protection price failed for %s: %s" % (client_symbol, e))
        return None
    calculated["price_source"] = "protection_%s" % calculated["price_source"]
    return calculated


def place_market_order(order_request, client_symbol, api_symbol, side, quantity):
    client_order_id = order_request.get("client_order_id")
    market_type_value = order_request.get("market_type")
    market_type = validate_market_order_type(client_symbol, 0 if market_type_value is None else market_type_value)
    protection = get_market_protection_price(order_request, client_symbol, side, quantity)
    protection_price = protection.get("price") if protection else None
    signed_quantity = quantity if side == "BUY" else -quantity

    if protection_price is None:
        order_sn = order_market(client_symbol, signed_quantity, market_type)
    else:
        order_sn = order_market(client_symbol, signed_quantity, market_type, protection_price)
    remember_client_order_id(order_sn, client_order_id, side=side)

    message = "%s %s 市价单, 数量: %d, 市价类型: %s" % (side, client_symbol, quantity, market_type)
    if protection_price is not None:
        message += ", 保护限价: %s" % protection_price
    log.info("交易指令已提交: %s" % message)

    raw_status = None
    if order_sn:
        order_info = get_first_order(order_sn)
        remember_client_order_aliases(order_info, client_order_id, side=side)
        raw_status = str(value_of(order_info, "status", ""))
        if raw_status == "9":
            log.error("交易失败: %s失败(被拒绝)" % message)
            return {
                "ok": False,
                "client_order_id": client_order_id,
                "status": "FAILED",
                "error_code": "BROKER_REJECTED",
                "retryable": True,
                "symbol": api_symbol,
                "client_symbol": client_symbol,
                "side": side,
                "quantity": quantity,
                **order_clip_fields(order_request, quantity),
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
        "client_order_id": client_order_id,
        "status": "SUCCESS",
        "symbol": api_symbol,
        "client_symbol": client_symbol,
        "side": side,
        "quantity": quantity,
        **order_clip_fields(order_request, quantity),
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
    client_order_id = order_request.get("client_order_id")
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
    remember_client_order_id(order_sn, client_order_id, side=side)

    status = "FAILED"
    message = ""
    raw_status = None
    if order_sn:
        order_info = get_first_order(order_sn)
        remember_client_order_aliases(order_info, client_order_id, side=side)
        raw_status = str(value_of(order_info, "status", ""))
        if raw_status == "9":
            message = "%s %s失败(被拒绝)" % (side, client_symbol)
        else:
            status = "SUCCESS"
            message = "%s %s, 数量: %d, 价格: %s" % (side, client_symbol, quantity, limit_price)
    else:
        message = "%s %s失败(无订单号)" % (side, client_symbol)

    if status == "SUCCESS":
        log.info("交易指令已提交: %s" % message)
    else:
        log.error("交易失败: %s" % message)

    return {
        "ok": status == "SUCCESS",
        "client_order_id": client_order_id,
        "status": status,
        "error_code": "BROKER_REJECTED" if raw_status == "9" else None,
        "retryable": True if raw_status == "9" else None,
        "symbol": api_symbol,
        "client_symbol": client_symbol,
        "side": side,
        "quantity": quantity,
        **order_clip_fields(order_request, quantity),
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
    order_request = dict(order_request or {})
    symbol = order_request.get("symbol")
    client_symbol = convert_to_client_code(symbol)
    api_symbol = convert_to_api_code(client_symbol)
    side = str(order_request.get("side") or "").upper()
    quantity = int(order_request.get("quantity") or 0)
    requested_quantity = quantity

    if side not in ("BUY", "SELL"):
        raise Exception("orders[%d].side must be BUY or SELL" % index)
    if quantity <= 0:
        raise Exception("orders[%d].quantity must be greater than 0" % index)

    order_request["_requested_quantity"] = requested_quantity
    order_request["_submitted_quantity"] = quantity
    order_request["_quantity_clipped"] = False
    quantity, clip_meta = apply_sell_quantity_clip(order_request, client_symbol, api_symbol, side, quantity)
    order_request["_submitted_quantity"] = quantity
    order_request["_quantity_clipped"] = clip_meta.get("quantity_clipped")
    order_request["_sellable_quantity"] = clip_meta.get("sellable_quantity")
    order_request["_position_quantity"] = clip_meta.get("position_quantity")
    order_request["_block_reason"] = clip_meta.get("block_reason")
    order_request["_block_message"] = clip_meta.get("block_message")
    order_request["clip_sell_to_available"] = clip_meta.get("clip_sell_to_available")
    rule_result = validate_order_rules(order_request, client_symbol, api_symbol, side, quantity, requested_quantity)
    if rule_result:
        return rule_result
    if quantity <= 0:
        message = clip_meta.get("block_message") or (
            "SELL %s 可卖数量不足，请求%d，持仓%d，可卖%d，未提交订单" % (
                client_symbol,
                requested_quantity,
                int(clip_meta.get("position_quantity") or 0),
                int(clip_meta.get("sellable_quantity") or 0),
            )
        )
        log.error("交易失败: %s" % message)
        return {
            "ok": False,
            "client_order_id": order_request.get("client_order_id"),
            "status": "FAILED",
            "symbol": api_symbol,
            "client_symbol": client_symbol,
            "side": side,
            "quantity": 0,
            **order_clip_fields(order_request, 0),
            "order_type": normalize_order_type(order_request),
            "calculated_price": None,
            "price_source": None,
            "price_level": order_request.get("price_level"),
            "snapshot_time": None,
            "submitted_price": None,
            "order_id": None,
            "raw_status": None,
            "message": message,
        }

    if normalize_order_type(order_request) == "MARKET":
        return place_market_order(order_request, client_symbol, api_symbol, side, quantity)
    return place_limit_order(order_request, client_symbol, api_symbol, side, quantity)


def get_first_order(order_sn, max_attempts=3, wait_seconds=0.5):
    """Query order status, retrying up to max_attempts times until status is
    no longer '0' (just submitted) or attempts are exhausted."""
    order_info = None
    for attempt in range(max_attempts):
        try:
            orders = get_order(order_sn)
            if isinstance(orders, list):
                order_info = orders[0] if orders else None
            else:
                order_info = orders
        except Exception:
            order_info = None
        status = str(value_of(order_info, "status", ""))
        if status != "0" and status != "":
            return order_info
        if attempt < max_attempts - 1:
            time.sleep(wait_seconds)
    return order_info


def remember_client_order_id(order_sn, client_order_id, side=None):
    if not order_sn or not client_order_id:
        return
    if not hasattr(g, "order_client_id_by_order_id"):
        g.order_client_id_by_order_id = {}
    g.order_client_id_by_order_id[str(order_sn)] = client_order_id
    if side:
        if not hasattr(g, "order_side_by_order_id"):
            g.order_side_by_order_id = {}
        g.order_side_by_order_id[str(order_sn)] = str(side).upper()
    if not hasattr(g, "order_last_known_status"):
        g.order_last_known_status = {}
    g.order_last_known_status[str(order_sn)] = "0"


def remember_client_order_aliases(order_item, client_order_id, side=None):
    if not order_item or not client_order_id:
        return
    for key in ("order_id", "id", "entrust_no"):
        value = value_of(order_item, key)
        if value:
            remember_client_order_id(value, client_order_id, side=side)


def order_aliases(order_item):
    aliases = []
    for key in ("order_id", "id", "entrust_no"):
        value = value_of(order_item, key)
        if value is None:
            continue
        text = str(value)
        if text not in aliases:
            aliases.append(text)
    return aliases


TERMINAL_ORDER_STATUSES = {"5", "6", "8", "9"}
ORDER_FILL_STATUSES = {"4", "5", "7", "8"}


def sync_tracked_order_statuses():
    """Poll tracked orders and push order_event for any status changes.

    PTrade does not fire on_order_response for orders rejected at the broker
    level (status=9).  This function runs on each handle_data / tick_data
    callback to compensate.
    """
    tracked = getattr(g, "order_client_id_by_order_id", {})
    if not tracked:
        return

    try:
        last_status = getattr(g, "order_last_known_status", {})

        try:
            all_orders = get_orders() or []
        except Exception as exc:
            log_warn("get_orders failed, fallback to get_all_orders: %s" % exc)
            all_orders = get_all_orders() or []

        # Build lookup by order_id (order_sn)
        order_map = {}
        for item in all_orders:
            for oid in order_aliases(item):
                order_map[oid] = item

        current_dt = get_current_dt()
        changed_orders = []
        finished_keys = []

        for order_sn in list(tracked.keys()):
            item = order_map.get(str(order_sn))
            if item is None:
                continue
            status = str(value_of(item, "status", ""))
            prev = last_status.get(str(order_sn))
            if status == prev:
                continue
            # Status changed
            last_status[str(order_sn)] = status
            changed_orders.append(normalize_order(item, current_dt))
            if status in TERMINAL_ORDER_STATUSES:
                finished_keys.extend(order_aliases(item) or [order_sn])

        if changed_orders:
            log.info("sync_tracked_order_statuses: pushing %d order updates" % len(changed_orders))
            send_ws_event({
                "type": "order_event",
                "source": "status_sync",
                "orders": changed_orders,
                "ts": datetime.now().isoformat(),
            })

        # Clean up terminal orders from tracking
        for key in finished_keys:
            tracked.pop(str(key), None)
            last_status.pop(str(key), None)
    except Exception as exc:
        log_warn("sync_tracked_order_statuses failed: %s" % exc)


def lookup_client_order_id(order_id):
    if not order_id:
        return None
    try:
        mapping = getattr(g, "order_client_id_by_order_id", {})
    except NameError:
        return None
    return mapping.get(str(order_id))


def lookup_order_side(order_id):
    if not order_id:
        return None
    try:
        mapping = getattr(g, "order_side_by_order_id", {})
    except NameError:
        return None
    return mapping.get(str(order_id))


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

    order_id = first_value(order_item, ("order_id", "id", "entrust_no"))
    side = lookup_order_side(order_id) or lookup_order_side(value_of(order_item, "entrust_no"))
    if side:
        return side
    return None


def get_order_filled_quantity(order_item):
    filled_value = value_of(order_item, "filled", None)
    if filled_value is not None:
        quantity = filled_value
    else:
        status = str(value_of(order_item, "status", ""))
        if status not in ORDER_FILL_STATUSES:
            return 0
        quantity = first_value(order_item, ("filled_quantity", "business_amount", "filled_amount"), 0)
    try:
        quantity = abs(int(quantity or 0))
    except Exception:
        quantity = 0
    return quantity


def normalize_order(order_item, current_dt):
    raw_symbol = first_value(order_item, ("symbol", "stock_code", "security", "sid"))
    quantity = first_value(order_item, ("amount", "quantity", "entrust_amount"), 0) or 0
    try:
        quantity = abs(int(float(quantity)))
    except Exception:
        pass

    order_id = first_value(order_item, ("order_id", "id", "entrust_no"))
    entrust_no = value_of(order_item, "entrust_no")
    return {
        "client_order_id": value_of(order_item, "client_order_id", lookup_client_order_id(order_id) or lookup_client_order_id(entrust_no)),
        "symbol": convert_to_api_code(raw_symbol),
        "client_symbol": raw_symbol,
        "side": get_order_side(order_item),
        "quantity": quantity,
        "price": first_value(order_item, ("price", "limit", "entrust_price", "business_price")),
        "status": value_of(order_item, "status"),
        "order_id": order_id,
        "entrust_no": value_of(order_item, "entrust_no", order_id),
        "entrust_bs": value_of(order_item, "entrust_bs"),
        "entrust_type": value_of(order_item, "entrust_type"),
        "filled_quantity": get_order_filled_quantity(order_item),
        "avg_fill_price": first_value(order_item, ("business_price", "avg_fill_price", "filled_price")),
        "submitted_at": first_value(order_item, ("entrust_time", "order_time", "dt", "created"), current_dt.isoformat()),
        "event_time": current_dt.isoformat(),
        "raw": stringify_unknown_fields(order_item),
    }


def normalize_trade(trade_item, current_dt):
    raw_symbol = first_value(trade_item, ("symbol", "stock_code", "security", "sid"))
    order_id = value_of(trade_item, "order_id", value_of(trade_item, "entrust_no"))
    quantity = first_value(trade_item, ("business_amount", "quantity", "filled_amount", "filled"), 0) or 0
    try:
        quantity = abs(int(quantity))
    except Exception:
        pass

    return {
        "client_order_id": value_of(trade_item, "client_order_id", lookup_client_order_id(order_id)),
        "symbol": convert_to_api_code(raw_symbol),
        "client_symbol": raw_symbol,
        "side": get_order_side(trade_item),
        "quantity": quantity,
        "price": value_of(trade_item, "business_price", value_of(trade_item, "price")),
        "amount": value_of(trade_item, "business_balance", value_of(trade_item, "amount")),
        "order_id": order_id,
        "entrust_no": value_of(trade_item, "entrust_no", order_id),
        "business_no": first_value(trade_item, ("business_id", "business_no", "deal_no", "match_no")),
        "business_time": value_of(trade_item, "business_time", value_of(trade_item, "trade_time", current_dt.isoformat())),
        "traded_at": value_of(trade_item, "business_time", value_of(trade_item, "trade_time", current_dt.isoformat())),
        "raw": stringify_unknown_fields(trade_item),
    }


def on_order_response(context, order_list):
    update_current_context(context)
    current_dt = get_current_dt()
    orders = [normalize_order(item, current_dt) for item in (order_list or [])]
    send_ws_event({
        "type": "order_event",
        "orders": orders,
        "ts": datetime.now().isoformat(),
    })
    # Update tracking so sync_tracked_order_statuses won't re-push
    tracked = getattr(g, "order_client_id_by_order_id", {})
    last_status = getattr(g, "order_last_known_status", {})
    for item in (order_list or []):
        oid = str(first_value(item, ("order_id", "id", "entrust_no"), ""))
        status = str(value_of(item, "status", ""))
        client_order_id = lookup_client_order_id(oid) or lookup_client_order_id(value_of(item, "entrust_no"))
        remember_client_order_aliases(item, client_order_id, side=lookup_order_side(oid))
        aliases = order_aliases(item) or [oid]
        for alias in aliases:
            last_status[str(alias)] = status
        if oid in last_status:
            last_status[oid] = status
        if status in TERMINAL_ORDER_STATUSES:
            for alias in aliases:
                tracked.pop(str(alias), None)
                last_status.pop(str(alias), None)
    process_pending_commands()


def on_trade_response(context, trade_list):
    update_current_context(context)
    current_dt = get_current_dt()
    trades = [normalize_trade(item, current_dt) for item in (trade_list or [])]
    send_ws_event({
        "type": "trade_event",
        "trades": trades,
        "ts": datetime.now().isoformat(),
    })
    process_pending_commands()


def get_today_orders_payload():
    current_dt = get_current_dt()
    today_orders = get_all_orders() or []
    return {
        "current_time": current_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "orders": [normalize_order(item, current_dt) for item in today_orders],
    }


def normalize_deliver_date(value):
    if value is None or value == "":
        return get_current_dt().strftime("%Y%m%d")
    text = str(value).strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10].replace("-", "")
    return text


def normalize_deliver_records(records):
    if records is None:
        return []
    if hasattr(records, "to_dict"):
        try:
            records = records.to_dict("records")
        except Exception:
            records = records.to_dict()
    if isinstance(records, dict):
        if all(isinstance(value, dict) for value in records.values()):
            records = records.values()
        else:
            records = [records]
    result = []
    for item in records or []:
        if isinstance(item, dict):
            result.append(stringify_unknown_fields(item))
        else:
            result.append({"value": str(item)})
    return result


def get_deliver_payload(start_date=None, end_date=None):
    start = normalize_deliver_date(start_date)
    end = normalize_deliver_date(end_date or start_date)
    try:
        deliver_func = get_deliver
    except NameError:
        raise Exception("PTrade get_deliver is unavailable in this runtime")
    records = deliver_func(start, end) or []
    return {
        "start_date": start,
        "end_date": end,
        "records": normalize_deliver_records(records),
    }


def push_deliver_records():
    """在 before_trading_start 阶段主动推送近期交割单给后端。"""
    try:
        current_dt = get_current_dt()
        # 官方文档：仅支持查询上一个交易日（包含）之前的交割单信息
        yesterday = (current_dt - timedelta(days=1)).strftime("%Y%m%d")
        # A股可能存在长达10天的不开盘假期（如春节连周末），所以往前倒推15天
        start_dt = current_dt - timedelta(days=15)
        start_date_str = start_dt.strftime("%Y%m%d")
        payload = get_deliver_payload(start_date_str, yesterday)
        records = payload.get("records") or []
        send_ws_event({
            "type": "deliver_event",
            "data": payload,
            "ts": datetime.now().isoformat(),
        })
        log.info("push_deliver_records: pushed %d records from %s to %s" % (len(records), start_date_str, yesterday))
    except Exception as exc:
        log_warn("push_deliver_records failed: %s" % exc)


def normalize_position(pos):
    symbol = first_value(pos, ("sid", "symbol", "stock_code"))
    return {
        "symbol": convert_to_api_code(symbol),
        "client_symbol": symbol,
        "quantity": first_value(pos, ("amount", "current_amount", "quantity"), 0),
        "available_quantity": first_value(pos, ("enable_amount", "available_quantity", "sellable_quantity"), 0),
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
            if first_value(pos, ("amount", "current_amount", "quantity"), 0) != 0
        ],
    }


def get_assets_payload():
    context = _CURRENT_CONTEXT
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

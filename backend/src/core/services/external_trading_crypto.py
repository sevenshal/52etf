import base64
import hashlib
import hmac
import json
import os
import struct
import time
from typing import Any, Dict


RSA_PUBLIC_N_HEX = (
    "d10e83e0f75ddef1fa41d524bbf4ff76dc9f28a1d1d376f09a9920b0e66362503b5fba39003215f68a911bb33d160745f9f452bfa775c73ca9a3741509b1e5f0e74f35fe2f7e09e4da3bd0eefdea5765322b62a90c080e0ab500853ce8147d7e837dd3cda9c089fe47934065a0da0f3e00cb9de406bd254e0e585d5c67f7af3e0d0729847ca04e69b9ce81e598cdde04e50305e7ecdd0fbeba18a30f307ac795f8145bb149e8a855eaff687077f95305b6419fbf3878dca91edef4666f51fdcdd1c70495fa94f74bdd2733261e04cffaa24a8b040d46897e940ad25756093538d85b321b115cd29970cd51fba8b18c48b2b6e406a71d72a9b58b402d0025854b"
)
RSA_PUBLIC_E = 65537
SHARED_KEY_B64 = "W4YVL+KUu7gcLlAuMGf/oD5T4y0RXjNvxVgAWrHVNe4="
SIGNATURE_MAX_SKEW_SECONDS = 90

_SHA256_DIGESTINFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")
_SHARED_KEY = base64.b64decode(SHARED_KEY_B64)
_ENC_KEY = hashlib.sha256(b"external-trading-enc:" + _SHARED_KEY).digest()
_MAC_KEY = hashlib.sha256(b"external-trading-mac:" + _SHARED_KEY).digest()
_USED_NONCES: Dict[str, float] = {}


class ExternalTradingCryptoError(Exception):
    pass


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + padding).encode("ascii"))


def canonical_handshake_payload(account_id: str, name: str, identifier: str, ts: str, nonce: str) -> bytes:
    payload = {
        "account_id": account_id,
        "identifier": identifier,
        "name": name,
        "nonce": nonce,
        "ts": str(ts),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _rsa_sha256_expected_block(message: bytes, key_size: int) -> bytes:
    digest_info = _SHA256_DIGESTINFO_PREFIX + hashlib.sha256(message).digest()
    padding_len = key_size - len(digest_info) - 3
    if padding_len < 8:
        raise ExternalTradingCryptoError("invalid rsa key size")
    return b"\x00\x01" + (b"\xff" * padding_len) + b"\x00" + digest_info


def verify_rsa_sha256_signature(message: bytes, signature_b64: str) -> bool:
    try:
        n = int(RSA_PUBLIC_N_HEX, 16)
        e = RSA_PUBLIC_E
        key_size = (n.bit_length() + 7) // 8
        signature = b64url_decode(signature_b64)
        if len(signature) != key_size:
            return False
        signature_int = int.from_bytes(signature, "big")
        actual = pow(signature_int, e, n).to_bytes(key_size, "big")
        expected = _rsa_sha256_expected_block(message, key_size)
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def verify_handshake_signature(
    account_id: str,
    name: str,
    identifier: str,
    ts: str,
    nonce: str,
    signature: str,
) -> None:
    if not ts or not nonce or not signature:
        raise ExternalTradingCryptoError("signature, ts and nonce are required")

    try:
        timestamp = int(ts)
    except Exception as exc:
        raise ExternalTradingCryptoError("invalid timestamp") from exc

    now = int(time.time())
    if abs(now - timestamp) > SIGNATURE_MAX_SKEW_SECONDS:
        raise ExternalTradingCryptoError("signature timestamp expired")

    _drop_expired_nonces(now)
    nonce_key = "%s:%s:%s" % (account_id, identifier, nonce)
    if nonce_key in _USED_NONCES:
        raise ExternalTradingCryptoError("signature nonce replayed")

    message = canonical_handshake_payload(account_id, name, identifier, str(ts), nonce)
    if not verify_rsa_sha256_signature(message, signature):
        raise ExternalTradingCryptoError("invalid signature")

    _USED_NONCES[nonce_key] = float(now)


def _drop_expired_nonces(now: int):
    expire_before = now - SIGNATURE_MAX_SKEW_SECONDS
    for nonce_key, seen_at in list(_USED_NONCES.items()):
        if seen_at < expire_before:
            _USED_NONCES.pop(nonce_key, None)


def _rotl32(value: int, shift: int) -> int:
    return ((value << shift) & 0xffffffff) | (value >> (32 - shift))


def _quarter_round(state, a, b, c, d):
    state[a] = (state[a] + state[b]) & 0xffffffff
    state[d] ^= state[a]
    state[d] = _rotl32(state[d], 16)
    state[c] = (state[c] + state[d]) & 0xffffffff
    state[b] ^= state[c]
    state[b] = _rotl32(state[b], 12)
    state[a] = (state[a] + state[b]) & 0xffffffff
    state[d] ^= state[a]
    state[d] = _rotl32(state[d], 8)
    state[c] = (state[c] + state[d]) & 0xffffffff
    state[b] ^= state[c]
    state[b] = _rotl32(state[b], 7)


def _chacha20_block(key: bytes, counter: int, nonce: bytes) -> bytes:
    constants = b"expand 32-byte k"
    state = list(struct.unpack("<4I", constants))
    state.extend(struct.unpack("<8I", key))
    state.append(counter & 0xffffffff)
    state.extend(struct.unpack("<3I", nonce))
    working = state[:]
    for _ in range(10):
        _quarter_round(working, 0, 4, 8, 12)
        _quarter_round(working, 1, 5, 9, 13)
        _quarter_round(working, 2, 6, 10, 14)
        _quarter_round(working, 3, 7, 11, 15)
        _quarter_round(working, 0, 5, 10, 15)
        _quarter_round(working, 1, 6, 11, 12)
        _quarter_round(working, 2, 7, 8, 13)
        _quarter_round(working, 3, 4, 9, 14)
    output = [(working[i] + state[i]) & 0xffffffff for i in range(16)]
    return struct.pack("<16I", *output)


def _chacha20_xor(data: bytes, nonce: bytes) -> bytes:
    result = bytearray()
    counter = 1
    for offset in range(0, len(data), 64):
        block = _chacha20_block(_ENC_KEY, counter, nonce)
        chunk = data[offset:offset + 64]
        result.extend(bytes([chunk[i] ^ block[i] for i in range(len(chunk))]))
        counter += 1
    return bytes(result)


def encrypt_message(message: Dict[str, Any]) -> str:
    nonce = os.urandom(12)
    plaintext = json.dumps(message, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    ciphertext = _chacha20_xor(plaintext, nonce)
    mac = hmac.new(_MAC_KEY, nonce + ciphertext, hashlib.sha256).digest()
    envelope = {
        "type": "secure",
        "alg": "CHACHA20-HMAC-SHA256",
        "nonce": b64url_encode(nonce),
        "ciphertext": b64url_encode(ciphertext),
        "mac": b64url_encode(mac),
    }
    return json.dumps(envelope, separators=(",", ":"))


def decrypt_message(raw_message: str) -> Dict[str, Any]:
    try:
        envelope = json.loads(raw_message)
        if envelope.get("type") != "secure":
            raise ExternalTradingCryptoError("message is not encrypted")
        nonce = b64url_decode(envelope.get("nonce", ""))
        ciphertext = b64url_decode(envelope.get("ciphertext", ""))
        mac = b64url_decode(envelope.get("mac", ""))
    except ExternalTradingCryptoError:
        raise
    except Exception as exc:
        raise ExternalTradingCryptoError("invalid encrypted envelope") from exc

    expected_mac = hmac.new(_MAC_KEY, nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected_mac):
        raise ExternalTradingCryptoError("message authentication failed")

    try:
        plaintext = _chacha20_xor(ciphertext, nonce)
        return json.loads(plaintext.decode("utf-8"))
    except Exception as exc:
        raise ExternalTradingCryptoError("message decryption failed") from exc

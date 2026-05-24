from typing import Any, Dict, List, Optional


ALLOWED_EXECUTOR_PRICE_LEVELS = {-1, 0, 1, 2, 3, 4, 5}
DEFAULT_EXECUTOR_PRICE_LEVEL = 1
DEFAULT_EXECUTOR_LOT_SIZE = 100
DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS = 120
MAX_EXECUTOR_ORDER_TIMEOUT_SECONDS = 86400
DEFAULT_EXECUTOR_MAX_REPLACE_COUNT = 3
DEFAULT_EXECUTOR_MAX_SLIPPAGE_PCT = 0.5
DEFAULT_EXECUTOR_PRICE_LEVEL_SEQUENCE = [1, 2, 3, 5, -1]
DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS_SEQUENCE = [
    DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS
] * len(DEFAULT_EXECUTOR_PRICE_LEVEL_SEQUENCE)
DEFAULT_EXECUTOR_CLIP_SELL_TO_AVAILABLE = True


def safe_int_or_none(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except Exception:
        return None


def normalize_bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return bool(default)


def normalize_price_level(value: Any, default: int = DEFAULT_EXECUTOR_PRICE_LEVEL) -> int:
    parsed = safe_int_or_none(value)
    if parsed in ALLOWED_EXECUTOR_PRICE_LEVELS:
        return int(parsed)
    return default


def normalize_lot_size(value: Any, default: int = DEFAULT_EXECUTOR_LOT_SIZE) -> int:
    parsed = safe_int_or_none(value)
    if parsed and parsed > 0:
        return parsed
    return default


def normalize_timeout_seconds(value: Any, default: int = DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS) -> int:
    parsed = safe_int_or_none(value)
    if parsed and parsed >= 10:
        return parsed
    return default


def normalize_max_replace_count(value: Any, default: int = DEFAULT_EXECUTOR_MAX_REPLACE_COUNT) -> int:
    parsed = safe_int_or_none(value)
    if parsed is not None and parsed >= 0:
        return parsed
    return default


def normalize_max_slippage_pct(value: Any, default: float = DEFAULT_EXECUTOR_MAX_SLIPPAGE_PCT) -> float:
    try:
        if value is None or value == "":
            return float(default)
        parsed = float(value)
        if parsed >= 0:
            return parsed
    except Exception:
        pass
    return float(default)


def normalize_clip_sell_to_available(
    value: Any,
    default: bool = DEFAULT_EXECUTOR_CLIP_SELL_TO_AVAILABLE,
) -> bool:
    return True


def normalize_price_level_sequence(value: Any, default: Optional[List[int]] = None) -> List[int]:
    fallback = list(default or DEFAULT_EXECUTOR_PRICE_LEVEL_SEQUENCE)
    raw_items = value
    if raw_items is None or raw_items == "":
        raw_items = fallback
    if isinstance(raw_items, str):
        raw_items = [part.strip() for part in raw_items.split(",") if part.strip()]
    if not isinstance(raw_items, (list, tuple)):
        raw_items = fallback

    sequence: List[int] = []
    for item in raw_items:
        parsed = safe_int_or_none(item)
        if parsed in ALLOWED_EXECUTOR_PRICE_LEVELS:
            sequence.append(int(parsed))
    return sequence or fallback


def normalize_timeout_seconds_sequence(value: Any, default: Optional[List[int]] = None) -> List[int]:
    fallback = list(default or DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS_SEQUENCE)
    raw_items = value
    if raw_items is None or raw_items == "":
        raw_items = fallback
    if isinstance(raw_items, str):
        raw_items = [part.strip() for part in raw_items.split(",") if part.strip()]
    if not isinstance(raw_items, (list, tuple)):
        raw_items = fallback

    sequence: List[int] = []
    for item in raw_items:
        parsed = safe_int_or_none(item)
        if parsed is not None and 10 <= parsed <= MAX_EXECUTOR_ORDER_TIMEOUT_SECONDS:
            sequence.append(int(parsed))
    return sequence or fallback


def parse_price_level_sequence_text(value: Any) -> Optional[List[int]]:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return normalize_price_level_sequence(value, default=[])


def price_level_aggressiveness_rank(value: Any) -> int:
    level = normalize_price_level(value)
    if level == -1:
        return 6
    if level == 0:
        return 0
    return level


def most_aggressive_price_level(levels: List[Any], default: int = DEFAULT_EXECUTOR_PRICE_LEVEL) -> int:
    normalized = [normalize_price_level(level, default) for level in levels if level is not None]
    if not normalized:
        return default
    return max(normalized, key=price_level_aggressiveness_rank)


def resolve_execution_policy(account: Any, sub_account: Any = None, fallback: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    fallback = fallback or {}
    account_sequence = normalize_price_level_sequence(
        getattr(account, "executor_price_level_sequence", None),
        default=fallback.get("price_level_sequence") or DEFAULT_EXECUTOR_PRICE_LEVEL_SEQUENCE,
    )
    account_timeout_seconds = normalize_timeout_seconds(
        getattr(account, "executor_order_timeout_seconds", None),
        normalize_timeout_seconds(fallback.get("order_timeout_seconds"), DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS),
    )
    account_timeout_sequence = normalize_timeout_seconds_sequence(
        getattr(account, "executor_order_timeout_seconds_sequence", None),
        default=fallback.get("order_timeout_seconds_sequence") or [
            account_timeout_seconds
        ] * len(DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS_SEQUENCE),
    )
    policy = {
        "price_level": account_sequence[0],
        "lot_size": normalize_lot_size(
            getattr(account, "executor_lot_size", None),
            normalize_lot_size(fallback.get("lot_size"), DEFAULT_EXECUTOR_LOT_SIZE),
        ),
        "order_timeout_seconds": account_timeout_sequence[0],
        "max_replace_count": normalize_max_replace_count(
            getattr(account, "executor_max_replace_count", None),
            normalize_max_replace_count(fallback.get("max_replace_count"), DEFAULT_EXECUTOR_MAX_REPLACE_COUNT),
        ),
        "max_slippage_pct": normalize_max_slippage_pct(
            getattr(account, "executor_max_slippage_pct", None),
            normalize_max_slippage_pct(fallback.get("max_slippage_pct"), DEFAULT_EXECUTOR_MAX_SLIPPAGE_PCT),
        ),
        "clip_sell_to_available": True,
        "price_level_sequence": account_sequence,
        "order_timeout_seconds_sequence": account_timeout_sequence,
        "source": "account",
    }

    if sub_account is not None:
        sub_sequence = getattr(sub_account, "executor_price_level_sequence", None)
        if sub_sequence:
            policy["price_level_sequence"] = normalize_price_level_sequence(sub_sequence, default=account_sequence)
            policy["price_level"] = policy["price_level_sequence"][0]
            policy["source"] = "sub_account"
        sub_timeout_sequence = getattr(sub_account, "executor_order_timeout_seconds_sequence", None)
        if sub_timeout_sequence:
            policy["order_timeout_seconds_sequence"] = normalize_timeout_seconds_sequence(
                sub_timeout_sequence,
                default=account_timeout_sequence,
            )
            policy["order_timeout_seconds"] = policy["order_timeout_seconds_sequence"][0]
            policy["source"] = "sub_account"
        for field, normalizer in (
            ("lot_size", normalize_lot_size),
            ("max_replace_count", normalize_max_replace_count),
            ("max_slippage_pct", normalize_max_slippage_pct),
        ):
            attr = f"executor_{field}"
            value = getattr(sub_account, attr, None)
            if value is not None:
                policy[field] = normalizer(value, policy[field])
                policy["source"] = "sub_account"
        sub_timeout_seconds = getattr(sub_account, "executor_order_timeout_seconds", None)
        if sub_timeout_seconds is not None and not sub_timeout_sequence:
            policy["order_timeout_seconds"] = normalize_timeout_seconds(
                sub_timeout_seconds,
                policy["order_timeout_seconds"],
            )
            policy["order_timeout_seconds_sequence"] = [
                policy["order_timeout_seconds"]
            ] * len(DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS_SEQUENCE)
            policy["source"] = "sub_account"
    return policy


def aggregate_execution_policy(policies: List[Dict[str, Any]], fallback: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    fallback = fallback or {}
    if not policies:
        sequence = normalize_price_level_sequence(fallback.get("price_level_sequence"))
        timeout_sequence = normalize_timeout_seconds_sequence(
            fallback.get("order_timeout_seconds_sequence"),
            default=[
                normalize_timeout_seconds(
                    fallback.get("order_timeout_seconds"),
                    DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS,
                )
            ] * len(DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS_SEQUENCE),
        )
        return {
            "price_level": sequence[0],
            "lot_size": normalize_lot_size(fallback.get("lot_size"), DEFAULT_EXECUTOR_LOT_SIZE),
            "order_timeout_seconds": timeout_sequence[0],
            "max_replace_count": normalize_max_replace_count(
                fallback.get("max_replace_count"),
                DEFAULT_EXECUTOR_MAX_REPLACE_COUNT,
            ),
            "max_slippage_pct": normalize_max_slippage_pct(
                fallback.get("max_slippage_pct"),
                DEFAULT_EXECUTOR_MAX_SLIPPAGE_PCT,
            ),
            "clip_sell_to_available": True,
            "price_level_sequence": sequence,
            "order_timeout_seconds_sequence": timeout_sequence,
            "source": "fallback",
        }

    base_sequence = normalize_price_level_sequence(policies[0].get("price_level_sequence"))
    timeout_sequences = [
        normalize_timeout_seconds_sequence(
            item.get("order_timeout_seconds_sequence"),
            default=[
                normalize_timeout_seconds(item.get("order_timeout_seconds"), DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS)
            ] * len(DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS_SEQUENCE),
        )
        for item in policies
    ]
    max_timeout_sequence_length = max(len(item) for item in timeout_sequences)
    timeout_sequence = [
        min(item[min(index, len(item) - 1)] for item in timeout_sequences)
        for index in range(max_timeout_sequence_length)
    ]
    return {
        "price_level": base_sequence[0],
        "lot_size": max(normalize_lot_size(item.get("lot_size")) for item in policies),
        "order_timeout_seconds": timeout_sequence[0],
        "max_replace_count": min(normalize_max_replace_count(item.get("max_replace_count")) for item in policies),
        "max_slippage_pct": min(normalize_max_slippage_pct(item.get("max_slippage_pct")) for item in policies),
        "clip_sell_to_available": True,
        "price_level_sequence": normalize_price_level_sequence(base_sequence),
        "order_timeout_seconds_sequence": timeout_sequence,
        "source": "aggregated",
    }

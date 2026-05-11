from typing import Any, Dict, List, Optional


ALLOWED_EXECUTOR_PRICE_LEVELS = {-1, 0, 1, 2, 3, 4, 5}
DEFAULT_EXECUTOR_PRICE_LEVEL = 1
DEFAULT_EXECUTOR_LOT_SIZE = 100
DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS = 120
DEFAULT_EXECUTOR_MAX_REPLACE_COUNT = 3
DEFAULT_EXECUTOR_PRICE_LEVEL_SEQUENCE = [1, 2, 3, 5, -1]


def safe_int_or_none(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except Exception:
        return None


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
        if parsed in ALLOWED_EXECUTOR_PRICE_LEVELS and parsed not in sequence:
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
    policy = {
        "price_level": normalize_price_level(
            getattr(account, "executor_price_level", None),
            normalize_price_level(fallback.get("price_level"), DEFAULT_EXECUTOR_PRICE_LEVEL),
        ),
        "lot_size": normalize_lot_size(
            getattr(account, "executor_lot_size", None),
            normalize_lot_size(fallback.get("lot_size"), DEFAULT_EXECUTOR_LOT_SIZE),
        ),
        "order_timeout_seconds": normalize_timeout_seconds(
            getattr(account, "executor_order_timeout_seconds", None),
            normalize_timeout_seconds(fallback.get("order_timeout_seconds"), DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS),
        ),
        "max_replace_count": normalize_max_replace_count(
            getattr(account, "executor_max_replace_count", None),
            normalize_max_replace_count(fallback.get("max_replace_count"), DEFAULT_EXECUTOR_MAX_REPLACE_COUNT),
        ),
        "price_level_sequence": account_sequence,
        "source": "account",
    }

    if sub_account is not None:
        sub_sequence = getattr(sub_account, "executor_price_level_sequence", None)
        if sub_sequence:
            policy["price_level_sequence"] = normalize_price_level_sequence(sub_sequence, default=account_sequence)
            policy["source"] = "sub_account"
        for field, normalizer in (
            ("price_level", normalize_price_level),
            ("lot_size", normalize_lot_size),
            ("order_timeout_seconds", normalize_timeout_seconds),
            ("max_replace_count", normalize_max_replace_count),
        ):
            attr = f"executor_{field}"
            value = getattr(sub_account, attr, None)
            if value is not None:
                policy[field] = normalizer(value, policy[field])
                policy["source"] = "sub_account"
    return policy


def aggregate_execution_policy(policies: List[Dict[str, Any]], fallback: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    fallback = fallback or {}
    if not policies:
        return {
            "price_level": normalize_price_level(fallback.get("price_level"), DEFAULT_EXECUTOR_PRICE_LEVEL),
            "lot_size": normalize_lot_size(fallback.get("lot_size"), DEFAULT_EXECUTOR_LOT_SIZE),
            "order_timeout_seconds": normalize_timeout_seconds(
                fallback.get("order_timeout_seconds"),
                DEFAULT_EXECUTOR_ORDER_TIMEOUT_SECONDS,
            ),
            "max_replace_count": normalize_max_replace_count(
                fallback.get("max_replace_count"),
                DEFAULT_EXECUTOR_MAX_REPLACE_COUNT,
            ),
            "price_level_sequence": normalize_price_level_sequence(fallback.get("price_level_sequence")),
            "source": "fallback",
        }

    price_level = most_aggressive_price_level([item.get("price_level") for item in policies])
    base_sequence = normalize_price_level_sequence(policies[0].get("price_level_sequence"))
    if price_level not in base_sequence:
        base_sequence = [price_level] + base_sequence
    return {
        "price_level": price_level,
        "lot_size": max(normalize_lot_size(item.get("lot_size")) for item in policies),
        "order_timeout_seconds": min(normalize_timeout_seconds(item.get("order_timeout_seconds")) for item in policies),
        "max_replace_count": min(normalize_max_replace_count(item.get("max_replace_count")) for item in policies),
        "price_level_sequence": normalize_price_level_sequence(base_sequence),
        "source": "aggregated",
    }

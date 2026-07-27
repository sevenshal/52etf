from collections import Counter
from typing import Any, Dict


def summarize_external_trading_executor_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Return an allowlisted summary safe for production logs."""
    accounts = result.get("accounts") or []
    summary = {
        key: result.get(key)
        for key in ("status", "trigger_source", "checked", "processed", "skipped", "failed")
    }
    status_counts = Counter(str(item.get("status") or "UNKNOWN") for item in accounts)
    reason_counts = Counter(str(item.get("reason")) for item in accounts if item.get("reason"))
    if status_counts:
        summary["account_status_counts"] = dict(sorted(status_counts.items()))
    if reason_counts:
        summary["reason_counts"] = dict(sorted(reason_counts.items()))
    return summary


def summarize_external_trading_net_asset_snapshot(result: Dict[str, Any]) -> Dict[str, Any]:
    """Return snapshot health fields without account names or financial values."""
    return {
        key: result.get(key)
        for key in ("status", "trading_date", "checked", "recorded", "failed")
    }

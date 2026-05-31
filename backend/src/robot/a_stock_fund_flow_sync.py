import logging
import time
from datetime import date, datetime
from typing import Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from ..core.analytics_database import ANALYTICS_DB_PATH, ensure_analytics_schema
from ..core.duckdb_utils import connect_duckdb
from ..core.services.a_stock_fund_flow import (
    fetch_market_rank_all,
    fetch_stock_fund_flow_daily,
)


logger = logging.getLogger(__name__)

TABLE_NAME = "a_stock_fund_flow_daily"
SOURCE = "eastmoney_push2"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_HISTORY_DAYS = 120
DEFAULT_BATCH_SIZE = 200
DEFAULT_REQUEST_INTERVAL_SECONDS = 0.03

FUND_FLOW_COLUMNS = [
    "trade_date",
    "ts_code",
    "symbol",
    "name",
    "close",
    "pct_chg",
    "main_net",
    "main_net_pct",
    "super_net",
    "super_net_pct",
    "large_net",
    "large_net_pct",
    "mid_net",
    "mid_net_pct",
    "small_net",
    "small_net_pct",
    "source",
    "source_updated_at",
    "created_at",
    "updated_at",
]


def normalize_a_stock_ts_code(code: str) -> str:
    raw = str(code or "").strip().upper()
    if "." in raw:
        raw = raw.split(".", 1)[0]
    if raw.startswith(("SH", "SZ", "BJ")):
        raw = raw[2:]
    if raw.startswith(("6", "9")):
        return f"{raw}.SH"
    if raw.startswith("8"):
        return f"{raw}.BJ"
    return f"{raw}.SZ"


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _today_shanghai() -> date:
    return datetime.now(SHANGHAI_TZ).date()


def _quote(identifier: str) -> str:
    return f'"{identifier.replace(chr(34), chr(34) * 2)}"'


def _chunks(items: List, size: int) -> Iterable[List]:
    for index in range(0, len(items), size):
        yield items[index:index + size]


def _table_row_count() -> int:
    ensure_analytics_schema()
    connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=False)
    try:
        result = connection.execute(f"SELECT COUNT(*) FROM {_quote(TABLE_NAME)}").fetchone()
        return int(result[0] or 0)
    finally:
        connection.close()


def _latest_trade_date() -> Optional[date]:
    ensure_analytics_schema()
    connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=False)
    try:
        result = connection.execute(f"SELECT MAX(trade_date) FROM {_quote(TABLE_NAME)}").fetchone()
        return result[0] if result and result[0] else None
    finally:
        connection.close()


def _insert_or_replace_rows(rows: List[Dict]) -> int:
    if not rows:
        return 0
    ensure_analytics_schema()
    frame = pd.DataFrame(rows)
    frame = frame.loc[:, FUND_FLOW_COLUMNS]
    connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=False)
    temp_name = "a_stock_fund_flow_insert_frame"
    quoted_columns = ", ".join(_quote(column) for column in FUND_FLOW_COLUMNS)
    try:
        connection.register(temp_name, frame)
        connection.execute(
            (
                f"INSERT OR REPLACE INTO {_quote(TABLE_NAME)} ({quoted_columns}) "
                f"SELECT {quoted_columns} FROM {_quote(temp_name)}"
            )
        )
        return len(frame)
    finally:
        connection.close()


def _rank_item_trade_date(item: Dict) -> date:
    parsed = _parse_iso_datetime(item.get("updated_at"))
    return parsed.date() if parsed else _today_shanghai()


def rank_item_to_daily_record(item: Dict, *, now: Optional[datetime] = None) -> Dict:
    saved_at = now or datetime.now(SHANGHAI_TZ).replace(tzinfo=None)
    source_updated_at = _parse_iso_datetime(item.get("updated_at"))
    code = str(item.get("code") or "").strip()
    return {
        "trade_date": _rank_item_trade_date(item),
        "ts_code": normalize_a_stock_ts_code(code),
        "symbol": code,
        "name": item.get("name") or "",
        "close": item.get("price"),
        "pct_chg": item.get("change_pct"),
        "main_net": item.get("main_net"),
        "main_net_pct": item.get("main_net_pct"),
        "super_net": item.get("super_net"),
        "super_net_pct": item.get("super_net_pct"),
        "large_net": item.get("large_net"),
        "large_net_pct": item.get("large_net_pct"),
        "mid_net": item.get("mid_net"),
        "mid_net_pct": item.get("mid_net_pct"),
        "small_net": item.get("small_net"),
        "small_net_pct": item.get("small_net_pct"),
        "source": SOURCE,
        "source_updated_at": source_updated_at,
        "created_at": saved_at,
        "updated_at": saved_at,
    }


def daily_flow_to_record(code: str, name: str, row: Dict, *, now: Optional[datetime] = None) -> Dict:
    saved_at = now or datetime.now(SHANGHAI_TZ).replace(tzinfo=None)
    trade_date = _parse_date(row.get("date"))
    if not trade_date:
        raise ValueError(f"Invalid fund flow date for {code}: {row.get('date')}")
    return {
        "trade_date": trade_date,
        "ts_code": normalize_a_stock_ts_code(code),
        "symbol": str(code),
        "name": name or "",
        "close": row.get("close"),
        "pct_chg": row.get("change_pct"),
        "main_net": row.get("main_net"),
        "main_net_pct": row.get("main_net_pct"),
        "super_net": row.get("super_net"),
        "super_net_pct": row.get("super_net_pct"),
        "large_net": row.get("large_net"),
        "large_net_pct": row.get("large_net_pct"),
        "mid_net": row.get("mid_net"),
        "mid_net_pct": row.get("mid_net_pct"),
        "small_net": row.get("small_net"),
        "small_net_pct": row.get("small_net_pct"),
        "source": SOURCE,
        "source_updated_at": None,
        "created_at": saved_at,
        "updated_at": saved_at,
    }


def sync_current_a_stock_fund_flow() -> Dict:
    snapshot = fetch_market_rank_all(direction="inflow")
    items = snapshot.get("items") or []
    now = datetime.now(SHANGHAI_TZ).replace(tzinfo=None)
    rows = [rank_item_to_daily_record(item, now=now) for item in items if item.get("code")]
    saved = 0
    for batch in _chunks(rows, DEFAULT_BATCH_SIZE):
        saved += _insert_or_replace_rows(batch)
    trade_dates = sorted({row["trade_date"] for row in rows})
    return {
        "mode": "incremental",
        "total": snapshot.get("total") or len(items),
        "fetched_symbols": len(items),
        "saved_rows": saved,
        "trade_dates": [item.isoformat() for item in trade_dates],
        "latest_trade_date": trade_dates[-1].isoformat() if trade_dates else None,
    }


def backfill_recent_a_stock_fund_flow(
    *,
    start_date: Optional[date] = None,
    history_days: int = DEFAULT_HISTORY_DAYS,
    request_interval_seconds: float = DEFAULT_REQUEST_INTERVAL_SECONDS,
) -> Dict:
    snapshot = fetch_market_rank_all(direction="inflow")
    symbols = [
        {"code": item.get("code"), "name": item.get("name") or ""}
        for item in (snapshot.get("items") or [])
        if item.get("code")
    ]
    errors: List[Dict] = []
    saved = 0
    fetched_rows = 0
    min_trade_date: Optional[date] = None
    max_trade_date: Optional[date] = None
    batch_rows: List[Dict] = []
    now = datetime.now(SHANGHAI_TZ).replace(tzinfo=None)
    safe_history_days = min(max(int(history_days), 1), DEFAULT_HISTORY_DAYS)

    for index, item in enumerate(symbols, start=1):
        code = item["code"]
        try:
            payload = fetch_stock_fund_flow_daily(code, daily_limit=safe_history_days)
            name = payload.get("name") or item.get("name") or ""
            for row in payload.get("daily") or []:
                record = daily_flow_to_record(code, name, row, now=now)
                if start_date and record["trade_date"] < start_date:
                    continue
                batch_rows.append(record)
                fetched_rows += 1
                min_trade_date = record["trade_date"] if min_trade_date is None else min(min_trade_date, record["trade_date"])
                max_trade_date = record["trade_date"] if max_trade_date is None else max(max_trade_date, record["trade_date"])
            if len(batch_rows) >= DEFAULT_BATCH_SIZE * safe_history_days:
                saved += _insert_or_replace_rows(batch_rows)
                batch_rows = []
        except Exception as exc:
            errors.append({"code": code, "error": str(exc)})
            logger.warning("A stock fund flow backfill failed for %s: %s", code, exc)
        if request_interval_seconds > 0 and index < len(symbols):
            time.sleep(request_interval_seconds)

    if batch_rows:
        saved += _insert_or_replace_rows(batch_rows)

    return {
        "mode": "full",
        "symbols": len(symbols),
        "fetched_rows": fetched_rows,
        "saved_rows": saved,
        "start_date": min_trade_date.isoformat() if min_trade_date else None,
        "end_date": max_trade_date.isoformat() if max_trade_date else None,
        "errors": errors,
    }


def sync_a_stock_fund_flow(start_date: Optional[date] = None, full: Optional[bool] = None) -> Dict:
    existing_rows = _table_row_count()
    previous_latest_trade_date = _latest_trade_date()
    should_full = bool(full) or start_date is not None or existing_rows == 0
    if should_full:
        result = backfill_recent_a_stock_fund_flow(start_date=start_date)
        result["existing_rows_before"] = existing_rows
        result["previous_latest_trade_date"] = (
            previous_latest_trade_date.isoformat() if previous_latest_trade_date else None
        )
        return result

    result = sync_current_a_stock_fund_flow()
    result["existing_rows_before"] = existing_rows
    result["previous_latest_trade_date"] = (
        previous_latest_trade_date.isoformat() if previous_latest_trade_date else None
    )
    return result

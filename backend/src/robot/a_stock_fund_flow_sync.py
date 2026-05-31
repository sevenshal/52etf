import logging
import time
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

from ..core.analytics_database import ANALYTICS_DB_PATH, ensure_analytics_schema
from ..core.duckdb_utils import connect_duckdb
from ..core.services.a_stock_fund_flow import (
    fetch_market_rank_all,
    fetch_stock_fund_flow_daily,
)
from ..core.services.tushare import TushareService


logger = logging.getLogger(__name__)

TABLE_NAME = "a_stock_fund_flow_daily"
SOURCE_EASTMONEY_PUSH2 = "eastmoney_push2"
SOURCE_TUSHARE_MONEYFLOW_DC = "tushare_moneyflow_dc"
SOURCE_TUSHARE_MONEYFLOW = "tushare_moneyflow"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
TUSHARE_MONEYFLOW_DC_START_DATE = date(2023, 9, 11)
DEFAULT_HISTORY_DAYS = 120
DEFAULT_BATCH_SIZE = 200
DEFAULT_BACKFILL_BATCH_ROWS = 50000
DEFAULT_REQUEST_INTERVAL_SECONDS = 0.03
DEFAULT_TUSHARE_REQUEST_INTERVAL_SECONDS = 0.2
AMOUNT_WAN_TO_YUAN = 10000.0

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


def _parse_tushare_trade_date(value) -> Optional[date]:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def _today_shanghai() -> date:
    return datetime.now(SHANGHAI_TZ).date()


def _quote(identifier: str) -> str:
    return f'"{identifier.replace(chr(34), chr(34) * 2)}"'


def _chunks(items: List, size: int) -> Iterable[List]:
    for index in range(0, len(items), size):
        yield items[index:index + size]


def _safe_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    if isinstance(value, str):
        text = value.strip()
        if not text or text in {"-", "None", "nan", "NaN"}:
            return None
        value = text.replace(",", "")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _amount_wan_to_yuan(value) -> Optional[float]:
    number = _safe_float(value)
    return number * AMOUNT_WAN_TO_YUAN if number is not None else None


def _net_amount_wan_to_yuan(buy, sell) -> Optional[float]:
    buy_number = _safe_float(buy)
    sell_number = _safe_float(sell)
    if buy_number is None and sell_number is None:
        return None
    return ((buy_number or 0.0) - (sell_number or 0.0)) * AMOUNT_WAN_TO_YUAN


def _sum_optional(*values) -> Optional[float]:
    present = [float(value) for value in values if value is not None]
    return sum(present) if present else None


def _symbol_from_ts_code(ts_code: str) -> str:
    return str(ts_code or "").split(".", 1)[0].strip()


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
        "source": SOURCE_EASTMONEY_PUSH2,
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
        "source": SOURCE_EASTMONEY_PUSH2,
        "source_updated_at": None,
        "created_at": saved_at,
        "updated_at": saved_at,
    }


def tushare_moneyflow_dc_row_to_record(row, *, now: Optional[datetime] = None) -> Dict:
    item = row.to_dict() if hasattr(row, "to_dict") else dict(row)
    saved_at = now or datetime.now(SHANGHAI_TZ).replace(tzinfo=None)
    trade_date = _parse_tushare_trade_date(item.get("trade_date"))
    ts_code = normalize_a_stock_ts_code(item.get("ts_code") or "")
    if not trade_date:
        raise ValueError(f"Invalid Tushare moneyflow_dc trade_date: {item.get('trade_date')}")
    if not ts_code:
        raise ValueError(f"Invalid Tushare moneyflow_dc ts_code: {item.get('ts_code')}")
    return {
        "trade_date": trade_date,
        "ts_code": ts_code,
        "symbol": _symbol_from_ts_code(ts_code),
        "name": item.get("name") or "",
        "close": _safe_float(item.get("close")),
        "pct_chg": _safe_float(item.get("pct_change")),
        "main_net": _amount_wan_to_yuan(item.get("net_amount")),
        "main_net_pct": _safe_float(item.get("net_amount_rate")),
        "super_net": _amount_wan_to_yuan(item.get("buy_elg_amount")),
        "super_net_pct": _safe_float(item.get("buy_elg_amount_rate")),
        "large_net": _amount_wan_to_yuan(item.get("buy_lg_amount")),
        "large_net_pct": _safe_float(item.get("buy_lg_amount_rate")),
        "mid_net": _amount_wan_to_yuan(item.get("buy_md_amount")),
        "mid_net_pct": _safe_float(item.get("buy_md_amount_rate")),
        "small_net": _amount_wan_to_yuan(item.get("buy_sm_amount")),
        "small_net_pct": _safe_float(item.get("buy_sm_amount_rate")),
        "source": SOURCE_TUSHARE_MONEYFLOW_DC,
        "source_updated_at": None,
        "created_at": saved_at,
        "updated_at": saved_at,
    }


def tushare_moneyflow_dc_frame_to_records(frame: pd.DataFrame, *, now: Optional[datetime] = None) -> List[Dict]:
    saved_at = now or datetime.now(SHANGHAI_TZ).replace(tzinfo=None)
    return [
        tushare_moneyflow_dc_row_to_record(row, now=saved_at)
        for _, row in frame.iterrows()
        if row.get("ts_code")
    ]


def tushare_moneyflow_row_to_record(
    row,
    *,
    now: Optional[datetime] = None,
    name_map: Optional[Dict[str, str]] = None,
) -> Dict:
    item = row.to_dict() if hasattr(row, "to_dict") else dict(row)
    saved_at = now or datetime.now(SHANGHAI_TZ).replace(tzinfo=None)
    trade_date = _parse_tushare_trade_date(item.get("trade_date"))
    ts_code = normalize_a_stock_ts_code(item.get("ts_code") or "")
    if not trade_date:
        raise ValueError(f"Invalid Tushare moneyflow trade_date: {item.get('trade_date')}")
    if not ts_code:
        raise ValueError(f"Invalid Tushare moneyflow ts_code: {item.get('ts_code')}")
    super_net = _net_amount_wan_to_yuan(item.get("buy_elg_amount"), item.get("sell_elg_amount"))
    large_net = _net_amount_wan_to_yuan(item.get("buy_lg_amount"), item.get("sell_lg_amount"))
    mid_net = _net_amount_wan_to_yuan(item.get("buy_md_amount"), item.get("sell_md_amount"))
    small_net = _net_amount_wan_to_yuan(item.get("buy_sm_amount"), item.get("sell_sm_amount"))
    return {
        "trade_date": trade_date,
        "ts_code": ts_code,
        "symbol": _symbol_from_ts_code(ts_code),
        "name": (name_map or {}).get(ts_code, ""),
        "close": None,
        "pct_chg": None,
        "main_net": _sum_optional(super_net, large_net),
        "main_net_pct": None,
        "super_net": super_net,
        "super_net_pct": None,
        "large_net": large_net,
        "large_net_pct": None,
        "mid_net": mid_net,
        "mid_net_pct": None,
        "small_net": small_net,
        "small_net_pct": None,
        "source": SOURCE_TUSHARE_MONEYFLOW,
        "source_updated_at": None,
        "created_at": saved_at,
        "updated_at": saved_at,
    }


def tushare_moneyflow_frame_to_records(
    frame: pd.DataFrame,
    *,
    now: Optional[datetime] = None,
    name_map: Optional[Dict[str, str]] = None,
) -> List[Dict]:
    saved_at = now or datetime.now(SHANGHAI_TZ).replace(tzinfo=None)
    return [
        tushare_moneyflow_row_to_record(row, now=saved_at, name_map=name_map)
        for _, row in frame.iterrows()
        if row.get("ts_code")
    ]


def _local_stock_name_map() -> Dict[str, str]:
    ensure_analytics_schema()
    connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
    try:
        rows = connection.execute("SELECT ts_code, name FROM a_stock_basic").fetchall()
    except Exception as exc:
        logger.warning("Local A stock names unavailable: %s", exc)
        return {}
    finally:
        connection.close()
    return {str(ts_code): (name or "") for ts_code, name in rows}


def _local_open_trade_dates(start_date: date, end_date: date, *, limit: Optional[int] = None) -> List[date]:
    if start_date > end_date:
        return []
    ensure_analytics_schema()
    connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
    try:
        if limit:
            rows = connection.execute(
                """
                SELECT DISTINCT trade_date
                FROM a_stock_market_daily_qfq
                WHERE trade_date BETWEEN ? AND ?
                ORDER BY trade_date DESC
                LIMIT ?
                """,
                [start_date, end_date, int(limit)],
            ).fetchall()
            return sorted(row[0] for row in rows if row and row[0])
        rows = connection.execute(
            """
            SELECT DISTINCT trade_date
            FROM a_stock_market_daily_qfq
            WHERE trade_date BETWEEN ? AND ?
            ORDER BY trade_date
            """,
            [start_date, end_date],
        ).fetchall()
        return [row[0] for row in rows if row and row[0]]
    except Exception as exc:
        logger.warning("Local A stock trade dates unavailable: %s", exc)
        return []
    finally:
        connection.close()


def _tushare_open_trade_dates(
    start_date: date,
    end_date: date,
    *,
    tushare_service: Optional[TushareService] = None,
) -> List[date]:
    service = tushare_service or TushareService.getInstance()
    calendar = service.get_trade_calendar_frame(start_date, end_date)
    if calendar is None or calendar.empty:
        return []
    frame = calendar.copy()
    frame = frame[frame["is_open"].astype(str) == "1"]
    dates = [_parse_tushare_trade_date(item) for item in frame["cal_date"].tolist()]
    return sorted(item for item in dates if item)


def resolve_a_stock_fund_flow_trade_dates(
    *,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    history_days: int = DEFAULT_HISTORY_DAYS,
    tushare_service: Optional[TushareService] = None,
) -> List[date]:
    end_value = end_date or _today_shanghai()
    if start_date:
        dates = _tushare_open_trade_dates(start_date, end_value, tushare_service=tushare_service)
        if dates:
            return dates
        return _local_open_trade_dates(start_date, end_value)

    safe_history_days = max(int(history_days), 1)
    lookback_days = max(safe_history_days * 2 + 60, 260)
    calendar_start = end_value - timedelta(days=lookback_days)
    dates = _tushare_open_trade_dates(calendar_start, end_value, tushare_service=tushare_service)
    if len(dates) >= safe_history_days:
        return dates[-safe_history_days:]
    local_dates = _local_open_trade_dates(calendar_start, end_value, limit=safe_history_days)
    if local_dates:
        return local_dates[-safe_history_days:]
    return dates


def _fetch_tushare_moneyflow_dc_frame(
    trade_date: date,
    *,
    tushare_service: Optional[TushareService] = None,
    max_attempts: int = 3,
) -> pd.DataFrame:
    service = tushare_service or TushareService.getInstance()
    trade_date_text = trade_date.strftime("%Y%m%d")
    last_error: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            frame = service.pro.moneyflow_dc(trade_date=trade_date_text)
            if frame is None:
                return pd.DataFrame()
            return frame if isinstance(frame, pd.DataFrame) else pd.DataFrame(frame)
        except Exception as exc:
            last_error = exc
            if attempt < max_attempts:
                time.sleep(min(attempt * 2, 10))
    raise RuntimeError(f"Tushare moneyflow_dc failed for {trade_date_text}: {last_error}")


def _fetch_tushare_moneyflow_frame(
    trade_date: date,
    *,
    tushare_service: Optional[TushareService] = None,
    max_attempts: int = 3,
) -> pd.DataFrame:
    service = tushare_service or TushareService.getInstance()
    trade_date_text = trade_date.strftime("%Y%m%d")
    last_error: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            frame = service.pro.moneyflow(trade_date=trade_date_text)
            if frame is None:
                return pd.DataFrame()
            return frame if isinstance(frame, pd.DataFrame) else pd.DataFrame(frame)
        except Exception as exc:
            last_error = exc
            if attempt < max_attempts:
                time.sleep(min(attempt * 2, 10))
    raise RuntimeError(f"Tushare moneyflow failed for {trade_date_text}: {last_error}")


def _fetch_tushare_best_moneyflow_frame(
    trade_date: date,
    *,
    tushare_service: Optional[TushareService] = None,
) -> Tuple[pd.DataFrame, str]:
    if trade_date >= TUSHARE_MONEYFLOW_DC_START_DATE:
        frame = _fetch_tushare_moneyflow_dc_frame(trade_date, tushare_service=tushare_service)
        if not frame.empty:
            return frame, SOURCE_TUSHARE_MONEYFLOW_DC
        logger.warning("Tushare moneyflow_dc returned empty rows for %s, falling back to moneyflow", trade_date)
    frame = _fetch_tushare_moneyflow_frame(trade_date, tushare_service=tushare_service)
    return frame, SOURCE_TUSHARE_MONEYFLOW


def sync_tushare_moneyflow_dc_trade_date(
    trade_date: date,
    *,
    tushare_service: Optional[TushareService] = None,
    mode: str = "incremental",
) -> Dict:
    frame = _fetch_tushare_moneyflow_dc_frame(trade_date, tushare_service=tushare_service)
    if frame.empty:
        raise RuntimeError(f"Tushare moneyflow_dc returned empty rows for {trade_date}")
    now = datetime.now(SHANGHAI_TZ).replace(tzinfo=None)
    rows = tushare_moneyflow_dc_frame_to_records(frame, now=now)
    saved = _insert_or_replace_rows(rows)
    return {
        "mode": mode,
        "source": SOURCE_TUSHARE_MONEYFLOW_DC,
        "fetched_rows": len(frame),
        "fetched_symbols": frame["ts_code"].nunique() if "ts_code" in frame.columns else len(rows),
        "saved_rows": saved,
        "trade_dates": [trade_date.isoformat()],
        "latest_trade_date": trade_date.isoformat(),
    }


def sync_tushare_moneyflow_trade_date(
    trade_date: date,
    *,
    tushare_service: Optional[TushareService] = None,
    mode: str = "incremental",
    name_map: Optional[Dict[str, str]] = None,
) -> Dict:
    frame, source = _fetch_tushare_best_moneyflow_frame(trade_date, tushare_service=tushare_service)
    if frame.empty:
        raise RuntimeError(f"Tushare {source} returned empty rows for {trade_date}")
    now = datetime.now(SHANGHAI_TZ).replace(tzinfo=None)
    if source == SOURCE_TUSHARE_MONEYFLOW_DC:
        rows = tushare_moneyflow_dc_frame_to_records(frame, now=now)
    else:
        rows = tushare_moneyflow_frame_to_records(frame, now=now, name_map=name_map)
    saved = _insert_or_replace_rows(rows)
    return {
        "mode": mode,
        "source": source,
        "fetched_rows": len(frame),
        "fetched_symbols": frame["ts_code"].nunique() if "ts_code" in frame.columns else len(rows),
        "saved_rows": saved,
        "trade_dates": [trade_date.isoformat()],
        "latest_trade_date": trade_date.isoformat(),
    }


def sync_current_a_stock_fund_flow_from_eastmoney() -> Dict:
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
        "source": SOURCE_EASTMONEY_PUSH2,
        "total": snapshot.get("total") or len(items),
        "fetched_symbols": len(items),
        "saved_rows": saved,
        "trade_dates": [item.isoformat() for item in trade_dates],
        "latest_trade_date": trade_dates[-1].isoformat() if trade_dates else None,
    }


def sync_current_a_stock_fund_flow(*, tushare_service: Optional[TushareService] = None) -> Dict:
    try:
        trade_dates = resolve_a_stock_fund_flow_trade_dates(
            history_days=1,
            tushare_service=tushare_service,
        )
        if not trade_dates:
            raise RuntimeError("No open A-share trade date resolved for fund flow sync")
        return sync_tushare_moneyflow_trade_date(
            trade_dates[-1],
            tushare_service=tushare_service,
            mode="incremental",
        )
    except Exception as exc:
        logger.warning("Tushare moneyflow_dc incremental sync failed, falling back to Eastmoney push2: %s", exc)
        result = sync_current_a_stock_fund_flow_from_eastmoney()
        result["primary_source"] = SOURCE_TUSHARE_MONEYFLOW_DC
        result["primary_error"] = str(exc)
        return result


def backfill_recent_a_stock_fund_flow_from_eastmoney(
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
        "source": SOURCE_EASTMONEY_PUSH2,
        "symbols": len(symbols),
        "fetched_rows": fetched_rows,
        "saved_rows": saved,
        "start_date": min_trade_date.isoformat() if min_trade_date else None,
        "end_date": max_trade_date.isoformat() if max_trade_date else None,
        "errors": errors,
    }


def backfill_recent_a_stock_fund_flow_from_tushare(
    *,
    start_date: Optional[date] = None,
    history_days: int = DEFAULT_HISTORY_DAYS,
    request_interval_seconds: float = DEFAULT_TUSHARE_REQUEST_INTERVAL_SECONDS,
    tushare_service: Optional[TushareService] = None,
) -> Dict:
    trade_dates = resolve_a_stock_fund_flow_trade_dates(
        start_date=start_date,
        history_days=history_days,
        tushare_service=tushare_service,
    )
    if not trade_dates:
        raise RuntimeError("No open A-share trade dates resolved for fund flow backfill")

    errors: List[Dict] = []
    saved = 0
    fetched_rows = 0
    max_symbols = 0
    min_trade_date: Optional[date] = None
    max_trade_date: Optional[date] = None
    batch_rows: List[Dict] = []
    now = datetime.now(SHANGHAI_TZ).replace(tzinfo=None)
    name_map = _local_stock_name_map()
    source_counts: Dict[str, int] = {}
    for index, trade_date in enumerate(trade_dates, start=1):
        try:
            frame, source = _fetch_tushare_best_moneyflow_frame(
                trade_date,
                tushare_service=tushare_service,
            )
            if frame.empty:
                raise RuntimeError(f"Tushare {source} returned empty rows for {trade_date}")
            if source == SOURCE_TUSHARE_MONEYFLOW_DC:
                records = tushare_moneyflow_dc_frame_to_records(frame, now=now)
            else:
                records = tushare_moneyflow_frame_to_records(frame, now=now, name_map=name_map)
            batch_rows.extend(records)
            fetched_rows += len(frame)
            max_symbols = max(max_symbols, frame["ts_code"].nunique() if "ts_code" in frame.columns else len(records))
            source_counts[source] = source_counts.get(source, 0) + len(records)
            min_trade_date = trade_date if min_trade_date is None else min(min_trade_date, trade_date)
            max_trade_date = trade_date if max_trade_date is None else max(max_trade_date, trade_date)
            if len(batch_rows) >= DEFAULT_BACKFILL_BATCH_ROWS:
                saved += _insert_or_replace_rows(batch_rows)
                logger.info(
                    "A stock Tushare moneyflow_dc backfill saved batch rows=%s progress=%s/%s",
                    len(batch_rows),
                    index,
                    len(trade_dates),
                )
                batch_rows = []
        except Exception as exc:
            errors.append({"trade_date": trade_date.isoformat(), "error": str(exc)})
            logger.warning("A stock Tushare moneyflow_dc backfill failed for %s: %s", trade_date, exc)
        if request_interval_seconds > 0 and index < len(trade_dates):
            time.sleep(request_interval_seconds)

    if batch_rows:
        saved += _insert_or_replace_rows(batch_rows)
        logger.info("A stock Tushare moneyflow_dc backfill saved final batch rows=%s", len(batch_rows))

    return {
        "mode": "full",
        "source": SOURCE_TUSHARE_MONEYFLOW_DC,
        "symbols": max_symbols,
        "fetched_rows": fetched_rows,
        "saved_rows": saved,
        "trade_dates": len(trade_dates),
        "start_date": min_trade_date.isoformat() if min_trade_date else None,
        "end_date": max_trade_date.isoformat() if max_trade_date else None,
        "source_counts": source_counts,
        "errors": errors,
    }


def backfill_recent_a_stock_fund_flow(
    *,
    start_date: Optional[date] = None,
    history_days: int = DEFAULT_HISTORY_DAYS,
    request_interval_seconds: float = DEFAULT_TUSHARE_REQUEST_INTERVAL_SECONDS,
    tushare_service: Optional[TushareService] = None,
) -> Dict:
    try:
        return backfill_recent_a_stock_fund_flow_from_tushare(
            start_date=start_date,
            history_days=history_days,
            request_interval_seconds=request_interval_seconds,
            tushare_service=tushare_service,
        )
    except Exception as exc:
        logger.warning("Tushare moneyflow_dc backfill failed, falling back to Eastmoney push2his: %s", exc)
        result = backfill_recent_a_stock_fund_flow_from_eastmoney(
            start_date=start_date,
            history_days=history_days,
        )
        result["primary_source"] = SOURCE_TUSHARE_MONEYFLOW_DC
        result["primary_error"] = str(exc)
        return result


def sync_a_stock_fund_flow(
    start_date: Optional[date] = None,
    full: Optional[bool] = None,
    tushare_service: Optional[TushareService] = None,
) -> Dict:
    existing_rows = _table_row_count()
    previous_latest_trade_date = _latest_trade_date()
    should_full = bool(full) or start_date is not None or existing_rows == 0
    if should_full:
        result = backfill_recent_a_stock_fund_flow(
            start_date=start_date,
            tushare_service=tushare_service,
        )
        result["existing_rows_before"] = existing_rows
        result["previous_latest_trade_date"] = (
            previous_latest_trade_date.isoformat() if previous_latest_trade_date else None
        )
        return result

    result = sync_current_a_stock_fund_flow(tushare_service=tushare_service)
    result["existing_rows_before"] = existing_rows
    result["previous_latest_trade_date"] = (
        previous_latest_trade_date.isoformat() if previous_latest_trade_date else None
    )
    return result

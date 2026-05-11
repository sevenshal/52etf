import hashlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..core.analytics_database import (
    ANALYTICS_DB_PATH,
    AStockBasic,
    AStockChinaBondYieldCurveDaily,
    AStockChinaBondYieldCurveDef,
    AStockFundDaily,
    AStockIncome,
    AStockIndexDaily,
    AStockIndexWeight,
    AStockMarketDaily,
    AStockNameChange,
    AStockOptionBasic,
    AStockOptionDaily,
    AStockRepoDaily,
    AnalyticsSession,
)
from ..core.services.chinabond import ChinaBondYieldCurveService
from ..core.services.tushare import TushareService
from .a_stock_base_data_config import (
    A_STOCK_FEAR_SAFE_HAVEN_INDEXES,
    A_STOCK_FACTOR_INDEX_POOLS,
    A_STOCK_ETF_DAILY_SYMBOLS,
    A_STOCK_INDEX_FEAR_GREED_TARGETS,
    BENCHMARK_INDEXES,
    CHINABOND_CREDIT_CURVES,
    DEFAULT_START_DATE,
    MAX_MARKET_DAILY_OHL_ZERO_PCT,
    MIN_MARKET_DAILY_ROWS,
    RAW_FETCH_LOOKBACK_DAYS,
)


logger = logging.getLogger(__name__)

ProgressCallback = Callable[[Dict], None]

SYNC_WORKERS = 5
SYNC_REFRESH_OVERLAP_DAYS = 45
A_STOCK_MARKET_DAILY_WARMUP_DAYS = 550
A_STOCK_INDEX_DAILY_WARMUP_DAYS = 220
A_STOCK_FUND_DAILY_WARMUP_DAYS = 220
A_STOCK_INDEX_WEIGHT_WARMUP_DAYS = 200
A_STOCK_OPTION_DAILY_WARMUP_DAYS = 200
A_STOCK_REPO_DAILY_WARMUP_DAYS = 200
A_STOCK_CHINABOND_WARMUP_DAYS = 200
INCOME_HISTORY_LOOKBACK_DAYS = 365 * 6
INCOME_INSERT_BATCH_ROWS = 5000
INCOME_INSERT_BATCH_FRAMES = 500
A_STOCK_OPTION_DAILY_CHUNK_TRADING_DAYS = 20
A_STOCK_OPTION_DAILY_CHUNK_CALENDAR_DAYS = 45
A_STOCK_REPO_DAILY_CHUNK_TRADING_DAYS = 30
A_STOCK_REPO_DAILY_CHUNK_CALENDAR_DAYS = 70
A_STOCK_CHINABOND_CHUNK_TRADING_DAYS = 10
A_STOCK_CHINABOND_CHUNK_CALENDAR_DAYS = 30
A_STOCK_OPTION_DAILY_SYNC_EXCHANGES = ("SSE", "SZSE")


def _clean_text(value) -> Optional[str]:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    text_value = str(value).strip()
    return text_value or None


def _parse_date(value) -> Optional[date]:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text_value = str(value).strip()
    if not text_value:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text_value, fmt).date()
        except ValueError:
            continue
    return None


def _year_chunks(start_date: date, end_date: date) -> Iterable[Tuple[date, date]]:
    current = date(start_date.year, 1, 1)
    if current < start_date:
        current = start_date
    while current <= end_date:
        chunk_end = min(date(current.year, 12, 31), end_date)
        yield current, chunk_end
        current = chunk_end + timedelta(days=1)


def _quote_duckdb_identifier(identifier: str) -> str:
    return f'"{identifier.replace(chr(34), chr(34) * 2)}"'


def _clean_text_series(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(pd.NA, index=frame.index, dtype="string")
    result = frame[column].astype("string").str.strip()
    return result.mask(result == "")


def _date_series(frame: pd.DataFrame, column: str, fallback: Optional[date] = None) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(fallback, index=frame.index, dtype="object")

    text_values = frame[column].astype("string").str.strip()
    parsed = pd.to_datetime(text_values, format="%Y%m%d", errors="coerce")
    missing = parsed.isna() & text_values.notna() & (text_values != "")
    if missing.any():
        parsed.loc[missing] = pd.to_datetime(text_values.loc[missing], format="%Y-%m-%d", errors="coerce")
    if fallback is not None:
        parsed = parsed.fillna(pd.Timestamp(fallback))
    return parsed.dt.date


def _numeric_series(frame: pd.DataFrame, column: str, digits: int) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(pd.array([pd.NA] * len(frame), dtype="Float64"), index=frame.index)
    return pd.to_numeric(frame[column], errors="coerce").round(digits).astype("Float64")


def _insert_or_replace_analytics_frame(
    table_name: str,
    columns: List[str],
    frame: pd.DataFrame,
    replace_dates: Optional[List[date]] = None,
):
    if frame.empty:
        return

    import duckdb  # type: ignore

    insert_frame = frame.loc[:, columns]
    quoted_table = _quote_duckdb_identifier(table_name)
    quoted_columns = ", ".join(_quote_duckdb_identifier(column) for column in columns)
    temp_frame_name = "analytics_insert_frame"
    temp_delete_dates_name = "analytics_delete_dates"
    insert_sql = (
        f"INSERT OR REPLACE INTO {quoted_table} ({quoted_columns}) "
        f"SELECT {quoted_columns} FROM {_quote_duckdb_identifier(temp_frame_name)}"
    )

    connection = duckdb.connect(database=ANALYTICS_DB_PATH, read_only=False)
    try:
        connection.execute("BEGIN TRANSACTION")
        if replace_dates:
            delete_frame = pd.DataFrame({"trade_date": replace_dates})
            connection.register(temp_delete_dates_name, delete_frame)
            connection.execute(
                f"""
                DELETE FROM {quoted_table}
                USING {_quote_duckdb_identifier(temp_delete_dates_name)}
                WHERE {quoted_table}.trade_date = {_quote_duckdb_identifier(temp_delete_dates_name)}.trade_date
                """
            )
        connection.register(temp_frame_name, insert_frame)
        connection.execute(insert_sql)
        connection.execute("COMMIT")
    except Exception:
        try:
            connection.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        connection.close()


def _chunks(items: List, size: int):
    for index in range(0, len(items), size):
        yield items[index:index + size]


def _date_chunks_by_span(
    dates: List[date],
    max_items: int,
    max_calendar_span_days: int,
) -> List[List[date]]:
    chunks: List[List[date]] = []
    current_chunk: List[date] = []
    for current_date in dates:
        if current_chunk and (
            len(current_chunk) >= max_items
            or (current_date - current_chunk[0]).days > max_calendar_span_days
        ):
            chunks.append(current_chunk)
            current_chunk = []
        current_chunk.append(current_date)
    if current_chunk:
        chunks.append(current_chunk)
    return chunks


def _warmup_start(anchor_date: date, warmup_days: int) -> date:
    return anchor_date - timedelta(days=warmup_days)


def _market_day_needs_refresh(day_stats: Optional[Dict]) -> bool:
    if not day_stats:
        return True
    row_count = int(day_stats.get("row_count") or 0)
    if row_count < MIN_MARKET_DAILY_ROWS:
        return True
    ohl_zero_rows = int(day_stats.get("ohl_zero_rows") or 0)
    ohl_zero_pct = ohl_zero_rows / row_count * 100 if row_count else 100.0
    return ohl_zero_pct > MAX_MARKET_DAILY_OHL_ZERO_PCT


def _option_day_needs_refresh(day_stats: Optional[Dict]) -> bool:
    if not day_stats:
        return True
    row_count = int(day_stats.get("row_count") or 0)
    if row_count <= 0:
        return True
    exchange_count = int(day_stats.get("exchange_count") or 0)
    return exchange_count < len(A_STOCK_OPTION_DAILY_SYNC_EXCHANGES)


def _repo_day_needs_refresh(day_stats: Optional[Dict]) -> bool:
    if not day_stats:
        return True
    return int(day_stats.get("row_count") or 0) <= 0


def _chinabond_day_needs_refresh(day_stats: Optional[Dict]) -> bool:
    if not day_stats:
        return True
    row_count = int(day_stats.get("row_count") or 0)
    curve_count = int(day_stats.get("curve_count") or 0)
    return row_count <= 0 or curve_count < len(CHINABOND_CREDIT_CURVES)


def _a_stock_index_daily_items() -> List[Dict[str, Optional[str]]]:
    return list(
        {
            str(item["ts_code"]).upper(): item
            for item in [
                *BENCHMARK_INDEXES,
                *A_STOCK_FEAR_SAFE_HAVEN_INDEXES,
                *[
                    {
                        "ts_code": str(item.get("index_code") or item["symbol"]).upper(),
                        "name": item.get("index_name") or item.get("label"),
                    }
                    for item in A_STOCK_INDEX_FEAR_GREED_TARGETS
                ],
                *[
                    {"ts_code": item["index_code"], "name": item["name"]}
                    for item in A_STOCK_FACTOR_INDEX_POOLS
                ],
            ]
        }.values()
    )


class AStockBaseDataSyncService:
    def __init__(
        self,
        analytics_db: Optional[Session] = None,
        tushare_service: Optional[TushareService] = None,
        progress_callback: Optional[ProgressCallback] = None,
    ):
        self.analytics_db = analytics_db or AnalyticsSession()
        self._owns_analytics_db = analytics_db is None
        self.tushare = tushare_service or TushareService.getInstance()
        self.chinabond = ChinaBondYieldCurveService()
        self.progress_callback = progress_callback
        self.logger = logging.getLogger(self.__class__.__name__)

    def close(self):
        if self._owns_analytics_db:
            AnalyticsSession.remove()

    def _progress(self, message: str, progress: int, **extra):
        payload = {
            "message": message,
            "progress": max(0, min(100, int(progress))),
            **extra,
        }
        self.logger.info("%s (%s%%)", message, payload["progress"])
        if self.progress_callback:
            self.progress_callback(payload)

    def sync_reference_data(self, start_date: date, end_date: date):
        self._progress("同步A股基础信息", 2)
        basic_frame = self.tushare.get_a_stock_basic_frame(["L", "D"])
        if not basic_frame.empty:
            self._upsert_stock_basic(basic_frame)

        self._progress("同步A股名称/ST变更记录", 4)
        name_frames = []
        name_start = max(date(1990, 1, 1), start_date - timedelta(days=3650))
        for chunk_start, chunk_end in _year_chunks(name_start, end_date + timedelta(days=30)):
            frame = self.tushare.get_a_stock_name_changes_frame(chunk_start, chunk_end)
            if not frame.empty:
                name_frames.append(frame)
        if name_frames:
            name_frame = pd.concat(name_frames, ignore_index=True).drop_duplicates()
            self._replace_name_changes(name_frame)

    def sync_reference_data_incremental(self, start_date: date, end_date: date):
        self._progress("增量同步A股基础信息", 5)
        basic_frame = self.tushare.get_a_stock_basic_frame(["L", "D"])
        if not basic_frame.empty:
            self._upsert_stock_basic(basic_frame)

        self._progress("增量同步A股名称/ST变更记录", 8)
        name_frames = []
        name_start = max(date(1990, 1, 1), start_date - timedelta(days=90))
        name_end = end_date + timedelta(days=30)
        for chunk_start, chunk_end in _year_chunks(name_start, name_end):
            frame = self.tushare.get_a_stock_name_changes_frame(chunk_start, chunk_end)
            if not frame.empty:
                name_frames.append(frame)
        if name_frames:
            name_frame = pd.concat(name_frames, ignore_index=True).drop_duplicates()
            self._replace_name_changes_range(name_frame, name_start, name_end)

    def _upsert_stock_basic(self, frame: pd.DataFrame):
        now = datetime.now()
        mappings = []
        for _, row in frame.iterrows():
            ts_code = str(row.get("ts_code") or "").strip()
            if not ts_code:
                continue
            mappings.append(
                {
                    "ts_code": ts_code,
                    "symbol": _clean_text(row.get("symbol")),
                    "name": _clean_text(row.get("name")),
                    "area": _clean_text(row.get("area")),
                    "industry": _clean_text(row.get("industry")),
                    "market": _clean_text(row.get("market")),
                    "exchange": _clean_text(row.get("exchange")),
                    "list_date": _parse_date(row.get("list_date")),
                    "delist_date": _parse_date(row.get("delist_date")),
                    "list_status": _clean_text(row.get("list_status")),
                    "updated_at": now,
                }
            )
        self._replace_analytics_table(AStockBasic, mappings)

    def _replace_name_changes(self, frame: pd.DataFrame):
        self.analytics_db.query(AStockNameChange).delete(synchronize_session=False)
        self.analytics_db.commit()
        self._insert_name_changes(frame)

    def _replace_name_changes_range(self, frame: pd.DataFrame, start_date: date, end_date: date):
        self.analytics_db.execute(
            text("""
                DELETE FROM a_stock_name_changes
                WHERE start_date >= :start_date AND start_date <= :end_date
            """),
            {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
        )
        self.analytics_db.commit()
        self._insert_name_changes(frame)

    def _insert_name_changes(self, frame: pd.DataFrame):
        now = datetime.now()
        mappings = []
        for _, row in frame.iterrows():
            ts_code = str(row.get("ts_code") or "").strip()
            if not ts_code:
                continue
            start = _parse_date(row.get("start_date"))
            end = _parse_date(row.get("end_date"))
            name = _clean_text(row.get("name"))
            reason = _clean_text(row.get("change_reason"))
            row_id = hashlib.sha1(
                "|".join(
                    [
                        ts_code,
                        name or "",
                        start.isoformat() if start else "",
                        end.isoformat() if end else "",
                        reason or "",
                    ]
                ).encode("utf-8")
            ).hexdigest()
            mappings.append(
                {
                    "id": row_id,
                    "ts_code": ts_code,
                    "name": name,
                    "start_date": start,
                    "end_date": end,
                    "change_reason": reason,
                    "updated_at": now,
                }
            )
        mappings = list({item["id"]: item for item in mappings}.values())
        self._insert_analytics_mappings(AStockNameChange, mappings)

    def _replace_analytics_table(self, model, mappings: List[Dict], batch_size: int = 1000):
        self.analytics_db.query(model).delete(synchronize_session=False)
        self.analytics_db.commit()
        self._insert_analytics_mappings(model, mappings, batch_size=batch_size)

    def _insert_analytics_mappings(self, model, mappings: List[Dict], batch_size: int = 1000):
        if not mappings:
            return
        table = model.__table__
        for batch in _chunks(mappings, batch_size):
            self.analytics_db.execute(table.insert(), batch)
        self.analytics_db.commit()

    def _existing_market_day_stats(self, start_date: date, end_date: date) -> Dict[date, Dict]:
        rows = self.analytics_db.execute(
            text("""
                SELECT
                    trade_date,
                    COUNT(*) AS row_count,
                    SUM(CASE
                        WHEN COALESCE(open, 0) = 0
                          OR COALESCE(high, 0) = 0
                          OR COALESCE(low, 0) = 0
                        THEN 1 ELSE 0
                    END) AS ohl_zero_rows
                FROM a_stock_market_daily
                WHERE trade_date >= :start_date AND trade_date <= :end_date
                GROUP BY trade_date
            """),
            {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
        ).fetchall()
        return {
            _parse_date(row[0]): {
                "row_count": int(row[1] or 0),
                "ohl_zero_rows": int(row[2] or 0),
            }
            for row in rows
            if _parse_date(row[0])
        }

    def _ensure_market_days(self, trading_dates: List[date]):
        if not trading_dates:
            return
        stats_by_date = self._existing_market_day_stats(min(trading_dates), max(trading_dates))
        missing_dates = [item for item in trading_dates if _market_day_needs_refresh(stats_by_date.get(item))]
        if not missing_dates:
            self._progress("全市场日行情缓存已就绪", 50, processed_dates=len(trading_dates), total_dates=len(trading_dates))
            return

        chunk_size = 8
        max_calendar_span_days = 20
        chunks = []
        current_chunk: List[date] = []
        for missing_date in missing_dates:
            if current_chunk and (
                len(current_chunk) >= chunk_size
                or (missing_date - current_chunk[0]).days > max_calendar_span_days
            ):
                chunks.append(current_chunk)
                current_chunk = []
            current_chunk.append(missing_date)
        if current_chunk:
            chunks.append(current_chunk)

        def fetch_chunk(chunk: List[date]) -> Tuple[date, date, pd.DataFrame]:
            chunk_start = min(chunk)
            chunk_end = max(chunk)
            frame = self.tushare.get_a_stock_market_daily_range_frame(chunk_start, chunk_end)
            return chunk_start, chunk_end, frame

        completed = 0
        workers = min(SYNC_WORKERS, len(chunks))
        if workers <= 1:
            for chunk in chunks:
                chunk_start, chunk_end, frame = fetch_chunk(chunk)
                completed += 1
                progress = 6 + int(completed / max(len(chunks), 1) * 44)
                self._progress(
                    f"批量缓存全市场日行情 {chunk_start.isoformat()} ~ {chunk_end.isoformat()}",
                    progress,
                    processed_chunks=completed,
                    total_chunks=len(chunks),
                )
                if not frame.empty:
                    self._upsert_market_frame(frame)
            return

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(fetch_chunk, chunk) for chunk in chunks]
            for future in as_completed(futures):
                chunk_start, chunk_end, frame = future.result()
                completed += 1
                progress = 6 + int(completed / max(len(chunks), 1) * 44)
                self._progress(
                    f"批量缓存全市场日行情 {chunk_start.isoformat()} ~ {chunk_end.isoformat()}",
                    progress,
                    processed_chunks=completed,
                    total_chunks=len(chunks),
                )
                if not frame.empty:
                    self._upsert_market_frame(frame)

    def _existing_option_day_stats(self, start_date: date, end_date: date) -> Dict[date, Dict]:
        rows = self.analytics_db.execute(
            text("""
                SELECT
                    trade_date,
                    COUNT(*) AS row_count,
                    COUNT(DISTINCT exchange) AS exchange_count
                FROM a_stock_option_daily
                WHERE trade_date >= :start_date AND trade_date <= :end_date
                GROUP BY trade_date
            """),
            {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
        ).fetchall()
        return {
            _parse_date(row[0]): {
                "row_count": int(row[1] or 0),
                "exchange_count": int(row[2] or 0),
            }
            for row in rows
            if _parse_date(row[0])
        }

    def _existing_repo_day_stats(self, start_date: date, end_date: date) -> Dict[date, Dict]:
        rows = self.analytics_db.execute(
            text("""
                SELECT trade_date, COUNT(*) AS row_count
                FROM a_stock_repo_daily
                WHERE trade_date >= :start_date AND trade_date <= :end_date
                GROUP BY trade_date
            """),
            {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
        ).fetchall()
        return {
            _parse_date(row[0]): {"row_count": int(row[1] or 0)}
            for row in rows
            if _parse_date(row[0])
        }

    def _existing_chinabond_day_stats(self, start_date: date, end_date: date) -> Dict[date, Dict]:
        rows = self.analytics_db.execute(
            text("""
                SELECT
                    trade_date,
                    COUNT(*) AS row_count,
                    COUNT(DISTINCT curve_id) AS curve_count
                FROM a_stock_chinabond_yield_curve_daily
                WHERE trade_date >= :start_date AND trade_date <= :end_date
                GROUP BY trade_date
            """),
            {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
        ).fetchall()
        return {
            _parse_date(row[0]): {
                "row_count": int(row[1] or 0),
                "curve_count": int(row[2] or 0),
            }
            for row in rows
            if _parse_date(row[0])
        }

    def _upsert_market_frame(self, frame: pd.DataFrame, trade_date: Optional[date] = None):
        market_frame = self._normalize_market_frame(frame, trade_date=trade_date)
        self._bulk_replace_market_daily(market_frame)

    @staticmethod
    def _normalize_market_frame(frame: pd.DataFrame, trade_date: Optional[date] = None) -> pd.DataFrame:
        columns = [
            "trade_date",
            "ts_code",
            "open",
            "high",
            "low",
            "close",
            "pre_close",
            "change",
            "pct_chg",
            "vol",
            "amount",
            "total_mv",
            "circ_mv",
            "float_share",
            "total_share",
            "turnover_rate",
            "created_at",
            "updated_at",
        ]
        if frame.empty:
            return pd.DataFrame(columns=columns)

        now = datetime.now()
        normalized = pd.DataFrame(index=frame.index)
        normalized["trade_date"] = _date_series(frame, "trade_date", fallback=trade_date)
        normalized["ts_code"] = _clean_text_series(frame, "ts_code")
        normalized["open"] = _numeric_series(frame, "open", 6)
        normalized["high"] = _numeric_series(frame, "high", 6)
        normalized["low"] = _numeric_series(frame, "low", 6)
        normalized["close"] = _numeric_series(frame, "close", 6)
        normalized["pre_close"] = _numeric_series(frame, "pre_close", 6)
        normalized["change"] = _numeric_series(frame, "change", 6)
        normalized["pct_chg"] = _numeric_series(frame, "pct_chg", 6)
        normalized["vol"] = _numeric_series(frame, "vol", 4)
        normalized["amount"] = _numeric_series(frame, "amount", 4)
        normalized["total_mv"] = _numeric_series(frame, "total_mv", 4)
        normalized["circ_mv"] = _numeric_series(frame, "circ_mv", 4)
        normalized["float_share"] = _numeric_series(frame, "float_share", 4)
        normalized["total_share"] = _numeric_series(frame, "total_share", 4)
        normalized["turnover_rate"] = _numeric_series(frame, "turnover_rate", 6)
        normalized["created_at"] = now
        normalized["updated_at"] = now
        normalized = normalized.dropna(subset=["trade_date", "ts_code"])
        if normalized.empty:
            return pd.DataFrame(columns=columns)
        normalized = normalized.drop_duplicates(subset=["trade_date", "ts_code"], keep="last")
        return normalized.loc[:, columns]

    def _bulk_replace_market_daily(self, frame: pd.DataFrame):
        if frame.empty:
            return
        self.analytics_db.commit()
        date_counts = frame["trade_date"].value_counts()
        replace_dates = sorted(
            trade_date
            for trade_date, row_count in date_counts.to_dict().items()
            if row_count >= MIN_MARKET_DAILY_ROWS
        )
        columns = [
            "trade_date",
            "ts_code",
            "open",
            "high",
            "low",
            "close",
            "pre_close",
            "change",
            "pct_chg",
            "vol",
            "amount",
            "total_mv",
            "circ_mv",
            "float_share",
            "total_share",
            "turnover_rate",
            "created_at",
            "updated_at",
        ]
        table = AStockMarketDaily.__table__
        _insert_or_replace_analytics_frame(
            table.name,
            columns,
            frame,
            replace_dates=replace_dates,
        )

    def _index_daily_stats(self, index_codes: List[str]) -> Dict[str, Dict[str, Optional[date]]]:
        if not index_codes:
            return {}
        normalized_codes = [str(code or "").strip().upper() for code in index_codes if code]
        params = {f"code_{idx}": code for idx, code in enumerate(normalized_codes)}
        placeholders = ", ".join(f":{key}" for key in params)
        rows = self.analytics_db.execute(
            text(
                f"""
                SELECT ts_code, MIN(trade_date), MAX(trade_date), COUNT(*)
                FROM a_stock_index_daily
                WHERE ts_code IN ({placeholders})
                GROUP BY ts_code
                """
            ),
            params,
        ).fetchall()
        stats = {
            code: {"min_date": None, "max_date": None, "rows": 0}
            for code in normalized_codes
        }
        for row in rows:
            code = str(row[0]).upper()
            stats[code] = {
                "min_date": _parse_date(row[1]),
                "max_date": _parse_date(row[2]),
                "rows": int(row[3] or 0),
            }
        return stats

    def sync_index_daily(
        self,
        start_date: date,
        end_date: date,
        incremental: bool = True,
        explicit_start: Optional[date] = None,
    ) -> Dict:
        default_start = _parse_date(start_date)
        end_value = _parse_date(end_date)
        index_items = _a_stock_index_daily_items()
        if not default_start or not end_value or default_start > end_value or not index_items:
            return {"index_count": len(index_items), "jobs": 0, "saved_rows": 0, "errors": [], "start_date": None}

        index_codes = [str(item["ts_code"]).upper() for item in index_items if item.get("ts_code")]
        stats_by_index = self._index_daily_stats(index_codes)
        jobs: List[Tuple[str, date, date]] = []
        for index_code in index_codes:
            stats = stats_by_index.get(index_code) or {}
            min_date = stats.get("min_date")
            max_date = stats.get("max_date")
            if explicit_start:
                index_start = _warmup_start(explicit_start, A_STOCK_INDEX_DAILY_WARMUP_DAYS)
            elif incremental:
                if not min_date or min_date > default_start:
                    index_start = default_start
                else:
                    index_start = _overlap_start(default_start, max_date)
            else:
                index_start = default_start
            if index_start <= end_value:
                jobs.append((index_code, index_start, end_value))

        if not jobs:
            return {
                "index_count": len(index_items),
                "jobs": 0,
                "saved_rows": 0,
                "errors": [],
                "start_date": None,
                "end_date": end_value.isoformat(),
            }

        saved_rows = 0
        errors: List[Dict[str, str]] = []
        completed = 0
        total_jobs = len(jobs)

        def fetch_job(index_code: str, index_start: date, index_end: date) -> Tuple[str, date, date, pd.DataFrame]:
            frame = self.tushare.get_index_daily_range_frame(index_code, index_start, index_end)
            return index_code, index_start, index_end, frame

        def report_progress(index_code: str, index_start: date, index_end: date):
            self._progress(
                (
                    f"批量同步A股指数日行情 {completed}/{total_jobs}，"
                    f"最近完成 {index_code} {index_start.isoformat()} ~ {index_end.isoformat()}"
                ),
                62,
                processed_jobs=completed,
                total_jobs=total_jobs,
                index_daily_saved_rows=saved_rows,
                index_daily_errors=len(errors),
            )

        workers = min(SYNC_WORKERS, total_jobs)
        if workers <= 1:
            for index_code, index_start, index_end in jobs:
                try:
                    _, _, _, frame = fetch_job(index_code, index_start, index_end)
                    saved_rows += _upsert_index_daily_frame(self.analytics_db, frame)
                except Exception as exc:
                    self.logger.warning("A stock index daily sync failed for %s %s~%s: %s", index_code, index_start, index_end, exc)
                    errors.append({"index_code": index_code, "start_date": index_start.isoformat(), "end_date": index_end.isoformat(), "error": str(exc)})
                completed += 1
                report_progress(index_code, index_start, index_end)
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(fetch_job, index_code, index_start, index_end): (index_code, index_start, index_end)
                    for index_code, index_start, index_end in jobs
                }
                for future in as_completed(futures):
                    index_code, index_start, index_end = futures[future]
                    try:
                        _, _, _, frame = future.result()
                        saved_rows += _upsert_index_daily_frame(self.analytics_db, frame)
                    except Exception as exc:
                        self.logger.warning("A stock index daily sync failed for %s %s~%s: %s", index_code, index_start, index_end, exc)
                        errors.append({"index_code": index_code, "start_date": index_start.isoformat(), "end_date": index_end.isoformat(), "error": str(exc)})
                    completed += 1
                    report_progress(index_code, index_start, index_end)

        return {
            "index_count": len(index_items),
            "jobs": total_jobs,
            "saved_rows": saved_rows,
            "errors": errors,
            "start_date": min(job[1] for job in jobs).isoformat(),
            "end_date": end_value.isoformat(),
        }

    def _latest_fund_daily_dates(self, symbols: List[str]) -> Dict[str, Optional[date]]:
        if not symbols:
            return {}
        normalized_symbols = [str(symbol or "").strip().upper() for symbol in symbols if symbol]
        params = {f"symbol_{idx}": symbol for idx, symbol in enumerate(normalized_symbols)}
        placeholders = ", ".join(f":{key}" for key in params)
        rows = self.analytics_db.execute(
            text(
                f"""
                SELECT ts_code, MAX(trade_date)
                FROM a_stock_fund_daily
                WHERE ts_code IN ({placeholders})
                GROUP BY ts_code
                """
            ),
            params,
        ).fetchall()
        latest = {symbol: None for symbol in normalized_symbols}
        for row in rows:
            latest[str(row[0]).upper()] = _parse_date(row[1])
        return latest

    def sync_fund_daily(
        self,
        start_date: date,
        end_date: date,
        incremental: bool = True,
        explicit_start: Optional[date] = None,
    ) -> Dict:
        end_value = _parse_date(end_date)
        default_start = _parse_date(start_date)
        symbols = list(dict.fromkeys(str(symbol or "").strip().upper() for symbol in A_STOCK_ETF_DAILY_SYMBOLS if symbol))
        if not default_start or not end_value or default_start > end_value or not symbols:
            return {"symbol_count": len(symbols), "jobs": 0, "saved_rows": 0, "errors": [], "start_date": None}

        latest_by_symbol = self._latest_fund_daily_dates(symbols)
        jobs: List[Tuple[str, date, date]] = []
        for symbol in symbols:
            if explicit_start:
                symbol_start = _warmup_start(explicit_start, A_STOCK_FUND_DAILY_WARMUP_DAYS)
            elif incremental:
                symbol_start = _overlap_start(default_start, latest_by_symbol.get(symbol))
            else:
                symbol_start = default_start
            if symbol_start <= end_value:
                jobs.append((symbol, symbol_start, end_value))

        if not jobs:
            return {
                "symbol_count": len(symbols),
                "jobs": 0,
                "saved_rows": 0,
                "errors": [],
                "start_date": None,
                "end_date": end_value.isoformat(),
            }

        def fetch_job(symbol: str, symbol_start: date, symbol_end: date) -> Tuple[str, date, date, pd.DataFrame]:
            frame = self.tushare.get_a_stock_fund_daily_range_frame(
                symbol,
                symbol_start,
                symbol_end,
                raise_on_error=True,
            )
            return symbol, symbol_start, symbol_end, frame

        saved_rows = 0
        errors: List[Dict[str, str]] = []
        completed = 0
        total_jobs = len(jobs)
        workers = min(SYNC_WORKERS, total_jobs)

        def report_progress(symbol: str, symbol_start: date, symbol_end: date):
            self._progress(
                (
                    f"批量同步A股ETF日行情 {completed}/{total_jobs}，"
                    f"最近完成 {symbol} {symbol_start.isoformat()} ~ {symbol_end.isoformat()}"
                ),
                64,
                processed_jobs=completed,
                total_jobs=total_jobs,
                fund_daily_saved_rows=saved_rows,
                fund_daily_errors=len(errors),
            )

        if workers <= 1:
            for symbol, symbol_start, symbol_end in jobs:
                try:
                    _, _, _, frame = fetch_job(symbol, symbol_start, symbol_end)
                    saved_rows += _bulk_upsert_fund_daily_frame(self.analytics_db, frame)
                except Exception as exc:
                    self.logger.warning("A stock ETF daily sync failed for %s %s~%s: %s", symbol, symbol_start, symbol_end, exc)
                    errors.append({"symbol": symbol, "start_date": symbol_start.isoformat(), "end_date": symbol_end.isoformat(), "error": str(exc)})
                completed += 1
                report_progress(symbol, symbol_start, symbol_end)
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(fetch_job, symbol, symbol_start, symbol_end): (symbol, symbol_start, symbol_end)
                    for symbol, symbol_start, symbol_end in jobs
                }
                for future in as_completed(futures):
                    symbol, symbol_start, symbol_end = futures[future]
                    try:
                        _, _, _, frame = future.result()
                        saved_rows += _bulk_upsert_fund_daily_frame(self.analytics_db, frame)
                    except Exception as exc:
                        self.logger.warning("A stock ETF daily sync failed for %s %s~%s: %s", symbol, symbol_start, symbol_end, exc)
                        errors.append({"symbol": symbol, "start_date": symbol_start.isoformat(), "end_date": symbol_end.isoformat(), "error": str(exc)})
                    completed += 1
                    report_progress(symbol, symbol_start, symbol_end)

        return {
            "symbol_count": len(symbols),
            "jobs": total_jobs,
            "saved_rows": saved_rows,
            "errors": errors,
            "start_date": min(job[1] for job in jobs).isoformat(),
            "end_date": end_value.isoformat(),
        }

    def _latest_index_weight_dates(self, index_codes: List[str]) -> Dict[str, Optional[date]]:
        if not index_codes:
            return {}
        params = {f"code_{idx}": code for idx, code in enumerate(index_codes)}
        placeholders = ", ".join(f":{key}" for key in params)
        rows = self.analytics_db.execute(
            text(
                f"""
                SELECT index_code, MAX(trade_date)
                FROM a_stock_index_weight
                WHERE index_code IN ({placeholders})
                GROUP BY index_code
                """
            ),
            params,
        ).fetchall()
        latest = {code: None for code in index_codes}
        for row in rows:
            latest[str(row[0])] = _parse_date(row[1])
        return latest

    def sync_index_weight(
        self,
        start_date: date,
        end_date: date,
        incremental: bool = True,
        explicit_start: Optional[date] = None,
    ) -> Dict:
        end_value = _parse_date(end_date)
        if not end_value:
            return {"index_count": 0, "jobs": 0, "saved_rows": 0, "errors": []}

        index_items = list(
            {
                str(item.get("index_code") or item["symbol"]).upper(): {
                    "index_code": str(item.get("index_code") or item["symbol"]).upper(),
                    "index_name": item.get("index_name") or item.get("name") or item.get("label"),
                }
                for item in [
                    *A_STOCK_INDEX_FEAR_GREED_TARGETS,
                    *[
                        {"index_code": item["index_code"], "index_name": item["name"]}
                        for item in A_STOCK_FACTOR_INDEX_POOLS
                    ],
                ]
            }.values()
        )
        latest_by_index = self._latest_index_weight_dates([item["index_code"] for item in index_items])
        default_start = _parse_date(start_date) or _warmup_start(DEFAULT_START_DATE, A_STOCK_INDEX_WEIGHT_WARMUP_DAYS)
        jobs: List[Tuple[str, date, date]] = []
        for item in index_items:
            index_code = item["index_code"]
            if explicit_start:
                index_start = _warmup_start(explicit_start, A_STOCK_INDEX_WEIGHT_WARMUP_DAYS)
            elif incremental:
                index_start = _overlap_start(default_start, latest_by_index.get(index_code))
            else:
                index_start = default_start
            if index_start > end_value:
                continue
            for chunk_start, chunk_end in _year_chunks(index_start, end_value):
                jobs.append((index_code, chunk_start, chunk_end))

        if not jobs:
            return {
                "index_count": len(index_items),
                "jobs": 0,
                "saved_rows": 0,
                "errors": [],
                "start_date": None,
                "end_date": end_value.isoformat(),
            }

        saved_rows = 0
        errors = []
        completed = 0
        total_jobs = len(jobs)

        def fetch_job(index_code: str, chunk_start: date, chunk_end: date) -> Tuple[str, date, date, pd.DataFrame]:
            frame = self.tushare.get_index_weight_range_frame(index_code, chunk_start, chunk_end, raise_on_error=True)
            return index_code, chunk_start, chunk_end, frame

        def save_frame(frame: pd.DataFrame) -> int:
            return _bulk_replace_index_weight_frame(self.analytics_db, frame)

        def report_progress(index_code: str, chunk_start: date, chunk_end: date):
            self._progress(
                f"批量同步A股指数成分权重 {completed}/{total_jobs}，{index_code} {chunk_start.isoformat()} ~ {chunk_end.isoformat()}",
                66,
                processed_jobs=completed,
                total_jobs=total_jobs,
                index_code=index_code,
                chunk_start=chunk_start.isoformat(),
                chunk_end=chunk_end.isoformat(),
                index_weight_saved_rows=saved_rows,
                index_weight_errors=len(errors),
            )

        workers = min(SYNC_WORKERS, total_jobs)
        if workers <= 1:
            for index_code, chunk_start, chunk_end in jobs:
                try:
                    _, _, _, frame = fetch_job(index_code, chunk_start, chunk_end)
                    saved_rows += save_frame(frame)
                except Exception as exc:
                    self.logger.warning("A stock index_weight sync failed for %s %s~%s: %s", index_code, chunk_start, chunk_end, exc)
                    errors.append({"index_code": index_code, "start_date": chunk_start.isoformat(), "end_date": chunk_end.isoformat(), "error": str(exc)})
                completed += 1
                if completed == 1 or completed == total_jobs or completed % 5 == 0:
                    report_progress(index_code, chunk_start, chunk_end)
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(fetch_job, index_code, chunk_start, chunk_end): (index_code, chunk_start, chunk_end)
                    for index_code, chunk_start, chunk_end in jobs
                }
                for future in as_completed(futures):
                    index_code, chunk_start, chunk_end = futures[future]
                    try:
                        _, _, _, frame = future.result()
                        saved_rows += save_frame(frame)
                    except Exception as exc:
                        self.logger.warning("A stock index_weight sync failed for %s %s~%s: %s", index_code, chunk_start, chunk_end, exc)
                        errors.append({"index_code": index_code, "start_date": chunk_start.isoformat(), "end_date": chunk_end.isoformat(), "error": str(exc)})
                    completed += 1
                    if completed == 1 or completed == total_jobs or completed % 5 == 0:
                        report_progress(index_code, chunk_start, chunk_end)

        return {
            "index_count": len(index_items),
            "jobs": total_jobs,
            "saved_rows": saved_rows,
            "errors": errors,
            "start_date": min(job[1] for job in jobs).isoformat(),
            "end_date": end_value.isoformat(),
        }

    def sync_option_basic(self) -> int:
        frames = []
        for exchange in A_STOCK_OPTION_DAILY_SYNC_EXCHANGES:
            frame = self.tushare.get_option_basic_frame(exchange)
            if not frame.empty:
                frames.append(frame)
        if not frames:
            return 0
        combined = pd.concat(frames, ignore_index=True)
        return _bulk_upsert_option_basic_frame(self.analytics_db, combined)

    def sync_option_and_repo_daily(
        self,
        start_date: date,
        end_date: date,
        repo_start_date: Optional[date] = None,
    ) -> Dict:
        option_start_value = _parse_date(start_date)
        repo_start_value = _parse_date(repo_start_date) or option_start_value
        end_value = _parse_date(end_date)
        if not option_start_value or not repo_start_value or not end_value:
            return {
                "start_date": option_start_value.isoformat() if option_start_value else None,
                "repo_start_date": repo_start_value.isoformat() if repo_start_value else None,
                "end_date": end_value.isoformat() if end_value else None,
                "trading_days": 0,
                "option_trading_days": 0,
                "repo_trading_days": 0,
                "option_refresh_dates": 0,
                "repo_refresh_dates": 0,
                "option_saved_rows": 0,
                "repo_saved_rows": 0,
            }
        if option_start_value > end_value and repo_start_value > end_value:
            return {
                "start_date": option_start_value.isoformat(),
                "repo_start_date": repo_start_value.isoformat(),
                "end_date": end_value.isoformat() if end_value else None,
                "trading_days": 0,
                "option_trading_days": 0,
                "repo_trading_days": 0,
                "option_refresh_dates": 0,
                "repo_refresh_dates": 0,
                "option_saved_rows": 0,
                "repo_saved_rows": 0,
            }

        calendar_start = min(option_start_value, repo_start_value)
        calendar = self.tushare.get_trade_calendar_frame(calendar_start, end_value)
        trading_dates = [
            item
            for item in calendar[calendar["is_open"] == 1]["cal_date"].tolist()
            if item <= end_value
        ] if not calendar.empty else []

        option_trading_dates = [item for item in trading_dates if option_start_value <= item <= end_value]
        repo_trading_dates = [item for item in trading_dates if repo_start_value <= item <= end_value]

        option_refresh_dates = self._option_daily_dates_needing_refresh(option_trading_dates)
        repo_refresh_dates = self._repo_daily_dates_needing_refresh(repo_trading_dates)

        if option_refresh_dates:
            self._progress(
                (
                    f"A股期权日行情待补 {len(option_refresh_dates)} 个交易日 "
                    f"{min(option_refresh_dates).isoformat()} ~ {max(option_refresh_dates).isoformat()}"
                ),
                72,
                refresh_dates=len(option_refresh_dates),
                total_dates=len(option_trading_dates),
            )
        else:
            self._progress(
                "A股期权日行情缓存已就绪",
                74,
                processed_dates=len(option_trading_dates),
                total_dates=len(option_trading_dates),
            )
        if repo_refresh_dates:
            self._progress(
                (
                    f"A股回购日行情待补 {len(repo_refresh_dates)} 个交易日 "
                    f"{min(repo_refresh_dates).isoformat()} ~ {max(repo_refresh_dates).isoformat()}"
                ),
                76,
                refresh_dates=len(repo_refresh_dates),
                total_dates=len(repo_trading_dates),
            )
        else:
            self._progress(
                "A股回购日行情缓存已就绪",
                78,
                processed_dates=len(repo_trading_dates),
                total_dates=len(repo_trading_dates),
            )

        option_result = self._sync_option_daily_ranges(option_refresh_dates)
        repo_result = self._sync_repo_daily_dates(repo_refresh_dates)

        return {
            "start_date": option_start_value.isoformat(),
            "repo_start_date": repo_start_value.isoformat(),
            "end_date": end_value.isoformat(),
            "trading_days": len(set(option_trading_dates + repo_trading_dates)),
            "option_trading_days": len(option_trading_dates),
            "repo_trading_days": len(repo_trading_dates),
            "option_refresh_dates": len(option_refresh_dates),
            "repo_refresh_dates": len(repo_refresh_dates),
            "option_saved_rows": option_result.get("saved_rows", 0),
            "repo_saved_rows": repo_result.get("saved_rows", 0),
            "option_chunks": option_result.get("chunks", 0),
            "repo_chunks": repo_result.get("chunks", 0),
            "option_errors": len(option_result.get("errors") or []),
            "repo_errors": len(repo_result.get("errors") or []),
        }

    def _option_daily_dates_needing_refresh(self, trading_dates: List[date]) -> List[date]:
        if not trading_dates:
            return []
        stats_by_date = self._existing_option_day_stats(min(trading_dates), max(trading_dates))
        return [item for item in trading_dates if _option_day_needs_refresh(stats_by_date.get(item))]

    def _repo_daily_dates_needing_refresh(self, trading_dates: List[date]) -> List[date]:
        if not trading_dates:
            return []
        stats_by_date = self._existing_repo_day_stats(min(trading_dates), max(trading_dates))
        return [item for item in trading_dates if _repo_day_needs_refresh(stats_by_date.get(item))]

    def _sync_option_daily_ranges(self, trading_dates: List[date]) -> Dict:
        chunks = _date_chunks_by_span(
            trading_dates,
            A_STOCK_OPTION_DAILY_CHUNK_TRADING_DAYS,
            A_STOCK_OPTION_DAILY_CHUNK_CALENDAR_DAYS,
        )
        if not chunks:
            return {"chunks": 0, "saved_rows": 0, "errors": []}

        def fetch_chunk(chunk: List[date]) -> Tuple[date, date, pd.DataFrame]:
            chunk_start = min(chunk)
            chunk_end = max(chunk)
            frames = []
            for exchange in A_STOCK_OPTION_DAILY_SYNC_EXCHANGES:
                frame = self.tushare.get_option_daily_range_frame(
                    chunk_start,
                    chunk_end,
                    exchange,
                    raise_on_error=True,
                )
                if not frame.empty:
                    frames.append(frame)
            combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
            return chunk_start, chunk_end, combined

        def save_chunk(chunk_start: date, chunk_end: date, frame: pd.DataFrame) -> int:
            if frame.empty:
                return 0
            return _bulk_replace_option_daily_frame(self.analytics_db, frame)

        saved_rows = 0
        errors: List[Dict[str, str]] = []
        processed_chunks = 0
        total_chunks = len(chunks)
        workers = min(SYNC_WORKERS, total_chunks)
        refresh_start = min(trading_dates)
        refresh_end = max(trading_dates)

        def report_progress(chunk_start: date, chunk_end: date):
            self._progress(
                (
                    f"批量同步A股期权日行情 {processed_chunks}/{total_chunks}，"
                    f"待补范围 {refresh_start.isoformat()} ~ {refresh_end.isoformat()}，"
                    f"最近完成 {chunk_start.isoformat()} ~ {chunk_end.isoformat()}"
                ),
                72 + int(processed_chunks / max(total_chunks, 1) * 4),
                processed_chunks=processed_chunks,
                total_chunks=total_chunks,
                option_saved_rows=saved_rows,
                option_errors=len(errors),
            )

        if workers <= 1:
            for chunk in chunks:
                chunk_start, chunk_end = min(chunk), max(chunk)
                try:
                    chunk_start, chunk_end, frame = fetch_chunk(chunk)
                    saved_rows += save_chunk(chunk_start, chunk_end, frame)
                except Exception as exc:
                    self.logger.warning("A stock option daily sync failed for %s~%s: %s", chunk_start, chunk_end, exc)
                    errors.append({"start_date": chunk_start.isoformat(), "end_date": chunk_end.isoformat(), "error": str(exc)})
                processed_chunks += 1
                if processed_chunks == 1 or processed_chunks == total_chunks or processed_chunks % 5 == 0:
                    report_progress(chunk_start, chunk_end)
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(fetch_chunk, chunk): chunk for chunk in chunks}
                for future in as_completed(futures):
                    chunk = futures[future]
                    chunk_start, chunk_end = min(chunk), max(chunk)
                    try:
                        chunk_start, chunk_end, frame = future.result()
                        saved_rows += save_chunk(chunk_start, chunk_end, frame)
                    except Exception as exc:
                        self.logger.warning("A stock option daily sync failed for %s~%s: %s", chunk_start, chunk_end, exc)
                        errors.append({"start_date": chunk_start.isoformat(), "end_date": chunk_end.isoformat(), "error": str(exc)})
                    processed_chunks += 1
                    if processed_chunks == 1 or processed_chunks == total_chunks or processed_chunks % 5 == 0:
                        report_progress(chunk_start, chunk_end)

        return {"chunks": total_chunks, "saved_rows": saved_rows, "errors": errors}

    def _sync_repo_daily_dates(self, trading_dates: List[date]) -> Dict:
        chunks = _date_chunks_by_span(
            trading_dates,
            A_STOCK_REPO_DAILY_CHUNK_TRADING_DAYS,
            A_STOCK_REPO_DAILY_CHUNK_CALENDAR_DAYS,
        )
        if not trading_dates:
            return {"chunks": 0, "saved_rows": 0, "errors": []}

        def fetch_chunk(chunk: List[date]) -> Tuple[date, date, pd.DataFrame]:
            chunk_start = min(chunk)
            chunk_end = max(chunk)
            frame = self.tushare.get_repo_daily_range_frame(
                chunk_start,
                chunk_end,
                raise_on_error=True,
            )
            return chunk_start, chunk_end, frame

        saved_rows = 0
        errors: List[Dict[str, str]] = []
        processed_chunks = 0
        total_chunks = len(chunks)
        workers = min(SYNC_WORKERS, total_chunks)
        refresh_start = min(trading_dates)
        refresh_end = max(trading_dates)

        def save_chunk(chunk_start: date, chunk_end: date, frame: pd.DataFrame) -> int:
            if frame.empty:
                return 0
            return _bulk_replace_repo_daily_frame(self.analytics_db, frame)

        def report_progress(chunk_start: date, chunk_end: date):
            self._progress(
                (
                    f"批量同步A股回购日行情 {processed_chunks}/{total_chunks}，"
                    f"待补范围 {refresh_start.isoformat()} ~ {refresh_end.isoformat()}，"
                    f"最近完成 {chunk_start.isoformat()} ~ {chunk_end.isoformat()}"
                ),
                76 + int(processed_chunks / max(total_chunks, 1) * 2),
                processed_chunks=processed_chunks,
                total_chunks=total_chunks,
                repo_saved_rows=saved_rows,
                repo_errors=len(errors),
            )

        if workers <= 1:
            for chunk in chunks:
                chunk_start, chunk_end = min(chunk), max(chunk)
                try:
                    chunk_start, chunk_end, frame = fetch_chunk(chunk)
                    saved_rows += save_chunk(chunk_start, chunk_end, frame)
                except Exception as exc:
                    self.logger.warning("A stock repo daily sync failed for %s~%s: %s", chunk_start, chunk_end, exc)
                    errors.append({"start_date": chunk_start.isoformat(), "end_date": chunk_end.isoformat(), "error": str(exc)})
                processed_chunks += 1
                if processed_chunks == 1 or processed_chunks == total_chunks or processed_chunks % 5 == 0:
                    report_progress(chunk_start, chunk_end)
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(fetch_chunk, chunk): chunk for chunk in chunks}
                for future in as_completed(futures):
                    chunk = futures[future]
                    chunk_start, chunk_end = min(chunk), max(chunk)
                    try:
                        chunk_start, chunk_end, frame = future.result()
                        saved_rows += save_chunk(chunk_start, chunk_end, frame)
                    except Exception as exc:
                        self.logger.warning("A stock repo daily sync failed for %s~%s: %s", chunk_start, chunk_end, exc)
                        errors.append({"start_date": chunk_start.isoformat(), "end_date": chunk_end.isoformat(), "error": str(exc)})
                    processed_chunks += 1
                    if processed_chunks == 1 or processed_chunks == total_chunks or processed_chunks % 5 == 0:
                        report_progress(chunk_start, chunk_end)

        return {"chunks": total_chunks, "saved_rows": saved_rows, "errors": errors}

    def sync_chinabond_curve_defs(self) -> int:
        now = datetime.now()
        frame = pd.DataFrame(
            [
                {
                    "curve_id": item["curve_id"],
                    "curve_name": item["curve_name"],
                    "category": item["category"],
                    "rating": item["rating"],
                    "pair_key": item["pair_key"],
                    "updated_at": now,
                }
                for item in CHINABOND_CREDIT_CURVES
            ]
        )
        if frame.empty:
            return 0
        columns = ["curve_id", "curve_name", "category", "rating", "pair_key", "updated_at"]
        self.analytics_db.commit()
        _insert_or_replace_analytics_frame(
            AStockChinaBondYieldCurveDef.__tablename__,
            columns,
            frame.loc[:, columns],
        )
        return len(frame)

    def sync_chinabond_yield_curves(self, start_date: date, end_date: date) -> Dict:
        start_value = _parse_date(start_date)
        end_value = _parse_date(end_date)
        curve_ids = [item["curve_id"] for item in CHINABOND_CREDIT_CURVES]
        if not start_value or not end_value or start_value > end_value or not curve_ids:
            return {
                "start_date": start_value.isoformat() if start_value else None,
                "end_date": end_value.isoformat() if end_value else None,
                "trading_days": 0,
                "saved_rows": 0,
                "errors": [],
            }

        calendar = self.tushare.get_trade_calendar_frame(start_value, end_value)
        trading_dates = [
            item
            for item in calendar[calendar["is_open"] == 1]["cal_date"].tolist()
            if item <= end_value
        ] if not calendar.empty else []
        refresh_dates = self._chinabond_yield_curve_dates_needing_refresh(trading_dates)
        if refresh_dates:
            self._progress(
                (
                    f"中债信用曲线待补 {len(refresh_dates)} 个交易日 "
                    f"{min(refresh_dates).isoformat()} ~ {max(refresh_dates).isoformat()}"
                ),
                79,
                refresh_dates=len(refresh_dates),
                total_dates=len(trading_dates),
            )
        else:
            self._progress(
                "中债信用曲线缓存已就绪",
                82,
                processed_dates=len(trading_dates),
                total_dates=len(trading_dates),
            )
            return {
                "start_date": start_value.isoformat(),
                "end_date": end_value.isoformat(),
                "trading_days": len(trading_dates),
                "refresh_dates": 0,
                "chunks": 0,
                "saved_rows": 0,
                "errors": [],
            }

        result = self._sync_chinabond_yield_curve_dates(refresh_dates, curve_ids)
        return {
            "start_date": start_value.isoformat(),
            "end_date": end_value.isoformat(),
            "trading_days": len(trading_dates),
            "refresh_dates": len(refresh_dates),
            "chunks": result.get("chunks", 0),
            "saved_rows": result.get("saved_rows", 0),
            "errors": result.get("errors") or [],
        }

    def _chinabond_yield_curve_dates_needing_refresh(self, trading_dates: List[date]) -> List[date]:
        if not trading_dates:
            return []
        stats_by_date = self._existing_chinabond_day_stats(min(trading_dates), max(trading_dates))
        return [item for item in trading_dates if _chinabond_day_needs_refresh(stats_by_date.get(item))]

    def _sync_chinabond_yield_curve_dates(self, trading_dates: List[date], curve_ids: List[str]) -> Dict:
        chunks = _date_chunks_by_span(
            trading_dates,
            A_STOCK_CHINABOND_CHUNK_TRADING_DAYS,
            A_STOCK_CHINABOND_CHUNK_CALENDAR_DAYS,
        )
        if not chunks:
            return {"chunks": 0, "saved_rows": 0, "errors": []}

        def fetch_chunk(chunk: List[date]) -> Tuple[date, date, pd.DataFrame]:
            chunk_start = min(chunk)
            chunk_end = max(chunk)
            chinabond = ChinaBondYieldCurveService(timeout=self.chinabond.timeout)
            frame = chinabond.get_yield_curve_dates_frame(chunk, curve_ids)
            return chunk_start, chunk_end, frame

        saved_rows = 0
        errors: List[Dict[str, str]] = []
        processed_chunks = 0
        total_chunks = len(chunks)
        workers = min(SYNC_WORKERS, total_chunks)
        refresh_start = min(trading_dates)
        refresh_end = max(trading_dates)

        def save_chunk(chunk_start: date, chunk_end: date, frame: pd.DataFrame) -> int:
            if frame.empty:
                return 0
            return _bulk_replace_chinabond_yield_curve_frame(self.analytics_db, frame)

        def report_progress(chunk_start: date, chunk_end: date):
            self._progress(
                (
                    f"批量同步中债信用曲线 {processed_chunks}/{total_chunks}，"
                    f"待补范围 {refresh_start.isoformat()} ~ {refresh_end.isoformat()}，"
                    f"最近完成 {chunk_start.isoformat()} ~ {chunk_end.isoformat()}"
                ),
                79 + int(processed_chunks / max(total_chunks, 1) * 3),
                processed_chunks=processed_chunks,
                total_chunks=total_chunks,
                saved_rows=saved_rows,
                errors=len(errors),
            )

        if workers <= 1:
            for chunk in chunks:
                chunk_start, chunk_end = min(chunk), max(chunk)
                try:
                    chunk_start, chunk_end, frame = fetch_chunk(chunk)
                    saved_rows += save_chunk(chunk_start, chunk_end, frame)
                except Exception as exc:
                    self.logger.warning("ChinaBond yield curve sync failed for %s~%s: %s", chunk_start, chunk_end, exc)
                    errors.append({"start_date": chunk_start.isoformat(), "end_date": chunk_end.isoformat(), "error": str(exc)})
                processed_chunks += 1
                if processed_chunks == 1 or processed_chunks == total_chunks or processed_chunks % 5 == 0:
                    report_progress(chunk_start, chunk_end)
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(fetch_chunk, chunk): chunk for chunk in chunks}
                for future in as_completed(futures):
                    chunk = futures[future]
                    chunk_start, chunk_end = min(chunk), max(chunk)
                    try:
                        chunk_start, chunk_end, frame = future.result()
                        saved_rows += save_chunk(chunk_start, chunk_end, frame)
                    except Exception as exc:
                        self.logger.warning("ChinaBond yield curve sync failed for %s~%s: %s", chunk_start, chunk_end, exc)
                        errors.append({"start_date": chunk_start.isoformat(), "end_date": chunk_end.isoformat(), "error": str(exc)})
                    processed_chunks += 1
                    if processed_chunks == 1 or processed_chunks == total_chunks or processed_chunks % 5 == 0:
                        report_progress(chunk_start, chunk_end)

        return {"chunks": total_chunks, "saved_rows": saved_rows, "errors": errors}

    def sync_base_data(
        self,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        incremental: bool = True,
    ) -> Dict:
        end_value = _parse_date(end_date) or date.today()
        explicit_start = _parse_date(start_date)
        if explicit_start and explicit_start > end_value:
            raise ValueError("开始日期不能晚于结束日期")

        latest_market_date = _latest_analytics_date(self.analytics_db, AStockMarketDaily, "trade_date")
        latest_option_date = _latest_analytics_date(self.analytics_db, AStockOptionDaily, "trade_date")
        latest_repo_date = _latest_analytics_date(self.analytics_db, AStockRepoDaily, "trade_date")
        latest_chinabond_date = _latest_analytics_date(self.analytics_db, AStockChinaBondYieldCurveDaily, "trade_date")
        name_change_rows_before = _count_analytics_table_rows(self.analytics_db, AStockNameChange.__tablename__)
        self.analytics_db.commit()

        reference_full_refresh = not incremental or name_change_rows_before <= 0
        reference_start = explicit_start or max(DEFAULT_START_DATE, end_value - timedelta(days=90))
        if reference_full_refresh and not explicit_start:
            self.sync_reference_data(DEFAULT_START_DATE, end_value)
        else:
            self.sync_reference_data_incremental(reference_start, end_value)

        market_warmup_days = max(RAW_FETCH_LOOKBACK_DAYS, A_STOCK_MARKET_DAILY_WARMUP_DAYS)
        market_default_start = _warmup_start(DEFAULT_START_DATE, market_warmup_days)
        if explicit_start:
            market_start = _warmup_start(explicit_start, market_warmup_days)
        elif incremental:
            market_start = _overlap_start(market_default_start, latest_market_date)
        else:
            market_start = market_default_start

        self._progress("同步A股全市场日行情缓存", 28, start_date=market_start.isoformat(), end_date=end_value.isoformat())
        trade_calendar = self.tushare.get_trade_calendar_frame(market_start, end_value)
        trading_dates = [
            item
            for item in trade_calendar[trade_calendar["is_open"] == 1]["cal_date"].tolist()
            if item <= end_value
        ] if not trade_calendar.empty else []
        if trading_dates:
            self._ensure_market_days(trading_dates)

        index_default_start = _warmup_start(DEFAULT_START_DATE, A_STOCK_INDEX_DAILY_WARMUP_DAYS)
        index_display_start = _warmup_start(explicit_start, A_STOCK_INDEX_DAILY_WARMUP_DAYS) if explicit_start else index_default_start
        self._progress("同步A股指数日行情缓存", 62, start_date=index_display_start.isoformat(), end_date=end_value.isoformat())
        index_daily_result = self.sync_index_daily(
            index_default_start,
            end_value,
            incremental=incremental,
            explicit_start=explicit_start,
        )

        fund_default_start = _warmup_start(DEFAULT_START_DATE, A_STOCK_FUND_DAILY_WARMUP_DAYS)
        fund_display_start = _warmup_start(explicit_start, A_STOCK_FUND_DAILY_WARMUP_DAYS) if explicit_start else fund_default_start
        self._progress(
            "同步A股ETF日行情缓存",
            64,
            start_date=fund_display_start.isoformat(),
            end_date=end_value.isoformat(),
        )
        fund_daily_result = self.sync_fund_daily(
            fund_default_start,
            end_value,
            incremental=incremental,
            explicit_start=explicit_start,
        )

        index_weight_default_start = _warmup_start(DEFAULT_START_DATE, A_STOCK_INDEX_WEIGHT_WARMUP_DAYS)
        if explicit_start:
            index_weight_start = _warmup_start(explicit_start, A_STOCK_INDEX_WEIGHT_WARMUP_DAYS)
        elif incremental:
            index_weight_start = index_weight_default_start
        else:
            index_weight_start = index_weight_default_start
        self._progress(
            "同步A股指数成分权重缓存",
            66,
            start_date=index_weight_start.isoformat(),
            end_date=end_value.isoformat(),
        )
        index_weight_result = self.sync_index_weight(
            index_weight_start,
            end_value,
            incremental=incremental,
            explicit_start=explicit_start,
        )

        option_default_start = _warmup_start(DEFAULT_START_DATE, A_STOCK_OPTION_DAILY_WARMUP_DAYS)
        repo_default_start = _warmup_start(DEFAULT_START_DATE, A_STOCK_REPO_DAILY_WARMUP_DAYS)
        if explicit_start:
            option_start = _warmup_start(explicit_start, A_STOCK_OPTION_DAILY_WARMUP_DAYS)
            repo_start = _warmup_start(explicit_start, A_STOCK_REPO_DAILY_WARMUP_DAYS)
        elif incremental:
            option_start = _overlap_start(option_default_start, latest_option_date)
            repo_start = _overlap_start(repo_default_start, latest_repo_date)
        else:
            option_start = option_default_start
            repo_start = repo_default_start

        self._progress("同步A股期权合约基础信息", 68)
        option_basic_rows_saved = self.sync_option_basic()

        self._progress(
            "同步A股期权/回购日行情缓存",
            72,
            start_date=option_start.isoformat(),
            repo_start_date=repo_start.isoformat(),
            end_date=end_value.isoformat(),
        )
        option_repo_result = self.sync_option_and_repo_daily(
            option_start,
            end_value,
            repo_start_date=repo_start,
        )

        chinabond_default_start = _warmup_start(DEFAULT_START_DATE, A_STOCK_CHINABOND_WARMUP_DAYS)
        if explicit_start:
            chinabond_start = _warmup_start(explicit_start, A_STOCK_CHINABOND_WARMUP_DAYS)
        elif incremental:
            chinabond_start = _overlap_start(chinabond_default_start, latest_chinabond_date)
        else:
            chinabond_start = chinabond_default_start

        self._progress("同步中债信用曲线定义", 78)
        chinabond_curve_defs_saved = self.sync_chinabond_curve_defs()

        self._progress(
            "同步中债信用收益率曲线",
            79,
            start_date=chinabond_start.isoformat(),
            end_date=end_value.isoformat(),
        )
        chinabond_result = self.sync_chinabond_yield_curves(chinabond_start, end_value)

        requested_income_start = explicit_start - timedelta(days=INCOME_HISTORY_LOOKBACK_DAYS) if explicit_start else None
        income_start = requested_income_start
        income_end = end_value
        income_incremental = incremental and not requested_income_start
        income_sync_mode = "per_symbol" if requested_income_start else ("incremental" if incremental else "full")
        income_symbols = _load_income_symbols(self.analytics_db)
        income_symbol_scope = "a_stock_basic"
        self._progress(
            "同步A股利润表财务数据缓存",
            82,
            start_date=income_start.isoformat() if income_start else None,
            end_date=income_end.isoformat(),
            lookback_days=INCOME_HISTORY_LOOKBACK_DAYS,
            mode=income_sync_mode,
            requested_start_date=requested_income_start.isoformat() if requested_income_start else None,
            symbol_scope=income_symbol_scope,
            symbols=len(income_symbols),
        )
        income_result = sync_a_stock_income_data(
            start_date=income_start,
            end_date=income_end,
            incremental=income_incremental,
            symbols=income_symbols,
            symbol_scope=income_symbol_scope,
            tushare_service=self.tushare,
            analytics_db=self.analytics_db,
            progress_callback=self.progress_callback,
        )

        basic_rows = _count_analytics_table_rows(self.analytics_db, AStockBasic.__tablename__)
        name_change_rows = _count_analytics_table_rows(self.analytics_db, AStockNameChange.__tablename__)
        market_rows = _count_analytics_table_rows(self.analytics_db, AStockMarketDaily.__tablename__)
        fund_daily_rows = _count_analytics_table_rows(self.analytics_db, AStockFundDaily.__tablename__)
        index_rows = _count_analytics_table_rows(self.analytics_db, AStockIndexDaily.__tablename__)
        index_weight_rows = _count_analytics_table_rows(self.analytics_db, AStockIndexWeight.__tablename__)
        income_rows = _count_analytics_table_rows(self.analytics_db, AStockIncome.__tablename__)
        option_basic_rows = _count_analytics_table_rows(self.analytics_db, AStockOptionBasic.__tablename__)
        option_daily_rows = _count_analytics_table_rows(self.analytics_db, AStockOptionDaily.__tablename__)
        repo_daily_rows = _count_analytics_table_rows(self.analytics_db, AStockRepoDaily.__tablename__)
        chinabond_curve_def_rows = _count_analytics_table_rows(self.analytics_db, AStockChinaBondYieldCurveDef.__tablename__)
        chinabond_curve_daily_rows = _count_analytics_table_rows(self.analytics_db, AStockChinaBondYieldCurveDaily.__tablename__)
        self.analytics_db.commit()

        self._progress("A股基础数据同步完成", 100)
        return {
            "status": "completed",
            "mode": "incremental" if incremental else "full",
            "start_date": explicit_start.isoformat() if explicit_start else None,
            "end_date": end_value.isoformat(),
            "warmup_days": {
                "market_daily": market_warmup_days,
                "index_daily": A_STOCK_INDEX_DAILY_WARMUP_DAYS,
                "fund_daily": A_STOCK_FUND_DAILY_WARMUP_DAYS,
                "index_weight": A_STOCK_INDEX_WEIGHT_WARMUP_DAYS,
                "option_daily": A_STOCK_OPTION_DAILY_WARMUP_DAYS,
                "repo_daily": A_STOCK_REPO_DAILY_WARMUP_DAYS,
                "chinabond": A_STOCK_CHINABOND_WARMUP_DAYS,
                "income": INCOME_HISTORY_LOOKBACK_DAYS,
            },
            "reference_full_refresh": reference_full_refresh,
            "market_start_date": market_start.isoformat(),
            "market_trade_days": len(trading_dates),
            "index_start_date": index_daily_result.get("start_date") or index_display_start.isoformat(),
            "index_end_date": index_daily_result.get("end_date"),
            "index_daily_index_count": index_daily_result.get("index_count"),
            "index_daily_jobs": index_daily_result.get("jobs"),
            "index_daily_saved_rows": index_daily_result.get("saved_rows"),
            "index_daily_errors": len(index_daily_result.get("errors") or []),
            "fund_daily_start_date": fund_daily_result.get("start_date") or fund_display_start.isoformat(),
            "fund_daily_end_date": fund_daily_result.get("end_date"),
            "fund_daily_symbol_count": fund_daily_result.get("symbol_count"),
            "fund_daily_jobs": fund_daily_result.get("jobs"),
            "fund_daily_saved_rows": fund_daily_result.get("saved_rows"),
            "fund_daily_errors": len(fund_daily_result.get("errors") or []),
            "index_weight_start_date": index_weight_result.get("start_date") or index_weight_start.isoformat(),
            "index_weight_end_date": index_weight_result.get("end_date"),
            "index_weight_index_count": index_weight_result.get("index_count"),
            "index_weight_jobs": index_weight_result.get("jobs"),
            "index_weight_saved_rows": index_weight_result.get("saved_rows"),
            "index_weight_errors": len(index_weight_result.get("errors") or []),
            "option_start_date": option_start.isoformat(),
            "repo_start_date": repo_start.isoformat(),
            "option_end_date": end_value.isoformat(),
            "option_basic_rows_saved": option_basic_rows_saved,
            "option_daily_saved_rows": option_repo_result.get("option_saved_rows"),
            "repo_daily_saved_rows": option_repo_result.get("repo_saved_rows"),
            "option_repo_trading_days": option_repo_result.get("trading_days"),
            "option_daily_trading_days": option_repo_result.get("option_trading_days"),
            "repo_daily_trading_days": option_repo_result.get("repo_trading_days"),
            "option_daily_refresh_dates": option_repo_result.get("option_refresh_dates"),
            "repo_daily_refresh_dates": option_repo_result.get("repo_refresh_dates"),
            "option_daily_chunks": option_repo_result.get("option_chunks"),
            "repo_daily_chunks": option_repo_result.get("repo_chunks"),
            "option_daily_errors": option_repo_result.get("option_errors"),
            "repo_daily_errors": option_repo_result.get("repo_errors"),
            "chinabond_start_date": chinabond_start.isoformat(),
            "chinabond_end_date": end_value.isoformat(),
            "chinabond_curve_defs_saved": chinabond_curve_defs_saved,
            "chinabond_curve_daily_saved_rows": chinabond_result.get("saved_rows"),
            "chinabond_curve_trading_days": chinabond_result.get("trading_days"),
            "chinabond_curve_refresh_dates": chinabond_result.get("refresh_dates"),
            "chinabond_curve_chunks": chinabond_result.get("chunks"),
            "chinabond_curve_errors": len(chinabond_result.get("errors") or []),
            "income_start_date": income_result.get("start_date"),
            "income_end_date": income_result.get("end_date"),
            "income_sync_mode": income_sync_mode,
            "income_symbol_scope": income_symbol_scope,
            "income_symbols": income_result.get("symbols"),
            "income_requested_start_date": requested_income_start.isoformat() if requested_income_start else None,
            "income_earliest_ann_date_before": income_result.get("earliest_ann_date"),
            "income_latest_ann_date_before": income_result.get("latest_ann_date"),
            "income_fetched_rows": income_result.get("fetched_rows"),
            "income_saved_rows": income_result.get("saved_rows"),
            "income_empty_symbols": income_result.get("empty_symbols"),
            "income_non_empty_symbols": income_result.get("non_empty_symbols"),
            "income_skipped_symbols": income_result.get("skipped_symbols"),
            "income_backfill_symbols": income_result.get("backfill_symbols"),
            "income_incremental_symbols": income_result.get("incremental_symbols"),
            "income_full_symbols": income_result.get("full_symbols"),
            "income_fetch_seconds": income_result.get("fetch_seconds"),
            "income_insert_seconds": income_result.get("insert_seconds"),
            "income_total_seconds": income_result.get("total_seconds"),
            "income_avg_fetch_ms": income_result.get("avg_fetch_ms"),
            "income_insert_batches": income_result.get("insert_batches"),
            "tables": {
                AStockBasic.__tablename__: basic_rows,
                AStockIncome.__tablename__: income_rows,
                AStockFundDaily.__tablename__: fund_daily_rows,
                AStockIndexDaily.__tablename__: index_rows,
                AStockIndexWeight.__tablename__: index_weight_rows,
                AStockMarketDaily.__tablename__: market_rows,
                AStockNameChange.__tablename__: name_change_rows,
                AStockOptionBasic.__tablename__: option_basic_rows,
                AStockOptionDaily.__tablename__: option_daily_rows,
                AStockRepoDaily.__tablename__: repo_daily_rows,
                AStockChinaBondYieldCurveDef.__tablename__: chinabond_curve_def_rows,
                AStockChinaBondYieldCurveDaily.__tablename__: chinabond_curve_daily_rows,
            },
        }


def _normalize_income_frame(frame: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "id",
        "ts_code",
        "end_date",
        "ann_date",
        "operate_income",
        "rd_exp",
        "report_type",
        "created_at",
        "updated_at",
    ]
    if frame.empty:
        return pd.DataFrame(columns=columns)

    now = datetime.now()
    normalized = pd.DataFrame(index=frame.index)
    normalized["ts_code"] = _clean_text_series(frame, "ts_code")
    normalized["end_date"] = _date_series(frame, "end_date")
    normalized["ann_date"] = _date_series(frame, "ann_date")
    normalized["operate_income"] = _numeric_series(frame, "operate_income", 4)
    normalized["rd_exp"] = _numeric_series(frame, "rd_exp", 4)
    normalized["report_type"] = _clean_text_series(frame, "report_type").fillna("")
    normalized = normalized.dropna(subset=["ts_code", "end_date"])
    if normalized.empty:
        return pd.DataFrame(columns=columns)

    key_columns = ["ts_code", "end_date", "ann_date", "report_type"]
    normalized["id"] = normalized[key_columns].apply(
        lambda row: hashlib.sha1(
            "|".join("" if pd.isna(value) else str(value) for value in row).encode("utf-8")
        ).hexdigest(),
        axis=1,
    )
    normalized["created_at"] = now
    normalized["updated_at"] = now
    normalized = normalized.drop_duplicates(subset=["id"], keep="last")
    return normalized.loc[:, columns]


def _bulk_upsert_income_frame(analytics_db: Session, frame: pd.DataFrame) -> int:
    if frame.empty:
        return 0
    normalized = _normalize_income_frame(frame)
    if normalized.empty:
        return 0

    columns = [
        "id",
        "ts_code",
        "end_date",
        "ann_date",
        "operate_income",
        "rd_exp",
        "report_type",
        "created_at",
        "updated_at",
    ]
    table = AStockIncome.__table__
    analytics_db.commit()
    # Keep this on the same DuckDB bulk path used by market/index daily:
    # register one DataFrame and let DuckDB execute a set-based INSERT OR REPLACE.
    _insert_or_replace_analytics_frame(
        table.name,
        columns,
        normalized.loc[:, columns],
    )
    return len(normalized)


def _normalize_option_basic_frame(frame: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "ts_code",
        "exchange",
        "name",
        "per_unit",
        "opt_code",
        "opt_type",
        "call_put",
        "exercise_type",
        "exercise_price",
        "s_month",
        "maturity_date",
        "list_price",
        "list_date",
        "delist_date",
        "last_edate",
        "last_ddate",
        "quote_unit",
        "min_price_chg",
        "updated_at",
    ]
    if frame.empty:
        return pd.DataFrame(columns=columns)

    now = datetime.now()
    normalized = pd.DataFrame(index=frame.index)
    normalized["ts_code"] = _clean_text_series(frame, "ts_code")
    normalized["exchange"] = _clean_text_series(frame, "exchange")
    normalized["name"] = _clean_text_series(frame, "name")
    normalized["per_unit"] = _numeric_series(frame, "per_unit", 6)
    normalized["opt_code"] = _clean_text_series(frame, "opt_code")
    normalized["opt_type"] = _clean_text_series(frame, "opt_type")
    normalized["call_put"] = _clean_text_series(frame, "call_put")
    normalized["exercise_type"] = _clean_text_series(frame, "exercise_type")
    normalized["exercise_price"] = _numeric_series(frame, "exercise_price", 6)
    normalized["s_month"] = _clean_text_series(frame, "s_month")
    normalized["maturity_date"] = _date_series(frame, "maturity_date")
    normalized["list_price"] = _numeric_series(frame, "list_price", 6)
    normalized["list_date"] = _date_series(frame, "list_date")
    normalized["delist_date"] = _date_series(frame, "delist_date")
    normalized["last_edate"] = _date_series(frame, "last_edate")
    normalized["last_ddate"] = _date_series(frame, "last_ddate")
    normalized["quote_unit"] = _clean_text_series(frame, "quote_unit")
    normalized["min_price_chg"] = _numeric_series(frame, "min_price_chg", 6)
    normalized["updated_at"] = now
    normalized = normalized.dropna(subset=["ts_code"])
    if normalized.empty:
        return pd.DataFrame(columns=columns)
    normalized = normalized.drop_duplicates(subset=["ts_code"], keep="last")
    return normalized.loc[:, columns]


def _bulk_upsert_option_basic_frame(analytics_db: Session, frame: pd.DataFrame) -> int:
    normalized = _normalize_option_basic_frame(frame)
    if normalized.empty:
        return 0
    columns = list(normalized.columns)
    analytics_db.commit()
    _insert_or_replace_analytics_frame(
        AStockOptionBasic.__tablename__,
        columns,
        normalized.loc[:, columns],
    )
    return len(normalized)


def _normalize_index_weight_frame(frame: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "index_code",
        "trade_date",
        "con_code",
        "weight",
        "created_at",
        "updated_at",
    ]
    if frame.empty:
        return pd.DataFrame(columns=columns)

    now = datetime.now()
    normalized = pd.DataFrame(index=frame.index)
    normalized["index_code"] = _clean_text_series(frame, "index_code")
    normalized["trade_date"] = _date_series(frame, "trade_date")
    normalized["con_code"] = _clean_text_series(frame, "con_code")
    normalized["weight"] = _numeric_series(frame, "weight", 6)
    normalized["created_at"] = now
    normalized["updated_at"] = now
    normalized = normalized.dropna(subset=["index_code", "trade_date", "con_code"])
    if normalized.empty:
        return pd.DataFrame(columns=columns)
    normalized = normalized.drop_duplicates(subset=["index_code", "trade_date", "con_code"], keep="last")
    return normalized.loc[:, columns]


def _bulk_replace_index_weight_frame(analytics_db: Session, frame: pd.DataFrame) -> int:
    normalized = _normalize_index_weight_frame(frame)
    if normalized.empty:
        return 0

    import duckdb  # type: ignore

    columns = list(normalized.columns)
    key_frame = normalized.loc[:, ["index_code", "trade_date"]].drop_duplicates()
    temp_frame_name = "analytics_index_weight_insert_frame"
    temp_keys_name = "analytics_index_weight_replace_keys"
    quoted_table = _quote_duckdb_identifier(AStockIndexWeight.__tablename__)
    quoted_columns = ", ".join(_quote_duckdb_identifier(column) for column in columns)
    analytics_db.commit()
    connection = duckdb.connect(database=ANALYTICS_DB_PATH, read_only=False)
    try:
        connection.execute("BEGIN TRANSACTION")
        connection.register(temp_keys_name, key_frame)
        connection.execute(
            f"""
            DELETE FROM {quoted_table}
            USING {_quote_duckdb_identifier(temp_keys_name)}
            WHERE {quoted_table}.index_code = {_quote_duckdb_identifier(temp_keys_name)}.index_code
              AND {quoted_table}.trade_date = {_quote_duckdb_identifier(temp_keys_name)}.trade_date
            """
        )
        connection.register(temp_frame_name, normalized.loc[:, columns])
        connection.execute(
            f"""
            INSERT INTO {quoted_table} ({quoted_columns})
            SELECT {quoted_columns}
            FROM {_quote_duckdb_identifier(temp_frame_name)}
            """
        )
        connection.execute("COMMIT")
    except Exception:
        try:
            connection.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        connection.close()
    return len(normalized)


def _normalize_option_daily_frame(frame: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "trade_date",
        "ts_code",
        "exchange",
        "pre_settle",
        "pre_close",
        "open",
        "high",
        "low",
        "close",
        "settle",
        "vol",
        "amount",
        "oi",
        "created_at",
        "updated_at",
    ]
    if frame.empty:
        return pd.DataFrame(columns=columns)

    now = datetime.now()
    normalized = pd.DataFrame(index=frame.index)
    normalized["trade_date"] = _date_series(frame, "trade_date")
    normalized["ts_code"] = _clean_text_series(frame, "ts_code")
    normalized["exchange"] = _clean_text_series(frame, "exchange")
    for column in (
        "pre_settle",
        "pre_close",
        "open",
        "high",
        "low",
        "close",
        "settle",
        "vol",
        "amount",
        "oi",
    ):
        normalized[column] = _numeric_series(frame, column, 6)
    normalized["created_at"] = now
    normalized["updated_at"] = now
    normalized = normalized.dropna(subset=["trade_date", "ts_code"])
    if normalized.empty:
        return pd.DataFrame(columns=columns)
    normalized = normalized.drop_duplicates(subset=["trade_date", "ts_code"], keep="last")
    return normalized.loc[:, columns]


def _bulk_replace_option_daily_frame(analytics_db: Session, frame: pd.DataFrame) -> int:
    normalized = _normalize_option_daily_frame(frame)
    if normalized.empty:
        return 0
    columns = list(normalized.columns)
    replace_dates = sorted(normalized["trade_date"].dropna().unique().tolist())
    analytics_db.commit()
    _insert_or_replace_analytics_frame(
        AStockOptionDaily.__tablename__,
        columns,
        normalized.loc[:, columns],
        replace_dates=replace_dates,
    )
    return len(normalized)


def _normalize_repo_daily_frame(frame: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "trade_date",
        "ts_code",
        "repo_maturity",
        "pre_close",
        "open",
        "high",
        "low",
        "close",
        "weight",
        "weight_r",
        "amount",
        "num",
        "created_at",
        "updated_at",
    ]
    if frame.empty:
        return pd.DataFrame(columns=columns)

    now = datetime.now()
    normalized = pd.DataFrame(index=frame.index)
    normalized["trade_date"] = _date_series(frame, "trade_date")
    normalized["ts_code"] = _clean_text_series(frame, "ts_code")
    normalized["repo_maturity"] = _clean_text_series(frame, "repo_maturity")
    for column in ("pre_close", "open", "high", "low", "close", "weight", "weight_r", "amount", "num"):
        normalized[column] = _numeric_series(frame, column, 6)
    normalized["created_at"] = now
    normalized["updated_at"] = now
    normalized = normalized.dropna(subset=["trade_date", "ts_code"])
    if normalized.empty:
        return pd.DataFrame(columns=columns)
    normalized = normalized.drop_duplicates(subset=["trade_date", "ts_code"], keep="last")
    return normalized.loc[:, columns]


def _bulk_replace_repo_daily_frame(analytics_db: Session, frame: pd.DataFrame) -> int:
    normalized = _normalize_repo_daily_frame(frame)
    if normalized.empty:
        return 0
    columns = list(normalized.columns)
    replace_dates = sorted(normalized["trade_date"].dropna().unique().tolist())
    analytics_db.commit()
    _insert_or_replace_analytics_frame(
        AStockRepoDaily.__tablename__,
        columns,
        normalized.loc[:, columns],
        replace_dates=replace_dates,
    )
    return len(normalized)


def _normalize_chinabond_yield_curve_frame(frame: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "trade_date",
        "curve_id",
        "curve_name",
        "term",
        "yield_rate",
        "created_at",
        "updated_at",
    ]
    if frame.empty:
        return pd.DataFrame(columns=columns)

    now = datetime.now()
    normalized = pd.DataFrame(index=frame.index)
    normalized["trade_date"] = _date_series(frame, "trade_date")
    normalized["curve_id"] = _clean_text_series(frame, "curve_id")
    normalized["curve_name"] = _clean_text_series(frame, "curve_name")
    normalized["term"] = _numeric_series(frame, "term", 6)
    normalized["yield_rate"] = _numeric_series(frame, "yield_rate", 6)
    normalized["created_at"] = now
    normalized["updated_at"] = now
    normalized = normalized.dropna(subset=["trade_date", "curve_id", "term"])
    if normalized.empty:
        return pd.DataFrame(columns=columns)
    normalized = normalized.drop_duplicates(subset=["trade_date", "curve_id", "term"], keep="last")
    return normalized.loc[:, columns]


def _bulk_replace_chinabond_yield_curve_frame(analytics_db: Session, frame: pd.DataFrame) -> int:
    normalized = _normalize_chinabond_yield_curve_frame(frame)
    if normalized.empty:
        return 0
    columns = list(normalized.columns)
    replace_dates = sorted(normalized["trade_date"].dropna().unique().tolist())
    analytics_db.commit()
    _insert_or_replace_analytics_frame(
        AStockChinaBondYieldCurveDaily.__tablename__,
        columns,
        normalized.loc[:, columns],
        replace_dates=replace_dates,
    )
    return len(normalized)


def _income_ann_date_bounds(analytics_db: Session) -> Tuple[Optional[date], Optional[date]]:
    row = analytics_db.execute(
        text("""
            SELECT MIN(ann_date), MAX(ann_date)
            FROM a_stock_income
            WHERE ann_date IS NOT NULL
        """)
    ).fetchone()
    if not row:
        return None, None
    return _parse_date(row[0]), _parse_date(row[1])


def _income_ann_date_bounds_by_symbol(analytics_db: Session) -> Dict[str, Tuple[Optional[date], Optional[date]]]:
    rows = analytics_db.execute(
        text("""
            SELECT ts_code, MIN(ann_date), MAX(ann_date)
            FROM a_stock_income
            WHERE ts_code IS NOT NULL AND ann_date IS NOT NULL
            GROUP BY ts_code
        """)
    ).fetchall()
    result = {}
    for row in rows:
        symbol = str(row[0] or "").strip().upper()
        if not symbol:
            continue
        result[symbol] = (_parse_date(row[1]), _parse_date(row[2]))
    return result


def _income_list_dates_by_symbol(analytics_db: Session) -> Dict[str, Optional[date]]:
    rows = (
        analytics_db.query(AStockBasic.ts_code, AStockBasic.list_date)
        .filter(AStockBasic.ts_code.isnot(None))
        .all()
    )
    result: Dict[str, Optional[date]] = {}
    for row in rows:
        symbol = str(row[0] or "").strip().upper()
        if not symbol:
            continue
        result[symbol] = _parse_date(row[1])
    return result


def _load_income_symbols(analytics_db: Session) -> List[str]:
    rows = (
        analytics_db.query(AStockBasic.ts_code)
        .filter(AStockBasic.ts_code.isnot(None))
        .order_by(AStockBasic.ts_code.asc())
        .all()
    )
    symbols = []
    seen = set()
    for row in rows:
        symbol = str(row[0] or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        symbols.append(symbol)
    return symbols


def _filter_income_ann_date_range(frame: pd.DataFrame, start_date: date, end_date: date) -> pd.DataFrame:
    if frame.empty or "ann_date" not in frame.columns:
        return pd.DataFrame()
    working = frame.copy()
    working["ann_date"] = working["ann_date"].map(_parse_date)
    working = working[working["ann_date"].notna()]
    working = working[(working["ann_date"] >= start_date) & (working["ann_date"] <= end_date)]
    return working


def _plan_income_symbol_ranges(
    symbols: List[str],
    start_date: Optional[date],
    end_date: date,
    *,
    incremental: bool,
    symbol_bounds: Dict[str, Tuple[Optional[date], Optional[date]]],
    symbol_list_dates: Optional[Dict[str, Optional[date]]] = None,
) -> Tuple[List[Tuple[str, date, date, str]], Dict[str, int]]:
    default_start = DEFAULT_START_DATE - timedelta(days=INCOME_HISTORY_LOOKBACK_DAYS)
    refresh_cutoff = end_date - timedelta(days=SYNC_REFRESH_OVERLAP_DAYS)
    list_dates = symbol_list_dates or {}
    jobs: List[Tuple[str, date, date, str]] = []
    stats = {
        "skipped": 0,
        "backfill": 0,
        "incremental": 0,
        "full": 0,
    }

    for symbol in symbols:
        earliest_ann_date, latest_ann_date = symbol_bounds.get(symbol, (None, None))
        list_date = list_dates.get(symbol)
        latest_is_fresh = bool(latest_ann_date and latest_ann_date >= refresh_cutoff)
        symbol_start: Optional[date] = None
        symbol_end = end_date
        sync_kind = "incremental"

        if start_date:
            if earliest_ann_date is None:
                symbol_start = max(start_date, list_date) if list_date and list_date > start_date else start_date
                sync_kind = "backfill"
            elif start_date < earliest_ann_date:
                if list_date and list_date > start_date:
                    if latest_is_fresh:
                        stats["skipped"] += 1
                        continue
                    if latest_ann_date:
                        symbol_start = max(default_start, latest_ann_date - timedelta(days=SYNC_REFRESH_OVERLAP_DAYS))
                        sync_kind = "incremental"
                    else:
                        symbol_start = list_date
                        sync_kind = "backfill"
                else:
                    symbol_start = start_date
                    symbol_end = min(end_date, earliest_ann_date - timedelta(days=1))
                    sync_kind = "backfill"
            elif latest_ann_date:
                if latest_is_fresh:
                    stats["skipped"] += 1
                    continue
                else:
                    symbol_start = max(default_start, latest_ann_date - timedelta(days=SYNC_REFRESH_OVERLAP_DAYS))
                    sync_kind = "incremental"
            else:
                symbol_start = start_date
                sync_kind = "backfill"
        elif incremental:
            if latest_ann_date:
                symbol_start = max(default_start, latest_ann_date - timedelta(days=SYNC_REFRESH_OVERLAP_DAYS))
                sync_kind = "incremental"
            else:
                symbol_start = default_start
                sync_kind = "backfill"
        else:
            symbol_start = default_start
            sync_kind = "full"

        if not symbol_start or symbol_start > symbol_end:
            stats["skipped"] += 1
            continue

        jobs.append((symbol, symbol_start, symbol_end, sync_kind))
        stats[sync_kind] += 1

    return jobs, stats


def sync_a_stock_income_data(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    incremental: bool = True,
    symbols: Optional[List[str]] = None,
    symbol_scope: str = "a_stock_basic",
    tushare_service: Optional[TushareService] = None,
    analytics_db: Optional[Session] = None,
    progress_callback: Optional[ProgressCallback] = None,
) -> Dict:
    end_value = _parse_date(end_date) or date.today()
    default_start = DEFAULT_START_DATE - timedelta(days=INCOME_HISTORY_LOOKBACK_DAYS)
    owns_analytics_db = analytics_db is None
    analytics_session = analytics_db or AnalyticsSession()
    try:
        explicit_start = _parse_date(start_date)
        earliest_ann_date, latest_ann_date = _income_ann_date_bounds(analytics_session)
        symbol_bounds = _income_ann_date_bounds_by_symbol(analytics_session)
        symbol_list_dates = _income_list_dates_by_symbol(analytics_session)
        analytics_session.commit()
        start_value = explicit_start or default_start

        if start_value > end_value:
            return {
                "status": "up_to_date",
                "start_date": start_value.isoformat(),
                "end_date": end_value.isoformat(),
                "fetched_rows": 0,
                "saved_rows": 0,
                "chunks": 0,
                "symbols": 0,
                "processed_symbols": 0,
                "symbol_scope": symbol_scope,
                "empty_symbols": 0,
                "non_empty_symbols": 0,
                "fetch_seconds": 0.0,
                "insert_seconds": 0.0,
                "total_seconds": 0.0,
                "avg_fetch_ms": 0.0,
                "insert_batches": 0,
                "earliest_ann_date": earliest_ann_date.isoformat() if earliest_ann_date else None,
                "latest_ann_date": latest_ann_date.isoformat() if latest_ann_date else None,
                "skipped_symbols": 0,
                "backfill_symbols": 0,
                "incremental_symbols": 0,
                "full_symbols": 0,
            }

        if symbols is None:
            income_symbols = _load_income_symbols(analytics_session)
        else:
            income_symbols = sorted(
                {
                    str(symbol or "").strip().upper()
                    for symbol in symbols
                    if str(symbol or "").strip()
                }
            )
        if not income_symbols:
            return {
                "status": "skipped",
                "start_date": start_value.isoformat(),
                "end_date": end_value.isoformat(),
                "fetched_rows": 0,
                "saved_rows": 0,
                "chunks": 0,
                "symbols": 0,
                "processed_symbols": 0,
                "symbol_scope": symbol_scope,
                "empty_symbols": 0,
                "non_empty_symbols": 0,
                "fetch_seconds": 0.0,
                "insert_seconds": 0.0,
                "total_seconds": 0.0,
                "avg_fetch_ms": 0.0,
                "insert_batches": 0,
                "earliest_ann_date": earliest_ann_date.isoformat() if earliest_ann_date else None,
                "latest_ann_date": latest_ann_date.isoformat() if latest_ann_date else None,
                "skipped_symbols": 0,
                "backfill_symbols": 0,
                "incremental_symbols": 0,
                "full_symbols": 0,
            }

        income_jobs, job_stats = _plan_income_symbol_ranges(
            income_symbols,
            explicit_start,
            end_value,
            incremental=incremental,
            symbol_bounds=symbol_bounds,
            symbol_list_dates=symbol_list_dates,
        )
        skipped_symbols = job_stats["skipped"]
        backfill_symbols = job_stats["backfill"]
        incremental_symbols = job_stats["incremental"]
        full_symbols = job_stats["full"]
        if not income_jobs:
            return {
                "status": "up_to_date",
                "start_date": start_value.isoformat(),
                "end_date": end_value.isoformat(),
                "fetched_rows": 0,
                "saved_rows": 0,
                "chunks": 0,
                "symbols": len(income_symbols),
                "processed_symbols": 0,
                "symbol_scope": symbol_scope,
                "empty_symbols": 0,
                "non_empty_symbols": 0,
                "fetch_seconds": 0.0,
                "insert_seconds": 0.0,
                "total_seconds": 0.0,
                "avg_fetch_ms": 0.0,
                "insert_batches": 0,
                "earliest_ann_date": earliest_ann_date.isoformat() if earliest_ann_date else None,
                "latest_ann_date": latest_ann_date.isoformat() if latest_ann_date else None,
                "skipped_symbols": skipped_symbols,
                "backfill_symbols": backfill_symbols,
                "incremental_symbols": incremental_symbols,
                "full_symbols": full_symbols,
            }

        tushare = tushare_service or TushareService.getInstance()
        workers = min(SYNC_WORKERS, len(income_jobs))
        sync_started_at = time.perf_counter()
        fetch_seconds = 0.0
        insert_seconds = 0.0
        fetched_rows = 0
        saved_rows = 0
        processed_symbols = 0
        empty_symbols = 0
        non_empty_symbols = 0
        insert_batches = 0
        pending_income_frames: List[pd.DataFrame] = []
        pending_income_rows = 0
        logger.info(
            (
                "Sync A stock income data base_range=%s~%s symbols=%s fetch_jobs=%s "
                "skipped=%s backfill=%s incremental_jobs=%s full_jobs=%s "
                "scope=%s workers=%s incremental=%s ann_date_bounds=%s~%s"
            ),
            start_value,
            end_value,
            len(income_symbols),
            len(income_jobs),
            skipped_symbols,
            backfill_symbols,
            incremental_symbols,
            full_symbols,
            symbol_scope,
            workers,
            incremental,
            earliest_ann_date,
            latest_ann_date,
        )

        def fetch_symbol(job: Tuple[str, date, date, str]) -> Tuple[str, pd.DataFrame, float, date, date, str]:
            symbol, symbol_start, symbol_end, sync_kind = job
            fetch_started_at = time.perf_counter()
            frame = tushare.get_a_stock_income_range_frame(symbol_start, symbol_end, ts_code=symbol)
            if isinstance(frame, pd.DataFrame) and not frame.empty:
                frame = _filter_income_ann_date_range(frame, symbol_start, symbol_end)
            fetch_elapsed = time.perf_counter() - fetch_started_at
            return symbol, frame if isinstance(frame, pd.DataFrame) else pd.DataFrame(), fetch_elapsed, symbol_start, symbol_end, sync_kind

        def flush_income_frames():
            nonlocal insert_batches, insert_seconds, pending_income_frames, pending_income_rows, saved_rows
            if not pending_income_frames:
                return
            if len(pending_income_frames) == 1:
                batch_frame = pending_income_frames[0]
            else:
                batch_frame = pd.concat(pending_income_frames, ignore_index=True)
            insert_started_at = time.perf_counter()
            saved_rows += _bulk_upsert_income_frame(analytics_session, batch_frame)
            insert_seconds += time.perf_counter() - insert_started_at
            insert_batches += 1
            pending_income_frames = []
            pending_income_rows = 0

        def queue_income_frame(frame: pd.DataFrame):
            nonlocal pending_income_rows
            pending_income_frames.append(frame)
            pending_income_rows += len(frame)
            if (
                pending_income_rows >= INCOME_INSERT_BATCH_ROWS
                or len(pending_income_frames) >= INCOME_INSERT_BATCH_FRAMES
            ):
                flush_income_frames()

        def report_progress(processed: int, symbol: str):
            if progress_callback:
                progress_callback(
                    {
                        "message": f"同步A股利润表 {symbol}",
                        "progress": 5 + int((processed + skipped_symbols) / max(len(income_symbols), 1) * 90),
                        "processed_symbols": processed,
                        "total_symbols": len(income_symbols),
                        "fetch_jobs": len(income_jobs),
                        "skipped_symbols": skipped_symbols,
                        "backfill_symbols": backfill_symbols,
                        "incremental_symbols": incremental_symbols,
                        "full_symbols": full_symbols,
                        "symbol_scope": symbol_scope,
                        "fetch_seconds": round(fetch_seconds, 3),
                        "insert_seconds": round(insert_seconds, 3),
                        "fetched_rows": fetched_rows,
                        "saved_rows": saved_rows,
                        "empty_symbols": empty_symbols,
                        "non_empty_symbols": non_empty_symbols,
                        "insert_batches": insert_batches,
                        "pending_insert_rows": pending_income_rows,
                    }
                )

        if workers <= 1:
            for job in income_jobs:
                symbol, frame, fetch_elapsed, _symbol_start, _symbol_end, _sync_kind = fetch_symbol(job)
                fetch_seconds += fetch_elapsed
                processed_symbols += 1
                fetched_rows += len(frame)
                if frame.empty:
                    empty_symbols += 1
                else:
                    non_empty_symbols += 1
                    queue_income_frame(frame)
                if processed_symbols == 1 or processed_symbols == len(income_jobs) or processed_symbols % 50 == 0:
                    report_progress(processed_symbols, symbol)
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(fetch_symbol, job) for job in income_jobs]
                for future in as_completed(futures):
                    symbol, frame, fetch_elapsed, _symbol_start, _symbol_end, _sync_kind = future.result()
                    fetch_seconds += fetch_elapsed
                    processed_symbols += 1
                    fetched_rows += len(frame)
                    if frame.empty:
                        empty_symbols += 1
                    else:
                        non_empty_symbols += 1
                        queue_income_frame(frame)
                    if processed_symbols == 1 or processed_symbols == len(income_jobs) or processed_symbols % 50 == 0:
                        report_progress(processed_symbols, symbol)

        flush_income_frames()
        total_seconds = time.perf_counter() - sync_started_at
        avg_fetch_ms = fetch_seconds / max(processed_symbols, 1) * 1000
        logger.info(
            (
                "A stock income sync timing: total=%.3fs fetch=%.3fs insert=%.3fs "
                "avg_fetch=%.1fms symbols=%s fetch_jobs=%s skipped=%s backfill=%s "
                "incremental_jobs=%s full_jobs=%s empty_symbols=%s non_empty_symbols=%s "
                "fetched_rows=%s saved_rows=%s insert_batches=%s scope=%s"
            ),
            total_seconds,
            fetch_seconds,
            insert_seconds,
            avg_fetch_ms,
            len(income_symbols),
            processed_symbols,
            skipped_symbols,
            backfill_symbols,
            incremental_symbols,
            full_symbols,
            empty_symbols,
            non_empty_symbols,
            fetched_rows,
            saved_rows,
            insert_batches,
            symbol_scope,
        )

        if progress_callback:
            progress_callback(
                {
                    "message": "A股利润表同步完成",
                    "progress": 100,
                    "processed_symbols": processed_symbols,
                    "total_symbols": len(income_symbols),
                    "fetch_jobs": len(income_jobs),
                    "skipped_symbols": skipped_symbols,
                    "backfill_symbols": backfill_symbols,
                    "incremental_symbols": incremental_symbols,
                    "full_symbols": full_symbols,
                    "symbol_scope": symbol_scope,
                    "fetch_seconds": round(fetch_seconds, 3),
                    "insert_seconds": round(insert_seconds, 3),
                    "total_seconds": round(total_seconds, 3),
                    "insert_batches": insert_batches,
                }
            )

        return {
            "status": "completed",
            "start_date": start_value.isoformat(),
            "end_date": end_value.isoformat(),
            "fetched_rows": fetched_rows,
            "saved_rows": saved_rows,
            "chunks": len(income_jobs),
            "symbols": len(income_symbols),
            "processed_symbols": processed_symbols,
            "symbol_scope": symbol_scope,
            "empty_symbols": empty_symbols,
            "non_empty_symbols": non_empty_symbols,
            "fetch_seconds": round(fetch_seconds, 3),
            "insert_seconds": round(insert_seconds, 3),
            "total_seconds": round(total_seconds, 3),
            "avg_fetch_ms": round(avg_fetch_ms, 3),
            "insert_batches": insert_batches,
            "earliest_ann_date": earliest_ann_date.isoformat() if earliest_ann_date else None,
            "latest_ann_date": latest_ann_date.isoformat() if latest_ann_date else None,
            "skipped_symbols": skipped_symbols,
            "backfill_symbols": backfill_symbols,
            "incremental_symbols": incremental_symbols,
            "full_symbols": full_symbols,
        }
    finally:
        if owns_analytics_db:
            AnalyticsSession.remove()


def _count_analytics_table_rows(analytics_db: Session, table_name: str) -> int:
    analytics_db.commit()
    import duckdb  # type: ignore

    try:
        connection = duckdb.connect(database=ANALYTICS_DB_PATH, read_only=False)
        try:
            row = connection.execute(
                f"SELECT COUNT(*) FROM {_quote_duckdb_identifier(table_name)}"
            ).fetchone()
            return int(row[0] or 0) if row else 0
        finally:
            connection.close()
    except Exception:
        row = analytics_db.execute(text(f"SELECT COUNT(*) FROM {_quote_duckdb_identifier(table_name)}")).fetchone()
        return int(row[0] or 0) if row else 0


def _latest_analytics_date(analytics_db: Session, model, column_name: str) -> Optional[date]:
    column = getattr(model, column_name)
    row = (
        analytics_db.query(column)
        .filter(column.isnot(None))
        .order_by(column.desc())
        .first()
    )
    return _parse_date(row[0]) if row else None


def _overlap_start(default_start: date, latest_date: Optional[date]) -> date:
    if not latest_date:
        return default_start
    return max(default_start, latest_date - timedelta(days=SYNC_REFRESH_OVERLAP_DAYS))


def sync_a_stock_base_data(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    incremental: bool = True,
    tushare_service: Optional[TushareService] = None,
    analytics_db: Optional[Session] = None,
    progress_callback: Optional[ProgressCallback] = None,
) -> Dict:
    service = AStockBaseDataSyncService(
        analytics_db=analytics_db,
        tushare_service=tushare_service,
        progress_callback=progress_callback,
    )
    try:
        return service.sync_base_data(
            start_date=start_date,
            end_date=end_date,
            incremental=incremental,
        )
    finally:
        service.close()


def _normalize_fund_daily_frame(frame: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "ts_code",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "change",
        "pct_chg",
        "vol",
        "amount",
        "created_at",
        "updated_at",
    ]
    if frame.empty:
        return pd.DataFrame(columns=columns)

    now = datetime.now()
    normalized = pd.DataFrame(index=frame.index)
    normalized["ts_code"] = _clean_text_series(frame, "ts_code")
    normalized["trade_date"] = _date_series(frame, "trade_date")
    normalized["open"] = _numeric_series(frame, "open", 6)
    normalized["high"] = _numeric_series(frame, "high", 6)
    normalized["low"] = _numeric_series(frame, "low", 6)
    normalized["close"] = _numeric_series(frame, "close", 6)
    normalized["pre_close"] = _numeric_series(frame, "pre_close", 6)
    normalized["change"] = _numeric_series(frame, "change", 6)
    normalized["pct_chg"] = _numeric_series(frame, "pct_chg", 6)
    normalized["vol"] = _numeric_series(frame, "vol", 4)
    normalized["amount"] = _numeric_series(frame, "amount", 4)
    normalized["created_at"] = now
    normalized["updated_at"] = now
    normalized = normalized.dropna(subset=["ts_code", "trade_date"])
    if normalized.empty:
        return pd.DataFrame(columns=columns)
    normalized = normalized.drop_duplicates(subset=["ts_code", "trade_date"], keep="last")
    return normalized.loc[:, columns]


def _bulk_upsert_fund_daily_frame(analytics_db: Session, frame: pd.DataFrame) -> int:
    normalized = _normalize_fund_daily_frame(frame)
    if normalized.empty:
        return 0
    columns = list(normalized.columns)
    analytics_db.commit()
    _insert_or_replace_analytics_frame(
        AStockFundDaily.__tablename__,
        columns,
        normalized.loc[:, columns],
    )
    return len(normalized)


def _upsert_index_daily_frame(analytics_db: Session, frame: pd.DataFrame):
    if frame.empty:
        return 0

    now = datetime.now()
    columns = [
        "ts_code",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "change",
        "pct_chg",
        "vol",
        "amount",
        "created_at",
        "updated_at",
    ]
    normalized = pd.DataFrame(index=frame.index)
    normalized["ts_code"] = _clean_text_series(frame, "ts_code")
    normalized["trade_date"] = _date_series(frame, "trade_date")
    normalized["open"] = _numeric_series(frame, "open", 6)
    normalized["high"] = _numeric_series(frame, "high", 6)
    normalized["low"] = _numeric_series(frame, "low", 6)
    normalized["close"] = _numeric_series(frame, "close", 6)
    normalized["pre_close"] = _numeric_series(frame, "pre_close", 6)
    normalized["change"] = _numeric_series(frame, "change", 6)
    normalized["pct_chg"] = _numeric_series(frame, "pct_chg", 6)
    normalized["vol"] = _numeric_series(frame, "vol", 4)
    normalized["amount"] = _numeric_series(frame, "amount", 4)
    normalized["created_at"] = now
    normalized["updated_at"] = now
    normalized = normalized.dropna(subset=["ts_code", "trade_date"])
    if normalized.empty:
        return 0
    normalized = normalized.drop_duplicates(subset=["ts_code", "trade_date"], keep="last")
    table = AStockIndexDaily.__table__
    analytics_db.commit()
    _insert_or_replace_analytics_frame(
        table.name,
        columns,
        normalized.loc[:, columns],
    )
    return len(normalized)


def sync_benchmark_index_daily(
    start_date: date,
    end_date: date,
    tushare_service: Optional[TushareService] = None,
    analytics_db: Optional[Session] = None,
):
    start_value = _parse_date(start_date)
    end_value = _parse_date(end_date)
    if not start_value or not end_value or start_value > end_value:
        return

    tushare = tushare_service or TushareService.getInstance()
    owns_analytics_db = analytics_db is None
    analytics_session = analytics_db or AnalyticsSession()
    try:
        index_map = {
            item["ts_code"]: item
            for item in [
                *BENCHMARK_INDEXES,
                *A_STOCK_FEAR_SAFE_HAVEN_INDEXES,
                *[
                    {
                        "ts_code": str(item.get("index_code") or item["symbol"]).upper(),
                        "name": item.get("index_name") or item.get("label"),
                    }
                    for item in A_STOCK_INDEX_FEAR_GREED_TARGETS
                ],
                *[
                    {"ts_code": item["index_code"], "name": item["name"]}
                    for item in A_STOCK_FACTOR_INDEX_POOLS
                ],
            ]
        }
        for benchmark in index_map.values():
            ts_code = benchmark["ts_code"]
            frame = tushare.get_index_daily_range_frame(ts_code, start_value, end_value)
            _upsert_index_daily_frame(analytics_session, frame)
    finally:
        if owns_analytics_db:
            AnalyticsSession.remove()

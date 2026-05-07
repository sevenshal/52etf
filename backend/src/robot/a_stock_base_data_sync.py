import hashlib
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..core.analytics_database import (
    ANALYTICS_DB_PATH,
    AStockBasic,
    AStockIncome,
    AStockIndexDaily,
    AStockMarketDaily,
    AStockNameChange,
    AnalyticsSession,
)
from ..core.services.tushare import TushareService
from .a_stock_base_data_config import (
    BENCHMARK_INDEXES,
    DEFAULT_START_DATE,
    MAX_MARKET_DAILY_OHL_ZERO_PCT,
    MIN_MARKET_DAILY_ROWS,
    RAW_FETCH_LOOKBACK_DAYS,
)


logger = logging.getLogger(__name__)

ProgressCallback = Callable[[Dict], None]

MARKET_FETCH_WORKERS = max(
    1,
    int(
        os.getenv(
            "A_STOCK_BASE_DATA_SYNC_FETCH_WORKERS",
            os.getenv("A_STOCK_INNOVATION100_FETCH_WORKERS", "3"),
        )
    ),
)
INCOME_HISTORY_LOOKBACK_DAYS = max(365, int(os.getenv("A_STOCK_INCOME_HISTORY_LOOKBACK_DAYS", str(365 * 6))))
INCOME_SYNC_REFRESH_OVERLAP_DAYS = max(0, int(os.getenv("A_STOCK_INCOME_SYNC_REFRESH_OVERLAP_DAYS", "45")))
INCOME_SYNC_WORKERS = max(1, int(os.getenv("A_STOCK_INCOME_SYNC_WORKERS", "1")))
A_STOCK_BASE_DATA_SYNC_REFRESH_OVERLAP_DAYS = max(
    0,
    int(os.getenv("A_STOCK_BASE_DATA_SYNC_REFRESH_OVERLAP_DAYS", "45")),
)


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


def _market_day_needs_refresh(day_stats: Optional[Dict]) -> bool:
    if not day_stats:
        return True
    row_count = int(day_stats.get("row_count") or 0)
    if row_count < MIN_MARKET_DAILY_ROWS:
        return True
    ohl_zero_rows = int(day_stats.get("ohl_zero_rows") or 0)
    ohl_zero_pct = ohl_zero_rows / row_count * 100 if row_count else 100.0
    return ohl_zero_pct > MAX_MARKET_DAILY_OHL_ZERO_PCT


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
        workers = min(MARKET_FETCH_WORKERS, len(chunks))
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
        latest_index_date = _latest_analytics_date(self.analytics_db, AStockIndexDaily, "trade_date")
        name_change_rows_before = _count_analytics_table_rows(self.analytics_db, AStockNameChange.__tablename__)
        self.analytics_db.commit()

        reference_full_refresh = not incremental or name_change_rows_before <= 0
        reference_start = explicit_start or max(DEFAULT_START_DATE, end_value - timedelta(days=90))
        if reference_full_refresh and not explicit_start:
            self.sync_reference_data(DEFAULT_START_DATE, end_value)
        else:
            self.sync_reference_data_incremental(reference_start, end_value)

        market_default_start = DEFAULT_START_DATE - timedelta(days=RAW_FETCH_LOOKBACK_DAYS)
        if explicit_start:
            market_start = max(market_default_start, explicit_start)
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

        index_default_start = DEFAULT_START_DATE
        if explicit_start:
            index_start = max(index_default_start, explicit_start)
        elif incremental:
            index_start = _overlap_start(index_default_start, latest_index_date)
        else:
            index_start = index_default_start
        self._progress("同步A股基准指数日行情缓存", 62, start_date=index_start.isoformat(), end_date=end_value.isoformat())
        sync_benchmark_index_daily(
            index_start,
            end_value,
            tushare_service=self.tushare,
            analytics_db=self.analytics_db,
        )

        income_start = explicit_start - timedelta(days=INCOME_HISTORY_LOOKBACK_DAYS) if explicit_start else None
        self._progress("同步A股利润表财务数据缓存", 76)
        income_result = sync_a_stock_income_data(
            start_date=income_start,
            end_date=end_value,
            incremental=incremental and not explicit_start,
            tushare_service=self.tushare,
            analytics_db=self.analytics_db,
            progress_callback=self.progress_callback,
        )

        basic_rows = _count_analytics_table_rows(self.analytics_db, AStockBasic.__tablename__)
        name_change_rows = _count_analytics_table_rows(self.analytics_db, AStockNameChange.__tablename__)
        market_rows = _count_analytics_table_rows(self.analytics_db, AStockMarketDaily.__tablename__)
        index_rows = _count_analytics_table_rows(self.analytics_db, AStockIndexDaily.__tablename__)
        income_rows = _count_analytics_table_rows(self.analytics_db, AStockIncome.__tablename__)
        self.analytics_db.commit()

        self._progress("A股基础数据同步完成", 100)
        return {
            "status": "completed",
            "mode": "incremental" if incremental else "full",
            "start_date": explicit_start.isoformat() if explicit_start else None,
            "end_date": end_value.isoformat(),
            "reference_full_refresh": reference_full_refresh,
            "market_start_date": market_start.isoformat(),
            "market_trade_days": len(trading_dates),
            "index_start_date": index_start.isoformat(),
            "income_start_date": income_result.get("start_date"),
            "income_fetched_rows": income_result.get("fetched_rows"),
            "income_saved_rows": income_result.get("saved_rows"),
            "tables": {
                AStockBasic.__tablename__: basic_rows,
                AStockIncome.__tablename__: income_rows,
                AStockIndexDaily.__tablename__: index_rows,
                AStockMarketDaily.__tablename__: market_rows,
                AStockNameChange.__tablename__: name_change_rows,
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


def _upsert_income_frame(analytics_db: Session, frame: pd.DataFrame) -> int:
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
    _insert_or_replace_analytics_frame(
        table.name,
        columns,
        normalized.loc[:, columns],
    )
    return len(normalized)


def _latest_income_ann_date(analytics_db: Session) -> Optional[date]:
    row = (
        analytics_db.query(AStockIncome.ann_date)
        .filter(AStockIncome.ann_date.isnot(None))
        .order_by(AStockIncome.ann_date.desc())
        .first()
    )
    return _parse_date(row[0]) if row else None


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


def sync_a_stock_income_data(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    incremental: bool = True,
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
        latest_ann_date = _latest_income_ann_date(analytics_session)
        analytics_session.commit()
        if explicit_start:
            start_value = explicit_start
        elif incremental and latest_ann_date:
            start_value = max(default_start, latest_ann_date - timedelta(days=INCOME_SYNC_REFRESH_OVERLAP_DAYS))
        else:
            start_value = default_start

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
                "latest_ann_date": latest_ann_date.isoformat() if latest_ann_date else None,
            }

        tushare = tushare_service or TushareService.getInstance()
        symbols = _load_income_symbols(analytics_session)
        if not symbols:
            return {
                "status": "no_symbols",
                "start_date": start_value.isoformat(),
                "end_date": end_value.isoformat(),
                "fetched_rows": 0,
                "saved_rows": 0,
                "chunks": 0,
                "symbols": 0,
                "processed_symbols": 0,
                "latest_ann_date": latest_ann_date.isoformat() if latest_ann_date else None,
            }

        workers = min(INCOME_SYNC_WORKERS, len(symbols))
        fetched_rows = 0
        saved_rows = 0
        processed_symbols = 0
        logger.info(
            "Sync A stock income data range=%s~%s symbols=%s workers=%s incremental=%s latest_ann_date=%s",
            start_value,
            end_value,
            len(symbols),
            workers,
            incremental,
            latest_ann_date,
        )

        def fetch_symbol(symbol: str) -> Tuple[str, pd.DataFrame]:
            frame = tushare.get_a_stock_income_range_frame(start_value, end_value, ts_code=symbol)
            if isinstance(frame, pd.DataFrame) and not frame.empty:
                frame = _filter_income_ann_date_range(frame, start_value, end_value)
            return symbol, frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()

        def report_progress(processed: int, symbol: str):
            if progress_callback:
                progress_callback(
                    {
                        "message": f"同步A股利润表 {symbol}",
                        "progress": 5 + int(processed / max(len(symbols), 1) * 90),
                        "processed_symbols": processed,
                        "total_symbols": len(symbols),
                    }
                )

        if workers <= 1:
            for symbol in symbols:
                _, frame = fetch_symbol(symbol)
                processed_symbols += 1
                fetched_rows += len(frame)
                if not frame.empty:
                    saved_rows += _upsert_income_frame(analytics_session, frame)
                if processed_symbols == 1 or processed_symbols == len(symbols) or processed_symbols % 50 == 0:
                    report_progress(processed_symbols, symbol)
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(fetch_symbol, symbol) for symbol in symbols]
                for future in as_completed(futures):
                    symbol, frame = future.result()
                    processed_symbols += 1
                    fetched_rows += len(frame)
                    if not frame.empty:
                        saved_rows += _upsert_income_frame(analytics_session, frame)
                    if processed_symbols == 1 or processed_symbols == len(symbols) or processed_symbols % 50 == 0:
                        report_progress(processed_symbols, symbol)

        if progress_callback:
            progress_callback(
                {
                    "message": "A股利润表同步完成",
                    "progress": 100,
                    "processed_symbols": processed_symbols,
                    "total_symbols": len(symbols),
                }
            )

        return {
            "status": "completed",
            "start_date": start_value.isoformat(),
            "end_date": end_value.isoformat(),
            "fetched_rows": fetched_rows,
            "saved_rows": saved_rows,
            "chunks": len(symbols),
            "symbols": len(symbols),
            "processed_symbols": processed_symbols,
            "latest_ann_date": latest_ann_date.isoformat() if latest_ann_date else None,
        }
    finally:
        if owns_analytics_db:
            AnalyticsSession.remove()


def _count_analytics_table_rows(analytics_db: Session, table_name: str) -> int:
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
    return max(default_start, latest_date - timedelta(days=A_STOCK_BASE_DATA_SYNC_REFRESH_OVERLAP_DAYS))


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


def _upsert_index_daily_frame(analytics_db: Session, frame: pd.DataFrame):
    if frame.empty:
        return

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
        return
    normalized = normalized.drop_duplicates(subset=["ts_code", "trade_date"], keep="last")
    table = AStockIndexDaily.__table__
    analytics_db.commit()
    _insert_or_replace_analytics_frame(
        table.name,
        columns,
        normalized.loc[:, columns],
    )


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
        for benchmark in BENCHMARK_INDEXES:
            ts_code = benchmark["ts_code"]
            frame = tushare.get_index_daily_range_frame(ts_code, start_value, end_value)
            _upsert_index_daily_frame(analytics_session, frame)
    finally:
        if owns_analytics_db:
            AnalyticsSession.remove()

import logging
import math
import os
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import Callable, Deque, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..core.database import (
    AStockBasic,
    AStockIndexDaily,
    AStockInnovation100Constituent,
    AStockInnovation100Level,
    AStockInnovation100Rebalance,
    AStockMarketDaily,
    AStockNameChange,
    engine,
)
from ..core.services.tushare import TushareService


INDEX_CODE = "CNINNO100"
INDEX_NAME = "A股创新100"
BASE_LEVEL = 1000.0
DEFAULT_START_DATE = date(2020, 1, 1)
BENCHMARK_INDEXES = [
    {"ts_code": "000300.SH", "name": "沪深300"},
    {"ts_code": "000905.SH", "name": "中证500"},
]
TARGET_CONSTITUENT_COUNT = 100
DIRECT_ENTRY_RANK = 75
RETENTION_RANK = 125
MIN_LISTING_DAYS = 365
LIQUIDITY_WINDOW = 60
MIN_AVG_AMOUNT_60D = 100_000.0  # Tushare amount单位为千元，约等于1亿元人民币。
MIN_MARKET_DAILY_ROWS = 3500
MAX_MARKET_DAILY_OHL_ZERO_PCT = 1.0
MAX_SINGLE_WEIGHT = 0.10
TOP5_WEIGHT_CAP = 0.40
LARGE_WEIGHT_THRESHOLD = 0.045
LARGE_WEIGHT_CAP = 0.48
RAW_FETCH_LOOKBACK_DAYS = 180
MARKET_FETCH_WORKERS = max(1, int(os.getenv("A_STOCK_INNOVATION100_FETCH_WORKERS", "3")))

INNOVATION_INDUSTRIES = {
    "IT设备",
    "互联网",
    "元器件",
    "半导体",
    "软件服务",
    "通信设备",
    "电信运营",
    "电器仪表",
    "电气设备",
    "专用机械",
    "工程机械",
    "机床制造",
    "机械基件",
    "运输设备",
    "汽车整车",
    "汽车配件",
    "航空",
    "船舶",
    "化学制药",
    "生物制药",
    "医疗保健",
    "医药商业",
    "中成药",
    "环境保护",
    "新型电力",
}

EXCLUDED_INDUSTRY_KEYWORDS = (
    "银行",
    "保险",
    "证券",
    "多元金融",
    "地产",
    "房产",
)


ProgressCallback = Callable[[Dict], None]


def _round_or_none(value, digits: int = 4):
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return round(numeric, digits)


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


def _is_finite_positive(value) -> bool:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(numeric) and numeric > 0


def _year_chunks(start_date: date, end_date: date) -> Iterable[Tuple[date, date]]:
    current = date(start_date.year, 1, 1)
    if current < start_date:
        current = start_date
    while current <= end_date:
        chunk_end = min(date(current.year, 12, 31), end_date)
        yield current, chunk_end
        current = chunk_end + timedelta(days=1)


class AStockInnovation100Builder:
    def __init__(
        self,
        db: Session,
        tushare_service: Optional[TushareService] = None,
        progress_callback: Optional[ProgressCallback] = None,
    ):
        self.db = db
        self.tushare = tushare_service or TushareService.getInstance()
        self.progress_callback = progress_callback
        self.logger = logging.getLogger(self.__class__.__name__)

    def _progress(self, message: str, progress: int, **extra):
        payload = {
            "message": message,
            "progress": max(0, min(100, int(progress))),
            **extra,
        }
        self.logger.info("%s (%s%%)", message, payload["progress"])
        if self.progress_callback:
            self.progress_callback(payload)

    @staticmethod
    def rule_snapshot() -> Dict:
        return {
            "index_code": INDEX_CODE,
            "index_name": INDEX_NAME,
            "base_level": BASE_LEVEL,
            "target_constituent_count": TARGET_CONSTITUENT_COUNT,
            "direct_entry_rank": DIRECT_ENTRY_RANK,
            "retention_rank": RETENTION_RANK,
            "min_listing_days": MIN_LISTING_DAYS,
            "liquidity_window": LIQUIDITY_WINDOW,
            "min_avg_amount_60d": MIN_AVG_AMOUNT_60D,
            "max_single_weight_pct": MAX_SINGLE_WEIGHT * 100,
            "top5_weight_cap_pct": TOP5_WEIGHT_CAP * 100,
            "large_weight_threshold_pct": LARGE_WEIGHT_THRESHOLD * 100,
            "large_weight_cap_pct": LARGE_WEIGHT_CAP * 100,
            "reconstitution": "每年12月最后一个交易日收盘选样，下一交易日生效；初始日当日生效",
            "rebalance": "每季度最后一个交易日收盘调整权重，下一交易日生效",
            "universe": "沪深A股，剔除ST/退市整理、金融地产、上市不足一年和60日成交额不足1亿元的股票",
            "industries": sorted(INNOVATION_INDUSTRIES),
        }

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
        self._bulk_upsert(AStockBasic, mappings, ["ts_code"])

    def _replace_name_changes(self, frame: pd.DataFrame):
        self.db.query(AStockNameChange).delete(synchronize_session=False)
        self._insert_name_changes(frame)

    def _replace_name_changes_range(self, frame: pd.DataFrame, start_date: date, end_date: date):
        self.db.execute(
            text("""
                DELETE FROM a_stock_name_changes
                WHERE start_date >= :start_date AND start_date <= :end_date
            """),
            {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
        )
        self._insert_name_changes(frame)

    def _insert_name_changes(self, frame: pd.DataFrame):
        now = datetime.now()
        mappings = []
        for _, row in frame.iterrows():
            ts_code = str(row.get("ts_code") or "").strip()
            if not ts_code:
                continue
            mappings.append(
                {
                    "ts_code": ts_code,
                    "name": _clean_text(row.get("name")),
                    "start_date": _parse_date(row.get("start_date")),
                    "end_date": _parse_date(row.get("end_date")),
                    "change_reason": _clean_text(row.get("change_reason")),
                    "updated_at": now,
                }
            )
        for batch in self._chunks(mappings, 1000):
            self.db.bulk_insert_mappings(AStockNameChange, batch)
        self.db.commit()

    def _bulk_upsert(self, model, mappings: List[Dict], index_elements: List[str], batch_size: int = 1000):
        if not mappings:
            return
        table = model.__table__
        for batch in self._chunks(mappings, batch_size):
            stmt = sqlite_insert(table).values(batch)
            update_columns = {
                column.name: getattr(stmt.excluded, column.name)
                for column in table.columns
                if column.name not in index_elements and not column.primary_key
            }
            self.db.execute(stmt.on_conflict_do_update(index_elements=index_elements, set_=update_columns))
        self.db.commit()

    @staticmethod
    def _chunks(items: List[Dict], size: int):
        for index in range(0, len(items), size):
            yield items[index:index + size]

    @staticmethod
    def _market_day_needs_refresh(day_stats: Optional[Dict]) -> bool:
        if not day_stats:
            return True
        row_count = int(day_stats.get("row_count") or 0)
        if row_count < MIN_MARKET_DAILY_ROWS:
            return True
        ohl_zero_rows = int(day_stats.get("ohl_zero_rows") or 0)
        ohl_zero_pct = ohl_zero_rows / row_count * 100 if row_count else 100.0
        return ohl_zero_pct > MAX_MARKET_DAILY_OHL_ZERO_PCT

    def _ensure_market_day(self, trade_date: date):
        row = self.db.execute(
            text("""
                SELECT
                    COUNT(*) AS row_count,
                    SUM(CASE
                        WHEN COALESCE(open, 0) = 0
                          OR COALESCE(high, 0) = 0
                          OR COALESCE(low, 0) = 0
                        THEN 1 ELSE 0
                    END) AS ohl_zero_rows
                FROM a_stock_market_daily
                WHERE trade_date = :trade_date
            """),
            {"trade_date": trade_date.isoformat()},
        ).fetchone()
        day_stats = {
            "row_count": int(row[0] or 0),
            "ohl_zero_rows": int(row[1] or 0),
        } if row else None
        if not self._market_day_needs_refresh(day_stats):
            return

        frame = self.tushare.get_a_stock_market_daily_frame(trade_date)
        if frame.empty:
            return

        self._upsert_market_frame(frame, trade_date=trade_date)

    def _existing_market_day_stats(self, start_date: date, end_date: date) -> Dict[date, Dict]:
        rows = self.db.execute(
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
        missing_dates = [item for item in trading_dates if self._market_day_needs_refresh(stats_by_date.get(item))]
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
            frame = TushareService.getInstance().get_a_stock_market_daily_range_frame(chunk_start, chunk_end)
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
        now = datetime.now()
        mappings = []
        for _, row in frame.iterrows():
            ts_code = str(row.get("ts_code") or "").strip()
            if not ts_code:
                continue
            mappings.append(
                {
                    "trade_date": _parse_date(row.get("trade_date")) or trade_date,
                    "ts_code": ts_code,
                    "open": _round_or_none(row.get("open"), 6),
                    "high": _round_or_none(row.get("high"), 6),
                    "low": _round_or_none(row.get("low"), 6),
                    "close": _round_or_none(row.get("close"), 6),
                    "pre_close": _round_or_none(row.get("pre_close"), 6),
                    "change": _round_or_none(row.get("change"), 6),
                    "pct_chg": _round_or_none(row.get("pct_chg"), 6),
                    "vol": _round_or_none(row.get("vol"), 4),
                    "amount": _round_or_none(row.get("amount"), 4),
                    "total_mv": _round_or_none(row.get("total_mv"), 4),
                    "circ_mv": _round_or_none(row.get("circ_mv"), 4),
                    "float_share": _round_or_none(row.get("float_share"), 4),
                    "total_share": _round_or_none(row.get("total_share"), 4),
                    "turnover_rate": _round_or_none(row.get("turnover_rate"), 6),
                    "created_at": now,
                    "updated_at": now,
                }
            )
        self._bulk_replace_market_daily(mappings)

    def _bulk_replace_market_daily(self, mappings: List[Dict], batch_size: int = 10000):
        if not mappings:
            return
        date_counts = Counter(item.get("trade_date") for item in mappings if item.get("trade_date"))
        replace_dates = sorted(
            trade_date
            for trade_date, row_count in date_counts.items()
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
        placeholders = ",".join(["?"] * len(columns))
        sql = f"""
            INSERT OR REPLACE INTO a_stock_market_daily ({",".join(columns)})
            VALUES ({placeholders})
        """

        def normalize(value):
            if isinstance(value, datetime):
                return value.isoformat(sep=" ")
            if isinstance(value, date):
                return value.isoformat()
            return value

        raw_conn = engine.raw_connection()
        try:
            cursor = raw_conn.cursor()
            if replace_dates:
                cursor.executemany(
                    "DELETE FROM a_stock_market_daily WHERE trade_date = ?",
                    [(normalize(trade_date),) for trade_date in replace_dates],
                )
            for batch in self._chunks(mappings, batch_size):
                cursor.executemany(
                    sql,
                    [tuple(normalize(item.get(column)) for column in columns) for item in batch],
                )
            raw_conn.commit()
        finally:
            raw_conn.close()

    def _load_market_day(self, trade_date: date) -> pd.DataFrame:
        sql = """
            SELECT
                trade_date, ts_code, open, high, low, close, pre_close, change, pct_chg,
                vol, amount, total_mv, circ_mv, float_share, total_share, turnover_rate
            FROM a_stock_market_daily
            WHERE trade_date = :trade_date
        """
        frame = pd.read_sql_query(sql, engine, params={"trade_date": trade_date.isoformat()})
        if frame.empty:
            return frame
        frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
        return frame

    def _load_market_frames_by_date(self, start_date: date, end_date: date) -> Dict[date, pd.DataFrame]:
        sql = """
            SELECT trade_date, ts_code, close, pct_chg, amount, total_mv, circ_mv
            FROM a_stock_market_daily
            WHERE trade_date >= :start_date AND trade_date <= :end_date
            ORDER BY trade_date
        """
        frame = pd.read_sql_query(
            sql,
            engine,
            params={"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
        )
        if frame.empty:
            return {}
        frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
        return {
            trade_date: group.drop(columns=["trade_date"]).reset_index(drop=True)
            for trade_date, group in frame.groupby("trade_date", sort=False)
        }

    def _load_basic_map(self) -> Dict[str, Dict]:
        rows = self.db.query(AStockBasic).all()
        return {
            row.ts_code: {
                "ts_code": row.ts_code,
                "symbol": row.symbol,
                "name": row.name,
                "area": row.area,
                "industry": row.industry,
                "market": row.market,
                "exchange": row.exchange,
                "list_date": row.list_date,
                "delist_date": row.delist_date,
                "list_status": row.list_status,
            }
            for row in rows
        }

    def _load_st_intervals(self) -> Dict[str, List[Tuple[date, date]]]:
        intervals: Dict[str, List[Tuple[date, date]]] = defaultdict(list)
        rows = self.db.query(AStockNameChange).all()
        for row in rows:
            name = (row.name or "").upper()
            reason = (row.change_reason or "").upper()
            if "ST" not in name and "ST" not in reason and "退" not in name and "终止上市" not in reason:
                continue
            start = row.start_date or date(1900, 1, 1)
            end = row.end_date or date(2099, 12, 31)
            intervals[row.ts_code].append((start, end))
        return intervals

    def _is_st_or_retiring(self, ts_code: str, as_of: date, basic: Optional[Dict], intervals: Dict[str, List[Tuple[date, date]]]) -> bool:
        name = str((basic or {}).get("name") or "").upper()
        if "ST" in name or "退" in name:
            return True
        for start, end in intervals.get(ts_code, []):
            if start <= as_of <= end:
                return True
        return False

    def _is_basic_eligible(self, ts_code: str, as_of: date, basic: Optional[Dict], intervals: Dict[str, List[Tuple[date, date]]]) -> bool:
        if not basic:
            return False
        if basic.get("exchange") not in {"SSE", "SZSE"}:
            return False
        industry = str(basic.get("industry") or "")
        if not industry or industry not in INNOVATION_INDUSTRIES:
            return False
        if any(keyword in industry for keyword in EXCLUDED_INDUSTRY_KEYWORDS):
            return False
        list_date = basic.get("list_date")
        if not list_date or (as_of - list_date).days < MIN_LISTING_DAYS:
            return False
        delist_date = basic.get("delist_date")
        if delist_date and delist_date <= as_of:
            return False
        if self._is_st_or_retiring(ts_code, as_of, basic, intervals):
            return False
        return True

    def _rank_candidates(
        self,
        market_frame: pd.DataFrame,
        as_of: date,
        basic_map: Dict[str, Dict],
        st_intervals: Dict[str, List[Tuple[date, date]]],
        amount_history: Dict[str, Deque[float]],
    ) -> List[Dict]:
        if market_frame.empty:
            return []

        candidates = []
        for row in market_frame.itertuples(index=False):
            ts_code = str(getattr(row, "ts_code", "") or "")
            if not ts_code:
                continue
            close = getattr(row, "close", None)
            circ_mv = getattr(row, "circ_mv", None)
            if not _is_finite_positive(close) or not _is_finite_positive(circ_mv):
                continue
            basic = basic_map.get(ts_code)
            if not self._is_basic_eligible(ts_code, as_of, basic, st_intervals):
                continue
            history = amount_history.get(ts_code)
            avg_amount = float(np.mean(history)) if history else 0.0
            if avg_amount < MIN_AVG_AMOUNT_60D:
                continue
            candidates.append(
                {
                    "ts_code": ts_code,
                    "name": basic.get("name"),
                    "industry": basic.get("industry"),
                    "close": float(close),
                    "pct_chg": float(getattr(row, "pct_chg", 0.0) or 0.0),
                    "amount": float(getattr(row, "amount", 0.0) or 0.0),
                    "avg_amount_60d": avg_amount,
                    "total_mv": float(getattr(row, "total_mv", 0.0) or 0.0),
                    "circ_mv": float(circ_mv),
                }
            )

        candidates.sort(key=lambda item: (item["circ_mv"], item["amount"]), reverse=True)
        for rank, item in enumerate(candidates, start=1):
            item["rank"] = rank
        return candidates

    def _select_constituents(
        self,
        ranked: List[Dict],
        previous_symbols: List[str],
        reconstitution: bool,
    ) -> List[Dict]:
        if not ranked:
            return []
        ranked_by_symbol = {item["ts_code"]: item for item in ranked}
        selected_symbols: List[str] = []

        if reconstitution or not previous_symbols:
            for item in ranked[:DIRECT_ENTRY_RANK]:
                selected_symbols.append(item["ts_code"])

            previous_set = set(previous_symbols)
            retained = [
                item["ts_code"]
                for item in ranked[:RETENTION_RANK]
                if item["ts_code"] in previous_set and item["ts_code"] not in selected_symbols
            ]
            selected_symbols.extend(retained)
        else:
            selected_symbols.extend([symbol for symbol in previous_symbols if symbol in ranked_by_symbol])

        if len(selected_symbols) < TARGET_CONSTITUENT_COUNT:
            for item in ranked:
                symbol = item["ts_code"]
                if symbol in selected_symbols:
                    continue
                selected_symbols.append(symbol)
                if len(selected_symbols) >= TARGET_CONSTITUENT_COUNT:
                    break

        selected = [ranked_by_symbol[symbol] for symbol in selected_symbols[:TARGET_CONSTITUENT_COUNT] if symbol in ranked_by_symbol]
        selected.sort(key=lambda item: item["rank"])
        return selected

    @staticmethod
    def _cap_single(weights: np.ndarray, cap: float) -> np.ndarray:
        capped = weights.astype(float).copy()
        if capped.sum() <= 0:
            return capped
        capped = capped / capped.sum()
        for _ in range(20):
            over = capped > cap + 1e-12
            if not over.any():
                break
            excess = float(np.sum(capped[over] - cap))
            capped[over] = cap
            under = ~over
            under_sum = float(np.sum(capped[under]))
            if under_sum <= 0 or excess <= 0:
                break
            capped[under] += capped[under] / under_sum * excess
        return capped / capped.sum() if capped.sum() > 0 else capped

    @classmethod
    def _redistribute_excess(cls, weights: np.ndarray, locked: np.ndarray, excess: float) -> np.ndarray:
        if excess <= 0:
            return weights
        receivers = ~locked
        receiver_sum = float(np.sum(weights[receivers]))
        if receiver_sum <= 0:
            return weights
        weights[receivers] += weights[receivers] / receiver_sum * excess
        return cls._cap_single(weights, MAX_SINGLE_WEIGHT)

    @classmethod
    def _apply_weight_caps(cls, raw_weights: List[float]) -> List[float]:
        weights = np.array(raw_weights, dtype=float)
        if weights.sum() <= 0:
            return [0.0 for _ in raw_weights]
        weights = cls._cap_single(weights / weights.sum(), MAX_SINGLE_WEIGHT)

        for _ in range(10):
            changed = False
            order = np.argsort(-weights)
            top5 = order[:5]
            top5_sum = float(np.sum(weights[top5]))
            if top5_sum > TOP5_WEIGHT_CAP + 1e-12:
                excess = top5_sum - TOP5_WEIGHT_CAP
                weights[top5] *= TOP5_WEIGHT_CAP / top5_sum
                locked = np.zeros(len(weights), dtype=bool)
                locked[top5] = True
                weights = cls._redistribute_excess(weights, locked, excess)
                changed = True

            large = weights > LARGE_WEIGHT_THRESHOLD + 1e-12
            large_sum = float(np.sum(weights[large]))
            if large.any() and large_sum > LARGE_WEIGHT_CAP + 1e-12 and large.sum() < len(weights):
                excess = large_sum - LARGE_WEIGHT_CAP
                weights[large] *= LARGE_WEIGHT_CAP / large_sum
                weights = cls._redistribute_excess(weights, large, excess)
                changed = True

            weights = cls._cap_single(weights, MAX_SINGLE_WEIGHT)
            if not changed:
                break

        weights = weights / weights.sum() if weights.sum() > 0 else weights
        return [float(item) for item in weights]

    def _build_weighted_constituents(
        self,
        selected: List[Dict],
        previous_weight_map: Dict[str, float],
    ) -> Tuple[List[Dict], float]:
        if not selected:
            return [], 0.0
        total_circ_mv = sum(float(item.get("circ_mv") or 0.0) for item in selected)
        raw_weights = [
            float(item.get("circ_mv") or 0.0) / total_circ_mv if total_circ_mv > 0 else 0.0
            for item in selected
        ]
        capped_weights = self._apply_weight_caps(raw_weights)
        weighted = []
        for item, raw_weight, weight in zip(selected, raw_weights, capped_weights):
            row = dict(item)
            row["raw_weight"] = raw_weight
            row["weight"] = weight
            weighted.append(row)

        symbols = {item["ts_code"] for item in weighted}
        turnover = 0.0
        for item in weighted:
            turnover += abs(float(item["weight"]) - float(previous_weight_map.get(item["ts_code"], 0.0)))
        for symbol, previous_weight in previous_weight_map.items():
            if symbol not in symbols:
                turnover += abs(float(previous_weight))
        return weighted, turnover / 2 * 100

    @staticmethod
    def _is_quarter_end(trading_dates: List[date], index: int) -> bool:
        if index >= len(trading_dates) - 1:
            return True
        current = trading_dates[index]
        next_date = trading_dates[index + 1]
        return (current.year, (current.month - 1) // 3) != (next_date.year, (next_date.month - 1) // 3)

    @staticmethod
    def _rebalance_type(current_date: date, is_initial: bool) -> str:
        if is_initial:
            return "inception"
        if current_date.month == 12:
            return "annual_reconstitution"
        return "quarterly_reweight"

    def _save_rebalance(
        self,
        rebalance_date: date,
        effective_date: Optional[date],
        rebalance_type: str,
        constituents: List[Dict],
        previous_symbols: List[str],
        previous_weight_map: Dict[str, float],
        turnover_pct: float,
    ) -> AStockInnovation100Rebalance:
        symbols = [item["ts_code"] for item in constituents]
        previous_set = set(previous_symbols)
        current_set = set(symbols)
        additions = [item for item in constituents if item["ts_code"] not in previous_set]
        removals = [symbol for symbol in previous_symbols if symbol not in current_set]
        total_circ_mv = sum(float(item.get("circ_mv") or 0.0) for item in constituents)

        record = AStockInnovation100Rebalance(
            index_code=INDEX_CODE,
            rebalance_date=rebalance_date,
            effective_date=effective_date,
            rebalance_type=rebalance_type,
            constituent_count=len(constituents),
            turnover_pct=_round_or_none(turnover_pct, 6),
            total_circ_mv=_round_or_none(total_circ_mv, 4),
            additions=[
                {
                    "ts_code": item["ts_code"],
                    "name": item.get("name"),
                    "industry": item.get("industry"),
                    "rank": item.get("rank"),
                    "weight_pct": _round_or_none(float(item.get("weight") or 0.0) * 100, 6),
                }
                for item in additions
            ],
            removals=removals,
            rule_snapshot=self.rule_snapshot(),
            created_at=datetime.now(),
        )
        self.db.add(record)
        self.db.flush()

        for item in constituents:
            action = "added" if item["ts_code"] not in previous_set else "retained"
            self.db.add(
                AStockInnovation100Constituent(
                    index_code=INDEX_CODE,
                    rebalance_id=record.id,
                    ts_code=item["ts_code"],
                    rebalance_date=rebalance_date,
                    effective_date=effective_date,
                    name=item.get("name"),
                    industry=item.get("industry"),
                    rank=item.get("rank"),
                    raw_weight_pct=_round_or_none(float(item.get("raw_weight") or 0.0) * 100, 6),
                    weight_pct=_round_or_none(float(item.get("weight") or 0.0) * 100, 6),
                    total_mv=_round_or_none(item.get("total_mv"), 4),
                    circ_mv=_round_or_none(item.get("circ_mv"), 4),
                    avg_amount_60d=_round_or_none(item.get("avg_amount_60d"), 4),
                    action=action,
                    created_at=datetime.now(),
                )
            )
        self.db.commit()
        return record

    def _delete_existing_index_outputs(self):
        self.db.query(AStockInnovation100Constituent).filter(
            AStockInnovation100Constituent.index_code == INDEX_CODE
        ).delete(synchronize_session=False)
        self.db.query(AStockInnovation100Rebalance).filter(
            AStockInnovation100Rebalance.index_code == INDEX_CODE
        ).delete(synchronize_session=False)
        self.db.query(AStockInnovation100Level).filter(
            AStockInnovation100Level.index_code == INDEX_CODE
        ).delete(synchronize_session=False)
        self.db.commit()

    def rebuild(
        self,
        start_date: date = DEFAULT_START_DATE,
        end_date: Optional[date] = None,
        force_rebuild_outputs: bool = True,
    ) -> Dict:
        start_date = _parse_date(start_date) or DEFAULT_START_DATE
        end_date = _parse_date(end_date) or date.today()
        if start_date > end_date:
            raise ValueError("开始日期不能晚于结束日期")

        self.sync_reference_data(start_date, end_date)
        if force_rebuild_outputs:
            self._progress("清理旧的创新100指数结果", 6)
            self._delete_existing_index_outputs()

        trade_calendar = self.tushare.get_trade_calendar_frame(
            start_date - timedelta(days=RAW_FETCH_LOOKBACK_DAYS),
            end_date,
        )
        if trade_calendar.empty:
            raise RuntimeError("没有获取到交易日历")
        trading_dates = [
            item
            for item in trade_calendar[trade_calendar["is_open"] == 1]["cal_date"].tolist()
            if item <= end_date
        ]
        if not trading_dates:
            raise RuntimeError("指定区间内没有交易日")

        basic_map = self._load_basic_map()
        st_intervals = self._load_st_intervals()
        amount_history: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=LIQUIDITY_WINDOW))
        current_constituents: List[Dict] = []
        current_weight_map: Dict[str, float] = {}
        pending_constituents: Optional[List[Dict]] = None
        pending_effective_date: Optional[date] = None
        levels: List[Dict] = []
        level = BASE_LEVEL
        high_watermark = BASE_LEVEL
        last_market_frame = pd.DataFrame()

        self._ensure_market_days(trading_dates)
        market_day_stats = self._existing_market_day_stats(min(trading_dates), max(trading_dates))
        available_trading_dates = [
            item
            for item in trading_dates
            if not self._market_day_needs_refresh(market_day_stats.get(item))
        ]
        skipped_dates = len(trading_dates) - len(available_trading_dates)
        if skipped_dates:
            self._progress(
                f"跳过{skipped_dates}个尚无完整行情的交易日",
                50,
                skipped_dates=skipped_dates,
                total_dates=len(trading_dates),
            )
        trading_dates = available_trading_dates
        if not trading_dates:
            raise RuntimeError("指定区间内没有完整行情的交易日")

        start_index = next((idx for idx, item in enumerate(trading_dates) if item >= start_date), None)
        if start_index is None:
            raise RuntimeError("开始日期之后没有完整行情的交易日")

        total_dates = len(trading_dates)
        self._progress("载入全市场行情缓存", 50, processed_dates=0, total_dates=total_dates)
        market_frames_by_date = self._load_market_frames_by_date(min(trading_dates), max(trading_dates))

        for idx, current_date in enumerate(trading_dates):
            calc_progress = 50 + int(idx / max(total_dates, 1) * 40)
            if idx == 0 or idx == total_dates - 1 or idx % 20 == 0:
                self._progress(
                    f"计算指数点位 {current_date.isoformat()}",
                    calc_progress,
                    processed_dates=idx + 1,
                    total_dates=total_dates,
                )
            market_frame = market_frames_by_date.get(current_date, pd.DataFrame())
            if market_frame.empty:
                self._ensure_market_day(current_date)
                market_frame = self._load_market_day(current_date)
                if not market_frame.empty and "trade_date" in market_frame.columns:
                    market_frames_by_date[current_date] = market_frame.drop(columns=["trade_date"]).reset_index(drop=True)
            last_market_frame = market_frame
            symbols = np.array([], dtype=str)
            if not market_frame.empty:
                symbols = market_frame["ts_code"].astype(str).to_numpy()
                amounts = np.nan_to_num(market_frame["amount"].to_numpy(dtype=float), nan=0.0)
                for ts_code, amount in zip(symbols, amounts):
                    if ts_code and math.isfinite(amount) and amount >= 0:
                        amount_history[ts_code].append(float(amount))

            if current_date < start_date:
                continue

            is_first_output_day = idx == start_index
            if is_first_output_day:
                ranked = self._rank_candidates(market_frame, current_date, basic_map, st_intervals, amount_history)
                selected = self._select_constituents(ranked, [], reconstitution=True)
                current_constituents, turnover_pct = self._build_weighted_constituents(selected, {})
                current_weight_map = {item["ts_code"]: float(item["weight"]) for item in current_constituents}
                self._save_rebalance(
                    rebalance_date=current_date,
                    effective_date=current_date,
                    rebalance_type="inception",
                    constituents=current_constituents,
                    previous_symbols=[],
                    previous_weight_map={},
                    turnover_pct=turnover_pct,
                )
                daily_return = 0.0
            else:
                if pending_constituents is not None and pending_effective_date == current_date:
                    current_constituents = pending_constituents
                    current_weight_map = {item["ts_code"]: float(item["weight"]) for item in current_constituents}
                    pending_constituents = None
                    pending_effective_date = None

                pct_changes = (
                    np.nan_to_num(market_frame["pct_chg"].to_numpy(dtype=float), nan=0.0) / 100.0
                    if not market_frame.empty
                    else np.array([], dtype=float)
                )
                pct_change_by_symbol = dict(
                    zip(symbols, pct_changes)
                )
                daily_return = sum(
                    float(item.get("weight") or 0.0) * pct_change_by_symbol.get(item["ts_code"], 0.0)
                    for item in current_constituents
                )
                if not math.isfinite(daily_return):
                    daily_return = 0.0
                level *= (1.0 + daily_return)
                high_watermark = max(high_watermark, level)

            drawdown_pct = (level / high_watermark - 1.0) * 100 if high_watermark > 0 else 0.0
            levels.append(
                {
                    "index_code": INDEX_CODE,
                    "date": current_date,
                    "level": _round_or_none(level, 6),
                    "daily_return_pct": _round_or_none(daily_return * 100, 6),
                    "drawdown_pct": _round_or_none(drawdown_pct, 6),
                    "constituent_count": len(current_constituents),
                    "total_circ_mv": _round_or_none(sum(float(item.get("circ_mv") or 0.0) for item in current_constituents), 4),
                    "created_at": datetime.now(),
                    "updated_at": datetime.now(),
                }
            )

            if idx < len(trading_dates) - 1 and not is_first_output_day and self._is_quarter_end(trading_dates, idx):
                ranked = self._rank_candidates(market_frame, current_date, basic_map, st_intervals, amount_history)
                rebalance_type = self._rebalance_type(current_date, is_initial=False)
                selected = self._select_constituents(
                    ranked,
                    [item["ts_code"] for item in current_constituents],
                    reconstitution=rebalance_type == "annual_reconstitution",
                )
                next_constituents, turnover_pct = self._build_weighted_constituents(selected, current_weight_map)
                effective_date = trading_dates[idx + 1]
                self._save_rebalance(
                    rebalance_date=current_date,
                    effective_date=effective_date,
                    rebalance_type=rebalance_type,
                    constituents=next_constituents,
                    previous_symbols=[item["ts_code"] for item in current_constituents],
                    previous_weight_map=current_weight_map,
                    turnover_pct=turnover_pct,
                )
                pending_constituents = next_constituents
                pending_effective_date = effective_date

        self._progress("写入创新100指数点位", 92)
        self._bulk_upsert(AStockInnovation100Level, levels, ["index_code", "date"], batch_size=1000)

        latest_level = levels[-1] if levels else None
        total_return_pct = (
            (float(latest_level["level"]) / BASE_LEVEL - 1.0) * 100
            if latest_level and latest_level.get("level")
            else None
        )
        rebalances_count = self.db.query(AStockInnovation100Rebalance).filter(
            AStockInnovation100Rebalance.index_code == INDEX_CODE
        ).count()
        latest_rebalance = self.db.query(AStockInnovation100Rebalance).filter(
            AStockInnovation100Rebalance.index_code == INDEX_CODE
        ).order_by(AStockInnovation100Rebalance.rebalance_date.desc(), AStockInnovation100Rebalance.id.desc()).first()

        self._progress("A股创新100回跑完成", 100)
        return {
            "index_code": INDEX_CODE,
            "index_name": INDEX_NAME,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "latest_date": latest_level["date"].isoformat() if latest_level else None,
            "latest_level": latest_level.get("level") if latest_level else None,
            "total_return_pct": _round_or_none(total_return_pct, 4),
            "levels_saved": len(levels),
            "rebalances_saved": rebalances_count,
            "latest_rebalance_id": latest_rebalance.id if latest_rebalance else None,
            "latest_rebalance_date": latest_rebalance.rebalance_date.isoformat() if latest_rebalance else None,
            "rule_snapshot": self.rule_snapshot(),
            "last_market_date": trading_dates[-1].isoformat() if trading_dates else None,
        }

    def _load_constituents_for_rebalance(self, rebalance_id: int) -> List[Dict]:
        rows = (
            self.db.query(AStockInnovation100Constituent)
            .filter(
                AStockInnovation100Constituent.index_code == INDEX_CODE,
                AStockInnovation100Constituent.rebalance_id == rebalance_id,
            )
            .order_by(AStockInnovation100Constituent.rank.asc())
            .all()
        )
        return [
            {
                "ts_code": row.ts_code,
                "name": row.name,
                "industry": row.industry,
                "rank": row.rank,
                "raw_weight": float(row.raw_weight_pct or 0.0) / 100.0,
                "weight": float(row.weight_pct or 0.0) / 100.0,
                "total_mv": row.total_mv,
                "circ_mv": row.circ_mv,
                "avg_amount_60d": row.avg_amount_60d,
            }
            for row in rows
        ]

    def _load_incremental_state(self, as_of: date) -> Dict:
        latest_level = (
            self.db.query(AStockInnovation100Level)
            .filter(AStockInnovation100Level.index_code == INDEX_CODE)
            .order_by(AStockInnovation100Level.date.desc())
            .first()
        )
        if not latest_level:
            return {}

        high_watermark_row = (
            self.db.query(AStockInnovation100Level.level)
            .filter(
                AStockInnovation100Level.index_code == INDEX_CODE,
                AStockInnovation100Level.date <= latest_level.date,
            )
            .order_by(AStockInnovation100Level.level.desc())
            .first()
        )
        high_watermark = float(high_watermark_row[0]) if high_watermark_row and high_watermark_row[0] else float(latest_level.level or BASE_LEVEL)

        effective_rebalance = (
            self.db.query(AStockInnovation100Rebalance)
            .filter(
                AStockInnovation100Rebalance.index_code == INDEX_CODE,
                AStockInnovation100Rebalance.effective_date <= latest_level.date,
            )
            .order_by(AStockInnovation100Rebalance.effective_date.desc(), AStockInnovation100Rebalance.id.desc())
            .first()
        )
        if not effective_rebalance:
            return {}

        current_constituents = self._load_constituents_for_rebalance(effective_rebalance.id)
        if not current_constituents:
            return {}

        pending_rebalance = (
            self.db.query(AStockInnovation100Rebalance)
            .filter(
                AStockInnovation100Rebalance.index_code == INDEX_CODE,
                AStockInnovation100Rebalance.rebalance_date <= latest_level.date,
                AStockInnovation100Rebalance.effective_date > latest_level.date,
                AStockInnovation100Rebalance.effective_date <= as_of,
            )
            .order_by(AStockInnovation100Rebalance.effective_date.asc(), AStockInnovation100Rebalance.id.asc())
            .first()
        )
        pending_constituents = self._load_constituents_for_rebalance(pending_rebalance.id) if pending_rebalance else None

        return {
            "latest_level": latest_level,
            "level": float(latest_level.level or BASE_LEVEL),
            "high_watermark": high_watermark,
            "current_constituents": current_constituents,
            "current_weight_map": {item["ts_code"]: float(item.get("weight") or 0.0) for item in current_constituents},
            "pending_constituents": pending_constituents,
            "pending_effective_date": pending_rebalance.effective_date if pending_rebalance else None,
        }

    def refresh_incremental(self, end_date: Optional[date] = None) -> Dict:
        end_date = _parse_date(end_date) or date.today()
        state = self._load_incremental_state(end_date)
        if not state:
            self._progress("未找到可增量续算的创新100结果，执行首次全量回跑", 0)
            return self.rebuild(start_date=DEFAULT_START_DATE, end_date=end_date, force_rebuild_outputs=True)

        latest_level = state["latest_level"]
        latest_date = latest_level.date
        if latest_date >= end_date:
            return {
                "index_code": INDEX_CODE,
                "index_name": INDEX_NAME,
                "mode": "incremental",
                "status": "up_to_date",
                "start_date": latest_date.isoformat(),
                "end_date": end_date.isoformat(),
                "latest_date": latest_date.isoformat(),
                "latest_level": latest_level.level,
                "levels_saved": 0,
                "rebalances_saved": 0,
            }

        calendar_start = latest_date - timedelta(days=RAW_FETCH_LOOKBACK_DAYS)
        trade_calendar = self.tushare.get_trade_calendar_frame(calendar_start, end_date)
        if trade_calendar.empty:
            raise RuntimeError("没有获取到交易日历")

        trading_dates = [
            item
            for item in trade_calendar[trade_calendar["is_open"] == 1]["cal_date"].tolist()
            if item <= end_date
        ]
        if not trading_dates:
            raise RuntimeError("指定区间内没有交易日")

        self.sync_reference_data_incremental(max(DEFAULT_START_DATE, latest_date - timedelta(days=30)), end_date)
        self._ensure_market_days(trading_dates)
        market_day_stats = self._existing_market_day_stats(min(trading_dates), max(trading_dates))
        trading_dates = [
            item
            for item in trading_dates
            if not self._market_day_needs_refresh(market_day_stats.get(item))
        ]
        new_trading_dates = [item for item in trading_dates if item > latest_date]
        if not new_trading_dates:
            return {
                "index_code": INDEX_CODE,
                "index_name": INDEX_NAME,
                "mode": "incremental",
                "status": "up_to_date",
                "start_date": latest_date.isoformat(),
                "end_date": end_date.isoformat(),
                "latest_date": latest_date.isoformat(),
                "latest_level": latest_level.level,
                "levels_saved": 0,
                "rebalances_saved": 0,
            }

        basic_map = self._load_basic_map()
        st_intervals = self._load_st_intervals()
        amount_history: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=LIQUIDITY_WINDOW))
        current_constituents: List[Dict] = state["current_constituents"]
        current_weight_map: Dict[str, float] = state["current_weight_map"]
        pending_constituents: Optional[List[Dict]] = state["pending_constituents"]
        pending_effective_date: Optional[date] = state["pending_effective_date"]
        level = float(state["level"])
        high_watermark = float(state["high_watermark"])
        levels: List[Dict] = []
        rebalances_before = self.db.query(AStockInnovation100Rebalance).filter(
            AStockInnovation100Rebalance.index_code == INDEX_CODE
        ).count()

        self._progress(
            "载入创新100增量行情缓存",
            35,
            latest_date=latest_date.isoformat(),
            end_date=end_date.isoformat(),
        )
        market_frames_by_date = self._load_market_frames_by_date(min(trading_dates), max(trading_dates))

        for warmup_date in [item for item in trading_dates if item <= latest_date]:
            market_frame = market_frames_by_date.get(warmup_date, pd.DataFrame())
            if market_frame.empty:
                continue
            symbols = market_frame["ts_code"].astype(str).to_numpy()
            amounts = np.nan_to_num(market_frame["amount"].to_numpy(dtype=float), nan=0.0)
            for ts_code, amount in zip(symbols, amounts):
                if ts_code and math.isfinite(amount) and amount >= 0:
                    amount_history[ts_code].append(float(amount))

        total_dates = len(new_trading_dates)
        for output_index, current_date in enumerate(new_trading_dates):
            calc_progress = 50 + int(output_index / max(total_dates, 1) * 40)
            self._progress(
                f"增量计算创新100指数点位 {current_date.isoformat()}",
                calc_progress,
                processed_dates=output_index + 1,
                total_dates=total_dates,
            )
            market_frame = market_frames_by_date.get(current_date, pd.DataFrame())
            if market_frame.empty:
                self._ensure_market_day(current_date)
                market_frame = self._load_market_day(current_date)
                if not market_frame.empty and "trade_date" in market_frame.columns:
                    market_frames_by_date[current_date] = market_frame.drop(columns=["trade_date"]).reset_index(drop=True)

            symbols = np.array([], dtype=str)
            if not market_frame.empty:
                symbols = market_frame["ts_code"].astype(str).to_numpy()
                amounts = np.nan_to_num(market_frame["amount"].to_numpy(dtype=float), nan=0.0)
                for ts_code, amount in zip(symbols, amounts):
                    if ts_code and math.isfinite(amount) and amount >= 0:
                        amount_history[ts_code].append(float(amount))

            if pending_constituents is not None and pending_effective_date == current_date:
                current_constituents = pending_constituents
                current_weight_map = {item["ts_code"]: float(item["weight"]) for item in current_constituents}
                pending_constituents = None
                pending_effective_date = None

            pct_changes = (
                np.nan_to_num(market_frame["pct_chg"].to_numpy(dtype=float), nan=0.0) / 100.0
                if not market_frame.empty
                else np.array([], dtype=float)
            )
            pct_change_by_symbol = dict(zip(symbols, pct_changes))
            daily_return = sum(
                float(item.get("weight") or 0.0) * pct_change_by_symbol.get(item["ts_code"], 0.0)
                for item in current_constituents
            )
            if not math.isfinite(daily_return):
                daily_return = 0.0
            level *= (1.0 + daily_return)
            high_watermark = max(high_watermark, level)
            drawdown_pct = (level / high_watermark - 1.0) * 100 if high_watermark > 0 else 0.0
            levels.append(
                {
                    "index_code": INDEX_CODE,
                    "date": current_date,
                    "level": _round_or_none(level, 6),
                    "daily_return_pct": _round_or_none(daily_return * 100, 6),
                    "drawdown_pct": _round_or_none(drawdown_pct, 6),
                    "constituent_count": len(current_constituents),
                    "total_circ_mv": _round_or_none(sum(float(item.get("circ_mv") or 0.0) for item in current_constituents), 4),
                    "created_at": datetime.now(),
                    "updated_at": datetime.now(),
                }
            )

            current_index = trading_dates.index(current_date)
            if current_index < len(trading_dates) - 1 and self._is_quarter_end(trading_dates, current_index):
                existing_rebalance = (
                    self.db.query(AStockInnovation100Rebalance)
                    .filter(
                        AStockInnovation100Rebalance.index_code == INDEX_CODE,
                        AStockInnovation100Rebalance.rebalance_date == current_date,
                    )
                    .first()
                )
                if not existing_rebalance:
                    ranked = self._rank_candidates(market_frame, current_date, basic_map, st_intervals, amount_history)
                    rebalance_type = self._rebalance_type(current_date, is_initial=False)
                    selected = self._select_constituents(
                        ranked,
                        [item["ts_code"] for item in current_constituents],
                        reconstitution=rebalance_type == "annual_reconstitution",
                    )
                    next_constituents, turnover_pct = self._build_weighted_constituents(selected, current_weight_map)
                    effective_date = trading_dates[current_index + 1]
                    self._save_rebalance(
                        rebalance_date=current_date,
                        effective_date=effective_date,
                        rebalance_type=rebalance_type,
                        constituents=next_constituents,
                        previous_symbols=[item["ts_code"] for item in current_constituents],
                        previous_weight_map=current_weight_map,
                        turnover_pct=turnover_pct,
                    )
                    pending_constituents = next_constituents
                    pending_effective_date = effective_date

        self._progress("写入创新100增量指数点位", 92)
        self._bulk_upsert(AStockInnovation100Level, levels, ["index_code", "date"], batch_size=1000)

        latest_saved = levels[-1] if levels else latest_level
        rebalances_after = self.db.query(AStockInnovation100Rebalance).filter(
            AStockInnovation100Rebalance.index_code == INDEX_CODE
        ).count()
        self._progress("A股创新100增量刷新完成", 100)
        return {
            "index_code": INDEX_CODE,
            "index_name": INDEX_NAME,
            "mode": "incremental",
            "status": "completed",
            "start_date": new_trading_dates[0].isoformat(),
            "end_date": end_date.isoformat(),
            "latest_date": latest_saved["date"].isoformat() if isinstance(latest_saved, dict) else latest_saved.date.isoformat(),
            "latest_level": latest_saved.get("level") if isinstance(latest_saved, dict) else latest_saved.level,
            "levels_saved": len(levels),
            "rebalances_saved": rebalances_after - rebalances_before,
            "last_market_date": new_trading_dates[-1].isoformat(),
        }


def rebuild_a_stock_innovation100(
    db: Session,
    start_date: date = DEFAULT_START_DATE,
    end_date: Optional[date] = None,
    progress_callback: Optional[ProgressCallback] = None,
) -> Dict:
    builder = AStockInnovation100Builder(db, progress_callback=progress_callback)
    return builder.rebuild(start_date=start_date, end_date=end_date, force_rebuild_outputs=True)


def _upsert_index_daily_frame(db: Session, frame: pd.DataFrame):
    if frame.empty:
        return

    now = datetime.now()
    mappings = []
    for _, row in frame.iterrows():
        ts_code = str(row.get("ts_code") or "").strip()
        trade_date = _parse_date(row.get("trade_date"))
        if not ts_code or not trade_date:
            continue
        mappings.append(
            {
                "ts_code": ts_code,
                "trade_date": trade_date,
                "open": _round_or_none(row.get("open"), 6),
                "high": _round_or_none(row.get("high"), 6),
                "low": _round_or_none(row.get("low"), 6),
                "close": _round_or_none(row.get("close"), 6),
                "pre_close": _round_or_none(row.get("pre_close"), 6),
                "change": _round_or_none(row.get("change"), 6),
                "pct_chg": _round_or_none(row.get("pct_chg"), 6),
                "vol": _round_or_none(row.get("vol"), 4),
                "amount": _round_or_none(row.get("amount"), 4),
                "created_at": now,
                "updated_at": now,
            }
        )

    if not mappings:
        return

    table = AStockIndexDaily.__table__
    for batch in AStockInnovation100Builder._chunks(mappings, 1000):
        stmt = sqlite_insert(table).values(batch)
        update_columns = {
            column.name: getattr(stmt.excluded, column.name)
            for column in table.columns
            if column.name not in {"ts_code", "trade_date"} and not column.primary_key
        }
        db.execute(stmt.on_conflict_do_update(index_elements=["ts_code", "trade_date"], set_=update_columns))
    db.commit()


def sync_benchmark_index_daily(
    db: Session,
    start_date: date,
    end_date: date,
    tushare_service: Optional[TushareService] = None,
):
    start_value = _parse_date(start_date)
    end_value = _parse_date(end_date)
    if not start_value or not end_value or start_value > end_value:
        return

    tushare = tushare_service or TushareService.getInstance()
    for benchmark in BENCHMARK_INDEXES:
        ts_code = benchmark["ts_code"]
        first_date = (
            db.query(AStockIndexDaily.trade_date)
            .filter(AStockIndexDaily.ts_code == ts_code)
            .order_by(AStockIndexDaily.trade_date.asc())
            .first()
        )
        last_date = (
            db.query(AStockIndexDaily.trade_date)
            .filter(AStockIndexDaily.ts_code == ts_code)
            .order_by(AStockIndexDaily.trade_date.desc())
            .first()
        )
        cached_start = first_date[0] if first_date else None
        cached_end = last_date[0] if last_date else None

        fetch_ranges: List[Tuple[date, date]] = []
        if not cached_start or not cached_end:
            fetch_ranges.append((start_value, end_value))
        else:
            if cached_start > start_value:
                fetch_ranges.append((start_value, cached_start - timedelta(days=1)))
            if cached_end < end_value:
                fetch_ranges.append((cached_end + timedelta(days=1), end_value))

        for range_start, range_end in fetch_ranges:
            if range_start > range_end:
                continue
            frame = tushare.get_index_daily_range_frame(ts_code, range_start, range_end)
            _upsert_index_daily_frame(db, frame)


def load_benchmark_index_curves(db: Session, start_date: date, end_date: date) -> List[Dict]:
    start_value = _parse_date(start_date)
    end_value = _parse_date(end_date)
    if not start_value or not end_value or start_value > end_value:
        return []

    sync_benchmark_index_daily(db, start_value, end_value)
    curves = []
    for benchmark in BENCHMARK_INDEXES:
        ts_code = benchmark["ts_code"]
        rows = (
            db.query(AStockIndexDaily)
            .filter(
                AStockIndexDaily.ts_code == ts_code,
                AStockIndexDaily.trade_date >= start_value,
                AStockIndexDaily.trade_date <= end_value,
            )
            .order_by(AStockIndexDaily.trade_date.asc())
            .all()
        )
        base_close = next((float(row.close) for row in rows if _is_finite_positive(row.close)), None)
        levels = []
        for row in rows:
            close = float(row.close) if _is_finite_positive(row.close) else None
            levels.append(
                {
                    "date": row.trade_date.isoformat(),
                    "close": _round_or_none(close, 6),
                    "level": _round_or_none(close / base_close * BASE_LEVEL, 6) if close and base_close else None,
                    "daily_return_pct": _round_or_none(row.pct_chg, 6),
                }
            )
        curves.append(
            {
                "ts_code": ts_code,
                "name": benchmark["name"],
                "base_level": BASE_LEVEL,
                "levels": levels,
            }
        )
    return curves


def load_a_stock_innovation100_summary(db: Session) -> Dict:
    latest_level = db.query(AStockInnovation100Level).filter(
        AStockInnovation100Level.index_code == INDEX_CODE
    ).order_by(AStockInnovation100Level.date.desc()).first()
    first_level = db.query(AStockInnovation100Level).filter(
        AStockInnovation100Level.index_code == INDEX_CODE
    ).order_by(AStockInnovation100Level.date.asc()).first()
    latest_rebalance = db.query(AStockInnovation100Rebalance).filter(
        AStockInnovation100Rebalance.index_code == INDEX_CODE
    ).order_by(AStockInnovation100Rebalance.rebalance_date.desc(), AStockInnovation100Rebalance.id.desc()).first()
    if not latest_level or not first_level:
        return {
            "index_code": INDEX_CODE,
            "index_name": INDEX_NAME,
            "rule_snapshot": AStockInnovation100Builder.rule_snapshot(),
            "has_data": False,
        }

    level_rows = db.query(AStockInnovation100Level).filter(
        AStockInnovation100Level.index_code == INDEX_CODE
    ).order_by(AStockInnovation100Level.date.asc()).all()
    values = [float(row.level) for row in level_rows if row.level]
    returns = [
        float(row.daily_return_pct) / 100.0
        for row in level_rows
        if row.daily_return_pct is not None
    ]
    total_return_pct = (latest_level.level / first_level.level - 1.0) * 100 if first_level.level else None
    years = max((latest_level.date - first_level.date).days / 365.25, 1 / 365.25)
    annualized_return_pct = ((latest_level.level / first_level.level) ** (1 / years) - 1.0) * 100 if first_level.level else None
    volatility_pct = float(np.std(returns, ddof=1) * math.sqrt(252) * 100) if len(returns) > 1 else None
    sharpe = (annualized_return_pct / volatility_pct) if volatility_pct and volatility_pct > 0 else None
    max_drawdown_pct = min((row.drawdown_pct or 0.0) for row in level_rows)
    return {
        "index_code": INDEX_CODE,
        "index_name": INDEX_NAME,
        "has_data": True,
        "start_date": first_level.date.isoformat(),
        "latest_date": latest_level.date.isoformat(),
        "latest_level": _round_or_none(latest_level.level, 4),
        "total_return_pct": _round_or_none(total_return_pct, 4),
        "annualized_return_pct": _round_or_none(annualized_return_pct, 4),
        "annualized_volatility_pct": _round_or_none(volatility_pct, 4),
        "sharpe_ratio": _round_or_none(sharpe, 4),
        "max_drawdown_pct": _round_or_none(max_drawdown_pct, 4),
        "constituent_count": latest_level.constituent_count,
        "rebalances_count": db.query(AStockInnovation100Rebalance).filter(
            AStockInnovation100Rebalance.index_code == INDEX_CODE
        ).count(),
        "latest_rebalance_id": latest_rebalance.id if latest_rebalance else None,
        "latest_rebalance_date": latest_rebalance.rebalance_date.isoformat() if latest_rebalance else None,
        "latest_effective_date": latest_rebalance.effective_date.isoformat() if latest_rebalance and latest_rebalance.effective_date else None,
        "rule_snapshot": AStockInnovation100Builder.rule_snapshot(),
    }


def compute_yearly_returns(level_rows: List[AStockInnovation100Level]) -> List[Dict]:
    by_year: Dict[int, List[AStockInnovation100Level]] = defaultdict(list)
    for row in level_rows:
        by_year[row.date.year].append(row)
    results = []
    for year in sorted(by_year):
        rows = sorted(by_year[year], key=lambda item: item.date)
        if not rows or not rows[0].level:
            continue
        start_value = float(rows[0].level)
        end_value = float(rows[-1].level)
        results.append(
            {
                "year": year,
                "start_date": rows[0].date.isoformat(),
                "end_date": rows[-1].date.isoformat(),
                "start_level": _round_or_none(start_value, 4),
                "end_level": _round_or_none(end_value, 4),
                "return_pct": _round_or_none((end_value / start_value - 1.0) * 100, 4),
                "max_drawdown_pct": _round_or_none(min((row.drawdown_pct or 0.0) for row in rows), 4),
            }
        )
    return results

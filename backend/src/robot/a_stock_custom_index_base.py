"""自算A股指数（A创100、微盘400等）的公共管道。

这里只放与编制规则无关的部分：全市场行情缓存的分窗加载、交易日筛选、基础信息/ST
区间、成分权重的价格漂移，以及进度回调和批量 upsert。具体的样本空间、排序口径、
加权方式和调仓节奏由各自的 builder 子类实现。
"""
import logging
import math
from collections import defaultdict
from datetime import date, datetime
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import pandas as pd
import polars as pl
from sqlalchemy import text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..core.analytics_database import (
    AStockBasic,
    AStockIndexDaily,
    AStockNameChange,
    AnalyticsSession,
    analytics_engine,
)
from .a_stock_base_data_config import (
    BENCHMARK_INDEXES,
    MAX_MARKET_DAILY_OHL_ZERO_PCT,
    MIN_MARKET_DAILY_ROWS,
)

ProgressCallback = Callable[[Dict], None]
# 自算指数统一以 1000 点起算，基准曲线也归一到同一起点方便叠图对比。
BASE_LEVEL = 1000.0


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


def _is_market_frame_empty(frame: Optional[pl.DataFrame]) -> bool:
    return frame is None or frame.is_empty()


class CustomIndexBuilderBase:
    """自算指数 builder 的公共基类。"""

    # 子类按需覆盖：一次从 DuckDB 读入多少个交易日，以及范围行情需要哪些列。
    MARKET_FRAME_LOAD_DAYS = 20
    MARKET_RANGE_COLUMNS: Tuple[str, ...] = (
        "close",
        "pct_chg",
        "amount",
        "total_mv",
        "circ_mv",
    )

    def __init__(
        self,
        db: Session,
        analytics_db: Optional[Session] = None,
        progress_callback: Optional[ProgressCallback] = None,
    ):
        self.db = db
        self.analytics_db = analytics_db or AnalyticsSession()
        self._owns_analytics_db = analytics_db is None
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

    def _cached_market_trading_dates(self, start_date: date, end_date: date) -> List[date]:
        rows = self.analytics_db.execute(
            text("""
                SELECT trade_date
                FROM a_stock_market_daily
                WHERE trade_date >= :start_date AND trade_date <= :end_date
                GROUP BY trade_date
                ORDER BY trade_date
            """),
            {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
        ).fetchall()
        return [
            parsed
            for parsed in (_parse_date(row[0]) for row in rows)
            if parsed
        ]

    @staticmethod
    def _partition_market_frames(frame: pl.DataFrame) -> Dict[date, pl.DataFrame]:
        if frame.is_empty():
            return {}
        return {
            key[0]: group.drop("trade_date")
            for key, group in frame.partition_by("trade_date", as_dict=True, maintain_order=True).items()
        }

    def _load_market_range_frame(self, start_date: date, end_date: date) -> pl.DataFrame:
        columns = ["trade_date", "ts_code", *self.MARKET_RANGE_COLUMNS]
        sql = f"""
            SELECT {', '.join(columns)}
            FROM a_stock_market_daily
            WHERE trade_date >= :start_date AND trade_date <= :end_date
            ORDER BY trade_date
        """
        return self._read_analytics_frame(sql, {"start_date": start_date, "end_date": end_date}, columns)

    def _load_market_frames_by_date(self, start_date: date, end_date: date) -> Dict[date, pl.DataFrame]:
        return self._partition_market_frames(self._load_market_range_frame(start_date, end_date))

    @staticmethod
    def _read_analytics_frame(sql: str, params: Dict, columns: List[str]) -> pl.DataFrame:
        with analytics_engine.connect() as connection:
            rows = connection.execute(text(sql), params).fetchall()
        if not rows:
            return pl.DataFrame(schema=columns)
        return pl.DataFrame([tuple(row) for row in rows], schema=columns, orient="row", infer_schema_length=None)

    def _iter_market_frames_by_date(
        self,
        trading_dates: List[date],
        load_message: str,
        progress_start: Optional[int] = None,
        progress_end: Optional[int] = None,
    ) -> Iterable[Tuple[int, date, pl.DataFrame]]:
        total_dates = len(trading_dates)
        if not total_dates:
            return

        load_days = max(1, int(self.MARKET_FRAME_LOAD_DAYS))
        total_chunks = math.ceil(total_dates / load_days)
        for chunk_index, chunk in enumerate(self._chunks(trading_dates, load_days), start=1):
            chunk_start = chunk[0]
            chunk_end = chunk[-1]
            if total_chunks > 1 and progress_start is not None and progress_end is not None:
                chunk_offset = (chunk_index - 1) * load_days
                chunk_progress = progress_start + int(chunk_offset / max(total_dates, 1) * (progress_end - progress_start))
                self._progress(
                    f"{load_message} {chunk_start.isoformat()} ~ {chunk_end.isoformat()}",
                    chunk_progress,
                    processed_chunks=chunk_index,
                    total_chunks=total_chunks,
                    window_days=len(chunk),
                )
            frames_by_date = self._load_market_frames_by_date(chunk_start, chunk_end)
            chunk_offset = (chunk_index - 1) * load_days
            for offset, current_date in enumerate(chunk):
                yield chunk_offset + offset, current_date, frames_by_date.get(current_date, pl.DataFrame())
            del frames_by_date

    def _load_basic_map(self) -> Dict[str, Dict]:
        rows = self.analytics_db.query(AStockBasic).all()
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
        rows = self.analytics_db.query(AStockNameChange).all()
        for row in rows:
            name = (row.name or "").upper()
            reason = (row.change_reason or "").upper()
            if "ST" not in name and "ST" not in reason and "退" not in name and "终止上市" not in reason:
                continue
            start = row.start_date or date(1900, 1, 1)
            end = row.end_date or date(2099, 12, 31)
            intervals[row.ts_code].append((start, end))
        return intervals

    def _is_st_or_retiring(
        self,
        ts_code: str,
        as_of: date,
        basic: Optional[Dict],
        intervals: Dict[str, List[Tuple[date, date]]],
    ) -> bool:
        name = str((basic or {}).get("name") or "").upper()
        if "ST" in name or "退" in name:
            return True
        for start, end in intervals.get(ts_code, []):
            if start <= as_of <= end:
                return True
        return False

    @staticmethod
    def _is_delisted(basic: Optional[Dict], as_of: date) -> bool:
        delist_date = (basic or {}).get("delist_date")
        return bool(delist_date and delist_date <= as_of)

    @staticmethod
    def _update_amount_history(
        market_frame: pl.DataFrame,
        amount_history: Dict[str, "object"],
    ) -> None:
        if _is_market_frame_empty(market_frame) or not {"ts_code", "amount"}.issubset(set(market_frame.columns)):
            return
        frame = market_frame.select(["ts_code", "amount"]).filter(
            pl.col("ts_code").is_not_null()
            & pl.col("amount").is_not_null()
            & (pl.col("amount") >= 0)
        )
        for ts_code, amount in frame.iter_rows():
            if ts_code and math.isfinite(float(amount)):
                amount_history[str(ts_code)].append(float(amount))

    @staticmethod
    def _daily_returns_by_symbol(market_frame: pl.DataFrame, symbols: Iterable[str]) -> Dict[str, float]:
        """当日成分股收益率；停牌（当日无行情）不出现在返回值里，由调用方按 0 收益处理。"""
        wanted = list(symbols)
        if _is_market_frame_empty(market_frame) or not wanted:
            return {}
        if not {"ts_code", "pct_chg"}.issubset(set(market_frame.columns)):
            return {}
        frame = market_frame.select(["ts_code", "pct_chg"]).filter(
            pl.col("ts_code").is_in(wanted) & pl.col("pct_chg").is_not_null()
        )
        returns: Dict[str, float] = {}
        for ts_code, pct_chg in frame.iter_rows():
            try:
                pct_value = float(pct_chg)
            except (TypeError, ValueError):
                continue
            if math.isfinite(pct_value):
                returns[str(ts_code)] = pct_value / 100.0
        return returns

    @classmethod
    def _advance_weights(
        cls,
        market_frame: pl.DataFrame,
        weight_map: Dict[str, float],
        basic_map: Optional[Dict[str, Dict]] = None,
        as_of: Optional[date] = None,
    ) -> Tuple[float, Dict[str, float]]:
        """算出当日加权收益，并把权重按价格涨跌漂移到下一交易日。

        指数在两次调仓之间不会重置权重，成分股涨得多权重自然变大。之前的实现把权重
        固定住，等价于每天都做一次再平衡，会系统性低估调仓周期较长的指数。停牌当天
        按 0 收益处理（权重保留），已退市的成分剔除后对剩余权重归一。
        """
        if not weight_map:
            return 0.0, {}

        active = weight_map
        if basic_map is not None and as_of is not None:
            active = {
                symbol: weight
                for symbol, weight in weight_map.items()
                if not cls._is_delisted(basic_map.get(symbol), as_of)
            }
        active = {symbol: float(weight) for symbol, weight in active.items() if float(weight) > 0}
        active_sum = sum(active.values())
        if active_sum <= 0:
            return 0.0, {}
        if not math.isclose(active_sum, 1.0, rel_tol=0, abs_tol=1e-9):
            active = {symbol: weight / active_sum for symbol, weight in active.items()}

        returns = cls._daily_returns_by_symbol(market_frame, active.keys())
        daily_return = sum(weight * returns.get(symbol, 0.0) for symbol, weight in active.items())
        if not math.isfinite(daily_return):
            daily_return = 0.0

        drifted = {
            symbol: weight * (1.0 + returns.get(symbol, 0.0))
            for symbol, weight in active.items()
        }
        drifted_sum = sum(drifted.values())
        if drifted_sum <= 0 or not math.isfinite(drifted_sum):
            return daily_return, active
        return daily_return, {symbol: weight / drifted_sum for symbol, weight in drifted.items()}

    @staticmethod
    def _is_period_end(trading_dates: List[date], index: int, period: str) -> bool:
        """当前交易日是否为所在周期（月/季）的最后一个交易日。"""
        if index >= len(trading_dates) - 1:
            return True
        current = trading_dates[index]
        next_date = trading_dates[index + 1]
        if period == "month":
            return (current.year, current.month) != (next_date.year, next_date.month)
        if period == "quarter":
            return (current.year, (current.month - 1) // 3) != (next_date.year, (next_date.month - 1) // 3)
        raise ValueError(f"unsupported period: {period}")


def load_benchmark_index_curves(db: Session, start_date: date, end_date: date) -> List[Dict]:
    start_value = _parse_date(start_date)
    end_value = _parse_date(end_date)
    if not start_value or not end_value or start_value > end_value:
        return []

    analytics_db = AnalyticsSession()
    try:
        curves = []
        for benchmark in BENCHMARK_INDEXES:
            ts_code = benchmark["ts_code"]
            rows = (
                analytics_db.query(AStockIndexDaily)
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
    finally:
        AnalyticsSession.remove()


def compute_yearly_returns(level_rows: List) -> List[Dict]:
    """自算指数点位行的年度收益/回撤，只要求行上有 date / level / drawdown_pct。"""
    by_year: Dict[int, List] = defaultdict(list)
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

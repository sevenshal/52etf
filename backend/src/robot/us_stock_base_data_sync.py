import logging
import os
import re
import time
from io import StringIO
from copy import deepcopy
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from urllib.request import Request, urlopen

import pandas as pd
from sqlalchemy import distinct, or_

from ..core.analytics_database import USStockDaily
from ..core.duckdb_utils import ANALYTICS_DB_PATH, connect_duckdb
from ..core.database import ETFHolding, Session, StockEVC, StockStaticInfoHistory, StockStaticInfoSnapshot
from ..core.services.longport import LongPortKlineQuotaExceeded, LongPortService
from ..core.static_info import STATIC_INFO_FIELDS
from ..core.utils import normalize_us_equity_symbol


logger = logging.getLogger(__name__)

ProgressCallback = Callable[[Dict], None]

US_STOCK_STATIC_INFO_BATCH_SIZE = max(1, int(os.getenv("US_STOCK_STATIC_INFO_BATCH_SIZE", "500")))
US_STOCK_DAILY_INSERT_BATCH_SYMBOLS = max(1, int(os.getenv("US_STOCK_DAILY_INSERT_BATCH_SYMBOLS", "25")))
US_STOCK_DAILY_REFRESH_OVERLAP_DAYS = max(0, int(os.getenv("US_STOCK_DAILY_REFRESH_OVERLAP_DAYS", "7")))
US_STOCK_DAILY_MAX_SYMBOLS = max(0, int(os.getenv("US_STOCK_DAILY_MAX_SYMBOLS", "0")))
US_STOCK_DAILY_ADJUST_PRICE_ABS_TOL = max(
    0.0,
    float(os.getenv("US_STOCK_DAILY_ADJUST_PRICE_ABS_TOL", "0.0001")),
)
US_STOCK_DAILY_ADJUST_PRICE_REL_TOL = max(
    0.0,
    float(os.getenv("US_STOCK_DAILY_ADJUST_PRICE_REL_TOL", "0.000001")),
)
US_STOCK_BASE_DATA_CANDIDATE_ETFS = [
    item.strip().upper()
    for item in os.getenv("US_STOCK_BASE_DATA_CANDIDATE_ETFS", "SPY.US,QQQ.US,GLD.US").split(",")
    if item.strip()
]
NASDAQ_TRADER_NASDAQ_LISTED_URL = os.getenv(
    "NASDAQ_TRADER_NASDAQ_LISTED_URL",
    "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
)
NASDAQ_TRADER_OTHER_LISTED_URL = os.getenv(
    "NASDAQ_TRADER_OTHER_LISTED_URL",
    "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
)
NASDAQ_TRADER_TIMEOUT_SECONDS = max(1, int(os.getenv("NASDAQ_TRADER_TIMEOUT_SECONDS", "20")))

EXCHANGE_NAME_MAP = {
    "A": "NYSE American",
    "N": "NYSE",
    "P": "NYSE Arca",
    "V": "IEX",
    "Z": "Cboe BZX",
}
NON_COMPANY_SECURITY_NAME_PATTERN = re.compile(
    r"\b(warrants?|rights?|units?|preferred|preference|notes?|bonds?|debentures?)\b",
    re.IGNORECASE,
)
US_STOCK_DAILY_COLUMNS = [
    "symbol",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
    "adjust_type",
    "period",
    "created_at",
    "updated_at",
]
US_STOCK_DAILY_PRICE_FIELDS = ("open", "high", "low", "close")


def _default_start_date() -> date:
    value = os.getenv("US_STOCK_BASE_DATA_SYNC_START_DATE", "2020-01-01")
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        logger.warning("Invalid US_STOCK_BASE_DATA_SYNC_START_DATE=%s, fallback to 2020-01-01", value)
        return date(2020, 1, 1)


DEFAULT_START_DATE = _default_start_date()


def _quote_duckdb_identifier(identifier: str) -> str:
    return f'"{identifier.replace(chr(34), chr(34) * 2)}"'


def _chunks(items: Sequence, size: int):
    for index in range(0, len(items), size):
        yield items[index:index + size]


def _normalize_static_info_payload(static_info: Dict) -> Dict:
    stock_derivatives = static_info.get("stock_derivatives") or []
    if not isinstance(stock_derivatives, (list, tuple, set)):
        stock_derivatives = [stock_derivatives] if stock_derivatives else []
    normalized_derivatives = sorted(
        dict.fromkeys(
            str(item)
            for item in stock_derivatives
            if item is not None and str(item)
        )
    )
    return {
        "symbol": normalize_us_equity_symbol(static_info.get("symbol")),
        "name_cn": static_info.get("name_cn"),
        "name_en": static_info.get("name_en"),
        "name_hk": static_info.get("name_hk"),
        "exchange": static_info.get("exchange"),
        "currency": static_info.get("currency"),
        "lot_size": static_info.get("lot_size"),
        "total_shares": static_info.get("total_shares"),
        "circulating_shares": static_info.get("circulating_shares"),
        "hk_shares": static_info.get("hk_shares"),
        "eps": static_info.get("eps"),
        "eps_ttm": static_info.get("eps_ttm"),
        "bps": static_info.get("bps"),
        "dividend_yield": static_info.get("dividend_yield"),
        "stock_derivatives": normalized_derivatives,
        "board": static_info.get("board"),
    }


def _payload_from_record(record) -> Dict:
    return {field: deepcopy(getattr(record, field, None)) for field in STATIC_INFO_FIELDS}


def _create_static_info_model(model_cls, payload: Dict, record_date: date, now: datetime, created_at: Optional[datetime] = None):
    kwargs = {field: deepcopy(payload.get(field)) for field in STATIC_INFO_FIELDS}
    kwargs.update({
        "date": record_date,
        "raw_data": deepcopy(payload),
        "created_at": created_at or now,
        "updated_at": now,
    })
    return model_cls(**kwargs)


def _insert_or_replace_frame(
    table_name: str,
    columns: List[str],
    frame: pd.DataFrame,
    replace_ranges: Optional[List[Tuple[str, date, date]]] = None,
):
    if frame.empty:
        return

    insert_frame = frame.loc[:, columns]
    quoted_table = _quote_duckdb_identifier(table_name)
    quoted_columns = ", ".join(_quote_duckdb_identifier(column) for column in columns)
    temp_frame_name = "us_stock_insert_frame"
    temp_replace_ranges_name = "us_stock_replace_ranges"
    insert_sql = (
        f"INSERT OR REPLACE INTO {quoted_table} ({quoted_columns}) "
        f"SELECT {quoted_columns} FROM {_quote_duckdb_identifier(temp_frame_name)}"
    )

    connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=False)
    try:
        connection.execute("BEGIN TRANSACTION")
        if replace_ranges:
            replace_frame = pd.DataFrame(
                replace_ranges,
                columns=["symbol", "start_date", "end_date"],
            )
            connection.register(temp_replace_ranges_name, replace_frame)
            quoted_ranges = _quote_duckdb_identifier(temp_replace_ranges_name)
            connection.execute(
                f"""
                DELETE FROM {quoted_table}
                USING {quoted_ranges}
                WHERE {quoted_table}.symbol = {quoted_ranges}.symbol
                  AND {quoted_table}.trade_date >= {quoted_ranges}.start_date
                  AND {quoted_table}.trade_date <= {quoted_ranges}.end_date
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


def _daily_date_bounds() -> Dict[str, Tuple[date, date]]:

    connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
    try:
        rows = connection.execute(
            """
            SELECT
                symbol,
                MIN(trade_date) AS earliest_date,
                MAX(trade_date) AS latest_date
            FROM us_stock_daily
            GROUP BY symbol
            """
        ).fetchall()
    finally:
        connection.close()
    return {
        str(symbol): (earliest_date, latest_date)
        for symbol, earliest_date, latest_date in rows
        if symbol and isinstance(earliest_date, date) and isinstance(latest_date, date)
    }


def _existing_daily_prices(symbol: str, start_date: date, end_date: date) -> Dict[date, Dict]:

    connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
    try:
        rows = connection.execute(
            """
            SELECT trade_date, open, high, low, close
            FROM us_stock_daily
            WHERE symbol = ?
              AND trade_date >= ?
              AND trade_date <= ?
            """,
            [symbol, start_date, end_date],
        ).fetchall()
    finally:
        connection.close()

    result: Dict[date, Dict] = {}
    for trade_date, open_value, high_value, low_value, close_value in rows:
        normalized_date = _kline_trade_date(trade_date)
        if not normalized_date:
            continue
        result[normalized_date] = {
            "open": open_value,
            "high": high_value,
            "low": low_value,
            "close": close_value,
        }
    return result


def _count_table_rows(table_name: str) -> int:

    connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
    try:
        row = connection.execute(
            f"SELECT COUNT(*) FROM {_quote_duckdb_identifier(table_name)}"
        ).fetchone()
    finally:
        connection.close()
    return int(row[0] if row else 0)


def _evc_static_info_symbols(batch_size: int = US_STOCK_STATIC_INFO_BATCH_SIZE) -> List[str]:
    db = Session()
    symbols: List[str] = []
    offset = 0
    try:
        while True:
            rows = (
                db.query(StockEVC.symbol)
                .distinct()
                .order_by(StockEVC.symbol)
                .offset(offset)
                .limit(batch_size)
                .all()
            )
            batch_symbols = [
                normalize_us_equity_symbol(row[0])
                for row in rows
                if row and row[0]
            ]
            batch_symbols = [symbol for symbol in batch_symbols if symbol]
            if not batch_symbols:
                break
            symbols.extend(batch_symbols)
            if len(rows) < batch_size:
                break
            offset += batch_size
    finally:
        db.close()

    return sorted(dict.fromkeys(symbols))


def _candidate_etf_component_symbols(candidate_etfs: Optional[Sequence[str]] = None) -> List[str]:
    etf_symbols = [
        normalize_us_equity_symbol(item)
        for item in (candidate_etfs or US_STOCK_BASE_DATA_CANDIDATE_ETFS)
    ]
    etf_symbols = [item for item in dict.fromkeys(etf_symbols) if item]
    if not etf_symbols:
        return []

    db = Session()
    try:
        rows = (
            db.query(distinct(ETFHolding.symbol))
            .filter(
                ETFHolding.etf_symbol.in_(etf_symbols),
                or_(ETFHolding.asset_class == "Equity", ETFHolding.asset_class == "EQUITY"),
            )
            .all()
        )
    finally:
        db.close()

    symbols = []
    for row in rows:
        symbol = normalize_us_equity_symbol(row[0])
        if symbol and symbol.endswith(".US"):
            symbols.append(symbol)
    return sorted(dict.fromkeys(symbols))


def _managed_etf_symbols(candidate_etfs: Optional[Sequence[str]] = None) -> List[str]:
    from .etf_manager import get_leveraged_etf_map_symbols

    symbols = [
        normalize_us_equity_symbol(item)
        for item in [
            *(candidate_etfs or US_STOCK_BASE_DATA_CANDIDATE_ETFS),
            *get_leveraged_etf_map_symbols(),
        ]
    ]
    return sorted(dict.fromkeys(symbol for symbol in symbols if symbol and symbol.endswith(".US")))


def _daily_sync_symbols(candidate_etfs: Optional[Sequence[str]] = None) -> Tuple[List[str], List[str], List[str]]:
    component_symbols = _candidate_etf_component_symbols(candidate_etfs)
    etf_symbols = _managed_etf_symbols(candidate_etfs)
    etf_set = set(etf_symbols)

    if US_STOCK_DAILY_MAX_SYMBOLS > 0:
        remaining_limit = max(0, US_STOCK_DAILY_MAX_SYMBOLS - len(etf_symbols))
        component_symbols = [
            symbol
            for symbol in component_symbols
            if symbol not in etf_set
        ][:remaining_limit]

    combined_symbols = sorted(dict.fromkeys([*etf_symbols, *component_symbols]))
    combined_set = set(combined_symbols)
    included_component_symbols = sorted(symbol for symbol in component_symbols if symbol in combined_set)
    return combined_symbols, included_component_symbols, sorted(etf_set)


def _kline_trade_date(timestamp) -> Optional[date]:
    if isinstance(timestamp, datetime):
        return timestamp.date()
    if isinstance(timestamp, date):
        return timestamp
    return None


def _fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=NASDAQ_TRADER_TIMEOUT_SECONDS) as response:
        return response.read().decode("utf-8", errors="replace")


def _read_symbol_directory(text: str) -> pd.DataFrame:
    useful_lines = [
        line
        for line in text.splitlines()
        if line and not line.startswith("File Creation Time")
    ]
    if not useful_lines:
        return pd.DataFrame()
    return pd.read_csv(StringIO("\n".join(useful_lines)), sep="|", dtype=str).fillna("")


def _is_tradable_company_row(row: Dict) -> bool:
    security_name = str(row.get("Security Name") or "")
    return (
        str(row.get("Test Issue") or "").upper() != "Y"
        and str(row.get("ETF") or "").upper() != "Y"
        and str(row.get("NextShares") or "").upper() != "Y"
        and not NON_COMPANY_SECURITY_NAME_PATTERN.search(security_name)
    )


def _fetch_nasdaq_trader_us_equities() -> List[Dict]:
    securities: List[Dict] = []

    nasdaq_frame = _read_symbol_directory(_fetch_text(NASDAQ_TRADER_NASDAQ_LISTED_URL))
    for _, row in nasdaq_frame.iterrows():
        data = row.to_dict()
        if not _is_tradable_company_row(data):
            continue
        symbol = normalize_us_equity_symbol(data.get("Symbol"))
        if not symbol:
            continue
        securities.append({
            "symbol": symbol,
            "name_en": data.get("Security Name") or None,
            "exchange": "NASDAQ",
            "source": "nasdaq_trader_nasdaqlisted",
        })

    other_frame = _read_symbol_directory(_fetch_text(NASDAQ_TRADER_OTHER_LISTED_URL))
    for _, row in other_frame.iterrows():
        data = row.to_dict()
        if not _is_tradable_company_row(data):
            continue
        symbol = normalize_us_equity_symbol(data.get("ACT Symbol") or data.get("NASDAQ Symbol"))
        if not symbol:
            continue
        exchange_code = str(data.get("Exchange") or "").upper()
        securities.append({
            "symbol": symbol,
            "name_en": data.get("Security Name") or None,
            "exchange": EXCHANGE_NAME_MAP.get(exchange_code, exchange_code or None),
            "source": "nasdaq_trader_otherlisted",
        })

    deduped: Dict[str, Dict] = {}
    for item in securities:
        deduped.setdefault(item["symbol"], item)
    return sorted(deduped.values(), key=lambda item: item["symbol"])


class USStockBaseDataSyncService:
    def __init__(
        self,
        longport_service: Optional[LongPortService] = None,
        progress_callback: Optional[ProgressCallback] = None,
    ):
        self.longport = longport_service or LongPortService.get_instance()
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

    def _securities_from_symbols(self, symbols: Sequence[str]) -> List[Dict]:
        normalized: List[Dict] = []
        seen = set()
        for item in symbols or []:
            symbol = normalize_us_equity_symbol(item)
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            normalized.append({
                "symbol": symbol,
                "source": "provided_symbols",
            })
        return sorted(normalized, key=lambda item: item["symbol"])

    def _fetch_us_securities(
        self,
        symbols: Optional[Sequence[str]] = None,
        max_symbols: Optional[int] = None,
    ) -> List[Dict]:
        limit = max(0, int(max_symbols or 0))
        if symbols is not None:
            normalized = self._securities_from_symbols(symbols)
            if limit > 0:
                normalized = normalized[:limit]
            return normalized

        try:
            securities = _fetch_nasdaq_trader_us_equities()
            if securities:
                self.logger.info("Loaded %s US securities from Nasdaq Trader symbol directories", len(securities))
                if limit > 0:
                    return securities[:limit]
                return securities
        except Exception as exc:
            self.logger.warning("Fetch Nasdaq Trader US securities failed, fallback to LongPort overnight list: %s", exc)

        securities = self.longport.get_security_list("US")
        normalized: List[Dict] = []
        seen = set()
        for item in securities or []:
            symbol = normalize_us_equity_symbol(item.get("symbol"))
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            normalized.append({
                "symbol": symbol,
                "name_cn": item.get("name_cn"),
                "name_en": item.get("name_en"),
                "name_hk": item.get("name_hk"),
                "exchange": item.get("exchange"),
                "source": "longport_overnight",
            })
        normalized.sort(key=lambda item: item["symbol"])
        if limit > 0:
            normalized = normalized[:limit]
        return normalized

    def _fetch_static_info_map(self, symbols: List[str]) -> Tuple[Dict[str, Dict], int]:
        result: Dict[str, Dict] = {}
        missing_count = 0
        total_batches = max(1, (len(symbols) + US_STOCK_STATIC_INFO_BATCH_SIZE - 1) // US_STOCK_STATIC_INFO_BATCH_SIZE)

        for batch_index, batch_symbols in enumerate(
            _chunks(symbols, US_STOCK_STATIC_INFO_BATCH_SIZE),
            start=1,
        ):
            self._progress(
                f"同步美股 static_info {batch_index}/{total_batches}",
                10 + int(20 * batch_index / total_batches),
            )
            try:
                static_infos = self.longport.get_static_info(list(batch_symbols))
            except Exception as exc:
                self.logger.warning("Fetch US static info batch %s failed: %s", batch_index, exc)
                missing_count += len(batch_symbols)
                continue

            fetched_symbols = set()
            for info in static_infos or []:
                payload = _normalize_static_info_payload(info)
                symbol = payload.get("symbol")
                if not symbol:
                    continue
                result[symbol] = payload
                fetched_symbols.add(symbol)
            missing_count += len(set(batch_symbols) - fetched_symbols)

        return result, missing_count

    def sync_static_info_snapshots(
        self,
        symbols: Sequence[str],
        static_info_map: Optional[Dict[str, Dict]] = None,
        batch_size: int = US_STOCK_STATIC_INFO_BATCH_SIZE,
    ) -> Dict:
        """同步 LongPort static_info 快照与历史记录到 SQLite。"""
        normalized_symbols = [item["symbol"] for item in self._securities_from_symbols(symbols)]
        if static_info_map is None:
            static_info_map, _missing = self._fetch_static_info_map(normalized_symbols)

        current_date = date.today()
        now = datetime.now()
        total_symbols = 0
        fetched_symbols = 0
        created_count = 0
        changed_count = 0
        refreshed_count = 0
        history_count = 0
        missing_count = 0
        db = Session()

        try:
            for batch_symbols in _chunks(normalized_symbols, batch_size):
                batch_symbols = [symbol for symbol in batch_symbols if symbol]
                if not batch_symbols:
                    continue
                total_symbols += len(batch_symbols)
                snapshot_rows = (
                    db.query(StockStaticInfoSnapshot)
                    .filter(StockStaticInfoSnapshot.symbol.in_(batch_symbols))
                    .all()
                )
                snapshot_map = {row.symbol: row for row in snapshot_rows}

                for symbol in batch_symbols:
                    payload = static_info_map.get(symbol)
                    if not payload:
                        missing_count += 1
                        continue

                    fetched_symbols += 1
                    existing = snapshot_map.get(symbol)
                    if existing is None:
                        db.add(_create_static_info_model(
                            StockStaticInfoSnapshot,
                            payload,
                            current_date,
                            now,
                        ))
                        created_count += 1
                        continue

                    existing_payload = _payload_from_record(existing)
                    if existing_payload != payload:
                        db.merge(_create_static_info_model(
                            StockStaticInfoHistory,
                            existing_payload,
                            existing.date,
                            now,
                            created_at=existing.created_at or now,
                        ))
                        history_count += 1
                        changed_count += 1
                    else:
                        refreshed_count += 1

                    for field in STATIC_INFO_FIELDS:
                        setattr(existing, field, deepcopy(payload.get(field)))
                    existing.date = current_date
                    existing.raw_data = deepcopy(payload)
                    existing.updated_at = now

                db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        self.logger.info(
            "Static info snapshot sync completed: symbols=%s fetched=%s created=%s changed=%s refreshed=%s history=%s missing=%s",
            total_symbols,
            fetched_symbols,
            created_count,
            changed_count,
            refreshed_count,
            history_count,
            missing_count,
        )
        return {
            "symbols": total_symbols,
            "fetched": fetched_symbols,
            "created": created_count,
            "changed": changed_count,
            "refreshed": refreshed_count,
            "history": history_count,
            "missing": missing_count,
        }

    def _daily_start_for_symbol(
        self,
        symbol: str,
        latest_daily_dates: Dict[str, date],
        explicit_start_date: Optional[date],
    ) -> date:
        if explicit_start_date:
            return explicit_start_date
        latest_date = latest_daily_dates.get(symbol)
        if latest_date:
            return max(DEFAULT_START_DATE, latest_date - timedelta(days=US_STOCK_DAILY_REFRESH_OVERLAP_DAYS))
        return DEFAULT_START_DATE

    @staticmethod
    def _price_value_changed(old_value, new_value) -> bool:
        old_missing = bool(pd.isna(old_value))
        new_missing = bool(pd.isna(new_value))
        if old_missing or new_missing:
            return old_missing != new_missing
        try:
            old_number = float(old_value)
            new_number = float(new_value)
        except (TypeError, ValueError):
            return old_value != new_value
        diff = abs(old_number - new_number)
        tolerance = max(
            US_STOCK_DAILY_ADJUST_PRICE_ABS_TOL,
            US_STOCK_DAILY_ADJUST_PRICE_REL_TOL * max(abs(old_number), abs(new_number), 1.0),
        )
        return diff > tolerance

    def _find_adjusted_price_change(self, symbol: str, rows: List[Dict]) -> Optional[Dict]:
        trade_dates = [
            row["trade_date"]
            for row in rows
            if isinstance(row.get("trade_date"), date)
        ]
        if not trade_dates:
            return None

        existing_prices = _existing_daily_prices(symbol, min(trade_dates), max(trade_dates))
        if not existing_prices:
            return None

        for row in rows:
            trade_date = row.get("trade_date")
            if not isinstance(trade_date, date):
                continue
            existing = existing_prices.get(trade_date)
            if not existing:
                continue
            for field in US_STOCK_DAILY_PRICE_FIELDS:
                old_value = existing.get(field)
                new_value = row.get(field)
                if self._price_value_changed(old_value, new_value):
                    return {
                        "symbol": symbol,
                        "trade_date": trade_date.isoformat(),
                        "field": field,
                        "old_value": old_value,
                        "new_value": new_value,
                    }
        return None

    def _build_daily_rows(self, symbol: str, klines: List[Dict], start_date: date, end_date: date, now: datetime) -> List[Dict]:
        rows = []
        for kline in klines or []:
            trade_date = _kline_trade_date(kline.get("timestamp"))
            if not trade_date or trade_date < start_date or trade_date > end_date:
                continue
            rows.append({
                "symbol": symbol,
                "trade_date": trade_date,
                "open": kline.get("open"),
                "high": kline.get("high"),
                "low": kline.get("low"),
                "close": kline.get("close"),
                "volume": kline.get("volume"),
                "turnover": kline.get("turnover"),
                "adjust_type": "forward",
                "period": "d",
                "created_at": now,
                "updated_at": now,
            })
        return rows

    def _flush_daily_rows(
        self,
        rows: List[Dict],
        replace_ranges: Optional[List[Tuple[str, date, date]]] = None,
    ) -> int:
        if not rows:
            return 0
        frame = pd.DataFrame(rows)
        frame = frame.drop_duplicates(subset=["symbol", "trade_date"], keep="last")
        _insert_or_replace_frame(
            USStockDaily.__tablename__,
            US_STOCK_DAILY_COLUMNS,
            frame,
            replace_ranges=replace_ranges,
        )
        return len(frame)

    def _sync_daily_klines(
        self,
        symbols: List[str],
        start_date: Optional[date],
        end_date: date,
        now: datetime,
    ) -> Dict:
        daily_date_bounds = {} if start_date else _daily_date_bounds()
        latest_daily_dates = {
            symbol: bounds[1]
            for symbol, bounds in daily_date_bounds.items()
        }
        pending_rows: List[Dict] = []
        pending_replace_ranges: List[Tuple[str, date, date]] = []
        saved_rows = 0
        fetched_symbols = 0
        empty_symbols = 0
        skipped_symbols = 0
        adjustment_refresh_symbols: List[str] = []
        adjustment_refresh_errors: List[Dict] = []
        errors: List[Dict] = []
        total = len(symbols)

        for index, symbol in enumerate(symbols, start=1):
            symbol_start = self._daily_start_for_symbol(symbol, latest_daily_dates, start_date)
            if symbol_start > end_date:
                skipped_symbols += 1
                continue

            self._progress(
                f"同步美股日K {index}/{total}: {symbol}",
                35 + int(60 * index / max(1, total)),
                symbol=symbol,
                start_date=symbol_start.isoformat(),
                end_date=end_date.isoformat(),
            )
            try:
                klines = self.longport.get_candlesticks_by_date(symbol, symbol_start, end_date, "d")
                rows = self._build_daily_rows(symbol, klines, symbol_start, end_date, now)
                if not rows:
                    empty_symbols += 1
                    continue
                if start_date is None and latest_daily_dates.get(symbol):
                    price_change = self._find_adjusted_price_change(symbol, rows)
                    if price_change:
                        full_refresh_start = daily_date_bounds[symbol][0]
                        self.logger.info(
                            "Detected adjusted US daily price change for %s on %s %s old=%s new=%s; full refresh from %s",
                            symbol,
                            price_change.get("trade_date"),
                            price_change.get("field"),
                            price_change.get("old_value"),
                            price_change.get("new_value"),
                            full_refresh_start,
                        )
                        self._progress(
                            f"检测到美股复权变化，重刷日K: {symbol}",
                            35 + int(60 * index / max(1, total)),
                            symbol=symbol,
                            start_date=full_refresh_start.isoformat(),
                            end_date=end_date.isoformat(),
                        )
                        full_klines = self.longport.get_candlesticks_by_date(
                            symbol,
                            full_refresh_start,
                            end_date,
                            "d",
                        )
                        full_rows = self._build_daily_rows(symbol, full_klines, full_refresh_start, end_date, now)
                        if full_rows:
                            rows = full_rows
                            pending_replace_ranges.append((symbol, full_refresh_start, end_date))
                            adjustment_refresh_symbols.append(symbol)
                        else:
                            self.logger.warning(
                                "Adjusted price change detected for %s but full refresh returned empty; skip incremental rows",
                                symbol,
                            )
                            error_payload = {
                                "symbol": symbol,
                                "error": "full_refresh_empty",
                                "change": price_change,
                            }
                            adjustment_refresh_errors.append(error_payload)
                            errors.append(error_payload)
                            continue
                pending_rows.extend(rows)
                fetched_symbols += 1
            except Exception as exc:
                self.logger.warning("Fetch US daily klines failed for %s: %s", symbol, exc)
                error_payload = {"symbol": symbol, "error": str(exc)}
                if isinstance(exc, LongPortKlineQuotaExceeded):
                    error_payload["error_type"] = "longport_kline_quota_exceeded"
                errors.append(error_payload)
                continue

            if index % US_STOCK_DAILY_INSERT_BATCH_SYMBOLS == 0:
                saved_rows += self._flush_daily_rows(pending_rows, pending_replace_ranges)
                pending_rows = []
                pending_replace_ranges = []

        saved_rows += self._flush_daily_rows(pending_rows, pending_replace_ranges)
        return {
            "daily_symbols": total,
            "daily_fetched_symbols": fetched_symbols,
            "daily_empty_symbols": empty_symbols,
            "daily_skipped_symbols": skipped_symbols,
            "daily_adjustment_refresh_symbols": adjustment_refresh_symbols,
            "daily_adjustment_refresh_count": len(adjustment_refresh_symbols),
            "daily_adjustment_refresh_errors": adjustment_refresh_errors,
            "daily_saved_rows": saved_rows,
            "daily_errors": errors,
        }

    def sync(
        self,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> Dict:
        started = time.perf_counter()
        current_date = date.today()
        end_value = end_date or current_date
        now = datetime.now()

        self._progress("准备美股同步股票池", 3)
        evc_symbols = _evc_static_info_symbols()
        static_securities = self._fetch_us_securities(symbols=evc_symbols)
        static_symbols = [item["symbol"] for item in static_securities]
        if not static_symbols:
            raise RuntimeError("EVC 未返回可同步 static_info 的美股列表")

        daily_symbols, daily_component_symbols, daily_etf_symbols = _daily_sync_symbols()
        if not daily_symbols:
            raise RuntimeError("未找到可同步日K的美股列表，请先同步 ETF 持仓数据")
        if not daily_component_symbols:
            self.logger.warning("No SPY/QQQ component symbols found; US daily sync will only include managed ETFs")
        daily_securities = self._fetch_us_securities(
            symbols=daily_symbols,
        )
        daily_symbols = [item["symbol"] for item in daily_securities]
        if not daily_symbols:
            raise RuntimeError("没有可同步日K的美股列表")

        self._progress("同步美股 static_info 快照", 8, symbols=len(static_symbols))
        static_info_map, static_missing = self._fetch_static_info_map(static_symbols)
        static_snapshot_result = self.sync_static_info_snapshots(
            symbols=static_symbols,
            static_info_map=static_info_map,
        )

        self._progress("同步美股日K到 DuckDB", 35, symbols=len(daily_symbols))
        daily_result = self._sync_daily_klines(daily_symbols, start_date, end_value, now)

        daily_rows = _count_table_rows(USStockDaily.__tablename__)
        total_seconds = round(time.perf_counter() - started, 3)
        result = {
            "status": "partial_failed" if daily_result.get("daily_errors") else "success",
            "start_date": (start_date or DEFAULT_START_DATE).isoformat(),
            "end_date": end_value.isoformat(),
            "symbols": len(static_symbols),
            "static_symbols": len(static_symbols),
            "daily_symbols": len(daily_symbols),
            "daily_component_symbols": len(daily_component_symbols),
            "daily_etf_symbols": len(daily_etf_symbols),
            "daily_etf_symbol_list": daily_etf_symbols,
            "static_info_fetched": len(static_info_map),
            "static_info_missing": static_missing,
            "static_snapshot": static_snapshot_result,
            "tables": {
                USStockDaily.__tablename__: daily_rows,
            },
            "total_seconds": total_seconds,
            **daily_result,
        }
        self._progress("美股基础数据同步完成", 100, **result)
        return result


def sync_us_stock_base_data(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> Dict:
    service = USStockBaseDataSyncService()
    return service.sync(start_date=start_date, end_date=end_date)

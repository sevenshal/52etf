import logging
import os
from typing import Any, Dict, Iterable, Optional, Set

from sqlalchemy.orm import Session

from ..database import StockStaticInfoSnapshot
from ..utils import normalize_us_equity_symbol
from ...robot.a_stock_base_data_config import A_STOCK_ETF_DAILY_NAMES, A_STOCK_FACTOR_INDEX_POOLS

logger = logging.getLogger(__name__)
ANALYTICS_DB_PATH = os.getenv("ANALYTICS_DB_PATH", "/var/lib/quant_robot/analytics.duckdb")

SYMBOL_NAME_FALLBACKS = {
    "INNO100.CN": "A股创新100",
    "CNN*.US": "CNN Fear & Greed",
    "SPY.US": "标普500ETF",
    "QQQ.US": "纳斯达克100ETF",
    "SOXX.US": "iShares费城半导体ETF",
    "SOXL.US": "三倍做多半导体ETF",
    "TQQQ.US": "三倍做多纳指100ETF",
}


def normalize_symbol_for_name(symbol: Any) -> Optional[str]:
    text = str(symbol or "").strip().upper()
    if not text:
        return None
    if "." not in text and len(text) == 6 and text.isdigit():
        if text.startswith(("60", "68", "51", "52", "56", "58", "50", "11")):
            return f"{text}.SH"
        if text.startswith(("00", "30", "20", "15", "12", "13")):
            return f"{text}.SZ"
        if text.startswith(("43", "83", "87", "88", "92")):
            return f"{text}.BJ"
    parts = text.split(".")
    if len(parts) == 2:
        first, second = parts
        if first in {"SH", "SS", "SZ", "BJ"}:
            market = "SH" if first in {"SH", "SS"} else first
            return f"{second}.{market}"
        market = "SH" if second in {"SH", "SS"} else second
        return f"{first}.{market}"
    return normalize_us_equity_symbol(text) or text


def symbol_lookup_keys(symbol: Any) -> Set[str]:
    normalized = normalize_symbol_for_name(symbol)
    if not normalized:
        return set()
    keys = {normalized}
    parts = normalized.split(".")
    if len(parts) == 2:
        code, market = parts
        keys.add(code)
        keys.add(f"{market}.{code}")
        if market == "SH":
            keys.update({f"{code}.SS", f"SS.{code}"})
        if market == "US":
            keys.add(code)
    return {str(item).strip().upper() for item in keys if str(item).strip()}


def _remember_name(name_by_key: Dict[str, str], symbol: Any, name: Any, *, overwrite: bool = False) -> None:
    text = str(name or "").strip()
    if not text:
        return
    for key in symbol_lookup_keys(symbol):
        if overwrite or key not in name_by_key:
            name_by_key[key] = text


def _load_a_stock_names(normalized_symbols: Iterable[str], name_by_key: Dict[str, str]) -> None:
    candidates = set()
    raw_codes = set()
    for symbol in normalized_symbols:
        keys = symbol_lookup_keys(symbol)
        candidates.update(keys)
        raw_codes.update(key for key in keys if "." not in key)
    if not candidates:
        return

    try:
        import duckdb

        connection = duckdb.connect(database=ANALYTICS_DB_PATH, read_only=True)
        try:
            placeholders = ", ".join(["?"] * len(candidates))
            raw_placeholders = ", ".join(["?"] * len(raw_codes or {"__empty__"}))
            stock_rows = connection.execute(
                f"""
                SELECT ts_code, symbol, name
                FROM a_stock_basic
                WHERE ts_code IN ({placeholders})
                   OR symbol IN ({raw_placeholders})
                """,
                [*sorted(candidates), *sorted(raw_codes or {"__empty__"})],
            ).fetchall()
            for ts_code, raw_code, name in stock_rows:
                _remember_name(name_by_key, ts_code, name, overwrite=True)
                _remember_name(name_by_key, raw_code, name, overwrite=True)

            fund_rows = connection.execute(
                f"""
                SELECT ts_code, name
                FROM a_stock_fund_basic
                WHERE ts_code IN ({placeholders})
                """,
                sorted(candidates),
            ).fetchall()
            for ts_code, name in fund_rows:
                _remember_name(name_by_key, ts_code, name, overwrite=True)
        finally:
            connection.close()
    except Exception as exc:
        logger.warning("Failed to load A stock symbol names: %s", exc)


def _load_us_symbol_names(db: Optional[Session], normalized_symbols: Iterable[str], name_by_key: Dict[str, str]) -> None:
    if db is None:
        return
    candidates = set()
    for symbol in normalized_symbols:
        normalized = normalize_us_equity_symbol(symbol)
        if normalized:
            candidates.add(normalized)
            candidates.add(normalized[:-3])
    if not candidates:
        return

    try:
        rows = (
            db.query(
                StockStaticInfoSnapshot.symbol,
                StockStaticInfoSnapshot.name_cn,
                StockStaticInfoSnapshot.name_hk,
                StockStaticInfoSnapshot.name_en,
            )
            .filter(StockStaticInfoSnapshot.symbol.in_(sorted(candidates)))
            .all()
        )
        for symbol, name_cn, name_hk, name_en in rows:
            name = name_cn or name_hk or name_en
            _remember_name(name_by_key, symbol, name, overwrite=True)
    except Exception:
        logger.exception("Failed to load US stock static info symbol names")


def load_symbol_name_map(symbols: Iterable[Any], db: Optional[Session] = None) -> Dict[str, str]:
    normalized_symbols = sorted({
        normalized
        for normalized in (normalize_symbol_for_name(symbol) for symbol in (symbols or []))
        if normalized
    })
    if not normalized_symbols:
        return {}

    name_by_key: Dict[str, str] = {}
    a_stock_symbols = [
        symbol
        for symbol in normalized_symbols
        if symbol.endswith((".SH", ".SZ", ".BJ")) or symbol in A_STOCK_ETF_DAILY_NAMES
    ]
    if a_stock_symbols:
        _load_a_stock_names(a_stock_symbols, name_by_key)
    _load_us_symbol_names(db, normalized_symbols, name_by_key)

    for symbol, name in A_STOCK_ETF_DAILY_NAMES.items():
        _remember_name(name_by_key, symbol, name)
    for item in A_STOCK_FACTOR_INDEX_POOLS:
        _remember_name(name_by_key, item.get("index_code"), item.get("name"))
    for symbol, name in SYMBOL_NAME_FALLBACKS.items():
        _remember_name(name_by_key, symbol, name)

    result: Dict[str, str] = {}
    for symbol in normalized_symbols:
        lookup_keys = [symbol, *sorted(symbol_lookup_keys(symbol) - {symbol})]
        for key in lookup_keys:
            name = name_by_key.get(key)
            if name:
                result[symbol] = name
                break
    return result


def attach_symbol_names(rows: Iterable[Dict[str, Any]], name_map: Dict[str, str], symbol_key: str = "symbol") -> None:
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        symbol = normalize_symbol_for_name(row.get(symbol_key))
        if symbol:
            row["symbol_name"] = name_map.get(symbol)


def format_symbol_label(symbol: Any, name_map: Dict[str, str]) -> str:
    normalized = normalize_symbol_for_name(symbol)
    if not normalized:
        return str(symbol or "")
    name = name_map.get(normalized)
    return f"{name} {normalized}" if name else normalized

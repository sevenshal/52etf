from copy import deepcopy
from typing import Dict, List, Sequence

from sqlalchemy.orm import Session as ORMSession

from .database import StockStaticInfoSnapshot

STATIC_INFO_FIELDS = [
    "symbol",
    "name_cn",
    "name_en",
    "name_hk",
    "exchange",
    "currency",
    "lot_size",
    "total_shares",
    "circulating_shares",
    "hk_shares",
    "eps",
    "eps_ttm",
    "bps",
    "dividend_yield",
    "stock_derivatives",
    "board",
]

STATIC_INFO_BATCH_SIZE = 500


def _normalize_symbols(symbols: Sequence[str]) -> List[str]:
    result = []
    seen = set()
    for symbol in symbols or []:
        text = str(symbol or "").strip().upper()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _record_to_static_info(record) -> Dict:
    return {field: deepcopy(getattr(record, field, None)) for field in STATIC_INFO_FIELDS}


def get_static_info_snapshot_map(
    db: ORMSession,
    symbols: Sequence[str],
    batch_size: int = STATIC_INFO_BATCH_SIZE,
) -> Dict[str, Dict]:
    normalized_symbols = _normalize_symbols(symbols)
    if not normalized_symbols:
        return {}

    result: Dict[str, Dict] = {}
    for i in range(0, len(normalized_symbols), batch_size):
        batch_symbols = normalized_symbols[i:i + batch_size]
        rows = (
            db.query(StockStaticInfoSnapshot)
            .filter(StockStaticInfoSnapshot.symbol.in_(batch_symbols))
            .all()
        )
        for row in rows:
            if row.symbol:
                result[row.symbol] = _record_to_static_info(row)
    return result


def get_static_info_snapshot(
    db: ORMSession,
    symbol: str,
) -> Dict:
    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_symbol:
        return {}
    return get_static_info_snapshot_map(db, [normalized_symbol]).get(normalized_symbol, {})

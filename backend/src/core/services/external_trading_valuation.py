import asyncio
import logging
import os
import threading
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy.orm import Session

from ..external_trading_database import (
    ExternalTradingLedgerPosition,
    ExternalTradingSubAccount,
)
from .external_trading import ExternalTradingConnectionError, external_trading_hub
from .external_trading_ledger import normalize_symbol, safe_float, safe_int
from .longport import LongPortService

logger = logging.getLogger(__name__)

QUOTE_CACHE_TTL_SECONDS = 10.0
LONGPORT_MARKET_DATA_ACCOUNT_ID = os.getenv("EXTERNAL_TRADING_VALUATION_LONGPORT_ACCOUNT_ID", "LBPT10001248")

_quote_cache: Dict[str, Dict[str, Any]] = {}
_quote_cache_lock = threading.Lock()


class ExternalTradingValuationError(Exception):
    """Raised when a virtual sub-account cannot be valued with current prices."""


def _now_ts() -> float:
    return time.monotonic()


def _normalize_symbols(symbols: Iterable[Any]) -> List[str]:
    result = []
    for symbol in symbols or []:
        normalized = normalize_symbol(symbol)
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _extract_quote_price(quote: Any) -> float:
    if quote is None:
        return 0.0
    if isinstance(quote, (int, float)):
        return safe_float(quote)
    if not isinstance(quote, dict):
        return safe_float(getattr(quote, "price", None))

    for key in ("price", "last_price", "last_px", "latest_price", "current_price", "last_done"):
        price = safe_float(quote.get(key))
        if price > 0:
            return price
    bid = safe_float(quote.get("bid"))
    ask = safe_float(quote.get("ask"))
    if bid > 0 and ask > 0:
        return round((bid + ask) / 2, 6)
    return bid or ask or 0.0


def _normalize_quote_map(quotes: Optional[Any], source: str) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    if not quotes:
        return result
    items = quotes.values() if isinstance(quotes, dict) else quotes
    for item in items or []:
        if isinstance(item, dict):
            symbol = normalize_symbol(item.get("symbol") or item.get("client_symbol"))
        else:
            symbol = normalize_symbol(getattr(item, "symbol", None))
        if not symbol:
            continue
        price = _extract_quote_price(item)
        if price <= 0:
            continue
        result[symbol] = {
            "symbol": symbol,
            "price": price,
            "source": source,
            "raw": item,
            "cached": False,
        }
    return result


def _get_cached_quote(symbol: str) -> Optional[Dict[str, Any]]:
    now = _now_ts()
    with _quote_cache_lock:
        cached = _quote_cache.get(symbol)
        if not cached:
            return None
        if now - safe_float(cached.get("cached_at_ts")) > QUOTE_CACHE_TTL_SECONDS:
            return None
        return {**cached, "cached": True}


def _store_quote(symbol: str, price: float, source: str, raw: Optional[Any] = None) -> Dict[str, Any]:
    item = {
        "symbol": symbol,
        "price": float(price),
        "source": source,
        "raw": raw,
        "cached": False,
        "cached_at": datetime.now().isoformat(),
        "cached_at_ts": _now_ts(),
    }
    with _quote_cache_lock:
        _quote_cache[symbol] = item
    return item


async def _fetch_hub_quotes(
    external_trading_account_id: int,
    symbols: List[str],
    timeout: float,
) -> Dict[str, Dict[str, Any]]:
    if not symbols:
        return {}
    response = await external_trading_hub.get_quotes(external_trading_account_id, symbols, timeout=timeout)
    result = {}
    for symbol, item in _normalize_quote_map(response.get("quotes") or [], "hub").items():
        result[symbol] = _store_quote(symbol, item["price"], "hub", item.get("raw") or item)
    return result


async def _fetch_longport_quotes(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    if not symbols:
        return {}

    def _call_longport():
        service = LongPortService.get_instance(LONGPORT_MARKET_DATA_ACCOUNT_ID)
        return service.get_quote_batch(symbols)

    loop = asyncio.get_running_loop()
    response = await loop.run_in_executor(None, _call_longport)
    result = {}
    for symbol, item in _normalize_quote_map(response or [], "longport").items():
        result[symbol] = _store_quote(symbol, item["price"], "longport", item.get("raw") or item)
    return result


async def get_realtime_price_details(
    external_trading_account_id: int,
    symbols: Iterable[Any],
    *,
    timeout: float = 10.0,
    prefetched_quotes: Optional[Any] = None,
) -> Dict[str, Dict[str, Any]]:
    normalized_symbols = _normalize_symbols(symbols)
    result: Dict[str, Dict[str, Any]] = {}

    for symbol, item in _normalize_quote_map(prefetched_quotes, "prefetched_hub").items():
        if symbol in normalized_symbols:
            result[symbol] = _store_quote(symbol, item["price"], "prefetched_hub", item.get("raw") or item)

    missing = [symbol for symbol in normalized_symbols if symbol not in result]
    for symbol in list(missing):
        cached = _get_cached_quote(symbol)
        if cached:
            result[symbol] = cached
    missing = [symbol for symbol in normalized_symbols if symbol not in result]

    hub_error = None
    if missing:
        try:
            result.update(await _fetch_hub_quotes(external_trading_account_id, missing, timeout))
        except ExternalTradingConnectionError as exc:
            hub_error = exc
            logger.warning("External trading hub quote failed for valuation: %s", exc)
        except Exception as exc:
            hub_error = exc
            logger.warning("External trading hub quote failed for valuation: %s", exc)
    missing = [symbol for symbol in normalized_symbols if symbol not in result]

    longport_error = None
    if missing:
        try:
            result.update(await _fetch_longport_quotes(missing))
        except Exception as exc:
            longport_error = exc
            logger.warning("LongPort quote fallback failed for valuation: %s", exc)
    missing = [symbol for symbol in normalized_symbols if symbol not in result]
    if missing:
        errors = []
        if hub_error:
            errors.append(f"hub: {hub_error}")
        if longport_error:
            errors.append(f"longport: {longport_error}")
        suffix = f" ({'; '.join(errors)})" if errors else ""
        raise ExternalTradingValuationError(f"无法获取以下标的最新价: {', '.join(missing)}{suffix}")
    return result


async def calculate_sub_account_net_asset(
    db: Session,
    sub_account: ExternalTradingSubAccount,
    *,
    positions: Optional[List[ExternalTradingLedgerPosition]] = None,
    timeout: float = 10.0,
    prefetched_quotes: Optional[Any] = None,
    update_positions: bool = True,
) -> Dict[str, Any]:
    if positions is None:
        positions = (
            db.query(ExternalTradingLedgerPosition)
            .filter(ExternalTradingLedgerPosition.sub_account_id == sub_account.id)
            .all()
        )
    held_positions = [row for row in positions or [] if safe_int(row.quantity) > 0]
    symbols = [row.symbol for row in held_positions]
    price_details = await get_realtime_price_details(
        sub_account.external_trading_account_id,
        symbols,
        timeout=timeout,
        prefetched_quotes=prefetched_quotes,
    ) if symbols else {}

    now = datetime.now()
    position_market_value = 0.0
    position_rows = []
    for position in held_positions:
        symbol = normalize_symbol(position.symbol)
        detail = price_details.get(symbol)
        if not detail:
            raise ExternalTradingValuationError(f"无法获取 {symbol} 最新价")
        price = safe_float(detail.get("price"))
        quantity = safe_int(position.quantity)
        market_value = round(quantity * price, 2)
        position_market_value += market_value
        if update_positions:
            position.market_price = price
            position.market_value = market_value
            position.updated_at = now
        position_rows.append({
            "symbol": symbol,
            "quantity": quantity,
            "price": price,
            "market_value": market_value,
            "price_source": detail.get("source"),
            "cached": bool(detail.get("cached")),
        })

    cash_available = round(safe_float(sub_account.cash_available), 2)
    position_market_value = round(position_market_value, 2)
    return {
        "sub_account_id": sub_account.id,
        "cash_available": cash_available,
        "position_market_value": position_market_value,
        "net_asset": round(cash_available + position_market_value, 2),
        "positions": position_rows,
        "position_symbols": [row["symbol"] for row in position_rows],
        "price_details": price_details,
        "valued_at": now.isoformat(),
    }

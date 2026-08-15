import asyncio
import logging
import os
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy.orm import Session

from ..external_trading_database import (
    ExternalTradingLedgerPosition,
    ExternalTradingSubAccount,
)
from .external_trading import ExternalTradingConnectionError, external_trading_hub
from .external_trading_ledger import normalize_symbol, safe_float, safe_int
from .longport import LongPortService
from .tushare import TushareService

logger = logging.getLogger(__name__)

LONGPORT_MARKET_DATA_ACCOUNT_ID = os.getenv("EXTERNAL_TRADING_VALUATION_LONGPORT_ACCOUNT_ID", "LBPT10001248")
CHINA_TZ = ZoneInfo("Asia/Shanghai")


class ExternalTradingValuationError(Exception):
    """Raised when a virtual sub-account cannot be valued with current prices."""


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


def _parse_quote_timestamp(value: Any) -> Optional[Any]:
    """解析行情时间戳（epoch 秒或时间字符串）为带 A 股时区的 datetime。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), CHINA_TZ)
        except Exception:
            return None
    try:
        parsed = pd.to_datetime(value, errors="coerce")
        if parsed is pd.NaT:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=CHINA_TZ)
        return parsed.astimezone(CHINA_TZ)
    except Exception:
        return None


def _quote_is_today(
    item: Any,
    symbol: Optional[str],
    today: Optional[Any] = None,
    filter_stale_quotes: bool = False,
) -> bool:
    """filter_stale_quotes=True 时，对 A 股标的用行情时间戳判断'今日已开盘'。

    longport/hub 在停盘或未开盘（如跨境 ETF 延迟到 10:30）时可能返回上一交易日
    价格（带昨日 timestamp/trade_time），必须丢弃，否则会把昨收当实时价下单。
    带时间戳但解析不出、或没有时间字段的报价保持原样（信任源本身）。
    美股/港股等非 A 股标的不过滤（交易时段在境外，不能用 A 股日历判断）。
    默认 False 不过滤，兼容估值、策略行情等依赖昨收价的场景。
    """
    if not filter_stale_quotes:
        return True
    normalized = str(symbol or "").upper()
    if not normalized.endswith((".SH", ".SZ")):
        return True
    raw = item if isinstance(item, dict) else {}
    timestamp_value = raw.get("timestamp") or raw.get("trade_time") or raw.get("quote_time")
    if timestamp_value is None:
        return True
    parsed = _parse_quote_timestamp(timestamp_value)
    if parsed is None:
        return True
    if today is None:
        today = datetime.now(CHINA_TZ).date()
    return parsed.date() == today


def _normalize_quote_map(
    quotes: Optional[Any],
    source: str,
    *,
    today: Optional[Any] = None,
    filter_stale_quotes: bool = False,
) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    if not quotes:
        return result
    items = quotes.items() if isinstance(quotes, dict) else [(None, item) for item in quotes]
    for key, item in items or []:
        item_source = source
        if isinstance(item, dict):
            symbol = normalize_symbol(item.get("symbol") or item.get("client_symbol") or key)
            item_source = item.get("source") or item.get("price_source") or source
        else:
            symbol = normalize_symbol(getattr(item, "symbol", None) or key)
        if not symbol:
            continue
        if not _quote_is_today(item, symbol, today=today, filter_stale_quotes=filter_stale_quotes):
            # 上一交易日/非今日数据，无法确认今日已开盘
            continue
        price = _extract_quote_price(item)
        if price <= 0:
            continue
        result[symbol] = {
            "symbol": symbol,
            "price": price,
            "source": item_source,
            "raw": item,
        }
    return result


async def _fetch_hub_quotes(
    external_trading_account_id: int,
    symbols: List[str],
    timeout: float,
    *,
    filter_stale_quotes: bool = False,
) -> Dict[str, Dict[str, Any]]:
    if not symbols:
        return {}
    response = await external_trading_hub.get_quotes(external_trading_account_id, symbols, timeout=timeout)
    return _normalize_quote_map(
        response.get("quotes") or [],
        "hub",
        filter_stale_quotes=filter_stale_quotes,
    )


async def _fetch_longport_quotes(
    symbols: List[str],
    *,
    filter_stale_quotes: bool = False,
) -> Dict[str, Dict[str, Any]]:
    if not symbols:
        return {}

    def _call_longport():
        service = LongPortService.get_instance(LONGPORT_MARKET_DATA_ACCOUNT_ID)
        return service.get_quote_batch(symbols)

    loop = asyncio.get_running_loop()
    response = await loop.run_in_executor(None, _call_longport)
    return _normalize_quote_map(response or [], "longport", filter_stale_quotes=filter_stale_quotes)


def _parse_tushare_realtime_frame(
    frame: Any,
    *,
    today: Optional[Any] = None,
    filter_stale_quotes: bool = False,
) -> Dict[str, Dict[str, Any]]:
    """解析 tushare rt_k / rt_etf_k 返回。

    filter_stale_quotes=True 时只保留 trade_time 为当日的行：休市或未开盘
    （如跨境 ETF 延迟到 10:30）时 tushare 返回的是上一交易日数据（trade_time
    是昨天），必须丢弃，否则会把昨天的收盘价当成实时价照常下单。
    默认 False 不过滤，兼容估值、策略行情等依赖昨收价的场景。
    """
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return {}
    if today is None:
        today = datetime.now(CHINA_TZ).date()
    result: Dict[str, Dict[str, Any]] = {}
    for _, row in frame.iterrows():
        ts_code = str(row.get("ts_code") or "").strip().upper()
        symbol = normalize_symbol(ts_code)
        if not symbol:
            continue
        close = safe_float(row.get("close"))
        if close <= 0:
            continue
        if filter_stale_quotes:
            raw_trade_time = row.get("trade_time")
            if raw_trade_time is None or (isinstance(raw_trade_time, float) and pd.isna(raw_trade_time)):
                continue
            trade_time = pd.to_datetime(raw_trade_time, errors="coerce")
            if trade_time is pd.NaT or trade_time.date() != today:
                # 上一交易日/非今日数据，无法确认今日已开盘
                continue
        result[symbol] = {
            "symbol": symbol,
            "price": close,
            "source": "tushare_rt",
            "raw": row.to_dict(),
        }
    return result


async def _fetch_tushare_realtime_quotes(
    symbols: List[str],
    *,
    today: Optional[Any] = None,
    filter_stale_quotes: bool = False,
) -> Dict[str, Dict[str, Any]]:
    """tushare 实时行情（优先源）：股票走 rt_k，ETF 走 rt_etf_k（沪市带 topic）。
    filter_stale_quotes=True 时只接受 trade_time 为当日的行，未开盘/停牌
    （如 159941 延迟到 10:30）自然无价。
    """
    if not symbols:
        return {}
    a_share = [symbol for symbol in symbols if str(symbol or "").upper().endswith((".SH", ".SZ"))]
    if not a_share:
        return {}

    def _call_tushare():
        service = TushareService.get_instance()
        frames = []
        stock_frame = service.get_a_stock_realtime_rt_k_frame(a_share)
        if isinstance(stock_frame, pd.DataFrame) and not stock_frame.empty:
            frames.append(stock_frame)
        etf_frame = service.get_a_stock_realtime_etf_rt_k_frame(a_share)
        if isinstance(etf_frame, pd.DataFrame) and not etf_frame.empty:
            frames.append(etf_frame)
        if not frames:
            return {}
        merged = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["ts_code"])
        return _parse_tushare_realtime_frame(
            merged,
            today=today,
            filter_stale_quotes=filter_stale_quotes,
        )

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _call_tushare)


async def get_realtime_price_details(
    external_trading_account_id: int,
    symbols: Iterable[Any],
    *,
    timeout: float = 10.0,
    prefetched_prices: Optional[Any] = None,
    raise_on_missing: bool = True,
    filter_stale_quotes: bool = False,
) -> Dict[str, Dict[str, Any]]:
    """获取实时行情，多源合并（tushare → longport → hub）。

    filter_stale_quotes=True 时只接受当日数据（A 股标的），停盘/未开盘标的自然无价，
    用于外部交易执行器的'未开盘/停牌自动跳过'。默认 False 不过滤，
    兼容估值、策略行情等依赖昨收价的场景。
    """
    normalized_symbols = _normalize_symbols(symbols)
    result: Dict[str, Dict[str, Any]] = {}

    for symbol, item in _normalize_quote_map(
        prefetched_prices,
        "prefetched_prices",
        filter_stale_quotes=filter_stale_quotes,
    ).items():
        if symbol in normalized_symbols:
            result[symbol] = item

    missing = [symbol for symbol in normalized_symbols if symbol not in result]

    tushare_error = None
    hub_error = None
    longport_error = None

    async def fetch_tushare_for_missing():
        nonlocal tushare_error
        current_missing = [symbol for symbol in normalized_symbols if symbol not in result]
        if not current_missing:
            return
        try:
            result.update(await _fetch_tushare_realtime_quotes(
                current_missing,
                filter_stale_quotes=filter_stale_quotes,
            ))
        except Exception as exc:
            tushare_error = exc
            logger.warning("Tushare realtime quote failed for valuation: %s", exc)

    async def fetch_hub_for_missing():
        nonlocal hub_error
        current_missing = [symbol for symbol in normalized_symbols if symbol not in result]
        if not current_missing:
            return
        try:
            result.update(await _fetch_hub_quotes(
                external_trading_account_id,
                current_missing,
                timeout,
                filter_stale_quotes=filter_stale_quotes,
            ))
        except ExternalTradingConnectionError as exc:
            hub_error = exc
            logger.warning("External trading hub quote failed for valuation: %s", exc)
        except Exception as exc:
            hub_error = exc
            logger.warning("External trading hub quote failed for valuation: %s", exc)

    async def fetch_longport_for_missing():
        nonlocal longport_error
        current_missing = [symbol for symbol in normalized_symbols if symbol not in result]
        if not current_missing:
            return
        try:
            result.update(await _fetch_longport_quotes(
                current_missing,
                filter_stale_quotes=filter_stale_quotes,
            ))
        except Exception as exc:
            longport_error = exc
            logger.warning("LongPort quote fallback failed for valuation: %s", exc)

    await fetch_tushare_for_missing()
    await fetch_longport_for_missing()
    await fetch_hub_for_missing()

    missing = [symbol for symbol in normalized_symbols if symbol not in result]
    if missing and raise_on_missing:
        errors = []
        if tushare_error:
            errors.append(f"tushare: {tushare_error}")
        if longport_error:
            errors.append(f"longport: {longport_error}")
        if hub_error:
            errors.append(f"hub: {hub_error}")
        suffix = f" ({'; '.join(errors)})" if errors else ""
        raise ExternalTradingValuationError(f"无法获取以下标的最新价: {', '.join(missing)}{suffix}")
    return result


async def get_realtime_reference_prices(
    external_trading_account_id: int,
    symbols: Iterable[Any],
    *,
    timeout: float = 10.0,
    prefetched_prices: Optional[Any] = None,
    filter_stale_quotes: bool = False,
) -> Dict[str, float]:
    price_details = await get_realtime_price_details(
        external_trading_account_id,
        symbols,
        timeout=timeout,
        prefetched_prices=prefetched_prices,
        filter_stale_quotes=filter_stale_quotes,
    )
    return {
        normalize_symbol(symbol): safe_float(detail.get("price"))
        for symbol, detail in price_details.items()
        if normalize_symbol(symbol) and safe_float(detail.get("price")) > 0
    }


async def calculate_sub_account_net_asset(
    db: Session,
    sub_account: ExternalTradingSubAccount,
    *,
    positions: Optional[List[ExternalTradingLedgerPosition]] = None,
    timeout: float = 10.0,
    prefetched_prices: Optional[Any] = None,
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
        prefetched_prices=prefetched_prices,
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

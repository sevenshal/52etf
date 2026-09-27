"""指数估值点位统一入口：A股指数（成分一致预期）+ 美股指数ETF（EVC 成分公允价值）。

情绪量能回测与 A股情绪量能实盘的估值闸门都走这里，保证两边口径一致。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, Optional, Tuple

from .a_stock_index_valuation import load_a_stock_index_valuation_position_history
from .us_index_valuation import load_us_index_valuation_position_history
from .external_trading_market import _is_china_trading_day, _is_us_trading_day


def load_index_valuation_position_history(
    symbol: str,
    *,
    end_date: Optional[date] = None,
) -> Dict[date, Dict[int, Optional[float]]]:
    normalized_symbol = str(symbol or "").strip().upper()
    if normalized_symbol.endswith(".US"):
        return load_us_index_valuation_position_history(normalized_symbol, end_date=end_date)
    return load_a_stock_index_valuation_position_history(normalized_symbol, end_date=end_date)


def _latest_or_previous_trading_day(symbol: str, as_of_date: date) -> Tuple[date, date]:
    """Return the latest market day on/before ``as_of_date`` and its predecessor."""
    is_us = str(symbol or "").strip().upper().endswith(".US")
    is_trading_day = _is_us_trading_day if is_us else _is_china_trading_day

    latest = as_of_date
    while not is_trading_day(latest):
        latest -= timedelta(days=1)
    previous = latest - timedelta(days=1)
    while not is_trading_day(previous):
        previous -= timedelta(days=1)
    return latest, previous


def resolve_index_valuation_position(
    history: Dict[date, Dict[int, Optional[float]]],
    symbol: str,
    *,
    as_of_date: date,
    window: int,
) -> Tuple[Optional[float], Optional[date]]:
    """Resolve a valuation from the signal market day or one prior market day only.

    Missing valuation must not silently turn a configured valuation gate into an
    unrestricted trade.  The returned date lets callers record when the one-day
    fallback was used.
    """
    if not history:
        return None, None
    latest, previous = _latest_or_previous_trading_day(symbol, as_of_date)
    for candidate_date in (latest, previous):
        row = history.get(candidate_date) or {}
        value = row.get(window)
        if value is not None:
            return float(value), candidate_date
    return None, None


def load_latest_index_valuation_position(
    symbol: str,
    *,
    as_of_date: date,
    window: int,
) -> Optional[float]:
    """Return the latest confirmed valuation position available at ``as_of_date``.

    The valuation feed can lag the trading quote (especially near the US close),
    so callers must never look up an exact same-day value and accidentally treat
    a missing value as an unvalued market.  The history loader already excludes
    future rows; selecting the last row here makes the rule explicit for live
    trading as well as tail-of-day checks.
    """
    history = load_index_valuation_position_history(symbol, end_date=as_of_date)
    value, _ = resolve_index_valuation_position(
        history,
        symbol,
        as_of_date=as_of_date,
        window=window,
    )
    return value

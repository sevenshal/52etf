"""指数估值点位统一入口：A股指数（成分一致预期）+ 美股指数ETF（EVC 成分公允价值）。

情绪量能回测与 A股情绪量能实盘的估值闸门都走这里，保证两边口径一致。
"""
from __future__ import annotations

from datetime import date
from typing import Dict, Optional

from .a_stock_index_valuation import load_a_stock_index_valuation_position_history
from .us_index_valuation import load_us_index_valuation_position_history


def load_index_valuation_position_history(
    symbol: str,
    *,
    end_date: Optional[date] = None,
) -> Dict[date, Dict[int, Optional[float]]]:
    normalized_symbol = str(symbol or "").strip().upper()
    if normalized_symbol.endswith(".US"):
        return load_us_index_valuation_position_history(normalized_symbol, end_date=end_date)
    return load_a_stock_index_valuation_position_history(normalized_symbol, end_date=end_date)


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
    if not history:
        return None
    confirmed_dates = [item_date for item_date in history if item_date <= as_of_date]
    if not confirmed_dates:
        return None
    value = history[max(confirmed_dates)].get(window)
    return float(value) if value is not None else None

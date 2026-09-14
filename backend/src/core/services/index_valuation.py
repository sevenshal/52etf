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

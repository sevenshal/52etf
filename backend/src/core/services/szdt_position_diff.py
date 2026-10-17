"""守猪逮兔回测最新持仓与当前实盘持仓的对比合并。

回测结果只包含“回测末日仍持有”的标的，实盘里可能有回测早已卖出、或者压根没被
策略配置覆盖的仓位。这里把两侧做一次全量对齐：

- 回测持有的标的：补上 ``live_quantity`` / ``live_quantity_diff``；
- 只有实盘持有的标的：追加一行 ``live_only=True`` 的占位行（回测股数 0、差额为负），
  让“回测没有、实盘有”的仓位也能被看见。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

A_SHARE_MARKETS = {"SH", "SZ", "BJ"}


def a_stock_live_symbol(code: Any) -> str:
    """SZDT 的 SH.510300 → 账本/行情使用的 510300.SH；其他格式原样大写。"""
    text = str(code or "").strip().upper()
    market, separator, ticker = text.partition(".")
    if separator and market in A_SHARE_MARKETS and ticker:
        return f"{ticker}.{market}"
    return text


def a_stock_backtest_code(symbol: Any) -> str:
    """账本/行情使用的 510300.SH → SZDT 的 SH.510300；其他格式原样大写。"""
    text = str(symbol or "").strip().upper()
    ticker, separator, market = text.partition(".")
    if separator and market in A_SHARE_MARKETS and ticker:
        return f"{market}.{ticker}"
    return text


def us_live_symbol(code: Any) -> str:
    """US.TQQQ → TQQQ；已是裸代码时原样返回。"""
    text = str(code or "").strip().upper()
    return text[3:] if text.startswith("US.") else text


def us_backtest_code(symbol: Any) -> str:
    """TQQQ → US.TQQQ；已带 US. 前缀时原样返回。"""
    text = str(symbol or "").strip().upper()
    if not text:
        return text
    return text if text.startswith("US.") else f"US.{text}"


def _safe_quantity(value: Any) -> int:
    if isinstance(value, Mapping):
        value = value.get("quantity", value.get("qty"))
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _safe_price(value: Any) -> float:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return 0.0
    return price if price > 0 else 0.0


def merge_backtest_live_positions(
    latest_positions: Iterable[Mapping[str, Any]],
    live_positions: Mapping[str, Any],
    *,
    to_live_symbol: Callable[[Any], str] = a_stock_live_symbol,
    to_backtest_code: Callable[[Any], str] = a_stock_backtest_code,
    names: Optional[Mapping[str, str]] = None,
    prices: Optional[Mapping[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """合并回测末日持仓与实盘持仓，返回可直接给前端的行列表。

    ``live_positions`` 的键使用实盘口径（A股 ``510300.SH`` / 美股 ``TQQQ``），
    值可以是数量，也可以是 ``{"quantity": ...}`` / ``{"qty": ...}`` 形式。
    """
    names = names or {}
    prices = prices or {}
    rows: List[Dict[str, Any]] = []
    matched_symbols = set()

    for item in latest_positions or []:
        row = dict(item)
        symbol = to_live_symbol(row.get("code"))
        matched_symbols.add(symbol)
        live_quantity = max(0, _safe_quantity(live_positions.get(symbol)))
        row["live_quantity"] = live_quantity
        row["live_quantity_diff"] = _safe_quantity(row.get("quantity")) - live_quantity
        row["live_only"] = False
        rows.append(row)

    extra_rows: List[Dict[str, Any]] = []
    for symbol, raw_quantity in (live_positions or {}).items():
        quantity = _safe_quantity(raw_quantity)
        if quantity <= 0 or symbol in matched_symbols:
            continue
        price = _safe_price(prices.get(symbol))
        code = to_backtest_code(symbol)
        extra_rows.append({
            "code": code,
            "name": names.get(symbol) or names.get(str(symbol).upper()) or code,
            "quantity": 0,
            "price": round(price, 4) if price else None,
            "score": None,
            # 回测侧没有仓位：仓位占比与回测市值都不适用，前端显示 “-”。
            "market_value": None,
            "position_pct": None,
            "live_quantity": quantity,
            "live_quantity_diff": -quantity,
            "live_only": True,
        })
    # 实盘独有仓位按估算市值倒序，金额最大的先看到。
    extra_rows.sort(
        key=lambda item: (-(item["live_quantity"] * (item["price"] or 0)), -item["live_quantity"], item["code"])
    )
    return rows + extra_rows

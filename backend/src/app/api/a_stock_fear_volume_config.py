"""A 股情绪量能策略的市场配置。

这里刻意不依赖 SOXL 回测模块。A 股实盘和 A 股回测共用的标的、恐贪来源及
估值窗口放在这里，避免 SOXL 参数演进时影响 A 股策略的可用配置。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from ...core.services.a_stock_index_valuation import (
    VALUATION_POSITION_MAX_WINDOW,
    VALUATION_POSITION_SHORT_WINDOW,
)
from ...robot.a_stock_base_data_config import (
    A_STOCK_ETF_DAILY_NAMES,
    A_STOCK_INDEX_FEAR_GREED_PROXY_ETFS,
)


VALUATION_POSITION_WINDOWS = (
    VALUATION_POSITION_SHORT_WINDOW,
    VALUATION_POSITION_MAX_WINDOW,
)
SELL_MA5_CONFIRM_OFF = "off"
SELL_MA5_CONFIRM_ALL = "all"
SELL_MA5_CONFIRM_NON_MAIN = "non_main"
SELL_MA5_CONFIRM_MODES = {
    SELL_MA5_CONFIRM_OFF,
    SELL_MA5_CONFIRM_ALL,
    SELL_MA5_CONFIRM_NON_MAIN,
}
A_STOCK_FEAR_VOLUME_EXTRA_TARGET_ETFS = ("501225.SH", "159941.SZ", "159509.SZ")


def _normalize_symbol(value: str) -> str:
    return str(value or "").strip().upper()


def _fear_source_key_for_symbol(symbol: str) -> str:
    normalized = _normalize_symbol(symbol)
    return "a_stock_" + re.sub(r"[^a-z0-9]+", "_", normalized.lower()).strip("_")


def _fear_label(target: Dict[str, Any]) -> str:
    return f"{target.get('ticker') or target.get('label') or target['symbol']} 指数贪恐"


def _build_fear_sources() -> Dict[str, Dict[str, Any]]:
    # 延迟导入以避开机器人配置初始化时的循环依赖。
    from ...core.services.a_stock_fear_greed_clone_service import A_STOCK_FEAR_GREED_TARGETS

    sources: Dict[str, Dict[str, Any]] = {}
    for target in A_STOCK_FEAR_GREED_TARGETS:
        symbol = _normalize_symbol(target["symbol"])
        sources[_fear_source_key_for_symbol(symbol)] = {
            "label": _fear_label(target),
            "column": "etf_fear_greed",
            "symbol": symbol,
            "market": "a_stock",
        }
    return sources


def _build_target_options() -> List[Dict[str, Any]]:
    options: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for symbol in [*A_STOCK_INDEX_FEAR_GREED_PROXY_ETFS, *A_STOCK_FEAR_VOLUME_EXTRA_TARGET_ETFS]:
        normalized = _normalize_symbol(symbol)
        if normalized in seen:
            continue
        seen.add(normalized)
        options.append({
            "label": f"{A_STOCK_ETF_DAILY_NAMES.get(normalized, normalized)} {normalized}",
            "value": normalized,
            "market": "a_stock",
        })
    return options


def _build_preset_pairs() -> List[Dict[str, Any]]:
    from ...core.services.a_stock_fear_greed_clone_service import A_STOCK_FEAR_GREED_TARGETS

    pairs: List[Dict[str, Any]] = []
    for target in A_STOCK_FEAR_GREED_TARGETS:
        etf_symbol = target.get("proxy_etf")
        if not etf_symbol:
            continue
        fear_symbol = _normalize_symbol(target["symbol"])
        target_symbol = _normalize_symbol(etf_symbol)
        target_label = f"{A_STOCK_ETF_DAILY_NAMES.get(target_symbol, target_symbol)} {target_symbol}"
        fear_label = _fear_label(target)
        pairs.append({
            "key": f"{target_symbol}:{fear_symbol}",
            "target_symbol": target_symbol,
            "target_label": target_label,
            "fear_source": _fear_source_key_for_symbol(fear_symbol),
            "fear_symbol": fear_symbol,
            "fear_label": fear_label,
            "label": f"{target_label} × {fear_label}",
        })
    return pairs


A_STOCK_FEAR_SOURCE_OPTIONS = _build_fear_sources()
A_STOCK_TARGET_OPTIONS = _build_target_options()
A_STOCK_PRESET_PAIRS = _build_preset_pairs()
# A 股策略允许将美股指数恐贪作为跨市场观察腿；这是数据来源配置，不引入
# SOXL 的仓位、止盈或估值逻辑。
FEAR_SOURCE_OPTIONS = {
    "cnn": {"label": "CNN贪恐", "column": "cnn_fear_greed", "market": "us"},
    "soxx_clone": {"label": "SOXX 半导体自算贪恐", "column": "etf_fear_greed", "symbol": "SOXX.US", "market": "us"},
    "spy_clone": {"label": "SPY 标普500自算贪恐", "column": "etf_fear_greed", "symbol": "SPY.US", "market": "us"},
    "qqq_clone": {"label": "QQQ 纳指100自算贪恐", "column": "etf_fear_greed", "symbol": "QQQ.US", "market": "us"},
    "dia_clone": {"label": "DIA 道琼斯自算贪恐", "column": "etf_fear_greed", "symbol": "DIA.US", "market": "us"},
    **A_STOCK_FEAR_SOURCE_OPTIONS,
}

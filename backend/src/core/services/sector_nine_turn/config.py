"""板块九转策略的可配置超参数（全局单份）。

存在 SQLite ``sector_nine_turn_configs`` 表（单行 JSON）。读写都是独立短事务，返回普通
dict 快照；调用方不要在长事务里调用。

默认值取自 ``research/sector_nine_turn_fear_gate_backtest.md`` 里表现最好的一组：
板块与个股同日出信号（布防窗口 0 个交易日），板块范围剔除宽基/风格指数，只保留
科创50/100/200 和上证红利这几只。
"""

from __future__ import annotations

import copy
import math
from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ....robot.a_stock_base_data_config import A_STOCK_INDEX_FEAR_GREED_TARGETS

# 宽基/风格指数：成分覆盖整个市场或整个板块，低 9 其实就是大盘信号，选股能力差。
# 回测里这一组单独跑是负收益，所以默认只保留用户点名的这几只。
BROAD_MARKET_INDEXES = frozenset({
    "000300.SH", "000016.SH", "000510.SH", "000905.SH", "000852.SH", "932000.CSI",
    "000985.SH", "899050.BJ", "000680.SH", "000688.SH", "000698.SH", "000699.SH",
    "399006.SZ", "000015.SH",
})
# 默认保留的宽基/风格指数
KEPT_BROAD_INDEXES = ("000688.SH", "000698.SH", "000699.SH", "000015.SH")

SELL_MODES = {
    "low2_wait": "高9后首次低2且回撤>N个ATR；回撤不够就继续等下一次低2",
    "low2_first_only": "高9后只认第一次低2，回撤不够本轮作废，等下一次高9",
    "low_ge2_wait": "高9后任意低2及以上且回撤>N个ATR",
}
PICK_ORDERS = {
    "fear_asc": "板块贪恐分数低（更恐慌）的优先",
    "code_asc": "按股票代码排序（不掺入贪恐信息）",
}

DEFAULT_BACKTEST_START = date(2023, 1, 1)


def _default_index_codes() -> List[str]:
    codes = []
    for item in A_STOCK_INDEX_FEAR_GREED_TARGETS:
        code = str(item.get("symbol") or "").upper()
        if not code or code in codes:
            continue
        if code in BROAD_MARKET_INDEXES and code not in KEPT_BROAD_INDEXES:
            continue
        codes.append(code)
    return codes


DEFAULT_CONFIG: Dict[str, Any] = {
    "universe": {
        # 空列表表示"用默认板块集合"；页面保存时会写成显式代码清单
        "index_codes": [],
    },
    "signal": {
        "fear_threshold": 40.0,
        "low_count_min": 9.0,
        "buy_high_count": 2.0,
        "arm_window_days": 0.0,
        "high_count_min": 9.0,
        "sell_low_count": 2.0,
        "sell_atr_multiple": 2.0,
        "sell_mode": "low2_wait",
    },
    "portfolio": {
        "max_positions": 10.0,
        "pick_order": "fear_asc",
    },
    "paper": {
        "enabled": True,
        "initial_capital": 1000000.0,
        "commission_pct": 0.03,
        "stamp_tax_pct": 0.05,
    },
}

# key -> (下限, 上限, 是否整数)
_SIGNAL_BOUNDS = {
    "fear_threshold": (0.0, 100.0, False),
    "low_count_min": (4.0, 30.0, True),
    "buy_high_count": (1.0, 9.0, True),
    "arm_window_days": (0.0, 60.0, True),
    "high_count_min": (4.0, 30.0, True),
    "sell_low_count": (1.0, 9.0, True),
    "sell_atr_multiple": (0.0, 10.0, False),
}
_PORTFOLIO_BOUNDS = {"max_positions": (1.0, 100.0, True)}
_PAPER_BOUNDS = {
    "initial_capital": (10000.0, 1e10, False),
    "commission_pct": (0.0, 1.0, False),
    "stamp_tax_pct": (0.0, 1.0, False),
}


def index_catalog() -> List[Dict[str, Any]]:
    """所有可选板块及其分类，页面用来做勾选列表。"""
    catalog: List[Dict[str, Any]] = []
    seen = set()
    for item in A_STOCK_INDEX_FEAR_GREED_TARGETS:
        code = str(item.get("symbol") or "").upper()
        if not code or code in seen:
            continue
        seen.add(code)
        catalog.append({
            "index_code": code,
            "name": item.get("ticker") or item.get("label") or code,
            "index_name": item.get("index_name") or item.get("ticker") or code,
            "category": "宽基" if code in BROAD_MARKET_INDEXES else "行业主题",
            "default_selected": not (code in BROAD_MARKET_INDEXES and code not in KEPT_BROAD_INDEXES),
        })
    return catalog


def _number(value: Any, fallback: float, bounds) -> float:
    low, high, integral = bounds
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(number):
        return fallback
    number = min(max(number, low), high)
    return float(int(round(number))) if integral else number


def _bool(value: Any, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return fallback
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _normalize_section(raw: Any, defaults: Mapping[str, Any], bounds: Mapping[str, Any]) -> Dict[str, Any]:
    source = raw if isinstance(raw, Mapping) else {}
    return {
        key: _number(source.get(key, defaults[key]), defaults[key], bound)
        for key, bound in bounds.items()
    }


def normalize_sector_nine_turn_config(raw: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """和默认值合并 + 夹到合法区间；未知键丢弃，缺失键回落默认值。"""
    defaults = copy.deepcopy(DEFAULT_CONFIG)
    source = raw if isinstance(raw, Mapping) else {}

    known_codes = {item["index_code"] for item in index_catalog()}
    universe_raw = source.get("universe") if isinstance(source.get("universe"), Mapping) else {}
    codes_raw = universe_raw.get("index_codes")
    codes: List[str] = []
    if isinstance(codes_raw, Sequence) and not isinstance(codes_raw, (str, bytes)):
        for code in codes_raw:
            text = str(code or "").upper()
            if text in known_codes and text not in codes:
                codes.append(text)
    result: Dict[str, Any] = {"universe": {"index_codes": codes}}

    signal_raw = source.get("signal") if isinstance(source.get("signal"), Mapping) else {}
    signal = _normalize_section(signal_raw, defaults["signal"], _SIGNAL_BOUNDS)
    sell_mode = str(signal_raw.get("sell_mode") or defaults["signal"]["sell_mode"])
    signal["sell_mode"] = sell_mode if sell_mode in SELL_MODES else defaults["signal"]["sell_mode"]
    result["signal"] = signal

    portfolio_raw = source.get("portfolio") if isinstance(source.get("portfolio"), Mapping) else {}
    portfolio = _normalize_section(portfolio_raw, defaults["portfolio"], _PORTFOLIO_BOUNDS)
    pick_order = str(portfolio_raw.get("pick_order") or defaults["portfolio"]["pick_order"])
    portfolio["pick_order"] = pick_order if pick_order in PICK_ORDERS else defaults["portfolio"]["pick_order"]
    result["portfolio"] = portfolio

    paper_raw = source.get("paper") if isinstance(source.get("paper"), Mapping) else {}
    paper = _normalize_section(paper_raw, defaults["paper"], _PAPER_BOUNDS)
    paper["enabled"] = _bool(paper_raw.get("enabled"), defaults["paper"]["enabled"])
    result["paper"] = paper
    return result


def resolve_index_codes(config: Mapping[str, Any]) -> List[str]:
    """配置里没显式勾选板块时回落到默认集合。"""
    codes = list((config.get("universe") or {}).get("index_codes") or [])
    return codes or _default_index_codes()


def default_sector_nine_turn_config() -> Dict[str, Any]:
    return normalize_sector_nine_turn_config(DEFAULT_CONFIG)


def load_sector_nine_turn_config() -> Dict[str, Any]:
    """读取配置（短事务）；表里没有行时返回默认值。附带 updated_at/updated_by。"""
    from ...database import SectorNineTurnConfig, SessionLocal

    with SessionLocal() as db:
        row = db.query(SectorNineTurnConfig).first()
        if row is None:
            return {**default_sector_nine_turn_config(), "updated_at": None, "updated_by": None}
        payload = dict(row.payload or {})
        updated_at = row.updated_at.isoformat() if row.updated_at else None
        updated_by = row.updated_by
    return {**normalize_sector_nine_turn_config(payload), "updated_at": updated_at, "updated_by": updated_by}


def save_sector_nine_turn_config(payload: Mapping[str, Any], *, updated_by: Optional[str] = None) -> Dict[str, Any]:
    """整份覆盖保存（先归一化）；返回保存后的快照。"""
    from datetime import datetime

    from ...database import SectorNineTurnConfig, SessionLocal

    normalized = normalize_sector_nine_turn_config(payload)
    with SessionLocal() as db:
        row = db.query(SectorNineTurnConfig).first()
        if row is None:
            row = SectorNineTurnConfig()
            db.add(row)
        row.payload = normalized
        row.updated_at = datetime.now()
        row.updated_by = updated_by
        db.commit()
    return {**normalized, "updated_at": datetime.now().isoformat(), "updated_by": updated_by}

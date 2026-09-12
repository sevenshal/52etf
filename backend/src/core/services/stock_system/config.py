"""选股交易系统的可配置超参数（全局单份）。

存在 SQLite ``stock_system_configs`` 表（单行 JSON）。读写都是独立短事务，返回普通
dict 快照；调用方不要在长事务里调用。

硬闸门的取向是**从宽**：只拦明显有问题的股票（最近 12 个月亏损、盈利但经营现金流
为负、负债/商誉失控、营收大幅萎缩），不拿多年平均指标去卡成长股；股票之间谁更好
交给可配置权重的软评分去排。
"""

from __future__ import annotations

import copy
import math
from typing import Any, Dict, List, Mapping, Optional

GROUP_LABELS = {
    "valuation": "估值",
    "growth": "成长",
    "quality": "质量",
    "expectation": "预期",
}

# 硬闸门：comparator=min 表示"不低于"，max 表示"不高于"。数据缺失时不拦，只记提示——
# 缺数据只是没信息，不是负面信号。
GATE_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "key": "min_latest_roic_pct",
        "label": "最近12个月 ROIC 不低于",
        "unit": "%",
        "comparator": "min",
        "applies_to": "non_financial",
        "description": "TTM 息税前利润×(1−税率)/投入资本。默认 0：只拦主业在亏钱的公司。亏损期成长股(如创新药)想纳入可以关掉。",
    },
    {
        "key": "min_latest_roe_financial_pct",
        "label": "金融股最近12个月 ROE 不低于",
        "unit": "%",
        "comparator": "min",
        "applies_to": "financial",
        "description": "银行/保险/证券不适用 ROIC，改看 TTM 归母净利/归母权益。",
    },
    {
        "key": "min_ocf_to_np_ttm",
        "label": "最近12个月 经营现金流/净利润 不低于",
        "unit": "倍",
        "comparator": "min",
        "applies_to": "non_financial",
        "description": "只在净利润为正时判断。默认 0：拦\"账面盈利、现金却在流出\"的利润注水嫌疑。",
        "missing_note": "净利润非正或缺少现金流数据，不适用",
    },
    {
        "key": "max_debt_to_assets_pct",
        "label": "资产负债率 不高于",
        "unit": "%",
        "comparator": "max",
        "applies_to": "non_financial",
        "description": "最新一期资产负债表。金融股的负债结构不同，不参与。",
    },
    {
        "key": "max_goodwill_to_equity_pct",
        "label": "商誉/归母净资产 不高于",
        "unit": "%",
        "comparator": "max",
        "applies_to": "all",
        "description": "商誉占净资产过高的公司有集中减值风险。",
    },
    {
        "key": "min_revenue_yoy_pct",
        "label": "营收同比 不低于",
        "unit": "%",
        "comparator": "min",
        "applies_to": "all",
        "description": "最新报告期累计营收同比。默认 −30：只拦生意在大幅萎缩的公司。",
    },
]

# 软评分因子：全部"越大越好"，在通过闸门的股票里做截面百分位，再按权重合成。
FACTOR_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "key": "dcf_return_pct",
        "label": "DCF 潜在回报率",
        "group": "valuation",
        "unit": "%",
        "description": "价值投资扫描器的再投资口径 DCF（金融股为剩余收益模型）算出的 内在价值/市值−1，按最新 TTM 口径。",
    },
    {
        "key": "consensus_upside_pct",
        "label": "卖方共识低估率",
        "group": "valuation",
        "unit": "%",
        "description": "当前财年目标价按机构去重后的最低下沿 / 收盘价 − 1（最悲观单家口径）。",
    },
    {
        "key": "earnings_yield_pct",
        "label": "盈利收益率(1/PE_TTM)",
        "group": "valuation",
        "unit": "%",
        "description": "亏损公司 PE 为空，此项缺失。",
    },
    {
        "key": "revenue_yoy_pct",
        "label": "营收同比",
        "group": "growth",
        "unit": "%",
        "description": "最新报告期累计营收同比（tushare or_yoy）。",
    },
    {
        "key": "profit_yoy_pct",
        "label": "扣非净利同比",
        "group": "growth",
        "unit": "%",
        "description": "最新报告期扣非归母净利同比，缺失时用归母净利同比。",
    },
    {
        "key": "quarter_revenue_yoy_pct",
        "label": "单季营收同比",
        "group": "growth",
        "unit": "%",
        "description": "最新单季营收同比，比累计口径更早反映拐点。",
    },
    {
        "key": "consensus_growth_pct",
        "label": "一致预期增速",
        "group": "expectation",
        "unit": "%",
        "description": "卖方一致预期下财年相对当前财年的增速。",
    },
    {
        "key": "consensus_revision_pct",
        "label": "目标价中枢变化",
        "group": "expectation",
        "unit": "%",
        "description": "共识目标价中位数相对 N 日前的变化（按复权因子换算到同一价格口径），反映卖方预期上修/下修。",
    },
    {
        "key": "profitability_pct",
        "label": "最近12个月 ROIC",
        "group": "quality",
        "unit": "%",
        "description": "TTM ROIC；金融股用 TTM ROE。",
    },
    {
        "key": "ocf_to_np_ttm",
        "label": "经营现金流/净利润",
        "group": "quality",
        "unit": "倍",
        "description": "最近12个月口径，净利润为正时才有值。",
    },
]

DEFAULT_CONFIG: Dict[str, Any] = {
    "universe": {
        "exclude_st": True,
        "avg_window_days": 5,
        "min_avg_total_mv_100m": 50.0,   # 近 N 日平均总市值下限（亿元）
        "min_avg_amount_10k": 1500.0,    # 近 N 日平均成交额下限（万元）
    },
    "gates": {
        "min_latest_roic_pct": {"enabled": True, "threshold": 0.0},
        "min_latest_roe_financial_pct": {"enabled": True, "threshold": 3.0},
        "min_ocf_to_np_ttm": {"enabled": True, "threshold": 0.0},
        "max_debt_to_assets_pct": {"enabled": True, "threshold": 85.0},
        "max_goodwill_to_equity_pct": {"enabled": True, "threshold": 50.0},
        "min_revenue_yoy_pct": {"enabled": True, "threshold": -30.0},
    },
    "factors": {
        "dcf_return_pct": {"enabled": True, "weight": 1.0},
        "consensus_upside_pct": {"enabled": True, "weight": 1.0},
        "earnings_yield_pct": {"enabled": True, "weight": 0.5},
        "revenue_yoy_pct": {"enabled": True, "weight": 1.0},
        "profit_yoy_pct": {"enabled": True, "weight": 1.0},
        "quarter_revenue_yoy_pct": {"enabled": True, "weight": 0.5},
        "consensus_growth_pct": {"enabled": True, "weight": 0.5},
        "consensus_revision_pct": {"enabled": True, "weight": 0.5},
        "profitability_pct": {"enabled": True, "weight": 1.0},
        "ocf_to_np_ttm": {"enabled": True, "weight": 0.5},
    },
    "scoring": {
        # 有值因子的权重之和占全部启用权重的比例低于它，不给综合分（数据太少排不准）
        "min_factor_coverage": 0.5,
        "revision_lookback_days": 60,
    },
    "pool": {
        "size": 100,
    },
    # 第二层：情绪面择时
    "timing": {
        # 定总仓位用的市场指数
        "market_index": "000985.SH",
        # 0 = 信号一直有效，直到出现反向信号；>0 时超过这么多个交易日退回中性
        "signal_expiry_days": 0,
        # 贪恐分数不低于它视为过热：不开新仓，仓位上限按防守档
        "overheat_enabled": True,
        "overheat_score": 80.0,
    },
    # 第二层：仓位控制（百分比都是占总资产）
    "position": {
        "max_positions": 20,
        "max_single_weight_pct": 8.0,
        "min_position_weight_pct": 2.0,
        # 市场状态 → 总仓位上限
        "exposure_offense_pct": 100.0,
        "exposure_neutral_pct": 70.0,
        "exposure_defense_pct": 40.0,
        # 板块状态 → 单一板块仓位上限（防守 0 = 不开新仓）
        "sector_cap_offense_pct": 30.0,
        "sector_cap_neutral_pct": 20.0,
        "sector_cap_defense_pct": 0.0,
        # 不属于任何行业贪恐指数的股票合在一起的上限，状态跟随市场
        "unmapped_sector_cap_pct": 20.0,
    },
    # 第三层：技术面信号（入场 = 任一触发 × 全部过滤；出场任一即卖）
    "signals": {
        "nine_turn_reversal": {"enabled": True, "min_low_count": 9, "window_days": 10},
        "support_bounce": {"enabled": True, "max_distance_atr": 1.0},
        "macd_golden_cross": {"enabled": True, "below_zero_only": False},
        "breakout": {"enabled": False, "breakout_atr": 0.15, "min_volume_z": 1.0},
        # 雪球过滤：N 日权价比 ≥ 阈值且持有组合数 ≥ 下限。数据历史短，做成开关；数据覆盖不到当天时自动跳过。
        # 窗口默认 5，与「雪球持仓」页面和 K 线雪球副图的 5日权价比同一口径。
        "xueqiu_ratio": {
            "enabled": True,
            "lookback_days": 5,
            "min_ratio": 1.25,
            "min_holding_cubes": 8,
            "block_missing": True,
        },
        "initial_stop_atr": 2.0,
        "trailing_stop_atr": 2.0,
        "defense_trailing_stop_atr": 1.5,
        "trailing_min_low_count": 2,
        "take_profit_high_nine": False,
        "exit_on_gate_fail": True,
        "exit_pool_rank": 200,
        "risk_per_trade_pct": 1.0,
    },
    # 模拟盘：信号日收盘出单，下一交易日开盘成交
    "paper": {
        "enabled": True,
        "initial_capital": 1000000.0,
        "commission_pct": 0.03,
        "stamp_tax_pct": 0.05,
    },
}

# 第三层参数的合法区间（未列出的数值按 ±1e12 处理）
_SIGNAL_BOUNDS = {
    "nine_turn_reversal": {"min_low_count": (2, 13, True), "window_days": (1, 60, True)},
    "support_bounce": {"max_distance_atr": (0.0, 5.0, False)},
    "breakout": {"breakout_atr": (0.0, 3.0, False), "min_volume_z": (-3.0, 5.0, False)},
    "xueqiu_ratio": {
        "lookback_days": (1, 30, True),
        "min_ratio": (0.0, 10.0, False),
        "min_holding_cubes": (0, 500, True),
    },
    "initial_stop_atr": (0.5, 10.0, False),
    "trailing_stop_atr": (0.5, 10.0, False),
    "defense_trailing_stop_atr": (0.5, 10.0, False),
    "trailing_min_low_count": (0, 9, True),
    "exit_pool_rank": (1, 5000, True),
    "risk_per_trade_pct": (0.1, 10.0, False),
}
_PAPER_BOUNDS = {
    "initial_capital": (10000.0, 1e10, False),
    "commission_pct": (0.0, 1.0, False),
    "stamp_tax_pct": (0.0, 1.0, False),
}

# 宽基和风格指数：用来判断市场，不当作个股所属的"板块"（板块只取行业/主题指数）
BROAD_INDEX_CODES = frozenset({
    "000300.SH", "000016.SH", "000510.SH", "000905.SH", "000852.SH", "932000.CSI", "000985.SH",
    "899050.BJ", "000680.SH", "000688.SH", "000698.SH", "000699.SH", "399006.SZ",
    "000015.SH", "H30269.CSI", "INNO100.CN",
})

# 数值型参数的合法区间：(最小值, 最大值, 是否取整)
_UNIVERSE_BOUNDS = {
    "avg_window_days": (1, 60, True),
    "min_avg_total_mv_100m": (0.0, 100000.0, False),
    "min_avg_amount_10k": (0.0, 10000000.0, False),
}
_SCORING_BOUNDS = {
    "min_factor_coverage": (0.0, 1.0, False),
    "revision_lookback_days": (5, 365, True),
}
_POOL_BOUNDS = {"size": (1, 1000, True)}
_GATE_THRESHOLD_BOUNDS = (-1000.0, 100000.0)
_FACTOR_WEIGHT_BOUNDS = (0.0, 100.0)
_TIMING_BOUNDS = {
    "signal_expiry_days": (0, 250, True),
    "overheat_score": (50.0, 100.0, False),
}
_POSITION_BOUNDS = {
    "max_positions": (1, 200, True),
    **{
        key: (0.0, 100.0, False)
        for key in DEFAULT_CONFIG["position"]
        if key.endswith("_pct")
    },
}


def market_index_options() -> List[Dict[str, str]]:
    """可以用来定总仓位的市场指数：有贪恐计算的宽基/风格指数。"""
    from ....robot.a_stock_base_data_config import A_STOCK_INDEX_FEAR_GREED_TARGETS

    options = []
    seen = set()
    for target in A_STOCK_INDEX_FEAR_GREED_TARGETS:
        symbol = str(target.get("symbol") or "").upper()
        if symbol in BROAD_INDEX_CODES and symbol not in seen:
            seen.add(symbol)
            options.append({"symbol": symbol, "name": str(target.get("ticker") or target.get("label") or symbol)})
    return options


def config_definitions() -> Dict[str, Any]:
    """前端据此渲染配置表单；新增闸门/因子只需改这里。"""
    from .technical import TRIGGER_DEFINITIONS

    return {
        "groups": GROUP_LABELS,
        "gates": copy.deepcopy(GATE_DEFINITIONS),
        "factors": copy.deepcopy(FACTOR_DEFINITIONS),
        "market_index_options": market_index_options(),
        "triggers": copy.deepcopy(TRIGGER_DEFINITIONS),
    }


def _normalize_section(raw: Any, defaults: Mapping[str, Any], bounds: Mapping[str, Any]) -> Dict[str, Any]:
    """按默认值的结构递归归一化：布尔走 _bool，数值夹到 bounds（缺省 ±1e12），子字典递归。"""
    raw = raw if isinstance(raw, Mapping) else {}
    result: Dict[str, Any] = {}
    for key, default in defaults.items():
        value = raw.get(key, default)
        if isinstance(default, Mapping):
            result[key] = _normalize_section(value, default, bounds.get(key) or {})
        elif isinstance(default, bool):
            result[key] = _bool(value, default)
        else:
            key_bounds = bounds.get(key) or (-1e12, 1e12, isinstance(default, int))
            result[key] = _number(value, default, key_bounds)
    return result


def _number(value: Any, fallback: float, bounds: tuple) -> float:
    low, high, *rest = bounds
    as_int = bool(rest and rest[0])
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(fallback)
    if not math.isfinite(number):
        number = float(fallback)
    number = min(max(number, low), high)
    return int(round(number)) if as_int else number


def _bool(value: Any, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return fallback


def normalize_stock_system_config(raw: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """与默认值合并、类型收敛、夹到合法区间；未知键丢弃。可重复调用。"""
    raw = raw or {}
    defaults = DEFAULT_CONFIG
    result: Dict[str, Any] = {}

    universe_raw = raw.get("universe") or {}
    universe = {"exclude_st": _bool(universe_raw.get("exclude_st"), defaults["universe"]["exclude_st"])}
    for key, bounds in _UNIVERSE_BOUNDS.items():
        universe[key] = _number(universe_raw.get(key, defaults["universe"][key]), defaults["universe"][key], bounds)
    result["universe"] = universe

    gates_raw = raw.get("gates") or {}
    result["gates"] = {}
    for key, default in defaults["gates"].items():
        item = gates_raw.get(key) or {}
        result["gates"][key] = {
            "enabled": _bool(item.get("enabled"), default["enabled"]),
            "threshold": _number(item.get("threshold", default["threshold"]), default["threshold"], _GATE_THRESHOLD_BOUNDS),
        }

    factors_raw = raw.get("factors") or {}
    result["factors"] = {}
    for key, default in defaults["factors"].items():
        item = factors_raw.get(key) or {}
        result["factors"][key] = {
            "enabled": _bool(item.get("enabled"), default["enabled"]),
            "weight": _number(item.get("weight", default["weight"]), default["weight"], _FACTOR_WEIGHT_BOUNDS),
        }

    scoring_raw = raw.get("scoring") or {}
    result["scoring"] = {
        key: _number(scoring_raw.get(key, defaults["scoring"][key]), defaults["scoring"][key], bounds)
        for key, bounds in _SCORING_BOUNDS.items()
    }

    pool_raw = raw.get("pool") or {}
    result["pool"] = {
        key: _number(pool_raw.get(key, defaults["pool"][key]), defaults["pool"][key], bounds)
        for key, bounds in _POOL_BOUNDS.items()
    }

    timing_raw = raw.get("timing") or {}
    timing_defaults = defaults["timing"]
    market_index = str(timing_raw.get("market_index") or timing_defaults["market_index"]).strip().upper()
    if market_index not in {option["symbol"] for option in market_index_options()}:
        market_index = timing_defaults["market_index"]
    result["timing"] = {
        "market_index": market_index,
        "overheat_enabled": _bool(timing_raw.get("overheat_enabled"), timing_defaults["overheat_enabled"]),
        **{
            key: _number(timing_raw.get(key, timing_defaults[key]), timing_defaults[key], bounds)
            for key, bounds in _TIMING_BOUNDS.items()
        },
    }

    position_raw = raw.get("position") or {}
    result["position"] = {
        key: _number(position_raw.get(key, defaults["position"][key]), defaults["position"][key], bounds)
        for key, bounds in _POSITION_BOUNDS.items()
    }

    result["signals"] = _normalize_section(raw.get("signals"), defaults["signals"], _SIGNAL_BOUNDS)
    result["paper"] = _normalize_section(raw.get("paper"), defaults["paper"], _PAPER_BOUNDS)
    return result


def default_stock_system_config() -> Dict[str, Any]:
    return normalize_stock_system_config(DEFAULT_CONFIG)


def load_stock_system_config() -> Dict[str, Any]:
    """读取配置（短事务）；表里没有行时返回默认值。附带 updated_at/updated_by。"""
    from ...database import SessionLocal, StockSystemConfig

    with SessionLocal() as db:
        row = db.query(StockSystemConfig).first()
        if row is None:
            return {**default_stock_system_config(), "updated_at": None, "updated_by": None}
        payload = dict(row.payload or {})
        updated_at = row.updated_at.isoformat() if row.updated_at else None
        updated_by = row.updated_by
    return {**normalize_stock_system_config(payload), "updated_at": updated_at, "updated_by": updated_by}


def save_stock_system_config(payload: Mapping[str, Any], *, updated_by: Optional[str] = None) -> Dict[str, Any]:
    """整份覆盖保存（先归一化）；返回保存后的快照。"""
    from datetime import datetime

    from ...database import SessionLocal, StockSystemConfig

    normalized = normalize_stock_system_config(payload)
    with SessionLocal() as db:
        row = db.query(StockSystemConfig).first()
        if row is None:
            row = StockSystemConfig()
            db.add(row)
        row.payload = normalized
        row.updated_at = datetime.now()
        row.updated_by = updated_by
        db.commit()
    return load_stock_system_config()


def reset_stock_system_config(*, updated_by: Optional[str] = None) -> Dict[str, Any]:
    return save_stock_system_config(default_stock_system_config(), updated_by=updated_by)


def strip_metadata(config: Mapping[str, Any]) -> Dict[str, Any]:
    """去掉 updated_at/updated_by，只留参与计算的部分（快照里存的就是它）。"""
    return normalize_stock_system_config(config)

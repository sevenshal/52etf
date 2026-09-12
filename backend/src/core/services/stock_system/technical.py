"""选股系统第三层：技术面——入场触发、雪球权价比过滤、止损与出场。

所有指标来自 ``indicators.compute_chart_indicators``：与个股详情页 K 线图同一套算法、同一组默认参数
（支撑压力窗口 125、放量 z 值 60 日、九转/ATR14、MACD 12/26/9），页面上看到的红绿点、支撑压力线和
这里判定用的是同一份数字。

入场 = 至少一个触发 × 全部过滤都通过（候选只来自第二层分到目标仓位的股票）：

- 触发（任一即可，逐条可关）
  - 九转低位反转：最近 N 天内出现过低九（lowCount ≥ 9），今天是其后第一次高二——项目研究里的买点口径；
  - 回踩支撑企稳：今天最低价回踩到最近支撑上方 ≤ k 个 ATR，收阳且收盘高于昨收；
  - MACD 金叉：DIF 今天上穿 DEA（可限定零轴下方）；
  - 放量突破压力：收盘站上昨天的最近压力 + k×ATR 且放量 z 值达标。研究里突破没有显著优势，默认关闭。
- 过滤（逐条可关）：雪球 N 日权价比 ≥ 阈值。

出场（任一即卖）：止损（较买入跌破 k×ATR 对应的幅度）、移动止损（买入后最近红点收盘回撤 ≥ k 个 ATR
且出现低 N）、可选高九止盈、基本面闸门不通过、跌出股票池排名缓冲、离开股票池范围。板块或市场处于防守时
移动止损收紧到防守档——研究显示顶部信号直接清仓在强趋势里离场过早，所以防守只收紧止损、不直接卖。
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence

# 止损幅度的上下限：ATR 很小的股票止损太近会被日内噪音打掉，ATR 很大的股票止损太远没有意义
STOP_PCT_BOUNDS = (0.02, 0.25)
MIN_BARS = 35

TRIGGER_DEFINITIONS: List[Dict[str, str]] = [
    {
        "key": "nine_turn_reversal",
        "label": "九转低位反转",
        "description": "最近 N 个交易日内出现过低九，今天是其后第一次高二（K 线图上的绿点转红点）。",
    },
    {
        "key": "support_bounce",
        "label": "回踩支撑企稳",
        "description": "今天最低价回踩到 K 线图「最近支撑」上方 k 个 ATR 以内，收阳且收盘高于昨收。",
    },
    {
        "key": "macd_golden_cross",
        "label": "MACD 金叉",
        "description": "DIF 今天上穿 DEA，可限定只在零轴下方的金叉。",
    },
    {
        "key": "breakout",
        "label": "放量突破压力",
        "description": "收盘站上昨天的「最近压力」+ k 个 ATR，且放量 z 值达标。研究里突破没有显著优势，默认关闭。",
    },
]
TRIGGER_LABELS = {item["key"]: item["label"] for item in TRIGGER_DEFINITIONS}
EXIT_LABELS = {
    "stop_loss": "止损",
    "trailing_stop": "移动止损",
    "take_profit_high_nine": "高九止盈",
    "gate_fail": "基本面闸门未通过",
    "pool_exit": "跌出股票池",
    "universe_exit": "离开股票池范围",
}


def _num(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def bar_date(row: Mapping[str, Any]) -> Optional[str]:
    value = row.get("trade_date") or row.get("timestamp")
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def _level(support_resistance: Optional[Mapping[str, Any]], side: str, role: str) -> Optional[Mapping[str, Any]]:
    levels = (support_resistance or {}).get("supports" if side == "support" else "resistances") or []
    return next((level for level in levels if role in (level.get("roles") or [])), None)


def _trigger(key: str, detail: str) -> Dict[str, str]:
    return {"key": key, "label": TRIGGER_LABELS[key], "detail": detail}


def evaluate_entry(
    rows: Sequence[Mapping[str, Any]],
    macd: Mapping[str, Sequence[Optional[float]]],
    signals: Mapping[str, Any],
) -> Dict[str, Any]:
    """用最后一根 K 线判定入场触发，并给出按 ATR 算的止损幅度（百分比）和止损价。"""
    result: Dict[str, Any] = {
        "triggers": [], "close": None, "atr": None, "stop_pct": None, "stop_price": None,
        "bar_date": None, "note": None,
    }
    if len(rows) < MIN_BARS:
        result["note"] = f"K 线不足 {MIN_BARS} 根"
        return result
    today, prev = rows[-1], rows[-2]
    close = _num(today.get("close"))
    atr = _num(today.get("atr14"))
    result.update(close=close, atr=atr, bar_date=bar_date(today))
    triggers: List[Dict[str, str]] = []

    config = signals["nine_turn_reversal"]
    if config["enabled"] and today.get("highCount") == 2:
        last = len(rows) - 1
        start = max(0, last - int(config["window_days"]))
        low_index = next(
            (j for j in range(last - 1, start - 1, -1) if (rows[j].get("lowCount") or 0) >= config["min_low_count"]),
            None,
        )
        if low_index is not None and all((rows[k].get("highCount") or 0) < 2 for k in range(low_index + 1, last)):
            triggers.append(_trigger(
                "nine_turn_reversal",
                f"{bar_date(rows[low_index])} 低{rows[low_index].get('lowCount')}后首次高二",
            ))

    config = signals["support_bounce"]
    if config["enabled"] and atr and atr > 0:
        support = _level(today.get("support_resistance"), "support", "nearest")
        low, open_price, prev_close = _num(today.get("low")), _num(today.get("open")), _num(prev.get("close"))
        if support and None not in (low, open_price, prev_close, close):
            level = float(support["price"])
            distance = (low - level) / atr
            if distance <= config["max_distance_atr"] and close > level and close > open_price and close > prev_close:
                triggers.append(_trigger(
                    "support_bounce",
                    f"最低 {low:.2f} 回踩最近支撑 {level:.2f}（{distance:.2f} ATR）后收阳",
                ))

    config = signals["macd_golden_cross"]
    dif, dea = list(macd.get("dif") or []), list(macd.get("dea") or [])
    if config["enabled"] and len(dif) >= 2 and len(dea) >= 2 and None not in (dif[-1], dea[-1], dif[-2], dea[-2]):
        crossed = dif[-1] > dea[-1] and dif[-2] <= dea[-2]
        if crossed and (not config["below_zero_only"] or dif[-1] < 0):
            triggers.append(_trigger("macd_golden_cross", f"DIF {dif[-1]:.3f} 上穿 DEA {dea[-1]:.3f}"))

    config = signals["breakout"]
    if config["enabled"] and atr and atr > 0:
        resistance = _level(prev.get("support_resistance"), "resistance", "nearest")
        volume_z = _num(today.get("volumeZScore"))
        if resistance and close is not None and volume_z is not None:
            level = float(resistance["price"])
            if close > level + config["breakout_atr"] * atr and volume_z >= config["min_volume_z"]:
                triggers.append(_trigger(
                    "breakout",
                    f"收盘 {close:.2f} 突破压力 {level:.2f}，放量 z={volume_z:.2f}",
                ))

    result["triggers"] = triggers
    if close and atr and atr > 0:
        stop = min(max(signals["initial_stop_atr"] * atr / close, STOP_PCT_BOUNDS[0]), STOP_PCT_BOUNDS[1])
        result["stop_pct"] = round(stop * 100.0, 2)
        result["stop_price"] = round(close * (1 - stop), 2)
    return result


def evaluate_xueqiu_filter(
    item: Optional[Mapping[str, Any]],
    config: Mapping[str, Any],
    availability: Mapping[str, Any],
) -> Dict[str, Any]:
    """雪球过滤：N 日权价比 ≥ 阈值，且当前持有它的活跃组合数 ≥ 下限。

    数据覆盖不到当天（历史太短、快照缺失）时自动跳过，只记提示。不在雪球持仓里、或 N 个
    快照日前还没持有（新进，算不出权价比）的，按 ``block_missing`` 决定拦还是放。
    """
    if not config["enabled"]:
        return {"status": "disabled", "passed": True, "ratio": None, "holding_cubes": None, "detail": None}
    if not availability.get("available"):
        return {"status": "unavailable", "passed": True, "ratio": None, "holding_cubes": None,
                "detail": f"雪球数据不可用，本日跳过此过滤：{availability.get('reason')}"}
    item = item or {}
    lookback = config["lookback_days"]
    holding_cubes = int(item.get("holding_cube_count") or 0)
    ratio = _num(item.get("weight_price_ratio"))
    base = {"ratio": ratio, "holding_cubes": holding_cubes,
            "weight_multiple": _num(item.get("weight_multiple")), "price_multiple": _num(item.get("price_multiple"))}
    if holding_cubes == 0:
        return {**base, "status": "missing", "passed": not config["block_missing"], "detail": "不在雪球活跃组合持仓里"}
    if holding_cubes < config["min_holding_cubes"]:
        return {**base, "status": "fail", "passed": False,
                "detail": f"雪球持有组合 {holding_cubes} 个 < {config['min_holding_cubes']}"}
    if ratio is None:
        return {**base, "status": "missing", "passed": not config["block_missing"],
                "detail": f"{lookback} 个快照日前还没有组合持有（新进），算不出权价比"}
    passed = ratio >= config["min_ratio"]
    return {
        **base,
        "status": "pass" if passed else "fail",
        "passed": passed,
        "detail": (
            f"{lookback}日权价比 {ratio:.2f}{' ≥ ' if passed else ' < '}{config['min_ratio']:g}，"
            f"持有组合 {holding_cubes} 个"
        ),
    }


def planned_weight_pct(target_weight_pct: Optional[float], stop_pct: Optional[float], risk_per_trade_pct: float) -> Optional[float]:
    """单笔风险预算：触发止损时亏掉的不超过总资产的 risk_per_trade_pct；再受第二层目标仓位约束。"""
    if target_weight_pct is None or target_weight_pct <= 0:
        return None
    if not stop_pct or stop_pct <= 0:
        return round(target_weight_pct, 2)
    return round(min(target_weight_pct, risk_per_trade_pct / stop_pct * 100.0), 2)


def evaluate_exit(
    rows: Sequence[Mapping[str, Any]],
    *,
    entry_date: date,
    return_pct: Optional[float],
    stop_pct: Optional[float],
    defending: bool,
    in_universe: bool,
    gate_passed: Optional[bool],
    pool_rank: Optional[int],
    signals: Mapping[str, Any],
) -> List[Dict[str, str]]:
    """持仓的出场理由（空列表 = 继续持有）。"""
    exits: List[Dict[str, str]] = []

    def add(key: str, detail: str) -> None:
        exits.append({"key": key, "label": EXIT_LABELS[key], "detail": detail})

    if return_pct is not None and stop_pct is not None and return_pct <= -stop_pct:
        add("stop_loss", f"较买入 {return_pct:.1f}%，止损线 −{stop_pct:.1f}%")

    if rows:
        today = rows[-1]
        close, atr = _num(today.get("close")), _num(today.get("atr14"))
        entry_iso = entry_date.isoformat()
        # 锚点是买入以后最近的红点（高 N≥2），不是图上"最近任意红点"：买入前的高点不该拿来算回撤
        anchor = next(
            (row for row in reversed(rows) if (row.get("highCount") or 0) >= 2 and (bar_date(row) or "") >= entry_iso),
            None,
        )
        multiple = signals["defense_trailing_stop_atr"] if defending else signals["trailing_stop_atr"]
        if anchor is not None and close is not None and atr and atr > 0:
            drawdown = (float(anchor["close"]) - close) / atr
            low_count = today.get("lowCount") or 0
            if drawdown >= multiple and low_count >= signals["trailing_min_low_count"]:
                add(
                    "trailing_stop",
                    f"较买入后红点 {bar_date(anchor)} 收盘回撤 {drawdown:.2f} ATR（阈值 {multiple:g}{'，防守收紧' if defending else ''}），低{low_count}",
                )
        if signals["take_profit_high_nine"] and today.get("highCount") == 9:
            add("take_profit_high_nine", "今天出现高九")

    if not in_universe:
        add("universe_exit", "已不在股票池范围内（ST、市值或成交额不达标、停牌）")
    elif gate_passed is False:
        if signals["exit_on_gate_fail"]:
            add("gate_fail", "最新一期基本面硬闸门未通过")
    elif pool_rank is None or pool_rank > signals["exit_pool_rank"]:
        add("pool_exit", f"综合排名 {pool_rank if pool_rank is not None else '无'}，超出 {signals['exit_pool_rank']} 名缓冲")
    return exits

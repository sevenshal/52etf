"""板块九转策略的信号定义——回测、每日信号、模拟盘共用这一份。

九转计数、ATR14 和"距最近红点的回撤"全部来自 ``stock_system.indicators.append_nine_turn_atr``，
也就是个股详情页 K 线图用的那一套：页面上看到的红点/绿点，和这里判定买卖用的是同一个函数。

三条规则：

- **板块触发**：板块自身出现低 N（默认 ``lowCount >= 9``）之后，**首次**出现高 M（默认
  ``highCount == 2``）的那一天。同一次低 N 只消费一次。
- **个股买入**：板块触发后进入布防窗口（默认 0 个交易日，即板块与个股必须同日），
  窗口内成分股自身也出现"低 N 后首次高 M"。
- **卖出**：买入后出现高 K（默认 ``highCount >= 9``），此后首次出现低 L（默认
  ``lowCount == 2``）且最近红点收盘到当日收盘的回撤 > N 个 ATR14。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ..stock_system.indicators import append_nine_turn_atr

SELL_MODE_WAIT = "low2_wait"
SELL_MODE_FIRST_ONLY = "low2_first_only"
SELL_MODE_GE = "low_ge2_wait"


@dataclass(frozen=True)
class SignalParams:
    """从配置里取出的信号参数；回测和实盘都从同一份配置派生，避免两边漂移。"""

    low_count_min: int = 9
    buy_high_count: int = 2
    arm_window_days: int = 0
    high_count_min: int = 9
    sell_low_count: int = 2
    sell_atr_multiple: float = 2.0
    sell_mode: str = SELL_MODE_WAIT
    fear_threshold: float = 40.0

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "SignalParams":
        signal = dict((config or {}).get("signal") or {})
        return cls(
            low_count_min=int(signal.get("low_count_min", 9)),
            buy_high_count=int(signal.get("buy_high_count", 2)),
            arm_window_days=int(signal.get("arm_window_days", 0)),
            high_count_min=int(signal.get("high_count_min", 9)),
            sell_low_count=int(signal.get("sell_low_count", 2)),
            sell_atr_multiple=float(signal.get("sell_atr_multiple", 2.0)),
            sell_mode=str(signal.get("sell_mode") or SELL_MODE_WAIT),
            fear_threshold=float(signal.get("fear_threshold", 40.0)),
        )


def nine_turn_rows(klines: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """逐根补九转计数 / ATR14 / 红点回撤（生产口径，与 K 线图一致）。"""
    return append_nine_turn_atr(klines or [])


def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def low_high_turn_indices(rows: Sequence[Mapping[str, Any]], params: SignalParams) -> List[int]:
    """"低 N 后首次高 M" 的行号列表；同一次低 N 只触发一次。

    板块触发和个股买入用的是同一个函数——两边的形态定义必须一模一样。
    """
    armed = False
    fired: List[int] = []
    for index, row in enumerate(rows):
        low_count = int(row.get("lowCount") or 0)
        high_count = int(row.get("highCount") or 0)
        if low_count >= params.low_count_min:
            armed = True
        if armed and high_count == params.buy_high_count:
            fired.append(index)
            armed = False
    return fired


def low9_armed_state(rows: Sequence[Mapping[str, Any]], params: SignalParams) -> List[Dict[str, Any]]:
    """逐根给出"低 N 是否已出现、出现在哪一天、当天是否触发"，页面用来解释板块状态。"""
    armed = False
    armed_index: Optional[int] = None
    state: List[Dict[str, Any]] = []
    for index, row in enumerate(rows):
        low_count = int(row.get("lowCount") or 0)
        high_count = int(row.get("highCount") or 0)
        if low_count >= params.low_count_min:
            armed = True
            armed_index = index
        triggered = armed and high_count == params.buy_high_count
        state.append({"armed": armed, "armed_index": armed_index, "triggered": triggered})
        if triggered:
            armed = False
            armed_index = None
    return state


def sell_signal_index(rows: Sequence[Mapping[str, Any]], entry_index: int,
                      params: SignalParams) -> Optional[int]:
    """买入后第一个满足卖出规则的行号（信号日，次一交易日开盘成交）。"""
    armed = False
    for index in range(entry_index, len(rows)):
        row = rows[index]
        if int(row.get("highCount") or 0) >= params.high_count_min:
            armed = True
            continue
        if not armed:
            continue
        if evaluate_sell_row(row, params):
            return index
        if params.sell_mode == SELL_MODE_FIRST_ONLY and _matches_sell_count(row, params):
            # 高 K 后的第一个低 L 回撤不够，本轮作废，等下一次高 K。
            armed = False
    return None


def _matches_sell_count(row: Mapping[str, Any], params: SignalParams) -> bool:
    low_count = int(row.get("lowCount") or 0)
    if params.sell_mode == SELL_MODE_GE:
        return low_count >= params.sell_low_count
    return low_count == params.sell_low_count


def evaluate_sell_row(row: Mapping[str, Any], params: SignalParams) -> bool:
    """单根 K 线是否满足"低 L + 回撤够"（不含"之前出现过高 K"这一条，由调用方跟踪）。"""
    if not _matches_sell_count(row, params):
        return False
    drawdown = row.get("risingDrawdownAtr")
    return _finite(drawdown) and float(drawdown) > params.sell_atr_multiple


def sector_triggers(rows: Sequence[Mapping[str, Any]], dates: Sequence[date],
                    params: SignalParams,
                    fear_scores: Optional[Mapping[date, float]] = None) -> List[Dict[str, Any]]:
    """板块触发日列表，附带当天贪恐分数和是否过闸门。"""
    triggers: List[Dict[str, Any]] = []
    for index in low_high_turn_indices(rows, params):
        day = dates[index]
        score = None
        if fear_scores is not None:
            raw = fear_scores.get(day)
            score = float(raw) if _finite(raw) else None
        triggers.append({
            "index": index,
            "signal_date": day,
            "fear_score": score,
            "fear_passed": score is not None and score <= params.fear_threshold,
        })
    return triggers


def armed_windows(triggers: Sequence[Mapping[str, Any]], dates: Sequence[date],
                  params: SignalParams, *, require_fear: bool = True) -> Dict[date, Dict[str, Any]]:
    """把板块触发展开成"哪些交易日这个板块处于布防状态"。

    窗口 0 表示只有触发日当天可选股；窗口 N 表示触发日之后 N 个交易日内都可选。
    """
    windows: Dict[date, Dict[str, Any]] = {}
    for trigger in triggers:
        if require_fear and not trigger.get("fear_passed"):
            continue
        start = int(trigger["index"])
        for offset in range(0, params.arm_window_days + 1):
            position = start + offset
            if position >= len(dates):
                break
            # 后触发的板块信号覆盖先触发的，贪恐分数跟着最近一次
            windows[dates[position]] = {
                "signal_date": trigger["signal_date"],
                "fear_score": trigger.get("fear_score"),
            }
    return windows

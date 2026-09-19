"""板块九转策略的信号定义——回测、每日信号、模拟盘共用这一份。

九转计数、ATR14 和"距最近红点的回撤"全部来自 ``stock_system.indicators.append_nine_turn_atr``，
也就是个股详情页 K 线图用的那一套：页面上看到的红点/绿点，和这里判定买卖用的是同一个函数。

三条规则：

- **板块触发**：板块自身出现低 N（默认 ``lowCount >= 9``）之后，**首次**出现高 M（默认
  ``highCount`` 落在板块区间内，默认就是 2）的那一天。同一次低 N 只消费一次。触发日回看最近若干个交易日（默认 5 个，
  含当天），只要**其中任意一天**的自算贪恐分数 ≤ 闸门就算过——贪恐见底和九转翻红往往差几天，
  只看触发当天会漏掉刚反弹上来的那一批。
- **个股买入**：板块触发后进入布防窗口（默认 0 个交易日，即板块与个股必须同日），
  窗口内成分股自身也出现"低 N 后高 N 落进 [1, 4] 区间"，**且那一根要放量**——log 成交量比不含当日、
  往前 20 个交易日的平均 log 成交量高出至少 1 个标准差（倍数和回看天数都可配）。
  放量 z 值用的是 K 线图画放量标记的同一个函数 ``preprocess_klines_volume``。
- **卖出**：买入后出现高 K（默认 ``highCount >= 9``），此后首次出现低 L（默认
  ``lowCount == 2``）且最近红点收盘到当日收盘的回撤 > N 个 ATR14。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ..stock_system.indicators import append_nine_turn_atr, preprocess_klines_volume

SELL_MODE_WAIT = "low2_wait"
SELL_MODE_FIRST_ONLY = "low2_first_only"
SELL_MODE_GE = "low_ge2_wait"


@dataclass(frozen=True)
class SignalParams:
    """从配置里取出的信号参数；回测和实盘都从同一份配置派生，避免两边漂移。

    这里的字段默认值必须和 ``config.DEFAULT_CONFIG`` 保持一致（有测试盯着）：
    直接 ``SignalParams()`` 构造出来的，要和走配置的那条路一模一样。
    """

    low_count_min: int = 9
    # 个股买点允许的高 N 范围（含两端）；范围内第一根**且放量**的那根才是信号，
    # 所以范围给量能过滤留了重试机会：高1不放量就等高2、高3……冲过上限这次低N作废
    buy_high_min: int = 2
    buy_high_max: int = 4
    # 板块触发用的高 N 范围（板块不看量能，所以这里通常是一个点）
    sector_high_min: int = 2
    sector_high_max: int = 2
    arm_window_days: int = 0
    high_count_min: int = 9
    sell_low_count: int = 2
    sell_atr_multiple: float = 2.0
    sell_mode: str = SELL_MODE_WAIT
    fear_threshold: float = 40.0
    fear_lookback_days: int = 3
    volume_filter_enabled: bool = True
    volume_z_min: float = 1.0
    volume_lookback_days: int = 20

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "SignalParams":
        signal = dict((config or {}).get("signal") or {})
        return cls(
            low_count_min=int(signal.get("low_count_min", 9)),
            buy_high_min=int(signal.get("buy_high_min", 1)),
            buy_high_max=int(signal.get("buy_high_max", 4)),
            sector_high_min=int(signal.get("sector_high_min", 2)),
            sector_high_max=int(signal.get("sector_high_max", 2)),
            arm_window_days=int(signal.get("arm_window_days", 0)),
            high_count_min=int(signal.get("high_count_min", 9)),
            sell_low_count=int(signal.get("sell_low_count", 2)),
            sell_atr_multiple=float(signal.get("sell_atr_multiple", 2.0)),
            sell_mode=str(signal.get("sell_mode") or SELL_MODE_WAIT),
            fear_threshold=float(signal.get("fear_threshold", 40.0)),
            fear_lookback_days=int(signal.get("fear_lookback_days", 5)),
            volume_filter_enabled=bool(signal.get("volume_filter_enabled", True)),
            volume_z_min=float(signal.get("volume_z_min", 1.0)),
            volume_lookback_days=int(signal.get("volume_lookback_days", 20)),
        )


def nine_turn_rows(klines: Sequence[Mapping[str, Any]],
                   params: Optional[SignalParams] = None) -> List[Dict[str, Any]]:
    """逐根补九转计数 / ATR14 / 红点回撤（生产口径，与 K 线图一致）。

    给了 ``params`` 且开了量能过滤时，先跑一遍 ``preprocess_klines_volume`` 补放量 z 值——
    和 K 线图上的放量标记同一个函数、同一个口径（log10 成交量，窗口不含当根）。
    板块层不看量能，所以不传 ``params``。
    """
    rows = list(klines or [])
    if params is not None and params.volume_filter_enabled:
        rows = preprocess_klines_volume(rows, params.volume_z_min, params.volume_lookback_days)
    return append_nine_turn_atr(rows)


def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def volume_passed(row: Mapping[str, Any], params: SignalParams) -> bool:
    """当天是不是放量：放量 z 值 > 阈值。关了过滤或真的没数据时不拦。"""
    if not params.volume_filter_enabled:
        return True
    z_score = row.get("volumeZScore")
    if _finite(z_score):
        return float(z_score) > params.volume_z_min
    # 窗口内成交量一点波动都没有（标准差 0）时 z 值算不出来，但"比均值大不大"仍然有意义
    log_volume, log_mean = row.get("logVolume"), row.get("logVolumeMean")
    if _finite(log_volume) and _finite(log_mean):
        return float(log_volume) > float(log_mean)
    # 上市不满一个回看窗口、或当天停牌没有成交量：没信息不算负面信号，不拦
    return True


def low_high_turn_indices(rows: Sequence[Mapping[str, Any]], params: SignalParams,
                          *, high_min: Optional[int] = None, high_max: Optional[int] = None,
                          require_volume: bool = False) -> List[int]:
    """"低 N 后首个落在高 [min, max] 区间、（可选）且放量的那根" 的行号；同一次低 N 只触发一次。

    板块触发和个股买入用的是同一个函数——两边的形态定义必须一模一样，只是区间和是否看量能
    由调用方给：板块用 ``sector_high_*`` 且不看量能，个股用 ``buy_high_*`` 且要求放量。

    区间的意义在于给量能过滤留重试机会：高1那根没放量就等高2、高3……一旦高 N 冲过上限，
    这一次低 N 就作废，等下一次低 N。中途涨势断了（高 N 归零）不算作废，还可以等下一波。
    """
    low = params.buy_high_min if high_min is None else high_min
    high = params.buy_high_max if high_max is None else high_max
    armed = False
    fired: List[int] = []
    for index, row in enumerate(rows):
        low_count = int(row.get("lowCount") or 0)
        high_count = int(row.get("highCount") or 0)
        if low_count >= params.low_count_min:
            armed = True
            continue
        if not armed:
            continue
        if high_count > high:
            armed = False               # 已经冲过区间上限，这次低 N 作废
            continue
        if high_count < low:
            continue                    # 还没到区间（含涨势断掉的 0），继续等
        if require_volume and not volume_passed(row, params):
            continue                    # 形态到了但没放量，等区间内的下一根
        fired.append(index)
        armed = False
    return fired


def low9_armed_state(rows: Sequence[Mapping[str, Any]], params: SignalParams) -> List[Dict[str, Any]]:  # noqa: D401
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
        triggered = armed and params.sector_high_min <= high_count <= params.sector_high_max
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
    lookback = max(1, params.fear_lookback_days)
    for index in low_high_turn_indices(rows, params, high_min=params.sector_high_min,
                                       high_max=params.sector_high_max):
        day = dates[index]
        score = None
        if fear_scores is not None:
            raw = fear_scores.get(day)
            score = float(raw) if _finite(raw) else None
        # 回看窗口按板块自己的交易日历取，含触发日当天
        window: List[tuple] = []
        if fear_scores is not None:
            for position in range(max(0, index - lookback + 1), index + 1):
                raw = fear_scores.get(dates[position])
                if _finite(raw):
                    window.append((float(raw), dates[position]))
        passed = [item for item in window if item[0] <= params.fear_threshold]
        best = min(window) if window else None
        triggers.append({
            "index": index,
            "signal_date": day,
            "fear_score": score,
            "fear_min": best[0] if best else None,
            # 窗口内最早一次触及闸门的那天，页面上用来解释"为什么现在算过"
            "fear_pass_date": min(item[1] for item in passed) if passed else None,
            "fear_passed": bool(passed),
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
                # 排序用的分数取窗口内最恐慌的那天，触发当天可能已经反弹上去了
                "fear_score": trigger.get("fear_min", trigger.get("fear_score")),
                "fear_score_today": trigger.get("fear_score"),
                "fear_pass_date": trigger.get("fear_pass_date"),
            }
    return windows

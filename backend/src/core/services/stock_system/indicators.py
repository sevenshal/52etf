"""技术指标：与个股详情页 K 线图同一口径的 Python 实现。

K 线图上的神奇九转、ATR、MACD、成交量密集区支撑压力和放量 z 值原本只在前端算
（``frontend/src/utils/nineTurn.js`` / ``macd.js`` / ``klines.js``），后端没法拿来全市场
扫描或自动交易。这里逐行移植，**口径以前端为准**：页面上看到的信号必须和交易用的
信号是同一套算法算出来的。

一致性由 ``backend/tests/fixtures/stock_indicator_parity.json`` 守着：夹具由前端代码
生成（``frontend/scripts/generate-indicator-parity-fixture.mjs``），前端测试保证夹具与
当前 JS 输出一致，后端测试保证本模块与夹具一致。改任何一边的算法，两边测试会一起
提醒你同步另一边。

输入 K 线是 dict 列表，字段与 ``/api/stock/a-stock/klines`` 返回的一致：
``open/high/low/close/volume/turnover_rate``。输出沿用 JS 的字段名（驼峰），方便和
前端逐字段对照。
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence

DEFAULT_ATR_WINDOW = 14
DEFAULT_MACD_PARAMS = {"fast": 12, "slow": 26, "signal": 9}
# 以下默认值与 StockKlineChart.jsx 一致
CHART_SUPPORT_RESISTANCE_WINDOW = 125
CHART_SUPPORT_RESISTANCE_BIN_COUNT = 48
CHART_SUPPORT_RESISTANCE_LEVELS_PER_SIDE = 2
CHART_VOLUME_STD_MULTIPLIER = 1.0
CHART_ENABLE_TURNOVER_DECAY = True
CHART_VOLUME_LOOKBACK_DAYS = 60


def _to_number(value: Any) -> Optional[float]:
    """对应 JS 的 toNumber/toFiniteNumber：空值和非有限数都当缺失。"""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return float(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _js_or(value: Any, fallback: float) -> float:
    """对应 JS 的 ``Number(value) || fallback``：0 和 NaN 都退回 fallback。"""
    number = _to_number(value)
    return fallback if number is None or number == 0 else number


def _to_fixed(value: float, digits: int) -> float:
    """对应 JS 的 ``Number(x.toFixed(digits))``。

    toFixed 按 double 的精确十进制值四舍五入、恰好一半时远离零；Python 的 round 是
    银行家舍入，0.125 这类值两边会差一位，所以用 Decimal 复刻。
    """
    quantum = Decimal(1).scaleb(-digits)
    return float(Decimal(value).quantize(quantum, rounding=ROUND_HALF_UP))


def _price_key(value: float) -> str:
    """对应 JS 的 ``Number(price).toFixed(8)``，用作按价格去重的键。"""
    return str(Decimal(value).quantize(Decimal("1e-8"), rounding=ROUND_HALF_UP))


def _js_sum(values) -> float:
    """从左到右逐个相加，对应 JS 的 ``reduce((sum, v) => sum + v, 0)``。

    不能用内置 sum：Python 3.12 起它对浮点数做补偿求和，末位和 JS 不同，再经过
    toFixed(2) 会在 .xx5 附近舍入到不同的值。
    """
    total = 0.0
    for value in values:
        total += value
    return total


def _mean_and_std(values: Sequence[float]) -> tuple[float, float]:
    """总体均值与总体标准差（除以 n，和前端一致）。"""
    if not values:
        return 0.0, 0.0
    mean = _js_sum(values) / len(values)
    variance = _js_sum((value - mean) ** 2 for value in values) / len(values)
    return mean, math.sqrt(variance)


# ---------------------------------------------------------------------------
# 神奇九转 + ATR（nineTurn.js: appendNineTurnAtr）
# ---------------------------------------------------------------------------

def append_nine_turn_atr(klines: Sequence[Mapping[str, Any]], atr_window: int = DEFAULT_ATR_WINDOW) -> List[Dict[str, Any]]:
    """逐根追加九转计数、ATR 和"距最近红点的回撤"。

    - ``highCount``：连续多少根满足 收盘 > 4 根前收盘（高 N），断了清零；``lowCount`` 反之。
    - ``atr14``：真实波幅的简单均值（不是 Wilder 平滑）。
    - ``latestRisingClose``：最近一个高 N≥2 的红点收盘价；``risingDrawdownAtr`` 是当前
      收盘相对它回撤了几个 ATR，卖出规则用它。
    """
    rows = list(klines or [])
    high_count = 0
    low_count = 0
    latest_rising_close: Optional[float] = None
    latest_rising_count: Optional[int] = None
    true_ranges: List[float] = []
    output: List[Dict[str, Any]] = []

    for index, item in enumerate(rows):
        high = _to_number(item.get("high"))
        low = _to_number(item.get("low"))
        close = _to_number(item.get("close"))
        previous_close = _to_number(rows[index - 1].get("close")) if index > 0 else close
        close_lag4 = _to_number(rows[index - 4].get("close")) if index >= 4 else None
        components = [
            high - low if high is not None and low is not None else None,
            abs(high - previous_close) if high is not None and previous_close is not None else None,
            abs(low - previous_close) if low is not None and previous_close is not None else None,
        ]
        true_range = 0.0
        for component in components:
            if component is not None and math.isfinite(component):
                true_range = max(true_range, component)
        true_ranges.append(true_range)

        high_count = high_count + 1 if close is not None and close_lag4 is not None and close > close_lag4 else 0
        low_count = low_count + 1 if close is not None and close_lag4 is not None and close < close_lag4 else 0
        if high_count >= 2 and close is not None:
            latest_rising_close = close
            latest_rising_count = high_count

        atr = (
            _js_sum(true_ranges[index - atr_window + 1:index + 1]) / atr_window
            if index >= atr_window - 1
            else None
        )

        output.append({
            **item,
            "atr14": atr,
            "highCount": high_count,
            "lowCount": low_count,
            "latestRisingClose": latest_rising_close,
            "latestRisingCount": latest_rising_count,
            "risingDrawdownPct": (
                (latest_rising_close - close) / latest_rising_close * 100
                if latest_rising_close is not None and close is not None and latest_rising_close != 0
                else None
            ),
            "risingDrawdownAtr": (
                (latest_rising_close - close) / atr
                if latest_rising_close is not None and close is not None and atr is not None and atr > 0
                else None
            ),
        })
    return output


# ---------------------------------------------------------------------------
# MACD（macd.js: calculateMacd）
# ---------------------------------------------------------------------------

def _normalize_period(value: Any, fallback: int) -> int:
    number = _to_number(value)
    if number is None:
        return fallback
    period = math.floor(number)
    return period if period > 0 else fallback


def _exponential_moving_average(values: Sequence[Optional[float]], period: int) -> List[Optional[float]]:
    """递推 EMA，用首个有效值播种（通达信/同花顺口径）。"""
    alpha = 2 / (period + 1)
    previous: Optional[float] = None
    output: List[Optional[float]] = []
    for value in values:
        if value is None:
            output.append(None)
            continue
        previous = value if previous is None else previous + alpha * (value - previous)
        output.append(previous)
    return output


def histogram_growing(histogram: Sequence[Optional[float]]) -> List[Optional[bool]]:
    """MACD 柱在放大还是收缩；变号的那一根一律算放大。"""
    previous: Optional[float] = None
    output: List[Optional[bool]] = []
    for value in histogram or []:
        if value is None:
            output.append(None)
            continue
        sign_changed = previous is None or (value >= 0) != (previous >= 0)
        output.append(True if sign_changed else abs(value) >= abs(previous))
        previous = value
    return output


def calculate_macd(klines: Sequence[Mapping[str, Any]], params: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """DIF/DEA/MACD 柱（柱 = (DIF − DEA) × 2，A 股口径）；前 slow−1 根置空。"""
    params = params or {}
    fast = _normalize_period(params.get("fast"), DEFAULT_MACD_PARAMS["fast"])
    slow = _normalize_period(params.get("slow"), DEFAULT_MACD_PARAMS["slow"])
    signal = _normalize_period(params.get("signal"), DEFAULT_MACD_PARAMS["signal"])

    closes = [_to_number((item or {}).get("close")) for item in (klines or [])]
    fast_ema = _exponential_moving_average(closes, fast)
    slow_ema = _exponential_moving_average(closes, slow)
    raw_dif = [
        None if fast_ema[index] is None or slow_ema[index] is None else fast_ema[index] - slow_ema[index]
        for index in range(len(closes))
    ]
    raw_dea = _exponential_moving_average(raw_dif, signal)

    warm_up = max(slow, fast, signal) - 1
    dif: List[Optional[float]] = []
    dea: List[Optional[float]] = []
    histogram: List[Optional[float]] = []
    for index, value in enumerate(raw_dif):
        if index < warm_up or value is None or raw_dea[index] is None:
            dif.append(None)
            dea.append(None)
            histogram.append(None)
            continue
        dif.append(value)
        dea.append(raw_dea[index])
        histogram.append((value - raw_dea[index]) * 2)

    return {
        "dif": dif,
        "dea": dea,
        "histogram": histogram,
        "growing": histogram_growing(histogram),
        "params": {"fast": fast, "slow": slow, "signal": signal},
    }


# ---------------------------------------------------------------------------
# 成交量密集区支撑压力（klines.js: appendRollingPocSupportResistance）
# ---------------------------------------------------------------------------

def _is_valid_profile_kline(kline: Mapping[str, Any]) -> bool:
    high = _to_number(kline.get("high"))
    low = _to_number(kline.get("low"))
    close = _to_number(kline.get("close"))
    volume = _to_number(kline.get("volume"))
    return (
        high is not None and low is not None and close is not None and volume is not None
        and high > 0 and low > 0 and close > 0 and volume > 0
        and high >= low
    )


def _normalize_turnover_rate(value: Any) -> float:
    rate = _to_number(value)
    if rate is None or rate <= 0:
        return 0.0
    return min(rate, 1.0)


def _calculate_poc_window(
    window_klines: Sequence[Mapping[str, Any]],
    reference_price: Any,
    *,
    bin_count: float = CHART_SUPPORT_RESISTANCE_BIN_COUNT,
    max_levels_per_side: float = CHART_SUPPORT_RESISTANCE_LEVELS_PER_SIDE,
    volume_std_multiplier: float = CHART_VOLUME_STD_MULTIPLIER,
    enable_turnover_decay: bool = False,
    close_price_weight_multiplier: float = 10,
) -> Optional[Dict[str, Any]]:
    valid_klines = [kline for kline in window_klines if _is_valid_profile_kline(kline)]
    current_price = _to_number(reference_price)
    if not valid_klines or current_price is None or current_price <= 0:
        return None

    min_price = min(float(kline["low"]) for kline in valid_klines)
    max_price = max(float(kline["high"]) for kline in valid_klines)
    price_range = max_price - min_price
    if price_range <= 0:
        return None

    bins = max(12, min(120, _js_or(bin_count, 48)))
    levels_per_side = max(1, min(2, _js_or(max_levels_per_side, 2)))
    bin_size = price_range / bins
    close_weight_multiplier = max(1, _js_or(close_price_weight_multiplier, 1))
    bin_total = math.ceil(bins)  # Array.from({ length: bins })：长度取整

    profile = [
        {"price": min_price + (index + 0.5) * bin_size, "volume": 0.0, "touch_count": 0, "max_daily_volume": 0.0}
        for index in range(bin_total)
    ]

    def clamp_index(index: int) -> int:
        return max(0, min(len(profile) - 1, index))

    def bin_start(index: int) -> float:
        return min_price + index * bin_size

    def bin_end(index: int) -> float:
        return max_price if index == len(profile) - 1 else min_price + (index + 1) * bin_size

    def price_index(price: float) -> int:
        return clamp_index(math.floor((price - min_price) / bin_size))

    def volume_allocations(kline: Mapping[str, Any]) -> List[tuple[int, float]]:
        high = float(kline["high"])
        low = float(kline["low"])
        close = float(kline["close"])
        close_index = price_index(close or low)
        if high <= low:
            return [(close_index, 1.0)]

        overlaps: List[tuple[int, float]] = []
        for index in range(price_index(low), price_index(high) + 1):
            overlap = max(0.0, min(high, bin_end(index)) - max(low, bin_start(index)))
            if overlap > 0:
                overlaps.append((index, overlap))
        if not overlaps:
            return [(close_index, 1.0)]
        total_overlap = _js_sum(overlap for _, overlap in overlaps)
        if total_overlap <= 0:
            return [(close_index, 1.0)]

        boosted_index = close_index
        close_allocation = next((item for item in overlaps if item[0] == close_index), None)
        if close_allocation is None:
            close_allocation = overlaps[0]
            for item in overlaps:
                if abs(profile[item[0]]["price"] - close) < abs(profile[close_allocation[0]]["price"] - close):
                    close_allocation = item
            boosted_index = close_allocation[0]

        if len(overlaps) == 1 or close_weight_multiplier <= 1:
            return [(index, overlap / total_overlap) for index, overlap in overlaps]

        base_close_share = close_allocation[1] / total_overlap
        boosted_close_share = (base_close_share * close_weight_multiplier) / (
            (base_close_share * close_weight_multiplier) + (1 - base_close_share)
        )
        other_share = (1 - boosted_close_share) / (len(overlaps) - 1)
        return [
            (index, boosted_close_share if index == boosted_index else other_share)
            for index, _ in overlaps
        ]

    for kline in valid_klines:
        volume = float(kline["volume"])
        if enable_turnover_decay:
            turnover = kline.get("turnover_rate")
            if turnover is None:
                turnover = kline.get("turnoverRate")
            decay_rate = _normalize_turnover_rate(turnover)
            if decay_rate > 0:
                remaining_rate = 1 - decay_rate
                for item in profile:
                    item["volume"] *= remaining_rate
        for index, share in volume_allocations(kline):
            allocated = volume * share
            if allocated <= 0:
                continue
            profile[index]["volume"] += allocated
            profile[index]["touch_count"] += 1
            profile[index]["max_daily_volume"] = max(profile[index]["max_daily_volume"], allocated)

    profile_volumes = [item["volume"] for item in profile if item["volume"] > 0]
    profile_mean, profile_std = _mean_and_std(profile_volumes)
    profile_threshold = profile_mean + profile_std * max(0, _js_or(volume_std_multiplier, 0))

    candidates_by_price: Dict[str, Dict[str, Any]] = {}
    for item in profile:
        if item["volume"] <= 0 or item["volume"] <= profile_threshold:
            continue
        daily_equivalent = item["volume"] / max(1, item["touch_count"])
        candidates_by_price[_price_key(item["price"])] = {
            **item,
            "average_daily_volume": daily_equivalent,
            "daily_equivalent_volume": daily_equivalent,
        }
    candidates = sorted(candidates_by_price.values(), key=lambda item: item["volume"], reverse=True)
    if not candidates:
        return None

    def distance(item: Mapping[str, Any]) -> float:
        return abs(float(item["price"]) - current_price)

    def pick_strongest(items: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        best = items[0]
        for item in items:
            if item["volume"] > best["volume"]:
                best = item
            elif item["volume"] < best["volume"]:
                continue
            elif distance(item) < distance(best):
                best = item
            elif distance(item) > distance(best):
                continue
            elif item["daily_equivalent_volume"] > best["daily_equivalent_volume"]:
                best = item
        return best

    def format_level(item: Mapping[str, Any], side: str, roles: List[str]) -> Dict[str, Any]:
        price = float(item["price"])
        volume_zscore = (item["volume"] - profile_mean) / profile_std if profile_std > 0 else None
        return {
            "rank": 0,
            "side": side,
            "roles": roles,
            "price": _to_fixed(price, 2),
            "volume": _to_fixed(item["volume"], 2),
            "daily_equivalent_volume": _to_fixed(item["daily_equivalent_volume"], 2),
            "average_daily_volume": _to_fixed(item["average_daily_volume"], 2),
            "max_daily_volume": _to_fixed(item["max_daily_volume"], 2),
            "volume_threshold": _to_fixed(profile_threshold, 2),
            "volume_zscore": None if volume_zscore is None else _to_fixed(volume_zscore, 2),
            "touch_count": item["touch_count"],
            "distance_pct": _to_fixed(abs(price - current_price) / current_price * 100, 2),
        }

    def select_side_levels(items: Sequence[Dict[str, Any]], side: str) -> List[Dict[str, Any]]:
        if not items:
            return []
        strongest = pick_strongest(items)
        nearest = items[0]
        for item in items:
            if distance(item) < distance(nearest):
                nearest = item
            elif distance(item) == distance(nearest) and item["volume"] > nearest["volume"]:
                nearest = item

        selected: Dict[str, Dict[str, Any]] = {}
        for role, item in (("strongest", strongest), ("nearest", nearest)):
            key = _price_key(item["price"])
            if key not in selected:
                selected[key] = format_level(item, side, [role])
            elif role not in selected[key]["roles"]:
                selected[key]["roles"].append(role)

        levels = list(selected.values())[: int(levels_per_side)]
        levels.sort(key=lambda level: level["price"], reverse=(side == "support"))
        return [{**level, "rank": index + 1} for index, level in enumerate(levels)]

    supports = select_side_levels([item for item in candidates if float(item["price"]) < current_price], "support")
    resistances = select_side_levels([item for item in candidates if float(item["price"]) > current_price], "resistance")
    poc_level = format_level(pick_strongest(candidates), "poc", ["poc"])
    return {
        "poc": poc_level["price"],
        "poc_level": poc_level,
        "levels": [*supports, *resistances],
        "supports": supports,
        "resistances": resistances,
        "profile_volume_mean": _to_fixed(profile_mean, 2),
        "profile_volume_std": _to_fixed(profile_std, 2),
        "profile_volume_threshold": _to_fixed(profile_threshold, 2),
    }


def append_rolling_poc_support_resistance(
    klines: Sequence[Mapping[str, Any]],
    *,
    window: float = 200,
    bin_count: float = CHART_SUPPORT_RESISTANCE_BIN_COUNT,
    max_levels_per_side: float = CHART_SUPPORT_RESISTANCE_LEVELS_PER_SIDE,
    min_periods: Optional[int] = None,
    volume_std_multiplier: float = CHART_VOLUME_STD_MULTIPLIER,
    enable_turnover_decay: bool = False,
    close_price_weight_multiplier: float = 10,
    output_start_index: int = 0,
) -> List[Dict[str, Any]]:
    """每根 K 线用它**之前** window 根的成交量分布找支撑/压力（不含当根）。

    ``output_start_index`` 之前的 K 线不算（置 None）：扫描只关心最新一根时传
    ``len(klines) - 1``，省掉整段回放。
    """
    rows = list(klines or [])
    lookback = int(max(1, _js_or(window, 1)))
    required_periods = min(lookback, 20) if min_periods is None else min_periods
    start_index = max(0, min(int(_to_number(output_start_index) or 0), len(rows)))
    params = {
        "bin_count": bin_count,
        "max_levels_per_side": max_levels_per_side,
        "volume_std_multiplier": volume_std_multiplier,
        "enable_turnover_decay": enable_turnover_decay,
        "close_price_weight_multiplier": close_price_weight_multiplier,
    }

    output: List[Dict[str, Any]] = []
    for index, kline in enumerate(rows):
        support_resistance = (
            None if index < start_index else _support_resistance_at(rows, index, lookback, required_periods, params)
        )
        output.append({**kline, "support_resistance": support_resistance})
    return output


def _support_resistance_at(
    rows: Sequence[Mapping[str, Any]],
    index: int,
    lookback: int,
    required_periods: int,
    params: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    window_klines = rows[max(0, index - lookback):index]
    if len(window_klines) < required_periods:
        return None
    return _calculate_poc_window(window_klines, rows[index].get("close"), **params)


def chart_support_resistance_at(klines: Sequence[Mapping[str, Any]], index: int) -> Optional[Dict[str, Any]]:
    """K 线图默认参数下第 index 根的支撑压力，结果与 ``compute_chart_indicators`` 该根的值相同。

    回测按天只需要当天和前一天两根，逐根单算比整段滚动快得多。
    """
    return _support_resistance_at(
        klines,
        index,
        CHART_SUPPORT_RESISTANCE_WINDOW,
        CHART_SUPPORT_RESISTANCE_WINDOW,
        {
            "bin_count": CHART_SUPPORT_RESISTANCE_BIN_COUNT,
            "max_levels_per_side": CHART_SUPPORT_RESISTANCE_LEVELS_PER_SIDE,
            "volume_std_multiplier": CHART_VOLUME_STD_MULTIPLIER,
            "enable_turnover_decay": CHART_ENABLE_TURNOVER_DECAY,
            "close_price_weight_multiplier": 10,
        },
    )


# ---------------------------------------------------------------------------
# 放量 z 值（klines.js: preprocessKlinesVolume）
# ---------------------------------------------------------------------------

def _log10_volume(volume: Any) -> Optional[float]:
    number = _to_number(volume)
    return math.log10(number) if number is not None and number > 0 else None


def preprocess_klines_volume(
    klines: Sequence[Mapping[str, Any]],
    z_score_threshold: float = 1,
    days: int = CHART_VOLUME_LOOKBACK_DAYS,
) -> List[Dict[str, Any]]:
    """用过去 days 根（不含当根）的 log10 成交量算放量 z 值。"""
    rows = list(klines or [])
    lookback = int(max(1, _js_or(days, 60)))
    threshold = max(0, _js_or(z_score_threshold, 0))
    empty = {
        "volumeMA": None,
        "volumeStdDev": None,
        "volumeArithmeticMA": None,
        "logVolumeMean": None,
        "logVolumeStdDev": None,
        "volumeZScore": None,
        "volumeMultiple": None,
        "isVolumeSpike": False,
    }

    output: List[Dict[str, Any]] = []
    for index, kline in enumerate(rows):
        current_log_volume = _log10_volume(kline.get("volume"))
        if index < lookback or current_log_volume is None:
            output.append({**kline, **empty, "logVolume": current_log_volume})
            continue
        window = rows[index - lookback:index]
        window_volumes = [
            volume for volume in (_to_number(item.get("volume")) for item in window)
            if volume is not None and volume > 0
        ]
        window_log_volumes = [
            value for value in (_log10_volume(item.get("volume")) for item in window) if value is not None
        ]
        if len(window_log_volumes) < lookback:
            output.append({**kline, **empty, "logVolume": current_log_volume})
            continue

        log_mean, log_std = _mean_and_std(window_log_volumes)
        volume_z_score = (current_log_volume - log_mean) / log_std if log_std > 0 else None
        output.append({
            **kline,
            "volumeMA": math.pow(10, log_mean),
            "volumeStdDev": log_std,
            "volumeArithmeticMA": _js_sum(window_volumes) / len(window_volumes),
            "logVolume": current_log_volume,
            "logVolumeMean": log_mean,
            "logVolumeStdDev": log_std,
            "volumeZScore": volume_z_score,
            "volumeMultiple": math.pow(10, current_log_volume - log_mean),
            "isVolumeSpike": volume_z_score is not None and volume_z_score > threshold,
        })
    return output


# ---------------------------------------------------------------------------
# K 线图的完整流水线（StockKlineChart.jsx）
# ---------------------------------------------------------------------------

def compute_chart_indicators(
    klines: Sequence[Mapping[str, Any]],
    *,
    support_resistance_window: int = CHART_SUPPORT_RESISTANCE_WINDOW,
    volume_std_multiplier: float = CHART_VOLUME_STD_MULTIPLIER,
    enable_turnover_decay: bool = CHART_ENABLE_TURNOVER_DECAY,
    macd_params: Optional[Mapping[str, Any]] = None,
    output_start_index: int = 0,
) -> Dict[str, Any]:
    """与 K 线图完全相同的处理顺序：支撑压力 → 放量 z 值 → 九转/ATR，MACD 单独算。

    默认参数就是页面打开时的默认值；页面上调过的参数同名传进来即可对齐。
    """
    enriched = append_rolling_poc_support_resistance(
        klines,
        window=support_resistance_window,
        bin_count=CHART_SUPPORT_RESISTANCE_BIN_COUNT,
        max_levels_per_side=CHART_SUPPORT_RESISTANCE_LEVELS_PER_SIDE,
        min_periods=support_resistance_window,
        volume_std_multiplier=volume_std_multiplier,
        enable_turnover_decay=enable_turnover_decay,
        output_start_index=output_start_index,
    )
    processed = append_nine_turn_atr(
        preprocess_klines_volume(enriched, volume_std_multiplier, CHART_VOLUME_LOOKBACK_DAYS)
    )
    return {"klines": processed, "macd": calculate_macd(klines, macd_params)}

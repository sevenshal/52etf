from __future__ import annotations

import math
from statistics import fmean
from typing import Iterable, Optional, Tuple


def calculate_volume_ratio(
    current_volume: Optional[float],
    previous_volumes: Iterable[Optional[float]],
    lookback: int = 20,
) -> Tuple[Optional[float], Optional[float]]:
    """Calculate current volume / prior-volume mean, excluding current day."""
    values = list(previous_volumes)[:lookback]
    if current_volume is None or len(values) < lookback:
        return None, None
    try:
        current = float(current_volume)
        history = [float(value) for value in values if value is not None]
    except (TypeError, ValueError):
        return None, None
    if (
        len(history) < lookback
        or not math.isfinite(current)
        or any(not math.isfinite(value) for value in history)
    ):
        return None, None
    average = fmean(history)
    if average <= 0:
        return None, None
    return current / average, average


def calculate_log_volume_z_score(
    current_volume: Optional[float],
    previous_volumes: Iterable[Optional[float]],
    lookback: int = 21,
) -> Optional[float]:
    """log-volume z-score of the current day vs the prior ``lookback`` trading days.

    z = (log(volume[T]) - mean(log(volume[T-lookback..T-1]))) / std(log(prior)).
    Positive means 放量 (volume expansion), negative means 缩量. Returns None when
    the current volume or the full prior window is unavailable.
    """
    values = list(previous_volumes)[:lookback]
    if current_volume is None:
        return None
    try:
        current = float(current_volume)
        history = [float(value) for value in values if value is not None]
    except (TypeError, ValueError):
        return None
    if (
        len(history) < lookback
        or current <= 0
        or any(value <= 0 for value in history)
        or not math.isfinite(current)
        or any(not math.isfinite(value) for value in history)
    ):
        return None
    log_values = [math.log(value) for value in history]
    log_current = math.log(current)
    mean = fmean(log_values)
    if len(log_values) > 1:
        variance = sum((value - mean) ** 2 for value in log_values) / (len(log_values) - 1)
        std = math.sqrt(variance) if variance > 0 else 0.0
    else:
        std = 0.0
    if std <= 0:
        return 0.0
    return (log_current - mean) / std

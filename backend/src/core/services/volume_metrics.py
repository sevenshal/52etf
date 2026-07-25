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

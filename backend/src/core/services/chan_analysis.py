"""Adapter between 52etf market data and the native Chan structure engine.

API responses only expose plain dictionaries so a future engine change cannot
leak implementation details through the public API.  The CZSC comparison
adapter now lives in ``lab/czsc_oracle.py`` and is not imported by production
code; the only remaining CZSC use in the backend is ``BarGenerator`` for
1m→5m/30m resampling in ``chan_minute_data`` (not Chan structure logic).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable
import sys
from pathlib import Path

from czsc import Freq, RawBar


NATIVE_ENGINE_VERSION = "2.0"

# The production copy lives inside backend/src so deployment artifacts are
# self-contained.  Keep a lab fallback for local research checkouts that use
# the un-packaged adapter directly.
try:
    from .chan_native import Kline as NativeKline
    from .chan_native import build_segments as native_build_segments
    from .chan_native import calculate as native_calculate
    from .chan_native import detect_buy_sell as native_detect_buy_sell
except ImportError:  # pragma: no cover - only for legacy research layouts
    _NATIVE_DIR = Path(__file__).resolve().parents[4] / "lab"
    if str(_NATIVE_DIR) not in sys.path:
        sys.path.insert(0, str(_NATIVE_DIR))
    from chan_native import Kline as NativeKline  # noqa: E402
    from chan_native import build_segments as native_build_segments  # noqa: E402
    from chan_native import calculate as native_calculate  # noqa: E402
    from chan_native import detect_buy_sell as native_detect_buy_sell  # noqa: E402

FREQ_MAP = {
    "1m": Freq.F1,
    "5m": Freq.F5,
    "30m": Freq.F30,
    "d": Freq.D,
}


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result


def rows_to_raw_bars(symbol: str, rows: Iterable[dict[str, Any]], freq: str) -> list[RawBar]:
    """Validate and convert normalized OHLCV rows to CZSC RawBar objects.

    Only used by ``chan_minute_data`` for bar resampling; the Chan structure
    engine works directly on the normalized dict rows.
    """
    czsc_freq = FREQ_MAP.get(freq)
    if czsc_freq is None:
        raise ValueError(f"Unsupported frequency: {freq}")

    bars: list[RawBar] = []
    for index, row in enumerate(rows):
        dt = row.get("timestamp") or row.get("dt") or row.get("trade_time")
        if isinstance(dt, str):
            dt = datetime.fromisoformat(dt)
        if not isinstance(dt, datetime):
            raise ValueError(f"Invalid bar datetime at index {index}")
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)

        values = {key: _number(row.get(key)) for key in ("open", "close", "high", "low")}
        if any(value is None for value in values.values()):
            raise ValueError(f"Invalid OHLC value at index {index}")
        if values["low"] > min(values["open"], values["close"]) or values["high"] < max(
            values["open"], values["close"]
        ):
            raise ValueError(f"Inconsistent OHLC value at index {index}")

        bars.append(
            RawBar(
                symbol=symbol,
                dt=dt,
                freq=czsc_freq,
                open=values["open"],
                close=values["close"],
                high=values["high"],
                low=values["low"],
                vol=max(0.0, _number(row.get("volume", row.get("vol"))) or 0.0),
                amount=max(0.0, _number(row.get("turnover", row.get("amount"))) or 0.0),
                id=index,
            )
        )
    return bars


def _native_analysis(symbol: str, rows: list[dict[str, Any]], freq: str, *, confirmed: bool, include_history: bool) -> dict[str, Any]:
    """Serialize the strict left-to-right structural engine for the API."""
    native_bars = [
        NativeKline(
            i=i,
            high=float(row["high"]),
            low=float(row["low"]),
            dt=row.get("timestamp"),
            close=_number(row.get("close")),
        )
        for i, row in enumerate(rows)
    ]
    normalized, fractals, strokes, centers = native_calculate(native_bars, min_gap=4)
    segments = native_build_segments(strokes)
    by_id = {bar.i: bar for bar in normalized}

    def dt_for(index: int) -> str | None:
        bar = by_id.get(index)
        return _iso(bar.dt) if bar else None

    serialized_fractals = [
        {"dt": dt_for(item.i), "mark": item.mark, "price": item.price,
         "confirm_dt": dt_for(item.confirm_i), "confirmed": bool(item.confirm_i <= normalized[-1].i)}
        for item in fractals
    ]
    serialized_strokes = [
        {"start": dt_for(item.start.i), "end": dt_for(item.end.i), "direction": "向上" if item.direction == "up" else "向下",
         "high": item.high, "low": item.low, "power_price": abs(item.end.price - item.start.price),
         "power_volume": None, "length": max(1, item.end.i - item.start.i), "finished": True}
        for item in strokes
    ]
    serialized_segments = []
    for item in segments:
        start_stroke, end_stroke = strokes[item.start_stroke], strokes[item.end_stroke]
        serialized_segments.append({
            "start": dt_for(start_stroke.start.i), "end": dt_for(end_stroke.end.i),
            "direction": "向上" if item.direction == "up" else "向下", "high": item.high, "low": item.low,
            "start_price": item.start_price, "end_price": item.end_price,
            "stroke_count": item.end_stroke - item.start_stroke + 1, "power_price": item.power_price,
            "power_volume": None, "partition": item.partition, "confirm_dt": dt_for(item.confirm_i),
        })
    serialized_centers = []
    for item in centers:
        first = segments[item.start_stroke] if item.start_stroke < len(segments) else None
        last = segments[item.end_stroke] if item.end_stroke < len(segments) else None
        serialized_centers.append({
            "start": dt_for(strokes[first.start_stroke].start.i) if first else None,
            "end": dt_for(strokes[last.end_stroke].end.i) if last else None,
            "zg": item.zg, "zd": item.zd, "gg": item.gg, "dd": item.dd,
            "center": (item.zg + item.zd) / 2, "valid": item.status == "active",
            "status": item.status, "kind": item.kind, "growth": item.growth,
            "level": item.level, "trend": item.trend,
            "confirm_dt": dt_for(item.confirm_i), "break_dt": dt_for(item.broken_at) if item.broken_at is not None else None,
        })
    events = native_detect_buy_sell(strokes, segments, centers, bars=normalized)

    def serialize_event(event: Any) -> dict[str, Any]:
        return {"name": "native_chan", "key": f"native_{freq}_{event.kind}", "value": event.kind,
                "type": event.kind, "detail": event.detail, "score": 0,
                "confirmed": bool(confirmed), "bar_time": dt_for(event.confirm_i),
                "center_start_stroke": event.center_start_stroke, "center_end_stroke": event.center_end_stroke,
                "break_stroke": event.break_stroke, "segment_start_stroke": event.segment_start_stroke,
                "segment_end_stroke": event.segment_end_stroke}

    history = [serialize_event(event) for event in events] if include_history else []
    latest_id = normalized[-1].i if normalized else -1
    current = [serialize_event(event) for event in events if event.confirm_i == latest_id]
    return {"symbol": symbol, "freq": freq, "engine_version": NATIVE_ENGINE_VERSION, "engine": "native_structural",
            "bar_count": len(native_bars), "latest_bar_time": _iso(native_bars[-1].dt) if native_bars else None,
            "fractals": serialized_fractals, "strokes": serialized_strokes, "segments": serialized_segments,
            "centers": serialized_centers, "signals": current, "signal_history": history,
            "signal_history_count": len(history), "signal_replay_mode": "native_confirmed_structure"}


def analyze_bars(
    symbol: str,
    rows: Iterable[dict[str, Any]],
    freq: str = "d",
    *,
    confirmed: bool = True,
    include_history: bool = True,
) -> dict[str, Any]:
    """Return chart-ready structures from the strict native Chan engine."""
    normalized_rows = [dict(row) for row in rows]
    if len(normalized_rows) < 20:
        raise ValueError("At least 20 bars are required for Chan analysis")
    return _native_analysis(symbol, normalized_rows, freq, confirmed=confirmed, include_history=include_history)

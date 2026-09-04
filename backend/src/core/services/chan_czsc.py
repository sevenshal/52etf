"""CZSC 1.0.1 adapter — the selectable "open-source engine" for Chan analysis.

Output is shaped like ``chan_analysis._native_analysis`` so the same chart /
scanner / paper-trading code can consume either engine.  ``czsc`` (a compiled
wheel) is imported lazily so the backend only loads it when the CZSC engine is
actually chosen.
"""

from __future__ import annotations

from typing import Any, Iterable

from .chan_analysis import _iso, _number, rows_to_raw_bars

CZSC_ENGINE_VERSION = "1.0.1"

# Official CZSC signal functions; ``expected`` filters the neutral "其他" output.
_SIGNAL_SPECS = (
    ("cxt_first_buy_V221126", {"di": 1}, {"一买"}),
    ("cxt_second_bs_V240524", {"di": 1, "w": 15, "t": 2}, {"二买", "二卖"}),
    ("cxt_third_buy_V230228", {"di": 1}, {"三买"}),
    ("cxt_first_sell_V221126", {"di": 1}, {"一卖"}),
    ("cxt_third_bs_V230318", {"di": 1, "ma_type": "SMA", "timeperiod": 34}, {"三买", "三卖"}),
)


def _mark(value: Any) -> str:
    text = str(value)
    return "top" if ("G" in text or "顶" in text) else "bottom"


def _direction(value: Any) -> str:
    text = str(value).lower()
    return "向下" if ("down" in text or "向下" in text) else "向上"


def _serialize_bi(bi: Any) -> dict[str, Any]:
    return {
        "start": _iso(bi.sdt), "end": _iso(bi.edt), "direction": _direction(bi.direction),
        "high": _number(bi.high), "low": _number(bi.low),
        "power_price": _number(bi.power_price), "power_volume": _number(bi.power_volume),
        "length": int(bi.length), "finished": True,
    }


def _directional_segments(bis: list[Any]) -> list[dict[str, Any]]:
    """Rebuild directional segments from finished strokes (CZSC 1.0.1 has no xd_list)."""
    if not bis:
        return []

    def is_down(bi: Any) -> bool:
        return "down" in str(getattr(bi, "direction", "")).lower() or "向下" in str(getattr(bi, "direction", ""))

    def serialize(items: list[Any], down: bool) -> dict[str, Any]:
        return {
            "start": _iso(items[0].sdt), "end": _iso(items[-1].edt),
            "direction": "向下" if down else "向上",
            "high": max(float(x.high) for x in items), "low": min(float(x.low) for x in items),
            "start_price": float(items[0].high if down else items[0].low),
            "end_price": float(items[-1].low if down else items[-1].high),
            "stroke_count": len(items),
            "power_price": sum(abs(float(getattr(x, "power_price", 0) or 0)) for x in items),
            "power_volume": sum(abs(float(getattr(x, "power_volume", 0) or 0)) for x in items),
            "partition": None, "confirm_dt": _iso(items[-1].edt),
        }

    segments: list[dict[str, Any]] = []
    current = [bis[0]]
    down = is_down(bis[0])
    extreme = float(getattr(bis[0], "low" if down else "high"))
    for bi in bis[1:]:
        if is_down(bi) != down:
            current.append(bi)
            continue
        value = float(getattr(bi, "low" if down else "high"))
        extends = value <= extreme if down else value >= extreme
        if extends:
            current.append(bi)
            extreme = value
            continue
        if len(current) >= 2:
            segments.append(serialize(current, down))
        current = [bi]
        down = is_down(bi)
        extreme = value
    if len(current) >= 2:
        segments.append(serialize(current, down))
    return segments


def _serialize_zs(zs: Any) -> dict[str, Any]:
    return {
        "start": _iso(zs.sdt), "end": _iso(zs.edt),
        "zg": _number(zs.zg), "zd": _number(zs.zd), "gg": _number(zs.gg), "dd": _number(zs.dd),
        "center": _number(zs.zz), "valid": bool(zs.is_valid),
        "status": "active" if zs.is_valid else "broken",
        "kind": "unknown", "growth": None, "level": 0, "trend": "",
        "confirm_dt": _iso(zs.edt), "break_dt": None,
    }


def _recognized_signals(c: Any, confirmed: bool) -> list[dict[str, Any]]:
    from czsc._native import signals as native_signals  # lazy: compiled wheel

    out: list[dict[str, Any]] = []
    for name, params, expected in _SIGNAL_SPECS:
        namespace = getattr(native_signals, name.split("_", 1)[0])
        for signal in namespace.call_signal(name, c, params):
            if signal.v1 not in expected:
                continue
            out.append({
                "name": name, "key": signal.key, "value": signal.value, "type": signal.v1,
                "detail": signal.v2, "score": int(signal.score), "confirmed": confirmed,
                "bar_time": _iso(c.bars_raw[-1].dt) if c.bars_raw else None,
                "center_start_stroke": None, "center_end_stroke": None, "break_stroke": None,
                "segment_start_stroke": None, "segment_end_stroke": None,
            })
    return out


def _replay_history(c: Any, raw_bars: list[Any], confirmed: bool) -> list[dict[str, Any]]:
    """One event per neutral→active transition of each configured signal."""
    from czsc import Signal, generate_czsc_signals  # lazy
    from czsc._native import signals as native_signals

    descriptors: list[dict[str, Any]] = []
    for name, params, expected in _SIGNAL_SPECS:
        namespace = getattr(native_signals, name.split("_", 1)[0])
        got = namespace.call_signal(name, c, params)
        if got:
            descriptors.append({"name": name, "key": got[0].key, "expected": expected,
                                "config": {"name": name, "freq": str(c.freq), **params}})
    if not descriptors or len(raw_bars) < 2:
        return []
    rows = generate_czsc_signals(
        raw_bars, [item["config"] for item in descriptors],
        sdt=raw_bars[0].dt.strftime("%Y%m%d"), init_n=1, df=False,
    )
    bars_by_id = {int(bar.id): bar for bar in raw_bars}
    latest_id = int(raw_bars[-1].id)
    previous: dict[str, str | None] = {item["name"]: None for item in descriptors}
    events: list[dict[str, Any]] = []
    for row in rows:
        try:
            bar_id = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        bar = bars_by_id.get(bar_id)
        if bar is None:
            continue
        for descriptor in descriptors:
            value = row.get(descriptor["key"])
            current_type: str | None = None
            signal: Any = None
            if value:
                try:
                    signal = Signal(key=descriptor["key"], value=str(value))
                except (TypeError, ValueError):
                    signal = None
                if signal is not None and signal.v1 in descriptor["expected"]:
                    current_type = signal.v1
            if current_type is not None and current_type != previous[descriptor["name"]] and signal is not None:
                events.append({
                    "name": descriptor["name"], "key": signal.key, "value": signal.value,
                    "type": signal.v1, "detail": signal.v2, "score": int(signal.score),
                    "confirmed": confirmed if bar_id == latest_id else True, "bar_time": _iso(bar.dt),
                    "center_start_stroke": None, "center_end_stroke": None, "break_stroke": None,
                    "segment_start_stroke": None, "segment_end_stroke": None,
                })
            previous[descriptor["name"]] = current_type
    return events


def analyze_bars_czsc(
    symbol: str,
    rows: Iterable[dict[str, Any]],
    freq: str = "d",
    *,
    confirmed: bool = True,
    include_history: bool = True,
) -> dict[str, Any]:
    """CZSC-engine analysis in the same dict shape as the native engine."""
    from czsc import CZSC  # lazy: compiled wheel

    raw_bars = rows_to_raw_bars(symbol, rows, freq)
    if len(raw_bars) < 20:
        raise ValueError("At least 20 bars are required for Chan analysis")
    c = CZSC(raw_bars)
    current = _recognized_signals(c, confirmed)
    history = _replay_history(c, raw_bars, confirmed) if include_history else []
    return {
        "symbol": symbol, "freq": freq, "engine": "czsc", "engine_version": CZSC_ENGINE_VERSION,
        "bar_count": len(raw_bars), "latest_bar_time": _iso(raw_bars[-1].dt),
        "fractals": [
            {"dt": _iso(fx.dt), "mark": _mark(fx.mark), "price": _number(fx.fx),
             "confirm_dt": _iso(fx.dt), "confirmed": True}
            for fx in c.fx_list
        ],
        "strokes": [_serialize_bi(bi) for bi in c.bi_list],
        "segments": _directional_segments(c.bi_list),
        "centers": [_serialize_zs(zs) for zs in c.zs_list],
        "signals": current,
        "signal_history": history,
        "signal_history_count": len(history),
        "signal_replay_mode": "czsc_bar_close_activation",
    }

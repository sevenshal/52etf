"""Stable adapter between 52etf market data and CZSC.

Keep all upstream CZSC objects inside this module. API responses only expose
plain dictionaries so a future CZSC upgrade cannot leak implementation details
through the public API.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from czsc import CZSC, Freq, RawBar
from czsc._native import signals as native_signals


CZSC_VERSION = "1.0.1"

FREQ_MAP = {
    "1m": Freq.F1,
    "5m": Freq.F5,
    "30m": Freq.F30,
    "d": Freq.D,
}

# These are official CZSC signals. ``expected`` prevents the neutral "其他"
# output from being presented as an actionable Chan-theory marker.
DEFAULT_SIGNAL_SPECS = (
    ("cxt_first_buy_V221126", {"di": 1}, {"一买"}),
    ("cxt_second_bs_V240524", {"di": 1, "w": 15, "t": 2}, {"二买", "二卖"}),
    ("cxt_third_buy_V230228", {"di": 1}, {"三买"}),
    ("cxt_first_sell_V221126", {"di": 1}, {"一卖"}),
    ("cxt_third_bs_V230318", {"di": 1, "ma_type": "SMA", "timeperiod": 34}, {"三买", "三卖"}),
)


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
    """Validate and convert normalized OHLCV rows to CZSC RawBar objects."""
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


def _serialize_fx(fx: Any) -> dict[str, Any]:
    return {
        "dt": _iso(fx.dt),
        "mark": str(fx.mark),
        "price": _number(fx.fx),
        "high": _number(fx.high),
        "low": _number(fx.low),
        "power": fx.power_str,
        "has_zs": bool(fx.has_zs),
    }


def _serialize_bi(bi: Any) -> dict[str, Any]:
    return {
        "start": _iso(bi.sdt),
        "end": _iso(bi.edt),
        "direction": str(bi.direction),
        "high": _number(bi.high),
        "low": _number(bi.low),
        "power_price": _number(bi.power_price),
        "power_volume": _number(bi.power_volume),
        "length": int(bi.length),
        "finished": True,
    }


def _serialize_zs(zs: Any) -> dict[str, Any]:
    return {
        "start": _iso(zs.sdt),
        "end": _iso(zs.edt),
        "zg": _number(zs.zg),
        "zd": _number(zs.zd),
        "gg": _number(zs.gg),
        "dd": _number(zs.dd),
        "center": _number(zs.zz),
        "valid": bool(zs.is_valid),
    }


def _recognized_signals(c: CZSC, confirmed: bool) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for name, params, expected in DEFAULT_SIGNAL_SPECS:
        category = name.split("_", 1)[0]
        namespace = getattr(native_signals, category)
        for signal in namespace.call_signal(name, c, params):
            if signal.v1 not in expected:
                continue
            result.append(
                {
                    "name": name,
                    "key": signal.key,
                    "value": signal.value,
                    "type": signal.v1,
                    "detail": signal.v2,
                    "score": int(signal.score),
                    "confirmed": confirmed,
                    "bar_time": _iso(c.bars_raw[-1].dt) if c.bars_raw else None,
                }
            )
    return result


def analyze_bars(
    symbol: str,
    rows: Iterable[dict[str, Any]],
    freq: str = "d",
    *,
    confirmed: bool = True,
) -> dict[str, Any]:
    """Return chart-ready Chan structures and official CZSC signals."""
    raw_bars = rows_to_raw_bars(symbol, rows, freq)
    if len(raw_bars) < 20:
        raise ValueError("At least 20 bars are required for Chan analysis")
    c = CZSC(raw_bars)
    return {
        "symbol": symbol,
        "freq": freq,
        "czsc_version": CZSC_VERSION,
        "bar_count": len(raw_bars),
        "latest_bar_time": _iso(raw_bars[-1].dt),
        "fractals": [_serialize_fx(item) for item in c.fx_list],
        "strokes": [_serialize_bi(item) for item in c.bi_list],
        "centers": [_serialize_zs(item) for item in c.zs_list],
        "signals": _recognized_signals(c, confirmed),
    }

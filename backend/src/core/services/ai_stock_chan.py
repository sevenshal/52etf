"""Chan 1-minute 一买 / 一卖 confirmation for the AI-stock paper portfolio.

Entry and AI-advised exit both defer to the open-source CZSC engine
(``analyze_bars_czsc_legacy``): rolling 1-minute bars are read from DuckDB and
merged with the current day's realtime minute bars (the same data path
``chan_scanner`` uses).  Any missing feed, short history or engine error
resolves to "not confirmed" so an outage never forces a trade.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

MIN_BARS = 40  # CZSC needs >= 20; keep a margin so early-session noise is ignored
HISTORY_CALENDAR_DAYS = 12


def _sane_ohlc(row: Dict[str, Any]) -> bool:
    try:
        o = float(row["open"]); h = float(row["high"]); low = float(row["low"]); c = float(row["close"])
    except (KeyError, TypeError, ValueError):
        return False
    return low <= min(o, c) and h >= max(o, c) and low <= h and low > 0


def _load_1m_rows(ts_code: str, now: datetime) -> List[Dict[str, Any]]:
    from .a_stock_consensus import normalize_a_stock_symbol
    from .chan_minute_data import (
        fetch_realtime_minute_rows,
        load_minute_rows,
        merge_minute_rows,
    )

    normalized = normalize_a_stock_symbol(ts_code)
    start = datetime.combine(now.date() - timedelta(days=HISTORY_CALENDAR_DAYS), time.min)
    end = datetime.combine(now.date(), time.max)
    rows = [row for row in load_minute_rows(normalized, start, end) if _sane_ohlc(row)]
    try:
        realtime = fetch_realtime_minute_rows([normalized], "1MIN").get(normalized) or []
    except Exception as exc:  # noqa: BLE001 - realtime feed is best-effort
        logger.warning("AI stock chan realtime 1m fetch failed for %s: %s", normalized, exc)
        realtime = []
    realtime = [row for row in realtime if _sane_ohlc(row)]
    if realtime:
        rows = merge_minute_rows(rows, realtime)
    return rows


def _detect(ts_code: str, now: datetime, wanted: str) -> Tuple[bool, Dict[str, Any]]:
    try:
        rows = _load_1m_rows(ts_code, now)
        if len(rows) < MIN_BARS:
            return False, {"reason": f"1分钟K线不足({len(rows)})"}
        from .chan_analysis import analyze_bars_czsc_legacy

        analysis = analyze_bars_czsc_legacy(
            ts_code, rows, "1m", confirmed=False, include_history=False
        )
        hits = [
            signal
            for signal in analysis.get("signals") or []
            if signal.get("type") == wanted
        ]
        if hits:
            return True, {
                "signal": wanted,
                "bar_time": hits[0].get("bar_time"),
                "detail": hits[0].get("detail"),
                "bar_count": len(rows),
            }
        return False, {"reason": f"未出现{wanted}", "bar_count": len(rows)}
    except Exception as exc:  # noqa: BLE001 - never let a chan error force/block a trade
        logger.warning("AI stock chan %s detection failed for %s: %s", wanted, ts_code, exc)
        return False, {"reason": f"缠论{wanted}判定失败: {exc}"}


def first_buy_confirmed(ts_code: str, now: datetime) -> Tuple[bool, Dict[str, Any]]:
    """Return whether a CZSC 一买 signal is active on the latest 1-minute bar."""
    return _detect(ts_code, now, "一买")


def first_sell_confirmed(ts_code: str, now: datetime) -> Tuple[bool, Dict[str, Any]]:
    """Return whether a CZSC 一卖 signal is active on the latest 1-minute bar."""
    return _detect(ts_code, now, "一卖")

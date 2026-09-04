"""Chan 一/二/三类 buy/sell confirmation for the AI-stock paper portfolio.

Entry and AI-advised exit defer to the native structure engine
(``chan_analysis.analyze_bars``) — the same engine that drives the Chan
analysis chart and the background scanner, so the paper portfolio never
trades a signal the chart would not show.

A buy is confirmed when **any** of 一买 / 二买 / 三买 is active on the latest
completed 1-minute bar, or — only at a minute that closes a 5-minute A-share
bar — on the latest 5-minute bar.  A sell is the symmetric 一卖 / 二卖 / 三卖
check.  Rolling 1-minute bars are read from DuckDB and merged with the
current day's realtime minute bars (the same data path ``chan_scanner``
uses); 5-minute bars are aggregated from those.  Any missing feed, short
history or engine error resolves to "not confirmed" so an outage never
forces a trade.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Any, Dict, List, Sequence, Tuple

logger = logging.getLogger(__name__)

MIN_BARS_1M = 40  # native engine needs >= 20; keep a margin so early-session noise is ignored
MIN_BARS_5M = 20
HISTORY_CALENDAR_DAYS = 12
BUY_SIGNALS = ("一买", "二买", "三买")
SELL_SIGNALS = ("一卖", "二卖", "三卖")


def _sane_ohlc(row: Dict[str, Any]) -> bool:
    try:
        o = float(row["open"]); h = float(row["high"]); low = float(row["low"]); c = float(row["close"])
    except (KeyError, TypeError, ValueError):
        return False
    return low <= min(o, c) and h >= max(o, c) and low <= h and low > 0


def _on_5m_close(now: datetime) -> bool:
    """True when ``now`` is the minute that closes an A-share 5-minute bar.

    Morning 5m bars close at 9:35, 9:40, … 11:30; afternoon at 13:05 … 15:00.
    """
    if now.minute % 5 != 0:
        return False
    t = now.time()
    return time(9, 35) <= t <= time(11, 30) or time(13, 5) <= t <= time(15, 0)


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


def _signals_on(ts_code: str, rows: List[Dict[str, Any]], freq: str, wanted: Sequence[str], engine: str) -> Dict[str, Any] | None:
    from .chan_analysis import analyze_bars

    analysis = analyze_bars(ts_code, rows, freq, confirmed=False, include_history=False, engine=engine)
    hits = [s for s in (analysis.get("signals") or []) if s.get("type") in wanted]
    if not hits:
        return None
    return {
        "freq": freq,
        "engine": engine,
        "signal": hits[0].get("type"),
        "bar_time": hits[0].get("bar_time"),
        "detail": hits[0].get("detail"),
        "bar_count": len(rows),
        "matched": sorted({s.get("type") for s in hits}),
    }


def _detect(ts_code: str, now: datetime, wanted: Sequence[str], engine: str = "native") -> Tuple[bool, Dict[str, Any]]:
    label = "/".join(wanted)
    try:
        rows_1m = _load_1m_rows(ts_code, now)
    except Exception as exc:  # noqa: BLE001 - never let a data error force/block a trade
        logger.warning("AI stock chan 1m load failed for %s: %s", ts_code, exc)
        return False, {"reason": f"1分钟K线加载失败: {exc}"}

    checks: List[Tuple[str, List[Dict[str, Any]]]] = []
    if len(rows_1m) >= MIN_BARS_1M:
        checks.append(("1m", rows_1m))
    if _on_5m_close(now) and len(rows_1m) >= MIN_BARS_1M:
        try:
            from .chan_minute_data import aggregate_minute_rows

            rows_5m = aggregate_minute_rows(ts_code, rows_1m, "5m")
            if len(rows_5m) >= MIN_BARS_5M:
                checks.append(("5m", rows_5m))
        except Exception as exc:  # noqa: BLE001 - 5m is best-effort on top of 1m
            logger.warning("AI stock chan 5m aggregate failed for %s: %s", ts_code, exc)

    if not checks:
        return False, {"reason": f"K线不足(1m={len(rows_1m)})"}

    for freq, rows in checks:
        try:
            hit = _signals_on(ts_code, rows, freq, wanted, engine)
        except Exception as exc:  # noqa: BLE001 - never let a chan error force/block a trade
            logger.warning("AI stock chan %s %s (%s) detection failed for %s: %s", freq, label, engine, ts_code, exc)
            continue
        if hit:
            return True, hit
    return False, {"reason": f"未出现{label}", "checked": [f for f, _ in checks]}


def _pick_types(types: Sequence[str] | None, allowed: Sequence[str]) -> Tuple[str, ...]:
    """Keep only recognised signal types; fall back to ``allowed`` when empty."""
    if types:
        chosen = tuple(t for t in allowed if t in set(types))
        if chosen:
            return chosen
    return tuple(allowed)


def buy_confirmed(ts_code: str, now: datetime, types: Sequence[str] | None = None, engine: str = "native") -> Tuple[bool, Dict[str, Any]]:
    """Whether a configured 买点 (default 一/二/三买) is active on the latest 1m (or 5m-close) bar."""
    return _detect(ts_code, now, _pick_types(types, BUY_SIGNALS), engine)


def sell_confirmed(ts_code: str, now: datetime, types: Sequence[str] | None = None, engine: str = "native") -> Tuple[bool, Dict[str, Any]]:
    """Whether a configured 卖点 (default 一/二/三卖) is active on the latest 1m (or 5m-close) bar."""
    return _detect(ts_code, now, _pick_types(types, SELL_SIGNALS), engine)

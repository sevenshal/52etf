#!/usr/bin/env python3
"""Backtest index fear/greed turn signals with constituent nine-turn entries/exits.

The index turn signals mirror the production historical-curve implementation:

* MA5 bottom/top reversals with an extreme-score lookback;
* volume bottom/top signals using proxy-ETF (or fallback index) log-volume z;
* independent cooldowns for the four signal kinds.

Each index/constituent pair is simulated independently.  A bottom signal arms
the constituents present in the point-in-time weight snapshot.  A constituent
buys on high-count == 2.  A top signal switches the pair into sell mode; an
open position exits on low-count in {2, 3, 4} when its latest red marker was at
least high 7 and the close has fallen more than two ATR14 from that marker.
Signals are decided at the close and executed at the next available open.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

import duckdb
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.src.robot.a_stock_base_data_config import (  # noqa: E402
    A_STOCK_INDEX_FEAR_GREED_TARGETS,
)


DEFAULT_ANALYTICS_DB = "/home/quantd/quant_prod/quant_robot/analytics.duckdb"
DEFAULT_SQLITE_DB = "/home/quantd/quant_prod/quant_robot/evc_stocks.db"
DEFAULT_START = date(2023, 1, 1)
DEFAULT_OUTPUT = "research/output/a_stock_fear_nine_turn_component_backtest"
SIGNAL_MODES = ("ma5", "volume", "any")
SELL_LOW_COUNTS = (2, 3, 4)
MIN_ANCHOR_HIGH_COUNT = 7
SELL_ATR_THRESHOLD = 2.0
COMMISSION_RATE = 0.0003
STAMP_DUTY_RATE = 0.0005
AGGREGATE_VOLUME_ETFS = {
    "000985.SH": ["510300.SH", "510500.SH", "512100.SH", "563300.SH"],
}


@dataclass(frozen=True)
class SignalConfig:
    ma5_bottom_score: float
    ma5_top_score: float
    ma5_lookback_days: int
    volume_bottom_score: float
    volume_top_score: float
    volume_expand_z: float
    volume_shrink_z: float
    cooldown_days: int
    updated_at: str | None


def _finite(value: Any) -> bool:
    try:
        return value is not None and bool(np.isfinite(float(value)))
    except (TypeError, ValueError):
        return False


def _load_signal_config(connection: sqlite3.Connection) -> SignalConfig:
    row = connection.execute(
        """
        SELECT ma5_bottom_score, ma5_top_score, ma5_lookback_days,
               volume_bottom_score, volume_top_score, volume_expand_std,
               volume_shrink_std, cooldown_days, updated_at
        FROM fear_greed_signal_configs ORDER BY id LIMIT 1
        """
    ).fetchone()
    if row is None:
        return SignalConfig(25.0, 75.0, 5, 30.0, 75.0, 1.25, -0.25, 5, None)
    return SignalConfig(
        ma5_bottom_score=float(row[0]),
        ma5_top_score=float(row[1]),
        ma5_lookback_days=int(row[2]),
        volume_bottom_score=float(row[3]),
        volume_top_score=float(row[4]),
        volume_expand_z=float(row[5]),
        volume_shrink_z=-abs(float(row[6])),
        cooldown_days=int(row[7]),
        updated_at=str(row[8]) if row[8] is not None else None,
    )


def _compute_turn_signals(
    fear: pd.DataFrame,
    signal_volumes: pd.Series,
    config: SignalConfig,
) -> pd.DataFrame:
    """Mirror ``compute_turn_signals`` without importing mutating backend setup."""
    frame = fear.sort_values("date").reset_index(drop=True).copy()
    scores = pd.to_numeric(frame["score"], errors="coerce").to_numpy(float)
    volumes = pd.to_numeric(signal_volumes, errors="coerce").to_numpy(float)
    ma5 = pd.Series(scores).rolling(5, min_periods=5).mean().to_numpy(float)
    flags = {
        kind: np.zeros(len(frame), dtype=bool)
        for kind in ("ma5_bottom", "ma5_top", "volume_bottom", "volume_top")
    }
    log_z_values = np.full(len(frame), np.nan)
    last_signal = {kind: -config.cooldown_days - 1 for kind in flags}
    valid_volume_history: list[tuple[int, float]] = []

    for index, score in enumerate(scores):
        if _finite(volumes[index]) and float(volumes[index]) > 0:
            previous = [
                value
                for prior_index, value in valid_volume_history
                if prior_index >= index - 60
            ][-20:]
            if len(previous) == 20 and all(value > 0 for value in previous):
                logs = np.log(np.asarray(previous, dtype=float))
                standard_deviation = float(logs.std(ddof=1))
                log_z_values[index] = (
                    (math.log(float(volumes[index])) - float(logs.mean())) / standard_deviation
                    if standard_deviation > 0 else 0.0
                )
            valid_volume_history.append((index, float(volumes[index])))

        if not _finite(score):
            continue
        recent = scores[max(0, index - config.ma5_lookback_days + 1): index + 1]
        candidates = {
            "ma5_bottom": (
                index >= 2
                and _finite(ma5[index]) and _finite(ma5[index - 1]) and _finite(ma5[index - 2])
                and ma5[index] > ma5[index - 1] < ma5[index - 2]
                and np.any(np.isfinite(recent) & (recent <= config.ma5_bottom_score))
            ),
            "ma5_top": (
                index >= 2
                and _finite(ma5[index]) and _finite(ma5[index - 1]) and _finite(ma5[index - 2])
                and ma5[index] < ma5[index - 1] > ma5[index - 2]
                and np.any(np.isfinite(recent) & (recent >= config.ma5_top_score))
            ),
            "volume_bottom": (
                score <= config.volume_bottom_score
                and _finite(log_z_values[index])
                and log_z_values[index] > config.volume_expand_z
            ),
            "volume_top": (
                score >= config.volume_top_score
                and _finite(log_z_values[index])
                and log_z_values[index] < config.volume_shrink_z
            ),
        }
        for kind, matched in candidates.items():
            if matched and index - last_signal[kind] > config.cooldown_days:
                flags[kind][index] = True
                last_signal[kind] = index

    frame["ma5"] = ma5
    frame["log_volume_z"] = log_z_values
    for kind, values in flags.items():
        frame[kind] = values
    return frame


def _mode_events(signals: pd.DataFrame, mode: str, start: date, end: date) -> list[tuple[date, str]]:
    active = signals[(signals["date"].dt.date >= start) & (signals["date"].dt.date <= end)]
    events: list[tuple[date, str]] = []
    for row in active.itertuples(index=False):
        if mode == "ma5":
            bottom, top = bool(row.ma5_bottom), bool(row.ma5_top)
        elif mode == "volume":
            bottom, top = bool(row.volume_bottom), bool(row.volume_top)
        else:
            bottom = bool(row.ma5_bottom or row.volume_bottom)
            top = bool(row.ma5_top or row.volume_top)
        # A contradictory same-day union is not assigned an arbitrary regime.
        if bottom == top:
            continue
        events.append((row.date.date(), "bottom" if bottom else "top"))
    return events


def _run_lengths(flag: pd.Series) -> pd.Series:
    normalized = flag.fillna(False).astype(bool)
    groups = (~normalized).cumsum()
    return normalized.astype(int).groupby(groups).cumsum().where(normalized, 0).astype(int)


def _prepare_stock(stock: pd.DataFrame) -> pd.DataFrame:
    frame = stock.sort_values("trade_date").reset_index(drop=True).copy()
    close = pd.to_numeric(frame["close"], errors="coerce")
    lag4 = close.shift(4)
    frame["high_count"] = _run_lengths(close > lag4)
    frame["low_count"] = _run_lengths(close < lag4)
    low_anchor = frame["low_count"] >= 9
    frame["last_low9_date"] = frame["trade_date"].where(low_anchor).ffill()
    previous_close = close.shift(1)
    frame["true_range"] = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    frame["atr14"] = frame["true_range"].rolling(14, min_periods=14).mean()
    red = frame["high_count"] >= 2
    frame["last_red_close"] = close.where(red).ffill()
    frame["last_red_count"] = frame["high_count"].where(red).ffill()
    frame["last_red_date"] = frame["trade_date"].where(red).ffill()
    frame["red_drawdown_atr"] = (frame["last_red_close"] - close) / frame["atr14"]
    return frame


def _members_as_of(
    snapshot_dates: np.ndarray,
    members_by_snapshot: dict[pd.Timestamp, frozenset[str]],
    signal_day: date,
) -> frozenset[str]:
    day = np.datetime64(signal_day)
    position = int(np.searchsorted(snapshot_dates, day, side="right") - 1)
    if position < 0:
        return frozenset()
    timestamp = pd.Timestamp(snapshot_dates[position])
    return members_by_snapshot.get(timestamp, frozenset())


def _maximum_drawdown(values: pd.Series) -> float:
    return float((values / values.cummax() - 1).min()) if len(values) else 0.0


def _simulate_pair(
    stock: pd.DataFrame,
    events: list[tuple[date, str]],
    bottom_members: dict[date, frozenset[str]],
    stock_code: str,
    index_code: str,
    index_name: str,
    mode: str,
    start: date,
    end: date,
) -> tuple[dict[str, Any], list[dict[str, Any]], pd.Series, pd.Series]:
    active = stock[
        (stock["trade_date"].dt.date >= start)
        & (stock["trade_date"].dt.date <= end)
    ].reset_index(drop=True)
    eligible_bottoms = sorted(day for day, members in bottom_members.items() if stock_code in members)
    if active.empty or not eligible_bottoms:
        raise ValueError("pair has no active bars or eligible bottom")

    capital = 1.0
    units = 0.0
    entry_capital: float | None = None
    entry_price: float | None = None
    entry_date: date | None = None
    entry_signal_date: date | None = None
    entry_bottom_date: date | None = None
    entry_low9_date: date | None = None
    consumed_low9_date: date | None = None
    pending: dict[str, Any] | None = None
    regime: str | None = None
    eligible = False
    latest_bottom_date: date | None = None
    latest_top_date: date | None = None
    event_index = 0
    trade_rows: list[dict[str, Any]] = []
    equity_values: list[float] = []
    exposure_days = 0
    buy_signal_count = 0
    sell_signal_count = 0

    first_eligible_bottom = eligible_bottoms[0]
    benchmark_entry_index = next(
        (index for index, day in enumerate(active["trade_date"].dt.date) if day >= first_eligible_bottom),
        None,
    )
    benchmark_units = 0.0
    benchmark_values: list[float] = []

    for row_index, row in active.iterrows():
        day = row["trade_date"].date()
        open_price = float(row["open"])
        close_price = float(row["close"])

        # Prior close signal executes at this stock's next available open.
        if pending is not None:
            if pending["side"] == "buy":
                entry_capital = capital
                units = capital * (1 - COMMISSION_RATE) / open_price
                capital = 0.0
                entry_price = open_price
                entry_date = day
                entry_signal_date = pending["signal_date"]
                entry_bottom_date = pending["bottom_date"]
                entry_low9_date = pending["low9_date"]
                consumed_low9_date = pending["low9_date"]
            else:
                gross = units * open_price
                capital = gross * (1 - COMMISSION_RATE - STAMP_DUTY_RATE)
                net_return = capital / float(entry_capital) - 1
                trade_rows.append({
                    "index_code": index_code,
                    "index_name": index_name,
                    "signal_mode": mode,
                    "ts_code": stock_code,
                    "name": str(row.get("name") or ""),
                    "bottom_signal_date": entry_bottom_date,
                    "buy_low9_date": entry_low9_date,
                    "buy_signal_date": entry_signal_date,
                    "buy_date": entry_date,
                    "buy_price": entry_price,
                    "top_signal_date": pending["top_signal_date"],
                    "sell_signal_date": pending["signal_date"],
                    "sell_date": day,
                    "sell_price": open_price,
                    "sell_low_count": pending["low_count"],
                    "anchor_high_count": pending["anchor_high_count"],
                    "anchor_date": pending["anchor_date"],
                    "anchor_close": pending["anchor_close"],
                    "sell_atr14": pending["atr14"],
                    "sell_drawdown_atr": pending["drawdown_atr"],
                    "holding_calendar_days": (day - entry_date).days,
                    "net_return": net_return,
                })
                units = 0.0
                entry_capital = entry_price = None
                entry_date = entry_signal_date = entry_bottom_date = None
                entry_low9_date = None
            pending = None

        # Apply every index signal observed since this stock last traded.
        while event_index < len(events) and events[event_index][0] <= day:
            event_day, kind = events[event_index]
            if kind == "bottom":
                regime = "bottom"
                latest_bottom_date = event_day
                eligible = stock_code in bottom_members.get(event_day, frozenset())
            else:
                regime = "top"
                eligible = False
                latest_top_date = event_day
            event_index += 1

        equity_values.append(capital if units == 0 else units * close_price)
        if units > 0:
            exposure_days += 1

        if benchmark_entry_index is not None and row_index == benchmark_entry_index:
            benchmark_units = (1 - COMMISSION_RATE) / open_price
        benchmark_values.append(1.0 if benchmark_units == 0 else benchmark_units * close_price)

        if row_index + 1 >= len(active):
            continue
        if units == 0 and pending is None:
            low9_timestamp = row["last_low9_date"]
            low9_date = low9_timestamp.date() if pd.notna(low9_timestamp) else None
            if (
                regime == "bottom"
                and eligible
                and int(row["high_count"]) == 2
                and low9_date is not None
                and low9_date < day
                and low9_date != consumed_low9_date
            ):
                pending = {
                    "side": "buy",
                    "signal_date": day,
                    "bottom_date": latest_bottom_date,
                    "low9_date": low9_date,
                }
                buy_signal_count += 1
        elif units > 0 and pending is None:
            if (
                regime == "top"
                and int(row["low_count"]) in SELL_LOW_COUNTS
                and _finite(row["last_red_count"])
                and float(row["last_red_count"]) >= MIN_ANCHOR_HIGH_COUNT
                and _finite(row["red_drawdown_atr"])
                and float(row["red_drawdown_atr"]) > SELL_ATR_THRESHOLD
            ):
                pending = {
                    "side": "sell",
                    "signal_date": day,
                    "top_signal_date": latest_top_date,
                    "low_count": int(row["low_count"]),
                    "anchor_high_count": int(row["last_red_count"]),
                    "anchor_date": row["last_red_date"].date(),
                    "anchor_close": float(row["last_red_close"]),
                    "atr14": float(row["atr14"]),
                    "drawdown_atr": float(row["red_drawdown_atr"]),
                }
                sell_signal_count += 1

    index = pd.DatetimeIndex(active["trade_date"])
    equity = pd.Series(equity_values, index=index, name=stock_code)
    benchmark = pd.Series(benchmark_values, index=index, name=stock_code)
    completed = pd.DataFrame(trade_rows)
    strategy_return = float(equity.iloc[-1] - 1)
    benchmark_return = float(benchmark.iloc[-1] - 1)
    summary = {
        "index_code": index_code,
        "index_name": index_name,
        "signal_mode": mode,
        "ts_code": stock_code,
        "name": str(active.iloc[-1].get("name") or ""),
        "start_date": active.iloc[0]["trade_date"].date(),
        "end_date": active.iloc[-1]["trade_date"].date(),
        "first_eligible_bottom": first_eligible_bottom,
        "eligible_bottom_signals": len(eligible_bottoms),
        "buy_signals": buy_signal_count,
        "sell_signals": sell_signal_count,
        "completed_trades": len(trade_rows),
        "strategy_return": strategy_return,
        "buy_hold_return": benchmark_return,
        "excess_return": strategy_return - benchmark_return,
        "max_drawdown": _maximum_drawdown(equity),
        "buy_hold_max_drawdown": _maximum_drawdown(benchmark),
        "exposure_pct": exposure_days / len(active) * 100,
        "win_rate": (completed["net_return"] > 0).mean() if len(completed) else np.nan,
        "median_trade_return": completed["net_return"].median() if len(completed) else np.nan,
        "open_position": bool(units > 0),
    }
    return summary, trade_rows, equity, benchmark


def _load_inputs(
    analytics_db: str,
    sqlite_db: str,
    target_codes: list[str],
    start: date,
    end: date,
) -> tuple[
    SignalConfig,
    dict[str, pd.DataFrame],
    dict[str, str],
    pd.DataFrame,
    pd.DataFrame,
    dict[str, str],
]:
    uri = f"file:{Path(sqlite_db).resolve()}?mode=ro"
    sqlite_connection = sqlite3.connect(uri, uri=True)
    try:
        config = _load_signal_config(sqlite_connection)
        placeholders = ",".join("?" for _ in target_codes)
        fear = pd.read_sql_query(
            f"""
            SELECT symbol, date, score, etf_volume
            FROM etf_fear_greed_clone_history
            WHERE symbol IN ({placeholders})
            ORDER BY symbol, date
            """,
            sqlite_connection,
            params=target_codes,
            parse_dates=["date"],
        )
    finally:
        sqlite_connection.close()

    target_by_code = {
        str(item["symbol"]).upper(): item
        for item in A_STOCK_INDEX_FEAR_GREED_TARGETS
        if str(item["symbol"]).upper() in target_codes
    }
    proxy_symbols = sorted({
        str(item.get("proxy_etf") or "").upper()
        for item in target_by_code.values()
        if item.get("proxy_etf")
    } | {symbol for values in AGGREGATE_VOLUME_ETFS.values() for symbol in values})

    connection = duckdb.connect(analytics_db, read_only=True)
    try:
        proxy_codes = pd.DataFrame({"ts_code": proxy_symbols})
        connection.register("wanted_proxy_codes", proxy_codes)
        proxy_bars = connection.execute(
            """
            SELECT f.ts_code, f.trade_date, f.vol, f.amount
            FROM a_stock_fund_daily_qfq f
            JOIN wanted_proxy_codes p USING (ts_code)
            WHERE f.trade_date BETWEEN ? AND ?
            ORDER BY f.ts_code, f.trade_date
            """,
            [start - timedelta(days=1500), end],
        ).df()
        wanted_indexes = pd.DataFrame({"index_code": target_codes})
        connection.register("wanted_indexes", wanted_indexes)
        weights = connection.execute(
            """
            SELECT w.index_code, w.trade_date, w.con_code, w.weight
            FROM a_stock_index_weight w
            JOIN wanted_indexes i USING (index_code)
            ORDER BY w.index_code, w.trade_date, w.con_code
            """
        ).df()
    finally:
        connection.close()

    fear_signals: dict[str, pd.DataFrame] = {}
    volume_sources: dict[str, str] = {}
    proxy_bars["trade_date"] = pd.to_datetime(proxy_bars["trade_date"])
    for code, group in fear.groupby("symbol", sort=True):
        index_fear = group.sort_values("date").reset_index(drop=True)
        first_date = index_fear["date"].min()
        last_date = index_fear["date"].max()
        volumes = pd.to_numeric(index_fear["etf_volume"], errors="coerce").copy()
        volume_source = "index_volume"
        if code in AGGREGATE_VOLUME_ETFS:
            parts = proxy_bars[proxy_bars["ts_code"].isin(AGGREGATE_VOLUME_ETFS[code])]
            aggregate = parts.groupby("trade_date", as_index=True)["amount"].sum(min_count=1)
            if not aggregate.empty and aggregate.index.min() <= first_date and aggregate.index.max() >= last_date:
                volumes = index_fear["date"].map(aggregate)
                volume_source = "aggregate_proxy_etf_turnover"
        else:
            proxy = str(target_by_code.get(code, {}).get("proxy_etf") or "").upper()
            bars = proxy_bars[proxy_bars["ts_code"] == proxy].set_index("trade_date") if proxy else pd.DataFrame()
            if not bars.empty and bars.index.min() <= first_date and bars.index.max() >= last_date:
                volumes = index_fear["date"].map(bars["vol"])
                volume_source = f"proxy_etf:{proxy}"
        fear_signals[code] = _compute_turn_signals(index_fear, volumes, config)
        volume_sources[code] = volume_source

    index_names = {
        code: str(target_by_code.get(code, {}).get("ticker") or code)
        for code in target_codes
    }
    weights["trade_date"] = pd.to_datetime(weights["trade_date"])
    return config, fear_signals, volume_sources, weights, fear, index_names


def analyze(
    analytics_db: str = DEFAULT_ANALYTICS_DB,
    sqlite_db: str = DEFAULT_SQLITE_DB,
    output: str | Path = DEFAULT_OUTPUT,
    start: date = DEFAULT_START,
    end: date | None = None,
    index_codes: Iterable[str] | None = None,
) -> dict[str, pd.DataFrame | dict[str, Any]]:
    all_targets = {str(item["symbol"]).upper(): item for item in A_STOCK_INDEX_FEAR_GREED_TARGETS}
    selected_codes = [str(code).upper() for code in (index_codes or all_targets)]
    selected_codes = [code for code in selected_codes if code in all_targets]
    if not selected_codes:
        raise ValueError("no configured A-share fear/greed indexes selected")

    if end is None:
        uri = f"file:{Path(sqlite_db).resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as sqlite_connection:
            end_text = sqlite_connection.execute(
                "SELECT max(date) FROM etf_fear_greed_clone_history"
            ).fetchone()[0]
        end = pd.Timestamp(end_text).date()

    config, fear_signals, volume_sources, weights, fear, index_names = _load_inputs(
        analytics_db, sqlite_db, selected_codes, start, end
    )

    snapshot_dates_by_index: dict[str, np.ndarray] = {}
    members_by_index_snapshot: dict[str, dict[pd.Timestamp, frozenset[str]]] = {}
    for code, group in weights.groupby("index_code", sort=True):
        members = {
            pd.Timestamp(snapshot): frozenset(part["con_code"].astype(str))
            for snapshot, part in group.groupby("trade_date", sort=True)
        }
        members_by_index_snapshot[code] = members
        snapshot_dates_by_index[code] = np.array(sorted(members), dtype="datetime64[ns]")

    events_by_index_mode: dict[tuple[str, str], list[tuple[date, str]]] = {}
    bottom_members_by_index_mode: dict[tuple[str, str], dict[date, frozenset[str]]] = {}
    candidate_codes: set[str] = set()
    signal_rows: list[dict[str, Any]] = []
    for code, signals in fear_signals.items():
        if code not in snapshot_dates_by_index:
            continue
        for mode in SIGNAL_MODES:
            events = _mode_events(signals, mode, start, end)
            bottom_members: dict[date, frozenset[str]] = {}
            for event_day, kind in events:
                if kind == "bottom":
                    members = _members_as_of(
                        snapshot_dates_by_index[code],
                        members_by_index_snapshot[code],
                        event_day,
                    )
                    if members:
                        bottom_members[event_day] = members
                        candidate_codes.update(members)
            events_by_index_mode[(code, mode)] = events
            bottom_members_by_index_mode[(code, mode)] = bottom_members
            signal_rows.append({
                "index_code": code,
                "index_name": index_names[code],
                "signal_mode": mode,
                "fear_start_date": signals["date"].min().date(),
                "fear_end_date": signals["date"].max().date(),
                "bottom_signals": sum(kind == "bottom" for _, kind in events),
                "top_signals": sum(kind == "top" for _, kind in events),
                "bottoms_with_members": len(bottom_members),
                "candidate_stocks": len(set().union(*bottom_members.values())) if bottom_members else 0,
                "volume_source": volume_sources[code],
            })

    if not candidate_codes:
        raise ValueError("no point-in-time constituents found for selected bottom signals")

    connection = duckdb.connect(analytics_db, read_only=True)
    try:
        connection.register("candidate_codes", pd.DataFrame({"ts_code": sorted(candidate_codes)}))
        prices = connection.execute(
            """
            SELECT q.ts_code, b.name, q.trade_date, q.open, q.high, q.low, q.close
            FROM a_stock_market_daily_qfq q
            JOIN candidate_codes c USING (ts_code)
            LEFT JOIN a_stock_basic b USING (ts_code)
            WHERE q.trade_date BETWEEN ? AND ?
              AND q.open > 0 AND q.high > 0 AND q.low > 0 AND q.close > 0
              AND q.vol > 0
            ORDER BY q.ts_code, q.trade_date
            """,
            [start - timedelta(days=120), end],
        ).df()
    finally:
        connection.close()
    prices["trade_date"] = pd.to_datetime(prices["trade_date"])
    prepared_stocks = {
        code: _prepare_stock(group)
        for code, group in prices.groupby("ts_code", sort=True)
    }

    pair_rows: list[dict[str, Any]] = []
    trade_rows: list[dict[str, Any]] = []
    index_rows: list[dict[str, Any]] = []
    equity_rows: list[pd.DataFrame] = []
    signal_summary = pd.DataFrame(signal_rows)

    for signal_info in signal_summary.itertuples(index=False):
        code = signal_info.index_code
        mode = signal_info.signal_mode
        bottom_members = bottom_members_by_index_mode[(code, mode)]
        candidates = sorted(set().union(*bottom_members.values())) if bottom_members else []
        if not candidates:
            continue
        events = events_by_index_mode[(code, mode)]
        pair_equities: list[pd.Series] = []
        pair_benchmarks: list[pd.Series] = []
        local_pair_rows: list[dict[str, Any]] = []
        local_trade_rows: list[dict[str, Any]] = []
        for stock_code in candidates:
            stock = prepared_stocks.get(stock_code)
            if stock is None:
                continue
            try:
                pair, trades, equity, benchmark = _simulate_pair(
                    stock,
                    events,
                    bottom_members,
                    stock_code,
                    code,
                    index_names[code],
                    mode,
                    start,
                    end,
                )
            except ValueError:
                continue
            local_pair_rows.append(pair)
            local_trade_rows.extend(trades)
            pair_equities.append(equity)
            pair_benchmarks.append(benchmark)
        if not local_pair_rows:
            continue

        pairs = pd.DataFrame(local_pair_rows)
        index_calendar = pd.DatetimeIndex(sorted({
            timestamp
            for series in pair_equities
            for timestamp in series.index
        }))
        equity_panel = pd.concat(
            [series.reindex(index_calendar).ffill().fillna(1.0) for series in pair_equities],
            axis=1,
        )
        benchmark_panel = pd.concat(
            [series.reindex(index_calendar).ffill().fillna(1.0) for series in pair_benchmarks],
            axis=1,
        )
        portfolio = equity_panel.mean(axis=1)
        benchmark_portfolio = benchmark_panel.mean(axis=1)
        elapsed_years = max((index_calendar[-1] - index_calendar[0]).days / 365.25, 1 / 365.25)
        local_trades = pd.DataFrame(local_trade_rows)
        index_rows.append({
            "index_code": code,
            "index_name": index_names[code],
            "signal_mode": mode,
            "start_date": index_calendar[0].date(),
            "end_date": index_calendar[-1].date(),
            "bottom_signals": signal_info.bottom_signals,
            "top_signals": signal_info.top_signals,
            "candidate_stocks": len(pairs),
            "stocks_with_buys": int((pairs["buy_signals"] > 0).sum()),
            "stocks_with_completed_trades": int((pairs["completed_trades"] > 0).sum()),
            "completed_trades": len(local_trades),
            "portfolio_return": float(portfolio.iloc[-1] - 1),
            "portfolio_cagr": float(portfolio.iloc[-1] ** (1 / elapsed_years) - 1),
            "portfolio_max_drawdown": _maximum_drawdown(portfolio),
            "buy_hold_return": float(benchmark_portfolio.iloc[-1] - 1),
            "buy_hold_cagr": float(benchmark_portfolio.iloc[-1] ** (1 / elapsed_years) - 1),
            "buy_hold_max_drawdown": _maximum_drawdown(benchmark_portfolio),
            "median_stock_return": float(pairs["strategy_return"].median()),
            "positive_stock_pct": float((pairs["strategy_return"] > 0).mean() * 100),
            "median_excess_return": float(pairs["excess_return"].median()),
            "median_stock_max_drawdown": float(pairs["max_drawdown"].median()),
            "median_exposure_pct": float(pairs["exposure_pct"].median()),
            "trade_win_rate_pct": (
                float((local_trades["net_return"] > 0).mean() * 100)
                if len(local_trades) else np.nan
            ),
            "median_trade_return": (
                float(local_trades["net_return"].median())
                if len(local_trades) else np.nan
            ),
        })
        equity_rows.append(pd.DataFrame({
            "trade_date": index_calendar,
            "index_code": code,
            "index_name": index_names[code],
            "signal_mode": mode,
            "equity": portfolio.to_numpy(),
            "buy_hold_equity": benchmark_portfolio.to_numpy(),
        }))
        pair_rows.extend(local_pair_rows)
        trade_rows.extend(local_trade_rows)

    pair_summary = pd.DataFrame(pair_rows)
    trades = pd.DataFrame(trade_rows)
    index_summary = pd.DataFrame(index_rows)
    index_equity = pd.concat(equity_rows, ignore_index=True) if equity_rows else pd.DataFrame()
    mode_summary = (
        pair_summary.groupby("signal_mode", observed=True)
        .agg(
            indices=("index_code", "nunique"),
            index_stock_pairs=("ts_code", "size"),
            pairs_with_buys=("buy_signals", lambda values: int((values > 0).sum())),
            pairs_with_completed_trades=("completed_trades", lambda values: int((values > 0).sum())),
            completed_trades=("completed_trades", "sum"),
            equal_weight_pair_return=("strategy_return", "mean"),
            median_pair_return=("strategy_return", "median"),
            positive_pair_pct=("strategy_return", lambda values: (values > 0).mean() * 100),
            mean_buy_hold_return=("buy_hold_return", "mean"),
            median_excess_return=("excess_return", "median"),
            median_max_drawdown=("max_drawdown", "median"),
            median_exposure_pct=("exposure_pct", "median"),
        )
        .reset_index()
    )
    if not trades.empty:
        trade_mode = (
            trades.groupby("signal_mode", observed=True)
            .agg(
                trade_win_rate_pct=("net_return", lambda values: (values > 0).mean() * 100),
                median_trade_return=("net_return", "median"),
                mean_trade_return=("net_return", "mean"),
                median_holding_days=("holding_calendar_days", "median"),
            )
            .reset_index()
        )
        mode_summary = mode_summary.merge(trade_mode, on="signal_mode", how="left")

    metadata = {
        "analytics_source": analytics_db,
        "fear_source": sqlite_db,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "configured_indexes": len(selected_codes),
        "indexes_with_fear_history": len(fear_signals),
        "indexes_backtested": int(index_summary["index_code"].nunique()) if not index_summary.empty else 0,
        "signal_modes": list(SIGNAL_MODES),
        "signal_config": config.__dict__,
        "entry_rule": (
            "after index bottom, point-in-time constituent buys on the first high_count == 2 "
            "after an unconsumed low_count >= 9; next open"
        ),
        "exit_rule": (
            "after index top, low_count in 2/3/4 and latest red marker high_count >= 7 "
            "and drawdown > 2 ATR14; next open"
        ),
        "regime_rule": "latest bottom enables entries and cancels sell mode; latest top disables entries and enables exits",
        "component_rule": "constituents frozen from latest index-weight snapshot available on each bottom-signal date",
        "commission_each_side": COMMISSION_RATE,
        "sell_stamp_duty": STAMP_DUTY_RATE,
        "open_positions_marked_at_final_close": True,
        "candidate_stock_codes": len(candidate_codes),
        "index_stock_pairs": len(pair_summary),
        "completed_trades": len(trades),
    }

    destination = Path(output)
    destination.mkdir(parents=True, exist_ok=True)
    pair_summary.to_csv(destination / "pair_summary.csv", index=False)
    trades.to_csv(destination / "trades.csv", index=False)
    index_summary.to_csv(destination / "index_summary.csv", index=False)
    mode_summary.to_csv(destination / "mode_summary.csv", index=False)
    signal_summary.to_csv(destination / "signal_summary.csv", index=False)
    index_equity.to_csv(destination / "index_equity.csv", index=False)
    (destination / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return {
        "pair_summary": pair_summary,
        "trades": trades,
        "index_summary": index_summary,
        "mode_summary": mode_summary,
        "signal_summary": signal_summary,
        "index_equity": index_equity,
        "metadata": metadata,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--analytics-db", default=DEFAULT_ANALYTICS_DB)
    parser.add_argument("--sqlite-db", default=DEFAULT_SQLITE_DB)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--indexes", help="comma-separated configured index symbols")
    args = parser.parse_args()
    indexes = [value.strip().upper() for value in args.indexes.split(",")] if args.indexes else None
    result = analyze(
        analytics_db=args.analytics_db,
        sqlite_db=args.sqlite_db,
        output=args.output,
        start=args.start,
        end=args.end,
        index_codes=indexes,
    )
    print(json.dumps(result["metadata"], ensure_ascii=False, indent=2, default=str))
    print(result["mode_summary"].to_string(index=False))


if __name__ == "__main__":
    main()

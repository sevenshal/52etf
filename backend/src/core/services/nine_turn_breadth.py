"""Nine-turn breadth research service for A-share indices.

The module is intentionally independent from Factor Lab.  It reads point-in-time
index constituents and QFQ daily prices from DuckDB, then exposes a compact base
snapshot that the API can classify with a user-selected percentile.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

from ..duckdb_utils import ANALYTICS_DB_PATH, connect_duckdb
from ...robot.a_stock_base_data_config import A_STOCK_INDEX_FEAR_GREED_TARGETS


DEFAULT_PERCENTILE = 90.0
LOOKBACK_TRADING_DAYS = 252
STAGE_MIN = 7
STAGE_MAX = 9
CACHE_TTL_SECONDS = 300


@dataclass
class _CacheEntry:
    created_at: float
    as_of_date: date
    payload: Dict


_cache_lock = threading.Lock()
_base_cache: Optional[_CacheEntry] = None


def _target_metadata() -> Dict[str, Dict]:
    result: Dict[str, Dict] = {}
    broad_codes = {
        "000300.SH", "000510.SH", "000905.SH", "000985.SH", "899050.BJ",
        "000680.SH", "000688.SH", "000698.SH", "000699.SH", "399006.SZ",
    }
    for item in A_STOCK_INDEX_FEAR_GREED_TARGETS:
        code = str(item.get("symbol") or "").upper()
        if not code or code in result:
            continue
        result[code] = {
            "index_code": code,
            "name": item.get("ticker") or item.get("label") or code,
            "label": item.get("label") or item.get("ticker") or code,
            "index_name": item.get("index_name") or item.get("ticker") or code,
            "category": "宽基" if code in broad_codes else "行业主题",
        }
    return result


def _safe_float(value) -> Optional[float]:
    if value is None or pd.isna(value):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _compute_stages(frame: pd.DataFrame) -> pd.DataFrame:
    """Add raw consecutive high/low setup counts using close versus close[-4]."""
    if frame.empty:
        result = frame.copy()
        result["high_stage"] = pd.Series(dtype="int64")
        result["low_stage"] = pd.Series(dtype="int64")
        return result
    result = frame.sort_values(["ts_code", "trade_date"]).copy()
    lag4 = result.groupby("ts_code", sort=False)["close"].shift(4)
    high_flag = result["close"] > lag4
    low_flag = result["close"] < lag4

    def streak(flag: pd.Series) -> pd.Series:
        resets = (~flag).groupby(result["ts_code"], sort=False).cumsum()
        values = flag.astype("int64").groupby([result["ts_code"], resets], sort=False).cumsum()
        return values.where(flag, 0).astype("int64")

    result["high_stage"] = streak(high_flag)
    result["low_stage"] = streak(low_flag)
    return result


def _percentile(values: Iterable[float], percentile: float) -> Optional[float]:
    usable = np.asarray([float(value) for value in values if value is not None and math.isfinite(float(value))])
    if not len(usable):
        return None
    return float(np.percentile(usable, percentile))


def _connect():
    return connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)


def _placeholders(values: Sequence[str]) -> str:
    return ",".join("?" for _ in values)


def _latest_trade_date(connection) -> date:
    row = connection.execute("SELECT MAX(trade_date) FROM a_stock_market_daily_qfq").fetchone()
    if not row or not row[0]:
        raise RuntimeError("A股前复权行情为空")
    return row[0]


def _load_price_stages(connection, index_codes: Sequence[str], as_of_date: date) -> pd.DataFrame:
    history_start = as_of_date - timedelta(days=430)
    query = f"""
        WITH available_members AS (
          SELECT DISTINCT con_code AS ts_code
          FROM a_stock_index_weight
          WHERE index_code IN ({_placeholders(index_codes)})
        )
        SELECT d.ts_code, d.trade_date, d.close, d.pct_chg, b.name
        FROM a_stock_market_daily_qfq d
        JOIN available_members m USING (ts_code)
        LEFT JOIN a_stock_basic b USING (ts_code)
        WHERE d.trade_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
        ORDER BY d.ts_code, d.trade_date
    """
    prices = connection.execute(query, [*index_codes, history_start, as_of_date]).fetchdf()
    return _compute_stages(prices)


def _load_memberships(connection, index_codes: Sequence[str], start_date: date, as_of_date: date) -> pd.DataFrame:
    query = f"""
        WITH index_dates AS (
          SELECT ts_code AS index_code, trade_date
          FROM a_stock_index_daily
          WHERE ts_code IN ({_placeholders(index_codes)})
            AND trade_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
        ), mapped AS (
          SELECT d.index_code, d.trade_date, MAX(w.trade_date) AS snapshot_date
          FROM index_dates d
          JOIN (SELECT DISTINCT index_code, trade_date FROM a_stock_index_weight) w
            ON w.index_code = d.index_code AND w.trade_date <= d.trade_date
          GROUP BY d.index_code, d.trade_date
        )
        SELECT m.index_code, m.trade_date, w.con_code AS ts_code, w.weight
        FROM mapped m
        JOIN a_stock_index_weight w
          ON w.index_code = m.index_code AND w.trade_date = m.snapshot_date
        ORDER BY m.index_code, m.trade_date, w.weight DESC NULLS LAST, w.con_code
    """
    return connection.execute(query, [*index_codes, start_date, as_of_date]).fetchdf()


def _build_base_snapshot(as_of_date: date, connection=None) -> Dict:
    metadata = _target_metadata()
    own_connection = connection is None
    connection = connection or _connect()
    try:
        available_rows = connection.execute(
            f"SELECT DISTINCT index_code FROM a_stock_index_weight WHERE index_code IN ({_placeholders(list(metadata))})",
            list(metadata),
        ).fetchall()
        index_codes = [row[0] for row in available_rows if row[0] in metadata]
        if not index_codes:
            return {"as_of_date": as_of_date.isoformat(), "boards": [], "details": {}}
        prices = _load_price_stages(connection, index_codes, as_of_date)
        if prices.empty:
            return {"as_of_date": as_of_date.isoformat(), "boards": [], "details": {}}
        valid_dates = sorted(pd.to_datetime(prices["trade_date"]).dt.date.unique())
        start_date = valid_dates[max(0, len(valid_dates) - LOOKBACK_TRADING_DAYS - 1)]
        memberships = _load_memberships(connection, index_codes, start_date, as_of_date)
    finally:
        if own_connection:
            connection.close()

    joined = memberships.merge(
        prices[["ts_code", "trade_date", "close", "pct_chg", "name", "high_stage", "low_stage"]],
        on=["ts_code", "trade_date"],
        how="inner",
    )
    joined["high_active"] = joined["high_stage"].between(STAGE_MIN, STAGE_MAX)
    joined["low_active"] = joined["low_stage"].between(STAGE_MIN, STAGE_MAX)
    daily = (
        joined.groupby(["index_code", "trade_date"], observed=True)
        .agg(
            eligible_members=("ts_code", "size"),
            high_count=("high_active", "sum"),
            low_count=("low_active", "sum"),
            high_share=("high_active", "mean"),
            low_share=("low_active", "mean"),
        )
        .reset_index()
    )
    latest_date = pd.Timestamp(as_of_date)
    current = joined.loc[pd.to_datetime(joined["trade_date"]) == latest_date].copy()
    boards: List[Dict] = []
    details: Dict[str, List[Dict]] = {}
    for code in index_codes:
        history = daily.loc[daily["index_code"] == code].sort_values("trade_date").tail(LOOKBACK_TRADING_DAYS + 1)
        today = history.loc[pd.to_datetime(history["trade_date"]) == latest_date]
        if history.empty or today.empty:
            continue
        row = today.iloc[-1]
        board_members = current.loc[current["index_code"] == code].sort_values(
            ["high_stage", "low_stage", "weight"], ascending=[False, False, False], na_position="last"
        )
        details[code] = [
            {
                "ts_code": member.ts_code,
                "name": member.name if isinstance(member.name, str) and member.name.strip() else member.ts_code,
                "weight": _safe_float(member.weight),
                "close": _safe_float(member.close),
                "pct_chg": _safe_float(member.pct_chg),
                "high_stage": int(member.high_stage),
                "low_stage": int(member.low_stage),
            }
            for member in board_members.itertuples(index=False)
        ]
        boards.append({
            **metadata[code],
            "eligible_members": int(row["eligible_members"]),
            "high_count": int(row["high_count"]),
            "low_count": int(row["low_count"]),
            "high_share": float(row["high_share"]),
            "low_share": float(row["low_share"]),
            "high_history": history["high_share"].astype(float).tolist()[:-1],
            "low_history": history["low_share"].astype(float).tolist()[:-1],
            "history_days": max(0, len(history) - 1),
        })
    boards.sort(key=lambda item: (item["category"] != "宽基", item["name"], item["index_code"]))
    return {"as_of_date": as_of_date.isoformat(), "boards": boards, "details": details}


def _get_base_snapshot(force_refresh: bool = False) -> Dict:
    global _base_cache
    with _connect() as connection:
        as_of_date = _latest_trade_date(connection)
        with _cache_lock:
            cache = _base_cache
            if (
                not force_refresh
                and cache is not None
                and cache.as_of_date == as_of_date
                and time.monotonic() - cache.created_at < CACHE_TTL_SECONDS
            ):
                return cache.payload
        payload = _build_base_snapshot(as_of_date, connection=connection)
    with _cache_lock:
        _base_cache = _CacheEntry(time.monotonic(), as_of_date, payload)
    return payload


def _classify_board(board: Dict, percentile: float) -> Dict:
    high_threshold = _percentile(board.get("high_history") or [], percentile)
    low_threshold = _percentile(board.get("low_history") or [], percentile)
    high_triggered = bool(high_threshold is not None and board["high_share"] >= high_threshold and board["high_share"] > 0)
    low_triggered = bool(low_threshold is not None and board["low_share"] >= low_threshold and board["low_share"] > 0)
    if high_triggered and low_triggered:
        signal = "both"
    elif high_triggered:
        signal = "high"
    elif low_triggered:
        signal = "low"
    else:
        signal = "neutral"
    return {
        key: value for key, value in board.items() if key not in {"high_history", "low_history"}
    } | {
        "high_threshold": high_threshold,
        "low_threshold": low_threshold,
        "high_triggered": high_triggered,
        "low_triggered": low_triggered,
        "signal": signal,
    }


def get_nine_turn_breadth_overview(percentile: float = DEFAULT_PERCENTILE, force_refresh: bool = False) -> Dict:
    percentile = float(percentile)
    if not 50 <= percentile <= 99:
        raise ValueError("分位阈值必须在50到99之间")
    base = _get_base_snapshot(force_refresh=force_refresh)
    return {
        "as_of_date": base["as_of_date"],
        "percentile": percentile,
        "lookback_days": LOOKBACK_TRADING_DAYS,
        "stage_range": [STAGE_MIN, STAGE_MAX],
        "boards": [_classify_board(board, percentile) for board in base["boards"]],
    }


def get_nine_turn_breadth_detail(index_code: str, percentile: float = DEFAULT_PERCENTILE) -> Dict:
    code = str(index_code or "").strip().upper()
    overview = get_nine_turn_breadth_overview(percentile=percentile)
    board = next((item for item in overview["boards"] if item["index_code"] == code), None)
    if board is None:
        raise KeyError(code)
    base = _get_base_snapshot()
    members = base["details"].get(code, [])
    members.sort(
        key=lambda item: (
            max(item["high_stage"], item["low_stage"]) < 2,
            -max(item["high_stage"], item["low_stage"]),
            item["ts_code"],
        )
    )
    return {**board, "as_of_date": overview["as_of_date"], "percentile": overview["percentile"], "members": members}

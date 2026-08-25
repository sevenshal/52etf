"""Filtered, persistent background scans using official CZSC signals."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import date, datetime, time, timedelta
from typing import Any

import pandas as pd

from ..duckdb_utils import ANALYTICS_DB_PATH, connect_duckdb
from .chan_analysis import analyze_bars
from .chan_minute_data import aggregate_minute_rows, fetch_realtime_minute_rows, load_minute_rows


BUY_SIGNALS = {"一买", "二买", "三买"}
SELL_SIGNALS = {"一卖", "二卖", "三卖"}
REALTIME_FREQ_MAP = {"1m": "1MIN", "5m": "5MIN", "30m": "30MIN"}


def _placeholders(values: list[Any]) -> str:
    return ",".join("?" for _ in values)


def filter_stock_pool(filters: dict[str, Any]) -> list[dict[str, Any]]:
    """Filter with DuckDB before invoking CZSC; all values use parameters."""
    clauses = ["b.list_status = 'L'", "m.close IS NOT NULL", "m.amount IS NOT NULL"]
    params: list[Any] = []
    mappings = (
        ("min_total_mv", "m.total_mv >= ?"),
        ("max_total_mv", "m.total_mv <= ?"),
        ("min_circ_mv", "m.circ_mv >= ?"),
        ("max_circ_mv", "m.circ_mv <= ?"),
        ("min_avg_amount", "liq.avg_amount >= ?"),
        ("min_turnover_rate", "m.turnover_rate >= ?"),
        ("max_turnover_rate", "m.turnover_rate <= ?"),
    )
    for key, sql in mappings:
        value = filters.get(key)
        if value is not None:
            clauses.append(sql)
            params.append(float(value))
    if filters.get("exclude_st", True):
        clauses.append("b.name NOT LIKE '%ST%'")

    index_codes = [str(item).upper() for item in filters.get("index_codes") or []]
    if index_codes:
        clauses.append(
            "EXISTS (SELECT 1 FROM a_stock_index_weight iw WHERE iw.con_code = b.ts_code "
            f"AND iw.index_code IN ({_placeholders(index_codes)}) "
            "AND iw.trade_date = (SELECT MAX(iw2.trade_date) FROM a_stock_index_weight iw2 WHERE iw2.index_code = iw.index_code))"
        )
        params.extend(index_codes)

    board_codes = [str(item).upper() for item in filters.get("board_codes") or []]
    if board_codes:
        clauses.append(
            f"EXISTS (SELECT 1 FROM a_stock_ths_member tm WHERE tm.con_code = b.ts_code AND tm.ths_code IN ({_placeholders(board_codes)}) AND (tm.is_new IS NULL OR tm.is_new <> 'N'))"
        )
        params.extend(board_codes)

    lookback = max(1, min(60, int(filters.get("liquidity_days") or 20)))
    limit = max(1, min(2000, int(filters.get("limit") or 500)))
    sql = f"""
        WITH latest AS (SELECT MAX(trade_date) AS trade_date FROM a_stock_market_daily),
        recent_dates AS (
            SELECT DISTINCT trade_date FROM a_stock_market_daily ORDER BY trade_date DESC LIMIT {lookback}
        ),
        liq AS (
            SELECT ts_code, AVG(amount) AS avg_amount
            FROM a_stock_market_daily
            WHERE trade_date IN (SELECT trade_date FROM recent_dates)
            GROUP BY ts_code
        )
        SELECT b.ts_code, b.name, b.industry, b.market, m.close, m.total_mv, m.circ_mv,
               m.amount, m.turnover_rate, liq.avg_amount, m.trade_date
        FROM a_stock_basic b
        JOIN latest l ON TRUE
        JOIN a_stock_market_daily m ON m.ts_code = b.ts_code AND m.trade_date = l.trade_date
        JOIN liq ON liq.ts_code = b.ts_code
        WHERE {' AND '.join(clauses)}
        ORDER BY liq.avg_amount DESC, m.circ_mv DESC
        LIMIT {limit}
    """
    connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
    try:
        frame = connection.execute(sql, params).fetchdf()
    finally:
        connection.close()
    return frame.to_dict("records")


def _load_daily_rows(symbol: str, years: int = 4) -> list[dict[str, Any]]:
    connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
    try:
        frame = connection.execute(
            """
            SELECT CAST(trade_date AS TIMESTAMP) + INTERVAL 15 HOUR AS timestamp,
                   open, high, low, close, volume, turnover
            FROM a_stock_market_daily_qfq
            WHERE ts_code = ? AND trade_date >= ?
            ORDER BY trade_date
            """,
            [symbol, date.today() - timedelta(days=365 * years)],
        ).fetchdf()
    finally:
        connection.close()
    return frame.to_dict("records")


def _load_scan_rows(
    symbol: str,
    freq: str,
    realtime_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if freq == "d":
        return _load_daily_rows(symbol)
    rows = load_minute_rows(
        symbol,
        datetime.combine(date.today() - timedelta(days=100), time.min),
        datetime.combine(date.today(), time.max),
    )
    rows = aggregate_minute_rows(symbol, rows, freq)
    if realtime_rows:
        rows_by_time = {}
        for row in [*rows, *realtime_rows]:
            timestamp = pd.to_datetime(row.get("timestamp"), errors="coerce")
            if pd.isna(timestamp):
                continue
            normalized = dict(row)
            normalized["timestamp"] = timestamp.to_pydatetime()
            rows_by_time[normalized["timestamp"]] = normalized
        rows = [rows_by_time[timestamp] for timestamp in sorted(rows_by_time)]
    return rows


def _write_run(run_id: str, **updates: Any) -> None:
    if not updates:
        return
    allowed = {"status", "candidate_count", "processed_count", "signal_count", "error_count", "finished_at"}
    clean = {key: value for key, value in updates.items() if key in allowed}
    if not clean:
        return
    connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=False)
    try:
        assignments = ", ".join(f"{key} = ?" for key in clean)
        connection.execute(f"UPDATE chan_scan_run SET {assignments} WHERE id = ?", [*clean.values(), run_id])
    finally:
        connection.close()


class ChanScanManager:
    _lock = threading.Lock()
    _cancelled: set[str] = set()
    _active: set[str] = set()

    @classmethod
    def start(cls, freq: str, filters: dict[str, Any], signal_side: str = "buy", realtime: bool = False) -> str:
        with cls._lock:
            if cls._active:
                raise ValueError("已有缠论扫描正在运行")
        run_id = uuid.uuid4().hex
        connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=False)
        try:
            connection.execute(
                "INSERT INTO chan_scan_run VALUES (?, 'PENDING', ?, ?, 0, 0, 0, 0, ?, NULL)",
                [run_id, freq, json.dumps({**filters, "signal_side": signal_side, "realtime": realtime}, ensure_ascii=False), datetime.now()],
            )
        finally:
            connection.close()
        with cls._lock:
            cls._active.add(run_id)
        threading.Thread(
            target=cls._run,
            args=(run_id, freq, filters, signal_side, realtime),
            daemon=True,
            name=f"chan-scan-{run_id[:8]}",
        ).start()
        return run_id

    @classmethod
    def cancel(cls, run_id: str) -> None:
        with cls._lock:
            cls._cancelled.add(run_id)

    @classmethod
    def _is_cancelled(cls, run_id: str) -> bool:
        with cls._lock:
            return run_id in cls._cancelled

    @classmethod
    def _run(cls, run_id: str, freq: str, filters: dict[str, Any], signal_side: str, realtime: bool) -> None:
        _write_run(run_id, status="RUNNING")
        processed = errors = 0
        signal_rows: list[dict[str, Any]] = []
        try:
            candidates = filter_stock_pool(filters)
            _write_run(run_id, candidate_count=len(candidates))
            realtime_rows_by_symbol = {}
            if realtime and freq != "d" and candidates:
                realtime_rows_by_symbol = fetch_realtime_minute_rows(
                    [item["ts_code"] for item in candidates],
                    REALTIME_FREQ_MAP[freq],
                )
            expected = BUY_SIGNALS if signal_side == "buy" else SELL_SIGNALS if signal_side == "sell" else BUY_SIGNALS | SELL_SIGNALS
            for candidate in candidates:
                if cls._is_cancelled(run_id):
                    _write_run(run_id, status="CANCELLED", finished_at=datetime.now())
                    return
                symbol = candidate["ts_code"]
                try:
                    rows = _load_scan_rows(symbol, freq, realtime_rows_by_symbol.get(symbol))
                    analysis = analyze_bars(
                        symbol,
                        rows,
                        freq,
                        confirmed=not realtime,
                        include_history=False,
                    )
                    for signal in analysis["signals"]:
                        if signal["type"] in expected:
                            signal_rows.append({"symbol": symbol, **signal})
                except Exception:
                    errors += 1
                processed += 1
                if processed % 20 == 0:
                    _write_run(run_id, processed_count=processed, signal_count=len(signal_rows), error_count=errors)

            if signal_rows:
                frame = pd.DataFrame(
                    [
                        {
                            "id": f"{run_id}:{index}", "run_id": run_id, "ts_code": item["symbol"],
                            "name": item["name"], "signal_type": item["type"], "detail": item["detail"],
                            "signal_key": item["key"], "signal_value": item["value"],
                            "bar_time": pd.to_datetime(item["bar_time"]), "confirmed": bool(item["confirmed"]),
                            "created_at": datetime.now(),
                        }
                        for index, item in enumerate(signal_rows)
                    ]
                )
                connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=False)
                try:
                    connection.register("chan_scan_signal_rows", frame)
                    connection.execute("INSERT INTO chan_scan_signal SELECT * FROM chan_scan_signal_rows")
                finally:
                    connection.close()
            _write_run(
                run_id, status="SUCCESS" if not errors else "PARTIAL_SUCCESS",
                processed_count=processed, signal_count=len(signal_rows), error_count=errors, finished_at=datetime.now(),
            )
        except Exception:
            _write_run(run_id, status="FAILED", processed_count=processed, signal_count=len(signal_rows), error_count=errors + 1, finished_at=datetime.now())
        finally:
            with cls._lock:
                cls._active.discard(run_id)
                cls._cancelled.discard(run_id)


def get_scan(run_id: str) -> dict[str, Any] | None:
    connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
    try:
        run = connection.execute("SELECT * FROM chan_scan_run WHERE id = ?", [run_id]).fetchdf()
        if run.empty:
            return None
        signals = connection.execute(
            """
            SELECT s.*, b.name, b.industry, m.total_mv, m.circ_mv, m.amount, m.turnover_rate
            FROM chan_scan_signal s
            LEFT JOIN a_stock_basic b ON b.ts_code = s.ts_code
            LEFT JOIN a_stock_market_daily m ON m.ts_code = s.ts_code
              AND m.trade_date = (SELECT MAX(trade_date) FROM a_stock_market_daily)
            WHERE s.run_id = ? ORDER BY s.bar_time DESC, s.ts_code
            """,
            [run_id],
        ).fetchdf()
    finally:
        connection.close()
    result = run.iloc[0].to_dict()
    result["filters"] = json.loads(result.pop("filters_json"))
    result["signals"] = signals.to_dict("records")
    return result


def list_scans(limit: int = 20) -> list[dict[str, Any]]:
    connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
    try:
        frame = connection.execute(
            "SELECT * EXCLUDE(filters_json) FROM chan_scan_run ORDER BY started_at DESC LIMIT ?",
            [max(1, min(100, limit))],
        ).fetchdf()
    finally:
        connection.close()
    return frame.to_dict("records")

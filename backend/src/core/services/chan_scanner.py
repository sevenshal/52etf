"""Filtered, persistent background scans using the strict native Chan engine.

The per-symbol work (minute-bar load + structural analysis) is batched: one
``WHERE ts_code IN (...)`` query per chunk over a single reused read
connection, instead of one fresh DuckDB connection + heavy qfq-view recompute
per symbol.  A watchdog marks a run ``FAILED`` if progress stalls, and stale
``RUNNING`` / ``PENDING`` rows plus orphaned in-memory ``_active`` entries are
reaped on the next ``start`` so a wedged run can never block new scans or
spin the UI forever.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time as _time
import uuid
from datetime import date, datetime, time, timedelta
from typing import Any

import pandas as pd

from ..duckdb_utils import ANALYTICS_DB_PATH, connect_duckdb
from .chan_analysis import analyze_bars
from .chan_minute_data import (
    aggregate_minute_rows,
    fetch_realtime_minute_rows,
    merge_minute_rows,
)

logger = logging.getLogger(__name__)

BUY_SIGNALS = {"一买", "二买", "三买"}
SELL_SIGNALS = {"一卖", "二卖", "三卖"}
REALTIME_FREQ_MAP = {"1m": "1MIN", "5m": "5MIN", "30m": "30MIN"}
TERMINAL_STATUSES = {"SUCCESS", "PARTIAL_SUCCESS", "FAILED", "CANCELLED"}

SCAN_CHUNK = max(20, int(os.getenv("CHAN_SCAN_CHUNK", "120")))
STALL_SECONDS = max(60, int(os.getenv("CHAN_SCAN_STALL_SECONDS", "300")))
STALE_RUN_MINUTES = 15
MINUTE_LOOKBACK_DAYS = 100
DAILY_LOOKBACK_YEARS = 4
MIN_BARS_FOR_ANALYSIS = 20


def _placeholders(values: list[Any]) -> str:
    return ",".join("?" for _ in values)


def filter_stock_pool(filters: dict[str, Any], connection: Any = None) -> list[dict[str, Any]]:
    """Filter with DuckDB before invoking the structural engine; all values use parameters."""
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
    limit = max(1, min(5000, int(filters.get("limit") or 500)))
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
    own = connection is None
    conn = connection or connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
    try:
        frame = conn.execute(sql, params).fetchdf()
    finally:
        if own:
            conn.close()
    return frame.to_dict("records")


def _rows_by_symbol(frame: pd.DataFrame, symbols: list[str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in symbols}
    if frame.empty:
        return out
    for symbol, group in frame.groupby("ts_code", sort=False):
        out[str(symbol)] = group.drop(columns=["ts_code"]).to_dict("records")
    return out


def _batch_load_minute(connection: Any, symbols: list[str]) -> dict[str, list[dict[str, Any]]]:
    if not symbols:
        return {}
    start = datetime.combine(date.today() - timedelta(days=MINUTE_LOOKBACK_DAYS), time.min)
    end = datetime.combine(date.today(), time.max)
    frame = connection.execute(
        f"""
        SELECT ts_code, trade_time AS timestamp, open, high, low, close,
               vol AS volume, amount AS turnover
        FROM a_stock_minute_bar_qfq
        WHERE ts_code IN ({_placeholders(symbols)}) AND trade_time >= ? AND trade_time <= ?
        ORDER BY ts_code, trade_time
        """,
        [*symbols, start, end],
    ).fetchdf()
    return _rows_by_symbol(frame, symbols)


def _batch_load_daily(connection: Any, symbols: list[str]) -> dict[str, list[dict[str, Any]]]:
    if not symbols:
        return {}
    frame = connection.execute(
        f"""
        SELECT ts_code, CAST(trade_date AS TIMESTAMP) + INTERVAL 15 HOUR AS timestamp,
               open, high, low, close, volume, turnover
        FROM a_stock_market_daily_qfq
        WHERE ts_code IN ({_placeholders(symbols)}) AND trade_date >= ?
        ORDER BY ts_code, trade_date
        """,
        [*symbols, date.today() - timedelta(days=365 * DAILY_LOOKBACK_YEARS)],
    ).fetchdf()
    return _rows_by_symbol(frame, symbols)


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


def _safe_write_run(run_id: str, **updates: Any) -> None:
    try:
        _write_run(run_id, **updates)
    except Exception:  # noqa: BLE001 - a progress write must never crash the run
        logger.warning("chan scan progress write failed for %s", run_id, exc_info=True)


def _recover_stale_runs() -> None:
    """Fail rows a dead process left behind (only reached when no run is active)."""
    connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=False)
    try:
        connection.execute(
            "UPDATE chan_scan_run SET status = 'FAILED', finished_at = ? "
            "WHERE status IN ('RUNNING', 'PENDING')",
            [datetime.now()],
        )
    finally:
        connection.close()


class ChanScanManager:
    _lock = threading.Lock()
    _cancelled: set[str] = set()
    _active: set[str] = set()
    _progress: dict[str, tuple[int, float]] = {}

    @classmethod
    def _reap_active(cls) -> None:
        """Drop ``_active`` ids whose DB row is already terminal or missing."""
        with cls._lock:
            active = list(cls._active)
        if not active:
            return
        try:
            connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
            try:
                rows = connection.execute(
                    f"SELECT id, status FROM chan_scan_run WHERE id IN ({_placeholders(active)})",
                    active,
                ).fetchall()
            finally:
                connection.close()
        except Exception:  # noqa: BLE001
            return
        status_by_id = {row[0]: row[1] for row in rows}
        with cls._lock:
            for run_id in active:
                if status_by_id.get(run_id, "FAILED") in TERMINAL_STATUSES:
                    cls._active.discard(run_id)
                    cls._progress.pop(run_id, None)

    @classmethod
    def start(cls, freq: str, filters: dict[str, Any], signal_side: str = "buy", realtime: bool = False) -> str:
        cls._reap_active()
        with cls._lock:
            if cls._active:
                raise ValueError("已有缠论扫描正在运行")
        _recover_stale_runs()
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
            cls._progress[run_id] = (0, _time.monotonic())
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
    def _touch_progress(cls, run_id: str, processed: int) -> None:
        with cls._lock:
            cls._progress[run_id] = (processed, _time.monotonic())

    @classmethod
    def _watchdog(cls, run_id: str, stop_event: threading.Event, poll: float = 15.0) -> None:
        while not stop_event.wait(poll):
            with cls._lock:
                snapshot = cls._progress.get(run_id)
            if snapshot is None:
                return
            processed, touched_at = snapshot
            if _time.monotonic() - touched_at > STALL_SECONDS:
                logger.error("chan scan %s stalled at processed=%s; marking FAILED", run_id, processed)
                with cls._lock:
                    cls._cancelled.add(run_id)
                _safe_write_run(run_id, status="FAILED", processed_count=processed, finished_at=datetime.now())
                return

    @classmethod
    def _run(cls, run_id: str, freq: str, filters: dict[str, Any], signal_side: str, realtime: bool) -> None:
        processed = errors = 0
        signal_rows: list[dict[str, Any]] = []
        connection = None
        stop_watchdog = threading.Event()
        watchdog = threading.Thread(
            target=cls._watchdog, args=(run_id, stop_watchdog), daemon=True, name=f"chan-scan-wd-{run_id[:8]}"
        )
        watchdog.start()
        try:
            _safe_write_run(run_id, status="RUNNING")
            connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
            candidates = filter_stock_pool(filters, connection=connection)
            _safe_write_run(run_id, candidate_count=len(candidates))
            symbols = [item["ts_code"] for item in candidates]
            name_by_symbol = {item["ts_code"]: item.get("name") for item in candidates}

            realtime_rows_by_symbol: dict[str, list[dict[str, Any]]] = {}
            if realtime and freq != "d" and symbols:
                try:
                    realtime_rows_by_symbol = fetch_realtime_minute_rows(symbols, REALTIME_FREQ_MAP[freq])
                except Exception:  # noqa: BLE001 - realtime is best-effort
                    logger.warning("chan scan realtime fetch failed for run %s", run_id, exc_info=True)

            expected = (
                BUY_SIGNALS if signal_side == "buy"
                else SELL_SIGNALS if signal_side == "sell"
                else BUY_SIGNALS | SELL_SIGNALS
            )
            total = len(symbols)
            for offset in range(0, total, SCAN_CHUNK):
                if cls._is_cancelled(run_id):
                    _safe_write_run(
                        run_id, status="CANCELLED", finished_at=datetime.now(),
                        processed_count=processed, signal_count=len(signal_rows), error_count=errors,
                    )
                    return
                chunk = symbols[offset:offset + SCAN_CHUNK]
                try:
                    rows_by_symbol = (
                        _batch_load_daily(connection, chunk) if freq == "d"
                        else _batch_load_minute(connection, chunk)
                    )
                except Exception:  # noqa: BLE001 - a bad chunk load fails only that chunk
                    logger.exception("chan scan batch load failed for run %s", run_id)
                    rows_by_symbol = {}

                for symbol in chunk:
                    try:
                        rows = rows_by_symbol.get(symbol) or []
                        if rows and freq not in ("d", "1m"):
                            rows = aggregate_minute_rows(symbol, rows, freq)
                        realtime_rows = realtime_rows_by_symbol.get(symbol)
                        if realtime_rows:
                            rows = merge_minute_rows(rows, realtime_rows)
                        if len(rows) >= MIN_BARS_FOR_ANALYSIS:
                            analysis = analyze_bars(
                                symbol, rows, freq, confirmed=not realtime, include_history=False
                            )
                            for signal in analysis["signals"]:
                                if signal["type"] in expected:
                                    signal_rows.append(
                                        {"symbol": symbol, **signal, "name": name_by_symbol.get(symbol) or signal.get("name")}
                                    )
                    except Exception:  # noqa: BLE001 - one bad symbol must not stop the scan
                        errors += 1
                    processed += 1

                cls._touch_progress(run_id, processed)
                _safe_write_run(
                    run_id, processed_count=processed, signal_count=len(signal_rows), error_count=errors
                )

            if signal_rows:
                frame = pd.DataFrame(
                    [
                        {
                            "id": f"{run_id}:{index}", "run_id": run_id, "ts_code": item["symbol"],
                            "name": item.get("name"), "signal_type": item["type"], "detail": item["detail"],
                            "signal_key": item["key"], "signal_value": item["value"],
                            "bar_time": pd.to_datetime(item["bar_time"]), "confirmed": bool(item["confirmed"]),
                            "created_at": datetime.now(),
                        }
                        for index, item in enumerate(signal_rows)
                    ]
                )
                writer = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=False)
                try:
                    writer.register("chan_scan_signal_rows", frame)
                    writer.execute("INSERT INTO chan_scan_signal SELECT * FROM chan_scan_signal_rows")
                finally:
                    writer.close()

            if cls._is_cancelled(run_id):
                return
            _safe_write_run(
                run_id, status="SUCCESS" if not errors else "PARTIAL_SUCCESS",
                processed_count=processed, signal_count=len(signal_rows), error_count=errors, finished_at=datetime.now(),
            )
        except Exception:  # noqa: BLE001
            logger.exception("chan scan run %s failed", run_id)
            _safe_write_run(
                run_id, status="FAILED", processed_count=processed,
                signal_count=len(signal_rows), error_count=errors + 1, finished_at=datetime.now(),
            )
        finally:
            stop_watchdog.set()
            if connection is not None:
                try:
                    connection.close()
                except Exception:  # noqa: BLE001
                    pass
            with cls._lock:
                cls._active.discard(run_id)
                cls._cancelled.discard(run_id)
                cls._progress.pop(run_id, None)


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

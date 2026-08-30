#!/usr/bin/env python3
"""Validate committed nine-turn ATR summaries and optional local raw outputs."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import duckdb


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_close(actual: float, expected: float, label: str, tolerance: float = 1e-9) -> None:
    if not math.isclose(float(actual), float(expected), rel_tol=tolerance, abs_tol=tolerance):
        raise AssertionError(f"{label}: actual={actual}, expected={expected}")


def _csv_sql(path: Path) -> str:
    escaped = str(path).replace("'", "''")
    return f"read_csv_auto('{escaped}')"


def _validate_sell_study(connection: duckdb.DuckDBPyConnection, root: Path) -> None:
    output = root / "research/output/nine_turn_atr_sell"
    metadata = _read_json(output / "metadata.json")
    summary = _csv_sql(output / "by_low_count.csv")
    low2_summary = _csv_sql(output / "low2_true_false_summary.csv")
    total_events, low2_events = connection.execute(
        f"SELECT sum(events), sum(events) FILTER (WHERE low_count=2) FROM {summary}"
    ).fetchone()
    assert int(total_events) == metadata["sell_candidates"]
    assert int(low2_events) == metadata["low2_candidates"]
    true_low2 = connection.execute(
        f"SELECT events FROM {low2_summary} WHERE label='真卖点'"
    ).fetchone()[0]
    assert int(true_low2) == metadata["true_low2"]

    raw = output / "sell_events.csv"
    if raw.exists():
        raw_table = _csv_sql(raw)
        raw_total, raw_low2, raw_true = connection.execute(
            f"""
            SELECT count(*), count(*) FILTER (WHERE low_count=2),
                   count(*) FILTER (WHERE low_count=2 AND is_true_sell)
            FROM {raw_table}
            """
        ).fetchone()
        assert int(raw_total) == metadata["sell_candidates"]
        assert int(raw_low2) == metadata["low2_candidates"]
        assert int(raw_true) == metadata["true_low2"]


def _validate_path_study(connection: duckdb.DuckDBPyConnection, root: Path) -> None:
    output = root / "research/output/nine_turn_atr_path_validation"
    metadata = _read_json(output / "metadata.json")
    grouped = _csv_sql(output / "by_low_count.csv")
    thresholds = _csv_sql(output / "threshold_summary.csv")
    event_count = connection.execute(f"SELECT sum(events) FROM {grouped}").fetchone()[0]
    assert int(event_count) == metadata["candidate_events"]
    best = connection.execute(
        f"SELECT threshold_atr, mean_sell_advantage_20d_pct FROM {thresholds} "
        "ORDER BY mean_sell_advantage_20d_pct DESC LIMIT 1"
    ).fetchone()
    expected = metadata["best_by_mean_20d_sell_advantage"]
    _assert_close(best[0], expected["threshold_atr"], "best path threshold")
    _assert_close(best[1], expected["mean_sell_advantage_20d_pct"], "best path advantage")

    raw = output / "path_labeled_events.csv"
    if raw.exists():
        raw_table = _csv_sql(raw)
        raw_events, raw_cycles = connection.execute(
            f"SELECT count(*), count(DISTINCT cycle_id) FROM {raw_table}"
        ).fetchone()
        assert int(raw_events) == metadata["candidate_events"]
        assert int(raw_cycles) == metadata["cycles"]


def _validate_backtest(connection: duckdb.DuckDBPyConnection, output: Path) -> None:
    metadata = _read_json(output / "metadata.json")
    quantiles = _csv_sql(output / "stock_return_quantiles.csv")
    median = connection.execute(
        f"SELECT strategy_return, excess_return, max_drawdown FROM {quantiles} WHERE quantile=0.5"
    ).fetchone()
    _assert_close(median[0] * 100, metadata["median_stock_return_pct"], f"{output.name} median return")
    _assert_close(median[1] * 100, metadata["median_excess_return_pct"], f"{output.name} median excess")
    _assert_close(median[2] * 100, metadata["median_stock_max_drawdown_pct"], f"{output.name} median drawdown")

    trades = output / "trades.csv"
    if trades.exists():
        trade_table = _csv_sql(trades)
        count, win_rate, median_return = connection.execute(
            f"""
            SELECT count(*), avg((net_return > 0)::INTEGER) * 100, median(net_return) * 100
            FROM {trade_table}
            """
        ).fetchone()
        assert int(count) == metadata["completed_trades"]
        _assert_close(win_rate, metadata["trade_win_rate_pct"], f"{output.name} win rate")
        _assert_close(median_return, metadata["median_trade_return_pct"], f"{output.name} median trade")

    stocks = output / "stock_summary.csv"
    if stocks.exists():
        stock_table = _csv_sql(stocks)
        count, mean_return, positive_rate = connection.execute(
            f"""
            SELECT count(*), avg(strategy_return) * 100,
                   avg((strategy_return > 0)::INTEGER) * 100
            FROM {stock_table}
            """
        ).fetchone()
        assert int(count) == metadata["stocks"]
        _assert_close(mean_return, metadata["mean_stock_return_pct"], f"{output.name} mean return")
        _assert_close(positive_rate, metadata["positive_stock_pct"], f"{output.name} positive rate")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    connection = duckdb.connect()
    try:
        _validate_sell_study(connection, root)
        _validate_path_study(connection, root)
        _validate_backtest(connection, root / "research/output/nine_turn_atr_strategy_backtest")
        _validate_backtest(connection, root / "research/output/nine_turn_atr_strategy_backtest_low2_5_atr3")
    finally:
        connection.close()
    print("nine-turn ATR summaries validated")


if __name__ == "__main__":
    main()

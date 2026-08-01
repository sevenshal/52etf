#!/usr/bin/env python3
"""CLI wrapper for the production A-share fear/greed ETF backtest engine."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb
import pandas as pd

API_ROOT = Path(__file__).resolve().parents[1]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from src.core.services.a_stock_fear_etf_backtest_engine import (
    DEFAULT_EXCLUDED,
    build_signal_rows,
    load_etf_bars,
    load_fear,
    run_backtest,
    summarize,
    target_mapping,
)


DEFAULT_SQLITE = Path("/var/lib/quant_robot/evc_stocks.db")
DEFAULT_DUCKDB = Path("/var/lib/quant_robot/analytics.duckdb")
DEFAULT_OUTPUT = Path("lab/output/a_stock_fear_etf_range_backtest")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", type=Path, default=DEFAULT_SQLITE)
    parser.add_argument("--duckdb", type=Path, default=DEFAULT_DUCKDB)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--start", default="2020-01-02")
    parser.add_argument("--end", default="2099-12-31")
    parser.add_argument("--initial-capital", type=float, default=1_000_000)
    parser.add_argument("--fear-entry", type=float, default=25)
    parser.add_argument("--fear-exit", type=float, default=70)
    parser.add_argument("--volume-std-multiplier", type=float, default=1)
    parser.add_argument("--no-new-high-days", type=int, default=10)
    parser.add_argument("--commission-pct", type=float, default=0.03)
    parser.add_argument("--slippage-pct", type=float, default=0.02)
    parser.add_argument("--stamp-duty-pct", type=float, default=0.05)
    parser.add_argument("--lot-size", type=int, default=100)
    parser.add_argument("--exclude-index", action="append", default=list(DEFAULT_EXCLUDED))
    args = parser.parse_args()

    excluded = {str(value).upper() for value in args.exclude_index}
    mapping = target_mapping(excluded)
    fear = load_fear(args.sqlite, list(mapping), args.start, args.end)
    mapping = {index: etf for index, etf in mapping.items() if index in set(fear["index_symbol"])}
    with duckdb.connect(str(args.duckdb), read_only=True) as connection:
        bars = load_etf_bars(connection, sorted(set(mapping.values())), args.start, args.end)
        available_etfs = set(bars["etf_symbol"])
        mapping = {index: etf for index, etf in mapping.items() if etf in available_etfs}
        fear = fear[fear["index_symbol"].isin(mapping)].copy()
        bars = bars[bars["etf_symbol"].isin(mapping.values())].copy()
        benchmark = connection.execute(
            """
            SELECT trade_date, close FROM a_stock_index_daily
            WHERE ts_code = '000300.SH' AND trade_date BETWEEN ? AND ?
            ORDER BY trade_date
            """,
            [args.start, args.end],
        ).fetch_df()

    signals = build_signal_rows(
        bars, fear, mapping, args.fear_entry, args.volume_std_multiplier
    )
    curve, trades = run_backtest(
        bars, fear, signals,
        initial_capital=args.initial_capital,
        fear_greed_exit=args.fear_exit,
        no_new_high_days=args.no_new_high_days,
        commission_pct=args.commission_pct,
        slippage_pct=args.slippage_pct,
        stamp_duty_pct=args.stamp_duty_pct,
        lot_size=args.lot_size,
    )
    benchmark["date"] = pd.to_datetime(benchmark["trade_date"]).dt.date.astype(str)
    benchmark["close"] = pd.to_numeric(benchmark["close"], errors="coerce")
    benchmark = benchmark.dropna(subset=["close"])
    benchmark["benchmark_value"] = (
        args.initial_capital * benchmark["close"] / benchmark.iloc[0]["close"]
    )
    curve = curve.merge(benchmark[["date", "benchmark_value"]], on="date", how="left")
    curve["benchmark_value"] = curve["benchmark_value"].ffill()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    curve.to_csv(args.output_dir / "equity_curve.csv", index=False)
    trades.to_csv(args.output_dir / "trades.csv", index=False)
    annual = curve.assign(year=pd.to_datetime(curve["date"]).dt.year).groupby("year").agg(
        start_value=("value", "first"), end_value=("value", "last")
    )
    annual["return_pct"] = (annual["end_value"] / annual["start_value"] - 1) * 100
    annual.to_csv(args.output_dir / "annual_returns.csv")
    payload = {
        "summary": summarize(curve, trades, args.initial_capital),
        "parameters": vars(args)
        | {
            "sqlite": str(args.sqlite),
            "duckdb": str(args.duckdb),
            "output_dir": str(args.output_dir),
        },
        "excluded_indexes": sorted(excluded),
        "index_etf_mapping": mapping,
        "definitions": {
            "entry": "index fear < 25 and ETF volume > prior-20 mean + 1 sample standard deviation",
            "execution": "signal at close, execute next tradable session open",
            "range": "10 consecutive ETF sessions without a new post-entry intraday high",
            "exit_arm": "range state and index fear > 70",
            "exit": "after armed, ETF close >= midpoint of post-entry high and low",
            "candidate_ranking": "volume z-score descending, fear ascending, ETF symbol ascending",
            "positioning": "one ETF maximum, invest all available cash in board lots",
        },
    }
    (args.output_dir / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Validate rule invariants and summary consistency for the component backtest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


DEFAULT_OUTPUT = Path("research/output/a_stock_fear_nine_turn_component_backtest")


def validate(output: Path = DEFAULT_OUTPUT) -> dict[str, object]:
    metadata = json.loads((output / "metadata.json").read_text(encoding="utf-8"))
    signals = pd.read_csv(output / "signal_summary.csv")
    pairs = pd.read_csv(output / "pair_summary.csv")
    trades = pd.read_csv(output / "trades.csv")
    indexes = pd.read_csv(output / "index_summary.csv")

    date_columns = [
        "bottom_signal_date",
        "buy_low9_date",
        "buy_signal_date",
        "buy_date",
        "top_signal_date",
        "sell_signal_date",
        "sell_date",
        "anchor_date",
    ]
    for column in date_columns:
        trades[column] = pd.to_datetime(trades[column], errors="raise")

    checks = {
        "buy_has_prior_low9": bool((trades["buy_low9_date"] < trades["buy_signal_date"]).all()),
        "low9_not_reused": bool(
            ~trades.duplicated(["index_code", "signal_mode", "ts_code", "buy_low9_date"]).any()
        ),
        "buy_executes_after_signal": bool((trades["buy_date"] > trades["buy_signal_date"]).all()),
        "sell_low_count_valid": bool(trades["sell_low_count"].isin([2, 3, 4]).all()),
        "sell_anchor_at_least_high7": bool((trades["anchor_high_count"] >= 7).all()),
        "sell_drawdown_over_2atr": bool((trades["sell_drawdown_atr"] > 2).all()),
        "anchor_precedes_sell": bool((trades["anchor_date"] < trades["sell_signal_date"]).all()),
        "index_top_precedes_sell": bool(
            (trades["top_signal_date"] <= trades["sell_signal_date"]).all()
        ),
        "sell_executes_after_signal": bool((trades["sell_date"] > trades["sell_signal_date"]).all()),
        "pair_key_unique": bool(
            ~pairs.duplicated(["index_code", "signal_mode", "ts_code"]).any()
        ),
        "signal_key_unique": bool(~signals.duplicated(["index_code", "signal_mode"]).any()),
        "trade_count_reconciles": bool(int(pairs["completed_trades"].sum()) == len(trades)),
        "metadata_trade_count_reconciles": bool(metadata["completed_trades"] == len(trades)),
        "metadata_pair_count_reconciles": bool(metadata["index_stock_pairs"] == len(pairs)),
        "metadata_index_count_reconciles": bool(
            metadata["indexes_backtested"] == indexes["index_code"].nunique()
        ),
    }
    failures = [name for name, passed in checks.items() if not passed]
    result = {
        "status": "passed" if not failures else "failed",
        "checks": checks,
        "failures": failures,
        "rows": {
            "signals": len(signals),
            "index_summaries": len(indexes),
            "index_stock_pairs": len(pairs),
            "trades": len(trades),
        },
    }
    if failures:
        raise AssertionError(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    print(json.dumps(validate(args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

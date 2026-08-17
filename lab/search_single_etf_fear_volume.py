#!/usr/bin/env python3
"""Parameter search for a portfolio holding at most one A-share ETF."""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pandas as pd

import a_stock_fear_volume_portfolio_backtest as engine
from a_stock_fear_volume_portfolio_backtest import Params, load_data, run_backtest


START = "2023-03-22"
TRAIN_END = "2025-03-31"
OOS_START = "2025-04-01"
END = "2026-07-23"
OUTPUT = Path("/private/tmp/a_stock_single_etf_parameter_search.json")


def selected_metrics(result: dict) -> dict:
    keys = (
        "total_return",
        "cagr",
        "max_drawdown",
        "sharpe",
        "calmar",
        "trades",
        "sell_trades",
        "average_positions",
        "average_cash_weight",
        "maximum_close_weight_observed",
    )
    return {key: result[key] for key in keys}


def search_for_cap(fear: pd.DataFrame, prices: pd.DataFrame, cap: float) -> dict:
    engine.MAX_POSITIONS = 1
    engine.MAX_WEIGHT = cap
    rows = []
    for values in itertools.product(
        [20.0, 25.0, 30.0, 35.0],
        [1.0, 1.1, 1.3, 1.5],
        [40.0, 50.0, 60.0],
        [65.0, 70.0, 75.0, 80.0],
        [False, True],
    ):
        params = Params(*values)
        train = run_backtest(fear, prices, params, START, TRAIN_END)[0]
        oos = run_backtest(fear, prices, params, OOS_START, END)[0]
        rows.append(
            {
                **params.__dict__,
                "key": params.key,
                "train": selected_metrics(train),
                "oos": selected_metrics(oos),
            }
        )

    flat = pd.DataFrame(
        [
            {
                **{key: row[key] for key in Params.__dataclass_fields__},
                "key": row["key"],
                **{f"train_{key}": value for key, value in row["train"].items()},
                **{f"oos_{key}": value for key, value in row["oos"].items()},
            }
            for row in rows
        ]
    )
    eligible = flat[flat["train_sell_trades"] >= 6].copy()
    eligible["train_score"] = (
        eligible["train_calmar"].rank(pct=True)
        + eligible["train_sharpe"].rank(pct=True)
        + eligible["train_cagr"].rank(pct=True)
    )
    selected = eligible.sort_values(
        ["train_score", "train_calmar", "train_sharpe"], ascending=False
    ).head(20)

    selected_results = []
    for _, row in selected.iterrows():
        params = Params(
            buy_fear=float(row["buy_fear"]),
            volume_ratio=float(row["volume_ratio"]),
            rotate_fear=float(row["rotate_fear"]),
            cash_fear=float(row["cash_fear"]),
            require_rebound=bool(row["require_rebound"]),
        )
        full = run_backtest(fear, prices, params, START, END)[0]
        selected_results.append(
            {
                "params": params.__dict__,
                "train": {
                    key.removeprefix("train_"): row[key]
                    for key in row.index
                    if key.startswith("train_") and key != "train_score"
                },
                "oos": {
                    key.removeprefix("oos_"): row[key]
                    for key in row.index
                    if key.startswith("oos_")
                },
                "full": selected_metrics(full),
            }
        )

    # Summarize how the train-selected family behaves out of sample.
    top = selected.head(20)
    return {
        "weight_cap": cap,
        "grid_size": len(rows),
        "train_selected_oos_summary": {
            "positive_rate": float((top["oos_total_return"] > 0).mean()),
            "median_cagr": float(top["oos_cagr"].median()),
            "min_cagr": float(top["oos_cagr"].min()),
            "median_max_drawdown": float(top["oos_max_drawdown"].median()),
            "worst_max_drawdown": float(top["oos_max_drawdown"].min()),
            "median_sharpe": float(top["oos_sharpe"].median()),
        },
        "top_train_selected": selected_results,
    }


def main() -> None:
    fear, prices = load_data("2023-01-01", END)
    payload = {
        "periods": {
            "train": [START, TRAIN_END],
            "oos": [OOS_START, END],
            "full": [START, END],
        },
        "grid": {
            "buy_fear": [20.0, 25.0, 30.0, 35.0],
            "volume_ratio": [1.0, 1.1, 1.3, 1.5],
            "rotate_fear": [40.0, 50.0, 60.0],
            "cash_fear": [65.0, 70.0, 75.0, 80.0],
            "require_rebound": [False, True],
        },
        "results": [
            search_for_cap(fear, prices, 0.50),
            search_for_cap(fear, prices, 1.00),
        ],
    }
    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

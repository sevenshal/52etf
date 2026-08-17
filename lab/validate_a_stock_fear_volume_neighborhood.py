#!/usr/bin/env python3
"""Focused robustness validation around fear=30 and volume-ratio=1.3."""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import numpy as np
import pandas as pd

from a_stock_fear_volume_portfolio_backtest import Params, load_data, run_backtest


START = "2023-03-22"
TRAIN_END = "2025-03-31"
OOS_START = "2025-04-01"
END = "2026-07-23"
OUTPUT = Path("/private/tmp/a_stock_fear_volume_neighborhood.json")


def compact(metrics: dict) -> dict:
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
    )
    return {key: metrics[key] for key in keys}


def main() -> None:
    fear, prices = load_data("2023-01-01", END)
    rows = []
    for values in itertools.product(
        [27.5, 30.0, 32.5],
        [1.2, 1.3, 1.4],
        [50.0, 60.0],
        [70.0, 75.0, 80.0],
        [False, True],
    ):
        params = Params(*values)
        train = run_backtest(fear, prices, params, START, TRAIN_END)[0]
        oos = run_backtest(fear, prices, params, OOS_START, END)[0]
        full = run_backtest(fear, prices, params, START, END)[0]
        rows.append(
            {
                **params.__dict__,
                "key": params.key,
                "train": compact(train),
                "oos": compact(oos),
                "full": compact(full),
            }
        )

    flat = pd.DataFrame(
        [
            {
                "buy_fear": row["buy_fear"],
                "volume_ratio": row["volume_ratio"],
                "rotate_fear": row["rotate_fear"],
                "cash_fear": row["cash_fear"],
                "require_rebound": row["require_rebound"],
                **{f"train_{key}": value for key, value in row["train"].items()},
                **{f"oos_{key}": value for key, value in row["oos"].items()},
                **{f"full_{key}": value for key, value in row["full"].items()},
            }
            for row in rows
        ]
    )

    pair_summaries = []
    for (buy_fear, volume_ratio), group in flat.groupby(
        ["buy_fear", "volume_ratio"], sort=True
    ):
        pair_summaries.append(
            {
                "buy_fear": float(buy_fear),
                "volume_ratio": float(volume_ratio),
                "variants": int(len(group)),
                "train_median_cagr": float(group["train_cagr"].median()),
                "train_median_calmar": float(group["train_calmar"].median()),
                "oos_median_cagr": float(group["oos_cagr"].median()),
                "oos_min_cagr": float(group["oos_cagr"].min()),
                "oos_positive_rate": float((group["oos_total_return"] > 0).mean()),
                "oos_median_max_drawdown": float(
                    group["oos_max_drawdown"].median()
                ),
                "oos_worst_max_drawdown": float(group["oos_max_drawdown"].min()),
                "oos_median_sharpe": float(group["oos_sharpe"].median()),
                "full_median_cagr": float(group["full_cagr"].median()),
                "full_median_calmar": float(group["full_calmar"].median()),
                "full_median_cash_weight": float(
                    group["full_average_cash_weight"].median()
                ),
            }
        )

    # Select only on training data, then report untouched OOS performance.
    eligible = flat[flat["train_sell_trades"] >= 6].copy()
    eligible["train_rank"] = (
        eligible["train_calmar"].rank(pct=True)
        + eligible["train_sharpe"].rank(pct=True)
        + eligible["train_cagr"].rank(pct=True)
    )
    chosen = eligible.sort_values(
        ["train_rank", "train_calmar", "train_sharpe"], ascending=False
    ).head(10)
    train_selected_oos = []
    for _, row in chosen.iterrows():
        train_selected_oos.append(
            {
                "params": {
                    "buy_fear": float(row["buy_fear"]),
                    "volume_ratio": float(row["volume_ratio"]),
                    "rotate_fear": float(row["rotate_fear"]),
                    "cash_fear": float(row["cash_fear"]),
                    "require_rebound": bool(row["require_rebound"]),
                },
                "train": {
                    "cagr": float(row["train_cagr"]),
                    "max_drawdown": float(row["train_max_drawdown"]),
                    "sharpe": float(row["train_sharpe"]),
                    "calmar": float(row["train_calmar"]),
                },
                "oos": {
                    "cagr": float(row["oos_cagr"]),
                    "max_drawdown": float(row["oos_max_drawdown"]),
                    "sharpe": float(row["oos_sharpe"]),
                    "calmar": float(row["oos_calmar"]),
                    "average_cash_weight": float(row["oos_average_cash_weight"]),
                },
                "full": {
                    "cagr": float(row["full_cagr"]),
                    "max_drawdown": float(row["full_max_drawdown"]),
                    "sharpe": float(row["full_sharpe"]),
                    "calmar": float(row["full_calmar"]),
                },
            }
        )

    payload = {
        "periods": {
            "full": [START, END],
            "train": [START, TRAIN_END],
            "oos": [OOS_START, END],
        },
        "grid": {
            "buy_fear": [27.5, 30.0, 32.5],
            "volume_ratio": [1.2, 1.3, 1.4],
            "rotate_fear": [50.0, 60.0],
            "cash_fear": [70.0, 75.0, 80.0],
            "require_rebound": [False, True],
            "variants": len(rows),
        },
        "pair_summaries": pair_summaries,
        "train_selected_oos": train_selected_oos,
        "raw": rows,
    }
    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

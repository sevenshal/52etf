#!/usr/bin/env python3
"""Validation cuts for the support/resistance study output."""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


def clustered_summary(events: pd.DataFrame) -> pd.DataFrame:
    rows = []
    keys = ["split", "method", "direction"]
    for group_key, group in events.groupby(keys, observed=True):
        stock = group.groupby("ts_code", observed=True).agg(
            mean_return_20d=("return_20d", "mean"),
            win_rate=("win_before_loss", "mean"),
            retention=("retained_3d", "mean"),
        )
        rows.append(
            {
                **dict(zip(keys, group_key)),
                "signals": len(group),
                "stocks": len(stock),
                "stock_mean_return_20d": stock["mean_return_20d"].mean(),
                "stock_return_p025": stock["mean_return_20d"].quantile(0.025),
                "stock_return_p975": stock["mean_return_20d"].quantile(0.975),
                "stocks_positive_return_share": (stock["mean_return_20d"] > 0).mean(),
                "stock_mean_win_rate": stock["win_rate"].mean(),
                "stocks_win_rate_above_one_third": (stock["win_rate"] > 1 / 3).mean(),
                "stock_mean_retention": stock["retention"].mean(),
            }
        )
    return pd.DataFrame(rows)


def retention_cut(events: pd.DataFrame) -> pd.DataFrame:
    return (
        events.groupby(["split", "method", "direction", "retained_3d"], observed=True)
        .agg(
            signals=("ts_code", "size"),
            win_rate=("win_before_loss", "mean"),
            median_return_20d=("return_20d", "median"),
            mean_return_20d=("return_20d", "mean"),
            confirmed_win_rate=("confirmed_win_before_loss", "mean"),
            confirmed_median_return_20d=("confirmed_return_20d", "median"),
            confirmed_mean_return_20d=("confirmed_return_20d", "mean"),
        )
        .reset_index()
    )


def yearly_cut(events: pd.DataFrame) -> pd.DataFrame:
    events = events.copy()
    events["year"] = pd.to_datetime(events["trade_date"]).dt.year
    return (
        events.groupby(["year", "method", "direction"], observed=True)
        .agg(
            signals=("ts_code", "size"),
            stocks=("ts_code", "nunique"),
            win_rate=("win_before_loss", "mean"),
            median_return_20d=("return_20d", "median"),
            mean_return_20d=("return_20d", "mean"),
        )
        .reset_index()
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="research/output/support_resistance/events.csv")
    args = parser.parse_args()
    source = Path(args.input)
    output = source.parent
    events = pd.read_csv(source)
    clustered_summary(events).to_csv(output / "cluster_validation.csv", index=False)
    retention_cut(events).to_csv(output / "retention_validation.csv", index=False)
    yearly_cut(events).to_csv(output / "year_validation.csv", index=False)
    print(clustered_summary(events).to_string(index=False))
    print("\nSample-out retention confirmation:\n")
    test = retention_cut(events).query("split == 'test'")
    print(test.to_string(index=False))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Cross-index, date-clustered validation for nine-turn breadth events."""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", default="research/output/nine_turn_breadth")
    args = parser.parse_args()
    directory = Path(args.directory)
    events = pd.read_csv(directory / "events.csv", parse_dates=["trade_date"])
    breadth = pd.read_csv(directory / "breadth_daily.csv", parse_dates=["trade_date"])
    rows = []
    yearly_rows = []
    index_rows = []
    for (metric, side), group in events.groupby(["metric", "side"], observed=True):
        clustered = group.groupby("trade_date", observed=True)["signed_forward_20d"].mean().dropna()
        statistic = stats.ttest_1samp(clustered, 0, nan_policy="omit")
        rows.append({
            "metric": metric,
            "side": side,
            "events": len(group),
            "unique_dates": len(clustered),
            "date_cluster_mean_20d": clustered.mean(),
            "date_cluster_median_20d": clustered.median(),
            "date_cluster_positive_share": (clustered > 0).mean(),
            "t_stat_vs_zero": statistic.statistic,
            "p_value_vs_zero": statistic.pvalue,
        })
        temp = group.copy()
        temp["year"] = temp["trade_date"].dt.year
        for year, year_group in temp.groupby("year", observed=True):
            date_values = year_group.groupby("trade_date")["signed_forward_20d"].mean()
            yearly_rows.append({
                "metric": metric, "side": side, "year": year,
                "events": len(year_group), "unique_dates": len(date_values),
                "mean_20d": date_values.mean(), "positive_share": (date_values > 0).mean(),
            })
        for code, index_group in group.groupby("index_code", observed=True):
            panel = breadth.loc[breadth["index_code"] == code].sort_values("trade_date").copy()
            panel["past20"] = panel["index_close"] / panel["index_close"].shift(20) - 1
            panel["forward20"] = panel["index_close"].shift(-20) / panel["index_close"] - 1
            baseline = panel.loc[panel["past20"] <= -0.03, "forward20"] if side == "low" else -panel.loc[panel["past20"] >= 0.03, "forward20"]
            event_mean = index_group["signed_forward_20d"].mean()
            index_rows.append({
                "index_code": code, "metric": metric, "side": side,
                "events": len(index_group), "event_mean_20d": event_mean,
                "baseline_mean_20d": baseline.mean(),
                "excess_20d": event_mean - baseline.mean(),
            })
    clustered_summary = pd.DataFrame(rows)
    yearly = pd.DataFrame(yearly_rows)
    by_index = pd.DataFrame(index_rows)
    clustered_summary.to_csv(directory / "date_cluster_validation.csv", index=False)
    yearly.to_csv(directory / "year_validation.csv", index=False)
    by_index.to_csv(directory / "index_excess_validation.csv", index=False)
    print(clustered_summary.to_string(index=False))
    print("\nIndex breadth of positive excess:\n")
    print(by_index.groupby(["metric", "side"]).agg(indices=("index_code", "size"), positive_excess_share=("excess_20d", lambda x: (x > 0).mean()), median_excess=("excess_20d", "median")).reset_index().to_string(index=False))


if __name__ == "__main__":
    main()

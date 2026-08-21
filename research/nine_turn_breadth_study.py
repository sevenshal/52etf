#!/usr/bin/env python3
"""Point-in-time A-share index breadth study for the simplified nine-turn setup.

Definition: a low (high) setup increments while close is below (above) the
close four trading days earlier.  ``exact9`` fires on the ninth consecutive
day; ``stage7_9`` covers stages seven through nine.  Historical index members
are resolved from the latest constituent snapshot available on each date.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd


DEFAULT_DB = "/home/quantd/quant_prod/quant_robot/analytics.duckdb"


BREADTH_SQL = r"""
WITH stock_lag AS (
  SELECT ts_code, trade_date, close,
         lag(close, 4) OVER (PARTITION BY ts_code ORDER BY trade_date) AS close_lag4
  FROM a_stock_market_daily_qfq
  WHERE trade_date BETWEEN DATE '2019-01-01' AND DATE '2026-08-20'
), flags AS (
  SELECT *, close < close_lag4 AS low_flag, close > close_lag4 AS high_flag
  FROM stock_lag
), islands AS (
  SELECT *,
    sum(CASE WHEN low_flag THEN 0 ELSE 1 END) OVER (PARTITION BY ts_code ORDER BY trade_date) AS low_group,
    sum(CASE WHEN high_flag THEN 0 ELSE 1 END) OVER (PARTITION BY ts_code ORDER BY trade_date) AS high_group
  FROM flags
), setups AS (
  SELECT ts_code, trade_date,
    CASE WHEN low_flag THEN sum(low_flag::INT) OVER (PARTITION BY ts_code, low_group ORDER BY trade_date) ELSE 0 END AS low_count,
    CASE WHEN high_flag THEN sum(high_flag::INT) OVER (PARTITION BY ts_code, high_group ORDER BY trade_date) ELSE 0 END AS high_count
  FROM islands
), index_dates AS (
  SELECT d.ts_code AS index_code, d.trade_date, d.close
  FROM a_stock_index_daily d
  JOIN (SELECT DISTINCT index_code FROM a_stock_index_weight) x ON x.index_code = d.ts_code
  WHERE d.trade_date BETWEEN DATE '2020-01-01' AND DATE '2026-08-20'
), mapped_dates AS (
  SELECT d.index_code, d.trade_date, d.close, max(w.trade_date) AS snapshot_date
  FROM index_dates d
  JOIN (SELECT DISTINCT index_code, trade_date FROM a_stock_index_weight) w
    ON w.index_code = d.index_code AND w.trade_date <= d.trade_date
  GROUP BY d.index_code, d.trade_date, d.close
), member_day AS (
  SELECT d.index_code, d.trade_date, d.close AS index_close,
         w.con_code, w.weight, s.low_count, s.high_count
  FROM mapped_dates d
  JOIN a_stock_index_weight w
    ON w.index_code = d.index_code AND w.trade_date = d.snapshot_date
  JOIN setups s ON s.ts_code = w.con_code AND s.trade_date = d.trade_date
)
SELECT index_code, trade_date, any_value(index_close) AS index_close,
       count(*) AS eligible_members,
       avg((low_count = 9)::INT) AS low9_share,
       avg((high_count = 9)::INT) AS high9_share,
       avg((low_count BETWEEN 7 AND 9)::INT) AS low7_9_share,
       avg((high_count BETWEEN 7 AND 9)::INT) AS high7_9_share,
       sum(weight * (low_count = 9)::INT) / nullif(sum(weight), 0) AS low9_weighted,
       sum(weight * (high_count = 9)::INT) / nullif(sum(weight), 0) AS high9_weighted,
       sum(weight * (low_count BETWEEN 7 AND 9)::INT) / nullif(sum(weight), 0) AS low7_9_weighted,
       sum(weight * (high_count BETWEEN 7 AND 9)::INT) / nullif(sum(weight), 0) AS high7_9_weighted
FROM member_day
GROUP BY index_code, trade_date
ORDER BY index_code, trade_date
"""


def add_outcomes(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.sort_values("trade_date").copy()
    frame["past20_return"] = frame["index_close"] / frame["index_close"].shift(20) - 1
    for horizon in (5, 10, 20):
        frame[f"forward_{horizon}d"] = frame["index_close"].shift(-horizon) / frame["index_close"] - 1
    return frame


def expanding_threshold_signal(series: pd.Series, percentile: float = 0.9) -> pd.Series:
    threshold = series.shift(1).rolling(252, min_periods=126).quantile(percentile)
    return (series > 0) & (series >= threshold) & (series.shift(1) < threshold.shift(1))


def select_events(frame: pd.DataFrame, metric: str, side: str, cooldown: int = 10) -> pd.DataFrame:
    candidates = expanding_threshold_signal(frame[metric])
    trend = frame["past20_return"] <= -0.03 if side == "low" else frame["past20_return"] >= 0.03
    selected = candidates & trend
    keep = np.zeros(len(frame), dtype=bool)
    last = -999
    for index in np.where(selected.to_numpy())[0]:
        if index - last >= cooldown:
            keep[index] = True
            last = index
    result = frame.loc[keep].copy()
    result["metric"] = metric
    result["side"] = side
    result["signed_forward_5d"] = result["forward_5d"] if side == "low" else -result["forward_5d"]
    result["signed_forward_10d"] = result["forward_10d"] if side == "low" else -result["forward_10d"]
    result["signed_forward_20d"] = result["forward_20d"] if side == "low" else -result["forward_20d"]
    return result


def matched_baseline(frame: pd.DataFrame, side: str) -> pd.DataFrame:
    trend = frame["past20_return"] <= -0.03 if side == "low" else frame["past20_return"] >= 0.03
    result = frame.loc[trend].copy()
    for horizon in (5, 10, 20):
        raw = result[f"forward_{horizon}d"]
        result[f"signed_forward_{horizon}d"] = raw if side == "low" else -raw
    return result


def summarize(events: pd.DataFrame, panels: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for (metric, side), group in events.groupby(["metric", "side"], observed=True):
        baseline_parts = [matched_baseline(panel, side) for panel in panels.values()]
        baseline = pd.concat(baseline_parts, ignore_index=True)
        row = {"metric": metric, "side": side, "events": len(group), "indices": group["index_code"].nunique()}
        for horizon in (5, 10, 20):
            field = f"signed_forward_{horizon}d"
            row[f"event_mean_{horizon}d"] = group[field].mean()
            row[f"event_median_{horizon}d"] = group[field].median()
            row[f"event_positive_{horizon}d"] = (group[field] > 0).mean()
            row[f"baseline_mean_{horizon}d"] = baseline[field].mean()
            row[f"excess_vs_baseline_{horizon}d"] = group[field].mean() - baseline[field].mean()
        rows.append(row)
    return pd.DataFrame(rows)


def by_index(events: pd.DataFrame) -> pd.DataFrame:
    return (
        events.groupby(["index_code", "metric", "side"], observed=True)
        .agg(
            events=("trade_date", "size"),
            mean_5d=("signed_forward_5d", "mean"),
            mean_10d=("signed_forward_10d", "mean"),
            mean_20d=("signed_forward_20d", "mean"),
            positive_20d=("signed_forward_20d", lambda value: (value > 0).mean()),
        )
        .reset_index()
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=DEFAULT_DB)
    parser.add_argument("--output", default="research/output/nine_turn_breadth")
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(args.database, read_only=True) as connection:
        breadth = connection.sql(BREADTH_SQL).fetchdf()
    panels = {
        code: add_outcomes(group.reset_index(drop=True))
        for code, group in breadth.groupby("index_code", sort=True)
        if len(group) >= 500
    }
    event_parts = []
    for code, panel in panels.items():
        for metric in ("low9_share", "high9_share", "low7_9_share", "high7_9_share", "low7_9_weighted", "high7_9_weighted"):
            side = "low" if metric.startswith("low") else "high"
            selected = select_events(panel, metric, side)
            selected["index_code"] = code
            event_parts.append(selected)
    events = pd.concat(event_parts, ignore_index=True).dropna(subset=["signed_forward_20d"])
    summary = summarize(events, panels)
    breadth.to_csv(output / "breadth_daily.csv", index=False)
    events.to_csv(output / "events.csv", index=False)
    summary.to_csv(output / "summary.csv", index=False)
    by_index(events).to_csv(output / "by_index.csv", index=False)
    metadata = {
        "source": args.database,
        "indices_with_500_days": len(panels),
        "breadth_rows": len(breadth),
        "events": len(events),
        "definition": "close vs close four trading days earlier; exact ninth or stages seven through nine",
        "signal": "cross above trailing 252-day 90th percentile after a prior 20-day index move of at least 3%; 10-day cooldown",
    }
    (output / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(metadata, ensure_ascii=False, indent=2))
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()

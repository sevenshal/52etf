#!/usr/bin/env python3
"""Validate nine-turn low-N sell thresholds with forward competing price barriers."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from nine_turn_atr_sell_study import DEFAULT_DATABASE, _run_lengths


LOW_COUNTS = tuple(range(2, 7))
THRESHOLDS = tuple(np.arange(0.0, 3.01, 0.25).round(2))
HORIZON = 20
DOWN_BARRIER_ATR = 1.5
UP_BARRIER_ATR = 1.0


def _classify_path(stock: pd.DataFrame, candidate_indices: np.ndarray) -> pd.DataFrame:
    highs = stock["high"].to_numpy(float)
    lows = stock["low"].to_numpy(float)
    closes = stock["close"].to_numpy(float)
    opens = stock["open"].to_numpy(float)
    atr = stock["atr14"].to_numpy(float)
    anchors = stock["anchor_close"].to_numpy(float)
    rows: list[dict] = []
    for index in candidate_indices:
        if index + HORIZON >= len(stock) or not np.isfinite(atr[index]):
            continue
        future_slice = slice(index + 1, index + HORIZON + 1)
        lower_barrier = closes[index] - DOWN_BARRIER_ATR * atr[index]
        upper_barrier = min(anchors[index], closes[index] + UP_BARRIER_ATR * atr[index])
        lower_hits = np.flatnonzero(lows[future_slice] <= lower_barrier)
        upper_hits = np.flatnonzero(highs[future_slice] >= upper_barrier)
        lower_day = int(lower_hits[0] + 1) if len(lower_hits) else None
        upper_day = int(upper_hits[0] + 1) if len(upper_hits) else None
        if lower_day is not None and (upper_day is None or lower_day < upper_day):
            label = "真卖点"
        elif upper_day is not None and (lower_day is None or upper_day < lower_day):
            label = "假卖点"
        else:
            label = "未确认"
        next_open = opens[index + 1]
        result = {
            "row_index": int(index),
            "path_label": label,
            "lower_hit_day": lower_day,
            "upper_hit_day": upper_day,
            "next_open": next_open,
            "future_min_return_20d": lows[future_slice].min() / closes[index] - 1,
            "future_max_return_20d": highs[future_slice].max() / closes[index] - 1,
        }
        for hold in (5, 10, 20):
            future_close = closes[index + hold]
            result[f"sell_advantage_{hold}d"] = next_open / future_close - 1
        rows.append(result)
    return pd.DataFrame(rows).set_index("row_index") if rows else pd.DataFrame()


def analyze(database: str = DEFAULT_DATABASE, output: str | Path | None = None) -> dict:
    connection = duckdb.connect(database, read_only=True)
    end_date = connection.execute("SELECT max(trade_date) FROM a_stock_market_daily").fetchone()[0]
    sql = """
    WITH eligible AS (
        SELECT m.ts_code, b.name
        FROM a_stock_market_daily m JOIN a_stock_basic b USING (ts_code)
        WHERE m.trade_date = ? AND b.list_status = 'L'
          AND m.total_mv >= 500000 AND m.amount >= 50000 AND m.vol > 0
          AND upper(coalesce(b.name, '')) NOT LIKE '%ST%'
          AND NOT EXISTS (
              SELECT 1 FROM a_stock_name_changes nc
              WHERE nc.ts_code = m.ts_code
                AND upper(coalesce(nc.name, '')) LIKE '%ST%'
                AND coalesce(nc.start_date, DATE '1900-01-01') <= ?
                AND coalesce(nc.end_date, DATE '2999-12-31') >= DATE '2023-01-01'
          )
    )
    SELECT q.ts_code, e.name, q.trade_date, q.open, q.high, q.low, q.close, q.vol
    FROM a_stock_market_daily_qfq q JOIN eligible e USING (ts_code)
    WHERE q.trade_date BETWEEN DATE '2022-10-01' AND ?
      AND q.open > 0 AND q.close > 0 AND q.high > 0 AND q.low > 0 AND q.vol > 0
    ORDER BY q.ts_code, q.trade_date
    """
    prices = connection.execute(sql, [end_date, end_date, end_date]).df()
    connection.close()

    event_parts: list[pd.DataFrame] = []
    for _, stock in prices.groupby("ts_code", sort=False):
        stock = stock.sort_values("trade_date").reset_index(drop=True).copy()
        lag4 = stock["close"].shift(4)
        stock["high_count"] = _run_lengths(stock["close"] > lag4)
        stock["low_count"] = _run_lengths(stock["close"] < lag4)
        previous_close = stock["close"].shift(1)
        stock["true_range"] = pd.concat(
            [stock["high"] - stock["low"], (stock["high"] - previous_close).abs(), (stock["low"] - previous_close).abs()],
            axis=1,
        ).max(axis=1)
        stock["atr14"] = stock["true_range"].rolling(14, min_periods=14).mean()
        anchor = stock["high_count"] >= 9
        stock["anchor_close"] = stock["close"].where(anchor).ffill()
        stock["anchor_date"] = stock["trade_date"].where(anchor).ffill()
        candidate_mask = (
            (stock["trade_date"] >= pd.Timestamp("2023-01-01"))
            & stock["low_count"].isin(LOW_COUNTS)
            & stock["anchor_close"].notna()
            & (stock["anchor_date"] < stock["trade_date"])
            & stock["atr14"].notna()
        )
        candidate_indices = stock.index[candidate_mask].to_numpy()
        if not len(candidate_indices):
            continue
        path = _classify_path(stock, candidate_indices)
        if path.empty:
            continue
        candidates = stock.loc[path.index].join(path).copy()
        candidates["drawdown_atr"] = (candidates["anchor_close"] - candidates["close"]) / candidates["atr14"]
        candidates["atr_pct"] = candidates["atr14"] / candidates["close"]
        event_parts.append(candidates)

    events = pd.concat(event_parts, ignore_index=True)
    events["year"] = pd.to_datetime(events["trade_date"]).dt.year
    events["cycle_id"] = events["ts_code"] + "|" + events["anchor_date"].astype(str)

    threshold_rows: list[dict] = []
    selected_parts: list[pd.DataFrame] = []
    for threshold in THRESHOLDS:
        eligible = events.loc[events["drawdown_atr"] >= threshold].copy()
        selected = (
            eligible.sort_values(["ts_code", "anchor_date", "trade_date", "low_count"])
            .drop_duplicates("cycle_id", keep="first")
            .copy()
        )
        selected["threshold_atr"] = threshold
        selected_parts.append(selected)
        resolved = selected["path_label"].isin(["真卖点", "假卖点"])
        true_rate = (selected.loc[resolved, "path_label"] == "真卖点").mean() if resolved.any() else np.nan
        threshold_rows.append({
            "threshold_atr": threshold,
            "cycles_signaled": len(selected),
            "resolved_pct": resolved.mean() * 100,
            "true_rate_pct": true_rate * 100,
            "median_low_count": selected["low_count"].median(),
            "median_drawdown_atr": selected["drawdown_atr"].median(),
            **{f"mean_sell_advantage_{hold}d_pct": selected[f"sell_advantage_{hold}d"].mean() * 100 for hold in (5, 10, 20)},
            **{f"median_sell_advantage_{hold}d_pct": selected[f"sell_advantage_{hold}d"].median() * 100 for hold in (5, 10, 20)},
        })
    thresholds = pd.DataFrame(threshold_rows)
    selections = pd.concat(selected_parts, ignore_index=True)
    by_low_count = (
        events.groupby(["low_count", "path_label"], observed=True)
        .agg(events=("ts_code", "size"), stocks=("ts_code", "nunique"), median_drawdown_atr=("drawdown_atr", "median"))
        .reset_index()
    )
    by_year = (
        selections.loc[selections["threshold_atr"].isin([0.5, 1.0, 1.5])]
        .groupby(["threshold_atr", "year"], observed=True)
        .agg(
            cycles=("cycle_id", "size"),
            true_rate_pct=("path_label", lambda x: (x[x.isin(["真卖点", "假卖点"])] == "真卖点").mean() * 100),
            mean_sell_advantage_20d_pct=("sell_advantage_20d", lambda x: x.mean() * 100),
        )
        .reset_index()
    )
    best_row = thresholds.loc[thresholds["mean_sell_advantage_20d_pct"].idxmax()].to_dict()
    metadata = {
        "source": database,
        "start_date": "2023-01-01",
        "end_date": str(end_date),
        "candidate_events": len(events),
        "cycles": events["cycle_id"].nunique(),
        "candidate_low_counts": list(LOW_COUNTS),
        "future_horizon_days": HORIZON,
        "true_barrier_atr": DOWN_BARRIER_ATR,
        "false_recovery_barrier_atr": UP_BARRIER_ATR,
        "best_by_mean_20d_sell_advantage": best_row,
    }
    result = {"events": events, "thresholds": thresholds, "selections": selections, "by_low_count": by_low_count, "by_year": by_year, "metadata": metadata}
    if output is not None:
        destination = Path(output)
        destination.mkdir(parents=True, exist_ok=True)
        events.to_csv(destination / "path_labeled_events.csv", index=False)
        thresholds.to_csv(destination / "threshold_summary.csv", index=False)
        selections.to_csv(destination / "threshold_selections.csv", index=False)
        by_low_count.to_csv(destination / "by_low_count.csv", index=False)
        by_year.to_csv(destination / "by_year.csv", index=False)
        (destination / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--output", default="research/output/nine_turn_atr_path_validation")
    args = parser.parse_args()
    result = analyze(args.database, args.output)
    print(json.dumps(result["metadata"], ensure_ascii=False, indent=2, default=str))
    print(result["thresholds"].to_string(index=False))
    print(result["by_low_count"].to_string(index=False))


if __name__ == "__main__":
    main()

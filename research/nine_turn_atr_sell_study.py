#!/usr/bin/env python3
"""Classify A-share nine-turn sell candidates and calibrate ATR drawdown thresholds."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from scipy import stats


DEFAULT_DATABASE = "/home/quantd/quant_prod/quant_robot/analytics.duckdb"


def _run_lengths(flag: pd.Series) -> pd.Series:
    groups = (~flag).cumsum()
    return flag.astype(int).groupby(groups).cumsum().where(flag, 0).astype(int)


def _strict_center_extreme(close: pd.Series, side: str, wing: int = 20) -> pd.Series:
    previous = close.shift(1).rolling(wing, min_periods=wing)
    following = close.shift(-1).iloc[::-1].rolling(wing, min_periods=wing).max().iloc[::-1]
    if side == "high":
        return (close > previous.max()) & (close > following)
    following = close.shift(-1).iloc[::-1].rolling(wing, min_periods=wing).min().iloc[::-1]
    return (close < previous.min()) & (close < following)


def _best_threshold(events: pd.DataFrame) -> dict:
    usable = events.dropna(subset=["drawdown_atr", "is_true_sell"]).copy()
    y = usable["is_true_sell"].astype(int).to_numpy()
    score = usable["drawdown_atr"].to_numpy()
    if len(np.unique(y)) < 2:
        return {}
    positives = y == 1
    negatives = ~positives
    order = np.argsort(score, kind="mergesort")
    sorted_score = score[order]
    sorted_positive = positives[order]
    last_for_value = np.r_[sorted_score[1:] != sorted_score[:-1], True]
    thresholds = sorted_score[last_for_value]
    true_positives = np.cumsum(sorted_positive)[last_for_value]
    false_positives = np.cumsum(~sorted_positive)[last_for_value]
    sensitivities = true_positives / positives.sum()
    specificities = 1 - false_positives / negatives.sum()
    best = int(np.nanargmax(sensitivities + specificities - 1))
    threshold = float(thresholds[best])
    predicted = score <= threshold
    ranks = stats.rankdata(score)
    positive_rank_sum = ranks[positives].sum()
    positive_count = positives.sum()
    negative_count = negatives.sum()
    auc = (positive_rank_sum - positive_count * (positive_count + 1) / 2) / (positive_count * negative_count)
    return {
        "roc_auc_if_larger_drawdown_means_true": float(auc),
        "roc_auc_if_smaller_drawdown_means_true": float(1 - auc),
        "best_maximum_drawdown_atr": threshold,
        "sensitivity_pct": float((predicted[y == 1]).mean() * 100),
        "specificity_pct": float((~predicted[y == 0]).mean() * 100),
        "precision_pct": float(y[predicted].mean() * 100) if predicted.any() else None,
    }


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
    SELECT q.ts_code, e.name, q.trade_date, q.high, q.low, q.close, q.vol
    FROM a_stock_market_daily_qfq q JOIN eligible e USING (ts_code)
    WHERE q.trade_date BETWEEN DATE '2022-10-01' AND ?
      AND q.close > 0 AND q.high > 0 AND q.low > 0 AND q.vol > 0
    ORDER BY q.ts_code, q.trade_date
    """
    prices = connection.execute(sql, [end_date, end_date, end_date]).df()
    connection.close()

    event_parts: list[pd.DataFrame] = []
    turning_parts: list[pd.DataFrame] = []
    for ts_code, stock in prices.groupby("ts_code", sort=False):
        stock = stock.sort_values("trade_date").reset_index(drop=True).copy()
        stock["close_lag4"] = stock["close"].shift(4)
        stock["high_count"] = _run_lengths(stock["close"] > stock["close_lag4"])
        stock["low_count"] = _run_lengths(stock["close"] < stock["close_lag4"])
        previous_close = stock["close"].shift(1)
        stock["true_range"] = pd.concat(
            [
                stock["high"] - stock["low"],
                (stock["high"] - previous_close).abs(),
                (stock["low"] - previous_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        stock["atr14"] = stock["true_range"].rolling(14, min_periods=14).mean()
        stock["atr_pct"] = stock["atr14"] / stock["close"]
        stock["is_local_high"] = _strict_center_extreme(stock["close"], "high")
        stock["is_local_low"] = _strict_center_extreme(stock["close"], "low")

        anchor = stock["high_count"] >= 9
        stock["anchor_close"] = stock["close"].where(anchor).ffill()
        stock["anchor_date"] = stock["trade_date"].where(anchor).ffill()
        stock["anchor_high_count"] = stock["high_count"].where(anchor).ffill()
        candidates = stock.loc[
            (stock["trade_date"] >= pd.Timestamp("2023-01-01"))
            & stock["low_count"].between(2, 9)
            & stock["anchor_close"].notna()
            & (stock["anchor_date"] < stock["trade_date"])
            & stock["atr14"].notna()
        ].copy()
        if candidates.empty:
            continue

        candidates["drawdown_pct"] = (candidates["anchor_close"] - candidates["close"]) / candidates["anchor_close"]
        candidates["drawdown_atr"] = candidates["drawdown_pct"] / candidates["atr_pct"]
        candidates["is_true_sell"] = False

        high_indices = stock.index[
            stock["is_local_high"] & (stock["trade_date"] >= pd.Timestamp("2023-01-01"))
        ]
        candidate_low2 = candidates.loc[candidates["low_count"] == 2]
        for high_index in high_indices:
            after = candidate_low2.index[candidate_low2.index > high_index]
            if len(after):
                candidates.loc[after[0], "is_true_sell"] = True

        candidates["label"] = np.where(candidates["is_true_sell"], "真卖点", "假卖点")
        event_parts.append(candidates)
        turning_parts.append(
            stock.loc[stock["is_local_high"] | stock["is_local_low"], [
                "ts_code", "name", "trade_date", "close", "is_local_high", "is_local_low"
            ]]
        )

    events = pd.concat(event_parts, ignore_index=True)
    turns = pd.concat(turning_parts, ignore_index=True)
    summary = (
        events.groupby(["label", "low_count"], observed=True)
        .agg(
            events=("ts_code", "size"),
            stocks=("ts_code", "nunique"),
            median_drawdown_pct=("drawdown_pct", "median"),
            p25_drawdown_atr=("drawdown_atr", lambda x: x.quantile(0.25)),
            median_drawdown_atr=("drawdown_atr", "median"),
            p75_drawdown_atr=("drawdown_atr", lambda x: x.quantile(0.75)),
            p90_drawdown_atr=("drawdown_atr", lambda x: x.quantile(0.90)),
            median_atr_pct=("atr_pct", "median"),
        )
        .reset_index()
    )
    low2 = events.loc[events["low_count"] == 2].copy()
    low2_summary = (
        low2.groupby("label", observed=True)
        .agg(
            events=("ts_code", "size"),
            stocks=("ts_code", "nunique"),
            median_drawdown_pct=("drawdown_pct", "median"),
            mean_drawdown_atr=("drawdown_atr", "mean"),
            p10_drawdown_atr=("drawdown_atr", lambda x: x.quantile(0.10)),
            p25_drawdown_atr=("drawdown_atr", lambda x: x.quantile(0.25)),
            median_drawdown_atr=("drawdown_atr", "median"),
            p75_drawdown_atr=("drawdown_atr", lambda x: x.quantile(0.75)),
            p90_drawdown_atr=("drawdown_atr", lambda x: x.quantile(0.90)),
        )
        .reset_index()
    )
    low2["year"] = pd.to_datetime(low2["trade_date"]).dt.year
    yearly = (
        low2.groupby(["year", "label"], observed=True)
        .agg(events=("ts_code", "size"), median_drawdown_atr=("drawdown_atr", "median"))
        .reset_index()
    )
    threshold_rows = []
    for value in (0.0, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0):
        for direction in ("至少回撤", "至多回撤"):
            selected = low2["drawdown_atr"] >= value if direction == "至少回撤" else low2["drawdown_atr"] <= value
            threshold_rows.append({
                "direction": direction,
                "threshold_atr": value,
                "selected_pct": selected.mean() * 100,
                "true_sell_recall_pct": selected[low2["is_true_sell"]].mean() * 100,
                "false_sell_rejection_pct": (~selected[~low2["is_true_sell"]]).mean() * 100,
                "true_sell_precision_pct": low2.loc[selected, "is_true_sell"].mean() * 100 if selected.any() else np.nan,
            })
    threshold_table = pd.DataFrame(threshold_rows)
    threshold = _best_threshold(low2)
    metadata = {
        "source": database,
        "start_date": "2023-01-01",
        "end_date": str(end_date),
        "eligible_stocks": int(events["ts_code"].nunique()),
        "sell_candidates": int(len(events)),
        "low2_candidates": int(len(low2)),
        "true_low2": int(low2["is_true_sell"].sum()),
        "local_highs": int(turns["is_local_high"].sum()),
        "candidate_definition": "latest prior high_count>=9; subsequent low_count 2..9",
        "truth_definition": "first eligible low2 after each strict +/-20-session local close high",
        "drawdown_atr": "((anchor_close-signal_close)/anchor_close)/(ATR14/signal_close)",
        **threshold,
    }
    result = {
        "events": events,
        "summary": summary,
        "low2_summary": low2_summary,
        "yearly": yearly,
        "threshold_table": threshold_table,
        "turns": turns,
        "metadata": metadata,
    }
    if output is not None:
        destination = Path(output)
        destination.mkdir(parents=True, exist_ok=True)
        events.to_csv(destination / "sell_events.csv", index=False)
        summary.to_csv(destination / "by_low_count.csv", index=False)
        low2_summary.to_csv(destination / "low2_true_false_summary.csv", index=False)
        yearly.to_csv(destination / "low2_by_year.csv", index=False)
        threshold_table.to_csv(destination / "low2_threshold_diagnostics.csv", index=False)
        turns.to_csv(destination / "turning_points.csv", index=False)
        (destination / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--output", default="research/output/nine_turn_atr_sell")
    args = parser.parse_args()
    result = analyze(args.database, args.output)
    print(json.dumps(result["metadata"], ensure_ascii=False, indent=2))
    print(result["low2_summary"].to_string(index=False))
    print(result["summary"].to_string(index=False))


if __name__ == "__main__":
    main()

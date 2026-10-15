#!/usr/bin/env python3
"""Add an A-share gold ETF candidate driven by GLD's five-factor fear/greed.

This experiment preserves the user's four-asset production rotation exactly and
adds 518880.SH as a fifth, mutually exclusive candidate.  Gold uses:

* fear/greed: stored self-calculated GLD.US five-factor history;
* volume ratio: GLD.US daily volume, aligned with the previous available US
  session before the A-share decision;
* execution: 518880.SH next A-share session open, then pessimistic high/low
  fill (-1) exactly like the production strategy;
* valuation: disabled for gold only (NaN valuation position means no gate).

The N-candidate state machine is the existing research extension in
``lab.seesaw_nine_turn_backtest``.  Its four-asset result is asserted to match
the production engine before gold is introduced.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.src.app.api import soxl_fear_backtest as fb  # noqa: E402
from backend.src.core.database import ETFFearGreedCloneHistory, Session  # noqa: E402
from lab.seesaw_nine_turn_backtest import (  # noqa: E402
    SELL_CONFIRM_MA5_SIGNAL,
    SELL_CONFIRM_NONE,
    Target,
    load_target_frame,
    run_rotation,
)
from research.a_stock_fear_component_top10_backtest import (  # noqa: E402
    install_local_market_calendars,
    production_params,
)

START = date(2023, 3, 22)
TRAIN_END = date(2025, 3, 31)
TEST_START = date(2025, 4, 1)
END = date(2026, 9, 30)
INITIAL_CAPITAL = 1_000_000.0
GOLD_SYMBOL = "518880.SH"
GLD_SYMBOL = "GLD.US"

BASE_TARGETS = [
    Target("main", "510880.SH", "a_stock_000015_sh", None, 35.0, 1.6),
    Target("sub", "512480.SH", "a_stock_000688_sh", "588000.SH", 25.0, 1.6),
    Target("sub2", "159509.SZ", "qqq_clone", "QQQ.US", 25.0, 1.4),
    Target("sub3", "516510.SH", "a_stock_930851_csi", None, 20.0, 1.3),
]
SELL_CONFIRM = {
    "main": SELL_CONFIRM_NONE,
    "sub": SELL_CONFIRM_MA5_SIGNAL,
    "sub2": SELL_CONFIRM_MA5_SIGNAL,
    "sub3": SELL_CONFIRM_MA5_SIGNAL,
    "gold": SELL_CONFIRM_MA5_SIGNAL,
}


def clone_history(symbol: str, start: date, end: date) -> pd.DataFrame:
    db = Session()
    try:
        rows = (
            db.query(ETFFearGreedCloneHistory.date, ETFFearGreedCloneHistory.score)
            .filter(
                ETFFearGreedCloneHistory.symbol == symbol,
                ETFFearGreedCloneHistory.date >= start,
                ETFFearGreedCloneHistory.date <= end,
            )
            .order_by(ETFFearGreedCloneHistory.date.asc())
            .all()
        )
    finally:
        Session.remove()
    frame = pd.DataFrame(rows, columns=["fear_date", "gld_fear"])
    if frame.empty:
        raise RuntimeError(f"no stored GLD self-calculated fear/greed history for {start}..{end}")
    frame["fear_date"] = pd.to_datetime(frame["fear_date"])
    frame["gld_fear"] = pd.to_numeric(frame["gld_fear"], errors="coerce")
    return frame.dropna().drop_duplicates("fear_date", keep="last").sort_values("fear_date")


def gold_frame(start: date, end: date) -> tuple[pd.DataFrame, dict]:
    # qqq_clone is only a temporary scaffold for price/GLD volume fields; its
    # fear and valuation fields are replaced below before any strategy run.
    scaffold = Target("gold", GOLD_SYMBOL, "qqq_clone", GLD_SYMBOL, 35.0, 1.3)
    frame = load_target_frame(scaffold, start, end, use_cache=False)
    attrs = frame.attrs.copy()
    history = clone_history(GLD_SYMBOL, start, end).rename(columns={"fear_date": "gld_score_date"})
    working = frame.copy()
    working["date_ts"] = pd.to_datetime(working["date"])
    aligned = pd.merge_asof(
        working.sort_values("date_ts"), history,
        left_on="date_ts", right_on="gld_score_date", direction="backward",
    )
    missing = int(aligned["gld_fear"].isna().sum())
    if missing:
        raise RuntimeError(f"GLD history leaves {missing} A-share sessions without a prior score")
    alignment_lag_days = (aligned["date_ts"] - aligned["gld_score_date"]).dt.days
    aligned["fear_greed"] = aligned["gld_fear"].astype(float)
    # Gold is explicitly exempt from the configured valuation sell gate.
    for column in ("valuation_position_252", "valuation_position_504"):
        if column in aligned:
            aligned[column] = np.nan
    aligned = aligned.drop(columns=["date_ts", "gld_score_date", "gld_fear"])
    aligned.attrs = attrs
    aligned.attrs["fear_source_label"] = "GLD 自算五因子贪恐"
    quality = {
        "gold_symbol": GOLD_SYMBOL,
        "gld_fear_symbol": GLD_SYMBOL,
        "a_share_sessions": int(len(aligned)),
        "gld_history_points": int(len(history)),
        "gld_first_date": str(history["gld_score_date"].min().date()),
        "gld_last_date": str(history["gld_score_date"].max().date()),
        "gld_score_min": float(history["gld_fear"].min()),
        "gld_score_max": float(history["gld_fear"].max()),
        "missing_prior_score": missing,
        "max_alignment_lag_days": int(alignment_lag_days.max()),
    }
    return aligned, quality


def slice_frames(frames: dict[str, pd.DataFrame], start: date, end: date) -> dict[str, pd.DataFrame]:
    output: dict[str, pd.DataFrame] = {}
    for key, frame in frames.items():
        item = frame.loc[(frame["date"] >= start) & (frame["date"] <= end)].copy()
        item.attrs = frame.attrs.copy()
        output[key] = item
    return output


def run(frames: dict[str, pd.DataFrame], targets: list[Target]) -> dict:
    return run_rotation(frames, targets, production_params(), sell_confirm=SELL_CONFIRM, initial_capital=INITIAL_CAPITAL)


def metrics(label: str, result: dict) -> dict:
    return {
        "strategy": label,
        "total_return_pct": float(result["total_return"]),
        "annualized_return_pct": float(result["annualized_return"]),
        "annualized_volatility_pct": float(result["annualized_volatility"]),
        "max_drawdown_pct": float(result["max_drawdown"]),
        "sharpe": float(result["sharpe_ratio"]),
        "sortino": float(result["sortino_ratio"]),
        "calmar": float(result["calmar_ratio"]),
        "buy_count": int(result["buy_count"]),
        "sell_count": int(result["sell_count"]),
        "idle_days": int(result["idle_days"]),
        "trading_days": int(result["trading_days"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--analytics-db", default=os.getenv("ANALYTICS_DB_PATH", "/home/quantd/quant_prod/quant_robot/analytics.duckdb"))
    parser.add_argument("--output", type=Path, default=ROOT / "research/output/a_stock_rotation_gold_gld_backtest")
    args = parser.parse_args()
    if os.getenv("QUANT_DB_READ_ONLY", "").lower() not in {"1", "true", "yes", "on"}:
        raise SystemExit("Set QUANT_DB_READ_ONLY=true before running this research script.")

    args.output.mkdir(parents=True, exist_ok=True)
    install_local_market_calendars(args.analytics_db, START, END)
    frames = {target.key: load_target_frame(target, START, END, use_cache=False) for target in BASE_TARGETS}
    gold, quality = gold_frame(START, END)
    frames["gold"] = gold

    # The generic N-target extension must exactly reproduce the existing result
    # before it is trusted with the fifth leg.
    base_full = run(frames, BASE_TARGETS)
    expected = {"total_return": 375.7528954690099, "annualized_return": 58.191571571193904, "max_drawdown": 17.38913107798735, "sharpe_ratio": 2.1301239006474457}
    for key, value in expected.items():
        if abs(float(base_full[key]) - value) > 1e-6:
            raise AssertionError(f"baseline mismatch for {key}: {base_full[key]} != {value}")

    rows = []
    for period, first, last in (("full", START, END), ("train", START, TRAIN_END), ("test", TEST_START, END)):
        rows.append({"period": period, **metrics("original_four_assets", run(slice_frames(frames, first, last), BASE_TARGETS))})

    grid_rows = []
    candidate_rows: list[tuple[float, Target, dict]] = []
    for buy_threshold in (15.0, 20.0, 25.0, 30.0, 35.0, 40.0, 45.0):
        for volume_threshold in (1.0, 1.2, 1.4, 1.6, 1.8):
            gold_target = Target("gold", GOLD_SYMBOL, "gld_clone", GLD_SYMBOL, buy_threshold, volume_threshold)
            targets = [*BASE_TARGETS, gold_target]
            train_result = run(slice_frames(frames, START, TRAIN_END), targets)
            candidate_rows.append((float(train_result["annualized_return"]), gold_target, train_result))
    candidate_rows.sort(key=lambda value: (value[0], -value[2]["max_drawdown"]), reverse=True)
    chosen_score, chosen_gold, chosen_train = candidate_rows[0]
    chosen_targets = [*BASE_TARGETS, chosen_gold]
    for rank, (score, target, train_result) in enumerate(candidate_rows, start=1):
        test_result = run(slice_frames(frames, TEST_START, END), [*BASE_TARGETS, target])
        grid_rows.append({
            "rank_by_train_ann_return": rank,
            "gold_buy_threshold_lte": target.buy_threshold,
            "gold_gld_volume_ratio_threshold_gte": target.volume_ratio_threshold,
            "train_annualized_return_pct": float(train_result["annualized_return"]),
            "train_max_drawdown_pct": float(train_result["max_drawdown"]),
            "train_sharpe": float(train_result["sharpe_ratio"]),
            "train_gold_buys": sum(t["action"] == "BUY" and t["symbol"] == GOLD_SYMBOL for t in train_result["trades"]),
            "test_annualized_return_pct": float(test_result["annualized_return"]),
            "test_max_drawdown_pct": float(test_result["max_drawdown"]),
            "test_sharpe": float(test_result["sharpe_ratio"]),
            "test_gold_buys": sum(t["action"] == "BUY" and t["symbol"] == GOLD_SYMBOL for t in test_result["trades"]),
        })

    selected = {"full": run(frames, chosen_targets), "train": chosen_train, "test": run(slice_frames(frames, TEST_START, END), chosen_targets)}
    for period, result in selected.items():
        rows.append({
            "period": period,
            **metrics("four_assets_plus_gold_train_selected", result),
            "gold_buy_threshold_lte": chosen_gold.buy_threshold,
            "gold_gld_volume_ratio_threshold_gte": chosen_gold.volume_ratio_threshold,
        })
    pd.DataFrame(rows).to_csv(args.output / "comparison.csv", index=False)
    pd.DataFrame(grid_rows).to_csv(args.output / "gold_threshold_grid.csv", index=False)
    pd.DataFrame([quality]).to_csv(args.output / "data_quality.csv", index=False)
    pd.DataFrame(selected["full"]["trades"]).to_csv(args.output / "selected_trades.csv", index=False)
    pd.DataFrame([t for t in selected["full"]["trades"] if t["symbol"] == GOLD_SYMBOL]).to_csv(args.output / "gold_trades.csv", index=False)
    metadata = {
        "window": {"start": str(START), "train_end": str(TRAIN_END), "test_start": str(TEST_START), "end": str(END)},
        "gold": {"trading_symbol": GOLD_SYMBOL, "fear_source": GLD_SYMBOL, "volume_source": GLD_SYMBOL, "valuation_gate": "disabled"},
        "search": {"gold_buy_threshold_lte": [15, 20, 25, 30, 35, 40, 45], "gold_volume_ratio_threshold_gte": [1.0, 1.2, 1.4, 1.6, 1.8], "selection": "highest train annualized return; test period not used for selection"},
        "baseline_assertion": expected,
    }
    (args.output / "artifact.json").write_text(pd.Series(metadata).to_json(force_ascii=False, indent=2), encoding="utf-8")
    print(pd.DataFrame(rows).to_string(index=False))
    print(f"OUTPUT={args.output}")


if __name__ == "__main__":
    main()

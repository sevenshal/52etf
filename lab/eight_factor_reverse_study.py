"""反向基金份额第八因子的生产策略回测与参数搜索。"""
from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from lab.eight_factor_live_strategy_study import (
    END, START, build_base, initialize_import_environment, metric_row,
    production_params, score_version, slice_base,
)


OUTPUT_DIR = Path("lab/output/eight_factor_reverse_study")
PARAMETER_COLUMNS = [
    "buy_threshold", "greed_threshold", "volume_ratio_threshold",
    "sub_buy_threshold", "sub_volume_ratio_threshold", "sub2_buy_threshold",
    "sub2_volume_ratio_threshold", "swap_threshold",
]
PERIODS = {
    "full": (START, END),
    "train_2023_2024": (START, "2024-12-31"),
    "test_2025_2026": ("2025-01-01", END),
}


def run_period(engine, bases, params, period: str, *, detailed: bool = False):
    period_start, period_end = PERIODS[period]
    main_base, sub_base, sub2_base = bases
    return engine(
        slice_base(main_base, period_start, period_end),
        slice_base(sub_base, period_start, period_end),
        params, 1_000_000, detailed,
        sub2_base_df=slice_base(sub2_base, period_start, period_end),
    )


def main() -> None:
    working_db = initialize_import_environment()
    try:
        from backend.src.app.api.soxl_fear_backtest import _run_seesaw_backtest

        bases = tuple(score_version(base, 8) for base in (
            build_base(
                "510880.SH", "000015.SH", "510880.SH",
                eight_factor_etf="510880.SH", share_factor_direction="negative",
            ),
            build_base(
                "512480.SH", "000688.SH", "588000.SH",
                eight_factor_etf="588000.SH", share_factor_direction="negative",
            ),
            build_base(
                "159941.SZ", "QQQ.US", "QQQ.US", eight_factor_etf=None,
                share_factor_direction="negative",
            ),
        ))
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        fixed_rows = []
        fixed_detail = None
        for period in PERIODS:
            result = run_period(
                _run_seesaw_backtest, bases, production_params(), period,
                detailed=period == "full",
            )
            fixed_rows.append(metric_row(
                "reverse_share_production_params", 8, period,
                production_params(), result,
            ))
            if period == "full":
                fixed_detail = result
        pd.DataFrame(fixed_rows).to_csv(OUTPUT_DIR / "production_params.csv", index=False)
        pd.DataFrame(fixed_detail.get("trades", [])).to_csv(
            OUTPUT_DIR / "production_trades.csv", index=False,
        )

        grid = itertools.product(
            [25.0, 30.0, 35.0], [65.0, 70.0, 75.0], [1.4, 1.6, 1.8],
            [20.0, 25.0, 30.0], [1.4, 1.6],
            [15.0, 20.0, 25.0], [1.2, 1.3, 1.4], [40.0, 45.0, 50.0],
        )
        train_rows = []
        full_rows = []
        for values in grid:
            params = production_params(**dict(zip(PARAMETER_COLUMNS, values)))
            for period, destination in (
                ("train_2023_2024", train_rows), ("full", full_rows),
            ):
                result = run_period(_run_seesaw_backtest, bases, params, period)
                destination.append(metric_row("reverse_share_grid", 8, period, params, result))
        train = pd.DataFrame(train_rows)
        full = pd.DataFrame(full_rows)
        train.to_csv(OUTPUT_DIR / "train_grid.csv", index=False)
        full.to_csv(OUTPUT_DIR / "full_grid.csv", index=False)

        eligible = train[(train["buy_count"] >= 2) & (train["sell_count"] >= 2)]
        selected = eligible.sort_values(
            ["sharpe_ratio", "annualized_return_pct", "calmar_ratio"], ascending=False,
        ).head(50)
        validation_rows = []
        for rank, (_, item) in enumerate(selected.iterrows(), 1):
            params = production_params(**{key: item[key] for key in PARAMETER_COLUMNS})
            for period in ("test_2025_2026", "full"):
                result = run_period(_run_seesaw_backtest, bases, params, period)
                row = metric_row(f"train_rank_{rank}", 8, period, params, result)
                row["train_rank"] = rank
                validation_rows.append(row)
        validation = pd.DataFrame(validation_rows)
        validation.to_csv(OUTPUT_DIR / "top50_validation.csv", index=False)

        print("Fixed production parameters")
        print(pd.DataFrame(fixed_rows).to_string(index=False))
        print("\nFull-sample top return")
        print(full.sort_values(
            ["total_return_pct", "sharpe_ratio"], ascending=False,
        ).head(10).to_string(index=False))
        print("\nTraining-selected top validation")
        print(validation[validation["period"] == "test_2025_2026"].sort_values(
            ["sharpe_ratio", "total_return_pct"], ascending=False,
        ).head(10).to_string(index=False))
    finally:
        working_db.unlink(missing_ok=True)


if __name__ == "__main__":
    main()

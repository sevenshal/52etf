"""正向份额八因子策略：贪恐阈值步长1的局部联合细搜。"""
from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from lab.eight_factor_live_strategy_study import (
    END, START, build_base, initialize_import_environment, production_params,
    score_version, slice_base,
)


OUTPUT_DIR = Path("lab/output/eight_factor_positive_fine_search")
PARAMETER_COLUMNS = [
    "buy_threshold", "greed_threshold", "volume_ratio_threshold",
    "sub_buy_threshold", "sub_volume_ratio_threshold", "sub2_buy_threshold",
    "sub2_volume_ratio_threshold", "swap_threshold",
]
SEARCH_VALUES = {
    "buy_threshold": list(range(25, 41)),
    "greed_threshold": list(range(65, 76)),
    "volume_ratio_threshold": [1.4, 1.6, 1.8],
    "sub_buy_threshold": list(range(20, 36)),
    "sub_volume_ratio_threshold": [1.4, 1.6],
    "sub2_buy_threshold": list(range(15, 26)),
    "sub2_volume_ratio_threshold": [1.2, 1.3, 1.4],
    "swap_threshold": list(range(40, 51)),
}


def metrics(result: dict, params: dict, stage: str, period: str) -> dict:
    return {
        "stage": stage, "period": period, **params,
        "total_return_pct": result["total_return"],
        "annualized_return_pct": result["annualized_return"],
        "max_drawdown_pct": result["max_drawdown"],
        "sharpe_ratio": result["sharpe_ratio"],
        "sortino_ratio": result["sortino_ratio"],
        "calmar_ratio": result["calmar_ratio"],
        "buy_count": result["buy_count"], "sell_count": result["sell_count"],
    }


def main() -> None:
    working_db = initialize_import_environment()
    try:
        from backend.src.app.api.soxl_fear_backtest import _run_seesaw_backtest

        full_bases = tuple(score_version(base, 8) for base in (
            build_base("510880.SH", "000015.SH", "510880.SH", eight_factor_etf="510880.SH"),
            build_base("512480.SH", "000688.SH", "588000.SH", eight_factor_etf="588000.SH"),
            build_base("159941.SZ", "QQQ.US", "QQQ.US", eight_factor_etf=None),
        ))

        def run(values: dict, bases=full_bases):
            return _run_seesaw_backtest(
                bases[0], bases[1], production_params(**values), 1_000_000, False,
                sub2_base_df=bases[2],
            )

        coarse = pd.read_csv(
            "lab/output/eight_factor_live_strategy_study/eight_factor_full_grid.csv"
        ).sort_values(["sharpe_ratio", "total_return_pct"], ascending=False)
        seeds = coarse.head(60)[PARAMETER_COLUMNS].drop_duplicates().head(12)
        evaluated: dict[tuple, dict] = {}

        def evaluate(values: dict, stage: str) -> dict:
            key = tuple(values[column] for column in PARAMETER_COLUMNS)
            if key not in evaluated:
                evaluated[key] = metrics(run(values), values, stage, "full")
            return evaluated[key]

        coordinate_endpoints = []
        for _, seed in seeds.iterrows():
            current = {column: float(seed[column]) for column in PARAMETER_COLUMNS}
            for _ in range(6):
                changed = False
                for column in PARAMETER_COLUMNS:
                    candidates = []
                    for value in SEARCH_VALUES[column]:
                        candidate = dict(current)
                        candidate[column] = float(value)
                        candidates.append(evaluate(candidate, "coordinate"))
                    best = max(
                        candidates,
                        key=lambda row: (row["sharpe_ratio"], row["total_return_pct"]),
                    )
                    next_values = {key: best[key] for key in PARAMETER_COLUMNS}
                    if tuple(next_values.values()) != tuple(current.values()):
                        current = next_values
                        changed = True
                if not changed:
                    break
            coordinate_endpoints.append(evaluate(current, "coordinate_endpoint"))

        endpoints = pd.DataFrame(coordinate_endpoints).sort_values(
            ["sharpe_ratio", "total_return_pct"], ascending=False,
        ).drop_duplicates(PARAMETER_COLUMNS).head(5)

        # 对五个贪恐阈值做±2的联合步长1搜索；量比沿用每个候选点的最优值。
        for _, endpoint in endpoints.iterrows():
            ranges = []
            for column in [
                "buy_threshold", "greed_threshold", "sub_buy_threshold",
                "sub2_buy_threshold", "swap_threshold",
            ]:
                center = int(endpoint[column])
                allowed = SEARCH_VALUES[column]
                ranges.append([value for value in range(center - 2, center + 3) if value in allowed])
            for values in itertools.product(*ranges):
                candidate = {column: float(endpoint[column]) for column in PARAMETER_COLUMNS}
                for column, value in zip(
                    ["buy_threshold", "greed_threshold", "sub_buy_threshold",
                     "sub2_buy_threshold", "swap_threshold"], values,
                ):
                    candidate[column] = float(value)
                evaluate(candidate, "joint_fine")

        results = pd.DataFrame(evaluated.values()).sort_values(
            ["sharpe_ratio", "total_return_pct"], ascending=False,
        )
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        results.to_csv(OUTPUT_DIR / "full_results.csv", index=False)
        endpoints.to_csv(OUTPUT_DIR / "coordinate_endpoints.csv", index=False)

        # 严格样本外：只用训练期粗网格定位并细搜，再验证训练期最佳点。
        train_bases = tuple(slice_base(base, START, "2024-12-31") for base in full_bases)
        test_bases = tuple(slice_base(base, "2025-01-01", END) for base in full_bases)
        coarse_train = pd.read_csv(
            "lab/output/eight_factor_live_strategy_study/eight_factor_train_grid.csv"
        ).sort_values(["sharpe_ratio", "annualized_return_pct"], ascending=False)
        train_seeds = coarse_train.head(60)[PARAMETER_COLUMNS].drop_duplicates().head(12)
        train_evaluated: dict[tuple, dict] = {}

        def evaluate_train(values: dict, stage: str) -> dict:
            key = tuple(values[column] for column in PARAMETER_COLUMNS)
            if key not in train_evaluated:
                train_evaluated[key] = metrics(
                    run(values, train_bases), values, stage, "train_2023_2024",
                )
            return train_evaluated[key]

        train_endpoints = []
        for _, seed in train_seeds.iterrows():
            current = {column: float(seed[column]) for column in PARAMETER_COLUMNS}
            for _ in range(6):
                changed = False
                for column in PARAMETER_COLUMNS:
                    candidates = []
                    for value in SEARCH_VALUES[column]:
                        candidate = dict(current)
                        candidate[column] = float(value)
                        candidates.append(evaluate_train(candidate, "train_coordinate"))
                    best = max(
                        candidates,
                        key=lambda row: (row["sharpe_ratio"], row["annualized_return_pct"]),
                    )
                    next_values = {key: best[key] for key in PARAMETER_COLUMNS}
                    if tuple(next_values.values()) != tuple(current.values()):
                        current = next_values
                        changed = True
                if not changed:
                    break
            train_endpoints.append(evaluate_train(current, "train_coordinate_endpoint"))

        unique_train_endpoints = pd.DataFrame(train_endpoints).sort_values(
            ["sharpe_ratio", "annualized_return_pct"], ascending=False,
        ).drop_duplicates(PARAMETER_COLUMNS).head(5)
        for _, endpoint in unique_train_endpoints.iterrows():
            ranges = []
            threshold_columns = [
                "buy_threshold", "greed_threshold", "sub_buy_threshold",
                "sub2_buy_threshold", "swap_threshold",
            ]
            for column in threshold_columns:
                center = int(endpoint[column])
                ranges.append([
                    value for value in range(center - 2, center + 3)
                    if value in SEARCH_VALUES[column]
                ])
            for threshold_values in itertools.product(*ranges):
                candidate = {column: float(endpoint[column]) for column in PARAMETER_COLUMNS}
                for column, value in zip(threshold_columns, threshold_values):
                    candidate[column] = float(value)
                evaluate_train(candidate, "train_joint_fine")

        train_results = pd.DataFrame(train_evaluated.values()).sort_values(
            ["sharpe_ratio", "annualized_return_pct", "calmar_ratio"], ascending=False,
        )
        train_results.to_csv(OUTPUT_DIR / "train_results.csv", index=False)
        validation_rows = []
        for rank, (_, item) in enumerate(train_results.head(50).iterrows(), 1):
            values = {column: float(item[column]) for column in PARAMETER_COLUMNS}
            row = metrics(run(values, test_bases), values, "train_top50", "test_2025_2026")
            row["train_rank"] = rank
            validation_rows.append(row)
        validation = pd.DataFrame(validation_rows)
        validation.to_csv(OUTPUT_DIR / "top50_validation.csv", index=False)

        print(f"unique evaluated: {len(results)}")
        print("\nFull best Sharpe")
        print(results.head(10).to_string(index=False))
        print("\nTrain-selected test validation")
        print(validation.sort_values(
            ["sharpe_ratio", "total_return_pct"], ascending=False,
        ).head(10).to_string(index=False))
    finally:
        working_db.unlink(missing_ok=True)


if __name__ == "__main__":
    main()

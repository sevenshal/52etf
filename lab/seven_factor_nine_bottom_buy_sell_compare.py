"""七因子九底扩展买入，比较原卖出与九顶/MA5组合卖出。"""
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

OUTPUT_DIR = Path("lab/output/seven_factor_nine_bottom_buy_sell_compare")
CORE = [
    "buy_threshold", "greed_threshold", "volume_ratio_threshold",
    "sub_buy_threshold", "sub_volume_ratio_threshold", "sub2_buy_threshold",
    "sub2_volume_ratio_threshold", "swap_threshold", "nine_bottom_buy_threshold",
]
MA = ["ma5_top_score", "ma5_lookback_days", "turn_signal_cooldown_days"]
VALUES = {
    "buy_threshold": list(range(25, 36)), "greed_threshold": list(range(40, 86)),
    "volume_ratio_threshold": [1.4, 1.6, 1.8], "sub_buy_threshold": list(range(20, 31)),
    "sub_volume_ratio_threshold": [1.4, 1.6], "sub2_buy_threshold": list(range(15, 26)),
    "sub2_volume_ratio_threshold": [1.2, 1.3, 1.4], "swap_threshold": list(range(40, 51)),
    "nine_bottom_buy_threshold": list(range(10, 41)), "ma5_top_score": list(range(60, 91)),
    "ma5_lookback_days": list(range(3, 11)), "turn_signal_cooldown_days": list(range(0, 11)),
}


def result_row(result, values, mode, period, stage):
    return {
        "sell_mode": mode, "period": period, "stage": stage, **values,
        "total_return_pct": result["total_return"], "annualized_return_pct": result["annualized_return"],
        "max_drawdown_pct": result["max_drawdown"], "sharpe_ratio": result["sharpe_ratio"],
        "sortino_ratio": result["sortino_ratio"], "calmar_ratio": result["calmar_ratio"],
        "buy_count": result["buy_count"], "sell_count": result["sell_count"],
    }


def main():
    temp_db = initialize_import_environment()
    try:
        from backend.src.app.api.soxl_fear_backtest import _run_seesaw_backtest
        full_bases = tuple(score_version(base, 7) for base in (
            build_base("510880.SH", "000015.SH", "510880.SH", eight_factor_etf="510880.SH"),
            build_base("512480.SH", "000688.SH", "588000.SH", eight_factor_etf="588000.SH"),
            build_base("159941.SZ", "QQQ.US", "QQQ.US", eight_factor_etf=None),
        ))
        defaults = production_params()

        def columns(mode):
            return CORE + (MA if mode == "nine_or_ma5" else [])

        def run(values, mode, bases=full_bases):
            params = production_params(
                **values, buy_legacy_or_nine_bottom=True,
                sell_nine_top_or_ma5=mode == "nine_or_ma5",
            )
            return _run_seesaw_backtest(bases[0], bases[1], params, 1_000_000, False, sub2_base_df=bases[2])

        def seeds(mode):
            result = []
            for buy, greed, nine_buy in itertools.product((25, 30, 35), (50, 60, 70, 80), (15, 25, 35)):
                item = {column: getattr(defaults, column) for column in columns(mode)}
                item.update(buy_threshold=float(buy), greed_threshold=float(greed), nine_bottom_buy_threshold=float(nine_buy))
                if mode == "nine_or_ma5":
                    item.update(ma5_top_score=70.0, ma5_lookback_days=4, turn_signal_cooldown_days=0)
                result.append(item)
            return result

        def search(mode, bases, period):
            cols = columns(mode)
            cache = {}

            def evaluate(values, stage):
                key = tuple(values[column] for column in cols)
                if key not in cache:
                    cache[key] = result_row(run(values, mode, bases), values, mode, period, stage)
                return cache[key]

            endpoints = []
            for seed in seeds(mode):
                current = dict(seed)
                for _ in range(6):
                    changed = False
                    for column in cols:
                        candidates = []
                        for value in VALUES[column]:
                            candidate = dict(current)
                            candidate[column] = int(value) if column in {"ma5_lookback_days", "turn_signal_cooldown_days"} else float(value)
                            candidates.append(evaluate(candidate, "coordinate"))
                        best = max(candidates, key=lambda x: (x["sharpe_ratio"], x["total_return_pct"]))
                        next_values = {column: best[column] for column in cols}
                        if tuple(next_values.values()) != tuple(current.values()):
                            current, changed = next_values, True
                    if not changed:
                        break
                endpoints.append(evaluate(current, "endpoint"))
            endpoint_frame = pd.DataFrame(endpoints).sort_values(
                ["sharpe_ratio", "total_return_pct"], ascending=False,
            ).drop_duplicates(cols).head(4)

            # 联合细化两个买入阈值、九底阈值和贪婪阈值；组合卖出再加入MA5参数。
            joint = ["buy_threshold", "greed_threshold", "nine_bottom_buy_threshold"]
            if mode == "nine_or_ma5":
                joint += MA
            for _, endpoint in endpoint_frame.iterrows():
                ranges = []
                for column in joint:
                    radius = 1 if column in {"ma5_top_score", "ma5_lookback_days", "turn_signal_cooldown_days"} else 2
                    center = int(endpoint[column])
                    ranges.append([v for v in range(center-radius, center+radius+1) if v in VALUES[column]])
                for combination in itertools.product(*ranges):
                    candidate = {column: endpoint[column] for column in cols}
                    candidate.update(dict(zip(joint, combination)))
                    evaluate(candidate, "joint_fine")
            return pd.DataFrame(cache.values()).sort_values(
                ["sharpe_ratio", "total_return_pct"], ascending=False,
            )

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        train_bases = tuple(slice_base(base, START, "2024-12-31") for base in full_bases)
        test_bases = tuple(slice_base(base, "2025-01-01", END) for base in full_bases)
        summary = []
        for mode in ("legacy", "nine_or_ma5"):
            full = search(mode, full_bases, "full")
            train = search(mode, train_bases, "train_2023_2024")
            full.to_csv(OUTPUT_DIR / f"{mode}_full_results.csv", index=False)
            train.to_csv(OUTPUT_DIR / f"{mode}_train_results.csv", index=False)
            validation = []
            for rank, (_, item) in enumerate(train.head(50).iterrows(), 1):
                vals = {column: item[column] for column in columns(mode)}
                tested = result_row(run(vals, mode, test_bases), vals, mode, "test_2025_2026", "train_top50")
                tested["train_rank"] = rank
                validation.append(tested)
            pd.DataFrame(validation).to_csv(OUTPUT_DIR / f"{mode}_top50_validation.csv", index=False)
            summary.append(full.iloc[0].to_dict())
            print(f"\n{mode}: full={len(full)}, train={len(train)}")
            print(full.head(8).to_string(index=False))
            print("test")
            print(pd.DataFrame(validation).sort_values(["sharpe_ratio", "total_return_pct"], ascending=False).head(8).to_string(index=False))
        pd.DataFrame(summary).to_csv(OUTPUT_DIR / "full_best_comparison.csv", index=False)
    finally:
        temp_db.unlink(missing_ok=True)


if __name__ == "__main__":
    main()

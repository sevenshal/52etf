#!/usr/bin/env python3
"""Compare 1/3/5-snapshot Xueqiu weight/price ratios on one common sample.

The input panels are produced by ``study_xueqiu_weight_price_ratio.py``. Signals
are formed after the snapshot close and entered at the next trading-day open.
"""
from __future__ import annotations

import argparse
import json
from itertools import combinations
from pathlib import Path
from typing import Dict, Iterable

import numpy as np
import pandas as pd
from scipy import stats


DEFAULT_LOOKBACKS = (1, 3, 5)
DEFAULT_INPUT_TEMPLATE = (
    "lab/output/xueqiu_weight_price_lookback_{lookback}d_20260701_20260826/"
    "factor_panel.csv"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-template", default=DEFAULT_INPUT_TEMPLATE)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("lab/output/xueqiu_weight_price_lookback_comparison_20260701_20260826"),
    )
    parser.add_argument("--lookbacks", default="1,3,5")
    parser.add_argument("--cost-bps", type=float, default=20.0)
    parser.add_argument("--min-ratio", type=float, default=1.23)
    parser.add_argument("--max-ratio", type=float, default=6.0)
    parser.add_argument("--min-holding-cubes", type=int, default=5)
    parser.add_argument("--holding-cube-increase", type=int, default=1)
    parser.add_argument("--current-rank-limit", type=int, default=100)
    parser.add_argument("--new-entry-rank-limit", type=int, default=30)
    parser.add_argument("--new-entry-min-cubes", type=int, default=10)
    return parser.parse_args()


def _ttest(values: Iterable[float]) -> Dict[str, float | int | None]:
    series = pd.Series(values, dtype=float).dropna()
    if len(series) < 2:
        return {"n": len(series), "t": None, "p": None}
    result = stats.ttest_1samp(series, 0.0)
    return {"n": len(series), "t": float(result.statistic), "p": float(result.pvalue)}


def load_common_panels(input_template: str, lookbacks: Iterable[int]) -> Dict[int, pd.DataFrame]:
    frames: Dict[int, pd.DataFrame] = {}
    key_sets = []
    for lookback in lookbacks:
        path = Path(input_template.format(lookback=lookback))
        frame = pd.read_csv(path)
        frame["snapshot_date"] = pd.to_datetime(frame["snapshot_date"]).dt.date.astype(str)
        frame["key"] = list(zip(frame["snapshot_date"], frame["ts_code"]))
        frames[lookback] = frame
        key_sets.append(set(frame["key"]))
    common_keys = set.intersection(*key_sets)
    for lookback, frame in frames.items():
        frames[lookback] = frame[frame["key"].isin(common_keys)].copy()
    return frames


def add_production_signal(
    frame: pd.DataFrame,
    *,
    min_ratio: float,
    max_ratio: float,
    min_holding_cubes: int,
    holding_cube_increase: int,
    current_rank_limit: int,
    new_entry_rank_limit: int,
    new_entry_min_cubes: int,
) -> pd.DataFrame:
    result = frame.copy()
    effective_cube_change = result["cube_change"].fillna(result["holding_cubes"])
    effective_weight_change = result["weight_change"].fillna(result["total_weight"])
    outer = (
        (result["composite_rank"] <= current_rank_limit)
        & (result["holding_cubes"] >= min_holding_cubes)
        & (effective_cube_change >= holding_cube_increase)
        & (effective_weight_change > 0)
    )
    strong_new = (
        result["prior_total_weight"].isna()
        & (result["composite_rank"] <= new_entry_rank_limit)
        & (result["holding_cubes"] >= new_entry_min_cubes)
    )
    contrarian = (
        (result["weight_multiple"] > 1.05)
        & (result["price_multiple"] < 1.0)
        & result["ratio"].between(min_ratio, max_ratio, inclusive="both")
    )
    result["production_signal"] = outer & (strong_new | contrarian)
    # Equal-count comparison pool: same production gates, but no lower ratio threshold.
    result["rankable_candidate"] = outer & (
        (result["weight_multiple"] > 1.05)
        & (result["price_multiple"] < 1.0)
        & (result["ratio"] <= max_ratio)
    )
    return result


def analyze(
    *,
    input_template: str = DEFAULT_INPUT_TEMPLATE,
    lookbacks: Iterable[int] = DEFAULT_LOOKBACKS,
    cost_bps: float = 20.0,
    min_ratio: float = 1.23,
    max_ratio: float = 6.0,
    min_holding_cubes: int = 5,
    holding_cube_increase: int = 1,
    current_rank_limit: int = 100,
    new_entry_rank_limit: int = 30,
    new_entry_min_cubes: int = 10,
) -> Dict[str, object]:
    lookbacks = tuple(sorted({int(value) for value in lookbacks}))
    frames = load_common_panels(input_template, lookbacks)
    source_quality = {}
    quality_path = Path(input_template.format(lookback=max(lookbacks))).with_name("metrics.json")
    if quality_path.exists():
        source_quality = json.loads(quality_path.read_text(encoding="utf-8"))
    common_rows = len(next(iter(frames.values())))
    cost = cost_bps / 10_000.0
    signal_rows = []
    daily_signal_rows = []
    factor_rows = []
    daily_factor_rows = []
    topn_rows = []
    selections: Dict[int, Dict[str, set[str]]] = {}

    for lookback, raw_frame in frames.items():
        frame = add_production_signal(
            raw_frame,
            min_ratio=min_ratio,
            max_ratio=max_ratio,
            min_holding_cubes=min_holding_cubes,
            holding_cube_increase=holding_cube_increase,
            current_rank_limit=current_rank_limit,
            new_entry_rank_limit=new_entry_rank_limit,
            new_entry_min_cubes=new_entry_min_cubes,
        )
        frames[lookback] = frame
        selections[lookback] = {
            snapshot_date: set(group["ts_code"])
            for snapshot_date, group in frame[frame["production_signal"]].groupby("snapshot_date")
        }

        for horizon in (1, 3, 5, 10):
            return_col = f"ret_{horizon}d"
            mature = frame.dropna(subset=[return_col]).copy()
            universe_return = mature.groupby("snapshot_date")[return_col].mean()
            selected = mature[mature["production_signal"]]
            daily = selected.groupby("snapshot_date").agg(
                signal_count=("ts_code", "size"),
                gross_return=(return_col, "mean"),
            )
            daily["universe_return"] = universe_return.reindex(daily.index)
            daily["gross_excess"] = daily["gross_return"] - daily["universe_return"]
            daily["net_excess"] = daily["gross_excess"] - cost
            test = _ttest(daily["net_excess"])
            signal_rows.append(
                {
                    "lookback_days": lookback,
                    "horizon_days": horizon,
                    "signal_instances": int(len(selected)),
                    "signal_dates": int(len(daily)),
                    "avg_signals_per_date": float(daily["signal_count"].mean()),
                    "gross_return": float(daily["gross_return"].mean()),
                    "gross_excess": float(daily["gross_excess"].mean()),
                    "net_excess": float(daily["net_excess"].mean()),
                    "net_excess_win_rate": float((daily["net_excess"] > 0).mean()),
                    "net_excess_t": test["t"],
                    "net_excess_p": test["p"],
                }
            )
            for snapshot_date, row in daily.reset_index().iterrows():
                daily_signal_rows.append(
                    {"lookback_days": lookback, "horizon_days": horizon, **row.to_dict()}
                )

            daily_topn = []
            for snapshot_date, candidates in mature[mature["rankable_candidate"]].groupby(
                "snapshot_date"
            ):
                picked = candidates.sort_values("ratio", ascending=False).head(10)
                gross_return = float(picked[return_col].mean())
                daily_topn.append(
                    {
                        "snapshot_date": snapshot_date,
                        "selected_count": len(picked),
                        "gross_return": gross_return,
                        "net_excess": gross_return - float(universe_return.loc[snapshot_date]) - cost,
                    }
                )
            topn = pd.DataFrame(daily_topn)
            topn_test = _ttest(topn["net_excess"] if not topn.empty else [])
            topn_rows.append(
                {
                    "lookback_days": lookback,
                    "horizon_days": horizon,
                    "selection_dates": int(len(topn)),
                    "avg_selected_count": float(topn["selected_count"].mean()),
                    "net_excess": float(topn["net_excess"].mean()),
                    "net_excess_win_rate": float((topn["net_excess"] > 0).mean()),
                    "net_excess_p": topn_test["p"],
                }
            )

            daily_cross_sections = []
            factor_sample = mature.dropna(subset=["ratio"])
            for snapshot_date, cross_section in factor_sample.groupby("snapshot_date"):
                if len(cross_section) < 20:
                    continue
                cross_section = cross_section.copy()
                cross_section["decile"] = pd.qcut(
                    cross_section["ratio"].rank(method="first"), 10, labels=False
                )
                rank_ic = stats.spearmanr(
                    cross_section["ratio"], cross_section[return_col]
                ).statistic
                top_return = cross_section.loc[
                    cross_section["decile"] == 9, return_col
                ].mean()
                bottom_return = cross_section.loc[
                    cross_section["decile"] == 0, return_col
                ].mean()
                daily_cross_sections.append(
                    {
                        "snapshot_date": snapshot_date,
                        "rank_ic": rank_ic,
                        "top_bottom_spread": top_return - bottom_return,
                        "sample_size": len(cross_section),
                    }
                )
            cross_sections = pd.DataFrame(daily_cross_sections)
            spread_test = _ttest(cross_sections["top_bottom_spread"])
            factor_rows.append(
                {
                    "lookback_days": lookback,
                    "horizon_days": horizon,
                    "cross_sections": int(len(cross_sections)),
                    "stock_days": int(len(factor_sample)),
                    "rank_ic": float(cross_sections["rank_ic"].mean()),
                    "rank_ic_positive_rate": float((cross_sections["rank_ic"] > 0).mean()),
                    "top_bottom_spread": float(cross_sections["top_bottom_spread"].mean()),
                    "top_bottom_spread_p": spread_test["p"],
                }
            )
            for row in daily_cross_sections:
                daily_factor_rows.append(
                    {"lookback_days": lookback, "horizon_days": horizon, **row}
                )

    overlap_rows = []
    all_dates = sorted(set().union(*(set(value) for value in selections.values())))
    for left, right in combinations(lookbacks, 2):
        daily_jaccard = []
        for snapshot_date in all_dates:
            left_symbols = selections[left].get(snapshot_date, set())
            right_symbols = selections[right].get(snapshot_date, set())
            union = left_symbols | right_symbols
            if not union:
                continue
            daily_jaccard.append(len(left_symbols & right_symbols) / len(union))
        overlap_rows.append(
            {
                "left_lookback_days": left,
                "right_lookback_days": right,
                "dates": len(daily_jaccard),
                "mean_daily_jaccard": float(np.mean(daily_jaccard)),
            }
        )

    signal_metrics = pd.DataFrame(signal_rows)
    daily_signal = pd.DataFrame(daily_signal_rows)
    factor_metrics = pd.DataFrame(factor_rows)
    daily_factor = pd.DataFrame(daily_factor_rows)
    topn_metrics = pd.DataFrame(topn_rows)
    overlap = pd.DataFrame(overlap_rows)
    pairwise_rows = []
    for horizon in (1, 3, 5, 10):
        aligned = daily_signal[daily_signal["horizon_days"] == horizon].pivot(
            index="snapshot_date", columns="lookback_days", values="net_excess"
        )
        for left, right in combinations(lookbacks, 2):
            difference = (aligned[left] - aligned[right]).dropna()
            test = _ttest(difference)
            pairwise_rows.append(
                {
                    "horizon_days": horizon,
                    "left_lookback_days": left,
                    "right_lookback_days": right,
                    "common_dates": int(len(difference)),
                    "left_minus_right_net_excess": float(difference.mean()),
                    "difference_p": test["p"],
                }
            )
    pairwise = pd.DataFrame(pairwise_rows)
    metadata = {
        "lookbacks": list(lookbacks),
        "common_stock_days": common_rows,
        "common_snapshot_dates": int(next(iter(frames.values()))["snapshot_date"].nunique()),
        "common_symbols": int(next(iter(frames.values()))["ts_code"].nunique()),
        "snapshot_min": min(frame["snapshot_date"].min() for frame in frames.values()),
        "snapshot_max": max(frame["snapshot_date"].max() for frame in frames.values()),
        "cost_bps": cost_bps,
        "holdings_valid_from": source_quality.get("holdings_valid_from"),
        "missing_snapshot_trading_days": source_quality.get(
            "missing_snapshot_trading_days", []
        ),
        "production_parameters": {
            "min_ratio": min_ratio,
            "max_ratio": max_ratio,
            "min_holding_cubes": min_holding_cubes,
            "holding_cube_increase": holding_cube_increase,
            "current_rank_limit": current_rank_limit,
            "new_entry_rank_limit": new_entry_rank_limit,
            "new_entry_min_cubes": new_entry_min_cubes,
        },
    }
    return {
        "signal_metrics": signal_metrics,
        "daily_signal": daily_signal,
        "factor_metrics": factor_metrics,
        "daily_factor": daily_factor,
        "topn_metrics": topn_metrics,
        "overlap": overlap,
        "pairwise": pairwise,
        "metadata": metadata,
    }


def _pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def write_outputs(result: Dict[str, object], output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for name in (
        "signal_metrics",
        "daily_signal",
        "factor_metrics",
        "daily_factor",
        "topn_metrics",
        "overlap",
        "pairwise",
    ):
        result[name].to_csv(output / f"{name}.csv", index=False)
    (output / "metrics.json").write_text(
        json.dumps(result["metadata"], ensure_ascii=False, indent=2), encoding="utf-8"
    )

    signal_metrics = result["signal_metrics"]
    factor_metrics = result["factor_metrics"]
    topn_metrics = result["topn_metrics"]
    pairwise = result["pairwise"]
    rows = signal_metrics[signal_metrics["horizon_days"].isin([5, 10])]
    lines = [
        "# 雪球权价比 1/3/5 日回看对比",
        "",
        "信号在收盘快照后形成，下一交易日开盘进入；净超额已扣 20 bps 往返成本。",
        "",
        "## 生产门槛信号",
        "",
        "|回看|持有期|信号数|日期数|日均信号|净超额|超额胜率|p值|",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows.itertuples():
        lines.append(
            f"|{row.lookback_days}日|{row.horizon_days}日|{row.signal_instances}|"
            f"{row.signal_dates}|{row.avg_signals_per_date:.1f}|{_pct(row.net_excess)}|"
            f"{_pct(row.net_excess_win_rate)}|{row.net_excess_p:.3f}|"
        )
    matched_3v5_5d = pairwise[
        (pairwise["horizon_days"] == 5)
        & (pairwise["left_lookback_days"] == 3)
        & (pairwise["right_lookback_days"] == 5)
    ].iloc[0]
    matched_direction = "高" if matched_3v5_5d.left_minus_right_net_excess >= 0 else "低"
    lines += [
        "",
        "## 连续因子与等数量排序（5日持有）",
        "",
        "|回看|Rank IC|Top-Bottom|Top10净超额|Top10超额胜率|",
        "|---:|---:|---:|---:|---:|",
    ]
    factors_5d = factor_metrics[factor_metrics["horizon_days"] == 5].set_index(
        "lookback_days"
    )
    topn_5d = topn_metrics[topn_metrics["horizon_days"] == 5].set_index(
        "lookback_days"
    )
    for lookback in result["metadata"]["lookbacks"]:
        factor = factors_5d.loc[lookback]
        topn = topn_5d.loc[lookback]
        lines.append(
            f"|{lookback}日|{factor.rank_ic:.3f}|{_pct(factor.top_bottom_spread)}|"
            f"{_pct(topn.net_excess)}|{_pct(topn.net_excess_win_rate)}|"
        )
    lines += [
        "",
        "## 结论",
        "",
        "- 不建议切到1日：5日与10日持有的生产门槛净超额均为负，短期噪声最大。",
        "- 3日可作为影子观察版本，但当前样本下没有稳定胜过5日；"
        f"同日配对的5日持有净超额比5日版{matched_direction} "
        f"{_pct(abs(matched_3v5_5d.left_minus_right_net_excess))}"
        f"（p={matched_3v5_5d.difference_p:.3f}）。",
        "- 保留5日为线上默认；样本仅约两个月。除1日版相对5日版的10日配对差外，主差异均未达到常用统计显著性门槛。",
    ]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    lookbacks = tuple(int(value.strip()) for value in args.lookbacks.split(",") if value.strip())
    result = analyze(
        input_template=args.input_template,
        lookbacks=lookbacks,
        cost_bps=args.cost_bps,
        min_ratio=args.min_ratio,
        max_ratio=args.max_ratio,
        min_holding_cubes=args.min_holding_cubes,
        holding_cube_increase=args.holding_cube_increase,
        current_rank_limit=args.current_rank_limit,
        new_entry_rank_limit=args.new_entry_rank_limit,
        new_entry_min_cubes=args.new_entry_min_cubes,
    )
    write_outputs(result, args.output)
    print(args.output / "report.md")


if __name__ == "__main__":
    main()

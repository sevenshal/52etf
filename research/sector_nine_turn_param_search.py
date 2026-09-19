#!/usr/bin/env python3
"""板块九转的超参数搜索：在同一套随机挑股口径下网格搜索，并用样本外检验防过拟合。

评价指标一律是"随机挑股 N 次的**中位**组合收益"和对应回撤——同一天信号远多于空仓位，
只报一条确定性净值等于把挑股运气当成本事。

防过拟合的两件事：

1. **样本切两段**：2023-01-01~2025-06-30 调参（in-sample），2025-07-01 之后只做检验（out-of-sample）。
   选参数只看前段，后段用来看排名还站不站得住。
2. **看平台不看尖峰**：报每个候选在邻域里的表现，孤立的高点不取。

复用 ``sector_nine_turn_stock_system_merge`` 的数据缓存和回测函数。
"""

from __future__ import annotations

import argparse
import itertools
import json
import pickle
import sys
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT), str(ROOT / "backend")):
    if path not in sys.path:
        sys.path.insert(0, path)

from research.sector_nine_turn_stock_system_merge import (  # noqa: E402
    BASE,
    CACHE_PATH,
    DEFAULT_OUTPUT,
    Variant,
    build_trades,
    performance,
    simulate,
)

SPLIT_DATE = date(2025, 7, 1)
TUNE_KEYS = ("fear_threshold", "fear_lookback", "volume_z_min", "volume_lookback",
             "buy_high_min", "buy_high_max", "sector_high_min", "sector_high_max",
             "low_count_min", "max_positions", "arm_window", "exit_atr", "exit_low_count",
             "require_high9", "sectors", "pick")


def _slice(cache: Dict[str, Any], start: date, end: date) -> Dict[str, Any]:
    """按日期区间切一份浅拷贝的缓存（交易日历和起止变了，其它共享）。"""
    sliced = dict(cache)
    sliced["start"], sliced["end"] = start, end
    sliced["calendar"] = [day for day in cache["calendar"] if start <= day <= end]
    sliced.pop("_closes", None)
    sliced["_closes"] = cache.get("_closes")
    return sliced


def evaluate(cache: Dict[str, Any], variant: Variant, trials: int) -> Dict[str, Any]:
    trades = build_trades(cache, variant)
    if not trades:
        return {"signals": 0, "median": None, "p10": None, "p90": None, "mdd": None, "calmar": None}
    returns = sorted((simulate(trades, cache, variant, seed)["navs"][-1] - 1) * 100.0
                     for seed in range(trials))
    drawdowns = []
    for seed in range(min(trials, 10)):          # 回撤用前 10 条路径的中位数，够用且省时间
        navs = simulate(trades, cache, variant, seed)["navs"]
        drawdowns.append(performance(navs, cache["calendar"])["mdd_pct"])
    years = max((cache["calendar"][-1] - cache["calendar"][0]).days / 365.25, 1e-6)
    median = returns[len(returns) // 2]
    mdd = float(np.median(drawdowns))
    cagr = ((1 + median / 100) ** (1 / years) - 1) * 100
    win = float(np.mean([trade["return_pct"] > 0 for trade in trades]) * 100)
    return {
        "signals": len(trades), "win_pct": round(win, 2),
        "median": round(median, 2), "p10": round(returns[int(trials * 0.1)], 2),
        "p90": round(returns[min(trials - 1, int(trials * 0.9))], 2),
        "mdd": round(mdd, 2), "cagr": round(cagr, 2),
        "calmar": round(cagr / abs(mdd), 3) if mdd < 0 else None,
    }


def grid() -> List[Variant]:
    """主网格：四个互相影响最强的维度全组合，其余先固定在当前默认。"""
    ranges = [(2, 2), (1, 2), (1, 3), (1, 4), (2, 3), (2, 4), (3, 4)]
    variants = []
    for (low, high), z_min, lookback, positions in itertools.product(
            ranges, (None, 0.0, 0.5, 1.0, 1.5), (1, 3, 5), (5, 8, 10)):
        key = f"h{low}{high}_z{'off' if z_min is None else z_min}_f{lookback}_p{positions}"
        variants.append(replace(
            BASE, key=key, label=key, buy_high_min=low, buy_high_max=high,
            volume_z_min=z_min, fear_lookback=lookback, max_positions=positions))
    return variants


def refine(best: Variant) -> List[Variant]:
    """围着主网格的赢家再扫其余维度，一次只动一个。"""
    variants = [replace(best, key="winner", label="主网格赢家")]

    def add(key: str, **changes: Any) -> None:
        variants.append(replace(best, key=key, label=key, **changes))

    for value in (30.0, 35.0, 40.0, 45.0, 50.0):
        add(f"fear{value:g}", fear_threshold=value)
    for value in (10, 20, 40, 60):
        add(f"vollook{value}", volume_lookback=value)
    for low, high in ((1, 2), (2, 2), (2, 3), (1, 3)):
        add(f"sect{low}{high}", sector_high_min=low, sector_high_max=high)
    for value in (7, 8, 9, 10, 11, 12):
        add(f"low{value}", low_count_min=value)
    for value in (0, 1, 2, 3, 5):
        add(f"arm{value}", arm_window=value)
    for value in (1.5, 2.0, 2.5, 3.0):
        add(f"exitatr{value:g}", exit_atr=value)
    for value in (2, 3):
        add(f"exitlow{value}", exit_low_count=value)
    add("exit_nohigh9", require_high9=False)
    for value in ("default54", "all64", "industry"):
        add(f"sect_{value}", sectors=value)
    for value in (3, 4, 5, 6, 8, 10, 12):
        add(f"pos{value}", max_positions=value)
    return variants


def run(cache: Dict[str, Any], variants: Sequence[Variant], trials: int,
        tune_end: date, full_end: date, label: str) -> pd.DataFrame:
    tune = _slice(cache, cache["start"], tune_end)
    hold = _slice(cache, tune_end, full_end)
    rows = []
    for index, variant in enumerate(variants, start=1):
        inside = evaluate(tune, variant, trials)
        outside = evaluate(hold, variant, trials)
        rows.append({
            "key": variant.key,
            **{name: getattr(variant, name) for name in
               ("buy_high_min", "buy_high_max", "sector_high_min", "sector_high_max",
                "volume_z_min", "fear_lookback", "fear_threshold", "volume_lookback",
                "low_count_min", "arm_window", "exit_atr", "max_positions", "sectors")},
            "in_signals": inside["signals"], "in_median": inside["median"],
            "in_mdd": inside["mdd"], "in_calmar": inside["calmar"],
            "out_median": outside["median"], "out_mdd": outside["mdd"], "out_calmar": outside["calmar"],
            "win_pct": inside.get("win_pct"),
        })
        if index % 25 == 0 or index == len(variants):
            print(f"  [{label}] {index}/{len(variants)}")
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=25)
    parser.add_argument("--stage", default="grid", choices=("grid", "refine"))
    parser.add_argument("--winner", default=None, help="refine 阶段用的起点（grid 结果里的 key）")
    args = parser.parse_args()

    with CACHE_PATH.open("rb") as handle:
        cache = pickle.load(handle)
    full_end = cache["end"]
    print(f"[info] 调参段 {cache['start']} ~ {SPLIT_DATE}，检验段 {SPLIT_DATE} ~ {full_end}")

    if args.stage == "grid":
        variants = grid()
        print(f"[info] 主网格 {len(variants)} 组")
        frame = run(cache, variants, args.trials, SPLIT_DATE, full_end, "grid")
        frame.to_csv(DEFAULT_OUTPUT / "param_search_grid.csv", index=False)
    else:
        source = pd.read_csv(DEFAULT_OUTPUT / "param_search_grid.csv")
        row = (source[source["key"] == args.winner] if args.winner
               else source.sort_values("in_calmar", ascending=False)).iloc[0]
        best = replace(
            BASE, key="best", buy_high_min=int(row["buy_high_min"]), buy_high_max=int(row["buy_high_max"]),
            volume_z_min=None if pd.isna(row["volume_z_min"]) else float(row["volume_z_min"]),
            fear_lookback=int(row["fear_lookback"]), max_positions=int(row["max_positions"]))
        print(f"[info] 以 {row['key']} 为起点细调")
        frame = run(cache, refine(best), args.trials, SPLIT_DATE, full_end, "refine")
        frame.to_csv(DEFAULT_OUTPUT / "param_search_refine.csv", index=False)

    pd.set_option("display.width", 250)
    ranked = frame.sort_values("in_calmar", ascending=False)
    print(ranked.head(25).to_string(index=False))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""组合层合并：把板块九转和选股系统的日净值按固定权重混合，检验风险收益能不能同时变好。

规则层的合并（把选股系统的基本面池、评分、出场塞进板块九转）在
``sector_nine_turn_stock_system_merge.py`` 里逐条试过，全部更差。这里试的是另一种合并：
两条腿各跑各的、各分一部分资金、按日再平衡。

板块九转那一腿不用单条确定性净值，而是跑 N 条随机挑股路径，对每条路径都和选股系统做配对比较——
"混合更好"必须在大部分路径上成立，否则只是挑股运气。

先跑一次 ``sector_nine_turn_stock_system_merge.py`` 生成数据缓存，本脚本直接复用。
"""

from __future__ import annotations

import argparse
import pickle
import sqlite3
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Sequence

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT), str(ROOT / "backend")):
    if path not in sys.path:
        sys.path.insert(0, path)

from research.sector_nine_turn_stock_system_merge import (  # noqa: E402
    BASE,
    CACHE_PATH,
    DEFAULT_OUTPUT,
    DEFAULT_SQLITE_DB,
    build_trades,
    performance,
    simulate,
)

STOCK_SYSTEM_RUN_ID = 3          # 已有的 2022-07 起逐周调仓回测
PARTNERS = ("sentiment", "technical")
WEIGHTS = (0.5, 0.6)
DEFAULT_TRIALS = 60


def _stock_system_navs(sqlite_db: str, calendar: Sequence[date]) -> pd.DataFrame:
    connection = sqlite3.connect(f"file:{Path(sqlite_db).resolve()}?mode=ro", uri=True)
    try:
        frame = pd.read_sql_query(
            "SELECT variant, trade_date, nav FROM stock_system_backtest_navs WHERE run_id = ?",
            connection, params=[STOCK_SYSTEM_RUN_ID], parse_dates=["trade_date"])
    finally:
        connection.close()
    navs = frame.pivot(index="trade_date", columns="variant", values="nav")
    navs = navs.reindex(pd.DatetimeIndex(calendar)).ffill().bfill()
    return navs / navs.iloc[0]


def run(sqlite_db: str, trials: int, max_positions: int) -> pd.DataFrame:
    with CACHE_PATH.open("rb") as handle:
        cache = pickle.load(handle)
    calendar = cache["calendar"]
    variant = replace(BASE, max_positions=max_positions)
    trades = build_trades(cache, variant)
    print(f"板块九转 {max_positions} 仓：{len(trades)} 个信号，{trials} 条随机挑股路径")
    partners = _stock_system_navs(sqlite_db, calendar)
    index = pd.DatetimeIndex(calendar)

    rows: List[Dict[str, Any]] = []
    for seed in range(trials):
        navs = pd.Series(simulate(trades, cache, variant, seed)["navs"], index=index)
        own = navs.pct_change().fillna(0)
        solo = performance(navs.tolist(), calendar)
        row = {"seed": seed, "solo_total": solo["total_pct"], "solo_mdd": solo["mdd_pct"],
               "solo_calmar": solo["calmar"], "solo_vol": solo["vol_pct"]}
        for partner in PARTNERS:
            other = partners[partner].pct_change().fillna(0)
            for weight in WEIGHTS:
                mixed = (1 + (weight * own + (1 - weight) * other)).cumprod()
                stats = performance(mixed.tolist(), calendar)
                key = f"{partner[:4]}_w{int(weight * 100)}"
                row.update({f"{key}_total": stats["total_pct"], f"{key}_mdd": stats["mdd_pct"],
                            f"{key}_calmar": stats["calmar"], f"{key}_vol": stats["vol_pct"]})
        rows.append(row)
    return pd.DataFrame(rows), calendar


def report(frame: pd.DataFrame, calendar: Sequence[date]) -> None:
    groups = [("板块九转 单独", "solo")]
    groups += [(f"混合 {partner[:4]} w={weight}", f"{partner[:4]}_w{int(weight * 100)}")
               for partner in PARTNERS for weight in WEIGHTS]

    def band(prefix: str, metric: str) -> str:
        values = frame[f"{prefix}_{metric}"]
        return f"{values.median():7.1f}[{values.quantile(0.1):6.1f},{values.quantile(0.9):6.1f}]"

    print("\n=== 随机挑股路径上的分布（中位 [10%, 90%]）===")
    print(f"{'':22s} {'总收益%':>24s} {'回撤%':>24s} {'Calmar':>24s} {'波动%':>8s}")
    for label, prefix in groups:
        print(f"{label:22s} {band(prefix, 'total'):>24s} {band(prefix, 'mdd'):>24s} "
              f"{band(prefix, 'calmar'):>24s} {frame[f'{prefix}_vol'].median():7.1f}")

    print("\n=== 逐条路径配对比较：混合 vs 板块九转单独 ===")
    for label, prefix in groups[1:]:
        print(f"{label:22s} Calmar 更好 {(frame[f'{prefix}_calmar'] > frame['solo_calmar']).mean() * 100:5.1f}%"
              f"   回撤更浅 {(frame[f'{prefix}_mdd'] > frame['solo_mdd']).mean() * 100:5.1f}%"
              f"   总收益更高 {(frame[f'{prefix}_total'] > frame['solo_total']).mean() * 100:5.1f}%")

    years = (calendar[-1] - calendar[0]).days / 365.25
    target = frame["solo_vol"].median()
    print(f"\n=== 把波动都拉平到 {target:.1f}% 之后的年化 ===")
    for label, prefix in groups:
        cagr = ((1 + frame[f"{prefix}_total"] / 100) ** (1 / years) - 1) * 100
        print(f"{label:22s} 原年化 {cagr.median():6.2f}%  波动 {frame[f'{prefix}_vol'].median():5.1f}%"
              f"  → 拉平后 {(cagr * target / frame[f'{prefix}_vol']).median():6.2f}%")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite-db", default=DEFAULT_SQLITE_DB)
    parser.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    parser.add_argument("--max-positions", type=int, default=5)
    args = parser.parse_args()
    if not CACHE_PATH.exists():
        raise SystemExit(f"缺少数据缓存 {CACHE_PATH}，先跑一次 sector_nine_turn_stock_system_merge.py")
    frame, calendar = run(args.sqlite_db, args.trials, args.max_positions)
    report(frame, calendar)
    DEFAULT_OUTPUT.mkdir(parents=True, exist_ok=True)
    frame.to_csv(DEFAULT_OUTPUT / "blend_robustness.csv", index=False)
    print(f"\n明细写入 {DEFAULT_OUTPUT / 'blend_robustness.csv'}")


if __name__ == "__main__":
    main()

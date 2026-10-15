#!/usr/bin/env python3
"""Search one common SZDT rule across every ETF with usable daily history."""
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import replace
from itertools import product
from pathlib import Path

import pandas as pd

import szdt_a_etf_hyperparameter_search as search


BUY = (-90, -80, -70, -60, -50)
SELL = (50, 60, 70, 80, 90)
FACTOR = (0.5, 1.0, 2.0, 3.0, 5.0)
VOLUME = (0.0, 1.0, 1.2, 1.3, 1.5)


async def main(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    configured = search.prod_configs()
    universe, histories = await search.fetch_universe_and_histories()
    names = {str(item["code"]).upper(): str(item.get("name") or item["code"]) for item in universe}
    prices = await search.fetch_prices(list(names))

    frames = {}
    for code in sorted(set(names) & set(histories) & set(prices)):
        frame = prices[code].merge(histories[code], on="date", how="inner").dropna().sort_values("date")
        # At least twenty prior sessions are required for a ratio; retain every
        # otherwise usable name so this is truly a full-universe portfolio.
        if len(frame) >= 25:
            frames[code] = frame
    base = {code: configured.get(code, search.Config(code=code, name=names[code])) for code in frames}

    search.MARKETS = {}
    for code, frame in frames.items():
        points = list(frame.itertuples(index=False))
        search.MARKETS[code] = {
            point.date: (
                float(point.open), float(point.close),
                float(point.volume_ratio) if pd.notna(point.volume_ratio) else 0.0,
                float(point.score), points[index + 1].date if index + 1 < len(points) else None,
            )
            for index, point in enumerate(points)
        }
    search.MARKET_DAYS = sorted({day for market in search.MARKETS.values() for day in market})

    rows = []
    codes = tuple(frames)
    for buy, sell, buy_factor, sell_factor, volume in product(BUY, SELL, FACTOR, FACTOR, VOLUME):
        configs = {
            code: replace(base[code], when_buy=buy, when_sell=sell, buy_factor=buy_factor,
                          sell_factor=sell_factor, volume_ratio=volume, max_position=100)
            for code in codes
        }
        result = search.portfolio_backtest(frames, configs, codes, search.START, search.END)
        rows.append({
            "when_buy": buy, "when_sell": sell, "buy_factor": buy_factor,
            "sell_factor": sell_factor, "volume_ratio": volume, **result,
        })
    table = pd.DataFrame(rows).sort_values(
        ["sharpe", "annualized_return", "max_drawdown"], ascending=[False, False, False]
    )
    table.to_csv(output / "uniform_global_grid.csv", index=False)
    best = table.iloc[0].to_dict()
    summary = {
        "coverage": {"szdt_universe": len(universe), "histories": len(histories), "price_histories": len(prices), "included": len(codes), "configured": len(configured)},
        "period": {"start": str(search.START), "end": str(search.END)},
        "grid": {"buy": BUY, "sell": SELL, "buy_factor": FACTOR, "sell_factor": FACTOR, "volume_ratio": VOLUME, "evaluations": len(table)},
        "rule": "all symbols share all searched parameters; max position is removed (cash-only, no leverage); configured fixed amounts are retained and unconfigured symbols use 20k",
        "best": best,
        "top_20": table.head(20).to_dict(orient="records"),
    }
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("/tmp/szdt-a-etf-uniform-grid"))
    args = parser.parse_args()
    asyncio.run(main(args.out))

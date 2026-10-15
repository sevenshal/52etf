#!/usr/bin/env python3
"""Search common fear/greed rules and daily-volume-ratio filters together.

The simulation uses the signal-day close for both orders.  It is useful for
comparing rules, but is more optimistic than a production next-bar execution.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import replace
from itertools import product
from pathlib import Path

import pandas as pd

import szdt_a_etf_hyperparameter_search as search
from szdt_a_etf_close_volume_comparison import run


# Includes the two previously found optima: no-volume (-90/80, factor 7,
# 10k/200k) and ratio 1.2 (-90/80, factor 3, 50k/200k).
BUY_THRESHOLDS = (-95, -90, -85, -80, -70)
SELL_THRESHOLDS = (70, 75, 80, 85, 90)
BUY_FACTORS = (2.0, 3.0, 5.0, 7.0, 10.0)
SELL_FACTORS = (1.0, 3.0, 5.0)
BUY_AMOUNTS = (5_000.0, 10_000.0, 20_000.0, 50_000.0)
SELL_AMOUNTS = (100_000.0, 200_000.0, 300_000.0)
VOLUME_RATIOS = (0.0, 0.5, 0.8, 1.0, 1.1, 1.2, 1.3, 1.5)


async def main(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    configured = search.prod_configs()
    universe, histories = await search.fetch_universe_and_histories()
    names = {str(item["code"]).upper(): str(item.get("name") or item["code"]) for item in universe}
    prices = await search.fetch_prices(list(names))
    frames = {}
    for code in sorted(set(names) & set(histories) & set(prices)):
        frame = prices[code].merge(histories[code], on="date", how="inner").dropna().sort_values("date")
        if len(frame) >= 25:
            frames[code] = frame

    base = {code: configured.get(code, search.Config(code=code, name=names[code])) for code in frames}
    search.MARKETS = {
        code: {
            row.date: (
                float(row.open), float(row.close),
                float(row.volume_ratio) if pd.notna(row.volume_ratio) else 0.0,
                float(row.score),
            )
            for row in frame.itertuples(index=False)
        }
        for code, frame in frames.items()
    }
    search.MARKET_DAYS = sorted({day for market in search.MARKETS.values() for day in market})
    codes = tuple(frames)
    rows = []
    dimensions = product(
        BUY_THRESHOLDS, SELL_THRESHOLDS, BUY_FACTORS, SELL_FACTORS,
        BUY_AMOUNTS, SELL_AMOUNTS, VOLUME_RATIOS,
    )
    for buy, sell, buy_factor, sell_factor, buy_amount, sell_amount, volume_ratio in dimensions:
        configs = {
            code: replace(
                base[code], when_buy=buy, when_sell=sell,
                buy_factor=buy_factor, sell_factor=sell_factor,
                buy_amount=buy_amount, sell_amount=sell_amount,
                volume_ratio=volume_ratio, max_position=100,
            )
            for code in codes
        }
        rows.append({
            "when_buy": buy, "when_sell": sell,
            "buy_factor": buy_factor, "sell_factor": sell_factor,
            "buy_amount": buy_amount, "sell_amount": sell_amount,
            "volume_ratio": volume_ratio,
            **run(configs, codes),
        })

    table = pd.DataFrame(rows).sort_values(
        ["sharpe", "annualized_return", "max_drawdown"], ascending=[False, False, False]
    )
    table.to_csv(output / "uniform_close_volume_ratio_grid.csv", index=False)
    best_by_volume = (
        table.sort_values(["volume_ratio", "sharpe", "annualized_return"], ascending=[True, False, False])
        .groupby("volume_ratio", as_index=False).first().sort_values("sharpe", ascending=False)
    )
    best_by_volume.to_csv(output / "best_by_volume_ratio.csv", index=False)
    summary = {
        "coverage": {
            "szdt_universe": len(universe), "histories": len(histories),
            "price_histories": len(prices), "included": len(codes),
        },
        "period": {"start": str(search.START), "end": str(search.END)},
        "execution": "signal-day close; sells before buys; daily volume ratio filter; cash-only; no single-ETF cap; no EMA/SMA exit",
        "grid": {
            "buy_thresholds": BUY_THRESHOLDS, "sell_thresholds": SELL_THRESHOLDS,
            "buy_factors": BUY_FACTORS, "sell_factors": SELL_FACTORS,
            "buy_amounts": BUY_AMOUNTS, "sell_amounts": SELL_AMOUNTS,
            "volume_ratios": VOLUME_RATIOS, "evaluations": len(table),
        },
        "best": table.iloc[0].to_dict(),
        "best_by_volume_ratio": best_by_volume.to_dict(orient="records"),
        "top_20": table.head(20).to_dict(orient="records"),
    }
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("/tmp/szdt-close-volume-ratio"))
    asyncio.run(main(parser.parse_args().out))

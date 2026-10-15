#!/usr/bin/env python3
"""Boundary refinement for the no-volume, same-close global search."""
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import replace
from itertools import product
from pathlib import Path

import pandas as pd
import szdt_a_etf_hyperparameter_search as search
from szdt_a_etf_uniform_close_no_volume_search import run

BUY = (-99, -98, -95, -92, -90)
SELL = (75, 80, 85)
BUY_FACTOR = (3.0, 5.0, 7.0, 10.0)
SELL_FACTOR = (1.0, 3.0, 5.0, 7.0, 10.0)
BUY_AMOUNT = (10_000.0, 20_000.0, 30_000.0)
SELL_AMOUNT = (100_000.0, 200_000.0, 300_000.0, 500_000.0)

async def main(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    configured = search.prod_configs()
    universe, histories = await search.fetch_universe_and_histories()
    names = {str(item["code"]).upper(): str(item.get("name") or item["code"]) for item in universe}
    prices = await search.fetch_prices(list(names))
    frames = {}
    for code in sorted(set(names) & set(histories) & set(prices)):
        frame = prices[code].merge(histories[code], on="date", how="inner").dropna().sort_values("date")
        if len(frame) >= 2: frames[code] = frame
    base = {code: configured.get(code, search.Config(code=code, name=names[code])) for code in frames}
    search.MARKETS = {code: {row.date: (float(row.open), float(row.close), 0.0, float(row.score)) for row in frame.itertuples(index=False)} for code, frame in frames.items()}
    search.MARKET_DAYS = sorted({day for market in search.MARKETS.values() for day in market})
    codes, rows = tuple(frames), []
    for buy, sell, bf, sf, ba, sa in product(BUY, SELL, BUY_FACTOR, SELL_FACTOR, BUY_AMOUNT, SELL_AMOUNT):
        configs = {code: replace(base[code], when_buy=buy, when_sell=sell, buy_factor=bf, sell_factor=sf, buy_amount=ba, sell_amount=sa, volume_ratio=0.0, max_position=100) for code in codes}
        rows.append({"when_buy": buy, "when_sell": sell, "buy_factor": bf, "sell_factor": sf, "buy_amount": ba, "sell_amount": sa, **run(configs, codes)})
    table = pd.DataFrame(rows).sort_values(["sharpe", "annualized_return", "max_drawdown"], ascending=[False, False, False])
    table.to_csv(output / "refined_grid.csv", index=False)
    summary = {"coverage": {"szdt_universe": len(universe), "price_histories": len(prices), "included": len(codes)}, "execution": "signal-day close; sells before buys; volume ratio disabled; cash-only", "grid": {"buy": BUY, "sell": SELL, "buy_factor": BUY_FACTOR, "sell_factor": SELL_FACTOR, "buy_amount": BUY_AMOUNT, "sell_amount": SELL_AMOUNT, "evaluations": len(table)}, "best": table.iloc[0].to_dict(), "top_20": table.head(20).to_dict(orient="records")}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--out", type=Path, default=Path("/tmp/szdt-close-no-volume-refine")); asyncio.run(main(parser.parse_args().out))

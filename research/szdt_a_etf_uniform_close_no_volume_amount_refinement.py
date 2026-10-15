#!/usr/bin/env python3
"""Refine the low buy-amount region of the same-close/no-volume rule."""
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

BUY_AMOUNT = (2_000.0, 5_000.0, 7_500.0, 10_000.0, 15_000.0)
BUY_FACTOR = (3.0, 5.0, 7.0, 10.0)
SELL_AMOUNT = (100_000.0, 200_000.0, 300_000.0)
SELL_FACTOR = (1.0, 3.0, 5.0, 7.0)

async def main(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    configured = search.prod_configs(); universe, histories = await search.fetch_universe_and_histories()
    names = {str(item["code"]).upper(): str(item.get("name") or item["code"]) for item in universe}
    prices = await search.fetch_prices(list(names)); frames = {}
    for code in sorted(set(names) & set(histories) & set(prices)):
        frame = prices[code].merge(histories[code], on="date", how="inner").dropna().sort_values("date")
        if len(frame) >= 2: frames[code] = frame
    base = {code: configured.get(code, search.Config(code=code, name=names[code])) for code in frames}
    search.MARKETS = {code: {row.date: (float(row.open), float(row.close), 0.0, float(row.score)) for row in frame.itertuples(index=False)} for code, frame in frames.items()}
    search.MARKET_DAYS = sorted({day for market in search.MARKETS.values() for day in market})
    codes, rows = tuple(frames), []
    for bf, sf, ba, sa in product(BUY_FACTOR, SELL_FACTOR, BUY_AMOUNT, SELL_AMOUNT):
        configs = {code: replace(base[code], when_buy=-90, when_sell=80, buy_factor=bf, sell_factor=sf, buy_amount=ba, sell_amount=sa, volume_ratio=0.0, max_position=100) for code in codes}
        rows.append({"when_buy": -90, "when_sell": 80, "buy_factor": bf, "sell_factor": sf, "buy_amount": ba, "sell_amount": sa, **run(configs, codes)})
    table = pd.DataFrame(rows).sort_values(["sharpe", "annualized_return", "max_drawdown"], ascending=[False, False, False])
    table.to_csv(output / "amount_refined_grid.csv", index=False)
    summary = {"coverage": {"szdt_universe": len(universe), "price_histories": len(prices), "included": len(codes)}, "execution": "signal-day close; no volume filter; cash-only", "grid": {"buy_amount": BUY_AMOUNT, "buy_factor": BUY_FACTOR, "sell_amount": SELL_AMOUNT, "sell_factor": SELL_FACTOR, "evaluations": len(table)}, "best": table.iloc[0].to_dict(), "top_20": table.head(20).to_dict(orient="records")}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2)); print(json.dumps(summary, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--out", type=Path, default=Path("/tmp/szdt-close-no-volume-amount")); asyncio.run(main(parser.parse_args().out))

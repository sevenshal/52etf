#!/usr/bin/env python3
"""Full-sample common-rule search with a fixed daily volume ratio of 1.2."""
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


BUY = (-95, -90, -80, -70, -60, -50)
SELL = (50, 60, 70, 80, 90)
FACTOR = (1.0, 2.0, 3.0, 5.0)
AMOUNT = (20_000.0, 50_000.0, 100_000.0, 200_000.0)
VOLUME_RATIO = 1.2


async def main(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    configured = search.prod_configs(); universe, histories = await search.fetch_universe_and_histories()
    names = {str(item["code"]).upper(): str(item.get("name") or item["code"]) for item in universe}
    prices = await search.fetch_prices(list(names)); frames = {}
    for code in sorted(set(names) & set(histories) & set(prices)):
        frame = prices[code].merge(histories[code], on="date", how="inner").dropna().sort_values("date")
        if len(frame) >= 25: frames[code] = frame
    base = {code: configured.get(code, search.Config(code=code, name=names[code])) for code in frames}
    search.MARKETS = {
        code: {row.date: (float(row.open), float(row.close), float(row.volume_ratio) if pd.notna(row.volume_ratio) else 0.0, float(row.score)) for row in frame.itertuples(index=False)}
        for code, frame in frames.items()
    }
    search.MARKET_DAYS = sorted({day for market in search.MARKETS.values() for day in market}); codes, rows = tuple(frames), []
    for buy, sell, buy_factor, sell_factor, buy_amount, sell_amount in product(BUY, SELL, FACTOR, FACTOR, AMOUNT, AMOUNT):
        configs = {code: replace(base[code], when_buy=buy, when_sell=sell, buy_factor=buy_factor, sell_factor=sell_factor, buy_amount=buy_amount, sell_amount=sell_amount, volume_ratio=VOLUME_RATIO, max_position=100) for code in codes}
        rows.append({"when_buy": buy, "when_sell": sell, "buy_factor": buy_factor, "sell_factor": sell_factor, "buy_amount": buy_amount, "sell_amount": sell_amount, **run(configs, codes)})
    table = pd.DataFrame(rows).sort_values(["sharpe", "annualized_return", "max_drawdown"], ascending=[False, False, False])
    table.to_csv(output / "uniform_close_volume_12_grid.csv", index=False)
    summary = {"coverage": {"szdt_universe": len(universe), "histories": len(histories), "price_histories": len(prices), "included": len(codes)}, "period": {"start": str(search.START), "end": str(search.END)}, "execution": "signal-day close; sells before buys; daily volume ratio >= 1.2; cash-only; no single-ETF cap", "grid": {"buy": BUY, "sell": SELL, "factor": FACTOR, "amount": AMOUNT, "evaluations": len(table)}, "best": table.iloc[0].to_dict(), "top_20": table.head(20).to_dict(orient="records")}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2)); print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--out", type=Path, default=Path("/tmp/szdt-close-volume-12")); asyncio.run(main(parser.parse_args().out))

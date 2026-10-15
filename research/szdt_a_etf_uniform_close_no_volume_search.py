#!/usr/bin/env python3
"""Full-sample search: common parameters, no volume filter, signal-day close fills."""
from __future__ import annotations

import argparse
import asyncio
import json
from collections import defaultdict
from dataclasses import replace
from itertools import product
from pathlib import Path

import pandas as pd

import szdt_a_etf_hyperparameter_search as search


BUY = (-95, -90, -80, -70, -60, -50)
SELL = (50, 60, 70, 80, 90)
FACTOR = (1.0, 2.0, 3.0, 5.0)
AMOUNT = (20_000.0, 50_000.0, 100_000.0, 200_000.0)


def run(configs: dict[str, search.Config], codes: tuple[str, ...]) -> dict:
    """Known daily signal is filled at that same session's closing price."""
    cash, shares, nav, trades = search.INITIAL_CAPITAL, defaultdict(int), [], 0
    for day in search.MARKET_DAYS:
        # Sell first so closing proceeds are eligible for same-close buy signals.
        for code in codes:
            row = search.MARKETS[code].get(day)
            if row is None or shares[code] < search.LOT:
                continue
            _, close, _, score = row
            cfg = configs[code]
            if score < cfg.when_sell:
                continue
            factor = min(1.0, max(0.0, (score - cfg.when_sell) / (100 - cfg.when_sell)))
            requested = cfg.sell_amount * 3 ** (factor ** cfg.sell_factor)
            quantity = min(shares[code], max(search.LOT, int(requested / close / search.LOT) * search.LOT))
            gross = quantity * close
            cash += gross - search.fee(gross)
            shares[code] -= quantity
            trades += 1
        for code in codes:
            row = search.MARKETS[code].get(day)
            if row is None:
                continue
            _, close, _, score = row
            cfg = configs[code]
            if score > cfg.when_buy:
                continue
            factor = min(1.0, max(0.0, (cfg.when_buy - score) / (cfg.when_buy + 100)))
            requested = cfg.buy_amount * 3 ** (factor ** cfg.buy_factor)
            quantity = int(min(cash, requested) / close / search.LOT) * search.LOT
            gross = quantity * close
            if quantity >= search.LOT and gross + search.fee(gross) <= cash:
                cash -= gross + search.fee(gross)
                shares[code] += quantity
                trades += 1
        nav.append(cash + sum(shares[code] * search.MARKETS[code][day][1] for code in codes if day in search.MARKETS[code]))
    return search.metric(nav, search.INITIAL_CAPITAL, trades)


async def main(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    configured = search.prod_configs()
    universe, histories = await search.fetch_universe_and_histories()
    names = {str(item["code"]).upper(): str(item.get("name") or item["code"]) for item in universe}
    prices = await search.fetch_prices(list(names))
    frames = {}
    for code in sorted(set(names) & set(histories) & set(prices)):
        frame = prices[code].merge(histories[code], on="date", how="inner").dropna().sort_values("date")
        if len(frame) >= 2:
            frames[code] = frame
    base = {code: configured.get(code, search.Config(code=code, name=names[code])) for code in frames}
    search.MARKETS = {}
    for code, frame in frames.items():
        search.MARKETS[code] = {
            row.date: (float(row.open), float(row.close), 0.0, float(row.score))
            for row in frame.itertuples(index=False)
        }
    search.MARKET_DAYS = sorted({day for market in search.MARKETS.values() for day in market})
    codes = tuple(frames)
    rows = []
    for buy, sell, buy_factor, sell_factor, buy_amount, sell_amount in product(BUY, SELL, FACTOR, FACTOR, AMOUNT, AMOUNT):
        configs = {
            code: replace(base[code], when_buy=buy, when_sell=sell, buy_factor=buy_factor, sell_factor=sell_factor,
                          buy_amount=buy_amount, sell_amount=sell_amount, volume_ratio=0.0, max_position=100)
            for code in codes
        }
        rows.append({"when_buy": buy, "when_sell": sell, "buy_factor": buy_factor, "sell_factor": sell_factor,
                     "buy_amount": buy_amount, "sell_amount": sell_amount, **run(configs, codes)})
    table = pd.DataFrame(rows).sort_values(["sharpe", "annualized_return", "max_drawdown"], ascending=[False, False, False])
    table.to_csv(output / "uniform_close_no_volume_grid.csv", index=False)
    summary = {
        "coverage": {"szdt_universe": len(universe), "histories": len(histories), "price_histories": len(prices), "included": len(codes)},
        "period": {"start": str(search.START), "end": str(search.END)},
        "execution": "signal-day close; sells before buys; no volume filter; no single-ETF cap; cash-only",
        "grid": {"buy": BUY, "sell": SELL, "factor": FACTOR, "amount": AMOUNT, "evaluations": len(table)},
        "best": table.iloc[0].to_dict(), "top_20": table.head(20).to_dict(orient="records"),
    }
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("/tmp/szdt-close-no-volume"))
    asyncio.run(main(parser.parse_args().out))

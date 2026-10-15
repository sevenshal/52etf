#!/usr/bin/env python3
"""Exact common-rule search for the top two price-Sharpe ETFs per correlation cluster.

Clusters use daily price returns only.  Clusters with three or more members
contribute their top two buy-and-hold Sharpe ETFs (on common price dates);
smaller clusters contribute their top ETF.  This script searches one common
strategy for the resulting candidate basket, without EMA5 sell confirmation.
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


CODES = (
    "SH.515050", "SH.512480", "SH.510880", "SH.513120", "SH.513060", "SZ.159928",
    "SH.512800", "SH.512880", "SH.513000", "SH.513030", "SZ.159920", "SH.513130",
    "SZ.159502", "SZ.159509", "SZ.159941", "SH.513630", "SH.513730", "SH.515030",
    "SH.515230", "SH.515790", "SH.518880", "SH.520830", "SH.520870", "SZ.159518",
    "SZ.159529", "SZ.159687", "SZ.159865", "SZ.159869",
)
BUY = (-90, -85, -80, -75, -70)
SELL = (70, 75, 80, 85, 90)
FACTOR = (1.0, 2.0, 3.0, 4.0, 5.0)
AMOUNT = (10_000, 20_000, 30_000, 40_000, 50_000, 60_000, 70_000, 80_000, 90_000, 100_000)
VOLUME = (1.2, 1.3, 1.4)


async def main(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    configured = search.prod_configs()
    universe, histories = await search.fetch_universe_and_histories()
    names = {str(item["code"]).upper(): str(item.get("name") or item["code"]) for item in universe}
    prices = await search.fetch_prices(list(CODES))
    for _ in range(3):
        missing = [code for code in CODES if code not in prices]
        if not missing:
            break
        await asyncio.sleep(1)
        prices.update(await search.fetch_prices(missing))
    for code in [code for code in CODES if code not in prices]:
        prices.update(await search.fetch_prices([code]))

    frames = {}
    for code in CODES:
        if code not in histories or code not in prices:
            continue
        frame = prices[code].merge(histories[code], on="date", how="inner").dropna().sort_values("date")
        if len(frame) >= 80:
            frames[code] = frame
    if set(frames) != set(CODES):
        raise RuntimeError(f"missing selected histories: {sorted(set(CODES) - set(frames))}")
    base = {code: configured.get(code, search.Config(code=code, name=names[code])) for code in CODES}
    search.MARKETS = {}
    for code, frame in frames.items():
        points = list(frame.itertuples(index=False))
        search.MARKETS[code] = {
            point.date: (float(point.open), float(point.close), float(point.volume_ratio) if pd.notna(point.volume_ratio) else 0.0,
                         float(point.score), points[index + 1].date if index + 1 < len(points) else None)
            for index, point in enumerate(points)
        }
    search.MARKET_DAYS = sorted({day for market in search.MARKETS.values() for day in market})

    rows = []
    for buy, sell, buy_factor, sell_factor, buy_amount, sell_amount, volume_ratio in product(BUY, SELL, FACTOR, FACTOR, AMOUNT, AMOUNT, VOLUME):
        configs = {
            code: replace(base[code], when_buy=buy, when_sell=sell, buy_factor=buy_factor, sell_factor=sell_factor,
                          buy_amount=buy_amount, sell_amount=sell_amount, volume_ratio=volume_ratio, max_position=100)
            for code in CODES
        }
        result = search.portfolio_backtest(frames, configs, CODES, search.START, search.END)
        rows.append({"when_buy": buy, "when_sell": sell, "buy_factor": buy_factor, "sell_factor": sell_factor,
                     "buy_amount": buy_amount, "sell_amount": sell_amount, "volume_ratio": volume_ratio, **result})
    table = pd.DataFrame(rows).sort_values(["sharpe", "annualized_return", "max_drawdown"], ascending=[False, False, False])
    table.to_csv(output / "top2_cluster_uniform_grid.csv", index=False)
    summary = {"coverage": {"selected": len(CODES), "loaded": len(frames), "codes": list(CODES)},
               "period": {"start": str(search.START), "end": str(search.END)},
               "rules": {"sell_on_ema5_breakdown": False, "max_position": "none (cash-only)", "initial_capital": search.INITIAL_CAPITAL},
               "grid": {"buy": BUY, "sell": SELL, "factor": FACTOR, "amount": AMOUNT, "volume_ratio": VOLUME, "evaluations": len(table)},
               "best": table.iloc[0].to_dict(), "top_20": table.head(20).to_dict(orient="records")}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("/tmp/szdt-a-etf-top2-cluster-search"))
    asyncio.run(main(parser.parse_args().out))

#!/usr/bin/env python3
"""Comparable same-close results for the prior volume rule and new no-volume rule."""
from __future__ import annotations

import argparse
import asyncio
import json
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

import szdt_a_etf_hyperparameter_search as search


RULES = {
    "prior_volume_1_2": dict(when_buy=-90, when_sell=70, buy_factor=5.0, sell_factor=1.0, buy_amount=20_000.0, sell_amount=20_000.0, volume_ratio=1.2),
    "no_volume_best": dict(when_buy=-90, when_sell=80, buy_factor=7.0, sell_factor=1.0, buy_amount=10_000.0, sell_amount=200_000.0, volume_ratio=0.0),
}


def run(configs: dict, codes: tuple[str, ...]) -> dict:
    cash, shares, nav, trades = search.INITIAL_CAPITAL, defaultdict(int), [], 0
    for day in search.MARKET_DAYS:
        for code in codes:  # close: sell first
            row = search.MARKETS[code].get(day)
            if row is None or shares[code] < search.LOT: continue
            _, close, _, score = row; cfg = configs[code]
            if score < cfg.when_sell: continue
            factor = min(1.0, max(0.0, (score - cfg.when_sell) / (100 - cfg.when_sell)))
            quantity = min(shares[code], max(search.LOT, int((cfg.sell_amount * 3 ** (factor ** cfg.sell_factor)) / close / search.LOT) * search.LOT))
            gross = quantity * close; cash += gross - search.fee(gross); shares[code] -= quantity; trades += 1
        for code in codes:
            row = search.MARKETS[code].get(day)
            if row is None: continue
            _, close, ratio, score = row; cfg = configs[code]
            if score > cfg.when_buy or (cfg.volume_ratio > 0 and ratio < cfg.volume_ratio): continue
            factor = min(1.0, max(0.0, (cfg.when_buy - score) / (cfg.when_buy + 100)))
            quantity = int(min(cash, cfg.buy_amount * 3 ** (factor ** cfg.buy_factor)) / close / search.LOT) * search.LOT
            gross = quantity * close
            if quantity >= search.LOT and gross + search.fee(gross) <= cash:
                cash -= gross + search.fee(gross); shares[code] += quantity; trades += 1
        nav.append(cash + sum(shares[code] * search.MARKETS[code][day][1] for code in codes if day in search.MARKETS[code]))
    return search.metric(nav, search.INITIAL_CAPITAL, trades)


async def main(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    configured = search.prod_configs(); universe, histories = await search.fetch_universe_and_histories()
    names = {str(row["code"]).upper(): str(row.get("name") or row["code"]) for row in universe}; prices = await search.fetch_prices(list(names))
    frames = {code: prices[code].merge(histories[code], on="date", how="inner").dropna().sort_values("date") for code in sorted(set(names) & set(histories) & set(prices))}
    frames = {code: frame for code, frame in frames.items() if len(frame) >= 25}
    base = {code: configured.get(code, search.Config(code=code, name=names[code])) for code in frames}
    search.MARKETS = {code: {row.date: (float(row.open), float(row.close), float(row.volume_ratio) if row.volume_ratio == row.volume_ratio else 0.0, float(row.score)) for row in frame.itertuples(index=False)} for code, frame in frames.items()}
    search.MARKET_DAYS = sorted({day for market in search.MARKETS.values() for day in market}); codes = tuple(frames)
    results = []
    for label, values in RULES.items():
        configs = {code: replace(base[code], max_position=100, **values) for code in codes}
        results.append({"rule": label, **values, **run(configs, codes)})
    summary = {"coverage": {"szdt_universe": len(universe), "price_histories": len(prices), "included": len(codes)}, "execution": "signal-day close; sells before buys; cash-only", "results": results}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2)); print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--out", type=Path, default=Path("/tmp/szdt-close-comparison")); asyncio.run(main(parser.parse_args().out))

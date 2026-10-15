#!/usr/bin/env python3
"""Compare fixed sell amounts and EMA5/SMA5 greed exits for the uniform rule."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

import pandas as pd

import szdt_a_etf_hyperparameter_search as search

BUY_AMOUNT = 20_000.0
SELL_AMOUNTS = (20_000.0, 50_000.0, 100_000.0, 200_000.0)
WHEN_BUY, WHEN_SELL, BUY_FACTOR, SELL_FACTOR, VOLUME_RATIO = -90, 70, 5.0, 1.0, 1.2


def run(frames: dict[str, pd.DataFrame], configs: dict, mode: str) -> dict:
    days = search.MARKET_DAYS
    cash, shares, pending, nav, trades = search.INITIAL_CAPITAL, defaultdict(int), defaultdict(list), [], 0
    for day in days:
        for code, side, score in pending.pop(day, []):
            row = search.MARKETS[code].get(day)
            if row is None:
                continue
            open_price = row[0]
            equity = cash + sum(shares[item] * search.MARKETS[item][day][1] for item in configs if day in search.MARKETS[item])
            cfg = configs[code]
            if side == "buy":
                factor = min(1.0, max(0.0, (cfg.when_buy - score) / (cfg.when_buy + 100)))
                requested = cfg.buy_amount * 3 ** (factor ** cfg.buy_factor)
                quantity = int(min(cash, requested) / open_price / search.LOT) * search.LOT
                gross = quantity * open_price
                if quantity >= search.LOT and gross + search.fee(gross) <= cash:
                    cash -= gross + search.fee(gross); shares[code] += quantity; trades += 1
            elif side == "sell" and shares[code] >= search.LOT:
                factor = min(1.0, max(0.0, (score - cfg.when_sell) / (100 - cfg.when_sell)))
                requested = cfg.sell_amount * 3 ** (factor ** cfg.sell_factor)
                quantity = min(shares[code], max(search.LOT, int(requested / open_price / search.LOT) * search.LOT))
                gross = quantity * open_price
                cash += gross - search.fee(gross); shares[code] -= quantity; trades += 1
        for code, cfg in configs.items():
            row = search.MARKETS[code].get(day)
            if row is None or row[4] is None:
                continue
            open_price, close, ratio, score, next_day, ema5, sma5 = row
            if score <= cfg.when_buy and ratio >= cfg.volume_ratio:
                pending[next_day].append((code, "buy", score))
            elif score >= cfg.when_sell:
                ma = ema5 if mode == "ema5" else sma5 if mode == "sma5" else None
                if ma is None or close < ma:
                    pending[next_day].append((code, "sell", score))
        equity = cash + sum(shares[item] * search.MARKETS[item][day][1] for item in configs if day in search.MARKETS[item])
        nav.append(equity)
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
        if len(frame) >= 25:
            frame["ema5"] = frame.close.ewm(span=5, adjust=False).mean()
            frame["sma5"] = frame.close.rolling(5, min_periods=5).mean()
            frames[code] = frame
    base = {code: configured.get(code, search.Config(code=code, name=names[code])) for code in frames}
    search.MARKETS = {}
    for code, frame in frames.items():
        points = list(frame.itertuples(index=False))
        search.MARKETS[code] = {
            point.date: (float(point.open), float(point.close), float(point.volume_ratio) if pd.notna(point.volume_ratio) else 0.0,
                         float(point.score), points[index + 1].date if index + 1 < len(points) else None,
                         float(point.ema5) if pd.notna(point.ema5) else None, float(point.sma5) if pd.notna(point.sma5) else None)
            for index, point in enumerate(points)
        }
    search.MARKET_DAYS = sorted({day for market in search.MARKETS.values() for day in market})
    rows = []
    for mode in ("none", "ema5", "sma5"):
        amounts = (20_000.0,) if mode == "none" else SELL_AMOUNTS
        for sell_amount in amounts:
            configs = {code: replace(base[code], buy_amount=BUY_AMOUNT, sell_amount=sell_amount, when_buy=WHEN_BUY, when_sell=WHEN_SELL, buy_factor=BUY_FACTOR, sell_factor=SELL_FACTOR, volume_ratio=VOLUME_RATIO, max_position=100) for code in frames}
            rows.append({"exit_confirmation": mode, "buy_amount": BUY_AMOUNT, "sell_amount": sell_amount, **run(frames, configs, mode)})
    table = pd.DataFrame(rows).sort_values(["sharpe", "annualized_return"], ascending=False)
    table.to_csv(output / "ma_exit_comparison.csv", index=False)
    summary = {"coverage": {"szdt_universe": len(universe), "price_histories": len(prices), "included": len(frames)}, "rule": {"when_buy": WHEN_BUY, "when_sell": WHEN_SELL, "buy_factor": BUY_FACTOR, "sell_factor": SELL_FACTOR, "volume_ratio": VOLUME_RATIO, "buy_amount": BUY_AMOUNT, "max_position": "none / cash-only"}, "results": table.to_dict(orient="records")}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("/tmp/szdt-ma-exit"))
    asyncio.run(main(parser.parse_args().out))

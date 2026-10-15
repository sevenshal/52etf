#!/usr/bin/env python3
"""Search cash-funded fear/greed rotation rules for the full SZDT A-ETF set."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
from collections import defaultdict
from dataclasses import replace
from itertools import product
from pathlib import Path

import pandas as pd

import szdt_a_etf_hyperparameter_search as search


BUY_THRESHOLDS = (-90, -85)
SELL_THRESHOLDS = (75, 80, 85, 90)
BUY_FACTORS = (1.0, 2.0, 3.0, 5.0)
BUY_AMOUNTS = (10_000.0, 20_000.0, 50_000.0, 100_000.0, 200_000.0, 500_000.0)
SELL_AMOUNTS = (10_000.0, 20_000.0, 50_000.0, 100_000.0, 200_000.0, 500_000.0)
VOLUME_RATIOS = (0.0, 0.8, 1.2)
# 101 is the explicit "rotation disabled" option: SZDT scores top out at 100.
ROTATE_FROM_SCORES = (101, -30, 0, 30, 60)
SLIPPAGE = 0.001  # 10bp each side, consistent with the system backtest default.


def buy_request(cfg, close: float, score: float) -> tuple[int, float]:
    factor = min(1.0, max(0.0, (cfg.when_buy - score) / (cfg.when_buy + 100)))
    requested = cfg.buy_amount * 3 ** (factor ** cfg.buy_factor)
    quantity = int(requested / close / search.LOT) * search.LOT
    return quantity, quantity * close


def run_rotation(configs: dict, codes: tuple[str, ...], rotate_from_score: float) -> dict:
    """Same-close simulation: normal sells, then fearful buys, rotating if needed.

    When cash cannot fund a buy order, only *other* holdings whose current
    fear/greed score is at least ``rotate_from_score`` are sold.  They are
    ranked from most greedy to least greedy and only enough lots are sold to
    fund the requested buy plus its fee.
    """
    cash, shares, nav, trades, rotation_trades = search.INITIAL_CAPITAL, defaultdict(int), [], 0, 0
    for day in search.MARKET_DAYS:
        # First, execute ordinary greed exits exactly as in the non-rotation rule.
        for code in codes:
            row = search.MARKETS[code].get(day)
            if row is None or shares[code] < search.LOT:
                continue
            _, close, _, score = row
            cfg = configs[code]
            if score < cfg.when_sell:
                continue
            factor = min(1.0, max(0.0, (score - cfg.when_sell) / (100 - cfg.when_sell)))
            quantity = min(
                shares[code],
                max(search.LOT, int((cfg.sell_amount * 3 ** factor) / close / search.LOT) * search.LOT),
            )
            gross = quantity * close * (1 - SLIPPAGE)
            cash += gross - search.fee(gross)
            shares[code] -= quantity
            trades += 1

        # More fearful candidates get the available/rotated cash first.
        candidates = []
        for code in codes:
            row = search.MARKETS[code].get(day)
            if row is None:
                continue
            _, close, ratio, score = row
            cfg = configs[code]
            if score <= cfg.when_buy and (cfg.volume_ratio <= 0 or ratio >= cfg.volume_ratio):
                candidates.append((score, code, close))

        for score, code, close in sorted(candidates):
            cfg = configs[code]
            quantity, gross = buy_request(cfg, close, score)
            if quantity < search.LOT:
                continue
            required = gross + search.fee(gross)
            if cash < required:
                # Holdings without a current quote cannot be liquidated here.
                sources = []
                for source in codes:
                    if source == code or shares[source] < search.LOT:
                        continue
                    source_row = search.MARKETS[source].get(day)
                    if source_row is None:
                        continue
                    _, source_close, _, source_score = source_row
                    if source_score >= rotate_from_score:
                        sources.append((source_score, source, source_close))
                for _, source, source_close in sorted(sources, reverse=True):
                    shortfall = required - cash
                    if shortfall <= 0:
                        break
                    # Leave a small fee cushion and always trade whole lots.
                    quantity_to_sell = math.ceil(shortfall / (source_close * search.LOT)) * search.LOT
                    quantity_to_sell = min(shares[source], max(search.LOT, quantity_to_sell))
                    proceeds = quantity_to_sell * source_close * (1 - SLIPPAGE)
                    cash += proceeds - search.fee(proceeds)
                    shares[source] -= quantity_to_sell
                    trades += 1
                    rotation_trades += 1
            # If all suitable holdings were exhausted, take the largest valid partial order.
            affordable = int(min(cash, gross) / (close * (1 + SLIPPAGE)) / search.LOT) * search.LOT
            buy_quantity = min(quantity, affordable)
            buy_gross = buy_quantity * close * (1 + SLIPPAGE)
            while buy_quantity >= search.LOT and buy_gross + search.fee(buy_gross) > cash:
                buy_quantity -= search.LOT
                buy_gross = buy_quantity * close * (1 + SLIPPAGE)
            if buy_quantity >= search.LOT and buy_gross + search.fee(buy_gross) <= cash:
                cash -= buy_gross + search.fee(buy_gross)
                shares[code] += buy_quantity
                trades += 1
        nav.append(cash + sum(shares[code] * search.MARKETS[code][day][1] for code in codes if day in search.MARKETS[code]))
    return {**search.metric(nav, search.INITIAL_CAPITAL, trades), "rotation_trades": rotation_trades}


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
    if len(frames) < 53:
        raise RuntimeError(f"incomplete price coverage: {len(frames)} / 53 expected")

    base = {code: configured.get(code, search.Config(code=code, name=names[code])) for code in frames}
    search.MARKETS = {
        code: {
            row.date: (float(row.open), float(row.close), float(row.volume_ratio) if pd.notna(row.volume_ratio) else 0.0, float(row.score))
            for row in frame.itertuples(index=False)
        }
        for code, frame in frames.items()
    }
    search.MARKET_DAYS = sorted({day for market in search.MARKETS.values() for day in market})
    codes, rows = tuple(frames), []
    for buy, sell, buy_factor, buy_amount, sell_amount, ratio, rotate_from in product(
        BUY_THRESHOLDS, SELL_THRESHOLDS, BUY_FACTORS, BUY_AMOUNTS,
        SELL_AMOUNTS, VOLUME_RATIOS, ROTATE_FROM_SCORES,
    ):
        configs = {
            code: replace(
                base[code], when_buy=buy, when_sell=sell, buy_factor=buy_factor,
                sell_factor=1.0, buy_amount=buy_amount, sell_amount=sell_amount,
                volume_ratio=ratio, max_position=100,
            ) for code in codes
        }
        result = run_rotation(configs, codes, rotate_from)
        max_drawdown = abs(float(result["max_drawdown"]))
        calmar = float(result["annualized_return"]) / max_drawdown if max_drawdown else 0.0
        rows.append({
            "when_buy": buy, "when_sell": sell, "buy_factor": buy_factor,
            "buy_amount": buy_amount, "sell_amount": sell_amount,
            "volume_ratio": ratio, "rotation_enabled": rotate_from <= 100,
            "rotate_from_score": None if rotate_from == 101 else rotate_from,
            "calmar": calmar, **result,
        })
    table = pd.DataFrame(rows).sort_values(["calmar", "annualized_return", "sharpe"], ascending=[False, False, False])
    table.to_csv(output / "uniform_close_rotation_grid.csv", index=False)
    best_by_rotation = table.sort_values(["rotation_enabled", "rotate_from_score", "calmar"], ascending=[True, True, False]).groupby(["rotation_enabled", "rotate_from_score"], dropna=False, as_index=False).first()
    best_by_rotation.to_csv(output / "best_by_rotation_threshold.csv", index=False)
    summary = {
        "coverage": {"szdt_universe": len(universe), "histories": len(histories), "price_histories": len(prices), "included": len(codes)},
        "period": {"start": str(search.START), "end": str(search.END)},
        "execution": "signal-day close; 10bp slippage each side; normal greed exits then buys ordered by lowest score; cash shortage rotates other holdings from highest to lowest score; cash-only; no single-ETF cap; no EMA/SMA exit",
        "grid": {"buy_thresholds": BUY_THRESHOLDS, "sell_thresholds": SELL_THRESHOLDS, "buy_factors": BUY_FACTORS, "buy_amounts": BUY_AMOUNTS, "sell_amounts": SELL_AMOUNTS, "volume_ratios": VOLUME_RATIOS, "rotate_from_scores": [None, -30, 0, 30, 60], "evaluations": len(table)},
        "best": table.iloc[0].to_dict(), "best_by_rotation_threshold": best_by_rotation.to_dict(orient="records"), "top_20": table.head(20).to_dict(orient="records"),
    }
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("/tmp/szdt-close-rotation"))
    asyncio.run(main(parser.parse_args().out))

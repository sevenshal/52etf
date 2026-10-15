#!/usr/bin/env python3
"""EMA5-confirmed sell comparison for the fixed 15-ETF SZDT strategy.

The sell timing matches ``run_szdt_a_stock_backtest``: a sell is queued for
the next open only when that day's emotion is greedy enough *and* the daily
close is below its EMA5.  It does not carry a previous greed signal forward.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pandas as pd

import szdt_a_etf_hyperparameter_search as search
from szdt_a_etf_15_per_etf_volume_search import COMMON, VOLUME_CANDIDATES
from szdt_a_etf_15_uniform_strategy_search import CODES


def asset_backtest_ema5(frame: pd.DataFrame, cfg: search.Config) -> dict[str, float]:
    frame = frame[(frame.date >= search.START) & (frame.date <= search.END)].reset_index(drop=True)
    cash, shares, nav, trades, pending = 100_000.0, 0, [], 0, None
    for idx, row in frame.iterrows():
        open_price, close = float(row.open), float(row.close)
        if pending == "buy":
            equity = cash + shares * open_price
            if shares * open_price / max(equity, 1) * 100 < cfg.max_position:
                factor = min(1.0, max(0.0, (cfg.when_buy - float(frame.score.iloc[idx - 1])) / (cfg.when_buy + 100)))
                requested = cfg.buy_amount * 3 ** (factor ** cfg.buy_factor)
                quantity = int(min(cash, requested) / open_price / search.LOT) * search.LOT
                gross = quantity * open_price
                if quantity >= search.LOT and gross + search.fee(gross) <= cash:
                    cash -= gross + search.fee(gross); shares += quantity; trades += 1
        elif pending == "sell" and shares >= search.LOT:
            factor = min(1.0, max(0.0, (float(frame.score.iloc[idx - 1]) - cfg.when_sell) / (100 - cfg.when_sell)))
            requested = cfg.sell_amount * 3 ** (factor ** cfg.sell_factor)
            quantity = min(shares, max(search.LOT, int(requested / open_price / search.LOT) * search.LOT))
            gross = quantity * open_price
            cash += gross - search.fee(gross); shares -= quantity; trades += 1
        pending = None
        if idx < len(frame) - 1:
            score = float(row.score)
            if score <= cfg.when_buy and (cfg.volume_ratio <= 0 or float(row.volume_ratio or 0) >= cfg.volume_ratio):
                pending = "buy"
            elif score >= cfg.when_sell and bool(row.close < row.ema5):
                pending = "sell"
        nav.append(cash + shares * close)
    return search.metric(nav, 100_000.0, trades)


def portfolio_backtest_ema5(configs: dict[str, search.Config], codes: tuple[str, ...]) -> dict[str, float]:
    days = [day for day in search.MARKET_DAYS if search.START <= day <= search.END]
    cash, shares, pending, nav, trades = search.INITIAL_CAPITAL, {code: 0 for code in codes}, {}, [], 0
    for day in days:
        for code, side, signal_score in pending.pop(day, []):
            row = search.MARKETS[code].get(day)
            if row is None:
                continue
            price, cfg = row[0], configs[code]
            equity = cash + sum(shares[k] * search.MARKETS[k][day][1] for k in codes if day in search.MARKETS[k])
            if side == "buy" and shares[code] * price / max(equity, 1) * 100 < cfg.max_position:
                factor = min(1, max(0, (cfg.when_buy - signal_score) / (cfg.when_buy + 100)))
                requested = cfg.buy_amount * 3 ** (factor ** cfg.buy_factor)
                qty = int(min(cash, requested) / price / search.LOT) * search.LOT
                gross = qty * price
                if qty >= search.LOT and gross + search.fee(gross) <= cash:
                    cash -= gross + search.fee(gross); shares[code] += qty; trades += 1
            elif side == "sell" and shares[code] >= search.LOT:
                factor = min(1, max(0, (signal_score - cfg.when_sell) / (100 - cfg.when_sell)))
                requested = cfg.sell_amount * 3 ** (factor ** cfg.sell_factor)
                qty = min(shares[code], max(search.LOT, int(requested / price / search.LOT) * search.LOT))
                gross = qty * price
                cash += gross - search.fee(gross); shares[code] -= qty; trades += 1
        for code in codes:
            row = search.MARKETS[code].get(day)
            if row is None or row[4] is None:
                continue
            score, below_ema5 = row[3], row[5]
            cfg = configs[code]
            if score <= cfg.when_buy and (cfg.volume_ratio <= 0 or row[2] >= cfg.volume_ratio):
                pending.setdefault(row[4], []).append((code, "buy", score))
            elif score >= cfg.when_sell and below_ema5:
                pending.setdefault(row[4], []).append((code, "sell", score))
        nav.append(cash + sum(shares[k] * search.MARKETS[k][day][1] for k in codes if day in search.MARKETS[k]))
    return search.metric(nav, search.INITIAL_CAPITAL, trades)


async def main(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    configured = search.prod_configs()
    universe, histories = await search.fetch_universe_and_histories()
    names = {str(item["code"]).upper(): str(item.get("name") or item["code"]) for item in universe}
    prices = await search.fetch_prices(list(CODES))
    for code in CODES:
        if code not in prices:
            prices.update(await search.fetch_prices([code]))
    frames = {}
    for code in CODES:
        if code in histories and code in prices:
            frame = prices[code].merge(histories[code], on="date", how="inner").dropna().sort_values("date")
            frame["ema5"] = frame.close.ewm(span=5, adjust=False).mean()
            if len(frame) >= 80:
                frames[code] = frame
    if set(frames) != set(CODES):
        raise RuntimeError(f"missing selected histories: {sorted(set(CODES) - set(frames))}")
    base = {code: configured.get(code, search.Config(code=code, name=names[code])) for code in CODES}

    rows, selected = [], {}
    for code in CODES:
        candidates = []
        for ratio in VOLUME_CANDIDATES:
            cfg = replace(base[code], volume_ratio=ratio, **COMMON)
            result = asset_backtest_ema5(frames[code], cfg)
            rows.append({"code": code, "name": cfg.name, "volume_ratio": ratio, **result})
            candidates.append((result, cfg))
        comparable = [item for item in candidates if item[0]["trades"] >= 2] or candidates
        selected[code] = max(comparable, key=lambda item: (item[0]["sharpe"], item[0]["annualized_return"], item[0]["max_drawdown"]))[1]
    table = pd.DataFrame(rows).sort_values(["code", "volume_ratio"])
    table.to_csv(output / "per_etf_volume_candidates_ema5.csv", index=False)
    selected_table = table.merge(pd.DataFrame({"code": list(selected), "selected_volume_ratio": [selected[c].volume_ratio for c in selected]}), on="code")
    selected_table = selected_table[selected_table.volume_ratio == selected_table.selected_volume_ratio].drop(columns="selected_volume_ratio")
    selected_table.sort_values(["sharpe", "annualized_return"], ascending=False).to_csv(output / "per_etf_volume_selected_ema5.csv", index=False)

    search.MARKETS = {}
    for code, frame in frames.items():
        points = list(frame.itertuples(index=False))
        search.MARKETS[code] = {p.date: (float(p.open), float(p.close), float(p.volume_ratio) if pd.notna(p.volume_ratio) else 0.0, float(p.score), points[i + 1].date if i + 1 < len(points) else None, bool(p.close < p.ema5)) for i, p in enumerate(points)}
    search.MARKET_DAYS = sorted({day for market in search.MARKETS.values() for day in market})
    portfolios = []
    for ratio in VOLUME_CANDIDATES:
        cfgs = {code: replace(base[code], volume_ratio=ratio, **COMMON) for code in CODES}
        portfolios.append({"variant": f"uniform_{ratio:.1f}", "volume_ratio": ratio, **portfolio_backtest_ema5(cfgs, CODES)})
    portfolios.append({"variant": "per_etf_selected", "volume_ratio": None, **portfolio_backtest_ema5(selected, CODES)})
    portfolio_table = pd.DataFrame(portfolios).sort_values(["sharpe", "annualized_return"], ascending=False)
    portfolio_table.to_csv(output / "portfolio_comparison_ema5.csv", index=False)
    summary = {"coverage": {"selected": len(CODES), "loaded": len(frames)}, "period": {"start": str(search.START), "end": str(search.END)}, "rules": {"sell_on_ema5_breakdown": True, "execution": "next_open", **COMMON}, "volume_candidates": VOLUME_CANDIDATES, "selection": "per-ETF highest full-sample Sharpe; require at least two fills when available", "selected_per_etf": selected_table.to_dict(orient="records"), "portfolio_comparison": portfolio_table.to_dict(orient="records")}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("/tmp/szdt-a-etf-15-ema5-volume"))
    asyncio.run(main(parser.parse_args().out))

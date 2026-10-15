#!/usr/bin/env python3
"""Select an independent daily volume-ratio threshold for each of 15 ETFs.

All other parameters are the uniform full-sample optimum from the prior grid
search.  The script is deliberately read-only: price bars come from the public
API and current emotion history from SZDT.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pandas as pd

import szdt_a_etf_hyperparameter_search as search
from szdt_a_etf_15_uniform_strategy_search import CODES


VOLUME_CANDIDATES = (1.2, 1.3, 1.4)
# The full-sample, portfolio-level Sharpe optimum from the exact 62,500 grid.
COMMON = dict(
    when_buy=-75,
    when_sell=75,
    buy_factor=5.0,
    sell_factor=1.0,
    buy_amount=10_000.0,
    sell_amount=100_000.0,
    max_position=100,
)


async def main(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    configured = search.prod_configs()
    universe, histories = await search.fetch_universe_and_histories()
    names = {str(item["code"]).upper(): str(item.get("name") or item["code"]) for item in universe}
    prices = await search.fetch_prices(list(CODES))
    for code in CODES:
        if code not in prices:
            prices.update(await search.fetch_prices([code]))

    frames: dict[str, pd.DataFrame] = {}
    for code in CODES:
        if code not in histories or code not in prices:
            continue
        frame = prices[code].merge(histories[code], on="date", how="inner").dropna().sort_values("date")
        if len(frame) >= 80:
            frames[code] = frame
    if set(frames) != set(CODES):
        raise RuntimeError(f"missing selected histories: {sorted(set(CODES) - set(frames))}")

    base = {code: configured.get(code, search.Config(code=code, name=names[code])) for code in CODES}
    all_rows: list[dict] = []
    selected: dict[str, search.Config] = {}
    for code in CODES:
        candidates: list[tuple[dict, search.Config]] = []
        for volume_ratio in VOLUME_CANDIDATES:
            cfg = replace(base[code], volume_ratio=volume_ratio, **COMMON)
            result = search.asset_backtest(frames[code], cfg, search.START, search.END)
            row = {"code": code, "name": cfg.name, "volume_ratio": volume_ratio, **result}
            all_rows.append(row)
            candidates.append((result, cfg))
        # A threshold is only comparable if it generated at least two fills.
        comparable = [item for item in candidates if item[0]["trades"] >= 2] or candidates
        result, cfg = max(
            comparable,
            key=lambda item: (item[0]["sharpe"], item[0]["annualized_return"], item[0]["max_drawdown"]),
        )
        selected[code] = cfg

    table = pd.DataFrame(all_rows).sort_values(["code", "volume_ratio"])
    table.to_csv(output / "per_etf_volume_candidates.csv", index=False)
    selected_table = table.merge(
        pd.DataFrame({"code": list(selected), "selected_volume_ratio": [selected[c].volume_ratio for c in selected]}),
        on="code",
    )
    selected_table = selected_table[selected_table.volume_ratio == selected_table.selected_volume_ratio].drop(columns="selected_volume_ratio")
    selected_table = selected_table.sort_values(["sharpe", "annualized_return"], ascending=False)
    selected_table.to_csv(output / "per_etf_volume_selected.csv", index=False)

    search.MARKETS = {}
    for code, frame in frames.items():
        points = list(frame.itertuples(index=False))
        search.MARKETS[code] = {
            point.date: (
                float(point.open), float(point.close),
                float(point.volume_ratio) if pd.notna(point.volume_ratio) else 0.0,
                float(point.score), points[index + 1].date if index + 1 < len(points) else None,
            )
            for index, point in enumerate(points)
        }
    search.MARKET_DAYS = sorted({day for market in search.MARKETS.values() for day in market})

    portfolio_rows = []
    for volume_ratio in VOLUME_CANDIDATES:
        configs = {code: replace(base[code], volume_ratio=volume_ratio, **COMMON) for code in CODES}
        portfolio_rows.append({"variant": f"uniform_{volume_ratio:.1f}", "volume_ratio": volume_ratio,
                               **search.portfolio_backtest(frames, configs, CODES, search.START, search.END)})
    portfolio_rows.append({"variant": "per_etf_selected", "volume_ratio": None,
                           **search.portfolio_backtest(frames, selected, CODES, search.START, search.END)})
    portfolios = pd.DataFrame(portfolio_rows).sort_values(["sharpe", "annualized_return"], ascending=False)
    portfolios.to_csv(output / "portfolio_comparison.csv", index=False)

    summary = {
        "coverage": {"selected": len(CODES), "loaded": len(frames), "codes": list(CODES)},
        "period": {"start": str(search.START), "end": str(search.END)},
        "fixed_common_parameters": COMMON,
        "volume_candidates": VOLUME_CANDIDATES,
        "selection": "per-ETF highest full-sample Sharpe; require at least two fills when available",
        "selected_per_etf": selected_table.to_dict(orient="records"),
        "portfolio_comparison": portfolios.to_dict(orient="records"),
    }
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("/tmp/szdt-a-etf-15-per-volume"))
    asyncio.run(main(parser.parse_args().out))

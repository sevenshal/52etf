#!/usr/bin/env python3
"""Read-only, walk-forward hyperparameter search for all SZDT A-share ETFs.

The script deliberately keeps production writes out of the process.  It reads
the active account's configuration, takes SZDT's current type-3 universe as
the source of truth, and fills unconfigured names with 20k buy/sell amounts
and factors of one.  Signals are evaluated at daily close and filled at the
next available daily open; volume ratio is current daily volume divided by the
previous 20 sessions' mean volume.

Run:
  QUANT_DB_READ_ONLY=true HOME=/tmp/szdt-research \
  .venv/bin/python research/szdt_a_etf_hyperparameter_search.py --out /tmp/out
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sqlite3
from dataclasses import dataclass, replace
from datetime import date
from itertools import product
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "backend"))
from src.core.services.szdt import SZDTService  # noqa: E402


ACCOUNT_ID = os.environ.get("SZDT_ACCOUNT_ID", "amNDUzNWU4OTA4NjcyZWYwMGYyZDMyYzQzNDFjYjAwOTUK")
PROD_DB = os.environ.get("QUANT_SQLITE_PATH", "/home/quantd/quant_prod/quant_robot/evc_stocks.db")
KLINES_URL = "https://api.52etf.vip/api/stock/a-stock/klines/{symbol}"
START, TRAIN_END, END = date(2024, 6, 17), date(2025, 12, 31), date(2026, 10, 9)
INITIAL_CAPITAL, FEE_RATE, MIN_FEE, LOT = 1_000_000.0, 0.0003, 5.0, 100
BUY_GRID = (-90, -80, -70, -60, -50)
SELL_GRID = (50, 60, 70, 80, 90)
VOLUME_GRID = (0.0, 1.0, 1.2, 1.3, 1.5)
CAP_GRID = (10, 20, 40, 60, 100)
MARKETS: dict[str, dict[date, tuple[float, float, float, float, date | None]]] = {}
MARKET_DAYS: list[date] = []


@dataclass(frozen=True)
class Config:
    code: str
    name: str
    buy_amount: float = 20_000.0
    sell_amount: float = 20_000.0
    buy_factor: float = 1.0
    sell_factor: float = 1.0
    when_buy: int = -70
    when_sell: int = 60
    volume_ratio: float = 0.0
    max_position: int = 40


def canonical(code: str) -> str:
    exchange, symbol = code.upper().split(".")
    return f"{symbol}.{exchange}"


def prod_configs() -> dict[str, Config]:
    conn = sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            """SELECT code,name,buy_amount,sell_amount,buy_factor,sell_factor,
                      when_buy,when_sell,buy_volume_ratio,max_position
                 FROM szdt_trade_stocks
                WHERE account_id=? AND type=3""", (ACCOUNT_ID,)
        ).fetchall()
    finally:
        conn.close()
    return {
        row[0].upper(): Config(
            code=row[0].upper(), name=row[1], buy_amount=float(row[2]),
            sell_amount=float(row[3]), buy_factor=float(row[4]),
            sell_factor=float(row[5]), when_buy=int(row[6]), when_sell=int(row[7]),
            volume_ratio=float(row[8] or 0), max_position=max(1, int(row[9] or 100)),
        ) for row in rows
    }


async def fetch_universe_and_histories() -> tuple[list[dict[str, Any]], dict[str, pd.DataFrame]]:
    service = SZDTService()
    listing = await service.get_etf_emotion(3)
    items = (listing or {}).get("data") or []
    semaphore = asyncio.Semaphore(5)
    async def one(item: dict[str, Any]):
        async with semaphore:
            response = await service.get_etf_emotion_history(str(item["code"]).upper())
        frame = pd.DataFrame((response or {}).get("data") or [])
        if frame.empty or not {"date", "score"}.issubset(frame.columns):
            return str(item["code"]).upper(), None
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
        frame["score"] = pd.to_numeric(frame["score"], errors="coerce")
        frame = frame.dropna().drop_duplicates("date", keep="last").sort_values("date")
        return str(item["code"]).upper(), frame[["date", "score"]]
    pairs = await asyncio.gather(*(one(item) for item in items))
    return items, {code: frame for code, frame in pairs if frame is not None and not frame.empty}


async def fetch_prices(codes: list[str]) -> dict[str, pd.DataFrame]:
    # The public kline endpoint starts dropping requests when a full SZDT
    # universe is fetched too aggressively.  A small concurrency limit plus
    # bounded retries keeps a grid search from silently using a partial basket.
    semaphore = asyncio.Semaphore(2)
    params = {"start_date": START.isoformat(), "end_date": END.isoformat(), "period": "d"}
    headers = {"X-Account-ID": ACCOUNT_ID, "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=40) as client:
        async def one(code: str):
            async with semaphore:
                rows = None
                for attempt in range(3):
                    try:
                        response = await client.get(KLINES_URL.format(symbol=canonical(code)), params=params, headers=headers)
                        response.raise_for_status()
                        candidate = response.json()
                        if isinstance(candidate, list):
                            rows = candidate
                            break
                    except Exception:
                        pass
                    await asyncio.sleep(0.5 * (attempt + 1))
                if rows is None:
                    return code, None
            frame = pd.DataFrame(rows if isinstance(rows, list) else [])
            required = {"timestamp", "open", "close", "volume"}
            if frame.empty or not required.issubset(frame.columns):
                return code, None
            frame["date"] = pd.to_datetime(frame["timestamp"], errors="coerce").dt.date
            for field in ("open", "close", "volume"):
                frame[field] = pd.to_numeric(frame[field], errors="coerce")
            frame = frame.dropna(subset=["date", "open", "close", "volume"])
            frame = frame[(frame.open > 0) & (frame.close > 0) & (frame.volume >= 0)]
            frame = frame.drop_duplicates("date", keep="last").sort_values("date")
            frame["volume_ratio"] = frame.volume / frame.volume.shift(1).rolling(20, min_periods=20).mean()
            return code, frame[["date", "open", "close", "volume_ratio"]]
        rows = await asyncio.gather(*(one(code) for code in codes))
    return {code: frame for code, frame in rows if frame is not None and len(frame) >= 80}


def fee(value: float) -> float:
    return max(MIN_FEE, value * FEE_RATE) if value > 0 else 0.0


def metric(nav: list[float], initial: float, trades: int) -> dict[str, float]:
    series = pd.Series(nav, dtype=float)
    daily = series.pct_change().dropna()
    total = series.iloc[-1] / initial - 1
    annual = (1 + total) ** (252 / max(1, len(daily))) - 1
    drawdown = float((series / series.cummax() - 1).min())
    std = daily.std()
    sharpe = float(daily.mean() / std * math.sqrt(252)) if len(daily) > 3 and std > 0 else -99.0
    return {"total_return": total, "annualized_return": annual, "max_drawdown": drawdown, "sharpe": sharpe, "trades": trades, "final_nav": float(series.iloc[-1])}


def asset_backtest(frame: pd.DataFrame, cfg: Config, start: date, end: date, initial: float = 100_000) -> dict[str, float]:
    frame = frame[(frame.date >= start) & (frame.date <= end)].reset_index(drop=True)
    cash, shares, nav, trade_count, pending = float(initial), 0, [], 0, None
    for idx, row in frame.iterrows():
        open_price, close = float(row.open), float(row.close)
        if pending == "buy":
            equity = cash + shares * open_price
            position = shares * open_price
            if position / max(equity, 1) * 100 < cfg.max_position:
                factor = min(1.0, max(0.0, (cfg.when_buy - float(frame.score.iloc[idx - 1])) / (cfg.when_buy + 100)))
                requested = cfg.buy_amount * 3 ** (factor ** cfg.buy_factor)
                quantity = int(min(cash, requested) / open_price / LOT) * LOT
                gross = quantity * open_price
                if quantity >= LOT and gross + fee(gross) <= cash:
                    cash -= gross + fee(gross); shares += quantity; trade_count += 1
        elif pending == "sell" and shares >= LOT:
            factor = min(1.0, max(0.0, (float(frame.score.iloc[idx - 1]) - cfg.when_sell) / (100 - cfg.when_sell)))
            requested = cfg.sell_amount * 3 ** (factor ** cfg.sell_factor)
            quantity = min(shares, max(LOT, int(requested / open_price / LOT) * LOT))
            gross = quantity * open_price
            cash += gross - fee(gross); shares -= quantity; trade_count += 1
        pending = None
        if idx < len(frame) - 1:
            score = float(row.score)
            if score <= cfg.when_buy and (cfg.volume_ratio <= 0 or float(row.volume_ratio or 0) >= cfg.volume_ratio): pending = "buy"
            elif score >= cfg.when_sell: pending = "sell"
        nav.append(cash + shares * close)
    return metric(nav, initial, trade_count)


def portfolio_backtest(frames: dict[str, pd.DataFrame], configs: dict[str, Config], codes: tuple[str, ...], start: date, end: date) -> dict[str, float]:
    days = [day for day in MARKET_DAYS if start <= day <= end]
    cash, shares, pending, nav, trades = INITIAL_CAPITAL, {code: 0 for code in codes}, {}, [], 0
    for day in days:
        for code, side, signal_score in list(pending.pop(day, [])):
            row = MARKETS[code].get(day)
            if row is None: continue
            price = row[0]
            equity = cash + sum(shares[k] * MARKETS[k][day][1] for k in codes if day in MARKETS[k])
            cfg = configs[code]
            if side == "buy" and shares[code] * price / max(equity, 1) * 100 < cfg.max_position:
                factor = min(1, max(0, (cfg.when_buy - signal_score) / (cfg.when_buy + 100)))
                wanted = cfg.buy_amount * 3 ** (factor ** cfg.buy_factor)
                qty = int(min(cash, wanted) / price / LOT) * LOT
                gross = qty * price
                if qty >= LOT and gross + fee(gross) <= cash:
                    cash -= gross + fee(gross); shares[code] += qty; trades += 1
            elif side == "sell" and shares[code] >= LOT:
                factor = min(1, max(0, (signal_score - cfg.when_sell) / (100 - cfg.when_sell)))
                wanted = cfg.sell_amount * 3 ** (factor ** cfg.sell_factor)
                qty = min(shares[code], max(LOT, int(wanted / price / LOT) * LOT))
                gross = qty * price
                cash += gross - fee(gross); shares[code] -= qty; trades += 1
        for code in codes:
            row = MARKETS[code].get(day)
            if row is None or row[4] is None: continue
            score = row[3]
            if score <= configs[code].when_buy and (configs[code].volume_ratio <= 0 or row[2] >= configs[code].volume_ratio):
                pending.setdefault(row[4], []).append((code, "buy", score))
            elif score >= configs[code].when_sell:
                pending.setdefault(row[4], []).append((code, "sell", score))
        equity = cash + sum(shares[k] * MARKETS[k][day][1] for k in codes if day in MARKETS[k])
        nav.append(equity)
    return metric(nav, INITIAL_CAPITAL, trades)


def choose_best(frames: dict[str, pd.DataFrame], base: dict[str, Config]) -> tuple[dict[str, Config], pd.DataFrame]:
    selected, report = {}, []
    for code, frame in frames.items():
        candidates = []
        for buy, sell, vol, cap in product(BUY_GRID, SELL_GRID, VOLUME_GRID, CAP_GRID):
            cfg = replace(base[code], when_buy=buy, when_sell=sell, volume_ratio=vol, max_position=cap)
            result = asset_backtest(frame, cfg, START, TRAIN_END)
            if result["trades"] >= 4: candidates.append((result, cfg))
        if not candidates: continue
        result, cfg = max(candidates, key=lambda item: (item[0]["sharpe"], item[0]["annualized_return"], item[0]["max_drawdown"]))
        selected[code] = cfg
        report.append({"code": code, "name": cfg.name, **cfg.__dict__, **{f"train_{k}": v for k, v in result.items()}})
    return selected, pd.DataFrame(report).sort_values(["train_sharpe", "train_annualized_return"], ascending=False)


def beam_select(frames: dict[str, pd.DataFrame], configs: dict[str, Config], ranked: list[str], count: int, width: int = 12) -> tuple[tuple[str, ...], dict[str, float]]:
    beams: list[tuple[tuple[str, ...], dict[str, float]]] = [(tuple(), {"sharpe": -99.0})]
    for _ in range(count):
        expanded = []
        for chosen, _ in beams:
            for code in ranked:
                if code in chosen: continue
                candidate = tuple(sorted((*chosen, code)))
                result = portfolio_backtest(frames, configs, candidate, START, TRAIN_END)
                if result["trades"] >= 4: expanded.append((candidate, result))
        dedup = {codes: result for codes, result in expanded}
        beams = sorted(dedup.items(), key=lambda item: (item[1]["sharpe"], item[1]["annualized_return"], item[1]["max_drawdown"]), reverse=True)[:width]
        if not beams: raise RuntimeError("beam search exhausted")
    best_codes, train = beams[0]
    return best_codes, {"train": train, "test": portfolio_backtest(frames, configs, best_codes, date(2026, 1, 1), END)}


async def async_main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    configured = prod_configs()
    universe, histories = await fetch_universe_and_histories()
    names = {str(item["code"]).upper(): str(item.get("name") or item["code"]) for item in universe}
    prices = await fetch_prices(list(names))
    frames: dict[str, pd.DataFrame] = {}
    for code in sorted(set(names) & set(histories) & set(prices)):
        frame = prices[code].merge(histories[code], on="date", how="inner").dropna().sort_values("date")
        if len(frame[(frame.date >= START) & (frame.date <= END)]) >= 220: frames[code] = frame
    base = {code: configured.get(code, Config(code=code, name=names[code])) for code in frames}
    global MARKETS, MARKET_DAYS
    MARKETS = {}
    for code, frame in frames.items():
        points = list(frame.itertuples(index=False))
        MARKETS[code] = {
            point.date: (float(point.open), float(point.close), float(point.volume_ratio) if pd.notna(point.volume_ratio) else 0.0, float(point.score), points[index + 1].date if index + 1 < len(points) else None)
            for index, point in enumerate(points)
        }
    MARKET_DAYS = sorted({day for market in MARKETS.values() for day in market})
    saved = out / "per_etf_best_train.csv"
    if saved.exists():
        table = pd.read_csv(saved)
        chosen = {
            row.code: Config(
                code=row.code, name=row.name, buy_amount=float(row.buy_amount), sell_amount=float(row.sell_amount),
                buy_factor=float(row.buy_factor), sell_factor=float(row.sell_factor), when_buy=int(row.when_buy),
                when_sell=int(row.when_sell), volume_ratio=float(row.volume_ratio), max_position=int(row.max_position),
            ) for row in table.itertuples(index=False) if row.code in frames
        }
        table = table[table.code.isin(chosen)]
    else:
        chosen, table = choose_best(frames, base)
        table.to_csv(saved, index=False)
    ranked = table.code.head(30).tolist()
    results = {"coverage": {"szdt_universe": len(universe), "histories": len(histories), "price_histories": len(prices), "eligible": len(chosen), "configured": len(configured)}, "period": {"start": str(START), "train_end": str(TRAIN_END), "test_start": "2026-01-01", "end": str(END)}, "grid": {"buy": BUY_GRID, "sell": SELL_GRID, "volume_ratio": VOLUME_GRID, "max_position": CAP_GRID}}
    for size in (10, 20):
        codes, result = beam_select(frames, chosen, ranked, size)
        results[f"top_{size}"] = {"codes": list(codes), **result}
    (out / "summary.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("/tmp/szdt-a-etf-grid"))
    args = parser.parse_args()
    asyncio.run(async_main(args.out))

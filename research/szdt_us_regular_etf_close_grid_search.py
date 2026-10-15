#!/usr/bin/env python3
"""Full-grid SZDT US regular-ETF search with same-close execution.

The universe is SZDT ``etf_type=2``.  Signals are evaluated and filled at the
same daily close as requested; sell orders are applied before buy orders on a
day so released cash is available to concurrent buy signals.  Prices include a
20-session pre-start buffer solely for the daily volume-ratio baseline.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import multiprocessing as mp
from datetime import date
from itertools import product
from pathlib import Path

import httpx
import pandas as pd

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from src.core.services.szdt import SZDTService  # noqa: E402


START, END = date(2024, 6, 17), date(2026, 10, 9)
INITIAL_CAPITAL, FEE_RATE, MIN_FEE = 1_000_000.0, 0.0003, 5.0
ACCOUNT_ID = "amNDUzNWU4OTA4NjcyZWYwMGYyZDMyYzQzNDFjYjAwOTUK"
BUY = (-90, -85, -80, -75, -70)
SELL = (70, 75, 80, 85, 90)
FACTOR = (1.0, 2.0, 3.0, 4.0, 5.0)
AMOUNT = tuple(float(value) for value in range(10_000, 100_001, 10_000))
VOLUME = (1.2, 1.3, 1.4)

MARKETS: dict[str, dict[date, tuple[float, float, float, float]]] = {}
MARKET_DAYS: list[date] = []
CODES: tuple[str, ...] = ()


def fee(value: float) -> float:
    return max(MIN_FEE, value * FEE_RATE) if value > 0 else 0.0


def scaled_trade_amount(amount: float, score: float, threshold: float, exponent: float, *, is_buy: bool) -> float:
    """Match the production SZDT trader/backtest sizing formula exactly."""
    if is_buy:
        normalized = (threshold - score) / (threshold + 100.0)
    else:
        normalized = (score - threshold) / (100.0 - threshold)
    normalized = min(1.0, max(0.0, normalized))
    return amount * (3.0 ** (normalized ** exponent))


def run_one(params: tuple[float, ...]) -> dict[str, float]:
    buy, sell, buy_factor, sell_factor, buy_amount, sell_amount, volume_ratio = params
    cash, shares, nav, trades = INITIAL_CAPITAL, {code: 0 for code in CODES}, [], 0
    for day in MARKET_DAYS:
        rows = {code: MARKETS[code].get(day) for code in CODES}

        # All signals are known at the close.  Free cash from sells first.
        for code in CODES:
            row = rows[code]
            if row is None or row[3] < sell or shares[code] <= 0:
                continue
            close, score = row[1], row[3]
            wanted = scaled_trade_amount(sell_amount, score, sell, sell_factor, is_buy=False)
            quantity = min(shares[code], max(1, int(wanted / close)))
            gross = quantity * close
            cash += gross - fee(gross)
            shares[code] -= quantity
            trades += 1

        for code in CODES:
            row = rows[code]
            if row is None or row[3] > buy or row[2] < volume_ratio:
                continue
            close, score = row[1], row[3]
            wanted = scaled_trade_amount(buy_amount, score, buy, buy_factor, is_buy=True)
            quantity = int(min(cash, wanted) / close)
            gross = quantity * close
            while quantity > 0 and gross + fee(gross) > cash:
                quantity -= 1
                gross = quantity * close
            if quantity:
                cash -= gross + fee(gross)
                shares[code] += quantity
                trades += 1

        equity = cash + sum(shares[code] * rows[code][1] for code in CODES if rows[code] is not None)
        nav.append(equity)

    series = pd.Series(nav, dtype=float)
    daily = series.pct_change().dropna()
    total = float(series.iloc[-1] / INITIAL_CAPITAL - 1)
    annual = float((1 + total) ** (252 / max(1, len(daily))) - 1)
    drawdown = float((series / series.cummax() - 1).min())
    volatility = daily.std()
    sharpe = float(daily.mean() / volatility * math.sqrt(252)) if len(daily) > 3 and volatility > 0 else -99.0
    return {
        "when_buy": buy, "when_sell": sell, "buy_factor": buy_factor, "sell_factor": sell_factor,
        "buy_amount": buy_amount, "sell_amount": sell_amount, "volume_ratio": volume_ratio,
        "total_return": total, "annualized_return": annual, "max_drawdown": drawdown,
        "sharpe": sharpe, "trades": trades, "final_nav": float(series.iloc[-1]),
    }


async def load_frames(etf_type: int, codes: set[str] | None = None) -> tuple[dict[str, pd.DataFrame], dict[str, str], int, int]:
    service = SZDTService()
    listing = await service.get_etf_emotion(etf_type)
    items = (listing or {}).get("data") or []
    names = {str(item["code"]).upper(): str(item.get("name") or item["code"]) for item in items}
    if codes:
        names = {code: name for code, name in names.items() if code in codes}
    semaphore = asyncio.Semaphore(6)

    async def history(code: str):
        async with semaphore:
            response = await service.get_etf_emotion_history(code)
        frame = pd.DataFrame((response or {}).get("data") or [])
        if frame.empty or not {"date", "score"}.issubset(frame.columns):
            return code, None
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
        frame["score"] = pd.to_numeric(frame["score"], errors="coerce")
        return code, frame.dropna().drop_duplicates("date", keep="last")[["date", "score"]].sort_values("date")

    histories = dict((code, frame) for code, frame in await asyncio.gather(*(history(code) for code in names)) if frame is not None)
    params = {"start_date": "2024-05-15", "end_date": END.isoformat(), "period": "d"}
    headers = {"X-Account-ID": ACCOUNT_ID, "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=40) as client:
        async def price(code: str):
            symbol = f"{code[3:]}.US"
            async with semaphore:
                try:
                    response = await client.get(f"https://api.52etf.vip/api/stock/klines/{symbol}", params=params, headers=headers)
                    response.raise_for_status()
                    rows = response.json()
                except Exception:
                    return code, None
            frame = pd.DataFrame(rows if isinstance(rows, list) else [])
            if frame.empty or not {"timestamp", "open", "close", "volume"}.issubset(frame.columns):
                return code, None
            frame["date"] = pd.to_datetime(frame["timestamp"], errors="coerce").dt.date
            for field in ("open", "close", "volume"):
                frame[field] = pd.to_numeric(frame[field], errors="coerce")
            frame = frame.dropna(subset=["date", "open", "close", "volume"]).sort_values("date").drop_duplicates("date", keep="last")
            frame["volume_ratio"] = frame.volume / frame.volume.shift(1).rolling(20, min_periods=20).mean()
            return code, frame[["date", "open", "close", "volume_ratio"]]
        prices = dict((code, frame) for code, frame in await asyncio.gather(*(price(code) for code in names)) if frame is not None)

    frames = {}
    for code in names:
        if code not in histories or code not in prices:
            continue
        frame = prices[code].merge(histories[code], on="date", how="inner").dropna(subset=["open", "close", "score"]).sort_values("date")
        frame = frame[(frame.date >= START) & (frame.date <= END)]
        if len(frame) >= 80:
            frames[code] = frame
    return frames, names, len(items), len(prices)


async def main(output: Path, etf_type: int, codes: set[str] | None = None) -> None:
    output.mkdir(parents=True, exist_ok=True)
    frames, names, listing_count, price_count = await load_frames(etf_type, codes)
    global MARKETS, MARKET_DAYS, CODES
    CODES = tuple(sorted(frames))
    MARKETS = {}
    for code, frame in frames.items():
        MARKETS[code] = {
            point.date: (float(point.open), float(point.close), float(point.volume_ratio) if pd.notna(point.volume_ratio) else 0.0, float(point.score))
            for point in frame.itertuples(index=False)
        }
    MARKET_DAYS = sorted({day for market in MARKETS.values() for day in market})
    params = list(product(BUY, SELL, FACTOR, FACTOR, AMOUNT, AMOUNT, VOLUME))
    with mp.Pool(processes=min(4, mp.cpu_count())) as pool:
        rows = list(pool.imap_unordered(run_one, params, chunksize=64))
    table = pd.DataFrame(rows).sort_values(["sharpe", "annualized_return", "max_drawdown"], ascending=[False, False, False])
    table.to_csv(output / "us_regular_close_grid.csv", index=False)
    summary = {
        "coverage": {"etf_type": etf_type, "listing": listing_count, "price_histories": price_count, "eligible": len(frames), "codes": list(CODES), "names": {code: names[code] for code in CODES}},
        "period": {"start": str(START), "end": str(END)},
        "rules": {"execution": "same_close; sell_before_buy", "ema5": False, "leverage": False, "single_symbol_cap": "none", "initial_capital_usd": INITIAL_CAPITAL, "fee_rate": FEE_RATE, "min_fee_usd": MIN_FEE},
        "grid": {"buy": BUY, "sell": SELL, "factor": FACTOR, "amount": AMOUNT, "volume_ratio": VOLUME, "evaluations": len(table)},
        "best": table.iloc[0].to_dict(), "top_30": table.head(30).to_dict(orient="records"),
    }
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("/tmp/szdt-us-regular-close-grid"))
    parser.add_argument("--etf-type", type=int, default=2, choices=(1, 2, 7))
    parser.add_argument("--codes", default="", help="可选，逗号分隔的 US.TQQQ 代码列表")
    arguments = parser.parse_args()
    selected_codes = {item.strip().upper() for item in arguments.codes.split(",") if item.strip()} or None
    asyncio.run(main(arguments.out, arguments.etf_type, selected_codes))

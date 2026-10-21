"""Search self-calculated Fear & Greed thresholds for the SZDT A-share ETF basket.

Run against an explicit read-only production copy, for example::

  sg quantd -c 'QUANT_DB_READ_ONLY=true ANALYTICS_DB_PATH=/home/quantd/quant_prod/quant_robot/analytics.duckdb \
    /home/sevenshal/Dev/github/quant/52etf/.venv/bin/python research/szdt_a_stock_self_fear_search.py \
    --sqlite /home/quantd/quant_prod/quant_robot/evc_stocks.db'

The search preserves the live strategy's cash-only, same-close, sell-before-buy,
lot, fee, slippage and rotation behaviour.  It searches only trigger thresholds.
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from src.robot.a_stock_base_data_config import A_STOCK_INDEX_FEAR_GREED_TARGETS  # noqa: E402


BUY_THRESHOLDS = (10, 15, 20, 25, 30)
SELL_THRESHOLDS = (60, 65, 70, 75, 80)
ROTATION_THRESHOLDS = (50, 60, 70, 80)
VOLUME_THRESHOLDS = (0.0, 0.8, 1.2, 1.4)
INITIAL_CAPITAL = 1_000_000.0
COMMISSION_RATE = 0.0003
MIN_COMMISSION = 5.0
SLIPPAGE_BPS = 10.0
LOT = 100

OVERSEAS_PROXY_ETFS = (
    {"code": "SZ.159920", "name": "恒生ETF", "fear_symbol": "HSI.HK"},
    {"code": "SH.513130", "name": "恒生科技ETF", "fear_symbol": "HSTECH.HK"},
    {"code": "SH.513120", "name": "香港创新药ETF", "fear_symbol": "931250.CSI"},
    {"code": "SZ.159941", "name": "纳指ETF", "fear_symbol": "QQQ.US"},
    {"code": "SH.513500", "name": "标普500ETF", "fear_symbol": "SPY.US"},
    {"code": "SH.513400", "name": "道琼斯ETF", "fear_symbol": "DIA.US"},
    {"code": "SH.501225", "name": "全球芯片LOF", "fear_symbol": "SOXX.US"},
    {"code": "SH.518880", "name": "黄金ETF", "fear_symbol": "GLD.US"},
)


def self_fear_candidate_universe() -> list[dict]:
    candidates = []
    for target in A_STOCK_INDEX_FEAR_GREED_TARGETS:
        if proxy := target.get("proxy_etf"):
            ticker, market = str(proxy).upper().split(".", 1)
            candidates.append({"code": f"{market}.{ticker}", "name": str(target.get("label") or target.get("ticker") or proxy), "fear_symbol": str(target["symbol"]).upper()})
    candidates.extend(OVERSEAS_PROXY_ETFS)
    return list({item["code"]: item for item in candidates}.values())


def fee(amount: float) -> float:
    return max(MIN_COMMISSION, amount * COMMISSION_RATE) if amount else 0.0


def load_template(sqlite_path: str) -> dict:
    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            """SELECT max_position,buy_amount,sell_amount,buy_factor,sell_factor
               FROM szdt_trade_stocks WHERE type=3 ORDER BY enabled DESC,id LIMIT 1"""
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise RuntimeError("No A-share ETF strategy template found")
    return dict(max_position=float(row[0]), buy_amount=float(row[1]), sell_amount=float(row[2]),
                buy_factor=float(row[3]), sell_factor=float(row[4]))


def load_stocks(sqlite_path: str) -> list[dict]:
    template = load_template(sqlite_path)
    return [{**candidate, **template} for candidate in self_fear_candidate_universe()]


def load_data(sqlite_path: str, analytics_path: str, stocks: list[dict]) -> tuple[list[date], dict, dict, dict]:
    sources = sorted({item["fear_symbol"] for item in stocks})
    source_placeholders = ",".join("?" for _ in sources)
    sqlite_conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        score_rows = sqlite_conn.execute(
            f"SELECT symbol,date,score FROM etf_fear_greed_clone_history WHERE symbol IN ({source_placeholders})",
            sources,
        ).fetchall()
    finally:
        sqlite_conn.close()
    scores_by_source = {(str(symbol).upper(), pd.Timestamp(day).date()): float(score) for symbol, day, score in score_rows if score is not None}

    ts_codes = [resolve_code(item["code"]) for item in stocks]
    db = duckdb.connect(analytics_path, read_only=True)
    try:
        bars = db.execute(
            f"""SELECT ts_code,trade_date,close,vol FROM a_stock_fund_daily_qfq
                WHERE ts_code IN ({','.join('?' for _ in ts_codes)}) ORDER BY ts_code,trade_date""",
            ts_codes,
        ).fetchdf()
    finally:
        db.close()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"]).dt.date
    bars["close"] = pd.to_numeric(bars["close"], errors="coerce")
    bars["vol"] = pd.to_numeric(bars["vol"], errors="coerce")
    bars["code"] = bars["ts_code"].map(lambda value: f"{str(value).split('.')[1]}.{str(value).split('.')[0]}")
    bars["volume_ratio"] = bars.groupby("code")["vol"].transform(lambda values: values / values.shift(1).rolling(20, min_periods=20).mean())
    prices = {(row.code, row.trade_date): float(row.close) for row in bars.itertuples() if pd.notna(row.close) and row.close > 0}
    volumes = {(row.code, row.trade_date): float(row.volume_ratio) for row in bars.itertuples() if pd.notna(row.volume_ratio) and row.volume_ratio > 0}
    scores = {(item["code"], day): score for item in stocks for (source, day), score in scores_by_source.items() if source == item["fear_symbol"]}
    sessions = sorted({day for code, day in prices if (code, day) in scores})
    return sessions, prices, volumes, scores


def resolve_code(code: str) -> str:
    market, _, ticker = code.upper().partition(".")
    return f"{ticker}.{market}"


def backtest(days, prices, volumes, scores, stocks, buy, sell, rotation, volume_threshold, *, half_cash_top1=False):
    cash = INITIAL_CAPITAL
    shares = defaultdict(int)
    navs = []
    trades = rotation_trades = 0
    for day in days:
        # Sell first, exactly as the live/backtest engine does.
        for stock in stocks:
            code = stock["code"]
            score, close = scores.get((code, day)), prices.get((code, day))
            if score is None or not close or shares[code] < LOT or score < sell:
                continue
            factor = min(1.0, max(0.0, (score - sell) / (100 - sell)))
            amount = stock["sell_amount"] * (3 ** (factor ** stock["sell_factor"]))
            quantity = min(shares[code], max(LOT, int(amount / close / LOT) * LOT))
            gross = quantity * close * (1 - SLIPPAGE_BPS / 10_000)
            cash += gross - fee(gross)
            shares[code] -= quantity
            trades += 1
        # Fear first, then alphabetically to make every candidate deterministic.
        candidates = sorted(stocks, key=lambda item: (scores.get((item["code"], day), 101), item["code"]))
        if half_cash_top1:
            candidates = [
                stock for stock in candidates
                if scores.get((stock["code"], day)) is not None
                and prices.get((stock["code"], day))
                and scores[(stock["code"], day)] <= buy
                and (volume_threshold <= 0 or volumes.get((stock["code"], day), 0) >= volume_threshold)
            ][:1]
        for stock in candidates:
            code = stock["code"]
            score, close = scores.get((code, day)), prices.get((code, day))
            if score is None or not close or score > buy or (volume_threshold > 0 and volumes.get((code, day), 0) < volume_threshold):
                continue
            equity = cash + sum(quantity * prices.get((symbol, day), 0) for symbol, quantity in shares.items())
            if equity <= 0 or shares[code] * close / equity * 100 >= stock["max_position"]:
                continue
            factor = min(1.0, max(0.0, (buy - score) / buy))
            target = cash * 0.5 if half_cash_top1 else stock["buy_amount"] * (3 ** (factor ** stock["buy_factor"]))
            if cash < target:
                for source_score, source_code, source_close in sorted(
                    ((scores.get((source["code"], day)), source["code"], prices.get((source["code"], day))) for source in stocks
                     if source["code"] != code and shares[source["code"]] >= LOT and scores.get((source["code"], day), -1) >= rotation and prices.get((source["code"], day))),
                    reverse=True,
                ):
                    if cash >= target:
                        break
                    quantity = min(shares[source_code], max(LOT, math.ceil((target - cash) / source_close / LOT) * LOT))
                    gross = quantity * source_close * (1 - SLIPPAGE_BPS / 10_000)
                    cash += gross - fee(gross)
                    shares[source_code] -= quantity
                    trades += 1
                    rotation_trades += 1
            available = min(cash, target)
            quantity = int(available / (close * (1 + SLIPPAGE_BPS / 10_000)) / LOT) * LOT
            gross = quantity * close * (1 + SLIPPAGE_BPS / 10_000)
            if quantity < LOT or gross + fee(gross) > cash:
                continue
            cash -= gross + fee(gross)
            shares[code] += quantity
            trades += 1
        navs.append(cash + sum(quantity * prices.get((symbol, day), 0) for symbol, quantity in shares.items()))
    series = pd.Series(navs)
    returns = series.pct_change().dropna()
    annualized = (series.iloc[-1] / INITIAL_CAPITAL) ** (252 / max(len(returns), 1)) - 1
    drawdown = (series / series.cummax() - 1).min()
    sharpe = returns.mean() / returns.std() * math.sqrt(252) if len(returns) > 1 and returns.std() > 0 else float("nan")
    return dict(buy_threshold=buy, sell_threshold=sell, rotation_threshold=rotation, volume_ratio=volume_threshold,
                total_return_pct=round((series.iloc[-1] / INITIAL_CAPITAL - 1) * 100, 2), annualized_return_pct=round(annualized * 100, 2),
                max_drawdown_pct=round(drawdown * 100, 2), sharpe=round(float(sharpe), 3), trade_count=trades, rotation_sell_count=rotation_trades,
                final_nav=round(float(series.iloc[-1]), 2), half_cash_top1=half_cash_top1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite", required=True)
    parser.add_argument("--analytics", default="/home/quantd/quant_prod/quant_robot/analytics.duckdb")
    parser.add_argument("--output", type=Path, default=ROOT / "research/output/szdt_a_stock_self_fear_search.json")
    args = parser.parse_args()
    stocks = load_stocks(args.sqlite)
    days, prices, volumes, scores = load_data(args.sqlite, args.analytics, stocks)
    if not days:
        raise SystemExit("No score/price history")
    results = [backtest(days, prices, volumes, scores, stocks, *candidate) for candidate in itertools.product(BUY_THRESHOLDS, SELL_THRESHOLDS, ROTATION_THRESHOLDS, VOLUME_THRESHOLDS)]
    ranked = sorted(results, key=lambda item: (item["annualized_return_pct"], item["sharpe"], item["max_drawdown_pct"]), reverse=True)
    active_codes = {code for code, _ in scores}
    half_cash_results = [
        backtest(days, prices, volumes, scores, stocks, 15, sell, rotation, 1.4, half_cash_top1=True)
        for sell, rotation in itertools.product((60, 65, 70, 75, 80, 85, 90), (50, 60, 70, 80, 90))
    ]
    half_cash_ranked = sorted(half_cash_results, key=lambda item: (item["annualized_return_pct"], item["sharpe"], item["max_drawdown_pct"]), reverse=True)
    payload = {"period": {"start": str(days[0]), "end": str(days[-1]), "sessions": len(days)}, "candidate_count": len(stocks), "candidates_with_history": len({item["code"] for item in stocks} & active_codes), "grid": {"buy": BUY_THRESHOLDS, "sell": SELL_THRESHOLDS, "rotation": ROTATION_THRESHOLDS, "volume": VOLUME_THRESHOLDS}, "top_20": ranked[:20], "half_cash_top1_buy_15_volume_1_4": half_cash_ranked[:20], "mappings": {item["code"]: item["fear_symbol"] for item in stocks}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

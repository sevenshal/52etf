"""守猪逮兔 A 股 ETF 配置回测（与自动交易的阈值/金额/EMA5 规则对齐）。"""
from __future__ import annotations

import asyncio
import math
from collections import defaultdict
from datetime import date
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

from .duckdb_analytics import connect_analytics_db
from .szdt import SZDTService
from .tushare import TushareService


COMMISSION_RATE = 0.0003
MIN_COMMISSION = 5.0
LOT_SIZE = 100


def normalize_szdt_etf_code(code: Any) -> str:
    return TushareService.normalize_symbol(str(code or ""))


async def _load_emotion_histories(stocks: List[Dict[str, Any]]) -> Dict[str, pd.DataFrame]:
    service = SZDTService()
    histories: Dict[str, pd.DataFrame] = {}
    for offset in range(0, len(stocks), 5):
        batch = stocks[offset:offset + 5]
        responses = await asyncio.gather(*[service.get_etf_emotion_history(item["code"]) for item in batch])
        for stock, response in zip(batch, responses):
            frame = pd.DataFrame((response or {}).get("data") or [])
            if frame.empty or not {"date", "score"}.issubset(frame.columns):
                continue
            frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
            frame["score"] = pd.to_numeric(frame["score"], errors="coerce")
            frame = frame.dropna(subset=["date", "score"]).drop_duplicates("date", keep="last")
            if not frame.empty:
                histories[stock["code"]] = frame[["date", "score"]].sort_values("date")
    return histories


def _load_daily_frame(symbols: Iterable[str]) -> pd.DataFrame:
    symbols = list(dict.fromkeys(symbols))
    if not symbols:
        return pd.DataFrame()
    placeholders = ",".join("?" for _ in symbols)
    connection = connect_analytics_db()
    try:
        frame = connection.execute(
            f"""
            SELECT ts_code, trade_date, open, close, vol
            FROM a_stock_fund_daily_qfq
            WHERE ts_code IN ({placeholders})
            ORDER BY ts_code, trade_date
            """,
            symbols,
        ).fetchdf()
    finally:
        connection.close()
    if frame.empty:
        return frame
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce").dt.date
    frame["code"] = frame["ts_code"].map(lambda value: f"{str(value).split('.')[1]}.{str(value).split('.')[0]}")
    frame["vol"] = pd.to_numeric(frame["vol"], errors="coerce")
    frame["volume_ratio"] = frame.groupby("code")["vol"].transform(
        lambda values: values / values.shift(1).rolling(20, min_periods=20).mean()
    )
    frame["ema5"] = frame.groupby("code")["close"].transform(
        lambda values: values.ewm(span=5, adjust=False).mean()
    )
    return frame.dropna(subset=["trade_date", "open", "close"])


def _fee(amount: float) -> float:
    return max(MIN_COMMISSION, amount * COMMISSION_RATE) if amount > 0 else 0.0


async def run_szdt_a_stock_backtest(
    stocks: List[Dict[str, Any]],
    *,
    sell_on_ema5_breakdown: bool,
    rotation_enabled: bool = False,
    rotation_threshold: int = -30,
    initial_capital: float = 1_000_000.0,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    allow_leverage: bool = False,
    slippage_bps: float = 10,
) -> Dict[str, Any]:
    """用当前配置回放：T 日信号，下一可交易日开盘成交。"""
    stocks = [dict(item, code=str(item["code"]).upper()) for item in stocks if item.get("enabled")]
    if not stocks:
        raise ValueError("没有启用的 A股ETF 配置")
    histories = await _load_emotion_histories(stocks)
    stocks = [item for item in stocks if item["code"] in histories]
    if not stocks:
        raise ValueError("未能获取启用标的的守猪逮兔历史贪恐数据")
    symbols = [normalize_szdt_etf_code(item["code"]) for item in stocks]
    frame = _load_daily_frame(symbols)
    if frame.empty:
        raise ValueError("分析库缺少启用 ETF 的日线数据")

    all_days = sorted(frame["trade_date"].unique())
    first_history = min(history["date"].min() for history in histories.values())
    last_history = max(history["date"].max() for history in histories.values())
    actual_start = max(start_date or first_history, first_history)
    actual_end = min(end_date or last_history, last_history)
    if actual_start >= actual_end:
        raise ValueError("回测日期范围没有可用数据")

    prices = {
        (row.code, row.trade_date): (float(row.open), float(row.close))
        for row in frame.itertuples()
        if pd.notna(row.open) and pd.notna(row.close) and float(row.open) > 0 and float(row.close) > 0
    }
    volume_ratios = {
        (row.code, row.trade_date): float(row.volume_ratio)
        for row in frame.itertuples()
        if pd.notna(row.volume_ratio) and float(row.volume_ratio) > 0
    }
    below_ema5 = {
        (row.code, row.trade_date): bool(float(row.close) < float(row.ema5))
        for row in frame.itertuples() if pd.notna(row.ema5)
    }
    scores = {
        (code, row.date): float(row.score)
        for code, history in histories.items() for row in history.itertuples()
    }

    cash = float(initial_capital)
    shares: Dict[str, int] = defaultdict(int)
    trades: List[Dict[str, Any]] = []
    rotation_trade_count = 0
    curve: List[Dict[str, Any]] = []
    financing_cost = 0.0
    daily_days = [item for item in all_days if actual_start <= item <= actual_end]

    for day in daily_days:
        if allow_leverage and cash < 0:
            interest = -cash * 0.05 / 252
            cash -= interest
            financing_cost += interest
        # T 日日线贪恐和量比，以 T 日收盘价成交；同日先卖后买，使卖出资金可复用。
        for stock in stocks:
            code = stock["code"]
            score = scores.get((code, day))
            quote = prices.get((code, day))
            if score is None or not quote or shares[code] < LOT_SIZE:
                continue
            close_price = quote[1]
            if score < float(stock["when_sell"]) or (sell_on_ema5_breakdown and not below_ema5.get((code, day), False)):
                continue
            factor = min(1.0, max(0.0, (score - float(stock["when_sell"])) / (100 - float(stock["when_sell"]))))
            amount = float(stock["sell_amount"]) * (3 ** (factor ** float(stock["sell_factor"])))
            quantity = min(shares[code], max(LOT_SIZE, int(amount / close_price / LOT_SIZE) * LOT_SIZE))
            execution_price = close_price * (1 - slippage_bps / 10_000)
            gross = quantity * execution_price
            cash += gross - _fee(gross)
            shares[code] -= quantity
            trades.append({"date": day.isoformat(), "code": code, "name": stock["name"], "side": "SELL", "quantity": quantity, "price": round(execution_price, 4), "score": round(score, 2), "amount": round(gross, 2)})

        for stock in stocks:
            code = stock["code"]
            score = scores.get((code, day))
            quote = prices.get((code, day))
            threshold = float(stock.get("buy_volume_ratio") or 0)
            if score is None or not quote or score > float(stock["when_buy"]) or (threshold > 0 and volume_ratios.get((code, day), 0) < threshold):
                continue
            close_price = quote[1]
            execution_price = close_price * (1 + slippage_bps / 10_000)
            equity = cash + sum(quantity * prices[(symbol, day)][1] for symbol, quantity in shares.items() if (symbol, day) in prices)
            if equity <= 0 or shares[code] * close_price / equity * 100 >= float(stock["max_position"]):
                continue
            factor = min(1.0, max(0.0, (float(stock["when_buy"]) - score) / (float(stock["when_buy"]) + 100)))
            target_amount = float(stock["buy_amount"]) * (3 ** (factor ** float(stock["buy_factor"])))
            # 现金不足时，只在显式开启轮动后，从贪恐值更高的其他持仓中腾挪。
            # 先卖最贪婪的，且只卖到足以覆盖当前目标买单的整手数。
            if rotation_enabled and not allow_leverage and cash < target_amount:
                for source in sorted(
                    (
                        (float(source_score), source_code, source_quote[1])
                        for source_code, source_score in ((item["code"], scores.get((item["code"], day))) for item in stocks)
                        if source_code != code
                        and source_score is not None
                        and float(source_score) >= float(rotation_threshold)
                        and shares[source_code] >= LOT_SIZE
                        and (source_quote := prices.get((source_code, day)))
                    ),
                    reverse=True,
                ):
                    if cash >= target_amount:
                        break
                    source_score, source_code, source_price = source
                    needed = target_amount - cash
                    source_quantity = min(
                        shares[source_code],
                        max(LOT_SIZE, math.ceil(needed / source_price / LOT_SIZE) * LOT_SIZE),
                    )
                    source_gross = source_quantity * source_price * (1 - slippage_bps / 10_000)
                    cash += source_gross - _fee(source_gross)
                    shares[source_code] -= source_quantity
                    rotation_trade_count += 1
                    trades.append({
                        "date": day.isoformat(), "code": source_code,
                        "name": next(item["name"] for item in stocks if item["code"] == source_code),
                        "side": "ROTATE_SELL", "quantity": source_quantity,
                        "price": round(source_price, 4), "score": round(source_score, 2),
                        "amount": round(source_gross, 2),
                    })
            available = target_amount if allow_leverage else min(cash, target_amount)
            quantity = int(available / execution_price / LOT_SIZE) * LOT_SIZE
            gross = quantity * execution_price
            fee = _fee(gross)
            while quantity >= LOT_SIZE and not allow_leverage and gross + fee > cash:
                quantity -= LOT_SIZE
                gross = quantity * execution_price
                fee = _fee(gross)
            if quantity < LOT_SIZE:
                continue
            cash -= gross + fee
            shares[code] += quantity
            trades.append({"date": day.isoformat(), "code": code, "name": stock["name"], "side": "BUY", "quantity": quantity, "price": round(execution_price, 4), "score": round(score, 2), "amount": round(gross, 2)})

        long_value = sum(quantity * prices[(symbol, day)][1] for symbol, quantity in shares.items() if (symbol, day) in prices)
        equity = cash + long_value
        curve.append({"date": day.isoformat(), "nav": round(equity, 2), "gross_exposure": round(long_value / equity * 100, 2) if equity > 0 else 0})

    nav = pd.Series([point["nav"] for point in curve])
    peak_nav = 0.0
    for point in curve:
        peak_nav = max(peak_nav, float(point["nav"]))
        point["drawdown_pct"] = round((float(point["nav"]) / peak_nav - 1) * 100, 2) if peak_nav else 0.0
    returns = nav.pct_change().dropna()
    total_return = nav.iloc[-1] / initial_capital - 1
    annualized = (nav.iloc[-1] / initial_capital) ** (252 / max(len(returns), 1)) - 1
    drawdown = (nav / nav.cummax() - 1).min()
    sharpe = returns.mean() / returns.std() * math.sqrt(252) if len(returns) > 1 and returns.std() > 0 else None
    gross = pd.Series([point["gross_exposure"] for point in curve]) / 100
    final_day = daily_days[-1]
    final_nav = float(nav.iloc[-1])
    latest_positions = []
    for stock in stocks:
        code = stock["code"]
        quantity = shares[code]
        quote = prices.get((code, final_day))
        if quantity <= 0 or not quote:
            continue
        value = quantity * quote[1]
        latest_positions.append({
            "code": code, "name": stock["name"], "quantity": quantity,
            "price": round(quote[1], 4), "score": scores.get((code, final_day)),
            "market_value": round(value, 2), "position_pct": round(value / final_nav * 100, 2) if final_nav > 0 else 0,
        })
    return {
        "period": {"start_date": curve[0]["date"], "end_date": curve[-1]["date"], "sessions": len(returns)},
        "rules": {"sell_on_ema5_breakdown": sell_on_ema5_breakdown, "rotation_enabled": rotation_enabled, "rotation_threshold": rotation_threshold if rotation_enabled else None, "allow_leverage": allow_leverage, "slippage_bps": slippage_bps, "execution": "same_close; sell_before_buy"},
        "metrics": {
            "initial_capital": round(initial_capital, 2), "final_nav": round(float(nav.iloc[-1]), 2),
            "total_return_pct": round(total_return * 100, 2), "annualized_return_pct": round(annualized * 100, 2),
            "max_drawdown_pct": round(drawdown * 100, 2), "sharpe": round(float(sharpe), 2) if sharpe is not None else None,
            "trade_count": len(trades), "buy_count": sum(item["side"] == "BUY" for item in trades), "sell_count": sum(item["side"] in {"SELL", "ROTATE_SELL"} for item in trades), "rotation_sell_count": rotation_trade_count,
            "avg_gross_exposure_pct": round(float(gross.mean() * 100), 2), "peak_gross_exposure_pct": round(float(gross.max() * 100), 2),
            # 总多头市值 / 当日权益。不开杠杆时最高为 1.00 倍；允许杠杆后可超过 1。
            "max_leverage_ratio": round(float(gross.max()), 4),
            "capital_efficiency_pct": round(float(annualized / gross.mean() * 100), 2) if gross.mean() > 0 else None,
            "financing_cost": round(financing_cost, 2),
        },
        "curve": curve, "trades": trades, "latest_positions": latest_positions,
        "coverage": {"configured_stocks": len(stocks), "history_loaded": len(histories)},
    }

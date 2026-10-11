"""守猪逮兔美股配置回测，口径与 SZDTUSTrader 的金额系数计算保持一致。"""
from __future__ import annotations

import asyncio
import math
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional

import httpx
import pandas as pd

from .szdt import SZDTService


COMMISSION_RATE = 0.0003
MIN_COMMISSION = 5.0
US_STOCK_TYPES = (1, 2, 7)


def _fee(amount: float) -> float:
    return max(MIN_COMMISSION, amount * COMMISSION_RATE) if amount > 0 else 0.0


def _scaled_amount(amount: float, score: float, threshold: float, exponent: float, *, is_buy: bool) -> float:
    """与 ``SZDTUSTrader`` 完全相同：金额 * 3 ** (标准化分数 ** 系数)。"""
    if is_buy:
        strength = (threshold - score) / (threshold + 100.0)
    else:
        strength = (score - threshold) / (100.0 - threshold)
    strength = min(1.0, max(0.0, strength))
    return amount * (3.0 ** (strength ** exponent))


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
                histories[str(stock["code"]).upper()] = frame[["date", "score"]].sort_values("date")
    return histories


async def _load_us_daily_frame(
    codes: Iterable[str], *, start_date: date, end_date: date, account_id: str,
) -> pd.DataFrame:
    """走系统已有的美股 K 线接口，并保留 20 日量比所需的前置区间。"""
    semaphore = asyncio.Semaphore(6)
    headers = {"Accept": "application/json", "X-Account-ID": account_id}
    params = {"start_date": start_date.isoformat(), "end_date": end_date.isoformat(), "period": "d"}

    async with httpx.AsyncClient(timeout=40) as client:
        async def fetch(code: str) -> pd.DataFrame:
            parts = str(code).upper().split(".", 1)
            if len(parts) != 2 or parts[0] != "US":
                return pd.DataFrame()
            async with semaphore:
                response = await client.get(
                    f"https://api.52etf.vip/api/stock/klines/{parts[1]}.US", params=params, headers=headers,
                )
            response.raise_for_status()
            frame = pd.DataFrame(response.json() if isinstance(response.json(), list) else [])
            if frame.empty or not {"timestamp", "close", "volume"}.issubset(frame.columns):
                return pd.DataFrame()
            frame["date"] = pd.to_datetime(frame["timestamp"], errors="coerce").dt.date
            frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
            frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce")
            frame["code"] = code
            return frame[["code", "date", "close", "volume"]]

        frames = await asyncio.gather(*(fetch(code) for code in dict.fromkeys(codes)), return_exceptions=True)
    usable = [frame for frame in frames if isinstance(frame, pd.DataFrame) and not frame.empty]
    if not usable:
        return pd.DataFrame()
    frame = pd.concat(usable, ignore_index=True).dropna(subset=["date", "close", "volume"])
    frame = frame[(frame["close"] > 0) & (frame["volume"] >= 0)].sort_values(["code", "date"])
    frame["volume_ratio"] = frame.groupby("code")["volume"].transform(
        lambda values: values / values.shift(1).rolling(20, min_periods=20).mean()
    )
    return frame


async def run_szdt_us_backtest(
    stocks: List[Dict[str, Any]], *, account_id: str, initial_capital: float = 1_000_000.0,
    start_date: Optional[date] = None, end_date: Optional[date] = None,
    rotation_enabled: bool = False, rotation_threshold: int = -30,
) -> Dict[str, Any]:
    """日线贪恐及量比在 T 日收盘，用 T 日收盘价成交；同日先卖后买。"""
    stocks = [dict(item, code=str(item["code"]).upper()) for item in stocks if item.get("enabled")]
    if not stocks:
        raise ValueError("没有启用的美股配置")
    histories = await _load_emotion_histories(stocks)
    stocks = [stock for stock in stocks if stock["code"] in histories]
    if not stocks:
        raise ValueError("未能获取启用标的的守猪逮兔历史贪恐数据")

    first_history = min(frame["date"].min() for frame in histories.values())
    last_history = max(frame["date"].max() for frame in histories.values())
    actual_start = max(start_date or first_history, first_history)
    actual_end = min(end_date or last_history, last_history)
    if actual_start >= actual_end:
        raise ValueError("回测日期范围没有可用数据")
    frame = await _load_us_daily_frame(
        [stock["code"] for stock in stocks], start_date=actual_start - timedelta(days=45),
        end_date=actual_end, account_id=account_id,
    )
    if frame.empty:
        raise ValueError("未能获取启用美股标的的日线数据")

    prices = {
        (row.code, row.date): float(row.close) for row in frame.itertuples()
        if pd.notna(row.close) and float(row.close) > 0
    }
    ratios = {
        (row.code, row.date): float(row.volume_ratio) for row in frame.itertuples()
        if pd.notna(row.volume_ratio) and float(row.volume_ratio) > 0
    }
    scores = {
        (code, row.date): float(row.score) for code, history in histories.items() for row in history.itertuples()
    }
    days = sorted({day for code, day in prices if actual_start <= day <= actual_end})
    if not days:
        raise ValueError("回测日期范围没有可用的价格数据")

    cash = float(initial_capital)
    shares: Dict[str, int] = defaultdict(int)
    trades: List[Dict[str, Any]] = []
    rotation_sell_count = 0
    curve: List[Dict[str, Any]] = []
    for day in days:
        # 收盘信号先减仓，释放的现金可用于同日买入。
        for stock in stocks:
            code = stock["code"]
            score, price = scores.get((code, day)), prices.get((code, day))
            if score is None or price is None or shares[code] <= 0 or score < float(stock["when_sell"]):
                continue
            wanted = _scaled_amount(float(stock["sell_amount"]), score, float(stock["when_sell"]), float(stock["sell_factor"]), is_buy=False)
            quantity = min(shares[code], max(1, int(wanted / price)))
            gross = quantity * price
            cash += gross - _fee(gross)
            shares[code] -= quantity
            trades.append({"date": day.isoformat(), "code": code, "name": stock["name"], "side": "SELL", "quantity": quantity, "price": round(price, 4), "score": round(score, 2), "amount": round(gross, 2)})

        for stock in stocks:
            code = stock["code"]
            score, price = scores.get((code, day)), prices.get((code, day))
            threshold = float(stock.get("buy_volume_ratio") or 0)
            if score is None or price is None or score > float(stock["when_buy"]) or (threshold > 0 and ratios.get((code, day), 0) < threshold):
                continue
            equity = cash + sum(quantity * prices.get((symbol, day), 0) for symbol, quantity in shares.items())
            if equity <= 0 or shares[code] * price / equity * 100 >= float(stock["max_position"]):
                continue
            wanted = _scaled_amount(float(stock["buy_amount"]), score, float(stock["when_buy"]), float(stock["buy_factor"]), is_buy=True)
            if rotation_enabled and cash < wanted:
                sources = sorted(
                    ((float(source_score), source_code, source_price) for source_code, source_score in ((item["code"], scores.get((item["code"], day))) for item in stocks)
                     if source_code != code and source_score is not None and float(source_score) >= rotation_threshold
                     and shares[source_code] > 0 and (source_price := prices.get((source_code, day)))),
                    reverse=True,
                )
                for source_score, source_code, source_price in sources:
                    if cash >= wanted:
                        break
                    quantity_to_sell = min(shares[source_code], max(1, math.ceil((wanted - cash) / source_price)))
                    source_gross = quantity_to_sell * source_price
                    cash += source_gross - _fee(source_gross)
                    shares[source_code] -= quantity_to_sell
                    rotation_sell_count += 1
                    trades.append({"date": day.isoformat(), "code": source_code, "name": next(item["name"] for item in stocks if item["code"] == source_code), "side": "ROTATE_SELL", "quantity": quantity_to_sell, "price": round(source_price, 4), "score": round(source_score, 2), "amount": round(source_gross, 2)})
            quantity = int(min(cash, wanted) / price)
            gross = quantity * price
            while quantity > 0 and gross + _fee(gross) > cash:
                quantity -= 1
                gross = quantity * price
            if quantity <= 0:
                continue
            cash -= gross + _fee(gross)
            shares[code] += quantity
            trades.append({"date": day.isoformat(), "code": code, "name": stock["name"], "side": "BUY", "quantity": quantity, "price": round(price, 4), "score": round(score, 2), "amount": round(gross, 2)})

        long_value = sum(quantity * prices.get((symbol, day), 0) for symbol, quantity in shares.items())
        nav = cash + long_value
        curve.append({"date": day.isoformat(), "nav": round(nav, 2), "gross_exposure": round(long_value / nav * 100, 2) if nav > 0 else 0})

    nav = pd.Series([point["nav"] for point in curve], dtype=float)
    peak_nav = 0.0
    for point in curve:
        peak_nav = max(peak_nav, float(point["nav"]))
        point["drawdown_pct"] = round((float(point["nav"]) / peak_nav - 1) * 100, 2) if peak_nav else 0.0
    returns = nav.pct_change().dropna()
    total_return = nav.iloc[-1] / initial_capital - 1
    annualized = (nav.iloc[-1] / initial_capital) ** (252 / max(len(returns), 1)) - 1
    drawdown = (nav / nav.cummax() - 1).min()
    sharpe = returns.mean() / returns.std() * math.sqrt(252) if len(returns) > 1 and returns.std() > 0 else None
    gross = pd.Series([point["gross_exposure"] for point in curve], dtype=float) / 100
    final_day = days[-1]
    final_nav = float(nav.iloc[-1])
    latest_positions = []
    for stock in stocks:
        code = stock["code"]
        quantity = shares[code]
        price = prices.get((code, final_day))
        if quantity <= 0 or not price:
            continue
        value = quantity * price
        latest_positions.append({
            "code": code, "name": stock["name"], "quantity": quantity,
            "price": round(price, 4), "score": scores.get((code, final_day)),
            "market_value": round(value, 2), "position_pct": round(value / final_nav * 100, 2) if final_nav > 0 else 0,
        })
    return {
        "period": {"start_date": curve[0]["date"], "end_date": curve[-1]["date"], "sessions": len(returns)},
        "rules": {"execution": "same_close; sell_before_buy", "allow_leverage": False, "sell_on_ema5_breakdown": False, "rotation_enabled": rotation_enabled, "rotation_threshold": rotation_threshold if rotation_enabled else None},
        "metrics": {
            "initial_capital": round(initial_capital, 2), "final_nav": round(float(nav.iloc[-1]), 2),
            "total_return_pct": round(total_return * 100, 2), "annualized_return_pct": round(annualized * 100, 2),
            "max_drawdown_pct": round(drawdown * 100, 2), "sharpe": round(float(sharpe), 2) if sharpe is not None else None,
            "trade_count": len(trades), "buy_count": sum(item["side"] == "BUY" for item in trades), "sell_count": sum(item["side"] in {"SELL", "ROTATE_SELL"} for item in trades), "rotation_sell_count": rotation_sell_count,
            "avg_gross_exposure_pct": round(float(gross.mean() * 100), 2), "peak_gross_exposure_pct": round(float(gross.max() * 100), 2),
            "max_leverage_ratio": round(float(gross.max()), 4),
            "capital_efficiency_pct": round(float(annualized / gross.mean() * 100), 2) if gross.mean() > 0 else None,
            "financing_cost": 0.0,
        },
        "curve": curve, "trades": trades, "latest_positions": latest_positions,
        "coverage": {"configured_stocks": len(stocks), "history_loaded": len(histories), "types": list(US_STOCK_TYPES)},
    }

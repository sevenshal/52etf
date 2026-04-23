import logging
import os
import threading
import uuid
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import date, datetime
from heapq import heappush, heapreplace
from itertools import product
from typing import Dict, Iterator, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError, validator

from ...core.services.longport import LongPortService
from ...core.services.quote import QuoteService
from .account import valid_account

router = APIRouter(prefix="/api/soxl-fear-backtest", tags=["SOXL Fear Backtest"])
logger = logging.getLogger(__name__)
SEARCH_JOBS: Dict[str, Dict] = {}
SEARCH_JOBS_LOCK = threading.Lock()
SEARCH_JOB_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="soxl-fear-search")
SEARCH_EVAL_MAX_WORKERS = min(8, max(2, (os.cpu_count() or 4) // 2))

CNN_HEADERS = {
    "accept": "*/*",
    "accept-language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "cache-control": "no-cache",
    "origin": "https://www.cnn.com",
    "pragma": "no-cache",
    "referer": "https://www.cnn.com/",
    "user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
}
CNN_BASE_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"


class SOXLFearStrategyParams(BaseModel):
    buy_threshold: float = 40.0
    greed_threshold: float = 40.0
    volume_ratio_threshold: float = 1.4
    buy_position_pct: float = 10.0
    cooldown_days: int = 5
    trailing_stop_pct: float = 8.0
    sell_position_pct: float = 50.0
    sell_reduction_basis: str = "portfolio"
    max_take_profit_sells_per_cycle: int = 3
    min_position_pct_after_take_profit: float = 10.0
    rebalance_threshold_pct: float = 5.0

    @validator("buy_position_pct", "trailing_stop_pct", "sell_position_pct")
    def validate_positive_percent(cls, value):
        if value <= 0 or value > 100:
            raise ValueError("百分比参数必须在 0 到 100 之间")
        return value

    @validator("min_position_pct_after_take_profit")
    def validate_min_position_pct_after_take_profit(cls, value):
        if value < 0 or value > 100:
            raise ValueError("百分比参数必须在 0 到 100 之间")
        return value

    @validator("rebalance_threshold_pct")
    def validate_rebalance_threshold_pct(cls, value):
        if value < 0 or value > 100:
            raise ValueError("调仓阈值必须在 0 到 100 之间")
        return value

    @validator("cooldown_days")
    def validate_cooldown(cls, value):
        if value < 0:
            raise ValueError("冷却天数不能小于 0")
        return value

    @validator("buy_threshold", "greed_threshold")
    def validate_threshold(cls, value):
        if value < 0 or value > 100:
            raise ValueError("阈值必须在 0 到 100 之间")
        return value

    @validator("max_take_profit_sells_per_cycle")
    def validate_max_take_profit_sells_per_cycle(cls, value):
        if value < 1 or value > 20:
            raise ValueError("同轮止盈最多卖出次数必须在 1 到 20 之间")
        return value

    @validator("sell_reduction_basis")
    def validate_sell_reduction_basis(cls, value):
        if value not in {"portfolio", "holdings"}:
            raise ValueError("止盈减仓口径仅支持 portfolio 或 holdings")
        return value

class SOXLFearSearchParams(BaseModel):
    symbol: str = "SOXL.US"
    initial_capital: float = 100000.0
    start_date: str = "2021-01-01"
    end_date: Optional[str] = None
    top_n: int = 20
    objective: str = "annualized_return"
    eval_workers: Optional[int] = None
    rebalance_threshold_pct: float = 5.0
    buy_threshold_values: List[float] = Field(default_factory=lambda: [30.0, 40.0, 50.0])
    greed_threshold_values: List[float] = Field(default_factory=lambda: [30.0, 40.0, 50.0])
    volume_ratio_threshold_values: List[float] = Field(default_factory=lambda: [1.2, 1.4, 1.6])
    buy_position_pct_values: List[float] = Field(default_factory=lambda: [40.0, 50.0, 60.0])
    cooldown_days_values: List[int] = Field(default_factory=lambda: [5, 10, 15])
    trailing_stop_pct_values: List[float] = Field(default_factory=lambda: [3.0, 5.0, 7.0])
    sell_position_pct_values: List[float] = Field(default_factory=lambda: [40.0, 50.0, 60.0])
    sell_reduction_basis_values: List[str] = Field(default_factory=lambda: ["portfolio", "holdings"])
    max_take_profit_sells_per_cycle_values: List[int] = Field(default_factory=lambda: [1, 2, 3])
    min_position_pct_after_take_profit_values: List[float] = Field(default_factory=lambda: [5.0, 10.0, 15.0])

    @validator("top_n")
    def validate_top_n(cls, value):
        if value <= 0 or value > 500:
            raise ValueError("top_n 必须在 1 到 500 之间")
        return value

    @validator("objective")
    def validate_objective(cls, value):
        if value not in {"annualized_return", "sharpe_ratio"}:
            raise ValueError("objective 仅支持 annualized_return 或 sharpe_ratio")
        return value

    @validator("eval_workers")
    def validate_eval_workers(cls, value):
        if value is None:
            return value
        if value < 1 or value > 16:
            raise ValueError("eval_workers 必须在 1 到 16 之间")
        return value

    @validator("rebalance_threshold_pct")
    def validate_search_rebalance_threshold_pct(cls, value):
        if value < 0 or value > 100:
            raise ValueError("调仓阈值必须在 0 到 100 之间")
        return value


class SOXLFearSearchJobCreated(BaseModel):
    task_id: str
    status: str
    total_combinations: int


class SOXLFearSearchJobStatus(BaseModel):
    task_id: str
    status: str
    progress: int = 0
    processed_combinations: int = 0
    total_combinations: int = 0
    skipped_combinations: int = 0
    message: Optional[str] = None
    result: Optional[Dict] = None
    error: Optional[str] = None


def _parse_date(value: Optional[str], default: Optional[date] = None) -> date:
    if not value:
        if default is None:
            raise ValueError("日期不能为空")
        return default
    return datetime.strptime(value, "%Y-%m-%d").date()


def _safe_float(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, float) and np.isnan(value):
        return None
    return float(value)


def _round_or_none(value, digits: int = 4):
    numeric = _safe_float(value)
    if numeric is None:
        return None
    return round(numeric, digits)


def _floor_share_count(value: float) -> int:
    if value <= 0:
        return 0
    return int(np.floor(value))


def _ceil_share_count(value: float) -> int:
    if value <= 0:
        return 0
    return int(np.ceil(value))


def _describe_df_range(df: pd.DataFrame, date_col: str = "date") -> str:
    if df is None or df.empty or date_col not in df.columns:
        return "0 rows"
    return f"{len(df)} rows, {df.iloc[0][date_col]} ~ {df.iloc[-1][date_col]}"


def _fetch_cnn_history(start_date: date, end_date: date) -> pd.DataFrame:
    response = requests.get(
        f"{CNN_BASE_URL}/{start_date.isoformat()}",
        headers=CNN_HEADERS,
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("fear_and_greed_historical", {}).get("data", [])
    if not rows:
        raise ValueError("CNN 历史恐贪数据为空")

    df = pd.DataFrame([
        {
            "date": datetime.utcfromtimestamp(item["x"] / 1000).date(),
            "cnn_fear_greed": float(item["y"]),
        }
        for item in rows
        if item.get("x") is not None and item.get("y") is not None
    ])
    df = df.drop_duplicates(subset=["date"], keep="last")
    df = df[(df["date"] >= start_date) & (df["date"] <= end_date)].sort_values("date")
    if df.empty:
        raise ValueError("指定区间内没有 CNN 恐贪数据")
    return df


def _fetch_price_history(symbol: str, start_date: date, end_date: date) -> pd.DataFrame:
    quote_service = QuoteService(LongPortService.get_instance())
    klines = quote_service.get_klines(
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
    )
    if not klines:
        raise ValueError(f"{symbol} 在指定区间内没有 K 线数据")

    df = pd.DataFrame([
        {
            "date": item["timestamp"].date() if hasattr(item["timestamp"], "date") else item["timestamp"],
            "open": float(item["open"]),
            "high": float(item["high"]),
            "low": float(item["low"]),
            "close": float(item["close"]),
            "volume": float(item["volume"]),
            "turnover": float(item["turnover"]),
        }
        for item in klines
    ]).sort_values("date")
    if df.empty:
        raise ValueError(f"{symbol} 在指定区间内没有可用 K 线")
    return df


def _prepare_base_dataframe(symbol: str, start_date: date, end_date: date) -> Tuple[pd.DataFrame, Dict]:
    price_df = _fetch_price_history(symbol, start_date, end_date)
    cnn_df = _fetch_cnn_history(start_date, end_date)

    merged_df = price_df.merge(cnn_df, on="date", how="left")
    merged_df = merged_df.sort_values("date").reset_index(drop=True)
    merged_df["cnn_fear_greed"] = merged_df["cnn_fear_greed"].ffill()
    merged_df["ma20"] = merged_df["close"].rolling(20).mean()
    merged_df["volume_ma20"] = merged_df["volume"].rolling(20).mean()
    merged_df["volume_ratio"] = np.where(
        merged_df["volume_ma20"] > 0,
        merged_df["volume"] / merged_df["volume_ma20"],
        np.nan,
    )
    base_df = merged_df.dropna(subset=["cnn_fear_greed", "ma20", "volume_ma20"]).reset_index(drop=True)

    if base_df.empty:
        diagnostics = (
            f"price={_describe_df_range(price_df)}; "
            f"cnn={_describe_df_range(cnn_df)}; "
            f"merged={_describe_df_range(merged_df)}; "
            f"merged_non_null_cnn={int(merged_df['cnn_fear_greed'].notna().sum()) if 'cnn_fear_greed' in merged_df else 0}; "
            f"merged_non_null_ma20={int(merged_df['ma20'].notna().sum()) if 'ma20' in merged_df else 0}; "
            f"merged_non_null_volume_ma20={int(merged_df['volume_ma20'].notna().sum()) if 'volume_ma20' in merged_df else 0}"
        )
        raise ValueError(
            f"{symbol} 与 CNN 恐贪没有足够重叠的数据区间。"
            f" 请求区间: {start_date} ~ {end_date}。"
            f" 诊断: {diagnostics}"
        )

    meta = {
        "requested_symbol": symbol,
        "requested_start_date": start_date.isoformat(),
        "requested_end_date": end_date.isoformat(),
        "effective_start_date": base_df.iloc[0]["date"].isoformat(),
        "effective_end_date": base_df.iloc[-1]["date"].isoformat(),
        "trading_days": int(len(base_df)),
        "cnn_points": int(len(cnn_df)),
        "price_points": int(len(price_df)),
    }
    return base_df, meta


def _compute_yearly_returns(equity_df: pd.DataFrame) -> List[Dict]:
    if equity_df.empty:
        return []
    yearly = []
    grouped = equity_df.groupby(equity_df["date"].str[:4])
    for year, group in grouped:
        start_value = float(group.iloc[0]["value"])
        end_value = float(group.iloc[-1]["value"])
        yearly.append({
            "year": year,
            "return": ((end_value / start_value) - 1) * 100 if start_value > 0 else 0.0,
        })
    return yearly


def _compute_yearly_returns_from_arrays(date_strings: List[str], equity_values: np.ndarray) -> List[Dict]:
    if len(date_strings) == 0 or len(equity_values) == 0:
        return []

    yearly = []
    current_year = date_strings[0][:4]
    start_value = float(equity_values[0])
    last_value = float(equity_values[0])

    for date_str, value in zip(date_strings, equity_values):
        year = date_str[:4]
        numeric_value = float(value)
        if year != current_year:
            yearly.append({
                "year": current_year,
                "return": ((last_value / start_value) - 1) * 100 if start_value > 0 else 0.0,
            })
            current_year = year
            start_value = numeric_value
        last_value = numeric_value

    yearly.append({
        "year": current_year,
        "return": ((last_value / start_value) - 1) * 100 if start_value > 0 else 0.0,
    })
    return yearly


def _run_backtest(base_df: pd.DataFrame, params: SOXLFearStrategyParams, initial_capital: float, detailed: bool = False) -> Dict:
    dates = base_df["date"].tolist()
    date_strings = [item.isoformat() if hasattr(item, "isoformat") else str(item) for item in dates]
    close_prices = base_df["close"].to_numpy(dtype=float, copy=False)
    volumes = base_df["volume"].to_numpy(dtype=float, copy=False)
    ma20_values = base_df["ma20"].to_numpy(dtype=float, copy=False)
    volume_ma20_values = base_df["volume_ma20"].to_numpy(dtype=float, copy=False)
    volume_ratios = base_df["volume_ratio"].to_numpy(dtype=float, copy=False)
    cnn_values = base_df["cnn_fear_greed"].to_numpy(dtype=float, copy=False)

    if detailed:
        open_prices = base_df["open"].to_numpy(dtype=float, copy=False)
        high_prices = base_df["high"].to_numpy(dtype=float, copy=False)
        low_prices = base_df["low"].to_numpy(dtype=float, copy=False)

    cash = float(initial_capital)
    shares = 0
    avg_cost = 0.0
    cooldown_remaining = 0
    greed_peak_price = None
    take_profit_sell_count_in_cycle = 0
    trades: List[Dict] = []
    equity_curve: List[Dict] = []
    daily_data: List[Dict] = []
    closed_trade_count = 0
    winning_trade_count = 0
    equity_values = np.empty(len(close_prices), dtype=float)
    benchmark_values = np.empty(len(close_prices), dtype=float)

    benchmark_shares = _floor_share_count(initial_capital / float(close_prices[0])) if float(close_prices[0]) > 0 else 0
    benchmark_cash = initial_capital - benchmark_shares * float(close_prices[0]) if float(close_prices[0]) > 0 else initial_capital

    for index in range(len(close_prices)):
        current_date = date_strings[index]
        close_price = float(close_prices[index])
        cnn_score = float(cnn_values[index])
        volume_ratio = float(volume_ratios[index])
        can_trade = cooldown_remaining == 0
        action_taken = False

        if cooldown_remaining > 0:
            cooldown_remaining -= 1
            can_trade = False

        is_fear = cnn_score <= params.buy_threshold
        is_greedy = cnn_score >= params.greed_threshold

        if shares > 0:
            if is_greedy:
                greed_peak_price = max(greed_peak_price or close_price, close_price)
            else:
                greed_peak_price = None
                take_profit_sell_count_in_cycle = 0
        else:
            greed_peak_price = None
            take_profit_sell_count_in_cycle = 0

        if (
            shares > 0
            and is_greedy
            and can_trade
            and greed_peak_price
            and take_profit_sell_count_in_cycle < params.max_take_profit_sells_per_cycle
        ):
            drawdown_from_peak = ((greed_peak_price - close_price) / greed_peak_price) * 100 if greed_peak_price > 0 else 0.0
            if drawdown_from_peak >= params.trailing_stop_pct and close_price > avg_cost:
                portfolio_value = cash + shares * close_price
                current_position_pct = (shares * close_price / portfolio_value * 100) if portfolio_value > 0 else 0.0
                min_hold_shares = (
                    portfolio_value * (params.min_position_pct_after_take_profit / 100.0) / close_price
                    if portfolio_value > 0 and close_price > 0
                    else 0.0
                )
                max_sell_shares = max(0, shares - _floor_share_count(min_hold_shares))
                if params.sell_reduction_basis == "holdings":
                    requested_sell_shares = _floor_share_count(shares * (params.sell_position_pct / 100.0))
                else:
                    requested_sell_shares = _floor_share_count(
                        portfolio_value * (params.sell_position_pct / 100.0) / close_price
                    )
                sell_shares = min(shares, requested_sell_shares, max_sell_shares)

                if current_position_pct > params.min_position_pct_after_take_profit and sell_shares >= 1:
                    sell_amount = sell_shares * close_price
                    sell_adjustment_pct = (sell_amount / portfolio_value * 100) if portfolio_value > 0 else 0.0
                    if sell_adjustment_pct <= params.rebalance_threshold_pct:
                        sell_shares = 0

                if current_position_pct > params.min_position_pct_after_take_profit and sell_shares >= 1:
                    sell_amount = sell_shares * close_price
                    cost_amount = sell_shares * avg_cost
                    profit = sell_amount - cost_amount
                    profit_pct = ((close_price / avg_cost) - 1) * 100 if avg_cost > 0 else 0.0

                    cash += sell_amount
                    shares -= sell_shares
                    holdings_value_after = shares * close_price
                    net_value_after = cash + holdings_value_after
                    if shares <= 0:
                        shares = 0
                        avg_cost = 0.0
                        greed_peak_price = None
                        take_profit_sell_count_in_cycle = 0
                    else:
                        # Reset the trailing-stop anchor after a partial take-profit.
                        # This avoids repeatedly selling against the same historical peak
                        # without a fresh rebound/new high inside the take-profit regime.
                        greed_peak_price = close_price
                        take_profit_sell_count_in_cycle += 1

                    cooldown_remaining = params.cooldown_days
                    action_taken = True
                    closed_trade_count += 1
                    if profit > 0:
                        winning_trade_count += 1

                    trades.append({
                        "date": current_date,
                        "action": "SELL",
                        "price": close_price,
                        "shares": sell_shares,
                        "amount": sell_amount,
                        "cash_after": cash,
                        "position_after": shares,
                        "holdings_value_after": holdings_value_after,
                        "net_value_after": net_value_after,
                        "position_pct_after": (holdings_value_after / net_value_after * 100) if net_value_after > 0 else 0.0,
                        "avg_cost_after": avg_cost,
                        "profit": profit,
                        "profit_pct": profit_pct,
                        "reason": (
                            f"CNN {cnn_score:.2f} 进入止盈区后回撤 {drawdown_from_peak:.2f}% 触发移动止盈"
                            f"，本轮第 {take_profit_sell_count_in_cycle} 次卖出"
                        ),
                        "cnn_score": cnn_score,
                        "volume_ratio": volume_ratio,
                    })

        if not action_taken and is_fear and volume_ratio >= params.volume_ratio_threshold and can_trade:
            portfolio_value = cash + shares * close_price
            buy_amount = min(cash, portfolio_value * (params.buy_position_pct / 100.0))
            if buy_amount > 0:
                buy_shares = min(_floor_share_count(buy_amount / close_price), _floor_share_count(cash / close_price))
                if buy_shares >= 1:
                    actual_buy_amount = buy_shares * close_price
                    buy_adjustment_pct = (actual_buy_amount / portfolio_value * 100) if portfolio_value > 0 else 0.0
                    if buy_adjustment_pct <= params.rebalance_threshold_pct:
                        buy_shares = 0
                if buy_shares >= 1:
                    actual_buy_amount = buy_shares * close_price
                    total_cost = shares * avg_cost + actual_buy_amount
                    shares += buy_shares
                    avg_cost = total_cost / shares if shares > 0 else 0.0
                    cash -= actual_buy_amount
                    cooldown_remaining = params.cooldown_days
                    greed_peak_price = None
                    take_profit_sell_count_in_cycle = 0
                    holdings_value_after = shares * close_price
                    net_value_after = cash + holdings_value_after

                    trades.append({
                        "date": current_date,
                        "action": "BUY",
                        "price": close_price,
                        "shares": buy_shares,
                        "amount": actual_buy_amount,
                        "cash_after": cash,
                        "position_after": shares,
                        "holdings_value_after": holdings_value_after,
                        "net_value_after": net_value_after,
                        "position_pct_after": (holdings_value_after / net_value_after * 100) if net_value_after > 0 else 0.0,
                        "avg_cost_after": avg_cost,
                        "profit": 0.0,
                        "profit_pct": 0.0,
                        "reason": f"CNN {cnn_score:.2f} 进入买入区 + 成交量放大 {volume_ratio:.2f}",
                        "cnn_score": cnn_score,
                        "volume_ratio": volume_ratio,
                    })

        equity_value = cash + shares * close_price
        benchmark_value = benchmark_cash + benchmark_shares * close_price
        equity_values[index] = equity_value
        benchmark_values[index] = benchmark_value

        if detailed:
            equity_curve.append({
                "date": current_date,
                "value": equity_value,
                "benchmark_value": benchmark_value,
            })
            daily_data.append({
                "date": current_date,
                "open": float(open_prices[index]),
                "high": float(high_prices[index]),
                "low": float(low_prices[index]),
                "close": close_price,
                "volume": float(volumes[index]),
                "ma20": float(ma20_values[index]),
                "volume_ma20": float(volume_ma20_values[index]),
                "volume_ratio": volume_ratio,
                "cnn_fear_greed": float(cnn_values[index]),
                "equity": equity_value,
                "cash": cash,
                "shares": shares,
                "avg_cost": avg_cost,
                "benchmark_value": benchmark_value,
            })

    start_value = float(equity_values[0])
    end_value = float(equity_values[-1])
    total_return = ((end_value / start_value) - 1) * 100 if start_value > 0 else 0.0
    annualized_return = 0.0
    if start_value > 0 and len(equity_values) > 1:
        annualized_return = ((end_value / start_value) ** (252 / len(equity_values)) - 1) * 100

    cumulative_peaks = np.maximum.accumulate(equity_values)
    drawdowns = (equity_values / cumulative_peaks) - 1
    max_drawdown = abs(float(drawdowns.min())) * 100 if len(drawdowns) > 0 else 0.0
    returns = np.diff(equity_values) / equity_values[:-1] if len(equity_values) > 1 else np.array([])
    sharpe_ratio = 0.0
    if len(returns) > 1 and float(np.std(returns)) > 0:
        sharpe_ratio = float((float(np.mean(returns)) / float(np.std(returns))) * np.sqrt(252))
    calmar_ratio = float(annualized_return / max_drawdown) if max_drawdown > 0 else 0.0
    win_rate = (winning_trade_count / closed_trade_count * 100) if closed_trade_count > 0 else 0.0

    result = {
        "params": params.dict(),
        "total_return": total_return,
        "annualized_return": annualized_return,
        "max_drawdown": max_drawdown,
        "sharpe_ratio": sharpe_ratio,
        "calmar_ratio": calmar_ratio,
        "win_rate": win_rate,
        "trade_count": len(trades),
        "buy_count": sum(1 for item in trades if item["action"] == "BUY"),
        "sell_count": sum(1 for item in trades if item["action"] == "SELL"),
        "ending_value": end_value,
        "ending_cash": cash,
        "ending_shares": shares,
        "yearly_returns": _compute_yearly_returns_from_arrays(date_strings, equity_values),
    }
    if detailed:
        result["trades"] = trades
        result["equity_curve"] = equity_curve
        result["daily_data"] = daily_data
    return result


def _count_search_params(payload: SOXLFearSearchParams) -> int:
    value_groups = [
        payload.buy_threshold_values,
        payload.greed_threshold_values,
        payload.volume_ratio_threshold_values,
        payload.buy_position_pct_values,
        payload.cooldown_days_values,
        payload.trailing_stop_pct_values,
        payload.sell_position_pct_values,
        payload.sell_reduction_basis_values,
        payload.max_take_profit_sells_per_cycle_values,
        payload.min_position_pct_after_take_profit_values,
    ]
    total = 1
    for values in value_groups:
        if not values:
            return 0
        total *= len(values)
    return total


def _iter_search_params(payload: SOXLFearSearchParams) -> Iterator[SOXLFearStrategyParams]:
    for values in product(
        payload.buy_threshold_values,
        payload.greed_threshold_values,
        payload.volume_ratio_threshold_values,
        payload.buy_position_pct_values,
        payload.cooldown_days_values,
        payload.trailing_stop_pct_values,
        payload.sell_position_pct_values,
        payload.sell_reduction_basis_values,
        payload.max_take_profit_sells_per_cycle_values,
        payload.min_position_pct_after_take_profit_values,
    ):
        yield SOXLFearStrategyParams(
            buy_threshold=float(values[0]),
            greed_threshold=float(values[1]),
            volume_ratio_threshold=float(values[2]),
            buy_position_pct=float(values[3]),
            cooldown_days=int(values[4]),
            trailing_stop_pct=float(values[5]),
            sell_position_pct=float(values[6]),
            sell_reduction_basis=str(values[7]),
            max_take_profit_sells_per_cycle=int(values[8]),
            min_position_pct_after_take_profit=float(values[9]),
            rebalance_threshold_pct=float(payload.rebalance_threshold_pct),
        )


def _evaluate_search_candidates(
    payload: SOXLFearSearchParams,
    base_df: pd.DataFrame,
    progress_callback=None,
) -> Tuple[List[Dict], Dict, int]:
    total_combinations = _count_search_params(payload)
    eval_workers = payload.eval_workers or SEARCH_EVAL_MAX_WORKERS
    eval_batch_size = max(16, eval_workers * 8)
    top_results_heap = []
    best_summary = None
    best_sort_key = None
    skipped_combinations = 0
    processed_combinations = 0

    def handle_completed_result(index: int, sort_key: Tuple[float, float, float, float], result: Dict):
        nonlocal best_summary
        nonlocal best_sort_key

        if best_summary is None or best_sort_key is None or sort_key > best_sort_key:
            best_summary = result
            best_sort_key = sort_key

        heap_entry = (sort_key, index, result)
        if len(top_results_heap) < payload.top_n:
            heappush(top_results_heap, heap_entry)
        elif sort_key > top_results_heap[0][0]:
            heapreplace(top_results_heap, heap_entry)

    def iter_value_batches() -> Iterator[List[Tuple[int, Tuple]]]:
        batch = []
        for index, values in enumerate(
            product(
                payload.buy_threshold_values,
                payload.greed_threshold_values,
                payload.volume_ratio_threshold_values,
                payload.buy_position_pct_values,
                payload.cooldown_days_values,
                payload.trailing_stop_pct_values,
                payload.sell_position_pct_values,
                payload.sell_reduction_basis_values,
                payload.max_take_profit_sells_per_cycle_values,
                payload.min_position_pct_after_take_profit_values,
            ),
            start=1,
        ):
            batch.append((index, values))
            if len(batch) >= eval_batch_size:
                yield batch
                batch = []
        if batch:
            yield batch

    def flush_futures(futures_map):
        nonlocal processed_combinations
        nonlocal skipped_combinations
        for future in as_completed(list(futures_map.keys())):
            batch_meta = futures_map[future]
            try:
                batch_result = future.result()
                for index, sort_key, result in batch_result["results"]:
                    handle_completed_result(index, sort_key, result)
                skipped_combinations += batch_result["skipped_combinations"]
                for index, message in batch_result["skip_messages"]:
                    if skipped_combinations <= 5 or skipped_combinations % 100 == 0:
                        logger.warning(
                            "Skipping invalid SOXL fear parameter combination %s/%s: %s",
                            index,
                            total_combinations,
                            message,
                        )
                processed_combinations += batch_result["processed_combinations"]
            except Exception as exc:
                skipped_combinations += batch_meta["batch_size"]
                processed_combinations += batch_meta["batch_size"]
                logger.warning(
                    "Skipping failed SOXL fear parameter batch %s-%s: %s",
                    batch_meta["start_index"],
                    batch_meta["end_index"],
                    str(exc),
                )
            if progress_callback:
                progress_callback(processed_combinations, total_combinations, skipped_combinations)

    with ProcessPoolExecutor(max_workers=eval_workers) as executor:
        futures_map = {}
        max_pending_batches = max(eval_workers * 2, 2)

        for batch in iter_value_batches():
            future = executor.submit(
                _evaluate_search_batch,
                base_df,
                payload.initial_capital,
                payload.objective,
                payload.rebalance_threshold_pct,
                batch,
            )
            futures_map[future] = {
                "start_index": batch[0][0],
                "end_index": batch[-1][0],
                "batch_size": len(batch),
            }

            if len(futures_map) >= max_pending_batches:
                flush_futures(futures_map)
                futures_map = {}

        if futures_map:
            flush_futures(futures_map)

    results = [item[2] for item in sorted(top_results_heap, key=lambda item: item[0], reverse=True)]
    if not results or best_summary is None:
        raise ValueError("没有可用的回测结果")

    return results, best_summary, skipped_combinations


def _result_sort_key(result: Dict, objective: str = "annualized_return") -> Tuple[float, float, float, float]:
    if objective == "sharpe_ratio":
        return (
            float(result["sharpe_ratio"]),
            float(result["annualized_return"]),
            float(result["calmar_ratio"]),
            -float(result["max_drawdown"]),
        )
    return (
        float(result["annualized_return"]),
        float(result["sharpe_ratio"]),
        float(result["calmar_ratio"]),
        -float(result["max_drawdown"]),
    )


def _evaluate_search_batch(
    base_df: pd.DataFrame,
    initial_capital: float,
    objective: str,
    rebalance_threshold_pct: float,
    batch_items: List[Tuple[int, Tuple]],
) -> Dict:
    results = []
    skipped_combinations = 0
    skip_messages = []

    for index, values in batch_items:
        try:
            params = SOXLFearStrategyParams(
                buy_threshold=float(values[0]),
                greed_threshold=float(values[1]),
                volume_ratio_threshold=float(values[2]),
                buy_position_pct=float(values[3]),
                cooldown_days=int(values[4]),
                trailing_stop_pct=float(values[5]),
                sell_position_pct=float(values[6]),
                sell_reduction_basis=str(values[7]),
                max_take_profit_sells_per_cycle=int(values[8]),
                min_position_pct_after_take_profit=float(values[9]),
                rebalance_threshold_pct=float(rebalance_threshold_pct),
            )
        except ValidationError as exc:
            skipped_combinations += 1
            if len(skip_messages) < 5:
                skip_messages.append((index, exc.errors()[0].get("msg", str(exc))))
            continue

        result = _run_backtest(base_df, params, initial_capital, detailed=False)
        sort_key = _result_sort_key(result, objective)
        results.append((index, sort_key, result))

    return {
        "processed_combinations": len(batch_items),
        "skipped_combinations": skipped_combinations,
        "skip_messages": skip_messages,
        "results": results,
    }


def _serialize_summary(result: Dict) -> Dict:
    serialized = {
        "annualized_return": _round_or_none(result["annualized_return"], 4),
        "total_return": _round_or_none(result["total_return"], 4),
        "max_drawdown": _round_or_none(result["max_drawdown"], 4),
        "sharpe_ratio": _round_or_none(result["sharpe_ratio"], 4),
        "calmar_ratio": _round_or_none(result["calmar_ratio"], 4),
        "win_rate": _round_or_none(result["win_rate"], 4),
        "trade_count": int(result["trade_count"]),
        "buy_count": int(result["buy_count"]),
        "sell_count": int(result["sell_count"]),
    }
    serialized.update(result["params"])
    return serialized


def _build_search_response(payload: SOXLFearSearchParams) -> Dict:
    start_date = _parse_date(payload.start_date)
    end_date = _parse_date(payload.end_date, default=date.today())
    if start_date >= end_date:
        raise ValueError("开始日期必须早于结束日期")

    total_combinations = _count_search_params(payload)
    if total_combinations <= 0:
        raise ValueError("至少需要提供一组有效的超参数候选值")

    base_df, meta = _prepare_base_dataframe(payload.symbol, start_date, end_date)
    logger.info(
        "Starting SOXL fear parameter search, symbol=%s, combinations=%s, top_n=%s",
        payload.symbol,
        total_combinations,
        payload.top_n,
    )

    def progress_callback(index: int, total: int, skipped: int):
        if index % 1000 == 0 or index == total:
            logger.info(
                "SOXL fear parameter search progress %s/%s (%.2f%%), skipped=%s",
                index,
                total,
                (index / total) * 100,
                skipped,
            )

    results, best_summary, skipped_combinations = _evaluate_search_candidates(
        payload,
        base_df,
        progress_callback=progress_callback,
    )

    best_result = _run_backtest(
        base_df,
        SOXLFearStrategyParams(**best_summary["params"]),
        payload.initial_capital,
        detailed=True,
    )

    return {
        "meta": {
            **meta,
            "initial_capital": payload.initial_capital,
            "eval_workers": payload.eval_workers or SEARCH_EVAL_MAX_WORKERS,
            "searched_combinations": total_combinations,
            "skipped_combinations": skipped_combinations,
            "valid_combinations": total_combinations - skipped_combinations,
            "returned_results": min(payload.top_n, len(results)),
            "objective": payload.objective,
        },
        "results": [_serialize_summary(item) for item in results[:payload.top_n]],
        "best_result": {
            **best_result,
            "params": best_result["params"],
        },
    }


def _cleanup_finished_jobs(max_age_hours: int = 12):
    threshold = datetime.now().timestamp() - max_age_hours * 3600
    with SEARCH_JOBS_LOCK:
        expired_ids = [
            task_id
            for task_id, job in SEARCH_JOBS.items()
            if job.get("updated_at", 0) < threshold and job.get("status") in {"completed", "failed"}
        ]
        for task_id in expired_ids:
            SEARCH_JOBS.pop(task_id, None)


def _update_search_job(task_id: str, **updates):
    with SEARCH_JOBS_LOCK:
        job = SEARCH_JOBS.get(task_id)
        if not job:
            return
        job.update(updates)
        job["updated_at"] = datetime.now().timestamp()


def _run_search_job(task_id: str, payload: SOXLFearSearchParams):
    try:
        start_date = _parse_date(payload.start_date)
        end_date = _parse_date(payload.end_date, default=date.today())
        if start_date >= end_date:
            raise ValueError("开始日期必须早于结束日期")

        total_combinations = _count_search_params(payload)
        if total_combinations <= 0:
            raise ValueError("至少需要提供一组有效的超参数候选值")

        _update_search_job(
            task_id,
            status="running",
            progress=1,
            total_combinations=total_combinations,
            processed_combinations=0,
            skipped_combinations=0,
            message="正在准备回测数据",
        )

        logger.info(
            "Starting SOXL fear parameter search job, task_id=%s, symbol=%s, start_date=%s, end_date=%s, combinations=%s, top_n=%s",
            task_id,
            payload.symbol,
            start_date,
            end_date,
            total_combinations,
            payload.top_n,
        )

        base_df, meta = _prepare_base_dataframe(payload.symbol, start_date, end_date)

        def progress_callback(index: int, total: int, skipped: int):
            if index == 1 or index % 100 == 0 or index == total:
                progress = min(99, max(1, int(index / total * 100)))
                _update_search_job(
                    task_id,
                    progress=progress,
                    processed_combinations=index,
                    total_combinations=total,
                    skipped_combinations=skipped,
                    message=f"正在评估参数组合 {index}/{total}",
                )

            if index % 1000 == 0 or index == total:
                logger.info(
                    "SOXL fear parameter search job=%s progress %s/%s (%.2f%%), skipped=%s",
                    task_id,
                    index,
                    total,
                    (index / total) * 100,
                    skipped,
                )

        results, best_summary, skipped_combinations = _evaluate_search_candidates(
            payload,
            base_df,
            progress_callback=progress_callback,
        )

        _update_search_job(
            task_id,
            progress=99,
            processed_combinations=total_combinations,
            total_combinations=total_combinations,
            skipped_combinations=skipped_combinations,
            message="正在生成最优参数的详细回测结果",
        )

        best_result = _run_backtest(
            base_df,
            SOXLFearStrategyParams(**best_summary["params"]),
            payload.initial_capital,
            detailed=True,
        )

        result_payload = {
            "meta": {
                **meta,
                "initial_capital": payload.initial_capital,
                "eval_workers": payload.eval_workers or SEARCH_EVAL_MAX_WORKERS,
                "searched_combinations": total_combinations,
                "skipped_combinations": skipped_combinations,
                "valid_combinations": total_combinations - skipped_combinations,
                "returned_results": min(payload.top_n, len(results)),
                "objective": payload.objective,
            },
            "results": [_serialize_summary(item) for item in results[:payload.top_n]],
            "best_result": {
                **best_result,
                "params": best_result["params"],
            },
        }

        _update_search_job(
            task_id,
            status="completed",
            progress=100,
            processed_combinations=total_combinations,
            total_combinations=total_combinations,
            skipped_combinations=skipped_combinations,
            message="搜索完成",
            result=result_payload,
        )
    except Exception as exc:
        logger.exception("SOXL fear parameter search job failed, task_id=%s", task_id)
        _update_search_job(
            task_id,
            status="failed",
            message="搜索失败",
            error=str(exc),
        )


@router.post("/search")
def search_soxl_fear_params(
    payload: SOXLFearSearchParams,
    account_id: str = Depends(valid_account),
):
    try:
        return _build_search_response(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/search/jobs", response_model=SOXLFearSearchJobCreated)
def create_soxl_fear_search_job(
    payload: SOXLFearSearchParams,
    account_id: str = Depends(valid_account),
):
    try:
        start_date = _parse_date(payload.start_date)
        end_date = _parse_date(payload.end_date, default=date.today())
        if start_date >= end_date:
            raise ValueError("开始日期必须早于结束日期")

        total_combinations = _count_search_params(payload)
        if total_combinations <= 0:
            raise ValueError("至少需要提供一组有效的超参数候选值")

        _cleanup_finished_jobs()
        task_id = uuid.uuid4().hex
        with SEARCH_JOBS_LOCK:
            SEARCH_JOBS[task_id] = {
                "task_id": task_id,
                "status": "pending",
                "progress": 0,
                "processed_combinations": 0,
                "total_combinations": total_combinations,
                "skipped_combinations": 0,
                "message": "任务已创建，等待执行",
                "result": None,
                "error": None,
                "updated_at": datetime.now().timestamp(),
            }

        SEARCH_JOB_EXECUTOR.submit(_run_search_job, task_id, payload)
        return SOXLFearSearchJobCreated(
            task_id=task_id,
            status="pending",
            total_combinations=total_combinations,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/search/jobs/{task_id}", response_model=SOXLFearSearchJobStatus)
def get_soxl_fear_search_job_status(
    task_id: str,
    account_id: str = Depends(valid_account),
):
    with SEARCH_JOBS_LOCK:
        job = SEARCH_JOBS.get(task_id)
        if not job:
            raise HTTPException(status_code=404, detail="任务不存在或已过期")
        return SOXLFearSearchJobStatus(**job)


class SOXLFearRunParams(BaseModel):
    symbol: str = "SOXL.US"
    initial_capital: float = 100000.0
    start_date: str = "2021-01-01"
    end_date: Optional[str] = None
    params: SOXLFearStrategyParams


@router.post("/run")
def run_soxl_fear_backtest(
    payload: SOXLFearRunParams,
    account_id: str = Depends(valid_account),
):
    try:
        start_date = _parse_date(payload.start_date)
        end_date = _parse_date(payload.end_date, default=date.today())
        if start_date >= end_date:
            raise ValueError("开始日期必须早于结束日期")

        base_df, meta = _prepare_base_dataframe(payload.symbol, start_date, end_date)
        result = _run_backtest(base_df, payload.params, payload.initial_capital, detailed=True)
        result["meta"] = {
            **meta,
            "initial_capital": payload.initial_capital,
        }
        return result
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

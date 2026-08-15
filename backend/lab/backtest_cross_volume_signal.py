#!/usr/bin/env python3
"""Lab: 上证红利恐贪 + 红利低波量能/标的 交叉回测.

User's original simple strategy (reported on the A-Stock Fear ETF Backtest page):
  - 买入: 极恐(fear<30) + 20日量比≥1.3 → 买 100%
  - 卖出: 极贪(fear>70) → 卖 100%
Baseline run: 000015.SH(上证红利恐贪) -> 510880.SH(红利ETF).
New experiment: 000015.SH(上证红利恐贪) -> 512890.SH(红利低波ETF, 量能也用它的).

2x2 矩阵把"量能来源"和"交易标的"两个变量拆开：
  fear=000015.SH 固定, 量能来源 ∈ {510880, 512890}, 交易标的 ∈ {510880, 512890}
再加一组纯红利低波对照 (fear=H30269.CSI, 量能/交易=512890)。

Signals form at close, execute at next open — same semantics as the API engine.
"""

from __future__ import annotations

import os

from datetime import date, timedelta

import duckdb
import numpy as np
import pandas as pd

# 只读访问生产数据（quantd 组可读）。刻意不 import src.core.database，
# 避免触发其 schema 迁移写逻辑。
DB_PATH = "/home/quantd/quant_prod/quant_robot/evc_stocks.db"
ANALYTICS_DB_PATH = "/home/quantd/quant_prod/quant_robot/analytics.duckdb"

from src.core.services.a_stock_fear_etf_backtest_engine import (  # noqa: E402
    build_bars_by_date,
    build_fear_by_date,
    build_signal_rows,
    load_etf_bars,
    load_fear,
    prepare_fear_features,
    prepare_market_features,
    run_backtest,
    summarize,
)

START = "2023-03-22"
END = "2026-08-14"
INITIAL_CAPITAL = 1_000_000.0


def make_params(**overrides):
    base = {
        "extreme_fear_threshold": 30.0,
        "volume_ratio_threshold": 1.3,
        "volume_window": 20,
        "bottom_fear_threshold": 20.0,
        "bottom_ma_window": 5,
        "extreme_buy_fraction": 1.0,
        "bottom_buy_fraction": 0.0,     # 关闭"见底反转"买入，只留极恐放量
        "greed_threshold": 70.0,
        "greed_sell_fraction": 1.0,     # 极贪清仓
        "stop_loss_pct": 100.0,         # 关闭固定止损
        "stop_cooldown_days": 20,
        "volatility_window": 20,
        "volatility_baseline_window": 20,
        "volatility_std_multiplier": 0.5,
        "trailing_drawdown_pct": 5.0,
        "max_positions": 1,
        "commission_pct": 0.03,
        "min_commission": 5.0,
        "slippage_pct": 0.02,
        "stamp_duty_pct": 0.0,
        "lot_size": 100,
        "sort_by_fear": True,
        "buy_when_flat_only": True,
        "top_sell_threshold": None,     # 关闭见顶反转卖出
    }
    base.update(overrides)
    return base


def load_fear_and_bars(indexes: list[str], etfs: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    start = date.fromisoformat(START)
    end = date.fromisoformat(END)
    padding = max(180, 20 * 3)
    feature_start = (start - timedelta(days=padding)).isoformat()
    fear = load_fear(DB_PATH, indexes, feature_start, end.isoformat())
    with duckdb.connect(ANALYTICS_DB_PATH, read_only=True) as conn:
        bars = load_etf_bars(conn, etfs, feature_start, end.isoformat())
    return fear, bars


def build_signal_rows_cross(
    fear: pd.DataFrame,
    volume_bars: pd.DataFrame,
    mapping: dict[str, str],
    params: dict,
) -> dict[object, list[dict]]:
    """信号 = 指定指数的恐贪分数 + 指定量能标的的 20 日量比."""
    featured_bars = prepare_market_features(
        volume_bars,
        volume_window=params["volume_window"],
        volatility_window=params["volatility_window"],
        volatility_baseline_window=params["volatility_baseline_window"],
        volatility_std_multiplier=params["volatility_std_multiplier"],
    )
    featured_fear = prepare_fear_features(fear, params["bottom_ma_window"])
    return build_signal_rows(
        featured_bars, featured_fear, mapping,
        extreme_fear_threshold=params["extreme_fear_threshold"],
        volume_ratio_threshold=params["volume_ratio_threshold"],
        bottom_fear_threshold=params["bottom_fear_threshold"],
        extreme_buy_fraction=params["extreme_buy_fraction"],
        bottom_buy_fraction=params["bottom_buy_fraction"],
        start_date=START, end_date=END, sort_by_fear=params["sort_by_fear"],
    )


def run_case(
    name: str,
    *,
    fear_index: str,
    volume_etf: str,
    trade_etf: str,
    params: dict,
    show_trades: bool = False,
) -> dict:
    """fear_index 的恐贪分数 + volume_etf 的量比 → 交易 trade_etf."""
    fear, volume_bars = load_fear_and_bars([fear_index], [volume_etf])
    _, trade_bars = load_fear_and_bars([fear_index], [trade_etf])
    mapping = {fear_index: volume_etf}
    signals = build_signal_rows_cross(fear, volume_bars, mapping, params)

    # 信号里的 etf_symbol 是量能标的；重写为实际交易标的，让 run_backtest 用交易标的行情成交
    if volume_etf != trade_etf:
        signals = {
            day: [{**item, "etf_symbol": trade_etf} for item in items]
            for day, items in signals.items()
        }
    _, featured_trade_bars = load_fear_and_bars([fear_index], [trade_etf])
    featured_trade_bars = prepare_market_features(
        featured_trade_bars,
        volume_window=params["volume_window"],
        volatility_window=params["volatility_window"],
        volatility_baseline_window=params["volatility_baseline_window"],
        volatility_std_multiplier=params["volatility_std_multiplier"],
    )
    curve, trades = run_backtest(
        featured_trade_bars, fear, signals,
        start_date=START, end_date=END,
        initial_capital=INITIAL_CAPITAL,
        greed_threshold=params["greed_threshold"],
        greed_sell_fraction=params["greed_sell_fraction"],
        stop_loss=params["stop_loss_pct"] / 100,
        stop_cooldown_days=params["stop_cooldown_days"],
        trailing_drawdown=params["trailing_drawdown_pct"] / 100,
        commission_pct=params["commission_pct"], min_commission=params["min_commission"],
        slippage_pct=params["slippage_pct"], stamp_duty_pct=params["stamp_duty_pct"],
        lot_size=params["lot_size"], max_positions=params["max_positions"],
        buy_when_flat_only=params["buy_when_flat_only"], top_signals=None,
    )
    summary = summarize(curve, trades, INITIAL_CAPITAL)
    summary["calmar_ratio"] = (
        summary["annualized_return_pct"] / abs(summary["max_drawdown_pct"])
        if summary.get("max_drawdown_pct") else None
    )
    print(f"\n===== {name} =====")
    print(f"贪恐={fear_index} 量能={volume_etf} 交易={trade_etf}  信号日数={len(signals)}")
    for key in (
        "total_return_pct", "annualized_return_pct", "max_drawdown_pct",
        "sharpe_zero_rf", "annualized_volatility_pct", "average_exposure_pct",
        "average_holding_count", "buy_count", "sell_count", "realized_pnl",
        "final_value", "turnover_pct", "closed_trade_win_rate_pct",
    ):
        print(f"  {key}: {summary.get(key)}")
    if show_trades and len(trades):
        cols = ["date", "signal_date", "action", "etf_symbol", "quantity", "price", "reason", "fear_score", "volume_ratio"]
        with pd.option_context("display.max_rows", None, "display.width", 200):
            print(trades[cols].to_string(index=False))
    return {"name": name, "summary": summary, "curve": curve, "trades": trades, "signals": signals}


if __name__ == "__main__":
    simple = make_params()

    # 1) 复现用户基线: 上证红利恐贪 + 红利ETF自身量能/买卖
    run_case("A 基线: 上证红利恐贪→红利ETF量能/标的(510880)",
             fear_index="000015.SH", volume_etf="510880.SH", trade_etf="510880.SH",
             params=simple, show_trades=True)

    # 2) 用户新想法: 贪恐用上证红利, 量能用红利低波ETF, 买卖红利低波ETF
    run_case("B 实验: 上证红利恐贪→红利低波量能/标的(512890)",
             fear_index="000015.SH", volume_etf="512890.SH", trade_etf="512890.SH",
             params=simple, show_trades=True)

    # 3) 拆变量: 量能仍用红利ETF(510880), 但交易红利低波(512890) —— 看"交易标的变化"单独的影响
    run_case("C 拆解: 上证红利恐贪→红利ETF量能(510880)→交易红利低波(512890)",
             fear_index="000015.SH", volume_etf="510880.SH", trade_etf="512890.SH",
             params=simple, show_trades=False)

    # 4) 拆变量: 量能用红利低波(512890), 但交易红利ETF(510880) —— 看"量能来源变化"单独的影响
    run_case("D 拆解: 上证红利恐贪→红利低波量能(512890)→交易红利ETF(510880)",
             fear_index="000015.SH", volume_etf="512890.SH", trade_etf="510880.SH",
             params=simple, show_trades=False)

    # 5) 对照组: 红利低波自己的恐贪 + 自己的量能/买卖
    run_case("E 对照: 红利低波恐贪→红利低波量能/标的(512890)",
             fear_index="H30269.CSI", volume_etf="512890.SH", trade_etf="512890.SH",
             params=simple, show_trades=False)

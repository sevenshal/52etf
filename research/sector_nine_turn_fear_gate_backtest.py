#!/usr/bin/env python3
"""板块九转 + 贪恐闸门 → 成分股九转的择时回测。

规则（用户口径）：

1. 板块（贪恐配置里的 64 个 A 股指数）自身出现神奇九转的低 9（``lowCount >= 9``）后，
   **首次**出现高 2（``highCount == 2``）的那一天为板块触发日；同一次低 9 只消费一次。
2. 板块触发日的自算贪恐分数必须 <= 40（分数越低越恐慌）。
3. 板块触发后进入一段"布防窗口"（默认 5 个交易日）。窗口内，该板块当时权重快照里的成分股
   若自身也出现"低 9 后首次高 2"，就在信号次日开盘买入。
4. 卖出：持仓股自买入后出现高 9（``highCount >= 9``），随后首次出现低 2（``lowCount == 2``）
   且最近红点收盘到当前收盘的回撤 > 2 × ATR14 时，在信号次日开盘卖出。

九转/ATR 一律调用生产同一套 ``append_nine_turn_atr``（与个股详情页 K 线图同口径），
不另写近似实现。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.src.core.services.stock_system.indicators import (  # noqa: E402
    append_nine_turn_atr,
)
from backend.src.robot.a_stock_base_data_config import (  # noqa: E402
    A_STOCK_INDEX_FEAR_GREED_TARGETS,
)

DEFAULT_ANALYTICS_DB = "/home/quantd/quant_prod/quant_robot/analytics.duckdb"
DEFAULT_SQLITE_DB = "/home/quantd/quant_prod/quant_robot/evc_stocks.db"
DEFAULT_START = date(2023, 1, 1)
DEFAULT_OUTPUT = "research/output/sector_nine_turn_fear_gate_backtest"
WARMUP_DAYS = 400
BENCHMARK_INDEX = "000985.SH"
BENCHMARK_LABEL = "中证全指"

FEAR_THRESHOLD = 40.0
ARM_WINDOW = 5
SELL_ATR_THRESHOLD = 2.0
MAX_POSITIONS = 10
COMMISSION_RATE = 0.0003
STAMP_DUTY_RATE = 0.0005
TRADING_DAYS_PER_YEAR = 244

SELL_MODES = ("low2_wait", "low2_first_only", "low_ge2_wait")
RANDOM_TRIALS = 25
RANDOM_TRIAL_VARIANTS = (
    "full", "full_window0", "full_window3", "no_fear", "stock_only",
    "full_industry_only", "full_pos20", "full_window0_pos20",
)

# 宽基/风格指数不是"板块"，单独拆出来看行业主题板块的效果。
BROAD_MARKET_INDEXES = frozenset({
    "000300.SH", "000016.SH", "000510.SH", "000905.SH", "000852.SH", "932000.CSI",
    "000985.SH", "899050.BJ", "000680.SH", "000688.SH", "000698.SH", "000699.SH", "399006.SZ",
})


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------

def nine_turn_frame(bars: pd.DataFrame) -> pd.DataFrame:
    """用生产口径的 ``append_nine_turn_atr`` 逐根补九转计数、ATR 和红点回撤。"""
    ordered = bars.sort_values("trade_date").reset_index(drop=True)
    records = ordered[["open", "high", "low", "close"]].astype(float).to_dict("records")
    enriched = pd.DataFrame(append_nine_turn_atr(records))
    enriched["trade_date"] = ordered["trade_date"].to_numpy()
    if "name" in ordered.columns:
        enriched["name"] = ordered["name"].to_numpy()
    return enriched


def low9_high2_signals(frame: pd.DataFrame) -> list[int]:
    """低 9 后首次高 2 的行号；同一次低 9 只触发一次。"""
    armed = False
    fired: list[int] = []
    for index, (low_count, high_count) in enumerate(
        zip(frame["lowCount"].to_numpy(int), frame["highCount"].to_numpy(int))
    ):
        if low_count >= 9:
            armed = True
        if armed and high_count == 2:
            fired.append(index)
            armed = False
    return fired


def sell_row(frame: pd.DataFrame, entry_index: int, sell_mode: str,
             atr_threshold: float = SELL_ATR_THRESHOLD) -> int | None:
    """买入后第一个满足卖出规则的行号（信号日，次日开盘成交）。"""
    high_counts = frame["highCount"].to_numpy(int)
    low_counts = frame["lowCount"].to_numpy(int)
    drawdowns = pd.to_numeric(frame["risingDrawdownAtr"], errors="coerce").to_numpy(float)
    armed = False
    for index in range(entry_index, len(frame)):
        if high_counts[index] >= 9:
            armed = True
            continue
        if not armed:
            continue
        low_count = low_counts[index]
        drawdown = drawdowns[index]
        if sell_mode == "low_ge2_wait":
            matched_count = low_count >= 2
        else:
            matched_count = low_count == 2
        if not matched_count:
            continue
        if np.isfinite(drawdown) and drawdown > atr_threshold:
            return index
        if sell_mode == "low2_first_only":
            # 高 9 后的第一个低 2 回撤不够，本轮作废，等下一次高 9。
            armed = False
    return None


# ---------------------------------------------------------------------------
# 数据
# ---------------------------------------------------------------------------

@dataclass
class Inputs:
    fear: pd.DataFrame
    index_bars: dict[str, pd.DataFrame]
    weights: pd.DataFrame
    index_names: dict[str, str]
    benchmark: pd.DataFrame


def load_inputs(analytics_db: str, sqlite_db: str, codes: Sequence[str],
                start: date, end: date) -> Inputs:
    uri = f"file:{Path(sqlite_db).resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        placeholders = ",".join("?" for _ in codes)
        fear = pd.read_sql_query(
            f"""
            SELECT symbol, date, score
            FROM etf_fear_greed_clone_history
            WHERE symbol IN ({placeholders})
            ORDER BY symbol, date
            """,
            connection,
            params=list(codes),
            parse_dates=["date"],
        )
    finally:
        connection.close()

    duck = duckdb.connect(analytics_db, read_only=True)
    try:
        # 中证全指既是板块之一又是基准，去重后再 join，否则它的 K 线会被匹配两次。
        duck.register("wanted_indexes", pd.DataFrame({"ts_code": sorted(set(codes) | {BENCHMARK_INDEX})}))
        bars = duck.execute(
            """
            SELECT d.ts_code, d.trade_date, d.open, d.high, d.low, d.close
            FROM a_stock_index_daily d
            JOIN wanted_indexes w USING (ts_code)
            WHERE d.trade_date BETWEEN ? AND ?
              AND d.open > 0 AND d.high > 0 AND d.low > 0 AND d.close > 0
            ORDER BY d.ts_code, d.trade_date
            """,
            [start - timedelta(days=WARMUP_DAYS), end],
        ).df()
        duck.register("wanted_weight_indexes", pd.DataFrame({"index_code": list(codes)}))
        weights = duck.execute(
            """
            SELECT w.index_code, w.trade_date, w.con_code
            FROM a_stock_index_weight w
            JOIN wanted_weight_indexes i USING (index_code)
            ORDER BY w.index_code, w.trade_date, w.con_code
            """
        ).df()
    finally:
        duck.close()

    bars = bars.drop_duplicates(["ts_code", "trade_date"]).reset_index(drop=True)
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    weights["trade_date"] = pd.to_datetime(weights["trade_date"])
    index_bars = {code: group.reset_index(drop=True) for code, group in bars.groupby("ts_code", sort=True)}
    names = {
        str(item["symbol"]).upper(): str(item.get("ticker") or item["symbol"])
        for item in A_STOCK_INDEX_FEAR_GREED_TARGETS
    }
    return Inputs(
        fear=fear,
        index_bars=index_bars,
        weights=weights,
        index_names={code: names.get(code, code) for code in codes},
        benchmark=index_bars.get(BENCHMARK_INDEX, pd.DataFrame()),
    )


def load_stock_bars(analytics_db: str, symbols: Sequence[str], start: date, end: date) -> dict[str, pd.DataFrame]:
    duck = duckdb.connect(analytics_db, read_only=True)
    try:
        duck.register("candidate_codes", pd.DataFrame({"ts_code": sorted(set(symbols))}))
        prices = duck.execute(
            """
            SELECT q.ts_code, b.name, q.trade_date, q.open, q.high, q.low, q.close
            FROM a_stock_market_daily_qfq q
            JOIN candidate_codes c USING (ts_code)
            LEFT JOIN a_stock_basic b USING (ts_code)
            WHERE q.trade_date BETWEEN ? AND ?
              AND q.open > 0 AND q.high > 0 AND q.low > 0 AND q.close > 0
              AND q.vol > 0
            ORDER BY q.ts_code, q.trade_date
            """,
            [start - timedelta(days=WARMUP_DAYS), end],
        ).df()
    finally:
        duck.close()
    prices["trade_date"] = pd.to_datetime(prices["trade_date"])
    return {code: group.reset_index(drop=True) for code, group in prices.groupby("ts_code", sort=True)}


# ---------------------------------------------------------------------------
# 板块层
# ---------------------------------------------------------------------------

def sector_triggers(inputs: Inputs, codes: Sequence[str], start: date, end: date,
                    fear_threshold: float | None) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    fear_by_code = {
        code: group.set_index(group["date"].dt.normalize())["score"].astype(float)
        for code, group in inputs.fear.groupby("symbol", sort=True)
    }
    for code in codes:
        bars = inputs.index_bars.get(code)
        if bars is None or bars.empty:
            continue
        frame = nine_turn_frame(bars)
        scores = fear_by_code.get(code)
        dates = pd.DatetimeIndex(frame["trade_date"])
        for position in low9_high2_signals(frame):
            day = dates[position].date()
            if not (start <= day <= end):
                continue
            score = float(scores.get(dates[position], np.nan)) if scores is not None else np.nan
            rows.append({
                "index_code": code,
                "index_name": inputs.index_names.get(code, code),
                "signal_date": day,
                "fear_score": score,
                "fear_pass": bool(np.isfinite(score) and score <= fear_threshold)
                if fear_threshold is not None else True,
                "fear_available": bool(np.isfinite(score)),
            })
    return pd.DataFrame(rows)


def members_as_of(snapshots: np.ndarray, members: dict[pd.Timestamp, frozenset[str]],
                  day: date) -> frozenset[str]:
    position = int(np.searchsorted(snapshots, np.datetime64(day), side="right") - 1)
    if position < 0:
        return frozenset()
    return members.get(pd.Timestamp(snapshots[position]), frozenset())


# ---------------------------------------------------------------------------
# 候选交易
# ---------------------------------------------------------------------------

def build_trades(stock_frames: dict[str, pd.DataFrame],
                 stock_signals: dict[str, list[int]],
                 triggers: pd.DataFrame,
                 membership: dict[str, tuple[np.ndarray, dict[pd.Timestamp, frozenset[str]]]],
                 arm_window: int,
                 sell_mode: str,
                 start: date,
                 end: date,
                 require_sector: bool = True) -> pd.DataFrame:
    """把板块触发 × 成分股信号配成候选交易（含开平仓日期与价格）。"""
    rows: list[dict[str, Any]] = []
    if require_sector:
        armed: dict[str, list[tuple[date, str, str, float]]] = {}
        for trigger in triggers.itertuples(index=False):
            snapshots, members = membership.get(trigger.index_code, (np.array([], dtype="datetime64[ns]"), {}))
            for symbol in members_as_of(snapshots, members, trigger.signal_date):
                armed.setdefault(symbol, []).append(
                    (trigger.signal_date, trigger.index_code, trigger.index_name, trigger.fear_score)
                )
    else:
        armed = {symbol: [] for symbol in stock_signals}

    for symbol, positions in stock_signals.items():
        frame = stock_frames.get(symbol)
        if frame is None or frame.empty:
            continue
        dates = pd.DatetimeIndex(frame["trade_date"])
        day_list = [timestamp.date() for timestamp in dates]
        sector_events = sorted(armed.get(symbol, []))
        sector_days = [event[0] for event in sector_events]
        opens = frame["open"].astype(float).to_numpy()
        closes = frame["close"].astype(float).to_numpy()
        for position in positions:
            signal_day = day_list[position]
            if not (start <= signal_day <= end):
                continue
            sector_code = sector_name = None
            sector_day = None
            fear_score = np.nan
            if require_sector:
                # 板块触发日 <= 个股信号日，且相隔不超过 arm_window 个交易日。
                cut = bisect_right(sector_days, signal_day)
                matched = None
                for event in reversed(sector_events[:cut]):
                    # 板块触发日落在个股交易日历上的位置，用交易日数衡量布防窗口。
                    anchor = bisect_right(day_list, event[0]) - 1
                    if anchor < 0:
                        break
                    gap = position - anchor
                    if gap <= arm_window:
                        matched = event
                        break
                    break
                if matched is None:
                    continue
                sector_day, sector_code, sector_name, fear_score = matched
            if position + 1 >= len(frame):
                continue
            entry_index = position + 1
            exit_signal = sell_row(frame, entry_index, sell_mode)
            entry_price = float(opens[entry_index])
            if exit_signal is not None and exit_signal + 1 < len(frame):
                exit_index = exit_signal + 1
                exit_price = float(opens[exit_index])
                closed = True
            else:
                exit_index = len(frame) - 1
                exit_price = float(closes[exit_index])
                closed = False
            gross = exit_price / entry_price
            net = gross * (1 - COMMISSION_RATE) * (1 - COMMISSION_RATE - STAMP_DUTY_RATE) - 1
            # 同一笔买入若不用卖出规则、一路持到样本末尾的净收益，用来单独检验卖出规则的价值。
            hold_to_end = (
                float(closes[-1]) / entry_price * (1 - COMMISSION_RATE) * (1 - COMMISSION_RATE - STAMP_DUTY_RATE) - 1
            )
            rows.append({
                "ts_code": symbol,
                "name": str(frame["name"].iloc[-1]) if "name" in frame.columns else "",
                "index_code": sector_code,
                "index_name": sector_name,
                "sector_signal_date": sector_day,
                "sector_fear_score": fear_score,
                "buy_signal_date": signal_day,
                "buy_date": day_list[entry_index],
                "buy_price": entry_price,
                "sell_signal_date": day_list[exit_signal] if exit_signal is not None else None,
                "sell_date": day_list[exit_index],
                "sell_price": exit_price,
                "closed": closed,
                "sell_drawdown_atr": float(frame["risingDrawdownAtr"].iloc[exit_signal])
                if exit_signal is not None else np.nan,
                "holding_trading_days": exit_index - entry_index,
                "holding_calendar_days": (day_list[exit_index] - day_list[entry_index]).days,
                "gross_return": gross - 1,
                "net_return": net,
                "hold_to_end_return": hold_to_end,
                "entry_index": entry_index,
                "exit_index": exit_index,
            })
    trades = pd.DataFrame(rows)
    if not trades.empty:
        trades = trades.sort_values(["buy_date", "ts_code"]).reset_index(drop=True)
    return trades


# ---------------------------------------------------------------------------
# 组合回测
# ---------------------------------------------------------------------------

def close_lookup_table(stock_frames: dict[str, pd.DataFrame]) -> dict[str, dict[date, float]]:
    """逐日盯市用的收盘价表；随机排序试验要跑很多遍，提前建好避免重复构造。"""
    return {
        symbol: dict(zip(
            (timestamp.date() for timestamp in pd.DatetimeIndex(frame["trade_date"])),
            frame["close"].astype(float).to_numpy(),
        ))
        for symbol, frame in stock_frames.items()
    }


def simulate_portfolio(trades: pd.DataFrame, close_lookup: dict[str, dict[date, float]],
                       calendar: list[date], max_positions: int,
                       seed: int | None = None, priority: str = "fear") -> tuple[pd.Series, pd.DataFrame]:
    """同一天信号多于空仓位时的取舍会明显影响结果。

    ``seed=None`` 走确定性排序：``priority='fear'`` 是"板块贪恐分数低（更恐慌）优先"，
    ``priority='code'`` 只按代码排序——关掉贪恐闸门的消融必须用后者，否则排序里又把贪恐分数
    偷偷用回来了。给定 seed 则当天随机排序，用来衡量"挑哪几只"带来的运气成分。
    """
    if trades.empty:
        return pd.Series(dtype=float), pd.DataFrame()
    entries: dict[date, list[int]] = {
        day: list(group) for day, group in trades.groupby("buy_date", sort=True).groups.items()
    }
    if seed is None:
        codes = trades["ts_code"].astype(str)
        scores = (
            pd.to_numeric(trades["sector_fear_score"], errors="coerce").fillna(50.0)
            if priority == "fear"
            else pd.Series(0.0, index=trades.index)
        )
        ranks = {index: (float(scores.loc[index]), codes.loc[index]) for index in trades.index}
        for rows in entries.values():
            rows.sort(key=ranks.__getitem__)
    else:
        generator = np.random.default_rng(seed)
        for rows in entries.values():
            generator.shuffle(rows)
    cash = 1.0
    positions: dict[str, dict[str, Any]] = {}
    equity_rows: list[float] = []
    taken: list[int] = []
    last_price: dict[str, float] = {}

    for day in calendar:
        for symbol in list(positions):
            position = positions[symbol]
            if position["sell_date"] <= day:
                cash += position["units"] * position["sell_price"] * (1 - COMMISSION_RATE - STAMP_DUTY_RATE)
                positions.pop(symbol)
        for row_index in entries.get(day, []):
            row = trades.loc[row_index]
            symbol = row["ts_code"]
            if symbol in positions or len(positions) >= max_positions:
                continue
            equity = cash + sum(
                item["units"] * last_price.get(code, item["buy_price"])
                for code, item in positions.items()
            )
            budget = min(cash, equity / max_positions)
            if budget <= 1e-9:
                continue
            units = budget * (1 - COMMISSION_RATE) / float(row["buy_price"])
            cash -= budget
            positions[symbol] = {
                "units": units,
                "buy_price": float(row["buy_price"]),
                "sell_date": row["sell_date"],
                "sell_price": float(row["sell_price"]),
            }
            taken.append(row_index)
        market_value = 0.0
        for symbol, position in positions.items():
            price = (close_lookup.get(symbol) or {}).get(day)
            if price is None or not np.isfinite(price):
                price = last_price.get(symbol, position["buy_price"])
            last_price[symbol] = price
            market_value += position["units"] * price
        equity_rows.append(cash + market_value)

    equity = pd.Series(equity_rows, index=pd.DatetimeIndex(calendar), name="equity")
    return equity, trades.loc[taken].copy()


def maximum_drawdown(values: pd.Series) -> float:
    return float((values / values.cummax() - 1).min()) if len(values) else float("nan")


def performance(equity: pd.Series) -> dict[str, float]:
    if equity.empty:
        return {}
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1 / 365.25)
    total = float(equity.iloc[-1] / equity.iloc[0] - 1)
    daily = equity.pct_change().dropna()
    volatility = float(daily.std() * np.sqrt(TRADING_DAYS_PER_YEAR)) if len(daily) > 1 else float("nan")
    cagr = float((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1)
    drawdown = maximum_drawdown(equity)
    return {
        "total_return": total,
        "cagr": cagr,
        "annual_volatility": volatility,
        "sharpe_like": cagr / volatility if volatility and np.isfinite(volatility) and volatility > 0 else float("nan"),
        "max_drawdown": drawdown,
        "calmar": cagr / abs(drawdown) if drawdown and np.isfinite(drawdown) and drawdown < 0 else float("nan"),
    }


def trade_stats(trades: pd.DataFrame) -> dict[str, Any]:
    if trades.empty:
        return {"trades": 0}
    closed = trades[trades["closed"]]
    return {
        "trades": int(len(trades)),
        "closed_trades": int(len(closed)),
        "open_at_end": int((~trades["closed"]).sum()),
        "win_rate": float((trades["net_return"] > 0).mean()),
        "mean_return": float(trades["net_return"].mean()),
        "median_return": float(trades["net_return"].median()),
        "median_holding_trading_days": float(trades["holding_trading_days"].median()),
        "profit_factor": float(
            trades.loc[trades["net_return"] > 0, "net_return"].sum()
            / abs(trades.loc[trades["net_return"] < 0, "net_return"].sum())
        ) if (trades["net_return"] < 0).any() else float("inf"),
        "best": float(trades["net_return"].max()),
        "worst": float(trades["net_return"].min()),
        "mean_hold_to_end_return": float(trades["hold_to_end_return"].mean()),
        "median_hold_to_end_return": float(trades["hold_to_end_return"].median()),
        "beat_hold_to_end_pct": float((trades["net_return"] > trades["hold_to_end_return"]).mean()),
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def analyze(analytics_db: str = DEFAULT_ANALYTICS_DB,
            sqlite_db: str = DEFAULT_SQLITE_DB,
            output: str | Path = DEFAULT_OUTPUT,
            start: date = DEFAULT_START,
            end: date | None = None,
            index_codes: Iterable[str] | None = None,
            max_positions: int = MAX_POSITIONS) -> dict[str, Any]:
    all_codes = [str(item["symbol"]).upper() for item in A_STOCK_INDEX_FEAR_GREED_TARGETS]
    codes = [code for code in (index_codes or all_codes) if code in set(all_codes)]
    if not codes:
        raise ValueError("no configured A-share fear/greed indexes selected")

    if end is None:
        uri = f"file:{Path(sqlite_db).resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            end = pd.Timestamp(
                connection.execute("SELECT max(date) FROM etf_fear_greed_clone_history").fetchone()[0]
            ).date()

    inputs = load_inputs(analytics_db, sqlite_db, codes, start, end)
    triggers = sector_triggers(inputs, codes, start, end, FEAR_THRESHOLD)
    if triggers.empty:
        raise ValueError("no sector low9->high2 triggers in window")

    membership: dict[str, tuple[np.ndarray, dict[pd.Timestamp, frozenset[str]]]] = {}
    for code, group in inputs.weights.groupby("index_code", sort=True):
        members = {
            pd.Timestamp(snapshot): frozenset(part["con_code"].astype(str))
            for snapshot, part in group.groupby("trade_date", sort=True)
        }
        membership[code] = (np.array(sorted(members), dtype="datetime64[ns]"), members)

    gated = triggers[triggers["fear_pass"]]
    candidate_codes: set[str] = set()
    for trigger in triggers.itertuples(index=False):
        snapshots, members = membership.get(trigger.index_code, (np.array([], dtype="datetime64[ns]"), {}))
        candidate_codes.update(members_as_of(snapshots, members, trigger.signal_date))
    print(f"[info] sector triggers={len(triggers)} fear-gated={len(gated)} candidate stocks={len(candidate_codes)}")

    raw_bars = load_stock_bars(analytics_db, sorted(candidate_codes), start, end)
    stock_frames = {symbol: nine_turn_frame(bars) for symbol, bars in raw_bars.items() if len(bars) > 30}
    stock_signals = {symbol: low9_high2_signals(frame) for symbol, frame in stock_frames.items()}
    print(f"[info] stock frames={len(stock_frames)} low9->high2 signals={sum(len(v) for v in stock_signals.values())}")

    benchmark_bars = inputs.benchmark
    benchmark_bars = benchmark_bars[
        (benchmark_bars["trade_date"].dt.date >= start) & (benchmark_bars["trade_date"].dt.date <= end)
    ].reset_index(drop=True)
    calendar = [timestamp.date() for timestamp in pd.DatetimeIndex(benchmark_bars["trade_date"])]
    benchmark_equity = pd.Series(
        (benchmark_bars["close"] / benchmark_bars["close"].iloc[0]).to_numpy(float),
        index=pd.DatetimeIndex(benchmark_bars["trade_date"]),
        name="benchmark",
    )

    close_lookup = close_lookup_table(stock_frames)
    variants = [
        {"key": "full", "label": "板块九转+贪恐<=40+个股九转", "triggers": gated,
         "arm_window": ARM_WINDOW, "sell_mode": "low2_wait", "require_sector": True},
        {"key": "no_fear", "label": "板块九转+个股九转（去掉贪恐闸门）", "triggers": triggers,
         "arm_window": ARM_WINDOW, "sell_mode": "low2_wait", "require_sector": True, "fear_gated": False},
        {"key": "stock_only", "label": "只看个股九转（不看板块）", "triggers": triggers,
         "arm_window": ARM_WINDOW, "sell_mode": "low2_wait", "require_sector": False, "fear_gated": False},
    ]
    for window in (0, 3, 10, 20):
        variants.append({
            "key": f"full_window{window}", "label": f"完整规则·布防窗口 {window} 个交易日",
            "triggers": gated, "arm_window": window, "sell_mode": "low2_wait", "require_sector": True,
        })
    industry = gated[~gated["index_code"].isin(BROAD_MARKET_INDEXES)]
    broad = gated[gated["index_code"].isin(BROAD_MARKET_INDEXES)]
    variants.append({"key": "full_industry_only", "label": "完整规则·只用行业主题板块",
                     "triggers": industry, "arm_window": ARM_WINDOW,
                     "sell_mode": "low2_wait", "require_sector": True})
    variants.append({"key": "full_broad_only", "label": "完整规则·只用宽基/风格指数",
                     "triggers": broad, "arm_window": ARM_WINDOW,
                     "sell_mode": "low2_wait", "require_sector": True})
    variants.append({"key": "full_industry_window0", "label": "只用行业主题板块·布防窗口 0 个交易日",
                     "triggers": industry, "arm_window": 0,
                     "sell_mode": "low2_wait", "require_sector": True})
    # 信号远多于仓位，放宽持仓上限看结果对"能装下多少信号"有多敏感。
    variants.append({"key": "full_pos20", "label": "完整规则·最多 20 只持仓",
                     "triggers": gated, "arm_window": ARM_WINDOW, "sell_mode": "low2_wait",
                     "require_sector": True, "max_positions": 20})
    variants.append({"key": "full_window0_pos20", "label": "布防窗口 0·最多 20 只持仓",
                     "triggers": gated, "arm_window": 0, "sell_mode": "low2_wait",
                     "require_sector": True, "max_positions": 20})
    for mode in ("low2_first_only", "low_ge2_wait"):
        variants.append({
            "key": f"full_sell_{mode}", "label": f"完整规则·卖出口径 {mode}",
            "triggers": gated, "arm_window": ARM_WINDOW, "sell_mode": mode, "require_sector": True,
        })

    summary_rows: list[dict[str, Any]] = []
    all_trades: dict[str, pd.DataFrame] = {}
    equity_curves: dict[str, pd.Series] = {}
    for variant in variants:
        trades = build_trades(
            stock_frames, stock_signals, variant["triggers"], membership,
            variant["arm_window"], variant["sell_mode"], start, end, variant["require_sector"],
        )
        priority = "fear" if variant.get("fear_gated", True) else "code"
        slots = int(variant.get("max_positions", max_positions))
        equity, taken = simulate_portfolio(trades, close_lookup, calendar, slots, priority=priority)
        stats = trade_stats(trades)
        portfolio_stats = performance(equity) if not equity.empty else {}
        taken_stats = trade_stats(taken) if not taken.empty else {}
        trial_returns: list[float] = []
        trial_drawdowns: list[float] = []
        if variant["key"] in RANDOM_TRIAL_VARIANTS and not trades.empty:
            for seed in range(RANDOM_TRIALS):
                trial_equity, _ = simulate_portfolio(trades, close_lookup, calendar, slots, seed, priority)
                trial_stats = performance(trial_equity)
                trial_returns.append(trial_stats["total_return"])
                trial_drawdowns.append(trial_stats["max_drawdown"])
        summary_rows.append({
            "variant": variant["key"],
            "label": variant["label"],
            "max_positions": slots,
            "signal_trades": stats.get("trades", 0),
            "signal_win_rate": stats.get("win_rate", np.nan),
            "signal_mean_return": stats.get("mean_return", np.nan),
            "signal_median_return": stats.get("median_return", np.nan),
            "signal_median_holding_days": stats.get("median_holding_trading_days", np.nan),
            "signal_profit_factor": stats.get("profit_factor", np.nan),
            "signal_open_at_end": stats.get("open_at_end", np.nan),
            "signal_median_hold_to_end": stats.get("median_hold_to_end_return", np.nan),
            "signal_beat_hold_to_end_pct": stats.get("beat_hold_to_end_pct", np.nan),
            "portfolio_trades": taken_stats.get("trades", 0),
            "portfolio_win_rate": taken_stats.get("win_rate", np.nan),
            **{f"portfolio_{key}": value for key, value in portfolio_stats.items()},
            "random_pick_trials": len(trial_returns),
            "random_pick_return_p10": float(np.percentile(trial_returns, 10)) if trial_returns else np.nan,
            "random_pick_return_median": float(np.median(trial_returns)) if trial_returns else np.nan,
            "random_pick_return_p90": float(np.percentile(trial_returns, 90)) if trial_returns else np.nan,
            "random_pick_max_drawdown_median": float(np.median(trial_drawdowns)) if trial_drawdowns else np.nan,
        })
        all_trades[variant["key"]] = trades
        equity_curves[variant["key"]] = equity
        print(f"[info] {variant['key']}: signals={stats.get('trades', 0)} "
              f"portfolio_return={portfolio_stats.get('total_return', float('nan')):.2%}")

    benchmark_stats = performance(benchmark_equity)
    summary = pd.DataFrame(summary_rows)

    output_path = Path(output)
    output_path.mkdir(parents=True, exist_ok=True)
    summary.to_csv(output_path / "variant_summary.csv", index=False)
    triggers.to_csv(output_path / "sector_triggers.csv", index=False)
    main_trades = all_trades["full"].drop(columns=["entry_index", "exit_index"])
    main_trades.to_csv(output_path / "trades_full.csv", index=False)
    curves = pd.DataFrame({key: value for key, value in equity_curves.items() if not value.empty})
    curves["benchmark"] = benchmark_equity
    curves.to_csv(output_path / "equity_curves.csv")

    yearly_rows = []
    full_trades = all_trades["full"]
    if not full_trades.empty:
        full_trades = full_trades.assign(year=pd.to_datetime(full_trades["buy_date"]).dt.year)
        for year, group in full_trades.groupby("year"):
            yearly_rows.append({
                "year": int(year),
                "trades": int(len(group)),
                "win_rate": float((group["net_return"] > 0).mean()),
                "median_return": float(group["net_return"].median()),
                "mean_return": float(group["net_return"].mean()),
            })
    pd.DataFrame(yearly_rows).to_csv(output_path / "yearly_trades.csv", index=False)

    sector_rows = []
    if not full_trades.empty:
        for (code, name), group in full_trades.groupby(["index_code", "index_name"]):
            sector_rows.append({
                "index_code": code,
                "index_name": name,
                "trades": int(len(group)),
                "win_rate": float((group["net_return"] > 0).mean()),
                "median_return": float(group["net_return"].median()),
                "mean_return": float(group["net_return"].mean()),
            })
    pd.DataFrame(sector_rows).sort_values("trades", ascending=False).to_csv(
        output_path / "sector_trades.csv", index=False)

    metadata = {
        "start": str(start),
        "end": str(end),
        "indexes": len(codes),
        "fear_threshold": FEAR_THRESHOLD,
        "arm_window": ARM_WINDOW,
        "sell_atr_threshold": SELL_ATR_THRESHOLD,
        "max_positions": max_positions,
        "commission_rate": COMMISSION_RATE,
        "stamp_duty_rate": STAMP_DUTY_RATE,
        "sector_triggers": int(len(triggers)),
        "sector_triggers_fear_pass": int(len(gated)),
        "candidate_stocks": len(candidate_codes),
        "benchmark": {"code": BENCHMARK_INDEX, "label": BENCHMARK_LABEL, **benchmark_stats},
    }
    (output_path / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(metadata, ensure_ascii=False, indent=2))
    print(summary.to_string(index=False))
    return {"summary": summary, "metadata": metadata, "trades": all_trades, "equity": curves}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analytics-db", default=DEFAULT_ANALYTICS_DB)
    parser.add_argument("--sqlite-db", default=DEFAULT_SQLITE_DB)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--start", default=str(DEFAULT_START))
    parser.add_argument("--end", default=None)
    parser.add_argument("--max-positions", type=int, default=MAX_POSITIONS)
    parser.add_argument("--index", action="append", default=None)
    args = parser.parse_args()
    analyze(
        analytics_db=args.analytics_db,
        sqlite_db=args.sqlite_db,
        output=args.output,
        start=pd.Timestamp(args.start).date(),
        end=pd.Timestamp(args.end).date() if args.end else None,
        index_codes=args.index,
        max_positions=args.max_positions,
    )


if __name__ == "__main__":
    main()

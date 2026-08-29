"""缠论一二三买 × 一二三卖事件配对回测。

回测定义：信号在 K 线收盘后确认，下一根 K 线开盘成交；每只股票同一时间只持有一笔。
结果是跨股票的事件收益统计，不假设组合资金容量或并发持仓上限。
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd
from czsc import BarGenerator


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from src.core.services.chan_analysis import analyze_bars, rows_to_raw_bars  # noqa: E402


# 沪深300、中证500、中证1000；中证2000需使用932000.CSI的独立权重数据
INDEX_CODES = ("000300.SH", "000905.SH", "000852.SH")
CSI2000_CODE = "932000.CSI"
BUY_TYPES = ("一买", "二买", "三买")
SELL_TYPES = ("一卖", "二卖", "三卖")
FREQUENCIES = ("1m", "5m", "30m", "d")


@dataclass(frozen=True)
class BacktestConfig:
    database: str
    start_date: date
    end_date: date
    min_avg_amount: float
    liquidity_days: int
    buy_cost_bps: float
    sell_cost_bps: float
    supplemental_weights: str | None
    include_csi2000: bool = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, help="analytics.duckdb 路径")
    parser.add_argument("--start-date", default="2020-01-01")
    parser.add_argument("--end-date", default=date.today().isoformat())
    parser.add_argument("--freqs", nargs="+", choices=FREQUENCIES, default=list(FREQUENCIES))
    parser.add_argument("--min-avg-amount", type=float, default=100_000.0,
                        help="近N日平均成交额门槛，单位为千元；默认1亿元")
    parser.add_argument("--liquidity-days", type=int, default=20)
    parser.add_argument("--buy-cost-bps", type=float, default=15.0)
    parser.add_argument("--sell-cost-bps", type=float, default=25.0)
    parser.add_argument("--min-sell-trading-days", type=int, default=0,
                        help="买入后至少经过多少个交易日才允许卖点退出；0为原始口径")
    parser.add_argument("--supplemental-weights", help="补充指数权重CSV，列同 a_stock_index_weight")
    parser.add_argument("--include-csi2000", action="store_true", help="纳入正确代码932000.CSI；需数据库或补充CSV提供历史成分")
    parser.add_argument("--output-dir", default="lab/output/chan_signal_pair_backtest")
    parser.add_argument("--symbol-limit", type=int, help="仅供快速冒烟测试")
    return parser.parse_args()


def _load_weights(connection: duckdb.DuckDBPyConnection, config: BacktestConfig) -> pd.DataFrame:
    index_codes = INDEX_CODES + ((CSI2000_CODE,) if config.include_csi2000 else ())
    placeholders = ", ".join("?" for _ in index_codes)
    weights = connection.execute(
        f"""
        SELECT index_code, con_code, trade_date, weight
        FROM a_stock_index_weight
        WHERE index_code IN ({placeholders})
          AND trade_date <= ?
        """,
        [*index_codes, config.end_date],
    ).fetchdf()
    if config.supplemental_weights:
        extra = pd.read_csv(config.supplemental_weights)
        required = {"index_code", "con_code", "trade_date"}
        if not required.issubset(extra.columns):
            raise ValueError(f"补充权重文件缺少列: {sorted(required - set(extra.columns))}")
        if "weight" not in extra:
            extra["weight"] = np.nan
        weights = pd.concat([weights, extra[list(required) + ["weight"]]], ignore_index=True)
    weights["index_code"] = weights["index_code"].astype(str).str.upper()
    weights["con_code"] = weights["con_code"].astype(str).str.upper()
    weights["trade_date"] = pd.to_datetime(weights["trade_date"]).dt.date
    weights = weights[weights["index_code"].isin(index_codes)]
    weights = weights.drop_duplicates(["index_code", "con_code", "trade_date"], keep="last")
    missing = sorted(set(index_codes) - set(weights["index_code"]))
    if missing:
        raise ValueError(f"指数成分数据缺失: {missing}；请用 --supplemental-weights 补充")
    return weights.sort_values(["trade_date", "index_code", "con_code"])


def _membership_intervals(weights: pd.DataFrame) -> dict[str, list[tuple[date, date]]]:
    """把离散调仓快照转成每只股票的并集有效区间。"""
    intervals: dict[str, list[tuple[date, date]]] = defaultdict(list)
    # 各指数快照日期不一定一致，必须分别展开；股票属于任一指数即入池。
    for _, index_weights in weights.groupby("index_code"):
        snapshots = sorted(index_weights["trade_date"].unique())
        by_date = {
            d: set(index_weights.loc[index_weights["trade_date"] == d, "con_code"])
            for d in snapshots
        }
        active_start: dict[str, date] = {}
        previous: set[str] = set()
        for snapshot in snapshots:
            current = by_date[snapshot]
            for symbol in current - previous:
                active_start[symbol] = snapshot
            for symbol in previous - current:
                intervals[symbol].append((active_start.pop(symbol), snapshot))
            previous = current
        for symbol in previous:
            intervals[symbol].append((active_start[symbol], date.max))
    return dict(intervals)


def _load_daily_filters(
    connection: duckdb.DuckDBPyConnection,
    symbols: list[str],
    config: BacktestConfig,
) -> pd.DataFrame:
    connection.register("chan_bt_symbols", pd.DataFrame({"ts_code": symbols}))
    frame = connection.execute(
        f"""
        WITH base AS (
            SELECT d.ts_code, d.trade_date, d.amount,
                   AVG(d.amount) OVER (
                       PARTITION BY d.ts_code ORDER BY d.trade_date
                       ROWS BETWEEN {config.liquidity_days} PRECEDING AND 1 PRECEDING
                   ) AS avg_amount,
                   COUNT(d.amount) OVER (
                       PARTITION BY d.ts_code ORDER BY d.trade_date
                       ROWS BETWEEN {config.liquidity_days} PRECEDING AND 1 PRECEDING
                   ) AS liquidity_observations
            FROM a_stock_market_daily d
            JOIN chan_bt_symbols s USING (ts_code)
            WHERE d.trade_date BETWEEN ? - INTERVAL {config.liquidity_days * 2 + 10} DAY AND ?
        )
        SELECT b.ts_code, b.trade_date, b.avg_amount, b.liquidity_observations,
               NOT EXISTS (
                   SELECT 1 FROM a_stock_name_changes n
                   WHERE n.ts_code = b.ts_code
                     AND n.start_date <= b.trade_date
                     AND COALESCE(n.end_date, DATE '9999-12-31') >= b.trade_date
                     AND UPPER(COALESCE(n.name, '')) LIKE '%ST%'
               ) AS not_st
        FROM base b
        WHERE b.trade_date BETWEEN ? AND ?
        """,
        [config.start_date, config.end_date, config.start_date, config.end_date],
    ).fetchdf()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
    return frame


def _load_bars(
    connection: duckdb.DuckDBPyConnection,
    symbol: str,
    freq: str,
    config: BacktestConfig,
) -> pd.DataFrame:
    if freq == "d":
        frame = connection.execute(
            """
            SELECT CAST(trade_date AS TIMESTAMP) + INTERVAL 15 HOUR AS timestamp,
                   open, high, low, close, volume, turnover
            FROM a_stock_market_daily_qfq
            WHERE ts_code = ? AND trade_date BETWEEN ? AND ?
            ORDER BY trade_date
            """,
            [symbol, config.start_date, config.end_date],
        ).fetchdf()
    else:
        frame = connection.execute(
            """
            SELECT trade_time AS timestamp, open, high, low, close, vol AS volume, amount AS turnover
            FROM a_stock_minute_bar_qfq
            WHERE ts_code = ? AND CAST(trade_time AS DATE) BETWEEN ? AND ?
            ORDER BY trade_time
            """,
            [symbol, config.start_date, config.end_date],
        ).fetchdf()
        if not frame.empty and freq != "1m":
            target = {"5m": "5分钟", "30m": "30分钟"}[freq]
            raw_bars = rows_to_raw_bars(symbol, frame.to_dict("records"), "1m")
            generator = BarGenerator(
                base_freq="1分钟", freqs=[target], max_count=max(2000, len(raw_bars)), market="A股"
            )
            for bar in raw_bars:
                generator.update(bar)
            frame = pd.DataFrame([
                {"timestamp": bar.dt, "open": bar.open, "high": bar.high, "low": bar.low,
                 "close": bar.close, "volume": bar.vol, "turnover": bar.amount}
                for bar in generator.bars[target]
            ])
    return frame.dropna(subset=["timestamp", "open", "high", "low", "close"]).reset_index(drop=True)


def _in_intervals(value: date, intervals: list[tuple[date, date]]) -> bool:
    return any(start <= value < end for start, end in intervals)


def _extract_trades(
    symbol: str,
    freq: str,
    bars: pd.DataFrame,
    membership: list[tuple[date, date]],
    eligibility: dict[date, bool],
    config: BacktestConfig,
    min_sell_trading_days: int = 0,
) -> list[dict[str, Any]]:
    if len(bars) < 20:
        return []
    rows = bars.to_dict("records")
    analysis = analyze_bars(symbol, rows, freq, confirmed=True, include_history=True)
    by_time: dict[pd.Timestamp, set[str]] = defaultdict(set)
    for signal in analysis["signal_history"]:
        by_time[pd.Timestamp(signal["bar_time"])].add(signal["type"])
    times = pd.DatetimeIndex(pd.to_datetime(bars["timestamp"]))
    bar_dates = np.asarray(times.date)
    trading_day_order = {d: i for i, d in enumerate(dict.fromkeys(bar_dates))}
    index_by_time = {pd.Timestamp(ts): i for i, ts in enumerate(times)}
    completed: list[dict[str, Any]] = []
    for buy_type, sell_type in itertools.product(BUY_TYPES, SELL_TYPES):
        entry: tuple[int, pd.Timestamp, float] | None = None
        for signal_time in sorted(by_time):
            bar_index = index_by_time.get(signal_time)
            if bar_index is None or bar_index + 1 >= len(bars):
                continue
            execution_index = bar_index + 1
            execution_time = times[execution_index]
            signal_date = signal_time.date()
            active = _in_intervals(signal_date, membership) and eligibility.get(signal_date, False)
            signal_types = by_time[signal_time]
            if entry is None:
                if buy_type in signal_types and active:
                    entry = (execution_index, execution_time, float(bars.iloc[execution_index]["open"]))
                continue
            if sell_type not in signal_types:
                continue
            if trading_day_order.get(execution_time.date(), 0) - trading_day_order.get(entry[1].date(), 0) < min_sell_trading_days:
                continue
            entry_index, entry_time, entry_price = entry
            exit_price = float(bars.iloc[execution_index]["open"])
            gross_return = exit_price / entry_price - 1.0
            net_return = (
                exit_price * (1.0 - config.sell_cost_bps / 10_000.0)
                / (entry_price * (1.0 + config.buy_cost_bps / 10_000.0)) - 1.0
            )
            completed.append({
                "freq": freq, "buy_type": buy_type, "sell_type": sell_type, "symbol": symbol,
                "entry_time": entry_time, "exit_time": execution_time, "entry_price": entry_price,
                "exit_price": exit_price, "gross_return": gross_return, "net_return": net_return,
                "holding_bars": execution_index - entry_index,
                "holding_days": (execution_time.date() - entry_time.date()).days,
                "exit_reason": "signal",
            })
            entry = None
        # 期末持仓必须计价；丢弃未闭环交易会系统性高估长持有组合。
        if entry is not None:
            entry_index, entry_time, entry_price = entry
            execution_index = len(bars) - 1
            execution_time = times[execution_index]
            exit_price = float(bars.iloc[execution_index]["close"])
            completed.append({
                "freq": freq, "buy_type": buy_type, "sell_type": sell_type, "symbol": symbol,
                "entry_time": entry_time, "exit_time": execution_time, "entry_price": entry_price,
                "exit_price": exit_price, "gross_return": exit_price / entry_price - 1.0,
                "net_return": (
                    exit_price * (1.0 - config.sell_cost_bps / 10_000.0)
                    / (entry_price * (1.0 + config.buy_cost_bps / 10_000.0)) - 1.0
                ),
                "holding_bars": execution_index - entry_index,
                "holding_days": (execution_time.date() - entry_time.date()).days,
                "exit_reason": "end_of_data",
            })
    return completed


def _summarize(trades: pd.DataFrame, freq: str, buy_type: str, sell_type: str) -> dict[str, Any]:
    subset = trades[(trades["freq"] == freq) & (trades["buy_type"] == buy_type) & (trades["sell_type"] == sell_type)]
    returns = subset["net_return"].astype(float)
    wins = returns[returns > 0]
    losses = returns[returns < 0]
    profit_factor = wins.sum() / abs(losses.sum()) if len(losses) and losses.sum() else math.inf
    return {
        "freq": freq, "buy_type": buy_type, "sell_type": sell_type,
        "trade_count": int(len(subset)), "symbol_count": int(subset["symbol"].nunique()),
        "win_rate": float((returns > 0).mean()) if len(returns) else np.nan,
        "avg_net_return": float(returns.mean()) if len(returns) else np.nan,
        "median_net_return": float(returns.median()) if len(returns) else np.nan,
        "total_net_pnl_per_unit": float(returns.sum()) if len(returns) else np.nan,
        "profit_factor": float(profit_factor),
        "p05_return": float(returns.quantile(0.05)) if len(returns) else np.nan,
        "p95_return": float(returns.quantile(0.95)) if len(returns) else np.nan,
        "avg_holding_bars": float(subset["holding_bars"].mean()) if len(subset) else np.nan,
        "avg_holding_days": float(subset["holding_days"].mean()) if len(subset) else np.nan,
    }


def main() -> None:
    args = parse_args()
    config = BacktestConfig(
        database=args.database,
        start_date=date.fromisoformat(args.start_date),
        end_date=date.fromisoformat(args.end_date),
        min_avg_amount=args.min_avg_amount,
        liquidity_days=max(2, args.liquidity_days),
        buy_cost_bps=args.buy_cost_bps,
        sell_cost_bps=args.sell_cost_bps,
        supplemental_weights=args.supplemental_weights,
        include_csi2000=args.include_csi2000,
    )
    output_dir = REPO_ROOT / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(config.database, read_only=True)
    try:
        weights = _load_weights(connection, config)
        membership = _membership_intervals(weights)
        symbols = sorted(membership)
        if args.symbol_limit:
            symbols = symbols[: args.symbol_limit]
        daily_filters = _load_daily_filters(connection, symbols, config)
        daily_filters["eligible"] = (
            daily_filters["not_st"].fillna(False)
            & (daily_filters["liquidity_observations"] >= config.liquidity_days)
            & (daily_filters["avg_amount"] >= config.min_avg_amount)
        )
        eligibility_by_symbol = {
            symbol: dict(zip(group["trade_date"], group["eligible"], strict=False))
            for symbol, group in daily_filters.groupby("ts_code")
        }
        trades: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for freq in args.freqs:
            for index, symbol in enumerate(symbols, start=1):
                try:
                    bars = _load_bars(connection, symbol, freq, config)
                    trades.extend(_extract_trades(
                        symbol, freq, bars, membership[symbol], eligibility_by_symbol.get(symbol, {}), config,
                        args.min_sell_trading_days,
                    ))
                except Exception as exc:  # keep a complete audit trail for large cross-sectional runs
                    errors.append({"freq": freq, "symbol": symbol, "error": repr(exc)})
                if index % 100 == 0 or index == len(symbols):
                    print(f"{freq}: {index}/{len(symbols)} symbols, {len(trades)} trades, {len(errors)} errors", flush=True)
    finally:
        connection.close()

    trade_columns = [
        "freq", "buy_type", "sell_type", "symbol", "entry_time", "exit_time", "entry_price",
        "exit_price", "gross_return", "net_return", "holding_bars", "holding_days", "exit_reason",
    ]
    trade_frame = pd.DataFrame(trades, columns=trade_columns)
    summaries = [
        _summarize(trade_frame, freq, buy, sell)
        for freq, buy, sell in itertools.product(args.freqs, BUY_TYPES, SELL_TYPES)
    ]
    summary_frame = pd.DataFrame(summaries)
    summary_frame["rank_in_freq"] = summary_frame.groupby("freq")["avg_net_return"].rank(
        ascending=False, method="min"
    )
    trade_frame.to_csv(output_dir / "trades.csv", index=False)
    summary_frame.sort_values(["freq", "rank_in_freq"]).to_csv(output_dir / "summary.csv", index=False)
    pd.DataFrame(errors).to_csv(output_dir / "errors.csv", index=False)
    metadata = {
        "generated_at": datetime.now().isoformat(), "config": asdict(config), "frequencies": args.freqs,
        "index_codes": INDEX_CODES, "candidate_symbol_count": len(symbols),
        "weight_snapshot_counts": weights.groupby("index_code")["trade_date"].nunique().to_dict(),
        "trade_count": len(trade_frame), "error_count": len(errors),
        "method": "bar-close signal; next-bar-open execution; non-overlapping trades per symbol and signal pair",
        "min_sell_trading_days": args.min_sell_trading_days,
    }
    metadata["config"]["start_date"] = config.start_date.isoformat()
    metadata["config"]["end_date"] = config.end_date.isoformat()
    (output_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(summary_frame.sort_values(["freq", "rank_in_freq"]).to_string(index=False))


if __name__ == "__main__":
    main()

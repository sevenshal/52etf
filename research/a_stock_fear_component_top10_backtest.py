#!/usr/bin/env python3
"""Replay the production fear/volume rotation with liquid constituent baskets.

The production state machine remains the source of signal and execution dates.
For each A-share ETF BUY, this experiment replaces the ETF with an equal-weight
basket of the ten highest-turnover point-in-time index constituents:

* constituent membership = latest stored index-weight snapshot on/before signal day;
* liquidity rank = signal-day A-share turnover (``amount``), known after close;
* execution = next-session pessimistic price (BUY high / SELL low), matching -1 slippage;
* basket membership is frozen until the production state machine exits or rotates;
* 159509.SZ remains an ETF because its Nasdaq constituents are not A-share tradable.

The script is read-only with respect to production databases.  It writes reviewed
CSV/JSON evidence beneath ``research/output/a_stock_fear_component_top10_backtest``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Reuse the existing read-only import guards around the production backtest code.
from lab import seesaw_nine_turn_backtest as guarded  # noqa: E402

fb = guarded.fb

INITIAL_CAPITAL = 1_000_000.0
LOT_SIZE = 100
TOP_N = 10
COMMISSION_RATE = 0.00025
MIN_COMMISSION = 5.0
TRANSFER_FEE_RATE = 0.00001

ETF_TO_INDEX = {
    "510880.SH": "000015.SH",   # 上证红利
    "512480.SH": "H30184.CSI",  # 中证全指半导体（ETF 实际跟踪方向）
    "516510.SH": "930851.CSI",  # 中证云计算与大数据主题
}
DIRECT_ETFS = {"159509.SZ"}


def production_params() -> Any:
    """Parameters copied from the user's 2026-10-07 best result."""
    return fb.SOXLFearStrategyParams(
        buy_threshold=35,
        greed_threshold=70,
        volume_ratio_threshold=1.6,
        volume_ratio_consecutive_days=1,
        volume_z_threshold=None,
        sell_shrink_z=-1,
        buy_position_pct=100,
        cooldown_days=0,
        trailing_stop_pct=0,
        sell_position_pct=100,
        sell_reduction_basis="holdings",
        sell_price_above_avg_cost=False,
        max_take_profit_sells_per_cycle=2,
        min_position_pct_after_take_profit=0,
        rebalance_threshold_pct=0,
        execute_next_open=True,
        slippage_pct=-1,
        stamp_duty_pct=0,
        buy_turn_signal_mode="legacy",
        sell_turn_signal_mode="legacy",
        sub_symbol="512480.SH",
        sub_fear_source="a_stock_000688_sh",
        sub_volume_signal_symbol="588000.SH",
        sub_buy_threshold=25,
        sub_volume_ratio_threshold=1.6,
        sub2_symbol="159509.SZ",
        sub2_fear_source="qqq_clone",
        sub2_volume_signal_symbol="QQQ.US",
        sub2_buy_threshold=25,
        sub2_volume_ratio_threshold=1.4,
        sub3_symbol="516510.SH",
        sub3_fear_source="a_stock_930851_csi",
        sub3_volume_signal_symbol=None,
        sub3_buy_threshold=20,
        sub3_volume_ratio_threshold=1.3,
        sell_ma5_confirm="non_main",
        swap_threshold=45,
        valuation_window=252,
        valuation_buy_max=None,
        valuation_sell_min=80,
        valuation_force_sell_greed=90,
    )


def install_local_market_calendars(analytics_db: str, start: date, end: date) -> None:
    """Keep valuation-date resolution offline and tied to stored market sessions."""
    from backend.src.core.services import index_valuation

    connection = duckdb.connect(analytics_db, read_only=True)
    try:
        china_days = {
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT trade_date FROM a_stock_index_daily "
                "WHERE trade_date BETWEEN ? AND ?",
                [(start - pd.Timedelta(days=10)).isoformat(), end.isoformat()],
            ).fetchall()
        }
        us_days = {
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT trade_date FROM us_stock_daily "
                "WHERE trade_date BETWEEN ? AND ?",
                [(start - pd.Timedelta(days=10)).isoformat(), end.isoformat()],
            ).fetchall()
        }
    finally:
        connection.close()
    index_valuation._is_china_trading_day = lambda check_date: check_date in china_days
    index_valuation._is_us_trading_day = lambda check_date: check_date in us_days


def run_production_baseline(
    start: date, end: date, analytics_db: str
) -> tuple[dict, dict[str, pd.DataFrame]]:
    install_local_market_calendars(analytics_db, start, end)
    params = production_params()
    main, _ = fb._prepare_base_dataframe(
        "510880.SH", start, end, "a_stock_000015_sh", None
    )
    sub, _ = fb._prepare_base_dataframe(
        "512480.SH", start, end, "a_stock_000688_sh", "588000.SH"
    )
    sub2, _ = fb._prepare_base_dataframe(
        "159509.SZ", start, end, "qqq_clone", "QQQ.US"
    )
    sub3, _ = fb._prepare_base_dataframe(
        "516510.SH", start, end, "a_stock_930851_csi", None
    )
    result = fb._run_seesaw_backtest(
        main,
        sub,
        params,
        INITIAL_CAPITAL,
        detailed=True,
        sub2_base_df=sub2,
        sub3_base_df=sub3,
    )
    return result, {
        "510880.SH": main,
        "512480.SH": sub,
        "159509.SZ": sub2,
        "516510.SH": sub3,
    }


def _placeholders(values: list[str]) -> str:
    return ",".join("?" for _ in values)


def select_baskets(
    connection: duckdb.DuckDBPyConnection,
    trades: list[dict],
    trading_dates: list[str],
) -> tuple[dict[int, list[str]], pd.DataFrame, list[dict]]:
    """Select each BUY basket using only data available on its signal day."""
    baskets: dict[int, list[str]] = {}
    selection_rows: list[dict] = []
    quality_rows: list[dict] = []

    previous_session = {
        trading_dates[index]: trading_dates[index - 1]
        for index in range(1, len(trading_dates))
    }
    for trade_index, trade in enumerate(trades):
        if trade.get("action") != "BUY":
            continue
        etf = str(trade.get("symbol") or "")
        if etf in DIRECT_ETFS:
            baskets[trade_index] = [etf]
            continue
        index_code = ETF_TO_INDEX.get(etf)
        if not index_code:
            raise ValueError(f"No constituent mapping for BUY symbol {etf}")

        execution_day = str(trade.get("date"))[:10]
        # The seesaw engine shifts signals by one row for next-open execution,
        # but its detailed trade payload labels the execution day as signal_date.
        # Recover the actual information cutoff explicitly from the session list.
        signal_day = previous_session[execution_day]
        snapshot = connection.execute(
            "SELECT MAX(trade_date) FROM a_stock_index_weight "
            "WHERE index_code = ? AND trade_date <= ?",
            [index_code, signal_day],
        ).fetchone()[0]
        if snapshot is None:
            raise ValueError(f"No point-in-time constituents for {index_code} on {signal_day}")
        members = [
            row[0]
            for row in connection.execute(
                "SELECT con_code FROM a_stock_index_weight "
                "WHERE index_code = ? AND trade_date = ?",
                [index_code, snapshot],
            ).fetchall()
        ]
        ranked = connection.execute(
            f"""
            SELECT ts_code, amount, high, low, close, limit_status
            FROM a_stock_market_daily_qfq
            WHERE trade_date = ? AND ts_code IN ({_placeholders(members)})
              AND amount IS NOT NULL AND amount > 0
            ORDER BY amount DESC, ts_code
            """,
            [signal_day, *members],
        ).fetchdf()
        selected = ranked.head(TOP_N).copy()
        baskets[trade_index] = selected["ts_code"].astype(str).tolist()
        quality_rows.append(
            {
                "trade_index": trade_index,
                "etf_symbol": etf,
                "index_code": index_code,
                "signal_date": signal_day,
                "execution_date": execution_day,
                "snapshot_date": str(snapshot)[:10],
                "snapshot_lag_days": (pd.Timestamp(signal_day) - pd.Timestamp(snapshot)).days,
                "constituent_count": len(members),
                "rankable_count": len(ranked),
                "selected_count": len(selected),
            }
        )
        for rank, row in enumerate(selected.itertuples(index=False), start=1):
            selection_rows.append(
                {
                    "trade_index": trade_index,
                    "etf_symbol": etf,
                    "index_code": index_code,
                    "signal_date": signal_day,
                    "execution_date": execution_day,
                    "snapshot_date": str(snapshot)[:10],
                    "rank": rank,
                    "stock_symbol": str(row.ts_code),
                    "signal_amount_thousand_yuan": float(row.amount),
                    "signal_limit_status": int(row.limit_status or 0),
                }
            )
    return baskets, pd.DataFrame(selection_rows), quality_rows


def load_stock_bars(
    connection: duckdb.DuckDBPyConnection,
    symbols: set[str],
    start: date,
    end: date,
) -> pd.DataFrame:
    symbol_list = sorted(symbols)
    if not symbol_list:
        return pd.DataFrame()
    frame = connection.execute(
        f"""
        SELECT trade_date, ts_code, open, high, low, close, pre_close, amount, limit_status
        FROM a_stock_market_daily_qfq
        WHERE ts_code IN ({_placeholders(symbol_list)})
          AND trade_date BETWEEN ? AND ?
        ORDER BY trade_date, ts_code
        """,
        [*symbol_list, start.isoformat(), end.isoformat()],
    ).fetchdf()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.strftime("%Y-%m-%d")
    return frame


def _fee(notional: float, enabled: bool) -> float:
    if not enabled or notional <= 0:
        return 0.0
    return max(MIN_COMMISSION, notional * COMMISSION_RATE) + notional * TRANSFER_FEE_RATE


def _stamp_rate(day: str, is_stock: bool, enabled: bool) -> float:
    if not enabled or not is_stock:
        return 0.0
    # A-share stamp duty was halved from 0.10% to 0.05% on 2023-08-28.
    return 0.0005 if day >= "2023-08-28" else 0.001


def simulate_component_portfolio(
    baseline: dict,
    etf_frames: dict[str, pd.DataFrame],
    baskets: dict[int, list[str]],
    stock_bars: pd.DataFrame,
    with_stock_costs: bool,
) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    dates = [str(row["date"])[:10] for row in baseline["equity_curve"]]
    actions: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    for trade_index, trade in enumerate(baseline["trades"]):
        actions[str(trade["date"])[:10]].append((trade_index, trade))

    stock_lookup = {
        (str(row.trade_date), str(row.ts_code)): row._asdict()
        for row in stock_bars.itertuples(index=False)
    }
    etf_lookup: dict[tuple[str, str], dict] = {}
    for symbol, frame in etf_frames.items():
        for row in frame.to_dict("records"):
            etf_lookup[(str(row["date"])[:10], symbol)] = row

    cash = INITIAL_CAPITAL
    positions: dict[str, dict] = {}
    last_marks: dict[str, float] = {}
    curve_rows: list[dict] = []
    execution_rows: list[dict] = []
    missing_execution_prices: list[dict] = []

    def bar(day: str, symbol: str) -> dict | None:
        if symbol in DIRECT_ETFS:
            return etf_lookup.get((day, symbol))
        return stock_lookup.get((day, symbol))

    for day in dates:
        day_actions = actions.get(day, [])
        for _, trade in day_actions:
            if trade.get("action") != "SELL":
                continue
            for symbol, position in list(positions.items()):
                info = bar(day, symbol)
                if info is None or not float(info.get("low") or 0) > 0:
                    missing_execution_prices.append({"date": day, "symbol": symbol, "side": "SELL"})
                    continue
                price = float(info["low"])
                notional = position["shares"] * price
                commission = _fee(notional, with_stock_costs)
                stamp = notional * _stamp_rate(day, symbol not in DIRECT_ETFS, with_stock_costs)
                cash += notional - commission - stamp
                execution_rows.append(
                    {
                        "date": day,
                        "side": "SELL",
                        "signal_etf": trade.get("symbol"),
                        "symbol": symbol,
                        "shares": position["shares"],
                        "price": price,
                        "notional": notional,
                        "commission": commission,
                        "stamp_duty": stamp,
                    }
                )
                del positions[symbol]

        for trade_index, trade in day_actions:
            if trade.get("action") != "BUY":
                continue
            selected = baskets[trade_index]
            usable = [symbol for symbol in selected if bar(day, symbol) is not None]
            if len(usable) != len(selected):
                missing = sorted(set(selected) - set(usable))
                missing_execution_prices.extend(
                    {"date": day, "symbol": symbol, "side": "BUY"} for symbol in missing
                )
            if not usable:
                continue
            budget_each = cash / len(usable)
            for symbol in usable:
                info = bar(day, symbol)
                price = float(info.get("high") or 0)
                if price <= 0:
                    missing_execution_prices.append({"date": day, "symbol": symbol, "side": "BUY"})
                    continue
                shares = math.floor(budget_each / price / LOT_SIZE) * LOT_SIZE
                while shares > 0:
                    notional = shares * price
                    commission = _fee(notional, with_stock_costs)
                    if notional + commission <= min(cash, budget_each) + 1e-8:
                        break
                    shares -= LOT_SIZE
                if shares <= 0:
                    continue
                notional = shares * price
                commission = _fee(notional, with_stock_costs)
                cash -= notional + commission
                positions[symbol] = {
                    "shares": shares,
                    "cost": notional + commission,
                    "source_etf": trade.get("symbol"),
                }
                execution_rows.append(
                    {
                        "date": day,
                        "side": "BUY",
                        "signal_etf": trade.get("symbol"),
                        "symbol": symbol,
                        "shares": shares,
                        "price": price,
                        "notional": notional,
                        "commission": commission,
                        "stamp_duty": 0.0,
                    }
                )

        market_value = 0.0
        for symbol, position in positions.items():
            info = bar(day, symbol)
            if info is not None and float(info.get("close") or 0) > 0:
                last_marks[symbol] = float(info["close"])
            mark = last_marks.get(symbol)
            if mark is None:
                raise ValueError(f"No close mark for held symbol {symbol} on {day}")
            market_value += position["shares"] * mark
        curve_rows.append(
            {
                "date": day,
                "value": cash + market_value,
                "cash": cash,
                "market_value": market_value,
                "position_count": len(positions),
            }
        )

    curve = pd.DataFrame(curve_rows)
    metrics, drawdowns = fb._compute_equity_metrics(
        curve["date"].tolist(), curve["value"].to_numpy(dtype=float)
    )
    curve["drawdown_pct"] = drawdowns * 100
    executions = pd.DataFrame(execution_rows)
    metrics.update(
        {
            "total_fees": float(
                executions[["commission", "stamp_duty"]].sum().sum()
            ) if not executions.empty else 0.0,
            "stock_trade_count": int(len(executions)),
            "missing_execution_price_count": len(missing_execution_prices),
            "missing_execution_prices": missing_execution_prices,
        }
    )
    return metrics, curve, executions


def compact_metrics(result: dict) -> dict:
    keys = [
        "total_return",
        "annualized_return",
        "annualized_volatility",
        "max_drawdown",
        "max_drawdown_duration_days",
        "sharpe_ratio",
        "sortino_ratio",
        "calmar_ratio",
        "win_rate",
        "profit_loss_ratio",
        "ending_value",
    ]
    return {key: result.get(key) for key in keys}


def build_cycle_comparison(baseline_trades: list[dict], executions: pd.DataFrame) -> pd.DataFrame:
    """Compare completed holding-period returns before compounding."""
    trades = pd.DataFrame(baseline_trades).reset_index(drop=True)
    rows: list[dict] = []
    for trade_index, buy in trades.loc[trades["action"] == "BUY"].iterrows():
        following_sells = trades.loc[(trades.index > trade_index) & (trades["action"] == "SELL")]
        if following_sells.empty:
            continue
        sell = following_sells.iloc[0]
        buy_date, sell_date = str(buy["date"])[:10], str(sell["date"])[:10]
        component_buys = executions.loc[
            (executions["date"] == buy_date) & (executions["side"] == "BUY")
        ]
        component_sells = executions.loc[
            (executions["date"] == sell_date) & (executions["side"] == "SELL")
        ]
        etf_return = float(sell["amount"] / buy["amount"] - 1.0)
        component_return = float(
            component_sells["notional"].sum() / component_buys["notional"].sum() - 1.0
        )
        rows.append(
            {
                "buy_date": buy_date,
                "sell_date": sell_date,
                "etf_symbol": buy["symbol"],
                "holding_count": len(component_buys),
                "etf_return_pct": etf_return * 100,
                "component_return_pct": component_return * 100,
                "component_minus_etf_pct_points": (component_return - etf_return) * 100,
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2023-03-22")
    parser.add_argument("--end", default="2026-10-07")
    parser.add_argument(
        "--analytics-db",
        default=os.getenv("ANALYTICS_DB_PATH") or "/home/quantd/quant_prod/quant_robot/analytics.duckdb",
    )
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "research/output/a_stock_fear_component_top10_backtest"),
    )
    args = parser.parse_args()
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)

    baseline, etf_frames = run_production_baseline(start, end, args.analytics_db)
    connection = duckdb.connect(args.analytics_db, read_only=True)
    try:
        trading_dates = [str(row["date"])[:10] for row in baseline["equity_curve"]]
        baskets, selections, quality_rows = select_baskets(
            connection, baseline["trades"], trading_dates
        )
        stock_symbols = {
            symbol
            for symbols in baskets.values()
            for symbol in symbols
            if symbol not in DIRECT_ETFS
        }
        stock_bars = load_stock_bars(connection, stock_symbols, start, end)
    finally:
        connection.close()

    gross, gross_curve, gross_exec = simulate_component_portfolio(
        baseline, etf_frames, baskets, stock_bars, with_stock_costs=False
    )
    net, net_curve, net_exec = simulate_component_portfolio(
        baseline, etf_frames, baskets, stock_bars, with_stock_costs=True
    )
    cycle_comparison = build_cycle_comparison(baseline["trades"], gross_exec)
    baseline_metrics = compact_metrics(baseline)
    baseline_metrics.update(
        {
            "trade_count": int(baseline.get("trade_count") or 0),
            "buy_count": int(baseline.get("buy_count") or 0),
            "sell_count": int(baseline.get("sell_count") or 0),
            "idle_days": int(baseline.get("idle_days") or 0),
        }
    )
    comparison = pd.DataFrame(
        [
            {"variant": "original_etf", **baseline_metrics, "total_fees": 0.0},
            {"variant": "component_top10_gross", **compact_metrics(gross), "total_fees": gross["total_fees"]},
            {"variant": "component_top10_net", **compact_metrics(net), "total_fees": net["total_fees"]},
        ]
    )

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    comparison.to_csv(output_dir / "comparison.csv", index=False)
    selections.to_csv(output_dir / "selections.csv", index=False)
    pd.DataFrame(baseline["trades"]).to_csv(output_dir / "baseline_trades.csv", index=False)
    cycle_comparison.to_csv(output_dir / "cycle_comparison.csv", index=False)
    pd.DataFrame(quality_rows).to_csv(output_dir / "data_quality.csv", index=False)
    gross_curve.to_csv(output_dir / "gross_equity_curve.csv", index=False)
    net_curve.to_csv(output_dir / "net_equity_curve.csv", index=False)
    gross_exec.to_csv(output_dir / "gross_executions.csv", index=False)
    net_exec.to_csv(output_dir / "net_executions.csv", index=False)
    artifact = {
        "method": {
            "start": args.start,
            "requested_end": args.end,
            "effective_end": str(baseline["equity_curve"][-1]["date"])[:10],
            "top_n": TOP_N,
            "selection": "latest index snapshot <= signal date; rank signal-date amount descending",
            "execution": "production signal schedule; next-session BUY high / SELL low; 100-share lots",
            "gross_costs": "none, isolates constituent-selection effect",
            "net_costs": "commission 2.5bp min CNY5 + transfer 0.1bp; stock sell stamp 10bp before 2023-08-28 else 5bp",
            "direct_etfs": sorted(DIRECT_ETFS),
            "etf_to_index": ETF_TO_INDEX,
        },
        "baseline": baseline_metrics,
        "component_top10_gross": gross,
        "component_top10_net": net,
        "completed_cycle_comparison": cycle_comparison.to_dict("records"),
        "quality": quality_rows,
    }
    (output_dir / "artifact.json").write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(comparison.to_string(index=False))
    print(f"\nSelections: {len(selections)} rows; output: {output_dir}")


if __name__ == "__main__":
    main()

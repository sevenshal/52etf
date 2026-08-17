#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import duckdb
import pandas as pd

from backtest_xueqiu_top_holdings_strategy import (
    DEFAULT_ANALYTICS_DB,
    DEFAULT_LIST_PATH,
    compute_metrics,
    effective_event_date,
    json_safe,
    load_json,
    load_price_data,
    ms_to_shanghai,
    normalize_weights,
    one_way_turnover,
    parse_date,
    raw_to_ts_code,
    safe_float,
    ts_to_raw_symbol,
    write_json,
)
from backtest_xueqiu_top_holdings_strategy_backward import (
    initial_holdings,
    inverse_drift,
    reverse_event,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT_DIR = ROOT / "lab" / "output" / "xueqiu_top_holdings_strategy_20251001_backward_top15"
DEFAULT_FULL_HISTORY_DIR = ROOT / "lab" / "output" / "xueqiu_top_holdings_strategy_20260101" / "rebalance_history_cache"
DEFAULT_OUTPUT_DIR = ROOT / "lab" / "output" / "xueqiu_top_holdings_strategy_20251001_user_active380_cashlike_bucket10"
CASH_SYMBOL = "CASH"
CASHLIKE_NAME_KEYWORDS = (
    "货币",
    "日利",
    "添益",
    "保证金",
    "快线",
    "短融",
    "国债",
    "政金债",
    "公司债",
    "可转债",
    "债ETF",
    "现金流",
)
RETAIN_WEIGHT_TOLERANCE_PCT = 1.0


def event_sort_value(event: Dict[str, Any]) -> Tuple[float, int]:
    return (safe_float(event.get("created_at"), 0.0) or 0.0, int(event.get("id") or 0))


def event_created_date(event: Dict[str, Any]) -> Optional[date]:
    created_at = ms_to_shanghai(event.get("created_at"))
    return created_at.date() if created_at else None


def user_rebalance_date(event: Dict[str, Any]) -> Optional[date]:
    if event.get("status") not in (None, "success"):
        return None
    if event.get("category") != "user_rebalancing":
        return None
    updated_at = ms_to_shanghai(event.get("updated_at") or event.get("created_at"))
    return updated_at.date() if updated_at else None


def is_cashlike_etf(symbol: str, name: str) -> bool:
    if symbol == CASH_SYMBOL:
        return True
    text = f"{symbol} {name}".upper()
    return any(keyword.upper() in text for keyword in CASHLIKE_NAME_KEYWORDS)


def load_cached_snapshots(
    cubes: List[Dict[str, Any]],
    *,
    snapshot_dir: Path,
    full_history_dir: Path,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    current_dir = snapshot_dir / "current_holdings_cache"
    recent_history_dir = snapshot_dir / "recent_rebalance_history_cache"
    snapshots: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []

    for cube in cubes:
        symbol = str(cube.get("symbol") or "").strip().upper()
        current_path = current_dir / f"{symbol}.json"
        recent_history_path = recent_history_dir / f"{symbol}.json"
        full_history_path = full_history_dir / f"{symbol}.json"
        if not current_path.exists():
            failures.append({"symbol": symbol, "error": "missing current holdings cache"})
            continue
        history_path = recent_history_path if recent_history_path.exists() else full_history_path
        if not history_path.exists():
            failures.append({"symbol": symbol, "error": "missing rebalance history cache"})
            continue
        snapshots.append(
            {
                "cube": cube,
                "current": load_json(current_path, {}),
                "history": load_json(history_path, {}),
            }
        )

    snapshots.sort(key=lambda item: int((item.get("cube") or {}).get("year_rank") or 999999))
    return snapshots, failures


def collect_symbols_and_names(snapshots: Iterable[Dict[str, Any]]) -> Tuple[List[str], Dict[str, str]]:
    symbols = set()
    names: Dict[str, str] = {}
    for snapshot in snapshots:
        for row in (snapshot.get("current") or {}).get("holdings") or []:
            ts_code = raw_to_ts_code(row.get("symbol"))
            if not ts_code:
                continue
            symbols.add(ts_code)
            name = str(row.get("name") or "").strip()
            if name:
                names[ts_code] = name
        for event in (snapshot.get("history") or {}).get("items") or []:
            for row in event.get("rebalancing_histories") or []:
                ts_code = raw_to_ts_code(row.get("stock_symbol"))
                if not ts_code:
                    continue
                symbols.add(ts_code)
                name = str(row.get("stock_name") or "").strip()
                if name:
                    names[ts_code] = name
    return sorted(symbols), names


def collect_cashlike_hits(snapshots: Iterable[Dict[str, Any]], names: Dict[str, str]) -> List[Dict[str, Any]]:
    current_counts: Counter[Tuple[str, str]] = Counter()
    history_counts: Counter[Tuple[str, str]] = Counter()

    for snapshot in snapshots:
        seen_current = set()
        for row in (snapshot.get("current") or {}).get("holdings") or []:
            ts_code = raw_to_ts_code(row.get("symbol"))
            if not ts_code:
                continue
            name = str(row.get("name") or names.get(ts_code) or "").strip()
            if is_cashlike_etf(ts_code, name):
                seen_current.add((ts_code, name))
        for key in seen_current:
            current_counts[key] += 1

        seen_history = set()
        for event in (snapshot.get("history") or {}).get("items") or []:
            for row in event.get("rebalancing_histories") or []:
                ts_code = raw_to_ts_code(row.get("stock_symbol"))
                if not ts_code:
                    continue
                name = str(row.get("stock_name") or names.get(ts_code) or "").strip()
                if is_cashlike_etf(ts_code, name):
                    seen_history.add((ts_code, name))
        for key in seen_history:
            history_counts[key] += 1

    rows = []
    for key in set(current_counts) | set(history_counts):
        symbol, name = key
        rows.append(
            {
                "symbol": symbol,
                "xueqiu_symbol": ts_to_raw_symbol(symbol),
                "name": name,
                "current_holding_cube_count": current_counts.get(key, 0),
                "history_cube_count": history_counts.get(key, 0),
            }
        )
    rows.sort(key=lambda row: (row["current_holding_cube_count"], row["history_cube_count"]), reverse=True)
    return rows


def latest_user_dates(history: Dict[str, Any]) -> List[date]:
    dates = [item_date for item_date in (user_rebalance_date(event) for event in history.get("items") or []) if item_date]
    return sorted(set(dates))


def is_user_active_on(day: date, user_dates: List[date], active_days: int) -> bool:
    index = bisect_right(user_dates, day) - 1
    if index < 0:
        return False
    return (day - user_dates[index]).days <= active_days


def fold_cashlike_holdings(
    holdings: Dict[str, float],
    cash_pct: float,
    names: Dict[str, str],
    *,
    cashlike_as_cash: bool,
) -> Tuple[Dict[str, float], float]:
    cash_total = max(cash_pct, 0.0)
    stock_holdings: Dict[str, float] = {}
    for symbol, weight in holdings.items():
        if cashlike_as_cash and is_cashlike_etf(symbol, names.get(symbol, "")):
            cash_total += max(weight, 0.0)
        elif weight > 1e-9:
            stock_holdings[symbol] = weight
    return stock_holdings, min(max(cash_total, 0.0), 100.0)


def reconstruct_daily_aggregates(
    snapshots: List[Dict[str, Any]],
    returns: pd.DataFrame,
    *,
    start_date: date,
    end_date: date,
    active_days: int,
    names: Dict[str, str],
    cashlike_as_cash: bool,
) -> Tuple[
    Dict[date, Dict[str, float]],
    Dict[date, float],
    Dict[date, int],
    Dict[date, Dict[str, int]],
]:
    trading_days = [day for day in list(returns.index) if start_date <= day <= end_date]
    aggregate: Dict[date, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    cash_sums: Dict[date, float] = defaultdict(float)
    active_counts: Dict[date, int] = defaultdict(int)
    holding_counts: Dict[date, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    if not trading_days:
        return aggregate, cash_sums, active_counts, holding_counts

    for index, snapshot in enumerate(snapshots, start=1):
        holdings, cash_pct = initial_holdings(snapshot)
        user_dates = latest_user_dates(snapshot.get("history") or {})

        events_by_day: Dict[date, List[Dict[str, Any]]] = defaultdict(list)
        post_end_events: List[Dict[str, Any]] = []
        for event in (snapshot.get("history") or {}).get("items") or []:
            if event.get("status") not in (None, "success"):
                continue
            item_date = event_created_date(event)
            if not item_date:
                continue
            if item_date > end_date:
                post_end_events.append(event)
                continue
            if item_date < start_date:
                continue
            effective = effective_event_date(item_date, trading_days)
            if effective is not None:
                events_by_day[effective].append(event)

        for event in sorted(post_end_events, key=event_sort_value, reverse=True):
            holdings, cash_pct = reverse_event(holdings, cash_pct, event)
        for event_list in events_by_day.values():
            event_list.sort(key=event_sort_value)

        for day in reversed(trading_days):
            if is_user_active_on(day, user_dates, active_days):
                folded_holdings, folded_cash = fold_cashlike_holdings(
                    holdings,
                    cash_pct,
                    names,
                    cashlike_as_cash=cashlike_as_cash,
                )
                active_counts[day] += 1
                cash_sums[day] += folded_cash
                for symbol, weight in folded_holdings.items():
                    aggregate[day][symbol] += weight
                    holding_counts[day][symbol] += 1

            for event in reversed(events_by_day.get(day, [])):
                holdings, cash_pct = reverse_event(holdings, cash_pct, event)
            holdings, cash_pct = inverse_drift(holdings, cash_pct, returns.loc[day])

        if index % 100 == 0 or index == len(snapshots):
            print(f"reconstructed {index}/{len(snapshots)} cubes", flush=True)

    return aggregate, cash_sums, active_counts, holding_counts


def cash_bucket_floor10(cash_composite_pct: float) -> float:
    return min(100.0, max(0.0, math.floor((cash_composite_pct + 1e-9) / 10.0) * 10.0))


def build_daily_infos(
    aggregate: Dict[date, Dict[str, float]],
    cash_sums: Dict[date, float],
    active_counts: Dict[date, int],
    holding_counts: Dict[date, Dict[str, int]],
    *,
    names: Dict[str, str],
    total_slots: int,
    buffer_width: int,
    top_rows_limit: int,
) -> Tuple[Dict[date, Dict[str, Any]], List[Dict[str, Any]]]:
    infos: Dict[date, Dict[str, Any]] = {}
    rank_rows: List[Dict[str, Any]] = []

    for day in sorted(aggregate):
        active = active_counts.get(day, 0)
        if active <= 0:
            continue
        ranking = sorted(aggregate[day].items(), key=lambda item: item[1], reverse=True)
        cash_composite = cash_sums.get(day, 0.0) / active
        cash_target = cash_bucket_floor10(cash_composite)
        cash_slots = int(round(cash_target / 10.0))
        stock_slots = max(0, total_slots - cash_slots)
        keep_count = max(stock_slots + buffer_width, stock_slots)
        ranked_symbols = [symbol for symbol, _ in ranking]
        rank_map = {symbol: rank for rank, symbol in enumerate(ranked_symbols, start=1)}

        infos[day] = {
            "date": day,
            "ranking": ranking,
            "ranked_symbols": ranked_symbols,
            "rank_map": rank_map,
            "active_cube_count": active,
            "cash_composite_pct": cash_composite,
            "cash_target_pct": cash_target,
            "cash_slots": cash_slots,
            "stock_slots": stock_slots,
            "keep_count": keep_count,
        }

        for rank, (symbol, weight_sum) in enumerate(ranking[:top_rows_limit], start=1):
            rank_rows.append(
                {
                    "date": day.isoformat(),
                    "rank": rank,
                    "symbol": symbol,
                    "xueqiu_symbol": ts_to_raw_symbol(symbol),
                    "stock_name": names.get(symbol, ""),
                    "aggregate_weight_pct": weight_sum / active,
                    "holding_cube_count": holding_counts[day].get(symbol, 0),
                    "active_cube_count": active,
                    "cash_composite_pct": cash_composite,
                    "cash_target_pct": cash_target,
                    "cash_slots": cash_slots,
                    "stock_slots": stock_slots,
                }
            )

    return infos, rank_rows


def post_return_weights(weights: Dict[str, float], returns: pd.Series) -> Dict[str, float]:
    values = {
        symbol: weight * (1.0 + (safe_float(returns.get(symbol), 0.0) or 0.0))
        for symbol, weight in weights.items()
    }
    return normalize_weights(values)


def needs_rebalance(
    current: Dict[str, float],
    info: Dict[str, Any],
    *,
    previous_cash_target: Optional[float],
) -> bool:
    if not current:
        return True
    cash_target = float(info["cash_target_pct"])
    if previous_cash_target is None or abs(cash_target - previous_cash_target) > 1e-9:
        return True

    stock_slots = int(info["stock_slots"])
    keep_set = set(info["ranked_symbols"][: int(info["keep_count"])])
    held_stocks = [symbol for symbol, weight in current.items() if symbol != CASH_SYMBOL and weight > 1e-6]
    if len(held_stocks) != stock_slots:
        return True
    return any(symbol not in keep_set for symbol in held_stocks)


def build_stock_min_turnover_weights(
    *,
    final_symbols: List[str],
    added_symbols: List[str],
    current: Dict[str, float],
    stock_budget: float,
) -> Dict[str, float]:
    if not final_symbols or stock_budget <= 1e-9:
        return {}

    added_set = set(added_symbols)
    retained_symbols = [symbol for symbol in final_symbols if symbol not in added_set]
    equal_weight = stock_budget / len(final_symbols)

    weights: Dict[str, float] = {}
    retained_sum = 0.0
    for symbol in retained_symbols:
        current_weight = safe_float(current.get(symbol))
        if current_weight is None or current_weight <= 0:
            weight = equal_weight
        elif abs(current_weight - equal_weight) > RETAIN_WEIGHT_TOLERANCE_PCT:
            weight = equal_weight
        else:
            weight = current_weight
        weights[symbol] = weight
        retained_sum += weight

    remaining_weight = stock_budget - retained_sum
    if added_symbols:
        if remaining_weight <= 0:
            retained_target_sum = max(0.0, stock_budget - equal_weight * len(added_symbols))
            scale = retained_target_sum / retained_sum if retained_sum > 0 else 0.0
            for symbol in retained_symbols:
                weights[symbol] = weights[symbol] * scale
            remaining_weight = stock_budget - retained_target_sum
        per_added_weight = remaining_weight / len(added_symbols)
        for symbol in added_symbols:
            weights[symbol] = max(0.0, per_added_weight)
        return weights

    if abs(remaining_weight) <= 1e-9:
        return weights

    underweight_symbols = [
        symbol for symbol in retained_symbols
        if weights.get(symbol, 0.0) < equal_weight
    ]
    recipients = underweight_symbols or retained_symbols
    if not recipients:
        return weights

    if remaining_weight > 0 and underweight_symbols:
        capacity = sum(equal_weight - weights[symbol] for symbol in underweight_symbols)
        if capacity > 0:
            allocated = 0.0
            for symbol in underweight_symbols:
                share = (equal_weight - weights[symbol]) / capacity
                add_weight = min(equal_weight - weights[symbol], remaining_weight * share)
                weights[symbol] += add_weight
                allocated += add_weight
            remaining_weight -= allocated
            recipients = retained_symbols

    if abs(remaining_weight) > 1e-9 and recipients:
        per_symbol_delta = remaining_weight / len(recipients)
        for symbol in recipients:
            weights[symbol] = max(0.0, weights.get(symbol, 0.0) + per_symbol_delta)

    total = sum(weights.values())
    if total > 0 and abs(total - stock_budget) > 1e-9:
        scale = stock_budget / total
        weights = {symbol: weight * scale for symbol, weight in weights.items()}
    return weights


def rebalance_min_turnover(current: Dict[str, float], info: Dict[str, Any]) -> Dict[str, float]:
    cash_target = float(info["cash_target_pct"])
    stock_slots = int(info["stock_slots"])
    ranked_symbols = list(info["ranked_symbols"])
    rank_map = dict(info["rank_map"])
    keep_set = set(ranked_symbols[: int(info["keep_count"])])
    stock_budget = max(0.0, 100.0 - cash_target)

    if stock_slots <= 0 or stock_budget <= 1e-9:
        return {CASH_SYMBOL: 100.0}

    retained_symbols = [
        symbol for symbol, weight in current.items()
        if symbol != CASH_SYMBOL and weight > 1e-9 and symbol in keep_set
    ]
    if len(retained_symbols) > stock_slots:
        retained_symbols = sorted(
            retained_symbols,
            key=lambda symbol: (
                rank_map.get(symbol, 999999),
                symbol,
            ),
        )[:stock_slots]

    added_symbols: List[str] = []
    retained_set = set(retained_symbols)
    for symbol in ranked_symbols[:stock_slots]:
        if len(retained_symbols) + len(added_symbols) >= stock_slots:
            break
        if symbol not in retained_set and symbol not in added_symbols:
            added_symbols.append(symbol)

    final_symbols = retained_symbols + added_symbols
    final_symbols = sorted(
        final_symbols,
        key=lambda symbol: (
            rank_map.get(symbol, 999999),
            symbol,
        ),
    )
    stock_weights = build_stock_min_turnover_weights(
        final_symbols=final_symbols,
        added_symbols=added_symbols,
        current=current,
        stock_budget=stock_budget,
    )
    target = {symbol: weight for symbol, weight in stock_weights.items() if weight > 1e-8}
    if cash_target > 1e-9:
        target[CASH_SYMBOL] = cash_target

    total = sum(target.values())
    if total > 0 and abs(total - 100.0) > 1e-8:
        stock_total = sum(weight for symbol, weight in target.items() if symbol != CASH_SYMBOL)
        if stock_total > 0:
            stock_scale = max(0.0, 100.0 - target.get(CASH_SYMBOL, 0.0)) / stock_total
            for symbol in list(target):
                if symbol != CASH_SYMBOL:
                    target[symbol] *= stock_scale
        elif CASH_SYMBOL in target:
            target[CASH_SYMBOL] = 100.0
    return {symbol: weight for symbol, weight in target.items() if weight > 1e-8}


def format_buy_sell(before: Dict[str, float], after: Dict[str, float]) -> Tuple[str, str, int, int]:
    before_symbols = {symbol for symbol, weight in before.items() if weight > 1e-6}
    after_symbols = {symbol for symbol, weight in after.items() if weight > 1e-6}
    buy_symbols = sorted(after_symbols - before_symbols)
    sell_symbols = sorted(before_symbols - after_symbols)

    def fmt(symbol: str) -> str:
        return CASH_SYMBOL if symbol == CASH_SYMBOL else ts_to_raw_symbol(symbol)

    return (
        ",".join(fmt(symbol) for symbol in buy_symbols),
        ",".join(fmt(symbol) for symbol in sell_symbols),
        len(buy_symbols),
        len(sell_symbols),
    )


def load_benchmark(analytics_db: Path, *, start_date: date, end_date: date) -> pd.DataFrame:
    con = duckdb.connect(str(analytics_db), read_only=True)
    try:
        bench_df = con.execute(
            """
            SELECT trade_date, close
            FROM a_stock_index_daily
            WHERE ts_code = '000905.SH'
              AND trade_date BETWEEN ? AND ?
            ORDER BY trade_date
            """,
            [start_date, end_date],
        ).fetchdf()
    finally:
        con.close()
    bench_df["trade_date"] = pd.to_datetime(bench_df["trade_date"]).dt.date
    return bench_df.set_index("trade_date").sort_index()


def backtest_infos(
    infos: Dict[date, Dict[str, Any]],
    returns: pd.DataFrame,
    benchmark_close: pd.DataFrame,
    *,
    names: Dict[str, str],
    slippage_pct: float,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, Dict[str, Any]]:
    dates = [day for day in sorted(infos) if day in returns.index]
    if len(dates) < 2:
        raise RuntimeError("not enough target dates")

    nav_raw = 1.0
    nav_slip = 1.0
    current: Dict[str, float] = {}
    previous_cash_target: Optional[float] = None
    curve_rows: List[Dict[str, Any]] = []
    rebalance_rows: List[Dict[str, Any]] = []
    holding_rows: List[Dict[str, Any]] = []

    for index, day in enumerate(dates):
        info = infos[day]
        daily_return = 0.0
        if index > 0:
            ret_row = returns.loc[day]
            daily_return = sum(
                (weight / 100.0) * (safe_float(ret_row.get(symbol), 0.0) or 0.0)
                for symbol, weight in current.items()
            )
            nav_raw *= 1.0 + daily_return
            nav_slip *= 1.0 + daily_return
            current = post_return_weights(current, ret_row)

        traded = needs_rebalance(current, info, previous_cash_target=previous_cash_target)
        action = "hold"
        turnover = 0.0
        cost_pct = 0.0
        if traded:
            before_trade = dict(current)
            target = rebalance_min_turnover(current, info)
            turnover = one_way_turnover(current, target)
            cost_pct = (turnover / 100.0) * slippage_pct
            nav_slip *= 1.0 - cost_pct / 100.0
            current = target
            action = "initial" if index == 0 else "component_changed"
            buy, sell, buy_count, sell_count = format_buy_sell(before_trade, current)
            rebalance_rows.append(
                {
                    "date": day.isoformat(),
                    "action": action,
                    "active_cube_count": info["active_cube_count"],
                    "cash_composite_pct": info["cash_composite_pct"],
                    "cash_target_pct": info["cash_target_pct"],
                    "actual_cash_pct": current.get(CASH_SYMBOL, 0.0),
                    "stock_slots": info["stock_slots"],
                    "buy": buy,
                    "sell": sell,
                    "buy_count": buy_count,
                    "sell_count": sell_count,
                    "turnover_pct": turnover,
                    "slippage_cost_pct": cost_pct,
                    "holdings": json.dumps(json_safe(current), ensure_ascii=False, sort_keys=True),
                }
            )

        previous_cash_target = float(info["cash_target_pct"])
        curve_rows.append(
            {
                "date": day.isoformat(),
                "raw_nav": nav_raw,
                "slippage_nav": nav_slip,
                "daily_return_pct": daily_return * 100.0,
                "turnover_pct": turnover,
                "slippage_cost_pct": cost_pct,
                "traded": traded,
                "action": action,
                "active_cube_count": info["active_cube_count"],
                "cash_composite_pct": info["cash_composite_pct"],
                "cash_target_pct": info["cash_target_pct"],
                "actual_cash_pct": current.get(CASH_SYMBOL, 0.0),
                "stock_slots": info["stock_slots"],
            }
        )
        for symbol, weight in sorted(current.items()):
            holding_rows.append(
                {
                    "date": day.isoformat(),
                    "symbol": symbol,
                    "xueqiu_symbol": CASH_SYMBOL if symbol == CASH_SYMBOL else ts_to_raw_symbol(symbol),
                    "stock_name": "现金" if symbol == CASH_SYMBOL else names.get(symbol, ""),
                    "weight_pct": weight,
                }
            )

    curve = pd.DataFrame(curve_rows)
    bench = benchmark_close.loc[[day for day in dates if day in benchmark_close.index]].copy()
    if not bench.empty:
        bench_nav = bench["close"] / bench["close"].iloc[0]
        curve["benchmark_nav"] = curve["date"].map({day.isoformat(): value for day, value in bench_nav.items()})
    else:
        curve["benchmark_nav"] = None

    rebalances = pd.DataFrame(rebalance_rows)
    holdings = pd.DataFrame(holding_rows)
    performance = {
        "raw": compute_metrics(curve, "raw_nav"),
        "after_slippage": compute_metrics(curve, "slippage_nav"),
        "benchmark": compute_metrics(curve.dropna(subset=["benchmark_nav"]).reset_index(drop=True), "benchmark_nav"),
        "total_slippage_cost_pct": float(curve["slippage_cost_pct"].sum()),
    }
    return curve, rebalances, holdings, performance


def monthly_returns(curve: pd.DataFrame) -> List[Dict[str, Any]]:
    df = curve[["date", "slippage_nav"]].copy()
    df["date"] = pd.to_datetime(df["date"])
    df["month"] = df["date"].dt.strftime("%Y-%m")
    rows = []
    for month, group in df.groupby("month"):
        if len(group) < 2:
            continue
        ret = group["slippage_nav"].iloc[-1] / group["slippage_nav"].iloc[0] - 1.0
        rows.append({"month": month, "return_pct": ret * 100.0})
    return rows


def summarize_run(
    *,
    label: str,
    method: str,
    output_dir: Path,
    curve: pd.DataFrame,
    rebalances: pd.DataFrame,
    holdings: pd.DataFrame,
    rank_rows: List[Dict[str, Any]],
    performance: Dict[str, Any],
    snapshots: List[Dict[str, Any]],
    failures: List[Dict[str, Any]],
    active_days: int,
    total_slots: int,
    buffer_width: int,
    slippage_pct: float,
    cashlike_as_cash: bool,
    cashlike_hits: List[Dict[str, Any]],
) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rank_df = pd.DataFrame(rank_rows)
    monthly = pd.DataFrame(monthly_returns(curve))

    curve.to_csv(output_dir / "strategy_curve.csv", index=False)
    rebalances.to_csv(output_dir / "rebalance_records.csv", index=False)
    holdings.to_csv(output_dir / "daily_holdings.csv", index=False)
    rank_df.to_csv(output_dir / "daily_rank_top25.csv", index=False)
    monthly.to_csv(output_dir / "monthly_returns.csv", index=False)

    latest_date = str(curve["date"].iloc[-1])
    latest_holdings = holdings[holdings["date"] == latest_date].sort_values("weight_pct", ascending=False).to_dict("records")
    cash_targets = curve["cash_target_pct"].value_counts().sort_index().to_dict()
    slip = performance["after_slippage"]

    summary = {
        "strategy": label,
        "method": method,
        "start_date": str(curve["date"].iloc[0]),
        "end_date": str(curve["date"].iloc[-1]),
        "active_filter_days": active_days,
        "active_mode": "user_rebalancing",
        "active_time_field": "user_rebalancing.updated_at",
        "cashlike_as_cash": cashlike_as_cash,
        "cashlike_name_keywords": list(CASHLIKE_NAME_KEYWORDS),
        "total_slots": total_slots,
        "buffer_width": buffer_width,
        "slippage_pct": slippage_pct,
        "trading_days": int(len(curve)),
        "snapshot_success_count": len(snapshots),
        "snapshot_failed_count": len(failures),
        "snapshot_failures": failures,
        "avg_active_cube_count": float(curve["active_cube_count"].mean()),
        "median_active_cube_count": float(curve["active_cube_count"].median()),
        "latest_active_cube_count": int(curve["active_cube_count"].iloc[-1]),
        "min_active_cube_count": int(curve["active_cube_count"].min()),
        "max_active_cube_count": int(curve["active_cube_count"].max()),
        "avg_cash_composite_pct": float(curve["cash_composite_pct"].mean()),
        "median_cash_composite_pct": float(curve["cash_composite_pct"].median()),
        "min_cash_composite_pct": float(curve["cash_composite_pct"].min()),
        "max_cash_composite_pct": float(curve["cash_composite_pct"].max()),
        "latest_cash_composite_pct": float(curve["cash_composite_pct"].iloc[-1]),
        "latest_cash_target_pct": float(curve["cash_target_pct"].iloc[-1]),
        "cash_target_day_counts": {str(key): int(value) for key, value in cash_targets.items()},
        "rebalance_days_including_initial": int(len(rebalances)),
        "rebalance_days_excluding_initial": int(max(len(rebalances) - 1, 0)),
        "turnover_pct_including_initial": float(curve["turnover_pct"].sum()),
        "turnover_pct_excluding_initial": float(curve["turnover_pct"].iloc[1:].sum()),
        "buy_actions_excluding_initial": int(rebalances["buy_count"].iloc[1:].sum()) if len(rebalances) > 1 else 0,
        "sell_actions_excluding_initial": int(rebalances["sell_count"].iloc[1:].sum()) if len(rebalances) > 1 else 0,
        "performance": performance,
        "latest_holdings": latest_holdings,
        "cashlike_hits": cashlike_hits if cashlike_as_cash else [],
        "files": {
            "output_dir": str(output_dir),
            "curve_csv": str(output_dir / "strategy_curve.csv"),
            "rebalances_csv": str(output_dir / "rebalance_records.csv"),
            "holdings_csv": str(output_dir / "daily_holdings.csv"),
            "daily_rank_csv": str(output_dir / "daily_rank_top25.csv"),
            "summary_json": str(output_dir / "summary.json"),
            "report_md": str(output_dir / "report.md"),
        },
    }
    write_json(output_dir / "summary.json", summary)

    def fmt(value: Any, suffix: str = "", digits: int = 2) -> str:
        number = safe_float(value)
        if number is None:
            return "N/A"
        return f"{number:.{digits}f}{suffix}"

    lines = [
        f"# {label}",
        "",
        f"- 区间：{summary['start_date']} 至 {summary['end_date']}，交易日 {summary['trading_days']} 天。",
        f"- active：主理人调仓 {active_days} 日内；现金：按 10% 向下取整，占用 10 个总槽位。",
        f"- 现金类 ETF 并入现金：{'是' if cashlike_as_cash else '否'}。",
        f"- active 组合数：平均 {fmt(summary['avg_active_cube_count'], digits=2)}，最新 {summary['latest_active_cube_count']}。",
        f"- 现金综合权重：平均 {fmt(summary['avg_cash_composite_pct'], '%')}，最小 {fmt(summary['min_cash_composite_pct'], '%')}，最大 {fmt(summary['max_cash_composite_pct'], '%')}，最新 {fmt(summary['latest_cash_composite_pct'], '%')}。",
        f"- 现金目标档位天数：{summary['cash_target_day_counts']}。",
        f"- 滑点后：总收益 {fmt(slip.get('total_return_pct'), '%')}，年化 {fmt(slip.get('annualized_return_pct'), '%')}，夏普 {fmt(slip.get('sharpe'))}，最大回撤 {fmt(slip.get('max_drawdown_pct'), '%')}。",
        f"- 换手：不含初始 {fmt(summary['turnover_pct_excluding_initial'], '%')}，调仓日 {summary['rebalance_days_excluding_initial']} 天。",
        "",
        "## 最新持仓",
        "",
        "| 标的 | 名称 | 权重 |",
        "| --- | --- | ---: |",
    ]
    for row in latest_holdings:
        lines.append(f"| {row['xueqiu_symbol']} | {row['stock_name']} | {fmt(row['weight_pct'], '%')} |")
    if cashlike_as_cash and cashlike_hits:
        lines.extend(["", "## 命中的现金类 ETF", "", "| 标的 | 名称 | 当前持有组合数 | 历史出现组合数 |", "| --- | --- | ---: | ---: |"])
        for row in cashlike_hits[:20]:
            lines.append(
                f"| {row['xueqiu_symbol']} | {row['name']} | {row['current_holding_cube_count']} | {row['history_cube_count']} |"
            )
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def run_policy(
    *,
    snapshots: List[Dict[str, Any]],
    failures: List[Dict[str, Any]],
    returns: pd.DataFrame,
    benchmark: pd.DataFrame,
    names: Dict[str, str],
    output_dir: Path,
    active_days: int,
    cashlike_as_cash: bool,
    total_slots: int,
    buffer_width: int,
    slippage_pct: float,
    cashlike_hits: List[Dict[str, Any]],
) -> Dict[str, Any]:
    aggregate, cash_sums, active_counts, holding_counts = reconstruct_daily_aggregates(
        snapshots,
        returns,
        start_date=returns.index[0],
        end_date=returns.index[-1],
        active_days=active_days,
        names=names,
        cashlike_as_cash=cashlike_as_cash,
    )
    infos, rank_rows = build_daily_infos(
        aggregate,
        cash_sums,
        active_counts,
        holding_counts,
        names=names,
        total_slots=total_slots,
        buffer_width=buffer_width,
        top_rows_limit=25,
    )
    curve, rebalances, holdings, performance = backtest_infos(
        infos,
        returns,
        benchmark,
        names=names,
        slippage_pct=slippage_pct,
    )
    label = (
        f"主理人 active{active_days}：现金类ETF并入现金 + 10%档位占位"
        if cashlike_as_cash
        else f"主理人 active{active_days}：普通现金 + 10%档位占位基线"
    )
    method = (
        f"backward_cached_user_active{active_days}_cashlike_etf_cash_bucket_floor10_top10_sell12_min_turnover"
        if cashlike_as_cash
        else f"backward_cached_user_active{active_days}_regular_cash_bucket_floor10_top10_sell12_min_turnover"
    )
    return summarize_run(
        label=label,
        method=method,
        output_dir=output_dir,
        curve=curve,
        rebalances=rebalances,
        holdings=holdings,
        rank_rows=rank_rows,
        performance=performance,
        snapshots=snapshots,
        failures=failures,
        active_days=active_days,
        total_slots=total_slots,
        buffer_width=buffer_width,
        slippage_pct=slippage_pct,
        cashlike_as_cash=cashlike_as_cash,
        cashlike_hits=cashlike_hits,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list-path", type=Path, default=DEFAULT_LIST_PATH)
    parser.add_argument("--snapshot-dir", type=Path, default=DEFAULT_SNAPSHOT_DIR)
    parser.add_argument("--full-history-dir", type=Path, default=DEFAULT_FULL_HISTORY_DIR)
    parser.add_argument("--analytics-db", type=Path, default=DEFAULT_ANALYTICS_DB)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--start-date", default="2025-10-01")
    parser.add_argument("--end-date", default="2026-05-22")
    parser.add_argument("--active-days", type=int, default=380)
    parser.add_argument("--total-slots", type=int, default=10)
    parser.add_argument("--buffer-width", type=int, default=2)
    parser.add_argument("--slippage", type=float, default=0.5)
    args = parser.parse_args()

    cubes = load_json(args.list_path, [])
    if not cubes:
        raise RuntimeError(f"No cubes loaded from {args.list_path}")
    snapshots, failures = load_cached_snapshots(
        cubes,
        snapshot_dir=args.snapshot_dir,
        full_history_dir=args.full_history_dir,
    )
    if not snapshots:
        raise RuntimeError("No cached snapshots loaded")

    symbols, names = collect_symbols_and_names(snapshots)
    cashlike_hits = collect_cashlike_hits(snapshots, names)
    start_date = parse_date(args.start_date)
    end_date = parse_date(args.end_date)
    _, returns = load_price_data(args.analytics_db, symbols, start=start_date, end=end_date)
    returns = returns.loc[(returns.index >= start_date) & (returns.index <= end_date)]
    if returns.empty:
        raise RuntimeError("No returns loaded for selected date range")
    benchmark = load_benchmark(args.analytics_db, start_date=returns.index[0], end_date=returns.index[-1])

    regular = run_policy(
        snapshots=snapshots,
        failures=failures,
        returns=returns,
        benchmark=benchmark,
        names=names,
        output_dir=args.output_dir / "regular_cash_only",
        active_days=args.active_days,
        cashlike_as_cash=False,
        total_slots=args.total_slots,
        buffer_width=args.buffer_width,
        slippage_pct=args.slippage,
        cashlike_hits=[],
    )
    cashlike = run_policy(
        snapshots=snapshots,
        failures=failures,
        returns=returns,
        benchmark=benchmark,
        names=names,
        output_dir=args.output_dir / "cashlike_etf_as_cash",
        active_days=args.active_days,
        cashlike_as_cash=True,
        total_slots=args.total_slots,
        buffer_width=args.buffer_width,
        slippage_pct=args.slippage,
        cashlike_hits=cashlike_hits,
    )

    comparison = {
        "regular_cash_only": regular,
        "cashlike_etf_as_cash": cashlike,
        "delta_after_slippage_total_return_pct": (
            cashlike["performance"]["after_slippage"]["total_return_pct"]
            - regular["performance"]["after_slippage"]["total_return_pct"]
        ),
        "delta_after_slippage_annualized_return_pct": (
            cashlike["performance"]["after_slippage"]["annualized_return_pct"]
            - regular["performance"]["after_slippage"]["annualized_return_pct"]
        ),
        "delta_after_slippage_max_drawdown_pct": (
            cashlike["performance"]["after_slippage"]["max_drawdown_pct"]
            - regular["performance"]["after_slippage"]["max_drawdown_pct"]
        ),
        "delta_turnover_pct_excluding_initial": (
            cashlike["turnover_pct_excluding_initial"] - regular["turnover_pct_excluding_initial"]
        ),
        "snapshot_failures": failures,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_json(args.output_dir / "comparison_summary.json", comparison)

    print(json.dumps(json_safe(comparison), ensure_ascii=False, indent=2))
    print(f"Saved comparison: {args.output_dir / 'comparison_summary.json'}")


if __name__ == "__main__":
    main()

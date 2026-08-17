#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import time
from bisect import bisect_left
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import duckdb
import pandas as pd
import requests

from backtest_xueqiu_top_holdings_strategy import (
    DEFAULT_ANALYTICS_DB,
    DEFAULT_LIST_PATH,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_SQLITE_DB,
    XUEQIU_API_BASE_URL,
    backtest_targets,
    build_daily_targets,
    compute_metrics,
    effective_event_date,
    fetch_rebalance_page,
    get_cookie,
    json_safe,
    load_json,
    load_price_data,
    monthly_returns,
    ms_to_shanghai,
    parse_date,
    raw_to_ts_code,
    render_report,
    request_headers,
    safe_float,
    ts_to_raw_symbol,
    write_json,
)


DEFAULT_BACKWARD_OUTPUT_DIR = DEFAULT_OUTPUT_DIR.with_name(DEFAULT_OUTPUT_DIR.name + "_backward")


def event_date(event: Dict[str, Any]) -> Optional[date]:
    created_at = ms_to_shanghai(event.get("created_at"))
    return created_at.date() if created_at else None


def fetch_recent_history(
    *,
    cube: Dict[str, Any],
    cookie: str,
    recent_cache_dir: Path,
    full_cache_dir: Path,
    refresh: bool,
    timeout: float,
    start_date: date,
    max_pages: int,
) -> Dict[str, Any]:
    symbol = str(cube.get("symbol") or "").strip().upper()
    full_cache_path = full_cache_dir / f"{symbol}.json"
    if full_cache_path.exists() and not refresh:
        payload = load_json(full_cache_path, {})
        payload["from_cache"] = "full"
        return payload

    cache_path = recent_cache_dir / f"{symbol}.json"
    if cache_path.exists() and not refresh:
        payload = load_json(cache_path, {})
        if payload.get("history_start_date") == start_date.isoformat():
            payload["from_cache"] = "recent"
            return payload

    session = requests.Session()
    session.headers.update(request_headers(cookie, symbol))
    count = 50
    all_items: List[Dict[str, Any]] = []
    total_count: Optional[int] = None
    page = 1
    while page <= max_pages:
        payload = fetch_rebalance_page(session, symbol, page, count, timeout)
        if total_count is None and payload.get("totalCount") is not None:
            total_count = int(payload.get("totalCount") or 0)
        items = payload.get("list") or []
        if not items:
            break
        all_items.extend(items)
        oldest = min((item_date for item_date in (event_date(item) for item in items) if item_date), default=None)
        if oldest and oldest < start_date:
            break
        if total_count is not None and len(all_items) >= total_count:
            break
        page += 1
        time.sleep(0.15)

    result = {
        "symbol": symbol,
        "year_rank": cube.get("year_rank"),
        "cube_name": cube.get("cube_name"),
        "screen_name": cube.get("screen_name"),
        "history_start_date": start_date.isoformat(),
        "total_count": total_count,
        "pages_fetched": page,
        "fetched_at": datetime.now().isoformat(),
        "items": all_items,
        "from_cache": False,
    }
    write_json(cache_path, result)
    return result


def fetch_current_holdings(
    *,
    cube: Dict[str, Any],
    cookie: str,
    cache_dir: Path,
    refresh: bool,
    timeout: float,
) -> Dict[str, Any]:
    symbol = str(cube.get("symbol") or "").strip().upper()
    cache_path = cache_dir / f"{symbol}.json"
    if cache_path.exists() and not refresh:
        payload = load_json(cache_path, {})
        payload["from_cache"] = True
        return payload

    session = requests.Session()
    session.headers.update(request_headers(cookie, symbol))
    last_error: Optional[Exception] = None
    for attempt in range(5):
        try:
            response = session.get(
                f"{XUEQIU_API_BASE_URL}/cube/center/cube/holdSymbols.json",
                params={"symbol": symbol},
                timeout=timeout,
            )
            if response.status_code >= 400:
                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
            payload = response.json()
            holdings = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(holdings, list):
                raise RuntimeError(f"Unexpected holdSymbols payload: {str(payload)[:300]}")
            result = {
                "symbol": symbol,
                "year_rank": cube.get("year_rank"),
                "cube_name": cube.get("cube_name"),
                "screen_name": cube.get("screen_name"),
                "fetched_at": datetime.now().isoformat(),
                "holdings": holdings,
                "from_cache": False,
            }
            write_json(cache_path, result)
            return result
        except Exception as exc:
            last_error = exc
            time.sleep(min(10.0, 0.6 * (attempt + 1)))
    raise RuntimeError(str(last_error) if last_error else "unknown holdSymbols error")


def fetch_snapshot(
    cube: Dict[str, Any],
    *,
    cookie: str,
    output_dir: Path,
    refresh: bool,
    timeout: float,
    start_date: date,
    max_pages: int,
) -> Dict[str, Any]:
    return {
        "cube": cube,
        "history": fetch_recent_history(
            cube=cube,
            cookie=cookie,
            recent_cache_dir=output_dir / "recent_rebalance_history_cache",
            full_cache_dir=output_dir.parent / "xueqiu_top_holdings_strategy_20260101" / "rebalance_history_cache",
            refresh=refresh,
            timeout=timeout,
            start_date=start_date,
            max_pages=max_pages,
        ),
        "current": fetch_current_holdings(
            cube=cube,
            cookie=cookie,
            cache_dir=output_dir / "current_holdings_cache",
            refresh=refresh,
            timeout=timeout,
        ),
    }


def load_snapshots(
    cubes: List[Dict[str, Any]],
    *,
    cookie: str,
    output_dir: Path,
    refresh: bool,
    workers: int,
    timeout: float,
    start_date: date,
    max_pages: int,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    snapshots: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                fetch_snapshot,
                cube,
                cookie=cookie,
                output_dir=output_dir,
                refresh=refresh,
                timeout=timeout,
                start_date=start_date,
                max_pages=max_pages,
            ): cube
            for cube in cubes
        }
        total = len(futures)
        for index, future in enumerate(as_completed(futures), start=1):
            cube = futures[future]
            try:
                snapshots.append(future.result())
            except Exception as exc:
                failures.append({"symbol": cube.get("symbol"), "error": str(exc)})
            if index % 50 == 0 or index == total:
                print(f"snapshots {index}/{total} ok={len(snapshots)} failed={len(failures)}", flush=True)
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
            if row.get("name"):
                names[ts_code] = str(row.get("name"))
        for event in (snapshot.get("history") or {}).get("items") or []:
            for row in event.get("rebalancing_histories") or []:
                ts_code = raw_to_ts_code(row.get("stock_symbol"))
                if not ts_code:
                    continue
                symbols.add(ts_code)
                if row.get("stock_name"):
                    names[ts_code] = str(row.get("stock_name"))
    return sorted(symbols), names


def initial_holdings(snapshot: Dict[str, Any]) -> Tuple[Dict[str, float], float]:
    holdings: Dict[str, float] = {}
    for row in (snapshot.get("current") or {}).get("holdings") or []:
        ts_code = raw_to_ts_code(row.get("symbol"))
        weight = safe_float(row.get("weight"), 0.0) or 0.0
        if ts_code and weight > 1e-8:
            holdings[ts_code] = weight
    stock_total = sum(holdings.values())
    if stock_total > 100.0:
        factor = 100.0 / stock_total
        holdings = {symbol: weight * factor for symbol, weight in holdings.items()}
        stock_total = 100.0
    return holdings, max(0.0, 100.0 - stock_total)


def reverse_event(holdings: Dict[str, float], cash_pct: float, event: Dict[str, Any]) -> Tuple[Dict[str, float], float]:
    previous = dict(holdings)
    for row in event.get("rebalancing_histories") or []:
        ts_code = raw_to_ts_code(row.get("stock_symbol"))
        if not ts_code:
            continue
        prev_weight = safe_float(row.get("prev_weight_adjusted"), None)
        if prev_weight is None:
            prev_weight = safe_float(row.get("prev_weight"), 0.0) or 0.0
        if prev_weight > 1e-8:
            previous[ts_code] = prev_weight
        else:
            previous.pop(ts_code, None)
    previous = {symbol: weight for symbol, weight in previous.items() if weight > 1e-8 and math.isfinite(weight)}
    stock_total = sum(previous.values())
    if stock_total > 100.0:
        factor = 100.0 / stock_total
        previous = {symbol: weight * factor for symbol, weight in previous.items()}
        stock_total = 100.0
    return previous, max(0.0, 100.0 - stock_total)


def inverse_drift(holdings: Dict[str, float], cash_pct: float, returns: pd.Series) -> Tuple[Dict[str, float], float]:
    if not holdings:
        return holdings, cash_pct
    values: Dict[str, float] = {}
    total = max(cash_pct, 0.0)
    for symbol, weight in holdings.items():
        ret = safe_float(returns.get(symbol), 0.0) or 0.0
        denominator = 1.0 + ret
        if denominator <= 0:
            continue
        value = max(weight, 0.0) / denominator
        if value > 1e-9:
            values[symbol] = value
            total += value
    if total <= 0:
        return {}, 100.0
    scale = 100.0 / total
    return {symbol: value * scale for symbol, value in values.items()}, cash_pct * scale


def reverse_reconstruct_daily_aggregates(
    snapshots: List[Dict[str, Any]],
    returns: pd.DataFrame,
    *,
    start_date: date,
    end_date: date,
) -> Tuple[Dict[date, Dict[str, float]], Dict[date, int], Dict[date, Dict[str, int]]]:
    trading_days = [day for day in list(returns.index) if start_date <= day <= end_date]
    aggregate: Dict[date, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    active_counts: Dict[date, int] = defaultdict(int)
    holding_counts: Dict[date, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    if not trading_days:
        return aggregate, active_counts, holding_counts

    for index, snapshot in enumerate(snapshots, start=1):
        holdings, cash_pct = initial_holdings(snapshot)
        if not holdings:
            continue

        events_by_day: Dict[date, List[Dict[str, Any]]] = defaultdict(list)
        post_end_events: List[Dict[str, Any]] = []
        for event in (snapshot.get("history") or {}).get("items") or []:
            item_date = event_date(event)
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

        for event in sorted(post_end_events, key=lambda item: (item.get("created_at") or 0, item.get("id") or 0), reverse=True):
            holdings, cash_pct = reverse_event(holdings, cash_pct, event)
        for event_list in events_by_day.values():
            event_list.sort(key=lambda item: (item.get("created_at") or 0, item.get("id") or 0))

        for day in reversed(trading_days):
            if holdings:
                active_counts[day] += 1
                for symbol, weight in holdings.items():
                    aggregate[day][symbol] += weight
                    holding_counts[day][symbol] += 1
            for event in reversed(events_by_day.get(day, [])):
                holdings, cash_pct = reverse_event(holdings, cash_pct, event)
            holdings, cash_pct = inverse_drift(holdings, cash_pct, returns.loc[day])

        if index % 100 == 0 or index == len(snapshots):
            print(f"reverse reconstructed {index}/{len(snapshots)} cubes", flush=True)
    return aggregate, active_counts, holding_counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list-path", type=Path, default=DEFAULT_LIST_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_BACKWARD_OUTPUT_DIR)
    parser.add_argument("--sqlite-db", type=Path, default=DEFAULT_SQLITE_DB)
    parser.add_argument("--analytics-db", type=Path, default=DEFAULT_ANALYTICS_DB)
    parser.add_argument("--start-date", default="2026-01-01")
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--slippage", type=float, default=0.5)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--max-pages", type=int, default=12)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    start_date = parse_date(args.start_date)
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    cubes = load_json(args.list_path, [])
    if not cubes:
        raise RuntimeError(f"No cubes loaded from {args.list_path}")
    cookie = get_cookie(args.sqlite_db)
    snapshots, failures = load_snapshots(
        cubes,
        cookie=cookie,
        output_dir=output_dir,
        refresh=args.refresh,
        workers=args.workers,
        timeout=args.timeout,
        start_date=start_date,
        max_pages=args.max_pages,
    )

    symbols, name_map = collect_symbols_and_names(snapshots)
    if not symbols:
        raise RuntimeError("No symbols loaded from snapshots")
    _, stock_returns = load_price_data(
        args.analytics_db,
        symbols,
        start=start_date,
        end=parse_date(args.end_date) if args.end_date else date(2099, 12, 31),
    )
    if args.end_date:
        end_date = parse_date(args.end_date)
        stock_returns = stock_returns.loc[stock_returns.index <= end_date]
    end_date = stock_returns.index[-1]

    con = duckdb.connect(str(args.analytics_db), read_only=True)
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
    bench_df = bench_df.set_index("trade_date").sort_index()

    aggregate, active_counts, holding_counts = reverse_reconstruct_daily_aggregates(
        snapshots,
        stock_returns,
        start_date=start_date,
        end_date=end_date,
    )
    targets, top_rows = build_daily_targets(aggregate, active_counts, holding_counts, top_n=args.top_n)
    targets = {day: target for day, target in targets.items() if start_date <= day <= end_date}
    top_rows = [row for row in top_rows if start_date <= parse_date(row["date"]) <= end_date]
    for row in top_rows:
        row["stock_name"] = name_map.get(row["symbol"], "")

    curve, performance = backtest_targets(
        targets,
        stock_returns,
        bench_df,
        slippage_pct=args.slippage,
    )
    monthly = monthly_returns(curve, "slippage_nav")
    latest_date = max(targets)
    latest_top = [row for row in top_rows if row["date"] == latest_date.isoformat()]

    summary = {
        "method": "backward_from_current_holdings_and_recent_rebalance_history",
        "method_note": "Uses current full Snowball holdings and reverses rebalance events from the latest snapshot back to the start date.",
        "start_date": curve["date"].iloc[0],
        "end_date": curve["date"].iloc[-1],
        "top_n": args.top_n,
        "slippage_pct": args.slippage,
        "cube_count": len(cubes),
        "snapshot_success_count": len(snapshots),
        "snapshot_failed_count": len(failures),
        "snapshot_failures": failures,
        "unique_stock_count": len(symbols),
        "active_cube_count_latest": active_counts.get(latest_date, 0),
        "latest_target_date": latest_date.isoformat(),
        "performance": performance,
        "files": {
            "output_dir": str(output_dir),
            "curve_csv": str(output_dir / "strategy_curve.csv"),
            "daily_top10_csv": str(output_dir / "daily_top10.csv"),
            "summary_json": str(output_dir / "summary.json"),
            "report_md": str(output_dir / "report.md"),
        },
    }

    report = render_report(
        {
            **summary,
            "history_success_count": len(snapshots),
            "history_failed_count": len(failures),
        },
        latest_top,
        monthly,
    )
    report += "\n> 说明：本次为快速回测口径，使用当前完整持仓 + 2026-01-01 以来历史调仓记录倒推每日持仓。\n"

    curve.to_csv(output_dir / "strategy_curve.csv", index=False)
    pd.DataFrame(top_rows).to_csv(output_dir / "daily_top10.csv", index=False)
    pd.DataFrame(monthly).to_csv(output_dir / "monthly_returns.csv", index=False)
    write_json(output_dir / "summary.json", summary)
    (output_dir / "report.md").write_text(report, encoding="utf-8")

    print(json.dumps(json_safe(summary), ensure_ascii=False, indent=2))
    print(f"Saved report: {output_dir / 'report.md'}")


if __name__ == "__main__":
    main()

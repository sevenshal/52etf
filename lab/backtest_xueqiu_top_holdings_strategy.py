#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sqlite3
import sys
import time
from bisect import bisect_left
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import duckdb
import pandas as pd
import requests


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LIST_PATH = ROOT / "lab" / "output" / "xueqiu_year_top1000_20260525_slippage_0_5" / "top1000_list.json"
DEFAULT_OUTPUT_DIR = ROOT / "lab" / "output" / "xueqiu_top_holdings_strategy_20260101"
DEFAULT_SQLITE_DB = Path("/var/lib/quant_robot/evc_stocks.db")
DEFAULT_ANALYTICS_DB = Path("/var/lib/quant_robot/analytics.duckdb")
SH_TZ = ZoneInfo("Asia/Shanghai")
XUEQIU_API_BASE_URL = "https://api.xueqiu.com"


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None:
            return default
        number = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(number) or math.isinf(number):
        return default
    return number


def ms_to_shanghai(ms: Any) -> Optional[datetime]:
    value = safe_float(ms)
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).astimezone(SH_TZ)


def normalize_raw_symbol(symbol: Any) -> Optional[str]:
    text = str(symbol or "").strip().upper().replace(".", "")
    if len(text) == 8 and (text.startswith("SH") or text.startswith("SZ")):
        return text
    if len(text) == 8 and (text.endswith("SH") or text.endswith("SZ")):
        return text[-2:] + text[:6]
    return None


def raw_to_ts_code(symbol: Any) -> Optional[str]:
    raw = normalize_raw_symbol(symbol)
    if not raw:
        return None
    return f"{raw[2:]}.{raw[:2]}"


def ts_to_raw_symbol(ts_code: str) -> str:
    code, market = ts_code.split(".")
    return f"{market}{code}"


def get_cookie(sqlite_db: Path) -> str:
    override = (os.getenv("XUEQIU_COOKIE_OVERRIDE") or "").strip()
    if override:
        if "xq_a_token=" in override:
            return override
        return f"xq_a_token={override};"

    with sqlite3.connect(sqlite_db) as con:
        rows = con.execute(
            """
            SELECT xueqiu_cookie
            FROM snowball_account_configs
            WHERE xueqiu_cookie IS NOT NULL
            ORDER BY updated_at DESC
            """
        ).fetchall()
    for (cookie,) in rows:
        cookie_text = (cookie or "").strip()
        if "xq_a_token=" in cookie_text:
            return cookie_text
    for (cookie,) in rows:
        cookie_text = (cookie or "").strip()
        if cookie_text:
            return f"xq_a_token={cookie_text};"
    raise RuntimeError(f"No Xueqiu cookie found in {sqlite_db}")


def request_headers(cookie: str, cube_symbol: str) -> Dict[str, str]:
    return {
        "Host": "api.xueqiu.com",
        "Cookie": cookie,
        "accept": "application/json",
        "accept-language": "zh-Hans-CN;q=1, en-CN;q=0.9",
        "x-device-os": "iOS 26.4.2",
        "x-device-model-name": "iPhone 16 Pro Max_iPhone17,2",
        "x-device-id": "933A28E8-45D4-447A-AA4D-93FECC7B78C5",
        "user-agent": "Xueqiu iPhone 14.90.2",
        "priority": "u=3, i",
        "Referer": f"https://xueqiu.com/P/{cube_symbol}",
    }


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(json_safe(value), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [json_safe(item) for item in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "item") and callable(value.item):
        return json_safe(value.item())
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def fetch_rebalance_page(
    session: requests.Session,
    cube_symbol: str,
    page: int,
    count: int,
    timeout: float,
    retries: int = 6,
) -> Dict[str, Any]:
    last_error: Optional[Exception] = None
    for attempt in range(retries):
        try:
            response = session.get(
                f"{XUEQIU_API_BASE_URL}/cubes/rebalancing/history.json",
                params={"cube_symbol": cube_symbol, "count": count, "page": page},
                timeout=timeout,
            )
            if response.status_code >= 400:
                error_code = ""
                try:
                    error_payload = response.json()
                    error_code = str(error_payload.get("error_code") or "")
                except ValueError:
                    pass
                error = RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
                setattr(error, "xueqiu_error_code", error_code)
                raise error
            payload = response.json()
            if not isinstance(payload, dict) or "list" not in payload:
                raise RuntimeError(f"Unexpected payload: {str(payload)[:300]}")
            return payload
        except Exception as exc:
            last_error = exc
            delay = min(10.0, 0.8 * (attempt + 1))
            if getattr(exc, "xueqiu_error_code", "") in {"10026", "400016"}:
                delay = min(45.0, 5.0 * (attempt + 1))
            time.sleep(delay)
    raise RuntimeError(str(last_error) if last_error else "unknown request error")


def fetch_or_load_history(
    *,
    cube: Dict[str, Any],
    cookie: str,
    cache_dir: Path,
    refresh: bool,
    timeout: float,
    max_pages: int,
) -> Dict[str, Any]:
    symbol = str(cube.get("symbol") or "").strip().upper()
    cache_path = cache_dir / f"{symbol}.json"
    if cache_path.exists() and not refresh:
        payload = load_json(cache_path, {})
        payload["from_cache"] = True
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
        if total_count is not None and len(all_items) >= total_count:
            break
        page += 1
        time.sleep(0.12)

    result = {
        "symbol": symbol,
        "year_rank": cube.get("year_rank"),
        "cube_name": cube.get("cube_name"),
        "screen_name": cube.get("screen_name"),
        "total_count": total_count,
        "pages_fetched": page,
        "page_limit_hit": page >= max_pages and (total_count is None or len(all_items) < total_count),
        "fetched_at": datetime.now(SH_TZ).isoformat(),
        "items": all_items,
        "from_cache": False,
    }
    write_json(cache_path, result)
    return result


def load_histories(
    cubes: List[Dict[str, Any]],
    *,
    cookie: str,
    cache_dir: Path,
    refresh: bool,
    workers: int,
    timeout: float,
    max_pages: int,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    histories: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    total = len(cubes)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                fetch_or_load_history,
                cube=cube,
                cookie=cookie,
                cache_dir=cache_dir,
                refresh=refresh,
                timeout=timeout,
                max_pages=max_pages,
            ): cube
            for cube in cubes
        }
        for index, future in enumerate(as_completed(futures), start=1):
            cube = futures[future]
            symbol = cube.get("symbol")
            try:
                histories.append(future.result())
            except Exception as exc:
                failures.append({"symbol": symbol, "error": str(exc)})
            if index % 50 == 0 or index == total:
                print(f"history {index}/{total} ok={len(histories)} failed={len(failures)}", flush=True)
    histories.sort(key=lambda item: int(item.get("year_rank") or 999999))
    return histories, failures


def iter_events(history: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    for event in history.get("items") or []:
        created_at = ms_to_shanghai(event.get("created_at"))
        if not created_at:
            continue
        yield {
            "id": event.get("id") or 0,
            "created_at": created_at,
            "event_date": created_at.date(),
            "cash_pct": safe_float(event.get("cash"), 0.0) or 0.0,
            "histories": event.get("rebalancing_histories") or [],
        }


def collect_symbols_and_names(histories: Iterable[Dict[str, Any]]) -> Tuple[List[str], Dict[str, str], Optional[date]]:
    symbols = set()
    names: Dict[str, str] = {}
    min_event_date: Optional[date] = None
    for history in histories:
        for event in iter_events(history):
            event_date = event["event_date"]
            if min_event_date is None or event_date < min_event_date:
                min_event_date = event_date
            for row in event["histories"]:
                ts_code = raw_to_ts_code(row.get("stock_symbol"))
                if not ts_code:
                    continue
                symbols.add(ts_code)
                stock_name = str(row.get("stock_name") or "").strip()
                if stock_name:
                    names[ts_code] = stock_name
    return sorted(symbols), names, min_event_date


def chunked(values: List[str], size: int) -> Iterable[List[str]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def load_price_data(
    analytics_db: Path,
    symbols: List[str],
    *,
    start: date,
    end: date,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    con = duckdb.connect(str(analytics_db), read_only=True)
    try:
        frames = []
        for chunk in chunked(symbols, 500):
            placeholders = ",".join(["?"] * len(chunk))
            frames.append(
                con.execute(
                    f"""
                    SELECT ts_code, trade_date, close
                    FROM a_stock_market_daily_qfq
                    WHERE trade_date BETWEEN ? AND ?
                      AND ts_code IN ({placeholders})
                    ORDER BY trade_date, ts_code
                    """,
                    [start, end, *chunk],
                ).fetchdf()
            )
        price_df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        benchmark_df = con.execute(
            """
            SELECT trade_date, close
            FROM a_stock_index_daily
            WHERE ts_code = '000905.SH'
              AND trade_date BETWEEN ? AND ?
            ORDER BY trade_date
            """,
            [start, end],
        ).fetchdf()
    finally:
        con.close()

    if price_df.empty:
        raise RuntimeError("No A-share price data loaded for history symbols")
    price_df["trade_date"] = pd.to_datetime(price_df["trade_date"]).dt.date
    benchmark_df["trade_date"] = pd.to_datetime(benchmark_df["trade_date"]).dt.date
    close = (
        price_df.pivot_table(index="trade_date", columns="ts_code", values="close", aggfunc="last")
        .sort_index()
        .ffill()
    )
    returns = close.pct_change().fillna(0.0)
    benchmark = benchmark_df.set_index("trade_date").sort_index()
    benchmark["daily_return"] = benchmark["close"].pct_change().fillna(0.0)
    return close, returns


def effective_event_date(event_date: date, trading_days: List[date]) -> Optional[date]:
    idx = bisect_left(trading_days, event_date)
    if idx >= len(trading_days):
        return None
    return trading_days[idx]


def drift_holdings(
    holdings: Dict[str, float],
    cash_pct: float,
    returns: pd.Series,
) -> Tuple[Dict[str, float], float]:
    if not holdings:
        return holdings, cash_pct
    values: Dict[str, float] = {}
    total = max(cash_pct, 0.0)
    for symbol, weight in holdings.items():
        ret = safe_float(returns.get(symbol), 0.0) or 0.0
        value = max(weight, 0.0) * (1.0 + ret)
        if value > 1e-9:
            values[symbol] = value
            total += value
    if total <= 0:
        return {}, 100.0
    scale = 100.0 / total
    return {symbol: value * scale for symbol, value in values.items()}, cash_pct * scale


def apply_event(
    holdings: Dict[str, float],
    cash_pct: float,
    event: Dict[str, Any],
) -> Tuple[Dict[str, float], float]:
    next_holdings = dict(holdings)
    changed = set()
    for row in event["histories"]:
        ts_code = raw_to_ts_code(row.get("stock_symbol"))
        if not ts_code:
            continue
        target_weight = safe_float(row.get("target_weight"), safe_float(row.get("weight"), 0.0)) or 0.0
        changed.add(ts_code)
        if target_weight > 1e-8:
            next_holdings[ts_code] = target_weight
        else:
            next_holdings.pop(ts_code, None)

    event_cash = min(max(event.get("cash_pct", cash_pct), 0.0), 100.0)
    target_stock_total = max(100.0 - event_cash, 0.0)
    changed_sum = sum(next_holdings.get(symbol, 0.0) for symbol in changed)
    unchanged = [symbol for symbol in list(next_holdings.keys()) if symbol not in changed]
    unchanged_sum = sum(next_holdings.get(symbol, 0.0) for symbol in unchanged)
    remaining = target_stock_total - changed_sum

    if unchanged and unchanged_sum > 1e-9:
        factor = max(remaining, 0.0) / unchanged_sum
        for symbol in unchanged:
            next_holdings[symbol] *= factor
    elif changed_sum > 1e-9 and abs(changed_sum - target_stock_total) > 1e-6:
        factor = target_stock_total / changed_sum
        for symbol in list(next_holdings.keys()):
            next_holdings[symbol] *= factor

    next_holdings = {
        symbol: weight for symbol, weight in next_holdings.items()
        if weight > 1e-6 and math.isfinite(weight)
    }
    stock_total = sum(next_holdings.values())
    if stock_total > 100.0 and stock_total > 0:
        factor = target_stock_total / stock_total if target_stock_total > 0 else 0.0
        next_holdings = {symbol: weight * factor for symbol, weight in next_holdings.items() if weight * factor > 1e-6}
        stock_total = sum(next_holdings.values())
    return next_holdings, max(0.0, 100.0 - stock_total)


def reconstruct_daily_aggregates(
    histories: List[Dict[str, Any]],
    returns: pd.DataFrame,
    *,
    start_date: date,
) -> Tuple[Dict[date, Dict[str, float]], Dict[date, int], Dict[date, Dict[str, int]]]:
    trading_days = list(returns.index)
    trading_day_set = set(trading_days)
    aggregate: Dict[date, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    active_counts: Dict[date, int] = defaultdict(int)
    holding_counts: Dict[date, Dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for history_index, history in enumerate(histories, start=1):
        events_by_day: Dict[date, List[Dict[str, Any]]] = defaultdict(list)
        for event in iter_events(history):
            effective = effective_event_date(event["event_date"], trading_days)
            if effective is not None:
                events_by_day[effective].append(event)
        if not events_by_day:
            continue
        for event_list in events_by_day.values():
            event_list.sort(key=lambda item: (item["created_at"], item["id"]))

        first_day = min(events_by_day)
        holdings: Dict[str, float] = {}
        cash_pct = 100.0
        active = False
        previous_day: Optional[date] = None

        for day in trading_days[bisect_left(trading_days, first_day):]:
            if previous_day is not None and active:
                holdings, cash_pct = drift_holdings(holdings, cash_pct, returns.loc[day])
            for event in events_by_day.get(day, []):
                holdings, cash_pct = apply_event(holdings, cash_pct, event)
                active = True
            if day >= start_date and active and holdings:
                active_counts[day] += 1
                for symbol, weight in holdings.items():
                    aggregate[day][symbol] += weight
                    holding_counts[day][symbol] += 1
            previous_day = day

        if history_index % 100 == 0 or history_index == len(histories):
            print(f"reconstructed {history_index}/{len(histories)} cubes", flush=True)
    return aggregate, active_counts, holding_counts


def build_daily_targets(
    aggregate: Dict[date, Dict[str, float]],
    active_counts: Dict[date, int],
    holding_counts: Dict[date, Dict[str, int]],
    *,
    top_n: int,
) -> Tuple[Dict[date, Dict[str, float]], List[Dict[str, Any]]]:
    targets: Dict[date, Dict[str, float]] = {}
    rows: List[Dict[str, Any]] = []
    for day in sorted(aggregate):
        ranking = sorted(aggregate[day].items(), key=lambda item: item[1], reverse=True)[:top_n]
        top_total = sum(weight for _, weight in ranking)
        if top_total <= 0:
            continue
        target = {symbol: weight / top_total * 100.0 for symbol, weight in ranking}
        targets[day] = target
        active = active_counts.get(day, 0)
        for rank, (symbol, weight_sum) in enumerate(ranking, start=1):
            rows.append(
                {
                    "date": day.isoformat(),
                    "rank": rank,
                    "symbol": symbol,
                    "xueqiu_symbol": ts_to_raw_symbol(symbol),
                    "aggregate_weight_pct": weight_sum / active if active else None,
                    "target_weight_pct": target[symbol],
                    "holding_cube_count": holding_counts[day].get(symbol, 0),
                    "active_cube_count": active,
                }
            )
    return targets, rows


def normalize_weights(weights: Dict[str, float]) -> Dict[str, float]:
    total = sum(max(weight, 0.0) for weight in weights.values())
    if total <= 0:
        return {}
    return {symbol: weight / total * 100.0 for symbol, weight in weights.items() if weight > 1e-9}


def post_return_weights(weights: Dict[str, float], returns: pd.Series) -> Dict[str, float]:
    values = {
        symbol: weight * (1.0 + (safe_float(returns.get(symbol), 0.0) or 0.0))
        for symbol, weight in weights.items()
    }
    return normalize_weights(values)


def one_way_turnover(current: Dict[str, float], target: Dict[str, float]) -> float:
    symbols = set(current) | set(target)
    return sum(abs(current.get(symbol, 0.0) - target.get(symbol, 0.0)) for symbol in symbols) / 2.0


def compute_metrics(curve: pd.DataFrame, nav_col: str) -> Dict[str, Any]:
    if curve.empty:
        return {}
    nav = pd.to_numeric(curve[nav_col], errors="coerce").dropna()
    if len(nav) < 2:
        return {}
    daily = nav.pct_change().dropna()
    elapsed_days = (pd.to_datetime(curve["date"].iloc[-1]).date() - pd.to_datetime(curve["date"].iloc[0]).date()).days
    total_return = nav.iloc[-1] / nav.iloc[0] - 1.0
    annualized = (nav.iloc[-1] / nav.iloc[0]) ** (365.0 / elapsed_days) - 1.0 if elapsed_days > 0 else 0.0
    volatility = daily.std(ddof=0) * math.sqrt(252.0) if not daily.empty else 0.0
    sharpe = (daily.mean() / daily.std(ddof=0) * math.sqrt(252.0)) if len(daily) > 1 and daily.std(ddof=0) > 0 else None
    drawdown = nav / nav.cummax() - 1.0
    max_drawdown = drawdown.min()
    calmar = annualized / abs(max_drawdown) if max_drawdown < 0 else None
    return {
        "start_date": str(curve["date"].iloc[0]),
        "end_date": str(curve["date"].iloc[-1]),
        "elapsed_days": elapsed_days,
        "trading_days": int(len(nav)),
        "total_return_pct": total_return * 100.0,
        "annualized_return_pct": annualized * 100.0,
        "annualized_volatility_pct": volatility * 100.0,
        "sharpe": sharpe,
        "max_drawdown_pct": max_drawdown * 100.0,
        "calmar": calmar,
        "daily_win_rate_pct": float((daily > 0).mean() * 100.0) if not daily.empty else None,
    }


def backtest_targets(
    targets: Dict[date, Dict[str, float]],
    returns: pd.DataFrame,
    benchmark_close: pd.DataFrame,
    *,
    slippage_pct: float,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    dates = [day for day in sorted(targets) if day in returns.index]
    if len(dates) < 2:
        raise RuntimeError("Not enough daily targets to backtest")

    nav_raw = 1.0
    nav_slip = 1.0
    current_weights = targets[dates[0]]
    nav_slip *= 1.0 - (sum(current_weights.values()) / 100.0) * (slippage_pct / 100.0)
    total_slippage_cost = (1.0 - nav_slip) * 100.0
    rows = [
        {
            "date": dates[0].isoformat(),
            "raw_nav": nav_raw,
            "slippage_nav": nav_slip,
            "daily_return_pct": 0.0,
            "slippage_cost_pct": total_slippage_cost,
            "turnover_pct": 100.0,
        }
    ]

    for day in dates[1:]:
        ret_row = returns.loc[day]
        daily_return = sum((weight / 100.0) * (safe_float(ret_row.get(symbol), 0.0) or 0.0) for symbol, weight in current_weights.items())
        nav_raw *= 1.0 + daily_return
        nav_slip *= 1.0 + daily_return

        drifted = post_return_weights(current_weights, ret_row)
        target = targets[day]
        turnover = one_way_turnover(drifted, target)
        cost_rate = (turnover / 100.0) * (slippage_pct / 100.0)
        nav_slip *= 1.0 - cost_rate
        total_slippage_cost += cost_rate * 100.0

        rows.append(
            {
                "date": day.isoformat(),
                "raw_nav": nav_raw,
                "slippage_nav": nav_slip,
                "daily_return_pct": daily_return * 100.0,
                "slippage_cost_pct": cost_rate * 100.0,
                "turnover_pct": turnover,
            }
        )
        current_weights = target

    curve = pd.DataFrame(rows)
    bench = benchmark_close.copy()
    bench = bench.loc[[day for day in dates if day in bench.index]].copy()
    if not bench.empty:
        bench_nav = bench["close"] / bench["close"].iloc[0]
        bench_map = {day.isoformat(): value for day, value in bench_nav.items()}
        curve["benchmark_nav"] = curve["date"].map(bench_map)
    else:
        curve["benchmark_nav"] = None

    summary = {
        "raw": compute_metrics(curve, "raw_nav"),
        "after_slippage": compute_metrics(curve, "slippage_nav"),
        "benchmark": compute_metrics(curve.dropna(subset=["benchmark_nav"]).reset_index(drop=True), "benchmark_nav"),
        "total_slippage_cost_pct": total_slippage_cost,
    }
    return curve, summary


def monthly_returns(curve: pd.DataFrame, nav_col: str) -> List[Dict[str, Any]]:
    df = curve[["date", nav_col]].copy()
    df["date"] = pd.to_datetime(df["date"])
    df["month"] = df["date"].dt.strftime("%Y-%m")
    rows = []
    for month, group in df.groupby("month"):
        if len(group) < 2:
            continue
        ret = group[nav_col].iloc[-1] / group[nav_col].iloc[0] - 1.0
        rows.append({"month": month, "return_pct": ret * 100.0})
    return rows


def render_report(summary: Dict[str, Any], latest_top: List[Dict[str, Any]], monthly: List[Dict[str, Any]]) -> str:
    raw = summary["performance"]["raw"]
    slip = summary["performance"]["after_slippage"]
    bench = summary["performance"]["benchmark"]

    def fmt(value: Any, suffix: str = "", digits: int = 2) -> str:
        number = safe_float(value)
        if number is None:
            return "N/A"
        return f"{number:.{digits}f}{suffix}"

    lines = [
        "# 雪球年榜Top1000综合持仓Top10策略回测",
        "",
        f"- 回测区间：{summary['start_date']} 至 {summary['end_date']}",
        f"- 组合数：{summary['cube_count']}，历史调仓成功缓存：{summary['history_success_count']}，失败：{summary['history_failed_count']}",
        f"- 口径：每日还原年榜1000组合的收盘后持仓，按个股综合权重取Top10，并在Top10内归一化到100%。",
        f"- 交易假设：使用第T日收盘后生成的Top10目标，持有到下一交易日；滑点列按单边 {summary['slippage_pct']}% 计算调仓成本。",
        "",
        "## 绩效",
        "",
        "| 指标 | 原始 | 滑点后 | 中证500 |",
        "| --- | ---: | ---: | ---: |",
        f"| 总收益 | {fmt(raw.get('total_return_pct'), '%')} | {fmt(slip.get('total_return_pct'), '%')} | {fmt(bench.get('total_return_pct'), '%')} |",
        f"| 年化 | {fmt(raw.get('annualized_return_pct'), '%')} | {fmt(slip.get('annualized_return_pct'), '%')} | {fmt(bench.get('annualized_return_pct'), '%')} |",
        f"| 夏普 | {fmt(raw.get('sharpe'))} | {fmt(slip.get('sharpe'))} | {fmt(bench.get('sharpe'))} |",
        f"| 最大回撤 | {fmt(raw.get('max_drawdown_pct'), '%')} | {fmt(slip.get('max_drawdown_pct'), '%')} | {fmt(bench.get('max_drawdown_pct'), '%')} |",
        f"| Calmar | {fmt(raw.get('calmar'))} | {fmt(slip.get('calmar'))} | {fmt(bench.get('calmar'))} |",
        "",
        "## 最近一日Top10",
        "",
        "| 排名 | 股票 | 名称 | 综合权重 | 目标权重 | 持有组合数 | 活跃组合数 |",
        "| ---: | --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for row in latest_top:
        lines.append(
            "| {rank} | {xueqiu_symbol} | {stock_name} | {agg} | {target} | {cube_count} | {active} |".format(
                rank=row.get("rank"),
                xueqiu_symbol=row.get("xueqiu_symbol"),
                stock_name=row.get("stock_name") or "",
                agg=fmt(row.get("aggregate_weight_pct"), "%"),
                target=fmt(row.get("target_weight_pct"), "%"),
                cube_count=row.get("holding_cube_count"),
                active=row.get("active_cube_count"),
            )
        )
    lines.extend([
        "",
        "## 月度收益（滑点后）",
        "",
        "| 月份 | 收益 |",
        "| --- | ---: |",
    ])
    for row in monthly:
        lines.append(f"| {row['month']} | {fmt(row['return_pct'], '%')} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list-path", type=Path, default=DEFAULT_LIST_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--sqlite-db", type=Path, default=DEFAULT_SQLITE_DB)
    parser.add_argument("--analytics-db", type=Path, default=DEFAULT_ANALYTICS_DB)
    parser.add_argument("--start-date", default="2026-01-01")
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--slippage", type=float, default=0.5)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--max-pages", type=int, default=50)
    parser.add_argument("--refresh-history", action="store_true")
    args = parser.parse_args()

    start_date = parse_date(args.start_date)
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = output_dir / "rebalance_history_cache"

    cubes = load_json(args.list_path, [])
    if not cubes:
        raise RuntimeError(f"No cubes loaded from {args.list_path}")
    cookie = get_cookie(args.sqlite_db)
    histories, failures = load_histories(
        cubes,
        cookie=cookie,
        cache_dir=cache_dir,
        refresh=args.refresh_history,
        workers=args.workers,
        timeout=args.timeout,
        max_pages=args.max_pages,
    )

    symbols, name_map, min_event_date = collect_symbols_and_names(histories)
    if not symbols or min_event_date is None:
        raise RuntimeError("No usable rebalance histories")
    price_start = min(min_event_date, start_date)
    stock_close, stock_returns = load_price_data(
        args.analytics_db,
        symbols,
        start=price_start,
        end=parse_date(args.end_date) if args.end_date else date(2099, 12, 31),
    )
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
            [price_start, stock_returns.index[-1]],
        ).fetchdf()
    finally:
        con.close()
    bench_df["trade_date"] = pd.to_datetime(bench_df["trade_date"]).dt.date
    bench_df = bench_df.set_index("trade_date").sort_index()

    if args.end_date:
        end_date = parse_date(args.end_date)
        stock_returns = stock_returns.loc[stock_returns.index <= end_date]
        bench_df = bench_df.loc[bench_df.index <= end_date]
    end_date = stock_returns.index[-1]

    aggregate, active_counts, holding_counts = reconstruct_daily_aggregates(
        histories,
        stock_returns,
        start_date=start_date,
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
        "start_date": curve["date"].iloc[0],
        "end_date": curve["date"].iloc[-1],
        "top_n": args.top_n,
        "slippage_pct": args.slippage,
        "cube_count": len(cubes),
        "history_success_count": len(histories),
        "history_failed_count": len(failures),
        "history_failures": failures,
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
    report = render_report(summary, latest_top, monthly)

    curve.to_csv(output_dir / "strategy_curve.csv", index=False)
    pd.DataFrame(top_rows).to_csv(output_dir / "daily_top10.csv", index=False)
    pd.DataFrame(monthly).to_csv(output_dir / "monthly_returns.csv", index=False)
    write_json(output_dir / "summary.json", summary)
    (output_dir / "report.md").write_text(report, encoding="utf-8")

    print(json.dumps(json_safe(summary), ensure_ascii=False, indent=2))
    print(f"Saved report: {output_dir / 'report.md'}")


if __name__ == "__main__":
    main()

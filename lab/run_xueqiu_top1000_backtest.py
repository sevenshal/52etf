#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd


ROOT = Path(__file__).resolve().parents[1] / "backend"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.database import SessionLocal, SnowballAccountConfig  # noqa: E402
from src.core.services.snowball_backtest import (  # noqa: E402
    build_curve_points,
    build_performance_comparison,
    compute_performance_metrics,
    merge_yearly_returns,
    run_snowball_cube_backtest,
)


DEFAULT_OUTPUT_DIR = (
    ROOT
    / "lab"
    / "output"
    / "xueqiu_year_top1000_20260525_slippage_0_5"
)
DEFAULT_LIST_PATH = DEFAULT_OUTPUT_DIR / "top1000_list.json"
DEFAULT_START_DATE = date(2000, 1, 1)
DEFAULT_END_DATE = date(2026, 5, 25)
CSV_FIELDS = [
    "year_rank",
    "symbol",
    "cube_name",
    "screen_name",
    "cube_follower_count",
    "cube_age_days",
    "status",
    "error",
    "sharpe_bucket",
    "xueqiu_year_gain_pct",
    "actual_nav_start",
    "actual_nav_end",
    "rebalance_count",
    "avg_monthly_rebalances",
    "raw_total_return_pct",
    "slippage_total_return_pct",
    "benchmark_total_return_pct",
    "excess_after_slippage_pct",
    "raw_annualized_return_pct",
    "slippage_annualized_return_pct",
    "benchmark_annualized_return_pct",
    "raw_max_drawdown_pct",
    "slippage_max_drawdown_pct",
    "benchmark_max_drawdown_pct",
    "raw_sharpe",
    "slippage_sharpe",
    "benchmark_sharpe",
    "raw_calmar",
    "slippage_calmar",
    "total_slippage_cost_pct",
    "ending_nav_drag_pct",
    "effective_start_date",
    "oldest_rebalance_date",
    "page_limit_hit",
    "pages_fetched",
]


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def get_cookie() -> str:
    db = SessionLocal()
    try:
        rows = (
            db.query(SnowballAccountConfig)
            .filter(SnowballAccountConfig.xueqiu_cookie.isnot(None))
            .all()
        )
        for row in rows:
            cookie = (row.xueqiu_cookie or "").strip()
            if "xq_a_token=" in cookie:
                return cookie
        for row in rows:
            cookie = (row.xueqiu_cookie or "").strip()
            if cookie:
                return f"xq_a_token={cookie};"
    finally:
        db.close()
    raise RuntimeError("No Xueqiu cookie found in snowball_account_configs.")


def parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    return datetime.strptime(value[:10], "%Y-%m-%d").date()


def elapsed_months(start: Optional[str], end: Optional[str]) -> Optional[int]:
    start_date = parse_date(start)
    end_date = parse_date(end)
    if not start_date or not end_date:
        return None
    return max((end_date.year - start_date.year) * 12 + end_date.month - start_date.month + 1, 1)


def safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def safe_int(value: Any) -> Optional[int]:
    number = safe_float(value)
    if number is None:
        return None
    return int(number)


def sharpe_bucket(value: Any) -> Optional[int]:
    number = safe_float(value)
    if number is None:
        return None
    return math.floor(number)


def get_metric(metrics: Optional[Dict[str, Any]], key: str) -> Optional[float]:
    if not metrics:
        return None
    return safe_float(metrics.get(key))


def result_dir(output_dir: Path, symbol: str) -> Path:
    return output_dir / symbol


def existing_result(output_dir: Path, symbol: str) -> Optional[Dict[str, Any]]:
    summary_path = result_dir(output_dir, symbol) / "performance_summary.json"
    if not summary_path.exists():
        return None
    try:
        return json.loads(summary_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def write_result_files(output_dir: Path, symbol: str, result: Dict[str, Any]) -> None:
    target_dir = result_dir(output_dir, symbol)
    target_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(target_dir / "performance_summary.json", result)
    pd.DataFrame(result.get("curve_points") or []).to_csv(
        target_dir / "curve_points.csv",
        index=False,
    )
    pd.DataFrame(result.get("yearly_returns") or []).to_csv(
        target_dir / "yearly_returns.csv",
        index=False,
    )


def row_from_result(info: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    raw = result.get("performance_raw") or {}
    slip = result.get("performance_after_slippage") or {}
    bench = result.get("benchmark_metrics") or {}
    rebalancing = result.get("rebalancing") or {}
    rebalance_fetch = result.get("rebalance_fetch") or {}
    slippage = result.get("slippage") or {}
    comparison = result.get("comparison") or {}
    rebalance_count = int(rebalancing.get("rebalance_count") or 0)
    months = elapsed_months(raw.get("start_date"), raw.get("end_date"))
    slip_sharpe = get_metric(slip, "sharpe")

    return {
        "year_rank": info.get("year_rank"),
        "symbol": info.get("symbol"),
        "cube_name": result.get("cube_name") or info.get("cube_name"),
        "screen_name": info.get("screen_name"),
        "cube_follower_count": safe_int(
            info.get("cube_follower_count")
            if info.get("cube_follower_count") is not None
            else info.get("follower_count")
        ),
        "cube_age_days": safe_int(raw.get("elapsed_days")),
        "status": "SUCCESS",
        "error": "",
        "sharpe_bucket": sharpe_bucket(slip_sharpe),
        "xueqiu_year_gain_pct": safe_float(info.get("year_gain")),
        "actual_nav_start": raw.get("start_date"),
        "actual_nav_end": raw.get("end_date"),
        "rebalance_count": rebalance_count,
        "avg_monthly_rebalances": (rebalance_count / months if months else None),
        "raw_total_return_pct": get_metric(raw, "total_return_pct"),
        "slippage_total_return_pct": get_metric(slip, "total_return_pct"),
        "benchmark_total_return_pct": get_metric(bench, "total_return_pct"),
        "excess_after_slippage_pct": (
            get_metric(slip, "total_return_pct") - get_metric(bench, "total_return_pct")
            if get_metric(slip, "total_return_pct") is not None
            and get_metric(bench, "total_return_pct") is not None
            else None
        ),
        "raw_annualized_return_pct": get_metric(raw, "annualized_return_pct"),
        "slippage_annualized_return_pct": get_metric(slip, "annualized_return_pct"),
        "benchmark_annualized_return_pct": get_metric(bench, "annualized_return_pct"),
        "raw_max_drawdown_pct": get_metric(raw, "max_drawdown_pct"),
        "slippage_max_drawdown_pct": get_metric(slip, "max_drawdown_pct"),
        "benchmark_max_drawdown_pct": get_metric(bench, "max_drawdown_pct"),
        "raw_sharpe": get_metric(raw, "sharpe"),
        "slippage_sharpe": slip_sharpe,
        "benchmark_sharpe": get_metric(bench, "sharpe"),
        "raw_calmar": get_metric(raw, "calmar"),
        "slippage_calmar": get_metric(slip, "calmar"),
        "total_slippage_cost_pct": safe_float(slippage.get("total_slippage_cost_pct")),
        "ending_nav_drag_pct": safe_float(
            comparison.get("ending_nav_ratio_after_slippage_vs_raw_pct")
        )
        if "ending_nav_ratio_after_slippage_vs_raw_pct" in comparison
        else safe_float(slippage.get("ending_nav_drag_pct")),
        "effective_start_date": result.get("effective_start_date"),
        "oldest_rebalance_date": rebalance_fetch.get("oldest_fetched_date"),
        "page_limit_hit": rebalance_fetch.get("page_limit_hit"),
        "pages_fetched": rebalance_fetch.get("pages_fetched"),
    }


def row_from_failure(info: Dict[str, Any], error: str) -> Dict[str, Any]:
    row = {field: None for field in CSV_FIELDS}
    row.update(
        {
            "year_rank": info.get("year_rank"),
            "symbol": info.get("symbol"),
            "cube_name": info.get("cube_name"),
            "screen_name": info.get("screen_name"),
            "cube_follower_count": safe_int(
                info.get("cube_follower_count")
                if info.get("cube_follower_count") is not None
                else info.get("follower_count")
            ),
            "cube_age_days": None,
            "status": "FAILED",
            "error": error,
            "xueqiu_year_gain_pct": safe_float(info.get("year_gain")),
        }
    )
    return row


def sort_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    def key(row: Dict[str, Any]) -> tuple:
        if row.get("status") != "SUCCESS":
            return (-999999, -math.inf, -math.inf, -int(row.get("year_rank") or 0))
        bucket = row.get("sharpe_bucket")
        ann = safe_float(row.get("slippage_annualized_return_pct"))
        total = safe_float(row.get("slippage_total_return_pct"))
        return (
            bucket if bucket is not None else -999999,
            ann if ann is not None else -math.inf,
            total if total is not None else -math.inf,
            -int(row.get("year_rank") or 0),
        )

    return sorted(rows, key=key, reverse=True)


def format_number(value: Any, digits: int = 2, suffix: str = "") -> str:
    number = safe_float(value)
    if number is None:
        return "-"
    return f"{number:.{digits}f}{suffix}"


def format_integer(value: Any) -> str:
    number = safe_int(value)
    if number is None:
        return "-"
    return str(number)


def md_escape(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|")


def build_rows(items: List[Dict[str, Any]], output_dir: Path, state: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for info in items:
        symbol = info.get("symbol")
        if not symbol:
            continue
        result = existing_result(output_dir, symbol)
        if result:
            rows.append(row_from_result(info, result))
            continue
        state_item = state.get(symbol) or {}
        if state_item.get("status") == "FAILED":
            rows.append(row_from_failure(info, state_item.get("error") or "unknown error"))
        else:
            rows.append(row_from_failure(info, "not completed"))
    return rows


def write_summary_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in CSV_FIELDS})


def write_report(
    path: Path,
    rows: List[Dict[str, Any]],
    *,
    slippage_pct: float,
    start_date: date,
    end_date: date,
) -> None:
    success_rows = [row for row in rows if row.get("status") == "SUCCESS"]
    failed_rows = [row for row in rows if row.get("status") != "SUCCESS"]
    bucket_counts: Dict[Any, int] = {}
    for row in success_rows:
        bucket = row.get("sharpe_bucket")
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "# 雪球年榜前1000组合回测汇总",
        "",
        "- 抓取榜单：雪球组合年榜前1000",
        f"- 更新时间：{now}",
        f"- 请求回测起点：{start_date.isoformat()}，实际起点以雪球最早净值为准",
        f"- 回测结束：{end_date.isoformat()}",
        f"- 单边滑点：{slippage_pct:.2f}%",
        "- 基准：中证500（000905.SH）",
        "- 排序：先按滑点后夏普值向下取整分档降序，再按同档滑点后年化收益率降序",
        f"- 成功：{len(success_rows)}，失败/未完成：{len(failed_rows)}",
        "",
        "## 口径说明",
        "",
        "- 原始曲线：雪球官方组合净值曲线。",
        "- 滑点后曲线：在雪球官方净值曲线基础上，按每次调仓的总换手率扣减单边滑点成本。",
        f"- 单次调仓成本：`sum(abs(target_weight - previous_weight)) * {slippage_pct:.2f}%`。",
        "- 多次调仓成本：按每日成本生成累计折扣乘数，`滑点后净值 = 原始净值 * 累计乘数`。",
        "- 本文排序和分档均使用“滑点后夏普”；夏普分档为向下取整。",
        "- 加自选人数：雪球组合详情接口返回的 `follower_count`。",
        "- 成立天数：按组合实际可用净值起点到本次回测结束日的自然日天数计算。",
        "",
        "## 夏普分档统计",
        "",
        "| 夏普分档 | 数量 |",
        "| ---: | ---: |",
    ]
    for bucket, count in sorted(
        bucket_counts.items(),
        key=lambda item: item[0] if item[0] is not None else -999999,
        reverse=True,
    ):
        lines.append(f"| {bucket if bucket is not None else '-'} | {count} |")

    lines.extend(
        [
            "",
            "## 分档排序明细",
            "",
            "| 排名 | 夏普分档 | 年榜排名 | 组合 | 名称 | 创建者 | 加自选人数 | 成立天数 | 实际净值起点 | 调仓数 | 月均调仓 | 滑点后夏普 | 滑点后年化 | 原始总收益 | 滑点后总收益 | 中证500 | 滑点后最大回撤 |",
            "| ---: | ---: | ---: | --- | --- | --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for index, row in enumerate(success_rows, start=1):
        lines.append(
            "| "
            + " | ".join(
                [
                    str(index),
                    str(row.get("sharpe_bucket")) if row.get("sharpe_bucket") is not None else "-",
                    str(row.get("year_rank") or ""),
                    md_escape(row.get("symbol")),
                    md_escape(row.get("cube_name")),
                    md_escape(row.get("screen_name")),
                    format_integer(row.get("cube_follower_count")),
                    format_integer(row.get("cube_age_days")),
                    md_escape(row.get("actual_nav_start")),
                    str(row.get("rebalance_count") or 0),
                    format_number(row.get("avg_monthly_rebalances")),
                    format_number(row.get("slippage_sharpe")),
                    format_number(row.get("slippage_annualized_return_pct"), suffix="%"),
                    format_number(row.get("raw_total_return_pct"), suffix="%"),
                    format_number(row.get("slippage_total_return_pct"), suffix="%"),
                    format_number(row.get("benchmark_total_return_pct"), suffix="%"),
                    format_number(row.get("slippage_max_drawdown_pct"), suffix="%"),
                ]
            )
            + " |"
        )

    if failed_rows:
        lines.extend(
            [
                "",
                "## 失败或未完成",
                "",
                "| 年榜排名 | 组合 | 名称 | 错误 |",
                "| ---: | --- | --- | --- |",
            ]
        )
        for row in failed_rows:
            lines.append(
                f"| {row.get('year_rank') or ''} | {md_escape(row.get('symbol'))} | "
                f"{md_escape(row.get('cube_name'))} | {md_escape(row.get('error'))} |"
            )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def save_state_from_rows(state_path: Path, rows: List[Dict[str, Any]]) -> None:
    state: Dict[str, Any] = {}
    for row in rows:
        symbol = row.get("symbol")
        if not symbol:
            continue
        state[symbol] = {
            "status": row.get("status"),
            "error": row.get("error") or None,
            "year_rank": row.get("year_rank"),
            "slippage_sharpe": row.get("slippage_sharpe"),
            "slippage_annualized_return_pct": row.get("slippage_annualized_return_pct"),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
    write_json_atomic(state_path, state)


def recompute_slippage_result(result: Dict[str, Any], target_slippage_pct: float) -> Dict[str, Any]:
    curve_points = result.get("curve_points") or []
    if not curve_points:
        raise ValueError("Missing curve_points in source result")

    source_slippage_pct = safe_float((result.get("slippage") or {}).get("slippage_pct_per_side"))
    if source_slippage_pct is None or source_slippage_pct <= 0:
        raise ValueError("Missing positive source slippage_pct_per_side")

    scale = float(target_slippage_pct) / source_slippage_pct
    nav_df = pd.DataFrame(curve_points).copy()
    nav_df["date"] = pd.to_datetime(nav_df["date"])
    nav_df = nav_df.sort_values("date").reset_index(drop=True)
    nav_df["nav"] = pd.to_numeric(nav_df["raw_nav"], errors="coerce")
    nav_df["benchmark_nav"] = pd.to_numeric(nav_df.get("benchmark_nav"), errors="coerce")
    nav_df["benchmark_cumulative_return_pct"] = pd.to_numeric(
        nav_df.get("benchmark_return_pct"),
        errors="coerce",
    )
    nav_df["benchmark_drawdown_pct"] = pd.to_numeric(
        nav_df.get("benchmark_drawdown_pct"),
        errors="coerce",
    )
    nav_df["slippage_cost_pct"] = (
        pd.to_numeric(nav_df.get("slippage_cost_pct"), errors="coerce").fillna(0.0) * scale
    )
    nav_df["slippage_cost_rate"] = nav_df["slippage_cost_pct"] / 100.0
    nav_df["cumulative_return_pct"] = (nav_df["nav"] / nav_df["nav"].iloc[0] - 1.0) * 100.0
    nav_df["daily_return"] = nav_df["nav"].pct_change()
    nav_df["drawdown_pct"] = (nav_df["nav"] / nav_df["nav"].cummax() - 1.0) * 100.0
    nav_df["slippage_multiplier"] = 1.0 - nav_df["slippage_cost_rate"]
    nav_df["cumulative_slippage_multiplier"] = nav_df["slippage_multiplier"].cumprod()
    nav_df["nav_after_slippage"] = nav_df["nav"] * nav_df["cumulative_slippage_multiplier"]
    raw_start_nav = float(nav_df["nav"].iloc[0])
    nav_df["cumulative_return_after_slippage_pct"] = (
        nav_df["nav_after_slippage"] / raw_start_nav - 1.0
    ) * 100.0
    nav_df["daily_return_after_slippage"] = nav_df["nav_after_slippage"].pct_change()
    nav_df["drawdown_after_slippage_pct"] = (
        nav_df["nav_after_slippage"] / nav_df["nav_after_slippage"].cummax() - 1.0
    ) * 100.0

    performance_raw = result.get("performance_raw") or {}
    benchmark_metrics = result.get("benchmark_metrics")
    performance_after_slippage = compute_performance_metrics(
        nav_df,
        nav_col="nav_after_slippage",
        daily_return_col="daily_return_after_slippage",
        drawdown_col="drawdown_after_slippage_pct",
        reference_start_value=raw_start_nav,
    )
    comparison = build_performance_comparison(performance_raw, performance_after_slippage)

    source_slippage = result.get("slippage") or {}
    adjusted_end_nav = float(nav_df["nav_after_slippage"].iloc[-1])
    raw_end_nav = float(nav_df["nav"].iloc[-1])
    applied_rebalance_count = safe_int(source_slippage.get("applied_rebalance_count")) or 0
    total_slippage_cost_pct = float(nav_df["slippage_cost_pct"].sum())
    slippage_summary = dict(source_slippage)
    slippage_summary.update(
        {
            "slippage_pct_per_side": float(target_slippage_pct),
            "total_slippage_cost_pct": total_slippage_cost_pct,
            "average_slippage_cost_pct_per_rebalance": (
                total_slippage_cost_pct / applied_rebalance_count
                if applied_rebalance_count > 0
                else None
            ),
            "max_single_day_slippage_cost_pct": float(nav_df["slippage_cost_pct"].max()),
            "ending_nav_drag_pct": ((adjusted_end_nav / raw_end_nav) - 1.0) * 100.0
            if raw_end_nav
            else None,
            "ending_return_drag_pct_points": ((raw_end_nav - adjusted_end_nav) / raw_start_nav) * 100.0
            if raw_start_nav
            else None,
        }
    )

    new_result = json.loads(json.dumps(result, ensure_ascii=False))
    new_result["performance_after_slippage"] = performance_after_slippage
    new_result["slippage"] = slippage_summary
    new_result["comparison"] = comparison
    new_result["curve_points"] = build_curve_points(nav_df)
    new_result["yearly_returns"] = merge_yearly_returns(
        performance_raw,
        performance_after_slippage,
        benchmark_metrics,
    )
    return new_result


def recompute_slippage_outputs(
    items: List[Dict[str, Any]],
    *,
    source_dir: Path,
    output_dir: Path,
    slippage_pct: float,
) -> Dict[str, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(output_dir / "top1000_list.json", items)
    success = 0
    failed = 0
    for item in items:
        symbol = item.get("symbol")
        if not symbol:
            continue
        source_result = existing_result(source_dir, symbol)
        if not source_result:
            failed += 1
            continue
        try:
            result = recompute_slippage_result(source_result, slippage_pct)
            write_result_files(output_dir, symbol, result)
            success += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL recompute {symbol}: {exc}", flush=True)
    return {"success": success, "failed": failed}


def refresh_outputs(
    items: List[Dict[str, Any]],
    output_dir: Path,
    state_path: Path,
    *,
    slippage_pct: float,
    start_date: date,
    end_date: date,
) -> Dict[str, int]:
    state = load_json(state_path, {})
    rows = sort_rows(build_rows(items, output_dir, state))
    write_summary_csv(output_dir / "sharpe_bucket_summary.csv", rows)
    write_report(
        output_dir / "sharpe_bucket_report.md",
        rows,
        slippage_pct=slippage_pct,
        start_date=start_date,
        end_date=end_date,
    )
    save_state_from_rows(state_path, rows)
    success_count = sum(1 for row in rows if row.get("status") == "SUCCESS")
    failed_count = len(rows) - success_count
    return {"success": success_count, "failed_or_pending": failed_count}


def run_one(
    info: Dict[str, Any],
    *,
    cookie: str,
    output_dir: Path,
    slippage_pct: float,
    start_date: date,
    end_date: date,
    retries: int,
) -> Dict[str, Any]:
    symbol = info["symbol"]
    last_error: Optional[BaseException] = None
    for attempt in range(1, retries + 1):
        try:
            result = run_snowball_cube_backtest(
                cube_symbol=symbol,
                cookie=cookie,
                slippage_pct=slippage_pct,
                start_date=start_date,
                end_date=end_date,
                timeout=30.0,
            )
            write_result_files(output_dir, symbol, result)
            row = row_from_result(info, result)
            return {"status": "SUCCESS", "row": row, "attempt": attempt}
        except BaseException as exc:  # noqa: BLE001
            last_error = exc
            if attempt < retries:
                time.sleep(1.2 * attempt)
    error = repr(last_error)
    return {"status": "FAILED", "row": row_from_failure(info, error), "attempt": retries}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Xueqiu year top1000 cube backtests.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--list-path", type=Path, default=DEFAULT_LIST_PATH)
    parser.add_argument("--slippage", type=float, default=0.5)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--pause", type=float, default=0.5)
    parser.add_argument("--start-rank", type=int, default=1)
    parser.add_argument("--end-rank", type=int, default=1000)
    parser.add_argument("--refresh-only", action="store_true")
    parser.add_argument(
        "--recompute-slippage-from-output-dir",
        type=Path,
        default=None,
        help="Reuse existing backtest curve/cost outputs from this directory and recompute only slippage.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / "batch_state.json"
    items: List[Dict[str, Any]] = load_json(args.list_path, [])
    if not items:
        raise RuntimeError(f"No ranking items found: {args.list_path}")

    item_by_symbol = {item["symbol"]: item for item in items if item.get("symbol")}
    selected = [
        item
        for item in items
        if args.start_rank <= int(item.get("year_rank") or 0) <= args.end_rank
    ]

    if args.recompute_slippage_from_output_dir:
        recompute_status = recompute_slippage_outputs(
            items,
            source_dir=args.recompute_slippage_from_output_dir,
            output_dir=output_dir,
            slippage_pct=args.slippage,
        )
        print(
            f"RECOMPUTE success={recompute_status['success']} failed={recompute_status['failed']}",
            flush=True,
        )
        final_status = refresh_outputs(
            items,
            output_dir,
            state_path,
            slippage_pct=args.slippage,
            start_date=DEFAULT_START_DATE,
            end_date=DEFAULT_END_DATE,
        )
        print(
            f"FINAL success={final_status['success']} "
            f"failed_or_pending={final_status['failed_or_pending']}",
            flush=True,
        )
        return

    status = refresh_outputs(
        items,
        output_dir,
        state_path,
        slippage_pct=args.slippage,
        start_date=DEFAULT_START_DATE,
        end_date=DEFAULT_END_DATE,
    )
    print(f"REFRESH success={status['success']} failed_or_pending={status['failed_or_pending']}", flush=True)
    if args.refresh_only:
        return

    to_run = [
        item
        for item in selected
        if not existing_result(output_dir, item["symbol"])
    ]
    print(f"TODO {len(to_run)} of selected {len(selected)}", flush=True)
    if not to_run:
        return

    cookie = get_cookie()
    lock = threading.Lock()
    completed = 0
    last_refresh = 0
    rows_by_symbol = {
        row["symbol"]: row
        for row in build_rows(items, output_dir, load_json(state_path, {}))
        if row.get("symbol")
    }

    def persist_progress(force: bool = False) -> None:
        nonlocal last_refresh
        if force or completed - last_refresh >= 10:
            sorted_rows = sort_rows(
                rows_by_symbol.get(item["symbol"]) or row_from_failure(item, "not completed")
                for item in items
                if item.get("symbol")
            )
            write_summary_csv(output_dir / "sharpe_bucket_summary.csv", sorted_rows)
            write_report(
                output_dir / "sharpe_bucket_report.md",
                sorted_rows,
                slippage_pct=args.slippage,
                start_date=DEFAULT_START_DATE,
                end_date=DEFAULT_END_DATE,
            )
            save_state_from_rows(state_path, sorted_rows)
            last_refresh = completed

    if args.workers <= 1:
        for item in to_run:
            symbol = item["symbol"]
            print(f"RUN {int(item.get('year_rank')):04d} {symbol} {item.get('cube_name')}", flush=True)
            result = run_one(
                item,
                cookie=cookie,
                output_dir=output_dir,
                slippage_pct=args.slippage,
                start_date=DEFAULT_START_DATE,
                end_date=DEFAULT_END_DATE,
                retries=args.retries,
            )
            row = result["row"]
            completed += 1
            rows_by_symbol[symbol] = row
            slip_sharpe = format_number(row.get("slippage_sharpe"))
            ann = format_number(row.get("slippage_annualized_return_pct"), suffix="%")
            if result["status"] == "SUCCESS":
                print(
                    f"DONE {int(item.get('year_rank')):04d} {symbol} "
                    f"sharpe={slip_sharpe} ann={ann} progress={completed}/{len(to_run)}",
                    flush=True,
                )
            else:
                print(
                    f"FAIL {int(item.get('year_rank')):04d} {symbol} "
                    f"{row.get('error')} progress={completed}/{len(to_run)}",
                    flush=True,
                )
            persist_progress()
            if args.pause > 0:
                time.sleep(args.pause)
    else:
        with ThreadPoolExecutor(max_workers=max(args.workers, 1)) as executor:
            future_map = {
                executor.submit(
                    run_one,
                    item,
                    cookie=cookie,
                    output_dir=output_dir,
                    slippage_pct=args.slippage,
                    start_date=DEFAULT_START_DATE,
                    end_date=DEFAULT_END_DATE,
                    retries=args.retries,
                ): item
                for item in to_run
            }
            for future in as_completed(future_map):
                item = future_map[future]
                symbol = item["symbol"]
                with lock:
                    completed += 1
                try:
                    result = future.result()
                except BaseException as exc:  # noqa: BLE001
                    result = {
                        "status": "FAILED",
                        "row": row_from_failure(item, repr(exc)),
                        "attempt": args.retries,
                    }
                row = result["row"]
                with lock:
                    rows_by_symbol[symbol] = row
                    slip_sharpe = format_number(row.get("slippage_sharpe"))
                    ann = format_number(row.get("slippage_annualized_return_pct"), suffix="%")
                    if result["status"] == "SUCCESS":
                        print(
                            f"DONE {int(item.get('year_rank')):04d} {symbol} "
                            f"sharpe={slip_sharpe} ann={ann} progress={completed}/{len(to_run)}",
                            flush=True,
                        )
                    else:
                        print(
                            f"FAIL {int(item.get('year_rank')):04d} {symbol} "
                            f"{row.get('error')} progress={completed}/{len(to_run)}",
                            flush=True,
                        )
                    persist_progress()

    with lock:
        persist_progress(force=True)
    final_status = refresh_outputs(
        items,
        output_dir,
        state_path,
        slippage_pct=args.slippage,
        start_date=DEFAULT_START_DATE,
        end_date=DEFAULT_END_DATE,
    )
    print(
        f"FINAL success={final_status['success']} "
        f"failed_or_pending={final_status['failed_or_pending']}",
        flush=True,
    )

    # Touch every item once so a missing symbol in the source list is obvious in review.
    missing = [symbol for symbol in item_by_symbol if symbol not in rows_by_symbol]
    if missing:
        print(f"WARN missing rows: {len(missing)}", flush=True)


if __name__ == "__main__":
    main()

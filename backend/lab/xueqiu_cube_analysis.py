#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import time as time_module
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
import requests


SH_TZ = ZoneInfo("Asia/Shanghai")
MAX_REBALANCE_PAGES = 50
REBALANCE_COUNT_CANDIDATES = (50, 40, 30, 25, 20)
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/148.0.0.0 Safari/537.36"
)


@dataclass
class AnalysisArtifacts:
    output_dir: Path
    details_csv: Path
    events_csv: Path
    nav_csv: Path
    slippage_schedule_csv: Path
    summary_json: Path
    report_md: Path


@dataclass
class RebalanceFetchResult:
    items: List[Dict[str, Any]]
    page_size: int
    total_expected: Optional[int]
    page_limit_hit: bool
    oldest_fetched_date: Optional[date]
    pages_fetched: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch Xueqiu cube rebalance history and performance summary."
    )
    parser.add_argument("--cube-symbol", required=True, help="Cube symbol, e.g. ZH1189922")
    parser.add_argument(
        "--start-date",
        default="20160101",
        help="Requested start date in YYYYMMDD format. Default: 20160101",
    )
    parser.add_argument(
        "--end-date",
        default=datetime.now(SH_TZ).strftime("%Y%m%d"),
        help="Requested end date in YYYYMMDD format. Default: today in Asia/Shanghai",
    )
    parser.add_argument(
        "--cookie",
        default=os.getenv("XUEQIU_COOKIE"),
        help="Full Xueqiu cookie string. Defaults to env XUEQIU_COOKIE.",
    )
    parser.add_argument(
        "--cookie-file",
        help="Optional file containing the full Xueqiu cookie string.",
    )
    parser.add_argument(
        "--output-dir",
        help="Directory for CSV/JSON/Markdown outputs. Defaults to lab/output/<cube_symbol>.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds. Default: 30",
    )
    parser.add_argument(
        "--slippage-pct",
        type=float,
        default=0.1,
        help="Single-side slippage in percent. Default: 0.1 means 10 bps per trade side.",
    )
    return parser.parse_args()


def parse_yyyymmdd(value: str) -> date:
    return datetime.strptime(value, "%Y%m%d").date()


def read_cookie(args: argparse.Namespace) -> str:
    cookie = args.cookie
    if args.cookie_file:
        cookie = Path(args.cookie_file).read_text(encoding="utf-8").strip()
    if not cookie:
        raise ValueError("Missing cookie. Use --cookie, --cookie-file, or env XUEQIU_COOKIE.")
    return cookie


def build_session(cookie: str, cube_symbol: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": f"https://xueqiu.com/P/{cube_symbol}",
            "User-Agent": DEFAULT_UA,
            "X-Requested-With": "XMLHttpRequest",
            "Cookie": cookie,
        }
    )
    return session


def dt_to_ms(dt_obj: datetime) -> int:
    if dt_obj.tzinfo is None:
        dt_obj = dt_obj.replace(tzinfo=SH_TZ)
    return int(dt_obj.astimezone(timezone.utc).timestamp() * 1000)


def ms_to_shanghai(ms: Optional[int]) -> Optional[datetime]:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(SH_TZ)


def normalize_value(value: Any) -> Any:
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if hasattr(value, "item") and callable(value.item):
        return normalize_value(value.item())
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [json_safe(item) for item in value]
    return normalize_value(value)


def fetch_rebalance_page(
    session: requests.Session,
    cube_symbol: str,
    page: int,
    *,
    count: int = 20,
    timeout: float = 30.0,
    max_retries: int = 5,
) -> Dict[str, Any]:
    last_error: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            response = session.get(
                "https://xueqiu.com/cubes/rebalancing/history.json",
                params={"cube_symbol": cube_symbol, "count": count, "page": page},
                timeout=timeout,
            )
            if response.status_code >= 400:
                snippet = response.text[:300]
                raise requests.HTTPError(
                    f"HTTP {response.status_code} on page {page}: {snippet}",
                    response=response,
                )
            payload = response.json()
            if not isinstance(payload, dict) or "list" not in payload:
                raise ValueError(f"Unexpected rebalance payload on page {page}: {payload}")
            return payload
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt >= max_retries - 1:
                break
            time_module.sleep(0.6 * (attempt + 1))
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"Unknown error fetching rebalance page {page}")


def _rebalance_item_date(item: Dict[str, Any]) -> Optional[date]:
    created_at = ms_to_shanghai(item.get("created_at"))
    return created_at.date() if created_at else None


def _oldest_rebalance_date(items: Iterable[Dict[str, Any]]) -> Optional[date]:
    dates = [item_date for item_date in (_rebalance_item_date(item) for item in items) if item_date]
    return min(dates) if dates else None


def _choose_rebalance_count(
    session: requests.Session,
    cube_symbol: str,
    *,
    timeout: float = 30.0,
) -> Tuple[int, Dict[str, Any]]:
    last_error: Optional[Exception] = None
    for count in REBALANCE_COUNT_CANDIDATES:
        try:
            payload = fetch_rebalance_page(
                session,
                cube_symbol,
                1,
                count=count,
                timeout=timeout,
            )
            return count, payload
        except Exception as exc:
            last_error = exc
            continue
    if last_error is not None:
        raise last_error
    raise RuntimeError("Unable to determine a valid rebalance page size.")


def fetch_all_rebalances(
    session: requests.Session,
    cube_symbol: str,
    *,
    start_date: Optional[date] = None,
    timeout: float = 30.0,
) -> RebalanceFetchResult:
    count, first_payload = _choose_rebalance_count(session, cube_symbol, timeout=timeout)
    page = 1
    all_items: List[Dict[str, Any]] = []
    total_expected = first_payload.get("totalCount")
    page_limit_hit = False
    oldest_fetched_date: Optional[date] = None
    while True:
        payload = first_payload if page == 1 else fetch_rebalance_page(
            session,
            cube_symbol,
            page,
            count=count,
            timeout=timeout,
        )
        items = payload.get("list") or []
        if not items:
            break
        all_items.extend(items)
        oldest_date = _oldest_rebalance_date(items)
        if oldest_date and (oldest_fetched_date is None or oldest_date < oldest_fetched_date):
            oldest_fetched_date = oldest_date
        if start_date and oldest_date and oldest_date < start_date:
            break
        if total_expected and len(all_items) >= int(total_expected):
            break
        if page >= MAX_REBALANCE_PAGES:
            page_limit_hit = True
            break
        page += 1
        time_module.sleep(0.15)
    return RebalanceFetchResult(
        items=all_items,
        page_size=count,
        total_expected=int(total_expected) if total_expected is not None else None,
        page_limit_hit=page_limit_hit,
        oldest_fetched_date=oldest_fetched_date,
        pages_fetched=page,
    )


def fetch_nav_history(
    session: requests.Session,
    cube_symbol: str,
    start_date: date,
    end_date: date,
    *,
    timeout: float = 30.0,
) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    start_ms = dt_to_ms(datetime.combine(start_date, time.min, tzinfo=SH_TZ))
    end_ms = dt_to_ms(datetime.combine(end_date, time.max, tzinfo=SH_TZ))
    response = session.get(
        "https://xueqiu.com/cubes/nav_daily/all.json",
        params={"cube_symbol": cube_symbol, "since": start_ms, "until": end_ms},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list) or not payload:
        raise ValueError(f"Unexpected nav payload: {payload}")
    series = payload[0]
    return series.get("name"), series.get("list") or []


def choose_previous_weight(row: Dict[str, Any]) -> float:
    for field in ("prev_weight_adjusted", "prev_target_weight", "prev_weight"):
        value = row.get(field)
        if value is not None:
            return float(value)
    return 0.0


def flatten_rebalance_history(items: Iterable[Dict[str, Any]]) -> Tuple[pd.DataFrame, pd.DataFrame]:
    event_rows: List[Dict[str, Any]] = []
    detail_rows: List[Dict[str, Any]] = []

    for event in items:
        created_at = ms_to_shanghai(event.get("created_at"))
        updated_at = ms_to_shanghai(event.get("updated_at"))
        histories = event.get("rebalancing_histories") or []
        turnover = 0.0
        buy_turnover = 0.0
        sell_turnover = 0.0
        buy_count = 0
        sell_count = 0
        unchanged_count = 0

        for row in histories:
            prev_weight = choose_previous_weight(row)
            target_weight = float(row.get("target_weight") or 0.0)
            delta_weight = target_weight - prev_weight
            abs_delta = abs(delta_weight)
            turnover += abs_delta
            if delta_weight > 1e-9:
                buy_count += 1
                buy_turnover += delta_weight
            elif delta_weight < -1e-9:
                sell_count += 1
                sell_turnover += abs(delta_weight)
            else:
                unchanged_count += 1

            detail_rows.append(
                {
                    "event_id": event.get("id"),
                    "event_created_at": created_at,
                    "event_date": created_at.date() if created_at else None,
                    "event_status": event.get("status"),
                    "event_category": event.get("category"),
                    "stock_symbol": row.get("stock_symbol"),
                    "stock_name": row.get("stock_name"),
                    "target_weight_pct": target_weight,
                    "current_weight_pct": float(row.get("weight") or 0.0),
                    "previous_weight_pct": prev_weight,
                    "delta_weight_pct": delta_weight,
                    "price": row.get("price"),
                    "volume": row.get("volume"),
                    "target_volume": row.get("target_volume"),
                    "previous_volume": row.get("prev_volume"),
                    "net_value": row.get("net_value"),
                    "previous_net_value": row.get("prev_net_value"),
                    "proactive": row.get("proactive"),
                }
            )

        event_rows.append(
            {
                "event_id": event.get("id"),
                "prev_rebalancing_id": event.get("prev_bebalancing_id"),
                "created_at": created_at,
                "updated_at": updated_at,
                "date": created_at.date() if created_at else None,
                "status": event.get("status"),
                "category": event.get("category"),
                "exe_strategy": event.get("exe_strategy"),
                "cash_pct": float(event.get("cash") or 0.0),
                "cash_value": float(event.get("cash_value") or 0.0),
                "comment": event.get("comment") or "",
                "diff": float(event.get("diff") or 0.0),
                "new_buy_count": int(event.get("new_buy_count") or 0),
                "holdings_count": len(histories),
                "buy_count": buy_count,
                "sell_count": sell_count,
                "unchanged_count": unchanged_count,
                "buy_turnover_pct": buy_turnover,
                "sell_turnover_pct": sell_turnover,
                "gross_turnover_pct": turnover,
                "one_way_turnover_pct": turnover / 2.0,
            }
        )

    event_df = pd.DataFrame(event_rows).sort_values(
        by=["created_at", "event_id"], ascending=[True, True]
    ).reset_index(drop=True)
    detail_df = pd.DataFrame(detail_rows).sort_values(
        by=["event_created_at", "event_id", "stock_symbol"], ascending=[True, True, True]
    ).reset_index(drop=True)
    return event_df, detail_df


def build_nav_dataframe(nav_points: Iterable[Dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame(nav_points).copy()
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"], format="%Y-%m-%d")
    df = df.sort_values("date").reset_index(drop=True)
    df["nav"] = pd.to_numeric(df["value"], errors="coerce")
    df["cumulative_return_pct"] = (df["nav"] / df["nav"].iloc[0] - 1.0) * 100.0
    df["daily_return"] = df["nav"].pct_change()
    df["rolling_peak"] = df["nav"].cummax()
    df["drawdown_pct"] = (df["nav"] / df["rolling_peak"] - 1.0) * 100.0
    return df


def build_slippage_adjusted_dataframe(
    nav_df: pd.DataFrame,
    event_df: pd.DataFrame,
    *,
    slippage_pct: float,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, Dict[str, Any]]:
    slippage_pct = max(float(slippage_pct), 0.0)
    enriched_events = event_df.copy()
    if enriched_events.empty:
        enriched_events["slippage_pct_per_side"] = pd.Series(dtype=float)
        enriched_events["slippage_cost_rate"] = pd.Series(dtype=float)
        enriched_events["slippage_cost_pct"] = pd.Series(dtype=float)
        enriched_events["slippage_effective_date"] = pd.Series(dtype="datetime64[ns]")
        enriched_events["slippage_applied"] = pd.Series(dtype=bool)
    else:
        enriched_events["slippage_pct_per_side"] = slippage_pct
        enriched_events["slippage_cost_rate"] = (
            pd.to_numeric(enriched_events["gross_turnover_pct"], errors="coerce").fillna(0.0) / 100.0
        ) * (slippage_pct / 100.0)
        enriched_events["slippage_cost_pct"] = enriched_events["slippage_cost_rate"] * 100.0

        nav_dates = pd.DatetimeIndex(pd.to_datetime(nav_df["date"]))
        effective_dates: List[pd.Timestamp] = []
        applied_flags: List[bool] = []
        for event_date in enriched_events["date"]:
            if pd.isna(event_date) or nav_dates.empty:
                effective_dates.append(pd.NaT)
                applied_flags.append(False)
                continue
            idx = nav_dates.searchsorted(pd.Timestamp(event_date), side="left")
            if idx >= len(nav_dates):
                effective_dates.append(pd.NaT)
                applied_flags.append(False)
            else:
                effective_dates.append(nav_dates[idx])
                applied_flags.append(True)
        enriched_events["slippage_effective_date"] = pd.to_datetime(effective_dates)
        enriched_events["slippage_applied"] = applied_flags

    if nav_df.empty:
        raise ValueError("No NAV data returned by Xueqiu.")

    cost_columns = [
        "slippage_event_count",
        "slippage_gross_turnover_pct",
        "slippage_one_way_turnover_pct",
        "slippage_cost_rate",
        "slippage_cost_pct",
    ]

    if enriched_events.empty or not bool(enriched_events["slippage_applied"].any()):
        schedule_df = pd.DataFrame({"date": pd.to_datetime(nav_df["date"])})
        for column in cost_columns:
            schedule_df[column] = 0.0
        schedule_df["slippage_event_count"] = schedule_df["slippage_event_count"].astype(int)
    else:
        applied_events = enriched_events.loc[enriched_events["slippage_applied"]].copy()
        schedule_df = (
            applied_events.groupby("slippage_effective_date", dropna=False)
            .agg(
                slippage_event_count=("event_id", "size"),
                slippage_gross_turnover_pct=("gross_turnover_pct", "sum"),
                slippage_one_way_turnover_pct=("one_way_turnover_pct", "sum"),
                slippage_cost_rate=("slippage_cost_rate", "sum"),
                slippage_cost_pct=("slippage_cost_pct", "sum"),
            )
            .reset_index()
            .rename(columns={"slippage_effective_date": "date"})
        )
        schedule_df["date"] = pd.to_datetime(schedule_df["date"])
        schedule_df = (
            pd.DataFrame({"date": pd.to_datetime(nav_df["date"])})
            .merge(schedule_df, on="date", how="left")
            .fillna(0.0)
        )
        schedule_df["slippage_event_count"] = schedule_df["slippage_event_count"].astype(int)

    adjusted_df = nav_df.copy()
    adjusted_df["date"] = pd.to_datetime(adjusted_df["date"])
    adjusted_df = adjusted_df.merge(schedule_df, on="date", how="left")
    for column in cost_columns:
        if column not in adjusted_df.columns:
            adjusted_df[column] = 0.0
    adjusted_df[cost_columns] = adjusted_df[cost_columns].fillna(0.0)
    adjusted_df["slippage_event_count"] = adjusted_df["slippage_event_count"].astype(int)
    adjusted_df["slippage_multiplier"] = 1.0 - adjusted_df["slippage_cost_rate"]
    adjusted_df["cumulative_slippage_multiplier"] = adjusted_df["slippage_multiplier"].cumprod()
    adjusted_df["nav_after_slippage"] = adjusted_df["nav"] * adjusted_df["cumulative_slippage_multiplier"]
    raw_start_nav = float(adjusted_df["nav"].iloc[0])
    adjusted_df["cumulative_return_after_slippage_pct"] = (
        adjusted_df["nav_after_slippage"] / raw_start_nav - 1.0
    ) * 100.0
    adjusted_df["daily_return_after_slippage"] = adjusted_df["nav_after_slippage"].pct_change()
    adjusted_df["rolling_peak_after_slippage"] = adjusted_df["nav_after_slippage"].cummax()
    adjusted_df["drawdown_after_slippage_pct"] = (
        adjusted_df["nav_after_slippage"] / adjusted_df["rolling_peak_after_slippage"] - 1.0
    ) * 100.0

    adjusted_end_nav = float(adjusted_df["nav_after_slippage"].iloc[-1])
    raw_end_nav = float(adjusted_df["nav"].iloc[-1])
    applied_rebalance_count = int(enriched_events["slippage_applied"].sum()) if not enriched_events.empty else 0
    total_rebalance_count = int(len(enriched_events))
    summary = {
        "slippage_pct_per_side": slippage_pct,
        "cost_basis": "gross_turnover_pct_times_single_side_slippage",
        "applied_rebalance_count": applied_rebalance_count,
        "unapplied_rebalance_count": total_rebalance_count - applied_rebalance_count,
        "effective_cost_days": int((adjusted_df["slippage_cost_rate"] > 0).sum()),
        "total_slippage_cost_pct": float(adjusted_df["slippage_cost_pct"].sum()),
        "average_slippage_cost_pct_per_rebalance": (
            float(adjusted_df["slippage_cost_pct"].sum()) / applied_rebalance_count
            if applied_rebalance_count > 0
            else None
        ),
        "max_single_day_slippage_cost_pct": float(adjusted_df["slippage_cost_pct"].max()),
        "first_cost_date": (
            adjusted_df.loc[adjusted_df["slippage_cost_rate"] > 0, "date"].iloc[0].date().isoformat()
            if bool((adjusted_df["slippage_cost_rate"] > 0).any())
            else None
        ),
        "last_cost_date": (
            adjusted_df.loc[adjusted_df["slippage_cost_rate"] > 0, "date"].iloc[-1].date().isoformat()
            if bool((adjusted_df["slippage_cost_rate"] > 0).any())
            else None
        ),
        "ending_nav_drag_pct": ((adjusted_end_nav / raw_end_nav) - 1.0) * 100.0 if raw_end_nav else None,
        "ending_return_drag_pct_points": ((raw_end_nav - adjusted_end_nav) / raw_start_nav) * 100.0
        if raw_start_nav
        else None,
    }
    return adjusted_df, enriched_events, schedule_df, summary


def summarize_extremes(series: pd.Series) -> Tuple[Optional[str], Optional[float], Optional[str], Optional[float]]:
    clean = series.dropna()
    if clean.empty:
        return None, None, None, None
    best_idx = clean.idxmax()
    worst_idx = clean.idxmin()
    best_date = clean.index[clean.index.get_loc(best_idx)]
    worst_date = clean.index[clean.index.get_loc(worst_idx)]
    return (
        best_date.strftime("%Y-%m-%d"),
        float(clean.loc[best_idx] * 100.0),
        worst_date.strftime("%Y-%m-%d"),
        float(clean.loc[worst_idx] * 100.0),
    )


def compute_performance_metrics(
    nav_df: pd.DataFrame,
    *,
    nav_col: str = "nav",
    daily_return_col: Optional[str] = None,
    drawdown_col: Optional[str] = None,
    reference_start_value: Optional[float] = None,
) -> Dict[str, Any]:
    if nav_df.empty:
        raise ValueError("No NAV data returned by Xueqiu.")

    date_index = pd.to_datetime(nav_df["date"])
    nav_series = pd.Series(pd.to_numeric(nav_df[nav_col], errors="coerce").values, index=date_index)
    if nav_series.dropna().empty:
        raise ValueError(f"No valid NAV values found in column: {nav_col}")

    start_date = nav_series.index[0].date()
    end_date = nav_series.index[-1].date()
    elapsed_days = max((end_date - start_date).days, 0)
    series_start_value = float(nav_series.iloc[0])
    nav_start = float(reference_start_value) if reference_start_value is not None else series_start_value
    nav_end = float(nav_series.iloc[-1])
    total_return_pct = (nav_end / nav_start - 1.0) * 100.0 if nav_start else 0.0
    annualized_return_pct = (
        ((nav_end / nav_start) ** (365.0 / elapsed_days) - 1.0) * 100.0
        if elapsed_days > 0 and nav_start > 0 and nav_end > 0
        else None
    )

    if daily_return_col and daily_return_col in nav_df.columns:
        daily_returns = pd.Series(
            pd.to_numeric(nav_df[daily_return_col], errors="coerce").values,
            index=date_index,
        ).dropna()
    else:
        daily_returns = nav_series.pct_change().dropna()
    annualized_volatility_pct = (
        float(daily_returns.std(ddof=1) * math.sqrt(252.0) * 100.0)
        if len(daily_returns) > 1 and daily_returns.std(ddof=1) > 0
        else None
    )
    sharpe = (
        float(daily_returns.mean() / daily_returns.std(ddof=1) * math.sqrt(252.0))
        if len(daily_returns) > 1 and daily_returns.std(ddof=1) > 0
        else None
    )
    if drawdown_col and drawdown_col in nav_df.columns:
        drawdown_series = pd.Series(
            pd.to_numeric(nav_df[drawdown_col], errors="coerce").values,
            index=date_index,
        )
    else:
        drawdown_series = (nav_series / nav_series.cummax() - 1.0) * 100.0
    max_drawdown_pct = float(drawdown_series.min())
    calmar = (
        float(annualized_return_pct / abs(max_drawdown_pct))
        if annualized_return_pct is not None and max_drawdown_pct < 0
        else None
    )

    monthly_nav = nav_series.resample("ME").last()
    monthly_returns = monthly_nav.pct_change()
    yearly_returns = (
        nav_series.groupby(nav_series.index.year).agg(["first", "last"])
        .assign(return_pct=lambda x: (x["last"] / x["first"] - 1.0) * 100.0)
    )

    monthly_return_table = [
        {
            "month": idx.strftime("%Y-%m"),
            "return_pct": float(value * 100.0),
        }
        for idx, value in monthly_returns.dropna().items()
    ]
    yearly_return_table = [
        {"year": str(year), "return_pct": float(row["return_pct"])}
        for year, row in yearly_returns.iterrows()
    ]

    best_day, best_day_return, worst_day, worst_day_return = summarize_extremes(daily_returns)
    best_month, best_month_return, worst_month, worst_month_return = summarize_extremes(monthly_returns)

    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "elapsed_days": elapsed_days,
        "trading_days": int(len(nav_series)),
        "series_start_value": series_start_value,
        "nav_start": nav_start,
        "nav_end": nav_end,
        "total_return_pct": total_return_pct,
        "annualized_return_pct": annualized_return_pct,
        "annualized_volatility_pct": annualized_volatility_pct,
        "sharpe": sharpe,
        "calmar": calmar,
        "max_drawdown_pct": max_drawdown_pct,
        "daily_win_rate_pct": float((daily_returns > 0).mean() * 100.0) if not daily_returns.empty else None,
        "monthly_win_rate_pct": float((monthly_returns.dropna() > 0).mean() * 100.0)
        if not monthly_returns.dropna().empty
        else None,
        "best_day": best_day,
        "best_day_return_pct": best_day_return,
        "worst_day": worst_day,
        "worst_day_return_pct": worst_day_return,
        "best_month": best_month,
        "best_month_return_pct": best_month_return,
        "worst_month": worst_month,
        "worst_month_return_pct": worst_month_return,
        "monthly_returns": monthly_return_table,
        "yearly_returns": yearly_return_table,
    }


def build_performance_comparison(
    raw_metrics: Dict[str, Any],
    slippage_metrics: Dict[str, Any],
) -> Dict[str, Any]:
    def diff(key: str) -> Optional[float]:
        raw_value = raw_metrics.get(key)
        slip_value = slippage_metrics.get(key)
        if raw_value is None or slip_value is None:
            return None
        return float(slip_value - raw_value)

    raw_nav_end = raw_metrics.get("nav_end")
    slip_nav_end = slippage_metrics.get("nav_end")
    ending_nav_ratio_pct = None
    if raw_nav_end not in (None, 0) and slip_nav_end is not None:
        ending_nav_ratio_pct = (float(slip_nav_end) / float(raw_nav_end) - 1.0) * 100.0

    return {
        "ending_nav_ratio_pct": ending_nav_ratio_pct,
        "total_return_drag_pct_points": diff("total_return_pct"),
        "annualized_return_drag_pct_points": diff("annualized_return_pct"),
        "annualized_volatility_delta_pct_points": diff("annualized_volatility_pct"),
        "max_drawdown_delta_pct_points": diff("max_drawdown_pct"),
        "daily_win_rate_delta_pct_points": diff("daily_win_rate_pct"),
        "monthly_win_rate_delta_pct_points": diff("monthly_win_rate_pct"),
        "sharpe_delta": diff("sharpe"),
        "calmar_delta": diff("calmar"),
    }


def compute_rebalance_metrics(event_df: pd.DataFrame, detail_df: pd.DataFrame) -> Dict[str, Any]:
    if event_df.empty:
        return {
            "rebalance_count": 0,
            "detail_rows": 0,
            "first_rebalance_at": None,
            "last_rebalance_at": None,
            "average_one_way_turnover_pct": None,
            "median_one_way_turnover_pct": None,
            "average_holdings_count": None,
            "average_gross_turnover_pct": None,
            "top_traded_symbols": [],
        }

    top_symbols = (
        detail_df.groupby(["stock_symbol", "stock_name"], dropna=False)
        .size()
        .reset_index(name="event_rows")
        .sort_values(["event_rows", "stock_symbol"], ascending=[False, True])
        .head(15)
    )

    return {
        "rebalance_count": int(len(event_df)),
        "detail_rows": int(len(detail_df)),
        "first_rebalance_at": event_df["created_at"].iloc[0].isoformat(),
        "last_rebalance_at": event_df["created_at"].iloc[-1].isoformat(),
        "average_one_way_turnover_pct": float(event_df["one_way_turnover_pct"].mean()),
        "median_one_way_turnover_pct": float(event_df["one_way_turnover_pct"].median()),
        "average_gross_turnover_pct": float(event_df["gross_turnover_pct"].mean()),
        "average_holdings_count": float(event_df["holdings_count"].mean()),
        "top_traded_symbols": top_symbols.to_dict(orient="records"),
    }


def build_output_paths(base_dir: Path, cube_symbol: str) -> AnalysisArtifacts:
    output_dir = base_dir / cube_symbol
    output_dir.mkdir(parents=True, exist_ok=True)
    return AnalysisArtifacts(
        output_dir=output_dir,
        details_csv=output_dir / "rebalancing_details.csv",
        events_csv=output_dir / "rebalancing_events.csv",
        nav_csv=output_dir / "nav_curve.csv",
        slippage_schedule_csv=output_dir / "slippage_schedule.csv",
        summary_json=output_dir / "performance_summary.json",
        report_md=output_dir / "performance_report.md",
    )


def render_report(
    *,
    cube_symbol: str,
    cube_name: Optional[str],
    requested_start_date: date,
    requested_end_date: date,
    actual_rebalance_start: Optional[str],
    actual_nav_start: str,
    summary: Dict[str, Any],
) -> str:
    raw_perf = summary["performance_raw"]
    slip_perf = summary["performance_after_slippage"]
    comparison = summary["comparison"]
    slippage = summary["slippage"]
    rebalance = summary["rebalancing"]
    fetch_info = summary["rebalance_fetch"]
    effective_start_date = summary.get("effective_start_date")

    def fmt_pct(value: Optional[float], digits: int = 2) -> str:
        if value is None:
            return "N/A"
        return f"{value:.{digits}f}%"

    def fmt_num(value: Optional[float], digits: int = 3) -> str:
        if value is None:
            return "N/A"
        return f"{value:.{digits}f}"

    lines = [
        f"# Xueqiu Cube Report: {cube_symbol}",
        "",
        f"- Cube name: {cube_name or 'N/A'}",
        f"- Requested range: {requested_start_date.isoformat()} to {requested_end_date.isoformat()}",
        f"- Effective range start: {effective_start_date}",
        f"- First rebalance available: {actual_rebalance_start or 'N/A'}",
        f"- First NAV available: {actual_nav_start}",
        f"- Rebalance page size used: {fetch_info['page_size']}",
        f"- Rebalance pages fetched: {fetch_info['pages_fetched']}",
        f"- Page limit fallback triggered: {'Yes' if fetch_info['page_limit_hit'] else 'No'}",
        "",
        "## Slippage Assumption",
        "",
        f"- Single-side slippage: {fmt_pct(slippage['slippage_pct_per_side'])}",
        "- Cost formula: gross turnover x single-side slippage",
        f"- Rebalances applied within NAV window: {int(slippage['applied_rebalance_count'])}",
        f"- Rebalances beyond latest NAV date: {int(slippage['unapplied_rebalance_count'])}",
        f"- Aggregate slippage cost deducted: {fmt_pct(slippage['total_slippage_cost_pct'])}",
        f"- Ending NAV drag vs raw: {fmt_pct(slippage['ending_nav_drag_pct'])}",
        "",
        "## Performance Comparison",
        "",
        "| Metric | Raw | After Slippage | Delta |",
        "| --- | ---: | ---: | ---: |",
        f"| End NAV | {raw_perf['nav_end']:.4f} | {slip_perf['nav_end']:.4f} | {fmt_pct(comparison['ending_nav_ratio_pct'])} |",
        f"| Total return | {fmt_pct(raw_perf['total_return_pct'])} | {fmt_pct(slip_perf['total_return_pct'])} | {fmt_pct(comparison['total_return_drag_pct_points'])} |",
        f"| Annualized return | {fmt_pct(raw_perf['annualized_return_pct'])} | {fmt_pct(slip_perf['annualized_return_pct'])} | {fmt_pct(comparison['annualized_return_drag_pct_points'])} |",
        f"| Max drawdown | {fmt_pct(raw_perf['max_drawdown_pct'])} | {fmt_pct(slip_perf['max_drawdown_pct'])} | {fmt_pct(comparison['max_drawdown_delta_pct_points'])} |",
        f"| Annualized volatility | {fmt_pct(raw_perf['annualized_volatility_pct'])} | {fmt_pct(slip_perf['annualized_volatility_pct'])} | {fmt_pct(comparison['annualized_volatility_delta_pct_points'])} |",
        f"| Sharpe | {fmt_num(raw_perf['sharpe'])} | {fmt_num(slip_perf['sharpe'])} | {fmt_num(comparison['sharpe_delta'])} |",
        f"| Calmar | {fmt_num(raw_perf['calmar'])} | {fmt_num(slip_perf['calmar'])} | {fmt_num(comparison['calmar_delta'])} |",
        f"| Daily win rate | {fmt_pct(raw_perf['daily_win_rate_pct'])} | {fmt_pct(slip_perf['daily_win_rate_pct'])} | {fmt_pct(comparison['daily_win_rate_delta_pct_points'])} |",
        f"| Monthly win rate | {fmt_pct(raw_perf['monthly_win_rate_pct'])} | {fmt_pct(slip_perf['monthly_win_rate_pct'])} | {fmt_pct(comparison['monthly_win_rate_delta_pct_points'])} |",
        "",
        "## Rebalancing",
        "",
        f"- Rebalance count: {rebalance['rebalance_count']}",
        f"- Average gross turnover: {rebalance['average_gross_turnover_pct']:.2f}%"
        if rebalance["average_gross_turnover_pct"] is not None
        else "- Average gross turnover: N/A",
        f"- Average one-way turnover: {rebalance['average_one_way_turnover_pct']:.2f}%"
        if rebalance["average_one_way_turnover_pct"] is not None
        else "- Average one-way turnover: N/A",
        f"- Median one-way turnover: {rebalance['median_one_way_turnover_pct']:.2f}%"
        if rebalance["median_one_way_turnover_pct"] is not None
        else "- Median one-way turnover: N/A",
        f"- Average holdings count: {rebalance['average_holdings_count']:.2f}"
        if rebalance["average_holdings_count"] is not None
        else "- Average holdings count: N/A",
        "",
        "## Best/Worst",
        "",
        f"- Raw best day: {raw_perf['best_day']} ({raw_perf['best_day_return_pct']:.2f}%)"
        if raw_perf["best_day"]
        else "- Raw best day: N/A",
        f"- Raw worst day: {raw_perf['worst_day']} ({raw_perf['worst_day_return_pct']:.2f}%)"
        if raw_perf["worst_day"]
        else "- Raw worst day: N/A",
        f"- Slippage best day: {slip_perf['best_day']} ({slip_perf['best_day_return_pct']:.2f}%)"
        if slip_perf["best_day"]
        else "- Best day: N/A",
        f"- Slippage worst day: {slip_perf['worst_day']} ({slip_perf['worst_day_return_pct']:.2f}%)"
        if slip_perf["worst_day"]
        else "- Slippage worst day: N/A",
        f"- Raw best month: {raw_perf['best_month']} ({raw_perf['best_month_return_pct']:.2f}%)"
        if raw_perf["best_month"]
        else "- Raw best month: N/A",
        f"- Raw worst month: {raw_perf['worst_month']} ({raw_perf['worst_month_return_pct']:.2f}%)"
        if raw_perf["worst_month"]
        else "- Raw worst month: N/A",
        "",
        "## Yearly Returns",
        "",
        "| Year | Raw | After Slippage |",
        "| --- | ---: | ---: |",
    ]

    raw_years = {row["year"]: row["return_pct"] for row in raw_perf["yearly_returns"]}
    slip_years = {row["year"]: row["return_pct"] for row in slip_perf["yearly_returns"]}
    all_years = sorted(set(raw_years) | set(slip_years))
    for year in all_years:
        lines.append(
            f"| {year} | {fmt_pct(raw_years.get(year))} | {fmt_pct(slip_years.get(year))} |"
        )

    lines.extend(
        [
            "",
            "## Top Traded Symbols",
            "",
            "| Symbol | Name | Event Rows |",
            "| --- | --- | ---: |",
        ]
    )
    for row in rebalance["top_traded_symbols"]:
        lines.append(
            f"| {row.get('stock_symbol') or ''} | {row.get('stock_name') or ''} | {int(row.get('event_rows') or 0)} |"
        )
    lines.append("")
    return "\n".join(lines)


def save_outputs(
    artifacts: AnalysisArtifacts,
    *,
    event_df: pd.DataFrame,
    detail_df: pd.DataFrame,
    nav_df: pd.DataFrame,
    slippage_schedule_df: pd.DataFrame,
    summary: Dict[str, Any],
    report: str,
) -> None:
    event_df.to_csv(artifacts.events_csv, index=False)
    detail_df.to_csv(artifacts.details_csv, index=False)
    nav_df.to_csv(artifacts.nav_csv, index=False)
    slippage_schedule_df.to_csv(artifacts.slippage_schedule_csv, index=False)
    artifacts.summary_json.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    artifacts.report_md.write_text(report, encoding="utf-8")


def main() -> None:
    args = parse_args()
    cube_symbol = args.cube_symbol.strip().upper()
    requested_start = parse_yyyymmdd(args.start_date)
    requested_end = parse_yyyymmdd(args.end_date)
    if requested_start > requested_end:
        raise ValueError("start-date must be <= end-date")

    cookie = read_cookie(args)
    if args.output_dir:
        explicit_dir = Path(args.output_dir).expanduser()
        explicit_dir.mkdir(parents=True, exist_ok=True)
        artifacts = AnalysisArtifacts(
            output_dir=explicit_dir,
            details_csv=explicit_dir / "rebalancing_details.csv",
            events_csv=explicit_dir / "rebalancing_events.csv",
            nav_csv=explicit_dir / "nav_curve.csv",
            slippage_schedule_csv=explicit_dir / "slippage_schedule.csv",
            summary_json=explicit_dir / "performance_summary.json",
            report_md=explicit_dir / "performance_report.md",
        )
    else:
        base_dir = Path(__file__).resolve().parent / "output"
        artifacts = build_output_paths(base_dir, cube_symbol)

    session = build_session(cookie, cube_symbol)
    rebalance_fetch = fetch_all_rebalances(
        session,
        cube_symbol,
        start_date=requested_start,
        timeout=args.timeout,
    )
    effective_start = requested_start
    if (
        rebalance_fetch.page_limit_hit
        and rebalance_fetch.oldest_fetched_date
        and rebalance_fetch.oldest_fetched_date > requested_start
    ):
        effective_start = rebalance_fetch.oldest_fetched_date
    cube_name, nav_points = fetch_nav_history(
        session,
        cube_symbol,
        effective_start,
        requested_end,
        timeout=args.timeout,
    )

    event_df, detail_df = flatten_rebalance_history(rebalance_fetch.items)
    nav_df = build_nav_dataframe(nav_points)

    if not event_df.empty:
        event_df = event_df.loc[
            (event_df["date"] >= effective_start) & (event_df["date"] <= requested_end)
        ].reset_index(drop=True)
    if not detail_df.empty:
        detail_df = detail_df.loc[
            (detail_df["event_date"] >= effective_start) & (detail_df["event_date"] <= requested_end)
        ].reset_index(drop=True)
    if not nav_df.empty:
        nav_df = nav_df.loc[
            (nav_df["date"].dt.date >= effective_start) & (nav_df["date"].dt.date <= requested_end)
        ].reset_index(drop=True)

    performance_raw = compute_performance_metrics(nav_df)
    nav_with_slippage_df, event_with_slippage_df, slippage_schedule_df, slippage_summary = (
        build_slippage_adjusted_dataframe(
            nav_df,
            event_df,
            slippage_pct=args.slippage_pct,
        )
    )
    performance_after_slippage = compute_performance_metrics(
        nav_with_slippage_df,
        nav_col="nav_after_slippage",
        daily_return_col="daily_return_after_slippage",
        drawdown_col="drawdown_after_slippage_pct",
        reference_start_value=float(nav_df["nav"].iloc[0]),
    )
    comparison = build_performance_comparison(performance_raw, performance_after_slippage)
    rebalancing = compute_rebalance_metrics(event_with_slippage_df, detail_df)
    actual_rebalance_start = (
        event_with_slippage_df["created_at"].iloc[0].isoformat() if not event_with_slippage_df.empty else None
    )
    actual_nav_start = performance_raw["start_date"]

    summary = {
        "cube_symbol": cube_symbol,
        "cube_name": cube_name,
        "requested_start_date": requested_start.isoformat(),
        "requested_end_date": requested_end.isoformat(),
        "effective_start_date": effective_start.isoformat(),
        "actual_rebalance_start": actual_rebalance_start,
        "actual_nav_start": actual_nav_start,
        "rebalance_fetch": {
            "page_size": rebalance_fetch.page_size,
            "pages_fetched": rebalance_fetch.pages_fetched,
            "total_expected": rebalance_fetch.total_expected,
            "page_limit_hit": rebalance_fetch.page_limit_hit,
            "oldest_fetched_date": rebalance_fetch.oldest_fetched_date.isoformat()
            if rebalance_fetch.oldest_fetched_date
            else None,
        },
        "performance_raw": performance_raw,
        "performance_after_slippage": performance_after_slippage,
        "slippage": slippage_summary,
        "comparison": comparison,
        "rebalancing": rebalancing,
        "files": {
            "output_dir": str(artifacts.output_dir),
            "events_csv": str(artifacts.events_csv),
            "details_csv": str(artifacts.details_csv),
            "nav_csv": str(artifacts.nav_csv),
            "slippage_schedule_csv": str(artifacts.slippage_schedule_csv),
            "summary_json": str(artifacts.summary_json),
            "report_md": str(artifacts.report_md),
        },
    }
    summary = json_safe(summary)

    report = render_report(
        cube_symbol=cube_symbol,
        cube_name=cube_name,
        requested_start_date=requested_start,
        requested_end_date=requested_end,
        actual_rebalance_start=actual_rebalance_start,
        actual_nav_start=actual_nav_start,
        summary=summary,
    )
    save_outputs(
        artifacts,
        event_df=event_with_slippage_df,
        detail_df=detail_df,
        nav_df=nav_with_slippage_df,
        slippage_schedule_df=slippage_schedule_df,
        summary=summary,
        report=report,
    )

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print()
    print(f"Saved outputs to: {artifacts.output_dir}")


if __name__ == "__main__":
    main()

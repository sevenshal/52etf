from __future__ import annotations

import json
import math
import time as time_module
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import duckdb
import pandas as pd
import requests

from ..analytics_database import ANALYTICS_DB_PATH


SH_TZ = ZoneInfo("Asia/Shanghai")
BENCHMARK_SYMBOL = "000905.SH"
BENCHMARK_NAME = "中证500"
DEFAULT_START_DATE = date(2000, 1, 1)
MAX_REBALANCE_PAGES = 50
REBALANCE_COUNT_CANDIDATES = (50, 40, 30, 25, 20)
DEFAULT_UA = (
    "Xueqiu iPhone 14.90.2"
)
XUEQIU_API_BASE_URL = "https://api.xueqiu.com"


@dataclass
class RebalanceFetchResult:
    items: List[Dict[str, Any]]
    page_size: int
    total_expected: Optional[int]
    page_limit_hit: bool
    oldest_fetched_date: Optional[date]
    pages_fetched: int


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


def build_session(cookie: str, cube_symbol: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/json",
            "Accept-Language": "zh-Hans-CN;q=1, en-CN;q=0.9",
            "Referer": f"https://xueqiu.com/P/{cube_symbol}",
            "User-Agent": DEFAULT_UA,
            "X-Device-OS": "iOS 26.4.2",
            "X-Device-Model-Name": "iPhone 16 Pro Max_iPhone17,2",
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
                f"{XUEQIU_API_BASE_URL}/cubes/rebalancing/history.json",
                params={"cube_symbol": cube_symbol, "count": count, "page": page},
                timeout=timeout,
            )
            if response.status_code >= 400:
                error_code = None
                try:
                    error_payload = response.json()
                    error_code = str(error_payload.get("error_code") or "")
                except ValueError:
                    pass
                snippet = response.text[:300]
                error = requests.HTTPError(
                    f"HTTP {response.status_code} on page {page}: {snippet}",
                    response=response,
                )
                setattr(error, "xueqiu_error_code", error_code)
                raise error
            payload = response.json()
            if not isinstance(payload, dict) or "list" not in payload:
                raise ValueError(f"Unexpected rebalance payload on page {page}: {payload}")
            return payload
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt >= max_retries - 1:
                break
            error_code = getattr(exc, "xueqiu_error_code", None)
            retry_delay = 0.6 * (attempt + 1)
            if error_code in {"10026", "400016"}:
                retry_delay = min(45.0, 5.0 * (attempt + 1))
            time_module.sleep(retry_delay)
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
            payload = fetch_rebalance_page(session, cube_symbol, 1, count=count, timeout=timeout)
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
    max_pages: int = MAX_REBALANCE_PAGES,
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
        if page >= max_pages:
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
        f"{XUEQIU_API_BASE_URL}/cubes/nav_daily/all.json",
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

    event_df = pd.DataFrame(event_rows)
    detail_df = pd.DataFrame(detail_rows)
    if not event_df.empty:
        event_df = event_df.sort_values(by=["created_at", "event_id"], ascending=[True, True]).reset_index(drop=True)
    if not detail_df.empty:
        detail_df = detail_df.sort_values(
            by=["event_created_at", "event_id", "stock_symbol"],
            ascending=[True, True, True],
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
        raise ValueError("No NAV data returned.")

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


def build_benchmark_dataframe(nav_df: pd.DataFrame, start_date: date, end_date: date) -> pd.DataFrame:
    nav_dates = pd.DataFrame({"date": pd.to_datetime(nav_df["date"])})
    nav_dates["date"] = nav_dates["date"].astype("datetime64[ns]")
    if nav_dates.empty:
        return nav_dates

    try:
        connection = duckdb.connect(ANALYTICS_DB_PATH, read_only=True)
        try:
            rows = connection.execute(
                """
                SELECT CAST(trade_date AS DATE) AS date, CAST(close AS DOUBLE) AS close
                FROM a_stock_index_daily
                WHERE ts_code = ?
                  AND trade_date BETWEEN ? AND ?
                  AND close IS NOT NULL
                  AND close > 0
                ORDER BY trade_date
                """,
                [BENCHMARK_SYMBOL, start_date, end_date],
            ).fetchall()
        finally:
            connection.close()
    except Exception:
        rows = []

    if not rows:
        benchmark = nav_dates.copy()
        benchmark["benchmark_nav"] = None
        benchmark["benchmark_cumulative_return_pct"] = None
        benchmark["benchmark_daily_return"] = None
        benchmark["benchmark_drawdown_pct"] = None
        return benchmark

    benchmark_raw = pd.DataFrame(rows, columns=["date", "close"])
    benchmark_raw["date"] = pd.to_datetime(benchmark_raw["date"])
    benchmark_raw["date"] = benchmark_raw["date"].astype("datetime64[ns]")
    benchmark_raw["close"] = pd.to_numeric(benchmark_raw["close"], errors="coerce")
    benchmark_raw = benchmark_raw.dropna(subset=["close"]).sort_values("date")
    benchmark = pd.merge_asof(
        nav_dates.sort_values("date"),
        benchmark_raw,
        on="date",
        direction="backward",
    )
    benchmark["close"] = benchmark["close"].ffill().bfill()
    if benchmark["close"].dropna().empty:
        benchmark["benchmark_nav"] = None
        benchmark["benchmark_cumulative_return_pct"] = None
        benchmark["benchmark_daily_return"] = None
        benchmark["benchmark_drawdown_pct"] = None
        return benchmark.drop(columns=["close"], errors="ignore")

    start_close = float(benchmark["close"].dropna().iloc[0])
    benchmark["benchmark_nav"] = benchmark["close"] / start_close
    benchmark["benchmark_cumulative_return_pct"] = (benchmark["benchmark_nav"] / benchmark["benchmark_nav"].iloc[0] - 1.0) * 100.0
    benchmark["benchmark_daily_return"] = benchmark["benchmark_nav"].pct_change()
    benchmark["benchmark_drawdown_pct"] = (benchmark["benchmark_nav"] / benchmark["benchmark_nav"].cummax() - 1.0) * 100.0
    return benchmark.drop(columns=["close"], errors="ignore")


def merge_yearly_returns(
    raw_metrics: Dict[str, Any],
    slippage_metrics: Dict[str, Any],
    benchmark_metrics: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    raw_years = {row["year"]: row["return_pct"] for row in raw_metrics.get("yearly_returns") or []}
    slip_years = {row["year"]: row["return_pct"] for row in slippage_metrics.get("yearly_returns") or []}
    benchmark_years = {
        row["year"]: row["return_pct"]
        for row in (benchmark_metrics or {}).get("yearly_returns") or []
    }
    all_years = sorted(set(raw_years) | set(slip_years) | set(benchmark_years))
    return [
        {
            "year": year,
            "raw_return_pct": raw_years.get(year),
            "slippage_return_pct": slip_years.get(year),
            "benchmark_return_pct": benchmark_years.get(year),
            "excess_return_after_slippage_pct": (
                slip_years[year] - benchmark_years[year]
                if year in slip_years and year in benchmark_years
                else None
            ),
        }
        for year in all_years
    ]


def build_curve_points(nav_df: pd.DataFrame) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for row in nav_df.to_dict(orient="records"):
        row_date = pd.Timestamp(row["date"]).date().isoformat()
        rows.append(
            {
                "date": row_date,
                "raw_nav": normalize_value(row.get("nav")),
                "slippage_nav": normalize_value(row.get("nav_after_slippage")),
                "benchmark_nav": normalize_value(row.get("benchmark_nav")),
                "raw_return_pct": normalize_value(row.get("cumulative_return_pct")),
                "slippage_return_pct": normalize_value(row.get("cumulative_return_after_slippage_pct")),
                "benchmark_return_pct": normalize_value(row.get("benchmark_cumulative_return_pct")),
                "raw_drawdown_pct": normalize_value(row.get("drawdown_pct")),
                "slippage_drawdown_pct": normalize_value(row.get("drawdown_after_slippage_pct")),
                "benchmark_drawdown_pct": normalize_value(row.get("benchmark_drawdown_pct")),
                "slippage_cost_pct": normalize_value(row.get("slippage_cost_pct")),
            }
        )
    return rows


def run_snowball_cube_backtest(
    *,
    cube_symbol: str,
    cookie: str,
    slippage_pct: float,
    start_date: date = DEFAULT_START_DATE,
    end_date: Optional[date] = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    cube_symbol = str(cube_symbol or "").strip().upper()
    if not cube_symbol:
        raise ValueError("Missing cube symbol")
    if not cookie:
        raise ValueError("Missing Xueqiu cookie")

    requested_end = end_date or datetime.now(SH_TZ).date()
    session = build_session(cookie, cube_symbol)
    rebalance_fetch = fetch_all_rebalances(
        session,
        cube_symbol,
        start_date=start_date,
        timeout=timeout,
    )
    effective_start = start_date
    if (
        rebalance_fetch.page_limit_hit
        and rebalance_fetch.oldest_fetched_date
        and rebalance_fetch.oldest_fetched_date > start_date
    ):
        effective_start = rebalance_fetch.oldest_fetched_date

    cube_name, nav_points = fetch_nav_history(
        session,
        cube_symbol,
        effective_start,
        requested_end,
        timeout=timeout,
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
    nav_with_slippage_df, event_with_slippage_df, _slippage_schedule_df, slippage_summary = (
        build_slippage_adjusted_dataframe(nav_df, event_df, slippage_pct=slippage_pct)
    )
    performance_after_slippage = compute_performance_metrics(
        nav_with_slippage_df,
        nav_col="nav_after_slippage",
        daily_return_col="daily_return_after_slippage",
        drawdown_col="drawdown_after_slippage_pct",
        reference_start_value=float(nav_df["nav"].iloc[0]),
    )

    actual_start_date = datetime.strptime(performance_raw["start_date"], "%Y-%m-%d").date()
    actual_end_date = datetime.strptime(performance_raw["end_date"], "%Y-%m-%d").date()
    benchmark_df = build_benchmark_dataframe(nav_with_slippage_df, actual_start_date, actual_end_date)
    nav_with_slippage_df = nav_with_slippage_df.merge(benchmark_df, on="date", how="left")

    benchmark_metrics = None
    if "benchmark_nav" in nav_with_slippage_df.columns and not nav_with_slippage_df["benchmark_nav"].dropna().empty:
        benchmark_metrics = compute_performance_metrics(
            nav_with_slippage_df.dropna(subset=["benchmark_nav"]).reset_index(drop=True),
            nav_col="benchmark_nav",
            daily_return_col="benchmark_daily_return",
            drawdown_col="benchmark_drawdown_pct",
        )

    comparison = build_performance_comparison(performance_raw, performance_after_slippage)
    rebalancing = compute_rebalance_metrics(event_with_slippage_df, detail_df)
    actual_rebalance_start = (
        event_with_slippage_df["created_at"].iloc[0].isoformat() if not event_with_slippage_df.empty else None
    )
    curve_points = build_curve_points(nav_with_slippage_df)
    yearly_returns = merge_yearly_returns(performance_raw, performance_after_slippage, benchmark_metrics)

    summary = {
        "cube_symbol": cube_symbol,
        "cube_name": cube_name,
        "requested_start_date": start_date.isoformat(),
        "requested_end_date": requested_end.isoformat(),
        "effective_start_date": effective_start.isoformat(),
        "actual_rebalance_start": actual_rebalance_start,
        "actual_nav_start": performance_raw["start_date"],
        "actual_nav_end": performance_raw["end_date"],
        "benchmark_symbol": BENCHMARK_SYMBOL,
        "benchmark_name": BENCHMARK_NAME,
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
        "benchmark_metrics": benchmark_metrics,
        "slippage": slippage_summary,
        "comparison": comparison,
        "rebalancing": rebalancing,
        "yearly_returns": yearly_returns,
        "curve_points": curve_points,
    }
    return json_safe(summary)

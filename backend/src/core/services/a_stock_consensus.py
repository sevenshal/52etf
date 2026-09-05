from __future__ import annotations

import re
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import text


A_STOCK_MARKET_CAP_UNIT = 10_000.0
FLOAT_COMPARE_EPSILON = 1e-9

# 研报取数窗口：post_latest_annual = 最近一次年报公告日之后(正常)；
# post_prev_annual = 最近一次年报后暂无研报，退回上一次年报之后(估值待更新)；
# unfiltered = 缺少年报公告日，无法按年报切窗口。
CONSENSUS_WINDOW_POST_LATEST_ANNUAL = "post_latest_annual"
CONSENSUS_WINDOW_POST_PREV_ANNUAL = "post_prev_annual"
CONSENSUS_WINDOW_UNFILTERED = "unfiltered"


def normalize_a_stock_symbol(symbol: Optional[str]) -> str:
    raw = str(symbol or "").strip().upper()
    if not raw:
        return ""
    if "." in raw:
        code, suffix = raw.split(".", 1)
        return f"{code}.{suffix}"
    code = re.sub(r"\D", "", raw)
    if not code:
        return raw
    if code.startswith(("43", "83", "87", "88", "92")):
        return f"{code}.BJ"
    if code.startswith(("6", "9")):
        return f"{code}.SH"
    return f"{code}.SZ"


def _looks_like_a_stock_code(value: Optional[str]) -> bool:
    raw = str(value or "").strip().upper()
    if not raw:
        return False
    return bool(re.fullmatch(r"\d{6}(?:\.(?:SH|SZ|BJ))?", raw))


def _safe_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _positive_float(value: Any) -> Optional[float]:
    number = _safe_float(value)
    if number is None or number <= 0:
        return None
    return number


def _avg(values: Iterable[Optional[float]]) -> Optional[float]:
    clean = [value for value in values if value is not None]
    if not clean:
        return None
    return sum(clean) / len(clean)


def _percentile(values: Iterable[Optional[float]], percentile: float) -> Optional[float]:
    clean = sorted(value for value in values if value is not None)
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    position = (len(clean) - 1) * percentile
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(clean) - 1)
    fraction = position - lower_index
    return clean[lower_index] + (clean[upper_index] - clean[lower_index]) * fraction


def _parse_fiscal_year(value: Any) -> Optional[int]:
    text_value = str(value or "").strip()
    if not text_value:
        return None
    match = re.search(r"(19\d{2}|20\d{2})", text_value)
    if not match:
        return None
    year = int(match.group(1))
    if year < 1990 or year > 2100:
        return None
    return year


def _date_to_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _report_key(row: Mapping[str, Any]) -> Tuple[str, str, str, str]:
    return (
        str(row.get("report_date") or ""),
        str(row.get("org_name") or "").strip(),
        str(row.get("author_name") or "").strip(),
        str(row.get("report_title") or "").strip(),
    )


def _target_price_bounds(row: Mapping[str, Any]) -> Optional[Tuple[float, float]]:
    """返回单篇研报自洽的目标价 (下沿, 上沿)。

    Tushare report_rc 的 min_price/max_price 经常只填一边(点目标价或数据缺失)，
    单边填充时用另一边兜底，保证同一篇研报的下沿/上沿始终成对出现。否则按缺失
    字段分别取 min/max 会让区间落在不同的研报子集上，出现"均值不在区间内"。
    """
    min_price = _positive_float(row.get("min_price"))
    max_price = _positive_float(row.get("max_price"))
    low = min_price if min_price is not None else max_price
    high = max_price if max_price is not None else min_price
    if low is None or high is None:
        return None
    return (low, high) if low <= high else (high, low)


def _target_price(row: Mapping[str, Any]) -> Optional[float]:
    bounds = _target_price_bounds(row)
    if bounds is None:
        return None
    return (bounds[0] + bounds[1]) / 2.0


def _row_to_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)).date()
    except (TypeError, ValueError):
        return None


def _pick_forecast_years(years: Sequence[int], latest_trade_date: Optional[date]) -> Tuple[Optional[int], Optional[int]]:
    sorted_years = sorted(set(years))
    if len(sorted_years) < 2:
        return None, None
    reference_year = latest_trade_date.year if latest_trade_date else date.today().year
    future_years = [year for year in sorted_years if year >= reference_year]
    if len(future_years) >= 2:
        return future_years[0], future_years[1]
    return sorted_years[0], sorted_years[1]


def _growth_pct(current_value: Optional[float], next_value: Optional[float]) -> Optional[float]:
    if current_value is None or next_value is None or current_value <= 0:
        return None
    return (next_value / current_value - 1.0) * 100.0


def _select_rows_by_annual_cutoff(
    rows: Sequence[Mapping[str, Any]],
    latest_annual_ann_date: Optional[date],
    prev_annual_ann_date: Optional[date],
) -> Tuple[Sequence[Mapping[str, Any]], str]:
    """只保留最近一次年报公告日之后发布的研报。

    年报披露后卖方才会按新的经营数据重新给盈利预测和目标价，混入年报前的旧研报
    会把过时的目标价拉进共识。若最近一次年报后还没有带目标价的研报，退回到上一次
    年报之后的研报，并把窗口标记为 post_prev_annual(前端提示"估值待更新")。
    """
    if latest_annual_ann_date is None:
        return rows, CONSENSUS_WINDOW_UNFILTERED

    def kept_since(cutoff: date) -> List[Mapping[str, Any]]:
        selected: List[Mapping[str, Any]] = []
        for row in rows:
            report_date = _row_to_date(row.get("report_date"))
            if report_date is not None and report_date >= cutoff:
                selected.append(row)
        return selected

    def has_target(candidate_rows: Sequence[Mapping[str, Any]]) -> bool:
        return any(_target_price_bounds(row) is not None for row in candidate_rows)

    latest_rows = kept_since(latest_annual_ann_date)
    if has_target(latest_rows):
        return latest_rows, CONSENSUS_WINDOW_POST_LATEST_ANNUAL

    if prev_annual_ann_date is not None:
        prev_rows = kept_since(prev_annual_ann_date)
        if has_target(prev_rows):
            return prev_rows, CONSENSUS_WINDOW_POST_PREV_ANNUAL

    return rows, CONSENSUS_WINDOW_UNFILTERED


def _aggregate_report_rows(
    rows: Sequence[Mapping[str, Any]],
    latest_trade_date: Optional[date],
    *,
    latest_annual_ann_date: Optional[date] = None,
    prev_annual_ann_date: Optional[date] = None,
) -> Optional[Dict[str, Any]]:
    rows, consensus_window = _select_rows_by_annual_cutoff(
        rows,
        latest_annual_ann_date,
        prev_annual_ann_date,
    )
    target_prices: Dict[Tuple[str, str, str, str], float] = {}
    target_lows: Dict[Tuple[str, str, str, str], float] = {}
    target_highs: Dict[Tuple[str, str, str, str], float] = {}
    forecast_by_year: Dict[int, Dict[Tuple[str, str, str, str], Dict[str, Optional[float]]]] = defaultdict(dict)
    report_keys = set()
    orgs = set()
    latest_report_date = None
    rating_counter: Counter[str] = Counter()

    for row in rows:
        key = _report_key(row)
        report_keys.add(key)
        org_name = str(row.get("org_name") or "").strip()
        if org_name:
            orgs.add(org_name)
        report_date = row.get("report_date")
        if latest_report_date is None or (report_date is not None and report_date > latest_report_date):
            latest_report_date = report_date
        rating = str(row.get("rating") or "").strip()
        if rating:
            rating_counter[rating] += 1

        bounds = _target_price_bounds(row)
        if bounds is not None and key not in target_prices:
            target_prices[key] = (bounds[0] + bounds[1]) / 2.0
            target_lows[key] = bounds[0]
            target_highs[key] = bounds[1]

        fiscal_year = _parse_fiscal_year(row.get("quarter"))
        if fiscal_year is None:
            continue
        forecast_by_year[fiscal_year][key] = {
            "eps": _positive_float(row.get("eps")),
            "np": _positive_float(row.get("np")),
            "pe": _positive_float(row.get("pe")),
        }

    target_report_count = len(target_prices)
    target_price_avg = _avg(target_prices.values())
    if target_report_count <= 0 or target_price_avg is None:
        return None

    current_year, next_year = _pick_forecast_years(list(forecast_by_year), latest_trade_date)
    current_forecasts = forecast_by_year.get(current_year, {}) if current_year else {}
    next_forecasts = forecast_by_year.get(next_year, {}) if next_year else {}
    current_eps = _avg(item.get("eps") for item in current_forecasts.values())
    next_eps = _avg(item.get("eps") for item in next_forecasts.values())
    current_np = _avg(item.get("np") for item in current_forecasts.values())
    next_np = _avg(item.get("np") for item in next_forecasts.values())
    current_pe = _avg(item.get("pe") for item in current_forecasts.values())
    next_pe = _avg(item.get("pe") for item in next_forecasts.values())

    growth_source = "eps"
    growth_pct = _growth_pct(current_eps, next_eps)
    if growth_pct is None:
        growth_source = "np"
        growth_pct = _growth_pct(current_np, next_np)

    return {
        "latest_report_date": latest_report_date,
        "target_price_avg": target_price_avg,
        "target_price_min": min(target_lows.values()),
        "target_price_max": max(target_highs.values()),
        "target_price_q25": _percentile(target_prices.values(), 0.25),
        "target_price_median": _percentile(target_prices.values(), 0.5),
        "target_price_q75": _percentile(target_prices.values(), 0.75),
        "growth_pct": growth_pct,
        "growth_source": growth_source if growth_pct is not None else None,
        "forecast_year": current_year,
        "next_forecast_year": next_year,
        "consensus_eps": current_eps,
        "next_consensus_eps": next_eps,
        "consensus_np": current_np,
        "next_consensus_np": next_np,
        "consensus_pe": current_pe,
        "next_consensus_pe": next_pe,
        "report_count": len(report_keys),
        "target_report_count": target_report_count,
        "organization_count": len(orgs),
        "rating": rating_counter.most_common(1)[0][0] if rating_counter else None,
        "consensus_window": consensus_window,
        "is_stale": consensus_window != CONSENSUS_WINDOW_POST_LATEST_ANNUAL,
        "latest_annual_ann_date": latest_annual_ann_date,
    }


def build_a_stock_consensus_candidates(
    rows: Sequence[Mapping[str, Any]],
    latest_trade_date: Optional[date],
    *,
    search_symbol: str = "",
    has_search: bool = False,
    min_market_cap_100m: Optional[float] = 100.0,
    max_market_cap_100m: Optional[float] = None,
    min_undervalue_pct: Optional[float] = 10.0,
    min_growth_pct: Optional[float] = 10.0,
    min_report_count: int = 1,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        symbol = str(row.get("ts_code") or "").strip().upper()
        if symbol:
            grouped[symbol].append(row)

    normalized_symbol = normalize_a_stock_symbol(search_symbol)
    apply_filters = not has_search and not normalized_symbol
    min_reports = 1 if has_search or normalized_symbol else max(1, int(min_report_count or 1))
    normalized_limit = max(1, min(int(limit or 200), 1000))

    candidates: List[Dict[str, Any]] = []
    for symbol, symbol_rows in grouped.items():
        first = symbol_rows[0]
        close = _positive_float(first.get("close"))
        if close is None:
            continue

        total_mv = _positive_float(first.get("total_mv"))
        circ_mv = _positive_float(first.get("circ_mv"))
        market_cap_100m = total_mv / A_STOCK_MARKET_CAP_UNIT if total_mv is not None else None
        circ_market_cap_100m = circ_mv / A_STOCK_MARKET_CAP_UNIT if circ_mv is not None else None

        aggregate = _aggregate_report_rows(
            symbol_rows,
            latest_trade_date,
            latest_annual_ann_date=_row_to_date(first.get("latest_annual_ann_date")),
            prev_annual_ann_date=_row_to_date(first.get("prev_annual_ann_date")),
        )
        if aggregate is None:
            continue
        target_report_count = aggregate["target_report_count"]
        if target_report_count < min_reports:
            continue

        target_price_avg = aggregate["target_price_avg"]
        target_price_min = aggregate["target_price_min"]
        target_price_max = aggregate["target_price_max"]
        undervalue_pct = (target_price_min / close - 1.0) * 100.0
        growth_pct = aggregate["growth_pct"]

        if apply_filters:
            if market_cap_100m is None:
                continue
            if min_market_cap_100m is not None and market_cap_100m < float(min_market_cap_100m):
                continue
            if max_market_cap_100m is not None and market_cap_100m > float(max_market_cap_100m):
                continue
            if min_undervalue_pct is not None and undervalue_pct + FLOAT_COMPARE_EPSILON < float(min_undervalue_pct):
                continue
            if min_growth_pct is not None and (
                growth_pct is None or growth_pct + FLOAT_COMPARE_EPSILON < float(min_growth_pct)
            ):
                continue

        candidates.append({
            "symbol": symbol,
            "name": first.get("stock_name") or first.get("report_name") or symbol,
            "industry": first.get("industry"),
            "market": first.get("market"),
            "trade_date": _date_to_iso(first.get("trade_date")),
            "latest_report_date": _date_to_iso(aggregate["latest_report_date"]),
            "close": close,
            "target_price_avg": target_price_avg,
            "target_price_min": target_price_min,
            "target_price_max": target_price_max,
            "undervalue_pct": undervalue_pct,
            "growth_pct": growth_pct,
            "growth_source": aggregate["growth_source"],
            "forecast_year": aggregate["forecast_year"],
            "next_forecast_year": aggregate["next_forecast_year"],
            "consensus_eps": aggregate["consensus_eps"],
            "next_consensus_eps": aggregate["next_consensus_eps"],
            "consensus_np": aggregate["consensus_np"],
            "next_consensus_np": aggregate["next_consensus_np"],
            "consensus_pe": aggregate["consensus_pe"],
            "next_consensus_pe": aggregate["next_consensus_pe"],
            "market_cap_100m": market_cap_100m,
            "circ_market_cap_100m": circ_market_cap_100m,
            "report_count": aggregate["report_count"],
            "target_report_count": target_report_count,
            "organization_count": aggregate["organization_count"],
            "rating": aggregate["rating"],
            "consensus_window": aggregate["consensus_window"],
            "is_stale": aggregate["is_stale"],
            "latest_annual_ann_date": _date_to_iso(aggregate["latest_annual_ann_date"]),
        })

    candidates.sort(
        key=lambda item: (
            item.get("undervalue_pct") if item.get("undervalue_pct") is not None else -999999.0,
            item.get("growth_pct") if item.get("growth_pct") is not None else -999999.0,
            item.get("target_report_count") or 0,
        ),
        reverse=True,
    )
    return candidates[:normalized_limit]


def build_a_stock_consensus_history(
    rows: Sequence[Mapping[str, Any]],
    latest_trade_date: Optional[date],
    *,
    limit: int = 1260,
) -> List[Dict[str, Any]]:
    grouped: Dict[date, List[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        report_date = _row_to_date(row.get("report_date"))
        if report_date is not None:
            grouped[report_date].append(row)

    history: List[Dict[str, Any]] = []
    for report_date, report_rows in grouped.items():
        aggregate = _aggregate_report_rows(report_rows, latest_trade_date)
        if aggregate is None:
            continue
        history.append({
            "date": report_date.isoformat(),
            "fair_value_lo": aggregate["target_price_min"],
            "fair_value_hi": aggregate["target_price_max"],
            "target_price_avg": aggregate["target_price_avg"],
            "target_price_min": aggregate["target_price_min"],
            "target_price_max": aggregate["target_price_max"],
            "forward_next_fy_lo": None,
            "forward_next_fy_hi": None,
            "pe_ratio": aggregate["consensus_pe"],
            "forward_pe_ratio": aggregate["next_consensus_pe"],
            "growth_pct": aggregate["growth_pct"],
            "growth_source": aggregate["growth_source"],
            "forecast_year": aggregate["forecast_year"],
            "next_forecast_year": aggregate["next_forecast_year"],
            "report_count": aggregate["report_count"],
            "target_report_count": aggregate["target_report_count"],
            "organization_count": aggregate["organization_count"],
            "rating": aggregate["rating"],
        })

    normalized_limit = max(1, min(int(limit or 1260), 5000))
    history.sort(key=lambda item: item["date"], reverse=True)
    return history[:normalized_limit]


def build_a_stock_rolling_consensus_history(
    rows: Sequence[Mapping[str, Any]],
    trade_dates: Iterable[date],
    *,
    report_lookback_days: int = 180,
) -> List[Dict[str, Any]]:
    """Build point-in-time consensus ranges for each trading day."""
    normalized_dates = sorted(set(day for day in trade_dates if day))
    dated_rows = sorted(
        (
            (report_day, row)
            for row in rows
            if (report_day := _row_to_date(row.get("report_date"))) is not None
        ),
        key=lambda item: item[0],
    )
    if not normalized_dates or not dated_rows:
        return []

    lookback = timedelta(days=max(1, min(int(report_lookback_days or 180), 1095)))
    cursor = 0
    active_rows: List[Mapping[str, Any]] = []
    history: List[Dict[str, Any]] = []
    for trade_day in normalized_dates:
        while cursor < len(dated_rows) and dated_rows[cursor][0] <= trade_day:
            active_rows.append(dated_rows[cursor][1])
            cursor += 1
        window_start = trade_day - lookback
        active_rows = [
            row for row in active_rows
            if (_row_to_date(row.get("report_date")) or date.min) >= window_start
        ]
        aggregate = _aggregate_report_rows(active_rows, trade_day)
        if aggregate is None:
            continue
        growth_pct = aggregate.get("growth_pct")
        growth_factor = 1.0 + growth_pct / 100.0 if growth_pct is not None else None
        fair_value_lo = aggregate.get("target_price_q25")
        fair_value_hi = aggregate.get("target_price_q75")
        history.append({
            "date": trade_day.isoformat(),
            "fair_value_lo": fair_value_lo,
            "fair_value_hi": fair_value_hi,
            "target_price_avg": aggregate.get("target_price_avg"),
            "target_price_min": aggregate.get("target_price_min"),
            "target_price_max": aggregate.get("target_price_max"),
            "forward_next_fy_lo": (
                fair_value_lo * growth_factor
                if fair_value_lo is not None and growth_factor is not None else None
            ),
            "forward_next_fy_hi": (
                fair_value_hi * growth_factor
                if fair_value_hi is not None and growth_factor is not None else None
            ),
            "pe_ratio": aggregate.get("consensus_pe"),
            "forward_pe_ratio": aggregate.get("next_consensus_pe"),
            "growth_pct": growth_pct,
            "growth_source": aggregate.get("growth_source"),
            "forecast_year": aggregate.get("forecast_year"),
            "next_forecast_year": aggregate.get("next_forecast_year"),
            "report_count": aggregate.get("report_count"),
            "target_report_count": aggregate.get("target_report_count"),
            "organization_count": aggregate.get("organization_count"),
            "rating": aggregate.get("rating"),
        })
    return history


def load_a_stock_consensus_history(
    db: Any,
    symbol: str,
    *,
    limit: int = 1260,
    report_lookback_days: int = 180,
) -> List[Dict[str, Any]]:
    normalized_symbol = normalize_a_stock_symbol(symbol)
    if not normalized_symbol:
        return []

    normalized_limit = max(1, min(int(limit or 1260), 5000))
    trade_dates = [row[0] for row in db.execute(text("""
        SELECT trade_date
        FROM a_stock_market_daily
        WHERE ts_code = :symbol
        ORDER BY trade_date DESC
        LIMIT :limit
    """), {"symbol": normalized_symbol, "limit": normalized_limit}).all()]
    if not trade_dates:
        return []
    earliest_trade_date = min(trade_dates)
    report_start = earliest_trade_date - timedelta(
        days=max(1, min(int(report_lookback_days or 180), 1095))
    )
    rows = db.execute(
        text(
            """
            SELECT
                ts_code,
                name AS report_name,
                report_date,
                report_title,
                org_name,
                author_name,
                quarter,
                eps,
                pe,
                np,
                rating,
                max_price,
                min_price
            FROM a_stock_report_rc
            WHERE ts_code = :symbol
              AND report_date >= :report_start
            ORDER BY report_date DESC
            """
        ),
        {"symbol": normalized_symbol, "report_start": report_start},
    ).mappings().all()
    return build_a_stock_rolling_consensus_history(
        rows,
        trade_dates,
        report_lookback_days=report_lookback_days,
    )


def load_a_stock_annual_ann_dates(
    db: Any,
    symbols: Sequence[str],
) -> Dict[str, List[date]]:
    """按股票加载历次年报公告日(升序)。

    只认 end_date 为 12-31 的年报；同一财年若有更正/追溯调整产生多条记录，
    取该财年最新的一个公告日，避免重述公告把"上一次年报"挤成同一财年。
    """
    if not symbols:
        return {}
    ann_dates: Dict[str, List[date]] = defaultdict(list)
    for offset in range(0, len(symbols), 500):
        chunk = symbols[offset:offset + 500]
        symbol_params = {f"symbol_{index}": symbol for index, symbol in enumerate(chunk)}
        placeholders = ",".join(f":{key}" for key in symbol_params)
        rows = db.execute(
            text(
                f"""
                SELECT ts_code, end_date, MAX(ann_date) AS ann_date
                FROM a_stock_income
                WHERE ts_code IN ({placeholders})
                  AND ann_date IS NOT NULL
                  AND end_date IS NOT NULL
                  AND strftime(end_date, '%m-%d') = '12-31'
                GROUP BY ts_code, end_date
                """
            ),
            symbol_params,
        ).mappings().all()
        for row in rows:
            ann_date = _row_to_date(row.get("ann_date"))
            symbol = str(row.get("ts_code") or "").strip().upper()
            if ann_date is not None and symbol:
                ann_dates[symbol].append(ann_date)
    return {symbol: sorted(set(values)) for symbol, values in ann_dates.items()}


def _annual_cutoffs_as_of(
    ann_dates: Sequence[date],
    as_of: Optional[date],
) -> Tuple[Optional[date], Optional[date]]:
    """取截至 as_of 已经公告的最近一次/上一次年报公告日。

    历史曲线是逐日回放的，某一天能看到的年报必须是那天之前已经披露的，
    否则会把未来信息漏进当天的共识窗口。
    """
    if not ann_dates or as_of is None:
        return None, None
    disclosed = bisect_right(ann_dates, as_of)
    latest = ann_dates[disclosed - 1] if disclosed >= 1 else None
    previous = ann_dates[disclosed - 2] if disclosed >= 2 else None
    return latest, previous


def load_a_stock_consensus_valuation_map(
    db: Any,
    symbols: Iterable[str],
    *,
    report_lookback_days: int = 180,
) -> Dict[str, Dict[str, Any]]:
    """Load latest A-share consensus target ranges for a constituent universe."""
    normalized_symbols = list(dict.fromkeys(
        normalize_a_stock_symbol(symbol) for symbol in symbols if normalize_a_stock_symbol(symbol)
    ))
    if not normalized_symbols:
        return {}
    latest_trade_date = db.execute(text("SELECT MAX(trade_date) FROM a_stock_market_daily")).scalar()
    if latest_trade_date is None:
        return {}
    start_date = latest_trade_date - timedelta(days=max(1, min(int(report_lookback_days), 1095)))
    rows: List[Mapping[str, Any]] = []
    for offset in range(0, len(normalized_symbols), 500):
        chunk = normalized_symbols[offset:offset + 500]
        symbol_params = {f"symbol_{index}": symbol for index, symbol in enumerate(chunk)}
        placeholders = ",".join(f":{key}" for key in symbol_params)
        rows.extend(db.execute(
            text(
                f"""
                WITH latest_market AS (
                    SELECT ts_code, trade_date, close
                    FROM a_stock_market_daily
                    WHERE trade_date = :latest_trade_date
                )
                SELECT
                    r.ts_code, r.name AS report_name, r.report_date, r.report_title,
                    r.org_name, r.author_name, r.quarter, r.eps, r.pe, r.np,
                    r.rating, r.max_price, r.min_price,
                    m.trade_date, m.close
                FROM a_stock_report_rc r
                JOIN latest_market m ON m.ts_code = r.ts_code
                WHERE r.ts_code IN ({placeholders})
                  AND r.report_date >= :start_date
                ORDER BY r.ts_code, r.report_date DESC
                """
            ),
            {**symbol_params, "latest_trade_date": latest_trade_date, "start_date": start_date},
        ).mappings().all())

    grouped: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("ts_code") or "").upper()].append(row)

    annual_ann_dates = load_a_stock_annual_ann_dates(db, list(grouped))

    result: Dict[str, Dict[str, Any]] = {}
    for symbol, symbol_rows in grouped.items():
        latest_annual, prev_annual = _annual_cutoffs_as_of(
            annual_ann_dates.get(symbol, []),
            latest_trade_date,
        )
        aggregate = _aggregate_report_rows(
            symbol_rows,
            latest_trade_date,
            latest_annual_ann_date=latest_annual,
            prev_annual_ann_date=prev_annual,
        )
        close = _positive_float(symbol_rows[0].get("close"))
        if aggregate is None or close is None:
            continue
        growth_pct = aggregate.get("growth_pct")
        growth_factor = 1.0 + growth_pct / 100.0 if growth_pct is not None else None
        result[symbol] = {
            "symbol": symbol,
            "date": aggregate.get("latest_report_date"),
            "last_price": close,
            "fair_value_lo": aggregate.get("target_price_q25"),
            "fair_value_mid": aggregate.get("target_price_median"),
            "fair_value_hi": aggregate.get("target_price_q75"),
            "forward_next_fy_lo": (
                aggregate["target_price_q25"] * growth_factor if growth_factor is not None else None
            ),
            "forward_next_fy_mid": (
                aggregate["target_price_median"] * growth_factor if growth_factor is not None else None
            ),
            "forward_next_fy_hi": (
                aggregate["target_price_q75"] * growth_factor if growth_factor is not None else None
            ),
            "growth_pct": growth_pct,
            "report_count": aggregate.get("report_count"),
            "target_report_count": aggregate.get("target_report_count"),
            "organization_count": aggregate.get("organization_count"),
            "is_stale": aggregate.get("is_stale"),
        }
    return result


def load_a_stock_consensus_valuation_history_map(
    db: Any,
    symbols: Iterable[str],
    trade_dates: Iterable[date],
    *,
    report_lookback_days: int = 180,
) -> Dict[date, Dict[str, Dict[str, Any]]]:
    """Build point-in-time consensus valuation inputs for historical trade dates."""
    normalized_symbols = list(dict.fromkeys(
        normalized for symbol in symbols
        if (normalized := normalize_a_stock_symbol(symbol))
    ))
    normalized_dates = sorted(set(day for day in trade_dates if day))
    if not normalized_symbols or not normalized_dates:
        return {}

    start_date = normalized_dates[0]
    end_date = normalized_dates[-1]
    report_start = start_date - timedelta(days=max(1, min(int(report_lookback_days), 1095)))
    market_by_date: Dict[date, Dict[str, float]] = defaultdict(dict)
    reports_by_symbol: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)

    for offset in range(0, len(normalized_symbols), 500):
        chunk = normalized_symbols[offset:offset + 500]
        symbol_params = {f"symbol_{index}": symbol for index, symbol in enumerate(chunk)}
        placeholders = ",".join(f":{key}" for key in symbol_params)
        params = {**symbol_params, "start_date": start_date, "end_date": end_date}
        market_rows = db.execute(text(f"""
            SELECT ts_code, trade_date, close
            FROM a_stock_market_daily
            WHERE ts_code IN ({placeholders})
              AND trade_date BETWEEN :start_date AND :end_date
            ORDER BY trade_date, ts_code
        """), params).mappings().all()
        for row in market_rows:
            row_date = _row_to_date(row.get("trade_date"))
            close = _positive_float(row.get("close"))
            if row_date and close is not None:
                market_by_date[row_date][str(row.get("ts_code") or "").upper()] = close

        report_rows = db.execute(text(f"""
            SELECT
                ts_code, name AS report_name, report_date, report_title,
                org_name, author_name, quarter, eps, pe, np, rating,
                max_price, min_price
            FROM a_stock_report_rc
            WHERE ts_code IN ({placeholders})
              AND report_date BETWEEN :report_start AND :end_date
            ORDER BY ts_code, report_date
        """), {**symbol_params, "report_start": report_start, "end_date": end_date}).mappings().all()
        for row in report_rows:
            reports_by_symbol[str(row.get("ts_code") or "").upper()].append(row)

    annual_ann_dates = load_a_stock_annual_ann_dates(db, list(reports_by_symbol))

    result: Dict[date, Dict[str, Dict[str, Any]]] = defaultdict(dict)
    lookback = timedelta(days=max(1, min(int(report_lookback_days), 1095)))
    for symbol, report_rows in reports_by_symbol.items():
        symbol_ann_dates = annual_ann_dates.get(symbol, [])
        dated_rows = [
            (report_day, row)
            for row in report_rows
            if (report_day := _row_to_date(row.get("report_date")))
        ]
        cursor = 0
        active_rows: List[Mapping[str, Any]] = []
        aggregate: Optional[Dict[str, Any]] = None
        cutoffs: Tuple[Optional[date], Optional[date]] = (None, None)
        for trade_day in normalized_dates:
            close = market_by_date.get(trade_day, {}).get(symbol)
            window_start = trade_day - lookback
            changed = False
            while cursor < len(dated_rows) and dated_rows[cursor][0] <= trade_day:
                active_rows.append(dated_rows[cursor][1])
                cursor += 1
                changed = True
            retained_rows = [
                row for row in active_rows
                if (_row_to_date(row.get("report_date")) or date.min) >= window_start
            ]
            if len(retained_rows) != len(active_rows):
                active_rows = retained_rows
                changed = True
            # 年报公告日跨过去时窗口口径变了，即使研报集合没变也要重算。
            day_cutoffs = _annual_cutoffs_as_of(symbol_ann_dates, trade_day)
            if day_cutoffs != cutoffs:
                cutoffs = day_cutoffs
                changed = True
            if changed:
                aggregate = _aggregate_report_rows(
                    active_rows,
                    trade_day,
                    latest_annual_ann_date=cutoffs[0],
                    prev_annual_ann_date=cutoffs[1],
                )
            if close is None or aggregate is None:
                continue
            growth_pct = aggregate.get("growth_pct")
            growth_factor = 1.0 + growth_pct / 100.0 if growth_pct is not None else None
            result[trade_day][symbol] = {
                "symbol": symbol,
                "date": aggregate.get("latest_report_date"),
                "last_price": close,
                "fair_value_lo": aggregate.get("target_price_q25"),
                "fair_value_mid": aggregate.get("target_price_median"),
                "fair_value_hi": aggregate.get("target_price_q75"),
                "forward_next_fy_lo": (
                    aggregate["target_price_q25"] * growth_factor if growth_factor is not None else None
                ),
                "forward_next_fy_mid": (
                    aggregate["target_price_median"] * growth_factor if growth_factor is not None else None
                ),
                "forward_next_fy_hi": (
                    aggregate["target_price_q75"] * growth_factor if growth_factor is not None else None
                ),
                "target_report_count": aggregate.get("target_report_count"),
                "is_stale": aggregate.get("is_stale"),
            }
    return {day: values for day, values in result.items()}


def load_a_stock_klines(
    db: Any,
    symbol: str,
    *,
    start_date: date,
    end_date: date,
) -> List[Dict[str, Any]]:
    normalized_symbol = normalize_a_stock_symbol(symbol)
    if not normalized_symbol or start_date > end_date:
        return []
    def query_rows(table_name: str, volume_column: str, turnover_column: str):
        return db.execute(
            text(
                f"""
            SELECT
                trade_date,
                open,
                high,
                low,
                close,
                {volume_column} AS volume,
                {turnover_column} AS turnover,
                turnover_rate
            FROM {table_name}
            WHERE ts_code = :symbol
              AND trade_date >= :start_date
              AND trade_date <= :end_date
            ORDER BY trade_date
            """
            ),
            {
                "symbol": normalized_symbol,
                "start_date": start_date,
                "end_date": end_date,
            },
        ).mappings().all()

    rows = query_rows("a_stock_market_daily_qfq", "volume", "turnover")
    if not rows:
        rows = query_rows("a_stock_market_daily", "vol", "amount")

    result: List[Dict[str, Any]] = []
    for row in rows:
        trade_date = _row_to_date(row.get("trade_date"))
        if trade_date is None:
            continue
        result.append({
            "timestamp": datetime.combine(trade_date, time(hour=15)),
            "open": _safe_float(row.get("open")) or 0.0,
            "high": _safe_float(row.get("high")) or 0.0,
            "low": _safe_float(row.get("low")) or 0.0,
            "close": _safe_float(row.get("close")) or 0.0,
            "volume": _safe_float(row.get("volume")) or 0.0,
            "turnover": _safe_float(row.get("turnover")) or 0.0,
            "turnover_rate": _safe_float(row.get("turnover_rate")),
        })
    return result


def search_a_stock_consensus_candidates(
    db: Any,
    *,
    symbol: Optional[str] = None,
    min_market_cap_100m: Optional[float] = 100.0,
    max_market_cap_100m: Optional[float] = None,
    min_undervalue_pct: Optional[float] = 10.0,
    min_growth_pct: Optional[float] = 10.0,
    report_lookback_days: int = 180,
    min_report_count: int = 1,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    latest_trade_date = db.execute(text("SELECT MAX(trade_date) FROM a_stock_market_daily")).scalar()
    if latest_trade_date is None:
        return []

    search_text = str(symbol or "").strip()
    search_symbol = normalize_a_stock_symbol(search_text) if _looks_like_a_stock_code(search_text) else ""
    name_search = search_text if search_text and not search_symbol else ""
    has_search = bool(search_text)
    lookback_days = max(1, min(int(report_lookback_days or 180), 1095))
    start_date = latest_trade_date - timedelta(days=lookback_days)
    date_filter = "" if has_search else "AND r.report_date >= :start_date"
    search_filter = ""
    params = {"start_date": start_date, "symbol": search_symbol, "name_pattern": f"%{name_search}%"}
    if search_symbol:
        search_filter = "AND r.ts_code = :symbol"
    elif name_search:
        search_filter = "AND (b.name LIKE :name_pattern OR r.name LIKE :name_pattern)"
    rows = db.execute(
        text(
            f"""
            WITH latest_market AS (
                SELECT *
                FROM a_stock_market_daily
                WHERE trade_date = :latest_trade_date
            ),
            annual_ann AS (
                -- 每个年度(end_date 为 12-31)只保留一个公告日，避免追溯调整/更正
                -- 产生的多条记录把"上一次年报"挤成同一财年的重述公告。
                SELECT ts_code, end_date, MAX(ann_date) AS ann_date
                FROM a_stock_income
                WHERE ann_date IS NOT NULL
                  AND end_date IS NOT NULL
                  AND strftime(end_date, '%m-%d') = '12-31'
                GROUP BY ts_code, end_date
            ),
            annual_ranked AS (
                SELECT
                    ts_code,
                    ann_date,
                    ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY end_date DESC) AS rn
                FROM annual_ann
            ),
            annual_cutoff AS (
                SELECT
                    ts_code,
                    MAX(CASE WHEN rn = 1 THEN ann_date END) AS latest_annual_ann_date,
                    MAX(CASE WHEN rn = 2 THEN ann_date END) AS prev_annual_ann_date
                FROM annual_ranked
                WHERE rn <= 2
                GROUP BY ts_code
            )
            SELECT
                r.ts_code,
                r.name AS report_name,
                r.report_date,
                r.report_title,
                r.org_name,
                r.author_name,
                r.quarter,
                r.eps,
                r.pe,
                r.np,
                r.rating,
                r.max_price,
                r.min_price,
                b.name AS stock_name,
                b.industry,
                b.market,
                m.trade_date,
                m.close,
                m.total_mv,
                m.circ_mv,
                a.latest_annual_ann_date,
                a.prev_annual_ann_date
            FROM a_stock_report_rc r
            JOIN latest_market m ON m.ts_code = r.ts_code
            LEFT JOIN a_stock_basic b ON b.ts_code = r.ts_code
            LEFT JOIN annual_cutoff a ON a.ts_code = r.ts_code
            WHERE 1 = 1
              {date_filter}
              {search_filter}
            ORDER BY r.ts_code, r.report_date DESC
            """
        ),
        {**params, "latest_trade_date": latest_trade_date},
    ).mappings().all()

    return build_a_stock_consensus_candidates(
        rows,
        latest_trade_date,
        search_symbol=search_symbol,
        has_search=has_search,
        min_market_cap_100m=min_market_cap_100m,
        max_market_cap_100m=max_market_cap_100m,
        min_undervalue_pct=min_undervalue_pct,
        min_growth_pct=min_growth_pct,
        min_report_count=min_report_count,
        limit=limit,
    )

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import text


A_STOCK_MARKET_CAP_UNIT = 10_000.0
FLOAT_COMPARE_EPSILON = 1e-9


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


def _target_price(row: Mapping[str, Any]) -> Optional[float]:
    min_price = _positive_float(row.get("min_price"))
    max_price = _positive_float(row.get("max_price"))
    if min_price is not None and max_price is not None:
        return (min_price + max_price) / 2.0
    return max_price if max_price is not None else min_price


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


def _aggregate_report_rows(
    rows: Sequence[Mapping[str, Any]],
    latest_trade_date: Optional[date],
) -> Optional[Dict[str, Any]]:
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

        target = _target_price(row)
        if target is not None and key not in target_prices:
            target_prices[key] = target
        min_price = _positive_float(row.get("min_price"))
        max_price = _positive_float(row.get("max_price"))
        if min_price is not None and key not in target_lows:
            target_lows[key] = min_price
        if max_price is not None and key not in target_highs:
            target_highs[key] = max_price

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
        "target_price_min": min(target_lows.values()) if target_lows else min(target_prices.values()),
        "target_price_max": max(target_highs.values()) if target_highs else max(target_prices.values()),
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

        aggregate = _aggregate_report_rows(symbol_rows, latest_trade_date)
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


def load_a_stock_consensus_history(db: Any, symbol: str, *, limit: int = 1260) -> List[Dict[str, Any]]:
    normalized_symbol = normalize_a_stock_symbol(symbol)
    if not normalized_symbol:
        return []

    latest_trade_date = db.execute(text("SELECT MAX(trade_date) FROM a_stock_market_daily")).scalar()
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
            ORDER BY report_date DESC
            """
        ),
        {"symbol": normalized_symbol},
    ).mappings().all()
    return build_a_stock_consensus_history(rows, latest_trade_date, limit=limit)


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

    result: Dict[str, Dict[str, Any]] = {}
    for symbol, symbol_rows in grouped.items():
        aggregate = _aggregate_report_rows(symbol_rows, latest_trade_date)
        close = _positive_float(symbol_rows[0].get("close"))
        if aggregate is None or close is None:
            continue
        growth_pct = aggregate.get("growth_pct")
        growth_factor = 1.0 + growth_pct / 100.0 if growth_pct is not None else None
        result[symbol] = {
            "symbol": symbol,
            "date": aggregate.get("latest_report_date"),
            "last_price": close,
            "fair_value_lo": aggregate.get("target_price_min"),
            "fair_value_hi": aggregate.get("target_price_max"),
            "forward_next_fy_lo": (
                aggregate["target_price_min"] * growth_factor if growth_factor is not None else None
            ),
            "forward_next_fy_hi": (
                aggregate["target_price_max"] * growth_factor if growth_factor is not None else None
            ),
            "growth_pct": growth_pct,
            "report_count": aggregate.get("report_count"),
            "organization_count": aggregate.get("organization_count"),
        }
    return result


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
                m.circ_mv
            FROM a_stock_report_rc r
            JOIN latest_market m ON m.ts_code = r.ts_code
            LEFT JOIN a_stock_basic b ON b.ts_code = r.ts_code
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

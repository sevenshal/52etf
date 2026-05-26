from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import math
import os
import re
import sys
import traceback
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

import httpx

from ..core.database import (
    SessionLocal,
    SnowballAccountConfig,
    XueqiuCubeRankCache,
    XueqiuTopHoldingsRun,
)
from ..core.utils import send_alert_email, sendmail


ROOT = Path(__file__).resolve().parents[2]
CHINA_TZ = ZoneInfo("Asia/Shanghai")
XUEQIU_API_BASE_URL = "https://api.xueqiu.com"
XUEQIU_STOCK_BASE_URL = "https://stock.xueqiu.com"
XUEQIU_WEB_BASE_URL = "https://xueqiu.com"
RANK_CACHE_TYPE = "year"
RANK_CACHE_TTL_DAYS = 30
RANK_PAGE_SIZE = 20
RANK_TARGET_COUNT = 1000
DEFAULT_TARGET_CUBE_SYMBOL = "ZH3630096"
DEFAULT_TARGET_CUBE_ID = 3664154
DEFAULT_OUTPUT_DIR = ROOT / "lab" / "output" / "xueqiu_top_holdings"
DEFAULT_RECEIVER_EMAIL = "405290618@qq.com"
DEFAULT_WORKERS = 8
DEFAULT_TIMEOUT = 15.0
DEFAULT_RETRIES = 3
XUEQIU_REBALANCE_ALLOWED_QUOTE_TYPES = {11, 82}
REBALANCE_QUOTE_BATCH_SIZE = 50


logger = logging.getLogger("xueqiu_top_holdings_report")


@dataclass
class CubeInfo:
    year_rank: Optional[int]
    symbol: str
    cube_name: str = ""
    screen_name: str = ""
    cube_id: Optional[int] = None
    daily_gain: Optional[float] = None
    week_gain: Optional[float] = None
    year_gain: Optional[float] = None
    recommend_count: Optional[int] = None
    net_value: Optional[float] = None
    raw_data: Optional[Dict[str, Any]] = None


@dataclass
class CubeFetchResult:
    cube: CubeInfo
    holdings: List[Dict[str, Any]]
    error: Optional[str] = None


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
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_xueqiu_symbol(symbol: Any) -> Optional[str]:
    text = str(symbol or "").strip().upper()
    if not text or text in {"CASH", "CN_CASH", "USD", "HKD"}:
        return None
    text = text.replace("_", ".")
    if "." in text:
        prefix, code = text.split(".", 1)
        if prefix in {"SH", "SZ", "BJ"} and re.fullmatch(r"\d{6}", code):
            return f"{prefix}.{code}"
        if code in {"SH", "SZ", "BJ"} and re.fullmatch(r"\d{6}", prefix):
            return f"{code}.{prefix}"
        return text
    if len(text) == 8 and text[:2] in {"SH", "SZ", "BJ"} and text[2:].isdigit():
        return f"{text[:2]}.{text[2:]}"
    return text


def to_raw_xueqiu_symbol(symbol: Any) -> str:
    normalized = normalize_xueqiu_symbol(symbol)
    if not normalized:
        return ""
    return normalized.replace(".", "")


def get_holding_name(holding: Dict[str, Any]) -> str:
    for key in ("stockName", "stock_name", "name", "stockNameCN"):
        value = holding.get(key)
        if value:
            return str(value)
    return ""


def get_holding_weight(holding: Dict[str, Any]) -> Optional[float]:
    for key in ("weight", "target_weight", "targetWeight"):
        number = safe_float(holding.get(key))
        if number is not None:
            return number
    return None


def get_latest_cookie() -> str:
    db = SessionLocal()
    try:
        rows = (
            db.query(SnowballAccountConfig)
            .filter(SnowballAccountConfig.xueqiu_cookie.isnot(None))
            .order_by(SnowballAccountConfig.updated_at.desc())
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


def build_headers(
    cookie: str,
    *,
    host: str = "api.xueqiu.com",
    referer: Optional[str] = None,
    content_type: Optional[str] = None,
) -> Dict[str, str]:
    headers = {
        "Host": host,
        "Cookie": cookie if "xq_a_token=" in cookie else f"xq_a_token={cookie};",
        "accept": "application/json",
        "accept-language": "zh-Hans-CN;q=1, en-CN;q=0.9",
        "x-device-os": "iOS 26.4.2",
        "x-device-model-name": "iPhone 16 Pro Max_iPhone17,2",
        "user-agent": "Xueqiu iPhone 14.90.2",
        "priority": "u=3, i",
    }
    if referer:
        headers["Referer"] = referer
    if content_type:
        headers["content-type"] = content_type
    return headers


def cube_from_rank_item(item: Dict[str, Any], rank: int) -> CubeInfo:
    return CubeInfo(
        year_rank=rank,
        symbol=str(item.get("symbol") or "").strip().upper(),
        cube_id=safe_int(item.get("cube_id")),
        cube_name=str(item.get("cube_name") or ""),
        screen_name=str(item.get("screen_name") or ""),
        daily_gain=safe_float(item.get("daily_gain")),
        week_gain=safe_float(item.get("week_gain")),
        year_gain=safe_float(item.get("year_gain")),
        recommend_count=safe_int(item.get("recommend_count")),
        net_value=safe_float(item.get("net_value")),
        raw_data=item,
    )


def cube_from_cache_row(row: XueqiuCubeRankCache) -> CubeInfo:
    return CubeInfo(
        year_rank=row.year_rank,
        symbol=row.symbol,
        cube_id=row.cube_id,
        cube_name=row.cube_name or "",
        screen_name=row.screen_name or "",
        daily_gain=row.daily_gain,
        week_gain=row.week_gain,
        year_gain=row.year_gain,
        recommend_count=row.recommend_count,
        net_value=row.net_value,
        raw_data=row.raw_data or {},
    )


def load_cubes_from_file(path: Path, limit: Optional[int] = None) -> List[CubeInfo]:
    items = json.loads(path.read_text(encoding="utf-8"))
    cubes: List[CubeInfo] = []
    for index, item in enumerate(items, start=1):
        rank = safe_int(item.get("year_rank")) or index
        cube = cube_from_rank_item(item, rank)
        if not cube.symbol:
            continue
        cubes.append(cube)
        if limit and len(cubes) >= limit:
            break
    return cubes


async def fetch_year_top_cubes(
    *,
    cookie: str,
    target_count: int = RANK_TARGET_COUNT,
    page_size: int = RANK_PAGE_SIZE,
    timeout: float = DEFAULT_TIMEOUT,
) -> List[CubeInfo]:
    headers = build_headers(cookie)
    cubes: List[CubeInfo] = []
    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(timeout)) as client:
        page = 1
        while len(cubes) < target_count:
            response = await client.get(
                f"{XUEQIU_API_BASE_URL}/cube/center/cube_found/ring-list.json",
                params={
                    "page": page,
                    "size": page_size,
                    "type": "year",
                    "_": int(datetime.now(CHINA_TZ).timestamp() * 1000),
                },
            )
            response.raise_for_status()
            payload = response.json()
            batch = ((payload.get("data") or {}).get("list") or [])
            if not batch:
                break
            for item in batch:
                cube = cube_from_rank_item(item, len(cubes) + 1)
                if cube.symbol:
                    cubes.append(cube)
                if len(cubes) >= target_count:
                    break
            if len(batch) < page_size:
                break
            page += 1
            await asyncio.sleep(0.08)
    if len(cubes) < target_count:
        raise RuntimeError(f"Xueqiu year rank returned only {len(cubes)} cubes, expected {target_count}.")
    return cubes[:target_count]


def load_cached_year_top_cubes(
    *,
    limit: int = RANK_TARGET_COUNT,
    max_age_days: int = RANK_CACHE_TTL_DAYS,
) -> Tuple[List[CubeInfo], Optional[datetime]]:
    db = SessionLocal()
    try:
        latest = (
            db.query(XueqiuCubeRankCache)
            .filter(XueqiuCubeRankCache.rank_type == RANK_CACHE_TYPE)
            .order_by(XueqiuCubeRankCache.fetched_at.desc())
            .first()
        )
        if not latest or not latest.fetched_at:
            return [], None
        if latest.fetched_at < datetime.now() - timedelta(days=max_age_days):
            return [], latest.fetched_at
        rows = (
            db.query(XueqiuCubeRankCache)
            .filter(
                XueqiuCubeRankCache.rank_type == RANK_CACHE_TYPE,
                XueqiuCubeRankCache.fetched_at == latest.fetched_at,
            )
            .order_by(XueqiuCubeRankCache.year_rank.asc())
            .limit(limit)
            .all()
        )
        if len(rows) < limit:
            return [], latest.fetched_at
        return [cube_from_cache_row(row) for row in rows], latest.fetched_at
    finally:
        db.close()


def save_year_top_cubes(cubes: List[CubeInfo], fetched_at: datetime) -> None:
    db = SessionLocal()
    try:
        db.query(XueqiuCubeRankCache).filter(
            XueqiuCubeRankCache.rank_type == RANK_CACHE_TYPE
        ).delete(synchronize_session=False)
        for cube in cubes:
            db.add(
                XueqiuCubeRankCache(
                    rank_type=RANK_CACHE_TYPE,
                    year_rank=cube.year_rank or 0,
                    symbol=cube.symbol,
                    cube_id=cube.cube_id,
                    cube_name=cube.cube_name,
                    screen_name=cube.screen_name,
                    daily_gain=cube.daily_gain,
                    week_gain=cube.week_gain,
                    year_gain=cube.year_gain,
                    recommend_count=cube.recommend_count,
                    net_value=cube.net_value,
                    raw_data=cube.raw_data or {},
                    fetched_at=fetched_at,
                )
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def load_or_refresh_year_top_cubes(
    *,
    cookie: str,
    force_refresh: bool = False,
    limit: int = RANK_TARGET_COUNT,
    timeout: float = DEFAULT_TIMEOUT,
) -> Tuple[List[CubeInfo], datetime, bool]:
    if not force_refresh:
        cached, fetched_at = load_cached_year_top_cubes(limit=limit)
        if cached and fetched_at:
            return cached, fetched_at, False

    fetched_at = datetime.now()
    cubes = await fetch_year_top_cubes(cookie=cookie, target_count=limit, timeout=timeout)
    save_year_top_cubes(cubes, fetched_at)
    return cubes, fetched_at, True


def is_china_trading_day(check_date: date) -> bool:
    if check_date.weekday() >= 5:
        return False
    try:
        from ..core.services.tushare import TushareService

        calendar = TushareService.get_instance().get_trade_calendar_frame(check_date, check_date)
        if not calendar.empty:
            row = calendar.iloc[0]
            return int(row.get("is_open") or 0) == 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("A-share trading calendar check failed for %s: %s", check_date, exc)
    return True


async def fetch_cube_holdings(
    client: httpx.AsyncClient,
    cube: CubeInfo,
    *,
    semaphore: asyncio.Semaphore,
    retries: int,
) -> CubeFetchResult:
    url = f"{XUEQIU_API_BASE_URL}/cube/center/cube/holdSymbols.json"
    last_error: Optional[BaseException] = None
    async with semaphore:
        for attempt in range(1, retries + 1):
            try:
                response = await client.get(url, params={"symbol": cube.symbol})
                response.raise_for_status()
                payload = response.json()
                if payload.get("result_code") == 0 and payload.get("success"):
                    holdings = payload.get("data") or []
                    if not isinstance(holdings, list):
                        raise ValueError(f"Unexpected holdings payload: {payload}")
                    return CubeFetchResult(cube=cube, holdings=holdings)
                raise ValueError(payload.get("message") or f"Xueqiu API error: {payload}")
            except BaseException as exc:  # noqa: BLE001
                last_error = exc
                if attempt < retries:
                    await asyncio.sleep(min(6.0, 0.8 * attempt))
        return CubeFetchResult(cube=cube, holdings=[], error=repr(last_error))


async def fetch_all_holdings(
    cubes: List[CubeInfo],
    *,
    cookie: str,
    workers: int,
    timeout: float,
    retries: int,
) -> List[CubeFetchResult]:
    headers = build_headers(cookie)
    timeout_config = httpx.Timeout(timeout)
    limits = httpx.Limits(max_connections=max(workers, 1), max_keepalive_connections=max(workers, 1))
    semaphore = asyncio.Semaphore(max(workers, 1))
    async with httpx.AsyncClient(headers=headers, timeout=timeout_config, limits=limits) as client:
        tasks = [
            fetch_cube_holdings(client, cube, semaphore=semaphore, retries=retries)
            for cube in cubes
        ]
        results: List[CubeFetchResult] = []
        for index, task in enumerate(asyncio.as_completed(tasks), start=1):
            result = await task
            results.append(result)
            if index % 100 == 0:
                logger.info("Fetched holdings for %s/%s cubes", index, len(cubes))
        return results


def aggregate_holdings(results: Iterable[CubeFetchResult]) -> Dict[str, Any]:
    stock_to_cubes: Dict[str, Set[str]] = defaultdict(set)
    stock_names: Dict[str, str] = {}
    stock_total_weight: Dict[str, float] = defaultdict(float)
    stock_cube_examples: Dict[str, List[str]] = defaultdict(list)
    holding_rows: List[Dict[str, Any]] = []
    success_count = 0
    failed_results: List[CubeFetchResult] = []

    for result in results:
        if result.error:
            failed_results.append(result)
            continue
        success_count += 1
        seen_symbols: Set[str] = set()
        for holding in result.holdings:
            symbol = normalize_xueqiu_symbol(holding.get("symbol"))
            weight = get_holding_weight(holding)
            if not symbol or weight is None or weight <= 0:
                continue
            name = get_holding_name(holding)
            holding_rows.append(
                {
                    "cube_symbol": result.cube.symbol,
                    "cube_name": result.cube.cube_name,
                    "year_rank": result.cube.year_rank,
                    "stock_symbol": symbol,
                    "stock_name": name,
                    "weight_pct": weight,
                }
            )
            if symbol in seen_symbols:
                continue
            seen_symbols.add(symbol)
            stock_to_cubes[symbol].add(result.cube.symbol)
            stock_total_weight[symbol] += weight
            if name and symbol not in stock_names:
                stock_names[symbol] = name
            if len(stock_cube_examples[symbol]) < 5:
                stock_cube_examples[symbol].append(result.cube.cube_name or result.cube.symbol)

    ranking = []
    for symbol, cube_symbols in stock_to_cubes.items():
        cube_count = len(cube_symbols)
        total_weight = stock_total_weight[symbol]
        ranking.append(
            {
                "stock_symbol": symbol,
                "stock_name": stock_names.get(symbol, ""),
                "holding_cube_count": cube_count,
                "holding_cube_ratio_pct": cube_count / success_count * 100.0 if success_count else None,
                "total_weight_pct": total_weight,
                "composite_weight_pct": total_weight / success_count if success_count else None,
                "average_weight_pct": total_weight / cube_count if cube_count else None,
                "example_cubes": stock_cube_examples.get(symbol, []),
            }
        )

    total_stock_weight_pct = sum(item["total_weight_pct"] for item in ranking)
    for item in ranking:
        item["global_normalized_weight_pct"] = (
            item["total_weight_pct"] / total_stock_weight_pct * 100.0
            if total_stock_weight_pct > 0
            else None
        )
    ranking.sort(
        key=lambda item: (
            item["total_weight_pct"],
            item["holding_cube_count"],
            item["stock_symbol"],
        ),
        reverse=True,
    )
    return {
        "success_count": success_count,
        "failed_results": failed_results,
        "holding_rows": holding_rows,
        "ranking": ranking,
        "total_stock_weight_pct": total_stock_weight_pct,
    }


def fmt_number(value: Any, digits: int = 2, suffix: str = "") -> str:
    number = safe_float(value)
    if number is None:
        return "-"
    return f"{number:.{digits}f}{suffix}"


def add_top_normalized_weights(ranking: List[Dict[str, Any]], top_n: int) -> List[Dict[str, Any]]:
    selected = [dict(item) for item in ranking[:top_n]]
    selected_weight = sum(
        safe_float(item.get("composite_weight_pct")) or 0.0
        for item in selected
    )
    for item in selected:
        weight = safe_float(item.get("composite_weight_pct")) or 0.0
        item["top_normalized_weight_pct"] = (
            weight / selected_weight * 100.0
            if selected_weight > 0
            else None
        )
    return selected


def rounded_rebalance_weights(top_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    selected = [dict(item) for item in top_items]
    if not selected:
        return selected
    rounded_weights = [
        round(safe_float(item.get("top_normalized_weight_pct")) or 0.0, 2)
        for item in selected
    ]
    delta = round(100.0 - sum(rounded_weights), 2)
    rounded_weights[0] = round(rounded_weights[0] + delta, 2)
    for item, weight in zip(selected, rounded_weights):
        item["rebalance_weight_pct"] = weight
    return selected


async def fetch_batch_quotes(
    *,
    cookie: str,
    symbols: List[str],
    timeout: float = DEFAULT_TIMEOUT,
) -> Dict[str, Dict[str, Any]]:
    if not symbols:
        return {}
    raw_symbols = [to_raw_xueqiu_symbol(symbol) for symbol in symbols if to_raw_xueqiu_symbol(symbol)]
    headers = build_headers(cookie, host="stock.xueqiu.com")
    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(timeout)) as client:
        response = await client.get(
            f"{XUEQIU_STOCK_BASE_URL}/v5/stock/batch/quote.json",
            params={"symbol": ",".join(raw_symbols)},
        )
        response.raise_for_status()
        payload = response.json()
    result: Dict[str, Dict[str, Any]] = {}
    for item in ((payload.get("data") or {}).get("items") or []):
        quote = item.get("quote") or {}
        raw_symbol = quote.get("symbol")
        if not raw_symbol:
            continue
        result[str(raw_symbol).upper()] = {
            "price": safe_float(quote.get("current")),
            "name": quote.get("name") or "",
            "quote": quote,
        }
    return result


def describe_rebalance_quote_rejection(raw_symbol: str, quote: Dict[str, Any]) -> Optional[str]:
    quote_type = safe_int(quote.get("type"))
    price = safe_float(quote.get("current"))
    status = safe_int(quote.get("status"))
    if quote_type not in XUEQIU_REBALANCE_ALLOWED_QUOTE_TYPES:
        return f"quote_type={quote_type} not allowed"
    if not price or price <= 0:
        return "missing valid current price"
    if status is not None and status <= 0:
        return f"status={status}"
    if not raw_symbol:
        return "missing symbol"
    return None


async def select_rebalance_top_items(
    *,
    cookie: str,
    ranking: List[Dict[str, Any]],
    top_n: int,
    timeout: float = DEFAULT_TIMEOUT,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    selected: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    candidates = [item for item in ranking if to_raw_xueqiu_symbol(item.get("stock_symbol"))]

    for start in range(0, len(candidates), REBALANCE_QUOTE_BATCH_SIZE):
        chunk = candidates[start:start + REBALANCE_QUOTE_BATCH_SIZE]
        quotes = await fetch_batch_quotes(
            cookie=cookie,
            symbols=[item["stock_symbol"] for item in chunk],
            timeout=timeout,
        )
        for item in chunk:
            raw_symbol = to_raw_xueqiu_symbol(item.get("stock_symbol"))
            quote = (quotes.get(raw_symbol) or {}).get("quote") or {}
            rejection = describe_rebalance_quote_rejection(raw_symbol, quote)
            if rejection:
                skipped_item = dict(item)
                skipped_item["rebalance_skip_reason"] = rejection
                skipped_item["rebalance_quote_type"] = safe_int(quote.get("type"))
                skipped.append(skipped_item)
                continue
            selected.append(dict(item))
            if len(selected) >= top_n:
                return add_top_normalized_weights(selected, top_n), skipped

    raise RuntimeError(
        f"Unable to select {top_n} Xueqiu-rebalanceable stocks; selected={len(selected)} skipped={len(skipped)}"
    )


async def fetch_stock_metadata(
    *,
    cookie: str,
    symbol: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    raw_symbol = to_raw_xueqiu_symbol(symbol)
    headers = build_headers(
        cookie,
        host="xueqiu.com",
        referer=f"{XUEQIU_WEB_BASE_URL}/S/{raw_symbol}",
    )
    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(timeout), follow_redirects=True) as client:
        response = await client.get(
            f"{XUEQIU_WEB_BASE_URL}/query/v1/search/stock.json",
            params={"code": raw_symbol, "size": 10, "page": 1},
        )
        response.raise_for_status()
        payload = response.json()
    stocks = payload.get("stocks") or []
    exact = None
    for stock in stocks:
        if str(stock.get("code") or "").upper() == raw_symbol:
            exact = stock
            break
    exact = exact or (stocks[0] if stocks else {})
    return {
        "stock_id": safe_int(exact.get("stock_id")),
        "stock_name": exact.get("name") or "",
        "segment_name": exact.get("ind_name") or "",
        "raw": exact,
    }


async def fetch_stock_metadata_map(
    *,
    cookie: str,
    symbols: List[str],
    timeout: float = DEFAULT_TIMEOUT,
) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for symbol in symbols:
        raw_symbol = to_raw_xueqiu_symbol(symbol)
        if not raw_symbol:
            continue
        try:
            result[raw_symbol] = await fetch_stock_metadata(
                cookie=cookie,
                symbol=raw_symbol,
                timeout=timeout,
            )
            await asyncio.sleep(0.05)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to fetch stock metadata for %s: %s", raw_symbol, exc)
            result[raw_symbol] = {}
    return result


async def resolve_target_cube_id(
    *,
    cookie: str,
    target_cube_symbol: str,
    fallback_cube_id: Optional[int],
    timeout: float = DEFAULT_TIMEOUT,
) -> int:
    event = await fetch_latest_target_rebalance(
        cookie=cookie,
        target_cube_symbol=target_cube_symbol,
        timeout=timeout,
    )
    if event:
        cube_id = safe_int(event.get("cube_id"))
        if cube_id:
            return cube_id
    if fallback_cube_id:
        return int(fallback_cube_id)
    raise RuntimeError(f"Unable to resolve cube_id for {target_cube_symbol}")


async def fetch_latest_target_rebalance(
    *,
    cookie: str,
    target_cube_symbol: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> Optional[Dict[str, Any]]:
    headers = build_headers(
        cookie,
        referer=f"{XUEQIU_WEB_BASE_URL}/P/{target_cube_symbol}",
    )
    try:
        async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(timeout)) as client:
            response = await client.get(
                f"{XUEQIU_API_BASE_URL}/cubes/rebalancing/history.json",
                params={"cube_symbol": target_cube_symbol, "count": 1, "page": 1},
            )
            response.raise_for_status()
            payload = response.json()
        events = payload.get("list") or []
        return events[0] if events else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to load latest target rebalance for %s: %s", target_cube_symbol, exc)
        return None


async def build_rebalance_payload(
    *,
    cookie: str,
    target_cube_symbol: str,
    target_cube_id: int,
    top_items: List[Dict[str, Any]],
    timeout: float = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    rebalance_items = rounded_rebalance_weights(top_items)
    symbols = [item["stock_symbol"] for item in rebalance_items]
    quotes = await fetch_batch_quotes(cookie=cookie, symbols=symbols, timeout=timeout)
    metadata_map = await fetch_stock_metadata_map(cookie=cookie, symbols=symbols, timeout=timeout)

    holdings: List[Dict[str, Any]] = []
    for item in rebalance_items:
        raw_symbol = to_raw_xueqiu_symbol(item["stock_symbol"])
        weight = safe_float(item.get("rebalance_weight_pct")) or 0.0
        quote = quotes.get(raw_symbol) or {}
        metadata = metadata_map.get(raw_symbol) or {}
        quote_body = (quote.get("quote") or {})
        rejection = describe_rebalance_quote_rejection(raw_symbol, quote_body)
        if rejection:
            raise RuntimeError(f"Unsupported Xueqiu rebalance stock {raw_symbol}: {rejection}")
        price = safe_float(quote.get("price"))
        if not price or price <= 0:
            raise RuntimeError(f"Missing valid current price for {raw_symbol}")
        stock_name = metadata.get("stock_name") or quote.get("name") or item.get("stock_name") or ""
        volume = round((weight / 100.0) / price, 12)
        holding = {
            "stock_symbol": raw_symbol,
            "volume": volume,
            "weight": weight,
            "proactive": True,
            "stock_name": stock_name,
        }
        stock_id = metadata.get("stock_id")
        if stock_id:
            holding["stock_id"] = stock_id
        segment_name = metadata.get("segment_name") or item.get("segment_name") or "其他"
        holding["segment_name"] = segment_name
        holdings.append(holding)
        item["rebalance_weight_pct"] = weight
        item["rebalance_volume"] = volume
        item["rebalance_price"] = price
        item["stock_name"] = stock_name or item.get("stock_name")
        item["stock_id"] = stock_id
        item["segment_name"] = segment_name

    cash_pct = round(100.0 - sum(safe_float(row.get("weight")) or 0.0 for row in holdings), 2)
    if abs(cash_pct) < 0.005:
        cash_pct = 0.0
    if cash_pct < 0:
        holdings[0]["weight"] = round((safe_float(holdings[0]["weight"]) or 0.0) + cash_pct, 2)
        cash_pct = 0.0

    return {
        "target_cube_symbol": target_cube_symbol,
        "cube_id": target_cube_id,
        "cash": cash_pct,
        "comment": f"自动按雪球年榜Top1000综合持仓权重调仓 {datetime.now(CHINA_TZ).strftime('%Y-%m-%d %H:%M')}",
        "market": "cn",
        "holdings": holdings,
        "top_items": rebalance_items,
    }


async def create_xueqiu_rebalance(
    *,
    cookie: str,
    payload: Dict[str, Any],
    dry_run: bool,
    timeout: float = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    if dry_run:
        return {
            "dry_run": True,
            "message": "dry-run only; no rebalance request sent",
            "payload": payload,
        }

    headers = build_headers(
        cookie,
        referer=f"{XUEQIU_WEB_BASE_URL}/P/{payload['target_cube_symbol']}",
        content_type="application/x-www-form-urlencoded",
    )
    form_data = {
        "cash": str(payload["cash"]),
        "comment": payload.get("comment") or "",
        "cube_id": str(payload["cube_id"]),
        "holdings": json.dumps(payload["holdings"], ensure_ascii=False, separators=(",", ":")),
        "market": payload.get("market") or "cn",
    }
    request_debug = {
        "cube_id": payload.get("cube_id"),
        "cash": payload.get("cash"),
        "market": payload.get("market") or "cn",
        "holdings": payload.get("holdings") or [],
    }
    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(timeout)) as client:
        response = await client.post(
            f"{XUEQIU_API_BASE_URL}/cubes/rebalancing/create.json",
            params={"_": int(datetime.now(CHINA_TZ).timestamp() * 1000)},
            data=form_data,
        )
        try:
            result = response.json()
        except ValueError:
            result = {"raw_response": response.text}
        if response.status_code >= 400:
            raise RuntimeError(
                "Xueqiu rebalance HTTP "
                f"{response.status_code}: {json.dumps(result, ensure_ascii=False)}; "
                f"request={json.dumps(request_debug, ensure_ascii=False, separators=(',', ':'))}"
            )
    if isinstance(result, dict) and result.get("error_code") and not result.get("id"):
        raise RuntimeError(f"Xueqiu rebalance failed: {result}")
    return result


def build_report(
    *,
    run_at: datetime,
    cubes: List[CubeInfo],
    aggregate: Dict[str, Any],
    top_n: int,
    rank_cache_fetched_at: Optional[datetime],
    rank_cache_refreshed: bool,
    rebalance_skipped_items: Optional[List[Dict[str, Any]]] = None,
    target_cube_symbol: Optional[str] = None,
    rebalance_payload: Optional[Dict[str, Any]] = None,
    rebalance_response: Optional[Dict[str, Any]] = None,
    dry_run: bool = False,
) -> str:
    ranking = aggregate["ranking"]
    top_items = (
        rebalance_payload.get("top_items")
        if rebalance_payload
        else add_top_normalized_weights(ranking, top_n)
    )
    failed_results = aggregate["failed_results"]
    success_count = aggregate["success_count"]
    total_stock_weight_pct = safe_float(aggregate.get("total_stock_weight_pct")) or 0.0
    lines = [
        f"雪球年榜1000组合综合持仓权重 Top{top_n}",
        "",
        f"统计时间: {run_at.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"年榜缓存时间: {rank_cache_fetched_at.strftime('%Y-%m-%d %H:%M:%S') if rank_cache_fetched_at else '-'}",
        f"年榜本次刷新: {'是' if rank_cache_refreshed else '否'}",
        f"组合总数: {len(cubes)}",
        f"拉取成功: {success_count}",
        f"拉取失败: {len(failed_results)}",
        f"覆盖股票数: {len(ranking)}",
        f"非现金持仓合计权重: {fmt_number(total_stock_weight_pct / success_count if success_count else None, suffix='%')}",
        "",
        "统计口径: 把成功拉取的组合等权合成一个组合；个股综合权重 = 该股票在所有成功组合中的持仓权重之和 / 成功组合数，未持有记为 0。",
        f"最终权重: 选取综合权重最高的 Top{top_n} 后，在 Top{top_n} 内按综合权重重新归一化到 100%。",
    ]
    if rebalance_payload:
        status = "dry-run" if dry_run else "已提交"
        if isinstance(rebalance_response, dict) and rebalance_response.get("skipped"):
            status = "已跳过"
        elif isinstance(rebalance_response, dict) and rebalance_response.get("status"):
            status = str(rebalance_response.get("status"))
        lines.extend(
            [
                "",
                f"目标组合: {target_cube_symbol or rebalance_payload.get('target_cube_symbol')} / cube_id={rebalance_payload.get('cube_id')}",
                f"调仓状态: {status}",
                f"目标现金: {fmt_number(rebalance_payload.get('cash'), suffix='%')}",
            ]
        )
        if isinstance(rebalance_response, dict) and rebalance_response.get("error_message"):
            lines.append(f"雪球提示: {rebalance_response.get('error_message')}")
        if isinstance(rebalance_response, dict) and rebalance_response.get("message"):
            lines.append(f"任务提示: {rebalance_response.get('message')}")
        if rebalance_skipped_items:
            skipped_preview = [
                f"{item.get('stock_symbol')}({item.get('stock_name') or ''}, {item.get('rebalance_skip_reason')})"
                for item in rebalance_skipped_items[:10]
            ]
            lines.append(f"已跳过不可调仓标的: {'; '.join(skipped_preview)}")
    lines.extend(
        [
            "",
            "| 排名 | 股票 | 名称 | 综合权重 | 最终归一权重 | 调仓权重 | 持仓组合数 | 占成功组合 | 持有组合平均权重 | 示例组合 |",
            "| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for index, item in enumerate(top_items[:top_n], start=1):
        lines.append(
            "| "
            + " | ".join(
                [
                    str(index),
                    item["stock_symbol"],
                    str(item.get("stock_name") or ""),
                    fmt_number(item.get("composite_weight_pct"), suffix="%"),
                    fmt_number(item.get("top_normalized_weight_pct"), suffix="%"),
                    fmt_number(item.get("rebalance_weight_pct"), suffix="%"),
                    str(item["holding_cube_count"]),
                    fmt_number(item.get("holding_cube_ratio_pct"), suffix="%"),
                    fmt_number(item.get("average_weight_pct"), suffix="%"),
                    "、".join(item.get("example_cubes") or []),
                ]
            )
            + " |"
        )

    if failed_results:
        lines.extend(["", "拉取失败样例:"])
        for result in failed_results[:10]:
            lines.append(f"- {result.cube.symbol} {result.cube.cube_name}: {result.error}")
    lines.append("")
    return "\n".join(lines)


def write_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    field: "、".join(row.get(field) or [])
                    if field == "example_cubes"
                    else row.get(field)
                    for field in fieldnames
                }
            )


def write_outputs(
    *,
    output_dir: Path,
    run_at: datetime,
    cubes: List[CubeInfo],
    aggregate: Dict[str, Any],
    report_text: str,
    top_items: List[Dict[str, Any]],
    rebalance_payload: Optional[Dict[str, Any]],
    rebalance_response: Optional[Dict[str, Any]],
    rebalance_skipped_items: Optional[List[Dict[str, Any]]],
    rank_cache_fetched_at: Optional[datetime],
    rank_cache_refreshed: bool,
) -> Path:
    run_dir = output_dir / run_at.strftime("%Y-%m-%d")
    run_dir.mkdir(parents=True, exist_ok=True)
    ranking = aggregate["ranking"]
    failed_results = aggregate["failed_results"]
    top_by_symbol = {item["stock_symbol"]: item for item in top_items}
    ranking_rows = []
    for row in ranking:
        enriched = dict(row)
        top_row = top_by_symbol.get(row["stock_symbol"])
        if top_row:
            enriched["top_normalized_weight_pct"] = top_row.get("top_normalized_weight_pct")
            enriched["rebalance_weight_pct"] = top_row.get("rebalance_weight_pct")
        ranking_rows.append(enriched)

    metadata = {
        "run_at": run_at.isoformat(),
        "rank_cache_fetched_at": rank_cache_fetched_at.isoformat() if rank_cache_fetched_at else None,
        "rank_cache_refreshed": rank_cache_refreshed,
        "cube_count": len(cubes),
        "success_count": aggregate["success_count"],
        "failed_count": len(failed_results),
        "stock_count": len(ranking),
        "total_stock_weight_pct": aggregate.get("total_stock_weight_pct"),
        "top": top_items,
        "rebalance_payload": rebalance_payload,
        "rebalance_response": rebalance_response,
        "rebalance_skipped": rebalance_skipped_items or [],
        "failed": [
            {
                "symbol": result.cube.symbol,
                "cube_name": result.cube.cube_name,
                "error": result.error,
            }
            for result in failed_results
        ],
    }
    (run_dir / "summary.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (run_dir / "report.md").write_text(report_text, encoding="utf-8")
    write_csv(
        run_dir / "top_stocks.csv",
        ranking_rows,
        [
            "stock_symbol",
            "stock_name",
            "composite_weight_pct",
            "global_normalized_weight_pct",
            "top_normalized_weight_pct",
            "rebalance_weight_pct",
            "holding_cube_count",
            "holding_cube_ratio_pct",
            "total_weight_pct",
            "average_weight_pct",
            "example_cubes",
        ],
    )
    write_csv(
        run_dir / "holdings.csv",
        aggregate["holding_rows"],
        [
            "cube_symbol",
            "cube_name",
            "year_rank",
            "stock_symbol",
            "stock_name",
            "weight_pct",
        ],
    )
    return run_dir


def save_run_record(
    *,
    run_at: datetime,
    target_cube_symbol: str,
    target_cube_id: Optional[int],
    status: str,
    message: str,
    dry_run: bool,
    rank_cache_fetched_at: Optional[datetime],
    rank_cache_refreshed: bool,
    cubes: List[CubeInfo],
    aggregate: Dict[str, Any],
    top_items: List[Dict[str, Any]],
    rebalance_payload: Optional[Dict[str, Any]],
    rebalance_response: Optional[Dict[str, Any]],
) -> int:
    failed_results = aggregate.get("failed_results") or []
    db = SessionLocal()
    try:
        record = XueqiuTopHoldingsRun(
            run_at=run_at.replace(tzinfo=None),
            target_cube_symbol=target_cube_symbol,
            target_cube_id=target_cube_id,
            status=status,
            message=message,
            dry_run=dry_run,
            rank_cache_fetched_at=rank_cache_fetched_at,
            rank_cache_refreshed=rank_cache_refreshed,
            cube_count=len(cubes),
            success_count=aggregate.get("success_count"),
            failed_count=len(failed_results),
            stock_count=len(aggregate.get("ranking") or []),
            top_n=len(top_items),
            cash_pct=(rebalance_payload or {}).get("cash"),
            top_holdings=top_items,
            failed_cubes=[
                {
                    "symbol": result.cube.symbol,
                    "cube_name": result.cube.cube_name,
                    "error": result.error,
                }
                for result in failed_results
            ],
            rebalance_payload=rebalance_payload,
            rebalance_response=rebalance_response,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        return int(record.id)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def run_top_holdings_job(
    *,
    list_path: Optional[str] = None,
    output_dir: str = str(DEFAULT_OUTPUT_DIR),
    receiver_email: Optional[str] = None,
    top_n: int = 10,
    limit: int = RANK_TARGET_COUNT,
    workers: int = DEFAULT_WORKERS,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    no_email: bool = False,
    force: bool = False,
    force_refresh_rank: bool = False,
    execute_rebalance: bool = False,
    dry_run: bool = False,
    target_cube_symbol: str = DEFAULT_TARGET_CUBE_SYMBOL,
    target_cube_id: Optional[int] = None,
) -> Dict[str, Any]:
    run_at = datetime.now(CHINA_TZ)
    if not force and not is_china_trading_day(run_at.date()):
        message = f"跳过雪球Top持仓统计: {run_at.date().isoformat()} 不是A股交易日"
        logger.info(message)
        return {"skipped": True, "message": message}

    cookie = get_latest_cookie()
    rank_cache_fetched_at: Optional[datetime] = None
    rank_cache_refreshed = False
    if list_path:
        cubes = load_cubes_from_file(Path(list_path).expanduser(), limit=limit)
        rank_cache_fetched_at = run_at.replace(tzinfo=None)
        rank_cache_refreshed = False
    else:
        cubes, rank_cache_fetched_at, rank_cache_refreshed = await load_or_refresh_year_top_cubes(
            cookie=cookie,
            force_refresh=force_refresh_rank,
            limit=limit,
            timeout=timeout,
        )
    if not cubes:
        raise RuntimeError("No Xueqiu cubes available.")

    results = await fetch_all_holdings(
        cubes,
        cookie=cookie,
        workers=workers,
        timeout=timeout,
        retries=retries,
    )
    aggregate = aggregate_holdings(results)
    top_items = add_top_normalized_weights(aggregate["ranking"], top_n)
    rebalance_skipped_items: List[Dict[str, Any]] = []
    rebalance_payload = None
    rebalance_response = None
    resolved_target_cube_id = None
    latest_target_rebalance = None

    if execute_rebalance:
        top_items, rebalance_skipped_items = await select_rebalance_top_items(
            cookie=cookie,
            ranking=aggregate["ranking"],
            top_n=top_n,
            timeout=timeout,
        )
        fallback_cube_id = target_cube_id or safe_int(os.getenv("XUEQIU_TOP_HOLDINGS_TARGET_CUBE_ID")) or DEFAULT_TARGET_CUBE_ID
        latest_target_rebalance = await fetch_latest_target_rebalance(
            cookie=cookie,
            target_cube_symbol=target_cube_symbol,
            timeout=timeout,
        )
        resolved_target_cube_id = (
            safe_int((latest_target_rebalance or {}).get("cube_id"))
            or fallback_cube_id
        )
        rebalance_payload = await build_rebalance_payload(
            cookie=cookie,
            target_cube_symbol=target_cube_symbol,
            target_cube_id=resolved_target_cube_id,
            top_items=top_items,
            timeout=timeout,
        )
        top_items = rebalance_payload["top_items"]
        rebalance_response = await create_xueqiu_rebalance(
            cookie=cookie,
            payload=rebalance_payload,
            dry_run=dry_run,
            timeout=timeout,
        )

    report_text = build_report(
        run_at=run_at,
        cubes=cubes,
        aggregate=aggregate,
        top_n=top_n,
        rank_cache_fetched_at=rank_cache_fetched_at,
        rank_cache_refreshed=rank_cache_refreshed,
        rebalance_skipped_items=rebalance_skipped_items,
        target_cube_symbol=target_cube_symbol if execute_rebalance else None,
        rebalance_payload=rebalance_payload,
        rebalance_response=rebalance_response,
        dry_run=dry_run,
    )
    output_path = write_outputs(
        output_dir=Path(output_dir).expanduser(),
        run_at=run_at,
        cubes=cubes,
        aggregate=aggregate,
        report_text=report_text,
        top_items=top_items,
        rebalance_payload=rebalance_payload,
        rebalance_response=rebalance_response,
        rebalance_skipped_items=rebalance_skipped_items,
        rank_cache_fetched_at=rank_cache_fetched_at,
        rank_cache_refreshed=rank_cache_refreshed,
    )
    status = "DRY_RUN" if dry_run and execute_rebalance else "SUCCESS"
    if isinstance(rebalance_response, dict) and rebalance_response.get("skipped"):
        status = "SKIPPED"
    message = (
        f"success={aggregate['success_count']} failed={len(aggregate['failed_results'])} "
        f"stocks={len(aggregate['ranking'])} output={output_path}"
    )
    record_id = save_run_record(
        run_at=run_at,
        target_cube_symbol=target_cube_symbol,
        target_cube_id=resolved_target_cube_id,
        status=status,
        message=message,
        dry_run=dry_run,
        rank_cache_fetched_at=rank_cache_fetched_at,
        rank_cache_refreshed=rank_cache_refreshed,
        cubes=cubes,
        aggregate=aggregate,
        top_items=top_items,
        rebalance_payload=rebalance_payload,
        rebalance_response=rebalance_response,
    )
    if not no_email:
        receiver = receiver_email or os.getenv("XUEQIU_TOP_HOLDINGS_EMAIL") or DEFAULT_RECEIVER_EMAIL
        subject = (
            f"雪球年榜1000组合Top{top_n}自动调仓 - {run_at.strftime('%Y-%m-%d')}"
            if execute_rebalance
            else f"雪球年榜1000组合综合持仓权重 Top{top_n} - {run_at.strftime('%Y-%m-%d')}"
        )
        sendmail(receiver, subject, report_text)
    return {
        "skipped": False,
        "record_id": record_id,
        "output_dir": str(output_path),
        "rank_cache_fetched_at": rank_cache_fetched_at.isoformat() if rank_cache_fetched_at else None,
        "rank_cache_refreshed": rank_cache_refreshed,
        "success_count": aggregate["success_count"],
        "failed_count": len(aggregate["failed_results"]),
        "stock_count": len(aggregate["ranking"]),
        "target_cube_symbol": target_cube_symbol if execute_rebalance else None,
        "target_cube_id": resolved_target_cube_id,
        "dry_run": dry_run,
        "rebalance_response": rebalance_response,
        "rebalance_skipped": rebalance_skipped_items,
        "top": top_items,
    }


def process_xueqiu_top_holdings_rebalance_for_robot() -> str:
    result = asyncio.run(
        run_top_holdings_job(
            top_n=10,
            execute_rebalance=True,
            dry_run=False,
            no_email=False,
            force=False,
            workers=DEFAULT_WORKERS,
            timeout=DEFAULT_TIMEOUT,
            retries=DEFAULT_RETRIES,
            target_cube_symbol=os.getenv("XUEQIU_TOP_HOLDINGS_TARGET_CUBE_SYMBOL", DEFAULT_TARGET_CUBE_SYMBOL),
        )
    )
    if result.get("skipped"):
        return str(result.get("message"))
    response = result.get("rebalance_response") or {}
    return (
        "雪球Top1000综合持仓自动调仓 "
        f"record_id={result.get('record_id')} "
        f"target={result.get('target_cube_symbol')} "
        f"cube_id={result.get('target_cube_id')} "
        f"success={result.get('success_count')} "
        f"failed={result.get('failed_count')} "
        f"stocks={result.get('stock_count')} "
        f"rebalance_skipped={response.get('skipped') if isinstance(response, dict) else None} "
        f"rebalance_id={response.get('id') if isinstance(response, dict) else None} "
        f"rebalance_status={response.get('status') if isinstance(response, dict) else None}"
    )


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Xueqiu top1000 cube holding popularity and rebalance.")
    parser.add_argument("--list-path", default=None, help="Optional local top1000_list.json; bypasses DB ranking cache.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for daily file outputs.")
    parser.add_argument("--receiver-email", default=None, help="Receiver email. Defaults to XUEQIU_TOP_HOLDINGS_EMAIL or alert email.")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--limit", type=int, default=RANK_TARGET_COUNT)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    parser.add_argument("--no-email", action="store_true")
    parser.add_argument("--force", action="store_true", help="Run even if today is not an A-share trading day.")
    parser.add_argument("--force-refresh-rank", action="store_true", help="Refresh Xueqiu year rank even if DB cache is fresh.")
    parser.add_argument("--execute-rebalance", action="store_true", help="Submit rebalance request to the target cube.")
    parser.add_argument("--dry-run", action="store_true", help="Build rebalance payload without sending it.")
    parser.add_argument("--target-cube-symbol", default=os.getenv("XUEQIU_TOP_HOLDINGS_TARGET_CUBE_SYMBOL", DEFAULT_TARGET_CUBE_SYMBOL))
    parser.add_argument("--target-cube-id", type=int, default=safe_int(os.getenv("XUEQIU_TOP_HOLDINGS_TARGET_CUBE_ID")))
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = parse_args(argv)
    try:
        result = asyncio.run(
            run_top_holdings_job(
                list_path=args.list_path,
                output_dir=args.output_dir,
                receiver_email=args.receiver_email,
                top_n=args.top,
                limit=args.limit,
                workers=args.workers,
                timeout=args.timeout,
                retries=args.retries,
                no_email=args.no_email,
                force=args.force,
                force_refresh_rank=args.force_refresh_rank,
                execute_rebalance=args.execute_rebalance,
                dry_run=args.dry_run,
                target_cube_symbol=args.target_cube_symbol,
                target_cube_id=args.target_cube_id,
            )
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("Xueqiu top holdings job failed")
        try:
            send_alert_email(
                "雪球年榜1000组合综合持仓任务失败",
                f"Error: {exc}\n\nTraceback:\n{traceback.format_exc()}",
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to send failure alert")
        return 1


if __name__ == "__main__":
    sys.exit(main())

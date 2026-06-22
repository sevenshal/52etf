from __future__ import annotations

import argparse
import asyncio
import csv
import html
import json
import logging
import math
import os
import re
import sys
import traceback
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

import httpx
import pandas as pd

from ..core.database import (
    SessionLocal,
    SnowballAccountConfig,
    XueqiuCubeRankCache,
    XueqiuTopHoldingsRun,
)
from ..core.duckdb_utils import ANALYTICS_DB_PATH, connect_duckdb
from ..core.utils import send_alert_email, send_configured_email


ROOT = Path(__file__).resolve().parents[2]
CHINA_TZ = ZoneInfo("Asia/Shanghai")
XUEQIU_API_BASE_URL = "https://api.xueqiu.com"
XUEQIU_STOCK_BASE_URL = "https://stock.xueqiu.com"
XUEQIU_WEB_BASE_URL = "https://xueqiu.com"
RANK_CACHE_TYPE = "year"
RANK_CACHE_TTL_DAYS = 7
RANK_PAGE_SIZE = 20
RANK_TARGET_COUNT = 1000
DEFAULT_TARGET_CUBE_SYMBOL = "ZH3630096"
DEFAULT_TARGET_CUBE_ID = 3664154
DEFAULT_OUTPUT_DIR = ROOT / "lab" / "output" / "xueqiu_top_holdings"
DEFAULT_WORKERS = 8
DEFAULT_TIMEOUT = 15.0
DEFAULT_RETRIES = 3
CASH_SYMBOL = "CASH"
CASH_NAME = "现金"
# Xueqiu quote type examples: 11=A-share, 13=ETF/fund, 82=STAR Market, 17=exchange repo.
XUEQIU_REBALANCE_BLOCKED_QUOTE_TYPES_ENV = "XUEQIU_REBALANCE_BLOCKED_QUOTE_TYPES"
DEFAULT_XUEQIU_REBALANCE_BLOCKED_QUOTE_TYPES = {17}
REBALANCE_QUOTE_BATCH_SIZE = 50
BUFFER_STRATEGY_TOP_N = 10
BUFFER_STRATEGY_SELL_RANK = 12
BUFFER_STRATEGY_NAME = "Top10等权 + 跌出Top12才卖 + 从Top10补位 + 成分变化才调仓"
BUFFER_RETAIN_WEIGHT_TOLERANCE_PCT = 1.0
BUFFER_EXECUTION_WEIGHT_RULE = (
    f"最小换手：保留成分偏离等权不超过{BUFFER_RETAIN_WEIGHT_TOLERANCE_PCT:g}个百分点时沿用当前权重，"
    "卖出释放权重分配给补位成分"
)
ACTIVE_REBALANCE_LOOKBACK_DAYS = 90
ACTIVE_REBALANCE_MAX_FAILED_RATIO = 0.10
XUEQIU_CUBE_HOLDINGS_SNAPSHOT_TABLE = "xueqiu_cube_holdings_snapshots"
XUEQIU_CUBE_HOLDINGS_SNAPSHOT_COLUMNS = [
    "snapshot_date",
    "snapshot_at",
    "rank_type",
    "year_rank",
    "cube_symbol",
    "cube_id",
    "cube_name",
    "screen_name",
    "latest_rebalance_at",
    "latest_rebalance_id",
    "latest_rebalance_status",
    "holdings_source",
    "active_rebalance_days",
    "is_active",
    "stock_symbol",
    "raw_stock_symbol",
    "stock_name",
    "stock_id",
    "segment_name",
    "weight_pct",
    "raw_holding_json",
    "created_at",
    "updated_at",
]


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


@dataclass
class CubeCurrentResult:
    cube: CubeInfo
    holdings: List[Dict[str, Any]]
    latest_rebalance_at: Optional[datetime] = None
    latest_rebalance_id: Optional[int] = None
    latest_rebalance_status: str = ""
    holdings_source: str = ""
    active: bool = False
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


def get_rebalance_blocked_quote_types() -> Set[int]:
    raw_value = os.getenv(XUEQIU_REBALANCE_BLOCKED_QUOTE_TYPES_ENV)
    if raw_value is None:
        return set(DEFAULT_XUEQIU_REBALANCE_BLOCKED_QUOTE_TYPES)

    blocked_types: Set[int] = set()
    for item in re.split(r"[\s,;]+", raw_value.strip()):
        quote_type = safe_int(item)
        if quote_type is not None:
            blocked_types.add(quote_type)
    return blocked_types


def xueqiu_timestamp_to_datetime(value: Any) -> Optional[datetime]:
    number = safe_float(value)
    if number is None:
        return None
    seconds = number / 1000.0 if number > 10_000_000_000 else number
    try:
        return datetime.fromtimestamp(seconds, tz=CHINA_TZ)
    except (OSError, OverflowError, ValueError):
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


def is_cash_symbol(symbol: Any) -> bool:
    text = str(symbol or "").strip().upper()
    return text in {CASH_SYMBOL, "CN_CASH"}


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


def calculate_cash_weight_from_holdings(non_cash_weight_pct: float) -> float:
    cash_weight_pct = 100.0 - non_cash_weight_pct
    if abs(cash_weight_pct) < 0.005:
        return 0.0
    return max(0.0, min(100.0, cash_weight_pct))


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


def normalize_ranked_cubes(cubes: Iterable[CubeInfo]) -> List[CubeInfo]:
    best_by_symbol: Dict[str, Tuple[int, int, CubeInfo]] = {}
    duplicate_symbols: Dict[str, int] = defaultdict(int)
    invalid_rank_count = 0
    dropped_symbol_count = 0

    for index, cube in enumerate(cubes, start=1):
        symbol = (cube.symbol or "").strip()
        if not symbol:
            dropped_symbol_count += 1
            continue
        rank = safe_int(cube.year_rank)
        if rank is None or rank <= 0:
            rank = index
            invalid_rank_count += 1
        normalized_cube = replace(cube, symbol=symbol, year_rank=rank)
        existing = best_by_symbol.get(symbol)
        if existing is not None:
            duplicate_symbols[symbol] += 1
            if rank >= existing[0]:
                continue
        best_by_symbol[symbol] = (rank, index, normalized_cube)

    ordered_cubes = [
        cube
        for _, _, cube in sorted(
            best_by_symbol.values(),
            key=lambda item: (item[0], item[1]),
        )
    ]
    renumbered_count = 0
    result: List[CubeInfo] = []
    for normalized_rank, cube in enumerate(ordered_cubes, start=1):
        if cube.year_rank != normalized_rank:
            renumbered_count += 1
        result.append(replace(cube, year_rank=normalized_rank))

    if duplicate_symbols:
        sample_symbols = ", ".join(sorted(duplicate_symbols)[:5])
        logger.warning(
            "Collapsed duplicate Xueqiu year-rank cubes before persistence: duplicates=%s symbols=%s sample=%s",
            sum(duplicate_symbols.values()),
            len(duplicate_symbols),
            sample_symbols or "-",
        )
    if dropped_symbol_count or invalid_rank_count or renumbered_count:
        logger.warning(
            "Normalized Xueqiu year-rank cubes before persistence: dropped_blank_symbols=%s invalid_ranks=%s renumbered=%s final_count=%s",
            dropped_symbol_count,
            invalid_rank_count,
            renumbered_count,
            len(result),
        )
    return result


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
    return normalize_ranked_cubes(cubes)


async def fetch_year_top_cubes(
    *,
    cookie: str,
    target_count: int = RANK_TARGET_COUNT,
    page_size: int = RANK_PAGE_SIZE,
    timeout: float = DEFAULT_TIMEOUT,
) -> List[CubeInfo]:
    headers = build_headers(cookie)
    cubes: List[CubeInfo] = []
    duplicate_symbols: Dict[str, int] = defaultdict(int)
    seen_symbols: Set[str] = set()
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
                symbol = (cube.symbol or "").strip()
                if not symbol:
                    continue
                if symbol in seen_symbols:
                    duplicate_symbols[symbol] += 1
                    continue
                cube.symbol = symbol
                cube.year_rank = len(cubes) + 1
                cubes.append(cube)
                seen_symbols.add(symbol)
                if len(cubes) >= target_count:
                    break
            if len(batch) < page_size:
                break
            page += 1
            await asyncio.sleep(0.08)
    if duplicate_symbols:
        sample_symbols = ", ".join(sorted(duplicate_symbols)[:5])
        logger.warning(
            "Skipped duplicate Xueqiu year-rank symbols while fetching: duplicates=%s symbols=%s sample=%s",
            sum(duplicate_symbols.values()),
            len(duplicate_symbols),
            sample_symbols or "-",
        )
    if len(cubes) < target_count:
        if duplicate_symbols and cubes:
            logger.warning(
                "Using shortened Xueqiu year-rank list after de-duplication: unique_count=%s expected=%s",
                len(cubes),
                target_count,
            )
            return cubes
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
        if not rows:
            return [], latest.fetched_at
        if len(rows) < limit:
            logger.warning(
                "Using shortened Xueqiu year-rank cache: cached_count=%s requested_limit=%s fetched_at=%s",
                len(rows),
                limit,
                latest.fetched_at.strftime("%Y-%m-%d %H:%M:%S"),
            )
        return [cube_from_cache_row(row) for row in rows], latest.fetched_at
    finally:
        db.close()


def save_year_top_cubes(cubes: List[CubeInfo], fetched_at: datetime) -> None:
    normalized_cubes = normalize_ranked_cubes(cubes)
    db = SessionLocal()
    try:
        db.query(XueqiuCubeRankCache).filter(
            XueqiuCubeRankCache.rank_type == RANK_CACHE_TYPE
        ).delete(synchronize_session=False)
        for cube in normalized_cubes:
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


def extract_current_rebalance_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    if "last_rb" in payload or "last_success_rb" in payload:
        return payload
    data = payload.get("data")
    if isinstance(data, dict) and ("last_rb" in data or "last_success_rb" in data):
        return data
    return payload


def select_current_holdings(payload: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], str]:
    last_success_rb = payload.get("last_success_rb")
    last_rb = payload.get("last_rb")
    if isinstance(last_success_rb, dict):
        holdings = last_success_rb.get("holdings")
        if isinstance(holdings, list):
            return holdings, "last_success_rb"
    if isinstance(last_rb, dict):
        holdings = last_rb.get("holdings")
        if isinstance(holdings, list):
            return holdings, "last_rb"
    return [], ""


async def fetch_cube_current(
    client: httpx.AsyncClient,
    cube: CubeInfo,
    *,
    active_since: datetime,
    semaphore: asyncio.Semaphore,
    retries: int,
) -> CubeCurrentResult:
    url = f"{XUEQIU_API_BASE_URL}/cubes/rebalancing/current.json"
    last_error: Optional[BaseException] = None
    async with semaphore:
        for attempt in range(1, retries + 1):
            try:
                response = await client.get(url, params={"cube_symbol": cube.symbol})
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
                if not isinstance(payload, dict):
                    raise ValueError(f"Unexpected current payload for {cube.symbol}: {payload}")
                payload = extract_current_rebalance_payload(payload)
                last_rb = payload.get("last_rb")
                if not isinstance(last_rb, dict):
                    last_rb = {}
                latest_at = xueqiu_timestamp_to_datetime(last_rb.get("created_at"))
                latest_id = safe_int(last_rb.get("id"))
                latest_status = str(last_rb.get("status") or "")
                holdings, holdings_source = select_current_holdings(payload)
                return CubeCurrentResult(
                    cube=cube,
                    holdings=holdings,
                    latest_rebalance_at=latest_at,
                    latest_rebalance_id=latest_id,
                    latest_rebalance_status=latest_status,
                    holdings_source=holdings_source,
                    active=bool(latest_at and latest_at >= active_since),
                )
            except BaseException as exc:  # noqa: BLE001
                last_error = exc
                if attempt < retries:
                    retry_delay = min(10.0, 0.8 * attempt)
                    if getattr(exc, "xueqiu_error_code", "") in {"10026", "400016"}:
                        retry_delay = min(45.0, 5.0 * attempt)
                    await asyncio.sleep(retry_delay)
        return CubeCurrentResult(cube=cube, holdings=[], error=repr(last_error))


async def fetch_all_cube_current(
    cubes: List[CubeInfo],
    *,
    cookie: str,
    workers: int,
    timeout: float,
    retries: int,
    active_since: datetime,
) -> List[CubeCurrentResult]:
    headers = build_headers(cookie, referer=XUEQIU_WEB_BASE_URL)
    timeout_config = httpx.Timeout(timeout)
    current_workers = max(1, workers)
    limits = httpx.Limits(max_connections=current_workers, max_keepalive_connections=current_workers)
    semaphore = asyncio.Semaphore(current_workers)
    async with httpx.AsyncClient(headers=headers, timeout=timeout_config, limits=limits) as client:
        tasks = [
            fetch_cube_current(
                client,
                cube,
                active_since=active_since,
                semaphore=semaphore,
                retries=retries,
            )
            for cube in cubes
        ]
        results: List[CubeCurrentResult] = []
        for index, task in enumerate(asyncio.as_completed(tasks), start=1):
            result = await task
            results.append(result)
            if index % 100 == 0:
                logger.info("Fetched current snapshots for %s/%s cubes", index, len(cubes))
        return results


def build_active_filter_summary(
    *,
    source_cubes: List[CubeInfo],
    current_results: List[CubeCurrentResult],
    active_since: datetime,
    lookback_days: int,
) -> Dict[str, Any]:
    active_results = [result for result in current_results if result.active and not result.error]
    failed_results = [result for result in current_results if result.error]
    inactive_results = [result for result in current_results if not result.active and not result.error]
    fallback_results = [result for result in current_results if result.holdings_source == "last_rb"]
    latest_times = [
        result.latest_rebalance_at
        for result in current_results
        if result.latest_rebalance_at is not None
    ]
    active_latest_times = [
        result.latest_rebalance_at
        for result in active_results
        if result.latest_rebalance_at is not None
    ]
    return {
        "enabled": True,
        "lookback_days": lookback_days,
        "active_since": active_since.isoformat(),
        "source_cube_count": len(source_cubes),
        "active_cube_count": len(active_results),
        "inactive_cube_count": len(inactive_results),
        "activity_failed_count": len(failed_results),
        "current_snapshot_failed_count": len(failed_results),
        "holdings_fallback_count": len(fallback_results),
        "latest_rebalance_at_max": max(latest_times).isoformat() if latest_times else None,
        "latest_rebalance_at_min_active": min(active_latest_times).isoformat() if active_latest_times else None,
        "failed_examples": [
            {
                "symbol": result.cube.symbol,
                "cube_name": result.cube.cube_name,
                "error": result.error,
            }
            for result in failed_results[:10]
        ],
    }


def _quote_duckdb_identifier(identifier: str) -> str:
    return '"' + str(identifier).replace('"', '""') + '"'


def _to_naive_china_datetime(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(CHINA_TZ).replace(tzinfo=None)
    return value.replace(tzinfo=None)


def _holding_stock_id(holding: Dict[str, Any]) -> Optional[int]:
    for key in ("stock_id", "stockId", "stockID"):
        value = safe_int(holding.get(key))
        if value is not None:
            return value
    return None


def _holding_segment_name(holding: Dict[str, Any]) -> str:
    for key in ("segment_name", "segmentName", "ind_name", "industry"):
        value = holding.get(key)
        if value:
            return str(value)
    return ""


def ensure_xueqiu_cube_holdings_snapshot_schema(connection) -> None:
    table = _quote_duckdb_identifier(XUEQIU_CUBE_HOLDINGS_SNAPSHOT_TABLE)
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {table} (
            snapshot_date DATE NOT NULL,
            snapshot_at TIMESTAMP NOT NULL,
            rank_type VARCHAR NOT NULL,
            year_rank INTEGER,
            cube_symbol VARCHAR NOT NULL,
            cube_id BIGINT,
            cube_name VARCHAR,
            screen_name VARCHAR,
            latest_rebalance_at TIMESTAMP,
            latest_rebalance_id BIGINT,
            latest_rebalance_status VARCHAR,
            holdings_source VARCHAR,
            active_rebalance_days INTEGER,
            is_active BOOLEAN,
            stock_symbol VARCHAR NOT NULL,
            raw_stock_symbol VARCHAR,
            stock_name VARCHAR,
            stock_id BIGINT,
            segment_name VARCHAR,
            weight_pct DOUBLE,
            raw_holding_json VARCHAR,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            PRIMARY KEY (snapshot_date, cube_symbol, stock_symbol)
        )
        """
    )
    connection.execute(
        f"CREATE INDEX IF NOT EXISTS idx_xueqiu_cube_holdings_snapshot_stock "
        f"ON {table}(snapshot_date, stock_symbol)"
    )
    connection.execute(
        f"CREATE INDEX IF NOT EXISTS idx_xueqiu_cube_holdings_snapshot_cube "
        f"ON {table}(cube_symbol, snapshot_date)"
    )
    connection.execute(
        f"CREATE INDEX IF NOT EXISTS idx_xueqiu_cube_holdings_snapshot_active_stock "
        f"ON {table}(is_active, snapshot_date, stock_symbol)"
    )


def build_xueqiu_cube_holdings_snapshot_rows(
    *,
    run_at: datetime,
    current_results: List[CubeCurrentResult],
    active_rebalance_days: Optional[int],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    snapshot_at = _to_naive_china_datetime(run_at) or datetime.now()
    snapshot_date = snapshot_at.date()
    saved_at = datetime.now(CHINA_TZ).replace(tzinfo=None)
    rows: List[Dict[str, Any]] = []
    replace_cube_symbols: Set[str] = set()
    for result in current_results:
        if result.error:
            continue
        cube = result.cube
        replace_cube_symbols.add(cube.symbol)
        latest_rebalance_at = _to_naive_china_datetime(result.latest_rebalance_at)
        for holding in result.holdings:
            symbol = normalize_xueqiu_symbol(
                holding.get("symbol")
                or holding.get("stock_symbol")
                or holding.get("stockSymbol")
            )
            weight = get_holding_weight(holding)
            if not symbol or weight is None or weight <= 0:
                continue
            raw_symbol = to_raw_xueqiu_symbol(symbol)
            rows.append(
                {
                    "snapshot_date": snapshot_date,
                    "snapshot_at": snapshot_at,
                    "rank_type": RANK_CACHE_TYPE,
                    "year_rank": cube.year_rank,
                    "cube_symbol": cube.symbol,
                    "cube_id": cube.cube_id,
                    "cube_name": cube.cube_name,
                    "screen_name": cube.screen_name,
                    "latest_rebalance_at": latest_rebalance_at,
                    "latest_rebalance_id": result.latest_rebalance_id,
                    "latest_rebalance_status": result.latest_rebalance_status,
                    "holdings_source": result.holdings_source,
                    "active_rebalance_days": active_rebalance_days,
                    "is_active": bool(result.active),
                    "stock_symbol": symbol,
                    "raw_stock_symbol": raw_symbol,
                    "stock_name": get_holding_name(holding),
                    "stock_id": _holding_stock_id(holding),
                    "segment_name": _holding_segment_name(holding),
                    "weight_pct": float(weight),
                    "raw_holding_json": json.dumps(
                        holding,
                        ensure_ascii=False,
                        default=str,
                        separators=(",", ":"),
                    ),
                    "created_at": saved_at,
                    "updated_at": saved_at,
                }
            )
    return rows, sorted(replace_cube_symbols)


def save_xueqiu_cube_holdings_snapshots_to_duckdb(
    *,
    run_at: datetime,
    current_results: List[CubeCurrentResult],
    active_rebalance_days: Optional[int],
) -> Dict[str, Any]:
    rows, replace_cube_symbols = build_xueqiu_cube_holdings_snapshot_rows(
        run_at=run_at,
        current_results=current_results,
        active_rebalance_days=active_rebalance_days,
    )
    snapshot_at = _to_naive_china_datetime(run_at) or datetime.now()
    snapshot_date = snapshot_at.date()
    connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=False)
    table = _quote_duckdb_identifier(XUEQIU_CUBE_HOLDINGS_SNAPSHOT_TABLE)
    temp_rows_name = "xueqiu_cube_holdings_snapshot_rows"
    temp_cubes_name = "xueqiu_cube_holdings_snapshot_cubes"
    try:
        ensure_xueqiu_cube_holdings_snapshot_schema(connection)
        if replace_cube_symbols:
            cube_frame = pd.DataFrame({"cube_symbol": replace_cube_symbols})
            connection.register(temp_cubes_name, cube_frame)
            connection.execute(
                (
                    f"DELETE FROM {table} "
                    f"USING {_quote_duckdb_identifier(temp_cubes_name)} AS source "
                    f"WHERE {table}.snapshot_date = ? "
                    f"AND {table}.cube_symbol = source.cube_symbol"
                ),
                [snapshot_date],
            )
        if rows:
            frame = pd.DataFrame(rows).loc[:, XUEQIU_CUBE_HOLDINGS_SNAPSHOT_COLUMNS]
            connection.register(temp_rows_name, frame)
            quoted_columns = ", ".join(
                _quote_duckdb_identifier(column)
                for column in XUEQIU_CUBE_HOLDINGS_SNAPSHOT_COLUMNS
            )
            connection.execute(
                (
                    f"INSERT OR REPLACE INTO {table} ({quoted_columns}) "
                    f"SELECT {quoted_columns} FROM {_quote_duckdb_identifier(temp_rows_name)}"
                )
            )
        return {
            "table": XUEQIU_CUBE_HOLDINGS_SNAPSHOT_TABLE,
            "snapshot_date": snapshot_date.isoformat(),
            "snapshot_at": snapshot_at.isoformat(),
            "saved_rows": len(rows),
            "replaced_cube_count": len(replace_cube_symbols),
            "failed_cube_count": len([result for result in current_results if result.error]),
            "database": ANALYTICS_DB_PATH,
        }
    finally:
        connection.close()


async def fetch_target_cube_current_payload(
    *,
    cookie: str,
    target_cube_symbol: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    headers = build_headers(
        cookie,
        referer=f"{XUEQIU_WEB_BASE_URL}/P/{target_cube_symbol}",
    )
    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(timeout)) as client:
        response = await client.get(
            f"{XUEQIU_API_BASE_URL}/cubes/rebalancing/current.json",
            params={"cube_symbol": target_cube_symbol},
        )
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected target cube current payload for {target_cube_symbol}: {payload}")
    return extract_current_rebalance_payload(payload)


async def fetch_target_cube_holdings(
    *,
    cookie: str,
    target_cube_symbol: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> List[Dict[str, Any]]:
    payload = await fetch_target_cube_current_payload(
        cookie=cookie,
        target_cube_symbol=target_cube_symbol,
        timeout=timeout,
    )
    holdings, source = select_current_holdings(payload)
    if source:
        return holdings
    raise RuntimeError(f"Unexpected target cube holdings payload for {target_cube_symbol}: {payload}")


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
        cube_non_cash_weight_pct = 0.0
        for holding in result.holdings:
            symbol = normalize_xueqiu_symbol(
                holding.get("symbol")
                or holding.get("stock_symbol")
                or holding.get("stockSymbol")
            )
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
                    "is_cash": False,
                    "weight_pct": weight,
                }
            )
            if symbol in seen_symbols:
                continue
            seen_symbols.add(symbol)
            cube_non_cash_weight_pct += weight
            stock_to_cubes[symbol].add(result.cube.symbol)
            stock_total_weight[symbol] += weight
            if name and symbol not in stock_names:
                stock_names[symbol] = name
            if len(stock_cube_examples[symbol]) < 5:
                stock_cube_examples[symbol].append(result.cube.cube_name or result.cube.symbol)

        cash_weight = calculate_cash_weight_from_holdings(cube_non_cash_weight_pct)
        if cash_weight > 0:
            holding_rows.append(
                {
                    "cube_symbol": result.cube.symbol,
                    "cube_name": result.cube.cube_name,
                    "year_rank": result.cube.year_rank,
                    "stock_symbol": CASH_SYMBOL,
                    "stock_name": CASH_NAME,
                    "weight_pct": cash_weight,
                    "is_cash": True,
                }
            )
            stock_to_cubes[CASH_SYMBOL].add(result.cube.symbol)
            stock_total_weight[CASH_SYMBOL] += cash_weight
            if len(stock_cube_examples[CASH_SYMBOL]) < 5:
                stock_cube_examples[CASH_SYMBOL].append(result.cube.cube_name or result.cube.symbol)

    if success_count:
        stock_to_cubes.setdefault(CASH_SYMBOL, set())
        stock_total_weight.setdefault(CASH_SYMBOL, 0.0)
        stock_names[CASH_SYMBOL] = CASH_NAME

    ranking = []
    for symbol, cube_symbols in stock_to_cubes.items():
        cube_count = len(cube_symbols)
        total_weight = stock_total_weight[symbol]
        is_cash = is_cash_symbol(symbol)
        average_weight_pct = total_weight / cube_count if cube_count else (0.0 if is_cash else None)
        ranking.append(
            {
                "stock_symbol": symbol,
                "stock_name": CASH_NAME if is_cash else stock_names.get(symbol, ""),
                "is_cash": is_cash,
                "holding_cube_count": cube_count,
                "holding_cube_ratio_pct": cube_count / success_count * 100.0 if success_count else None,
                "total_weight_pct": total_weight,
                "composite_weight_pct": total_weight / success_count if success_count else None,
                "average_weight_pct": average_weight_pct,
                "example_cubes": stock_cube_examples.get(symbol, []),
            }
        )

    total_stock_weight_pct = sum(
        item["total_weight_pct"]
        for item in ranking
        if not item.get("is_cash")
    )
    total_cash_weight_pct = stock_total_weight.get(CASH_SYMBOL, 0.0) if success_count else 0.0
    total_portfolio_weight_pct = total_stock_weight_pct + total_cash_weight_pct
    for item in ranking:
        item["global_normalized_weight_pct"] = (
            item["total_weight_pct"] / total_portfolio_weight_pct * 100.0
            if total_portfolio_weight_pct > 0
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
    for index, item in enumerate(ranking, start=1):
        item["composite_rank"] = index
    cash_item = next((item for item in ranking if item.get("is_cash")), None)
    return {
        "success_count": success_count,
        "failed_results": failed_results,
        "holding_rows": holding_rows,
        "ranking": ranking,
        "total_stock_weight_pct": total_stock_weight_pct,
        "total_cash_weight_pct": total_cash_weight_pct,
        "total_portfolio_weight_pct": total_portfolio_weight_pct,
        "cash_item": dict(cash_item) if cash_item else None,
    }


def fmt_number(value: Any, digits: int = 2, suffix: str = "") -> str:
    number = safe_float(value)
    if number is None:
        return "-"
    return f"{number:.{digits}f}{suffix}"


def get_cash_ranking_item(aggregate: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    cash_item = aggregate.get("cash_item")
    if isinstance(cash_item, dict):
        return cash_item
    for item in aggregate.get("ranking") or []:
        if isinstance(item, dict) and (item.get("is_cash") or is_cash_symbol(item.get("stock_symbol"))):
            return item
    return None


def count_non_cash_ranking_items(ranking: List[Dict[str, Any]]) -> int:
    return len([
        item
        for item in ranking
        if not (item.get("is_cash") or is_cash_symbol(item.get("stock_symbol")))
    ])


def build_report_table_items(
    *,
    top_items: List[Dict[str, Any]],
    aggregate: Dict[str, Any],
    top_n: int,
) -> List[Dict[str, Any]]:
    display_items = [dict(item) for item in top_items[:top_n]]
    if any(item.get("is_cash") or is_cash_symbol(item.get("stock_symbol")) for item in display_items):
        return display_items

    cash_item = get_cash_ranking_item(aggregate)
    if cash_item:
        display_items.append(dict(cash_item))
    return display_items


def fmt_datetime_value(value: Any) -> str:
    if isinstance(value, datetime):
        dt_value = value
    elif isinstance(value, str) and value:
        try:
            dt_value = datetime.fromisoformat(value)
        except ValueError:
            return value
    else:
        return "-"
    return dt_value.astimezone(CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")


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


def _rebalance_source_weight(item: Dict[str, Any]) -> float:
    rebalance_weight = safe_float(item.get("rebalance_weight_pct"))
    if rebalance_weight is not None:
        return rebalance_weight
    return safe_float(item.get("top_normalized_weight_pct")) or 0.0


def rounded_rebalance_weights(top_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    selected = [dict(item) for item in top_items]
    if not selected:
        return selected
    rounded_weights = [
        round(_rebalance_source_weight(item), 2)
        for item in selected
    ]
    delta = round(100.0 - sum(rounded_weights), 2)
    adjust_index = next(
        (
            index
            for index, item in enumerate(selected)
            if item.get("strategy_action") in {"buy", "trim", "adjust"}
        ),
        0,
    )
    rounded_weights[adjust_index] = round(rounded_weights[adjust_index] + delta, 2)
    for item, weight in zip(selected, rounded_weights):
        item["rebalance_weight_pct"] = weight
    return selected


def extract_current_target_holdings(holdings: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    current: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for holding in holdings:
        symbol = normalize_xueqiu_symbol(holding.get("symbol") or holding.get("stock_symbol"))
        weight = get_holding_weight(holding)
        if not symbol or weight is None or weight <= 0 or symbol in seen:
            continue
        seen.add(symbol)
        current.append(
            {
                "stock_symbol": symbol,
                "stock_name": get_holding_name(holding),
                "weight_pct": weight,
                "raw": holding,
            }
        )
    return current


def _ranked_rebalance_candidates(ranking: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for index, item in enumerate(ranking, start=1):
        symbol = normalize_xueqiu_symbol(item.get("stock_symbol"))
        if not symbol or not to_raw_xueqiu_symbol(symbol):
            continue
        candidate = dict(item)
        candidate["stock_symbol"] = symbol
        candidate["strategy_rank"] = index
        candidates.append(candidate)
    return candidates


def _symbol_label(symbol: str, item_by_symbol: Dict[str, Dict[str, Any]], current_by_symbol: Dict[str, Dict[str, Any]]) -> str:
    item = item_by_symbol.get(symbol) or {}
    current = current_by_symbol.get(symbol) or {}
    return f"{symbol}({item.get('stock_name') or current.get('stock_name') or ''})"


def build_min_turnover_execution_weights(
    *,
    final_symbols: List[str],
    added_symbols: List[str],
    current_by_symbol: Dict[str, Dict[str, Any]],
) -> Dict[str, float]:
    """Build submission weights that avoid touching retained holdings unnecessarily."""
    if not final_symbols:
        return {}

    added_set = set(added_symbols)
    retained_symbols = [symbol for symbol in final_symbols if symbol not in added_set]
    equal_weight = 100.0 / len(final_symbols)

    weights: Dict[str, float] = {}
    retained_sum = 0.0
    for symbol in retained_symbols:
        current_weight = safe_float((current_by_symbol.get(symbol) or {}).get("weight_pct"))
        if current_weight is None or current_weight <= 0:
            weight = equal_weight
        elif abs(current_weight - equal_weight) > BUFFER_RETAIN_WEIGHT_TOLERANCE_PCT:
            weight = equal_weight
        else:
            weight = current_weight
        weights[symbol] = weight
        retained_sum += weight

    remaining_weight = 100.0 - retained_sum
    if added_symbols:
        if remaining_weight <= 0:
            retained_target_sum = max(0.0, 100.0 - equal_weight * len(added_symbols))
            scale = retained_target_sum / retained_sum if retained_sum > 0 else 0.0
            for symbol in retained_symbols:
                weights[symbol] = weights[symbol] * scale
            remaining_weight = 100.0 - retained_target_sum
        per_added_weight = remaining_weight / len(added_symbols)
        for symbol in added_symbols:
            weights[symbol] = max(0.0, per_added_weight)
        return weights

    if abs(remaining_weight) <= 1e-9:
        return weights

    underweight_symbols = [
        symbol
        for symbol in retained_symbols
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
    if total > 0 and abs(total - 100.0) > 1e-9:
        scale = 100.0 / total
        weights = {symbol: weight * scale for symbol, weight in weights.items()}
    return weights


def build_equal_top10_top12_buffer_plan(
    *,
    ranking: List[Dict[str, Any]],
    current_holdings: List[Dict[str, Any]],
    top_n: int = BUFFER_STRATEGY_TOP_N,
    sell_rank: int = BUFFER_STRATEGY_SELL_RANK,
) -> Dict[str, Any]:
    candidates = _ranked_rebalance_candidates(ranking)
    if len(candidates) < max(top_n, sell_rank):
        raise RuntimeError(
            f"Not enough ranked stocks for buffered rebalance plan: "
            f"candidates={len(candidates)} top_n={top_n} sell_rank={sell_rank}"
        )

    top10_items = candidates[:top_n]
    top12_items = candidates[:sell_rank]
    top10_symbols = [item["stock_symbol"] for item in top10_items]
    top12_symbols = {item["stock_symbol"] for item in top12_items}
    item_by_symbol = {item["stock_symbol"]: item for item in candidates}

    current_items = extract_current_target_holdings(current_holdings)
    current_by_symbol = {item["stock_symbol"]: item for item in current_items}
    current_symbols = [item["stock_symbol"] for item in current_items]

    retained_symbols = [symbol for symbol in current_symbols if symbol in top12_symbols]
    removed_symbols = [symbol for symbol in current_symbols if symbol not in top12_symbols]
    trim_removed_symbols: List[str] = []
    if len(retained_symbols) > top_n:
        ranked_retained = sorted(
            retained_symbols,
            key=lambda symbol: (
                safe_int((item_by_symbol.get(symbol) or {}).get("strategy_rank")) or 999999,
                symbol,
            ),
        )
        retained_symbols = ranked_retained[:top_n]
        trim_removed_symbols = ranked_retained[top_n:]
        removed_symbols.extend(trim_removed_symbols)

    added_symbols: List[str] = []
    retained_set = set(retained_symbols)
    for symbol in top10_symbols:
        if len(retained_symbols) + len(added_symbols) >= top_n:
            break
        if symbol not in retained_set and symbol not in added_symbols:
            added_symbols.append(symbol)

    final_symbols = retained_symbols + added_symbols
    if len(final_symbols) < top_n:
        raise RuntimeError(
            f"Unable to fill buffered target holdings to {top_n}: "
            f"retained={len(retained_symbols)} added={len(added_symbols)}"
        )

    final_symbols = sorted(
        final_symbols,
        key=lambda symbol: (
            safe_int((item_by_symbol.get(symbol) or {}).get("strategy_rank")) or 999999,
            symbol,
        ),
    )
    final_symbol_set = set(final_symbols)
    current_symbol_set = set(current_symbols)
    component_changed = final_symbol_set != current_symbol_set
    equal_weight = 100.0 / len(final_symbols) if final_symbols else 0.0
    execution_weights = build_min_turnover_execution_weights(
        final_symbols=final_symbols,
        added_symbols=added_symbols,
        current_by_symbol=current_by_symbol,
    )

    target_items: List[Dict[str, Any]] = []
    for symbol in final_symbols:
        item = dict(item_by_symbol.get(symbol) or {})
        current_weight = (current_by_symbol.get(symbol) or {}).get("weight_pct")
        execution_weight = execution_weights.get(symbol, equal_weight)
        strategy_action = "buy" if symbol in added_symbols else "keep"
        if strategy_action == "keep":
            current_number = safe_float(current_weight)
            if current_number is None or abs(execution_weight - current_number) > 0.005:
                strategy_action = "adjust"
        item["stock_symbol"] = symbol
        item["stock_name"] = item.get("stock_name") or (current_by_symbol.get(symbol) or {}).get("stock_name") or ""
        item["top_normalized_weight_pct"] = equal_weight
        item["rebalance_weight_pct"] = execution_weight
        item["strategy_action"] = strategy_action
        item["current_weight_pct"] = current_weight
        target_items.append(item)

    removed_items: List[Dict[str, Any]] = []
    for symbol in removed_symbols:
        item = dict(item_by_symbol.get(symbol) or {})
        item["stock_symbol"] = symbol
        item["stock_name"] = item.get("stock_name") or (current_by_symbol.get(symbol) or {}).get("stock_name") or ""
        item["strategy_action"] = "trim" if symbol in trim_removed_symbols else "sell"
        item["current_weight_pct"] = (current_by_symbol.get(symbol) or {}).get("weight_pct")
        removed_items.append(item)

    return {
        "strategy_name": BUFFER_STRATEGY_NAME,
        "top_n": top_n,
        "sell_rank": sell_rank,
        "top10_symbols": top10_symbols,
        "top12_symbols": [item["stock_symbol"] for item in top12_items],
        "current_symbols": current_symbols,
        "retained_symbols": retained_symbols,
        "removed_symbols": removed_symbols,
        "trim_removed_symbols": trim_removed_symbols,
        "added_symbols": added_symbols,
        "final_symbols": final_symbols,
        "component_changed": component_changed,
        "execution_weight_rule": BUFFER_EXECUTION_WEIGHT_RULE,
        "target_items": target_items,
        "removed_items": removed_items,
        "current_items": current_items,
        "summary": {
            "current": [
                _symbol_label(symbol, item_by_symbol, current_by_symbol)
                for symbol in current_symbols
            ],
            "retained": [
                _symbol_label(symbol, item_by_symbol, current_by_symbol)
                for symbol in retained_symbols
            ],
            "removed": [
                _symbol_label(symbol, item_by_symbol, current_by_symbol)
                for symbol in removed_symbols
            ],
            "added": [
                _symbol_label(symbol, item_by_symbol, current_by_symbol)
                for symbol in added_symbols
            ],
            "final": [
                _symbol_label(symbol, item_by_symbol, current_by_symbol)
                for symbol in final_symbols
            ],
        },
    }


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
    if not raw_symbol:
        return "missing symbol"
    if not price or price <= 0:
        return "missing valid current price"
    if status is not None and status <= 0:
        return f"status={status}"
    if quote_type in get_rebalance_blocked_quote_types():
        return f"quote_type={quote_type} blocked"
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
    try:
        payload = await fetch_target_cube_current_payload(
            cookie=cookie,
            target_cube_symbol=target_cube_symbol,
            timeout=timeout,
        )
        event = payload.get("last_rb")
        return event if isinstance(event, dict) else None
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
    skipped_items: List[Dict[str, Any]] = []
    for item in rebalance_items:
        raw_symbol = to_raw_xueqiu_symbol(item["stock_symbol"])
        weight = safe_float(item.get("rebalance_weight_pct")) or 0.0
        quote = quotes.get(raw_symbol) or {}
        metadata = metadata_map.get(raw_symbol) or {}
        quote_body = (quote.get("quote") or {})
        rejection = describe_rebalance_quote_rejection(raw_symbol, quote_body)
        if rejection:
            item["rebalance_weight_pct"] = weight
            item["rebalance_skip_reason"] = rejection
            item["rebalance_quote_type"] = safe_int(quote_body.get("type"))
            item["stock_name"] = metadata.get("stock_name") or quote.get("name") or item.get("stock_name") or ""
            item["stock_id"] = metadata.get("stock_id")
            item["segment_name"] = metadata.get("segment_name") or item.get("segment_name") or "其他"
            skipped_items.append(dict(item))
            logger.warning("Skipping unsupported Xueqiu rebalance stock %s: %s", raw_symbol, rejection)
            continue
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
        "comment": "",
        "market": "cn",
        "holdings": holdings,
        "top_items": rebalance_items,
        "skipped_items": skipped_items,
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


def extract_rebalance_action_lines(rebalance_response: Optional[Dict[str, Any]]) -> List[str]:
    if not isinstance(rebalance_response, dict):
        return []

    lines: List[str] = []
    comment = rebalance_response.get("comment")
    if isinstance(comment, str):
        lines.extend(line.strip() for line in comment.splitlines() if line.strip())

    histories = rebalance_response.get("rebalancing_histories") or []
    if isinstance(histories, list) and histories:
        lines.append("调仓明细:")
        for history in histories:
            if isinstance(history, dict):
                def first_present(*keys: str) -> Any:
                    for key in keys:
                        if key in history and history[key] is not None:
                            return history[key]
                    return None

                stock_name = first_present("stock_name", "stockName", "name")
                stock_symbol = first_present("stock_symbol", "stockSymbol", "symbol")
                prev_weight = first_present("prev_weight", "prevWeight", "old_weight")
                target_weight = first_present("target_weight", "targetWeight", "weight")
                if stock_name or stock_symbol or prev_weight is not None or target_weight is not None:
                    lines.append(
                        f"{stock_name or ''} {stock_symbol or ''}: "
                        f"{fmt_number(prev_weight, suffix='%')} -> {fmt_number(target_weight, suffix='%')}"
                    )
                else:
                    lines.append(json.dumps(history, ensure_ascii=False, separators=(",", ":")))
            elif history is not None:
                lines.append(str(history))
    return lines


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
    strategy_plan: Optional[Dict[str, Any]] = None,
    active_filter_summary: Optional[Dict[str, Any]] = None,
    holdings_snapshot_result: Optional[Dict[str, Any]] = None,
    dry_run: bool = False,
) -> str:
    ranking = aggregate["ranking"]
    top_items = (
        rebalance_payload.get("top_items")
        if rebalance_payload
        else strategy_plan.get("target_items")
        if strategy_plan
        else add_top_normalized_weights(ranking, top_n)
    )
    failed_results = aggregate["failed_results"]
    success_count = aggregate["success_count"]
    stock_count = count_non_cash_ranking_items(ranking)
    total_stock_weight_pct = safe_float(aggregate.get("total_stock_weight_pct")) or 0.0
    cash_item = get_cash_ranking_item(aggregate) or {}
    cash_weight_pct = safe_float(cash_item.get("composite_weight_pct"))
    report_table_items = build_report_table_items(
        top_items=top_items,
        aggregate=aggregate,
        top_n=top_n,
    )
    filter_label = (
        f"活跃{active_filter_summary.get('lookback_days')}天"
        if active_filter_summary
        else ""
    )
    lines = [
        f"雪球年榜1000{filter_label}组合综合持仓权重 Top{top_n}",
        "",
        f"统计时间: {run_at.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"年榜缓存时间: {rank_cache_fetched_at.strftime('%Y-%m-%d %H:%M:%S') if rank_cache_fetched_at else '-'}",
        f"年榜本次刷新: {'是' if rank_cache_refreshed else '否'}",
        f"组合总数: {len(cubes)}",
    ]
    if active_filter_summary:
        lines.extend(
            [
                f"活跃筛选: 最近 {active_filter_summary.get('lookback_days')} 天有调仓",
                f"活跃截止时间: {fmt_datetime_value(active_filter_summary.get('active_since'))}",
                f"活跃组合: {active_filter_summary.get('active_cube_count')}",
                f"非活跃组合: {active_filter_summary.get('inactive_cube_count')}",
                f"活跃检查失败: {active_filter_summary.get('activity_failed_count')}",
                f"持仓回退到last_rb: {active_filter_summary.get('holdings_fallback_count')}",
            ]
        )
    if holdings_snapshot_result:
        if holdings_snapshot_result.get("error"):
            lines.append(f"DuckDB持仓快照: 写入失败 - {holdings_snapshot_result.get('error')}")
        else:
            lines.append(
                "DuckDB持仓快照: "
                f"{holdings_snapshot_result.get('table')} "
                f"date={holdings_snapshot_result.get('snapshot_date')} "
                f"rows={holdings_snapshot_result.get('saved_rows')} "
                f"cubes={holdings_snapshot_result.get('replaced_cube_count')}"
            )
    scope_text = (
        f"先筛选最近 {active_filter_summary.get('lookback_days')} 天有调仓的组合；"
        "再把成功拉取的活跃组合等权合成一个组合"
        if active_filter_summary
        else "把成功拉取的组合等权合成一个组合"
    )
    lines.extend(
        [
            f"拉取成功: {success_count}",
            f"拉取失败: {len(failed_results)}",
            f"覆盖股票数: {stock_count}",
            f"非现金持仓合计权重: {fmt_number(total_stock_weight_pct / success_count if success_count else None, suffix='%')}",
            f"现金综合权重: {fmt_number(cash_weight_pct, suffix='%')}",
            f"现金持仓组合: {cash_item.get('holding_cube_count', 0)} ({fmt_number(cash_item.get('holding_cube_ratio_pct'), suffix='%')})",
            "",
            f"统计口径: {scope_text}；个股/现金综合权重 = 该项在统计组合中的持仓权重之和 / 统计组合数，未持有记为 0。",
            f"调仓策略: {strategy_plan.get('strategy_name') if strategy_plan else f'选取综合权重最高的 Top{top_n} 后归一化'}。",
        ]
    )
    if strategy_plan:
        plan_summary = strategy_plan.get("summary") or {}
        lines.extend(
            [
                f"买入/补位: 从当前综合排名 Top{strategy_plan.get('top_n', top_n)} 中补足到 {strategy_plan.get('top_n', top_n)} 只。",
                f"卖出: 已有持仓跌出 Top{strategy_plan.get('sell_rank', BUFFER_STRATEGY_SELL_RANK)} 才卖。",
                f"权重: {strategy_plan.get('execution_weight_rule') or BUFFER_EXECUTION_WEIGHT_RULE}；成分无变化不提交调仓。",
                f"当前持仓: {'、'.join(plan_summary.get('current') or []) or '-'}",
                f"保留: {'、'.join(plan_summary.get('retained') or []) or '-'}",
                f"卖出: {'、'.join(plan_summary.get('removed') or []) or '-'}",
                f"买入: {'、'.join(plan_summary.get('added') or []) or '-'}",
                f"最终持仓: {'、'.join(plan_summary.get('final') or []) or '-'}",
            ]
        )
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
        rebalance_action_lines = extract_rebalance_action_lines(rebalance_response)
        if rebalance_action_lines:
            lines.extend(["", "雪球返回调仓动作:"])
            lines.extend(f"- {line}" for line in rebalance_action_lines)
    elif isinstance(rebalance_response, dict) and rebalance_response.get("skipped"):
        lines.extend(["", "调仓状态: 已跳过", f"任务提示: {rebalance_response.get('message') or '-'}"])
    lines.extend(
        [
            "",
            "| 综合排名 | 股票 | 名称 | 综合权重 | 目标权重 | 调仓权重 | 策略动作 | 当前权重 | 持仓组合数 | 占成功组合 | 持有组合平均权重 | 示例组合 |",
            "| ---: | --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for index, item in enumerate(report_table_items, start=1):
        lines.append(
            "| "
            + " | ".join(
                [
                    str(item.get("strategy_rank") or item.get("composite_rank") or index),
                    str(item.get("stock_symbol") or ""),
                    str(item.get("stock_name") or ""),
                    fmt_number(item.get("composite_weight_pct"), suffix="%"),
                    fmt_number(item.get("top_normalized_weight_pct"), suffix="%"),
                    fmt_number(item.get("rebalance_weight_pct"), suffix="%"),
                    str(item.get("strategy_action") or ""),
                    fmt_number(item.get("current_weight_pct"), suffix="%"),
                    str(item.get("holding_cube_count") or ""),
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


def build_report_html(
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
    strategy_plan: Optional[Dict[str, Any]] = None,
    active_filter_summary: Optional[Dict[str, Any]] = None,
    holdings_snapshot_result: Optional[Dict[str, Any]] = None,
    dry_run: bool = False,
) -> str:
    ranking = aggregate["ranking"]
    top_items = (
        rebalance_payload.get("top_items")
        if rebalance_payload
        else strategy_plan.get("target_items")
        if strategy_plan
        else add_top_normalized_weights(ranking, top_n)
    )
    failed_results = aggregate["failed_results"]
    success_count = aggregate["success_count"]
    stock_count = count_non_cash_ranking_items(ranking)
    total_stock_weight_pct = safe_float(aggregate.get("total_stock_weight_pct")) or 0.0
    cash_item = get_cash_ranking_item(aggregate) or {}
    cash_weight_pct = safe_float(cash_item.get("composite_weight_pct"))
    report_table_items = build_report_table_items(
        top_items=top_items,
        aggregate=aggregate,
        top_n=top_n,
    )
    filter_label = (
        f"活跃{active_filter_summary.get('lookback_days')}天"
        if active_filter_summary
        else ""
    )
    scope_text = (
        f"先筛选最近 {active_filter_summary.get('lookback_days')} 天有调仓的组合；"
        "再把成功拉取的活跃组合等权合成一个组合"
        if active_filter_summary
        else "把成功拉取的组合等权合成一个组合"
    )

    def esc(value: Any) -> str:
        return html.escape(str(value if value is not None else ""))

    summary_rows = [
        ("统计时间", run_at.strftime("%Y-%m-%d %H:%M:%S %Z")),
        ("年榜缓存时间", rank_cache_fetched_at.strftime("%Y-%m-%d %H:%M:%S") if rank_cache_fetched_at else "-"),
        ("年榜本次刷新", "是" if rank_cache_refreshed else "否"),
        ("组合总数", len(cubes)),
    ]
    if active_filter_summary:
        summary_rows.extend(
            [
                ("活跃筛选", f"最近 {active_filter_summary.get('lookback_days')} 天有调仓"),
                ("活跃截止时间", fmt_datetime_value(active_filter_summary.get("active_since"))),
                ("活跃组合", active_filter_summary.get("active_cube_count")),
                ("非活跃组合", active_filter_summary.get("inactive_cube_count")),
                ("活跃检查失败", active_filter_summary.get("activity_failed_count")),
                ("持仓回退到last_rb", active_filter_summary.get("holdings_fallback_count")),
            ]
        )
    if holdings_snapshot_result:
        if holdings_snapshot_result.get("error"):
            summary_rows.append(("DuckDB持仓快照", f"写入失败 - {holdings_snapshot_result.get('error')}"))
        else:
            summary_rows.append(
                (
                    "DuckDB持仓快照",
                    (
                        f"{holdings_snapshot_result.get('table')} "
                        f"date={holdings_snapshot_result.get('snapshot_date')} "
                        f"rows={holdings_snapshot_result.get('saved_rows')} "
                        f"cubes={holdings_snapshot_result.get('replaced_cube_count')}"
                    ),
                )
            )
    summary_rows.extend(
        [
            ("拉取成功", success_count),
            ("拉取失败", len(failed_results)),
            ("覆盖股票数", stock_count),
            ("非现金持仓合计权重", fmt_number(total_stock_weight_pct / success_count if success_count else None, suffix="%")),
            ("现金综合权重", fmt_number(cash_weight_pct, suffix="%")),
            ("现金持仓组合", f"{cash_item.get('holding_cube_count', 0)} ({fmt_number(cash_item.get('holding_cube_ratio_pct'), suffix='%')})"),
            ("调仓策略", strategy_plan.get("strategy_name") if strategy_plan else f"Top{top_n}综合权重归一"),
        ]
    )
    if strategy_plan:
        plan_summary = strategy_plan.get("summary") or {}
        summary_rows.extend(
            [
                ("买入/补位", f"从当前综合排名 Top{strategy_plan.get('top_n', top_n)} 中补足到 {strategy_plan.get('top_n', top_n)} 只"),
                ("卖出规则", f"已有持仓跌出 Top{strategy_plan.get('sell_rank', BUFFER_STRATEGY_SELL_RANK)} 才卖"),
                ("权重规则", f"{strategy_plan.get('execution_weight_rule') or BUFFER_EXECUTION_WEIGHT_RULE}；成分无变化不提交调仓"),
                ("当前持仓", "、".join(plan_summary.get("current") or []) or "-"),
                ("保留", "、".join(plan_summary.get("retained") or []) or "-"),
                ("卖出", "、".join(plan_summary.get("removed") or []) or "-"),
                ("买入", "、".join(plan_summary.get("added") or []) or "-"),
                ("最终持仓", "、".join(plan_summary.get("final") or []) or "-"),
            ]
        )
    if rebalance_payload:
        status = "dry-run" if dry_run else "已提交"
        if isinstance(rebalance_response, dict) and rebalance_response.get("skipped"):
            status = "已跳过"
        elif isinstance(rebalance_response, dict) and rebalance_response.get("status"):
            status = str(rebalance_response.get("status"))
        summary_rows.extend(
            [
                ("目标组合", f"{target_cube_symbol or rebalance_payload.get('target_cube_symbol')} / cube_id={rebalance_payload.get('cube_id')}"),
                ("调仓状态", status),
                ("目标现金", fmt_number(rebalance_payload.get("cash"), suffix="%")),
            ]
        )
        if isinstance(rebalance_response, dict) and rebalance_response.get("error_message"):
            summary_rows.append(("雪球提示", rebalance_response.get("error_message")))
        if isinstance(rebalance_response, dict) and rebalance_response.get("message"):
            summary_rows.append(("任务提示", rebalance_response.get("message")))
        if rebalance_skipped_items:
            skipped_preview = [
                f"{item.get('stock_symbol')}({item.get('stock_name') or ''}, {item.get('rebalance_skip_reason')})"
                for item in rebalance_skipped_items[:10]
            ]
            summary_rows.append(("已跳过不可调仓标的", "; ".join(skipped_preview)))
    elif isinstance(rebalance_response, dict) and rebalance_response.get("skipped"):
        summary_rows.append(("调仓状态", "已跳过"))
        summary_rows.append(("任务提示", rebalance_response.get("message") or "-"))
    rebalance_action_lines = extract_rebalance_action_lines(rebalance_response)

    rows_html = "\n".join(
        f"<tr><th>{esc(label)}</th><td>{esc(value)}</td></tr>"
        for label, value in summary_rows
    )
    table_rows = []
    for index, item in enumerate(report_table_items, start=1):
        table_rows.append(
            "<tr>"
            f"<td class=\"num\">{esc(item.get('strategy_rank') or item.get('composite_rank') or index)}</td>"
            f"<td>{esc(item.get('stock_symbol'))}</td>"
            f"<td>{esc(item.get('stock_name') or '')}</td>"
            f"<td class=\"num\">{esc(fmt_number(item.get('composite_weight_pct'), suffix='%'))}</td>"
            f"<td class=\"num\">{esc(fmt_number(item.get('top_normalized_weight_pct'), suffix='%'))}</td>"
            f"<td class=\"num\">{esc(fmt_number(item.get('rebalance_weight_pct'), suffix='%'))}</td>"
            f"<td>{esc(item.get('strategy_action') or '')}</td>"
            f"<td class=\"num\">{esc(fmt_number(item.get('current_weight_pct'), suffix='%'))}</td>"
            f"<td class=\"num\">{esc(item.get('holding_cube_count'))}</td>"
            f"<td class=\"num\">{esc(fmt_number(item.get('holding_cube_ratio_pct'), suffix='%'))}</td>"
            f"<td class=\"num\">{esc(fmt_number(item.get('average_weight_pct'), suffix='%'))}</td>"
            f"<td>{esc('、'.join(item.get('example_cubes') or []))}</td>"
            "</tr>"
        )
    failed_html = ""
    if failed_results:
        failed_items = "".join(
            f"<li>{esc(result.cube.symbol)} {esc(result.cube.cube_name)}: {esc(result.error)}</li>"
            for result in failed_results[:10]
        )
        failed_html = f"<h2>拉取失败样例</h2><ul>{failed_items}</ul>"
    rebalance_action_html = ""
    if rebalance_action_lines:
        action_items = "".join(f"<li>{esc(line)}</li>" for line in rebalance_action_lines)
        rebalance_action_html = f"<h2>雪球返回调仓动作</h2><ul>{action_items}</ul>"
    final_weight_text = (
        strategy_plan.get("execution_weight_rule") or BUFFER_EXECUTION_WEIGHT_RULE
        if strategy_plan
        else f"选取综合权重最高的 Top{top_n} 后归一化"
    )

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #111827; line-height: 1.45; }}
    h1 {{ font-size: 20px; margin: 0 0 16px; }}
    h2 {{ font-size: 16px; margin: 20px 0 8px; }}
    p {{ margin: 8px 0; }}
    table {{ border-collapse: collapse; width: 100%; margin: 12px 0 20px; font-size: 13px; }}
    th, td {{ border: 1px solid #d1d5db; padding: 7px 9px; vertical-align: top; }}
    th {{ background: #f3f4f6; text-align: left; font-weight: 600; }}
    .summary th {{ width: 160px; }}
    .num {{ text-align: right; white-space: nowrap; }}
  </style>
</head>
<body>
  <h1>雪球年榜1000{filter_label}组合综合持仓权重 Top{top_n}</h1>
  <table class="summary">{rows_html}</table>
  {rebalance_action_html}
  <p>统计口径: {esc(scope_text)}；个股/现金综合权重 = 该项在统计组合中的持仓权重之和 / 统计组合数，未持有记为 0。</p>
  <p>最终权重: {esc(final_weight_text)}。</p>
  <table>
    <thead>
      <tr>
        <th>综合排名</th><th>股票</th><th>名称</th><th>综合权重</th><th>目标权重</th><th>调仓权重</th><th>策略动作</th><th>当前权重</th>
        <th>持仓组合数</th><th>占成功组合</th><th>持有组合平均权重</th><th>示例组合</th>
      </tr>
    </thead>
    <tbody>
      {''.join(table_rows)}
    </tbody>
  </table>
  {failed_html}
</body>
</html>"""


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
    strategy_plan: Optional[Dict[str, Any]] = None,
    active_filter_summary: Optional[Dict[str, Any]] = None,
    holdings_snapshot_result: Optional[Dict[str, Any]] = None,
) -> Path:
    run_dir = output_dir / run_at.strftime("%Y-%m-%d")
    run_dir.mkdir(parents=True, exist_ok=True)
    ranking = aggregate["ranking"]
    failed_results = aggregate["failed_results"]
    stock_count = count_non_cash_ranking_items(ranking)
    cash_item = get_cash_ranking_item(aggregate)
    top_by_symbol = {item["stock_symbol"]: item for item in top_items}
    ranking_rows = []
    for row in ranking:
        enriched = dict(row)
        top_row = top_by_symbol.get(row["stock_symbol"])
        if top_row:
            enriched["strategy_rank"] = top_row.get("strategy_rank")
            enriched["top_normalized_weight_pct"] = top_row.get("top_normalized_weight_pct")
            enriched["rebalance_weight_pct"] = top_row.get("rebalance_weight_pct")
            enriched["strategy_action"] = top_row.get("strategy_action")
            enriched["current_weight_pct"] = top_row.get("current_weight_pct")
        ranking_rows.append(enriched)

    metadata = {
        "run_at": run_at.isoformat(),
        "rank_cache_fetched_at": rank_cache_fetched_at.isoformat() if rank_cache_fetched_at else None,
        "rank_cache_refreshed": rank_cache_refreshed,
        "cube_count": len(cubes),
        "active_filter": active_filter_summary,
        "holdings_snapshot": holdings_snapshot_result,
        "success_count": aggregate["success_count"],
        "failed_count": len(failed_results),
        "stock_count": stock_count,
        "total_stock_weight_pct": aggregate.get("total_stock_weight_pct"),
        "total_cash_weight_pct": aggregate.get("total_cash_weight_pct"),
        "total_portfolio_weight_pct": aggregate.get("total_portfolio_weight_pct"),
        "cash_item": cash_item,
        "top": top_items,
        "strategy_plan": strategy_plan,
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
            "strategy_rank",
            "stock_symbol",
            "stock_name",
            "is_cash",
            "composite_weight_pct",
            "global_normalized_weight_pct",
            "top_normalized_weight_pct",
            "rebalance_weight_pct",
            "strategy_action",
            "current_weight_pct",
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
            "is_cash",
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
            stock_count=count_non_cash_ranking_items(aggregate.get("ranking") or []),
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
    active_rebalance_days: Optional[int] = None,
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

    active_filter_summary: Optional[Dict[str, Any]] = None
    active_since = run_at - timedelta(days=active_rebalance_days or 0)
    current_results = await fetch_all_cube_current(
        cubes,
        cookie=cookie,
        workers=workers,
        timeout=timeout,
        retries=retries,
        active_since=active_since,
    )
    holdings_snapshot_result: Optional[Dict[str, Any]] = None
    try:
        holdings_snapshot_result = save_xueqiu_cube_holdings_snapshots_to_duckdb(
            run_at=run_at,
            current_results=current_results,
            active_rebalance_days=active_rebalance_days,
        )
        logger.info(
            "Saved Xueqiu cube holdings snapshots to DuckDB: table=%s date=%s rows=%s cubes=%s failed=%s",
            holdings_snapshot_result.get("table"),
            holdings_snapshot_result.get("snapshot_date"),
            holdings_snapshot_result.get("saved_rows"),
            holdings_snapshot_result.get("replaced_cube_count"),
            holdings_snapshot_result.get("failed_cube_count"),
        )
    except Exception as exc:  # noqa: BLE001
        holdings_snapshot_result = {
            "table": XUEQIU_CUBE_HOLDINGS_SNAPSHOT_TABLE,
            "database": ANALYTICS_DB_PATH,
            "error": str(exc),
        }
        logger.warning("Failed to save Xueqiu cube holdings snapshots to DuckDB: %s", exc)
    if active_rebalance_days and active_rebalance_days > 0:
        active_filter_summary = build_active_filter_summary(
            source_cubes=cubes,
            current_results=current_results,
            active_since=active_since,
            lookback_days=active_rebalance_days,
        )
        logger.info(
            "Filtered Xueqiu cubes by latest rebalance within %s days using current snapshots: "
            "active=%s source=%s failed=%s fallback_holdings=%s",
            active_rebalance_days,
            active_filter_summary["active_cube_count"],
            active_filter_summary["source_cube_count"],
            active_filter_summary["activity_failed_count"],
            active_filter_summary["holdings_fallback_count"],
        )
        failure_limit = max(
            5,
            math.ceil(len(cubes) * ACTIVE_REBALANCE_MAX_FAILED_RATIO),
        )
        if active_filter_summary["activity_failed_count"] > failure_limit:
            raise RuntimeError(
                "Xueqiu current snapshot fetch failed for too many cubes: "
                f"failed={active_filter_summary['activity_failed_count']} "
                f"limit={failure_limit} source={len(cubes)}"
            )
        current_results = [
            result
            for result in current_results
            if result.active or result.error
        ]
        if not any(not result.error for result in current_results):
            raise RuntimeError(f"No Xueqiu cubes rebalanced within {active_rebalance_days} days.")

    results = [
        CubeFetchResult(cube=result.cube, holdings=result.holdings, error=result.error)
        for result in current_results
    ]
    aggregate = aggregate_holdings(results)
    top_items = add_top_normalized_weights(aggregate["ranking"], top_n)
    rebalance_skipped_items: List[Dict[str, Any]] = []
    rebalance_payload = None
    rebalance_response = None
    strategy_plan: Optional[Dict[str, Any]] = None
    resolved_target_cube_id = None
    latest_target_rebalance = None

    if execute_rebalance:
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
        current_target_holdings = await fetch_target_cube_holdings(
            cookie=cookie,
            target_cube_symbol=target_cube_symbol,
            timeout=timeout,
        )
        strategy_plan = build_equal_top10_top12_buffer_plan(
            ranking=aggregate["ranking"],
            current_holdings=current_target_holdings,
            top_n=top_n,
            sell_rank=max(BUFFER_STRATEGY_SELL_RANK, top_n),
        )
        top_items = strategy_plan["target_items"]
        if strategy_plan.get("component_changed"):
            rebalance_payload = await build_rebalance_payload(
                cookie=cookie,
                target_cube_symbol=target_cube_symbol,
                target_cube_id=resolved_target_cube_id,
                top_items=top_items,
                timeout=timeout,
            )
            rebalance_payload["strategy"] = strategy_plan.get("strategy_name")
            rebalance_payload["strategy_plan"] = strategy_plan
            rebalance_payload["active_filter"] = active_filter_summary
            rebalance_skipped_items.extend(rebalance_payload.get("skipped_items") or [])
            top_items = rebalance_payload["top_items"]
            if rebalance_payload.get("holdings"):
                rebalance_response = await create_xueqiu_rebalance(
                    cookie=cookie,
                    payload=rebalance_payload,
                    dry_run=dry_run,
                    timeout=timeout,
                )
            else:
                rebalance_response = {
                    "skipped": True,
                    "message": "目标标的均不可调仓，已保留现金仓位并跳过提交。",
                    "strategy": strategy_plan.get("strategy_name"),
                    "strategy_plan": strategy_plan,
                    "active_filter": active_filter_summary,
                }
        else:
            rebalance_response = {
                "skipped": True,
                "message": "目标组合成分未变化，按Top10等权缓冲策略不提交调仓。",
                "strategy": strategy_plan.get("strategy_name"),
                "strategy_plan": strategy_plan,
                "active_filter": active_filter_summary,
            }

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
        strategy_plan=strategy_plan,
        active_filter_summary=active_filter_summary,
        holdings_snapshot_result=holdings_snapshot_result,
        dry_run=dry_run,
    )
    report_html = build_report_html(
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
        strategy_plan=strategy_plan,
        active_filter_summary=active_filter_summary,
        holdings_snapshot_result=holdings_snapshot_result,
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
        strategy_plan=strategy_plan,
        active_filter_summary=active_filter_summary,
        holdings_snapshot_result=holdings_snapshot_result,
    )
    status = "DRY_RUN" if dry_run and execute_rebalance else "SUCCESS"
    if isinstance(rebalance_response, dict) and rebalance_response.get("skipped"):
        status = "SKIPPED"
    active_message = ""
    if active_filter_summary:
        active_message = (
            f" active={active_filter_summary.get('active_cube_count')}/"
            f"{active_filter_summary.get('source_cube_count')}"
        )
    snapshot_message = ""
    if holdings_snapshot_result:
        if holdings_snapshot_result.get("error"):
            snapshot_message = " snapshot_error=1"
        else:
            snapshot_message = f" snapshot_rows={holdings_snapshot_result.get('saved_rows')}"
    message = (
        f"success={aggregate['success_count']} failed={len(aggregate['failed_results'])} "
        f"stocks={len(aggregate['ranking'])}{active_message}{snapshot_message} output={output_path}"
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
        subject_filter = (
            f"活跃{active_filter_summary.get('lookback_days')}天"
            if active_filter_summary
            else ""
        )
        subject = (
            f"雪球年榜1000{subject_filter}组合Top{top_n}自动调仓 - {run_at.strftime('%Y-%m-%d')}"
            if execute_rebalance
            else f"雪球年榜1000{subject_filter}组合综合持仓权重 Top{top_n} - {run_at.strftime('%Y-%m-%d')}"
        )
        send_configured_email(
            "xueqiu_top_holdings_report",
            subject,
            report_html,
            mimeType="html",
            receiver_email=receiver_email,
        )
    return {
        "skipped": False,
        "record_id": record_id,
        "output_dir": str(output_path),
        "rank_cache_fetched_at": rank_cache_fetched_at.isoformat() if rank_cache_fetched_at else None,
        "rank_cache_refreshed": rank_cache_refreshed,
        "success_count": aggregate["success_count"],
        "failed_count": len(aggregate["failed_results"]),
        "stock_count": count_non_cash_ranking_items(aggregate["ranking"]),
        "target_cube_symbol": target_cube_symbol if execute_rebalance else None,
        "target_cube_id": resolved_target_cube_id,
        "dry_run": dry_run,
        "rebalance_response": rebalance_response,
        "rebalance_skipped": rebalance_skipped_items,
        "active_filter": active_filter_summary,
        "holdings_snapshot": holdings_snapshot_result,
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
            active_rebalance_days=ACTIVE_REBALANCE_LOOKBACK_DAYS,
        )
    )
    if result.get("skipped"):
        return str(result.get("message"))
    response = result.get("rebalance_response") or {}
    active_filter = result.get("active_filter") or {}
    active_message = (
        f"active={active_filter.get('active_cube_count')}/{active_filter.get('source_cube_count')} "
        if active_filter
        else ""
    )
    holdings_snapshot = result.get("holdings_snapshot") or {}
    snapshot_message = (
        "snapshot_error=1 "
        if holdings_snapshot.get("error")
        else f"snapshot_rows={holdings_snapshot.get('saved_rows')} "
    )
    return (
        "雪球Top1000活跃90天综合持仓自动调仓 "
        f"record_id={result.get('record_id')} "
        f"target={result.get('target_cube_symbol')} "
        f"cube_id={result.get('target_cube_id')} "
        f"success={result.get('success_count')} "
        f"failed={result.get('failed_count')} "
        f"stocks={result.get('stock_count')} "
        f"{active_message}"
        f"{snapshot_message}"
        f"rebalance_skipped={response.get('skipped') if isinstance(response, dict) else None} "
        f"rebalance_id={response.get('id') if isinstance(response, dict) else None} "
        f"rebalance_status={response.get('status') if isinstance(response, dict) else None}"
    )


def process_xueqiu_year_rank_refresh_for_robot() -> str:
    async def _run() -> Dict[str, Any]:
        cookie = get_latest_cookie()
        fetched_at = datetime.now()
        cubes = await fetch_year_top_cubes(
            cookie=cookie,
            target_count=RANK_TARGET_COUNT,
            page_size=RANK_PAGE_SIZE,
            timeout=DEFAULT_TIMEOUT,
        )
        save_year_top_cubes(cubes, fetched_at)
        return {
            "fetched_at": fetched_at,
            "count": len(cubes),
            "first_symbol": cubes[0].symbol if cubes else None,
            "last_symbol": cubes[-1].symbol if cubes else None,
        }

    result = asyncio.run(_run())
    fetched_at = result["fetched_at"]
    return (
        "雪球年榜Top1000缓存刷新 "
        f"count={result.get('count')} "
        f"fetched_at={fetched_at.strftime('%Y-%m-%d %H:%M:%S')} "
        f"first={result.get('first_symbol')} "
        f"last={result.get('last_symbol')}"
    )


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Xueqiu top1000 cube holding popularity and rebalance.")
    parser.add_argument("--list-path", default=None, help="Optional local top1000_list.json; bypasses DB ranking cache.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for daily file outputs.")
    parser.add_argument("--receiver-email", default=None, help="Receiver email. Defaults to configured report/default email.")
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
    parser.add_argument(
        "--active-rebalance-days",
        type=int,
        default=safe_int(os.getenv("XUEQIU_TOP_HOLDINGS_ACTIVE_REBALANCE_DAYS")),
        help="Only include cubes whose latest rebalance is within N days. Use 0 to disable.",
    )
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
                active_rebalance_days=args.active_rebalance_days,
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
                scenario_key="xueqiu_top_holdings_failure",
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to send failure alert")
        return 1


if __name__ == "__main__":
    sys.exit(main())

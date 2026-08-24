from __future__ import annotations

import argparse
import asyncio
import csv
import html
import json
import logging
import math
import os
import random
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
    ETFFearGreedCloneHistory,
    SessionLocal,
    SystemServiceCredential,
    XueqiuCubeActivityCache,
    XueqiuCubeRankCache,
    XueqiuStrategyConfig,
    XueqiuTopHoldingsRun,
)
from ..core.duckdb_utils import ANALYTICS_DB_PATH, connect_duckdb
from ..core.services.fear_greed_signal_config import (
    MA5_BOTTOM_SCORE_DEFAULT,
    MA5_LOOKBACK_DAYS_DEFAULT,
    MA5_TOP_SCORE_DEFAULT,
    VOLUME_BOTTOM_SCORE_DEFAULT,
    VOLUME_EXPAND_STD_DEFAULT,
    VOLUME_SHRINK_STD_DEFAULT,
    VOLUME_TOP_SCORE_DEFAULT,
    load_fear_greed_signal_config,
)
from ..core.utils import send_alert_email, send_configured_email


ROOT = Path(__file__).resolve().parents[2]
CHINA_TZ = ZoneInfo("Asia/Shanghai")
XUEQIU_API_BASE_URL = "https://api.xueqiu.com"
XUEQIU_STOCK_BASE_URL = "https://stock.xueqiu.com"
XUEQIU_WEB_BASE_URL = "https://xueqiu.com"
RANK_CACHE_TYPE = "year"
RANK_CACHE_TTL_DAYS = 7
RANK_CACHE_MIN_VALID_RATIO = 0.90
RANK_CACHE_MIN_VALID_LIMIT = 100
RANK_CACHE_DRIFT_MIN_OVERLAP_RATIO = 0.50
RANK_CACHE_DRIFT_BASELINE_CACHE_MAX_AGE_DAYS = 30
RANK_CACHE_DRIFT_RECENT_SNAPSHOT_COUNT = 10
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
DEFAULT_XUEQIU_REBALANCE_BLOCKED_QUOTE_TYPES = {13, 17}
REBALANCE_QUOTE_BATCH_SIZE = 50
BUFFER_STRATEGY_TOP_N = 10
BUFFER_STRATEGY_SELL_RANK = 12
CSI_ALL_SHARE_FEAR_GREED_SYMBOL = "000985.SH"
FEAR_GREED_FEAR_THRESHOLD = 25.0
FEAR_GREED_GREED_THRESHOLD = 75.0
FEAR_GREED_FEAR_TARGET_COUNT = 10
FEAR_GREED_GREED_TARGET_COUNT = 3
# MA5均线型信号回看天数（默认值，被统一信号配置覆盖）
XUEQIU_MA5_LOOKBACK_DAYS_DEFAULT = 5
REPORT_TABLE_DISPLAY_RANK = BUFFER_STRATEGY_SELL_RANK
BUFFER_STRATEGY_NAME = "Top10等权 + 跌出Top12才卖 + 从Top10补位 + 成分变化才调仓"
BUFFER_RETAIN_WEIGHT_TOLERANCE_PCT = 1.0
BUFFER_EXECUTION_WEIGHT_RULE = (
    f"最小换手：保留成分偏离等权不超过{BUFFER_RETAIN_WEIGHT_TOLERANCE_PCT:g}个百分点时沿用当前权重，"
    "卖出释放权重分配给补位成分"
)
RANK_ACCELERATION_TARGET_CUBE_SYMBOL = "ZH3644546"
RANK_ACCELERATION_COMPARE_TRADING_DAYS = 5
RANK_ACCELERATION_TOP_N = 10
RANK_ACCELERATION_SELL_RANK = 30
RANK_ACCELERATION_CURRENT_RANK_LIMIT = 50
RANK_ACCELERATION_MIN_HOLDING_CUBES = 8
RANK_ACCELERATION_MIN_HOLDING_CUBE_INCREASE = 3
RANK_ACCELERATION_MIN_RANK_CHANGE = 20
RANK_ACCELERATION_NEW_ENTRY_RANK_LIMIT = 20
RANK_ACCELERATION_NEW_ENTRY_MIN_HOLDING_CUBES = 10
RANK_ACCELERATION_RETAIN_CURRENT_RANK_LIMIT = 100
RANK_ACCELERATION_RETAIN_MIN_HOLDING_CUBES = 5
RANK_ACCELERATION_MAX_SEGMENT_POSITIONS = 3
RANK_ACCELERATION_MAX_REPLACEMENTS = 2
RANK_ACCELERATION_MIN_HOLDING_TRADING_DAYS = 5
RANK_ACCELERATION_BUY_CONFIRM_WINDOW = 3
RANK_ACCELERATION_BUY_CONFIRM_MIN_DAYS = 2
RANK_ACCELERATION_SELL_CONFIRM_DAYS = 2
RANK_ACCELERATION_ROLLING_REPLACEMENT_DAYS = 5
RANK_ACCELERATION_ROLLING_MAX_REPLACEMENTS = 3
RANK_ACCELERATION_HARD_EXIT_RANK = 150
RANK_ACCELERATION_HARD_EXIT_MIN_HOLDING_CUBES = 3
RANK_ACCELERATION_STRATEGY_NAME = (
    "5日排名加速TopN等权 + 恐贪择时(恐慌10只/贪婪3只) + 3日买入确认 + 持有5日"
    " + 跌出加速缓冲连续2日才卖 + 每次最多替换2只/滚动5日最多3只"
)
# 星澜叁号：与贰号同一套框架，只是选股指标从“5日排名上升”换成“5日权价比”
# （5日权重倍数÷5日股价倍数，≈1为被动，明显>1说明权重涨幅超过股价，疑似主动加仓），
# 并按恐贪择时动态目标仓位：恐慌≤10只、贪婪≤3只。
WEIGHT_PRICE_RATIO_TARGET_CUBE_SYMBOL = "ZH3664736"
WEIGHT_PRICE_RATIO_TOP_N = 10
WEIGHT_PRICE_RATIO_MIN_RATIO = 1.15
WEIGHT_PRICE_RATIO_SELL_RANK_MULTIPLIER = 3.0
WEIGHT_PRICE_RATIO_CURRENT_RANK_LIMIT = 100
WEIGHT_PRICE_RATIO_MIN_HOLDING_CUBES = 8
WEIGHT_PRICE_RATIO_MIN_HOLDING_CUBE_INCREASE = 2
WEIGHT_PRICE_RATIO_NEW_ENTRY_RANK_LIMIT = 30
WEIGHT_PRICE_RATIO_NEW_ENTRY_MIN_HOLDING_CUBES = 10
WEIGHT_PRICE_RATIO_RETAIN_CURRENT_RANK_LIMIT = 200
WEIGHT_PRICE_RATIO_RETAIN_MIN_HOLDING_CUBES = 5
WEIGHT_PRICE_RATIO_HARD_EXIT_RANK = 250
WEIGHT_PRICE_RATIO_HARD_EXIT_MIN_HOLDING_CUBES = 3
WEIGHT_PRICE_RATIO_STRATEGY_NAME = (
    "5日权价比TopN等权 + 恐贪择时(恐慌10只/贪婪3只) + 3日买入确认 + 持有5日"
    " + 跌出权价比缓冲连续2日才卖 + 每次最多替换2只/滚动5日最多3只"
)
ACTIVE_REBALANCE_LOOKBACK_DAYS = 360
ACTIVE_REBALANCE_MAX_FAILED_RATIO = 0.10
ACTIVE_REBALANCE_ACTIVITY_TYPE = "manager_user_rebalance"
ACTIVE_REBALANCE_ACTIVITY_LABEL = "主理人调仓"
ACTIVE_REBALANCE_CACHE_TTL_HOURS = 24
ACTIVE_REBALANCE_ACTIVITY_REFRESH_WORKERS = 1
ACTIVE_REBALANCE_CACHE_MISS_ERROR = "missing_cached_manager_activity"
XUEQIU_ACTIVITY_REQUEST_MIN_INTERVAL_SECONDS = 0.35
XUEQIU_ACTIVITY_REQUEST_JITTER_SECONDS = 0.08
XUEQIU_ACTIVITY_HTTP_ERROR_COOLDOWN_SECONDS = 5.0
XUEQIU_ACTIVITY_THROTTLE_STATUS_CODES = {400, 403, 429}
XUEQIU_CUBE_RANK_HISTORY_TABLE = "xueqiu_cube_rank_history"
XUEQIU_CUBE_RANK_HISTORY_COLUMNS = [
    "rank_date",
    "fetched_at",
    "rank_type",
    "year_rank",
    "cube_symbol",
    "cube_id",
    "cube_name",
    "screen_name",
    "daily_gain",
    "week_gain",
    "year_gain",
    "recommend_count",
    "net_value",
    "raw_cube_json",
    "created_at",
    "updated_at",
]
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
    "active_rebalance_at",
    "active_rebalance_id",
    "active_rebalance_status",
    "active_rebalance_category",
    "active_rebalance_source",
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
XUEQIU_CUBE_SYMBOL_PATTERN = re.compile(r"^ZH\d+$")


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
class CubeActivityResult:
    symbol: str
    latest_rebalance_at: Optional[datetime] = None
    latest_rebalance_id: Optional[int] = None
    latest_rebalance_status: str = ""
    latest_rebalance_category: str = ""
    source: str = ACTIVE_REBALANCE_ACTIVITY_TYPE
    pages_fetched: int = 0
    page_limit_hit: bool = False
    cache_hit: bool = False
    checked_at: Optional[datetime] = None
    raw_event: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


@dataclass
class CubeCurrentResult:
    cube: CubeInfo
    holdings: List[Dict[str, Any]]
    latest_rebalance_at: Optional[datetime] = None
    latest_rebalance_id: Optional[int] = None
    latest_rebalance_status: str = ""
    active_rebalance_at: Optional[datetime] = None
    active_rebalance_id: Optional[int] = None
    active_rebalance_status: str = ""
    active_rebalance_category: str = ""
    active_rebalance_source: str = ACTIVE_REBALANCE_ACTIVITY_TYPE
    activity_cache_hit: bool = False
    activity_pages_fetched: int = 0
    activity_page_limit_hit: bool = False
    activity_error: Optional[str] = None
    holdings_source: str = ""
    active: bool = False
    error: Optional[str] = None


class AsyncRequestPacer:
    def __init__(
        self,
        *,
        min_interval_seconds: float,
        jitter_seconds: float = 0.0,
    ) -> None:
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self.jitter_seconds = max(0.0, float(jitter_seconds))
        self._next_request_at = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            wait_seconds = max(0.0, self._next_request_at - now)
            jitter_seconds = random.uniform(0.0, self.jitter_seconds) if self.jitter_seconds else 0.0
            self._next_request_at = max(now, self._next_request_at) + self.min_interval_seconds + jitter_seconds
        if wait_seconds > 0:
            await asyncio.sleep(wait_seconds)

    async def cooldown(self, seconds: float) -> None:
        delay = max(0.0, float(seconds))
        if delay <= 0:
            return
        async with self._lock:
            loop = asyncio.get_running_loop()
            self._next_request_at = max(self._next_request_at, loop.time() + delay)


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


def as_china_datetime(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(CHINA_TZ)
    return value.replace(tzinfo=CHINA_TZ)


def activity_cache_checked_after(ttl_hours: float = ACTIVE_REBALANCE_CACHE_TTL_HOURS) -> datetime:
    cutoff = datetime.now(CHINA_TZ) - timedelta(hours=max(0.0, float(ttl_hours)))
    return cutoff.replace(tzinfo=None)


def activity_cache_checked_at() -> datetime:
    return datetime.now(CHINA_TZ).replace(tzinfo=None)


def _rebalance_event_time(event: Dict[str, Any]) -> Optional[datetime]:
    return (
        xueqiu_timestamp_to_datetime(event.get("updated_at"))
        or xueqiu_timestamp_to_datetime(event.get("created_at"))
    )


def _rebalance_event_created_time(event: Dict[str, Any]) -> Optional[datetime]:
    return xueqiu_timestamp_to_datetime(event.get("created_at")) or _rebalance_event_time(event)


def is_manager_rebalance_event(event: Dict[str, Any]) -> bool:
    return (
        str(event.get("category") or "") == "user_rebalancing"
        and str(event.get("status") or "") == "success"
    )


def latest_manager_rebalance_from_events(
    symbol: str,
    events: Iterable[Dict[str, Any]],
    *,
    pages_fetched: int = 0,
    page_limit_hit: bool = False,
    checked_at: Optional[datetime] = None,
) -> CubeActivityResult:
    latest_event: Optional[Dict[str, Any]] = None
    latest_at: Optional[datetime] = None
    for event in events:
        if not isinstance(event, dict) or not is_manager_rebalance_event(event):
            continue
        event_at = _rebalance_event_time(event)
        if event_at is None:
            continue
        if latest_at is None or event_at > latest_at:
            latest_event = event
            latest_at = event_at

    return CubeActivityResult(
        symbol=symbol,
        latest_rebalance_at=latest_at,
        latest_rebalance_id=safe_int((latest_event or {}).get("id")),
        latest_rebalance_status=str((latest_event or {}).get("status") or ""),
        latest_rebalance_category=str((latest_event or {}).get("category") or ""),
        pages_fetched=pages_fetched,
        page_limit_hit=page_limit_hit,
        checked_at=checked_at,
        raw_event=latest_event,
    )


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
        row = db.get(SystemServiceCredential, "snowball")
        cookie = (row.cookie or "").strip() if row else ""
        if "xq_a_token=" in cookie:
            return cookie
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


def is_valid_xueqiu_cube_symbol(symbol: Optional[str]) -> bool:
    return bool(XUEQIU_CUBE_SYMBOL_PATTERN.fullmatch(str(symbol or "").strip().upper()))


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
    dropped_invalid_symbol_count = 0

    for index, cube in enumerate(cubes, start=1):
        symbol = (cube.symbol or "").strip().upper()
        if not symbol:
            dropped_symbol_count += 1
            continue
        if not is_valid_xueqiu_cube_symbol(symbol):
            dropped_invalid_symbol_count += 1
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
    if dropped_symbol_count or dropped_invalid_symbol_count or invalid_rank_count or renumbered_count:
        logger.warning(
            "Normalized Xueqiu year-rank cubes before persistence: "
            "dropped_blank_symbols=%s dropped_invalid_symbols=%s invalid_ranks=%s renumbered=%s final_count=%s",
            dropped_symbol_count,
            dropped_invalid_symbol_count,
            invalid_rank_count,
            renumbered_count,
            len(result),
        )
    return result


def cube_symbol_set(cubes: Iterable[CubeInfo]) -> Set[str]:
    symbols: Set[str] = set()
    for cube in cubes:
        symbol = (cube.symbol or "").strip().upper()
        if is_valid_xueqiu_cube_symbol(symbol):
            symbols.add(symbol)
    return symbols


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
    invalid_symbols: List[str] = []
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
                symbol = (cube.symbol or "").strip().upper()
                if not symbol:
                    continue
                if not is_valid_xueqiu_cube_symbol(symbol):
                    invalid_symbols.append(symbol)
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
    if invalid_symbols:
        sample_symbols = ", ".join(sorted(set(invalid_symbols))[:5])
        logger.warning(
            "Skipped invalid Xueqiu year-rank symbols while fetching: invalid=%s sample=%s",
            len(invalid_symbols),
            sample_symbols or "-",
        )
    if len(cubes) < target_count:
        if invalid_symbols:
            raise RuntimeError(
                "Xueqiu year rank returned invalid cube symbols: "
                f"valid={len(cubes)} invalid={len(invalid_symbols)} expected={target_count}"
            )
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
        cubes = normalize_ranked_cubes(cube_from_cache_row(row) for row in rows)
        if limit >= RANK_CACHE_MIN_VALID_LIMIT and len(cubes) < math.ceil(limit * RANK_CACHE_MIN_VALID_RATIO):
            logger.warning(
                "Ignoring shortened Xueqiu year-rank cache: valid_count=%s requested_limit=%s fetched_at=%s",
                len(cubes),
                limit,
                latest.fetched_at.strftime("%Y-%m-%d %H:%M:%S"),
            )
            return [], latest.fetched_at
        if len(cubes) < limit:
            logger.warning(
                "Using shortened Xueqiu year-rank cache: cached_count=%s requested_limit=%s fetched_at=%s",
                len(cubes),
                limit,
                latest.fetched_at.strftime("%Y-%m-%d %H:%M:%S"),
            )
        return cubes, latest.fetched_at
    finally:
        db.close()


def _duckdb_table_exists(connection, table_name: str) -> bool:
    row = connection.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.tables
        WHERE table_name = ?
        """,
        [table_name],
    ).fetchone()
    return bool(row and row[0])


def load_recent_xueqiu_rank_history_cube_sets(
    *,
    limit: int = RANK_CACHE_DRIFT_RECENT_SNAPSHOT_COUNT,
    exclude_date: Optional[date] = None,
) -> List[Tuple[str, Set[str]]]:
    if limit <= 0:
        return []
    connection = None
    try:
        connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
        if not _duckdb_table_exists(connection, XUEQIU_CUBE_RANK_HISTORY_TABLE):
            return []

        table = _quote_duckdb_identifier(XUEQIU_CUBE_RANK_HISTORY_TABLE)
        params: List[Any] = [RANK_CACHE_TYPE]
        where_clause = "WHERE rank_type = ?"
        if exclude_date is not None:
            where_clause += " AND rank_date <> ?"
            params.append(exclude_date)
        date_rows = connection.execute(
            (
                f"SELECT rank_date, COUNT(DISTINCT cube_symbol) AS cube_count "
                f"FROM {table} "
                f"{where_clause} "
                f"GROUP BY rank_date "
                f"ORDER BY rank_date DESC "
                f"LIMIT ?"
            ),
            [*params, limit],
        ).fetchall()

        baselines: List[Tuple[str, Set[str]]] = []
        for rank_date, _cube_count in date_rows:
            rows = connection.execute(
                f"SELECT DISTINCT cube_symbol FROM {table} WHERE rank_type = ? AND rank_date = ?",
                [RANK_CACHE_TYPE, rank_date],
            ).fetchall()
            symbols = {
                str(row[0]).strip().upper()
                for row in rows
                if row and is_valid_xueqiu_cube_symbol(str(row[0]).strip().upper())
            }
            if symbols:
                date_label = rank_date.isoformat() if hasattr(rank_date, "isoformat") else str(rank_date)
                baselines.append((f"rank_history:{date_label}", symbols))
        return baselines
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to load recent Xueqiu rank history for rank drift guard: %s", exc)
        return []
    finally:
        if connection is not None:
            connection.close()


def load_recent_xueqiu_snapshot_cube_sets(
    *,
    limit: int = RANK_CACHE_DRIFT_RECENT_SNAPSHOT_COUNT,
    exclude_date: Optional[date] = None,
) -> List[Tuple[str, Set[str]]]:
    if limit <= 0:
        return []
    connection = None
    try:
        connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
        if not _duckdb_table_exists(connection, XUEQIU_CUBE_HOLDINGS_SNAPSHOT_TABLE):
            return []

        table = _quote_duckdb_identifier(XUEQIU_CUBE_HOLDINGS_SNAPSHOT_TABLE)
        params: List[Any] = []
        where_clause = ""
        if exclude_date is not None:
            where_clause = "WHERE snapshot_date <> ?"
            params.append(exclude_date)
        date_rows = connection.execute(
            (
                f"SELECT snapshot_date, COUNT(DISTINCT cube_symbol) AS cube_count "
                f"FROM {table} "
                f"{where_clause} "
                f"GROUP BY snapshot_date "
                f"ORDER BY snapshot_date DESC "
                f"LIMIT ?"
            ),
            [*params, limit],
        ).fetchall()

        baselines: List[Tuple[str, Set[str]]] = []
        for snapshot_date, _cube_count in date_rows:
            rows = connection.execute(
                f"SELECT DISTINCT cube_symbol FROM {table} WHERE snapshot_date = ?",
                [snapshot_date],
            ).fetchall()
            symbols = {
                str(row[0]).strip().upper()
                for row in rows
                if row and is_valid_xueqiu_cube_symbol(str(row[0]).strip().upper())
            }
            if symbols:
                date_label = snapshot_date.isoformat() if hasattr(snapshot_date, "isoformat") else str(snapshot_date)
                baselines.append((f"snapshot:{date_label}", symbols))
        return baselines
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to load recent Xueqiu holdings snapshots for rank drift guard: %s", exc)
        return []
    finally:
        if connection is not None:
            connection.close()


def load_xueqiu_rank_drift_baselines(
    *,
    limit: int = RANK_TARGET_COUNT,
) -> List[Tuple[str, Set[str]]]:
    today = datetime.now(CHINA_TZ).date()
    baselines: List[Tuple[str, Set[str]]] = []
    baselines.extend(
        (label, symbols)
        for label, symbols in load_recent_xueqiu_rank_history_cube_sets(
            limit=RANK_CACHE_DRIFT_RECENT_SNAPSHOT_COUNT,
            exclude_date=today,
        )
        if len(symbols) >= RANK_CACHE_MIN_VALID_LIMIT
    )
    baselines.extend(
        (label, symbols)
        for label, symbols in load_recent_xueqiu_snapshot_cube_sets(
            limit=RANK_CACHE_DRIFT_RECENT_SNAPSHOT_COUNT,
            exclude_date=today,
        )
        if len(symbols) >= RANK_CACHE_MIN_VALID_LIMIT
    )

    cached, cached_at = load_cached_year_top_cubes(
        limit=limit,
        max_age_days=RANK_CACHE_DRIFT_BASELINE_CACHE_MAX_AGE_DAYS,
    )
    if cached:
        label = "rank_cache"
        if cached_at is not None:
            label = f"rank_cache:{cached_at.strftime('%Y-%m-%d %H:%M:%S')}"
        symbols = cube_symbol_set(cached)
        if len(symbols) >= RANK_CACHE_MIN_VALID_LIMIT:
            baselines.append((label, symbols))
    return baselines


def validate_xueqiu_rank_cache_drift(
    cubes: Iterable[CubeInfo],
    baseline_symbol_sets: Iterable[Tuple[str, Set[str]]],
    *,
    min_overlap_ratio: float = RANK_CACHE_DRIFT_MIN_OVERLAP_RATIO,
    min_symbol_count: int = RANK_CACHE_MIN_VALID_LIMIT,
) -> Dict[str, Any]:
    new_symbols = cube_symbol_set(cubes)
    candidates: List[Dict[str, Any]] = []
    for label, baseline_symbols in baseline_symbol_sets:
        normalized_baseline = {
            str(symbol).strip().upper()
            for symbol in baseline_symbols
            if is_valid_xueqiu_cube_symbol(str(symbol).strip().upper())
        }
        if len(new_symbols) < min_symbol_count or len(normalized_baseline) < min_symbol_count:
            continue
        overlap_count = len(new_symbols & normalized_baseline)
        denominator = min(len(new_symbols), len(normalized_baseline))
        overlap_ratio = overlap_count / denominator if denominator else 0.0
        candidates.append(
            {
                "label": label,
                "new_count": len(new_symbols),
                "baseline_count": len(normalized_baseline),
                "overlap_count": overlap_count,
                "overlap_ratio": overlap_ratio,
            }
        )

    if not candidates:
        logger.warning(
            "Skipped Xueqiu year-rank drift guard because no usable baseline exists: new_count=%s",
            len(new_symbols),
        )
        return {
            "checked": False,
            "new_count": len(new_symbols),
            "baseline_count": 0,
        }

    best = max(candidates, key=lambda item: (item["overlap_ratio"], item["overlap_count"]))
    if best["overlap_ratio"] < min_overlap_ratio:
        candidate_summary = ", ".join(
            (
                f"{item['label']}={item['overlap_count']}/"
                f"{min(item['new_count'], item['baseline_count'])}"
                f"({item['overlap_ratio']:.1%})"
            )
            for item in sorted(candidates, key=lambda item: item["overlap_ratio"], reverse=True)[:5]
        )
        raise RuntimeError(
            "Xueqiu year rank refresh drift too large: "
            f"best={best['label']} overlap={best['overlap_count']}/"
            f"{min(best['new_count'], best['baseline_count'])}"
            f"({best['overlap_ratio']:.1%}) "
            f"threshold={min_overlap_ratio:.0%} baselines={candidate_summary}"
        )

    logger.info(
        "Validated Xueqiu year-rank drift: best=%s overlap=%s/%s ratio=%.1f%% baselines=%s",
        best["label"],
        best["overlap_count"],
        min(best["new_count"], best["baseline_count"]),
        best["overlap_ratio"] * 100,
        len(candidates),
    )
    return {
        "checked": True,
        "new_count": len(new_symbols),
        "baseline_count": len(candidates),
        "best_label": best["label"],
        "best_overlap_count": best["overlap_count"],
        "best_overlap_ratio": best["overlap_ratio"],
    }


def save_validated_year_top_cubes(
    cubes: List[CubeInfo],
    fetched_at: datetime,
    *,
    min_overlap_ratio: float = RANK_CACHE_DRIFT_MIN_OVERLAP_RATIO,
) -> Dict[str, Any]:
    baselines = load_xueqiu_rank_drift_baselines(limit=len(cubes) or RANK_TARGET_COUNT)
    drift_summary = validate_xueqiu_rank_cache_drift(
        cubes,
        baselines,
        min_overlap_ratio=min_overlap_ratio,
    )
    save_xueqiu_cube_rank_history_to_duckdb(cubes, fetched_at)
    save_year_top_cubes(cubes, fetched_at)
    return drift_summary


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


def _activity_from_cache_row(row: XueqiuCubeActivityCache) -> CubeActivityResult:
    return CubeActivityResult(
        symbol=row.symbol,
        latest_rebalance_at=as_china_datetime(row.latest_rebalance_at),
        latest_rebalance_id=row.latest_rebalance_id,
        latest_rebalance_status=row.latest_rebalance_status or "",
        latest_rebalance_category=row.latest_rebalance_category or "",
        source=row.activity_type or ACTIVE_REBALANCE_ACTIVITY_TYPE,
        pages_fetched=row.pages_fetched or 0,
        page_limit_hit=bool(row.page_limit_hit),
        cache_hit=True,
        checked_at=as_china_datetime(row.checked_at),
        raw_event=(row.raw_data or {}).get("latest_event") if isinstance(row.raw_data, dict) else None,
    )


def load_cached_cube_activity(
    cubes: List[CubeInfo],
    *,
    activity_type: str = ACTIVE_REBALANCE_ACTIVITY_TYPE,
    min_checked_at: Optional[datetime] = None,
) -> Dict[str, CubeActivityResult]:
    symbols = sorted({cube.symbol for cube in cubes if cube.symbol})
    if not symbols:
        return {}
    db = SessionLocal()
    try:
        query = db.query(XueqiuCubeActivityCache).filter(
            XueqiuCubeActivityCache.activity_type == activity_type,
            XueqiuCubeActivityCache.symbol.in_(symbols),
        )
        if min_checked_at is not None:
            query = query.filter(XueqiuCubeActivityCache.checked_at >= min_checked_at)
        rows = query.all()
        return {row.symbol: _activity_from_cache_row(row) for row in rows}
    finally:
        db.close()


def save_cube_activity_cache(
    activity_results: Iterable[CubeActivityResult],
    *,
    activity_type: str = ACTIVE_REBALANCE_ACTIVITY_TYPE,
) -> int:
    results = [
        result
        for result in activity_results
        if result.symbol and not result.cache_hit and not result.error
    ]
    if not results:
        return 0
    now = activity_cache_checked_at()
    db = SessionLocal()
    saved_count = 0
    try:
        for result in results:
            row = (
                db.query(XueqiuCubeActivityCache)
                .filter(
                    XueqiuCubeActivityCache.activity_type == activity_type,
                    XueqiuCubeActivityCache.symbol == result.symbol,
                )
                .first()
            )
            if row is None:
                row = XueqiuCubeActivityCache(
                    activity_type=activity_type,
                    symbol=result.symbol,
                    created_at=now,
                )
                db.add(row)
            row.latest_rebalance_at = _to_naive_china_datetime(result.latest_rebalance_at)
            row.latest_rebalance_id = result.latest_rebalance_id
            row.latest_rebalance_status = result.latest_rebalance_status
            row.latest_rebalance_category = result.latest_rebalance_category
            row.pages_fetched = result.pages_fetched
            row.page_limit_hit = bool(result.page_limit_hit)
            row.raw_data = {
                "latest_event": result.raw_event,
                "source": result.source,
            }
            row.checked_at = _to_naive_china_datetime(result.checked_at) or now
            row.updated_at = now
            saved_count += 1
        db.commit()
        return saved_count
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
    min_overlap_ratio: float = RANK_CACHE_DRIFT_MIN_OVERLAP_RATIO,
    drift_summary_out: Optional[Dict[str, Any]] = None,
) -> Tuple[List[CubeInfo], datetime, bool]:
    if not force_refresh:
        cached, fetched_at = load_cached_year_top_cubes(limit=limit)
        if cached and fetched_at:
            if drift_summary_out is not None:
                drift_summary_out.clear()
                drift_summary_out.update({"checked": False, "source": "cache"})
            return cached, fetched_at, False

    fetched_at = datetime.now()
    cubes = await fetch_year_top_cubes(cookie=cookie, target_count=limit, timeout=timeout)
    drift_summary = save_validated_year_top_cubes(
        cubes,
        fetched_at,
        min_overlap_ratio=min_overlap_ratio,
    )
    if drift_summary_out is not None:
        drift_summary_out.clear()
        drift_summary_out.update(drift_summary)
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


def extract_cube_show_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = payload.get("data")
    if isinstance(data, dict):
        cube = data.get("cube")
        if isinstance(cube, dict):
            return cube
        return data
    cube = payload.get("cube")
    if isinstance(cube, dict):
        return cube
    return payload


def extract_last_user_rebalance_id(payload: Dict[str, Any]) -> Optional[int]:
    cube_payload = extract_cube_show_payload(payload)
    for key in ("last_user_rb_gid", "lastUserRbGid", "last_user_rb_id", "lastUserRbId"):
        value = safe_int(cube_payload.get(key))
        if value is not None:
            return value
    return None


def extract_show_origin_rebalance_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = payload.get("data")
    if isinstance(data, dict):
        rebalancing = data.get("rebalancing")
        if isinstance(rebalancing, dict):
            return rebalancing
        return data
    rebalancing = payload.get("rebalancing")
    if isinstance(rebalancing, dict):
        return rebalancing
    return payload


def manager_rebalance_from_show_origin(
    symbol: str,
    *,
    rb_id: Optional[int],
    origin_payload: Optional[Dict[str, Any]],
    checked_at: Optional[datetime] = None,
) -> CubeActivityResult:
    if rb_id is None:
        return CubeActivityResult(
            symbol=symbol,
            checked_at=checked_at,
            raw_event=None,
        )
    if not isinstance(origin_payload, dict):
        return CubeActivityResult(
            symbol=symbol,
            latest_rebalance_id=rb_id,
            checked_at=checked_at,
            error=f"Unexpected show_origin payload for {symbol}: {origin_payload}",
        )

    event = extract_show_origin_rebalance_payload(origin_payload)
    event_at = _rebalance_event_time(event)
    category = str(event.get("category") or "")
    status = str(event.get("status") or "")
    if not event_at or category != "user_rebalancing" or status != "success":
        return CubeActivityResult(
            symbol=symbol,
            latest_rebalance_id=safe_int(event.get("id")) or rb_id,
            latest_rebalance_status=status,
            latest_rebalance_category=category,
            checked_at=checked_at,
            raw_event=event,
        )
    return CubeActivityResult(
        symbol=symbol,
        latest_rebalance_at=event_at,
        latest_rebalance_id=safe_int(event.get("id")) or rb_id,
        latest_rebalance_status=status,
        latest_rebalance_category=category,
        checked_at=checked_at,
        raw_event=event,
    )


async def fetch_cube_manager_activity(
    client: httpx.AsyncClient,
    cube: CubeInfo,
    *,
    retries: int,
    previous_activity: Optional[CubeActivityResult] = None,
    request_pacer: Optional[AsyncRequestPacer] = None,
) -> CubeActivityResult:
    last_error: Optional[BaseException] = None
    checked_at = datetime.now(CHINA_TZ)

    for attempt in range(1, retries + 1):
        try:
            if request_pacer is not None:
                await request_pacer.wait()
            show_response = await client.get(
                f"{XUEQIU_API_BASE_URL}/cubes/show.json",
                params={"symbol": cube.symbol},
            )
            if show_response.status_code >= 400:
                error_code = ""
                try:
                    error_payload = show_response.json()
                    error_code = str(error_payload.get("error_code") or "")
                except ValueError:
                    pass
                error = RuntimeError(f"HTTP {show_response.status_code}: {show_response.text[:300]}")
                setattr(error, "xueqiu_status_code", show_response.status_code)
                setattr(error, "xueqiu_error_code", error_code)
                raise error
            show_payload = show_response.json()
            if not isinstance(show_payload, dict):
                raise ValueError(f"Unexpected cube show payload for {cube.symbol}: {show_payload}")
            rb_id = extract_last_user_rebalance_id(show_payload)
            if rb_id is None:
                return manager_rebalance_from_show_origin(
                    cube.symbol,
                    rb_id=None,
                    origin_payload=None,
                    checked_at=checked_at,
                )
            if previous_activity and previous_activity.latest_rebalance_id == rb_id and not previous_activity.error:
                return replace(
                    previous_activity,
                    checked_at=checked_at,
                    cache_hit=False,
                    pages_fetched=1,
                    page_limit_hit=False,
                    source=ACTIVE_REBALANCE_ACTIVITY_TYPE,
                )

            if request_pacer is not None:
                await request_pacer.wait()
            origin_response = await client.get(
                f"{XUEQIU_API_BASE_URL}/cubes/rebalancing/show_origin.json",
                params={"rb_id": rb_id},
            )
            if origin_response.status_code >= 400:
                error_code = ""
                try:
                    error_payload = origin_response.json()
                    error_code = str(error_payload.get("error_code") or "")
                except ValueError:
                    pass
                error = RuntimeError(f"HTTP {origin_response.status_code}: {origin_response.text[:300]}")
                setattr(error, "xueqiu_status_code", origin_response.status_code)
                setattr(error, "xueqiu_error_code", error_code)
                raise error
            origin_payload = origin_response.json()
            if not isinstance(origin_payload, dict):
                raise ValueError(f"Unexpected show_origin payload for {cube.symbol}: {origin_payload}")
            return manager_rebalance_from_show_origin(
                cube.symbol,
                rb_id=rb_id,
                origin_payload=origin_payload,
                checked_at=checked_at,
            )
        except BaseException as exc:  # noqa: BLE001
            last_error = exc
            if attempt < retries:
                retry_delay = min(10.0, 0.8 * attempt)
                status_code = safe_int(getattr(exc, "xueqiu_status_code", None))
                if (
                    status_code in XUEQIU_ACTIVITY_THROTTLE_STATUS_CODES
                    or getattr(exc, "xueqiu_error_code", "") in {"10026", "400016"}
                ):
                    retry_delay = min(45.0, XUEQIU_ACTIVITY_HTTP_ERROR_COOLDOWN_SECONDS * attempt)
                    if request_pacer is not None:
                        await request_pacer.cooldown(retry_delay)
                await asyncio.sleep(retry_delay)
    return CubeActivityResult(
        symbol=cube.symbol,
        checked_at=checked_at,
        error=repr(last_error),
    )


async def fetch_cube_current(
    client: httpx.AsyncClient,
    cube: CubeInfo,
    *,
    active_since: Optional[datetime],
    semaphore: asyncio.Semaphore,
    retries: int,
    cached_activity: Optional[CubeActivityResult] = None,
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
                activity = cached_activity
                active_rebalance_at = activity.latest_rebalance_at if activity else None
                activity_error = activity.error if activity else None
                return CubeCurrentResult(
                    cube=cube,
                    holdings=holdings,
                    latest_rebalance_at=latest_at,
                    latest_rebalance_id=latest_id,
                    latest_rebalance_status=latest_status,
                    active_rebalance_at=active_rebalance_at,
                    active_rebalance_id=activity.latest_rebalance_id if activity else None,
                    active_rebalance_status=activity.latest_rebalance_status if activity else "",
                    active_rebalance_category=activity.latest_rebalance_category if activity else "",
                    active_rebalance_source=ACTIVE_REBALANCE_ACTIVITY_TYPE,
                    activity_cache_hit=bool(activity and activity.cache_hit),
                    activity_pages_fetched=activity.pages_fetched if activity else 0,
                    activity_page_limit_hit=bool(activity and activity.page_limit_hit),
                    activity_error=activity_error,
                    holdings_source=holdings_source,
                    active=bool(
                        active_since is not None
                        and active_rebalance_at
                        and active_rebalance_at >= active_since
                        and not activity_error
                    ),
                )
            except BaseException as exc:  # noqa: BLE001
                last_error = exc
                if attempt < retries:
                    retry_delay = min(10.0, 0.8 * attempt)
                    if getattr(exc, "xueqiu_error_code", "") in {"10026", "400016"}:
                        retry_delay = min(45.0, 5.0 * attempt)
                    await asyncio.sleep(retry_delay)
        return CubeCurrentResult(cube=cube, holdings=[], error=repr(last_error))


def apply_activity_to_current_result(
    result: CubeCurrentResult,
    activity: CubeActivityResult,
    *,
    active_since: datetime,
) -> CubeCurrentResult:
    active_rebalance_at = activity.latest_rebalance_at
    return replace(
        result,
        active_rebalance_at=active_rebalance_at,
        active_rebalance_id=activity.latest_rebalance_id,
        active_rebalance_status=activity.latest_rebalance_status,
        active_rebalance_category=activity.latest_rebalance_category,
        active_rebalance_source=activity.source or ACTIVE_REBALANCE_ACTIVITY_TYPE,
        activity_cache_hit=activity.cache_hit,
        activity_pages_fetched=activity.pages_fetched,
        activity_page_limit_hit=activity.page_limit_hit,
        activity_error=activity.error,
        active=bool(
            active_rebalance_at
            and active_rebalance_at >= active_since
            and not activity.error
        ),
    )


async def fetch_all_cube_current(
    cubes: List[CubeInfo],
    *,
    cookie: str,
    workers: int,
    timeout: float,
    retries: int,
    active_since: Optional[datetime],
    refresh_activity_cache: bool = True,
) -> List[CubeCurrentResult]:
    cached_activity = (
        load_cached_cube_activity(
            cubes,
            min_checked_at=activity_cache_checked_after() if refresh_activity_cache else None,
        )
        if active_since is not None
        else {}
    )
    previous_activity = (
        load_cached_cube_activity(cubes)
        if active_since is not None and refresh_activity_cache
        else dict(cached_activity)
    )
    if cached_activity:
        logger.info(
            "Loaded Xueqiu cube manager activity cache: cached=%s source=%s ttl_hours=%s refresh_missing=%s",
            len(cached_activity),
            ACTIVE_REBALANCE_ACTIVITY_TYPE,
            ACTIVE_REBALANCE_CACHE_TTL_HOURS if refresh_activity_cache else "disabled",
            refresh_activity_cache,
        )
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
                cached_activity=cached_activity.get(cube.symbol),
            )
            for cube in cubes
        ]
        results: List[CubeCurrentResult] = []
        for index, task in enumerate(asyncio.as_completed(tasks), start=1):
            result = await task
            results.append(result)
            if index % 100 == 0:
                logger.info("Fetched current snapshots for %s/%s cubes", index, len(cubes))
        if active_since is not None and not refresh_activity_cache:
            for index, result in enumerate(results):
                if result.error or result.activity_cache_hit:
                    continue
                missing_activity = CubeActivityResult(
                    symbol=result.cube.symbol,
                    source=ACTIVE_REBALANCE_ACTIVITY_TYPE,
                    checked_at=datetime.now(CHINA_TZ),
                    error=ACTIVE_REBALANCE_CACHE_MISS_ERROR,
                )
                results[index] = apply_activity_to_current_result(
                    result,
                    missing_activity,
                    active_since=active_since,
                )
        elif active_since is not None:
            activity_workers = max(1, min(current_workers, ACTIVE_REBALANCE_ACTIVITY_REFRESH_WORKERS))
            activity_pacer = AsyncRequestPacer(
                min_interval_seconds=XUEQIU_ACTIVITY_REQUEST_MIN_INTERVAL_SECONDS,
                jitter_seconds=XUEQIU_ACTIVITY_REQUEST_JITTER_SECONDS,
            )
            logger.info(
                "Fetching Xueqiu manager activity: missing=%s cached=%s workers=%s",
                len([result for result in results if not result.error and not result.activity_cache_hit]),
                len([result for result in results if result.activity_cache_hit]),
                activity_workers,
            )
            activity_targets = [
                (index, result)
                for index, result in enumerate(results)
                if not result.error and not result.activity_cache_hit
            ]
            activity_results: List[CubeActivityResult] = []
            if activity_workers == 1:
                for activity_index, (result_index, result) in enumerate(activity_targets, start=1):
                    activity = await fetch_cube_manager_activity(
                        client,
                        result.cube,
                        retries=retries,
                        previous_activity=previous_activity.get(result.cube.symbol),
                        request_pacer=activity_pacer,
                    )
                    results[result_index] = apply_activity_to_current_result(
                        results[result_index],
                        activity,
                        active_since=active_since,
                    )
                    activity_results.append(activity)
                    if activity_index % 100 == 0:
                        logger.info(
                            "Fetched Xueqiu manager activity for %s/%s cubes",
                            activity_index,
                            len(activity_targets),
                        )
            else:
                activity_semaphore = asyncio.Semaphore(activity_workers)

                async def fetch_activity_for_result(index: int, result: CubeCurrentResult) -> Tuple[int, CubeActivityResult]:
                    async with activity_semaphore:
                        activity = await fetch_cube_manager_activity(
                            client,
                            result.cube,
                            retries=retries,
                            previous_activity=previous_activity.get(result.cube.symbol),
                            request_pacer=activity_pacer,
                        )
                        return index, activity

                activity_tasks = [
                    fetch_activity_for_result(index, result)
                    for index, result in activity_targets
                ]
                for activity_index, task in enumerate(asyncio.as_completed(activity_tasks), start=1):
                    result_index, activity = await task
                    results[result_index] = apply_activity_to_current_result(
                        results[result_index],
                        activity,
                        active_since=active_since,
                    )
                    activity_results.append(activity)
                    if activity_index % 100 == 0:
                        logger.info(
                            "Fetched Xueqiu manager activity for %s/%s cubes",
                            activity_index,
                            len(activity_tasks),
                        )
            saved_activity_count = save_cube_activity_cache(activity_results)
            if saved_activity_count:
                logger.info(
                    "Saved Xueqiu cube manager activity cache: saved=%s source=%s",
                    saved_activity_count,
                    ACTIVE_REBALANCE_ACTIVITY_TYPE,
                )
        return results


def xueqiu_fetch_failure_limit(source_count: int) -> int:
    return max(5, math.ceil(max(0, source_count) * ACTIVE_REBALANCE_MAX_FAILED_RATIO))


def ensure_xueqiu_current_fetch_quality(
    current_results: List[CubeCurrentResult],
    *,
    source_count: int,
) -> None:
    failed_results = [result for result in current_results if result.error]
    failure_limit = xueqiu_fetch_failure_limit(source_count)
    if len(failed_results) > failure_limit:
        examples = ", ".join(
            f"{result.cube.symbol}:{result.error}"
            for result in failed_results[:3]
        )
        raise RuntimeError(
            "Xueqiu current holdings fetch failed for too many cubes before saving snapshot: "
            f"failed={len(failed_results)} limit={failure_limit} source={source_count} "
            f"examples={examples}"
        )


def build_active_filter_summary(
    *,
    source_cubes: List[CubeInfo],
    current_results: List[CubeCurrentResult],
    active_since: datetime,
    lookback_days: int,
) -> Dict[str, Any]:
    active_results = [
        result for result in current_results if result.active and not (result.error or result.activity_error)
    ]
    failed_results = [result for result in current_results if result.error or result.activity_error]
    current_failed_results = [result for result in current_results if result.error]
    activity_failed_results = [result for result in current_results if result.activity_error and not result.error]
    inactive_results = [
        result for result in current_results if not result.active and not (result.error or result.activity_error)
    ]
    fallback_results = [result for result in current_results if result.holdings_source == "last_rb"]
    latest_times = [
        result.active_rebalance_at
        for result in current_results
        if result.active_rebalance_at is not None
    ]
    active_latest_times = [
        result.active_rebalance_at
        for result in active_results
        if result.active_rebalance_at is not None
    ]
    return {
        "enabled": True,
        "lookback_days": lookback_days,
        "activity_source": ACTIVE_REBALANCE_ACTIVITY_TYPE,
        "activity_label": ACTIVE_REBALANCE_ACTIVITY_LABEL,
        "active_since": active_since.isoformat(),
        "source_cube_count": len(source_cubes),
        "active_cube_count": len(active_results),
        "inactive_cube_count": len(inactive_results),
        "activity_failed_count": len(failed_results),
        "current_snapshot_failed_count": len(current_failed_results),
        "manager_activity_failed_count": len(activity_failed_results),
        "activity_cache_hit_count": len([result for result in current_results if result.activity_cache_hit]),
        "activity_page_limit_hit_count": len([result for result in current_results if result.activity_page_limit_hit]),
        "holdings_fallback_count": len(fallback_results),
        "latest_active_rebalance_at_max": max(latest_times).isoformat() if latest_times else None,
        "latest_active_rebalance_at_min_active": min(active_latest_times).isoformat() if active_latest_times else None,
        "failed_examples": [
            {
                "symbol": result.cube.symbol,
                "cube_name": result.cube.cube_name,
                "error": result.error or result.activity_error,
            }
            for result in failed_results[:10]
        ],
    }


def active_filter_description(active_filter_summary: Optional[Dict[str, Any]]) -> str:
    if not active_filter_summary:
        return ""
    label = active_filter_summary.get("activity_label") or "调仓"
    return f"最近 {active_filter_summary.get('lookback_days')} 天有{label}"


def active_filter_compact_label(active_filter_summary: Optional[Dict[str, Any]]) -> str:
    if not active_filter_summary:
        return ""
    label = active_filter_summary.get("activity_label") or "调仓"
    return f"{label}活跃{active_filter_summary.get('lookback_days')}天"


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


def ensure_xueqiu_cube_rank_history_schema(connection) -> None:
    table = _quote_duckdb_identifier(XUEQIU_CUBE_RANK_HISTORY_TABLE)
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {table} (
            rank_date DATE NOT NULL,
            fetched_at TIMESTAMP NOT NULL,
            rank_type VARCHAR NOT NULL,
            year_rank INTEGER NOT NULL,
            cube_symbol VARCHAR NOT NULL,
            cube_id BIGINT,
            cube_name VARCHAR,
            screen_name VARCHAR,
            daily_gain DOUBLE,
            week_gain DOUBLE,
            year_gain DOUBLE,
            recommend_count BIGINT,
            net_value DOUBLE,
            raw_cube_json VARCHAR,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            PRIMARY KEY (rank_date, rank_type, cube_symbol)
        )
        """
    )
    connection.execute(
        f"CREATE INDEX IF NOT EXISTS idx_xueqiu_cube_rank_history_date "
        f"ON {table}(rank_type, rank_date, year_rank)"
    )
    connection.execute(
        f"CREATE INDEX IF NOT EXISTS idx_xueqiu_cube_rank_history_symbol "
        f"ON {table}(cube_symbol, rank_date)"
    )


def build_xueqiu_cube_rank_history_rows(
    *,
    cubes: List[CubeInfo],
    fetched_at: datetime,
) -> Tuple[List[Dict[str, Any]], date]:
    normalized_cubes = normalize_ranked_cubes(cubes)
    rank_at = _to_naive_china_datetime(fetched_at) or datetime.now(CHINA_TZ).replace(tzinfo=None)
    rank_date = rank_at.date()
    saved_at = datetime.now(CHINA_TZ).replace(tzinfo=None)
    rows: List[Dict[str, Any]] = []
    for cube in normalized_cubes:
        rows.append(
            {
                "rank_date": rank_date,
                "fetched_at": rank_at,
                "rank_type": RANK_CACHE_TYPE,
                "year_rank": cube.year_rank or 0,
                "cube_symbol": cube.symbol,
                "cube_id": cube.cube_id,
                "cube_name": cube.cube_name,
                "screen_name": cube.screen_name,
                "daily_gain": cube.daily_gain,
                "week_gain": cube.week_gain,
                "year_gain": cube.year_gain,
                "recommend_count": cube.recommend_count,
                "net_value": cube.net_value,
                "raw_cube_json": json.dumps(
                    cube.raw_data or {},
                    ensure_ascii=False,
                    default=str,
                    separators=(",", ":"),
                ),
                "created_at": saved_at,
                "updated_at": saved_at,
            }
        )
    return rows, rank_date


def save_xueqiu_cube_rank_history_to_duckdb(
    cubes: List[CubeInfo],
    fetched_at: datetime,
) -> Dict[str, Any]:
    rows, rank_date = build_xueqiu_cube_rank_history_rows(
        cubes=cubes,
        fetched_at=fetched_at,
    )
    connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=False)
    table = _quote_duckdb_identifier(XUEQIU_CUBE_RANK_HISTORY_TABLE)
    temp_rows_name = "xueqiu_cube_rank_history_rows"
    try:
        ensure_xueqiu_cube_rank_history_schema(connection)
        connection.execute(
            f"DELETE FROM {table} WHERE rank_date = ? AND rank_type = ?",
            [rank_date, RANK_CACHE_TYPE],
        )
        if rows:
            frame = pd.DataFrame(rows).loc[:, XUEQIU_CUBE_RANK_HISTORY_COLUMNS]
            connection.register(temp_rows_name, frame)
            quoted_columns = ", ".join(
                _quote_duckdb_identifier(column)
                for column in XUEQIU_CUBE_RANK_HISTORY_COLUMNS
            )
            connection.execute(
                (
                    f"INSERT INTO {table} ({quoted_columns}) "
                    f"SELECT {quoted_columns} FROM {_quote_duckdb_identifier(temp_rows_name)}"
                )
            )
        logger.info(
            "Saved Xueqiu cube rank history to DuckDB: table=%s date=%s rows=%s database=%s",
            XUEQIU_CUBE_RANK_HISTORY_TABLE,
            rank_date.isoformat(),
            len(rows),
            ANALYTICS_DB_PATH,
        )
        return {
            "table": XUEQIU_CUBE_RANK_HISTORY_TABLE,
            "rank_date": rank_date.isoformat(),
            "saved_rows": len(rows),
            "database": ANALYTICS_DB_PATH,
        }
    finally:
        connection.close()


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
            active_rebalance_at TIMESTAMP,
            active_rebalance_id BIGINT,
            active_rebalance_status VARCHAR,
            active_rebalance_category VARCHAR,
            active_rebalance_source VARCHAR,
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
    existing_columns = {
        row[1]
        for row in connection.execute(
            f"PRAGMA table_info({_quote_duckdb_identifier(XUEQIU_CUBE_HOLDINGS_SNAPSHOT_TABLE)})"
        ).fetchall()
    }
    column_ddls = {
        "active_rebalance_at": "TIMESTAMP",
        "active_rebalance_id": "BIGINT",
        "active_rebalance_status": "VARCHAR",
        "active_rebalance_category": "VARCHAR",
        "active_rebalance_source": "VARCHAR",
    }
    for column_name, column_type in column_ddls.items():
        if column_name not in existing_columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column_name} {column_type}")
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
        active_rebalance_at = _to_naive_china_datetime(result.active_rebalance_at)
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
                    "active_rebalance_at": active_rebalance_at,
                    "active_rebalance_id": result.active_rebalance_id,
                    "active_rebalance_status": result.active_rebalance_status,
                    "active_rebalance_category": result.active_rebalance_category,
                    "active_rebalance_source": result.active_rebalance_source,
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


def load_latest_saved_cube_holdings_snapshot(
    *,
    before_date: date,
    active_only: bool = True,
) -> Tuple[date, List[CubeInfo], List[CubeCurrentResult]]:
    """Load the latest completed holdings snapshot before an execution day.

    The evening cache job owns snapshot collection.  The next trading day's
    rebalance must consume that frozen snapshot instead of fetching manager
    holdings again, otherwise weights and end-of-day prices have different
    timestamps.
    """
    connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
    try:
        row = connection.execute(
            f"""
            SELECT MAX(snapshot_date)
            FROM {XUEQIU_CUBE_HOLDINGS_SNAPSHOT_TABLE}
            WHERE snapshot_date < ?
              {"AND COALESCE(is_active, FALSE)" if active_only else ""}
            """,
            [before_date],
        ).fetchone()
        snapshot_date = row[0] if row else None
        if not snapshot_date:
            raise RuntimeError(f"No saved Xueqiu holdings snapshot before {before_date.isoformat()}")
        price_row = connection.execute(
            "SELECT MAX(trade_date) FROM a_stock_market_daily_qfq WHERE trade_date < ?",
            [before_date],
        ).fetchone()
        expected_signal_date = price_row[0] if price_row else None
        if expected_signal_date is None or snapshot_date != expected_signal_date:
            raise RuntimeError(
                "Frozen Xueqiu holdings snapshot is stale or has no matching A-share close: "
                f"snapshot_date={snapshot_date} expected_signal_date={expected_signal_date}"
            )
        rows = connection.execute(
            f"""
            SELECT year_rank, cube_symbol, cube_id, cube_name, screen_name,
                   latest_rebalance_at, latest_rebalance_id, latest_rebalance_status,
                   active_rebalance_at, active_rebalance_id, active_rebalance_status,
                   active_rebalance_category, active_rebalance_source, holdings_source,
                   COALESCE(is_active, FALSE), raw_holding_json, stock_symbol,
                   stock_name, stock_id, segment_name, weight_pct
            FROM {XUEQIU_CUBE_HOLDINGS_SNAPSHOT_TABLE}
            WHERE snapshot_date = ?
              {"AND COALESCE(is_active, FALSE)" if active_only else ""}
            ORDER BY year_rank, cube_symbol, stock_symbol
            """,
            [snapshot_date],
        ).fetchall()
    finally:
        connection.close()

    grouped: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        cube_symbol = str(row[1])
        entry = grouped.setdefault(
            cube_symbol,
            {"meta": row, "holdings": []},
        )
        try:
            holding = json.loads(row[15]) if row[15] else {}
        except (TypeError, ValueError):
            holding = {}
        holding.update({
            "symbol": row[16],
            "name": row[17],
            "stock_id": row[18],
            "segment_name": row[19],
            "weight": row[20],
        })
        entry["holdings"].append(holding)

    cubes: List[CubeInfo] = []
    results: List[CubeCurrentResult] = []
    for entry in grouped.values():
        row = entry["meta"]
        cube = CubeInfo(
            year_rank=safe_int(row[0]), symbol=str(row[1]), cube_id=safe_int(row[2]),
            cube_name=str(row[3] or ""), screen_name=str(row[4] or ""),
        )
        cubes.append(cube)
        results.append(CubeCurrentResult(
            cube=cube,
            holdings=entry["holdings"],
            latest_rebalance_at=row[5], latest_rebalance_id=safe_int(row[6]),
            latest_rebalance_status=str(row[7] or ""),
            active_rebalance_at=row[8], active_rebalance_id=safe_int(row[9]),
            active_rebalance_status=str(row[10] or ""),
            active_rebalance_category=str(row[11] or ""),
            active_rebalance_source=str(row[12] or ""),
            holdings_source=str(row[13] or "snapshot"), active=bool(row[14]),
        ))
    if not results:
        raise RuntimeError(f"Saved Xueqiu snapshot {snapshot_date} contains no usable holdings")
    return snapshot_date, cubes, results


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
    stock_segments: Dict[str, str] = {}
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
            segment_name = str(
                holding.get("segment_name")
                or holding.get("segmentName")
                or ""
            ).strip()
            holding_rows.append(
                {
                    "cube_symbol": result.cube.symbol,
                    "cube_name": result.cube.cube_name,
                    "year_rank": result.cube.year_rank,
                    "stock_symbol": symbol,
                    "stock_name": name,
                    "segment_name": segment_name,
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
            if segment_name and symbol not in stock_segments:
                stock_segments[symbol] = segment_name
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
                "segment_name": CASH_NAME if is_cash else stock_segments.get(symbol, ""),
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


def report_display_count(top_n: int, sell_rank: Optional[int] = None) -> int:
    return max(top_n, sell_rank or REPORT_TABLE_DISPLAY_RANK)


def report_item_symbol_key(item: Dict[str, Any]) -> str:
    symbol = item.get("stock_symbol")
    if item.get("is_cash") or is_cash_symbol(symbol):
        return CASH_SYMBOL
    normalized = normalize_xueqiu_symbol(symbol)
    return normalized or str(symbol or "").strip().upper()


def merge_report_table_item(
    ranking_item: Dict[str, Any],
    target_items_by_symbol: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    merged = dict(ranking_item)
    target_item = target_items_by_symbol.get(report_item_symbol_key(ranking_item))
    if not target_item:
        return merged
    for key in (
        "strategy_rank",
        "top_normalized_weight_pct",
        "rebalance_weight_pct",
        "strategy_action",
        "current_weight_pct",
        "rebalance_skip_reason",
        "rebalance_quote_type",
        "stock_id",
        "segment_name",
    ):
        if key in target_item:
            merged[key] = target_item.get(key)
    if target_item.get("stock_name"):
        merged["stock_name"] = target_item.get("stock_name")
    return merged


def build_report_table_items(
    *,
    top_items: List[Dict[str, Any]],
    aggregate: Dict[str, Any],
    top_n: int,
    sell_rank: Optional[int] = None,
) -> List[Dict[str, Any]]:
    target_items_by_symbol = {
        report_item_symbol_key(item): item
        for item in top_items
        if report_item_symbol_key(item)
    }
    seen_symbols: Set[str] = set()
    display_items: List[Dict[str, Any]] = []
    for ranking_item in (aggregate.get("ranking") or [])[:report_display_count(top_n, sell_rank)]:
        symbol_key = report_item_symbol_key(ranking_item)
        if symbol_key and symbol_key in seen_symbols:
            continue
        if symbol_key:
            seen_symbols.add(symbol_key)
        display_items.append(merge_report_table_item(ranking_item, target_items_by_symbol))

    if any(item.get("is_cash") or is_cash_symbol(item.get("stock_symbol")) for item in display_items):
        return display_items

    cash_item = get_cash_ranking_item(aggregate)
    if cash_item:
        display_items.append(merge_report_table_item(cash_item, target_items_by_symbol))
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
    source_weights = [_rebalance_source_weight(item) for item in selected]
    rounded_weights = [round(weight, 2) for weight in source_weights]
    source_total = sum(source_weights)
    if source_total >= 99.995:
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
    target_total_weight_pct: float = 100.0,
) -> Dict[str, float]:
    """Build submission weights that avoid touching retained holdings unnecessarily."""
    if not final_symbols:
        return {}

    added_set = set(added_symbols)
    retained_symbols = [symbol for symbol in final_symbols if symbol not in added_set]
    equal_weight = target_total_weight_pct / len(final_symbols)

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

    remaining_weight = target_total_weight_pct - retained_sum
    if added_symbols:
        if remaining_weight <= 0:
            retained_target_sum = max(0.0, target_total_weight_pct - equal_weight * len(added_symbols))
            scale = retained_target_sum / retained_sum if retained_sum > 0 else 0.0
            for symbol in retained_symbols:
                weights[symbol] = weights[symbol] * scale
            remaining_weight = target_total_weight_pct - retained_target_sum
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
    if total > 0 and abs(total - target_total_weight_pct) > 1e-9:
        scale = target_total_weight_pct / total
        weights = {symbol: weight * scale for symbol, weight in weights.items()}
    return weights


def build_equal_top10_top12_buffer_plan(
    *,
    ranking: List[Dict[str, Any]],
    current_holdings: List[Dict[str, Any]],
    top_n: int = BUFFER_STRATEGY_TOP_N,
    sell_rank: int = BUFFER_STRATEGY_SELL_RANK,
    target_total_weight_pct: float = 100.0,
    min_holding_cubes: Optional[int] = None,
) -> Dict[str, Any]:
    candidates = _ranked_rebalance_candidates(ranking)
    if min_holding_cubes:
        filtered_candidates: List[Dict[str, Any]] = []
        for candidate in candidates:
            cube_count = safe_int(candidate.get("holding_cube_count"))
            if cube_count is None or cube_count >= int(min_holding_cubes):
                filtered_candidates.append(candidate)
        candidates = filtered_candidates
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
    equal_weight = target_total_weight_pct / len(final_symbols) if final_symbols else 0.0
    execution_weights = build_min_turnover_execution_weights(
        final_symbols=final_symbols,
        added_symbols=added_symbols,
        current_by_symbol=current_by_symbol,
        target_total_weight_pct=target_total_weight_pct,
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
        "strategy_name": (
            f"Top{top_n}等权 + 跌出Top{sell_rank}才卖 + "
            f"从Top{top_n}补位 + 成分变化才调仓"
        ),
        "top_n": top_n,
        "sell_rank": sell_rank,
        "target_total_weight_pct": target_total_weight_pct,
        "target_cash_weight_pct": 100.0 - target_total_weight_pct,
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


def load_latest_csi_all_share_fear_greed() -> Optional[Dict[str, Any]]:
    """Read a plain snapshot so no ORM object/session crosses external I/O.

    Includes the proxy-ETF log-volume z-score (放量/缩量确认) computed by the
    shared 自算贪恐 summary service for 中证全指, plus the most recent 7 daily
    scores (chronological) for the MA5 moving-average signal.
    """
    with SessionLocal() as db:
        rows = (
            db.query(ETFFearGreedCloneHistory)
            .filter(ETFFearGreedCloneHistory.symbol == CSI_ALL_SHARE_FEAR_GREED_SYMBOL)
            .order_by(ETFFearGreedCloneHistory.date.desc())
            .limit(7)
            .all()
        )
        if not rows:
            return None
        latest = rows[0]
        base = {
            "symbol": latest.symbol,
            "date": latest.date.isoformat(),
            "score": float(latest.score),
            "rating": latest.rating,
            "recent_scores": [
                {"date": row.date.isoformat(), "score": float(row.score)}
                for row in reversed(rows)
            ],
        }
    try:
        from ..core.services.etf_fear_greed_clone_service import ETFFearGreedCloneCalculator

        calculator = ETFFearGreedCloneCalculator()
        summary = calculator.load_summaries_from_db([CSI_ALL_SHARE_FEAR_GREED_SYMBOL])
        data = (summary.get("data") or [{}])[0]
        base["volume_ratio_20d"] = data.get("volume_ratio_20d")
        base["log_volume_z"] = data.get("log_volume_z")
        base["is_stale"] = data.get("is_stale")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to load CSI All Share log-volume z-score: %s", exc)
        base["volume_ratio_20d"] = None
        base["log_volume_z"] = None
        base["is_stale"] = None
    return base


XUEQIU_STRATEGY_CONFIG_DEFAULTS = {
    "buffer": {
        "strategy_key": "buffer",
        "label": "星澜壹号 · 综合权重",
        "fear_target_count": FEAR_GREED_FEAR_TARGET_COUNT,
        "greed_target_count": FEAR_GREED_GREED_TARGET_COUNT,
        "min_holding_cubes": 8,
        "buy_confirm_prior_days": RANK_ACCELERATION_BUY_CONFIRM_MIN_DAYS - 1,
        "current_rank_limit": 100,
        "holding_cube_increase": 2,
        "metric_threshold": WEIGHT_PRICE_RATIO_MIN_RATIO,
        "new_entry_rank_limit": WEIGHT_PRICE_RATIO_NEW_ENTRY_RANK_LIMIT,
        "new_entry_min_cubes": WEIGHT_PRICE_RATIO_NEW_ENTRY_MIN_HOLDING_CUBES,
        "min_weight_increase": 0.0,
        "hard_exit_rank": 250,
        "hard_exit_min_cubes": RANK_ACCELERATION_HARD_EXIT_MIN_HOLDING_CUBES,
        "sell_rank": RANK_ACCELERATION_SELL_RANK,
        "sell_confirm_days": RANK_ACCELERATION_SELL_CONFIRM_DAYS,
        "min_holding_days": RANK_ACCELERATION_MIN_HOLDING_TRADING_DAYS,
        "retain_rank_limit": WEIGHT_PRICE_RATIO_RETAIN_CURRENT_RANK_LIMIT,
        "retain_min_cubes": WEIGHT_PRICE_RATIO_RETAIN_MIN_HOLDING_CUBES,
    },
    "rank_acceleration": {
        "strategy_key": "rank_acceleration",
        "label": "星澜贰号 · 5日排名加速",
        "fear_target_count": FEAR_GREED_FEAR_TARGET_COUNT,
        "greed_target_count": FEAR_GREED_GREED_TARGET_COUNT,
        "min_holding_cubes": RANK_ACCELERATION_MIN_HOLDING_CUBES,
        "buy_confirm_prior_days": RANK_ACCELERATION_BUY_CONFIRM_MIN_DAYS - 1,
        "current_rank_limit": RANK_ACCELERATION_CURRENT_RANK_LIMIT,
        "holding_cube_increase": RANK_ACCELERATION_MIN_HOLDING_CUBE_INCREASE,
        "metric_threshold": RANK_ACCELERATION_MIN_RANK_CHANGE,
        "new_entry_rank_limit": RANK_ACCELERATION_NEW_ENTRY_RANK_LIMIT,
        "new_entry_min_cubes": RANK_ACCELERATION_NEW_ENTRY_MIN_HOLDING_CUBES,
        "min_weight_increase": 0.0,
        "hard_exit_rank": RANK_ACCELERATION_HARD_EXIT_RANK,
        "hard_exit_min_cubes": RANK_ACCELERATION_HARD_EXIT_MIN_HOLDING_CUBES,
        "sell_rank": RANK_ACCELERATION_SELL_RANK,
        "sell_confirm_days": RANK_ACCELERATION_SELL_CONFIRM_DAYS,
        "min_holding_days": RANK_ACCELERATION_MIN_HOLDING_TRADING_DAYS,
        "retain_rank_limit": RANK_ACCELERATION_RETAIN_CURRENT_RANK_LIMIT,
        "retain_min_cubes": RANK_ACCELERATION_RETAIN_MIN_HOLDING_CUBES,
    },
    "weight_price_ratio": {
        "strategy_key": "weight_price_ratio",
        "label": "星澜叁号 · 5日权价比",
        "fear_target_count": FEAR_GREED_FEAR_TARGET_COUNT,
        "greed_target_count": FEAR_GREED_GREED_TARGET_COUNT,
        "min_holding_cubes": WEIGHT_PRICE_RATIO_MIN_HOLDING_CUBES,
        "buy_confirm_prior_days": RANK_ACCELERATION_BUY_CONFIRM_MIN_DAYS - 1,
        "current_rank_limit": WEIGHT_PRICE_RATIO_CURRENT_RANK_LIMIT,
        "holding_cube_increase": WEIGHT_PRICE_RATIO_MIN_HOLDING_CUBE_INCREASE,
        "metric_threshold": WEIGHT_PRICE_RATIO_MIN_RATIO,
        "new_entry_rank_limit": WEIGHT_PRICE_RATIO_NEW_ENTRY_RANK_LIMIT,
        "new_entry_min_cubes": WEIGHT_PRICE_RATIO_NEW_ENTRY_MIN_HOLDING_CUBES,
        "min_weight_increase": 0.0,
        "hard_exit_rank": WEIGHT_PRICE_RATIO_HARD_EXIT_RANK,
        "hard_exit_min_cubes": WEIGHT_PRICE_RATIO_HARD_EXIT_MIN_HOLDING_CUBES,
        "sell_rank": RANK_ACCELERATION_SELL_RANK,
        "sell_confirm_days": RANK_ACCELERATION_SELL_CONFIRM_DAYS,
        "min_holding_days": RANK_ACCELERATION_MIN_HOLDING_TRADING_DAYS,
        "retain_rank_limit": WEIGHT_PRICE_RATIO_RETAIN_CURRENT_RANK_LIMIT,
        "retain_min_cubes": WEIGHT_PRICE_RATIO_RETAIN_MIN_HOLDING_CUBES,
    },
}


def load_xueqiu_strategy_config(strategy_key: str) -> Dict[str, Any]:
    """Read one strategy config as a plain dict snapshot (fall back to defaults)."""
    defaults = dict(XUEQIU_STRATEGY_CONFIG_DEFAULTS.get(strategy_key) or {
        "strategy_key": strategy_key,
        "label": strategy_key,
    })
    with SessionLocal() as db:
        row = (
            db.query(XueqiuStrategyConfig)
            .filter(XueqiuStrategyConfig.strategy_key == strategy_key)
            .first()
        )
        if row is None:
            return defaults
        return {
            **defaults,
            "enabled": bool(row.enabled),
            "fear_target_count": int(row.fear_target_count),
            "greed_target_count": int(row.greed_target_count),
            "min_holding_cubes": int(row.min_holding_cubes),
            "buy_confirm_prior_days": int(row.buy_confirm_prior_days),
            "current_rank_limit": int(row.current_rank_limit),
            "holding_cube_increase": int(row.holding_cube_increase),
            "metric_threshold": float(row.metric_threshold),
            "new_entry_rank_limit": int(row.new_entry_rank_limit),
            "new_entry_min_cubes": int(row.new_entry_min_cubes),
            "min_weight_increase": float(row.min_weight_increase),
            "hard_exit_rank": int(row.hard_exit_rank),
            "hard_exit_min_cubes": int(row.hard_exit_min_cubes),
            "sell_rank": int(row.sell_rank),
            "sell_confirm_days": int(row.sell_confirm_days),
            "min_holding_days": int(row.min_holding_days),
            "retain_rank_limit": int(row.retain_rank_limit),
            "retain_min_cubes": int(row.retain_min_cubes),
            "updated_at": (
                row.updated_at.isoformat() if row.updated_at else None
            ),
        }


def _ma5_cross_state(recent_scores: List[float]) -> Optional[str]:
    """MA5 拐点：当日 > 昨日 < 前日 → up；当日 < 昨日 > 前日 → down。"""
    if len(recent_scores) < 7:
        return None
    ma5_today = sum(recent_scores[-5:]) / 5.0
    ma5_yesterday = sum(recent_scores[-6:-1]) / 5.0
    ma5_day_before = sum(recent_scores[-7:-2]) / 5.0
    if ma5_today > ma5_yesterday < ma5_day_before:
        return "up"
    if ma5_today < ma5_yesterday > ma5_day_before:
        return "down"
    return None


def resolve_xueqiu_strategy_position_target(
    fear_greed: Optional[Dict[str, Any]],
    *,
    current_holding_count: Optional[int] = None,
    fear_threshold: float = VOLUME_BOTTOM_SCORE_DEFAULT,
    greed_threshold: float = VOLUME_TOP_SCORE_DEFAULT,
    fear_volume_std: float = VOLUME_EXPAND_STD_DEFAULT,
    greed_volume_std: float = VOLUME_SHRINK_STD_DEFAULT,
    ma5_bottom_score: float = MA5_BOTTOM_SCORE_DEFAULT,
    ma5_top_score: float = MA5_TOP_SCORE_DEFAULT,
    ma5_lookback_days: int = MA5_LOOKBACK_DAYS_DEFAULT,
    fear_target_count: int = FEAR_GREED_FEAR_TARGET_COUNT,
    greed_target_count: int = FEAR_GREED_GREED_TARGET_COUNT,
    default_top_n: int = BUFFER_STRATEGY_TOP_N,
) -> Tuple[int, str]:
    """量能型 + MA5均线型 双信号的目标仓位管理。

    底（扩仓 3→x）：
      - 量能型：恐贪 ≤ fear_threshold 且放量（log量比z > fear_volume_std）
      - MA5型：恐贪MA5由降转升 且 最近 ma5_lookback_days 日任意恐贪 ≤ ma5_bottom_score
    顶（收缩 10→y）：
      - 量能型：恐贪 ≥ greed_threshold 且缩量（log量比z < -greed_volume_std）
      - MA5型：恐贪MA5由升转降 且 最近 ma5_lookback_days 日任意恐贪 ≥ ma5_top_score
    其余情况维持当前仓位（当前不足 y 只时补到 y），避免无信号时来回切换或持仓过少锁死。
    """
    if not fear_greed:
        return default_top_n, "missing_fallback"
    score = safe_float(fear_greed.get("score"))
    if score is None:
        return default_top_n, "invalid_fallback"
    volume_z = safe_float(fear_greed.get("log_volume_z"))
    recent_scores = [
        safe_float(item.get("score"))
        for item in (fear_greed.get("recent_scores") or [])
    ]
    recent_scores = [value for value in recent_scores if value is not None]

    volume_bottom = (
        volume_z is not None
        and score <= fear_threshold
        and volume_z > fear_volume_std
    )
    volume_top = (
        volume_z is not None
        and score >= greed_threshold
        and volume_z < -greed_volume_std
    )
    normalized_lookback = max(1, int(ma5_lookback_days or XUEQIU_MA5_LOOKBACK_DAYS_DEFAULT))
    recent_window = recent_scores[-normalized_lookback:]
    ma5_state = _ma5_cross_state(recent_scores)
    ma5_bottom = (
        ma5_state == "up"
        and len(recent_window) > 0
        and any(value <= ma5_bottom_score for value in recent_window)
    )
    ma5_top = (
        ma5_state == "down"
        and len(recent_window) > 0
        and any(value >= ma5_top_score for value in recent_window)
    )

    if volume_bottom and ma5_bottom:
        return fear_target_count, "bottom_both"
    if volume_bottom:
        return fear_target_count, "volume_bottom"
    if ma5_bottom:
        return fear_target_count, "ma5_bottom"
    if volume_top and ma5_top:
        return greed_target_count, "top_both"
    if volume_top:
        return greed_target_count, "volume_top"
    if ma5_top:
        return greed_target_count, "ma5_top"
    if current_holding_count is not None and current_holding_count > 0:
        return max(int(current_holding_count), greed_target_count), "neutral_keep_current"
    return default_top_n, "neutral_keep_default"


def resolve_fear_greed_target_count(
    fear_greed: Optional[Dict[str, Any]],
    *,
    current_holding_count: Optional[int] = None,
    default_top_n: int = BUFFER_STRATEGY_TOP_N,
) -> Tuple[int, str]:
    if not fear_greed:
        return default_top_n, "missing_fallback"
    score = safe_float(fear_greed.get("score"))
    if score is None:
        return default_top_n, "invalid_fallback"
    if score < FEAR_GREED_FEAR_THRESHOLD:
        return FEAR_GREED_FEAR_TARGET_COUNT, "fear"
    if score > FEAR_GREED_GREED_THRESHOLD:
        return FEAR_GREED_GREED_TARGET_COUNT, "greed"
    if current_holding_count is not None and current_holding_count <= FEAR_GREED_GREED_TARGET_COUNT:
        return FEAR_GREED_GREED_TARGET_COUNT, "neutral_keep_3"
    return default_top_n, "neutral_keep_10"


def load_xueqiu_rank_comparison_snapshot(
    *,
    current_snapshot_date: date,
    trading_days: int = RANK_ACCELERATION_COMPARE_TRADING_DAYS,
    active_only: bool = True,
) -> Dict[str, Any]:
    """Load the ranked holdings snapshot N prior trading snapshots before the current run."""
    normalized_days = max(1, int(trading_days or RANK_ACCELERATION_COMPARE_TRADING_DAYS))
    connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
    try:
        table_exists = connection.execute(
            """
            SELECT COUNT(*)
            FROM information_schema.tables
            WHERE table_name = ?
            """,
            [XUEQIU_CUBE_HOLDINGS_SNAPSHOT_TABLE],
        ).fetchone()[0]
        if not table_exists:
            return {
                "available": False,
                "reason": "snapshot_table_missing",
                "compare_snapshot_date": None,
                "trading_days": normalized_days,
                "items": [],
                "by_symbol": {},
            }

        compare_row = connection.execute(
            f"""
            SELECT snapshot_date
            FROM (
                SELECT
                    snapshot_date,
                    ROW_NUMBER() OVER (ORDER BY snapshot_date DESC) AS snapshot_rank_desc
                FROM (
                    SELECT DISTINCT snapshot_date
                    FROM {XUEQIU_CUBE_HOLDINGS_SNAPSHOT_TABLE}
                    WHERE snapshot_date < ?
                      {"AND COALESCE(is_active, FALSE)" if active_only else ""}
                ) snapshot_dates
            ) ranked_dates
            WHERE snapshot_rank_desc = ?
            """,
            [current_snapshot_date, normalized_days],
        ).fetchone()
        if not compare_row:
            return {
                "available": False,
                "reason": "comparison_snapshot_missing",
                "compare_snapshot_date": None,
                "trading_days": normalized_days,
                "items": [],
                "by_symbol": {},
            }

        compare_snapshot_date = compare_row[0]
        compare_snapshot_date_label = (
            compare_snapshot_date.isoformat()
            if hasattr(compare_snapshot_date, "isoformat")
            else str(compare_snapshot_date)
        )
        active_filter_sql = "AND COALESCE(is_active, FALSE)" if active_only else ""
        cursor = connection.execute(
            f"""
            WITH base_holdings AS (
                SELECT
                    cube_symbol,
                    stock_symbol,
                    stock_name,
                    segment_name,
                    CAST(weight_pct AS DOUBLE) AS weight_pct
                FROM {XUEQIU_CUBE_HOLDINGS_SNAPSHOT_TABLE}
                WHERE snapshot_date = ?
                  AND weight_pct IS NOT NULL
                  AND weight_pct > 0
                  {active_filter_sql}
            ),
            cube_weights AS (
                SELECT cube_symbol, SUM(weight_pct) AS stock_weight_pct
                FROM base_holdings
                GROUP BY cube_symbol
            ),
            cash_holdings AS (
                SELECT
                    cube_symbol,
                    '{CASH_SYMBOL}' AS stock_symbol,
                    '{CASH_NAME}' AS stock_name,
                    '{CASH_NAME}' AS segment_name,
                    GREATEST(0.0, 100.0 - stock_weight_pct) AS weight_pct
                FROM cube_weights
                WHERE GREATEST(0.0, 100.0 - stock_weight_pct) > 0.005
            ),
            holding_union AS (
                SELECT cube_symbol, stock_symbol, stock_name, segment_name, weight_pct
                FROM base_holdings
                UNION ALL
                SELECT cube_symbol, stock_symbol, stock_name, segment_name, weight_pct
                FROM cash_holdings
            ),
            stock_summary AS (
                SELECT
                    stock_symbol,
                    ANY_VALUE(stock_name) AS stock_name,
                    ANY_VALUE(segment_name) AS segment_name,
                    COUNT(DISTINCT cube_symbol) AS holding_cube_count,
                    SUM(weight_pct) AS total_weight_pct
                FROM holding_union
                GROUP BY stock_symbol
            ),
            ranked AS (
                SELECT
                    ROW_NUMBER() OVER (
                        ORDER BY total_weight_pct DESC, holding_cube_count DESC, stock_symbol DESC
                    ) AS composite_rank,
                    *
                FROM stock_summary
            )
            SELECT *
            FROM ranked
            ORDER BY composite_rank
            """,
            [compare_snapshot_date],
        )
        columns = [column[0] for column in cursor.description or []]
        items = [dict(zip(columns, row)) for row in cursor.fetchall()]
        by_symbol = {
            str(item.get("stock_symbol") or ""): item
            for item in items
            if item.get("stock_symbol")
        }
        return {
            "available": True,
            "reason": None,
            "compare_snapshot_date": compare_snapshot_date_label,
            "trading_days": normalized_days,
            "items": items,
            "by_symbol": by_symbol,
        }
    finally:
        connection.close()


def _xueqiu_buy_eligible_core(
    item: Dict[str, Any],
    previous: Optional[Dict[str, Any]],
    *,
    comparison_universe_count: int,
    metric: str,
    min_metric_threshold: float,
    min_holding_cubes: int,
    current_rank_limit: int,
    holding_cube_increase: int,
    min_weight_increase: float,
    new_entry_rank_limit: int,
    new_entry_min_cubes: int,
) -> bool:
    """买入资格公式（贰号排名加速/叁号权价比共用，参数均可配置）。

    满足：综合排名≤current_rank_limit、活跃组合数≥min_holding_cubes、
    组合数增加≥holding_cube_increase、总权重上升>min_weight_increase、
    策略指标≥min_metric_threshold（权价比≥x 或 排名上升≥x名），
    或强势新进（5日前未持有、排名≤new_entry_rank_limit 且组合数≥new_entry_min_cubes）。
    """
    ratio_metric = metric == "weight_price_ratio"
    current_rank = safe_int(item.get("composite_rank"))
    if current_rank is None:
        return False
    current_holding_cubes = safe_int(item.get("holding_cube_count")) or 0
    current_total_weight = safe_float(item.get("total_weight_pct")) or 0.0
    is_new = previous is None
    previous_holding_cubes = safe_int((previous or {}).get("holding_cube_count")) or 0
    previous_total_weight = safe_float((previous or {}).get("total_weight_pct")) or 0.0
    holding_cube_change = current_holding_cubes - previous_holding_cubes
    total_weight_change = current_total_weight - previous_total_weight
    strong_new_entry = (
        is_new
        and current_rank <= new_entry_rank_limit
        and current_holding_cubes >= new_entry_min_cubes
    )
    if ratio_metric:
        metric_eligible = strong_new_entry or (
            safe_float(item.get("weight_price_ratio_5d")) is not None
            and safe_float(item.get("weight_price_ratio_5d")) >= min_metric_threshold
        )
    else:
        effective_previous_rank = (
            safe_int((previous or {}).get("composite_rank"))
            or (comparison_universe_count + 1)
        )
        effective_rank_change = effective_previous_rank - current_rank
        metric_eligible = strong_new_entry or (
            not is_new and effective_rank_change >= min_metric_threshold
        )
    return (
        current_rank <= current_rank_limit
        and current_holding_cubes >= min_holding_cubes
        and holding_cube_change >= holding_cube_increase
        and total_weight_change > min_weight_increase
        and metric_eligible
    )


def load_xueqiu_snapshot_signal_history(
    *,
    current_snapshot_date: date,
    prior_days: int = 2,
    active_only: bool = True,
    metric: str = "weight_price_ratio",
    min_holding_cubes: Optional[int] = None,
    min_metric_threshold: Optional[float] = None,
    current_rank_limit: Optional[int] = None,
    holding_cube_increase: Optional[int] = None,
    min_weight_increase: Optional[float] = None,
    new_entry_rank_limit: Optional[int] = None,
    new_entry_min_cubes: Optional[int] = None,
    hard_exit_rank: Optional[int] = None,
    hard_exit_min_cubes: Optional[int] = None,
    retain_rank_limit: Optional[int] = None,
    retain_min_cubes: Optional[int] = None,
    sell_rank: Optional[int] = None,
    limit: int = 2000,
) -> List[Dict[str, Any]]:
    """在最近 ``prior_days`` 个快照日上重算买入资格与卖出信号（滑动窗口确认）。

    与线上计划共用同一套公式（_xueqiu_buy_eligible_core），数据来自每日快照 + 行情，
    不依赖机器人运行记录。
    - eligible_symbols：当天符合买入资格
    - normal_exit_symbols：当天跌出缓冲池（按指标排序的 hold 候选 Top sell_rank）
      且非硬退出 → 普通卖出信号
    返回 newest-first：[{snapshot_date, eligible_symbols, normal_exit_symbols}, ...]；
    任何错误返回 []。
    """
    normalized_days = max(1, int(prior_days or 2))
    ratio_metric = metric == "weight_price_ratio"
    effective_min_cubes = (
        int(min_holding_cubes)
        if min_holding_cubes is not None
        else (
            WEIGHT_PRICE_RATIO_MIN_HOLDING_CUBES
            if ratio_metric
            else RANK_ACCELERATION_MIN_HOLDING_CUBES
        )
    )
    effective_rank_limit = (
        int(current_rank_limit)
        if current_rank_limit is not None
        else (
            WEIGHT_PRICE_RATIO_CURRENT_RANK_LIMIT
            if ratio_metric
            else RANK_ACCELERATION_CURRENT_RANK_LIMIT
        )
    )
    effective_cube_increase = (
        int(holding_cube_increase)
        if holding_cube_increase is not None
        else (
            WEIGHT_PRICE_RATIO_MIN_HOLDING_CUBE_INCREASE
            if ratio_metric
            else RANK_ACCELERATION_MIN_HOLDING_CUBE_INCREASE
        )
    )
    effective_min_weight_increase = (
        float(min_weight_increase) if min_weight_increase is not None else 0.0
    )
    effective_new_entry_rank_limit = (
        int(new_entry_rank_limit)
        if new_entry_rank_limit is not None
        else (
            WEIGHT_PRICE_RATIO_NEW_ENTRY_RANK_LIMIT
            if ratio_metric
            else RANK_ACCELERATION_NEW_ENTRY_RANK_LIMIT
        )
    )
    effective_new_entry_min_cubes = (
        int(new_entry_min_cubes)
        if new_entry_min_cubes is not None
        else (
            WEIGHT_PRICE_RATIO_NEW_ENTRY_MIN_HOLDING_CUBES
            if ratio_metric
            else RANK_ACCELERATION_NEW_ENTRY_MIN_HOLDING_CUBES
        )
    )
    effective_metric_threshold = (
        min_metric_threshold
        if min_metric_threshold is not None
        else (
            WEIGHT_PRICE_RATIO_MIN_RATIO
            if ratio_metric
            else RANK_ACCELERATION_MIN_RANK_CHANGE
        )
    )
    effective_hard_exit_rank = (
        int(hard_exit_rank)
        if hard_exit_rank is not None
        else (
            WEIGHT_PRICE_RATIO_HARD_EXIT_RANK
            if ratio_metric
            else RANK_ACCELERATION_HARD_EXIT_RANK
        )
    )
    effective_hard_exit_min_cubes = (
        int(hard_exit_min_cubes)
        if hard_exit_min_cubes is not None
        else (
            WEIGHT_PRICE_RATIO_HARD_EXIT_MIN_HOLDING_CUBES
            if ratio_metric
            else RANK_ACCELERATION_HARD_EXIT_MIN_HOLDING_CUBES
        )
    )
    effective_retain_rank_limit = (
        int(retain_rank_limit)
        if retain_rank_limit is not None
        else (
            WEIGHT_PRICE_RATIO_RETAIN_CURRENT_RANK_LIMIT
            if ratio_metric
            else RANK_ACCELERATION_RETAIN_CURRENT_RANK_LIMIT
        )
    )
    effective_retain_min_cubes = (
        int(retain_min_cubes)
        if retain_min_cubes is not None
        else (
            WEIGHT_PRICE_RATIO_RETAIN_MIN_HOLDING_CUBES
            if ratio_metric
            else RANK_ACCELERATION_RETAIN_MIN_HOLDING_CUBES
        )
    )
    effective_sell_rank = max(
        1,
        int(sell_rank) if sell_rank is not None else RANK_ACCELERATION_SELL_RANK,
    )
    try:
        from ..app.api.xueqiu_holdings import load_xueqiu_top_holdings_latest

        connection = connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)
        try:
            rows = connection.execute(
                f"""
                SELECT DISTINCT snapshot_date
                FROM {XUEQIU_CUBE_HOLDINGS_SNAPSHOT_TABLE}
                WHERE snapshot_date < ?
                ORDER BY snapshot_date DESC
                LIMIT ?
                """,
                [current_snapshot_date, normalized_days],
            ).fetchall()
        finally:
            connection.close()
        results: List[Dict[str, Any]] = []
        for (snapshot_day,) in rows:
            latest = load_xueqiu_top_holdings_latest(
                snapshot_date=snapshot_day,
                active_only=active_only,
                limit=limit,
            )
            if not latest.get("available"):
                continue
            comparison = load_xueqiu_rank_comparison_snapshot(
                current_snapshot_date=snapshot_day,
                trading_days=RANK_ACCELERATION_COMPARE_TRADING_DAYS,
                active_only=active_only,
            )
            comparison_by_symbol = comparison.get("by_symbol") or {}
            comparison_universe_count = len(comparison.get("items") or [])
            eligible: Set[str] = set()
            hold_entries: List[Tuple[float, int, float, int, str, Dict[str, Any]]] = []
            hard_exit_by_symbol: Dict[str, bool] = {}
            for item in latest.get("items") or []:
                symbol = normalize_xueqiu_symbol(item.get("stock_symbol"))
                if not symbol or is_cash_symbol(symbol):
                    continue
                if _xueqiu_buy_eligible_core(
                    item,
                    comparison_by_symbol.get(symbol),
                    comparison_universe_count=comparison_universe_count,
                    metric=metric,
                    min_metric_threshold=effective_metric_threshold,
                    min_holding_cubes=effective_min_cubes,
                    current_rank_limit=effective_rank_limit,
                    holding_cube_increase=effective_cube_increase,
                    min_weight_increase=effective_min_weight_increase,
                    new_entry_rank_limit=effective_new_entry_rank_limit,
                    new_entry_min_cubes=effective_new_entry_min_cubes,
                ):
                    eligible.add(symbol)
                rank = safe_int(item.get("composite_rank"))
                cubes = safe_int(item.get("holding_cube_count")) or 0
                previous = comparison_by_symbol.get(symbol)
                cube_change = cubes - (safe_int((previous or {}).get("holding_cube_count")) or 0)
                weight_change = (safe_float(item.get("total_weight_pct")) or 0.0) - (
                    safe_float((previous or {}).get("total_weight_pct")) or 0.0
                )
                hard_exit = (
                    rank is None
                    or rank > effective_hard_exit_rank
                    or cubes < effective_hard_exit_min_cubes
                )
                hard_exit_by_symbol[symbol] = hard_exit
                hold_pool_eligible = (
                    rank is not None
                    and rank <= effective_retain_rank_limit
                    and cubes >= effective_retain_min_cubes
                    and not (cube_change < 0 and weight_change < 0)
                )
                if hold_pool_eligible:
                    hold_entries.append(
                        (
                            (
                                safe_float(item.get("weight_price_ratio_5d"))
                                if ratio_metric
                                else safe_float(item.get("acceleration_rank_change_5d"))
                            ) or 0.0,
                            cube_change,
                            weight_change,
                            -(safe_int(item.get("composite_rank")) or 999999),
                            symbol,
                            item,
                        )
                    )
            hold_entries.sort(key=lambda entry: entry[:5], reverse=True)
            buffer_symbols = {
                str(entry[4])
                for entry in hold_entries[:effective_sell_rank]
            }
            normal_exit_symbols = sorted(
                symbol
                for symbol, is_hard_exit in hard_exit_by_symbol.items()
                if not is_hard_exit and symbol not in buffer_symbols
            )
            results.append({
                "snapshot_date": snapshot_day.isoformat(),
                "eligible_symbols": sorted(eligible),
                "normal_exit_symbols": normal_exit_symbols,
            })
        return results
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to load Xueqiu snapshot signal history: %s", exc)
        return []


def load_xueqiu_weight_price_ratio_map(
    *,
    active_only: bool = True,
    limit: int = 2000,
    snapshot_date: Optional[date] = None,
) -> Dict[str, Dict[str, Any]]:
    """Load the latest holdings snapshot with 5-day weight/price ratio per symbol.

    Reuses the shared analytics query (same DuckDB the robot writes snapshots into),
    which computes weight_multiple_5d / momentum_multiple_5d together with price data.
    Returns {normalized_xueqiu_symbol: item} so the robot can enrich its own ranking.
    """
    from ..app.api.xueqiu_holdings import load_xueqiu_top_holdings_latest

    latest = load_xueqiu_top_holdings_latest(
        active_only=active_only,
        limit=limit,
        snapshot_date=snapshot_date,
    )
    by_symbol: Dict[str, Dict[str, Any]] = {}
    for item in latest.get("items") or []:
        symbol = normalize_xueqiu_symbol(item.get("stock_symbol"))
        if symbol:
            by_symbol[symbol] = item
    return by_symbol


def _as_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _rank_acceleration_history_plan(entry: Dict[str, Any]) -> Dict[str, Any]:
    plan = entry.get("strategy_plan")
    if isinstance(plan, dict):
        return plan
    payload = entry.get("rebalance_payload")
    if isinstance(payload, dict) and isinstance(payload.get("strategy_plan"), dict):
        return payload["strategy_plan"]
    response = entry.get("rebalance_response")
    if isinstance(response, dict) and isinstance(response.get("strategy_plan"), dict):
        return response["strategy_plan"]
    return {}


def _is_rank_acceleration_initial_build(plan: Dict[str, Any]) -> bool:
    if "initial_build" in plan:
        return bool(plan.get("initial_build"))
    current_symbols = list(plan.get("current_symbols") or [])
    added_symbols = list(plan.get("added_symbols") or [])
    target_count = safe_int(plan.get("top_n")) or RANK_ACCELERATION_TOP_N
    return len(current_symbols) <= 1 and len(added_symbols) >= target_count


def load_rank_acceleration_strategy_history(
    *,
    target_cube_symbol: str,
    current_snapshot_date: date,
    limit: int = 40,
) -> List[Dict[str, Any]]:
    """Load one production strategy snapshot per prior run date using a short DB read."""
    cutoff = datetime.combine(current_snapshot_date, datetime.min.time())
    db = SessionLocal()
    try:
        records = (
            db.query(XueqiuTopHoldingsRun)
            .filter(
                XueqiuTopHoldingsRun.target_cube_symbol == target_cube_symbol,
                XueqiuTopHoldingsRun.run_at < cutoff,
                XueqiuTopHoldingsRun.dry_run.is_(False),
            )
            .order_by(XueqiuTopHoldingsRun.run_at.desc(), XueqiuTopHoldingsRun.id.desc())
            .limit(max(1, int(limit)))
            .all()
        )
        history: List[Dict[str, Any]] = []
        seen_dates: Set[date] = set()
        for record in records:
            run_date = _as_date(record.run_at)
            if run_date is None or run_date in seen_dates:
                continue
            seen_dates.add(run_date)
            payload = dict(record.rebalance_payload or {})
            response = dict(record.rebalance_response or {})
            plan = _rank_acceleration_history_plan(
                {
                    "rebalance_payload": payload,
                    "rebalance_response": response,
                }
            )
            executed = record.status == "SUCCESS" and not response.get("skipped")
            executed_added_symbols = [
                str(item.get("stock_symbol") or "")
                for item in (record.top_holdings or [])
                if isinstance(item, dict)
                and item.get("strategy_action") == "buy"
                and item.get("stock_symbol")
            ]
            if executed and not executed_added_symbols:
                executed_added_symbols = list(plan.get("added_symbols") or [])
            initial_build = _is_rank_acceleration_initial_build(plan)
            history.append(
                {
                    "run_date": run_date,
                    "status": record.status,
                    "strategy_plan": plan,
                    "rebalance_executed": executed,
                    "added_symbols": executed_added_symbols if executed else [],
                    "replacement_count": (
                        0 if initial_build else len(executed_added_symbols)
                    ) if executed else 0,
                }
            )
        return history
    finally:
        db.close()


def build_rank_acceleration_buffer_plan(
    *,
    ranking: List[Dict[str, Any]],
    comparison_snapshot: Dict[str, Any],
    current_holdings: List[Dict[str, Any]],
    strategy_history: Optional[List[Dict[str, Any]]] = None,
    current_snapshot_date: Optional[date] = None,
    top_n: int = RANK_ACCELERATION_TOP_N,
    sell_rank: int = RANK_ACCELERATION_SELL_RANK,
    max_segment_positions: int = RANK_ACCELERATION_MAX_SEGMENT_POSITIONS,
    max_replacements: int = RANK_ACCELERATION_MAX_REPLACEMENTS,
    min_holding_cubes: Optional[int] = None,
    current_rank_limit: Optional[int] = None,
    holding_cube_increase: Optional[int] = None,
    min_weight_increase: Optional[float] = None,
    new_entry_rank_limit: Optional[int] = None,
    new_entry_min_cubes: Optional[int] = None,
    hard_exit_rank: Optional[int] = None,
    hard_exit_min_cubes: Optional[int] = None,
    sell_confirm_days: Optional[int] = None,
    min_holding_days: Optional[int] = None,
    retain_rank_limit: Optional[int] = None,
    retain_min_cubes: Optional[int] = None,
    buy_confirm_prior_days: Optional[int] = None,
    signal_history: Optional[List[Dict[str, Any]]] = None,
    metric: str = "rank_acceleration",
    min_metric_threshold: Optional[float] = None,
    strategy_name_override: Optional[str] = None,
    fear_greed_regime: Optional[str] = None,
    target_total_weight_pct: Optional[float] = None,
) -> Dict[str, Any]:
    """Build a buffered equal-weight plan over a strategy metric.

    metric="rank_acceleration" keeps the 星澜贰号 behaviour: stocks are ranked by
    5-day composite-rank increase.  metric="weight_price_ratio" (星澜叁号) ranks by
    weight_price_ratio_5d = 5d weight multiple / 5d price multiple; the ratio fields
    are expected to be pre-merged into ``ranking`` items by the caller.
    """
    if metric not in ("rank_acceleration", "weight_price_ratio"):
        raise ValueError(f"Unsupported plan metric: {metric}")
    ratio_metric = metric == "weight_price_ratio"
    if min_metric_threshold is None:
        min_metric_threshold = (
            WEIGHT_PRICE_RATIO_MIN_RATIO
            if ratio_metric
            else RANK_ACCELERATION_MIN_RANK_CHANGE
        )
    hard_exit_rank = (
        int(hard_exit_rank)
        if hard_exit_rank is not None
        else (
            WEIGHT_PRICE_RATIO_HARD_EXIT_RANK
            if ratio_metric
            else RANK_ACCELERATION_HARD_EXIT_RANK
        )
    )
    hard_exit_min_cubes = (
        int(hard_exit_min_cubes)
        if hard_exit_min_cubes is not None
        else (
            WEIGHT_PRICE_RATIO_HARD_EXIT_MIN_HOLDING_CUBES
            if ratio_metric
            else RANK_ACCELERATION_HARD_EXIT_MIN_HOLDING_CUBES
        )
    )
    effective_min_cubes = (
        int(min_holding_cubes)
        if min_holding_cubes is not None
        else (
            WEIGHT_PRICE_RATIO_MIN_HOLDING_CUBES
            if ratio_metric
            else RANK_ACCELERATION_MIN_HOLDING_CUBES
        )
    )
    effective_rank_limit = (
        int(current_rank_limit)
        if current_rank_limit is not None
        else (
            WEIGHT_PRICE_RATIO_CURRENT_RANK_LIMIT
            if ratio_metric
            else RANK_ACCELERATION_CURRENT_RANK_LIMIT
        )
    )
    effective_cube_increase = (
        int(holding_cube_increase)
        if holding_cube_increase is not None
        else (
            WEIGHT_PRICE_RATIO_MIN_HOLDING_CUBE_INCREASE
            if ratio_metric
            else RANK_ACCELERATION_MIN_HOLDING_CUBE_INCREASE
        )
    )
    effective_min_weight_increase = (
        float(min_weight_increase) if min_weight_increase is not None else 0.0
    )
    effective_new_entry_rank_limit = (
        int(new_entry_rank_limit)
        if new_entry_rank_limit is not None
        else (
            WEIGHT_PRICE_RATIO_NEW_ENTRY_RANK_LIMIT
            if ratio_metric
            else RANK_ACCELERATION_NEW_ENTRY_RANK_LIMIT
        )
    )
    effective_new_entry_min_cubes = (
        int(new_entry_min_cubes)
        if new_entry_min_cubes is not None
        else (
            WEIGHT_PRICE_RATIO_NEW_ENTRY_MIN_HOLDING_CUBES
            if ratio_metric
            else RANK_ACCELERATION_NEW_ENTRY_MIN_HOLDING_CUBES
        )
    )
    effective_retain_rank_limit = (
        int(retain_rank_limit)
        if retain_rank_limit is not None
        else (
            WEIGHT_PRICE_RATIO_RETAIN_CURRENT_RANK_LIMIT
            if ratio_metric
            else RANK_ACCELERATION_RETAIN_CURRENT_RANK_LIMIT
        )
    )
    effective_retain_min_cubes = (
        int(retain_min_cubes)
        if retain_min_cubes is not None
        else (
            WEIGHT_PRICE_RATIO_RETAIN_MIN_HOLDING_CUBES
            if ratio_metric
            else RANK_ACCELERATION_RETAIN_MIN_HOLDING_CUBES
        )
    )
    effective_sell_confirm_days = (
        int(sell_confirm_days)
        if sell_confirm_days is not None
        else RANK_ACCELERATION_SELL_CONFIRM_DAYS
    )
    effective_min_holding_days = (
        int(min_holding_days)
        if min_holding_days is not None
        else RANK_ACCELERATION_MIN_HOLDING_TRADING_DAYS
    )
    effective_buy_confirm_prior_days = (
        int(buy_confirm_prior_days)
        if buy_confirm_prior_days is not None
        else RANK_ACCELERATION_BUY_CONFIRM_MIN_DAYS - 1
    )
    if not comparison_snapshot.get("available"):
        raise RuntimeError(
            "Rank acceleration comparison snapshot unavailable: "
            f"{comparison_snapshot.get('reason') or 'unknown'}"
        )

    normalized_top_n = max(1, int(top_n or RANK_ACCELERATION_TOP_N))
    normalized_target_total_weight_pct = min(
        100.0,
        max(
            0.0,
            float(target_total_weight_pct)
            if target_total_weight_pct is not None
            else float(normalized_top_n * 10),
        ),
    )
    normalized_sell_rank = max(normalized_top_n, int(sell_rank or RANK_ACCELERATION_SELL_RANK))
    snapshot_date = current_snapshot_date or date.today()
    comparison_by_symbol = comparison_snapshot.get("by_symbol") or {}
    comparison_universe_count = len(comparison_snapshot.get("items") or [])
    current_items = extract_current_target_holdings(current_holdings)
    current_by_symbol = {item["stock_symbol"]: item for item in current_items}
    current_symbols = [item["stock_symbol"] for item in current_items]
    current_symbol_set = set(current_symbols)
    enriched_items: List[Dict[str, Any]] = []

    normalized_history: List[Dict[str, Any]] = []
    seen_history_dates: Set[date] = set()
    for history_entry in strategy_history or []:
        run_date = _as_date(history_entry.get("run_date"))
        if run_date is None or run_date >= snapshot_date or run_date in seen_history_dates:
            continue
        seen_history_dates.add(run_date)
        entry = dict(history_entry)
        entry["run_date"] = run_date
        normalized_history.append(entry)
    normalized_history.sort(key=lambda entry: entry["run_date"], reverse=True)

    def history_signal_symbols(entry: Dict[str, Any], key: str) -> Set[str]:
        plan = _rank_acceleration_history_plan(entry)
        values = plan.get(key) or []
        if key == "daily_buy_signal_symbols" and not values:
            values = [
                item.get("stock_symbol")
                for item in (plan.get("acceleration_items") or [])
                if isinstance(item, dict) and item.get("buy_eligible")
            ][:normalized_top_n]
        return {str(value) for value in values if value}

    if signal_history is None:
        signal_history = load_xueqiu_snapshot_signal_history(
            current_snapshot_date=snapshot_date,
            prior_days=max(
                effective_buy_confirm_prior_days,
                effective_sell_confirm_days - 1,
            ),
            metric=metric,
            min_holding_cubes=effective_min_cubes,
            min_metric_threshold=min_metric_threshold,
            current_rank_limit=effective_rank_limit,
            holding_cube_increase=effective_cube_increase,
            min_weight_increase=effective_min_weight_increase,
            new_entry_rank_limit=effective_new_entry_rank_limit,
            new_entry_min_cubes=effective_new_entry_min_cubes,
            hard_exit_rank=hard_exit_rank,
            hard_exit_min_cubes=hard_exit_min_cubes,
            retain_rank_limit=effective_retain_rank_limit,
            retain_min_cubes=effective_retain_min_cubes,
            sell_rank=normalized_sell_rank,
        )
    # 滑动窗口确认：买入资格/卖出信号均按快照重算（不依赖运行记录），newest-first
    previous_buy_signal_sets = [
        {str(value) for value in (entry.get("eligible_symbols") or []) if value}
        for entry in (signal_history or [])
    ]
    previous_exit_signal_sets = [
        {str(value) for value in (entry.get("normal_exit_symbols") or []) if value}
        for entry in (signal_history or [])[: effective_sell_confirm_days - 1]
    ]

    for index, ranking_item in enumerate(ranking, start=1):
        symbol = normalize_xueqiu_symbol(ranking_item.get("stock_symbol"))
        if not symbol or not to_raw_xueqiu_symbol(symbol):
            continue
        item = dict(ranking_item)
        item["stock_symbol"] = symbol
        current_rank = safe_int(item.get("composite_rank")) or index
        current_holding_cubes = safe_int(item.get("holding_cube_count")) or 0
        current_total_weight = safe_float(item.get("total_weight_pct")) or 0.0
        previous = comparison_by_symbol.get(symbol)
        is_new = previous is None
        previous_rank = safe_int((previous or {}).get("composite_rank"))
        previous_holding_cubes = safe_int((previous or {}).get("holding_cube_count")) or 0
        previous_total_weight = safe_float((previous or {}).get("total_weight_pct")) or 0.0
        effective_previous_rank = previous_rank or (comparison_universe_count + 1)
        effective_rank_change = effective_previous_rank - current_rank
        holding_cube_change = current_holding_cubes - previous_holding_cubes
        total_weight_change = current_total_weight - previous_total_weight
        strong_new_entry = (
            is_new
            and current_rank <= effective_new_entry_rank_limit
            and current_holding_cubes >= effective_new_entry_min_cubes
        )
        buy_eligible = _xueqiu_buy_eligible_core(
            item,
            previous,
            comparison_universe_count=comparison_universe_count,
            metric=metric,
            min_metric_threshold=min_metric_threshold,
            min_holding_cubes=effective_min_cubes,
            current_rank_limit=effective_rank_limit,
            holding_cube_increase=effective_cube_increase,
            min_weight_increase=effective_min_weight_increase,
            new_entry_rank_limit=effective_new_entry_rank_limit,
            new_entry_min_cubes=effective_new_entry_min_cubes,
        )
        hold_pool_eligible = (
            current_rank
            <= (
                WEIGHT_PRICE_RATIO_RETAIN_CURRENT_RANK_LIMIT
                if ratio_metric
                else RANK_ACCELERATION_RETAIN_CURRENT_RANK_LIMIT
            )
            and current_holding_cubes
            >= (
                WEIGHT_PRICE_RATIO_RETAIN_MIN_HOLDING_CUBES
                if ratio_metric
                else RANK_ACCELERATION_RETAIN_MIN_HOLDING_CUBES
            )
            and not (holding_cube_change < 0 and total_weight_change < 0)
        )
        item.update(
            {
                "rank_compare_snapshot_date": comparison_snapshot.get("compare_snapshot_date"),
                "rank_5d_ago": previous_rank,
                "rank_change_5d": None if is_new else effective_rank_change,
                "acceleration_rank_change_5d": effective_rank_change,
                "holding_cube_count_5d_ago": previous_holding_cubes,
                "holding_cube_count_change_5d": holding_cube_change,
                "total_weight_pct_5d_ago": previous_total_weight,
                "total_weight_change_5d": total_weight_change,
                "is_new_5d": is_new,
                "strong_new_entry": strong_new_entry,
                "buy_eligible": buy_eligible,
                "hold_pool_eligible": hold_pool_eligible,
            }
        )
        enriched_items.append(item)

    def primary_metric(item: Dict[str, Any]) -> float:
        if ratio_metric:
            return safe_float(item.get("weight_price_ratio_5d")) or 0.0
        return safe_float(item.get("acceleration_rank_change_5d")) or 0.0

    def acceleration_sort_key(item: Dict[str, Any]) -> Tuple[float, int, float, int, str]:
        return (
            primary_metric(item),
            safe_int(item.get("holding_cube_count_change_5d")) or 0,
            safe_float(item.get("total_weight_change_5d")) or 0.0,
            -(safe_int(item.get("composite_rank")) or 999999),
            str(item.get("stock_symbol") or ""),
        )

    acceleration_items = sorted(enriched_items, key=acceleration_sort_key, reverse=True)
    for index, item in enumerate(acceleration_items, start=1):
        item["acceleration_rank"] = index
        item["strategy_rank"] = index

    raw_buy_candidates = sorted(
        [item for item in enriched_items if item.get("buy_eligible")],
        key=acceleration_sort_key,
        reverse=True,
    )
    daily_buy_signal_symbols: List[str] = []
    for index, item in enumerate(raw_buy_candidates, start=1):
        item["buy_signal_rank"] = index
        item["daily_buy_signal"] = index <= normalized_top_n
        if item["daily_buy_signal"]:
            daily_buy_signal_symbols.append(item["stock_symbol"])

    daily_buy_signal_set = set(daily_buy_signal_symbols)
    confirmed_buy_candidates: List[Dict[str, Any]] = []
    for item in raw_buy_candidates:
        symbol = item["stock_symbol"]
        prior_confirmation_days = sum(
            int(symbol in signal_set) for signal_set in previous_buy_signal_sets
        )
        item["buy_confirmation_count"] = 1 + prior_confirmation_days
        item["buy_confirmed"] = prior_confirmation_days >= effective_buy_confirm_prior_days
        if item["buy_confirmed"]:
            confirmed_buy_candidates.append(item)

    hold_candidates = sorted(
        [item for item in enriched_items if item.get("hold_pool_eligible")],
        key=acceleration_sort_key,
        reverse=True,
    )
    for index, item in enumerate(hold_candidates, start=1):
        item["hold_buffer_rank"] = index
        item["retain_eligible"] = index <= normalized_sell_rank
    item_by_symbol = {item["stock_symbol"]: item for item in enriched_items}
    buffer_symbols = {
        item["stock_symbol"]
        for item in hold_candidates[:normalized_sell_rank]
    }

    def history_added_symbols(entry: Dict[str, Any]) -> Set[str]:
        values = entry.get("added_symbols")
        if values is None:
            values = _rank_acceleration_history_plan(entry).get("added_symbols") or []
        return {str(value) for value in values if value}

    entry_dates: Dict[str, Optional[date]] = {}
    for symbol in current_symbols:
        entry_dates[symbol] = next(
            (
                entry["run_date"]
                for entry in normalized_history
                if entry.get("rebalance_executed") and symbol in history_added_symbols(entry)
            ),
            None,
        )

    hard_exit_symbols: List[str] = []
    normal_exit_signal_symbols: List[str] = []
    confirmed_normal_exit_symbols: List[str] = []
    min_holding_blocked_symbols: List[str] = []
    holding_statuses: List[Dict[str, Any]] = []
    normal_exit_ready: List[str] = []
    for symbol in current_symbols:
        item = item_by_symbol.get(symbol) or {}
        current_rank = safe_int(item.get("composite_rank"))
        current_holding_cubes = safe_int(item.get("holding_cube_count")) or 0
        hard_exit = (
            current_rank is None
            or current_rank > hard_exit_rank
            or current_holding_cubes < hard_exit_min_cubes
        )
        normal_exit_signal = not hard_exit and symbol not in buffer_symbols
        exit_confirmed = (
            normal_exit_signal
            and len(previous_exit_signal_sets) == effective_sell_confirm_days - 1
            and all(symbol in signal_set for signal_set in previous_exit_signal_sets)
        )
        entry_date = entry_dates[symbol]
        completed_holding_days = None
        if entry_date is not None:
            completed_holding_days = len(
                {
                    entry["run_date"]
                    for entry in normalized_history
                    if entry_date < entry["run_date"] < snapshot_date
                }
            )
        min_holding_satisfied = (
            completed_holding_days is None
            or completed_holding_days >= effective_min_holding_days
        )
        if hard_exit:
            hard_exit_symbols.append(symbol)
        if normal_exit_signal:
            normal_exit_signal_symbols.append(symbol)
        if exit_confirmed:
            confirmed_normal_exit_symbols.append(symbol)
            if min_holding_satisfied:
                normal_exit_ready.append(symbol)
            else:
                min_holding_blocked_symbols.append(symbol)
        holding_statuses.append(
            {
                "stock_symbol": symbol,
                "entry_date": entry_date.isoformat() if entry_date else None,
                "completed_holding_days": completed_holding_days,
                "hold_buffer_rank": safe_int(item.get("hold_buffer_rank")),
                "hard_exit": hard_exit,
                "normal_exit_signal": normal_exit_signal,
                "exit_confirmed": exit_confirmed,
                "min_holding_satisfied": min_holding_satisfied,
            }
        )

    def history_replacement_count(entry: Dict[str, Any]) -> int:
        if not entry.get("rebalance_executed"):
            return 0
        value = safe_int(entry.get("replacement_count"))
        if value is not None:
            return max(0, value)
        plan = _rank_acceleration_history_plan(entry)
        if _is_rank_acceleration_initial_build(plan):
            return 0
        return len(history_added_symbols(entry))

    rolling_history = normalized_history[: RANK_ACCELERATION_ROLLING_REPLACEMENT_DAYS - 1]
    rolling_prior_replacements = sum(history_replacement_count(entry) for entry in rolling_history)
    rolling_replacement_capacity = max(
        0,
        RANK_ACCELERATION_ROLLING_MAX_REPLACEMENTS - rolling_prior_replacements,
    )
    initial_build = not current_symbols
    addition_capacity = (
        normalized_top_n
        if initial_build
        else min(max_replacements, rolling_replacement_capacity)
    )

    def normal_exit_sort_key(symbol: str) -> Tuple[int, int, float, str]:
        item = item_by_symbol.get(symbol) or {}
        return (
            safe_int(item.get("hold_buffer_rank")) or 999999,
            safe_int(item.get("composite_rank")) or 999999,
            -primary_metric(item),
            symbol,
        )

    normal_exit_ready = sorted(normal_exit_ready, key=normal_exit_sort_key, reverse=True)
    hard_exit_set = set(hard_exit_symbols)
    retained_symbols = [symbol for symbol in current_symbols if symbol not in hard_exit_set]

    # 恐贪择时收缩目标仓位（如贪婪10只→3只）时，按策略指标排名直接裁剪超配部分，
    # 与壹号的行为一致。贰号 top_n 固定，retained 不会超过 top_n，因此不受影响。
    trim_removed_symbols: List[str] = []
    if len(retained_symbols) > normalized_top_n:
        ranked_retained = sorted(
            retained_symbols,
            key=lambda symbol: (
                safe_int((item_by_symbol.get(symbol) or {}).get("hold_buffer_rank")) or 999999,
                symbol,
            ),
        )
        retained_symbols = ranked_retained[:normalized_top_n]
        trim_removed_symbols = ranked_retained[normalized_top_n:]

    segment_counts: Dict[str, int] = defaultdict(int)

    def segment_key(symbol: str) -> str:
        segment = str((item_by_symbol.get(symbol) or {}).get("segment_name") or "").strip()
        return "" if segment in {"", "其他", CASH_NAME} else segment

    for symbol in retained_symbols:
        segment = segment_key(symbol)
        if segment:
            segment_counts[segment] += 1

    added_symbols: List[str] = []
    selected_set = set(retained_symbols)
    buy_candidates = [
        item
        for item in confirmed_buy_candidates
        if item.get("stock_symbol") not in current_symbol_set
    ]

    def take_next_buy() -> Optional[str]:
        for item in buy_candidates:
            symbol = item["stock_symbol"]
            if symbol in selected_set:
                continue
            segment = segment_key(symbol)
            if segment and segment_counts[segment] >= max_segment_positions:
                continue
            selected_set.add(symbol)
            if segment:
                segment_counts[segment] += 1
            return symbol
        return None

    remaining_additions = addition_capacity
    base_vacancies = max(0, normalized_top_n - len(retained_symbols))
    while base_vacancies > 0 and remaining_additions > 0:
        symbol = take_next_buy()
        if not symbol:
            break
        added_symbols.append(symbol)
        base_vacancies -= 1
        remaining_additions -= 1

    normal_removed_symbols: List[str] = []
    for symbol in normal_exit_ready:
        if remaining_additions <= 0:
            break
        if symbol not in retained_symbols:
            continue
        removed_segment = segment_key(symbol)
        retained_symbols.remove(symbol)
        selected_set.discard(symbol)
        if removed_segment:
            segment_counts[removed_segment] = max(0, segment_counts[removed_segment] - 1)
        replacement_symbol = take_next_buy()
        if not replacement_symbol:
            retained_symbols.append(symbol)
            selected_set.add(symbol)
            if removed_segment:
                segment_counts[removed_segment] += 1
            continue
        normal_removed_symbols.append(symbol)
        added_symbols.append(replacement_symbol)
        remaining_additions -= 1

    removed_symbols = hard_exit_symbols + trim_removed_symbols + normal_removed_symbols
    planned_removed = hard_exit_symbols + trim_removed_symbols + normal_exit_ready
    deferred_normal_exit_symbols = [
        symbol for symbol in normal_exit_ready if symbol not in set(normal_removed_symbols)
    ]

    final_symbols = retained_symbols + added_symbols
    final_symbols = sorted(
        final_symbols,
        key=lambda symbol: (
            safe_int((item_by_symbol.get(symbol) or {}).get("acceleration_rank")) or 999999,
            symbol,
        ),
    )
    component_changed = set(final_symbols) != set(current_symbols)
    target_weight = normalized_target_total_weight_pct / normalized_top_n
    execution_weights = (
        build_min_turnover_execution_weights(
            final_symbols=final_symbols,
            added_symbols=added_symbols,
            current_by_symbol=current_by_symbol,
            target_total_weight_pct=normalized_target_total_weight_pct,
        )
        if len(final_symbols) == normalized_top_n
        else {symbol: target_weight for symbol in final_symbols}
    )

    target_items: List[Dict[str, Any]] = []
    for symbol in final_symbols:
        item = dict(item_by_symbol.get(symbol) or {})
        current_weight = safe_float((current_by_symbol.get(symbol) or {}).get("weight_pct"))
        execution_weight = execution_weights.get(symbol, target_weight)
        strategy_action = "buy" if symbol in added_symbols else "keep"
        if strategy_action == "keep" and (
            current_weight is None
            or abs(execution_weight - current_weight) > BUFFER_RETAIN_WEIGHT_TOLERANCE_PCT
        ):
            strategy_action = "adjust"
        item.update(
            {
                "stock_symbol": symbol,
                "stock_name": item.get("stock_name") or (current_by_symbol.get(symbol) or {}).get("stock_name") or "",
                "strategy_rank": item.get("acceleration_rank"),
                "top_normalized_weight_pct": target_weight,
                "rebalance_weight_pct": execution_weight,
                "strategy_action": strategy_action,
                "current_weight_pct": current_weight,
            }
        )
        target_items.append(item)

    # 星澜贰、叁号的目标持股数变化也会改变目标总仓位。即使成分未变，也要提交一次
    # 权重调整（例如修正历史上 3 只各 33.33% 为 3 只各约 10%）。
    weight_adjustment_required = any(
        item.get("strategy_action") == "adjust" for item in target_items
    )
    component_changed = component_changed or weight_adjustment_required

    removed_items: List[Dict[str, Any]] = []
    trim_removed_set = set(trim_removed_symbols)
    for symbol in removed_symbols:
        item = dict(item_by_symbol.get(symbol) or {})
        if symbol in trim_removed_set:
            action = "trim"
            exit_reason = (
                f"恐贪择时目标仓位降至{normalized_top_n}只，按权价比排名调出"
                if ratio_metric
                else f"目标仓位降至{normalized_top_n}只，按加速排名调出"
            )
        elif symbol in hard_exit_set:
            action = "hard_sell"
            exit_reason = f"综合排名>{hard_exit_rank}或活跃组合数<{hard_exit_min_cubes}"
        else:
            action = "sell"
            exit_reason = (
                f"连续{effective_sell_confirm_days}日跌出权价比缓冲Top{normalized_sell_rank}"
                if ratio_metric
                else f"连续{effective_sell_confirm_days}日跌出加速Top{normalized_sell_rank}"
            )
        item.update(
            {
                "stock_symbol": symbol,
                "stock_name": item.get("stock_name") or (current_by_symbol.get(symbol) or {}).get("stock_name") or "",
                "strategy_action": action,
                "exit_reason": exit_reason,
                "current_weight_pct": (current_by_symbol.get(symbol) or {}).get("weight_pct"),
            }
        )
        removed_items.append(item)

    return {
        "strategy_name": strategy_name_override or (
            WEIGHT_PRICE_RATIO_STRATEGY_NAME if ratio_metric else RANK_ACCELERATION_STRATEGY_NAME
        ),
        "top_n": normalized_top_n,
        "target_total_weight_pct": normalized_target_total_weight_pct,
        "target_cash_weight_pct": 100.0 - normalized_target_total_weight_pct,
        "sell_rank": normalized_sell_rank,
        "compare_snapshot_date": comparison_snapshot.get("compare_snapshot_date"),
        "compare_trading_days": comparison_snapshot.get("trading_days"),
        "buy_rule": (
            (
                f"当前综合排名Top{effective_rank_limit}、至少{effective_min_cubes}个活跃组合持有、"
                f"持仓组合增加至少{effective_cube_increase}个且总权重上升>"
                f"{effective_min_weight_increase:g}、5日权价比≥{min_metric_threshold:g}"
                f"（权重涨幅超过股价涨幅，疑似主动加仓）；"
                f"强势新进需进入Top{effective_new_entry_rank_limit}且≥{effective_new_entry_min_cubes}个组合；"
                f"当天符合资格；历史确认：最近快照日至少{effective_buy_confirm_prior_days}天也符合"
                f"（{effective_buy_confirm_prior_days}个快照日，0=只看当天，按快照滑动窗口重算）"
            )
            if ratio_metric
            else (
                f"当前综合排名Top{effective_rank_limit}、至少{effective_min_cubes}个活跃组合持有、"
                f"持仓组合增加至少{effective_cube_increase}个且总权重上升>"
                f"{effective_min_weight_increase:g}；旧标的5日上升至少{min_metric_threshold:g}名，"
                f"强势新进需进入Top{effective_new_entry_rank_limit}且≥{effective_new_entry_min_cubes}个组合；"
                f"当天符合资格；历史确认：最近快照日至少{effective_buy_confirm_prior_days}天也符合"
                f"（{effective_buy_confirm_prior_days}个快照日，0=只看当天，按快照滑动窗口重算）"
            )
        ),
        "sell_rule": (
            (
                f"持满{effective_min_holding_days}个完整交易日且连续"
                f"{effective_sell_confirm_days}日跌出5日权价比Top{normalized_sell_rank}才卖；"
                f"综合排名>{hard_exit_rank}或活跃组合数<{hard_exit_min_cubes}立即退出"
            )
            if ratio_metric
            else (
                f"持满{effective_min_holding_days}个完整交易日且连续"
                f"{effective_sell_confirm_days}日跌出5日排名加速Top{normalized_sell_rank}才卖；"
                f"综合排名>{hard_exit_rank}或活跃组合数<{hard_exit_min_cubes}立即退出"
            )
        ),
        "execution_weight_rule": (
            f"Top{normalized_top_n}每只目标上限{target_weight:g}%；不足{normalized_top_n}只时剩余资金留现金；"
            f"新增成分按板块最多{max_segment_positions}只；每次最多替换{max_replacements}只，"
            f"滚动{RANK_ACCELERATION_ROLLING_REPLACEMENT_DAYS}日最多"
            f"{RANK_ACCELERATION_ROLLING_MAX_REPLACEMENTS}只"
        ),
        "metric": metric,
        "fear_greed_regime": fear_greed_regime,
        "current_symbols": current_symbols,
        "retained_symbols": retained_symbols,
        "removed_symbols": removed_symbols,
        "planned_removed_symbols": planned_removed,
        "added_symbols": added_symbols,
        "final_symbols": final_symbols,
        "component_changed": component_changed,
        "weight_adjustment_required": weight_adjustment_required,
        "initial_build": initial_build,
        "daily_buy_signal_symbols": daily_buy_signal_symbols,
        "confirmed_buy_symbols": [item["stock_symbol"] for item in confirmed_buy_candidates],
        "normal_exit_signal_symbols": normal_exit_signal_symbols,
        "confirmed_normal_exit_symbols": confirmed_normal_exit_symbols,
        "hard_exit_symbols": hard_exit_symbols,
        "trim_removed_symbols": trim_removed_symbols,
        "normal_removed_symbols": normal_removed_symbols,
        "min_holding_blocked_symbols": min_holding_blocked_symbols,
        "deferred_normal_exit_symbols": deferred_normal_exit_symbols,
        "holding_statuses": holding_statuses,
        "rolling_prior_replacements": rolling_prior_replacements,
        "rolling_replacement_capacity": rolling_replacement_capacity,
        "replacement_count": 0 if initial_build else len(added_symbols),
        "eligible_buy_count": len(buy_candidates),
        "eligible_retain_count": len(hold_candidates),
        "target_items": target_items,
        "removed_items": removed_items,
        "acceleration_items": hold_candidates[:normalized_sell_rank],
        "current_items": current_items,
        "summary": {
            "current": [_symbol_label(symbol, item_by_symbol, current_by_symbol) for symbol in current_symbols],
            "retained": [_symbol_label(symbol, item_by_symbol, current_by_symbol) for symbol in retained_symbols],
            "removed": [_symbol_label(symbol, item_by_symbol, current_by_symbol) for symbol in removed_symbols],
            "added": [_symbol_label(symbol, item_by_symbol, current_by_symbol) for symbol in added_symbols],
            "final": [_symbol_label(symbol, item_by_symbol, current_by_symbol) for symbol in final_symbols],
        },
    }


def build_weight_price_ratio_buffer_plan(
    *,
    ranking: List[Dict[str, Any]],
    comparison_snapshot: Dict[str, Any],
    current_holdings: List[Dict[str, Any]],
    strategy_history: Optional[List[Dict[str, Any]]] = None,
    current_snapshot_date: Optional[date] = None,
    top_n: int = WEIGHT_PRICE_RATIO_TOP_N,
    sell_rank: Optional[int] = None,
    max_segment_positions: int = RANK_ACCELERATION_MAX_SEGMENT_POSITIONS,
    max_replacements: int = RANK_ACCELERATION_MAX_REPLACEMENTS,
    min_holding_cubes: Optional[int] = None,
    current_rank_limit: Optional[int] = None,
    holding_cube_increase: Optional[int] = None,
    min_weight_increase: Optional[float] = None,
    new_entry_rank_limit: Optional[int] = None,
    new_entry_min_cubes: Optional[int] = None,
    hard_exit_rank: Optional[int] = None,
    hard_exit_min_cubes: Optional[int] = None,
    sell_confirm_days: Optional[int] = None,
    min_holding_days: Optional[int] = None,
    retain_rank_limit: Optional[int] = None,
    retain_min_cubes: Optional[int] = None,
    buy_confirm_prior_days: Optional[int] = None,
    signal_history: Optional[List[Dict[str, Any]]] = None,
    fear_greed_regime: Optional[str] = None,
) -> Dict[str, Any]:
    """星澜叁号：按 5日权价比 排序/买入，缓冲卖出，等同贰号框架。"""
    normalized_sell_rank = sell_rank if sell_rank is not None else max(
        top_n,
        int(round(WEIGHT_PRICE_RATIO_SELL_RANK_MULTIPLIER * top_n)),
    )
    return build_rank_acceleration_buffer_plan(
        ranking=ranking,
        comparison_snapshot=comparison_snapshot,
        current_holdings=current_holdings,
        strategy_history=strategy_history,
        current_snapshot_date=current_snapshot_date,
        top_n=top_n,
        sell_rank=normalized_sell_rank,
        max_segment_positions=max_segment_positions,
        max_replacements=max_replacements,
        min_holding_cubes=min_holding_cubes,
        current_rank_limit=current_rank_limit,
        holding_cube_increase=holding_cube_increase,
        min_weight_increase=min_weight_increase,
        new_entry_rank_limit=new_entry_rank_limit,
        new_entry_min_cubes=new_entry_min_cubes,
        hard_exit_rank=hard_exit_rank,
        hard_exit_min_cubes=hard_exit_min_cubes,
        sell_confirm_days=sell_confirm_days,
        min_holding_days=min_holding_days,
        retain_rank_limit=retain_rank_limit,
        retain_min_cubes=retain_min_cubes,
        buy_confirm_prior_days=buy_confirm_prior_days,
        signal_history=signal_history,
        metric="weight_price_ratio",
        strategy_name_override=WEIGHT_PRICE_RATIO_STRATEGY_NAME,
        fear_greed_regime=fear_greed_regime,
    )

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
    strategy_sell_rank = safe_int((strategy_plan or {}).get("sell_rank"))
    report_table_items = build_report_table_items(
        top_items=top_items,
        aggregate=aggregate,
        top_n=top_n,
        sell_rank=strategy_sell_rank,
    )
    display_count = report_display_count(top_n, strategy_sell_rank)
    filter_label = active_filter_compact_label(active_filter_summary)
    lines = [
        f"雪球年榜1000{filter_label}组合综合持仓权重 Top{display_count}",
        "",
        f"统计时间: {run_at.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"年榜缓存时间: {rank_cache_fetched_at.strftime('%Y-%m-%d %H:%M:%S') if rank_cache_fetched_at else '-'}",
        f"年榜本次刷新: {'是' if rank_cache_refreshed else '否'}",
        f"组合总数: {len(cubes)}",
    ]
    if active_filter_summary:
        lines.extend(
            [
                f"活跃筛选: {active_filter_description(active_filter_summary)}",
                f"活跃截止时间: {fmt_datetime_value(active_filter_summary.get('active_since'))}",
                f"活跃组合: {active_filter_summary.get('active_cube_count')}",
                f"非活跃组合: {active_filter_summary.get('inactive_cube_count')}",
                f"活跃检查失败: {active_filter_summary.get('activity_failed_count')}",
                f"主理人调仓缓存命中: {active_filter_summary.get('activity_cache_hit_count')}",
                f"主理人调仓翻页触顶: {active_filter_summary.get('activity_page_limit_hit_count')}",
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
        f"先筛选{active_filter_description(active_filter_summary)}的组合；"
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
            f"邮件展示: 综合排名 Top{display_count}；目标权重只计算 Top{top_n}。",
        ]
    )
    if strategy_plan:
        plan_summary = strategy_plan.get("summary") or {}
        buy_rule = strategy_plan.get("buy_rule") or (
            f"从当前综合排名 Top{strategy_plan.get('top_n', top_n)} 中补足到 "
            f"{strategy_plan.get('top_n', top_n)} 只"
        )
        sell_rule = strategy_plan.get("sell_rule") or (
            f"已有持仓跌出 Top{strategy_plan.get('sell_rank', BUFFER_STRATEGY_SELL_RANK)} 才卖"
        )
        lines.extend(
            [
                f"买入/补位: {buy_rule}。",
                f"卖出: {sell_rule}。",
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
    strategy_sell_rank = safe_int((strategy_plan or {}).get("sell_rank"))
    report_table_items = build_report_table_items(
        top_items=top_items,
        aggregate=aggregate,
        top_n=top_n,
        sell_rank=strategy_sell_rank,
    )
    display_count = report_display_count(top_n, strategy_sell_rank)
    filter_label = active_filter_compact_label(active_filter_summary)
    scope_text = (
        f"先筛选{active_filter_description(active_filter_summary)}的组合；"
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
                ("活跃筛选", active_filter_description(active_filter_summary)),
                ("活跃截止时间", fmt_datetime_value(active_filter_summary.get("active_since"))),
                ("活跃组合", active_filter_summary.get("active_cube_count")),
                ("非活跃组合", active_filter_summary.get("inactive_cube_count")),
                ("活跃检查失败", active_filter_summary.get("activity_failed_count")),
                ("主理人调仓缓存命中", active_filter_summary.get("activity_cache_hit_count")),
                ("主理人调仓翻页触顶", active_filter_summary.get("activity_page_limit_hit_count")),
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
            ("邮件展示", f"综合排名 Top{display_count}；目标权重只计算 Top{top_n}"),
        ]
    )
    if strategy_plan:
        plan_summary = strategy_plan.get("summary") or {}
        buy_rule = strategy_plan.get("buy_rule") or (
            f"从当前综合排名 Top{strategy_plan.get('top_n', top_n)} 中补足到 "
            f"{strategy_plan.get('top_n', top_n)} 只"
        )
        sell_rule = strategy_plan.get("sell_rule") or (
            f"已有持仓跌出 Top{strategy_plan.get('sell_rank', BUFFER_STRATEGY_SELL_RANK)} 才卖"
        )
        summary_rows.extend(
            [
                ("买入/补位", buy_rule),
                ("卖出规则", sell_rule),
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
  <h1>雪球年榜1000{filter_label}组合综合持仓权重 Top{display_count}</h1>
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


def build_rank_acceleration_email_section_html(result: Dict[str, Any]) -> str:
    def esc(value: Any) -> str:
        return html.escape(str(value if value is not None else ""))

    strategy_plan = result.get("strategy_plan") or {}
    plan_summary = strategy_plan.get("summary") or {}
    rebalance_payload = result.get("rebalance_payload") or {}
    rebalance_response = result.get("rebalance_response") or {}
    comparison_snapshot = result.get("comparison_snapshot") or {}
    target_items = result.get("top_items") or strategy_plan.get("target_items") or []
    status = result.get("status") or "UNKNOWN"
    response_status = rebalance_response.get("status")
    response_id = rebalance_response.get("id")
    response_message = (
        rebalance_response.get("message")
        or rebalance_response.get("error")
        or rebalance_response.get("error_message")
        or "-"
    )
    compare_snapshot_date = (
        comparison_snapshot.get("compare_snapshot_date")
        or strategy_plan.get("compare_snapshot_date")
        or "-"
    )
    fear_greed = strategy_plan.get("fear_greed") or {}
    regime = strategy_plan.get("fear_greed_regime") or "-"
    strategy_config = strategy_plan.get("strategy_config") or {}
    fear_target = safe_int(strategy_config.get("fear_target_count")) or FEAR_GREED_FEAR_TARGET_COUNT
    greed_target = safe_int(strategy_config.get("greed_target_count")) or FEAR_GREED_GREED_TARGET_COUNT
    regime_labels = {
        "volume_bottom": f"量能底（目标{fear_target}只）",
        "volume_top": f"量能顶（目标{greed_target}只）",
        "ma5_bottom": f"MA5底（目标{fear_target}只）",
        "ma5_top": f"MA5顶（目标{greed_target}只）",
        "bottom_both": f"量能+MA5底（目标{fear_target}只）",
        "top_both": f"量能+MA5顶（目标{greed_target}只）",
        "neutral_keep_current": f"中性（维持当前，至少{greed_target}只）",
        "neutral_keep_default": f"中性/空仓（默认{fear_target}只）",
        "missing_fallback": f"缺失（默认{fear_target}只）",
        "invalid_fallback": f"无效（默认{fear_target}只）",
    }
    summary_rows = [
        ("目标组合", f"{result.get('target_cube_symbol') or RANK_ACCELERATION_TARGET_CUBE_SYMBOL} / cube_id={result.get('target_cube_id') or '-'}"),
        ("任务状态", status),
        ("雪球调仓", response_status or ("已跳过" if rebalance_response.get("skipped") else "-")),
        ("雪球调仓ID", response_id or "-"),
        ("5日对比快照", compare_snapshot_date),
        ("恐贪择时", (
            f"{fear_greed.get('score') if fear_greed.get('score') is not None else '-'} "
            f"(log量比z={fear_greed.get('log_volume_z') if fear_greed.get('log_volume_z') is not None else '-'}, "
            f"量比={fear_greed.get('volume_ratio_20d') if fear_greed.get('volume_ratio_20d') is not None else '-'}) "
            f"{regime_labels.get(regime, regime)}"
        )),
        ("目标持仓数", strategy_plan.get("top_n", "-")),
        ("已确认买入候选", len(strategy_plan.get("confirmed_buy_symbols") or [])),
        ("当日买入信号", len(strategy_plan.get("daily_buy_signal_symbols") or [])),
        ("缓冲候选", strategy_plan.get("eligible_retain_count", 0)),
        ("普通退出信号", len(strategy_plan.get("normal_exit_signal_symbols") or [])),
        ("硬退出", "、".join(strategy_plan.get("hard_exit_symbols") or []) or "-"),
        ("持有期保护", "、".join(strategy_plan.get("min_holding_blocked_symbols") or []) or "-"),
        (
            "滚动换仓额度",
            f"此前4个交易日已替换{strategy_plan.get('rolling_prior_replacements', 0)}只；"
            f"本次计划替换{strategy_plan.get('replacement_count', 0)}只",
        ),
        ("目标现金", fmt_number(rebalance_payload.get("cash"), suffix="%")),
        ("买入规则", strategy_plan.get("buy_rule") or "-"),
        ("卖出规则", strategy_plan.get("sell_rule") or "-"),
        ("权重规则", strategy_plan.get("execution_weight_rule") or "-"),
        ("当前持仓", "、".join(plan_summary.get("current") or []) or "-"),
        ("保留", "、".join(plan_summary.get("retained") or []) or "-"),
        ("卖出", "、".join(plan_summary.get("removed") or []) or "-"),
        ("买入", "、".join(plan_summary.get("added") or []) or "-"),
        ("最终持仓", "、".join(plan_summary.get("final") or []) or "-"),
        ("任务提示", response_message),
    ]
    summary_html = "".join(
        f"<tr><th>{esc(label)}</th><td>{esc(value)}</td></tr>"
        for label, value in summary_rows
    )

    target_rows = []
    for index, item in enumerate(target_items, start=1):
        previous_rank = "新进" if item.get("is_new_5d") else (
            f"#{safe_int(item.get('rank_5d_ago'))}"
            if safe_int(item.get("rank_5d_ago")) is not None
            else "-"
        )
        rank_change = safe_int(item.get("acceleration_rank_change_5d"))
        rank_change_text = f"+{rank_change}" if rank_change is not None and rank_change > 0 else str(rank_change or "-")
        holding_change = safe_int(item.get("holding_cube_count_change_5d"))
        holding_change_text = (
            f"+{holding_change}"
            if holding_change is not None and holding_change > 0
            else str(holding_change or "-")
        )
        target_rows.append(
            "<tr>"
            f"<td class=\"num\">{esc(item.get('strategy_rank') or index)}</td>"
            f"<td>{esc(item.get('stock_symbol'))}</td>"
            f"<td>{esc(item.get('stock_name') or '')}</td>"
            f"<td class=\"num\">{esc('#' + str(item.get('composite_rank')) if item.get('composite_rank') else '-')}</td>"
            f"<td class=\"num\">{esc(previous_rank)}</td>"
            f"<td class=\"num\">{esc(rank_change_text)}</td>"
            f"<td class=\"num\">{esc(item.get('holding_cube_count') or '-')}</td>"
            f"<td class=\"num\">{esc(holding_change_text)}</td>"
            f"<td class=\"num\">{esc(fmt_number(item.get('rebalance_weight_pct'), suffix='%'))}</td>"
            f"<td>{esc(item.get('strategy_action') or '')}</td>"
            "</tr>"
        )
    target_table_body = "".join(target_rows) or (
        '<tr><td colspan="10" style="text-align:center;color:#6b7280;">本次没有可展示的目标持仓</td></tr>'
    )
    return f"""
  <hr style="border:0;border-top:2px solid #d1d5db;margin:28px 0;">
  <h1>星澜贰号 · 5日排名加速组合</h1>
  <table class="summary">{summary_html}</table>
  <h2>目标持仓与调仓动作</h2>
  <table>
    <thead>
      <tr>
        <th>加速排名</th><th>股票</th><th>名称</th><th>当前综合排名</th><th>5日前排名</th>
        <th>5日上升</th><th>持仓组合数</th><th>组合数变化</th><th>目标权重</th><th>动作</th>
      </tr>
    </thead>
    <tbody>{target_table_body}</tbody>
  </table>
"""


def append_rank_acceleration_email_section(
    report_html: str,
    result: Optional[Dict[str, Any]],
) -> str:
    if not result:
        return report_html
    section_html = build_rank_acceleration_email_section_html(result)
    closing_tag = "</body>"
    if closing_tag in report_html:
        return report_html.replace(closing_tag, f"{section_html}\n{closing_tag}", 1)
    return f"{report_html}{section_html}"


def build_weight_price_ratio_email_section_html(result: Dict[str, Any]) -> str:
    def esc(value: Any) -> str:
        return html.escape(str(value if value is not None else ""))

    strategy_plan = result.get("strategy_plan") or {}
    plan_summary = strategy_plan.get("summary") or {}
    rebalance_payload = result.get("rebalance_payload") or {}
    rebalance_response = result.get("rebalance_response") or {}
    comparison_snapshot = result.get("comparison_snapshot") or {}
    target_items = result.get("top_items") or strategy_plan.get("target_items") or []
    status = result.get("status") or "UNKNOWN"
    response_status = rebalance_response.get("status")
    response_id = rebalance_response.get("id")
    response_message = (
        rebalance_response.get("message")
        or rebalance_response.get("error")
        or rebalance_response.get("error_message")
        or "-"
    )
    compare_snapshot_date = (
        comparison_snapshot.get("compare_snapshot_date")
        or strategy_plan.get("compare_snapshot_date")
        or "-"
    )
    fear_greed = strategy_plan.get("fear_greed") or {}
    regime = strategy_plan.get("fear_greed_regime") or "-"
    strategy_config = strategy_plan.get("strategy_config") or {}
    fear_target = safe_int(strategy_config.get("fear_target_count")) or FEAR_GREED_FEAR_TARGET_COUNT
    greed_target = safe_int(strategy_config.get("greed_target_count")) or FEAR_GREED_GREED_TARGET_COUNT
    regime_labels = {
        "volume_bottom": f"量能底（目标{fear_target}只）",
        "volume_top": f"量能顶（目标{greed_target}只）",
        "ma5_bottom": f"MA5底（目标{fear_target}只）",
        "ma5_top": f"MA5顶（目标{greed_target}只）",
        "bottom_both": f"量能+MA5底（目标{fear_target}只）",
        "top_both": f"量能+MA5顶（目标{greed_target}只）",
        "neutral_keep_current": f"中性（维持当前，至少{greed_target}只）",
        "neutral_keep_default": f"中性/空仓（默认{fear_target}只）",
        "missing_fallback": f"缺失（默认{fear_target}只）",
        "invalid_fallback": f"无效（默认{fear_target}只）",
    }
    summary_rows = [
        ("目标组合", f"{result.get('target_cube_symbol') or WEIGHT_PRICE_RATIO_TARGET_CUBE_SYMBOL} / cube_id={result.get('target_cube_id') or '-'}"),
        ("任务状态", status),
        ("雪球调仓", response_status or ("已跳过" if rebalance_response.get("skipped") else "-")),
        ("雪球调仓ID", response_id or "-"),
        ("5日对比快照", compare_snapshot_date),
        ("恐贪择时", (
            f"{fear_greed.get('score') if fear_greed.get('score') is not None else '-'} "
            f"(log量比z={fear_greed.get('log_volume_z') if fear_greed.get('log_volume_z') is not None else '-'}, "
            f"量比={fear_greed.get('volume_ratio_20d') if fear_greed.get('volume_ratio_20d') is not None else '-'}) "
            f"{regime_labels.get(regime, regime)}"
        )),
        ("目标持仓数", strategy_plan.get("top_n", "-")),
        ("已确认买入候选", len(strategy_plan.get("confirmed_buy_symbols") or [])),
        ("当日买入信号", len(strategy_plan.get("daily_buy_signal_symbols") or [])),
        ("缓冲候选", strategy_plan.get("eligible_retain_count", 0)),
        ("普通退出信号", len(strategy_plan.get("normal_exit_signal_symbols") or [])),
        ("硬退出", "、".join(strategy_plan.get("hard_exit_symbols") or []) or "-"),
        ("持有期保护", "、".join(strategy_plan.get("min_holding_blocked_symbols") or []) or "-"),
        (
            "滚动换仓额度",
            f"此前4个交易日已替换{strategy_plan.get('rolling_prior_replacements', 0)}只；"
            f"本次计划替换{strategy_plan.get('replacement_count', 0)}只",
        ),
        ("目标现金", fmt_number(rebalance_payload.get("cash"), suffix="%")),
        ("买入规则", strategy_plan.get("buy_rule") or "-"),
        ("卖出规则", strategy_plan.get("sell_rule") or "-"),
        ("权重规则", strategy_plan.get("execution_weight_rule") or "-"),
        ("当前持仓", "、".join(plan_summary.get("current") or []) or "-"),
        ("保留", "、".join(plan_summary.get("retained") or []) or "-"),
        ("卖出", "、".join(plan_summary.get("removed") or []) or "-"),
        ("买入", "、".join(plan_summary.get("added") or []) or "-"),
        ("最终持仓", "、".join(plan_summary.get("final") or []) or "-"),
        ("任务提示", response_message),
    ]
    summary_html = "".join(
        f"<tr><th>{esc(label)}</th><td>{esc(value)}</td></tr>"
        for label, value in summary_rows
    )

    target_rows = []
    for index, item in enumerate(target_items, start=1):
        ratio = safe_float(item.get("weight_price_ratio_5d"))
        weight_multiple = safe_float(item.get("weight_multiple_5d"))
        momentum_multiple = safe_float(item.get("momentum_multiple_5d"))
        holding_change = safe_int(item.get("holding_cube_count_change_5d"))
        holding_change_text = (
            f"+{holding_change}"
            if holding_change is not None and holding_change > 0
            else str(holding_change or "-")
        )
        ratio_text = fmt_number(ratio) if ratio is not None else "-"
        weight_multiple_text = fmt_number(weight_multiple) if weight_multiple is not None else "-"
        momentum_multiple_text = fmt_number(momentum_multiple) if momentum_multiple is not None else "-"
        target_rows.append(
            "<tr>"
            f"<td class=\"num\">{esc(item.get('strategy_rank') or index)}</td>"
            f"<td>{esc(item.get('stock_symbol'))}</td>"
            f"<td>{esc(item.get('stock_name') or '')}</td>"
            f"<td class=\"num\">{esc('#' + str(item.get('composite_rank')) if item.get('composite_rank') else '-')}</td>"
            f"<td class=\"num\">{esc(weight_multiple_text)}</td>"
            f"<td class=\"num\">{esc(momentum_multiple_text)}</td>"
            f"<td class=\"num\">{esc(ratio_text)}</td>"
            f"<td class=\"num\">{esc(item.get('holding_cube_count') or '-')}</td>"
            f"<td class=\"num\">{esc(holding_change_text)}</td>"
            f"<td class=\"num\">{esc(fmt_number(item.get('rebalance_weight_pct'), suffix='%'))}</td>"
            f"<td>{esc(item.get('strategy_action') or '')}</td>"
            "</tr>"
        )
    target_table_body = "".join(target_rows) or (
        '<tr><td colspan="11" style="text-align:center;color:#6b7280;">本次没有可展示的目标持仓</td></tr>'
    )
    return f"""
  <hr style="border:0;border-top:2px solid #d1d5db;margin:28px 0;">
  <h1>星澜叁号 · 5日权价比组合</h1>
  <table class="summary">{summary_html}</table>
  <h2>目标持仓与调仓动作</h2>
  <table>
    <thead>
      <tr>
        <th>权价比排名</th><th>股票</th><th>名称</th><th>当前综合排名</th><th>5日权重倍数</th>
        <th>5日股价倍数</th><th>5日权价比</th><th>持仓组合数</th><th>组合数变化</th><th>目标权重</th><th>动作</th>
      </tr>
    </thead>
    <tbody>{target_table_body}</tbody>
  </table>
"""


def append_weight_price_ratio_email_section(
    report_html: str,
    result: Optional[Dict[str, Any]],
) -> str:
    if not result:
        return report_html
    section_html = build_weight_price_ratio_email_section_html(result)
    closing_tag = "</body>"
    if closing_tag in report_html:
        return report_html.replace(closing_tag, f"{section_html}\n{closing_tag}", 1)
    return f"{report_html}{section_html}"


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


async def execute_rank_acceleration_target_rebalance(
    *,
    cookie: str,
    aggregate: Dict[str, Any],
    current_snapshot_date: date,
    target_cube_symbol: str,
    target_cube_id: Optional[int],
    active_filter_summary: Optional[Dict[str, Any]],
    dry_run: bool,
    timeout: float,
) -> Dict[str, Any]:
    comparison_snapshot = load_xueqiu_rank_comparison_snapshot(
        current_snapshot_date=current_snapshot_date,
        trading_days=RANK_ACCELERATION_COMPARE_TRADING_DAYS,
        active_only=True,
    )
    if not comparison_snapshot.get("available"):
        return {
            "target_cube_symbol": target_cube_symbol,
            "target_cube_id": target_cube_id,
            "status": "SKIPPED",
            "top_items": [],
            "strategy_plan": None,
            "rebalance_payload": None,
            "rebalance_response": {
                "skipped": True,
                "message": (
                    f"缺少{RANK_ACCELERATION_COMPARE_TRADING_DAYS}个交易日前的持仓快照，"
                    "星澜贰号本次不调仓。"
                ),
                "reason": comparison_snapshot.get("reason"),
            },
            "rebalance_skipped": [],
            "comparison_snapshot": comparison_snapshot,
        }

    current_payload = await fetch_target_cube_current_payload(
        cookie=cookie,
        target_cube_symbol=target_cube_symbol,
        timeout=timeout,
    )
    current_holdings, holdings_source = select_current_holdings(current_payload)
    if not holdings_source:
        raise RuntimeError(f"Unexpected target cube holdings payload for {target_cube_symbol}: {current_payload}")
    latest_target_rebalance = current_payload.get("last_rb") if isinstance(current_payload.get("last_rb"), dict) else {}
    resolved_target_cube_id = (
        safe_int(latest_target_rebalance.get("cube_id"))
        or safe_int(current_payload.get("cube_id"))
        or safe_int(target_cube_id)
        or safe_int(os.getenv("XUEQIU_RANK_ACCELERATION_TARGET_CUBE_ID"))
    )
    if not resolved_target_cube_id:
        raise RuntimeError(
            f"Unable to resolve cube_id for rank acceleration target {target_cube_symbol}; "
            "configure XUEQIU_RANK_ACCELERATION_TARGET_CUBE_ID"
        )

    strategy_history = load_rank_acceleration_strategy_history(
        target_cube_symbol=target_cube_symbol,
        current_snapshot_date=current_snapshot_date,
    )
    try:
        fear_greed_snapshot = load_latest_csi_all_share_fear_greed()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to load CSI All Share fear greed for 星澜贰号: %s", exc)
        fear_greed_snapshot = None
    strategy_config = load_xueqiu_strategy_config("rank_acceleration")
    signal_config = load_fear_greed_signal_config()
    strategy_top_n, fear_greed_regime = resolve_xueqiu_strategy_position_target(
        fear_greed_snapshot,
        current_holding_count=len(extract_current_target_holdings(current_holdings)),
        fear_threshold=signal_config["volume_bottom_score"],
        greed_threshold=signal_config["volume_top_score"],
        fear_target_count=strategy_config["fear_target_count"],
        greed_target_count=strategy_config["greed_target_count"],
        fear_volume_std=signal_config["volume_expand_std"],
        greed_volume_std=signal_config["volume_shrink_std"],
        ma5_bottom_score=signal_config["ma5_bottom_score"],
        ma5_top_score=signal_config["ma5_top_score"],
        ma5_lookback_days=signal_config["ma5_lookback_days"],
        default_top_n=RANK_ACCELERATION_TOP_N,
    )
    strategy_sell_rank = max(
        strategy_top_n,
        int(round(strategy_config["sell_rank"] * strategy_top_n / RANK_ACCELERATION_TOP_N)),
    )
    strategy_plan = build_rank_acceleration_buffer_plan(
        ranking=aggregate["ranking"],
        comparison_snapshot=comparison_snapshot,
        current_holdings=current_holdings,
        strategy_history=strategy_history,
        current_snapshot_date=current_snapshot_date,
        top_n=strategy_top_n,
        sell_rank=strategy_sell_rank,
        min_holding_cubes=strategy_config["min_holding_cubes"],
        current_rank_limit=strategy_config["current_rank_limit"],
        holding_cube_increase=strategy_config["holding_cube_increase"],
        min_weight_increase=strategy_config["min_weight_increase"],
        new_entry_rank_limit=strategy_config["new_entry_rank_limit"],
        new_entry_min_cubes=strategy_config["new_entry_min_cubes"],
        hard_exit_rank=strategy_config["hard_exit_rank"],
        hard_exit_min_cubes=strategy_config["hard_exit_min_cubes"],
        sell_confirm_days=strategy_config["sell_confirm_days"],
        min_holding_days=strategy_config["min_holding_days"],
        retain_rank_limit=strategy_config["retain_rank_limit"],
        retain_min_cubes=strategy_config["retain_min_cubes"],
        buy_confirm_prior_days=strategy_config["buy_confirm_prior_days"],
        fear_greed_regime=fear_greed_regime,
    )
    strategy_plan["fear_greed"] = fear_greed_snapshot
    strategy_plan["fear_greed_regime"] = fear_greed_regime
    strategy_plan["configured_top_n"] = RANK_ACCELERATION_TOP_N
    strategy_plan["strategy_config"] = strategy_config
    top_items = strategy_plan["target_items"]
    rebalance_payload = None
    rebalance_response: Dict[str, Any]
    rebalance_skipped_items: List[Dict[str, Any]] = []
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
                "message": "星澜贰号目标标的均不可调仓，已跳过提交。",
                "strategy": strategy_plan.get("strategy_name"),
            }
    else:
        rebalance_response = {
            "skipped": True,
            "message": "星澜贰号目标组合成分未变化，本次不提交调仓。",
            "strategy": strategy_plan.get("strategy_name"),
            "strategy_plan": strategy_plan,
        }

    status = "DRY_RUN" if dry_run else "SUCCESS"
    if rebalance_response.get("skipped"):
        status = "SKIPPED"
    return {
        "target_cube_symbol": target_cube_symbol,
        "target_cube_id": resolved_target_cube_id,
        "status": status,
        "top_items": top_items,
        "strategy_plan": strategy_plan,
        "rebalance_payload": rebalance_payload,
        "rebalance_response": rebalance_response,
        "rebalance_skipped": rebalance_skipped_items,
        "comparison_snapshot": comparison_snapshot,
    }


async def execute_weight_price_ratio_target_rebalance(
    *,
    cookie: str,
    aggregate: Dict[str, Any],
    current_snapshot_date: date,
    target_cube_symbol: str,
    target_cube_id: Optional[int],
    active_filter_summary: Optional[Dict[str, Any]],
    dry_run: bool,
    timeout: float,
) -> Dict[str, Any]:
    """星澜叁号：按 5日权价比（权重倍数÷股价倍数）选股，叠加恐贪择时动态目标仓位。

    与星澜贰号共用同一套缓冲/确认/持有期/滚动换仓框架，只是排序与买入门槛
    换成 weight_price_ratio_5d，目标持仓数按恐贪择时在恐慌10只/贪婪3只间切换。
    """
    comparison_snapshot = load_xueqiu_rank_comparison_snapshot(
        current_snapshot_date=current_snapshot_date,
        trading_days=RANK_ACCELERATION_COMPARE_TRADING_DAYS,
        active_only=True,
    )
    if not comparison_snapshot.get("available"):
        return {
            "target_cube_symbol": target_cube_symbol,
            "target_cube_id": target_cube_id,
            "status": "SKIPPED",
            "top_items": [],
            "strategy_plan": None,
            "rebalance_payload": None,
            "rebalance_response": {
                "skipped": True,
                "message": (
                    f"缺少{RANK_ACCELERATION_COMPARE_TRADING_DAYS}个交易日前的持仓快照，"
                    "星澜叁号本次不调仓。"
                ),
                "reason": comparison_snapshot.get("reason"),
            },
            "rebalance_skipped": [],
            "comparison_snapshot": comparison_snapshot,
        }

    try:
        ratio_by_symbol = load_xueqiu_weight_price_ratio_map(
            active_only=True,
            limit=2000,
            snapshot_date=current_snapshot_date,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to load 5d weight/price ratio map for 星澜叁号: %s", exc)
        return {
            "target_cube_symbol": target_cube_symbol,
            "target_cube_id": target_cube_id,
            "status": "SKIPPED",
            "top_items": [],
            "strategy_plan": None,
            "rebalance_payload": None,
            "rebalance_response": {
                "skipped": True,
                "message": f"5日权价比数据不可用，星澜叁号本次不调仓: {exc}",
                "reason": "weight_price_ratio_unavailable",
            },
            "rebalance_skipped": [],
            "comparison_snapshot": comparison_snapshot,
        }

    ranking_with_ratio: List[Dict[str, Any]] = []
    ratio_item_count = 0
    for item in aggregate.get("ranking") or []:
        enriched = dict(item)
        symbol = normalize_xueqiu_symbol(item.get("stock_symbol"))
        ratio_item = ratio_by_symbol.get(symbol) or {}
        if safe_float(ratio_item.get("weight_price_ratio_5d")) is not None:
            ratio_item_count += 1
        for key in (
            "weight_price_ratio_5d",
            "weight_multiple_5d",
            "momentum_multiple_5d",
            "weight_change_5d",
            "momentum_5d",
            "weight_5d_ago",
        ):
            enriched[key] = ratio_item.get(key)
        ranking_with_ratio.append(enriched)
    if not ratio_item_count:
        return {
            "target_cube_symbol": target_cube_symbol,
            "target_cube_id": target_cube_id,
            "status": "SKIPPED",
            "top_items": [],
            "strategy_plan": None,
            "rebalance_payload": None,
            "rebalance_response": {
                "skipped": True,
                "message": "当前没有可用的5日权价比数据（可能缺少日线行情），星澜叁号本次不调仓。",
                "reason": "weight_price_ratio_empty",
            },
            "rebalance_skipped": [],
            "comparison_snapshot": comparison_snapshot,
        }

    current_payload = await fetch_target_cube_current_payload(
        cookie=cookie,
        target_cube_symbol=target_cube_symbol,
        timeout=timeout,
    )
    current_holdings, holdings_source = select_current_holdings(current_payload)
    if not holdings_source:
        raise RuntimeError(f"Unexpected target cube holdings payload for {target_cube_symbol}: {current_payload}")
    latest_target_rebalance = current_payload.get("last_rb") if isinstance(current_payload.get("last_rb"), dict) else {}
    resolved_target_cube_id = (
        safe_int(latest_target_rebalance.get("cube_id"))
        or safe_int(current_payload.get("cube_id"))
        or safe_int(target_cube_id)
        or safe_int(os.getenv("XUEQIU_WEIGHT_PRICE_RATIO_TARGET_CUBE_ID"))
    )
    if not resolved_target_cube_id:
        raise RuntimeError(
            f"Unable to resolve cube_id for weight-price-ratio target {target_cube_symbol}; "
            "configure XUEQIU_WEIGHT_PRICE_RATIO_TARGET_CUBE_ID"
        )

    try:
        fear_greed_snapshot = load_latest_csi_all_share_fear_greed()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to load CSI All Share fear greed for 星澜叁号: %s", exc)
        fear_greed_snapshot = None
    strategy_config = load_xueqiu_strategy_config("weight_price_ratio")
    signal_config = load_fear_greed_signal_config()
    strategy_top_n, fear_greed_regime = resolve_xueqiu_strategy_position_target(
        fear_greed_snapshot,
        current_holding_count=len(extract_current_target_holdings(current_holdings)),
        fear_threshold=signal_config["volume_bottom_score"],
        greed_threshold=signal_config["volume_top_score"],
        fear_target_count=strategy_config["fear_target_count"],
        greed_target_count=strategy_config["greed_target_count"],
        fear_volume_std=signal_config["volume_expand_std"],
        greed_volume_std=signal_config["volume_shrink_std"],
        ma5_bottom_score=signal_config["ma5_bottom_score"],
        ma5_top_score=signal_config["ma5_top_score"],
        ma5_lookback_days=signal_config["ma5_lookback_days"],
        default_top_n=WEIGHT_PRICE_RATIO_TOP_N,
    )
    strategy_sell_rank = max(
        strategy_top_n,
        int(round(strategy_config["sell_rank"] * strategy_top_n / WEIGHT_PRICE_RATIO_TOP_N)),
    )

    strategy_history = load_rank_acceleration_strategy_history(
        target_cube_symbol=target_cube_symbol,
        current_snapshot_date=current_snapshot_date,
    )
    strategy_plan = build_weight_price_ratio_buffer_plan(
        ranking=ranking_with_ratio,
        comparison_snapshot=comparison_snapshot,
        current_holdings=current_holdings,
        strategy_history=strategy_history,
        current_snapshot_date=current_snapshot_date,
        top_n=strategy_top_n,
        sell_rank=strategy_sell_rank,
        min_holding_cubes=strategy_config["min_holding_cubes"],
        current_rank_limit=strategy_config["current_rank_limit"],
        holding_cube_increase=strategy_config["holding_cube_increase"],
        min_weight_increase=strategy_config["min_weight_increase"],
        new_entry_rank_limit=strategy_config["new_entry_rank_limit"],
        new_entry_min_cubes=strategy_config["new_entry_min_cubes"],
        hard_exit_rank=strategy_config["hard_exit_rank"],
        hard_exit_min_cubes=strategy_config["hard_exit_min_cubes"],
        sell_confirm_days=strategy_config["sell_confirm_days"],
        min_holding_days=strategy_config["min_holding_days"],
        retain_rank_limit=strategy_config["retain_rank_limit"],
        retain_min_cubes=strategy_config["retain_min_cubes"],
        buy_confirm_prior_days=strategy_config["buy_confirm_prior_days"],
        fear_greed_regime=fear_greed_regime,
    )
    strategy_plan["fear_greed"] = fear_greed_snapshot
    strategy_plan["fear_greed_regime"] = fear_greed_regime
    strategy_plan["configured_top_n"] = WEIGHT_PRICE_RATIO_TOP_N
    strategy_plan["ratio_item_count"] = ratio_item_count
    strategy_plan["strategy_config"] = strategy_config

    top_items = strategy_plan["target_items"]
    rebalance_payload = None
    rebalance_response: Dict[str, Any]
    rebalance_skipped_items: List[Dict[str, Any]] = []
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
                "message": "星澜叁号目标标的均不可调仓，已跳过提交。",
                "strategy": strategy_plan.get("strategy_name"),
            }
    else:
        rebalance_response = {
            "skipped": True,
            "message": "星澜叁号目标组合成分未变化，本次不提交调仓。",
            "strategy": strategy_plan.get("strategy_name"),
            "strategy_plan": strategy_plan,
        }

    status = "DRY_RUN" if dry_run else "SUCCESS"
    if rebalance_response.get("skipped"):
        status = "SKIPPED"
    return {
        "target_cube_symbol": target_cube_symbol,
        "target_cube_id": resolved_target_cube_id,
        "status": status,
        "top_items": top_items,
        "strategy_plan": strategy_plan,
        "rebalance_payload": rebalance_payload,
        "rebalance_response": rebalance_response,
        "rebalance_skipped": rebalance_skipped_items,
        "comparison_snapshot": comparison_snapshot,
    }


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
    rank_acceleration_target_cube_symbol: Optional[str] = None,
    rank_acceleration_target_cube_id: Optional[int] = None,
    weight_price_ratio_target_cube_symbol: Optional[str] = None,
    weight_price_ratio_target_cube_id: Optional[int] = None,
    active_rebalance_days: Optional[int] = None,
    sell_rank: int = BUFFER_STRATEGY_SELL_RANK,
    refresh_activity_cache: bool = True,
    use_previous_saved_snapshot: bool = False,
) -> Dict[str, Any]:
    run_at = datetime.now(CHINA_TZ)
    if not force and not is_china_trading_day(run_at.date()):
        message = f"跳过雪球Top持仓统计: {run_at.date().isoformat()} 不是A股交易日"
        logger.info(message)
        return {"skipped": True, "message": message}

    cookie = get_latest_cookie()
    signal_snapshot_date = run_at.date()
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
    active_since = (
        run_at - timedelta(days=active_rebalance_days)
        if active_rebalance_days and active_rebalance_days > 0
        else None
    )
    if use_previous_saved_snapshot:
        signal_snapshot_date, cubes, current_results = load_latest_saved_cube_holdings_snapshot(
            before_date=run_at.date(),
            active_only=bool(active_since),
        )
        logger.info(
            "Using frozen Xueqiu holdings snapshot for rebalance: signal_date=%s execution_date=%s cubes=%s",
            signal_snapshot_date, run_at.date(), len(cubes),
        )
    else:
        current_results = await fetch_all_cube_current(
            cubes,
            cookie=cookie,
            workers=workers,
            timeout=timeout,
            retries=retries,
            active_since=active_since,
            refresh_activity_cache=refresh_activity_cache,
        )
        ensure_xueqiu_current_fetch_quality(current_results, source_count=len(cubes))
    snapshot_results = list(current_results)
    holdings_snapshot_result: Optional[Dict[str, Any]] = None
    if active_rebalance_days and active_rebalance_days > 0 and active_since is not None:
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
        failure_limit = xueqiu_fetch_failure_limit(len(cubes))
        if active_filter_summary["activity_failed_count"] > failure_limit:
            raise RuntimeError(
                "Xueqiu current/activity fetch failed for too many cubes: "
                f"failed={active_filter_summary['activity_failed_count']} "
                f"limit={failure_limit} source={len(cubes)}"
            )
        current_results = [
            result
            for result in current_results
            if result.active or result.error or result.activity_error
        ]
        if not any(not (result.error or result.activity_error) for result in current_results):
            raise RuntimeError(f"No Xueqiu cubes rebalanced within {active_rebalance_days} days.")

    try:
        if use_previous_saved_snapshot:
            holdings_snapshot_result = {
                "table": XUEQIU_CUBE_HOLDINGS_SNAPSHOT_TABLE,
                "snapshot_date": signal_snapshot_date.isoformat(),
                "reused": True,
            }
        else:
            holdings_snapshot_result = save_xueqiu_cube_holdings_snapshots_to_duckdb(
                run_at=run_at,
                current_results=snapshot_results,
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

    results = [
        CubeFetchResult(cube=result.cube, holdings=result.holdings, error=result.error or result.activity_error)
        for result in current_results
    ]
    aggregate = aggregate_holdings(results)
    top_items = add_top_normalized_weights(aggregate["ranking"], top_n)
    rebalance_skipped_items: List[Dict[str, Any]] = []
    rebalance_payload = None
    rebalance_response = None
    strategy_plan: Optional[Dict[str, Any]] = None
    fear_greed_snapshot: Optional[Dict[str, Any]] = None
    resolved_target_cube_id = None
    latest_target_rebalance = None
    rank_acceleration_result: Optional[Dict[str, Any]] = None

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
        try:
            fear_greed_snapshot = load_latest_csi_all_share_fear_greed()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load CSI All Share fear greed, using current Top%s logic: %s", top_n, exc)
            fear_greed_snapshot = None
        strategy_config = load_xueqiu_strategy_config("buffer")
        signal_config = load_fear_greed_signal_config()
        strategy_top_n, fear_greed_regime = resolve_xueqiu_strategy_position_target(
            fear_greed_snapshot,
            current_holding_count=len(extract_current_target_holdings(current_target_holdings)),
            fear_threshold=signal_config["volume_bottom_score"],
            greed_threshold=signal_config["volume_top_score"],
            fear_target_count=strategy_config["fear_target_count"],
            greed_target_count=strategy_config["greed_target_count"],
            fear_volume_std=signal_config["volume_expand_std"],
            greed_volume_std=signal_config["volume_shrink_std"],
            ma5_bottom_score=signal_config["ma5_bottom_score"],
            ma5_top_score=signal_config["ma5_top_score"],
            ma5_lookback_days=signal_config["ma5_lookback_days"],
            default_top_n=top_n,
        )
        strategy_plan = build_equal_top10_top12_buffer_plan(
            ranking=aggregate["ranking"],
            current_holdings=current_target_holdings,
            top_n=strategy_top_n,
            sell_rank=max(safe_int(sell_rank) or BUFFER_STRATEGY_SELL_RANK, strategy_top_n),
            target_total_weight_pct=float(strategy_top_n * 10),
            min_holding_cubes=strategy_config["min_holding_cubes"],
        )
        strategy_plan["fear_greed"] = fear_greed_snapshot
        strategy_plan["fear_greed_regime"] = fear_greed_regime
        strategy_plan["configured_top_n"] = top_n
        strategy_plan["strategy_config"] = strategy_config
        strategy_plan["strategy_name"] = (
            f"中证全指恐贪择时({fear_greed_regime}) + "
            f"{strategy_plan['strategy_name']}"
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
                "message": (
                    f"目标组合成分未变化，按Top{strategy_top_n}等权/跌出Top"
                    f"{strategy_plan['sell_rank']}缓冲策略不提交调仓。"
                ),
                "strategy": strategy_plan.get("strategy_name"),
                "strategy_plan": strategy_plan,
                "active_filter": active_filter_summary,
            }

    if execute_rebalance and rank_acceleration_target_cube_symbol:
        try:
            rank_acceleration_result = await execute_rank_acceleration_target_rebalance(
                cookie=cookie,
                aggregate=aggregate,
                current_snapshot_date=signal_snapshot_date,
                target_cube_symbol=rank_acceleration_target_cube_symbol,
                target_cube_id=rank_acceleration_target_cube_id,
                active_filter_summary=active_filter_summary,
                dry_run=dry_run,
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Rank acceleration target rebalance failed: target=%s",
                rank_acceleration_target_cube_symbol,
            )
            rank_acceleration_result = {
                "target_cube_symbol": rank_acceleration_target_cube_symbol,
                "target_cube_id": rank_acceleration_target_cube_id,
                "status": "FAILED",
                "top_items": [],
                "strategy_plan": None,
                "rebalance_payload": None,
                "rebalance_response": {
                    "skipped": True,
                    "error": str(exc),
                    "message": f"星澜贰号调仓失败: {exc}",
                },
                "rebalance_skipped": [],
            }

    weight_price_ratio_result: Optional[Dict[str, Any]] = None
    if execute_rebalance and weight_price_ratio_target_cube_symbol:
        try:
            weight_price_ratio_result = await execute_weight_price_ratio_target_rebalance(
                cookie=cookie,
                aggregate=aggregate,
                current_snapshot_date=signal_snapshot_date,
                target_cube_symbol=weight_price_ratio_target_cube_symbol,
                target_cube_id=weight_price_ratio_target_cube_id,
                active_filter_summary=active_filter_summary,
                dry_run=dry_run,
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Weight-price-ratio target rebalance failed: target=%s",
                weight_price_ratio_target_cube_symbol,
            )
            weight_price_ratio_result = {
                "target_cube_symbol": weight_price_ratio_target_cube_symbol,
                "target_cube_id": weight_price_ratio_target_cube_id,
                "status": "FAILED",
                "top_items": [],
                "strategy_plan": None,
                "rebalance_payload": None,
                "rebalance_response": {
                    "skipped": True,
                    "error": str(exc),
                    "message": f"星澜叁号调仓失败: {exc}",
                },
                "rebalance_skipped": [],
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
    if rank_acceleration_result:
        secondary_response = rank_acceleration_result.get("rebalance_response") or {}
        secondary_message = (
            f"shared_snapshot={signal_snapshot_date.isoformat()} "
            f"compare_snapshot={(rank_acceleration_result.get('comparison_snapshot') or {}).get('compare_snapshot_date')} "
            f"status={rank_acceleration_result.get('status')} "
            f"target={rank_acceleration_result.get('target_cube_symbol')}"
        )
        secondary_record_id = save_run_record(
            run_at=run_at,
            target_cube_symbol=rank_acceleration_result.get("target_cube_symbol") or RANK_ACCELERATION_TARGET_CUBE_SYMBOL,
            target_cube_id=rank_acceleration_result.get("target_cube_id"),
            status=rank_acceleration_result.get("status") or "FAILED",
            message=secondary_message,
            dry_run=dry_run,
            rank_cache_fetched_at=rank_cache_fetched_at,
            rank_cache_refreshed=rank_cache_refreshed,
            cubes=cubes,
            aggregate=aggregate,
            top_items=rank_acceleration_result.get("top_items") or [],
            rebalance_payload=rank_acceleration_result.get("rebalance_payload"),
            rebalance_response=secondary_response,
        )
        rank_acceleration_result["record_id"] = secondary_record_id
    if weight_price_ratio_result:
        tertiary_response = weight_price_ratio_result.get("rebalance_response") or {}
        tertiary_message = (
            f"shared_snapshot={signal_snapshot_date.isoformat()} "
            f"compare_snapshot={(weight_price_ratio_result.get('comparison_snapshot') or {}).get('compare_snapshot_date')} "
            f"status={weight_price_ratio_result.get('status')} "
            f"target={weight_price_ratio_result.get('target_cube_symbol')}"
        )
        tertiary_record_id = save_run_record(
            run_at=run_at,
            target_cube_symbol=(
                weight_price_ratio_result.get("target_cube_symbol")
                or WEIGHT_PRICE_RATIO_TARGET_CUBE_SYMBOL
            ),
            target_cube_id=weight_price_ratio_result.get("target_cube_id"),
            status=weight_price_ratio_result.get("status") or "FAILED",
            message=tertiary_message,
            dry_run=dry_run,
            rank_cache_fetched_at=rank_cache_fetched_at,
            rank_cache_refreshed=rank_cache_refreshed,
            cubes=cubes,
            aggregate=aggregate,
            top_items=weight_price_ratio_result.get("top_items") or [],
            rebalance_payload=weight_price_ratio_result.get("rebalance_payload"),
            rebalance_response=tertiary_response,
        )
        weight_price_ratio_result["record_id"] = tertiary_record_id
    if not no_email:
        subject_filter = active_filter_compact_label(active_filter_summary)
        subject_display_count = report_display_count(top_n, safe_int((strategy_plan or {}).get("sell_rank")))
        email_report_html = append_rank_acceleration_email_section(
            report_html,
            rank_acceleration_result,
        )
        email_report_html = append_weight_price_ratio_email_section(
            email_report_html,
            weight_price_ratio_result,
        )
        if execute_rebalance and (rank_acceleration_result or weight_price_ratio_result):
            subject = f"雪球年榜1000{subject_filter}多组合自动调仓 - {run_at.strftime('%Y-%m-%d')}"
        elif execute_rebalance:
            subject = (
                f"雪球年榜1000{subject_filter}组合Top{subject_display_count}展示/"
                f"Top{top_n}自动调仓 - {run_at.strftime('%Y-%m-%d')}"
            )
        else:
            subject = (
                f"雪球年榜1000{subject_filter}组合综合持仓权重 "
                f"Top{subject_display_count} - {run_at.strftime('%Y-%m-%d')}"
            )
        send_configured_email(
            "xueqiu_top_holdings_report",
            subject,
            email_report_html,
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
        "signal_snapshot_date": signal_snapshot_date.isoformat(),
        "top": top_items,
        "rank_acceleration_target": rank_acceleration_result,
        "weight_price_ratio_target": weight_price_ratio_result,
        "fear_greed": fear_greed_snapshot,
    }


def process_xueqiu_top_holdings_rebalance_for_robot(
    top_n: int = BUFFER_STRATEGY_TOP_N,
    active_rebalance_days: int = ACTIVE_REBALANCE_LOOKBACK_DAYS,
    sell_rank: int = BUFFER_STRATEGY_SELL_RANK,
) -> str:
    normalized_top_n = max(1, int(top_n or BUFFER_STRATEGY_TOP_N))
    normalized_active_days = max(1, int(active_rebalance_days or ACTIVE_REBALANCE_LOOKBACK_DAYS))
    normalized_sell_rank = max(normalized_top_n, int(sell_rank or BUFFER_STRATEGY_SELL_RANK))
    result = asyncio.run(
        run_top_holdings_job(
            top_n=normalized_top_n,
            execute_rebalance=True,
            dry_run=False,
            no_email=False,
            force=False,
            workers=DEFAULT_WORKERS,
            timeout=DEFAULT_TIMEOUT,
            retries=DEFAULT_RETRIES,
            target_cube_symbol=os.getenv("XUEQIU_TOP_HOLDINGS_TARGET_CUBE_SYMBOL", DEFAULT_TARGET_CUBE_SYMBOL),
            rank_acceleration_target_cube_symbol=os.getenv(
                "XUEQIU_RANK_ACCELERATION_TARGET_CUBE_SYMBOL",
                RANK_ACCELERATION_TARGET_CUBE_SYMBOL,
            ),
            rank_acceleration_target_cube_id=safe_int(
                os.getenv("XUEQIU_RANK_ACCELERATION_TARGET_CUBE_ID")
            ),
            weight_price_ratio_target_cube_symbol=os.getenv(
                "XUEQIU_WEIGHT_PRICE_RATIO_TARGET_CUBE_SYMBOL",
                WEIGHT_PRICE_RATIO_TARGET_CUBE_SYMBOL,
            ),
            weight_price_ratio_target_cube_id=safe_int(
                os.getenv("XUEQIU_WEIGHT_PRICE_RATIO_TARGET_CUBE_ID")
            ),
            active_rebalance_days=normalized_active_days,
            sell_rank=normalized_sell_rank,
            refresh_activity_cache=False,
            use_previous_saved_snapshot=True,
        )
    )
    if result.get("skipped"):
        return str(result.get("message"))
    response = result.get("rebalance_response") or {}
    rank_acceleration = result.get("rank_acceleration_target") or {}
    rank_acceleration_response = rank_acceleration.get("rebalance_response") or {}
    weight_price_ratio = result.get("weight_price_ratio_target") or {}
    weight_price_ratio_response = weight_price_ratio.get("rebalance_response") or {}
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
        f"雪球Top1000主理人活跃{normalized_active_days}天综合持仓自动调仓 "
        f"record_id={result.get('record_id')} "
        f"top_n={normalized_top_n} "
        f"sell_rank={normalized_sell_rank} "
        f"target={result.get('target_cube_symbol')} "
        f"cube_id={result.get('target_cube_id')} "
        f"success={result.get('success_count')} "
        f"failed={result.get('failed_count')} "
        f"stocks={result.get('stock_count')} "
        f"{active_message}"
        f"{snapshot_message}"
        f"rebalance_skipped={response.get('skipped') if isinstance(response, dict) else None} "
        f"rebalance_id={response.get('id') if isinstance(response, dict) else None} "
        f"rebalance_status={response.get('status') if isinstance(response, dict) else None} "
        f"rank_acceleration_target={rank_acceleration.get('target_cube_symbol')} "
        f"rank_acceleration_record_id={rank_acceleration.get('record_id')} "
        f"rank_acceleration_status={rank_acceleration.get('status')} "
        f"rank_acceleration_rebalance_skipped="
        f"{rank_acceleration_response.get('skipped') if isinstance(rank_acceleration_response, dict) else None} "
        f"rank_acceleration_rebalance_id="
        f"{rank_acceleration_response.get('id') if isinstance(rank_acceleration_response, dict) else None} "
        f"weight_price_ratio_target={weight_price_ratio.get('target_cube_symbol')} "
        f"weight_price_ratio_record_id={weight_price_ratio.get('record_id')} "
        f"weight_price_ratio_status={weight_price_ratio.get('status')} "
        f"weight_price_ratio_rebalance_skipped="
        f"{weight_price_ratio_response.get('skipped') if isinstance(weight_price_ratio_response, dict) else None} "
        f"weight_price_ratio_rebalance_id="
        f"{weight_price_ratio_response.get('id') if isinstance(weight_price_ratio_response, dict) else None}"
    )


async def run_top_holdings_cache_refresh_job(
    *,
    limit: int = RANK_TARGET_COUNT,
    workers: int = DEFAULT_WORKERS,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    force_refresh_rank: bool = True,
    force_refresh_activity: bool = False,
    rank_drift_min_overlap_ratio: float = RANK_CACHE_DRIFT_MIN_OVERLAP_RATIO,
    activity_cache_ttl_hours: float = ACTIVE_REBALANCE_CACHE_TTL_HOURS,
    activity_request_min_interval_seconds: float = XUEQIU_ACTIVITY_REQUEST_MIN_INTERVAL_SECONDS,
) -> Dict[str, Any]:
    run_at = datetime.now(CHINA_TZ)
    cookie = get_latest_cookie()
    rank_drift_summary: Dict[str, Any] = {}
    cubes, rank_cache_fetched_at, rank_cache_refreshed = await load_or_refresh_year_top_cubes(
        cookie=cookie,
        force_refresh=force_refresh_rank,
        limit=limit,
        timeout=timeout,
        min_overlap_ratio=rank_drift_min_overlap_ratio,
        drift_summary_out=rank_drift_summary,
    )
    cached_activity = (
        {}
        if force_refresh_activity
        else load_cached_cube_activity(
            cubes,
            min_checked_at=activity_cache_checked_after(activity_cache_ttl_hours),
        )
    )
    previous_activity = load_cached_cube_activity(cubes)
    refresh_cubes = [
        cube
        for cube in cubes
        if cube.symbol not in cached_activity
    ]

    activity_results: List[CubeActivityResult] = []
    if refresh_cubes:
        activity_workers = 1
        headers = build_headers(cookie, referer=XUEQIU_WEB_BASE_URL)
        timeout_config = httpx.Timeout(timeout)
        limits = httpx.Limits(max_connections=activity_workers, max_keepalive_connections=activity_workers)
        activity_pacer = AsyncRequestPacer(
            min_interval_seconds=activity_request_min_interval_seconds,
            jitter_seconds=XUEQIU_ACTIVITY_REQUEST_JITTER_SECONDS,
        )
        logger.info(
            "Refreshing Xueqiu manager activity cache sequentially: refresh=%s cached=%s previous_cache=%s min_interval=%.2fs",
            len(refresh_cubes),
            len(cached_activity),
            len(previous_activity),
            activity_request_min_interval_seconds,
        )
        async with httpx.AsyncClient(headers=headers, timeout=timeout_config, limits=limits) as client:
            for index, cube in enumerate(refresh_cubes, start=1):
                activity_results.append(
                    await fetch_cube_manager_activity(
                        client,
                        cube,
                        retries=retries,
                        previous_activity=previous_activity.get(cube.symbol),
                        request_pacer=activity_pacer,
                    )
                )
                if index % 100 == 0:
                    logger.info(
                        "Refreshed Xueqiu manager activity cache source for %s/%s cubes",
                        index,
                        len(refresh_cubes),
                    )

    saved_activity_count = save_cube_activity_cache(activity_results)
    failed_results = [result for result in activity_results if result.error]

    # Freeze the complete manager holdings after the rank/activity refresh.  The
    # next trading-day rebalance consumes this EOD snapshot and matching close;
    # it must not fetch a new intraday weight snapshot.
    active_since = run_at - timedelta(days=ACTIVE_REBALANCE_LOOKBACK_DAYS)
    current_results = await fetch_all_cube_current(
        cubes,
        cookie=cookie,
        workers=workers,
        timeout=timeout,
        retries=retries,
        active_since=active_since,
        refresh_activity_cache=False,
    )
    ensure_xueqiu_current_fetch_quality(current_results, source_count=len(cubes))
    holdings_snapshot = save_xueqiu_cube_holdings_snapshots_to_duckdb(
        run_at=run_at,
        current_results=current_results,
        active_rebalance_days=ACTIVE_REBALANCE_LOOKBACK_DAYS,
    )

    return {
        "run_at": run_at.isoformat(),
        "rank_cache_fetched_at": rank_cache_fetched_at.isoformat() if rank_cache_fetched_at else None,
        "rank_cache_refreshed": rank_cache_refreshed,
        "source_cube_count": len(cubes),
        "fresh_cache_count": len(cached_activity),
        "refresh_cube_count": len(refresh_cubes),
        "saved_activity_count": saved_activity_count,
        "failed_count": len(failed_results),
        "holdings_snapshot": holdings_snapshot,
        "activity_source": ACTIVE_REBALANCE_ACTIVITY_TYPE,
        "activity_label": ACTIVE_REBALANCE_ACTIVITY_LABEL,
        "activity_cache_ttl_hours": activity_cache_ttl_hours,
        "rank_drift_min_overlap_ratio": rank_drift_min_overlap_ratio,
        "rank_drift": rank_drift_summary,
        "activity_request_min_interval_seconds": activity_request_min_interval_seconds,
        "failed_examples": [
            {
                "symbol": result.symbol,
                "error": result.error,
            }
            for result in failed_results[:10]
        ],
    }


def process_xueqiu_top_holdings_cache_refresh_for_robot(
    rank_limit: int = RANK_TARGET_COUNT,
    rank_drift_min_overlap_pct: float = RANK_CACHE_DRIFT_MIN_OVERLAP_RATIO * 100,
    activity_cache_ttl_hours: float = ACTIVE_REBALANCE_CACHE_TTL_HOURS,
    activity_request_min_interval_ms: int = int(XUEQIU_ACTIVITY_REQUEST_MIN_INTERVAL_SECONDS * 1000),
) -> str:
    normalized_rank_limit = max(RANK_CACHE_MIN_VALID_LIMIT, min(RANK_TARGET_COUNT, int(rank_limit or RANK_TARGET_COUNT)))
    normalized_overlap_ratio = max(0.0, min(1.0, float(rank_drift_min_overlap_pct or 0.0) / 100.0))
    normalized_cache_ttl_hours = max(0.0, float(activity_cache_ttl_hours))
    normalized_interval_seconds = max(0.0, float(activity_request_min_interval_ms or 0) / 1000.0)
    result = asyncio.run(
        run_top_holdings_cache_refresh_job(
            limit=normalized_rank_limit,
            workers=DEFAULT_WORKERS,
            timeout=DEFAULT_TIMEOUT,
            retries=DEFAULT_RETRIES,
            force_refresh_rank=True,
            force_refresh_activity=False,
            rank_drift_min_overlap_ratio=normalized_overlap_ratio,
            activity_cache_ttl_hours=normalized_cache_ttl_hours,
            activity_request_min_interval_seconds=normalized_interval_seconds,
        )
    )
    rank_drift = result.get("rank_drift") or {}
    rank_overlap_message = "rank_overlap=not_checked"
    if rank_drift.get("checked") and rank_drift.get("best_overlap_ratio") is not None:
        rank_overlap_message = (
            f"rank_overlap={float(rank_drift.get('best_overlap_ratio')):.1%} "
            f"rank_overlap_count={rank_drift.get('best_overlap_count')}"
        )
    return (
        f"雪球年榜Top{normalized_rank_limit}榜单和主理人调仓缓存刷新 "
        f"rank_refreshed={result.get('rank_cache_refreshed')} "
        f"overlap_threshold={normalized_overlap_ratio:.0%} "
        f"{rank_overlap_message} "
        f"activity_ttl_hours={normalized_cache_ttl_hours:g} "
        f"source={result.get('source_cube_count')} "
        f"fresh_cache={result.get('fresh_cache_count')} "
        f"refresh={result.get('refresh_cube_count')} "
        f"saved={result.get('saved_activity_count')} "
        f"snapshot_rows={(result.get('holdings_snapshot') or {}).get('saved_rows')} "
        f"failed={result.get('failed_count')}"
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
        save_validated_year_top_cubes(cubes, fetched_at)
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
    parser.add_argument("--no-refresh-activity-cache", action="store_true", help="Use cached manager rebalance activity only.")
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
                refresh_activity_cache=not args.no_refresh_activity_cache,
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

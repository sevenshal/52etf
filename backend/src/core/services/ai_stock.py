"""AI-selected A-share recommendations and an auditable paper portfolio.

The LLM is intentionally the selector: it chooses only from a deterministic
candidate snapshot.  Deterministic code validates that selection and controls
paper-trading risk; it never silently substitutes a factor-ranked stock.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from sqlalchemy import desc

from ..database import (
    AIStockBenchmarkSnapshot,
    AIStockEvaluation,
    AIStockHoldEvaluation,
    AIStockPaperEquity,
    AIStockPaperLot,
    AIStockPaperPortfolio,
    AIStockPaperTrade,
    AIStockRecommendation,
    AIStockRecommendationRun,
    AIStockRecommendationHitRate,
    AIStockServiceConfig,
    AIStockStrategyConfig,
    AIStockTHSIndexCache,
    ETFFearGreedCloneHistory,
    get_db_ctx,
)
from .a_stock_fund_flow import (
    fetch_industry_rank,
    fetch_market_rank,
    fetch_northbound_realtime,
    fetch_stock_fund_flow,
)
from ..duckdb_utils import connect_duckdb
from ..event_stream import publish_event
from .tushare import TushareService


logger = logging.getLogger(__name__)
SHANGHAI_TZ_NAME = "Asia/Shanghai"
PROMPT_VERSION = "news-ths-v8"  # v8: 新闻窗口固定为上一交易日14:00起并按时间升序，提升跨批前缀缓存命中
DEFAULT_MODEL = "deepseek-chat"
CODE_PATTERN = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")

COMMISSION_RATE = 0.00025
TRANSFER_RATE = 0.00001
SELL_STAMP_DUTY_RATE = 0.0005
MIN_COMMISSION = 5.0
MAX_CANDIDATES = 1500
MAX_RECOMMENDATIONS = 10
MIN_LISTING_DAYS = 183
TARGET_RETURN_PCT_MIN = 5.0
TARGET_RETURN_PCT_MAX = 10.0
NEWS_SIGNAL_WEIGHT = 0.5  # 纯新闻信号在排序中的权重，0 = 关闭（回到纯 AI 信心排序）
XUEQIU_SIGNAL_ENABLED = 0  # 雪球活跃组合行为信号(xueqiu块)是否喂给 AI 选股：0=关（默认，先观察），1=开
XUEQIU_WEIGHT_GAIN_CLIP = (0.2, 10.0)  # 权重5日升速(倍数)裁剪区间；极端值(如新进小权重被大量买入)会爆到几百倍
XUEQIU_RATIO_CLIP = (0.0, 10.0)  # 权价比(权重升速÷价格升速)裁剪上限
MAX_EVENTS = 8
MAX_BOARDS = 8
MAX_CANDIDATES_PER_BOARD = 200
MIN_MARKET_CAP = 1_000_000  # 万元 = 100 亿
MIN_AVG_TURNOVER = 20_000  # 千元 = 2000 万
THS_INDEX_TYPES = ("N", "TH", "I")


def _load_strategy_params() -> Dict[str, int]:
    """Return configurable limits from DB, falling back to module constants."""
    try:
        with get_db_ctx() as db:
            config = db.get(AIStockServiceConfig, 1)
            max_c = config.max_candidates if config and config.max_candidates is not None else MAX_CANDIDATES
            max_e = config.max_events if config and config.max_events is not None else MAX_EVENTS
            max_b = config.max_boards if config and config.max_boards is not None else MAX_BOARDS
            max_cpb = config.max_candidates_per_board if config and config.max_candidates_per_board is not None else MAX_CANDIDATES_PER_BOARD
            min_mc = config.min_market_cap if config and config.min_market_cap is not None else MIN_MARKET_CAP
            min_to = config.min_avg_turnover if config and config.min_avg_turnover is not None else MIN_AVG_TURNOVER
            max_r = config.max_recommendations if config and config.max_recommendations is not None else MAX_RECOMMENDATIONS
            min_ld = config.min_listing_days if config and config.min_listing_days is not None else MIN_LISTING_DAYS
            tr_min = config.target_return_pct_min if config and config.target_return_pct_min is not None else TARGET_RETURN_PCT_MIN
            tr_max = config.target_return_pct_max if config and config.target_return_pct_max is not None else TARGET_RETURN_PCT_MAX
            nsw = config.news_signal_weight if config and config.news_signal_weight is not None else NEWS_SIGNAL_WEIGHT
            xse = config.xueqiu_signal_enabled if config and config.xueqiu_signal_enabled is not None else XUEQIU_SIGNAL_ENABLED
        return {
            "max_candidates": max_c, "max_events": max_e, "max_boards": max_b,
            "max_candidates_per_board": max_cpb, "min_market_cap": min_mc, "min_avg_turnover": min_to,
            "max_recommendations": max_r, "min_listing_days": min_ld,
            "target_return_pct_min": tr_min, "target_return_pct_max": tr_max,
            "news_signal_weight": nsw,
            "xueqiu_signal_enabled": xse,
        }
    except Exception:
        return {
            "max_candidates": MAX_CANDIDATES, "max_events": MAX_EVENTS, "max_boards": MAX_BOARDS,
            "max_candidates_per_board": MAX_CANDIDATES_PER_BOARD, "min_market_cap": MIN_MARKET_CAP, "min_avg_turnover": MIN_AVG_TURNOVER,
            "max_recommendations": MAX_RECOMMENDATIONS, "min_listing_days": MIN_LISTING_DAYS,
            "target_return_pct_min": TARGET_RETURN_PCT_MIN, "target_return_pct_max": TARGET_RETURN_PCT_MAX,
            "news_signal_weight": NEWS_SIGNAL_WEIGHT,
            "xueqiu_signal_enabled": XUEQIU_SIGNAL_ENABLED,
        }


def _json_safe(value: Any) -> Any:
    """Recursively convert date/datetime to ISO strings for JSON embedding."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


class AIStockError(RuntimeError):
    pass


class AIStockConfigurationError(AIStockError):
    pass


class AIStockModelError(AIStockError):
    pass


def get_ai_stock_service_settings() -> Dict[str, Any]:
    """Return redacted integration settings from a short SQLite read."""
    with get_db_ctx() as db:
        config = db.get(AIStockServiceConfig, 1)
        return {
            "deepseek_configured": bool(config and config.deepseek_api_key),
            "deepseek_model": (config.deepseek_model if config else None) or DEFAULT_MODEL,
            "deepseek_base_url": (config.deepseek_base_url if config else None) or "https://api.deepseek.com",
            "max_candidates": config.max_candidates if config and config.max_candidates is not None else MAX_CANDIDATES,
            "max_events": config.max_events if config and config.max_events is not None else MAX_EVENTS,
            "max_boards": config.max_boards if config and config.max_boards is not None else MAX_BOARDS,
            "max_candidates_per_board": config.max_candidates_per_board if config and config.max_candidates_per_board is not None else MAX_CANDIDATES_PER_BOARD,
            "min_market_cap": config.min_market_cap if config and config.min_market_cap is not None else MIN_MARKET_CAP,
            "min_avg_turnover": config.min_avg_turnover if config and config.min_avg_turnover is not None else MIN_AVG_TURNOVER,
            "max_recommendations": config.max_recommendations if config and config.max_recommendations is not None else MAX_RECOMMENDATIONS,
            "min_listing_days": config.min_listing_days if config and config.min_listing_days is not None else MIN_LISTING_DAYS,
            "target_return_pct_min": config.target_return_pct_min if config and config.target_return_pct_min is not None else TARGET_RETURN_PCT_MIN,
            "target_return_pct_max": config.target_return_pct_max if config and config.target_return_pct_max is not None else TARGET_RETURN_PCT_MAX,
            "news_signal_weight": config.news_signal_weight if config and config.news_signal_weight is not None else NEWS_SIGNAL_WEIGHT,
            "xueqiu_signal_enabled": config.xueqiu_signal_enabled if config and config.xueqiu_signal_enabled is not None else XUEQIU_SIGNAL_ENABLED,
            "updated_at": config.updated_at if config else None,
            "updated_by": config.updated_by if config else None,
        }


def _load_ai_stock_service_config_for_runtime() -> Dict[str, Optional[str]]:
    """Read the write-only key for a server-side model invocation only."""
    with get_db_ctx() as db:
        config = db.get(AIStockServiceConfig, 1)
        return {
            "deepseek_api_key": config.deepseek_api_key if config else None,
            "deepseek_model": config.deepseek_model if config else None,
            "deepseek_base_url": config.deepseek_base_url if config else None,
        }


def update_ai_stock_service_settings(
    *,
    deepseek_api_key: Optional[str],
    deepseek_model: Optional[str],
    updated_by: str,
    max_candidates: Optional[int] = None,
    max_events: Optional[int] = None,
    max_boards: Optional[int] = None,
    max_candidates_per_board: Optional[int] = None,
    min_market_cap: Optional[int] = None,
    min_avg_turnover: Optional[int] = None,
    max_recommendations: Optional[int] = None,
    min_listing_days: Optional[int] = None,
    target_return_pct_min: Optional[float] = None,
    target_return_pct_max: Optional[float] = None,
    news_signal_weight: Optional[float] = None,
    xueqiu_signal_enabled: Optional[int] = None,
) -> Dict[str, Any]:
    """Persist write-only DeepSeek key and strategy limits in one short transaction."""
    key = str(deepseek_api_key or "").strip()
    model = str(deepseek_model or "").strip()
    if deepseek_api_key is not None and key and len(key) > 512:
        raise ValueError("DeepSeek API Key 不能超过 512 个字符")
    if model and len(model) > 100:
        raise ValueError("模型名称不能超过 100 个字符")
    for name, value in [("max_candidates", max_candidates), ("max_events", max_events), ("max_boards", max_boards), ("max_candidates_per_board", max_candidates_per_board), ("min_market_cap", min_market_cap), ("min_avg_turnover", min_avg_turnover), ("max_recommendations", max_recommendations), ("min_listing_days", min_listing_days)]:
        if value is not None and (not isinstance(value, int) or value < 1 or value > 100_000_000):
            raise ValueError(f"{name} 必须是 1-100000000 的整数")
    for name, value in [("target_return_pct_min", target_return_pct_min), ("target_return_pct_max", target_return_pct_max)]:
        if value is not None and (not isinstance(value, (int, float)) or value < 0 or value > 100):
            raise ValueError(f"{name} 必须是 0-100 的百分比")
    if target_return_pct_min is not None and target_return_pct_max is not None and target_return_pct_min > target_return_pct_max:
        raise ValueError("目标收益下限不能大于上限")
    if news_signal_weight is not None and (not isinstance(news_signal_weight, (int, float)) or news_signal_weight < 0 or news_signal_weight > 1):
        raise ValueError("新闻信号权重必须是 0-1 之间的比例")
    if xueqiu_signal_enabled is not None and xueqiu_signal_enabled not in (0, 1):
        raise ValueError("雪球行为信号开关必须是 0 或 1")
    with get_db_ctx() as db:
        config = db.get(AIStockServiceConfig, 1)
        if not config:
            config = AIStockServiceConfig(id=1)
            db.add(config)
        if deepseek_api_key is not None:
            config.deepseek_api_key = key or None
        if model:
            config.deepseek_model = model
        if max_candidates is not None:
            config.max_candidates = max_candidates
        if max_events is not None:
            config.max_events = max_events
        if max_boards is not None:
            config.max_boards = max_boards
        if max_candidates_per_board is not None:
            config.max_candidates_per_board = max_candidates_per_board
        if min_market_cap is not None:
            config.min_market_cap = min_market_cap
        if min_avg_turnover is not None:
            config.min_avg_turnover = min_avg_turnover
        if max_recommendations is not None:
            config.max_recommendations = max_recommendations
        if min_listing_days is not None:
            config.min_listing_days = min_listing_days
        if target_return_pct_min is not None:
            config.target_return_pct_min = target_return_pct_min
        if target_return_pct_max is not None:
            config.target_return_pct_max = target_return_pct_max
        if news_signal_weight is not None:
            config.news_signal_weight = news_signal_weight
        if xueqiu_signal_enabled is not None:
            config.xueqiu_signal_enabled = xueqiu_signal_enabled
        config.updated_by = updated_by
    return get_ai_stock_service_settings()


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return default
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _round(value: Any, digits: int = 2) -> Optional[float]:
    value = _safe_float(value)
    return round(value, digits) if value is not None else None


def _clamp(value: Any, lower: float, upper: float, default: float) -> float:
    value = _safe_float(value, default)
    return max(lower, min(upper, value if value is not None else default))


def _normalize_ts_code(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if raw.startswith(("SH", "SZ", "BJ")) and len(raw) == 8:
        raw = f"{raw[2:]}.{raw[:2]}"
    if "." not in raw and raw.isdigit() and len(raw) == 6:
        if raw.startswith(("6", "9")):
            raw = f"{raw}.SH"
        elif raw.startswith(("4", "8")):
            raw = f"{raw}.BJ"
        else:
            raw = f"{raw}.SZ"
    return raw


def _now(now: Optional[datetime] = None) -> datetime:
    return now.replace(tzinfo=None) if now and now.tzinfo else (now or datetime.now())


def _run_type_for_time(now: datetime) -> str:
    minute = now.hour * 60 + now.minute
    if minute <= 9 * 60 + 30:
        return "PREOPEN"
    if minute <= 9 * 60 + 45:
        return "OPENING"
    return "INTRADAY"


def _is_market_session(now: datetime) -> bool:
    minute = now.hour * 60 + now.minute
    return (9 * 60 + 31 <= minute <= 11 * 60 + 30) or (13 * 60 <= minute <= 14 * 60 + 57)


def _is_recommendation_window(now: datetime) -> bool:
    minute = now.hour * 60 + now.minute
    return now.weekday() < 5 and ((9 * 60 + 15 <= minute <= 9 * 60 + 30) or _is_market_session(now))


def _serialize_news_headlines(*frames: Tuple[str, Any]) -> List[Dict[str, Any]]:
    """新闻标题归一化：按标题精确去重（保留最早时间、合并来源），不再输出 kind 字段。

    输入仍按 (kind, frame) 分组以支持多来源；输出只保留模型需要的字段。
    """
    rows = []
    for _news_kind, frame in frames:
        if frame is None or frame.empty:
            continue
        for _, row in frame.iterrows():
            title = str(row.get("title") or "").strip()
            if not title:
                continue
            rows.append(
                {
                    "time": row.get("datetime").isoformat() if hasattr(row.get("datetime"), "isoformat") else str(row.get("datetime") or ""),
                    "source": str(row.get("source") or "").strip(),
                    "title": title,
                }
            )
    rows.sort(key=lambda item: (item["time"], item["source"], item["title"]))
    # 标题完全相同的合并：按 time 升序排序后第一条即最早，来源用 | 连接去重
    deduped: List[Dict[str, Any]] = []
    index_by_title: Dict[str, int] = {}
    for item in rows:
        title = item["title"]
        existing = index_by_title.get(title)
        if existing is not None:
            target = deduped[existing]
            if item["source"] and item["source"] not in target["source"].split("|"):
                target["source"] = f"{target['source']}|{item['source']}" if target["source"] else item["source"]
            continue
        index_by_title[title] = len(deduped)
        deduped.append(item)
    for index, item in enumerate(deduped, start=1):
        item["headline_id"] = f"N{index:04d}"
    return deduped


def _validated_events(raw_response: Dict[str, Any], headlines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    headline_ids = {item["headline_id"] for item in headlines}
    accepted: List[Dict[str, Any]] = []
    seen = set()
    for item in raw_response.get("events") or []:
        if not isinstance(item, dict):
            continue
        term = str(item.get("hotword") or item.get("term") or "").strip()
        evidence_ids = [str(value).strip() for value in item.get("headline_ids") or []]
        evidence_ids = [value for value in evidence_ids if value in headline_ids]
        if len(term) < 2 or len(term) > 32 or term in seen or not evidence_ids:
            continue
        seen.add(term)
        aliases = []
        for value in item.get("aliases") or item.get("board_keywords") or []:
            text = str(value or "").strip()
            if 2 <= len(text) <= 32 and text not in aliases:
                aliases.append(text)
        accepted.append(
            {
                "hotword": term,
                "score": _clamp(item.get("score"), 0.0, 100.0, 0.0),
                "headline_ids": evidence_ids[:8],
                "aliases": aliases[:8],
                "direction": str(item.get("direction") or "中性")[:16],
                "rationale": str(item.get("rationale") or "")[:500],
                # 文本内在属性：tone/quantitative/ambiguity 不需要股票特征即可判断，
                # text_surprise 是"标题本身出乎常识的程度"，与个股/板块特征无关
                "tone": str(item.get("tone") or "neutral")[:16],
                "quantitative": _clamp(item.get("quantitative"), 0.0, 100.0, 0.0),
                "ambiguity": _clamp(item.get("ambiguity"), 0.0, 100.0, 0.0),
                "text_surprise": _clamp(item.get("text_surprise"), 0.0, 100.0, 0.0),
            }
        )
    accepted = sorted(accepted, key=lambda item: (-item["score"], item["hotword"]))[:_load_strategy_params()["max_events"]]
    for index, item in enumerate(accepted, start=1):
        item["event_id"] = f"E{index:02d}"
    return accepted


# Kept as a compatibility helper for older unit callers; V3 itself uses events.
def _validated_hotwords(raw_response: Dict[str, Any], headlines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return _validated_events({"events": raw_response.get("hotwords") or []}, headlines)


def _is_candidate_eligible(candidate: Dict[str, Any], min_listing_days: Optional[int] = None) -> bool:
    code = _normalize_ts_code(candidate.get("ts_code"))
    name = str(candidate.get("name") or "")
    price = _safe_float(candidate.get("price"), 0.0) or 0.0
    listing_min = min_listing_days if min_listing_days is not None else MIN_LISTING_DAYS
    return bool(
        CODE_PATTERN.fullmatch(code)
        and price > 0
        and "ST" not in name.upper()
        and not candidate.get("is_suspended")
        and candidate.get("data_fresh") is not False
        and (_safe_float(candidate.get("listing_days"), 0.0) or 0.0) >= listing_min
    )


def _listing_days(value: Any, as_of: date) -> Optional[int]:
    """Normalize Tushare's listing date variants to an auditable age in days."""
    if isinstance(value, datetime):
        listed_on = value.date()
    elif isinstance(value, date):
        listed_on = value
    else:
        raw = str(value or "").strip()
        listed_on = None
        for pattern in ("%Y-%m-%d", "%Y%m%d"):
            try:
                listed_on = datetime.strptime(raw[:10], pattern).date()
                break
            except ValueError:
                continue
    return max(0, (as_of - listed_on).days) if listed_on and listed_on <= as_of else None


def _window_features(rows: List[Tuple[Optional[float], Optional[float], Optional[float]]]) -> Dict[str, Any]:
    """Trailing-window features from ascending daily bars.

    rows: [(close, pct_chg, turnover_rate)] ordered by trade_date asc.
    Returns mom_5d/mom_20d/mom_60d (percent returns), volatility_20d
    (annualized percent), price_position (0-1 within the 20d range) and
    turnover_avg_20d (percent).  Missing windows yield None values.
    """
    n = len(rows)
    if n < 2:
        return {}
    closes = [row[0] for row in rows]
    pct = [row[1] for row in rows]
    turn = [row[2] for row in rows]
    last_close = closes[-1] if closes[-1] else 0.0
    feat: Dict[str, Any] = {}

    def momentum(window: int) -> Optional[float]:
        idx = n - 1 - window
        base = closes[idx] if 0 <= idx < n else None
        if base and base > 0 and last_close > 0:
            return round((last_close / base - 1) * 100, 2)
        return None

    feat["mom_5d"] = momentum(5)
    feat["mom_20d"] = momentum(20)
    feat["mom_60d"] = momentum(60)

    valid_pct = [v for v in pct[-20:] if v is not None]
    if len(valid_pct) >= 5:
        mean = sum(valid_pct) / len(valid_pct)
        var = sum((v - mean) ** 2 for v in valid_pct) / (len(valid_pct) - 1)
        feat["volatility_20d"] = round(math.sqrt(var) * math.sqrt(252), 2)
    else:
        feat["volatility_20d"] = None

    win = [v for v in closes[-20:] if v is not None and v > 0]
    if len(win) >= 2:
        lo, hi = min(win), max(win)
        feat["price_position"] = round((last_close - lo) / (hi - lo), 3) if hi > lo else 0.5
    else:
        feat["price_position"] = None

    valid_turn = [v for v in turn[-20:] if v is not None]
    feat["turnover_avg_20d"] = round(sum(valid_turn) / len(valid_turn), 3) if valid_turn else None
    return feat


def _news_signal_score(candidate: Dict[str, Any]) -> float:
    """Pure-news signal 0-100 (The Inefficient Pricing of News, NBER w35093).

    Combines LLM text attributes (tone / quantitative / ambiguity /
    text_surprise) with a data-computed characteristic residual (momentum /
    price position).  Underreaction setups — negative or neutral tone with
    concrete numbers, price sitting low in its 20d range — score high
    (market over-pessimism, drift opportunity).  Overreaction setups — vague
    high-attention topics on extended momentum — score low (chase risk).
    Neutral 50 when the candidate has no linked news.
    """
    events = candidate.get("events") or []
    if not events:
        return 50.0
    scores = []
    for ev in events:
        tone = str(ev.get("tone") or "neutral").lower()
        quantitative = _clamp(ev.get("quantitative"), 0.0, 100.0, 0.0) / 100.0
        ambiguity = _clamp(ev.get("ambiguity"), 0.0, 100.0, 0.0) / 100.0
        text_surprise = _clamp(ev.get("text_surprise"), 0.0, 100.0, 0.0) / 100.0
        tone_bonus = 30.0 if tone == "negative" else (10.0 if tone == "neutral" else -10.0)
        under_score = 50.0 + text_surprise * 30.0 + quantitative * 20.0 + tone_bonus
        over_penalty = ambiguity * 25.0
        mom20 = _safe_float(candidate.get("mom_20d"))
        position = _safe_float(candidate.get("price_position"))
        if mom20 is not None and mom20 > 15:
            # extended momentum: news is largely expected content, not shock
            over_penalty += min(25.0, (mom20 - 15) * 1.0)
        if position is not None and position < 0.3 and tone == "negative":
            # low price position + negative news -> market already pessimistic
            under_score += 10.0
        scores.append(max(0.0, min(100.0, under_score - over_penalty)))
    if not scores:
        return 50.0
    return round(sum(scores) / len(scores), 1)


def _execution_score(candidate: Dict[str, Any]) -> float:
    """A transparent execution score; it does not choose the recommended stock."""
    change = _clamp(candidate.get("change_pct"), -10.0, 10.0, 0.0)
    money = _safe_float(candidate.get("main_net"), 0.0) or 0.0
    turnover = _safe_float(candidate.get("turnover"), 0.0) or 0.0
    score = 55.0 + change * 1.5 + min(15.0, max(-8.0, money / 100_000_000 * 4.0))
    if turnover > 0:
        score += min(8.0, turnover / 2.0)
    return round(_clamp(score, 0.0, 100.0, 0.0), 3)


def _load_xueqiu_behavior_map(limit: int = 2000) -> Dict[str, Dict[str, Any]]:
    """Latest Xueqiu active-portfolio behavior snapshot, keyed by A-share ts_code.

    Reuses the same analytics query the 星澜 robots rely on
    (``load_xueqiu_top_holdings_latest``); lazy-import avoids any import-time
    coupling between the AI-stock service and the robot module.
    """
    from ...app.api.xueqiu_holdings import load_xueqiu_top_holdings_latest

    latest = load_xueqiu_top_holdings_latest(active_only=True, limit=limit)
    by_ts: Dict[str, Dict[str, Any]] = {}
    for item in latest.get("items") or []:
        match = re.fullmatch(r"(SH|SZ|BJ)\.(\d{6})", str(item.get("stock_symbol") or ""))
        if match:
            by_ts[f"{match.group(2)}.{match.group(1)}"] = item
    return by_ts


def _clamp_multiple(value: Any, bounds: Tuple[float, float]) -> Optional[float]:
    """Round a positive multiple, clipping extreme values to ``bounds``; None stays None."""
    number = _safe_float(value)
    if number is None:
        return None
    return round(max(bounds[0], min(bounds[1], number)), 3)


def _safe_int_value(value: Any) -> Optional[int]:
    """int() wrapper that keeps None/blank as None (for optional 5-day-change fields)."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _attach_xueqiu_behavior(candidates: List[Dict[str, Any]]) -> None:
    """Attach the xueqiu behavior block to candidates in the current Xueqiu snapshot.

    Candidates absent from the snapshot get no block at all.  The block carries
    current levels (weight/rank/cube_count) plus pre-computed 5-day changes and the
    deterministic direction label (computed by ``load_xueqiu_top_holdings_latest``);
    the 5-day-change fields are None for new entries (not held 5 trading days ago), which
    the prompt guidance explains explicitly.  Any snapshot outage only skips the
    enrichment (the recommendation itself must never depend on this data).
    """
    try:
        behavior_map = _load_xueqiu_behavior_map()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Xueqiu behavior snapshot unavailable, skipping xueqiu enrichment: %s", exc)
        return
    for candidate in candidates:
        item = behavior_map.get(candidate.get("ts_code"))
        if not item:
            continue
        weight_gain = _clamp_multiple(item.get("weight_multiple_5d"), XUEQIU_WEIGHT_GAIN_CLIP)
        price_gain = _round(item.get("momentum_multiple_5d"), 3)
        ratio = _clamp_multiple(item.get("weight_price_ratio_5d"), XUEQIU_RATIO_CLIP)
        cube_count = _safe_int_value(item.get("holding_cube_count"))
        cube_count_5d_ago = _safe_int_value(item.get("cube_count_5d_ago"))
        cube_gain = cube_count - cube_count_5d_ago if (cube_count is not None and cube_count_5d_ago is not None) else None
        candidate["xueqiu"] = {
            "weight": _round(item.get("composite_weight_pct"), 2),
            "rank": _safe_int_value(item.get("composite_rank")),
            "cube_count": cube_count,
            "weight_gain_5d": weight_gain,
            "price_gain_5d": price_gain,
            "rank_up_5d": _safe_int_value(item.get("rank_change_5d")),
            "cube_gain_5d": cube_gain,
            "ratio_5d": ratio,
            "direction": item.get("direction") or "持平",
        }


class AIStockDataProvider:
    """Collects strict, reproducible V3 news/THS snapshots outside SQLite work."""

    def __init__(self, tushare: Optional[TushareService] = None):
        self.tushare = tushare or TushareService.get_instance()

    def previous_trading_day_news_anchor(self, now: datetime) -> datetime:
        """Return 14:00 on the latest open market day strictly before ``now``.

        A fixed daily anchor keeps the ordered news list prefix stable across
        recommendation batches.  Unlike a rolling 24-hour window, earlier
        headlines do not fall off and renumber every time the job runs.
        """
        as_of = _now(now)
        end_date = as_of.date() - timedelta(days=1)
        frame = self.tushare.get_trade_calendar_frame(end_date - timedelta(days=14), end_date)
        if frame is None or frame.empty:
            raise AIStockError("Tushare trade_cal 不可用，不能确定新闻窗口起点")
        open_days = [
            row.get("cal_date")
            for _, row in frame.iterrows()
            if str(row.get("is_open")) in {"1", "1.0"} and row.get("cal_date") and row.get("cal_date") <= end_date
        ]
        if not open_days:
            raise AIStockError("最近没有可用的上一交易日，不能确定新闻窗口起点")
        return datetime.combine(max(open_days), datetime.min.time()).replace(hour=14)

    def build_news_snapshot(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        as_of = _now(now)
        news_anchor = self.previous_trading_day_news_anchor(as_of)
        major_news_frame = self.tushare.get_a_stock_major_news_frame(news_anchor, as_of)
        if major_news_frame is None or major_news_frame.empty:
            raise AIStockError("Tushare major_news 未返回上一交易日14:00以来的通讯标题，严格模式停止本批次")
        headlines = _serialize_news_headlines(("major_news", major_news_frame))
        if not headlines:
            raise AIStockError("新闻接口返回内容没有有效标题，严格模式停止本批次")
        return {
            "generated_at": as_of.isoformat(timespec="seconds"),
            "trade_date": as_of.date().isoformat(),
            "headlines": headlines,
            "source_status": {
                "major_news": {
                    "status": "SUCCESS",
                    "headline_count": int(len(major_news_frame)),
                    "window_start": news_anchor.isoformat(timespec="seconds"),
                    "window_end": as_of.isoformat(timespec="seconds"),
                },
                "headline_count": len(headlines),
                "headline_raw_count": int(len(major_news_frame)),
            },
        }

    def ths_index_catalog(self) -> Dict[str, Any]:
        cutoff = _now() - timedelta(hours=24)
        with get_db_ctx() as db:
            rows = db.query(AIStockTHSIndexCache).filter(AIStockTHSIndexCache.fetched_at >= cutoff).all()
            cached = [
                {"ts_code": row.ts_code, "name": row.name, "count": row.constituent_count, "exchange": row.exchange, "list_date": row.list_date.isoformat() if row.list_date else None, "type": row.index_type}
                for row in rows
            ]
            cached_fetched_at = max((row.fetched_at for row in rows), default=None)
        if {item["type"] for item in cached} >= set(THS_INDEX_TYPES):
            return {"items": cached, "cached": True, "fetched_at": cached_fetched_at.isoformat(timespec="seconds") if cached_fetched_at else _now().isoformat(timespec="seconds")}

        fetched: List[Dict[str, Any]] = []
        for index_type in THS_INDEX_TYPES:
            frame = self.tushare.get_ths_index_frame(index_type)
            if frame is None or frame.empty:
                raise AIStockError(f"Tushare ths_index({index_type}) 不可用，严格模式停止本批次")
            for _, row in frame.iterrows():
                fetched.append({
                    "ts_code": str(row.get("ts_code") or "").upper(), "name": str(row.get("name") or ""),
                    "count": int(_safe_float(row.get("count"), 0.0) or 0), "exchange": str(row.get("exchange") or "A"),
                    "list_date": row.get("list_date"), "type": index_type,
                })
        fetched = [item for item in fetched if item["ts_code"] and item["name"]]
        if not fetched:
            raise AIStockError("THS 指数目录为空，严格模式停止本批次")
        with get_db_ctx() as db:
            db.query(AIStockTHSIndexCache).delete()
            fetched_at = _now()
            for item in fetched:
                db.add(AIStockTHSIndexCache(index_type=item["type"], ts_code=item["ts_code"], name=item["name"], constituent_count=item["count"], exchange=item["exchange"], list_date=item["list_date"], fetched_at=fetched_at))
        return {"items": fetched, "cached": False, "fetched_at": _now().isoformat(timespec="seconds")}

    def latest_completed_market_date(self, now: datetime) -> date:
        frame = self.tushare.get_trade_calendar_frame(now.date() - timedelta(days=14), now.date())
        if frame is None or frame.empty:
            raise AIStockError("Tushare trade_cal 不可用，不能确定板块强弱基准日")
        include_today = now.hour * 60 + now.minute >= 15 * 60 + 10
        open_days = [row.get("cal_date") for _, row in frame.iterrows() if str(row.get("is_open")) in {"1", "1.0"} and (row.get("cal_date") < now.date() or (include_today and row.get("cal_date") == now.date()))]
        if not open_days:
            raise AIStockError("没有可用的已完成交易日")
        return max(open_days)

    def build_candidate_snapshot(self, now: datetime, events: List[Dict[str, Any]], board_mappings: List[Dict[str, Any]], catalog: Dict[str, Any]) -> Dict[str, Any]:
        """Use AI-selected THS boards as the only constituent-stock source."""
        as_of = _now(now)
        catalog_map = {item["ts_code"]: item for item in catalog.get("items") or []}
        selected = []
        for mapping in board_mappings:
            code = str(mapping.get("ths_code") or "").upper()
            if code in catalog_map and code not in {item["ths_code"] for item in selected}:
                selected.append({**mapping, "ths_code": code, "name": catalog_map[code]["name"], "type": catalog_map[code]["type"]})
        if not selected:
            raise AIStockModelError("AI 未映射到有效 THS 板块")
        market_date = self.latest_completed_market_date(as_of)
        daily = self.tushare.get_ths_daily_frame(market_date)
        flow = self.tushare.get_ths_moneyflow_frame(market_date)
        limits = self.tushare.get_a_stock_limit_concepts_frame(market_date)
        if daily is None or daily.empty or flow is None or flow.empty:
            raise AIStockError("THS 板块行情或资金流不可用，严格模式停止本批次")
        if limits is None or limits.empty:
            raise AIStockError("limit_cpt_list 不可用，严格模式停止本批次")
        daily_map = {str(row.get("ts_code") or "").upper(): row.to_dict() for _, row in daily.iterrows()}
        flow_map = {str(row.get("ts_code") or "").upper(): row.to_dict() for _, row in flow.iterrows()}
        limit_by_name = {str(row.get("name") or "").strip(): row.to_dict() for _, row in limits.iterrows()} if not limits.empty else {}
        event_map = {item["event_id"]: item for item in events}
        candidates: Dict[str, Dict[str, Any]] = {}
        boards: List[Dict[str, Any]] = []
        for board in selected:
            member = self.tushare.get_ths_member_frame(board["ths_code"])
            if member is None or member.empty:
                raise AIStockError(f"THS 板块 {board['ths_code']} 成分股不可用，严格模式停止本批次")
            strength = {"ths_daily": _json_safe(daily_map.get(board["ths_code"])), "moneyflow_cnt_ths": _json_safe(flow_map.get(board["ths_code"])), "limit_cpt_list": _json_safe(limit_by_name.get(board["name"]))}
            boards.append({**board, "strength": strength, "member_count": int(len(member))})
            for _, row in member.iterrows():
                ts_code = _normalize_ts_code(row.get("con_code"))
                if not CODE_PATTERN.fullmatch(ts_code):
                    continue
                entry = candidates.setdefault(ts_code, {"ts_code": ts_code, "name": str(row.get("con_name") or ts_code), "industry": "", "themes": [], "board_codes": [], "event_ids": [], "board_strength": []})
                if board["name"] not in entry["themes"]:
                    entry["themes"].append(board["name"])
                if board["ths_code"] not in entry["board_codes"]:
                    entry["board_codes"].append(board["ths_code"])
                    entry["board_strength"].append({"ths_code": board["ths_code"], **strength})
                if board["event_id"] not in entry["event_ids"]:
                    entry["event_ids"].append(board["event_id"])
        candidate_list = list(candidates.values())

        # ── Pre-filter with stock_basic *before* enforcing the cap so that
        # ST / 次新股 / long-delisted stocks never inflate the count. ──
        basic = self.tushare.get_a_stock_basic_frame(["L"])
        if basic is None or basic.empty:
            raise AIStockError("stock_basic 不可用，严格模式停止本批次")
        list_dates = {_normalize_ts_code(row.get("ts_code")): row.get("list_date") for _, row in basic.iterrows()}
        params = _load_strategy_params()
        pre_filtered: List[Dict[str, Any]] = []
        for candidate in candidate_list:
            name = str(candidate.get("name") or "")
            if "ST" in name.upper():
                continue
            list_date = list_dates.get(candidate["ts_code"])
            candidate["list_date"] = list_date.isoformat() if hasattr(list_date, "isoformat") else None
            candidate["listing_days"] = _listing_days(list_date, as_of.date())
            if (_safe_float(candidate.get("listing_days"), 0.0) or 0.0) < int(params.get("min_listing_days", MIN_LISTING_DAYS)):
                continue
            pre_filtered.append(candidate)

        # ── Fetch market-cap / turnover from DuckDB, then apply per-board
        # size cap + absolute minimums *before* the global candidate cap. ──
        # Note: the DuckDB daily sync can lag the trade_cal-based market_date
        # by a day (T+1), so query the latest date actually present in DuckDB.
        daily_lookup: Dict[str, Dict[str, float]] = {}
        window_lookup: Dict[str, Dict[str, Any]] = {}
        data_date: Optional[date] = None
        if pre_filtered:
            analytics_path = os.getenv("ANALYTICS_DB_PATH", "/var/lib/quant_robot/analytics.duckdb")
            codes = [c["ts_code"] for c in pre_filtered]
            try:
                with connect_duckdb(analytics_path, prefer_read_only=True) as ddb:
                    row = ddb.execute("SELECT MAX(trade_date) FROM a_stock_market_daily").fetchone()
                    data_date = row[0] if row and row[0] else None
                    if data_date is not None:
                        placeholders = ",".join(["?" for _ in codes])
                        rows = ddb.execute(
                            f"SELECT ts_code, total_mv, amount, close, pct_chg FROM a_stock_market_daily WHERE trade_date = ? AND ts_code IN ({placeholders})",
                            [data_date, *codes],
                        ).fetchall()
                    else:
                        rows = []
                for ts_code, total_mv, amount, close, pct_chg in rows:
                    daily_lookup[ts_code] = {"total_mv": float(total_mv or 0), "amount": float(amount or 0), "close": float(close or 0), "pct_chg": float(pct_chg or 0)}
                # Trailing-window features (momentum/volatility/price position /
                # avg turnover) used as the stock-characteristic baseline for the
                # pure-news residual.  One batched read, computed in memory.
                window_lookup: Dict[str, Dict[str, Any]] = {}
                wrows = ddb.execute(
                    f"SELECT ts_code, close, pct_chg, turnover_rate FROM a_stock_market_daily WHERE ts_code IN ({placeholders}) ORDER BY ts_code, trade_date",
                    codes,
                ).fetchall()
                bucket: List[Tuple[Optional[float], Optional[float], Optional[float]]] = []
                current_code: Optional[str] = None
                for w_ts_code, w_close, w_pct, w_turn in wrows:
                    if w_ts_code != current_code:
                        if current_code is not None and bucket:
                            window_lookup[current_code] = _window_features(bucket)
                        current_code = w_ts_code
                        bucket = []
                    bucket.append(
                        (
                            float(w_close) if w_close is not None else None,
                            float(w_pct) if w_pct is not None else None,
                            float(w_turn) if w_turn is not None else None,
                        )
                    )
                if current_code is not None and bucket:
                    window_lookup[current_code] = _window_features(bucket)
            except Exception as exc:
                logger.warning("DuckDB daily fetch failed, skipping size/liquidity filter: %s", exc)
                data_date = None

        # RPS（相对强度）：20 日动量的横截面百分位排名（0-100，越高越强）。
        # 用于每板块候选截取（配合 max_candidates_per_board），避免只按市值取头部。
        rps_sorted = sorted(
            v for v in (_safe_float((window_lookup.get(c["ts_code"]) or {}).get("mom_20d")) for c in pre_filtered) if v is not None
        )
        rps_n = len(rps_sorted)
        for candidate in pre_filtered:
            mom = _safe_float((window_lookup.get(candidate["ts_code"]) or {}).get("mom_20d"))
            candidate["rps"] = round(bisect_left(rps_sorted, mom) / rps_n * 100, 1) if (mom is not None and rps_n) else 50.0

        # Per-board: sort by RPS desc (tiebreak market cap), take top N.
        board_candidates: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for candidate in pre_filtered:
            for board_code in candidate.get("board_codes") or []:
                board_candidates[board_code].append(candidate)
        capped: Dict[str, Dict[str, Any]] = {}
        for board_code, members in board_candidates.items():
            members.sort(key=lambda c: (c.get("rps", 0.0), daily_lookup.get(c["ts_code"], {}).get("total_mv", 0)), reverse=True)
            for member in members[:params["max_candidates_per_board"]]:
                capped[member["ts_code"]] = member

        # Absolute minimums: market cap & turnover.  When DuckDB data is
        # unavailable (empty lookup), keep only the per-board size cap so a
        # data-sync outage never blocks recommendations entirely.
        # Note: the local vol/amount columns are currently zero-filled by a
        # sync-side merge bug, so the turnover filter is applied only when the
        # stored amount column actually carries non-zero data.
        filtered: List[Dict[str, Any]] = []
        if daily_lookup:
            max_amount = max((dl.get("amount", 0) for dl in daily_lookup.values()), default=0)
            for candidate in capped.values():
                dl = daily_lookup.get(candidate["ts_code"], {})
                mv_ok = dl.get("total_mv", 0) >= params["min_market_cap"]
                to_ok = dl.get("amount", 0) >= params["min_avg_turnover"] if max_amount > 0 else True
                if mv_ok and to_ok:
                    candidate["total_mv"] = dl.get("total_mv")
                    candidate["avg_turnover"] = dl.get("amount")
                    filtered.append(candidate)
        else:
            filtered = list(capped.values())

        if len(filtered) > params["max_candidates"]:
            raise AIStockError(f"THS 成分股候选达到 {len(filtered)} 只，超过 {params['max_candidates']} 的完整审计上限")

        # Realtime quotes (intraday only); fall back to DuckDB close for
        # after-hours / weekend runs so recommendations never depend on a
        # live tape being open.
        quote_map: Dict[str, Dict[str, Any]] = {}
        for offset in range(0, len(filtered), 50):
            quotes = self.tushare.get_quote_batch([item["ts_code"] for item in filtered[offset:offset + 50]])
            for quote in quotes or []:
                quote_map[_normalize_ts_code(quote.get("symbol"))] = quote
        for candidate in filtered:
            candidate.update(window_lookup.get(candidate["ts_code"]) or {})
        eligible = []
        for candidate in filtered:
            quote = quote_map.get(candidate["ts_code"])
            if quote:
                quote_time = quote.get("timestamp")
                candidate.update({"name": quote.get("name") or candidate["name"], "price": _safe_float(quote.get("price")), "change_pct": _safe_float(quote.get("percent_change")), "turnover": _safe_float(quote.get("turnover")), "quote_time": quote_time.isoformat() if hasattr(quote_time, "isoformat") else None, "data_fresh": True})
            else:
                # No live tape (closed market / missing quote): use the last
                # available DuckDB close as the price basis.
                daily = daily_lookup.get(candidate["ts_code"], {})
                fallback_price = daily.get("close")
                if fallback_price and fallback_price > 0:
                    candidate.update({"name": candidate["name"], "price": fallback_price, "change_pct": daily.get("pct_chg"), "turnover": (daily.get("amount") or 0) * 1000, "quote_time": (data_date or market_date).isoformat() + "T00:00:00", "data_fresh": True})
                else:
                    candidate["data_fresh"] = False
            if _is_candidate_eligible(candidate, int(params.get("min_listing_days", MIN_LISTING_DAYS))):
                candidate["execution_score"] = _execution_score(candidate)
                candidate["events"] = [event_map[event_id] for event_id in candidate["event_ids"] if event_id in event_map]
                eligible.append(candidate)
        if not eligible:
            raise AIStockError("THS 成分股中没有满足上市/交易条件的股票")
        if int(params.get("xueqiu_signal_enabled", XUEQIU_SIGNAL_ENABLED)):
            # 雪球活跃组合行为快照合并（xueqiu 块）：开关开启才查询并附加；
            # 快照不可用时只跳过合并，绝不影响推荐主流程。
            _attach_xueqiu_behavior(eligible)
        return {"generated_at": as_of.isoformat(timespec="seconds"), "trade_date": as_of.date().isoformat(), "market_as_of_date": market_date.isoformat(), "events": events, "boards": boards, "candidates": eligible, "source_status": {"ths_index": "SUCCESS", "ths_member": "SUCCESS", "ths_daily": "SUCCESS", "moneyflow_cnt_ths": "SUCCESS", "limit_cpt_list": "SUCCESS", "candidate_count": len(eligible)}}

    def minute_entry_confirmed(self, ts_code: str) -> Tuple[bool, Dict[str, Any]]:
        bars = self.tushare.get_a_stock_realtime_minute_frame(ts_code, "1MIN")
        if bars is None or len(bars) < 5:
            return False, {"reason": "分钟行情不足，无法确认入场"}
        recent = bars.tail(5)
        latest = recent.iloc[-1]
        prior = recent.iloc[-2]
        latest_close = _safe_float(latest.get("close"), 0.0) or 0.0
        prior_close = _safe_float(prior.get("close"), 0.0) or 0.0
        latest_volume = _safe_float(latest.get("vol"), 0.0) or 0.0
        baseline = _safe_float(bars.tail(min(20, len(bars))).iloc[:-1]["vol"].mean(), 0.0) or 0.0
        confirmed = latest_close >= prior_close and latest_volume >= baseline * 0.8
        return confirmed, {
            "latest_close": latest_close,
            "prior_close": prior_close,
            "latest_volume": latest_volume,
            "average_volume": baseline,
        }

    def quotes(self, symbols: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        result = self.tushare.get_quote_batch(list(dict.fromkeys(symbols)))
        return {_normalize_ts_code(item.get("symbol")): item for item in result}


class DeepSeekStockSelector:
    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        settings = _load_ai_stock_service_config_for_runtime() if api_key is None else {}
        self.api_key = (api_key or (settings.get("deepseek_api_key") if settings else None) or os.getenv("DEEPSEEK_API_KEY") or "").strip()
        self.model = (model or (settings.get("deepseek_model") if settings else None) or os.getenv("DEEPSEEK_MODEL") or DEFAULT_MODEL).strip()
        self.base_url = ((settings.get("deepseek_base_url") if settings else None) or os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/")

    def _call_json(self, messages: List[Dict[str, str]], max_tokens: Optional[int] = None, read_timeout: Optional[int] = None) -> Tuple[Dict[str, Any], str, Dict[str, Any], Dict[str, Any]]:
        if not self.api_key:
            raise AIStockConfigurationError("未配置 DEEPSEEK_API_KEY，不能生成 AI 推荐")
        body = {
            "model": self.model,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": messages,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        # Long read timeout: round-3 payloads (news + boards + hundreds of
        # candidates) can take DeepSeek well over a minute.  Transient network
        # errors get one retry; parse errors do not (a bad model reply will
        # just repeat itself).
        last_exc: Optional[Exception] = None
        for attempt in range(2):
            try:
                response = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json=body,
                    timeout=(30, read_timeout or 180),
                )
                response.raise_for_status()
                payload = response.json()
                content = (payload.get("choices") or [{}])[0].get("message", {}).get("content", "")
                parsed = json.loads(content)
                if not isinstance(parsed, dict):
                    raise AIStockModelError("DeepSeek 返回不是 JSON 对象")
                return parsed, content, body, {"completion_id": payload.get("id"), "usage": payload.get("usage") or {}, "model": payload.get("model")}
            except requests.RequestException as exc:
                last_exc = exc
                if attempt == 0:
                    logger.warning("DeepSeek 请求失败（第 1 次，准备重试）: %s", exc)
                    time.sleep(1)
                    continue
                raise AIStockModelError(f"DeepSeek 请求失败（已重试）: {exc}") from exc
            except (ValueError, TypeError, IndexError) as exc:
                raise AIStockModelError(f"DeepSeek 响应解析失败: {exc}") from exc
        raise AIStockModelError(f"DeepSeek 请求失败: {last_exc}")

    def extract_events(self, news_snapshot: Dict[str, Any]) -> Dict[str, Any]:
        headlines = news_snapshot.get("headlines") or []
        if not headlines:
            raise AIStockModelError("上一交易日14:00以来没有可用新闻标题，不能生成新闻驱动推荐")
        instruction = {
            "task": "你是中国A股新闻研究员。只根据下列自上一交易日14:00以来、截至本批次时间且已按时间升序排列的全部新闻标题，识别会影响A股的独立事件；不要根据价格、资金流或任何外部知识补充事件。",
            "constraints": {
                "max_events": _load_strategy_params()["max_events"],
                "must_cite_headline_ids": True,
                "must_return_json": True,
                "aliases": "用于下一轮理解新闻事件与同花顺板块目录关系的中文别名",
            },
            "news_headlines": headlines,
            "field_guidance": {
                "tone": "按标题措辞情绪方向判断: negative=利空措辞/positive=利好措辞/neutral=中性，不要用外部知识",
                "quantitative": "0-100，标题包含多少具体可验证的量化信息(业绩数字/订单金额/增速/比例)，无数字=0",
                "ambiguity": "0-100，信息模糊度('有望''或将''可能'等不确定措辞=高分；确定数字=低分)",
                "text_surprise": "0-100，标题本身多大程度超出市场对该公司的普遍预期，仅凭常识判断，不需要个股数据",
            },
            "response_schema": {
                "events": [
                    {
                        "hotword": "新闻热点词/事件",
                        "score": 0,
                        "headline_ids": ["N0001"],
                        "aliases": ["可用于理解题材的词"],
                        "direction": "利多/利空/中性",
                        "rationale": "仅基于标题的简要归因",
                        "tone": "negative/positive/neutral",
                        "quantitative": 0,
                        "ambiguity": 0,
                        "text_surprise": 0,
                    }
                ]
            },
        }
        messages = [
            {"role": "system", "content": "严格输出 JSON；不得编造标题编号或新闻中没有的热点。"},
            {"role": "user", "content": json.dumps(instruction, ensure_ascii=False)},
        ]
        raw_response, content, request_body, metadata = self._call_json(messages)
        events = _validated_events(raw_response, headlines)
        if not events:
            raise AIStockModelError("DeepSeek 未返回带新闻标题证据的有效事件")
        return {
            "model": self.model,
            "events": events,
            "transcript": {
                "stage": "NEWS_EVENTS",
                "request": request_body,
                "response_content": content,
                "response_json": raw_response,
                "response_metadata": metadata,
            },
        }

    def map_events_to_ths(
        self,
        event_stage: Dict[str, Any],
        catalog: Dict[str, Any],
    ) -> Dict[str, Any]:
        indexes = [
            {
                "ths_code": item["ts_code"], "name": item["name"], "type": item["type"], "constituent_count": item.get("count"),
            }
            for item in catalog.get("items") or []
        ]
        instruction: Dict[str, Any] = {
            "task": "你是A股题材研究员。以下是 Tushare 全量同花顺概念(N)、主题(TH)、行业(I)目录，以及需要映射到板块的新闻事件。请为每个事件选择最具直接产业关联的板块代码；只能从目录返回代码，不能选择股票；优先选择包含行业龙头股的板块。",
            "constraints": {
                "max_boards_per_event": 2,
                "max_unique_boards": _load_strategy_params()["max_boards"],
                "must_return_json": True,
            },
            # 大而静态的目录前置（跨批命中磁盘缓存），小而动态的事件后置
            "ths_index_catalog": indexes,
            "validated_events": event_stage["events"],
            "response_schema": {
                "board_mappings": [
                    {
                        "event_id": "E01",
                        "boards": [{"ths_code": "目录中的代码", "relevance": 0, "reason": "事件→板块的中文关联"}],
                    }
                ]
            },
        }
        # 自包含请求：system + 目录(前置) + 事件(后置)，不重放轮1新闻；目录为跨批固定前缀 → 缓存命中
        messages = [
            {"role": "system", "content": "严格输出 JSON；不得编造标题编号或新闻中没有的热点。"},
            {"role": "user", "content": json.dumps(instruction, ensure_ascii=False)},
        ]
        raw_response, content, request_body, metadata = self._call_json(messages)
        mappings = _validated_board_mappings(raw_response, event_stage["events"], catalog.get("items") or [])
        if not mappings:
            raise AIStockModelError("DeepSeek 未把新闻事件映射到有效 THS 板块")
        return {
            "model": self.model,
            "board_mappings": mappings,
            "transcript": {
                "stage": "EVENTS_TO_THS_BOARDS",
                "request": request_body,
                "response_content": content,
                "response_json": raw_response,
                "response_metadata": metadata,
            },
        }

    def select_from_ths_conversation(self, event_stage: Dict[str, Any], board_stage: Dict[str, Any], snapshot: Dict[str, Any], top_n: Optional[int] = None, fear_greed_ref: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        candidates = snapshot.get("candidates") or []
        if not candidates:
            raise AIStockModelError("已映射 THS 板块没有合格成分股")
        _params = _load_strategy_params()
        _news_enabled = float(_params.get("news_signal_weight", NEWS_SIGNAL_WEIGHT)) > 0
        _xueqiu_enabled = bool(int(_params.get("xueqiu_signal_enabled", XUEQIU_SIGNAL_ENABLED)))
        _compact_keys = ("ts_code", "name", "industry", "themes", "board_codes", "event_ids", "price", "change_pct", "turnover", "execution_score", "rps")
        if _news_enabled:
            # 纯新闻信号开启时才把信号与窗口特征喂给模型；权重=0 时保持旧字段集
            _compact_keys = _compact_keys + ("news_signal", "mom_5d", "mom_20d", "volatility_20d", "price_position", "turnover_avg_20d")
        if _xueqiu_enabled:
            # 雪球行为快照开启时才附加 xueqiu 块（候选内无该块 = 不在雪球活跃持仓内）
            _compact_keys = _compact_keys + ("xueqiu",)
        compact_candidates = [{key: item.get(key) for key in _compact_keys} for item in candidates]
        _tr_min = float(_params.get("target_return_pct_min", TARGET_RETURN_PCT_MIN))
        _tr_max = float(_params.get("target_return_pct_max", TARGET_RETURN_PCT_MAX))
        instruction = {
            "task": "延续新闻事件会话。以下是已验证的 THS 板块映射及全部合格成分股数据。只能从成分股选择，新闻事件和 THS 板块关联优先；板块强弱只作排序与执行参考。每只股票只能出现一次，严禁重复。",
            "constraints": {"max_picks": min(max(int(top_n or 0), 1), int(_params.get("max_recommendations", MAX_RECOMMENDATIONS))), "confidence_range": [0, 100], "target_return_pct_range": [_tr_min, _tr_max], "must_return_json": True},
            "validated_events": event_stage["events"], "validated_board_mappings": board_stage["board_mappings"], "board_market_snapshot": snapshot.get("boards") or [], "candidates": compact_candidates,
            "response_schema": {"picks": [{"ts_code": "候选列表完整 ts_code", "confidence": 0, "target_return_pct": 5, "reason": "新闻事件→THS板块→股票", "risks": "风险", "themes": ["THS板块"], "evidence": [{"event_id": "E01", "headline_id": "N0001", "ths_code": "885000.TI"}]}]},
        }
        if _news_enabled:
            instruction["news_signal_guidance"] = "candidates 的 news_signal(0-100) 是新闻定价效率信号，高分(≥65)=市场可能反应不足的潜在机会(负面/量化/低位组合)，低分(≤35)=高关注模糊主题的追高风险。请把它作为排序参考之一，与新闻证据链、板块强弱、技术面综合判断，不要仅凭信号选股，也不要机械拒绝低分股票。"
        if _xueqiu_enabled:
            instruction["xueqiu_guidance"] = (
                "candidates 的 xueqiu 是雪球活跃组合持仓行为快照，direction 是系统判定的确定性方向标签，"
                "直接按标签解读、勿自行计算：顺势加仓/逆势吸筹=组合在买入（逆势吸筹=价格跌而权重加，错杀/低位信号更强）；"
                "借涨减仓/减仓=组合在卖出；持平=权重变化由价格推动，无主动行为；"
                "新进=5日前未持有（排名≤30且组合数≥10属强新进，可提高置信度）。"
                "组合数上升(cube_gain_5d>0)=扩散更可信，下降=收敛。"
                "xueqiu 缺失=不在雪球持仓，不惩罚。"
                "参考线：权价比≥1.15 且组合数+2 且排名≤100（仅校准非准入）。"
                "本系统以新闻事件链选股，xueqiu 仅作排序与置信度参考。"
            )
        if fear_greed_ref:
            instruction["fear_greed_reference"] = fear_greed_ref
            instruction["fear_greed_guidance"] = "本系统另提供了一批行业/主题的恐慌贪婪指数(0-100，越低越恐慌/超卖，越高越贪婪/拥挤)及其生命周期阶段 stage，与 board_market_snapshot 里的板块按名称对应。阶段含义：启动(恐慌错杀后修复，敢买，可适度提高置信度)、扩散(资金流入、上涨扩散，可参与)、主线(持续强势、健康上涨，正常买)、拥挤(贪婪过热，应降低置信度或要求更强新闻证据)、退潮(从高位回落，回避或减仓)、探底(恐慌延续、尚未企稳，回避等企稳)、中性(无明确方向)。请结合 news_signal、板块强弱、技术面综合判断，不要机械套用。"
        first_messages = event_stage["transcript"]["request"]["messages"]
        # 轮3自包含：轮1新闻 + 事件回复 + 选股指令（内嵌事件/映射/板块/候选）。
        # 不再重放轮2目录与映射回复：mappings 已结构化内嵌，目录原文对选股无用，
        # 省 ~54K tokens/批；[S, 新闻] 前缀仍命中轮1批内缓存。
        messages = [
            *first_messages,
            {"role": "assistant", "content": event_stage["transcript"]["response_content"]},
            {"role": "user", "content": json.dumps(instruction, ensure_ascii=False)},
        ]
        raw_response, content, request_body, metadata = self._call_json(messages)
        if not isinstance(raw_response.get("picks"), list):
            raise AIStockModelError("DeepSeek 返回不包含 picks 列表")
        return {"model": self.model, "response": raw_response, "transcript": {"stage": "THS_BOARDS_TO_STOCK_SELECTION", "request": request_body, "response_content": content, "response_json": raw_response, "response_metadata": metadata}}

    def evaluate_positions(
        self,
        event_stage: Dict[str, Any],
        board_stage: Dict[str, Any],
        positions: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Round 4: score current paper positions (hold_score 0-100, low = sell).

        Reuses the round-1 news context (same message pattern as round 3) so the
        model judges each open position against today's raw headlines, events and
        sector strength.  The score is advisory only: process_minute still enforces
        the hard price rules.
        """
        if not positions:
            raise AIStockModelError("没有持仓需要评估")
        instruction = {
            "task": "延续新闻事件会话。以下是今日事件、THS 板块映射及模拟盘当前持仓（成本/现价/盈亏/持有天数）。请结合今日新闻事件与板块强弱，为每只持仓给出 hold_score(0-100)：分数越低越倾向卖出，越高越倾向继续持有。只评估现有持仓，不得新增或推荐其他标的。",
            "constraints": {
                "score_meaning": "0-100：≥70=继续持有/加仓(错价机会大、事件与板块强势)；50=中性；≤30=倾向卖出(基本面恶化、过度反应追高风险、事件利空、板块转弱)",
                "must_return_json": True,
            },
            "validated_events": event_stage["events"],
            "validated_board_mappings": board_stage.get("board_mappings") or [],
            "board_market_snapshot": board_stage.get("boards") or [],
            "positions": positions,
            "response_schema": {
                "evaluations": [
                    {"ts_code": "持仓 ts_code", "hold_score": 0, "reason": "中文理由：结合新闻事件/板块/盈亏的一句话判断"}
                ]
            },
        }
        first_messages = event_stage["transcript"]["request"]["messages"]
        messages = [*first_messages, {"role": "assistant", "content": event_stage["transcript"]["response_content"]}, {"role": "user", "content": json.dumps(instruction, ensure_ascii=False)}]
        raw_response, content, request_body, metadata = self._call_json(messages)
        if not isinstance(raw_response.get("evaluations"), list):
            raise AIStockModelError("DeepSeek 返回不包含 evaluations 列表")
        return {"model": self.model, "response": raw_response, "transcript": {"stage": "HOLD_EVALUATION", "request": request_body, "response_content": content, "response_json": raw_response, "response_metadata": metadata}}

def evaluate_paper_holdings(*, now: Optional[datetime] = None, event_stage: Dict[str, Any], board_stage: Dict[str, Any], run_id: int, selector: Optional[DeepSeekStockSelector] = None) -> Dict[str, Any]:
    """Advisory round-4 hold evaluation persisted to ai_stock_hold_evaluations.

    Reads current paper positions as plain snapshots (short transaction),
    asks the model to score each with hold_score (low = sell bias), then
    persists valid evaluations.  Failures are reported but never raise: the
    recommendation batch itself must not depend on this advisory step.
    """
    paper = AIStockPaperTradingService()
    positions = paper.positions()
    if not positions:
        return {"evaluated": False, "reason": "无持仓"}
    compact = [
        {
            "ts_code": item["ts_code"],
            "name": item["name"],
            "cost": item["cost"],
            "price": item["price"],
            "pnl_pct": item["pnl_pct"],
            "held_days": item["held_days"],
        }
        for item in positions
    ]
    try:
        selection = (selector or DeepSeekStockSelector()).evaluate_positions(event_stage, board_stage, compact)
    except Exception as exc:
        logger.warning("AI hold evaluation skipped: %s", exc)
        return {"evaluated": False, "reason": str(exc)[:200]}
    raw = selection["response"]
    by_code = {_normalize_ts_code(item["ts_code"]): item for item in positions}
    valid = []
    for item in raw.get("evaluations") or []:
        if not isinstance(item, dict):
            continue
        code = _normalize_ts_code(item.get("ts_code"))
        pos = by_code.get(code)
        if not pos:
            continue
        valid.append(
            {
                "ts_code": code,
                "name": pos["name"],
                "hold_score": _clamp(item.get("hold_score"), 0.0, 100.0, 50.0),
                "reason": str(item.get("reason") or "")[:500],
            }
        )
    if not valid:
        return {"evaluated": False, "reason": "AI 未返回有效评估"}
    with get_db_ctx() as db:
        portfolio = AIStockPaperTradingService._ensure_portfolio(db)
        for item in valid:
            db.add(AIStockHoldEvaluation(portfolio_id=portfolio.id, run_id=run_id, ts_code=item["ts_code"], name=item["name"], hold_score=item["hold_score"], reason=item["reason"]))
    return {"evaluated": True, "count": len(valid)}


def _validated_board_mappings(raw_response: Dict[str, Any], events: List[Dict[str, Any]], catalog: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    event_ids = {item["event_id"] for item in events}
    catalog_map = {str(item.get("ts_code") or "").upper(): item for item in catalog}
    accepted: List[Dict[str, Any]] = []
    seen = set()
    for item in raw_response.get("board_mappings") or []:
        event_id = str(item.get("event_id") or "").strip()
        if event_id not in event_ids:
            continue
        count = 0
        for board in item.get("boards") or []:
            code = str(board.get("ths_code") or "").strip().upper()
            if code not in catalog_map or code in seen or count >= 2 or len(accepted) >= _load_strategy_params()["max_boards"]:
                continue
            seen.add(code)
            count += 1
            accepted.append({"event_id": event_id, "ths_code": code, "relevance": _clamp(board.get("relevance"), 0.0, 100.0, 0.0), "reason": str(board.get("reason") or "")[:600]})
    return accepted


def _validated_picks(
    snapshot: Dict[str, Any],
    raw_response: Dict[str, Any],
    top_n: int,
    *,
    news_headlines: Optional[List[Dict[str, Any]]] = None,
    hotwords: Optional[List[Dict[str, Any]]] = None,
    board_mappings: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    candidate_map = {_normalize_ts_code(item.get("ts_code")): item for item in snapshot.get("candidates") or []}
    known_headlines = {item.get("headline_id"): item for item in news_headlines or []}
    known_events = {str(item.get("event_id") or "").strip(): item for item in hotwords or []}
    valid_event_boards = {(item.get("event_id"), item.get("ths_code")) for item in board_mappings or []}
    _params = _load_strategy_params()
    _tr_min = float(_params.get("target_return_pct_min", TARGET_RETURN_PCT_MIN))
    _tr_max = float(_params.get("target_return_pct_max", TARGET_RETURN_PCT_MAX))
    accepted: List[Dict[str, Any]] = []
    seen = set()
    for pick in raw_response.get("picks") or []:
        if not isinstance(pick, dict):
            continue
        ts_code = _normalize_ts_code(pick.get("ts_code"))
        candidate = candidate_map.get(ts_code)
        if not candidate or ts_code in seen or not _is_candidate_eligible(candidate, int(_load_strategy_params().get("min_listing_days", MIN_LISTING_DAYS))):
            continue
        confidence = _safe_float(pick.get("confidence"))
        target_return = _safe_float(pick.get("target_return_pct"))
        reason = str(pick.get("reason") or "").strip()
        if confidence is None or not reason or target_return is None:
            continue
        evidence = []
        for item in pick.get("evidence") or pick.get("news_evidence") or []:
            if not isinstance(item, dict):
                continue
            event_id = str(item.get("event_id") or "").strip()
            headline_id = str(item.get("headline_id") or "").strip()
            board_code = str(item.get("ths_code") or "").strip().upper()
            headline = known_headlines.get(headline_id)
            event = known_events.get(event_id)
            if event and headline and headline_id in (event.get("headline_ids") or []) and event_id in (candidate.get("event_ids") or []) and board_code in (candidate.get("board_codes") or []) and (event_id, board_code) in valid_event_boards:
                evidence.append(
                    {
                        "event_id": event_id,
                        "hotword": event.get("hotword") or event.get("term"),
                        "headline_id": headline_id,
                        "ths_code": board_code,
                        "headline": headline.get("title"),
                    }
                )
        # Actual AI batches must contain an auditable news-to-stock link.  The
        # optional branch preserves pure unit-test coverage of basic validation.
        if news_headlines is not None and hotwords is not None and board_mappings is not None and not evidence:
            continue
        seen.add(ts_code)
        requested_themes = [str(value)[:80] for value in pick.get("themes") or [] if str(value).strip()]
        allowed_themes = [str(value)[:80] for value in candidate.get("themes") or [] if str(value).strip()]
        themes = [value for value in requested_themes if value in allowed_themes] or allowed_themes
        accepted.append(
            {
                "ts_code": ts_code,
                "name": candidate["name"],
                "industry": candidate.get("industry") or "",
                "themes": themes[:8],
                "recommendation_price": float(candidate["price"]),
                "target_return_pct": _clamp(target_return, _tr_min, _tr_max, _tr_min),
                "ai_confidence": _clamp(confidence, 0.0, 100.0, 0.0),
                "execution_score": _execution_score(candidate),
                "news_signal": _news_signal_score(candidate),
                "reason": reason[:1200],
                "risks": str(pick.get("risks") or "")[:800],
                "evidence": evidence or [str(value)[:400] for value in pick.get("evidence") or [] if str(value).strip()][:8],
                "candidate_snapshot": candidate,
            }
        )
    _nsw = float(_params.get("news_signal_weight", NEWS_SIGNAL_WEIGHT))
    accepted.sort(key=lambda item: (-item["ai_confidence"], -item.get("news_signal", 50.0) * _nsw, -item["execution_score"], item["ts_code"]))
    for index, item in enumerate(accepted[:top_n], start=1):
        item["rank"] = index
        item["target_price"] = round(item["recommendation_price"] * (1 + item["target_return_pct"] / 100), 3)
    return accepted[:top_n]


class AIStockRecommendationService:
    def __init__(self, provider: Optional[AIStockDataProvider] = None, selector: Optional[DeepSeekStockSelector] = None):
        self.provider = provider or AIStockDataProvider()
        self.selector = selector or DeepSeekStockSelector()

    def run_recommendation(
        self,
        *,
        now: Optional[datetime] = None,
        run_type: Optional[str] = None,
        top_n: Optional[int] = None,
        allow_after_hours: bool = False,
    ) -> Dict[str, Any]:
        timestamp = _now(now)
        if not allow_after_hours and not _is_recommendation_window(timestamp):
            raise AIStockError("收盘后仅做历史复盘，不生成新的可交易 AI 推荐")
        kind = (run_type or _run_type_for_time(timestamp)).upper()
        if kind not in {"PREOPEN", "OPENING", "INTRADAY"}:
            raise ValueError("run_type 必须为 PREOPEN、OPENING 或 INTRADAY")
        configured_top = int(_load_strategy_params().get("max_recommendations", MAX_RECOMMENDATIONS))
        top_n = min(max(int(top_n) if top_n is not None else configured_top, 1), configured_top)

        run_id = self._reserve_run(timestamp, kind)
        return self._execute_reserved_run(run_id, timestamp, kind, top_n)

    def _reserve_run(self, timestamp: datetime, kind: str) -> int:
        """同步占位一条 RUNNING 批次，供同步/异步两条路径复用，避免异步下重复触发。"""
        with get_db_ctx() as db:
            run = AIStockRecommendationRun(
                trade_date=timestamp.date(),
                run_type=kind,
                run_at=timestamp,
                status="RUNNING",
                prompt_version=PROMPT_VERSION,
            )
            db.add(run)
            db.flush()
            return run.id

    def _execute_reserved_run(self, run_id: int, timestamp: datetime, kind: str, top_n: int) -> Dict[str, Any]:
        transcript: Dict[str, Any] = {"conversation_version": PROMPT_VERSION, "stages": []}
        try:
            news_snapshot = self.provider.build_news_snapshot(timestamp)
            event_stage = self.selector.extract_events(news_snapshot)
            transcript["stages"].append(event_stage["transcript"])
            catalog = self.provider.ths_index_catalog()
            board_stage = self.selector.map_events_to_ths(event_stage, catalog)
            transcript["stages"].append(board_stage["transcript"])
            snapshot = self.provider.build_candidate_snapshot(timestamp, event_stage["events"], board_stage["board_mappings"], catalog)
            selection = self.selector.select_from_ths_conversation(event_stage, board_stage, snapshot, top_n=top_n, fear_greed_ref=_a_stock_fear_greed_reference())
            transcript["stages"].append(selection["transcript"])
            picks = _validated_picks(
                snapshot,
                selection["response"],
                top_n,
                news_headlines=news_snapshot["headlines"],
                hotwords=event_stage["events"],
                board_mappings=board_stage["board_mappings"],
            )
            if not picks:
                raise AIStockModelError("DeepSeek 未返回带新闻→THS板块→股票证据的有效候选")
        except Exception as exc:
            with get_db_ctx() as db:
                run = db.get(AIStockRecommendationRun, run_id)
                if run:
                    run.status = "FAILED"
                    run.completed_at = _now()
                    run.error_message = str(exc)[:2000]
                    run.ai_raw_response = transcript
            raise

        with get_db_ctx() as db:
            run = db.get(AIStockRecommendationRun, run_id)
            if not run:
                raise AIStockError("AI 推荐批次写入失败")
            run.status = "SUCCESS"
            run.completed_at = _now()
            run.model_name = selection["model"]
            run.market_snapshot = {
                "events": event_stage["events"],
                "ths_catalog": {"cached": catalog.get("cached"), "fetched_at": catalog.get("fetched_at"), "count": len(catalog.get("items") or [])},
                "board_mappings": board_stage["board_mappings"],
                "boards": snapshot.get("boards") or [],
                "market_as_of_date": snapshot.get("market_as_of_date"),
                "news_source_status": news_snapshot.get("source_status") or {},
                "source_status": snapshot.get("source_status") or {},
            }
            run.news_snapshot = news_snapshot["headlines"]
            run.candidate_snapshot = snapshot.get("candidates") or []
            run.ai_raw_response = transcript
            for pick in picks:
                db.add(AIStockRecommendation(run_id=run.id, **pick))
        # Round 4 (advisory): score current paper positions when the option is
        # enabled.  Never allowed to fail the batch.
        try:
            hold_cfg = AIStockPaperTradingService().strategy_config()
            if (hold_cfg.get("parameters") or {}).get("hold_evaluation_enabled"):
                # run 在 session 关闭后已脱离（expire_on_commit 会过期其属性），
                # 必须使用先前捕获的 run_id，避免 DetachedInstanceError。
                evaluate_paper_holdings(now=timestamp, event_stage=event_stage, board_stage=board_stage, run_id=run_id)
        except Exception as exc:
            logger.warning("AI hold evaluation step skipped: %s", exc)
        return self.get_run(run_id)

    def run_recommendation_async(
        self,
        *,
        now: Optional[datetime] = None,
        run_type: Optional[str] = None,
        top_n: Optional[int] = None,
        allow_after_hours: bool = False,
    ) -> int:
        """同步占位一条 RUNNING 批次，然后在后台线程执行完整生成流程。

        与 run_recommendation 的差别：占位后立即返回 run_id，不阻塞调用方
        （机器人主循环 / HTTP 请求）。完成/失败通过 publish_event 推送。
        """
        timestamp = _now(now)
        if not allow_after_hours and not _is_recommendation_window(timestamp):
            raise AIStockError("收盘后仅做历史复盘，不生成新的可交易 AI 推荐")
        kind = (run_type or _run_type_for_time(timestamp)).upper()
        if kind not in {"PREOPEN", "OPENING", "INTRADAY"}:
            raise ValueError("run_type 必须为 PREOPEN、OPENING 或 INTRADAY")
        configured_top = int(_load_strategy_params().get("max_recommendations", MAX_RECOMMENDATIONS))
        top_n = min(max(int(top_n) if top_n is not None else configured_top, 1), configured_top)

        run_id = self._reserve_run(timestamp, kind)

        def _worker() -> None:
            try:
                result = self._execute_reserved_run(run_id, timestamp, kind, top_n)
                _publish_recommendation_result(result, "SUCCESS")
            except Exception as exc:
                logger.exception("Async AI stock recommendation failed")
                _publish_recommendation_result({"id": run_id, "run_type": kind}, "FAILED", str(exc))

        threading.Thread(target=_worker, daemon=True, name="ai-stock-run").start()
        return run_id

    @staticmethod
    def _recommendation_payload(row: AIStockRecommendation) -> Dict[str, Any]:
        return {
            "id": row.id,
            "ts_code": row.ts_code,
            "name": row.name,
            "industry": row.industry,
            "themes": row.themes or [],
            "recommendation_price": row.recommendation_price,
            "target_return_pct": row.target_return_pct,
            "target_price": row.target_price,
            "ai_confidence": row.ai_confidence,
            "execution_score": row.execution_score,
            "news_signal": row.news_signal if row.news_signal is not None else 50.0,
            "rank": row.rank,
            "reason": row.reason,
            "risks": row.risks,
            "evidence": row.evidence or [],
            "created_at": row.created_at,
        }

    def get_run(self, run_id: int) -> Dict[str, Any]:
        with get_db_ctx() as db:
            run = db.get(AIStockRecommendationRun, int(run_id))
            if not run:
                raise AIStockError("未找到 AI 推荐批次")
            rows = db.query(AIStockRecommendation).filter(AIStockRecommendation.run_id == run.id).order_by(AIStockRecommendation.rank).all()
            return {
                "id": run.id,
                "run_at": run.run_at,
                "trade_date": run.trade_date,
                "run_type": run.run_type,
                "status": run.status,
                "model_name": run.model_name,
                "prompt_version": run.prompt_version,
                "candidate_count": len(run.candidate_snapshot or []),
                "news_count": len(run.news_snapshot or []),
                "error_message": run.error_message,
                "recommendations": [self._recommendation_payload(row) for row in rows],
            }

    def get_run_evidence(self, run_id: int) -> Dict[str, Any]:
        """News→THS→constituents evidence chain for one batch, loaded on demand."""
        with get_db_ctx() as db:
            run = db.get(AIStockRecommendationRun, int(run_id))
            if not run:
                raise AIStockError("未找到 AI 推荐批次")
            market_snapshot = run.market_snapshot or {}
            news_snapshot = run.news_snapshot or []
        return {"market_snapshot": market_snapshot, "news_snapshot": news_snapshot}

    def get_run_transcript(self, run_id: int) -> Dict[str, Any]:
        """Full AI conversation transcript for one batch, loaded on demand."""
        with get_db_ctx() as db:
            run = db.get(AIStockRecommendationRun, int(run_id))
            if not run:
                raise AIStockError("未找到 AI 推荐批次")
            ai_raw_response = run.ai_raw_response or {}
        return {"ai_raw_response": ai_raw_response}

    def today(self, limit: int = 200) -> List[Dict[str, Any]]:
        """All AI picks from today's batches, deduplicated by ts_code.

        Repeated picks across batches collapse into one row: first (earliest)
        run_at, average ai_confidence / target_return_pct / prices, plus a
        recommendation_count column.  Sorted by average ai_confidence desc.
        """
        today = _now().date()
        with get_db_ctx() as db:
            rows = (
                db.query(AIStockRecommendation)
                .join(AIStockRecommendationRun, AIStockRecommendation.run_id == AIStockRecommendationRun.id)
                .filter(
                    AIStockRecommendationRun.status == "SUCCESS",
                    AIStockRecommendationRun.trade_date == today,
                )
                .order_by(desc(AIStockRecommendationRun.run_at), AIStockRecommendation.rank)
                .limit(min(max(int(limit), 1), 500))
                .all()
            )
            if not rows:
                return []
            run_ids = {row.run_id for row in rows}
            runs = db.query(AIStockRecommendationRun).filter(AIStockRecommendationRun.id.in_(run_ids)).all()
            run_by_id = {run.id: run for run in runs}

            grouped: Dict[str, Dict[str, Any]] = {}
            for row in rows:
                payload = self._recommendation_payload(row)
                run_at = run_by_id[row.run_id].run_at
                group = grouped.get(row.ts_code)
                if group is None:
                    group = {
                        "payload": payload,
                        "run_at": run_at,
                        "count": 0,
                        "confidence_sum": 0.0,
                        "return_sum": 0.0,
                        "price_sum": 0.0,
                    }
                    grouped[row.ts_code] = group
                if run_at < group["run_at"]:
                    group["run_at"] = run_at
                group["count"] += 1
                group["confidence_sum"] += float(payload.get("ai_confidence") or 0.0)
                group["return_sum"] += float(payload.get("target_return_pct") or 0.0)
                group["price_sum"] += float(payload.get("recommendation_price") or 0.0)

            results = []
            for code, group in grouped.items():
                count = max(group["count"], 1)
                item = dict(group["payload"])
                item["run_at"] = group["run_at"]
                item["recommendation_count"] = group["count"]
                item["ai_confidence"] = round(group["confidence_sum"] / count, 1)
                item["target_return_pct"] = round(group["return_sum"] / count, 2)
                avg_price = group["price_sum"] / count
                item["recommendation_price"] = round(avg_price, 3)
                item["target_price"] = round(avg_price * (1 + item["target_return_pct"] / 100), 3)
                results.append(item)

            results.sort(key=lambda item: (-item["ai_confidence"], item["run_at"], item["ts_code"]))
            for index, item in enumerate(results, start=1):
                item["rank"] = index
            return results

    def current(self, limit: Optional[int] = None) -> Dict[str, Any]:
        with get_db_ctx() as db:
            run = db.query(AIStockRecommendationRun).filter(AIStockRecommendationRun.status == "SUCCESS").order_by(desc(AIStockRecommendationRun.run_at)).first()
            if not run:
                return {"run": None, "recommendations": []}
            run_id = run.id
        if limit is None:
            limit = int(_load_strategy_params().get("max_recommendations", MAX_RECOMMENDATIONS))
        payload = self.get_run(run_id)
        return {"run": {key: value for key, value in payload.items() if key != "recommendations"}, "recommendations": payload["recommendations"][:limit]}

    def history(self, trade_date: Optional[date] = None, run_type: Optional[str] = None, limit: int = 60) -> List[Dict[str, Any]]:
        with get_db_ctx() as db:
            query = db.query(AIStockRecommendationRun).filter(AIStockRecommendationRun.status == "SUCCESS")
            if trade_date:
                query = query.filter(AIStockRecommendationRun.trade_date == trade_date)
            if run_type:
                query = query.filter(AIStockRecommendationRun.run_type == str(run_type).upper())
            rows = query.order_by(desc(AIStockRecommendationRun.run_at)).limit(min(max(int(limit), 1), 200)).all()
            return [
                {
                    "id": row.id,
                    "run_at": row.run_at,
                    "trade_date": row.trade_date,
                    "run_type": row.run_type,
                    "model_name": row.model_name,
                    "candidate_count": len(row.candidate_snapshot or []),
                    "recommendation_count": db.query(AIStockRecommendation).filter(AIStockRecommendation.run_id == row.id).count(),
                }
                for row in rows
            ]

    def run_performance(self, run_id: int, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Calculate cost-adjusted forward observations for one saved AI batch.

        The recommendation snapshot is copied in a short transaction.  Daily
        bars are fetched only after the session is closed, so this read-only
        historical view never holds SQLite open during an external request.
        """
        with get_db_ctx() as db:
            run = db.get(AIStockRecommendationRun, int(run_id))
            if not run:
                raise AIStockError("未找到 AI 推荐批次")
            run_date = run.trade_date
            rows = db.query(AIStockRecommendation).filter(AIStockRecommendation.run_id == run.id).order_by(AIStockRecommendation.rank).all()
            recommendations = [
                {"id": row.id, "ts_code": row.ts_code, "recommendation_price": row.recommendation_price}
                for row in rows
            ]
        if not recommendations:
            return {"run_id": int(run_id), "items": [], "as_of": _now(now).date()}

        as_of = _now(now).date()
        if as_of < run_date:
            return {"run_id": int(run_id), "items": [], "as_of": as_of}
        # 20 calendar days covers five trading days even over a normal holiday.
        end_date = min(as_of, run_date + timedelta(days=20))
        frame = AIStockDataProvider().tushare.get_a_stock_daily_range_frame(run_date, end_date)
        by_symbol: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        if frame is not None and not frame.empty:
            for _, daily in frame.iterrows():
                ts_code = _normalize_ts_code(daily.get("ts_code"))
                if ts_code:
                    by_symbol[ts_code].append(
                        {
                            "trade_date": daily.get("trade_date"),
                            "close": _safe_float(daily.get("close")),
                            "high": _safe_float(daily.get("high")),
                        }
                    )
        result = []
        for recommendation in recommendations:
            entry_price = _safe_float(recommendation["recommendation_price"], 0.0) or 0.0
            observations = sorted(
                [row for row in by_symbol.get(recommendation["ts_code"], []) if row.get("trade_date") and row["trade_date"] >= run_date],
                key=lambda row: row["trade_date"],
            )
            same_day = next((row for row in observations if row["trade_date"] == run_date), None)
            later_days = [row for row in observations if row["trade_date"] > run_date]
            five_day_window = (([same_day] if same_day else []) + later_days)[:5]

            def net_return(price: Optional[float]) -> Optional[float]:
                if not entry_price or not price or price <= 0:
                    return None
                quantity = 100
                buy_amount = entry_price * quantity
                sell_amount = price * quantity
                return round(((sell_amount - _sell_fee(sell_amount)) / (buy_amount + _buy_fee(buy_amount)) - 1) * 100, 3)

            result.append(
                {
                    "recommendation_id": recommendation["id"],
                    "ts_code": recommendation["ts_code"],
                    "same_day_return_pct": net_return(same_day.get("close") if same_day else None),
                    "next_day_return_pct": net_return(later_days[0].get("close")) if later_days else None,
                    "five_day_return_pct": net_return(five_day_window[4].get("close")) if len(five_day_window) >= 5 else None,
                    "five_day_high_return_pct": net_return(max((row.get("high") or 0.0 for row in five_day_window), default=0.0)) if five_day_window else None,
                    "observed_trading_days": len(five_day_window),
                }
            )
        return {"run_id": int(run_id), "as_of": as_of, "items": result}


def _scheduled_recommendation_type(timestamp: datetime) -> Optional[str]:
    """Return the one batch type allowed for this local Shanghai minute."""
    if timestamp.weekday() >= 5:
        return None
    minute = timestamp.hour * 60 + timestamp.minute
    if minute == 9 * 60 + 26:
        return "PREOPEN"
    if minute == 9 * 60 + 40:
        return "OPENING"
    intraday_minutes = {
        10 * 60,
        10 * 60 + 30,
        11 * 60,
        11 * 60 + 30,
        13 * 60,
        13 * 60 + 30,
        14 * 60,
        14 * 60 + 30,
    }
    return "INTRADAY" if minute in intraday_minutes else None


def process_ai_stock_automation_for_robot(now: Optional[datetime] = None) -> Dict[str, Any]:
    """Run due recommendation, benchmark, and paper-trading work.

    Each database access in this coordinator is intentionally short.  The
    slower provider/model/http work is delegated to service calls after those
    sessions have closed.
    """
    timestamp = _now(now)
    result: Dict[str, Any] = {"timestamp": timestamp.isoformat(), "recommendation": None, "paper": None, "benchmark": None, "review": None, "hit_rate": None}
    run_type = _scheduled_recommendation_type(timestamp)
    if run_type:
        minute_start = timestamp.replace(second=0, microsecond=0)
        minute_end = minute_start + timedelta(minutes=1)
        with get_db_ctx() as db:
            already_started = (
                db.query(AIStockRecommendationRun.id)
                .filter(
                    AIStockRecommendationRun.trade_date == timestamp.date(),
                    AIStockRecommendationRun.run_type == run_type,
                    AIStockRecommendationRun.run_at >= minute_start,
                    AIStockRecommendationRun.run_at < minute_end,
                )
                .first()
                is not None
            )
        if not already_started:
            try:
                # 异步：占位后立即返回，不阻塞机器人主循环（DeepSeek 多轮可能耗时数分钟），
                # 避免卡住后续 OPENING/INTRADAY 的触发窗口。
                AIStockRecommendationService().run_recommendation_async(now=timestamp, run_type=run_type)
                result["recommendation"] = {"status": "RUNNING", "run_type": run_type}
                # The reference service is optional and strictly read-only.
                result["benchmark"] = AIStockBenchmarkCollector().collect(now=timestamp)
            except Exception as exc:
                logger.exception("AI stock scheduled recommendation failed")
                result["recommendation"] = {"status": "FAILED", "message": str(exc)}
                _publish_recommendation_result({"id": None, "run_type": run_type}, "FAILED", str(exc))

    if _is_market_session(timestamp) and timestamp.weekday() < 5:
        try:
            result["paper"] = AIStockPaperTradingService().process_minute(now=timestamp)
        except Exception as exc:
            logger.exception("AI stock paper trading minute failed")
            result["paper"] = {"processed": False, "message": str(exc)}
    # The closing task is review-only: it collects comparison observations and
    # evaluates prior batches, but never produces a fresh tradable pick.
    if timestamp.weekday() < 5 and timestamp.hour == 15 and timestamp.minute == 5:
        with get_db_ctx() as db:
            reviewed = (
                db.query(AIStockEvaluation.id)
                .filter(AIStockEvaluation.window_end == timestamp.date())
                .first()
                is not None
            )
        if not reviewed:
            try:
                result["benchmark"] = AIStockBenchmarkCollector().collect(now=timestamp)
                result["review"] = evaluate_ai_stock_benchmark(now=timestamp)
            except Exception as exc:
                logger.exception("AI stock closing review failed")
                result["review"] = {"status": "FAILED", "message": str(exc)}
    # 每交易日 16:20 评估推荐命中率（收盘后，当日数据已定）
    if timestamp.weekday() < 5 and timestamp.hour == 16 and timestamp.minute == 20:
        with get_db_ctx() as db:
            done = (
                db.query(AIStockRecommendationHitRate.id)
                .filter(AIStockRecommendationHitRate.window_end == timestamp.date())
                .first()
                is not None
            )
        if not done:
            try:
                result["hit_rate"] = evaluate_recommendation_hit_rate(now=timestamp)
            except Exception as exc:
                logger.exception("AI stock hit-rate evaluation failed")
                result["hit_rate"] = {"status": "FAILED", "message": str(exc)}
    return result


def _publish_recommendation_result(run: Dict[str, Any], status: str, message: str = "") -> None:
    """Broadcast a recommendation batch status update over the shared WS stream."""
    try:
        publish_event(
            None,
            "ai_stock_run_updated",
            {
                "run_id": run.get("id"),
                "status": status,
                "run_type": run.get("run_type"),
                "message": str(message)[:500] if message else "",
            },
        )
    except Exception:
        logger.exception("Failed to publish ai_stock_run_updated event")


def trigger_recommendation_async(
    run_type: Optional[str] = None,
    top_n: Optional[int] = None,
) -> Dict[str, Any]:
    """Fire a recommendation batch in a background thread and return immediately.

    The full run can take minutes (news + 3 DeepSeek rounds + THS data), so the
    HTTP request never blocks on it.  Completion/failure is pushed to every
    connected frontend via the shared backend event stream
    (``ai_stock_run_updated``).
    """
    try:
        AIStockRecommendationService().run_recommendation_async(
            run_type=run_type,
            top_n=top_n,
            allow_after_hours=True,
        )
    except Exception as exc:
        logger.exception("Failed to start async AI stock recommendation")
        return {"status": "FAILED", "message": str(exc)}
    return {"status": "RUNNING", "message": "AI 荐股已触发，结果将通过事件推送更新"}


def _buy_fee(amount: float) -> float:
    return round(max(MIN_COMMISSION, amount * COMMISSION_RATE) + amount * TRANSFER_RATE, 2)


def _sell_fee(amount: float) -> float:
    return round(max(MIN_COMMISSION, amount * COMMISSION_RATE) + amount * (TRANSFER_RATE + SELL_STAMP_DUTY_RATE), 2)


def _fear_greed_target(fear_greed: float) -> float:
    if fear_greed >= 80:
        return 0.20
    if fear_greed >= 60:
        return 0.375
    if fear_greed <= 20:
        return 0.60
    return 0.50


def _holding_days(bought_at: datetime, today: date) -> int:
    return max(0, (today - bought_at.date()).days)


@dataclass
class PlannedTrade:
    side: str
    ts_code: str
    name: str
    price: float
    quantity: int
    reason_code: str
    reason: str
    recommendation_id: Optional[int] = None
    lot_id: Optional[int] = None
    state_snapshot: Optional[Dict[str, Any]] = None


class AIStockPaperTradingService:
    def __init__(self, provider: Optional[AIStockDataProvider] = None):
        self.provider = provider or AIStockDataProvider()

    @staticmethod
    def _ensure_portfolio(db) -> AIStockPaperPortfolio:
        portfolio = db.get(AIStockPaperPortfolio, 1)
        if not portfolio:
            portfolio = AIStockPaperPortfolio(id=1)
            db.add(portfolio)
            db.flush()
        return portfolio

    @staticmethod
    def _ensure_strategy_config(db) -> AIStockStrategyConfig:
        config = db.get(AIStockStrategyConfig, 1)
        if not config:
            config = AIStockStrategyConfig(
                id=1,
                parameters={
                    "max_positions": 10,
                    "slot_count": 5,
                    "single_stock_cap": 0.20,
                    "max_execution_target": 0.90,
                    "entry_price_cap_pct": 1.0,
                    "stop_loss_half_pct": -8.0,
                    "stop_loss_full_pct": -12.0,
                    "trading_start_minute": 585,
                    "hold_evaluation_enabled": False,
                    "hold_sell_threshold": 30.0,
                    "max_buys_per_day": 3,
                    "rotation_confidence_gap": 20.0,
                    "rotation_stale_days": 3,
                },
            )
            db.add(config)
            db.flush()
        return config

    def _snapshot(self, snapshot_date: date) -> Dict[str, Any]:
        with get_db_ctx() as db:
            portfolio = self._ensure_portfolio(db)
            strategy_config = self._ensure_strategy_config(db)
            lots = db.query(AIStockPaperLot).filter(AIStockPaperLot.portfolio_id == portfolio.id, AIStockPaperLot.remaining_quantity > 0).order_by(AIStockPaperLot.bought_at).all()
            since = datetime.combine(snapshot_date, datetime.min.time()) - timedelta(days=2)
            recommendations = (
                db.query(AIStockRecommendation)
                .join(AIStockRecommendationRun, AIStockRecommendation.run_id == AIStockRecommendationRun.id)
                .filter(AIStockRecommendationRun.status == "SUCCESS", AIStockRecommendationRun.run_at >= since)
                .order_by(desc(AIStockRecommendationRun.run_at), AIStockRecommendation.rank)
                .all()
            )
            today_buys = db.query(AIStockPaperTrade).filter(
                AIStockPaperTrade.portfolio_id == portfolio.id,
                AIStockPaperTrade.side == "BUY",
                AIStockPaperTrade.trade_date == snapshot_date,
            ).all()
            eval_rows = (
                db.query(AIStockHoldEvaluation)
                .filter(AIStockHoldEvaluation.portfolio_id == portfolio.id)
                .order_by(desc(AIStockHoldEvaluation.evaluated_at))
                .all()
            )
            hold_scores: Dict[str, Dict[str, Any]] = {}
            for ev in eval_rows:
                if ev.ts_code not in hold_scores:
                    hold_scores[ev.ts_code] = {"hold_score": float(ev.hold_score), "reason": ev.reason}
            # 每只持仓最近一次被推荐（SUCCESS 批次）的 trade_date，用于"未获再推荐"换仓
            last_recommended: Dict[str, date] = {}
            held_codes = [lot.ts_code for lot in lots]
            if held_codes:
                rec_dates = (
                    db.query(AIStockRecommendation.ts_code, AIStockRecommendationRun.trade_date)
                    .join(AIStockRecommendationRun, AIStockRecommendation.run_id == AIStockRecommendationRun.id)
                    .filter(AIStockRecommendationRun.status == "SUCCESS", AIStockRecommendation.ts_code.in_(held_codes))
                    .order_by(desc(AIStockRecommendationRun.trade_date))
                    .all()
                )
                for ts_code, trade_date in rec_dates:
                    if ts_code not in last_recommended:
                        last_recommended[ts_code] = trade_date
            return {
                "portfolio": {
                    "id": portfolio.id,
                    "enabled": portfolio.enabled,
                    "cash": portfolio.cash,
                    "last_processed_minute": portfolio.last_processed_minute,
                    "last_execution_target": portfolio.last_execution_target,
                    "strategy_enabled": strategy_config.enabled,
                    "strategy_params": dict(strategy_config.parameters or {}),
                },
                "lots": [
                    {
                        "id": row.id,
                        "recommendation_id": row.recommendation_id,
                        "ts_code": row.ts_code,
                        "name": row.name,
                        "bought_at": row.bought_at,
                        "buy_price": row.buy_price,
                        "remaining_quantity": row.remaining_quantity,
                        "target_price": row.target_price,
                        "peak_price": row.peak_price if row.peak_price is not None else row.buy_price,
                        "stop_half_triggered": row.stop_half_triggered,
                    }
                    for row in lots
                ],
                "recommendations": [AIStockRecommendationService._recommendation_payload(row) for row in recommendations],
                "today_buys": {(row.ts_code, row.recommendation_id) for row in today_buys},
                "today_buy_count": len(today_buys),
                "hold_scores": hold_scores,
                "last_recommended_dates": last_recommended,
            }

    def process_minute(self, now: Optional[datetime] = None, fear_greed: Optional[float] = None) -> Dict[str, Any]:
        timestamp = _now(now)
        if not _is_market_session(timestamp):
            return {"processed": False, "reason": "不在 A 股连续竞价时段"}
        # 开盘初期 K 线不足，延迟到 trading_start_minute（默认 9:45=585）才执行交易，
        # 避免开盘脉冲导致误入场/误止损。推荐生成不受影响（走 _is_recommendation_window）。
        state = self._snapshot(timestamp.date())
        portfolio = state["portfolio"]
        minute_of_day = timestamp.hour * 60 + timestamp.minute
        sp = portfolio.get("strategy_params") or {}
        start_minute = int(_safe_float(sp.get("trading_start_minute"), 585.0))
        if minute_of_day < start_minute:
            return {"processed": False, "reason": f"未到交易开始时间（{start_minute // 60:02d}:{start_minute % 60:02d}）"}
        minute_key = timestamp.replace(second=0, microsecond=0)
        if not portfolio["enabled"] or not portfolio.get("strategy_enabled", True) or portfolio["last_processed_minute"] == minute_key:
            return {"processed": False, "reason": "模拟盘未启用或该分钟已处理"}

        fg = _clamp(fear_greed if fear_greed is not None else os.getenv("AI_STOCK_DEFAULT_FEAR_GREED"), 0.0, 100.0, 50.0)
        symbols = [lot["ts_code"] for lot in state["lots"]] + [item["ts_code"] for item in state["recommendations"]]
        try:
            quotes = self.provider.quotes(symbols)
        except Exception as exc:
            return {"processed": False, "reason": f"行情读取失败: {exc}"}

        sp = state["portfolio"].get("strategy_params") or {}
        execution_target = min(_safe_float(sp.get("max_execution_target"), 0.90), _fear_greed_target(fg) * 1.4)
        stop_loss_full = _safe_float(sp.get("stop_loss_full_pct"), -12.0)
        stop_loss_half = _safe_float(sp.get("stop_loss_half_pct"), -8.0)
        rotation_gap = _safe_float(sp.get("rotation_confidence_gap"), 20.0)
        rotation_stale_days = int(_safe_float(sp.get("rotation_stale_days"), 3.0))
        max_buys_per_day = int(_safe_float(sp.get("max_buys_per_day"), 3.0))
        hold_enabled = bool(sp.get("hold_evaluation_enabled"))
        # 移动止盈所需 MA10（按日缓存，一天只拉一次日线）
        ma10_by_symbol = _ma10_by_symbol([lot["ts_code"] for lot in state["lots"]], timestamp.date())
        plans: List[PlannedTrade] = []
        lot_by_symbol: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for lot in state["lots"]:
            lot_by_symbol[lot["ts_code"]].append(lot)
            quote = quotes.get(lot["ts_code"]) or {}
            price = _safe_float(quote.get("price"), 0.0) or 0.0
            if price <= 0 or lot["bought_at"].date() >= timestamp.date():
                continue
            pnl_pct = (price / lot["buy_price"] - 1) * 100 if lot["buy_price"] else 0.0
            held_days = _holding_days(lot["bought_at"], timestamp.date())
            reason_code = None
            quantity = lot["remaining_quantity"]
            hold_info = (state.get("hold_scores") or {}).get(lot["ts_code"])
            hold_score = hold_info.get("hold_score") if hold_info else None
            hold_sell_threshold = float(_safe_float(sp.get("hold_sell_threshold"), 30.0))
            peak = _safe_float(lot.get("peak_price"), lot["buy_price"]) or lot["buy_price"]
            ma10 = ma10_by_symbol.get(lot["ts_code"])
            last_rec = (state.get("last_recommended_dates") or {}).get(lot["ts_code"])
            if price >= lot["target_price"] and not (hold_enabled and hold_score is not None and hold_score >= 70):
                # AI 高评分(≥70=继续持有)时可延迟止盈，让错价修复的利润奔跑
                reason_code = "TARGET_PROFIT"
            elif (
                peak >= lot["target_price"]
                and ma10 is not None and price < ma10
                and (last_rec is None or last_rec < timestamp.date())
            ):
                # 移动止盈：目标价已触及后，现价跌破 MA10 且当日未被再推荐 → 离场
                reason_code = "TRAILING_STOP"
            elif pnl_pct <= stop_loss_full and fg >= 20:
                reason_code = "STOP_LOSS_FULL"
            elif pnl_pct <= stop_loss_half and not lot.get("stop_half_triggered"):
                reason_code = "STOP_LOSS_HALF"
                quantity = max(100, (quantity // 2 // 100) * 100)
            elif fg >= 80:
                reason_code = "EXTREME_GREED_EXIT"
            elif hold_enabled and hold_score is not None and hold_score <= hold_sell_threshold:
                # AI 低评分 = 倾向卖出（基本面/事件/板块转弱），作为规则外的补充信号
                reason_code = "AI_SIGNAL"
            elif held_days >= 30:
                reason_code = "MAX_HOLD_DAYS"
            if reason_code and quantity > 0:
                if reason_code == "TRAILING_STOP":
                    reason_text = f"TRAILING_STOP: 目标价 {lot['target_price']:.3f} 已触及，现价 {price:.3f} 跌破 MA10({ma10:.3f})，当日未获再推荐，移动止盈离场"
                else:
                    reason_text = f"{reason_code}: 现价 {price:.3f}，持仓收益 {pnl_pct:.2f}%"
                    if reason_code == "AI_SIGNAL" and hold_info and hold_info.get("reason"):
                        reason_text += f"；AI评估(hold_score={hold_score:.0f}): {hold_info['reason']}"
                plans.append(
                    PlannedTrade(
                        side="SELL",
                        ts_code=lot["ts_code"],
                        name=lot["name"],
                        price=price,
                        quantity=min(quantity, lot["remaining_quantity"]),
                        lot_id=lot["id"],
                        reason_code=reason_code,
                        reason=reason_text,
                        state_snapshot={"fear_greed": fg, "pnl_pct": pnl_pct, "held_days": held_days, "hold_score": hold_score},
                    )
                )

        # A sharp risk-appetite downgrade reduces sellable lots proportionally.
        previous_target = _safe_float(portfolio.get("last_execution_target"))
        if previous_target and previous_target - execution_target > 0.15:
            reduce_ratio = 1 - execution_target / previous_target
            already_selling_lots = {plan.lot_id for plan in plans if plan.side == "SELL"}
            for lot in state["lots"]:
                if lot["id"] in already_selling_lots or lot["bought_at"].date() >= timestamp.date():
                    continue
                price = _safe_float((quotes.get(lot["ts_code"]) or {}).get("price"), 0.0) or 0.0
                quantity = int((lot["remaining_quantity"] * reduce_ratio) // 100) * 100
                if price > 0 and quantity >= 100:
                    plans.append(
                        PlannedTrade(
                            side="SELL",
                            ts_code=lot["ts_code"],
                            name=lot["name"],
                            price=price,
                            quantity=quantity,
                            lot_id=lot["id"],
                            reason_code="ALLOCATION_DOWNSHIFT",
                            reason=f"贪恐目标仓位由 {previous_target:.0%} 下调至 {execution_target:.0%}",
                            state_snapshot={"fear_greed": fg, "previous_target": previous_target, "execution_target": execution_target},
                        )
                    )

        sell_value_by_symbol = defaultdict(float)
        for plan in plans:
            if plan.side == "SELL":
                sell_value_by_symbol[plan.ts_code] += plan.price * plan.quantity - _sell_fee(plan.price * plan.quantity)
        position_values = {
            symbol: sum(((_safe_float(quotes.get(symbol, {}).get("price"), 0.0) or 0.0) * lot["remaining_quantity"]) for lot in lots)
            for symbol, lots in lot_by_symbol.items()
        }
        equity_before = float(portfolio["cash"]) + sum(position_values.values())
        projected_cash = float(portfolio["cash"]) + sum(sell_value_by_symbol.values())
        projected_positions = {
            symbol: max(0.0, value - sum(plan.price * plan.quantity for plan in plans if plan.side == "SELL" and plan.ts_code == symbol))
            for symbol, value in position_values.items()
        }
        projected_positions = {symbol: value for symbol, value in projected_positions.items() if value > 0}
        slot_value = equity_before * execution_target / max(int(sp.get("slot_count", 5)), 1)

        planned_codes = {plan.ts_code for plan in plans if plan.side == "BUY"}
        position_count = len(projected_positions)
        entry_price_cap = 1.0 + _safe_float(sp.get("entry_price_cap_pct"), 1.0) / 100.0
        max_positions = max(int(sp.get("max_positions", 10)), 1)
        single_stock_cap = _safe_float(sp.get("single_stock_cap"), 0.20)
        # 分批建仓：当日买入次数上限（新建仓与加仓都计入）
        buys_today = int(state.get("today_buy_count", 0)) + len([p for p in plans if p.side == "BUY"])
        for recommendation in state["recommendations"]:
            if buys_today >= max_buys_per_day:
                break
            ts_code = recommendation["ts_code"]
            if ts_code in planned_codes or (ts_code, recommendation["id"]) in state["today_buys"]:
                continue
            quote = quotes.get(ts_code) or {}
            price = _safe_float(quote.get("price"), 0.0) or 0.0
            if price <= 0 or price > recommendation["recommendation_price"] * entry_price_cap:
                continue
            current_value = projected_positions.get(ts_code, 0.0)
            is_add = current_value > 0
            if is_add and current_value >= slot_value / 2:
                continue
            if not is_add and position_count >= max_positions:
                # 满仓换仓（两信号，先"未获再推荐"后"信心差"）：
                # 信号1：持仓已连续 N 个交易日未被再推荐（AI 已失去关注）→ 卖最久的那只
                # 信号2：新推荐信心与最低 hold_score 持仓的差值超阈值
                selling_lots = {plan.lot_id for plan in plans if plan.side == "SELL" and plan.lot_id}
                last_rec = state.get("last_recommended_dates") or {}
                today = timestamp.date()
                out_lot = None
                out_reason = ""
                out_state: Dict[str, Any] = {"fear_greed": fg}
                sellable = [
                    held for held in state["lots"]
                    if held["bought_at"].date() < today and held["id"] not in selling_lots
                ]
                # 信号1：最久未获再推荐
                if sellable:
                    def _stale_days(held):
                        rec = last_rec.get(held["ts_code"])
                        return (today - rec).days if rec else 9999
                    most_stale = max(sellable, key=_stale_days)
                    stale_days = _stale_days(most_stale)
                    if stale_days >= rotation_stale_days:
                        out_lot = most_stale
                        out_reason = f"轮换让位：已{stale_days}个交易日未获再推荐，为今日新推荐 {recommendation['name']}(#{recommendation['rank']}) 腾位"
                        out_state["stale_days"] = stale_days
                # 信号2：hold_score 信心差
                if out_lot is None and sellable:
                    rotation_candidates = []
                    for held in sellable:
                        hs = (state.get("hold_scores") or {}).get(held["ts_code"])
                        if hs is not None:
                            rotation_candidates.append((hs["hold_score"], held))
                    if rotation_candidates:
                        rotation_candidates.sort(key=lambda item: item[0])
                        out_score, candidate = rotation_candidates[0]
                        if recommendation["ai_confidence"] - out_score >= rotation_gap:
                            out_lot = candidate
                            out_reason = f"换仓：新推荐 {recommendation['name']} 信心 {recommendation['ai_confidence']:.1f} 高于 {candidate['name']} hold_score {out_score:.0f}"
                            out_state.update({"hold_score": out_score, "incoming_confidence": recommendation["ai_confidence"]})
                if out_lot is None:
                    continue
                out_price = _safe_float((quotes.get(out_lot["ts_code"]) or {}).get("price"), 0.0) or 0.0
                if out_price <= 0:
                    continue
                plans.append(
                    PlannedTrade(
                        side="SELL",
                        ts_code=out_lot["ts_code"],
                        name=out_lot["name"],
                        price=out_price,
                        quantity=out_lot["remaining_quantity"],
                        lot_id=out_lot["id"],
                        reason_code="ROTATION_OUT",
                        reason=out_reason,
                        state_snapshot=out_state,
                    )
                )
                projected_cash += out_price * out_lot["remaining_quantity"] - _sell_fee(out_price * out_lot["remaining_quantity"])
                projected_positions.pop(out_lot["ts_code"], None)
                position_count -= 1
            try:
                confirmed, minute_state = self.provider.minute_entry_confirmed(ts_code)
            except Exception as exc:
                logger.warning("AI stock minute confirmation failed for %s: %s", ts_code, exc)
                continue
            if not confirmed:
                continue
            max_single = equity_before * single_stock_cap
            amount = min(slot_value - current_value, max_single - current_value, projected_cash)
            quantity = int(max(0.0, amount) / price // 100) * 100
            if quantity < 100:
                continue
            amount = price * quantity
            fee = _buy_fee(amount)
            if amount + fee > projected_cash:
                quantity = int(max(0.0, projected_cash - fee) / price // 100) * 100
                amount = price * quantity
            if quantity < 100:
                continue
            plans.append(
                PlannedTrade(
                    side="BUY",
                    ts_code=ts_code,
                    name=recommendation["name"],
                    price=price,
                    quantity=quantity,
                    recommendation_id=recommendation["id"],
                    reason_code="AI_RECOMMENDATION_ENTRY",
                    reason=f"{'补仓·AI推荐' if is_add else 'AI推荐买入'}：AI 推荐 #{recommendation['rank']}，信心 {recommendation['ai_confidence']:.1f}，分钟趋势确认",
                    state_snapshot={"fear_greed": fg, "execution_target": execution_target, "minute": minute_state},
                )
            )
            projected_cash -= amount + _buy_fee(amount)
            projected_positions[ts_code] = current_value + amount
            if not is_add:
                position_count += 1
            buys_today += 1

        return self._commit_minute(timestamp, minute_key, fg, execution_target, quotes, plans)

    def _commit_minute(self, timestamp: datetime, minute_key: datetime, fear_greed: float, execution_target: float, quotes: Dict[str, Dict[str, Any]], plans: List[PlannedTrade]) -> Dict[str, Any]:
        with get_db_ctx() as db:
            portfolio = self._ensure_portfolio(db)
            if portfolio.last_processed_minute == minute_key:
                return {"processed": False, "reason": "该分钟已被其他工作者处理"}
            executed = []
            for plan in plans:
                amount = round(plan.price * plan.quantity, 2)
                fee = _buy_fee(amount) if plan.side == "BUY" else _sell_fee(amount)
                realized_pnl = None
                lot = db.get(AIStockPaperLot, plan.lot_id) if plan.lot_id else None
                if plan.side == "BUY":
                    if amount + fee > portfolio.cash:
                        continue
                    portfolio.cash = round(portfolio.cash - amount - fee, 2)
                    recommendation = db.get(AIStockRecommendation, plan.recommendation_id)
                    lot = AIStockPaperLot(
                        portfolio_id=portfolio.id,
                        recommendation_id=plan.recommendation_id,
                        ts_code=plan.ts_code,
                        name=plan.name,
                        bought_at=timestamp,
                        buy_price=plan.price,
                        quantity=plan.quantity,
                        remaining_quantity=plan.quantity,
                        target_price=recommendation.target_price if recommendation else plan.price * 1.05,
                        peak_price=plan.price,
                    )
                    db.add(lot)
                    db.flush()
                else:
                    if not lot or lot.remaining_quantity < plan.quantity or lot.bought_at.date() >= timestamp.date():
                        continue
                    lot.remaining_quantity -= plan.quantity
                    if plan.reason_code == "STOP_LOSS_HALF":
                        lot.stop_half_triggered = True
                    portfolio.cash = round(portfolio.cash + amount - fee, 2)
                    realized_pnl = round((plan.price - lot.buy_price) * plan.quantity - fee, 2)
                db.add(
                    AIStockPaperTrade(
                        portfolio_id=portfolio.id,
                        lot_id=lot.id if lot else None,
                        recommendation_id=plan.recommendation_id or (lot.recommendation_id if lot else None),
                        executed_at=timestamp,
                        trade_date=timestamp.date(),
                        ts_code=plan.ts_code,
                        name=plan.name,
                        side=plan.side,
                        price=plan.price,
                        quantity=plan.quantity,
                        amount=amount,
                        fee=fee,
                        realized_pnl=realized_pnl,
                        reason_code=plan.reason_code,
                        reason=plan.reason,
                        state_snapshot=plan.state_snapshot or {"fear_greed": fear_greed},
                    )
                )
                executed.append({"side": plan.side, "ts_code": plan.ts_code, "quantity": plan.quantity, "reason_code": plan.reason_code})

            active_lots = db.query(AIStockPaperLot).filter(AIStockPaperLot.portfolio_id == portfolio.id, AIStockPaperLot.remaining_quantity > 0).all()
            for active_lot in active_lots:
                current_price = _safe_float((quotes.get(active_lot.ts_code) or {}).get("price"), active_lot.buy_price) or active_lot.buy_price
                if active_lot.stop_half_triggered and current_price >= active_lot.buy_price * 0.95:
                    active_lot.stop_half_triggered = False
                # 移动止盈基准：逐分钟抬升已见最高价（初始等于成本价）
                current_peak = active_lot.peak_price if active_lot.peak_price is not None else active_lot.buy_price
                if current_price > current_peak:
                    active_lot.peak_price = current_price
            market_value = sum(((_safe_float(quotes.get(lot.ts_code, {}).get("price"), lot.buy_price) or lot.buy_price) * lot.remaining_quantity) for lot in active_lots)
            db.add(
                AIStockPaperEquity(
                    portfolio_id=portfolio.id,
                    recorded_at=minute_key,
                    cash=portfolio.cash,
                    market_value=round(market_value, 2),
                    total_equity=round(portfolio.cash + market_value, 2),
                )
            )
            portfolio.last_processed_minute = minute_key
            portfolio.last_execution_target = execution_target
            return {"processed": True, "trades": executed, "fear_greed": fear_greed}

    def strategy_config(self) -> Dict[str, Any]:
        """Return paper-trading strategy parameters (JSON column) in a short read."""
        with get_db_ctx() as db:
            config = self._ensure_strategy_config(db)
            return {
                "enabled": config.enabled,
                "parameters": dict(config.parameters or {}),
            }

    def update_strategy_config(self, *, updated_by: str, enabled: Optional[bool] = None, **parameters) -> Dict[str, Any]:
        """Persist paper-trading strategy parameters in one short transaction."""
        allowed = {
            "max_positions", "slot_count", "single_stock_cap", "max_execution_target",
            "entry_price_cap_pct", "stop_loss_half_pct", "stop_loss_full_pct",
            "trading_start_minute", "hold_evaluation_enabled", "hold_sell_threshold",
            "max_buys_per_day", "rotation_confidence_gap", "rotation_stale_days",
        }
        unknown = set(parameters) - allowed
        if unknown:
            raise ValueError(f"未知参数: {', '.join(sorted(unknown))}")
        for name, value in parameters.items():
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"{name} 必须是数字")
            if name in {"max_positions", "slot_count", "max_buys_per_day", "rotation_stale_days"}:
                if value < 1 or value > 1000 or value != int(value):
                    raise ValueError(f"{name} 必须是 1-1000 的整数")
            elif name in {"stop_loss_half_pct", "stop_loss_full_pct"}:
                if value < -100 or value > 0:
                    raise ValueError(f"{name} 必须是 -100~0 的百分比")
            elif name == "trading_start_minute":
                if value < 9 * 60 + 30 or value > 11 * 60 + 30 or value != int(value):
                    raise ValueError(f"{name} 必须是 570(9:30)~690(11:30) 的整数分钟")
            elif name == "hold_evaluation_enabled":
                value = bool(value)
            elif name == "hold_sell_threshold":
                if value < 0 or value > 100:
                    raise ValueError(f"{name} 必须是 0-100 的分数")
            elif name == "rotation_confidence_gap":
                if value < 0 or value > 100:
                    raise ValueError(f"{name} 必须是 0-100 的分数差")
            else:
                if value < 0 or value > 1.0:
                    raise ValueError(f"{name} 必须是 0-1 之间的比例")
            parameters[name] = value
        with get_db_ctx() as db:
            config = self._ensure_strategy_config(db)
            if enabled is not None:
                config.enabled = bool(enabled)
            merged = dict(config.parameters or {})
            merged.update(parameters)
            config.parameters = merged
            config.updated_by = updated_by
        return self.strategy_config()

    def overview(self) -> Dict[str, Any]:
        with get_db_ctx() as db:
            portfolio = self._ensure_portfolio(db)
            equity = db.query(AIStockPaperEquity).filter(AIStockPaperEquity.portfolio_id == portfolio.id).order_by(desc(AIStockPaperEquity.recorded_at)).first()
            total_equity = equity.total_equity if equity else portfolio.cash
            return {
                "portfolio_id": portfolio.id,
                "enabled": portfolio.enabled,
                "initial_cash": portfolio.initial_cash,
                "cash": portfolio.cash,
                "market_value": equity.market_value if equity else 0.0,
                "total_equity": total_equity,
                "total_pnl": round(total_equity - portfolio.initial_cash, 2),
                "total_return_pct": round((total_equity / portfolio.initial_cash - 1) * 100, 3) if portfolio.initial_cash else 0.0,
                "last_processed_minute": portfolio.last_processed_minute,
            }

    def positions(self) -> List[Dict[str, Any]]:
        with get_db_ctx() as db:
            portfolio = self._ensure_portfolio(db)
            lots = db.query(AIStockPaperLot).filter(AIStockPaperLot.portfolio_id == portfolio.id, AIStockPaperLot.remaining_quantity > 0).order_by(AIStockPaperLot.bought_at).all()
            # 短事务内复制成 dict 快照，避免 ORM 对象跨 session 访问（DetachedInstanceError）
            rows = [
                {
                    "ts_code": lot.ts_code,
                    "name": lot.name,
                    "remaining_quantity": lot.remaining_quantity,
                    "buy_price": lot.buy_price,
                    "target_price": lot.target_price,
                    "bought_at": lot.bought_at,
                }
                for lot in lots
            ]
        grouped = defaultdict(list)
        for row in rows:
            grouped[row["ts_code"]].append(row)
        symbols = list(grouped)
        quotes = self.provider.quotes(symbols) if symbols else {}
        result = []
        today = datetime.now().date()
        for symbol, items in grouped.items():
            quantity = sum(item["remaining_quantity"] for item in items)
            cost = sum(item["buy_price"] * item["remaining_quantity"] for item in items) / quantity
            quote = quotes.get(symbol) or {}
            price = _safe_float(quote.get("price"), cost) or cost
            target_price = max(item["target_price"] for item in items)
            result.append(
                {
                    "ts_code": symbol,
                    "name": items[0]["name"],
                    "quantity": quantity,
                    "sellable_quantity": sum(item["remaining_quantity"] for item in items if item["bought_at"].date() < today),
                    "cost": round(cost, 3),
                    "price": price,
                    "market_value": round(price * quantity, 2),
                    "pnl_pct": round((price / cost - 1) * 100, 3) if cost else 0.0,
                    "target_price": target_price,
                    "held_days": max(_holding_days(item["bought_at"], today) for item in items),
                }
            )
        return sorted(result, key=lambda item: -item["market_value"])

    def hold_evaluations(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Recent AI hold evaluations, newest first (short read, no external IO)."""
        safe_limit = min(max(1, int(limit)), 500)
        with get_db_ctx() as db:
            portfolio = self._ensure_portfolio(db)
            rows = (
                db.query(AIStockHoldEvaluation)
                .filter(AIStockHoldEvaluation.portfolio_id == portfolio.id)
                .order_by(desc(AIStockHoldEvaluation.evaluated_at), desc(AIStockHoldEvaluation.id))
                .limit(safe_limit)
                .all()
            )
            return [
                {
                    "id": row.id,
                    "ts_code": row.ts_code,
                    "name": row.name,
                    "hold_score": row.hold_score,
                    "reason": row.reason,
                    "evaluated_at": row.evaluated_at,
                }
                for row in rows
            ]

    def paper_statistics(self) -> Dict[str, Any]:
        """胜率统计：按已平仓卖出事件汇总（每笔 SELL 为一个已实现盈亏事件）。"""
        with get_db_ctx() as db:
            portfolio = self._ensure_portfolio(db)
            sells = (
                db.query(AIStockPaperTrade)
                .filter(AIStockPaperTrade.portfolio_id == portfolio.id, AIStockPaperTrade.side == "SELL")
                .order_by(AIStockPaperTrade.executed_at)
                .all()
            )
            lots = db.query(AIStockPaperLot).filter(AIStockPaperLot.portfolio_id == portfolio.id).all()
            bought_at = {lot.id: lot.bought_at for lot in lots}
            # 短事务内复制成普通快照，避免 ORM 对象跨 session 访问（DetachedInstanceError）
            sells_snapshot = [
                {"realized_pnl": s.realized_pnl, "executed_at": s.executed_at, "lot_id": s.lot_id}
                for s in sells
            ]

        total = len(sells_snapshot)
        wins = [s for s in sells_snapshot if (s["realized_pnl"] or 0) > 0]
        losses = [s for s in sells_snapshot if (s["realized_pnl"] or 0) < 0]
        total_pnl = round(sum((s["realized_pnl"] or 0) for s in sells_snapshot), 2)
        gross_win = sum((s["realized_pnl"] or 0) for s in wins)
        gross_loss = abs(sum((s["realized_pnl"] or 0) for s in losses))
        holding_days = [
            max(0, (s["executed_at"].date() - bought_at[s["lot_id"]].date()).days)
            for s in sells_snapshot
            if s["lot_id"] and s["lot_id"] in bought_at
        ]
        return {
            "closed_trades": total,
            "win_trades": len(wins),
            "loss_trades": len(losses),
            "win_rate_pct": round(len(wins) / total * 100, 2) if total else None,
            "total_realized_pnl": total_pnl,
            "avg_win_pnl": round(sum((s["realized_pnl"] or 0) for s in wins) / len(wins), 2) if wins else None,
            "avg_loss_pnl": round(sum((s["realized_pnl"] or 0) for s in losses) / len(losses), 2) if losses else None,
            "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
            "avg_holding_days": round(sum(holding_days) / len(holding_days), 1) if holding_days else None,
        }

    def trades(self, page: int = 1, page_size: int = 50) -> Dict[str, Any]:
        safe_page = max(1, int(page))
        safe_size = min(max(1, int(page_size)), 200)
        with get_db_ctx() as db:
            query = db.query(AIStockPaperTrade).filter(AIStockPaperTrade.portfolio_id == 1)
            total = query.count()
            rows = query.order_by(desc(AIStockPaperTrade.executed_at)).offset((safe_page - 1) * safe_size).limit(safe_size).all()
            return {
                "total": total,
                "items": [
                    {
                        "id": row.id,
                        "executed_at": row.executed_at,
                        "ts_code": row.ts_code,
                        "name": row.name,
                        "side": row.side,
                        "price": row.price,
                        "quantity": row.quantity,
                        "amount": row.amount,
                        "fee": row.fee,
                        "realized_pnl": row.realized_pnl,
                        "reason_code": row.reason_code,
                        "reason": row.reason,
                    }
                    for row in rows
                ],
            }

    def equity_curve(self) -> List[Dict[str, Any]]:
        with get_db_ctx() as db:
            rows = db.query(AIStockPaperEquity).filter(AIStockPaperEquity.portfolio_id == 1).order_by(AIStockPaperEquity.recorded_at).limit(2000).all()
            return [{"recorded_at": row.recorded_at, "total_equity": row.total_equity, "cash": row.cash, "market_value": row.market_value} for row in rows]


def _nested_payload_rows(payload: Any, keys: Tuple[str, ...]) -> List[Dict[str, Any]]:
    """Tolerate the reference site's API envelope without relying on one shape."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    queue: List[Any] = [payload]
    seen = set()
    while queue:
        value = queue.pop(0)
        if id(value) in seen:
            continue
        seen.add(id(value))
        if isinstance(value, dict):
            for key in keys:
                candidate = value.get(key)
                if isinstance(candidate, list):
                    return [item for item in candidate if isinstance(item, dict)]
            queue.extend(child for child in value.values() if isinstance(child, (dict, list)))
        elif isinstance(value, list):
            if value and all(isinstance(item, dict) for item in value):
                return value
            queue.extend(value)
    return []


def _nested_numeric(payload: Any, names: Tuple[str, ...]) -> Optional[float]:
    if isinstance(payload, dict):
        for name in names:
            value = _safe_float(payload.get(name))
            if value is not None:
                return value
        for value in payload.values():
            found = _nested_numeric(value, names)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _nested_numeric(value, names)
            if found is not None:
                return found
    return None


def _return_and_drawdown(equity_values: List[float]) -> Tuple[Optional[float], Optional[float]]:
    values = [float(value) for value in equity_values if _safe_float(value) and float(value) > 0]
    if len(values) < 2:
        return None, None
    total_return = (values[-1] / values[0] - 1) * 100
    peak = values[0]
    max_drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        max_drawdown = min(max_drawdown, (value / peak - 1) * 100)
    return round(total_return, 3), round(abs(max_drawdown), 3)


class AIStockBenchmarkCollector:
    """Optional read-only collector for the reference site's published AI endpoints."""

    def __init__(self):
        self.base_url = (os.getenv("AI_STOCK_BENCHMARK_URL") or "https://sai.fanlyun.com").rstrip("/")
        self.phone = (os.getenv("AI_STOCK_BENCHMARK_PHONE") or "").strip()
        self.password = (os.getenv("AI_STOCK_BENCHMARK_PASSWORD") or "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.phone and self.password)

    def collect(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        if not self.configured:
            return {"configured": False, "collected": False, "message": "未配置目标站只读凭据"}
        session = requests.Session()
        try:
            login = session.post(
                f"{self.base_url}/api/v1/users/auth/login",
                json={"phone": self.phone, "password": self.password},
                timeout=30,
            )
            login.raise_for_status()
            payload = login.json()
            token = payload.get("access_token") or (payload.get("data") or {}).get("access_token")
            if token:
                session.headers["Authorization"] = f"Bearer {token}"
            recommendations = session.get(f"{self.base_url}/api/v1/ai/recommendations", params={"top_n": 10, "rec_type": "INTRADAY"}, timeout=30)
            recommendations.raise_for_status()
            sim = session.get(f"{self.base_url}/api/v1/ai/sim-trading/overview", timeout=30)
            sim.raise_for_status()
            snapshots = [("recommendations", recommendations.json(), "SUCCESS", None), ("sim_overview", sim.json(), "SUCCESS", None)]
            for snapshot_type, path in (
                ("recommendation_history", "/api/v1/ai/recommendations/history"),
                ("recommendation_history_dates", "/api/v1/ai/recommendations/history-dates"),
                ("recommendation_intraday_batches", "/api/v1/ai/recommendations/intraday-batches"),
                ("recommendation_hit_rate", "/api/v1/ai/recommendations/hit-rate"),
                ("sim_positions", "/api/v1/ai/sim-trading/positions"),
                ("sim_trades", "/api/v1/ai/sim-trading/trades"),
                ("sim_equity_curve", "/api/v1/ai/sim-trading/equity-curve"),
            ):
                try:
                    response = session.get(f"{self.base_url}{path}", timeout=30)
                    response.raise_for_status()
                    snapshots.append((snapshot_type, response.json(), "SUCCESS", None))
                except (requests.RequestException, ValueError) as exc:
                    # A single unavailable comparison endpoint must not discard
                    # the independently useful recommendation snapshot.
                    snapshots.append((snapshot_type, {}, "FAILED", str(exc)[:1000]))
        except (requests.RequestException, ValueError) as exc:
            with get_db_ctx() as db:
                db.add(AIStockBenchmarkSnapshot(captured_at=_now(now), trade_date=_now(now).date(), snapshot_type="collection", payload={}, status="FAILED", message=str(exc)[:1000]))
            return {"configured": True, "collected": False, "message": str(exc)}
        with get_db_ctx() as db:
            for snapshot_type, payload, status, message in snapshots:
                db.add(AIStockBenchmarkSnapshot(captured_at=_now(now), trade_date=_now(now).date(), snapshot_type=snapshot_type, payload=payload, status=status, message=message))
        return {"configured": True, "collected": True, "types": [item[0] for item in snapshots]}

    def status(self) -> Dict[str, Any]:
        with get_db_ctx() as db:
            latest = db.query(AIStockBenchmarkSnapshot).order_by(desc(AIStockBenchmarkSnapshot.captured_at)).first()
            return {"configured": self.configured, "last_captured_at": latest.captured_at if latest else None, "last_status": latest.status if latest else None, "last_message": latest.message if latest else None}


def ai_stock_runtime_logs(limit: int = 100) -> List[Dict[str, Any]]:
    """Return concise persisted events for administrator diagnosis."""
    safe_limit = min(max(int(limit), 1), 300)
    with get_db_ctx() as db:
        runs = db.query(AIStockRecommendationRun).order_by(desc(AIStockRecommendationRun.run_at)).limit(safe_limit).all()
        snapshots = db.query(AIStockBenchmarkSnapshot).order_by(desc(AIStockBenchmarkSnapshot.captured_at)).limit(safe_limit).all()
        trades = db.query(AIStockPaperTrade).order_by(desc(AIStockPaperTrade.executed_at)).limit(safe_limit).all()
        events = [
            {
                "time": row.run_at,
                "category": "RECOMMENDATION",
                "status": row.status,
                "message": row.error_message or f"{row.run_type} 批次完成",
                "reference_id": row.id,
            }
            for row in runs
        ] + [
            {
                "time": row.captured_at,
                "category": "BENCHMARK",
                "status": row.status,
                "message": row.message or f"已采集 {row.snapshot_type}",
                "reference_id": row.id,
            }
            for row in snapshots
        ] + [
            {
                "time": row.executed_at,
                "category": "PAPER_TRADE",
                "status": "SUCCESS",
                "message": f"{row.side} {row.ts_code}：{row.reason_code}",
                "reference_id": row.id,
            }
            for row in trades
        ]
    return sorted(events, key=lambda item: item["time"], reverse=True)[:safe_limit]


RUN_TYPE_LABELS = {"PREOPEN": "竞价 9:26", "OPENING": "开盘 9:40", "INTRADAY": "盘中"}


def _aggregate_hit_metrics(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """按绝对收益（未扣费、未对比大盘）聚合一批推荐的命中指标。"""
    same_day_hits = same_day_total = 0
    next_day_hits = next_day_total = 0
    target_hits = target_total = 0
    week_hits = week_total = 0
    next_day_returns: List[float] = []
    week_returns: List[float] = []
    for rec in records:
        if rec["same_day_close"] is not None:
            same_day_total += 1
            if rec["same_day_close"] > rec["entry_price"]:
                same_day_hits += 1
        if rec["next_day_close"] is not None:
            next_day_total += 1
            if rec["next_day_close"] > rec["entry_price"]:
                next_day_hits += 1
            next_day_returns.append(rec["next_day_close"] / rec["entry_price"] - 1)
        if rec["window_high"] is not None:
            target_total += 1
            if rec["window_high"] >= rec["target_price"]:
                target_hits += 1
        if rec["week_close"] is not None:
            week_total += 1
            if rec["week_close"] > rec["entry_price"]:
                week_hits += 1
            week_returns.append(rec["week_close"] / rec["entry_price"] - 1)
    return {
        "count": len(records),
        "same_day_hit_pct": round(same_day_hits / same_day_total * 100, 1) if same_day_total else None,
        "next_day_hit_pct": round(next_day_hits / next_day_total * 100, 1) if next_day_total else None,
        "target_hit_pct": round(target_hits / target_total * 100, 1) if target_total else None,
        "week_hit_pct": round(week_hits / week_total * 100, 1) if week_total else None,
        "week_evaluable_count": week_total,
        "avg_next_day_return_pct": round(sum(next_day_returns) / len(next_day_returns) * 100, 2) if next_day_returns else None,
        "avg_week_return_pct": round(sum(week_returns) / len(week_returns) * 100, 2) if week_returns else None,
    }


def evaluate_recommendation_hit_rate(window_days: int = 20, now: Optional[datetime] = None) -> Dict[str, Any]:
    """计算近 N 个交易日推荐命中率（当日/次日/触目标/一周），并持久化快照。"""
    timestamp = _now(now)
    window_start = timestamp.date() - timedelta(days=max(1, int(window_days)) * 2)
    with get_db_ctx() as db:
        rows = (
            db.query(AIStockRecommendation, AIStockRecommendationRun)
            .join(AIStockRecommendationRun, AIStockRecommendation.run_id == AIStockRecommendationRun.id)
            .filter(
                AIStockRecommendationRun.status == "SUCCESS",
                AIStockRecommendationRun.trade_date >= window_start,
                AIStockRecommendationRun.trade_date <= timestamp.date(),
            )
            .all()
        )
        recs = [
            {
                "ts_code": rec.ts_code,
                "entry_price": _safe_float(rec.recommendation_price, 0.0) or 0.0,
                "target_price": _safe_float(rec.target_price, 0.0) or 0.0,
                "trade_date": run.trade_date,
                "run_type": run.run_type,
            }
            for rec, run in rows
        ]
    if not recs:
        return {"evaluated": False, "window_start": window_start.isoformat(), "window_end": timestamp.date().isoformat(), "total_count": 0, "summary": None, "by_type": []}

    frame = AIStockDataProvider().tushare.get_a_stock_daily_range_frame(window_start, timestamp.date())
    by_symbol: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    if frame is not None and not frame.empty:
        for _, daily in frame.iterrows():
            ts_code = _normalize_ts_code(daily.get("ts_code"))
            if ts_code:
                by_symbol[ts_code].append(
                    {
                        "trade_date": daily.get("trade_date"),
                        "close": _safe_float(daily.get("close")),
                        "high": _safe_float(daily.get("high")),
                    }
                )

    evaluated: List[Dict[str, Any]] = []
    for rec in recs:
        entry = rec["entry_price"]
        if entry <= 0:
            continue
        observations = sorted(
            [r for r in by_symbol.get(rec["ts_code"], []) if r.get("trade_date") and r["trade_date"] >= rec["trade_date"]],
            key=lambda r: r["trade_date"],
        )
        same_day = next((r for r in observations if r["trade_date"] == rec["trade_date"]), None)
        later = [r for r in observations if r["trade_date"] > rec["trade_date"]]
        five_day = observations[:5]
        week = observations[5] if len(observations) >= 6 else None  # D+5 收盘
        evaluated.append(
            {
                "run_type": rec["run_type"],
                "entry_price": entry,
                "target_price": rec["target_price"],
                "same_day_close": same_day["close"] if same_day else None,
                "next_day_close": later[0]["close"] if later else None,
                "window_high": max((r["high"] for r in five_day if r["high"]), default=None),
                "week_close": week["close"] if week else None,
            }
        )

    by_type = []
    for run_type, label in RUN_TYPE_LABELS.items():
        type_records = [r for r in evaluated if r["run_type"] == run_type]
        if type_records:
            by_type.append({"run_type": run_type, "label": label, **_aggregate_hit_metrics(type_records)})

    payload = {
        "window_start": window_start.isoformat(),
        "window_end": timestamp.date().isoformat(),
        "evaluated_at": timestamp.isoformat(),
        "total_count": len(evaluated),
        "summary": _aggregate_hit_metrics(evaluated),
        "by_type": by_type,
    }
    with get_db_ctx() as db:
        db.add(
            AIStockRecommendationHitRate(
                evaluated_at=timestamp,
                window_start=window_start,
                window_end=timestamp.date(),
                total_count=len(evaluated),
                payload=payload,
            )
        )
        db.flush()
    return payload


def latest_recommendation_hit_rate() -> Optional[Dict[str, Any]]:
    """返回最近一次推荐命中率快照（供前端展示）。"""
    with get_db_ctx() as db:
        row = db.query(AIStockRecommendationHitRate).order_by(desc(AIStockRecommendationHitRate.evaluated_at)).first()
        return row.payload if row else None


def _shanghai_index_values(start_date: date, end_date: date) -> List[float]:
    """上证指数(000001.SH)日收盘价序列，用于对标。"""
    try:
        frame = TushareService.get_instance().get_index_daily_range_frame("000001.SH", start_date, end_date)
        if frame is None or frame.empty or "close" not in frame.columns:
            return []
        return [float(value) for value in frame["close"] if _safe_float(value)]
    except Exception as exc:
        logger.warning("Benchmark 上证指数 fetch failed: %s", exc)
        return []


_daily_bars_cache: Dict[date, Dict[str, List[Dict[str, Any]]]] = {}
_daily_bars_lock = threading.Lock()


def _daily_bars_by_symbol(as_of: date) -> Dict[str, List[Dict[str, Any]]]:
    """按交易日缓存的全市场日线（close），供 MA10 等日内计算复用。"""
    with _daily_bars_lock:
        cached = _daily_bars_cache.get(as_of)
    if cached is not None:
        return cached
    start = as_of - timedelta(days=45)
    try:
        frame = AIStockDataProvider().tushare.get_a_stock_daily_range_frame(start, as_of)
    except Exception as exc:
        logger.warning("Daily bars fetch failed for MA10: %s", exc)
        return {}
    by_symbol: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    if frame is not None and not frame.empty:
        for _, daily in frame.iterrows():
            ts_code = _normalize_ts_code(daily.get("ts_code"))
            if ts_code:
                by_symbol[ts_code].append({"trade_date": daily.get("trade_date"), "close": _safe_float(daily.get("close"))})
    with _daily_bars_lock:
        _daily_bars_cache[as_of] = by_symbol
    return by_symbol


def _ma10_by_symbol(codes: Iterable[str], as_of: date) -> Dict[str, float]:
    """每只代码的 10 日均线（取 as_of 之前已收盘的最近 10 个交易日收盘价均值）。"""
    by_symbol = _daily_bars_by_symbol(as_of)
    result: Dict[str, float] = {}
    for ts_code in codes:
        closes = [
            row["close"]
            for row in by_symbol.get(ts_code, [])
            if row.get("close") and row.get("trade_date") and row["trade_date"] < as_of
        ]
        if len(closes) >= 10:
            result[ts_code] = round(sum(closes[-10:]) / 10, 3)
    return result


# 只取行业/主题/板块类贪恐：排除宽基、策略(红利/低波)指数。
# 注：自编指数 A创100(INNO100.CN) 在服务层单独定义，不在 A_STOCK_INDEX_FEAR_GREED_TARGETS 里。
A_STOCK_FEAR_GREED_EXCLUDED_SYMBOLS = {
    "000300.SH",    # 沪深300
    "000510.SH",    # 中证A500
    "000905.SH",    # 中证500
    "000985.SH",    # 中证全指
    "899050.BJ",    # 北证50
    "000688.SH",    # 科创50
    "000698.SH",    # 科创100
    "000699.SH",    # 科创200
    "399006.SZ",    # 创业板指
    "000015.SH",    # 上证红利（策略/风格）
    "H30269.CSI",   # 红利低波（策略/风格）
}


def _classify_theme_stage(score: Optional[float], trend_5d: Optional[float]) -> Optional[str]:
    """按贪恐值(0-100) + 近5日变化分类主题生命周期阶段。

    启动(恐慌错杀后修复)/扩散(资金流入、上涨扩散)/主线(持续强势)/拥挤(贪婪过热)/
    退潮(从高位回落)/探底(恐慌延续、尚未企稳)/中性(无明确方向)。
    """
    if score is None:
        return None
    trend = trend_5d if trend_5d is not None else 0.0
    if score >= 70:
        return "拥挤"
    if score <= 35:
        # 恐慌区：回升=启动；继续走弱=探底（不是退潮）
        return "启动" if trend >= 0 else "探底"
    if trend > 5:
        return "主线" if score >= 50 else "扩散"
    if trend < -5:
        # 从非恐慌区明显回落才是退潮
        return "退潮"
    return "中性"


def _a_stock_fear_greed_reference() -> List[Dict[str, Any]]:
    """各行业/主题/板块最新恐慌贪婪指数 + 生命周期阶段。

    供 AI 在选股步骤参考：启动/主线敢买，拥挤谨慎，退潮回避。
    只取行业/主题/板块类指数，排除宽基与策略指数。
    """
    try:
        from ...robot.a_stock_base_data_config import A_STOCK_INDEX_FEAR_GREED_TARGETS
    except Exception as exc:
        logger.warning("Failed to import A-share fear-greed targets: %s", exc)
        return []
    label_by_symbol = {
        str(item["symbol"]).upper(): (item.get("ticker") or item.get("label") or item.get("index_name") or str(item["symbol"]))
        for item in A_STOCK_INDEX_FEAR_GREED_TARGETS
        if str(item["symbol"]).upper() not in A_STOCK_FEAR_GREED_EXCLUDED_SYMBOLS
    }
    symbols = list(label_by_symbol)
    try:
        with get_db_ctx() as db:
            rows = (
                db.query(ETFFearGreedCloneHistory)
                .filter(ETFFearGreedCloneHistory.symbol.in_(symbols))
                .order_by(ETFFearGreedCloneHistory.symbol, desc(ETFFearGreedCloneHistory.date))
                .all()
            )
            # 短事务内取每个 symbol 最近 6 个日频分（date 已倒序）
            series: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for row in rows:
                if len(series[row.symbol]) < 6:
                    series[row.symbol].append({"score": float(row.score), "rating": row.rating})
        reference = []
        for sym in symbols:
            if sym not in series:
                continue
            items = series[sym]
            current = items[0]["score"]
            prev_score = items[1]["score"] if len(items) >= 2 else None
            if len(items) >= 6:
                trend_5d = round(current - items[5]["score"], 2)
            elif len(items) >= 2:
                trend_5d = round(current - items[-1]["score"], 2)
            else:
                trend_5d = None
            reference.append(
                {
                    "name": label_by_symbol[sym],
                    "fear_greed": round(current, 1),
                    "rating": items[0]["rating"],
                    "prev_score": round(prev_score, 1) if prev_score is not None else None,
                    "trend_5d": trend_5d,
                    "stage": _classify_theme_stage(current, trend_5d),
                }
            )
        return sorted(reference, key=lambda item: item["name"])
    except Exception as exc:
        logger.warning("Failed to load A-share fear-greed reference: %s", exc)
        return []


def evaluate_ai_stock_benchmark(window_days: int = 20, now: Optional[datetime] = None) -> Dict[str, Any]:
    """对标上证指数：对比模拟盘净值与 000001.SH 同窗收益率/回撤。"""
    timestamp = _now(now)
    window_start = timestamp.date() - timedelta(days=max(1, int(window_days)) - 1)
    curve_start = timestamp - timedelta(days=max(10, int(window_days) * 2))
    with get_db_ctx() as db:
        system_curve_rows = (
            db.query(AIStockPaperEquity)
            .filter(AIStockPaperEquity.portfolio_id == 1, AIStockPaperEquity.recorded_at >= curve_start)
            .order_by(AIStockPaperEquity.recorded_at)
            .all()
        )
        portfolio = db.get(AIStockPaperPortfolio, 1)
    # 系统净值按交易日对齐（取每日最后一个点），与指数日频可比
    system_daily: Dict[date, float] = {}
    for row in system_curve_rows:
        system_daily[row.recorded_at.date()] = row.total_equity
    system_values = [system_daily[day] for day in sorted(system_daily)]
    if len(system_values) == 1 and portfolio:
        system_values.insert(0, portfolio.initial_cash)
    system_return, system_drawdown = _return_and_drawdown(system_values)

    benchmark_values = _shanghai_index_values(curve_start.date(), timestamp.date())
    benchmark_return, benchmark_drawdown = _return_and_drawdown(benchmark_values)

    gates = {
        "net_return": system_return is not None and benchmark_return is not None and system_return >= benchmark_return,
        "drawdown": system_drawdown is not None and benchmark_drawdown is not None and system_drawdown <= benchmark_drawdown + 2,
    }
    passed = all(gates.values()) if gates else False
    with get_db_ctx() as db:
        evaluation = AIStockEvaluation(
            evaluated_at=timestamp,
            window_start=window_start,
            window_end=timestamp.date(),
            theme_overlap_pct=None,
            stock_overlap_pct=None,
            system_return_pct=system_return,
            benchmark_return_pct=benchmark_return,
            system_max_drawdown_pct=system_drawdown,
            benchmark_max_drawdown_pct=benchmark_drawdown,
            passed=passed,
            details={
                "benchmark": "上证指数(000001.SH)",
                "system_curve_points": len(system_values),
                "benchmark_curve_points": len(benchmark_values),
                "gates": gates,
                "performance_status": "完整" if all(value is not None for value in (system_return, benchmark_return, system_drawdown, benchmark_drawdown)) else "待积累完整模拟盘曲线",
            },
        )
        db.add(evaluation)
        db.flush()
        return {"id": evaluation.id, "window_start": window_start, "window_end": timestamp.date(), "theme_overlap_pct": None, "stock_overlap_pct": None, "passed": passed, "details": evaluation.details}

"""行业关联：把个股盘中状态按申万一/二/三级行业聚合，看资金在往哪个板块走。

数据来源与调用预算：

- 行业骨架：申万三级分类、当前成分、成分变更历史与行业指数日线，都由「A股基础数据同步」
  任务统一落到 DuckDB（见 robot/a_stock_base_data_sync.sync_sw_industry_data），本模块只读不拉。
- 个股盘中状态：复用提示看板每分钟那一次 `rt_k` 全市场快照与同一套四档标签，增量调用为 0。
- 连板：DuckDB `a_stock_market_daily.limit_status` 的历史连续涨停天数，盘前算一次。

排序沿用参考站点的经验公式（情绪分 / 综合分），等提示看板攒够命中后涨幅数据，
再用实际收益验证哪种打分更能预测后续表现。
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from .duckdb_analytics import connect_analytics_db, safe_float
from .market_alerts import (
    LABEL_ACTIVE,
    LABEL_AVOID,
    LABEL_STRONG,
    LABEL_WATCH,
    AlertThresholds,
    DEFAULT_THRESHOLDS,
    StockStructure,
    baseline_at,
    classify,
)

logger = logging.getLogger(__name__)

SW_SOURCE = "SW2021"
MEMBER_PAGE_SIZE = 3000
LEVELS = ("l1", "l2", "l3")
CACHE_TTL_SECONDS = 30
LIMIT_UP_STATUS = (2, 3)
MIN_RANK_COUNT = 5        # 成分股少于此数的行业不参与排名，避免小样本噪声顶上榜首

_cache: Dict[str, Tuple[float, Any]] = {}


class IndustryRelationDataError(RuntimeError):
    pass


# ---------- 聚合 ----------

def load_members(connection, as_of: Optional[date] = None) -> pd.DataFrame:
    """行业成分。as_of 为空取当前归属；给了日期就用变更历史还原那天的归属。"""
    if as_of is None:
        frame = connection.execute(
            "SELECT ts_code, l1_name, l2_name, l3_name FROM a_stock_sw_member WHERE l1_name <> ''"
        ).fetchdf()
    else:
        # 变更历史按 index_code 记录，用三级行业的 index_code 反查一/二级名称
        frame = connection.execute(
            """
            SELECT c.con_code AS ts_code,
                   l1.industry_name AS l1_name,
                   l2.industry_name AS l2_name,
                   l3.industry_name AS l3_name
            FROM a_stock_sw_member_change c
            JOIN a_stock_sw_industry l3 ON l3.index_code = c.index_code AND l3.level = 'L3'
            LEFT JOIN a_stock_sw_industry l2 ON l2.industry_code = l3.parent_code AND l2.level = 'L2'
            LEFT JOIN a_stock_sw_industry l1 ON l1.industry_code = l2.parent_code AND l1.level = 'L1'
            WHERE c.in_date <= ? AND (c.out_date IS NULL OR c.out_date > ?)
            """,
            [as_of, as_of],
        ).fetchdf()
    if frame.empty:
        raise IndustryRelationDataError("分析库没有申万成分股，请先跑「A股基础数据同步」任务")
    return frame


def load_industry_daily(connection, ts_codes: List[str], days: int = 60) -> pd.DataFrame:
    """申万行业指数日线，用于行业历史走势与估值。"""
    if not ts_codes:
        return pd.DataFrame()
    placeholders = ",".join("?" for _ in ts_codes)
    return connection.execute(
        f"""
        SELECT ts_code, trade_date, name, close, pct_chg_placeholder, vol, amount, pe, pb
        FROM (
            SELECT ts_code, trade_date, name, close,
                   close / NULLIF(LAG(close) OVER (PARTITION BY ts_code ORDER BY trade_date), 0) - 1
                       AS pct_chg_placeholder,
                   vol, amount, pe, pb
            FROM a_stock_sw_daily
            WHERE ts_code IN ({placeholders})
        )
        ORDER BY ts_code, trade_date DESC
        LIMIT ?
        """,
        [*ts_codes, days * max(1, len(ts_codes))],
    ).fetchdf()


def load_board_counts(connection, today: date, lookback_days: int = 20) -> Dict[str, int]:
    """每只股票截至上一交易日的连续涨停天数（用于首板/二板/多板）。"""
    frame = connection.execute(
        """
        SELECT ts_code, trade_date, limit_status
        FROM a_stock_market_daily
        WHERE trade_date >= ? AND trade_date < ?
        ORDER BY ts_code, trade_date
        """,
        [today - timedelta(days=lookback_days * 2), today],
    ).fetchdf()
    if frame.empty:
        return {}
    counts: Dict[str, int] = {}
    for ts_code, group in frame.groupby("ts_code"):
        streak = 0
        for status in reversed(group["limit_status"].tolist()):
            if status in LIMIT_UP_STATUS:
                streak += 1
            else:
                break
        if streak:
            counts[str(ts_code)] = streak
    return counts


def sentiment_score(row: Dict[str, Any]) -> float:
    """情绪分 = 涨停×10 + 首板×6 + 二板×9 + 多板×12 − 跌停×10 + 上涨占比×30 + 涨幅×2，裁到 0~100。"""
    count = max(1, row["count"])
    raw = (
        row["lu"] * 10 + row["fb"] * 6 + row["eb"] * 9 + row["lb3"] * 12 - row["ld"] * 10
        + row["up"] / count * 30 + (row["pct"] or 0) * 2
    )
    return round(max(0.0, min(100.0, raw)), 1)


def composite_score(row: Dict[str, Any], max_amount: float) -> float:
    """综合分 = 涨幅分×45% + 量能分×25% + 情绪分×30%。"""
    pct_score = max(0.0, min(100.0, ((row["pct"] or 0) + 5) / 15 * 100))
    volume_score = min(100.0, (row["amount_yi"] / max_amount * 100) if max_amount else 0.0)
    return round(pct_score * 0.45 + volume_score * 0.25 + row["sentiment"] * 0.30, 1)


def aggregate_by_level(
    stocks: List[Dict[str, Any]],
    level: str,
    min_count: int = MIN_RANK_COUNT,
) -> List[Dict[str, Any]]:
    """把个股行聚合到某一级行业。stocks 每行需含 l1_name/l2_name/l3_name 与盘中状态。

    成分股少于 min_count 的行业仍然返回，但不参与排名（rank=None）：三级行业里有
    只有两三只成分股的小类，一只涨停就能把整个行业顶到榜首，那不是板块效应。
    """
    parent_key = {"l1": None, "l2": "l1_name", "l3": "l2_name"}[level]
    name_key = f"{level}_name"
    buckets: Dict[str, Dict[str, Any]] = {}
    for stock in stocks:
        name = stock.get(name_key)
        if not name:
            continue
        bucket = buckets.setdefault(name, {
            "name": name,
            "parent": stock.get(parent_key) if parent_key else "",
            "count": 0, "up": 0, "down": 0, "lu": 0, "ld": 0,
            "fb": 0, "eb": 0, "lb3": 0,
            "st": 0, "by": 0, "gw": 0, "av": 0,
            "_pct_sum": 0.0, "amount_yi": 0.0,
        })
        bucket["count"] += 1
        pct = stock.get("pct") or 0.0
        bucket["_pct_sum"] += pct
        bucket["amount_yi"] += stock.get("amount_yi") or 0.0
        if pct > 0:
            bucket["up"] += 1
        elif pct < 0:
            bucket["down"] += 1
        if stock.get("limit_up"):
            bucket["lu"] += 1
            boards = stock.get("boards") or 1
            if boards <= 1:
                bucket["fb"] += 1
            elif boards == 2:
                bucket["eb"] += 1
            else:
                bucket["lb3"] += 1
        if stock.get("limit_down"):
            bucket["ld"] += 1
        label = stock.get("label")
        if label == LABEL_STRONG:
            bucket["st"] += 1
        elif label == LABEL_ACTIVE:
            bucket["by"] += 1
        elif label == LABEL_WATCH:
            bucket["gw"] += 1
        elif label == LABEL_AVOID:
            bucket["av"] += 1

    rows = []
    for bucket in buckets.values():
        bucket["pct"] = round(bucket["_pct_sum"] / bucket["count"], 2)
        bucket["amount_yi"] = round(bucket["amount_yi"], 2)
        bucket.pop("_pct_sum")
        bucket["sentiment"] = sentiment_score(bucket)
        rows.append(bucket)
    max_amount = max((row["amount_yi"] for row in rows), default=0.0)
    for row in rows:
        row["composite"] = composite_score(row, max_amount)
    rows.sort(key=lambda item: -item["composite"])
    ranked = [row for row in rows if row["count"] >= min_count]
    for index, row in enumerate(ranked, start=1):
        row["rank"] = index
        row["rank_str"] = f"{index}/{len(ranked)}"
    for row in rows:
        if row["count"] < min_count:
            row["rank"] = None
            row["rank_str"] = f"成分<{min_count}"
    return rows


def build_stock_rows(
    quotes: pd.DataFrame,
    members: pd.DataFrame,
    structures: Dict[str, StockStructure],
    baseline_minute: Dict[str, float],
    limits: Dict[str, Tuple[Optional[float], Optional[float]]],
    boards: Dict[str, int],
    thresholds: AlertThresholds = DEFAULT_THRESHOLDS,
) -> List[Dict[str, Any]]:
    """把快照拼成带行业归属与四档标签的个股行。"""
    member_map = {
        str(row.ts_code): (row.l1_name, row.l2_name, row.l3_name)
        for row in members.itertuples(index=False)
    }
    rows: List[Dict[str, Any]] = []
    for quote in quotes.itertuples(index=False):
        ts_code = str(getattr(quote, "ts_code", "")).strip().upper()
        industry = member_map.get(ts_code)
        if not industry:
            continue
        price = safe_float(getattr(quote, "close", None))
        pre_close = safe_float(getattr(quote, "pre_close", None))
        if not price or not pre_close:
            continue
        up_limit, down_limit = limits.get(ts_code, (None, None))
        structure = structures.get(ts_code)
        label = None
        if structure:
            label = classify(
                price=price, pre_close=pre_close,
                day_open=safe_float(getattr(quote, "open", None)) or 0.0,
                cum_amount=safe_float(getattr(quote, "amount", None)) or 0.0,
                volume_ratio=(
                    (safe_float(getattr(quote, "vol", None)) or 0.0) / baseline_minute[ts_code]
                    if baseline_minute.get(ts_code) else None
                ),
                structure=structure, thresholds=thresholds,
            )
        prior_boards = boards.get(ts_code, 0)
        limit_up = bool(up_limit and price >= up_limit - 1e-4)
        rows.append({
            "ts_code": ts_code,
            "code": ts_code.split(".")[0],
            "name": structure.name if structure else ts_code,
            "l1_name": industry[0], "l2_name": industry[1], "l3_name": industry[2],
            "pct": round((price / pre_close - 1) * 100, 2),
            "price": round(price, 3),
            "amount_yi": round((safe_float(getattr(quote, "amount", None)) or 0.0) / 1e8, 2),
            "label": label,
            "limit_up": limit_up,
            "limit_down": bool(down_limit and price <= down_limit + 1e-4),
            "boards": prior_boards + 1 if limit_up else prior_boards,
        })
    return rows


def _load_relation(now: datetime, thresholds: AlertThresholds) -> Dict[str, Any]:
    from .market_alerts import get_scanner_state, get_market_snapshot

    today = now.date()
    state = get_scanner_state(today)
    quotes = get_market_snapshot()
    if quotes is None or quotes.empty:
        raise IndustryRelationDataError("tushare 快照为空")

    connection = connect_analytics_db()
    try:
        members = load_members(connection)
        boards = load_board_counts(connection, today)
    finally:
        connection.close()

    limits = state.get("limits") or {}
    minute_label = now.strftime("%H:%M")
    baseline_minute = baseline_at(state.get("baseline"), minute_label)
    stocks = build_stock_rows(
        quotes, members, state.get("structures") or {}, baseline_minute, limits, boards, thresholds,
    )
    if not stocks:
        raise IndustryRelationDataError("快照与申万成分股没有交集")

    return {
        "date": today.isoformat(),
        "fetched_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "申万三级行业（SW2021）· 盘中快照复用提示看板",
        "picked": len(stocks),
        "min_rank_count": MIN_RANK_COUNT,
        "l1": aggregate_by_level(stocks, "l1"),
        "l2": aggregate_by_level(stocks, "l2"),
        "l3": aggregate_by_level(stocks, "l3"),
        "members": stocks,
    }


def fetch_industry_relation(
    now: Optional[datetime] = None,
    thresholds: AlertThresholds = DEFAULT_THRESHOLDS,
) -> Dict[str, Any]:
    now = now or datetime.now()
    hit = _cache.get("relation")
    if hit and time.time() - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]
    value = _load_relation(now, thresholds)
    _cache["relation"] = (time.time(), value)
    return value

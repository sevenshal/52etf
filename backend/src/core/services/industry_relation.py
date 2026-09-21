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
LIMIT_DOWN_STATUS = (5, 6)
MIN_RANK_COUNT = 5
RECENT_LABEL_DAYS = 3          # 成分股「前3标签」看前 3 个交易日的收盘口径状态
MICRO_CAP_COUNT = 400          # 微盘股范围取总市值最小的 N 只

# 市场范围：代码前缀类直接按 ts_code 判断，指数类走 a_stock_index_weight
UNIVERSE_OPTIONS: Tuple[Dict[str, Any], ...] = (
    {"key": "all", "name": "全A"},
    {"key": "sh_main", "name": "上证主板", "prefixes": ("60",), "suffix": ".SH"},
    {"key": "sz_main", "name": "深证主板", "prefixes": ("00",), "suffix": ".SZ"},
    {"key": "star", "name": "科创板", "prefixes": ("688",), "suffix": ".SH"},
    {"key": "gem", "name": "创业板", "prefixes": ("30",), "suffix": ".SZ"},
    {"key": "bj", "name": "北交所", "suffix": ".BJ"},
    {"key": "sz50", "name": "上证50", "index_code": "000016.SH"},
    {"key": "hs300", "name": "沪深300", "index_code": "000300.SH"},
    {"key": "zz500", "name": "中证500", "index_code": "000905.SH"},
    {"key": "zz1000", "name": "中证1000", "index_code": "000852.SH"},
    {"key": "zz2000", "name": "中证2000", "index_code": "932000.CSI"},
    {"key": "micro", "name": "微盘股", "micro": True},
)
UNIVERSE_BY_KEY = {item["key"]: item for item in UNIVERSE_OPTIONS}

# 焦点过滤：既可以按标签，也可以按涨停/连板状态
FOCUS_OPTIONS: Tuple[Dict[str, str], ...] = (
    {"key": "", "name": "全部"},
    {"key": "lu", "name": "涨停"},
    {"key": "fb", "name": "首板"},
    {"key": "eb", "name": "二板"},
    {"key": "lb", "name": "多板"},
    {"key": "st", "name": "强势"},
    {"key": "by", "name": "活跃"},
    {"key": "gw", "name": "观望"},
    {"key": "av", "name": "规避"},
    {"key": "ld", "name": "跌停"},
)        # 成分股少于此数的行业不参与排名，避免小样本噪声顶上榜首

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


def load_universe_codes(connection, universe: str, today: date) -> Optional[set]:
    """市场范围 → 股票代码集合；返回 None 表示全A（不过滤）。"""
    option = UNIVERSE_BY_KEY.get(universe or "all")
    if not option or option["key"] == "all":
        return None

    if option.get("index_code"):
        rows = connection.execute(
            """
            SELECT con_code FROM a_stock_index_weight
            WHERE index_code = ? AND trade_date = (
                SELECT max(trade_date) FROM a_stock_index_weight WHERE index_code = ?
            )
            """,
            [option["index_code"], option["index_code"]],
        ).fetchall()
        return {str(row[0]).strip().upper() for row in rows if row and row[0]}

    if option.get("micro"):
        rows = connection.execute(
            """
            SELECT ts_code FROM a_stock_market_daily
            WHERE trade_date = (SELECT max(trade_date) FROM a_stock_market_daily)
              AND total_mv IS NOT NULL
            ORDER BY total_mv ASC LIMIT ?
            """,
            [MICRO_CAP_COUNT],
        ).fetchall()
        return {str(row[0]).strip().upper() for row in rows if row and row[0]}

    # 代码前缀类范围（板块），直接用交易所后缀 + 代码前缀判断
    suffix = option.get("suffix")
    prefixes = option.get("prefixes")
    rows = connection.execute("SELECT DISTINCT ts_code FROM a_stock_sw_member").fetchall()
    codes = set()
    for row in rows:
        ts_code = str(row[0]).strip().upper()
        if suffix and not ts_code.endswith(suffix):
            continue
        if prefixes and not ts_code.split(".")[0].startswith(prefixes):
            continue
        codes.add(ts_code)
    return codes


def load_recent_labels(connection, today: date, days: int = RECENT_LABEL_DAYS) -> Dict[str, List[Dict[str, Any]]]:
    """前 N 个交易日的收盘口径标签与连板数：{ts_code: [{d, label, boards}, ...]}（按日期升序）。

    用日线还原，所以历史上线第一天就有数据，不必等盘中扫描攒。
    量比分母用该日前 20 日均量，与盘中同时段量比口径一致（只是粒度到日）。
    """
    frame = connection.execute(
        """
        SELECT ts_code, trade_date, open, close, pre_close, vol, amount, limit_status
        FROM a_stock_market_daily
        WHERE trade_date >= ?
        ORDER BY ts_code, trade_date
        """,
        [today - timedelta(days=90)],
    ).fetchdf()
    if frame.empty:
        return {}

    frame = frame.sort_values(["ts_code", "trade_date"])
    grouped = frame.groupby("ts_code", sort=False)
    # 前 20 日均量（不含当日）与九转计数所需的 4 日前收盘
    frame["avg_vol20"] = grouped["vol"].transform(lambda x: x.shift(1).rolling(20, min_periods=5).mean())
    frame["close_ref4"] = grouped["close"].transform(lambda x: x.shift(4))
    frame["close_ref3"] = grouped["close"].transform(lambda x: x.shift(3))
    frame["td_up"] = 0
    frame["td_down"] = 0

    results: Dict[str, List[Dict[str, Any]]] = {}
    for ts_code, group in grouped:
        closes = group["close"].tolist()
        up_runs, down_runs = [], []
        up = down = 0
        for index in range(len(closes)):
            ref = closes[index - 4] if index >= 4 else None
            if ref is None or pd.isna(ref):
                up = down = 0
            elif closes[index] > ref:
                up, down = up + 1, 0
            elif closes[index] < ref:
                down, up = down + 1, 0
            else:
                up = down = 0
            up_runs.append(up)
            down_runs.append(down)

        tail = group.tail(days)
        history: List[Dict[str, Any]] = []
        offset = len(group) - len(tail)
        for position, row in enumerate(tail.itertuples(index=False)):
            index = offset + position
            label = _daily_label(row, up_runs[index], down_runs[index])
            boards = 0
            if row.limit_status in LIMIT_UP_STATUS:
                boards = 1
                back = index - 1
                while back >= 0 and group["limit_status"].iloc[back] in LIMIT_UP_STATUS:
                    boards += 1
                    back -= 1
            history.append({
                "d": row.trade_date.strftime("%Y-%m-%d") if hasattr(row.trade_date, "strftime") else str(row.trade_date)[:10],
                "label": label,
                "boards": boards,
                "limit_down": bool(row.limit_status in LIMIT_DOWN_STATUS),
            })
        results[str(ts_code)] = history
    return results


def _daily_label(row: Any, td_up: int, td_down: int) -> Optional[str]:
    """按收盘口径给某一天打标签，阈值与盘中一致（量比分母换成 20 日均量）。"""
    close = safe_float(row.close)
    pre_close = safe_float(row.pre_close)
    day_open = safe_float(row.open)
    if not close or not pre_close or not day_open:
        return None
    pct = (close / pre_close - 1) * 100
    amount_yuan = (safe_float(row.amount) or 0.0) * 1000     # 日线 amount 单位千元
    avg_vol = safe_float(getattr(row, "avg_vol20", None))
    volume_ratio = (safe_float(row.vol) or 0.0) / avg_vol if avg_vol else None
    close_ref4 = safe_float(getattr(row, "close_ref4", None))
    close_ref3 = safe_float(getattr(row, "close_ref3", None))

    if (td_up >= DEFAULT_THRESHOLDS.active_td_up_min and pct > 0 and close > day_open
            and amount_yuan >= DEFAULT_THRESHOLDS.min_amount_yuan
            and volume_ratio is not None and volume_ratio >= DEFAULT_THRESHOLDS.min_volume_ratio):
        if (DEFAULT_THRESHOLDS.strong_td_up_min <= td_up <= DEFAULT_THRESHOLDS.strong_td_up_max
                and volume_ratio >= DEFAULT_THRESHOLDS.strong_volume_ratio):
            return LABEL_STRONG
        return LABEL_ACTIVE
    sharp_drop = bool(
        close_ref4 and close_ref3 and close < close_ref4 and close < close_ref3
        and (close_ref3 - close) / close * 100 > DEFAULT_THRESHOLDS.drop_pct_vs_3d
    )
    if td_down >= DEFAULT_THRESHOLDS.watch_td_down_min or sharp_drop:
        if td_down >= DEFAULT_THRESHOLDS.avoid_td_down_min and pct < 0:
            return LABEL_AVOID
        return LABEL_WATCH
    return None


def focus_matches(stock: Dict[str, Any], focus: str) -> bool:
    if not focus:
        return True
    if focus == "lu":
        return bool(stock.get("limit_up"))
    if focus == "ld":
        return bool(stock.get("limit_down"))
    if focus == "fb":
        return bool(stock.get("limit_up")) and (stock.get("boards") or 0) <= 1
    if focus == "eb":
        return bool(stock.get("limit_up")) and (stock.get("boards") or 0) == 2
    if focus == "lb":
        return bool(stock.get("limit_up")) and (stock.get("boards") or 0) >= 3
    label_map = {"st": LABEL_STRONG, "by": LABEL_ACTIVE, "gw": LABEL_WATCH, "av": LABEL_AVOID}
    return stock.get("label") == label_map.get(focus)


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


def _load_relation(
    now: datetime,
    thresholds: AlertThresholds,
    universe: str,
    focus: str,
    l1_label: str = "",
    l2_label: str = "",
) -> Dict[str, Any]:
    from .market_alerts import get_market_snapshot, get_scanner_state

    today = now.date()
    state = get_scanner_state(today)
    quotes = get_market_snapshot()
    if quotes is None or quotes.empty:
        raise IndustryRelationDataError("tushare 快照为空")

    connection = connect_analytics_db()
    try:
        members = load_members(connection)
        boards = load_board_counts(connection, today)
        universe_codes = load_universe_codes(connection, universe, today)
        recent_labels = _cached_recent_labels(connection, today)
    finally:
        connection.close()

    warnings: List[str] = []
    baseline = state.get("baseline")
    limits = state.get("limits") or {}
    if baseline is None or getattr(baseline, "empty", True):
        warnings.append("量能基准未就绪（分析库分钟线缺失或盘前任务未跑），强势/活跃暂不可用")
    if not limits:
        warnings.append("涨跌停价(stk_limit)未取到，涨停/跌停与连板统计暂不可用")

    minute_label = now.strftime("%H:%M")
    stocks = build_stock_rows(
        quotes, members, state.get("structures") or {},
        baseline_at(baseline, minute_label), limits, boards, thresholds,
    )
    if not stocks:
        raise IndustryRelationDataError("快照与申万成分股没有交集")

    hits, l1_labels, l2_labels = _load_today_hits(today)
    for stock in stocks:
        stock["labels_3d"] = recent_labels.get(stock["ts_code"], [])
        hit = hits.get(stock["ts_code"])
        stock["hit_time"] = hit["hit_time"] if hit else None
        stock["last_change_time"] = hit["last_change_time"] if hit else None
        if hit and hit.get("label"):
            # 命中记录优先：与提示看板保持同一口径
            stock["live_label"] = stock.get("label")
            stock["label"] = hit["label"]
        stock["l1_label"] = l1_labels.get(stock["l1_name"])
        stock["l2_label"] = l2_labels.get(stock["l2_name"])

    if universe_codes is not None:
        stocks = [stock for stock in stocks if stock["ts_code"] in universe_codes]
    if focus:
        stocks = [stock for stock in stocks if focus_matches(stock, focus)]
    # 与个股焦点组合：按所属申万一级/二级行业自身的当日标签过滤
    from .market_alerts import industry_label_matches

    if l1_label:
        stocks = [stock for stock in stocks if industry_label_matches(stock.get("l1_label"), l1_label)]
    if l2_label:
        stocks = [stock for stock in stocks if industry_label_matches(stock.get("l2_label"), l2_label)]
    if not stocks:
        return {
            "date": today.isoformat(),
            "fetched_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "universe": universe, "focus": focus,
            "universe_name": UNIVERSE_BY_KEY.get(universe or "all", {}).get("name", "全A"),
            "picked": 0, "min_rank_count": MIN_RANK_COUNT,
            "universe_options": list(UNIVERSE_OPTIONS), "focus_options": list(FOCUS_OPTIONS),
            "l1": [], "l2": [], "l3": [], "members": [],
        }

    return {
        "date": today.isoformat(),
        "fetched_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "申万三级行业（SW2021）· 盘中快照复用提示看板",
        "universe": universe or "all",
        "universe_name": UNIVERSE_BY_KEY.get(universe or "all", {}).get("name", "全A"),
        "focus": focus or "",
        "picked": len(stocks),
        "min_rank_count": MIN_RANK_COUNT,
        "warnings": warnings,
        "universe_options": [
            {"key": item["key"], "name": item["name"]} for item in UNIVERSE_OPTIONS
        ],
        "focus_options": list(FOCUS_OPTIONS),
        "l1": _with_industry_labels(aggregate_by_level(stocks, "l1"), l1_labels),
        "l2": _with_industry_labels(aggregate_by_level(stocks, "l2"), l2_labels),
        "l3": aggregate_by_level(stocks, "l3"),       # 三级 tushare 没有行情，不打行业自身标签
        "members": stocks,
    }


def _with_industry_labels(rows: List[Dict[str, Any]], labels: Dict[str, str]) -> List[Dict[str, Any]]:
    for row in rows:
        row["label"] = labels.get(row["name"])
    return rows


def _load_today_hits(today: date) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str], Dict[str, str]]:
    """今日提示看板的记录：(个股 {代码: {标签, 首次时刻}}, 一级 {行业名: 标签}, 二级 {行业名: 标签})。

    行业关联的「今日标签」以命中记录为准（最新标签，盘中变化会覆盖），与提示看板同一口径；
    申万一/二级自己的信号也来自同一张表，用来给行业行打标签、按行业标签过滤成分股。
    """
    try:
        from ..database import MarketAlertHit, get_db_ctx
        from .market_alerts import ENTITY_STOCK, sw_label_maps

        with get_db_ctx() as db:
            rows = db.query(MarketAlertHit).filter(MarketAlertHit.trade_date == today).all()
            l1_map, l2_map = sw_label_maps(rows)
            stocks = {
                str(row.ts_code): {
                    "hit_time": str(row.hit_time),
                    "last_change_time": str(row.last_change_time or row.hit_time),
                    "label": str(row.label),
                }
                for row in rows
                if (row.entity_type or ENTITY_STOCK) == ENTITY_STOCK
            }
        return stocks, l1_map, l2_map
    except Exception as exc:  # noqa: BLE001  命中记录只是附加信息
        logger.warning("读取当日命中记录失败: %s", exc)
        return {}, {}, {}


def _cached_recent_labels(connection, today: date) -> Dict[str, List[Dict[str, Any]]]:
    """前3标签按日变化，缓存到当天结束。"""
    key = f"recent_labels:{today.isoformat()}"
    hit = _cache.get(key)
    if hit:
        return hit[1]
    value = load_recent_labels(connection, today)
    _cache[key] = (time.time(), value)
    return value


def fetch_industry_relation(
    universe: str = "all",
    focus: str = "",
    l1_label: str = "",
    l2_label: str = "",
    now: Optional[datetime] = None,
    thresholds: AlertThresholds = DEFAULT_THRESHOLDS,
) -> Dict[str, Any]:
    now = now or datetime.now()
    key = f"relation:{universe}:{focus}:{l1_label}:{l2_label}"
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]
    value = _load_relation(now, thresholds, universe or "all", focus or "", l1_label or "", l2_label or "")
    _cache[key] = (time.time(), value)
    return value


def fetch_industry_history(level: str, name: str, days: int = 60) -> Dict[str, Any]:
    """某个行业的指数日线走势（申万行业指数），用于关联结构面板。"""
    level_key = str(level or "l1").upper()
    if level_key not in {"L1", "L2", "L3"}:
        raise ValueError("行业层级必须是 l1/l2/l3")
    connection = connect_analytics_db()
    try:
        row = connection.execute(
            "SELECT index_code FROM a_stock_sw_industry WHERE level = ? AND industry_name = ? LIMIT 1",
            [level_key, name],
        ).fetchone()
        if not row:
            raise IndustryRelationDataError(f"找不到行业 {name}（{level_key}）")
        index_code = str(row[0])
        frame = connection.execute(
            """
            SELECT trade_date, close, vol, amount, pe, pb
            FROM a_stock_sw_daily WHERE ts_code = ?
            ORDER BY trade_date DESC LIMIT ?
            """,
            [index_code, max(5, min(int(days or 60), 250))],
        ).fetchdf()
    finally:
        connection.close()
    if frame.empty:
        raise IndustryRelationDataError(f"{name} 没有行业指数日线，请先跑「A股基础数据同步」")
    frame = frame.sort_values("trade_date")
    closes = frame["close"].tolist()
    return {
        "level": level_key,
        "name": name,
        "index_code": index_code,
        "dates": [
            d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10]
            for d in frame["trade_date"]
        ],
        "close": [safe_float(v, 2) for v in closes],
        "amount_yi": [safe_float((v or 0) / 1e4, 2) for v in frame["amount"]],   # sw_daily amount 单位万元
        "pe": [safe_float(v, 2) for v in frame["pe"]],
        "pb": [safe_float(v, 2) for v in frame["pb"]],
        "pct_range": round((closes[-1] / closes[0] - 1) * 100, 2) if closes and closes[0] else None,
    }

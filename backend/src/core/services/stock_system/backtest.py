"""选股系统回测：按历史交易日回放三层系统，并做消融对比。

每个交易日做的事情和实盘一模一样，而且调用的是同一批函数：

- 第一层：调仓日（默认每月第一个交易日）用 ``compute_fundamental_pool(as_of=调仓日)`` 算股票池，
  全部 point-in-time（财报按披露日、行情/利率/beta 截止到当天）。结果按第一层参数缓存，换第二、三层
  参数重跑时不用再算；
- 第二层：每天用贪恐历史曲线上截至当天的顶/底标记（``regime_series``/``regime_at``，与实盘
  ``classify_regime`` 同一判定）和 ``build_allocation`` 算目标仓位；
- 第三层：``compute_signals`` + ``plan_orders`` 出信号和订单，``paper.settle`` 下一交易日开盘撮合、
  收盘盯市，费用、涨跌停、停牌、分红送转的处理都与模拟盘一致。

消融方案（同一套撮合，只差规则）：

- ``pool_hold``：股票池排名前 N（N = 最大持仓数）等权持有，只在调仓日再平衡——基准；
- ``sentiment``：加上第二层，每天按目标仓位调仓（偏离 ≥ 2 个百分点才动），防守/过热板块里已有的
  持仓不卖不加；
- ``technical``：加上第三层的技术面进出，关闭雪球过滤；
- ``full``：完整系统（雪球过滤按配置；雪球数据 2026-06-25 起才有，之前自动跳过）。

另外输出第一层的验收检验：每个调仓日按综合分分五组，看之后 20/60 个交易日的收益是否单调、
头尾价差和秩相关（IC）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from bisect import bisect_right
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from . import paper
from .allocation import UNMAPPED_SECTOR, _constituent_counts, assign_sectors, build_allocation
from .config import normalize_stock_system_config
from .indicators import (
    CHART_VOLUME_LOOKBACK_DAYS,
    CHART_VOLUME_STD_MULTIPLIER,
    append_nine_turn_atr,
    calculate_macd,
    chart_support_resistance_at,
    preprocess_klines_volume,
)
from .sentiment import regime_at, regime_series, target_catalog
from .trading import KLINE_LOOKBACK_DAYS, compute_signals, plan_orders

logger = logging.getLogger(__name__)

# 第一层逻辑改了就改这个版本号，旧的股票池缓存自动失效
POOL_CACHE_VERSION = "p1-2026-09-12"
BENCHMARK_INDEX = "000985.SH"
BENCHMARK_LABEL = "中证全指"
TRADING_DAYS_PER_YEAR = 244
REBALANCE_THRESHOLD_PCT = 2.0
FORWARD_HORIZONS = (20, 60)
POOL_ROW_FIELDS = ("ts_code", "name", "industry", "gate_passed", "pool_rank", "composite_score", "in_pool")
# 雪球持仓快照从这天起才有效（与 xueqiu_holdings.XUEQIU_HOLDINGS_VALID_FROM 一致），之前不必去查
XUEQIU_DATA_FROM = date(2026, 6, 25)

VARIANTS: List[Dict[str, str]] = [
    {"key": "pool_hold", "label": "股票池等权持有",
     "description": "第一层排名前 N 只等权持有，只在调仓日再平衡。作为基准：后面每一层都要证明比它好。"},
    {"key": "sentiment", "label": "+情绪择时与仓位",
     "description": "按第二层每天的目标仓位调仓（偏离 ≥ 2 个百分点才动）；防守/过热板块里已有的持仓不卖不加。"},
    {"key": "technical", "label": "+技术面进出（关雪球）",
     "description": "第三层入场触发、止损和出场规则，关闭雪球过滤。"},
    {"key": "full", "label": "完整系统",
     "description": "三层全开，雪球过滤按当前配置（雪球数据 2026-06-25 起才有，之前自动跳过这条过滤）。"},
]
VARIANT_KEYS = [variant["key"] for variant in VARIANTS]
VARIANT_LABELS = {variant["key"]: variant["label"] for variant in VARIANTS}


class BacktestCancelled(Exception):
    """用户取消了回测。"""


def pool_cache_key(config: Mapping[str, Any]) -> str:
    payload = {key: config[key] for key in ("universe", "gates", "factors", "scoring", "pool")}
    digest = hashlib.sha1((json.dumps(payload, sort_keys=True) + POOL_CACHE_VERSION).encode("utf-8"))
    return digest.hexdigest()[:24]


def rebalance_dates(days: Sequence[date], frequency: str) -> List[date]:
    """每月（或每周）第一个交易日。"""
    result: List[date] = []
    last_key = None
    for day in days:
        key = (day.year, day.month) if frequency == "monthly" else tuple(day.isocalendar()[:2])
        if key != last_key:
            result.append(day)
            last_key = key
    return result


# ---------------------------------------------------------------------------
# 依赖（回测进程里全部指向数据工作区；测试可以逐个替换）
# ---------------------------------------------------------------------------

@dataclass
class BacktestDeps:
    connect: Callable[[], Any]
    compute_pool: Callable[[date, Mapping[str, Any]], Dict[str, Any]]
    pool_cache_get: Callable[[str, date], Optional[Dict[str, Any]]]
    pool_cache_put: Callable[[str, date, Dict[str, Any]], None]
    history_loader: Callable[[str, date], List[Dict[str, Any]]]
    membership_loader: Callable[[Sequence[str], date], Dict[str, List[Dict[str, str]]]]
    kline_loader: Callable[[Sequence[str], date, date], Dict[str, List[Dict[str, Any]]]]
    xueqiu_loader: Callable[[Sequence[str], date, int], Dict[str, Any]]


def default_deps() -> BacktestDeps:
    """实盘同一批函数；回测进程里 ANALYTICS_DB_PATH 已指向数据工作区。"""
    from ...analytics_database import get_analytics_db_ctx
    from ...database import SessionLocal, StockSystemBacktestPoolCache
    from ..a_stock_consensus import load_a_stock_klines_batch
    from ..duckdb_analytics import connect_analytics_db
    from .allocation import _default_membership_loader
    from .fundamental_pool import compute_fundamental_pool
    from .sentiment import CalculatorHistoryLoader
    from .trading import _default_xueqiu_loader

    def pool_cache_get(key: str, day: date) -> Optional[Dict[str, Any]]:
        with SessionLocal() as db:
            row = db.get(StockSystemBacktestPoolCache, (key, day))
            return {"rows": row.rows, "summary": row.summary} if row else None

    def pool_cache_put(key: str, day: date, payload: Dict[str, Any]) -> None:
        with SessionLocal() as db:
            row = db.get(StockSystemBacktestPoolCache, (key, day)) or StockSystemBacktestPoolCache(pool_key=key, trade_date=day)
            row.rows = payload["rows"]
            row.summary = payload.get("summary")
            row.created_at = datetime.now()
            db.merge(row)
            db.commit()

    def kline_loader(symbols: Sequence[str], start: date, end: date) -> Dict[str, List[Dict[str, Any]]]:
        with get_analytics_db_ctx() as db:
            return load_a_stock_klines_batch(db, symbols, start_date=start, end_date=end)

    return BacktestDeps(
        connect=connect_analytics_db,
        compute_pool=lambda day, config: compute_fundamental_pool(as_of=day, config=config),
        pool_cache_get=pool_cache_get,
        pool_cache_put=pool_cache_put,
        history_loader=CalculatorHistoryLoader(),
        membership_loader=_default_membership_loader,
        kline_loader=kline_loader,
        xueqiu_loader=_default_xueqiu_loader,
    )


# ---------------------------------------------------------------------------
# 指标缓存：整段历史只算一次，支撑压力按需逐根算
# ---------------------------------------------------------------------------

class IndicatorCache:
    """回测里的技术指标。

    九转/ATR、放量 z 值、MACD 对每只股票整段历史算一次，按天切片给 ``compute_signals``；支撑压力只在
    需要的那两根（当天和前一天）上现算。实盘按 400 个自然日的窗口现算：EMA 从窗口第一根播种，预热
    270 根以上后与整段计算的差异 < 1e-8；九转计数、ATR、放量 z 值、支撑压力只看最近几十到 125 根，
    两种算法结果一致。
    """

    def __init__(self, loader: Callable[[Sequence[str], date, date], Dict[str, List[Dict[str, Any]]]],
                 start: date, end: date, max_symbols: int = 250):
        self._loader = loader
        self._start = start - timedelta(days=KLINE_LOOKBACK_DAYS)
        self._end = end
        self._max_symbols = max_symbols
        self._cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()

    def _entry(self, ts_code: str) -> Dict[str, Any]:
        entry = self._cache.get(ts_code)
        if entry is not None:
            self._cache.move_to_end(ts_code)
            return entry
        klines = self._loader([ts_code], self._start, self._end).get(ts_code) or []
        entry = {
            "klines": klines,
            "rows": append_nine_turn_atr(
                preprocess_klines_volume(klines, CHART_VOLUME_STD_MULTIPLIER, CHART_VOLUME_LOOKBACK_DAYS)
            ),
            "macd": calculate_macd(klines),
            "dates": [kline["timestamp"].date() for kline in klines],
        }
        self._cache[ts_code] = entry
        while len(self._cache) > self._max_symbols:
            self._cache.popitem(last=False)
        return entry

    def provider(self, day: date) -> Callable[[str], Dict[str, Any]]:
        def provide(ts_code: str) -> Dict[str, Any]:
            entry = self._entry(ts_code)
            index = bisect_right(entry["dates"], day) - 1
            if index < 0:
                return {"klines": [], "macd": {"dif": [], "dea": []}}
            for position in (index - 1, index):
                if position >= 0 and "support_resistance" not in entry["rows"][position]:
                    entry["rows"][position]["support_resistance"] = chart_support_resistance_at(entry["klines"], position)
            return {
                "klines": entry["rows"][:index + 1],
                "macd": {"dif": entry["macd"]["dif"][:index + 1], "dea": entry["macd"]["dea"][:index + 1]},
            }
        return provide


# ---------------------------------------------------------------------------
# 目标权重再平衡（基准与情绪方案用）
# ---------------------------------------------------------------------------

def plan_target_orders(
    day: date,
    book: Mapping[str, Any],
    targets: Mapping[str, Mapping[str, Any]],
    keep: Sequence[str],
    threshold_pct: float,
    max_single_weight_pct: float,
) -> List[Dict[str, Any]]:
    """把持仓调到目标权重：不在目标里的卖掉（keep 里的除外），超配减仓，低配/新目标买入。"""
    nav = paper.book_nav(book)
    if nav <= 0:
        return []
    orders: List[Dict[str, Any]] = []
    keep_set = set(keep)
    for ts_code, position in book["positions"].items():
        if ts_code in keep_set:
            continue
        weight_now = float(position.get("market_value") or 0.0) / nav * 100.0
        target = targets.get(ts_code)
        if target is None:
            orders.append({"signal_date": day, "ts_code": ts_code, "name": position.get("name"),
                           "side": paper.SIDE_SELL, "status": paper.ORDER_PENDING, "reason": "不在目标持仓里"})
        elif weight_now - float(target["weight"]) > threshold_pct and weight_now > 0:
            orders.append({"signal_date": day, "ts_code": ts_code, "name": position.get("name"),
                           "side": paper.SIDE_SELL, "status": paper.ORDER_PENDING,
                           "fraction": (weight_now - float(target["weight"])) / weight_now, "reason": "超配减仓"})
    for ts_code, target in targets.items():
        position = book["positions"].get(ts_code)
        weight_now = float(position.get("market_value") or 0.0) / nav * 100.0 if position else 0.0
        gap = float(target["weight"]) - weight_now
        if gap > threshold_pct:
            orders.append({
                "signal_date": day, "ts_code": ts_code, "name": target.get("name"),
                "side": paper.SIDE_BUY, "status": paper.ORDER_PENDING, "budget": nav * gap / 100.0,
                # 差额买不满一手时允许买一手，上限是单只仓位上限（高价股一手可能就超过目标权重）
                "max_budget": nav * max_single_weight_pct / 100.0,
                "target_weight_pct": float(target["weight"]), "allow_add": True,
                "sector_code": target.get("sector_code"), "sector_name": target.get("sector_name"),
                "reason": "低配加仓" if position else "新进目标持仓",
            })
    return orders


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------

def nav_metrics(navs: Sequence[Mapping[str, Any]], initial: float) -> Dict[str, Any]:
    if not navs:
        return {}
    values = np.array([initial, *[float(point["nav"]) for point in navs]], dtype=float)
    returns = values[1:] / values[:-1] - 1.0
    total = values[-1] / initial - 1.0
    years = max(len(navs) / TRADING_DAYS_PER_YEAR, 1e-9)
    cagr = (1.0 + total) ** (1.0 / years) - 1.0 if total > -1 else -1.0
    drawdown = float((values / np.maximum.accumulate(values) - 1.0).min())
    volatility = float(returns.std(ddof=0) * math.sqrt(TRADING_DAYS_PER_YEAR))
    sharpe = float(returns.mean() / returns.std(ddof=0) * math.sqrt(TRADING_DAYS_PER_YEAR)) if returns.std(ddof=0) > 0 else None
    exposures = [float(point["exposure_pct"]) for point in navs if point.get("exposure_pct") is not None]
    return {
        "total_return_pct": round(total * 100.0, 2),
        "cagr_pct": round(cagr * 100.0, 2),
        "max_drawdown_pct": round(drawdown * 100.0, 2),
        "volatility_pct": round(volatility * 100.0, 2),
        "sharpe": round(sharpe, 2) if sharpe is not None else None,
        "calmar": round(cagr / abs(drawdown), 2) if drawdown < 0 else None,
        "avg_exposure_pct": round(sum(exposures) / len(exposures), 1) if exposures else None,
        "final_nav": round(float(values[-1]), 2),
        "days": len(navs),
    }


def trade_metrics(trades: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    if not trades:
        return {"trades": 0}
    returns = [float(trade["return_pct"]) for trade in trades if trade.get("return_pct") is not None]
    wins = [value for value in returns if value > 0]
    losses = [value for value in returns if value <= 0]
    pnl_win = sum(float(trade["pnl"]) for trade in trades if (trade.get("pnl") or 0) > 0)
    pnl_loss = -sum(float(trade["pnl"]) for trade in trades if (trade.get("pnl") or 0) < 0)
    holding = [int(trade["holding_days"]) for trade in trades if trade.get("holding_days") is not None]
    return {
        "trades": len(trades),
        "win_rate_pct": round(len(wins) / len(returns) * 100.0, 1) if returns else None,
        "avg_return_pct": round(sum(returns) / len(returns), 2) if returns else None,
        "avg_win_pct": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss_pct": round(sum(losses) / len(losses), 2) if losses else None,
        "profit_factor": round(pnl_win / pnl_loss, 2) if pnl_loss > 0 else None,
        "avg_holding_days": round(sum(holding) / len(holding), 1) if holding else None,
    }


def yearly_returns(navs: Sequence[Mapping[str, Any]], initial: float) -> Dict[str, float]:
    result: Dict[str, float] = {}
    previous = initial
    by_year: Dict[str, float] = {}
    for point in navs:
        by_year[str(point["trade_date"])[:4]] = float(point["nav"])
    for year in sorted(by_year):
        result[year] = round((by_year[year] / previous - 1.0) * 100.0, 2)
        previous = by_year[year]
    return result


def pool_score_analysis(
    pools: Mapping[date, Sequence[Mapping[str, Any]]],
    days: Sequence[date],
    closes: Mapping[date, Mapping[str, float]],
    horizons: Sequence[int] = FORWARD_HORIZONS,
) -> Dict[str, Any]:
    """第一层验收：每个调仓日按综合分五等分，看之后 N 个交易日（前复权收盘到收盘）的平均收益。"""
    position = {day: index for index, day in enumerate(days)}
    result: Dict[str, Any] = {}
    for horizon in horizons:
        per_date = []
        for day, rows in sorted(pools.items()):
            index = position.get(day)
            if index is None or index + horizon >= len(days):
                continue
            end_day = days[index + horizon]
            records = []
            for row in rows:
                score = row.get("composite_score")
                start_close = closes.get(day, {}).get(row["ts_code"])
                end_close = closes.get(end_day, {}).get(row["ts_code"])
                if row.get("gate_passed") and score is not None and start_close and end_close:
                    records.append({"score": float(score), "ret": (end_close / start_close - 1.0) * 100.0,
                                    "in_pool": bool(row.get("in_pool"))})
            if len(records) < 25:
                continue
            frame = pd.DataFrame(records)
            frame["quintile"] = pd.qcut(frame["score"].rank(method="first"), 5, labels=[1, 2, 3, 4, 5])
            means = frame.groupby("quintile", observed=True)["ret"].mean()
            per_date.append({
                "date": day.isoformat(),
                "quintiles": [round(float(means.get(q, float("nan"))), 2) for q in (1, 2, 3, 4, 5)],
                "spread": round(float(means.get(5) - means.get(1)), 2),
                "ic": round(float(frame["score"].corr(frame["ret"], method="spearman")), 4),
                "pool_excess": round(float(frame.loc[frame["in_pool"], "ret"].mean() - frame["ret"].mean()), 2)
                if frame["in_pool"].any() else None,
                "count": len(frame),
            })
        if not per_date:
            result[str(horizon)] = {"dates": 0}
            continue
        ics = np.array([item["ic"] for item in per_date], dtype=float)
        spreads = np.array([item["spread"] for item in per_date], dtype=float)
        pool_excess = [item["pool_excess"] for item in per_date if item["pool_excess"] is not None]
        result[str(horizon)] = {
            "dates": len(per_date),
            "quintile_mean_pct": [
                round(float(np.nanmean([item["quintiles"][q] for item in per_date])), 2) for q in range(5)
            ],
            "spread_mean_pct": round(float(spreads.mean()), 2),
            "spread_positive_pct": round(float((spreads > 0).mean() * 100.0), 1),
            "ic_mean": round(float(np.nanmean(ics)), 4),
            "ic_t": round(float(np.nanmean(ics) / np.nanstd(ics) * math.sqrt(len(ics))), 2) if np.nanstd(ics) > 0 else None,
            "pool_excess_mean_pct": round(float(np.mean(pool_excess)), 2) if pool_excess else None,
            "per_date": per_date,
        }
    return result


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _trading_days(connection, start: date, end: date) -> List[date]:
    rows = connection.execute(
        "SELECT DISTINCT trade_date FROM a_stock_market_daily WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
        [start, end],
    ).fetchall()
    return [row[0] if isinstance(row[0], date) else row[0].date() for row in rows]


def _previous_trading_day(connection, day: date) -> Optional[date]:
    row = connection.execute("SELECT MAX(trade_date) FROM a_stock_market_daily WHERE trade_date < ?", [day]).fetchone()
    value = row[0] if row else None
    return value if isinstance(value, date) or value is None else value.date()


def _load_closes(connection, symbols: Sequence[str], days: Sequence[date]) -> Dict[date, Dict[str, float]]:
    if not symbols or not days:
        return {}
    result: Dict[date, Dict[str, float]] = {}
    symbols = list(symbols)
    for offset in range(0, len(symbols), 1000):
        chunk = symbols[offset:offset + 1000]
        frame = connection.execute(
            f"""
            SELECT ts_code, trade_date, close FROM a_stock_market_daily_qfq
            WHERE ts_code IN ({', '.join('?' for _ in chunk)})
              AND trade_date IN ({', '.join('?' for _ in days)})
            """,
            [*chunk, *days],
        ).fetchall()
        for ts_code, trade_date, close in frame:
            day = trade_date if isinstance(trade_date, date) else trade_date.date()
            if close:
                result.setdefault(day, {})[str(ts_code)] = float(close)
    return result


def _benchmark_navs(connection, days: Sequence[date], initial: float) -> List[Dict[str, Any]]:
    rows = connection.execute(
        "SELECT trade_date, close FROM a_stock_index_daily WHERE ts_code = ? AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
        [BENCHMARK_INDEX, days[0] - timedelta(days=15), days[-1]],
    ).fetchall()
    closes = {(row[0] if isinstance(row[0], date) else row[0].date()): float(row[1]) for row in rows if row[1]}
    ordered = sorted(closes)
    base_day = [day for day in ordered if day < days[0]]
    base = closes[base_day[-1]] if base_day else closes.get(days[0])
    if not base:
        return []
    navs, last = [], base
    for day in days:
        last = closes.get(day, last)
        navs.append({"trade_date": day, "nav": initial * last / base, "exposure_pct": 100.0, "positions": None})
    return navs


def _pool_payload(result: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "rows": [{field: row.get(field) for field in POOL_ROW_FIELDS} for row in result.get("rows") or []],
        "summary": result.get("summary") or {},
    }


def _variant_config(config: Mapping[str, Any], variant: str) -> Dict[str, Any]:
    config = json.loads(json.dumps(config))
    if variant == "technical":
        config["signals"]["xueqiu_ratio"]["enabled"] = False
    return normalize_stock_system_config(config)


def _trades_from_events(events: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    trades = []
    for event in events:
        if event.get("side") != paper.SIDE_SELL or event.get("status") != paper.ORDER_FILLED:
            continue
        entry_date = event.get("entry_date")
        exit_date = date.fromisoformat(event["date"])
        cost = float(event.get("cost") or 0.0)
        trades.append({
            "ts_code": event["ts_code"],
            "name": event.get("name"),
            "entry_date": entry_date,
            "exit_date": exit_date,
            "entry_price": event.get("entry_price"),
            "exit_price": event.get("fill_price"),
            "quantity": event.get("quantity"),
            "pnl": event.get("realized_pnl"),
            "return_pct": (float(event["realized_pnl"]) / cost * 100.0) if cost > 0 and event.get("realized_pnl") is not None else None,
            "holding_days": (exit_date - entry_date).days if isinstance(entry_date, date) else None,
            "reason": event.get("reason"),
        })
    return trades


def run_backtest(
    *,
    start: date,
    end: date,
    config: Mapping[str, Any],
    variants: Sequence[str] = tuple(VARIANT_KEYS),
    frequency: str = "monthly",
    initial_capital: Optional[float] = None,
    deps: Optional[BacktestDeps] = None,
    progress: Callable[[float, str], None] = lambda pct, message: None,
    cancelled: Callable[[], bool] = lambda: False,
) -> Dict[str, Any]:
    started = time.monotonic()
    deps = deps or default_deps()
    config = normalize_stock_system_config(config)
    variants = [variant for variant in VARIANT_KEYS if variant in set(variants)] or list(VARIANT_KEYS)
    initial = float(initial_capital or config["paper"]["initial_capital"])
    paper_config = config["paper"]
    timing = config["timing"]
    position_config = config["position"]
    notes: List[str] = []

    connection = deps.connect()
    try:
        days = _trading_days(connection, start, end)
        before_start = _previous_trading_day(connection, days[0]) if days else None
    finally:
        connection.close()
    if not days or before_start is None:
        raise ValueError(f"{start}~{end} 之间没有可回测的交易日（或缺少起始日之前的行情）")
    rebalance_days = rebalance_dates(days, frequency)

    # --- 第一层：调仓日的历史股票池 ---
    key = pool_cache_key(config)
    pools: Dict[date, Dict[str, Any]] = {}
    for index, day in enumerate(rebalance_days):
        if cancelled():
            raise BacktestCancelled()
        cached = deps.pool_cache_get(key, day)
        if cached is None:
            progress(5 + 55 * index / len(rebalance_days), f"计算 {day} 的历史股票池（{index + 1}/{len(rebalance_days)}）")
            result = deps.compute_pool(day, config)
            if result.get("status") != "completed":
                raise RuntimeError(f"{day} 股票池计算失败：{result.get('message') or result.get('status')}")
            cached = _pool_payload(result)
            deps.pool_cache_put(key, day, cached)
        pools[day] = cached
    progress(60, "股票池就绪，开始逐日回放")

    # --- 第二层准备：各指数贪恐历史、每个调仓日的板块归属 ---
    catalog = target_catalog()
    series = {}
    for item in catalog:
        try:
            series[item["symbol"]] = regime_series(deps.history_loader(item["symbol"], end))
        except Exception as exc:  # noqa: BLE001 单个指数读失败按"没有数据"处理
            logger.warning("backtest fear-greed history unavailable for %s: %s", item["symbol"], exc)
            series[item["symbol"]] = regime_series([])
    sectors_by_day: Dict[date, Dict[str, Optional[str]]] = {}
    counts_by_day: Dict[date, Dict[str, int]] = {}
    for day, pool in pools.items():
        symbols = [row["ts_code"] for row in pool["rows"] if row.get("in_pool")]
        memberships = deps.membership_loader(symbols, day) if symbols else {}
        connection = deps.connect()
        try:
            counts_by_day[day] = _constituent_counts(connection, [item["symbol"] for item in catalog], day)
        finally:
            connection.close()
        sectors_by_day[day] = assign_sectors(memberships, counts_by_day[day], catalog)

    indicators = IndicatorCache(deps.kline_loader, start, end)

    def xueqiu_loader(symbols: Sequence[str], day: date, lookback: int) -> Dict[str, Any]:
        if day < XUEQIU_DATA_FROM:
            return {"available": False, "reason": f"雪球持仓数据 {XUEQIU_DATA_FROM} 起才有", "items": {}}
        return deps.xueqiu_loader(symbols, day, lookback)

    variant_configs = {variant: _variant_config(config, variant) for variant in variants}
    books = {
        variant: {
            "account": {"id": None, "initial_capital": initial, "cash": initial,
                        "started_on": days[0], "last_trade_date": before_start},
            "positions": {},
            "pending": [],
        }
        for variant in variants
    }
    navs: Dict[str, List[Dict[str, Any]]] = {variant: [] for variant in variants}
    trades: Dict[str, List[Dict[str, Any]]] = {variant: [] for variant in variants}
    rebalance_set = set(rebalance_days)

    # --- 逐日回放 ---
    for index, day in enumerate(days):
        if index % 20 == 0:
            if cancelled():
                raise BacktestCancelled()
            progress(60 + 35 * index / len(days), f"回放 {day}（{index + 1}/{len(days)}）")
        pool_day = rebalance_days[bisect_right(rebalance_days, day) - 1]
        pool = pools[pool_day]
        in_pool = [row for row in pool["rows"] if row.get("in_pool")]
        regimes = {symbol: regime_at(item, day, timing) for symbol, item in series.items()}
        built = build_allocation(day, in_pool, sectors_by_day[pool_day], regimes, counts_by_day[pool_day], config, catalog)
        allocation = {"run": {"trade_date": day.isoformat(), "summary": built["summary"]},
                      "regimes": built["regimes"], "rows": built["rows"]}

        for variant in variants:
            book = books[variant]
            settled = paper.settle(book, day, paper_config, connect=deps.connect)
            for point in settled["navs"]:
                navs[variant].append({
                    "trade_date": point["trade_date"], "nav": point["nav"], "positions": point["positions"],
                    "exposure_pct": point["market_value"] / point["nav"] * 100.0 if point["nav"] else None,
                })
            trades[variant].extend(_trades_from_events(settled["events"]))
            book["pending"] = [order for order in book["pending"] if order["status"] == paper.ORDER_PENDING]

            if variant == "pool_hold":
                if day in rebalance_set:
                    top = sorted((row for row in in_pool if row.get("pool_rank")), key=lambda row: row["pool_rank"])
                    top = top[: int(position_config["max_positions"])]
                    weight = 100.0 / len(top) if top else 0.0
                    targets = {row["ts_code"]: {"weight": weight, "name": row.get("name")} for row in top}
                    book["pending"].extend(plan_target_orders(
                        day, book, targets, (), 0.5, position_config["max_single_weight_pct"]))
            elif variant == "sentiment":
                in_pool_set = {row["ts_code"] for row in in_pool}
                targets = {
                    row["ts_code"]: {"weight": row["target_weight_pct"], "name": row.get("name"),
                                     "sector_code": row.get("sector_code"), "sector_name": row.get("sector_name")}
                    for row in built["rows"] if row["status"] == "target"
                }
                keep = [code for code in book["positions"] if code in in_pool_set and code not in targets]
                book["pending"].extend(plan_target_orders(
                    day, book, targets, keep, REBALANCE_THRESHOLD_PCT, position_config["max_single_weight_pct"]))
            else:
                variant_config = variant_configs[variant]
                signals = compute_signals(
                    day, variant_config, allocation, pool["rows"], book["positions"],
                    xueqiu_loader=xueqiu_loader, indicator_provider=indicators.provider(day),
                )
                book["pending"].extend(plan_orders(day, signals["rows"], book, allocation, variant_config))

    progress(96, "统计结果")
    connection = deps.connect()
    try:
        benchmark = _benchmark_navs(connection, days, initial)
        passed_symbols = sorted({row["ts_code"] for pool in pools.values() for row in pool["rows"] if row.get("gate_passed")})
        needed_days = set(pools)
        for pool_day in pools:
            position = days.index(pool_day)
            for horizon in FORWARD_HORIZONS:
                if position + horizon < len(days):
                    needed_days.add(days[position + horizon])
        closes = _load_closes(connection, passed_symbols, sorted(needed_days))
    finally:
        connection.close()
    score_analysis = pool_score_analysis({day: pool["rows"] for day, pool in pools.items()}, days, closes)

    if days[0] < XUEQIU_DATA_FROM <= days[-1] and "full" in variants:
        notes.append(f"雪球过滤只在 {XUEQIU_DATA_FROM} 之后生效，之前「完整系统」与「+技术面进出」相同")
    elif days[-1] < XUEQIU_DATA_FROM and "full" in variants:
        notes.append("回测区间里没有雪球持仓数据，「完整系统」与「+技术面进出」相同")
    if config["factors"]["earnings_yield_pct"]["enabled"]:
        notes.append("盈利收益率因子依赖 pe_ttm 历史；「A股估值列历史回填」没跑之前它在历史股票池里缺失，按覆盖率自动降权")

    summary = {
        "variants": {
            variant: {
                "label": VARIANT_LABELS[variant],
                **nav_metrics(navs[variant], initial),
                **trade_metrics(trades[variant]),
                "open_positions": len(books[variant]["positions"]),
            }
            for variant in variants
        },
        "benchmark": {"label": BENCHMARK_LABEL, **nav_metrics(benchmark, initial)},
        "yearly": {
            **{variant: yearly_returns(navs[variant], initial) for variant in variants},
            "benchmark": yearly_returns(benchmark, initial),
        },
        "pool_score": score_analysis,
        "rebalance_days": [day.isoformat() for day in rebalance_days],
        "trading_days": len(days),
        "notes": notes,
        "duration_seconds": round(time.monotonic() - started, 1),
    }
    return {"summary": summary, "navs": {**navs, "benchmark": benchmark}, "trades": trades}

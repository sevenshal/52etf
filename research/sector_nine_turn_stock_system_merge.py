#!/usr/bin/env python3
"""把「板块九转」和「选股系统」合起来回测，看合并后能不能同时超过两个单独的策略。

两套策略的重叠与互补（读代码得到的事实，不是猜测）：

- 选股系统 P3 的入场触发之一 ``nine_turn_reversal`` 本来就是"个股低9后首次高2"；
- 选股系统 P3 的 ``trailing_stop`` = 买入后最近红点回撤 ≥ 2 ATR 且低≥2，
  和板块九转的卖出规则只差一条"必须先出现高9"；
- 板块九转独有的是**板块层的九转 + 贪恐闸门**（选股系统的第二层只按贪恐顶/底定仓位，
  不看板块自身的九转形态）；
- 选股系统独有的是**基本面股票池**（硬闸门 + 综合评分排名）、**市场状态定总仓位**、
  以及 ``stop_loss`` / ``pool_exit`` / ``gate_fail`` 这几条出场。

所以"合并"就是：用板块九转的板块层择时决定**什么时候可以买**，用选股系统的基本面池决定
**买哪些**，出场规则两边的条件做组合。这个脚本把这些开关拆成可独立评估的维度。

数据来源全部是生产库的只读副本：

- 基本面池：SQLite ``stock_system_backtest_pool_cache``（选股系统回测留下的逐周 point-in-time 快照，
  含 gate_passed / pool_rank / composite_score / in_pool）；
- 市场状态：``stock_system.sentiment`` 的 ``regime_series`` / ``regime_at``，与实盘同一判定；
- 九转/ATR：``stock_system.indicators.append_nine_turn_atr``，与 K 线图同一函数；
- 选股系统基准净值：SQLite ``stock_system_backtest_navs``（run 3，2022-07 起逐日）。
"""

from __future__ import annotations

import argparse
import json
import math
import pickle
import random
import sqlite3
import sys
from bisect import bisect_right
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from src.core.services.stock_system.indicators import append_nine_turn_atr  # noqa: E402
from src.robot.a_stock_base_data_config import A_STOCK_INDEX_FEAR_GREED_TARGETS  # noqa: E402

DEFAULT_ANALYTICS_DB = "/home/quantd/quant_prod/quant_robot/analytics.duckdb"
DEFAULT_SQLITE_DB = "/home/quantd/quant_prod/quant_robot/evc_stocks.db"
DEFAULT_START = date(2023, 1, 1)
DEFAULT_END = date(2026, 9, 11)          # 与选股系统 run 3 的最后一个净值日对齐
DEFAULT_OUTPUT = ROOT / "research/output/sector_nine_turn_stock_system_merge"
CACHE_PATH = DEFAULT_OUTPUT / "cache.pkl"
WARMUP_DAYS = 400
BENCHMARK_INDEX = "000985.SH"
COMMISSION = 0.0003
STAMP_TAX = 0.0005
TRADING_DAYS = 244
RANDOM_TRIALS = 25

# 与 sector_nine_turn.config 同一份：宽基里只留科创50/100/200 和上证红利
BROAD = frozenset({
    "000300.SH", "000016.SH", "000510.SH", "000905.SH", "000852.SH", "932000.CSI",
    "000985.SH", "899050.BJ", "000680.SH", "000688.SH", "000698.SH", "000699.SH",
    "399006.SZ", "000015.SH",
})
KEPT_BROAD = ("000688.SH", "000698.SH", "000699.SH", "000015.SH")


def default_sectors() -> List[str]:
    codes = []
    for item in A_STOCK_INDEX_FEAR_GREED_TARGETS:
        code = str(item["symbol"]).upper()
        if code in codes:
            continue
        if code in BROAD and code not in KEPT_BROAD:
            continue
        codes.append(code)
    return codes


def all_sectors() -> List[str]:
    return list(dict.fromkeys(str(item["symbol"]).upper() for item in A_STOCK_INDEX_FEAR_GREED_TARGETS))


def industry_sectors() -> List[str]:
    return [code for code in all_sectors() if code not in BROAD]


# ---------------------------------------------------------------------------
# 变体定义
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Variant:
    key: str
    label: str
    # 板块层
    sectors: str = "default54"          # default54 / all64 / industry / none
    fear_threshold: float = 40.0
    fear_lookback: int = 5              # 闸门回看几个交易日（含触发当天）；1 = 旧的"只看当天"
    arm_window: int = 0
    # 个股入场
    low_count_min: int = 9
    buy_high_count: int = 2
    # 基本面（选股系统第一层）
    fundamental: str = "none"           # none / gate / pool / rank300 / rank500 / rank600
    # 排序
    pick: str = "fear_asc"              # fear_asc / score_desc / rank_asc / code
    max_positions: int = 10
    # 出场
    require_high9: bool = True
    exit_atr: float = 2.0
    exit_low_count: int = 2
    exit_low_count_ge: bool = False     # True = 低2及以上都算
    anchor_after_entry: bool = False    # True = 回撤锚点只取买入后的红点（选股系统口径）
    stop_loss_atr: Optional[float] = None   # 买入价 - N×ATR 的初始止损（选股系统口径）
    exit_pool_rank: Optional[int] = None    # 跌出排名就走（选股系统 pool_exit）
    exit_on_gate_fail: bool = False
    # 市场层（选股系统第二层）
    market_gate: bool = False           # 防守状态不开新仓


# ---------------------------------------------------------------------------
# 数据装载（贵，只做一次，落盘缓存）
# ---------------------------------------------------------------------------

def _bars_from_rows(rows: Sequence[Sequence[Any]]) -> List[Dict[str, Any]]:
    return [{
        "timestamp": datetime.combine(row[0], time(15)),
        "open": float(row[1]), "high": float(row[2]), "low": float(row[3]), "close": float(row[4]),
    } for row in rows]


def _nine_turn_arrays(bars: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    rows = append_nine_turn_atr(bars)
    return {
        "dates": [bar["timestamp"].date() for bar in bars],
        "open": np.array([row["open"] for row in rows], dtype=float),
        "close": np.array([row["close"] for row in rows], dtype=float),
        "high_count": np.array([row["highCount"] for row in rows], dtype=np.int16),
        "low_count": np.array([row["lowCount"] for row in rows], dtype=np.int16),
        "atr": np.array([row["atr14"] if row["atr14"] is not None else np.nan for row in rows], dtype=float),
        "drawdown_atr": np.array(
            [row["risingDrawdownAtr"] if row["risingDrawdownAtr"] is not None else np.nan for row in rows],
            dtype=float),
        "rising_close": np.array(
            [row["latestRisingClose"] if row["latestRisingClose"] is not None else np.nan for row in rows],
            dtype=float),
    }


def build_cache(analytics_db: str, sqlite_db: str, start: date, end: date) -> Dict[str, Any]:
    warmup = start - timedelta(days=WARMUP_DAYS)
    sectors = all_sectors()
    print(f"[1/6] 板块日线与九转（{len(sectors)} 个）")
    connection = duckdb.connect(analytics_db, read_only=True)
    try:
        placeholders = ", ".join("?" for _ in sectors + [BENCHMARK_INDEX])
        index_rows = connection.execute(f"""
            SELECT ts_code, trade_date, open, high, low, close FROM a_stock_index_daily
            WHERE ts_code IN ({placeholders}) AND trade_date BETWEEN ? AND ?
              AND open > 0 AND high > 0 AND low > 0 AND close > 0
            ORDER BY ts_code, trade_date
        """, [*sectors, BENCHMARK_INDEX, warmup, end]).fetchall()
        weight_rows = connection.execute(f"""
            SELECT index_code, trade_date, con_code FROM a_stock_index_weight
            WHERE index_code IN ({", ".join("?" for _ in sectors)}) AND trade_date <= ?
            ORDER BY index_code, trade_date
        """, [*sectors, end]).fetchall()
    finally:
        connection.close()

    by_index: Dict[str, List[Any]] = {}
    for row in index_rows:
        by_index.setdefault(str(row[0]).upper(), []).append(row[1:])
    sector_arrays = {code: _nine_turn_arrays(_bars_from_rows(rows)) for code, rows in by_index.items()}

    memberships: Dict[str, Dict[date, List[str]]] = {}
    for index_code, trade_date, con_code in weight_rows:
        day = trade_date if isinstance(trade_date, date) else trade_date.date()
        memberships.setdefault(str(index_code).upper(), {}).setdefault(day, []).append(str(con_code).upper())
    memberships = {code: sorted(snapshots.items()) for code, snapshots in memberships.items()}

    print("[2/6] 贪恐分数与市场状态（生产同一入口）")
    fear = _load_fear(sqlite_db, sectors)
    market = _load_market_regime(sqlite_db)

    print("[3/6] 板块触发日")
    triggers: Dict[str, List[Dict[str, Any]]] = {}
    for code in sectors:
        arrays = sector_arrays.get(code)
        if not arrays:
            continue
        triggers[code] = _turn_signals(arrays, fear.get(code) or {}, start, end)

    print("[4/6] 候选成分股清单")
    candidates: set[str] = set()
    for code, items in triggers.items():
        snapshots = memberships.get(code) or []
        for item in items:
            candidates.update(_members_as_of(snapshots, item["signal_date"]))
    candidates = sorted(candidates)
    print(f"      {len(candidates)} 只")

    print("[5/6] 成分股日线与九转")
    connection = duckdb.connect(analytics_db, read_only=True)
    stock_arrays: Dict[str, Dict[str, Any]] = {}
    names: Dict[str, str] = {}
    try:
        for offset in range(0, len(candidates), 400):
            chunk = candidates[offset:offset + 400]
            placeholders = ", ".join("?" for _ in chunk)
            rows = connection.execute(f"""
                SELECT q.ts_code, q.trade_date, q.open, q.high, q.low, q.close, b.name
                FROM a_stock_market_daily_qfq q
                LEFT JOIN a_stock_basic b USING (ts_code)
                WHERE q.ts_code IN ({placeholders}) AND q.trade_date BETWEEN ? AND ?
                  AND q.open > 0 AND q.high > 0 AND q.low > 0 AND q.close > 0 AND q.vol > 0
                ORDER BY q.ts_code, q.trade_date
            """, [*chunk, warmup, end]).fetchall()
            grouped: Dict[str, List[Any]] = {}
            for row in rows:
                grouped.setdefault(str(row[0]).upper(), []).append(row[1:6])
                names[str(row[0]).upper()] = str(row[6] or "")
            for symbol, bars in grouped.items():
                if len(bars) >= 40:
                    stock_arrays[symbol] = _nine_turn_arrays(_bars_from_rows(bars))
            print(f"      {min(offset + 400, len(candidates))}/{len(candidates)}")
    finally:
        connection.close()

    print("[6/6] 基本面池 point-in-time 快照")
    pool = _load_pool_cache(sqlite_db)

    benchmark = sector_arrays.get(BENCHMARK_INDEX)
    calendar = [day for day in benchmark["dates"] if start <= day <= end]
    return {
        "sector_arrays": sector_arrays, "triggers": triggers, "memberships": memberships,
        "fear": fear, "market": market, "stock_arrays": stock_arrays, "names": names,
        "pool": pool, "calendar": calendar, "start": start, "end": end,
        "benchmark": {day: value for day, value in zip(benchmark["dates"], benchmark["close"])},
    }


def _turn_signals(arrays: Mapping[str, Any], fear: Mapping[date, float],
                  start: date, end: date, low_min: int = 9, high_count: int = 2) -> List[Dict[str, Any]]:
    armed = False
    fired: List[Dict[str, Any]] = []
    dates = arrays["dates"]
    for index in range(len(dates)):
        if arrays["low_count"][index] >= low_min:
            armed = True
        if armed and arrays["high_count"][index] == high_count:
            armed = False
            day = dates[index]
            if start <= day <= end:
                score = fear.get(day)
                fired.append({"index": index, "signal_date": day, "fear_score": score})
    return fired


def _members_as_of(snapshots: Sequence[Tuple[date, List[str]]], day: date) -> List[str]:
    chosen: List[str] = []
    for snapshot_day, members in snapshots:
        if snapshot_day <= day:
            chosen = members
        else:
            break
    return chosen


def _load_fear(sqlite_db: str, codes: Sequence[str]) -> Dict[str, Dict[date, float]]:
    connection = sqlite3.connect(f"file:{Path(sqlite_db).resolve()}?mode=ro", uri=True)
    try:
        placeholders = ", ".join("?" for _ in codes)
        rows = connection.execute(
            f"SELECT symbol, date, score FROM etf_fear_greed_clone_history WHERE symbol IN ({placeholders})",
            list(codes),
        ).fetchall()
    finally:
        connection.close()
    result: Dict[str, Dict[date, float]] = {}
    for symbol, day, score in rows:
        if score is None:
            continue
        result.setdefault(str(symbol).upper(), {})[date.fromisoformat(str(day)[:10])] = float(score)
    return result


def _load_market_regime(sqlite_db: str) -> Dict[date, str]:
    """用生产的 regime_series / regime_at 逐日给出市场状态（进攻/中性/防守）。

    ``ETFFearGreedCloneCalculator`` 通过 ``QUANT_SQLITE_PATH`` 读主库，而 import
    ``core.database`` 会 ``create_all``——不能指向只读的生产库。调用方用
    ``MERGE_SLIM_SQLITE`` 指定一份只含贪恐历史和信号配置的本地副本。
    """
    import os

    os.environ["QUANT_SQLITE_PATH"] = os.environ.get("MERGE_SLIM_SQLITE") or sqlite_db
    from src.core.services.stock_system.sentiment import regime_at, regime_series
    from src.core.services.etf_fear_greed_clone_service import ETFFearGreedCloneCalculator

    calculator = ETFFearGreedCloneCalculator()
    history = calculator.load_history_from_db(
        symbol="000985.SH", include_components=False, include_latest_holdings=False).get("data") or []
    series = regime_series(history)
    timing = {"market_index": "000985.SH", "overheat_enabled": True,
              "signal_expiry_days": 0, "overheat_score": 80.0}
    states: Dict[date, str] = {}
    for text in series["dates"]:
        day = date.fromisoformat(text)
        regime = regime_at(series, day, timing)
        states[day] = f"{regime.get('state')}{'|overheated' if regime.get('overheated') else ''}"
    return states


def _load_pool_cache(sqlite_db: str) -> Dict[str, Any]:
    connection = sqlite3.connect(f"file:{Path(sqlite_db).resolve()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT trade_date, rows FROM stock_system_backtest_pool_cache ORDER BY trade_date").fetchall()
    finally:
        connection.close()
    snapshots: List[Tuple[date, Dict[str, Tuple[bool, Optional[int], Optional[float], bool]]]] = []
    for trade_date, payload in rows:
        day = date.fromisoformat(str(trade_date)[:10])
        items = json.loads(payload) if isinstance(payload, str) else payload
        table = {
            str(item["ts_code"]).upper(): (
                bool(item.get("gate_passed")),
                int(item["pool_rank"]) if item.get("pool_rank") is not None else None,
                float(item["composite_score"]) if item.get("composite_score") is not None else None,
                bool(item.get("in_pool")),
            )
            for item in items
        }
        snapshots.append((day, table))
    return {"dates": [day for day, _ in snapshots], "tables": [table for _, table in snapshots]}


def pool_at(pool: Mapping[str, Any], day: date, symbol: str):
    position = bisect_right(pool["dates"], day) - 1
    if position < 0:
        return None
    return pool["tables"][position].get(symbol)


# ---------------------------------------------------------------------------
# 单个变体的回测
# ---------------------------------------------------------------------------

def _sector_universe(name: str) -> List[str]:
    return {"default54": default_sectors(), "all64": all_sectors(),
            "industry": industry_sectors(), "none": []}[name]


def armed_days(cache: Mapping[str, Any], variant: Variant) -> Dict[str, Dict[date, Dict[str, Any]]]:
    """每只股票在哪些交易日处于"板块布防"状态（含当时的板块与贪恐分数）。"""
    result: Dict[str, Dict[date, Dict[str, Any]]] = {}
    for code in _sector_universe(variant.sectors):
        arrays = cache["sector_arrays"].get(code)
        items = cache["triggers"].get(code)
        if not arrays or not items:
            continue
        snapshots = cache["memberships"].get(code) or []
        dates = arrays["dates"]
        series = cache["fear"].get(code) or {}
        lookback = max(1, variant.fear_lookback)
        for item in items:
            # 闸门看最近 N 个交易日（含触发当天）的最低分；排序也用这个最低分
            window = [series[dates[position]]
                      for position in range(max(0, item["index"] - lookback + 1), item["index"] + 1)
                      if dates[position] in series]
            score = min(window) if window else None
            if score is None or score > variant.fear_threshold:
                continue
            members = _members_as_of(snapshots, item["signal_date"])
            if not members:
                continue
            for offset in range(variant.arm_window + 1):
                position = item["index"] + offset
                if position >= len(dates):
                    break
                day = dates[position]
                info = {"index_code": code, "signal_date": item["signal_date"], "fear_score": score}
                for symbol in members:
                    current = result.setdefault(symbol, {}).get(day)
                    if current is None or score < current["fear_score"]:
                        result[symbol][day] = info
    return result


def stock_turn_indices(arrays: Mapping[str, Any], variant: Variant) -> List[int]:
    armed = False
    fired: List[int] = []
    for index in range(len(arrays["dates"])):
        if arrays["low_count"][index] >= variant.low_count_min:
            armed = True
        if armed and arrays["high_count"][index] == variant.buy_high_count:
            fired.append(index)
            armed = False
    return fired


def _exit_index(arrays: Mapping[str, Any], entry_index: int, variant: Variant,
                pool: Mapping[str, Any], symbol: str) -> Tuple[Optional[int], str]:
    """买入后第一个满足任一出场条件的行号和理由。"""
    dates = arrays["dates"]
    close, high_count, low_count = arrays["close"], arrays["high_count"], arrays["low_count"]
    atr, rising_close = arrays["atr"], arrays["rising_close"]
    drawdown_all = arrays["drawdown_atr"]
    entry_price = arrays["open"][entry_index]
    stop_price = (entry_price - variant.stop_loss_atr * atr[entry_index]
                  if variant.stop_loss_atr and np.isfinite(atr[entry_index]) else None)
    armed = not variant.require_high9
    anchor_close = np.nan
    for index in range(entry_index, len(dates)):
        if variant.anchor_after_entry and high_count[index] >= 2:
            anchor_close = close[index]
        if stop_price is not None and close[index] <= stop_price:
            return index, "stop_loss"
        if variant.exit_pool_rank is not None or variant.exit_on_gate_fail:
            row = pool_at(pool, dates[index], symbol)
            if row is not None:
                gate_passed, pool_rank, _, _ = row
                if variant.exit_on_gate_fail and not gate_passed:
                    return index, "gate_fail"
                if variant.exit_pool_rank is not None and (pool_rank is None or pool_rank > variant.exit_pool_rank):
                    return index, "pool_exit"
        if high_count[index] >= 9:
            armed = True
            continue
        if not armed:
            continue
        matched = (low_count[index] >= variant.exit_low_count if variant.exit_low_count_ge
                   else low_count[index] == variant.exit_low_count)
        if not matched:
            continue
        if variant.anchor_after_entry:
            drawdown = ((anchor_close - close[index]) / atr[index]
                        if np.isfinite(anchor_close) and np.isfinite(atr[index]) and atr[index] > 0 else np.nan)
        else:
            drawdown = drawdown_all[index]
        if np.isfinite(drawdown) and drawdown > variant.exit_atr:
            return index, "trailing_stop"
    return None, "open"


def _fundamental_ok(row, variant: Variant) -> bool:
    if variant.fundamental == "none":
        return True
    if row is None:
        return False
    gate_passed, pool_rank, score, in_pool = row
    if variant.fundamental == "gate":
        return gate_passed
    if variant.fundamental == "pool":
        return in_pool
    if variant.fundamental == "rank300":
        return gate_passed and pool_rank is not None and pool_rank <= 300
    if variant.fundamental == "rank500":
        return gate_passed and pool_rank is not None and pool_rank <= 500
    if variant.fundamental == "rank600":
        return gate_passed and pool_rank is not None and pool_rank <= 600
    raise ValueError(variant.fundamental)


def build_trades(cache: Mapping[str, Any], variant: Variant) -> List[Dict[str, Any]]:
    armed = armed_days(cache, variant) if variant.sectors != "none" else {}
    pool, market = cache["pool"], cache["market"]
    start, end = cache["start"], cache["end"]
    trades: List[Dict[str, Any]] = []
    symbols = armed.keys() if variant.sectors != "none" else cache["stock_arrays"].keys()
    for symbol in symbols:
        arrays = cache["stock_arrays"].get(symbol)
        if arrays is None:
            continue
        dates = arrays["dates"]
        for index in stock_turn_indices(arrays, variant):
            day = dates[index]
            if not (start <= day <= end) or index + 1 >= len(dates):
                continue
            sector = armed.get(symbol, {}).get(day) if variant.sectors != "none" else None
            if variant.sectors != "none" and sector is None:
                continue
            if variant.market_gate and str(market.get(day, "")).startswith("defense"):
                continue
            row = pool_at(pool, day, symbol)
            if not _fundamental_ok(row, variant):
                continue
            entry_index = index + 1
            exit_signal, reason = _exit_index(arrays, entry_index, variant, pool, symbol)
            entry_price = float(arrays["open"][entry_index])
            if exit_signal is not None and exit_signal + 1 < len(dates):
                exit_index = exit_signal + 1
                exit_price = float(arrays["open"][exit_index])
                closed = True
            else:
                exit_index = len(dates) - 1
                exit_price = float(arrays["close"][exit_index])
                closed = False
                reason = "open"
            net = exit_price / entry_price * (1 - COMMISSION) * (1 - COMMISSION - STAMP_TAX) - 1
            trades.append({
                "ts_code": symbol, "name": cache["names"].get(symbol),
                "index_code": (sector or {}).get("index_code"),
                "fear_score": (sector or {}).get("fear_score"),
                "composite_score": row[2] if row else None,
                "pool_rank": row[1] if row else None,
                "signal_date": day, "entry_date": dates[entry_index], "entry_price": entry_price,
                "exit_date": dates[exit_index], "exit_price": exit_price, "closed": closed,
                "exit_reason": reason, "holding_days": exit_index - entry_index,
                "return_pct": net * 100.0,
                "hold_to_end_pct": (float(arrays["close"][-1]) / entry_price
                                    * (1 - COMMISSION) * (1 - COMMISSION - STAMP_TAX) - 1) * 100.0,
            })
    trades.sort(key=lambda item: (item["entry_date"], item["ts_code"]))
    return trades


def simulate(trades: Sequence[Mapping[str, Any]], cache: Dict[str, Any],
             variant: Variant, seed: Optional[int] = None) -> Dict[str, Any]:
    calendar = cache["calendar"]
    closes = cache.get("_closes")
    if closes is None:
        # 逐日盯市的收盘价表：5000 多只股票建一次要几秒，随机试验要跑几十遍，必须只建一次
        closes = {symbol: dict(zip(arrays["dates"], arrays["close"]))
                  for symbol, arrays in cache["stock_arrays"].items()}
        cache["_closes"] = closes
    entries: Dict[date, List[int]] = {}
    for index, trade in enumerate(trades):
        entries.setdefault(trade["entry_date"], []).append(index)
    if seed is None:
        def rank(index: int):
            trade = trades[index]
            if variant.pick == "fear_asc":
                return (trade["fear_score"] if trade["fear_score"] is not None else 1e9, trade["ts_code"])
            if variant.pick == "score_desc":
                return (-(trade["composite_score"] or -1e9), trade["ts_code"])
            if variant.pick == "rank_asc":
                return (trade["pool_rank"] if trade["pool_rank"] is not None else 10**9, trade["ts_code"])
            return (0.0, trade["ts_code"])
        for rows in entries.values():
            rows.sort(key=rank)
    else:
        generator = random.Random(seed)
        for rows in entries.values():
            generator.shuffle(rows)

    cash = 1.0
    positions: Dict[str, Dict[str, Any]] = {}
    last_price: Dict[str, float] = {}
    navs: List[float] = []
    taken: List[int] = []
    invested_days = 0
    for day in calendar:
        for symbol in [code for code, item in positions.items() if item["exit_date"] <= day]:
            cash += positions.pop(symbol)["proceeds"]
        for index in entries.get(day, []):
            trade = trades[index]
            symbol = trade["ts_code"]
            if symbol in positions or len(positions) >= variant.max_positions:
                continue
            equity = cash + sum(item["units"] * last_price.get(code, item["entry_price"])
                                for code, item in positions.items())
            budget = min(cash, equity / variant.max_positions)
            if budget <= 1e-9:
                continue
            cash -= budget
            positions[symbol] = {
                "units": budget / trade["entry_price"], "entry_price": trade["entry_price"],
                "exit_date": trade["exit_date"], "proceeds": budget * (1 + trade["return_pct"] / 100.0),
            }
            taken.append(index)
        market_value = 0.0
        for symbol, item in positions.items():
            price = closes.get(symbol, {}).get(day)
            if price is None or not np.isfinite(price):
                price = last_price.get(symbol, item["entry_price"])
            last_price[symbol] = price
            market_value += item["units"] * price
        navs.append(cash + market_value)
        if positions:
            invested_days += 1
    return {"navs": navs, "taken": taken, "exposure_pct": invested_days / max(len(calendar), 1) * 100.0}


def performance(navs: Sequence[float], calendar: Sequence[date]) -> Dict[str, Any]:
    if not navs:
        return {}
    years = max((calendar[-1] - calendar[0]).days / 365.25, 1 / 365.25)
    returns = [navs[i] / navs[i - 1] - 1 for i in range(1, len(navs)) if navs[i - 1] > 0]
    mean = sum(returns) / len(returns) if returns else 0.0
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1) if len(returns) > 1 else 0.0
    volatility = math.sqrt(variance) * math.sqrt(TRADING_DAYS)
    peak, drawdown = float("-inf"), 0.0
    for value in navs:
        peak = max(peak, value)
        drawdown = min(drawdown, value / peak - 1)
    cagr = navs[-1] ** (1 / years) - 1
    return {
        "total_pct": (navs[-1] - 1) * 100.0, "cagr_pct": cagr * 100.0,
        "vol_pct": volatility * 100.0, "mdd_pct": drawdown * 100.0,
        "calmar": cagr / abs(drawdown) if drawdown < 0 else None,
        "sharpe_like": cagr / volatility if volatility > 0 else None,
    }


def evaluate(cache: Dict[str, Any], variant: Variant, trials: int = RANDOM_TRIALS) -> Dict[str, Any]:
    trades = build_trades(cache, variant)
    base = simulate(trades, cache, variant)
    stats = performance(base["navs"], cache["calendar"])
    returns = [trade["return_pct"] for trade in trades]
    taken = [trades[index] for index in base["taken"]]
    trial_returns: List[float] = []
    for seed in range(trials):
        trial = simulate(trades, cache, variant, seed)
        trial_returns.append((trial["navs"][-1] - 1) * 100.0)
    trial_returns.sort()
    row = {
        "variant": variant.key, "label": variant.label,
        "signals": len(trades), "taken": len(taken),
        "win_pct": float(np.mean([value > 0 for value in returns]) * 100) if returns else None,
        "mean_pct": float(np.mean(returns)) if returns else None,
        "median_pct": float(np.median(returns)) if returns else None,
        "beat_hold_pct": float(np.mean([t["return_pct"] > t["hold_to_end_pct"] for t in trades]) * 100)
        if trades else None,
        "open_end": sum(1 for trade in trades if not trade["closed"]),
        "exposure_pct": base["exposure_pct"],
        **stats,
        "rand_p10": trial_returns[int(len(trial_returns) * 0.1)] if trial_returns else None,
        "rand_med": trial_returns[len(trial_returns) // 2] if trial_returns else None,
        "rand_p90": trial_returns[min(len(trial_returns) - 1, int(len(trial_returns) * 0.9))]
        if trial_returns else None,
    }
    return {"row": row, "trades": trades, "navs": base["navs"]}


def load_cache(analytics_db: str, sqlite_db: str, start: date, end: date, rebuild: bool) -> Dict[str, Any]:
    DEFAULT_OUTPUT.mkdir(parents=True, exist_ok=True)
    if CACHE_PATH.exists() and not rebuild:
        with CACHE_PATH.open("rb") as handle:
            cache = pickle.load(handle)
        if cache.get("start") == start and cache.get("end") == end:
            print(f"[cache] 复用 {CACHE_PATH}")
            return cache
    cache = build_cache(analytics_db, sqlite_db, start, end)
    with CACHE_PATH.open("wb") as handle:
        pickle.dump(cache, handle, protocol=4)
    print(f"[cache] 写入 {CACHE_PATH}")
    return cache


# ---------------------------------------------------------------------------
# 变体清单：先沿各个维度单独试（坐标下降），再把赢的组合起来
# ---------------------------------------------------------------------------

BASE = Variant(key="base", label="板块九转（当前线上默认）")


def stage_two() -> List[Variant]:
    """第一轮的结论：选股系统的每一层单独加上去都让组合收益下降，但单笔质量上升——
    典型的"信号被砍掉太多、仓位空着吃现金拖累"。所以第二轮把过滤和集中度一起调。"""
    variants = []
    for fundamental, label in (("none", "不过滤"), ("gate", "过硬闸门"),
                               ("pool", "股票池前100"), ("rank500", "排名≤500")):
        for count in (3, 5, 8, 10):
            variants.append(replace(
                BASE, key=f"{fundamental}_pos{count}", label=f"{label} × 最多 {count} 仓",
                fundamental=fundamental, max_positions=count))
    # 出场：把降回撤的两条和高集中度组合起来
    for count in (5, 8):
        variants.append(replace(BASE, key=f"gatefail_pos{count}", label=f"硬闸门转不过就走 × {count} 仓",
                                exit_on_gate_fail=True, max_positions=count))
        variants.append(replace(BASE, key=f"poolexit_pos{count}", label=f"跌出排名200就走 × {count} 仓",
                                exit_pool_rank=200, max_positions=count))
        variants.append(replace(BASE, key=f"gatefail_pool_pos{count}",
                                label=f"股票池前100 + 硬闸门出场 × {count} 仓",
                                fundamental="pool", exit_on_gate_fail=True, max_positions=count))
    return variants


def stage_four() -> List[Variant]:
    """贪恐闸门从"只看触发当天"改成"最近 N 个交易日触及过"的影响。"""
    variants = []
    for count in (5, 10):
        base = replace(BASE, max_positions=count)
        for lookback in (1, 2, 3, 5, 8, 10, 15):
            variants.append(replace(
                base, key=f"look{lookback}_pos{count}",
                label=f"贪恐回看 {lookback} 个交易日 × {count} 仓", fear_lookback=lookback))
    return variants


def stage_three() -> List[Variant]:
    """个股买点本身的参数：低 N 起算、买在高几。集中度固定在第二轮胜出的 5 仓。"""
    base5 = replace(BASE, max_positions=5)
    variants = [replace(base5, key="pos5_base", label="板块九转 5 仓（第二轮最好）")]
    for low in (7, 8, 10, 11, 12, 13):
        variants.append(replace(base5, key=f"low{low}", label=f"个股低{low}起算", low_count_min=low))
    for high in (1, 3, 4):
        variants.append(replace(base5, key=f"high{high}", label=f"个股买在高{high}", buy_high_count=high))
    # 板块和个股用不同的低 N（板块要深、个股可浅一点）——需要两套参数，这里用近似：只调个股
    for low, high in ((7, 1), (8, 1), (10, 3), (12, 3)):
        variants.append(replace(base5, key=f"low{low}high{high}", label=f"个股低{low}+高{high}",
                                low_count_min=low, buy_high_count=high))
    return variants


def rank_skill_test(cache: Dict[str, Any], variant: Variant, trials: int = 200) -> List[Dict[str, Any]]:
    """同一批信号，不同挑股顺序 vs 随机挑股的分布——排序规则到底是本事还是运气。"""
    trades = build_trades(cache, variant)
    random_returns = sorted((simulate(trades, cache, variant, seed)["navs"][-1] - 1) * 100.0
                            for seed in range(trials))
    rows = []
    for pick in ("fear_asc", "score_desc", "rank_asc", "code"):
        candidate = replace(variant, pick=pick)
        value = (simulate(trades, cache, candidate)["navs"][-1] - 1) * 100.0
        percentile = sum(1 for item in random_returns if item < value) / len(random_returns) * 100.0
        rows.append({"pick": pick, "total_pct": round(value, 2),
                     "percentile_vs_random": round(percentile, 1),
                     "random_p10": round(random_returns[int(trials * 0.1)], 2),
                     "random_median": round(random_returns[trials // 2], 2),
                     "random_p90": round(random_returns[int(trials * 0.9)], 2)})
    return rows


def stage_one() -> List[Variant]:
    variants = [BASE]
    # 基本面：选股系统第一层能不能提高单笔质量
    for name, label in (("gate", "过硬闸门"), ("pool", "在股票池前100"),
                        ("rank300", "综合排名≤300"), ("rank500", "综合排名≤500"),
                        ("rank600", "综合排名≤600")):
        variants.append(replace(BASE, key=f"fund_{name}", label=f"+基本面：{label}", fundamental=name))
    # 排序：信号多于仓位时挑谁
    for pick, label in (("score_desc", "综合评分高的优先"), ("rank_asc", "池内排名靠前优先"), ("code", "按代码")):
        variants.append(replace(BASE, key=f"pick_{pick}", label=f"+排序：{label}", pick=pick))
    # 最大持仓数
    for count in (5, 8, 15, 20):
        variants.append(replace(BASE, key=f"pos{count}", label=f"+最大持仓 {count}", max_positions=count))
    # 贪恐闸门
    for threshold in (25, 30, 35, 45, 50):
        variants.append(replace(BASE, key=f"fear{threshold}",
                                label=f"+贪恐闸门 ≤{threshold}", fear_threshold=float(threshold)))
    # 板块候选池
    for name, label in (("all64", "全部 64 个板块"), ("industry", "只用行业主题板块"), ("none", "不看板块")):
        variants.append(replace(BASE, key=f"sect_{name}", label=f"+板块范围：{label}", sectors=name))
    # 布防窗口
    for window in (1, 3, 5):
        variants.append(replace(BASE, key=f"win{window}", label=f"+布防窗口 {window} 个交易日", arm_window=window))
    # 出场
    variants += [
        replace(BASE, key="exit_anchor", label="+回撤锚点只取买入后红点（选股系统口径）", anchor_after_entry=True),
        replace(BASE, key="exit_nohigh9", label="+去掉「先出现高9」= 选股系统的跟踪止损", require_high9=False),
        replace(BASE, key="exit_atr15", label="+回撤阈值 1.5 ATR", exit_atr=1.5),
        replace(BASE, key="exit_atr25", label="+回撤阈值 2.5 ATR", exit_atr=2.5),
        replace(BASE, key="exit_atr30", label="+回撤阈值 3 ATR", exit_atr=3.0),
        replace(BASE, key="exit_lowge2", label="+低2及以上都算", exit_low_count_ge=True),
        replace(BASE, key="exit_stop2atr", label="+初始止损 2 ATR（选股系统口径）", stop_loss_atr=2.0),
        replace(BASE, key="exit_stop3atr", label="+初始止损 3 ATR", stop_loss_atr=3.0),
        replace(BASE, key="exit_poolrank200", label="+跌出综合排名 200 就走", exit_pool_rank=200),
        replace(BASE, key="exit_gatefail", label="+硬闸门转不过就走", exit_on_gate_fail=True),
        replace(BASE, key="market_gate", label="+市场防守状态不开新仓", market_gate=True),
    ]
    return variants


def render(rows: Sequence[Mapping[str, Any]]) -> str:
    frame = pd.DataFrame(rows)
    columns = ["variant", "label", "signals", "taken", "win_pct", "mean_pct", "median_pct",
               "beat_hold_pct", "open_end", "exposure_pct", "total_pct", "cagr_pct", "mdd_pct",
               "calmar", "rand_p10", "rand_med", "rand_p90"]
    frame = frame[[column for column in columns if column in frame.columns]]
    for column in frame.columns:
        if frame[column].dtype.kind == "f":
            frame[column] = frame[column].round(2)
    return frame.to_string(index=False)


def baselines(sqlite_db: str, start: date, end: date) -> pd.DataFrame:
    """选股系统 run 3 的逐日净值，截到同一窗口，作为对照组。"""
    connection = sqlite3.connect(f"file:{Path(sqlite_db).resolve()}?mode=ro", uri=True)
    try:
        frame = pd.read_sql_query(
            "SELECT variant, trade_date, nav FROM stock_system_backtest_navs WHERE run_id = 3",
            connection, parse_dates=["trade_date"])
    finally:
        connection.close()
    frame = frame[(frame["trade_date"].dt.date >= start) & (frame["trade_date"].dt.date <= end)]
    rows = []
    for variant, group in frame.groupby("variant"):
        group = group.sort_values("trade_date")
        navs = (group["nav"] / group["nav"].iloc[0]).tolist()
        calendar = [value.date() for value in group["trade_date"]]
        rows.append({"variant": f"选股系统:{variant}", "label": "选股系统 run3", **performance(navs, calendar)})
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analytics-db", default=DEFAULT_ANALYTICS_DB)
    parser.add_argument("--sqlite-db", default=DEFAULT_SQLITE_DB)
    parser.add_argument("--start", default=str(DEFAULT_START))
    parser.add_argument("--end", default=str(DEFAULT_END))
    parser.add_argument("--rebuild-cache", action="store_true")
    parser.add_argument("--trials", type=int, default=RANDOM_TRIALS)
    parser.add_argument("--stage", default="one")
    args = parser.parse_args()

    start = pd.Timestamp(args.start).date()
    end = pd.Timestamp(args.end).date()
    cache = load_cache(args.analytics_db, args.sqlite_db, start, end, args.rebuild_cache)
    print(f"[info] 交易日 {len(cache['calendar'])}，候选股票 {len(cache['stock_arrays'])}，"
          f"基本面快照 {len(cache['pool']['dates'])} 期")

    variants = {"one": stage_one, "two": stage_two, "three": stage_three,
                "four": stage_four}[args.stage]()
    rows, trades_by_variant, navs_by_variant = [], {}, {}
    for variant in variants:
        result = evaluate(cache, variant, args.trials)
        rows.append(result["row"])
        trades_by_variant[variant.key] = result["trades"]
        navs_by_variant[variant.key] = result["navs"]
        row = result["row"]
        print(f"  {variant.key:20s} 信号={row['signals']:6d} 入组={row['taken']:4d} "
              f"胜率={row['win_pct'] or 0:5.1f}% 均值={row['mean_pct'] or 0:6.2f}% "
              f"总收益={row['total_pct']:8.2f}% 回撤={row['mdd_pct']:7.2f}% "
              f"随机中位={row['rand_med'] or 0:8.2f}%")

    DEFAULT_OUTPUT.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(DEFAULT_OUTPUT / f"stage_{args.stage}_summary.csv", index=False)
    with (DEFAULT_OUTPUT / f"stage_{args.stage}_navs.json").open("w") as handle:
        json.dump({"calendar": [day.isoformat() for day in cache["calendar"]], "navs": navs_by_variant}, handle)
    print()
    print(render(rows))
    print()
    print("对照：选股系统 run 3（同一窗口）")
    print(baselines(args.sqlite_db, start, end).round(2).to_string(index=False))

    if args.stage == "two":
        print()
        print("挑股顺序的本事 vs 运气（同一批信号，200 次随机挑股作分布）")
        for count in (5, 10):
            probe = replace(BASE, max_positions=count)
            print(f"  最多 {count} 仓：")
            print(pd.DataFrame(rank_skill_test(cache, probe)).to_string(index=False))


if __name__ == "__main__":
    main()

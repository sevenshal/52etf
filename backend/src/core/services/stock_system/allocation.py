"""选股系统第二层：仓位控制——市场状态定总仓位，板块状态定能不能开新仓和板块上限。

输入：
- 当日股票池（第一层快照，按综合分排名）；
- 每只股票所属的"最细"行业贪恐指数：成分取 ≤ 交易日的最新一期权重快照，和雪球持仓表 /
  个股详情页「所属贪恐指数」同一口径（复用同一个函数），排除宽基和风格指数后取成分股
  最少的那条，一样多时取配置里更靠后（更细分）的；
- 市场（默认中证全指）和各板块的情绪状态（见 sentiment.py）。

分配规则（阈值全部可配置）：
1. 总仓位上限 = 市场状态对应的档位（进攻 / 中性 / 防守，过热按防守）；
2. 按股票池排名逐只考虑，所属板块处于防守或过热的不开新仓；
3. 单只目标权重 = min(单只上限, 总仓位上限 / 最大持仓数)，再受板块剩余额度和总剩余额度
   限制；分到的不足最小仓位就跳过，把名额让给排在后面、其它板块的股票；
4. 不属于任何行业贪恐指数的股票归到"未归类"，跟随市场状态，共用一个上限。

这里给的是"如果今天按规则建组合该怎么配"的目标仓位，不下单；什么时候进出由技术层
（P3）决定。防守板块里已有的持仓不强制卖出（研究显示顶信号直接清仓在强趋势里离场
过早），交给技术层收紧止损。
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from ..duckdb_analytics import connect_analytics_db, duckdb_table_exists
from .config import BROAD_INDEX_CODES, load_stock_system_config, normalize_stock_system_config
from .fundamental_pool import load_pool_snapshot
from .sentiment import (
    STATE_DEFENSE,
    STATE_LABELS,
    HistoryLoader,
    cap_state,
    classify_regime,
    entry_allowed,
    load_regimes,
    target_catalog,
)
from .storage import clean_record, ensure_tables, replace_trade_date_rows, run_record, snapshot_frame

logger = logging.getLogger(__name__)

UNMAPPED_SECTOR = "UNMAPPED"
UNMAPPED_SECTOR_NAME = "未归类"
STATUS_TARGET = "target"
STATUS_BLOCKED = "blocked"
STATUS_SKIPPED = "skipped"
_EPSILON = 1e-9

MembershipLoader = Callable[[Sequence[str], date], Dict[str, List[Dict[str, str]]]]

REGIME_COLUMNS = [
    "trade_date", "index_code", "name", "category", "is_market", "constituent_count", "state",
    "overheated", "stale", "score", "score_date", "signal_side", "signal_label", "signal_date",
    "days_since_signal", "note", "cap_pct", "pool_members", "allocated_pct",
]
ALLOCATION_COLUMNS = [
    "trade_date", "ts_code", "name", "industry", "pool_rank", "composite_score", "sector_code",
    "sector_name", "sector_state", "sector_overheated", "status", "target_weight_pct", "reason",
]
ALLOCATION_RUN_COLUMNS = [
    "trade_date", "as_of", "market_index", "market_state", "market_overheated", "market_score",
    "exposure_pct", "invested_pct", "positions", "duration_seconds", "config_json", "summary_json",
    "created_at",
]


def _default_membership_loader(symbols: Sequence[str], as_of: date) -> Dict[str, List[Dict[str, str]]]:
    """个股所属的有贪恐计算的指数：直接复用雪球持仓表「所属贪恐指数」那一列的算法。"""
    from ....app.api.xueqiu_holdings import _attach_xueqiu_fear_index_memberships

    items = [{"stock_symbol": symbol} for symbol in symbols]
    connection = connect_analytics_db()
    try:
        _attach_xueqiu_fear_index_memberships(connection, items, as_of)
    finally:
        connection.close()
    return {symbol: item.get("fear_indexes") or [] for symbol, item in zip(symbols, items)}


def _constituent_counts(connection, index_codes: Sequence[str], as_of: date) -> Dict[str, int]:
    if not index_codes or not duckdb_table_exists(connection, "a_stock_index_weight"):
        return {}
    placeholders = ", ".join("?" for _ in index_codes)
    rows = connection.execute(
        f"""
        WITH latest AS (
            SELECT index_code, MAX(trade_date) AS trade_date
            FROM a_stock_index_weight
            WHERE index_code IN ({placeholders}) AND trade_date <= ?
            GROUP BY index_code
        )
        SELECT w.index_code, COUNT(*)
        FROM a_stock_index_weight w
        JOIN latest l ON l.index_code = w.index_code AND l.trade_date = w.trade_date
        GROUP BY w.index_code
        """,
        [*index_codes, as_of],
    ).fetchall()
    return {str(code): int(count) for code, count in rows}


def assign_sectors(
    memberships: Mapping[str, Sequence[Mapping[str, str]]],
    counts: Mapping[str, int],
    catalog: Sequence[Mapping[str, Any]],
) -> Dict[str, Optional[str]]:
    """每只股票归到它所属的最细分的行业贪恐指数；只属于宽基/风格指数的返回 None。"""
    order = {item["symbol"]: item["order"] for item in catalog}
    result: Dict[str, Optional[str]] = {}
    for ts_code, indexes in memberships.items():
        candidates = [
            str(index["symbol"]).upper()
            for index in indexes
            if str(index["symbol"]).upper() in order and str(index["symbol"]).upper() not in BROAD_INDEX_CODES
        ]
        result[ts_code] = (
            min(candidates, key=lambda symbol: (counts.get(symbol, 10 ** 9), -order[symbol]))
            if candidates
            else None
        )
    return result


def _blocked_reason(regime: Mapping[str, Any], name: str) -> str:
    if regime.get("overheated"):
        return f"{name}过热：贪恐 {regime.get('score'):.1f}，不追新仓"
    signal = regime.get("signal") or {}
    return f"{name}处于防守：最近信号是 {signal.get('date')} 的{signal.get('label')}，防守档上限为 0，不开新仓"


def plan_allocation(
    pool_rows: Sequence[Mapping[str, Any]],
    sectors: Mapping[str, Optional[str]],
    regimes: Mapping[str, Mapping[str, Any]],
    market_regime: Mapping[str, Any],
    position: Mapping[str, Any],
    sector_names: Mapping[str, str],
) -> Dict[str, Any]:
    """按股票池排名贪心分配目标仓位。返回逐只结果和汇总（权重均为百分比）。"""
    budget = float(position[f"exposure_{cap_state(market_regime)}_pct"]) / 100.0
    max_positions = int(position["max_positions"])
    max_single = float(position["max_single_weight_pct"]) / 100.0
    min_weight = float(position["min_position_weight_pct"]) / 100.0
    base_weight = min(max_single, budget / max_positions) if max_positions > 0 else 0.0

    used_by_sector: Dict[str, float] = {}
    remaining = budget
    positions = 0
    rows: List[Dict[str, Any]] = []
    for row in sorted(pool_rows, key=lambda item: (item.get("pool_rank") or 10 ** 9, item["ts_code"])):
        ts_code = row["ts_code"]
        sector_code = sectors.get(ts_code)
        if sector_code:
            regime = regimes.get(sector_code) or {"state": "neutral"}
            sector_key = sector_code
            sector_name = sector_names.get(sector_code, sector_code)
            cap = float(position[f"sector_cap_{cap_state(regime)}_pct"]) / 100.0
        else:
            regime = market_regime
            sector_key = UNMAPPED_SECTOR
            sector_name = UNMAPPED_SECTOR_NAME
            cap = float(position["unmapped_sector_cap_pct"]) / 100.0
            if cap_state(regime) == STATE_DEFENSE:
                cap = min(cap, float(position["sector_cap_defense_pct"]) / 100.0)
        defending = cap_state(regime) == STATE_DEFENSE and cap <= _EPSILON

        record = {
            "ts_code": ts_code,
            "name": row.get("name"),
            "industry": row.get("industry"),
            "pool_rank": row.get("pool_rank"),
            "composite_score": row.get("composite_score"),
            "sector_code": sector_key,
            "sector_name": sector_name,
            "sector_state": regime.get("state"),
            "sector_overheated": bool(regime.get("overheated")),
            "status": STATUS_TARGET,
            "target_weight_pct": None,
            "reason": None,
        }
        if not entry_allowed(regime) or defending:
            record["status"] = STATUS_BLOCKED
            record["reason"] = _blocked_reason(regime, sector_name if sector_code else "市场")
        elif positions >= max_positions:
            record["status"] = STATUS_SKIPPED
            record["reason"] = f"已达最大持仓数 {max_positions}"
        else:
            sector_room = cap - used_by_sector.get(sector_key, 0.0)
            weight = min(base_weight, sector_room, remaining)
            if weight + _EPSILON < min_weight:
                record["status"] = STATUS_SKIPPED
                if sector_room + _EPSILON < min(base_weight, remaining):
                    record["reason"] = f"{sector_name}板块仓位已满（上限 {cap * 100:g}%）"
                else:
                    record["reason"] = f"总仓位已满（上限 {budget * 100:g}%）"
            else:
                used_by_sector[sector_key] = used_by_sector.get(sector_key, 0.0) + weight
                remaining -= weight
                positions += 1
                record["target_weight_pct"] = round(weight * 100.0, 2)
        rows.append(record)

    invested = budget - remaining
    return {
        "rows": rows,
        "summary": {
            "exposure_pct": round(budget * 100.0, 2),
            "invested_pct": round(invested * 100.0, 2),
            "cash_pct": round(100.0 - invested * 100.0, 2),
            "base_weight_pct": round(base_weight * 100.0, 2),
            "positions": positions,
            "blocked": sum(1 for item in rows if item["status"] == STATUS_BLOCKED),
            "skipped": sum(1 for item in rows if item["status"] == STATUS_SKIPPED),
        },
        "allocated_by_sector": {key: round(value * 100.0, 2) for key, value in used_by_sector.items()},
    }


def _regime_row(
    item: Mapping[str, Any],
    regime: Mapping[str, Any],
    *,
    is_market: bool,
    constituent_count: Optional[int],
    cap_pct: Optional[float],
    pool_members: int,
    allocated_pct: float,
) -> Dict[str, Any]:
    signal = regime.get("signal") or {}
    return {
        "index_code": item["symbol"],
        "name": item["name"],
        "category": item["category"],
        "is_market": is_market,
        "constituent_count": constituent_count,
        "state": regime.get("state"),
        "state_label": STATE_LABELS.get(regime.get("state")),
        "overheated": bool(regime.get("overheated")),
        "stale": bool(regime.get("stale")),
        "score": regime.get("score"),
        "score_date": regime.get("score_date"),
        "signal_side": signal.get("side"),
        "signal_label": signal.get("label"),
        "signal_date": signal.get("date"),
        "days_since_signal": regime.get("days_since_signal"),
        "note": regime.get("note"),
        "cap_pct": cap_pct,
        "pool_members": pool_members,
        "allocated_pct": allocated_pct,
    }


def compute_allocation(
    as_of: Optional[date] = None,
    config: Optional[Mapping[str, Any]] = None,
    *,
    pool_loader: Callable[..., Dict[str, Any]] = load_pool_snapshot,
    history_loader: Optional[HistoryLoader] = None,
    membership_loader: Optional[MembershipLoader] = None,
    connect: Callable[[], Any] = connect_analytics_db,
) -> Dict[str, Any]:
    """用 as_of（默认最新）那一份股票池快照算情绪状态和目标仓位（只读，不写库）。"""
    started = time.monotonic()
    config = normalize_stock_system_config(config if config is not None else load_stock_system_config())
    timing = config["timing"]
    position = config["position"]

    pool = pool_loader(as_of, view="pool")
    run = pool.get("run")
    if not run:
        return {
            "status": "no_pool",
            "message": "还没有股票池快照，先计算第一层基本面股票池",
            "as_of": (as_of or date.today()).isoformat(),
            "trade_date": None,
            "config": config,
            "summary": {},
            "regimes": [],
            "rows": [],
        }
    trade_date = date.fromisoformat(str(run["trade_date"]))
    pool_rows = pool.get("rows") or []
    symbols = [row["ts_code"] for row in pool_rows]

    catalog = target_catalog()
    catalog_symbols = [item["symbol"] for item in catalog]
    memberships = (membership_loader or _default_membership_loader)(symbols, trade_date) if symbols else {}
    connection = connect()
    try:
        counts = _constituent_counts(connection, catalog_symbols, trade_date)
    finally:
        connection.close()
    sectors = assign_sectors(memberships, counts, catalog)

    regimes = load_regimes(catalog_symbols, trade_date, timing, history_loader)
    built = build_allocation(trade_date, pool_rows, sectors, regimes, counts, config, catalog)
    return {
        "status": "completed",
        "as_of": (as_of or trade_date).isoformat(),
        "trade_date": trade_date.isoformat(),
        "config": config,
        **built,
        "duration_seconds": round(time.monotonic() - started, 1),
    }


def build_allocation(
    trade_date: date,
    pool_rows: Sequence[Mapping[str, Any]],
    sectors: Mapping[str, Optional[str]],
    regimes: Mapping[str, Mapping[str, Any]],
    counts: Mapping[str, int],
    config: Mapping[str, Any],
    catalog: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """由股票池、板块归属和各指数状态算出市场状态、目标仓位和板块状态表（纯计算）。

    实盘的 ``compute_allocation`` 和回测逐日回放共用它。
    """
    timing = config["timing"]
    position = config["position"]
    symbols = [row["ts_code"] for row in pool_rows]
    market_code = timing["market_index"]
    market_regime = regimes.get(market_code) or classify_regime([], trade_date, timing)
    sector_names = {item["symbol"]: item["name"] for item in catalog}

    plan = plan_allocation(pool_rows, sectors, regimes, market_regime, position, sector_names)

    members_by_sector = Counter(sectors.get(symbol) or UNMAPPED_SECTOR for symbol in symbols)
    allocated = plan["allocated_by_sector"]
    regime_rows = []
    for item in catalog:
        regime = regimes.get(item["symbol"]) or classify_regime([], trade_date, timing)
        is_market = item["symbol"] == market_code
        if is_market:
            cap_pct = plan["summary"]["exposure_pct"]
        elif item["category"] == "sector":
            cap_pct = float(position[f"sector_cap_{cap_state(regime)}_pct"])
        else:
            cap_pct = None
        regime_rows.append(_regime_row(
            item,
            regime,
            is_market=is_market,
            constituent_count=counts.get(item["symbol"]),
            cap_pct=cap_pct,
            pool_members=members_by_sector.get(item["symbol"], 0),
            allocated_pct=allocated.get(item["symbol"], 0.0),
        ))
    if members_by_sector.get(UNMAPPED_SECTOR):
        regime_rows.append(_regime_row(
            {"symbol": UNMAPPED_SECTOR, "name": UNMAPPED_SECTOR_NAME, "category": "unmapped"},
            {**market_regime, "note": "不属于任何行业贪恐指数，状态跟随市场"},
            is_market=False,
            constituent_count=None,
            cap_pct=float(position["unmapped_sector_cap_pct"]),
            pool_members=members_by_sector[UNMAPPED_SECTOR],
            allocated_pct=allocated.get(UNMAPPED_SECTOR, 0.0),
        ))

    market_signal = market_regime.get("signal") or {}
    market = {
        "index_code": market_code,
        "name": sector_names.get(market_code, market_code),
        "state": market_regime.get("state"),
        "state_label": STATE_LABELS.get(market_regime.get("state")),
        "overheated": bool(market_regime.get("overheated")),
        "stale": bool(market_regime.get("stale")),
        "score": market_regime.get("score"),
        "score_date": market_regime.get("score_date"),
        "signal_label": market_signal.get("label"),
        "signal_date": market_signal.get("date"),
        "days_since_signal": market_regime.get("days_since_signal"),
        "note": market_regime.get("note"),
    }
    sector_rows = [row for row in regime_rows if row["category"] == "sector"]
    summary = {
        **plan["summary"],
        "market": market,
        "pool_size": len(pool_rows),
        "mapped": sum(1 for symbol in symbols if sectors.get(symbol)),
        "sector_states": dict(Counter(row["state"] for row in sector_rows)),
        "sectors_overheated": sum(1 for row in sector_rows if row["overheated"]),
    }
    return {"market": market, "summary": summary, "regimes": regime_rows, "rows": plan["rows"]}


def _as_date(value: Any) -> Optional[date]:
    return date.fromisoformat(str(value)) if value else None


def save_allocation(result: Mapping[str, Any]) -> None:
    """同一交易日重跑时整批覆盖（三张表一个事务）。"""
    if result.get("status") != "completed" or not result.get("trade_date"):
        return
    from ...analytics_database import (
        StockSystemAllocationRun,
        StockSystemAllocationSnapshot,
        StockSystemRegimeSnapshot,
    )

    trade_date = date.fromisoformat(result["trade_date"])
    regimes = snapshot_frame(
        [
            {
                **row,
                "trade_date": trade_date,
                "score_date": _as_date(row.get("score_date")),
                "signal_date": _as_date(row.get("signal_date")),
            }
            for row in result["regimes"]
        ],
        REGIME_COLUMNS,
        text_columns=("index_code", "name", "category", "state", "signal_side", "signal_label", "note"),
        bool_columns=("is_market", "overheated", "stale"),
        int_columns=("constituent_count", "days_since_signal", "pool_members"),
        raw_columns=("trade_date", "score_date", "signal_date"),
    )
    allocations = snapshot_frame(
        [{**row, "trade_date": trade_date} for row in result["rows"]],
        ALLOCATION_COLUMNS,
        text_columns=("ts_code", "name", "industry", "sector_code", "sector_name", "sector_state", "status", "reason"),
        bool_columns=("sector_overheated",),
        int_columns=("pool_rank",),
        raw_columns=("trade_date",),
    )
    market = result.get("market") or {}
    summary = result.get("summary") or {}
    run = snapshot_frame(
        [{
            "trade_date": trade_date,
            "as_of": date.fromisoformat(result["as_of"]),
            "market_index": market.get("index_code"),
            "market_state": market.get("state"),
            "market_overheated": market.get("overheated"),
            "market_score": market.get("score"),
            "exposure_pct": summary.get("exposure_pct"),
            "invested_pct": summary.get("invested_pct"),
            "positions": summary.get("positions"),
            "duration_seconds": result.get("duration_seconds"),
            "config_json": result.get("config") or {},
            "summary_json": summary,
            "created_at": datetime.now(),
        }],
        ALLOCATION_RUN_COLUMNS,
        text_columns=("market_index", "market_state"),
        bool_columns=("market_overheated",),
        int_columns=("positions",),
        raw_columns=("trade_date", "as_of", "created_at"),
        json_columns=("config_json", "summary_json"),
    )
    ensure_tables(StockSystemRegimeSnapshot, StockSystemAllocationSnapshot, StockSystemAllocationRun)
    replace_trade_date_rows(
        trade_date,
        [
            ("stock_system_regime_snapshot", regimes),
            ("stock_system_allocation_snapshot", allocations),
            ("stock_system_allocation_run", run),
        ],
    )


def run_allocation(as_of: Optional[date] = None) -> Dict[str, Any]:
    """定时任务入口：算一次并落库，返回摘要（不含逐股明细）。"""
    result = compute_allocation(as_of=as_of)
    save_allocation(result)
    return {key: value for key, value in result.items() if key not in ("rows", "regimes")}


def load_allocation(
    trade_date: Optional[date] = None,
    *,
    connect: Callable[[], Any] = connect_analytics_db,
) -> Dict[str, Any]:
    """读取某个交易日（默认最新）的情绪状态和目标仓位。"""
    connection = connect()
    try:
        if not duckdb_table_exists(connection, "stock_system_allocation_run"):
            return {"status": "empty", "run": None, "regimes": [], "rows": []}
        if trade_date is None:
            run_frame = connection.execute(
                "SELECT * FROM stock_system_allocation_run ORDER BY trade_date DESC LIMIT 1"
            ).fetchdf()
        else:
            run_frame = connection.execute(
                "SELECT * FROM stock_system_allocation_run WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT 1",
                [trade_date],
            ).fetchdf()
        if run_frame.empty:
            return {"status": "empty", "run": None, "regimes": [], "rows": []}
        run = run_frame.to_dict("records")[0]
        regimes = connection.execute(
            """
            SELECT * FROM stock_system_regime_snapshot
            WHERE trade_date = ?
            ORDER BY is_market DESC, category, allocated_pct DESC NULLS LAST, score DESC NULLS LAST
            """,
            [run["trade_date"]],
        ).fetchdf()
        rows = connection.execute(
            """
            SELECT * FROM stock_system_allocation_snapshot
            WHERE trade_date = ?
            ORDER BY pool_rank NULLS LAST, ts_code
            """,
            [run["trade_date"]],
        ).fetchdf()
    finally:
        connection.close()

    regime_records = []
    for row in regimes.to_dict("records"):
        record = clean_record(row)
        record["state_label"] = STATE_LABELS.get(record.get("state"))
        regime_records.append(record)
    return {
        "status": "completed",
        "run": run_record(run),
        "regimes": regime_records,
        "rows": [clean_record(row) for row in rows.to_dict("records")],
    }

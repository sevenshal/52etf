"""选股系统第三层编排：技术信号 + 模拟盘。

每个交易日在股票池、情绪择时与仓位之后运行：

1. 模拟盘从上次结算日推进到当天（撮合上一次的订单、逐日盯市，见 paper.py）；
2. 对「第二层分到目标仓位、还没持有」的股票判定入场（任一触发 × 全部过滤），对模拟盘持仓判定出场；
3. 按市场总仓位、板块上限、最大持仓数和可用资金把入场信号排成买单，出场信号排成卖单，下一交易日开盘撮合；
4. 当天评估过的每只股票写一行信号快照（DuckDB），订单和账户写回 SQLite。

K 线用和个股详情页同一个加载口径（``load_a_stock_klines_batch``），指标用同一套算法
（``compute_chart_indicators``）；雪球权价比用和「雪球持仓」页同一套口径
（``load_xueqiu_weight_price_ratios``）。
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from . import paper
from .allocation import UNMAPPED_SECTOR, load_allocation
from .config import load_stock_system_config, normalize_stock_system_config
from .fundamental_pool import load_pool_snapshot
from .indicators import compute_chart_indicators
from .sentiment import STATE_DEFENSE
from .storage import clean_record, ensure_tables, replace_trade_date_rows, snapshot_frame
from .technical import evaluate_entry, evaluate_exit, evaluate_xueqiu_filter, planned_weight_pct

logger = logging.getLogger(__name__)

# 自然日：支撑压力 125 根 + 放量 z 值 60 根 + MACD 预热，留足余量
KLINE_LOOKBACK_DAYS = 400

ACTION_BUY = "buy"
ACTION_NO_ROOM = "no_room"
ACTION_FILTERED = "filtered"
ACTION_WATCH = "watch"
ACTION_SELL = "sell"
ACTION_HOLD = "hold"

SIGNAL_COLUMNS = [
    "trade_date", "ts_code", "name", "role", "pool_rank", "sector_name", "sector_state", "target_weight_pct",
    "bar_date", "close", "atr", "trigger_keys", "triggers", "xueqiu_status", "xueqiu_ratio", "xueqiu_detail",
    "filter_passed", "action", "planned_weight_pct", "stop_pct", "stop_price", "exits", "note",
]

KlineLoader = Callable[[Sequence[str], date], Dict[str, List[Dict[str, Any]]]]
XueqiuLoader = Callable[[Sequence[str], date, int], Dict[str, Any]]


def _default_kline_loader(symbols: Sequence[str], trade_date: date) -> Dict[str, List[Dict[str, Any]]]:
    from ...analytics_database import get_analytics_db_ctx
    from ..a_stock_consensus import load_a_stock_klines_batch

    with get_analytics_db_ctx() as db:
        return load_a_stock_klines_batch(
            db, symbols, start_date=trade_date - timedelta(days=KLINE_LOOKBACK_DAYS), end_date=trade_date
        )


def _default_xueqiu_loader(symbols: Sequence[str], trade_date: date, lookback: int) -> Dict[str, Any]:
    from ....app.api.xueqiu_holdings import load_xueqiu_weight_price_ratios

    return load_xueqiu_weight_price_ratios(symbols, trade_date, lookback=lookback)


def _indicators(klines: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    # 支撑压力只算最后两根：入场用今天的，突破用昨天的，没必要把整段历史都回放一遍
    return compute_chart_indicators(klines, output_start_index=max(0, len(klines) - 2))


def _is_defending(*regimes: Mapping[str, Any]) -> bool:
    return any(regime.get("state") == STATE_DEFENSE or regime.get("overheated") for regime in regimes if regime)


def compute_signals(
    trade_date: date,
    config: Mapping[str, Any],
    allocation: Mapping[str, Any],
    pool_rows: Sequence[Mapping[str, Any]],
    holdings: Mapping[str, Mapping[str, Any]],
    *,
    kline_loader: KlineLoader = _default_kline_loader,
    xueqiu_loader: XueqiuLoader = _default_xueqiu_loader,
    indicator_provider: Optional[Callable[[str], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """候选的入场信号和持仓的出场信号（纯计算，不写库）。

    ``indicator_provider(ts_code) -> {"klines", "macd"}`` 由回测传入（整段历史预先算好的指标按日切片）；
    实盘留空，按 ``kline_loader`` 加载 K 线后现算，两条路的指标算法相同。
    """
    signals = config["signals"]
    xueqiu_config = signals["xueqiu_ratio"]
    trade_iso = trade_date.isoformat()
    allocation_rows = {row["ts_code"]: row for row in allocation.get("rows") or []}
    regimes = {row["index_code"]: row for row in allocation.get("regimes") or []}
    market = ((allocation.get("run") or {}).get("summary") or {}).get("market") or {}
    pool_by_symbol = {row["ts_code"]: row for row in pool_rows}

    candidates = [
        row for row in allocation.get("rows") or []
        if row.get("status") == "target" and row["ts_code"] not in holdings
    ]
    symbols = list(dict.fromkeys([*holdings, *(row["ts_code"] for row in candidates)]))
    if indicator_provider is None:
        klines = kline_loader(symbols, trade_date) if symbols else {}

        def indicator_provider(ts_code: str) -> Dict[str, Any]:
            return _indicators(klines.get(ts_code) or [])

    xueqiu: Dict[str, Any] = {"available": False, "reason": "未启用", "items": {}}
    if xueqiu_config["enabled"] and candidates:
        try:
            xueqiu = xueqiu_loader([row["ts_code"] for row in candidates], trade_date, int(xueqiu_config["lookback_days"]))
        except Exception as exc:  # noqa: BLE001 雪球数据出问题只让过滤跳过，不能拖垮信号
            logger.warning("stock system xueqiu ratios unavailable: %s", exc)
            xueqiu = {"available": False, "reason": f"读取失败：{exc}", "items": {}}

    rows: List[Dict[str, Any]] = []
    for candidate in candidates:
        ts_code = candidate["ts_code"]
        processed = indicator_provider(ts_code)
        entry = evaluate_entry(processed["klines"], processed["macd"], signals)
        xueqiu_check = evaluate_xueqiu_filter((xueqiu.get("items") or {}).get(ts_code), xueqiu_config, xueqiu)
        note = entry["note"]
        triggers = entry["triggers"]
        if entry["bar_date"] and entry["bar_date"] != trade_iso:
            note = f"{trade_iso} 没有行情（停牌），不判定"
            triggers = []
        if triggers and xueqiu_check["passed"]:
            action = ACTION_BUY
        elif triggers:
            action = ACTION_FILTERED
        else:
            action = ACTION_WATCH
        rows.append({
            "ts_code": ts_code,
            "name": candidate.get("name"),
            "role": "candidate",
            "pool_rank": candidate.get("pool_rank"),
            "sector_code": candidate.get("sector_code"),
            "sector_name": candidate.get("sector_name"),
            "sector_state": candidate.get("sector_state"),
            "target_weight_pct": candidate.get("target_weight_pct"),
            "bar_date": entry["bar_date"],
            "close": entry["close"],
            "atr": entry["atr"],
            "triggers": triggers,
            "trigger_keys": [trigger["key"] for trigger in triggers],
            "xueqiu_status": xueqiu_check["status"],
            "xueqiu_ratio": xueqiu_check.get("ratio"),
            "xueqiu_detail": xueqiu_check.get("detail"),
            "filter_passed": xueqiu_check["passed"],
            "action": action,
            "planned_weight_pct": (
                planned_weight_pct(candidate.get("target_weight_pct"), entry["stop_pct"], signals["risk_per_trade_pct"])
                if action == ACTION_BUY
                else None
            ),
            "stop_pct": entry["stop_pct"],
            "stop_price": entry["stop_price"],
            "exits": [],
            "note": note,
        })

    for ts_code, position in holdings.items():
        processed = indicator_provider(ts_code)
        bars = processed["klines"]
        pool_row = pool_by_symbol.get(ts_code)
        allocation_row = allocation_rows.get(ts_code) or {}
        sector_code = allocation_row.get("sector_code") or position.get("sector_code") or UNMAPPED_SECTOR
        sector_regime = regimes.get(sector_code) or {}
        defending = _is_defending(sector_regime, market)
        entry_date = position["entry_date"]
        if isinstance(entry_date, str):
            entry_date = date.fromisoformat(entry_date)
        exits = evaluate_exit(
            bars,
            entry_date=entry_date,
            return_pct=paper.position_return_pct(position),
            stop_pct=position.get("stop_pct"),
            defending=defending,
            in_universe=pool_row is not None,
            gate_passed=(pool_row or {}).get("gate_passed"),
            pool_rank=(pool_row or {}).get("pool_rank"),
            signals=signals,
        )
        last = bars[-1] if bars else {}
        rows.append({
            "ts_code": ts_code,
            "name": position.get("name"),
            "role": "holding",
            "pool_rank": (pool_row or {}).get("pool_rank"),
            "sector_code": sector_code,
            "sector_name": allocation_row.get("sector_name") or position.get("sector_name"),
            "sector_state": sector_regime.get("state") or market.get("state"),
            "target_weight_pct": allocation_row.get("target_weight_pct"),
            "bar_date": (last.get("timestamp").date().isoformat() if last.get("timestamp") else None),
            "close": last.get("close"),
            "atr": last.get("atr14"),
            "triggers": [],
            "trigger_keys": [],
            "xueqiu_status": None,
            "xueqiu_ratio": None,
            "xueqiu_detail": None,
            "filter_passed": None,
            "action": ACTION_SELL if exits else ACTION_HOLD,
            "planned_weight_pct": None,
            "stop_pct": position.get("stop_pct"),
            "stop_price": None,
            "exits": exits,
            "note": "板块或市场处于防守，移动止损已收紧" if defending and not exits else None,
        })

    return {
        "rows": rows,
        "xueqiu": {key: xueqiu.get(key) for key in ("available", "reason", "snapshot_date", "compare_snapshot_date", "lookback")},
    }


def plan_orders(
    trade_date: date,
    signal_rows: List[Dict[str, Any]],
    book: Mapping[str, Any],
    allocation: Mapping[str, Any],
    config: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    """把信号排成下一交易日开盘撮合的订单：先卖后买，买单受总仓位、板块上限、持仓数和资金约束。"""
    position_config = config["position"]
    paper_config = config["paper"]
    positions = book["positions"]
    nav = paper.book_nav(book)
    if nav <= 0:
        return []
    sell_fee = (paper_config["commission_pct"] + paper_config["stamp_tax_pct"]) / 100.0
    orders: List[Dict[str, Any]] = []

    selling = set()
    for row in signal_rows:
        if row["role"] != "holding" or row["action"] != ACTION_SELL:
            continue
        position = positions[row["ts_code"]]
        selling.add(row["ts_code"])
        orders.append({
            "signal_date": trade_date, "ts_code": row["ts_code"], "name": row.get("name"),
            "side": paper.SIDE_SELL, "status": paper.ORDER_PENDING, "quantity": position["quantity"],
            "sector_code": position.get("sector_code"), "sector_name": position.get("sector_name"),
            "reason": "；".join(f"{item['label']}：{item['detail']}" for item in row["exits"]),
        })

    regimes = {row["index_code"]: row for row in allocation.get("regimes") or []}
    allocation_rows = {row["ts_code"]: row for row in allocation.get("rows") or []}
    exposure_cap = float(((allocation.get("run") or {}).get("summary") or {}).get("exposure_pct") or 0.0)
    kept = {code: position for code, position in positions.items() if code not in selling}
    invested_pct = sum(float(p.get("market_value") or 0.0) for p in kept.values()) / nav * 100.0
    sector_used: Dict[str, float] = {}
    for code, position in kept.items():
        sector = (allocation_rows.get(code) or {}).get("sector_code") or position.get("sector_code") or UNMAPPED_SECTOR
        sector_used[sector] = sector_used.get(sector, 0.0) + float(position.get("market_value") or 0.0) / nav * 100.0
    cash_available = float(book["account"]["cash"]) + sum(
        float(positions[code].get("market_value") or 0.0) * (1 - sell_fee) for code in selling
    )
    position_count = len(kept)
    max_positions = int(position_config["max_positions"])
    min_weight = float(position_config["min_position_weight_pct"])

    for row in sorted(
        (item for item in signal_rows if item["role"] == "candidate" and item["action"] == ACTION_BUY),
        key=lambda item: (item.get("pool_rank") or 10 ** 9, item["ts_code"]),
    ):
        sector = row.get("sector_code") or UNMAPPED_SECTOR
        sector_cap = (regimes.get(sector) or {}).get("cap_pct")
        if sector_cap is None:
            sector_cap = float(position_config["unmapped_sector_cap_pct"])
        room = min(
            exposure_cap - invested_pct,
            float(sector_cap) - sector_used.get(sector, 0.0),
            cash_available / nav * 100.0,
        )
        weight = min(float(row["planned_weight_pct"] or 0.0), room)
        if position_count >= max_positions:
            row.update(action=ACTION_NO_ROOM, planned_weight_pct=None, note=f"持仓已满 {max_positions} 只")
            continue
        if weight + 1e-9 < min_weight:
            row.update(action=ACTION_NO_ROOM, planned_weight_pct=None,
                       note=f"剩余额度 {max(room, 0.0):.1f}%（总仓位/板块上限/现金），不足最小仓位 {min_weight:g}%")
            continue
        budget = nav * weight / 100.0
        orders.append({
            "signal_date": trade_date, "ts_code": row["ts_code"], "name": row.get("name"),
            "side": paper.SIDE_BUY, "status": paper.ORDER_PENDING, "target_weight_pct": round(weight, 2),
            "budget": budget, "stop_pct": row.get("stop_pct"),
            "sector_code": sector, "sector_name": row.get("sector_name"),
            "reason": "；".join(f"{item['label']}：{item['detail']}" for item in row["triggers"])
            + (f"；{row['xueqiu_detail']}" if row.get("xueqiu_detail") else ""),
        })
        row["planned_weight_pct"] = round(weight, 2)
        invested_pct += weight
        sector_used[sector] = sector_used.get(sector, 0.0) + weight
        cash_available -= budget
        position_count += 1
    return orders


def save_signal_snapshot(trade_date: date, rows: Sequence[Mapping[str, Any]]) -> None:
    from ...analytics_database import StockSystemSignalSnapshot

    frame = snapshot_frame(
        [
            {
                **row,
                "trade_date": trade_date,
                "bar_date": date.fromisoformat(row["bar_date"]) if row.get("bar_date") else None,
            }
            for row in rows
        ],
        SIGNAL_COLUMNS,
        text_columns=("ts_code", "name", "role", "sector_name", "sector_state", "xueqiu_status", "xueqiu_detail",
                      "action", "note"),
        bool_columns=("filter_passed",),
        int_columns=("pool_rank",),
        raw_columns=("trade_date", "bar_date"),
        json_columns=("trigger_keys", "triggers", "exits"),
    )
    ensure_tables(StockSystemSignalSnapshot)
    replace_trade_date_rows(trade_date, [("stock_system_signal_snapshot", frame)])


def run_trading_day(
    as_of: Optional[date] = None,
    config: Optional[Mapping[str, Any]] = None,
    *,
    allocation_loader: Callable[..., Dict[str, Any]] = load_allocation,
    pool_loader: Callable[..., Dict[str, Any]] = load_pool_snapshot,
    kline_loader: KlineLoader = _default_kline_loader,
    xueqiu_loader: XueqiuLoader = _default_xueqiu_loader,
    connect: Optional[Callable[[], Any]] = None,
) -> Dict[str, Any]:
    """定时任务入口：推进模拟盘 → 出信号 → 排订单 → 落库。返回摘要。"""
    started = time.monotonic()
    config = normalize_stock_system_config(config if config is not None else load_stock_system_config())
    allocation = allocation_loader(as_of)
    if not allocation.get("run"):
        return {"status": "no_allocation", "message": "还没有情绪择时与仓位结果，先计算前两层"}
    trade_date = date.fromisoformat(str(allocation["run"]["trade_date"]))
    pool = pool_loader(trade_date, view="all")
    pool_rows = pool.get("rows") or []

    paper_config = config["paper"]
    paper_note = None
    paper_active = False
    settlement: Dict[str, Any] = {"days": [], "events": [], "navs": []}
    book: Dict[str, Any] = {"account": None, "positions": {}, "pending": []}
    if paper_config["enabled"]:
        book = paper.load_book()
        account = book["account"]
        if account is None or account.get("last_trade_date") is None:
            capital = float(account["initial_capital"]) if account else float(paper_config["initial_capital"])
            book["account"] = {
                "id": (account or {}).get("id"),
                "initial_capital": capital,
                "cash": capital,
                "started_on": trade_date,
                "last_trade_date": trade_date,
            }
            settlement["navs"] = [{"trade_date": trade_date, "cash": capital, "market_value": 0.0,
                                   "nav": capital, "positions": 0}]
            paper_note = f"模拟盘从 {trade_date} 开始，初始资金 {capital:,.0f}"
            paper_active = True
        elif account["last_trade_date"] > trade_date:
            paper_note = f"模拟盘已结算到 {account['last_trade_date']}，更早的日期只算信号、不动模拟盘"
        else:
            settle_kwargs = {"connect": connect} if connect else {}
            settlement = paper.settle(book, trade_date, paper_config, **settle_kwargs)
            paper_active = True

    holdings = book["positions"] if paper_active else {}
    signals = compute_signals(
        trade_date, config, allocation, pool_rows, holdings,
        kline_loader=kline_loader, xueqiu_loader=xueqiu_loader,
    )
    orders = plan_orders(trade_date, signals["rows"], book, allocation, config) if paper_active else []
    if paper_active:
        paper.save_book(book, new_orders=orders, replace_signal_date=trade_date, navs=settlement["navs"])
    save_signal_snapshot(trade_date, signals["rows"])

    rows = signals["rows"]
    nav = paper.book_nav(book) if paper_active else None
    return {
        "status": "completed",
        "trade_date": trade_date.isoformat(),
        "summary": {
            "candidates": sum(1 for row in rows if row["role"] == "candidate"),
            "triggered": sum(1 for row in rows if row["role"] == "candidate" and row["trigger_keys"]),
            "filtered": sum(1 for row in rows if row["action"] == ACTION_FILTERED),
            "buy_orders": sum(1 for order in orders if order["side"] == paper.SIDE_BUY),
            "sell_orders": sum(1 for order in orders if order["side"] == paper.SIDE_SELL),
            "holdings": sum(1 for row in rows if row["role"] == "holding"),
            "fills": sum(1 for event in settlement["events"] if event["status"] == paper.ORDER_FILLED),
            "settled_days": settlement["days"],
            "nav": nav,
            "xueqiu": signals["xueqiu"],
            "paper_note": paper_note,
        },
        "duration_seconds": round(time.monotonic() - started, 1),
    }


def load_signals(trade_date: Optional[date] = None, *, connect: Optional[Callable[[], Any]] = None) -> Dict[str, Any]:
    """读取某个交易日（默认最新）的信号快照。"""
    from ..duckdb_analytics import connect_analytics_db, duckdb_table_exists

    connection = (connect or connect_analytics_db)()
    try:
        if not duckdb_table_exists(connection, "stock_system_signal_snapshot"):
            return {"status": "empty", "trade_date": None, "rows": []}
        if trade_date is None:
            latest = connection.execute("SELECT MAX(trade_date) FROM stock_system_signal_snapshot").fetchone()[0]
        else:
            latest = connection.execute(
                "SELECT MAX(trade_date) FROM stock_system_signal_snapshot WHERE trade_date <= ?", [trade_date]
            ).fetchone()[0]
        if latest is None:
            return {"status": "empty", "trade_date": None, "rows": []}
        frame = connection.execute(
            """
            SELECT * FROM stock_system_signal_snapshot WHERE trade_date = ?
            ORDER BY role DESC, pool_rank NULLS LAST, ts_code
            """,
            [latest],
        ).fetchdf()
    finally:
        connection.close()
    rows = [
        clean_record(row, {"trigger_keys": [], "triggers": [], "exits": []})
        for row in frame.to_dict("records")
    ]
    trade_iso = latest.isoformat() if hasattr(latest, "isoformat") else str(latest)[:10]
    return {"status": "completed", "trade_date": trade_iso[:10], "rows": rows}

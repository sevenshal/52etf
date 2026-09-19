"""板块九转策略的每日计算：板块状态 → 个股信号 → 模拟盘出单。

每个交易日做三件事，顺序固定：

1. **板块层**：对配置里的每个板块算九转，找出"低 9 后首次高 2"的触发日，再用当天的自算
   贪恐分数过闸门（默认 ≤ 40）。布防窗口默认 0，也就是板块和个股必须同一天出信号。
2. **个股层**：对处于布防状态的板块，取信号日之前最近一期权重快照里的成分股，算九转，
   当天自己也出现"低 9 后首次高 2"、**并且放量**（log 成交量高于前 20 个交易日均值 1 个标准差）
   的就是买入信号；同时对模拟盘持仓判定卖出
   （买入后出现过高 9，此后首次低 2 且回撤 > 2 个 ATR）。
3. **模拟盘**：先把账户结算推进到当天（昨天的单按今天开盘撮合、收盘盯市），再按空仓位
   数量生成明天开盘执行的买卖单。

信号判定全部走 ``signals`` 里的函数，和回测是同一份代码；撮合走
``stock_system.paper.settle``，和选股系统模拟盘是同一套成交规则。
"""

from __future__ import annotations

import logging
import time as _time
from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ...analytics_database import SectorNineTurnSectorSnapshot, SectorNineTurnSignalSnapshot
from . import data, paper
from .config import (
    BROAD_MARKET_INDEXES,
    index_catalog,
    load_sector_nine_turn_config,
    resolve_index_codes,
)
from .signals import SignalParams, armed_windows, low_high_turn_indices, nine_turn_rows, sector_triggers
from ..stock_system.storage import ensure_tables, replace_trade_date_rows, snapshot_frame

logger = logging.getLogger(__name__)

ROLE_CANDIDATE = "candidate"
ROLE_HOLDING = "holding"
ACTION_BUY = "buy"
ACTION_SELL = "sell"
ACTION_HOLD = "hold"
ACTION_NONE = "none"

_SECTOR_COLUMNS = (
    "trade_date", "index_code", "index_name", "category", "close", "high_count", "low_count",
    "fear_score", "fear_min", "fear_pass_date", "low9_armed", "low9_date", "turn_signal",
    "armed", "armed_since", "fear_passed", "note",
)
_SIGNAL_COLUMNS = (
    "trade_date", "ts_code", "name", "role", "sector_code", "sector_name", "sector_fear_score",
    "sector_signal_date", "close", "atr", "high_count", "low_count", "low9_date", "high9_date",
    "rising_drawdown_atr", "volume_z", "action", "rank", "note",
)


def _sector_state(code: str, bars: Sequence[Mapping[str, Any]], params: SignalParams,
                  fear_series: Mapping[date, float], as_of: date,
                  meta: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """单个板块截至 as_of 的九转 / 贪恐 / 布防状态。"""
    if not bars:
        return None
    rows = nine_turn_rows(bars)
    dates = data.bar_dates(bars)
    if not dates or dates[-1] != as_of:
        # 板块当天没有行情（指数停更或数据没同步），不参与当天选股
        return None
    triggers = sector_triggers(rows, dates, params, fear_series)
    windows = armed_windows(triggers, dates, params, require_fear=True)
    window = windows.get(as_of)
    last = rows[-1]
    low9_date = None
    for index in range(len(rows) - 1, -1, -1):
        if int(rows[index].get("lowCount") or 0) >= params.low_count_min:
            low9_date = dates[index]
            break
    turn_indices = set(low_high_turn_indices(rows, params, high_min=params.sector_high_min,
                                             high_max=params.sector_high_max))
    triggered_today = (len(rows) - 1) in turn_indices
    today_trigger = next((item for item in triggers if item["signal_date"] == as_of), None)
    return {
        "trade_date": as_of,
        "index_code": code,
        "index_name": meta.get("name") or code,
        "category": "宽基" if code in BROAD_MARKET_INDEXES else "行业主题",
        "close": last.get("close"),
        "high_count": int(last.get("highCount") or 0),
        "low_count": int(last.get("lowCount") or 0),
        "fear_score": fear_series.get(as_of),
        # 闸门看的是最近 N 个交易日的最低分，不是当天这一个数
        "fear_min": (today_trigger or {}).get("fear_min"),
        "fear_pass_date": (today_trigger or {}).get("fear_pass_date"),
        "low9_armed": low9_date is not None and not triggered_today,
        "low9_date": low9_date,
        "turn_signal": triggered_today,
        "armed": window is not None,
        "armed_since": window["signal_date"] if window else None,
        "fear_passed": bool(today_trigger and today_trigger.get("fear_passed")),
        "note": None,
    }


def _position_sell_state(rows: Sequence[Mapping[str, Any]], dates: Sequence[date],
                         entry_date: date, params: SignalParams) -> Dict[str, Any]:
    """持仓从买入日到今天的卖出规则状态：是否出现过高 9、哪天出现、是否已经该卖。"""
    entry_index = next((index for index, day in enumerate(dates) if day >= entry_date), None)
    if entry_index is None:
        return {"high9_armed": False, "high9_date": None, "sell_date": None}
    high9_date = None
    for index in range(entry_index, len(rows)):
        if int(rows[index].get("highCount") or 0) >= params.high_count_min:
            high9_date = dates[index]
    from .signals import sell_signal_index

    sell_index = sell_signal_index(rows, entry_index, params)
    return {
        "high9_armed": high9_date is not None,
        "high9_date": high9_date,
        "sell_date": dates[sell_index] if sell_index is not None else None,
    }


def run_trading_day(as_of: Optional[date] = None, *, config: Optional[Mapping[str, Any]] = None,
                    advance_paper: bool = True) -> Dict[str, Any]:
    """跑一个交易日：板块状态 + 个股信号 + 模拟盘推进。返回给定时任务和页面用的摘要。"""
    started = _time.monotonic()
    config = dict(config or load_sector_nine_turn_config())
    params = SignalParams.from_config(config)
    codes = resolve_index_codes(config)
    trade_date = data.latest_trade_date(as_of)
    if trade_date is None:
        return {"status": "skipped", "message": "分析库里没有可用的交易日"}

    catalog = {item["index_code"]: item for item in index_catalog()}
    start = data.warmup_start(trade_date)
    index_bars = data.load_index_bars(codes, start, trade_date)
    fear_scores = data.load_fear_scores(codes, trade_date)

    sector_rows: List[Dict[str, Any]] = []
    armed_sectors: List[Dict[str, Any]] = []
    for code in codes:
        state = _sector_state(code, index_bars.get(code) or [], params,
                              fear_scores.get(code) or {}, trade_date, catalog.get(code) or {})
        if state is None:
            continue
        sector_rows.append(state)
        if state["armed"]:
            armed_sectors.append(state)

    memberships = data.load_index_members([item["index_code"] for item in armed_sectors]) if armed_sectors else {}
    candidates: Dict[str, Dict[str, Any]] = {}
    for sector in armed_sectors:
        members = data.members_as_of(memberships.get(sector["index_code"]) or [], sector["armed_since"])
        for symbol in members:
            # 一只股票可能同时属于多个板块，保留贪恐最低（最恐慌）的那个；
            # 比的是回看窗口内的最低分，和闸门判定用同一个口径
            current = candidates.get(symbol)
            score = sector.get("fear_min") if sector.get("fear_min") is not None else sector.get("fear_score")
            current_score = (current or {}).get("fear_min") or (current or {}).get("fear_score")
            if current is None or (score is not None and (current_score is None or score < current_score)):
                candidates[symbol] = sector

    book = paper.load_book()
    holdings = dict(book["positions"]) if book["account"] else {}
    symbols = sorted(set(candidates) | set(holdings))
    stock_bars = data.load_stock_bars(symbols, start, trade_date) if symbols else {}
    names = data.load_stock_names(symbols) if symbols else {}

    signal_rows: List[Dict[str, Any]] = []
    buy_signals: List[Dict[str, Any]] = []
    sell_signals: List[Dict[str, Any]] = []
    for symbol in symbols:
        bars = stock_bars.get(symbol) or []
        if not bars:
            continue
        rows = nine_turn_rows(bars, params)
        dates = data.bar_dates(bars)
        last = rows[-1]
        sector = candidates.get(symbol)
        position = holdings.get(symbol)
        low9_date = None
        for index in range(len(rows) - 1, -1, -1):
            if int(rows[index].get("lowCount") or 0) >= params.low_count_min:
                low9_date = dates[index]
                break
        record = {
            "trade_date": trade_date,
            "ts_code": symbol,
            "name": names.get(symbol) or (position or {}).get("name"),
            "role": ROLE_HOLDING if position else ROLE_CANDIDATE,
            "sector_code": (sector or {}).get("index_code") or (position or {}).get("sector_code"),
            "sector_name": (sector or {}).get("index_name") or (position or {}).get("sector_name"),
            "sector_fear_score": ((sector or {}).get("fear_min")
                                  if (sector or {}).get("fear_min") is not None
                                  else (sector or {}).get("fear_score")),
            "sector_signal_date": (sector or {}).get("armed_since"),
            "close": last.get("close"),
            "atr": last.get("atr14"),
            "high_count": int(last.get("highCount") or 0),
            "low_count": int(last.get("lowCount") or 0),
            "low9_date": low9_date,
            "high9_date": None,
            "rising_drawdown_atr": last.get("risingDrawdownAtr"),
            "volume_z": last.get("volumeZScore"),
            "action": ACTION_NONE,
            "rank": None,
            "note": None,
        }
        if dates[-1] != trade_date:
            record["note"] = f"最新 K 线停在 {dates[-1]}，当天停牌或数据缺失"
            signal_rows.append(record)
            continue

        if position is not None:
            state = _position_sell_state(rows, dates, position["entry_date"], params)
            record["high9_date"] = state["high9_date"]
            if state["sell_date"] is not None and state["sell_date"] <= trade_date:
                record["action"] = ACTION_SELL
                record["note"] = (
                    f"高{params.high_count_min}后低{params.sell_low_count}，"
                    f"回撤 {record['rising_drawdown_atr']:.2f} 个 ATR"
                    if record["rising_drawdown_atr"] is not None else "满足卖出规则"
                )
                sell_signals.append({**record, "position": position})
            else:
                record["action"] = ACTION_HOLD
                record["note"] = (
                    "等高9后的低2" if state["high9_armed"] else f"还没出现高{params.high_count_min}"
                )
        elif sector is not None:
            fired = set(low_high_turn_indices(
                rows, params, high_min=params.buy_high_min, high_max=params.buy_high_max,
                require_volume=True))
            if (len(rows) - 1) in fired:
                record["action"] = ACTION_BUY
                note = f"低{params.low_count_min}后高{record['high_count']}"
                if params.volume_filter_enabled and record["volume_z"] is not None:
                    note += f"，放量 z={record['volume_z']:.2f}"
                record["note"] = note
                buy_signals.append(record)
            elif (len(rows) - 1) in set(low_high_turn_indices(
                    rows, params, high_min=params.buy_high_min, high_max=params.buy_high_max)):
                # 形态到了但量没放出来：记下来，页面上能看到"差在哪"
                record["note"] = (
                    f"低{params.low_count_min}后高{record['high_count']}，"
                    f"但放量 z="
                    f"{record['volume_z']:.2f} 未过 {params.volume_z_min:g}"
                    if record["volume_z"] is not None else "形态到了但当天没有成交量数据"
                )
        signal_rows.append(record)

    pick_order = str((config.get("portfolio") or {}).get("pick_order") or "fear_asc")
    if pick_order == "fear_asc":
        buy_signals.sort(key=lambda item: (
            item["sector_fear_score"] if item["sector_fear_score"] is not None else 1e9, item["ts_code"]))
    else:
        buy_signals.sort(key=lambda item: item["ts_code"])
    for rank, item in enumerate(buy_signals, start=1):
        item["rank"] = rank
        for record in signal_rows:
            if record["ts_code"] == item["ts_code"]:
                record["rank"] = rank

    ensure_tables(SectorNineTurnSectorSnapshot, SectorNineTurnSignalSnapshot)
    replace_trade_date_rows(trade_date, [
        ("sector_nine_turn_sector_snapshot", snapshot_frame(
            sector_rows, _SECTOR_COLUMNS,
            text_columns=("index_code", "index_name", "category", "note"),
            bool_columns=("low9_armed", "turn_signal", "armed", "fear_passed"),
            int_columns=("high_count", "low_count"),
            raw_columns=("trade_date", "low9_date", "armed_since", "fear_pass_date"),
        )),
        ("sector_nine_turn_signal_snapshot", snapshot_frame(
            signal_rows, _SIGNAL_COLUMNS,
            text_columns=("ts_code", "name", "role", "sector_code", "sector_name", "action", "note"),
            int_columns=("high_count", "low_count", "rank"),
            raw_columns=("trade_date", "sector_signal_date", "low9_date", "high9_date"),
        )),
    ])

    paper_summary = _advance_paper(config, trade_date, buy_signals, sell_signals) if advance_paper else {}
    summary = {
        "trade_date": trade_date.isoformat(),
        "sectors": len(sector_rows),
        "sectors_triggered": sum(1 for row in sector_rows if row["turn_signal"]),
        "sectors_armed": len(armed_sectors),
        "candidates": len(candidates),
        "buy_signals": len(buy_signals),
        "sell_signals": len(sell_signals),
        **paper_summary,
    }
    return {
        "status": "completed",
        "trade_date": trade_date.isoformat(),
        "duration_seconds": round(_time.monotonic() - started, 1),
        "summary": summary,
    }


def _advance_paper(config: Mapping[str, Any], trade_date: date,
                   buy_signals: Sequence[Mapping[str, Any]],
                   sell_signals: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """结算到当天，再按空仓位生成明天开盘执行的买卖单。"""
    paper_config = dict(config.get("paper") or {})
    if not paper_config.get("enabled", True):
        return {"paper_note": "模拟盘已关闭"}

    book = paper.load_book()
    account = book["account"]
    navs: List[Dict[str, Any]] = []
    note = None
    if account is None or account.get("last_trade_date") is None:
        # 首次运行：账户从当天开始，先落一条起始净值；之后每天才有"上次结算日"可推进
        capital = float(account["initial_capital"]) if account else float(paper_config["initial_capital"])
        book["account"] = {
            "id": (account or {}).get("id"),
            "initial_capital": capital,
            "cash": capital,
            "started_on": trade_date,
            "last_trade_date": trade_date,
        }
        navs = [{"trade_date": trade_date, "cash": capital, "market_value": 0.0, "nav": capital, "positions": 0}]
        note = f"模拟盘从 {trade_date} 开始，初始资金 {capital:,.0f}"
    elif account["last_trade_date"] > trade_date:
        return {"paper_note": f"模拟盘已结算到 {account['last_trade_date']}，更早的日期只算信号、不动模拟盘"}
    else:
        navs = paper.settle(book, trade_date, paper_config)["navs"]

    nav = paper.book_nav(book)
    max_positions = int((config.get("portfolio") or {}).get("max_positions") or 10)
    held = set(book["positions"])
    selling = {item["ts_code"] for item in sell_signals}
    orders: List[Dict[str, Any]] = []
    for item in sell_signals:
        orders.append({
            "signal_date": trade_date, "ts_code": item["ts_code"], "name": item.get("name"),
            "side": paper.SIDE_SELL, "status": paper.ORDER_PENDING,
            "sector_code": item.get("sector_code"), "sector_name": item.get("sector_name"),
            "reason": item.get("note"),
        })
    # 今天挂出的卖单要到明天开盘才成交，腾出来的仓位当天不算空位
    free_slots = max(0, max_positions - len(held))
    budget = nav / max_positions if max_positions > 0 else 0.0
    cash = float(book["account"]["cash"])
    for item in buy_signals:
        if free_slots <= 0:
            break
        if item["ts_code"] in held or item["ts_code"] in selling:
            continue
        spend = min(budget, cash)
        if spend <= 0:
            break
        orders.append({
            "signal_date": trade_date, "ts_code": item["ts_code"], "name": item.get("name"),
            "side": paper.SIDE_BUY, "status": paper.ORDER_PENDING, "budget": spend,
            "sector_code": item.get("sector_code"), "sector_name": item.get("sector_name"),
            "reason": f"{item.get('sector_name')} {item.get('note')}",
        })
        cash -= spend
        free_slots -= 1

    paper.save_book(book, new_orders=orders, replace_signal_date=trade_date, navs=navs)
    summary = {
        "nav": nav,
        "fills": sum(1 for order in book["pending"] if order.get("exec_date") == trade_date
                     and order.get("status") == paper.ORDER_FILLED),
        "holdings": len(book["positions"]),
        "buy_orders": sum(1 for order in orders if order["side"] == paper.SIDE_BUY),
        "sell_orders": sum(1 for order in orders if order["side"] == paper.SIDE_SELL),
    }
    if note:
        summary["paper_note"] = note
    return summary

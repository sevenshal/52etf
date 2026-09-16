"""板块九转策略回测：按历史交易日回放，并和两个消融方案、基准对比。

和实盘共用同一份信号代码（``signals``）与同一批数据入口（``data``）：板块触发、个股买入、
卖出判定都调用同样的函数，只是这里一次性把整段历史算完再逐日回放。

消融方案：

- ``strategy``：完整规则（板块九转 + 贪恐闸门 + 个股九转）；
- ``no_fear``：去掉贪恐闸门，其余不变；
- ``stock_only``：完全不看板块，候选池里每只股票自己出"低9→首次高2"就买。

另外对完整规则做若干次"同一天信号多于空仓位时随机挑股"的试验：信号数远多于仓位，
取哪几只对结果影响很大，只报一条确定性净值会高估确定性。
"""

from __future__ import annotations

import logging
import math
from datetime import date
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from . import data
from .config import resolve_index_codes
from .signals import (
    SignalParams,
    armed_windows,
    low_high_turn_indices,
    nine_turn_rows,
    sector_triggers,
    sell_signal_index,
)

logger = logging.getLogger(__name__)

BENCHMARK_INDEX = "000985.SH"
BENCHMARK_LABEL = "中证全指"
TRADING_DAYS_PER_YEAR = 244
RANDOM_TRIALS = 25

VARIANTS = [
    {"key": "strategy", "label": "完整规则", "require_sector": True, "require_fear": True},
    {"key": "no_fear", "label": "去掉贪恐闸门", "require_sector": True, "require_fear": False},
    {"key": "stock_only", "label": "只看个股九转", "require_sector": False, "require_fear": False},
]
VARIANT_KEYS = [variant["key"] for variant in VARIANTS]


class BacktestCancelled(Exception):
    """用户取消了回测。"""


def _maximum_drawdown(values: Sequence[float]) -> float:
    peak = float("-inf")
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1.0)
    return worst


def _performance(navs: Sequence[float], days: Sequence[date]) -> Dict[str, Any]:
    if not navs or navs[0] <= 0:
        return {}
    years = max((days[-1] - days[0]).days / 365.25, 1 / 365.25)
    total = navs[-1] / navs[0] - 1.0
    returns = [navs[index] / navs[index - 1] - 1.0 for index in range(1, len(navs)) if navs[index - 1] > 0]
    mean = sum(returns) / len(returns) if returns else 0.0
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1) if len(returns) > 1 else 0.0
    volatility = math.sqrt(variance) * math.sqrt(TRADING_DAYS_PER_YEAR)
    drawdown = _maximum_drawdown(navs)
    cagr = (navs[-1] / navs[0]) ** (1 / years) - 1.0
    return {
        "total_return_pct": total * 100.0,
        "cagr_pct": cagr * 100.0,
        "annual_volatility_pct": volatility * 100.0,
        "max_drawdown_pct": drawdown * 100.0,
        "calmar": cagr / abs(drawdown) if drawdown < 0 else None,
    }


def _build_candidate_trades(stock_rows: Mapping[str, Dict[str, Any]],
                            armed_by_symbol: Mapping[str, Mapping[date, Mapping[str, Any]]],
                            params: SignalParams, start: date, end: date,
                            require_sector: bool,
                            commission: float, stamp_tax: float) -> List[Dict[str, Any]]:
    """把"板块布防 × 个股信号"配成候选交易，含开平仓日期与价格。"""
    trades: List[Dict[str, Any]] = []
    for symbol, entry in stock_rows.items():
        rows, dates = entry["rows"], entry["dates"]
        opens, closes = entry["opens"], entry["closes"]
        armed = armed_by_symbol.get(symbol) or {}
        for position in low_high_turn_indices(rows, params):
            signal_day = dates[position]
            if not (start <= signal_day <= end) or position + 1 >= len(rows):
                continue
            sector = armed.get(signal_day) if require_sector else None
            if require_sector and sector is None:
                continue
            entry_index = position + 1
            exit_signal = sell_signal_index(rows, entry_index, params)
            entry_price = opens[entry_index]
            if not entry_price:
                continue
            if exit_signal is not None and exit_signal + 1 < len(rows):
                exit_index = exit_signal + 1
                exit_price = opens[exit_index]
                closed = True
            else:
                exit_index = len(rows) - 1
                exit_price = closes[exit_index]
                closed = False
            if not exit_price:
                continue
            net = exit_price / entry_price * (1 - commission) * (1 - commission - stamp_tax) - 1.0
            trades.append({
                "ts_code": symbol,
                "name": entry.get("name"),
                "sector_code": (sector or {}).get("index_code"),
                "sector_name": (sector or {}).get("index_name"),
                "sector_signal_date": (sector or {}).get("signal_date"),
                "sector_fear_score": (sector or {}).get("fear_score"),
                "signal_date": signal_day,
                "entry_date": dates[entry_index],
                "entry_price": entry_price,
                "exit_date": dates[exit_index],
                "exit_price": exit_price,
                "closed": closed,
                "sell_drawdown_atr": rows[exit_signal].get("risingDrawdownAtr") if exit_signal is not None else None,
                "holding_days": exit_index - entry_index,
                "return_pct": net * 100.0,
            })
    trades.sort(key=lambda item: (item["entry_date"], item["ts_code"]))
    return trades


def _simulate_portfolio(trades: Sequence[Mapping[str, Any]], closes_by_symbol: Mapping[str, Mapping[date, float]],
                        calendar: Sequence[date], max_positions: int,
                        pick_order: str, seed: Optional[int] = None) -> Dict[str, Any]:
    """等权组合：每笔按 净值/最大持仓数 下单，同一只股票不重复持有。

    ``seed`` 给定时当天信号随机排序，用来衡量"挑哪几只"的运气成分。
    """
    entries: Dict[date, List[int]] = {}
    for index, trade in enumerate(trades):
        entries.setdefault(trade["entry_date"], []).append(index)
    if seed is None:
        if pick_order == "fear_asc":
            def rank(index: int):
                score = trades[index].get("sector_fear_score")
                return (score if score is not None else 1e9, trades[index]["ts_code"])
        else:
            def rank(index: int):
                return (0.0, trades[index]["ts_code"])
        for rows in entries.values():
            rows.sort(key=rank)
    else:
        import random

        generator = random.Random(seed)
        for rows in entries.values():
            generator.shuffle(rows)

    cash = 1.0
    positions: Dict[str, Dict[str, Any]] = {}
    last_price: Dict[str, float] = {}
    navs: List[float] = []
    counts: List[int] = []
    taken: List[int] = []
    for day in calendar:
        for symbol in [code for code, item in positions.items() if item["exit_date"] <= day]:
            cash += positions.pop(symbol)["proceeds"]
        for index in entries.get(day, []):
            trade = trades[index]
            symbol = trade["ts_code"]
            if symbol in positions or len(positions) >= max_positions:
                continue
            equity = cash + sum(
                item["units"] * last_price.get(code, item["entry_price"])
                for code, item in positions.items()
            )
            budget = min(cash, equity / max_positions)
            if budget <= 1e-9:
                continue
            units = budget / trade["entry_price"]
            cash -= budget
            positions[symbol] = {
                # units 只用于逐日盯市（按收盘价），落袋金额直接用已扣费的净收益率算
                "units": units,
                "entry_price": trade["entry_price"],
                "exit_date": trade["exit_date"],
                "proceeds": budget * (1.0 + trade["return_pct"] / 100.0),
            }
            taken.append(index)
        market_value = 0.0
        for symbol, item in positions.items():
            price = (closes_by_symbol.get(symbol) or {}).get(day)
            if price is None or not math.isfinite(price):
                price = last_price.get(symbol, item["entry_price"])
            last_price[symbol] = price
            market_value += item["units"] * price
        navs.append(cash + market_value)
        counts.append(len(positions))
    return {"navs": navs, "positions": counts, "taken": taken}


def _trade_stats(trades: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    if not trades:
        return {"trades": 0}
    returns = [trade["return_pct"] for trade in trades]
    ordered = sorted(returns)
    middle = len(ordered) // 2
    median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    wins = [value for value in returns if value > 0]
    losses = [value for value in returns if value < 0]
    holding = sorted(trade["holding_days"] for trade in trades)
    return {
        "trades": len(trades),
        "closed_trades": sum(1 for trade in trades if trade["closed"]),
        "open_at_end": sum(1 for trade in trades if not trade["closed"]),
        "win_rate_pct": len(wins) / len(returns) * 100.0,
        "mean_return_pct": sum(returns) / len(returns),
        "median_return_pct": median,
        "median_holding_days": holding[len(holding) // 2],
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
    }


def run_backtest(config: Mapping[str, Any], start: date, end: Optional[date] = None, *,
                 progress: Optional[Callable[[float, str], None]] = None,
                 should_cancel: Optional[Callable[[], bool]] = None,
                 random_trials: int = RANDOM_TRIALS) -> Dict[str, Any]:
    """回放整段历史；``progress(比例, 说明)`` 供任务页轮询，``should_cancel`` 供取消。"""

    def report(ratio: float, message: str) -> None:
        if progress:
            progress(max(0.0, min(1.0, ratio)), message)
        if should_cancel and should_cancel():
            raise BacktestCancelled()

    params = SignalParams.from_config(config)
    codes = resolve_index_codes(config)
    paper_config = dict(config.get("paper") or {})
    commission = float(paper_config.get("commission_pct", 0.03)) / 100.0
    stamp_tax = float(paper_config.get("stamp_tax_pct", 0.05)) / 100.0
    max_positions = int((config.get("portfolio") or {}).get("max_positions") or 10)
    pick_order = str((config.get("portfolio") or {}).get("pick_order") or "fear_asc")

    end = end or data.latest_trade_date()
    if end is None or start >= end:
        raise ValueError("回测区间不合法")

    report(0.02, "加载板块日线与贪恐历史")
    warmup = data.warmup_start(start)
    index_bars = data.load_index_bars([*codes, BENCHMARK_INDEX], warmup, end)
    fear_scores = data.load_fear_scores(codes, end)

    report(0.10, "计算板块触发与布防窗口")
    from .config import index_catalog

    catalog = {item["index_code"]: item for item in index_catalog()}
    sector_windows: Dict[str, Dict[date, Dict[str, Any]]] = {}
    sector_windows_nofear: Dict[str, Dict[date, Dict[str, Any]]] = {}
    trigger_rows: List[Dict[str, Any]] = []
    for code in codes:
        bars = index_bars.get(code) or []
        if not bars:
            continue
        rows = nine_turn_rows(bars)
        dates = data.bar_dates(bars)
        triggers = sector_triggers(rows, dates, params, fear_scores.get(code) or {})
        meta = catalog.get(code) or {}
        for trigger in triggers:
            if start <= trigger["signal_date"] <= end:
                trigger_rows.append({
                    "index_code": code, "index_name": meta.get("name") or code,
                    "signal_date": trigger["signal_date"], "fear_score": trigger["fear_score"],
                    "fear_passed": trigger["fear_passed"],
                })
        for target, require_fear in ((sector_windows, True), (sector_windows_nofear, False)):
            window = armed_windows(triggers, dates, params, require_fear=require_fear)
            target[code] = {
                day: {**value, "index_code": code, "index_name": meta.get("name") or code}
                for day, value in window.items() if start <= day <= end
            }

    report(0.18, "取板块成分股")
    memberships = data.load_index_members(codes)
    armed_full: Dict[str, Dict[date, Dict[str, Any]]] = {}
    armed_nofear: Dict[str, Dict[date, Dict[str, Any]]] = {}
    candidate_codes: set[str] = set()
    for source, target in ((sector_windows, armed_full), (sector_windows_nofear, armed_nofear)):
        for code, windows in source.items():
            snapshots = memberships.get(code) or []
            for day, info in windows.items():
                members = data.members_as_of(snapshots, info["signal_date"])
                for symbol in members:
                    existing = target.setdefault(symbol, {}).get(day)
                    score = info.get("fear_score")
                    if existing is None or (score is not None and (existing.get("fear_score") is None
                                                                   or score < existing["fear_score"])):
                        target[symbol][day] = info
                    candidate_codes.add(symbol)

    if not candidate_codes:
        raise ValueError("区间内没有任何板块触发，无法回测")

    report(0.25, f"加载 {len(candidate_codes)} 只成分股日线")
    symbols = sorted(candidate_codes)
    stock_rows: Dict[str, Dict[str, Any]] = {}
    names = data.load_stock_names(symbols)
    chunk_size = 300
    for offset in range(0, len(symbols), chunk_size):
        chunk = symbols[offset:offset + chunk_size]
        bars_by_symbol = data.load_stock_bars(chunk, warmup, end)
        for symbol in chunk:
            bars = bars_by_symbol.get(symbol) or []
            if len(bars) < 30:
                continue
            rows = nine_turn_rows(bars)
            dates = data.bar_dates(bars)
            stock_rows[symbol] = {
                "rows": rows,
                "dates": dates,
                "opens": [row.get("open") for row in rows],
                "closes": [row.get("close") for row in rows],
                "close_by_date": {day: rows[index].get("close") for index, day in enumerate(dates)},
                "name": names.get(symbol),
            }
        report(0.25 + 0.35 * (offset + chunk_size) / max(len(symbols), 1),
               f"已算九转 {min(offset + chunk_size, len(symbols))}/{len(symbols)} 只")

    benchmark_bars = [bar for bar in (index_bars.get(BENCHMARK_INDEX) or [])
                      if start <= bar["timestamp"].date() <= end]
    calendar = [bar["timestamp"].date() for bar in benchmark_bars]
    if not calendar:
        raise ValueError("缺少基准指数行情，无法回测")
    benchmark_navs = [bar["close"] / benchmark_bars[0]["close"] for bar in benchmark_bars]
    closes_by_symbol = {symbol: entry["close_by_date"] for symbol, entry in stock_rows.items()}

    results: Dict[str, Any] = {}
    all_trades: Dict[str, List[Dict[str, Any]]] = {}
    for step, variant in enumerate(VARIANTS):
        report(0.62 + 0.10 * step, f"回放方案：{variant['label']}")
        armed = armed_full if variant["require_fear"] else armed_nofear
        trades = _build_candidate_trades(
            stock_rows, armed, params, start, end, variant["require_sector"], commission, stamp_tax)
        simulation = _simulate_portfolio(trades, closes_by_symbol, calendar, max_positions, pick_order)
        taken = set(simulation["taken"])
        for index, trade in enumerate(trades):
            trade["taken"] = index in taken
        stats = _trade_stats(trades)
        performance = _performance(simulation["navs"], calendar)
        trial_returns: List[float] = []
        if variant["key"] == "strategy" and trades and random_trials > 0:
            for seed in range(random_trials):
                trial = _simulate_portfolio(trades, closes_by_symbol, calendar, max_positions, pick_order, seed)
                trial_returns.append((trial["navs"][-1] - 1.0) * 100.0)
                if should_cancel and should_cancel():
                    raise BacktestCancelled()
        trial_returns.sort()
        results[variant["key"]] = {
            "key": variant["key"], "label": variant["label"],
            **stats, **performance,
            "portfolio_trades": len(taken),
            "random_pick_trials": len(trial_returns),
            "random_pick_return_p10_pct": trial_returns[int(len(trial_returns) * 0.1)] if trial_returns else None,
            "random_pick_return_median_pct": trial_returns[len(trial_returns) // 2] if trial_returns else None,
            "random_pick_return_p90_pct": trial_returns[min(len(trial_returns) - 1, int(len(trial_returns) * 0.9))]
            if trial_returns else None,
            "navs": simulation["navs"],
            "positions": simulation["positions"],
        }
        all_trades[variant["key"]] = trades

    report(0.95, "汇总结果")
    benchmark = {
        "key": "benchmark", "label": f"{BENCHMARK_LABEL}买入持有",
        **_performance(benchmark_navs, calendar), "navs": benchmark_navs,
    }
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "calendar": [day.isoformat() for day in calendar],
        "sectors": len(codes),
        "sector_triggers": len(trigger_rows),
        "sector_triggers_fear_passed": sum(1 for row in trigger_rows if row["fear_passed"]),
        "candidate_stocks": len(stock_rows),
        "max_positions": max_positions,
        "variants": results,
        "benchmark": benchmark,
        "trades": all_trades,
        "triggers": trigger_rows,
    }

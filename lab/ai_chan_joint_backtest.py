"""AI 推荐 × 自有缠论一买/一卖的组合级联合回测。

规则：
* 每个交易日的 AI 推荐只在推荐时间之后有效；盘前推荐可在当日开盘处理；
  盘中推荐不能倒推到开盘，只能等待推荐之后确认的新 1m 一买。
* 若推荐日前最近一笔日线自有缠论一买仍在 lookback 交易日内，且开盘相对昨收
  不高于 1%，开盘买入；高开超过 1% 则等待推荐之后的新 1m 一买。
* 其他情况均等待推荐之后的新 1m 一买，下一根 1m 开盘成交。
* 单只股票投入不超过组合权益20%，不补仓；卖出信号在K线确认后下一根1m开盘
  执行，日线一卖在下一交易日开盘执行。买入当日禁止卖出，严格遵守T+1。

这是研究脚本，不修改生产策略，也不使用未确认的未来K线。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "lab")]

from chan_native import Kline, calculate, detect_buy_sell, build_segments  # noqa: E402
from src.core.services.chan_analysis import analyze_bars_czsc_legacy  # noqa: E402


BUY = "一买"
SELL = "一卖"
BUY_FEE_RATE = 0.00025
TRANSFER_RATE = 0.00001
SELL_STAMP = 0.0005
MIN_COMMISSION = 5.0


def buy_fee(amount: float) -> float:
    return max(MIN_COMMISSION, amount * BUY_FEE_RATE) + amount * TRANSFER_RATE


def sell_fee(amount: float) -> float:
    return max(MIN_COMMISSION, amount * BUY_FEE_RATE) + amount * (TRANSFER_RATE + SELL_STAMP)


@dataclass(frozen=True)
class Config:
    analytics_db: str
    main_db: str
    start_date: date
    end_date: date
    daily_lookback: int
    max_stock_weight: float = 0.20
    gap_threshold: float = 0.01
    recommendation_1m_only: bool = False
    exit_only_without_today_recommendation: bool = False


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--analytics-db", default="/home/quantd/quant_prod/quant_robot/analytics.duckdb")
    p.add_argument("--main-db", default="/home/quantd/quant_prod/quant_robot/evc_stocks.db")
    p.add_argument("--start-date", default="2026-08-12")
    p.add_argument("--end-date", default="2026-08-28")
    p.add_argument("--lookbacks", nargs="+", type=int, default=[20, 40, 60, 90, 120])
    p.add_argument("--output-dir", default="lab/output/ai_chan_joint_backtest_20260829")
    p.add_argument("--symbol-limit", type=int, default=0, help="仅用于快速冒烟测试；0表示全部")
    p.add_argument("--signal-engine", choices=("native", "czsc_legacy"), default="native")
    p.add_argument("--recommendation-1m-only", action="store_true", help="仅推荐后1m一买入场")
    p.add_argument("--exit-only-without-today-recommendation", action="store_true", help="仅今日未推荐且非当日买入时按1m一卖退出")
    return p.parse_args()


def load_recommendations(path: str, start: date, end: date) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return all successful rec rows and the latest pre-open batch per day."""
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        runs = pd.read_sql_query(
            """
            SELECT id AS run_id, run_at, trade_date, run_type, prompt_version, model_name
            FROM ai_stock_recommendation_runs
            WHERE status='SUCCESS' AND trade_date BETWEEN ? AND ?
            ORDER BY run_at
            """,
            db,
            params=[start.isoformat(), end.isoformat()],
            parse_dates=["run_at", "trade_date"],
        )
        if runs.empty:
            return runs, runs
        recs = pd.read_sql_query(
            """
            SELECT a.id AS recommendation_id, a.run_id, a.ts_code, a.name,
                   a.ai_confidence, a.execution_score, a.rank,
                   r.run_at, r.trade_date, r.run_type, r.prompt_version, r.model_name
            FROM ai_stock_recommendations a
            JOIN ai_stock_recommendation_runs r ON r.id=a.run_id
            WHERE r.status='SUCCESS' AND r.trade_date BETWEEN ? AND ?
            ORDER BY r.run_at, a.rank, a.ts_code
            """,
            db,
            params=[start.isoformat(), end.isoformat()],
            parse_dates=["run_at", "trade_date"],
        )
    recs["trade_date"] = pd.to_datetime(recs["trade_date"]).dt.date
    recs["run_at"] = pd.to_datetime(recs["run_at"])
    # A symbol is considered recommended at its first appearance on that date.
    # This avoids counting every hourly refresh as a new independent signal.
    recs = recs.sort_values(["trade_date", "ts_code", "run_at", "rank"])
    first = recs.drop_duplicates(["trade_date", "ts_code"], keep="first").copy()
    first["available_at"] = first["run_at"]
    # Latest successful pre-open batch is the only batch allowed to trigger the
    # special open-buy branch.  A failed/duplicate early batch is not treated as
    # an extra candidate.
    pre = recs[(recs["run_type"] == "PREOPEN") & (recs["run_at"].dt.time <= time(9, 30))]
    if pre.empty:
        return first, pre
    latest_run = pre.groupby("trade_date")["run_at"].transform("max") == pre["run_at"]
    return first, pre[latest_run].copy()


def load_market_data(
    analytics_db: str, symbols: list[str], start: date, end: date
) -> tuple[pd.DataFrame, pd.DataFrame, list[date]]:
    con = duckdb.connect(analytics_db, read_only=True)
    con.register("joint_symbols", pd.DataFrame({"ts_code": symbols}))
    daily = con.execute(
        """
        SELECT d.ts_code, d.trade_date, d.open, d.high, d.low, d.close, d.pre_close
        FROM a_stock_market_daily_qfq d JOIN joint_symbols s USING (ts_code)
        WHERE d.trade_date <= ? ORDER BY d.ts_code, d.trade_date
        """,
        [end],
    ).fetchdf()
    minute = con.execute(
        """
        SELECT m.ts_code, m.trade_time, m.open, m.high, m.low, m.close
        FROM a_stock_minute_bar_qfq m JOIN joint_symbols s USING (ts_code)
        WHERE CAST(m.trade_time AS DATE) BETWEEN ? AND ?
        ORDER BY m.ts_code, m.trade_time
        """,
        [start - timedelta(days=220), end],
    ).fetchdf()
    sessions = con.execute(
        """
        SELECT DISTINCT trade_date FROM a_stock_market_daily_qfq
        WHERE trade_date <= ? ORDER BY trade_date
        """,
        [end],
    ).fetchdf()["trade_date"].tolist()
    con.close()
    daily["trade_date"] = pd.to_datetime(daily["trade_date"]).dt.date
    minute["trade_time"] = pd.to_datetime(minute["trade_time"])
    sessions = [pd.Timestamp(x).date() for x in sessions]
    return daily, minute, sessions


def native_events(frame: pd.DataFrame, time_col: str, daily: bool = False) -> dict[pd.Timestamp, set[str]]:
    if len(frame) < 30:
        return {}
    bars = [
        Kline(i=i, high=float(row.high), low=float(row.low), dt=getattr(row, time_col))
        for i, row in enumerate(frame.itertuples())
    ]
    _, _, strokes, centers = calculate(bars, min_gap=4)
    events = detect_buy_sell(strokes, build_segments(strokes), centers)
    out: dict[pd.Timestamp, set[str]] = defaultdict(set)
    for event in events:
        if event.kind not in {BUY, SELL}:
            continue
        if event.confirm_i < 0 or event.confirm_i >= len(frame):
            continue
        ts = pd.Timestamp(frame.iloc[event.confirm_i][time_col])
        out[ts].add(event.kind)
    return dict(out)


def prepare_symbol_features(daily: pd.DataFrame, minute: pd.DataFrame, signal_engine: str = "native") -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for symbol, d in daily.groupby("ts_code", sort=False):
        d = d.sort_values("trade_date").reset_index(drop=True)
        # Events are generated from all available history, but each event is
        # only exposed at its own confirmed daily close.
        if signal_engine == "czsc_legacy":
            d_rows = [{"timestamp": pd.Timestamp(r.trade_date), "open": r.open, "high": r.high, "low": r.low, "close": r.close, "volume": 0, "turnover": 0} for r in d.itertuples()]
            d_events = _legacy_events(symbol, d_rows, "d")
        else:
            d_events = native_events(d, "trade_date", daily=True)
        m = minute[minute["ts_code"] == symbol].sort_values("trade_time").reset_index(drop=True)
        if signal_engine == "czsc_legacy":
            m_rows = [{"timestamp": pd.Timestamp(r.trade_time), "open": r.open, "high": r.high, "low": r.low, "close": r.close, "volume": 0, "turnover": 0} for r in m.itertuples()]
            m_events = _legacy_events(symbol, m_rows, "1m")
        else:
            m_events = native_events(m, "trade_time")
        result[symbol] = {
            "daily": d,
            "minute": m,
            "daily_events": d_events,
            "minute_events": m_events,
        }
    return result


def _legacy_events(symbol: str, rows: list[dict[str, Any]], freq: str) -> dict[pd.Timestamp, set[str]]:
    if len(rows) < 20:
        return {}
    try:
        analysis = analyze_bars_czsc_legacy(symbol, rows, freq, confirmed=True, include_history=True)
    except Exception:
        return {}
    out: dict[pd.Timestamp, set[str]] = defaultdict(set)
    for signal in analysis.get("signal_history", []):
        kind = signal.get("type")
        if kind in {BUY, SELL}:
            out[pd.Timestamp(signal["bar_time"])] .add(kind)
    return dict(out)


def _latest_daily_state(events: dict[pd.Timestamp, set[str]], as_of: date, sessions: list[date], lookback: int) -> tuple[bool, int | None]:
    eligible_sessions = [x for x in sessions if x < as_of]
    if not eligible_sessions:
        return False, None
    allowed = set(eligible_sessions[-lookback:])
    candidates: list[tuple[date, str]] = []
    for ts, kinds in events.items():
        d = pd.Timestamp(ts).date()
        if d >= as_of:
            continue
        for kind in kinds:
            if kind in {BUY, SELL}:
                candidates.append((d, kind))
    if not candidates:
        return False, None
    latest_date, latest_kind = max(candidates)
    return latest_kind == BUY and latest_date in allowed, (as_of - latest_date).days


def _first_open(daily: pd.DataFrame, day: date) -> tuple[float | None, float | None]:
    row = daily[daily.trade_date == day]
    if row.empty:
        return None, None
    r = row.iloc[0]
    prev = r.pre_close
    if pd.isna(prev) or not prev or prev <= 0:
        prior = daily[daily.trade_date < day]
        prev = prior.iloc[-1].close if not prior.empty else None
    return float(r.open), (float(prev) if prev is not None and not pd.isna(prev) else None)


def _day_minute_indices(m: pd.DataFrame) -> dict[date, list[int]]:
    out: dict[date, list[int]] = defaultdict(list)
    for i, ts in enumerate(m.trade_time):
        out[pd.Timestamp(ts).date()].append(i)
    return dict(out)


def simulate(
    features: dict[str, dict[str, Any]],
    recs: pd.DataFrame,
    preopen: pd.DataFrame,
    sessions: list[date],
    cfg: Config,
) -> tuple[dict[str, Any], pd.DataFrame, pd.DataFrame]:
    """Replay one configuration; prices are qfq and costs use production rates."""
    eval_sessions = [d for d in sessions if cfg.start_date <= d <= cfg.end_date]
    cash = 1_000_000.0
    positions: dict[str, dict[str, Any]] = {}
    trades: list[dict[str, Any]] = []
    equity_rows: list[dict[str, Any]] = []
    rec_by_day: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for row in recs.itertuples():
        d = row.trade_date
        if d in eval_sessions:
            rec_by_day[d].append(row._asdict())
    pre_by_day: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for row in preopen.itertuples():
        if row.trade_date in eval_sessions:
            pre_by_day[row.trade_date].append(row._asdict())

    # Map every confirmed minute event to its next minute execution index.
    event_exec: dict[tuple[str, int], list[tuple[int, str, pd.Timestamp]]] = defaultdict(list)
    for symbol, f in features.items():
        m = f["minute"]
        idx = {pd.Timestamp(ts): i for i, ts in enumerate(m.trade_time)}
        for ts, kinds in f["minute_events"].items():
            i = idx.get(pd.Timestamp(ts))
            if i is None:
                continue
            # Events confirmed on the last bar of a day execute next session
            # open (the first available minute after the event).
            if i + 1 < len(m):
                exec_i = i + 1
            else:
                continue
            for kind in kinds:
                event_exec[(symbol, exec_i)].append((i, kind, pd.Timestamp(ts)))

    run_date = None
    open_buy_candidates: dict[date, list[dict[str, Any]]] = defaultdict(list)
    wait_candidates: dict[date, list[dict[str, Any]]] = defaultdict(list)
    today_recommended: dict[date, set[str]] = defaultdict(set)
    for day in eval_sessions:
        for rec in rec_by_day.get(day, []):
            symbol = rec["ts_code"]
            if symbol not in features:
                continue
            today_recommended[day].add(symbol)
            available = pd.Timestamp(rec["available_at"])
            dstate, age = _latest_daily_state(features[symbol]["daily_events"], day, sessions, cfg.daily_lookback)
            rec.update({"daily_buy_recent": dstate, "daily_buy_age": age})
            if (not cfg.recommendation_1m_only) and dstate and available.time() <= time(9, 30):
                open_buy_candidates[day].append(rec)
            if cfg.recommendation_1m_only or (not dstate) or available.time() > time(9, 30) or dstate:
                wait_candidates[day].append(rec)

    # Candidate keys prevent repeated refreshes and ensure one buy per name/day.
    pending_buys: dict[tuple[str, date], dict[str, Any]] = {}
    minute_by_symbol_day: dict[tuple[str, date], list[int]] = {}
    for symbol, f in features.items():
        for day, indices in _day_minute_indices(f["minute"]).items():
            minute_by_symbol_day[(symbol, day)] = indices

    def portfolio_equity(timestamp: pd.Timestamp) -> float:
        total = cash
        for symbol, pos in positions.items():
            m = features[symbol]["minute"]
            prior = m[m.trade_time <= timestamp]
            if not prior.empty:
                total += float(prior.iloc[-1].close) * pos["quantity"]
        return total

    def execute_buy(symbol: str, timestamp: pd.Timestamp, price: float, rec: dict[str, Any], reason: str) -> None:
        nonlocal cash
        if symbol in positions or price <= 0:
            return
        equity = portfolio_equity(timestamp)
        amount_cap = min(equity * cfg.max_stock_weight, cash)
        quantity = int(amount_cap / price // 100) * 100
        if quantity < 100:
            return
        amount = price * quantity
        fee = buy_fee(amount)
        if amount + fee > cash:
            quantity = int(max(0.0, cash - fee) / price // 100) * 100
            amount = price * quantity
            fee = buy_fee(amount)
        if quantity < 100 or amount + fee > cash:
            return
        cash -= amount + fee
        positions[symbol] = {"quantity": quantity, "buy_price": price, "buy_time": timestamp, "rec": rec}
        trades.append({"timestamp": timestamp, "symbol": symbol, "side": "BUY", "price": price, "quantity": quantity, "amount": amount, "fee": fee, "pnl": None, "reason": reason, "recommendation_id": rec.get("recommendation_id"), "daily_buy_recent": rec.get("daily_buy_recent"), "daily_buy_age": rec.get("daily_buy_age")})

    def execute_sell(symbol: str, timestamp: pd.Timestamp, price: float, reason: str) -> None:
        nonlocal cash
        pos = positions.get(symbol)
        if not pos or price <= 0:
            return
        amount = price * pos["quantity"]
        fee = sell_fee(amount)
        pnl = amount - fee - pos["buy_price"] * pos["quantity"]
        cash += amount - fee
        trades.append({"timestamp": timestamp, "symbol": symbol, "side": "SELL", "price": price, "quantity": pos["quantity"], "amount": amount, "fee": fee, "pnl": pnl, "reason": reason, "recommendation_id": pos["rec"].get("recommendation_id"), "daily_buy_recent": pos["rec"].get("daily_buy_recent"), "daily_buy_age": pos["rec"].get("daily_buy_age")})
        positions.pop(symbol, None)

    # Daily sell events are known only at a completed daily close. Schedule the
    # next available session's first minute; this automatically respects T+1.
    daily_sell_exec: dict[tuple[date, str], set[str]] = defaultdict(set)
    for symbol, f in features.items():
        for ts, kinds in f["daily_events"].items():
            if cfg.exit_only_without_today_recommendation or SELL not in kinds:
                continue
            d = pd.Timestamp(ts).date()
            next_days = [x for x in sessions if x > d]
            if next_days:
                daily_sell_exec[(next_days[0], symbol)].add(SELL)

    # Replay minute bars across all candidate symbols. This is event-driven but
    # deterministic: sells are processed before buys at the same timestamp.
    timeline: list[tuple[pd.Timestamp, str, int]] = []
    for symbol, f in features.items():
        for i, ts in enumerate(f["minute"].trade_time):
            d = pd.Timestamp(ts).date()
            if d in eval_sessions:
                timeline.append((pd.Timestamp(ts), symbol, i))
    timeline.sort(key=lambda x: (x[0], x[1]))
    global_first_ts: dict[date, pd.Timestamp] = {}
    for ts, _, _ in timeline:
        global_first_ts.setdefault(ts.date(), ts)
    open_processed_days: set[date] = set()
    for timestamp, symbol, i in timeline:
        day = timestamp.date()
        f = features[symbol]
        m = f["minute"]
        price = float(m.iloc[i].open)
        if not np.isfinite(price) or price <= 0:
            continue

        # Daily sell at next session open; T+1 is naturally satisfied because
        # a same-day buy cannot have been in positions before this timestamp.
        if (day, symbol) in daily_sell_exec and symbol in positions:
            execute_sell(symbol, timestamp, price, "DAILY_一卖")

        # Fresh 1m sell event executes on the next 1m open. If it fires on the
        # buy date, defer to the first minute of the next session for T+1.
        for confirm_i, kind, signal_ts in event_exec.get((symbol, i), []):
            if kind != SELL or symbol not in positions:
                continue
            if cfg.exit_only_without_today_recommendation and (symbol in today_recommended.get(day, set()) or positions[symbol]["buy_time"].date() == day):
                continue
            buy_day = positions[symbol]["buy_time"].date()
            if buy_day == day:
                next_days = [x for x in sessions if x > day]
                if next_days and day == timestamp.date():
                    # The next session-open daily event will be caught via a
                    # synthetic pending marker below.
                    positions[symbol]["deferred_1m_sell"] = True
                continue
            execute_sell(symbol, timestamp, price, "1M_一卖")

        # Deferred T+1 1m sell at next session's first minute.
        if symbol in positions and positions[symbol].get("deferred_1m_sell"):
            buy_day = positions[symbol]["buy_time"].date()
            if day > buy_day:
                execute_sell(symbol, timestamp, price, "1M_一卖_T+1")

        # At the global first minute, process all eligible open candidates in
        # AI rank order so symbol iteration order cannot affect cash allocation.
        if timestamp == global_first_ts.get(day) and day not in open_processed_days:
            open_processed_days.add(day)
            for rec in sorted(open_buy_candidates.get(day, []), key=lambda x: (x.get("rank", 9999), -float(x.get("ai_confidence") or 0), x["ts_code"])):
                sym = rec["ts_code"]
                op, prev = _first_open(features[sym]["daily"], day)
                if op is not None and prev is not None and op <= prev * (1 + cfg.gap_threshold):
                    execute_buy(sym, timestamp, op, rec, "AI今日推荐+近期日线一买开盘")

        # A new logical 1m buy must be confirmed after this recommendation.
        # It is allowed to execute only on the same recommendation day.
        for confirm_i, kind, signal_ts in event_exec.get((symbol, i), []):
            if kind != BUY:
                continue
            for rec in wait_candidates.get(day, []):
                if rec["ts_code"] != symbol or pd.Timestamp(rec["available_at"]) > signal_ts:
                    continue
                pending_buys[(symbol, day)] = rec
            rec = pending_buys.pop((symbol, day), None)
            if rec is not None:
                execute_buy(symbol, timestamp, price, rec, "AI今日推荐+推荐后新逻辑1M一买")

    # Mark open positions at the last available close. The report exposes both
    # realised and marked equity so a short observation window is not mistaken
    # for a fully closed portfolio.
    for symbol, pos in positions.items():
        m = features[symbol]["minute"]
        last = m.iloc[-1]
        pos["mark_price"] = float(last.close)
        pos["unrealized_pnl"] = (pos["mark_price"] - pos["buy_price"]) * pos["quantity"]
    marked_equity = cash + sum(p["mark_price"] * p["quantity"] for p in positions.values())
    sells = pd.DataFrame([x for x in trades if x["side"] == "SELL"])
    buys = pd.DataFrame([x for x in trades if x["side"] == "BUY"])
    summary = {
        "lookback_days": cfg.daily_lookback,
        "initial_cash": 1_000_000.0,
        "marked_equity": marked_equity,
        "return_pct": marked_equity / 1_000_000.0 - 1,
        "cash": cash,
        "open_positions": len(positions),
        "buy_count": len(buys),
        "sell_count": len(sells),
        "closed_win_rate": float((sells.pnl > 0).mean()) if not sells.empty else None,
        "realized_pnl": float(sells.pnl.sum()) if not sells.empty else 0.0,
        "fees": float(pd.DataFrame(trades).fee.sum()) if trades else 0.0,
        "recommendation_days": len(rec_by_day),
        "candidate_day_symbols": sum(len(x) for x in rec_by_day.values()),
        "positions_marked_unrealized_pnl": float(sum(p.get("unrealized_pnl", 0) for p in positions.values())),
    }
    return summary, pd.DataFrame(trades), pd.DataFrame([{"timestamp": cfg.end_date, "equity": marked_equity, "cash": cash, "open_positions": len(positions)}])


def main() -> None:
    args = parse_args()
    start, end = date.fromisoformat(args.start_date), date.fromisoformat(args.end_date)
    all_recs, preopen = load_recommendations(args.main_db, start, end)
    if all_recs.empty:
        raise SystemExit("没有可用的 SUCCESS AI 推荐")
    symbols = sorted(set(all_recs.ts_code.astype(str)))
    if args.symbol_limit:
        symbols = symbols[: args.symbol_limit]
        all_recs = all_recs[all_recs.ts_code.isin(symbols)].copy()
        preopen = preopen[preopen.ts_code.isin(symbols)].copy()
    print(f"AI推荐: {len(all_recs)} rows, {len(symbols)} symbols; preopen rows={len(preopen)}", flush=True)
    daily, minute, sessions = load_market_data(args.analytics_db, symbols, start, end)
    available_symbols = sorted(set(daily.ts_code) & set(minute.ts_code))
    all_recs = all_recs[all_recs.ts_code.isin(available_symbols)].copy()
    preopen = preopen[preopen.ts_code.isin(available_symbols)].copy()
    print(f"市场数据覆盖: {len(available_symbols)} symbols, {len(sessions)} sessions, minute={minute.trade_time.min()}~{minute.trade_time.max()}", flush=True)
    features = prepare_symbol_features(daily, minute, args.signal_engine)
    print(f"缠论特征完成: {len(features)} symbols", flush=True)

    out_dir = ROOT / args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    summaries: list[dict[str, Any]] = []
    for lookback in args.lookbacks:
        cfg = Config(args.analytics_db, args.main_db, start, end, lookback, recommendation_1m_only=args.recommendation_1m_only, exit_only_without_today_recommendation=args.exit_only_without_today_recommendation)
        summary, trades, equity = simulate(features, all_recs, preopen, sessions, cfg)
        summary.update({"start_date": start.isoformat(), "end_date": end.isoformat(), "minute_data_as_of": str(minute.trade_time.max()), "daily_data_as_of": str(daily.trade_date.max()), "recommendation_prompt_versions": sorted(all_recs.prompt_version.dropna().astype(str).unique())})
        summaries.append(summary)
        trades.to_csv(out_dir / f"trades_lookback_{lookback}d.csv", index=False)
        equity.to_csv(out_dir / f"equity_lookback_{lookback}d.csv", index=False)
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    pd.DataFrame(summaries).to_csv(out_dir / "summary.csv", index=False)
    (out_dir / "metadata.json").write_text(json.dumps({"config": vars(args), "sessions": [x.isoformat() for x in sessions], "recommendation_rows": len(all_recs), "symbols": len(available_symbols)}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(pd.DataFrame(summaries)[["lookback_days", "buy_count", "sell_count", "open_positions", "realized_pnl", "marked_equity", "return_pct", "closed_win_rate", "fees"]].to_string(index=False))


if __name__ == "__main__":
    main()

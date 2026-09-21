"""提示看板历史回放：用分钟线还原每个交易日盘中每一分钟的快照，逐分钟重跑信号。

与盘中扫描完全同一口径——判定用 evaluate_snapshot / classify，写库用 AlertScanner._write_hits
（首次命中时刻、强势>活跃>规避>观望 的当日覆盖规则、信号变更流水都一样），回放只负责还原快照：

- 某分钟 m 的"快照"：当天截至 m 的最后一根 1 分钟线收盘为现价，首根开盘为今开，
  截至 m 的累计量/累计额为当日累计，昨收取自日线。个股用 a_stock_minute_bar（未复权，
  与盘中 rt_k 同一价格口径），申万一/二级用 a_stock_sw_minute_bar。
- 结构与量能基准直接调盘中用的 build_structures / build_volume_baseline，传入回放日期即可，
  它们本来就只看该日期之前的数据，不会用到未来信息。

全部数据来自 DuckDB，不调 tushare。能回放多远取决于分钟库：某天要有当天的分钟线，
以及它之前 baseline_days 个交易日的分钟线（算同时段基准），不满足的日期会跳过并说明原因。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from ..database import MarketAlertEvent, MarketAlertHit, get_db_ctx
from .duckdb_analytics import connect_analytics_db, safe_float
from .market_alerts import (
    BASELINE_TRADING_DAYS,
    DEFAULT_THRESHOLDS,
    SPEED_LOOKBACK_ROUNDS,
    AlertScanner,
    AlertThresholds,
    baseline_at,
    build_structures,
    build_sw_structures,
    build_volume_baseline,
    evaluate_snapshot,
    in_scan_window,
)


logger = logging.getLogger(__name__)

MIN_STOCKS_WITH_MINUTES = 1000   # 当天分钟线覆盖的股票太少，说明那天没同步全，不回放


def replay_minutes(columns: List[str]) -> List[str]:
    """回放要评估的分钟：与盘中扫描时段一致（09:35~15:05，午休不扫），只取有分钟线的时刻。"""
    result = []
    for label in sorted(columns):
        hour, minute = int(label[:2]), int(label[3:5])
        if in_scan_window(datetime(2000, 1, 3, hour, minute)):
            result.append(label)
    return result


def build_minute_panels(frame: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """把当天的分钟线整理成按分钟取快照所需的宽表。

    frame 列：ts_code, t(HH:MM), open, close, vol, amount（已按 ts_code、时间排序）。
    返回 close（按分钟向后填充，停牌的分钟沿用上一价）、累计量、累计额、今开。
    """
    if frame is None or frame.empty:
        return None
    close = frame.pivot(index="ts_code", columns="t", values="close").sort_index(axis=1).ffill(axis=1)
    cum_vol = frame.pivot(index="ts_code", columns="t", values="vol").sort_index(axis=1).fillna(0).cumsum(axis=1)
    cum_amount = frame.pivot(index="ts_code", columns="t", values="amount").sort_index(axis=1).fillna(0).cumsum(axis=1)
    day_open = frame.groupby("ts_code", sort=False)["open"].first()
    return {"close": close, "cum_vol": cum_vol, "cum_amount": cum_amount, "open": day_open}


def snapshot_at(panels: Dict[str, Any], pre_close: Dict[str, float], minute: str) -> pd.DataFrame:
    """还原「截至某分钟」的全市场快照，列与盘中 rt_k 快照一致：ts_code, close, pre_close, open, vol, amount。

    这一分钟全市场都没有分钟线时（宽表里没有这一列），退到不晚于它的最近一列，
    与 baseline_at 的处理一致；早于第一根则返回空。
    """
    columns = [column for column in panels["close"].columns if column <= minute]
    if not columns:
        return pd.DataFrame(columns=["ts_code", "close", "pre_close", "open", "vol", "amount"])
    minute = columns[-1]
    close = panels["close"][minute]
    quotes = pd.DataFrame({
        "ts_code": close.index,
        "close": close.values,
        "pre_close": [pre_close.get(code) for code in close.index],
        "open": panels["open"].reindex(close.index).values,
        "vol": panels["cum_vol"][minute].reindex(close.index).values,
        "amount": panels["cum_amount"][minute].reindex(close.index).values,
    })
    # 当天还没开出第一笔的（停牌/晚开）此刻没有价格，和盘中快照里查不到一样，直接不参与
    return quotes.dropna(subset=["close", "pre_close"])


def _pct_map(quotes: pd.DataFrame) -> Dict[str, float]:
    result: Dict[str, float] = {}
    for row in quotes.itertuples(index=False):
        price, pre_close = safe_float(row.close), safe_float(row.pre_close)
        if price and pre_close:
            result[str(row.ts_code)] = (price / pre_close - 1) * 100
    return result


def _load_day_minutes(connection, table: str, day: date) -> pd.DataFrame:
    return connection.execute(
        f"""
        SELECT ts_code, strftime(trade_time, '%H:%M') AS t, open, close, vol, amount
        FROM {table}
        WHERE trade_time >= ? AND trade_time < ?
        ORDER BY ts_code, trade_time
        """,
        [datetime.combine(day, datetime.min.time()), datetime.combine(day + timedelta(days=1), datetime.min.time())],
    ).fetchdf()


def _stock_pre_close(connection, day: date) -> Dict[str, float]:
    rows = connection.execute(
        "SELECT ts_code, pre_close FROM a_stock_market_daily WHERE trade_date = ?", [day]
    ).fetchall()
    return {str(code): float(value) for code, value in rows if value}


def _sw_pre_close(connection, day: date) -> Dict[str, float]:
    """申万日线没有昨收字段，取该日之前最后一个交易日的收盘。"""
    rows = connection.execute(
        """
        SELECT ts_code, close FROM (
            SELECT ts_code, close,
                   row_number() OVER (PARTITION BY ts_code ORDER BY trade_date DESC) AS rn
            FROM a_stock_sw_daily WHERE trade_date < ?
        ) WHERE rn = 1
        """,
        [day],
    ).fetchall()
    return {str(code): float(value) for code, value in rows if value}


def _baseline_days_available(connection, table: str, day: date, days: int) -> int:
    return int(connection.execute(
        f"""
        SELECT count(*) FROM (
            SELECT DISTINCT CAST(trade_time AS DATE) AS d FROM {table}
            WHERE CAST(trade_time AS DATE) < ? ORDER BY d DESC LIMIT ?
        )
        """,
        [day, days],
    ).fetchone()[0] or 0)


def replay_day(
    connection,
    day: date,
    *,
    baseline_days: int = BASELINE_TRADING_DAYS,
    thresholds: AlertThresholds = DEFAULT_THRESHOLDS,
    overwrite: bool = True,
) -> Dict[str, Any]:
    """回放单个交易日。返回 {date, status, ...}，status 为 done / skipped。"""
    label = day.isoformat()
    with get_db_ctx() as db:
        existing = db.query(MarketAlertHit).filter(MarketAlertHit.trade_date == day).count()
    if existing and not overwrite:
        return {"date": label, "status": "skipped", "reason": f"已有 {existing} 条记录，未勾选覆盖"}

    stock_minutes = _load_day_minutes(connection, "a_stock_minute_bar", day)
    covered = stock_minutes["ts_code"].nunique() if not stock_minutes.empty else 0
    if covered < MIN_STOCKS_WITH_MINUTES:
        return {"date": label, "status": "skipped", "reason": f"当天分钟线只覆盖 {covered} 只股票"}
    available = _baseline_days_available(connection, "a_stock_minute_bar", day, baseline_days)
    if available < baseline_days:
        return {
            "date": label, "status": "skipped",
            "reason": f"量能基准不足：之前只有 {available} 个交易日的分钟线，需要 {baseline_days} 个",
        }

    # 与盘中完全相同的结构与基准，只是"今天"换成回放日
    structures = build_structures(connection, day)
    baseline = build_volume_baseline(connection, day, days=baseline_days)
    stock_panels = build_minute_panels(stock_minutes)
    stock_pre = _stock_pre_close(connection, day)

    sw_structures = build_sw_structures(connection, day)
    sw_minutes = _load_day_minutes(connection, "a_stock_sw_minute_bar", day)
    # 申万一/二级缺数据时只关掉行业信号、个股照常回放，但要把原因带出去——
    # 否则任务显示"完成"，页面上行业信号却是空的，看不出是缺了分钟线
    sw_history = _baseline_days_available(connection, "a_stock_sw_minute_bar", day, baseline_days)
    if not sw_structures:
        sw_reason = "没有申万一/二级日线"
    elif sw_minutes.empty:
        sw_reason = "当天没有申万分钟线"
    elif sw_history < baseline_days:
        sw_reason = f"申万分钟线量能基准不足（之前只有 {sw_history} 天，需要 {baseline_days} 天）"
    else:
        sw_reason = ""
    sw_ready = not sw_reason
    sw_panels = build_minute_panels(sw_minutes) if sw_ready else None
    sw_baseline = build_volume_baseline(connection, day, days=baseline_days, table="a_stock_sw_minute_bar") if sw_ready else None
    sw_pre = _sw_pre_close(connection, day) if sw_ready else {}

    if overwrite and existing:
        with get_db_ctx() as db:
            db.query(MarketAlertEvent).filter(MarketAlertEvent.trade_date == day).delete(synchronize_session=False)
            db.query(MarketAlertHit).filter(MarketAlertHit.trade_date == day).delete(synchronize_session=False)

    # 每个回放日用一个全新的扫描器实例，保证当日覆盖状态从零开始、不串到别的日期
    writer = AlertScanner()
    minutes = replay_minutes(list(stock_panels["close"].columns))
    pct_history: List[Dict[str, float]] = []
    total_hits = total_sw_hits = 0
    for minute in minutes:
        quotes = snapshot_at(stock_panels, stock_pre, minute)
        previous = pct_history[0] if len(pct_history) >= SPEED_LOOKBACK_ROUNDS else None
        hits = evaluate_snapshot(quotes, structures, baseline_at(baseline, minute), minute, previous, thresholds)
        current_pct = _pct_map(quotes)

        sw_hits: List[Dict[str, Any]] = []
        if sw_panels is not None and minute in sw_panels["close"].columns:
            sw_quotes = snapshot_at(sw_panels, sw_pre, minute)
            sw_quotes = sw_quotes[sw_quotes["ts_code"].isin(sw_structures)]
            sw_hits = evaluate_snapshot(
                sw_quotes, sw_structures, baseline_at(sw_baseline, minute), minute, previous, thresholds,
            )
            current_pct.update(_pct_map(sw_quotes))

        pct_history.append(current_pct)
        if len(pct_history) > SPEED_LOOKBACK_ROUNDS:
            pct_history.pop(0)
        if hits or sw_hits:
            writer._write_hits(day, minute, hits + sw_hits)
        total_hits += len(hits)
        total_sw_hits += len(sw_hits)

    with get_db_ctx() as db:
        rows = db.query(MarketAlertHit.entity_type, MarketAlertHit.label).filter(MarketAlertHit.trade_date == day).all()
        events = db.query(MarketAlertEvent).filter(MarketAlertEvent.trade_date == day).count()
    by_label: Dict[str, int] = {}
    for entity_type, value in rows:
        key = f"{'个股' if (entity_type or 'stock') == 'stock' else '行业'}{value}"
        by_label[key] = by_label.get(key, 0) + 1
    return {
        "date": label,
        "status": "done",
        "minutes": len(minutes),
        "stocks": int(covered),
        "sw": "on" if sw_ready else "off",
        "sw_reason": sw_reason,
        "records": len(rows),
        "events": events,
        "by_label": by_label,
    }


def replay_alerts(
    start: date,
    end: date,
    *,
    baseline_days: int = BASELINE_TRADING_DAYS,
    thresholds: AlertThresholds = DEFAULT_THRESHOLDS,
    overwrite: bool = True,
    today: Optional[date] = None,
    progress=None,
) -> Dict[str, Any]:
    """回放 [start, end] 之间的交易日（不含今天，今天由盘中扫描负责）。"""
    today = today or date.today()
    if start > end:
        raise ValueError("开始日期不能晚于结束日期")
    connection = connect_analytics_db()
    try:
        trade_days = [
            row[0] for row in connection.execute(
                "SELECT DISTINCT trade_date FROM a_stock_market_daily "
                "WHERE trade_date >= ? AND trade_date <= ? AND trade_date < ? ORDER BY trade_date",
                [start, end, today],
            ).fetchall()
        ]
        results: List[Dict[str, Any]] = []
        for index, day in enumerate(trade_days, start=1):
            try:
                result = replay_day(connection, day, baseline_days=baseline_days,
                                    thresholds=thresholds, overwrite=overwrite)
            except Exception as exc:  # noqa: BLE001  单日失败不影响其它日期
                logger.exception("提示看板回放 %s 失败", day)
                result = {"date": day.isoformat(), "status": "failed", "reason": str(exc)}
            results.append(result)
            if progress:
                progress(index, len(trade_days), result)
    finally:
        connection.close()
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "trade_days": len(trade_days),
        "done": sum(1 for item in results if item["status"] == "done"),
        "skipped": sum(1 for item in results if item["status"] == "skipped"),
        "failed": sum(1 for item in results if item["status"] == "failed"),
        "days": results,
    }

"""净利润断层：财报净利高增 + 公告后首个交易日跳空高开、收阳且未封板。

口径（全 A、只看每只股票最近一期财报）：

- 最近财报 = 报告期最新的一期；同一报告期有多次公告（更正/重述）时取首次公告日和首次公告的数据，
  那才是"市场第一次看到这份财报"的时点。
- 净利增速 = tushare fina_indicator.netprofit_yoy（归母净利润同比，%），要求 30% ≤ 增速 ≤ 3000%，
  上限用来剔除上年同期基数过小造成的虚高增速。
- T = 公告日，T+1 = 公告日之后的第一个交易日（公告多在盘后/非交易日发布）。
- T+1 同时满足：上市满 120 个交易日、成交额 ≥ 3000 万、开盘价较前收高开 ≥ 2%、
  收盘 > 开盘（收阳）、收盘未封涨停（以 tushare stk_limit 的涨停价为准）。
- 信号按 T+1 收盘价买入；至今涨跌幅按前复权口径算到分析库最新一天的收盘价。
- 估值列取分析库最新一天：PE(TTM)、PB、PS(TTM) 来自 tushare daily_basic；扣非 ROE(TTM) =
  最新一期扣非净利润滚动 TTM ÷ 该期末归母净资产；ROE/PB = 扣非 ROE(%) ÷ PB。

数据全部来自 DuckDB 分析库（财报、日K、复权因子、交易日），由每晚 A股基础数据同步写入；
信号任务排在同步之后跑（默认 18:25，定时任务串行排队，同步没跑完会等它）。
只有涨停价分析库里没有，用 tushare stk_limit 按信号日取。
"""
from __future__ import annotations

import bisect
import logging
import threading
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from ..database import MarketSignalSnapshot, get_db_ctx
from .duckdb_analytics import connect_analytics_db, safe_float


logger = logging.getLogger(__name__)

SIGNAL_KEY = "earnings_gap"

MIN_PROFIT_YOY = 30.0
MAX_PROFIT_YOY = 3000.0
MIN_GAP_PCT = 2.0
MIN_AMOUNT_YUAN = 3e7
MIN_LISTED_TRADE_DAYS = 120

# 只在这段时间内找"最近一期财报"，更早的说明公司已长期未披露，不再参与
REPORT_LOOKBACK_DAYS = 420
CALENDAR_LOOKBACK_DAYS = 3 * 366
# 扣非 ROE(TTM) 需要本期、上年年报、上年同期三期数据
ROE_LOOKBACK_DAYS = 2 * 366

CRITERIA = {
    "min_profit_yoy": MIN_PROFIT_YOY,
    "max_profit_yoy": MAX_PROFIT_YOY,
    "min_gap_pct": MIN_GAP_PCT,
    "min_amount_yuan": MIN_AMOUNT_YUAN,
    "min_listed_trade_days": MIN_LISTED_TRADE_DAYS,
}

_refresh_lock = threading.Lock()
# 历史交易日的涨停价不会变，进程内缓存避免每次重算都重复请求
_stk_limit_cache: Dict[date, Dict[str, float]] = {}


class EarningsGapDataError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 纯函数（便于单测）
# ---------------------------------------------------------------------------


def pick_latest_reports(frame: pd.DataFrame) -> pd.DataFrame:
    """每只股票取报告期最新的一期；同一报告期多次公告时取首次公告那一行（整行，不跨行拼字段）。"""
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["ts_code", "end_date", "ann_date", "netprofit_yoy"])
    data = frame.dropna(subset=["ts_code", "end_date", "ann_date"]).copy()
    data["ts_code"] = data["ts_code"].astype(str).str.strip().str.upper()
    data = data.sort_values(["ts_code", "end_date", "ann_date"], ascending=[True, False, True])
    return data.drop_duplicates(subset=["ts_code"], keep="first").reset_index(drop=True)


def next_trade_date(calendar: Sequence[date], value: date) -> Optional[date]:
    """严格晚于 value 的第一个交易日。"""
    index = bisect.bisect_right(calendar, value)
    return calendar[index] if index < len(calendar) else None


def listed_trade_days(calendar: Sequence[date], list_date: Optional[date], as_of: date) -> int:
    """上市日（含）到 as_of（含）之间的交易日数；上市早于日历起点时视为足够长。"""
    if list_date is None:
        return 0
    if list_date < calendar[0]:
        return len(calendar) + 10_000
    start = bisect.bisect_left(calendar, list_date)
    end = bisect.bisect_right(calendar, as_of)
    return max(0, end - start)


def fallback_up_limit(ts_code: str, pre_close: float) -> float:
    """stk_limit 取不到时按板块规则估算涨停价（不区分 ST）。"""
    code = ts_code.split(".")[0]
    if ts_code.endswith(".BJ"):
        ratio = 0.30
    elif code.startswith(("300", "301", "688", "689")):
        ratio = 0.20
    else:
        ratio = 0.10
    return round(pre_close * (1 + ratio) + 1e-9, 2)


def ttm_from_cumulative(values: Dict[date, float], latest: date) -> Optional[float]:
    """报告期累计值滚成 TTM：年报直接用；否则 本期累计 + 上年年报 − 上年同期累计。缺任一期返回 None。"""
    current = values.get(latest)
    if current is None:
        return None
    if latest.month == 12:
        return current
    last_annual = values.get(date(latest.year - 1, 12, 31))
    try:
        last_same = values.get(latest.replace(year=latest.year - 1))
    except ValueError:
        last_same = None
    if last_annual is None or last_same is None:
        return None
    return current + last_annual - last_same


def dedt_roe_ttm(profit_dedt: Dict[date, float], parent_equity: Dict[date, float]) -> Optional[float]:
    """扣非 ROE(TTM, %) = 最新一期滚动 TTM 扣非净利 ÷ 该期末归母净资产；净资产 ≤ 0 时不算。"""
    periods = sorted(end for end in profit_dedt if end in parent_equity)
    if not periods:
        return None
    latest = periods[-1]
    ttm = ttm_from_cumulative(profit_dedt, latest)
    equity = parent_equity.get(latest)
    if ttm is None or equity is None or equity <= 0:
        return None
    return ttm / equity * 100


def evaluate_t1_bar(bar: Dict[str, Any], up_limit: Optional[float]) -> Dict[str, Any]:
    """按 T+1 日线判断跳空高开、收阳、未封板、成交额；返回各项指标和是否满足。"""
    open_price = safe_float(bar.get("open"))
    close = safe_float(bar.get("close"))
    pre_close = safe_float(bar.get("pre_close"))
    amount_yuan = safe_float(bar.get("amount_yuan"))
    if not all(value is not None and value > 0 for value in (open_price, close, pre_close)):
        return {"passed": False}
    gap_pct = (open_price / pre_close - 1) * 100
    pct_chg = (close / pre_close - 1) * 100
    sealed = up_limit is not None and round(close, 2) >= round(up_limit, 2)
    passed = (
        gap_pct >= MIN_GAP_PCT - 1e-9
        and close > open_price
        and not sealed
        and amount_yuan is not None
        and amount_yuan >= MIN_AMOUNT_YUAN
    )
    return {
        "passed": passed,
        "gap_pct": gap_pct,
        "pct_chg": pct_chg,
        "sealed": sealed,
        "amount_yuan": amount_yuan,
    }


def _to_date(value: Any) -> Optional[date]:
    if value is None or value is pd.NaT or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        parsed = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(parsed) else parsed.date()


def _round(value: Any, digits: int = 2) -> Optional[float]:
    return safe_float(value, digits)


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------


def _load_trade_calendar(connection, since: date) -> List[date]:
    """分析库里有日K的交易日，即已同步完成的交易日历。"""
    frame = connection.execute(
        "SELECT DISTINCT trade_date FROM a_stock_market_daily WHERE trade_date >= ? ORDER BY trade_date",
        [since],
    ).fetchdf()
    return [value for value in (_to_date(item) for item in frame["trade_date"]) if value]


def _load_reports(connection, since: date) -> pd.DataFrame:
    frame = connection.execute(
        """
        SELECT ts_code, end_date, ann_date, netprofit_yoy
        FROM a_stock_fina_indicator
        WHERE end_date >= ? AND ann_date IS NOT NULL
        """,
        [since],
    ).fetchdf()
    if frame.empty:
        raise EarningsGapDataError("分析库没有近期财务指标数据")
    for column in ("end_date", "ann_date"):
        frame[column] = frame[column].map(_to_date)
    frame["netprofit_yoy"] = pd.to_numeric(frame["netprofit_yoy"], errors="coerce")
    return pick_latest_reports(frame)


def _load_basic(connection) -> pd.DataFrame:
    frame = connection.execute(
        """
        SELECT ts_code, name, industry, list_date
        FROM a_stock_basic
        WHERE list_status = 'L'
        """
    ).fetchdf()
    frame["ts_code"] = frame["ts_code"].astype(str).str.strip().str.upper()
    frame["list_date"] = frame["list_date"].map(_to_date)
    return frame


def _db_bars(connection, pairs: pd.DataFrame) -> Dict[Tuple[str, date], Dict[str, Any]]:
    """按 (ts_code, trade_date) 读分析库原始日线；amount 单位千元换算成元。"""
    if pairs.empty:
        return {}
    connection.register("earnings_gap_pairs", pairs[["ts_code", "trade_date"]])
    try:
        frame = connection.execute(
            """
            SELECT m.ts_code, m.trade_date, m.open, m.high, m.close, m.pre_close, m.amount
            FROM a_stock_market_daily m
            JOIN earnings_gap_pairs p ON p.ts_code = m.ts_code AND p.trade_date = m.trade_date
            """
        ).fetchdf()
    finally:
        connection.unregister("earnings_gap_pairs")
    bars = {}
    for row in frame.itertuples(index=False):
        bars[(row.ts_code, _to_date(row.trade_date))] = {
            "open": row.open,
            "high": row.high,
            "close": row.close,
            "pre_close": row.pre_close,
            "amount_yuan": None if pd.isna(row.amount) else float(row.amount) * 1000,
        }
    return bars


def _up_limits(service, trade_date: date, warnings: List[str]) -> Dict[str, float]:
    cached = _stk_limit_cache.get(trade_date)
    if cached is not None:
        return cached
    frame = service.get_a_stock_stk_limit_frame(trade_date)
    if frame is None or frame.empty:
        warnings.append(f"{trade_date.isoformat()} 涨停价(stk_limit)获取失败，按板块规则估算")
        return {}
    limits = {str(row.ts_code).upper(): float(row.up_limit) for row in frame.itertuples(index=False)}
    _stk_limit_cache[trade_date] = limits
    return limits


def _latest_db_prices(connection, codes: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """每只股票分析库里最新一根日线的收盘价和复权因子。"""
    if not codes:
        return {}
    placeholders = ",".join("?" for _ in codes)
    frame = connection.execute(
        f"""
        WITH latest AS (
            SELECT ts_code, MAX(trade_date) AS trade_date
            FROM a_stock_market_daily
            WHERE ts_code IN ({placeholders})
            GROUP BY ts_code
        )
        SELECT m.ts_code, m.trade_date, m.close, m.pe_ttm, m.pb, m.ps_ttm, f.adj_factor
        FROM a_stock_market_daily m
        JOIN latest l ON l.ts_code = m.ts_code AND l.trade_date = m.trade_date
        LEFT JOIN a_stock_adj_factor f ON f.ts_code = m.ts_code AND f.trade_date = m.trade_date
        """,
        list(codes),
    ).fetchdf()
    return {
        row.ts_code: {
            "trade_date": _to_date(row.trade_date),
            "close": safe_float(row.close),
            "adj_factor": safe_float(row.adj_factor),
            "pe_ttm": safe_float(row.pe_ttm),
            "pb": safe_float(row.pb),
            "ps_ttm": safe_float(row.ps_ttm),
        }
        for row in frame.itertuples(index=False)
    }


def _dedt_roe_by_code(connection, codes: Sequence[str], since: date) -> Dict[str, Optional[float]]:
    """扣非 ROE(TTM)。同一报告期有更正/重述时取最新公告的数据；净资产取合并报表(report_type=1)。"""
    if not codes:
        return {}
    placeholders = ",".join("?" for _ in codes)
    profit = connection.execute(
        f"""
        SELECT ts_code, end_date, profit_dedt
        FROM (
            SELECT ts_code, end_date, profit_dedt,
                   ROW_NUMBER() OVER (PARTITION BY ts_code, end_date ORDER BY ann_date DESC) AS rn
            FROM a_stock_fina_indicator
            WHERE ts_code IN ({placeholders}) AND end_date >= ? AND profit_dedt IS NOT NULL
        )
        WHERE rn = 1
        """,
        [*codes, since],
    ).fetchdf()
    equity = connection.execute(
        f"""
        SELECT ts_code, end_date, total_hldr_eqy_exc_min_int
        FROM (
            SELECT ts_code, end_date, total_hldr_eqy_exc_min_int,
                   ROW_NUMBER() OVER (PARTITION BY ts_code, end_date ORDER BY ann_date DESC) AS rn
            FROM a_stock_balancesheet
            WHERE ts_code IN ({placeholders}) AND end_date >= ? AND report_type = '1'
              AND total_hldr_eqy_exc_min_int IS NOT NULL
        )
        WHERE rn = 1
        """,
        [*codes, since],
    ).fetchdf()
    profit_map: Dict[str, Dict[date, float]] = {}
    for row in profit.itertuples(index=False):
        profit_map.setdefault(row.ts_code, {})[_to_date(row.end_date)] = float(row.profit_dedt)
    equity_map: Dict[str, Dict[date, float]] = {}
    for row in equity.itertuples(index=False):
        equity_map.setdefault(row.ts_code, {})[_to_date(row.end_date)] = float(row.total_hldr_eqy_exc_min_int)
    return {code: dedt_roe_ttm(profit_map.get(code, {}), equity_map.get(code, {})) for code in codes}


def _adj_factors(connection, pairs: Sequence[Tuple[str, date]]) -> Dict[Tuple[str, date], float]:
    if not pairs:
        return {}
    frame = pd.DataFrame(pairs, columns=["ts_code", "trade_date"])
    connection.register("earnings_gap_adj_pairs", frame)
    try:
        result = connection.execute(
            """
            SELECT f.ts_code, f.trade_date, f.adj_factor
            FROM a_stock_adj_factor f
            JOIN earnings_gap_adj_pairs p ON p.ts_code = f.ts_code AND p.trade_date = f.trade_date
            """
        ).fetchdf()
    finally:
        connection.unregister("earnings_gap_adj_pairs")
    return {
        (row.ts_code, _to_date(row.trade_date)): float(row.adj_factor)
        for row in result.itertuples(index=False)
        if safe_float(row.adj_factor)
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def compute_earnings_gap(now: Optional[datetime] = None, service=None, connection=None) -> Dict[str, Any]:
    from .tushare import TushareService

    now = now or datetime.now()
    today = now.date()
    service = service or TushareService.get_instance()
    own_connection = connection is None
    connection = connection or connect_analytics_db()
    warnings: List[str] = []
    try:
        calendar = _load_trade_calendar(connection, today - timedelta(days=CALENDAR_LOOKBACK_DAYS))
        if not calendar:
            raise EarningsGapDataError("分析库没有日K数据")
        # 分析库最新一个交易日；公告日在它当天或之后的，T+1 还没到（或还没同步），不参与
        last_trade_date = calendar[-1]

        reports = _load_reports(connection, today - timedelta(days=REPORT_LOOKBACK_DAYS))
        basic = _load_basic(connection)
        data = reports.merge(basic, on="ts_code", how="inner")
        stats = {"reports": int(len(data))}

        data = data[(data["netprofit_yoy"] >= MIN_PROFIT_YOY) & (data["netprofit_yoy"] <= MAX_PROFIT_YOY)].copy()
        stats["growth_passed"] = int(len(data))

        data["t1_date"] = data["ann_date"].map(lambda value: next_trade_date(calendar, value))
        ready = data["t1_date"].notna()
        stats["pending"] = int((~ready).sum())
        data = data[ready].copy()

        data["listed_days"] = [
            listed_trade_days(calendar, list_date, t1)
            for list_date, t1 in zip(data["list_date"], data["t1_date"])
        ]
        data = data[data["listed_days"] >= MIN_LISTED_TRADE_DAYS].copy()
        stats["listed_passed"] = int(len(data))

        pairs = pd.DataFrame({"ts_code": data["ts_code"], "trade_date": data["t1_date"]})
        bars = _db_bars(connection, pairs)

        signals = []
        for row in data.itertuples(index=False):
            bar = bars.get((row.ts_code, row.t1_date))
            if not bar or not evaluate_t1_bar(bar, None)["passed"]:
                continue
            limits = _up_limits(service, row.t1_date, warnings)
            up_limit = limits.get(row.ts_code) or fallback_up_limit(row.ts_code, float(bar["pre_close"]))
            result = evaluate_t1_bar(bar, up_limit)
            if result["passed"]:
                signals.append((row, bar, result, up_limit))
        stats["signals"] = len(signals)

        items = _build_items(connection, signals, calendar)
        return {
            "computed_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "trade_date": last_trade_date.isoformat(),
            "criteria": CRITERIA,
            "stats": stats,
            "warnings": list(dict.fromkeys(warnings)),
            "items": items,
        }
    finally:
        if own_connection:
            connection.close()


def _build_items(connection, signals, calendar) -> List[Dict[str, Any]]:
    if not signals:
        return []
    codes = sorted({row.ts_code for row, _, _, _ in signals})
    latest_db = _latest_db_prices(connection, codes)
    adj = _adj_factors(connection, [(row.ts_code, row.t1_date) for row, _, _, _ in signals])
    roe = _dedt_roe_by_code(connection, codes, calendar[-1] - timedelta(days=ROE_LOOKBACK_DAYS))

    items = []
    for row, bar, result, up_limit in signals:
        code = row.ts_code
        t1 = row.t1_date
        t1_close = float(bar["close"])
        latest = latest_db.get(code) or {}
        latest_date = latest.get("trade_date")
        latest_close = latest.get("close")
        adj_last = latest.get("adj_factor")
        adj_t1 = adj.get((code, t1)) or adj_last

        since_pct = None
        if latest_date is not None and latest_close and adj_last and adj_t1:
            since_pct = (latest_close * adj_last / (t1_close * adj_t1) - 1) * 100
        pb = latest.get("pb")
        roe_ttm = roe.get(code)
        holding_days = None
        if latest_date is not None:
            holding_days = max(0, bisect.bisect_right(calendar, latest_date) - bisect.bisect_right(calendar, t1))

        items.append({
            "symbol": code,
            "name": row.name,
            "industry": row.industry,
            "end_date": row.end_date.isoformat(),
            "ann_date": row.ann_date.isoformat(),
            "netprofit_yoy": _round(row.netprofit_yoy),
            "signal_date": t1.isoformat(),
            "t1_open_gap_pct": _round(result["gap_pct"]),
            "t1_pct_chg": _round(result["pct_chg"]),
            "t1_close": _round(t1_close, 3),
            "t1_up_limit": _round(up_limit, 3),
            "t1_amount_yi": _round(result["amount_yuan"] / 1e8, 2),
            "listed_trade_days": int(row.listed_days),
            "latest_price": _round(latest_close, 3),
            "latest_date": latest_date.isoformat() if latest_date else None,
            "since_pct": _round(since_pct),
            "holding_days": holding_days,
            "pe_ttm": _round(latest.get("pe_ttm")),
            "pb": _round(pb),
            "ps_ttm": _round(latest.get("ps_ttm")),
            "roe_dedt_ttm": _round(roe_ttm),
            "roe_pb": _round(roe_ttm / pb) if roe_ttm is not None and pb and pb > 0 else None,
        })
    items.sort(key=lambda item: (item["signal_date"], item["netprofit_yoy"] or 0), reverse=True)
    return items


# ---------------------------------------------------------------------------
# 快照读写
# ---------------------------------------------------------------------------


def save_earnings_gap_snapshot(payload: Dict[str, Any]) -> None:
    with get_db_ctx() as db:
        db.merge(MarketSignalSnapshot(
            signal_key=SIGNAL_KEY,
            trade_date=date.fromisoformat(payload["trade_date"]),
            payload=payload,
            computed_at=datetime.strptime(payload["computed_at"], "%Y-%m-%d %H:%M:%S"),
        ))


def load_earnings_gap_snapshot() -> Optional[Dict[str, Any]]:
    with get_db_ctx() as db:
        snapshot = db.get(MarketSignalSnapshot, SIGNAL_KEY)
        return dict(snapshot.payload) if snapshot and snapshot.payload else None


def refresh_earnings_gap(now: Optional[datetime] = None) -> Dict[str, Any]:
    """重新计算并保存快照；同一时间只允许一个计算在跑。"""
    with _refresh_lock:
        payload = compute_earnings_gap(now=now)
        save_earnings_gap_snapshot(payload)
        return payload


def get_earnings_gap(refresh: bool = False) -> Dict[str, Any]:
    if not refresh:
        snapshot = load_earnings_gap_snapshot()
        if snapshot:
            return snapshot
    return refresh_earnings_gap()

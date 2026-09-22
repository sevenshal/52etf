"""净利润断层：业绩公告（财报/快报/预告）净利高增 + 公告后首个交易日跳空高开、收阳且未封板。

口径（全 A、每只股票看最新一个报告期的业绩公告）：

- 事件源三选多（页面可配）：
  - 财报 = tushare fina_indicator，净利同比 netprofit_yoy、营收同比 or_yoy、
    单季净利环比 q_netprofit_qoq、单季营收环比 q_sales_qoq
  - 快报 = tushare express，净利同比按 净利润 ÷ 去年同期修正后净利润 − 1 算，营收同比取 yoy_sales
  - 预告 = tushare forecast，只有净利变动幅度区间，净利同比取**下限** p_change_min（最保守）
  每只股票取最新一个报告期，该期的预告/快报/财报都作为候选事件（先发的不被后发的覆盖）；
  同一报告期多个事件源都触发断层时只保留最早那条（通常是预告，断层反应的是第一次出现的新消息）；
  同一天多个事件源公告时只留一条，优先级 财报 > 快报 > 预告（数据最全的优先）。
- T = 公告日，T+1 = 公告日之后的第一个交易日（公告多在盘后/非交易日发布）。
- T+1 的硬条件（阈值都在页面上可改，见 DEFAULT_CONFIG）：上市满 N 个交易日、成交额 ≥ 下限、
  开盘较前收高开 ≥ 下限、收盘 > 开盘（收阳）、收盘未封涨停（tushare stk_limit 的涨停价）、
  可选：成交额 ≥ N 日均额的若干倍、必须留真缺口（T+1 最低价 > T 日最高价）。
- 信号按 T+1 收盘价买入；至今涨跌幅、最大涨幅按前复权口径算到分析库最新一天。
- 缺口回补 = T+1 之后任意一天的最低价回落到 T 日最高价（换算到 T+1 价格口径）之下。
- 估值列取分析库最新一天：PE(TTM)、PB、PS(TTM) 来自 tushare daily_basic；扣非 ROE(TTM) =
  最新一期扣非净利润滚动 TTM ÷ 该期末归母净资产；ROE/PB = 扣非 ROE(%) ÷ PB。

数据全部来自 DuckDB 分析库（财报/快报/预告、日K、复权因子、交易日），由每晚 A股基础数据同步写入；
信号任务排在同步之后跑（默认 18:25，定时任务串行排队，同步没跑完会等它）。
只有涨停价分析库里没有，用 tushare stk_limit 按信号日取。
"""
from __future__ import annotations

import bisect
import logging
import math
import threading
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from ..database import MarketSignalConfig, MarketSignalSnapshot, get_db_ctx
from .duckdb_analytics import connect_analytics_db, safe_float


logger = logging.getLogger(__name__)

SIGNAL_KEY = "earnings_gap"
# 快照结构版本：新增/改动 items 里的字段时 +1，旧版本快照会被自动重算，
# 否则页面会一直显示上一版代码算出来的老快照（新列全是空）
PAYLOAD_VERSION = 5

SOURCE_REPORT = "report"
SOURCE_EXPRESS = "express"
SOURCE_FORECAST = "forecast"
SOURCE_LABELS = {SOURCE_REPORT: "财报", SOURCE_EXPRESS: "快报", SOURCE_FORECAST: "预告"}
# 公告日相同时谁说了算：财报数据最全，预告只有区间
SOURCE_PRIORITY = {SOURCE_REPORT: 0, SOURCE_EXPRESS: 1, SOURCE_FORECAST: 2}

DEFAULT_CONFIG: Dict[str, Any] = {
    "sources": [SOURCE_REPORT, SOURCE_EXPRESS, SOURCE_FORECAST],
    "min_profit_yoy": 30.0,
    "max_profit_yoy": 3000.0,
    "min_gap_pct": 2.0,
    "min_amount_yuan": 3e7,
    "min_listed_trade_days": 120,
    "require_bullish_close": True,
    "require_unsealed": True,
    "require_true_gap": False,
    "min_amount_ratio": 0.0,
    "amount_ratio_days": 20,
}
CONFIG_NUMERIC_BOUNDS = {
    "min_profit_yoy": (-1000.0, 10000.0),
    "max_profit_yoy": (0.0, 1e6),
    "min_gap_pct": (0.0, 100.0),
    "min_amount_yuan": (0.0, 1e12),
    "min_listed_trade_days": (0, 5000),
    "min_amount_ratio": (0.0, 100.0),
    "amount_ratio_days": (1, 250),
}
CONFIG_BOOL_KEYS = ("require_bullish_close", "require_unsealed", "require_true_gap")
INTEGER_CONFIG_KEYS = ("min_listed_trade_days", "amount_ratio_days")

# 只在这段时间内找"最近一次业绩公告"，更早的说明公司已长期未披露，不再参与
REPORT_LOOKBACK_DAYS = 420
CALENDAR_LOOKBACK_DAYS = 3 * 366
# 扣非 ROE(TTM) 需要本期、上年年报、上年同期三期数据
ROE_LOOKBACK_DAYS = 2 * 366

_refresh_lock = threading.Lock()
# 历史交易日的涨停价不会变，进程内缓存避免每次重算都重复请求
_stk_limit_cache: Dict[date, Dict[str, float]] = {}


class EarningsGapDataError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def normalize_config(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """把页面/库里的配置并进默认值，并做类型与范围校验；非法值直接抛 ValueError。"""
    config = dict(DEFAULT_CONFIG)
    for key, value in (payload or {}).items():
        if key not in DEFAULT_CONFIG:
            continue
        if key == "sources":
            sources = [str(item) for item in (value or []) if str(item) in SOURCE_LABELS]
            if not sources:
                raise ValueError("事件源至少要选一个（财报/快报/预告）")
            config["sources"] = sorted(set(sources), key=lambda item: SOURCE_PRIORITY[item])
        elif key in CONFIG_BOOL_KEYS:
            config[key] = bool(value)
        else:
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"{key} 必须是数字")
            low, high = CONFIG_NUMERIC_BOUNDS[key]
            if not low <= number <= high:
                raise ValueError(f"{key} 必须在 {low} ~ {high} 之间")
            config[key] = int(number) if key in INTEGER_CONFIG_KEYS else number
    if config["min_profit_yoy"] > config["max_profit_yoy"]:
        raise ValueError("净利同比下限不能大于上限")
    return config


def load_config() -> Dict[str, Any]:
    with get_db_ctx() as db:
        row = db.get(MarketSignalConfig, SIGNAL_KEY)
        payload = dict(row.payload) if row and row.payload else {}
    try:
        return normalize_config(payload)
    except ValueError:  # 库里存了坏值时不要把页面卡死
        logger.warning("earnings gap config invalid, falling back to defaults: %s", payload)
        return dict(DEFAULT_CONFIG)


def save_config(payload: Dict[str, Any], updated_by: Optional[str] = None) -> Dict[str, Any]:
    config = normalize_config(payload)
    with get_db_ctx() as db:
        db.merge(MarketSignalConfig(
            signal_key=SIGNAL_KEY,
            payload=config,
            updated_by=updated_by,
            updated_at=datetime.now(),
        ))
    return config


# ---------------------------------------------------------------------------
# 纯函数（便于单测）
# ---------------------------------------------------------------------------


def select_candidate_events(frame: pd.DataFrame) -> pd.DataFrame:
    """每只股票取最新一个报告期，保留该报告期里每个事件源的公告，作为候选事件。

    不能只留"每只股票最近一条公告"：同一季里预告/快报先发、财报后发，按单条最新取的话，
    7 月预告触发的断层信号到 8 月中报一出就被覆盖（生产上预告只剩 3 条、列表全是财报）。
    报告期一更新（比如三季报预告出来），上一期的事件就不再参与——那已经是旧消息了。
    同一只股票同一天多个事件源公告时只留一条（财报 > 快报 > 预告），避免同一根 T+1 K 线算两次。
    同一报告期多个事件源都触发时只保留最早那条，见 keep_earliest_signal_per_period。
    """
    columns = ["ts_code", "source", "end_date", "ann_date", "np_yoy"]
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)
    data = frame.dropna(subset=["ts_code", "end_date", "ann_date"]).copy()
    data["ts_code"] = data["ts_code"].astype(str).str.strip().str.upper()
    data["source_rank"] = data["source"].map(SOURCE_PRIORITY)
    latest_period = data.groupby("ts_code")["end_date"].transform("max")
    data = data[data["end_date"] == latest_period]
    data = data.sort_values(["ts_code", "ann_date", "source_rank"])
    # 同一报告期同一事件源多次公告（更正/修正）时，_load_events 已经只留首次公告
    data = data.drop_duplicates(subset=["ts_code", "source"], keep="first")
    return data.drop_duplicates(subset=["ts_code", "ann_date"], keep="first").reset_index(drop=True)


def keep_earliest_signal_per_period(signals: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """同一只股票同一报告期被多个事件源触发断层时，只保留最早的那条信号。

    回测（2018-08 至今，同一报告期多源触发的 45 组）：最早那条（35 组是预告）20 日 +6.31%、
    胜率 66.7%，后面的只有 -0.51%、51.1%。断层反应的是市场没预期到的新消息，
    预告之后的正式财报多半只是确认已知数字。按 T+1 先后比，同一天按 财报 > 快报 > 预告。
    """
    best: Dict[Tuple[str, date], Dict[str, Any]] = {}
    for signal in signals:
        row = signal["row"]
        key = (row.ts_code, row.end_date)
        rank = (row.t1_date, SOURCE_PRIORITY.get(row.source, 99))
        current = best.get(key)
        if current is None or rank < (current["row"].t1_date, SOURCE_PRIORITY.get(current["row"].source, 99)):
            best[key] = signal
    return list(best.values())


def express_np_yoy(n_income: Any, last_year_np: Any, fallback: Any = None) -> Optional[float]:
    """快报净利同比：净利润 ÷ 去年同期修正后净利润 − 1；基数 ≤ 0 时退回接口给的扣非同比。"""
    current = safe_float(n_income)
    base = safe_float(last_year_np)
    if current is not None and base is not None and base > 0:
        return (current / base - 1) * 100
    return safe_float(fallback)


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


def evaluate_t1_bar(
    bar: Dict[str, Any],
    up_limit: Optional[float],
    config: Dict[str, Any],
    *,
    prev_high: Optional[float] = None,
    amount_ratio: Optional[float] = None,
) -> Dict[str, Any]:
    """按 T+1 日线判断跳空高开、收阳、未封板、成交额等硬条件，返回各项指标和是否满足。

    prev_high 已换算到 T+1 的价格口径；amount_ratio 为 None 表示没有均量可比，不判量比条件。
    """
    open_price = safe_float(bar.get("open"))
    close = safe_float(bar.get("close"))
    low = safe_float(bar.get("low"))
    pre_close = safe_float(bar.get("pre_close"))
    amount_yuan = safe_float(bar.get("amount_yuan"))
    if not all(value is not None and value > 0 for value in (open_price, close, pre_close)):
        return {"passed": False}
    gap_pct = (open_price / pre_close - 1) * 100
    pct_chg = (close / pre_close - 1) * 100
    sealed = up_limit is not None and round(close, 2) >= round(up_limit, 2)
    true_gap = None if (low is None or prev_high is None) else low > prev_high + 1e-9
    passed = (
        gap_pct >= float(config["min_gap_pct"]) - 1e-9
        and amount_yuan is not None
        and amount_yuan >= float(config["min_amount_yuan"])
    )
    if config["require_bullish_close"] and close <= open_price:
        passed = False
    if config["require_unsealed"] and sealed:
        passed = False
    if config["require_true_gap"] and not true_gap:
        passed = False
    if config["min_amount_ratio"] and amount_ratio is not None and amount_ratio < float(config["min_amount_ratio"]):
        passed = False
    return {
        "passed": passed,
        "gap_pct": gap_pct,
        "pct_chg": pct_chg,
        "sealed": sealed,
        "true_gap": true_gap,
        "amount_yuan": amount_yuan,
    }


def gap_fill_status(
    prev_high: Optional[float],
    has_true_gap: Optional[bool],
    lows_after: Sequence[Tuple[date, float]],
) -> Dict[str, Any]:
    """缺口是否回补：T+1 之后任一天最低价 ≤ T 日最高价（同一价格口径）即视为回补。"""
    if not has_true_gap or prev_high is None:
        return {"has_true_gap": bool(has_true_gap), "gap_filled": None, "gap_filled_date": None}
    for trade_date, low in lows_after:
        if low is not None and low <= prev_high + 1e-9:
            return {"has_true_gap": True, "gap_filled": True, "gap_filled_date": trade_date}
    return {"has_true_gap": True, "gap_filled": False, "gap_filled_date": None}


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


def _text(value: Any) -> Optional[str]:
    """DuckDB 取回来的空文本列是 NaN（float），不能直接塞进 JSON。"""
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    text = str(value).strip()
    return text or None


def json_safe(value: Any) -> Any:
    """递归把 NaN/Inf、numpy 标量换成 JSON 能表示的值。

    payload 直接交给 FastAPI 序列化，任何一列漏了 NaN 都会让整个接口 500
    （"Out of range float values are not JSON compliant"），所以在出口统一兜一次底。
    """
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "item") and not isinstance(value, (str, bytes, date, datetime)):
        # numpy/pandas 标量
        try:
            return json_safe(value.item())
        except (AttributeError, ValueError):
            return value
    return value


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


def _table_exists(connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?", [table_name]
    ).fetchone()
    return bool(row and row[0])


def _load_events(connection, since: date, sources: Sequence[str], warnings: List[str]) -> pd.DataFrame:
    """三个事件源各取最近一次公告，合成统一结构：ts_code/source/end_date/ann_date/同比环比。"""
    frames = []
    if SOURCE_REPORT in sources:
        frame = connection.execute(
            """
            SELECT ts_code, end_date, ann_date, netprofit_yoy AS np_yoy, or_yoy,
                   q_netprofit_qoq AS np_qoq, q_sales_qoq AS or_qoq
            FROM a_stock_fina_indicator
            WHERE end_date >= ? AND ann_date IS NOT NULL
            """,
            [since],
        ).fetchdf()
        # 同一报告期多次公告（更正/重述）取首次公告那一行，那才是市场第一次看到的数据
        frame = frame.sort_values(["ts_code", "end_date", "ann_date"]).drop_duplicates(
            subset=["ts_code", "end_date"], keep="first"
        )
        frame["source"] = SOURCE_REPORT
        frames.append(frame)

    if SOURCE_EXPRESS in sources:
        if _table_exists(connection, "a_stock_express"):
            frame = connection.execute(
                """
                SELECT ts_code, end_date, ann_date, n_income, yoy_net_profit, yoy_dedu_np,
                       yoy_sales AS or_yoy
                FROM a_stock_express
                WHERE end_date >= ? AND ann_date IS NOT NULL
                """,
                [since],
            ).fetchdf()
            if not frame.empty:
                frame = frame.sort_values(["ts_code", "end_date", "ann_date"]).drop_duplicates(
                    subset=["ts_code", "end_date"], keep="first"
                )
                frame["np_yoy"] = [
                    express_np_yoy(row.n_income, row.yoy_net_profit, row.yoy_dedu_np)
                    for row in frame.itertuples(index=False)
                ]
                frame["source"] = SOURCE_EXPRESS
                frames.append(frame.drop(columns=["n_income", "yoy_net_profit", "yoy_dedu_np"]))
        else:
            warnings.append("分析库还没有业绩快报表（a_stock_express），本次未纳入快报事件")

    if SOURCE_FORECAST in sources:
        if _table_exists(connection, "a_stock_forecast"):
            frame = connection.execute(
                """
                SELECT ts_code, end_date, ann_date, p_change_min AS np_yoy, p_change_max, type AS forecast_type
                FROM a_stock_forecast
                WHERE end_date >= ? AND ann_date IS NOT NULL
                """,
                [since],
            ).fetchdf()
            if not frame.empty:
                frame = frame.sort_values(["ts_code", "end_date", "ann_date"]).drop_duplicates(
                    subset=["ts_code", "end_date"], keep="first"
                )
                frame["source"] = SOURCE_FORECAST
                frames.append(frame)
        else:
            warnings.append("分析库还没有业绩预告表（a_stock_forecast），本次未纳入预告事件")

    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        raise EarningsGapDataError("分析库没有近期业绩公告数据（财报/快报/预告）")
    combined = pd.concat(frames, ignore_index=True)
    for column in ("end_date", "ann_date"):
        combined[column] = combined[column].map(_to_date)
    for column in ("np_yoy", "or_yoy", "np_qoq", "or_qoq", "p_change_max"):
        if column not in combined.columns:
            combined[column] = None
        combined[column] = pd.to_numeric(combined[column], errors="coerce")
    if "forecast_type" not in combined.columns:
        combined["forecast_type"] = None
    return select_candidate_events(combined)


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
            SELECT m.ts_code, m.trade_date, m.open, m.high, m.low, m.close, m.pre_close, m.amount
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
            "low": row.low,
            "close": row.close,
            "pre_close": row.pre_close,
            "amount_yuan": None if pd.isna(row.amount) else float(row.amount) * 1000,
        }
    return bars


def _average_amounts(connection, windows: pd.DataFrame) -> Dict[Tuple[str, date], Dict[str, Any]]:
    """T+1 之前 N 个交易日的平均成交额（元）。windows 需含 ts_code/trade_date/window_start。"""
    if windows.empty:
        return {}
    connection.register("earnings_gap_windows", windows[["ts_code", "trade_date", "window_start"]])
    try:
        frame = connection.execute(
            """
            SELECT w.ts_code, w.trade_date, AVG(m.amount) AS avg_amount, COUNT(*) AS days
            FROM earnings_gap_windows w
            JOIN a_stock_market_daily m
              ON m.ts_code = w.ts_code
             AND m.trade_date >= w.window_start
             AND m.trade_date < w.trade_date
            GROUP BY w.ts_code, w.trade_date
            """
        ).fetchdf()
    finally:
        connection.unregister("earnings_gap_windows")
    return {
        (row.ts_code, _to_date(row.trade_date)): {
            "avg_amount_yuan": None if pd.isna(row.avg_amount) else float(row.avg_amount) * 1000,
            "days": int(row.days),
        }
        for row in frame.itertuples(index=False)
    }


def _forward_series(connection, codes: Sequence[str], since: date) -> Dict[str, pd.DataFrame]:
    """信号股票从最早信号日起的日线（含复权因子），用来算最大涨幅和缺口回补。"""
    if not codes:
        return {}
    placeholders = ",".join("?" for _ in codes)
    frame = connection.execute(
        f"""
        SELECT m.ts_code, m.trade_date, m.high, m.low, f.adj_factor
        FROM a_stock_market_daily m
        LEFT JOIN a_stock_adj_factor f ON f.ts_code = m.ts_code AND f.trade_date = m.trade_date
        WHERE m.ts_code IN ({placeholders}) AND m.trade_date >= ?
        ORDER BY m.ts_code, m.trade_date
        """,
        [*codes, since],
    ).fetchdf()
    if frame.empty:
        return {}
    frame["trade_date"] = frame["trade_date"].map(_to_date)
    return {code: part for code, part in frame.groupby("ts_code")}


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
    """每只股票分析库里最新一根日线的收盘价、复权因子和估值。"""
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
    frame = pd.DataFrame(sorted(set(pairs)), columns=["ts_code", "trade_date"])
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


def compute_earnings_gap(
    now: Optional[datetime] = None,
    service=None,
    connection=None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    from .tushare import TushareService

    now = now or datetime.now()
    today = now.date()
    config = normalize_config(config) if config is not None else load_config()
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
        calendar_index = {value: index for index, value in enumerate(calendar)}

        events = _load_events(connection, today - timedelta(days=REPORT_LOOKBACK_DAYS), config["sources"], warnings)
        basic = _load_basic(connection)
        data = events.merge(basic, on="ts_code", how="inner")
        stats = {"events": int(len(data))}
        stats.update({
            f"events_{source}": int((data["source"] == source).sum()) for source in config["sources"]
        })

        data = data[
            (data["np_yoy"] >= float(config["min_profit_yoy"]))
            & (data["np_yoy"] <= float(config["max_profit_yoy"]))
        ].copy()
        stats["growth_passed"] = int(len(data))
        if data.empty:
            stats.update({"pending": 0, "listed_passed": 0, "signals": 0})
            return _payload(now, last_trade_date, config, stats, warnings, [])

        data["t1_date"] = data["ann_date"].map(lambda value: next_trade_date(calendar, value))
        # 空 DataFrame 上 map 出来的是 object 掩码，直接拿去索引会被当成"选列"，必须转成 bool
        ready = data["t1_date"].map(lambda t1: t1 is not None and t1 <= last_trade_date).astype(bool)
        stats["pending"] = int((~ready).sum())
        data = data[ready].copy()

        data["listed_days"] = [
            listed_trade_days(calendar, list_date, t1)
            for list_date, t1 in zip(data["list_date"], data["t1_date"])
        ]
        data = data[data["listed_days"] >= int(config["min_listed_trade_days"])].copy()
        stats["listed_passed"] = int(len(data))
        if data.empty:
            stats["signals"] = 0
            return _payload(now, last_trade_date, config, stats, warnings, [])

        # T+1 与 T 两天的日线：T 日最高价用来判断真缺口
        data["t0_date"] = [
            calendar[calendar_index[t1] - 1] if calendar_index.get(t1) else None
            for t1 in data["t1_date"]
        ]
        pairs = pd.concat([
            pd.DataFrame({"ts_code": data["ts_code"], "trade_date": data["t1_date"]}),
            pd.DataFrame({"ts_code": data["ts_code"], "trade_date": data["t0_date"]}).dropna(),
        ], ignore_index=True)
        bars = _db_bars(connection, pairs)

        window_days = int(config["amount_ratio_days"])
        windows = pd.DataFrame({
            "ts_code": data["ts_code"],
            "trade_date": data["t1_date"],
            "window_start": [
                calendar[max(0, calendar_index[t1] - window_days)] for t1 in data["t1_date"]
            ],
        })
        averages = _average_amounts(connection, windows)
        adj = _adj_factors(
            connection,
            [(row.ts_code, row.t1_date) for row in data.itertuples(index=False)]
            + [(row.ts_code, row.t0_date) for row in data.itertuples(index=False) if row.t0_date],
        )

        signals = []
        for row in data.itertuples(index=False):
            bar = bars.get((row.ts_code, row.t1_date))
            if not bar:
                continue
            prev_bar = bars.get((row.ts_code, row.t0_date)) if row.t0_date else None
            prev_high = safe_float(prev_bar.get("high")) if prev_bar else None
            if prev_high is not None:
                # T 日最高价换算到 T+1 的价格口径（T+1 是除权日时两天价格不可直接比）
                adj_t0 = adj.get((row.ts_code, row.t0_date))
                adj_t1 = adj.get((row.ts_code, row.t1_date))
                if adj_t0 and adj_t1:
                    prev_high = prev_high * adj_t0 / adj_t1
            average = averages.get((row.ts_code, row.t1_date)) or {}
            avg_amount = average.get("avg_amount_yuan")
            amount_yuan = safe_float(bar.get("amount_yuan"))
            amount_ratio = (
                amount_yuan / avg_amount if amount_yuan is not None and avg_amount else None
            )
            quick = evaluate_t1_bar(bar, None, config, prev_high=prev_high, amount_ratio=amount_ratio)
            if not quick["passed"]:
                continue
            up_limit = None
            if config["require_unsealed"]:
                limits = _up_limits(service, row.t1_date, warnings)
                up_limit = limits.get(row.ts_code) or fallback_up_limit(row.ts_code, float(bar["pre_close"]))
            result = evaluate_t1_bar(bar, up_limit, config, prev_high=prev_high, amount_ratio=amount_ratio)
            if not result["passed"]:
                continue
            signals.append({
                "row": row,
                "bar": bar,
                "result": result,
                "up_limit": up_limit,
                "prev_high": prev_high,
                "amount_ratio": amount_ratio,
            })
        stats["triggered"] = len(signals)
        signals = keep_earliest_signal_per_period(signals)
        stats["signals"] = len(signals)

        items = _build_items(connection, signals, calendar)
        return _payload(now, last_trade_date, config, stats, warnings, items)
    finally:
        if own_connection:
            connection.close()


def _payload(now, last_trade_date, config, stats, warnings, items) -> Dict[str, Any]:
    return json_safe({
        "payload_version": PAYLOAD_VERSION,
        "computed_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "trade_date": last_trade_date.isoformat(),
        "criteria": config,
        "source_labels": SOURCE_LABELS,
        "stats": stats,
        "warnings": list(dict.fromkeys(warnings)),
        "items": items,
    })


def _build_items(connection, signals, calendar) -> List[Dict[str, Any]]:
    if not signals:
        return []
    codes = sorted({signal["row"].ts_code for signal in signals})
    latest_db = _latest_db_prices(connection, codes)
    adj = _adj_factors(connection, [(signal["row"].ts_code, signal["row"].t1_date) for signal in signals])
    roe = _dedt_roe_by_code(connection, codes, calendar[-1] - timedelta(days=ROE_LOOKBACK_DAYS))
    series = _forward_series(connection, codes, min(signal["row"].t1_date for signal in signals))

    items = []
    for signal in signals:
        row, bar, result = signal["row"], signal["bar"], signal["result"]
        code = row.ts_code
        t1 = row.t1_date
        t1_close = float(bar["close"])
        latest = latest_db.get(code) or {}
        latest_date = latest.get("trade_date")
        latest_close = latest.get("close")
        adj_last = latest.get("adj_factor")
        adj_t1 = adj.get((code, t1)) or adj_last
        pb = latest.get("pb")
        roe_ttm = roe.get(code)

        # 复权因子缺失（老数据）时退回不复权，比整列空着有用
        factor_last = (adj_last / adj_t1) if adj_last and adj_t1 else 1.0
        since_pct = None
        if latest_date is not None and latest_close:
            since_pct = (latest_close * factor_last / t1_close - 1) * 100

        # 买在 T+1 收盘，最大涨幅从 T+2 起算；缺口回补同样只看 T+1 之后
        max_gain_pct = None
        fill = {"has_true_gap": bool(result.get("true_gap")), "gap_filled": None, "gap_filled_date": None}
        part = series.get(code)
        if part is not None:
            after = part[part["trade_date"] > t1]

            def _factor(value):
                return (float(value) / adj_t1) if adj_t1 and not pd.isna(value) else 1.0

            highs = [
                float(item.high) * _factor(item.adj_factor)
                for item in after.itertuples(index=False)
                if not pd.isna(item.high)
            ]
            if highs:
                max_gain_pct = (max(highs) / t1_close - 1) * 100
            lows_after = [
                (item.trade_date, float(item.low) * _factor(item.adj_factor))
                for item in after.itertuples(index=False)
                if not pd.isna(item.low)
            ]
            fill = gap_fill_status(signal["prev_high"], result.get("true_gap"), lows_after)

        holding_days = None
        if latest_date is not None:
            holding_days = max(0, bisect.bisect_right(calendar, latest_date) - bisect.bisect_right(calendar, t1))

        items.append({
            "symbol": code,
            "name": _text(row.name),
            "industry": _text(row.industry),
            "source": row.source,
            "source_label": SOURCE_LABELS.get(row.source, row.source),
            "forecast_type": _text(getattr(row, "forecast_type", None)),
            "np_yoy_max": _round(getattr(row, "p_change_max", None)),
            "end_date": row.end_date.isoformat(),
            "ann_date": row.ann_date.isoformat(),
            "np_yoy": _round(row.np_yoy),
            "or_yoy": _round(getattr(row, "or_yoy", None)),
            "np_qoq": _round(getattr(row, "np_qoq", None)),
            "or_qoq": _round(getattr(row, "or_qoq", None)),
            "signal_date": t1.isoformat(),
            "t1_open_gap_pct": _round(result["gap_pct"]),
            "t1_pct_chg": _round(result["pct_chg"]),
            "prev_high": _round(signal["prev_high"], 3),
            "t1_open": _round(bar.get("open"), 3),
            "t1_close": _round(t1_close, 3),
            "t1_up_limit": _round(signal["up_limit"], 3),
            "t1_amount_yi": _round(result["amount_yuan"] / 1e8, 2),
            "amount_ratio": _round(signal["amount_ratio"]),
            "listed_trade_days": int(row.listed_days),
            "latest_price": _round(latest_close, 3),
            "latest_date": latest_date.isoformat() if latest_date else None,
            "since_pct": _round(since_pct),
            "max_gain_pct": _round(max_gain_pct),
            "holding_days": holding_days,
            "has_true_gap": fill["has_true_gap"],
            "gap_filled": fill["gap_filled"],
            "gap_filled_date": fill["gap_filled_date"].isoformat() if fill["gap_filled_date"] else None,
            "pe_ttm": _round(latest.get("pe_ttm")),
            "pb": _round(pb),
            "ps_ttm": _round(latest.get("ps_ttm")),
            "roe_dedt_ttm": _round(roe_ttm),
            "roe_pb": _round(roe_ttm / pb) if roe_ttm is not None and pb and pb > 0 else None,
        })
    items.sort(key=lambda item: (item["signal_date"], item["np_yoy"] or 0), reverse=True)
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
    """默认读快照；结构版本过期或阈值改过（快照与当前配置对不上）时自动重算。"""
    if not refresh:
        snapshot = load_earnings_gap_snapshot()
        if snapshot:
            stale_version = int(snapshot.get("payload_version") or 0) < PAYLOAD_VERSION
            stale_config = snapshot.get("criteria") != load_config()
            if not stale_version and not stale_config:
                return snapshot
            logger.info(
                "earnings gap snapshot stale (version=%s config_changed=%s), recomputing",
                snapshot.get("payload_version"), stale_config,
            )
    return refresh_earnings_gap()

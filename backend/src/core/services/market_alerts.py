"""提示看板：盘中扫描全市场，按四档标签记录个股首次命中。

口径与调用预算（每轮只调 1 次 tushare）：

- 盘中每分钟一次 `rt_k` 通配符全市场快照，直接拿到现价/昨收/今开/当日累计量/当日累计额；
  5 分钟涨速由本进程前几轮快照差分得到，不额外拉分钟线。
- 同时段量比 = 当日累计量 ÷ 前 20 个交易日同一时刻累计量均值，基准表盘前从 DuckDB
  `a_stock_minute_bar`（全市场滚动 32 个交易日，每天 21:30 盘后同步）算一次，不占接口。
- 九转高/低计数、均量、名称行业等来自 DuckDB 日线，涨跌停价每天取一次 `stk_limit`。

标签（一根轴上的四档，方向由九转计数定，强度由量比与位置定）：

- 强势：活跃全部条件 + 九转高计数 3~4（实测这一档命中后表现最好）+ 量比 ≥2
- 活跃：九转高计数 ≥2 · 当日上涨 · 现价>今开 · 累计成交额 ≥0.8亿 · 量比 ≥1.3
- 观望：九转低计数 ≥2，或急跌结构（较 3 日前跌 >4% 且跌破 4 日前开盘价）
- 规避：观望条件 + 九转低计数 ≥4 + 当日下跌

每只个股每个交易日只记第一次命中；命中后每轮只更新现价与命中后涨幅，用于事后打分。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from ..database import MarketAlertHit, get_db_ctx
from .duckdb_analytics import connect_analytics_db, safe_float
from .tushare import TushareService


logger = logging.getLogger(__name__)

LABEL_STRONG = "强势"
LABEL_ACTIVE = "活跃"
LABEL_WATCH = "观望"
LABEL_AVOID = "规避"
BULLISH_LABELS = (LABEL_STRONG, LABEL_ACTIVE)

SCAN_START = dtime(9, 35)
SCAN_END = dtime(15, 5)
LUNCH_START = dtime(11, 31)
LUNCH_END = dtime(13, 0)
SPEED_LOOKBACK_ROUNDS = 5          # 5 分钟涨速：与 5 轮之前的快照比
BASELINE_TRADING_DAYS = 20   # 分析库滚动保留 32 个交易日，20 日基准仍在窗口内
MIN_LISTED_TRADING_DAYS = 60


@dataclass(frozen=True)
class AlertThresholds:
    """全部阈值集中在这里，后续按命中后涨幅回测结果调整。"""
    min_amount_yuan: float = 0.8e8
    min_volume_ratio: float = 1.3
    strong_volume_ratio: float = 2.0
    active_td_up_min: int = 2
    strong_td_up_min: int = 3
    strong_td_up_max: int = 4
    watch_td_down_min: int = 2
    avoid_td_down_min: int = 4
    drop_pct_vs_3d: float = 4.0


DEFAULT_THRESHOLDS = AlertThresholds()


@dataclass
class StockStructure:
    """盘前算好的个股结构：九转计数基数、参考收盘价、名称行业等。"""
    ts_code: str
    name: str = ""
    industry: str = ""          # = 申万一级，保留这个字段名兼容老调用方
    industry_l1: str = ""       # 申万一级（与行业关联同一口径）
    industry_l2: str = ""
    industry_l3: str = ""
    td_up_prev: int = 0        # 截至昨日的连续 C>REF(C,4) 天数
    td_down_prev: int = 0      # 截至昨日的连续 C<REF(C,4) 天数
    close_ref: List[float] = field(default_factory=list)   # 最近 5 日收盘，close_ref[-1] 为昨收
    open_ref4: Optional[float] = None                      # 4 日前开盘价
    listed_days: int = 0
    is_st: bool = False
    days_since_low9: Optional[int] = None                  # 距上次「低9」的交易日数（仅记录，不参与判定）

    def __post_init__(self) -> None:
        # ST 一律由名称派生，避免调用方漏传标志位导致风险股被当成正常股打标签
        if not self.is_st and "ST" in (self.name or "").upper():
            object.__setattr__(self, "is_st", True)


def in_scan_window(now: datetime) -> bool:
    current = now.time()
    if current < SCAN_START or current > SCAN_END:
        return False
    return not (LUNCH_START <= current < LUNCH_END)


def td_counts(closes: List[float]) -> Tuple[int, int, Optional[int]]:
    """返回 (连续高计数, 连续低计数, 距上次低9的K线数)。高=C>REF(C,4)，低=C<REF(C,4)。"""
    up = down = 0
    last_low9: Optional[int] = None
    up_run = down_run = 0
    for i in range(4, len(closes)):
        if closes[i] > closes[i - 4]:
            up_run, down_run = up_run + 1, 0
        elif closes[i] < closes[i - 4]:
            down_run, up_run = down_run + 1, 0
        else:
            up_run = down_run = 0
        if down_run >= 9:
            last_low9 = len(closes) - 1 - i
        up, down = up_run, down_run
    return up, down, last_low9


def _text(value: Any) -> str:
    return "" if value is None or (isinstance(value, float) and pd.isna(value)) else str(value)


def build_structures(connection, today: date, lookback_days: int = 90) -> Dict[str, StockStructure]:
    """从 DuckDB 日线算出每只股票的九转计数基数与参考价（不含当日）。"""
    frame = connection.execute(
        """
        SELECT d.ts_code, d.trade_date, d.close, d.open, b.name, b.list_date,
               m.l1_name, m.l2_name, m.l3_name
        FROM a_stock_market_daily d
        LEFT JOIN a_stock_basic b ON b.ts_code = d.ts_code
        -- 行业统一用申万三级（与行业关联同一口径），不用 stock_basic 的单级 industry
        LEFT JOIN a_stock_sw_member m ON m.ts_code = d.ts_code
        WHERE d.trade_date >= ? AND d.trade_date < ?
        ORDER BY d.ts_code, d.trade_date
        """,
        [today - timedelta(days=lookback_days * 2), today],
    ).fetchdf()
    if frame.empty:
        return {}

    structures: Dict[str, StockStructure] = {}
    for ts_code, group in frame.groupby("ts_code"):
        closes = [float(x) for x in group["close"].tolist() if x is not None and not pd.isna(x)]
        if len(closes) < 10:
            continue
        up, down, since_low9 = td_counts(closes)
        name = str(group["name"].iloc[-1] or "")
        list_date = group["list_date"].iloc[-1]
        structures[str(ts_code)] = StockStructure(
            ts_code=str(ts_code),
            name=name,
            industry=_text(group["l1_name"].iloc[-1]),
            industry_l1=_text(group["l1_name"].iloc[-1]),
            industry_l2=_text(group["l2_name"].iloc[-1]),
            industry_l3=_text(group["l3_name"].iloc[-1]),
            td_up_prev=up,
            td_down_prev=down,
            close_ref=closes[-5:],
            open_ref4=safe_float(group["open"].iloc[-4]) if len(group) >= 4 else None,
            listed_days=len(closes),
            days_since_low9=since_low9,
        )
    return structures


def build_volume_baseline(connection, today: date, days: int = BASELINE_TRADING_DAYS) -> pd.DataFrame:
    """同时段基准：前 N 个有数据的交易日，每只股票每个时刻的累计成交量均值。

    返回 index=ts_code、columns=HH:MM 的 float32 矩阵（约 5500×241）。
    不要摊平成 {(代码, 时刻): 量} 的字典——那样 130 万个元组键要占 200MB 以上内存，
    而矩阵只要几 MB，每轮扫描也只需要取出当前分钟那一列。
    """
    dates = [
        row[0] for row in connection.execute(
            """
            SELECT DISTINCT CAST(trade_time AS DATE) AS d
            FROM a_stock_minute_bar
            WHERE CAST(trade_time AS DATE) < ?
            ORDER BY d DESC LIMIT ?
            """,
            [today, days],
        ).fetchall()
    ]
    if not dates:
        return pd.DataFrame()
    frame = connection.execute(
        """
        WITH cum AS (
            SELECT ts_code,
                   CAST(trade_time AS DATE) AS d,
                   strftime(trade_time, '%H:%M') AS t,
                   SUM(vol) OVER (PARTITION BY ts_code, CAST(trade_time AS DATE) ORDER BY trade_time) AS cum_vol
            FROM a_stock_minute_bar
            WHERE CAST(trade_time AS DATE) >= ? AND CAST(trade_time AS DATE) <= ?
        )
        SELECT ts_code, t, AVG(cum_vol) AS base_cum_vol
        FROM cum GROUP BY ts_code, t
        """,
        [min(dates), max(dates)],
    ).fetchdf()
    if frame.empty:
        return pd.DataFrame()
    matrix = frame.pivot(index="ts_code", columns="t", values="base_cum_vol")
    return matrix.astype("float32")


def baseline_at(baseline: pd.DataFrame, minute_label: str) -> Dict[str, float]:
    """取出某一分钟那一列：{ts_code: 基准累计量}。

    分钟列只有真实交易分钟（09:30~11:30、13:01~15:00），所以午休、收盘后或任何
    非交易分钟直接按 minute_label 取会整列取空，进而让所有多头标签消失。
    这里退到「不晚于该时刻的最近一个分钟列」；早于开盘才返回空。
    """
    if baseline is None or baseline.empty:
        return {}
    column = _nearest_minute_column(baseline.columns, minute_label)
    if column is None:
        return {}
    values = baseline[column].dropna()
    return {str(code): float(value) for code, value in values.items() if value > 0}


def _nearest_minute_column(columns: Any, minute_label: str) -> Optional[str]:
    candidates = sorted(str(column) for column in columns)
    if not candidates:
        return None
    if minute_label in candidates:
        return minute_label
    earlier = [column for column in candidates if column <= minute_label]
    return earlier[-1] if earlier else None


def classify(
    *,
    price: float,
    pre_close: float,
    day_open: float,
    cum_amount: float,
    volume_ratio: Optional[float],
    structure: StockStructure,
    thresholds: AlertThresholds = DEFAULT_THRESHOLDS,
) -> Optional[str]:
    """按四档标签判定；返回 None 表示当前不打标签。"""
    if not price or not pre_close or pre_close <= 0:
        return None
    if structure.is_st or structure.listed_days < MIN_LISTED_TRADING_DAYS:
        return None

    pct = (price / pre_close - 1) * 100
    refs = structure.close_ref
    close_ref4 = refs[-4] if len(refs) >= 4 else None
    close_ref3 = refs[-3] if len(refs) >= 3 else None

    td_up = structure.td_up_prev + 1 if close_ref4 and price > close_ref4 else 0
    td_down = structure.td_down_prev + 1 if close_ref4 and price < close_ref4 else 0

    # 多头侧：九转高计数 + 上涨 + 站上今开 + 成交额门槛 + 同时段放量
    if (td_up >= thresholds.active_td_up_min and pct > 0 and day_open and price > day_open
            and cum_amount >= thresholds.min_amount_yuan
            and volume_ratio is not None and volume_ratio >= thresholds.min_volume_ratio):
        if (thresholds.strong_td_up_min <= td_up <= thresholds.strong_td_up_max
                and volume_ratio >= thresholds.strong_volume_ratio):
            return LABEL_STRONG
        return LABEL_ACTIVE

    # 空头侧：九转低计数，或急跌结构（较 3 日前跌超 4% 且跌破 4 日前开盘）
    sharp_drop = bool(
        close_ref4 and close_ref3 and structure.open_ref4
        and price < close_ref4 and price < structure.open_ref4 and price < close_ref3
        and (close_ref3 - price) / price * 100 > thresholds.drop_pct_vs_3d
    )
    if td_down >= thresholds.watch_td_down_min or sharp_drop:
        if td_down >= thresholds.avoid_td_down_min and pct < 0:
            return LABEL_AVOID
        return LABEL_WATCH
    return None


def alert_score(pct: float, volume_ratio: Optional[float], td_up: int) -> float:
    """0~100 综合分：涨幅 45% + 量能 25% + 结构 30%，用于同档内排序。"""
    pct_score = max(0.0, min(1.0, (pct + 5) / 15)) * 45
    volume_score = max(0.0, min(1.0, ((volume_ratio or 0) - 0.5) / 2.5)) * 25
    # 结构分：高计数 3~4 给满分，1 和 ≥7 明显更差
    structure_map = {0: 0.0, 1: 0.3, 2: 0.6, 3: 1.0, 4: 1.0, 5: 0.7, 6: 0.6}
    structure_score = structure_map.get(td_up, 0.4) * 30
    return round(pct_score + volume_score + structure_score, 1)


def evaluate_snapshot(
    quotes: pd.DataFrame,
    structures: Dict[str, StockStructure],
    baseline_minute: Dict[str, float],
    minute_label: str,
    previous_pct: Optional[Dict[str, float]] = None,
    thresholds: AlertThresholds = DEFAULT_THRESHOLDS,
) -> List[Dict[str, Any]]:
    """把一轮全市场快照评成命中行（不含"每日仅首次"的去重，由调用方负责）。"""
    results: List[Dict[str, Any]] = []
    if quotes is None or quotes.empty:
        return results
    for quote in quotes.itertuples(index=False):
        ts_code = str(getattr(quote, "ts_code", "")).strip().upper()
        structure = structures.get(ts_code)
        if not structure:
            continue
        price = safe_float(getattr(quote, "close", None))
        pre_close = safe_float(getattr(quote, "pre_close", None))
        day_open = safe_float(getattr(quote, "open", None))
        cum_amount = safe_float(getattr(quote, "amount", None)) or 0.0
        cum_vol = safe_float(getattr(quote, "vol", None)) or 0.0
        if not price or not pre_close:
            continue
        base = baseline_minute.get(ts_code)
        volume_ratio = round(cum_vol / base, 2) if base and base > 0 else None

        label = classify(
            price=price, pre_close=pre_close, day_open=day_open or 0.0,
            cum_amount=cum_amount, volume_ratio=volume_ratio,
            structure=structure, thresholds=thresholds,
        )
        if not label:
            continue
        pct = (price / pre_close - 1) * 100
        close_ref4 = structure.close_ref[-4] if len(structure.close_ref) >= 4 else None
        td_up = structure.td_up_prev + 1 if close_ref4 and price > close_ref4 else 0
        td_down = structure.td_down_prev + 1 if close_ref4 and price < close_ref4 else 0
        speed5 = None
        if previous_pct and ts_code in previous_pct:
            speed5 = round(pct - previous_pct[ts_code], 2)
        results.append({
            "ts_code": ts_code,
            "name": structure.name,
            "industry": structure.industry,
            "industry_l1": structure.industry_l1,
            "industry_l2": structure.industry_l2,
            "industry_l3": structure.industry_l3,
            "label": label,
            "hit_time": minute_label,
            "price": round(price, 3),
            "pct": round(pct, 2),
            "amount_yi": round(cum_amount / 1e8, 2),
            "volume_ratio": volume_ratio,
            "speed5": speed5,
            "td_up": td_up,
            "td_down": td_down,
            "days_since_low9": structure.days_since_low9,
            "score": alert_score(pct, volume_ratio, td_up),
        })
    return results


class AlertScanner:
    """进程内保存基准表与最近几轮快照，供 1 分钟一轮的定时任务调用。"""

    def __init__(self) -> None:
        self._baseline_date: Optional[date] = None
        self._baseline_days: int = BASELINE_TRADING_DAYS
        self._baseline: pd.DataFrame = pd.DataFrame()
        self._structures: Dict[str, StockStructure] = {}
        self._pct_history: List[Dict[str, float]] = []
        self._recorded: Dict[date, set] = {}
        self._limits: Dict[str, Tuple[Optional[float], Optional[float]]] = {}
        self._limits_date: Optional[date] = None

    def prepare(self, today: date, connection=None,
                baseline_days: int = BASELINE_TRADING_DAYS) -> Dict[str, Any]:
        """盘前（或当天首轮）构建基准表与结构快照。"""
        own = connection is None
        connection = connection or connect_analytics_db()
        try:
            self._structures = build_structures(connection, today)
            self._baseline = build_volume_baseline(connection, today, days=baseline_days)
            # 分钟库还没同步好或分析库被占用时会拿到空基准，这时不要标记成已准备，
            # 否则这一整天的多头标签都会因为没有量比而消失
            if self._structures and not self._baseline.empty:
                self._baseline_date = today
            else:
                self._baseline_date = None
                logger.warning(
                    "提示看板基准未就绪：结构 %s 只、基准 %s 个点，下次调用会重试",
                    len(self._structures), int(self._baseline.size),
                )
            self._baseline_days = baseline_days
        finally:
            if own:
                connection.close()
        self._pct_history = []
        return {
            "structures": len(self._structures),
            "baseline_points": int(self._baseline.size),
            "baseline_days": baseline_days,
        }

    def limits(self, today: date) -> Dict[str, Tuple[Optional[float], Optional[float]]]:
        """当日涨跌停价，每天只取一次 stk_limit。"""
        if self._limits_date == today:
            return self._limits
        try:
            frame = TushareService.get_instance().get_a_stock_stk_limit_frame(today)
            if frame is not None and not frame.empty:
                self._limits = {
                    str(row.ts_code).strip().upper(): (safe_float(row.up_limit), safe_float(row.down_limit))
                    for row in frame.itertuples(index=False)
                }
                self._limits_date = today
            else:
                # 盘前 stk_limit 还没发布时会取到空；不要把空结果缓存一整天
                logger.warning("tushare stk_limit 返回空（%s），下次调用会重试", today)
                self._limits = {}
        except Exception as exc:  # noqa: BLE001  取不到只影响涨停/连板统计
            logger.warning("tushare stk_limit 获取 %s 失败: %s", today, exc)
            self._limits = {}
        return self._limits

    def _recorded_today(self, today: date) -> set:
        if today not in self._recorded:
            with get_db_ctx() as db:
                rows = db.query(MarketAlertHit.ts_code).filter(MarketAlertHit.trade_date == today).all()
            self._recorded = {today: {row[0] for row in rows}}
        return self._recorded[today]

    def scan(self, now: Optional[datetime] = None,
             thresholds: AlertThresholds = DEFAULT_THRESHOLDS) -> Dict[str, Any]:
        now = now or datetime.now()
        today = now.date()
        if not in_scan_window(now):
            return {"skipped": "不在扫描时段"}
        if self._baseline_date != today:
            self.prepare(today)
        if not self._structures:
            return {"skipped": "分析库没有日线数据"}

        quotes = get_market_snapshot()
        if quotes is None or quotes.empty:
            return {"skipped": "快照为空"}
        if "trade_time" in quotes.columns:
            quotes = quotes[pd.to_datetime(quotes["trade_time"], errors="coerce").dt.date == today]
        if quotes.empty:
            return {"skipped": "快照不是当日行情"}

        minute_label = now.strftime("%H:%M")
        previous = self._pct_history[0] if len(self._pct_history) >= SPEED_LOOKBACK_ROUNDS else None
        hits = evaluate_snapshot(
            quotes, self._structures, baseline_at(self._baseline, minute_label),
            minute_label, previous, thresholds,
        )

        current_pct: Dict[str, float] = {}
        for quote in quotes.itertuples(index=False):
            price = safe_float(getattr(quote, "close", None))
            pre_close = safe_float(getattr(quote, "pre_close", None))
            if price and pre_close:
                current_pct[str(quote.ts_code).strip().upper()] = (price / pre_close - 1) * 100
        self._pct_history.append(current_pct)
        if len(self._pct_history) > SPEED_LOOKBACK_ROUNDS:
            self._pct_history.pop(0)

        recorded = self._recorded_today(today)
        new_rows = [hit for hit in hits if hit["ts_code"] not in recorded]
        price_map = {code: pct for code, pct in current_pct.items()}
        with get_db_ctx() as db:
            for hit in new_rows:
                db.add(MarketAlertHit(
                    trade_date=today,
                    ts_code=hit["ts_code"],
                    hit_time=hit["hit_time"],
                    name=hit["name"],
                    industry=hit["industry"],
                    industry_l1=hit["industry_l1"],
                    industry_l2=hit["industry_l2"],
                    industry_l3=hit["industry_l3"],
                    label=hit["label"],
                    score=hit["score"],
                    price=hit["price"],
                    pct=hit["pct"],
                    amount_yi=hit["amount_yi"],
                    volume_ratio=hit["volume_ratio"],
                    speed5=hit["speed5"],
                    td_up=hit["td_up"],
                    td_down=hit["td_down"],
                    days_since_low9=hit["days_since_low9"],
                    last_price=hit["price"],
                    cum_pct=0.0,
                ))
                recorded.add(hit["ts_code"])
            # 已记录的行每轮刷新现价与命中后涨幅，供事后打分
            for row in db.query(MarketAlertHit).filter(MarketAlertHit.trade_date == today).all():
                pct_now = price_map.get(row.ts_code)
                quote_price = None
                if pct_now is not None and row.price:
                    quote_price = row.price * (1 + (pct_now - (row.pct or 0)) / 100)
                if quote_price:
                    row.last_price = round(quote_price, 3)
                    row.cum_pct = round((quote_price / row.price - 1) * 100, 2)
        return {
            "minute": minute_label,
            "scanned": int(len(quotes)),
            "hits": len(hits),
            "recorded": len(new_rows),
        }


_snapshot_cache: Tuple[float, Optional[pd.DataFrame]] = (0.0, None)
SNAPSHOT_TTL_SECONDS = 30


def get_market_snapshot(force: bool = False) -> Optional[pd.DataFrame]:
    """全市场快照（rt_k 通配符），30 秒内复用。

    提示看板扫描与行业关联聚合共用这一份，保证每分钟只打一次 tushare。
    """
    global _snapshot_cache
    cached_at, cached = _snapshot_cache
    if not force and cached is not None and time.time() - cached_at < SNAPSHOT_TTL_SECONDS:
        return cached
    frame = TushareService.get_instance().get_a_stock_realtime_market_frame()
    _snapshot_cache = (time.time(), frame)
    return frame


_scanner = AlertScanner()


def get_scanner_state(today: date) -> Dict[str, Any]:
    """行业关联复用扫描器盘前算好的结构/基准/涨跌停价，没准备好就现算。"""
    if _scanner._baseline_date != today:
        _scanner.prepare(today)
    return {
        "structures": _scanner._structures,
        "baseline": _scanner._baseline,
        "limits": _scanner.limits(today),
    }


def run_alert_scan(now: Optional[datetime] = None,
                   thresholds: AlertThresholds = DEFAULT_THRESHOLDS) -> Dict[str, Any]:
    return _scanner.scan(now=now, thresholds=thresholds)


def prepare_alert_baseline(today: Optional[date] = None,
                           baseline_days: int = BASELINE_TRADING_DAYS) -> Dict[str, Any]:
    return _scanner.prepare(today or date.today(), baseline_days=baseline_days)


def _row_to_dict(row: MarketAlertHit) -> Dict[str, Any]:
    return {
        "ts_code": row.ts_code,
        "code": row.ts_code.split(".")[0],
        "name": row.name,
        "industry": row.industry,
        "industry_l1": row.industry_l1 or row.industry,
        "industry_l2": row.industry_l2 or row.industry,
        "industry_l3": row.industry_l3 or row.industry,
        "label": row.label,
        "score": row.score,
        "hit_time": row.hit_time,
        "price": row.price,
        "pct": row.pct,
        "amount_yi": row.amount_yi,
        "volume_ratio": row.volume_ratio,
        "speed5": row.speed5,
        "td_up": row.td_up,
        "td_down": row.td_down,
        "days_since_low9": row.days_since_low9,
        "last_price": row.last_price,
        "cum_pct": row.cum_pct,
    }


TIME_BUCKETS: Tuple[Tuple[str, str, str], ...] = (
    ("09:35~10:00", "09:35", "10:00"),
    ("10:00~10:30", "10:00", "10:30"),
    ("10:30~11:00", "10:30", "11:00"),
    ("11:00~11:30", "11:00", "11:31"),
    ("13:00~13:30", "13:00", "13:30"),
    ("13:30~14:00", "13:30", "14:00"),
    ("14:00~14:30", "14:00", "14:30"),
    ("14:30~15:05", "14:30", "15:06"),
)
RETURN_BUCKETS: Tuple[Tuple[str, Optional[float], Optional[float]], ...] = (
    ("+5%以上", 5.0, None),
    ("+3%~+5%", 3.0, 5.0),
    ("0~+3%", 0.0, 3.0),
    ("-3%~0", -3.0, 0.0),
    ("-3%以下", None, -3.0),
)


INDUSTRY_LEVELS = ("l1", "l2", "l3")


def summarize_hits(rows: List[Dict[str, Any]], level: str = "l1") -> Dict[str, Any]:
    """命中后表现统计：总体、按标签、按时段、按行业（指定申万级别）、按涨幅分档。"""
    industry_key = f"industry_{level if level in INDUSTRY_LEVELS else 'l1'}"
    scored = [row for row in rows if row.get("cum_pct") is not None]

    def _avg(values: List[float]) -> Optional[float]:
        return round(sum(values) / len(values), 2) if values else None

    by_label: Dict[str, Dict[str, Any]] = {}
    for label in (LABEL_STRONG, LABEL_ACTIVE, LABEL_WATCH, LABEL_AVOID):
        group = [row["cum_pct"] for row in scored if row["label"] == label]
        if group:
            by_label[label] = {
                "count": len(group),
                "avg": _avg(group),
                "win_rate": round(sum(1 for v in group if v > 0) / len(group) * 100, 1),
            }

    buckets = []
    for name, start, end in TIME_BUCKETS:
        group = [row["cum_pct"] for row in scored if start <= (row["hit_time"] or "") < end]
        buckets.append({"label": name, "count": len(group), "avg": _avg(group)})

    distribution = []
    for name, low, high in RETURN_BUCKETS:
        count = sum(
            1 for row in scored
            if (low is None or row["cum_pct"] >= low) and (high is None or row["cum_pct"] < high)
        )
        distribution.append({"label": name, "count": count})

    industries: Dict[str, List[float]] = {}
    for row in scored:
        # 老记录没有申万字段时退回旧的单级 industry，避免历史日期全部归到"未知"
        name = row.get(industry_key) or row.get("industry") or "未知"
        industries.setdefault(name, []).append(row["cum_pct"])
    industry_rows = sorted(
        ({"industry": k, "count": len(v), "avg": _avg(v)} for k, v in industries.items()),
        key=lambda item: -item["count"],
    )[:20]

    values = [row["cum_pct"] for row in scored]
    best = max(scored, key=lambda row: row["cum_pct"], default=None)
    worst = min(scored, key=lambda row: row["cum_pct"], default=None)
    bullish = [row["cum_pct"] for row in scored if row["label"] in BULLISH_LABELS]
    return {
        "total": len(rows),
        "scored": len(values),
        "avg": _avg(values),
        "win_rate": round(sum(1 for v in values if v > 0) / len(values) * 100, 1) if values else None,
        "bullish_avg": _avg(bullish),
        "best": {"name": best["name"], "cum_pct": best["cum_pct"]} if best else None,
        "worst": {"name": worst["name"], "cum_pct": worst["cum_pct"]} if worst else None,
        "by_label": by_label,
        "time_buckets": buckets,
        "return_distribution": distribution,
        "industries": industry_rows,
    }


def fetch_alerts(
    trade_date: Optional[date] = None,
    label: Optional[str] = None,
    level: str = "l1",
) -> Dict[str, Any]:
    """读取某个交易日的命中记录与事后统计。"""
    with get_db_ctx() as db:
        dates = [
            row[0] for row in db.query(MarketAlertHit.trade_date)
            .distinct().order_by(MarketAlertHit.trade_date.desc()).limit(30).all()
        ]
        target = trade_date or (dates[0] if dates else None)
        rows: List[Dict[str, Any]] = []
        if target:
            query = db.query(MarketAlertHit).filter(MarketAlertHit.trade_date == target)
            if label:
                query = query.filter(MarketAlertHit.label == label)
            rows = [_row_to_dict(row) for row in query.order_by(MarketAlertHit.hit_time.desc()).all()]
    return {
        "date": target.isoformat() if target else None,
        "dates": [d.isoformat() for d in dates],
        "rows": rows,
        "industry_level": level if level in INDUSTRY_LEVELS else "l1",
        "summary": summarize_hits(rows, level),
        "thresholds": {
            "min_amount_yi": DEFAULT_THRESHOLDS.min_amount_yuan / 1e8,
            "min_volume_ratio": DEFAULT_THRESHOLDS.min_volume_ratio,
            "strong_volume_ratio": DEFAULT_THRESHOLDS.strong_volume_ratio,
            "active_td_up_min": DEFAULT_THRESHOLDS.active_td_up_min,
        },
    }

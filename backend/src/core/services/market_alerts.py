"""提示看板：盘中扫描全市场，按四档标签记录个股首次命中。

口径与调用预算（每轮只调 1 次 tushare）：

- 盘中每分钟一次 `rt_k` 通配符全市场快照，直接拿到现价/昨收/今开/当日累计量/当日累计额；
  5 分钟涨速由本进程前几轮快照差分得到，不额外拉分钟线。
- 同时段量比 = 当日累计量 ÷ 前 20 个交易日同一时刻累计量均值，基准表盘前从 DuckDB
  `a_stock_minute_bar`（全市场滚动 32 个交易日，每天 21:30 盘后同步）算一次，不占接口。
- 九转高/低计数、均量、名称行业等来自 DuckDB 日线，涨跌停价每天取一次 `stk_limit`。

标签（一根轴上的四档，方向由九转计数定，强度由量比与位置定）：

- 强势：活跃 + 当日累计量 ≥ 前 8 日最大日成交量
- 活跃：价格结构 COND1|COND2 · 3日涨幅 5%~15% · 累计成交额 ≥0.8亿 · 同时段量比 ≥1.3
- 观望：九转低计数 ≥2，或急跌结构（较 3 日前跌 >4% 且跌破 4 日前开盘价）
- 规避：观望 + 较最近一根高计数≥1 的收盘回撤 >7%

判定细节见 classify()。
每只个股每个交易日只记第一次命中；命中后每轮只更新现价与命中后涨幅，用于事后打分。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from ..database import MarketAlertEvent, MarketAlertHit, get_db_ctx
from .duckdb_analytics import connect_analytics_db, safe_float
from .tushare import TushareService


logger = logging.getLogger(__name__)

ENTITY_STOCK = "stock"
ENTITY_SW_L1 = "sw_l1"
ENTITY_SW_L2 = "sw_l2"
SW_ENTITY_BY_LEVEL = {"L1": ENTITY_SW_L1, "L2": ENTITY_SW_L2}

LABEL_STRONG = "强势"
LABEL_ACTIVE = "活跃"
LABEL_WATCH = "观望"
LABEL_AVOID = "规避"
BULLISH_LABELS = (LABEL_STRONG, LABEL_ACTIVE)

# 当日信号覆盖优先级：强势 > 活跃 > 规避 > 观望，只有排名更高的新标签才能覆盖当前标签。
# 即 强势可覆盖其余三个、活跃可覆盖观望与规避、规避可覆盖观望，其余变化一律忽略、不算信号变更。
# 覆盖只在当天内判断：每个交易日各自一行，隔日从头开始，不与前一天比较。
LABEL_OVERRIDE_RANK = {LABEL_WATCH: 0, LABEL_AVOID: 1, LABEL_ACTIVE: 2, LABEL_STRONG: 3}


def can_override(current: Optional[str], new: Optional[str]) -> bool:
    """当日已有标签 current 时，新判定 new 能否覆盖它（只能往更强的方向变）。"""
    if not new or new not in LABEL_OVERRIDE_RANK:
        return False
    if not current or current not in LABEL_OVERRIDE_RANK:
        return True
    return LABEL_OVERRIDE_RANK[new] > LABEL_OVERRIDE_RANK[current]

SCAN_START = dtime(9, 35)
SCAN_END = dtime(15, 5)
LUNCH_START = dtime(11, 31)
LUNCH_END = dtime(13, 0)
SPEED_LOOKBACK_ROUNDS = 5          # 5 分钟涨速：与 5 轮之前的快照比
BASELINE_TRADING_DAYS = 20   # 分析库滚动保留 32 个交易日，20 日基准仍在窗口内
MIN_LISTED_TRADING_DAYS = 60


@dataclass(frozen=True)
class AlertThresholds:
    """全部阈值集中在这里。

    活跃/强势的结构与 3 日涨幅区间、观望公式，是用参考站点（kpan）全市场 4 天共 2.2 万个
    收盘口径标签逐条验证过的（见 research/kpan_label_regression.py），不要随意改；
    量比与成交额门槛是我们自己的量能口径，可按命中后收益调。
    """
    min_amount_yuan: float = 0.8e8        # 成交额下限（参考站点实测硬边界正好 0.8 亿）
    min_volume_ratio: float = 1.3         # 同时段量比下限（我们的量能口径）
    gain3_min_pct: float = 5.0            # 3 日涨幅下限（参考站点所有活跃样本都 > 5%）
    gain3_max_pct: float = 15.0           # 3 日涨幅上限（超过 15% 的不再算活跃，避免追高）
    strong_volume_days: int = 8           # 强势：当日累计量 ≥ 前 N 个交易日的最大日成交量（=通达信 V≥HHV(V,9)）
    watch_td_down_min: int = 2
    avoid_drawdown_pct: float = 7.0   # 规避：较最近一根高计数≥1 的收盘回撤超过该比例
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
    open_ref1: Optional[float] = None                      # 昨日开盘价（活跃 COND1 要求昨日收阳）
    vol_max_prev: Optional[float] = None                   # 前 N 个交易日最大日成交量（股，与实时快照同单位）
    listed_days: int = 0
    is_st: bool = False
    days_since_low9: Optional[int] = None                  # 距上次「低9」的交易日数（仅记录，不参与判定）
    last_up_close: Optional[float] = None                  # 最近一根高计数≥1（C>REF(C,4)）的收盘，规避的回撤参照
    entity_type: str = "stock"                              # stock / sw_l1 / sw_l2

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


def last_up_close(closes: List[float]) -> Optional[float]:
    """最近一根满足 C>REF(C,4)（九转高计数≥1）的 K 线收盘价；窗口内没有则 None。"""
    for i in range(len(closes) - 1, 3, -1):
        if closes[i] > closes[i - 4]:
            return closes[i]
    return None


def _text(value: Any) -> str:
    return "" if value is None or (isinstance(value, float) and pd.isna(value)) else str(value)


def build_structures(connection, today: date, lookback_days: int = 90) -> Dict[str, StockStructure]:
    """从 DuckDB 日线算出每只股票的九转计数基数与参考价（不含当日）。"""
    frame = connection.execute(
        """
        SELECT d.ts_code, d.trade_date, d.close, d.open, d.vol, b.name, b.list_date,
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
            open_ref1=safe_float(group["open"].iloc[-1]),
            vol_max_prev=_max_prev_volume(group["vol"], STOCK_DAILY_VOL_TO_SHARES),
            listed_days=len(closes),
            days_since_low9=since_low9,
            last_up_close=last_up_close(closes),
        )
    return structures


# 日线成交量单位 → 实时快照/分钟线的"股"（均用真实数据核对过）
STOCK_DAILY_VOL_TO_SHARES = 100        # a_stock_market_daily.vol 是手
SW_DAILY_VOL_TO_SHARES = 10_000        # a_stock_sw_daily.vol 是万股


def _max_prev_volume(volumes: pd.Series, to_shares: float,
                     days: int = AlertThresholds.strong_volume_days) -> Optional[float]:
    """前 days 个交易日（不含当日）的最大日成交量，换算成股。"""
    values = [float(v) for v in volumes.tail(days).tolist() if v is not None and not pd.isna(v)]
    return max(values) * to_shares if values else None


BASELINE_TABLES = ("a_stock_minute_bar", "a_stock_sw_minute_bar")


def build_sw_structures(connection, today: date, lookback_days: int = 90) -> Dict[str, StockStructure]:
    """申万一/二级行业指数的结构快照（九转计数、参考价），与个股同一套判定口径。"""
    industries = connection.execute(
        "SELECT index_code, industry_name, industry_code, level, parent_code FROM a_stock_sw_industry "
        "WHERE level IN ('L1', 'L2')"
    ).fetchdf()
    if industries.empty:
        return {}
    name_by_code = dict(zip(industries["industry_code"], industries["industry_name"]))
    meta = {
        str(row.index_code).upper(): (
            row.level,
            str(row.industry_name),
            str(row.industry_name) if row.level == "L1" else str(name_by_code.get(row.parent_code, "")),
        )
        for row in industries.itertuples(index=False)
    }
    frame = connection.execute(
        """
        SELECT ts_code, trade_date, close, open, vol FROM a_stock_sw_daily
        WHERE trade_date >= ? AND trade_date < ?
        ORDER BY ts_code, trade_date
        """,
        [today - timedelta(days=lookback_days * 2), today],
    ).fetchdf()
    structures: Dict[str, StockStructure] = {}
    for ts_code, group in frame.groupby("ts_code"):
        code = str(ts_code).upper()
        if code not in meta:
            continue
        level, name, l1_name = meta[code]
        closes = [float(x) for x in group["close"].tolist() if x is not None and not pd.isna(x)]
        if len(closes) < 10:
            continue
        up, down, since_low9 = td_counts(closes)
        structures[code] = StockStructure(
            ts_code=code,
            name=name,
            industry=l1_name,
            industry_l1=l1_name,
            industry_l2=name if level == "L2" else "",
            td_up_prev=up,
            td_down_prev=down,
            close_ref=closes[-5:],
            open_ref4=safe_float(group["open"].iloc[-4]) if len(group) >= 4 else None,
            open_ref1=safe_float(group["open"].iloc[-1]),
            vol_max_prev=_max_prev_volume(group["vol"], SW_DAILY_VOL_TO_SHARES),
            listed_days=len(closes),
            days_since_low9=since_low9,
            last_up_close=last_up_close(closes),
        )
        structures[code].entity_type = SW_ENTITY_BY_LEVEL[level]
    return structures


def build_volume_baseline(
    connection,
    today: date,
    days: int = BASELINE_TRADING_DAYS,
    table: str = "a_stock_minute_bar",
) -> pd.DataFrame:
    """同时段基准：前 N 个有数据的交易日，每只股票每个时刻的累计成交量均值。

    返回 index=ts_code、columns=HH:MM 的 float32 矩阵（约 5500×241）。
    table 可选个股分钟表或申万分钟表，两者口径完全一致。
    不要摊平成 {(代码, 时刻): 量} 的字典——那样 130 万个元组键要占 200MB 以上内存，
    而矩阵只要几 MB，每轮扫描也只需要取出当前分钟那一列。
    """
    if table not in BASELINE_TABLES:
        raise ValueError(f"不支持的分钟表: {table}")
    dates = [
        row[0] for row in connection.execute(
            f"""
            SELECT DISTINCT CAST(trade_time AS DATE) AS d
            FROM {table}
            WHERE CAST(trade_time AS DATE) < ?
            ORDER BY d DESC LIMIT ?
            """,
            [today, days],
        ).fetchall()
    ]
    if not dates:
        return pd.DataFrame()
    frame = connection.execute(
        f"""
        WITH cum AS (
            SELECT ts_code,
                   CAST(trade_time AS DATE) AS d,
                   strftime(trade_time, '%H:%M') AS t,
                   SUM(vol) OVER (PARTITION BY ts_code, CAST(trade_time AS DATE) ORDER BY trade_time) AS cum_vol
            FROM {table}
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
    cum_volume: Optional[float] = None,
    thresholds: AlertThresholds = DEFAULT_THRESHOLDS,
) -> Optional[str]:
    """按四档标签判定；返回 None 表示当前不打标签。

    价格结构、3 日涨幅区间、强势的量能条件、观望公式都与参考站点（kpan）一致，
    是用其全市场收盘口径标签逐条验证过的；只有活跃的量能门槛是我们自己的同时段量比。

    活跃（下列全部满足）：
      ① 价格结构 COND1 OR COND2（通达信原式）
         COND1 = 九转高计数≥2 AND C>REF(C,1) AND REF(C,1)>REF(C,2) AND C>O AND REF(C,1)>REF(O,1)
         COND2 = C>REF(C,1) AND C>O AND (REF(C,1)<REF(C,5) OR 九转高计数≥2) AND C>REF(C,4) AND 3日涨幅>5%
      ② 5% < 3日涨幅 < 15%            （参考站点所有活跃样本都落在这个区间，含走 COND1 的）
      ③ 累计成交额 ≥ 0.8 亿
      ④ 同时段量比 ≥ 1.3             （我们的量能口径；参考站点这一道在日线上无法精确还原）
    强势：活跃 AND 当日累计量 ≥ 前 8 个交易日最大日成交量（通达信 V≥HHV(V,9)，验证 100%）
    观望：九转低计数≥2，或急跌结构（参考站点公开公式，验证 100%）
    规避：观望 AND 现价较最近一根高计数≥1（C>REF(C,4)）的收盘回撤 >7%（参考站点口径）
    """
    if not price or not pre_close or pre_close <= 0:
        return None
    if structure.is_st or structure.listed_days < MIN_LISTED_TRADING_DAYS:
        return None

    refs = structure.close_ref
    close_ref1 = refs[-1] if len(refs) >= 1 else None
    close_ref2 = refs[-2] if len(refs) >= 2 else None
    close_ref3 = refs[-3] if len(refs) >= 3 else None
    close_ref4 = refs[-4] if len(refs) >= 4 else None
    close_ref5 = refs[-5] if len(refs) >= 5 else None

    td_up = structure.td_up_prev + 1 if close_ref4 and price > close_ref4 else 0
    td_down = structure.td_down_prev + 1 if close_ref4 and price < close_ref4 else 0

    # ---- 多头侧 ----
    if close_ref1 and close_ref3 and day_open:
        gain3 = (price / close_ref3 - 1) * 100
        rising_today = price > close_ref1 and price > day_open                     # 当日上涨且收阳
        cond1 = bool(
            td_up >= 2 and rising_today and close_ref2 and close_ref1 > close_ref2
            and structure.open_ref1 and close_ref1 > structure.open_ref1
        )
        cond2 = bool(
            rising_today and close_ref4 and price > close_ref4 and gain3 > 5.0
            and ((close_ref5 and close_ref1 < close_ref5) or td_up >= 2)
        )
        if ((cond1 or cond2)
                and thresholds.gain3_min_pct < gain3 < thresholds.gain3_max_pct
                and cum_amount >= thresholds.min_amount_yuan
                and volume_ratio is not None and volume_ratio >= thresholds.min_volume_ratio):
            if cum_volume and structure.vol_max_prev and cum_volume >= structure.vol_max_prev:
                return LABEL_STRONG
            return LABEL_ACTIVE

    # ---- 空头侧：九转低计数，或急跌结构（较 3 日前跌超 4% 且跌破 4 日前开盘） ----
    sharp_drop = bool(
        close_ref4 and close_ref3 and structure.open_ref4
        and price < close_ref4 and price < structure.open_ref4 and price < close_ref3
        and (close_ref3 - price) / price * 100 > thresholds.drop_pct_vs_3d
    )
    if td_down >= thresholds.watch_td_down_min or sharp_drop:
        if (structure.last_up_close
                and price < structure.last_up_close * (1 - thresholds.avoid_drawdown_pct / 100)):
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
            structure=structure, cum_volume=cum_vol, thresholds=thresholds,
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
            "entity_type": structure.entity_type,
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


LIMITS_RETRY_SECONDS = 300   # stk_limit 取到空/失败后的重试间隔


class AlertScanner:
    """进程内保存基准表与最近几轮快照，供 1 分钟一轮的定时任务调用。"""

    def __init__(self) -> None:
        self._baseline_date: Optional[date] = None
        self._baseline_days: int = BASELINE_TRADING_DAYS
        self._baseline: pd.DataFrame = pd.DataFrame()
        self._structures: Dict[str, StockStructure] = {}
        self._pct_history: List[Dict[str, float]] = []
        self._recorded: Dict[date, Dict[str, str]] = {}   # {交易日: {代码: 当前标签}}
        self._limits: Dict[str, Tuple[Optional[float], Optional[float]]] = {}
        self._limits_date: Optional[date] = None
        self._limits_retry_at = 0.0          # 取到空/失败后，最早什么时候再试（time.time()）
        # 申万一/二级：与个股同一口径，只是结构来自行业指数日线、基准来自申万分钟线、行情来自 rt_sw_k
        self._sw_structures: Dict[str, StockStructure] = {}
        self._sw_baseline: pd.DataFrame = pd.DataFrame()

    def prepare(self, today: date, connection=None,
                baseline_days: int = BASELINE_TRADING_DAYS) -> Dict[str, Any]:
        """盘前（或当天首轮）构建基准表与结构快照。"""
        own = connection is None
        connection = connection or connect_analytics_db()
        try:
            self._structures = build_structures(connection, today)
            self._baseline = build_volume_baseline(connection, today, days=baseline_days)
            try:
                self._sw_structures = build_sw_structures(connection, today)
                self._sw_baseline = build_volume_baseline(
                    connection, today, days=baseline_days, table="a_stock_sw_minute_bar",
                )
            except Exception as exc:  # noqa: BLE001  申万缺失只影响行业信号，不拖累个股
                logger.warning("申万一二级基准构建失败: %s", exc)
                self._sw_structures, self._sw_baseline = {}, pd.DataFrame()
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
            "sw_structures": len(self._sw_structures),
            "sw_baseline_points": int(self._sw_baseline.size),
            "baseline_days": baseline_days,
        }

    def limits(self, today: date) -> Dict[str, Tuple[Optional[float], Optional[float]]]:
        """当日涨跌停价，每天只取一次 stk_limit。

        盘前还没发布时会取到空：不缓存一整天，但也不每次调用都重打接口
        （行业关联每个请求都会走到这里），空结果/失败后隔 LIMITS_RETRY_SECONDS 再试。
        """
        if self._limits_date == today:
            return self._limits
        if time.time() < self._limits_retry_at:
            return self._limits
        try:
            frame = TushareService.get_instance().get_a_stock_stk_limit_frame(today)
            if frame is not None and not frame.empty:
                self._limits = {
                    str(row.ts_code).strip().upper(): (safe_float(row.up_limit), safe_float(row.down_limit))
                    for row in frame.itertuples(index=False)
                }
                self._limits_date = today
                return self._limits
            logger.warning("tushare stk_limit 返回空（%s），%ss 后重试", today, LIMITS_RETRY_SECONDS)
        except Exception as exc:  # noqa: BLE001  取不到只影响涨停/连板统计
            logger.warning("tushare stk_limit 获取 %s 失败: %s", today, exc)
        self._limits = {}
        self._limits_retry_at = time.time() + LIMITS_RETRY_SECONDS
        return self._limits

    def _recorded_today(self, today: date) -> Dict[str, str]:
        if today not in self._recorded:
            with get_db_ctx() as db:
                rows = db.query(MarketAlertHit.ts_code, MarketAlertHit.label).filter(
                    MarketAlertHit.trade_date == today
                ).all()
            self._recorded = {today: {row[0]: row[1] for row in rows}}
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
        current_pct = _pct_map(quotes)

        # 申万一/二级：rt_sw_k 一次拿全部，口径与个股一致（每轮多 1 次调用）
        sw_hits: List[Dict[str, Any]] = []
        if self._sw_structures:
            try:
                sw_quotes = get_sw_snapshot()
                if sw_quotes is not None and not sw_quotes.empty:
                    if "trade_time" in sw_quotes.columns:
                        sw_quotes = sw_quotes[
                            pd.to_datetime(sw_quotes["trade_time"], errors="coerce").dt.date == today
                        ]
                    sw_quotes = sw_quotes[sw_quotes["ts_code"].isin(self._sw_structures)]
                    sw_hits = evaluate_snapshot(
                        sw_quotes, self._sw_structures, baseline_at(self._sw_baseline, minute_label),
                        minute_label, previous, thresholds,
                    )
                    current_pct.update(_pct_map(sw_quotes))
            except Exception as exc:  # noqa: BLE001  行业信号失败不影响个股
                logger.warning("申万实时行情获取失败: %s", exc)

        self._pct_history.append(current_pct)
        if len(self._pct_history) > SPEED_LOOKBACK_ROUNDS:
            self._pct_history.pop(0)

        written = self._write_hits(today, minute_label, hits + sw_hits)
        return {
            "minute": minute_label,
            "scanned": int(len(quotes)),
            "hits": len(hits),
            "sw_hits": len(sw_hits),
            **written,
        }

    def _write_hits(
        self,
        today: date,
        minute_label: str,
        hits: List[Dict[str, Any]],
    ) -> Dict[str, int]:
        """写入当日状态：新出现的插入；标签只按优先级往更强的方向覆盖，每次覆盖在流水表记一条。

        覆盖规则见 can_override：强势 > 活跃 > 规避 > 观望，只有更强的能覆盖更弱的。
        往弱的方向变（如 活跃→观望、规避→观望、强势→活跃）一律忽略，不改标签、不记流水。
        覆盖只在当天内判断，隔日各自独立。
        命中价/首次命中时刻始终以首次命中为准。现价与命中后涨幅不落库，读取时用最新快照现算
        （见 attach_latest_returns），省掉每分钟对当天全部命中行的写库，读到的也总是最新价。
        """
        recorded = self._recorded_today(today)
        inserted = changed = 0
        with get_db_ctx() as db:
            rows_by_code = {
                row.ts_code: row
                for row in db.query(MarketAlertHit).filter(MarketAlertHit.trade_date == today).all()
            }
            for hit in hits:
                code = hit["ts_code"]
                row = rows_by_code.get(code)
                if row is None:
                    row = MarketAlertHit(
                        trade_date=today,
                        ts_code=code,
                        entity_type=hit.get("entity_type") or ENTITY_STOCK,
                        hit_time=minute_label,
                        last_change_time=minute_label,
                        change_count=0,
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
                    )
                    db.add(row)
                    rows_by_code[code] = row
                    db.add(_event_from_hit(today, minute_label, hit, prev_label=None))
                    inserted += 1
                elif can_override(row.label, hit["label"]):
                    db.add(_event_from_hit(today, minute_label, hit, prev_label=row.label))
                    row.label = hit["label"]
                    row.last_change_time = minute_label
                    row.change_count = (row.change_count or 0) + 1
                    row.score = hit["score"]
                    row.volume_ratio = hit["volume_ratio"]
                    row.td_up = hit["td_up"]
                    row.td_down = hit["td_down"]
                    changed += 1
                recorded[code] = row.label

        return {"recorded": inserted, "changed": changed}


def _pct_map(quotes: pd.DataFrame) -> Dict[str, float]:
    result: Dict[str, float] = {}
    for quote in quotes.itertuples(index=False):
        price = safe_float(getattr(quote, "close", None))
        pre_close = safe_float(getattr(quote, "pre_close", None))
        if price and pre_close:
            result[str(quote.ts_code).strip().upper()] = (price / pre_close - 1) * 100
    return result


def _event_from_hit(today: date, minute_label: str, hit: Dict[str, Any], prev_label: Optional[str]) -> MarketAlertEvent:
    return MarketAlertEvent(
        trade_date=today,
        ts_code=hit["ts_code"],
        entity_type=hit.get("entity_type") or ENTITY_STOCK,
        event_time=minute_label,
        label=hit["label"],
        prev_label=prev_label,
        price=hit["price"],
        pct=hit["pct"],
        volume_ratio=hit["volume_ratio"],
        td_up=hit["td_up"],
        td_down=hit["td_down"],
    )


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


_sw_snapshot_cache: Tuple[float, Optional[pd.DataFrame]] = (0.0, None)


def get_sw_snapshot(force: bool = False) -> Optional[pd.DataFrame]:
    """申万一/二级实时行情（rt_sw_k），30 秒内复用；扫描与读取命中后涨幅共用。"""
    global _sw_snapshot_cache
    cached_at, cached = _sw_snapshot_cache
    if not force and cached is not None and time.time() - cached_at < SNAPSHOT_TTL_SECONDS:
        return cached
    frame = TushareService.get_instance().get_sw_realtime_frame()
    _sw_snapshot_cache = (time.time(), frame)
    return frame


def latest_price_map() -> Dict[str, Tuple[float, date]]:
    """{代码: (最新价, 行情日期)}，个股取 rt_k 快照、申万取 rt_sw_k 快照，都走 30 秒缓存。

    盘后/休市时快照返回的是最近一个交易日的收盘，这正是"最新价"该有的含义。
    """
    prices: Dict[str, Tuple[float, date]] = {}
    for loader in (get_market_snapshot, get_sw_snapshot):
        try:
            frame = loader()
        except Exception as exc:  # noqa: BLE001  取不到最新价时命中后涨幅留空，不影响列表
            logger.warning("读取最新价失败: %s", exc)
            continue
        if frame is None or frame.empty:
            continue
        quote_dates = (
            pd.to_datetime(frame["trade_time"], errors="coerce").dt.date
            if "trade_time" in frame.columns else pd.Series([date.today()] * len(frame), index=frame.index)
        )
        for code, close, quote_date in zip(frame["ts_code"], frame["close"], quote_dates):
            price = safe_float(close)
            if price and quote_date is not None and not pd.isna(quote_date):
                prices[str(code).strip().upper()] = (price, quote_date)
    return prices


_today_adj_cache: Dict[date, Dict[str, float]] = {}


def _today_adj_factors(day: date) -> Dict[str, float]:
    """当天的复权因子：分析库每晚才同步，盘中遇到除权日要向 tushare 取一次当天全市场因子，按天缓存。"""
    if day in _today_adj_cache:
        return _today_adj_cache[day]
    try:
        frame = TushareService.get_instance().get_a_stock_adj_factor_range_frame(day, day)
        factors = {
            str(row.ts_code).strip().upper(): float(row.adj_factor)
            for row in frame.itertuples(index=False) if row.adj_factor
        } if frame is not None and not frame.empty else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("tushare 当日复权因子获取失败: %s", exc)
        factors = {}
    if factors:                    # 取到才缓存，盘前还没发布时下次重试
        _today_adj_cache[day] = factors
    return factors


def load_adj_ratios(codes: List[str], hit_date: date, quote_date: date) -> Dict[str, float]:
    """命中价换算到最新价复权基准的系数：因子(命中日) ÷ 因子(行情日)。

    跨日若中间发生过送转、分红等除权，原始价格不可直接相除（十送十会被当成 −50%）。
    缺因子的股票不换算（系数 1），同一天内本来也不需要换算。
    """
    if not codes or hit_date == quote_date:
        return {}
    connection = connect_analytics_db()
    try:
        placeholders = ",".join("?" for _ in codes)
        rows = connection.execute(
            f"""
            SELECT ts_code, trade_date, adj_factor FROM a_stock_adj_factor
            WHERE ts_code IN ({placeholders}) AND trade_date IN (?, ?)
            """,
            [*codes, hit_date, quote_date],
        ).fetchall()
    finally:
        connection.close()
    at_hit: Dict[str, float] = {}
    at_quote: Dict[str, float] = {}
    for code, trade_date, factor in rows:
        if not factor:
            continue
        target = at_hit if trade_date == hit_date else at_quote
        target[str(code)] = float(factor)
    missing = [code for code in codes if code in at_hit and code not in at_quote]
    if missing and quote_date == date.today():
        today_factors = _today_adj_factors(quote_date)
        for code in missing:
            if code in today_factors:
                at_quote[code] = today_factors[code]
    return {code: at_hit[code] / at_quote[code] for code in at_hit if code in at_quote and at_quote[code]}


def attach_latest_returns(rows: List[Dict[str, Any]], hit_date: date,
                          prices: Optional[Dict[str, Tuple[float, date]]] = None) -> List[Dict[str, Any]]:
    """给命中行补上最新价与命中后涨幅（命中价 → 最新价，跨日按复权换算）。"""
    prices = latest_price_map() if prices is None else prices
    stock_codes_by_date: Dict[date, List[str]] = {}
    for row in rows:
        quote = prices.get(row["ts_code"])
        if quote and (row.get("entity_type") or ENTITY_STOCK) == ENTITY_STOCK and quote[1] != hit_date:
            stock_codes_by_date.setdefault(quote[1], []).append(row["ts_code"])
    ratios: Dict[str, float] = {}
    for quote_date, codes in stock_codes_by_date.items():
        try:
            ratios.update(load_adj_ratios(codes, hit_date, quote_date))
        except Exception as exc:  # noqa: BLE001  取不到复权因子时按原始价计算
            logger.warning("读取复权因子失败，命中后涨幅按原始价计算: %s", exc)

    for row in rows:
        quote = prices.get(row["ts_code"])
        if not quote or not row.get("price"):
            row["last_price"], row["cum_pct"], row["price_date"] = None, None, None
            continue
        price, quote_date = quote
        base = row["price"] * ratios.get(row["ts_code"], 1.0)
        row["last_price"] = round(price, 3)
        row["cum_pct"] = round((price / base - 1) * 100, 2) if base else None
        row["price_date"] = quote_date.isoformat()
    return rows


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
        "entity_type": row.entity_type or ENTITY_STOCK,
        "label": row.label,
        "score": row.score,
        "hit_time": row.hit_time,
        "last_change_time": row.last_change_time or row.hit_time,
        "change_count": row.change_count or 0,
        "price": row.price,
        "pct": row.pct,
        "amount_yi": row.amount_yi,
        "volume_ratio": row.volume_ratio,
        "speed5": row.speed5,
        "td_up": row.td_up,
        "td_down": row.td_down,
        "days_since_low9": row.days_since_low9,
        "last_price": None,          # 读取时由 attach_latest_returns 用最新快照现算
        "cum_pct": None,
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


LABEL_FILTER_VALUES = ("", "none", LABEL_STRONG, LABEL_ACTIVE, LABEL_WATCH, LABEL_AVOID)


def sw_label_maps(rows: List[MarketAlertHit]) -> Tuple[Dict[str, str], Dict[str, str]]:
    """当日申万一/二级信号：{行业名: 最新标签}。个股按所属行业名去查。"""
    l1: Dict[str, str] = {}
    l2: Dict[str, str] = {}
    for row in rows:
        if row.entity_type == ENTITY_SW_L1:
            l1[row.name] = row.label
        elif row.entity_type == ENTITY_SW_L2:
            l2[row.name] = row.label
    return l1, l2


def industry_label_matches(actual: Optional[str], wanted: Optional[str]) -> bool:
    """行业标签过滤：空=不限；none=该行业当日无信号；其余按标签精确匹配。"""
    if not wanted:
        return True
    if wanted == "none":
        return not actual
    return actual == wanted


def fetch_alerts(
    trade_date: Optional[date] = None,
    label: Optional[str] = None,
    level: str = "l1",
    l1_label: Optional[str] = None,
    l2_label: Optional[str] = None,
) -> Dict[str, Any]:
    """读取某个交易日的命中记录与事后统计。

    列表只放个股，每只附上所属申万一级/二级行业当日的最新标签（l1_label / l2_label），
    可以与个股标签组合过滤，例如「个股活跃 + 一级强势 + 二级活跃」。
    """
    with get_db_ctx() as db:
        dates = [
            row[0] for row in db.query(MarketAlertHit.trade_date)
            .distinct().order_by(MarketAlertHit.trade_date.desc()).limit(30).all()
        ]
        target = trade_date or (dates[0] if dates else None)
        rows: List[Dict[str, Any]] = []
        sw_rows: List[Dict[str, Any]] = []
        if target:
            all_rows = db.query(MarketAlertHit).filter(MarketAlertHit.trade_date == target).all()
            l1_map, l2_map = sw_label_maps(all_rows)
            for row in sorted(all_rows, key=lambda item: item.hit_time or "", reverse=True):
                item = _row_to_dict(row)
                if item["entity_type"] != ENTITY_STOCK:
                    sw_rows.append(item)
                    continue
                item["l1_label"] = l1_map.get(item["industry_l1"])
                item["l2_label"] = l2_map.get(item["industry_l2"])
                if label and item["label"] != label:
                    continue
                if not industry_label_matches(item["l1_label"], l1_label):
                    continue
                if not industry_label_matches(item["l2_label"], l2_label):
                    continue
                rows.append(item)
    if target:
        # 命中后涨幅不落库，读取时用最新价现算（统计也基于它）
        attach_latest_returns(rows + sw_rows, target)
    return {
        "date": target.isoformat() if target else None,
        "dates": [d.isoformat() for d in dates],
        "rows": rows,
        # 当日申万一/二级自身的信号，前端用来显示行业信号条
        "sw_signals": sorted(sw_rows, key=lambda item: (item["entity_type"], item["name"] or "")),
        "industry_level": level if level in INDUSTRY_LEVELS else "l1",
        "summary": summarize_hits(rows, level),
        "thresholds": {
            "min_amount_yi": DEFAULT_THRESHOLDS.min_amount_yuan / 1e8,
            "min_volume_ratio": DEFAULT_THRESHOLDS.min_volume_ratio,
            "gain3_min_pct": DEFAULT_THRESHOLDS.gain3_min_pct,
            "gain3_max_pct": DEFAULT_THRESHOLDS.gain3_max_pct,
            "strong_volume_days": DEFAULT_THRESHOLDS.strong_volume_days,
        },
    }

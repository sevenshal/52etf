"""港股情绪数据源：南向资金与市场沽空比率。

两个序列只服务港股指数贪恐的**诊断分项**（不参与总分）：

1. 南向资金净买入：tushare ``moneyflow_hsgt``，按日入库 ``hk_southbound_daily``。
   该接口的 ``south_money`` 字段在 2023-11-24 之前是当日净额（百万元），之后
   改成累计净买入（亿元），这里统一归一化成「亿元/日、正数=净买入」。
2. 市场沽空比率：港交所每日行情页 ``dayquot/d{YYMMDD}e.htm`` 尾部的
   ``Short Selling of Designated Securities (excluding ETP) as % total turnover``。
   官方只保留约两个月，所以靠每日增量累积；早期日期天然缺失。

两个来源都不稳定属于「尽力而为」：失败只记一条日志，不让定时任务整体失败。
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Dict, Optional

import pandas as pd
import requests

from ..database import HKSouthboundDaily, HKShortSellingDaily, Session

LOGGER = logging.getLogger(__name__)

# tushare 从这一天起把 south_money 改成累计净买入（亿元）；之前是当日净额（百万元）。
SOUTHBOUND_CUMULATIVE_START = date(2023, 11, 24)
# 换口径当天的差分没有意义（前一日还是另一种口径），丢掉。
SOUTHBOUND_SWITCH_GAP_DAYS = 5

DAYQUOT_URL = "https://www.hkex.com.hk/eng/stat/smstat/dayquot/d{stamp}e.htm"
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Referer": "https://www.hkex.com.hk/",
}

SHORT_RATIO_RE = re.compile(
    r"Short Selling of Designated Securities \(excluding ETP\) as % total turnover\s*:\s*([\d.]+)%"
)
SHORT_VALUE_RE = re.compile(r"Short Selling Turnover Total Value \(\$\)\s*:\s*HKD\s+([\d,]+)")
MARKET_TURNOVER_RE = re.compile(r"Total market turnover\s*:\s*HKD\s+([\d,]+)")
# 页面很大，沽空汇总在末尾，只取最后一段即可。
TAIL_BYTES = 200_000


def normalize_southbound_net(frame: pd.DataFrame) -> pd.Series:
    """把 ``south_money`` 归一化成「亿元/日」，正数代表净买入。

    ``frame`` 需要 ``trade_date``（YYYYMMDD 字符串）与 ``south_money`` 两列。
    2023-11-24 之前是当日净额、单位百万元，除以 100；之后是累计值，取差分。
    """
    if frame.empty:
        return pd.Series(dtype=float)
    data = frame.copy()
    data["trade_date"] = pd.to_datetime(data["trade_date"], format="%Y%m%d")
    data = data.sort_values("trade_date")
    index = pd.DatetimeIndex(data["trade_date"], name="date")
    # 统一用日期索引，后面按布尔掩码赋值才不会因为索引错位变成空值。
    levels = pd.Series(
        pd.to_numeric(data["south_money"], errors="coerce").to_numpy(),
        index=index,
        dtype=float,
    )

    cumulative = index >= pd.Timestamp(SOUTHBOUND_CUMULATIVE_START)
    net = pd.Series(index=index, dtype=float)
    net[cumulative] = levels[cumulative].diff()
    net[~cumulative] = levels[~cumulative] / 100.0

    gap_end = pd.Timestamp(SOUTHBOUND_CUMULATIVE_START) + timedelta(days=SOUTHBOUND_SWITCH_GAP_DAYS)
    net.loc[(index >= pd.Timestamp(SOUTHBOUND_CUMULATIVE_START)) & (index <= gap_end)] = float("nan")
    return net


def fetch_southbound(start_date: date, end_date: date) -> pd.DataFrame:
    """按年分段拉 tushare 南向资金；没有配置 token 时抛 ValueError。"""
    from .tushare_account import get_tushare_token_for_runtime

    token = (get_tushare_token_for_runtime() or "").strip()
    if not token:
        raise ValueError("Tushare token 未配置，无法同步南向资金")
    import tushare as ts

    pro = ts.pro_api(token)
    frames = []
    for year in range(start_date.year, end_date.year + 1):
        frame = pro.moneyflow_hsgt(
            start_date=f"{year}0101", end_date=f"{year}1231"
        )
        if frame is not None and not frame.empty:
            frames.append(frame)
    if not frames:
        return pd.DataFrame()
    merged = pd.concat(frames).drop_duplicates("trade_date").sort_values("trade_date")
    return merged[
        (merged["trade_date"] >= start_date.strftime("%Y%m%d"))
        & (merged["trade_date"] <= end_date.strftime("%Y%m%d"))
    ]


def sync_southbound(start_date: date, end_date: date) -> Dict[str, object]:
    frame = fetch_southbound(start_date, end_date)
    if frame.empty:
        return {"saved": 0, "start_date": start_date.isoformat(), "end_date": end_date.isoformat()}
    net = normalize_southbound_net(frame)
    db = Session()
    saved = 0
    try:
        for row in frame.itertuples(index=False):
            day = datetime.strptime(str(row.trade_date), "%Y%m%d").date()
            value = net.get(pd.Timestamp(day))
            db.merge(
                HKSouthboundDaily(
                    date=day,
                    south_net=None if value is None or pd.isna(value) else round(float(value), 4),
                    ggt_ss=float(row.ggt_ss) if pd.notna(row.ggt_ss) else None,
                    ggt_sz=float(row.ggt_sz) if pd.notna(row.ggt_sz) else None,
                    source="tushare.moneyflow_hsgt",
                    updated_at=datetime.now(),
                )
            )
            saved += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        Session.remove()
    return {
        "saved": saved,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
    }


def parse_short_selling_summary(text: str) -> Optional[Dict[str, float]]:
    """从港交所每日行情页解析市场沽空汇总；非交易日返回 None。"""
    ratio = SHORT_RATIO_RE.search(text)
    if not ratio:
        return None
    short_value = SHORT_VALUE_RE.search(text)
    turnover = MARKET_TURNOVER_RE.search(text)
    return {
        "short_ratio": float(ratio.group(1)) / 100.0,
        "short_turnover": (
            float(short_value.group(1).replace(",", "")) if short_value else None
        ),
        "market_turnover": float(turnover.group(1).replace(",", "")) if turnover else None,
    }


def fetch_short_selling(day: date) -> Optional[Dict[str, float]]:
    url = DAYQUOT_URL.format(stamp=day.strftime("%y%m%d"))
    try:
        response = requests.get(
            url, headers={**HTTP_HEADERS, "Range": f"bytes=-{TAIL_BYTES}"}, timeout=30
        )
    except Exception as exc:  # noqa: BLE001 - 网络问题不该中断整批
        LOGGER.warning("short selling page failed %s: %s", day, exc)
        return None
    if response.status_code not in (200, 206):
        return None
    return parse_short_selling_summary(response.content.decode("latin-1", errors="ignore"))


def sync_short_selling(start_date: date, end_date: date) -> Dict[str, object]:
    db = Session()
    saved = 0
    missing = 0
    try:
        day = start_date
        while day <= end_date:
            if day.weekday() < 5:
                summary = fetch_short_selling(day)
                if summary is None:
                    missing += 1
                else:
                    db.merge(
                        HKShortSellingDaily(
                            date=day,
                            short_turnover=summary["short_turnover"],
                            market_turnover=summary["market_turnover"],
                            short_ratio=round(summary["short_ratio"], 6),
                            updated_at=datetime.now(),
                        )
                    )
                    saved += 1
            day += timedelta(days=1)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        Session.remove()
    return {
        "saved": saved,
        "missing": missing,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
    }


def load_southbound_net(start_date: date, end_date: date) -> pd.Series:
    db = Session()
    try:
        rows = (
            db.query(HKSouthboundDaily.date, HKSouthboundDaily.south_net)
            .filter(
                HKSouthboundDaily.date >= start_date,
                HKSouthboundDaily.date <= end_date,
            )
            .order_by(HKSouthboundDaily.date)
            .all()
        )
    finally:
        Session.remove()
    if not rows:
        return pd.Series(dtype=float)
    index = pd.DatetimeIndex([row[0] for row in rows], name="date")
    return pd.Series([row[1] for row in rows], index=index, dtype=float)


def load_short_selling_ratio(start_date: date, end_date: date) -> pd.Series:
    db = Session()
    try:
        rows = (
            db.query(HKShortSellingDaily.date, HKShortSellingDaily.short_ratio)
            .filter(
                HKShortSellingDaily.date >= start_date,
                HKShortSellingDaily.date <= end_date,
            )
            .order_by(HKShortSellingDaily.date)
            .all()
        )
    finally:
        Session.remove()
    if not rows:
        return pd.Series(dtype=float)
    index = pd.DatetimeIndex([row[0] for row in rows], name="date")
    return pd.Series([row[1] for row in rows], index=index, dtype=float)

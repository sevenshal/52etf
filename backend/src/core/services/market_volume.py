"""沪深两市分时成交额对比（缩放量）。

数据源：东方财富分时接口 trends2（ndays=5），上证指数 1.000001 成交额 + 深证成指 0.399001 成交额
= 沪深两市 A 股成交额。每行格式：``时间,开,收,高,低,成交量,成交额,均价``。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

import requests


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
TRENDS_URLS = (
    "https://push2his.eastmoney.com/api/qt/stock/trends2/get",
    "https://push2.eastmoney.com/api/qt/stock/trends2/get",
)
SH_SECID = "1.000001"
SZ_SECID = "0.399001"
TRENDS_NDAYS = 5
FULL_DAY_MINUTES = 241  # 09:30 集合竞价 + 上午 120 + 下午 120
CACHE_TTL_SECONDS = 20
YI = 1e8

_cache: Dict[str, Tuple[float, Dict[str, List[Tuple[str, float, float]]]]] = {}


class MarketVolumeDataError(RuntimeError):
    pass


def _safe_float(value: Any) -> Optional[float]:
    if value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_trends_lines(lines: List[str]) -> Dict[str, List[Tuple[str, float, float]]]:
    """按交易日分组：{date: [(HH:MM, close, amount), ...]}，保持时间顺序。"""
    by_date: Dict[str, List[Tuple[str, float, float]]] = {}
    for line in lines or []:
        parts = str(line).split(",")
        if len(parts) < 7 or " " not in parts[0]:
            continue
        date, minute = parts[0].split(" ", 1)
        close = _safe_float(parts[2])
        amount = _safe_float(parts[6])
        if close is None or amount is None:
            continue
        by_date.setdefault(date, []).append((minute, close, amount))
    return by_date


def _fetch_trends(secid: str) -> Dict[str, List[Tuple[str, float, float]]]:
    cached = _cache.get(secid)
    now = time.time()
    if cached and now - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "ndays": TRENDS_NDAYS,
        "iscr": 0,
    }
    last_error: Optional[Exception] = None
    for url in (url for url in TRENDS_URLS for _ in range(2)):
        try:
            response = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=15)
            response.raise_for_status()
            lines = ((response.json() or {}).get("data") or {}).get("trends") or []
            parsed = parse_trends_lines(lines)
            if parsed:
                _cache[secid] = (now, parsed)
                return parsed
            last_error = MarketVolumeDataError(f"{secid} 分时数据为空")
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
    raise MarketVolumeDataError(f"东方财富分时数据获取失败({secid}): {last_error}")


def _yi(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value / YI, 2)


def build_volume_compare(
    sh: Dict[str, List[Tuple[str, float, float]]],
    sz: Dict[str, List[Tuple[str, float, float]]],
    target_date: Optional[str] = None,
    compare_date: Optional[str] = None,
) -> Dict[str, Any]:
    dates = sorted(set(sh) & set(sz))
    if len(dates) < 2:
        raise MarketVolumeDataError("分时数据不足两个交易日，无法对比")

    # 目标日需要有前一交易日（用于对比量与涨跌幅基准），所以最早一天不能作为目标日
    selectable = dates[1:]
    target = target_date or selectable[-1]
    if target not in selectable:
        raise ValueError(f"目标日期不可选：{target}，可选 {', '.join(selectable)}")
    target_index = dates.index(target)
    compare = compare_date or dates[target_index - 1]
    if compare not in dates or compare == target:
        raise ValueError(f"对比日期不可选：{compare}")

    prev_date = dates[target_index - 1]
    sh_prev_close = sh[prev_date][-1][1]
    sz_prev_close = sz[prev_date][-1][1]

    def _amount_map(date: str) -> Dict[str, float]:
        sz_amount = {minute: amount for minute, _, amount in sz[date]}
        return {minute: amount + sz_amount[minute] for minute, _, amount in sh[date] if minute in sz_amount}

    target_amounts = _amount_map(target)
    compare_amounts = _amount_map(compare)
    sh_close = {minute: close for minute, close, _ in sh[target]}
    sz_close = {minute: close for minute, close, _ in sz[target]}

    grid = sorted(set(compare_amounts) | set(target_amounts))
    points: List[Dict[str, Any]] = []
    target_cum = 0.0
    compare_cum = 0.0
    last_time = None
    compare_same_time_cum = 0.0
    for minute in grid:
        target_amount = target_amounts.get(minute)
        compare_amount = compare_amounts.get(minute)
        if compare_amount is not None:
            compare_cum += compare_amount
        has_target = target_amount is not None
        if has_target:
            target_cum += target_amount
            last_time = minute
            compare_same_time_cum = compare_cum
        deviation = None
        if has_target and compare_amount:
            deviation = round((target_amount / compare_amount - 1) * 100, 2)
        sh_pct = (sh_close[minute] / sh_prev_close - 1) * 100 if minute in sh_close and sh_prev_close else None
        sz_pct = (sz_close[minute] / sz_prev_close - 1) * 100 if minute in sz_close and sz_prev_close else None
        points.append({
            "time": minute,
            "target_amount": _yi(target_amount),
            "compare_amount": _yi(compare_amount),
            "target_cum": _yi(target_cum) if has_target else None,
            "compare_cum": _yi(compare_cum) if compare_amount is not None else None,
            "diff_cum": _yi(target_cum - compare_cum) if has_target else None,
            "deviation_pct": deviation,
            "sh_pct": None if sh_pct is None else round(sh_pct, 3),
            "sz_pct": None if sz_pct is None else round(sz_pct, 3),
        })

    diff = target_cum - compare_same_time_cum
    return {
        "source": "eastmoney trends2（上证指数 + 深证成指成交额）",
        "dates": dates,
        "selectable_dates": list(reversed(selectable)),
        "target_date": target,
        "compare_date": compare,
        "is_intraday": len(target_amounts) < FULL_DAY_MINUTES,
        "last_time": last_time,
        "target_total": _yi(target_cum),
        "compare_same_time_total": _yi(compare_same_time_cum),
        "compare_full_total": _yi(compare_cum),
        "diff": _yi(diff),
        "diff_pct": round(diff / compare_same_time_cum * 100, 2) if compare_same_time_cum else None,
        "points": points,
    }


def fetch_intraday_volume_compare(
    target_date: Optional[str] = None,
    compare_date: Optional[str] = None,
) -> Dict[str, Any]:
    sh = _fetch_trends(SH_SECID)
    sz = _fetch_trends(SZ_SECID)
    result = build_volume_compare(sh, sz, target_date=target_date, compare_date=compare_date)
    result["fetched_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return result

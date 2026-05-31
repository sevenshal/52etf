from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
import re
import time

import requests


UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
EASTMONEY_CLIST_URL = "http://push2.eastmoney.com/api/qt/clist/get"
EASTMONEY_STOCK_FLOW_MINUTE_URL = "http://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
EASTMONEY_STOCK_FLOW_DAILY_URL = "http://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
HSGT_URL = "https://data.hexin.cn/market/hsgtApi/method/dayChart/"
HSGT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "Chrome/117.0.0.0 Safari/537.36"
    ),
    "Host": "data.hexin.cn",
    "Referer": "https://data.hexin.cn/",
}

RANK_FIELDS = (
    "f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87,f124"
)
INDUSTRY_FIELDS = (
    "f12,f14,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87,"
    "f104,f105,f128,f140,f136,f124"
)
STOCK_CODE_PATTERN = re.compile(r"^\d{6}$")


class FundFlowDataError(RuntimeError):
    pass


def normalize_stock_code(value: str) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        raise ValueError("股票代码不能为空")

    if "." in raw:
        raw = raw.split(".", 1)[0]
    if raw.startswith(("SH", "SZ", "BJ")):
        raw = raw[2:]

    if not STOCK_CODE_PATTERN.match(raw):
        raise ValueError("股票代码必须是 6 位数字，支持 600519、600519.SH、SH600519")
    return raw


def _secid_for_code(code: str) -> str:
    market = 1 if code.startswith(("6", "9")) else 0
    return f"{market}.{code}"


def _safe_float(value: Any) -> Optional[float]:
    if value is None or value == "" or value == "-":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> Optional[int]:
    if value is None or value == "" or value == "-":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _safe_round(value: Any, digits: int = 2) -> Optional[float]:
    number = _safe_float(value)
    if number is None:
        return None
    return round(number, digits)


def _format_epoch(value: Any) -> Optional[str]:
    epoch = _safe_int(value)
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch).isoformat(timespec="seconds")


def _request_json(url: str, *, params: Optional[Dict[str, Any]] = None,
                  headers: Optional[Dict[str, str]] = None, timeout: int = 15) -> Dict[str, Any]:
    last_request_error: Optional[requests.RequestException] = None
    for attempt in range(2):
        try:
            response = requests.get(url, params=params, headers=headers or {"User-Agent": UA}, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            last_request_error = exc
            if attempt == 0:
                time.sleep(0.25)
                continue
        except ValueError as exc:
            raise FundFlowDataError("数据源返回了无法解析的 JSON") from exc
    raise FundFlowDataError(f"数据源请求失败: {last_request_error}")


def _parse_rank_item(row: Dict[str, Any], rank: int, *, include_leader: bool = False) -> Dict[str, Any]:
    item = {
        "rank": rank,
        "code": row.get("f12") or "",
        "name": row.get("f14") or "",
        "price": _safe_float(row.get("f2")),
        "change_pct": _safe_round(row.get("f3")),
        "main_net": _safe_float(row.get("f62")),
        "main_net_pct": _safe_round(row.get("f184")),
        "super_net": _safe_float(row.get("f66")),
        "super_net_pct": _safe_round(row.get("f69")),
        "large_net": _safe_float(row.get("f72")),
        "large_net_pct": _safe_round(row.get("f75")),
        "mid_net": _safe_float(row.get("f78")),
        "mid_net_pct": _safe_round(row.get("f81")),
        "small_net": _safe_float(row.get("f84")),
        "small_net_pct": _safe_round(row.get("f87")),
        "updated_at": _format_epoch(row.get("f124")),
    }
    if include_leader:
        item.update({
            "up_count": _safe_int(row.get("f104")),
            "down_count": _safe_int(row.get("f105")),
            "leader": row.get("f128") or "",
            "leader_code": row.get("f140") or "",
            "leader_change_pct": _safe_round(row.get("f136")),
        })
    return item


def _fetch_rank(fs: str, limit: int, direction: str, *, fields: str,
                include_leader: bool = False) -> Dict[str, Any]:
    page_size = min(max(int(limit), 1), 100)
    return _fetch_rank_page(
        fs,
        page_number=1,
        page_size=page_size,
        direction=direction,
        fields=fields,
        include_leader=include_leader,
    )


def _fetch_rank_page(
    fs: str,
    *,
    page_number: int,
    page_size: int,
    direction: str,
    fields: str,
    include_leader: bool = False,
) -> Dict[str, Any]:
    po = "0" if direction == "outflow" else "1"
    params = {
        "pn": str(page_number),
        "pz": str(page_size),
        "po": po,
        "np": "1",
        "fltt": "2",
        "invt": "2",
        "fid": "f62",
        "fs": fs,
        "fields": fields,
    }
    payload = _request_json(EASTMONEY_CLIST_URL, params=params)
    data = payload.get("data") or {}
    rows = data.get("diff") or []
    return {
        "total": _safe_int(data.get("total")) or len(rows),
        "items": [
            _parse_rank_item(row, (page_number - 1) * page_size + index + 1, include_leader=include_leader)
            for index, row in enumerate(rows)
        ],
    }


def fetch_market_rank(limit: int = 30, direction: str = "inflow") -> Dict[str, Any]:
    return _fetch_rank(
        "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
        limit,
        direction,
        fields=RANK_FIELDS,
    )


def fetch_market_rank_all(direction: str = "inflow", page_size: int = 100) -> Dict[str, Any]:
    fs = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
    safe_page_size = min(max(int(page_size), 1), 100)
    first_page = _fetch_rank_page(
        fs,
        page_number=1,
        page_size=safe_page_size,
        direction=direction,
        fields=RANK_FIELDS,
    )
    items = list(first_page["items"])
    total = int(first_page.get("total") or len(items))
    page_count = (total + safe_page_size - 1) // safe_page_size
    for page_number in range(2, page_count + 1):
        page = _fetch_rank_page(
            fs,
            page_number=page_number,
            page_size=safe_page_size,
            direction=direction,
            fields=RANK_FIELDS,
        )
        page_items = page.get("items") or []
        if not page_items:
            break
        items.extend(page_items)
    return {"total": total, "items": items}


def fetch_industry_rank(limit: int = 30, direction: str = "inflow") -> Dict[str, Any]:
    return _fetch_rank(
        "m:90+t:2",
        limit,
        direction,
        fields=INDUSTRY_FIELDS,
        include_leader=True,
    )


def _parse_hsgt_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    times = payload.get("time") or []
    hgt = payload.get("hgt") or []
    sgt = payload.get("sgt") or []
    points: List[Dict[str, Any]] = []

    for index, time_label in enumerate(times):
        hgt_value = _safe_float(hgt[index]) if index < len(hgt) else None
        sgt_value = _safe_float(sgt[index]) if index < len(sgt) else None
        total = None
        if hgt_value is not None and sgt_value is not None:
            total = hgt_value + sgt_value
        points.append({
            "time": time_label,
            "hgt_yi": hgt_value,
            "sgt_yi": sgt_value,
            "total_yi": total,
        })

    latest = next((point for point in reversed(points) if point["total_yi"] is not None), None)
    return {
        "points": points,
        "latest": latest,
        "unit": "亿元",
    }


def fetch_northbound_realtime() -> Dict[str, Any]:
    payload = _request_json(HSGT_URL, headers=HSGT_HEADERS, timeout=10)
    return _parse_hsgt_payload(payload)


def _parse_stock_flow_lines(lines: List[str], *, daily: bool = False) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for line in lines:
        parts = str(line).split(",")
        if len(parts) < 6:
            continue
        row = {
            "date" if daily else "time": parts[0],
            "main_net": _safe_float(parts[1]),
            "small_net": _safe_float(parts[2]),
            "mid_net": _safe_float(parts[3]),
            "large_net": _safe_float(parts[4]),
            "super_net": _safe_float(parts[5]),
        }
        if daily:
            row.update({
                "main_net_pct": _safe_round(parts[6]) if len(parts) > 6 else None,
                "small_net_pct": _safe_round(parts[7]) if len(parts) > 7 else None,
                "mid_net_pct": _safe_round(parts[8]) if len(parts) > 8 else None,
                "large_net_pct": _safe_round(parts[9]) if len(parts) > 9 else None,
                "super_net_pct": _safe_round(parts[10]) if len(parts) > 10 else None,
                "close": _safe_float(parts[11]) if len(parts) > 11 else None,
                "change_pct": _safe_round(parts[12]) if len(parts) > 12 else None,
            })
        rows.append(row)
    return rows


def fetch_stock_fund_flow(code: str, daily_limit: int = 60) -> Dict[str, Any]:
    stock_code = normalize_stock_code(code)
    secid = _secid_for_code(stock_code)

    minute_payload = _request_json(
        EASTMONEY_STOCK_FLOW_MINUTE_URL,
        params={
            "secid": secid,
            "klt": 1,
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57",
        },
        timeout=10,
    )
    daily_payload = _request_json(
        EASTMONEY_STOCK_FLOW_DAILY_URL,
        params={
            "secid": secid,
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
            "lmt": str(daily_limit),
        },
        timeout=15,
    )

    minute_data = minute_payload.get("data") or {}
    daily_data = daily_payload.get("data") or {}
    minute = _parse_stock_flow_lines(minute_data.get("klines") or [], daily=False)
    daily = _parse_stock_flow_lines(daily_data.get("klines") or [], daily=True)

    recent_5 = daily[-5:]
    recent_20 = daily[-20:]
    latest_minute = minute[-1] if minute else None
    latest_daily = daily[-1] if daily else None
    return {
        "code": stock_code,
        "name": minute_data.get("name") or daily_data.get("name") or "",
        "minute": minute,
        "daily": daily,
        "summary": {
            "latest_time": latest_minute.get("time") if latest_minute else None,
            "latest_date": latest_daily.get("date") if latest_daily else None,
            "latest_main_net": (
                latest_minute.get("main_net") if latest_minute else
                (latest_daily.get("main_net") if latest_daily else None)
            ),
            "latest_super_net": (
                latest_minute.get("super_net") if latest_minute else
                (latest_daily.get("super_net") if latest_daily else None)
            ),
            "recent_5_main_net": sum(row.get("main_net") or 0 for row in recent_5),
            "recent_20_main_net": sum(row.get("main_net") or 0 for row in recent_20),
        },
    }


def fetch_stock_fund_flow_daily(code: str, daily_limit: int = 120) -> Dict[str, Any]:
    stock_code = normalize_stock_code(code)
    secid = _secid_for_code(stock_code)
    payload = _request_json(
        EASTMONEY_STOCK_FLOW_DAILY_URL,
        params={
            "secid": secid,
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
            "lmt": str(min(max(int(daily_limit), 1), 120)),
        },
        timeout=15,
    )
    data = payload.get("data") or {}
    return {
        "code": stock_code,
        "name": data.get("name") or "",
        "daily": _parse_stock_flow_lines(data.get("klines") or [], daily=True),
    }


def fetch_fund_flow_dashboard(limit: int = 30, stock_code: Optional[str] = None) -> Dict[str, Any]:
    errors: List[Dict[str, str]] = []

    def capture(section: str, func, fallback):
        try:
            return func()
        except Exception as exc:
            errors.append({"section": section, "message": str(exc)})
            return fallback

    market_inflow = capture("market_inflow", lambda: fetch_market_rank(limit, "inflow"), {"total": 0, "items": []})
    market_outflow = capture("market_outflow", lambda: fetch_market_rank(limit, "outflow"), {"total": 0, "items": []})
    industry_inflow = capture("industry_inflow", lambda: fetch_industry_rank(limit, "inflow"), {"total": 0, "items": []})
    industry_outflow = capture("industry_outflow", lambda: fetch_industry_rank(limit, "outflow"), {"total": 0, "items": []})
    northbound = capture("northbound", fetch_northbound_realtime, {"points": [], "latest": None, "unit": "亿元"})
    stock = None
    if stock_code:
        stock = capture("stock", lambda: fetch_stock_fund_flow(stock_code), None)

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": {
            "market_rank": "eastmoney_push2",
            "industry_rank": "eastmoney_push2",
            "stock_fund_flow": "eastmoney_push2",
            "northbound": "hexin_hsgt",
        },
        "unit": {
            "fund_flow": "元",
            "northbound": "亿元",
        },
        "market_rank": {
            "inflow": market_inflow,
            "outflow": market_outflow,
        },
        "industry_rank": {
            "inflow": industry_inflow,
            "outflow": industry_outflow,
        },
        "northbound": northbound,
        "stock": stock,
        "errors": errors,
        "notes": [
            "主力资金为数据源按成交单规模估算的指标，不是交易所官方口径。",
            "非交易日或盘后会返回最近一个有数据的交易日。",
        ],
    }

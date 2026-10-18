"""港交所（HKEX）指数期权 / ETF 期权的日度数据。

数据全部来自港交所官方网站，免费：

1. 每日市场报告（htm，保留约一年）
   ``https://www.hkex.com.hk/eng/stat/dmstat/dayrpt/{hsio|hhio|htio}{YYMMDD}.htm``
   - 每档行权价的成交量、未平仓合约、IV%；
   - 页脚直接给出 ``MARKET PUT/CALL RATIO``（成交量口径）与市场总成交/总持仓。

2. 每日结算统计 DTOP（zip，历史可回溯到 2019 年）
   ``https://www.hkex.com.hk/eng/stat/dmstat/oi/DTOP_F_YYYYMMDD.zip``（HSI / HHI / MHI 指数期权）
   ``https://www.hkex.com.hk/eng/stat/dmstat/oi/DTOP_O_YYYYMMDD.zip``（股票 / ETF 期权）
   - 按到期日、行权价给出 call/put 的毛持仓、成交量、结算价；
   - **不含 IV**，需要用 Black-76（假设 r=q、远期=现货）从结算价反解；
   - 只下 ``*_opt_dtl_all.rpt``（单文件含全部标的，含恒生科技 HTI）；
     按产品拆分的 ``*_opt_dtl_hti.rpt`` 并不存在，读 all 才不会漏。

港股只有少数 ETF 有期权（2800 盈富等），流动性远低于指数期权，所以贪恐打分用指数期权
（恒指 / 恒生科技），ETF 期权只入库备查。
"""

from __future__ import annotations

import io
import logging
import math
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd
import requests
from sqlalchemy import text

from ..analytics_database import AnalyticsSession
from ..database import HKOptionDaily, Session

HKEX_REPORT_BASE = "https://www.hkex.com.hk/eng/stat/dmstat/dayrpt"
HKEX_DTOP_BASE = "https://www.hkex.com.hk/eng/stat/dmstat/oi"
HKEX_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)


@dataclass(frozen=True)
class HKOptionProduct:
    key: str
    label: str
    index_code: str
    report: str
    dtop_underlying: str


HK_OPTION_PRODUCTS: Dict[str, HKOptionProduct] = {
    "hsio": HKOptionProduct(
        key="hsio",
        label="恒生指数期权",
        index_code="HSI",
        report="hsio",
        dtop_underlying="HSI",
    ),
    "hhio": HKOptionProduct(
        key="hhio",
        label="国企指数期权",
        index_code="HSCEI",
        report="hhio",
        dtop_underlying="HHI",
    ),
    "htio": HKOptionProduct(
        key="htio",
        label="恒生科技指数期权",
        index_code="HSTECH",
        report="htio",
        dtop_underlying="HTI",
    ),
}

# 港股指数期权标的 → 期权的对应关系。中证香港创新药没有本指数期权市场，
# 用恒生科技指数期权做代理（创新药权重的 beta 更接近成长/科技，而不是恒指的金融地产）。
HK_FEAR_TARGET_OPTION_PRODUCT: Dict[str, str] = {
    "HSI.HK": "hsio",
    "HSTECH.HK": "htio",
    "931250.CSI": "htio",
}

# 港股有期权的 ETF：目前只有个位数，且成交极薄。key 是 DTOP 里的 HKATS code。
# 2800 盈富基金（TRF）是唯一还有一定成交的，其余只作占位，未入库就不展示。
HK_OPTION_ETF_PRODUCTS: Dict[str, Dict[str, str]] = {
    "trf": {"symbol": "2800.HK", "label": "盈富基金期权"},
}


@dataclass
class OptionSeries:
    month: str
    strike: float
    option_type: str
    volume: int = 0
    open_interest: int = 0
    iv: Optional[float] = None  # 百分数，15 表示 15%
    settle: Optional[float] = None


@dataclass
class OptionDaySummary:
    product: str
    trade_date: date
    call_volume: int
    put_volume: int
    call_open_interest: int
    put_open_interest: int
    reported_put_call_ratio: Optional[float] = None
    atm_iv: Optional[float] = None
    atm_iv_source: Optional[str] = None
    atm_strike: Optional[float] = None
    active_month: Optional[str] = None
    underlying_close: Optional[float] = None
    series: List[OptionSeries] = field(default_factory=list)

    @property
    def total_volume(self) -> int:
        return self.call_volume + self.put_volume

    @property
    def total_open_interest(self) -> int:
        return self.call_open_interest + self.put_open_interest

    @property
    def put_call_volume_ratio(self) -> Optional[float]:
        if self.call_volume <= 0:
            return None
        return self.put_volume / self.call_volume

    @property
    def put_call_open_interest_ratio(self) -> Optional[float]:
        if self.call_open_interest <= 0:
            return None
        return self.put_open_interest / self.call_open_interest


_MARKET_PCR_RE = re.compile(r"MARKET PUT/CALL RATIO\s+([\d.]+|-+)")
_MARKET_TOTAL_RE = re.compile(r"MARKET TOTAL\s+([\d,]+)\s+([\d,]+)\s*([+-][\d,]+)?")
_MONTH_ROW_RE = re.compile(r"^([A-Z]{3}-\d{2})\s+([\d,]+)\s+([CP])\s+(.+)$")
_DTOP_UNDERLYING_RE = re.compile(r"^UNDERLYING\s*:\s*(\S+)")
_DTOP_MARKET_RE = re.compile(r"^MARKET\s*:\s*(\S+)")
_DTOP_EXPIRY_RE = re.compile(r"^EXPIRATION DATE\s*:\s*(\d{2})\s+([A-Z]{3})\s+(\d{2})")


def _to_int(value) -> int:
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0


def _to_float(value) -> Optional[float]:
    text = str(value or "").replace(",", "").strip()
    if not text or text in {"-", "+", "N/A"}:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def parse_market_report(text: str, product: str = "") -> Optional[OptionDaySummary]:
    """解析港交所「每日市场报告」htm，返回当日的 PUT/CALL 汇总与全部分档行。

    行格式（日期块内）::

        OCT-26  23400 C    ...after-hours...  |  ...day session...  |  high low volume OI chg
                                                                    ^ day: opening high low OQP close chg IV% volume
    """
    if not text:
        return None
    series: List[OptionSeries] = []
    reported_pcr = None
    market_total: Optional[Tuple[int, int]] = None

    for line in text.splitlines():
        if not reported_pcr:
            match = _MARKET_PCR_RE.search(line)
            if match and match.group(1) not in {"-", ""}:
                reported_pcr = _to_float(match.group(1))
                continue
        if market_total is None:
            match = _MARKET_TOTAL_RE.search(line)
            if match:
                market_total = (_to_int(match.group(1)), _to_int(match.group(2)))
                continue
        match = _MONTH_ROW_RE.match(line.strip("\r"))
        if not match:
            continue
        blocks = [block.strip() for block in match.group(4).split("|")]
        if len(blocks) < 3:
            continue
        day_tokens = blocks[1].split()
        combined_tokens = blocks[2].split()
        if len(combined_tokens) < 4:
            continue
        series.append(
            OptionSeries(
                month=match.group(1),
                strike=_to_float(match.group(2)) or 0.0,
                option_type=match.group(3),
                volume=_to_int(combined_tokens[2]),
                open_interest=_to_int(combined_tokens[3]),
                iv=_to_float(day_tokens[5]) if len(day_tokens) >= 7 else None,
                settle=_to_float(day_tokens[3]) if len(day_tokens) >= 7 else None,
            )
        )

    if not series:
        return None
    call_volume = sum(item.volume for item in series if item.option_type == "C")
    put_volume = sum(item.volume for item in series if item.option_type == "P")
    call_oi = sum(item.open_interest for item in series if item.option_type == "C")
    put_oi = sum(item.open_interest for item in series if item.option_type == "P")
    if market_total:
        # 官方汇总优先，避免个别行解析漏掉时总量偏小。
        call_volume, put_volume = _split_market_total(market_total[0], put_volume, call_volume)
        call_oi, put_oi = _split_market_total(market_total[1], put_oi, call_oi)
    return OptionDaySummary(
        product=product,
        trade_date=date.today(),
        call_volume=call_volume,
        put_volume=put_volume,
        call_open_interest=call_oi,
        put_open_interest=put_oi,
        reported_put_call_ratio=reported_pcr,
        series=series,
    )


def _split_market_total(total: int, put_value: int, call_value: int) -> Tuple[int, int]:
    """官方给的是市场合计；按解析出的 call/put 比例拆回去，比例缺失时对半分。"""
    if total <= 0:
        return call_value, put_value
    parsed = call_value + put_value
    if parsed <= 0:
        return total, 0
    if abs(parsed - total) <= max(2, total * 0.02):
        return call_value, put_value
    call_share = call_value / parsed
    call = int(round(total * call_share))
    return call, max(total - call, 0)


def _month_expiry(month_label: str, trade_date: date) -> date:
    """``OCT-26`` → 2026 年 10 月的到期日（当月最后一个工作日的前一个工作日）。"""
    try:
        month = datetime.strptime(month_label.split("-")[0], "%b").month
        year = 2000 + int(month_label.split("-")[1])
    except (ValueError, IndexError):
        return trade_date
    last = _last_business_day(year, month)
    return _previous_business_day(last)


def _last_business_day(year: int, month: int) -> date:
    if month == 12:
        cursor = date(year, 12, 31)
    else:
        cursor = date(year, month + 1, 1) - timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor -= timedelta(days=1)
    return cursor


def _previous_business_day(day: date) -> date:
    cursor = day - timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor -= timedelta(days=1)
    return cursor


def _norm_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def black76_price(forward: float, strike: float, years: float, vol: float, is_call: bool) -> float:
    """Black-76。港股指数期权按 r=q 处理，远期取现货。"""
    intrinsic = max(forward - strike, 0.0) if is_call else max(strike - forward, 0.0)
    if years <= 0 or vol <= 0 or forward <= 0 or strike <= 0:
        return intrinsic
    root_t = math.sqrt(years)
    d1 = (math.log(forward / strike) + 0.5 * vol * vol * years) / (vol * root_t)
    d2 = d1 - vol * root_t
    if is_call:
        return forward * _norm_cdf(d1) - strike * _norm_cdf(d2)
    return strike * _norm_cdf(-d2) - forward * _norm_cdf(-d1)


def implied_vol(
    price: Optional[float],
    forward: float,
    strike: float,
    years: float,
    is_call: bool,
    low: float = 1e-4,
    high: float = 5.0,
) -> Optional[float]:
    """从期权价格反解隐含波动率（小数，0.25 = 25%）。

    价格低于内在价值或高于 high 档理论价时返回 None —— 这种报价通常来自
    流动性极差的档位或结算价四舍五入，硬解会给出夸张的波动率。
    """
    if price is None or price <= 0 or forward <= 0 or strike <= 0 or years <= 0:
        return None
    intrinsic = max(forward - strike, 0.0) if is_call else max(strike - forward, 0.0)
    if price <= intrinsic + 1e-8:
        return None
    if black76_price(forward, strike, years, high, is_call) < price:
        return None
    low_value = black76_price(forward, strike, years, low, is_call)
    if low_value > price:
        return low
    left, right = low, high
    for _ in range(100):
        middle = (left + right) / 2.0
        if black76_price(forward, strike, years, middle, is_call) < price:
            left = middle
        else:
            right = middle
        if right - left < 1e-6:
            break
    return (left + right) / 2.0


def atm_iv(
    summary: OptionDaySummary,
    spot: Optional[float],
    trade_date: Optional[date] = None,
) -> Tuple[Optional[float], Optional[float], Optional[str], Optional[str]]:
    """取成交最活跃月份里最接近平值的那一档，返回年化 IV（百分数）。

    优先用报告自带的 IV%；全为 0（很多档位不报 IV）时用结算价反解。
    """
    if not spot or not summary.series:
        return None, None, None, None
    trade_date = trade_date or summary.trade_date
    by_month: Dict[str, List[OptionSeries]] = {}
    for item in summary.series:
        by_month.setdefault(item.month, []).append(item)
    month, rows = max(by_month.items(), key=lambda pair: sum(x.volume for x in pair[1]))
    strike = min({item.strike for item in rows}, key=lambda value: abs(value - spot))
    picks = [item for item in rows if item.strike == strike]
    reported = [item.iv for item in picks if item.iv and item.iv > 0]
    if reported:
        return sum(reported) / len(reported), strike, month, "report"
    years = max((_month_expiry(month, trade_date) - trade_date).days, 1) / 365.0
    inverted = [
        vol * 100.0
        for vol in (
            implied_vol(item.settle, spot, item.strike, years, item.option_type == "C")
            for item in picks
        )
        if vol
    ]
    if inverted:
        return sum(inverted) / len(inverted), strike, month, "inverted"
    return None, strike, month, None


def parse_dtop_report(text: str) -> Dict[str, Dict[str, object]]:
    """解析 DTOP 期权明细（by market），按标的汇总成交/持仓并保留分档结算价。"""
    result: Dict[str, Dict[str, object]] = {}
    underlying: Optional[str] = None
    expiry: Optional[date] = None

    for raw_line in text.splitlines():
        line = raw_line.strip("\r")
        match = _DTOP_UNDERLYING_RE.match(line)
        if match:
            underlying = match.group(1)
            result.setdefault(underlying, _empty_dtop_bucket())
            continue
        if underlying is None:
            continue
        match = _DTOP_EXPIRY_RE.match(line)
        if match:
            expiry = _parse_dtop_expiry(match.group(1), match.group(2), match.group(3))
            continue
        tokens = line.split()
        if len(tokens) != 15 or expiry is None:
            continue
        strike = _to_float(tokens[0])
        if strike is None:
            continue
        bucket = result[underlying]
        bucket["call_volume"] += _to_int(tokens[4])
        bucket["put_volume"] += _to_int(tokens[11])
        bucket["call_open_interest"] += _to_int(tokens[1])
        bucket["put_open_interest"] += _to_int(tokens[8])
        bucket["series"].append(
            {
                "expiry": expiry,
                "strike": strike,
                "call_settle": _to_float(tokens[6]),
                "put_settle": _to_float(tokens[13]),
                "call_volume": _to_int(tokens[4]),
                "put_volume": _to_int(tokens[11]),
            }
        )
    return result


def _empty_dtop_bucket() -> Dict[str, object]:
    return {
        "call_volume": 0,
        "put_volume": 0,
        "call_open_interest": 0,
        "put_open_interest": 0,
        "series": [],
    }


def _parse_dtop_expiry(day: str, month: str, year: str) -> Optional[date]:
    try:
        return datetime.strptime(f"{day}{month}{year}", "%d%b%y").date()
    except ValueError:
        return None


def dtop_atm_iv(bucket: Dict[str, object], spot: Optional[float], trade_date: date) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    """DTOP 没有 IV：用最近月份里最接近平值的一档结算价反解。"""
    if not spot or not bucket.get("series"):
        return None, None, None
    series = [item for item in bucket["series"] if item["expiry"] and item["expiry"] >= trade_date]
    if not series:
        series = list(bucket["series"])
    front = min(item["expiry"] for item in series)
    rows = [item for item in series if item["expiry"] == front]
    strike = min({item["strike"] for item in rows}, key=lambda value: abs(value - spot))
    years = max((front - trade_date).days, 1) / 365.0
    values = []
    for item in rows:
        if item["strike"] != strike:
            continue
        for price_key, is_call in (("call_settle", True), ("put_settle", False)):
            vol = implied_vol(item[price_key], spot, strike, years, is_call)
            if vol:
                values.append(vol * 100.0)
    if not values:
        return None, strike, None
    return sum(values) / len(values), strike, "inverted"


class HKOptionDailySync:
    """把港交所期权日报写进 ``hk_option_daily``。"""

    def __init__(
        self,
        session=None,
        timeout: float = 30.0,
        include_etf: bool = True,
        allow_dtop_backfill: bool = True,
    ):
        self.logger = logging.getLogger(self.__class__.__name__)
        self._external_session = session is not None
        self.db = session or Session()
        self.timeout = timeout
        self.include_etf = include_etf
        self.allow_dtop_backfill = allow_dtop_backfill

    def close(self):
        if not self._external_session:
            self.db.close()
            Session.remove()

    # ---------------------------------------------------------------- 网络

    def _fetch_text(self, url: str) -> Optional[str]:
        response = requests.get(
            url,
            timeout=self.timeout,
            headers={"User-Agent": HKEX_USER_AGENT, "Referer": "https://www.hkex.com.hk/"},
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.content.decode("latin-1", errors="ignore")

    def _fetch_zip_texts(self, url: str, suffixes: Sequence[str]) -> Dict[str, str]:
        response = requests.get(
            url,
            timeout=max(self.timeout, 60.0),
            headers={"User-Agent": HKEX_USER_AGENT, "Referer": "https://www.hkex.com.hk/"},
        )
        if response.status_code == 404:
            return {}
        response.raise_for_status()
        texts: Dict[str, str] = {}
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            for name in archive.namelist():
                if not name.lower().endswith(".rpt"):
                    continue
                if not any(name.lower().endswith(suffix) for suffix in suffixes):
                    continue
                texts[name] = archive.read(name).decode("latin-1", errors="ignore")
        return texts

    # ---------------------------------------------------------------- 写入

    def _upsert(self, summary: OptionDaySummary) -> None:
        record = (
            self.db.query(HKOptionDaily)
            .filter(
                HKOptionDaily.product == summary.product,
                HKOptionDaily.date == summary.trade_date,
            )
            .first()
        )
        values = {
            "call_volume": summary.call_volume,
            "put_volume": summary.put_volume,
            "total_volume": summary.total_volume,
            "put_call_volume_ratio": _round(summary.put_call_volume_ratio, 6),
            "call_open_interest": summary.call_open_interest,
            "put_open_interest": summary.put_open_interest,
            "total_open_interest": summary.total_open_interest,
            "put_call_open_interest_ratio": _round(summary.put_call_open_interest_ratio, 6),
            "atm_iv": _round(summary.atm_iv, 4),
            "atm_iv_source": summary.atm_iv_source,
            "atm_strike": summary.atm_strike,
            "active_month": summary.active_month,
            "underlying_close": _round(summary.underlying_close, 6),
            "reported_put_call_ratio": _round(summary.reported_put_call_ratio, 6),
            "updated_at": datetime.now(),
        }
        if record is None:
            self.db.add(
                HKOptionDaily(
                    product=summary.product,
                    date=summary.trade_date,
                    created_at=datetime.now(),
                    **values,
                )
            )
        else:
            for key, value in values.items():
                setattr(record, key, value)

    # ---------------------------------------------------------------- 同步

    def sync_range(self, start_date: date, end_date: date) -> Dict[str, object]:
        days = 0
        saved = 0
        skipped = 0
        errors: List[Dict[str, str]] = []
        cursor = start_date
        while cursor <= end_date:
            if cursor.weekday() >= 5:
                cursor += timedelta(days=1)
                continue
            days += 1
            try:
                result = self.sync_date(cursor)
                saved += result["saved"]
                if not result["trading_day"]:
                    skipped += 1
            except Exception as exc:  # noqa: BLE001 - 网络/解析异常按天隔离
                self.db.rollback()
                errors.append({"date": cursor.isoformat(), "error": str(exc)})
                self.logger.warning("HK option sync failed for %s: %s", cursor, exc, exc_info=True)
            cursor += timedelta(days=1)
        self.db.commit()
        return {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "weekday_count": days,
            "saved": saved,
            "non_trading_days": skipped,
            "errors": errors,
        }

    def sync_date(self, trade_date: date) -> Dict[str, object]:
        saved = 0
        products: Dict[str, str] = {}
        ivs: Dict[str, Optional[float]] = {}
        missing: List[str] = []
        spot = self._index_closes(trade_date)

        for key, product in HK_OPTION_PRODUCTS.items():
            url = f"{HKEX_REPORT_BASE}/{product.report}{trade_date:%y%m%d}.htm"
            text = self._fetch_text(url)
            if text is None:
                missing.append(key)
                continue
            summary = parse_market_report(text, key)
            if summary is None:
                missing.append(key)
                continue
            summary.trade_date = trade_date
            summary.underlying_close = spot.get(product.index_code)
            (
                summary.atm_iv,
                summary.atm_strike,
                summary.active_month,
                summary.atm_iv_source,
            ) = atm_iv(summary, summary.underlying_close, trade_date)
            self._upsert(summary)
            products[key] = "report"
            ivs[key] = summary.atm_iv
            saved += 1

        if missing and self.allow_dtop_backfill:
            saved += self._backfill_from_dtop(trade_date, missing, spot, products, ivs)
        if self.include_etf:
            saved += self._sync_etf_from_dtop(trade_date)
        self.db.commit()
        return {
            "date": trade_date.isoformat(),
            "saved": saved,
            "trading_day": bool(products),
            "products": products,
            "atm_iv": {key: _round(value, 2) for key, value in ivs.items()},
        }

    def _backfill_from_dtop(
        self,
        trade_date: date,
        missing: Iterable[str],
        spot: Dict[str, Optional[float]],
        products: Dict[str, str],
        ivs: Dict[str, Optional[float]],
    ) -> int:
        """htm 报告只保留约一年，更早的日期用 DTOP 结算统计 + 反解 IV 回补。"""
        wanted = {HK_OPTION_PRODUCTS[key].dtop_underlying: key for key in missing if key in HK_OPTION_PRODUCTS}
        if not wanted:
            return 0
        url = f"{HKEX_DTOP_BASE}/DTOP_F_{trade_date:%Y%m%d}.zip"
        try:
            texts = self._fetch_zip_texts(url, ("_opt_dtl_all.rpt",))
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("DTOP index-option download failed for %s: %s", trade_date, exc)
            return 0
        saved = 0
        covered = set()
        for text in texts.values():
            buckets = parse_dtop_report(text)
            for underlying, key in wanted.items():
                bucket = buckets.get(underlying)
                if not bucket or not bucket["call_volume"] and not bucket["put_volume"]:
                    continue
                covered.add(key)
                product = HK_OPTION_PRODUCTS[key]
                summary = OptionDaySummary(
                    product=key,
                    trade_date=trade_date,
                    call_volume=int(bucket["call_volume"]),
                    put_volume=int(bucket["put_volume"]),
                    call_open_interest=int(bucket["call_open_interest"]),
                    put_open_interest=int(bucket["put_open_interest"]),
                )
                summary.underlying_close = spot.get(product.index_code)
                iv, strike, source = dtop_atm_iv(bucket, summary.underlying_close, trade_date)
                summary.atm_iv = iv
                summary.atm_strike = strike
                summary.atm_iv_source = source
                self._upsert(summary)
                products[key] = "dtop"
                ivs[key] = summary.atm_iv
                saved += 1
        # 官方 all 文件里个别标的（例如 HTI）没有单产品文件，缺数据时记一笔便于排查。
        for key in set(wanted.values()) - covered:
            self.logger.info(
                "DTOP %s has no %s option detail for %s; product %s left unfilled",
                url.rsplit("/", 1)[-1],
                HK_OPTION_PRODUCTS[key].dtop_underlying,
                trade_date,
                key,
            )
        return saved

    def _sync_etf_from_dtop(self, trade_date: date) -> int:
        """ETF 期权（目前只有 2800 盈富）：DTOP_O 里按标的拆好的明细。"""
        suffixes = tuple(f"_opt_dtl_{key}.rpt" for key in HK_OPTION_ETF_PRODUCTS)
        url = f"{HKEX_DTOP_BASE}/DTOP_O_{trade_date:%Y%m%d}.zip"
        try:
            texts = self._fetch_zip_texts(url, suffixes)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("DTOP stock-option download failed for %s: %s", trade_date, exc)
            return 0
        saved = 0
        for name, text in texts.items():
            key = name.rsplit("_opt_dtl_", 1)[-1].replace(".rpt", "")
            if key not in HK_OPTION_ETF_PRODUCTS:
                continue
            buckets = parse_dtop_report(text)
            for underlying, bucket in buckets.items():
                if underlying != key.upper() or not (bucket["call_volume"] or bucket["put_volume"]):
                    continue
                self._upsert(
                    OptionDaySummary(
                        product=key,
                        trade_date=trade_date,
                        call_volume=int(bucket["call_volume"]),
                        put_volume=int(bucket["put_volume"]),
                        call_open_interest=int(bucket["call_open_interest"]),
                        put_open_interest=int(bucket["put_open_interest"]),
                    )
                )
                saved += 1
        return saved

    def _index_closes(self, trade_date: date) -> Dict[str, Optional[float]]:
        codes = sorted({product.index_code for product in HK_OPTION_PRODUCTS.values()})
        params = {"trade_date": trade_date}
        placeholders = []
        for index, code in enumerate(codes):
            params[f"c{index}"] = code
            placeholders.append(f":c{index}")
        db = AnalyticsSession()
        try:
            rows = db.execute(
                text(
                    f"""
                    SELECT ts_code, close FROM hk_index_daily
                    WHERE trade_date = :trade_date AND ts_code IN ({", ".join(placeholders)})
                    """
                ),
                params,
            ).fetchall()
        except Exception:  # noqa: BLE001 - 分析库缺失时退化为不做 IV 反解
            return {}
        finally:
            AnalyticsSession.remove()
        return {str(code): float(close) for code, close in rows if close is not None}


def _round(value: Optional[float], digits: int) -> Optional[float]:
    if value is None or not math.isfinite(float(value)):
        return None
    return round(float(value), digits)


def load_option_pcr_series(product: str, start_date: date, end_date: date) -> pd.Series:
    """读某产品在区间内的成交量口径 PUT/CALL 比率（缺失日为空）。"""
    db = Session()
    try:
        rows = (
            db.query(HKOptionDaily.date, HKOptionDaily.put_call_volume_ratio)
            .filter(
                HKOptionDaily.product == product,
                HKOptionDaily.date >= start_date,
                HKOptionDaily.date <= end_date,
            )
            .order_by(HKOptionDaily.date)
            .all()
        )
    finally:
        Session.remove()
    if not rows:
        return pd.Series(dtype=float)
    index = pd.DatetimeIndex([row[0] for row in rows], name="date")
    return pd.Series([row[1] for row in rows], index=index, dtype=float)

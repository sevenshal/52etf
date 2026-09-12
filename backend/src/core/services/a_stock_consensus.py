from __future__ import annotations

import re
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Dict, Iterable, List, Mapping, NamedTuple, Optional, Sequence, Tuple

from sqlalchemy import text


A_STOCK_MARKET_CAP_UNIT = 10_000.0
FLOAT_COMPARE_EPSILON = 1e-9

# 列表按估值状态筛选：fresh = 只看 T 池(最新)，stale = 只看退到 T-1 池的(待更新)。
STALE_FILTER_FRESH = "fresh"
STALE_FILTER_STALE = "stale"

# 研报候选池：先用 T 期(最新一份已披露定期报告)披露日之后的研报；给出目标价的机构
# 不足 MIN_POOL_ORGANIZATIONS 家时退到 T-1 期披露日之后，最多退到 T-1，再没有就不估值。
CONSENSUS_POOL_T = "T"
CONSENSUS_POOL_T1 = "T-1"
MIN_POOL_ORGANIZATIONS = 2
# 估值展开的财年：当前财年、下财年、下下财年。
FORECAST_HORIZON_LABELS = ("当前财年", "下财年", "下下财年")
# 取研报时往回拉的天数：T-1 期披露日最远约在 7 个月前，留足余量。
REPORT_FETCH_LOOKBACK_DAYS = 400
# 取财报披露日时往回拉的天数：要能覆盖最早一个交易日的 T、T-1 两期。
DISCLOSURE_FETCH_LOOKBACK_DAYS = 800
# 复权因子比研报多往前取几天，保证最早一篇研报的前一交易日也有因子。
PRICE_FACTOR_PADDING_DAYS = 10
# 指数估值里成分股缺估值时沿用上一次估值的最长天数，超过视为无人覆盖。
VALUATION_FFILL_MAX_DAYS = 365

# 前瞻 PE 通道(可开关的"历史估值法")：没给目标价的机构，用该股近 3 年每天
# 股价 ÷ 当时一致预期未来 12 个月 EPS 的 20% / 80% 分位 × 这家机构的未来 12 个月 EPS 估值。
VALUATION_METHOD_TARGET_PRICE = "target_price"
VALUATION_METHOD_PE_BAND = "pe_band"
PE_BAND_AVAILABLE = "available"
PE_BAND_WINDOW_DAYS = 365 * 3
PE_BAND_LOW_PERCENTILE = 0.2
PE_BAND_HIGH_PERCENTILE = 0.8
# 至少一年的有效样本；窗口内有正的前瞻 EPS 的交易日要占九成以上(否则多是亏损或覆盖断档)。
PE_BAND_MIN_DAYS = 250
PE_BAND_MIN_VALID_RATIO = 0.9
# 80 分位超过 20 分位的 3 倍，说明估值体系本身不稳定(周期、困境反转)，通道不可信。
PE_BAND_MAX_WIDTH = 3.0
# 逐日回放历史时，每隔这么多个交易日重算一次通道(通道本身变化很慢)。
PE_BAND_REFRESH_EVERY_DAYS = 5

# 同一家券商在数据源里的别名：2026-08 起数据源改用全称，同一篇研报会挂在两个机构名下。
# 只收同一法人主体的改名/简称；合并前的券商(如国泰君安与海通证券)在合并前仍是两家。
ORG_NAME_ALIASES = {
    "中金公司": "中金",
    "国泰海通证券": "国泰海通",
    "中信建投证券": "中信建投",
    "申万宏源研究": "申万宏源证券",
    "中银证券": "中银国际",
    "安信证券": "国投证券",
}

_FISCAL_YEAR_QUARTER_PATTERN = re.compile(r"(\d{4})Q4")
_PERIOD_LABEL_SUFFIX = {(3, 31): "一季报", (6, 30): "半年报", (9, 30): "三季报", (12, 31): "年报"}


def normalize_a_stock_symbol(symbol: Optional[str]) -> str:
    raw = str(symbol or "").strip().upper()
    if not raw:
        return ""
    if "." in raw:
        code, suffix = raw.split(".", 1)
        return f"{code}.{suffix}"
    code = re.sub(r"\D", "", raw)
    if not code:
        return raw
    if code.startswith(("43", "83", "87", "88", "92")):
        return f"{code}.BJ"
    if code.startswith(("6", "9")):
        return f"{code}.SH"
    return f"{code}.SZ"


def _looks_like_a_stock_code(value: Optional[str]) -> bool:
    raw = str(value or "").strip().upper()
    if not raw:
        return False
    return bool(re.fullmatch(r"\d{6}(?:\.(?:SH|SZ|BJ))?", raw))


def _safe_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _positive_float(value: Any) -> Optional[float]:
    number = _safe_float(value)
    if number is None or number <= 0:
        return None
    return number


def _avg(values: Iterable[Optional[float]]) -> Optional[float]:
    clean = [value for value in values if value is not None]
    if not clean:
        return None
    return sum(clean) / len(clean)


def _percentile(values: Iterable[Optional[float]], percentile: float) -> Optional[float]:
    clean = sorted(value for value in values if value is not None)
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    position = (len(clean) - 1) * percentile
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(clean) - 1)
    fraction = position - lower_index
    return clean[lower_index] + (clean[upper_index] - clean[lower_index]) * fraction


def _date_to_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _row_to_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)).date()
    except (TypeError, ValueError):
        return None


def _growth_pct(current_value: Optional[float], next_value: Optional[float]) -> Optional[float]:
    if current_value is None or next_value is None or current_value <= 0:
        return None
    return (next_value / current_value - 1.0) * 100.0


def _target_price_bounds(row: Mapping[str, Any]) -> Optional[Tuple[float, float]]:
    """返回单篇研报自洽的目标价 (下沿, 上沿)。

    Tushare report_rc 的 min_price/max_price 经常只填一边(点目标价或数据缺失)，
    单边填充时用另一边兜底，保证同一篇研报的下沿/上沿始终成对出现。否则按缺失
    字段分别取 min/max 会让区间落在不同的研报子集上，出现"均值不在区间内"。
    """
    min_price = _positive_float(row.get("min_price"))
    max_price = _positive_float(row.get("max_price"))
    low = min_price if min_price is not None else max_price
    high = max_price if max_price is not None else min_price
    if low is None or high is None:
        return None
    return (low, high) if low <= high else (high, low)


def _normalize_org_name(value: Any) -> str:
    name = str(value or "").strip()
    return ORG_NAME_ALIASES.get(name, name)


def _parse_forecast_fiscal_year(value: Any) -> Optional[int]:
    """研报 quarter 字段只认全年预测 YYYYQ4。

    Q1-Q3 是中报/季报期的预测(比如上半年 EPS)，和全年口径不可比；NULL、"Q"、
    "2026Q5" 之类的脏数据直接丢弃。
    """
    match = _FISCAL_YEAR_QUARTER_PATTERN.fullmatch(str(value or "").strip().upper())
    return int(match.group(1)) if match else None


def _period_label(period: Optional[date]) -> Optional[str]:
    if period is None:
        return None
    suffix = _PERIOD_LABEL_SUFFIX.get((period.month, period.day))
    return f"{period.year}{suffix}" if suffix else period.isoformat()


class DisclosureCutoffs(NamedTuple):
    """某一时点可见的财报截面。

    T 期 = 截至当天已披露的最新一份定期报告(按报告期)；T-1 期 = 披露日严格早于
    T 期披露日的最近一次披露。近半数 A 股公司的年报和次年一季报同一天披露，若按
    报告期取"上一期"，T-1 与 T 的披露日相同，退到 T-1 池子等于没退。
    """

    t_period: Optional[date] = None
    t_date: Optional[date] = None
    t1_period: Optional[date] = None
    t1_date: Optional[date] = None


def _disclosure_cutoffs_as_of(
    disclosures: Sequence[Tuple[date, date]],
    as_of: Optional[date],
) -> DisclosureCutoffs:
    """disclosures 为 [(披露日, 报告期末)]，按披露日升序；只看 as_of 当天已披露的。"""
    if not disclosures or as_of is None:
        return DisclosureCutoffs()
    visible = disclosures[:bisect_right(disclosures, (as_of, date.max))]
    if not visible:
        return DisclosureCutoffs()
    t_date, t_period = max(visible, key=lambda item: (item[1], item[0]))
    earlier_dates = [disclosed for disclosed, _ in visible if disclosed < t_date]
    if not earlier_dates:
        return DisclosureCutoffs(t_period, t_date)
    t1_date = max(earlier_dates)
    t1_period = max(period for disclosed, period in visible if disclosed == t1_date)
    return DisclosureCutoffs(t_period, t_date, t1_period, t1_date)


def load_a_stock_disclosure_dates(
    db: Any,
    symbols: Sequence[str],
    *,
    since: Optional[date] = None,
) -> Dict[str, List[Tuple[date, date]]]:
    """按股票加载定期报告的首次披露日，返回 [(披露日, 报告期末)]，按披露日升序。

    ann_date 是首次公告日；f_ann_date 是更正/重述后的日期，永远不早于 ann_date，
    不能当成"市场第一次看到这份财报"的时点。同一报告期有多条记录时取最早的公告日。
    """
    normalized_symbols = list(dict.fromkeys(symbol for symbol in symbols if symbol))
    if not normalized_symbols:
        return {}
    since_filter = "AND end_date >= :since" if since is not None else ""
    disclosures: Dict[str, List[Tuple[date, date]]] = defaultdict(list)
    for offset in range(0, len(normalized_symbols), 500):
        chunk = normalized_symbols[offset:offset + 500]
        symbol_params = {f"symbol_{index}": symbol for index, symbol in enumerate(chunk)}
        placeholders = ",".join(f":{key}" for key in symbol_params)
        rows = db.execute(
            text(
                f"""
                SELECT ts_code, end_date, MIN(ann_date) AS ann_date
                FROM a_stock_income
                WHERE ts_code IN ({placeholders})
                  AND ann_date IS NOT NULL
                  AND end_date IS NOT NULL
                  {since_filter}
                GROUP BY ts_code, end_date
                """
            ),
            {**symbol_params, "since": since},
        ).mappings().all()
        for row in rows:
            symbol = str(row.get("ts_code") or "").strip().upper()
            disclosed = _row_to_date(row.get("ann_date"))
            period = _row_to_date(row.get("end_date"))
            if symbol and disclosed is not None and period is not None:
                disclosures[symbol].append((disclosed, period))
    return {symbol: sorted(set(values)) for symbol, values in disclosures.items()}


class _PriceFactors(NamedTuple):
    """复权因子的阶梯序列(只保留变化点)，用来把不同时点的价格换算到同一口径。

    tushare 的 adj_factor 在每次除权(送转、分红)后变大，原始价格 × 复权因子 即后复权价，
    所以某天的原始价格 P 换算到 basis 那天的口径 = P × 因子(那天) / 因子(basis)。
    """

    dates: List[date]
    values: List[float]

    def on_or_before(self, day: Optional[date]) -> Optional[float]:
        if day is None:
            return None
        index = bisect_right(self.dates, day)
        return self.values[index - 1] if index else None

    @property
    def latest(self) -> Optional[float]:
        return self.values[-1] if self.values else None


def _price_scale(source_factor: Optional[float], basis_factor: Optional[float]) -> float:
    """source 时点的价格换算到 basis 时点口径的系数；缺复权因子时不换算。"""
    if source_factor is None or basis_factor is None:
        return 1.0
    return source_factor / basis_factor


def load_a_stock_price_factors(
    db: Any,
    symbols: Sequence[str],
    *,
    start: date,
    end: Optional[date] = None,
) -> Dict[str, _PriceFactors]:
    """按股票加载复权因子，只取变化点(区间第一天 + 每次除权当天)。"""
    normalized_symbols = list(dict.fromkeys(symbol for symbol in symbols if symbol))
    if not normalized_symbols:
        return {}
    end_filter = "AND trade_date <= :end" if end is not None else ""
    points: Dict[str, List[Tuple[date, float]]] = defaultdict(list)
    for offset in range(0, len(normalized_symbols), 500):
        chunk = normalized_symbols[offset:offset + 500]
        symbol_params = {f"symbol_{index}": symbol for index, symbol in enumerate(chunk)}
        placeholders = ",".join(f":{key}" for key in symbol_params)
        rows = db.execute(
            text(
                f"""
                SELECT ts_code, trade_date, adj_factor
                FROM (
                    SELECT
                        ts_code,
                        trade_date,
                        adj_factor,
                        LAG(adj_factor) OVER (PARTITION BY ts_code ORDER BY trade_date) AS previous_factor
                    FROM a_stock_adj_factor
                    WHERE ts_code IN ({placeholders})
                      AND trade_date >= :start
                      {end_filter}
                      AND adj_factor > 0
                )
                WHERE previous_factor IS NULL OR adj_factor <> previous_factor
                ORDER BY ts_code, trade_date
                """
            ),
            {**symbol_params, "start": start, "end": end},
        ).mappings().all()
        for row in rows:
            symbol = str(row.get("ts_code") or "").strip().upper()
            day = _row_to_date(row.get("trade_date"))
            factor = _positive_float(row.get("adj_factor"))
            if symbol and day is not None and factor is not None:
                points[symbol].append((day, factor))
    result: Dict[str, _PriceFactors] = {}
    for symbol, values in points.items():
        values.sort()
        result[symbol] = _PriceFactors([day for day, _ in values], [factor for _, factor in values])
    return result


class _ForecastRow(NamedTuple):
    report_date: date
    org_name: str
    raw_org_name: str
    author_name: str
    report_title: str
    order_key: str
    quarter: str
    fiscal_year: int
    eps: Optional[float]
    np: Optional[float]
    pe: Optional[float]
    rating: str
    target_low: Optional[float]
    target_high: Optional[float]
    # 写研报时(前一交易日收盘)的复权因子；目标价、EPS 都是按这个股价口径给的。
    price_factor: Optional[float] = None

    @property
    def report_key(self) -> Tuple[date, str, str, str]:
        return (self.report_date, self.org_name, self.author_name, self.report_title)

    @property
    def recency(self) -> Tuple[date, str]:
        return (self.report_date, self.order_key)


def _normalize_forecast_rows(
    rows: Iterable[Mapping[str, Any]],
    price_factors: Optional[_PriceFactors] = None,
) -> List[_ForecastRow]:
    """把研报明细行规整成全年预测记录，按研报日期升序。

    丢掉：quarter 不是 YYYYQ4 的行、没有机构名或研报日期的行。机构名先按别名归一，
    后面的机构计数和"同机构取最新"才不会被同一家券商的两个名字骗过。研报通常引用
    发布前一交易日的收盘价，所以记下那天的复权因子，供之后按除权换算。
    """
    records: List[_ForecastRow] = []
    for row in rows:
        report_date = _row_to_date(row.get("report_date"))
        fiscal_year = _parse_forecast_fiscal_year(row.get("quarter"))
        raw_org_name = str(row.get("org_name") or "").strip()
        if report_date is None or fiscal_year is None or not raw_org_name:
            continue
        bounds = _target_price_bounds(row)
        records.append(_ForecastRow(
            report_date=report_date,
            org_name=_normalize_org_name(raw_org_name),
            raw_org_name=raw_org_name,
            author_name=str(row.get("author_name") or "").strip(),
            report_title=str(row.get("report_title") or "").strip(),
            order_key=_date_to_iso(row.get("create_time")) or "",
            quarter=f"{fiscal_year}Q4",
            fiscal_year=fiscal_year,
            eps=_positive_float(row.get("eps")),
            np=_positive_float(row.get("np")),
            pe=_positive_float(row.get("pe")),
            rating=str(row.get("rating") or "").strip(),
            target_low=bounds[0] if bounds else None,
            target_high=bounds[1] if bounds else None,
            price_factor=(
                price_factors.on_or_before(report_date - timedelta(days=1)) if price_factors else None
            ),
        ))
    records.sort(key=lambda record: record.recency)
    return records


def _has_target_price(record: _ForecastRow) -> bool:
    return record.target_low is not None


def _has_eps_forecast(record: _ForecastRow) -> bool:
    return record.eps is not None


def _has_target_or_eps(record: _ForecastRow) -> bool:
    return record.target_low is not None or record.eps is not None


def _valuable_organizations(
    records: Iterable[_ForecastRow],
    valuable: Callable[[_ForecastRow], bool] = _has_target_price,
) -> set:
    return {record.org_name for record in records if valuable(record)}


def _select_consensus_pool(
    records: Sequence[_ForecastRow],
    as_of: date,
    cutoffs: DisclosureCutoffs,
    valuable: Callable[[_ForecastRow], bool] = _has_target_price,
) -> Optional[Tuple[str, date, List[_ForecastRow]]]:
    """按 T 期 / T-1 期披露日选研报候选池，返回 (池子, 起始日, 研报行)。

    只用预测当前年份及以后财年的行。T 池里"能给出估值"的机构 >= 2 家就用 T 池；否则
    退到 T-1 池(没有 T-1 期时 T-1 池就是 T 池)，只要有一家就估值；T-1 池也没有则
    返回 None，不估值。valuable 判定一行研报能否给出估值：默认是给了目标价，启用前瞻
    PE 通道后给了 EPS 预测也算。
    """
    if cutoffs.t_date is None:
        return None
    eligible = [
        record for record in records
        if record.report_date <= as_of and record.fiscal_year >= as_of.year
    ]
    t_pool = [record for record in eligible if record.report_date >= cutoffs.t_date]
    if len(_valuable_organizations(t_pool, valuable)) >= MIN_POOL_ORGANIZATIONS:
        return CONSENSUS_POOL_T, cutoffs.t_date, t_pool
    t1_start = cutoffs.t1_date or cutoffs.t_date
    t1_pool = [record for record in eligible if record.report_date >= t1_start]
    if _valuable_organizations(t1_pool, valuable):
        return CONSENSUS_POOL_T1, t1_start, t1_pool
    return None


def _forecast_ratio(
    base: Optional[_ForecastRow],
    target: Optional[_ForecastRow],
) -> Tuple[Optional[float], Optional[str]]:
    if base is None or target is None:
        return None, None
    if base.eps is not None and target.eps is not None:
        return target.eps / base.eps, "eps"
    if base.np is not None and target.np is not None:
        return target.np / base.np, "np"
    return None, None


def _ntm_weight(day: date) -> float:
    """未来 12 个月里落在下一财年的比例：年初为 0，年底接近 1。"""
    year_start = date(day.year, 1, 1)
    return (day - year_start).days / (date(day.year + 1, 1, 1) - year_start).days


def _ntm_value(current: Optional[float], following: Optional[float], day: date) -> Optional[float]:
    """未来 12 个月 EPS：按年内已过去的时间在当年、次年预测之间加权。"""
    if current is None or following is None:
        return None
    weight = _ntm_weight(day)
    return (1.0 - weight) * current + weight * following


def _organization_view(
    org_name: str,
    report_rows: Sequence[_ForecastRow],
    raw_bounds: Tuple[float, float],
    method: str,
    fiscal_years: Sequence[int],
    basis_factor: Optional[float],
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    head = report_rows[0]
    raw_low, raw_high = raw_bounds
    # 目标价(或 PE 通道估值)按写研报时的股价口径给出；之后送转、分红除权的，按复权因子
    # 换算到估值口径，否则除权后股价机械下跌、旧估值不动，会凭空多出"低估"空间。
    price_adjustment = _price_scale(head.price_factor, basis_factor)
    target_low, target_high = raw_low * price_adjustment, raw_high * price_adjustment
    by_year: Dict[int, _ForecastRow] = {}
    for row in report_rows:
        by_year[row.fiscal_year] = row
    base = by_year.get(fiscal_years[0])
    values: List[Optional[Dict[str, Any]]] = []
    for offset, fiscal_year in enumerate(fiscal_years):
        if offset == 0:
            ratio, basis = 1.0, method
        else:
            ratio, basis = _forecast_ratio(base, by_year.get(fiscal_year))
        if ratio is None:
            values.append(None)
            continue
        low, high = target_low * ratio, target_high * ratio
        values.append({
            "fiscal_year": fiscal_year,
            "lo": low,
            "hi": high,
            "mid": (low + high) / 2.0,
            "ratio": ratio,
            "basis": basis,
            "base_quarter": base.quarter if offset and base is not None else None,
            "quarter": f"{fiscal_year}Q4",
        })
    view: Dict[str, Any] = {
        "org_name": org_name,
        "method": method,
        "raw_org_names": sorted({row.raw_org_name for row in report_rows}),
        "report_date": head.report_date,
        "report_title": head.report_title,
        "author_name": head.author_name,
        "rating": next((row.rating for row in report_rows if row.rating), None),
        "target_price_low": target_low,
        "target_price_high": target_high,
        "target_price_low_raw": raw_low,
        "target_price_high_raw": raw_high,
        "price_adjustment": price_adjustment,
        "forecasts": [
            {
                "quarter": row.quarter,
                "fiscal_year": row.fiscal_year,
                "eps": row.eps,
                "eps_adjusted": row.eps * price_adjustment if row.eps is not None else None,
                "np": row.np,
                "pe": row.pe,
            }
            for row in sorted(by_year.values(), key=lambda item: item.fiscal_year)
        ],
        "values": values,
    }
    if extra:
        view.update(extra)
        if extra.get("ntm_eps") is not None:
            view["ntm_eps_adjusted"] = extra["ntm_eps"] * price_adjustment
    return view


def _organization_views(
    pool: Sequence[_ForecastRow],
    fiscal_years: Sequence[int],
    basis_factor: Optional[float] = None,
    pe_band: Optional[Mapping[str, Any]] = None,
    as_of: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """每家机构取它在池子里最新的一篇带目标价研报，按该研报自己的盈利预测展开财年估值。

    当前财年估值 = 目标价；下财年 / 下下财年 = 目标价 × 该研报 EPS(n) / EPS(当前财年)，
    EPS 缺失时退用净利润之比。同一篇研报里的目标价和盈利预测是同一个分析师在同一天
    给的，比拿共识目标价乘共识增速(混了不同机构)更自洽。

    传入可用的前瞻 PE 通道时，池子里从没给过目标价的机构，取它最新一篇同时有当年、
    次年 EPS 的研报，当前财年估值 = 通道 20% / 80% 分位 × 这家机构的未来 12 个月 EPS。
    """
    reports: Dict[Tuple[date, str, str, str], List[_ForecastRow]] = defaultdict(list)
    for record in pool:
        reports[record.report_key].append(record)

    latest_target: Dict[str, Tuple[Tuple[date, str], List[_ForecastRow], Tuple[float, float]]] = {}
    latest_forecast: Dict[str, Tuple[Tuple[date, str], List[_ForecastRow], float]] = {}
    for report_rows in reports.values():
        recency = max(row.recency for row in report_rows)
        org_name = report_rows[0].org_name
        bounds = next(
            ((row.target_low, row.target_high) for row in report_rows if row.target_low is not None),
            None,
        )
        if bounds is not None:
            current = latest_target.get(org_name)
            if current is None or recency > current[0]:
                latest_target[org_name] = (recency, report_rows, bounds)
        if pe_band is not None and as_of is not None:
            eps_by_year = {row.fiscal_year: row.eps for row in report_rows}
            ntm = _ntm_value(eps_by_year.get(fiscal_years[0]), eps_by_year.get(fiscal_years[1]), as_of)
            if ntm is not None and ntm > 0:
                current = latest_forecast.get(org_name)
                if current is None or recency > current[0]:
                    latest_forecast[org_name] = (recency, report_rows, ntm)

    views = [
        _organization_view(
            org_name, report_rows, bounds, VALUATION_METHOD_TARGET_PRICE, fiscal_years, basis_factor,
        )
        for org_name, (_, report_rows, bounds) in latest_target.items()
    ]
    if pe_band is not None and as_of is not None:
        for org_name, (_, report_rows, ntm) in latest_forecast.items():
            if org_name in latest_target:
                continue
            views.append(_organization_view(
                org_name,
                report_rows,
                (pe_band["low_pe"] * ntm, pe_band["high_pe"] * ntm),
                VALUATION_METHOD_PE_BAND,
                fiscal_years,
                basis_factor,
                extra={"ntm_eps": ntm, "ntm_weight": _ntm_weight(as_of)},
            ))
    views.sort(key=lambda view: (view["target_price_low"], view["org_name"]))
    return views


def _horizon_summary(
    views: Sequence[Mapping[str, Any]],
    offset: int,
    fiscal_year: int,
) -> Dict[str, Any]:
    entries = [
        (view["org_name"], view["values"][offset])
        for view in views
        if view["values"][offset] is not None
    ]
    summary: Dict[str, Any] = {
        "offset": offset,
        "label": FORECAST_HORIZON_LABELS[offset],
        "fiscal_year": fiscal_year,
        "organization_count": len(entries),
        "lo": None,
        "hi": None,
        "avg": None,
        "median": None,
        "lo_org": None,
        "hi_org": None,
    }
    if not entries:
        return summary
    lo_org, lo_value = min(entries, key=lambda entry: entry[1]["lo"])
    hi_org, hi_value = max(entries, key=lambda entry: entry[1]["hi"])
    mids = [value["mid"] for _, value in entries]
    summary.update(
        lo=lo_value["lo"],
        hi=hi_value["hi"],
        avg=_avg(mids),
        median=_percentile(mids, 0.5),
        lo_org=lo_org,
        hi_org=hi_org,
    )
    return summary


def _consensus_forecasts(
    pool: Sequence[_ForecastRow],
    fiscal_years: Sequence[int],
    basis_factor: Optional[float] = None,
) -> Dict[int, Dict[str, Optional[float]]]:
    """共识盈利预测：同一机构 + 同一 quarter 只用它最新的那条预测，再对机构取均值。

    研报 EPS = 预测归母净利 ÷ 写研报时的总股本，送转后旧研报不会重算，所以 EPS 和
    目标价一样按复权因子换算到同一口径再平均；净利润、市盈率与股本无关，不用换算。
    """
    latest: Dict[Tuple[str, int], _ForecastRow] = {}
    for record in pool:
        key = (record.org_name, record.fiscal_year)
        current = latest.get(key)
        if current is None or record.recency >= current.recency:
            latest[key] = record
    def adjusted(record: _ForecastRow, field: str) -> Optional[float]:
        value = getattr(record, field)
        if field == "eps" and value is not None:
            return value * _price_scale(record.price_factor, basis_factor)
        return value

    return {
        fiscal_year: {
            field: _avg(
                adjusted(record, field)
                for (_, year), record in latest.items()
                if year == fiscal_year
            )
            for field in ("eps", "np", "pe")
        }
        for fiscal_year in fiscal_years
    }


def _aggregate_forecast_records(
    records: Sequence[_ForecastRow],
    as_of: date,
    cutoffs: DisclosureCutoffs,
    *,
    basis_factor: Optional[float] = None,
    pe_band: Optional[Mapping[str, Any]] = None,
    include_details: bool = False,
) -> Optional[Dict[str, Any]]:
    """basis_factor 是输出价格口径对应的复权因子：取估值日当天的就是当天原始价格口径。

    pe_band 为这只股票的前瞻 PE 通道；只有状态可用时才启用，此时给了 EPS 预测的机构也
    计入"能给出估值"的机构数。
    """
    band_usable = bool(pe_band) and pe_band.get("status") == PE_BAND_AVAILABLE
    selected = _select_consensus_pool(
        records, as_of, cutoffs, _has_target_or_eps if band_usable else _has_target_price,
    )
    if selected is None:
        return None
    pool_name, pool_start, pool = selected
    fiscal_years = tuple(as_of.year + offset for offset in range(len(FORECAST_HORIZON_LABELS)))
    views = _organization_views(pool, fiscal_years, basis_factor, pe_band if band_usable else None, as_of)
    if not views:
        return None
    horizons = [
        _horizon_summary(views, offset, fiscal_year)
        for offset, fiscal_year in enumerate(fiscal_years)
    ]
    forecasts = _consensus_forecasts(pool, fiscal_years, basis_factor)
    current, following, after_next = (forecasts[year] for year in fiscal_years)
    growth_source = "eps"
    growth_pct = _growth_pct(current["eps"], following["eps"])
    if growth_pct is None:
        growth_source = "np"
        growth_pct = _growth_pct(current["np"], following["np"])
    rating_counter = Counter(view["rating"] for view in views if view["rating"])
    current_horizon = horizons[0]

    aggregate: Dict[str, Any] = {
        "pool": pool_name,
        "is_stale": pool_name != CONSENSUS_POOL_T,
        "pool_start_date": pool_start,
        "t_period": cutoffs.t_period,
        "t_disclosure_date": cutoffs.t_date,
        "t1_period": cutoffs.t1_period,
        "t1_disclosure_date": cutoffs.t1_date,
        "latest_report_date": max(record.report_date for record in pool),
        "horizons": horizons,
        "target_price_avg": current_horizon["avg"],
        "target_price_median": current_horizon["median"],
        "target_price_min": current_horizon["lo"],
        "target_price_max": current_horizon["hi"],
        "growth_pct": growth_pct,
        "growth_source": growth_source if growth_pct is not None else None,
        "forecast_year": fiscal_years[0],
        "next_forecast_year": fiscal_years[1],
        "next2_forecast_year": fiscal_years[2],
        "consensus_eps": current["eps"],
        "next_consensus_eps": following["eps"],
        "next2_consensus_eps": after_next["eps"],
        "consensus_np": current["np"],
        "next_consensus_np": following["np"],
        "consensus_pe": current["pe"],
        "next_consensus_pe": following["pe"],
        "report_count": len({record.report_key for record in pool}),
        # 每家机构只算一篇研报；带目标价研报数 = 用目标价估值的机构数。
        "target_report_count": sum(1 for view in views if view["method"] == VALUATION_METHOD_TARGET_PRICE),
        "organization_count": len(views),
        "method_counts": dict(Counter(view["method"] for view in views)),
        "pe_band": dict(pe_band) if pe_band else None,
        "rating": rating_counter.most_common(1)[0][0] if rating_counter else None,
    }
    if include_details:
        aggregate["organizations"] = views
    return aggregate


def _aggregate_report_rows(
    rows: Sequence[Mapping[str, Any]],
    as_of: date,
    cutoffs: DisclosureCutoffs,
    *,
    price_factors: Optional[_PriceFactors] = None,
    pe_band: Optional[Mapping[str, Any]] = None,
    include_details: bool = False,
) -> Optional[Dict[str, Any]]:
    """输出按估值日当天的原始价格口径，和当天收盘价直接可比。"""
    return _aggregate_forecast_records(
        _normalize_forecast_rows(rows, price_factors),
        as_of,
        cutoffs,
        basis_factor=price_factors.on_or_before(as_of) if price_factors else None,
        pe_band=pe_band,
        include_details=include_details,
    )


def _forward_pe_series(
    records: Sequence[_ForecastRow],
    disclosures: Sequence[Tuple[date, date]],
    closes: Sequence[Tuple[date, float]],
    price_factors: Optional[_PriceFactors],
) -> List[Tuple[date, Optional[float]]]:
    """逐个交易日的前瞻 PE = 股价 ÷ 一致预期未来 12 个月 EPS。

    一致预期 EPS 走和估值同一套研报池规则(T/T-1 期、机构 + quarter 取最新)，只是
    "能给出估值"的门槛换成给了 EPS 预测；股价和 EPS 都换算成后复权口径再相除，
    除权前后可比。当天没有一致预期或 EPS 不为正时记为 None。
    """
    report_dates = [record.report_date for record in records]
    cache_key = None
    consensus: Optional[Tuple[Optional[float], Optional[float]]] = None
    series: List[Tuple[date, Optional[float]]] = []
    for day, close in closes:
        visible = bisect_right(report_dates, day)
        cutoffs = _disclosure_cutoffs_as_of(disclosures, day)
        key = (visible, cutoffs, day.year)
        if key != cache_key:
            cache_key = key
            selected = _select_consensus_pool(records[:visible], day, cutoffs, _has_eps_forecast)
            if selected is None:
                consensus = None
            else:
                # basis_factor=1 即后复权口径：EPS × 写研报时的复权因子。
                forecasts = _consensus_forecasts(selected[2], (day.year, day.year + 1), 1.0)
                consensus = (forecasts[day.year]["eps"], forecasts[day.year + 1]["eps"])
        ntm = _ntm_value(consensus[0], consensus[1], day) if consensus else None
        factor = price_factors.on_or_before(day) if price_factors else None
        series.append((day, close * (factor or 1.0) / ntm if ntm is not None and ntm > 0 else None))
    return series


def _pe_band(series: Sequence[Tuple[date, Optional[float]]], as_of: date) -> Dict[str, Any]:
    """as_of 当天可见的前瞻 PE 通道：近 3 年的 20% / 50% / 80% 分位。"""
    window_start = as_of - timedelta(days=PE_BAND_WINDOW_DAYS)
    window = [pe for day, pe in series if window_start < day <= as_of]
    valid = sorted(pe for pe in window if pe is not None and pe > 0)
    band: Dict[str, Any] = {
        "status": "unavailable",
        "reason": None,
        "as_of": as_of,
        "window_start": window_start,
        "days": len(window),
        "valid_days": len(valid),
        "low_pe": None,
        "mid_pe": None,
        "high_pe": None,
        "current_pe": next((pe for day, pe in reversed(series) if day <= as_of), None),
    }
    if len(valid) < PE_BAND_MIN_DAYS:
        band["reason"] = "insufficient_history"
        return band
    band.update(
        low_pe=_percentile(valid, PE_BAND_LOW_PERCENTILE),
        mid_pe=_percentile(valid, 0.5),
        high_pe=_percentile(valid, PE_BAND_HIGH_PERCENTILE),
    )
    if len(valid) < PE_BAND_MIN_VALID_RATIO * len(window):
        band["reason"] = "unprofitable_or_uncovered"
    elif band["high_pe"] > PE_BAND_MAX_WIDTH * band["low_pe"]:
        band["reason"] = "unstable_multiple"
    else:
        band["status"] = PE_BAND_AVAILABLE
    return band


def _load_daily_closes(
    db: Any,
    symbols: Sequence[str],
    *,
    start: date,
    end: date,
) -> Dict[str, List[Tuple[date, float]]]:
    closes: Dict[str, List[Tuple[date, float]]] = defaultdict(list)
    for offset in range(0, len(symbols), 500):
        chunk = symbols[offset:offset + 500]
        symbol_params = {f"symbol_{index}": symbol for index, symbol in enumerate(chunk)}
        placeholders = ",".join(f":{key}" for key in symbol_params)
        rows = db.execute(text(f"""
            SELECT ts_code, trade_date, close
            FROM a_stock_market_daily
            WHERE ts_code IN ({placeholders})
              AND trade_date > :start
              AND trade_date <= :end
              AND close > 0
            ORDER BY ts_code, trade_date
        """), {**symbol_params, "start": start, "end": end}).mappings().all()
        for row in rows:
            day = _row_to_date(row.get("trade_date"))
            close = _positive_float(row.get("close"))
            if day is not None and close is not None:
                closes[str(row.get("ts_code") or "").strip().upper()].append((day, close))
    return closes


def compute_forward_pe_bands(
    db: Any,
    symbols: Iterable[str],
    as_of: date,
) -> Dict[str, Dict[str, Any]]:
    """计算一批股票截至 as_of 的前瞻 PE 通道。全市场约 3000 只要二十来秒，列表页用
    定时任务预先算好的结果；个股页只算一只，现算即可。"""
    normalized_symbols = list(dict.fromkeys(
        normalized for symbol in symbols
        if (normalized := normalize_a_stock_symbol(symbol))
    ))
    if not normalized_symbols:
        return {}
    window_start = as_of - timedelta(days=PE_BAND_WINDOW_DAYS)
    report_start = window_start - timedelta(days=REPORT_FETCH_LOOKBACK_DAYS)
    rows_by_symbol = _load_report_rows_by_symbol(
        db, normalized_symbols, report_start=report_start, report_end=as_of,
    )
    covered = list(rows_by_symbol)
    disclosures = load_a_stock_disclosure_dates(
        db, covered, since=report_start - timedelta(days=DISCLOSURE_FETCH_LOOKBACK_DAYS),
    )
    price_factors = load_a_stock_price_factors(
        db, covered, start=report_start - timedelta(days=PRICE_FACTOR_PADDING_DAYS), end=as_of,
    )
    closes = _load_daily_closes(db, covered, start=window_start, end=as_of)
    bands: Dict[str, Dict[str, Any]] = {}
    for symbol in covered:
        symbol_factors = price_factors.get(symbol)
        series = _forward_pe_series(
            _normalize_forecast_rows(rows_by_symbol[symbol], symbol_factors),
            disclosures.get(symbol, []),
            closes.get(symbol, []),
            symbol_factors,
        )
        bands[symbol] = _pe_band(series, as_of)
    return bands


def _pool_fields(aggregate: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "pool": aggregate["pool"],
        "is_stale": aggregate["is_stale"],
        "pool_start_date": _date_to_iso(aggregate["pool_start_date"]),
        "t_period": _date_to_iso(aggregate["t_period"]),
        "t_period_label": _period_label(aggregate["t_period"]),
        "t_disclosure_date": _date_to_iso(aggregate["t_disclosure_date"]),
        "t1_period": _date_to_iso(aggregate["t1_period"]),
        "t1_period_label": _period_label(aggregate["t1_period"]),
        "t1_disclosure_date": _date_to_iso(aggregate["t1_disclosure_date"]),
    }


def _iter_point_in_time_aggregates(
    records: Sequence[_ForecastRow],
    disclosures: Sequence[Tuple[date, date]],
    trade_dates: Sequence[date],
    basis_for_day: Optional[Callable[[date], Optional[float]]] = None,
    pe_band_for_day: Optional[Callable[[date], Optional[Mapping[str, Any]]]] = None,
):
    """逐个交易日回放共识：只看当天及以前的研报、当天已披露的财报。

    basis_for_day 给出每天输出价格口径对应的复权因子。研报集合、财报截面、当前年份、
    价格口径都没变时复用上一次的结果，避免每天重算。产出 (交易日, 聚合结果, 口径因子)。
    """
    report_dates = [record.report_date for record in records]
    cache_key = None
    aggregate: Optional[Dict[str, Any]] = None
    for trade_day in trade_dates:
        visible = bisect_right(report_dates, trade_day)
        cutoffs = _disclosure_cutoffs_as_of(disclosures, trade_day)
        basis = basis_for_day(trade_day) if basis_for_day else None
        pe_band = pe_band_for_day(trade_day) if pe_band_for_day else None
        key = (visible, cutoffs, trade_day.year, basis, id(pe_band) if pe_band else None)
        if key != cache_key:
            cache_key = key
            aggregate = _aggregate_forecast_records(
                records[:visible], trade_day, cutoffs, basis_factor=basis, pe_band=pe_band,
            )
        yield trade_day, aggregate, basis


def build_a_stock_consensus_candidates(
    rows: Sequence[Mapping[str, Any]],
    latest_trade_date: Optional[date],
    *,
    disclosures: Optional[Mapping[str, Sequence[Tuple[date, date]]]] = None,
    price_factors: Optional[Mapping[str, _PriceFactors]] = None,
    pe_bands: Optional[Mapping[str, Mapping[str, Any]]] = None,
    search_symbol: str = "",
    has_search: bool = False,
    min_market_cap_100m: Optional[float] = 100.0,
    max_market_cap_100m: Optional[float] = None,
    min_undervalue_pct: Optional[float] = 10.0,
    min_growth_pct: Optional[float] = 10.0,
    min_organization_count: int = 1,
    stale_filter: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    if latest_trade_date is None:
        return []
    disclosures = disclosures or {}
    price_factors = price_factors or {}
    grouped: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        symbol = str(row.get("ts_code") or "").strip().upper()
        if symbol:
            grouped[symbol].append(row)

    normalized_symbol = normalize_a_stock_symbol(search_symbol)
    apply_filters = not has_search and not normalized_symbol
    min_organizations = (
        1 if has_search or normalized_symbol else max(1, int(min_organization_count or 1))
    )
    normalized_limit = max(1, min(int(limit or 200), 1000))

    candidates: List[Dict[str, Any]] = []
    for symbol, symbol_rows in grouped.items():
        first = symbol_rows[0]
        close = _positive_float(first.get("close"))
        if close is None:
            continue

        total_mv = _positive_float(first.get("total_mv"))
        circ_mv = _positive_float(first.get("circ_mv"))
        market_cap_100m = total_mv / A_STOCK_MARKET_CAP_UNIT if total_mv is not None else None
        circ_market_cap_100m = circ_mv / A_STOCK_MARKET_CAP_UNIT if circ_mv is not None else None

        aggregate = _aggregate_report_rows(
            symbol_rows,
            latest_trade_date,
            _disclosure_cutoffs_as_of(disclosures.get(symbol, []), latest_trade_date),
            price_factors=price_factors.get(symbol),
            pe_band=pe_bands.get(symbol) if pe_bands else None,
        )
        if aggregate is None:
            continue
        if aggregate["organization_count"] < min_organizations:
            continue

        target_price_min = aggregate["target_price_min"]
        # 低估率取最悲观的单家机构目标价下沿。
        undervalue_pct = (target_price_min / close - 1.0) * 100.0
        growth_pct = aggregate["growth_pct"]

        if apply_filters:
            if market_cap_100m is None:
                continue
            if min_market_cap_100m is not None and market_cap_100m < float(min_market_cap_100m):
                continue
            if max_market_cap_100m is not None and market_cap_100m > float(max_market_cap_100m):
                continue
            if min_undervalue_pct is not None and undervalue_pct + FLOAT_COMPARE_EPSILON < float(min_undervalue_pct):
                continue
            if min_growth_pct is not None and (
                growth_pct is None or growth_pct + FLOAT_COMPARE_EPSILON < float(min_growth_pct)
            ):
                continue
            if stale_filter == STALE_FILTER_FRESH and aggregate["is_stale"]:
                continue
            if stale_filter == STALE_FILTER_STALE and not aggregate["is_stale"]:
                continue

        candidates.append({
            "symbol": symbol,
            "name": first.get("stock_name") or first.get("report_name") or symbol,
            "industry": first.get("industry"),
            "market": first.get("market"),
            "trade_date": _date_to_iso(first.get("trade_date")),
            "latest_report_date": _date_to_iso(aggregate["latest_report_date"]),
            "close": close,
            "target_price_avg": aggregate["target_price_avg"],
            "target_price_min": target_price_min,
            "target_price_max": aggregate["target_price_max"],
            "undervalue_pct": undervalue_pct,
            "growth_pct": growth_pct,
            "growth_source": aggregate["growth_source"],
            "forecast_year": aggregate["forecast_year"],
            "next_forecast_year": aggregate["next_forecast_year"],
            "consensus_eps": aggregate["consensus_eps"],
            "next_consensus_eps": aggregate["next_consensus_eps"],
            "consensus_np": aggregate["consensus_np"],
            "next_consensus_np": aggregate["next_consensus_np"],
            "consensus_pe": aggregate["consensus_pe"],
            "next_consensus_pe": aggregate["next_consensus_pe"],
            "market_cap_100m": market_cap_100m,
            "circ_market_cap_100m": circ_market_cap_100m,
            "report_count": aggregate["report_count"],
            "target_report_count": aggregate["target_report_count"],
            "organization_count": aggregate["organization_count"],
            "rating": aggregate["rating"],
            "method_counts": aggregate["method_counts"],
            "pe_band": aggregate["pe_band"],
            **_pool_fields(aggregate),
        })

    candidates.sort(
        key=lambda item: (
            item.get("undervalue_pct") if item.get("undervalue_pct") is not None else -999999.0,
            item.get("growth_pct") if item.get("growth_pct") is not None else -999999.0,
            item.get("organization_count") or 0,
        ),
        reverse=True,
    )
    return candidates[:normalized_limit]


def _history_point(trade_day: date, aggregate: Mapping[str, Any]) -> Dict[str, Any]:
    current, following, after_next = aggregate["horizons"]
    return {
        "date": trade_day.isoformat(),
        # 估值上下限 = 按机构去重后各家的最低 / 最高目标价，能直接追溯到具体机构。
        "fair_value_lo": current["lo"],
        "fair_value_hi": current["hi"],
        "target_price_avg": aggregate["target_price_avg"],
        "target_price_min": aggregate["target_price_min"],
        "target_price_max": aggregate["target_price_max"],
        "forward_next_fy_lo": following["lo"],
        "forward_next_fy_hi": following["hi"],
        "forward_next2_fy_lo": after_next["lo"],
        "forward_next2_fy_hi": after_next["hi"],
        "pe_ratio": aggregate["consensus_pe"],
        "forward_pe_ratio": aggregate["next_consensus_pe"],
        "growth_pct": aggregate["growth_pct"],
        "growth_source": aggregate["growth_source"],
        "forecast_year": aggregate["forecast_year"],
        "next_forecast_year": aggregate["next_forecast_year"],
        "next2_forecast_year": aggregate["next2_forecast_year"],
        "report_count": aggregate["report_count"],
        "target_report_count": aggregate["target_report_count"],
        "organization_count": aggregate["organization_count"],
        "rating": aggregate["rating"],
        "pool": aggregate["pool"],
        "is_stale": aggregate["is_stale"],
    }


def build_a_stock_rolling_consensus_history(
    rows: Sequence[Mapping[str, Any]],
    trade_dates: Iterable[date],
    *,
    disclosures: Sequence[Tuple[date, date]] = (),
    price_factors: Optional[_PriceFactors] = None,
    closes: Optional[Sequence[Tuple[date, float]]] = None,
) -> List[Dict[str, Any]]:
    """逐个交易日的共识估值。某天没有估值(T-1 池也空)就不出点，前端按前值填充画线。

    传入 closes(需覆盖图表起点往前 3 年)时启用前瞻 PE 通道：每个交易日用当天之前 3 年的
    通道，每 PE_BAND_REFRESH_EVERY_DAYS 个交易日重算一次，不用未来数据。

    个股 K 线画的是前复权价(a_stock_market_daily_qfq，锚点是最新复权因子)，估值线统一
    换算到同一个锚点，除权前后的线才能和 K 线对齐。
    """
    normalized_dates = sorted(set(day for day in trade_dates if day))
    records = _normalize_forecast_rows(rows, price_factors)
    if not normalized_dates or not records:
        return []
    anchor = price_factors.latest if price_factors else None
    bands_by_day: Dict[date, Dict[str, Any]] = {}
    if closes is not None:
        series = _forward_pe_series(records, disclosures, closes, price_factors)
        band: Optional[Dict[str, Any]] = None
        for index, trade_day in enumerate(normalized_dates):
            if band is None or index % PE_BAND_REFRESH_EVERY_DAYS == 0:
                band = _pe_band(series, trade_day)
            bands_by_day[trade_day] = band
    return [
        _history_point(trade_day, aggregate)
        for trade_day, aggregate, _ in _iter_point_in_time_aggregates(
            records,
            disclosures,
            normalized_dates,
            basis_for_day=lambda _day: anchor,
            pe_band_for_day=bands_by_day.get if bands_by_day else None,
        )
        if aggregate is not None
    ]


_REPORT_COLUMNS = """
    r.ts_code, r.name AS report_name, r.report_date, r.report_title,
    r.org_name, r.author_name, r.quarter, r.eps, r.pe, r.np,
    r.rating, r.max_price, r.min_price, r.create_time
"""


def load_a_stock_consensus_history(
    db: Any,
    symbol: str,
    *,
    limit: int = 1260,
    use_pe_band: bool = False,
) -> List[Dict[str, Any]]:
    normalized_symbol = normalize_a_stock_symbol(symbol)
    if not normalized_symbol:
        return []

    normalized_limit = max(1, min(int(limit or 1260), 5000))
    trade_dates = [
        _row_to_date(row[0])
        for row in db.execute(text("""
            SELECT trade_date
            FROM a_stock_market_daily
            WHERE ts_code = :symbol
            ORDER BY trade_date DESC
            LIMIT :limit
        """), {"symbol": normalized_symbol, "limit": normalized_limit}).all()
    ]
    trade_dates = [day for day in trade_dates if day is not None]
    if not trade_dates:
        return []
    earliest_trade_date = min(trade_dates)
    # 开启前瞻 PE 通道时，图表第一天也要有它之前 3 年的通道，研报和股价都得往前多取 3 年。
    band_start = earliest_trade_date - timedelta(days=PE_BAND_WINDOW_DAYS) if use_pe_band else earliest_trade_date
    report_start = band_start - timedelta(days=REPORT_FETCH_LOOKBACK_DAYS)
    rows = db.execute(
        text(
            f"""
            SELECT {_REPORT_COLUMNS}
            FROM a_stock_report_rc r
            WHERE r.ts_code = :symbol
              AND r.report_date >= :report_start
            ORDER BY r.report_date
            """
        ),
        {
            "symbol": normalized_symbol,
            "report_start": report_start,
        },
    ).mappings().all()
    disclosures = load_a_stock_disclosure_dates(
        db,
        [normalized_symbol],
        since=report_start - timedelta(days=DISCLOSURE_FETCH_LOOKBACK_DAYS),
    )
    price_factors = load_a_stock_price_factors(
        db, [normalized_symbol], start=report_start - timedelta(days=PRICE_FACTOR_PADDING_DAYS),
    )
    closes = None
    if use_pe_band:
        closes = _load_daily_closes(
            db, [normalized_symbol], start=band_start, end=max(trade_dates),
        ).get(normalized_symbol, [])
    return build_a_stock_rolling_consensus_history(
        rows,
        trade_dates,
        disclosures=disclosures.get(normalized_symbol, []),
        price_factors=price_factors.get(normalized_symbol),
        closes=closes,
    )


def load_a_stock_consensus_detail(db: Any, symbol: str, *, use_pe_band: bool = False) -> Dict[str, Any]:
    """个股详情页用：最新交易日的三个财年估值上下限，以及每家机构的研报明细。

    use_pe_band 时现算这一只股票的前瞻 PE 通道，没给目标价的机构用通道补估值。
    """
    normalized_symbol = normalize_a_stock_symbol(symbol)
    if not normalized_symbol:
        return {"status": "unavailable", "reason": "invalid_symbol"}
    market = db.execute(text("""
        SELECT trade_date, close
        FROM a_stock_market_daily
        WHERE ts_code = :symbol
        ORDER BY trade_date DESC
        LIMIT 1
    """), {"symbol": normalized_symbol}).mappings().first()
    as_of = _row_to_date(market.get("trade_date")) if market else None
    if as_of is None:
        return {"symbol": normalized_symbol, "status": "unavailable", "reason": "market_data_missing"}

    rows = db.execute(
        text(
            f"""
            SELECT {_REPORT_COLUMNS}
            FROM a_stock_report_rc r
            WHERE r.ts_code = :symbol
              AND r.report_date >= :report_start
              AND r.report_date <= :as_of
            ORDER BY r.report_date
            """
        ),
        {
            "symbol": normalized_symbol,
            "report_start": as_of - timedelta(days=REPORT_FETCH_LOOKBACK_DAYS),
            "as_of": as_of,
        },
    ).mappings().all()
    disclosures = load_a_stock_disclosure_dates(
        db,
        [normalized_symbol],
        since=as_of - timedelta(days=DISCLOSURE_FETCH_LOOKBACK_DAYS),
    ).get(normalized_symbol, [])
    cutoffs = _disclosure_cutoffs_as_of(disclosures, as_of)
    payload: Dict[str, Any] = {
        "symbol": normalized_symbol,
        "as_of": as_of.isoformat(),
        "close": _positive_float(market.get("close")),
        "min_pool_organizations": MIN_POOL_ORGANIZATIONS,
        "t_period": _date_to_iso(cutoffs.t_period),
        "t_period_label": _period_label(cutoffs.t_period),
        "t_disclosure_date": _date_to_iso(cutoffs.t_date),
        "t1_period": _date_to_iso(cutoffs.t1_period),
        "t1_period_label": _period_label(cutoffs.t1_period),
        "t1_disclosure_date": _date_to_iso(cutoffs.t1_date),
    }
    price_factors = load_a_stock_price_factors(
        db,
        [normalized_symbol],
        start=as_of - timedelta(days=REPORT_FETCH_LOOKBACK_DAYS + PRICE_FACTOR_PADDING_DAYS),
        end=as_of,
    ).get(normalized_symbol)
    pe_band = compute_forward_pe_bands(db, [normalized_symbol], as_of).get(normalized_symbol) if use_pe_band else None
    payload["use_pe_band"] = use_pe_band
    payload["pe_band"] = pe_band
    aggregate = _aggregate_report_rows(
        rows, as_of, cutoffs, price_factors=price_factors, pe_band=pe_band, include_details=True,
    )
    if aggregate is None:
        payload.update(
            status="unavailable",
            reason="disclosure_missing" if cutoffs.t_date is None else "no_target_price_in_pool",
        )
        return payload
    payload.update(
        status="available",
        **_pool_fields(aggregate),
        organization_count=aggregate["organization_count"],
        method_counts=aggregate["method_counts"],
        report_count=aggregate["report_count"],
        latest_report_date=_date_to_iso(aggregate["latest_report_date"]),
        horizons=aggregate["horizons"],
        organizations=[
            {**view, "report_date": _date_to_iso(view["report_date"])}
            for view in aggregate["organizations"]
        ],
    )
    return payload


def _index_valuation_payload(symbol: str, aggregate: Mapping[str, Any]) -> Dict[str, Any]:
    """指数估值用的成分股估值：下限 / 中枢 / 上限 = 各机构最低 / 中位 / 最高。"""
    current, following, _ = aggregate["horizons"]
    return {
        "symbol": symbol,
        "date": aggregate.get("latest_report_date"),
        "fair_value_lo": current["lo"],
        "fair_value_mid": current["median"],
        "fair_value_hi": current["hi"],
        "forward_next_fy_lo": following["lo"],
        "forward_next_fy_mid": following["median"],
        "forward_next_fy_hi": following["hi"],
        "growth_pct": aggregate.get("growth_pct"),
        "report_count": aggregate.get("report_count"),
        "target_report_count": aggregate.get("target_report_count"),
        "organization_count": aggregate.get("organization_count"),
        "pool": aggregate.get("pool"),
        "is_stale": aggregate.get("is_stale"),
    }


_INDEX_PRICE_FIELDS = (
    "fair_value_lo", "fair_value_mid", "fair_value_hi",
    "forward_next_fy_lo", "forward_next_fy_mid", "forward_next_fy_hi",
)


def _rescale_price_fields(payload: Dict[str, Any], scale: float) -> Dict[str, Any]:
    if abs(scale - 1.0) < FLOAT_COMPARE_EPSILON:
        return payload
    return {
        **payload,
        **{
            field: payload[field] * scale
            for field in _INDEX_PRICE_FIELDS
            if payload.get(field) is not None
        },
    }


def _load_report_rows_by_symbol(
    db: Any,
    symbols: Sequence[str],
    *,
    report_start: date,
    report_end: date,
) -> Dict[str, List[Mapping[str, Any]]]:
    rows_by_symbol: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for offset in range(0, len(symbols), 500):
        chunk = symbols[offset:offset + 500]
        symbol_params = {f"symbol_{index}": symbol for index, symbol in enumerate(chunk)}
        placeholders = ",".join(f":{key}" for key in symbol_params)
        rows = db.execute(text(f"""
            SELECT {_REPORT_COLUMNS}
            FROM a_stock_report_rc r
            WHERE r.ts_code IN ({placeholders})
              AND r.report_date BETWEEN :report_start AND :report_end
            ORDER BY r.ts_code, r.report_date
        """), {**symbol_params, "report_start": report_start, "report_end": report_end}).mappings().all()
        for row in rows:
            rows_by_symbol[str(row.get("ts_code") or "").strip().upper()].append(row)
    return rows_by_symbol


def load_a_stock_consensus_valuation_map(
    db: Any,
    symbols: Iterable[str],
    as_of: Optional[date] = None,
) -> Dict[str, Dict[str, Any]]:
    """Load A-share consensus target ranges for a constituent universe.

    默认取最新交易日；传 ``as_of`` 时取 as_of 及之前最近一个交易日，研报、财报披露日
    和复权因子都按那一天截断(point-in-time)，供选股系统回放历史快照使用。
    """
    normalized_symbols = list(dict.fromkeys(
        normalized for symbol in symbols
        if (normalized := normalize_a_stock_symbol(symbol))
    ))
    if not normalized_symbols:
        return {}
    if as_of is None:
        latest_trade_date = _row_to_date(
            db.execute(text("SELECT MAX(trade_date) FROM a_stock_market_daily")).scalar()
        )
    else:
        latest_trade_date = _row_to_date(
            db.execute(
                text("SELECT MAX(trade_date) FROM a_stock_market_daily WHERE trade_date <= :as_of"),
                {"as_of": as_of},
            ).scalar()
        )
    if latest_trade_date is None:
        return {}

    closes: Dict[str, float] = {}
    for offset in range(0, len(normalized_symbols), 500):
        chunk = normalized_symbols[offset:offset + 500]
        symbol_params = {f"symbol_{index}": symbol for index, symbol in enumerate(chunk)}
        placeholders = ",".join(f":{key}" for key in symbol_params)
        for row in db.execute(text(f"""
            SELECT ts_code, close
            FROM a_stock_market_daily
            WHERE trade_date = :latest_trade_date
              AND ts_code IN ({placeholders})
        """), {**symbol_params, "latest_trade_date": latest_trade_date}).mappings().all():
            close = _positive_float(row.get("close"))
            if close is not None:
                closes[str(row.get("ts_code") or "").strip().upper()] = close

    rows_by_symbol = _load_report_rows_by_symbol(
        db,
        normalized_symbols,
        report_start=latest_trade_date - timedelta(days=REPORT_FETCH_LOOKBACK_DAYS),
        report_end=latest_trade_date,
    )
    disclosures = load_a_stock_disclosure_dates(
        db,
        list(rows_by_symbol),
        since=latest_trade_date - timedelta(days=DISCLOSURE_FETCH_LOOKBACK_DAYS),
    )
    price_factors = load_a_stock_price_factors(
        db,
        list(rows_by_symbol),
        start=latest_trade_date - timedelta(days=REPORT_FETCH_LOOKBACK_DAYS + PRICE_FACTOR_PADDING_DAYS),
        end=latest_trade_date,
    )
    result: Dict[str, Dict[str, Any]] = {}
    for symbol, symbol_rows in rows_by_symbol.items():
        close = closes.get(symbol)
        aggregate = _aggregate_report_rows(
            symbol_rows,
            latest_trade_date,
            _disclosure_cutoffs_as_of(disclosures.get(symbol, []), latest_trade_date),
            price_factors=price_factors.get(symbol),
        )
        if aggregate is None or close is None:
            continue
        result[symbol] = {**_index_valuation_payload(symbol, aggregate), "last_price": close}
    return result


def load_a_stock_consensus_valuation_history_map(
    db: Any,
    symbols: Iterable[str],
    trade_dates: Iterable[date],
) -> Dict[date, Dict[str, Dict[str, Any]]]:
    """逐日回放成分股共识估值，供指数估值系数曲线使用。

    某天成分股没有估值(T-1 池也空)时沿用它上一次的估值，最多沿用
    VALUATION_FFILL_MAX_DAYS 天；沿用的估值标记 is_ffilled / is_stale。为了让窗口
    第一天也能接上之前的估值，会从窗口起点往前多回放 VALUATION_FFILL_MAX_DAYS 天。
    """
    normalized_symbols = list(dict.fromkeys(
        normalized for symbol in symbols
        if (normalized := normalize_a_stock_symbol(symbol))
    ))
    requested_dates = sorted(set(day for day in trade_dates if day))
    if not normalized_symbols or not requested_dates:
        return {}

    start_date = requested_dates[0]
    end_date = requested_dates[-1]
    warmup_start = start_date - timedelta(days=VALUATION_FFILL_MAX_DAYS)
    market_by_date: Dict[date, Dict[str, float]] = defaultdict(dict)
    for offset in range(0, len(normalized_symbols), 500):
        chunk = normalized_symbols[offset:offset + 500]
        symbol_params = {f"symbol_{index}": symbol for index, symbol in enumerate(chunk)}
        placeholders = ",".join(f":{key}" for key in symbol_params)
        market_rows = db.execute(text(f"""
            SELECT ts_code, trade_date, close
            FROM a_stock_market_daily
            WHERE ts_code IN ({placeholders})
              AND trade_date BETWEEN :start_date AND :end_date
            ORDER BY trade_date, ts_code
        """), {**symbol_params, "start_date": warmup_start, "end_date": end_date}).mappings().all()
        for row in market_rows:
            row_date = _row_to_date(row.get("trade_date"))
            close = _positive_float(row.get("close"))
            if row_date and close is not None:
                market_by_date[row_date][str(row.get("ts_code") or "").upper()] = close

    rows_by_symbol = _load_report_rows_by_symbol(
        db,
        normalized_symbols,
        report_start=warmup_start - timedelta(days=REPORT_FETCH_LOOKBACK_DAYS),
        report_end=end_date,
    )
    disclosures = load_a_stock_disclosure_dates(
        db,
        list(rows_by_symbol),
        since=warmup_start - timedelta(days=DISCLOSURE_FETCH_LOOKBACK_DAYS),
    )
    price_factors = load_a_stock_price_factors(
        db,
        list(rows_by_symbol),
        start=warmup_start - timedelta(days=REPORT_FETCH_LOOKBACK_DAYS + PRICE_FACTOR_PADDING_DAYS),
        end=end_date,
    )
    requested = set(requested_dates)
    replay_dates = sorted(requested | {day for day in market_by_date if day <= end_date})

    result: Dict[date, Dict[str, Dict[str, Any]]] = defaultdict(dict)
    for symbol, symbol_rows in rows_by_symbol.items():
        symbol_factors = price_factors.get(symbol)
        records = _normalize_forecast_rows(symbol_rows, symbol_factors)
        if not records:
            continue
        last_payload: Optional[Dict[str, Any]] = None
        last_valued_day: Optional[date] = None
        last_basis: Optional[float] = None
        last_aggregate: Optional[Dict[str, Any]] = None
        for trade_day, aggregate, basis in _iter_point_in_time_aggregates(
            records,
            disclosures.get(symbol, []),
            replay_dates,
            basis_for_day=symbol_factors.on_or_before if symbol_factors else None,
        ):
            if aggregate is not None:
                if aggregate is not last_aggregate:
                    last_aggregate = aggregate
                    last_payload = _index_valuation_payload(symbol, aggregate)
                last_valued_day, last_basis = trade_day, basis
                payload, is_ffilled = last_payload, False
            elif (
                last_payload is not None
                and last_valued_day is not None
                and (trade_day - last_valued_day).days <= VALUATION_FFILL_MAX_DAYS
            ):
                # 沿用期间发生了除权的，把旧估值换算到当天的价格口径。
                payload = _rescale_price_fields(last_payload, _price_scale(last_basis, basis))
                is_ffilled = True
            else:
                continue
            if trade_day not in requested:
                continue
            close = market_by_date.get(trade_day, {}).get(symbol)
            if close is None:
                continue
            result[trade_day][symbol] = {
                **payload,
                "last_price": close,
                "is_ffilled": is_ffilled,
                "is_stale": bool(payload.get("is_stale")) or is_ffilled,
            }
    return {day: values for day, values in result.items()}


def load_a_stock_klines(
    db: Any,
    symbol: str,
    *,
    start_date: date,
    end_date: date,
) -> List[Dict[str, Any]]:
    normalized_symbol = normalize_a_stock_symbol(symbol)
    if not normalized_symbol or start_date > end_date:
        return []
    def query_rows(table_name: str, volume_column: str, turnover_column: str):
        return db.execute(
            text(
                f"""
            SELECT
                trade_date,
                open,
                high,
                low,
                close,
                {volume_column} AS volume,
                {turnover_column} AS turnover,
                turnover_rate
            FROM {table_name}
            WHERE ts_code = :symbol
              AND trade_date >= :start_date
              AND trade_date <= :end_date
            ORDER BY trade_date
            """
            ),
            {
                "symbol": normalized_symbol,
                "start_date": start_date,
                "end_date": end_date,
            },
        ).mappings().all()

    rows = query_rows("a_stock_market_daily_qfq", "volume", "turnover")
    if not rows:
        rows = query_rows("a_stock_market_daily", "vol", "amount")

    result: List[Dict[str, Any]] = []
    for row in rows:
        payload = _a_stock_kline_payload(row)
        if payload is not None:
            result.append(payload)
    return result


def _a_stock_kline_payload(row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """个股详情页 K 线的一根日 K；单只和批量加载共用，保证选股系统的技术信号和 K 线图吃的是同一份输入。"""
    trade_date = _row_to_date(row.get("trade_date"))
    if trade_date is None:
        return None
    # tushare daily_basic 的 turnover_rate 是百分数（2.02 表示 2.02%）；K 线接口统一返回
    # 小数口径，和美港股 volume/股本 算出的一致，前端换手衰减按小数计算。
    turnover_rate_pct = _safe_float(row.get("turnover_rate"))
    return {
        "timestamp": datetime.combine(trade_date, time(hour=15)),
        "open": _safe_float(row.get("open")) or 0.0,
        "high": _safe_float(row.get("high")) or 0.0,
        "low": _safe_float(row.get("low")) or 0.0,
        "close": _safe_float(row.get("close")) or 0.0,
        "volume": _safe_float(row.get("volume")) or 0.0,
        "turnover": _safe_float(row.get("turnover")) or 0.0,
        "turnover_rate": None if turnover_rate_pct is None else turnover_rate_pct / 100.0,
    }


def load_a_stock_klines_batch(
    db: Any,
    symbols: Iterable[str],
    *,
    start_date: date,
    end_date: date,
) -> Dict[str, List[Dict[str, Any]]]:
    """多只股票的前复权日 K：与 ``load_a_stock_klines`` 同一张表、同一组字段、同一种格式。

    前复权视图里没有行（缺复权因子）的股票退回原始日线，和单只加载的兜底一致。
    """
    normalized_symbols = list(dict.fromkeys(
        normalized for symbol in symbols if (normalized := normalize_a_stock_symbol(symbol))
    ))
    result: Dict[str, List[Dict[str, Any]]] = {symbol: [] for symbol in normalized_symbols}
    if not normalized_symbols or start_date > end_date:
        return result

    def query(table_name: str, volume_column: str, turnover_column: str, chunk: Sequence[str]):
        params = {f"symbol_{index}": symbol for index, symbol in enumerate(chunk)}
        placeholders = ",".join(f":{key}" for key in params)
        return db.execute(text(f"""
            SELECT ts_code, trade_date, open, high, low, close,
                   {volume_column} AS volume, {turnover_column} AS turnover, turnover_rate
            FROM {table_name}
            WHERE ts_code IN ({placeholders})
              AND trade_date >= :start_date
              AND trade_date <= :end_date
            ORDER BY ts_code, trade_date
        """), {**params, "start_date": start_date, "end_date": end_date}).mappings().all()

    for offset in range(0, len(normalized_symbols), 500):
        chunk = normalized_symbols[offset:offset + 500]
        for row in query("a_stock_market_daily_qfq", "volume", "turnover", chunk):
            payload = _a_stock_kline_payload(row)
            if payload is not None:
                result[str(row.get("ts_code") or "").upper()].append(payload)
        missing = [symbol for symbol in chunk if not result[symbol]]
        if missing:
            for row in query("a_stock_market_daily", "vol", "amount", missing):
                payload = _a_stock_kline_payload(row)
                if payload is not None:
                    result[str(row.get("ts_code") or "").upper()].append(payload)
    return result




def search_a_stock_consensus_candidates(
    db: Any,
    *,
    symbol: Optional[str] = None,
    min_market_cap_100m: Optional[float] = 100.0,
    max_market_cap_100m: Optional[float] = None,
    min_undervalue_pct: Optional[float] = 10.0,
    min_growth_pct: Optional[float] = 10.0,
    min_organization_count: int = 1,
    stale_filter: Optional[str] = None,
    limit: int = 200,
    pe_bands: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """pe_bands 为预先算好的前瞻 PE 通道(开启"PE 通道补估值"时传入)。"""
    latest_trade_date = _row_to_date(
        db.execute(text("SELECT MAX(trade_date) FROM a_stock_market_daily")).scalar()
    )
    if latest_trade_date is None:
        return []

    search_text = str(symbol or "").strip()
    search_symbol = normalize_a_stock_symbol(search_text) if _looks_like_a_stock_code(search_text) else ""
    name_search = search_text if search_text and not search_symbol else ""
    has_search = bool(search_text)
    search_filter = ""
    if search_symbol:
        search_filter = "AND r.ts_code = :symbol"
    elif name_search:
        search_filter = "AND (b.name LIKE :name_pattern OR r.name LIKE :name_pattern)"
    # 研报候选池最远只退到 T-1 期披露日，更早的研报不会进任何池子，不必取。
    rows = db.execute(
        text(
            f"""
            WITH latest_market AS (
                SELECT *
                FROM a_stock_market_daily
                WHERE trade_date = :latest_trade_date
            )
            SELECT
                {_REPORT_COLUMNS},
                b.name AS stock_name,
                b.industry,
                b.market,
                m.trade_date,
                m.close,
                m.total_mv,
                m.circ_mv
            FROM a_stock_report_rc r
            JOIN latest_market m ON m.ts_code = r.ts_code
            LEFT JOIN a_stock_basic b ON b.ts_code = r.ts_code
            WHERE r.report_date BETWEEN :report_start AND :latest_trade_date
              {search_filter}
            ORDER BY r.ts_code, r.report_date
            """
        ),
        {
            "latest_trade_date": latest_trade_date,
            "report_start": latest_trade_date - timedelta(days=REPORT_FETCH_LOOKBACK_DAYS),
            "symbol": search_symbol,
            "name_pattern": f"%{name_search}%",
        },
    ).mappings().all()
    symbols = sorted({str(row.get("ts_code") or "").strip().upper() for row in rows})
    disclosures = load_a_stock_disclosure_dates(
        db,
        symbols,
        since=latest_trade_date - timedelta(days=DISCLOSURE_FETCH_LOOKBACK_DAYS),
    )
    price_factors = load_a_stock_price_factors(
        db,
        symbols,
        start=latest_trade_date - timedelta(days=REPORT_FETCH_LOOKBACK_DAYS + PRICE_FACTOR_PADDING_DAYS),
        end=latest_trade_date,
    )
    return build_a_stock_consensus_candidates(
        rows,
        latest_trade_date,
        disclosures=disclosures,
        price_factors=price_factors,
        pe_bands=pe_bands,
        search_symbol=search_symbol,
        has_search=has_search,
        min_market_cap_100m=min_market_cap_100m,
        max_market_cap_100m=max_market_cap_100m,
        min_undervalue_pct=min_undervalue_pct,
        min_growth_pct=min_growth_pct,
        min_organization_count=min_organization_count,
        stale_filter=stale_filter,
        limit=limit,
    )

"""K 线图上的事件标记：卖方研报与定期报告披露。

这里不重新定义任何口径，全部复用已有的规范实现：

- 研报走 `a_stock_consensus` 的 `_load_report_rows_by_symbol` 与 `_normalize_forecast_rows`
  ——同一篇研报的认定键(`report_key`)、机构别名归一、写研报时(前一交易日)的复权因子，
  都和 K 线上的估值上下限是同一套。K 线是前复权的，研报目标价和 EPS 必须按同一个
  复权因子换算，否则点开弹窗看到的目标价会和图上的价位差出一次送转。
- 财报标记用 `load_a_stock_disclosure_dates` 的**首次**披露日(`MIN(ann_date)`)。
  `load_a_stock_financials` 为了取最新数字会保留公告日最晚的那条(更正稿)，拿它定位
  标记的话，一份被更正过的年报会被标在更正日而不是市场第一次看到它的那天。
  数值再从 `load_a_stock_financials` 取，和个股详情页的财务数据卡片一致。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .a_stock_consensus import (
    _load_report_rows_by_symbol,
    _normalize_forecast_rows,
    _normalize_org_name,
    _price_scale,
    _row_to_date,
    _target_price_bounds,
    load_a_stock_disclosure_dates,
    load_a_stock_price_factors,
    normalize_a_stock_symbol,
)
from .a_stock_financials import _period_label, load_a_stock_financials

# 5 年窗口约 20 个报告期，多取几期保证窗口最前面那几次披露也有数值
FINANCIAL_PERIODS_TO_LOAD = 28
# 复权因子多往前取一段，保证窗口里第一篇研报的"前一交易日"也有因子
PRICE_FACTOR_LOOKBACK_DAYS = 30
# 披露日按报告期末筛选，报告期末要比窗口起点更早才能覆盖窗口里的第一次披露
DISCLOSURE_LOOKBACK_DAYS = 400


def _rounded(value: Optional[float], digits: int = 2) -> Optional[float]:
    return round(value, digits) if value is not None else None


def _research_days(db: Any, symbol: str, start: date, end: date) -> List[Dict[str, Any]]:
    rows = _load_report_rows_by_symbol(db, [symbol], report_start=start, report_end=end).get(symbol, [])
    if not rows:
        return []
    factors = load_a_stock_price_factors(
        db, [symbol], start=start - timedelta(days=PRICE_FACTOR_LOOKBACK_DAYS), end=end
    ).get(symbol)
    latest_factor = factors.latest if factors else None

    # 按研报分组：一篇研报在 report_rc 里是按预测年份拆成多行的，标记上的数字要数
    # "几篇研报"而不是"几行"。分组键和 _ForecastRow.report_key 保持一致。
    reports: Dict[Tuple[date, str, str, str], Dict[str, Any]] = {}
    for row in rows:
        report_date = _row_to_date(row.get("report_date"))
        if report_date is None:
            continue
        raw_org_name = str(row.get("org_name") or "").strip()
        key = (
            report_date,
            _normalize_org_name(raw_org_name),
            str(row.get("author_name") or "").strip(),
            str(row.get("report_title") or "").strip(),
        )
        entry = reports.get(key)
        if entry is None:
            source_factor = factors.on_or_before(report_date - timedelta(days=1)) if factors else None
            entry = reports[key] = {
                "report_date": report_date,
                "org_name": key[1] or "未知机构",
                "raw_org_name": raw_org_name,
                "author_name": key[2],
                "report_title": key[3],
                "rating": "",
                "_bounds": None,
                "_scale": _price_scale(source_factor, latest_factor),
                "_forecasts": {},
            }
        if not entry["rating"]:
            entry["rating"] = str(row.get("rating") or "").strip()
        bounds = _target_price_bounds(row)
        if bounds:
            low, high = entry["_bounds"] or bounds
            entry["_bounds"] = (min(low, bounds[0]), max(high, bounds[1]))

    # 盈利预测走规范化：只认全年预测(YYYYQ4)，EPS 按写研报时的复权因子换算到前复权口径
    for record in _normalize_forecast_rows(rows, factors):
        entry = reports.get(record.report_key)
        if entry is None:
            continue
        scale = _price_scale(record.price_factor, latest_factor)
        entry["_forecasts"][record.fiscal_year] = {
            "fiscal_year": record.fiscal_year,
            "eps": _rounded(record.eps * scale, 3) if record.eps is not None else None,
            "eps_raw": record.eps,
            "np": record.np,  # 研报预测净利润，单位万元
            "pe": record.pe,
        }

    by_day: Dict[date, List[Dict[str, Any]]] = defaultdict(list)
    for entry in reports.values():
        bounds = entry.pop("_bounds")
        scale = entry.pop("_scale")
        forecasts = entry.pop("_forecasts")
        report_date = entry.pop("report_date")
        entry.update({
            "target_low": _rounded(bounds[0] * scale) if bounds else None,
            "target_high": _rounded(bounds[1] * scale) if bounds else None,
            "target_low_raw": bounds[0] if bounds else None,
            "target_high_raw": bounds[1] if bounds else None,
            # ≠1 说明写研报之后发生过除权，目标价已换算到当前前复权口径
            "price_scale": _rounded(scale, 4),
            "forecasts": [forecasts[year] for year in sorted(forecasts)],
        })
        by_day[report_date].append(entry)

    return [
        {
            "date": day.isoformat(),
            "count": len(items),
            "reports": sorted(items, key=lambda item: (item["org_name"], item["report_title"])),
        }
        for day, items in sorted(by_day.items())
    ]


def _financial_reports(db: Any, symbol: str, start: date, end: date) -> List[Dict[str, Any]]:
    disclosures = load_a_stock_disclosure_dates(
        db, [symbol], since=start - timedelta(days=DISCLOSURE_LOOKBACK_DAYS)
    ).get(symbol, [])
    in_window = [(disclosed, period) for disclosed, period in disclosures if start <= disclosed <= end]
    if not in_window:
        return []
    values_by_period = {
        period["end_date"]: period
        for period in load_a_stock_financials(symbol, periods=FINANCIAL_PERIODS_TO_LOAD)["periods"]
    }

    events = []
    for disclosed, period in in_window:
        values = values_by_period.get(period.isoformat())
        income = values["income"] if values else {}
        indicator = values["indicator"] if values else {}
        cashflow = values["cashflow"] if values else {}
        events.append({
            "date": disclosed.isoformat(),
            "end_date": period.isoformat(),
            "period_label": _period_label(period),
            "is_annual": (period.month, period.day) == (12, 31),
            "has_values": values is not None,
            "revenue": income.get("revenue") if income.get("revenue") is not None else income.get("total_revenue"),
            "revenue_yoy": indicator.get("or_yoy"),
            "n_income_attr_p": income.get("n_income_attr_p"),
            "netprofit_yoy": indicator.get("netprofit_yoy"),
            "profit_dedt": indicator.get("profit_dedt"),
            "dt_netprofit_yoy": indicator.get("dt_netprofit_yoy"),
            "grossprofit_margin": indicator.get("grossprofit_margin"),
            "netprofit_margin": indicator.get("netprofit_margin"),
            "roe_waa": indicator.get("roe_waa"),
            "eps": indicator.get("eps"),
            "n_cashflow_act": cashflow.get("n_cashflow_act"),
        })
    return events


def load_a_stock_chart_events(db: Any, symbol: str, *, start: date, end: date) -> Dict[str, Any]:
    """返回 [start, end] 区间内的研报(按研报日期分组)与定期报告首次披露事件。

    事件日期是原始日期；周末或盘后发布的，由前端对齐到下一个交易日再画——
    对齐规则只和 K 线本身有关，放在画图的地方做，接口保持事实原样。
    """
    normalized = normalize_a_stock_symbol(symbol)
    if not normalized:
        return {"ts_code": "", "research_days": [], "financial_reports": []}
    return {
        "ts_code": normalized,
        "research_days": _research_days(db, normalized, start, end),
        "financial_reports": _financial_reports(db, normalized, start, end),
    }

"""K 线图上的事件标记：卖方研报、定期报告、业绩快报与业绩预告。

这里不重新定义任何口径，全部复用已有的规范实现：

- 研报走 `a_stock_consensus` 的 `_load_report_rows_by_symbol` 与 `_normalize_forecast_rows`
  ——同一篇研报的认定键(`report_key`)、机构别名归一、写研报时(前一交易日)的复权因子，
  都和 K 线上的估值上下限是同一套。K 线是前复权的，研报目标价和 EPS 必须按同一个
  复权因子换算，否则点开弹窗看到的目标价会和图上的价位差出一次送转。
- 财报标记用 `load_a_stock_disclosure_dates` 的**首次**披露日(`MIN(ann_date)`)。
  `load_a_stock_financials` 为了取最新数字会保留公告日最晚的那条(更正稿)，拿它定位
  标记的话，一份被更正过的年报会被标在更正日而不是市场第一次看到它的那天。
  数值再从 `load_a_stock_financials` 取，和个股详情页的财务数据卡片一致。
- 快报/预告直接读 `a_stock_express` / `a_stock_forecast` 原表，**每条公告一个事件**。
  这两张表和财报不一样：预告的"修正公告"是带新数字的新信息（不是对同一份文档的技术性
  更正），市场会针对修正日再反应一次，所以按公告日各标一个而不是只留首次；
  `first_ann_date` 一并给出，界面据此区分"首次预告"和"修正预告"。
  快报的净利同比走 `earnings_gap.express_np_yoy`，和净利润断层策略同一个算法，
  保证图上点开看到的同比和策略当时用的那个数是同一个。
- 每篇研报的每个预测年度附带「较上次」与历次预测（见 `_attach_eps_revisions`）。

事件日期一律是**原始公告日**（tushare 的 `ann_date`，已经按交易所口径跨日：
盘后披露的公告日本身就是次一交易日）。周末/盘后发布的由前端对齐到"市场第一次能
对它做出反应的"那根 K 线，对齐规则只和 K 线本身有关，接口保持事实原样。
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import text

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
from .duckdb_analytics import safe_float
from .earnings_gap import express_np_yoy

# 5 年窗口约 20 个报告期，多取几期保证窗口最前面那几次披露也有数值
FINANCIAL_PERIODS_TO_LOAD = 28
# 复权因子多往前取一段，保证窗口里第一篇研报的"前一交易日"也有因子
PRICE_FACTOR_LOOKBACK_DAYS = 30
# 披露日按报告期末筛选，报告期末要比窗口起点更早才能覆盖窗口里的第一次披露
DISCLOSURE_LOOKBACK_DAYS = 400
# 找"上一篇"时往窗口前多取的天数：窗口里最早那几篇研报，它们的上一篇多半在窗口之外
REVISION_LOOKBACK_DAYS = 730
# 每个预测年度展开的历次预测最多带几篇（取最近的），控制接口体积
REVISION_HISTORY_LIMIT = 12

# 快报/预告原表的取数列。只读展示要用的那些，不 SELECT *：
# 快报有 30+ 列（含一堆和去年同期的对比原始值），全带出去接口会白胖一圈。
EXPRESS_COLUMNS = (
    "ann_date", "end_date", "is_audit", "revenue", "operate_profit", "total_profit",
    "n_income", "total_assets", "total_hldr_eqy_exc_min_int", "diluted_eps",
    "diluted_roe", "yoy_net_profit", "bps", "yoy_sales", "yoy_dedu_np",
    "perf_summary", "remark",
)
FORECAST_COLUMNS = (
    "ann_date", "first_ann_date", "end_date", "type", "p_change_min", "p_change_max",
    "net_profit_min", "net_profit_max", "last_parent_net", "summary", "change_reason",
)

_AUTHOR_SEPARATORS = re.compile(r"[,，、;；/\s]+")


def _rounded(value: Optional[float], digits: int = 2) -> Optional[float]:
    return round(value, digits) if value is not None else None


def _authors(author_name: str) -> frozenset:
    """研报作者串（"刘俊,边文姣,邵梓洋"）拆成人名集合，用来判断两篇是否有共同分析师。"""
    return frozenset(name for name in _AUTHOR_SEPARATORS.split(author_name or "") if name)


def _change_pct(current: Optional[float], previous: Optional[float]) -> Optional[float]:
    if current is None or previous is None or previous == 0:
        return None
    return round((current - previous) / abs(previous) * 100.0, 2)


def _disclosed_between(
    disclosures: List[Tuple[date, date]],
    after: date,
    upto: date,
) -> List[str]:
    """(after, upto] 之间首次披露的定期报告，如 ["2025年报", "2026中报"]。"""
    return [_period_label(period) for disclosed, period in disclosures if after < disclosed <= upto]


def _attach_eps_revisions(entries, disclosures: List[Tuple[date, date]]) -> None:
    """给每篇研报的每个预测年度补上「较上次」(`revision`)和历次预测(`history`)。

    比对对象：同一机构、同一预测年度、给了 EPS 的更早研报。优先取与本篇**至少有一位相同
    分析师**的最近一篇（团队常有增减人，按作者串完全相等会把真正的"同一批人上次的预测"
    漏掉）；一篇都没有时退到同机构的最近一篇，并标 `match="org"`，让界面能区分"同一批人
    改了预测"和"换了人给的新口径"。

    先后顺序用 `_ForecastRow.recency`（研报日期 + 入库时间），和估值池"同机构取最新"同一个
    排序，同一天的两篇也能分出先后。EPS 都已按写研报时的复权因子换算到前复权口径——比
    原始 EPS 的话，一次 10 送 5 会凭空显示成下调三分之一。
    """
    chains: Dict[Tuple[str, int], List[Tuple[Any, Dict[str, Any], Dict[str, Any]]]] = defaultdict(list)
    for entry in entries:
        for fiscal_year, forecast in entry["_forecasts"].items():
            forecast["revision"] = None
            forecast["history"] = []
            if forecast["eps"] is not None and entry["_recency"] is not None:
                chains[(entry["org_name"], fiscal_year)].append((entry["_recency"], entry, forecast))

    for chain in chains.values():
        chain.sort(key=lambda item: item[0])
        for index, (_, entry, forecast) in enumerate(chain):
            earlier = chain[:index]
            previous = next(
                (item for item in reversed(earlier) if item[1]["_authors"] & entry["_authors"]),
                None,
            )
            match = "analyst"
            if previous is None and earlier:
                previous, match = earlier[-1], "org"
            if previous is not None:
                previous_entry, previous_forecast = previous[1], previous[2]
                forecast["revision"] = {
                    "prev_date": previous_entry["report_date"].isoformat(),
                    "prev_eps": previous_forecast["eps"],
                    "prev_eps_raw": previous_forecast["eps_raw"],
                    "prev_authors": previous_entry["author_name"],
                    "change_pct": _change_pct(forecast["eps"], previous_forecast["eps"]),
                    "match": match,
                    "disclosed_between": _disclosed_between(
                        disclosures, previous_entry["report_date"], entry["report_date"]
                    ),
                }

            history = []
            for position, (_, other_entry, other_forecast) in enumerate(chain[: index + 1]):
                before = chain[position - 1] if position else None
                history.append({
                    "report_date": other_entry["report_date"].isoformat(),
                    "author_name": other_entry["author_name"],
                    "eps": other_forecast["eps"],
                    "eps_raw": other_forecast["eps_raw"],
                    "rating": other_entry["rating"],
                    "target_low": other_entry["target_low"],
                    "target_high": other_entry["target_high"],
                    # 与当前这篇是否有共同分析师：界面上换了人的点画成空心灰点
                    "shares_analyst": bool(other_entry["_authors"] & entry["_authors"]),
                    "change_pct": _change_pct(other_forecast["eps"], before[2]["eps"]) if before else None,
                    "disclosed_between": (
                        _disclosed_between(disclosures, before[1]["report_date"], other_entry["report_date"])
                        if before else []
                    ),
                })
            forecast["history"] = history[-REVISION_HISTORY_LIMIT:]


def _research_days(
    db: Any,
    symbol: str,
    start: date,
    end: date,
    disclosures: Optional[List[Tuple[date, date]]] = None,
) -> List[Dict[str, Any]]:
    # 多往前取一段研报只用来找"上一篇"，输出仍只含 [start, end] 内的研报
    lookback_start = start - timedelta(days=REVISION_LOOKBACK_DAYS)
    rows = _load_report_rows_by_symbol(db, [symbol], report_start=lookback_start, report_end=end).get(symbol, [])
    if not rows:
        return []
    factors = load_a_stock_price_factors(
        db, [symbol], start=lookback_start - timedelta(days=PRICE_FACTOR_LOOKBACK_DAYS), end=end
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
                "_recency": None,
                "_authors": _authors(key[2]),
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
        if entry["_recency"] is None or record.recency > entry["_recency"]:
            entry["_recency"] = record.recency
        entry["_forecasts"][record.fiscal_year] = {
            "fiscal_year": record.fiscal_year,
            "eps": _rounded(record.eps * scale, 3) if record.eps is not None else None,
            "eps_raw": record.eps,
            "np": record.np,  # 研报预测净利润，单位万元
            "pe": record.pe,
        }

    for entry in reports.values():
        bounds = entry.pop("_bounds")
        scale = entry.pop("_scale")
        entry.update({
            "target_low": _rounded(bounds[0] * scale) if bounds else None,
            "target_high": _rounded(bounds[1] * scale) if bounds else None,
            "target_low_raw": bounds[0] if bounds else None,
            "target_high_raw": bounds[1] if bounds else None,
            # ≠1 说明写研报之后发生过除权，目标价已换算到当前前复权口径
            "price_scale": _rounded(scale, 4),
        })
    _attach_eps_revisions(reports.values(), disclosures or [])

    by_day: Dict[date, List[Dict[str, Any]]] = defaultdict(list)
    for entry in reports.values():
        if entry["report_date"] < start:
            continue  # 回看区间里的研报只作为"上一篇"参与比对，不单独出现在图上
        forecasts = entry.pop("_forecasts")
        entry.pop("_recency")
        entry.pop("_authors")
        report_date = entry.pop("report_date")
        entry["forecasts"] = [forecasts[year] for year in sorted(forecasts)]
        by_day[report_date].append(entry)

    return [
        {
            "date": day.isoformat(),
            "count": len(items),
            "reports": sorted(items, key=lambda item: (item["org_name"], item["report_title"])),
        }
        for day, items in sorted(by_day.items())
    ]


def _financial_reports(
    db: Any,
    symbol: str,
    start: date,
    end: date,
    disclosures: List[Tuple[date, date]],
) -> List[Dict[str, Any]]:
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


def _table_exists(db: Any, table: str) -> bool:
    """存量分析库可能还没有快报/预告表（同步任务没跑过），缺表时按"没有事件"处理。"""
    row = db.execute(
        text("SELECT COUNT(*) FROM information_schema.tables WHERE table_name = :name"),
        {"name": table},
    ).fetchone()
    return bool(row and row[0])


def _announcement_rows(
    db: Any,
    table: str,
    columns: Sequence[str],
    symbol: str,
    start: date,
    end: date,
) -> List[Any]:
    """按公告日取窗口内的公告行。用 ann_date 而不是报告期末筛，和研报/财报标记同一口径：
    标记画在"什么时候公告的"，不是"属于哪一期"。"""
    if not _table_exists(db, table):
        return []
    return db.execute(
        text(
            f"""
            SELECT {", ".join(columns)}
            FROM {table}
            WHERE ts_code = :symbol
              AND ann_date IS NOT NULL
              AND end_date IS NOT NULL
              AND ann_date BETWEEN :start AND :end
            ORDER BY ann_date, end_date
            """
        ),
        {"symbol": symbol, "start": start, "end": end},
    ).mappings().all()


def _express_reports(db: Any, symbol: str, start: date, end: date) -> List[Dict[str, Any]]:
    """业绩快报标记（a_stock_express）。数值按公告原样给出，单位是元。

    「净利同比」复用 `express_np_yoy`（净利润 ÷ 去年同期修正后净利润 − 1，基数 ≤ 0 时
    退回接口的扣非同比）——和净利润断层的快报事件同一个算法，图上看到的百分比就是
    策略当时用的那个数，不会出现"图上有信号、数字对不上"的分叉。
    """
    events = []
    for row in _announcement_rows(db, "a_stock_express", EXPRESS_COLUMNS, symbol, start, end):
        announced = _row_to_date(row.get("ann_date"))
        period = _row_to_date(row.get("end_date"))
        if announced is None or period is None:
            continue
        n_income = safe_float(row.get("n_income"))
        events.append({
            "date": announced.isoformat(),
            "end_date": period.isoformat(),
            "period_label": _period_label(period),
            "is_annual": (period.month, period.day) == (12, 31),
            # "1"=已审计、"0"=未审计、"2"=无此项，原样带出去由界面翻译
            "is_audit": (str(row.get("is_audit")).strip() or None) if row.get("is_audit") is not None else None,
            "revenue": safe_float(row.get("revenue")),
            "revenue_yoy": _rounded(safe_float(row.get("yoy_sales"))),
            "operate_profit": safe_float(row.get("operate_profit")),
            "total_profit": safe_float(row.get("total_profit")),
            "n_income": n_income,
            "last_year_n_income": safe_float(row.get("yoy_net_profit")),
            "netprofit_yoy": _rounded(express_np_yoy(n_income, row.get("yoy_net_profit"), row.get("yoy_dedu_np"))),
            "total_assets": safe_float(row.get("total_assets")),
            "total_hldr_eqy_exc_min_int": safe_float(row.get("total_hldr_eqy_exc_min_int")),
            "diluted_eps": safe_float(row.get("diluted_eps")),
            "diluted_roe": _rounded(safe_float(row.get("diluted_roe"))),
            "bps": safe_float(row.get("bps")),
            "perf_summary": row.get("perf_summary"),
            "remark": row.get("remark"),
        })
    return events


def _forecast_reports(db: Any, symbol: str, start: date, end: date) -> List[Dict[str, Any]]:
    """业绩预告标记（a_stock_forecast）。净利润与变动幅度都是 tushare 原样口径：
    金额单位**万元**，`p_change_min/max` 是净利同比的下限/上限（%）。

    同一报告期可能公告多次（首次预告 + 后续修正），每次公告都是当天的独立信息，
    所以各标一个；`first_ann_date` 一起带上，界面据此标出"修正预告"。
    """
    events = []
    for row in _announcement_rows(db, "a_stock_forecast", FORECAST_COLUMNS, symbol, start, end):
        announced = _row_to_date(row.get("ann_date"))
        period = _row_to_date(row.get("end_date"))
        if announced is None or period is None:
            continue
        first_announced = _row_to_date(row.get("first_ann_date"))
        events.append({
            "date": announced.isoformat(),
            "end_date": period.isoformat(),
            "first_ann_date": first_announced.isoformat() if first_announced else None,
            "is_correction": bool(first_announced and first_announced < announced),
            "period_label": _period_label(period),
            "is_annual": (period.month, period.day) == (12, 31),
            "forecast_type": (str(row.get("type")).strip() or None) if row.get("type") is not None else None,
            "p_change_min": _rounded(safe_float(row.get("p_change_min"))),
            "p_change_max": _rounded(safe_float(row.get("p_change_max"))),
            "net_profit_min": safe_float(row.get("net_profit_min")),  # 万元
            "net_profit_max": safe_float(row.get("net_profit_max")),  # 万元
            "last_parent_net": safe_float(row.get("last_parent_net")),  # 万元
            "summary": row.get("summary"),
            "change_reason": row.get("change_reason"),
        })
    return events


def load_a_stock_chart_events(db: Any, symbol: str, *, start: date, end: date) -> Dict[str, Any]:
    """返回 [start, end] 区间内的研报(按研报日期分组)与三类业绩公告事件。

    - `financial_reports`：定期报告(财报)首次披露；
    - `express_reports`：业绩快报，按快报公告日；
    - `forecast_reports`：业绩预告，按预告公告日(修正预告单独成条)。

    事件日期是原始日期；周末或盘后发布的，由前端对齐到下一个交易日再画——
    对齐规则只和 K 线本身有关，放在画图的地方做，接口保持事实原样。
    """
    normalized = normalize_a_stock_symbol(symbol)
    if not normalized:
        return {
            "ts_code": "",
            "research_days": [],
            "financial_reports": [],
            "express_reports": [],
            "forecast_reports": [],
        }
    # 披露日只取一次：财报标记用窗口内那部分，研报修正用它标"两次预测之间披露了哪份财报"
    disclosures = load_a_stock_disclosure_dates(
        db,
        [normalized],
        since=start - timedelta(days=REVISION_LOOKBACK_DAYS + DISCLOSURE_LOOKBACK_DAYS),
    ).get(normalized, [])
    return {
        "ts_code": normalized,
        "research_days": _research_days(db, normalized, start, end, disclosures),
        "financial_reports": _financial_reports(db, normalized, start, end, disclosures),
        "express_reports": _express_reports(db, normalized, start, end),
        "forecast_reports": _forecast_reports(db, normalized, start, end),
    }


def load_a_stock_eps_revisions(
    db: Any,
    symbol: str,
    *,
    start: date,
    end: date,
) -> Dict[Tuple[str, str, str, str], Dict[int, Optional[Dict[str, Any]]]]:
    """按研报 (日期, 机构, 作者, 标题) 索引每个预测年度的「较上次」。

    直接跑 K 线研报侧栏同一条流水线（`_research_days` → `_attach_eps_revisions`），
    其他地方（如卖方一致预期估值弹窗）要显示修正时用它，和侧栏永远是同一个数：
    匹配规则、前复权口径、同日排序、回看区间都不会分叉。
    """
    normalized = normalize_a_stock_symbol(symbol)
    if not normalized:
        return {}
    disclosures = load_a_stock_disclosure_dates(
        db,
        [normalized],
        since=start - timedelta(days=REVISION_LOOKBACK_DAYS + DISCLOSURE_LOOKBACK_DAYS),
    ).get(normalized, [])
    index: Dict[Tuple[str, str, str, str], Dict[int, Optional[Dict[str, Any]]]] = {}
    for day in _research_days(db, normalized, start, end, disclosures):
        for report in day["reports"]:
            key = (day["date"], report["org_name"], report["author_name"], report["report_title"])
            index[key] = {forecast["fiscal_year"]: forecast["revision"] for forecast in report["forecasts"]}
    return index


def attach_consensus_eps_revisions(db: Any, symbol: str, detail: Dict[str, Any]) -> Dict[str, Any]:
    """给卖方一致预期估值详情里每家机构的盈利预测指引补上「较上次」(`forecast["revision"]`)。

    机构视图里的研报标识（研报日期、别名归一后的机构名、作者、标题）和研报侧栏的研报键是
    同一套，按它对上即可。找不到这篇研报时 revision 为 None（前端显示为没有可比的上一篇）。
    """
    organizations = (detail or {}).get("organizations") or []
    report_days = [day for day in (_row_to_date(org.get("report_date")) for org in organizations) if day]
    if not report_days:
        return detail
    end = _row_to_date(detail.get("as_of")) or max(report_days)
    revisions = load_a_stock_eps_revisions(db, symbol, start=min(report_days), end=max(end, max(report_days)))
    for org in organizations:
        day = _row_to_date(org.get("report_date"))
        key = (
            day.isoformat() if day else "",
            org.get("org_name") or "",
            str(org.get("author_name") or "").strip(),
            str(org.get("report_title") or "").strip(),
        )
        by_year = revisions.get(key, {})
        for forecast in org.get("forecasts") or []:
            forecast["revision"] = by_year.get(forecast.get("fiscal_year"))
    return detail

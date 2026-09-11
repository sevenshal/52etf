"""个股三张表核心数据 + 盈利能力指标，供个股详情页展示。

和 `value_investing_scanner` 的分工：那边是**算**（质量闸门、DCF、反推市场隐含假设），
这里只是**取**——把利润表/资产负债表/现金流量表/财务指标里最常看的那几十个字段按报告期
原样读出来。详情页上光有算出来的估值不够用，人要先能看见财报本身长什么样。

口径提示（前端必须显示，否则会被误读）：A股定期报告的利润表和现金流量表是**年初至今
累计**口径，所以"2026中报"那一列是上半年累计数，不是全年；财务指标里的比率
（ROE/净利率等）同理是该报告期的口径，不是年化值。需要滚动12个月的口径时，用的是
`value_investing_scanner` 里那套 TTM 覆盖层，不是这里。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .duckdb_analytics import connect_analytics_db, duckdb_table_exists, safe_float

DEFAULT_PERIOD_COUNT = 8

# 利润表：从营业总收入一路到归母净利润，中间的成本费用项按利润表本身的顺序排
INCOME_CORE_COLUMNS = (
    "total_revenue",       # 营业总收入
    "revenue",             # 营业收入
    "oper_cost",           # 营业成本
    "sell_exp",            # 销售费用
    "admin_exp",           # 管理费用
    "rd_exp",              # 研发费用
    "fin_exp",             # 财务费用
    "operate_profit",      # 营业利润
    "total_profit",        # 利润总额
    "income_tax",          # 所得税费用
    "n_income",            # 净利润(含少数股东)
    "n_income_attr_p",     # 归属母公司净利润
    "minority_gain",       # 少数股东损益
    "ebit",
    "ebitda",
)
BALANCESHEET_CORE_COLUMNS = (
    "total_assets",
    "total_liab",
    "total_cur_assets",
    "total_cur_liab",
    "money_cap",                    # 货币资金
    "accounts_receiv",              # 应收账款
    "inventories",                  # 存货
    "fix_assets",                   # 固定资产
    "cip",                          # 在建工程
    "goodwill",                     # 商誉
    "total_hldr_eqy_exc_min_int",   # 归母权益
    "minority_int",                 # 少数股东权益
    "st_borr",
    "lt_borr",
    "bond_payable",
)
CASHFLOW_CORE_COLUMNS = (
    "n_cashflow_act",           # 经营活动现金流净额
    "n_cashflow_inv_act",       # 投资活动现金流净额
    "n_cash_flows_fnc_act",     # 筹资活动现金流净额
    "c_pay_acq_const_fiolta",   # 购建固定/无形/其他长期资产支付的现金(资本开支)
    "depr_fa_coga_dpba",        # 固定资产折旧
    "amort_intang_assets",      # 无形资产摊销
    "free_cashflow",            # tushare 口径自由现金流
    "net_profit",
)
# 盈利能力/营运/偿债/成长，全部来自 fina_indicator
INDICATOR_CORE_COLUMNS = (
    "grossprofit_margin",   # 销售毛利率
    "netprofit_margin",     # 销售净利率
    "roe",                  # 净资产收益率(摊薄，期末净资产口径)
    "roe_waa",              # 净资产收益率(加权平均)
    "roe_dt",               # 净资产收益率(扣非/摊薄)
    "roa",
    "roic",
    "eps",
    "dt_eps",               # 扣非每股收益
    "profit_dedt",          # 扣非归母净利润(元)
    "bps",                  # 每股净资产
    "ocfps",                # 每股经营现金流
    "debt_to_assets",
    "current_ratio",
    "quick_ratio",
    "assets_turn",
    "arturn_days",          # 应收账款周转天数
    "invturn_days",         # 存货周转天数
    "or_yoy",               # 营业收入同比
    "netprofit_yoy",        # 归母净利润同比
    "dt_netprofit_yoy",     # 扣非归母净利润同比
    "ocf_yoy",              # 经营现金流同比
)

_PERIOD_LABELS = {(3, 31): "一季报", (6, 30): "中报", (9, 30): "三季报", (12, 31): "年报"}


def _period_label(end_date) -> str:
    suffix = _PERIOD_LABELS.get((end_date.month, end_date.day), end_date.strftime("%m-%d"))
    return f"{end_date.year}{suffix}"


def _fetch(connection, table: str, columns: Sequence[str], ts_code: str, periods: int,
           annual_only: bool) -> Dict[Any, Dict[str, Any]]:
    """按报告期取最近 `periods` 期，同一报告期只留公告时间最新的一条。

    去重逻辑和扫描器一致：财报会因更正/追溯调整出现同一 end_date 多条记录，
    不去重的话同一期会被重复算成两期。
    """
    if not duckdb_table_exists(connection, table):
        return {}
    available = {
        row[0]
        for row in connection.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
            [table],
        ).fetchall()
    }
    selected = [name for name in dict.fromkeys(("end_date", "ann_date", *columns)) if name in available]
    if "end_date" not in selected:
        return {}
    projection = ", ".join(f'"{name}"' for name in selected)
    annual_clause = " AND strftime(end_date, '%m-%d') = '12-31'" if annual_only else ""
    query = f"""
        WITH deduped AS (
            SELECT {projection},
                   ROW_NUMBER() OVER (PARTITION BY end_date ORDER BY ann_date DESC) AS _rn
            FROM {table}
            WHERE ts_code = ? AND end_date IS NOT NULL{annual_clause}
        )
        SELECT * EXCLUDE (_rn) FROM deduped WHERE _rn = 1
        ORDER BY end_date DESC LIMIT {int(periods)}
    """
    rows = connection.execute(query, [ts_code]).fetchdf().to_dict("records")
    return {row["end_date"]: row for row in rows}


def _values(row: Optional[Dict[str, Any]], columns: Sequence[str]) -> Dict[str, Optional[float]]:
    if not row:
        return {name: None for name in columns}
    return {name: safe_float(row.get(name)) for name in columns}


def load_a_stock_financials(
    ts_code: str,
    *,
    periods: int = DEFAULT_PERIOD_COUNT,
    annual_only: bool = False,
) -> Dict[str, Any]:
    """读取一只A股最近若干报告期的三张表核心数据与盈利能力指标。

    四张表各自按报告期取数后按 end_date 对齐——不能假设四张表的报告期完全一致
    （财务指标表的最新一期有时会比三张报表晚几天入库），对不齐的那一格给 None，
    而不是让整期消失。
    """
    symbol = str(ts_code or "").strip().upper()
    if not symbol:
        return {"ts_code": symbol, "periods": []}

    connection = connect_analytics_db()
    try:
        income = _fetch(connection, "a_stock_income", INCOME_CORE_COLUMNS, symbol, periods, annual_only)
        balancesheet = _fetch(connection, "a_stock_balancesheet", BALANCESHEET_CORE_COLUMNS, symbol, periods, annual_only)
        cashflow = _fetch(connection, "a_stock_cashflow", CASHFLOW_CORE_COLUMNS, symbol, periods, annual_only)
        indicator = _fetch(connection, "a_stock_fina_indicator", INDICATOR_CORE_COLUMNS, symbol, periods, annual_only)
    finally:
        connection.close()

    end_dates = sorted(
        set(income) | set(balancesheet) | set(cashflow) | set(indicator), reverse=True
    )[:periods]

    result_periods: List[Dict[str, Any]] = []
    for end_date in end_dates:
        income_row = income.get(end_date)
        income_values = _values(income_row, INCOME_CORE_COLUMNS)
        revenue = income_values.get("revenue") or income_values.get("total_revenue")
        oper_cost = income_values.get("oper_cost")
        indicator_values = _values(indicator.get(end_date), INDICATOR_CORE_COLUMNS)
        # 毛利/毛利率：财务指标表缺这一格时按利润表自己算，别让最常看的两个数空着
        gross_profit = revenue - oper_cost if revenue is not None and oper_cost is not None else None
        if indicator_values.get("grossprofit_margin") is None and gross_profit is not None and revenue:
            indicator_values["grossprofit_margin"] = safe_float(gross_profit / revenue * 100.0, 2)
        net_profit_parent = income_values.get("n_income_attr_p")
        if indicator_values.get("netprofit_margin") is None and net_profit_parent is not None and revenue:
            indicator_values["netprofit_margin"] = safe_float(net_profit_parent / revenue * 100.0, 2)

        ann_date = None
        for row in (income_row, balancesheet.get(end_date), cashflow.get(end_date), indicator.get(end_date)):
            if row and row.get("ann_date") is not None:
                ann_date = row["ann_date"]
                break

        result_periods.append({
            "end_date": end_date.strftime("%Y-%m-%d"),
            "ann_date": ann_date.strftime("%Y-%m-%d") if ann_date is not None else None,
            "period_label": _period_label(end_date),
            "is_annual": (end_date.month, end_date.day) == (12, 31),
            "income": {**income_values, "gross_profit": gross_profit},
            "balancesheet": _values(balancesheet.get(end_date), BALANCESHEET_CORE_COLUMNS),
            "cashflow": _values(cashflow.get(end_date), CASHFLOW_CORE_COLUMNS),
            "indicator": indicator_values,
        })

    return {"ts_code": symbol, "periods": result_periods}

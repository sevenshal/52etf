"""选股系统第一层：基本面股票池。

流程（全部按 as_of 当时能看到的数据，point-in-time）：

1. 股票池范围：剔除 ST、近 N 日平均市值 / 成交额不达标（见 universe.py）；
2. 硬闸门：只拦明显有问题的股票（最近 12 个月 ROIC 为负、盈利但经营现金流为负、
   负债率/商誉过高、营收大幅萎缩），阈值可配、可逐条关闭，数据缺失不拦；
3. 软评分：估值 / 成长 / 质量 / 预期四组因子，在通过闸门的股票里做截面百分位，
   按可配置权重合成综合分，取前 N 名入池。

估值因子直接复用价值投资扫描器（同一套 DCF 口径，个股详情页的数字和这里一致）与
卖方共识估值（最悲观单家目标价下沿）；成长因子取 tushare 财务指标里的同比字段。

结果按交易日写入 DuckDB 快照表，因子原值单独成列，后续可以按日期做前瞻收益检验。
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

import pandas as pd

from ..duckdb_analytics import connect_analytics_db, duckdb_table_exists, safe_float
from ..value_investing_scanner import _recent_period_rows, screen_value_investing_candidates
from .config import (
    FACTOR_DEFINITIONS,
    GATE_DEFINITIONS,
    GROUP_LABELS,
    load_stock_system_config,
    normalize_stock_system_config,
)
from .storage import clean_record, ensure_tables, replace_trade_date_rows, run_record, snapshot_frame
from .universe import load_universe

logger = logging.getLogger(__name__)

FINA_GROWTH_COLUMNS = ("or_yoy", "dt_netprofit_yoy", "netprofit_yoy", "q_sales_yoy")
BALANCESHEET_COLUMNS = ("goodwill", "total_hldr_eqy_exc_min_int")

# 闸门 → 判断用的指标
GATE_METRICS = {
    "min_latest_roic_pct": "latest_roic_pct",
    "min_latest_roe_financial_pct": "latest_roe_pct",
    "min_ocf_to_np_ttm": "ocf_to_np_ttm",
    "max_debt_to_assets_pct": "debt_to_assets_pct",
    "max_goodwill_to_equity_pct": "goodwill_to_equity_pct",
    "min_revenue_yoy_pct": "revenue_yoy_pct",
}
FACTOR_KEYS = [definition["key"] for definition in FACTOR_DEFINITIONS]
GROUP_KEYS = list(GROUP_LABELS)

SNAPSHOT_COLUMNS = [
    "trade_date", "ts_code", "name", "industry", "is_financial", "close",
    "avg_total_mv_100m", "avg_amount_10k", "gate_passed", "gate_reasons", "gate_notes",
    *FACTOR_KEYS, "debt_to_assets_pct", "goodwill_to_equity_pct", "factor_scores",
    *[f"{group}_score" for group in GROUP_KEYS], "composite_score", "coverage", "pool_rank", "in_pool",
]
_SNAPSHOT_JSON_COLUMNS = ("gate_reasons", "gate_notes", "factor_scores")
_SNAPSHOT_BOOL_COLUMNS = ("is_financial", "gate_passed", "in_pool")
_SNAPSHOT_TEXT_COLUMNS = ("ts_code", "name", "industry")

ConsensusLoader = Callable[[Sequence[str], date], Dict[str, Dict[str, Any]]]


def _default_consensus_loader(symbols: Sequence[str], as_of: date) -> Dict[str, Dict[str, Any]]:
    """卖方共识估值，和 A 股估值列表 / 个股详情同一套聚合口径。"""
    from ...analytics_database import get_analytics_db_ctx
    from ..a_stock_consensus import load_a_stock_consensus_valuation_map

    with get_analytics_db_ctx() as db:
        return load_a_stock_consensus_valuation_map(db, symbols, as_of=as_of)


def _latest_values(connection, table: str, columns: Sequence[str], symbols: Sequence[str], as_of: date) -> Dict[str, Dict[str, Any]]:
    """每只股票截至 as_of 已披露的最新一期报告里的指定列。"""
    if not symbols or not duckdb_table_exists(connection, table):
        return {}
    frame = _recent_period_rows(connection, table, columns, periods=1, symbols=symbols, as_of=as_of)
    if frame.empty:
        return {}
    result = {}
    for record in frame.to_dict("records"):
        result[str(record["ts_code"])] = record
    return result


def _adj_factors_on_or_before(connection, symbols: Sequence[str], day: date) -> Dict[str, float]:
    if not symbols or not duckdb_table_exists(connection, "a_stock_adj_factor"):
        return {}
    placeholders = ", ".join("?" for _ in symbols)
    frame = connection.execute(
        f"""
        SELECT ts_code, arg_max(adj_factor, trade_date) AS adj_factor
        FROM a_stock_adj_factor
        WHERE trade_date <= ? AND trade_date >= ? AND adj_factor > 0 AND ts_code IN ({placeholders})
        GROUP BY ts_code
        """,
        [day, day - timedelta(days=30), *symbols],
    ).fetchdf()
    return {str(row.ts_code): float(row.adj_factor) for row in frame.itertuples() if pd.notna(row.adj_factor)}


def _ratio_pct(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return (numerator / denominator - 1.0) * 100.0


def _consensus_revision_pct(
    now: Optional[Mapping[str, Any]],
    before: Optional[Mapping[str, Any]],
    factor_now: Optional[float],
    factor_before: Optional[float],
) -> Optional[float]:
    """共识目标价中位数的变化。两次估值都是各自当天的原始价格口径，中间有送转除权
    的话要先把旧值换算到今天的口径（raw_今天 = raw_当时 × 复权因子_当时 / 复权因子_今天），
    否则除权造成的机械下跌会被当成"下修"。"""
    mid_now = safe_float((now or {}).get("fair_value_mid"))
    mid_before = safe_float((before or {}).get("fair_value_mid"))
    if mid_now is None or mid_before is None or mid_before <= 0:
        return None
    scale = factor_before / factor_now if factor_now and factor_before else 1.0
    return _ratio_pct(mid_now, mid_before * scale)


def _stock_metrics(
    universe_row: Mapping[str, Any],
    scan_row: Optional[Mapping[str, Any]],
    fina_row: Optional[Mapping[str, Any]],
    balance_row: Optional[Mapping[str, Any]],
    consensus_now: Optional[Mapping[str, Any]],
    consensus_before: Optional[Mapping[str, Any]],
    factor_now: Optional[float],
    factor_before: Optional[float],
) -> Dict[str, Any]:
    scan_row = scan_row or {}
    fina_row = fina_row or {}
    is_financial = bool(scan_row.get("is_financial"))

    pe_ttm = safe_float(scan_row.get("pe_ttm"))
    latest_roic = safe_float(scan_row.get("latest_roic_pct"))
    latest_roe = safe_float(scan_row.get("latest_roe_pct"))

    goodwill_to_equity = None
    if balance_row is not None:
        equity = safe_float(balance_row.get("total_hldr_eqy_exc_min_int"))
        # tushare 没有商誉的公司该列是空而不是 0
        goodwill = safe_float(balance_row.get("goodwill")) or 0.0
        if equity is not None and equity > 0:
            goodwill_to_equity = goodwill / equity * 100.0

    consensus_upside = None
    if consensus_now:
        consensus_upside = _ratio_pct(
            safe_float(consensus_now.get("fair_value_lo")), safe_float(consensus_now.get("last_price"))
        )

    profit_yoy = safe_float(fina_row.get("dt_netprofit_yoy"))
    if profit_yoy is None:
        profit_yoy = safe_float(fina_row.get("netprofit_yoy"))

    return {
        "is_financial": is_financial,
        "has_fundamentals": bool(scan_row) or bool(fina_row),
        "dcf_return_pct": safe_float(scan_row.get("expected_return_pct")),
        "consensus_upside_pct": consensus_upside,
        "earnings_yield_pct": 100.0 / pe_ttm if pe_ttm is not None and pe_ttm > 0 else None,
        "revenue_yoy_pct": safe_float(fina_row.get("or_yoy")),
        "profit_yoy_pct": profit_yoy,
        "quarter_revenue_yoy_pct": safe_float(fina_row.get("q_sales_yoy")),
        "consensus_growth_pct": safe_float((consensus_now or {}).get("growth_pct")),
        "consensus_revision_pct": _consensus_revision_pct(consensus_now, consensus_before, factor_now, factor_before),
        "profitability_pct": latest_roe if is_financial else latest_roic,
        "ocf_to_np_ttm": safe_float(scan_row.get("ocf_to_net_profit_ttm")),
        "latest_roic_pct": latest_roic,
        "latest_roe_pct": latest_roe,
        "debt_to_assets_pct": safe_float(scan_row.get("debt_to_assets_pct")),
        "goodwill_to_equity_pct": goodwill_to_equity,
        "consensus_organization_count": (consensus_now or {}).get("organization_count"),
    }


def _format_value(value: float, unit: str) -> str:
    return f"{value:.2f}{unit}" if unit == "倍" else f"{value:.1f}{unit}"


def evaluate_gates(metrics: Mapping[str, Any], gates: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    """硬闸门：返回 passed / reasons（淘汰理由）/ notes（提示）/ failed_keys。"""
    reasons: List[str] = []
    notes: List[str] = []
    failed_keys: List[str] = []
    if not metrics.get("has_fundamentals"):
        return {"passed": False, "reasons": ["缺少财务数据，无法判断基本面"], "notes": [], "failed_keys": ["no_fundamentals"]}

    is_financial = bool(metrics.get("is_financial"))
    for definition in GATE_DEFINITIONS:
        key = definition["key"]
        setting = gates.get(key) or {}
        if not setting.get("enabled"):
            continue
        applies_to = definition["applies_to"]
        if (applies_to == "financial" and not is_financial) or (applies_to == "non_financial" and is_financial):
            continue
        value = safe_float(metrics.get(GATE_METRICS[key]))
        label = definition["label"]
        if value is None:
            notes.append(f"{label}：{definition.get('missing_note') or '缺少数据，未参与判断'}")
            continue
        threshold = float(setting["threshold"])
        unit = definition["unit"]
        failed = value < threshold if definition["comparator"] == "min" else value > threshold
        if failed:
            failed_keys.append(key)
            reasons.append(f"{label} {threshold:g}{unit}，实际 {_format_value(value, unit)}")
    return {"passed": not reasons, "reasons": reasons, "notes": notes, "failed_keys": failed_keys}


def score_rows(rows: List[Dict[str, Any]], config: Mapping[str, Any]) -> None:
    """在通过闸门的股票里做截面百分位并合成综合分（原地写回 rows）。

    每个因子只在有值的股票里排名（最高 100）；综合分 = 有值因子的加权平均，
    有值因子的权重占全部启用权重的比例（coverage）低于阈值时不给分。
    """
    factor_settings = config["factors"]
    enabled = [
        definition for definition in FACTOR_DEFINITIONS
        if factor_settings[definition["key"]]["enabled"] and factor_settings[definition["key"]]["weight"] > 0
    ]
    total_weight = sum(factor_settings[definition["key"]]["weight"] for definition in enabled)
    min_coverage = float(config["scoring"]["min_factor_coverage"])
    candidates = [row for row in rows if row["gate_passed"]]

    percentiles: Dict[str, Dict[int, float]] = {}
    for definition in enabled:
        key = definition["key"]
        series = pd.Series(
            [row.get(key) for row in candidates], index=range(len(candidates)), dtype="float64"
        ).dropna()
        ranks = series.rank(pct=True, method="average") * 100.0 if not series.empty else series
        percentiles[key] = {int(index): float(value) for index, value in ranks.items()}

    for position, row in enumerate(candidates):
        scores: Dict[str, float] = {}
        group_totals: Dict[str, List[float]] = {group: [0.0, 0.0] for group in GROUP_KEYS}
        available_weight = 0.0
        weighted_sum = 0.0
        for definition in enabled:
            key = definition["key"]
            value = percentiles[key].get(position)
            if value is None:
                continue
            weight = factor_settings[key]["weight"]
            scores[key] = round(value, 2)
            available_weight += weight
            weighted_sum += weight * value
            group_totals[definition["group"]][0] += weight * value
            group_totals[definition["group"]][1] += weight
        row["factor_scores"] = scores
        for group, (group_sum, group_weight) in group_totals.items():
            row[f"{group}_score"] = round(group_sum / group_weight, 2) if group_weight > 0 else None
        coverage = available_weight / total_weight if total_weight > 0 else 0.0
        row["coverage"] = round(coverage, 4)
        row["composite_score"] = (
            round(weighted_sum / available_weight, 2)
            if available_weight > 0 and coverage + 1e-12 >= min_coverage
            else None
        )

    ranked = sorted(
        (row for row in candidates if row.get("composite_score") is not None),
        key=lambda row: (-row["composite_score"], row["ts_code"]),
    )
    pool_size = int(config["pool"]["size"])
    for rank, row in enumerate(ranked, start=1):
        row["pool_rank"] = rank
        row["in_pool"] = rank <= pool_size


def compute_fundamental_pool(
    as_of: Optional[date] = None,
    config: Optional[Mapping[str, Any]] = None,
    *,
    scan_fn: Callable[..., Dict[str, Any]] = screen_value_investing_candidates,
    consensus_loader: Optional[ConsensusLoader] = None,
    connect: Callable[[], Any] = connect_analytics_db,
) -> Dict[str, Any]:
    """算一次基本面股票池（只读，不写库）。"""
    started = time.monotonic()
    config = normalize_stock_system_config(config if config is not None else load_stock_system_config())
    as_of_value = as_of or date.today()
    consensus_loader = consensus_loader or _default_consensus_loader
    lookback_days = int(config["scoring"]["revision_lookback_days"])

    connection = connect()
    try:
        universe = load_universe(connection, as_of_value, config)
        trade_date = universe["trade_date"]
        frame = universe["frame"]
        if trade_date is None or frame.empty:
            return {
                "status": "no_data",
                "message": "分析库里没有 as_of 之前的 A 股行情，无法构建股票池",
                "as_of": as_of_value.isoformat(),
                "trade_date": None,
                "config": config,
                "summary": {},
                "rows": [],
            }
        members = frame[frame["in_universe"]].to_dict("records")
        symbols = [row["ts_code"] for row in members]
        fina_rows = _latest_values(connection, "a_stock_fina_indicator", FINA_GROWTH_COLUMNS, symbols, trade_date)
        balance_rows = _latest_values(connection, "a_stock_balancesheet", BALANCESHEET_COLUMNS, symbols, trade_date)
        lookback_day = trade_date - timedelta(days=lookback_days)
        factors_now = _adj_factors_on_or_before(connection, symbols, trade_date)
        factors_before = _adj_factors_on_or_before(connection, symbols, lookback_day)
    finally:
        connection.close()

    scan: Dict[str, Any] = {}
    if symbols:
        scan = scan_fn(
            as_of=trade_date,
            symbols=symbols,
            force_valuation=True,
            exclude_st=False,
            min_total_mv=None,
            top_n=len(symbols) + 10,
        )
    scan_rows = {
        item["ts_code"]: item
        for item in [*scan.get("candidates", []), *scan.get("excluded_sample", [])]
    }

    revision_enabled = config["factors"]["consensus_revision_pct"]["enabled"]
    consensus_now: Dict[str, Dict[str, Any]] = {}
    consensus_before: Dict[str, Dict[str, Any]] = {}
    if symbols:
        try:
            consensus_now = consensus_loader(symbols, trade_date) or {}
            if revision_enabled:
                consensus_before = consensus_loader(symbols, lookback_day) or {}
        except Exception as exc:  # noqa: BLE001 共识估值缺失不应拖垮整个股票池
            logger.warning("stock system consensus load failed: %s", exc)

    rows: List[Dict[str, Any]] = []
    gate_failures: Dict[str, int] = {}
    for member in members:
        ts_code = member["ts_code"]
        metrics = _stock_metrics(
            member,
            scan_rows.get(ts_code),
            fina_rows.get(ts_code),
            balance_rows.get(ts_code),
            consensus_now.get(ts_code),
            consensus_before.get(ts_code),
            factors_now.get(ts_code),
            factors_before.get(ts_code),
        )
        gate = evaluate_gates(metrics, config["gates"])
        for key in gate["failed_keys"]:
            gate_failures[key] = gate_failures.get(key, 0) + 1
        rows.append({
            "ts_code": ts_code,
            "name": member.get("name"),
            "industry": member.get("industry"),
            "close": safe_float(member.get("close")),
            "avg_total_mv_100m": safe_float(member.get("avg_total_mv_100m"), 2),
            "avg_amount_10k": safe_float(member.get("avg_amount_10k"), 1),
            **metrics,
            "gate_passed": gate["passed"],
            "gate_reasons": gate["reasons"],
            "gate_notes": gate["notes"],
            "factor_scores": {},
            **{f"{group}_score": None for group in GROUP_KEYS},
            "composite_score": None,
            "coverage": None,
            "pool_rank": None,
            "in_pool": False,
        })

    score_rows(rows, config)

    universe_reasons: Dict[str, int] = {}
    for item in frame.itertuples():
        if not item.in_universe:
            universe_reasons[item.universe_reason] = universe_reasons.get(item.universe_reason, 0) + 1
    summary = {
        "universe_total": int(len(frame)),
        "universe_size": len(members),
        "universe_excluded": universe_reasons,
        "gate_passed": sum(1 for row in rows if row["gate_passed"]),
        "gate_failures": gate_failures,
        "scored": sum(1 for row in rows if row["composite_score"] is not None),
        "pool_size": sum(1 for row in rows if row["in_pool"]),
        "valuation_scan_status": scan.get("status"),
        "consensus_covered": sum(1 for symbol in symbols if symbol in consensus_now),
    }
    return {
        "status": "completed",
        "as_of": as_of_value.isoformat(),
        "trade_date": trade_date.isoformat(),
        "config": config,
        "summary": summary,
        "rows": rows,
        "duration_seconds": round(time.monotonic() - started, 1),
    }


# ---------------------------------------------------------------------------
# 快照持久化
# ---------------------------------------------------------------------------

RUN_COLUMNS = [
    "trade_date", "as_of", "status", "message", "universe_total", "universe_size", "gate_passed",
    "scored", "pool_size", "duration_seconds", "config_json", "summary_json", "created_at",
]


def save_pool_snapshot(result: Mapping[str, Any]) -> None:
    """同一交易日重跑时整批覆盖。"""
    if result.get("status") != "completed" or not result.get("trade_date"):
        return
    from ...analytics_database import StockSystemPoolRun, StockSystemPoolSnapshot

    trade_date = date.fromisoformat(result["trade_date"])
    summary = result.get("summary") or {}
    records = [
        {
            **row,
            "trade_date": trade_date,
            "gate_reasons": row.get("gate_reasons") or [],
            "gate_notes": row.get("gate_notes") or [],
            "factor_scores": row.get("factor_scores") or {},
        }
        for row in result["rows"]
    ]
    snapshot = snapshot_frame(
        records,
        SNAPSHOT_COLUMNS,
        text_columns=_SNAPSHOT_TEXT_COLUMNS,
        bool_columns=_SNAPSHOT_BOOL_COLUMNS,
        int_columns=("pool_rank",),
        raw_columns=("trade_date",),
        json_columns=_SNAPSHOT_JSON_COLUMNS,
    )
    run = snapshot_frame(
        [{
            **summary,
            "trade_date": trade_date,
            "as_of": date.fromisoformat(result["as_of"]),
            "status": result["status"],
            "message": result.get("message"),
            "duration_seconds": result.get("duration_seconds"),
            "config_json": result.get("config") or {},
            "summary_json": summary,
            "created_at": datetime.now(),
        }],
        RUN_COLUMNS,
        text_columns=("status", "message"),
        int_columns=("universe_total", "universe_size", "gate_passed", "scored", "pool_size"),
        raw_columns=("trade_date", "as_of", "created_at"),
        json_columns=("config_json", "summary_json"),
    )
    ensure_tables(StockSystemPoolRun, StockSystemPoolSnapshot)
    replace_trade_date_rows(
        trade_date,
        [("stock_system_pool_snapshot", snapshot), ("stock_system_pool_run", run)],
    )


def run_fundamental_pool(as_of: Optional[date] = None) -> Dict[str, Any]:
    """定时任务入口：算一次并落库，返回摘要（不含逐股明细）。"""
    result = compute_fundamental_pool(as_of=as_of)
    save_pool_snapshot(result)
    return {key: value for key, value in result.items() if key != "rows"}


POOL_VIEWS = {
    "pool": "in_pool",
    "passed": "gate_passed",
    "excluded": "NOT gate_passed",
    "all": "TRUE",
}


def load_pool_snapshot(
    trade_date: Optional[date] = None,
    *,
    view: str = "pool",
    connect: Callable[[], Any] = connect_analytics_db,
) -> Dict[str, Any]:
    """读取某个交易日（默认最新）的股票池快照。"""
    where = POOL_VIEWS.get(view, POOL_VIEWS["pool"])
    connection = connect()
    try:
        if not duckdb_table_exists(connection, "stock_system_pool_run"):
            return {"status": "empty", "run": None, "rows": []}
        if trade_date is None:
            run_frame = connection.execute(
                "SELECT * FROM stock_system_pool_run ORDER BY trade_date DESC LIMIT 1"
            ).fetchdf()
        else:
            run_frame = connection.execute(
                "SELECT * FROM stock_system_pool_run WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT 1",
                [trade_date],
            ).fetchdf()
        if run_frame.empty:
            return {"status": "empty", "run": None, "rows": []}
        run = run_frame.to_dict("records")[0]
        rows = connection.execute(
            f"""
            SELECT * FROM stock_system_pool_snapshot
            WHERE trade_date = ? AND {where}
            ORDER BY pool_rank NULLS LAST, composite_score DESC NULLS LAST, ts_code
            """,
            [run["trade_date"]],
        ).fetchdf()
    finally:
        connection.close()

    json_defaults = {"gate_reasons": [], "gate_notes": [], "factor_scores": {}}
    records = [clean_record(row, json_defaults) for row in rows.to_dict("records")]
    return {"status": "completed", "run": run_record(run), "view": view, "rows": records}


def list_pool_runs(limit: int = 30, *, connect: Callable[[], Any] = connect_analytics_db) -> List[Dict[str, Any]]:
    connection = connect()
    try:
        if not duckdb_table_exists(connection, "stock_system_pool_run"):
            return []
        frame = connection.execute(
            "SELECT * FROM stock_system_pool_run ORDER BY trade_date DESC LIMIT ?", [int(limit)]
        ).fetchdf()
    finally:
        connection.close()
    return [run_record(row) for row in frame.to_dict("records")]

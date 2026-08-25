"""雪球年榜组合持仓快照/综合权重/权价比 数据与配置接口。

从 factor_lab.py 拆出的独立模块，避免单文件过大。
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

import polars as pl
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ...core.database import (
    AIStockTHSIndexCache,
    AStockInnovation100Constituent,
    Session as DBSession,
    get_db_ctx,
)
from ...core.services.duckdb_analytics import (
    SYMBOL_PATTERN,
    connect_analytics_db,
    duckdb_query_dicts as _duckdb_query_dicts,
    duckdb_table_exists as _duckdb_table_exists,
    load_price_frame as _load_price_frame,
    safe_float as _safe_float,
)
from ...core.services.factor_backtest_engine import (
    A_STOCK_INNO100_INDEX_CODE,
    A_STOCK_INNO100_SYMBOL,
    normalize_a_stock_symbol,
)
from ...robot.a_stock_base_data_config import A_STOCK_INDEX_FEAR_GREED_TARGETS
from .account import valid_admin_account


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/factor-lab", tags=["Factor Lab · 雪球组合"])



def _connect_duckdb():
    try:
        return connect_analytics_db()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc),
        ) from exc

XUEQIU_TOP_HOLDINGS_SNAPSHOT_TABLE = "xueqiu_cube_holdings_snapshots"
XUEQIU_TOP_HOLDINGS_RANK_COMPARE_TRADING_DAYS = 5
XUEQIU_BOARD_MIN_STOCKS = 3
XUEQIU_CONTRARIAN_BOARD_MIN_STOCKS = 11


def _normalize_xueqiu_snapshot_symbol(symbol: Any) -> str:
    text = str(symbol or "").strip().upper()
    if not text:
        return ""
    if text in {"CASH", "CN_CASH"}:
        return "CASH"
    text = text.replace("_", ".")
    if "." in text:
        left, right = text.split(".", 1)
        if left in {"SH", "SZ", "BJ"} and re.fullmatch(r"\d{6}", right):
            return f"{left}.{right}"
        if right in {"SH", "SZ", "BJ"} and re.fullmatch(r"\d{6}", left):
            return f"{right}.{left}"
        return text
    if len(text) == 8 and text[:2] in {"SH", "SZ", "BJ"} and text[2:].isdigit():
        return f"{text[:2]}.{text[2:]}"
    return text


def _raw_xueqiu_snapshot_symbol(symbol: str) -> str:
    normalized = _normalize_xueqiu_snapshot_symbol(symbol)
    if normalized == "CASH":
        return "CASH"
    return normalized.replace(".", "")


def _xueqiu_symbol_to_ts_code(symbol: Any) -> str:
    normalized = _normalize_xueqiu_snapshot_symbol(symbol)
    match = re.fullmatch(r"(SH|SZ|BJ)\.(\d{6})", normalized)
    if match:
        return f"{match.group(2)}.{match.group(1)}"
    return normalize_a_stock_symbol(normalized)


def _xueqiu_top_holdings_snapshot_cte(active_only: bool) -> str:
    active_filter_sql = "WHERE COALESCE(is_active, FALSE)" if active_only else ""
    table = XUEQIU_TOP_HOLDINGS_SNAPSHOT_TABLE
    return f"""
        WITH base_holdings AS (
            SELECT
                snapshot_date,
                snapshot_at,
                rank_type,
                year_rank,
                cube_symbol,
                cube_id,
                cube_name,
                screen_name,
                latest_rebalance_at,
                active_rebalance_at,
                holdings_source,
                active_rebalance_days,
                COALESCE(is_active, FALSE) AS is_active,
                stock_symbol,
                raw_stock_symbol,
                stock_name,
                stock_id,
                segment_name,
                CAST(weight_pct AS DOUBLE) AS weight_pct
            FROM {table}
            WHERE weight_pct IS NOT NULL
              AND weight_pct > 0
        ),
        cube_rows AS (
            SELECT
                snapshot_date,
                MAX(snapshot_at) AS snapshot_at,
                ANY_VALUE(rank_type) AS rank_type,
                cube_symbol,
                ANY_VALUE(year_rank) AS year_rank,
                ANY_VALUE(cube_id) AS cube_id,
                ANY_VALUE(cube_name) AS cube_name,
                ANY_VALUE(screen_name) AS screen_name,
                MAX(latest_rebalance_at) AS latest_rebalance_at,
                MAX(active_rebalance_at) AS active_rebalance_at,
                ANY_VALUE(holdings_source) AS holdings_source,
                MAX(active_rebalance_days) AS active_rebalance_days,
                BOOL_OR(COALESCE(is_active, FALSE)) AS is_active,
                SUM(weight_pct) AS stock_weight_pct
            FROM base_holdings
            GROUP BY snapshot_date, cube_symbol
        ),
        cash_holdings AS (
            SELECT
                snapshot_date,
                snapshot_at,
                rank_type,
                year_rank,
                cube_symbol,
                cube_id,
                cube_name,
                screen_name,
                latest_rebalance_at,
                active_rebalance_at,
                holdings_source,
                active_rebalance_days,
                is_active,
                'CASH' AS stock_symbol,
                'CASH' AS raw_stock_symbol,
                '现金' AS stock_name,
                CAST(NULL AS BIGINT) AS stock_id,
                '现金' AS segment_name,
                GREATEST(0.0, 100.0 - stock_weight_pct) AS weight_pct
            FROM cube_rows
            WHERE GREATEST(0.0, 100.0 - stock_weight_pct) > 0.005
        ),
        holding_union AS (
            SELECT * FROM base_holdings
            UNION ALL
            SELECT * FROM cash_holdings
        ),
        filtered_holdings AS (
            SELECT *
            FROM holding_union
            {active_filter_sql}
        )
    """


def _empty_xueqiu_top_holdings_latest(active_only: bool, limit: int, reason: str) -> Dict[str, Any]:
    return {
        "available": False,
        "reason": reason,
        "active_only": active_only,
        "limit": limit,
        "snapshot_date": None,
        "rank_compare_snapshot_date": None,
        "rank_compare_trading_days": XUEQIU_TOP_HOLDINGS_RANK_COMPARE_TRADING_DAYS,
        "snapshot_at": None,
        "cube_count": 0,
        "source_cube_count": 0,
        "active_cube_count": 0,
        "active_rebalance_days": None,
        "index_options": [],
        "board_items": [],
        "contrarian_boards": [],
        "items": [],
    }


def _attach_xueqiu_fear_index_memberships(
    connection,
    items: List[Dict[str, Any]],
    snapshot_date: Any,
) -> List[Dict[str, str]]:
    """Attach current memberships for A-share indexes with fear/greed calculations."""
    targets = [
        {
            "symbol": str(target["symbol"]).upper(),
            "label": str(target.get("ticker") or target.get("label") or target["symbol"]),
        }
        for target in A_STOCK_INDEX_FEAR_GREED_TARGETS
    ]
    memberships: Dict[str, List[Dict[str, str]]] = {}

    if items and targets and _duckdb_table_exists(connection, "a_stock_index_weight"):
        target_symbols = [target["symbol"] for target in targets]
        placeholders = ", ".join("?" for _ in target_symbols)
        rows = _duckdb_query_dicts(
            connection,
            f"""
            WITH latest_dates AS (
                SELECT index_code, MAX(trade_date) AS trade_date
                FROM a_stock_index_weight
                WHERE index_code IN ({placeholders})
                  AND trade_date <= ?
                GROUP BY index_code
            )
            SELECT weights.index_code, weights.con_code
            FROM a_stock_index_weight weights
            JOIN latest_dates
              ON latest_dates.index_code = weights.index_code
             AND latest_dates.trade_date = weights.trade_date
            """,
            [*target_symbols, snapshot_date],
        )
        target_by_symbol = {target["symbol"]: target for target in targets}
        for row in rows:
            target = target_by_symbol.get(str(row.get("index_code") or "").upper())
            constituent = _xueqiu_symbol_to_ts_code(row.get("con_code"))
            if target and constituent:
                memberships.setdefault(constituent, []).append(target)

    # A创100 is a locally maintained custom index rather than a Tushare index-weight series.
    try:
        snapshot_day = (
            snapshot_date
            if isinstance(snapshot_date, date)
            else date.fromisoformat(str(snapshot_date))
        )
        with DBSession() as db:
            latest_date = db.query(
                AStockInnovation100Constituent.rebalance_date
            ).filter(
                AStockInnovation100Constituent.index_code == A_STOCK_INNO100_INDEX_CODE,
                AStockInnovation100Constituent.rebalance_date <= snapshot_day,
            ).order_by(
                AStockInnovation100Constituent.rebalance_date.desc()
            ).first()
            if latest_date:
                custom_target = {
                    "symbol": A_STOCK_INNO100_SYMBOL,
                    "label": "A创100",
                }
                custom_rows = db.query(AStockInnovation100Constituent.ts_code).filter(
                    AStockInnovation100Constituent.index_code == A_STOCK_INNO100_INDEX_CODE,
                    AStockInnovation100Constituent.rebalance_date == latest_date[0],
                ).all()
                for (ts_code,) in custom_rows:
                    constituent = _xueqiu_symbol_to_ts_code(ts_code)
                    if constituent:
                        memberships.setdefault(constituent, []).append(custom_target)
    except Exception as exc:
        logger.warning("Unable to load A创100 membership for 雪球持仓: %s", exc)

    used_options: Dict[str, Dict[str, str]] = {}
    for item in items:
        stock_symbol = _xueqiu_symbol_to_ts_code(item.get("stock_symbol"))
        index_memberships = sorted(
            memberships.get(stock_symbol, []),
            key=lambda value: (value["label"], value["symbol"]),
        )
        item["fear_indexes"] = index_memberships
        for membership in index_memberships:
            used_options[membership["symbol"]] = membership
    return sorted(used_options.values(), key=lambda value: (value["label"], value["symbol"]))


_XUEQIU_PRICE_TABLES = (
    "us_stock_daily",
    "a_stock_index_daily",
    "a_stock_fund_daily_qfq",
    "a_stock_market_daily_qfq",
)


def _xueqiu_direction(
    weight_gain: Any,
    price_gain: Any,
    ratio: Any,
) -> str:
    """雪球组合持仓行为方向标签（双条件判定，宁可保守）。

    与 AI 荐股 xueqiu 块共用同一套规则：
    仅当 权重升幅>5% 且 权价比>1.05 同时成立才判加仓方向（价格跌=逆势吸筹，价格涨=顺势加仓）；
    仅当 权重降幅>5% 且 权价比<0.95 同时成立才判减仓方向（价格涨=借涨减仓，价格跌=减仓）；
    其余（权重变动≤5%、或变动由价格推动、或两轴不一致）一律持平；权重数据缺失为新进。
    """

    def _f(value: Any) -> Optional[float]:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    weight_gain = _f(weight_gain)
    price_gain = _f(price_gain)
    ratio = _f(ratio)
    if weight_gain is None:
        return "新进"
    if weight_gain > 1.05 and ratio is not None and ratio > 1.05:
        return "顺势加仓" if price_gain is not None and price_gain >= 1.0 else "逆势吸筹"
    if weight_gain < 0.95 and ratio is not None and ratio < 0.95:
        return "借涨减仓" if price_gain is not None and price_gain >= 1.0 else "减仓"
    return "持平"


def _attach_xueqiu_5d_momentum(
    connection,
    items: List[Dict[str, Any]],
    compare_snapshot_date: Any,
    snapshot_date: Any,
) -> None:
    """Attach 5-day price momentum and weight/price multiple ratio to latest holdings items.

    momentum_5d = price return (pct) between the compare snapshot and the latest snapshot.
    momentum_multiple_5d = close(latest) / close(compare) price multiple.
    weight_multiple_5d = composite weight multiple (computed in SQL).
    weight_price_ratio_5d = weight_multiple_5d / momentum_multiple_5d.

    If a stock's weight rise is purely driven by its own price rise, both multiples move
    together and the ratio is near 1. A ratio clearly above 1 (especially weight up while
    price down) means the weight rise is NOT caused by the rally — it reflects active buying.
    """
    for item in items:
        item.setdefault("momentum_5d", None)
        item.setdefault("momentum_multiple_5d", None)
        item.setdefault("weight_price_ratio_5d", None)
    if not items or compare_snapshot_date is None or snapshot_date is None:
        return
    if not any(_duckdb_table_exists(connection, table) for table in _XUEQIU_PRICE_TABLES):
        return
    try:
        compare_day = (
            compare_snapshot_date
            if isinstance(compare_snapshot_date, date)
            else date.fromisoformat(str(compare_snapshot_date))
        )
        snapshot_day = (
            snapshot_date
            if isinstance(snapshot_date, date)
            else date.fromisoformat(str(snapshot_date))
        )
    except ValueError:
        return

    price_symbols: List[str] = []
    for item in items:
        raw_symbol = str(item.get("stock_symbol") or "").strip().upper()
        if raw_symbol in {"CASH", "CN_CASH"}:
            continue
        price_symbol = _xueqiu_symbol_to_ts_code(raw_symbol)
        if not price_symbol or not SYMBOL_PATTERN.match(price_symbol):
            continue
        price_symbols.append(price_symbol)
    if not price_symbols:
        return

    try:
        price_df = _load_price_frame(
            price_symbols,
            compare_day - timedelta(days=10),
            snapshot_day + timedelta(days=5),
        )
        if price_df.is_empty():
            return
        usable = price_df.filter(pl.col("trade_date") <= snapshot_day)
        if usable.is_empty():
            return
        momentum_by_symbol: Dict[str, float] = {}
        multiple_by_symbol: Dict[str, float] = {}
        for (symbol,), group in usable.group_by("symbol"):
            latest_close = group.sort("trade_date").tail(1).select("close").to_series()[0]
            base = group.filter(pl.col("trade_date") <= compare_day)
            if base.is_empty():
                continue
            base_close = base.sort("trade_date").tail(1).select("close").to_series()[0]
            if base_close and base_close > 0 and latest_close and latest_close > 0:
                momentum_by_symbol[symbol] = (latest_close / base_close - 1.0) * 100.0
                multiple_by_symbol[symbol] = latest_close / base_close
    except Exception as exc:
        logger.warning("Unable to compute 5d price momentum for 雪球持仓: %s", exc)
        return

    for item in items:
        price_symbol = _xueqiu_symbol_to_ts_code(
            str(item.get("stock_symbol") or "").strip().upper()
        )
        momentum = momentum_by_symbol.get(price_symbol)
        multiple = multiple_by_symbol.get(price_symbol)
        item["momentum_5d"] = round(momentum, 2) if momentum is not None else None
        item["momentum_multiple_5d"] = round(multiple, 3) if multiple is not None else None
        weight_multiple = item.get("weight_multiple_5d")
        if weight_multiple is not None:
            item["weight_multiple_5d"] = round(float(weight_multiple), 3)
            weight_multiple = item["weight_multiple_5d"]
        if (
            multiple is not None
            and weight_multiple is not None
            and multiple > 0
            and float(weight_multiple) > 0
        ):
            item["weight_price_ratio_5d"] = round(float(weight_multiple) / multiple, 2)
        else:
            item["weight_price_ratio_5d"] = None


def _load_xueqiu_board_momentum(
    connection,
    active_only: bool,
    snapshot_date: Any,
    compare_snapshot_date: Any,
) -> List[Dict[str, Any]]:
    """Aggregate Xueqiu holdings into cached THS boards and calculate 5-day ratios."""
    required = ("a_stock_ths_member", "a_stock_ths_daily")
    if not snapshot_date or not compare_snapshot_date or not all(
        _duckdb_table_exists(connection, table) for table in required
    ):
        return []

    # ORM values are copied before the short session closes (expire_on_commit=True).
    with DBSession() as db:
        catalog = {
            row.ts_code: {
                "ths_code": row.ts_code,
                "name": row.name,
                "board_type": row.index_type,
            }
            for row in db.query(AIStockTHSIndexCache).all()
        }
    if not catalog:
        return []

    cte = _xueqiu_top_holdings_snapshot_cte(active_only)
    rows = _duckdb_query_dicts(
        connection,
        f"""
        {cte},
        current_holdings AS (
            SELECT * FROM filtered_holdings WHERE snapshot_date = CAST(? AS DATE)
        ),
        compare_holdings AS (
            SELECT * FROM filtered_holdings WHERE snapshot_date = CAST(? AS DATE)
        ),
        current_cube_count AS (
            SELECT COUNT(DISTINCT cube_symbol) AS value FROM current_holdings
        ),
        compare_cube_count AS (
            SELECT COUNT(DISTINCT cube_symbol) AS value FROM compare_holdings
        ),
        current_stocks AS (
            SELECT
                stock_symbol,
                SUM(weight_pct) / NULLIF(MAX(current_cube_count.value), 0) AS composite_weight_pct,
                COUNT(DISTINCT cube_symbol) AS holding_cube_count
            FROM current_holdings CROSS JOIN current_cube_count
            WHERE stock_symbol NOT IN ('CASH', 'CN_CASH')
            GROUP BY stock_symbol
        ),
        compare_stocks AS (
            SELECT
                stock_symbol,
                SUM(weight_pct) / NULLIF(MAX(compare_cube_count.value), 0) AS composite_weight_pct
            FROM compare_holdings CROSS JOIN compare_cube_count
            WHERE stock_symbol NOT IN ('CASH', 'CN_CASH')
            GROUP BY stock_symbol
        ),
        normalized_members AS (
            SELECT
                ths_code,
                con_code,
                CASE
                    WHEN con_code LIKE '%.SH' THEN 'SH.' || LEFT(con_code, 6)
                    WHEN con_code LIKE '%.SZ' THEN 'SZ.' || LEFT(con_code, 6)
                    WHEN con_code LIKE '%.BJ' THEN 'BJ.' || LEFT(con_code, 6)
                    ELSE con_code
                END AS stock_symbol,
                in_date,
                out_date
            FROM a_stock_ths_member
        ),
        current_board_weights AS (
            SELECT
                members.ths_code,
                COUNT(DISTINCT current_stocks.stock_symbol) AS stock_count,
                SUM(current_stocks.composite_weight_pct) AS composite_weight_pct,
                SUM(current_stocks.holding_cube_count) AS stock_cube_links
            FROM normalized_members members
            JOIN current_stocks ON current_stocks.stock_symbol = members.stock_symbol
            WHERE (members.in_date IS NULL OR members.in_date <= CAST(? AS DATE))
              AND (members.out_date IS NULL OR members.out_date > CAST(? AS DATE))
            GROUP BY members.ths_code
        ),
        compare_board_weights AS (
            SELECT
                members.ths_code,
                SUM(compare_stocks.composite_weight_pct) AS weight_5d_ago
            FROM normalized_members members
            JOIN compare_stocks ON compare_stocks.stock_symbol = members.stock_symbol
            WHERE (members.in_date IS NULL OR members.in_date <= CAST(? AS DATE))
              AND (members.out_date IS NULL OR members.out_date > CAST(? AS DATE))
            GROUP BY members.ths_code
        ),
        latest_prices AS (
            SELECT ths_code, close
            FROM a_stock_ths_daily
            WHERE trade_date <= CAST(? AS DATE)
            QUALIFY ROW_NUMBER() OVER (PARTITION BY ths_code ORDER BY trade_date DESC) = 1
        ),
        compare_prices AS (
            SELECT ths_code, close
            FROM a_stock_ths_daily
            WHERE trade_date <= CAST(? AS DATE)
            QUALIFY ROW_NUMBER() OVER (PARTITION BY ths_code ORDER BY trade_date DESC) = 1
        )
        SELECT
            current_board_weights.*,
            compare_board_weights.weight_5d_ago,
            latest_prices.close AS close_price,
            compare_prices.close AS close_5d_ago
        FROM current_board_weights
        JOIN compare_board_weights USING (ths_code)
        JOIN latest_prices USING (ths_code)
        JOIN compare_prices USING (ths_code)
        WHERE current_board_weights.stock_count >= {XUEQIU_BOARD_MIN_STOCKS}
        """,
        [
            snapshot_date,
            compare_snapshot_date,
            snapshot_date,
            snapshot_date,
            compare_snapshot_date,
            compare_snapshot_date,
            snapshot_date,
            compare_snapshot_date,
        ],
    )
    items: List[Dict[str, Any]] = []
    for row in rows:
        meta = catalog.get(str(row.get("ths_code") or "").upper())
        current_weight = _safe_float(row.get("composite_weight_pct"))
        old_weight = _safe_float(row.get("weight_5d_ago"))
        close_price = _safe_float(row.get("close_price"))
        old_close = _safe_float(row.get("close_5d_ago"))
        if not meta or not current_weight or not old_weight or not close_price or not old_close:
            continue
        weight_multiple = current_weight / old_weight
        price_multiple = close_price / old_close
        ratio = weight_multiple / price_multiple if price_multiple > 0 else None
        direction = _xueqiu_direction(weight_multiple, price_multiple, ratio)
        items.append(
            {
                **meta,
                "stock_count": int(row.get("stock_count") or 0),
                "stock_cube_links": int(row.get("stock_cube_links") or 0),
                "composite_weight_pct": round(current_weight, 4),
                "weight_5d_ago": round(old_weight, 4),
                "weight_change_5d": round(current_weight - old_weight, 4),
                "weight_multiple_5d": round(weight_multiple, 3),
                "momentum_5d": round((price_multiple - 1.0) * 100.0, 2),
                "momentum_multiple_5d": round(price_multiple, 3),
                "weight_price_ratio_5d": round(ratio, 2) if ratio is not None else None,
                "direction": direction,
            }
        )
    return sorted(
        items,
        key=lambda item: (
            item["direction"] != "逆势吸筹",
            -(item.get("weight_price_ratio_5d") or 0),
            -item["composite_weight_pct"],
        ),
    )


def load_xueqiu_board_holding_symbols(
    ths_code: str,
    active_only: bool = True,
) -> Dict[str, Any]:
    """Return current Xueqiu-held stocks belonging to one cached THS board."""
    normalized_code = str(ths_code or "").strip().upper()
    if not normalized_code or len(normalized_code) > 24:
        raise HTTPException(status_code=400, detail="无效的同花顺板块代码")
    connection = _connect_duckdb()
    try:
        if not all(
            _duckdb_table_exists(connection, table)
            for table in (XUEQIU_TOP_HOLDINGS_SNAPSHOT_TABLE, "a_stock_ths_member")
        ):
            return {
                "ths_code": normalized_code,
                "name": None,
                "snapshot_date": None,
                "stock_symbols": [],
            }
        cte = _xueqiu_top_holdings_snapshot_cte(active_only)
        rows = _duckdb_query_dicts(
            connection,
            f"""
            {cte},
            latest_snapshot AS (
                SELECT MAX(snapshot_date) AS snapshot_date FROM filtered_holdings
            ),
            normalized_members AS (
                SELECT
                    CASE
                        WHEN con_code LIKE '%.SH' THEN 'SH.' || LEFT(con_code, 6)
                        WHEN con_code LIKE '%.SZ' THEN 'SZ.' || LEFT(con_code, 6)
                        WHEN con_code LIKE '%.BJ' THEN 'BJ.' || LEFT(con_code, 6)
                        ELSE con_code
                    END AS stock_symbol,
                    in_date,
                    out_date
                FROM a_stock_ths_member
                WHERE ths_code = ?
            )
            SELECT DISTINCT holdings.stock_symbol, latest_snapshot.snapshot_date
            FROM filtered_holdings holdings
            JOIN latest_snapshot ON holdings.snapshot_date = latest_snapshot.snapshot_date
            JOIN normalized_members members ON members.stock_symbol = holdings.stock_symbol
            WHERE holdings.stock_symbol NOT IN ('CASH', 'CN_CASH')
              AND (members.in_date IS NULL OR members.in_date <= latest_snapshot.snapshot_date)
              AND (members.out_date IS NULL OR members.out_date > latest_snapshot.snapshot_date)
            ORDER BY holdings.stock_symbol
            """,
            [normalized_code],
        )
        with DBSession() as db:
            catalog_row = db.query(AIStockTHSIndexCache).filter(
                AIStockTHSIndexCache.ts_code == normalized_code
            ).first()
            board_name = catalog_row.name if catalog_row else None
        return {
            "ths_code": normalized_code,
            "name": board_name,
            "snapshot_date": rows[0].get("snapshot_date") if rows else None,
            "stock_symbols": [row["stock_symbol"] for row in rows],
        }
    finally:
        connection.close()


def load_xueqiu_top_holdings_latest(
    active_only: bool = True,
    limit: int = 300,
    snapshot_date: Optional[date] = None,
) -> Dict[str, Any]:
    """最新（或指定 snapshot_date）持仓快照的综合权重/排名/权价比统计。"""
    normalized_limit = max(1, min(int(limit or 300), 2000))
    connection = _connect_duckdb()
    try:
        if not _duckdb_table_exists(connection, XUEQIU_TOP_HOLDINGS_SNAPSHOT_TABLE):
            return _empty_xueqiu_top_holdings_latest(active_only, normalized_limit, "snapshot_table_missing")

        if snapshot_date is None:
            latest_snapshot_cte = """
                SELECT MAX(snapshot_date) AS snapshot_date
                FROM cube_rows
            """
            latest_params: List[Any] = []
        else:
            latest_snapshot_cte = "SELECT CAST(? AS DATE) AS snapshot_date"
            latest_params = [snapshot_date]

        cte = _xueqiu_top_holdings_snapshot_cte(active_only)
        metadata_rows = _duckdb_query_dicts(
            connection,
            f"""
            {cte},
            latest_snapshot AS (
                {latest_snapshot_cte}
            ),
            rank_dates AS (
                SELECT DISTINCT snapshot_date
                FROM filtered_holdings
            ),
            compare_snapshot AS (
                SELECT snapshot_date
                FROM (
                    SELECT
                        snapshot_date,
                        ROW_NUMBER() OVER (ORDER BY snapshot_date DESC) AS snapshot_rank_desc
                    FROM rank_dates
                    WHERE snapshot_date < (SELECT snapshot_date FROM latest_snapshot)
                ) ranked_dates
                WHERE snapshot_rank_desc = {XUEQIU_TOP_HOLDINGS_RANK_COMPARE_TRADING_DAYS}
            ),
            latest_cube_summary AS (
                SELECT
                    cube_rows.snapshot_date,
                    MAX(cube_rows.snapshot_at) AS snapshot_at,
                    COUNT(DISTINCT cube_rows.cube_symbol) AS source_cube_count,
                    COUNT(DISTINCT CASE WHEN cube_rows.is_active THEN cube_rows.cube_symbol END) AS active_cube_count,
                    MAX(cube_rows.active_rebalance_days) AS active_rebalance_days
                FROM cube_rows
                JOIN latest_snapshot ON cube_rows.snapshot_date = latest_snapshot.snapshot_date
                GROUP BY cube_rows.snapshot_date
            ),
            filtered_latest_summary AS (
                SELECT
                    filtered_holdings.snapshot_date,
                    COUNT(DISTINCT filtered_holdings.cube_symbol) AS cube_count,
                    COUNT(*) AS holding_row_count
                FROM filtered_holdings
                JOIN latest_snapshot ON filtered_holdings.snapshot_date = latest_snapshot.snapshot_date
                GROUP BY filtered_holdings.snapshot_date
            )
            SELECT
                latest_snapshot.snapshot_date,
                compare_snapshot.snapshot_date AS rank_compare_snapshot_date,
                latest_cube_summary.snapshot_at,
                COALESCE(filtered_latest_summary.cube_count, 0) AS cube_count,
                COALESCE(filtered_latest_summary.holding_row_count, 0) AS holding_row_count,
                COALESCE(latest_cube_summary.source_cube_count, 0) AS source_cube_count,
                COALESCE(latest_cube_summary.active_cube_count, 0) AS active_cube_count,
                latest_cube_summary.active_rebalance_days
            FROM latest_snapshot
            LEFT JOIN compare_snapshot ON TRUE
            LEFT JOIN latest_cube_summary ON latest_cube_summary.snapshot_date = latest_snapshot.snapshot_date
            LEFT JOIN filtered_latest_summary ON filtered_latest_summary.snapshot_date = latest_snapshot.snapshot_date
            """,
            latest_params,
        )
        metadata = metadata_rows[0] if metadata_rows else {}
        snapshot_date = metadata.get("snapshot_date")
        if not snapshot_date:
            return _empty_xueqiu_top_holdings_latest(active_only, normalized_limit, "snapshot_empty")

        item_rows = _duckdb_query_dicts(
            connection,
            f"""
            {cte},
            latest_snapshot AS (
                {latest_snapshot_cte}
            ),
            rank_dates AS (
                SELECT DISTINCT snapshot_date
                FROM filtered_holdings
            ),
            compare_snapshot AS (
                SELECT snapshot_date
                FROM (
                    SELECT
                        snapshot_date,
                        ROW_NUMBER() OVER (ORDER BY snapshot_date DESC) AS snapshot_rank_desc
                    FROM rank_dates
                    WHERE snapshot_date < (SELECT snapshot_date FROM latest_snapshot)
                ) ranked_dates
                WHERE snapshot_rank_desc = {XUEQIU_TOP_HOLDINGS_RANK_COMPARE_TRADING_DAYS}
            ),
            snapshot_holdings AS (
                SELECT filtered_holdings.*
                FROM filtered_holdings
                JOIN latest_snapshot ON filtered_holdings.snapshot_date = latest_snapshot.snapshot_date
            ),
            snapshot_summary AS (
                SELECT COUNT(DISTINCT cube_symbol) AS cube_count
                FROM snapshot_holdings
            ),
            stock_summary AS (
                SELECT
                    stock_symbol,
                    ANY_VALUE(raw_stock_symbol) AS raw_stock_symbol,
                    ANY_VALUE(stock_name) AS stock_name,
                    ANY_VALUE(segment_name) AS segment_name,
                    MIN(year_rank) AS best_year_rank,
                    COUNT(DISTINCT cube_symbol) AS holding_cube_count,
                    SUM(weight_pct) AS total_weight_pct,
                    SUM(weight_pct) / NULLIF(MAX(snapshot_summary.cube_count), 0) AS composite_weight_pct,
                    COUNT(DISTINCT cube_symbol) * 100.0 / NULLIF(MAX(snapshot_summary.cube_count), 0) AS holding_cube_ratio_pct,
                    SUM(weight_pct) / NULLIF(COUNT(DISTINCT cube_symbol), 0) AS average_weight_pct
                FROM snapshot_holdings
                CROSS JOIN snapshot_summary
                GROUP BY stock_symbol
            ),
            ranked AS (
                SELECT
                    ROW_NUMBER() OVER (
                        ORDER BY total_weight_pct DESC, holding_cube_count DESC, stock_symbol DESC
                    ) AS composite_rank,
                    *
                FROM stock_summary
            ),
            compare_holdings AS (
                SELECT filtered_holdings.*
                FROM filtered_holdings
                JOIN compare_snapshot ON filtered_holdings.snapshot_date = compare_snapshot.snapshot_date
            ),
            compare_snapshot_summary AS (
                SELECT COUNT(DISTINCT cube_symbol) AS cube_count
                FROM compare_holdings
            ),
            compare_stock_summary AS (
                SELECT
                    stock_symbol,
                    COUNT(DISTINCT cube_symbol) AS holding_cube_count,
                    SUM(weight_pct) AS total_weight_pct,
                    SUM(weight_pct) / NULLIF(MAX(compare_snapshot_summary.cube_count), 0) AS composite_weight_pct
                FROM compare_holdings
                CROSS JOIN compare_snapshot_summary
                GROUP BY stock_symbol
            ),
            compare_ranked AS (
                SELECT
                    ROW_NUMBER() OVER (
                        ORDER BY total_weight_pct DESC, holding_cube_count DESC, stock_symbol DESC
                    ) AS composite_rank,
                    *
                FROM compare_stock_summary
            )
            SELECT
                ranked.*,
                compare_ranked.composite_rank AS rank_5d_ago,
                compare_ranked.composite_weight_pct AS weight_5d_ago,
                compare_ranked.holding_cube_count AS cube_count_5d_ago,
                CASE
                    WHEN compare_ranked.composite_rank IS NULL THEN NULL
                    ELSE compare_ranked.composite_rank - ranked.composite_rank
                END AS rank_change_5d,
                CASE
                    WHEN compare_ranked.composite_weight_pct IS NULL THEN NULL
                    ELSE ranked.composite_weight_pct - compare_ranked.composite_weight_pct
                END AS weight_change_5d,
                CASE
                    WHEN compare_ranked.composite_weight_pct IS NULL
                         OR compare_ranked.composite_weight_pct = 0
                    THEN NULL
                    ELSE ranked.composite_weight_pct / compare_ranked.composite_weight_pct
                END AS weight_multiple_5d
            FROM ranked
            LEFT JOIN compare_ranked ON compare_ranked.stock_symbol = ranked.stock_symbol
            ORDER BY ranked.composite_rank
            LIMIT ?
            """,
            [*latest_params, normalized_limit],
        )
        _attach_xueqiu_5d_momentum(
            connection,
            item_rows,
            metadata.get("rank_compare_snapshot_date"),
            snapshot_date,
        )
        for item in item_rows:
            item["direction"] = _xueqiu_direction(
                item.get("weight_multiple_5d"),
                item.get("momentum_multiple_5d"),
                item.get("weight_price_ratio_5d"),
            )
        index_options = _attach_xueqiu_fear_index_memberships(
            connection,
            item_rows,
            snapshot_date,
        )
        board_items = _load_xueqiu_board_momentum(
            connection,
            active_only,
            snapshot_date,
            metadata.get("rank_compare_snapshot_date"),
        )
        return {
            "available": True,
            "active_only": active_only,
            "limit": normalized_limit,
            "snapshot_date": snapshot_date,
            "rank_compare_snapshot_date": metadata.get("rank_compare_snapshot_date"),
            "rank_compare_trading_days": XUEQIU_TOP_HOLDINGS_RANK_COMPARE_TRADING_DAYS,
            "snapshot_at": metadata.get("snapshot_at"),
            "cube_count": metadata.get("cube_count") or 0,
            "holding_row_count": metadata.get("holding_row_count") or 0,
            "source_cube_count": metadata.get("source_cube_count") or 0,
            "active_cube_count": metadata.get("active_cube_count") or 0,
            "active_rebalance_days": metadata.get("active_rebalance_days"),
            "index_options": index_options,
            "board_items": board_items,
            "contrarian_boards": [
                item
                for item in board_items
                if item.get("direction") == "逆势吸筹"
                and int(item.get("stock_count") or 0) >= XUEQIU_CONTRARIAN_BOARD_MIN_STOCKS
            ][:12],
            "items": item_rows,
        }
    finally:
        connection.close()


def load_xueqiu_top_holdings_history(
    *,
    symbol: str,
    active_only: bool = True,
    limit: int = 500,
) -> Dict[str, Any]:
    normalized_symbol = _normalize_xueqiu_snapshot_symbol(symbol)
    raw_symbol = _raw_xueqiu_snapshot_symbol(normalized_symbol)
    normalized_limit = max(1, min(int(limit or 500), 2000))
    if not normalized_symbol:
        raise HTTPException(status_code=400, detail="symbol 不能为空")

    connection = _connect_duckdb()
    try:
        if not _duckdb_table_exists(connection, XUEQIU_TOP_HOLDINGS_SNAPSHOT_TABLE):
            return {
                "available": False,
                "reason": "snapshot_table_missing",
                "active_only": active_only,
                "symbol": normalized_symbol,
                "raw_symbol": raw_symbol,
                "limit": normalized_limit,
                "latest": None,
                "history": [],
            }

        cte = _xueqiu_top_holdings_snapshot_cte(active_only)
        rows = _duckdb_query_dicts(
            connection,
            f"""
            {cte},
            date_summary AS (
                SELECT
                    snapshot_date,
                    MAX(snapshot_at) AS snapshot_at,
                    COUNT(DISTINCT cube_symbol) AS cube_count,
                    MAX(active_rebalance_days) AS active_rebalance_days
                FROM filtered_holdings
                GROUP BY snapshot_date
            ),
            stock_summary AS (
                SELECT
                    filtered_holdings.snapshot_date,
                    MAX(date_summary.snapshot_at) AS snapshot_at,
                    MAX(date_summary.cube_count) AS cube_count,
                    MAX(date_summary.active_rebalance_days) AS active_rebalance_days,
                    filtered_holdings.stock_symbol,
                    ANY_VALUE(filtered_holdings.raw_stock_symbol) AS raw_stock_symbol,
                    ANY_VALUE(filtered_holdings.stock_name) AS stock_name,
                    ANY_VALUE(filtered_holdings.segment_name) AS segment_name,
                    MIN(filtered_holdings.year_rank) AS best_year_rank,
                    COUNT(DISTINCT filtered_holdings.cube_symbol) AS holding_cube_count,
                    SUM(filtered_holdings.weight_pct) AS total_weight_pct,
                    SUM(filtered_holdings.weight_pct) / NULLIF(MAX(date_summary.cube_count), 0) AS composite_weight_pct,
                    COUNT(DISTINCT filtered_holdings.cube_symbol) * 100.0 / NULLIF(MAX(date_summary.cube_count), 0) AS holding_cube_ratio_pct,
                    SUM(filtered_holdings.weight_pct) / NULLIF(COUNT(DISTINCT filtered_holdings.cube_symbol), 0) AS average_weight_pct
                FROM filtered_holdings
                JOIN date_summary ON filtered_holdings.snapshot_date = date_summary.snapshot_date
                GROUP BY filtered_holdings.snapshot_date, filtered_holdings.stock_symbol
            ),
            ranked AS (
                SELECT
                    ROW_NUMBER() OVER (
                        PARTITION BY snapshot_date
                        ORDER BY total_weight_pct DESC, holding_cube_count DESC, stock_symbol DESC
                    ) AS composite_rank,
                    *
                FROM stock_summary
            ),
            selected AS (
                SELECT *
                FROM ranked
                WHERE UPPER(stock_symbol) = ?
                   OR UPPER(raw_stock_symbol) = ?
                ORDER BY snapshot_date DESC
                LIMIT ?
            )
            SELECT *
            FROM selected
            ORDER BY snapshot_date
            """,
            [normalized_symbol, raw_symbol, normalized_limit],
        )
        for row in rows:
            row["close_price"] = None
        price_symbol = _xueqiu_symbol_to_ts_code(normalized_symbol)
        if (
            rows
            and price_symbol
            and SYMBOL_PATTERN.match(price_symbol)
            and any(_duckdb_table_exists(connection, table) for table in _XUEQIU_PRICE_TABLES)
        ):
            try:
                snapshot_days = [date.fromisoformat(str(row["snapshot_date"])) for row in rows]
                price_df = _load_price_frame(
                    [price_symbol],
                    min(snapshot_days) - timedelta(days=10),
                    max(snapshot_days) + timedelta(days=1),
                ).sort("trade_date")
                if not price_df.is_empty():
                    for row, snapshot_day in zip(rows, snapshot_days):
                        available = price_df.filter(pl.col("trade_date") <= snapshot_day)
                        if not available.is_empty():
                            close_price = available.tail(1).select("close").to_series()[0]
                            row["close_price"] = round(float(close_price), 3) if close_price is not None else None
            except Exception as exc:
                logger.warning("Unable to load price history for 雪球持仓 %s: %s", normalized_symbol, exc)
        return {
            "available": True,
            "active_only": active_only,
            "symbol": normalized_symbol,
            "raw_symbol": raw_symbol,
            "limit": normalized_limit,
            "latest": rows[-1] if rows else None,
            "history": rows,
        }
    finally:
        connection.close()


def load_xueqiu_top_holding_details(
    *,
    symbol: str,
    snapshot_date: date,
    active_only: bool = True,
    limit: int = 1000,
) -> Dict[str, Any]:
    normalized_symbol = _normalize_xueqiu_snapshot_symbol(symbol)
    raw_symbol = _raw_xueqiu_snapshot_symbol(normalized_symbol)
    normalized_limit = max(1, min(int(limit or 1000), 5000))
    if not normalized_symbol:
        raise HTTPException(status_code=400, detail="symbol 不能为空")

    connection = _connect_duckdb()
    try:
        if not _duckdb_table_exists(connection, XUEQIU_TOP_HOLDINGS_SNAPSHOT_TABLE):
            return {
                "available": False,
                "reason": "snapshot_table_missing",
                "active_only": active_only,
                "symbol": normalized_symbol,
                "raw_symbol": raw_symbol,
                "snapshot_date": snapshot_date.isoformat(),
                "limit": normalized_limit,
                "cube_count": 0,
                "holding_cube_count": 0,
                "holding_cube_ratio_pct": None,
                "total_weight_pct": 0.0,
                "average_weight_pct": None,
                "details": [],
            }

        cte = _xueqiu_top_holdings_snapshot_cte(active_only)
        summary_rows = _duckdb_query_dicts(
            connection,
            f"""
            {cte},
            date_summary AS (
                SELECT
                    snapshot_date,
                    MAX(snapshot_at) AS snapshot_at,
                    COUNT(DISTINCT cube_symbol) AS cube_count,
                    MAX(active_rebalance_days) AS active_rebalance_days
                FROM filtered_holdings
                WHERE snapshot_date = ?
                GROUP BY snapshot_date
            ),
            selected_holdings AS (
                SELECT filtered_holdings.*
                FROM filtered_holdings
                WHERE snapshot_date = ?
                  AND (
                    UPPER(stock_symbol) = ?
                    OR UPPER(raw_stock_symbol) = ?
                  )
            )
            SELECT
                MAX(selected_holdings.snapshot_date) AS snapshot_date,
                MAX(date_summary.snapshot_at) AS snapshot_at,
                MAX(date_summary.cube_count) AS cube_count,
                MAX(date_summary.active_rebalance_days) AS active_rebalance_days,
                ANY_VALUE(selected_holdings.stock_symbol) AS stock_symbol,
                ANY_VALUE(selected_holdings.raw_stock_symbol) AS raw_stock_symbol,
                ANY_VALUE(selected_holdings.stock_name) AS stock_name,
                ANY_VALUE(selected_holdings.segment_name) AS segment_name,
                COUNT(DISTINCT selected_holdings.cube_symbol) AS holding_cube_count,
                SUM(selected_holdings.weight_pct) AS total_weight_pct,
                SUM(selected_holdings.weight_pct) / NULLIF(COUNT(DISTINCT selected_holdings.cube_symbol), 0) AS average_weight_pct
            FROM date_summary
            LEFT JOIN selected_holdings ON selected_holdings.snapshot_date = date_summary.snapshot_date
            """,
            [snapshot_date, snapshot_date, normalized_symbol, raw_symbol],
        )
        rows = _duckdb_query_dicts(
            connection,
            f"""
            {cte},
            date_summary AS (
                SELECT
                    snapshot_date,
                    MAX(snapshot_at) AS snapshot_at,
                    COUNT(DISTINCT cube_symbol) AS cube_count,
                    MAX(active_rebalance_days) AS active_rebalance_days
                FROM filtered_holdings
                WHERE snapshot_date = ?
                GROUP BY snapshot_date
            ),
            selected_holdings AS (
                SELECT filtered_holdings.*
                FROM filtered_holdings
                WHERE snapshot_date = ?
                  AND (
                    UPPER(stock_symbol) = ?
                    OR UPPER(raw_stock_symbol) = ?
                  )
            )
            SELECT
                selected_holdings.snapshot_date,
                date_summary.snapshot_at,
                date_summary.cube_count,
                date_summary.active_rebalance_days,
                selected_holdings.stock_symbol,
                selected_holdings.raw_stock_symbol,
                selected_holdings.stock_name,
                selected_holdings.segment_name,
                selected_holdings.cube_symbol,
                selected_holdings.cube_id,
                selected_holdings.cube_name,
                selected_holdings.screen_name,
                selected_holdings.year_rank,
                selected_holdings.holdings_source,
                selected_holdings.is_active,
                selected_holdings.latest_rebalance_at,
                selected_holdings.active_rebalance_at,
                selected_holdings.weight_pct
            FROM selected_holdings
            JOIN date_summary ON selected_holdings.snapshot_date = date_summary.snapshot_date
            ORDER BY selected_holdings.weight_pct DESC, selected_holdings.year_rank ASC NULLS LAST, selected_holdings.cube_symbol
            LIMIT ?
            """,
            [snapshot_date, snapshot_date, normalized_symbol, raw_symbol, normalized_limit],
        )
        summary = summary_rows[0] if summary_rows else {}
        cube_count = summary.get("cube_count") or (rows[0].get("cube_count") if rows else 0)
        holding_cube_count = summary.get("holding_cube_count") or 0
        total_weight_pct = float(summary.get("total_weight_pct") or 0.0)
        return {
            "available": True,
            "active_only": active_only,
            "symbol": normalized_symbol,
            "raw_symbol": raw_symbol,
            "stock_name": summary.get("stock_name") or (rows[0].get("stock_name") if rows else None),
            "segment_name": summary.get("segment_name") or (rows[0].get("segment_name") if rows else None),
            "snapshot_date": snapshot_date.isoformat(),
            "snapshot_at": summary.get("snapshot_at") or (rows[0].get("snapshot_at") if rows else None),
            "active_rebalance_days": summary.get("active_rebalance_days") or (rows[0].get("active_rebalance_days") if rows else None),
            "limit": normalized_limit,
            "cube_count": cube_count or 0,
            "holding_cube_count": holding_cube_count,
            "holding_cube_ratio_pct": (
                holding_cube_count * 100.0 / cube_count
                if cube_count
                else None
            ),
            "total_weight_pct": total_weight_pct,
            "average_weight_pct": (
                total_weight_pct / holding_cube_count
                if holding_cube_count
                else None
            ),
            "details": rows,
        }
    finally:
        connection.close()




@router.get("/xueqiu-top-holdings/latest")
def get_xueqiu_top_holdings_latest(
    active_only: bool = Query(True, description="只统计主理人活跃组合"),
    limit: int = Query(300, ge=1, le=2000),
    _: str = Depends(valid_admin_account),
):
    return load_xueqiu_top_holdings_latest(active_only=active_only, limit=limit)


@router.get("/xueqiu-top-holdings/board-holdings")
def get_xueqiu_board_holding_symbols(
    ths_code: str = Query(..., min_length=1, max_length=24),
    active_only: bool = Query(True, description="只统计主理人活跃组合"),
    _: str = Depends(valid_admin_account),
):
    return load_xueqiu_board_holding_symbols(
        ths_code=ths_code,
        active_only=active_only,
    )


@router.get("/xueqiu-top-holdings/history")
def get_xueqiu_top_holdings_history(
    symbol: str = Query(..., min_length=1),
    active_only: bool = Query(True, description="只统计主理人活跃组合"),
    limit: int = Query(500, ge=1, le=2000),
    _: str = Depends(valid_admin_account),
):
    return load_xueqiu_top_holdings_history(
        symbol=symbol,
        active_only=active_only,
        limit=limit,
    )


@router.get("/xueqiu-top-holdings/details")
def get_xueqiu_top_holding_details(
    symbol: str = Query(..., min_length=1),
    snapshot_date: date = Query(...),
    active_only: bool = Query(True, description="只统计主理人活跃组合"),
    limit: int = Query(1000, ge=1, le=5000),
    _: str = Depends(valid_admin_account),
):
    return load_xueqiu_top_holding_details(
        symbol=symbol,
        snapshot_date=snapshot_date,
        active_only=active_only,
        limit=limit,
    )


class XueqiuStrategyConfigUpdate(BaseModel):
    """雪球星澜组合策略参数更新（壹号综合权重/贰号排名加速/叁号权价比）。

    仅含雪球组合相关参数；底/顶信号检测参数统一走 fear_greed_signal_configs 表。
    """
    enabled: Optional[bool] = None
    fear_target_count: Optional[int] = Field(None, ge=1, le=200, description="恐慌放量时的目标持仓数")
    greed_target_count: Optional[int] = Field(None, ge=1, le=200, description="贪婪缩量时的目标持仓数")
    min_holding_cubes: Optional[int] = Field(None, ge=1, le=200, description="买入候选最少持仓组合数")
    current_rank_limit: Optional[int] = Field(None, ge=1, le=1000, description="买入候选综合排名上限")
    holding_cube_increase: Optional[int] = Field(None, ge=0, le=100, description="买入候选持仓组合数增加下限")
    metric_threshold: Optional[float] = Field(None, ge=0, description="策略指标阈值（权价比≥x 或 排名上升≥x名）")
    metric_upper_threshold: Optional[float] = Field(None, ge=0, description="策略指标上限（叁号权价比≤x）")
    new_entry_rank_limit: Optional[int] = Field(None, ge=1, le=1000, description="强势新进综合排名上限")
    new_entry_min_cubes: Optional[int] = Field(None, ge=1, le=1000, description="强势新进最少持仓组合数")
    min_weight_increase: Optional[float] = Field(None, ge=0, description="买入候选总权重上升下限")
    hard_exit_rank: Optional[int] = Field(None, ge=1, le=5000, description="硬退出：综合排名超过该值立即卖")
    hard_exit_min_cubes: Optional[int] = Field(None, ge=1, le=1000, description="硬退出：活跃组合数低于该值立即卖")
    sell_rank: Optional[int] = Field(None, ge=1, le=5000, description="卖出缓冲基数（Top10 时的缓冲大小，随目标仓位等比缩放）")
    sell_confirm_days: Optional[int] = Field(None, ge=1, le=30, description="连续几日跌出缓冲才确认卖出")
    min_holding_days: Optional[int] = Field(None, ge=0, le=120, description="买入后最少持有完整交易日数")
    retain_rank_limit: Optional[int] = Field(None, ge=1, le=5000, description="缓冲候选综合排名上限")
    retain_min_cubes: Optional[int] = Field(None, ge=1, le=1000, description="缓冲候选最少持仓组合数")
    buy_confirm_prior_days: Optional[int] = Field(None, ge=0, le=30, description="买入确认需最近几个快照日也符合（0=只看当天符合即可）")
    max_replacements: Optional[int] = Field(None, ge=0, le=200, description="每次调仓最多替换几只")
    rolling_replacement_days: Optional[int] = Field(None, ge=1, le=120, description="滚动替换限制的回看天数")
    rolling_max_replacements: Optional[int] = Field(None, ge=0, le=500, description="滚动窗口内最多替换几只")
    take_profit_pct: Optional[float] = Field(None, gt=0, le=1000, description="短期全额止盈收益率")
    take_profit_max_holding_days: Optional[int] = Field(None, ge=1, le=500, description="止盈规则适用的最大持有交易日")
    take_profit_cooldown_days: Optional[int] = Field(None, ge=0, le=120, description="止盈后禁止重新买入的交易日数")


XUEQIU_STRATEGY_CONFIG_KEYS = (
    "buffer",
    "rank_acceleration",
    "weight_price_ratio",
)


@router.get("/xueqiu-strategy-configs")
def get_xueqiu_strategy_configs(
    _: str = Depends(valid_admin_account),
):
    from ...robot.xueqiu_top_holdings_report import (
        XUEQIU_STRATEGY_CONFIG_DEFAULTS,
        load_xueqiu_strategy_config,
    )

    return {
        "configs": [
            load_xueqiu_strategy_config(strategy_key)
            for strategy_key in XUEQIU_STRATEGY_CONFIG_DEFAULTS
        ],
    }


@router.put("/xueqiu-strategy-configs/{strategy_key}")
def update_xueqiu_strategy_config(
    strategy_key: str,
    payload: XueqiuStrategyConfigUpdate,
    _: str = Depends(valid_admin_account),
):
    if strategy_key not in XUEQIU_STRATEGY_CONFIG_KEYS:
        raise HTTPException(status_code=400, detail=f"未知策略: {strategy_key}")
    from ...core.database import XueqiuStrategyConfig
    from ...robot.xueqiu_top_holdings_report import load_xueqiu_strategy_config

    updates = payload.dict(exclude_none=True)
    with get_db_ctx() as db:
        row = (
            db.query(XueqiuStrategyConfig)
            .filter(XueqiuStrategyConfig.strategy_key == strategy_key)
            .first()
        )
        if row is None:
            row = XueqiuStrategyConfig(strategy_key=strategy_key)
            db.add(row)
        for key, value in updates.items():
            setattr(row, key, value)
    return load_xueqiu_strategy_config(strategy_key)

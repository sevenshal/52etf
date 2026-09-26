"""细分板块动量聚合：把持仓个股按板块汇总成板块级 5 日权价比/方向。

雪球持仓和东方财富持仓两个页面各自有一份几乎相同的"按板块聚合"逻辑，原来只对同花顺
(THS) 概念/行业/主题板块算一次。申万一二三级行业的成分表结构（板块代码 + 成分代码 +
进出日期区间）跟同花顺板块表完全一致，只是同花顺是多对多（一只股票可属于多个概念/
行业/主题板块），申万是严格的一股一行业树（每只股票在每一级只属于一个行业）——这个
差异体现在“成分归属”本身，不影响按板块汇总的 SQL 形状，所以两边共用同一套聚合模板。

调用方（xueqiu_holdings.py / eastmoney_holdings.py）先各自算好逐股的方向/日内涨跌代理
（这部分依赖各自的持仓快照结构，没法复用），写进两张临时表，再对每个板块来源
（同花顺 + 申万一/二/三级）各调一次 ``aggregate_board_rows``——逐股动量只算一次，
按板块分组算 4 次，避免为每个维度重跑一次昂贵的价格动量计算。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Tuple

from .duckdb_analytics import (
    duckdb_query_dicts as _duckdb_query_dicts,
    duckdb_table_exists as _duckdb_table_exists,
    safe_float as _safe_float,
)

SW_LEVELS: Tuple[str, ...] = ("L1", "L2", "L3")
SW_INDUSTRY_TABLE = "a_stock_sw_industry"
SW_MEMBER_CHANGE_TABLE = "a_stock_sw_member_change"
SW_DAILY_TABLE = "a_stock_sw_daily"
THS_MEMBER_TABLE = "a_stock_ths_member"
THS_DAILY_TABLE = "a_stock_ths_daily"

BOARD_SOURCES: Tuple[str, ...] = ("ths",) + tuple(f"sw_{level.lower()}" for level in SW_LEVELS)


def is_sw_board_code(code: Any) -> bool:
    """申万行业指数代码统一 ``.SI`` 后缀，同花顺板块代码统一 ``.TI`` 后缀。"""
    return str(code or "").strip().upper().endswith(".SI")


def board_source_of(code: Any) -> str:
    return "sw" if is_sw_board_code(code) else "ths"


def membership_source(source: str) -> Tuple[str, str]:
    """某个板块来源对应的（成分表名, 板块代码列名）。"""
    if source == "ths":
        return THS_MEMBER_TABLE, "ths_code"
    return SW_MEMBER_CHANGE_TABLE, "index_code"


def load_sw_board_catalog(connection, level: str) -> Dict[str, Dict[str, Any]]:
    """某一级申万行业目录：{index_code: {code, name, level}}。"""
    if not _duckdb_table_exists(connection, SW_INDUSTRY_TABLE):
        return {}
    rows = connection.execute(
        f"SELECT index_code, industry_name FROM {SW_INDUSTRY_TABLE} WHERE level = ?",
        [level],
    ).fetchall()
    return {
        str(index_code).upper(): {
            "code": str(index_code).upper(),
            "name": industry_name,
            "level": level,
        }
        for index_code, industry_name in rows
    }


def load_all_board_catalogs(
    connection, ths_catalog: Dict[str, Dict[str, Any]]
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """同花顺 + 申万一/二/三级，四套板块目录一起返回，key 与 ``BOARD_SOURCES`` 对应。"""
    catalogs: Dict[str, Dict[str, Dict[str, Any]]] = {"ths": ths_catalog}
    for level in SW_LEVELS:
        catalogs[f"sw_{level.lower()}"] = load_sw_board_catalog(connection, level)
    return catalogs


def aggregate_board_rows(
    connection,
    cte: str,
    snapshot_date: Any,
    compare_snapshot_date: Any,
    *,
    membership_table: str,
    code_column: str,
    catalog: Dict[str, Dict[str, Any]],
    min_stocks: int,
    stock_directions_table: str,
    stock_intraday_prices_table: str,
    direction_fn: Callable[[Any, Any, Any], str],
) -> List[Dict[str, Any]]:
    """把持仓聚合到板块：composite_weight_pct、5日权价比、方向、逆势吸筹占比。

    调用方必须先把逐股方向写进 ``stock_directions_table``（列：stock_symbol, direction,
    holding_cube_count），逐股日内价格代理写进 ``stock_intraday_prices_table``（列：
    stock_symbol, price_multiple_5d）——这两张临时表由调用方按各自的快照结构算好，
    这里只负责按板块分组汇总。
    """
    if not catalog or not _duckdb_table_exists(connection, membership_table):
        return []
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
                {code_column} AS board_code,
                CASE
                    WHEN con_code LIKE '%.SH' THEN 'SH.' || LEFT(con_code, 6)
                    WHEN con_code LIKE '%.SZ' THEN 'SZ.' || LEFT(con_code, 6)
                    WHEN con_code LIKE '%.BJ' THEN 'BJ.' || LEFT(con_code, 6)
                    ELSE con_code
                END AS stock_symbol,
                in_date,
                out_date
            FROM {membership_table}
        ),
        current_board_weights AS (
            SELECT
                members.board_code,
                COUNT(DISTINCT current_stocks.stock_symbol) AS stock_count,
                COUNT(DISTINCT CASE
                    WHEN directions.direction = '逆势吸筹'
                         AND directions.holding_cube_count >= 3
                    THEN current_stocks.stock_symbol
                END) AS contrarian_stock_count,
                SUM(current_stocks.composite_weight_pct) AS composite_weight_pct,
                SUM(current_stocks.holding_cube_count) AS stock_cube_links,
                AVG(intraday_prices.price_multiple_5d) AS price_multiple_5d,
                COUNT(intraday_prices.price_multiple_5d) AS priced_stock_count
            FROM normalized_members members
            JOIN current_stocks ON current_stocks.stock_symbol = members.stock_symbol
            LEFT JOIN {stock_directions_table} directions
              ON directions.stock_symbol = current_stocks.stock_symbol
            LEFT JOIN {stock_intraday_prices_table} intraday_prices
              ON intraday_prices.stock_symbol = current_stocks.stock_symbol
            WHERE (members.in_date IS NULL OR members.in_date <= CAST(? AS DATE))
              AND (members.out_date IS NULL OR members.out_date > CAST(? AS DATE))
            GROUP BY members.board_code
        ),
        compare_board_weights AS (
            SELECT
                members.board_code,
                SUM(compare_stocks.composite_weight_pct) AS weight_5d_ago
            FROM normalized_members members
            JOIN compare_stocks ON compare_stocks.stock_symbol = members.stock_symbol
            WHERE (members.in_date IS NULL OR members.in_date <= CAST(? AS DATE))
              AND (members.out_date IS NULL OR members.out_date > CAST(? AS DATE))
            GROUP BY members.board_code
        )
        SELECT
            current_board_weights.*,
            compare_board_weights.weight_5d_ago
        FROM current_board_weights
        JOIN compare_board_weights USING (board_code)
        WHERE current_board_weights.stock_count >= {min_stocks}
          AND current_board_weights.priced_stock_count > 0
        """,
        [
            snapshot_date,
            compare_snapshot_date,
            snapshot_date,
            snapshot_date,
            compare_snapshot_date,
            compare_snapshot_date,
        ],
    )
    items: List[Dict[str, Any]] = []
    for row in rows:
        meta = catalog.get(str(row.get("board_code") or "").upper())
        current_weight = _safe_float(row.get("composite_weight_pct"))
        old_weight = _safe_float(row.get("weight_5d_ago"))
        price_multiple = _safe_float(row.get("price_multiple_5d"))
        if not meta or not current_weight or not old_weight or not price_multiple:
            continue
        weight_multiple = current_weight / old_weight
        ratio = weight_multiple / price_multiple if price_multiple > 0 else None
        direction = direction_fn(weight_multiple, price_multiple, ratio)
        items.append(
            {
                **meta,
                "stock_count": int(row.get("stock_count") or 0),
                "contrarian_stock_count": int(row.get("contrarian_stock_count") or 0),
                "contrarian_stock_ratio_pct": round(
                    int(row.get("contrarian_stock_count") or 0)
                    * 100.0
                    / int(row.get("stock_count") or 1),
                    2,
                ),
                "stock_cube_links": int(row.get("stock_cube_links") or 0),
                "priced_stock_count": int(row.get("priced_stock_count") or 0),
                "price_source": "held_constituent_equal_weight_intraday",
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


def top_contrarian_boards(
    items: List[Dict[str, Any]], *, min_stocks: int, limit: int = 15
) -> List[Dict[str, Any]]:
    """逆势吸筹且覆盖股数达标的板块，按吸筹股占比取前 N。"""
    return sorted(
        [
            item
            for item in items
            if item.get("direction") == "逆势吸筹"
            and int(item.get("stock_count") or 0) >= min_stocks
            and float(item.get("contrarian_stock_ratio_pct") or 0) > 0
        ],
        key=lambda item: (
            -float(item.get("contrarian_stock_ratio_pct") or 0),
            -int(item.get("contrarian_stock_count") or 0),
            -float(item.get("composite_weight_pct") or 0),
        ),
    )[:limit]

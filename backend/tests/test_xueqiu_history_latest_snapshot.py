"""雪球持仓历史接口返回全局最新快照日，供个股详情判断"当前排行"是否还有效。

历史只包含这只股票上榜那些天的行，latest 是它最后一次上榜——跌出榜单后那一行的排名是旧的。
复用 test_factor_lab_xueqiu_top_holdings 的快照库构造函数（按模块导入，避免 pytest 重复收集那 19 个用例）。
"""

from datetime import date
from unittest.mock import patch

import duckdb
import pytest

import test_factor_lab_xueqiu_top_holdings as base
from src.app.api import xueqiu_holdings as factor_lab


def _history(db_path, symbol):
    with patch("src.core.services.duckdb_analytics.ANALYTICS_DB_PATH", str(db_path)), \
            patch.object(factor_lab, "XUEQIU_HOLDINGS_VALID_FROM", date(2000, 1, 1)):
        return factor_lab.load_xueqiu_top_holdings_history(symbol=symbol, active_only=True, limit=10)


@pytest.fixture
def snapshot_db(tmp_path):
    path = tmp_path / "analytics.duckdb"
    base.FactorLabXueqiuTopHoldingsTest._create_snapshot_db(None, str(path))
    # 股票D 只在 06-21 被 组合一 持有、06-22 已清仓：复制一行真实持仓只换股票，其余筛选字段原样
    con = duckdb.connect(str(path))
    con.execute("""
        INSERT INTO xueqiu_cube_holdings_snapshots
        SELECT * REPLACE ('SH.600009' AS stock_symbol, 'SH600009' AS raw_stock_symbol,
                          '股票D' AS stock_name, 5.0 AS weight_pct)
        FROM xueqiu_cube_holdings_snapshots
        WHERE snapshot_date = DATE '2026-06-21' AND cube_symbol = 'ZH1' AND stock_symbol = 'SH.600002'
    """)
    con.close()
    return path


def test_a_stock_still_listed_ends_on_the_global_latest_snapshot(snapshot_db):
    result = _history(snapshot_db, "SH600001")

    assert result["latest_snapshot_date"] == "2026-06-22"
    assert result["latest"]["snapshot_date"] == result["latest_snapshot_date"]
    assert result["latest"]["composite_rank"] == 1


def test_a_stock_that_dropped_out_ends_before_the_global_latest_snapshot(snapshot_db):
    """06-21 还在榜上，06-22 已不在——latest 里那个排名是 06-21 的旧排名，不能当成当前排名。"""
    result = _history(snapshot_db, "SH600009")

    assert [row["snapshot_date"] for row in result["history"]] == ["2026-06-21"]
    # 06-21 当天由综合排名接口自己排出的名次（排名口径以接口为准，这里只锁住它是旧日期的值）
    assert result["latest"]["composite_rank"] == 4
    assert result["latest_snapshot_date"] == "2026-06-22"
    assert result["latest"]["snapshot_date"] < result["latest_snapshot_date"]


def test_missing_snapshot_table_reports_no_latest_snapshot_date(tmp_path):
    empty = tmp_path / "empty.duckdb"
    duckdb.connect(str(empty)).close()

    result = _history(empty, "SH600001")

    assert result["available"] is False
    assert result["latest_snapshot_date"] is None
    assert result["history"] == []

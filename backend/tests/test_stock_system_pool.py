"""选股系统基本面股票池：股票池范围、从宽的硬闸门、可配置权重的软评分、快照落库。"""

from datetime import date, timedelta

import duckdb
import pytest

from src.core.duckdb_utils import ANALYTICS_DB_PATH, connect_duckdb
from src.core.services.stock_system import config as system_config
from src.core.services.stock_system import fundamental_pool as pool

AS_OF = date(2026, 9, 11)
TRADE_DATES = [date(2026, 9, day) for day in (1, 2, 3, 4, 7, 8, 9, 10, 11)]
RECENT_FIVE = TRADE_DATES[-5:]

# ts_code: (名称, 行业, 近期市值(亿), 近期成交额(万))
STOCKS = {
    "600001.SH": ("好公司", "电子", 100, 5000),
    "600002.SH": ("乙公司", "电子", 100, 5000),      # 以前是 *ST，as_of 时已摘帽
    "600003.SH": ("ST丙", "电子", 100, 5000),        # as_of 前两天刚被 ST
    "600004.SH": ("小市值", "电子", 40, 5000),
    "600005.SH": ("成交清淡", "电子", 100, 1000),
    "600006.SH": ("商誉公司", "传媒", 60, 5000),
    "600007.SH": ("亏损公司", "电子", 100, 5000),
    "600008.SH": ("某银行", "银行", 800, 50000),
    "600009.SH": ("现金流差", "电子", 100, 5000),
    "600010.SH": ("无财报", "电子", 100, 5000),
}

SCAN = {
    "600001.SH": dict(expected_return_pct=40.0, latest_roic_pct=18.0, ocf_to_net_profit_ttm=1.2, debt_to_assets_pct=40.0, pe_ttm=20.0),
    "600002.SH": dict(expected_return_pct=10.0, latest_roic_pct=12.0, ocf_to_net_profit_ttm=0.9, debt_to_assets_pct=None, pe_ttm=25.0),
    "600006.SH": dict(expected_return_pct=5.0, latest_roic_pct=9.0, ocf_to_net_profit_ttm=1.0, debt_to_assets_pct=30.0, pe_ttm=30.0),
    "600007.SH": dict(expected_return_pct=-20.0, latest_roic_pct=-5.0, ocf_to_net_profit_ttm=None, debt_to_assets_pct=50.0, pe_ttm=None),
    "600008.SH": dict(expected_return_pct=30.0, is_financial=True, latest_roic_pct=None, latest_roe_pct=11.0, debt_to_assets_pct=92.0, pe_ttm=5.0),
    "600009.SH": dict(expected_return_pct=60.0, latest_roic_pct=15.0, ocf_to_net_profit_ttm=-0.4, debt_to_assets_pct=45.0, pe_ttm=15.0),
}


@pytest.fixture
def market_db(tmp_path):
    path = tmp_path / "pool.duckdb"
    connection = duckdb.connect(str(path))
    connection.execute(
        "CREATE TABLE a_stock_market_daily (ts_code VARCHAR, trade_date DATE, close DOUBLE, total_mv DOUBLE, amount DOUBLE)"
    )
    rows = []
    for ts_code, (_, _, mv_100m, amount_10k) in STOCKS.items():
        for trade_date in TRADE_DATES:
            mv = mv_100m
            # 早于最近 5 个交易日的行情不参与平均：前面放一个很大的市值，平均时不能算进去
            if trade_date not in RECENT_FIVE:
                mv = 10_000
            rows.append((ts_code, trade_date, 20.0, mv * 1e4, amount_10k * 10.0))
    # 一只行情晚于 as_of 的"未来"交易日：不能被读进来
    rows.append(("600004.SH", AS_OF + timedelta(days=3), 20.0, 1e9, 1e9))
    connection.executemany("INSERT INTO a_stock_market_daily VALUES (?, ?, ?, ?, ?)", rows)

    connection.execute("CREATE TABLE a_stock_basic (ts_code VARCHAR, name VARCHAR, industry VARCHAR)")
    connection.executemany(
        "INSERT INTO a_stock_basic VALUES (?, ?, ?)",
        [(ts_code, "现名" + name, industry) for ts_code, (name, industry, _, _) in STOCKS.items()],
    )
    connection.execute(
        "CREATE TABLE a_stock_name_changes (id VARCHAR, ts_code VARCHAR, name VARCHAR, start_date DATE,"
        " end_date DATE, change_reason VARCHAR)"
    )
    name_changes = [
        ("n1", "600002.SH", "*ST乙", date(2024, 1, 1), date(2026, 9, 5), "ST"),
        ("n2", "600002.SH", "乙公司", date(2026, 9, 6), None, "撤销ST"),
        ("n3", "600003.SH", "丙公司", date(2010, 1, 1), date(2026, 9, 8), "改名"),
        ("n4", "600003.SH", "ST丙", date(2026, 9, 9), None, "ST"),
    ]
    for ts_code, (name, *_rest) in STOCKS.items():
        if ts_code not in ("600002.SH", "600003.SH"):
            name_changes.append((f"n-{ts_code}", ts_code, name, date(2010, 1, 1), None, "上市"))
    connection.executemany("INSERT INTO a_stock_name_changes VALUES (?, ?, ?, ?, ?, ?)", name_changes)

    connection.execute(
        "CREATE TABLE a_stock_fina_indicator (ts_code VARCHAR, end_date DATE, ann_date DATE, or_yoy DOUBLE,"
        " dt_netprofit_yoy DOUBLE, netprofit_yoy DOUBLE, q_sales_yoy DOUBLE)"
    )
    fina = [
        ("600001.SH", date(2026, 3, 31), date(2026, 4, 25), 30.0, 40.0, 38.0, 35.0),
        # as_of 之后才披露的半年报：不能被读进来
        ("600001.SH", date(2026, 6, 30), date(2026, 9, 20), 99.0, 99.0, 99.0, 99.0),
        ("600002.SH", date(2026, 6, 30), date(2026, 8, 20), 10.0, None, 12.0, 8.0),
        ("600006.SH", date(2026, 6, 30), date(2026, 8, 20), 5.0, 5.0, 5.0, 5.0),
        ("600007.SH", date(2026, 6, 30), date(2026, 8, 20), -10.0, -80.0, -80.0, -12.0),
        ("600008.SH", date(2026, 6, 30), date(2026, 8, 20), 3.0, 4.0, 4.0, 2.0),
        ("600009.SH", date(2026, 6, 30), date(2026, 8, 20), 25.0, 30.0, 30.0, 28.0),
    ]
    connection.executemany("INSERT INTO a_stock_fina_indicator VALUES (?, ?, ?, ?, ?, ?, ?)", fina)

    connection.execute(
        "CREATE TABLE a_stock_balancesheet (ts_code VARCHAR, end_date DATE, ann_date DATE, goodwill DOUBLE,"
        " total_hldr_eqy_exc_min_int DOUBLE)"
    )
    connection.executemany(
        "INSERT INTO a_stock_balancesheet VALUES (?, ?, ?, ?, ?)",
        [
            ("600001.SH", date(2026, 6, 30), date(2026, 8, 20), None, 1.0e10),
            ("600006.SH", date(2026, 6, 30), date(2026, 8, 20), 6.0e9, 1.0e10),
        ],
    )

    connection.execute("CREATE TABLE a_stock_adj_factor (ts_code VARCHAR, trade_date DATE, adj_factor DOUBLE)")
    connection.executemany(
        "INSERT INTO a_stock_adj_factor VALUES (?, ?, ?)",
        [
            ("600001.SH", date(2026, 7, 1), 1.0),
            ("600001.SH", date(2026, 7, 13), 1.0),
            # 7 月底 10 送 10：复权因子翻倍，原始股价减半
            ("600001.SH", date(2026, 7, 31), 2.0),
            ("600001.SH", AS_OF, 2.0),
        ],
    )
    connection.close()
    return path


def _scan_fn(**kwargs):
    assert kwargs["as_of"] == AS_OF
    assert kwargs["force_valuation"] is True
    candidates = [
        {"ts_code": ts_code, "is_financial": False, **values}
        for ts_code, values in SCAN.items()
        if ts_code in kwargs["symbols"]
    ]
    return {"status": "completed", "candidates": candidates, "excluded_sample": []}


def _consensus_loader(symbols, day):
    if day >= AS_OF - timedelta(days=5):
        return {
            "600001.SH": {"fair_value_lo": 30.0, "fair_value_mid": 40.0, "last_price": 20.0, "growth_pct": 25.0},
            "600009.SH": {"fair_value_lo": 18.0, "fair_value_mid": 22.0, "last_price": 20.0, "growth_pct": 10.0},
        }
    # 除权前的旧估值：原始价格口径是现在的两倍
    return {"600001.SH": {"fair_value_mid": 70.0, "last_price": 40.0}}


def _compute(market_db, config=None):
    return pool.compute_fundamental_pool(
        as_of=AS_OF,
        config=config if config is not None else system_config.default_stock_system_config(),
        scan_fn=_scan_fn,
        consensus_loader=_consensus_loader,
        connect=lambda: duckdb.connect(str(market_db), read_only=True),
    )


def _rows(result):
    return {row["ts_code"]: row for row in result["rows"]}


def test_universe_uses_recent_average_and_point_in_time_st(market_db):
    result = _compute(market_db)
    rows = _rows(result)

    assert result["trade_date"] == AS_OF.isoformat()
    assert "600002.SH" in rows            # as_of 时已摘帽
    assert "600003.SH" not in rows        # as_of 时是 ST
    assert "600004.SH" not in rows        # 近 5 日平均市值 40 亿，早期的大市值不算
    assert "600005.SH" not in rows        # 成交额 1000 万
    assert rows["600001.SH"]["avg_total_mv_100m"] == 100.0
    assert rows["600001.SH"]["avg_amount_10k"] == 5000.0
    assert result["summary"]["universe_excluded"] == {
        "ST/退市整理": 1,
        "近5日平均市值不足50亿": 1,
        "近5日平均成交额不足1500万": 1,
    }


def test_gates_only_block_obvious_problems(market_db):
    rows = _rows(_compute(market_db))

    assert rows["600001.SH"]["gate_passed"] is True
    assert rows["600007.SH"]["gate_passed"] is False
    assert "ROIC" in rows["600007.SH"]["gate_reasons"][0]
    assert rows["600009.SH"]["gate_passed"] is False
    assert "经营现金流" in rows["600009.SH"]["gate_reasons"][0]
    assert rows["600006.SH"]["gate_passed"] is False
    assert "商誉" in rows["600006.SH"]["gate_reasons"][0]
    assert rows["600010.SH"]["gate_reasons"] == ["缺少财务数据，无法判断基本面"]
    # 银行：负债率 92% 不拦（不适用），ROE 闸门通过
    assert rows["600008.SH"]["gate_passed"] is True
    # 缺数据只记提示，不拦
    assert rows["600002.SH"]["gate_passed"] is True
    assert any("资产负债率" in note for note in rows["600002.SH"]["gate_notes"])


def test_disabled_gate_lets_the_stock_through(market_db):
    config = system_config.default_stock_system_config()
    config["gates"]["min_latest_roic_pct"]["enabled"] = False
    assert _rows(_compute(market_db, config))["600007.SH"]["gate_passed"] is True


def test_reports_disclosed_after_as_of_are_ignored(market_db):
    row = _rows(_compute(market_db))["600001.SH"]
    assert row["revenue_yoy_pct"] == 30.0
    assert row["profit_yoy_pct"] == 40.0


def test_profit_growth_falls_back_to_net_profit_yoy(market_db):
    assert _rows(_compute(market_db))["600002.SH"]["profit_yoy_pct"] == 12.0


def test_consensus_revision_is_adjusted_for_ex_rights(market_db):
    row = _rows(_compute(market_db))["600001.SH"]
    assert row["consensus_upside_pct"] == pytest.approx(50.0)
    # 旧中枢 70 在除权前口径，换算到今天是 35，新中枢 40 → 上修 14.29%
    assert row["consensus_revision_pct"] == pytest.approx((40.0 / 35.0 - 1) * 100)


def test_scoring_follows_configured_weights_and_pool_size(market_db):
    config = system_config.default_stock_system_config()
    for key in config["factors"]:
        config["factors"][key]["weight"] = 0.0
    config["factors"]["dcf_return_pct"]["weight"] = 1.0
    config["pool"]["size"] = 2
    rows = _rows(_compute(market_db, config))

    # 通过闸门的：600001(40) 600008(30) 600002(10)
    assert rows["600001.SH"]["pool_rank"] == 1 and rows["600001.SH"]["in_pool"] is True
    assert rows["600008.SH"]["pool_rank"] == 2 and rows["600008.SH"]["in_pool"] is True
    assert rows["600002.SH"]["pool_rank"] == 3 and rows["600002.SH"]["in_pool"] is False
    assert rows["600001.SH"]["composite_score"] == 100.0
    assert rows["600001.SH"]["valuation_score"] == 100.0
    assert rows["600001.SH"]["growth_score"] is None
    # 没过闸门的不参与排名
    assert rows["600009.SH"]["pool_rank"] is None


def test_low_factor_coverage_gets_no_score(market_db):
    config = system_config.default_stock_system_config()
    config["scoring"]["min_factor_coverage"] = 0.9
    rows = _rows(_compute(market_db, config))
    # 600002 没有共识估值、没有扣非增速，有值因子的权重占比达不到 90%
    assert rows["600002.SH"]["coverage"] < 0.9
    assert rows["600002.SH"]["composite_score"] is None
    assert rows["600002.SH"]["pool_rank"] is None


def test_snapshot_round_trip_keeps_nulls(market_db):
    result = _compute(market_db)
    pool.save_pool_snapshot(result)
    # 同一交易日重跑覆盖，不会重复
    pool.save_pool_snapshot(result)

    connect = lambda: connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True)  # noqa: E731
    loaded = pool.load_pool_snapshot(view="all", connect=connect)
    assert loaded["run"]["trade_date"] == AS_OF.isoformat()
    assert loaded["run"]["summary"]["universe_size"] == result["summary"]["universe_size"]
    assert loaded["run"]["config"] == result["config"]
    assert len(loaded["rows"]) == len(result["rows"])

    by_symbol = {row["ts_code"]: row for row in loaded["rows"]}
    assert by_symbol["600001.SH"]["composite_score"] == _rows(result)["600001.SH"]["composite_score"]
    assert by_symbol["600010.SH"]["dcf_return_pct"] is None      # NULL 而不是 NaN
    assert by_symbol["600007.SH"]["gate_reasons"] == _rows(result)["600007.SH"]["gate_reasons"]
    assert by_symbol["600001.SH"]["factor_scores"]["dcf_return_pct"] > 0

    in_pool = pool.load_pool_snapshot(view="pool", connect=connect)["rows"]
    assert [row["pool_rank"] for row in in_pool] == sorted(row["pool_rank"] for row in in_pool)
    excluded = pool.load_pool_snapshot(view="excluded", connect=connect)["rows"]
    assert {row["ts_code"] for row in excluded} == {"600006.SH", "600007.SH", "600009.SH", "600010.SH"}
    assert pool.list_pool_runs(connect=connect)[0]["trade_date"] == AS_OF.isoformat()


def test_config_normalization_clamps_and_drops_unknown_keys():
    config = system_config.normalize_stock_system_config({
        "universe": {"avg_window_days": "0", "min_avg_total_mv_100m": -5, "exclude_st": "false", "bogus": 1},
        "gates": {"min_latest_roic_pct": {"enabled": "off", "threshold": "3.5"}},
        "factors": {"dcf_return_pct": {"weight": 1000}, "unknown_factor": {"weight": 1}},
        "scoring": {"min_factor_coverage": 2},
        "pool": {"size": 0},
    })
    assert config["universe"] == {
        "exclude_st": False,
        "avg_window_days": 1,
        "min_avg_total_mv_100m": 0.0,
        "min_avg_amount_10k": 1500.0,
    }
    assert config["gates"]["min_latest_roic_pct"] == {"enabled": False, "threshold": 3.5}
    assert config["factors"]["dcf_return_pct"] == {"enabled": True, "weight": 100.0}
    assert "unknown_factor" not in config["factors"]
    assert config["scoring"]["min_factor_coverage"] == 1.0
    assert config["pool"]["size"] == 1


def test_config_save_and_reset_round_trip():
    config = system_config.default_stock_system_config()
    config["pool"]["size"] = 42
    saved = system_config.save_stock_system_config(config, updated_by="tester")
    assert saved["pool"]["size"] == 42
    assert saved["updated_by"] == "tester"
    assert system_config.load_stock_system_config()["pool"]["size"] == 42
    assert system_config.reset_stock_system_config()["pool"]["size"] == 100

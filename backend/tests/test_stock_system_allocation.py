"""选股系统第二层：情绪择时（顶/底状态机）与仓位控制（总仓位、板块上限、目标仓位）。"""

from datetime import date, timedelta

import duckdb
import pytest

from src.core.duckdb_utils import ANALYTICS_DB_PATH, connect_duckdb
from src.core.services.stock_system import allocation
from src.core.services.stock_system import config as system_config
from src.core.services.stock_system import sentiment

AS_OF = date(2026, 9, 11)
TIMING = system_config.default_stock_system_config()["timing"]
MARKET = "000985.SH"
SEMI = "H30184.CSI"          # 半导体（87 只）
SEMI_EQUIP = "931743.CSI"    # 半导体材料设备（40 只，更细）
BROKER = "399975.SZ"         # 证券公司
LIQUOR = "399997.SZ"         # 中证白酒


def _history(marks, *, days=30, last_score=50.0, end=AS_OF):
    """按交易日升序造一段贪恐历史；marks = {倒数第几天: [kind, ...]}。"""
    rows = []
    for offset in range(days - 1, -1, -1):
        day = end - timedelta(days=offset)
        kinds = marks.get(offset, [])
        rows.append({
            "date": day.isoformat(),
            "score": last_score if offset == 0 else 50.0,
            "signals": [{"kind": kind, "label": {"ma5_bottom": "均线底", "ma5_top": "均线顶",
                                                 "volume_bottom": "放量底", "volume_top": "缩量顶"}[kind]}
                        for kind in kinds],
        })
    return rows


# --- 状态机 ---------------------------------------------------------------

def test_last_bottom_means_offense_and_last_top_means_defense():
    offense = sentiment.classify_regime(_history({10: ["ma5_top"], 3: ["volume_bottom"]}), AS_OF, TIMING)
    assert offense["state"] == sentiment.STATE_OFFENSE
    assert offense["signal"]["label"] == "放量底"
    assert offense["days_since_signal"] == 3

    defense = sentiment.classify_regime(_history({10: ["ma5_bottom"], 2: ["ma5_top"]}), AS_OF, TIMING)
    assert defense["state"] == sentiment.STATE_DEFENSE
    assert sentiment.cap_state(defense) == sentiment.STATE_DEFENSE


def test_same_day_conflict_uses_the_last_mark():
    regime = sentiment.classify_regime(_history({1: ["ma5_top", "volume_bottom"]}), AS_OF, TIMING)
    assert regime["state"] == sentiment.STATE_OFFENSE


def test_no_signal_expired_signal_and_stale_data_are_neutral():
    assert sentiment.classify_regime(_history({}), AS_OF, TIMING)["state"] == sentiment.STATE_NEUTRAL

    expiring = {**TIMING, "signal_expiry_days": 5}
    expired = sentiment.classify_regime(_history({8: ["ma5_bottom"]}), AS_OF, expiring)
    assert expired["state"] == sentiment.STATE_NEUTRAL
    assert "有效期" in expired["note"]

    stale = sentiment.classify_regime(_history({2: ["ma5_bottom"]}, end=AS_OF - timedelta(days=20)), AS_OF, TIMING)
    assert stale["state"] == sentiment.STATE_NEUTRAL and stale["stale"] is True


def test_history_after_as_of_is_ignored():
    rows = _history({1: ["ma5_bottom"]}) + [
        {"date": (AS_OF + timedelta(days=1)).isoformat(), "score": 90.0, "signals": [{"kind": "ma5_top", "label": "均线顶"}]},
    ]
    regime = sentiment.classify_regime(rows, AS_OF, TIMING)
    assert regime["state"] == sentiment.STATE_OFFENSE
    assert regime["score"] == 50.0


def test_overheat_blocks_entries_even_after_a_bottom():
    regime = sentiment.classify_regime(_history({3: ["ma5_bottom"]}, last_score=85.0), AS_OF, TIMING)
    assert regime["state"] == sentiment.STATE_OFFENSE and regime["overheated"] is True
    assert not sentiment.entry_allowed(regime)
    assert sentiment.cap_state(regime) == sentiment.STATE_DEFENSE
    relaxed = sentiment.classify_regime(_history({3: ["ma5_bottom"]}, last_score=85.0), AS_OF, {**TIMING, "overheat_enabled": False})
    assert sentiment.entry_allowed(relaxed)


# --- 板块归属 ---------------------------------------------------------------

def test_stock_goes_to_its_most_specific_sector_index():
    catalog = sentiment.target_catalog()
    memberships = {
        "688012.SH": [{"symbol": "000688.SH"}, {"symbol": SEMI}, {"symbol": SEMI_EQUIP}],
        "600030.SH": [{"symbol": "000300.SH"}, {"symbol": BROKER}],
        "000001.SZ": [{"symbol": "000300.SH"}],                  # 只在宽基里
    }
    counts = {"000688.SH": 50, SEMI: 87, SEMI_EQUIP: 40, BROKER: 49, "000300.SH": 300}
    sectors = allocation.assign_sectors(memberships, counts, catalog)
    assert sectors == {"688012.SH": SEMI_EQUIP, "600030.SH": BROKER, "000001.SZ": None}

    # 成分数一样时取配置里更靠后（更细分）的
    tied = allocation.assign_sectors({"688012.SH": memberships["688012.SH"]}, {SEMI: 40, SEMI_EQUIP: 40}, catalog)
    assert tied["688012.SH"] == SEMI_EQUIP


# --- 仓位分配 ---------------------------------------------------------------

POSITION = {
    **system_config.default_stock_system_config()["position"],
    "max_positions": 5,
    "max_single_weight_pct": 8.0,
    "min_position_weight_pct": 2.0,
}
NEUTRAL = {"state": sentiment.STATE_NEUTRAL, "overheated": False}
OFFENSE = {"state": sentiment.STATE_OFFENSE, "overheated": False}
DEFENSE = {"state": sentiment.STATE_DEFENSE, "overheated": False,
           "signal": {"date": "2026-09-01", "label": "均线顶"}}


def _pool(*symbols):
    return [{"ts_code": symbol, "name": symbol, "pool_rank": rank, "composite_score": 90 - rank}
            for rank, symbol in enumerate(symbols, start=1)]


def _plan(pool, sectors, regimes, market=NEUTRAL, position=POSITION):
    return allocation.plan_allocation(pool, sectors, regimes, market, position, {SEMI: "半导体", BROKER: "证券"})


def test_sector_cap_then_next_sector_takes_the_slot():
    pool = _pool("A", "B", "C", "D", "E")
    sectors = {"A": SEMI, "B": SEMI, "C": SEMI, "D": SEMI, "E": BROKER}
    result = _plan(pool, sectors, {SEMI: NEUTRAL, BROKER: NEUTRAL})
    rows = {row["ts_code"]: row for row in result["rows"]}

    # 市场中性 → 总仓位 70%，单只 = min(8%, 70%/5) = 8%；中性板块上限 20%
    assert [rows[s]["target_weight_pct"] for s in "ABC"] == [8.0, 8.0, 4.0]
    assert rows["D"]["status"] == allocation.STATUS_SKIPPED and "板块仓位已满" in rows["D"]["reason"]
    assert rows["E"]["target_weight_pct"] == 8.0
    assert result["summary"]["exposure_pct"] == 70.0
    assert result["summary"]["invested_pct"] == 28.0
    assert result["summary"]["positions"] == 4


def test_defense_and_overheated_sectors_open_no_new_positions():
    pool = _pool("A", "B", "C")
    sectors = {"A": SEMI, "B": BROKER, "C": None}
    overheated = {**OFFENSE, "overheated": True, "score": 86.0}
    result = _plan(pool, sectors, {SEMI: DEFENSE, BROKER: overheated})
    rows = {row["ts_code"]: row for row in result["rows"]}

    assert rows["A"]["status"] == allocation.STATUS_BLOCKED and "防守" in rows["A"]["reason"]
    assert rows["B"]["status"] == allocation.STATUS_BLOCKED and "过热" in rows["B"]["reason"]
    # 未归类的跟随市场（中性），用未归类上限
    assert rows["C"]["status"] == allocation.STATUS_TARGET
    assert rows["C"]["sector_code"] == allocation.UNMAPPED_SECTOR


def test_defense_cap_above_zero_allows_small_positions_but_overheat_still_blocks():
    pool = _pool("A", "B", "C")
    sectors = {"A": SEMI, "B": SEMI, "C": BROKER}
    overheated_defense = {**DEFENSE, "overheated": True, "score": 88.0}
    position = {**POSITION, "sector_cap_defense_pct": 5.0}
    rows = {row["ts_code"]: row for row in _plan(pool, sectors, {SEMI: DEFENSE, BROKER: overheated_defense}, position=position)["rows"]}

    assert rows["A"]["target_weight_pct"] == 5.0
    assert rows["B"]["status"] == allocation.STATUS_SKIPPED and "板块仓位已满" in rows["B"]["reason"]
    assert rows["C"]["status"] == allocation.STATUS_BLOCKED and "过热" in rows["C"]["reason"]


def test_market_state_sets_total_exposure_and_max_positions_stop():
    pool = _pool("A", "B", "C", "D", "E", "F", "G")
    sectors = {symbol: None for symbol in "ABCDEFG"}
    position = {**POSITION, "max_positions": 3, "max_single_weight_pct": 50.0, "unmapped_sector_cap_pct": 100.0}

    offense = _plan(pool, sectors, {}, market=OFFENSE, position=position)
    assert offense["summary"]["exposure_pct"] == 100.0
    assert offense["summary"]["positions"] == 3
    assert offense["rows"][3]["reason"] == "已达最大持仓数 3"

    defense_market = {**DEFENSE}
    # 市场防守：未归类股票跟随市场，不开新仓
    assert _plan(pool, sectors, {}, market=defense_market, position=position)["summary"]["positions"] == 0
    mapped = {symbol: BROKER for symbol in "ABCDEFG"}
    defended = _plan(pool, mapped, {BROKER: OFFENSE}, market=defense_market,
                     position={**position, "sector_cap_offense_pct": 100.0})
    # 板块进攻仍可开仓，但总仓位按市场防守档 40%
    assert defended["summary"]["exposure_pct"] == 40.0
    assert defended["summary"]["invested_pct"] == pytest.approx(40.0)


# --- 端到端 + 落库 ------------------------------------------------------------

@pytest.fixture
def weights_db(tmp_path):
    path = tmp_path / "weights.duckdb"
    connection = duckdb.connect(str(path))
    connection.execute("CREATE TABLE a_stock_index_weight (index_code VARCHAR, con_code VARCHAR, trade_date DATE, weight DOUBLE)")
    rows = []
    for index_code, count in ((SEMI, 87), (SEMI_EQUIP, 40), (BROKER, 49), (LIQUOR, 17)):
        rows += [(index_code, f"{i:06d}.SZ", date(2026, 8, 31), 1.0) for i in range(count)]
    rows.append((SEMI, "999999.SZ", AS_OF + timedelta(days=5), 1.0))   # 未来快照不能算进来
    connection.executemany("INSERT INTO a_stock_index_weight VALUES (?, ?, ?, ?)", rows)
    connection.close()
    return path


def _pool_loader(trade_date, view="pool"):
    return {
        "run": {"trade_date": AS_OF.isoformat()},
        "rows": [
            {"ts_code": "688012.SH", "name": "中微公司", "industry": "半导体", "pool_rank": 1, "composite_score": 88.0},
            {"ts_code": "600030.SH", "name": "中信证券", "industry": "证券", "pool_rank": 2, "composite_score": 80.0},
            {"ts_code": "600519.SH", "name": "贵州茅台", "industry": "白酒", "pool_rank": 3, "composite_score": 75.0},
            {"ts_code": "000001.SZ", "name": "平安银行", "industry": "银行", "pool_rank": 4, "composite_score": 70.0},
        ],
    }


def _membership_loader(symbols, as_of):
    assert as_of == AS_OF
    return {
        "688012.SH": [{"symbol": SEMI}, {"symbol": SEMI_EQUIP}, {"symbol": "000688.SH"}],
        "600030.SH": [{"symbol": BROKER}, {"symbol": "000300.SH"}],
        "600519.SH": [{"symbol": LIQUOR}, {"symbol": "000300.SH"}],
        "000001.SZ": [{"symbol": "000300.SH"}],
    }


HISTORIES = {
    MARKET: _history({4: ["ma5_bottom"]}, last_score=55.0),
    SEMI_EQUIP: _history({2: ["volume_bottom"]}, last_score=40.0),
    BROKER: _history({6: ["ma5_bottom"], 1: ["ma5_top"]}, last_score=78.0),
    LIQUOR: _history({3: ["ma5_bottom"]}, last_score=20.0),
}


def _history_loader(symbol, as_of):
    assert as_of == AS_OF
    return HISTORIES.get(symbol, [])


def test_compute_save_and_load_allocation(weights_db):
    result = allocation.compute_allocation(
        config=system_config.default_stock_system_config(),
        pool_loader=_pool_loader,
        history_loader=_history_loader,
        membership_loader=_membership_loader,
        connect=lambda: duckdb.connect(str(weights_db), read_only=True),
    )
    assert result["status"] == "completed"
    assert result["market"]["state"] == sentiment.STATE_OFFENSE
    assert result["summary"]["exposure_pct"] == 100.0

    rows = {row["ts_code"]: row for row in result["rows"]}
    assert rows["688012.SH"]["sector_code"] == SEMI_EQUIP
    assert rows["688012.SH"]["target_weight_pct"] == 5.0          # min(8%, 100%/20)
    assert rows["600030.SH"]["status"] == allocation.STATUS_BLOCKED   # 证券最后是顶信号
    assert rows["600519.SH"]["status"] == allocation.STATUS_TARGET
    assert rows["000001.SZ"]["sector_code"] == allocation.UNMAPPED_SECTOR

    regimes = {row["index_code"]: row for row in result["regimes"]}
    assert regimes[MARKET]["is_market"] is True
    assert regimes[SEMI_EQUIP]["constituent_count"] == 40
    assert regimes[SEMI]["constituent_count"] == 87               # 未来的成分快照没算进来
    assert regimes[SEMI_EQUIP]["pool_members"] == 1
    assert regimes[SEMI_EQUIP]["allocated_pct"] == 5.0
    assert regimes[allocation.UNMAPPED_SECTOR]["pool_members"] == 1
    assert regimes["931160.CSI"]["state"] == sentiment.STATE_NEUTRAL   # 没有历史的板块按中性

    allocation.save_allocation(result)
    allocation.save_allocation(result)      # 同一交易日重跑覆盖
    loaded = allocation.load_allocation(connect=lambda: connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True))
    assert loaded["run"]["trade_date"] == AS_OF.isoformat()
    assert loaded["run"]["summary"]["positions"] == result["summary"]["positions"]
    assert len(loaded["rows"]) == 4 and len(loaded["regimes"]) == len(result["regimes"])
    loaded_rows = {row["ts_code"]: row for row in loaded["rows"]}
    assert loaded_rows["600030.SH"]["target_weight_pct"] is None
    assert loaded_rows["688012.SH"]["target_weight_pct"] == 5.0
    loaded_regimes = {row["index_code"]: row for row in loaded["regimes"]}
    assert loaded_regimes[BROKER]["signal_date"] == (AS_OF - timedelta(days=1)).isoformat()
    assert loaded_regimes[BROKER]["state_label"] == "防守"


def test_missing_pool_snapshot_reports_no_pool():
    result = allocation.compute_allocation(
        config=system_config.default_stock_system_config(),
        pool_loader=lambda trade_date, view="pool": {"run": None, "rows": []},
    )
    assert result["status"] == "no_pool"


def test_timing_and_position_config_normalization():
    config = system_config.normalize_stock_system_config({
        "timing": {"market_index": "931160.CSI", "overheat_score": 10, "signal_expiry_days": "3"},
        "position": {"max_positions": 0, "exposure_offense_pct": 150, "bogus": 1},
    })
    assert config["timing"]["market_index"] == MARKET        # 行业指数不能当市场指数
    assert config["timing"]["overheat_score"] == 50.0
    assert config["timing"]["signal_expiry_days"] == 3
    assert config["position"]["max_positions"] == 1
    assert config["position"]["exposure_offense_pct"] == 100.0
    assert "bogus" not in config["position"]
    assert {"symbol": "000300.SH", "name": "沪深300"} in system_config.config_definitions()["market_index_options"]

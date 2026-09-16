"""选股系统第三层：入场触发、雪球过滤、出场规则、模拟盘撮合，以及两天的端到端流程。"""

import copy
from datetime import date, datetime, time, timedelta

import duckdb
import pytest

from src.core.duckdb_utils import ANALYTICS_DB_PATH, connect_duckdb
from src.core.services.stock_system import config as system_config
from src.core.services.stock_system import paper, technical, trading

DEFAULTS = system_config.default_stock_system_config()
SIGNALS = DEFAULTS["signals"]
START = date(2026, 1, 1)


def _bar(index, close, **extra):
    day = START + timedelta(days=index)
    return {
        "timestamp": datetime.combine(day, time(15)),
        "open": close, "high": close * 1.01, "low": close * 0.99, "close": close,
        "atr14": 1.0, "highCount": 0, "lowCount": 0, "support_resistance": None, "volumeZScore": None,
        **extra,
    }


def _rows(n=40, close=20.0):
    return [_bar(i, close) for i in range(n)]


NO_MACD = {"dif": [None] * 40, "dea": [None] * 40}


def _keys(entry):
    return [trigger["key"] for trigger in entry["triggers"]]


# --- 入场触发 ---------------------------------------------------------------

def test_nine_turn_reversal_fires_only_on_the_first_high_two_after_a_recent_low_nine():
    rows = _rows()
    rows[35]["lowCount"] = 9
    rows[36]["lowCount"] = 10
    rows[38]["highCount"] = 1
    rows[39]["highCount"] = 2
    assert _keys(technical.evaluate_entry(rows, NO_MACD, SIGNALS)) == ["nine_turn_reversal"]

    earlier_high_two = copy.deepcopy(rows)
    earlier_high_two[37]["highCount"] = 2
    assert technical.evaluate_entry(earlier_high_two, NO_MACD, SIGNALS)["triggers"] == []

    stale_low = _rows()
    stale_low[20]["lowCount"] = 9
    stale_low[39]["highCount"] = 2
    assert technical.evaluate_entry(stale_low, NO_MACD, SIGNALS)["triggers"] == []


def test_support_bounce_needs_a_bullish_candle_near_the_nearest_support():
    rows = _rows()
    rows[38]["close"] = 20.5
    rows[39].update(low=20.3, open=20.2, close=21.0, support_resistance={
        "supports": [{"price": 18.0, "roles": ["strongest"]}, {"price": 20.0, "roles": ["nearest"]}],
        "resistances": [],
    })
    assert _keys(technical.evaluate_entry(rows, NO_MACD, SIGNALS)) == ["support_bounce"]

    bearish = copy.deepcopy(rows)
    bearish[39].update(open=21.5)
    assert technical.evaluate_entry(bearish, NO_MACD, SIGNALS)["triggers"] == []

    far = copy.deepcopy(rows)
    far[39].update(low=21.5, open=21.4, close=22.0)
    assert technical.evaluate_entry(far, NO_MACD, SIGNALS)["triggers"] == []


def test_macd_golden_cross_and_below_zero_option():
    macd = {"dif": [None] * 38 + [-0.2, 0.1], "dea": [None] * 38 + [0.0, 0.0]}
    assert _keys(technical.evaluate_entry(_rows(), macd, SIGNALS)) == ["macd_golden_cross"]
    below_zero = copy.deepcopy(SIGNALS)
    below_zero["macd_golden_cross"]["below_zero_only"] = True
    assert technical.evaluate_entry(_rows(), macd, below_zero)["triggers"] == []


def test_breakout_is_off_by_default():
    rows = _rows()
    rows[38]["support_resistance"] = {"supports": [], "resistances": [{"price": 20.5, "roles": ["nearest"]}]}
    rows[39].update(close=21.0, volumeZScore=1.5)
    assert technical.evaluate_entry(rows, NO_MACD, SIGNALS)["triggers"] == []
    enabled = copy.deepcopy(SIGNALS)
    enabled["breakout"]["enabled"] = True
    assert _keys(technical.evaluate_entry(rows, NO_MACD, enabled)) == ["breakout"]


def test_stop_and_risk_based_weight():
    entry = technical.evaluate_entry(_rows(), NO_MACD, SIGNALS)
    # 止损 = 2 × ATR(1.0) / 收盘 20 = 10%
    assert entry["stop_pct"] == 10.0 and entry["stop_price"] == 18.0
    assert technical.planned_weight_pct(5.0, 10.0, 1.0) == 5.0
    assert technical.planned_weight_pct(8.0, 10.0, 0.5) == 5.0
    assert technical.evaluate_entry(_rows(10), NO_MACD, SIGNALS)["note"]


# --- 雪球过滤 ---------------------------------------------------------------

@pytest.mark.parametrize(
    "item, overrides, status, passed",
    [
        ({"holding_cube_count": 10, "weight_price_ratio": 1.3}, {}, "pass", True),
        ({"holding_cube_count": 10, "weight_price_ratio": 1.1}, {}, "fail", False),
        ({"holding_cube_count": 5, "weight_price_ratio": 2.0}, {}, "fail", False),
        ({"holding_cube_count": 10, "weight_price_ratio": None}, {}, "missing", False),
        (None, {}, "missing", False),
        (None, {"block_missing": False}, "missing", True),
        ({"holding_cube_count": 1, "weight_price_ratio": 0.5}, {"enabled": False}, "disabled", True),
    ],
)
def test_xueqiu_filter(item, overrides, status, passed):
    config = {**SIGNALS["xueqiu_ratio"], **overrides}
    result = technical.evaluate_xueqiu_filter(item, config, {"available": True})
    assert (result["status"], result["passed"]) == (status, passed)


def test_xueqiu_filter_is_skipped_when_data_does_not_cover_the_day():
    result = technical.evaluate_xueqiu_filter(None, SIGNALS["xueqiu_ratio"], {"available": False, "reason": "快照不足"})
    assert result["status"] == "unavailable" and result["passed"] is True


# --- 出场 -------------------------------------------------------------------

def _exit(rows, **overrides):
    kwargs = dict(entry_date=START + timedelta(days=30), return_pct=0.0, stop_pct=10.0, defending=False,
                  in_universe=True, gate_passed=True, pool_rank=10, signals=SIGNALS)
    kwargs.update(overrides)
    return [item["key"] for item in technical.evaluate_exit(rows, **kwargs)]


def test_trailing_stop_anchors_on_the_first_red_dot_after_entry():
    rows = _rows()
    rows[32].update(highCount=3, close=25.0)
    rows[39].update(close=22.5, lowCount=2)
    assert _exit(rows) == ["trailing_stop"]          # (25 − 22.5) / ATR 1 = 2.5 ≥ 2

    before_entry = _rows()
    before_entry[25].update(highCount=3, close=25.0)
    before_entry[39].update(close=22.5, lowCount=2)
    assert _exit(before_entry) == []                  # 买入前的高点不算

    shallow = copy.deepcopy(rows)
    shallow[39].update(close=23.4)                    # 回撤 1.6 ATR
    assert _exit(shallow) == []
    assert _exit(shallow, defending=True) == ["trailing_stop"]   # 防守收紧到 1.5


def test_stop_loss_and_fundamental_exits():
    rows = _rows()
    assert _exit(rows, return_pct=-11.0) == ["stop_loss"]
    assert _exit(rows, gate_passed=False, pool_rank=None) == ["gate_fail"]
    no_gate_exit = copy.deepcopy(SIGNALS)
    no_gate_exit["exit_on_gate_fail"] = False
    assert _exit(rows, gate_passed=False, pool_rank=None, signals=no_gate_exit) == []
    assert _exit(rows, pool_rank=250) == ["pool_exit"]
    assert _exit(rows, in_universe=False, gate_passed=None, pool_rank=None) == ["universe_exit"]


# --- 模拟盘撮合 ---------------------------------------------------------------

D0, D1, D2, D3 = date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10)
PAPER = DEFAULTS["paper"]


@pytest.fixture
def market_db(tmp_path):
    path = tmp_path / "market.duckdb"
    connection = duckdb.connect(str(path))
    connection.execute(
        "CREATE TABLE a_stock_market_daily (ts_code VARCHAR, trade_date DATE, open DOUBLE, close DOUBLE, pre_close DOUBLE)"
    )
    connection.execute("CREATE TABLE a_stock_adj_factor (ts_code VARCHAR, trade_date DATE, adj_factor DOUBLE)")
    rows = [
        # 甲：D1 开盘买入；D3 10 送 10，原始价格减半、复权因子翻倍
        ("600001.SH", D1, 10.0, 10.5, 9.9, 1.0),
        ("600001.SH", D2, 10.6, 11.0, 10.5, 1.0),
        ("600001.SH", D3, 5.5, 5.6, 5.5, 2.0),
        # 乙：D1 开盘涨停，买不进
        ("600002.SH", D1, 11.0, 11.0, 10.0, 1.0),
        # 丙：D1 停牌、D2 开盘跌停，都顺延，D3 才卖出
        ("600003.SH", D2, 18.0, 18.5, 20.0, 1.0),
        ("600003.SH", D3, 18.8, 19.0, 18.5, 1.0),
    ]
    connection.executemany("INSERT INTO a_stock_market_daily VALUES (?, ?, ?, ?, ?)", [row[:5] for row in rows])
    connection.executemany("INSERT INTO a_stock_adj_factor VALUES (?, ?, ?)", [(r[0], r[1], r[5]) for r in rows])
    connection.close()
    return path


def _book():
    return {
        "account": {"id": None, "initial_capital": 1e6, "cash": 1e6, "started_on": D0, "last_trade_date": D0},
        "positions": {
            "600003.SH": {
                "ts_code": "600003.SH", "name": "丙", "quantity": 1000, "entry_date": date(2026, 8, 1),
                "entry_price": 20.0, "entry_adj_factor": 1.0, "cost": 20006.0, "stop_pct": 10.0,
                "last_price": 20.0, "last_adj_factor": 1.0, "market_value": 20000.0,
            },
        },
        "pending": [
            {"id": None, "signal_date": D0, "ts_code": "600001.SH", "name": "甲", "side": "buy", "status": "pending", "budget": 100000.0, "stop_pct": 8.0},
            {"id": None, "signal_date": D0, "ts_code": "600002.SH", "name": "乙", "side": "buy", "status": "pending", "budget": 50000.0},
            {"id": None, "signal_date": D0, "ts_code": "600003.SH", "name": "丙", "side": "sell", "status": "pending", "quantity": 1000},
        ],
    }


def test_settle_fills_next_open_and_handles_limits_suspension_and_splits(market_db):
    book = _book()
    result = paper.settle(book, D3, PAPER, connect=lambda: duckdb.connect(str(market_db), read_only=True))
    orders = {order["ts_code"]: order for order in book["pending"]}

    assert result["days"] == [D1.isoformat(), D2.isoformat(), D3.isoformat()]
    buy = orders["600001.SH"]
    assert buy["status"] == "filled" and buy["exec_date"] == D1 and buy["fill_price"] == 10.0
    assert buy["quantity"] == 9900                          # 10 万 / (10 × 1.0003) 向下取整手
    assert orders["600002.SH"]["status"] == "cancelled" and "涨停" in orders["600002.SH"]["message"]

    sell = orders["600003.SH"]
    assert sell["status"] == "filled" and sell["exec_date"] == D3 and sell["fill_price"] == 18.8
    assert sell["realized_pnl"] == pytest.approx(18800 * (1 - 0.0008) - 20006.0)
    assert "600003.SH" not in book["positions"]

    held = book["positions"]["600001.SH"]
    # 除权后原始价 5.6，复权因子 2 → 市值没有假跳水
    assert held["market_value"] == pytest.approx(9900 * 5.6 * 2.0)
    assert paper.position_return_pct(held) == pytest.approx((5.6 * 2.0 / 10.0 - 1) * 100)
    assert len(result["navs"]) == 3
    assert result["navs"][-1]["nav"] == pytest.approx(book["account"]["cash"] + held["market_value"])
    assert book["account"]["last_trade_date"] == D3


def test_settle_keeps_a_suspended_sell_pending(market_db):
    book = _book()
    paper.settle(book, D1, PAPER, connect=lambda: duckdb.connect(str(market_db), read_only=True))
    sell = next(order for order in book["pending"] if order["side"] == "sell")
    assert sell["status"] == "pending" and "停牌" in sell["message"]
    assert "600003.SH" in book["positions"]


def test_price_limit_by_board():
    assert paper.price_limit_pct("600001.SH") == 0.10
    assert paper.price_limit_pct("300750.SZ") == 0.20
    assert paper.price_limit_pct("688012.SH") == 0.20
    assert paper.price_limit_pct("920174.BJ") == 0.30


# --- 端到端：两天 -------------------------------------------------------------

def _nine_turn_klines(end_day):
    """前段横盘、12 天连跌出低九，最后两天反弹：最后一根是低九后的第一次高二。"""
    closes = [30.0 if i % 2 == 0 else 30.2 for i in range(40)]
    closes += [29.5 - 0.5 * i for i in range(12)]
    closes += [30.0, 30.5]
    start = end_day - timedelta(days=len(closes) - 1)
    return [
        {"timestamp": datetime.combine(start + timedelta(days=i), time(15)), "open": c - 0.1, "high": c + 0.3,
         "low": c - 0.3, "close": c, "volume": 1e6, "turnover": 3e7, "turnover_rate": 1.0}
        for i, c in enumerate(closes)
    ]


def _flat_klines(end_day):
    return [
        # 收盘价完全不变：九转计数、MACD 都不会触发
        {"timestamp": datetime.combine(end_day - timedelta(days=59 - i), time(15)), "open": 10.0, "high": 10.2,
         "low": 9.8, "close": 10.0, "volume": 1e6, "turnover": 1e7, "turnover_rate": 1.0}
        for i in range(60)
    ]


ALLOCATION_ROWS = [
    {"ts_code": "600001.SH", "name": "甲", "status": "target", "pool_rank": 1, "sector_code": "H30184.CSI",
     "sector_name": "半导体", "sector_state": "offense", "target_weight_pct": 5.0},
    {"ts_code": "600002.SH", "name": "乙", "status": "target", "pool_rank": 2, "sector_code": "H30184.CSI",
     "sector_name": "半导体", "sector_state": "offense", "target_weight_pct": 5.0},
]


def _allocation_loader(day):
    def load(as_of=None):
        return {
            "run": {"trade_date": day.isoformat(),
                    "summary": {"exposure_pct": 100.0, "market": {"state": "offense", "overheated": False}}},
            "regimes": [{"index_code": "H30184.CSI", "cap_pct": 30.0, "state": "offense", "overheated": False}],
            "rows": ALLOCATION_ROWS,
        }
    return load


def _pool_loader(trade_date, view="all"):
    return {"rows": [{"ts_code": "600001.SH", "gate_passed": True, "pool_rank": 1},
                     {"ts_code": "600002.SH", "gate_passed": True, "pool_rank": 2}]}


def _kline_loader(symbols, trade_date):
    klines = {"600001.SH": _nine_turn_klines(D1), "600002.SH": _flat_klines(trade_date)}
    if trade_date > D1:
        last = klines["600001.SH"][-1]
        klines["600001.SH"] = klines["600001.SH"] + [
            {**last, "timestamp": datetime.combine(trade_date, time(15)), "close": 31.0, "open": 30.6}
        ]
    return {symbol: klines[symbol] for symbol in symbols}


def _xueqiu_loader(symbols, trade_date, lookback):
    assert lookback == 5
    return {"available": True, "snapshot_date": trade_date.isoformat(),
            "items": {"600001.SH": {"holding_cube_count": 12, "weight_price_ratio": 1.4},
                      "600002.SH": {"holding_cube_count": 3, "weight_price_ratio": 2.0}}}


def test_two_day_flow_signals_then_fills(market_db):
    paper.reset_paper(1e6)
    connect = lambda: duckdb.connect(str(market_db), read_only=True)  # noqa: E731
    common = dict(pool_loader=_pool_loader, kline_loader=_kline_loader, xueqiu_loader=_xueqiu_loader, connect=connect)

    day1 = trading.run_trading_day(config=DEFAULTS, allocation_loader=_allocation_loader(D1), **common)
    assert day1["status"] == "completed"
    assert day1["summary"]["buy_orders"] == 1 and day1["summary"]["paper_note"]
    signals = {row["ts_code"]: row for row in trading.load_signals(
        connect=lambda: connect_duckdb(ANALYTICS_DB_PATH, prefer_read_only=True))["rows"]}
    assert signals["600001.SH"]["action"] == "buy"
    # 反弹当天 MACD 也可能同时金叉，这里只要求九转触发在列
    assert "nine_turn_reversal" in signals["600001.SH"]["trigger_keys"]
    assert signals["600001.SH"]["xueqiu_status"] == "pass"
    assert signals["600002.SH"]["action"] == "watch"

    overview = paper.load_paper_overview()
    pending = [order for order in overview["orders"] if order["status"] == "pending"]
    assert [order["ts_code"] for order in pending] == ["600001.SH"]
    assert pending[0]["target_weight_pct"] <= 5.0

    # 同一天重跑：当天的订单被覆盖，不会重复
    trading.run_trading_day(config=DEFAULTS, allocation_loader=_allocation_loader(D1), **common)
    assert sum(1 for order in paper.load_paper_overview()["orders"] if order["status"] == "pending") == 1

    day2 = trading.run_trading_day(config=DEFAULTS, allocation_loader=_allocation_loader(D2), **common)
    assert day2["summary"]["fills"] == 1 and day2["summary"]["holdings"] == 1
    overview = paper.load_paper_overview()
    position = overview["positions"][0]
    assert position["ts_code"] == "600001.SH" and position["entry_price"] == 10.6
    assert position["quantity"] > 0 and position["stop_pct"] is not None
    assert [nav["trade_date"] for nav in overview["navs"]] == [D1.isoformat(), D2.isoformat()]
    assert overview["account"]["last_trade_date"] == D2.isoformat()

    # 更早的日期只算信号、不动模拟盘
    earlier = trading.run_trading_day(config=DEFAULTS, allocation_loader=_allocation_loader(D1), **common)
    assert "只算信号" in earlier["summary"]["paper_note"]
    assert paper.load_paper_overview()["account"]["last_trade_date"] == D2.isoformat()


def test_signal_and_paper_config_normalization():
    config = system_config.normalize_stock_system_config({
        "signals": {"xueqiu_ratio": {"enabled": "false", "min_holding_cubes": -3, "min_ratio": "1.5"},
                    "initial_stop_atr": 0, "bogus": 1},
        "paper": {"initial_capital": 1},
    })
    assert config["signals"]["xueqiu_ratio"]["enabled"] is False
    assert config["signals"]["xueqiu_ratio"]["min_holding_cubes"] == 0
    assert config["signals"]["xueqiu_ratio"]["min_ratio"] == 1.5
    assert config["signals"]["xueqiu_ratio"]["lookback_days"] == 5
    assert config["signals"]["initial_stop_atr"] == 0.5
    assert "bogus" not in config["signals"]
    assert config["paper"]["initial_capital"] == 10000.0
    assert [item["key"] for item in system_config.config_definitions()["triggers"]] == [
        "nine_turn_reversal", "support_bounce", "macd_golden_cross", "breakout",
    ]


# --- 高价股：计划仓位买不满一手 -------------------------------------------------

@pytest.fixture
def expensive_db(tmp_path):
    """中际旭创 2026-09-14 的真实价格：开盘 890，一手就要 8.9 万。"""
    path = tmp_path / "expensive.duckdb"
    connection = duckdb.connect(str(path))
    connection.execute(
        "CREATE TABLE a_stock_market_daily (ts_code VARCHAR, trade_date DATE, open DOUBLE, close DOUBLE, pre_close DOUBLE)"
    )
    connection.execute("CREATE TABLE a_stock_adj_factor (ts_code VARCHAR, trade_date DATE, adj_factor DOUBLE)")
    connection.execute("INSERT INTO a_stock_market_daily VALUES ('300308.SZ', DATE '2026-09-14', 890.0, 873.0, 926.0)")
    connection.execute("INSERT INTO a_stock_adj_factor VALUES ('300308.SZ', DATE '2026-09-14', 1.0)")
    connection.close()
    return path


EXPENSIVE_SIGNAL_DAY, EXPENSIVE_FILL_DAY = date(2026, 9, 11), date(2026, 9, 14)


def _expensive_book(max_budget):
    order = {"id": None, "signal_date": EXPENSIVE_SIGNAL_DAY, "ts_code": "300308.SZ", "name": "中际旭创",
             "side": "buy", "status": "pending", "budget": 50000.0}
    if max_budget is not None:
        order["max_budget"] = max_budget
    return {
        "account": {"id": None, "initial_capital": 1e6, "cash": 1e6, "started_on": None,
                    "last_trade_date": EXPENSIVE_SIGNAL_DAY},
        "positions": {},
        "pending": [order],
    }


def test_one_lot_is_bought_when_it_fits_the_single_position_cap(expensive_db):
    # 计划仓位 5 万买不满一手，但一手 8.9 万在 10 万的单只上限内
    book = _expensive_book(100000.0)
    paper.settle(book, EXPENSIVE_FILL_DAY, PAPER, connect=lambda: duckdb.connect(str(expensive_db), read_only=True))
    position = book["positions"]["300308.SZ"]
    assert position["quantity"] == 100 and position["entry_price"] == 890.0


def test_lot_over_the_cap_is_cancelled_with_an_explicit_reason(expensive_db):
    # 单只上限 8 万 < 一手 8.9 万：撤单，理由要说清是超上限而不是没钱
    book = _expensive_book(80000.0)
    paper.settle(book, EXPENSIVE_FILL_DAY, PAPER, connect=lambda: duckdb.connect(str(expensive_db), read_only=True))
    order = book["pending"][0]
    assert order["status"] == "cancelled" and not book["positions"]
    assert "一手 89,027" in order["message"] and "单只仓位上限 80,000" in order["message"]
    assert "计划仓位 50,000" in order["message"]


def test_lot_over_the_available_cash_says_so(expensive_db):
    # 单只上限够（10 万），但账户只剩 5 万现金：理由要指向现金
    book = _expensive_book(100000.0)
    book["account"]["cash"] = 50000.0
    paper.settle(book, EXPENSIVE_FILL_DAY, PAPER, connect=lambda: duckdb.connect(str(expensive_db), read_only=True))
    assert "超出可用现金 50,000" in book["pending"][0]["message"]


def test_plan_orders_passes_the_single_position_cap_as_the_lot_allowance():
    book = {"account": {"cash": 1e6}, "positions": {}, "pending": []}
    allocation = {
        "run": {"summary": {"exposure_pct": 100.0}},
        "regimes": [{"index_code": "930851.CSI", "cap_pct": 30.0}],
        "rows": [],
    }
    rows = [{
        "role": "candidate", "action": "buy", "ts_code": "300308.SZ", "name": "中际旭创", "pool_rank": 1,
        "sector_code": "930851.CSI", "sector_name": "云计算", "planned_weight_pct": 5.0, "stop_pct": 8.85,
        "triggers": [{"label": "回踩支撑企稳", "detail": "测试"}], "xueqiu_detail": None, "exits": [],
    }]
    orders = trading.plan_orders(D1, rows, book, allocation, DEFAULTS)
    assert orders[0]["budget"] == pytest.approx(50000.0)      # 计划仓位 5%
    assert orders[0]["max_budget"] == pytest.approx(80000.0)  # 单只上限 8%

"""选股系统回测：按日状态查询与逐日判定一致、单根支撑压力与滚动计算一致、减仓/加仓撮合、
目标权重再平衡、统计指标、第一层分组检验，以及一次完整的小样本回放。"""

import json
import random
from datetime import date, datetime, time, timedelta
from pathlib import Path

import duckdb
import pytest

from src.core.services.stock_system import backtest, backtest_runner, backtest_workspace, indicators, paper, sentiment
from src.core.services.stock_system import config as system_config

DEFAULTS = system_config.default_stock_system_config()
TIMING = DEFAULTS["timing"]


# --- 与实盘同一判定 ---------------------------------------------------------

def _history(days=120, seed=7):
    rng = random.Random(seed)
    kinds = ["ma5_bottom", "ma5_top", "volume_bottom", "volume_top"]
    rows, day = [], date(2026, 1, 1)
    for index in range(days):
        day += timedelta(days=1 if index % 7 else 3)
        marks = [{"kind": rng.choice(kinds), "label": "x"}] if rng.random() < 0.08 else []
        if rng.random() < 0.02:
            marks.append({"kind": rng.choice(kinds), "label": "y"})
        rows.append({"date": day.isoformat(), "score": None if index % 37 == 5 else rng.uniform(5, 95), "signals": marks})
    return rows


@pytest.mark.parametrize("timing", [TIMING, {**TIMING, "signal_expiry_days": 10}, {**TIMING, "overheat_score": 60.0}])
def test_regime_at_matches_classify_regime(timing):
    rows = _history()
    series = sentiment.regime_series(rows)
    probe_days = [date.fromisoformat(row["date"]) for row in rows[::3]]
    probe_days += [date(2025, 12, 1), date.fromisoformat(rows[-1]["date"]) + timedelta(days=20)]
    for day in probe_days:
        assert sentiment.regime_at(series, day, timing) == sentiment.classify_regime(rows, day, timing), day


def test_single_bar_support_resistance_matches_the_chart_pipeline():
    fixture = json.loads((Path(__file__).parent / "fixtures" / "stock_indicator_parity.json").read_text(encoding="utf-8"))
    klines = fixture["klines"]
    chart = indicators.compute_chart_indicators(klines)["klines"]
    for index in (10, 124, 125, 170, len(klines) - 1):
        assert indicators.chart_support_resistance_at(klines, index) == chart[index]["support_resistance"]


def test_rebalance_dates():
    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(40) if (date(2026, 1, 5) + timedelta(days=i)).weekday() < 5]
    assert backtest.rebalance_dates(days, "monthly") == [date(2026, 1, 5), date(2026, 2, 2)]
    weekly = backtest.rebalance_dates(days, "weekly")
    assert weekly[:3] == [date(2026, 1, 5), date(2026, 1, 12), date(2026, 1, 19)]


# --- 减仓 / 加仓撮合 ------------------------------------------------------------

@pytest.fixture
def bars_db(tmp_path):
    path = tmp_path / "bars.duckdb"
    connection = duckdb.connect(str(path))
    connection.execute("CREATE TABLE a_stock_market_daily (ts_code VARCHAR, trade_date DATE, open DOUBLE, close DOUBLE, pre_close DOUBLE)")
    connection.execute("CREATE TABLE a_stock_adj_factor (ts_code VARCHAR, trade_date DATE, adj_factor DOUBLE)")
    connection.executemany("INSERT INTO a_stock_market_daily VALUES (?, ?, ?, ?, ?)", [
        ("600001.SH", date(2026, 3, 2), 11.0, 11.0, 10.0),
        ("600001.SH", date(2026, 3, 3), 5.5, 5.6, 5.5),   # 10 送 10
    ])
    connection.executemany("INSERT INTO a_stock_adj_factor VALUES (?, ?, ?)", [
        ("600001.SH", date(2026, 3, 2), 1.0), ("600001.SH", date(2026, 3, 3), 2.0),
    ])
    connection.close()
    return path


def _position():
    return {"ts_code": "600001.SH", "name": "甲", "quantity": 1000, "entry_date": date(2026, 2, 1),
            "entry_price": 10.0, "entry_adj_factor": 1.0, "cost": 10003.0, "last_price": 10.0,
            "last_adj_factor": 1.0, "market_value": 10000.0}


def test_partial_sell_then_add_after_split(bars_db):
    book = {
        "account": {"id": None, "initial_capital": 1e5, "cash": 90000.0, "started_on": None, "last_trade_date": date(2026, 2, 27)},
        "positions": {"600001.SH": _position()},
        "pending": [
            {"signal_date": date(2026, 2, 27), "ts_code": "600001.SH", "side": "sell", "status": "pending", "fraction": 0.35},
        ],
    }
    connect = lambda: duckdb.connect(str(bars_db), read_only=True)  # noqa: E731
    result = paper.settle(book, date(2026, 3, 2), DEFAULTS["paper"], connect=connect)
    sell = result["events"][0]
    assert sell["quantity"] == 300 and sell["cost"] == pytest.approx(10003.0 * 0.3)
    assert book["positions"]["600001.SH"]["quantity"] == 700

    book["pending"] = [{"signal_date": date(2026, 3, 2), "ts_code": "600001.SH", "side": "buy", "status": "pending",
                        "budget": 5600.0, "allow_add": True}]
    paper.settle(book, date(2026, 3, 3), DEFAULTS["paper"], connect=connect)
    position = book["positions"]["600001.SH"]
    # 送转后原 700 股折成 1400 股；5600 元 / (5.5 × 1.0003) 取整手再加 1000 股
    assert position["quantity"] == pytest.approx(1400 + 1000)
    assert position["entry_adj_factor"] == 2.0
    assert position["market_value"] == pytest.approx(2400 * 5.6)
    # 加权成本价：1400 股 × 5.0（折算后）+ 1000 股 × 5.5
    assert position["entry_price"] == pytest.approx((1400 * 5.0 + 1000 * 5.5) / 2400)

    no_add = {**book, "pending": [{"signal_date": date(2026, 3, 2), "ts_code": "600001.SH", "side": "buy",
                                    "status": "pending", "budget": 5000.0}]}
    no_add["account"] = {**book["account"], "last_trade_date": date(2026, 3, 2)}
    events = paper.settle(no_add, date(2026, 3, 3), DEFAULTS["paper"], connect=connect)["events"]
    assert events[0]["status"] == "cancelled"   # 实盘路径不加仓


def test_plan_target_orders():
    book = {"account": {"cash": 50000.0}, "positions": {
        "A": {"name": "A", "market_value": 30000.0},   # 30%，目标 20%：减仓
        "B": {"name": "B", "market_value": 10000.0},   # 不在目标：卖
        "C": {"name": "C", "market_value": 10000.0},   # 不在目标但要保留
    }, "pending": []}
    orders = backtest.plan_target_orders(date(2026, 1, 5), book, {"A": {"weight": 20.0}, "D": {"weight": 10.0}}, ["C"], 2.0)
    by_code = {order["ts_code"]: order for order in orders}
    assert by_code["A"]["side"] == "sell" and by_code["A"]["fraction"] == pytest.approx(1 / 3)
    assert by_code["B"]["side"] == "sell" and "fraction" not in by_code["B"]
    assert by_code["D"]["side"] == "buy" and by_code["D"]["budget"] == pytest.approx(10000.0) and by_code["D"]["allow_add"]
    assert "C" not in by_code


# --- 统计 ---------------------------------------------------------------------

def test_metrics():
    navs = [{"trade_date": date(2025, 12, 30), "nav": 110.0, "exposure_pct": 100.0},
            {"trade_date": date(2025, 12, 31), "nav": 99.0, "exposure_pct": 50.0},
            {"trade_date": date(2026, 1, 2), "nav": 118.8, "exposure_pct": 50.0}]
    metrics = backtest.nav_metrics(navs, 100.0)
    assert metrics["total_return_pct"] == 18.8
    assert metrics["max_drawdown_pct"] == -10.0
    assert metrics["avg_exposure_pct"] == pytest.approx(66.7)
    assert backtest.yearly_returns(navs, 100.0) == {"2025": -1.0, "2026": 20.0}
    trades = backtest.trade_metrics([
        {"return_pct": 10.0, "pnl": 100.0, "holding_days": 10},
        {"return_pct": -5.0, "pnl": -50.0, "holding_days": 20},
    ])
    assert trades["win_rate_pct"] == 50.0 and trades["profit_factor"] == 2.0 and trades["avg_holding_days"] == 15.0


def test_pool_score_analysis_detects_monotone_scores():
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(30)]
    rows = [{"ts_code": f"S{i:03d}", "gate_passed": True, "composite_score": float(i), "in_pool": i >= 40}
            for i in range(50)]
    closes = {days[0]: {row["ts_code"]: 10.0 for row in rows},
              days[20]: {row["ts_code"]: 10.0 * (1 + 0.002 * i) for i, row in enumerate(rows)}}
    result = backtest.pool_score_analysis({days[0]: rows}, days, closes, horizons=(20,))["20"]
    assert result["dates"] == 1
    assert result["quintile_mean_pct"] == sorted(result["quintile_mean_pct"])
    assert result["ic_mean"] == pytest.approx(1.0)
    assert result["spread_mean_pct"] > 0 and result["pool_excess_mean_pct"] > 0


# --- 完整小样本回放 -----------------------------------------------------------

SYMBOLS = ["600001.SH", "600002.SH", "600003.SH"]


def _weekdays(start, count):
    days, day = [], start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


@pytest.fixture
def market(tmp_path):
    rng = random.Random(3)
    days = _weekdays(date(2025, 11, 3), 110)
    raw = {}
    for index, symbol in enumerate(SYMBOLS):
        close, rows = 10.0 + index, []
        for day in days:
            pre = close
            close = max(1.0, pre * (1 + rng.uniform(-0.03, 0.035)))
            rows.append((symbol, day, pre * (1 + rng.uniform(-0.01, 0.01)), close, pre))
        raw[symbol] = rows
    path = tmp_path / "market.duckdb"
    connection = duckdb.connect(str(path))
    connection.execute("CREATE TABLE a_stock_market_daily (ts_code VARCHAR, trade_date DATE, open DOUBLE, close DOUBLE, pre_close DOUBLE)")
    connection.execute("CREATE TABLE a_stock_adj_factor (ts_code VARCHAR, trade_date DATE, adj_factor DOUBLE)")
    connection.execute("CREATE TABLE a_stock_index_daily (ts_code VARCHAR, trade_date DATE, close DOUBLE)")
    all_rows = [row for rows in raw.values() for row in rows]
    connection.executemany("INSERT INTO a_stock_market_daily VALUES (?, ?, ?, ?, ?)", all_rows)
    connection.executemany("INSERT INTO a_stock_adj_factor VALUES (?, ?, 1.0)", [(r[0], r[1]) for r in all_rows])
    connection.execute("CREATE TABLE a_stock_market_daily_qfq AS SELECT ts_code, trade_date, close FROM a_stock_market_daily")
    connection.executemany("INSERT INTO a_stock_index_daily VALUES ('000985.SH', ?, ?)",
                           [(day, 5000 + i) for i, day in enumerate(days)])
    connection.close()
    return {"path": path, "days": days, "raw": raw}


def _deps(market, computed):
    cache = {}
    raw = market["raw"]

    def compute_pool(day, config):
        computed.append(day)
        return {"status": "completed", "summary": {"universe_size": 3}, "rows": [
            {"ts_code": symbol, "name": symbol, "industry": "测试", "gate_passed": True,
             "pool_rank": rank, "composite_score": 90.0 - rank, "in_pool": True}
            for rank, symbol in enumerate(SYMBOLS, start=1)
        ]}

    def history_loader(symbol, end):
        if symbol != "000985.SH":
            return []
        return [{"date": day.isoformat(), "score": 50.0,
                 "signals": [{"kind": "ma5_bottom", "label": "均线底"}] if i == 0 else []}
                for i, day in enumerate(market["days"])]

    def kline_loader(symbols, start, end):
        return {symbol: [
            {"timestamp": datetime.combine(day, time(15)), "open": o, "high": max(o, c) * 1.01, "low": min(o, c) * 0.99,
             "close": c, "volume": 1e6, "turnover": 1e7, "turnover_rate": 1.0}
            for _, day, o, c, _pre in raw[symbol] if start <= day <= end
        ] for symbol in symbols}

    return backtest.BacktestDeps(
        connect=lambda: duckdb.connect(str(market["path"]), read_only=True),
        compute_pool=compute_pool,
        pool_cache_get=lambda key, day: cache.get((key, day)),
        pool_cache_put=lambda key, day, payload: cache.__setitem__((key, day), payload),
        history_loader=history_loader,
        membership_loader=lambda symbols, day: {},
        kline_loader=kline_loader,
        xueqiu_loader=lambda symbols, day, lookback: {"available": False, "reason": "测试", "items": {}},
    )


def test_run_backtest_replays_every_variant(market):
    computed = []
    deps = _deps(market, computed)
    start, end = market["days"][60], market["days"][-1]
    progress = []
    result = backtest.run_backtest(start=start, end=end, config=DEFAULTS, deps=deps,
                                   progress=lambda pct, message: progress.append(pct))
    summary = result["summary"]
    days = [day for day in market["days"] if start <= day <= end]
    assert computed == backtest.rebalance_dates(days, "monthly")
    assert set(summary["variants"]) == set(backtest.VARIANT_KEYS)
    for variant in backtest.VARIANT_KEYS:
        assert len(result["navs"][variant]) == len(days)
        assert summary["variants"][variant]["days"] == len(days)
    # 基准：第一天出单、第二天开盘成交，之后满仓三只
    assert result["navs"]["pool_hold"][1]["positions"] == 3
    assert result["navs"]["pool_hold"][-1]["exposure_pct"] > 90
    assert len(result["navs"]["benchmark"]) == len(days)
    assert "pool_score" in summary and summary["benchmark"]["label"] == "中证全指"
    assert progress and max(progress) >= 96

    # 同样的第一层参数再跑：股票池走缓存
    computed.clear()
    backtest.run_backtest(start=start, end=end, config=DEFAULTS, deps=deps, variants=["pool_hold"])
    assert computed == []


def test_run_backtest_can_be_cancelled(market):
    with pytest.raises(backtest.BacktestCancelled):
        backtest.run_backtest(start=market["days"][60], end=market["days"][-1], config=DEFAULTS,
                              deps=_deps(market, []), cancelled=lambda: True)


# --- 任务管理与工作区 ----------------------------------------------------------

def test_normalize_params():
    params = backtest_runner.normalize_params({"start_date": "2024-01-01", "end_date": "2025-01-01",
                                               "variants": ["full", "bogus"]}, DEFAULTS)
    assert params["variants"] == ["full"] and params["frequency"] == "monthly"
    assert params["initial_capital"] == DEFAULTS["paper"]["initial_capital"]
    with pytest.raises(ValueError):
        backtest_runner.normalize_params({"start_date": "2025-01-01", "end_date": "2024-01-01"}, DEFAULTS)
    with pytest.raises(ValueError):
        backtest_runner.normalize_params({"frequency": "daily"}, DEFAULTS)


def test_results_round_trip_and_stale_run_recovery():
    from src.core.database import SessionLocal, StockSystemBacktestRun

    with SessionLocal() as db:
        run = StockSystemBacktestRun(status="running", progress=50.0, params={"start_date": "2026-01-01"},
                                     config={}, pid=999999, created_at=datetime.now())
        db.add(run)
        db.commit()
        run_id = run.id
    backtest_runner.save_results(run_id, {
        "navs": {"pool_hold": [{"trade_date": date(2026, 1, 5), "nav": 1e6, "exposure_pct": 90.0, "positions": 3}],
                 "benchmark": [{"trade_date": date(2026, 1, 5), "nav": 1.01e6, "exposure_pct": 100.0, "positions": None}]},
        "trades": {"pool_hold": [{"ts_code": "600001.SH", "name": "甲", "entry_date": date(2026, 1, 2),
                                  "exit_date": date(2026, 1, 5), "entry_price": 10.0, "exit_price": 11.0,
                                  "quantity": 100.0, "pnl": 99.0, "return_pct": 9.9, "holding_days": 3, "reason": "测试"}]},
    })
    detail = backtest_runner.load_run_detail(run_id)
    assert detail["navs"]["pool_hold"][0]["nav"] == 1e6 and detail["navs"]["benchmark"]
    assert detail["trades"]["pool_hold"][0]["return_pct"] == 9.9

    # pid 999999 不存在：列表时自动标成失败，之后可以删除
    runs = {item["id"]: item for item in backtest_runner.list_runs()}
    assert runs[run_id]["status"] == "failed"
    backtest_runner.delete_backtest(run_id)
    with pytest.raises(KeyError):
        backtest_runner.get_run(run_id)


def test_build_workspace_copies_only_the_planned_rows(tmp_path, monkeypatch):
    source = tmp_path / "source.duckdb"
    workspace = tmp_path / "workspace.duckdb"
    connection = duckdb.connect(str(source))
    connection.execute("CREATE TABLE a_stock_market_daily (ts_code VARCHAR, trade_date DATE, close DOUBLE, extra DOUBLE)")
    connection.executemany("INSERT INTO a_stock_market_daily VALUES (?, ?, ?, 0)",
                           [("600001.SH", date(2026, 1, d), 10.0 + d) for d in range(1, 11)])
    connection.execute("CREATE TABLE xueqiu_cube_holdings_snapshots (snapshot_date DATE, stock_symbol VARCHAR)")
    connection.execute("INSERT INTO xueqiu_cube_holdings_snapshots VALUES ('2026-01-05', 'SH.600001')")
    connection.close()
    connection = duckdb.connect(str(workspace))
    connection.execute("CREATE TABLE a_stock_market_daily (ts_code VARCHAR, trade_date DATE, close DOUBLE)")
    connection.close()

    monkeypatch.setattr(backtest_workspace, "ANALYTICS_DB_PATH", str(workspace))
    monkeypatch.setattr(backtest_workspace, "table_plan", lambda start, end: [
        ("a_stock_market_daily", "trade_date BETWEEN ? AND ?", [start, end], False),
        ("xueqiu_cube_holdings_snapshots", "TRUE", [], True),
        ("missing_table", "TRUE", [], False),
    ])
    counts = backtest_workspace.build_workspace(str(source), date(2026, 1, 3), date(2026, 1, 6))
    assert counts == {"a_stock_market_daily": 4, "xueqiu_cube_holdings_snapshots": 1}
    connection = duckdb.connect(str(workspace), read_only=True)
    assert connection.execute("SELECT MIN(trade_date), MAX(trade_date) FROM a_stock_market_daily").fetchone() == (
        date(2026, 1, 3), date(2026, 1, 6))
    connection.close()
    with pytest.raises(RuntimeError):
        monkeypatch.setattr(backtest_workspace, "ANALYTICS_DB_PATH", str(source))
        backtest_workspace.build_workspace(str(source), date(2026, 1, 3), date(2026, 1, 6))

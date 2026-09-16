"""板块九转策略：信号定义、布防窗口、配置默认值、模拟盘出单与回测账目。"""

from datetime import date, datetime, time, timedelta

import duckdb
import pytest

from src.core.services.sector_nine_turn import backtest, config as strategy_config, daily, paper, signals

PARAMS = signals.SignalParams.from_config(strategy_config.default_sector_nine_turn_config())
START = date(2026, 1, 5)


def _bars(closes, highs=None, lows=None):
    """按收盘价造一段日线；开盘=收盘，最高/最低给默认波动。"""
    rows = []
    for index, close in enumerate(closes):
        day = START + timedelta(days=index)
        rows.append({
            "timestamp": datetime.combine(day, time(15)),
            "open": close,
            "high": (highs[index] if highs else close * 1.01),
            "low": (lows[index] if lows else close * 0.99),
            "close": close,
        })
    return rows


def _dates(bars):
    return [bar["timestamp"].date() for bar in bars]


def _rows(count, close=20.0, **overrides):
    """直接造带九转字段的行，绕开 K 线，专门测规则本身。"""
    rows = []
    for index in range(count):
        rows.append({
            "timestamp": datetime.combine(START + timedelta(days=index), time(15)),
            "open": close, "high": close, "low": close, "close": close,
            "atr14": 1.0, "highCount": 0, "lowCount": 0,
            "latestRisingClose": None, "risingDrawdownAtr": None,
        })
    for index, values in (overrides or {}).items():
        rows[int(index)].update(values)
    return rows


# --- 买入形态 ---------------------------------------------------------------

def test_turn_signal_fires_on_the_first_high_two_after_a_low_nine_and_only_once():
    rows = _rows(20)
    rows[5]["lowCount"] = 9
    rows[8]["highCount"] = 1
    rows[9]["highCount"] = 2
    rows[10]["highCount"] = 3
    # 同一次低 9 之后再出现的高 2 不再触发
    rows[15]["highCount"] = 2
    assert signals.low_high_turn_indices(rows, PARAMS) == [9]


def test_a_second_low_nine_arms_the_signal_again():
    rows = _rows(20)
    rows[3]["lowCount"] = 9
    rows[5]["highCount"] = 2
    rows[10]["lowCount"] = 11          # ≥9 都算
    rows[14]["highCount"] = 2
    assert signals.low_high_turn_indices(rows, PARAMS) == [5, 14]


def test_high_two_without_a_prior_low_nine_does_not_fire():
    rows = _rows(20)
    rows[6]["lowCount"] = 8            # 没到 9
    rows[9]["highCount"] = 2
    assert signals.low_high_turn_indices(rows, PARAMS) == []


def test_turn_signal_matches_the_chart_indicator_on_real_shaped_bars():
    """用真实的九转口径（收盘 vs 4 根前收盘）跑一遍，确认和 append_nine_turn_atr 对得上。"""
    closes = [100 - index for index in range(16)]        # 连跌，堆出低 9 以上
    closes += [86 + index for index in range(1, 6)]      # 反弹，堆出高 1/2/3
    rows = signals.nine_turn_rows(_bars(closes))
    fired = signals.low_high_turn_indices(rows, PARAMS)
    assert fired, "应该出现低9后首次高2"
    first = fired[0]
    assert rows[first]["highCount"] == 2
    assert any(int(row["lowCount"] or 0) >= 9 for row in rows[:first])


# --- 卖出规则 ---------------------------------------------------------------

def test_sell_needs_high_nine_then_low_two_with_enough_drawdown():
    rows = _rows(20)
    rows[8]["highCount"] = 9
    rows[12]["lowCount"] = 2
    rows[12]["risingDrawdownAtr"] = 2.5
    assert signals.sell_signal_index(rows, 2, PARAMS) == 12


def test_sell_ignores_low_two_before_the_high_nine():
    rows = _rows(20)
    rows[4]["lowCount"] = 2
    rows[4]["risingDrawdownAtr"] = 5.0
    rows[8]["highCount"] = 9
    assert signals.sell_signal_index(rows, 2, PARAMS) is None


def test_sell_waits_for_the_next_low_two_when_the_drawdown_is_too_small():
    rows = _rows(20)
    rows[8]["highCount"] = 9
    rows[10]["lowCount"] = 2
    rows[10]["risingDrawdownAtr"] = 1.2      # 不够 2 个 ATR
    rows[15]["lowCount"] = 2
    rows[15]["risingDrawdownAtr"] = 3.0
    assert signals.sell_signal_index(rows, 2, PARAMS) == 15

    first_only = signals.SignalParams(sell_mode=signals.SELL_MODE_FIRST_ONLY)
    # 严格口径：第一个低 2 回撤不够就作废，之后没有新的高 9 就不再卖
    assert signals.sell_signal_index(rows, 2, first_only) is None


def test_sell_low_ge_two_mode_accepts_deeper_counts():
    rows = _rows(20)
    rows[8]["highCount"] = 9
    rows[12]["lowCount"] = 4
    rows[12]["risingDrawdownAtr"] = 3.0
    assert signals.sell_signal_index(rows, 2, PARAMS) is None
    loose = signals.SignalParams(sell_mode=signals.SELL_MODE_GE)
    assert signals.sell_signal_index(rows, 2, loose) == 12


# --- 布防窗口 ---------------------------------------------------------------

def _triggers(bars, scores):
    rows = signals.nine_turn_rows(bars)
    return rows, _dates(bars), signals.sector_triggers(rows, _dates(bars), PARAMS, scores)


def test_window_zero_arms_only_the_trigger_day():
    bars = _bars([100 - index for index in range(16)] + [86 + index for index in range(1, 6)])
    rows, dates, triggers = _triggers(bars, {day: 30.0 for day in _dates(bars)})
    window = signals.armed_windows(triggers, dates, PARAMS)
    assert len(window) == len(triggers)
    assert set(window) == {trigger["signal_date"] for trigger in triggers}


def test_wider_window_arms_the_following_trading_days():
    bars = _bars([100 - index for index in range(16)] + [86 + index for index in range(1, 6)])
    rows, dates, triggers = _triggers(bars, {day: 30.0 for day in _dates(bars)})
    wide = signals.SignalParams(arm_window_days=3)
    window = signals.armed_windows(triggers, dates, wide)
    first = triggers[0]["signal_date"]
    following = [day for day in dates if day >= first][:4]
    assert set(following).issubset(set(window))


def test_fear_gate_blocks_the_window():
    bars = _bars([100 - index for index in range(16)] + [86 + index for index in range(1, 6)])
    rows, dates, triggers = _triggers(bars, {day: 80.0 for day in _dates(bars)})
    assert all(not trigger["fear_passed"] for trigger in triggers)
    assert signals.armed_windows(triggers, dates, PARAMS) == {}
    # 关掉闸门后同样的触发日就能布防
    assert signals.armed_windows(triggers, dates, PARAMS, require_fear=False)


def test_missing_fear_score_does_not_pass_the_gate():
    bars = _bars([100 - index for index in range(16)] + [86 + index for index in range(1, 6)])
    rows, dates, triggers = _triggers(bars, {})
    assert all(trigger["fear_score"] is None and not trigger["fear_passed"] for trigger in triggers)


# --- 配置 -------------------------------------------------------------------

def test_default_universe_drops_broad_indexes_but_keeps_star_and_dividend():
    codes = set(strategy_config.resolve_index_codes(strategy_config.default_sector_nine_turn_config()))
    for kept in ("000688.SH", "000698.SH", "000699.SH", "000015.SH"):
        assert kept in codes
    for dropped in ("000300.SH", "000905.SH", "000852.SH", "000985.SH", "399006.SZ", "000680.SH"):
        assert dropped not in codes


def test_default_signal_params_match_the_best_backtest_setting():
    params = signals.SignalParams.from_config(strategy_config.default_sector_nine_turn_config())
    assert params.arm_window_days == 0          # 板块与个股同日
    assert params.fear_threshold == 40.0
    assert params.low_count_min == 9 and params.buy_high_count == 2
    assert params.high_count_min == 9 and params.sell_low_count == 2
    assert params.sell_atr_multiple == 2.0


def test_config_normalization_clamps_and_drops_unknown_keys():
    saved = strategy_config.normalize_sector_nine_turn_config({
        "universe": {"index_codes": ["000688.SH", "不存在.XX"]},
        "signal": {"fear_threshold": 500, "arm_window_days": -3, "sell_mode": "乱写"},
        "portfolio": {"max_positions": 0, "pick_order": "乱写"},
        "paper": {"initial_capital": 1, "enabled": False},
        "垃圾": 1,
    })
    assert saved["universe"]["index_codes"] == ["000688.SH"]
    assert saved["signal"]["fear_threshold"] == 100.0
    assert saved["signal"]["arm_window_days"] == 0.0
    assert saved["signal"]["sell_mode"] == "low2_wait"
    assert saved["portfolio"]["max_positions"] == 1.0
    assert saved["portfolio"]["pick_order"] == "fear_asc"
    assert saved["paper"]["initial_capital"] == 10000.0
    assert saved["paper"]["enabled"] is False
    assert "垃圾" not in saved


# --- 模拟盘出单 -------------------------------------------------------------

D0, D1 = date(2026, 9, 14), date(2026, 9, 15)
PAPER = {"enabled": True, "initial_capital": 1000000.0, "commission_pct": 0.03, "stamp_tax_pct": 0.05}


@pytest.fixture
def market_db(tmp_path):
    path = tmp_path / "market.duckdb"
    connection = duckdb.connect(str(path))
    connection.execute(
        "CREATE TABLE a_stock_market_daily (ts_code VARCHAR, trade_date DATE, open DOUBLE, close DOUBLE, pre_close DOUBLE)"
    )
    connection.execute("CREATE TABLE a_stock_adj_factor (ts_code VARCHAR, trade_date DATE, adj_factor DOUBLE)")
    connection.execute("INSERT INTO a_stock_market_daily VALUES ('600001.SH', ?, 10.0, 10.5, 9.9)", [D1])
    connection.execute("INSERT INTO a_stock_adj_factor VALUES ('600001.SH', ?, 1.0)", [D1])
    connection.close()
    return path


def test_settle_uses_the_shared_stock_system_engine(market_db):
    """撮合直接复用选股系统那份 settle，两套策略的成交规则必须一致。"""
    assert paper.settle is __import__(
        "src.core.services.stock_system.paper", fromlist=["settle"]).settle
    book = {
        "account": {"id": None, "initial_capital": 1e6, "cash": 1e6, "started_on": D0, "last_trade_date": D0},
        "positions": {},
        "pending": [{"id": None, "signal_date": D0, "ts_code": "600001.SH", "name": "甲",
                     "side": "buy", "status": "pending", "budget": 100000.0}],
    }
    paper.settle(book, D1, PAPER, connect=lambda: duckdb.connect(str(market_db), read_only=True))
    order = book["pending"][0]
    assert order["status"] == "filled" and order["exec_date"] == D1 and order["fill_price"] == 10.0
    assert book["positions"]["600001.SH"]["quantity"] == 9900


# --- 回测账目 ---------------------------------------------------------------

def test_portfolio_respects_the_position_cap_and_books_net_proceeds():
    trades = [
        {"ts_code": "600001.SH", "entry_date": D0, "entry_price": 10.0, "exit_date": D1,
         "exit_price": 12.0, "return_pct": 20.0, "sector_fear_score": 30.0},
        {"ts_code": "600002.SH", "entry_date": D0, "entry_price": 10.0, "exit_date": D1,
         "exit_price": 8.0, "return_pct": -20.0, "sector_fear_score": 20.0},
    ]
    closes = {"600001.SH": {D0: 10.0, D1: 12.0}, "600002.SH": {D0: 10.0, D1: 8.0}}
    single = backtest._simulate_portfolio(trades, closes, [D0, D1], max_positions=1, pick_order="fear_asc")
    # 贪恐更低的 600002 先进，只有一个仓位 → 600001 被挤掉
    assert single["taken"] == [1]
    both = backtest._simulate_portfolio(trades, closes, [D0, D1], max_positions=2, pick_order="fear_asc")
    assert sorted(both["taken"]) == [0, 1]
    # 两笔各占一半净值、一赚一亏 20%，卖出当天回到 1.0
    assert both["navs"][-1] == pytest.approx(1.0, abs=1e-9)


def test_portfolio_pick_order_can_ignore_the_fear_score():
    trades = [
        {"ts_code": "600001.SH", "entry_date": D0, "entry_price": 10.0, "exit_date": D1,
         "exit_price": 12.0, "return_pct": 20.0, "sector_fear_score": 39.0},
        {"ts_code": "600002.SH", "entry_date": D0, "entry_price": 10.0, "exit_date": D1,
         "exit_price": 8.0, "return_pct": -20.0, "sector_fear_score": 10.0},
    ]
    closes = {"600001.SH": {D0: 10.0, D1: 12.0}, "600002.SH": {D0: 10.0, D1: 8.0}}
    by_code = backtest._simulate_portfolio(trades, closes, [D0, D1], 1, pick_order="code_asc")
    assert by_code["taken"] == [0]


def test_trade_stats_counts_open_positions_separately():
    stats = backtest._trade_stats([
        {"return_pct": 10.0, "holding_days": 5, "closed": True},
        {"return_pct": -5.0, "holding_days": 9, "closed": False},
    ])
    assert stats["trades"] == 2 and stats["closed_trades"] == 1 and stats["open_at_end"] == 1
    assert stats["win_rate_pct"] == pytest.approx(50.0)
    assert stats["median_return_pct"] == pytest.approx(2.5)


# --- 端到端：板块触发 → 成分股买入 → 规则卖出 -------------------------------

def _falling_then_rising(start_price=100.0, fall=16, rise=14):
    """先连跌堆出低9以上、再连涨堆出高2 和高9，最后回落触发卖出。"""
    closes = [start_price - index for index in range(fall)]
    closes += [closes[-1] + index * 1.5 for index in range(1, rise + 1)]
    closes += [closes[-1] - index * 3.0 for index in range(1, 9)]
    return closes


@pytest.fixture
def fake_market(monkeypatch):
    """一个板块 + 一只成分股，走完"低9→高2 买入、高9→低2+回撤 卖出"的完整周期。"""
    closes = _falling_then_rising()
    bars = _bars(closes)
    days = _dates(bars)

    monkeypatch.setattr(backtest.data, "load_index_bars",
                        lambda codes, start, end, **kwargs: {code: bars for code in codes})
    monkeypatch.setattr(backtest.data, "load_fear_scores",
                        lambda codes, as_of: {code: {day: 20.0 for day in days} for code in codes})
    monkeypatch.setattr(backtest.data, "load_index_members",
                        lambda codes, **kwargs: {code: [{"trade_date": days[0], "members": ["600001.SH"]}]
                                                 for code in codes})
    monkeypatch.setattr(backtest.data, "load_stock_bars",
                        lambda symbols, start, end: {"600001.SH": bars})
    monkeypatch.setattr(backtest.data, "load_stock_names", lambda symbols, **kwargs: {"600001.SH": "甲"})
    monkeypatch.setattr(backtest.data, "latest_trade_date", lambda as_of=None, **kwargs: days[-1])
    return days


def test_run_backtest_produces_trades_navs_and_ablations(fake_market, monkeypatch):
    days = fake_market
    config = strategy_config.default_sector_nine_turn_config()
    config["universe"]["index_codes"] = ["931151.CSI"]
    result = backtest.run_backtest(config, days[0], days[-1], random_trials=3)

    assert result["sector_triggers"] >= 1
    assert result["sector_triggers_fear_passed"] == result["sector_triggers"]   # 贪恐 20 全过闸门
    trades = result["trades"]["strategy"]
    assert trades, "完整规则应该至少出一笔交易"
    first = trades[0]
    assert first["ts_code"] == "600001.SH"
    assert first["sector_code"] == "931151.CSI"
    # 板块与个股同日（窗口 0）：板块信号日就是个股信号日，买入落在下一根
    assert first["sector_signal_date"] == first["signal_date"]
    assert first["entry_date"] > first["signal_date"]
    assert first["closed"] and first["sell_drawdown_atr"] > 2.0

    strategy = result["variants"]["strategy"]
    assert len(strategy["navs"]) == len(result["calendar"])
    assert strategy["random_pick_trials"] == 3
    assert set(result["variants"]) == {"strategy", "no_fear", "stock_only"}
    assert result["benchmark"]["navs"]


def test_fear_gate_removes_all_trades_when_the_market_is_greedy(fake_market, monkeypatch):
    days = fake_market
    monkeypatch.setattr(backtest.data, "load_fear_scores",
                        lambda codes, as_of: {code: {day: 90.0 for day in days} for code in codes})
    config = strategy_config.default_sector_nine_turn_config()
    config["universe"]["index_codes"] = ["931151.CSI"]
    result = backtest.run_backtest(config, days[0], days[-1], random_trials=0)

    assert result["sector_triggers_fear_passed"] == 0
    assert result["trades"]["strategy"] == []
    # 去掉闸门 / 不看板块两个消融仍然有信号，说明差别只来自闸门
    assert result["trades"]["no_fear"] and result["trades"]["stock_only"]


def test_position_sell_state_tracks_high_nine_then_low_two():
    rows = _rows(20)
    rows[8]["highCount"] = 9
    rows[14]["lowCount"] = 2
    rows[14]["risingDrawdownAtr"] = 4.0
    dates = [row["timestamp"].date() for row in rows]
    state = daily._position_sell_state(rows, dates, dates[2], PARAMS)
    assert state["high9_armed"] is True
    assert state["high9_date"] == dates[8]
    assert state["sell_date"] == dates[14]

    # 买在高 9 之后：这一轮的高 9 不算数，要等下一次
    later = daily._position_sell_state(rows, dates, dates[10], PARAMS)
    assert later["high9_armed"] is False and later["sell_date"] is None


# --- 每日流程：写快照 + 出模拟盘订单 ----------------------------------------

@pytest.fixture
def fake_daily(monkeypatch):
    """把每日计算要读的四类数据换成假的，只留策略逻辑和落库。"""
    closes = _falling_then_rising()
    bars = _bars(closes)
    days = _dates(bars)
    trigger_day = None
    rows = signals.nine_turn_rows(bars)
    fired = signals.low_high_turn_indices(rows, PARAMS)
    trigger_day = days[fired[0]]

    monkeypatch.setattr(daily.data, "latest_trade_date", lambda as_of=None, **kwargs: as_of or days[-1])
    monkeypatch.setattr(daily.data, "load_index_bars",
                        lambda codes, start, end, **kwargs: {
                            code: [bar for bar in bars if bar["timestamp"].date() <= end] for code in codes})
    monkeypatch.setattr(daily.data, "load_fear_scores",
                        lambda codes, as_of: {code: {day: 20.0 for day in days} for code in codes})
    monkeypatch.setattr(daily.data, "load_index_members",
                        lambda codes, **kwargs: {code: [{"trade_date": days[0], "members": ["600001.SH"]}]
                                                 for code in codes})
    monkeypatch.setattr(daily.data, "load_stock_bars",
                        lambda symbols, start, end: {"600001.SH": [bar for bar in bars
                                                                   if bar["timestamp"].date() <= end]})
    monkeypatch.setattr(daily.data, "load_stock_names", lambda symbols, **kwargs: {"600001.SH": "甲"})
    return days, trigger_day


def test_run_trading_day_writes_snapshots_and_queues_a_buy_order(fake_daily):
    from src.core.database import SectorNineTurnPaperOrder, SessionLocal
    from src.core.services.sector_nine_turn.views import load_daily_view

    days, trigger_day = fake_daily
    paper.reset_paper(1000000.0)
    config = strategy_config.default_sector_nine_turn_config()
    config["universe"]["index_codes"] = ["931151.CSI"]

    result = daily.run_trading_day(as_of=trigger_day, config=config)
    assert result["status"] == "completed"
    summary = result["summary"]
    assert summary["sectors_triggered"] == 1
    assert summary["sectors_armed"] == 1
    assert summary["buy_signals"] == 1
    assert summary["buy_orders"] == 1

    view = load_daily_view(trigger_day)
    assert view["trade_date"] == trigger_day.isoformat()
    assert [row["index_code"] for row in view["sectors"]] == ["931151.CSI"]
    assert view["sectors"][0]["armed"] is True
    buy = next(row for row in view["signals"] if row["action"] == "buy")
    assert buy["ts_code"] == "600001.SH" and buy["rank"] == 1

    with SessionLocal() as db:
        orders = db.query(SectorNineTurnPaperOrder).all()
        assert len(orders) == 1
        assert orders[0].side == "buy" and orders[0].status == "pending"
        # 10 个仓位 → 每笔 10% 净值
        assert orders[0].budget == pytest.approx(100000.0)


def test_run_trading_day_on_a_quiet_day_produces_no_orders(fake_daily):
    days, trigger_day = fake_daily
    paper.reset_paper(1000000.0)
    config = strategy_config.default_sector_nine_turn_config()
    config["universe"]["index_codes"] = ["931151.CSI"]

    quiet = days[5]                       # 还在连跌途中，没有高 2
    result = daily.run_trading_day(as_of=quiet, config=config)
    assert result["summary"]["sectors_armed"] == 0
    assert result["summary"]["buy_signals"] == 0
    assert result["summary"]["buy_orders"] == 0


def test_two_day_flow_order_on_the_signal_day_then_fills_next_open(fake_daily, monkeypatch, tmp_path):
    """信号日只出单；下一交易日开盘才成交并落净值——首日必须把账户起点写进去，否则订单永远不成交。"""
    from src.core.database import SectorNineTurnPaperNav, SectorNineTurnPaperOrder, SectorNineTurnPaperPosition, SessionLocal

    days, trigger_day = fake_daily
    next_day = days[days.index(trigger_day) + 1]

    path = tmp_path / "market.duckdb"
    connection = duckdb.connect(str(path))
    connection.execute(
        "CREATE TABLE a_stock_market_daily (ts_code VARCHAR, trade_date DATE, open DOUBLE, close DOUBLE, pre_close DOUBLE)"
    )
    connection.execute("CREATE TABLE a_stock_adj_factor (ts_code VARCHAR, trade_date DATE, adj_factor DOUBLE)")
    connection.execute("INSERT INTO a_stock_market_daily VALUES ('600001.SH', ?, 20.0, 21.0, 19.8)", [next_day])
    connection.execute("INSERT INTO a_stock_adj_factor VALUES ('600001.SH', ?, 1.0)", [next_day])
    connection.close()
    original_settle = paper.settle          # daily.paper 就是 paper 模块，先存原函数再打桩，否则递归
    monkeypatch.setattr(daily.paper, "settle",
                        lambda book, trade_date, paper_config, **kwargs: original_settle(
                            book, trade_date, paper_config,
                            connect=lambda: duckdb.connect(str(path), read_only=True)))

    paper.reset_paper(1000000.0)
    config = strategy_config.default_sector_nine_turn_config()
    config["universe"]["index_codes"] = ["931151.CSI"]

    first = daily.run_trading_day(as_of=trigger_day, config=config)
    assert first["summary"]["buy_orders"] == 1
    assert first["summary"]["fills"] == 0
    assert "模拟盘从" in first["summary"]["paper_note"]

    second = daily.run_trading_day(as_of=next_day, config=config)
    assert second["summary"]["fills"] == 1
    with SessionLocal() as db:
        order = db.query(SectorNineTurnPaperOrder).filter(SectorNineTurnPaperOrder.side == "buy").one()
        assert order.status == "filled" and order.exec_date == next_day and order.fill_price == 20.0
        position = db.query(SectorNineTurnPaperPosition).one()
        assert position.ts_code == "600001.SH" and position.quantity == 4900   # 10 万 / (20 × 1.0003) 取整手
        navs = db.query(SectorNineTurnPaperNav).order_by(SectorNineTurnPaperNav.trade_date).all()
        assert [row.trade_date for row in navs] == [trigger_day, next_day]

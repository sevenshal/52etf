"""板块九转策略：信号定义、布防窗口、配置默认值、模拟盘出单与回测账目。"""

from datetime import date, datetime, time, timedelta

import duckdb
import pytest

from src.core.services.sector_nine_turn import backtest, config as strategy_config, daily, paper, signals

PARAMS = signals.SignalParams.from_config(strategy_config.default_sector_nine_turn_config())
# 板块层的高 N 区间（默认就是"高2"一个点）；只测形态的用例统一用它，免得被个股区间的默认值带偏
SECTOR = {"high_min": PARAMS.sector_high_min, "high_max": PARAMS.sector_high_max}
# 个股层的区间 + 量能
BUY = {"high_min": PARAMS.buy_high_min, "high_max": PARAMS.buy_high_max, "require_volume": True}
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


def _volume_bars(closes, volumes):
    bars = _bars(closes)
    for bar, volume in zip(bars, volumes):
        bar["volume"] = volume
    return bars


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
    assert signals.low_high_turn_indices(rows, PARAMS, **SECTOR) == [9]


def test_a_second_low_nine_arms_the_signal_again():
    rows = _rows(20)
    rows[3]["lowCount"] = 9
    rows[5]["highCount"] = 2
    rows[10]["lowCount"] = 11          # ≥9 都算
    rows[14]["highCount"] = 2
    assert signals.low_high_turn_indices(rows, PARAMS, **SECTOR) == [5, 14]


def test_high_two_without_a_prior_low_nine_does_not_fire():
    rows = _rows(20)
    rows[6]["lowCount"] = 8            # 没到 9
    rows[9]["highCount"] = 2
    assert signals.low_high_turn_indices(rows, PARAMS, **SECTOR) == []


def test_turn_signal_matches_the_chart_indicator_on_real_shaped_bars():
    """用真实的九转口径（收盘 vs 4 根前收盘）跑一遍，确认和 append_nine_turn_atr 对得上。"""
    closes = [100 - index for index in range(16)]        # 连跌，堆出低 9 以上
    closes += [86 + index for index in range(1, 6)]      # 反弹，堆出高 1/2/3
    rows = signals.nine_turn_rows(_bars(closes))
    fired = signals.low_high_turn_indices(rows, PARAMS, **SECTOR)
    assert fired, "应该出现低9后首次高2"
    first = fired[0]
    assert rows[first]["highCount"] == 2
    assert any(int(row["lowCount"] or 0) >= 9 for row in rows[:first])


# --- 量能过滤 ---------------------------------------------------------------

def _shaped_closes(lead_in=30, rally=10):
    """先走平一段（让放量窗口有足够历史），再连跌堆出低9以上，最后一路反弹把高 N 堆上去。"""
    return [100.0] * lead_in + [100 - index for index in range(16)] + [86 + index for index in range(1, rally + 1)]


def _bar_with_high_count(rows, target, after=0):
    """找出 highCount 正好等于 target 的那一根（从 after 之后开始找）。"""
    return next(index for index in range(after, len(rows)) if int(rows[index]["highCount"] or 0) == target)


def _base_volumes(count):
    """有正常起伏的成交量（标准差不为 0），否则 z 值算不出来。"""
    return [1_000_000.0 * (1 + 0.05 * ((index % 5) - 2)) for index in range(count)]


def test_stock_signal_requires_a_volume_spike_on_the_day():
    """区间收成一个点（只有高2一根候选），单独验量能这一条。"""
    one_bar = {"high_min": 2, "high_max": 2, "require_volume": True}
    closes = _shaped_closes()
    volumes = _base_volumes(len(closes))
    rows = signals.nine_turn_rows(_volume_bars(closes, volumes), PARAMS)
    fired = signals.low_high_turn_indices(rows, PARAMS, high_min=2, high_max=2)
    assert fired, "形态本身应该触发"
    bar = fired[0]
    assert rows[bar]["volumeZScore"] < PARAMS.volume_z_min
    assert signals.low_high_turn_indices(rows, PARAMS, **one_bar) == []

    spiked = list(volumes)
    spiked[bar] = 3_000_000.0
    rows = signals.nine_turn_rows(_volume_bars(closes, spiked), PARAMS)
    assert rows[bar]["volumeZScore"] > PARAMS.volume_z_min
    assert signals.low_high_turn_indices(rows, PARAMS, **one_bar) == fired


def test_high_range_waits_for_the_first_bar_that_actually_has_volume():
    """区间的意义：高1没放量就等高2、高3……第一根放量的才是信号。"""
    closes = _shaped_closes()
    volumes = _base_volumes(len(closes))
    rows = signals.nine_turn_rows(_volume_bars(closes, volumes), PARAMS)
    high_one = signals.low_high_turn_indices(rows, PARAMS, high_min=1, high_max=1)[0]
    high_three = _bar_with_high_count(rows, 3, after=high_one)

    spiked = list(volumes)
    spiked[high_three] = 3_000_000.0
    rows = signals.nine_turn_rows(_volume_bars(closes, spiked), PARAMS)
    assert signals.low_high_turn_indices(
        rows, PARAMS, high_min=1, high_max=4, require_volume=True) == [high_three]


def test_high_range_gives_up_once_the_count_passes_the_upper_bound():
    """区间内（高1~高4）都没放量，等到高5 才放量——这一次低9 已经作废，不补开。"""
    closes = _shaped_closes()
    volumes = _base_volumes(len(closes))
    rows = signals.nine_turn_rows(_volume_bars(closes, volumes), PARAMS)
    high_one = signals.low_high_turn_indices(rows, PARAMS, high_min=1, high_max=1)[0]
    beyond = _bar_with_high_count(rows, 5, after=high_one)     # 高5，已经冲过上限
    spiked = list(volumes)
    # 区间内几根压成缩量，确保它们自己不会先触发
    for index in range(high_one, beyond):
        spiked[index] = 300_000.0
    spiked[beyond] = 3_000_000.0
    rows = signals.nine_turn_rows(_volume_bars(closes, spiked), PARAMS)
    assert all(rows[index]["volumeZScore"] < PARAMS.volume_z_min for index in range(high_one, beyond))
    assert rows[beyond]["volumeZScore"] > PARAMS.volume_z_min
    assert signals.low_high_turn_indices(
        rows, PARAMS, high_min=1, high_max=4, require_volume=True) == []


def test_volume_filter_uses_the_prior_window_not_including_today():
    """窗口不含当日：当天那根放不放量，不会把自己算进均值里。"""
    closes = _shaped_closes()
    volumes = _base_volumes(len(closes))
    fired = signals.low_high_turn_indices(
        signals.nine_turn_rows(_volume_bars(closes, volumes), PARAMS), PARAMS,
        high_min=2, high_max=2)[0]
    spiked = list(volumes)
    spiked[fired] = 3_000_000.0
    rows = signals.nine_turn_rows(_volume_bars(closes, spiked), PARAMS)
    # 均值和标准差只由前 20 根决定，和当天这根无关
    baseline = signals.nine_turn_rows(_volume_bars(closes, volumes), PARAMS)
    assert rows[fired]["logVolumeMean"] == baseline[fired]["logVolumeMean"]
    assert rows[fired]["logVolumeStdDev"] == baseline[fired]["logVolumeStdDev"]

    # 前几根大幅放量、当天回到常态 → 相对最近的窗口反而是缩量
    shrunk = list(volumes)
    window = signals.SignalParams(volume_lookback_days=5)
    for offset, index in enumerate(range(fired - 5, fired)):
        shrunk[index] = 4_000_000.0 + offset * 100_000.0     # 有起伏，标准差不为 0
    rows = signals.nine_turn_rows(_volume_bars(closes, shrunk), window)
    assert rows[fired]["volumeZScore"] < 0
    assert signals.low_high_turn_indices(
        rows, window, high_min=2, high_max=2, require_volume=True) == []


def test_volume_filter_can_be_turned_off_and_threshold_is_configurable():
    closes = _shaped_closes()
    volumes = _base_volumes(len(closes))
    one_bar = {"high_min": 2, "high_max": 2, "require_volume": True}
    fired = signals.low_high_turn_indices(
        signals.nine_turn_rows(_volume_bars(closes, volumes), PARAMS), PARAMS, high_min=2, high_max=2)
    volumes[fired[0]] = 1_600_000.0            # 温和放量

    off = signals.SignalParams(volume_filter_enabled=False)
    assert signals.low_high_turn_indices(
        signals.nine_turn_rows(_volume_bars(closes, volumes), off), off, **one_bar) == fired

    loose = signals.SignalParams(volume_z_min=0.5)
    strict = signals.SignalParams(volume_z_min=8.0)
    assert signals.low_high_turn_indices(
        signals.nine_turn_rows(_volume_bars(closes, volumes), loose), loose, **one_bar) == fired
    assert signals.low_high_turn_indices(
        signals.nine_turn_rows(_volume_bars(closes, volumes), strict), strict, **one_bar) == []


def test_volume_filter_does_not_block_when_the_window_is_incomplete():
    """上市不满一个回看窗口就没有 z 值——没信息不算负面信号，不拦。"""
    closes = _shaped_closes()
    params = signals.SignalParams(volume_lookback_days=250)
    rows = signals.nine_turn_rows(_volume_bars(closes, _base_volumes(len(closes))), params)
    fired = signals.low_high_turn_indices(rows, PARAMS, high_min=2, high_max=2)
    assert rows[fired[0]]["volumeZScore"] is None
    assert signals.low_high_turn_indices(
        rows, params, high_min=2, high_max=2, require_volume=True) == fired


def test_sector_triggers_ignore_volume():
    """量能只管个股：板块层不看成交量，否则板块指数的量能会把择时也带偏。"""
    closes = _shaped_closes()
    rows = signals.nine_turn_rows(_volume_bars(closes, [1_000_000.0] * len(closes)), PARAMS)
    dates = _dates(_bars(closes))
    triggers = signals.sector_triggers(rows, dates, PARAMS, {day: 20.0 for day in dates})
    assert triggers, "板块触发不应该被量能拦掉"


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


def test_fear_gate_looks_back_several_trading_days():
    """贪恐见底和九转翻红常差几天：触发当天已经反弹上去，只要窗口内触及过就算过闸门。"""
    bars = _bars([100 - index for index in range(16)] + [86 + index for index in range(1, 6)])
    dates = _dates(bars)
    rows = signals.nine_turn_rows(bars)
    trigger_index = signals.low_high_turn_indices(rows, PARAMS, **SECTOR)[0]
    trigger_day = dates[trigger_index]

    inside = PARAMS.fear_lookback_days - 1          # 窗口含当天，最早能算数的那一天
    outside = PARAMS.fear_lookback_days + 1
    # 触发当天 55（不过闸门），但窗口内最早那天是 28（过闸门）
    scores = {day: 55.0 for day in dates}
    scores[dates[trigger_index - inside]] = 28.0
    triggers = signals.sector_triggers(rows, dates, PARAMS, scores)
    today = next(item for item in triggers if item["signal_date"] == trigger_day)
    assert today["fear_score"] == 55.0            # 当天分数照实记录
    assert today["fear_min"] == 28.0              # 闸门看的是窗口最低分
    assert today["fear_pass_date"] == dates[trigger_index - inside]
    assert today["fear_passed"] is True
    assert signals.armed_windows(triggers, dates, PARAMS).get(trigger_day)

    # 同样的 28 挪到窗口之外就不算数了
    stale = {day: 55.0 for day in dates}
    stale[dates[trigger_index - outside]] = 28.0
    triggers = signals.sector_triggers(rows, dates, PARAMS, stale)
    today = next(item for item in triggers if item["signal_date"] == trigger_day)
    assert today["fear_passed"] is False
    assert today["fear_pass_date"] is None


def test_fear_lookback_one_day_is_the_old_same_day_rule():
    bars = _bars([100 - index for index in range(16)] + [86 + index for index in range(1, 6)])
    dates = _dates(bars)
    rows = signals.nine_turn_rows(bars)
    trigger_index = signals.low_high_turn_indices(rows, PARAMS, **SECTOR)[0]
    scores = {day: 55.0 for day in dates}
    scores[dates[trigger_index - 1]] = 28.0
    same_day = signals.SignalParams(fear_lookback_days=1)
    triggers = signals.sector_triggers(rows, dates, same_day, scores)
    assert all(not trigger["fear_passed"] for trigger in triggers)


def test_armed_window_ranks_by_the_most_fearful_day_in_the_lookback():
    """布防信息里带的分数是窗口内最低分——排序和闸门判定必须同一个口径。"""
    bars = _bars([100 - index for index in range(16)] + [86 + index for index in range(1, 6)])
    dates = _dates(bars)
    rows = signals.nine_turn_rows(bars)
    trigger_index = signals.low_high_turn_indices(rows, PARAMS, **SECTOR)[0]
    scores = {day: 55.0 for day in dates}
    scores[dates[trigger_index - 1]] = 22.0
    triggers = signals.sector_triggers(rows, dates, PARAMS, scores)
    window = signals.armed_windows(triggers, dates, PARAMS)[dates[trigger_index]]
    assert window["fear_score"] == 22.0
    assert window["fear_score_today"] == 55.0
    assert window["fear_pass_date"] == dates[trigger_index - 1]


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


def test_default_signal_params_match_the_parameter_search_result():
    params = signals.SignalParams.from_config(strategy_config.default_sector_nine_turn_config())
    assert params.arm_window_days == 0          # 板块与个股同日
    assert params.fear_threshold == 40.0
    assert params.fear_lookback_days == 3       # 闸门看最近 3 个交易日，不是只看当天
    assert params.volume_filter_enabled is True
    assert params.volume_z_min == 1.0           # 放量至少 1 个标准差
    assert params.volume_lookback_days == 20    # 窗口不含当日，往前 20 个交易日
    assert params.low_count_min == 9
    assert (params.buy_high_min, params.buy_high_max) == (2, 4)
    assert (params.sector_high_min, params.sector_high_max) == (2, 2)
    assert params.high_count_min == 9 and params.sell_low_count == 2
    assert params.sell_atr_multiple == 2.0


def test_signal_params_dataclass_defaults_match_the_config_defaults():
    """直接 SignalParams() 构造的，必须和走配置那条路完全一样——否则两边会悄悄漂移。"""
    from dataclasses import asdict

    assert asdict(signals.SignalParams()) == asdict(
        signals.SignalParams.from_config(strategy_config.default_sector_nine_turn_config()))


def test_config_normalization_clamps_and_drops_unknown_keys():
    saved = strategy_config.normalize_sector_nine_turn_config({
        "universe": {"index_codes": ["000688.SH", "不存在.XX"]},
        "signal": {"fear_threshold": 500, "fear_lookback_days": 999,
                   "volume_z_min": 99, "volume_lookback_days": 1, "volume_filter_enabled": False,
                   "arm_window_days": -3, "sell_mode": "乱写"},
        "portfolio": {"max_positions": 0, "pick_order": "乱写"},
        "paper": {"initial_capital": 1, "enabled": False},
        "垃圾": 1,
    })
    assert saved["universe"]["index_codes"] == ["000688.SH"]
    assert saved["signal"]["fear_threshold"] == 100.0
    assert saved["signal"]["fear_lookback_days"] == 60.0
    assert saved["signal"]["volume_z_min"] == 10.0
    assert saved["signal"]["volume_lookback_days"] == 5.0
    assert saved["signal"]["volume_filter_enabled"] is False
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
    config["signal"]["buy_high_min"] = 2        # 和板块同一个点，专测流水线
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
    config["signal"]["buy_high_min"] = 2
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
    rows = signals.nine_turn_rows(bars)
    trigger_day = days[signals.low_high_turn_indices(rows, PARAMS, **SECTOR)[0]]

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
    config["signal"]["buy_high_min"] = 2

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
    config["signal"]["buy_high_min"] = 2

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
    config["signal"]["buy_high_min"] = 2

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

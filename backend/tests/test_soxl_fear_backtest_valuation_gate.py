from unittest.mock import patch

import numpy as np
import pandas as pd

from src.app.api.soxl_fear_backtest import (
    SOXLFearSearchParams,
    SOXLFearStrategyParams,
    _count_search_params,
    _prepare_base_dataframe,
    _run_backtest,
    _run_seesaw_backtest,
    _valuation_column,
)
from src.core.services import a_stock_index_valuation as valuation_service

# 估值点位越大越贵。第 1 天极恐放量买入；第 3 天起贪婪（第 4 天贪恐 90）；估值点位第 5 天起才升到 90（极度高估）
FEAR = [50.0, 20.0, 50.0, 80.0, 90.0, 80.0, 80.0, 80.0, 50.0, 50.0]
VALUATION = [50.0, 50.0, 50.0, 50.0, 50.0, 90.0, 90.0, 90.0, 90.0, 90.0]


def _frame(fear, valuation, symbol="510880.SH"):
    size = len(fear)
    dates = list(pd.bdate_range("2025-03-03", periods=size).date)
    price = [10.0 + index for index in range(size)]
    frame = pd.DataFrame({
        "date": dates,
        "signal_date": dates,
        "fear_date": dates,
        "open": price,
        "high": price,
        "low": price,
        "close": price,
        "execution_price": price,
        "volume": [100.0] * size,
        "signal_volume": [100.0] * size,
        "ma20": price,
        "volume_ma20": [50.0] * size,
        "volume_ratio": [2.0] * size,
        "fear_greed": fear,
        "log_z": [np.nan] * size,
        "log_z_self": [np.nan] * size,
        _valuation_column(252): valuation,
        _valuation_column(504): valuation,
    })
    frame.attrs["symbol"] = symbol
    frame.attrs["fear_source_label"] = "上证红利 指数贪恐"
    return frame


def _params(**overrides):
    values = dict(
        buy_threshold=30, greed_threshold=70, volume_ratio_threshold=1.5,
        buy_position_pct=100, cooldown_days=0, trailing_stop_pct=0,
        sell_position_pct=100, sell_reduction_basis="holdings", sell_price_above_avg_cost=False,
        min_position_pct_after_take_profit=0, rebalance_threshold_pct=0,
        execute_next_open=False, slippage_pct=0,
    )
    values.update(overrides)
    return SOXLFearStrategyParams(**values)


def _trade_days(result, action):
    return [trade["date"] for trade in result["trades"] if trade["action"] == action]


def _day(index):
    return pd.bdate_range("2025-03-03", periods=10)[index].date().isoformat()


def test_sell_gate_holds_until_valuation_is_expensive():
    frame = _frame(FEAR, VALUATION)
    plain = _run_backtest(frame, _params(), 1_000_000.0, detailed=True)
    gated = _run_backtest(frame, _params(valuation_sell_min=80), 1_000_000.0, detailed=True)

    assert _trade_days(plain, "SELL") == [_day(3)]
    assert _trade_days(gated, "SELL") == [_day(5)]
    assert "估值点位 90.0" in [t for t in gated["trades"] if t["action"] == "SELL"][0]["reason"]


def test_force_sell_greed_overrides_valuation_gate():
    frame = _frame(FEAR, VALUATION)
    result = _run_backtest(
        frame, _params(valuation_sell_min=80, valuation_force_sell_greed=85), 1_000_000.0, detailed=True,
    )
    assert _trade_days(result, "SELL") == [_day(4)]


def test_buy_gate_blocks_expensive_buy_but_missing_valuation_passes():
    # 买入闸门：估值点位 <= 20（极度低估）才买；第 1 天点位 50 不够便宜
    blocked = _run_backtest(_frame(FEAR, VALUATION), _params(valuation_buy_max=20), 1_000_000.0, detailed=True)
    assert _trade_days(blocked, "BUY") == []

    no_valuation = [np.nan] * len(FEAR)
    passed = _run_backtest(_frame(FEAR, no_valuation), _params(valuation_buy_max=20), 1_000_000.0, detailed=True)
    assert _trade_days(passed, "BUY") == [_day(1)]


def test_valuation_gate_uses_signal_day_value_with_next_open_execution():
    # 次日开盘成交：第 5 天（估值点位 90）是信号日，第 6 天开盘成交
    frame = _frame(FEAR, VALUATION)
    result = _run_backtest(
        frame, _params(valuation_sell_min=80, execute_next_open=True), 1_000_000.0, detailed=True,
    )
    assert _trade_days(result, "SELL") == [_day(6)]


def test_seesaw_applies_gate_to_each_leg_with_its_own_valuation():
    main = _frame([50.0] * len(FEAR), [np.nan] * len(FEAR))
    sub = _frame(FEAR, VALUATION, symbol="512480.SH")
    params = _params(
        sub_symbol="512480.SH", sub_buy_threshold=25, sub_volume_ratio_threshold=1.6,
        swap_threshold=45, valuation_sell_min=80,
    )
    result = _run_seesaw_backtest(main, sub, params, 1_000_000.0, detailed=True)
    assert _trade_days(result, "BUY") == [_day(1)]
    assert _trade_days(result, "SELL") == [_day(5)]

    # 候补腿没有估值时不设闸，贪婪即卖
    sub_without_valuation = _frame(FEAR, [np.nan] * len(FEAR), symbol="512480.SH")
    result = _run_seesaw_backtest(main, sub_without_valuation, params, 1_000_000.0, detailed=True)
    assert _trade_days(result, "SELL") == [_day(3)]


def test_prepare_base_dataframe_aligns_valuation_to_fear_date():
    dates = list(pd.bdate_range("2024-01-01", periods=40).date)
    price_df = pd.DataFrame({
        "date": dates, "open": [10.0] * 40, "high": [12.0] * 40, "low": [9.0] * 40,
        "close": [11.0] * 40, "volume": [100.0] * 40, "turnover": [1000.0] * 40,
    })
    fear_df = pd.DataFrame({"date": dates, "fear_greed": [50.0] * 40})
    positions = {dates[30]: {252: 91.0, 504: 85.0}}
    with patch(
        "src.app.api.soxl_fear_backtest._fetch_price_history", return_value=price_df,
    ), patch(
        "src.app.api.soxl_fear_backtest._fetch_fear_history",
        return_value=(fear_df, {"fear_source": "a_stock_000015_sh", "fear_source_label": "上证红利", "fear_points": 40}),
    ), patch(
        "src.app.api.soxl_fear_backtest._fetch_valuation_positions", return_value=positions,
    ):
        base_df, meta = _prepare_base_dataframe("510880.SH", dates[25], dates[-1], "a_stock_000015_sh")

    row = base_df[base_df["date"] == dates[30]].iloc[0]
    assert row[_valuation_column(252)] == 91.0
    assert row[_valuation_column(504)] == 85.0
    assert base_df[_valuation_column(252)].notna().sum() == 1
    assert meta["valuation_points"] == 1


def test_valuation_position_history_has_no_look_ahead():
    days = list(pd.bdate_range("2023-01-02", periods=300).date)
    gaps = [float((index * 37) % 101) for index in range(300)]
    full = valuation_service.build_valuation_position_history(zip(days, gaps))
    partial = valuation_service.build_valuation_position_history(zip(days[:200], gaps[:200]))

    assert all(full[day] == partial[day] for day in days[:200])
    assert full[days[100]][252] is None  # 样本不足 120 天
    # 最后一天与页面口径一致：用全部历史调用同一个分位函数
    fields = valuation_service._build_valuation_position_fields(gaps, gaps[-1])
    assert full[days[-1]][252] == fields["valuation_position_252_pct"]
    assert full[days[-1]][504] == fields["valuation_position_pct"]


def test_valuation_position_is_higher_when_more_expensive():
    # 估值偏离（上涨空间）越大越便宜 → 估值点位越小
    gaps = [float(value) for value in range(200)]
    cheapest = valuation_service._build_valuation_position_fields(gaps, 199.0)
    priciest = valuation_service._build_valuation_position_fields(gaps, 0.0)
    assert cheapest["valuation_position_pct"] < 1 and cheapest["valuation_position_label"] == "极度低估"
    assert priciest["valuation_position_pct"] > 99 and priciest["valuation_position_label"] == "极度高估"
    assert priciest["valuation_position_basis"] == valuation_service.VALUATION_POSITION_BASIS


def test_legacy_snapshot_position_is_converted_to_new_direction():
    legacy = {
        "valuation_position_pct": 85.0, "valuation_position_label": "极度低估",
        "valuation_position_252_pct": 30.0, "valuation_position_252_label": "高估",
    }
    converted = valuation_service._normalize_legacy_position_fields(legacy)
    assert (converted["valuation_position_pct"], converted["valuation_position_label"]) == (15.0, "极度低估")
    assert (converted["valuation_position_252_pct"], converted["valuation_position_252_label"]) == (70.0, "高估")
    # 已是新口径的快照原样返回
    assert valuation_service._normalize_legacy_position_fields(converted) is converted


def test_search_counts_valuation_candidates():
    base = SOXLFearSearchParams()
    widened = SOXLFearSearchParams(valuation_sell_min_values=[None, 70, 80], valuation_window_values=[252, 504])
    assert _count_search_params(widened) == _count_search_params(base) * 6

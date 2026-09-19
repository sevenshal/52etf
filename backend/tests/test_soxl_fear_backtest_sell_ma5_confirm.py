"""卖出跌破 MA5 确认 + 第三候补（四标的轮动）。

MA5 口径与实盘一致：用「量比来源标的」的收盘价，跌破自身 5 日均线才卖；
卖出信号一旦成立就一直挂着（即使贪恐回落），挂起期间不发起换仓。
"""

from unittest.mock import patch

import numpy as np
import pandas as pd

from src.app.api.soxl_fear_backtest import (
    SELL_MA5_CONFIRM_ALL,
    SELL_MA5_CONFIRM_NON_MAIN,
    SELL_MA5_CONFIRM_OFF,
    SIGNAL_BELOW_MA5_COLUMN,
    SOXLFearStrategyParams,
    _run_backtest,
    _run_seesaw_backtest,
    _valuation_column,
)


def _frame(fear, close, symbol="510880.SH", label="上证红利 指数贪恐"):
    size = len(fear)
    dates = list(pd.bdate_range("2025-03-03", periods=size).date)
    close = [float(item) for item in close]
    ma5 = pd.Series(close).rolling(5, min_periods=5).mean()
    below_ma5 = (pd.Series(close) < ma5).where(ma5.notna(), False).astype(bool)
    frame = pd.DataFrame({
        "date": dates,
        "signal_date": dates,
        "fear_date": dates,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "execution_price": close,
        "volume": [100.0] * size,
        "signal_volume": [100.0] * size,
        "ma20": close,
        "volume_ma20": [50.0] * size,
        "volume_ratio": [2.0] * size,
        "volume_ratio_consecutive_1": [2.0] * size,
        "fear_greed": fear,
        "log_z": [np.nan] * size,
        "log_z_self": [np.nan] * size,
        SIGNAL_BELOW_MA5_COLUMN: below_ma5,
        _valuation_column(252): [np.nan] * size,
        _valuation_column(504): [np.nan] * size,
    })
    frame.attrs["symbol"] = symbol
    frame.attrs["fear_source_label"] = label
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


# 第 2 天极恐买入；第 5 天起贪婪，但价格一路上行没跌破 MA5，第 9 天才回落跌破
FEAR = [50.0, 20.0, 50.0, 50.0, 80.0, 80.0, 60.0, 60.0, 60.0, 60.0]
CLOSE = [10.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 10.0, 10.0]


def _sell_dates(result):
    return [item["date"] for item in result["trades"] if item["action"] == "SELL"]


def test_sell_ma5_off_sells_on_greed_day():
    result = _run_backtest(_frame(FEAR, CLOSE), _params(), 100000.0, detailed=True)
    assert _sell_dates(result) == ["2025-03-07"]


def test_sell_ma5_all_waits_for_ma5_break():
    result = _run_backtest(
        _frame(FEAR, CLOSE), _params(sell_ma5_confirm=SELL_MA5_CONFIRM_ALL), 100000.0, detailed=True,
    )
    # 贪婪日（03-07）价格还在 MA5 上方，等到 03-13 跌破才卖
    assert _sell_dates(result) == ["2025-03-13"]


def test_sell_signal_stays_pending_after_fear_falls_back():
    """贪恐回落到卖出阈值以下，挂起的卖出信号依然有效。"""
    fear = [50.0, 20.0, 50.0, 50.0, 80.0, 50.0, 50.0, 50.0, 50.0, 50.0]
    result = _run_backtest(
        _frame(fear, CLOSE), _params(sell_ma5_confirm=SELL_MA5_CONFIRM_ALL), 100000.0, detailed=True,
    )
    assert _sell_dates(result) == ["2025-03-13"]


def _seesaw_frames():
    main = _frame(FEAR, CLOSE)
    # 候补：第 2 天更恐慌所以先买候补；第 5 天起贪婪，价格第 7 天就跌破 MA5
    sub_fear = [50.0, 10.0, 50.0, 50.0, 80.0, 80.0, 60.0, 60.0, 60.0, 60.0]
    sub_close = [10.0, 10.0, 11.0, 12.0, 13.0, 9.0, 9.0, 9.0, 9.0, 9.0]
    sub = _frame(sub_fear, sub_close, symbol="512480.SH", label="科创50 指数贪恐")
    return main, sub


def test_seesaw_non_main_only_confirms_candidates():
    main, sub = _seesaw_frames()
    params = _params(
        sub_symbol="512480.SH", sub_buy_threshold=30, sub_volume_ratio_threshold=1.5,
        swap_threshold=45, sell_ma5_confirm=SELL_MA5_CONFIRM_NON_MAIN,
    )
    result = _run_seesaw_backtest(main, sub, params, 100000.0, detailed=True)
    sells = [item for item in result["trades"] if item["action"] == "SELL"]
    # 持有的是候补（第 2 天更恐慌），候补要等跌破 MA5：贪婪日 03-07，价格回落跌破 MA5 是 03-10
    assert [item["symbol"] for item in sells] == ["512480.SH"]
    assert [item["date"] for item in sells] == ["2025-03-10"]


def test_seesaw_off_mode_unchanged():
    main, sub = _seesaw_frames()
    params = _params(
        sub_symbol="512480.SH", sub_buy_threshold=30, sub_volume_ratio_threshold=1.5,
        swap_threshold=45, sell_ma5_confirm=SELL_MA5_CONFIRM_OFF,
    )
    result = _run_seesaw_backtest(main, sub, params, 100000.0, detailed=True)
    assert [item["date"] for item in result["trades"] if item["action"] == "SELL"] == ["2025-03-07"]


def test_sub3_joins_rotation_and_can_be_bought():
    """第三候补最恐慌时应该被买入（四标的对称轮动）。"""
    main, sub = _seesaw_frames()
    sub2 = _frame([50.0] * 10, [10.0] * 10, symbol="159509.SZ", label="QQQ 纳指100自算贪恐")
    sub3_fear = [50.0, 5.0, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0]
    sub3 = _frame(sub3_fear, [10.0, 10.0, 12.0, 12.0, 12.0, 12.0, 12.0, 12.0, 12.0, 12.0],
                  symbol="516510.SH", label="云计算 指数贪恐")
    params = _params(
        sub_symbol="512480.SH", sub_buy_threshold=30, sub_volume_ratio_threshold=1.5,
        sub2_symbol="159509.SZ", sub2_buy_threshold=30, sub2_volume_ratio_threshold=1.5,
        sub3_symbol="516510.SH", sub3_buy_threshold=30, sub3_volume_ratio_threshold=1.5,
        swap_threshold=45,
    )
    result = _run_seesaw_backtest(
        main, sub, params, 100000.0, detailed=True, sub2_base_df=sub2, sub3_base_df=sub3,
    )
    buys = [item for item in result["trades"] if item["action"] == "BUY"]
    assert buys and buys[0]["symbol"] == "516510.SH"


def test_sub3_thresholds_join_the_search_grid():
    """第三候补阈值是候选列表，和 sub/sub2 一样参与组合搜索。"""
    from itertools import product

    from src.app.api.soxl_fear_backtest import (
        SOXLFearSearchParams,
        _count_search_params,
        _evaluate_search_batch,
    )

    payload = SOXLFearSearchParams(
        symbol="510880.SH",
        sub_symbol="512480.SH",
        sub3_symbol="516510.SH",
        sub3_buy_threshold_values=[20.0, 25.0],
        sub3_volume_ratio_threshold_values=[1.3, 1.6],
    )
    base = SOXLFearSearchParams(symbol="510880.SH", sub_symbol="512480.SH")
    # 2 × 2 组第三候补阈值 → 组合数翻 4 倍
    assert _count_search_params(payload) == _count_search_params(base) * 4

    # 网格里的取值要如实落到回测参数上（防止元组顺序错位）
    values = next(iter(product(
        payload.buy_threshold_values, payload.greed_threshold_values,
        payload.volume_ratio_threshold_values, payload.volume_ratio_consecutive_days_values,
        payload.buy_position_pct_values, payload.cooldown_days_values,
        payload.trailing_stop_pct_values, payload.sell_position_pct_values,
        payload.sell_reduction_basis_values, payload.sell_price_above_avg_cost_values,
        payload.max_take_profit_sells_per_cycle_values, payload.min_position_pct_after_take_profit_values,
        payload.execute_next_open_values, payload.sub_buy_threshold_values,
        payload.sub_volume_ratio_threshold_values, payload.swap_threshold_values,
        payload.sub2_buy_threshold_values, payload.sub2_volume_ratio_threshold_values,
        [25.0], [1.6],  # 第三候补恐慌阈值 / 量比阈值
        payload.volume_z_threshold_values, payload.sell_shrink_z_values,
        payload.buy_turn_signal_mode_values, payload.sell_turn_signal_mode_values,
        payload.ma5_bottom_score_values, payload.ma5_top_score_values,
        payload.ma5_lookback_days_values, payload.volume_bottom_score_values,
        payload.volume_top_score_values, payload.volume_expand_std_values,
        payload.volume_shrink_std_values, payload.turn_signal_cooldown_days_values,
        payload.valuation_window_values, payload.valuation_buy_max_values,
        payload.valuation_sell_min_values, payload.valuation_force_sell_greed_values,
    )))
    captured = {}

    def _capture(base_df, sub_base_df, params, initial_capital, detailed=False, **kwargs):
        captured["params"] = params
        return {"total_return": 0.0, "annualized_return": 0.0, "sharpe_ratio": 0.0,
                "max_drawdown": 0.0, "calmar_ratio": 0.0}

    main, sub = _seesaw_frames()
    with patch("src.app.api.soxl_fear_backtest._run_seesaw_backtest", _capture):
        _evaluate_search_batch(
            main, "a_stock_000015_sh", "上证红利 指数贪恐", 100000.0, "annualized_return", 0.0,
            [(0, values)], 0.0, 0.0,
            sub_base_df=sub, sub_symbol="512480.SH",
            sub3_base_df=main, sub3_symbol="516510.SH",
        )
    assert captured["params"].sub3_buy_threshold == 25.0
    assert captured["params"].sub3_volume_ratio_threshold == 1.6


def test_seesaw_trailing_stop_tracks_greed_peak():
    """移动止盈 > 0 的跷跷板回测：回撤到阈值才卖（曾因 greed_peak_price 漏写 nonlocal 直接抛错）。"""
    # 第 2 天极恐买入；第 5 天起贪婪并冲高，之后回落超过 10%
    fear = [50.0, 20.0, 50.0, 50.0, 80.0, 80.0, 80.0, 80.0, 80.0, 80.0]
    close = [10.0, 10.0, 11.0, 12.0, 20.0, 20.0, 19.5, 17.0, 17.0, 17.0]
    main = _frame(fear, close)
    sub = _frame([50.0] * 10, [10.0] * 10, symbol="512480.SH", label="科创50 指数贪恐")
    params = _params(
        trailing_stop_pct=10.0,
        sub_symbol="512480.SH", sub_buy_threshold=30, sub_volume_ratio_threshold=1.5,
        swap_threshold=45,
    )
    result = _run_seesaw_backtest(main, sub, params, 100000.0, detailed=True)
    sells = [item for item in result["trades"] if item["action"] == "SELL"]
    # 峰值 20.0，跌到 17.0 回撤 15% >= 10% 触发
    assert [item["date"] for item in sells] == ["2025-03-12"]
    assert "移动止盈" in sells[0]["reason"]

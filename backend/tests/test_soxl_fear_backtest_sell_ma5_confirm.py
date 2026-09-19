"""卖出跌破 MA5 确认 + 第三候补（四标的轮动）。

MA5 口径与实盘一致：用「量比来源标的」的收盘价，跌破自身 5 日均线才卖；
卖出信号一旦成立就一直挂着（即使贪恐回落），挂起期间不发起换仓。
"""

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

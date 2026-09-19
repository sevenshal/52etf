"""实盘：卖出跌破 MA5 确认（挂起的卖出信号 + 第三候补）。

与回测 ``sell_ma5_confirm`` 同口径：贪恐到达卖出阈值（且过估值闸门）后不立刻卖，
挂起等持仓标的的量比来源收盘跌破 5 日均线那天才卖；挂起期间不发起换仓。
"""

import asyncio
from contextlib import contextmanager
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd

from src.robot.a_stock_fear_strategy_trader import (
    SELL_MA5_CONFIRM_ALL,
    SELL_MA5_CONFIRM_NON_MAIN,
    SELL_MA5_CONFIRM_OFF,
    SHANGHAI_TZ,
    AStockFearStrategyTrader,
)

TRADER_MODULE = "src.robot.a_stock_fear_strategy_trader"
TODAY = datetime(2026, 9, 14, 9, 30, tzinfo=SHANGHAI_TZ)  # 周一
SIGNAL_DATE = date(2026, 9, 11)  # 前一交易日（周五）


class _EmptyQuery:
    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return None


@contextmanager
def _empty_db_ctx():
    yield SimpleNamespace(query=lambda *args, **kwargs: _EmptyQuery())


def _config(**overrides):
    values = dict(
        id=1, account_id="acc", enabled=True, symbol="510880.SH", fear_source="a_stock_000015_sh",
        volume_signal_symbol=None, sub_symbol=None, sub2_symbol=None, sub3_symbol=None, swap_threshold=None,
        buy_threshold=35.0, greed_threshold=70.0, volume_ratio_threshold=1.6, volume_z_threshold=None,
        sell_shrink_z=-1.0, trailing_stop_pct=0.0, buy_position_pct=100.0, sell_position_pct=100.0,
        cooldown_days=0, max_take_profit_sells_per_cycle=2, min_position_pct_after_take_profit=0.0,
        rebalance_threshold_pct=0.0, sell_reduction_basis="holdings", sell_price_above_avg_cost=False,
        external_trading_account_id=1, live_sub_account_id=1,
        valuation_window=252, valuation_buy_max=None, valuation_sell_min=None, valuation_force_sell_greed=None,
        sell_ma5_confirm=SELL_MA5_CONFIRM_OFF,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _bars(last_close):
    """前 4 根收盘 3.0，最后一根（信号日）按参数给定，用于控制是否跌破 MA5。"""
    dates = pd.bdate_range(end=SIGNAL_DATE, periods=30).date
    closes = [3.0] * 29 + [float(last_close)]
    return pd.DataFrame({
        "trade_date": dates, "open": closes, "high": closes, "low": closes,
        "close": closes, "volume": [100.0] * 30,
    })


def _holding_main_snapshot():
    return SimpleNamespace(
        shares=1000, available_shares=1000, avg_cost=2.5,
        sub_shares=0, sub_available_shares=0, sub_avg_cost=0.0,
        sub2_shares=0, sub2_available_shares=0, sub2_avg_cost=0.0,
        sub3_shares=0, sub3_available_shares=0, sub3_avg_cost=0.0,
        available_cash=0.0, portfolio_value=3000.0, has_today_order=False,
        external_trading_account_id=1, live_sub_account_id=1,
    )


def _run(config, fear, last_close, state_row=None):
    trader = AStockFearStrategyTrader()
    state_query = MagicMock()
    state_query.filter.return_value = state_query
    state_query.first.return_value = state_row

    @contextmanager
    def _db_ctx():
        yield SimpleNamespace(query=lambda model, *args: state_query if "State" in model.__name__ else _EmptyQuery())

    with patch(f"{TRADER_MODULE}.get_db_ctx", _db_ctx if state_row is not None else _empty_db_ctx), \
            patch(f"{TRADER_MODULE}._is_china_trading_day", lambda day: day.weekday() < 5), \
            patch(f"{TRADER_MODULE}.get_realtime_price_details",
                  AsyncMock(return_value={"510880.SH": {"price": 3.0}})), \
            patch(f"{TRADER_MODULE}.publish_event"), \
            patch(f"{TRADER_MODULE}.send_alert_email"), \
            patch.object(trader, "_china_now", return_value=TODAY), \
            patch.object(trader, "_fetch_fear_map", return_value={SIGNAL_DATE: fear}), \
            patch.object(trader, "_fetch_etf_bars", return_value=_bars(last_close)), \
            patch.object(trader, "_build_snapshot", AsyncMock(return_value=_holding_main_snapshot())), \
            patch.object(trader, "_sync_target_order", AsyncMock(return_value="order-1")) as sync_order, \
            patch.object(trader, "_persist_run_result") as persist, \
            patch.object(trader, "_send_rebalance_notification"), \
            patch.object(trader, "_append_error_log") as error_log:
        asyncio.run(trader.run_config_once(config, trigger_source="manual", ignore_enabled=True))
    assert not error_log.called, error_log.call_args
    return SimpleNamespace(sync_order=sync_order, persist=persist)


def test_below_ma5_helper():
    trader = AStockFearStrategyTrader()
    assert trader._below_ma5_at(_bars(2.0), SIGNAL_DATE) is True
    assert trader._below_ma5_at(_bars(4.0), SIGNAL_DATE) is False
    # 信号日没有数据 → None（视为未确认）
    assert trader._below_ma5_at(_bars(2.0), date(2026, 9, 14)) is None


def test_sell_ma5_off_sells_on_greed_day():
    result = _run(_config(), fear=80.0, last_close=4.0)
    assert result.sync_order.await_args.args[2] == "SELL"


def test_sell_ma5_all_waits_when_price_above_ma5():
    result = _run(_config(sell_ma5_confirm=SELL_MA5_CONFIRM_ALL), fear=80.0, last_close=4.0)
    assert not result.sync_order.await_count
    assert "等待跌破5日均线" in result.persist.call_args.kwargs["run_message"]
    # 卖出信号被挂起，写回状态
    assert result.persist.call_args.kwargs["state_values"].pending_sell_signal_date == SIGNAL_DATE


def test_sell_ma5_all_sells_once_price_breaks_ma5():
    result = _run(_config(sell_ma5_confirm=SELL_MA5_CONFIRM_ALL), fear=80.0, last_close=2.0)
    assert result.sync_order.await_args.args[2] == "SELL"
    assert result.persist.call_args.kwargs["state_values"].pending_sell_signal_date is None


def test_pending_sell_survives_fear_falling_back():
    """昨天已挂起卖出信号，今天贪恐回落但已跌破 MA5 → 照卖。"""
    state_row = SimpleNamespace(
        last_processed_date=date(2026, 9, 10), cooldown_remaining_days=0, greed_peak_price=None,
        take_profit_cycle_sell_count=0, pending_sell_signal_date=date(2026, 9, 10),
    )
    result = _run(
        _config(sell_ma5_confirm=SELL_MA5_CONFIRM_ALL), fear=50.0, last_close=2.0, state_row=state_row,
    )
    assert result.sync_order.await_args.args[2] == "SELL"


def test_non_main_mode_keeps_main_selling_immediately():
    # 持有的是主标的，non_main 模式下不等 MA5
    result = _run(_config(sell_ma5_confirm=SELL_MA5_CONFIRM_NON_MAIN), fear=80.0, last_close=4.0)
    assert result.sync_order.await_args.args[2] == "SELL"

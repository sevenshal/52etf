import asyncio
from contextlib import contextmanager
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest
from sqlalchemy import create_engine, text

import src.core.database as database
from src.app.api.a_stock_fear_strategy import AStockFearStrategyConfigPayload
from src.core.services.a_stock_index_valuation import valuation_buy_allowed, valuation_sell_allowed
from src.robot.a_stock_fear_strategy_trader import SHANGHAI_TZ, AStockFearStrategyTrader

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
        volume_signal_symbol=None, sub_symbol=None, sub2_symbol=None, swap_threshold=None,
        buy_threshold=35.0, greed_threshold=70.0, volume_ratio_threshold=1.6, volume_z_threshold=None,
        sell_shrink_z=-1.0, trailing_stop_pct=0.0, buy_position_pct=100.0, sell_position_pct=100.0,
        cooldown_days=0, max_take_profit_sells_per_cycle=2, min_position_pct_after_take_profit=0.0,
        rebalance_threshold_pct=0.0, sell_reduction_basis="holdings", sell_price_above_avg_cost=False,
        external_trading_account_id=1, live_sub_account_id=1,
        valuation_window=252, valuation_buy_min=None, valuation_sell_max=20.0, valuation_force_sell_greed=90.0,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _bars():
    dates = pd.bdate_range(end=SIGNAL_DATE, periods=30).date
    return pd.DataFrame({
        "trade_date": dates, "open": [3.0] * 30, "high": [3.0] * 30, "low": [3.0] * 30,
        "close": [3.0] * 30, "volume": [100.0] * 30,
    })


def _holding_main_snapshot():
    return SimpleNamespace(
        shares=1000, available_shares=1000, avg_cost=2.5,
        sub_shares=0, sub_available_shares=0, sub_avg_cost=0.0,
        sub2_shares=0, sub2_available_shares=0, sub2_avg_cost=0.0,
        available_cash=0.0, portfolio_value=3000.0, has_today_order=False,
        external_trading_account_id=1, live_sub_account_id=1,
    )


def _run(config, fear, positions):
    trader = AStockFearStrategyTrader()
    valuation_loader = MagicMock(return_value=positions)
    with patch(f"{TRADER_MODULE}.get_db_ctx", _empty_db_ctx), \
            patch(f"{TRADER_MODULE}._is_china_trading_day", lambda day: day.weekday() < 5), \
            patch(f"{TRADER_MODULE}.get_realtime_price_details", AsyncMock(return_value={"510880.SH": {"price": 3.0}})), \
            patch(f"{TRADER_MODULE}.publish_event"), \
            patch(f"{TRADER_MODULE}.send_alert_email"), \
            patch(f"{TRADER_MODULE}.load_index_valuation_position_history", valuation_loader), \
            patch.object(trader, "_china_now", return_value=TODAY), \
            patch.object(trader, "_fetch_fear_map", return_value={SIGNAL_DATE: fear}), \
            patch.object(trader, "_fetch_etf_bars", return_value=_bars()), \
            patch.object(trader, "_build_snapshot", AsyncMock(return_value=_holding_main_snapshot())) as snapshot, \
            patch.object(trader, "_sync_target_order", AsyncMock(return_value="order-1")) as sync_order, \
            patch.object(trader, "_persist_run_result") as persist, \
            patch.object(trader, "_send_rebalance_notification"), \
            patch.object(trader, "_append_error_log") as error_log:
        asyncio.run(trader.run_config_once(config, trigger_source="manual", ignore_enabled=True))
    assert not error_log.called, error_log.call_args
    return SimpleNamespace(sync_order=sync_order, persist=persist, snapshot=snapshot, valuation_loader=valuation_loader)


def _positions(value):
    return {SIGNAL_DATE: {252: value, 504: value}}


def test_live_sell_waits_until_valuation_is_expensive():
    held = _run(_config(), fear=80.0, positions=_positions(50.0))
    assert not held.sync_order.await_count
    assert "继续持有" in held.persist.call_args.kwargs["run_message"]
    assert "valuation_position_252=main=50.0" in held.persist.call_args.kwargs["message"]

    sold = _run(_config(), fear=80.0, positions=_positions(10.0))
    assert sold.sync_order.await_args.args[2] == "SELL"
    assert sold.sync_order.await_args.kwargs["symbol"] == "510880.SH"


def test_live_force_sell_greed_ignores_valuation():
    result = _run(_config(), fear=92.0, positions=_positions(50.0))
    assert result.sync_order.await_args.args[2] == "SELL"


def test_live_skips_when_signal_day_valuation_not_ready():
    result = _run(_config(), fear=80.0, positions={date(2026, 9, 10): {252: 10.0, 504: 10.0}})
    assert not result.snapshot.await_count
    assert not result.sync_order.await_count
    assert result.persist.call_args.kwargs["status"] == "SKIPPED"
    assert result.persist.call_args.kwargs["run_message"] == "估值数据未就绪"


def test_live_without_valuation_gate_keeps_old_behavior():
    result = _run(_config(valuation_sell_max=None, valuation_force_sell_greed=None), fear=80.0, positions={})
    assert not result.valuation_loader.called
    assert result.sync_order.await_args.args[2] == "SELL"


def test_gate_helpers_pass_when_valuation_missing():
    assert valuation_buy_allowed(None, 80)
    assert valuation_buy_allowed(float("nan"), 80)
    assert not valuation_buy_allowed(50.0, 80)
    assert valuation_sell_allowed(None, 80.0, 20, 90)
    assert not valuation_sell_allowed(50.0, 80.0, 20, 90)
    assert valuation_sell_allowed(50.0, 90.0, 20, 90)
    assert valuation_sell_allowed(50.0, 80.0, None, 90)


def test_config_payload_valuation_fields():
    # 存量配置补列后 valuation_window 可能为 NULL
    payload = AStockFearStrategyConfigPayload(valuation_window=None)
    assert payload.valuation_window == 252
    assert payload.valuation_sell_max is None
    payload = AStockFearStrategyConfigPayload(valuation_window=504, valuation_sell_max=20, valuation_force_sell_greed=90)
    assert (payload.valuation_window, payload.valuation_sell_max, payload.valuation_force_sell_greed) == (504, 20, 90)
    with pytest.raises(Exception):
        AStockFearStrategyConfigPayload(valuation_window=100)
    with pytest.raises(Exception):
        AStockFearStrategyConfigPayload(valuation_sell_max=120)


def test_schema_upgrade_adds_valuation_columns_to_old_table(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE a_stock_fear_strategy_configs (id INTEGER PRIMARY KEY, symbol VARCHAR)"))
        conn.execute(text("INSERT INTO a_stock_fear_strategy_configs (id, symbol) VALUES (1, '510880.SH')"))
    with patch.object(database, "engine", engine):
        database.ensure_a_stock_fear_strategy_schema()
        database.ensure_a_stock_fear_strategy_schema()  # 幂等
    with engine.connect() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(a_stock_fear_strategy_configs)"))}
        row = conn.execute(text(
            "SELECT valuation_window, valuation_buy_min, valuation_sell_max, valuation_force_sell_greed "
            "FROM a_stock_fear_strategy_configs"
        )).one()
    assert {"valuation_window", "valuation_buy_min", "valuation_sell_max", "valuation_force_sell_greed"} <= columns
    # 存量配置：窗口补默认 252，闸门关闭，实盘行为不变
    assert tuple(row) == (252, None, None, None)

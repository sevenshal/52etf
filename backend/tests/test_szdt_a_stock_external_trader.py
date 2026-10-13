import asyncio
from unittest.mock import AsyncMock, patch

from src.robot.szdt_a_stock_external_trader import SZDTAStockExternalTrader


def _stock(code, name):
    return {
        "code": code, "name": name, "type": 3,
        "when_buy": -90, "when_sell": 80, "max_position": 100,
        "buy_amount": 10_000, "sell_amount": 10_000,
        "buy_factor": 1, "sell_factor": 1, "buy_volume_ratio": 0,
        "lever": 1, "emo_area": "a",
    }


def test_a_stock_executor_uses_one_emotion_batch_and_one_target_sync_for_all_signals():
    trader = SZDTAStockExternalTrader()
    stocks = [_stock("SH.510300", "沪深300"), _stock("SZ.159915", "创业板")]
    snapshot = {
        "cash": 50_000, "positions": {}, "targets": {},
        "external_account_id": 9, "sub_account_id": 10,
    }
    emotions = {
        "SH510300": {"status": 1, "data": {"score": -95, "price": 4.0}},
        "SZ159915": {"status": 1, "data": {"score": -96, "price": 2.0}},
    }
    batch_sync = AsyncMock(return_value="批量目标仓位 2 个标的；执行器=SUCCESS")
    cooldowns = []
    logs = []

    with (
        patch.object(trader, "_select_active_stocks", return_value=stocks),
        patch.object(trader, "_load_external_snapshot", AsyncMock(return_value=snapshot)),
        patch("src.robot.szdt_a_stock_external_trader.get_realtime_reference_prices", AsyncMock(return_value={
            "510300.SH": 4.0, "159915.SZ": 2.0,
        })),
        patch.object(trader._szdt_service, "get_fresh_etf_emotions_from_list", AsyncMock(return_value=emotions)) as batch_fetch,
        patch.object(trader, "_sync_target_batch", batch_sync),
        patch.object(trader, "_enabled_stocks", return_value={}),
        patch.object(trader, "_set_cooldowns", lambda *_args: cooldowns.extend(_args[-1])),
        patch.object(trader, "_log", lambda *_args: logs.append(_args)),
    ):
        asyncio.run(trader.run_config_once({
            "id": 1, "account_id": "test", "a_sell_on_ema5_breakdown": False,
            "a_rotation_enabled": False, "a_rotation_threshold": 0,
        }, volume_metrics={}))

    batch_fetch.assert_awaited_once_with(3)
    batch_sync.assert_awaited_once()
    target_updates = batch_sync.await_args.args[2]
    assert set(target_updates) == {"510300.SH", "159915.SZ"}
    assert all(quantity >= 100 for quantity in target_updates.values())
    assert target_updates["510300.SH"] * 4.0 + target_updates["159915.SZ"] * 2.0 <= snapshot["cash"]
    assert len(cooldowns) == 2
    assert len(logs) == 2
    assert all("金额≈" in entry[2] for entry in logs)


def test_a_stock_volume_ratio_miss_does_not_cool_down_or_sync_target():
    trader = SZDTAStockExternalTrader()
    stock = _stock("SH.510300", "沪深300")
    stock["buy_volume_ratio"] = 1.4
    snapshot = {
        "cash": 50_000, "positions": {}, "targets": {},
        "external_account_id": 9, "sub_account_id": 10,
    }
    cooldowns = []
    batch_sync = AsyncMock()
    with (
        patch.object(trader, "_select_active_stocks", return_value=[stock]),
        patch.object(trader, "_load_external_snapshot", AsyncMock(return_value=snapshot)),
        patch("src.robot.szdt_a_stock_external_trader.get_realtime_reference_prices", AsyncMock(return_value={"510300.SH": 4.0})),
        patch.object(trader._szdt_service, "get_fresh_etf_emotions_from_list", AsyncMock(return_value={
            "SH510300": {"status": 1, "data": {"score": -95, "price": 4.0}},
        })),
        patch.object(trader, "_sync_target_batch", batch_sync),
        patch.object(trader, "_enabled_stocks", return_value={}),
        patch.object(trader, "_set_cooldowns", lambda *_args: cooldowns.extend(_args[-1])),
        patch.object(trader, "_log"),
    ):
        asyncio.run(trader.run_config_once({
            "id": 1, "account_id": "test", "a_sell_on_ema5_breakdown": False,
            "a_rotation_enabled": False, "a_rotation_threshold": 0,
        }, volume_metrics={"510300.SH": {"volume_ratio": 1.03}}))

    batch_sync.assert_not_awaited()
    assert cooldowns == []

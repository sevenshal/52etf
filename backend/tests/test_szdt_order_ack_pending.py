"""SZDT 美股策略在 IBKR 回执超时（PENDINGSUBMIT）下的行为。

线上事故背景（2026-10-01 22:43 CST，NAIL）：
`place_market_order` 在 8s 内没等到 orderStatus 回执 → `IBOrderSubmissionPending`
向上抛 → 决策后的 12h 冷却写入被跳过 → 60s 后同一只标的被再次选中并重复下单，
同一只票当天买了 2 × 29 股（实际两笔都成交），而应用日志里只留下第 2 笔。

这里锁定两件事：
1. 回执超时按「已提交」记录，不再冒泡成整轮失败（不发告警邮件）；
2. 无论下单结果如何，冷却都必须写入，下一轮不能再重复下单。
"""

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from src.core.database import StockCooldown, SzdtTradeStock, TradingLog, get_db_ctx
from src.core.services.ib_service import IBOrderSubmissionPending, IBKRService
from src.robot import szdt_us_trader
from src.robot.szdt_us_trader import SZDTUSTrader

PRICE = 26.27
ACCOUNT_ID = "szdt-ack-pending-account"

STOCK_ROW = {
    "code": "US.NAIL",
    "name": "三倍地产",
    "type": 1,
    "when_buy": -90,
    "when_sell": 60,
    "max_position": 10,
    "buy_amount": 777,
    "sell_amount": 777,
    "buy_factor": 10,
    "sell_factor": 10,
    "lever": 3,
    "emo_area": "",
}


def _account_ib(position_qty=0.0):
    values = [
        SimpleNamespace(tag="NetLiquidation", value="50000"),
        SimpleNamespace(tag="AvailableFunds", value="50000"),
        SimpleNamespace(tag="TotalCashValue", value="50000"),
    ]
    positions = []
    if position_qty:
        positions.append(
            SimpleNamespace(
                contract=SimpleNamespace(symbol="NAIL"),
                position=position_qty,
                marketPrice=PRICE,
                avgCost=PRICE,
            )
        )
    return SimpleNamespace(
        isConnected=lambda: True,
        accountValues=lambda: values,
        positions=lambda: positions,
        disconnect=lambda: None,
    )


def _service():
    service = IBKRService(port=4001, client_id=2001)
    service.ib = _account_ib()
    return service


def _pending_ack(symbol, order_id=20, status="PENDINGSUBMIT", timeout=8.0):
    return IBOrderSubmissionPending(
        symbol,
        timeout,
        status,
        trade=SimpleNamespace(order=SimpleNamespace(orderId=order_id)),
    )


def _fake_db_ctx(added):
    """只登记写入行、不落库的假事务（纯单测用）。"""

    @contextmanager
    def ctx():
        yield SimpleNamespace(add=added.append)

    return ctx


def _recording_db_ctx(added):
    """真实事务，同时登记 add() 过来的行（需要走真实冷却过滤时用）。"""

    @contextmanager
    def ctx():
        with get_db_ctx() as db:
            original_add = db.add

            def recording_add(row):
                added.append(row)
                return original_add(row)

            db.add = recording_add
            yield db

    return ctx


def _cooldowns(added):
    return [row for row in added if isinstance(row, StockCooldown)]


def _warnings(added):
    return [row for row in added if isinstance(row, TradingLog) and row.level == "WARNING"]


def _insert_stock(account_id):
    with get_db_ctx() as db:
        db.query(SzdtTradeStock).filter(SzdtTradeStock.account_id == account_id).delete()
        db.query(StockCooldown).filter(StockCooldown.account_id == account_id).delete()
        db.add(SzdtTradeStock(account_id=account_id, **STOCK_ROW))


class SZDTOrderAckPendingTest(TestCase):
    def _run_once(self, service, *, score, use_real_stock_selection=False, account_id=ACCOUNT_ID):
        """跑一轮 run_once，返回（写入的 DB 行, 告警邮件列表）。"""
        trader = SZDTUSTrader()
        emotion = {"status": 1, "data": {"score": score, "price": PRICE}}
        added = []
        alerts = []

        async def fake_connect(_self, _port, _client_id):
            return service

        patches = [
            patch.object(szdt_us_trader, "send_alert_email", lambda *args, **kwargs: alerts.append(args)),
            patch.object(SZDTUSTrader, "_ensure_ib_connected", fake_connect),
            patch.object(trader.szdt, "get_fresh_emotion_from_list", AsyncMock(return_value=emotion)),
        ]
        if use_real_stock_selection:
            # 走真实选股（真实 DB 冷却过滤）时，get_db_ctx 必须保持真实、可重复进入。
            patches.append(patch.object(szdt_us_trader, "get_db_ctx", _recording_db_ctx(added)))
        else:
            patches.append(patch.object(szdt_us_trader, "get_db_ctx", _fake_db_ctx(added)))
            patches.append(patch.object(SZDTUSTrader, "_get_next_stock", return_value=dict(STOCK_ROW)))

        for item in patches:
            item.start()
        try:
            asyncio.run(trader.run_once(SimpleNamespace(id=1, account_id=account_id), 4001))
        finally:
            for item in reversed(patches):
                item.stop()
        return added, alerts


class SZDTOrderAckPendingUnitTest(SZDTOrderAckPendingTest):
    def test_buy_ack_timeout_is_recorded_as_submitted(self):
        service = _service()
        placed = []

        async def fake_place(symbol, action, quantity):
            placed.append((symbol, action, quantity))
            raise _pending_ack(symbol, order_id=20)

        service.place_market_order = fake_place

        added, alerts = self._run_once(service, score=-90)

        self.assertEqual([("US.NAIL", "BUY", 29)], placed)
        self.assertEqual([], alerts, "回执超时不应作为整轮失败发告警邮件")
        self.assertIn("决策后冷却12h", [row.reason for row in _cooldowns(added)],
                      "回执超时也必须写入冷却，否则下一轮会重复下单")
        warnings = _warnings(added)
        self.assertEqual(1, len(warnings))
        self.assertIn("回执超时", warnings[0].message)
        self.assertIn("oid=20", warnings[0].message)

    def test_sell_ack_timeout_is_recorded_as_submitted(self):
        service = _service()
        service.ib = _account_ib(position_qty=100.0)
        placed = []

        async def fake_place(symbol, action, quantity):
            placed.append((symbol, action, quantity))
            raise _pending_ack(symbol, order_id=31)

        service.place_market_order = fake_place

        added, alerts = self._run_once(service, score=90)

        self.assertEqual("SELL", placed[0][1])
        self.assertEqual([], alerts)
        self.assertIn("决策后冷却1h", [row.reason for row in _cooldowns(added)])
        self.assertIn("SELL", _warnings(added)[0].message)

    def test_other_errors_still_alert_and_still_cool_down(self):
        """真实异常（如合约无法识别）仍要告警，但冷却同样不能漏写。"""
        service = _service()

        async def fake_place(symbol, action, quantity):
            raise ValueError(f"Unknown IB contract for {symbol}")

        service.place_market_order = fake_place

        added, alerts = self._run_once(service, score=-90)

        self.assertEqual(1, len(alerts))
        self.assertIn("决策后冷却12h", [row.reason for row in _cooldowns(added)])


class SZDTOrderAckPendingDuplicateGuardTest(SZDTOrderAckPendingTest):
    """复现线上事故：回执超时后下一轮不能再买一次（真实选股 + 真实冷却过滤）。"""

    def setUp(self):
        _insert_stock(ACCOUNT_ID)

    def test_second_tick_does_not_repeat_the_order(self):
        service = _service()
        placed = []

        async def fake_place(symbol, action, quantity):
            placed.append((symbol, action, quantity))
            raise _pending_ack(symbol, order_id=20)

        service.place_market_order = fake_place

        self._run_once(service, score=-90, use_real_stock_selection=True)
        self._run_once(service, score=-90, use_real_stock_selection=True)

        self.assertEqual([("US.NAIL", "BUY", 29)], placed, "冷却生效后第二轮不应再下单")

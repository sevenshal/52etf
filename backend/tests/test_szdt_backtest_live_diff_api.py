"""A股回测接口把「实盘独有」持仓也返回给前端（不依赖真实数据库/账本）。"""

import asyncio
from types import SimpleNamespace

import pytest

# 接口模块会间接触达 czsc（分钟数据），本地/CI 未安装时保持跳过而不是收集失败。
pytest.importorskip("czsc")

import src.app.api.szdt as szdt_api
import src.core.services.external_trading_valuation as valuation_module
import src.robot.szdt_a_stock_external_trader as a_stock_trader_module


class _FakeQuery:
    def __init__(self, rows, first=None):
        self._rows = rows
        self._first = first

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._first


class _FakeDb:
    def __init__(self, stock, name_rows, config):
        self._stock = stock
        self._name_rows = name_rows
        self._config = config
        self.closed = False

    def query(self, *entities):
        # 两列查询 = 名称映射；整行查询 = 配置行 / 策略配置。
        if len(entities) > 1:
            return _FakeQuery(self._name_rows)
        if entities[0] is szdt_api.SzdtTradeStock:
            return _FakeQuery([self._stock])
        if entities[0] is szdt_api.SZDTTradingConfig:
            return _FakeQuery([], first=self._config)
        return _FakeQuery([])

    def close(self):
        self.closed = True


def _stock(code, name):
    return SimpleNamespace(
        code=code, name=name, enabled=True, when_buy=-60, when_sell=60, max_position=100,
        buy_amount=10_000, sell_amount=10_000, buy_factor=1, sell_factor=1, buy_volume_ratio=0,
    )


def _config():
    return SimpleNamespace(
        id=7, account_id="acc", external_trading_account_id=11, live_sub_account_id=22,
        a_sell_on_ema5_breakdown=False, a_rotation_enabled=False, a_rotation_threshold=-30,
        enabled_a=True, ib_account_id=None,
    )


def _run(monkeypatch, latest_positions, live_positions):
    db = _FakeDb(
        stock=_stock("SH.510300", "300ETF"),
        name_rows=[("SH.510300", "300ETF"), ("SZ.159915", "创业板ETF")],
        config=_config(),
    )

    async def fake_backtest(*args, **kwargs):
        return {"latest_positions": latest_positions, "curve": [], "trades": []}

    class FakeTrader:
        async def _load_external_snapshot(self, config):
            return {"positions": live_positions, "targets": {}, "cash": 0.0}

    async def fake_prices(*args, **kwargs):
        return {"159915.SZ": 2.5}

    monkeypatch.setattr(szdt_api, "run_szdt_a_stock_backtest", fake_backtest)
    monkeypatch.setattr(a_stock_trader_module, "SZDTAStockExternalTrader", FakeTrader)
    monkeypatch.setattr(valuation_module, "get_realtime_reference_prices", fake_prices)

    payload = szdt_api.AStockSzdtBacktestRequest(initial_capital=1_000_000)
    return asyncio.run(szdt_api.run_a_stock_backtest(payload, account_id="acc", db=db))


def test_backtest_api_returns_live_only_positions(monkeypatch):
    result = _run(
        monkeypatch,
        latest_positions=[{
            "code": "SH.510300", "name": "300ETF", "quantity": 1000, "price": 4.0,
            "score": -30, "market_value": 4000.0, "position_pct": 10.0,
        }],
        live_positions={"510300.SH": 600, "159915.SZ": 2000},
    )

    positions = result["latest_positions"]
    assert [row["code"] for row in positions] == ["SH.510300", "SZ.159915"]
    assert positions[0]["live_quantity"] == 600
    assert positions[0]["live_quantity_diff"] == 400
    assert positions[0]["live_only"] is False
    live_only = positions[1]
    assert live_only["live_only"] is True
    assert live_only["name"] == "创业板ETF"
    assert live_only["quantity"] == 0
    assert live_only["live_quantity"] == 2000
    assert live_only["live_quantity_diff"] == -2000
    assert live_only["price"] == 2.5
    assert live_only["position_pct"] is None
    assert live_only["market_value"] is None


def test_backtest_api_returns_live_positions_when_backtest_is_empty(monkeypatch):
    result = _run(monkeypatch, latest_positions=[], live_positions={"159915.SZ": 300})

    positions = result["latest_positions"]
    assert len(positions) == 1
    assert positions[0]["code"] == "SZ.159915"
    assert positions[0]["live_only"] is True
    assert positions[0]["live_quantity_diff"] == -300


class _FakeSyncTrader:
    """记录 _sync_target_batch 收到的目标仓位，避免真实下单。"""

    snapshot = {}
    target_updates = {}

    async def _load_external_snapshot(self, config):
        return {"positions": dict(type(self).snapshot), "targets": {}, "cash": 0.0}

    async def _sync_target_batch(self, config, snapshot, target_updates, prices, reason):
        type(self).target_updates = dict(target_updates)
        return "ok"


def _sync(monkeypatch, positions, live_positions):
    db = _FakeDb(stock=_stock("SH.510300", "300ETF"), name_rows=[], config=_config())
    _FakeSyncTrader.snapshot = live_positions
    _FakeSyncTrader.target_updates = {}
    monkeypatch.setattr(a_stock_trader_module, "SZDTAStockExternalTrader", _FakeSyncTrader)
    payload = szdt_api.BacktestPositionSyncRequest(market="a", positions=positions)
    return asyncio.run(szdt_api.sync_backtest_positions(payload, account_id="acc", db=db))


def test_sync_can_clear_live_only_position_with_zero(monkeypatch):
    result = _sync(
        monkeypatch,
        positions=[szdt_api.BacktestPosition(code="SZ.159915", name="创业板ETF", quantity=0, price=0)],
        live_positions={"159915.SZ": 2000},
    )

    assert _FakeSyncTrader.target_updates == {"159915.SZ": 0}
    assert "1" in result["message"]


def test_sync_rejects_non_zero_target_for_unconfigured_code(monkeypatch):
    with pytest.raises(szdt_api.HTTPException) as excinfo:
        _sync(
            monkeypatch,
            positions=[szdt_api.BacktestPosition(code="SZ.159915", name="创业板ETF", quantity=100, price=0)],
            live_positions={"159915.SZ": 2000},
        )

    assert excinfo.value.status_code == 400
    assert "只能按 0 股清仓" in excinfo.value.detail


def test_sync_rejects_clearing_code_not_held_live(monkeypatch):
    with pytest.raises(szdt_api.HTTPException) as excinfo:
        _sync(
            monkeypatch,
            positions=[szdt_api.BacktestPosition(code="SZ.159915", name="创业板ETF", quantity=0, price=0)],
            live_positions={"510300.SH": 500},
        )

    assert excinfo.value.status_code == 400
    assert "不在实盘持仓中" in excinfo.value.detail

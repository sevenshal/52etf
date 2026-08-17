"""轻量虚拟子账户列表接口测试。

GET /api/external-trading-accounts/{external_account_id}/sub-accounts/options
只返回下拉选择所需的最小字段，不计算净值、不拉实时行情、不查持仓/手续费。
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.app.api import external_trading_accounts as api
from src.app.api.external_trading_accounts import router
from src.core.database import (
    Base,
    FactorLiveTradingConfig,
    PortfolioCopyConfig,
    SnowballCopyConfig,
    SoxlFearStrategyConfig,
    ValuationSimConfig,
)
from src.core.external_trading_database import (
    ExternalTradingAccount,
    ExternalTradingBase,
    ExternalTradingSubAccount,
)


def _make_app(main_session, ext_session):
    app = FastAPI()
    app.include_router(router)

    def _main_db():
        try:
            yield main_session
        finally:
            pass

    def _ext_db():
        try:
            yield ext_session
        finally:
            pass

    app.dependency_overrides[api.get_db] = _main_db
    app.dependency_overrides[api.get_external_trading_db] = _ext_db
    app.dependency_overrides[api.valid_admin_account] = lambda: "acct"
    return TestClient(app)


def _memory_engine():
    # TestClient 在独立线程跑请求，内存 SQLite 必须共享同一连接
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def _build_fixture():
    main_engine = _memory_engine()
    Base.metadata.create_all(
        main_engine,
        tables=[
            SnowballCopyConfig.__table__,
            PortfolioCopyConfig.__table__,
            FactorLiveTradingConfig.__table__,
            SoxlFearStrategyConfig.__table__,
            ValuationSimConfig.__table__,
        ],
    )
    main_session = sessionmaker(bind=main_engine, expire_on_commit=False)()

    ext_engine = _memory_engine()
    ExternalTradingBase.metadata.create_all(ext_engine)
    ext_session = sessionmaker(bind=ext_engine, expire_on_commit=False)()

    ext_session.add(ExternalTradingAccount(
        id=1,
        account_id="acct",
        name="Broker",
        identifier="broker",
        market_type="US_STOCK",
        enabled=True,
    ))

    def add_sub_account(id_, name, **kwargs):
        defaults = {
            "account_id": "acct",
            "external_trading_account_id": 1,
            "enabled": True,
            "cash_allocated": 100000.0,
            "cash_available": 50000.0,
        }
        defaults.update(kwargs)
        ext_session.add(ExternalTradingSubAccount(id=id_, name=name, **defaults))

    return main_session, ext_session, add_sub_account


def _commit_all(*sessions):
    for session in sessions:
        session.commit()


def test_options_empty_list():
    main_session, ext_session, _add = _build_fixture()
    _commit_all(main_session, ext_session)
    client = _make_app(main_session, ext_session)
    resp = client.get("/api/external-trading-accounts/1/sub-accounts/options")
    assert resp.status_code == 200
    assert resp.json() == []


def test_options_basic_fields_without_heavy_payload():
    main_session, ext_session, add_sub_account = _build_fixture()
    add_sub_account(11, "策略A")
    add_sub_account(12, "策略B", enabled=False, cash_allocated=200.0, cash_available=88.0)
    _commit_all(main_session, ext_session)
    client = _make_app(main_session, ext_session)

    resp = client.get("/api/external-trading-accounts/1/sub-accounts/options")
    assert resp.status_code == 200
    rows = resp.json()
    assert [row["id"] for row in rows] == [12, 11]  # 按 id desc

    row = next(r for r in rows if r["id"] == 12)
    assert row == {
        "id": 12,
        "name": "策略B",
        "enabled": False,
        "strategy_type": None,
        "strategy_config_id": None,
        "strategy_name": None,
        "binding_status": "FREE",
        "binding_label": "空闲",
        "cash_allocated": 200.0,
        "cash_available": 88.0,
    }

    # 轻量接口绝不允许出现重计算字段
    for r in rows:
        for heavy_key in (
            "net_asset",
            "position_market_value",
            "valuation",
            "positions",
            "trade_fee_summary",
            "effective_executor_policy",
            "position_count",
            "cumulative_trade_fee_total",
        ):
            assert heavy_key not in r


def test_options_resolves_binding_names_batched():
    main_session, ext_session, add_sub_account = _build_fixture()
    add_sub_account(11, "雪球跟单子账户",
                    strategy_type="snowball_copy_live", strategy_config_id=1001)
    add_sub_account(12, "组合跟单子账户",
                    strategy_type="portfolio_copy_live", strategy_config_id=2002)
    add_sub_account(13, "因子实盘子账户",
                    strategy_type="factor_live_trading", strategy_config_id=3003)
    add_sub_account(14, "SOXL子账户",
                    strategy_type="soxl_fear_strategy", strategy_config_id=4004)
    add_sub_account(15, "估值模拟子账户",
                    strategy_type="valuation_sim", strategy_config_id=5005)
    add_sub_account(16, "孤儿子账户",
                    strategy_type="snowball_copy_live", strategy_config_id=99999)
    add_sub_account(17, "空闲子账户")

    main_session.add(SnowballCopyConfig(
        id=1001, account_id="acct", cli_id="cli-1", combination_id="ZH1001",
        combination_name="雪球组合一号",
    ))
    main_session.add(PortfolioCopyConfig(
        id=2002, account_id="acct", portfolio_id="P2002", portfolio_name="美股组合二号",
    ))
    main_session.add(FactorLiveTradingConfig(
        id=3003, account_id="acct", name="因子实盘-小市值", request_payload={},
    ))
    main_session.add(SoxlFearStrategyConfig(
        id=4004, account_id="acct", symbol="SOXL.US",
    ))
    main_session.add(ValuationSimConfig(
        id=5005, account_id="acct", name="估值模拟-纳指",
    ))
    # 其它账号下的同名配置不应干扰
    main_session.add(SnowballCopyConfig(
        id=1002, account_id="other", cli_id="cli-2", combination_id="ZH9999",
        combination_name="别人的组合",
    ))
    _commit_all(main_session, ext_session)
    client = _make_app(main_session, ext_session)

    resp = client.get("/api/external-trading-accounts/1/sub-accounts/options")
    assert resp.status_code == 200
    by_id = {row["id"]: row for row in resp.json()}

    assert by_id[11]["strategy_name"] == "雪球组合一号"
    assert by_id[11]["binding_status"] == "BOUND"
    assert by_id[11]["binding_label"] == "雪球组合一号"

    assert by_id[12]["strategy_name"] == "美股组合二号"
    assert by_id[13]["strategy_name"] == "因子实盘-小市值"
    assert by_id[14]["strategy_name"] == "SOXL情绪量能自动交易 SOXL.US"
    assert by_id[15]["strategy_name"] == "估值模拟-纳指"

    # 配置已删除：显示兜底文案
    assert by_id[16]["strategy_name"] == "A股雪球跟单（配置已删除）"
    assert by_id[16]["binding_status"] == "BOUND"

    # 空闲子账户
    assert by_id[17]["strategy_name"] is None
    assert by_id[17]["binding_status"] == "FREE"
    assert by_id[17]["binding_label"] == "空闲"

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
from pydantic import BaseModel

from ...core.database import get_db, PortfolioCopyConfig, PortfolioCopyLog
from ...core.external_trading_database import (
    ExternalTradingAccount,
    ExternalTradingSubAccount,
    ExternalTradingTargetPosition,
    get_external_trading_db,
)
from ...core.services.external_trading_ledger import STRATEGY_PORTFOLIO_COPY
from ...core.services.external_trading_market import (
    EXTERNAL_TRADING_MARKET_US_STOCK,
    normalize_external_trading_market_type,
)
from .account import valid_account

router = APIRouter(prefix="/api/ib-copy-trading", tags=["ib-copy-trading"])

SUPPORTED_PORTFOLIO_COPY_PLATFORMS = {"futu", "star_wealth", "yingli"}
SUPPORTED_ACCOUNT_TYPES = {"ib", "longport", "external"}

class PortfolioCopyConfigSchema(BaseModel):
    id: Optional[int] = None
    account_id: Optional[str] = None
    enabled: bool = False
    portfolio_id: Optional[str] = None
    portfolio_name: Optional[str] = None
    cron_rule: str = "0 8 * * *"
    timezone: str = "America/New_York"
    ib_port: Optional[int] = None # Now optional
    ib_account_id: Optional[int] = None # Added
    total_position_ratio: Optional[float] = 100.0
    total_amount: Optional[float] = None
    tracking_error_pct: Optional[float] = 5.0
    api_headers: Optional[dict] = None
    account_type: Optional[str] = "ib" 
    longport_account_id: Optional[str] = None
    external_trading_account_id: Optional[int] = None
    live_sub_account_id: Optional[int] = None
    platform: Optional[str] = "futu"
    external_trading_account_name: Optional[str] = None
    live_sub_account_name: Optional[str] = None
    live_sub_account_enabled: Optional[bool] = None
    last_external_sync_at: Optional[datetime] = None
    last_external_sync_status: Optional[str] = None
    last_external_sync_message: Optional[str] = None

    class Config:
        from_attributes = True

class PortfolioCopyLogSchema(BaseModel):
    id: int
    config_id: Optional[int] = None
    account_id: str
    timestamp: datetime
    portfolio_id: Optional[str] = None
    action: str
    symbol: Optional[str] = None
    quantity: Optional[float] = None
    price: Optional[float] = None
    status: str
    message: Optional[str] = None

    class Config:
        from_attributes = True

def _portfolio_copy_config_response(
    config: PortfolioCopyConfig,
    trading_db: Session,
) -> PortfolioCopyConfigSchema:
    resp = PortfolioCopyConfigSchema.from_orm(config)
    if getattr(config, "external_trading_account_id", None):
        account = trading_db.query(ExternalTradingAccount).filter(
            ExternalTradingAccount.id == config.external_trading_account_id,
            ExternalTradingAccount.account_id == config.account_id,
        ).first()
        if account:
            resp.external_trading_account_name = f"{account.name} ({account.identifier})"
    if getattr(config, "live_sub_account_id", None):
        sub_account = trading_db.query(ExternalTradingSubAccount).filter(
            ExternalTradingSubAccount.id == config.live_sub_account_id,
            ExternalTradingSubAccount.account_id == config.account_id,
        ).first()
        if sub_account:
            resp.live_sub_account_name = sub_account.name
            resp.live_sub_account_enabled = sub_account.enabled
    return resp


def _validate_portfolio_external_account_selection(
    db: Session,
    account_id: str,
    external_account_id: Optional[int],
):
    if not external_account_id:
        return None
    account = db.query(ExternalTradingAccount).filter(
        ExternalTradingAccount.id == external_account_id,
        ExternalTradingAccount.account_id == account_id,
    ).first()
    if not account:
        raise HTTPException(status_code=400, detail="所选外部交易账户不存在")
    if not account.enabled:
        raise HTTPException(status_code=400, detail="所选外部交易账户未启用")
    if normalize_external_trading_market_type(account.market_type) != EXTERNAL_TRADING_MARKET_US_STOCK:
        raise HTTPException(status_code=400, detail="美股跟单只能选择美股外部交易账户")
    return account


def _get_valid_portfolio_live_sub_account_selection(
    db: Session,
    account_id: str,
    external_account_id: Optional[int],
    sub_account_id: Optional[int],
    *,
    config_id: Optional[int] = None,
    require_enabled: bool = False,
) -> Optional[ExternalTradingSubAccount]:
    if not sub_account_id:
        return None
    if not external_account_id:
        raise HTTPException(status_code=400, detail="选择虚拟子账户前请先选择外部交易账户")
    sub_account = db.query(ExternalTradingSubAccount).filter(
        ExternalTradingSubAccount.id == sub_account_id,
        ExternalTradingSubAccount.account_id == account_id,
        ExternalTradingSubAccount.external_trading_account_id == external_account_id,
    ).first()
    if not sub_account:
        raise HTTPException(status_code=400, detail="所选虚拟子账户不存在")
    if require_enabled and not sub_account.enabled:
        raise HTTPException(status_code=400, detail="所选虚拟子账户未启用")
    is_bound = bool(sub_account.strategy_type or sub_account.strategy_config_id)
    is_current_binding = (
        sub_account.strategy_type == STRATEGY_PORTFOLIO_COPY
        and config_id
        and sub_account.strategy_config_id == config_id
    )
    if is_bound and not is_current_binding:
        raise HTTPException(status_code=400, detail="所选虚拟子账户已被其他策略绑定")
    return sub_account


def _deactivate_portfolio_target_positions(
    db: Session,
    *,
    sub_account_id: Optional[int],
    config_id: Optional[int],
) -> None:
    if not sub_account_id:
        return
    query = db.query(ExternalTradingTargetPosition).filter(
        ExternalTradingTargetPosition.sub_account_id == sub_account_id,
        ExternalTradingTargetPosition.status == "ACTIVE",
    )
    if config_id:
        query = query.filter(
            ExternalTradingTargetPosition.strategy_type == STRATEGY_PORTFOLIO_COPY,
            ExternalTradingTargetPosition.strategy_config_id == config_id,
        )
    now = datetime.now()
    for row in query.all():
        row.status = "PREVIEW"
        row.updated_at = now


def _sync_portfolio_live_sub_account_binding(
    db: Session,
    config: PortfolioCopyConfig,
    *,
    previous_sub_account_id: Optional[int],
) -> None:
    if config.account_type == "external":
        if not config.external_trading_account_id:
            raise HTTPException(status_code=400, detail="使用外部交易账户时必须选择外部交易账户")
        if not config.live_sub_account_id:
            raise HTTPException(status_code=400, detail="使用外部交易账户时必须选择虚拟子账户")
    else:
        config.external_trading_account_id = None
        config.live_sub_account_id = None

    _validate_portfolio_external_account_selection(db, config.account_id, config.external_trading_account_id)
    selected_sub_account = _get_valid_portfolio_live_sub_account_selection(
        db,
        config.account_id,
        config.external_trading_account_id,
        config.live_sub_account_id,
        config_id=config.id,
        require_enabled=bool(config.live_sub_account_id),
    )

    if previous_sub_account_id and previous_sub_account_id != config.live_sub_account_id:
        _deactivate_portfolio_target_positions(
            db,
            sub_account_id=previous_sub_account_id,
            config_id=config.id,
        )
        previous = db.query(ExternalTradingSubAccount).filter(
            ExternalTradingSubAccount.id == previous_sub_account_id,
            ExternalTradingSubAccount.account_id == config.account_id,
            ExternalTradingSubAccount.strategy_type == STRATEGY_PORTFOLIO_COPY,
            ExternalTradingSubAccount.strategy_config_id == config.id,
        ).first()
        if previous:
            previous.strategy_type = None
            previous.strategy_config_id = None
            previous.updated_at = datetime.now()

    if not getattr(config, "enabled", True) or config.account_type != "external":
        _deactivate_portfolio_target_positions(
            db,
            sub_account_id=config.live_sub_account_id or previous_sub_account_id,
            config_id=config.id,
        )

    if selected_sub_account:
        selected_sub_account.strategy_type = STRATEGY_PORTFOLIO_COPY
        selected_sub_account.strategy_config_id = config.id
        selected_sub_account.updated_at = datetime.now()


@router.get("/configs", response_model=List[PortfolioCopyConfigSchema])
async def list_configs(
    db: Session = Depends(get_db),
    trading_db: Session = Depends(get_external_trading_db),
    account_id: str = Depends(valid_account)
):
    configs = db.query(PortfolioCopyConfig).filter(PortfolioCopyConfig.account_id == account_id).all()
    return [_portfolio_copy_config_response(config, trading_db) for config in configs]

@router.post("/configs", response_model=PortfolioCopyConfigSchema)
async def save_config(
    config_data: PortfolioCopyConfigSchema,
    db: Session = Depends(get_db),
    trading_db: Session = Depends(get_external_trading_db),
    account_id: str = Depends(valid_account)
):
    platform = config_data.platform or "futu"
    if platform not in SUPPORTED_PORTFOLIO_COPY_PLATFORMS:
        raise HTTPException(status_code=400, detail="不支持的组合来源")
    account_type = config_data.account_type or "ib"
    if account_type not in SUPPORTED_ACCOUNT_TYPES:
        raise HTTPException(status_code=400, detail="不支持的账户类型")

    if config_data.id:
        config = db.query(PortfolioCopyConfig).filter(
            PortfolioCopyConfig.id == config_data.id,
            PortfolioCopyConfig.account_id == account_id
        ).first()
        if not config:
            raise HTTPException(status_code=404, detail="Config not found")
        previous_sub_account_id = getattr(config, "live_sub_account_id", None)
        
        config.enabled = config_data.enabled
        config.portfolio_id = config_data.portfolio_id
        config.portfolio_name = config_data.portfolio_name
        config.cron_rule = config_data.cron_rule
        config.timezone = config_data.timezone
        config.ib_port = config_data.ib_port or 0
        config.ib_account_id = config_data.ib_account_id
        config.total_position_ratio = config_data.total_position_ratio
        config.total_amount = config_data.total_amount
        config.tracking_error_pct = config_data.tracking_error_pct
        config.api_headers = config_data.api_headers
        config.account_type = account_type
        config.longport_account_id = config_data.longport_account_id
        config.external_trading_account_id = config_data.external_trading_account_id
        config.live_sub_account_id = config_data.live_sub_account_id
        config.platform = platform
    else:
        config = PortfolioCopyConfig(
            account_id=account_id,
            enabled=config_data.enabled,
            portfolio_id=config_data.portfolio_id,
            portfolio_name=config_data.portfolio_name,
            cron_rule=config_data.cron_rule,
            timezone=config_data.timezone,
            ib_port=config_data.ib_port or 0,
            ib_account_id=config_data.ib_account_id,
            total_position_ratio=config_data.total_position_ratio,
            total_amount=config_data.total_amount,
            tracking_error_pct=config_data.tracking_error_pct,
            api_headers=config_data.api_headers,
            account_type=account_type,
            longport_account_id=config_data.longport_account_id,
            external_trading_account_id=config_data.external_trading_account_id,
            live_sub_account_id=config_data.live_sub_account_id,
            platform=platform
        )
        db.add(config)
        db.flush()
        previous_sub_account_id = None

    _sync_portfolio_live_sub_account_binding(
        trading_db,
        config,
        previous_sub_account_id=previous_sub_account_id,
    )
    
    trading_db.commit()
    db.commit()
    db.refresh(config)
    return _portfolio_copy_config_response(config, trading_db)

@router.delete("/configs/{config_id}")
async def delete_config(
    config_id: int,
    db: Session = Depends(get_db),
    trading_db: Session = Depends(get_external_trading_db),
    account_id: str = Depends(valid_account)
):
    config = db.query(PortfolioCopyConfig).filter(
        PortfolioCopyConfig.id == config_id,
        PortfolioCopyConfig.account_id == account_id
    ).first()
    if not config:
        raise HTTPException(status_code=404, detail="Config not found")

    if getattr(config, "live_sub_account_id", None):
        _deactivate_portfolio_target_positions(
            trading_db,
            sub_account_id=config.live_sub_account_id,
            config_id=config.id,
        )
        sub_account = trading_db.query(ExternalTradingSubAccount).filter(
            ExternalTradingSubAccount.id == config.live_sub_account_id,
            ExternalTradingSubAccount.account_id == account_id,
            ExternalTradingSubAccount.strategy_type == STRATEGY_PORTFOLIO_COPY,
            ExternalTradingSubAccount.strategy_config_id == config.id,
        ).first()
        if sub_account:
            sub_account.strategy_type = None
            sub_account.strategy_config_id = None
            sub_account.updated_at = datetime.now()
    
    db.delete(config)
    trading_db.commit()
    db.commit()
    return {"message": "Deleted successfully"}

@router.get("/logs", response_model=List[PortfolioCopyLogSchema])
async def list_logs(
    portfolio_id: Optional[str] = None,
    config_id: Optional[int] = None,
    db: Session = Depends(get_db),
    account_id: str = Depends(valid_account)
):
    query = db.query(PortfolioCopyLog)
    # Always filter by authenticated account_id
    query = query.filter(PortfolioCopyLog.account_id == account_id)
    if config_id:
        query = query.filter(PortfolioCopyLog.config_id == config_id)
    return query.order_by(PortfolioCopyLog.timestamp.desc()).limit(100).all()

@router.get("/portfolio-info/{portfolio_id}")
async def get_portfolio_info_proxy(
    portfolio_id: str, 
    platform: str = "futu",
    invest_id: Optional[str] = None,
    authorization: Optional[str] = None
):
    if platform not in SUPPORTED_PORTFOLIO_COPY_PLATFORMS:
        raise HTTPException(status_code=400, detail="不支持的组合来源")

    from ...robot.portfolio_copy_trader import PortfolioCopyTrader
    trader = PortfolioCopyTrader()
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
        }
        if invest_id:
            headers["investId"] = invest_id
        if authorization:
            headers["Authorization"] = authorization
            
        info = await trader.get_portfolio_info(portfolio_id, headers, platform=platform)
        return info
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/configs/{config_id}/preview")
async def preview_rebalance(
    config_id: int,
    db: Session = Depends(get_db),
    account_id: str = Depends(valid_account)
):
    from ...robot.portfolio_copy_trader import PortfolioCopyTrader
    config = db.query(PortfolioCopyConfig).filter(
        PortfolioCopyConfig.id == config_id,
        PortfolioCopyConfig.account_id == account_id
    ).first()
    if not config:
        raise HTTPException(status_code=404, detail="Config not found")
    if (config.platform or "futu") not in SUPPORTED_PORTFOLIO_COPY_PLATFORMS:
        raise HTTPException(status_code=400, detail="不支持的组合来源")
    
    trader = PortfolioCopyTrader()
    try:
        # 使用独特的 client_id (100 + config_id) 避免与后台机器人冲突
        # Offload to worker thread via task queue
        plan = await trader.submit_rebalance_task(config, client_id=100 + config_id)
        return plan
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/configs/{config_id}/sync-external-targets")
async def sync_external_targets(
    config_id: int,
    db: Session = Depends(get_db),
    account_id: str = Depends(valid_account),
):
    from ...robot.portfolio_copy_trader import PortfolioCopyTrader
    config = db.query(PortfolioCopyConfig).filter(
        PortfolioCopyConfig.id == config_id,
        PortfolioCopyConfig.account_id == account_id
    ).first()
    if not config:
        raise HTTPException(status_code=404, detail="Config not found")
    if config.account_type != "external":
        raise HTTPException(status_code=400, detail="该配置未使用外部交易账户")
    if (config.platform or "futu") not in SUPPORTED_PORTFOLIO_COPY_PLATFORMS:
        raise HTTPException(status_code=400, detail="不支持的组合来源")

    trader = PortfolioCopyTrader()
    try:
        return await trader.sync_external_targets(config, trigger_source="manual", trigger_executor=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

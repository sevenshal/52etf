import asyncio
from fastapi import APIRouter, HTTPException, Header, Depends, Query
from pydantic import BaseModel
from typing import Optional, List
from ...core.utils import get_data_file, read_json_file, write_json_file
from datetime import datetime
from sqlalchemy.orm import Session
from ...core.database import StockEVC, stock_tags, get_db, EVCTradeLog, EVCAccountConfig
from sqlalchemy import func, and_
from ...core.services.longport import LongPortService
from ...core.services.evc import EVCService

router = APIRouter(prefix="/api/evc")

class StrategyConfig(BaseModel):
    auto_trading_enabled: bool
    undervalue_threshold: float
    next_fy_growth_threshold: float
    max_hold_stock_count: int
    max_hold_amount_per_stock: int
    current_fy_hi_threshold: float
    next_fy_median_threshold: float

class ValuationSearchRequest(BaseModel):
    undervalue_threshold: float
    next_fy_growth_threshold: float
    symbol: Optional[str] = None

async def get_account_id(x_account_id: Optional[str] = Header(None)) -> str:
    if not x_account_id:
        raise HTTPException(status_code=401, detail="Missing account ID")
    return x_account_id

def get_strategy_config_path(account_id: str) -> str:
    return get_data_file(account_id, "evc_strategy.json")

_default_strategy_config = {
    "auto_trading_enabled": False,
    "undervalue_threshold": 0.9,
    "next_fy_growth_threshold": 1.1,
    "max_hold_stock_count": 20,
    "max_hold_amount_per_stock": 100000,
    "current_fy_hi_threshold": 1.0,
    "next_fy_median_threshold": 1.0
}

@router.get("/config")
async def get_config(
    account_id: str = Depends(get_account_id),
    db: Session = Depends(get_db)
):
    api_config = db.query(EVCAccountConfig).filter(
        EVCAccountConfig.account_id == account_id
    ).first()
    strategy_config = read_json_file(get_strategy_config_path(account_id)) or _default_strategy_config
    return {
        "activated": False,
        "evc_account_configured": bool(api_config and api_config.evc_username and api_config.evc_password),
        "evc_cookie_configured": bool(api_config and api_config.evc_cookie),
        **strategy_config
    }

@router.post("/update-strategy")
async def update_strategy(
    config: StrategyConfig,
    account_id: str = Depends(get_account_id)
):
    try:
        strategy_config = {
            "auto_trading_enabled": config.auto_trading_enabled,
            "undervalue_threshold": config.undervalue_threshold,
            "next_fy_growth_threshold": config.next_fy_growth_threshold,
            "max_hold_stock_count": config.max_hold_stock_count,
            "max_hold_amount_per_stock": config.max_hold_amount_per_stock,
            "current_fy_hi_threshold": config.current_fy_hi_threshold,
            "next_fy_median_threshold": config.next_fy_median_threshold
        }
        
        write_json_file(get_strategy_config_path(account_id), strategy_config)
        return {"message": "更新成功"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/valuation-search")
async def valuation_search(
    request: ValuationSearchRequest,
    account_id: str = Depends(get_account_id),
    db: Session = Depends(get_db)
):
    try:
        latest_date = db.query(func.max(StockEVC.date)).scalar()
        if not latest_date:
            return []

        # 如果提供了股票代码，只按股票代码过滤，不再套标签/低估率/增长率等筛选条件
        if request.symbol:
            symbol = request.symbol.strip().upper()
            if "." not in symbol:
                symbol = f"{symbol}.US"
            query = db.query(StockEVC).filter(
                StockEVC.date == latest_date,
                StockEVC.symbol == symbol
            )
        else:
            tag_ids = ["97638d21-2feb-4e7c-b47f-1984ff71dda6", "fbef4442-9f95-45e6-9859-b95f34889a5e"]

            # 基础查询：按 symbol + date 关联标签，避免跨历史日期产生大量重复行
            query = (
                db.query(StockEVC)
                .join(
                    stock_tags,
                    and_(
                        stock_tags.c.stock_symbol == StockEVC.symbol,
                        stock_tags.c.date == StockEVC.date
                    )
                )
                .filter(
                    StockEVC.date == latest_date,
                    stock_tags.c.tag_id.in_(tag_ids)
                )
                .distinct()
            )

            # 否则使用阈值条件
            query = query.filter(
                (StockEVC.last_price / StockEVC.fair_value_lo < request.undervalue_threshold),
                (StockEVC.forward_next_fy_lo / StockEVC.fair_value_lo > request.next_fy_growth_threshold),
                (StockEVC.forward_next_fy_hi / StockEVC.fair_value_hi > request.next_fy_growth_threshold)
            )

        stocks = query.all()
        
        # 获取所有股票代码（去重保持顺序）
        symbols = list(dict.fromkeys(stock.symbol for stock in stocks))
        
        # 获取股票静态信息
        quote_service = LongPortService.get_instance()
        static_info_list = []
        if symbols:
            static_info_list = await asyncio.to_thread(quote_service.get_static_info, symbols)
        
        # 将静态信息列表转换为以symbol为键的字典，方便查找
        static_info_dict = {}
        for info in static_info_list:
            if 'symbol' in info:
                static_info_dict[info['symbol']] = info
        
        result = []
        for stock in stocks:
            static_info = static_info_dict.get(stock.symbol, {})
            stock_data = {
                "symbol": stock.symbol,
                "company": static_info.get('name_cn') or stock.company,
                "last_price": stock.last_price,
                "fair_value_lo": stock.fair_value_lo,
                "fair_value_hi": stock.fair_value_hi,
                "forward_next_fy_lo": stock.forward_next_fy_lo,
                "forward_next_fy_hi": stock.forward_next_fy_hi,
                "fair_value_date": stock.fair_value_date,
                "pe_ratio": stock.pe_ratio,
                "beta": stock.beta,
                "forward_pe_ratio": stock.forward_pe_ratio,
                "date": stock.date,
                "static_info": static_info
            }
            
            result.append(stock_data)
        
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/trade-logs")
async def get_trade_logs(
    account_id: str = Depends(get_account_id),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db)
):
    try:
        query = db.query(EVCTradeLog).filter(
            EVCTradeLog.account_id == account_id
        ).order_by(EVCTradeLog.timestamp.desc())
        total = query.count()
        logs = query.offset((page - 1) * page_size).limit(page_size).all()
            
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": [
                {
                    "symbol": log.symbol,
                    "quantity": log.quantity,
                    "price": log.price,
                    "reason": log.reason,
                    "operation": log.operation,
                    "timestamp": log.timestamp.isoformat()
                }
                for log in logs
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/stock-evc/history/{symbol}")
def get_stock_evc_history(
    symbol: str,
    limit: int = Query(100, description="查询天数"),
    db: Session = Depends(get_db)
):
    records = (
        db.query(StockEVC)
        .filter(StockEVC.symbol == symbol)
        .order_by(StockEVC.date.desc())
        .limit(limit)
        .all()
    )
    # 去掉 company 和 _sa_instance_state 字段
    result = []
    for r in records:
        d = r.__dict__.copy()
        d.pop("company", None)
        d.pop("_sa_instance_state", None)
        result.append(d)
    return result

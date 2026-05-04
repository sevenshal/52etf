from fastapi import APIRouter, HTTPException, Header, Depends, Query
from pydantic import BaseModel
from typing import Optional, List
from ...core.utils import get_data_file, read_json_file, write_json_file
from datetime import datetime
from sqlalchemy.orm import Session
from ...core.database import StockEVC, StockTag, stock_tags, get_db, EVCTradeLog, EVCAccountConfig
from sqlalchemy import func, and_
from ...core.static_info import get_static_info_snapshot_map
from ...core.services.evc import EVCService

router = APIRouter(prefix="/api/evc")

ONE_HUNDRED_MILLION = 100_000_000

class StrategyConfig(BaseModel):
    auto_trading_enabled: bool
    undervalue_threshold: float
    next_fy_growth_threshold: float
    max_hold_stock_count: int
    max_hold_amount_per_stock: int
    current_fy_hi_threshold: float
    next_fy_median_threshold: float

class ValuationSearchRequest(BaseModel):
    undervalue_threshold: float = 0.9
    next_fy_growth_threshold: float = 1.1
    symbol: Optional[str] = None
    tag_ids: Optional[List[str]] = None
    include_static_info: Optional[bool] = None
    min_market_cap_100m: Optional[float] = None
    max_market_cap_100m: Optional[float] = None

async def get_account_id(x_account_id: Optional[str] = Header(None)) -> str:
    if not x_account_id:
        raise HTTPException(status_code=401, detail="Missing account ID")
    return x_account_id

def _market_cap_threshold_to_usd(value: Optional[float]) -> Optional[float]:
    if value is None or value <= 0:
        return None
    return value * ONE_HUNDRED_MILLION

def _calculate_market_cap(stock: StockEVC, static_info: dict) -> Optional[float]:
    shares = static_info.get("total_shares") if static_info else None
    if not isinstance(stock.last_price, (int, float)) or stock.last_price <= 0:
        return None
    if not isinstance(shares, (int, float)) or shares <= 0:
        return None
    return stock.last_price * shares

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
        search_symbol = request.symbol.strip().upper() if request.symbol else ""
        tag_ids = [tag_id for tag_id in (request.tag_ids or []) if tag_id]
        min_market_cap = _market_cap_threshold_to_usd(request.min_market_cap_100m)
        max_market_cap = _market_cap_threshold_to_usd(request.max_market_cap_100m)
        has_market_cap_filter = bool((min_market_cap or max_market_cap) and not search_symbol)

        # 如果提供了股票代码，只按股票代码查该标的最新记录，不再套标签/日期/低估率/增长率等筛选条件
        if search_symbol:
            symbol = search_symbol
            if "." not in symbol:
                symbol = f"{symbol}.US"
            query = (
                db.query(StockEVC)
                .filter(StockEVC.symbol == symbol)
                .order_by(StockEVC.date.desc())
                .limit(1)
            )
            stocks = query.all()
        else:
            latest_date = db.query(func.max(StockEVC.date)).scalar()
            if not latest_date:
                return []

            query = db.query(StockEVC).filter(StockEVC.date == latest_date)
            if tag_ids:
                query = (
                    query.join(
                        stock_tags,
                        and_(
                            stock_tags.c.stock_symbol == StockEVC.symbol,
                            stock_tags.c.date == StockEVC.date
                        )
                    )
                    .filter(stock_tags.c.tag_id.in_(tag_ids))
                    .distinct()
                )

            # 否则使用阈值条件；标签为空时不做标签过滤
            query = query.filter(
                StockEVC.fair_value_lo > 0,
                StockEVC.fair_value_hi > 0,
                StockEVC.last_price < request.undervalue_threshold * StockEVC.fair_value_lo,
                StockEVC.forward_next_fy_lo > request.next_fy_growth_threshold * StockEVC.fair_value_lo,
                StockEVC.forward_next_fy_hi > request.next_fy_growth_threshold * StockEVC.fair_value_hi
            )
            stocks = query.all()

        # 获取所有股票代码（去重保持顺序）
        symbols = list(dict.fromkeys(stock.symbol for stock in stocks))

        static_info_dict = get_static_info_snapshot_map(db, symbols)
        should_include_static_info = request.include_static_info is not False

        if has_market_cap_filter:
            filtered_stocks = []
            for stock in stocks:
                market_cap = _calculate_market_cap(stock, static_info_dict.get(stock.symbol, {}))
                if market_cap is None:
                    continue
                if min_market_cap is not None and market_cap < min_market_cap:
                    continue
                if max_market_cap is not None and market_cap > max_market_cap:
                    continue
                filtered_stocks.append(stock)
            stocks = filtered_stocks
        
        result = []
        for stock in stocks:
            static_info = static_info_dict.get(stock.symbol, {})
            market_cap = _calculate_market_cap(stock, static_info)
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
                "market_cap": market_cap,
                "market_cap_100m": market_cap / ONE_HUNDRED_MILLION if market_cap is not None else None,
                "static_info": static_info if should_include_static_info else {}
            }
            
            result.append(stock_data)
        
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/tags")
async def get_stock_tags(
    account_id: str = Depends(get_account_id),
    db: Session = Depends(get_db)
):
    try:
        latest_date = db.query(func.max(StockEVC.date)).scalar()
        stock_count_map = {}
        if latest_date:
            rows = (
                db.query(
                    stock_tags.c.tag_id,
                    func.count(func.distinct(stock_tags.c.stock_symbol)).label("stock_count")
                )
                .filter(stock_tags.c.date == latest_date)
                .group_by(stock_tags.c.tag_id)
                .all()
            )
            stock_count_map = {tag_id: stock_count for tag_id, stock_count in rows}

        tags = db.query(StockTag).all()
        tags = sorted(tags, key=lambda tag: (
            tag.sort_group if tag.sort_group is not None else 999999,
            tag.name or ""
        ))

        return [
            {
                "id": tag.id,
                "name": tag.name,
                "built_in": tag.built_in,
                "official_only": tag.official_only,
                "sort_group": tag.sort_group,
                "stock_count": stock_count_map.get(tag.id, 0),
            }
            for tag in tags
        ]
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

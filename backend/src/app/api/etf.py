from fastapi import APIRouter, HTTPException, Depends, Header, Query, Body
from pydantic import BaseModel
from typing import List, Optional, Dict
from datetime import date, datetime, timedelta
import json
from sqlalchemy import create_engine, select, desc, func, and_
from sqlalchemy.orm import Session
from ...core.database import ETFAnalysis, get_db_session, Session, ETFEmotion, ETFHolding, StockEVC, get_db
from .account import is_valid_account, valid_account
from ...core.services.quote import QuoteProvider
from ...core.services.longport import LongPortService



router = APIRouter(prefix="/api/etf")

class ETFComponent(BaseModel):
    symbol: str
    name: str
    asset_class: str
    shares: int
    market_value: float
    weight: float
    last_price: Optional[float]
    fair_value_lo: Optional[float]
    fair_value_hi: Optional[float]
    forward_next_fy_lo: Optional[float]
    forward_next_fy_hi: Optional[float]

class ETFReport(BaseModel):
    symbol: str
    min_fair_value_date: Optional[date]
    max_fair_value_date: Optional[date]
    date: date
    name: str
    update_date: str
    total_shares: float
    total_market_value: float
    current_price: float
    market_price: Optional[float]
    total_weight: float
    fair_value_lo: Optional[float]
    fair_value_hi: Optional[float]
    forward_next_fy_lo: Optional[float]
    forward_next_fy_hi: Optional[float]
    forward_stocks_value_lo: Optional[float]
    forward_stocks_value_hi: Optional[float]
    forward_stocks_fy_lo: Optional[float]
    forward_stocks_fy_hi: Optional[float]
    forward_stocks_weight: float
    eps: Optional[float] = None          # 新增: EPS
    eps_forward: Optional[float] = None      # 新增: 前瞻EPS
    eps_v2: Optional[float] = None        # 新增: EPSv2
    eps_ttm: Optional[float] = None
    created_at: datetime
    updated_at: datetime
    leveraged_symbol: Optional[str] = None
    leveraged_price: Optional[float] = None
    leveraged_szdt_score: Optional[int] = None
    leveraged_szdt_update_time: Optional[datetime] = None

    class Config:
        from_attributes = True

@router.get("/reports", response_model=List[ETFReport])
async def get_etf_reports(
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db)
):
    """获取最新的ETF分析报告列表"""
        # 获取最新分析日期
    latest_date = (
        db.query(func.max(ETFAnalysis.date))
        .scalar()
    )
    
    if not latest_date:
        return []
    
    # 获取最新日期的所有ETF数据，按total_market_value降序排序
    latest_reports = (
        db.query(ETFAnalysis)
        .filter(ETFAnalysis.date == latest_date)
        .order_by(desc(ETFAnalysis.total_market_value))
        .all()
    )

    # 获取所有ETF的市场价格
    symbols_to_quote = []
    for report in latest_reports:
        symbols_to_quote.append(report.symbol)

    quote_service: QuoteProvider = LongPortService('vNKpHJkLMnBQRSTUVWXYZabcdefghijkl')
    quote_info_list = quote_service.get_quote_batch(symbols_to_quote)
    quote_dict = {info['symbol']: info['price'] for info in quote_info_list}

    # 更新报告数据
    for report in latest_reports:
        report.market_price = quote_dict.get(report.symbol)
    
    return latest_reports

@router.get("/reports/{symbol}", response_model=ETFReport)
async def get_etf_report(
    symbol: str,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db)
):
    """获取指定ETF的详细报告"""
    # 获取最新的ETF分析记录
    latest_report = (
        db.query(ETFAnalysis)
        .filter(ETFAnalysis.symbol == symbol)
        .order_by(desc(ETFAnalysis.date))
        .first()
    )
    
    if not latest_report:
        raise HTTPException(status_code=404, message="未找到ETF报告")
    
    # 获取最新市场价格
    quote_service: QuoteProvider = LongPortService('vNKpHJkLMnBQRSTUVWXYZabcdefghijkl')
    quote_info = quote_service.get_quote(symbol)
    if quote_info:
        latest_report.market_price = quote_info['price']
    
    return latest_report

class ETFEmotionHistory(BaseModel):
    """ETF历史情绪数据"""
    date: datetime
    score: float
    momentum_score: float
    strength_score: float
    breadth_score: float
    volatility_score: float
    rsi_score: float

class EmotionWeights(BaseModel):
    momentum_score: float = 0.2
    strength_score: float = 0.2
    breadth_score: float = 0.2
    volatility_score: float = 0.2
    rsi_score: float = 0.2

@router.post("/emotions/{symbol}", response_model=List[ETFEmotionHistory])
async def get_etf_emotions(
    symbol: str,
    days: Optional[int] = Query(90, ge=1, le=365),
    account_id: str = Depends(valid_account),
    weights: EmotionWeights = Body(...),
    db: Session = Depends(get_db)
):
    """获取ETF的历史情绪指标数据"""
    start_date = datetime.now() - timedelta(days=days)
    
    db_emotions = (
        db.query(ETFEmotion)
        .filter(
            ETFEmotion.symbol == symbol,
            ETFEmotion.date >= start_date
        )
        .order_by(ETFEmotion.date.asc())
        .all()
    )
    
    emotions = [
        ETFEmotionHistory(
            date=e.date,
            score=(
                e.momentum_score * weights.momentum_score +
                e.strength_score * weights.strength_score +
                e.breadth_score * weights.breadth_score +
                e.volatility_score * weights.volatility_score +
                e.rsi_score * weights.rsi_score
            ),
            momentum_score=e.momentum_score,
            strength_score=e.strength_score,
            breadth_score=e.breadth_score,
            volatility_score=e.volatility_score,
            rsi_score=e.rsi_score
        ) for e in db_emotions
    ]
    
    return emotions

class ETFComponentDetail(BaseModel):
    """ETF成分股详细信息"""
    symbol: str
    name: str
    date: date
    asset_class: str
    weight: float
    last_price: Optional[float]
    fair_value_lo: Optional[float]
    fair_value_hi: Optional[float]
    forward_next_fy_lo: Optional[float]
    forward_next_fy_hi: Optional[float]
    pe_ratio: Optional[float]
    forward_pe_ratio: Optional[float]

@router.get("/components/{symbol}", response_model=List[ETFComponentDetail])
async def get_etf_components(
    symbol: str,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db)
):
    """获取ETF的最新持仓和成分股估值信息"""
    # 获取ETF最新持仓日期
    latest_holding_date = (
        db.query(func.max(ETFHolding.date))
        .filter(ETFHolding.etf_symbol == symbol)
        .scalar()
    )
    
    if not latest_holding_date:
        return []
    
    # 获取最新持仓信息
    holdings = (
        db.query(ETFHolding)
        .filter(
            ETFHolding.etf_symbol == symbol,
            ETFHolding.date == latest_holding_date
        )
        .all()
    )
    
    # 获取所有成分股的代码
    stock_symbols = [h.symbol for h in holdings if h.asset_class == 'Equity']
    
    # 获取成分股的最新估值信息
    latest_stocks = {}
    if stock_symbols:
        # 先获取StockEVC的最新日期
        latest_date = (
            db.query(func.max(StockEVC.date))
            .scalar()
        )
        
        if latest_date:
            # 使用最新日期获取所有成分股的信息
            stocks = (
                db.query(StockEVC)
                .filter(
                    StockEVC.symbol.in_(stock_symbols),
                    StockEVC.date == latest_date
                )
                .all()
            )
            latest_stocks = {s.symbol: s for s in stocks}
    
    # 组合持仓和估值信息
    components = []
    for holding in holdings:
        stock = latest_stocks.get(holding.symbol)
        components.append(ETFComponentDetail(
            symbol=holding.symbol,
            name=holding.name,
            date=holding.date,
            asset_class=holding.asset_class,
            weight=holding.weight,
            last_price=stock.last_price if stock else None,
            fair_value_lo=stock.fair_value_lo if stock else None,
            fair_value_hi=stock.fair_value_hi if stock else None,
            forward_next_fy_lo=stock.forward_next_fy_lo if stock else None,
            forward_next_fy_hi=stock.forward_next_fy_hi if stock else None,
            pe_ratio=stock.pe_ratio if stock else None,
            forward_pe_ratio=stock.forward_pe_ratio if stock else None
        ))
    
    # 按权重降序排序
    components.sort(key=lambda x: x.weight, reverse=True)
    
    return components


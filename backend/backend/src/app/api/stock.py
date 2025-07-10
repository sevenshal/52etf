from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime, timedelta
from ...core.database import Session, StockKline, StockFavorite, StockEVC, get_db_session
from .account import valid_account
from sqlalchemy import and_
from ...core.services.longport import LongPortService
from ...core.services.quote import QuoteService
from ...core.services.szdt import SZDTService

db_session = Session()

router = APIRouter(prefix="/api/stock")

class KLineData(BaseModel):
    """K线数据"""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float

class FavoriteResponse(BaseModel):
    """收藏响应"""
    success: bool
    message: str

@router.get("/klines/{symbol}", response_model=List[KLineData])
async def get_stock_klines(
    symbol: str,
    days: Optional[int] = Query(90, ge=1, le=2000),
    account_id: str = Depends(valid_account)
):
    """获取股票的K线数据"""
    trade_service: LongPortService = LongPortService(account_id)
    quote_service = QuoteService(trade_service)
    
    # 从 LongPort 获取 K 线数据
    klines_data = quote_service.get_klines(symbol, count=days, cache_only=False)
    
    # 转换为响应格式
    klines = [
        KLineData(
            timestamp=k['timestamp'],
            open=k['open'],
            high=k['high'],
            low=k['low'],
            close=k['close'],
            volume=k['volume'],
            turnover=k['turnover']
        ) for k in klines_data
    ]
    
    return klines

@router.post("/favorites/{symbol}", response_model=FavoriteResponse)
async def add_favorite(
    symbol: str,
    account_id: str = Depends(valid_account)
):
    """添加股票收藏"""
    with get_db_session(account_id) as session:
        favorite = session.query(StockFavorite).filter_by(
            symbol=symbol
        ).first()
        
        if favorite:
            return FavoriteResponse(success=False, message="已经收藏过该股票")
            
        new_favorite = StockFavorite(
            symbol=symbol
        )
        session.add(new_favorite)
        session.commit()
        
    return FavoriteResponse(success=True, message="收藏成功")

@router.delete("/favorites/{symbol}", response_model=FavoriteResponse)
async def remove_favorite(
    symbol: str,
    account_id: str = Depends(valid_account)
):
    """取消股票收藏"""
    with get_db_session(account_id) as session:
        favorite = session.query(StockFavorite).filter_by(
            symbol=symbol
        ).first()
        
        if not favorite:
            return FavoriteResponse(success=False, message="未找到收藏记录")
            
        session.delete(favorite)
        session.commit()
        
    return FavoriteResponse(success=True, message="取消收藏成功")

@router.get("/favorites", response_model=List[dict])
async def get_favorites(
    account_id: str = Depends(valid_account)
):
    """获取用户收藏的股票列表（包含完整信息）"""
    with get_db_session(account_id) as session:
        favorite_symbols = [f[0] for f in session.query(StockFavorite.symbol).all()]
        
    if not favorite_symbols:
        return []
    
    # 从主数据库中获取这些股票的最新信息
    latest_stocks = (
        db_session.query(StockEVC)
        .filter(
            and_(
                StockEVC.symbol.in_(favorite_symbols),
                StockEVC.date == db_session.query(StockEVC.date)
                .filter(StockEVC.symbol == StockEVC.symbol)
                .order_by(StockEVC.date.desc())
                .limit(1)
                .scalar_subquery()
            )
        )
        .all()
    )
    
    # 获取所有股票代码
    symbols = [stock.symbol for stock in latest_stocks]
    
    # 获取股票静态信息
    quote_service = LongPortService(account_id)
    static_info_list = []
    if symbols:
        static_info_list = quote_service.get_static_info(symbols)
    
    # 将静态信息列表转换为以symbol为键的字典，方便查找
    static_info_dict = {}
    for info in static_info_list:
        if 'symbol' in info:
            static_info_dict[info['symbol']] = info
    
    # 初始化 SZDT 服务
    szdt_service = SZDTService()
    
    # 获取情绪数据
    szdt_resp = await szdt_service.get_etf_emotion(etf_type=7)
    
    # 构建情绪数据字典
    emotion_dict = {}
    if szdt_resp and szdt_resp['status'] == 1:
        for item in szdt_resp['data']:
            if 'code' in item and 'emotion' in item:
                symbol = item['code'].replace('US.', '') + '.US'
                emotion_dict[symbol] = {
                    'price': float(item['emotion'].get('price', 0)),
                    'score': item['emotion'].get('score', 0),
                    'time': item['emotion'].get('updated_at', '')
                }
    
    # 转换为字典列表
    return [{
        'symbol': stock.symbol,
        'company': static_info_dict.get(stock.symbol, {})['name_cn'] or stock.company,
        'last_price': stock.last_price,
        'fair_value_lo': stock.fair_value_lo,
        'fair_value_hi': stock.fair_value_hi,
        'fair_value_date': stock.fair_value_date,
        'forward_next_fy_lo': stock.forward_next_fy_lo,
        'forward_next_fy_hi': stock.forward_next_fy_hi,
        'pe_ratio': stock.pe_ratio,
        'forward_pe_ratio': stock.forward_pe_ratio,
        'beta': stock.beta,
        'date': stock.date,
        'static_info': static_info_dict.get(stock.symbol, {}),
        'emotion_info': emotion_dict.get(stock.symbol)
    } for stock in latest_stocks]
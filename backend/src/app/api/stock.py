from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime, timedelta
from ...core.database import Session, StockKline, StockFavorite, StockEVC, ETFAnalysis, get_db
from .account import valid_account
from sqlalchemy import and_
from ...core.services.longport import LongPortService
from ...core.services.quote import QuoteService
from ...core.services.szdt import SZDTService

# db_session removed, use dependency injection
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
    pe: Optional[float] = None
    forward_pe: Optional[float] = None

class FavoriteResponse(BaseModel):
    """收藏响应"""
    success: bool
    message: str

@router.get("/klines/{symbol}", response_model=List[KLineData])
async def get_stock_klines(
    symbol: str,
    days: Optional[int] = Query(90, ge=1, le=10000),
    account_id: str = Depends(valid_account),
    period: Optional[str] = Query(default='d', enum=['d', 'w', 'm']),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """获取股票的K线数据"""
    trade_service: LongPortService = LongPortService(account_id)
    quote_service = QuoteService(trade_service)
    
    # 解析日期
    parsed_start_date = None
    parsed_end_date = None
    if start_date:
        try:
            parsed_start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid start_date format. Use YYYY-MM-DD")
            
    if end_date:
        try:
            parsed_end_date = datetime.strptime(end_date, '%Y-%m-%d').date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid end_date format. Use YYYY-MM-DD")
    else:
        parsed_end_date = datetime.now().date()

    if parsed_start_date and parsed_end_date:
        # 使用日期范围获取
        klines_data = quote_service.get_klines(
            symbol, 
            start_date=parsed_start_date, 
            end_date=parsed_end_date, 
            period=period
        )
    else:
        # 使用数量获取 (保留旧模式兼容性)
        count = days / 30 if period == 'm' else days / 7 if period == 'w' else days
        klines_data = quote_service.get_klines(symbol, count=int(count), cache_only=False, period=period)
    
    # 获取 ETFAnalysis 数据
    etf_analysis_dict = {}
    
    if klines_data:
        start_date_limit = min([k['timestamp'] for k in klines_data]).date()
        end_date_limit = max([k['timestamp'] for k in klines_data]).date()

        analysis_records = db.query(ETFAnalysis).filter(
            and_(
                ETFAnalysis.symbol == symbol,
                ETFAnalysis.date >= start_date_limit,
                ETFAnalysis.date <= end_date_limit
            )
        ).all()
            
        for record in analysis_records:
            # PE = market_price / eps
            # Forward PE = market_price / eps_forward
            pe = record.market_price / record.eps if record.market_price and record.eps else None
            forward_pe = record.market_price / record.eps_forward if record.market_price and record.eps_forward else None
            etf_analysis_dict[record.date] = {
                'pe': pe,
                'forward_pe': forward_pe
            }

    # 转换为响应格式
    klines = []
    for k in klines_data:
        k_date = k['timestamp'].date()
        analysis_data = etf_analysis_dict.get(k_date, {})
        
        klines.append(KLineData(
            timestamp=k['timestamp'],
            open=k['open'],
            high=k['high'],
            low=k['low'],
            close=k['close'],
            volume=k['volume'],
            turnover=k['turnover'],
            pe=analysis_data.get('pe'),
            forward_pe=analysis_data.get('forward_pe')
        ))
    
    return klines

@router.post("/favorites/{symbol}", response_model=FavoriteResponse)
async def add_favorite(
    symbol: str,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db)
):
    """添加股票收藏"""
    favorite = db.query(StockFavorite).filter_by(
        symbol=symbol,
        account_id=account_id
    ).first()
    
    if favorite:
        return FavoriteResponse(success=False, message="已经收藏过该股票")
        
    new_favorite = StockFavorite(
        symbol=symbol,
        account_id=account_id
    )
    db.add(new_favorite)
    db.commit()
        
    return FavoriteResponse(success=True, message="收藏成功")

@router.delete("/favorites/{symbol}", response_model=FavoriteResponse)
async def remove_favorite(
    symbol: str,
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db)
):
    """取消股票收藏"""
    favorite = db.query(StockFavorite).filter_by(
        symbol=symbol,
        account_id=account_id
    ).first()
    
    if not favorite:
        return FavoriteResponse(success=False, message="未找到收藏记录")
        
    db.delete(favorite)
    db.commit()
    return FavoriteResponse(success=True, message="取消收藏成功")

@router.get("/favorites", response_model=List[dict])
async def get_favorites(
    account_id: str = Depends(valid_account),
    db: Session = Depends(get_db)
):
    """获取用户收藏的股票列表（包含完整信息）"""
    favorite_symbols = [f[0] for f in db.query(StockFavorite.symbol).filter(StockFavorite.account_id == account_id).all()]
        
    if not favorite_symbols:
        return []
    
    # 从主数据库中获取这些股票的最新信息
    latest_stocks = (
        db.query(StockEVC)
        .filter(
            and_(
                StockEVC.symbol.in_(favorite_symbols),
                StockEVC.date == db.query(StockEVC.date)
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
from fastapi import APIRouter, HTTPException, Depends, Query, Request
from pydantic import BaseModel
from typing import List, Optional
import asyncio
from datetime import datetime
from ...core.database import StockFavorite, StockEVC, get_db, LongPortAccount
from .account import valid_account
from sqlalchemy import func, text
from ...core.services.longport import LongPortService
from ...core.services.quote import QuoteService
from ...core.services.szdt import SZDTService
from ...core.static_info import get_static_info_snapshot_map
from ...core.analytics_database import AnalyticsSession
from ...core.services.a_stock_consensus import load_a_stock_klines
from sqlalchemy.orm import Session

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
    turnover_rate: Optional[float] = None

class FavoriteResponse(BaseModel):
    """收藏响应"""
    success: bool
    message: str


def _parse_date_query(value: Optional[str], field_name: str):
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name} format. Use YYYY-MM-DD")


@router.get("/a-stock/symbols")
def search_a_stock_symbols(
    q: str = Query(default="", max_length=64),
    limit: int = Query(default=20, ge=1, le=50),
    _: str = Depends(valid_account),
):
    """按股票名称或代码搜索当前上市的 A 股。"""
    search_text = str(q or "").strip().upper()
    analytics_db = AnalyticsSession()
    try:
        rows = analytics_db.execute(
            text(
                """
                SELECT ts_code, symbol, name
                FROM a_stock_basic
                WHERE list_status = 'L'
                  AND (
                        :query = ''
                     OR UPPER(ts_code) LIKE :pattern
                     OR UPPER(symbol) LIKE :pattern
                     OR UPPER(COALESCE(name, '')) LIKE :pattern
                  )
                ORDER BY
                    CASE
                        WHEN UPPER(ts_code) = :query OR UPPER(symbol) = :query THEN 0
                        WHEN UPPER(ts_code) LIKE :prefix OR UPPER(symbol) LIKE :prefix THEN 1
                        WHEN UPPER(COALESCE(name, '')) LIKE :prefix THEN 2
                        ELSE 3
                    END,
                    ts_code
                LIMIT :limit
                """
            ),
            {
                "query": search_text,
                "pattern": f"%{search_text}%",
                "prefix": f"{search_text}%",
                "limit": limit,
            },
        ).mappings().all()
        return [
            {
                "value": row["ts_code"],
                "label": f"{row['name']} · {row['ts_code']}" if row["name"] else row["ts_code"],
                "name": row["name"],
            }
            for row in rows
        ]
    finally:
        analytics_db.close()
        AnalyticsSession.remove()


@router.get("/klines/{symbol}", response_model=List[KLineData])
async def get_stock_klines(
    request: Request,
    symbol: str,
    account_id: str = Depends(valid_account),
    period: Optional[str] = Query(default='d', enum=['d', 'w', 'm']),
    start_date: str = Query(...),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """获取股票的K线数据"""
    if "days" in request.query_params:
        raise HTTPException(status_code=400, detail="days 参数已不支持，请使用 start_date/end_date 日期区间")

    lp_account = db.query(LongPortAccount).filter(LongPortAccount.account_id == account_id).first()
    lp_account_id = lp_account.lp_account_id if lp_account else "LBPT10001248"
    trade_service: LongPortService = LongPortService.get_instance(lp_account_id)
    quote_service = QuoteService(trade_service)
    
    parsed_start_date = _parse_date_query(start_date, "start_date")
    parsed_end_date = _parse_date_query(end_date, "end_date") if end_date else datetime.now().date()

    # 使用日期范围获取
    klines_data = quote_service.get_klines(
        symbol,
        start_date=parsed_start_date,
        end_date=parsed_end_date,
        period=period
    )

    static_info = get_static_info_snapshot_map(db, [symbol]).get(symbol.upper(), {})
    circulating_shares = static_info.get("circulating_shares") if static_info else None
    total_shares = static_info.get("total_shares") if static_info else None
    turnover_base_shares = circulating_shares or total_shares

    def calculate_turnover_rate(volume):
        try:
            volume_value = float(volume)
            shares_value = float(turnover_base_shares)
        except (TypeError, ValueError):
            return None
        if volume_value < 0 or shares_value <= 0:
            return None
        return volume_value / shares_value
    
    # 转换为响应格式
    klines = []
    for k in klines_data:
        klines.append(KLineData(
            timestamp=k['timestamp'],
            open=k['open'],
            high=k['high'],
            low=k['low'],
            close=k['close'],
            volume=k['volume'],
            turnover=k['turnover'],
            turnover_rate=calculate_turnover_rate(k.get('volume')),
        ))
    
    return klines


@router.get("/a-stock/klines/{symbol}", response_model=List[KLineData])
async def get_a_stock_klines(
    request: Request,
    symbol: str,
    account_id: str = Depends(valid_account),
    period: Optional[str] = Query(default='d', enum=['d']),
    start_date: str = Query(...),
    end_date: Optional[str] = Query(None),
):
    """从本地 DuckDB 获取A股前复权日K。"""
    if "days" in request.query_params:
        raise HTTPException(status_code=400, detail="days 参数已不支持，请使用 start_date/end_date 日期区间")
    parsed_start_date = _parse_date_query(start_date, "start_date")
    parsed_end_date = _parse_date_query(end_date, "end_date") if end_date else datetime.now().date()
    if not parsed_start_date:
        raise HTTPException(status_code=400, detail="start_date is required")
    if parsed_start_date > parsed_end_date:
        raise HTTPException(status_code=400, detail="start_date 不能晚于 end_date")

    analytics_db = AnalyticsSession()
    try:
        rows = load_a_stock_klines(
            analytics_db,
            symbol,
            start_date=parsed_start_date,
            end_date=parsed_end_date,
        )
        return [KLineData(**row) for row in rows]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        analytics_db.close()
        AnalyticsSession.remove()


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

    latest_date = db.query(func.max(StockEVC.date)).scalar()
    if not latest_date:
        return []

    # 从主数据库中获取收藏股票在最新日期的估值信息
    latest_stocks = (
        db.query(StockEVC)
        .filter(
            StockEVC.symbol.in_(favorite_symbols),
            StockEVC.date == latest_date
        )
        .all()
    )

    # 按收藏顺序返回
    stock_map = {stock.symbol: stock for stock in latest_stocks}
    latest_stocks = [stock_map[s] for s in favorite_symbols if s in stock_map]
    
    # 获取所有股票代码
    symbols = [stock.symbol for stock in latest_stocks]

    # 直接从数据库快照读取静态信息
    static_info_dict = get_static_info_snapshot_map(db, symbols)

    # 初始化 SZDT 服务
    szdt_service = SZDTService()

    # 情绪接口超时则降级为空，避免拖慢整个接口
    try:
        szdt_resp = await asyncio.wait_for(szdt_service.get_etf_emotion(etf_type=7), timeout=4.0)
    except Exception:
        szdt_resp = None

    def calculate_market_cap(stock: StockEVC, static_info: dict):
        total_shares = static_info.get('total_shares') if static_info else None
        if not isinstance(stock.last_price, (int, float)) or stock.last_price <= 0:
            return None
        if not isinstance(total_shares, (int, float)) or total_shares <= 0:
            return None
        return stock.last_price * total_shares

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
    result = []
    for stock in latest_stocks:
        static_info = static_info_dict.get(stock.symbol, {})
        market_cap = calculate_market_cap(stock, static_info)
        result.append({
            'symbol': stock.symbol,
            'company': static_info.get('name_cn') or stock.company,
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
            'market_cap': market_cap,
            'market_cap_100m': market_cap / 100_000_000 if market_cap is not None else None,
            'static_info': static_info,
            'emotion_info': emotion_dict.get(stock.symbol)
        })

    return result

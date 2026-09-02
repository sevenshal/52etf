from fastapi import APIRouter, HTTPException, Depends, Query, Request
from pydantic import BaseModel
from typing import List, Optional
import asyncio
import math
from datetime import datetime, time
from ...core.database import StockFavorite, StockEVC, get_db, LongPortAccount
from .account import valid_account
from sqlalchemy import func, text
from ...core.services.longport import LongPortService
from ...core.services.quote import QuoteService
from ...core.services.szdt import SZDTService
from ...core.static_info import get_static_info_snapshot_map
from ...core.analytics_database import AnalyticsSession
from ...core.services.a_stock_consensus import load_a_stock_klines
from ...core.services.tushare import TushareService
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


def _safe_quote_number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _load_current_a_stock_kline(symbol: str, expected_date):
    """读取 Tushare 实时日K；只接受当天数据，避免未开盘时混入上一交易日。"""
    service = TushareService.get_instance()
    stock_frame = service.get_a_stock_realtime_rt_k_frame([symbol])
    frames = [stock_frame]
    if stock_frame is None or not hasattr(stock_frame, "empty") or stock_frame.empty:
        frames.append(service.get_a_stock_realtime_etf_rt_k_frame([symbol]))
    normalized_symbol = str(symbol or "").strip().upper()
    for frame in frames:
        if frame is None or not hasattr(frame, "empty") or frame.empty:
            continue
        for _, row in frame.iterrows():
            if str(row.get("ts_code") or "").strip().upper() != normalized_symbol:
                continue
            trade_time = row.get("trade_time")
            parsed_time = trade_time if isinstance(trade_time, datetime) else None
            if parsed_time is None:
                try:
                    parsed_time = datetime.fromisoformat(str(trade_time))
                except (TypeError, ValueError):
                    continue
            if parsed_time.date() != expected_date:
                continue
            open_price = _safe_quote_number(row.get("open"))
            high_price = _safe_quote_number(row.get("high"))
            low_price = _safe_quote_number(row.get("low"))
            close_price = _safe_quote_number(row.get("close"))
            if (
                not all(value is not None and value > 0 for value in (open_price, high_price, low_price, close_price))
                or high_price < max(open_price, close_price, low_price)
                or low_price > min(open_price, close_price, high_price)
            ):
                continue
            return {
                "timestamp": datetime.combine(expected_date, time(hour=15)),
                "open": open_price,
                "high": high_price,
                "low": low_price,
                "close": close_price,
                "volume": _safe_quote_number(row.get("vol")) or 0.0,
                "turnover": _safe_quote_number(row.get("amount")) or 0.0,
                "turnover_rate": None,
            }
    return None


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


@router.get("/a-stock/summary/{symbol}")
def get_a_stock_summary(
    symbol: str,
    _: str = Depends(valid_account),
    db: Session = Depends(get_db),
):
    """返回详情页行情头部所需的名称、股本和最新财务快照。"""
    normalized_symbol = str(symbol or "").strip().upper()
    static_info = get_static_info_snapshot_map(db, [normalized_symbol]).get(normalized_symbol, {})
    analytics_db = AnalyticsSession()
    try:
        basic = analytics_db.execute(
            text("SELECT name FROM a_stock_basic WHERE ts_code = :symbol LIMIT 1"),
            {"symbol": normalized_symbol},
        ).mappings().first()
        latest = analytics_db.execute(
            text(
                """
                SELECT total_share, float_share
                FROM a_stock_market_daily
                WHERE ts_code = :symbol
                ORDER BY trade_date DESC
                LIMIT 1
                """
            ),
            {"symbol": normalized_symbol},
        ).mappings().first()
    finally:
        analytics_db.close()
        AnalyticsSession.remove()

    def shares(static_key, daily_key):
        value = static_info.get(static_key)
        if value is not None:
            return _safe_quote_number(value)
        daily_value = _safe_quote_number(latest.get(daily_key)) if latest else None
        return daily_value * 10_000 if daily_value is not None else None

    return {
        "symbol": normalized_symbol,
        "name": (basic.get("name") if basic else None) or static_info.get("name_cn"),
        "currency": static_info.get("currency") or "CNY",
        "eps": _safe_quote_number(static_info.get("eps")),
        "bps": _safe_quote_number(static_info.get("bps")),
        "total_shares": shares("total_shares", "total_share"),
        "circulating_shares": shares("circulating_shares", "float_share"),
    }


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
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        analytics_db.close()
        AnalyticsSession.remove()

    # DuckDB 会话先关闭，再执行 Tushare 网络 IO，避免把短事务拖成长事务。
    today = datetime.now().date()
    has_today = any(row["timestamp"].date() == today for row in rows)
    if parsed_start_date <= today <= parsed_end_date and not has_today:
        realtime_row = await asyncio.to_thread(_load_current_a_stock_kline, symbol, today)
        if realtime_row:
            rows.append(realtime_row)
    return [KLineData(**row) for row in rows]


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

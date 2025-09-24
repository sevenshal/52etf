from fastapi import APIRouter, Query
from ...core.database import Session, MarketSignal

router = APIRouter()
db_session = Session()

@router.get("/api/market_signal")
def get_market_signal(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    query = db_session.query(MarketSignal).order_by(
        MarketSignal.date.desc(), MarketSignal.id.desc()
    )
    total = query.count()
    items = query.offset((page - 1) * page_size).limit(page_size).all()
    result = [
        {
            "symbol": item.symbol,
            "ver": item.ver,
            "close_price": item.close_price,
            "date": item.date.isoformat(),
            "direction": item.direction,
            "below_200ma_ratio": item.below_200ma_ratio,
            "vol_5_std": item.vol_5_std,
            "today_vol_std": item.today_vol_std,
            "low_50": item.low_50,
            "close_vs_low_50": item.close_vs_low_50,
            "v2_price_change_ratio": item.v2_price_change_ratio,
            "v2_stabilization_period": item.v2_stabilization_period
        }
        for item in items
    ]
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": result
    }

"""A股前瞻 PE 通道的每日预计算与读取。

通道本身的算法在 `a_stock_consensus.compute_forward_pe_bands`；全市场现算要二十来秒，
列表页等不起，所以由定时任务每天收盘后算好存进 SQLite，列表开启"PE 通道补估值"时
直接读。个股详情页只算一只，仍然现算。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, Mapping, Optional

from sqlalchemy import func, text

from ..analytics_database import AnalyticsSession
from ..database import AStockConsensusPeBand, Session
from .a_stock_consensus import (
    PE_BAND_AVAILABLE,
    REPORT_FETCH_LOOKBACK_DAYS,
    compute_forward_pe_bands,
    normalize_a_stock_symbol,
)

# 超过这么多天没刷新的通道当作没有，避免定时任务停了之后一直拿旧通道估值。
PE_BAND_MAX_STALE_DAYS = 10


def _to_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)).date()
    except (TypeError, ValueError):
        return None


def _jsonable(band: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        key: value.isoformat() if isinstance(value, (date, datetime)) else value
        for key, value in band.items()
    }


def refresh_a_stock_consensus_pe_bands() -> Dict[str, Any]:
    """按最新交易日，给近一年有研报覆盖的股票重算前瞻 PE 通道并落库。"""
    analytics_db = AnalyticsSession()
    try:
        as_of = _to_date(analytics_db.execute(text("SELECT MAX(trade_date) FROM a_stock_market_daily")).scalar())
        if as_of is None:
            return {"as_of": None, "saved": 0, "available": 0}
        symbols = [
            row[0]
            for row in analytics_db.execute(text("""
                SELECT DISTINCT ts_code
                FROM a_stock_report_rc
                WHERE report_date > :start AND report_date <= :as_of
            """), {"start": as_of - timedelta(days=REPORT_FETCH_LOOKBACK_DAYS), "as_of": as_of}).all()
        ]
        bands = compute_forward_pe_bands(analytics_db, symbols, as_of)
    finally:
        analytics_db.close()
        AnalyticsSession.remove()

    db = Session()
    try:
        for symbol, band in bands.items():
            db.merge(AStockConsensusPeBand(symbol=symbol, as_of=as_of, payload=_jsonable(band)))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        Session.remove()
    return {
        "as_of": as_of.isoformat(),
        "saved": len(bands),
        "available": sum(1 for band in bands.values() if band.get("status") == PE_BAND_AVAILABLE),
    }


def load_a_stock_consensus_pe_bands(symbols: Optional[Iterable[str]] = None) -> Dict[str, Dict[str, Any]]:
    """读预先算好的通道；只认最近一次刷新前后 PE_BAND_MAX_STALE_DAYS 天内的结果。"""
    db = Session()
    try:
        latest = db.query(func.max(AStockConsensusPeBand.as_of)).scalar()
        if latest is None:
            return {}
        query = db.query(AStockConsensusPeBand).filter(
            AStockConsensusPeBand.as_of >= latest - timedelta(days=PE_BAND_MAX_STALE_DAYS)
        )
        wanted = None
        if symbols is not None:
            wanted = [normalized for symbol in symbols if (normalized := normalize_a_stock_symbol(symbol))]
            query = query.filter(AStockConsensusPeBand.symbol.in_(wanted))
        return {row.symbol: dict(row.payload or {}) for row in query.all()}
    finally:
        Session.remove()

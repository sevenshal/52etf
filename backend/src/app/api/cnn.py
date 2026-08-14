import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool

from ...core.database import AStockFearGreedIntraday, Session
from ...core.services.etf_fear_greed_clone_service import ETFFearGreedCloneCalculator
from ...core.services.a_stock_index_valuation import load_a_stock_index_valuation
from ...core.services.a_stock_fear_greed_clone_service import A_STOCK_FEAR_GREED_TARGET_BY_SYMBOL
from ...core.services.fear_greed_clone_service import FearGreedCloneCalculator
from ...core.services.market import MarketService
from ...robot.cnn_fear_index import CNNFearGreedIndexScraper

router = APIRouter(prefix="/api/cnn")
logger = logging.getLogger(__name__)

@router.get("/fear-greed")
async def get_fear_greed_index():
    """获取最新的CNN恐贪指数。"""
    try:
        return await run_in_threadpool(_fetch_cnn_fear_greed)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取CNN恐贪指数失败: {str(e)}") 


@router.get("/fear-greed-clone")
async def get_fear_greed_clone(
    as_of: Optional[str] = None,
    history_days: int = 550,
    score_window: int = 252,
    min_periods: int = 120,
    include_history: bool = False,
):
    """获取独立复刻版恐贪指数。

    这个接口不抓 CNN 分数，而是用免费数据源计算 7 个代理指标，然后用
    rolling z-score + normal CDF 映射成 0-100 分。
    """
    try:
        calculator = FearGreedCloneCalculator()
        return await run_in_threadpool(
            lambda: calculator.calculate(
                as_of=as_of,
                history_days=history_days,
                score_window=score_window,
                min_periods=min_periods,
                include_history=include_history,
            )
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取独立恐贪指数失败: {str(e)}")


@router.get("/etf-fear-greed-clone")
async def get_etf_fear_greed_clone(
    symbol: str = "SOXX.US",
    as_of: Optional[str] = None,
    history_days: int = 550,
    score_window: int = 252,
    min_periods: int = 120,
    include_history: bool = False,
    history_points: int = 180,
    use_historical_holdings: bool = True,
):
    """获取ETF版本的独立复刻恐贪指数。

    默认计算 SOXX.US。持仓组件读取 etf_holdings 中的交易日快照；
    价格、期权和信用利差尽量使用免费数据源。
    """
    try:
        calculator = ETFFearGreedCloneCalculator()
        return await run_in_threadpool(
            lambda: calculator.calculate(
                symbol=symbol,
                as_of=as_of,
                history_days=history_days,
                score_window=score_window,
                min_periods=min_periods,
                include_history=include_history,
                history_points=history_points,
                use_historical_holdings=use_historical_holdings,
            )
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取ETF独立恐贪指数失败: {str(e)}")


@router.get("/etf-fear-greed-clone/realtime")
async def get_etf_fear_greed_clone_realtime(
    symbol: str = "SOXX.US",
    history_days: int = 550,
    score_window: int = 252,
    min_periods: int = 120,
    include_extended: bool = True,
    include_holdings_quotes: bool = True,
):
    """获取 ETF 盘中实时版恐贪复刻指数。

    这个接口使用 SQLite 中已回跑的日频组件作为评分基准，再用 LongPort
    实时行情更新 ETF、TLT 和 DB 持仓相关的价格驱动组件。期权 put/call
    优先使用 Barchart 实时到期日快照接口，接口失败时才读本地当天
    Barchart 快照；信用利差仍是日频数据，会沿用最近已入库值并标记。
    """
    try:
        calculator = ETFFearGreedCloneCalculator()
        return await run_in_threadpool(
            lambda: calculator.calculate_realtime_cached(
                symbol=symbol,
                history_days=history_days,
                score_window=score_window,
                min_periods=min_periods,
                include_extended=include_extended,
                include_holdings_quotes=include_holdings_quotes,
            )
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取ETF实时恐贪指数失败: {str(e)}")


@router.get("/etf-fear-greed-clone/intraday")
async def get_a_stock_fear_greed_intraday(
    symbol: Optional[str] = None,
    symbols: Optional[str] = None,
):
    """读取 A股盘中贪恐快照（独立盘中历史库，不入日频最终历史库）。"""
    try:
        symbol_list = _parse_symbols(symbol, symbols)
        if not symbol_list:
            raise HTTPException(status_code=400, detail="请提供 symbol 或 symbols 参数")
        return await run_in_threadpool(lambda: _load_intraday_snapshots(symbol_list))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取A股盘中贪恐快照失败: {str(e)}")


@router.get("/etf-fear-greed-clone/history")
async def get_etf_fear_greed_clone_history(
    symbol: str = "SOXX.US",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    include_components: bool = True,
    include_latest_holdings: bool = True,
):
    """从 SQLite 读取 ETF 恐贪复刻指数历史。"""
    try:
        calculator = ETFFearGreedCloneCalculator()
        result = await run_in_threadpool(
            lambda: calculator.load_history_from_db(
                symbol=symbol,
                start_date=_parse_date(start_date),
                end_date=_parse_date(end_date),
                include_components=include_components,
                include_latest_holdings=include_latest_holdings,
            )
        )
        if str(symbol or "").strip().upper() in A_STOCK_FEAR_GREED_TARGET_BY_SYMBOL:
            result["valuation"] = await run_in_threadpool(
                lambda: load_a_stock_index_valuation(symbol)
            )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取ETF独立恐贪历史失败: {str(e)}")


@router.get("/etf-fear-greed-clone/summaries")
async def get_etf_fear_greed_clone_summaries(
    symbols: str,
):
    """批量读取 ETF/指数恐贪复刻值摘要。

    A股指数叠加当日盘中快照；港股附加盘中实时版贪恐（轻量模式，进程内 5 分钟缓存，
    计算失败自动跳过，卡片仍显示最新收盘）。美股暂不叠加盘中。
    """
    try:
        symbol_list = [
            item.strip()
            for item in str(symbols or "").split(",")
            if item.strip()
        ]
        calculator = ETFFearGreedCloneCalculator()
        result = await run_in_threadpool(
            lambda: calculator.load_summaries_from_db(symbol_list)
        )
        a_stock_symbols = [
            symbol.upper()
            for symbol in symbol_list
            if symbol.upper() in A_STOCK_FEAR_GREED_TARGET_BY_SYMBOL
        ]
        intraday_map = {}
        if a_stock_symbols:
            today_shanghai = datetime.now(ZoneInfo("Asia/Shanghai")).date()
            intraday_map = await run_in_threadpool(
                lambda: _load_intraday_snapshot_map(
                    a_stock_symbols, trade_date=today_shanghai
                )
            )
        realtime_map = {}
        # 港股：盘中实时版贪恐（轻量模式：只取指数+TLT 实时价，不拉成分股行情）。
        # 仅港股交易时段才取，开盘前/收盘后/非交易日不取（避免显示昨收的陈旧盘中值）；
        # 美股暂不叠加盘中，卡片仍显示最新收盘。
        realtime_symbols = [
            symbol.upper()
            for symbol in symbol_list
            if symbol.upper() not in A_STOCK_FEAR_GREED_TARGET_BY_SYMBOL
            and symbol.upper().endswith(".HK")
        ]
        if realtime_symbols and not MarketService.is_hk_market_open():
            realtime_symbols = []
        if realtime_symbols:
            async def _load_realtime(current_symbol: str):
                try:
                    payload = await asyncio.wait_for(
                        run_in_threadpool(
                            lambda: calculator.calculate_realtime_cached(
                                symbol=current_symbol,
                                include_holdings_quotes=False,
                                include_extended=True,
                                # 摘要卡片只取 ETF+TLT 实时价，不批量拉成分股行情
                                fetch_holdings_quotes=False,
                            )
                        ),
                        timeout=15.0,
                    )
                    return current_symbol, _serialize_realtime_snapshot(payload)
                except Exception as exc:
                    logger.warning("ETF 实时贪恐 %s 计算失败: %s", current_symbol, exc)
                    return current_symbol, None

            realtime_results = await asyncio.gather(
                *(_load_realtime(symbol) for symbol in realtime_symbols)
            )
            realtime_map = {
                symbol: snapshot
                for symbol, snapshot in realtime_results
                if snapshot
            }
        for item in result.get("data", []):
            symbol = item.get("symbol")
            if symbol in A_STOCK_FEAR_GREED_TARGET_BY_SYMBOL:
                item["valuation"] = await run_in_threadpool(
                    lambda current_symbol=symbol: load_a_stock_index_valuation(current_symbol)
                )
            intraday = intraday_map.get(symbol)
            if intraday:
                item["intraday"] = intraday
            else:
                realtime = realtime_map.get(symbol)
                if realtime:
                    item["intraday"] = realtime
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取ETF独立恐贪摘要失败: {str(e)}")


@router.post("/etf-fear-greed-clone/backfill")
async def backfill_etf_fear_greed_clone(
    symbol: str = "SOXX.US",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    history_days: int = 1200,
    score_window: int = 252,
    min_periods: int = 120,
    use_historical_holdings: bool = True,
):
    """计算 ETF 恐贪复刻指数历史并写入 SQLite。"""
    try:
        calculator = ETFFearGreedCloneCalculator()
        return await run_in_threadpool(
            lambda: calculator.backfill_to_db(
                symbol=symbol,
                start_date=_parse_date(start_date),
                end_date=_parse_date(end_date),
                history_days=history_days,
                score_window=score_window,
                min_periods=min_periods,
                use_historical_holdings=use_historical_holdings,
            )
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"回跑ETF独立恐贪指数失败: {str(e)}")


def _parse_date(value: Optional[str]):
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def _parse_symbols(symbol: Optional[str], symbols: Optional[str]):
    parts = []
    for value in (symbol, symbols):
        for item in str(value or "").split(","):
            text = item.strip().upper()
            if text and text not in parts:
                parts.append(text)
    return parts


def _serialize_realtime_snapshot(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把美股/港股 ETF 实时贪恐计算结果映射成摘要卡通用的 intraday 结构。"""
    meta = payload.get("fear_and_greed_clone") or {}
    score = meta.get("score")
    if score is None:
        return None
    etf_price = payload.get("etf_price") or {}
    quote = etf_price.get("quote") or {}
    quote_time = quote.get("timestamp") or meta.get("timestamp")
    return {
        "symbol": meta.get("symbol"),
        "score": score,
        "rating": meta.get("rating"),
        "mode": "realtime",
        "snapshot_time": quote_time,
        "trade_date": meta.get("date"),
        "component_count": meta.get("component_count"),
        "components_used": meta.get("components_used") or [],
        "index_level": etf_price.get("close") or quote.get("price"),
        "quote_source": quote.get("source") or "longport",
        "quote_time": quote_time,
        "market_open": bool(meta.get("market_open")),
        "warnings": payload.get("warnings") or [],
    }


def _serialize_intraday_snapshot(row: AStockFearGreedIntraday) -> dict:
    etf_price = row.etf_price or {}
    if row.index_level is not None and not etf_price.get("close"):
        etf_price = {**etf_price, "close": row.index_level}
    return {
        "symbol": row.symbol,
        "score": row.score,
        "rating": row.rating,
        "method": row.method,
        "mode": "intraday",
        "snapshot_time": row.snapshot_time.isoformat() if row.snapshot_time else None,
        "trade_date": row.trade_date.isoformat() if row.trade_date else None,
        "component_count": row.component_count,
        "components_used": row.components_used or [],
        "index_level": row.index_level,
        "quote_source": row.quote_source,
        "quote_time": row.quote_time.isoformat() if row.quote_time else None,
        "market_open": bool(row.market_open),
        "etf_price": etf_price,
        "components": row.components or {},
        "warnings": row.warnings or [],
        "fear_and_greed_clone": {
            "symbol": row.symbol,
            "score": row.score,
            "rating": row.rating,
            "date": row.trade_date.isoformat() if row.trade_date else None,
            "timestamp": row.snapshot_time.isoformat() if row.snapshot_time else None,
            "method": row.method,
            "mode": "intraday",
            "component_count": row.component_count,
            "market_open": bool(row.market_open),
        },
    }


def _load_intraday_snapshots(symbol_list):
    db = Session()
    try:
        rows = (
            db.query(AStockFearGreedIntraday)
            .filter(AStockFearGreedIntraday.symbol.in_(symbol_list))
            .order_by(AStockFearGreedIntraday.snapshot_time.desc())
            .all()
        )
        latest_by_symbol = {}
        for row in rows:
            if row.symbol not in latest_by_symbol:
                latest_by_symbol[row.symbol] = row
        data = [
            _serialize_intraday_snapshot(latest_by_symbol[symbol])
            for symbol in symbol_list
            if symbol in latest_by_symbol
        ]
        return {"data": data}
    finally:
        Session.remove()


def _load_intraday_snapshot_map(symbol_list, trade_date=None):
    """Return {symbol: serialized intraday snapshot} for the latest snapshot.

    When trade_date is given, only snapshots for that trading day are returned,
    so a stale snapshot from a previous noon run is not overlaid on the summary.
    """
    db = Session()
    try:
        query = db.query(AStockFearGreedIntraday).filter(
            AStockFearGreedIntraday.symbol.in_(symbol_list)
        )
        if trade_date is not None:
            query = query.filter(AStockFearGreedIntraday.trade_date == trade_date)
        rows = query.order_by(AStockFearGreedIntraday.snapshot_time.desc()).all()
        latest_by_symbol = {}
        for row in rows:
            if row.symbol not in latest_by_symbol:
                latest_by_symbol[row.symbol] = row
        return {
            symbol: _serialize_intraday_snapshot(latest_by_symbol[symbol])
            for symbol in symbol_list
            if symbol in latest_by_symbol
        }
    finally:
        Session.remove()


def _fetch_cnn_fear_greed():
    scraper = CNNFearGreedIndexScraper()
    try:
        return scraper.fetch_data()
    finally:
        scraper.db_session.close()

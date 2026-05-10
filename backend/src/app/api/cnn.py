from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from ...core.services.etf_fear_greed_clone_service import ETFFearGreedCloneCalculator
from ...core.services.fear_greed_clone_service import FearGreedCloneCalculator
from ...robot.cnn_fear_index import CNNFearGreedIndexScraper

router = APIRouter(prefix="/api/cnn")

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
    max_holdings: int = 40,
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
                max_holdings=max_holdings,
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
    max_holdings: int = 40,
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
                max_holdings=max_holdings,
                include_extended=include_extended,
                include_holdings_quotes=include_holdings_quotes,
            )
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取ETF实时恐贪指数失败: {str(e)}")


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
        return await run_in_threadpool(
            lambda: calculator.load_history_from_db(
                symbol=symbol,
                start_date=_parse_date(start_date),
                end_date=_parse_date(end_date),
                include_components=include_components,
                include_latest_holdings=include_latest_holdings,
            )
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取ETF独立恐贪历史失败: {str(e)}")


@router.post("/etf-fear-greed-clone/backfill")
async def backfill_etf_fear_greed_clone(
    symbol: str = "SOXX.US",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    history_days: int = 1200,
    score_window: int = 252,
    min_periods: int = 120,
    max_holdings: int = 40,
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
                max_holdings=max_holdings,
                use_historical_holdings=use_historical_holdings,
            )
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"回跑ETF独立恐贪指数失败: {str(e)}")


def _parse_date(value: Optional[str]):
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def _fetch_cnn_fear_greed():
    scraper = CNNFearGreedIndexScraper()
    try:
        return scraper.fetch_data()
    finally:
        scraper.db_session.close()

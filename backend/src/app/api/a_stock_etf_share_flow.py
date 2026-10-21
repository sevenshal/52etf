"""宽基 ETF 净申赎压力：独立研究接口，不参与自算贪恐总分。"""
from fastapi import APIRouter, Depends, HTTPException, Query

from .account import valid_admin_account
from ...core.services.a_stock_etf_share_flow_service import AStockEtfShareFlowService


router = APIRouter(prefix="/api/a-stock-etf-share-flow", tags=["A-stock ETF Share Flow"])


@router.get("/research")
def get_a_stock_etf_share_flow_research(
    history_days: int = Query(504, ge=30, le=2000),
    _: str = Depends(valid_admin_account),
):
    try:
        return AStockEtfShareFlowService().get_payload(history_days=history_days)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"宽基ETF份额数据暂不可用: {exc}")

"""管理员专用：价值投资选股扫描(质量闸门 + 估值偏离度 + 潜在回报率)接口。"""

from datetime import date

from fastapi import APIRouter, Depends, Query

from ...core.services.value_investing_scanner import screen_value_investing_candidates
from .account import valid_admin_account

router = APIRouter(prefix="/api/value-investing", tags=["Value Investing"])


@router.get("/screen")
def screen_value_investing(
    min_total_mv: float | None = Query(default=None, ge=0, description="最小总市值(万元)"),
    exclude_st: bool = Query(default=True, description="是否剔除 ST/*ST 股票"),
    top_n: int = Query(default=100, ge=1, le=1000, description="返回候选数量上限"),
    min_avg_roe: float | None = Query(default=None, description="一般工商业质量闸门：近5年平均ROE下限(%)"),
    min_ocf_to_np: float | None = Query(default=None, description="质量闸门：经营现金流/净利润比下限"),
    min_fcf_positive_years: int | None = Query(default=None, description="质量闸门：近5年自由现金流为正的最少年数"),
    max_debt_to_assets: float | None = Query(default=None, description="质量闸门：资产负债率上限(%)"),
    min_avg_roe_financial: float | None = Query(default=None, description="银行/保险/证券质量闸门：近5年平均ROE下限(%)"),
    as_of: date | None = Query(default=None),
    _: str = Depends(valid_admin_account),
):
    """跑一次全市场价值投资扫描，按潜在 return% 排序返回候选列表。

    数据来自 DuckDB 分析库中的财务报表缓存(利润表/资产负债表/现金流量表/财务指标)，
    需要先跑过一轮 A 股基础数据同步才有数据；同步前会返回 status=fundamentals_not_synced。
    """
    overrides = {
        key: value
        for key, value in {
            "min_avg_roe": min_avg_roe,
            "min_ocf_to_np": min_ocf_to_np,
            "min_fcf_positive_years": min_fcf_positive_years,
            "max_debt_to_assets": max_debt_to_assets,
            "min_avg_roe_financial": min_avg_roe_financial,
        }.items()
        if value is not None
    }
    return screen_value_investing_candidates(
        min_total_mv=min_total_mv,
        exclude_st=exclude_st,
        quality_overrides=overrides or None,
        top_n=top_n,
        as_of=as_of,
    )

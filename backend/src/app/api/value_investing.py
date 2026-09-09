"""管理员专用：价值投资选股扫描(ROIC-WACC质量闸门 + DCF内在价值/潜在回报率)接口。"""

from datetime import date

from fastapi import APIRouter, Depends, Query

from ...core.services.value_investing_scanner import (
    DEFAULT_EQUITY_RISK_PREMIUM,
    DEFAULT_TERMINAL_GROWTH_RATE,
    evaluate_value_investing_stock,
    screen_value_investing_candidates,
)
from .account import valid_account, valid_admin_account

router = APIRouter(prefix="/api/value-investing", tags=["Value Investing"])


@router.get("/screen")
def screen_value_investing(
    min_total_mv: float | None = Query(default=None, ge=0, description="最小总市值(万元)"),
    exclude_st: bool = Query(default=True, description="是否剔除 ST/*ST 股票"),
    top_n: int = Query(default=100, ge=1, le=1000, description="返回候选数量上限"),
    min_roic_wacc_spread_pct: float | None = Query(
        default=None, description="一般工商业质量闸门：近5年平均ROIC与WACC的价差下限(百分点)"
    ),
    min_ocf_to_np: float | None = Query(default=None, description="质量闸门：经营现金流/净利润比下限"),
    min_fcf_positive_years: int | None = Query(default=None, description="质量闸门：近5年FCFF为正的最少年数"),
    max_debt_to_assets: float | None = Query(default=None, description="质量闸门：资产负债率上限(%)"),
    min_avg_roe_financial: float | None = Query(default=None, description="银行/保险/证券质量闸门：近5年平均ROE下限(%)"),
    min_value_growth_pct: float | None = Query(
        default=None, description="质量闸门：最新年报算出的内在价值相比去年同期的同比增幅下限(%)，默认0即不能倒退"
    ),
    risk_free_rate_pct: float | None = Query(
        default=None, description="WACC假设：无风险利率(%)，留空自动取中债国债收益率曲线10年期利率"
    ),
    equity_risk_premium_pct: float = Query(
        default=DEFAULT_EQUITY_RISK_PREMIUM * 100.0, description="WACC假设：股权风险溢价(%)"
    ),
    terminal_growth_rate_pct: float = Query(
        default=DEFAULT_TERMINAL_GROWTH_RATE * 100.0, description="DCF假设：永续增长率(%)"
    ),
    as_of: date | None = Query(default=None),
    _: str = Depends(valid_admin_account),
):
    """跑一次全市场价值投资扫描，按 DCF(非金融)/合理市净率(金融)测算的潜在 return% 排序返回候选列表。

    数据来自 DuckDB 分析库中的财务报表缓存(利润表/资产负债表/现金流量表/财务指标)，
    需要先跑过一轮 A 股基础数据同步才有数据；同步前会返回 status=fundamentals_not_synced。
    """
    overrides = {
        key: value
        for key, value in {
            "min_roic_wacc_spread_pct": min_roic_wacc_spread_pct,
            "min_ocf_to_np": min_ocf_to_np,
            "min_fcf_positive_years": min_fcf_positive_years,
            "max_debt_to_assets": max_debt_to_assets,
            "min_avg_roe_financial": min_avg_roe_financial,
            "min_value_growth_pct": min_value_growth_pct,
        }.items()
        if value is not None
    }
    return screen_value_investing_candidates(
        min_total_mv=min_total_mv,
        exclude_st=exclude_st,
        quality_overrides=overrides or None,
        top_n=top_n,
        as_of=as_of,
        risk_free_rate=(risk_free_rate_pct / 100.0) if risk_free_rate_pct is not None else None,
        equity_risk_premium=equity_risk_premium_pct / 100.0,
        terminal_growth_rate=terminal_growth_rate_pct / 100.0,
    )


@router.get("/stock/{ts_code}")
def value_investing_stock_profile(
    ts_code: str,
    as_of: date | None = Query(default=None),
    risk_free_rate_pct: float | None = Query(
        default=None, description="WACC假设：无风险利率(%)，留空自动取中债国债收益率曲线10年期利率"
    ),
    equity_risk_premium_pct: float = Query(
        default=DEFAULT_EQUITY_RISK_PREMIUM * 100.0, description="WACC假设：股权风险溢价(%)"
    ),
    terminal_growth_rate_pct: float = Query(
        default=DEFAULT_TERMINAL_GROWTH_RATE * 100.0, description="DCF假设：永续增长率(%)"
    ),
    _: str = Depends(valid_account),
):
    """单只股票的价值投资基本面画像，供个股详情页展示。

    走的是全市场扫描同一套 ROIC-WACC 质量闸门与 DCF/合理市净率估值，只是把范围收敛
    到这一只、并且质量闸门没通过时也把估值算完（详情页要看到差多少，而不只是"没过"）。
    与 /screen 不同，这里只读单只股票，不是管理员专用的全市场重扫描。
    """
    return evaluate_value_investing_stock(
        ts_code,
        as_of=as_of,
        risk_free_rate=(risk_free_rate_pct / 100.0) if risk_free_rate_pct is not None else None,
        equity_risk_premium=equity_risk_premium_pct / 100.0,
        terminal_growth_rate=terminal_growth_rate_pct / 100.0,
    )

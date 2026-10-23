from __future__ import annotations

import numpy as np
import pandas as pd

from src.core.services.a_stock_etf_share_flow_service import AStockEtfShareFlowService


def _frames(days: int = 410):
    dates = pd.bdate_range("2023-01-02", periods=days)
    codes = ["510300.SH", "510050.SH", "510500.SH"]
    shares = []
    prices = []
    for code_index, code in enumerate(codes):
        for index, day in enumerate(dates):
            # 每只基金都随日期变化，确保份额变化会进入聚合值。
            shares.append({"ts_code": code, "trade_date": day, "fd_share": 100000 + index * (20 + code_index)})
            prices.append({"ts_code": code, "trade_date": day, "close": 3 + code_index + index * .002})
    benchmark = pd.DataFrame({
        "trade_date": dates,
        "close": 4000 + np.sin(np.arange(days) / 9) * 50 + np.arange(days) * .2,
    })
    return pd.DataFrame(shares), pd.DataFrame(prices), benchmark


def test_share_flow_score_uses_only_prior_history_and_requires_basket_coverage():
    shares, prices, _ = _frames()
    flow = AStockEtfShareFlowService.calculate_flow_frame(shares, prices)

    assert not flow.empty
    assert flow.iloc[0]["etf_count"] == 3
    assert flow.iloc[8]["flow_20d_amount"] != flow.iloc[8]["flow_20d_amount"]
    assert flow.iloc[19]["flow_20d_amount"] > 0
    # 252 日滚动分位数至少要有 120 个 5 日观测，不能在样本开头产生分数。
    assert flow.iloc[100]["score"] != flow.iloc[100]["score"]
    assert flow.iloc[-1]["score"] == flow.iloc[-1]["score"]


def test_share_flow_analysis_has_forward_returns_and_hindsight_phase_labels():
    shares, prices, benchmark = _frames()
    flow = AStockEtfShareFlowService.calculate_flow_frame(shares, prices)
    result = AStockEtfShareFlowService.analyze(flow, benchmark)

    assert len(result["future_returns"]) == 5
    assert {"return_20d_mean_pct", "return_60d_mean_pct", "return_120d_mean_pct", "return_200d_mean_pct"}.issubset(
        result["future_returns"][0]
    )
    assert {item["phase"] for item in result["phase_distribution"]} == {"top", "bottom"}
    assert result["sample_count"] > 100


def test_symbol_flow_has_individual_five_day_net_amount_and_ratio():
    shares, prices, _ = _frames()
    result = AStockEtfShareFlowService.calculate_symbol_flow_frame(shares, prices)

    latest = result[result["ts_code"] == "510300.SH"].iloc[-1]
    assert latest["estimated_flow_amount"] > 0
    assert latest["flow_ratio"] > 0
    assert latest["flow_5d_amount"] > 0
    assert latest["flow_5d_ratio"] > 0
    assert latest["share_change_ratio"] > 0
    assert latest["share_ratio_20d"] > 1

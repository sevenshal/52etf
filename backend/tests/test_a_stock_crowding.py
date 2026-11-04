from src.core.services.a_stock_crowding import _rolling_percentiles


def test_crowding_score_renormalizes_when_a_component_is_missing():
    rows = [
        {
            "amount_to_float_mv_pct": 1.0, "turnover_rate_pct": 2.0, "leverage_ratio_pct": 0.5,
            "main_flow_ratio_pct": -0.1, "up_ratio_pct": 40.0, "pb": 1.2, "amount_hhi": 30.0,
        },
        {
            "amount_to_float_mv_pct": 2.0, "turnover_rate_pct": 3.0, "leverage_ratio_pct": None,
            "main_flow_ratio_pct": 0.2, "up_ratio_pct": 60.0, "pb": 1.5, "amount_hhi": 40.0,
        },
    ]

    _rolling_percentiles(rows)

    assert rows[0]["crowding_score"] == 100.0
    assert rows[1]["leverage_ratio_pct_percentile"] is None
    # 第二行的其余所有项目都是两天中的最高值；缺失杠杆项不能把总分拖低。
    assert rows[1]["crowding_score"] == 100.0

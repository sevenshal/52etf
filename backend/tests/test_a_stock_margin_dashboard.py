from datetime import date, timedelta

from src.core.services.a_stock_margin_dashboard import _rolling_percentile


def test_rolling_percentile_keeps_only_latest_five_year_window():
    rows = [
        {"trade_date": date(2020, 1, 1) + timedelta(days=index), "ratio": float(index)}
        for index in range(1251)
    ]

    _rolling_percentile(rows, "ratio")

    # 第 1251 条的窗口应为 1..1250，而非仍包含最早的 0。
    assert rows[0]["ratio_percentile_5y"] == 100.0
    assert rows[-1]["ratio_percentile_5y"] == 100.0


def test_rolling_percentile_handles_duplicate_values():
    rows = [{"ratio": 1.0}, {"ratio": 2.0}, {"ratio": 2.0}, {"ratio": None}]

    _rolling_percentile(rows, "ratio")

    assert rows[0]["ratio_percentile_5y"] == 100.0
    assert rows[1]["ratio_percentile_5y"] == 100.0
    assert rows[2]["ratio_percentile_5y"] == 100.0
    assert rows[3]["ratio_percentile_5y"] is None

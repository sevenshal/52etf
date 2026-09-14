from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch

from src.app.api import soxl_fear_backtest
from src.core.services import index_valuation
from src.core.services.us_index_valuation import build_us_valuation_rows
from src.robot.a_stock_fear_strategy_trader import _signal_day_valuation

LABOR_DAY_2026 = date(2026, 9, 7)


def _is_us_trading_day(day):
    return day.weekday() < 5 and day != LABOR_DAY_2026


def _row(day, lo=110.0, hi=130.0, nav=100.0, hour=5, weight=0.96):
    analysis_date = date.fromisoformat(day)
    computed_at = datetime(analysis_date.year, analysis_date.month, analysis_date.day, hour, 30)
    return SimpleNamespace(
        date=analysis_date, current_price=nav, market_price=nav,
        forward_stocks_value_lo=lo, forward_stocks_value_hi=hi, forward_stocks_weight=weight,
        min_fair_value_date=None, max_fair_value_date=None, created_at=computed_at, updated_at=computed_at,
    )


def test_rows_map_to_previous_us_close_and_drop_non_trading_days():
    with patch("src.core.services.us_index_valuation._is_us_trading_day", _is_us_trading_day):
        rows = build_us_valuation_rows([
            _row("2026-09-08"),  # 上海周二清晨 → 美股 9/7 劳动节休市，重复前一收盘，丢弃
            _row("2026-09-09"),  # → 美股 9/8 收盘
            _row("2026-09-12"),  # 上海周六 → 美股 9/11 周五收盘
            _row("2026-09-13"),  # → 9/12 周六，丢弃
            _row("2026-09-14"),  # → 9/13 周日，丢弃
        ])
    assert [row["date"] for row in rows] == [date(2026, 9, 8), date(2026, 9, 11)]
    # 有估值成分的公允价值中枢 120 ÷ 持仓净值 100 − 1
    assert rows[0]["current_gap_pct"] == 20.0


def test_coverage_collapse_is_dropped_and_forward_filled():
    # 2026-07-23 EVC 只同步到 360 只股票：QQQ 有估值成分的覆盖率从 0.96 掉到 0.11，偏离从 +9.5% 跳到 −11.5%
    with patch("src.core.services.us_index_valuation._is_us_trading_day", _is_us_trading_day):
        rows = build_us_valuation_rows([
            _row("2026-07-21", lo=110.0, hi=130.0),               # 美股 7/20
            _row("2026-07-22", lo=112.0, hi=132.0),               # 7/21
            _row("2026-07-23", lo=60.0, hi=70.0, weight=0.106),   # 7/22 覆盖率骤降 → 沿用 7/21
            _row("2026-07-24", lo=114.0, hi=134.0),               # 7/23 恢复
        ])
    assert [row["date"] for row in rows] == [date(2026, 7, 20), date(2026, 7, 21), date(2026, 7, 22), date(2026, 7, 23)]
    assert rows[2]["is_ffilled"] and rows[2]["current_gap_pct"] == rows[1]["current_gap_pct"] == 22.0
    assert rows[2]["coverage_ratio"] == 0.106
    assert not rows[3]["is_ffilled"] and rows[3]["current_gap_pct"] == 24.0


def test_persistently_lower_coverage_is_accepted_after_three_days():
    # 连续多天覆盖率都低 = 新常态，不是同步不完整：最多 ffill 3 天，之后接受新值
    with patch("src.core.services.us_index_valuation._is_us_trading_day", _is_us_trading_day):
        rows = build_us_valuation_rows([
            _row("2026-06-02"),                                   # 美股 6/1
            *[_row(f"2026-06-0{day}", lo=100.0, hi=100.0, weight=0.5) for day in range(3, 8)],  # 6/2 ~ 6/5（6/6 周六丢弃）
        ])
    assert [row["is_ffilled"] for row in rows] == [False, True, True, True, False]
    assert rows[-1]["current_gap_pct"] == 0.0


def test_intraday_rerun_is_not_a_close_snapshot():
    # 上海 22:30 重跑已进入美股当日交易时段，价格不是前一收盘
    with patch("src.core.services.us_index_valuation._is_us_trading_day", _is_us_trading_day):
        assert build_us_valuation_rows([_row("2026-09-10", hour=22)]) == []
        assert len(build_us_valuation_rows([_row("2026-09-10", hour=9)])) == 1


def test_dispatcher_routes_us_and_a_share_symbols():
    with patch.object(index_valuation, "load_us_index_valuation_position_history", return_value={"market": "us"}) as us_loader, \
            patch.object(index_valuation, "load_a_stock_index_valuation_position_history", return_value={"market": "a"}):
        assert index_valuation.load_index_valuation_position_history("qqq.us") == {"market": "us"}
        assert index_valuation.load_index_valuation_position_history("000015.SH") == {"market": "a"}
    us_loader.assert_called_once_with("QQQ.US", end_date=None)


def test_backtest_uses_index_valuation_for_us_fear_sources():
    signal_day = date(2026, 9, 11)
    with patch.object(soxl_fear_backtest, "load_index_valuation_position_history", return_value={}) as loader:
        soxl_fear_backtest._fetch_valuation_positions("qqq_clone", signal_day)
        soxl_fear_backtest._fetch_valuation_positions("cnn", signal_day)
    loader.assert_called_once_with("QQQ.US", end_date=signal_day)


def test_live_us_leg_only_not_ready_on_its_own_trading_day():
    positions = {date(2026, 9, 10): {252: 30.0, 504: 35.0}}
    with patch("src.robot.a_stock_fear_strategy_trader.load_index_valuation_position_history", return_value=positions):
        # A股交易日但美股休市：这条腿没有信号日恐贪、本来不出信号，不算未就绪
        assert _signal_day_valuation("qqq_clone", date(2026, 9, 11), 252, has_signal_day_fear=False) == (None, True)
        # 有信号日恐贪但估值还没算出来 → 未就绪
        assert _signal_day_valuation("qqq_clone", date(2026, 9, 11), 252, has_signal_day_fear=True) == (None, False)
        assert _signal_day_valuation("qqq_clone", date(2026, 9, 10), 504) == (35.0, True)
        assert _signal_day_valuation("cnn", date(2026, 9, 10), 252) == (None, True)

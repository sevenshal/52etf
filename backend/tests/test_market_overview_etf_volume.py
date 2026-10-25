"""国家队宽基 ETF 合并累计量比。"""

from datetime import date

from src.core.services.market_overview import build_national_team_etf_volume_compare
from src.core.services.email_settings import EMAIL_SCENARIO_BY_KEY
from src.core.services import national_team_etf_volume as universe_service
from src.core.services.national_team_etf_volume import (
    NATIONAL_TEAM_ETF_GROUPS,
)


def _metric(volume, baseline):
    return {"volume": volume, "volume_baseline_20d": baseline, "volume_ratio_mode": "same_time_cumulative_20d"}


def test_national_team_etf_volume_aggregates_same_index_before_ratio():
    metrics = {
        "510050.SH": _metric(140, 100),
        # 沪深300 四只必须先合并：(140+70+210+280)/(100+50+150+200)=1.4，不能平均各自量比。
        "510300.SH": _metric(140, 100),
        "510310.SH": _metric(70, 50),
        "159919.SZ": _metric(210, 150),
        "510330.SH": _metric(280, 200),
        "588000.SH": _metric(140, 100),
        "588080.SH": _metric(140, 100),
        "159915.SZ": _metric(140, 100),
        "159952.SZ": _metric(140, 100),
        "510880.SH": _metric(140, 100),
        "515080.SH": _metric(140, 100),
        "515180.SH": _metric(140, 100),
    }

    result = build_national_team_etf_volume_compare(
        metrics, date(2026, 10, 23), NATIONAL_TEAM_ETF_GROUPS,
    )

    hs300 = next(item for item in result["groups"] if item["name"] == "沪深300ETF")
    assert hs300["current_volume"] == 700
    assert hs300["baseline_volume"] == 500
    assert hs300["volume_ratio"] == 1.4
    # 阈值是严格超过 1.4 倍，刚好 1.4 不应触发。
    assert result["triggered"] is False


def test_national_team_etf_volume_requires_all_groups_ready_and_above_threshold():
    metrics = {
        "510050.SH": _metric(141, 100),
        "510300.SH": _metric(141, 100),
        "510310.SH": _metric(141, 100),
        "510330.SH": _metric(141, 100),
        "159919.SZ": _metric(141, 100),
        "588000.SH": _metric(141, 100),
        "588080.SH": _metric(141, 100),
        "159915.SZ": _metric(141, 100),
        "159952.SZ": _metric(141, 100),
        "510880.SH": _metric(141, 100),
        "515080.SH": _metric(141, 100),
        "515180.SH": _metric(141, 100),
    }

    assert build_national_team_etf_volume_compare(metrics, groups=NATIONAL_TEAM_ETF_GROUPS)["triggered"] is True
    del metrics["510880.SH"]
    assert build_national_team_etf_volume_compare(metrics, groups=NATIONAL_TEAM_ETF_GROUPS)["triggered"] is False


def test_national_team_alert_checks_combined_group_ratio_not_every_etf_ratio():
    """沪深300组内允许个别 ETF 未放量，告警看合并后的五个指数分组。"""
    metrics = {
        "510050.SH": _metric(150, 100),
        # 510310 单只仅 0.5 倍，但沪深300合并后为 (300+50+150+150)/400 = 1.625 倍。
        "510300.SH": _metric(300, 100),
        "510310.SH": _metric(50, 100),
        "510330.SH": _metric(150, 100),
        "159919.SZ": _metric(150, 100),
        "588000.SH": _metric(150, 100),
        "588080.SH": _metric(150, 100),
        "159915.SZ": _metric(150, 100),
        "159952.SZ": _metric(150, 100),
        "510880.SH": _metric(150, 100),
        "515080.SH": _metric(150, 100),
        "515180.SH": _metric(150, 100),
    }

    result = build_national_team_etf_volume_compare(metrics, groups=NATIONAL_TEAM_ETF_GROUPS)
    assert next(item for item in result["groups"] if item["name"] == "沪深300ETF")["volume_ratio"] == 1.625
    assert result["triggered"] is True


def test_universe_is_the_5_day_average_net_asset_screened_passive_etfs():
    assert universe_service.get_national_team_etf_groups() == (
        ("上证50ETF", ("510050.SH",)),
        ("沪深300ETF", ("510300.SH", "510310.SH", "510330.SH", "159919.SZ")),
        ("科创50ETF", ("588000.SH", "588080.SH")),
        ("创业板ETF", ("159915.SZ", "159952.SZ")),
        ("红利ETF", ("510880.SH", "515080.SH", "515180.SH")),
    )


def test_synchronous_volume_alert_email_scenario_is_registered():
    assert EMAIL_SCENARIO_BY_KEY["market_etf_volume_bottom_alert"]["category"] == "提醒"

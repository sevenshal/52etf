import pytest

from src.core.services.volume_metrics import calculate_log_volume_z_score, calculate_volume_ratio


def test_volume_ratio_20d_excludes_latest_day():
    previous = [float(value) for value in range(10, 210, 10)]

    ratio, average = calculate_volume_ratio(
        220.0,
        previous,
    )

    assert average == pytest.approx(105.0)
    assert ratio == pytest.approx(220.0 / 105.0)


def test_volume_ratio_20d_requires_twenty_previous_days():
    previous = [100.0 for _ in range(19)]

    assert calculate_volume_ratio(220.0, previous) == (None, None)


def test_log_volume_z_positive_for_volume_expansion():
    # 前 21 天成交量稳定在 100 附近（轻微波动），今天放量到 150 → z > 1
    previous = [100.0 + (index % 3 - 1) for index in range(21)]

    z = calculate_log_volume_z_score(150.0, previous)

    assert z is not None
    assert z > 1.0


def test_log_volume_z_negative_for_volume_shrink():
    # 前 21 天成交量稳定在 100 附近，今天缩量到 60 → z < -0.25
    previous = [100.0 + (index % 3 - 1) for index in range(21)]

    z = calculate_log_volume_z_score(60.0, previous)

    assert z is not None
    assert z < -0.25


def test_log_volume_z_requires_full_window():
    previous = [100.0 for _ in range(20)]

    assert calculate_log_volume_z_score(150.0, previous) is None


def test_log_volume_z_rejects_non_positive_volumes():
    previous = [100.0 for _ in range(21)]

    assert calculate_log_volume_z_score(0.0, previous) is None
    assert calculate_log_volume_z_score(None, previous) is None

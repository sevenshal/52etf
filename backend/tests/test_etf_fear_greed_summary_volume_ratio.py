import pytest

from src.core.services.volume_metrics import calculate_volume_ratio


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

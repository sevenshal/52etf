"""后端技术指标必须与个股详情页 K 线图的前端算法逐根一致。

夹具由前端代码生成（frontend/scripts/generate-indicator-parity-fixture.mjs），前端测试
保证夹具就是当前 JS 的输出。这里断言 Python 移植对同一组 K 线算出同样的结果。
"""

import json
import math
from pathlib import Path

import pytest

from src.core.services.stock_system import indicators

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "stock_indicator_parity.json").read_text(encoding="utf-8")
)
KLINES = FIXTURE["klines"]
PARAMS = FIXTURE["params"]


def _assert_same(actual, expected, path="root"):
    """浮点数按 1e-9 相对误差比较（log10/pow 在不同运行时可能差最后一位），其余严格相等。"""
    if isinstance(expected, dict):
        assert isinstance(actual, dict), path
        assert set(actual) == set(expected), f"{path}: {sorted(set(actual) ^ set(expected))}"
        for key in expected:
            _assert_same(actual[key], expected[key], f"{path}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list) and len(actual) == len(expected), path
        for index, (left, right) in enumerate(zip(actual, expected)):
            _assert_same(left, right, f"{path}[{index}]")
    elif isinstance(expected, bool) or expected is None or isinstance(expected, str):
        assert actual == expected, f"{path}: {actual!r} != {expected!r}"
    elif isinstance(expected, (int, float)):
        assert isinstance(actual, (int, float)) and not isinstance(actual, bool), path
        assert math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-12), f"{path}: {actual!r} != {expected!r}"
    else:  # pragma: no cover
        raise AssertionError(f"{path}: unexpected {type(expected)}")


def test_chart_pipeline_matches_frontend():
    result = indicators.compute_chart_indicators(
        KLINES,
        support_resistance_window=PARAMS["supportResistanceWindow"],
        volume_std_multiplier=PARAMS["volumeStdDevMultiplier"],
        enable_turnover_decay=PARAMS["enableTurnoverDecay"],
    )
    fields = FIXTURE["chart"][0].keys()
    actual = [{key: row.get(key) for key in fields} for row in result["klines"]]
    _assert_same(actual, FIXTURE["chart"])
    # 夹具必须真的覆盖到有支撑压力、有九转计数的行，否则上面的比较是空转
    assert sum(1 for row in actual if row["support_resistance"]) > 50
    assert max(row["highCount"] for row in actual) >= 9
    assert max(row["lowCount"] for row in actual) >= 9


def test_support_resistance_without_turnover_decay_matches_frontend():
    rows = indicators.append_rolling_poc_support_resistance(
        KLINES, window=60, volume_std_multiplier=0.5, enable_turnover_decay=False
    )
    _assert_same([row["support_resistance"] for row in rows], FIXTURE["support_resistance_no_decay"])


@pytest.mark.parametrize(
    "params, key",
    [(None, "macd"), ({"fast": 5, "slow": 20, "signal": 7}, "macd_custom")],
)
def test_macd_matches_frontend(params, key):
    _assert_same(indicators.calculate_macd(KLINES, params), FIXTURE[key])


def test_output_start_index_only_computes_the_tail():
    full = indicators.append_rolling_poc_support_resistance(KLINES, window=125, min_periods=125)
    tail = indicators.append_rolling_poc_support_resistance(
        KLINES, window=125, min_periods=125, output_start_index=len(KLINES) - 1
    )
    assert all(row["support_resistance"] is None for row in tail[:-1])
    assert tail[-1]["support_resistance"] == full[-1]["support_resistance"]


def test_to_fixed_rounds_half_away_from_zero_like_javascript():
    # 0.125 在二进制下精确可表示：JS (0.125).toFixed(2) === "0.13"，Python round 给 0.12
    assert indicators._to_fixed(0.125, 2) == 0.13
    assert indicators._to_fixed(-0.125, 2) == -0.13
    # 1.005 实际存的是 1.00499999...，JS 给 "1.00"
    assert indicators._to_fixed(1.005, 2) == 1.0

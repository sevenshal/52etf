"""自算贪恐历史曲线底/顶信号（后端统一计算）测试。

覆盖 `compute_turn_signals`：
- ma5 均线型：MA5 上穿 + 近 5 日恐贪 <= 25 为底；MA5 下穿 + 近 5 日恐贪 >= 75 为顶。
- 量能型：恐贪 <= 30 且放量（log 量 > 过去 20 日均值 1.25 个标准差）为底；
  恐贪 >= 75 且缩量（log 量 < 过去 20 日均值 0.25 个标准差）为顶。
- 每类信号的顶/底分别独立 5 个交易日冷却，冷却期内不重复出同类信号。
"""

import datetime as dt

from src.core.services.etf_fear_greed_clone_service import (
    compute_turn_signals,
    load_turn_signal_params,
)


def _rows(scores, volumes):
    start = dt.date(2024, 1, 2)
    return [
        {
            "date": (start + dt.timedelta(days=index)).isoformat(),
            "score": score,
            "etf_price": {"volume": volume},
        }
        for index, (score, volume) in enumerate(zip(scores, volumes))
    ]


def _dates(result):
    return {date_str: [signal["kind"] for signal in signals] for date_str, signals in result.items()}


def test_ma5_bottom_on_rising_ma_after_extreme_fear():
    # 分数一路跌到 20 后回升：MA5 上穿且近 5 日恐贪 <= 25 → 均线底
    scores = [70, 68, 66, 64, 62, 60, 58, 56, 54, 52,
              50, 48, 46, 44, 42, 40, 38, 36, 34, 32,
              30, 28, 26, 24, 22, 20, 22, 24, 26, 28, 30, 32]
    result = compute_turn_signals(_rows(scores, [100.0] * len(scores)))

    assert _dates(result) == {"2024-01-30": ["ma5_bottom"]}
    signal = result["2024-01-30"][0]
    assert signal["value"] == 22.8
    assert signal["label"] == "均线底"


def test_ma5_top_on_falling_ma_after_extreme_greed():
    # 分数一路涨到 80 后回落：MA5 下穿且近 5 日恐贪 >= 75 → 均线顶
    scores = [30, 32, 34, 36, 38, 40, 42, 44, 46, 48,
              50, 52, 54, 56, 58, 60, 62, 64, 66, 68,
              70, 72, 74, 76, 78, 80, 78, 76, 74, 72, 70, 68]
    result = compute_turn_signals(_rows(scores, [100.0] * len(scores)))

    assert _dates(result) == {"2024-01-30": ["ma5_top"]}
    signal = result["2024-01-30"][0]
    assert signal["value"] == 77.2
    assert signal["label"] == "均线顶"


def test_ma5_requires_5_day_window():
    # 分数全为无效值时 MA5 无法计算，不产生信号
    scores = [None] * 30
    result = compute_turn_signals(_rows(scores, [100.0] * len(scores)))
    assert result == {}


def test_volume_bottom_on_extreme_fear_with_expansion():
    base = [100.0 + (index % 3 - 1) for index in range(20)]
    result = compute_turn_signals(_rows([35] * 20 + [30], base + [200.0]))

    assert _dates(result) == {"2024-01-22": ["volume_bottom"]}
    signal = result["2024-01-22"][0]
    assert signal["value"] == 30.0
    assert signal["label"] == "放量底"


def test_volume_top_on_extreme_greed_with_shrink():
    base = [100.0 + (index % 3 - 1) for index in range(20)]
    result = compute_turn_signals(_rows([70] * 20 + [78], base + [60.0]))

    assert _dates(result) == {"2024-01-22": ["volume_top"]}
    signal = result["2024-01-22"][0]
    assert signal["value"] == 78.0
    assert signal["label"] == "缩量顶"


def test_volume_bottom_requires_expansion():
    # 恐贪 <= 30 但成交量未放大 → 不触发
    base = [100.0 + (index % 3 - 1) for index in range(20)]
    result = compute_turn_signals(_rows([35] * 20 + [30], base + [100.0]))
    assert result == {}


def test_volume_bottom_requires_extreme_fear():
    # 放量但恐贪 > 30 → 不触发
    base = [100.0 + (index % 3 - 1) for index in range(20)]
    result = compute_turn_signals(_rows([40] * 20 + [40], base + [200.0]))
    assert result == {}


def test_volume_requires_twenty_prior_volumes():
    # 前 20 个有效成交量不足 → 无法算 z，不触发
    base = [100.0 + (index % 3 - 1) for index in range(10)]
    result = compute_turn_signals(_rows([35] * 10 + [30], base + [200.0]))
    assert result == {}


def test_missing_volume_days_skipped_for_z():
    # 过去 20 个有效成交量中夹着无成交日（volume=None），跳过 None 收集满 20 个仍正常触发
    base = [100.0 + (index % 3 - 1) for index in range(21)]
    base[10] = None
    result = compute_turn_signals(_rows([35] * 21 + [30], base + [200.0]))
    assert _dates(result) == {"2024-01-23": ["volume_bottom"]}


def test_same_kind_cooldown_suppresses_repeat_within_5_days():
    # 连续多次见底回升：每次信号后 5 个交易日冷却，不重复出同类信号；
    # 冷却满 5 个交易日后（信号日 +6）恢复出信号。
    scores = (
        [70, 68, 66, 64, 62, 60, 58, 56, 54, 52,
         50, 48, 46, 44, 42, 40, 38, 36, 34, 32,
         30, 28, 26, 24, 22, 20, 22, 24,
         21, 19, 21, 23, 26, 29, 31,             # 第一次底 2024-02-01
         28, 25, 22, 20, 23, 26, 29, 32, 35]     # 冷却结束后第二次底
    )
    result = _dates(compute_turn_signals(_rows(scores, [100.0] * len(scores))))

    assert "2024-02-01" in result
    # 2024-02-01 之后的 5 个交易日（02-02 ~ 02-06）无同类信号
    for blocked in ("2024-02-02", "2024-02-03", "2024-02-04", "2024-02-05", "2024-02-06"):
        assert blocked not in result
    # 冷却满后（02-07、02-13）恢复出信号
    assert result["2024-02-07"] == ["ma5_bottom"]
    assert result["2024-02-13"] == ["ma5_bottom"]


def test_cooldown_tracked_per_kind_and_direction():
    # 不同类/不同方向的信号冷却互相独立：均线底之后量能顶立即可出
    scores = [70] * 20 + [78]
    base = [100.0 + (index % 3 - 1) for index in range(20)]
    volumes = base + [60.0]
    result = _dates(compute_turn_signals(_rows(scores, volumes)))
    assert result == {"2024-01-22": ["volume_top"]}


def test_both_signal_types_on_same_day():
    # 同一天既满足均线底又满足量能底 → 两个信号都出（冷却按类独立）
    scores = [70, 68, 66, 64, 62, 60, 58, 56, 54, 52,
              50, 48, 46, 44, 42, 40, 38, 36, 34, 32,
              30, 28, 26, 24, 22, 20, 22, 24, 26, 28, 30]
    base = [100.0 + (index % 3 - 1) for index in range(20)]
    volumes = base + [100.0] * 8 + [200.0] + [100.0] * 2
    result = _dates(compute_turn_signals(_rows(scores, volumes)))
    assert result == {"2024-01-30": ["ma5_bottom", "volume_bottom"]}


def test_custom_volume_bottom_score_param():
    # 量能底恐贪阈值可配置：默认 30 不触发，配成 40 后触发
    base = [100.0 + (index % 3 - 1) for index in range(20)]
    rows = _rows([45] * 20 + [40], base + [200.0])
    assert compute_turn_signals(rows) == {}
    result = compute_turn_signals(rows, {"volume_bottom_score": 40.0})
    assert _dates(result) == {"2024-01-22": ["volume_bottom"]}


def test_custom_cooldown_days_param():
    # 冷却天数可配置：cooldown=2 时同类信号间隔 >2 个交易日（默认 5 时更稀）
    scores = (
        [70, 68, 66, 64, 62, 60, 58, 56, 54, 52,
         50, 48, 46, 44, 42, 40, 38, 36, 34, 32,
         30, 28, 26, 24, 22, 20, 22, 24,
         21, 19, 21, 23, 26, 29, 31,
         28, 25, 22, 20, 23, 26, 29, 32, 35]
    )
    rows = _rows(scores, [100.0] * len(scores))
    default_result = _dates(compute_turn_signals(rows))
    assert default_result == {
        "2024-02-01": ["ma5_bottom"],
        "2024-02-07": ["ma5_bottom"],
        "2024-02-13": ["ma5_bottom"],
    }
    short_result = _dates(compute_turn_signals(rows, {"cooldown_days": 2}))
    assert "2024-02-04" in short_result  # 默认 5 日冷却时该日无信号
    assert "2024-02-04" not in default_result


def test_custom_ma5_lookback_days_param():
    # MA5 回看天数可配置：上穿日分数 28（>25）但 5 日内有 20 → lookback=5 触发、lookback=1 不触发
    scores = [70, 68, 66, 64, 62, 60, 58, 56, 54, 52,
              50, 48, 46, 44, 42, 40, 38, 36, 34, 32,
              30, 28, 26, 24, 22, 20, 22, 24, 26, 28, 30, 32]
    rows = _rows(scores, [100.0] * len(scores))
    assert _dates(compute_turn_signals(rows, {"ma5_lookback_days": 5})) == {"2024-01-30": ["ma5_bottom"]}
    assert compute_turn_signals(rows, {"ma5_lookback_days": 1}) == {}


def test_load_turn_signal_params_defaults_without_row():
    # 表里没有 fear_greed_signal_configs 行时回退默认参数
    from src.core.database import Base, Session

    Base.metadata.create_all(Session().bind)
    params = load_turn_signal_params()
    assert params == {
        "ma5_bottom_score": 25.0,
        "ma5_top_score": 75.0,
        "ma5_lookback_days": 5,
        "volume_bottom_score": 30.0,
        "volume_top_score": 75.0,
        "volume_expand_z": 1.25,
        "volume_shrink_z": -0.25,
        "cooldown_days": 5,
    }


def test_load_turn_signal_params_reads_config_row():
    # 插入统一信号配置行后，参数从 fear_greed_signal_configs 表读取
    from src.core.database import Base, FearGreedSignalConfig, Session

    Base.metadata.create_all(Session().bind)
    with Session() as db:
        db.query(FearGreedSignalConfig).delete()
        db.add(FearGreedSignalConfig(
            ma5_bottom_score=22.0,
            ma5_top_score=80.0,
            ma5_lookback_days=3,
            volume_bottom_score=33.0,
            volume_top_score=78.0,
            volume_expand_std=1.5,
            volume_shrink_std=0.4,
            cooldown_days=2,
        ))
        db.commit()
    params = load_turn_signal_params()
    assert params == {
        "ma5_bottom_score": 22.0,
        "ma5_top_score": 80.0,
        "ma5_lookback_days": 3,
        "volume_bottom_score": 33.0,
        "volume_top_score": 78.0,
        "volume_expand_z": 1.5,
        "volume_shrink_z": -0.4,
        "cooldown_days": 2,
    }

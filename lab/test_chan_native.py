import chan_native
from chan_native import Center, Fractal, Kline, Segment, Stroke, build_centers, build_segments, calculate, classify_center_relations, detect_buy_sell, detect_fractals, promote_segments_to_strokes, remove_inclusion, replay_snapshots, recursive_levels


def test_inclusion_is_merged_left_to_right():
    out = remove_inclusion([Kline(0, 10, 8), Kline(1, 9, 9), Kline(2, 11, 9)])
    assert len(out) == 1
    assert out[0].high == 11 and out[0].low == 9


def test_fractal_is_only_confirmed_by_right_bar():
    fx = detect_fractals([Kline(0, 1, 0), Kline(1, 3, 1), Kline(2, 2, 1)])
    assert len(fx) == 1
    assert fx[0].mark == "top"
    assert fx[0].confirm_i == 2


def test_stroke_min_gap_uses_normalized_positions():
    # Alternating pivots are close in source ids but separated after inclusion.
    bars = [Kline(i, h, l) for i, (h, l) in enumerate(
        [(10, 8), (9, 7), (11, 9), (10, 8), (12, 10), (11, 9), (13, 11), (12, 10), (14, 12)]
    )]
    _, fractals, strokes, _ = calculate(bars, min_gap=2)
    assert all(f.pos >= 0 for f in fractals)
    assert all(s.end.pos - s.start.pos >= 2 for s in strokes)


def test_segment_requires_three_strokes_and_opposite_break():
    def fx(i, mark, p):
        return Fractal(i, mark, p, i + 1, i)

    strokes = [
        Stroke(fx(0, "bottom", 10), fx(4, "top", 15), "up", 5),
        Stroke(fx(4, "top", 15), fx(8, "bottom", 12), "down", 9),
        Stroke(fx(8, "bottom", 12), fx(12, "top", 18), "up", 13),
        Stroke(fx(12, "top", 18), fx(16, "bottom", 8), "down", 17),
    ]
    segments = build_segments(strokes)
    assert len(segments) == 1
    assert segments[0].direction == "up"
    assert segments[0].start_stroke == 0 and segments[0].end_stroke == 2
    assert segments[0].confirm_i == 17


def test_center_extends_then_records_break():
    segs = [
        Segment(0, 0, "down", 10, 5, 1),
        Segment(1, 1, "up", 9, 6, 2),
        Segment(2, 2, "down", 8, 5.5, 3),
        Segment(3, 3, "up", 8.5, 6.2, 4),
        Segment(4, 4, "down", 7.8, 5.8, 5),
        Segment(5, 5, "up", 12, 9, 6),
    ]
    centers = build_centers([], segs)
    assert len(centers) == 1
    assert centers[0].status == "broken"
    assert centers[0].end_stroke == 4
    assert centers[0].broken_at == 6
    assert centers[0].break_stroke == 5
    assert centers[0].break_direction == "up"


def test_third_buy_needs_departure_pullback_and_reconfirmation():
    centers = [Center(0, 2, 10, 8, 3, "bottom_candidate", "broken", 4, "formation", 3, "up")]
    segs = [
        Segment(0, 0, "down", 10, 8, 1), Segment(1, 1, "up", 10, 8, 2),
        Segment(2, 2, "down", 10, 8, 3), Segment(3, 3, "up", 13, 11, 5),
        Segment(4, 4, "down", 12, 10.5, 7), Segment(5, 5, "up", 14, 11, 9),
    ]
    events = detect_buy_sell([], segs, centers)
    assert [(e.kind, e.confirm_i) for e in events if e.kind == "三买"] == [("三买", 9)]


def test_first_buy_rejects_single_metric_slowdown():
    """A slower price/time ratio alone is not a divergence confirmation."""
    centers = [Center(0, 2, 10, 8, 3, "bottom_candidate", "broken", 4, "formation", 3, "down")]
    segs = [
        Segment(0, 0, "down", 10, 8, 1, power_price=4, power_time=1),
        Segment(1, 1, "up", 10, 8, 2, power_price=2, power_time=1),
        # Efficiency is lower, but both absolute displacement and range rise.
        Segment(2, 2, "down", 9, 7, 3, power_price=5, power_time=2),
    ]
    assert not [e for e in detect_buy_sell([], segs, centers) if e.kind == "一买"]


def test_first_buy_requires_post_center_divergent_down_legs():
    centers = [Center(0, 2, 10, 8, 3, "bottom_candidate", "broken", 4, "formation", 3, "down")]
    segs = [
        Segment(3, 3, "down", 9, 7.5, 5, power_price=4, power_time=4),
        Segment(4, 4, "up", 9, 8, 6, power_price=2, power_time=2),
        Segment(5, 5, "down", 8.5, 7, 7, power_price=2, power_time=4),
    ]
    events = detect_buy_sell([], segs, centers)
    assert [(e.kind, e.confirm_i) for e in events] == [("一买", 7)]


def test_first_sell_is_symmetric_to_first_buy():
    centers = [Center(0, 2, 12, 10, 3, "top_candidate", "broken", 4, "formation", 3, "up")]
    segs = [
        Segment(3, 3, "up", 14, 11, 5, power_price=4, power_time=4),
        Segment(4, 4, "down", 12, 11, 6, power_price=2, power_time=2),
        Segment(5, 5, "up", 15, 11.5, 7, power_price=2, power_time=4),
    ]
    events = detect_buy_sell([], segs, centers)
    assert [(e.kind, e.confirm_i) for e in events] == [("一卖", 7)]


def test_up_segment_ends_on_feature_sequence_top_fractal():
    def fx(i, mark, p):
        return Fractal(i, mark, p, i + 1, i)

    strokes = [
        Stroke(fx(0, "bottom", 10), fx(4, "top", 15), "up", 5),
        Stroke(fx(4, "top", 15), fx(8, "bottom", 11), "down", 9),
        Stroke(fx(8, "bottom", 11), fx(12, "top", 16), "up", 13),
        Stroke(fx(12, "top", 16), fx(16, "bottom", 13), "down", 17),
        Stroke(fx(16, "bottom", 13), fx(20, "top", 17), "up", 21),
        Stroke(fx(20, "top", 15), fx(24, "bottom", 12), "down", 25),
    ]
    segments = build_segments(strokes)
    assert len(segments) == 1
    assert segments[0].direction == "up"
    assert segments[0].end_stroke == 4
    assert segments[0].confirm_i == 25


def test_replay_does_not_expose_unconfirmed_fractal():
    bars = [Kline(0, 1, 0), Kline(1, 3, 1), Kline(2, 2.5, 0.5), Kline(3, 2, 0.7)]
    snapshots = list(replay_snapshots(bars))
    assert not any(f.i == 1 for f in snapshots[0]["fractals"])
    assert not any(f.i == 1 for f in snapshots[1]["fractals"])
    assert any(f.i == 1 and f.confirm_i == 2 for f in snapshots[2]["fractals"])


def test_center_relation_marks_newborn_and_expansion():
    centers = [Center(0, 2, 10, 8, 3), Center(4, 6, 14, 12, 7)]
    classify_center_relations(centers)
    assert centers[1].growth == "newborn"
    centers = [Center(0, 2, 10, 8, 3), Center(4, 6, 11, 9, 7)]
    classify_center_relations(centers)
    assert centers[0].growth == centers[1].growth == "expansion"


def test_recursive_level_exposes_events_with_confirmation_indices():
    bars = [Kline(i, 100 + (i % 6), 98 + (i % 6)) for i in range(40)]
    levels = recursive_levels(bars, levels=2, min_gap=1)
    assert all("events" in level for level in levels)
    for level in levels:
        assert all(event.confirm_i >= 0 for event in level["events"])


def test_recursive_promotion_preserves_direction_and_confirmation():
    lower = [
        Segment(i, i, "up" if i % 2 == 0 else "down", 10 + i, 5 + i, 20 + i,
                start_price=6 + i, end_price=9 + i)
        for i in range(4)
    ]
    promoted = promote_segments_to_strokes(lower)
    assert [s.direction for s in promoted] == ["up", "down", "up", "down"]
    assert [s.confirm_i for s in promoted] == [20, 21, 22, 23]


def test_center_zone_is_fixed_and_extremes_track_true_range():
    """The [zd, zg] overlap is frozen at formation; gg/dd widen on extension."""
    segs = [
        Segment(0, 0, "down", 12, 9, 1),
        Segment(1, 1, "up", 11, 8, 2),
        Segment(2, 2, "down", 13, 10, 3),
        Segment(3, 3, "up", 15, 7, 4),   # still intersects [10, 11], wider extremes
        Segment(4, 4, "down", 20, 18, 5),  # clean break above the fixed zone
    ]
    centers = build_centers([], segs)
    assert len(centers) == 1
    z = centers[0]
    assert (z.zg, z.zd) == (11, 10)          # min(12,11,13), max(9,8,10)
    assert z.end_stroke == 3
    assert (z.gg, z.dd) == (15, 7)           # true extremes across formation + extension
    assert z.status == "broken" and z.break_stroke == 4 and z.break_direction == "down"


def _fx(i, mark, p):
    return Fractal(i, mark, p, i + 1, i)


def test_gap_feature_sequence_makes_a_second_type_segment():
    strokes = [
        Stroke(_fx(0, "bottom", 0), _fx(4, "top", 10), "up", 5),
        Stroke(_fx(4, "top", 10), _fx(8, "bottom", 8), "down", 9),
        Stroke(_fx(8, "bottom", 8), _fx(12, "top", 12), "up", 13),
        Stroke(_fx(12, "top", 12), _fx(16, "bottom", 11), "down", 17),   # gap vs stroke 1
        Stroke(_fx(16, "bottom", 11), _fx(20, "top", 11.5), "up", 21),
        Stroke(_fx(20, "top", 11.5), _fx(24, "bottom", 10.5), "down", 25),
        Stroke(_fx(24, "bottom", 10.5), _fx(28, "top", 11.8), "up", 29),
        Stroke(_fx(28, "top", 11.8), _fx(32, "bottom", 10), "down", 33),
    ]
    segments = build_segments(strokes)
    assert len(segments) == 1
    assert segments[0].direction == "up"
    assert segments[0].partition == "second"
    assert segments[0].end_stroke == 4
    assert segments[0].confirm_i == 33   # confirmed only after three further strokes


def test_first_buy_requires_center_boundary_break():
    """Divergence alone is not enough; the leg must break the center's low."""
    centers = [Center(0, 2, 10, 8, 3, "bottom_candidate", "broken", 4, "formation", 3, "down")]
    segs = [
        Segment(3, 3, "down", 9, 8.5, 5, power_price=4, power_time=4),   # low 8.5 >= zd 8
        Segment(4, 4, "up", 9, 8.7, 6, power_price=2, power_time=2),
        Segment(5, 5, "down", 9, 8.2, 7, power_price=2, power_time=4),
    ]
    assert not [e for e in detect_buy_sell([], segs, centers) if e.kind == "一买"]


def test_first_buy_uses_macd_area_divergence_when_bars_given(monkeypatch):
    centers = [Center(0, 2, 10, 8, 3, "bottom_candidate", "broken", 4, "formation", 3, "down")]
    strokes = [
        Stroke(_fx(0, "bottom", 8), _fx(5, "top", 9), "up", 6),
        Stroke(_fx(5, "top", 9), _fx(9, "bottom", 8), "down", 10),
        Stroke(_fx(9, "bottom", 8), _fx(14, "top", 9), "up", 15),
        Stroke(_fx(14, "top", 9), _fx(24, "bottom", 7), "down", 25),      # leg a: bars 14..24
        Stroke(_fx(24, "bottom", 7), _fx(34, "top", 8.5), "up", 35),
        Stroke(_fx(34, "top", 8.5), _fx(44, "bottom", 6), "down", 45),    # leg b: bars 34..44
    ]
    segs = [
        Segment(3, 3, "down", 9, 7, 25, start_price=9, end_price=7),
        Segment(4, 4, "up", 8.5, 7, 35, start_price=7, end_price=8.5),
        Segment(5, 5, "down", 8.5, 6, 45, start_price=8.5, end_price=6),
    ]
    hist = [0.0] * 46
    for k in range(14, 25):
        hist[k] = -1.0                      # area_a = 11
    for k in range(34, 45):
        hist[k] = -0.2                      # area_b = 2.2  (weaker second leg, new low)
    monkeypatch.setattr(chan_native, "macd_histogram", lambda bars, **kw: hist)
    bars = [Kline(i, 1.0, 0.0) for i in range(46)]

    hits = [e for e in detect_buy_sell(strokes, segs, centers, bars=bars) if e.kind == "一买"]
    assert hits and "MACD面积" in hits[0].detail

    for k in range(34, 45):
        hist[k] = -2.0                      # stronger second leg -> no 背驰
    assert not [e for e in detect_buy_sell(strokes, segs, centers, bars=bars) if e.kind == "一买"]


def test_recursive_levels_tag_center_level():
    bars = [Kline(i, 100 + (i % 7) * 2, 96 + (i % 7) * 2) for i in range(60)]
    levels = recursive_levels(bars, levels=2, min_gap=1)
    for depth, level in enumerate(levels):
        assert all(z.level == depth for z in level["centers"])


def test_macd_histogram_is_indexed_by_original_bar_id():
    """Inclusion-merged bars have gaps in ``Kline.i``; the histogram must stay
    addressable by that id, forward-filling the gaps, so ``hist[stroke.i]``
    lines up with the MACD-area window."""
    from chan_native import macd_histogram

    dense = [Kline(i, 10 + i * 0.1, 9 + i * 0.1, close=10 + i * 0.1) for i in range(40)]
    gappy = [b for b in dense if b.i not in {5, 6, 17, 18, 19, 30}]  # merged-away ids
    h_dense = macd_histogram(dense)
    h_gappy = macd_histogram(gappy)
    assert len(h_dense) == 40
    assert len(h_gappy) == 40  # length follows max id + 1, not len(bars)
    # forward-filled gaps keep the series close to the dense one
    assert max(abs(a - b) for a, b in zip(h_dense, h_gappy)) < 0.05


def test_buy_sell_events_are_stable_under_inclusion_gaps():
    """detect_buy_sell must give the same MACD-area events whether the caller
    passes the dense bars or only the subset that survived inclusion (whose
    ``Kline.i`` values are gappy but still index the histogram correctly)."""
    import math

    dense = [
        Kline(i, high=101 + 4 * math.sin(i / 23), low=99 + 4 * math.sin(i / 23),
              close=100 + 4 * math.sin(i / 23) + (i / 400 if i < 700 else 1.75 - i / 900))
        for i in range(1100)
    ]
    _n, _fx_, strokes, centers = calculate(dense, min_gap=4)
    segs = build_segments(strokes)
    gappy = [b for b in dense if b.i % 7 != 0]  # ~14% of ids removed

    def kinds(bars):
        return sorted((e.kind, e.detail) for e in detect_buy_sell(strokes, segs, centers, bars=bars))

    assert kinds(dense) == kinds(gappy)

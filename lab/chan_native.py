"""A small, auditable Chan-theory structural engine.

This module deliberately has no CZSC dependency.  It is intended for
research/backtests where every object must have a confirmation bar and must
be reproducible by replaying bars from left to right.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal


Direction = Literal["up", "down"]
Mark = Literal["top", "bottom"]


@dataclass
class Kline:
    i: int
    high: float
    low: float
    dt: object | None = None


@dataclass
class Fractal:
    i: int
    mark: Mark
    price: float
    confirm_i: int
    pos: int = -1


@dataclass
class Stroke:
    start: Fractal
    end: Fractal
    direction: Direction
    confirm_i: int

    @property
    def high(self) -> float:
        return max(self.start.price, self.end.price)

    @property
    def low(self) -> float:
        return min(self.start.price, self.end.price)


@dataclass
class Center:
    start_stroke: int
    end_stroke: int
    zg: float
    zd: float
    confirm_i: int
    kind: str = "unknown"  # bottom_candidate / top_candidate / unknown
    status: str = "active"  # active / broken
    broken_at: int | None = None
    growth: str = "formation"  # formation / extension / newborn / expansion
    break_stroke: int | None = None
    break_direction: Direction | None = None


@dataclass
class Segment:
    start_stroke: int
    end_stroke: int
    direction: Direction
    high: float
    low: float
    confirm_i: int
    power_price: float = 0.0
    power_time: int = 0
    start_price: float | None = None
    end_price: float | None = None


@dataclass
class SignalEvent:
    kind: str
    i: int
    confirm_i: int
    detail: str
    center_start_stroke: int | None = None
    center_end_stroke: int | None = None
    break_stroke: int | None = None
    segment_start_stroke: int | None = None
    segment_end_stroke: int | None = None


def remove_inclusion(bars: Iterable[Kline]) -> list[Kline]:
    """Merge包含关系 from left to right without looking ahead."""
    out: list[Kline] = []
    direction: Direction = "up"
    for bar in bars:
        cur = Kline(bar.i, float(bar.high), float(bar.low), bar.dt)
        if not out:
            out.append(cur)
            continue
        prev = out[-1]
        contains = (cur.high <= prev.high and cur.low >= prev.low) or (
            cur.high >= prev.high and cur.low <= prev.low
        )
        if not contains:
            direction = "up" if cur.high > prev.high else "down" if cur.low < prev.low else direction
            out.append(cur)
            continue
        if direction == "up":
            prev.high = max(prev.high, cur.high)
            prev.low = max(prev.low, cur.low)
        else:
            prev.high = min(prev.high, cur.high)
            prev.low = min(prev.low, cur.low)
        prev.i = cur.i
        prev.dt = cur.dt
    return out


def detect_fractals(bars: list[Kline]) -> list[Fractal]:
    """Detect confirmed 3-bar fractals; confirmation is the right neighbour."""
    result: list[Fractal] = []
    for i in range(1, len(bars) - 1):
        a, b, c = bars[i - 1], bars[i], bars[i + 1]
        if b.high > a.high and b.high >= c.high:
            result.append(Fractal(b.i, "top", b.high, c.i, i))
        elif b.low < a.low and b.low <= c.low:
            result.append(Fractal(b.i, "bottom", b.low, c.i, i))
    return result


def build_strokes(fractals: list[Fractal], min_gap: int = 4) -> list[Stroke]:
    """Build alternating strokes from confirmed fractals.

    Same-mark fractals are replaced by the more extreme one.  A stroke needs
    at least ``min_gap`` normalized bars between endpoints.  The endpoint's
    fractal confirmation index is the stroke confirmation index.
    """
    pivots: list[Fractal] = []
    for fx in fractals:
        if not pivots:
            pivots.append(fx)
            continue
        last = pivots[-1]
        if fx.mark == last.mark:
            better = fx.price > last.price if fx.mark == "top" else fx.price < last.price
            if better:
                pivots[-1] = fx
            continue
        # ``pos`` is the index after inclusion normalization; original bar
        # ids can have gaps because several source bars may be merged.
        if fx.pos - last.pos < min_gap:
            continue
        pivots.append(fx)
    strokes: list[Stroke] = []
    for a, b in zip(pivots, pivots[1:]):
        strokes.append(Stroke(a, b, "up" if a.mark == "bottom" else "down", b.confirm_i))
    return strokes


def build_centers(strokes: list[Stroke], segments: list[Segment] | None = None) -> list[Center]:
    """Create confirmed, de-duplicated overlapping centers.

    Consecutive overlapping three-stroke windows are one extending center,
    not several independent centers.  ``kind`` is only a directional
    candidate based on the structure immediately preceding the center; it is
    never inferred from future strokes.
    """
    result: list[Center] = []
    units = segments if segments is not None else strokes
    if len(units) < 3:
        return result
    i = 2
    while i < len(units):
        window = units[i - 2 : i + 1]
        zg = min(s.high for s in window)
        zd = max(s.low for s in window)
        if zd > zg:
            i += 1
            continue
        prior = units[i - 3].direction if i >= 3 else ""
        kind = "bottom_candidate" if prior == "down" else "top_candidate" if prior == "up" else "unknown"
        z = Center(i - 2, i, zg, zd, window[-1].confirm_i, kind)
        j = i + 1
        # Extend the same center while every new segment intersects the
        # current interval.  The interval is the cumulative intersection.
        while j < len(units):
            u = units[j]
            if u.high < z.zd or u.low > z.zg:
                z.status = "broken"
                z.broken_at = u.confirm_i
                z.break_stroke = j
                z.break_direction = u.direction
                break
            z.zg = min(z.zg, u.high)
            z.zd = max(z.zd, u.low)
            z.end_stroke = j
            z.confirm_i = u.confirm_i
            z.growth = "extension"
            j += 1
        result.append(z)
        i = max(j, i + 1)
    return result


def classify_center_relations(centers: list[Center]) -> list[Center]:
    """Mark consecutive center relations without changing their intervals."""
    for i in range(1, len(centers)):
        prev, cur = centers[i - 1], centers[i]
        if max(prev.zd, cur.zd) <= min(prev.zg, cur.zg):
            prev.growth = cur.growth = "expansion"
        elif cur.start_stroke > prev.end_stroke:
            cur.growth = "newborn"
    return centers


def build_segments(strokes: list[Stroke]) -> list[Segment]:
    """Build directional segments from confirmed strokes.

    A segment extends while a same-direction stroke makes a new extreme.  A
    non-extending same-direction stroke confirms the previous segment; the
    reversal stroke is then the first stroke of the next segment.  This is a
    deliberately conservative, replayable definition for research and does
    not inspect bars after ``confirm_i``.
    """
    if not strokes:
        return []
    result: list[Segment] = []
    start = 0
    direction = strokes[0].direction
    feature_indices: list[int] = []

    def feature_fractal(indices: list[int], mark: str) -> bool:
        if len(indices) < 3:
            return False
        # Feature-sequence elements have the same direction.  Apply inclusion
        # processing to these stroke ranges before looking for its fractal;
        # raw stroke highs/lows would create spurious segment breaks.
        normalized: list[tuple[float, float]] = []
        fdir = strokes[indices[0]].direction
        for x in indices:
            h, l = strokes[x].high, strokes[x].low
            if normalized and ((h <= normalized[-1][0] and l >= normalized[-1][1]) or (h >= normalized[-1][0] and l <= normalized[-1][1])):
                ph, pl = normalized[-1]
                normalized[-1] = ((min(ph, h), min(pl, l)) if fdir == "down" else (max(ph, h), max(pl, l)))
            else:
                normalized.append((h, l))
        if len(normalized) < 3:
            return False
        a, b, c = normalized[-3:]
        if mark == "top":
            return b[0] > a[0] and b[0] >= c[0] and b[1] > a[1] and b[1] >= c[1]
        return b[0] < a[0] and b[0] <= c[0] and b[1] < a[1] and b[1] <= c[1]

    for i in range(1, len(strokes)):
        s = strokes[i]
        opposite = s.direction != direction
        if opposite:
            feature_indices.append(i)
            # Direct pen destruction is the first, unambiguous termination.
            direct_break = (direction == "up" and s.low < strokes[start].low) or (
                direction == "down" and s.high > strokes[start].high
            )
            mark = "top" if direction == "up" else "bottom"
            if direct_break or feature_fractal(feature_indices, mark):
                end = i - 1
                if end - start + 1 >= 3:
                    group = strokes[start : end + 1]
                    high = max(x.high for x in group); low = min(x.low for x in group)
                    displacement = abs(group[-1].end.price - group[0].start.price)
                    duration = max(1, group[-1].end.i - group[0].start.i)
                    result.append(Segment(start, end, direction, high, low, s.confirm_i, displacement / max(low, 1e-12), duration,
                                          group[0].start.price, group[-1].end.price))
                start = i
                direction = s.direction
                feature_indices = []
    # Terminal segment is intentionally omitted because no destruction has
    # confirmed its end yet.
    return result


def promote_segments_to_strokes(segments: list[Segment]) -> list[Stroke]:
    """Promote completed lower-level trend types to higher-level strokes."""
    strokes: list[Stroke] = []
    for idx, lower in enumerate(segments):
        if lower.direction == "up":
            a = Fractal(idx, "bottom", lower.start_price if lower.start_price is not None else lower.low,
                        lower.confirm_i, idx)
            b = Fractal(idx + 1, "top", lower.end_price if lower.end_price is not None else lower.high,
                        lower.confirm_i, idx + 1)
        else:
            a = Fractal(idx, "top", lower.start_price if lower.start_price is not None else lower.high,
                        lower.confirm_i, idx)
            b = Fractal(idx + 1, "bottom", lower.end_price if lower.end_price is not None else lower.low,
                        lower.confirm_i, idx + 1)
        strokes.append(Stroke(a, b, lower.direction, lower.confirm_i))
    return strokes


def recursive_levels(bars: Iterable[Kline], levels: int = 2, min_gap: int = 4) -> list[dict[str, list]]:
    """Construct higher structures from lower-level completed segments.

    Level 0 is calculated from source bars.  Each next level promotes
    completed lower-level segments to directional strokes and repeats the
    feature-sequence/segment/center construction.  This preserves the
    recursive hierarchy instead of re-running raw three-bar fractals on
    already-completed trend types.  Promoted strokes carry the lower-level
    confirmation index, so a higher-level object cannot confirm before its
    inputs.
    """
    current = list(bars)
    output: list[dict[str, list]] = []
    previous_segments: list[Segment] | None = None
    for level in range(max(1, levels)):
        if level == 0:
            normalized, fractals, strokes, centers = calculate(current, min_gap=min_gap)
            segments = build_segments(strokes)
        else:
            # A completed lower-level segment is already a directional trend
            # type.  Turning it back into an ordinary bar and asking for a
            # three-bar fractal loses the recursion (especially on 30m data).
            # Promote its two endpoints to one higher-level stroke, then run
            # the same feature-sequence segment builder without weakening the
            # confirmation-time rules.
            assert previous_segments is not None
            strokes = promote_segments_to_strokes(previous_segments)
            normalized = [Kline(i=s.confirm_i, high=s.high, low=s.low, dt=s.confirm_i) for s in previous_segments]
            fractals = []
            segments = build_segments(strokes)
            centers = classify_center_relations(build_centers(strokes, segments))
        output.append({"bars": normalized, "fractals": fractals, "strokes": strokes, "segments": segments, "centers": centers, "events": detect_buy_sell(strokes, segments, centers)})
        if not segments:
            break
        # Carry the lower-level confirmation index into the synthetic bar;
        # using ``n`` here would make a higher-level confirmation appear
        # earlier than the low-level structure that created it.
        current = [Kline(i=s.confirm_i, high=s.high, low=s.low, dt=s.confirm_i) for s in segments]
        previous_segments = segments
    return output


def _center_between(centers: list[Center], i: int) -> Center | None:
    valid = [z for z in centers if z.confirm_i <= i]
    return valid[-1] if valid else None


def _center_before(centers: list[Center], stroke_index: int) -> Center | None:
    """Latest center completed before a candidate segment begins."""
    valid = [z for z in centers if z.end_stroke < stroke_index]
    return valid[-1] if valid else None


def detect_buy_sell(strokes: list[Stroke], segments: list[Segment], centers: list[Center]) -> list[SignalEvent]:
    """Conservative structural buy/sell events.

    These are intentionally explicit research definitions, not claims about
    every school of Chan theory: first buy/sell use weakening continuation of
    two same-direction segments; second buy/sell use a non-breaking pullback
    around the latest center; third buy/sell use a confirmed center breakout.
    """
    events: list[SignalEvent] = []
    emitted_structure: set[tuple[str, int, int]] = set()

    def weakening(a: Segment, b: Segment) -> bool:
        """Require a real second-leg divergence, not any one weak metric.

        A same-direction leg is considered weaker only when its displacement
        efficiency declines and at least one independent price/range measure
        also declines.  This avoids treating a shorter but equally forceful
        leg as a Chan divergence.
        """
        eff_a = a.power_price / max(a.power_time, 1)
        eff_b = b.power_price / max(b.power_time, 1)
        metrics = [
            eff_b < eff_a,
            (b.high - b.low) < (a.high - a.low),
            b.power_price < a.power_price,
        ]
        return metrics[0] and sum(metrics[1:]) >= 1

    def append_once(event: SignalEvent, structure_key: tuple[str, int, int] | None = None) -> None:
        key = (event.kind, event.confirm_i)
        if structure_key is not None and structure_key in emitted_structure:
            return
        if not any((x.kind, x.confirm_i) == key for x in events):
            events.append(event)
            if structure_key is not None:
                emitted_structure.add(structure_key)
    for j in range(2, len(segments)):
        a, mid, b = segments[j - 2], segments[j - 1], segments[j]
        # Same-direction legs must be separated by a completed counter-leg;
        # comparing adjacent segments would be structurally impossible.
        if a.direction == b.direction and a.direction == "down" and mid.direction == "up":
            # The divergence sequence starts immediately after the center;
            # looking up the center before ``b`` can accidentally select a
            # later extending center that already contains ``a``.
            z = _center_before(centers, a.start_stroke)
            if z and z.kind == "bottom_candidate" and z.status == "broken" and z.break_stroke == a.start_stroke and z.break_direction == "down" and b.low < a.low and a.low <= z.zd and weakening(a, b):
                append_once(SignalEvent("一买", b.end_stroke, b.confirm_i, "底部中枢下破后同向下跌创新低且力度衰减", z.start_stroke, z.end_stroke, z.break_stroke, a.start_stroke, b.end_stroke), ("一买", z.start_stroke, z.end_stroke))
        if a.direction == b.direction and a.direction == "up" and mid.direction == "down":
            z = _center_before(centers, a.start_stroke)
            if z and z.kind == "top_candidate" and z.status == "broken" and z.break_stroke == a.start_stroke and z.break_direction == "up" and b.high > a.high and a.high >= z.zg and weakening(a, b):
                append_once(SignalEvent("一卖", b.end_stroke, b.confirm_i, "顶部中枢上破后同向上涨创新高且力度衰减", z.start_stroke, z.end_stroke, z.break_stroke, a.start_stroke, b.end_stroke), ("一卖", z.start_stroke, z.end_stroke))
    # Second/third-class points require a complete departure-pullback-confirm
    # sequence.  A single breakout segment is never sufficient.
    for j in range(2, len(segments)):
        depart, pull, confirm = segments[j - 2], segments[j - 1], segments[j]
        if depart.direction == "up" and pull.direction == "down" and confirm.direction == "up":
            z = _center_before(centers, depart.start_stroke)
            # The departure must be the first completed segment after this
            # center; a later breakout is not a fresh second/third buy.
            if z and z.kind == "bottom_candidate" and z.status == "broken" and z.break_stroke == depart.start_stroke and z.break_direction == "up" and depart.high > z.zg:
                if depart.low > z.zg and pull.low > z.zg:
                    append_once(SignalEvent("三买", confirm.end_stroke, confirm.confirm_i, "离开底部中枢后回试不回中枢", z.start_stroke, z.end_stroke, z.break_stroke, depart.start_stroke, confirm.end_stroke), ("三买", z.start_stroke, z.end_stroke))
                elif depart.low <= z.zg and pull.low >= depart.low:
                    append_once(SignalEvent("二买", confirm.end_stroke, confirm.confirm_i, "上行后首次回调不破前低", z.start_stroke, z.end_stroke, z.break_stroke, depart.start_stroke, confirm.end_stroke), ("二买", z.start_stroke, z.end_stroke))
        if depart.direction == "down" and pull.direction == "up" and confirm.direction == "down":
            z = _center_before(centers, depart.start_stroke)
            if z and z.kind == "top_candidate" and z.status == "broken" and z.break_stroke == depart.start_stroke and z.break_direction == "down" and depart.low < z.zd:
                if depart.high < z.zd and pull.high < z.zd:
                    append_once(SignalEvent("三卖", confirm.end_stroke, confirm.confirm_i, "离开顶部中枢后回抽不回中枢", z.start_stroke, z.end_stroke, z.break_stroke, depart.start_stroke, confirm.end_stroke), ("三卖", z.start_stroke, z.end_stroke))
                elif depart.high >= z.zd and pull.high <= depart.high:
                    append_once(SignalEvent("二卖", confirm.end_stroke, confirm.confirm_i, "下行后首次反抽不破前高", z.start_stroke, z.end_stroke, z.break_stroke, depart.start_stroke, confirm.end_stroke), ("二卖", z.start_stroke, z.end_stroke))
    return events


def calculate(bars: Iterable[Kline], min_gap: int = 4) -> tuple[list[Kline], list[Fractal], list[Stroke], list[Center]]:
    normalized = remove_inclusion(bars)
    fractals = detect_fractals(normalized)
    strokes = build_strokes(fractals, min_gap=min_gap)
    segments = build_segments(strokes)
    return normalized, fractals, strokes, classify_center_relations(build_centers(strokes, segments))


def replay_snapshots(bars: Iterable[Kline], min_gap: int = 4):
    """Yield a structure snapshot after each source bar.

    This intentionally favors auditability over speed.  It is the reference
    implementation for checking that a production incremental implementation
    never differs from the left-to-right historical calculation.
    """
    history = []
    for bar in bars:
        history.append(bar)
        normalized, fractals, strokes, centers = calculate(history, min_gap=min_gap)
        yield {
            "bar_i": bar.i,
            "normalized": normalized,
            "fractals": fractals,
            "strokes": strokes,
            "segments": build_segments(strokes),
            "centers": centers,
        }

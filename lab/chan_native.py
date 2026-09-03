"""A small, auditable Chan-theory structural engine.

This module deliberately has no CZSC dependency.  It is intended for
research/backtests where every object must have a confirmation bar and must
be reproducible by replaying bars from left to right.

Engine version 2 adds:

* a 中枢 state machine with a **fixed** overlap zone ``[zd, zg]`` plus the
  true running extremes ``gg`` / ``dd`` and a level tag;
* 线段 第一种 / 第二种划分 (the feature-sequence gap is confirmed by three
  further strokes instead of terminating the segment immediately);
* MACD-area (盘整 / 趋势) 背驰 for the first buy/sell instead of a bare
  normalized-displacement slowdown.
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
    close: float | None = None


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
    zg: float  # fixed overlap high  = min(high) of the first three units
    zd: float  # fixed overlap low   = max(low) of the first three units
    confirm_i: int
    kind: str = "unknown"  # bottom_candidate / top_candidate / unknown
    status: str = "active"  # active / broken
    broken_at: int | None = None
    growth: str = "formation"  # formation / extension / newborn / expansion
    break_stroke: int | None = None
    break_direction: Direction | None = None
    gg: float | None = None  # true max(high) across every unit in the center
    dd: float | None = None  # true min(low) across every unit in the center
    level: int = 0
    trend: str = ""  # '' / 'up' / 'down' / 'range'

    def __post_init__(self) -> None:
        # A hand-built center (tests, promoted levels) may omit the true
        # extremes; fall back to the fixed zone so downstream math is safe.
        if self.gg is None:
            self.gg = self.zg
        if self.dd is None:
            self.dd = self.zd


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
    partition: str = "first"  # first / second (特征序列缺口)


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
        cur = Kline(bar.i, float(bar.high), float(bar.low), bar.dt, bar.close)
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
        prev.close = cur.close
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

    Three consecutive overlapping units form a center whose zone ``[zd, zg]``
    is **fixed** at formation.  Later units either intersect that fixed zone
    (延伸 — the center grows, ``gg`` / ``dd`` track the true extremes) or the
    first non-intersecting unit records the break.  ``kind`` is only a
    directional candidate from the structure immediately preceding the
    center; it is never inferred from future units.
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
        z = Center(
            i - 2, i, zg, zd, window[-1].confirm_i, kind,
            gg=max(s.high for s in window), dd=min(s.low for s in window),
        )
        j = i + 1
        # Extend while every new unit still intersects the *fixed* zone.
        while j < len(units):
            u = units[j]
            if u.high < z.zd or u.low > z.zg:
                z.status = "broken"
                z.broken_at = u.confirm_i
                z.break_stroke = j
                z.break_direction = u.direction
                break
            z.gg = max(z.gg, u.high)
            z.dd = min(z.dd, u.low)
            z.end_stroke = j
            z.confirm_i = u.confirm_i
            z.growth = "extension"
            j += 1
        result.append(z)
        i = max(j, i + 1)
    return result


def classify_center_relations(centers: list[Center]) -> list[Center]:
    """Classify consecutive centers as 上涨 / 下跌 趋势 or 盘整 / 扩展.

    The zone ``[zd, zg]`` is never mutated here.  ``trend`` is read by the
    first-buy/sell divergence check to tell 趋势背驰 from 盘整背驰.
    """
    for i in range(1, len(centers)):
        prev, cur = centers[i - 1], centers[i]
        disjoint_after = cur.start_stroke > prev.end_stroke
        if cur.dd > prev.gg:
            cur.trend = "up"
            if disjoint_after:
                cur.growth = "newborn"
        elif cur.gg < prev.dd:
            cur.trend = "down"
            if disjoint_after:
                cur.growth = "newborn"
        elif max(prev.dd, cur.dd) <= min(prev.gg, cur.gg):
            cur.trend = "range"
            if max(prev.zd, cur.zd) <= min(prev.zg, cur.zg):
                prev.growth = cur.growth = "expansion"
        elif disjoint_after:
            cur.growth = "newborn"
    return centers


def build_segments(strokes: list[Stroke]) -> list[Segment]:
    """Build directional segments from confirmed strokes.

    A segment extends while a same-direction stroke makes a new extreme.  The
    end is taken from the opposite-direction feature sequence:

    * 第一种划分 — no gap between the first two feature-sequence elements: the
      feature top/bottom fractal confirms the end immediately.
    * 第二种划分 — a gap is present: the break is only *pending* until three
      further strokes complete without price reclaiming the pre-break
      extreme.  A new extreme in the old direction voids the pending break.

    Direct pen destruction (price runs past the segment origin) is always an
    immediate 第一种 termination.  The in-progress terminal segment is
    omitted because nothing has confirmed its end.  This is a deliberately
    conservative, replayable definition and never inspects bars after
    ``confirm_i``.
    """
    if not strokes:
        return []
    result: list[Segment] = []
    start = 0
    direction = strokes[0].direction
    feature_indices: list[int] = []
    seg_extreme = strokes[0].high if direction == "up" else strokes[0].low
    pending: dict[str, float] | None = None

    def normalize(indices: list[int]) -> list[tuple[float, float]]:
        fdir = strokes[indices[0]].direction
        out: list[tuple[float, float]] = []
        for x in indices:
            h, l = strokes[x].high, strokes[x].low
            if out and ((h <= out[-1][0] and l >= out[-1][1]) or (h >= out[-1][0] and l <= out[-1][1])):
                ph, pl = out[-1]
                out[-1] = (min(ph, h), min(pl, l)) if fdir == "down" else (max(ph, h), max(pl, l))
            else:
                out.append((h, l))
        return out

    def feature_break(indices: list[int], mark: str) -> tuple[bool, bool]:
        """Return ``(is_fractal, has_gap)`` for the feature sequence."""
        if len(indices) < 3:
            return False, False
        seq = normalize(indices)
        if len(seq) < 3:
            return False, False
        a, b, c = seq[-3:]
        if mark == "top":
            hit = b[0] > a[0] and b[0] >= c[0] and b[1] > a[1] and b[1] >= c[1]
            gap = hit and a[0] < b[1]
        else:
            hit = b[0] < a[0] and b[0] <= c[0] and b[1] < a[1] and b[1] <= c[1]
            gap = hit and a[1] > b[0]
        return hit, gap

    def emit(end_stroke: int, confirm_i: int, second_type: bool) -> None:
        nonlocal start, direction, feature_indices, seg_extreme, pending
        if end_stroke - start + 1 >= 3:
            group = strokes[start : end_stroke + 1]
            high = max(x.high for x in group)
            low = min(x.low for x in group)
            displacement = abs(group[-1].end.price - group[0].start.price)
            duration = max(1, group[-1].end.i - group[0].start.i)
            result.append(
                Segment(
                    start, end_stroke, direction, high, low, confirm_i,
                    displacement / max(low, 1e-12), duration,
                    group[0].start.price, group[-1].end.price,
                    "second" if second_type else "first",
                )
            )
        start = end_stroke + 1
        direction = strokes[start].direction if start < len(strokes) else direction
        seg_extreme = (
            (strokes[start].high if direction == "up" else strokes[start].low)
            if start < len(strokes)
            else seg_extreme
        )
        feature_indices = []
        pending = None

    for i in range(1, len(strokes)):
        s = strokes[i]

        if s.direction == direction:
            reaches_new = s.high > seg_extreme if direction == "up" else s.low < seg_extreme
            if reaches_new:
                seg_extreme = s.high if direction == "up" else s.low
                if pending is not None:
                    # Price reclaimed the pre-break extreme: the gap break is void.
                    pending = None
                    feature_indices = []
            continue

        feature_indices.append(i)
        mark = "top" if direction == "up" else "bottom"

        if pending is not None:
            if i - int(pending["end_stroke"]) >= 3:
                end_s = int(pending["end_stroke"])
                emit(end_s, s.confirm_i, second_type=True)
                # Re-seed state from strokes already consumed by the new segment.
                for k in range(start + 1, i + 1):
                    if strokes[k].direction == direction:
                        ext = strokes[k].high if direction == "up" else strokes[k].low
                        if (direction == "up" and ext > seg_extreme) or (
                            direction == "down" and ext < seg_extreme
                        ):
                            seg_extreme = ext
                    else:
                        feature_indices.append(k)
            continue

        direct_break = (
            (direction == "up" and s.low < strokes[start].low)
            or (direction == "down" and s.high > strokes[start].high)
        )
        if direct_break:
            emit(i - 1, s.confirm_i, second_type=False)
            continue

        hit, gap = feature_break(feature_indices, mark)
        if hit and not gap:
            emit(i - 1, s.confirm_i, second_type=False)
        elif hit and gap:
            pending = {"end_stroke": float(i - 1), "extreme": seg_extreme}

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
        for z in centers:
            z.level = level
        # Only level 0 has real closes; a promoted synthetic bar has no
        # meaningful MACD area, so higher levels use the displacement fallback.
        event_bars = normalized if level == 0 else None
        output.append({
            "bars": normalized, "fractals": fractals, "strokes": strokes,
            "segments": segments, "centers": centers,
            "events": detect_buy_sell(strokes, segments, centers, bars=event_bars),
        })
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


def macd_histogram(bars: list[Kline], fast: int = 12, slow: int = 26, signal: int = 9) -> list[float]:
    """Return a causal MACD histogram indexed by original bar id ``Kline.i``.

    ``strokes``/``segments`` carry the pre-inclusion bar id (``fractal.i``),
    so the histogram must be addressable by that same id.  Inclusion-merged
    bars have gaps in ``i``; those positions are forward-filled with the last
    known close so ``hist[stroke.start.i]`` always lines up.
    """
    if not bars:
        return []
    ordered = sorted(bars, key=lambda b: b.i)
    n = ordered[-1].i + 1
    closes = [0.0] * n
    prev = float(ordered[0].close if ordered[0].close is not None else (ordered[0].high + ordered[0].low) / 2)
    k = 0
    for i in range(n):
        while k < len(ordered) and ordered[k].i == i:
            cur = ordered[k]
            prev = float(cur.close if cur.close is not None else (cur.high + cur.low) / 2)
            k += 1
        closes[i] = prev

    def ema(values, period):
        alpha = 2 / (period + 1); out = []; prev_e = values[0] if values else 0.0
        for value in values:
            prev_e = alpha * value + (1 - alpha) * prev_e; out.append(prev_e)
        return out

    dif = [a - b for a, b in zip(ema(closes, fast), ema(closes, slow))]
    dea = ema(dif, signal)
    return [a - b for a, b in zip(dif, dea)]


def _segment_macd_area(seg: Segment, strokes: list[Stroke], hist: list[float]) -> float:
    """Sum |MACD histogram| over the original-bar span a segment covers."""
    if not hist or seg.start_stroke >= len(strokes):
        return 0.0
    lo = max(0, strokes[seg.start_stroke].start.i)
    end_idx = seg.end_stroke if seg.end_stroke < len(strokes) else len(strokes) - 1
    hi = min(len(hist), strokes[end_idx].end.i + 1)
    return sum(abs(x) for x in hist[lo:hi]) if hi > lo else 0.0


def detect_buy_sell(
    strokes: list[Stroke],
    segments: list[Segment],
    centers: list[Center],
    bars: list[Kline] | None = None,
) -> list[SignalEvent]:
    """Conservative structural buy/sell events.

    These are explicit research definitions, not claims about every school of
    Chan theory.  First buy/sell require a center-boundary break, a fresh
    extreme, and a MACD-area 背驰 (盘整 vs 趋势 depends on whether the two
    preceding same-kind centers are trending).  When ``bars`` is omitted the
    check degrades to the earlier normalized-displacement slowdown so callers
    that never pass bars keep working.  Second/third buy/sell require a
    complete departure-pullback-confirm sequence around the latest center.
    """
    events: list[SignalEvent] = []
    emitted_structure: set[tuple[str, int, int]] = set()
    hist = macd_histogram(bars) if bars else []

    def divergence(a: Segment, b: Segment) -> str | None:
        """Return a 背驰 label when ``b`` is a genuine weaker second leg."""
        if a.direction != b.direction:
            return None
        made_extreme = (b.low < a.low) if a.direction == "down" else (b.high > a.high)
        if not made_extreme:
            return None
        if hist:
            area_a = _segment_macd_area(a, strokes, hist)
            area_b = _segment_macd_area(b, strokes, hist)
            if area_a <= 0 or area_b <= 0 or area_b >= area_a:
                return None
            metric = "MACD面积"
        else:
            eff_a = a.power_price / max(a.power_time, 1)
            eff_b = b.power_price / max(b.power_time, 1)
            checks = [
                (b.high - b.low) < (a.high - a.low),
                b.power_price < a.power_price,
            ]
            if not (eff_b < eff_a and sum(checks) >= 1):
                return None
            metric = "位移效率"
        same_kind = [
            z for z in centers
            if z.end_stroke < a.start_stroke
            and z.kind == ("bottom_candidate" if a.direction == "down" else "top_candidate")
        ]
        trend = len(same_kind) >= 2 and (
            (a.direction == "down" and same_kind[-1].dd < same_kind[-2].dd)
            or (a.direction == "up" and same_kind[-1].gg > same_kind[-2].gg)
        )
        return f"{'趋势' if trend else '盘整'}背驰({metric})"

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
        if a.direction == b.direction == "down" and mid.direction == "up":
            z = _center_before(centers, a.start_stroke)
            if z is not None and a.low < z.zd:
                tag = divergence(a, b)
                if tag:
                    append_once(
                        SignalEvent("一买", b.end_stroke, b.confirm_i, tag,
                                    z.start_stroke, z.end_stroke, z.break_stroke, a.start_stroke, b.end_stroke),
                        ("一买", z.start_stroke, z.end_stroke),
                    )
        if a.direction == b.direction == "up" and mid.direction == "down":
            z = _center_before(centers, a.start_stroke)
            if z is not None and a.high > z.zg:
                tag = divergence(a, b)
                if tag:
                    append_once(
                        SignalEvent("一卖", b.end_stroke, b.confirm_i, tag,
                                    z.start_stroke, z.end_stroke, z.break_stroke, a.start_stroke, b.end_stroke),
                        ("一卖", z.start_stroke, z.end_stroke),
                    )
    # Second/third-class points: scan forward from each center for the first
    # departure-pullback-confirm triplet that leaves it.  Anchoring on the
    # center (rather than every 3-segment window) is what makes 二/三类 fire at
    # a realistic rate — a sliding window almost never lands with its first
    # leg exactly on the departing segment.
    for z in centers:
        depart_stroke = z.break_stroke if z.break_stroke is not None else z.end_stroke + 1
        if depart_stroke + 2 >= len(segments) or depart_stroke <= 0:
            continue
        depart, pull, confirm = segments[depart_stroke], segments[depart_stroke + 1], segments[depart_stroke + 2]
        if (
            z.kind != "top_candidate"
            and depart.direction == "up" and pull.direction == "down" and confirm.direction == "up"
            and z.break_direction in (None, "up")
            and depart.high > z.zg
            and (depart.end_price is None or depart.end_price > z.zg)
        ):
            if pull.low > z.zg:
                append_once(SignalEvent("三买", confirm.end_stroke, confirm.confirm_i, "离开底部中枢后回试不回中枢", z.start_stroke, z.end_stroke, z.break_stroke, depart_stroke, confirm.end_stroke), ("三买", z.start_stroke, z.end_stroke))
            elif pull.low >= z.zd:
                append_once(SignalEvent("二买", confirm.end_stroke, confirm.confirm_i, "回抽中枢内不破中枢下沿", z.start_stroke, z.end_stroke, z.break_stroke, depart_stroke, confirm.end_stroke), ("二买", z.start_stroke, z.end_stroke))
        if (
            z.kind != "bottom_candidate"
            and depart.direction == "down" and pull.direction == "up" and confirm.direction == "down"
            and z.break_direction in (None, "down")
            and depart.low < z.zd
            and (depart.end_price is None or depart.end_price < z.zd)
        ):
            if pull.high < z.zd:
                append_once(SignalEvent("三卖", confirm.end_stroke, confirm.confirm_i, "离开顶部中枢后回抽不回中枢", z.start_stroke, z.end_stroke, z.break_stroke, depart_stroke, confirm.end_stroke), ("三卖", z.start_stroke, z.end_stroke))
            elif pull.high <= z.zg:
                append_once(SignalEvent("二卖", confirm.end_stroke, confirm.confirm_i, "反抽中枢内不破中枢上沿", z.start_stroke, z.end_stroke, z.break_stroke, depart_stroke, confirm.end_stroke), ("二卖", z.start_stroke, z.end_stroke))
    return events


def detect_buy_sell_classic(strokes, segments, centers, bars):
    """Classic-style experiment: trend centers + MACD area divergence.

    Superseded by :func:`detect_buy_sell` (which now folds MACD-area 背驰 into
    the main path).  Kept for the lab backtest scripts that import it.
    """
    hist = macd_histogram(bars)
    def area(seg):
        lo = max(0, seg.start_stroke < len(strokes) and strokes[seg.start_stroke].start.i or 0)
        hi = min(len(hist), (strokes[seg.end_stroke].end.i + 1) if seg.end_stroke < len(strokes) else len(hist))
        return sum(abs(x) for x in hist[lo:hi])
    events = []
    emitted = set()
    for j in range(2, len(segments)):
        a, mid, b = segments[j-2:j+1]
        if a.direction != b.direction or mid.direction == a.direction: continue
        prior = [z for z in centers if z.end_stroke < a.start_stroke]
        same = [z for z in prior if z.kind == ('bottom_candidate' if a.direction == 'down' else 'top_candidate')]
        trend = len(same) >= 2 and ((a.direction == 'down' and same[-1].zd < same[-2].zd) or (a.direction == 'up' and same[-1].zg > same[-2].zg))
        if not trend or area(a) <= 0 or area(b) <= 0 or area(b) >= area(a): continue
        if a.direction == 'down' and b.low >= a.low: continue
        if a.direction == 'up' and b.high <= a.high: continue
        key = (a.direction, same[-1].start_stroke, same[-1].end_stroke)
        if key in emitted: continue
        emitted.add(key)
        kind = '一买' if a.direction == 'down' else '一卖'
        events.append(SignalEvent(kind, b.end_stroke, b.confirm_i, '双中枢趋势 + MACD面积背驰', same[-1].start_stroke, same[-1].end_stroke, None, a.start_stroke, b.end_stroke))
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

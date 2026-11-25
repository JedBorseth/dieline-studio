"""Internal dieline model: lines and arcs tagged with a line type.

Coordinates are in the dieline's `units` ("in" for the R&L styles, which
compute in inches exactly as their ArtiosCAD standards do; "mm" for CF2 read
back in millimetres). Origin at the blank's lower-left corner, x to the right,
y up. CF2 and DXF are only ways of writing this model out.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum

Point = tuple[float, float]

# Coordinates are rounded to this many decimals when compared or written.
# Six keeps 1/32" and 1/64" exact.
PRECISION = 6
EPS = 10**-PRECISION


class LineType(StrEnum):
    CUT = "cut"
    CREASE = "crease"
    PERFORATION = "perforation"
    SCORE = "score"  # partial-depth cut (reverse/half cut)
    ANNOTATION = "annotation"  # dimensions, text guides: never manufactured


@dataclass(frozen=True, slots=True)
class Line:
    start: Point
    end: Point
    type: LineType

    @property
    def length(self) -> float:
        return math.dist(self.start, self.end)


@dataclass(frozen=True, slots=True)
class Arc:
    """Circular arc from start to end around center.

    clockwise=False means counter-clockwise (positive angle direction).
    """

    start: Point
    end: Point
    center: Point
    clockwise: bool
    type: LineType

    @property
    def radius(self) -> float:
        return math.dist(self.center, self.start)

    @property
    def sweep(self) -> float:
        """Signed sweep angle in radians (positive = counter-clockwise)."""
        a0 = math.atan2(self.start[1] - self.center[1], self.start[0] - self.center[0])
        a1 = math.atan2(self.end[1] - self.center[1], self.end[0] - self.center[0])
        sweep = a1 - a0
        if self.clockwise:
            while sweep >= 0:
                sweep -= math.tau
        else:
            while sweep <= 0:
                sweep += math.tau
        return sweep

    @property
    def length(self) -> float:
        return abs(self.sweep) * self.radius


Segment = Line | Arc


@dataclass(slots=True)
class Dieline:
    name: str
    units: str = "mm"  # "in" or "mm"
    segments: list[Segment] = field(default_factory=list)
    # Free-form facts about how the dieline was made (style, dims, board, ...).
    meta: dict[str, object] = field(default_factory=dict)

    def add_line(self, start: Point, end: Point, type: LineType) -> None:
        self.segments.append(Line(_round_pt(start), _round_pt(end), type))

    def add_polyline(self, points: list[Point], type: LineType, closed: bool = False) -> None:
        pts = list(points)
        if closed:
            pts.append(pts[0])
        for a, b in zip(pts, pts[1:]):
            if math.dist(a, b) > EPS:
                self.add_line(a, b, type)

    def of_type(self, type: LineType) -> list[Segment]:
        return [s for s in self.segments if s.type == type]

    def bounds(self, *, include_annotations: bool = False) -> tuple[float, float, float, float]:
        """(xmin, ymin, xmax, ymax) over manufactured segments (arcs use endpoints + extrema)."""
        xs: list[float] = []
        ys: list[float] = []
        for s in self.segments:
            if s.type == LineType.ANNOTATION and not include_annotations:
                continue
            for x, y in _segment_extent_points(s):
                xs.append(x)
                ys.append(y)
        if not xs:
            return (0.0, 0.0, 0.0, 0.0)
        return (min(xs), min(ys), max(xs), max(ys))

    @property
    def mm_per_unit(self) -> float:
        return 25.4 if self.units == "in" else 1.0

    def size(self) -> tuple[float, float]:
        xmin, ymin, xmax, ymax = self.bounds()
        return (xmax - xmin, ymax - ymin)


def _round_pt(p: Point) -> Point:
    return (round(p[0], PRECISION) + 0.0, round(p[1], PRECISION) + 0.0)


def _segment_extent_points(s: Segment) -> list[Point]:
    if isinstance(s, Line):
        return [s.start, s.end]
    pts = [s.start, s.end]
    a0 = math.atan2(s.start[1] - s.center[1], s.start[0] - s.center[0])
    sweep = s.sweep
    r = s.radius
    # Include axis-aligned extrema the arc passes through.
    for k in range(-8, 9):
        ang = k * math.pi / 2
        t = (ang - a0) / sweep if sweep else -1
        if 0 < t < 1:
            pts.append((s.center[0] + r * math.cos(ang), s.center[1] + r * math.sin(ang)))
    return pts

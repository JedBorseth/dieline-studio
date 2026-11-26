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
    op: str | None = None  # id of the Operation that drew it (Dieline.ops)

    @property
    def length(self) -> float:
        return math.dist(self.start, self.end)


@dataclass(frozen=True, slots=True)
class Arc:
    """Circular arc from start to end around center.

    clockwise=False means counter-clockwise (positive angle direction).
    full_circle=True means one whole turn from start back to the same point;
    otherwise start and end must differ and the sweep is less than a turn.
    """

    start: Point
    end: Point
    center: Point
    clockwise: bool
    type: LineType
    full_circle: bool = False
    op: str | None = None

    @property
    def radius(self) -> float:
        return math.dist(self.center, self.start)

    @property
    def sweep(self) -> float:
        """Signed sweep angle in radians (positive = counter-clockwise)."""
        if self.full_circle:
            return -math.tau if self.clockwise else math.tau
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


@dataclass(frozen=True, slots=True)
class Operation:
    """One thing the engine drew: an outline, a hinge, a hole, a slit...

    Every segment names the operation it belongs to. The operation declares
    which contacts with other operations are deliberate, which is how the
    topology checks tell a designed junction from a mistake
    (docs/DESIGN-SYSTEM.md, "Contact and disposition rules").

    ends_on / crosses / never list operation kinds or ids; "*" means any.
    """

    id: str
    kind: str  # outline | hinge | hole | slit | score | perf | annotation | cut | crease
    closed: bool = False  # its segments form closed loops
    # "none": no open ends allowed; "on": every open end must touch an operation
    # it may end on; "free": open ends may stop in the board (creases, nicks).
    open_ends: str = "none"
    ends_on: tuple[str, ...] = ()
    crosses: tuple[str, ...] = ()
    clearance: float = 0.0  # near-miss distance to other operations, in the dieline's units
    # How it's made (rules.RuleSpec); None means the plain rule of its line type at the part's pointage.
    rule: object | None = None

    def allows(self, other: "Operation", what: str) -> bool:
        names = self.ends_on if what == "ends_on" else self.crosses
        return "*" in names or other.kind in names or other.id in names


@dataclass(slots=True)
class Dieline:
    name: str
    units: str = "mm"  # "in" or "mm"
    segments: list[Segment] = field(default_factory=list)
    # Free-form facts about how the dieline was made (style, dims, board, ...).
    meta: dict[str, object] = field(default_factory=dict)
    ops: dict[str, Operation] = field(default_factory=dict)
    # Named faces, frames, reference lines and hinges (faces.FaceMap), when the engine drew it.
    faces: object | None = None
    # What later build stages attach (features.py): "tags" maps each feature segment to its feature id,
    # "features" lists each feature as resolved, "report" keeps the final topology report.
    extras: dict[str, object] = field(default_factory=dict)

    def add_op(self, op: Operation) -> str:
        if op.id in self.ops:
            raise ValueError(f"duplicate operation id {op.id!r}")
        self.ops[op.id] = op
        return op.id

    def add_line(self, start: Point, end: Point, type: LineType, op: str | None = None) -> None:
        self.segments.append(Line(_round_pt(start), _round_pt(end), type, op))

    def add_arc(self, start: Point, end: Point, center: Point, clockwise: bool, type: LineType,
                full_circle: bool = False, op: str | None = None) -> None:
        self.segments.append(Arc(_round_pt(start), _round_pt(end), _round_pt(center), clockwise, type,
                                 full_circle, op))

    def add_polyline(self, points: list[Point], type: LineType, closed: bool = False, op: str | None = None) -> None:
        pts = list(points)
        if closed:
            pts.append(pts[0])
        for a, b in zip(pts, pts[1:]):
            if math.dist(a, b) > EPS:
                self.add_line(a, b, type, op)

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

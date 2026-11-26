"""Topology validation: the contact and disposition rules of docs/DESIGN-SYSTEM.md.

Every place two segments meet, cross or come close is classified:

- permitted junction: a contact an operation declares (a hinge ending on the
  outline, a slit ending on its hinge). Accepted.
- near miss: no contact, but closer than an operation's clearance. Warn.
- invalid intersection: any undeclared touch, overlap or crossing, broken arc
  data, an undeclared open cut end, a branch, a rule off the board. The
  mutation that produced it is refused.
- unresolved island: board meant to stay with no connection to the blank (the
  middle of a cut-out O), or a cut that divides the blank. Drawn, export blocked.

How: every segment is split at every contact into one exact arrangement (nodes
merged within the physical tolerance, edges that are pieces of the original
lines and arcs). Contacts are classified at the nodes from the directions of
the edges meeting there, so a path that passes through another counts as a
crossing even when it has a vertex on it. Board or scrap is decided by the
closed contours alone (outline and holes, even-odd); slits only divide board.
The pieces are the board faces of the cut network, traced exactly through
lines and arcs.

The same rules run on each output file as reconstructed from its own rounded
geometry (`reconstruct`).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

from .geometry import Arc, Dieline, Line, LineType, Operation, Point, Segment

# Contact tolerance and arc radius tolerance, in inches (scaled to the dieline's units).
TOL_IN = 1e-5
ARC_RADIUS_TOL_IN = 1e-4
ANGLE_EPS = 1e-9  # directions closer than this at a node are the same direction
MAX_COORD_IN = 1e5  # no blank is 8,000 feet across: bigger is a corrupt value, not geometry
TAU = math.tau


@dataclass(frozen=True, slots=True)
class Issue:
    # arc | zero_length | open_end | branch | overlap | crossing | tangent | contact |
    # off_board | outline | island | split | near_miss
    kind: str
    message: str
    point: Point | None = None
    ops: tuple[str, ...] = ()
    # An island or split: the drawn segments its board's outer boundary runs along (its exact identity).
    segs: tuple[Segment, ...] = field(default=(), compare=False, repr=False)


@dataclass(slots=True)
class Report:
    invalid: list[Issue] = field(default_factory=list)  # refuse the mutation
    islands: list[Issue] = field(default_factory=list)  # drawn, export blocked
    near_misses: list[Issue] = field(default_factory=list)  # warn
    material_faces: int = 0

    @property
    def ok(self) -> bool:
        return not self.invalid


# ------------------------------------------------------------------ operations


def ensure_ops(d: Dieline) -> dict[str, Operation]:
    """Operations for every segment. Segments drawn without one (hand-built
    dielines, files read back) get inferred operations: every cut is one
    operation that must form closed loops, every other rule one operation per
    line type that may end anywhere."""
    ops = dict(d.ops)
    if all(s.op is not None and s.op in ops for s in d.segments):
        return ops
    ops.setdefault("_cut", Operation("_cut", "cut", closed=True))
    for t in LineType:
        if t != LineType.CUT:
            ops.setdefault(f"_{t.value}", Operation(f"_{t.value}", t.value, open_ends="free", ends_on=("*",)))
    return ops


def op_of(s: Segment, ops: dict[str, Operation]) -> Operation:
    if s.op is not None and s.op in ops:
        return ops[s.op]
    return ops["_cut" if s.type == LineType.CUT else f"_{s.type.value}"]


def _tagged(d: Dieline) -> bool:
    """Drawn by the engine with explicit operations (inferred ones start with "_")."""
    used = {s.op for s in d.segments if s.type != LineType.ANNOTATION}
    return bool(used) and all(o in d.ops and not o.startswith("_") for o in used)


# ------------------------------------------------------------------ primitives


def _sweep_raw(a: Arc) -> float:
    """The sweep a partial arc's endpoints imply, in (-tau, tau)."""
    a0 = math.atan2(a.start[1] - a.center[1], a.start[0] - a.center[0])
    a1 = math.atan2(a.end[1] - a.center[1], a.end[0] - a.center[0])
    d = (a1 - a0) % TAU
    return -((TAU - d) % TAU) if a.clockwise else d


def arc_issue(a: Arc, units: str) -> str | None:
    k = 1.0 if units == "in" else 25.4
    tol, rtol = TOL_IN * k, ARC_RADIUS_TOL_IN * k
    r0, r1 = math.dist(a.center, a.start), math.dist(a.center, a.end)
    if r0 <= tol or r1 <= tol:
        return "zero-radius arc"
    if abs(r0 - r1) > rtol:
        return f"arc start and end radii differ by {abs(r0 - r1):.6f}"
    same = math.dist(a.start, a.end) <= tol
    if a.full_circle:
        return None if same else "full circle whose start and end differ"
    if same:
        return "arc whose start and end coincide (not flagged as a full circle)"
    sweep = abs(_sweep_raw(a))
    if not (tol / r0 < sweep < TAU - tol / r0):
        return "partial arc whose sweep is a full turn or nothing"
    return None


class _Piece:
    """A segment with the parameterisation the arrangement needs.

    Lines: parameter = distance from start. Arcs: parameter = angle travelled
    from start in the arc's own direction (radians)."""

    __slots__ = ("seg", "op", "line", "a", "b", "c", "r", "a0", "dirn", "span", "box")

    def __init__(self, seg: Segment, op: Operation) -> None:
        self.seg, self.op = seg, op
        self.line = isinstance(seg, Line)
        if self.line:
            self.a, self.b = seg.start, seg.end
            self.span = math.dist(seg.start, seg.end)
            xs, ys = (seg.start[0], seg.end[0]), (seg.start[1], seg.end[1])
            self.box = (min(xs), min(ys), max(xs), max(ys))
        else:
            self.c, self.r = seg.center, math.dist(seg.center, seg.start)
            self.a0 = math.atan2(seg.start[1] - seg.center[1], seg.start[0] - seg.center[0])
            self.dirn = -1.0 if seg.clockwise else 1.0
            self.span = TAU if seg.full_circle else abs(_sweep_raw(seg))
            self.box = _arc_box(self.c, self.r, self.a0, self.dirn, self.span)

    @property
    def scale(self) -> float:
        """Parameter units to length units."""
        return 1.0 if self.line else self.r

    @property
    def full(self) -> bool:
        return not self.line and self.seg.full_circle

    def at(self, u: float) -> Point:
        if self.line:
            t = u / self.span
            return (self.a[0] + (self.b[0] - self.a[0]) * t, self.a[1] + (self.b[1] - self.a[1]) * t)
        ang = self.a0 + self.dirn * u
        return (self.c[0] + self.r * math.cos(ang), self.c[1] + self.r * math.sin(ang))

    def raw_param(self, p: Point) -> float:
        """Arcs: angle from start in the arc's direction, in [0, tau), unclamped."""
        return (self.dirn * (math.atan2(p[1] - self.c[1], p[0] - self.c[0]) - self.a0)) % TAU

    def param(self, p: Point) -> float:
        """Parameter of the point on this piece nearest p (clamped to the piece)."""
        if self.line:
            dx, dy = self.b[0] - self.a[0], self.b[1] - self.a[1]
            t = ((p[0] - self.a[0]) * dx + (p[1] - self.a[1]) * dy) / (self.span * self.span)
            return min(max(t, 0.0), 1.0) * self.span
        u = self.raw_param(p)
        if self.full or u <= self.span:
            return u
        return self.span if (u - self.span) < (TAU - u) else 0.0

    def dist(self, p: Point) -> float:
        return math.dist(p, self.at(self.param(p)))

    def ends(self) -> list[Point]:
        return [] if self.full else [self.seg.start, self.seg.end]

    def marks(self) -> list[Point]:
        """Its ends, or two opposite points on a full circle."""
        return self.ends() or [self.at(0.0), self.at(math.pi)]


def _arc_box(c: Point, r: float, a0: float, dirn: float, span: float) -> tuple[float, float, float, float]:
    pts = [(c[0] + r * math.cos(a0), c[1] + r * math.sin(a0)),
           (c[0] + r * math.cos(a0 + dirn * span), c[1] + r * math.sin(a0 + dirn * span))]
    for k in range(4):
        ang = k * math.pi / 2
        if (dirn * (ang - a0)) % TAU <= span:
            pts.append((c[0] + r * math.cos(ang), c[1] + r * math.sin(ang)))
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _roots_line_circle(a: Point, b: Point, c: Point, r: float) -> list[Point]:
    """Both real roots of the infinite line with the circle, solved from the
    foot of the centre on the line so a long line doesn't swamp the precision;
    one root when they're a double root to numerical precision (true tangency)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = math.hypot(dx, dy)
    ux, uy = dx / n, dy / n
    t = (c[0] - a[0]) * ux + (c[1] - a[1]) * uy
    fx, fy = a[0] + t * ux, a[1] + t * uy
    h = math.hypot(c[0] - fx, c[1] - fy)
    if h > r * (1 + 1e-12):
        return []
    half = math.sqrt(max((r - h) * (r + h), 0.0))
    if half <= 1e-9 * r:
        return [(fx, fy)]
    return [(fx - half * ux, fy - half * uy), (fx + half * ux, fy + half * uy)]


def _roots_circle_circle(c1: Point, r1: float, c2: Point, r2: float) -> list[Point]:
    """Intersections of two circles. The half-chord comes from Heron's formula in
    factored form, so two roots close together on large circles stay two."""
    d = math.dist(c1, c2)
    if d == 0:
        return []
    prod = (r1 + r2 + d) * (r1 + r2 - d) * (r1 - r2 + d) * (-r1 + r2 + d)
    if prod < -1e-9 * (r1 + r2) ** 4:
        return []
    h = math.sqrt(max(prod, 0.0)) / (2 * d)
    x = (d * d + r1 * r1 - r2 * r2) / (2 * d)
    ux, uy = (c2[0] - c1[0]) / d, (c2[1] - c1[1]) / d
    base = (c1[0] + x * ux, c1[1] + x * uy)
    if h <= 1e-9 * min(r1, r2):
        return [base]
    return [(base[0] - h * uy, base[1] + h * ux), (base[0] + h * uy, base[1] - h * ux)]


def _cocircular(p: _Piece, q: _Piece, tol: float) -> bool:
    return not p.line and not q.line and math.dist(p.c, q.c) <= tol and abs(p.r - q.r) <= tol


def _contact_points(p: _Piece, q: _Piece, tol: float) -> list[Point]:
    """Every point where p and q meet: exact intersections, each end (or circle
    mark) of one lying within tol of the other, and the closest approach when
    they pass within tol without meeting."""
    pts: list[Point] = []
    if _cocircular(p, q, tol):
        pts += [m for m in p.marks() if q.dist(m) <= tol]
        pts += [m for m in q.marks() if p.dist(m) <= tol]
        return pts
    pts += [e for e in p.ends() if q.dist(e) <= tol]
    pts += [e for e in q.ends() if p.dist(e) <= tol]
    if p.line and q.line:
        (ax, ay), (bx, by) = p.a, p.b
        (cx, cy), (dx, dy) = q.a, q.b
        rx, ry, sx, sy = bx - ax, by - ay, dx - cx, dy - cy
        den = rx * sy - ry * sx
        if abs(den) > 1e-12 * p.span * q.span:
            t = ((cx - ax) * sy - (cy - ay) * sx) / den
            cands = [(ax + t * rx, ay + t * ry)]
        else:
            cands = []
    elif p.line or q.line:
        ln, ar = (p, q) if p.line else (q, p)
        cands = _roots_line_circle(ln.a, ln.b, ar.c, ar.r)
    else:
        cands = _roots_circle_circle(p.c, p.r, q.c, q.r)
    pts += [pt for pt in cands if p.dist(pt) <= tol and q.dist(pt) <= tol]
    if not pts:
        dist, pa, _ = _closest(p, q)
        if dist <= tol:
            pts.append(pa)
    return pts


def _on(piece: _Piece, pt: Point) -> bool:
    return piece.dist(pt) <= 1e-9 * max(1.0, piece.scale)


def _candidates(p: _Piece, q: _Piece) -> list[tuple[float, Point, Point]]:
    """Analytic candidates for the closest approach of two pieces, as
    (distance, point on p, point on q): each one's ends, the foot of each arc's
    centre on the other, and the points of each circle facing the other
    (normal to a line, or along the line of centres)."""
    out: list[tuple[float, Point, Point]] = []

    def on_p(pt: Point) -> None:
        qq = q.at(q.param(pt))
        out.append((math.dist(pt, qq), pt, qq))

    def on_q(pt: Point) -> None:
        pp = p.at(p.param(pt))
        out.append((math.dist(pp, pt), pp, pt))

    for m in p.marks():
        on_p(m)
    for m in q.marks():
        on_q(m)
    for x, y, put in ((p, q, on_p), (q, p, on_q)):
        if not y.line:
            put(x.at(x.param(y.c)))
    for x, y, put in ((p, q, on_p), (q, p, on_q)):
        if x.line:
            continue
        if y.line:
            dx, dy = y.b[0] - y.a[0], y.b[1] - y.a[1]
            n = math.hypot(dx, dy)
            dirs = [(-dy / n, dx / n), (dy / n, -dx / n)]
        else:
            d = math.dist(x.c, y.c)
            if d == 0:
                continue
            ux, uy = (y.c[0] - x.c[0]) / d, (y.c[1] - x.c[1]) / d
            dirs = [(ux, uy), (-ux, -uy)]
        for ux, uy in dirs:
            cand = (x.c[0] + x.r * ux, x.c[1] + x.r * uy)
            if _on(x, cand):
                put(cand)
    return out


def _closest(p: _Piece, q: _Piece) -> tuple[float, Point, Point]:
    return min(_candidates(p, q), key=lambda c: c[0])


def _pairs(pieces: list[_Piece], pad: float):
    """Candidate pairs whose padded bounding boxes overlap: a sweep along
    whichever axis separates the pieces better (a column of vents sweeps on y)."""
    if not pieces:
        return

    def crowding(ax: int) -> float:
        lo = min(p.box[ax] for p in pieces)
        hi = max(p.box[ax + 2] for p in pieces)
        return sum(p.box[ax + 2] - p.box[ax] + 2 * pad for p in pieces) / max(hi - lo, 1e-12)

    ax = 0 if crowding(0) <= crowding(1) else 1
    oth = 1 - ax
    order = sorted(range(len(pieces)), key=lambda i: pieces[i].box[ax])
    active: list[int] = []
    for i in order:
        bi = pieces[i].box
        lo = bi[ax] - pad
        active = [j for j in active if pieces[j].box[ax + 2] >= lo]
        for j in active:
            bj = pieces[j].box
            if bi[oth] - pad <= bj[oth + 2] and bj[oth] - pad <= bi[oth + 2]:
                yield (j, i) if j < i else (i, j)
        active.append(i)


def _positive_overlap(p: _Piece, q: _Piece, tol: float) -> Point | None:
    """Where two collinear lines or two co-circular arcs share a stretch of
    positive length, however short: found before welding, which would hide a
    stretch shorter than the tolerance. "Share" is tight (a tenth of the
    tolerance), so two paths that merely meet end to end with rounding jitter,
    such as two arcs on slightly different centres, don't count."""
    if p.line and q.line:
        # The stretch of p along which q runs within tol of it (q's lateral offset
        # varies linearly along p), clipped to both segments.
        ux, uy = (p.b[0] - p.a[0]) / p.span, (p.b[1] - p.a[1]) / p.span
        lat = lambda pt: (pt[0] - p.a[0]) * uy - (pt[1] - p.a[1]) * ux  # noqa: E731
        along = lambda pt: (pt[0] - p.a[0]) * ux + (pt[1] - p.a[1]) * uy  # noqa: E731
        (t0, l0), (t1, l1) = sorted(((along(q.a), lat(q.a)), (along(q.b), lat(q.b))))
        lo, hi = max(0.0, t0), min(p.span, t1)
        if hi <= lo:
            return None
        if t1 - t0 > 1e-12:
            slope = (l1 - l0) / (t1 - t0)
            if abs(slope) > 1e-15:
                a, b = sorted(((-tol - l0) / slope + t0, (tol - l0) / slope + t0))
                lo, hi = max(lo, a), min(hi, b)
            elif abs(l0) > tol:
                return None
        elif abs(l0) > tol and abs(l1) > tol:
            return None
        # Any two lines meeting at an angle are within tol near the joint (tol / sin of the
        # angle). That's a junction, not an overlap: only nearly parallel lines sharing a
        # stretch, or lines running within tol for over 20 tol (crossing at under ~6 deg,
        # coincident for any die), overlap.
        qx, qy = (q.b[0] - q.a[0]) / q.span, (q.b[1] - q.a[1]) / q.span
        parallel = abs(ux * qy - uy * qx) <= 1e-6
        if (parallel and hi - lo > 1e-8 * max(1.0, p.span)) or hi - lo > 20 * tol:
            m = (lo + hi) / 2
            return (p.a[0] + ux * m, p.a[1] + uy * m)
        return None
    if not _cocircular(p, q, tol / 10):
        return _curve_stretch(p, q, tol)
    tol = tol / 10
    ip, iq = _ccw_intervals(p), _ccw_intervals(q)
    for a0, a1 in ip:
        for b0, b1 in iq:
            lo, hi = max(a0, b0), min(a1, b1)
            if hi - lo > 1e-8:
                m = (lo + hi) / 2
                pt = (p.c[0] + p.r * math.cos(m), p.c[1] + p.r * math.sin(m))
                # Angles only line up on a common circle: check the stretch is really on both.
                if q.dist(pt) <= tol and p.dist(pt) <= tol:
                    return pt
    return None


def _curve_stretch(p: _Piece, q: _Piece, tol: float) -> Point | None:
    """A line and an arc, or two arcs on different circles, that run within tol
    of each other for more than 20 tol: a shallow crossing or a graze,
    coincident for any die. The within-tol band is solved analytically on p
    (the line, or one of the arcs), then sampled for the part on both pieces."""
    if not p.line and q.line:
        p, q = q, p
    bands: list[tuple[float, float]] = []  # ranges of p's own parameter, before clipping
    if p.line:
        ux, uy = (p.b[0] - p.a[0]) / p.span, (p.b[1] - p.a[1]) / p.span
        t0 = (q.c[0] - p.a[0]) * ux + (q.c[1] - p.a[1]) * uy
        h = abs((q.c[0] - p.a[0]) * uy - (q.c[1] - p.a[1]) * ux)
        outer = (q.r + tol) ** 2 - h * h
        if outer < 0:
            return None
        inner = math.sqrt(max((q.r - tol) ** 2 - h * h, 0.0))
        outer = math.sqrt(outer)
        # A graze (the line passes within tol of the circle's edge) is one band, not two halves.
        bands = [(t0 - outer, t0 + outer)] if inner == 0 else [(t0 - outer, t0 - inner), (t0 + inner, t0 + outer)]
        to_pt = lambda u: p.at(min(max(u, 0.0), p.span))  # noqa: E731
        lo_b, hi_b = 0.0, p.span
        unit = 1.0
    else:
        d = math.dist(p.c, q.c)
        if d == 0:
            return None
        phic = math.atan2(q.c[1] - p.c[1], q.c[0] - p.c[0])
        cmin = (p.r ** 2 + d ** 2 - (q.r + tol) ** 2) / (2 * p.r * d)
        cmax = (p.r ** 2 + d ** 2 - max(q.r - tol, 0.0) ** 2) / (2 * p.r * d)
        if cmin > 1 or cmax < -1:
            return None
        a_in, a_out = math.acos(min(cmax, 1.0)), math.acos(max(cmin, -1.0))
        bands = ([(phic - a_out, phic + a_out)] if a_in == 0 else
                 [(phic - a_out, phic - a_in), (phic + a_in, phic + a_out)])  # absolute angles
        to_pt = lambda ang: (p.c[0] + p.r * math.cos(ang), p.c[1] + p.r * math.sin(ang))  # noqa: E731
        lo_b, hi_b = -math.inf, math.inf
        unit = p.r
    def inside(u: float) -> bool:
        pt = to_pt(u)
        return (p.line or _on_sweep(p, pt)) and q.dist(pt) <= tol * (1 + 1e-9) and (q.line or _on_sweep(q, pt))

    def edge(out_u: float, in_u: float) -> float:
        """Bisect to the exact boundary between a point outside and one inside."""
        for _ in range(60):
            mid = (out_u + in_u) / 2
            if inside(mid):
                in_u = mid
            else:
                out_u = mid
        return in_u

    n = 64
    for a, b in bands:
        a, b = max(a, lo_b), min(b, hi_b)
        if (b - a) * unit <= 20 * tol:
            continue  # too short to be coincident, however it lies
        us = [a + (b - a) * k / n for k in range(n + 1)]
        flags = [inside(u) for u in us]
        k = 0
        while k <= n:
            if not flags[k]:
                k += 1
                continue
            start = us[k] if k == 0 else edge(us[k - 1], us[k])
            j = k
            while j < n and flags[j + 1]:
                j += 1
            end = us[j] if j == n else edge(us[j + 1], us[j])
            if (end - start) * unit > 20 * tol:
                return to_pt((start + end) / 2)
            k = j + 1
    return None


def _on_sweep(q: _Piece, pt: Point) -> bool:
    """Is pt's angle on q within q's sweep (not just near one of its ends)?"""
    return q.full or q.raw_param(pt) <= q.span


def _ccw_intervals(p: _Piece) -> list[tuple[float, float]]:
    """An arc's angular extent as counter-clockwise intervals within [0, 2tau)."""
    if p.full:
        return [(0.0, TAU), (TAU, 2 * TAU)]
    start = p.a0 if p.dirn > 0 else p.a0 - p.span
    lo = start % TAU
    return [(lo, lo + p.span), (lo + TAU, lo + TAU + p.span), (lo - TAU, lo - TAU + p.span)]


# ------------------------------------------------------------------ arrangement


@dataclass(slots=True)
class _Edge:
    piece: _Piece
    u0: float
    u1: float
    n0: int
    n1: int

    @property
    def op(self) -> Operation:
        return self.piece.op

    def mid(self) -> Point:
        return self.piece.at((self.u0 + self.u1) / 2)

    def leave(self, at_start: bool) -> tuple[float, float]:
        """(direction angle, signed curvature) leaving the node at this end."""
        pc = self.piece
        if pc.line:
            dx, dy = pc.b[0] - pc.a[0], pc.b[1] - pc.a[1]
            ang = math.atan2(dy, dx) if at_start else math.atan2(-dy, -dx)
            return ang % TAU, 0.0
        u = self.u0 if at_start else self.u1
        sgn = pc.dirn if at_start else -pc.dirn
        theta = pc.a0 + pc.dirn * u
        ang = theta + (math.pi / 2 if sgn > 0 else -math.pi / 2)
        return ang % TAU, sgn / pc.r


class _Arrangement:
    def __init__(self, pieces: list[_Piece], tol: float) -> None:
        self.tol = tol
        self.pieces = pieces
        marks: list[tuple[Point, int, float]] = []  # (point, piece, parameter)
        for i, pc in enumerate(pieces):
            if pc.full:  # two nodes, so a circle becomes two edges
                marks += [(pc.at(0.0), i, 0.0), (pc.at(math.pi), i, math.pi), (pc.at(0.0), i, TAU)]
            else:
                marks += [(pc.seg.start, i, 0.0), (pc.seg.end, i, pc.span)]
        self.contacts: dict[tuple[int, int], list[Point]] = {}
        self.overlaps: list[tuple[int, int, Point]] = []
        for i, j in _pairs(pieces, tol * 2):
            ov = _positive_overlap(pieces[i], pieces[j], tol)
            if ov is not None:
                self.overlaps.append((i, j, ov))
            pts = _contact_points(pieces[i], pieces[j], tol)
            if pts:
                self.contacts[(i, j)] = pts
            for pt in pts:
                for k in (i, j):
                    u = pieces[k].param(pt)
                    if pieces[k].full and u < tol / pieces[k].r:
                        u = 0.0
                    marks.append((pt, k, u))
        # Merge points closer than tol into nodes (union-find over a grid).
        parent = list(range(len(marks)))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        grid: dict[tuple[int, int], list[int]] = defaultdict(list)
        for k, (pt, _, _) in enumerate(marks):
            gx, gy = math.floor(pt[0] / tol), math.floor(pt[1] / tol)
            for ox in (-1, 0, 1):
                for oy in (-1, 0, 1):
                    for other in grid.get((gx + ox, gy + oy), ()):
                        if math.dist(pt, marks[other][0]) <= tol:
                            ra, rb = find(k), find(other)
                            if ra != rb:
                                parent[ra] = rb
            grid[(gx, gy)].append(k)
        roots: dict[int, int] = {}
        self.nodes: list[Point] = []
        node_of = [0] * len(marks)
        members: dict[int, set[int]] = defaultdict(set)
        for k, (pt, piece, _) in enumerate(marks):
            r = find(k)
            if r not in roots:
                roots[r] = len(self.nodes)
                self.nodes.append(pt)
            node_of[k] = roots[r]
            members[roots[r]].add(piece)
        # A weld may only join pieces that are themselves in contact: a chain of
        # points each within tol of the next mustn't invent a junction.
        self.ambiguous: list[Point] = []
        for n, ps in members.items():
            if len(ps) <= 2:
                continue
            ps_l = sorted(ps)
            here = self.nodes[n]
            bad = any(
                not any(math.dist(pt, here) <= 2 * tol for pt in self.contacts.get((ps_l[x], ps_l[y]), ()))
                for x in range(len(ps_l)) for y in range(x + 1, len(ps_l)))
            if bad:
                self.ambiguous.append(here)
        on_piece: dict[int, list[tuple[float, int]]] = defaultdict(list)
        for k, (_, i, u) in enumerate(marks):
            on_piece[i].append((u, node_of[k]))
        self.edges: list[_Edge] = []
        for i, pc in enumerate(pieces):
            stops = sorted(set(on_piece[i]))
            for (u0, n0), (u1, n1) in zip(stops, stops[1:]):
                if n0 == n1:
                    continue  # merged: nothing between them
                self.edges.append(_Edge(pc, u0, u1, n0, n1))
        self.incident: dict[int, list[tuple[int, bool]]] = defaultdict(list)  # node -> (edge, at its start)
        for e_id, e in enumerate(self.edges):
            self.incident[e.n0].append((e_id, True))
            self.incident[e.n1].append((e_id, False))


def _between(x: float, lo: float, hi: float) -> bool:
    """Is angle x strictly inside the CCW interval from lo to hi?"""
    span = (hi - lo) % TAU
    off = (x - lo) % TAU
    return ANGLE_EPS < off < span - ANGLE_EPS


def _same_dir(a: tuple[float, float], b: tuple[float, float]) -> bool:
    d = abs((a[0] - b[0] + math.pi) % TAU - math.pi)
    return d <= 1e-6 and abs(a[1] - b[1]) <= 1e-9


def _tangent_join(u: tuple[float, float], v: tuple[float, float], tol: float) -> bool:
    """A curve meeting another path tangentially (same or opposite direction).
    The angle allows for endpoint rounding on small radii: 0.006 deg at least,
    far below any join a die could make."""
    if u[1] == 0.0 and v[1] == 0.0:
        return False
    thr = max(1e-4, 2 * tol * max(abs(u[1]), abs(v[1])))
    return any(abs((u[0] - w + math.pi) % TAU - math.pi) <= thr for w in (v[0], v[0] + math.pi))


def _ang_eq(a: float, b: float) -> bool:
    return abs((a - b + math.pi) % TAU - math.pi) <= 1e-6


# ------------------------------------------------------------------ validation


def validate(d: Dieline) -> Report:
    rep = Report()
    k = 1.0 if d.units == "in" else 25.4
    tol = TOL_IN * k
    ops = ensure_ops(d)
    segs = [s for s in d.segments if s.type != LineType.ANNOTATION]

    for s in segs:
        coords = [*s.start, *s.end, *(s.center if isinstance(s, Arc) else ())]
        if not all(math.isfinite(v) and abs(v) <= MAX_COORD_IN * k for v in coords):
            rep.invalid.append(Issue("finite", "a coordinate isn't a finite number of a sensible size", None,
                                     (op_of(s, ops).id,)))
            continue
        if isinstance(s, Arc):
            msg = arc_issue(s, d.units)
            if msg:
                rep.invalid.append(Issue("arc", msg, s.start, (op_of(s, ops).id,)))
        elif math.dist(s.start, s.end) <= tol:
            rep.invalid.append(Issue("zero_length", "zero-length segment", s.start, (op_of(s, ops).id,)))
    if rep.invalid:
        return rep  # the arrangement assumes sound segments

    for op in {op_of(s, ops).id: op_of(s, ops) for s in segs}.values():
        if op.closed and op.open_ends != "none":
            rep.invalid.append(Issue("outline", f"{_name(op)} is closed, so it can't have open ends; a notch "
                                     "belongs to the outline it changes", None, (op.id,)))
    pieces = [_Piece(s, op_of(s, ops)) for s in segs]
    arr = _Arrangement(pieces, tol)
    for pt in arr.ambiguous[:5]:
        rep.invalid.append(Issue("weld", "points that aren't touching each other are within the tolerance of a "
                                 "common point; move them apart or make them meet", pt))
    _check_overlaps(arr, rep)
    _check_nodes(arr, rep)
    tagged = _tagged(d)
    if tagged and not any(p.op.kind == "outline" for p in pieces):
        rep.invalid.append(Issue("outline", "there is no outline operation"))
    if not rep.invalid:
        _material(arr, rep, tagged)
    if not rep.invalid:
        _near_misses(pieces, arr, rep, tol)
    return rep


def _name(op: Operation) -> str:
    return op.id.lstrip("_") if op.id.startswith("_") else f"{op.kind} {op.id}"


def _check_overlaps(arr: _Arrangement, rep: Report) -> None:
    seen: set[tuple[str, ...]] = set()

    def report(a: Operation, b: Operation, pt: Point) -> None:
        key = tuple(sorted((a.id, b.id)))
        if key in seen:
            return
        seen.add(key)
        if a.id == b.id:
            msg = f"{_name(a)} overlaps itself (duplicate rule)"
        else:
            msg = f"{_name(a)} and {_name(b)} overlap (coincident rules)"
        rep.invalid.append(Issue("overlap", msg, pt, key))

    for i, j, pt in arr.overlaps:
        report(arr.pieces[i].op, arr.pieces[j].op, pt)
    groups: dict[tuple[int, int], list[_Edge]] = defaultdict(list)
    for e in arr.edges:
        groups[(min(e.n0, e.n1), max(e.n0, e.n1))].append(e)
    for group in groups.values():
        for x in range(len(group)):
            for y in range(x + 1, len(group)):
                a, b = group[x], group[y]
                # Same path if every sample of one lies within the tolerance of the other.
                sa = [a.piece.at(a.u0 + (a.u1 - a.u0) * f) for f in (0.25, 0.5, 0.75)]
                sb = [b.piece.at(b.u0 + (b.u1 - b.u0) * f) for f in (0.25, 0.5, 0.75)]
                if all(_edge_dist(b, p) <= arr.tol for p in sa) and all(_edge_dist(a, p) <= arr.tol for p in sb):
                    report(a.op, b.op, a.mid())


def _edge_dist(e: _Edge, p: Point) -> float:
    u = min(max(e.piece.param(p), e.u0), e.u1)
    return math.dist(p, e.piece.at(u))


def _check_nodes(arr: _Arrangement, rep: Report) -> None:
    """Classify every node from the edges meeting there."""
    for node, inc in arr.incident.items():
        p = arr.nodes[node]
        by_op: dict[str, list[tuple[float, float]]] = defaultdict(list)
        op_obj: dict[str, Operation] = {}
        for e_id, at_start in inc:
            e = arr.edges[e_id]
            by_op[e.op.id].append(e.leave(at_start))
            op_obj[e.op.id] = e.op
        flagged: set[str] = set()
        for op_id, dirs in by_op.items():
            op = op_obj[op_id]
            if len(dirs) > 2:
                flagged.add(op_id)
                rep.invalid.append(Issue("branch", f"{_name(op)} branches here ({len(dirs)} ends meet)", p, (op_id,)))
            elif len(dirs) == 2 and _same_dir(dirs[0], dirs[1]):
                flagged.add(op_id)
                rep.invalid.append(Issue("overlap", f"{_name(op)} doubles back on itself", p, (op_id,)))
        ids = sorted(o for o in by_op if o not in flagged)
        for x in range(len(ids)):
            for y in range(x + 1, len(ids)):
                a, b = op_obj[ids[x]], op_obj[ids[y]]
                da, db = by_op[a.id], by_op[b.id]
                if any(_same_dir(u, v) for u in da for v in db):
                    rep.invalid.append(Issue("tangent", f"{_name(a)} runs along {_name(b)}", p, (a.id, b.id)))
                    continue
                if len(da) >= 2 and len(db) >= 2:
                    lo, hi = da[0][0], da[1][0]
                    if any(_ang_eq(v[0], u[0]) for u in da[:2] for v in db[:2]):
                        crossing = False  # tangent paths meeting here
                    else:
                        crossing = _between(db[0][0], lo, hi) != _between(db[1][0], lo, hi)
                    if not crossing:
                        rep.invalid.append(Issue("tangent", f"{_name(a)} touches {_name(b)}", p, (a.id, b.id)))
                    elif not (a.allows(b, "crosses") or b.allows(a, "crosses")):
                        rep.invalid.append(Issue("crossing", f"{_name(a)} crosses {_name(b)}", p, (a.id, b.id)))
                    continue
                ender, other_dirs = (da, db) if len(da) == 1 else (db, da)
                if any(_tangent_join(ender[0], v, arr.tol) for v in other_dirs):
                    # Ending tangent to a curve (or with a curve tangent to the other path)
                    # leaves a sliver the die can't make. Straight lines meeting end to end in
                    # line are ordinary: a flap crease ending where the slot-bottom cut continues.
                    rep.invalid.append(Issue("tangent", f"{_name(a)} meets {_name(b)} tangentially", p, (a.id, b.id)))
                    continue
                if len(da) == 1 and len(db) == 1:
                    # Two paths ending at one point: fine if either may end on the other, or
                    # both end here on a third path they may end on (a cut and its relief nick).
                    third = [op_obj[o] for o in ids if o not in (a.id, b.id)]
                    ok = (a.allows(b, "ends_on") or b.allows(a, "ends_on")
                          or any(a.allows(t, "ends_on") and b.allows(t, "ends_on") for t in third))
                elif len(da) == 1:
                    ok = a.allows(b, "ends_on")
                else:
                    ok = b.allows(a, "ends_on")
                if not ok:
                    who, other = (a, b) if len(da) == 1 else (b, a)
                    rep.invalid.append(Issue("contact", f"{_name(who)} ends on {_name(other)} without declaring it",
                                             p, (a.id, b.id)))
        for op_id, dirs in by_op.items():
            if len(dirs) != 1:
                continue
            op = op_obj[op_id]
            if op.open_ends == "free":
                continue
            if op.open_ends == "on" and any(op.allows(op_obj[o], "ends_on") for o in by_op if o != op_id):
                continue
            rep.invalid.append(Issue("open_end", f"{_name(op)} has an open end", p, (op_id,)))


# ------------------------------------------------------------------ material


def _ends_of(arr: _Arrangement, e: _Edge, fwd: bool = True) -> tuple[Point, Point]:
    """An edge's endpoints as its welded nodes, so cycles close exactly."""
    a, b = arr.nodes[e.n0], arr.nodes[e.n1]
    return (a, b) if fwd else (b, a)


def _cycle_area(arr: _Arrangement, cycle: list[tuple[int, bool]]) -> float:
    ox, oy = arr.nodes[arr.edges[cycle[0][0]].n0]  # a local origin: less cancellation, no translation drift
    area = 0.0
    for e_id, fwd in cycle:
        e = arr.edges[e_id]
        (x0, y0), (x1, y1) = _ends_of(arr, e, fwd)
        area += ((x0 - ox) * (y1 - oy) - (x1 - ox) * (y0 - oy)) / 2
        if not e.piece.line:
            phi = (e.u1 - e.u0) * e.piece.dirn * (1 if fwd else -1)
            area += e.piece.r ** 2 / 2 * (phi - math.sin(phi))
    return area


class _Monotone:
    """Edges split into pieces monotone in y, for ray casting with one
    half-open rule (lower end in, upper end out) for lines and arcs alike."""

    def __init__(self, arr: _Arrangement, edge_ids) -> None:
        self.parts: list[tuple[float, float, float, float, _Piece | None, float]] = []
        for e_id in edge_ids:
            e = arr.edges[e_id]
            p0, p1 = _ends_of(arr, e)
            pc = e.piece
            if pc.line:
                self.parts.append((p0[0], p0[1], p1[0], p1[1], None, 0.0))
                continue
            cuts = [e.u0]
            for k in range(-2, 4):
                u = pc.dirn * (math.pi / 2 + k * math.pi - pc.a0)
                for uu in (u % TAU, u % TAU + TAU):
                    if e.u0 < uu < e.u1:
                        cuts.append(uu)
            cuts = sorted(set(cuts)) + [e.u1]
            for ua, ub in zip(cuts, cuts[1:]):
                a = p0 if ua == e.u0 else pc.at(ua)
                b = p1 if ub == e.u1 else pc.at(ub)
                side = math.cos(pc.a0 + pc.dirn * (ua + ub) / 2)
                self.parts.append((a[0], a[1], b[0], b[1], pc, 1.0 if side >= 0 else -1.0))
        ys = [v for part in self.parts for v in (part[1], part[3])]
        self.lo = min(ys) if ys else 0.0
        hi = max(ys) if ys else 1.0
        n = max(1, len(self.parts))
        self.h = max((hi - self.lo) / (4 * math.sqrt(n) + 1), 1e-9)
        self.strips: dict[int, list[int]] = defaultdict(list)
        for idx, (_, y0, _, y1, _, _) in enumerate(self.parts):
            for k in range(math.floor((min(y0, y1) - self.lo) / self.h), math.floor((max(y0, y1) - self.lo) / self.h) + 1):
                self.strips[k].append(idx)

    def crossings(self, p: Point) -> int:
        px, py = p
        count = 0
        for idx in self.strips.get(math.floor((py - self.lo) / self.h), ()):
            x0, y0, x1, y1, pc, side = self.parts[idx]
            if (y0 > py) == (y1 > py):
                continue
            if pc is None:
                x = x0 + (py - y0) * (x1 - x0) / (y1 - y0)
            else:
                dy = py - pc.c[1]
                x = pc.c[0] + side * math.sqrt(max(pc.r * pc.r - dy * dy, 0.0))
            if x > px:
                count += 1
        return count


def _interior(arr: _Arrangement, cycle: list[tuple[int, bool]], own: _Monotone) -> Point | None:
    """A point just inside a face, to the left of one of its boundary edges."""
    # Half the tolerance: any other boundary that close would have been a contact and a node,
    # so the probe lands in this face, not in a neighbouring component.
    return _interior_at(arr, cycle, own, arr.tol / 2)


def _interior_at(arr: _Arrangement, cycle: list[tuple[int, bool]], own: _Monotone, eps: float) -> Point | None:
    for e_id, fwd in cycle:
        e = arr.edges[e_id]
        pc = e.piece
        if pc.line:
            # The welded edge, as the ray cast sees it: between its nodes, not the original line.
            (x0, y0), (x1, y1) = _ends_of(arr, e)
            m = ((x0 + x1) / 2, (y0 + y1) / 2)
            dx, dy = x1 - x0, y1 - y0
        else:
            um = (e.u0 + e.u1) / 2
            m = pc.at(um)
            th = pc.a0 + pc.dirn * um
            dx, dy = -math.sin(th) * pc.dirn, math.cos(th) * pc.dirn
        if not fwd:
            dx, dy = -dx, -dy
        n = math.hypot(dx, dy)
        if n == 0:
            continue
        cand = (m[0] - dy / n * eps, m[1] + dx / n * eps)
        if own.crossings(cand) % 2 == 1:
            return cand
    return None


def _material(arr: _Arrangement, rep: Report, tagged: bool) -> None:
    """Board or scrap is decided by the closed contours alone (outline and
    holes, even-odd); a slit only divides board. The pieces are then the board
    faces of the whole cut network."""
    cut_edges = [i for i, e in enumerate(arr.edges) if e.piece.seg.type == LineType.CUT]
    closed = [i for i in cut_edges if arr.edges[i].op.closed]
    if not closed:
        rep.invalid.append(Issue("outline", "no closed cut: there is no outline"))
        return
    contours = _Monotone(arr, closed)
    outline_edges = [i for i in closed if arr.edges[i].op.kind == "outline"]
    outline = _Monotone(arr, outline_edges) if outline_edges else None  # tagged or partly tagged

    def on_board(pt: Point) -> bool:
        if outline is not None and outline.crossings(pt) % 2 == 0:
            return False  # outside the stock, whatever holes say
        return contours.crossings(pt) % 2 == 1

    if outline is not None:
        for i in closed:
            e = arr.edges[i]
            if e.op.kind != "outline" and outline.crossings(e.mid()) % 2 == 0:
                rep.invalid.append(Issue("off_board", f"{_name(e.op)} lies outside the outline", e.mid(), (e.op.id,)))
                return

    # Dangling cut edges (slits, hand-flap cuts ending on creases) don't bound faces.
    alive = set(cut_edges)
    deg: dict[int, int] = defaultdict(int)
    for i in alive:
        deg[arr.edges[i].n0] += 1
        deg[arr.edges[i].n1] += 1
    stack = [n for n, v in deg.items() if v == 1]
    while stack:
        n = stack.pop()
        if deg[n] != 1:
            continue
        for e_id, _ in arr.incident[n]:
            if e_id in alive:
                e = arr.edges[e_id]
                alive.discard(e_id)
                deg[e.n0] -= 1
                deg[e.n1] -= 1
                other = e.n1 if e.n0 == n else e.n0
                if deg[other] == 1:
                    stack.append(other)
                break
    out: dict[int, list[tuple[float, float, int, bool]]] = defaultdict(list)
    for i in alive:
        e = arr.edges[i]
        a0, k0 = e.leave(True)
        a1, k1 = e.leave(False)
        out[e.n0].append((a0, k0, i, True))
        out[e.n1].append((a1, k1, i, False))
    order: dict[int, list[tuple[int, bool]]] = {}
    for n, hs in out.items():
        hs.sort(key=lambda h: (h[0], h[1]))
        for x in range(1, len(hs)):  # equal directions: by curvature (left-turning is more CCW)
            y = x
            while y > 0 and abs(hs[y][0] - hs[y - 1][0]) <= 1e-6 and hs[y][1] < hs[y - 1][1]:
                hs[y], hs[y - 1] = hs[y - 1], hs[y]
                y -= 1
        order[n] = [(h[2], h[3]) for h in hs]

    def head(h: tuple[int, bool]) -> int:
        e = arr.edges[h[0]]
        return e.n1 if h[1] else e.n0

    seen: set[tuple[int, bool]] = set()
    board: list[tuple[float, list[tuple[int, bool]]]] = []
    for i in sorted(alive):
        for fwd in (True, False):
            if (i, fwd) in seen:
                continue
            cyc: list[tuple[int, bool]] = []
            cur = (i, fwd)
            while cur not in seen:
                seen.add(cur)
                cyc.append(cur)
                ring = order[head(cur)]
                cur = ring[ring.index((cur[0], not cur[1])) - 1]  # just clockwise of the twin
            area = _cycle_area(arr, cyc)
            if area <= arr.tol * arr.tol:
                continue  # an outer boundary, not a face
            probe = _interior(arr, cyc, _Monotone(arr, {e for e, _ in cyc}))
            if probe is not None and on_board(probe):
                board.append((area, cyc))
    rep.material_faces = len(board)
    if not board:
        rep.invalid.append(Issue("outline", "the cuts don't enclose any board"))
        return
    if tagged or outline is not None:
        blanks = [b for b in board if any(arr.edges[i].op.kind == "outline" for i, _ in b[1])]
        if not blanks:
            rep.invalid.append(Issue("outline", "the outline doesn't enclose the board"))
            return
    else:
        blanks = board
    blank = max(blanks, key=lambda b: b[0])
    for b in board:
        if b is blank:
            continue
        sliced = any(not arr.edges[i].op.closed for i, _ in b[1])
        kind = "split" if sliced else "island"
        msg = ("a cut divides the blank into separate pieces" if sliced
               else "a piece of board inside a cut-out has nothing holding it (an island)")
        e = arr.edges[b[1][0][0]]
        bound = tuple(dict.fromkeys(arr.edges[i].piece.seg for i, _ in b[1]))
        rep.islands.append(Issue(kind, msg, e.mid(), segs=bound))
    # Every rule and every open cut must lie on the board (closed contours are the boundary itself).
    for i, e in enumerate(arr.edges):
        if e.piece.seg.type == LineType.CUT and e.op.closed:
            continue
        pt = e.mid()
        if not on_board(pt):
            rep.invalid.append(Issue("off_board", f"{_name(e.op)} lies outside the board", pt, (e.op.id,)))
            return


# ------------------------------------------------------------------ near misses


def _near_misses(pieces: list[_Piece], arr: _Arrangement, rep: Report, tol: float) -> None:
    """Clearance is measured on the true lines and arcs, piece by piece. Where
    two pieces meet at a contact, only the neighbourhood of that contact is
    exempt, so a path that ends on a hinge and later swings back near it still warns."""
    pad = max((p.op.clearance for p in pieces), default=0.0)
    if pad <= 0:
        return
    found: set[frozenset[str]] = set()
    op_contacts: dict[frozenset[str], list[Point]] = defaultdict(list)
    for (i, j), pts in arr.contacts.items():
        op_contacts[frozenset((pieces[i].op.id, pieces[j].op.id))] += pts
    for i, j in _pairs(pieces, pad):
        a, b = pieces[i], pieces[j]
        clearance = max(a.op.clearance, b.op.clearance)
        if clearance <= 0 or a.op.id == b.op.id:
            continue
        key = frozenset((a.op.id, b.op.id))
        if key in found:
            continue
        near = op_contacts.get(key, [])
        cands = [c for c in _candidates(a, b)
                 if all(math.dist(c[1], q) > clearance and math.dist(c[2], q) > clearance for q in near)]
        if not cands:
            continue
        dist, pa, _ = min(cands, key=lambda c: c[0])
        if tol < dist < clearance:
            found.add(key)
            src, other = (a.op, b.op) if a.op.clearance >= b.op.clearance else (b.op, a.op)
            rep.near_misses.append(Issue("near_miss", f"{_name(src)} is {dist:.4g} from {_name(other)} "
                                         f"(clearance {clearance:g})", pa, (a.op.id, b.op.id)))


# ------------------------------------------------------------------ outputs


def reconstruct(design: Dieline, output: Dieline) -> tuple[Dieline | None, str | None]:
    """Map an output file's segments (read back from its own text) onto the
    design's segments and operations, so the same rules can run on the
    rounded geometry. Every design segment must be covered once along its
    whole length: nothing missing, nothing repeated.

    The match tolerance is tied to what the writers can lose: six decimals,
    and a millimetre round trip.

    Returns (dieline with op tags, None) or (None, reason)."""
    k_des = 1.0 if design.units == "in" else 25.4
    k = k_des / (1.0 if output.units == "in" else 25.4)  # output units -> design units
    ops = ensure_ops(design)
    out = Dieline(name=output.name, units=design.units, ops=dict(ops))
    tol = TOL_IN * k_des * 2
    src = [_Piece(s, op_of(s, ops)) for s in design.segments if s.type != LineType.ANNOTATION]
    # Cells about the size of a typical segment: a fine logo doesn't crowd one cell,
    # and a long outline edge is indexed along its path only.
    lengths = sorted(pc.span * pc.scale for pc in src) or [k_des]
    cell = min(max(2 * lengths[len(lengths) // 2], tol * 100), 0.5 * k_des)
    grid: dict[tuple[int, int], set[int]] = defaultdict(set)
    for idx, pc in enumerate(src):
        # Cells along the path (not the whole bounding box): a long diagonal stays cheap.
        steps = max(1, math.ceil(pc.span * pc.scale / (cell / 2)))
        for step in range(steps + 1):
            x, y = pc.at(pc.span * step / steps)
            grid[(math.floor(x / cell), math.floor(y / cell))].add(idx)
    cover: dict[int, list[tuple[float, float]]] = defaultdict(list)

    def where(p: Point) -> str:
        return f"({p[0]:.4g}, {p[1]:.4g})"

    for s in output.segments:
        if s.type == LineType.ANNOTATION:
            continue
        coords = [*s.start, *s.end, *(s.center if isinstance(s, Arc) else ())]
        if not all(math.isfinite(v) and abs(v) <= MAX_COORD_IN * (1.0 if output.units == "in" else 25.4) for v in coords):
            return None, "a coordinate isn't a finite number of a sensible size"
        s = _scale(s, k)
        if isinstance(s, Arc) and arc_issue(s, design.units):
            return None, f"a degenerate arc at {where(s.start)}"
        if isinstance(s, Line) and math.dist(s.start, s.end) <= TOL_IN * k_des:
            return None, f"a zero-length {s.type.value} at {where(s.start)}"
        best = None
        gx, gy = math.floor(s.start[0] / cell), math.floor(s.start[1] / cell)
        near = set().union(*(grid.get((gx + ox, gy + oy), ()) for ox in (-1, 0, 1) for oy in (-1, 0, 1)))
        for idx in sorted(near):
            t = src[idx]
            if t.seg.type != s.type:
                continue
            fit = _fit(t, s, tol)
            if fit is None:
                continue
            resid, ivs = fit
            clash = any(_overlap(iv, c, tol / t.scale) for iv in ivs for c in cover[idx])
            score = (clash, resid)
            if best is None or score < best[0]:
                best = (score, idx, ivs)
        if best is None:
            return None, f"a {s.type.value} at {where(s.start)} has no counterpart in the design"
        _, idx, ivs = best
        cover[idx] += ivs
        tag = src[idx].op.id
        if isinstance(s, Line):
            out.segments.append(Line(s.start, s.end, s.type, tag))
        else:
            out.segments.append(Arc(s.start, s.end, s.center, s.clockwise, s.type, s.full_circle, tag))
    for idx, t in enumerate(src):
        ivs = sorted(iv for iv in cover[idx] if iv[1] > iv[0])
        if not ivs:
            return None, f"the file is missing a {t.seg.type.value} near {where(t.at(0.0))}"
        union = 0.0
        lo, hi = ivs[0]
        for a, b in ivs[1:]:
            if a > hi:
                union += hi - lo
                lo, hi = a, b
            else:
                hi = max(hi, b)
        union += hi - lo
        total = sum(b - a for a, b in ivs)
        if (t.span - union) * t.scale > tol:
            return None, f"the file is missing part of a {t.seg.type.value} near {where(t.at(0.0))}"
        if (total - union) * t.scale > tol:
            return None, f"the file repeats part of a {t.seg.type.value} near {where(t.at(0.0))}"
    return out, None


def _overlap(a: tuple[float, float], b: tuple[float, float], tol: float) -> bool:
    return min(a[1], b[1]) - max(a[0], b[0]) > tol


def _fit(t: _Piece, s: Segment, tol: float) -> tuple[float, list[tuple[float, float]]] | None:
    """How output segment s lies on design piece t: (residual, intervals of t it covers)."""
    if t.line != isinstance(s, Line):
        return None
    if isinstance(s, Line):
        mid = ((s.start[0] + s.end[0]) / 2, (s.start[1] + s.end[1]) / 2)
        resid = max(t.dist(s.start), t.dist(s.end), t.dist(mid))
        if resid > tol:
            return None
        u0, u1 = sorted((t.param(s.start), t.param(s.end)))
        # tol is the match tolerance (twice the physical one); the covered length must agree
        # to half the physical tolerance, so a short substitute can't map onto nothing.
        if abs((u1 - u0) - math.dist(s.start, s.end)) > tol / 4:
            return None  # its ends project onto the source, but it doesn't run along it
        return resid, [(u0, u1)]
    if math.dist(t.c, s.center) > tol or abs(t.r - math.dist(s.center, s.start)) > tol:
        return None
    sp = _Piece(s, t.op)
    mid = sp.at(sp.span / 2)
    resid = max(t.dist(s.start), t.dist(mid), t.dist(s.end))
    if resid > tol:
        return None
    if s.full_circle:
        return (resid, [(0.0, TAU)]) if t.full else None
    same = s.clockwise == t.seg.clockwise
    lo = t.raw_param(s.start if same else s.end)
    if not t.full:
        if TAU - lo <= tol / t.r:
            lo -= TAU  # just before the start: keep the offset, so a mirror image covers nothing
        hi = lo + sp.span
        lo_c, hi_c = max(lo, 0.0), min(hi, t.span)
        # The part of the source it covers must be as long as it is, to half the physical tolerance.
        if hi > t.span + tol / 4 / t.r or (hi_c - lo_c) * t.r < sp.span * t.r - tol / 4:
            return None
        return resid, [(lo_c, hi_c)]
    hi = lo + sp.span
    if hi <= TAU:
        return resid, [(lo, hi)]
    return resid, [(lo, TAU), (0.0, hi - TAU)]


def _scale(s: Segment, k: float) -> Segment:
    if k == 1.0:
        return s
    sc = lambda p: (p[0] * k, p[1] * k)  # noqa: E731
    if isinstance(s, Line):
        return Line(sc(s.start), sc(s.end), s.type, s.op)
    return Arc(sc(s.start), sc(s.end), sc(s.center), s.clockwise, s.type, s.full_circle, s.op)

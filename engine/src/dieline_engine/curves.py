"""Curves for artwork: Béziers, elliptical arcs and splines, fitted with arcs
(docs/DESIGN-SYSTEM.md, "Artwork / Curves").

Dies and the sample table want smooth, bendable curves, not tiny chords, so
every curve is fitted with **biarcs** (pairs of tangent circular arcs) within a
tolerance at final size (default 0.002"). A piece where arcs can't fit falls
back to straight chords within the same tolerance; the fit reports how many.

A fitted outline keeps its **source curves**: the tooling checks measure bend
radius from the source curvature, so their results don't depend on how the
curve was fitted.

Everything here works in one local frame, in inches, y up.
"""

from __future__ import annotations

import bisect
import math
from collections import Counter
from dataclasses import dataclass, field

from .shapes import Prim
from .topology import MAX_COORD_IN

Point = tuple[float, float]
FIT_TOL_IN = 0.002
MAX_DEPTH = 14
MAX_SPLINE_POINTS = 5000  # control points in one spline, as stored geometry allows
ARC_CORRECTION_MAX = 0.01  # an SVG arc's radius may be enlarged by at most 1%


class CurveError(ValueError):
    pass


def _sub(a: Point, b: Point) -> Point:
    return (a[0] - b[0], a[1] - b[1])


def _dot(a: Point, b: Point) -> float:
    return a[0] * b[0] + a[1] * b[1]


def _cross(a: Point, b: Point) -> float:
    return a[0] * b[1] - a[1] * b[0]


def _unit(v: Point) -> Point | None:
    n = math.hypot(*v)
    return None if n <= 1e-15 else (v[0] / n, v[1] / n)


# ------------------------------------------------------------------ curves


class Curve:
    """A parametric curve on t in [0, 1]."""

    def point(self, t: float) -> Point:  # pragma: no cover - interface
        raise NotImplementedError

    def deriv(self, t: float, h: float = 1e-6) -> Point:
        a, b = self.point(max(t - h, 0.0)), self.point(min(t + h, 1.0))
        span = min(t + h, 1.0) - max(t - h, 0.0)
        return ((b[0] - a[0]) / span, (b[1] - a[1]) / span)

    def map(self, f) -> "Curve":
        """The curve through an affine map f (points only)."""
        return Mapped(self, f)

    def breaks(self) -> list[float]:
        """Where the curve's spans join (t in [0, 1]). A spline can trace a whole loop inside one short
        span and sit still in the rest, so sampling looks at every span, not only evenly along t."""
        return [0.0, 1.0]

    def pieces(self) -> list["Curve"]:
        """The curve cut where it may turn a corner (a spline's C0 knots), each piece its own curve on
        [0, 1]: differences taken inside a piece never reach across a corner."""
        return [self]

    def per_span(self) -> int:
        """Samples per span between breaks, enough for its polynomial degree."""
        return 8

    def polyline(self) -> list[Point] | None:
        """Its exact vertices when it's straight between its breaks (a degree-1 spline), else None."""
        return None

    def tangent(self, t: float, forward: bool = True) -> Point | None:
        """The unit tangent, looking a little further when the derivative vanishes
        (a Bézier whose control point sits on its end)."""
        u = _unit(self.deriv(t))
        if u is not None:
            return u
        for h in (1e-4, 1e-3, 1e-2):
            a = self.point(t)
            b = self.point(min(t + h, 1.0) if forward else max(t - h, 0.0))
            u = _unit(_sub(b, a) if forward else _sub(a, b))
            if u is not None:
                return u
        return None

    def radius_samples(self, n: int = 32) -> list[tuple[float, Point]]:
        """(radius of curvature, point) along the curve; a straight stretch has none. Evenly along
        each piece and through each of its spans, then closer in round the tightest."""
        if self.polyline() is not None:
            return []
        out = []
        for piece in self.pieces():
            brk = piece.breaks()
            # Evenly, and four through each span (a spline of thousands of spans stays quick).
            ts = sorted({*(i / n for i in range(n + 1)),
                         *(a + (b - a) * j / 4 for a, b in zip(brk, brk[1:]) if b > a for j in range(4))})
            got = _radii(piece, brk, ts)
            # Refine round the tightest few: a hairpin's peak can sit between samples.
            for _ in range(6):  # each round narrows the bracket round each of the tightest
                if not got:
                    break
                more = set()
                for _r, _p, t in sorted(got, key=lambda x: x[0])[:4]:
                    i = bisect.bisect_left(ts, t)
                    lo, hi = ts[max(i - 1, 0)], ts[min(i + 1, len(ts) - 1)]
                    more |= {lo + (hi - lo) * j / 8 for j in range(1, 8)}
                more -= set(ts)
                got += _radii(piece, brk, sorted(more))
                ts = sorted({*ts, *more})
            out += [(r, q) for r, q, _t in got]
        return out


def sample_ts(curve: Curve, n: int = 48) -> list[float]:
    """t values along a curve: n evenly, and the curve's own per-span samples through each span."""
    ts = curve.breaks()
    per = curve.per_span() if len(ts) > 2 else 1
    return sorted({*(i / n for i in range(n + 1)),
                   *(a + (b - a) * j / per for a, b in zip(ts, ts[1:]) if b > a for j in range(per)), 1.0})


def sample_points(curve: Curve, n: int = 48) -> list[Point]:
    """Points along a curve, every span of it included (for its extent)."""
    pts = curve.polyline()
    if pts is not None:
        return pts
    return [q.point(t) for q in curve.pieces() for t in sample_ts(q, n)]


def _radii(curve: Curve, brk: list[float], ts: list[float]) -> list[tuple[float, Point, float]]:
    """The radius at each t: the circle through the curve at t and just either side of it, all three
    inside t's own span (never across a knot). No parameter step is divided by, so a span a few
    floats wide is simply skipped when its points coincide."""
    out = []
    for t in ts:
        i = min(max(bisect.bisect_right(brk, t), 1), len(brk) - 1)
        a, b = brk[i - 1], brk[i]
        h = min(1e-4, (b - a) / 4)
        t0, t1 = max(t - h, a), min(t + h, b)
        if not t0 < t < t1:
            t = (t0 + t1) / 2
            if not t0 < t < t1:
                continue
        p0, p, p1 = curve.point(t0), curve.point(t), curve.point(t1)
        u, v, w = _sub(p, p0), _sub(p1, p), _sub(p1, p0)
        cr = abs(_cross(u, v))
        lu, lv, lw = math.hypot(*u), math.hypot(*v), math.hypot(*w)
        if cr <= 1e-15 * max(lu * lv, 1e-300) or min(lu, lv) <= 1e-12:
            continue  # straight there, or the points coincide
        out.append((lu * lv * lw / (2 * cr), p, t))
    return out


@dataclass(slots=True)
class Mapped(Curve):
    base: Curve
    f: object

    def point(self, t: float) -> Point:
        return self.f(self.base.point(t))  # type: ignore[operator]

    def breaks(self) -> list[float]:
        return self.base.breaks()

    def pieces(self) -> list[Curve]:
        parts = self.base.pieces()
        return [self] if len(parts) == 1 else [Mapped(q, self.f) for q in parts]

    def per_span(self) -> int:
        return self.base.per_span()

    def polyline(self) -> list[Point] | None:
        pts = self.base.polyline()
        return None if pts is None else [self.f(q) for q in pts]  # type: ignore[operator]


@dataclass(slots=True)
class Window(Curve):
    """The part of a curve on [ta, tb], as a curve on [0, 1], with its own breaks."""

    base: Curve
    ta: float
    tb: float
    brk: list[float]

    def point(self, t: float) -> Point:
        return self.base.point(self.ta + (self.tb - self.ta) * min(max(t, 0.0), 1.0))

    def breaks(self) -> list[float]:
        return self.brk

    def per_span(self) -> int:
        return self.base.per_span()


@dataclass(slots=True)
class Bezier(Curve):
    """A quadratic (3 points) or cubic (4 points) Bézier."""

    pts: tuple[Point, ...]

    def point(self, t: float) -> Point:
        p = self.pts
        u = 1 - t
        if len(p) == 3:
            return (u * u * p[0][0] + 2 * u * t * p[1][0] + t * t * p[2][0],
                    u * u * p[0][1] + 2 * u * t * p[1][1] + t * t * p[2][1])
        return (u ** 3 * p[0][0] + 3 * u * u * t * p[1][0] + 3 * u * t * t * p[2][0] + t ** 3 * p[3][0],
                u ** 3 * p[0][1] + 3 * u * u * t * p[1][1] + 3 * u * t * t * p[2][1] + t ** 3 * p[3][1])

    def deriv(self, t: float, h: float = 1e-6) -> Point:  # exact: h unused
        p = self.pts
        u = 1 - t
        if len(p) == 3:
            return (2 * u * (p[1][0] - p[0][0]) + 2 * t * (p[2][0] - p[1][0]),
                    2 * u * (p[1][1] - p[0][1]) + 2 * t * (p[2][1] - p[1][1]))
        return (3 * u * u * (p[1][0] - p[0][0]) + 6 * u * t * (p[2][0] - p[1][0]) + 3 * t * t * (p[3][0] - p[2][0]),
                3 * u * u * (p[1][1] - p[0][1]) + 6 * u * t * (p[2][1] - p[1][1]) + 3 * t * t * (p[3][1] - p[2][1]))

    def map(self, f) -> "Curve":
        return Bezier(tuple(f(q) for q in self.pts))  # affine maps keep Béziers Béziers


@dataclass(slots=True)
class EllipseArc(Curve):
    """centre + R(phi) (rx cos th, ry sin th), th from th0 by dth (signed)."""

    c: Point
    rx: float
    ry: float
    phi: float
    th0: float
    dth: float

    def point(self, t: float) -> Point:
        th = self.th0 + self.dth * t
        x, y = self.rx * math.cos(th), self.ry * math.sin(th)
        cp, sp = math.cos(self.phi), math.sin(self.phi)
        return (self.c[0] + x * cp - y * sp, self.c[1] + x * sp + y * cp)

    def deriv(self, t: float, h: float = 1e-6) -> Point:  # exact: h unused
        th = self.th0 + self.dth * t
        x, y = -self.rx * math.sin(th) * self.dth, self.ry * math.cos(th) * self.dth
        cp, sp = math.cos(self.phi), math.sin(self.phi)
        return (x * cp - y * sp, x * sp + y * cp)

    def radius_samples(self, n: int = 32) -> list[tuple[float, Point]]:
        out = []
        for i in range(n + 1):
            th = self.th0 + self.dth * i / n
            s, c = math.sin(th), math.cos(th)
            r = (self.rx ** 2 * s * s + self.ry ** 2 * c * c) ** 1.5 / (self.rx * self.ry)
            out.append((r, self.point(i / n)))
        return out


@dataclass(slots=True)
class AxisEllipse(Curve):
    """c + u cos(th) + v sin(th), th from th0 by dth: any ellipse arc under any affine map
    (u and v are conjugate semi-diameters)."""

    c: Point
    u: Point
    v: Point
    th0: float
    dth: float

    def point(self, t: float) -> Point:
        th = self.th0 + self.dth * t
        co, si = math.cos(th), math.sin(th)
        return (self.c[0] + self.u[0] * co + self.v[0] * si, self.c[1] + self.u[1] * co + self.v[1] * si)

    def deriv(self, t: float, h: float = 1e-6) -> Point:  # exact: h unused
        th = self.th0 + self.dth * t
        co, si = math.cos(th), math.sin(th)
        return ((-self.u[0] * si + self.v[0] * co) * self.dth, (-self.u[1] * si + self.v[1] * co) * self.dth)

    def map(self, f) -> "Curve":
        o = f((0.0, 0.0))
        lin = lambda w: (f(w)[0] - o[0], f(w)[1] - o[1])  # noqa: E731
        return AxisEllipse(f(self.c), lin(self.u), lin(self.v), self.th0, self.dth)


def axis_ellipse(e: "EllipseArc") -> AxisEllipse:
    cp, sp = math.cos(e.phi), math.sin(e.phi)
    return AxisEllipse(e.c, (e.rx * cp, e.rx * sp), (-e.ry * sp, e.ry * cp), e.th0, e.dth)


def reverse_curve(c: Curve) -> Curve:
    if isinstance(c, Bezier):
        return Bezier(tuple(reversed(c.pts)))
    if isinstance(c, AxisEllipse):
        return AxisEllipse(c.c, c.u, c.v, c.th0 + c.dth, -c.dth)
    if isinstance(c, EllipseArc):
        return EllipseArc(c.c, c.rx, c.ry, c.phi, c.th0 + c.dth, -c.dth)
    if isinstance(c, Nurbs):
        a, b = c.knots[0], c.knots[-1]
        return Nurbs(c.degree, tuple(a + b - k for k in reversed(c.knots)), tuple(reversed(c.ctrl)),
                     tuple(reversed(c.weights)))
    raise CurveError(f"can't reverse a {type(c).__name__}")


@dataclass(slots=True)
class Nurbs(Curve):
    """A (rational) B-spline, as DXF SPLINE stores it: degree, knots, control points, weights."""

    degree: int
    knots: tuple[float, ...]
    ctrl: tuple[Point, ...]
    weights: tuple[float, ...] = ()

    def _span(self, u: float) -> int:
        p, k = self.degree, self.knots
        n = len(self.ctrl) - 1
        if u >= k[n + 1]:
            return n
        lo, hi = p, n + 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if u < k[mid]:
                hi = mid
            else:
                lo = mid
        return lo

    def point(self, t: float) -> Point:
        p, k = self.degree, self.knots
        n = len(self.ctrl) - 1
        u = k[p] + (k[n + 1] - k[p]) * min(max(t, 0.0), 1.0)
        s = self._span(u)
        w = self.weights
        d = [(self.ctrl[j][0] * (w[j] if w else 1.0), self.ctrl[j][1] * (w[j] if w else 1.0), w[j] if w else 1.0)
             for j in range(s - p, s + 1)]
        for r in range(1, p + 1):
            for j in range(p, r - 1, -1):
                i = s - p + j
                den = k[i + p - r + 1] - k[i]
                a = 0.0 if den == 0 else (u - k[i]) / den
                d[j] = tuple((1 - a) * d[j - 1][m] + a * d[j][m] for m in range(3))  # type: ignore[assignment]
        x, y, ww = d[p]
        return (x / ww, y / ww)

    def map(self, f) -> "Curve":
        return Nurbs(self.degree, self.knots, tuple(f(q) for q in self.ctrl), self.weights)

    def breaks(self) -> list[float]:
        """Each distinct knot in the domain."""
        p, k = self.degree, self.knots
        lo, hi = k[p], k[len(self.ctrl)]
        if hi <= lo:
            return [0.0, 1.0]
        return sorted({0.0, 1.0, *((u - lo) / (hi - lo) for u in k[p:len(self.ctrl) + 1])})

    def pieces(self) -> list[Curve]:
        """Cut at each interior knot repeated degree times or more: only there can it turn a corner."""
        p, k = self.degree, self.knots
        lo, hi = k[p], k[len(self.ctrl)]
        if hi <= lo:
            return [self]
        runs = Counter(u for u in k[p + 1:len(self.ctrl)] if lo < u < hi)
        cuts = sorted(u for u, m in runs.items() if m >= p)
        if not cuts:
            return [self]
        brk = self.breaks()
        ts = [0.0, *((u - lo) / (hi - lo) for u in cuts), 1.0]
        out: list[Curve] = []
        for a, b in zip(ts, ts[1:]):
            if b <= a:
                continue
            inner = brk[bisect.bisect_right(brk, a):bisect.bisect_left(brk, b)]
            out.append(Window(self, a, b, [0.0, *((u - a) / (b - a) for u in inner), 1.0]))
        return out

    def per_span(self) -> int:
        return max(8, self.degree + 3)

    def polyline(self) -> list[Point] | None:
        """A degree-1 spline is straight between its knots (rational ones too), through these points."""
        if self.degree != 1:
            return None
        return [self.point(t) for t in self.breaks()]


def check_nurbs(degree: int, knots: list[float], ctrl: list[Point], weights: list[float]) -> None:
    if degree < 1 or degree > 10:
        raise CurveError(f"a spline of degree {degree} (1 to 10 are read)")
    if len(ctrl) < degree + 1:
        raise CurveError("a spline with fewer control points than its degree needs")
    if len(knots) != len(ctrl) + degree + 1:
        raise CurveError(f"a spline whose knot count ({len(knots)}) doesn't match its control points "
                         f"({len(ctrl)}) and degree ({degree})")
    if any(b < a for a, b in zip(knots, knots[1:])):
        raise CurveError("a spline whose knots go backwards")
    if knots[len(ctrl)] - knots[degree] <= 0:
        raise CurveError("a spline with no length in its knots")
    if weights and (len(weights) != len(ctrl) or any(w <= 0 for w in weights)):
        raise CurveError("a spline with bad weights")
    if len(ctrl) > MAX_SPLINE_POINTS:
        raise CurveError(f"a spline of more than {MAX_SPLINE_POINTS} control points; simplify it")


# ------------------------------------------------------------------ SVG arcs


@dataclass(slots=True)
class SvgArc:
    """An SVG endpoint arc in centre form, with the radius correction SVG applies."""

    curve: EllipseArc
    correction: float  # how much the radii were enlarged (0.012 = 1.2 %)


def svg_arc(p0: Point, rx: float, ry: float, rot_deg: float, large: bool, sweep: bool, p1: Point) -> SvgArc | None:
    """SVG 1.1 F.6.5: endpoint to centre form. None when the endpoints coincide (SVG draws
    nothing). Zero radii mean a straight line (the caller handles that)."""
    if math.dist(p0, p1) <= 1e-12:
        return None
    rx, ry = abs(rx), abs(ry)
    phi = math.radians(rot_deg % 360)
    cp, sp = math.cos(phi), math.sin(phi)
    dx, dy = (p0[0] - p1[0]) / 2, (p0[1] - p1[1]) / 2
    x1, y1 = cp * dx + sp * dy, -sp * dx + cp * dy
    lam = (x1 * x1) / (rx * rx) + (y1 * y1) / (ry * ry)
    correction = 0.0
    if lam > 1:
        k = math.sqrt(lam)
        rx, ry = rx * k, ry * k
        correction = k - 1
    num = rx * rx * ry * ry - rx * rx * y1 * y1 - ry * ry * x1 * x1
    den = rx * rx * y1 * y1 + ry * ry * x1 * x1
    co = math.sqrt(max(num / den, 0.0)) if den > 0 else 0.0
    if large == sweep:
        co = -co
    cxp, cyp = co * rx * y1 / ry, -co * ry * x1 / rx
    cx = cp * cxp - sp * cyp + (p0[0] + p1[0]) / 2
    cy = sp * cxp + cp * cyp + (p0[1] + p1[1]) / 2

    def ang(u: Point, v: Point) -> float:
        return math.atan2(_cross(u, v), _dot(u, v))

    th0 = ang((1.0, 0.0), ((x1 - cxp) / rx, (y1 - cyp) / ry))
    dth = ang(((x1 - cxp) / rx, (y1 - cyp) / ry), ((-x1 - cxp) / rx, (-y1 - cyp) / ry))
    if not sweep and dth > 0:
        dth -= math.tau
    elif sweep and dth < 0:
        dth += math.tau
    return SvgArc(EllipseArc((cx, cy), rx, ry, phi, th0, dth), correction)


def arc_prim(e: EllipseArc) -> Prim | None:
    """An elliptical arc that is a circle, as one exact arc (None when it isn't a circle)."""
    if abs(e.rx - e.ry) > 1e-9 * max(e.rx, e.ry) or abs(e.dth) >= math.tau - 1e-9:
        return None
    return Prim(e.point(0.0), e.point(1.0), e.c, e.dth < 0)


# ------------------------------------------------------------------ fitting


ARC_RADIUS_MAX = 1e4  # a fitted arc flatter than this is drawn as a line
ARC_SAGITTA_MIN = 1e-7


@dataclass(slots=True)
class Fit:
    prims: list[Prim] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)  # "fit" (an arc or line of a biarc) | "fallback" (a chord)
    fallback: int = 0
    max_dev: float = 0.0
    limit: int | None = None  # stop fitting past this many segments (collinear lines joined first)
    check_at: int = 0


def arc_through(p: Point, t: Point, q: Point) -> Prim:
    """The arc from p with tangent t that ends at q (a line when t points at q)."""
    w = _sub(q, p)
    n = (-t[1], t[0])
    nw = _dot(n, w)
    L2 = _dot(w, w)
    if abs(nw) <= 1e-9 * math.sqrt(L2):
        return Prim(p, q)
    r = L2 / (2 * nw)
    # Nearly straight: a line (the fit's deviation check still decides), never an arc whose centre is
    # far outside any drawing (the topology refuses coordinates over 1e5).
    sag = abs(r) - math.sqrt(max(r * r - L2 / 4, 0.0))
    if abs(r) > ARC_RADIUS_MAX or sag < ARC_SAGITTA_MIN:
        return Prim(p, q)
    c = (p[0] + n[0] * r, p[1] + n[1] * r)
    # Leave headroom for placement on the blank. The fitter still checks chord error.
    if max(abs(c[0]), abs(c[1])) > MAX_COORD_IN / 2:
        return Prim(p, q)
    return Prim(p, q, c, r < 0)


def _reverse(pr: Prim) -> Prim:
    return Prim(pr.end, pr.start, pr.center, not pr.clockwise, pr.full) if pr.center else Prim(pr.end, pr.start)


def biarc(p0: Point, t0: Point, p1: Point, t1: Point) -> list[Prim] | None:
    """Two tangent arcs from p0 (tangent t0) to p1 (tangent t1), with equal tangent lengths."""
    v = _sub(p1, p0)
    if math.hypot(*v) <= 1e-12:
        return None
    tt = (t0[0] + t1[0], t0[1] + t1[1])
    a = 2 * (_dot(t0, t1) - 1)
    b = -2 * _dot(v, tt)
    c = _dot(v, v)
    if abs(a) < 1e-12:
        if abs(b) < 1e-15:
            return None
        d = -c / b
    else:
        disc = b * b - 4 * a * c
        if disc < 0:
            return None
        roots = [(-b + math.sqrt(disc)) / (2 * a), (-b - math.sqrt(disc)) / (2 * a)]
        pos = [r for r in roots if r > 1e-12]
        if not pos:
            return None
        d = min(pos)
    if d <= 1e-12:
        return None
    q1 = (p0[0] + d * t0[0], p0[1] + d * t0[1])
    q2 = (p1[0] - d * t1[0], p1[1] - d * t1[1])
    j = ((q1[0] + q2[0]) / 2, (q1[1] + q2[1]) / 2)
    a1 = arc_through(p0, t0, j)
    a2 = _reverse(arc_through(p1, (-t1[0], -t1[1]), j))
    return [a1, a2]


def prim_point_dist(pr: Prim, p: Point) -> float:
    if pr.center is None:
        a, b = pr.start, pr.end
        d = _sub(b, a)
        L2 = _dot(d, d)
        t = 0.0 if L2 == 0 else min(max(_dot(_sub(p, a), d) / L2, 0.0), 1.0)
        return math.dist(p, (a[0] + d[0] * t, a[1] + d[1] * t))
    c = pr.center
    r = math.dist(c, pr.start)
    if pr.full:
        return abs(math.dist(p, c) - r)
    a0 = math.atan2(pr.start[1] - c[1], pr.start[0] - c[0])
    a1 = math.atan2(pr.end[1] - c[1], pr.end[0] - c[0])
    ap = math.atan2(p[1] - c[1], p[0] - c[0])
    sweep = (a1 - a0) % math.tau if not pr.clockwise else -((a0 - a1) % math.tau)
    rel = (ap - a0) % math.tau if not pr.clockwise else -((a0 - ap) % math.tau)
    inside = 0 <= rel <= sweep if sweep >= 0 else sweep <= rel <= 0
    if inside:
        return abs(math.dist(p, c) - r)
    return min(math.dist(p, pr.start), math.dist(p, pr.end))


def prim_points(pr: Prim, n: int = 8) -> list[Point]:
    if pr.center is None:
        return [(pr.start[0] + (pr.end[0] - pr.start[0]) * i / n, pr.start[1] + (pr.end[1] - pr.start[1]) * i / n)
                for i in range(n + 1)]
    c = pr.center
    r = math.dist(c, pr.start)
    a0 = math.atan2(pr.start[1] - c[1], pr.start[0] - c[0])
    if pr.full:
        sweep = -math.tau if pr.clockwise else math.tau
    else:
        a1 = math.atan2(pr.end[1] - c[1], pr.end[0] - c[0])
        sweep = (a1 - a0) % math.tau if not pr.clockwise else -((a0 - a1) % math.tau)
    return [(c[0] + r * math.cos(a0 + sweep * i / n), c[1] + r * math.sin(a0 + sweep * i / n)) for i in range(n + 1)]


def _deviation(curve: Curve, ta: float, tb: float, prims: list[Prim], extra: list[float] = ()) -> float:
    """Two-way: curve samples to the arcs, and arc samples to a dense polyline of the curve. `extra`
    are more samples (each span of a spline in range, which even spacing can step over)."""
    worst = 0.0
    m = 24
    for t in [ta + (tb - ta) * i / m for i in range(1, m)] + list(extra):
        p = curve.point(t)
        worst = max(worst, min(prim_point_dist(pr, p) for pr in prims))
    dense = [curve.point(t) for t in sorted({*(ta + (tb - ta) * i / 96 for i in range(97)), *extra})]
    chords = [Prim(a, b) for a, b in zip(dense, dense[1:]) if math.dist(a, b) > 0]
    if chords:
        for pr in prims:
            for p in prim_points(pr, 8)[1:-1]:
                worst = max(worst, min(prim_point_dist(c, p) for c in chords))
    return worst


class TooManySegments(CurveError):
    """Fitting went past the segment budget it was given."""


def fit(curve: Curve, tol: float = FIT_TOL_IN, limit: int | None = None) -> Fit:
    """Biarcs within tol of the curve; chords only where arcs can't fit. A spline is fitted piece by
    piece between the knots where it may turn a corner, and straight runs are joined into one line.
    `limit` stops early (TooManySegments) once past that many segments, collinear lines joined."""
    out = Fit()
    out.limit = None if limit is None else max(limit, 0)
    out.check_at = 0 if limit is None else out.limit
    start = curve.point(0.0)
    pts = curve.polyline()
    if pts is not None:  # exact: its own straights
        for a, b in zip(pts, pts[1:]):
            if math.dist(a, b) > 1e-12:
                out.prims.append(Prim(a, b))
                out.kinds.append("fit")
    else:
        for piece in curve.pieces():
            _fit_piece(piece, tol, out)
    _join_lines(out)
    # Chain exactly: each piece starts where the last ended.
    fixed: list[Prim] = []
    cur = start
    for pr in out.prims:
        fixed.append(Prim(cur, pr.end, pr.center, pr.clockwise, pr.full))
        cur = pr.end
    out.prims = fixed
    return out


def _join_lines(out: Fit) -> None:
    """Consecutive lines along one straight become one (a polyline spline of many collinear points)."""
    prims: list[Prim] = []
    kinds: list[str] = []
    for pr, kind in zip(out.prims, out.kinds, strict=True):
        if prims and pr.center is None and prims[-1].center is None and _straight_on(prims[-1], pr):
            prims[-1] = Prim(prims[-1].start, pr.end)
            kinds[-1] = "fallback" if "fallback" in (kind, kinds[-1]) else kinds[-1]
            continue
        prims.append(pr)
        kinds.append(kind)
    out.prims, out.kinds = prims, kinds
    out.fallback = kinds.count("fallback")


def _straight_on(a: Prim, b: Prim) -> bool:
    """b carries on a's line, the same way, from where a ends."""
    if math.dist(a.end, b.start) > 1e-9:
        return False
    u, v = _sub(a.end, a.start), _sub(b.end, b.start)
    la, lb = math.hypot(*u), math.hypot(*v)
    if la <= 1e-12 or lb <= 1e-12 or _dot(u, v) <= 0:
        return False
    return abs(_cross(u, _sub(b.end, a.start))) / la <= 1e-9


def _fit_piece(curve: Curve, tol: float, out: Fit) -> None:
    brk = curve.breaks()
    # Probes inside a range besides even steps: through every span, as densely as its degree needs.
    per = curve.per_span()
    marks = sorted({a + (b - a) * j / per for a, b in zip(brk, brk[1:]) for j in range(per)}) if len(brk) > 2 else []

    def probes(ta: float, tb: float) -> list[float]:
        if not marks:
            return []
        return marks[bisect.bisect_right(marks, ta):bisect.bisect_left(marks, tb)]

    def excursion(ta: float, tb: float) -> float:
        """How far the curve goes from where this piece starts (a loop can start and end at one point)."""
        a = curve.point(ta)
        ts = [ta + (tb - ta) * i / 16 for i in range(1, 17)] + probes(ta, tb)
        return max(math.dist(a, curve.point(t)) for t in ts)

    def chords(ta: float, tb: float, depth: int = 0) -> None:
        a, b = curve.point(ta), curve.point(tb)
        seg = Prim(a, b)
        dev = max(prim_point_dist(seg, curve.point(t))
                  for t in [ta + (tb - ta) * i / 8 for i in range(1, 8)] + probes(ta, tb))
        span = math.dist(a, b)
        if span <= 1e-12 and dev <= 1e-12:
            return
        if dev <= tol and span > 1e-12:
            out.prims.append(seg)
            out.kinds.append("fallback")
            out.fallback += 1
            out.max_dev = max(out.max_dev, dev)
            return
        if excursion(ta, tb) <= 1e-12:
            return
        if depth >= 20:
            raise CurveError("could not fit the artwork curve within tolerance")
        chords(ta, (ta + tb) / 2, depth + 1)
        chords((ta + tb) / 2, tb, depth + 1)

    def go(ta: float, tb: float, depth: int) -> None:
        if out.limit is not None and len(out.prims) > out.check_at:
            _join_lines(out)
            if len(out.prims) > out.limit:
                raise TooManySegments("too many segments after fitting")
            out.check_at = max(out.limit, 2 * len(out.prims) + 64)
        p0, p1 = curve.point(ta), curve.point(tb)
        if math.dist(p0, p1) <= 1e-9:
            # A closed curve, or a loop of one that comes back to where it started: split it. Only a
            # piece that never leaves its point is nothing.
            if excursion(ta, tb) <= 1e-9:
                return
            if depth >= MAX_DEPTH:
                chords(ta, tb)
                return
            go(ta, (ta + tb) / 2, depth + 1)
            go((ta + tb) / 2, tb, depth + 1)
            return
        t0, t1 = curve.tangent(ta, True), curve.tangent(tb, False)
        arcs = biarc(p0, t0, p1, t1) if t0 is not None and t1 is not None else None
        if arcs is not None:
            dev = _deviation(curve, ta, tb, arcs, probes(ta, tb))
            if dev <= tol:
                for pr in arcs:
                    if math.dist(pr.start, pr.end) > 1e-12:
                        out.prims.append(pr)
                        out.kinds.append("fit")
                out.max_dev = max(out.max_dev, dev)
                return
        if depth >= MAX_DEPTH or math.dist(p0, p1) < tol:
            chords(ta, tb)
            return
        go(ta, (ta + tb) / 2, depth + 1)
        go((ta + tb) / 2, tb, depth + 1)

    go(0.0, 1.0, 0)

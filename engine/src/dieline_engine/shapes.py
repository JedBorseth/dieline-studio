"""Library shapes in their own local frame: lines and true arcs around a datum
at (0, 0), +x right and +y up as seen from outside the folded box.

Placement (placement.py) puts a shape on a face; nothing here knows about
blanks or faces. Every outline is made of lines and circular arcs only, so
dies and the sample table get smooth, bendable curves.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .geometry import Arc, Line, LineType, Point, _round_pt, _segment_extent_points

TAU = math.tau


@dataclass(frozen=True, slots=True)
class Prim:
    """A line (center None) or an arc in a local frame."""

    start: Point
    end: Point
    center: Point | None = None
    clockwise: bool = False
    full: bool = False

    @property
    def is_arc(self) -> bool:
        return self.center is not None

    def map(self, f, reflects: bool) -> "Prim":
        if self.center is None:
            return Prim(f(self.start), f(self.end))
        return Prim(f(self.start), f(self.end), f(self.center), self.clockwise != reflects, self.full)

    def segment(self, type: LineType, op: str | None) -> Line | Arc:
        """As drawn: rounded to the model's precision, like every structure segment."""
        if self.center is None:
            return Line(_round_pt(self.start), _round_pt(self.end), type, op)
        start = _round_pt(self.start)
        return Arc(start, start if self.full else _round_pt(self.end), _round_pt(self.center), self.clockwise, type,
                   self.full, op)


def bbox(prims: list[Prim]) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for p in prims:
        for x, y in _segment_extent_points(p.segment(LineType.CUT, None)):
            xs.append(x)
            ys.append(y)
    return (min(xs), min(ys), max(xs), max(ys))


def transform(prims: list[Prim], *, rotation_deg: float = 0.0, flip: bool = False,
              offset: Point = (0.0, 0.0)) -> list[Prim]:
    """Flip (x -> -x) first, then rotate about the datum, then translate."""
    r = math.radians(rotation_deg)
    c, s = math.cos(r), math.sin(r)
    ox, oy = offset

    def f(p: Point) -> Point:
        x, y = (-p[0] if flip else p[0]), p[1]
        return (ox + x * c - y * s, oy + x * s + y * c)

    return [p.map(f, flip) for p in prims]


def _arc(c: Point, r: float, a0: float, a1: float, cw: bool = False) -> Prim:
    s = (c[0] + r * math.cos(a0), c[1] + r * math.sin(a0))
    e = (c[0] + r * math.cos(a1), c[1] + r * math.sin(a1))
    return Prim(s, e, c, cw)


def _line(a: Point, b: Point) -> list[Prim]:
    return [Prim(a, b)] if math.dist(a, b) > 1e-9 else []


def circle(d: float) -> list[Prim]:
    r = d / 2
    return [Prim((r, 0.0), (r, 0.0), (0.0, 0.0), False, True)]


def rounded_rect(w: float, h: float, r: float = 0.0) -> list[Prim]:
    """A w x h rectangle centred on the datum, corners of radius r (0: square).
    r = min(w, h)/2 is an obround; w = h = 2r a circle."""
    r = max(0.0, min(r, w / 2, h / 2))
    if r >= w / 2 - 1e-12 and r >= h / 2 - 1e-12:
        return circle(w)
    x, y = w / 2, h / 2
    out: list[Prim] = []
    # Counter-clockwise from the bottom edge.
    out += _line((-x + r, -y), (x - r, -y))
    if r:
        out.append(_arc((x - r, -y + r), r, -math.pi / 2, 0.0))
    out += _line((x, -y + r), (x, y - r))
    if r:
        out.append(_arc((x - r, y - r), r, 0.0, math.pi / 2))
    out += _line((x - r, y), (-x + r, y))
    if r:
        out.append(_arc((-x + r, y - r), r, math.pi / 2, math.pi))
    out += _line((-x, y - r), (-x, -y + r))
    if r:
        out.append(_arc((-x + r, -y + r), r, math.pi, 1.5 * math.pi))
    return out


def obround(w: float, h: float) -> list[Prim]:
    """Full-radius ends: the shop's File_Box_1 handhole is obround(3 5/8, 1 3/8)."""
    return rounded_rect(w, h, min(w, h) / 2)


def d_shape(w: float, h: float, r: float | None = None, flat: str = "top") -> list[Prim]:
    """A straight side (`flat`) and the two corners opposite it rounded with radius r
    (default: as round as the size allows)."""
    along, perp = (w, h) if flat in ("top", "bottom") else (h, w)
    r = min(along / 2, perp) if r is None else max(0.0, min(r, along / 2, perp))
    x, y = along / 2, perp / 2
    # Canonical: flat side on top, rounded corners at the bottom.
    out: list[Prim] = []
    out += _line((-x + r, -y), (x - r, -y))
    if r:
        out.append(_arc((x - r, -y + r), r, -math.pi / 2, 0.0))
    out += _line((x, -y + r), (x, y))
    out += _line((x, y), (-x, y))
    out += _line((-x, y), (-x, -y + r))
    if r:
        out.append(_arc((-x + r, -y + r), r, math.pi, 1.5 * math.pi))
    return transform(out, rotation_deg=_rot_for_side(flat))


def _rot_for_side(side: str) -> float:
    """The rotation that takes a canonical top side to `side`."""
    return {"top": 0.0, "left": 90.0, "bottom": 180.0, "right": -90.0}[side]


def oval(w: float, h: float) -> tuple[list[Prim], float]:
    """A four-centre oval through the ellipse's four vertices, tangent-continuous,
    and its largest radial gap from the true ellipse (sampled)."""
    a, b = w / 2, h / 2
    if abs(a - b) <= 1e-12:
        return circle(w), 0.0
    swap = b > a
    if swap:
        a, b = b, a
    L = math.hypot(a, b)
    ux, uy = a / L, -b / L  # from (0, b) toward (a, 0)
    e = (ux * (a - b), b + uy * (a - b))  # (0, b) moved a - b along it
    m = ((a + e[0]) / 2, e[1] / 2)  # midpoint of (a, 0) and e
    nx, ny = b / L, a / L  # perpendicular
    x1 = m[0] - m[1] * nx / ny  # where the bisector meets y = 0
    y2 = m[1] - m[0] * ny / nx  # and x = 0 (below the centre)
    r1, r2 = a - x1, b - y2
    d = math.hypot(x1, -y2)
    j = (x1 * r2 / d, y2 + (-y2) * r2 / d)  # the top-right junction
    tr, tl, bl, br = j, (-j[0], j[1]), (-j[0], -j[1]), (j[0], -j[1])
    prims = [
        Prim(br, tr, (x1, 0.0)),
        Prim(tr, tl, (0.0, y2)),
        Prim(tl, bl, (-x1, 0.0)),
        Prim(bl, br, (0.0, -y2)),
    ]
    gap = 0.0
    for k in range(181):
        t = math.pi / 2 * k / 180
        px, py = a * math.cos(t), b * math.sin(t)
        ang = math.atan2(py, px)
        jang = math.atan2(j[1], j[0])
        if ang <= jang:
            dist = abs(math.hypot(px - x1, py) - r1)
        else:
            dist = abs(math.hypot(px, py - y2) - r2)
        gap = max(gap, dist)
    if swap:
        prims = transform(prims, rotation_deg=90.0)
    return prims, gap


# ------------------------------------------------------------------ hand flaps

MIN_JOIN_DEG = 35.0  # provisional: the cut must meet a hinge at a real angle


@dataclass(frozen=True, slots=True)
class FlapProfile:
    """A hand flap in its local frame: the cut on its free sides, the hinge, and
    the opening it leaves (for clearance and web checks)."""

    cut: list[Prim]  # one open path, from one hinge end to the other
    hinge: tuple[Point, Point]
    opening: list[Prim]  # the full hole outline the flap swings out of
    join_deg: float  # the angle between the cut and the hinge at each end
    inset: float = 0.0  # how far a secant hinge sits inside the opening's side


def flap_profile(profile: str, along: float, perp: float, *, hinge: str, radius: float | None = None,
                 taper_deg: float = 0.0, min_join_deg: float = MIN_JOIN_DEG) -> FlapProfile:
    """A hand flap hinged on `hinge` (top | bottom | left | right of its own frame).

    `along` is the size along the hinge, `perp` the size across it. Built with
    the hinge on top, then turned so it lands on the named side."""
    A, P = along, perp
    hy = P / 2
    if profile in ("rect", "d_shape"):
        # A D-shape's default corners leave a straight start on each side, so the cut leaves the hinge
        # (and a relief nick carries on from it) in a straight line.
        r = 0.0 if profile == "rect" and radius is None else (min(A / 2, P) / 2 if radius is None else radius)
        r = max(0.0, min(r, A / 2, P))
        cut: list[Prim] = []
        cut += _line((-A / 2, hy), (-A / 2, -hy + r))
        if r:
            cut.append(_arc((-A / 2 + r, -hy + r), r, math.pi, 1.5 * math.pi))
        cut += _line((-A / 2 + r, -hy), (A / 2 - r, -hy))
        if r:
            cut.append(_arc((A / 2 - r, -hy + r), r, -math.pi / 2, 0.0))
        cut += _line((A / 2, -hy + r), (A / 2, hy))
        opening = cut + [Prim((A / 2, hy), (-A / 2, hy))]
        fp = FlapProfile(cut, ((-A / 2, hy), (A / 2, hy)), opening, 90.0)
    elif profile in ("obround", "circle"):
        if profile == "obround" and A < P - 1e-9:
            raise ValueError("an obround hand flap hinges on a long side: its hinge side must be the longer one "
                             "(top or bottom for a horizontal obround, left or right for a vertical one)")
        r = P / 2 if profile == "obround" else A / 2
        if profile == "circle":
            P = A
            hy = r
        straight = A / 2 - r if profile == "obround" else 0.0
        # Secant hinge: a chord inset from the side, crossing the end arcs at the minimum angle or more.
        inset = r * (1 - math.cos(math.radians(min_join_deg)))
        inset = math.ceil(inset * 32 - 1e-9) / 32  # to the next 1/32
        if inset >= r:
            raise ValueError("the opening is too small for a secant hinge")
        c = r - inset
        phi = math.asin(c / r)
        xe = straight + math.sqrt(r * r - c * c)
        cut = []
        if straight > 1e-12:
            cut.append(_arc((-straight, 0.0), r, math.pi - phi, 1.5 * math.pi))
            cut += _line((-straight, -r), (straight, -r))
            cut.append(_arc((straight, 0.0), r, -math.pi / 2, phi))
            opening = obround(A, P)
        else:
            cut.append(_arc((0.0, 0.0), r, math.pi - phi, phi + TAU))
            opening = circle(A)
        cut = [Prim(cut[0].start, cut[0].end, cut[0].center)] + cut[1:]
        fp = FlapProfile(cut, ((-xe, c), (xe, c)), opening, 90.0 - math.degrees(phi), inset)
    elif profile == "angled":
        if taper_deg < 0:
            raise ValueError("an angled hand flap narrows away from its hinge; a negative taper (a dovetail) "
                             "can't swing into the opening")
        if taper_deg >= 90 - min_join_deg + 1e-9:
            raise ValueError(f"a taper of {taper_deg:g} deg meets the hinge at under {min_join_deg:g} deg; "
                             f"use {90 - min_join_deg:g} deg or less")
        inset_x = P * math.tan(math.radians(taper_deg))
        if A - 2 * inset_x <= 1e-6:
            raise ValueError("the taper closes the flap before its free edge; reduce the taper or the depth")
        cut = [Prim((-A / 2, hy), (-A / 2 + inset_x, -hy)), Prim((-A / 2 + inset_x, -hy), (A / 2 - inset_x, -hy)),
               Prim((A / 2 - inset_x, -hy), (A / 2, hy))]
        opening = cut + [Prim((A / 2, hy), (-A / 2, hy))]
        fp = FlapProfile(cut, ((-A / 2, hy), (A / 2, hy)), opening, 90.0 - taper_deg)
    else:
        raise ValueError(f"unknown hand flap profile {profile!r}; profiles: obround, d_shape, rect, circle, angled")
    # The hinge runs exactly between the cut's two ends (no float daylight at the junction).
    fp = FlapProfile(fp.cut, (fp.cut[0].start, fp.cut[-1].end), fp.opening, fp.join_deg, fp.inset)
    if fp.join_deg < min_join_deg - 1e-6:
        raise ValueError(f"the cut meets the hinge at {fp.join_deg:.1f} deg, under {min_join_deg:g} deg")
    rot = _rot_for_side(hinge)
    if rot:
        a, b = transform([Prim(*fp.hinge)], rotation_deg=rot)[0].start, transform([Prim(*fp.hinge)],
                                                                                   rotation_deg=rot)[0].end
        fp = FlapProfile(transform(fp.cut, rotation_deg=rot), (a, b), transform(fp.opening, rotation_deg=rot),
                         fp.join_deg, fp.inset)
    return fp


# ------------------------------------------------------------------ notches


def notch(kind: str, width: float, depth: float | None, radius: float | None = None) -> list[Prim]:
    """A notch profile in edge coordinates: x along the edge from the notch's
    centre, y into the face (depth > 0). The path runs from (-width/2, 0) to
    (width/2, 0)."""
    w = width / 2
    if kind == "half_circle":
        return [Prim((-w, 0.0), (w, 0.0), (0.0, 0.0), True)]  # bulging into the face (+y)
    if depth is None or depth <= 0:
        raise ValueError(f"a {kind} notch needs a depth above zero")
    if kind == "v":
        return [Prim((-w, 0.0), (0.0, depth)), Prim((0.0, depth), (w, 0.0))]
    if kind == "rounded_rect":
        r = min(w, depth) if radius is None else max(0.0, min(radius, w, depth))
        out: list[Prim] = []
        out += _line((-w, 0.0), (-w, depth - r))
        if r:
            out.append(Prim((-w, depth - r), (-w + r, depth), (-w + r, depth - r), True))
        out += _line((-w + r, depth), (w - r, depth))
        if r:
            out.append(Prim((w - r, depth), (w, depth - r), (w - r, depth - r), True))
        out += _line((w, depth - r), (w, 0.0))
        return out
    raise ValueError(f"unknown notch {kind!r}; notches: half_circle, v, rounded_rect")

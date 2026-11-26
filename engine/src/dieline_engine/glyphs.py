"""Dieline Studio's own lettering: one geometric skeleton font, drawn two ways
(docs/DESIGN-SYSTEM.md, "Artwork / Sources").

- **Stencil text** (the default for cut lettering): every skeleton stroke is
  thickened into an outline with butt ends and mitred (or, under 90 deg,
  bevelled) corners. Wherever two strokes meet, their ends are cut back so a
  **stencil break** of board at least `gap` wide stays between them. Every
  counter (the middle of an O, an A, a B) therefore stays joined to the rest of
  the board: stencil text never leaves an island.
- **Stroke text**: the skeleton itself, as single lines, for crease or
  annotation only (cutting single lines would make hairline slits).

The glyphs are original straight-line designs made for this app (no third-party
font). Cap height is 6 units; the skeleton runs on stroke centre lines inset
half a unit, so a glyph's stencil outline fills its box [0, w] x [0, 6].
Uppercase A-Z, digits 0-9 and . , - ! ? / ' : + ( ) are drawn; lowercase is
drawn as uppercase.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache

Point = tuple[float, float]
CAP = 6.0  # units
STROKE = 1.0  # units
GAP_UNITS = 0.6  # the stencil break, in units, before any minimum
SPACE = 2.5  # the advance of a space, in units
LINE = 9.0  # line pitch, in units

# width, polylines (centre lines, y up).
GLYPHS: dict[str, tuple[float, tuple[tuple[Point, ...], ...]]] = {
    "A": (6, (((0.5, 0.5), (3.0, 5.5), (5.5, 0.5)), ((1.2, 1.9), (4.8, 1.9)))),
    "B": (4, (((0.5, 0.5), (0.5, 5.5)), ((0.5, 5.5), (2.0, 5.5), (2.8, 4.7), (2.8, 3.0)),
              ((0.5, 3.0), (2.9, 3.0), (3.5, 2.4), (3.5, 1.1), (2.9, 0.5), (0.5, 0.5)))),
    "C": (4.5, (((4.0, 4.5), (3.0, 5.5), (1.5, 5.5), (0.5, 4.5), (0.5, 1.5), (1.5, 0.5), (3.0, 0.5), (4.0, 1.5)),)),
    "D": (4, (((0.5, 0.5), (0.5, 5.5)), ((0.5, 5.5), (2.0, 5.5), (3.5, 4.0), (3.5, 2.0), (2.0, 0.5), (0.5, 0.5)))),
    "E": (4, (((3.5, 5.5), (0.5, 5.5), (0.5, 0.5), (3.5, 0.5)), ((0.5, 3.0), (2.8, 3.0)))),
    "F": (4, (((3.5, 5.5), (0.5, 5.5), (0.5, 0.5)), ((0.5, 3.0), (2.8, 3.0)))),
    "G": (4.5, (((4.0, 4.5), (3.0, 5.5), (1.5, 5.5), (0.5, 4.5), (0.5, 1.5), (1.5, 0.5), (3.0, 0.5), (4.0, 1.5),
                 (4.0, 2.8), (2.5, 2.8)),)),
    "H": (4.5, (((0.5, 0.5), (0.5, 5.5)), ((4.0, 0.5), (4.0, 5.5)), ((0.5, 3.0), (4.0, 3.0)))),
    "I": (1, (((0.5, 0.5), (0.5, 5.5)),)),
    "J": (4, (((3.5, 5.5), (3.5, 1.5), (2.5, 0.5), (1.5, 0.5), (0.5, 1.5), (0.5, 2.3)),)),
    "K": (4, (((0.5, 0.5), (0.5, 5.5)), ((3.5, 5.5), (0.5, 2.5)), ((2.3, 4.3), (3.6, 0.5)))),
    "L": (4, (((0.5, 5.5), (0.5, 0.5), (3.5, 0.5)),)),
    "M": (5, (((0.5, 0.5), (0.5, 5.5), (2.5, 3.0), (4.5, 5.5), (4.5, 0.5)),)),
    "N": (5, (((0.5, 0.5), (0.5, 5.5), (4.5, 0.5), (4.5, 5.5)),)),
    "O": (5, (((2.5, 5.5), (1.5, 5.5), (0.5, 4.5), (0.5, 1.5), (1.5, 0.5), (2.5, 0.5)),
              ((2.5, 0.5), (3.5, 0.5), (4.5, 1.5), (4.5, 4.5), (3.5, 5.5), (2.5, 5.5)))),
    "P": (4, (((0.5, 0.5), (0.5, 5.5)), ((0.5, 5.5), (2.5, 5.5), (3.5, 4.5), (3.5, 3.5), (2.5, 2.5), (0.5, 2.5)))),
    "Q": (5.5, (((2.5, 5.5), (1.5, 5.5), (0.5, 4.5), (0.5, 1.5), (1.5, 0.5), (2.5, 0.5)),
                ((2.5, 0.5), (3.5, 0.5), (4.5, 1.5), (4.5, 4.5), (3.5, 5.5), (2.5, 5.5)),
                ((4.0, 1.0), (5.2, -0.2)))),
    "R": (4, (((0.5, 0.5), (0.5, 5.5)), ((0.5, 5.5), (2.5, 5.5), (3.5, 4.5), (3.5, 3.5), (2.5, 2.5), (0.5, 2.5)),
              ((2.5, 2.5), (3.5, 0.5)))),
    "S": (4, (((3.5, 4.5), (2.5, 5.5), (1.5, 5.5), (0.5, 4.5), (0.5, 3.8), (3.5, 2.2), (3.5, 1.5), (2.5, 0.5),
               (1.5, 0.5), (0.5, 1.5)),)),
    "T": (4, (((0.5, 5.5), (3.5, 5.5)), ((2.0, 5.5), (2.0, 0.5)))),
    "U": (4, (((0.5, 5.5), (0.5, 1.5), (1.5, 0.5), (2.5, 0.5), (3.5, 1.5), (3.5, 5.5)),)),
    "V": (5, (((0.5, 5.5), (2.5, 0.5), (4.5, 5.5)),)),
    "W": (7, (((0.5, 5.5), (2.0, 0.5), (3.5, 4.0), (5.0, 0.5), (6.5, 5.5)),)),
    "X": (4, (((0.5, 0.5), (3.5, 5.5)), ((0.5, 5.5), (2.0, 3.0)), ((2.0, 3.0), (3.5, 0.5)))),
    "Y": (4, (((0.5, 5.5), (2.0, 3.0)), ((3.5, 5.5), (2.0, 3.0), (2.0, 0.5)))),
    "Z": (4, (((0.5, 5.5), (3.5, 5.5), (0.5, 0.5), (3.5, 0.5)),)),
    "0": (4.5, (((2.25, 5.5), (1.5, 5.5), (0.5, 4.5), (0.5, 1.5), (1.5, 0.5), (2.25, 0.5)),
                ((2.25, 0.5), (3.0, 0.5), (4.0, 1.5), (4.0, 4.5), (3.0, 5.5), (2.25, 5.5)))),
    "1": (3, (((0.5, 4.3), (2.0, 5.5), (2.0, 0.5)),)),
    "2": (4, (((0.5, 4.5), (1.5, 5.5), (2.5, 5.5), (3.5, 4.5), (3.5, 3.6), (0.5, 0.5), (3.5, 0.5)),)),
    "3": (4, (((0.5, 4.5), (1.5, 5.5), (2.5, 5.5), (3.5, 4.5), (3.5, 3.8), (2.7, 3.0), (3.5, 2.2), (3.5, 1.5),
               (2.5, 0.5), (1.5, 0.5), (0.5, 1.5)),)),
    "4": (4, (((1.0, 5.5), (0.5, 2.0), (3.0, 2.0)), ((3.0, 0.5), (3.0, 5.5)))),
    "5": (4, (((3.5, 5.5), (0.5, 5.5), (0.5, 3.3), (2.5, 3.3), (3.5, 2.3), (3.5, 1.5), (2.5, 0.5), (1.5, 0.5),
               (0.5, 1.5)),)),
    "6": (4, (((3.5, 4.5), (2.5, 5.5), (1.5, 5.5), (0.5, 4.5), (0.5, 1.5), (1.5, 0.5), (2.5, 0.5), (3.5, 1.5),
               (3.5, 2.3), (2.5, 3.3), (1.7, 3.3)),)),
    "7": (4, (((0.5, 5.5), (3.5, 5.5), (1.5, 0.5)),)),
    "8": (4, (((0.5, 3.0), (0.5, 4.5), (1.5, 5.5), (2.5, 5.5), (3.5, 4.5), (3.5, 3.0)),
              ((0.5, 3.0), (0.5, 1.5), (1.5, 0.5), (2.5, 0.5), (3.5, 1.5), (3.5, 3.0)),
              ((0.5, 3.0), (3.5, 3.0)))),
    "9": (4, (((0.5, 1.5), (1.5, 0.5), (2.5, 0.5), (3.5, 1.5), (3.5, 4.5), (2.5, 5.5), (1.5, 5.5), (0.5, 4.5),
               (0.5, 3.7), (1.5, 2.7), (2.3, 2.7)),)),
    ".": (1, (((0.5, 0.5), (0.5, 1.2)),)),
    ",": (1.2, (((0.7, 1.2), (0.7, 0.4), (0.3, -0.4)),)),
    "-": (3, (((0.5, 3.0), (2.5, 3.0)),)),
    "!": (1, (((0.5, 2.9), (0.5, 5.5)), ((0.5, 0.5), (0.5, 0.9)))),
    "?": (4, (((0.5, 4.5), (1.5, 5.5), (2.5, 5.5), (3.5, 4.5), (3.5, 3.9), (2.0, 3.0), (2.0, 2.9)),
              ((2.0, 0.5), (2.0, 0.9)))),
    "/": (4, (((0.5, 0.5), (3.5, 5.5)),)),
    "'": (1, (((0.5, 5.5), (0.5, 4.3)),)),
    ":": (1, (((0.5, 0.5), (0.5, 0.9)), ((0.5, 3.3), (0.5, 3.7)))),
    "+": (5, (((0.5, 3.0), (4.5, 3.0)), ((2.5, 0.5), (2.5, 3.0)), ((2.5, 3.0), (2.5, 5.5)))),
    "(": (2, (((1.5, 6.0), (0.5, 4.5), (0.5, 1.5), (1.5, 0.0)),)),
    ")": (2, (((0.5, 6.0), (1.5, 4.5), (1.5, 1.5), (0.5, 0.0)),)),
}
CHARS = "".join(sorted(GLYPHS)) + " "


class GlyphError(ValueError):
    pass


def check_text(text: str) -> str:
    """The text as it will be drawn (lowercase as uppercase); refuses characters the font doesn't have."""
    if not isinstance(text, str) or not text.strip():
        raise GlyphError("text must be a non-empty string")
    if len(text) > 80:
        raise GlyphError("text is limited to 80 characters")
    up = text.upper()
    bad = sorted({c for c in up if c not in GLYPHS and c not in " \n"})
    if bad:
        raise GlyphError(f"the lettering font has no {', '.join(repr(c) for c in bad)}; it has A-Z, 0-9 and "
                         ". , - ! ? / ' : + ( )")
    if up.count("\n") > 5:
        raise GlyphError("text is limited to 6 lines")
    return up


# ------------------------------------------------------------------ geometry helpers


def _sub(a: Point, b: Point) -> Point:
    return (a[0] - b[0], a[1] - b[1])


def _add(a: Point, b: Point, k: float = 1.0) -> Point:
    return (a[0] + b[0] * k, a[1] + b[1] * k)


def _unit(v: Point) -> Point:
    n = math.hypot(*v)
    return (v[0] / n, v[1] / n)


def _cross(a: Point, b: Point) -> float:
    return a[0] * b[1] - a[1] * b[0]


def _line_x(p: Point, d: Point, q: Point, e: Point) -> Point:
    """Intersection of p + s d and q + t e."""
    den = _cross(d, e)
    s = _cross(_sub(q, p), e) / den
    return (p[0] + d[0] * s, p[1] + d[1] * s)


def _seg_dist(a: Point, b: Point, c: Point, d: Point) -> float:
    def pt(p: Point, s: Point, e: Point) -> float:
        v = _sub(e, s)
        L2 = v[0] ** 2 + v[1] ** 2
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - s[0]) * v[0] + (p[1] - s[1]) * v[1]) / L2))
        return math.dist(p, (s[0] + v[0] * t, s[1] + v[1] * t))

    r, s = _sub(b, a), _sub(d, c)
    den = _cross(r, s)
    if abs(den) > 1e-15:
        t = _cross(_sub(c, a), s) / den
        u = _cross(_sub(c, a), r) / den
        if 0 <= t <= 1 and 0 <= u <= 1:
            return 0.0
    return min(pt(a, c, d), pt(b, c, d), pt(c, a, b), pt(d, a, b))


def _inside_convex(p: Point, poly: list[Point]) -> bool:
    sign = 0
    for a, b in zip(poly, poly[1:] + poly[:1]):
        c = _cross(_sub(b, a), _sub(p, a))
        if abs(c) < 1e-12:
            continue
        s = 1 if c > 0 else -1
        if sign == 0:
            sign = s
        elif s != sign:
            return False
    return True


def poly_dist(p: list[Point], q: list[Point]) -> float:
    """Distance between two convex polygons (0 when they overlap)."""
    if any(_inside_convex(v, q) for v in p) or any(_inside_convex(v, p) for v in q):
        return 0.0
    return min(_seg_dist(a, b, c, d) for a, b in zip(p, p[1:] + p[:1]) for c, d in zip(q, q[1:] + q[:1]))


# ------------------------------------------------------------------ thick strokes


@dataclass(slots=True)
class Stroke:
    pts: list[Point]  # the centre line, trimmed
    caps: tuple[Point | None, Point | None] = (None, None)  # an end cut along this direction (None: square)

    def _dirs(self) -> tuple[list[Point], list[Point]]:
        ds = [_unit(_sub(b, a)) for a, b in zip(self.pts, self.pts[1:])]
        return ds, [(-d[1], d[0]) for d in ds]

    def _end(self, which: int, h: float) -> tuple[Point, Point]:
        """The (left, right) corners of one end."""
        ds, ns = self._dirs()
        k = 0 if which == 0 else -1
        p, d, n = self.pts[k], ds[k], ns[k]
        a, b = _add(p, n, h), _add(p, n, -h)
        c = self.caps[which]
        if c is not None and abs(_cross(c, d)) > 1e-9:
            a, b = _line_x(a, d, p, c), _line_x(b, d, p, c)
        return a, b

    def outline(self, h: float) -> list[Point]:
        """The closed outline: square or angled ends, mitred corners (bevelled under 90 deg)."""
        pts = self.pts
        ds, ns = self._dirs()
        l0, r0 = self._end(0, h)
        left: list[Point] = [l0]
        right: list[Point] = [r0]
        for k in range(1, len(pts) - 1):
            d0, d1, n0, n1, p = ds[k - 1], ds[k], ns[k - 1], ns[k], pts[k]
            turn = _cross(d0, d1)
            cosang = -(d0[0] * d1[0] + d0[1] * d1[1])  # cos of the corner angle
            for side, out in ((1.0, left), (-1.0, right)):
                outer = (turn > 0 and side < 0) or (turn < 0 and side > 0)
                a = _add(p, n0, side * h)
                b = _add(p, n1, side * h)
                if abs(turn) < 1e-12:
                    out.append(a)
                elif outer and cosang > 1e-9:  # a corner under 90 deg: bevel
                    out += [a, b]
                else:
                    out.append(_line_x(a, d0, b, d1))
        l1, r1 = self._end(1, h)
        left.append(l1)
        right.append(r1)
        return left + right[::-1]

    def rects(self, h: float) -> list[list[Point]]:
        """A convex quad per segment (its ends cut as the stroke's are)."""
        ds, ns = self._dirs()
        out = []
        last = len(ds) - 1
        for i, ((a, b), n) in enumerate(zip(zip(self.pts, self.pts[1:]), ns)):
            la, ra = (_add(a, n, h), _add(a, n, -h)) if i else self._end(0, h)
            lb, rb = (_add(b, n, h), _add(b, n, -h)) if i < last else self._end(1, h)
            out.append([la, lb, rb, ra])
        return out

    def wedges(self, h: float) -> dict[int, list[Point]]:
        """The outer corner piece at each bent vertex, by vertex index."""
        ds, ns = self._dirs()
        out: dict[int, list[Point]] = {}
        for k in range(1, len(self.pts) - 1):
            d0, d1, n0, n1, p = ds[k - 1], ds[k], ns[k - 1], ns[k], self.pts[k]
            turn = _cross(d0, d1)
            if abs(turn) < 1e-12:
                continue
            side = -1.0 if turn > 0 else 1.0
            a, b = _add(p, n0, side * h), _add(p, n1, side * h)
            cosang = -(d0[0] * d1[0] + d0[1] * d1[1])
            poly = [p, a, b] if cosang > 1e-9 else [p, a, _line_x(a, d0, b, d1), b]
            out[k] = poly
        return out

    def parts(self, h: float) -> list[list[Point]]:
        return self.rects(h) + list(self.wedges(h).values())

    def near(self, h: float, p: Point, end: int | None) -> list[list[Point]]:
        """The pieces of this stroke at a junction: its end segment (end given), or the
        segment(s) and corner passing through p."""
        rects, wedges = self.rects(h), self.wedges(h)
        n = len(self.pts)
        if end is not None:
            i = 0 if end == 0 else n - 2
            out = [rects[i]]
            v = 1 if end == 0 else n - 2
            if v in wedges:
                out.append(wedges[v])
            return out
        out = []
        for i, (a, b) in enumerate(zip(self.pts, self.pts[1:])):
            if _seg_dist(p, p, a, b) <= 1e-6:
                out.append(rects[i])
        for k, wdg in wedges.items():
            if math.dist(self.pts[k], p) <= 1e-6:
                out.append(wdg)
        return out


def _on_polyline(p: Point, pts: tuple[Point, ...] | list[Point]) -> tuple[bool, bool, Point | None]:
    """(on it, at one of its ends, the direction of the segment it's inside, when not at a vertex)."""
    for a, b in zip(pts, pts[1:]):
        if _seg_dist(p, p, a, b) <= 1e-9:
            at_end = math.dist(p, pts[0]) <= 1e-9 or math.dist(p, pts[-1]) <= 1e-9
            vertex = any(math.dist(p, q) <= 1e-9 for q in pts)
            return True, at_end, None if vertex else _unit(_sub(b, a))
    return False, False, None


def _trimmed(pts: list[Point], start: float, end: float) -> list[Point]:
    """Ends moved in by start / end (negative: extended)."""
    out = list(pts)
    if start:
        d = _unit(_sub(out[1], out[0]))
        if start >= math.dist(out[0], out[1]) - 1e-9:
            raise GlyphError("too short")
        out[0] = _add(out[0], d, start)
    if end:
        d = _unit(_sub(out[-2], out[-1]))
        if end >= math.dist(out[-1], out[-2]) - 1e-9:
            raise GlyphError("too short")
        out[-1] = _add(out[-1], d, end)
    return out


@lru_cache(maxsize=512)
def stencil_glyph(ch: str, gap: float) -> tuple[tuple[Point, ...], ...]:
    """A glyph's stencil outlines (closed polygons, units), with breaks of at least `gap` units."""
    _, polys = GLYPHS[ch]
    h = STROKE / 2
    n = len(polys)
    trims = [[0.0, 0.0] for _ in range(n)]
    caps: list[list[Point | None]] = [[None, None] for _ in range(n)]
    # Junctions: every point where a stroke's end meets another stroke.
    junctions: list[tuple[Point, list[tuple[int, int]], list[int]]] = []
    joined: set[tuple[int, int]] = set()
    for i, pts in enumerate(polys):
        for which, p in ((0, pts[0]), (1, pts[-1])):
            if any(math.dist(p, j[0]) <= 1e-9 for j in junctions):
                continue
            ends, through = [], []
            cap = None
            for k, other in enumerate(polys):
                for wk, q in ((0, other[0]), (1, other[-1])):
                    if math.dist(p, q) <= 1e-9:
                        ends.append((k, wk))
                on, at_end, along = _on_polyline(p, other)
                if on and not at_end:
                    through.append(k)
                    cap = along
            if len(ends) + len(through) > 1:
                junctions.append((p, ends, through))
                joined |= set(ends)
                if len(ends) == 1 and len(through) == 1 and cap is not None:
                    caps[ends[0][0]][ends[0][1]] = cap  # a T on a straight stretch: cut along it
    for k in range(n):  # free ends are square: they reach the glyph's box
        for wk in (0, 1):
            if (k, wk) not in joined:
                trims[k][wk] = -h

    def stroke(k: int) -> Stroke:
        return Stroke(_trimmed(list(polys[k]), trims[k][0], trims[k][1]), (caps[k][0], caps[k][1]))

    for p, ends, through in junctions:
        members = sorted({k for k, _ in ends} | set(through))
        end_of = {k: wk for k, wk in ends}

        def ok(t: float) -> bool:
            saved = [trims[k][:] for k in range(n)]
            try:
                for k, wk in ends:
                    trims[k][wk] = max(saved[k][wk], t)
                near = {k: stroke(k).near(h, p, end_of.get(k)) for k in members}
            finally:
                for k in range(n):
                    trims[k] = saved[k]
            for x in range(len(members)):
                for y in range(x + 1, len(members)):
                    pa, pb = near[members[x]], near[members[y]]
                    if min((poly_dist(u, v) for u in pa for v in pb), default=math.inf) < gap - 1e-9:
                        return False
            return True

        lo, hi = 0.0, 0.0
        while True:
            try:
                if ok(hi):
                    break
            except GlyphError:
                raise GlyphError(f"{ch!r} can't keep a stencil break of {gap:.3g} units") from None
            lo, hi = hi, hi + 0.02
        for _ in range(30):
            mid = (lo + hi) / 2
            if ok(mid):
                hi = mid
            else:
                lo = mid
        for k, wk in ends:
            trims[k][wk] = max(trims[k][wk], hi)
    try:
        strokes = [stroke(k) for k in range(n)]
    except GlyphError:
        raise GlyphError(f"{ch!r} can't keep a stencil break of {gap:.3g} units") from None
    # Every piece clear of every other by the gap.
    for x in range(n):
        for y in range(x + 1, n):
            dmin = min(poly_dist(u, v) for u in strokes[x].parts(h) for v in strokes[y].parts(h))
            if dmin < gap - 1e-6:
                raise GlyphError(f"{ch!r}: strokes {x} and {y} are {dmin:.3f} units apart, under the {gap:.3g} "
                                 "unit stencil break")
    return tuple(tuple(s.outline(h)) for s in strokes)


def layout(text: str, spacing_units: float = 1.0) -> list[tuple[str, float, float]]:
    """Each drawn character and its origin (units), lines downward from y = 0, left-aligned."""
    out = []
    for row, line in enumerate(text.split("\n")):
        x = 0.0
        for c in line:
            if c == " ":
                x += SPACE
                continue
            out.append((c, x, -row * LINE))
            x += GLYPHS[c][0] + spacing_units
    return out


def stroke_paths(text: str, spacing_units: float = 1.0) -> list[tuple[int, tuple[Point, ...]]]:
    """Stroke text: (glyph index, polyline) in units."""
    out = []
    for gi, (c, x, y) in enumerate(layout(text, spacing_units)):
        for pts in GLYPHS[c][1]:
            out.append((gi, tuple((x + px, y + py) for px, py in pts)))
    return out


def stencil_outlines(text: str, gap_units: float, spacing_units: float = 1.0) -> list[tuple[int, tuple[Point, ...]]]:
    """Stencil text: (glyph index, closed polygon) in units."""
    out = []
    for gi, (c, x, y) in enumerate(layout(text, spacing_units)):
        for poly in stencil_glyph(c, round(gap_units, 6)):
            out.append((gi, tuple((x + px, y + py) for px, py in poly)))
    return out

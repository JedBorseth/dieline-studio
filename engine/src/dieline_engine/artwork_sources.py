"""Where artwork outlines come from, in the artwork's own local frame
(docs/DESIGN-SYSTEM.md, "Artwork / Sources"): the shape library, stencil and
stroke text, the agent's sketch, and (artwork_import.py) imported files.

Every source ends as `Contour`s: lines and true arcs in inches around the
artwork's datum (the centre of its outline), +x right and +y up as seen from
outside the box. Curves are fitted with biarcs at final size (curves.py), and
their source curvature is kept for the tooling checks.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from . import glyphs
from .curves import (ARC_CORRECTION_MAX, FIT_TOL_IN, Bezier, Curve, CurveError, EllipseArc, Nurbs, arc_prim,
                     axis_ellipse, check_nurbs, fit, svg_arc)
from .shapes import Prim, bbox as prim_bbox, circle as sh_circle, rounded_rect, transform

Point = tuple[float, float]
SNAP_IN = 0.005  # a loop declared closed may snap its ends this close (final size)
CLOSE_EPS = 1e-9
MAX_SEGMENTS = 5000  # per artwork, after fitting
MAX_CONTOURS = 600
MAX_PATH_CHARS = 20000
MAX_SKETCH_ITEMS = 200


class SourceError(ValueError):
    """The artwork's source can't be drawn: says what to change."""


@dataclass(slots=True)
class Contour:
    prims: list[Prim]
    kinds: list[str]  # per prim: line | arc (from the source) | fit (part of a fitted curve) | fallback (a chord)
    closed: bool
    rule: str | None = None  # the source's own default (stroke text: crease; a DXF crease colour)
    radii: list[tuple[float, Point]] = field(default_factory=list)  # source curvature samples (radius, point)
    group: int | None = None  # text: which glyph
    label: str = ""
    snapped: float = 0.0  # how far its ends were snapped to close it
    corners: list[int] = field(default_factory=list)  # prim indices that start at a true corner (set later)

    def mapped(self, f, reflects: bool, k: float = 1.0) -> "Contour":
        return Contour([p.map(f, reflects) for p in self.prims], list(self.kinds), self.closed, self.rule,
                       [(r * k, f(q)) for r, q in self.radii], self.group, self.label, self.snapped,
                       list(self.corners))


@dataclass(slots=True)
class Local:
    contours: list[Contour]
    provenance: str  # library | import | sketch
    words: str
    notes: list[tuple[str, str]] = field(default_factory=list)  # (status, message)
    fallback: int = 0  # straight chords where arcs couldn't fit


# ------------------------------------------------------------------ pieces


@dataclass(slots=True)
class Sub:
    """One subpath before scaling: exact prims and curves to fit, in source units, y up."""

    pieces: list[Any]  # Prim | Curve
    closed: bool
    rule: str | None = None
    label: str = ""

    def start(self) -> Point:
        p = self.pieces[0]
        return p.start if isinstance(p, Prim) else p.point(0.0)

    def end(self) -> Point:
        p = self.pieces[-1]
        return p.end if isinstance(p, Prim) else p.point(1.0)


def _piece_points(p: Any) -> list[Point]:
    if isinstance(p, Prim):
        if p.center is None:
            return [p.start, p.end]
        from .curves import prim_points  # noqa: PLC0415

        return prim_points(p, 16)
    return [p.point(i / 48) for i in range(49)]


def subs_bbox(subs: list[Sub]) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for s in subs:
        for p in s.pieces:
            for x, y in _piece_points(p):
                xs.append(x)
                ys.append(y)
    if not xs:
        raise SourceError("the artwork has no outline")
    return (min(xs), min(ys), max(xs), max(ys))


def scale_for(box: tuple[float, float, float, float], size: dict[str, float], natural: float | None,
              what: str) -> float:
    """Inches per source unit: from width, height or fit_box (uniform), else the file's own units."""
    w, h = box[2] - box[0], box[3] - box[1]
    if "width" in size:
        if w <= 1e-12:
            raise SourceError(f"{what} has no width to scale")
        return size["width"] / w
    if "height" in size:
        if h <= 1e-12:
            raise SourceError(f"{what} has no height to scale")
        return size["height"] / h
    if "fit_box" in size:
        bw, bh = size["fit_box"]
        ks = [bw / w if w > 1e-12 else math.inf, bh / h if h > 1e-12 else math.inf]
        k = min(ks)
        if not math.isfinite(k):
            raise SourceError(f"{what} has no size to scale")
        return k
    if natural is None:
        raise SourceError(f"{what} has no units of its own: give its width, height or fit_box")
    return natural


def finish(subs: list[Sub], k: float, *, declared_closed: bool, tol: float = FIT_TOL_IN,
           what: str = "the artwork") -> tuple[list[Contour], int]:
    """Scale by k about the outline's centre, fit the curves at final size, close what may be closed.
    Returns the contours and how many chords stand in for arcs."""
    x0, y0, x1, y1 = subs_bbox(subs)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2

    def f(p: Point) -> Point:
        return ((p[0] - cx) * k, (p[1] - cy) * k)

    out: list[Contour] = []
    fallback = 0
    total = 0
    for si, s in enumerate(subs):
        prims: list[Prim] = []
        kinds: list[str] = []
        radii: list[tuple[float, Point]] = []
        for p in s.pieces:
            if isinstance(p, Prim):
                prims.append(p.map(f, False))
                kinds.append("arc" if p.center is not None else "line")
                continue
            curve = p.map(f)
            ft = fit(curve, tol)
            prims += ft.prims
            kinds += ft.kinds
            fallback += ft.fallback
            radii += curve.radius_samples()
        pairs = [(p, kind) for p, kind in zip(prims, kinds, strict=True)
                 if p.full or math.dist(p.start, p.end) > 1e-9]
        if not pairs:
            continue
        prims = [p for p, _ in pairs]
        kinds = [kind for _, kind in pairs]
        closed = s.closed
        snapped = 0.0
        gap = math.dist(prims[0].start, prims[-1].end)
        if not closed and not prims[0].full and gap <= CLOSE_EPS * max(1.0, k):
            closed = True
        elif not closed and declared_closed and not prims[0].full:
            if gap > SNAP_IN + 1e-12:
                raise SourceError(f"{what}: path {si + 1} is declared closed, but its ends are {gap:.4f} in apart "
                                  f"at final size (they may snap only within {SNAP_IN} in); close it with Z or "
                                  "join its ends")
            closed, snapped = True, gap
        if closed and not prims[0].full and gap > 1e-12:
            # Join the ends exactly: a line or the last arc ends on the start.
            last = prims[-1]
            if last.center is None or math.dist(last.end, prims[0].start) <= 1e-7:
                prims[-1] = Prim(last.start, prims[0].start, last.center, last.clockwise)
            else:
                prims.append(Prim(last.end, prims[0].start))
                kinds.append("line")
        # Chain exactly: float noise is snapped; a real gap (a snapped join) gets a short line.
        i = 1
        while i < len(prims):
            q, prev = prims[i], prims[i - 1]
            if q.start != prev.end and not q.full:
                if math.dist(q.start, prev.end) > 1e-7:
                    prims.insert(i, Prim(prev.end, q.start))
                    kinds.insert(i, "line")
                    i += 1
                    continue
                prims[i] = Prim(prev.end, q.end, q.center, q.clockwise)
            i += 1
        total += len(prims)
        if total > MAX_SEGMENTS:
            raise SourceError(f"{what} has more than {MAX_SEGMENTS} segments after fitting; simplify it")
        out.append(Contour(prims, kinds[:len(prims)], closed, s.rule, radii, None, s.label or f"path {si + 1}",
                           snapped))
    if len(out) > MAX_CONTOURS:
        raise SourceError(f"{what} has more than {MAX_CONTOURS} separate outlines")
    if not out:
        raise SourceError(f"{what} has no outline")
    return out, fallback


def mark_corners(c: Contour) -> None:
    """Where the outline turns sharply (a true corner): between prims whose tangents differ."""
    n = len(c.prims)
    corners = []
    for i in range(n if c.closed else n - 1):
        a, b = c.prims[i], c.prims[(i + 1) % n]
        if not c.closed and i + 1 >= n:
            break
        ta, tb = end_tangent(a), start_tangent(b)
        if abs(math.atan2(ta[0] * tb[1] - ta[1] * tb[0], ta[0] * tb[0] + ta[1] * tb[1])) > math.radians(1.0):
            corners.append((i + 1) % n)
    c.corners = corners


def start_tangent(p: Prim) -> Point:
    if p.center is None:
        d = (p.end[0] - p.start[0], p.end[1] - p.start[1])
    else:
        r = (p.start[0] - p.center[0], p.start[1] - p.center[1])
        d = (r[1], -r[0]) if p.clockwise else (-r[1], r[0])
    n = math.hypot(*d) or 1.0
    return (d[0] / n, d[1] / n)


def end_tangent(p: Prim) -> Point:
    if p.center is None:
        d = (p.end[0] - p.start[0], p.end[1] - p.start[1])
    else:
        r = (p.end[0] - p.center[0], p.end[1] - p.center[1])
        d = (r[1], -r[0]) if p.clockwise else (-r[1], r[0])
    n = math.hypot(*d) or 1.0
    return (d[0] / n, d[1] / n)


# ------------------------------------------------------------------ the shape library

LIBRARY = ("circle", "ellipse", "rounded_rect", "polygon", "star", "heart", "smiley", "arrow")


def fillet(pts: list[Point], r: float, what: str) -> list[Prim]:
    """A closed polygon with every corner rounded to radius r (0: sharp)."""
    n = len(pts)
    if r <= 0:
        return [Prim(pts[i], pts[(i + 1) % n]) for i in range(n)]
    cuts: list[tuple[Point, Point, Point | None, bool]] = []  # tangent in, tangent out, centre, cw
    for i in range(n):
        p0, p, p1 = pts[i - 1], pts[i], pts[(i + 1) % n]
        u = _unit((p0[0] - p[0], p0[1] - p[1]))
        v = _unit((p1[0] - p[0], p1[1] - p[1]))
        cosang = max(-1.0, min(1.0, u[0] * v[0] + u[1] * v[1]))
        ang = math.acos(cosang)
        if ang <= 1e-9 or abs(ang - math.pi) <= 1e-9:
            cuts.append((p, p, None, False))
            continue
        t = r / math.tan(ang / 2)
        if t > min(math.dist(p0, p), math.dist(p, p1)) / 2 + 1e-12:
            raise SourceError(f"{what}: corner radius {r:.4g} in is too big for its corners; make it smaller")
        a = (p[0] + u[0] * t, p[1] + u[1] * t)
        b = (p[0] + v[0] * t, p[1] + v[1] * t)
        bis = _unit((u[0] + v[0], u[1] + v[1]))
        dist = r / math.sin(ang / 2)
        c = (p[0] + bis[0] * dist, p[1] + bis[1] * dist)
        # Turning direction of the corner decides the arc's sense.
        din = (p[0] - p0[0], p[1] - p0[1])
        dout = (p1[0] - p[0], p1[1] - p[1])
        cw = din[0] * dout[1] - din[1] * dout[0] < 0
        cuts.append((a, b, c, cw))
    out: list[Prim] = []
    for i in range(n):
        a, b, c, cw = cuts[i]
        if c is not None:
            out.append(Prim(a, b, c, cw))
        nxt = cuts[(i + 1) % n][0]
        if math.dist(b, nxt) > 1e-12:
            out.append(Prim(b, nxt))
    return out


def _unit(v: Point) -> Point:
    n = math.hypot(*v)
    return (v[0] / n, v[1] / n)


def _contour(prims: list[Prim], label: str, kinds: list[str] | None = None, rule: str | None = None,
             radii: list[tuple[float, Point]] | None = None) -> Contour:
    ks = kinds or ["arc" if p.center is not None else "line" for p in prims]
    return Contour(prims, ks, True, rule, radii or [], None, label)


def library(spec: dict[str, Any], size) -> Local:
    """size(name, what) evaluates a length field in inches (required unless a default is given)."""
    from .checks import fmt_in  # noqa: PLC0415

    shape = spec["shape"]
    what = f"{shape}"
    if shape == "circle":
        d = size("diameter")
        return Local([_contour(sh_circle(d), "circle")], "library", f"{fmt_in(d)} in circle")
    if shape == "ellipse":
        w, h = size("width"), size("height")
        e = EllipseArc((0.0, 0.0), w / 2, h / 2, 0.0, 0.0, math.tau)
        ft = fit(e)
        c = Contour(ft.prims, ft.kinds, True, None, e.radius_samples(), None, "ellipse")
        return Local([c], "library", f"{fmt_in(w)} x {fmt_in(h)} in ellipse (arcs within 0.002 in)",
                     fallback=ft.fallback)
    if shape == "rounded_rect":
        w, h = size("width"), size("height")
        r = size("corner_radius", 0.0)
        if r > min(w, h) / 2 + 1e-9:
            raise SourceError("rounded_rect: corner_radius must be at most half the smaller side")
        return Local([_contour(rounded_rect(w, h, r), "rectangle")], "library",
                     f"{fmt_in(w)} x {fmt_in(h)} in rectangle" + (f", corners r {fmt_in(r)}" if r else ""))
    if shape in ("polygon", "star"):
        n = spec.get("sides" if shape == "polygon" else "points", 6 if shape == "polygon" else 5)
        if isinstance(n, bool) or not isinstance(n, int) or not 3 <= n <= 24:
            raise SourceError(f"{shape}: {'sides' if shape == 'polygon' else 'points'} must be a whole number "
                              "from 3 to 24")
        d = size("diameter")
        r = size("corner_radius", 0.0)
        R = d / 2
        if shape == "polygon":
            pts = [(R * math.cos(math.pi / 2 + i * math.tau / n), R * math.sin(math.pi / 2 + i * math.tau / n))
                   for i in range(n)]
            words = f"{n}-sided polygon, {fmt_in(d)} in across its corners"
        else:
            k = spec.get("inner_ratio", 0.5)
            if isinstance(k, bool) or not isinstance(k, (int, float)) or not 0.15 <= k <= 0.9:
                raise SourceError("star: inner_ratio must be between 0.15 and 0.9")
            pts = []
            for i in range(2 * n):
                rr = R if i % 2 == 0 else R * k
                a = math.pi / 2 + i * math.pi / n
                pts.append((rr * math.cos(a), rr * math.sin(a)))
            words = f"{n}-point star, {fmt_in(d)} in across its points, inner ratio {k:g}"
        prims = fillet(pts, r, shape)
        if r:
            words += f", corners r {fmt_in(r)}"
        return Local([_contour(prims, shape)], "library", words)
    if shape == "heart":
        w, h = size("width"), size("height")
        kk = 0.6
        r = w / (2 * (1 + kk))
        y0 = h / 2 - r
        P = (0.0, -h / 2)
        out = []
        for side in (1, -1):
            C = (side * kk * r, y0)
            v = (C[0] - P[0], C[1] - P[1])
            L = math.hypot(*v)
            if L <= r * 1.05:
                raise SourceError("heart: too short for its width; make it taller")
            alpha = math.asin(r / L)
            base = math.atan2(v[1], v[0])
            ang = base - side * alpha
            tl = math.sqrt(L * L - r * r)
            out.append((P[0] + tl * math.cos(ang), P[1] + tl * math.sin(ang)))
        T_r, T_l = out
        X = (0.0, y0 + r * math.sqrt(1 - kk * kk))
        prims = [Prim(P, T_r), Prim(T_r, X, (kk * r, y0), False), Prim(X, T_l, (-kk * r, y0), False),
                 Prim(T_l, P)]
        return Local([_contour(prims, "heart")], "library", f"{fmt_in(w)} x {fmt_in(h)} in heart")
    if shape == "smiley":
        d = size("diameter")
        R = d / 2
        eye = 0.07 * d
        cs = [Contour(transform(sh_circle(2 * eye), offset=(sx * 0.18 * d, 0.15 * d)), ["arc"], True, None, [],
                      None, f"{'left' if sx < 0 else 'right'} eye") for sx in (-1, 1)]
        # The mouth: a band between two arcs, with round ends.
        c0 = (0.0, 0.05 * d)
        ro, ri = 0.32 * d, 0.22 * d
        rm, rc = (ro + ri) / 2, (ro - ri) / 2
        a0, a1 = math.radians(215), math.radians(325)

        def at(rad: float, a: float) -> Point:
            return (c0[0] + rad * math.cos(a), c0[1] + rad * math.sin(a))

        mouth = [Prim(at(ro, a0), at(ro, a1), c0, False),
                 Prim(at(ro, a1), at(ri, a1), at(rm, a1), False),
                 Prim(at(ri, a1), at(ri, a0), c0, True),
                 Prim(at(ri, a0), at(ro, a0), at(rm, a0), False)]
        cs.append(Contour(mouth, ["arc"] * 4, True, None, [], None, "mouth"))
        outline = spec.get("outline", "none")
        words = f"{fmt_in(d)} in smiley: eyes and mouth cut out"
        if outline == "crease":
            cs.append(Contour(sh_circle(d), ["arc"], True, "crease", [], None, "face outline"))
            words += ", face outline creased"
        del R
        return Local(cs, "library", words)
    if shape == "arrow":
        L = size("length")
        W = size("width")
        hl = size("head_length", 0.4 * L)
        sw = size("shaft_width", 0.4 * W)
        if hl >= L or sw >= W:
            raise SourceError("arrow: head_length must be under its length and shaft_width under its width")
        x0, x1, xh = -L / 2, L / 2, L / 2 - hl
        pts = [(x0, -sw / 2), (xh, -sw / 2), (xh, -W / 2), (x1, 0.0), (xh, W / 2), (xh, sw / 2), (x0, sw / 2)]
        return Local([_contour(fillet(pts, 0.0, "arrow"), "arrow")], "library",
                     f"{fmt_in(L)} x {fmt_in(W)} in arrow pointing right (turn it with rotation_deg)")
    raise SourceError(f"unknown shape {shape!r}; shapes: {', '.join(LIBRARY)}")


# ------------------------------------------------------------------ text


def text(spec: dict[str, Any], height: float, bridge_min: float, web_min: float, spacing: float | None) -> Local:
    from .checks import fmt_in  # noqa: PLC0415

    s = glyphs.check_text(spec["text"])
    u = height / glyphs.CAP
    sp = (spacing / u) if spacing is not None else max(1.0, web_min / u)
    if spec["kind"] == "stroke_text":
        cs = []
        for gi, pts in glyphs.stroke_paths(s, sp):
            prims = [Prim((a[0] * u, a[1] * u), (b[0] * u, b[1] * u)) for a, b in zip(pts, pts[1:])]
            cs.append(Contour(prims, ["line"] * len(prims), False, "crease", [], gi, f"{s.replace(chr(10), ' ')}"))
        _centre(cs)
        return Local(cs, "library", f"stroke text \"{spec['text']}\", {fmt_in(height)} in capitals, single lines")
    gap_u = max(glyphs.GAP_UNITS, bridge_min / u)
    try:
        outlines = glyphs.stencil_outlines(s, gap_u, sp)
    except glyphs.GlyphError:
        need = _min_height(s, bridge_min, sp * u if spacing is not None else None, web_min)
        raise SourceError(f"stencil text {fmt_in(height)} in tall can't keep stencil breaks of "
                          f"{fmt_in(round(bridge_min * 1024) / 1024)} in (the minimum bridge on this board); make it "
                          f"at least {fmt_in(need)} in tall") from None
    cs = []
    for gi, poly in outlines:
        pts = [(x * u, y * u) for x, y in poly]
        prims = [Prim(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))]
        cs.append(Contour(prims, ["line"] * len(prims), True, None, [], gi, f"glyph {gi + 1}"))
    _centre(cs)
    gap_in = gap_u * u
    return Local(cs, "library", f"stencil text \"{spec['text']}\", {fmt_in(height)} in capitals, stencil breaks "
                                f"{fmt_in(round(gap_in * 1024) / 1024)} in")


def _min_height(s: str, bridge_min: float, spacing_in: float | None, web_min: float) -> float:
    h = 0.5
    while h < 40:
        u = h / glyphs.CAP
        try:
            glyphs.stencil_outlines(s, max(glyphs.GAP_UNITS, bridge_min / u),
                                    spacing_in / u if spacing_in else max(1.0, web_min / u))
            return math.ceil(h * 8) / 8
        except glyphs.GlyphError:
            h *= 1.1
    return h


def _centre(cs: list[Contour]) -> None:
    pts = [q for c in cs for p in c.prims for q in (p.start, p.end)]
    cx = (min(p[0] for p in pts) + max(p[0] for p in pts)) / 2
    cy = (min(p[1] for p in pts) + max(p[1] for p in pts)) / 2
    for c in cs:
        c.prims = [Prim((p.start[0] - cx, p.start[1] - cy), (p.end[0] - cx, p.end[1] - cy)) for p in c.prims]


# ------------------------------------------------------------------ the agent's sketch (0-100 canvas)

CANVAS = 100.0
_NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_TOKEN = re.compile(r"[MmLlQqCcAaZzHhVvSsTt]|" + _NUMBER.pattern)


def parse_path(d: str, *, what: str, canvas: bool, corrections: list[float], strict_arcs: bool,
               full: bool = False, max_chars: int = MAX_PATH_CHARS) -> list[Sub]:
    """An SVG path as subpaths in its own units, y as written. The sketch takes M L Q C A Z
    (absolute or relative); `full` also takes H V S T, as imported files use them.
    canvas: coordinates must stay within the 0-100 canvas. strict_arcs: refuse arcs whose radius SVG
    would enlarge by more than 1 % (the sketch's rule)."""
    if not isinstance(d, str) or not d.strip():
        raise SourceError(f"{what}: a path needs its d string")
    if len(d) > max_chars:
        raise SourceError(f"{what}: the path is over {max_chars} characters; simplify it")
    toks = _TOKEN.findall(d)
    leftover = _TOKEN.sub("", d).replace(",", "").strip()
    if leftover:
        raise SourceError(f"{what}: can't read {leftover[:20]!r} in the path")
    subs: list[Sub] = []
    cur: list[Any] = []
    pos: Point = (0.0, 0.0)
    start: Point = (0.0, 0.0)
    i = 0
    cmd = None
    closed = False

    def num() -> float:
        nonlocal i
        if i >= len(toks) or toks[i].isalpha():
            raise SourceError(f"{what}: a {cmd} command is missing a number")
        v = float(toks[i])
        i += 1
        if not math.isfinite(v):
            raise SourceError(f"{what}: a number isn't finite")
        return v

    def flag() -> int:
        # SVG's grammar: an arc flag is one character, 0 or 1, so "0150" is flags 0 and 1, then 50.
        nonlocal i
        if i >= len(toks) or toks[i].isalpha():
            raise SourceError(f"{what}: a {cmd} command is missing a number")
        t = toks[i]
        rest = t[1:]
        if t[0] not in "01" or (rest and not _NUMBER.fullmatch(rest)):
            raise SourceError(f"{what}: an arc's large-arc and sweep flags must be 0 or 1")
        if rest:
            toks[i] = rest
        else:
            i += 1
        return int(t[0])

    def check(p: Point) -> Point:
        if canvas and not (-1e-9 <= p[0] <= CANVAS + 1e-9 and -1e-9 <= p[1] <= CANVAS + 1e-9):
            raise SourceError(f"{what}: point ({p[0]:g}, {p[1]:g}) is outside the 0-100 canvas")
        return p

    def flush() -> None:
        nonlocal cur, closed
        if cur:
            subs.append(Sub(cur, closed))
        cur, closed = [], False

    while i < len(toks):
        t = toks[i]
        if t.isalpha():
            cmd = t
            i += 1
            if cmd in "Zz":
                if cur:
                    if math.dist(pos, start) > 1e-12:
                        cur.append(Prim(pos, start))
                    closed = True
                    flush()
                pos = start
                continue
            if cmd in "HhVvSsTt" and not full:
                raise SourceError(f"{what}: {cmd} isn't one of the sketch's commands (M L Q C A Z); write it with "
                                  "L, C or Q")
        elif cmd is None:
            raise SourceError(f"{what}: the path must start with M")
        rel = cmd.islower()
        c = cmd.upper()
        base = pos if rel else (0.0, 0.0)
        if c in "HV":
            v = num()
            q = check((base[0] + v, pos[1]) if c == "H" else (pos[0], base[1] + v))
            if math.dist(q, pos) > 1e-12:
                cur.append(Prim(pos, q))
            pos = q
        elif c in "ST":
            prev = cur[-1] if cur else None
            want = 4 if c == "S" else 3
            if isinstance(prev, Bezier) and len(prev.pts) == want:
                r = (2 * pos[0] - prev.pts[-2][0], 2 * pos[1] - prev.pts[-2][1])
            else:
                r = pos
            if c == "S":
                c2 = check((base[0] + num(), base[1] + num()))
                q = check((base[0] + num(), base[1] + num()))
                cur.append(Bezier((pos, r, c2, q)))
            else:
                q = check((base[0] + num(), base[1] + num()))
                cur.append(Bezier((pos, r, q)))
            pos = q
        elif c == "M":
            flush()
            pos = check((base[0] + num(), base[1] + num()))
            start = pos
            cmd = "l" if rel else "L"  # extra pairs after M are lines
        elif c == "L":
            q = check((base[0] + num(), base[1] + num()))
            if math.dist(q, pos) > 1e-12:
                cur.append(Prim(pos, q))
            pos = q
        elif c == "Q":
            c1 = check((base[0] + num(), base[1] + num()))
            q = check((base[0] + num(), base[1] + num()))
            cur.append(Bezier((pos, c1, q)))
            pos = q
        elif c == "C":
            c1 = check((base[0] + num(), base[1] + num()))
            c2 = check((base[0] + num(), base[1] + num()))
            q = check((base[0] + num(), base[1] + num()))
            cur.append(Bezier((pos, c1, c2, q)))
            pos = q
        elif c == "A":
            rx, ry, rot = num(), num(), num()
            large, sweep = flag(), flag()
            q = check((base[0] + num(), base[1] + num()))
            if rx == 0 or ry == 0:
                if math.dist(q, pos) > 1e-12:
                    cur.append(Prim(pos, q))
            else:
                arc = svg_arc(pos, rx, ry, rot, bool(large), bool(sweep), q)
                if arc is not None:
                    if arc.correction > 1e-9:
                        corrections.append(arc.correction)
                        if strict_arcs and arc.correction > ARC_CORRECTION_MAX:
                            raise SourceError(
                                f"{what}: an arc's radius is too small to reach its end point; SVG would enlarge it "
                                f"by {arc.correction * 100:.1f}% (more than 1% is refused). Give the arc a radius "
                                "of at least half the distance between its ends")
                    exact = arc_prim(arc.curve)
                    cur.append(exact if exact is not None else axis_ellipse(arc.curve))
            pos = q
        if i < len(toks) and not toks[i].isalpha() and c == "Z":
            raise SourceError(f"{what}: numbers after Z")
    flush()
    return [s for s in subs if s.pieces]


def _flip(subs: list[Sub], height: float) -> list[Sub]:
    """y down (SVG, the sketch canvas) to y up."""
    def f(p: Point) -> Point:
        return (p[0], height - p[1])

    out = []
    for s in subs:
        pieces = [p.map(f, True) if isinstance(p, Prim) else p.map(f) for p in s.pieces]
        out.append(Sub(pieces, s.closed, s.rule, s.label))
    return out


def sketch_subs(sketch: dict[str, Any], corrections: list[float]) -> list[Sub]:
    """The sketch's shapes and paths, y up."""
    subs: list[Sub] = []
    shapes = sketch.get("shapes") or []
    paths = sketch.get("paths") or []
    if len(shapes) + len(paths) > MAX_SKETCH_ITEMS:
        raise SourceError(f"a sketch is limited to {MAX_SKETCH_ITEMS} shapes and paths")

    def n(v: Any, name: str, what: str) -> float:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise SourceError(f"{what}.{name} must be a number on the 0-100 canvas")
        return float(v)

    def inside(p: Point, what: str) -> None:
        if not (-1e-9 <= p[0] <= CANVAS + 1e-9 and -1e-9 <= p[1] <= CANVAS + 1e-9):
            raise SourceError(f"{what} reaches outside the 0-100 canvas")

    for k, s in enumerate(shapes):
        what = f"sketch shape {k + 1}"
        if not isinstance(s, dict):
            raise SourceError(f"{what} must be an object with a type")
        t = s.get("type")
        if t == "circle":
            cx, cy, r = n(s.get("cx"), "cx", what), n(s.get("cy"), "cy", what), n(s.get("r"), "r", what)
            if r <= 0:
                raise SourceError(f"{what}: r must be above zero")
            inside((cx - r, cy - r), what)
            inside((cx + r, cy + r), what)
            subs.append(Sub([Prim((cx + r, cy), (cx + r, cy), (cx, cy), False, True)], True, label=what))
        elif t == "ellipse":
            cx, cy = n(s.get("cx"), "cx", what), n(s.get("cy"), "cy", what)
            rx, ry = n(s.get("rx"), "rx", what), n(s.get("ry"), "ry", what)
            rot = n(s.get("rotation", 0), "rotation", what)
            if rx <= 0 or ry <= 0:
                raise SourceError(f"{what}: rx and ry must be above zero")
            e = EllipseArc((cx, cy), rx, ry, math.radians(rot), 0.0, math.tau)
            for q in (e.point(i / 64) for i in range(64)):
                inside(q, what)
            if abs(rx - ry) <= 1e-12:
                subs.append(Sub([Prim((cx + rx, cy), (cx + rx, cy), (cx, cy), False, True)], True, label=what))
            else:
                subs.append(Sub([e], True, label=what))
        elif t == "rect":
            x, y = n(s.get("x"), "x", what), n(s.get("y"), "y", what)
            w, h = n(s.get("width"), "width", what), n(s.get("height"), "height", what)
            r = n(s.get("r", 0), "r", what)
            if w <= 0 or h <= 0 or r < 0 or r > min(w, h) / 2:
                raise SourceError(f"{what}: width and height above zero, r from 0 to half the smaller side")
            inside((x, y), what)
            inside((x + w, y + h), what)
            prims = transform(rounded_rect(w, h, r), offset=(x + w / 2, y + h / 2))
            subs.append(Sub(prims, True, label=what))
        elif t == "polygon":
            pts = s.get("points")
            if not isinstance(pts, list) or len(pts) < 3 or len(pts) > 400:
                raise SourceError(f"{what}: points must be a list of 3 to 400 [x, y] pairs")
            ps = []
            for p in pts:
                if not (isinstance(p, list) and len(p) == 2):
                    raise SourceError(f"{what}: each point is [x, y]")
                q = (n(p[0], "x", what), n(p[1], "y", what))
                inside(q, what)
                ps.append(q)
            prims = [Prim(ps[i], ps[(i + 1) % len(ps)]) for i in range(len(ps)) if ps[i] != ps[(i + 1) % len(ps)]]
            subs.append(Sub(prims, True, label=what))
        else:
            raise SourceError(f"{what}: type must be circle, ellipse, rect or polygon")
    for k, p in enumerate(paths):
        what = f"sketch path {k + 1}"
        d = p.get("d") if isinstance(p, dict) else p
        for sub in parse_path(d, what=what, canvas=True, corrections=corrections, strict_arcs=True):
            sub.label = what
            subs.append(sub)
    if not subs:
        raise SourceError("the sketch has no shapes or paths")
    return _flip(subs, CANVAS)


def sketch(spec: dict[str, Any], size_in: dict[str, float], declared_closed: bool) -> Local:
    from .checks import fmt_in  # noqa: PLC0415

    corrections: list[float] = []
    subs = sketch_subs(spec["sketch"], corrections)
    box = subs_bbox(subs)
    k = scale_for(box, size_in, None, "the sketch")
    cs, fallback = finish(subs, k, declared_closed=declared_closed, what="the sketch")
    w, h = (box[2] - box[0]) * k, (box[3] - box[1]) * k
    local = Local(cs, "sketch", f"{fmt_in(round(w * 64) / 64)} x {fmt_in(round(h * 64) / 64)} in sketch, "
                                f"{len(cs)} outline{'s' if len(cs) != 1 else ''}", fallback=fallback)
    if corrections:
        local.notes.append(("info", f"{len(corrections)} sketch arc{'s' if len(corrections) > 1 else ''} had a "
                                    f"radius too small for its ends; enlarged by up to "
                                    f"{max(corrections) * 100:.2f}% as SVG does"))
    return local


__all__ = ["Contour", "Local", "Sub", "SourceError", "library", "text", "sketch", "finish", "scale_for", "subs_bbox",
           "parse_path", "mark_corners", "start_tangent", "end_tangent", "fillet", "LIBRARY", "Nurbs", "Curve",
           "CurveError", "check_nurbs"]

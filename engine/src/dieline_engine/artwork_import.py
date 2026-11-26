"""Imported artwork: an SVG or DXF the designer supplies from the reference
folders (docs/DESIGN-SYSTEM.md, "Artwork / Sources", item 3).

What's imported is the **cut contours**, never the filled appearance. A file
can look like one opening and cut as two (fill rules, strokes, clipping, hidden
paths), so the import lists everything that can make the two differ, and
everything it can't read, and nothing is committed until the designer has seen
the retained and discarded result and approved it (artwork_tools.py).

- SVG is read with defusedxml (no DTDs, no entities, no external references):
  path, rect, circle, ellipse, line, polygon, polyline and local `use`, with
  every transform resolved. Units come from the root's width/height when they're
  physical (in, mm, cm, pt, pc); px and unitless files need a size.
- DXF (ASCII): LINE, ARC, CIRCLE, LWPOLYLINE and POLYLINE (with bulges), SPLINE
  (control points, rational too), ELLIPSE and INSERT (scaled, rotated and
  mirrored blocks, nested). $INSUNITS is respected; without it a size is needed.
  Anything out of the drawing plane refuses the import. Colour 1 is read as
  cut and 2 as crease, as the shop's DXFs carry them.
- Limits: file size, elements or entities (blocks expanded), and segments after
  fitting (artwork_sources.MAX_SEGMENTS).

The parsed geometry is stored normalised in the part (`geometry`), with the
file's path and hash, so a moved or changed file can't change the design.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from typing import Any

from .artwork_sources import Sub, SourceError, parse_path
from .curves import AxisEllipse, Bezier, CurveError, Nurbs, check_nurbs, reverse_curve
from .shapes import Prim

Point = tuple[float, float]
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_ELEMENTS = 5000
MAX_WALKED_ELEMENTS = 4 * MAX_ELEMENTS
MAX_ENTITIES = 20000
MAX_DEPTH = 8
MAX_NESTED_COMPARE = 200  # closed subpaths of one filled path compared for the fill-rule note
MAX_IMPORT_PATH_CHARS = 500_000
UNITS_IN = {"in": 1.0, "mm": 1 / 25.4, "cm": 1 / 2.54, "pt": 1 / 72, "pc": 1 / 6, "ft": 12.0, "m": 1000 / 25.4}


class ImportRefused(ValueError):
    """The file can't be imported at all (binary, out of plane, too big, not readable)."""


@dataclass(slots=True)
class Parsed:
    subs: list[Sub]
    units_in: float | None  # inches per file unit, when the file says
    unsupported: list[str] = field(default_factory=list)  # listed; the import waits for the designer
    notes: list[str] = field(default_factory=list)  # how the file may look different from what's cut
    format: str = ""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse(name: str, data: bytes) -> Parsed:
    if len(data) > MAX_FILE_BYTES:
        raise ImportRefused(f"the file is over {MAX_FILE_BYTES // (1024 * 1024)} MB; artwork files are limited to that")
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    if ext == "svg":
        return parse_svg(data)
    if ext == "dxf":
        return parse_dxf(data)
    raise ImportRefused("artwork can be imported from SVG or DXF files only")


# ------------------------------------------------------------------ affine maps

Affine = tuple[float, float, float, float, float, float]  # a b c d e f: x' = a x + c y + e, y' = b x + d y + f
IDENTITY: Affine = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def compose(m: Affine, n: Affine) -> Affine:
    """m after n."""
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + c * B, b * A + d * B, a * C + c * D, b * C + d * D, a * E + c * F + e, b * E + d * F + f)


def apply(m: Affine, p: Point) -> Point:
    return (m[0] * p[0] + m[2] * p[1] + m[4], m[1] * p[0] + m[3] * p[1] + m[5])


def _similar(m: Affine) -> bool:
    """Uniform scale, rotation and maybe a mirror: circles stay circles."""
    a, b, c, d = m[:4]
    return abs(a * a + b * b - (c * c + d * d)) <= 1e-9 * (a * a + b * b + c * c + d * d) and \
        abs(a * c + b * d) <= 1e-9 * (a * a + b * b + c * c + d * d)


def _det(m: Affine) -> float:
    return m[0] * m[3] - m[1] * m[2]


def map_piece(p: Any, m: Affine) -> Any:
    f = lambda q: apply(m, q)  # noqa: E731
    if isinstance(p, Prim):
        if p.center is None:
            return Prim(f(p.start), f(p.end))
        if _similar(m):
            return p.map(f, _det(m) < 0)
        return _prim_ellipse(p).map(f)
    return p.map(f)


def _prim_ellipse(p: Prim) -> AxisEllipse:
    c = p.center
    r = math.dist(c, p.start)  # type: ignore[arg-type]
    a0 = math.atan2(p.start[1] - c[1], p.start[0] - c[0])  # type: ignore[index]
    if p.full:
        dth = -math.tau if p.clockwise else math.tau
    else:
        a1 = math.atan2(p.end[1] - c[1], p.end[0] - c[0])  # type: ignore[index]
        dth = (a1 - a0) % math.tau
        if p.clockwise:
            dth -= math.tau
    return AxisEllipse(c, (r, 0.0), (0.0, r), a0, dth)  # type: ignore[arg-type]


def reverse_piece(p: Any) -> Any:
    if isinstance(p, Prim):
        return Prim(p.end, p.start, p.center, not p.clockwise, p.full) if p.center is not None else Prim(p.end, p.start)
    return reverse_curve(p)


# ------------------------------------------------------------------ SVG

_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_LEN = re.compile(rf"^\s*({_NUM})\s*([a-z%]*)\s*$")
SHAPES = {"path", "rect", "circle", "ellipse", "line", "polygon", "polyline"}
CONTAINERS = {"g", "a", "switch"}
QUIET = {"title", "desc", "metadata", "defs", "symbol", "namedview", "style", "linearGradient", "radialGradient",
         "stop", "pattern", "marker", "filter", "font-face"}


def _tag(el: Any) -> str:
    t = el.tag if isinstance(el.tag, str) else ""
    return t.rsplit("}", 1)[-1]


def _attr(el: Any, name: str) -> str | None:
    v = el.get(name)
    if v is None:
        for k, val in el.attrib.items():
            if k.rsplit("}", 1)[-1] == name:
                return val
    return v


def _style(el: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (el.get("style") or "").split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip().lower()] = v.strip().lower()
    for k in ("display", "visibility", "opacity", "fill", "stroke", "fill-rule", "clip-path", "mask", "fill-opacity"):
        v = el.get(k)
        if v is not None:
            out.setdefault(k, v.strip().lower())
    return out


def _length(v: str | None, what: str) -> tuple[float, str] | None:
    if v is None:
        return None
    m = _LEN.match(v)
    if not m:
        raise ImportRefused(f"can't read the SVG {what} {v!r}")
    x = float(m.group(1))
    if not math.isfinite(x):
        raise ImportRefused(f"the SVG {what} {v!r} isn't finite")
    return x, m.group(2)


def _transform(text: str | None) -> Affine:
    m = IDENTITY
    if not text:
        return m
    for name, args in re.findall(r"(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^)]*)\)", text):
        a = [float(x) for x in re.findall(_NUM, args)]
        if name == "matrix" and len(a) == 6:
            t: Affine = (a[0], a[1], a[2], a[3], a[4], a[5])
        elif name == "translate" and a:
            t = (1, 0, 0, 1, a[0], a[1] if len(a) > 1 else 0.0)
        elif name == "scale" and a:
            t = (a[0], 0, 0, a[1] if len(a) > 1 else a[0], 0, 0)
        elif name == "rotate" and a:
            r = math.radians(a[0])
            t = (math.cos(r), math.sin(r), -math.sin(r), math.cos(r), 0, 0)
            if len(a) == 3:
                t = compose(compose((1, 0, 0, 1, a[1], a[2]), t), (1, 0, 0, 1, -a[1], -a[2]))
        elif name == "skewX" and a:
            t = (1, 0, math.tan(math.radians(a[0])), 1, 0, 0)
        elif name == "skewY" and a:
            t = (1, math.tan(math.radians(a[0])), 0, 1, 0, 0)
        else:
            raise ImportRefused(f"can't read the SVG transform {name}({args})")
        m = compose(m, t)
    if not all(math.isfinite(v) for v in m) or abs(_det(m)) < 1e-15:
        raise ImportRefused("an SVG transform that flattens or breaks the drawing")
    return m


def _num(el: Any, name: str, default: float | None = None) -> float:
    v = el.get(name)
    if v is None:
        if default is None:
            raise SourceError(f"a <{_tag(el)}> without its {name}")
        return default
    m = _LEN.match(v)
    if not m or m.group(2) not in ("", "px"):
        raise SourceError(f"a <{_tag(el)}> whose {name} isn't a plain number ({v!r})")
    x = float(m.group(1))
    if not math.isfinite(x):
        raise SourceError(f"a <{_tag(el)}> whose {name} isn't finite")
    return x


def _points(text: str | None) -> list[Point]:
    nums = [float(x) for x in re.findall(_NUM, text or "")]
    if not all(math.isfinite(x) for x in nums):
        raise SourceError("a <polygon> or <polyline> with a point that isn't finite")
    return [(nums[i], nums[i + 1]) for i in range(0, len(nums) - 1, 2)]


def _shape_subs(el: Any, tag: str, corrections: list[float]) -> list[Sub]:
    from .shapes import rounded_rect, transform  # noqa: PLC0415

    if tag == "path":
        return parse_path(el.get("d") or "", what="an SVG path", canvas=False, corrections=corrections,
                          strict_arcs=False, full=True, max_chars=MAX_IMPORT_PATH_CHARS)
    if tag == "rect":
        x, y, w, h = _num(el, "x", 0.0), _num(el, "y", 0.0), _num(el, "width"), _num(el, "height")
        rx, ry = el.get("rx"), el.get("ry")
        rxv = _num(el, "rx") if rx is not None else (_num(el, "ry") if ry is not None else 0.0)
        ryv = _num(el, "ry") if ry is not None else rxv
        if w <= 0 or h <= 0:
            return []
        rxv, ryv = min(rxv, w / 2), min(ryv, h / 2)
        if abs(rxv - ryv) <= 1e-12:
            return [Sub(transform(rounded_rect(w, h, rxv), offset=(x + w / 2, y + h / 2)), True)]
        # Elliptical corners: lines and quarter ellipses.
        pts = []
        cx0, cy0, cx1, cy1 = x + rxv, y + ryv, x + w - rxv, y + h - ryv
        pieces: list[Any] = [Prim((cx0, y), (cx1, y)),
                             AxisEllipse((cx1, cy0), (rxv, 0), (0, ryv), -math.pi / 2, math.pi / 2),
                             Prim((x + w, cy0), (x + w, cy1)),
                             AxisEllipse((cx1, cy1), (rxv, 0), (0, ryv), 0.0, math.pi / 2),
                             Prim((cx1, y + h), (cx0, y + h)),
                             AxisEllipse((cx0, cy1), (rxv, 0), (0, ryv), math.pi / 2, math.pi / 2),
                             Prim((x, cy1), (x, cy0)),
                             AxisEllipse((cx0, cy0), (rxv, 0), (0, ryv), math.pi, math.pi / 2)]
        del pts
        return [Sub([p for p in pieces if not isinstance(p, Prim) or math.dist(p.start, p.end) > 1e-12], True)]
    if tag == "circle":
        cx, cy, r = _num(el, "cx", 0.0), _num(el, "cy", 0.0), _num(el, "r")
        return [Sub([Prim((cx + r, cy), (cx + r, cy), (cx, cy), False, True)], True)] if r > 0 else []
    if tag == "ellipse":
        cx, cy, rx, ry = _num(el, "cx", 0.0), _num(el, "cy", 0.0), _num(el, "rx"), _num(el, "ry")
        if rx <= 0 or ry <= 0:
            return []
        return [Sub([AxisEllipse((cx, cy), (rx, 0.0), (0.0, ry), 0.0, math.tau)], True)]
    if tag == "line":
        a = (_num(el, "x1", 0.0), _num(el, "y1", 0.0))
        b = (_num(el, "x2", 0.0), _num(el, "y2", 0.0))
        return [Sub([Prim(a, b)], False)] if math.dist(a, b) > 1e-12 else []
    pts = _points(el.get("points"))
    if len(pts) < 2:
        return []
    closed = tag == "polygon"
    seq = pts + ([pts[0]] if closed else [])
    prims = [Prim(a, b) for a, b in zip(seq, seq[1:]) if math.dist(a, b) > 1e-12]
    return [Sub(prims, closed)] if prims else []


def parse_svg(data: bytes) -> Parsed:
    import defusedxml.ElementTree as ET  # noqa: PLC0415
    from defusedxml import DefusedXmlException  # noqa: PLC0415

    try:
        root = ET.fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except (DefusedXmlException, ET.ParseError, ValueError) as exc:
        raise ImportRefused(f"the SVG can't be read safely: {exc}") from None
    if _tag(root) != "svg":
        raise ImportRefused("this isn't an SVG (its root isn't <svg>)")
    out = Parsed([], None, format="svg")
    # Units: a physical width/height with a viewBox sets inches per user unit.
    vb = [float(x) for x in re.findall(_NUM, root.get("viewBox") or "")]
    if not all(math.isfinite(x) for x in vb):
        raise ImportRefused("the SVG's viewBox has a number that isn't finite")
    wl, hl = _length(root.get("width"), "width"), _length(root.get("height"), "height")
    k = None
    physical = ("in", "mm", "cm", "pt", "pc")
    ks = [dim[0] * UNITS_IN[dim[1]] / vb[idx] if len(vb) == 4 and vb[idx] > 0 else 1 / 96
          for dim, idx in ((wl, 2), (hl, 3)) if dim and dim[1] in physical and dim[0] > 0]
    if ks:
        k = ks[0]
    if len(ks) == 2 and abs(ks[0] - ks[1]) > 1e-6 * max(ks):
        # The viewBox has another shape than width x height: SVG fits it in (preserveAspectRatio, meet by
        # default), so the drawing is at the smaller scale, centred. "none" would stretch it, which a
        # uniform import can't do.
        par = (root.get("preserveAspectRatio") or "xMidYMid meet").split()
        if par and par[0] == "none":
            raise ImportRefused("the SVG stretches its drawing to a width and height of another shape "
                                "(preserveAspectRatio none); artwork is scaled uniformly, so fix the file's size or "
                                "viewBox")
        slice_ = len(par) > 1 and par[1] == "slice"
        k = max(ks) if slice_ else min(ks)
        out.notes.append(f"the SVG's viewBox isn't the shape of its width and height, so it's scaled as SVG "
                         f"{'slices' if slice_ else 'fits'} it ({'the larger' if slice_ else 'the smaller'} of the "
                         "two scales)" + ("; what the slice clips off is imported whole" if slice_ else ""))
    out.units_in = k
    if k is None:
        out.notes.append("the SVG has no physical size (px or no units), so the artwork needs a width, height or "
                         "fit_box")
    ids: dict[str, Any] = {}
    count = 0
    for el in root.iter():
        count += 1
        if count > MAX_ELEMENTS * 4:
            raise ImportRefused(f"the SVG has more than {MAX_ELEMENTS * 4} elements")
        i = el.get("id")
        if i:
            ids[i] = el
    unsupported: dict[str, int] = {}
    notes: list[str] = []
    corrections: list[float] = []
    drawn = 0
    visits = 0  # every element walked, with <use> expanded: a small file of nested uses can't run away

    def note(text: str) -> None:
        if text not in notes:
            notes.append(text)

    def skip(what: str) -> None:
        unsupported[what] = unsupported.get(what, 0) + 1

    def walk(el: Any, m: Affine, depth: int, hidden: bool, chain: tuple[str, ...]) -> None:
        nonlocal drawn, visits
        visits += 1
        if visits > MAX_WALKED_ELEMENTS:
            raise ImportRefused(f"the SVG has more than {MAX_WALKED_ELEMENTS} elements once its <use> references "
                                "are expanded")
        tag = _tag(el)
        st = _style(el)
        here = compose(m, _transform(el.get("transform")))
        if depth > MAX_DEPTH * 4:
            raise ImportRefused("the SVG nests too deeply")
        hid = hidden or st.get("display") == "none" or st.get("visibility") in ("hidden", "collapse") or \
            st.get("opacity") in ("0", "0.0")
        if st.get("clip-path") not in (None, "none") or st.get("mask") not in (None, "none"):
            skip("clipping or masks (ignored: everything under them is imported whole)")
        if tag in QUIET:
            if tag == "style":
                note("the SVG has a <style> sheet, which wasn't read: anything it hides or styles is imported as "
                     "drawn")
            return
        if tag in CONTAINERS or (tag == "svg" and depth == 0):
            for ch in el:
                walk(ch, here, depth + 1, hid, chain)
            return
        if tag == "use":
            href = _attr(el, "href") or ""
            if not href.startswith("#") or href[1:] not in ids:
                skip("a <use> of something outside the file (only local references are read)")
                return
            if href in chain or len(chain) > MAX_DEPTH:
                raise ImportRefused("a <use> refers to itself")
            ref = ids[href[1:]]
            t = compose(here, (1, 0, 0, 1, _num(el, "x", 0.0), _num(el, "y", 0.0)))
            if _tag(ref) == "symbol":
                for ch in ref:
                    walk(ch, t, depth + 1, hid, (*chain, href))
            else:
                walk(ref, t, depth + 1, hid, (*chain, href))
            return
        if tag not in SHAPES:
            skip(f"<{tag}>" + (" (text is never imported; convert it to outlines, or use stencil_text)"
                               if tag in ("text", "tspan", "textPath") else ""))
            return
        if hid:
            skip("hidden elements (display none, visibility hidden or opacity 0): not imported")
            return
        try:
            subs = _shape_subs(el, tag, corrections)
        except SourceError as exc:
            skip(f"an unreadable <{tag}> ({exc})")
            return
        if not subs:
            return
        drawn += 1
        if drawn > MAX_ELEMENTS:
            raise ImportRefused(f"the SVG draws more than {MAX_ELEMENTS} shapes")
        fill, stroke = st.get("fill"), st.get("stroke")
        if fill == "none" and stroke not in (None, "none"):
            note("stroke-only shapes are imported as their centre lines, not their stroke's outline")
        nonzero = (fill or "black") != "none" and len(subs) > 1 and st.get("fill-rule", "nonzero") == "nonzero"
        if nonzero and sum(1 for s in subs if s.closed) > MAX_NESTED_COMPARE:
            note("a filled path has too many outlines to compare which way each runs (fill-rule nonzero), so the "
                 "file may show solid what is cut: check the retained and discarded result")
        elif nonzero and _same_way_nested(subs):
            note("a filled path's inner outline runs the same way as its outer one (fill-rule nonzero), so the "
                 "file shows it solid, but both outlines are cut: the inside comes out or stays as an island")
        flip = (1.0, 0.0, 0.0, -1.0, 0.0, 0.0)  # y down to y up
        mm = compose(flip, here)
        for s in subs:
            pieces = [map_piece(p, mm) for p in s.pieces]
            out.subs.append(Sub(pieces, s.closed, None, f"<{tag}>{' #' + el.get('id') if el.get('id') else ''}"))

    walk(root, IDENTITY, 0, False, ())
    if corrections:
        note(f"{len(corrections)} arc{'s' if len(corrections) > 1 else ''} had radii too small for their ends and "
             f"were enlarged (up to {max(corrections) * 100:.2f}%) as SVG does")
    out.unsupported = [f"{n} x {k}" if n > 1 else k for k, n in unsupported.items()]
    out.notes += notes
    if not out.subs:
        raise ImportRefused("the SVG draws no outlines this importer reads (path, rect, circle, ellipse, line, "
                            "polygon, polyline, use)" + (f"; it has {', '.join(out.unsupported)}" if out.unsupported
                                                         else ""))
    return out


def _same_way_nested(subs: list[Sub]) -> bool:
    """Two closed subpaths, one inside the other, wound the same way (rough: polygons of samples)."""
    polys = []
    for s in subs:
        if not s.closed:
            continue
        pts = []
        for p in s.pieces:
            pts += [p.start] if isinstance(p, Prim) and p.center is None else (
                [p.point(i / 8) for i in range(8)] if not isinstance(p, Prim) else
                _prim_points(p))
        if len(pts) >= 3:
            polys.append(pts)
    for i, a in enumerate(polys):
        for j, b in enumerate(polys):
            if i != j and _inside(b[0], a) and (_area(a) > 0) == (_area(b) > 0):
                return True
    return False


def _prim_points(p: Prim) -> list[Point]:
    from .curves import prim_points  # noqa: PLC0415

    return prim_points(p, 8)[:-1]


def _area(pts: list[Point]) -> float:
    return sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(pts, pts[1:] + pts[:1])) / 2


def _inside(p: Point, poly: list[Point]) -> bool:
    c = False
    for a, b in zip(poly, poly[1:] + poly[:1]):
        if (a[1] > p[1]) != (b[1] > p[1]) and p[0] < a[0] + (p[1] - a[1]) * (b[0] - a[0]) / (b[1] - a[1]):
            c = not c
    return c


# ------------------------------------------------------------------ DXF

INSUNITS_IN = {1: 1.0, 2: 12.0, 4: 1 / 25.4, 5: 1 / 2.54, 6: 1000 / 25.4}
READ = {"LINE", "ARC", "CIRCLE", "LWPOLYLINE", "POLYLINE", "SPLINE", "ELLIPSE", "INSERT"}
SILENT = {"VERTEX", "SEQEND", "ENDBLK", "ATTRIB", "ATTDEF"}


def _float(v: str, what: str) -> float:
    """A DXF value as a finite number; anything else refuses the import."""
    try:
        x = float(v)
    except ValueError:
        raise ImportRefused(f"the DXF has {what} that isn't a number ({v[:20]!r})") from None
    if not math.isfinite(x):
        raise ImportRefused(f"the DXF has {what} that isn't finite ({v[:20]!r})")
    return x


def _int(v: str | None, default: int, what: str) -> int:
    if v is None or not v.strip():
        return default
    try:
        return int(v)
    except ValueError:
        raise ImportRefused(f"the DXF has {what} that isn't a whole number ({v[:20]!r})") from None


def _pairs(text: str) -> list[tuple[int, str]]:
    lines = text.splitlines()
    out = []
    for i in range(0, len(lines) - 1, 2):
        try:
            out.append((int(lines[i].strip()), lines[i + 1].strip()))
        except ValueError:
            raise ImportRefused(f"the DXF can't be read near line {i + 1}") from None
    return out


@dataclass(slots=True)
class _Ent:
    type: str
    codes: list[tuple[int, str]]
    vertices: list["_Ent"] = field(default_factory=list)

    def get(self, code: int, default: str | None = None) -> str | None:
        for c, v in self.codes:
            if c == code:
                return v
        return default

    def f(self, code: int, default: float | None = None) -> float:
        v = self.get(code)
        if v is None:
            if default is None:
                raise SourceError(f"a {self.type} without group {code}")
            return default
        return _float(v, f"a {self.type} value (group {code})")

    def i(self, code: int, default: int) -> int:
        return _int(self.get(code), default, f"a {self.type} value (group {code})")

    def all(self, code: int) -> list[float]:
        return [_float(v, f"a {self.type} value (group {code})") for c, v in self.codes if c == code]


def _entities(pairs: list[tuple[int, str]], start: int, stop_at: str) -> tuple[list[_Ent], int]:
    """Entities from pairs[start] (at a 0 code) until a 0 code `stop_at` (ENDSEC or ENDBLK)."""
    out: list[_Ent] = []
    i = start
    cur: _Ent | None = None
    poly: _Ent | None = None
    while i < len(pairs):
        c, v = pairs[i]
        if c == 0:
            if cur is not None:
                if cur.type == "VERTEX" and poly is not None:
                    poly.vertices.append(cur)
                elif cur.type == "SEQEND":
                    poly = None
                else:
                    out.append(cur)
                    if cur.type == "POLYLINE":
                        poly = cur
                cur = None
            if v == stop_at or v in ("ENDSEC", "EOF"):
                return out, i
            cur = _Ent(v, [])
        elif cur is not None:
            cur.codes.append((c, v))
        i += 1
    return out, i


def parse_dxf(data: bytes) -> Parsed:
    if data.startswith(b"AutoCAD Binary DXF"):
        raise ImportRefused("this is a binary DXF; save it as an ASCII DXF (any version) to import it")
    text = data.decode("utf-8", errors="replace")
    pairs = _pairs(text)
    out = Parsed([], None, format="dxf")
    insunits = None
    layer_colour: dict[str, int] = {}
    blocks: dict[str, tuple[Point, list[_Ent]]] = {}
    entities: list[_Ent] = []
    i = 0
    section = None
    while i < len(pairs):
        c, v = pairs[i]
        if c == 0 and v == "SECTION" and i + 1 < len(pairs):
            section = pairs[i + 1][1]
            i += 2
            continue
        if c == 0 and v == "ENDSEC":
            section = None
        if section == "HEADER" and c == 9 and v == "$INSUNITS" and i + 1 < len(pairs):
            try:
                insunits = int(pairs[i + 1][1])
            except ValueError:
                pass
        if section == "TABLES" and c == 0 and v == "LAYER":
            name, colour = None, None
            j = i + 1
            while j < len(pairs) and pairs[j][0] != 0:
                if pairs[j][0] == 2:
                    name = pairs[j][1].casefold()
                elif pairs[j][0] == 62:
                    try:
                        colour = int(pairs[j][1])
                    except ValueError:
                        pass
                j += 1
            if name is not None and colour is not None:
                layer_colour[name] = colour
        if section == "BLOCKS" and c == 0 and v == "BLOCK":
            j = i + 1
            name, base = None, (0.0, 0.0)
            bx = by = 0.0
            while j < len(pairs) and pairs[j][0] != 0:
                if pairs[j][0] == 2:
                    name = pairs[j][1]
                elif pairs[j][0] == 10:
                    bx = _float(pairs[j][1], "a BLOCK base point")
                elif pairs[j][0] == 20:
                    by = _float(pairs[j][1], "a BLOCK base point")
                j += 1
            base = (bx, by)
            ents, j = _entities(pairs, j, "ENDBLK")
            if name is not None:
                blocks[name] = (base, ents)
            i = j
            continue
        if section == "ENTITIES" and c == 0 and v not in ("SECTION",):
            entities, i = _entities(pairs, i, "ENDSEC")
            section = None
            continue
        i += 1
    if insunits in INSUNITS_IN:
        out.units_in = INSUNITS_IN[insunits]
    else:
        out.notes.append("the DXF doesn't say its units ($INSUNITS), so the artwork needs a width, height or fit_box"
                         + (f" (code {insunits} isn't one this importer reads)" if insunits not in (None, 0) else ""))
    unsupported: dict[str, int] = {}
    out_of_plane: list[str] = []
    count = 0
    pieces: list[tuple[Any, str | None]] = []  # (piece or closed Sub, rule)

    def colour_rule(e: _Ent, inherited: int | None) -> tuple[str | None, bool]:
        """(rule from its colour, whether it's on a layer that's off)."""
        col = e.i(62, 256)
        lay = (e.get(8, "0") or "0").casefold()
        off = layer_colour.get(lay, 7) < 0
        if col == 0 and inherited is not None:  # BYBLOCK
            col = inherited
        if col == 256:
            col = abs(layer_colour.get(lay, 7))
        return {1: "cut", 2: "crease"}.get(abs(col)), off

    def ocs(e: _Ent) -> Affine:
        nx, ny, nz = e.f(210, 0.0), e.f(220, 0.0), e.f(230, 1.0)
        if abs(nx) > 1e-9 or abs(ny) > 1e-9 or abs(abs(nz) - 1) > 1e-9:
            out_of_plane.append(f"a {e.type} whose extrusion isn't along Z")
            return IDENTITY
        return IDENTITY if nz > 0 else (-1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

    def zcheck(e: _Ent, *codes: int) -> None:
        for code in codes:
            for z in e.all(code):
                if abs(z) > 1e-9:
                    out_of_plane.append(f"a {e.type} off the drawing plane (z {z:g})")
                    return

    def emit(p: Any, m: Affine, rule: str | None, closed: bool = False) -> None:
        mp = map_piece(p, m) if not isinstance(p, Sub) else Sub([map_piece(q, m) for q in p.pieces], True)
        if closed and not isinstance(mp, Sub):
            mp = Sub([mp], True)
        pieces.append((mp, rule))

    def bulge_piece(a: Point, b: Point, bulge: float) -> Any:
        if abs(bulge) < 1e-12:
            return Prim(a, b)
        theta = 4 * math.atan(bulge)
        chord = math.dist(a, b)
        r = chord / (2 * math.sin(abs(theta) / 2))
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        h = math.sqrt(max(r * r - (chord / 2) ** 2, 0.0))
        dx, dy = (b[0] - a[0]) / chord, (b[1] - a[1]) / chord
        side = 1.0 if (bulge > 0) == (abs(theta) < math.pi) else -1.0
        c = (mid[0] - dy * h * side, mid[1] + dx * h * side)
        return Prim(a, b, c, bulge < 0)

    def poly(e: _Ent, verts: list[tuple[Point, float]], closed: bool, m: Affine, rule: str | None) -> None:
        seq = list(verts)
        segs: list[Any] = []
        n = len(seq)
        for k in range(n if closed else n - 1):
            (a, bu), (b, _) = seq[k], seq[(k + 1) % n]
            if math.dist(a, b) > 1e-12:
                segs.append(bulge_piece(a, b, bu))
        if segs:
            emit(Sub(segs, closed), m, rule) if closed else [emit(s, m, rule) for s in segs]

    def handle(e: _Ent, m: Affine, depth: int, inherited: int | None) -> None:
        nonlocal count
        count += 1
        if count > MAX_ENTITIES:
            raise ImportRefused(f"the DXF has more than {MAX_ENTITIES} entities (blocks expanded)")
        t = e.type
        if t in SILENT:
            return
        if t not in READ:
            unsupported[t] = unsupported.get(t, 0) + 1
            return
        rule, off = colour_rule(e, inherited)
        if off:
            unsupported["entities on layers that are off (not imported)"] = \
                unsupported.get("entities on layers that are off (not imported)", 0) + 1
            return
        try:
            if t == "LINE":
                zcheck(e, 30, 31)
                a, b = (e.f(10), e.f(20)), (e.f(11), e.f(21))
                if math.dist(a, b) > 1e-12:
                    emit(Prim(a, b), m, rule)
            elif t in ("ARC", "CIRCLE"):
                zcheck(e, 30, 38)
                mm = compose(m, ocs(e))
                c, r = (e.f(10), e.f(20)), e.f(40)
                if r <= 0:
                    raise SourceError(f"a {t} with no radius")
                if t == "CIRCLE":
                    emit(Prim((c[0] + r, c[1]), (c[0] + r, c[1]), c, False, True), mm, rule, closed=True)
                else:
                    a0, a1 = math.radians(e.f(50)), math.radians(e.f(51))
                    s = (c[0] + r * math.cos(a0), c[1] + r * math.sin(a0))
                    en = (c[0] + r * math.cos(a1), c[1] + r * math.sin(a1))
                    if math.dist(s, en) <= 1e-12:
                        emit(Prim(s, s, c, False, True), mm, rule, closed=True)
                    else:
                        emit(Prim(s, en, c, False), mm, rule)
            elif t == "LWPOLYLINE":
                zcheck(e, 38)
                mm = compose(m, ocs(e))
                verts: list[tuple[Point, float]] = []
                x = None
                for code, val in e.codes:
                    if code == 10:
                        x = _float(val, "an LWPOLYLINE vertex")
                    elif code == 20 and x is not None:
                        verts.append(((x, _float(val, "an LWPOLYLINE vertex")), 0.0))
                    elif code == 42 and verts:
                        verts[-1] = (verts[-1][0], _float(val, "an LWPOLYLINE bulge"))
                closed = e.i(70, 0) & 1 == 1
                poly(e, verts, closed, mm, rule)
            elif t == "POLYLINE":
                flags = e.i(70, 0)
                if flags & (8 | 16 | 64):
                    out_of_plane.append("a 3D POLYLINE or mesh")
                    return
                zcheck(e, 30)
                mm = compose(m, ocs(e))
                verts = []
                for v in e.vertices:
                    zcheck(v, 30)
                    verts.append(((v.f(10), v.f(20)), v.f(42, 0.0)))
                poly(e, verts, flags & 1 == 1, mm, rule)
            elif t == "SPLINE":
                zcheck(e, 30)
                nz = e.f(230, 1.0)
                if abs(e.f(210, 0.0)) > 1e-9 or abs(e.f(220, 0.0)) > 1e-9 or abs(abs(nz) - 1) > 1e-9:
                    out_of_plane.append("a SPLINE whose plane isn't the drawing's")
                    return
                deg = e.i(71, 3)
                knots = e.all(40)
                weights = e.all(41)
                xs, ys = e.all(10), e.all(20)
                ctrl = list(zip(xs, ys))
                if not ctrl:
                    unsupported["SPLINE given only by fit points"] = unsupported.get("SPLINE given only by fit "
                                                                                     "points", 0) + 1
                    return
                try:
                    check_nurbs(deg, knots, ctrl, weights)
                except CurveError as exc:
                    raise SourceError(str(exc)) from None
                curve = Nurbs(deg, tuple(knots), tuple(ctrl), tuple(weights))
                closed = e.i(70, 0) & 1 == 1 and math.dist(curve.point(0), curve.point(1)) < 1e-9
                emit(Sub([curve], True) if closed else curve, m, rule)
            elif t == "ELLIPSE":
                zcheck(e, 30, 31)
                nz = e.f(230, 1.0)
                if abs(e.f(210, 0.0)) > 1e-9 or abs(e.f(220, 0.0)) > 1e-9 or abs(abs(nz) - 1) > 1e-9:
                    out_of_plane.append("an ELLIPSE whose plane isn't the drawing's")
                    return
                c = (e.f(10), e.f(20))
                u = (e.f(11), e.f(21))
                ratio = e.f(40)
                if not 1e-9 < ratio <= 1 + 1e-9 or math.hypot(*u) <= 1e-12:
                    raise SourceError("an ELLIPSE with no size (its axis or ratio is zero, or the ratio isn't "
                                      "between 0 and 1)")
                v = (-u[1] * ratio, u[0] * ratio) if nz > 0 else (u[1] * ratio, -u[0] * ratio)
                t0, t1 = e.f(41, 0.0), e.f(42, math.tau)
                span = (t1 - t0) % math.tau or math.tau
                curve = AxisEllipse(c, u, v, t0, span)
                full = abs(span - math.tau) < 1e-9
                emit(Sub([curve], True) if full else curve, m, rule)
            elif t == "INSERT":
                if depth >= MAX_DEPTH:
                    raise ImportRefused("blocks nest more than 8 deep")
                if e.i(70, 1) > 1 or e.i(71, 1) > 1:
                    unsupported["arrayed INSERT (rows or columns)"] = unsupported.get("arrayed INSERT (rows or "
                                                                                      "columns)", 0) + 1
                    return
                zcheck(e, 30)
                name = e.get(2) or ""
                if name not in blocks:
                    unsupported[f"INSERT of a missing block {name!r}"] = 1
                    return
                base, ents = blocks[name]
                sx, sy = e.f(41, 1.0), e.f(42, 1.0)
                if sx == 0 or sy == 0:
                    raise SourceError("an INSERT scaled to nothing")
                r = math.radians(e.f(50, 0.0))
                ins = (e.f(10, 0.0), e.f(20, 0.0))
                t_ins: Affine = compose(
                    compose((math.cos(r), math.sin(r), -math.sin(r), math.cos(r), ins[0], ins[1]),
                            (sx, 0.0, 0.0, sy, 0.0, 0.0)),
                    (1.0, 0.0, 0.0, 1.0, -base[0], -base[1]))
                mm = compose(compose(m, ocs(e)), t_ins)
                col = e.i(62, 256)
                for ch in ents:
                    handle(ch, mm, depth + 1, col if col not in (0, 256) else inherited)
        except ImportRefused:
            raise  # a limit, or a file that can't be read: never listed and skipped
        except (SourceError, ValueError) as exc:
            key = f"an unreadable {t} ({exc})"
            unsupported[key] = unsupported.get(key, 0) + 1

    for e in entities:
        handle(e, IDENTITY, 0, None)
    if out_of_plane:
        listed = "; ".join(sorted(set(out_of_plane)))
        raise ImportRefused(f"the DXF has geometry out of the drawing plane ({listed}); flatten it to 2D first")
    out.unsupported = [f"{n} x {k}" if n > 1 else k for k, n in unsupported.items()]
    out.subs = chain(pieces)
    if not out.subs:
        raise ImportRefused("the DXF draws no lines, arcs, circles, polylines, splines or ellipses"
                            + (f"; it has {', '.join(out.unsupported)}" if out.unsupported else ""))
    return out


def _ends(p: Any) -> tuple[Point, Point]:
    if isinstance(p, Prim):
        return p.start, p.end
    return p.point(0.0), p.point(1.0)


def chain(pieces: list[tuple[Any, str | None]], tol: float | None = None) -> list[Sub]:
    """Separate open pieces (a DXF's lines and arcs) joined end to end into paths where exactly two
    meet. Ends must coincide within tol (default: a millionth of the drawing). Nothing is closed
    unless its ends meet."""
    subs: list[Sub] = [Sub(p.pieces, True, r) for p, r in pieces if isinstance(p, Sub)]
    open_ = [(p, r) for p, r in pieces if not isinstance(p, Sub)]
    if not open_:
        return subs
    pts = [q for p, _ in open_ for q in _ends(p)]
    span = max(max(p[0] for p in pts) - min(p[0] for p in pts), max(p[1] for p in pts) - min(p[1] for p in pts), 1e-9)
    tol = tol if tol is not None else span * 1e-7
    cell = max(tol * 4, 1e-12)
    grid: dict[tuple[int, int], list[tuple[int, int]]] = {}
    ends = []
    for i, (p, _) in enumerate(open_):
        for w, q in enumerate(_ends(p)):
            ends.append(q)
            grid.setdefault((math.floor(q[0] / cell), math.floor(q[1] / cell)), []).append((i, w))

    def at(q: Point) -> list[tuple[int, int]]:
        gx, gy = math.floor(q[0] / cell), math.floor(q[1] / cell)
        found = []
        for ox in (-1, 0, 1):
            for oy in (-1, 0, 1):
                for i, w in grid.get((gx + ox, gy + oy), ()):
                    if math.dist(_ends(open_[i][0])[w], q) <= tol:
                        found.append((i, w))
        return found

    used = [False] * len(open_)

    def walk(i: int, forward: bool) -> list[Any]:
        """From piece i, onward through its end (forward) or its start."""
        seq = []
        cur, fwd = i, forward
        while True:
            p = open_[cur][0]
            q = _ends(p)[1 if fwd else 0]
            same = [(j, w) for j, w in at(q) if open_[j][1] == open_[i][1]]  # a crease ending on a cut is no join
            nxt = [(j, w) for j, w in same if j != cur]
            if len(same) != 2 or len(nxt) != 1 or used[nxt[0][0]]:
                return seq
            j, w = nxt[0]
            used[j] = True
            piece = open_[j][0]
            seq.append(piece if w == 0 else reverse_piece(piece))
            cur, fwd = j, w == 0

    for i, (p, rule) in enumerate(open_):
        if used[i]:
            continue
        used[i] = True
        after = walk(i, True)
        before = walk(i, False)
        seq = [reverse_piece(q) for q in reversed(before)] + [p] + after
        a, b = _ends(seq[0])[0], _ends(seq[-1])[1]
        subs.append(Sub(seq, math.dist(a, b) <= tol and len(seq) > 1, rule))
    return subs


# ------------------------------------------------------------------ stored geometry


def to_json(subs: list[Sub]) -> list[dict[str, Any]]:
    out = []
    for s in subs:
        pieces = []
        for p in s.pieces:
            r = lambda v: round(v, 9)  # noqa: E731
            if isinstance(p, Prim):
                if p.center is None:
                    pieces.append({"l": [r(p.start[0]), r(p.start[1]), r(p.end[0]), r(p.end[1])]})
                else:
                    pieces.append({"a": [r(p.start[0]), r(p.start[1]), r(p.end[0]), r(p.end[1]), r(p.center[0]),
                                         r(p.center[1]), int(p.clockwise), int(p.full)]})
            elif isinstance(p, Bezier):
                pieces.append({"b": [[r(x), r(y)] for x, y in p.pts]})
            elif isinstance(p, AxisEllipse):
                pieces.append({"e": [r(p.c[0]), r(p.c[1]), r(p.u[0]), r(p.u[1]), r(p.v[0]), r(p.v[1]), r(p.th0),
                                     r(p.dth)]})
            elif isinstance(p, Nurbs):
                pieces.append({"n": {"d": p.degree, "k": [r(k) for k in p.knots],
                                     "c": [[r(x), r(y)] for x, y in p.ctrl], "w": [r(w) for w in p.weights]}})
            else:
                raise SourceError(f"can't store a {type(p).__name__}")
        item: dict[str, Any] = {"closed": s.closed, "pieces": pieces}
        if s.rule:
            item["rule"] = s.rule
        out.append(item)
    return out


def from_json(raw: Any) -> list[Sub]:
    """The stored geometry, checked: it comes from a job record or the UI, so nothing in it is trusted."""
    if not isinstance(raw, list) or len(raw) > 5000:
        raise SourceError("imported geometry must be a list of outlines")
    out = []
    total = 0

    def nums(v: Any, n: int) -> list[float]:
        if not isinstance(v, list) or len(v) != n or not all(
                isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in v):
            raise SourceError("imported geometry has a malformed piece")
        return [float(x) for x in v]

    def pts(v: Any, lo: int, hi: int) -> tuple[Point, ...]:
        if not isinstance(v, list) or not lo <= len(v) <= hi:
            raise SourceError("imported geometry has a malformed piece")
        return tuple(tuple(nums(q, 2)) for q in v)  # type: ignore[misc]

    for s in raw:
        if not isinstance(s, dict) or not isinstance(s.get("pieces"), list):
            raise SourceError("imported geometry has a malformed outline")
        pieces: list[Any] = []
        for p in s["pieces"]:
            total += 1
            if total > 50000:
                raise SourceError("imported geometry is too large")
            if not isinstance(p, dict) or len(p) != 1:
                raise SourceError("imported geometry has a malformed piece")
            (k, v), = p.items()
            if k == "l":
                a = nums(v, 4)
                pieces.append(Prim((a[0], a[1]), (a[2], a[3])))
            elif k == "a":
                a = nums(v, 8)
                pieces.append(Prim((a[0], a[1]), (a[2], a[3]), (a[4], a[5]), bool(a[6]), bool(a[7])))
            elif k == "b":
                pieces.append(Bezier(pts(v, 3, 4)))
            elif k == "e":
                a = nums(v, 8)
                pieces.append(AxisEllipse((a[0], a[1]), (a[2], a[3]), (a[4], a[5]), a[6], a[7]))
            elif k == "n":
                if not isinstance(v, dict) or not isinstance(v.get("d"), int):
                    raise SourceError("imported geometry has a malformed spline")
                ctrl = pts(v.get("c"), 2, 5000)
                knots = nums(v.get("k"), len(v.get("k") or []))
                w = nums(v.get("w") or [], len(v.get("w") or []))
                try:
                    check_nurbs(v["d"], knots, list(ctrl), w)
                except CurveError as exc:
                    raise SourceError(str(exc)) from None
                pieces.append(Nurbs(v["d"], tuple(knots), ctrl, tuple(w)))
            else:
                raise SourceError("imported geometry has a piece of an unknown kind")
        rule = s.get("rule")
        if rule not in (None, "cut", "crease"):
            raise SourceError("imported geometry has an unknown rule")
        if pieces:
            out.append(Sub(pieces, bool(s.get("closed")), rule))
    return out

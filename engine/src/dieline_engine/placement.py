"""Placement: putting anything with a local frame on named faces
(docs/DESIGN-SYSTEM.md, "Placement").

One schema for every feature and, in phase 4, every piece of artwork:

    on: <selector>
    datum: assembled | material | usable          # what "centred" means
    x: "centred" | {from: <reference>, to: near_edge | far_edge | centre, distance}
    y: the same
    pattern: {kind: single} | {kind: row | column, count, pitch}
           | {kind: grid, cols, rows, pitch_x, pitch_y} | {kind: spread, axis, count, inset}
    rotation_deg: 0
    mirror: none | position_only | position_and_shape
    pairs: [[P1, P3], ...]

`resolve(face_map, placement, box, env)` takes the thing's bounding box in its
own frame (relative to its datum, after rotation) and returns one `Copy` per
placed instance: the face, its datum position in the face frame, its rotation
and whether its shape is flipped. Nothing here draws.

Rules kept here:
- Every distance is relative to a named reference on a named face; there are no
  blank coordinates. x references are vertical lines, y references horizontal.
- From an edge, distance runs inward. From a centreline it's signed (+ toward
  +x or +y); near_edge is the side facing the centreline; 0 needs to: centre.
- `to` is required whenever an axis isn't centred: there's no default.
- x and y place the whole pattern's bounding box. spread owns its axis.
- position_only mirroring is the default for opposite faces: the placement is
  mirrored left for right on the second face of each pair; shapes aren't
  flipped unless position_and_shape is asked for.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .expressions import Env, ExpressionError, evaluate
from .faces import Face, FaceMap, opposite_pairs

TOL = 1e-6
TO = ("near_edge", "far_edge", "centre")
DATUMS = ("assembled", "material", "usable")
MIRRORS = ("none", "position_only", "position_and_shape")
PATTERNS = ("single", "row", "column", "grid", "spread")
MAX_COPIES = 400  # per feature, all faces together
Box = tuple[float, float, float, float]  # x0, y0, x1, y1 around the datum


class PlacementError(ValueError):
    pass


# ------------------------------------------------------------------ input


def normalize(raw: Any, *, what: str = "placement", axes_optional: bool = False) -> dict[str, Any]:
    """Check a placement's shape (not its values, which need the part) and fill
    the defaults. Raises PlacementError with what to change."""
    if not isinstance(raw, dict):
        raise PlacementError(f"{what} must be an object with on, x and y")
    known = {"on", "datum", "x", "y", "pattern", "rotation_deg", "mirror", "pairs"}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise PlacementError(f"{what} has no field(s) {', '.join(unknown)}; fields: {', '.join(sorted(known))}")
    on = raw.get("on")
    if isinstance(on, str):
        on_v: Any = on
    elif isinstance(on, list) and on and all(isinstance(x, str) for x in on):
        on_v = list(on)
    else:
        raise PlacementError(f"{what}.on must be a face id or selector (P2, end_panels, top_flaps, ...) or a list")
    datum = raw.get("datum", "assembled")
    if datum not in DATUMS:
        raise PlacementError(f"{what}.datum must be one of {', '.join(DATUMS)}")
    pattern = _pattern(raw.get("pattern"), what)
    out: dict[str, Any] = {"on": on_v, "datum": datum, "pattern": pattern}
    spread_axis = pattern.get("axis") if pattern["kind"] == "spread" else None
    for axis in ("x", "y"):
        v = raw.get(axis)
        if axis == spread_axis:
            if v is not None:
                raise PlacementError(f"{what}: a spread along {axis} places its own {axis}; leave {axis} out")
            continue
        if v is None:
            if axes_optional:
                continue
            raise PlacementError(f"{what}.{axis} is required: \"centred\" or {{from, to, distance}}")
        out[axis] = _axis(v, f"{what}.{axis}")
    rot = raw.get("rotation_deg", 0)
    if isinstance(rot, bool) or not isinstance(rot, (int, float)) or not math.isfinite(rot):
        raise PlacementError(f"{what}.rotation_deg must be a number of degrees")
    out["rotation_deg"] = float(rot) % 360.0
    mirror = raw.get("mirror")
    if mirror is not None and mirror not in MIRRORS:
        raise PlacementError(f"{what}.mirror must be one of {', '.join(MIRRORS)}")
    if mirror is not None:
        out["mirror"] = mirror
    pairs = raw.get("pairs")
    if pairs is not None:
        if not (isinstance(pairs, list) and all(isinstance(p, list) and len(p) == 2 and all(isinstance(x, str)
                                                                                           for x in p) for p in pairs)):
            raise PlacementError(f"{what}.pairs must be a list of [face, face] pairs, e.g. [[\"P1\", \"P3\"]]")
        out["pairs"] = [list(p) for p in pairs]
    return out


def _axis(v: Any, what: str) -> Any:
    if v in ("centred", "centered", "center", "centre"):
        return "centred"
    if not isinstance(v, dict):
        raise PlacementError(f"{what} must be \"centred\" or {{from, to, distance}}")
    unknown = sorted(set(v) - {"from", "to", "distance"})
    if unknown:
        raise PlacementError(f"{what} has no field(s) {', '.join(unknown)}")
    ref = v.get("from")
    if not isinstance(ref, str) or not ref:
        raise PlacementError(f"{what}.from must name a reference line (top_score, left, centre_x, free_edge, ...)")
    to = v.get("to")
    if to is None:
        raise PlacementError(f"{what}.to is required: does the distance run to the feature's near_edge, its "
                             "far_edge or its centre? Ask the user if they didn't say")
    if to not in TO:
        raise PlacementError(f"{what}.to must be one of {', '.join(TO)}")
    d = v.get("distance")
    if d is None or isinstance(d, bool) or not isinstance(d, (int, float, str)):
        raise PlacementError(f"{what}.distance is required: a number, a fraction like '2 3/4' or an expression")
    return {"from": ref, "to": to, "distance": d}


def _count(v: Any, what: str, lo: int = 1) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < lo or v > MAX_COPIES:
        raise PlacementError(f"{what} must be a whole number from {lo} to {MAX_COPIES}")
    return v


def _pattern(raw: Any, what: str) -> dict[str, Any]:
    if raw is None:
        return {"kind": "single"}
    if not isinstance(raw, dict) or raw.get("kind") not in PATTERNS:
        raise PlacementError(f"{what}.pattern.kind must be one of {', '.join(PATTERNS)}")
    k = raw["kind"]
    fields = {"single": set(), "row": {"count", "pitch"}, "column": {"count", "pitch"},
              "grid": {"cols", "rows", "pitch_x", "pitch_y"}, "spread": {"axis", "count", "inset"}}[k]
    unknown = sorted(set(raw) - fields - {"kind"})
    if unknown:
        raise PlacementError(f"{what}.pattern ({k}) has no field(s) {', '.join(unknown)}; it takes "
                             f"{', '.join(sorted(fields)) or 'nothing else'}")
    out: dict[str, Any] = {"kind": k}
    if k in ("row", "column"):
        out["count"] = _count(raw.get("count"), f"{what}.pattern.count")
        out["pitch"] = _length_in(raw.get("pitch"), f"{what}.pattern.pitch")
    elif k == "grid":
        out["cols"] = _count(raw.get("cols"), f"{what}.pattern.cols")
        out["rows"] = _count(raw.get("rows"), f"{what}.pattern.rows")
        if out["cols"] * out["rows"] > MAX_COPIES:
            raise PlacementError(f"{what}.pattern: more than {MAX_COPIES} copies")
        out["pitch_x"] = _length_in(raw.get("pitch_x"), f"{what}.pattern.pitch_x")
        out["pitch_y"] = _length_in(raw.get("pitch_y"), f"{what}.pattern.pitch_y")
    elif k == "spread":
        if raw.get("axis") not in ("x", "y"):
            raise PlacementError(f"{what}.pattern.axis must be x or y")
        out["axis"] = raw["axis"]
        out["count"] = _count(raw.get("count"), f"{what}.pattern.count")
        out["inset"] = _length_in(raw.get("inset"), f"{what}.pattern.inset")
    return out


def _length_in(v: Any, what: str) -> Any:
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise PlacementError(f"{what} is required: a length (a number, '1 1/2' or an expression)")
    return v


# ------------------------------------------------------------------ output


@dataclass(slots=True)
class Callout:
    """A measured distance, for the picture: from a reference to the feature, in the face frame."""

    axis: str  # x | y
    ref: str  # the reference line's name
    at: float  # the reference's coordinate along the axis
    to_coord: float  # where the measured point of the feature lands
    to: str  # near_edge | far_edge | centre
    distance: float
    text: str  # the distance as given


@dataclass(slots=True)
class Copy:
    face: str
    index: int  # 1-based, on this face
    of: int  # copies on this face
    x: float  # datum position, face frame
    y: float
    rotation_deg: float = 0.0
    flip: bool = False  # the shape itself is mirrored (position_and_shape on a second face)
    mirrored_from: str | None = None
    callouts: list[Callout] = field(default_factory=list)

    def label(self, what: str) -> str:
        return f"{what} {self.index} of {self.of} on {self.face}" if self.of > 1 else f"{what} on {self.face}"


def _ref(f: Face, name: str, axis: str, datum: str, where: str):
    """(coordinate along the axis, inward sign or 0 for a centreline, the line's name)."""
    if "." in name:
        fid, _, name = name.partition(".")
        if fid != f.id:
            raise PlacementError(f"{where}: reference {fid}.{name} is on {fid}, not on {f.id}")
    ln = f.lines.get(name)
    if ln is None:
        listed = ", ".join(sorted(f.lines))
        raise PlacementError(f"{where}: {f.id} has no reference line {name!r}; it has {listed}")
    vertical = abs(ln.start[0] - ln.end[0]) <= TOL
    horizontal = abs(ln.start[1] - ln.end[1]) <= TOL
    if axis == "x" and not vertical:
        raise PlacementError(f"{where}: {name} is a horizontal line on {f.id}, so it can only position y")
    if axis == "y" and not horizontal:
        raise PlacementError(f"{where}: {name} is a vertical line on {f.id}, so it can only position x")
    i = 0 if axis == "x" else 1
    c = ln.start[i]
    if ln.inward is None:  # a centreline: follows the datum
        if name == "centre_x":
            c = centre(f, "x", datum)
        elif name == "centre_y":
            c = centre(f, "y", datum)
        return c, 0, name
    s = 1 if ln.inward[i] > 0 else -1
    return c, s, name


def usable(f: Face, axis: str, cross: float | None = None) -> tuple[float, float]:
    """The face's span along an axis less its glue landing zone (where the glue
    lug lands): the largest free stretch. On a tapered face, along the line
    through `cross` (the other axis's coordinate)."""
    lo, hi = _polygon_span(f, axis, cross)
    spans = [(lo, hi)]
    i = 0 if axis == "x" else 1
    for k in f.keepouts:
        if k.kind != "glue_landing":
            continue
        a, b = (k.rect[0], k.rect[2]) if axis == "x" else (k.rect[1], k.rect[3])
        # Only a zone across the whole of the other axis cuts the span.
        c0, c1 = (k.rect[1], k.rect[3]) if axis == "x" else (k.rect[0], k.rect[2])
        other = f.height if axis == "x" else f.width
        if c0 > TOL or c1 < other - TOL:
            continue
        nxt = []
        for s0, s1 in spans:
            if b <= s0 or a >= s1:
                nxt.append((s0, s1))
                continue
            if a > s0:
                nxt.append((s0, a))
            if b < s1:
                nxt.append((b, s1))
        spans = nxt
    del i
    return max(spans, key=lambda s: s[1] - s[0]) if spans else (lo, hi)


def _polygon_span(f: Face, axis: str, cross: float | None) -> tuple[float, float]:
    full = (0.0, f.width) if axis == "x" else (0.0, f.height)
    if not f.polygon or cross is None:
        return full
    pts = [f.from_flat(p) for p in f.polygon]
    i, j = (0, 1) if axis == "x" else (1, 0)
    hits: list[float] = []
    for a, b in zip(pts, pts[1:] + pts[:1]):
        if (a[j] - cross) * (b[j] - cross) <= 0 and abs(a[j] - b[j]) > TOL:
            t = (cross - a[j]) / (b[j] - a[j])
            hits.append(a[i] + t * (b[i] - a[i]))
    if len(hits) < 2:
        return full
    return (max(min(hits), full[0]), min(max(hits), full[1]))


def centre(f: Face, axis: str, datum: str, cross: float | None = None) -> float:
    if axis == "y":
        if datum == "usable":
            lo, hi = usable(f, "y", cross)
            return (lo + hi) / 2
        return f.height / 2
    if datum == "assembled":
        return f.centre_x
    if datum == "material":
        return f.material_centre_x
    lo, hi = usable(f, "x", cross)
    return (lo + hi) / 2


def pattern_offsets(pattern: dict[str, Any], env: Env, k: float) -> list[tuple[float, float]]:
    """Copy offsets from the first copy, in inches (k: inches per part unit). Spread is resolved per face."""
    kind = pattern["kind"]
    if kind in ("single", "spread"):
        return [(0.0, 0.0)]
    if kind in ("row", "column"):
        p = _len(pattern["pitch"], env, "pattern.pitch") * k
        if p <= 0:
            raise PlacementError("pattern.pitch must be above zero")
        return [(i * p, 0.0) if kind == "row" else (0.0, -i * p) for i in range(pattern["count"])]
    px = _len(pattern["pitch_x"], env, "pattern.pitch_x") * k
    py = _len(pattern["pitch_y"], env, "pattern.pitch_y") * k
    if px <= 0 or py <= 0:
        raise PlacementError("pattern pitches must be above zero")
    return [(c * px, -r * py) for r in range(pattern["rows"]) for c in range(pattern["cols"])]


def _len(v: Any, env: Env, what: str) -> float:
    try:
        return evaluate(v, env, "length")
    except ExpressionError as exc:
        raise PlacementError(f"{what}: {exc}") from None


def _pairing(fm: FaceMap, faces: list[str], placement: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """The mirror mode and, for each mirrored face, the face it mirrors."""
    mirror = placement.get("mirror")
    explicit = placement.get("pairs")
    if mirror == "none":
        if explicit:
            raise PlacementError("pairs only apply with mirror position_only or position_and_shape")
        return "none", {}
    pairs: list[tuple[str, str]]
    if explicit:
        pairs = [(a, b) for a, b in explicit]
        for a, b in pairs:
            for x in (a, b):
                if x not in faces:
                    raise PlacementError(f"pairs names {x}, which isn't in this placement's faces ({', '.join(faces)})")
    else:
        auto = opposite_pairs(fm)
        pairs = [(a, b) for a, b in auto if a in faces and b in faces]
    seen: dict[str, str] = {}
    for a, b in pairs:
        for x in (a, b):
            if x in seen:
                raise PlacementError(f"{x} is in more than one pair")
            seen[x] = a
    unpaired = [f for f in faces if f not in seen]
    if len(faces) == 1:
        return "none", {}
    if unpaired:
        if mirror is None and not explicit:
            raise PlacementError(
                f"{', '.join(faces)} aren't all opposite faces, so it isn't clear how to mirror the placement "
                f"({', '.join(unpaired)} has no opposite here). Say mirror: none to place it the same on each "
                "face, or give pairs")
        raise PlacementError(f"{', '.join(unpaired)} isn't in any pair; give pairs for every face or mirror: none")
    return mirror or "position_only", {b: a for a, b in pairs}


def _mirror_axis(f_from: Face, f_to: Face, spec: Any) -> Any:
    """The x placement given for f_from, as it reads on its opposite face f_to."""
    if spec == "centred":
        return spec
    name = spec["from"].split(".")[-1]
    ln = f_from.lines.get(name)
    if ln is None:
        return spec  # reported when resolving
    if ln.inward is None:
        d = spec["distance"]
        neg = -d if isinstance(d, (int, float)) else f"-({d})"
        return {**spec, "from": name, "distance": neg}
    side = next((s for s in ("left", "right") if f_from.lines[s].start == ln.start and f_from.lines[s].end == ln.end),
                None)
    if side is None:
        raise PlacementError(f"{name} on {f_from.id} has no mirror image on {f_to.id}; give mirror: none")
    return {**spec, "from": "right" if side == "left" else "left"}


def resolve(fm: FaceMap, placement: dict[str, Any], box: Box, env: Env, *, k: float = 1.0,
            what: str = "feature", single_datum: bool = True) -> list[Copy]:
    """Every placed copy. `box` is the thing's bounding box around its datum in
    inches, already rotated; k converts the part's units to inches. Copies are
    returned face by face in the selector's order."""
    try:
        faces = fm.resolve(placement["on"])
    except ValueError as exc:
        raise PlacementError(str(exc)) from None
    mode, mirrors = _pairing(fm, faces, placement)
    pattern = placement["pattern"]
    offsets = pattern_offsets(pattern, env, k)
    copies: list[Copy] = []
    for fid in faces:
        f = fm.faces[fid]
        src = mirrors.get(fid)
        spec = dict(placement)
        if src is not None and "x" in placement:
            spec["x"] = _mirror_axis(fm.faces[src], f, placement["x"])
        flip = src is not None and mode == "position_and_shape"
        here = _resolve_face(f, spec, box, offsets, env, k, what, flip, single_datum)
        for c in here:
            c.mirrored_from = src
            if flip:
                c.flip = True
                c.rotation_deg = (-placement["rotation_deg"]) % 360.0
        copies += here
        if len(copies) > MAX_COPIES:
            raise PlacementError(f"{what}: more than {MAX_COPIES} copies")
    return copies


def _resolve_face(f: Face, spec: dict[str, Any], box: Box, offsets: list[tuple[float, float]], env: Env, k: float,
                  what: str, flip: bool, single_datum: bool) -> list[Copy]:
    bx0, by0, bx1, by1 = box
    if flip:
        bx0, bx1 = -bx1, -bx0
    pattern = spec["pattern"]
    datum = spec["datum"]
    rot = spec["rotation_deg"]
    where = f"{what} on {f.id}"
    if pattern["kind"] == "spread":
        axis = pattern["axis"]
        other = "y" if axis == "x" else "x"
        cross_pos, cross_callout = _axis_pos(f, other, spec.get(other), (bx0, by0, bx1, by1), [(0.0, 0.0)], env, k,
                                             datum, where, single_datum, None)
        cross_datum = cross_pos  # the datum's coordinate on the other axis
        lo, hi = usable(f, axis, cross_datum)
        inset = _len(pattern["inset"], env, f"{where}: pattern.inset") * k
        n = pattern["count"]
        size = (bx1 - bx0) if axis == "x" else (by1 - by0)
        lo_e, hi_e = lo + inset, hi - inset
        room = hi_e - lo_e
        if room < size - TOL:
            raise PlacementError(f"{where}: the spread doesn't fit: {room:.4g} in between the insets, each copy is "
                                 f"{size:.4g} in")
        if n == 1:
            starts = [(lo + hi) / 2 - size / 2]
        else:
            gap = (room - n * size) / (n - 1)
            if gap < -TOL:
                raise PlacementError(f"{where}: {n} copies of {size:.4g} in don't fit in {room:.4g} in")
            starts = [lo_e + i * (size + gap) for i in range(n)]
        lo_box = bx0 if axis == "x" else by0
        out = []
        for i, s in enumerate(starts):
            pos = s - lo_box
            x, y = (pos, cross_datum) if axis == "x" else (cross_datum, pos)
            c = Copy(f.id, i + 1, n, x, y, rot)
            if cross_callout:
                c.callouts.append(cross_callout)
            out.append(c)
        if axis == "y":
            out.reverse()  # top to bottom, as rows read
            for i, c in enumerate(out):
                c.index = i + 1
        return out
    xs, cx = _axis_pos(f, "x", spec["x"], (bx0, by0, bx1, by1), offsets, env, k, datum, where, single_datum, None)
    ys, cy = _axis_pos(f, "y", spec["y"], (bx0, by0, bx1, by1), offsets, env, k, datum, where, single_datum, xs)
    out = []
    for i, (ox, oy) in enumerate(offsets):
        c = Copy(f.id, i + 1, len(offsets), xs + ox, ys + oy, rot)
        if i == 0:
            c.callouts += [x for x in (cx, cy) if x is not None]
        out.append(c)
    return out


def _axis_pos(f: Face, axis: str, spec: Any, box: Box, offsets: list[tuple[float, float]], env: Env, k: float,
              datum: str, where: str, single_datum: bool, cross: float | None) -> tuple[float, Callout | None]:
    """The first copy's datum coordinate along one axis, and the distance it was measured as."""
    i = 0 if axis == "x" else 1
    lo = min(o[i] for o in offsets) + (box[0] if axis == "x" else box[1])
    hi = max(o[i] for o in offsets) + (box[2] if axis == "x" else box[3])
    # The pattern's own datum: the thing's datum for a single copy, else the pattern box's centre.
    pc = 0.0 if (len(offsets) == 1 and single_datum) else (lo + hi) / 2
    if spec == "centred":
        return centre(f, axis, datum, cross) - pc, None
    c, s, name = _ref(f, spec["from"], axis, datum, where)
    d = _len(spec["distance"], env, f"{where}: {axis}.distance") * k
    to = spec["to"]
    text = str(spec["distance"])
    if s != 0:
        if d < -TOL:
            raise PlacementError(f"{where}: {axis} distance {text} from {name} is negative, which would put it "
                                 "outside the face; distances from an edge run inward")
        mark = c + s * d
        if to == "centre":
            pos = mark - pc
        elif to == "near_edge":  # the side facing the reference: lo when it's below/left, hi when above/right
            pos = mark - lo if s > 0 else mark - hi
        else:
            pos = mark - hi if s > 0 else mark - lo
        return pos, Callout(axis, name, c, mark, to, d, text)
    if to == "centre":
        return c + d - pc, Callout(axis, name, c, c + d, to, d, text)
    if abs(d) <= TOL:
        raise PlacementError(f"{where}: a distance of 0 from {name} puts the feature's centre on it; use to: centre")
    if to == "near_edge":
        pos = c + d - lo if d > 0 else c + d - hi
    else:
        pos = c + d - hi if d > 0 else c + d - lo
    return pos, Callout(axis, name, c, c + d, to, d, text)


def describe(copies: list[Copy], placement: dict[str, Any], fmt) -> str:
    """The placement in plain words, for the read-back."""
    faces = list(dict.fromkeys(c.face for c in copies))
    parts = [f"on {', '.join(faces)}"]
    for axis in ("x", "y"):
        spec = placement.get(axis)
        if spec is None:
            continue
        if spec == "centred":
            parts.append(f"centred in {axis} ({placement.get('datum', 'assembled')})")
        else:
            to = {"near_edge": "near edge", "far_edge": "far edge", "centre": "centre"}[spec["to"]]
            parts.append(f"{axis}: {spec['distance']} from {spec['from']} to its {to}")
    pat = placement.get("pattern", {"kind": "single"})
    if pat["kind"] != "single":
        n = len(copies) // max(1, len(faces))
        parts.append(f"{pat['kind']} of {n} per face")
    mirrored = [c for c in copies if c.mirrored_from]
    if mirrored:
        parts.append("mirrored on " + ", ".join(dict.fromkeys(f"{c.face} (from {c.mirrored_from})" for c in mirrored)))
    return "; ".join(parts)

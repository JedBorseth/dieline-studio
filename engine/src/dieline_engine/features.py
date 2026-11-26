"""Features on standards: handholes, hand flaps, vents, windows, extra scores,
notches, perforations and tear strips (docs/DESIGN-SYSTEM.md, "Features").

A feature is parameters and a placement, never coordinates. The engine resolves
the placement on the part's face map (placement.py), builds the geometry from
library shapes (shapes.py) as typed operations that declare their deliberate
contacts, and runs the topology rules on the result.

- A feature whose placement no longer resolves, that falls outside its face,
  or that makes geometry that can't exist (an undeclared touch or crossing) is
  **stale**: it isn't drawn (a ghost shows where it was) and it blocks export.
  A tool call that adds or changes a feature refuses instead (`refusals`), so
  only a resize or a style change can leave one stale.
- Every default size here is provisional unless it says it came from a shop
  file; the checks say so.

Stored on the part (BoxParams.features) as plain JSON, each with a stable id
made by the engine (f1, f2, ...).
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Any

from . import placement as pl
from . import shapes as sh
from .expressions import Env, ExpressionError, env_for, evaluate
from .faces import Face, FaceMap
from .geometry import Arc, Dieline, Line, LineType, Operation, Point, Segment
from .rules import PERF_CUT, PERF_TIE, RuleSpec

KINDS = ("handhole", "hand_flap", "vent", "window", "score", "perforation", "tear_strip", "notch")
MAX_FEATURES = 40
TOL = 1e-6

# Defaults. The handhole is File_Box_1's (a shop file): 3 5/8 x 1 3/8, full-radius 11/16 ends, long
# side horizontal; and the perforation is its dedicated 3/16 x 3/16 rule. Everything else is provisional.
# Defaults carry their unit, so they mean the same on a millimetre part.
HANDHOLE = {"shape": "obround", "width": "3 5/8 in", "height": "1 3/8 in"}
VENT_DIAMETER = "1/2 in"
CLEARANCE_IN = 0.25
TAB_LENGTH_IN = 0.5
TEAR_WIDTH_IN = 0.75
PROVISIONAL = {
    "clearance": "1/4 in from scores, slots, edges and other features",
    "carry_web": "max(1 in, 6 x CAL) from the score above a handhole",
    "side_web": "max(1 in, 4 x CAL) to the scores either side",
    "hand_flap_join": "35 deg minimum between a hand flap's cut and its hinge",
    "vent": "1/2 in vent diameter",
    "tear_strip": "3/4 in tear strip width, 1/2 in pull tab",
    "notch_corner": "1/8 in from a notch to the end of its straight edge",
}


class FeatureError(ValueError):
    """A feature's input is wrong: says what to change."""


# ------------------------------------------------------------------ input


def _str_enum(item: dict, key: str, choices: tuple[str, ...], default: str | None, what: str) -> str | None:
    v = item.get(key, default)
    if v is None:
        return None
    if v not in choices:
        raise FeatureError(f"{what}.{key} must be one of {', '.join(choices)}")
    return v


def _length(item: dict, key: str, default: Any, what: str) -> Any:
    v = item.get(key, default)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise FeatureError(f"{what}.{key} must be a length: a number, a fraction like '1 3/8' or an expression")
    return v


def _bool(item: dict, key: str, what: str) -> bool:
    v = item.get(key, False)
    if not isinstance(v, bool):
        raise FeatureError(f"{what}.{key} must be true or false")
    return v


FIELDS: dict[str, set[str]] = {
    "handhole": {"shape", "width", "height", "diameter", "corner_radius", "flat", "placement"},
    "vent": {"shape", "width", "height", "diameter", "corner_radius", "placement"},
    "window": {"shape", "width", "height", "diameter", "corner_radius", "placement"},
    "hand_flap": {"profile", "width", "height", "diameter", "corner_radius", "hinge", "double_crease",
                  "relief_nicks", "taper_deg", "placement"},
    "score": {"route", "side"},
    "perforation": {"route", "cut", "tie", "phase", "crossing"},
    "tear_strip": {"route", "width", "start", "tab", "tab_length", "rails", "glue_joint"},
    "notch": {"shape", "width", "depth", "corner_radius", "on", "edge", "along", "pattern", "mirror", "pairs"},
}
COMMON = {"id", "kind", "label", "bound", "pointage"}
SHAPES = {"handhole": ("obround", "rect", "d_shape", "circle"), "vent": ("circle", "obround", "rect"),
          "window": ("rect", "rounded_rect", "circle", "ellipse")}


def normalize(item: Any, fid: str | None = None) -> dict[str, Any]:
    """Check one feature's shape (its values are checked when it's built) and fill defaults."""
    if not isinstance(item, dict):
        raise FeatureError("each feature must be an object with a kind")
    kind = item.get("kind")
    what = f"feature {fid or item.get('id') or '(new)'}"
    if kind not in KINDS:
        raise FeatureError(f"{what}.kind must be one of {', '.join(KINDS)}")
    what = f"{kind} {fid or item.get('id') or '(new)'}"
    allowed = FIELDS[kind] | COMMON
    unknown = sorted(set(item) - allowed)
    if unknown:
        raise FeatureError(f"{what} has no field(s) {', '.join(unknown)}; a {kind} takes "
                           f"{', '.join(sorted(FIELDS[kind] | {'label'}))}")
    out: dict[str, Any] = {"kind": kind}
    if fid or item.get("id"):
        out["id"] = fid or item["id"]
    label = item.get("label")
    if label is not None:
        if not isinstance(label, str) or not label.strip() or len(label) > 60:
            raise FeatureError(f"{what}.label must be a short name")
        out["label"] = label.strip()
    if "pointage" in item:
        p = item["pointage"]
        if isinstance(p, bool) or not isinstance(p, (int, float)) or not 1 <= p <= 12:
            raise FeatureError(f"{what}.pointage must be a rule thickness in points (2, 3, ...)")
        out["pointage"] = float(p)
    if isinstance(item.get("bound"), dict):
        out["bound"] = {str(k): str(v) for k, v in item["bound"].items()}
    if kind in SHAPES:
        default_shape = {"handhole": "obround", "vent": "circle", "window": "rect"}[kind]
        shape = _str_enum(item, "shape", SHAPES[kind], default_shape, what)
        out["shape"] = shape
        if shape == "circle":
            out["diameter"] = _length(item, "diameter", VENT_DIAMETER if kind == "vent" else None, what)
            if out["diameter"] is None:
                raise FeatureError(f"{what}: a circle needs a diameter")
        else:
            dw = HANDHOLE["width"] if kind == "handhole" and shape == "obround" else None
            dh = HANDHOLE["height"] if kind == "handhole" and shape == "obround" else None
            out["width"] = _length(item, "width", dw, what)
            out["height"] = _length(item, "height", dh, what)
            if out["width"] is None or out["height"] is None:
                raise FeatureError(f"{what}: a {shape} needs a width and a height")
            if shape in ("rect", "rounded_rect", "d_shape"):
                cr = _length(item, "corner_radius", None, what)
                if cr is not None:
                    out["corner_radius"] = cr
                elif shape == "rounded_rect":
                    raise FeatureError(f"{what}: a rounded_rect needs a corner_radius")
            if shape == "d_shape":
                out["flat"] = _str_enum(item, "flat", ("top", "bottom", "left", "right"), "top", what)
        out["placement"] = _placement(item, what)
    elif kind == "hand_flap":
        prof = _str_enum(item, "profile", ("obround", "d_shape", "rect", "circle", "angled"), "obround", what)
        out["profile"] = prof
        if prof == "circle":
            out["diameter"] = _length(item, "diameter", None, what)
            if out["diameter"] is None:
                raise FeatureError(f"{what}: a circle hand flap needs a diameter")
        else:
            dflt = prof == "obround"
            out["width"] = _length(item, "width", HANDHOLE["width"] if dflt else None, what)
            out["height"] = _length(item, "height", HANDHOLE["height"] if dflt else None, what)
            if out["width"] is None or out["height"] is None:
                raise FeatureError(f"{what}: a {prof} hand flap needs a width and a height")
        out["hinge"] = _str_enum(item, "hinge", ("top", "bottom", "left", "right"), "top", what)
        out["double_crease"] = _bool(item, "double_crease", what)
        out["relief_nicks"] = _bool(item, "relief_nicks", what)
        if out["relief_nicks"] and not out["double_crease"]:
            raise FeatureError(f"{what}: relief nicks run between the two creases of a double crease; "
                               "set double_crease too")
        if prof == "angled":
            t = item.get("taper_deg", 15)
            if isinstance(t, bool) or not isinstance(t, (int, float)):
                raise FeatureError(f"{what}.taper_deg must be a number of degrees")
            out["taper_deg"] = float(t)
        elif "taper_deg" in item:
            raise FeatureError(f"{what}: taper_deg is for profile angled")
        cr = _length(item, "corner_radius", None, what)
        if cr is not None:
            out["corner_radius"] = cr
        out["placement"] = _placement(item, what)
    elif kind in ("score", "perforation", "tear_strip"):
        out["route"] = _route(item.get("route"), what, kind)
        if kind == "score":
            out["side"] = _str_enum(item, "side", ("normal", "reverse"), "normal", what)
        elif kind == "perforation":
            out["cut"] = _length(item, "cut", "3/16 in", what)
            out["tie"] = _length(item, "tie", "3/16 in", what)
            out["phase"] = _length(item, "phase", 0, what)
            out["crossing"] = _str_enum(item, "crossing", ("tie_at_creases", "continuous"), "tie_at_creases", what)
        else:
            out["width"] = _length(item, "width", "3/4 in", what)
            out["start"] = _str_enum(item, "start", ("start", "end"), "start", what)
            out["tab"] = _str_enum(item, "tab", ("edge_tab", "angled_tab", "thumb_hole"), "edge_tab", what)
            out["tab_length"] = _length(item, "tab_length", "1/2 in", what)
            out["rails"] = _str_enum(item, "rails", ("perf", "cut_score"), "perf", what)
            out["glue_joint"] = _str_enum(item, "glue_joint", ("cross", "stop"), "cross", what)
    elif kind == "notch":
        out["shape"] = _str_enum(item, "shape", ("half_circle", "v", "rounded_rect"), "half_circle", what)
        out["width"] = _length(item, "width", None, what)
        if out["width"] is None:
            raise FeatureError(f"{what}: a notch needs a width (its opening along the edge)")
        if out["shape"] != "half_circle":
            out["depth"] = _length(item, "depth", None, what)
            if out["depth"] is None:
                raise FeatureError(f"{what}: a {out['shape']} notch needs a depth")
        elif "depth" in item:
            raise FeatureError(f"{what}: a half_circle notch's depth is half its width; leave depth out")
        cr = _length(item, "corner_radius", None, what)
        if cr is not None:
            out["corner_radius"] = cr
        on = item.get("on")
        if not (isinstance(on, str) or (isinstance(on, list) and on and all(isinstance(x, str) for x in on))):
            raise FeatureError(f"{what}.on must be a face or selector")
        out["on"] = on
        edge = item.get("edge")
        if not isinstance(edge, str) or not edge:
            raise FeatureError(f"{what}.edge must name the face edge: top, bottom, left, right, free_edge, ...")
        out["edge"] = edge
        along = item.get("along", "centred")
        try:
            out["along"] = pl._axis(along, f"{what}.along")
            out["pattern"] = pl._pattern(item.get("pattern"), f"{what}")
        except pl.PlacementError as exc:
            raise FeatureError(str(exc)) from None
        if out["pattern"]["kind"] not in ("single", "row"):
            raise FeatureError(f"{what}: notches along an edge take pattern single or row")
        for key in ("mirror", "pairs"):
            if key in item:
                out[key] = item[key]
    return out


def _placement(item: dict, what: str) -> dict[str, Any]:
    try:
        return pl.normalize(item.get("placement"), what=f"{what}.placement")
    except pl.PlacementError as exc:
        raise FeatureError(str(exc)) from None


def _route(raw: Any, what: str, kind: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise FeatureError(f"{what}.route must be an object: faces, direction and at")
    allowed = {"faces", "direction", "at", "slots"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise FeatureError(f"{what}.route has no field(s) {', '.join(unknown)}; it takes "
                           f"{', '.join(sorted(allowed))}")
    faces = raw.get("faces")
    if not (isinstance(faces, str) or (isinstance(faces, list) and faces and all(isinstance(x, str) for x in faces))):
        raise FeatureError(f"{what}.route.faces must be the faces it crosses, in order (e.g. [\"P1\", \"P2\"] or "
                           "\"body_panels\")")
    direction = raw.get("direction")
    if direction not in ("horizontal", "vertical"):
        raise FeatureError(f"{what}.route.direction must be horizontal or vertical (in the first face's frame, "
                           "seen from outside)")
    at = raw.get("at")
    if not isinstance(at, dict) or set(at) - {"from", "distance"} or not isinstance(at.get("from"), str) \
            or at.get("distance") is None:
        axis = "y" if direction == "horizontal" else "x"
        raise FeatureError(f"{what}.route.at must be {{from, distance}}: the {axis} reference on the first face "
                           "and the distance from it")
    slots = raw.get("slots", "stop")
    if slots not in ("stop", "jump"):
        raise FeatureError(f"{what}.route.slots must be stop or jump")
    return {"faces": faces, "direction": direction, "at": {"from": at["from"], "distance": at["distance"]},
            "slots": slots}


def upsert(existing: list[dict[str, Any]], items: list[Any] | None, remove: list[Any] | None
           ) -> tuple[list[dict[str, Any]], list[str]]:
    """Batch upsert by id: an item without an id is new (the engine gives it one), an
    item with an id changes that feature (fields not given keep their values), and
    `remove` lists ids to delete. Returns the new list and the ids added or changed."""
    feats = [copy.deepcopy(f) for f in existing]
    by_id = {f["id"]: f for f in feats}
    items = items or []
    remove = remove or []
    if not isinstance(items, list) or not isinstance(remove, list):
        raise FeatureError("items and remove must be lists")
    for r in remove:
        if r not in by_id:
            raise FeatureError(f"no feature {r!r} to remove; features: {', '.join(by_id) or 'none'}")
    feats = [f for f in feats if f["id"] not in remove]
    by_id = {f["id"]: f for f in feats}
    n = max([int(f["id"][1:]) for f in existing if str(f.get("id", ""))[1:].isdigit()] + [0])
    touched: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            raise FeatureError("each item must be an object")
        fid = item.get("id")
        if fid is None:
            n += 1
            fid = f"f{n}"
            new = normalize({k: v for k, v in item.items() if k not in ("id", "bound")}, fid)
            feats.append(new)
            by_id[fid] = new
        else:
            if fid not in by_id:
                raise FeatureError(f"no feature {fid!r}; features: {', '.join(by_id) or 'none'} "
                                   "(leave id out to add a new one)")
            old = by_id[fid]
            if "kind" in item and item["kind"] != old["kind"]:
                raise FeatureError(f"{fid} is a {old['kind']}; remove it and add a {item['kind']} instead")
            merged = {**old, **{k: v for k, v in item.items() if k != "bound"}}
            if "placement" in item and isinstance(item["placement"], dict) and isinstance(old.get("placement"), dict):
                merged["placement"] = {**old["placement"], **item["placement"]}
            merged.pop("bound", None)
            new = normalize(merged, fid)
            feats[feats.index(old)] = new
            by_id[fid] = new
        touched.append(fid)
    if len(feats) > MAX_FEATURES:
        raise FeatureError(f"a part can have at most {MAX_FEATURES} features")
    return feats, touched


def bind(features: list[dict[str, Any]], fm: FaceMap | None, ids: list[str]) -> list[dict[str, Any]]:
    """Record the role of each face a feature names by id (not by role), so a later
    order swap that changes what that face is makes the feature stale."""
    if fm is None:
        return features
    out = []
    for f in features:
        if f["id"] in ids:
            on = _selector(f)
            names = on if isinstance(on, list) else [on]
            bound = {n: fm.faces[n].role for n in names if isinstance(n, str) and n in fm.faces}
            f = {**f, "bound": bound} if bound else {k: v for k, v in f.items() if k != "bound"}
        out.append(f)
    return out


def _selector(f: dict[str, Any]) -> Any:
    if "placement" in f:
        return f["placement"]["on"]
    if "route" in f:
        return f["route"]["faces"]
    return f.get("on")


# ------------------------------------------------------------------ built records


@dataclass(slots=True)
class Placed:
    """One copy of a feature, as drawn."""

    face: str
    index: int
    of: int
    segs: list[Segment]  # manufactured, flat
    outline: list[Segment]  # the opening it leaves (holes, hand flaps) or its path (rules), flat
    callouts: list[pl.Callout] = field(default_factory=list)
    mirrored_from: str | None = None
    faces: list[str] = field(default_factory=list)  # routes: every face crossed
    crossings: list[tuple[float, str]] = field(default_factory=list)  # routes: (station, hinge op)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Built:
    id: str
    kind: str
    label: str
    spec: dict[str, Any]
    status: str = "ok"  # ok | stale
    reason: str | None = None
    copies: list[Placed] = field(default_factory=list)
    ops: list[str] = field(default_factory=list)
    notes: list[tuple[str, str]] = field(default_factory=list)  # (status, message) for the checks
    words: str = ""  # the read-back: faces, size, edge or centre
    extra_ops: list[Operation] = field(default_factory=list)
    notch_plan: list[Any] = field(default_factory=list)
    notch_segs: list[Segment] = field(default_factory=list)
    stations: dict[Segment, float] = field(default_factory=dict)  # perforation runs: where each starts

    @property
    def name(self) -> str:
        return f"{self.label} {self.id}"

    def faces(self) -> list[str]:
        out: list[str] = []
        for c in self.copies:
            for f in (c.faces or [c.face]):
                if f not in out:
                    out.append(f)
        return out

    def to_dict(self, units: str = "in") -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id, "kind": self.kind, "label": self.label, "status": self.status,
            "faces": self.faces(), "copies": len(self.copies), "summary": self.words,
            "selector": _selector(self.spec),
        }
        if self.reason:
            out["reason"] = self.reason
        boxes = [_bounds(c.outline or c.segs) for c in self.copies if (c.outline or c.segs)]
        if boxes:
            out["region"] = [round(min(b[0] for b in boxes), 4), round(min(b[1] for b in boxes), 4),
                             round(max(b[2] for b in boxes), 4), round(max(b[3] for b in boxes), 4)]
            out["copy_regions"] = [[round(v, 4) for v in b] for b in boxes]
        return out


def _bounds(segs: list[Segment]) -> tuple[float, float, float, float]:
    from .geometry import _segment_extent_points  # noqa: PLC0415

    pts = [p for s in segs for p in _segment_extent_points(s)]
    return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))


# ------------------------------------------------------------------ building


@dataclass(slots=True)
class Ctx:
    d: Dieline
    fm: FaceMap
    env: Env
    k: float  # inches per part unit
    cal: float  # inches
    units: str

    def length(self, v: Any, what: str) -> float:
        try:
            return evaluate(v, self.env, "length") * self.k
        except ExpressionError as exc:
            raise FeatureError(f"{what}: {exc}") from None

    def size(self, v: Any, what: str) -> float:
        x = self.length(v, what)
        if x <= 1e-4:
            raise FeatureError(f"{what} {v!r} comes to {x:.4g} in; a size must be above zero")
        return x


def _flat_prims(face: Face, prims: list[sh.Prim]) -> list[sh.Prim]:
    """Face frame to the flat blank: the frame is a mirror image of the drawing, so arcs turn the other way."""
    return [p.map(face.to_flat, face.mirrored) for p in prims]


def _segs(prims: list[sh.Prim], type: LineType, op: str) -> list[Segment]:
    return [p.segment(type, op) for p in prims]


def _closed_shape(spec: dict[str, Any], ctx: Ctx, what: str) -> tuple[list[sh.Prim], str]:
    shape = spec["shape"]
    if shape == "circle":
        d = ctx.size(spec["diameter"], f"{what} diameter")
        from .checks import fmt_in  # noqa: PLC0415

        return sh.circle(d), f"{fmt_in(d)} in circle"
    w = ctx.size(spec["width"], f"{what} width")
    h = ctx.size(spec["height"], f"{what} height")
    from .checks import fmt_in  # noqa: PLC0415

    size = f"{fmt_in(w)} x {fmt_in(h)} in"
    if shape == "obround":
        return sh.obround(w, h), f"{size} obround (full-radius ends)"
    if shape in ("rect", "rounded_rect"):
        r = ctx.length(spec.get("corner_radius", 0), f"{what} corner_radius") if spec.get("corner_radius") else 0.0
        if r < 0 or r > min(w, h) / 2 + TOL:
            raise FeatureError(f"{what}: corner_radius must be between 0 and half the smaller side")
        return sh.rounded_rect(w, h, r), f"{size} rectangle" + (f", corners r {fmt_in(r)}" if r else "")
    if shape == "d_shape":
        r = ctx.length(spec["corner_radius"], f"{what} corner_radius") if spec.get("corner_radius") else None
        return sh.d_shape(w, h, r, spec.get("flat", "top")), f"{size} D-shape, flat side {spec.get('flat', 'top')}"
    if shape == "ellipse":
        prims, gap = sh.oval(w, h)
        return prims, f"{size} oval (four arcs, within {fmt_in(round(gap * 1024) / 1024)} in of the ellipse)"
    raise FeatureError(f"{what}: unknown shape {shape!r}")


def _inside_face(face: Face, segs: list[Segment], tol: float = 1e-5) -> str | None:
    """None if every segment lies on the face (its polygon, which is convex), else where it leaves."""
    from .topology import _Piece  # noqa: PLC0415

    poly = face.polygon
    if not poly:
        return None
    dummy = Operation("_", "cut")
    for s in segs:
        pc = _Piece(s, dummy)
        n = 16 if isinstance(s, Arc) else 2
        for i in range(n + 1):
            p = pc.at(pc.span * i / n)
            out = _outside_by(poly, p)
            if out > tol:
                return f"it reaches {out:.4g} in outside {face.id}"
    return None


def _outside_by(poly: list[Point], p: Point) -> float:
    """How far p lies outside a convex polygon (0 inside), from its edges' half-planes."""
    area = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(poly, poly[1:] + poly[:1]))
    sign = 1.0 if area > 0 else -1.0
    worst = 0.0
    for a, b in zip(poly, poly[1:] + poly[:1]):
        L = math.dist(a, b)
        if L <= 1e-12:
            continue
        # Distance to the left of a->b for a CCW polygon is inside.
        cross = ((b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])) / L * sign
        worst = max(worst, -cross)
    return worst


def _env(params: Any, board: Any) -> Env:
    dims = {"L": params.length, "W": params.width, "D": params.depth}
    return env_for(params.units, board, dims)


def apply(d: Dieline, features: list[dict[str, Any]], params: Any, board: Any) -> None:
    """Draw every feature onto a standard's dieline (in place). Stale features are
    left out of the manufactured geometry and drawn as ghosts. Fills
    d.extras: features (Built records), tags (segment -> feature id), report."""
    from .topology import validate  # noqa: PLC0415

    fm = d.faces
    if not isinstance(fm, FaceMap):
        if features:
            raise FeatureError("this part has no face map, so features can't be placed on it")
        return
    ctx = Ctx(d, fm, _env(params, board), 1.0 if params.units == "in" else 1 / 25.4, board.cal, params.units)
    base = Dieline(d.name, d.units, list(d.segments), d.meta, dict(d.ops), d.faces)
    built: list[Built] = []
    for raw in features:
        b = Built(raw["id"], raw["kind"], raw.get("label") or raw["kind"].replace("_", " "), raw)
        try:
            _stale_by_role(b, fm)
            if b.status == "ok":
                _draft(b, ctx)
                if b.kind == "notch":  # on its own first, so its own mistakes are its own
                    _apply_notches(Dieline(d.name, d.units, list(base.segments), d.meta, dict(base.ops)), [b])
        except (FeatureError, pl.PlacementError, ExpressionError, ValueError) as exc:
            b.status, b.reason = "stale", str(getattr(exc, "message", None) or exc)
            if b.kind == "notch":
                b.copies = []
        built.append(b)

    def compose(ok: list[Built]) -> Dieline:
        out = Dieline(d.name, d.units, list(base.segments), d.meta, dict(base.ops), d.faces)
        notches = [b for b in ok if b.kind == "notch"]
        if notches:
            _apply_notches(out, notches)
        for b in ok:
            if b.kind == "notch":
                continue
            for op in b.extra_ops:
                out.add_op(op)
            for c in b.copies:
                out.segments.extend(c.segs)
        return out

    def stale(b: Built, why: str) -> None:
        b.status, b.reason = "stale", why

    live = [b for b in built if b.status == "ok"]
    while True:  # notches that only clash with each other
        try:
            final = compose(live)
            break
        except NotchError as exc:
            culprit = next(b for b in live if b.id == exc.fid)
            stale(culprit, str(exc))
            live.remove(culprit)
    report = validate(final)
    if not report.ok and live:
        # Find which features make it invalid: add them one at a time.
        good: list[Built] = []
        for b in live:
            try:
                trial = compose(good + [b])
            except NotchError as exc:
                stale(b, str(exc))
                continue
            r = validate(trial)
            if r.ok:
                good.append(b)
            else:
                stale(b, friendly(_topology_reason(r, b), trial))
        live = good
        final = compose(live)
        report = validate(final)
    d.segments, d.ops = final.segments, final.ops
    # Each feature segment's feature and face, for the tagged preview scene.
    tags: dict[Segment, dict[str, Any]] = {}
    stations: dict[Segment, float] = {}
    for b in built:
        if b.status != "ok":
            continue
        b.ops = [op.id for op in b.extra_ops]
        for c in b.copies:
            for s in (c.outline if b.kind == "notch" else c.segs):
                tags[s] = {"feature_id": b.id, "face_id": c.face}
        stations.update(b.stations)
    # Ghosts: where a stale feature would be, drawn as annotation (never manufactured).
    for b in built:
        if b.status == "stale":
            for c in b.copies:
                for s in (c.outline or c.segs):
                    g = _retype(s, LineType.ANNOTATION, None)
                    d.segments.append(g)
                    tags[g] = {"feature_id": b.id, "face_id": c.face, "stale": True}
    d.extras["features"] = built
    d.extras["tags"] = tags
    d.extras["stations"] = stations
    d.extras["report"] = report


class NotchError(FeatureError):
    def __init__(self, fid: str, message: str) -> None:
        super().__init__(message)
        self.fid = fid


def friendly(msg: str, d: Dieline) -> str:
    """Operation ids in a topology message, in a designer's words."""
    fm = d.faces if isinstance(d.faces, FaceMap) else None
    for op in d.ops.values():
        if op.kind == "hinge" and fm is not None and op.id in fm.hinges:
            h = fm.hinges[op.id]
            msg = msg.replace(f"hinge {op.id}", f"the {h.parent}/{h.child} score")
    msg = msg.replace("outline outline", "the blank's edge")
    for op in d.ops.values():
        if "." in op.id:
            fid, _, rest = op.id.partition(".")
            part = rest.split(".")[-1]
            role = {"cut": "'s cut", "hinge": "'s hinge", "outer": "'s outer crease", "nick": "'s relief nick",
                    "rail_a": "'s rail", "rail_b": "'s rail", "tab": "'s pull tab", "tab_a": "'s tab cut",
                    "tab_b": "'s tab cut"}.get(part, "")
            copy_no = rest.split(".")[0] if rest.split(".")[0].isdigit() else None
            msg = msg.replace(f"{op.kind} {op.id}", f"feature {fid}{f' copy {copy_no}' if copy_no else ''}{role}")
        elif op.kind in ("score", "perf"):
            msg = msg.replace(f"{op.kind} {op.id}", f"feature {op.id}")
    return msg


def _retype(s: Segment, t: LineType, op: str | None) -> Segment:
    if isinstance(s, Line):
        return Line(s.start, s.end, t, op)
    return Arc(s.start, s.end, s.center, s.clockwise, t, s.full_circle, op)


def _topology_reason(r: Any, b: Built) -> str:
    mine = [i for i in r.invalid if any(o == b.id or o.startswith(b.id + ".") or o == "outline" for o in i.ops)]
    issue = (mine or r.invalid)[0]
    return f"{issue.message}; move or resize {b.name}"


def _stale_by_role(b: Built, fm: FaceMap) -> None:
    bound = b.spec.get("bound") or {}
    for face, role in bound.items():
        f = fm.faces.get(face)
        if f is None:
            b.status, b.reason = "stale", f"{face} no longer exists on this part; rebind {b.name} to a face that does"
            return
        if f.role != role:
            b.status, b.reason = "stale", (f"{face} was a{'n' if role[0] in 'aeiou' else ''} {role} panel and is now "
                                           f"a{'n' if f.role[0] in 'aeiou' else ''} {f.role} panel; place "
                                           f"{b.name} again (by role, e.g. end_panels, to follow the panel order)")
            return


def _draft(b: Built, ctx: Ctx) -> None:
    b.extra_ops = []
    if b.kind in ("handhole", "vent", "window"):
        _draft_hole(b, ctx)
    elif b.kind == "hand_flap":
        _draft_flap(b, ctx)
    elif b.kind in ("score", "perforation"):
        _draft_rule(b, ctx)
    elif b.kind == "tear_strip":
        _draft_tear(b, ctx)
    elif b.kind == "notch":
        _draft_notch(b, ctx)


def _rule(b: Built, kind: str, ctx: Ctx, **kw: Any) -> RuleSpec | None:
    p = b.spec.get("pointage")
    if p is None and not kw:
        return None
    return RuleSpec(kind, float(p) if p is not None else float(ctx.d.meta.get("pointage", 2.0)), **kw)


def _place(b: Built, ctx: Ctx, local: list[sh.Prim], extra_local: list[list[sh.Prim]] | None = None
           ) -> list[tuple[pl.Copy, Face, list[sh.Prim], list[list[sh.Prim]]]]:
    spec = b.spec["placement"]
    rot = spec["rotation_deg"]
    box = sh.bbox(sh.transform(local, rotation_deg=rot))
    copies = pl.resolve(ctx.fm, spec, box, ctx.env, k=ctx.k, what=b.name)
    out = []
    for c in copies:
        face = ctx.fm.faces[c.face]
        mine = sh.transform(local, rotation_deg=c.rotation_deg, flip=c.flip, offset=(c.x, c.y))
        extras = [sh.transform(e, rotation_deg=c.rotation_deg, flip=c.flip, offset=(c.x, c.y))
                  for e in (extra_local or [])]
        out.append((c, face, _flat_prims(face, mine), [_flat_prims(face, e) for e in extras]))
    return out


def _draft_hole(b: Built, ctx: Ctx) -> None:
    local, words = _closed_shape(b.spec, ctx, b.name)
    rule = _rule(b, "cut", ctx)
    for c, face, flat, _ in _place(b, ctx, local):
        op = Operation(f"{b.id}.{len(b.copies) + 1}", "hole", closed=True, rule=rule)
        b.extra_ops.append(op)
        segs = _segs(flat, LineType.CUT, op.id)
        why = _inside_face(face, segs)
        if why:
            raise FeatureError(f"{c.label(b.name)} is outside its face: {why}")
        b.copies.append(Placed(c.face, c.index, c.of, segs, segs, c.callouts, c.mirrored_from))
    b.words = f"{words}; " + pl.describe(_copies_of(b), b.spec["placement"], None)


def _copies_of(b: Built) -> list[pl.Copy]:
    return [pl.Copy(c.face, c.index, c.of, 0.0, 0.0, mirrored_from=c.mirrored_from) for c in b.copies]


def _draft_flap(b: Built, ctx: Ctx) -> None:
    from .checks import fmt_in  # noqa: PLC0415

    spec = b.spec
    rot = spec["placement"]["rotation_deg"]
    if abs(rot % 90) > 1e-9:
        raise FeatureError(f"{b.name}: a hand flap turns in steps of 90 deg (rotation_deg 0, 90, 180 or 270); its "
                           "hinge side is named in the face frame")
    prof = spec["profile"]
    if prof == "circle":
        w = h = ctx.size(spec["diameter"], f"{b.name} diameter")
    else:
        w = ctx.size(spec["width"], f"{b.name} width")
        h = ctx.size(spec["height"], f"{b.name} height")
    if round(rot / 90) % 2:
        w, h = h, w  # rotated a quarter turn: the long side runs the other way
    hinge = spec["hinge"]
    along, perp = (w, h) if hinge in ("top", "bottom") else (h, w)
    radius = ctx.length(spec["corner_radius"], f"{b.name} corner_radius") if spec.get("corner_radius") else None
    try:
        fp = sh.flap_profile(prof, along, perp, hinge=hinge, radius=radius, taper_deg=spec.get("taper_deg", 0.0))
    except ValueError as exc:
        raise FeatureError(f"{b.name}: {exc}. Works: obround and circle on a long side (top or bottom when "
                           "horizontal), rect and d_shape on any side, angled with a taper under 55 deg") from None
    # The second crease of a double crease: 2 x CAL outside the opening, away from the hole.
    (ax, ay), (bx, by) = fp.hinge
    L = math.dist(fp.hinge[0], fp.hinge[1])
    nx, ny = (by - ay) / L, -(bx - ax) / L  # perpendicular; pick the side away from the opening
    ocx = sum(p.start[0] for p in fp.opening) / len(fp.opening)
    ocy = sum(p.start[1] for p in fp.opening) / len(fp.opening)
    if (ocx - ax) * nx + (ocy - ay) * ny > 0:
        nx, ny = -nx, -ny
    off = 2 * ctx.cal
    outer = ((ax + nx * off, ay + ny * off), (bx + nx * off, by + ny * off))
    extra: list[list[sh.Prim]] = [[sh.Prim(*fp.hinge)], fp.opening]
    if spec["double_crease"]:
        extra.append([sh.Prim(*outer)])
        if spec["relief_nicks"]:
            extra.append([sh.Prim(fp.hinge[0], outer[0]), sh.Prim(fp.hinge[1], outer[1])])
    # Distances measure the opening (the hole the flap leaves); the outer crease is checked on its face.
    spec_p = {**spec["placement"], "rotation_deg": 0.0}
    box = sh.bbox(fp.opening)
    copies = pl.resolve(ctx.fm, spec_p, box, ctx.env, k=ctx.k, what=b.name)
    cut_rule, crease_rule = _rule(b, "cut", ctx), _rule(b, "crease", ctx)
    for c in copies:
        face = ctx.fm.faces[c.face]
        i = len(b.copies) + 1
        t = lambda prims: _flat_prims(face, sh.transform(prims, flip=c.flip, offset=(c.x, c.y)))  # noqa: E731
        ids = {k: f"{b.id}.{i}.{k}" for k in ("cut", "hinge", "outer", "nick")}
        nicks = spec["double_crease"] and spec["relief_nicks"]
        ops = [
            Operation(ids["cut"], "flap_cut", open_ends="on",
                      ends_on=(ids["hinge"],) + ((ids["nick"],) if nicks else ()), rule=cut_rule),
            Operation(ids["hinge"], "flap_hinge", open_ends="on",
                      ends_on=(ids["cut"],) + ((ids["nick"],) if nicks else ()), rule=crease_rule),
        ]
        segs = _segs(t(fp.cut), LineType.CUT, ids["cut"]) + _segs(t([sh.Prim(*fp.hinge)]), LineType.CREASE,
                                                                   ids["hinge"])
        if spec["double_crease"]:
            ops.append(Operation(ids["outer"], "flap_crease", open_ends="on" if nicks else "free",
                                 ends_on=(ids["nick"],) if nicks else (), rule=crease_rule))
            segs += _segs(t(extra[2]), LineType.CREASE, ids["outer"])
            if nicks:
                ops.append(Operation(ids["nick"], "nick", open_ends="on",
                                     ends_on=(ids["hinge"], ids["outer"], ids["cut"]), rule=cut_rule))
                segs += _segs(t(extra[3]), LineType.CUT, ids["nick"])
        opening = _segs(t(fp.opening), LineType.CUT, ids["cut"])
        why = _inside_face(face, segs + opening)
        if why:
            raise FeatureError(f"{c.label(b.name)} is outside its face: {why}")
        b.extra_ops += ops
        outer_seg = _segs(t(extra[2]), LineType.CREASE, ids["outer"]) if spec["double_crease"] else []
        b.copies.append(Placed(c.face, c.index, c.of, segs, opening, c.callouts, c.mirrored_from,
                               extra={"hinge": hinge, "outer": outer_seg}))
    if hinge == "bottom":
        b.notes.append(("info", f"{b.name} hinges at the bottom: fine for access or venting, but a top hinge is "
                                "usual for carrying"))
    words = f"{fmt_in(w)} x {fmt_in(h)} in {prof} hand flap hinged on its {hinge}"
    if fp.inset:
        words += f" (secant hinge {fmt_in(fp.inset)} in inside the opening, so the flap is that much shorter)"
    if spec["double_crease"]:
        words += f", double crease 2 x CAL ({fmt_in(off)} in) outside" + (" with relief nicks" if nicks else "")
    b.words = f"{words}; " + pl.describe(_copies_of(b), spec_p, None)


# ------------------------------------------------------------------ routes


@dataclass(slots=True)
class Route:
    pieces: list[tuple[Point, Point, float]]  # flat start, end, station of the start
    faces: list[str]
    crossings: list[tuple[float, str]]  # (station, hinge op id)
    length: float
    ends: tuple[str, str]  # what each end lands on: outline | crease | slot


def _clip(poly: list[Point], p0: Point, u: Point) -> tuple[float, float] | None:
    """The parameter interval of the line p0 + t u inside a convex polygon."""
    lo, hi = -math.inf, math.inf
    area = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(poly, poly[1:] + poly[:1]))
    sign = 1.0 if area > 0 else -1.0
    for a, b in zip(poly, poly[1:] + poly[:1]):
        ex, ey = b[0] - a[0], b[1] - a[1]
        # inside: cross(e, p - a) * sign >= 0
        num = (ex * (p0[1] - a[1]) - ey * (p0[0] - a[0])) * sign
        den = (ex * u[1] - ey * u[0]) * sign
        if abs(den) < 1e-15:
            if num < -1e-9:
                return None
            continue
        t = -num / den
        if den > 0:
            lo = max(lo, t)
        else:
            hi = min(hi, t)
    if hi - lo <= 1e-9:
        return None
    return lo, hi


def route(ctx: Ctx, spec: dict[str, Any], what: str, offset: float = 0.0, glue_joint: str = "stop") -> Route:
    """A straight route across faces, from the first face's edge to the last face's edge.
    `offset` shifts it sideways (inches, in the first face's frame: +y for a horizontal route)."""
    fm = ctx.fm
    try:
        faces = fm.resolve(spec["faces"])
    except ValueError as exc:
        raise FeatureError(f"{what}: {exc}") from None
    f0 = fm.faces[faces[0]]  # the route's own first face gives its reference line, even with the lug added
    if glue_joint == "cross" and "glue_lug" not in faces and "glue_lug" in fm.faces:
        # A P1-to-P4 wrap carries on over the lug, beyond P4, whichever way the route runs.
        if faces[0] == "P1" and faces[-1] == "P4":
            faces = faces + ["glue_lug"]
        elif faces[0] == "P4" and faces[-1] == "P1":
            faces = ["glue_lug"] + faces
    axis = "y" if spec["direction"] == "horizontal" else "x"
    c, s, name = pl._ref(f0, spec["at"]["from"], axis, "assembled", what)
    dist = ctx.length(spec["at"]["distance"], f"{what} route.at.distance")
    if s != 0 and dist < -TOL:
        raise FeatureError(f"{what}: route distance from {name} must run inward (0 or more)")
    v = c + (s * dist if s else dist) + offset
    if spec["direction"] == "horizontal":
        p0, p1 = f0.to_flat((0.0, v)), f0.to_flat((1.0, v))
    else:
        p0, p1 = f0.to_flat((v, 0.0)), f0.to_flat((v, 1.0))
    u = (p1[0] - p0[0], p1[1] - p0[1])
    spans = []
    for fid in faces:
        iv = _clip(fm.faces[fid].polygon, p0, u)
        if iv is None:
            raise FeatureError(f"{what}: the route misses {fid} (at {name} + {dist:.4g} in on {f0.id})")
        spans.append(iv)
    if len(spans) > 1 and spans[1][0] < spans[0][0]:
        u = (-u[0], -u[1])
        spans = [(-b, -a) for a, b in spans]
    pieces: list[tuple[Point, Point, float]] = []
    crossings: list[tuple[float, str]] = []
    t_start = spans[0][0]
    run_start = spans[0][0]
    ends = ["outline", "outline"]
    at = lambda t: (p0[0] + u[0] * t, p0[1] + u[1] * t)  # noqa: E731
    for i in range(1, len(faces)):
        a, b = spans[i - 1], spans[i]
        fa, fb = faces[i - 1], faces[i]
        if b[0] < a[1] - 1e-6:
            raise FeatureError(f"{what}: {fb} doesn't follow {fa} along the route; list the faces in order")
        gap = b[0] - a[1]
        hinge = next((h for h in fm.hinges.values() if {h.parent, h.child} == {fa, fb}), None)
        if gap > 0.5:
            between = [f for f in fm.faces if f not in faces and _clip(fm.faces[f].polygon, p0, u) is not None
                       and a[1] - 1e-6 <= _clip(fm.faces[f].polygon, p0, u)[0] <= b[0] + 1e-6]  # type: ignore[index]
            raise FeatureError(f"{what}: {fa} and {fb} aren't joined by a hinge"
                               + (f" ({', '.join(between)} is between them; list it too)" if between else ""))
        if gap <= 1e-6:
            if hinge is None:
                raise FeatureError(f"{what}: {fa} and {fb} aren't joined by a hinge")
            pt = at(a[1])
            ha, hb = hinge.start, hinge.end
            # the crossing must be on the crease, not in a slot past its end
            L = math.dist(ha, hb)
            tt = ((pt[0] - ha[0]) * (hb[0] - ha[0]) + (pt[1] - ha[1]) * (hb[1] - ha[1])) / (L * L)
            if not (1e-6 < tt < 1 - 1e-6):
                raise FeatureError(f"{what}: the route crosses from {fa} to {fb} at the end of their score, where the "
                                   "slot is; move it")
            crossings.append((a[1] - t_start, hinge.id))
        else:
            if spec.get("slots") != "jump":
                raise FeatureError(f"{what}: between {fa} and {fb} there's a {gap:.4g} in gap (a slot); say "
                                   "route.slots: jump to jump it, or end the route on " + fa)
            pieces.append((at(run_start), at(a[1]), run_start - t_start))
            run_start = b[0]
    pieces.append((at(run_start), at(spans[-1][1]), run_start - t_start))
    total = spans[-1][1] - t_start
    # What each end lands on: the face edge there is a crease (another face's hinge) or a cut.
    for k, (fid, t) in enumerate(((faces[0], spans[0][0]), (faces[-1], spans[-1][1]))):
        ends[k] = _edge_kind(ctx.d, at(t))
    return Route(pieces, faces, crossings, total, (ends[0], ends[1]))


def _edge_kind(d: Dieline, p: Point) -> str:
    for s in d.segments:
        if isinstance(s, Line) and s.type in (LineType.CUT, LineType.CREASE):
            (ax, ay), (bx, by) = s.start, s.end
            L2 = (bx - ax) ** 2 + (by - ay) ** 2
            t = max(0.0, min(1.0, ((p[0] - ax) * (bx - ax) + (p[1] - ay) * (by - ay)) / L2))
            if math.dist(p, (ax + t * (bx - ax), ay + t * (by - ay))) <= 1e-5:
                return "crease" if s.type == LineType.CREASE else "outline"
    return "board"


def _rule_op(b: Built, oid: str, kind: str, ctx: Ctx, rule: RuleSpec | None, extra_ends: tuple[str, ...] = ()
             ) -> Operation:
    return Operation(oid, kind, open_ends="on", ends_on=("outline", "hinge", "flap_hinge") + extra_ends,
                     crosses=("hinge", "score", "perf", "flap_hinge"), rule=rule)


def _draft_rule(b: Built, ctx: Ctx) -> None:
    from .checks import fmt_in  # noqa: PLC0415

    spec = b.spec
    r = route(ctx, spec["route"], b.name)
    if b.kind == "score":
        rule = _rule(b, "crease", ctx, side=spec["side"]) if spec["side"] == "reverse" else _rule(b, "crease", ctx)
        op = _rule_op(b, b.id, "score", ctx, rule)
        t = LineType.CREASE
    else:
        cut = ctx.size(spec["cut"], f"{b.name} cut")
        tie = ctx.size(spec["tie"], f"{b.name} tie")
        phase = ctx.length(spec["phase"], f"{b.name} phase")
        rule = RuleSpec("perforation", float(spec.get("pointage") or ctx.d.meta.get("pointage", 2.0)),
                        cut=cut, tie=tie, phase=phase, crossing=spec["crossing"])
        op = _rule_op(b, b.id, "perf", ctx, rule)
        t = LineType.PERFORATION
    b.extra_ops.append(op)
    segs = []
    for a, e, st in r.pieces:
        seg = Line(a, e, t, op.id)
        segs.append(seg)
        b.stations[seg] = st
    b.copies.append(Placed(r.faces[0], 1, 1, segs, segs, faces=r.faces, crossings=r.crossings,
                           extra={"route": r}))
    a = spec["route"]["at"]
    side = " (reverse)" if spec.get("side") == "reverse" else ""
    what = "extra score" + side if b.kind == "score" else (f"perforation {fmt_in(rule.cut)} cut / "  # type: ignore[union-attr]
                                                            f"{fmt_in(rule.tie)} tie")  # type: ignore[union-attr]
    b.words = (f"{what}, {spec['route']['direction']}, {a['distance']} from {r.faces[0]}'s {a['from']}, across "
               f"{', '.join(r.faces)} ({fmt_in(round(r.length * 1024) / 1024)} in long)")


def _draft_tear(b: Built, ctx: Ctx) -> None:
    from .checks import fmt_in  # noqa: PLC0415

    spec = b.spec
    w = ctx.size(spec["width"], f"{b.name} width")
    tab_len = ctx.size(spec["tab_length"], f"{b.name} tab_length")
    rails = []
    for sign in (1, -1):
        rails.append(route(ctx, spec["route"], b.name, offset=sign * w / 2, glue_joint=spec["glue_joint"]))
    centre = route(ctx, spec["route"], b.name, glue_joint=spec["glue_joint"])
    if spec["start"] == "end":
        rails = [_reverse(r) for r in rails]
        centre = _reverse(centre)
    if rails[0].faces != rails[1].faces:
        raise FeatureError(f"{b.name}: the two rails cross different faces ({', '.join(rails[0].faces)} / "
                           f"{', '.join(rails[1].faces)}); move the strip so both stay on the same faces")
    if rails[0].pieces[0][1] == rails[0].pieces[0][0]:
        raise FeatureError(f"{b.name}: the strip has no length")
    perf = spec["rails"] == "perf"
    t = LineType.PERFORATION if perf else LineType.SCORE
    p = float(spec.get("pointage") or ctx.d.meta.get("pointage", 2.0))
    rule = RuleSpec("perforation", p, cut=PERF_CUT, tie=PERF_TIE) if perf else RuleSpec("cut_score", p)
    ids = {k: f"{b.id}.{k}" for k in ("rail_a", "rail_b", "tab")}
    segs: list[Segment] = []
    ops: list[Operation] = []
    start_cut = tab_len  # where the rails begin
    tab = spec["tab"]
    ux, uy = _dir(centre)
    nx, ny = -uy, ux
    origin = centre.pieces[0][0]
    pt = lambda s, o: (origin[0] + ux * s + nx * o, origin[1] + uy * s + ny * o)  # noqa: E731
    # Which side each rail is on (the offsets were in the face frame; the flat frame may mirror them).
    side_a = (rails[0].pieces[0][0][0] - origin[0]) * nx + (rails[0].pieces[0][0][1] - origin[1]) * ny
    oa, ob = (w / 2, -w / 2) if side_a > 0 else (-w / 2, w / 2)
    # Where each rail starts along the strip, from the centre line's start: not 0 where the strip
    # starts on a slanted edge (the glue lug's taper), so tabs and rails meet on each rail's own line.
    lead = [(r.pieces[0][0][0] - origin[0]) * ux + (r.pieces[0][0][1] - origin[1]) * uy for r in rails]
    starts = [start_cut - x for x in lead]  # in each rail's own stations
    tab_rule = _rule(b, "cut", ctx)  # the strip's pointage, on its tab cuts too
    tab_ops: list[Operation] = []
    tab_at = pt(tab_len / 2, 0.0)
    if tab == "edge_tab":
        if centre.ends[0] != "outline":
            raise FeatureError(f"{b.name}: an edge tab needs the strip to start at a cut edge of the blank; it starts "
                               "on a score here. Start it at the free edge or use tab angled_tab or thumb_hole")
        for k, (key, o) in enumerate((("tab_a", oa), ("tab_b", ob))):
            op = Operation(f"{b.id}.{key}", "tab_cut", open_ends="on",
                           ends_on=("outline", ids["rail_a" if key == "tab_a" else "rail_b"]), rule=tab_rule)
            tab_ops.append(op)
            segs.append(Line(pt(lead[k], o), pt(lead[k] + tab_len, o), LineType.CUT, op.id))
            starts[k] = tab_len  # each tab runs tab_len in from its own rail's start
        tab_at = pt((lead[0] + lead[1]) / 2 + tab_len / 2, 0.0)
    elif tab == "angled_tab":
        apex = tab_len - min(w / 2, max(tab_len - 1 / 8, 0.0))
        op = Operation(ids["tab"], "tab_cut", open_ends="on", ends_on=(ids["rail_a"], ids["rail_b"]), rule=tab_rule)
        tab_ops.append(op)
        segs += [Line(pt(tab_len, oa), pt(apex, 0.0), LineType.CUT, op.id),
                 Line(pt(apex, 0.0), pt(tab_len, ob), LineType.CUT, op.id)]
    else:  # thumb hole: a circle on the strip's start; the rails start where they leave it
        R = w / 2 + 1 / 8
        sc = R + CLEARANCE_IN
        op = Operation(ids["tab"], "hole", closed=True, rule=tab_rule)
        tab_ops.append(op)
        cc = pt(sc, 0.0)
        segs.append(Arc((cc[0] + R, cc[1]), (cc[0] + R, cc[1]), cc, False, LineType.CUT, True, op.id))
        starts = [sc + math.sqrt(R * R - (w / 2) ** 2) - x for x in lead]
        tab_at = cc  # the hole is the tab
    for k, (key, rail, o) in enumerate((("rail_a", rails[0], oa), ("rail_b", rails[1], ob))):
        ends = tuple(x.id for x in tab_ops)
        op = _rule_op(b, ids[key], "perf" if perf else "score", ctx, rule, ends)
        ops.append(op)
        for a_, e_, st in rail.pieces:
            L = math.dist(a_, e_)
            lo = max(starts[k] - st, 0.0)
            if lo >= L - 1e-9:
                continue
            ra = (a_[0] + (e_[0] - a_[0]) * lo / L, a_[1] + (e_[1] - a_[1]) * lo / L)
            seg = Line(ra, e_, t, op.id)
            segs.append(seg)
            b.stations[seg] = st + lo
    for s in segs:
        if s.op in (ids["tab"], f"{b.id}.tab_a", f"{b.id}.tab_b"):
            face = ctx.fm.faces[centre.faces[0]]
            why = _inside_face(face, [s])
            if why:
                raise FeatureError(f"{b.name}'s pull tab is outside {face.id}: {why}")
    b.extra_ops += tab_ops + ops
    b.copies.append(Placed(centre.faces[0], 1, 1, segs, segs, faces=centre.faces, crossings=centre.crossings,
                           extra={"route": centre, "width": w, "tab": tab, "tab_at": tab_at,
                                  "tab_len": tab_len, "rails": spec["rails"], "glue_joint": spec["glue_joint"]}))
    a = spec["route"]["at"]
    b.words = (f"{fmt_in(w)} in tear strip ({'perforated' if perf else 'cut-score'} rails, {tab.replace('_', ' ')} "
               f"at the {spec['start']}), {spec['route']['direction']}, centred {a['distance']} from "
               f"{centre.faces[0]}'s {a['from']}, across {', '.join(centre.faces)}")


def _dir(r: Route) -> tuple[float, float]:
    a, e, _ = r.pieces[0]
    L = math.dist(a, e)
    return ((e[0] - a[0]) / L, (e[1] - a[1]) / L)


def _reverse(r: Route) -> Route:
    total = r.length
    pieces = [(e, a, total - (st + math.dist(a, e))) for a, e, st in reversed(r.pieces)]
    return Route(pieces, list(reversed(r.faces)), [(total - s, h) for s, h in reversed(r.crossings)], total,
                 (r.ends[1], r.ends[0]))


# ------------------------------------------------------------------ notches


def _draft_notch(b: Built, ctx: Ctx) -> None:
    """Resolve where each notch goes; the outline is changed in _apply_notches."""
    from .checks import fmt_in  # noqa: PLC0415

    spec = b.spec
    fm = ctx.fm
    try:
        faces = fm.resolve(spec["on"])
    except ValueError as exc:
        raise FeatureError(f"{b.name}: {exc}") from None
    w = ctx.size(spec["width"], f"{b.name} width")
    depth = w / 2 if spec["shape"] == "half_circle" else ctx.size(spec["depth"], f"{b.name} depth")
    radius = ctx.length(spec["corner_radius"], f"{b.name} corner_radius") if spec.get("corner_radius") else None
    prof = sh.notch(spec["shape"], w, depth, radius)
    pattern = spec["pattern"]
    n = pattern.get("count", 1)
    pitch = ctx.length(pattern["pitch"], f"{b.name} pattern.pitch") if n > 1 else 0.0
    if n > 1 and pitch <= 0:
        raise FeatureError(f"{b.name}: pattern.pitch must be above zero")
    mode, mirrors = pl._pairing(fm, faces, {k: spec[k] for k in ("mirror", "pairs") if k in spec})
    b.notch_plan = []
    for fid in faces:
        f = fm.faces[fid]
        edge = _edge_side(f, spec["edge"], b.name)
        along_axis = "x" if edge in ("top", "bottom") else "y"
        along = spec["along"]
        src = mirrors.get(fid)
        if src is not None and along_axis == "x":
            along = pl._mirror_axis(fm.faces[src], f, along)
        box = (-w / 2, -w / 2, w / 2, w / 2)
        offs = [(i * pitch, 0.0) if along_axis == "x" else (0.0, -i * pitch) for i in range(n)]
        pos, callout = pl._axis_pos(f, along_axis, along, box, offs, ctx.env, ctx.k, "assembled", b.name, True, None)
        for i, o in enumerate(offs):
            centre_along = pos + (o[0] if along_axis == "x" else o[1])
            b.notch_plan.append((f, edge, centre_along, prof, w, depth, i + 1, n, callout if i == 0 else None,
                                 src))
    b.words = (f"{fmt_in(w)} in {spec['shape'].replace('_', ' ')} notch" +
               (f", {fmt_in(depth)} in deep" if spec["shape"] != "half_circle" else "") +
               f" in the {spec['edge']} of {', '.join(faces)}" +
               (f", {n} at {fmt_in(pitch)} in pitch" if n > 1 else "") +
               ("" if spec["along"] == "centred" else
                f", {spec['along']['distance']} from {spec['along']['from']} to its "
                f"{spec['along']['to'].replace('_', ' ')}"))


def _edge_side(f: Face, name: str, what: str) -> str:
    if name in ("top", "bottom", "left", "right"):
        return name
    ln = f.lines.get(name)
    if ln is None:
        raise FeatureError(f"{what}: {f.id} has no edge {name!r}; edges: top, bottom, left, right, "
                           + ", ".join(n for n in f.lines if n not in ("top", "bottom", "left", "right",
                                                                       "centre_x", "centre_y")))
    for side in ("top", "bottom", "left", "right"):
        if f.lines[side].start == ln.start and f.lines[side].end == ln.end:
            return side
    raise FeatureError(f"{what}: {name} isn't one of {f.id}'s sides")


def _apply_notches(d: Dieline, feats: list[Built]) -> None:
    """Every notch on the outline, edge by edge in one pass: each must sit on a
    straight cut span, clear of its ends and of the other notches there."""
    from .checks import fmt_in  # noqa: PLC0415

    corner = 1 / 8
    plans: dict[int, list[tuple[float, float, list[sh.Prim], Built, Any]]] = {}
    outline_idx = [i for i, s in enumerate(d.segments) if s.op == "outline" and isinstance(s, Line)]
    for b in feats:
        b.copies = []
        b.notch_segs = []
        for f, edge, c_along, prof, w, depth, idx, of, callout, src in b.notch_plan:
            # The notch's centre on the edge, and the edge's inward direction, in the face frame.
            ln = f.lines[edge]
            if edge in ("top", "bottom"):
                p_face, along_dir = (c_along, ln.start[1]), (1.0, 0.0)
            else:
                p_face, along_dir = (ln.start[0], c_along), (0.0, 1.0)
            pc = f.to_flat(p_face)
            pa = f.to_flat((p_face[0] - along_dir[0] * w / 2, p_face[1] - along_dir[1] * w / 2))
            pb = f.to_flat((p_face[0] + along_dir[0] * w / 2, p_face[1] + along_dir[1] * w / 2))
            where = f"{b.name}" + (f" ({idx} of {of} on {f.id})" if of > 1 else f" on {f.id}")
            hit = None
            for i in outline_idx:
                s = d.segments[i]
                if _on_line(s, pa) and _on_line(s, pb):
                    hit = i
                    break
            if hit is None:
                kinds = {e.kind for e in f.edges if e.side == edge}
                if "crease" in kinds and not any(e.kind == "cut" and _span_has(e, f, p_face, edge, w) for e in f.edges
                                                  if e.side == edge):
                    raise NotchError(b.id, f"{where}: {f.id}'s {edge} is a crease there, not a cut edge; a notch goes on "
                                       "a straight cut edge")
                raise NotchError(b.id, f"{where} isn't on one straight cut span of {f.id}'s {edge} (it runs past a corner, "
                                   "a slot or a curve); move it along the edge or make it narrower")
            s = d.segments[hit]
            L = s.length
            ux, uy = (s.end[0] - s.start[0]) / L, (s.end[1] - s.start[1]) / L
            ta = (pa[0] - s.start[0]) * ux + (pa[1] - s.start[1]) * uy
            tb = (pb[0] - s.start[0]) * ux + (pb[1] - s.start[1]) * uy
            lo, hi = min(ta, tb), max(ta, tb)
            # Clear of the ends of the straight cut, and of the face's own corners along it (where a
            # score meets the edge, the outline itself may run on past them).
            k_ = 0 if edge in ("top", "bottom") else 1
            span = f.width if k_ == 0 else f.height
            if lo < corner - 1e-9 or hi > L - corner + 1e-9 or c_along - w / 2 < corner - 1e-9 \
                    or c_along + w / 2 > span - corner + 1e-9:
                raise NotchError(b.id, f"{where} is within {fmt_in(corner)} in of the end of the straight edge; move it "
                                   "clear of the corner")
            inward = f.to_flat((p_face[0] + ln.inward[0], p_face[1] + ln.inward[1]))  # type: ignore[index]
            nx, ny = inward[0] - pc[0], inward[1] - pc[1]
            if depth >= (f.height if edge in ("top", "bottom") else f.width) - 1e-6:
                raise NotchError(b.id, f"{where} is deeper than the face")
            plans.setdefault(hit, []).append((lo, hi, prof, b, (pc, (ux, uy), (nx, ny), idx, of, f.id, callout, src)))
    # Split each outline line once, in order along it.
    replace: dict[int, list[Segment]] = {}
    for i, items in plans.items():
        s = d.segments[i]
        items.sort(key=lambda x: x[0])
        for (alo, ahi, *_), (blo, bhi, _p, bb, info) in zip(items, items[1:]):
            if blo < ahi + corner - 1e-9:
                raise NotchError(bb.id, f"{bb.name}: two notches on one edge are closer than {fmt_in(corner)} in; space "
                                   "them out")
        L = s.length
        ux, uy = (s.end[0] - s.start[0]) / L, (s.end[1] - s.start[1]) / L
        at = lambda t: (s.start[0] + ux * t, s.start[1] + uy * t)  # noqa: E731
        out: list[Segment] = []
        cur = 0.0
        for lo, hi, prof, bb, (pc, (eu, ev), (nx, ny), idx, of, fid, callout, src) in items:
            if lo - cur > 1e-9:
                out.append(Line(at(cur), at(lo), LineType.CUT, "outline"))
            # The profile runs from (-w/2, 0) to (w/2, 0) in edge coordinates (x along, y inward);
            # orient it to run with the outline (from lo to hi).
            def f_(p, pc=pc, ux=ux, uy=uy, nx=nx, ny=ny):
                return (pc[0] + ux * p[0] + nx * p[1], pc[1] + uy * p[0] + ny * p[1])
            reflects = (ux * ny - uy * nx) < 0
            mapped = [q.map(f_, reflects) for q in prof]
            # Snap the profile's two ends exactly onto the split points.
            first, last = mapped[0], mapped[-1]
            mapped[0] = sh.Prim(at(lo), first.end, first.center, first.clockwise) if len(mapped) > 1 else \
                sh.Prim(at(lo), at(hi), first.center, first.clockwise)
            if len(mapped) > 1:
                mapped[-1] = sh.Prim(last.start, at(hi), last.center, last.clockwise)
            segs = [m.segment(LineType.CUT, "outline") for m in mapped]
            out += segs
            bb.notch_segs += segs
            bb.copies.append(Placed(fid, idx, of, [], segs, [callout] if callout else [], src))
            cur = hi
        if L - cur > 1e-9:
            out.append(Line(at(cur), at(L), LineType.CUT, "outline"))
        replace[i] = out
    if replace:
        new: list[Segment] = []
        for i, s in enumerate(d.segments):
            new += replace.get(i, [s])
        d.segments = new


def _on_line(s: Line, p: Point) -> bool:
    (ax, ay), (bx, by) = s.start, s.end
    L2 = (bx - ax) ** 2 + (by - ay) ** 2
    t = ((p[0] - ax) * (bx - ax) + (p[1] - ay) * (by - ay)) / L2
    if t < -1e-9 or t > 1 + 1e-9:
        return False
    return math.dist(p, (ax + t * (bx - ax), ay + t * (by - ay))) <= 1e-6


def _span_has(e: Any, f: Face, p: Point, edge: str, w: float) -> bool:
    i = 0 if edge in ("top", "bottom") else 1
    lo, hi = sorted((e.start[i], e.end[i]))
    return lo - 1e-9 <= p[i] - w / 2 and p[i] + w / 2 <= hi + 1e-9

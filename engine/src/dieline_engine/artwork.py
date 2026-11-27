"""Artwork on standard parts: cut-outs that aren't structure (docs/DESIGN-SYSTEM.md,
"Artwork"). A smiley, a logo, lettering, a decorative window.

Artwork is always decorative: it sits on one face, clear of every hinge, slot
and edge; it can't change the outline, be a mate or be referenced by a
selector. It's drawn after the features (source -> features -> artwork ->
checks), from one of four sources (artwork_sources.py, artwork_import.py):

- the shape library and stencil / stroke text ("library artwork")
- the agent's sketch on a 0-100 canvas ("agent sketch": labelled agent-drawn
  artwork everywhere, verify before cutting)
- an imported SVG or DXF, approved by the designer ("imported artwork")

Each closed loop has a rule: cut (default), crease, perforation or partial cut
(every rule other than cut goes through the export capability table). Open
paths are never closed silently.

**Islands and bridges.** Retained and discarded regions are worked out across
every artwork loop and feature hole on the face together (even-odd nesting:
inside the outline is board, inside a hole is scrap, inside a loop in a hole
is board again). Board inside a hole is an island. The fixes, chosen per
island: bridges (at least two corridors of retained board, each at least
max(3/16", 1.5 x CAL) wide, across the flutes where they can go, with the cuts
redrawn round them and checked again), stencil breaks (bridges by another
name), a partial cut (the hole's outer loop becomes a partial cut, so the
board round the island stays) or an accepted drop-out. An unhandled island is a
`fail`: drawn, export blocked.

Stored on the part (BoxParams.artwork) as plain JSON, each with an engine-made
id (a1, a2, ...).
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from . import artwork_sources as src
from . import placement as pl
from .expressions import ExpressionError
from .faces import Face, FaceMap
from .features import Ctx, FeatureError, _env, _flat_prims, _inside_face, _retype, friendly
from .geometry import PRECISION, Arc, Dieline, Line, LineType, Operation, Point, Segment, _round_pt
from .rules import PERF_CUT, PERF_TIE, RuleSpec
from .shapes import Prim, bbox as prim_bbox, transform

KINDS = ("shape", "stencil_text", "stroke_text", "sketch", "import")
RULES = ("cut", "crease", "perforation", "partial_cut", "annotation")
FIXES = ("bridge", "stencil", "partial_cut", "drop_out")
MAX_ARTWORK = 20
EDGE_CLEARANCE_IN = 0.25
BRIDGE_MIN_IN = 3 / 16
PROVENANCE = {"library": "library artwork", "import": "imported artwork", "sketch": "agent sketch"}
RULE_LINE = {"cut": LineType.CUT, "crease": LineType.CREASE, "perforation": LineType.PERFORATION,
             "partial_cut": LineType.SCORE, "annotation": LineType.ANNOTATION}
PROVISIONAL = {
    "bend_radius": "1/8 in minimum bend radius (3/16 in for 3 pt rule on double wall)",
    "acute": "35 deg minimum angle at a corner",
    "short": "1/8 in shortest straight between corners",
    "web": "max(1/8 in, 2 x CAL) thinnest web between cuts",
    "edge": "1/4 in from the face's edges",
    "bridge": "bridges max(3/16 in, 1.5 x CAL) wide, two per island",
    "flags": "suspicious-artwork thresholds (long straights 1 in, slot-like 3:1 under 1/2 in, 80% span, 50% rules)",
}

FIELDS: dict[str, set[str]] = {
    "shape": {"shape", "width", "height", "diameter", "corner_radius", "sides", "points", "inner_ratio", "outline",
              "length", "head_length", "shaft_width"},
    "stencil_text": {"text", "height", "spacing"},
    "stroke_text": {"text", "height", "spacing"},
    "sketch": {"sketch", "width", "height", "closed"},
    "import": {"path", "width", "height", "fit_box", "units", "closed"},
}
COMMON = {"id", "kind", "label", "rule", "loop_rules", "islands", "bridge_width", "placement", "bound", "pointage"}
TRUSTED = {"source"}  # an import's stored file facts and geometry: only from the designer's approval or a record
UNITS = ("in", "mm", "cm", "pt", "px")
UNITS_IN = {"in": 1.0, "mm": 1 / 25.4, "cm": 1 / 2.54, "pt": 1 / 72, "px": 1 / 96}


class ArtworkError(ValueError):
    """An artwork's input is wrong: says what to change."""


# ------------------------------------------------------------------ input


def _length(item: dict, key: str, what: str) -> Any:
    v = item.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise ArtworkError(f"{what}.{key} must be a length: a number, a fraction like '1 3/8' or an expression")
    return v


def normalize(item: Any, aid: str | None = None, *, trusted: bool = False) -> dict[str, Any]:
    """Check one artwork item's shape (its geometry is checked when it's built) and fill defaults.
    Untrusted input (the agent) can't carry an import's stored geometry."""
    if not isinstance(item, dict):
        raise ArtworkError("each artwork item must be an object with a kind")
    kind = item.get("kind")
    what = f"artwork {aid or item.get('id') or '(new)'}"
    if kind not in KINDS:
        raise ArtworkError(f"{what}.kind must be one of {', '.join(KINDS)}")
    allowed = FIELDS[kind] | COMMON | (TRUSTED if kind == "import" else set())
    unknown = sorted(set(item) - allowed)
    if unknown:
        raise ArtworkError(f"{what} has no field(s) {', '.join(unknown)}; a {kind} takes "
                           f"{', '.join(sorted(FIELDS[kind] | (COMMON - {'id', 'kind', 'bound'})))}")
    if not trusted and "source" in item:
        raise ArtworkError(f"{what}: an import's file and geometry come only from the designer's approval of the "
                           "import; give its path instead")
    out: dict[str, Any] = {"kind": kind}
    if aid or item.get("id"):
        out["id"] = aid or item["id"]
    label = item.get("label")
    if label is not None:
        if not isinstance(label, str) or not label.strip() or len(label) > 60:
            raise ArtworkError(f"{what}.label must be a short name")
        out["label"] = label.strip()
    rule = item.get("rule")
    if rule is not None:
        if rule not in RULES:
            raise ArtworkError(f"{what}.rule must be one of {', '.join(RULES)}")
        if kind == "stroke_text" and rule in ("cut", "perforation", "partial_cut"):
            raise ArtworkError(f"{what}: stroke text is single lines; cutting them would make hairline slits, so "
                               "it can only be a crease (embossed) or annotation. Use stencil_text for cut lettering")
        out["rule"] = rule
    lr = item.get("loop_rules")
    if lr is not None:
        if not isinstance(lr, dict) or len(lr) > 600:
            raise ArtworkError(f"{what}.loop_rules must map outline numbers to rules, e.g. {{\"2\": \"crease\"}}")
        clean = {}
        for k, v in lr.items():
            if not str(k).isdigit() or int(k) < 1 or v not in RULES:
                raise ArtworkError(f"{what}.loop_rules: {k!r}: {v!r} isn't an outline number and a rule "
                                   f"({', '.join(RULES)})")
            if kind == "stroke_text" and v in ("cut", "perforation", "partial_cut"):
                raise ArtworkError(f"{what}: stroke text can only be a crease or annotation")
            clean[str(int(k))] = v
        out["loop_rules"] = clean
    isl = item.get("islands")
    if isl is not None:
        if isinstance(isl, str):
            if isl not in FIXES:
                raise ArtworkError(f"{what}.islands must be one of {', '.join(FIXES)}, or a map from island ids "
                                   "(i1, i2, ...) to those")
        elif isinstance(isl, dict):
            for k, v in isl.items():
                if not (isinstance(k, str) and k.startswith("i") and k[1:].isdigit()) or v not in FIXES:
                    raise ArtworkError(f"{what}.islands: {k!r}: {v!r} isn't an island id and one of "
                                       f"{', '.join(FIXES)}")
        else:
            raise ArtworkError(f"{what}.islands must be one of {', '.join(FIXES)} or a map from island ids")
        out["islands"] = isl
    bw = _length(item, "bridge_width", what)
    if bw is not None:
        out["bridge_width"] = bw
    if "pointage" in item:
        p = item["pointage"]
        if isinstance(p, bool) or not isinstance(p, (int, float)) or not 1 <= p <= 12:
            raise ArtworkError(f"{what}.pointage must be a rule thickness in points (2, 3, ...)")
        out["pointage"] = float(p)
    if isinstance(item.get("bound"), dict):
        out["bound"] = {str(k): str(v) for k, v in item["bound"].items()}
    try:
        out["placement"] = pl.normalize(item.get("placement"), what=f"{what}.placement")
    except pl.PlacementError as exc:
        raise ArtworkError(str(exc)) from None
    if out["placement"]["pattern"]["kind"] != "single":
        raise ArtworkError(f"{what}: artwork is one piece on one face; add another artwork item for another copy")
    if out["placement"].get("mirror") not in (None, "none"):
        raise ArtworkError(f"{what}: artwork goes on one face, so it doesn't mirror")
    if kind == "shape":
        shape = item.get("shape")
        if shape not in src.LIBRARY:
            raise ArtworkError(f"{what}.shape must be one of {', '.join(src.LIBRARY)}")
        out["shape"] = shape
        for key in ("width", "height", "diameter", "corner_radius", "length", "head_length", "shaft_width"):
            v = _length(item, key, what)
            if v is not None:
                out[key] = v
        need = {"circle": ("diameter",), "ellipse": ("width", "height"), "rounded_rect": ("width", "height"),
                "polygon": ("diameter",), "star": ("diameter",), "heart": ("width", "height"),
                "smiley": ("diameter",), "arrow": ("length", "width")}[shape]
        missing = [k for k in need if k not in out]
        if missing:
            raise ArtworkError(f"{what}: a {shape} needs {' and '.join(missing)}")
        for key in ("sides", "points"):
            if key in item:
                out[key] = item[key]
        if "inner_ratio" in item:
            out["inner_ratio"] = item["inner_ratio"]
        if "outline" in item:
            if item["outline"] not in ("none", "crease"):
                raise ArtworkError(f"{what}.outline must be none or crease (a smiley's face outline)")
            out["outline"] = item["outline"]
    elif kind in ("stencil_text", "stroke_text"):
        try:
            from .glyphs import check_text  # noqa: PLC0415

            check_text(item.get("text"))
        except ValueError as exc:
            raise ArtworkError(f"{what}: {exc}") from None
        out["text"] = item["text"]
        h = _length(item, "height", what)
        if h is None:
            raise ArtworkError(f"{what}: text needs its height (the capital height)")
        out["height"] = h
        sp = _length(item, "spacing", what)
        if sp is not None:
            out["spacing"] = sp
    elif kind == "sketch":
        sk = item.get("sketch")
        if not isinstance(sk, dict) or set(sk) - {"shapes", "paths"} or not (sk.get("shapes") or sk.get("paths")):
            raise ArtworkError(f"{what}.sketch must be {{shapes, paths}} on the 0-100 canvas")
        for key in ("shapes", "paths"):
            if key in sk and not isinstance(sk[key], list):
                raise ArtworkError(f"{what}.sketch.{key} must be a list")
        if len(json.dumps(sk)) > 60_000:
            raise ArtworkError(f"{what}: the sketch is too large; simplify it")
        out["sketch"] = copy.deepcopy(sk)
        _size(item, out, what, ("width", "height"), required=True)
        _closed(item, out, what)
    else:  # import
        path = item.get("path")
        if path is not None:
            if not isinstance(path, str) or not path.strip():
                raise ArtworkError(f"{what}.path must be a file path from search_files")
            out["path"] = path
        _size(item, out, what, ("width", "height", "fit_box"), required=False)
        if "units" in item:
            if item["units"] not in UNITS:
                raise ArtworkError(f"{what}.units must be one of {', '.join(UNITS)} (for a file without units)")
            out["units"] = item["units"]
        _closed(item, out, what)
        source = item.get("source")
        if source is not None:
            if not isinstance(source, dict) or not isinstance(source.get("geometry"), list):
                raise ArtworkError(f"{what}: an import's stored source is malformed")
            out["source"] = copy.deepcopy(source)
        elif trusted and "path" not in out:
            raise ArtworkError(f"{what}: an import needs its path")
    return out


def _size(item: dict, out: dict, what: str, keys: tuple[str, ...], required: bool) -> None:
    given = [k for k in keys if item.get(k) is not None]
    if len(given) > 1:
        raise ArtworkError(f"{what}: give one of {', '.join(keys)} (scaling is uniform)")
    if not given:
        if required:
            raise ArtworkError(f"{what}: give its {' or '.join(keys)} (it's scaled uniformly to that)")
        return
    k = given[0]
    if k == "fit_box":
        v = item[k]
        if not isinstance(v, list) or len(v) != 2:
            raise ArtworkError(f"{what}.fit_box must be [width, height]")
        out[k] = list(v)
    else:
        out[k] = _length(item, k, what)


def _closed(item: dict, out: dict, what: str) -> None:
    if "closed" in item:
        if not isinstance(item["closed"], bool):
            raise ArtworkError(f"{what}.closed must be true or false")
        if item["closed"]:
            out["closed"] = True


def upsert(existing: list[dict[str, Any]], items: list[Any] | None, remove: list[Any] | None, *,
           trusted: bool = False) -> tuple[list[dict[str, Any]], list[str]]:
    """Batch upsert by id: an item without an id is new (a1, a2, ...), an item with an id changes that
    artwork (fields left out keep their values, so moving it doesn't resend a sketch), `remove` deletes."""
    arts = [copy.deepcopy(a) for a in existing]
    by_id = {a["id"]: a for a in arts}
    items = items or []
    remove = remove or []
    if not isinstance(items, list) or not isinstance(remove, list):
        raise ArtworkError("items and remove must be lists")
    for r in remove:
        if r not in by_id:
            raise ArtworkError(f"no artwork {r!r} to remove; artwork: {', '.join(by_id) or 'none'}")
    arts = [a for a in arts if a["id"] not in remove]
    by_id = {a["id"]: a for a in arts}
    n = max([int(a["id"][1:]) for a in existing if str(a.get("id", ""))[1:].isdigit()] + [0])
    touched: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            raise ArtworkError("each artwork item must be an object")
        aid = item.get("id")
        if aid is None:
            if item.get("kind") == "import" and not trusted:
                raise ArtworkError("an import is shown to the designer first and added only when they approve it; "
                                   "call set_artwork with just that item (kind import, path, placement)")
            n += 1
            aid = f"a{n}"
            new = normalize({k: v for k, v in item.items() if k not in ("id", "bound")}, aid, trusted=trusted)
            arts.append(new)
            by_id[aid] = new
        else:
            if aid not in by_id:
                raise ArtworkError(f"no artwork {aid!r}; artwork: {', '.join(by_id) or 'none'} (leave id out to add "
                                   "a new one)")
            old = by_id[aid]
            if "kind" in item and item["kind"] != old["kind"]:
                raise ArtworkError(f"{aid} is {old['kind']}; remove it and add a {item['kind']} instead")
            if not trusted and ("source" in item or (old["kind"] == "import" and "path" in item
                                                     and item["path"] != old.get("path"))):
                raise ArtworkError(f"{aid}: an import's file can only change through a new import the designer "
                                   "approves; remove it and import the other file")
            merged = {**old, **{k: v for k, v in item.items() if k != "bound"}}
            if isinstance(item.get("placement"), dict) and isinstance(old.get("placement"), dict):
                merged["placement"] = {**old["placement"], **item["placement"]}
            for key in ("width", "height", "fit_box"):  # a new size replaces the old one
                if key in item:
                    for other in ("width", "height", "fit_box", "diameter") if old["kind"] in ("sketch", "import") \
                            else ():
                        if other != key:
                            merged.pop(other, None)
            merged.pop("bound", None)
            new = normalize(merged, aid, trusted=True)
            arts[arts.index(old)] = new
            by_id[aid] = new
        touched.append(aid)
    if len(arts) > MAX_ARTWORK:
        raise ArtworkError(f"a part can have at most {MAX_ARTWORK} artwork items")
    return arts, touched


def bind(arts: list[dict[str, Any]], fm: FaceMap | None, ids: list[str]) -> list[dict[str, Any]]:
    """Record the role of a face named by id, so a later order swap makes the artwork stale."""
    if fm is None:
        return arts
    out = []
    for a in arts:
        if a["id"] in ids:
            on = a["placement"]["on"]
            names = on if isinstance(on, list) else [on]
            bound = {x: fm.faces[x].role for x in names if isinstance(x, str) and x in fm.faces}
            a = {**a, "bound": bound} if bound else {k: v for k, v in a.items() if k != "bound"}
        out.append(a)
    return out


def for_agent(params: dict[str, Any]) -> dict[str, Any]:
    """A part's parameters as the model sees them: an import's stored geometry left out."""
    arts = params.get("artwork")
    if not arts:
        return params
    out = dict(params)
    out["artwork"] = []
    for a in arts:
        a = dict(a)
        if isinstance(a.get("source"), dict):
            s = a["source"]
            a["source"] = {k: s[k] for k in ("name", "format", "sha256") if k in s} | \
                {"geometry": f"{len(s.get('geometry') or [])} outlines (stored)"}
        out["artwork"].append(a)
    out.pop("acknowledged", None)
    return out


# ------------------------------------------------------------------ built records


@dataclass(slots=True)
class Drawn:
    """One outline of an artwork, as drawn on the blank (it may be several loops once bridged)."""

    n: int  # its number in the read-back (1-based)
    rule: str
    loops: list[list[Segment]]
    closed: bool
    kinds: dict[Segment, str] = field(default_factory=dict)  # line | arc | fit | fallback | bridge
    radii: list[tuple[float, Point]] = field(default_factory=list)  # source curvature, flat points
    group: int | None = None
    label: str = ""
    merged_into: int | None = None  # bridged into another outline

    @property
    def segs(self) -> list[Segment]:
        return [s for loop in self.loops for s in loop]


@dataclass(slots=True)
class Island:
    id: str  # i1, i2, ... per artwork
    loop: int  # the outline (n) round the island
    parent: int | None  # the artwork outline (n) round the hole it's in, or None (a feature's hole)
    fix: str | None
    status: str  # open | bridged | partial_cut | drop_out | failed
    reason: str = ""
    point: Point = (0.0, 0.0)
    region: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    bridges: int = 0
    width: float = 0.0
    segs: tuple[Segment, ...] = ()  # the island's own loop as drawn: its exact identity


@dataclass(slots=True)
class Art:
    id: str
    kind: str
    label: str
    spec: dict[str, Any]
    provenance: str = "library"
    status: str = "ok"
    reason: str | None = None
    face: str | None = None
    drawn: list[Drawn] = field(default_factory=list)
    words: str = ""
    notes: list[tuple[str, str]] = field(default_factory=list)
    ops: list[Operation] = field(default_factory=list)
    islands: list[Island] = field(default_factory=list)
    callouts: list[pl.Callout] = field(default_factory=list)
    fallback: int = 0
    bridge_width: float = 0.0
    ghost: list[Segment] = field(default_factory=list)

    @property
    def name(self) -> str:
        return f"{self.label} {self.id}"

    @property
    def agent_drawn(self) -> bool:
        return self.provenance == "sketch"

    def live(self) -> list[Drawn]:
        return [c for c in self.drawn if c.merged_into is None]

    def segs(self) -> list[Segment]:
        return [s for c in self.live() for s in c.segs]

    def fingerprint(self, face: Face | None = None) -> str:
        """Everything that decides what's cut: an acknowledgement is for exactly this. Each segment
        (rule, ends, centre, direction, full circle), each outline's rule spec (pointage), and the face
        it's on with that face's frame on the blank (a resize that keeps the flat coordinates still
        moves the face round it), so any change asks again."""
        h = hashlib.sha1()

        def r6(p: Any) -> Any:
            return None if p is None else tuple(round(v, 6) for v in p)

        h.update(repr(("face", self.face)).encode())
        if face is not None:
            h.update(repr((round(face.width, 6), round(face.height, 6), r6(face.origin), r6(face.x_axis),
                           r6(face.y_axis), [r6(q) for q in face.polygon])).encode())
        for op in sorted(self.ops, key=lambda o: o.id):
            h.update(repr((op.id, op.kind, op.closed, op.rule)).encode())
        for s in sorted(self.segs(), key=lambda s: (s.start, s.end)):
            h.update(repr((s.type.value, s.start, s.end, getattr(s, "center", None), getattr(s, "clockwise", None),
                           getattr(s, "full_circle", None))).encode())
        return h.hexdigest()[:16]

    def to_dict(self, units: str = "in") -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id, "kind": "artwork", "artwork_kind": self.kind, "label": self.label, "status": self.status,
            "faces": [self.face] if self.face else [], "copies": 1, "summary": self.words,
            "selector": self.spec.get("placement", {}).get("on"), "provenance": PROVENANCE[self.provenance],
        }
        if self.agent_drawn:
            out["agent_drawn"] = True
        if self.reason:
            out["reason"] = self.reason
        segs = self.segs() or self.ghost
        if segs:
            from .features import _bounds  # noqa: PLC0415

            b = _bounds(segs)
            out["region"] = [round(v, 4) for v in b]
            out["copy_regions"] = [out["region"]]
        if self.islands:
            out["islands"] = [{"id": i.id, "status": i.status, "fix": i.fix,
                               **({"reason": i.reason} if i.reason else {})}
                              for i in self.islands]
        rules: dict[str, int] = {}
        for c in self.live():
            rules[c.rule] = rules.get(c.rule, 0) + len(c.loops)
        if rules:
            out["rules"] = rules
        return out


# ------------------------------------------------------------------ building


def _bridge_min(cal: float) -> float:
    return max(BRIDGE_MIN_IN, 1.5 * cal)


def _web_min(cal: float) -> float:
    return max(1 / 8, 2 * cal)


def local(spec: dict[str, Any], ctx: Ctx, what: str) -> src.Local:
    """The artwork's outlines in its own frame, in inches."""

    def size(name: str, default: float | None = None) -> float:
        if spec.get(name) is None:
            if default is None:
                raise ArtworkError(f"{what}: needs {name}")
            return default
        return ctx.size(spec[name], f"{what} {name}")

    kind = spec["kind"]
    try:
        if kind == "shape":
            return src.library(spec, size)
        if kind in ("stencil_text", "stroke_text"):
            sp = ctx.size(spec["spacing"], f"{what} spacing") if spec.get("spacing") is not None else None
            return src.text(spec, size("height"), _bridge_min(ctx.cal), _web_min(ctx.cal), sp)
        if kind == "sketch":
            sz = {k: ctx.size(spec[k], f"{what} {k}") for k in ("width", "height") if spec.get(k) is not None}
            return src.sketch(spec, sz, bool(spec.get("closed")))
        return _imported(spec, ctx, what)
    except (FeatureError, ExpressionError) as exc:
        raise ArtworkError(str(exc)) from None


# Fitted imports, by what decides them (geometry, size, closed): a design is built several times a
# request (store, checks, scene), and a large spline is seconds of fitting each time.
_FITTED: "OrderedDict[str, tuple]" = OrderedDict()
_FITTED_MAX = 16
_FITTED_LOCK = threading.Lock()


def _fitted(geometry: Any, sz: dict[str, Any], natural: float | None, closed: bool, what: str) -> tuple:
    from .artwork_import import from_json  # noqa: PLC0415

    key = hashlib.sha256(json.dumps([geometry, sz, natural, closed, what], sort_keys=True,
                                    default=str).encode()).hexdigest()
    with _FITTED_LOCK:
        hit = _FITTED.get(key)
        if hit is not None:
            _FITTED.move_to_end(key)
            return copy.deepcopy(hit)
    subs = from_json(geometry)
    box = src.subs_bbox(subs)
    k = src.scale_for(box, sz, natural, what)
    cs, fallback = src.finish(subs, k, declared_closed=closed, what=what)
    got = (box, k, cs, fallback)
    with _FITTED_LOCK:
        _FITTED[key] = copy.deepcopy(got)
        while len(_FITTED) > _FITTED_MAX:
            _FITTED.popitem(last=False)
    return got


def _imported(spec: dict[str, Any], ctx: Ctx, what: str) -> src.Local:
    from .checks import fmt_in  # noqa: PLC0415

    source = spec.get("source")
    if not isinstance(source, dict):
        raise ArtworkError(f"{what}: this import hasn't been approved yet, so it has no geometry")
    natural = source.get("units_in")
    if natural is None and spec.get("units"):
        natural = UNITS_IN[spec["units"]]
    sz: dict[str, Any] = {}
    if spec.get("fit_box") is not None:
        sz["fit_box"] = [ctx.size(v, f"{what} fit_box") for v in spec["fit_box"]]
    for k in ("width", "height"):
        if spec.get(k) is not None:
            sz[k] = ctx.size(spec[k], f"{what} {k}")
    box, k, cs, fallback = _fitted(source["geometry"], sz, natural, bool(spec.get("closed")), what)
    w, h = (box[2] - box[0]) * k, (box[3] - box[1]) * k
    fmt = str(source.get("format", "file")).upper()
    words = (f"imported {fmt} {source.get('name', '')} (sha256 {str(source.get('sha256', ''))[:12]}), "
             f"{fmt_in(round(w * 64) / 64)} x {fmt_in(round(h * 64) / 64)} in, {len(cs)} outline"
             f"{'s' if len(cs) != 1 else ''}")
    out = src.Local(cs, "import", words, fallback=fallback)
    if source.get("unsupported"):
        out.notes.append(("info", f"imported without what it couldn't read: {'; '.join(source['unsupported'])}"))
    for n in source.get("notes") or []:
        out.notes.append(("info", f"import: {n}"))
    return out


def _rule_of(spec: dict[str, Any], c: src.Contour, n: int) -> str:
    lr = spec.get("loop_rules") or {}
    if str(n) in lr:
        return lr[str(n)]
    if spec.get("rule"):
        return spec["rule"]
    if c.rule:
        return c.rule
    return "crease" if spec["kind"] == "stroke_text" else "cut"


def _opspec(aid: str, n: int, rule: str, closed: bool, pointage: float | None, part_pointage: float,
            ends: tuple[str, ...] = ()) -> Operation:
    p = pointage if pointage is not None else part_pointage
    oid = f"{aid}.{n}"
    if rule == "cut":
        return Operation(oid, "artwork", closed=True, rule=RuleSpec("cut", p) if pointage is not None else None)
    if rule == "crease":
        return Operation(oid, "artwork_crease", closed=closed, open_ends="none" if closed else "free",
                         ends_on=ends, crosses=ends, rule=RuleSpec("crease", p) if pointage is not None else None)
    if rule == "perforation":
        return Operation(oid, "artwork_perf", closed=closed, open_ends="none" if closed else "free",
                         rule=RuleSpec("perforation", p, cut=PERF_CUT, tie=PERF_TIE))
    if rule == "partial_cut":
        return Operation(oid, "artwork_partial", closed=closed, open_ends="none" if closed else "free",
                         rule=RuleSpec("cut_score", p))
    return Operation(oid, "annotation", open_ends="free")


def _draft(a: Art, ctx: Ctx, part_pointage: float) -> None:
    """Place the artwork on its face and draw it (before islands and bridges)."""
    spec = a.spec
    loc = local(spec, ctx, a.name)
    a.provenance = loc.provenance
    a.fallback = loc.fallback
    a.notes = list(loc.notes)
    place = spec["placement"]
    rot = place["rotation_deg"]
    allp = [p for c in loc.contours for p in c.prims]
    box = prim_bbox(transform(allp, rotation_deg=rot))
    try:
        copies = pl.resolve(ctx.fm, place, box, ctx.env, k=ctx.k, what=a.name)
    except (pl.PlacementError, ExpressionError) as exc:
        raise ArtworkError(str(exc)) from None
    if len(copies) != 1:
        raise ArtworkError(f"{a.name} goes on one face; {place['on']!r} is {len(copies)} faces "
                           f"({', '.join(c.face for c in copies)}). Name one face, or add an item per face")
    cp = copies[0]
    face = ctx.fm.faces[cp.face]
    a.face = cp.face
    a.callouts = cp.callouts
    a.drawn = []
    lines: list[str] = []
    groups: dict[int, list[str]] = {}
    for n, c in enumerate(loc.contours, 1):
        rule = _rule_of(spec, c, n)
        if rule == "cut" and spec["kind"] == "stroke_text":
            rule = "crease"
        if not c.closed and rule in ("cut", "perforation", "partial_cut"):
            raise ArtworkError(f"{a.name}: outline {n} ({c.label}) is an open path, and an open path can't be "
                               f"{rule.replace('_', ' ')}: close it (Z, or closed: true to snap ends within 0.005 in) "
                               "or make it a crease or annotation")
        if c.snapped:
            lines.append(f"outline {n}'s ends snapped closed ({c.snapped:.4f} in)")
        src.mark_corners(c)
        mine = transform(c.prims, rotation_deg=cp.rotation_deg, flip=cp.flip, offset=(cp.x, cp.y))
        flat = _flat_prims(face, mine)
        lt = RULE_LINE[rule]
        segs = [p.segment(lt, f"{a.id}.{n}") for p in flat]
        kinds = {s: k for s, k in zip(segs, c.kinds)}
        r = math.radians(cp.rotation_deg)
        co, si = math.cos(r), math.sin(r)

        def fpt(q: Point, face=face, cp=cp, co=co, si=si) -> Point:
            x, y = (-q[0] if cp.flip else q[0]), q[1]
            return face.to_flat((cp.x + x * co - y * si, cp.y + x * si + y * co))

        radii = [(rr, fpt(q)) for rr, q in c.radii]
        a.drawn.append(Drawn(n, rule, [segs], c.closed, kinds, radii, c.group, c.label))
        if c.group is not None:
            groups.setdefault(c.group, []).append(f"{a.id}.{n}")
    pointage = spec.get("pointage")
    a.ops = []
    for d in a.drawn:
        ends = tuple(x for x in groups.get(d.group, []) if x != f"{a.id}.{d.n}") if d.group is not None else ()
        a.ops.append(_opspec(a.id, d.n, d.rule, d.closed, pointage, part_pointage, ends))
    why = _inside_face(face, [s for d in a.drawn for s in d.segs])
    if why:
        raise ArtworkError(f"{a.name} is outside {face.id}: {why}")
    counts: dict[str, int] = {}
    for d in a.drawn:
        counts[d.rule] = counts.get(d.rule, 0) + 1
    rules = ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in counts.items())
    head = "agent-drawn sketch, verify before cutting: " if a.provenance == "sketch" else ""
    a.words = f"{head}{loc.words}; {rules}; " + pl.describe([cp], place, None)
    if lines:
        a.words += "; " + "; ".join(lines)
    if loc.fallback:
        a.notes.append(("info", f"{a.name}: {loc.fallback} short straight chord{'s' if loc.fallback > 1 else ''} "
                                "stand in where arcs couldn't fit its curves (within 0.002 in)"))
    a.bridge_width = _bridge_width(spec, ctx, a.name)


def _bridge_width(spec: dict[str, Any], ctx: Ctx, what: str) -> float:
    from .checks import fmt_in  # noqa: PLC0415

    lo = _bridge_min(ctx.cal)
    if spec.get("bridge_width") is None:
        return lo
    w = ctx.size(spec["bridge_width"], f"{what} bridge_width")
    if w < lo - 1e-9:
        raise ArtworkError(f"{what}: bridge_width {fmt_in(round(w * 1024) / 1024)} in is under the minimum "
                           f"max(3/16 in, 1.5 x CAL) = {fmt_in(round(lo * 1024) / 1024)} in")
    return w


# ------------------------------------------------------------------ regions


def _monotone(s: Segment) -> list[tuple[Point, Point, Point | None, float, float]]:
    """A segment as y-monotone pieces: (start, end, centre or None, radius, side of the centre in x)."""
    if isinstance(s, Line):
        return [(s.start, s.end, None, 0.0, 0.0)]
    c, r = s.center, math.dist(s.center, s.start)
    a0 = math.atan2(s.start[1] - c[1], s.start[0] - c[0])
    if s.full_circle:
        sweep = -math.tau if s.clockwise else math.tau
    else:
        a1 = math.atan2(s.end[1] - c[1], s.end[0] - c[0])
        sweep = (a1 - a0) % math.tau if not s.clockwise else -((a0 - a1) % math.tau)
    cuts = [0.0]
    for k in range(-4, 5):
        ang = math.pi / 2 + k * math.pi
        u = (ang - a0) / sweep if sweep else -1
        if 1e-12 < u < 1 - 1e-12:
            cuts.append(u)
    cuts = sorted(cuts) + [1.0]
    out = []
    for u0, u1 in zip(cuts, cuts[1:]):
        p0 = s.start if u0 == 0 else (c[0] + r * math.cos(a0 + sweep * u0), c[1] + r * math.sin(a0 + sweep * u0))
        p1 = (s.end if not s.full_circle else s.start) if u1 == 1 else \
            (c[0] + r * math.cos(a0 + sweep * u1), c[1] + r * math.sin(a0 + sweep * u1))
        side = math.cos(a0 + sweep * (u0 + u1) / 2)
        out.append((p0, p1, c, r, 1.0 if side >= 0 else -1.0))
    return out


def contains(loop: list[Segment], p: Point) -> bool:
    """Is p inside the closed loop (exact through lines and arcs, even-odd)?"""
    count = 0
    for s in loop:
        for (x0, y0), (x1, y1), c, r, side in _monotone(s):
            if (y0 > p[1]) == (y1 > p[1]):
                continue
            if c is None:
                x = x0 + (p[1] - y0) * (x1 - x0) / (y1 - y0)
            else:
                dy = p[1] - c[1]
                x = c[0] + side * math.sqrt(max(r * r - dy * dy, 0.0))
            if x > p[0]:
                count += 1
    return count % 2 == 1


def _mid(s: Segment) -> Point:
    if isinstance(s, Line):
        return ((s.start[0] + s.end[0]) / 2, (s.start[1] + s.end[1]) / 2)
    c, r = s.center, math.dist(s.center, s.start)
    a0 = math.atan2(s.start[1] - c[1], s.start[0] - c[0])
    return (c[0] + r * math.cos(a0 + s.sweep / 2), c[1] + r * math.sin(a0 + s.sweep / 2))


def _box(segs: list[Segment]) -> tuple[float, float, float, float]:
    from .features import _bounds  # noqa: PLC0415

    return _bounds(segs)


@dataclass(slots=True)
class _Loop:
    segs: list[Segment]
    art: Art | None  # None: a feature's hole
    drawn: Drawn | None
    index: int  # which loop of the Drawn
    depth: int = 0
    parent: "_Loop | None" = None


def _nest(loops: list[_Loop]) -> None:
    """Depth (1 = a hole in the board) and the loop each one sits in."""
    boxes = [_box(lp.segs) for lp in loops]
    for i, lp in enumerate(loops):
        p = _mid(lp.segs[0])
        inside = [j for j, other in enumerate(loops) if j != i and
                  boxes[j][0] <= p[0] <= boxes[j][2] and boxes[j][1] <= p[1] <= boxes[j][3] and
                  contains(other.segs, p)]
        lp.depth = len(inside) + 1
        lp.parent = None
        if inside:
            # The innermost container: the one inside the most others.
            lp.parent = loops[max(inside, key=lambda j: _depth_of(loops, j, boxes))]


def _depth_of(loops: list[_Loop], j: int, boxes) -> int:
    p = _mid(loops[j].segs[0])
    return sum(1 for k, o in enumerate(loops) if k != j and boxes[k][0] <= p[0] <= boxes[k][2]
               and boxes[k][1] <= p[1] <= boxes[k][3] and contains(o.segs, p))


def _face_loops(arts: list[Art], d: Dieline, face: str) -> list[_Loop]:
    out: list[_Loop] = []
    for a in arts:
        if a.status != "ok" or a.face != face:
            continue
        for dr in a.live():
            if dr.rule != "cut" or not dr.closed:
                continue
            for k, loop in enumerate(dr.loops):
                out.append(_Loop(loop, a, dr, k))
    for b in d.extras.get("features") or []:
        if getattr(b, "status", "") != "ok" or b.kind not in ("handhole", "vent", "window"):
            continue
        for c in b.copies:
            if c.face == face and c.segs:
                out.append(_Loop(list(c.segs), None, None, 0))
    return out


# ------------------------------------------------------------------ bridges


def _seg_split(s: Segment, p: Point) -> list[Segment]:
    """Split a segment at a point on it (a full circle becomes a full turn starting there)."""
    p = _round_pt(p)
    if isinstance(s, Line):
        if p == s.start or p == s.end:
            return [s]
        return [Line(s.start, p, s.type, s.op), Line(p, s.end, s.type, s.op)]
    if s.full_circle:
        if p == s.start:
            return [s]
        return [Arc(s.start, p, s.center, s.clockwise, s.type, False, s.op),
                Arc(p, s.start, s.center, s.clockwise, s.type, False, s.op)]
    if p == s.start or p == s.end:
        return [s]
    return [Arc(s.start, p, s.center, s.clockwise, s.type, False, s.op),
            Arc(p, s.end, s.center, s.clockwise, s.type, False, s.op)]


def _ray_hits(o: Point, u: Point, segs: list[tuple[Segment, str]]) -> list[tuple[float, int, Point, float]]:
    """(distance along the ray, index, point, |sin| of the angle) for each crossing ahead."""
    from .topology import _Piece  # noqa: PLC0415

    dummy = Operation("_", "cut")
    out = []
    for i, (s, _) in enumerate(segs):
        if isinstance(s, Line):
            dx, dy = s.end[0] - s.start[0], s.end[1] - s.start[1]
            den = u[0] * dy - u[1] * dx
            if abs(den) < 1e-15:
                continue
            wx, wy = s.start[0] - o[0], s.start[1] - o[1]
            t = (wx * dy - wy * dx) / den
            v = (wx * u[1] - wy * u[0]) / den
            if t > 1e-9 and -1e-12 <= v <= 1 + 1e-12:
                L = math.hypot(dx, dy)
                out.append((t, i, (o[0] + u[0] * t, o[1] + u[1] * t), abs(den) / L))
            continue
        c = s.center
        r = math.dist(c, s.start)
        wx, wy = o[0] - c[0], o[1] - c[1]
        b = wx * u[0] + wy * u[1]
        disc = b * b - (wx * wx + wy * wy - r * r)
        if disc <= 0:
            continue
        pc = _Piece(s, dummy)
        for t in (-b - math.sqrt(disc), -b + math.sqrt(disc)):
            if t <= 1e-9:
                continue
            q = (o[0] + u[0] * t, o[1] + u[1] * t)
            if pc.dist(q) > 1e-7:
                continue
            rx, ry = (q[0] - c[0]) / r, (q[1] - c[1]) / r
            out.append((t, i, q, abs(rx * u[0] + ry * u[1])))
    return sorted(out)


def _key(p: Point) -> tuple[int, int]:
    return (round(p[0] * 10 ** (PRECISION - 1)), round(p[1] * 10 ** (PRECISION - 1)))


def _remove_chain(soup: list[tuple[Segment, str]], a: Point, b: Point, tag: str, keep) -> bool:
    """Remove the run of `tag` segments from a to b whose every midpoint passes `keep`; False if none."""
    ends: dict[tuple[int, int], list[int]] = {}
    for i, (s, t) in enumerate(soup):
        if t != tag:
            continue
        for q in (s.start, s.end):
            ends.setdefault(_key(q), []).append(i)
    ka, kb = _key(a), _key(b)
    for first in ends.get(ka, []):
        run = [first]
        node = ka
        ok = True
        cur = first
        while True:
            s = soup[cur][0]
            if not keep(_mid(s)):
                ok = False
                break
            nxt = _key(s.end) if _key(s.start) == node else _key(s.start)
            if nxt == kb:
                break
            cand = [j for j in ends.get(nxt, []) if j != cur and j not in run]
            if len(cand) != 1 or len(run) > 2000:
                ok = False
                break
            cur = cand[0]
            run.append(cur)
            node = nxt
        if ok:
            for j in sorted(run, reverse=True):
                del soup[j]
            return True
    return False


def _split_on(soup: list[tuple[Segment, str]], p: Point, tag: str) -> bool:
    """Split the `tag` segment that p lies on at p (nothing to do at a segment's end)."""
    from .topology import _Piece  # noqa: PLC0415

    dummy = Operation("_", "cut")
    for i, (s, t) in enumerate(soup):
        if t != tag:
            continue
        if _key(s.start) == _key(p) or _key(s.end) == _key(p):
            return True
        if _Piece(s, dummy).dist(p) <= 1e-6:
            soup[i:i + 1] = [(q, t) for q in _seg_split(s, p)]
            return True
    return False


def _corridor(soup: list[tuple[Segment, str]], others: list[tuple[Segment, str]], c: Point, u: Point, off: float,
              w: float, lt: LineType, op: str) -> bool:
    """One bridge from the island (tag I) across the hole to the board beyond its outer loop (tag P),
    going along u from the line through c offset by `off`. Changes soup in place when it fits."""
    n = (-u[1], u[0])
    sides = []
    for sgn in (1.0, -1.0, 0.0):
        q = (c[0] + n[0] * (off + sgn * w / 2), c[1] + n[1] * (off + sgn * w / 2))
        hits = _ray_hits(q, u, soup + others)
        if len(hits) < 2:
            return False
        h1, h2 = hits[0], hits[1]
        all_ = soup + others
        if all_[h1[1]][1] != "I" or all_[h2[1]][1] != "P" or h1[3] < 0.5 or h2[3] < 0.5:
            return False  # not cleanly from the island to the outer loop, or too shallow a crossing
        if h2[0] - h1[0] < 1e-6:
            return False
        if sgn:
            sides.append((q, h1, h2))
    # Nothing else may sit in the corridor.
    t_lo = min(s[1][0] for s in sides)
    t_hi = max(s[2][0] for s in sides)
    for s, _ in others:
        for p in (s.start, s.end, _mid(s)):
            along = (p[0] - c[0]) * u[0] + (p[1] - c[1]) * u[1]
            across = (p[0] - c[0]) * n[0] + (p[1] - c[1]) * n[1] - off
            if t_lo - 1e-9 <= along <= t_hi + 1e-9 and abs(across) <= w / 2 + 1e-9:
                return False
    work = list(soup)
    # Split the island and the outer loop where the corridor's sides cross them.
    pts = []
    for q, h1, h2 in sides:
        for h, tag in ((h1, "I"), (h2, "P")):
            p = _round_pt(h[2])
            if not _split_on(work, p, tag):
                return False
            pts.append(p)
    (aI, bP), (aI2, bP2) = (pts[0], pts[1]), (pts[2], pts[3])

    def in_strip(p: Point) -> bool:
        across = (p[0] - c[0]) * n[0] + (p[1] - c[1]) * n[1] - off
        along = (p[0] - c[0]) * u[0] + (p[1] - c[1]) * u[1]
        return abs(across) <= w / 2 + 1e-7 and along > 0

    if not _remove_chain(work, aI, aI2, "I", in_strip) or not _remove_chain(work, bP, bP2, "P", in_strip):
        return False
    for p0, p1 in ((aI, bP), (aI2, bP2)):
        work.append((Line(p0, p1, lt, op), "B"))
    soup[:] = work
    return True


def _chain(segs: list[Segment]) -> list[list[Segment]] | None:
    """Closed loops from a set of segments meeting end to end (None if they don't close)."""
    ends: dict[tuple[int, int], list[int]] = {}
    for i, s in enumerate(segs):
        if isinstance(s, Arc) and s.full_circle:
            continue
        for q in (s.start, s.end):
            ends.setdefault(_key(q), []).append(i)
    used = [False] * len(segs)
    loops: list[list[Segment]] = []
    for i, s in enumerate(segs):
        if used[i]:
            continue
        used[i] = True
        if isinstance(s, Arc) and s.full_circle:
            loops.append([s])
            continue
        loop = [s]
        start = _key(s.start)
        node = _key(s.end)
        cur = i
        while node != start:
            cand = [j for j in ends.get(node, []) if j != cur and not used[j]]
            if not cand:
                return None
            j = cand[0]
            used[j] = True
            t = segs[j]
            if _key(t.start) != node:
                t = _reverse(t)
            loop.append(t)
            node = _key(t.end)
            cur = j
            if len(loop) > 20000:
                return None
        loops.append(loop)
    return loops


def _reverse(s: Segment) -> Segment:
    if isinstance(s, Line):
        return Line(s.end, s.start, s.type, s.op)
    return Arc(s.end, s.start, s.center, not s.clockwise, s.type, s.full_circle, s.op)


def _interiors(island: _Loop, children: list[_Loop], n: int = 12) -> list[Point]:
    """Points inside the island's board (inside its loop, outside the holes in it), nearest its middle first."""
    x0, y0, x1, y1 = _box(island.segs)
    found = []
    for i in range(0, 15):
        for j in range(0, 15):
            p = (x0 + (x1 - x0) * (i + 0.5) / 15, y0 + (y1 - y0) * (j + 0.5) / 15)
            if contains(island.segs, p) and not any(contains(ch.segs, p) for ch in children):
                found.append(((i - 7) ** 2 + (j - 7) ** 2, p))
    found.sort()
    return [p for _, p in found[:n]]


def _retag(s: Segment, op: str) -> Segment:
    if isinstance(s, Line):
        return Line(s.start, s.end, s.type, op)
    return Arc(s.start, s.end, s.center, s.clockwise, s.type, s.full_circle, op)


def bridge(island: _Loop, hole: _Loop, children: list[_Loop], others: list[Segment], w: float,
           flute_axis: str) -> tuple[list[list[Segment]], int, set[Segment]] | None:
    """Two corridors of board from the island across its hole. Returns the redrawn loops (the hole's
    outer loop and the island's loop become the pieces of the hole between the corridors), the
    number of bridges, and the corridor sides. Corridors run across the flutes when they can."""
    pts = _interiors(island, children)
    if not pts:
        return None
    lt = hole.segs[0].type
    op = hole.segs[0].op or ""
    across = (1.0, 0.0) if flute_axis == "y" else (0.0, 1.0)  # flat: the flutes run along flute_axis
    along = (across[1], across[0])
    h = math.sqrt(0.5)
    dirs = [across, (-across[0], -across[1]), along, (-along[0], -along[1]), (h, h), (-h, -h), (h, -h), (-h, h)]
    x0, y0, x1, y1 = _box(island.segs)
    span = max(x1 - x0, y1 - y0)
    oth = [(s, "O") for s in others] + [(s, "O") for ch in children for s in ch.segs]

    def first(soup: list[tuple[Segment, str]], order: list[Point]) -> tuple[list[tuple[Segment, str]], Point] | None:
        for u in order:
            for c in pts:
                for off in (0.0, 0.15, -0.15, 0.3, -0.3):
                    trial = list(soup)
                    if _corridor(trial, oth, c, u, off * span, w, lt, op):
                        return trial, u
        return None

    soup0 = [(s, "I") for s in island.segs] + [(s, "P") for s in hole.segs]
    a = first(soup0, dirs)
    if a is None:
        return None
    soup1, ua = a
    opposite = (-ua[0], -ua[1])
    b = first(soup1, [opposite] + [u for u in dirs if u not in (ua, opposite)])
    if b is None:
        return None
    soup2, _ = b
    side_ends = {frozenset((s.start, s.end)) for s, t in soup2 if t == "B"}
    loops = _chain([_retag(s, op) for s, _ in soup2])
    if loops is None:
        return None
    sides = {s for lo in loops for s in lo if isinstance(s, Line) and frozenset((s.start, s.end)) in side_ends}
    return loops, 2, sides


# ------------------------------------------------------------------ islands


def _pass_limit(loops: int) -> int:
    """How many passes a face's islands get: each fix that changes the nesting takes one, and each
    removes a cut loop or joins an island to its hole, so this is never reached by sound artwork."""
    return 8 + 2 * loops


def _islands(arts: list[Art], d: Dieline, flute_axis: str) -> list[dict[str, Any]]:
    """Find every island the artwork makes (with the features' holes), apply each one's chosen fix,
    and record what's left. Returns records for the checks, each with the island's exact loop, so
    check_topology leaves only that island (unhandled or an accepted drop-out) to the artwork checks.

    Each pass works out the nesting and fixes every island it can whose loop and hole no fix in that
    pass has touched; a partial cut changes the nesting of everything inside it, so it ends the pass.
    The passes are bounded; if the bound is reached, every island still unresolved fails (never
    silently dropped). An island no artwork owns (a feature's hole inside another feature's) gets no
    record: the topology's material check fails it."""
    faces = sorted({a.face for a in arts if a.status == "ok" and a.face})
    records: list[dict[str, Any]] = []
    for a in arts:
        a.islands = []
    for face in faces:
        handled: set[tuple[Segment, ...]] = set()
        limit = _pass_limit(len(_face_loops(arts, d, face)))
        for _ in range(limit):
            loops = _face_loops(arts, d, face)
            _nest(loops)
            changed = False
            touched: set[tuple[Segment, ...]] = set()
            pending = [lp for lp in loops if lp.depth >= 2 and lp.depth % 2 == 0]
            for lp in sorted(pending, key=lambda x: x.depth):
                owner = lp.art if lp.art is not None else (lp.parent.art if lp.parent is not None else None)
                if owner is None:
                    continue  # a feature inside a feature: no artwork to fix it, so the material check fails it
                key = tuple(lp.segs)
                if key in handled:
                    continue
                if key in touched or (lp.parent is not None and tuple(lp.parent.segs) in touched):
                    changed = True  # its loop or hole was just redrawn: work it out again next pass
                    continue
                iid = f"i{len(owner.islands) + 1}"
                isl = Island(iid, lp.drawn.n if lp.drawn else 0, lp.parent.drawn.n if lp.parent and lp.parent.drawn
                             else None, None, "open", point=_mid(lp.segs[0]), region=_box(lp.segs), segs=key)
                fixes = owner.spec.get("islands")
                fix = fixes if isinstance(fixes, str) else (fixes or {}).get(iid)
                isl.fix = fix
                owner.islands.append(isl)
                handled.add(key)
                if fix is None:
                    continue
                if fix == "drop_out":
                    isl.status = "drop_out"
                    continue
                hole = lp.parent
                if hole is None or hole.art is not owner or hole.drawn is None or lp.art is not owner:
                    isl.status, isl.reason = "failed", ("its hole is a feature's or another artwork's, so this "
                                                        "artwork can't redraw it; choose drop_out or move it")
                    continue
                if fix == "partial_cut":
                    hd = hole.drawn
                    hd.rule = "partial_cut"
                    hd.loops = [[_retype(s, LineType.SCORE, s.op) for s in loop] for loop in hd.loops]
                    hd.kinds = {_retype(s, LineType.SCORE, s.op): k for s, k in hd.kinds.items()}
                    owner.ops = [_opspec(owner.id, hd.n, "partial_cut", True, owner.spec.get("pointage"), 2.0)
                                 if o.id == f"{owner.id}.{hd.n}" else o for o in owner.ops]
                    isl.status = "partial_cut"
                    changed = True
                    break  # everything inside the hole changes depth
                # bridge / stencil
                children = [x for x in loops if x.parent is lp]
                others = [s for x in loops if x is not lp and x is not hole and x not in children for s in x.segs]
                others += [s for s in d.segments if s.type != LineType.ANNOTATION and s.op != "outline"]
                got = bridge(lp, hole, children, others, owner.bridge_width, flute_axis)
                if got is None:
                    isl.status, isl.reason = "failed", ("two bridges couldn't be placed automatically (the corridors "
                                                        "would cross other cuts or meet the outline at a shallow "
                                                        "angle); choose drop_out or partial_cut, or change the artwork")
                    continue
                new_loops, count, sides = got
                hd, idr = hole.drawn, lp.drawn
                assert idr is not None
                touched.add(key)
                touched.add(tuple(hole.segs))
                keep = [lo for lo in hd.loops if lo is not hole.segs]
                hd.loops = keep + new_loops
                hd.kinds.update({s: "bridge" for s in sides})
                for s in idr.segs:
                    hd.kinds.setdefault(s, idr.kinds.get(s, "line"))
                for s in [s for lo in new_loops for s in lo]:
                    if s not in hd.kinds:
                        hd.kinds[s] = "arc" if isinstance(s, Arc) else "line"
                idr.loops = [lo for lo in idr.loops if lo is not lp.segs]
                if not idr.loops:
                    idr.merged_into = hd.n
                hd.radii += idr.radii
                isl.status, isl.bridges, isl.width = ("bridged" if fix == "bridge" else "stencil"), count, \
                    owner.bridge_width
                changed = True
            if not changed:
                break
        else:
            # The bound ran out: fail closed. Every island still there and not yet decided fails.
            loops = _face_loops(arts, d, face)
            _nest(loops)
            for lp in sorted([x for x in loops if x.depth >= 2 and x.depth % 2 == 0], key=lambda x: x.depth):
                owner = lp.art if lp.art is not None else (lp.parent.art if lp.parent is not None else None)
                key = tuple(lp.segs)
                if owner is None or key in handled:
                    continue
                handled.add(key)
                owner.islands.append(Island(
                    f"i{len(owner.islands) + 1}", lp.drawn.n if lp.drawn else 0,
                    lp.parent.drawn.n if lp.parent and lp.parent.drawn else None, None, "failed",
                    f"the engine stopped working out this face's islands after {limit} passes; simplify the "
                    "artwork or choose drop_out", _mid(lp.segs[0]), _box(lp.segs), segs=key))
        for a in arts:
            if a.face != face:
                continue
            for isl in a.islands:
                records.append({"artwork": a.id, "island": isl.id, "status": isl.status, "point": isl.point,
                                "region": isl.region, "loop": isl.loop, "segs": isl.segs})
    return records


# ------------------------------------------------------------------ the stage


def _stale_by_role(a: Art, fm: FaceMap) -> None:
    for face, role in (a.spec.get("bound") or {}).items():
        f = fm.faces.get(face)
        if f is None:
            a.status, a.reason = "stale", f"{face} no longer exists on this part; place {a.name} again"
            return
        if f.role != role:
            a.status, a.reason = "stale", (f"{face} was the {role} panel and is now the {f.role} panel; place "
                                           f"{a.name} again")
            return


def apply(d: Dieline, items: list[dict[str, Any]], params: Any, board: Any) -> None:
    """Draw every artwork item onto the part (in place), after its features. Fills d.extras:
    artwork (Art records), artwork_islands, and adds to tags and the topology report."""
    from .topology import validate  # noqa: PLC0415

    fm = d.faces
    if not isinstance(fm, FaceMap):
        if items:
            raise ArtworkError("this part has no face map, so artwork can't be placed on it")
        return
    ctx = Ctx(d, fm, _env(params, board), 1.0 if params.units == "in" else 1 / 25.4, board.cal, params.units)
    opts = d.meta.get("options") if isinstance(d.meta.get("options"), dict) else {}
    flute_axis = "x" if (opts or {}).get("flute") == "horizontal" else "y"
    part_pointage = float(d.meta.get("pointage", 2.0))  # type: ignore[arg-type]
    base = Dieline(d.name, d.units, list(d.segments), d.meta, dict(d.ops), d.faces, d.extras)
    arts: list[Art] = []
    for raw in items:
        a = Art(raw["id"], raw["kind"], raw.get("label") or _default_label(raw), raw)
        try:
            _stale_by_role(a, fm)
            if a.status == "ok":
                _draft(a, ctx, part_pointage)
        except (ArtworkError, src.SourceError, pl.PlacementError, ExpressionError, FeatureError, ValueError) as exc:
            a.status, a.reason = "stale", str(exc)
        arts.append(a)

    def compose(ok: list[Art]) -> Dieline:
        out = Dieline(d.name, d.units, list(base.segments), d.meta, dict(base.ops), d.faces, d.extras)
        for a in ok:
            for op in a.ops:
                if any(dr.n == int(op.id.split(".")[1]) and dr.merged_into is None for dr in a.drawn):
                    out.add_op(op)
            out.segments += a.segs()
        return out

    # Islands, then the whole design validated; artwork that breaks it is set aside (stale) and the rest
    # redrawn from their drafts (bridges depend on what else is live). Every pass sets at least one
    # artwork aside, so this ends; the islands are always worked out for exactly what's composed.
    while True:
        live = [a for a in arts if a.status == "ok"]
        records = _islands(live, base, flute_axis)
        final = compose(live)
        report = validate(final)
        if report.ok or not live:
            break
        good: list[Art] = []
        for a in live:
            trial = compose(good + [a])
            r = validate(trial)
            if r.ok:
                good.append(a)
            else:
                mine = [i for i in r.invalid if any(o.startswith(a.id + ".") or o == "outline" for o in i.ops)]
                issue = (mine or r.invalid)[0]
                a.status, a.reason = "stale", friendly(f"{issue.message}; move or resize {a.name}", trial)
        if len(good) == len(live):  # each fits alone but not together: never loop on it
            for a in live:
                a.status, a.reason = "stale", f"{a.name} breaks the design together with the other artwork"
        for a in arts:
            if a.status == "ok":
                try:
                    _draft(a, ctx, part_pointage)
                except (ArtworkError, src.SourceError, ValueError) as exc:
                    a.status, a.reason = "stale", str(exc)
    for a in arts:
        if a.status != "ok":
            a.islands = []
    d.segments, d.ops = final.segments, final.ops
    tags: dict[Segment, dict[str, Any]] = d.extras.get("tags") or {}  # type: ignore[assignment]
    for a in live:
        prov = PROVENANCE[a.provenance]
        for s in a.segs():
            tags[s] = {"feature_id": a.id, "face_id": a.face, "provenance": prov}
    for a in arts:
        if a.status == "stale":
            for dr in a.drawn:
                for s in dr.segs:
                    g = _retype(s, LineType.ANNOTATION, None)
                    a.ghost.append(g)
                    d.segments.append(g)
                    tags[g] = {"feature_id": a.id, "face_id": a.face, "stale": True,
                               "provenance": PROVENANCE[a.provenance]}
    d.extras["tags"] = tags
    d.extras["artwork"] = arts
    d.extras["artwork_islands"] = records
    d.extras["report"] = report


def _default_label(raw: dict[str, Any]) -> str:
    k = raw["kind"]
    if k == "shape":
        return str(raw.get("shape", "shape"))
    return {"stencil_text": "stencil text", "stroke_text": "stroke text", "sketch": "sketch",
            "import": "imported artwork"}[k]

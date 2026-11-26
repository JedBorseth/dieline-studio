"""Face maps: the named panels, flaps and lugs of a part, with their frames,
reference lines, keep-out zones and hinges (docs/DESIGN-SYSTEM.md, "Faces and
orientation" and "Geometry contract").

Features and artwork are placed on faces, never on raw blank coordinates. A
face's frame is how a designer sees it: from the outside of the folded box,
+y up on the box (toward a flap's free edge on a flap), +x to the right. The
flat drawing is inside up, so the engine's rigid map between a face frame and
the blank includes a mirror.

This module builds the face map for the glued slotted standards from the same
`Blank` that `slotted.draw` uses, so a face and its lines can't disagree.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .boards import Board
from .geometry import Point

Vec = tuple[float, float]


@dataclass(slots=True)
class RefLine:
    """A line on a face that placements measure from.

    `start`/`end` are in the face frame, directed; `inward` is the unit normal
    (face frame) pointing into the face. `kind` names what the line physically is;
    "datum" means a side of the face's bounding box made of several kinds of edge.
    A score's line runs its full construction length; `span` is where its crease
    actually is (shorter, between the slots) and `op` the crease operation.
    A physical edge (free_edge, open_edge) runs exactly where its cut is.
    """

    id: str  # "<face>.<name>"
    name: str  # top | bottom | left | right | centre_x | centre_y | top_score | free_edge | ...
    kind: str  # score | free_edge | open_edge | glue_score | centre | cut | datum
    start: Point
    end: Point
    inward: Vec | None  # None for centre lines (they have no inside)
    op: str | None = None
    span: tuple[Point, Point] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "name": self.name, "kind": self.kind, "start": _r(self.start),
                               "end": _r(self.end), "inward": _r(self.inward) if self.inward else None}
        if self.op:
            out["op"] = self.op
        if self.span:
            out["span"] = [_r(self.span[0]), _r(self.span[1])]
        return out


@dataclass(frozen=True, slots=True)
class EdgeSpan:
    """One exact piece of a face's boundary, as drawn: what it is and what's across it."""

    side: str  # top | bottom | left | right | taper (a slanted side)
    kind: str  # cut | crease
    start: Point  # face frame
    end: Point
    op: str | None  # the operation that drew it
    across: str | None  # creases: the face on the other side

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"side": self.side, "kind": self.kind, "start": _r(self.start), "end": _r(self.end),
                               "op": self.op}
        if self.across:
            out["across"] = self.across
        return out


@dataclass(frozen=True, slots=True)
class KeepOut:
    """A region of a face something else covers or lands on.

    kind: glue_landing (the glue lug lands here), overlap_band (the opposite flap
    overlaps here when the box is closed), covered (the outer flaps lie over it).
    side: inside | outside | conditional (either surface, depending on the order
    the flaps close). Sizes are nominal, from score-line projections; `clipped`
    when the nominal region is bigger than the face and was cut to fit."""

    kind: str
    rect: tuple[float, float, float, float]  # x0, y0, x1, y1 in the face frame
    side: str
    note: str
    clipped: bool = False

    def to_dict(self) -> dict[str, Any]:
        out = {"kind": self.kind, "rect": _r(self.rect), "side": self.side, "note": self.note}
        if self.clipped:
            out["clipped"] = True
        return out


@dataclass(slots=True)
class Face:
    id: str
    kind: str  # body | flap | lug | lug_ext
    role: str  # end | side | lug | flap | lug_ext
    width: float  # face frame extent along x
    height: float  # face frame extent along y
    # Rigid map to the flat blank: flat = origin + x * x_axis + y * y_axis.
    origin: Point
    x_axis: Vec
    y_axis: Vec
    lines: dict[str, RefLine] = field(default_factory=dict)
    keepouts: list[KeepOut] = field(default_factory=list)
    # Assembled and material centres along x, in the face frame (see the geometry contract).
    centre_x: float = 0.0
    material_centre_x: float = 0.0
    parent: str | None = None  # the face it hinges from
    hinge: str | None = None  # the hinge operation joining it to its parent
    end: str | None = None  # flaps: "top" or "bottom" of the folded box
    outer: bool | None = None  # flaps: outer (closes over the others) when the closure defines it
    panel: str | None = None  # flaps and lug extensions: the body panel they belong to
    polygon: list[Point] = field(default_factory=list)  # its exact outline on the flat blank
    edges: list[EdgeSpan] = field(default_factory=list)  # its boundary, piece by piece, as drawn
    up: str = ""  # in words, for a model: which way is up and what's on each side

    def to_flat(self, p: Point) -> Point:
        return (self.origin[0] + p[0] * self.x_axis[0] + p[1] * self.y_axis[0],
                self.origin[1] + p[0] * self.x_axis[1] + p[1] * self.y_axis[1])

    def from_flat(self, p: Point) -> Point:
        dx, dy = p[0] - self.origin[0], p[1] - self.origin[1]
        return (dx * self.x_axis[0] + dy * self.x_axis[1], dx * self.y_axis[0] + dy * self.y_axis[1])

    @property
    def mirrored(self) -> bool:
        """True when the face frame is a mirror image of the flat drawing (an outside view of an inside-up blank)."""
        return self.x_axis[0] * self.y_axis[1] - self.x_axis[1] * self.y_axis[0] < 0

    def corners_flat(self) -> list[Point]:
        return [self.to_flat(p) for p in ((0, 0), (self.width, 0), (self.width, self.height), (0, self.height))]

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id, "kind": self.kind, "role": self.role, "orientation": self.up,
            "width": round(self.width, 6), "height": round(self.height, 6),
            "centre_x": round(self.centre_x, 6), "material_centre_x": round(self.material_centre_x, 6),
            "lines": {k: v.to_dict() for k, v in self.lines.items()},
            "keepouts": [k.to_dict() for k in self.keepouts],
            "edges": [e.to_dict() for e in self.edges],
            "parent": self.parent, "hinge": self.hinge,
            "frame": {"origin_flat": _r(self.origin), "x_axis_flat": _r(self.x_axis), "y_axis_flat": _r(self.y_axis)},
        }
        for key in ("end", "outer", "panel"):
            if getattr(self, key) is not None:
                out[key] = getattr(self, key)
        return out


@dataclass(frozen=True, slots=True)
class Hinge:
    """A fold. The axis is directed so the child lies to the left of the
    direction of travel, seen on the inside face (the flat drawing). Folded,
    the inside surfaces meet IL from the score line on each panel and the
    outside surfaces OG from it."""

    id: str  # the crease operation's id
    parent: str
    child: str
    start: Point  # flat
    end: Point  # flat
    il: float
    og: float

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "parent": self.parent, "child": self.child, "start": _r(self.start),
                "end": _r(self.end), "il": self.il, "og": self.og}


@dataclass(slots=True)
class FaceMap:
    faces: dict[str, Face]
    hinges: dict[str, Hinge]
    drawing_side: str = "inside_up"
    selectors: dict[str, list[str]] = field(default_factory=dict)
    unavailable: dict[str, str] = field(default_factory=dict)  # selector -> why it isn't defined here
    viewing: str = "outside"  # placements are given as seen from outside the folded box

    def resolve(self, selector: str | list[str]) -> list[str]:
        """Face ids for a selector, a face id, or a list of either (duplicates removed, order kept)."""
        items = selector if isinstance(selector, list) else [selector]
        if not items:
            raise ValueError(f"an empty selection. Faces: {', '.join(self.faces)}; selectors: {', '.join(self.selectors)}")
        out: list[str] = []
        for item in items:
            if not isinstance(item, str):
                raise ValueError("a selector is a face id, a selector name, or a list of them")
            if item in self.faces:
                found = [item]
            elif item in self.selectors:
                found = self.selectors[item]
            elif item in self.unavailable:
                raise ValueError(f"'{item}' isn't defined for this part: {self.unavailable[item]}. "
                                 f"Faces: {', '.join(self.faces)}; selectors: {', '.join(self.selectors)}")
            else:
                raise ValueError(f"no face or selector '{item}'. Faces: {', '.join(self.faces)}; "
                                 f"selectors: {', '.join(self.selectors)}")
            if not found:
                raise ValueError(f"'{item}' matches no faces on this part. Faces: {', '.join(self.faces)}")
            out += [f for f in found if f not in out]
        return out

    def agent_view(self) -> dict[str, Any]:
        """What a model needs to name positions: compact, in fractions of an inch,
        no flat coordinates or operation ids."""
        from .checks import fmt_in  # noqa: PLC0415

        def size(x: float) -> str:
            return fmt_in(x)

        faces = []
        for f in self.faces.values():
            entry: dict[str, Any] = {"id": f.id, "role": f.role, "size_in": f"{size(f.width)} x {size(f.height)}",
                                     "orientation": f.up}
            entry["edges"] = {side: f.lines[side].kind for side in ("top", "bottom", "left", "right")}
            entry["lines"] = sorted(n for n in f.lines if n not in ("top", "bottom", "left", "right"))
            if abs(f.centre_x - f.width / 2) > 1e-9:
                entry["centre_x_in"] = size(f.centre_x)
            for k in f.keepouts:
                x0, y0, x1, y1 = k.rect
                entry.setdefault("keep_outs", []).append(
                    f"{k.kind} ({k.side}): x {size(x0)} to {size(x1)}, y {size(y0)} to {size(y1)}")
            if f.parent:
                entry["hinged_to"] = f.parent
            faces.append(entry)
        return {
            "units": "in",
            "viewing": "Faces are seen from OUTSIDE the folded box. Sizes are width x height in the face's own frame "
                       "(x right, y up from its bottom-left corner). Each face's orientation says which way is up "
                       "and what is on its sides.",
            "folding": "Folded, the body panels go round the box in order P1, P2, P3, P4; P4's glue lug glues to "
                       "P1's free edge. No panel is the front unless the user says so.",
            "faces": faces,
            "selectors": dict(self.selectors),
            "unavailable_selectors": dict(self.unavailable),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "viewing": self.viewing,
            "drawing_side": self.drawing_side,
            "frames": "Each face is seen from outside the folded box: +y up on the box (toward the free edge on a "
                      "flap), +x to the right. Distances are in the face frame, from its bottom-left corner.",
            "faces": [f.to_dict() for f in self.faces.values()],
            "hinges": [h.to_dict() for h in self.hinges.values()],
            "selectors": {k: v for k, v in self.selectors.items()},
            "unavailable_selectors": dict(self.unavailable),
        }


def _r(v):
    return tuple(round(x, 6) for x in v)


def _keep(f: Face, kind: str, rect: tuple[float, float, float, float], side: str, note: str) -> None:
    """Add a keep-out, cut to the face; say so when the nominal region didn't fit."""
    x0, y0, x1, y1 = rect
    cx0, cy0, cx1, cy1 = max(x0, 0.0), max(y0, 0.0), min(x1, f.width), min(y1, f.height)
    if cx1 <= cx0 or cy1 <= cy0:
        return
    clipped = (cx0, cy0, cx1, cy1) != (x0, y0, x1, y1)
    if clipped:
        note += " (the nominal region is bigger than this face)"
    f.keepouts.append(KeepOut(kind, (cx0, cy0, cx1, cy1), side, note, clipped))


# ------------------------------------------------------------------ slotted standards


def _rect_face(fid: str, kind: str, role: str, x0: float, x1: float, y0: float, y1: float, *,
               up: int, mirror: bool) -> Face:
    """A rectangular face from flat bounds. `up` is +1 when the face's +y is flat +y,
    -1 when it's flat -y. `mirror` is True for an outside view of an inside-up blank."""
    w, h = x1 - x0, y1 - y0
    y_axis = (0.0, float(up))
    # Seen from outside an inside-up drawing, left and right swap: x runs opposite to
    # the flat x when +y is flat +y, and with it when +y is flat -y (turned over).
    x_dir = -1.0 if (mirror and up > 0) or (not mirror and up < 0) else 1.0
    x_axis = (x_dir, 0.0)
    ox = x1 if x_dir < 0 else x0
    oy = y0 if up > 0 else y1
    return Face(fid, kind, role, w, h, (ox, oy), x_axis, y_axis)


def _edges(f: Face, names: dict[str, tuple[str, str]]) -> None:
    """Reference lines along a rectangular face's four sides, in its frame.
    `names` maps top/bottom/left/right to (physical name, kind)."""
    w, h = f.width, f.height
    sides = {
        "bottom": ((0.0, 0.0), (w, 0.0), (0.0, 1.0)),
        "top": ((0.0, h), (w, h), (0.0, -1.0)),
        "left": ((0.0, 0.0), (0.0, h), (1.0, 0.0)),
        "right": ((w, 0.0), (w, h), (-1.0, 0.0)),
    }
    for side, (a, b, n) in sides.items():
        phys, kind = names[side]
        line = RefLine(f"{f.id}.{side}", side, kind, a, b, n)
        f.lines[side] = line
        if phys != side:
            f.lines[phys] = RefLine(f"{f.id}.{phys}", phys, kind, a, b, n)
    f.lines["centre_y"] = RefLine(f"{f.id}.centre_y", "centre_y", "centre", (0.0, h / 2), (w, h / 2), None)


def _bare(text: str) -> str:
    return text.split(" ", 1)[1] if text.startswith(("a ", "an ", "the ")) else text


def names_of(f: Face, side: str) -> str:
    k = f.lines[side].kind
    return {"free_edge": "the free edge", "glue_score": "the glue score", "score": "a panel score",
            "open_edge": "an open edge"}.get(k, k)


def _centre(f: Face, x: float) -> None:
    f.centre_x = x
    f.lines["centre_x"] = RefLine(f"{f.id}.centre_x", "centre_x", "centre", (x, 0.0), (x, f.height), None)


def slotted_faces(blank: Any, raw: list[float], board: Board, *, order: str, closure: str,
                  top_style: str | None, bottom_style: str | None, use: str | None, half_slot: float,
                  glue_taper: float, lap: tuple[float, float] = (0.0, 0.0)) -> FaceMap:
    """The face map of a glued slotted blank, built from the `Blank` slotted.draw uses.

    raw: the standard's raw panels P1A..P4A (W or L). closure: inside | outside.
    use: for an HSC, tray (flaps at the bottom of the folded box) or lid; None otherwise.
    """
    p = blank.panels
    xs = [0.0]
    for w in p:
        xs.append(xs[-1] + w)
    c4 = xs[4]
    x_end = c4 + blank.glue
    e = blank.lug_extension
    y1 = max(max(blank.bottom) if blank.bottom else 0.0, e)
    y2 = y1 + blank.body
    h, t = half_slot, glue_taper
    X = lambda x: x_end - x  # noqa: E731  mirror: the glue lug on the left, panel 1 at the free edge

    tray = use == "tray"
    up = -1 if tray else 1  # an HSC tray's body is up toward flat -y (its flap row points down)
    roles = ["end", "side", "end", "side"] if order == "eses" else ["side", "end", "side", "end"]
    faces: dict[str, Face] = {}
    hinges: dict[str, Hinge] = {}

    def hinge(hid: str, parent: str, child: str, a: Point, b: Point) -> None:
        hinges[hid] = Hinge(hid, parent, child, a, b, board.il, board.og)

    def side_name(kind: str, side: str) -> str:
        return {"free_edge": "free_edge", "glue_score": "glue_score"}.get(kind, f"{side}_score")

    # --- body panels: P(i+1) spans flat X in [X(xs[i+1]), X(xs[i])]
    a1, a4 = raw[0] + board.cal, raw[3] + board.cal
    delta = board.il if closure == "outside" else 0.0
    for i in range(4):
        fid = f"P{i + 1}"
        f = _rect_face(fid, "body", roles[i], X(xs[i + 1]), X(xs[i]), y1, y2, up=up, mirror=True)
        high = "free_edge" if i == 0 else "score"  # the flat side at the larger X
        low = "glue_score" if i == 3 else "score"  # the flat side at the smaller X
        left, right = (high, low) if f.x_axis[0] < 0 else (low, high)
        flat_top = "score" if blank.top else "open_edge"
        flat_bottom = "score" if blank.bottom else "open_edge"
        top_kind, bottom_kind = (flat_bottom, flat_top) if tray else (flat_top, flat_bottom)
        both_open = top_kind == bottom_kind == "open_edge"  # a tube: name each opening
        _edges(f, {
            "top": ("top_score" if top_kind == "score" else "top_open_edge" if both_open else "open_edge", top_kind),
            "bottom": ("bottom_score" if bottom_kind == "score" else "bottom_open_edge" if both_open else "open_edge",
                       bottom_kind),
            "left": (side_name(left, "left"), left),
            "right": (side_name(right, "right"), right),
        })
        up_word = ("away from the flaps (a tray: its flaps are the bottom)" if tray else
                   "toward the top flaps" if blank.top else "toward the top of the drawing")
        f.up = (f"{roles[i]} panel seen from outside; +y is up on the box ({up_word}); its left side is "
                f"{names_of(f, 'left')}, its right side {names_of(f, 'right')}")
        w = f.width
        if i == 0:  # measured from the P1/P2 score, the side that isn't the free edge
            at, dirn = (w, -1) if left == "free_edge" else (0.0, 1)
            _centre(f, at + dirn * a1 / 2)
            f.material_centre_x = at + dirn * (a1 - 0.125 - delta) / 2
        elif i == 3:  # measured from the P3/P4 score, the side that isn't the glue score
            at, dirn = (w, -1) if left == "glue_score" else (0.0, 1)
            _centre(f, at + dirn * a4 / 2)
            f.material_centre_x = at + dirn * (a4 + delta) / 2
        else:
            _centre(f, w / 2)
            f.material_centre_x = w / 2
        f.polygon = f.corners_flat()
        faces[fid] = f
    # The chain P1 → P2 → P3 → P4 → glue lug: each child is at smaller flat X, so the
    # axis runs up (+y) to keep it on the left.
    for i in range(3):
        xb = X(xs[i + 1])
        hid = f"hinge_P{i + 1}_P{i + 2}"
        hinge(hid, f"P{i + 1}", f"P{i + 2}", (xb, y1), (xb, y2))
        faces[f"P{i + 2}"].parent, faces[f"P{i + 2}"].hinge = f"P{i + 1}", hid

    # --- glue lug: from the glue score to its free end
    lug = _rect_face("glue_lug", "lug", "lug", X(x_end), X(c4), y1, y2, up=up, mirror=True)
    # The glue score is at the larger flat X, which is the face's left when x runs opposite flat x.
    score_side = "left" if lug.x_axis[0] < 0 else "right"
    other_side = "left" if score_side == "right" else "right"
    lug_tb = (("top_score", "score"), ("bottom_score", "score")) if e else (("top", "datum"), ("bottom", "datum"))
    _edges(lug, {"top": lug_tb[0], "bottom": lug_tb[1],
                 score_side: ("glue_score", "glue_score"), other_side: ("free_edge", "free_edge")})
    lug.up = (f"glue lug seen from outside; +y is up on the box; its {score_side} side is the glue score, its "
              f"{other_side} side the free edge; it glues to P1's free edge (inside or outside, per the joint)")
    _centre(lug, lug.width / 2)
    lug.material_centre_x = lug.width / 2
    lug.parent, lug.hinge = "P4", "hinge_glue"
    if e:
        lug.polygon = lug.corners_flat()
    else:  # tapered corners at the free end
        lug.polygon = [(X(c4), y1), (X(c4 + h), y1), (X(x_end), y1 + t), (X(x_end), y2 - t), (X(c4 + h), y2),
                       (X(c4), y2)]
    faces["glue_lug"] = lug
    hinge("hinge_glue", "P4", "glue_lug", (X(c4), y1), (X(c4), y2))

    # --- REG_GLI_EXT: the lug runs past each body score and folds there
    if e:
        for end, ya, yb in (("bottom", y1 - e, y1), ("top", y2, y2 + e)):
            fid = f"glue_lug_ext_{end}"
            f = _rect_face(fid, "lug_ext", "lug_ext", X(x_end), X(c4 + h), ya, yb,
                           up=1 if end == "top" else -1, mirror=True)
            _edges(f, {"top": ("free_edge", "cut"), "bottom": ("hinge", "score"),
                       "left": ("left", "cut"), "right": ("right", "cut")})
            f.up = "glue lug extension seen from outside with its free edge up; its hinge is at the bottom"
            _centre(f, f.width / 2)
            f.material_centre_x = f.width / 2
            f.parent, f.hinge, f.panel = "glue_lug", f"hinge_lug_ext_{end}", "glue_lug"
            f.polygon = f.corners_flat()
            faces[fid] = f
            y = y2 if end == "top" else y1
            lo, hi = sorted((X(c4 + h), X(x_end)))
            # Child above (top): walk +X. Child below (bottom): walk -X.
            hinge(f.hinge, "glue_lug", fid, *(((lo, y), (hi, y)) if end == "top" else ((hi, y), (lo, y))))

    # --- flaps. On a tray the drawing's flap row is the bottom of the folded box.
    for row, y, flat_end in ((blank.bottom, y1, "bottom"), (blank.top, y2, "top")):
        if not row:
            continue
        end = {"top": "bottom", "bottom": "top"}[flat_end] if tray else flat_end
        style = top_style if flat_end == "top" else bottom_style
        for i in range(4):
            lo_x, hi_x = (xs[i] + h if i else 0.0, xs[i + 1] - h)
            depth = row[i % 2]
            fid = f"{end}_flap_P{i + 1}"
            ya, yb = (y, y + depth) if flat_end == "top" else (y - depth, y)
            f = _rect_face(fid, "flap", "flap", X(hi_x), X(lo_x), ya, yb, up=1 if flat_end == "top" else -1,
                           mirror=True)
            _edges(f, {"top": ("free_edge", "cut"), "bottom": ("hinge", "score"),
                       "left": ("left", "cut"), "right": ("right", "cut")})
            body = faces[f"P{i + 1}"]
            # Which of the flap's own edges is which, from the flap's own two ends (exact, however
            # narrow): its lo end is toward the previous panel (P1: the free edge), its hi end
            # toward the next panel (P4: the glue score). The body's matching sides follow the same flat X.
            lo_flat, hi_flat = X(lo_x), X(hi_x)
            body_high = "left" if body.x_axis[0] < 0 else "right"  # the body side at the larger flat X
            body_low = "right" if body_high == "left" else "left"
            sides = {}
            for fs, fx in (("left", 0.0), ("right", f.width)):
                flat_x = f.to_flat((fx, 0.0))[0]
                at_lo = abs(flat_x - lo_flat) < abs(flat_x - hi_flat)
                sides[fs] = names_of(body, body_high if at_lo else body_low)
            f.up = (f"{end} flap of P{i + 1}, seen from outside with its free edge up (flat, as hinged); its hinge "
                    f"is at the bottom; its left side is on P{i + 1}'s {_bare(sides['left'])} side, its right side "
                    f"on P{i + 1}'s {_bare(sides['right'])} side")
            # Its assembled centre is its panel's: carry the panel's centre line across.
            centre_flat = body.to_flat((body.centre_x, 0.0))
            _centre(f, f.from_flat((centre_flat[0], f.origin[1]))[0])
            f.material_centre_x = f.width / 2
            f.parent, f.hinge, f.end, f.panel = f"P{i + 1}", f"hinge_{flat_end}_flap_P{i + 1}", end, f"P{i + 1}"
            if style in ("regular", "gap_lap"):
                f.outer = roles[i] == "side"
            f.polygon = f.corners_flat()
            faces[fid] = f
            lo, hi = sorted((X(lo_x), X(hi_x)))
            hinge(f.hinge, f"P{i + 1}", fid, *(((lo, y), (hi, y)) if flat_end == "top" else ((hi, y), (lo, y))))

    # --- keep-out: where the glue lug lands on P1 when folded
    p1 = faces["P1"]
    gw = blank.glue
    side = "outside" if closure == "outside" else "inside"
    free_left = p1.lines["left"].kind == "free_edge"
    x0, x1 = (0.0, gw) if free_left else (p1.width - gw, p1.width)
    _keep(p1, "glue_landing", (x0, 0.0, x1, p1.height), side,
          f"the glue lug lands here ({side} face), {gw:g} in wide along the free edge")
    if e:  # the extended lug also lands on P1's flaps, up from their hinges
        free_flat_x = p1.to_flat((0.0, 0.0) if free_left else (p1.width, 0.0))[0]
        for end in ("top", "bottom"):
            fl = faces.get(f"{end}_flap_P1")
            if fl is None:
                continue
            fx = fl.from_flat((free_flat_x, fl.origin[1]))[0]
            strip = min(gw, fl.width)  # the extension's outer edge is flush with the lug's
            gx0, gx1 = (0.0, strip) if abs(fx) <= abs(fx - fl.width) else (fl.width - strip, fl.width)
            _keep(fl, "glue_landing", (gx0, 0.0, gx1, e), side,
                  f"the extended glue lug lands here, {e:g} in up from the hinge")

    # --- flaps over flaps when the box is closed, worked out in the assembled plane (nominal,
    # from the score lines; each flap reaches its depth less IL in from its wall, and the walls are
    # the raw inside dimensions apart).
    delta_j = board.il if closure == "outside" else 0.0

    def setback(j: int, toward_next: bool) -> float:
        """How far flap j stops short of the corner it shares with the next or previous panel.
        Half a slot at every panel score. At the joint corner (P4 → P1) it depends on the joint:
        P1's flap is flush with P1's free edge, which falls 1/8 (+ IL on an outside joint) short of
        the corner; P4's flap stops half a slot short of the glue score, which is on the corner
        (inside joint) or IL beyond it (outside)."""
        if toward_next and j == 3:
            return h - delta_j
        if not toward_next and j == 0:
            return 0.125 + delta_j
        return h

    for row, flat_end in ((blank.bottom, "bottom"), (blank.top, "top")):
        if not row:
            continue
        end = {"top": "bottom", "bottom": "top"}[flat_end] if tray else flat_end
        style = top_style if flat_end == "top" else bottom_style
        ids = [f"{end}_flap_P{i + 1}" for i in range(4)]
        reach = [row[i % 2] - board.il for i in range(4)]
        for i in range(4):
            A = faces[ids[i]]
            # The opposite flap: where the two overlap, either may be on top.
            o = (i + 2) % 4
            B = faces[ids[o]]
            lap_ = reach[i] + reach[o] - raw[(i + 1) % 4]
            if lap_ > 1e-9:
                # B's span, reflected onto A about their shared assembled centre (opposite walls
                # face opposite ways), so the band only covers where B really is.
                bx0, bx1 = A.centre_x + B.centre_x - B.width, A.centre_x + B.centre_x
                over = lap_ > A.height
                note = (f"overlaps the opposite flap by about {lap_:.4g} in when closed; which one is on top "
                        "depends on the order they close" + ("; the opposite flap reaches past this flap's hinge"
                                                             if over else ""))
                _keep(A, "overlap_band", (bx0, A.height - lap_, bx1, A.height), "conditional", note)
            # On a regular or gap/lap box the inner flaps close first and the outer flaps lie over them.
            if style not in ("regular", "gap_lap") or A.outer:
                continue
            next_right = A.x_axis[0] < 0  # x runs toward smaller flat X, where the next panel is
            half = raw[i] / 2  # the side walls are the inside width apart, about the assembled centre
            for j, toward in (((i + 1) % 4, "next"), ((i - 1) % 4, "previous")):
                on_right = (toward == "next") == next_right
                wall = A.centre_x + half if on_right else A.centre_x - half
                x0, x1 = (wall - reach[j], wall) if on_right else (wall, wall + reach[j])
                # Flap j's end at this corner: it's the corner toward j's previous panel when A is j's next.
                y0 = setback(j, toward_next=(toward == "previous"))
                _keep(A, "covered", (x0, y0, x1, A.height), "outside",
                      f"covered by P{j + 1}'s flap when the box is closed")

    fm = FaceMap(faces, hinges)
    body_ids = [f"P{i}" for i in range(1, 5)]
    fm.selectors = {
        "body_panels": body_ids,
        "end_panels": [f for f in body_ids if faces[f].role == "end"],
        "side_panels": [f for f in body_ids if faces[f].role == "side"],
    }
    for end in ("top", "bottom"):
        flaps = [f"{end}_flap_P{i}" for i in range(1, 5) if f"{end}_flap_P{i}" in faces]
        if not flaps:
            why = f"this part has no {end} flaps"
            for name in (f"{end}_flaps", f"{end}_outer_flaps", f"{end}_inner_flaps"):
                fm.unavailable[name] = why
            continue
        fm.selectors[f"{end}_flaps"] = flaps
        if all(faces[f].outer is not None for f in flaps):
            fm.selectors[f"{end}_outer_flaps"] = [f for f in flaps if faces[f].outer]
            fm.selectors[f"{end}_inner_flaps"] = [f for f in flaps if not faces[f].outer]
        else:
            why = "outer and inner flaps aren't defined for full-overlap or CSSC flaps; name the flaps"
            fm.unavailable[f"{end}_outer_flaps"] = fm.unavailable[f"{end}_inner_flaps"] = why
    return fm


def attach_edges(fm: FaceMap, d: Any, tol: float = 1e-6) -> None:
    """Fill each face's exact boundary spans from the drawing, and point its
    reference lines at them: a score's line keeps its construction length but
    gains the crease operation and its actual span; a physical edge (free edge,
    open edge) is cut back to where its cut really runs."""
    from .geometry import Line, LineType  # noqa: PLC0415

    segs = [s for s in d.segments if isinstance(s, Line) and s.type in (LineType.CUT, LineType.CREASE)]
    for f in fm.faces.values():
        spans: list[EdgeSpan] = []
        poly = f.polygon
        for a, b in zip(poly, poly[1:] + poly[:1]):
            L = math.dist(a, b)
            if L <= tol:
                continue
            ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
            pieces = []
            for sg in segs:
                lat = [abs((p_[0] - a[0]) * uy - (p_[1] - a[1]) * ux) for p_ in (sg.start, sg.end)]
                if max(lat) > tol:
                    continue
                t0, t1 = sorted(((sg.start[0] - a[0]) * ux + (sg.start[1] - a[1]) * uy,
                                 (sg.end[0] - a[0]) * ux + (sg.end[1] - a[1]) * uy))
                lo, hi = max(t0, 0.0), min(t1, L)
                if hi - lo > tol:
                    pieces.append((lo, hi, sg))
            for lo, hi, sg in sorted(pieces, key=lambda x: x[0]):
                pa = f.from_flat((a[0] + ux * lo, a[1] + uy * lo))
                pb = f.from_flat((a[0] + ux * hi, a[1] + uy * hi))
                kind = "cut" if sg.type == LineType.CUT else "crease"
                across = None
                if kind == "crease" and sg.op in fm.hinges:
                    hg = fm.hinges[sg.op]
                    across = hg.child if hg.parent == f.id else hg.parent
                spans.append(EdgeSpan(_side_of(f, pa, pb), kind, pa, pb, sg.op, across))
        f.edges = spans
        for side in ("top", "bottom", "left", "right"):
            mine = [e for e in spans if e.side == side]
            creases = [e for e in mine if e.kind == "crease"]
            cuts = [e for e in mine if e.kind == "cut"]
            for ln in [f.lines[side]] + [x for x in f.lines.values() if x is not f.lines[side]
                                          and x.start == f.lines[side].start and x.end == f.lines[side].end]:
                if creases and ln.kind in ("score", "glue_score"):
                    c = max(creases, key=lambda e: math.dist(e.start, e.end))
                    ln.op, ln.span = c.op, (c.start, c.end)
                elif cuts and ln.kind in ("free_edge", "open_edge") and ln.name not in ("top", "bottom", "left", "right"):
                    pts = [p_ for e in cuts for p_ in (e.start, e.end)]
                    axis = 1 if side in ("left", "right") else 0
                    lo_p, hi_p = min(pts, key=lambda q: q[axis]), max(pts, key=lambda q: q[axis])
                    ln.start, ln.end = (lo_p, hi_p) if _same_dir(ln, lo_p, hi_p) else (hi_p, lo_p)


def _side_of(f: Face, a: Point, b: Point, tol: float = 1e-6) -> str:
    if abs(a[1]) <= tol and abs(b[1]) <= tol:
        return "bottom"
    if abs(a[1] - f.height) <= tol and abs(b[1] - f.height) <= tol:
        return "top"
    if abs(a[0]) <= tol and abs(b[0]) <= tol:
        return "left"
    if abs(a[0] - f.width) <= tol and abs(b[0] - f.width) <= tol:
        return "right"
    return "taper"


def _same_dir(ln: RefLine, a: Point, b: Point) -> bool:
    return (ln.end[0] - ln.start[0]) * (b[0] - a[0]) + (ln.end[1] - ln.start[1]) * (b[1] - a[1]) >= 0


def opposite_pairs(fm: FaceMap) -> list[tuple[str, str]]:
    """Opposite faces on the folded box, for mirrored placements: P1↔P3, P2↔P4, and their flaps."""
    pairs = [("P1", "P3"), ("P2", "P4")]
    for end in ("top", "bottom"):
        for a, b in (("1", "3"), ("2", "4")):
            fa, fb = f"{end}_flap_P{a}", f"{end}_flap_P{b}"
            if fa in fm.faces and fb in fm.faces:
                pairs.append((fa, fb))
    return pairs


def polygon_area(poly: list[Point]) -> float:
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(poly, poly[1:] + poly[:1]))) / 2

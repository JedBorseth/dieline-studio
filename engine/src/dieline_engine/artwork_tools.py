"""The agent's artwork tool, `set_artwork`, and the designer's approvals
(docs/DESIGN-SYSTEM.md, "Agent tools" and "Artwork").

- `set_artwork(part?, items?, remove?, dry_run?)`: batch upsert by id like
  set_features. Moving or resizing an item by id doesn't resend its sketch.
- An **import** (an item of kind import with a path and no id) is read from the
  reference folders exactly as read_file is, parsed, and shown to the designer
  with its retained and discarded result and everything it couldn't read. It's
  never committed by the agent: the designer's click on the chat card commits
  it (`approve_import`), and only then can the agent move or resize it.
- Suspicious-artwork flags are acknowledged only by the designer, in the checks
  panel (`acknowledge`); no tool here can, and the store refuses a chat change
  that adds an acknowledgement.
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import artwork
from .artwork_import import ImportRefused, parse, sha256, to_json
from .artwork_sources import LIBRARY, SourceError
from .designstore import acknowledge, refuse_stale, set_artwork

DIST = {"type": ["number", "string"],
        "description": "A length in the part's units: a number, a fraction ('2 3/4') or an expression"}

ITEM = {
    "type": "object",
    "description": (
        "One artwork item. id: leave out for a new one; give it to change that item (fields left out keep their "
        "values: moving or resizing a sketch doesn't resend it). kind and its fields: "
        "shape {shape circle|ellipse|rounded_rect|polygon|star|heart|smiley|arrow, diameter (circle, polygon, star, "
        "smiley), width + height (ellipse, rounded_rect, heart), length + width (arrow, pointing right; turn with "
        "rotation_deg), corner_radius, sides, points, inner_ratio, outline none|crease (smiley)}; "
        "stencil_text {text, height (capital height), spacing}: cut lettering with stencil breaks, no islands; "
        "stroke_text {text, height}: single lines, crease or annotation only; "
        "sketch {sketch: {shapes, paths}, width or height, closed}: anything else, drawn on a 0-100 canvas in SVG "
        "coordinates (x right, y DOWN), shapes {type circle cx cy r | ellipse cx cy rx ry rotation | rect x y width "
        "height r | polygon points [[x, y], ...]} and paths (SVG path strings using only M L Q C A Z), scaled "
        "uniformly to width or height; "
        "import {path (from search_files), width | height | fit_box, units (for a file without units)}: an SVG or "
        "DXF, shown to the designer to approve. "
        "Every kind: placement (one face), rule cut|crease|perforation|partial_cut|annotation (default cut; "
        "stroke text crease), loop_rules {\"2\": \"crease\"} per outline number from the read-back, islands "
        "bridge|stencil|partial_cut|drop_out or {\"i1\": ...}, bridge_width."
    ),
    "properties": {
        "id": {"type": "string"}, "kind": {"type": "string", "enum": list(artwork.KINDS)},
        "label": {"type": "string"},
        "shape": {"type": "string", "enum": list(LIBRARY)},
        "width": DIST, "height": DIST, "diameter": DIST, "corner_radius": DIST, "length": DIST,
        "head_length": DIST, "shaft_width": DIST,
        "sides": {"type": "integer"}, "points": {"type": "integer"}, "inner_ratio": {"type": "number"},
        "outline": {"type": "string", "enum": ["none", "crease"]},
        "text": {"type": "string"}, "spacing": DIST,
        "sketch": {"type": "object", "properties": {
            "shapes": {"type": "array", "items": {"type": "object"}},
            "paths": {"type": "array", "items": {"type": ["string", "object"]}}}, "additionalProperties": False},
        "closed": {"type": "boolean", "description": "Paths are meant closed: ends within 0.005 in snap shut"},
        "path": {"type": "string", "description": "An SVG or DXF from search_files"},
        "fit_box": {"type": "array", "items": DIST, "minItems": 2, "maxItems": 2},
        "units": {"type": "string", "enum": list(artwork.UNITS)},
        "rule": {"type": "string", "enum": list(artwork.RULES)},
        "loop_rules": {"type": "object", "additionalProperties": {"type": "string", "enum": list(artwork.RULES)}},
        "islands": {"anyOf": [{"type": "string", "enum": list(artwork.FIXES)},
                              {"type": "object", "additionalProperties": {"type": "string",
                                                                         "enum": list(artwork.FIXES)}}]},
        "bridge_width": DIST,
        "placement": {"type": "object", "description": "As for features: on (one face), x, y, rotation_deg"},
        "pointage": {"type": "number"},
    },
    "additionalProperties": False,
}

TOOLS: list[dict[str, Any]] = [{
    "name": "set_artwork",
    "description": (
        "Add, change or remove decorative artwork on one part: library shapes, stencil or stroke lettering, your "
        "own sketch, or an SVG/DXF from the reference folders. Batch upsert by id like set_features. Artwork sits "
        "on ONE face, clear of every score, slot and edge; it never changes the structure. An import is shown to "
        "the designer to approve in the chat and isn't added until they do. Returns each item's faces, size, "
        "outlines and rules, its islands, the checks and a picture."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "part": {"type": "string", "description": "Part id (p1, p2, ...) or label"},
            "items": {"type": "array", "items": ITEM, "maxItems": 20},
            "remove": {"type": "array", "items": {"type": "string"}, "description": "Artwork ids to remove"},
            "dry_run": {"type": "boolean"},
        },
        "additionalProperties": False,
    },
}]
NAMES = {t["name"] for t in TOOLS}
KEYS = {"set_artwork": {"part", "items", "remove", "dry_run"}}

PROMPT = """

## Artwork (cut-outs, lettering, logos)
set_artwork puts decorative artwork on ONE face of a box part: library \
shapes (circle, ellipse, rounded_rect, polygon, star, heart, smiley, arrow), \
stencil_text (the default for cut lettering: its stencil breaks keep every \
counter attached), stroke_text (single lines, crease or annotation only), \
your own sketch, or an SVG/DXF from the reference folders.
- No structure in artwork, ever: never sketch flaps, slots, tabs, scores, \
panels, notches or outline changes. Structure comes from styles and features. \
Artwork can't touch a score, slot or edge, change the outline or be a mate.
- Placement is the feature placement (outside view, a named face, edge or \
centre for every distance; ask when the user didn't say).
- Sketch only what the library and text can't draw. The canvas is 0-100 in \
SVG coordinates (x right, y DOWN). Shapes: circle, ellipse, rect, polygon; \
paths use only M L Q C A Z. It's scaled uniformly to the width or height you \
give. An arc whose radius can't reach its end point is refused if SVG would \
enlarge it by more than 1%. An open path can't be cut; close it with Z.
- Islands: board left inside a hole (the middle of an O, a ring) has nothing \
holding it. The result lists each island (i1, i2, ...). The fixes are bridge \
(two corridors of board), stencil, partial_cut (the hole's outer loop becomes \
a partial cut) and drop_out (it falls out). partial_cut and drop_out change \
what's made: ask the user before choosing them.
- Imports: give the path from search_files. The designer sees what will be \
cut, what was left out and approves it with the card in the chat; you can't \
approve it. Once it's added you can move or resize it by id.
- Suspicious artwork (a long straight next to a score, a slot-like cut near \
an edge, artwork spanning most of a face, long crease or perforation artwork) \
blocks export until the DESIGNER acknowledges it in the checks panel. You \
can't acknowledge it; tell the user what was flagged and why.
- Read-back after any artwork change: what it is and its source, its size, \
face and placement (edge or centre), its rules, and its islands with the \
options. For your own sketch always say "agent-drawn sketch, verify before \
cutting". Mention artwork as a link, e.g. [smiley a1](feature:p1/a1)."""


# ------------------------------------------------------------------ pending imports


@dataclass(slots=True)
class Pending:
    id: str
    session: str
    design: tuple[str, int]
    part: str
    item: dict[str, Any]
    unsupported: list[str]
    name: str


class Imports:
    """Imports shown to the designer and waiting for their approval, per chat."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, Pending] = {}
        self._n = itertools.count(1)

    def add(self, session: str, design: tuple[str, int], part: str, item: dict[str, Any], unsupported: list[str],
            name: str) -> str:
        with self._lock:
            iid = f"import{next(self._n)}"
            self._items[iid] = Pending(iid, session, design, part, item, unsupported, name)
            while len(self._items) > 50:
                self._items.pop(next(iter(self._items)))
            return iid

    def take(self, session: str, iid: str) -> Pending:
        with self._lock:
            p = self._items.get(iid)
            if p is None or p.session != session:
                raise ValueError("that import isn't waiting any more; ask the chat to import the file again")
            return p

    def drop(self, iid: str) -> None:
        with self._lock:
            self._items.pop(iid, None)

    def expire_session(self, session: str) -> None:
        with self._lock:
            for k in [k for k, v in self._items.items() if v.session == session]:
                del self._items[k]


# ------------------------------------------------------------------ dispatch


def _lists(args: dict[str, Any]) -> tuple[list[Any] | None, list[Any] | None]:
    items, remove = args.get("items"), args.get("remove")
    if items is not None and not isinstance(items, list):
        raise ValueError("items must be a list of artwork items")
    if remove is not None and (not isinstance(remove, list) or not all(isinstance(r, str) for r in remove)):
        raise ValueError("remove must be a list of artwork ids")
    if not items and not remove:
        raise ValueError("set_artwork needs items to add or change, or ids to remove")
    return items, remove


def dispatch(ag: Any, s: Any, name: str, args: dict[str, Any], call_id: str | None) -> dict[str, Any]:
    unknown = sorted(set(args) - KEYS[name])
    if unknown:
        raise ValueError(f"{name} has no argument(s) {', '.join(unknown)}")
    replay = ag.store.replay(s.id, ag._request_id(s, call_id) or "")
    if replay is not None:
        ag._advance(s, replay)
        return ag._design_view(replay, replay.get("focus"))
    ag._check_bound(s)
    cur = ag._read(s)
    part = cur.part(args.get("part") if isinstance(args.get("part"), str) else None)
    items, remove = _lists(args)
    dry = args.get("dry_run", False)
    if not isinstance(dry, bool):
        raise ValueError("dry_run must be true or false")
    imports = [i for i in items or [] if isinstance(i, dict) and i.get("kind") == "import" and not i.get("id")]
    if imports:
        if len(items or []) != 1 or remove:
            raise ValueError("an import goes in a set_artwork call of its own (one item, nothing removed), so the "
                             "designer approves exactly that")
        return _import(ag, s, cur, part.id, imports[0], call_id)
    _, _, touched = set_artwork(cur, part.id, items, remove)  # validates before anything runs
    accept = (lambda p: refuse_stale(p, part.id, touched)) if touched else None  # noqa: E731
    payload = ag._mutate(s, lambda d: set_artwork(d, part.id, items, remove)[0], call_id, dry, part.id, accept)
    return ag._design_view(payload, part.id)


def _import(ag: Any, s: Any, cur: Any, part_id: str, raw: dict[str, Any], call_id: str | None) -> dict[str, Any]:
    path = raw.get("path")
    if not isinstance(path, str) or not path.strip():
        raise ValueError("an import needs the path of an SVG or DXF from search_files")
    if Path(path).suffix.lower() not in (".svg", ".dxf"):
        raise ValueError("artwork can be imported from SVG or DXF files only")
    item = artwork.normalize({k: v for k, v in raw.items() if k != "id"}, None)  # checks its shape first
    s.private = True  # file paths and contents are private
    real, data = ag._references().read_bytes(path)  # confined to the reference folders, as read_file is
    try:
        parsed = parse(real.name, data)
    except (ImportRefused, SourceError) as exc:
        raise ValueError(f"can't import {real.name}: {exc}") from None
    item["source"] = {"name": real.name, "path": str(real), "sha256": sha256(data), "format": parsed.format,
                      "units_in": parsed.units_in, "geometry": to_json(parsed.subs),
                      "unsupported": parsed.unsupported, "notes": parsed.notes}
    item.pop("path", None)
    item["path"] = str(real)

    def change(d: Any) -> Any:
        return set_artwork(d, part_id, [item], None, trusted=True)[0]

    # Built and checked, never committed here: the designer approves it. Artwork that can't be placed
    # (outside its face, across a score) is refused now, so the agent fixes its size or position before
    # a card is shown that would fail when the designer clicks it.
    payload = ag._mutate(s, change, call_id, True, part_id)
    view = ag._design_view(payload, part_id)
    new = [f for p in payload["parts"] if p["part_id"] == part_id for f in p.get("features", [])
           if f["id"] not in {a["id"] for a in cur.part(part_id).params.artwork} and f.get("kind") == "artwork"]
    if len(new) != 1:
        raise ValueError("could not identify the artwork to approve")
    refuse_stale(payload, part_id, [new[0]["id"]])
    iid = ag.artwork_imports.add(s.id, (cur.id, cur.revision), part_id, item, parsed.unsupported, real.name)
    view["import"] = {
        "id": iid, "file": real.name, "format": parsed.format, "sha256": item["source"]["sha256"][:16],
        "units": "from the file" if parsed.units_in else "none in the file (a size is needed)",
        "outlines": len(parsed.subs), "unsupported": parsed.unsupported, "notes": parsed.notes,
        "artwork": new[0],
        "status": "waiting for the designer: they approve it with the card under this tool result. You can't.",
    }
    view["dry_run"] = "nothing was changed: the import waits for the designer's approval"
    return view


def approve_from_card(ag: Any, session_id: str, iid: str, without_unsupported: bool = False) -> dict[str, Any]:
    """The designer approved an import on its chat card: commit it as their own change."""
    p = ag.artwork_imports.take(session_id, iid)
    cur = ag.store.current(session_id)
    if cur is None or (cur.id, cur.revision) != p.design:
        ag.artwork_imports.drop(iid)
        raise ValueError("the design changed after this import was shown; ask the chat to import the file again")
    if p.unsupported and not without_unsupported:
        raise ValueError(f"{p.name} has content the importer can't read ({'; '.join(p.unsupported)}); confirm "
                         "importing it without that")
    holder: dict[str, Any] = {}

    def change(d: Any) -> Any:
        state, _, touched = set_artwork(d, p.part, [p.item], None, trusted=True)
        holder["id"] = touched[0]
        return state

    payload = ag.store.mutate(session_id, change, expected=p.design, origin="form",
                              accept=lambda pl: refuse_stale(pl, p.part, [holder["id"]]))
    ag.artwork_imports.drop(iid)
    ag.note_choice(session_id, f"[The designer approved the import of {p.name} as artwork {holder['id']} on part "
                               f"{p.part}" + (" without the content it couldn't read" if p.unsupported else "") +
                   ". Read it back to them.]")
    return payload


def acknowledge_flags(ag: Any, session_id: str, design: tuple[str, int], part_id: str, keys: list[str]
                      ) -> dict[str, Any]:
    """The designer acknowledged suspicious-artwork flags in the checks panel."""
    if not keys or not all(isinstance(k, str) for k in keys):
        raise ValueError("keys must list the flags to acknowledge")
    cur = ag.store.snapshot(session_id, design[0], design[1])
    built = ag.store.payload(cur)
    part = next((x for x in built["parts"] if x["part_id"] == part_id), None)
    if part is None:
        raise ValueError(f"no part {part_id!r}")
    offered = {c["ack"] for c in part["checks"] if c.get("ack")}
    bad = [k for k in keys if k not in offered]
    if bad:
        raise ValueError("those flags aren't shown on this design any more; look again")
    payload = ag.store.mutate(session_id, lambda d: acknowledge(d, part_id, keys), expected=design, origin="form")
    ag.note_choice(session_id, f"[The designer acknowledged suspicious-artwork flag(s) on part {part_id}: "
                               + ", ".join(k.split(":")[0] + " " + k.split(":")[1] for k in keys) + "]")
    return payload

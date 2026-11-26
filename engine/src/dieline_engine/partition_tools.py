"""The chat's partition, fit-link and stack tools (docs/DESIGN-SYSTEM.md,
"Agent tools"). Kept out of agent.py so the agent loop stays generic; agent.py
lists these tools and sends their calls here.

All parameter-only: the model gives cells, sizes, counts and boards; the
engine works out the grid, the slots and the pieces. Every mutating call goes
through the agent's turn binding, retry ids and `dry_run`, like create_box.
"""

from __future__ import annotations

import uuid
from typing import Any

from . import designs, partitions, partsets, render
from .layouts import names_candidate
from .partitions import Box, Refused

NAMES = ("create_partition", "choose_layout", "link_fit", "unlink_fit", "create_stack")

_LEN = {"type": ["number", "string"],
        "description": "A length in the units given: a number, a fraction ('5 1/4'), ArtiosCAD sixteenths "
                       "('5:04', inches only) or an expression ('D - 1/2', '2 * CAL')"}
_LIST = {"type": "array", "items": _LEN}
_RULES = {
    "units": {"type": "string", "enum": ["in", "mm"]},
    "board": {"type": "string", "description": "Board code for every piece (default 32 B KR)"},
    "board_long": {"type": "string", "description": "Board for the long pieces (default: board)"},
    "board_short": {"type": "string", "description": "Board for the short pieces (default: board)"},
    "boards_long": {"type": "array", "items": {"type": "string"},
                    "description": "One board per long piece, first wall first (mixed boards)"},
    "boards_short": {"type": "array", "items": {"type": "string"}},
    "height": {**_LEN, "description": "Piece height (default: the box's inside depth; shorter is allowed)"},
    "clearance": {**_LEN, "description": "Total drop-in clearance per axis (default 1/8), split between the ends"},
    "clearance_long": _LEN, "clearance_short": _LEN,
    "slot_dir": {"type": "string", "enum": ["long_top", "short_top"],
                 "description": "long_top (default): long pieces slotted from the top, short from the bottom"},
    "mouth": {"type": "string", "enum": ["lead_in", "square"], "description": "Default lead_in (1/8 x 45 deg)"},
    "lead_in": {**_LEN, "description": "Lead-in size (default 1/8); reduced automatically to fit"},
    "overcut": {**_LEN, "description": "How far each slot goes past the middle (default 1/32)"},
    "slot_width": {**_LEN, "description": "Only to override the rule (mating CAL + 1/32, or + 1/16 over 1/8 CAL)"},
    "flute": {"type": "string", "enum": ["vertical", "horizontal"], "description": "In the assembled set (default "
                                                                                   "vertical)"},
}
_ACTION = {
    "action": {"type": "string", "enum": ["add", "replace_design", "replace_set"],
               "description": "add: another set in the design. replace_design: the set becomes the whole design. "
                              "replace_set: replace the set named by set_id, keeping its id and links. Default: add "
                              "when contained_in is given, otherwise replace_design."},
    "set_id": {"type": "string", "description": "The partition set to replace (action replace_set)"},
    "contained_in": {"type": "string", "description": "The box part (id or label) the set goes in: it supplies the "
                                                      "inside size and a fit link is made"},
    "label": {"type": "string", "description": "A short name for the set's parts, e.g. 'divider'"},
    "dry_run": {"type": "boolean"},
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "create_partition",
        "description": (
            "Make a slotted partition set: interlocking long and short pieces, half-slotted so they cross. Give "
            "input_mode and its fields; the engine works out the cells, piece lengths, slots (width from the "
            "mating board), and one part per distinct piece with its quantity per set. "
            "box_cells: box inside length/width/depth + cells_long x cells_short (equal cells). "
            "cell_sizes: lists of installed clear cell sizes per axis (unequal allowed) + height or depth. "
            "pieces: piece types with length, height and slot centre positions measured from a named end. "
            "product: count + item size + item_height (+ max_box or contained_in); returns layout CANDIDATES "
            "and draws nothing (the designer picks one; see choose_layout). "
            "A 1 x n layout (plain dividers, no slots) needs confirm_degenerate: true; 1 x 1 is refused. "
            "Every partition is flagged 'not calibrated'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "input_mode": {"type": "string", "enum": list(partitions.MODES)},
                "length": {**_LEN, "description": "box_cells: box inside length (the long axis)"},
                "width": {**_LEN, "description": "box_cells: box inside width"},
                "depth": {**_LEN, "description": "Box inside depth (box_cells; optional otherwise, for the "
                                                 "height check)"},
                "cells_long": {"type": ["integer", "array"], "items": _LEN,
                               "description": "box_cells: how many cells along the long axis. cell_sizes: the "
                                              "list of installed cell sizes along it"},
                "cells_short": {"type": ["integer", "array"], "items": _LEN},
                "pieces": {"type": "array", "items": {"type": "object", "properties": {
                    "axis": {"type": "string", "enum": ["long", "short"]}, "length": _LEN, "height": _LEN,
                    "slots": _LIST, "from": {"type": "string", "enum": ["start", "end"]},
                    "board": {"type": "string"}, "quantity": {"type": "integer", "minimum": 1}},
                    "required": ["axis", "length", "height"]},
                    "description": "pieces mode: the piece types"},
                "count": {"type": "integer", "minimum": 2, "description": "product: how many items"},
                "item": {"type": "object", "description": "product: {diameter} or {length, width}",
                         "properties": {"diameter": _LEN, "length": _LEN, "width": _LEN}},
                "item_height": _LEN,
                "cell_clearance": {**_LEN, "description": "product: clearance per side of each item (default "
                                                          "1/16)"},
                "max_box": {"type": "object", "properties": {"length": _LEN, "width": _LEN, "depth": _LEN},
                            "description": "product: the biggest box inside the set may need"},
                "perimeter": {"description": "product: 'none' (default), {air_cells: width} (an empty ring of "
                                             "cells) or {liner: thickness} (room for liner pads)"},
                "confirm_degenerate": {"type": "boolean"},
                **_RULES, **_ACTION,
            },
            "required": ["input_mode"],
            "additionalProperties": False,
        },
    },
    {
        "name": "choose_layout",
        "description": (
            "Commit a product-mode layout candidate. Only after the designer has named that candidate (its id, "
            "e.g. c2) in a message: pass that message's id (each user message starts with [message m<n>]). The "
            "designer can also just click the candidate's card, which commits it without you. A candidate "
            "expires when the design, the board values or the rules change."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "candidate_id": {"type": "string"},
                "message_id": {"type": "string", "description": "The designer's message that names the candidate"},
                "confirm_degenerate": {"type": "boolean"},
                **_ACTION,
            },
            "required": ["candidate_id", "message_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "link_fit",
        "description": (
            "Say that one part or set goes inside another (a partition set in its carton, a base under its "
            "telescoping lid: the lid is the container). The fit check compares the container's inside with "
            "the contained's outside on every revision."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"container": {"type": "string", "description": "Part id/label or group id"},
                           "contained": {"type": "string", "description": "Part id/label or group id (a set)"},
                           "dry_run": {"type": "boolean"}},
            "required": ["container", "contained"],
            "additionalProperties": False,
        },
    },
    {
        "name": "unlink_fit",
        "description": "Remove a fit link by its id.",
        "input_schema": {"type": "object", "properties": {"link": {"type": "string"}, "dry_run": {"type": "boolean"}},
                         "required": ["link"], "additionalProperties": False},
    },
    {
        "name": "create_stack",
        "description": (
            "Stack partition sets in tiers with layer pads between them, as one group, so their heights plus "
            "the pads are checked against the container's inside depth."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tiers": {"type": "array", "items": {"type": "string"}, "description": "Partition set ids"},
                "pads": {"type": "integer", "minimum": 0, "description": "How many layer pads"},
                "pad_board": {"type": "string"},
                "contained_in": {"type": "string", "description": "The box the stack goes in"},
                "dry_run": {"type": "boolean"},
            },
            "required": ["tiers"],
            "additionalProperties": False,
        },
    },
]

_META = {"input_mode", "action", "set_id", "contained_in", "label", "dry_run"}
KEYS = {t["name"]: set(t["input_schema"]["properties"]) for t in TOOLS}

PROMPT = """

## Partitions
create_partition makes slotted partition sets (interlocking long and short \
pieces) from parameters; it never takes coordinates. Use box_cells when the \
user gives the box inside and a cell count, cell_sizes for given (possibly \
unequal) cell sizes, pieces for given piece lengths and slot positions (say \
which end they're measured from), and product for "N items of this size": \
product returns layout candidates as cards and draws nothing. Never call \
choose_layout until the designer names a candidate (its id) in a message; \
pass that message's id. The designer may simply click a card instead. With a \
carton in the design, pass contained_in so the set takes its inside size and \
a fit link is checked. A 1 x n layout is plain dividers with no slots: ask \
before passing confirm_degenerate. After any partition change, read it back \
in plain words: the layout, the installed cell sizes, each piece's size and \
quantity, the flute direction, and that the allowances are not calibrated \
(the tool result's read_back has these). Fit links (link_fit) say what goes \
inside what; tiers of sets with layer pads are a stack (create_stack)."""


def _container(cur: Any, ref: str | None) -> Box | None:
    if ref is None:
        return None
    if cur is None:
        raise ValueError("contained_in needs a box in the design first")
    part = cur.part(ref)
    if getattr(part.params, "style", None) == "partition-slotted":
        raise ValueError(f"{part.label} is a partition piece; contained_in names the box the set goes in")
    dims = part.params.dims_in()
    return Box(dims.length, dims.width, dims.depth)


def _action(args: dict[str, Any]) -> tuple[str, str | None, str | None, str | None]:
    contained_in = args.get("contained_in")
    action = args.get("action", "add" if contained_in is not None else "replace_design")
    set_id, label = args.get("set_id"), args.get("label")
    for k, v in (("contained_in", contained_in), ("set_id", set_id), ("label", label), ("action", action)):
        if v is not None and not isinstance(v, str):
            raise ValueError(f"{k} must be a string")
    if action not in ("add", "replace_design", "replace_set"):
        raise ValueError("action must be add, replace_design or replace_set")
    if action == "replace_set" and not set_id:
        raise ValueError("replace_set needs set_id (the set to replace)")
    if action != "replace_set" and set_id is not None:
        raise ValueError("set_id only goes with action replace_set")
    if action == "replace_design" and contained_in is not None:
        raise ValueError("replace_design would remove the box named by contained_in; use action add")
    return action, set_id, contained_in, label


def _change(action: str, set_id: str | None, spec: Any, label: str | None, contained_in: str | None, gid: str):
    if action == "replace_set":
        return lambda d: partsets.replace_set(d, set_id, spec, contained_in)  # type: ignore[arg-type]
    if action == "add":
        return lambda d: partsets.add_set(d, spec, label, contained_in, gid=gid)[0]
    return lambda d: partsets.add_set(None, spec, label, gid=gid)[0]


def _bool(args: dict[str, Any], key: str) -> bool:
    v = args.get(key, False)
    if not isinstance(v, bool):
        raise ValueError(f"{key} must be true or false")
    return v


def dispatch(ag: Any, s: Any, name: str, args: dict[str, Any], call_id: str | None) -> dict[str, Any]:
    unknown = sorted(set(args) - KEYS[name])
    if unknown:
        raise ValueError(f"{name} has no argument(s) {', '.join(unknown)}")
    replay = ag.store.replay(s.id, ag._request_id(s, call_id) or "")
    if replay is not None:  # a retry of a call already applied
        ag._advance(s, replay)
        return view(replay, replay.get("focus"))
    try:
        if name == "create_partition":
            return _create(ag, s, args, call_id)
        if name == "choose_layout":
            return _choose(ag, s, args, call_id)
    except Refused as exc:
        raise ValueError(f"refused, nothing changed: {exc}") from None
    ag._check_bound(s)
    dry = _bool(args, "dry_run")
    if name == "link_fit":
        c, k = args.get("container"), args.get("contained")
        if not isinstance(c, str) or not isinstance(k, str):
            raise ValueError("container and contained must be part ids, labels or group ids")
        payload = ag._mutate(s, lambda d: partsets.add_link(d, c, k)[0], call_id, dry, None)
        return view(payload, None)
    if name == "unlink_fit":
        lid = args.get("link")
        if not isinstance(lid, str):
            raise ValueError("link must be a fit link id")
        payload = ag._mutate(s, lambda d: partsets.remove_link(d, lid), call_id, dry, None)
        return view(payload, None)
    tiers, pads = args.get("tiers"), args.get("pads", 0)
    if not isinstance(tiers, list) or not all(isinstance(t, str) for t in tiers):
        raise ValueError("tiers must list partition set ids")
    pad_board, contained_in = args.get("pad_board"), args.get("contained_in")
    payload = ag._mutate(s, lambda d: partsets.add_stack(d, tiers, pads, pad_board, contained_in)[0], call_id, dry,
                         None)
    return view(payload, None)


def _create(ag: Any, s: Any, args: dict[str, Any], call_id: str | None) -> dict[str, Any]:
    mode = args.get("input_mode")
    if mode not in partitions.MODES:
        raise ValueError(f"input_mode must be one of {', '.join(partitions.MODES)}")
    action, set_id, contained_in, label = _action(args)
    dry = _bool(args, "dry_run")
    cur = ag.store.current(s.id)
    if action != "replace_design" or contained_in is not None or mode == "product":
        ag._check_bound(s)  # resolving against the design: it must be the one this turn saw
    container = _container(cur, contained_in)
    if container is None and action == "replace_set" and cur is not None:
        # The set's own box (its fit link) still gives the inside size; no new link is made.
        held_by = next((lk.container for lk in cur.links if lk.contained == set_id
                        and any(p.id == lk.container for p in cur.parts)), None)
        if held_by is not None and getattr(cur.part(held_by).params, "style", None) != "partition-slotted":
            container = _container(cur, held_by)
    inputs = {k: v for k, v in args.items() if k not in _META}
    if mode == "product":
        found = partitions.candidates(inputs, container)
        if not found:
            raise ValueError("no layout fits: check the item size, the count and the maximum box size")
        offered = ag.layouts.offer(s.id, found, s.binding, contained_in, label, turn=s.number)
        exact = any(c["spare_cells"] == 0 and not c["degenerate"] for c in offered)
        return {
            "candidates": offered,
            "note": ("Nothing is drawn yet. These cards are shown to the designer: wait for them to click one, or "
                     "to name one (its id) in a message before calling choose_layout. Sheet usage is an estimate."
                     + ("" if exact else f" There's no exact layout for {inputs['count']} without a 1 x n strip, "
                                         "so these have spare cells.")),
            "design_unchanged": True,
        }
    spec = partitions.spec_from_inputs(mode, inputs, container)
    gid = set_id if action == "replace_set" else f"set_{uuid.uuid4().hex[:6]}"
    payload = ag._mutate(s, _change(action, set_id, spec, label, contained_in, gid), call_id, dry, gid)
    return view(payload, gid)


def _choose(ag: Any, s: Any, args: dict[str, Any], call_id: str | None) -> dict[str, Any]:
    cid, mid = args.get("candidate_id"), args.get("message_id")
    if not isinstance(cid, str) or not isinstance(mid, str):
        raise ValueError("candidate_id and message_id must be strings")
    session = ag.sessions.get(s.id)
    said = session.user_messages.get(mid) if session is not None else None
    if said is None:
        raise ValueError(f"there's no designer message {mid!r} in this chat; choose_layout needs the id of the "
                         "designer's message that names the candidate")
    ag._check_bound(s)
    cand = ag.layouts.get(s.id, cid, s.binding)
    if int(mid.lstrip("m") or 0) <= cand.turn or not names_candidate(said, cid):
        raise ValueError(f"the designer hasn't named {cid} in message {mid}. Wait until they name it (or click its "
                         "card); don't choose for them")
    if cand.degenerate and not _bool(args, "confirm_degenerate"):
        raise ValueError(f"{cid} is a 1 x n layout (plain dividers, no slots); pass confirm_degenerate: true once "
                         "the designer has agreed to that")
    action, set_id, contained_in, label = _action({"contained_in": cand.contained_in, **args})
    spec = cand.spec.model_copy(update={"chosen": {"candidate": cid, "via": "chat", "message_id": mid}})
    container = _container(ag.store.current(s.id), contained_in)
    if container is not None:  # the box it goes in now, which may not be the one it was offered for
        spec = spec.model_copy(update={"depth": container.depth})
    gid = set_id if action == "replace_set" else f"set_{uuid.uuid4().hex[:6]}"
    payload = ag._mutate(s, _change(action, set_id, spec, label or cand.label, contained_in, gid), call_id,
                         _bool(args, "dry_run"), gid)
    return view(payload, gid)


def choose_from_card(ag: Any, session_id: str, cid: str, action: str | None = None,
                     set_id: str | None = None) -> dict[str, Any]:
    """The designer clicked a candidate's card: commit it as the designer's own change."""
    cur = ag.store.current(session_id)
    binding = (cur.id, cur.revision) if cur else None
    cand = ag.layouts.get(session_id, cid, binding)
    args: dict[str, Any] = {"contained_in": cand.contained_in}
    if action is not None:
        args["action"] = action
    if set_id is not None:
        args["set_id"] = set_id
    try:
        action_, set_id_, contained_in, label = _action(args)
        spec = cand.spec.model_copy(update={"chosen": {"candidate": cid, "via": "card"}})
        container = _container(cur, contained_in)
        if container is not None:
            spec = spec.model_copy(update={"depth": container.depth})
        gid = set_id_ if action_ == "replace_set" else f"set_{uuid.uuid4().hex[:6]}"
        payload = ag.store.mutate(session_id, _change(action_, set_id_, spec, cand.label, contained_in, gid),
                                  expected=binding, focus=gid, origin="form")
    except Refused as exc:
        raise ValueError(f"refused, nothing changed: {exc}") from None
    ag.note_choice(session_id, f"[The designer chose layout {cid} by clicking its card; it is now set {gid}.]")
    return payload


# ------------------------------------------------------------------ what the model sees


def view(payload: dict[str, Any], focus: str | None) -> dict[str, Any]:
    """A compact result: the design's parts in brief, the set's read-back, its
    pieces and their warnings, the design's own checks, and one picture."""
    parts = payload["parts"]
    out: dict[str, Any] = {
        "design_id": payload["design_id"], "revision": payload["revision"],
        "parts": [{"part": p["part_id"], "label": p["label"], "quantity": p["quantity"], "name": p["name"],
                   "overall": p["overall"]} for p in parts],
        "groups": payload["groups"], "links": payload["links"],
        "design_overall": payload["overall"],
    }
    if payload.get("design_checks"):
        out["design_checks"] = payload["design_checks"]
    if payload.get("dry_run"):
        out["dry_run"] = "nothing was changed"
    group = next((g for g in payload["groups"] if g["id"] == focus), None)
    if group is not None and group["kind"] == "partition_set":
        ids = [m["ref"] for m in group["members"]]
        members = [p for p in parts if p["part_id"] in ids]
        params = designs.parse_params(members[0]["params"])
        plan = partitions.plan_set(params.set)
        out["set"] = group["id"]
        out["read_back"] = partitions.read_back(plan)
        out["pieces"] = [{
            "part": p["part_id"], "label": p["label"], "role": p["summary"]["role"], "quantity": p["quantity"],
            "piece_in": p["summary"]["piece_in"], "end_legs_in": p["summary"]["end_legs_in"],
            "slots_in": p["summary"]["slots_in"], "overall": p["overall"],
            "warnings": [c for c in p["checks"] if c["status"] not in ("pass", "info", "not_applicable")],
        } for p in members]
        out["set_summary"] = members[0]["summary"]["set"]
        dl = [designs.build(designs.parse_params(p["params"])) for p in members]
        png, info = render.render_set_png(plan, dl, [(p["label"], p["quantity"]) for p in members])
        out["view"], out["_image"] = info, png
    else:
        chosen = next((p for p in parts if p["part_id"] == focus), parts[-1])
        out["part"] = chosen["part_id"]
        out.update(designs.agent_view(chosen))
        png, info = render.render_png(designs.build(designs.parse_params(chosen["params"])))
        out["view"], out["_image"] = info, png
    return out

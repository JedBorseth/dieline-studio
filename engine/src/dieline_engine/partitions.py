"""Slotted partitions: one crossing model for every input mode
(docs/DESIGN-SYSTEM.md, "Partitions").

Every input mode (`box_cells`, `cell_sizes`, `pieces`, `product`) is turned
into one **grid** before anything is drawn (`PartitionSpec`): the installed
clear cells along the long and short axes, the walls between them (each a
piece with its own board) and the rules. `plan_set` does the arithmetic once:

    installed clear cells:  sum(cells) + sum(t) = B
    piece length:           B - C
    slot centre, box frame: cell_0 + t_0/2, then + t_0/2 + cell_1 + t_1/2, ...
    slot centre, on piece:  box-frame position - C/2

where B is the box inside span of the axis, t the thickness (CAL) of each wall
crossing it and C the total drop-in clearance. Long pieces run along the long
axis and are slotted where the short pieces cross them, and the other way
round. A slot's width comes from the **mating** piece's caliper.

Each distinct piece (axis, size, board and slot plan) becomes one part of the
set's group, with its quantity. A part's parameters are the whole set's spec
plus its role, so any part rebuilds alone, with today's rules, from the inputs.

Every number here is an industry default, flagged "not calibrated" until it
has been compared with real R&L sets.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .boards import Board, get_board
from .checks import CheckResult, fmt_in
from .expressions import Env, ExpressionError, Value, evaluate

SLOTTED = "partition-slotted"
RULES_VERSION = "partition-rules-1"

# The rules table: industry defaults, not calibrated (inches).
CLEARANCE = 1 / 8  # drop-in clearance, total per axis, split between the ends
OVERCUT = 1 / 32  # each slot goes this far past the middle of the crossing
LEAD_IN = 1 / 8  # 45 degree lead-in at each slot mouth
MIN_BEND_RADIUS = 1 / 8  # tightest curve 2-pt steel rule bends to (docs: tooling limits)
LOOSE = 1 / 16  # a slot wider than the mating caliper plus this is loose
PLAY = 1 / 8  # more vertical play than this at a crossing warns
WEB_MIN = 1 / 2  # residual web under a slot
WEB_FLUTE = 1.0  # residual web on a vertical-flute piece
RIM_MIN = 1 / 4  # rim between neighbouring mouths: max(this, 2 x CAL)
SMALL_CELL_CALS = 3  # cells under 3 x CAL warn
MAX_CELLS = 40  # per axis
EPS = 1e-6
MM = 25.4

Axis = Literal["long", "short"]


class Refused(ValueError):
    """These inputs give a set that can't exist or can't be made; nothing is drawn."""


def slot_width_for(mating_cal: float) -> float:
    """Mating CAL <= 1/8: CAL + 1/32; thicker: CAL + 1/16."""
    return mating_cal + (1 / 32 if mating_cal <= 1 / 8 + EPS else 1 / 16)


# ------------------------------------------------------------------ the spec (inches)


class PartitionSpec(BaseModel):
    """The one grid every input mode becomes. All lengths in inches."""

    model_config = ConfigDict(extra="forbid")

    long_cells: list[float] = Field(description="Installed clear cells along the long axis")
    short_cells: list[float] = Field(description="Installed clear cells along the short axis")
    height_long: float
    height_short: float
    depth: float | None = None  # the box's inside depth, when known (the height check)
    boards_long: list[str]  # one per long piece (len(short_cells) - 1)
    boards_short: list[str]  # one per short piece (len(long_cells) - 1)
    clearance_long: float = CLEARANCE
    clearance_short: float = CLEARANCE
    slot_dir: Literal["long_top", "short_top"] = "long_top"
    mouth: Literal["lead_in", "square"] = "lead_in"
    lead_in: float = LEAD_IN
    overcut: float = OVERCUT
    slot_width: float | None = None  # overrides the width rule for every slot
    flute: Literal["vertical", "horizontal"] = "vertical"
    degenerate_confirmed: bool = False  # a 1 x n set (one piece type, no slots) was asked for
    units: Literal["in", "mm"] = "in"  # the units the inputs were given in (display only)
    input_mode: str = "cell_sizes"
    inputs: dict[str, Any] = Field(default_factory=dict)  # as given, expressions and all
    chosen: dict[str, Any] | None = None  # product mode: which candidate, and who chose it
    rules: str = RULES_VERSION


class PartitionParams(BaseModel):
    """One part of a partition set: the set's spec plus which piece it is."""

    model_config = ConfigDict(extra="forbid")

    style: Literal["partition-slotted"] = SLOTTED
    units: Literal["in", "mm"] = "in"
    board: str
    role: str
    set: PartitionSpec


# ------------------------------------------------------------------ the plan


@dataclass(frozen=True, slots=True)
class Slot:
    centre: float  # from the piece's start end
    width: float
    depth: float  # as drawn, to the very bottom
    usable: float  # the part as wide as the mating board
    edge: str  # top | bottom: the edge it's cut from
    bottom: str  # round | square
    lead_in: float  # each side of the mouth, after any reduction
    lead_in_reduced: bool
    mating_board: str
    mating_cal: float
    crossing: int  # which wall of the other axis it takes


@dataclass(slots=True)
class Piece:
    """One piece type: every wall on its axis with this exact plan."""

    role: str
    axis: str
    walls: list[int]  # which walls of the axis (0 = nearest the first box wall)
    length: float
    height: float
    board: str
    cal: float
    slots: list[Slot]
    end_legs: tuple[float, float]

    @property
    def quantity(self) -> int:
        return len(self.walls)

    def symmetric(self) -> bool:
        a = [(round(s.centre, 5), round(s.width, 5), round(s.depth, 5)) for s in self.slots]
        b = sorted((round(self.length - s.centre, 5), round(s.width, 5), round(s.depth, 5)) for s in self.slots)
        return a == b


@dataclass(slots=True)
class SetPlan:
    spec: PartitionSpec
    box_long: float  # B: the box inside span the set needs, per axis
    box_short: float
    positions_long: list[float]  # box frame: centre of each short piece along the long axis
    positions_short: list[float]  # centre of each long piece along the short axis
    t_long: list[float]  # CAL of each wall crossing the long axis (the short pieces)
    t_short: list[float]  # CAL of each long piece
    pieces: list[Piece] = field(default_factory=list)
    play: float = 0.0  # vertical play at the crossings (usable overlap beyond what's needed)
    notes: list[str] = field(default_factory=list)

    @property
    def degenerate(self) -> bool:
        return len(self.spec.long_cells) == 1 or len(self.spec.short_cells) == 1

    def piece(self, role: str) -> Piece:
        for p in self.pieces:
            if p.role == role:
                return p
        raise Refused(f"this set has no {role!r} piece; it has {', '.join(p.role for p in self.pieces)}")

    @property
    def outside(self) -> tuple[float, float, float]:
        """The assembled set's outside envelope: long piece length, short piece length, height."""
        s = self.spec
        return self.box_long - s.clearance_long, self.box_short - s.clearance_short, max(s.height_long, s.height_short)


def _cal(code: str) -> float:
    return get_board(code).cal


def _positions(cells: list[float], walls: list[float]) -> list[float]:
    out, x = [], 0.0
    for j, t in enumerate(walls):
        x += cells[j] + (walls[j - 1] / 2 if j else 0.0) + t / 2
        out.append(x)
    return out


def plan_set(spec: PartitionSpec) -> SetPlan:
    """The grid arithmetic, the slots and the piece types. Refuses a set that
    can't exist or can't be made, naming what to change."""
    lc, sc = spec.long_cells, spec.short_cells
    if not lc or not sc:
        raise Refused("a partition needs at least one cell along each axis")
    if len(lc) > MAX_CELLS or len(sc) > MAX_CELLS:
        raise Refused(f"at most {MAX_CELLS} cells per axis")
    if len(lc) == 1 and len(sc) == 1:
        raise Refused("1 x 1 cells is no partition at all: there are no dividers. Ask for at least 2 cells "
                      "along one axis (1 x n gives plain strips with no slots; 2 x 2 or more gives a slotted set)")
    if (len(lc) == 1 or len(sc) == 1) and not spec.degenerate_confirmed:
        n = max(len(lc), len(sc))
        raise Refused(f"1 x {n} cells is one piece type with no slots (plain dividers, not an interlocking set); "
                      "pass confirm_degenerate: true if that's what's wanted")
    for name, cells in (("long", lc), ("short", sc)):
        for i, c in enumerate(cells):
            if not math.isfinite(c) or c <= EPS:
                raise Refused(f"{name}-axis cell {i + 1} is {fmt_in(c)} in; cells must be bigger than 0")
    n_long, n_short = len(sc) - 1, len(lc) - 1  # long pieces separate the short-axis cells
    if len(spec.boards_long) != n_long or len(spec.boards_short) != n_short:
        raise Refused(f"the set has {n_long} long and {n_short} short pieces; give one board per piece "
                      f"(got {len(spec.boards_long)} and {len(spec.boards_short)})")
    for v, what in ((spec.height_long, "long piece height"), (spec.height_short, "short piece height"),
                    (spec.clearance_long, "long-axis clearance"), (spec.clearance_short, "short-axis clearance"),
                    (spec.lead_in, "lead-in")):
        if not math.isfinite(v) or v < 0 or (what.endswith("height") and v <= EPS):
            raise Refused(f"{what} must be {'greater than 0' if what.endswith('height') else 'at least 0'}")
    for v, what in ((spec.overcut, "overcut"), (spec.slot_width, "slot width"), (spec.depth, "box depth")):
        if v is not None and (not math.isfinite(v) or abs(v) > 240):
            raise Refused(f"{what} must be a finite length")
    if spec.slot_width is not None and spec.slot_width <= EPS:
        raise Refused("slot width must be greater than 0")
    t_long = [_cal(b) for b in spec.boards_short]
    t_short = [_cal(b) for b in spec.boards_long]
    plan = SetPlan(spec, sum(lc) + sum(t_long), sum(sc) + sum(t_short), _positions(lc, t_long),
                   _positions(sc, t_short), t_long, t_short)

    long_top = spec.slot_dir == "long_top"
    h = min(spec.height_long, spec.height_short)
    h_top = spec.height_long if long_top else spec.height_short
    usable_top = h_top - h / 2 + spec.overcut
    usable_bottom = h / 2 + spec.overcut
    if n_long and n_short:
        plan.play = interlock(usable_top, usable_bottom, h_top)

    walls: list[tuple[str, int, Piece]] = []
    for axis, count, boards, cells, positions_other, boards_other, clearance, height, edge, usable in (
        ("long", n_long, spec.boards_long, lc, plan.positions_long, spec.boards_short, spec.clearance_long,
         spec.height_long, "top" if long_top else "bottom", usable_top if long_top else usable_bottom),
        ("short", n_short, spec.boards_short, sc, plan.positions_short, spec.boards_long, spec.clearance_short,
         spec.height_short, "bottom" if long_top else "top", usable_bottom if long_top else usable_top),
    ):
        box = plan.box_long if axis == "long" else plan.box_short
        length = box - clearance
        legs = (cells[0] - clearance / 2, cells[-1] - clearance / 2)
        if min(legs) <= EPS:
            raise Refused(f"the {axis} pieces' end cells ({fmt_in(cells[0])} and {fmt_in(cells[-1])} in) are no "
                          f"bigger than half the drop-in clearance ({fmt_in(clearance / 2)} in)")
        for i in range(count):
            board = boards[i]
            slots = [_slot(axis, j, pos - clearance / 2, boards_other[j], spec, height, edge, usable)
                     for j, pos in enumerate(positions_other)]
            _mouths(slots, length, _cal(board), spec)
            walls.append((axis, i, Piece("", axis, [i], length, height, get_board(board).id, _cal(board), slots,
                                         legs)))
    # One part per distinct plan, in wall order; the first keeps the axis name as its role.
    for axis in ("long", "short"):
        kinds: list[Piece] = []
        for ax, i, p in walls:
            if ax != axis:
                continue
            sig = _signature(p)
            match = next((k for k in kinds if _signature(k) == sig), None)
            if match is not None:
                match.walls.append(i)
            else:
                kinds.append(p)
        for n, k in enumerate(kinds):
            k.role = axis if n == 0 else f"{axis}_{n + 1}"
        plan.pieces += kinds
    if plan.play > PLAY + EPS:
        plan.notes.append(f"vertical play {fmt_in(plan.play)} in at each crossing")
    return plan


def interlock(usable_top: float, usable_bottom: float, h_top: float) -> float:
    """Both pieces standing on a common floor: the top-slotted piece (height
    h_top) is solid below h_top - usable_top, the bottom-slotted one above
    usable_bottom. They pass only if those don't overlap. Returns the vertical
    play; refuses when the slots don't overlap."""
    play = usable_top + usable_bottom - h_top
    if play < -EPS:
        raise Refused(f"the slots don't overlap: together their usable depths ({fmt_in(usable_top)} + "
                      f"{fmt_in(usable_bottom)} in) are {fmt_in(-play)} in short of the {fmt_in(h_top)} in piece, "
                      "so the pieces can't both reach the floor. Deepen the slots (overcut) or check the heights")
    return play


def round_loss(width: float, mating_cal: float) -> float:
    """How much depth a full-radius bottom loses: below this the slot is narrower than the mating board."""
    r = width / 2
    half = min(mating_cal / 2, r)
    return r - math.sqrt(max(r * r - half * half, 0.0))


def _slot(axis: str, j: int, centre: float, mating: str, spec: PartitionSpec, height: float, edge: str,
          usable: float) -> Slot:
    t_m = _cal(mating)
    width = spec.slot_width if spec.slot_width is not None else slot_width_for(t_m)
    if width < t_m - EPS:
        raise Refused(f"slot width {fmt_in(width)} in is narrower than the mating board's caliper "
                      f"({get_board(mating).id}, {fmt_in(t_m)} in): the pieces can't go together")
    rnd = width >= 2 * MIN_BEND_RADIUS - EPS
    depth = usable + (round_loss(width, t_m) if rnd else 0.0)
    if depth >= height - EPS:
        raise Refused(f"a {axis} piece's slot is {fmt_in(depth)} in deep on a {fmt_in(height)} in piece: it cuts "
                      "through and splits the piece. Check the heights and the overcut")
    if usable <= EPS:
        raise Refused(f"a {axis} piece's slot has no usable depth; check the overcut")
    return Slot(centre, width, depth, usable, edge, "round" if rnd else "square", spec.lead_in if spec.mouth == "lead_in"
                else 0.0, False, get_board(mating).id, t_m, j)


def _mouths(slots: list[Slot], length: float, cal: float, spec: PartitionSpec) -> None:
    """Slots inside the piece and apart; lead-ins reduced so neighbouring mouths keep a rim."""
    rim = max(RIM_MIN, 2 * cal)
    for k, s in enumerate(slots):
        lo, hi = s.centre - s.width / 2, s.centre + s.width / 2
        if lo <= EPS or hi >= length - EPS:
            raise Refused(f"slot {k + 1} reaches past the piece end; the end cells are too small")
        if k and lo <= slots[k - 1].centre + slots[k - 1].width / 2 + EPS:
            raise Refused(f"slots {k} and {k + 1} overlap (their mouths would run together); the cell between "
                          "them is smaller than a slot")
    for k, s in enumerate(slots):
        if s.lead_in <= 0:
            continue
        room = [s.lead_in, (s.depth - (s.width / 2 if s.bottom == "round" else 0.0)) / 2]
        lo, hi = s.centre - s.width / 2, s.centre + s.width / 2
        room.append(lo - rim if k == 0 else (lo - (slots[k - 1].centre + slots[k - 1].width / 2) - rim) / 2)
        room.append(length - hi - rim if k == len(slots) - 1 else
                    ((slots[k + 1].centre - slots[k + 1].width / 2) - hi - rim) / 2)
        m = max(0.0, min(room))
        if m < 1e-4:
            m = 0.0
        if m < s.lead_in - EPS:
            slots[k] = Slot(s.centre, s.width, s.depth, s.usable, s.edge, s.bottom, m, True, s.mating_board,
                            s.mating_cal, s.crossing)


def _signature(p: Piece) -> tuple:
    return (round(p.length, 6), round(p.height, 6), p.board,
            tuple((round(s.centre, 6), round(s.width, 6), round(s.depth, 6), s.edge, s.bottom, round(s.lead_in, 6),
                   s.mating_board) for s in p.slots))


# ------------------------------------------------------------------ input modes


@dataclass(slots=True)
class Box:
    """A container's inside, inches (from contained_in)."""

    length: float
    width: float
    depth: float


_COMMON = {"units", "board", "board_long", "board_short", "boards_long", "boards_short", "clearance",
           "clearance_long", "clearance_short", "height", "slot_dir", "mouth", "lead_in", "overcut", "slot_width",
           "flute", "confirm_degenerate"}
MODE_KEYS = {
    "box_cells": {"length", "width", "depth", "cells_long", "cells_short"},
    "cell_sizes": {"cells_long", "cells_short", "depth"},
    "pieces": {"pieces", "depth"},
    "product": {"count", "item", "item_height", "cell_clearance", "max_box", "perimeter"},
}
MODES = tuple(MODE_KEYS)


class _Ctx:
    """Reads lengths in the inputs' units (numbers, fractions or expressions) and returns inches."""

    def __init__(self, units: str, names: dict[str, float]) -> None:
        if units not in ("in", "mm"):
            raise Refused("units must be 'in' or 'mm'")
        self.units = units
        self.k = 1.0 if units == "in" else 1 / MM
        self.names = names  # in inches

    def length(self, value: Any, what: str, *, zero: bool = False, negative: bool = False) -> float:
        from .designs import parse_length

        if value is None:
            raise Refused(f"{what} is required")
        if self.units == "mm" and isinstance(value, str) and ":" in value:
            raise Refused(f"{what}: ArtiosCAD sixteenths (2:12) are only for inch values")
        try:
            v = parse_length(value) if not isinstance(value, str) else _try_plain(value)
            if v is None:
                env = Env(self.units, {k: Value(x / self.k, 1) for k, x in self.names.items()})  # inputs' units
                v = evaluate(value, env, "length")
        except (ExpressionError, ValueError) as exc:
            raise Refused(f"{what}: {exc}") from None
        if not math.isfinite(v) or (v < 0 and not negative) or (v == 0 and not zero and not negative):
            raise Refused(f"{what} must be {'at least 0' if zero else 'greater than 0'} (got {value!r})")
        inches = v * self.k
        if abs(inches) > 240:
            raise Refused(f"{what} is over 240 in; check the units")
        return inches


def _try_plain(text: str) -> float | None:
    from .designs import parse_length

    try:
        return parse_length(text)
    except ValueError:
        return None


def _count(value: Any, what: str, lo: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < lo or value > MAX_CELLS:
        raise Refused(f"{what} must be a whole number from {lo} to {MAX_CELLS}")
    return value


def _choice(args: dict[str, Any], key: str, choices: tuple[str, ...], default: str) -> str:
    v = args.get(key, default)
    if v not in choices:
        raise Refused(f"{key} must be one of {', '.join(choices)}")
    return v


def _boards(args: dict[str, Any], n_long: int, n_short: int) -> tuple[list[str], list[str]]:
    base = args.get("board", "32 B KR")
    out = []
    for axis, n in (("long", n_long), ("short", n_short)):
        listed = args.get(f"boards_{axis}")
        one = args.get(f"board_{axis}", base)
        for v in (base, one):
            if not isinstance(v, str):
                raise Refused("boards are ArtiosCAD board codes like '32 B KR'")
        if listed is not None:
            if not isinstance(listed, list) or len(listed) != n or not all(isinstance(b, str) for b in listed):
                raise Refused(f"boards_{axis} must list one board per {axis} piece ({n})")
            codes = listed
        else:
            codes = [one] * n
        try:
            out.append([get_board(c).id for c in codes])
        except ValueError as exc:
            raise Refused(str(exc)) from None
    return out[0], out[1]


def _board_names(args: dict[str, Any]) -> dict[str, float]:
    try:
        b = get_board(args.get("board", "32 B KR") if isinstance(args.get("board", "32 B KR"), str) else "")
    except ValueError as exc:
        raise Refused(str(exc)) from None
    return {"CAL": b.cal, "IL": b.il, "OG": b.og, "CRRV": b.crrv}


def _rules(args: dict[str, Any], ctx: _Ctx) -> dict[str, Any]:
    out: dict[str, Any] = {
        "slot_dir": _choice(args, "slot_dir", ("long_top", "short_top"), "long_top"),
        "mouth": _choice(args, "mouth", ("lead_in", "square"), "lead_in"),
        "flute": _choice(args, "flute", ("vertical", "horizontal"), "vertical"),
        "units": ctx.units,
    }
    both = args.get("clearance")
    for axis in ("long", "short"):
        v = args.get(f"clearance_{axis}", both)
        out[f"clearance_{axis}"] = CLEARANCE if v is None else ctx.length(v, f"clearance_{axis}", zero=True)
    if args.get("lead_in") is not None:
        out["lead_in"] = ctx.length(args["lead_in"], "lead_in", zero=True)
    if args.get("overcut") is not None:
        out["overcut"] = ctx.length(args["overcut"], "overcut", negative=True)
    if args.get("slot_width") is not None:
        out["slot_width"] = ctx.length(args["slot_width"], "slot_width")
    cd = args.get("confirm_degenerate", False)
    if not isinstance(cd, bool):
        raise Refused("confirm_degenerate must be true or false")
    out["degenerate_confirmed"] = cd
    return out


def _check_keys(mode: str, args: dict[str, Any]) -> None:
    allowed = (_COMMON - ({"height", "boards_long", "boards_short"} if mode == "pieces" else set())) | MODE_KEYS[mode]
    unknown = sorted(set(args) - allowed)
    if unknown:
        raise Refused(f"input_mode {mode} takes no {', '.join(unknown)}; it takes {', '.join(sorted(allowed))}")


def spec_from_inputs(mode: str, args: dict[str, Any], container: Box | None = None) -> PartitionSpec:
    """box_cells, cell_sizes or pieces inputs as the one grid. (product mode
    returns candidates instead: see `candidates`.)"""
    if mode not in MODES or mode == "product":
        raise Refused(f"input_mode must be one of {', '.join(MODES)}" if mode not in MODES
                      else "product mode returns layout candidates; it doesn't make a set directly")
    _check_keys(mode, args)
    units = args.get("units", "in")
    names = _board_names(args)
    if container is not None:
        names.update(L=container.length, W=container.width, D=container.depth)
    ctx0 = _Ctx(units, names)
    if mode == "box_cells":
        dims = {}
        for key, name, fallback in (("length", "L", container.length if container else None),
                                    ("width", "W", container.width if container else None),
                                    ("depth", "D", container.depth if container else None)):
            v = args.get(key)
            dims[name] = ctx0.length(v, key) if v is not None else fallback
            if dims[name] is None:
                raise Refused(f"box_cells needs the box inside {key} (or contained_in a box)")
        ctx = _Ctx(units, {**names, **dims})
        nl, ns = _count(args.get("cells_long"), "cells_long"), _count(args.get("cells_short"), "cells_short")
        boards_long, boards_short = _boards(args, ns - 1, nl - 1)
        t_long = sum(_cal(b) for b in boards_short)
        t_short = sum(_cal(b) for b in boards_long)
        cl = [(dims["L"] - t_long) / nl] * nl
        cs = [(dims["W"] - t_short) / ns] * ns
        height = ctx.length(args["height"], "height") if args.get("height") is not None else dims["D"]
        depth = dims["D"]
        hl = hs = height
    elif mode == "cell_sizes":
        dims = {"D": container.depth} if container else {}
        if args.get("depth") is not None:
            dims["D"] = ctx0.length(args["depth"], "depth")
        ctx = _Ctx(units, {**names, **dims})
        cl, cs = [], []
        for key, out in (("cells_long", cl), ("cells_short", cs)):
            raw = args.get(key)
            if not isinstance(raw, list) or not raw or len(raw) > MAX_CELLS:
                raise Refused(f"{key} must list the installed clear cell sizes along that axis (1 to {MAX_CELLS})")
            out += [ctx.length(v, f"{key}[{i + 1}]") for i, v in enumerate(raw)]
        boards_long, boards_short = _boards(args, len(cs) - 1, len(cl) - 1)
        if args.get("height") is not None:
            height = ctx.length(args["height"], "height")
        elif "D" in dims:
            height = dims["D"]
        else:
            raise Refused("cell_sizes needs the piece height (or the box depth, or contained_in a box)")
        depth = dims.get("D")
        hl = hs = height
    else:  # pieces
        ctx = _Ctx(units, {**names, **({"D": container.depth} if container else {})})
        cl, cs, hl, hs, boards_long, boards_short, depth = _from_pieces(args, ctx, _rules(args, ctx))
        if args.get("depth") is not None:
            depth = ctx.length(args["depth"], "depth")
        elif container is not None:
            depth = container.depth
    rules = _rules(args, ctx)
    spec = PartitionSpec(long_cells=cl, short_cells=cs, height_long=hl, height_short=hs, depth=depth,
                         boards_long=boards_long, boards_short=boards_short, input_mode=mode,
                         inputs=_jsonable(args), **rules)
    plan_set(spec)  # refuses here, before anything becomes a design
    return spec


def _from_pieces(args: dict[str, Any], ctx: _Ctx, rules: dict[str, Any]):
    """Piece types (length, height, slot positions from a named end) as a grid.
    Long pieces' slots are where the short pieces cross, and so on; positions
    that don't make one consistent grid are refused, naming the mismatch."""
    raw = args.get("pieces")
    if not isinstance(raw, list) or not raw or len(raw) > 2 * MAX_CELLS:
        raise Refused("pieces must list the piece types: {axis: long|short, length, height, slots: [positions], "
                      "from: start|end, board?, quantity?}")
    types: dict[str, list[dict[str, Any]]] = {"long": [], "short": []}
    for n, p in enumerate(raw):
        if not isinstance(p, dict):
            raise Refused(f"piece type {n + 1} must be an object")
        extra = sorted(set(p) - {"axis", "length", "height", "slots", "from", "board", "quantity"})
        if extra:
            raise Refused(f"piece type {n + 1} has no {', '.join(extra)}")
        axis = p.get("axis")
        if axis not in ("long", "short"):
            raise Refused(f"piece type {n + 1}: axis must be long or short")
        length = ctx.length(p.get("length"), f"piece type {n + 1} length")
        height = ctx.length(p.get("height"), f"piece type {n + 1} height")
        end = p.get("from", "start")
        if end not in ("start", "end"):
            raise Refused(f"piece type {n + 1}: from must be start or end (the piece end the slot positions are "
                          "measured from)")
        slots = p.get("slots", [])
        if not isinstance(slots, list) or len(slots) > MAX_CELLS:
            raise Refused(f"piece type {n + 1}: slots must list the slot centre positions")
        pos = [ctx.length(v, f"piece type {n + 1} slot {i + 1}") for i, v in enumerate(slots)]
        if end == "end":
            pos = [length - x for x in pos]
        pos.sort()
        q = p.get("quantity")
        if q is not None and (isinstance(q, bool) or not isinstance(q, int) or q < 1):
            raise Refused(f"piece type {n + 1}: quantity must be a whole number of at least 1")
        board = p.get("board", args.get(f"board_{axis}", args.get("board", "32 B KR")))
        try:
            board = get_board(board).id
        except (ValueError, AttributeError) as exc:
            raise Refused(f"piece type {n + 1}: {exc}") from None
        types[axis].append({"n": n + 1, "length": length, "height": height, "slots": pos, "board": board,
                            "quantity": q})
    if not types["long"] or not types["short"]:
        only = "long" if types["long"] else "short"
        raise Refused(f"only {only} piece types are listed. An interlocking set needs both; plain strips (1 x n, no "
                      "slots) have no slot positions to place them by, so use cell_sizes with one cell across them "
                      "and confirm_degenerate: true")
    for axis, ts in types.items():
        for t in ts[1:]:
            ref = ts[0]
            if abs(t["length"] - ref["length"]) > 1 / 64 or abs(t["height"] - ref["height"]) > 1 / 64:
                raise Refused(f"{axis} piece types {ref['n']} and {t['n']} differ in length or height; every piece "
                              "on one axis spans the same box")
            if len(t["slots"]) != len(ref["slots"]) or any(abs(a - b) > 1 / 64 for a, b in zip(t["slots"],
                                                                                              ref["slots"])):
                raise Refused(f"{axis} piece types {ref['n']} and {t['n']} have different slot positions; all "
                              f"{axis} pieces cross the same {('short' if axis == 'long' else 'long')} pieces")
    def count(axis: str) -> int:
        return sum(t["quantity"] or 0 for t in types[axis])

    other = {"long": "short", "short": "long"}
    for axis in ("long", "short"):
        need = len(types[other[axis]][0]["slots"])
        qs = [t["quantity"] for t in types[axis]]
        if None in qs:
            if len(qs) > 1:
                raise Refused(f"give a quantity for each {axis} piece type")
            types[axis][0]["quantity"] = need
        elif count(axis) != need:
            raise Refused(f"the {other[axis]} pieces have {need} slots, so the set needs {need} {axis} pieces; "
                          f"the {axis} piece types add up to {count(axis)}")
    lens, heights, cells, boards = {}, {}, {}, {}
    for axis in ("long", "short"):
        ts = types[axis]
        clearance = rules[f"clearance_{axis}"]
        ref = ts[0]
        lens[axis], heights[axis] = ref["length"], ref["height"]
        boards[axis] = [t["board"] for t in ts for _ in range(t["quantity"] or 1)]
    for axis in ("long", "short"):
        ref = types[axis][0]
        clearance = rules[f"clearance_{axis}"]
        box = ref["length"] + clearance
        walls = [_cal(b) for b in boards[other[axis]]]
        xs = [x + clearance / 2 for x in ref["slots"]]
        out, prev = [], 0.0
        for j, x in enumerate(xs):
            lo = x - walls[j] / 2
            out.append(lo - prev)
            prev = x + walls[j] / 2
        out.append(box - prev)
        for j, c in enumerate(out):
            if c <= EPS:
                where = "the piece's start" if j == 0 else "the piece's end" if j == len(out) - 1 else f"slots {j} and {j + 1}"
                raise Refused(f"the {axis} piece's slots don't leave a cell between {where} (cell {j + 1} is "
                              f"{fmt_in(c)} in); check the slot positions")
        cells[axis] = out
    return cells["long"], cells["short"], heights["long"], heights["short"], boards["long"], boards["short"], None


def _jsonable(v: Any) -> Any:
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    return str(v)


# ------------------------------------------------------------------ product mode


def candidates(args: dict[str, Any], container: Box | None = None, limit: int = 6) -> list[dict[str, Any]]:
    """Layouts for a count of items: each with its cells, the set's outside size,
    the box inside it needs and an estimated sheet usage, plus the spec it would
    make. Nothing is drawn."""
    _check_keys("product", args)
    units = args.get("units", "in")
    ctx = _Ctx(units, _board_names(args))
    count = args.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1 or count > 400:
        raise Refused("count must be a whole number of items, 1 to 400")
    if count == 1:
        raise Refused("one item needs no partition (1 x 1 has no dividers)")
    item = args.get("item")
    if not isinstance(item, dict) or not (set(item) == {"diameter"} or set(item) == {"length", "width"}):
        raise Refused("item must be {diameter} or {length, width}")
    if "diameter" in item:
        d = ctx.length(item["diameter"], "item diameter")
        sizes = [(d, d)]
    else:
        il, iw = ctx.length(item["length"], "item length"), ctx.length(item["width"], "item width")
        sizes = [(il, iw), (iw, il)] if abs(il - iw) > EPS else [(il, iw)]
    item_h = ctx.length(args.get("item_height"), "item_height")
    cc = ctx.length(args.get("cell_clearance", 1 / 16 / ctx.k), "cell_clearance", zero=True)
    perimeter = args.get("perimeter", "none")
    ring = liner = 0.0
    if perimeter != "none":
        if not isinstance(perimeter, dict) or len(perimeter) != 1 or not ({"air_cells"} >= set(perimeter)
                                                                         or {"liner"} >= set(perimeter)):
            raise Refused("perimeter must be 'none', {air_cells: width} or {liner: thickness}")
        if "air_cells" in perimeter:
            ring = ctx.length(perimeter["air_cells"], "air cell width")
        else:
            liner = ctx.length(perimeter["liner"], "liner thickness")
    max_box = args.get("max_box")
    limit_box: tuple[float | None, float | None, float | None] = (None, None, None)
    if max_box is not None:
        if not isinstance(max_box, dict) or not set(max_box) <= {"length", "width", "depth"}:
            raise Refused("max_box must be {length, width, depth?}")
        limit_box = tuple(ctx.length(max_box[k], f"max_box {k}") if max_box.get(k) is not None else None
                          for k in ("length", "width", "depth"))  # type: ignore[assignment]
    elif container is not None:
        limit_box = (container.length, container.width, container.depth)
    height = ctx.length(args["height"], "height") if args.get("height") is not None else item_h
    rules = _rules(args, ctx)
    out: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    for a in range(1, count + 1):
        for b in range(a, count + 1):
            n = a * b
            if n < count or n - count >= a or (a == 1 and b == 1):
                continue
            for nl, ns in {(b, a), (a, b)}:
                for along, across in sizes:
                    cl = [along + 2 * cc] * nl
                    cs = [across + 2 * cc] * ns
                    if ring:
                        cl, cs = [ring, *cl, ring], [ring, *cs, ring]
                    if len(cl) == 1 and len(cs) == 1:
                        continue
                    bl, bs = _boards(args, len(cs) - 1, len(cl) - 1)
                    spec_args = dict(long_cells=cl, short_cells=cs, height_long=height, height_short=height,
                                     depth=limit_box[2], boards_long=bl, boards_short=bs, input_mode="product",
                                     inputs=_jsonable(args), **{**rules, "degenerate_confirmed": True})
                    spec = PartitionSpec(**spec_args)
                    try:
                        plan = plan_set(spec)
                    except Refused:
                        continue
                    box_l, box_s = plan.box_long + 2 * liner, plan.box_short + 2 * liner
                    if box_l < box_s - EPS:
                        continue  # the long axis is the longer one
                    L, W, D = limit_box
                    if (L is not None and box_l > L + EPS) or (W is not None and box_s > W + EPS) or \
                            (D is not None and height > D + EPS):
                        continue
                    key = (tuple(round(x, 5) for x in cl), tuple(round(x, 5) for x in cs))
                    if key in seen:
                        continue
                    seen.add(key)
                    area = sum(p.quantity * p.length * p.height for p in plan.pieces) / 144
                    lo, so, ho = plan.outside
                    out.append({
                        "cells": f"{nl} x {ns}", "cells_long": nl, "cells_short": ns, "spare_cells": n - count,
                        "degenerate": nl == 1 or ns == 1,
                        "item_along_long_in": round(along, 6),
                        "cell_in": {"long": round(along + 2 * cc, 6), "short": round(across + 2 * cc, 6)},
                        "set_outside_in": {"long": round(lo, 6), "short": round(so, 6), "height": round(ho, 6)},
                        "box_inside_in": {"long": round(box_l, 6), "short": round(box_s, 6)},
                        "pieces": [{"role": p.role, "quantity": p.quantity, "length_in": round(p.length, 6),
                                    "height_in": round(p.height, 6), "board": p.board} for p in plan.pieces],
                        "sheet_usage_sqft_estimate": round(area, 3),
                        "spec": spec.model_dump(),
                    })
    out.sort(key=lambda c: (c["degenerate"], c["spare_cells"], c["sheet_usage_sqft_estimate"],
                            abs(c["box_inside_in"]["long"] - c["box_inside_in"]["short"])))
    return out[:limit]


# ------------------------------------------------------------------ checks


def _board_check(board: Board) -> CheckResult:
    if board.source == "artioscad":
        return CheckResult("board", "pass", f"{board.id} values from ArtiosCAD (CAL {fmt_in(board.cal)})")
    word = "inferred from real R&L designs" if board.source == "derived" else "nominal"
    return CheckResult("board", "warn", f"{board.id} CAL {fmt_in(board.cal)} is {word}; confirm in ArtiosCAD's "
                                        "board library (slot widths come from it)")


def piece_checks(plan: SetPlan, piece: Piece) -> list[CheckResult]:
    """The checks one piece carries (the crossing and set checks are the group's)."""
    spec = plan.spec
    out: list[CheckResult] = []
    loose = [k + 1 for k, s in enumerate(piece.slots) if s.width > s.mating_cal + LOOSE + EPS]
    if loose:
        out.append(CheckResult("partition_fit", "warn", f"slot(s) {', '.join(map(str, loose))} are more than 1/16 in "
                                                        "wider than the mating board: a loose fit"))
    elif piece.slots:
        ws = sorted({fmt_in(s.width) for s in piece.slots})
        out.append(CheckResult("partition_fit", "pass", f"slot width {' / '.join(ws)} in for the mating board"))
    else:
        out.append(CheckResult("partition_fit", "not_applicable", "this piece has no slots"))
    if piece.slots:
        web = min(piece.height - s.depth for s in piece.slots)
        if web < WEB_MIN - EPS:
            out.append(CheckResult("partition_web", "warn", f"only {fmt_in(web)} in of board left beside the deepest "
                                                            "slot (under 1/2 in); it may tear"))
        elif web < WEB_FLUTE - EPS and spec.flute == "vertical":
            out.append(CheckResult("partition_web", "warn", f"{fmt_in(web)} in left beside the deepest slot on a "
                                                            "vertical-flute piece (under 1 in): it may snap along a "
                                                            "flute"))
        else:
            out.append(CheckResult("partition_web", "pass", f"residual web {fmt_in(web)} in"))
        rim = max(RIM_MIN, 2 * piece.cal)
        notes, bad = [], []
        mouths = [(s.centre - s.width / 2 - s.lead_in, s.centre + s.width / 2 + s.lead_in) for s in piece.slots]
        for k in range(1, len(mouths)):
            gap = mouths[k][0] - mouths[k - 1][1]
            if gap < rim - EPS:
                bad.append(f"slots {k} and {k + 1} leave a {fmt_in(gap)} in rim")
        if mouths[0][0] < rim - EPS or piece.length - mouths[-1][1] < rim - EPS:
            bad.append(f"a mouth is within {fmt_in(rim)} in of a piece end")
        reduced = [k + 1 for k, s in enumerate(piece.slots) if s.lead_in_reduced]
        if reduced:
            notes.append(f"lead-ins reduced to fit on slot(s) {', '.join(map(str, reduced))}")
        if bad:
            out.append(CheckResult("partition_mouths", "warn", "; ".join(bad) + f" (under max(1/4 in, 2 x CAL) = "
                                                                 f"{fmt_in(rim)} in)" + (f"; {notes[0]}" if notes else "")))
        else:
            kind = "square mouths" if spec.mouth == "square" else f"{fmt_in(spec.lead_in)} x 45 deg lead-ins"
            out.append(CheckResult("partition_mouths", "pass", f"{kind}, rims at least {fmt_in(rim)} in" +
                                   (f"; {notes[0]}" if notes else "")))
        if not piece.symmetric():
            out.append(CheckResult("partition_orientation", "info", "the slot plan isn't the same from both ends: "
                                                                    "packers can put this piece in backwards. An "
                                                                    "orientation notch is suggested (not drawn)"))
    if spec.depth is None:
        out.append(CheckResult("partition_height", "unavailable", "the box's inside depth isn't known, so the "
                                                                  "height wasn't checked against it"))
    elif piece.height > spec.depth + EPS:
        out.append(CheckResult("partition_height", "warn", f"{fmt_in(piece.height)} in tall in a "
                                                           f"{fmt_in(spec.depth)} in deep box: the lid won't close"))
    else:
        out.append(CheckResult("partition_height", "pass", f"{fmt_in(piece.height)} in tall, inside the "
                                                           f"{fmt_in(spec.depth)} in depth"))
    out.append(_board_check(get_board(piece.board)))
    out.append(CheckResult("calibration", "warn", "partition allowances are industry defaults (slot width, depth, "
                                                  "clearance, lead-ins), not calibrated against a real R&L set"))
    return out


def set_checks(gid: str, plan: SetPlan, present: dict[str, int]) -> list[CheckResult]:
    """The set's own checks: the crossings, reciprocity against the parts that
    are really in the group, small cells. present: role -> quantity in the group."""
    spec = plan.spec
    out: list[CheckResult] = []
    expected = {p.role: p.quantity for p in plan.pieces}
    missing = [r for r in expected if r not in present]
    extra = [r for r in present if r not in expected]
    wrong = [r for r in expected if r in present and present[r] != expected[r]]
    if missing or extra or wrong:
        bits = ([f"missing its {', '.join(missing)} pieces"] if missing else []) + \
               ([f"has pieces its grid doesn't ({', '.join(extra)})"] if extra else []) + \
               ([f"{r} quantity {present[r]}, its grid needs {expected[r]}" for r in wrong])
        out.append(CheckResult(f"partition_{gid}_reciprocity", "fail",
                               f"set {gid} {'; '.join(bits)}: its crossings don't match. Replace the set "
                               "(create_partition with action replace_set) or remove it"))
    elif plan.degenerate:
        out.append(CheckResult(f"partition_{gid}_reciprocity", "not_applicable", f"set {gid} is 1 x n: no crossings"))
    else:
        longs = [p for p in plan.pieces if p.axis == "long"]
        shorts = [p for p in plan.pieces if p.axis == "short"]
        ok = all(len(p.slots) == sum(q.quantity for q in shorts) for p in longs) and \
            all(len(p.slots) == sum(q.quantity for q in longs) for p in shorts)
        # Each slot, back in the box frame, lands on the centre of the wall it takes.
        for p in plan.pieces:
            pos = plan.positions_long if p.axis == "long" else plan.positions_short
            c = spec.clearance_long if p.axis == "long" else spec.clearance_short
            ok = ok and all(abs(s.centre + c / 2 - pos[s.crossing]) <= 1e-6 for s in p.slots)
        if not ok:
            raise Refused(f"set {gid}'s long and short pieces don't agree on their crossings")
        n = sum(q.quantity for q in longs) * sum(q.quantity for q in shorts)
        out.append(CheckResult(f"partition_{gid}_reciprocity", "pass", f"set {gid}: {n} crossings, the same on the "
                                                                       "long and short pieces"))
    if not plan.degenerate:
        if plan.play > PLAY + EPS:
            out.append(CheckResult(f"partition_{gid}_interlock", "warn",
                                   f"set {gid}: {fmt_in(plan.play)} in of vertical play at each crossing (over "
                                   "1/8 in); the pieces will ride up and down"))
        else:
            out.append(CheckResult(f"partition_{gid}_interlock", "pass",
                                   f"set {gid}: slots overlap with {fmt_in(plan.play)} in play, both pieces on the "
                                   "floor (usable depths, round bottoms counted)"))
    cal = max([*plan.t_long, *plan.t_short], default=0.0)
    small = [f"{fmt_in(c)}" for c in [*spec.long_cells, *spec.short_cells] if c < SMALL_CELL_CALS * cal - EPS]
    if small:
        out.append(CheckResult(f"partition_{gid}_cells", "warn", f"set {gid}: cells of {', '.join(sorted(set(small)))} "
                                                                 f"in are under 3 x CAL ({fmt_in(3 * cal)} in)"))
    else:
        out.append(CheckResult(f"partition_{gid}_cells", "pass", f"set {gid}: every cell at least 3 x CAL"))
    if plan.degenerate:
        out.append(CheckResult(f"partition_{gid}_layout", "info", f"set {gid} is 1 x n: plain dividers with no "
                                                                  "slots, held only by the box"))
    if spec.chosen:
        how = (f"the designer's message {spec.chosen.get('message_id')}" if spec.chosen.get("via") == "chat"
               else "the designer's click on its card")
        out.append(CheckResult(f"partition_{gid}_layout_choice", "info",
                               f"set {gid}: layout {spec.chosen.get('candidate')} chosen by {how}"))
    return out


# ------------------------------------------------------------------ summary


def summary(plan: SetPlan, piece: Piece) -> dict[str, Any]:
    spec = plan.spec
    board = get_board(piece.board)
    r = lambda v: round(v, 6)  # noqa: E731
    lo, so, ho = plan.outside
    return {
        "style": SLOTTED,
        "units": "in",
        "board": board.id,
        "board_verified": board.verified,
        "board_source": board.source,
        "cal_in": board.cal,
        "il_in": board.il,
        "og_in": board.og,
        "corrugated": board.corrugated,
        "calibrated": False,
        "rules": spec.rules,
        "role": piece.role,
        "axis": piece.axis,
        "quantity_per_set": piece.quantity,
        "piece_in": {"length": r(piece.length), "height": r(piece.height)},
        "end_legs_in": [r(piece.end_legs[0]), r(piece.end_legs[1])],
        "slots_in": [{"centre": r(s.centre), "width": r(s.width), "depth": r(s.depth), "usable_depth": r(s.usable),
                      "from": s.edge, "bottom": s.bottom, "lead_in": r(s.lead_in), "mating_board": s.mating_board}
                     for s in piece.slots],
        "flute": spec.flute,
        "flute_axis": "y" if spec.flute == "vertical" else "x",  # in the flat piece: vertical runs with its height
        "set": {
            "input_mode": spec.input_mode,
            "cells": f"{len(spec.long_cells)} x {len(spec.short_cells)}",
            "cells_long_in": [r(c) for c in spec.long_cells],
            "cells_short_in": [r(c) for c in spec.short_cells],
            "box_inside_in": {"long": r(plan.box_long), "short": r(plan.box_short)},
            "set_outside_in": {"long": r(lo), "short": r(so), "height": r(ho)},
            "depth_in": r(spec.depth) if spec.depth is not None else None,
            "pieces": [{"role": p.role, "axis": p.axis, "quantity": p.quantity, "board": p.board,
                        "length_in": r(p.length), "height_in": r(p.height)} for p in plan.pieces],
            "play_in": r(plan.play),
            "slot_dir": spec.slot_dir,
        },
    }


def read_back(plan: SetPlan) -> str:
    """The set in plain words, for the agent to restate (system prompt read-back)."""
    s = plan.spec
    lo, so, ho = plan.outside
    cells = lambda cs: " / ".join(fmt_in(c) for c in cs) if len({round(c, 5) for c in cs}) > 1 else (  # noqa: E731
        f"{len(cs)} x {fmt_in(cs[0])}")
    pieces = "; ".join(f"{p.quantity} x {p.role} {fmt_in(p.length)} x {fmt_in(p.height)} in {p.board}"
                       for p in plan.pieces)
    return (f"{len(s.long_cells)} x {len(s.short_cells)} cells; installed cells long {cells(s.long_cells)} in, short "
            f"{cells(s.short_cells)} in; needs a box {fmt_in(plan.box_long)} x {fmt_in(plan.box_short)} in inside "
            f"(set {fmt_in(lo)} x {fmt_in(so)} x {fmt_in(ho)} in); pieces: {pieces}; flute {s.flute}; "
            f"{'long pieces slotted from the top' if s.slot_dir == 'long_top' else 'short pieces slotted from the top'}"
            "; not calibrated")


# ------------------------------------------------------------------ building a piece


def build(params: PartitionParams):
    """One piece of the set, drawn, with its face map and summary."""
    from .geometry import LineType  # noqa: PLC0415
    from .styles import partitions as drawing  # noqa: PLC0415

    plan = plan_set(params.set)
    piece = plan.piece(params.role)
    if get_board(params.board).id != piece.board:
        raise Refused(f"part board {params.board} doesn't match the set's {piece.role} pieces ({piece.board})")
    name = f"Partition {piece.role} {piece.length:g}x{piece.height:g} {piece.board}"
    d = drawing.draw(name, piece)
    d.faces = drawing.face_map(piece, d)
    w, h = d.size()
    d.meta = {
        **summary(plan, piece),
        "pointage": 2.0,
        "blank_in": {"width": round(w, 6), "height": round(h, 6)},
        "blank_area_sqft": round(w * h / 144, 3),
        "rule_length_in": {"cut": round(sum(x.length for x in d.of_type(LineType.CUT)), 4), "crease": 0.0},
    }
    return d


def checks_for(params: PartitionParams) -> list[CheckResult]:
    plan = plan_set(params.set)
    return piece_checks(plan, plan.piece(params.role))

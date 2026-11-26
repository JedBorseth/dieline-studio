"""Drawing a slotted partition piece, and its face map.

A piece is a rectangle, `length` along x from its start end (the end nearest
the box's first wall) and `height` up y from the floor it stands on. Slots are
cut from the top or bottom edge. Each slot is part of the piece's outline
operation: like the standards' flap slots and an edge notch, a slot is a
boundary operation that changes the outline, so the outline stays one closed
cut and the slot's mouth joins it by construction (docs/DESIGN-SYSTEM.md,
"Notches", and topology.py: "a notch belongs to the outline it changes").

A slot has straight walls, a 45 degree lead-in at the mouth (when there's
room) and a square bottom, or a full-radius round one when it's at least
twice the minimum bend radius wide. Only cut is used, which is verified for
CF2 and the sample-table DXF.
"""

from __future__ import annotations

from ..faces import Face, FaceMap, KeepOut, RefLine, attach_edges
from ..geometry import Dieline, LineType, Operation
from ..partitions import Piece, Slot


def draw(name: str, piece: Piece) -> Dieline:
    L, H = piece.length, piece.height
    d = Dieline(name=name, units="in")
    op = d.add_op(Operation("outline", "outline", closed=True))
    path: list[tuple] = []  # ("L", point) or ("A", end, centre, clockwise)

    def to(p: tuple[float, float]) -> None:
        path.append(("L", p))

    # Counter-clockwise: the bottom edge left to right, up the end, the top edge right to left.
    to((0.0, 0.0))
    for s in sorted((s for s in piece.slots if s.edge == "bottom"), key=lambda s: s.centre):
        _bottom_slot(path, s)
    to((L, 0.0))
    to((L, H))
    for s in sorted((s for s in piece.slots if s.edge == "top"), key=lambda s: -s.centre):
        _top_slot(path, s, H)
    to((0.0, H))
    # Emit, dropping the zero-length steps a square mouth leaves.
    cur = path[0][1]
    for step in path[1:] + [("L", path[0][1])]:
        if step[0] == "L":
            if abs(step[1][0] - cur[0]) > 1e-9 or abs(step[1][1] - cur[1]) > 1e-9:
                d.add_line(cur, step[1], LineType.CUT, op)
            cur = step[1]
        else:
            _, end, centre, cw = step
            d.add_arc(cur, end, centre, cw, LineType.CUT, op=op)
            cur = end
    return d


def _bottom_slot(path: list[tuple], s: Slot) -> None:
    x0, x1, m, depth = s.centre - s.width / 2, s.centre + s.width / 2, s.lead_in, s.depth
    path.append(("L", (x0 - m, 0.0)))
    path.append(("L", (x0, m)))
    if s.bottom == "round":
        r = s.width / 2
        path.append(("L", (x0, depth - r)))
        path.append(("A", (x1, depth - r), (s.centre, depth - r), True))  # over the top of the slot
    else:
        path.append(("L", (x0, depth)))
        path.append(("L", (x1, depth)))
    path.append(("L", (x1, m)))
    path.append(("L", (x1 + m, 0.0)))


def _top_slot(path: list[tuple], s: Slot, H: float) -> None:
    x0, x1, m, depth = s.centre - s.width / 2, s.centre + s.width / 2, s.lead_in, s.depth
    path.append(("L", (x1 + m, H)))
    path.append(("L", (x1, H - m)))
    if s.bottom == "round":
        r = s.width / 2
        path.append(("L", (x1, H - depth + r)))
        path.append(("A", (x0, H - depth + r), (s.centre, H - depth + r), True))  # under the bottom of the slot
    else:
        path.append(("L", (x1, H - depth)))
        path.append(("L", (x0, H - depth)))
    path.append(("L", (x0, H - m)))
    path.append(("L", (x0 - m, H)))


class PieceFaceMap(FaceMap):
    """A partition piece's one face, `piece`."""

    def agent_view(self) -> dict:
        from ..checks import fmt_in  # noqa: PLC0415

        f = self.faces["piece"]
        return {
            "units": "in",
            "viewing": "A partition piece has one face, 'piece', seen as drawn (either side: a partition has no "
                       "inside or outside). x runs from the piece's start end (nearest the box's first wall) to "
                       "its other end, y up from the floor it stands on.",
            "faces": [{
                "id": "piece", "role": "piece", "size_in": f"{fmt_in(f.width)} x {fmt_in(f.height)}",
                "orientation": f.up,
                "edges": {side: f.lines[side].kind for side in ("top", "bottom", "left", "right")},
                "lines": sorted(n for n in f.lines if n not in ("top", "bottom", "left", "right")),
                "keep_outs": [f"{k.kind}: x {fmt_in(k.rect[0])} to {fmt_in(k.rect[2])}, y {fmt_in(k.rect[1])} to "
                              f"{fmt_in(k.rect[3])}" for k in f.keepouts],
            }],
            "selectors": dict(self.selectors),
            "unavailable_selectors": dict(self.unavailable),
        }


def face_map(piece: Piece, d: Dieline) -> PieceFaceMap:
    """One face with the geometry contract's frame: flat = face (no mirror: a partition
    is the same from both sides), origin at the start end on the floor."""
    L, H = piece.length, piece.height
    f = Face("piece", "piece", "piece", L, H, (0.0, 0.0), (1.0, 0.0), (0.0, 1.0))
    sides = {
        "bottom": ((0.0, 0.0), (L, 0.0), (0.0, 1.0), "bottom_edge"),
        "top": ((0.0, H), (L, H), (0.0, -1.0), "top_edge"),
        "left": ((0.0, 0.0), (0.0, H), (1.0, 0.0), "start_end"),
        "right": ((L, 0.0), (L, H), (-1.0, 0.0), "end_end"),
    }
    for side, (a, b, n, phys) in sides.items():
        f.lines[side] = RefLine(f"piece.{side}", side, "free_edge", a, b, n)
        f.lines[phys] = RefLine(f"piece.{phys}", phys, "free_edge", a, b, n)
    f.lines["centre_x"] = RefLine("piece.centre_x", "centre_x", "centre", (L / 2, 0.0), (L / 2, H), None)
    f.lines["centre_y"] = RefLine("piece.centre_y", "centre_y", "centre", (0.0, H / 2), (L, H / 2), None)
    f.centre_x = f.material_centre_x = L / 2
    slotted = {s.edge for s in piece.slots}
    f.up = ("partition piece standing in the box; +y is up from the floor; its left side is the start end, its "
            "right side the other end" + (f"; slotted from the {' and '.join(sorted(slotted))}" if slotted else ""))
    for k, s in enumerate(piece.slots):
        x0, x1 = s.centre - s.width / 2 - s.lead_in, s.centre + s.width / 2 + s.lead_in
        y0, y1 = (0.0, s.depth) if s.edge == "bottom" else (H - s.depth, H)
        f.keepouts.append(KeepOut("slot", (x0, y0, x1, y1), "both",
                                  f"slot {k + 1}: the crossing piece passes through here"))
    f.polygon = f.corners_flat()
    fm = PieceFaceMap({"piece": f}, {}, drawing_side="either")
    fm.viewing = "as drawn"
    fm.selectors = {"pieces": ["piece"]}
    for sel in ("body_panels", "end_panels", "side_panels", "top_flaps", "bottom_flaps"):
        fm.unavailable[sel] = "a partition piece has no panels or flaps; its one face is 'piece'"
    attach_edges(fm, d)
    return fm

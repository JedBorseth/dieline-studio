"""Regular slotted container (RSC, FEFCO 0201).

Blank layout, left to right: glue flap | length | width | length | width.
Bottom flaps below the body, top flaps above it. All values are millimetres.

ALLOWANCES ARE PLACEHOLDERS. The rules in `allowances()` are common rules of
thumb, not our ArtiosCAD Standard's. Calibrate them against ArtiosCAD's RSC
(docs/PLAN.md milestone 4) before any output is used for production.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from ..boards import Board
from ..geometry import Dieline, LineType


@dataclass(frozen=True, slots=True)
class RscAllowances:
    length_panel: float  # added to inside length -> score-to-score length panel
    width_panel: float  # added to inside width -> score-to-score width panel
    depth: float  # added to inside depth -> score-to-score body height
    flap: float  # added to inside width before halving -> flap depth
    slot_width: float
    glue_flap: float
    glue_taper: float  # how far the glue flap's ends are cut back at the free edge


def allowances(board: Board) -> RscAllowances:
    t = board.caliper_mm
    return RscAllowances(
        length_panel=round(t, 1),
        width_panel=round(t, 1),
        depth=round(2 * t, 1),
        flap=round(t, 1),
        slot_width=round(max(6.0, 1.5 * t), 1),
        glue_flap=35.0 if t >= 3.0 else 30.0,
        glue_taper=10.0,
    )


def build(length: float, width: float, depth: float, board: Board) -> Dieline:
    a = allowances(board)
    panels = [
        length + a.length_panel,
        width + a.width_panel,
        length + a.length_panel,
        width + a.width_panel,
    ]
    gf = a.glue_flap
    flap = (width + a.flap) / 2
    body = depth + a.depth
    s = a.slot_width
    tg = a.glue_taper

    xs = [gf]
    for p in panels:
        xs.append(xs[-1] + p)
    y1, y2, y3 = flap, flap + body, 2 * flap + body
    last = len(panels) - 1

    def flap_span(i: int) -> tuple[float, float]:
        # Flaps stop half a slot short of each panel score; the free end of the
        # last panel has no slot.
        left = xs[i] + s / 2
        right = xs[i + 1] - s / 2 if i < last else xs[i + 1]
        return left, right

    d = Dieline(name=f"RSC {length:g}x{width:g}x{depth:g} {board.id}")

    # Outline: one closed cut contour, counter-clockwise from the glue score.
    pts: list[tuple[float, float]] = [(gf, y1)]
    for i in range(len(panels)):
        left, right = flap_span(i)
        pts += [(left, y1), (left, 0.0), (right, 0.0)]
        if i < last:
            pts.append((right, y1))
    for i in reversed(range(len(panels))):
        left, right = flap_span(i)
        if i < last:
            pts.append((right, y2))
        pts += [(right, y3), (left, y3), (left, y2)]
    pts += [(gf, y2), (0.0, y2 - tg), (0.0, y1 + tg)]
    d.add_polyline(pts, LineType.CUT, closed=True)

    # Panel creases (body only) including the glue flap fold.
    for x in xs[:-1]:
        d.add_line((x, y1), (x, y2), LineType.CREASE)
    # Flap hinge creases.
    for i in range(len(panels)):
        left, right = flap_span(i)
        d.add_line((left, y1), (right, y1), LineType.CREASE)
        d.add_line((left, y2), (right, y2), LineType.CREASE)

    blank_w, blank_h = xs[-1], y3
    d.meta = {
        "style": "fefco-0201",
        "inside_dims_mm": {"length": length, "width": width, "depth": depth},
        "board": board.id,
        "caliper_mm": board.caliper_mm,
        "allowances_mm": asdict(a),
        "calibrated": False,
        "panels_mm": {
            "glue_flap": gf,
            "length_panel": panels[0],
            "width_panel": panels[1],
            "body_height": body,
            "flap_depth": flap,
        },
        "blank_mm": {"width": round(blank_w, 2), "height": round(blank_h, 2)},
    }
    return d

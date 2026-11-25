"""Glued slotted boxes, replicating R&L's ArtiosCAD ServerLib standards.

Every formula below is copied from the standard's variable menus (ArtiosCAD
CASE(v1, cond1, v2, cond2, ..., default) rewritten as Python conditionals), and
the outline follows the standard's construction script. All values are inches.

- REG-GLIO-NX  "Reg - Glued/Stitched - In/Out - Non Ext."  (RSC, FOL, CSSC, gap/lap)
- REG_GLI_EXT  the same box with an extended, square-ended glue lug
- HSC_GL       half slotted container, glued
- Tube_Glued   four-panel sleeve, glued

Layout, as in the shop's sample-table DXFs: the glue lug is on the LEFT, then panels 4, 3, 2, 1, with
panel 1's free edge at the right. Bottom flaps, body, top flaps from bottom to
top. Slots are 3/8" wide centred on each panel
score; the glue lug's free-end corners are cut back 1/4", the taper starting
3/16" past the glue score.

These are the standards' formulas, not yet confirmed by an ArtiosCAD rebuild
(docs/PLAN.md milestone 4). A few variables carry an active 1/16 rounding in
the standard's binary menu record (mode 2 or 3 after the formula; mode 1 is
off): REG-GLIO-NX CD; REG_GLI_EXT CD and TF1/TF2/BF1/BF2; HSC_GL DP and FL.
Which way modes 2 and 3 round isn't known, so they are computed exactly here,
listed in `rounded_in`, and a check warns when one falls between sixteenths.

Known difference: REG-GLIO-NX's script draws panel 4's lower glue-side flap
wall from the TOP flap depth (TF2), which only matters when top and bottom
flap styles differ; this engine draws the bottom flap from BF2.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..boards import Board
from ..geometry import Dieline, LineType

SLOT = 3 / 8  # slot width, centred on the score (con ... offset2 +/-0.1875)
HALF_SLOT = SLOT / 2
GLUE_TAPER = 1 / 4  # movepoint ... by 0, +/-0.25 at the glue lug's free end

PANEL_ORDERS = {"eses": "E-S-E-S-GL", "sese": "S-E-S-E-GL"}
CLOSURES = {"inside": "Inside", "outside": "Outside"}
FLAP_STYLES = {
    "regular": "Regular (outer flaps meet)",
    "full_overlap": "Full overlap",
    "gap_lap": "Gap/Lap",
    "cssc": "CSSC (all flaps meet)",
}
DEFAULT_LAP = 0.5  # "Top Gap/Lap" default CASE(0.5, TF=3, 0)
EXT_LUG = 1.25  # REG_GLI_EXT: the glue lug runs 1 1/4" past each body score
SIXTEENTH = 1 / 16


def glue_lug(depth: float, cal: float, depth_inclusive: bool = False) -> float:
    """Glue Lug = CASE(1.5, (D>12 OR CAL>0.1875), 1.25). Tube_Glued uses D>=12.

    `CAL > 0.1875` is evaluated as `>=` on purpose: every C-flute (CAL 3/16)
    REG-GLIO-NX design this year with D <= 12 has the 1 1/2" lug in ArtiosCAD
    across the reference samples, presumably because the board caliper is stored in mm and converts to a hair over 3/16.
    """
    deep = depth >= 12 if depth_inclusive else depth > 12
    return 1.5 if deep or cal > 0.1875 - 1e-6 else 1.25


def raw_panels(length: float, width: float, order: str) -> list[float]:
    """P1A..P4A = CASE(L, PA=2, W) ... : E-S-E-S starts with the width (end) panel."""
    if order == "sese":
        return [length, width, length, width]
    return [width, length, width, length]


def joint_panels(raw: list[float], b: Board, closure: str) -> list[float]:
    """Score-to-score panels.

    P1 = CASE(P1A+((CAL-IL)-0.125), CL=2, (P1A+CAL)-0.125)
    P2 = P2A+CAL, P3 = P3A+CAL
    P4 = CASE(P4A+CAL+IL, CL=2, P4A+CAL)
    """
    p1a, p2a, p3a, p4a = raw
    if closure == "outside":
        return [p1a + b.cal - b.il - 0.125, p2a + b.cal, p3a + b.cal, p4a + b.cal + b.il]
    return [p1a + b.cal - 0.125, p2a + b.cal, p3a + b.cal, p4a + b.cal]


def flap_depths(style: str, width: float, raw: list[float], b: Board, lap: float) -> tuple[float, float]:
    """(flaps on panels 1 and 3, flaps on panels 2 and 4).

    TF1 = CASE(W/2+CAL-0.125, TF=1, W, TF=2, (W/2+CAL-0.125)+TLP/2, TF=3, P2A/2+CAL-0.125, TF=4, 0)
    TF2 = the same with P1A/2 for TF=4.
    """
    regular = width / 2 + b.cal - 0.125
    if style == "regular":
        return regular, regular
    if style == "full_overlap":
        return width, width
    if style == "gap_lap":
        return regular + lap / 2, regular + lap / 2
    if style == "cssc":
        return raw[1] / 2 + b.cal - 0.125, raw[0] / 2 + b.cal - 0.125
    raise ValueError(f"unknown flap style {style!r}")


def half_depth(style: str, first_flap: float, depth: float, width: float, b: Board) -> float:
    """TD (or BD) = CASE((D+4*CAL-2*IL+0.125)/2, TF=2, (D+4*CAL-2*IL+0.125)/2,
    (TF=3 AND TF1>(2/3)*W), (D+5*CAL-4*IL-0.0625)/2)"""
    if style == "full_overlap" or (style == "gap_lap" and first_flap > (2 / 3) * width):
        return (depth + 4 * b.cal - 2 * b.il + 0.125) / 2
    return (depth + 5 * b.cal - 4 * b.il - 0.0625) / 2


@dataclass(frozen=True, slots=True)
class Blank:
    panels: list[float]
    glue: float
    body: float  # score-to-score body height
    bottom: tuple[float, float] | None  # flap depths (panels 1/3, panels 2/4), or no flaps
    top: tuple[float, float] | None
    # 0: lug spans the body with 1/4" tapered corners. > 0: square-ended lug that
    # runs this far past each body score, creased across at the scores (REG_GLI_EXT).
    lug_extension: float = 0.0


def draw(name: str, blank: Blank) -> Dieline:
    """Outline, panel creases and flap hinge creases for a glued slotted blank."""
    p = blank.panels
    _validate(blank)
    xs = [0.0]
    for w in p:
        xs.append(xs[-1] + w)
    c1, c2, c3, c4 = xs[1:]
    x_end = c4 + blank.glue
    flap = lambda row, i: row[i % 2]  # noqa: E731  panels 1/3 use row[0], 2/4 row[1]
    e = blank.lug_extension
    y1 = max(max(blank.bottom) if blank.bottom else 0.0, e)
    y2 = y1 + blank.body
    t = GLUE_TAPER
    h = HALF_SLOT

    # Flap span of panel i: free edge on the left of panel 1, half a slot short of
    # every other score.
    def span(i: int) -> tuple[float, float]:
        return (xs[i] + h if i else 0.0, xs[i + 1] - h)

    pts: list[tuple[float, float]] = []
    if blank.bottom:
        for i in range(4):
            lo, hi = span(i)
            tip = y1 - flap(blank.bottom, i)
            pts += [(lo, y1), (lo, tip), (hi, tip), (hi, y1)]
        # Close directly from the top of panel 1 to its bottom flap tip.
        pts.pop(0)
    else:
        pts.append((0.0, y1))
    if e:
        pts += [(c4 + h, y1), (c4 + h, y1 - e), (x_end, y1 - e), (x_end, y2 + e), (c4 + h, y2 + e), (c4 + h, y2)]
    else:
        pts += [(c4 + h, y1), (x_end, y1 + t), (x_end, y2 - t), (c4 + h, y2)]
    if blank.top:
        for i in reversed(range(4)):
            lo, hi = span(i)
            tip = y2 + flap(blank.top, i)
            pts += [(hi, y2), (hi, tip), (lo, tip), (lo, y2)]
        pts.pop()
    else:
        pts.append((0.0, y2))
    # Built with panel 1 at x = 0, then mirrored so the glue lug sits on the left.
    m = lambda pt: (x_end - pt[0], pt[1])  # noqa: E731
    d = Dieline(name=name, units="in")
    d.add_polyline([m(pt) for pt in reversed(pts)], LineType.CUT, closed=True)

    for x in (c4, c3, c2, c1):
        d.add_line(m((x, y1)), m((x, y2)), LineType.CREASE)
    if e:  # the extended lug folds at the body scores
        for y in (y1, y2):
            d.add_line(m((x_end, y)), m((c4 + h, y)), LineType.CREASE)
    for row, y in ((blank.bottom, y1), (blank.top, y2)):
        if row:
            for i in reversed(range(4)):
                lo, hi = span(i)
                d.add_line(m((hi, y)), m((lo, y)), LineType.CREASE)
    return d


def _validate(blank: Blank) -> None:
    p = blank.panels
    if min(p) <= 0 or blank.body <= 0:
        raise ValueError("panel sizes must be positive; check the dimensions")
    if blank.bottom or blank.top:
        if p[0] <= HALF_SLOT or min(p[1:]) <= SLOT:
            raise ValueError(f"a panel is narrower than the {SLOT:g}\" slot; the box is too small for this style")
    if not blank.lug_extension and blank.body <= 2 * GLUE_TAPER:
        raise ValueError("the body is too short for the glue lug's 1/4\" tapers")
    for row in (blank.bottom, blank.top):
        if row and min(row) <= 0:
            raise ValueError("flap depths must be positive")


def blank_meta(blank: Blank, d: Dieline) -> dict[str, object]:
    w, h = d.size()
    return {
        "lug_extension_in": blank.lug_extension,
        "panels_in": [round(x, 6) for x in blank.panels],
        "glue_lug_in": blank.glue,
        "body_height_in": round(blank.body, 6),
        "bottom_flaps_in": [round(x, 6) for x in blank.bottom] if blank.bottom else None,
        "top_flaps_in": [round(x, 6) for x in blank.top] if blank.top else None,
        "slot_in": SLOT,
        "blank_in": {"width": round(w, 6), "height": round(h, 6)},
    }


# ----------------------------------------------------------------- the styles


def reg_glio_nx(
    length: float,
    width: float,
    depth: float,
    b: Board,
    order: str = "eses",
    closure: str = "inside",
    top_flaps: str = "regular",
    bottom_flaps: str = "regular",
    top_lap: float = DEFAULT_LAP,
    bottom_lap: float = DEFAULT_LAP,
) -> tuple[Blank, dict[str, object]]:
    raw = raw_panels(length, width, order)
    panels = joint_panels(raw, b, closure)
    # TLP = CASE(0.5, TF=3, 0): the lap only applies to Gap/Lap flaps.
    top = flap_depths(top_flaps, width, raw, b, top_lap if top_flaps == "gap_lap" else 0.0)
    bottom = flap_depths(bottom_flaps, width, raw, b, bottom_lap if bottom_flaps == "gap_lap" else 0.0)
    td = half_depth(top_flaps, top[0], depth, width, b)
    bd = half_depth(bottom_flaps, bottom[0], depth, width, b)
    blank = Blank(panels, glue_lug(depth, b.cal), td + bd, bottom, top)
    # Only CD rounds (mode 2); TD and BD have rounding off (mode 1).
    return blank, {"top_depth_in": round(td, 6), "bottom_depth_in": round(bd, 6),
                   "rounded_in": {"CD": td + bd}}


def reg_gli_ext(
    length: float,
    width: float,
    depth: float,
    b: Board,
    order: str = "eses",
    top_flaps: str = "regular",
    bottom_flaps: str = "regular",
    top_lap: float = DEFAULT_LAP,
    bottom_lap: float = DEFAULT_LAP,
) -> tuple[Blank, dict[str, object]]:
    """REG_GLI_EXT: P1 = (P1A+CAL)-0.125, P2..P4 = PnA+CAL (no joint option),
    TD1/BD1/flaps as REG-GLIO-NX, glue lug extended 1 1/4" past both scores."""
    p1a, p2a, p3a, p4a = raw = raw_panels(length, width, order)
    panels = [p1a + b.cal - 0.125, p2a + b.cal, p3a + b.cal, p4a + b.cal]
    # TLP = CASE(0, TF<>3, 0.5): the lap only applies to Gap/Lap flaps.
    top = flap_depths(top_flaps, width, raw, b, top_lap if top_flaps == "gap_lap" else 0.0)
    bottom = flap_depths(bottom_flaps, width, raw, b, bottom_lap if bottom_flaps == "gap_lap" else 0.0)
    td = half_depth(top_flaps, top[0], depth, width, b)
    bd = half_depth(bottom_flaps, bottom[0], depth, width, b)
    blank = Blank(panels, glue_lug(depth, b.cal), td + bd, bottom, top, lug_extension=EXT_LUG)
    # CD rounds (mode 2) and the four flap variables (mode 3); TD1 and BD1 don't.
    rounded = {"CD": td + bd, "TF1": top[0], "TF2": top[1], "BF1": bottom[0], "BF2": bottom[1]}
    return blank, {"top_depth_in": round(td, 6), "bottom_depth_in": round(bd, 6), "rounded_in": rounded}


def hsc_gl(
    length: float,
    width: float,
    depth: float,
    b: Board,
    order: str = "eses",
    closure: str = "inside",
) -> tuple[Blank, dict[str, object]]:
    raw = raw_panels(length, width, order)
    panels = joint_panels(raw, b, closure)
    body = depth + (5 * b.cal - 4 * b.il - 0.0625) / 2  # Depth Panel
    flaps = width / 2 + b.cal - 0.125  # Flaps
    # DP (mode 2) and FL (mode 3) round to 1/16 in the standard.
    return Blank(panels, glue_lug(depth, b.cal), body, None, (flaps, flaps)), {"rounded_in": {"DP": body, "FL": flaps}}


def tube_glued(
    length: float,
    width: float,
    depth: float,
    b: Board,
    order: str = "eses",
) -> tuple[Blank, dict[str, object]]:
    p1a, p2a, p3a, p4a = raw_panels(length, width, order)
    panels = [p1a + b.cal - 0.125, p2a + b.cal, p3a + b.cal, p4a + b.cal]
    return Blank(panels, glue_lug(depth, b.cal, depth_inclusive=True), depth, None, None), {}

"""Box style registry. Each style turns (inside dims, board, options) into a Dieline.

Style ids follow the R&L ArtiosCAD ServerLib standard each one replicates, so
designers see the names they already use.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

from ..boards import Board
from ..faces import attach_edges, slotted_faces
from ..geometry import Dieline, LineType
from . import slotted


@dataclass(frozen=True, slots=True)
class Dims:
    """Inside dimensions in inches."""

    length: float
    width: float
    depth: float


@dataclass(frozen=True, slots=True)
class StyleOption:
    id: str
    label: str
    description: str
    default: Any
    choices: dict[str, str] | None = None  # value -> label; None means a number (inches)
    min: float | None = None
    max: float | None = None

    def coerce(self, value: Any) -> Any:
        if self.choices is not None:
            if not isinstance(value, str) or value not in self.choices:
                raise ValueError(f"{self.id} must be one of {', '.join(self.choices)}")
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{self.id} must be a number (inches)")
        v = float(value)
        if not math.isfinite(v):
            raise ValueError(f"{self.id} must be finite")
        if (self.min is not None and v < self.min) or (self.max is not None and v > self.max):
            raise ValueError(f"{self.id} must be between {self.min:g} and {self.max:g} inches")
        return v

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "label": self.label, "description": self.description,
                               "default": self.default}
        if self.choices is not None:
            out["choices"] = [{"value": k, "label": v} for k, v in self.choices.items()]
        else:
            out["min"], out["max"] = self.min, self.max
        return out


@dataclass(frozen=True, slots=True)
class Style:
    id: str
    name: str
    standard: str  # the ServerLib standard it replicates
    description: str
    build: Callable[..., tuple[slotted.Blank, dict[str, object]]]
    options: list[StyleOption] = field(default_factory=list)
    # True once checked against an ArtiosCAD rebuild of the standard.
    calibrated: bool = False

    def resolve_options(self, given: dict[str, Any]) -> dict[str, Any]:
        known = {o.id: o for o in self.options}
        unknown = set(given) - set(known)
        if unknown:
            raise ValueError(f"style {self.id} has no option(s) {', '.join(sorted(unknown))}; "
                             f"options: {', '.join(known) or 'none'}")
        return {o.id: o.coerce(given[o.id]) if o.id in given else o.default for o in self.options}

    def make(self, dims: Dims, board: Board, options: dict[str, Any]) -> Dieline:
        opts = self.resolve_options(options)
        build_opts = {k: v for k, v in opts.items() if k != "use"}  # use changes the faces, not the drawing
        blank, extra = self.build(dims.length, dims.width, dims.depth, board, **build_opts)
        name = f"{self.standard} {_f(dims.length)}x{_f(dims.width)}x{_f(dims.depth)} {board.id}"
        d = slotted.draw(name, blank)
        d.faces = slotted_faces(
            blank, slotted.raw_panels(dims.length, dims.width, opts.get("order", "eses")), board,
            order=opts.get("order", "eses"), closure=opts.get("closure", "inside"),
            top_style=opts.get("top_flaps", "regular" if blank.top else None),
            bottom_style=opts.get("bottom_flaps", "regular" if blank.bottom else None),
            use=opts.get("use"), half_slot=slotted.HALF_SLOT, glue_taper=slotted.GLUE_TAPER,
            lap=(opts.get("top_lap", 0.0) if opts.get("top_flaps") == "gap_lap" else 0.0,
                 opts.get("bottom_lap", 0.0) if opts.get("bottom_flaps") == "gap_lap" else 0.0))
        attach_edges(d.faces, d)
        d.meta = {
            "style": self.id,
            "standard": self.standard,
            "units": "in",
            "inside_in": {"length": dims.length, "width": dims.width, "depth": dims.depth},
            "board": board.id,
            "board_verified": board.verified,
            "board_source": board.source,
            "cal_in": board.cal,
            "il_in": board.il,
            "og_in": board.og,
            "corrugated": board.corrugated,
            "options": opts,
            "calibrated": self.calibrated,
            **slotted.blank_meta(blank, d),
            **extra,
            "pointage": 2.0,  # the standards draw every rule `poi 2`
            "rule_length_in": {
                "cut": round(sum(x.length for x in d.of_type(LineType.CUT)), 4),
                "crease": round(sum(x.length for x in d.of_type(LineType.CREASE)), 4),
            },
            "blank_area_sqft": round(d.size()[0] * d.size()[1] / 144, 3),
        }
        return d

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "standard": self.standard, "description": self.description,
                "calibrated": self.calibrated, "options": [o.to_dict() for o in self.options]}


def _f(x: float) -> str:
    return f"{x:g}"


ORDER = StyleOption(
    "order", "Panel arrangement",
    "Which panel comes first at the free edge: E-S-E-S (end/width panel first) or S-E-S-E (side/length panel first). "
    "The glue lug is always on the last panel.",
    "eses", slotted.PANEL_ORDERS,
)
CLOSURE = StyleOption(
    "closure", "Joint",
    "Glued/stitched joint inside or outside. Outside takes IL off panel 1 and adds IL to panel 4.",
    "inside", slotted.CLOSURES,
)


LAPS = [
    StyleOption(f"{end}_lap", f"{end.title()} gap/lap",
                f"{end.title()} Gap/Lap (the standard's {var}): how far the outer {end} flaps overlap, in inches; "
                "negative leaves a gap. Only used with Gap/Lap flaps.",
                slotted.DEFAULT_LAP, None, -24.0, 24.0)
    for end, var in (("top", "TLP"), ("bottom", "BLP"))
]


def _flaps(id: str, label: str) -> StyleOption:
    return StyleOption(
        id, label,
        "Regular: outer flaps meet at the centre (RSC). Full overlap: every flap is the full width (FOL). "
        "Gap/Lap: regular plus half the lap (lap > 0 overlaps, < 0 leaves a gap). CSSC: all flaps meet "
        "(end-panel flaps are half the length).",
        "regular", slotted.FLAP_STYLES,
    )


STYLES: dict[str, Style] = {
    s.id: s
    for s in [
        Style(
            "reg-glio-nx",
            "Regular slotted container, glued (REG-GLIO-NX)",
            "REG-GLIO-NX",
            "R&L's main RSC standard: four panels, glue lug on panel 4, top and bottom flaps. Covers RSC "
            "(FEFCO 0201), full overlap (0203), CSSC (0204-style) and gap/lap flaps, joint inside or outside.",
            slotted.reg_glio_nx,
            [ORDER, CLOSURE, _flaps("top_flaps", "Top flaps"), _flaps("bottom_flaps", "Bottom flaps"),
             *LAPS],
        ),
        Style(
            "reg-gli-ext",
            "Regular slotted container, extended glue lug (REG_GLI_EXT)",
            "REG_GLI_EXT",
            "R&L's RSC with an extended glue lug: the lug runs 1 1/4\" past each body score with square ends "
            "and is creased across at the scores. Inside joint only. Same flap styles as REG-GLIO-NX.",
            slotted.reg_gli_ext,
            [ORDER, _flaps("top_flaps", "Top flaps"), _flaps("bottom_flaps", "Bottom flaps"),
             *LAPS],
        ),
        Style(
            "hsc-gl",
            "Half slotted container, glued (HSC_GL)",
            "HSC_GL",
            "R&L's HSC standard: an RSC with one flap row and one open edge. Used as a tray (the flaps form the "
            "bottom; the default) or a lid (the flaps form the top); the use option says which. FEFCO 0200.",
            slotted.hsc_gl,
            [ORDER, CLOSURE, StyleOption(
                "use", "Use", "Tray: the flap row is the bottom of the folded box (the default). Lid: the flap row "
                "is its top. Changes which way is up for placements, never the drawing.",
                "tray", {"tray": "Tray (flaps down)", "lid": "Lid (flaps up)"})],
        ),
        Style(
            "tube-glued",
            "Glued tube / sleeve (Tube_Glued)",
            "Tube_Glued",
            "R&L's tube standard: four panels and a glue lug, no flaps; open at both ends. Depth is the "
            "sleeve height.",
            slotted.tube_glued,
            [ORDER],
        ),
    ]
}


def get_style(style_id: str) -> Style:
    try:
        return STYLES[style_id]
    except KeyError:
        known = ", ".join(STYLES)
        raise ValueError(f"unknown style {style_id!r}; known styles: {known}") from None

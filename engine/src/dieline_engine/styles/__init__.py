"""Box style registry. Each style turns (dims, board) into a Dieline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..boards import Board
from ..geometry import Dieline
from . import rsc


@dataclass(frozen=True, slots=True)
class Dims:
    """Inside dimensions in millimetres."""

    length: float
    width: float
    depth: float


@dataclass(frozen=True, slots=True)
class Style:
    id: str
    name: str
    description: str
    build: Callable[[Dims, Board], Dieline]
    # Allowances still use placeholder rules (not calibrated against ArtiosCAD).
    calibrated: bool


STYLES: dict[str, Style] = {
    s.id: s
    for s in [
        Style(
            "fefco-0201",
            "Regular slotted container (RSC, FEFCO 0201)",
            "Corrugated shipping box. Four body panels, glue flap, top and bottom flaps "
            "of equal depth whose outer flaps meet at the centre.",
            lambda d, b: rsc.build(d.length, d.width, d.depth, b),
            calibrated=False,
        ),
    ]
}


def get_style(style_id: str) -> Style:
    try:
        return STYLES[style_id]
    except KeyError:
        known = ", ".join(STYLES)
        raise ValueError(f"unknown style {style_id!r}; known styles: {known}") from None

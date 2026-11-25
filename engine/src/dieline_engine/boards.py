"""Board grades as R&L names them in ArtiosCAD: `<ECT> <flute> <liner>`.

Each board carries the four values ArtiosCAD's board library stores and the
shop's standards use in their formulas, all in inches:

- CAL: caliper (thickness)
- IL: inside loss (how much a fold eats from the inside dimension)
- OG: outside gain
- CRRV: corrugated rounding value

`source` says where the numbers come from:

- "artioscad": read from ArtiosCAD (a real design's board record).
- "derived": CAL and IL solved from real R&L designs built from the standards
  (their blank sizes or exported creases reproduce exactly with these values).
- "nominal": a per-flute guess.

Anything but "artioscad" stays flagged until someone copies the real numbers
from the board library (DataCenter Admin, or one Agent > Capture design state
XML per board).
"""

from __future__ import annotations

from dataclasses import dataclass

MM_PER_INCH = 25.4


@dataclass(frozen=True, slots=True)
class Board:
    id: str  # the ArtiosCAD board code, e.g. "32 B KR"
    flute: str  # "E", "B", "C", "EB", "BC"
    cal: float  # inches
    il: float
    og: float
    crrv: float
    source: str = "nominal"  # "artioscad" | "derived" | "nominal"
    evidence: str = ""
    corrugated: bool = True

    @property
    def verified(self) -> bool:
        return self.source == "artioscad"

    @property
    def name(self) -> str:
        ect, _, liner = self.id.partition(f" {self.flute} ")
        liner_name = {"KR": "kraft"}.get(liner, liner)
        wall = "double wall" if len(self.flute) == 2 else "single wall"
        return f"{ect} ECT {self.flute}-flute {liner_name} ({wall})"

    @property
    def caliper_mm(self) -> float:
        return round(self.cal * MM_PER_INCH, 2)


# Values per flute (inches): (CAL, IL, source, evidence). OG and CRRV, which the
# current styles don't use, default to IL.
FLUTE_VALUES: dict[str, tuple[float, float, str, str]] = {
    "B": (1 / 8, 1 / 16, "nominal", "same as the verified 32 B KR record"),
    "C": (3 / 16, 1 / 8, "derived",
          "reproduces 9 REG-GLIO-NX 32 C KR designs"),
    "BC": (5 / 16, 3 / 16, "derived",
           "reproduces 48 BC KR and 71 BC KR REG-GLIO-NX/REG_GLI_EXT blanks"),
    "E": (1 / 16, 1 / 32, "nominal", "guess"),
    "EB": (3 / 16, 1 / 8, "nominal", "guess"),
}


# Codes whose own designs back the flute values; other codes of the same flute
# borrow them and stay "nominal".
_EVIDENCED = {"32 C KR", "48 BC KR", "71 BC KR"}


def _from_flute(code: str, flute: str) -> Board:
    cal, il, source, evidence = FLUTE_VALUES[flute]
    if source == "derived" and code not in _EVIDENCED:
        source, evidence = "nominal", f"assumed equal to other {flute}-flute boards ({evidence})"
    return Board(code, flute, cal, il, il, il, source, evidence)


# Read from ArtiosCAD (FD26-0093 board record, 2026-10-29).
_VERIFIED = [
    Board("32 B KR", "B", 0.125, 0.0625, 0.0625, 0.0625, "artioscad", "FD26-0093 board record"),
]

# Board codes used in this year's R&L designs, most used first.
_NOMINAL_CODES = [
    ("32 C KR", "C"),
    ("48 BC KR", "BC"),
    ("51 BC KR", "BC"),
    ("44 C KR", "C"),
    ("23 B KR", "B"),
    ("61 BC KR", "BC"),
    ("40 C KR", "C"),
    ("71 BC KR", "BC"),
    ("32 E KR", "E"),
    ("42 BC KR", "BC"),
    ("42 EB KR", "EB"),
    ("44 B KR", "B"),
    ("29 E KR", "E"),
    ("23 E KR", "E"),
    ("23 C KR", "C"),
    ("36 C KR", "C"),
    ("40 B KR", "B"),
    ("55 C KR", "C"),
    ("29 B KR", "B"),
]

BOARDS: dict[str, Board] = {
    b.id: b for b in [*_VERIFIED, *(_from_flute(code, flute) for code, flute in _NOMINAL_CODES)]
}

DEFAULT_BOARD = "32 B KR"  # the shop's ArtiosCAD default board


def get_board(board_id: str) -> Board:
    key = " ".join(board_id.upper().split())
    if key in BOARDS:
        return BOARDS[key]
    known = ", ".join(BOARDS)
    raise ValueError(f"unknown board {board_id!r}; known boards: {known}")

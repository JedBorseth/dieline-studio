"""Board grades: caliper and flute.

NOMINAL VALUES. Calipers below are typical industry figures, not our
supplier's. Replace them with measured calipers before calibration
(docs/PLAN.md milestone 4).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Board:
    id: str
    name: str
    flute: str  # "E", "B", "C", "BC", ... or "SBS" for folding carton board
    caliper_mm: float
    corrugated: bool = True


BOARDS: dict[str, Board] = {
    b.id: b
    for b in [
        Board("E-32ECT", "E-flute 32 ECT", "E", 1.6),
        Board("B-32ECT", "B-flute 32 ECT", "B", 3.2),
        Board("C-32ECT", "C-flute 32 ECT", "C", 4.0),
        Board("C-44ECT", "C-flute 44 ECT", "C", 4.2),
        Board("BC-48ECT", "BC double wall 48 ECT", "BC", 6.8),
        Board("SBS-18PT", "SBS 18 pt folding carton", "SBS", 0.46, corrugated=False),
    ]
}


def get_board(board_id: str) -> Board:
    try:
        return BOARDS[board_id]
    except KeyError:
        known = ", ".join(BOARDS)
        raise ValueError(f"unknown board {board_id!r}; known boards: {known}") from None

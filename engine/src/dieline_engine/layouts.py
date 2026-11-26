"""Product-mode layout candidates (docs/DESIGN-SYSTEM.md, "Choosing a layout").

`create_partition` in product mode draws nothing: it offers candidates. Each
one is tied to what it was made from:

- its inputs,
- the design it was offered against (id and revision; none when there was no
  design), which covers the container part's revision,
- the board values and the partition rule version.

If any of those changes, the candidate has expired and can't be chosen. The
designer normally chooses by clicking its card in the chat; the model may
choose only after the designer names the candidate in a message, and the
choice records that message's id.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Any

from .boards import get_board
from .partitions import RULES_VERSION, PartitionSpec

MAX_PER_SESSION = 60


@dataclass(frozen=True, slots=True)
class Candidate:
    id: str
    session: str
    spec: PartitionSpec
    binding: tuple[str, int] | None  # the design (id, revision) it was offered against
    boards: tuple[tuple[str, float, float, float], ...]  # (code, CAL, IL, OG) when offered
    rules: str
    contained_in: str | None
    label: str | None
    degenerate: bool
    summary: dict[str, Any] = field(default_factory=dict)
    turn: int = 0  # the chat turn it was offered in: only a later message can name it


def _boards(spec: PartitionSpec) -> tuple[tuple[str, float, float, float], ...]:
    out = []
    for code in sorted(set(spec.boards_long) | set(spec.boards_short)):
        b = get_board(code)
        out.append((b.id, b.cal, b.il, b.og))
    return tuple(out)


class Registry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_id: dict[tuple[str, str], Candidate] = {}
        self._order: list[tuple[str, str]] = []
        self._next: dict[str, int] = {}

    def offer(self, session: str, found: list[dict[str, Any]], binding: tuple[str, int] | None,
              contained_in: str | None = None, label: str | None = None, turn: int = 0) -> list[dict[str, Any]]:
        """Register candidates; returns them with their ids (c1, c2, ... never reused in a session)."""
        out = []
        with self._lock:
            for c in found:
                n = self._next.get(session, 0) + 1
                self._next[session] = n
                cid = f"c{n}"
                spec = PartitionSpec.model_validate(c["spec"])
                summary = {k: v for k, v in c.items() if k != "spec"}
                cand = Candidate(cid, session, spec, binding, _boards(spec), spec.rules, contained_in, label,
                                 bool(c.get("degenerate")), summary, turn)
                self._by_id[(session, cid)] = cand
                self._order.append((session, cid))
                out.append({"id": cid, **summary})
            mine = [k for k in self._order if k[0] == session]
            for key in mine[:-MAX_PER_SESSION]:
                self._by_id.pop(key, None)
                self._order.remove(key)
        return out

    def get(self, session: str, cid: str, binding: tuple[str, int] | None) -> Candidate:
        """The candidate, if it's still valid for the design as it is now."""
        with self._lock:
            cand = self._by_id.get((session, cid))
        if cand is None:
            raise ValueError(f"no layout candidate {cid!r} in this chat; run create_partition in product mode again")
        if cand.binding != binding:
            raise ValueError(f"layout candidate {cid} has expired: the design changed since it was offered. Run "
                             "create_partition in product mode again for fresh candidates")
        if cand.rules != RULES_VERSION:
            raise ValueError(f"layout candidate {cid} has expired: the partition rules changed since it was offered")
        if _boards(cand.spec) != cand.boards:
            raise ValueError(f"layout candidate {cid} has expired: its board values changed since it was offered")
        return cand

    def expire_session(self, session: str) -> None:
        with self._lock:
            for key in [k for k in self._order if k[0] == session]:
                self._by_id.pop(key, None)
                self._order.remove(key)


def names_candidate(text: str, cid: str) -> bool:
    """Whether a message names the candidate by its id, as a whole word ("c3", "layout c3", "C3.")."""
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(cid)}(?![A-Za-z0-9_])", text, re.IGNORECASE) is not None

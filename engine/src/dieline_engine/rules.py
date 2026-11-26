"""Rule specs and each export target's capability table
(docs/DESIGN-SYSTEM.md, "Rule types and export capability").

Every operation can carry a `RuleSpec`: what kind of steel rule makes it and
with which parameters. Structure drawn by the standards has none, which means
the plain rule of its line type at the part's pointage (2 pt).

A target exports only **verified** combinations: proved by importing or cutting
a reference file on the receiving equipment. Anything else blocks that target,
with the reason; the design stays drawn and other targets can still export.

Verified today: cut and crease at 2 pt in CF2 (codes 1 and 2, the shop's CF2
import tuning) and cut and crease in the sample-table DXF (colours 1 and 2).
Perforation, reverse crease, cut-score / partial cut and mixed pointage are
unverified in both: their CF2 codes and the table's colours aren't known yet.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .geometry import Arc, Dieline, Line, LineType, Segment

VERIFIED, UNVERIFIED, UNSUPPORTED = "verified", "unverified", "unsupported"
TARGETS = {"cf2": "ArtiosCAD import (CF2)", "dxf": "the sample table (DXF)"}

# File_Box_1's dedicated perforation rule, "3/16 3/16 perf" (a shop file): 3/16 cut, 3/16 tie.
PERF_CUT = 3 / 16
PERF_TIE = 3 / 16
DEFAULT_POINTAGE = 2.0


@dataclass(frozen=True, slots=True)
class RuleSpec:
    """How an operation is made.

    kind: cut | crease | perforation | cut_score | annotation.
    side: creases and cut-scores, normal (pressed from the inside face, as the
    standards' creases are) or reverse. Perforations: cut and tie lengths in
    inches, the phase (how far into the first cut the path starts) and the
    crossing policy at creases: tie_at_creases (default: a tie is centred on
    every crease crossed, so the crease stays continuous) or continuous (the
    pattern runs on; a cut landing on a crease is flagged)."""

    kind: str
    pointage: float = DEFAULT_POINTAGE
    side: str = "normal"
    cut: float | None = None
    tie: float | None = None
    phase: float = 0.0
    crossing: str = "tie_at_creases"
    depth: float | None = None  # cut-score: fraction of the caliper, when known

    def key(self) -> tuple[Any, ...]:
        return (self.kind, self.pointage, self.side, self.cut, self.tie)

    def describe(self) -> str:
        if self.kind == "perforation":
            from .checks import fmt_in  # noqa: PLC0415

            return f"perforation {fmt_in(self.cut or 0)} cut / {fmt_in(self.tie or 0)} tie"
        extra = " (reverse)" if self.side == "reverse" else ""
        return f"{self.kind.replace('_', '-')}{extra}"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "pointage": self.pointage}
        if self.kind in ("crease", "cut_score"):
            out["side"] = self.side
        if self.kind == "perforation":
            out.update(cut_in=self.cut, tie_in=self.tie, phase_in=self.phase, crossing=self.crossing)
        return out


LINE_KIND = {LineType.CUT: "cut", LineType.CREASE: "crease", LineType.PERFORATION: "perforation",
             LineType.SCORE: "cut_score", LineType.ANNOTATION: "annotation"}


def rule_of(d: Dieline, s: Segment) -> RuleSpec:
    op = d.ops.get(s.op) if s.op else None
    rule = getattr(op, "rule", None) if op is not None else None
    if isinstance(rule, RuleSpec):
        return rule
    pointage = float(d.meta.get("pointage", DEFAULT_POINTAGE))  # type: ignore[arg-type]
    kind = LINE_KIND[s.type]
    if kind == "perforation":
        return RuleSpec(kind, pointage, cut=PERF_CUT, tie=PERF_TIE)
    return RuleSpec(kind, pointage)


def rules_used(d: Dieline) -> list[RuleSpec]:
    seen: dict[tuple[Any, ...], RuleSpec] = {}
    for s in d.segments:
        if s.type == LineType.ANNOTATION:
            continue
        r = rule_of(d, s)
        seen.setdefault(r.key(), r)
    return list(seen.values())


def capability(target: str, rule: RuleSpec, *, expand_perfs: bool = False) -> tuple[str, str]:
    """(status, why) for one rule on one target."""
    if target == "cf2":
        if rule.kind in ("cut", "crease"):
            if rule.side == "reverse":
                return UNVERIFIED, "a reverse crease has no verified CF2 code yet"
            if rule.pointage != DEFAULT_POINTAGE:
                return UNVERIFIED, f"{rule.pointage:g} pt rules haven't been verified on CF2 import (only 2 pt)"
            return VERIFIED, "code 1/2, the shop's CF2 import tuning"
        if rule.kind == "perforation":
            return UNVERIFIED, "the CF2 code and cut/tie encoding for a perforation aren't verified yet"
        if rule.kind == "cut_score":
            return UNVERIFIED, "the CF2 code for a cut-score / partial cut isn't verified yet"
        return VERIFIED, "annotation isn't written"
    if target == "dxf":
        if rule.kind == "cut":
            return VERIFIED, "colour 1"
        if rule.kind == "crease":
            if rule.side == "reverse":
                return UNSUPPORTED, "the sample table creases from one side only; a reverse crease can't be cut there"
            return VERIFIED, "colour 2"
        if rule.kind == "perforation":
            if expand_perfs:
                return UNVERIFIED, ("perforations expanded into dashed cuts haven't been cut and checked on the "
                                    "sample table yet")
            return UNSUPPORTED, ("the sample table has no perforation colour; a perforation can only go as "
                                 "dashed cuts (dxf_expand_perfs), which isn't verified yet")
        if rule.kind == "cut_score":
            return UNSUPPORTED, "the sample table has no colour for a cut-score / partial cut"
        return VERIFIED, "annotation isn't written"
    raise ValueError(f"unknown export target {target!r}")


def blocks(d: Dieline, target: str, *, expand_perfs: bool = False) -> list[str]:
    """Why this target can't carry the design (empty when it can)."""
    out: list[str] = []
    used = rules_used(d)
    for r in used:
        status, why = capability(target, r, expand_perfs=expand_perfs)
        if status != VERIFIED:
            out.append(f"{_kind_word(r)} isn't verified for {TARGETS[target]} yet: {r.describe()}; {why} ({status})")
    if target == "cf2":
        points = {r.pointage for r in used if r.kind != "annotation"}
        if len(points) > 1:
            listed = ", ".join(f"{p:g}" for p in sorted(points))
            out.append(f"mixed pointage ({listed} pt): the CF2 writer and import haven't been verified with more "
                       "than one pointage (unverified)")
    return out


def _kind_word(r: RuleSpec) -> str:
    if r.kind in ("cut", "crease") and r.side == "reverse":
        return "reverse crease"
    if r.kind in ("cut", "crease") and r.pointage != DEFAULT_POINTAGE:
        return f"{r.pointage:g} pt {r.kind}"
    return r.kind.replace("_", "-")


def readiness(d: Dieline, formats: list[str] | None = None, *, expand_perfs: bool = False) -> dict[str, Any]:
    """Each target's readiness for the preview and the export bar."""
    out: dict[str, Any] = {}
    for fmt in formats or list(TARGETS):
        why = blocks(d, fmt, expand_perfs=expand_perfs)
        out[fmt] = {"ready": not why, "blocking": why, "target": TARGETS[fmt]}
    return out


def table() -> dict[str, list[dict[str, str]]]:
    """The capability table itself, for the docs and the agent."""
    rows = [RuleSpec("cut"), RuleSpec("crease"), RuleSpec("crease", side="reverse"),
            RuleSpec("perforation", cut=PERF_CUT, tie=PERF_TIE), RuleSpec("cut_score"),
            RuleSpec("cut", pointage=3.0)]
    out: dict[str, list[dict[str, str]]] = {}
    for t in TARGETS:
        out[t] = []
        for r in rows:
            s, why = capability(t, r)
            out[t].append({"rule": r.describe() + (f" {r.pointage:g} pt" if r.kind == "cut" else ""),
                           "status": s, "why": why})
        if t == "dxf":
            s, why = capability(t, rows[3], expand_perfs=True)
            out[t].append({"rule": "perforation as dashed cuts", "status": s, "why": why})
    return out


# ------------------------------------------------------------------ perforation stationing


def perf_cuts(length: float, rule: RuleSpec, crossings: list[float], start: float = 0.0,
              min_piece: float | None = None) -> list[tuple[float, float]]:
    """The cut intervals of a perforation, in stations along its path.

    The pattern is continuous: it starts at `start` (the station of this run on
    one stationed path, so a jumped slot doesn't restart it) with the rule's
    phase, cut then tie. With crossing policy tie_at_creases a tie is centred on
    every crease station in `crossings`, by trimming whatever cut lands there;
    a trimmed piece shorter than `min_piece` (default half a tie) is dropped."""
    cut, tie = rule.cut or PERF_CUT, rule.tie or PERF_TIE
    if cut <= 0 or tie <= 0:
        raise ValueError("a perforation's cut and tie must be above zero")
    period = cut + tie
    lo, hi = start, start + length
    k = math.floor((lo + rule.phase) / period) - 1
    pieces: list[tuple[float, float]] = []
    while True:
        a = k * period - rule.phase
        b = a + cut
        k += 1
        if a >= hi:
            break
        if len(pieces) > 100_000:
            raise ValueError("a perforation with more than 100,000 cuts")
        a, b = max(a, lo), min(b, hi)
        if b - a > 1e-9:
            pieces.append((a, b))
    if rule.crossing == "tie_at_creases" and crossings:
        floor = tie / 2 if min_piece is None else min_piece
        for c in crossings:
            gap = (c - tie / 2, c + tie / 2)
            nxt: list[tuple[float, float]] = []
            for a, b in pieces:
                if b <= gap[0] or a >= gap[1]:
                    nxt.append((a, b))
                    continue
                for x, y in ((a, gap[0]), (gap[1], b)):
                    if y - x >= floor - 1e-12:
                        nxt.append((x, y))
            pieces = nxt
    return pieces


def cuts_on_creases(pieces: list[tuple[float, float]], crossings: list[float]) -> list[float]:
    """Crease stations a cut lands on (the continuous policy can leave these)."""
    return [c for c in crossings if any(a - 1e-9 <= c <= b + 1e-9 for a, b in pieces)]


def expand_line(seg: Line, pieces: list[tuple[float, float]], offset: float) -> list[Line]:
    """Cut pieces of a perforation run along one straight segment that starts at station `offset`."""
    L = seg.length
    ux, uy = (seg.end[0] - seg.start[0]) / L, (seg.end[1] - seg.start[1]) / L
    out = []
    for a, b in pieces:
        a0, b0 = max(a - offset, 0.0), min(b - offset, L)
        if b0 - a0 > 1e-9:
            out.append(Line((seg.start[0] + ux * a0, seg.start[1] + uy * a0),
                            (seg.start[0] + ux * b0, seg.start[1] + uy * b0), LineType.CUT, seg.op))
    return out


def crease_stations(d: Dieline, seg: Segment, ends: bool = False) -> list[float]:
    """Where a perforation segment (straight or an arc) crosses creases, as distances
    along it from its start. ends: also its ends when they stop on a crease (a tie goes
    there too, half of it inside)."""
    if isinstance(seg, Arc):
        return _arc_crease_stations(d, seg, ends)
    out: list[float] = []
    (ax, ay), (bx, by) = seg.start, seg.end
    rx, ry = bx - ax, by - ay
    L = math.hypot(rx, ry)
    for s in d.segments:
        if s.type != LineType.CREASE or not isinstance(s, Line):
            continue
        (cx, cy), (ex, ey) = s.start, s.end
        sx, sy = ex - cx, ey - cy
        den = rx * sy - ry * sx
        if abs(den) < 1e-12:
            continue
        t = ((cx - ax) * sy - (cy - ay) * sx) / den
        u = ((cx - ax) * ry - (cy - ay) * rx) / den
        lo, hi = (-1e-9, 1 + 1e-9) if ends else (1e-9, 1 - 1e-9)
        if lo < t < hi and -1e-9 <= u <= 1 + 1e-9:
            out.append(min(max(t, 0.0), 1.0) * L)
    return sorted(out)


def _arc_crease_stations(d: Dieline, arc: Arc, ends: bool) -> list[float]:
    """Where an arc crosses straight creases, as arc lengths from its start."""
    (ox, oy), r, sweep = arc.center, arc.radius, arc.sweep
    a0 = math.atan2(arc.start[1] - oy, arc.start[0] - ox)
    L = abs(sweep) * r
    out: list[float] = []
    for s in d.segments:
        if s.type != LineType.CREASE or not isinstance(s, Line):
            continue
        (cx, cy), (ex, ey) = s.start, s.end
        dx, dy = ex - cx, ey - cy
        qa = dx * dx + dy * dy
        if qa < 1e-18:
            continue
        fx, fy = cx - ox, cy - oy
        qb = 2 * (fx * dx + fy * dy)
        disc = qb * qb - 4 * qa * (fx * fx + fy * fy - r * r)
        if disc < 0:
            continue
        for u in {(-qb - math.sqrt(disc)) / (2 * qa), (-qb + math.sqrt(disc)) / (2 * qa)}:
            if not -1e-9 <= u <= 1 + 1e-9:
                continue
            ang = math.atan2(cy + dy * u - oy, cx + dx * u - ox) - a0
            ang = ang % math.tau if sweep > 0 else -((-ang) % math.tau)
            st = abs(ang) * r
            if arc.full_circle and st > L - 1e-9:
                st = 0.0
            lo, hi = (-1e-9, L + 1e-9) if ends else (1e-9, L - 1e-9)
            if lo < st < hi:
                out.append(min(max(st, 0.0), L))
    return sorted(out)


def expand_arc(seg: Arc, pieces: list[tuple[float, float]], offset: float) -> list[Arc]:
    """Cut pieces of a perforation run along one arc that starts at station `offset`."""
    (ox, oy), r, sweep = seg.center, seg.radius, seg.sweep
    a0 = math.atan2(seg.start[1] - oy, seg.start[0] - ox)
    L = abs(sweep) * r
    k = 1.0 if sweep > 0 else -1.0
    at = lambda s: (ox + r * math.cos(a0 + k * s / r), oy + r * math.sin(a0 + k * s / r))  # noqa: E731
    out = []
    for a, b in pieces:
        a0_, b0_ = max(a - offset, 0.0), min(b - offset, L)
        if b0_ - a0_ > 1e-9:
            p, q = (seg.start if a0_ <= 1e-12 else at(a0_)), (seg.end if b0_ >= L - 1e-12 else at(b0_))
            out.append(Arc(p, q, seg.center, seg.clockwise, LineType.CUT, False, seg.op))
    return out


def expand_perforations(d: Dieline) -> Dieline:
    """The sample-table representation of a design's perforations: each one as
    its dashed cut pieces (a separate representation, unverified until cut on
    the table). The stationing runs on along each perforation operation, over
    jumped slots too (the features record each run's station; a run they don't
    record continues from the one before it in the same operation), so the
    pattern doesn't restart at every panel. Arcs (curved perforation artwork)
    become dashed arcs. Contacts carry over: each perforation's cuts may end
    where it ended (a tear strip's tab cuts), and whatever ended on the
    perforation ends on its cuts. The result goes through the topology rules
    like any output: a cut landing on a crease is a crossing there."""
    from dataclasses import replace  # noqa: PLC0415

    from .geometry import Operation  # noqa: PLC0415

    stations: dict[Any, float] = d.extras.get("stations") or {}  # type: ignore[assignment]
    out = Dieline(d.name, d.units, [], d.meta, dict(d.ops), d.faces, dict(d.extras))
    by_op: dict[str | None, list[Segment]] = {}
    for s in d.segments:
        if s.type == LineType.PERFORATION:
            by_op.setdefault(s.op, []).append(s)
        else:
            out.segments.append(s)
    renamed = {op_id: f"{op_id or 'perf'}.cuts" for op_id in by_op}
    # Ops a dashed cut may end on: named cuts only (a tear strip's tab cuts or thumb hole), never a crease.
    cut_ops = {s.op for s in d.segments if s.type == LineType.CUT and s.op}
    for op_id, segs in by_op.items():
        rule = rule_of(d, segs[0])
        old = d.ops.get(op_id) if op_id else None
        # Each piece may stop in the board or on the blank's edge or a crease it ends at, and on what
        # the perforation itself may end on; crossing a crease is undeclared, so a cut landing on one
        # is invalid here.
        carried = tuple(renamed.get(x, x) for x in (old.ends_on if old is not None else ())
                        if x in renamed or (x in cut_ops and x != "outline"))
        cut_op = Operation(renamed[op_id], "perf_cut", open_ends="free", ends_on=("outline", *carried),
                           rule=RuleSpec("cut", rule.pointage))
        out.ops[cut_op.id] = cut_op
        run = 0.0
        for seg in segs:
            st = float(stations[seg]) if seg in stations else run
            run = st + seg.length
            crossings = [st + c for c in crease_stations(d, seg, ends=True)]
            pieces = perf_cuts(seg.length, rule, crossings, start=st)
            if isinstance(seg, Arc):
                out.segments += [replace(p, op=cut_op.id) for p in expand_arc(seg, pieces, st)]
            else:
                out.segments += [Line(p.start, p.end, LineType.CUT, cut_op.id) for p in expand_line(seg, pieces, st)]
    # Whatever ended on (or crossed) a perforation now meets its cuts.
    for oid, op in list(out.ops.items()):
        if oid in renamed.values():
            continue
        ends = tuple(renamed.get(x, x) for x in op.ends_on)
        crosses = tuple(renamed.get(x, x) for x in op.crosses)
        if ends != op.ends_on or crosses != op.crosses:
            out.ops[oid] = replace(op, ends_on=ends, crosses=crosses)
    return out

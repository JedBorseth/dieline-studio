"""The design's own checks: partition sets, fit links and stacks
(docs/DESIGN-SYSTEM.md, "Fit between parts" and "Partition checks").

A fit link compares the contained part's or group's **assembled outside**
with the container's **assembled inside**:

- a standard box: inside is its L x W x D; outside is inside + 2 x CAL
- a partition set: its pieces' lengths and its height
- a stack: its widest tier, and the tiers' heights plus the layer pads

Across the two plan axes (longer matched with longer): under 1/16" of
clearance warns that it's tight, over 1/2" that it's loose, and a negative
clearance that it doesn't fit. Upwards, taller than the container's inside
depth warns (the lid won't close); a shorter insert is normal, so there's no
loose warning on depth. These are provisional limits.

Not checked, and said so: loading past inside joints and flaps, and on a
telescoping pair whether features on the lid and base walls line up.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from .boards import get_board
from .checks import CheckResult, fmt_in

TIGHT = 1 / 16
LOOSE = 1 / 2
EPS = 1e-6


@dataclass(frozen=True, slots=True)
class Envelope:
    a: float  # the two plan sizes, longer first
    b: float
    height: float
    what: str  # in words, for messages


def _plan(spec: Any):
    from .partitions import plan_set

    return plan_set(spec)


def _part(state: Any, ref: str):
    return next((p for p in state.parts if p.id == ref), None)


def _group(state: Any, ref: str):
    return next((g for g in state.groups if g.id == ref), None)


def _is_partition(params: Any) -> bool:
    return getattr(params, "style", None) == "partition-slotted"


def set_spec(state: Any, g: Any):
    """The spec every piece of a set shares, or None when its pieces disagree (or are gone)."""
    specs = []
    for m in g.members:
        p = _part(state, m.ref)
        if p is None or not _is_partition(p.params):
            return None
        specs.append(p.params.set)
    if not specs or any(s != specs[0] for s in specs[1:]):
        return None
    return specs[0]


def inside(state: Any, ref: str) -> Envelope | str:
    p = _part(state, ref)
    if p is not None:
        if _is_partition(p.params):
            return "a partition piece holds nothing"
        dims = p.params.dims_in()
        a, b = sorted((dims.length, dims.width), reverse=True)
        return Envelope(a, b, dims.depth, f"{p.label} inside {fmt_in(dims.length)} x {fmt_in(dims.width)} x "
                                          f"{fmt_in(dims.depth)} in")
    g = _group(state, ref)
    kind = g.kind if g else "group"
    return f"a {kind} group as a container isn't modelled yet; link the box itself"


def outside(state: Any, ref: str, _visiting: frozenset[str] = frozenset()) -> Envelope | str:
    if ref in _visiting:
        return f"{ref} contains itself (a group that's its own member); remove it from the group"
    p = _part(state, ref)
    if p is not None:
        if _is_partition(p.params):
            owner = next((g.id for g in state.groups if any(m.ref == p.id for m in g.members)), None)
            return f"{p.label} is one piece of a set; link the whole set{f' ({owner})' if owner else ''}"
        dims = p.params.dims_in()
        cal = get_board(p.params.board).cal
        a, b = sorted((dims.length + 2 * cal, dims.width + 2 * cal), reverse=True)
        return Envelope(a, b, dims.depth + 2 * cal, f"{p.label} outside {fmt_in(dims.length + 2 * cal)} x "
                                                    f"{fmt_in(dims.width + 2 * cal)} in")
    g = _group(state, ref)
    if g is None:
        return f"{ref} isn't in the design"
    if g.kind == "partition_set":
        spec = set_spec(state, g)
        if spec is None:
            return f"set {g.id}'s pieces don't make one set"
        lo, so, ho = _plan(spec).outside
        a, b = sorted((lo, so), reverse=True)
        return Envelope(a, b, ho, f"set {g.id} {fmt_in(lo)} x {fmt_in(so)} x {fmt_in(ho)} in")
    if g.kind == "stack":
        if not g.members:
            return f"stack {g.id} has no tiers"
        tiers = [outside(state, m.ref, _visiting | {ref}) for m in g.members]
        bad = [t for t in tiers if isinstance(t, str)]
        if bad:
            return bad[0]
        extra = dict(g.extra)
        pads = extra.get("pads", 0)
        if isinstance(pads, bool) or not isinstance(pads, int) or pads < 0:
            return f"stack {g.id}'s pad count isn't a whole number"
        try:
            pad_cal = get_board(extra["pad_board"]).cal if pads else 0.0
        except (KeyError, TypeError, ValueError):
            return f"stack {g.id} has {pads} pads but no known pad board"
        h = sum(t.height for t in tiers) + pads * pad_cal  # type: ignore[union-attr]
        a = max(t.a for t in tiers)  # type: ignore[union-attr]
        b = max(t.b for t in tiers)  # type: ignore[union-attr]
        return Envelope(a, b, h, f"stack {g.id} ({len(tiers)} tiers, {pads} pads) {fmt_in(h)} in high")
    return f"a {g.kind} group's assembled envelope isn't modelled yet"


def link_checks(state: Any, lk: Any) -> list[CheckResult]:
    cid = lk.id if lk.id.startswith("fit") else f"fit_{lk.id}"
    box, held = inside(state, lk.container), outside(state, lk.contained)
    for env in (box, held):
        if isinstance(env, str):
            return [CheckResult(cid, "unavailable", f"fit {lk.id} couldn't be checked: {env}")]
    assert isinstance(box, Envelope) and isinstance(held, Envelope)
    out: list[CheckResult] = []
    gaps = [box.a - held.a, box.b - held.b]
    words = []
    worst = "pass"
    for gap in gaps:
        if gap < -EPS:
            words.append(f"{fmt_in(-gap)} in too big")
            worst = "warn"
        elif gap < TIGHT - EPS:
            words.append(f"tight ({fmt_in(gap)} in clearance)")
            worst = "warn"
        elif gap > LOOSE + EPS:
            words.append(f"loose ({fmt_in(gap)} in clearance)")
            worst = "warn"
        else:
            words.append(f"{fmt_in(gap)} in clearance")
    msg = f"fit {lk.id}: {held.what} in {box.what}: {words[0]} along the length, {words[1]} across"
    if worst == "warn" and any(g < -EPS for g in gaps):
        msg += "; it doesn't go in"
    out.append(CheckResult(cid, worst, msg))
    g = _group(state, lk.contained)
    if g is not None and g.kind == "stack":  # the stack's own check reports its height, once
        if held.height > box.height + EPS:
            out.append(CheckResult(f"stack_{g.id}", "warn", f"stack {g.id}: tiers and pads come to "
                                                            f"{fmt_in(held.height)} in, over the "
                                                            f"{fmt_in(box.height)} in inside depth: the lid won't "
                                                            "close"))
        else:
            out.append(CheckResult(f"stack_{g.id}", "pass", f"stack {g.id}: tiers and pads {fmt_in(held.height)} "
                                                            f"in, inside the {fmt_in(box.height)} in depth"))
    elif held.height > box.height + EPS:
        out.append(CheckResult(f"{cid}_depth", "warn", f"fit {lk.id}: {fmt_in(held.height)} in high in a "
                                                       f"{fmt_in(box.height)} in deep box: the lid won't close"))
    return out


def design_checks(state: Any) -> list[CheckResult]:
    from .partitions import Refused, set_checks

    out: list[CheckResult] = []
    for g in state.groups:
        if g.kind != "partition_set":
            continue
        spec = set_spec(state, g)
        if spec is None:
            out.append(CheckResult(f"partition_{g.id}_reciprocity", "fail",
                                   f"set {g.id}'s pieces don't come from one set any more (a piece was changed or "
                                   "removed on its own); replace the set or remove it"))
            continue
        present: dict[str, int] = {}
        for m in g.members:
            p = _part(state, m.ref)
            present[p.params.role] = present.get(p.params.role, 0) + m.quantity  # type: ignore[union-attr]
        try:
            out += set_checks(g.id, _plan(spec), present)
        except Refused as exc:
            out.append(CheckResult(f"partition_{g.id}_reciprocity", "fail", str(exc)))
    # A partition piece is made, checked and exported with its set: one on its own (a job file or a
    # replace that kept a piece but not its group) skips the set's crossing, interlock and fit checks.
    grouped = {m.ref for g in state.groups if g.kind == "partition_set" for m in g.members}
    for p in state.parts:
        if getattr(p.params, "style", None) == "partition-slotted" and p.id not in grouped:
            out.append(CheckResult(f"partition_{p.id}_set", "fail",
                                   f"partition piece {p.id} isn't in a partition set, so its slots aren't checked "
                                   "against the pieces they cross; recreate the set with create_partition"))
    known = state.ids()
    per_container: dict[str, list[str]] = defaultdict(list)
    for lk in state.links:
        if lk.container not in known or lk.contained not in known:
            continue  # stale: reported by the store
        out += link_checks(state, lk)
        per_container[lk.container].append(lk.contained)
    for container, held in per_container.items():
        sets = [r for r in held if getattr(_group(state, r), "kind", None) == "partition_set"]
        if len(sets) > 1:
            out.append(CheckResult(f"fit_{container}_stack", "warn",
                                   f"{len(sets)} partition sets ({', '.join(sets)}) are linked into {container} "
                                   "without a stack group; make them a stack (with their layer pads) so their "
                                   "heights are checked together"))
    if any(lk.container in known and lk.contained in known for lk in state.links):
        out.append(CheckResult("fit_unchecked", "info", "fit checks compare envelopes only: loading past inside "
                                                        "joints and flaps isn't checked"))
    if any(g.kind == "telescoping" for g in state.groups):
        out.append(CheckResult("fit_telescoping_unchecked", "info", "on the telescoping pair, whether features on "
                                                                    "the lid and base walls line up isn't checked"))
    return out

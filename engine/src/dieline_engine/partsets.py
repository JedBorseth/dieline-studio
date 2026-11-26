"""Design changes for partition sets, fit links and stacks: pure functions
from one DesignState to the next, run through DesignStore.mutate like every
other change (docs/DESIGN-SYSTEM.md, "Design and parts").

- A set is a `partition_set` group. Each distinct piece is a part in it, with
  its quantity per set (the group owns it; the part carries the same number).
- `replace_set` keeps the group id, so links to the set stay valid and are
  checked again. New pieces are matched to old ones by role: a matched piece
  keeps its part id (and label), an unmatched old one is removed (direct links
  to it go stale), and an unmatched new one gets a new id.
- Ids of groups and links are never reused, so a stale link can't come back
  to life pointing at something new.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any

from .designstore import DesignState, FitLink, Group, Member, Part
from .partitions import PartitionParams, PartitionSpec, plan_set


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:6]}"


def pieces(spec: PartitionSpec) -> list[tuple[str, PartitionParams, int]]:
    """(role, the part's parameters, quantity per set) for each distinct piece."""
    plan = plan_set(spec)
    return [(p.role, PartitionParams(units=spec.units, board=p.board, role=p.role, set=spec), p.quantity)
            for p in plan.pieces]


def _label(d: DesignState | None, base: str, taken: set[str] | None = None) -> str:
    taken = set(taken or ()) | ({p.label for p in d.parts} if d else set())
    name, n = base, 2
    while name in taken:
        name, n = f"{base} {n}", n + 1
    return name


def resolve(d: DesignState, ref: str) -> str:
    """A part (id or label) or a group id."""
    if any(g.id == ref for g in d.groups):
        return ref
    return d.part(ref).id


def add_set(d: DesignState | None, spec: PartitionSpec, label: str | None = None,
            contained_in: str | None = None, gid: str | None = None) -> tuple[DesignState, str]:
    """The set as a new group of parts, added to the design (or a new design when there's none)."""
    if d is None:
        d = DesignState(uuid.uuid4().hex[:12], 1, (), next_part=1)
    container = resolve(d, contained_in) if contained_in is not None else None
    base = label or "partition"
    if label and any(p.label.startswith(f"{label} ") or p.label == label for p in d.parts):
        raise ValueError(f"another part is already labelled {label!r}; choose another label")
    gid = gid or _new_id("set")
    if any(g.id == gid for g in d.groups):
        raise ValueError(f"group {gid} already exists")
    parts, members, n = list(d.parts), [], d.next_part
    taken: set[str] = set()
    for role, params, qty in pieces(spec):
        name = _label(d, f"{base} {role}", taken)
        taken.add(name)
        pid = f"p{n}"
        n += 1
        parts.append(Part(pid, name, params, qty))
        members.append(Member(pid, role, qty))
    links = d.links
    if container is not None:
        links = (*links, FitLink(_new_id("fit"), container, gid))
    return replace(d, parts=tuple(parts), groups=(*d.groups, Group(gid, "partition_set", tuple(members))),
                   links=links, next_part=n), gid


def set_group(d: DesignState, set_id: str) -> Group:
    for g in d.groups:
        if g.id == set_id and g.kind == "partition_set":
            return g
    sets = [g.id for g in d.groups if g.kind == "partition_set"]
    raise ValueError(f"no partition set {set_id!r}; sets: {', '.join(sets) or 'none'}")


def replace_set(d: DesignState, set_id: str, spec: PartitionSpec, contained_in: str | None = None) -> DesignState:
    g = set_group(d, set_id)
    by_role: dict[str, Part] = {}
    for m in g.members:
        part = next((p for p in d.parts if p.id == m.ref), None)
        if part is not None:
            by_role[m.role] = part
    new = pieces(spec)
    keep_roles = {role for role, _, _ in new}
    gone = {p.id for role, p in by_role.items() if role not in keep_roles}
    parts = [p for p in d.parts if p.id not in gone]
    members, n = [], d.next_part
    base = next((p.label.rsplit(" ", 1)[0] for p in by_role.values() if " " in p.label), "partition")
    for role, params, qty in new:
        old = by_role.get(role)
        if old is not None:
            parts = [replace(p, params=params, quantity=qty) if p.id == old.id else p for p in parts]
            members.append(Member(old.id, role, qty))
        else:
            pid = f"p{n}"
            n += 1
            parts.append(Part(pid, _label(replace(d, parts=tuple(parts)), f"{base} {role}"), params, qty))
            members.append(Member(pid, role, qty))
    groups = tuple(replace(x, members=tuple(members)) if x.id == set_id else
                   replace(x, members=tuple(m for m in x.members if m.ref not in gone)) for x in d.groups)
    links = d.links
    if contained_in is not None:
        container = resolve(d, contained_in)
        if not any(lk.container == container and lk.contained == set_id for lk in links):
            links = (*links, FitLink(_new_id("fit"), container, set_id))
    return replace(d, parts=tuple(parts), groups=groups, links=links, next_part=n)


def remove_group(d: DesignState, gid: str) -> DesignState:
    """Remove a group. A partition set takes its pieces with it; a stack or an
    assembly only goes itself (its members stay). Links to it go stale."""
    g = next((x for x in d.groups if x.id == gid), None)
    if g is None:
        raise ValueError(f"no group {gid!r}; groups: {', '.join(x.id for x in d.groups) or 'none'}")
    gone = {m.ref for m in g.members} if g.kind == "partition_set" else set()
    parts = tuple(p for p in d.parts if p.id not in gone)
    if not parts:
        raise ValueError("that would leave the design with no parts; to start over, replace the whole design")
    groups = tuple(replace(x, members=tuple(m for m in x.members if m.ref not in gone and m.ref != gid))
                   for x in d.groups if x.id != gid)
    return replace(d, parts=parts, groups=tuple(x for x in groups if x.members))


def add_link(d: DesignState, container: str, contained: str) -> tuple[DesignState, str]:
    a, b = resolve(d, container), resolve(d, contained)
    if a == b:
        raise ValueError("a part or set can't contain itself")
    for lk in d.links:
        if lk.container == a and lk.contained == b:
            raise ValueError(f"fit link {lk.id} already joins {a} and {b}")
    lid = _new_id("fit")
    return replace(d, links=(*d.links, FitLink(lid, a, b))), lid


def remove_link(d: DesignState, link_id: str) -> DesignState:
    if not any(lk.id == link_id for lk in d.links):
        raise ValueError(f"no fit link {link_id!r}; links: {', '.join(lk.id for lk in d.links) or 'none'}")
    return replace(d, links=tuple(lk for lk in d.links if lk.id != link_id))


def add_stack(d: DesignState, tiers: list[str], pads: int = 0, pad_board: str | None = None,
              contained_in: str | None = None) -> tuple[DesignState, str]:
    """Tiers of partition sets with layer pads between them, as one stack group."""
    from .boards import get_board

    if not tiers:
        raise ValueError("a stack needs at least one tier (a partition set id)")
    members = []
    for t in tiers:
        set_group(d, t)
        if any(m.ref == t for m in members):
            raise ValueError(f"set {t} is listed twice")
        members.append(Member(t, "tier", 1))
    if isinstance(pads, bool) or not isinstance(pads, int) or pads < 0 or pads > 40:
        raise ValueError("pads must be a whole number from 0 to 40")
    extra: dict[str, Any] = {"pads": pads}
    if pads:
        extra["pad_board"] = get_board(pad_board or "32 B KR").id
    gid = _new_id("stack")
    d = replace(d, groups=(*d.groups, Group(gid, "stack", tuple(members), tuple(sorted(extra.items())))))
    if contained_in is not None:
        d, _ = add_link(d, contained_in, gid)
    return d, gid

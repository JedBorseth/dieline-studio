"""The engine's designs: one per chat session, with an id and a revision
(docs/DESIGN-SYSTEM.md, "Design and parts" and "Agent tools / Revisions").

Every change, from the form, a reopened job or the chat, goes through
`DesignStore.mutate`:

- It's atomic. The new design is built and checked in full before anything
  changes, outside the lock; then it's committed under the lock only if the
  design is still the one the change was based on (compare-and-swap). An edit
  made meanwhile is never overwritten; the change is refused instead.
- A caller may pass the design id and revision it expects (the chat always
  does). A mismatch is refused with DesignChanged.
- A request id makes a retry safe: the same id returns the first result and
  isn't applied twice.
- dry_run builds and checks the change and returns it without committing.

Exports work from a snapshot taken by id and revision, so the files are
exactly what was on screen.
"""

from __future__ import annotations

import copy
import json
import threading
import uuid
from contextlib import contextmanager
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any, Callable

from . import designs as _designs
from .checks import CheckResult, overall
from .designs import BoxParams
from .geometry import Arc, Dieline


class DesignChanged(ValueError):
    """The design isn't the one the change was based on: someone else edited it."""


@dataclass(frozen=True, slots=True)
class Part:
    id: str  # stable, engine-made: p1, p2, ...
    label: str
    params: BoxParams  # or a partition piece's PartitionParams (partitions.py)
    quantity: int = 1
    piece_no: str | None = None  # the shop's number for it, when known


@dataclass(frozen=True, slots=True)
class Member:
    ref: str  # a part id, or a group id (a stack can hold partition sets)
    role: str
    quantity: int = 1


@dataclass(frozen=True, slots=True)
class Group:
    id: str
    kind: str  # partition_set | telescoping | stack | assembly
    members: tuple[Member, ...]
    # Group-level settings, e.g. a stack's layer pads ({"pads": 2, "pad_board": "32 B KR"}).
    extra: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class FitLink:
    """The container's assembled inside against the contained's assembled outside."""

    id: str
    container: str  # part or group id
    contained: str


@dataclass(frozen=True, slots=True)
class DesignState:
    id: str
    revision: int
    parts: tuple[Part, ...]
    groups: tuple[Group, ...] = ()
    links: tuple[FitLink, ...] = ()
    next_part: int = 1
    seq: int = 0  # the session's commit order, across replacements (ids alone can't order them)

    def part(self, ref: str | None) -> Part:
        """A part by exact id first, then by label; with no ref, the only part. Errors list the parts."""
        listing = ", ".join(f"{p.id} ({p.label})" for p in self.parts)
        if ref is None:
            if len(self.parts) == 1:
                return self.parts[0]
            raise ValueError(f"this design has {len(self.parts)} parts; say which: {listing}")
        for p in self.parts:
            if p.id == ref:
                return p
        named = [p for p in self.parts if p.label == ref]
        if len(named) == 1:
            return named[0]
        if named:
            raise ValueError(f"several parts are labelled {ref!r} ({', '.join(p.id for p in named)}); use the id")
        raise ValueError(f"no part {ref!r}; parts: {listing}")

    def ids(self) -> set[str]:
        return {p.id for p in self.parts} | {g.id for g in self.groups}


# ------------------------------------------------------------------ changes (pure)


def new_design(params: BoxParams, label: str | None = None, quantity: int = 1) -> DesignState:
    _check_label(label)
    return DesignState(uuid.uuid4().hex[:12], 1, (Part("p1", label or _label(params), params, quantity),), next_part=2)


def add_part(d: DesignState, params: BoxParams, label: str | None = None, quantity: int = 1) -> DesignState:
    _check_label(label, d)
    pid = f"p{d.next_part}"
    taken = {p.label for p in d.parts}
    name = label or _label(params)
    if not label:  # a default label stays unique: "reg-glio-nx", "reg-glio-nx 2", ...
        n = 2
        while name in taken:
            name, n = f"{_label(params)} {n}", n + 1
    return replace(d, parts=(*d.parts, Part(pid, name, params, quantity)), next_part=d.next_part + 1)


def design_from_parts(parts: list[dict[str, Any]], groups: Any = (), links: Any = ()) -> DesignState:
    """A new design rebuilt from a job record's parts, keeping their ids, labels and quantities
    (so fit links and groups still point at the same parts)."""
    built: list[Part] = []
    for raw in parts:
        pid = raw.get("part_id")
        if not isinstance(pid, str) or not (len(pid) > 1 and pid[0] == "p" and pid[1:].isdigit()):
            raise ValueError(f"job file part id {pid!r} isn't a part id")
        if any(p.id == pid for p in built):
            raise ValueError(f"job file lists part {pid} twice")
        label = raw.get("label") or _label(raw["params"])
        if any(p.label == label for p in built):
            label = f"{label} ({pid})"
        q = raw.get("quantity", 1)
        if type(q) is not int or q < 1:
            raise ValueError("job file part quantity must be a whole number of at least 1")
        piece = raw.get("piece_no")
        built.append(Part(pid, label, raw["params"], q, piece if isinstance(piece, str) else None))
    if not built:
        raise ValueError("job file has no parts")
    gs = []
    for g in groups if isinstance(groups, list) else []:
        if isinstance(g, dict) and isinstance(g.get("id"), str) and isinstance(g.get("members"), list):
            members = []
            for m in g["members"]:
                q = m.get("quantity", 1) if isinstance(m, dict) else None
                if not isinstance(m, dict) or not isinstance(m.get("ref"), str) or type(q) is not int or q < 1:
                    raise ValueError(f"job file group {g['id']} has a member this app doesn't write")
                members.append(Member(m["ref"], str(m.get("role", "")), q))
            members = tuple(members)
            extra = g.get("extra") if isinstance(g.get("extra"), dict) else {}
            gs.append(Group(g["id"], str(g.get("kind", "assembly")), members,
                            tuple(sorted((str(k), v) for k, v in extra.items()))))
    lks = [FitLink(str(lk["id"]), str(lk["container"]), str(lk["contained"]))
           for lk in (links if isinstance(links, list) else [])
           if isinstance(lk, dict) and all(isinstance(lk.get(k), str) for k in ("id", "container", "contained"))]
    top = max(int(p.id[1:]) for p in built)
    return DesignState(uuid.uuid4().hex[:12], 1, tuple(built), tuple(gs), tuple(lks), next_part=top + 1)


def _check_label(label: str | None, d: DesignState | None = None) -> None:
    """A label may not look like a part id (p1, p2, ...) or repeat another part's label."""
    if label is None:
        return
    if not label.strip():
        raise ValueError("a label can't be empty")
    if len(label) > 1 and label[0] == "p" and label[1:].isdigit():
        raise ValueError(f"{label!r} looks like a part id; choose another label")
    if d is not None and any(p.label == label for p in d.parts):
        raise ValueError(f"another part is already labelled {label!r}")


def update_part(d: DesignState, ref: str | None, params: BoxParams | None = None, label: str | None = None,
                quantity: int | None = None) -> DesignState:
    """Replace a part's parameters, label or quantity. Its id and its place in its groups stay."""
    old = d.part(ref)
    if label is not None and label != old.label:
        _check_label(label, d)
    new = replace(old, params=params or old.params, label=label or old.label,
                  quantity=old.quantity if quantity is None else quantity)
    return replace(d, parts=tuple(new if p.id == old.id else p for p in d.parts))


def remove_part(d: DesignState, ref: str | None) -> DesignState:
    """Remove a part; it leaves its groups (an empty group goes too). Links to it become stale."""
    old = d.part(ref)
    if len(d.parts) == 1:
        raise ValueError("a design needs at least one part; to start over, create_box replaces the whole design")
    gone = {old.id}
    groups = list(d.groups)
    while True:  # a group left empty goes too, and so does any reference to it, all the way up
        kept = []
        for g in groups:
            members = tuple(m for m in g.members if m.ref not in gone)
            if members:
                kept.append(replace(g, members=members))
            else:
                gone.add(g.id)
        if len(kept) == len(groups) and all(a.members == b.members for a, b in zip(kept, groups)):
            break
        groups = kept
    return replace(d, parts=tuple(p for p in d.parts if p.id != old.id), groups=tuple(groups))


def _label(params: BoxParams) -> str:
    return params.style


def with_features(params: BoxParams, items: list[Any] | None, remove: list[Any] | None = None
                  ) -> tuple[BoxParams, list[str]]:
    """A part's parameters with features upserted by id (docs/DESIGN-SYSTEM.md, "Same pattern everywhere"):
    new items get engine-made ids, changed items keep theirs, `remove` deletes. Returns the ids touched."""
    from . import features  # noqa: PLC0415

    if not isinstance(params, BoxParams):
        raise ValueError("features go on standard box parts only; partition pieces can't take them yet")
    feats, touched = features.upsert(params.features, items, remove)
    base = params.model_dump()
    base.pop("features", None)
    fm = _designs.build(BoxParams.model_validate(base)).faces if feats else None
    feats = features.bind(feats, fm, touched)
    return BoxParams.model_validate({**base, "features": feats}), touched


def set_features(d: DesignState, ref: str | None, items: list[Any] | None, remove: list[Any] | None = None
                 ) -> tuple[DesignState, str, list[str]]:
    """Upsert and remove one part's features. Returns the new state, the part id and the ids touched."""
    old = d.part(ref)
    params, touched = with_features(old.params, items, remove)
    return update_part(d, old.id, params), old.id, touched


def with_artwork(params: BoxParams, items: list[Any] | None, remove: list[Any] | None = None, *,
                 trusted: bool = False) -> tuple[BoxParams, list[str]]:
    """A part's parameters with artwork upserted by id (docs/DESIGN-SYSTEM.md, "Same pattern everywhere").
    Untrusted (the agent's) input can't carry an import's geometry: that comes only with the designer's
    approval. Acknowledgements of removed artwork go with it."""
    from . import artwork  # noqa: PLC0415

    if not isinstance(params, BoxParams):
        raise ValueError("artwork goes on standard box parts only; partition pieces can't take it yet")
    arts, touched = artwork.upsert(params.artwork, items, remove, trusted=trusted)
    base = params.model_dump()
    base.pop("artwork", None)
    acks = [k for k in params.acknowledged if k.split(":")[0] in {a["id"] for a in arts}]
    base["acknowledged"] = acks
    fm = _designs.build(BoxParams.model_validate(base)).faces if arts else None
    arts = artwork.bind(arts, fm, touched)
    return BoxParams.model_validate({**base, "artwork": arts}), touched


def set_artwork(d: DesignState, ref: str | None, items: list[Any] | None, remove: list[Any] | None = None, *,
                trusted: bool = False) -> tuple[DesignState, str, list[str]]:
    """Upsert and remove one part's artwork. Returns the new state, the part id and the ids touched."""
    old = d.part(ref)
    params, touched = with_artwork(old.params, items, remove, trusted=trusted)
    return update_part(d, old.id, params), old.id, touched


def acknowledge(d: DesignState, ref: str, keys: list[str]) -> DesignState:
    """The designer acknowledged suspicious artwork in the checks panel (never the agent: the store
    refuses a chat change that adds one)."""
    old = d.part(ref)
    if not isinstance(old.params, BoxParams):
        raise ValueError("that part has no artwork")
    acks = sorted(set(old.params.acknowledged) | set(keys))
    return update_part(d, old.id, old.params.model_copy(update={"acknowledged": acks}))


def _acks(state: DesignState | None) -> dict[str, set[str]]:
    if state is None:
        return {}
    return {p.id: set(getattr(p.params, "acknowledged", None) or []) for p in state.parts}


class AcknowledgeRefused(ValueError):
    """Only the designer can acknowledge suspicious artwork."""


def _no_new_acks(cur: DesignState | None, proposed: DesignState) -> None:
    """A chat change may keep or drop acknowledgements, never add one, even by replacing the design."""
    before = _acks(cur) if cur is not None and proposed.id == cur.id else {}
    for pid, keys in _acks(proposed).items():
        if keys - before.get(pid, set()):
            raise AcknowledgeRefused("only the designer can acknowledge suspicious artwork, in the checks panel; "
                                     "the chat can't (tell the user what the flag is about instead)")


def refuse_stale(payload: dict[str, Any], part_id: str, ids: list[str]) -> None:
    """A call that adds or changes a feature is refused if that feature can't be placed: the
    previous revision stays. (Only a resize or a style change may leave a feature stale.)"""
    part = next((p for p in payload.get("parts", []) if p.get("part_id") == part_id), None)
    if part is None:
        return
    bad = [f for f in part.get("features", []) if f["id"] in ids and f["status"] == "stale"]
    if bad:
        raise ValueError("; ".join(f"{f['label']} {f['id']} can't be placed: {f.get('reason')}" for f in bad))


# ------------------------------------------------------------------ the store


@dataclass(slots=True)
class _Built:
    payload: dict[str, Any]
    dieline: Dieline


class DesignStore:
    def __init__(self, emit: Callable[[dict[str, Any]], None] | None = None) -> None:
        self._lock = threading.Lock()
        self._by_session: dict[str, DesignState] = {}
        self._done: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
        self._built: OrderedDict[str, _Built] = OrderedDict()
        self._seq: dict[str, int] = {}  # each session's commit count; it never goes back
        self._emit = emit

    # ------------------------------------------------------------ reading

    def current(self, session: str) -> DesignState | None:
        with self._lock:
            return self._by_session.get(session)

    def snapshot(self, session: str, design_id: str | None = None, revision: int | None = None) -> DesignState:
        """The session's design, checked against an expected id and revision when given."""
        with self._lock:
            cur = self._by_session.get(session)
        if cur is None:
            raise ValueError("no design yet")
        if design_id is not None and (cur.id, cur.revision) != (design_id, revision):
            raise DesignChanged(_changed_msg())
        return cur

    def dieline(self, state: DesignState, ref: str | None = None) -> Dieline:
        return copy.deepcopy(self._build(state.part(ref).params).dieline)

    def export_parts(self, state: DesignState) -> list[Any]:
        """Every part to export, after the design's own checks: a stale or failed design
        check (a fit link to a removed part) blocks the export like a failed part check."""
        from .exports import ExportPart

        self.check_exportable(state)
        return [ExportPart(p.id, p.label, p.params, p.quantity, p.piece_no) for p in state.parts]

    def check_exportable(self, state: DesignState) -> list[dict[str, Any]]:
        """The design's own checks, refusing if any blocks; returned for the manifest."""
        checks = [c.to_dict() for c in self._design_checks(state)]
        blocking = [c for c in checks if c["status"] in ("fail", "stale")]
        if blocking:
            raise _designs.DesignCheckFailed("not exported: " + "; ".join(c["message"] for c in blocking))
        return checks

    def replay(self, session: str, request_id: str) -> dict[str, Any] | None:
        """The result of a request already committed, for a retry."""
        with self._lock:
            hit = self._done.get((session, request_id))
        return copy.deepcopy(hit) if hit is not None else None

    @contextmanager
    def commit_boundary(self):
        """Hold off commits: a chat Stop takes this so no change of the stopped turn lands after it."""
        with self._lock:
            yield

    def payload(self, state: DesignState) -> dict[str, Any]:
        """What the UI and the agent see: every part built and checked, plus the design's own checks.
        The first part's fields also sit at the top level, as a single design always had them."""
        parts = []
        for p in state.parts:
            built = copy.deepcopy(self._build(p.params).payload)  # callers can't reach into the cache
            # A legacy id unique to this design, part and commit (the UI keys its status by it).
            built["id"] = f"{state.id}.{p.id}.{state.seq}"
            parts.append({**built, "part_id": p.id, "label": p.label, "quantity": p.quantity,
                          "piece_no": p.piece_no})
        design_checks = self._design_checks(state)
        statuses = [c for part in parts for c in part["checks"]] + [c.to_dict() for c in design_checks]
        result = {
            **parts[0],
            "design_id": state.id,
            "revision": state.revision,
            "seq": state.seq,
            "parts": parts,
            "groups": [{"id": g.id, "kind": g.kind,
                        "members": [{"ref": m.ref, "role": m.role, "quantity": m.quantity} for m in g.members],
                        **({"extra": dict(g.extra)} if g.extra else {})}
                       for g in state.groups],
            "links": [{"id": lk.id, "container": lk.container, "contained": lk.contained} for lk in state.links],
            "design_checks": [c.to_dict() for c in design_checks],
            "overall": overall([CheckResult(c["id"], c["status"], c["message"]) for c in statuses]),
        }
        return result

    def _design_checks(self, state: DesignState) -> list[CheckResult]:
        out: list[CheckResult] = []
        known = state.ids()
        for lk in state.links:
            missing = [r for r in (lk.container, lk.contained) if r not in known]
            if missing:
                out.append(CheckResult(f"link_{lk.id}", "stale",
                                       f"fit link {lk.id} points at {', '.join(missing)}, which is no longer in "
                                       "the design; relink it or remove it"))
        from . import fit  # partition sets, fit links and stacks (fit.py)

        out += fit.design_checks(state)
        return out

    def _build(self, params: BoxParams) -> _Built:
        key = json.dumps(params.model_dump(), sort_keys=True)
        with self._lock:
            hit = self._built.get(key)
            if hit is not None:
                self._built.move_to_end(key)
                return hit
        payload = _designs.create_box(params)  # validates and refuses invalid geometry
        d = _designs.build(params)
        payload["scene"] = scene(d)
        built = _Built(payload, d)
        with self._lock:
            self._built[key] = built
            while len(self._built) > 64:
                self._built.popitem(last=False)
        return built

    # ------------------------------------------------------------ changing

    def mutate(self, session: str, change: Callable[[DesignState | None], DesignState], *,
               expected: tuple[str, int] | None | str = "any", request_id: str | None = None,
               dry_run: bool = False, guard: Callable[[], None] | None = None,
               focus: str | None = None, origin: str = "chat",
               accept: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        """Build `change(current)` and commit it if the design is still `current`.

        expected: "any" (the designer's own form and job open) or the (design id, revision)
        the caller is working from; None means it expects no design yet.
        guard: called just before committing, under the lock; raising stops the commit
        (a chat turn that was stopped while its change was being built).
        accept: called with the built payload (also on a dry run); raising refuses the change
        (a feature the call added that can't be placed).
        """
        with self._lock:
            if request_id is not None and (session, request_id) in self._done:
                return copy.deepcopy(self._done[(session, request_id)])
            cur = self._by_session.get(session)
            seq = self._seq.get(session, 0)
        _check_expected(cur, expected)
        proposed = change(cur)
        if origin == "chat":
            _no_new_acks(cur, proposed)
        if cur is not None and proposed.id == cur.id:
            proposed = replace(proposed, revision=cur.revision + 1)
        proposed = replace(proposed, seq=seq + 1)
        payload = self.payload(proposed)  # builds and checks every part: raises before anything changes
        if focus is not None:
            payload["focus"] = focus  # the part the change was about, for a retry's answer
        if accept is not None:
            accept(payload)
        if dry_run:
            return {**payload, "seq": seq, "dry_run": True}
        with self._lock:
            if request_id is not None and (session, request_id) in self._done:
                return copy.deepcopy(self._done[(session, request_id)])  # an overlapping retry won
            now = self._by_session.get(session)
            if now is not cur or self._seq.get(session, 0) != seq:  # edited while this change was being built
                raise DesignChanged(_changed_msg())
            if guard is not None:
                guard()
            self._by_session[session] = proposed
            self._seq[session] = seq + 1
            if request_id is not None:
                self._done[(session, request_id)] = payload
                while len(self._done) > 256:
                    self._done.popitem(last=False)
            # Emitted under the lock, so the UI receives a session's designs in commit order.
            if self._emit is not None:
                # origin: "chat" for the agent's changes, "form" for the designer's own (the form,
                # a reopened job), whose replies the UI shows itself in its own request order.
                self._emit({"type": "design", "session_id": session, "design": payload, "origin": origin})
        return copy.deepcopy(payload)

    def forget(self, session: str) -> None:
        """A chat reset: its design and its retry keys go (the commit order carries on)."""
        with self._lock:
            self._by_session.pop(session, None)
            for key in [k for k in self._done if k[0] == session]:
                del self._done[key]


def _check_expected(cur: DesignState | None, expected: tuple[str, int] | None | str) -> None:
    if expected == "any":
        return
    if expected is None:
        if cur is not None:
            raise DesignChanged(_changed_msg())
        return
    if cur is None or (cur.id, cur.revision) != tuple(expected):  # type: ignore[arg-type]
        raise DesignChanged(_changed_msg())


def _changed_msg() -> str:
    return ("the design changed outside the chat (the user edited or replaced it); call view_design to see "
            "it, then try again")


# ------------------------------------------------------------------ preview scene


def scene(d: Dieline) -> list[dict[str, Any]]:
    """Every segment tagged with its operation, provenance (structure or
    feature), feature_id and face_id, for the preview to highlight and label
    (the CF2 stays clean for ArtiosCAD). A stale feature's ghost is tagged stale.
    A structure hinge's face is the face it hinges."""
    tags: dict[Any, dict[str, Any]] = d.extras.get("tags") or {}  # type: ignore[assignment]
    hinges = getattr(d.faces, "hinges", {}) or {}
    out = []
    for s in d.segments:
        op = d.ops.get(s.op) if s.op else None
        tag = tags.get(s)
        item: dict[str, Any] = {"type": s.type.value, "op": s.op, "kind": op.kind if op else None,
                                "provenance": "feature" if tag else "structure"}
        if tag:
            item.update(tag)
        elif s.op in hinges:
            item["face_id"] = hinges[s.op].child
        if isinstance(s, Arc):
            item.update(arc=True, start=list(s.start), end=list(s.end), center=list(s.center),
                        clockwise=s.clockwise, full_circle=s.full_circle)
        else:
            item.update(arc=False, start=list(s.start), end=list(s.end))
        out.append(item)
    return out

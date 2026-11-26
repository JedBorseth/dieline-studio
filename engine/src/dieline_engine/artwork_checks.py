"""Artwork checks (docs/DESIGN-SYSTEM.md, "Artwork checks" and "Suspicious artwork").

Geometry that can't exist never gets here: artwork that touches or crosses a
score, a slot, the outline or a feature, or leaves its face, is refused (or
stale after a resize). These are the checks on a committed design.

- **Tooling limits** (provisional, each a warning): bend radius from the source
  curvature, acute corners, short straights between corners, thin webs between
  cuts. Each produces **one** result with the count, the worst value and up to
  five sample locations, so a 500-segment logo can't flood the panel.
- **Placement:** closer than 1/4" to the face's edge, or in a keep-out zone.
- **Islands:** one result per artwork. An unhandled island is `fail`; a bridge,
  a partial cut or an accepted drop-out is reported (the last two change what's
  made, so they're explicit choices).
- **Suspicious artwork** (artwork is untrusted): a long straight parallel to a
  nearby hinge or edge, a slot-like rectangle near an edge, a path spanning
  more than 80% of its face, crease or perforation artwork longer than half the
  face's width. Each is `fail` until the **designer** acknowledges it in the
  checks panel (`ack` names what to acknowledge). The agent has no way to.
- Agent-drawn artwork is an info note everywhere it appears.
"""

from __future__ import annotations

import math
from typing import Any

from .artwork import EDGE_CLEARANCE_IN, PROVISIONAL, Art, Drawn, _bridge_min, _web_min
from .checks import CheckResult, fmt_in
from .faces import Face, FaceMap
from .features import _bounds
from .geometry import Arc, Dieline, Line, Operation, Point, Segment

BEND_MIN = 1 / 8
BEND_MIN_HEAVY = 3 / 16
ACUTE_DEG = 35.0
SHORT_MIN = 1 / 8
LONG_STRAIGHT = 1.0  # in: a straight this long next to a hinge or edge looks like a fold or slot
NEAR = 1.0  # in: "nearby"
PARALLEL_DEG = 3.0
SLOT_RATIO = 3.0
SLOT_WIDTH = 0.5
SPAN = 0.8
RULE_SPAN = 0.5
TOL = 1e-6


def _r(x: float) -> float:
    return round(x * 1024) / 1024


def _where(fm: FaceMap, art: Art, p: Point) -> str:
    f = fm.faces.get(art.face or "")
    if f is None:
        return f"{art.id}"
    q = f.from_flat(p)
    return f"{art.id} on {f.id} at ({fmt_in(_r(q[0]))}, {fmt_in(_r(q[1]))})"


def _agg(cid: str, label: str, samples: list[tuple[float, str, Art, Point]], unit: str, limit: str,
         worst_low: bool = True) -> CheckResult | None:
    """One result for a check: the count, the worst value and up to five places."""
    if not samples:
        return None
    samples.sort(key=lambda s: s[0] if worst_low else -s[0])
    worst = samples[0]
    places = []
    seen: set[str] = set()
    for _, where, _, _ in samples:
        if where not in seen:
            seen.add(where)
            places.append(where)
        if len(places) == 5:
            break
    arts = {s[2].id for s in samples}
    msg = (f"{len(samples)} {label} (worst {fmt_in(_r(worst[0])) if unit == 'in' else f'{worst[0]:.0f}'}"
           f"{' in' if unit == 'in' else ' deg'}; limit {limit}, provisional): " + "; ".join(places)
           + (" ..." if len(samples) > len(places) else ""))
    p = worst[3]
    region = (p[0] - 0.25, p[1] - 0.25, p[0] + 0.25, p[1] + 0.25)
    return CheckResult(cid, "warn", msg, "artwork", None, worst[2].id if len(arts) == 1 else None, region)


def _tangent(s: Segment, at_start: bool) -> Point:
    if isinstance(s, Line):
        d = (s.end[0] - s.start[0], s.end[1] - s.start[1])
    else:
        p = s.start if at_start else s.end
        r = (p[0] - s.center[0], p[1] - s.center[1])
        d = (r[1], -r[0]) if s.clockwise else (-r[1], r[0])
    n = math.hypot(*d) or 1.0
    return (d[0] / n, d[1] / n)


def _corners(loop: list[Segment], closed: bool) -> list[tuple[int, float]]:
    """(index of the segment starting at a corner, the angle there in degrees) for each true corner."""
    out = []
    n = len(loop)
    rng = range(n) if closed else range(1, n)
    for i in rng:
        a, b = loop[i - 1], loop[i]
        if a.end != b.start and math.dist(a.end, b.start) > 1e-5:
            continue
        ta, tb = _tangent(a, False), _tangent(b, True)
        turn = math.degrees(abs(math.atan2(ta[0] * tb[1] - ta[1] * tb[0], ta[0] * tb[0] + ta[1] * tb[1])))
        if turn > 1.0:
            out.append((i, 180.0 - turn))
    return out


def _seg_dist(a: list[Segment], b: list[Segment], limit: float) -> tuple[float, Point]:
    from .topology import _closest, _Piece  # noqa: PLC0415

    op = Operation("_", "cut")
    best = (math.inf, (0.0, 0.0))
    pa = [_Piece(s, op) for s in a]
    pb = [_Piece(s, op) for s in b]
    for p in pa:
        for q in pb:
            pad = min(best[0], limit)
            if p.box[0] - pad > q.box[2] or q.box[0] - pad > p.box[2] or \
                    p.box[1] - pad > q.box[3] or q.box[1] - pad > p.box[3]:
                continue
            dist, pt, _ = _closest(p, q)
            if dist < best[0]:
                best = (dist, pt)
    return best


def check_artwork(d: Dieline) -> list[CheckResult]:
    arts: list[Art] = d.extras.get("artwork") or []  # type: ignore[assignment]
    fm = d.faces
    if not arts or not isinstance(fm, FaceMap):
        return []
    meta = d.meta
    cal = float(meta.get("cal_in", 0.0))  # type: ignore[arg-type]
    pointage = float(meta.get("pointage", 2.0))  # type: ignore[arg-type]
    board = str(meta.get("board", ""))
    double_wall = len(board.split(" ")[1]) == 2 if " " in board else False
    acks = set(meta.get("acknowledged") or [])  # type: ignore[arg-type]
    out: list[CheckResult] = []
    live = [a for a in arts if a.status == "ok"]
    for a in arts:
        if a.status == "stale":
            out.append(CheckResult(f"artwork_{a.id}", "stale", f"{a.name} is stale: {a.reason}. It isn't drawn and "
                                   "blocks export until it's placed again or removed", "artwork", None, a.id))
    for a in live:
        if a.agent_drawn:
            out.append(CheckResult(f"artwork_{a.id}_agent_drawn", "info",
                                   f"agent-drawn artwork: {a.name}'s outline was sketched by the agent, not taken from "
                                   "the library or a file; verify its shape and size before cutting", "artwork", None,
                                   a.id, _bounds(a.segs())))
        for k, (status, msg) in enumerate(a.notes):
            out.append(CheckResult(f"artwork_{a.id}_note{k + 1}", status, msg, "artwork", None, a.id))
        out += _islands(a)
    # ---- tooling limits, one result each
    bend_lim = BEND_MIN_HEAVY if (pointage >= 3 and double_wall) else BEND_MIN
    bends: list[tuple[float, str, Art, Point]] = []
    acute: list[tuple[float, str, Art, Point]] = []
    short: list[tuple[float, str, Art, Point]] = []
    for a in live:
        for dr in a.live():
            if dr.rule == "annotation":
                continue
            tight = [(r, p) for r, p in dr.radii if r < bend_lim - TOL]
            for s in dr.segs:
                if isinstance(s, Arc) and dr.kinds.get(s, "arc") == "arc":
                    r = s.radius
                    if r < bend_lim - TOL:
                        tight.append((r, s.start))
            if tight:
                r, p = min(tight)
                bends.append((r, _where(fm, a, p), a, p))
            for loop in dr.loops:
                cs = _corners(loop, dr.closed)
                for i, ang in cs:
                    if ang < ACUTE_DEG - TOL:
                        acute.append((ang, _where(fm, a, loop[i].start), a, loop[i].start))
                at_corner = {i for i, _ in cs}
                n = len(loop)
                for i, s in enumerate(loop):
                    if not isinstance(s, Line) or dr.kinds.get(s, "line") not in ("line", "bridge"):
                        continue
                    nxt = (i + 1) % n if dr.closed else i + 1
                    ends = (i in at_corner or (not dr.closed and i == 0)) and \
                        (nxt in at_corner or (not dr.closed and i == n - 1))
                    if ends and s.length < SHORT_MIN - TOL:
                        short.append((s.length, _where(fm, a, s.start), a, s.start))
    for res in (
        _agg("artwork_bend_radius", f"curve{'s' if len(bends) != 1 else ''} tighter than the rule bends", bends,
             "in", f"{fmt_in(bend_lim)} in on the rule's centre line"),
        _agg("artwork_acute", f"corner{'s' if len(acute) != 1 else ''} under {ACUTE_DEG:g} deg (they pinch the "
             "dieboard)", acute, "deg", f"{ACUTE_DEG:g} deg"),
        _agg("artwork_short", f"straight{'s' if len(short) != 1 else ''} under {fmt_in(SHORT_MIN)} in between "
             "corners", short, "in", f"{fmt_in(SHORT_MIN)} in"),
        _thin_webs(d, live, fm, cal),
    ):
        if res is not None:
            out.append(res)
    # ---- placement: edge clearance and keep-outs
    edge: list[tuple[float, str, Art, Point]] = []
    keep: list[tuple[float, str, Art, Point]] = []
    for a in live:
        f = fm.faces.get(a.face or "")
        if f is None:
            continue
        segs = [s for s in a.segs() if s.type.value != "annotation"]
        if not segs:
            continue
        box = _face_box(f, _bounds(segs))
        gaps = {"left": box[0], "bottom": box[1], "right": f.width - box[2], "top": f.height - box[3]}
        side, g = min(gaps.items(), key=lambda kv: kv[1])
        if g < EDGE_CLEARANCE_IN - TOL:
            p = f.to_flat(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2))
            edge.append((g, f"{a.id} is {fmt_in(_r(g))} in from {f.id}'s {side} edge", a, p))
        for k in f.keepouts:
            x0, y0, x1, y1 = k.rect
            if min(box[2], x1) - max(box[0], x0) > TOL and min(box[3], y1) - max(box[1], y0) > TOL:
                p = f.to_flat(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2))
                depth = min(box[2], x1) - max(box[0], x0)
                what = "the glue landing zone" if k.kind == "glue_landing" else f"the {k.kind.replace('_', ' ')}"
                keep.append((depth, f"{a.id} enters {what} on {f.id} by {fmt_in(_r(depth))} in ({k.side})", a, p))
    for res in (_agg("artwork_edge", f"artwork closer than {fmt_in(EDGE_CLEARANCE_IN)} in to its face's edge", edge,
                     "in", f"{fmt_in(EDGE_CLEARANCE_IN)} in"),
                _agg("artwork_keepout", "artwork in a keep-out zone", keep, "in", "none", worst_low=False)):
        if res is not None:
            out.append(res)
    # ---- suspicious artwork
    for a in live:
        out += _flags(a, fm, acks)
    # ---- the read-back, and what's provisional
    if live:
        words = "; ".join(f"{a.name}: {a.words}" for a in live)
        out.append(CheckResult("artwork", "pass", f"{len(live)} artwork item{'s' if len(live) > 1 else ''} placed. "
                               f"{words}", "artwork"))
        out.append(CheckResult("artwork_limits", "warn", "artwork limits are provisional until the die maker confirms "
                               "them: " + "; ".join(PROVISIONAL.values()), "calibration"))
    return out


def _face_box(f: Face, flat: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    a = f.from_flat((flat[0], flat[1]))
    b = f.from_flat((flat[2], flat[3]))
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))


def _islands(a: Art) -> list[CheckResult]:
    if not a.islands:
        return []
    open_ = [i for i in a.islands if i.status in ("open", "failed")]
    done = [i for i in a.islands if i.status not in ("open", "failed")]
    words = {"bridged": "bridged", "stencil": "held by stencil breaks", "partial_cut": "kept by turning the hole's "
             "outer loop into a partial cut (an explicit choice: it changes what's made)",
             "drop_out": "accepted as a drop-out (an explicit choice: it falls out of the blank)"}
    out = []
    if open_:
        bw = fmt_in(_r(a.bridge_width))
        parts = [f"{i.id} (outline {i.loop}" + (f": {i.reason}" if i.reason else "") + ")" for i in open_]
        x0 = min(i.region[0] for i in open_)
        y0 = min(i.region[1] for i in open_)
        x1 = max(i.region[2] for i in open_)
        y1 = max(i.region[3] for i in open_)
        out.append(CheckResult(f"artwork_{a.id}_islands", "fail",
                               f"{a.name} leaves {len(open_)} island{'s' if len(open_) > 1 else ''} of board with "
                               f"nothing holding {'them' if len(open_) > 1 else 'it'}: {', '.join(parts)}. Choose for "
                               f"each (islands): bridge (two {bw} in corridors of board, across the flutes), stencil, "
                               "partial_cut (the hole's outer loop becomes a partial cut) or drop_out (it falls out). "
                               "Export is blocked until then", "artwork", None, a.id, (x0, y0, x1, y1)))
    if done:
        listed = "; ".join(f"{i.id} {words[i.status]}" + (f" ({i.bridges} x {fmt_in(_r(i.width))} in)" if i.bridges
                                                           else "") for i in done)
        out.append(CheckResult(f"artwork_{a.id}_islands_handled", "info", f"{a.name}: {listed}", "artwork", None,
                               a.id))
    return out


def _thin_webs(d: Dieline, live: list[Art], fm: FaceMap, cal: float) -> CheckResult | None:
    """Cuts closer than max(1/8, 2 x CAL): between artwork loops (a stencil glyph's own breaks and a
    bridge's corridor are checked as bridges instead) and from artwork to the features' cuts."""
    lim = _web_min(cal)
    samples: list[tuple[float, str, Art, Point]] = []
    by_face: dict[str, list[tuple[Art, Drawn, list[Segment]]]] = {}
    for a in live:
        for dr in a.live():
            if dr.rule not in ("cut", "perforation", "partial_cut"):
                continue
            for loop in dr.loops:
                segs = [s for s in loop if dr.kinds.get(s) != "bridge"]
                if segs:
                    by_face.setdefault(a.face or "", []).append((a, dr, segs))
    feats = []
    for b in d.extras.get("features") or []:
        if getattr(b, "status", "") == "ok":
            for c in b.copies:
                if c.segs:
                    feats.append((c.face, list(c.segs)))
    for face, items in by_face.items():
        boxes = [_bounds(s) for _, _, s in items]
        for i in range(len(items)):
            a, dr, segs = items[i]
            for j in range(i + 1, len(items)):
                b, dr2, segs2 = items[j]
                if a is b and dr.group is not None and dr.group == dr2.group:
                    continue  # a stencil glyph's breaks
                if a is b and dr is dr2:
                    continue  # one bridged outline's pieces: their gap is the bridge
                bi, bj = boxes[i], boxes[j]
                if bi[0] - lim > bj[2] or bj[0] - lim > bi[2] or bi[1] - lim > bj[3] or bj[1] - lim > bi[3]:
                    continue
                dist, p = _seg_dist(segs, segs2, lim)
                if TOL < dist < lim - TOL:
                    samples.append((dist, _where(fm, a, p) + (f" to {b.id}" if b is not a else ""), a, p))
            for fface, fsegs in feats:
                if fface != face:
                    continue
                dist, p = _seg_dist(segs, fsegs, lim)
                if TOL < dist < lim - TOL:
                    samples.append((dist, _where(fm, a, p) + " to a feature", a, p))
    return _agg("artwork_web", f"web{'s' if len(samples) != 1 else ''} of board thinner than the limit between cuts",
                samples, "in", f"max(1/8, 2 x CAL) = {fmt_in(_r(lim))} in")


def _flags(a: Art, fm: FaceMap, acks: set[str]) -> list[CheckResult]:
    f = fm.faces.get(a.face or "")
    if f is None:
        return []
    fp = a.fingerprint()
    found: dict[str, list[tuple[str, tuple[float, float, float, float]]]] = {}

    def flag(kind: str, msg: str, region: tuple[float, float, float, float]) -> None:
        found.setdefault(kind, []).append((msg, region))

    # Physical edges and hinges of the face, in its frame.
    edges = [(e.start, e.end, e.kind) for e in f.edges]
    for dr in a.live():
        for loop in dr.loops:
            # 1. a long straight parallel to a nearby hinge or edge
            for s in loop:
                if not isinstance(s, Line) or s.length < LONG_STRAIGHT - TOL or dr.kinds.get(s) == "bridge":
                    continue
                p0, p1 = f.from_flat(s.start), f.from_flat(s.end)
                u = ((p1[0] - p0[0]) / s.length, (p1[1] - p0[1]) / s.length)
                for e0, e1, kind in edges:
                    L = math.dist(e0, e1)
                    if L <= TOL:
                        continue
                    v = ((e1[0] - e0[0]) / L, (e1[1] - e0[1]) / L)
                    if abs(u[0] * v[1] - u[1] * v[0]) > math.sin(math.radians(PARALLEL_DEG)):
                        continue
                    nrm = (-v[1], v[0])
                    dist = abs((p0[0] - e0[0]) * nrm[0] + (p0[1] - e0[1]) * nrm[1])
                    t0 = (p0[0] - e0[0]) * v[0] + (p0[1] - e0[1]) * v[1]
                    t1 = (p1[0] - e0[0]) * v[0] + (p1[1] - e0[1]) * v[1]
                    if dist <= NEAR and max(t0, t1) > 0 and min(t0, t1) < L:
                        what = "hinge" if kind == "crease" else "edge"
                        flag("parallel", f"a {fmt_in(_r(s.length))} in straight {fmt_in(_r(dist))} in from and "
                                         f"parallel to {f.id}'s {what} (it looks like a fold or a slot)",
                             _bounds([s]))
                        break
            if not dr.closed:
                bx = _bounds(loop)
            else:
                bx = _bounds(loop)
            fb = _face_box(f, bx)
            w, h = fb[2] - fb[0], fb[3] - fb[1]
            # 2. a slot-like rectangle near an edge
            if dr.closed and dr.rule in ("cut",) and min(w, h) <= SLOT_WIDTH + TOL and \
                    max(w, h) >= SLOT_RATIO * max(min(w, h), 1e-9):
                gap = min(fb[0], fb[1], f.width - fb[2], f.height - fb[3])
                straight = sum(s.length for s in loop if isinstance(s, Line))
                total = sum(s.length for s in loop)
                if gap <= NEAR and straight >= 0.5 * total:
                    flag("slot", f"a slot-like {fmt_in(_r(w))} x {fmt_in(_r(h))} in cut {fmt_in(_r(gap))} in from "
                                 f"{f.id}'s edge (it looks like a slot)", bx)
            # 3. spanning most of the face
            if w > SPAN * f.width + TOL or h > SPAN * f.height + TOL:
                flag("span", f"an outline {fmt_in(_r(w))} x {fmt_in(_r(h))} in on the {fmt_in(_r(f.width))} x "
                             f"{fmt_in(_r(f.height))} in face {f.id} (over {SPAN:.0%} of it)", bx)
            # 4. crease or perforation artwork longer than half the face's width
            if dr.rule in ("crease", "perforation"):
                length = sum(s.length for s in loop)
                if length > RULE_SPAN * f.width + TOL:
                    flag("rule", f"a {dr.rule} {fmt_in(_r(length))} in long on {f.id}, over half its "
                                 f"{fmt_in(_r(f.width))} in width (it could act as a fold)", bx)
    out = []
    for kind, items in found.items():
        key = f"{a.id}:{kind}:{fp}"
        region = (min(r[0] for _, r in items), min(r[1] for _, r in items), max(r[2] for _, r in items),
                  max(r[3] for _, r in items))
        listed = "; ".join(m for m, _ in items[:5]) + (f" and {len(items) - 5} more" if len(items) > 5 else "")
        if key in acks:
            out.append(CheckResult(f"artwork_{a.id}_flag_{kind}", "info",
                                   f"suspicious artwork, acknowledged by the designer: {a.name}: {listed}", "artwork",
                                   None, a.id, region, ack=key, acknowledged=True))
        else:
            out.append(CheckResult(f"artwork_{a.id}_flag_{kind}", "fail",
                                   f"suspicious artwork: {a.name}: {listed}. Export is blocked until the designer "
                                   "acknowledges it in the checks panel", "artwork", None, a.id, region, ack=key))
    return out


def flag_keys(checks: list[dict[str, Any]]) -> set[str]:
    """The acknowledgement keys a design's checks offer right now."""
    return {c["ack"] for c in checks if c.get("ack")}

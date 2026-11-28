"""Feature checks (docs/DESIGN-SYSTEM.md, "Feature checks" and "Routes across faces").

Geometry that can't exist (a feature touching, crossing or overlapping another
or a score, or one outside its face) never gets here: it's refused, or the
feature is stale. These are the checks on a committed design: warnings and
notes, each naming the copy ("vent 3 of 4 on P3"), with the region to zoom to.

Every limit here is provisional until the shop or the die maker confirms it.
"""

from __future__ import annotations

import math
from typing import Any

from .checks import CheckResult, fmt_in
from .faces import Face, FaceMap, opposite_pairs
from .features import CLEARANCE_IN, PROVISIONAL, Built, Placed, _bounds
from .geometry import Dieline, Operation, Segment
from .rules import PERF_TIE, RuleSpec, cuts_on_creases, perf_cuts

TOL = 1e-6
Box = tuple[float, float, float, float]


def _face_box(f: Face, flat: Box) -> Box:
    """A flat bounding box in a face's frame (faces are axis-aligned, so this is exact)."""
    a = f.from_flat((flat[0], flat[1]))
    b = f.from_flat((flat[2], flat[3]))
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))


def _clip(box: Box, f: Face) -> Box | None:
    x0, y0, x1, y1 = max(box[0], 0.0), max(box[1], 0.0), min(box[2], f.width), min(box[3], f.height)
    return (x0, y0, x1, y1) if x1 > x0 - TOL and y1 > y0 - TOL else None


def _name(b: Built, c: Placed) -> str:
    return f"{b.name}" + (f" ({c.index} of {c.of} on {c.face})" if c.of > 1 else f" on {c.face}")


def _side_name(f: Face, side: str) -> str:
    ln = f.lines[side]
    for name, other in f.lines.items():
        if name not in ("top", "bottom", "left", "right") and other.start == ln.start and other.end == ln.end:
            return f"{f.id}'s {name.replace('_', ' ')}"
    return f"{f.id}'s {side} edge"


def _distance(a: list[Segment], b: list[Segment]) -> tuple[float, Any]:
    from .topology import _closest, _Piece  # noqa: PLC0415

    op = Operation("_", "cut")
    best = (math.inf, None)
    pa = [_Piece(s, op) for s in a]
    pb = [_Piece(s, op) for s in b]
    for p in pa:
        for q in pb:
            if p.box[0] - best[0] > q.box[2] or q.box[0] - best[0] > p.box[2] or \
                    p.box[1] - best[0] > q.box[3] or q.box[1] - best[0] > p.box[3]:
                continue
            dist, pt, _ = _closest(p, q)
            if dist < best[0]:
                best = (dist, pt)
    return best


def check_features(d: Dieline) -> list[CheckResult]:
    fm = d.faces
    feats: list[Built] = d.extras.get("features") or []  # type: ignore[assignment]
    if not isinstance(fm, FaceMap) or not feats:
        return []
    meta = d.meta
    cal = float(meta.get("cal_in", 0.0))  # type: ignore[arg-type]
    opts = meta.get("options") or {}
    flute = opts.get("flute", "vertical") if isinstance(opts, dict) else "vertical"
    closure = opts.get("closure", "inside") if isinstance(opts, dict) else "inside"
    use = opts.get("use") if isinstance(opts, dict) else None
    board_flute = str(meta.get("board", "")).split(" ")[1] if " " in str(meta.get("board", "")) else ""
    clearance = CLEARANCE_IN
    carry = max(1.0, 6 * cal)
    side_web = max(1.0, 4 * cal)
    out: list[CheckResult] = []
    used: set[str] = set()

    def add(cid: str, status: str, msg: str, b: Built, region: Box | None, *, cat: str = "feature") -> None:
        out.append(CheckResult(cid, status, msg, cat, None, b.id, region))

    live = [b for b in feats if b.status == "ok"]
    for b in feats:
        if b.status == "stale":
            out.append(CheckResult(f"feature_{b.id}", "stale", f"{b.name} is stale: {b.reason}. It isn't drawn and "
                                   "blocks export until it's placed again or removed", "feature", None, b.id))
    for b in live:
        for status, msg in b.notes:
            add(f"feature_{b.id}_note", status, msg, b, None)

    holes = [(b, c) for b in live if b.kind in ("handhole", "vent", "window", "hand_flap") for c in b.copies]
    # ---- clearance from the face's own edges (its scores, slots and cut edges)
    for b, c in holes:
        f = fm.faces[c.face]
        own = c.outline + c.extra.get("outer", [])
        flat = _bounds(own)
        fb = _face_box(f, flat)
        gaps = {"left": fb[0], "bottom": fb[1], "right": f.width - fb[2], "top": f.height - fb[3]}
        near = [(g, s) for s, g in gaps.items() if TOL < g < clearance - TOL]
        if near:
            g, s = min(near)
            used.add("clearance")
            add(f"feature_{b.id}_{c.face}_{c.index}_clearance", "warn",
                f"{_name(b, c)} is {fmt_in(_round(g))} in from {_side_name(f, s)} (clearance "
                f"{fmt_in(clearance)} in, provisional)", b, flat)
        # ---- handhole webs (body panels): to the score above, and to the scores either side
        if b.kind in ("handhole", "hand_flap") and f.kind == "body":
            used.add("carry_web")
            top_kind = f.lines["top"].kind
            tops = _bounds(c.segs + c.extra.get("outer", []))
            web = f.height - _face_box(f, tops)[3]
            if top_kind != "score":
                why = ("the open edge is up on an HSC tray" if use == "tray" else "its top is an open edge")
                add(f"feature_{b.id}_{c.face}_{c.index}_carry", "info",
                    f"{_name(b, c)} has no score above it ({why}); the carry web is {fmt_in(_round(web))} in to the "
                    "edge", b, flat)
            elif web < carry - TOL:
                add(f"feature_{b.id}_{c.face}_{c.index}_carry", "warn",
                    f"carry web above {_name(b, c)} is {fmt_in(_round(web))} in to {_side_name(f, 'top')}; under "
                    f"max(1, 6 x CAL) = {fmt_in(_round(carry))} in (provisional)", b, flat)
            used.add("side_web")
            for s in ("left", "right"):
                if gaps[s] < side_web - TOL:
                    add(f"feature_{b.id}_{c.face}_{c.index}_side_{s}", "warn",
                        f"side web from {_name(b, c)} to {_side_name(f, s)} is {fmt_in(_round(gaps[s]))} in; under "
                        f"max(1, 4 x CAL) = {fmt_in(_round(side_web))} in (provisional)", b, flat)
            # ---- combined ligament: the uncut board above it, once other features are taken out
            x0, x1 = fb[0], fb[2]
            hole_top = _face_box(f, tops)[3]
            blocking = []
            for b2 in live:
                for c2 in b2.copies:
                    if c2 is c:
                        continue
                    segs2 = c2.outline or c2.segs
                    if not segs2:
                        continue
                    for s2 in segs2:
                        fb2 = _clip(_face_box(f, _bounds([s2])), f)
                        if fb2 is None or fb2[2] <= x0 + TOL or fb2[0] >= x1 - TOL or fb2[1] < hole_top - TOL:
                            continue
                        blocking.append((fb2[1] - hole_top, b2, c2))
            if blocking:
                lig, b2, c2 = min(blocking, key=lambda t: t[0])
                if lig < min(carry, web) - TOL:
                    add(f"feature_{b.id}_{c.face}_{c.index}_ligament", "warn",
                        f"only {fmt_in(_round(lig))} in of board between {_name(b, c)} and {_name(b2, c2)} above it "
                        f"(combined ligament; the carry web needs {fmt_in(_round(carry))} in, provisional)", b, flat)
            # ---- flutes along the handhole's long side
            wide = (fb[2] - fb[0]) > (fb[3] - fb[1]) + TOL
            tall = (fb[3] - fb[1]) > (fb[2] - fb[0]) + TOL
            if (flute == "horizontal" and wide) or (flute == "vertical" and tall):
                add(f"feature_{b.id}_{c.face}_{c.index}_flute", "warn",
                    f"the flutes run along {_name(b, c)}'s long side, so the carry web is weak across them", b, flat)
    # ---- the glue landing zone and overlap bands
    for b in live:
        for c in b.copies:
            segs = c.segs or c.outline
            if not segs:
                continue
            through = b.kind != "score"
            for fid in (c.faces or [c.face]):
                f = fm.faces[fid]
                for k in f.keepouts:
                    if k.kind == "glue_landing" and (through or k.side == "outside"):
                        boxes = [_clip(_face_box(f, _bounds([s])), f) for s in segs]
                        hit = [bx for bx in boxes if bx is not None and _overlap(bx, k.rect)]
                        if not hit:
                            continue
                        x0 = min(bx[0] for bx in hit)
                        x1 = max(bx[2] for bx in hit)
                        depth = min(x1, k.rect[2]) - max(x0, k.rect[0])
                        width = k.rect[2] - k.rect[0]
                        add(f"feature_{b.id}_{fid}_{c.index}_glue", "warn",
                            f"{_name(b, c)} enters the {fmt_in(_round(width))} in glue zone on {fid} by "
                            f"{fmt_in(_round(depth))} in (the glue lug lands there, {k.side})", b, _bounds(segs))
                    elif k.kind == "overlap_band" and through and b.kind != "tear_strip":
                        fb = _face_box(f, _bounds(segs))
                        if not _overlap(fb, k.rect):
                            continue
                        opp = next((y if x == fid else x for x, y in opposite_pairs(fm) if fid in (x, y)), None)
                        matched = opp is not None and any(fid2 == opp for b2 in live for c2 in b2.copies
                                                          for fid2 in (c2.faces or [c2.face]))
                        if not matched:
                            add(f"feature_{b.id}_{fid}_{c.index}_overlap", "warn",
                                f"{_name(b, c)} is in the band {opp} covers when the box is closed, and {opp} has no "
                                "matching cut-out", b, _bounds(segs))
    # ---- features too close to each other (near misses)
    # Edge notches count against holes and each other: applying them only keeps 1/8 in on one edge,
    # and notches on two edges of a face (or a hole beside one) leave the same thin web.
    cutouts = holes + [(b, c) for b in live if b.kind == "notch" for c in b.copies]
    for i, (b, c) in enumerate(cutouts):
        for b2, c2 in cutouts[i + 1:]:
            if c.face != c2.face:
                continue
            dist, _ = _distance(c.outline + c.extra.get("outer", []), c2.outline + c2.extra.get("outer", []))
            if TOL < dist < clearance - TOL:
                used.add("clearance")
                add(f"feature_{b.id}_{b2.id}_{c.face}_{c.index}_{c2.index}_near", "warn",
                    f"{_name(b, c)} is {fmt_in(_round(dist))} in from {_name(b2, c2)} (clearance {fmt_in(clearance)} "
                    "in, provisional)", b, _bounds(c.outline))
    # ---- perforations
    for b in live:
        if b.kind not in ("perforation", "tear_strip"):
            continue
        for c in b.copies:
            r = c.extra.get("route")
            rule = next((op.rule for op in b.extra_ops if isinstance(op.rule, RuleSpec)
                         and op.rule.kind == "perforation"), None)
            if rule is None:
                continue
            stations = [s for s, _ in c.crossings]
            if rule.crossing == "continuous" and r is not None:
                pieces = perf_cuts(r.length, rule, stations)
                landed = cuts_on_creases(pieces, stations)
                if landed:
                    add(f"feature_{b.id}_cut_on_crease", "warn",
                        f"{b.name}: {len(landed)} of its cuts land on a crease it crosses (crossing: continuous); "
                        "use tie_at_creases to keep the creases whole", b, _bounds(c.segs))
            if cal >= 3 / 16 - 1e-9 and (rule.tie or PERF_TIE) < max(3 / 16, cal) - TOL:
                add(f"feature_{b.id}_ties", "warn",
                    f"{b.name}: ties of {fmt_in(_round(rule.tie or PERF_TIE))} in on {fmt_in(cal)} in board; cut as "
                    f"dashed cuts on the sample table, ties under {fmt_in(_round(max(3 / 16, cal)))} in let the "
                    "blade overcut the bottom liner", b, _bounds(c.segs))
    # ---- tear strips
    for b in live:
        if b.kind != "tear_strip":
            continue
        c = b.copies[0]
        w = c.extra["width"]
        region = _bounds(c.segs)
        if w < 0.5 - TOL:
            add(f"feature_{b.id}_width", "warn", f"{b.name} is {fmt_in(_round(w))} in wide; under 1/2 in a strip may "
                "snap", b, region)
        tab = c.extra["tab_at"]
        vertical = [h for h in fm.hinges.values() if h.parent.startswith("P") and h.child in ("P2", "P3", "P4",
                                                                                              "glue_lug")]
        for h in vertical:
            dist = _point_line(tab, h.start, h.end)
            if dist < 0.5 - TOL:
                add(f"feature_{b.id}_tab_score", "warn",
                    f"{b.name}'s pull tab is {fmt_in(_round(dist))} in from the {h.parent}/{h.child} score; keep it "
                    "1/2 in clear", b, region)
                break
        for fid in c.faces[:1]:
            f = fm.faces[fid]
            p = f.from_flat(tab)
            for k in f.keepouts:
                if k.kind == "glue_landing" and k.rect[0] - TOL <= p[0] <= k.rect[2] + TOL \
                        and k.rect[1] - TOL <= p[1] <= k.rect[3] + TOL:
                    msg = ("the glue lug covers it on the outside, so it can't be reached" if closure == "outside"
                           else "glue lands on it unless the glue pattern stops short of the strip")
                    add(f"feature_{b.id}_tab_glue", "warn", f"{b.name}'s pull tab is in the glue zone on {fid}: {msg}",
                        b, region)
            if fid == "glue_lug" and closure == "inside":
                add(f"feature_{b.id}_tab_reach", "warn", f"{b.name}'s pull tab is on the glue lug, inside the box "
                    "once it's glued: it can't be reached", b, region)
        if {"P1", "P4"} <= set(c.faces) and "glue_lug" not in c.faces:  # a body wrap, either way round
            add(f"feature_{b.id}_joint", "warn", f"{b.name} stops at the glue joint; the lug will block the tear "
                "there (glue_joint: cross carries it over the lug)", b, region)
        if c.extra["rails"] == "perf" and len(board_flute) == 2:
            add(f"feature_{b.id}_doublewall", "warn", f"{b.name}: a plain perforation strip on double-wall "
                f"({board_flute}) board may not tear cleanly", b, region)
        r = c.extra["route"]
        runs_vertical = abs(r.pieces[0][1][0] - r.pieces[0][0][0]) < abs(r.pieces[0][1][1] - r.pieces[0][0][1])
        f0 = fm.faces[c.faces[0]]
        along_y_face = abs(f0.y_axis[1]) > 0.5  # a body panel's frame y is flat y
        strip_vertical = runs_vertical == along_y_face
        if (flute == "vertical" and strip_vertical) or (flute == "horizontal" and not strip_vertical):
            add(f"feature_{b.id}_flute", "warn", f"{b.name} runs along the flutes; the tear may wander", b, region)
    # ---- the read-back, and what's provisional
    placed = "; ".join(f"{b.name}: {b.words}" for b in live)
    if live:
        out.append(CheckResult("features", "pass", f"{len(live)} feature{'s' if len(live) > 1 else ''} placed. "
                               f"{placed}", "feature"))
    used.add("clearance")
    if any(b.kind == "hand_flap" for b in live):
        used.add("hand_flap_join")
    if any(b.kind == "vent" for b in live):
        used.add("vent")
    if any(b.kind == "tear_strip" for b in live):
        used.add("tear_strip")
    if any(b.kind == "notch" for b in live):
        used.add("notch_corner")
    listed = "; ".join(PROVISIONAL[k] for k in PROVISIONAL if k in used)
    if live:
        out.append(CheckResult("feature_limits", "warn", "feature limits and defaults are provisional until the shop "
                               f"or the die maker confirms them: {listed}", "calibration"))
    return out


def _round(x: float) -> float:
    return round(x * 1024) / 1024


def _overlap(a: Box, b: Box) -> bool:
    return min(a[2], b[2]) - max(a[0], b[0]) > TOL and min(a[3], b[3]) - max(a[1], b[1]) > TOL


def _point_line(p, a, b) -> float:
    (ax, ay), (bx, by) = a, b
    L2 = (bx - ax) ** 2 + (by - ay) ** 2
    t = max(0.0, min(1.0, ((p[0] - ax) * (bx - ax) + (p[1] - ay) * (by - ay)) / L2))
    return math.dist(p, (ax + t * (bx - ax), ay + t * (by - ay)))

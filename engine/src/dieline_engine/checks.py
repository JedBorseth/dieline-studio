"""Checks run on every generated dieline (docs/PLAN.md, architecture step 4).

Implemented: geometry checks and the first manufacturing checks.
Not yet: fold simulation, comparison with ArtiosCAD references.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import asdict, dataclass

from .geometry import EPS, PRECISION, Arc, Dieline, Line, LineType, Point


@dataclass(frozen=True, slots=True)
class CheckResult:
    id: str
    status: str  # "pass" | "warn" | "fail"
    message: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SheetLimits:
    """Largest blank the corrugator/press/sample table takes, in mm. Placeholder."""

    max_width: float = 2500.0
    max_height: float = 1600.0


def _key(p: Point) -> tuple[float, float]:
    return (round(p[0], PRECISION - 1), round(p[1], PRECISION - 1))


def check_line_types(d: Dieline) -> CheckResult:
    bad = [s for s in d.segments if not isinstance(s.type, LineType)]
    if bad:
        return CheckResult("line_types", "fail", f"{len(bad)} segment(s) have an invalid line type")
    counts = Counter(s.type.value for s in d.segments)
    summary = ", ".join(f"{n} {t}" for t, n in sorted(counts.items()))
    return CheckResult("line_types", "pass", f"all line types valid ({summary})")


def check_zero_length(d: Dieline) -> CheckResult:
    n = sum(1 for s in d.segments if s.length <= EPS)
    if n:
        return CheckResult("zero_length", "fail", f"{n} zero-length segment(s)")
    return CheckResult("zero_length", "pass", "no zero-length segments")


def check_closed_outline(d: Dieline) -> CheckResult:
    """Every cut endpoint must join an even number of cut segments (closed loops)."""
    degree: Counter[tuple[float, float]] = Counter()
    for s in d.of_type(LineType.CUT):
        degree[_key(s.start)] += 1
        degree[_key(s.end)] += 1
    if not degree:
        return CheckResult("closed_outline", "fail", "no cut lines")
    open_ends = [p for p, n in degree.items() if n % 2]
    if open_ends:
        sample = ", ".join(f"({x:g}, {y:g})" for x, y in open_ends[:4])
        return CheckResult("closed_outline", "fail", f"{len(open_ends)} open cut end(s), e.g. {sample}")
    return CheckResult("closed_outline", "pass", "cut outline is closed")


def check_overlaps(d: Dieline) -> CheckResult:
    """No duplicate or overlapping collinear lines (any line type)."""
    lines = [s for s in d.segments if isinstance(s, Line) and s.type != LineType.ANNOTATION]
    groups: dict[tuple, list[tuple[float, float, Line]]] = {}
    for ln in lines:
        (x1, y1), (x2, y2) = ln.start, ln.end
        ang = math.atan2(y2 - y1, x2 - x1) % math.pi
        if round(ang, 6) >= round(math.pi, 6):  # fold the wrap-around back onto 0
            ang = 0.0
        offset = -x1 * math.sin(ang) + y1 * math.cos(ang)  # distance along the normal
        t1 = x1 * math.cos(ang) + y1 * math.sin(ang)  # position along the line
        t2 = x2 * math.cos(ang) + y2 * math.sin(ang)
        key = (round(ang, 6), round(offset, PRECISION - 1))
        groups.setdefault(key, []).append((min(t1, t2), max(t1, t2), ln))
    overlaps = 0
    for spans in groups.values():
        spans.sort(key=lambda s: s[0])
        reach = -math.inf
        for lo, hi, _ in spans:
            if lo < reach - EPS * 10:
                overlaps += 1
            reach = max(reach, hi)
    overlaps += _arc_overlaps([s for s in d.segments if isinstance(s, Arc) and s.type != LineType.ANNOTATION])
    if overlaps:
        return CheckResult("overlaps", "fail", f"{overlaps} duplicate or overlapping segment(s)")
    return CheckResult("overlaps", "pass", "no duplicate or overlapping segments")


def _arc_overlaps(arcs: list[Arc]) -> int:
    """Count pairs of arcs on the same circle whose swept intervals overlap."""
    circles: dict[tuple, list[tuple[float, float, float]]] = {}
    for a in arcs:
        sweep = a.sweep
        start = a.end if a.clockwise else a.start  # normalise to a CCW interval
        a0 = math.atan2(start[1] - a.center[1], start[0] - a.center[0]) % math.tau
        key = (_key(a.center), round(a.radius, PRECISION - 1))
        circles.setdefault(key, []).append((a0, abs(sweep), a.radius))
    count = 0
    for spans in circles.values():
        for i in range(len(spans)):
            for j in range(i + 1, len(spans)):
                a1, l1, r = spans[i]
                a2, l2, _ = spans[j]
                eps = EPS * 10 / max(r, EPS)
                d = (a2 - a1) % math.tau  # where interval j starts, relative to i
                if d < l1 - eps or d + l2 > math.tau + eps:
                    count += 1
    return count


def check_crossings(d: Dieline) -> CheckResult:
    """No two lines may cross through each other's interiors.

    T-junctions (a crease ending on a cut) and shared endpoints are fine; a
    crossing means a self-intersecting outline or a rule cutting through a
    panel. Arcs are not yet included.
    """
    lines = [s for s in d.segments if isinstance(s, Line) and s.type != LineType.ANNOTATION]
    crossings: list[Point] = []
    for i in range(len(lines)):
        (ax, ay), (bx, by) = lines[i].start, lines[i].end
        for j in range(i + 1, len(lines)):
            (cx, cy), (dx, dy) = lines[j].start, lines[j].end
            rx, ry, sx, sy = bx - ax, by - ay, dx - cx, dy - cy
            denom = rx * sy - ry * sx
            if abs(denom) < 1e-12:
                continue  # parallel or collinear: check_overlaps handles those
            t = ((cx - ax) * sy - (cy - ay) * sx) / denom
            u = ((cx - ax) * ry - (cy - ay) * rx) / denom
            tol_t = EPS * 10 / math.hypot(rx, ry)
            tol_u = EPS * 10 / math.hypot(sx, sy)
            if tol_t < t < 1 - tol_t and tol_u < u < 1 - tol_u:
                crossings.append((ax + t * rx, ay + t * ry))
    if crossings:
        sample = ", ".join(f"({x:.1f}, {y:.1f})" for x, y in crossings[:4])
        return CheckResult("crossings", "fail", f"{len(crossings)} crossing line(s), e.g. at {sample}")
    return CheckResult("crossings", "pass", "no lines cross")


def check_sheet_fit(d: Dieline, limits: SheetLimits = SheetLimits()) -> CheckResult:
    w, h = d.size()
    fits = w <= limits.max_width and h <= limits.max_height
    if not d.meta.get("corrugated", True):
        # Folding carton can be turned on the sheet; corrugated can't, because the
        # flute direction fixes the blank's orientation.
        fits = fits or (h <= limits.max_width and w <= limits.max_height)
    msg = f"blank {w:.1f} x {h:.1f} mm vs sheet limit {limits.max_width:g} x {limits.max_height:g} mm"
    return CheckResult("sheet_fit", "pass" if fits else "fail", msg)


def check_rsc_manufacturing(d: Dieline) -> list[CheckResult]:
    meta = d.meta
    if meta.get("style") != "fefco-0201":
        return []
    out: list[CheckResult] = []
    panels = meta["panels_mm"]  # type: ignore[index]
    gf = float(panels["glue_flap"])  # type: ignore[index]
    out.append(
        CheckResult(
            "glue_flap_width",
            "pass" if gf >= 30.0 else "fail",
            f"glue flap {gf:g} mm (minimum 30 mm)",
        )
    )
    dims = meta["inside_dims_mm"]  # type: ignore[index]
    smallest = min(float(dims["length"]), float(dims["width"]), float(dims["depth"]))  # type: ignore[index]
    out.append(
        CheckResult(
            "min_panel",
            "pass" if smallest >= 50.0 else "warn",
            f"smallest inside dimension {smallest:g} mm (RSC below 50 mm is hard to run)",
        )
    )
    # Every RSC flap is half the width deep, so the two inner flaps (on the width
    # panels) collide when their combined depth exceeds the length panel.
    flap = float(panels["flap_depth"])  # type: ignore[index]
    length_panel = float(panels["length_panel"])  # type: ignore[index]
    if 2 * flap > length_panel:
        out.append(
            CheckResult(
                "inner_flaps",
                "fail",
                f"inner flaps ({2 * flap:.1f} mm together) overlap across the {length_panel:.1f} mm length panel; "
                "RSC needs length >= width",
            )
        )
    else:
        out.append(CheckResult("inner_flaps", "pass", "inner flaps fit across the length panel"))
    if not meta.get("calibrated", False):
        out.append(
            CheckResult(
                "calibration",
                "warn",
                "allowances are placeholder rules, not yet calibrated against ArtiosCAD",
            )
        )
    return out


def run_checks(d: Dieline) -> list[CheckResult]:
    results = [
        check_line_types(d),
        check_zero_length(d),
        check_closed_outline(d),
        check_overlaps(d),
        check_crossings(d),
        check_sheet_fit(d),
    ]
    results += check_rsc_manufacturing(d)
    return results


def overall(results: list[CheckResult]) -> str:
    statuses = {r.status for r in results}
    return "fail" if "fail" in statuses else "warn" if "warn" in statuses else "pass"

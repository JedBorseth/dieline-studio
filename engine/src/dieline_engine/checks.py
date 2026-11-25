"""Checks run on every generated dieline (docs/PLAN.md, architecture step 4).

Implemented: geometry checks, the standards' own checks, and which of R&L's
machines can run the blank. Not yet: fold simulation, comparison with
ArtiosCAD references. No shop thresholds are invented here: a limit appears
only when it comes from the shop's standards or machines.
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
class Machine:
    """A machine a blank can run on. Sheet sizes in inches.

    With `feed` set, sizes are (along the feed, across the machine) and the blank
    can't be turned: its height (flaps-body-flaps) runs along the feed. Without
    it, sizes are the press's sheet limits and the blank may be turned either way.
    """

    name: str
    max_size: tuple[float, float]
    min_size: tuple[float, float] | None = None
    feed: bool = False
    skip_feed_along: float | None = None  # longer feed length allowed in skip-feed mode


# The slotter's rated sheet limits: feeding 1625 x 4600 mm, skip feeding 2200 x 4600 mm, minimum
# 450 x 650 mm, where the first figure is the feed direction. On a slotter the
# slots run with the feed, so the blank's height goes along it and its length
# (the panels) across the machine. The die presses use the sheet min/max fields
# of their ArtiosCAD die press definitions in ServerLib, not their names.
MACHINES: list[Machine] = [
    Machine("Printing slotter die-cutter", (1625 / 25.4, 4600 / 25.4), (450 / 25.4, 650 / 25.4),
            feed=True, skip_feed_along=2200 / 25.4),
    Machine("Flatbed 60 x 46", (46.0, 60.0), (10.0, 15.0)),
    Machine("Bausse 44 x 33", (33.0, 44.0), (16.0, 20.0)),
    Machine("Pioneer 120 x 100", (120.0, 100.0), (20.4375, 23.625)),
    Machine("Jumbo 66 x 187", (64.0, 178.0), (17.75, 25.0)),
    Machine("Jumbo 66 x 190", (65.25, 110.0), (23.0, 23.0)),
]


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


def _fit(w: float, h: float, m: Machine) -> str:
    """'fits', 'skip' (slotter skip-feed only), 'small' (under the minimum sheet,
    may still run multi-up) or 'large'. w = blank length, h = blank height."""
    tol = EPS  # generated endpoints are rounded to six decimals
    if m.feed:
        along, across = h, w
        (mx_al, mx_ac), (mn_al, mn_ac) = m.max_size, m.min_size or (0.0, 0.0)
        if across > mx_ac + tol:
            return "large"
        skip = along > mx_al + tol
        if skip and (m.skip_feed_along is None or along > m.skip_feed_along + tol):
            return "large"
        if along < mn_al - tol or across < mn_ac - tol:
            return "small"
        return "skip" if skip else "fits"
    mx_a, mx_b = m.max_size
    mn_a, mn_b = m.min_size or (0.0, 0.0)
    result = "large"
    for a, b in ((w, h), (h, w)):
        if a <= mx_a + tol and b <= mx_b + tol:
            if a >= mn_a - tol and b >= mn_b - tol:
                return "fits"
            result = "small"
    return result


def check_machines(d: Dieline, machines: list[Machine] = MACHINES) -> CheckResult:
    """Which machines take the one-up blank by sheet size alone.

    A first screen, never a reason to block an export (the file may be going to
    the sample table or to ArtiosCAD for a layout): trims, grippers, multi-up,
    slotter head spacing and the press's flute direction aren't modelled.
    """
    k = d.mm_per_unit / 25.4
    w, h = (v * k for v in d.size())
    fits = {m.name: _fit(w, h, m) for m in machines}
    size = f"blank {fmt_in(w)} x {fmt_in(h)} in"
    ok = [n for n, f in fits.items() if f == "fits"]
    skip = [n for n, f in fits.items() if f == "skip"]
    small = [n for n, f in fits.items() if f == "small"]
    if ok:
        extra = f"; {', '.join(skip)} in skip-feed" if skip else ""
        return CheckResult("machines", "pass", f"{size} is within the sheet limits of {', '.join(ok)}{extra} "
                                               "(trims and layout not checked)")
    if skip:
        return CheckResult("machines", "warn", f"{size} is within the sheet limits only on the "
                                               f"{', '.join(skip)} in skip-feed mode (trims and layout not checked)")
    if small:
        return CheckResult("machines", "warn", f"{size} is under the minimum sheet of {', '.join(small)}; "
                                               "run it multi-up or on the sample table")
    return CheckResult("machines", "warn", f"{size} is larger than every R&L machine's maximum sheet; "
                                           "it needs another route (or a two-piece design)")


def check_preflight(d: Dieline) -> list[CheckResult]:
    """Two checks from the shop's ArtiosCAD Preflight profile (ServerLib
    'Corrugated - Inch' parameter set) that the engine can evaluate itself."""
    meta = d.meta
    if "panels_in" not in meta:
        return []
    out: list[CheckResult] = []
    cal = float(meta["cal_in"])  # type: ignore[arg-type]
    p = [float(x) for x in meta["panels_in"]]  # type: ignore[union-attr]
    # Checks 10/11 "Width/Length panel difference": flag when matching panels
    # differ by more than CAL x 1 + 0.79375 mm (1/32").
    limit = cal + 1 / 32
    diffs = [abs(p[0] - p[2]), abs(p[1] - p[3])]
    if max(diffs) > limit + 1e-9:
        out.append(CheckResult(
            "preflight_panels", "warn",
            f"matching panels differ by {fmt_in(max(diffs))} in; Preflight flags more than CAL + 1/32 "
            f"({fmt_in(limit)} in)",
        ))
    else:
        out.append(CheckResult("preflight_panels", "pass", "matching panels within Preflight's CAL + 1/32"))
    # Checks 15/16 "Crease/Knife pointage allowed by caliper", corrugated rows:
    # 2 pt up to 4.7625 mm (3/16"), 3 pt from 0.79375 to 9.525 mm,
    # and 5 pt from 4.7625 to 25.4 mm.
    pointage = float(meta.get("pointage", 2))  # type: ignore[arg-type]
    if pointage == 2 and cal > 0.1875 + 1e-9:
        out.append(CheckResult(
            "preflight_pointage", "warn",
            f"2 pt rules on a {fmt_in(cal)} in board; Preflight allows 2 pt only up to 3/16 in. "
            "The standards draw 2 pt; confirm what the die maker wants",
        ))
    return out


def check_standard(d: Dieline) -> list[CheckResult]:
    """The checks the ArtiosCAD standard itself enforces, plus what's still unconfirmed."""
    meta = d.meta
    if "standard" not in meta:
        return []
    out: list[CheckResult] = []
    dims = meta["inside_in"]  # type: ignore[index]
    length, width = float(dims["length"]), float(dims["width"])  # type: ignore[index]
    if length >= width:
        out.append(CheckResult("length_width", "pass", "length >= width"))
    else:
        out.append(CheckResult("length_width", "fail",
                               f"Length must be >= Width ({meta['standard']} check); swap L and W"))
    cal = fmt_in(float(meta["cal_in"]))  # type: ignore[arg-type]
    il = fmt_in(float(meta["il_in"]))  # type: ignore[arg-type]
    source = meta.get("board_source")
    if source == "artioscad":
        out.append(CheckResult("board", "pass", f"{meta['board']} values from ArtiosCAD (CAL {cal}, IL {il})"))
    elif source == "derived":
        out.append(CheckResult(
            "board", "warn",
            f"{meta['board']} CAL {cal}, IL {il} inferred from real R&L designs; confirm in ArtiosCAD's board library",
        ))
    else:
        out.append(CheckResult(
            "board", "warn",
            f"{meta['board']} uses nominal CAL {cal}, IL {il}; confirm in ArtiosCAD's board library before production",
        ))
    rounded = meta.get("rounded_in") or {}
    between = {k: v for k, v in rounded.items() if abs(v * 16 - round(v * 16)) > 1e-9}  # type: ignore[union-attr]
    if between:
        listed = ", ".join(f"{k} {fmt_in(v)}" for k, v in between.items())
        out.append(CheckResult(
            "rounding", "warn",
            f"{listed} fall between sixteenths. {meta['standard']} rounds these to 1/16. "
            "Verify the resulting score and flap positions in ArtiosCAD.",
        ))
    elif rounded:
        out.append(CheckResult("rounding", "pass", "the standard's rounded values are already on 1/16"))
    if not meta.get("calibrated", False):
        out.append(CheckResult(
            "calibration", "warn",
            f"formulas copied from the {meta['standard']} standard; not yet verified by an ArtiosCAD rebuild",
        ))
    return out


def fmt_in(x: float) -> str:
    """Inches as a mixed fraction to the nearest 1/32 when exact, else decimals."""
    n = round(x * 32)
    if abs(x * 32 - n) > 1e-6:
        return f"{x:.4f}".rstrip("0").rstrip(".")
    whole, rem = divmod(n, 32)
    if rem == 0:
        return str(whole)
    num, den = rem, 32
    while num % 2 == 0:
        num, den = num // 2, den // 2
    return f"{whole} {num}/{den}" if whole else f"{num}/{den}"


def run_checks(d: Dieline) -> list[CheckResult]:
    results = [
        check_line_types(d),
        check_zero_length(d),
        check_closed_outline(d),
        check_overlaps(d),
        check_crossings(d),
        check_machines(d),
    ]
    results += check_standard(d)
    results += check_preflight(d)
    return results


def overall(results: list[CheckResult]) -> str:
    statuses = {r.status for r in results}
    return "fail" if "fail" in statuses else "warn" if "warn" in statuses else "pass"

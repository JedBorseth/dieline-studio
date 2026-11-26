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

from .geometry import EPS, Dieline, LineType
from .topology import Issue, Report, validate


# pass | info | warn | fail | not_applicable | unavailable | stale. A refused
# mutation is not a check: it never becomes a revision (docs/DESIGN-SYSTEM.md, "Checks").
STATUSES = ("pass", "info", "warn", "fail", "not_applicable", "unavailable", "stale")


@dataclass(frozen=True, slots=True)
class CheckResult:
    id: str
    status: str
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


def check_line_types(d: Dieline) -> CheckResult:
    bad = [s for s in d.segments if not isinstance(s.type, LineType)]
    if bad:
        return CheckResult("line_types", "fail", f"{len(bad)} segment(s) have an invalid line type")
    counts = Counter(s.type.value for s in d.segments)
    summary = ", ".join(f"{n} {t}" for t, n in sorted(counts.items()))
    return CheckResult("line_types", "pass", f"all line types valid ({summary})")


def _sample(issues: list[Issue], units: str) -> str:
    pts = [i.point for i in issues if i.point is not None][:3]
    where = ", ".join(f"({x:.4g}, {y:.4g})" for x, y in pts)
    first = issues[0].message
    more = f" and {len(issues) - 1} more" if len(issues) > 1 else ""
    return f"{first}{more}" + (f" at {where}" if where else "")


def check_topology(d: Dieline, report: Report | None = None) -> list[CheckResult]:
    """The contact and disposition rules (topology.py). Invalid geometry is a
    `fail` here; the design pipeline refuses it before it becomes a revision."""
    r = report or validate(d)
    out: list[CheckResult] = []
    groups = [
        ("zero_length", ("zero_length", "arc"), "no zero-length segments, arcs consistent"),
        ("closed_outline", ("open_end", "branch", "outline", "weld"), "cut outline is closed; open ends are all declared"),
        ("overlaps", ("overlap",), "no duplicate or overlapping segments"),
        ("crossings", ("crossing", "tangent", "contact"), "no undeclared crossings or contacts"),
    ]
    # Broken segments stop the analysis early: what it never reached is unknown, not passed.
    early = any(i.kind in ("zero_length", "arc", "finite") for i in r.invalid)
    for cid, kinds, ok in groups:
        if cid == "zero_length":
            kinds = (*kinds, "finite")
        issues = [i for i in r.invalid if i.kind in kinds]
        if issues:
            out.append(CheckResult(cid, "fail", _sample(issues, d.units)))
        elif early:
            out.append(CheckResult(cid, "unavailable", "not checked until the segments above are valid"))
        else:
            out.append(CheckResult(cid, "pass", ok))
    off = [i for i in r.invalid if i.kind == "off_board"]
    if off:
        out.append(CheckResult("material", "fail", _sample(off, d.units)))
    elif r.invalid:
        out.append(CheckResult("material", "unavailable", "not checked until the geometry above is valid"))
    elif r.islands:
        out.append(CheckResult("material", "fail", _sample(r.islands, d.units) +
                               "; bridge it, or accept that it drops out"))
    else:
        out.append(CheckResult("material", "pass", "the blank is one connected piece"))
    if r.near_misses:
        out.append(CheckResult("clearance", "warn", _sample(r.near_misses, d.units)))
    return out


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


def run_checks(d: Dieline, report: Report | None = None) -> list[CheckResult]:
    results = [check_line_types(d), *check_topology(d, report), check_machines(d)]
    results += check_standard(d)
    results += check_preflight(d)
    return results


def overall(results: list[CheckResult]) -> str:
    """fail if any check fails or is stale (export blocked); warn if any warns
    or couldn't run; otherwise pass. info and not_applicable never change it."""
    statuses = {r.status for r in results}
    if statuses & {"fail", "stale"}:
        return "fail"
    if statuses & {"warn", "unavailable"}:
        return "warn"
    return "pass"

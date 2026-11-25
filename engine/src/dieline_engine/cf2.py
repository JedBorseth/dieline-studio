"""CF2 (Common Format 2) writer and reader.

UNCONFIRMED CONVENTIONS. The line-type codes, the units keyword and the arc
direction flag below follow Kodak's published CF2 description. docs/CONTEXT.md
warns that codes vary between systems: confirm every value against a CF2
exported from our ArtiosCAD (docs/PLAN.md open questions) before relying on it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .geometry import Arc, Dieline, Line, LineType, Point, Segment

# LineType -> CF2 line-type code. UNCONFIRMED (see module docstring).
CF2_LINE_CODES: dict[LineType, int] = {
    LineType.CUT: 1,
    LineType.CREASE: 2,
    LineType.PERFORATION: 3,
    LineType.SCORE: 4,
}
CF2_CODE_TYPES: dict[int, LineType] = {v: k for k, v in CF2_LINE_CODES.items()}

# Rule thickness in printer's points written for every line.
DEFAULT_POINTAGE = 2.0

# Arc direction flag. UNCONFIRMED: assumed 1 = counter-clockwise, -1 = clockwise.
ARC_CCW = 1
ARC_CW = -1

UNITS_MM = "UM"
UNITS_INCH = "UI"


def _num(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _safe_name(name: str) -> str:
    # CF2 is comma-separated: keep names free of commas and line breaks.
    return "".join(ch if ch not in ",\r\n" else " " for ch in name).strip() or "design"


def write_cf2(dieline: Dieline, pointage: float = DEFAULT_POINTAGE) -> str:
    name = _safe_name(dieline.name)
    xmin, ymin, xmax, ymax = dieline.bounds()
    out = [
        "$BOF",
        "V2",
        "ORDER",
        name,
        "END",
        f"MAIN,{name}",
        UNITS_MM,
        f"LL,{_num(xmin)},{_num(ymin)}",
        f"UR,{_num(xmax)},{_num(ymax)}",
        "SCALE,1,1",
    ]
    pt = _num(pointage)
    for seg in dieline.segments:
        code = CF2_LINE_CODES.get(seg.type)
        if code is None:  # annotations are not manufactured; leave them out
            continue
        if isinstance(seg, Line):
            (x1, y1), (x2, y2) = seg.start, seg.end
            out.append(f"L,{pt},{code},0,{_num(x1)},{_num(y1)},{_num(x2)},{_num(y2)},0,0")
        else:
            (sx, sy), (ex, ey), (cx, cy) = seg.start, seg.end, seg.center
            direction = ARC_CW if seg.clockwise else ARC_CCW
            out.append(
                f"A,{pt},{code},0,{_num(sx)},{_num(sy)},{_num(ex)},{_num(ey)},"
                f"{_num(cx)},{_num(cy)},{direction},0,0"
            )
    out += ["END", "$EOF"]
    return "\r\n".join(out) + "\r\n"


@dataclass(slots=True)
class Cf2ReadResult:
    dieline: Dieline
    units: str
    warnings: list[str] = field(default_factory=list)
    raw_codes: dict[int, int] = field(default_factory=dict)  # code -> count


@dataclass(frozen=True, slots=True)
class _Xform:
    dx: float = 0.0
    dy: float = 0.0
    angle_deg: float = 0.0
    sx: float = 1.0
    sy: float = 1.0

    def apply(self, p: Point) -> Point:
        x, y = p[0] * self.sx, p[1] * self.sy
        a = math.radians(self.angle_deg)
        c, s = math.cos(a), math.sin(a)
        return (x * c - y * s + self.dx, x * s + y * c + self.dy)

    def then(self, outer: "_Xform") -> "_Xform":
        # Compose: apply self, then outer. Only exact for uniform scale, which is
        # what CF2 producers use in practice.
        dx, dy = outer.apply((self.dx, self.dy))
        return _Xform(dx, dy, self.angle_deg + outer.angle_deg, self.sx * outer.sx, self.sy * outer.sy)

    @property
    def mirrored(self) -> bool:
        return (self.sx < 0) != (self.sy < 0)


def read_cf2(text: str, max_depth: int = 16) -> Cf2ReadResult:
    """Parse CF2 text into a Dieline (millimetres). Unknown records are skipped."""
    blocks: dict[str, list[list[str]]] = {}
    main_name: str | None = None
    current: list[list[str]] | None = None
    units = UNITS_MM
    warnings: list[str] = []

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        fields = [f.strip() for f in line.split(",")]
        key = fields[0].upper()
        if key in ("MAIN", "SUB") and len(fields) > 1:
            name = fields[1]
            current = blocks.setdefault(name, [])
            if key == "MAIN":
                main_name = name
        elif key == "END":
            current = None
        elif current is not None:
            if key in (UNITS_MM, UNITS_INCH):
                units = key
            else:
                current.append(fields)

    if main_name is None:
        raise ValueError("not a CF2 file: no MAIN block")

    scale = 25.4 if units == UNITS_INCH else 1.0
    dieline = Dieline(name=main_name)
    raw_codes: dict[int, int] = {}

    def to_type(code_s: str) -> LineType:
        code = int(float(code_s))
        raw_codes[code] = raw_codes.get(code, 0) + 1
        return CF2_CODE_TYPES.get(code, LineType.ANNOTATION)

    def emit(name: str, xf: _Xform, depth: int) -> None:
        if depth > max_depth:
            warnings.append(f"subroutine nesting deeper than {max_depth} at {name!r}; skipped")
            return
        for f in blocks.get(name, []):
            key = f[0].upper()
            try:
                if key == "L" and len(f) >= 8:
                    t = to_type(f[2])
                    p1 = xf.apply((float(f[4]) * scale, float(f[5]) * scale))
                    p2 = xf.apply((float(f[6]) * scale, float(f[7]) * scale))
                    dieline.add_line(p1, p2, t)
                elif key == "A" and len(f) >= 11:
                    t = to_type(f[2])
                    s = xf.apply((float(f[4]) * scale, float(f[5]) * scale))
                    e = xf.apply((float(f[6]) * scale, float(f[7]) * scale))
                    c = xf.apply((float(f[8]) * scale, float(f[9]) * scale))
                    cw = int(float(f[10])) == ARC_CW
                    if xf.mirrored:
                        cw = not cw
                    dieline.segments.append(Arc(s, e, c, cw, t))
                elif key == "C" and len(f) >= 3:
                    sub = f[1]
                    if sub not in blocks:
                        warnings.append(f"call to undefined subroutine {sub!r}")
                        continue
                    nums = [float(v) for v in f[2:7]] + [0.0, 0.0, 0.0, 1.0, 1.0][len(f[2:7]) :]
                    inner = _Xform(nums[0] * scale, nums[1] * scale, nums[2], nums[3], nums[4])
                    emit(sub, inner.then(xf), depth + 1)
            except ValueError:
                warnings.append(f"unparseable record skipped: {','.join(f)}")

    emit(main_name, _Xform(), 0)
    return Cf2ReadResult(dieline=dieline, units=units, warnings=warnings, raw_codes=raw_codes)


def segments_equal(a: list[Segment], b: list[Segment], tol: float = 1e-3) -> bool:
    """Order-insensitive comparison of two segment lists (lines only, direction-insensitive)."""

    def key(s: Segment) -> tuple:
        if isinstance(s, Line):
            p, q = sorted([s.start, s.end])
            return ("L", s.type, round(p[0] / tol), round(p[1] / tol), round(q[0] / tol), round(q[1] / tol))
        return ("A", s.type, s.start, s.end, s.center, s.clockwise)

    return sorted(map(key, a)) == sorted(map(key, b))

"""CF2 (Common Format 2) writer and reader.

Line-type codes: R&L's CF2 import tuning (ServerLib `Artios_CFF2.ITUNE`, the
shop default) maps CF2 types 1, 2, 3 and 4 straight onto ArtiosCAD line types
1, 2, 3 and 4, and the shop's standards draw cuts as type 1 and creases as
type 2. So cut = 1 and crease = 2 are confirmed for import here; what 3 and 4
mean in our line-type set is not.

STILL UNCONFIRMED: the units keyword (UI/UM) and the arc direction flag follow
Kodak's published CF2 description. Confirm them with the first CF2 import test
on the ArtiosCAD workstation (docs/PLAN.md milestone 3).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .geometry import Arc, Dieline, Line, LineType, Point, Segment

# LineType -> CF2 line-type code (cut and crease confirmed; see module docstring).
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
    s = f"{v:.6f}".rstrip("0").rstrip(".")
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
        UNITS_INCH if dieline.units == "in" else UNITS_MM,
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
            direction = ARC_CW if seg.clockwise else ARC_CCW
            (cx, cy) = seg.center
            for (sx, sy), (ex, ey) in _arc_pieces(seg):
                out.append(
                    f"A,{pt},{code},0,{_num(sx)},{_num(sy)},{_num(ex)},{_num(ey)},"
                    f"{_num(cx)},{_num(cy)},{direction},0,0"
                )
    out += ["END", "$EOF"]
    return "\r\n".join(out) + "\r\n"


def _arc_pieces(a: Arc) -> list[tuple[Point, Point]]:
    """A full circle is written as two half arcs: an A record whose start and
    end coincide is read as nothing by some CF2 importers."""
    if not a.full_circle:
        if math.dist(a.start, a.end) <= 1e-9:
            raise ValueError("an arc whose start and end coincide must be flagged as a full circle")
        return [(a.start, a.end)]
    cx, cy = a.center
    opposite = (2 * cx - a.start[0], 2 * cy - a.start[1])
    return [(a.start, opposite), (opposite, a.start)]


@dataclass(slots=True)
class Cf2ReadResult:
    dieline: Dieline
    units: str
    warnings: list[str] = field(default_factory=list)
    raw_codes: dict[int, int] = field(default_factory=dict)  # code -> count


@dataclass(frozen=True, slots=True)
class _Affine:
    """x' = a*x + b*y + e,  y' = c*x + d*y + f"""

    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = 1.0
    e: float = 0.0
    f: float = 0.0

    @staticmethod
    def call(dx: float, dy: float, angle_deg: float, sx: float, sy: float) -> "_Affine":
        # CFF2 subroutine call: rotate, then scale/mirror, then translate.
        r = math.radians(angle_deg)
        cos, sin = math.cos(r), math.sin(r)
        return _Affine(sx * cos, -sx * sin, sy * sin, sy * cos, dx, dy)

    def apply(self, p: Point) -> Point:
        x, y = p
        return (self.a * x + self.b * y + self.e, self.c * x + self.d * y + self.f)

    def then(self, outer: "_Affine") -> "_Affine":
        """The transform that applies self first, then outer."""
        o = outer
        return _Affine(
            o.a * self.a + o.b * self.c,
            o.a * self.b + o.b * self.d,
            o.c * self.a + o.d * self.c,
            o.c * self.b + o.d * self.d,
            o.a * self.e + o.b * self.f + o.e,
            o.c * self.e + o.d * self.f + o.f,
        )

    @property
    def mirrored(self) -> bool:
        return self.a * self.d - self.b * self.c < 0

    @property
    def conformal(self) -> bool:
        """True if circles stay circles (rotation + uniform scale, optionally mirrored)."""
        n1 = math.hypot(self.a, self.c)
        n2 = math.hypot(self.b, self.d)
        dot = self.a * self.b + self.c * self.d
        return abs(n1 - n2) <= 1e-9 * max(n1, n2, 1.0) and abs(dot) <= 1e-9 * max(n1 * n2, 1.0)


def _float(v: str) -> float:
    if not v.strip():
        raise ValueError("empty number")
    return float(v)


MAX_SEGMENTS = 200_000


def read_cf2(text: str, max_depth: int = 16, max_segments: int = MAX_SEGMENTS) -> Cf2ReadResult:
    """Parse CF2 text into a Dieline (millimetres). Unknown records are skipped.

    The units keyword (UM/UI) is honoured wherever it appears, including the
    header. A block's SCALE,sx,sy record scales that block's own coordinates.
    """
    blocks: dict[str, list[list[str]]] = {}
    block_scale: dict[str, tuple[float, float]] = {}
    main_name: str | None = None
    current_name: str | None = None
    units: str | None = None
    warnings: list[str] = []

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        fields = [f.strip() for f in line.split(",")]
        key = fields[0].upper()
        if key in (UNITS_MM, UNITS_INCH):
            if units is not None and units != key:
                warnings.append(f"conflicting units {units} and {key}; using {units}")
            units = units or key
        elif key in ("MAIN", "SUB") and len(fields) > 1:
            current_name = fields[1]
            blocks.setdefault(current_name, [])
            if key == "MAIN":
                main_name = current_name
        elif key == "END":
            current_name = None
        elif current_name is not None:
            if key == "SCALE" and len(fields) >= 3:
                try:
                    block_scale[current_name] = (_float(fields[1]), _float(fields[2]))
                except ValueError:
                    warnings.append(f"unparseable record skipped: {line}")
            else:
                blocks[current_name].append(fields)

    if main_name is None:
        raise ValueError("not a CF2 file: no MAIN block")

    units = units or UNITS_MM
    unit = 25.4 if units == UNITS_INCH else 1.0
    dieline = Dieline(name=main_name)
    raw_codes: dict[int, int] = {}

    def to_type(code_s: str) -> LineType:
        code = int(_float(code_s))
        raw_codes[code] = raw_codes.get(code, 0) + 1
        return CF2_CODE_TYPES.get(code, LineType.ANNOTATION)

    def emit(name: str, xf: _Affine, depth: int) -> None:
        """xf maps this block's file coordinates to world millimetres."""
        if depth > max_depth:
            warnings.append(f"subroutine nesting deeper than {max_depth} at {name!r}; skipped")
            return
        bsx, bsy = block_scale.get(name, (1.0, 1.0))
        local = _Affine(bsx, 0.0, 0.0, bsy, 0.0, 0.0).then(xf)  # block SCALE, then placement
        for f in blocks.get(name, []):
            if len(dieline.segments) >= max_segments:
                if not truncated:
                    truncated.append(True)
                    warnings.append(f"stopped after {max_segments} segments (subroutines repeat too much)")
                return
            key = f[0].upper()
            try:
                if key == "L" and len(f) >= 8:
                    p1 = local.apply((_float(f[4]), _float(f[5])))
                    p2 = local.apply((_float(f[6]), _float(f[7])))
                    dieline.add_line(p1, p2, to_type(f[2]))
                elif key == "A" and len(f) >= 11:
                    s = local.apply((_float(f[4]), _float(f[5])))
                    e = local.apply((_float(f[6]), _float(f[7])))
                    c = local.apply((_float(f[8]), _float(f[9])))
                    cw = int(_float(f[10])) == ARC_CW
                    if local.mirrored:
                        cw = not cw
                    if not local.conformal:
                        warnings.append(
                            f"arc in {name!r} is scaled non-uniformly; drawn as a circular arc (approximate)"
                        )
                    full = math.dist(s, e) <= 1e-9
                    dieline.segments.append(Arc(s, s if full else e, c, cw, to_type(f[2]), full))
                elif key == "C" and len(f) >= 3:
                    sub = f[1]
                    if sub not in blocks:
                        warnings.append(f"call to undefined subroutine {sub!r}")
                        continue
                    defaults = [0.0, 0.0, 0.0, 1.0, 1.0]
                    n = [_float(f[2 + i]) if 2 + i < len(f) and f[2 + i] else defaults[i] for i in range(5)]
                    call = _Affine.call(n[0], n[1], n[2], n[3], n[4])
                    emit(sub, call.then(local), depth + 1)
            except ValueError:
                warnings.append(f"unparseable record skipped: {','.join(f)}")

    truncated: list[bool] = []
    emit(main_name, _Affine(unit, 0.0, 0.0, unit, 0.0, 0.0), 0)
    return Cf2ReadResult(dieline=dieline, units=units, warnings=warnings, raw_codes=raw_codes)


def segments_equal(a: list[Segment], b: list[Segment], tol: float = 1e-3) -> bool:
    """Order-insensitive comparison of two segment lists (lines only, direction-insensitive)."""

    def key(s: Segment) -> tuple:
        if isinstance(s, Line):
            p, q = sorted([s.start, s.end])
            return ("L", s.type, round(p[0] / tol), round(p[1] / tol), round(q[0] / tol), round(q[1] / tol))
        return ("A", s.type, s.start, s.end, s.center, s.clockwise)

    return sorted(map(key, a)) == sorted(map(key, b))

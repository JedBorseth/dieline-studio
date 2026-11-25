"""DXF R12 writer in the format R&L sends to the AOLCUT sample table.

Matches the shop's own ArtiosCAD DXF exports:
AutoCAD R12 (AC1009), inch coordinates, CRLF line endings, one layer named
`Design`, and the line type carried in the entity colour, which is how
ArtiosCAD's "Artios DXF - Inch" tuning maps it back on import:

    colour 1 = cut, colour 2 = crease

Perforation and score have no confirmed colour on the sample table, so the
writer refuses them rather than guess (the current styles never produce them).
"""

from __future__ import annotations

import math

from .geometry import Arc, Dieline, Line, LineType

LAYER = "Design"
DXF_COLOURS: dict[LineType, int] = {
    LineType.CUT: 1,
    LineType.CREASE: 2,
}


def _code(code: int, value: object) -> str:
    return f"{code:>3}\r\n{value}\r\n"


def _num(v: float) -> str:
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def write_dxf(dieline: Dieline) -> str:
    """The dieline as DXF R12 text, in inches."""
    k = 1.0 if dieline.units == "in" else 1 / 25.4
    manufactured = [s for s in dieline.segments if s.type != LineType.ANNOTATION]
    unsupported = sorted({s.type.value for s in manufactured if s.type not in DXF_COLOURS})
    if unsupported:
        raise ValueError(f"DXF export has no sample-table colour for: {', '.join(unsupported)}")
    xmin, ymin, xmax, ymax = dieline.bounds()

    out = [
        _code(0, "SECTION"), _code(2, "HEADER"),
        _code(9, "$ACADVER"), _code(1, "AC1009"),
        _code(9, "$EXTMIN"), _code(10, _num(xmin * k)), _code(20, _num(ymin * k)),
        _code(9, "$EXTMAX"), _code(10, _num(xmax * k)), _code(20, _num(ymax * k)),
        _code(0, "ENDSEC"),
        _code(0, "SECTION"), _code(2, "TABLES"),
        _code(0, "TABLE"), _code(2, "LAYER"), _code(70, 1),
        _code(0, "LAYER"), _code(2, LAYER), _code(70, 64), _code(62, 7), _code(6, "CONTINUOUS"),
        _code(0, "ENDTAB"),
        _code(0, "ENDSEC"),
        _code(0, "SECTION"), _code(2, "ENTITIES"),
    ]
    for s in manufactured:
        colour = DXF_COLOURS[s.type]
        head = [_code(8, LAYER), _code(6, "CONTINUOUS"), _code(62, colour)]
        if isinstance(s, Line):
            (x1, y1), (x2, y2) = s.start, s.end
            out += [_code(0, "LINE"), *head,
                    _code(10, _num(x1 * k)), _code(20, _num(y1 * k)),
                    _code(11, _num(x2 * k)), _code(21, _num(y2 * k))]
        elif isinstance(s, Arc) and math.dist(s.start, s.end) <= 1e-9:
            # A full circle (hole): an ARC with equal angles is degenerate in DXF.
            cx, cy = s.center
            out += [_code(0, "CIRCLE"), *head,
                    _code(10, _num(cx * k)), _code(20, _num(cy * k)), _code(40, _num(s.radius * k))]
        elif isinstance(s, Arc):
            # DXF arcs always run counter-clockwise from the start angle to the end angle.
            a, b = (s.end, s.start) if s.clockwise else (s.start, s.end)
            cx, cy = s.center
            start = math.degrees(math.atan2(a[1] - cy, a[0] - cx)) % 360
            end = math.degrees(math.atan2(b[1] - cy, b[0] - cx)) % 360
            out += [_code(0, "ARC"), *head,
                    _code(10, _num(cx * k)), _code(20, _num(cy * k)),
                    _code(40, _num(s.radius * k)),
                    _code(50, _num(start)), _code(51, _num(end))]
    out += [_code(0, "ENDSEC"), _code(0, "EOF")]
    return "".join(out)


# ------------------------------------------------------------------ reading

MAX_ENTITIES = 200_000
# $INSUNITS code -> (dieline units, factor into them)
INSUNITS = {1: ("in", 1.0), 2: ("in", 12.0), 4: ("mm", 1.0), 5: ("mm", 10.0), 6: ("mm", 1000.0)}


def _header_int(pairs: list[tuple[str, str]], name: str) -> int | None:
    for i, (code, value) in enumerate(pairs):
        if code == "9" and value == name and i + 1 < len(pairs):
            try:
                return int(pairs[i + 1][1])
            except ValueError:
                return None
    return None


def read_dxf(text: str, units: str = "in") -> tuple[Dieline, list[str]]:
    """Read LINE, ARC and CIRCLE entities from a DXF (R12 or later) for viewing.

    Colours follow the shop's DXF import tuning: 1 = cut, 2 = crease. Anything
    else (other colours, dimensions, text, hatches) becomes annotation, so it
    shows but is never counted as rule. Units can't be read reliably from R12
    files; the shop's DXFs are inches.
    """
    if text.startswith("AutoCAD Binary DXF"):
        raise ValueError("this is a binary DXF; export it from ArtiosCAD as an ASCII (R12) DXF")
    pairs: list[tuple[str, str]] = []
    lines = text.splitlines()
    for i in range(0, len(lines) - 1, 2):
        pairs.append((lines[i].strip(), lines[i + 1].strip()))
    layer_colour: dict[str, int] = {}
    entities: list[dict[str, str]] = []
    skipped: dict[str, int] = {}
    section = None
    current: dict[str, str] | None = None
    in_layer = False
    layer_name = None
    for code, value in pairs:
        if code == "0":
            if current is not None:
                entities.append(current)
                current = None
            if value == "SECTION":
                section = None
            elif value == "ENDSEC":
                section = None
            in_layer = value == "LAYER" and section == "TABLES"
            layer_name = None
            if section == "ENTITIES" and value in ("LINE", "ARC", "CIRCLE"):
                current = {"_type": value}
                if len(entities) >= MAX_ENTITIES:
                    current = None
                    skipped["(over the entity limit)"] = skipped.get("(over the entity limit)", 0) + 1
            elif section == "ENTITIES" and value not in ("SEQEND", "VERTEX", "ENDSEC", "EOF", "SECTION"):
                skipped[value] = skipped.get(value, 0) + 1
            continue
        if code == "2" and section is None:
            section = value
            continue
        if in_layer:
            if code == "2":
                layer_name = value.casefold()
            elif code == "62" and layer_name is not None:
                try:
                    layer_colour[layer_name] = abs(int(value))
                except ValueError:
                    pass
            continue
        if current is not None and code not in current:
            current[code] = value
    if current is not None:
        entities.append(current)

    types = {1: LineType.CUT, 2: LineType.CREASE}
    warnings: list[str] = []
    # Units come only from $INSUNITS (R2000+). $MEASUREMENT is a drawing
    # convention (hatch patterns, linetypes), not a scale, so it's ignored.
    insunits = _header_int(pairs, "$INSUNITS")
    factor = 1.0
    if insunits in INSUNITS:
        units, factor = INSUNITS[insunits]
    elif insunits not in (None, 0):
        warnings.append(f"unsupported $INSUNITS code {insunits}; read as {units}")
    d = Dieline(name="dxf", units=units)
    for e in entities:
        try:
            colour = int(e.get("62", 256))
            if colour == 256:  # BYLAYER, whether explicit or omitted
                colour = layer_colour.get(e.get("8", "0").casefold(), 7)
            t = types.get(abs(colour), LineType.ANNOTATION)
            if e["_type"] == "LINE":
                d.add_line((float(e["10"]) * factor, float(e["20"]) * factor),
                           (float(e["11"]) * factor, float(e["21"]) * factor), t)
            else:
                cx, cy, r = float(e["10"]) * factor, float(e["20"]) * factor, float(e["40"]) * factor
                a0 = math.radians(float(e.get("50", 0))) if e["_type"] == "ARC" else 0.0
                a1 = math.radians(float(e.get("51", 360))) if e["_type"] == "ARC" else 0.0
                start = (cx + r * math.cos(a0), cy + r * math.sin(a0))
                end = (cx + r * math.cos(a1), cy + r * math.sin(a1))
                d.segments.append(Arc(start, end, (cx, cy), False, t))
        except (KeyError, ValueError):
            warnings.append(f"skipped an unreadable {e['_type']}")
    if units == "in" and insunits in (None, 0) and d.segments:
        w, h = d.size()
        if max(w, h) > 300:  # no R&L blank is 25 feet long: almost certainly millimetres
            d.units = "mm"
            warnings.append("no units in the file; sizes over 300 look like millimetres, so read as mm")
    if not insunits and not any("no units" in warning for warning in warnings):
        warnings.append(f"no units in the file; read as {d.units}")
    if skipped:
        listed = ", ".join(f"{n} {k}" for k, n in sorted(skipped.items(), key=lambda kv: -kv[1]))
        warnings.append(f"not shown (only LINE, ARC and CIRCLE are read): {listed}")
    if not any(seg.type in (LineType.CUT, LineType.CREASE) for seg in d.segments):
        warnings.append("no cut (colour 1) or crease (colour 2) lines found: this may not be a dieline export")
    return d, warnings

"""Render a dieline to a PNG the agent can look at, and list its geometry.

Read-only views of the deterministic engine's output: the agent uses them to
check its work (does the box look like what was asked, are the scores where
they should be). It never writes coordinates; geometry still comes only from
the styles.

The ruler and labels use the dieline's coordinate units. Panel and flap
sizes are labelled when the design has axis-aligned creases.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

from .geometry import Arc, Dieline, Line, LineType, Segment

MAX_SIDE = 1500  # px; keeps one image around 1-2k input tokens
MARGIN = 90  # room for the rulers and the right-hand height labels
LEGEND = 52  # two lines of legend under the drawing

COLOURS = {
    LineType.CUT: (20, 20, 20),
    LineType.CREASE: (30, 90, 220),
    LineType.PERFORATION: (220, 120, 0),
    LineType.SCORE: (0, 150, 80),
    LineType.ANNOTATION: (150, 150, 150),
}
DASHED = {LineType.CREASE, LineType.PERFORATION, LineType.SCORE}


@dataclass(frozen=True, slots=True)
class Region:
    """A window in the design's own units (inches for inch designs)."""

    x0: float
    y0: float
    x1: float
    y1: float

    @classmethod
    def parse(cls, value: object) -> "Region | None":
        if value is None:
            return None
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            raise ValueError("region must be [x0, y0, x1, y1]")
        try:
            x0, y0, x1, y1 = (float(v) for v in value)
        except (TypeError, ValueError):
            raise ValueError("region must be four numbers") from None
        if not all(math.isfinite(v) for v in (x0, y0, x1, y1)):
            raise ValueError("region must be finite")
        if x1 <= x0 or y1 <= y0:
            raise ValueError("region must have x1 > x0 and y1 > y0")
        return cls(x0, y0, x1, y1)


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except Exception:  # very old Pillow or no FreeType: bitmap font
        return ImageFont.load_default()


def _fmt(x: float, units: str) -> str:
    if units == "mm":
        return f"{x:.4f}".rstrip("0").rstrip(".")
    from .checks import fmt_in

    rounded = round(abs(x) * 1024) / 1024
    return ("-" if x < 0 and rounded else "") + fmt_in(rounded)


def _nice_step(span: float, target_ticks: int = 12) -> float:
    raw = span / target_ticks
    for step in (0.0625, 0.125, 0.25, 0.5, 1, 2, 3, 6, 12, 24, 48):
        if step >= raw:
            return step
    base = 10 ** math.floor(math.log10(raw))
    return next(base * step for step in (1, 2, 5, 10) if base * step >= raw)


def clip_line(a: tuple[float, float], b: tuple[float, float], win: Region) -> tuple | None:
    """Liang-Barsky: the part of segment a-b inside the window, or None."""
    (x0, y0), (x1, y1) = a, b
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0 - win.x0), (dx, win.x1 - x0), (-dy, y0 - win.y0), (dy, win.y1 - y0)):
        if abs(p) < 1e-15:
            if q < -1e-9:
                return None
            continue
        t = q / p
        if p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1 + 1e-12:
            return None
    return ((x0 + t0 * dx, y0 + t0 * dy), (x0 + t1 * dx, y0 + t1 * dy))


def _arc_points(s: Arc, n: int = 48) -> list[tuple[float, float]]:
    a0 = math.atan2(s.start[1] - s.center[1], s.start[0] - s.center[0])
    sweep = math.tau if math.dist(s.start, s.end) <= 1e-9 else s.sweep
    return [(s.center[0] + s.radius * math.cos(a0 + sweep * k / n),
             s.center[1] + s.radius * math.sin(a0 + sweep * k / n)) for k in range(n + 1)]


def touches(s: Segment, win: Region) -> bool:
    """True if any part of the segment lies inside the window."""
    if isinstance(s, Line):
        return clip_line(s.start, s.end, win) is not None
    pts = _arc_points(s)
    return any(clip_line(p, q, win) is not None for p, q in zip(pts, pts[1:]))


def _annotation_only(d: Dieline) -> bool:
    return all(segment.type == LineType.ANNOTATION for segment in d.segments)


def render_png(d: Dieline, region: Region | None = None, title: str | None = None) -> tuple[bytes, dict]:
    """PNG bytes plus a small dict describing the view (units, window, scale)."""
    xmin, ymin, xmax, ymax = d.bounds(include_annotations=_annotation_only(d))
    if not d.segments:
        raise ValueError("nothing to draw: the design has no lines")
    if region is None and (xmax - xmin <= 1e-9 or ymax - ymin <= 1e-9):
        raise ValueError("nothing to draw: the lines have no width or height")
    if region is None:
        win = Region(xmin, ymin, xmax, ymax)
    else:
        win = region
    span_x = max(win.x1 - win.x0, 1e-6)
    span_y = max(win.y1 - win.y0, 1e-6)
    scale = min((MAX_SIDE - 2 * MARGIN) / span_x, (MAX_SIDE - 2 * MARGIN - LEGEND) / span_y)
    w = max(int(span_x * scale) + 2 * MARGIN, 640)  # wide enough for the legend
    h = int(span_y * scale) + 2 * MARGIN + LEGEND

    img = Image.new("RGB", (w, h), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    small, normal = _font(13), _font(16)

    def px(p: tuple[float, float]) -> tuple[float, float]:
        return (MARGIN + (p[0] - win.x0) * scale, MARGIN + (win.y1 - p[1]) * scale)

    units = d.units
    _ruler(draw, win, scale, w, h - LEGEND, small, units)
    # Lines go on their own layer, clipped to the window, so a zoomed view never
    # spills into the rulers.
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ldraw = ImageDraw.Draw(layer)
    _artwork_fill(ldraw, d, px)
    # Clip to the window (plus a pixel of slack) before drawing, so a tight zoom
    # never walks the off-screen length of long dashed lines.
    slack = 4 / scale
    view = Region(win.x0 - slack, win.y0 - slack, win.x1 + slack, win.y1 + slack)
    for s in sorted(d.segments, key=lambda s: s.type == LineType.CUT):  # cuts on top
        if isinstance(s, Line):
            part = clip_line(s.start, s.end, view)
            if part is not None:
                _draw_segment(ldraw, Line(part[0], part[1], s.type), px, scale)
        elif touches(s, view):
            _draw_segment(ldraw, s, px, scale)
    frame = (MARGIN - 3, MARGIN - 3, w - MARGIN + 3, h - LEGEND - MARGIN + 3)
    clipped = layer.crop(frame)
    img.paste(clipped, frame[:2], clipped)
    if region is not None:
        draw.rectangle([MARGIN, MARGIN, w - MARGIN, h - LEGEND - MARGIN], outline=(200, 200, 200))

    _artwork_marks(draw, d, px, small)
    show_labels = region is None and not _annotation_only(d)
    if show_labels and d.meta.get("style") == "partition-slotted":
        _partition_labels(draw, d, px, small, normal)
    elif show_labels:
        _labels(draw, d, px, small, normal)

    others = sorted({seg.type.value for seg in d.segments} - {"cut", "crease"})
    key = "black = cut, blue dashed = crease" + (
        ", orange = perforation" if "perforation" in others else "") + (
        ", green = score" if "score" in others else "") + (
        ", grey = annotation" if "annotation" in others else "")
    if title is None and d.meta.get("quantity_per_set"):
        title = f"{d.name}   |   x {d.meta['quantity_per_set']} per set, not calibrated"
    line1 = f"{title or d.name}   |   {key}"
    line2 = (f"window x {_fmt(win.x0, units)}..{_fmt(win.x1, units)}, y {_fmt(win.y0, units)}..{_fmt(win.y1, units)} "
             f"{units}   |   {scale:.1f} px/{units}")
    if show_labels and d.meta.get("style") == "partition-slotted":
        line2 += "   |   labels: end or slot centre to slot centre; slot width x depth"
    elif show_labels:
        line2 += "   |   labels: distances between scores and edges"
    draw.text((10, h - LEGEND + 6), _fit(draw, line1, small, w - 20), fill=(60, 60, 60), font=small)
    draw.text((10, h - LEGEND + 26), _fit(draw, line2, small, w - 20), fill=(60, 60, 60), font=small)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    info = {
        "units": units,
        "window": [round(win.x0, 4), round(win.y0, 4), round(win.x1, 4), round(win.y1, 4)],
        "px_per_unit": round(scale, 3),
        "image_px": [w, h],
        "origin": "native design coordinates, x right and y up",
    }
    return buf.getvalue(), info


def _loop_points(loop) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for s in loop:
        pts += _arc_points(s, 24)[:-1] if isinstance(s, Arc) else [s.start]
    return pts


def _artwork_fill(draw, d: Dieline, px) -> None:
    """Artwork's discarded board shaded, retained board (islands too) left white, so the
    picture shows what the cuts will actually remove."""
    arts = d.extras.get("artwork") or []
    if not arts:
        return
    from .artwork import _face_loops, _nest  # noqa: PLC0415

    faces = {a.face for a in arts if a.status == "ok" and a.face}
    for face in faces:
        loops = _face_loops(arts, d, face)
        _nest(loops)
        for lp in sorted(loops, key=lambda x: x.depth):
            if lp.art is None:
                continue
            fill = (255, 214, 214, 255) if lp.depth % 2 == 1 else (255, 255, 255, 255)
            pts = [px(p) for p in _loop_points(lp.segs)]
            if len(pts) >= 3:
                draw.polygon(pts, fill=fill)


def _artwork_marks(draw, d: Dieline, px, font) -> None:
    """Agent-drawn artwork in a dashed frame labelled as such; islands nothing holds, circled."""
    arts = d.extras.get("artwork") or []
    accent = (200, 60, 0)
    for a in arts:
        if a.status != "ok":
            continue
        segs = a.segs()
        if not segs:
            continue
        from .features import _bounds  # noqa: PLC0415

        bx = _bounds(segs)
        if a.agent_drawn:
            (x0, y0), (x1, y1) = px((bx[0], bx[3])), px((bx[2], bx[1]))
            x0, y0, x1, y1 = x0 - 8, y0 - 8, x1 + 8, y1 + 8
            for a_, b_ in (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))):
                _dashed(draw, a_, b_, accent, 2, 7.0, 5.0)
            draw.text((x0, y0 - 18), f"agent-drawn sketch {a.id}: verify before cutting", fill=accent, font=font)
        for isl in a.islands:
            if isl.status in ("open", "failed"):
                cx, cy = px(((isl.region[0] + isl.region[2]) / 2, (isl.region[1] + isl.region[3]) / 2))
                draw.ellipse([cx - 12, cy - 12, cx + 12, cy + 12], outline=(220, 0, 0), width=3)
                draw.text((cx + 14, cy - 8), f"island {a.id}/{isl.id}", fill=(220, 0, 0), font=font)


def _ruler(draw: ImageDraw.ImageDraw, win: Region, scale: float, w: int, h: int, font, units: str) -> None:
    for axis in ("x", "y"):
        lo, hi = (win.x0, win.x1) if axis == "x" else (win.y0, win.y1)
        # About one labelled tick per 70 px, so labels never run into each other.
        step = _nice_step(hi - lo, max(2, min(12, int((hi - lo) * scale / 70))))
        first = math.floor(lo / step)
        for k in range(first, first + 200):  # bounded, and no float drift from repeated addition
            v = k * step
            if v > hi + 1e-9:
                break
            if v >= lo - 1e-9:
                if axis == "x":
                    x = MARGIN + (v - win.x0) * scale
                    draw.line([(x, MARGIN - 12), (x, MARGIN - 4)], fill=(120, 120, 120))
                    draw.line([(x, MARGIN), (x, h - MARGIN)], fill=(238, 238, 238))
                    draw.text((x + 2, MARGIN - 30), _fmt(v, units), fill=(110, 110, 110), font=font)
                else:
                    y = MARGIN + (win.y1 - v) * scale
                    draw.line([(MARGIN - 12, y), (MARGIN - 4, y)], fill=(120, 120, 120))
                    draw.line([(MARGIN, y), (w - MARGIN, y)], fill=(238, 238, 238))
                    draw.text((4, y - 8), _fmt(v, units), fill=(110, 110, 110), font=font)


def _draw_segment(draw: ImageDraw.ImageDraw, s: Segment, px, scale: float) -> None:
    colour = COLOURS.get(s.type, (150, 150, 150))
    width = 2
    if isinstance(s, Line):
        a, b = px(s.start), px(s.end)
        if s.type in DASHED:
            _dashed(draw, a, b, colour, width)
        else:
            draw.line([a, b], fill=colour, width=width)
        return
    cx, cy = px(s.center)
    r = s.radius * scale
    box = [cx - r, cy - r, cx + r, cy + r]
    if math.dist(s.start, s.end) <= 1e-9:
        draw.ellipse(box, outline=colour, width=width)
        return
    a0 = math.degrees(math.atan2(s.start[1] - s.center[1], s.start[0] - s.center[0]))
    a1 = math.degrees(math.atan2(s.end[1] - s.center[1], s.end[0] - s.center[0]))
    if s.clockwise:
        a0, a1 = a1, a0
    # Pillow measures angles clockwise in image space (y down): negate and swap.
    draw.arc(box, start=-a1, end=-a0, fill=colour, width=width)


def _dashed(draw, a, b, colour, width, dash=9.0, gap=6.0) -> None:
    length = math.dist(a, b)
    if length == 0:
        return
    ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
    t = 0.0
    while t < length:
        e = min(t + dash, length)
        draw.line([(a[0] + ux * t, a[1] + uy * t), (a[0] + ux * e, a[1] + uy * e)], fill=colour, width=width)
        t += dash + gap


def _fit(draw, text: str, font, width: float) -> str:
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "...", font=font) > width:
        text = text[:-1]
    return text + "..."


def _score_positions(d: Dieline, vertical: bool, tol: float, region: Region | None = None,
                     structure_only: bool = False) -> list[float]:
    """Score positions, tolerant of the tiny jitter in real CAD exports, with
    positions closer than the tolerance merged. structure_only: only the
    standard's own hinges (from the face map), for the panel labels."""
    i = 0 if vertical else 1
    hinges = set(getattr(d.faces, "hinges", {}) or {}) if structure_only else set()
    vals = sorted(
        (seg.start[i] + seg.end[i]) / 2
        for seg in d.of_type(LineType.CREASE)
        if (not hinges or seg.op in hinges)
        and isinstance(seg, Line) and abs(seg.start[i] - seg.end[i]) <= tol
        and seg.length > 4 * tol and (region is None or touches(seg, region))
    )
    out: list[float] = []
    for v in vals:
        if out and v - out[-1] <= 4 * tol:
            continue
        out.append(v)
    return out


def _labels(draw, d: Dieline, px, small, normal) -> None:
    """Distances between scores and blank edges, and each panel's flap depths, so
    sizes can be read off the picture. Labels that don't fit are left out."""
    xmin, ymin, xmax, ymax = d.bounds()
    tol = 0.001 * (25.4 if d.units == "mm" else 1.0)  # 1/1000 in
    min_span = 1 / 32 * (25.4 if d.units == "mm" else 1.0)
    xs = _score_positions(d, vertical=True, tol=tol, structure_only=True)
    ys = _score_positions(d, vertical=False, tol=tol, structure_only=True)
    scale = px((1, 0))[0] - px((0, 0))[0]

    def pill(cx: float, cy: float, text: str, room_px: float) -> None:
        tw = draw.textlength(text, font=normal)
        if tw + 8 > room_px:
            return  # doesn't fit: say nothing rather than cover the drawing
        draw.rectangle([cx - tw / 2 - 3, cy - 11, cx + tw / 2 + 3, cy + 11], fill=(255, 255, 230))
        draw.text((cx - tw / 2, cy - 9), text, fill=(150, 60, 0), font=normal)

    # The body is where the vertical (panel) scores run; flaps lie beyond it. With a face map, only the
    # standard's own hinges count: a feature's crease (a vertical hand-flap hinge, an extra score) isn't a panel.
    hinges = set(getattr(d.faces, "hinges", {}) or {})
    structural = [c for c in d.of_type(LineType.CREASE) if not hinges or c.op in hinges]
    vertical = [c for c in structural
                if isinstance(c, Line) and abs(c.start[0] - c.end[0]) <= tol and c.length > 4 * tol]
    if vertical:
        body = (min(min(c.start[1], c.end[1]) for c in vertical), max(max(c.start[1], c.end[1]) for c in vertical))
    else:
        body = (ymin, ymax)
    ybands = [ymin, *ys, ymax]
    mid_y = (body[0] + body[1]) / 2
    body_scores = sorted((c.start[0] + c.end[0]) / 2 for c in vertical
                         if min(c.start[1], c.end[1]) <= mid_y <= max(c.start[1], c.end[1]))
    xedges = [xmin, *xs, xmax]
    for a, b in zip(xedges, xedges[1:]):
        if b - a < min_span:
            continue
        if any(edge not in (xmin, xmax) and not any(abs(edge - x) <= tol for x in body_scores)
               for edge in (a, b)):
            continue
        x, y = px(((a + b) / 2, mid_y))
        pill(x, y, _fmt(b - a, d.units), (b - a) * scale)

    # Heights of each band down the right-hand side.
    for a, b in zip(ybands, ybands[1:]):
        if b - a >= min_span and (b - a) * scale >= 18:
            x, y = px((xmax, (a + b) / 2))
            draw.text((x + 8, y - 8), _fmt(b - a, d.units), fill=(150, 60, 0), font=normal)

    # Flap depths per panel: from the outer score to the furthest cut edge over
    # the panel's middle. Shows CSSC's deeper inner flaps and uneven flap rows.
    if ys and vertical:
        cuts = [c for c in d.of_type(LineType.CUT) if isinstance(c, Line) and abs(c.start[1] - c.end[1]) <= tol]
        for a, b in zip(xedges, xedges[1:]):
            if b - a < min_span or (b - a) * scale < 40:
                continue
            mx = (a + b) / 2
            over = [c.start[1] for c in cuts if min(c.start[0], c.end[0]) - tol <= mx <= max(c.start[0], c.end[0]) + tol]
            # Only a score at the body's edge hinges a flap; cuts beyond it are flap tips.
            top_score = next((y for y in ys if abs(y - body[1]) <= 4 * tol), None)
            bottom_score = next((y for y in ys if abs(y - body[0]) <= 4 * tol), None)
            top = max((y for y in over if y > body[1] + min_span), default=None) if top_score is not None else None
            bottom = (min((y for y in over if y < body[0] - min_span), default=None)
                      if bottom_score is not None else None)
            for tip, score in ((top, top_score), (bottom, bottom_score)):
                if tip is None or score is None:
                    continue
                depth = abs(tip - score)
                if depth * scale < 26 or any(min(tip, score) + tol < y < max(tip, score) - tol for y in ys):
                    continue
                x, y = px((mx, (tip + score) / 2))
                pill(x, y, _fmt(depth, d.units), (b - a) * scale)

    _face_labels(draw, d, px, small)
    _callouts(draw, d, px, small)

    # Overall size under the blank.
    x, y = px(((xmin + xmax) / 2, ymin))
    text = f"blank {_fmt(xmax - xmin, d.units)} x {_fmt(ymax - ymin, d.units)} {d.units}"
    tw = draw.textlength(text, font=normal)
    draw.text((x - tw / 2, y + 8), text, fill=(0, 0, 0), font=normal)


def geometry(d: Dieline, line_type: str | None = None, region: Region | None = None, limit: int = 500) -> dict:
    """Every manufactured segment (optionally one type, inside a window), plus
    the score positions designers check first."""
    wanted = None
    if line_type is not None:
        try:
            wanted = LineType(line_type)
        except ValueError:
            raise ValueError(f"line_type must be one of {', '.join(t.value for t in LineType)}") from None

    def inside(s: Segment) -> bool:
        return region is None or touches(s, region)  # any part in the window, not just an end

    r = lambda v: round(v, 5)  # noqa: E731
    out = []
    for i, s in enumerate(d.segments):
        if (wanted and s.type != wanted) or not inside(s):
            continue
        item: dict = {"i": i, "type": s.type.value}
        if isinstance(s, Line):
            item.update(kind="line", start=[r(s.start[0]), r(s.start[1])], end=[r(s.end[0]), r(s.end[1])],
                        length=r(s.length))
        else:
            item.update(kind="arc", start=[r(s.start[0]), r(s.start[1])], end=[r(s.end[0]), r(s.end[1])],
                        center=[r(s.center[0]), r(s.center[1])], radius=r(s.radius), clockwise=s.clockwise)
        out.append(item)
    xmin, ymin, xmax, ymax = d.bounds(include_annotations=_annotation_only(d))
    tol = 0.001 * (25.4 if d.units == "mm" else 1.0)
    return {
        "units": "in" if d.units == "in" else "mm",
        "origin": "native design coordinates, x right and y up",
        "bounds": [r(xmin), r(ymin), r(xmax), r(ymax)],
        # Score positions follow the same window (but not the line-type filter).
        "vertical_score_x": [r(v) for v in _score_positions(d, True, tol, region)],
        "horizontal_score_y": [r(v) for v in _score_positions(d, False, tol, region)],
        "segment_count": len(out),
        "segments": out[:limit],
        "truncated": len(out) > limit,
    }


FACE_SHORT = {"glue_lug": "GL", "glue_lug_ext_top": "GL ext", "glue_lug_ext_bottom": "GL ext"}


def _face_labels(draw, d: Dieline, px, font) -> None:
    """Each body panel and the glue lug named at its centre (GL, P4 ... P1), from the face map,
    so a feature's crease can never be mistaken for a panel."""
    fm = d.faces
    faces = getattr(fm, "faces", None)
    if not faces:
        return
    for f in faces.values():
        if f.kind not in ("body", "lug"):
            continue
        cx = sum(p[0] for p in f.polygon) / len(f.polygon)
        top = max(p[1] for p in f.polygon)
        x, y = px((cx, top))
        text = FACE_SHORT.get(f.id, f.id) + (f" ({f.role})" if f.role in ("end", "side") else "")
        tw = draw.textlength(text, font=font)
        draw.text((x - tw / 2, y + 4), text, fill=(90, 90, 90), font=font)


def _callouts(draw, d: Dieline, px, font) -> None:
    """Each feature's measured offsets (first copy on each face): a dimension line from its
    reference to the edge or centre it was measured to, labelled with the distance as given."""
    feats = d.extras.get("features") or []
    fm = d.faces
    if not feats or fm is None:
        return
    colour = (170, 0, 140)
    for b in feats:  # type: ignore[union-attr]
        for c in b.copies:
            if not c.callouts:
                continue
            f = fm.faces.get(c.face)  # type: ignore[attr-defined]
            if f is None:
                continue
            segs = c.outline or c.segs
            if not segs:
                continue
            xs = [p[0] for s in segs for p in (s.start, s.end)]
            ys = [p[1] for s in segs for p in (s.start, s.end)]
            mid_flat = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
            mid = f.from_flat(mid_flat)
            for co in c.callouts:
                if co.axis == "y":
                    a, b2 = f.to_flat((mid[0], co.at)), f.to_flat((mid[0], co.to_coord))
                else:
                    a, b2 = f.to_flat((co.at, mid[1])), f.to_flat((co.to_coord, mid[1]))
                pa, pb = px(a), px(b2)
                if math.dist(pa, pb) < 6:
                    continue
                draw.line([pa, pb], fill=colour, width=1)
                for q in (pa, pb):
                    draw.ellipse([q[0] - 2, q[1] - 2, q[0] + 2, q[1] + 2], fill=colour)
                mark = {"near_edge": "edge", "far_edge": "far edge", "centre": "centre"}[co.to]
                text = f"{b.id} {co.text} ({mark})"
                draw.text(((pa[0] + pb[0]) / 2 + 4, (pa[1] + pb[1]) / 2 - 7), text, fill=colour, font=font)


# ------------------------------------------------------------------ partitions


def _partition_labels(draw, d: Dieline, px, small, normal, caption: bool = True) -> None:
    """A partition piece: each slot's width x depth at its mouth, the distances
    between slot centres and the end legs along the piece, its length and height."""
    meta = d.meta
    L, H = meta["piece_in"]["length"], meta["piece_in"]["height"]  # type: ignore[index]
    slots = meta.get("slots_in") or []
    scale = px((1, 0))[0] - px((0, 0))[0]

    def text(x: float, y: float, t: str, colour=(150, 60, 0), font=normal) -> None:
        tw = draw.textlength(t, font=font)
        draw.text((x - tw / 2, y - 8), t, fill=colour, font=font)

    xs = [0.0, *[s["centre"] for s in slots], L]  # type: ignore[index]
    for a, b in zip(xs, xs[1:]):
        if (b - a) * scale > 40:
            x, y = px(((a + b) / 2, H / 2))
            text(x, y, _fmt(b - a, "in"))
    for k, s in enumerate(slots):  # just past each slot's bottom, inside the piece
        x, _ = px((s["centre"], 0))  # type: ignore[index]
        y = px((0, H - s["depth"]))[1] + 14 if s["from"] == "top" else px((0, s["depth"]))[1] - 14  # type: ignore[index]
        text(x, y, f"{k + 1}: {_fmt(s['width'], 'in')} x {_fmt(s['depth'], 'in')}", (30, 90, 220), small)
    if not caption:
        return
    x, y = px((L / 2, 0))
    label = f"piece {_fmt(L, 'in')} x {_fmt(H, 'in')} in" + (f", {len(slots)} slots from the {slots[0]['from']}"
                                                              if slots else ", no slots")
    tw = draw.textlength(label, font=normal)
    draw.text((x - tw / 2, y + 30), label, fill=(0, 0, 0), font=normal)


def render_set_png(plan, pieces: list[Dieline], labels: list[tuple[str, int]]) -> tuple[bytes, dict]:
    """A partition set: the plan view (box inside, walls, installed cells
    labelled) above each piece type with its slots, quantity and sizes."""
    spec = plan.spec
    gap = 1.25
    width = max([plan.box_long, *[p.meta["piece_in"]["length"] for p in pieces]])  # type: ignore[index]
    height = plan.box_short + sum(p.meta["piece_in"]["height"] + gap for p in pieces) + gap  # type: ignore[index]
    scale = min((MAX_SIDE - 2 * MARGIN) / width, (MAX_SIDE - 2 * MARGIN - LEGEND) / height)
    w = max(int(width * scale) + 2 * MARGIN, 640)
    h = int(height * scale) + 2 * MARGIN + LEGEND
    img = Image.new("RGB", (w, h), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    small, normal = _font(13), _font(16)

    # Plan view: y runs down the picture from the top margin.
    ox, oy = MARGIN, MARGIN

    def pv(x: float, y: float) -> tuple[float, float]:
        return (ox + x * scale, oy + y * scale)

    draw.rectangle([*pv(0, 0), *pv(plan.box_long, plan.box_short)], outline=(120, 120, 120), width=1)
    cl, cs = spec.clearance_long, spec.clearance_short
    for x, t in zip(plan.positions_long, plan.t_long):  # short pieces cross the long axis
        draw.rectangle([*pv(x - t / 2, cs / 2), *pv(x + t / 2, plan.box_short - cs / 2)], fill=(30, 30, 30))
    for y, t in zip(plan.positions_short, plan.t_short):
        draw.rectangle([*pv(cl / 2, y - t / 2), *pv(plan.box_long - cl / 2, y + t / 2)], fill=(30, 30, 30))
    edges_l = [0.0, *plan.positions_long, plan.box_long]
    for k, c in enumerate(spec.long_cells):
        mid = (edges_l[k] + edges_l[k + 1]) / 2
        t = _fmt(c, "in")
        tw = draw.textlength(t, font=small)
        x, y = pv(mid, 0)
        if tw + 6 < c * scale:
            draw.text((x - tw / 2, y - 18), t, fill=(150, 60, 0), font=small)
    edges_s = [0.0, *plan.positions_short, plan.box_short]
    for k, c in enumerate(spec.short_cells):
        mid = (edges_s[k] + edges_s[k + 1]) / 2
        x, y = pv(0, mid)
        if 16 < c * scale:
            t = _fmt(c, "in")
            draw.text((x - draw.textlength(t, font=small) - 6, y - 7), t, fill=(150, 60, 0), font=small)
    x, y = pv(plan.box_long / 2, plan.box_short)
    t = (f"plan: {len(spec.long_cells)} x {len(spec.short_cells)} cells, box inside {_fmt(plan.box_long, 'in')} x "
         f"{_fmt(plan.box_short, 'in')} in (installed cells labelled)")
    draw.text((x - draw.textlength(t, font=normal) / 2, y + 6), t, fill=(0, 0, 0), font=normal)

    # The pieces, each in its own band, drawn with y up.
    top = plan.box_short + gap
    for d, (label, qty) in zip(pieces, labels):
        H = d.meta["piece_in"]["height"]  # type: ignore[index]
        base = top + H

        def px(p: tuple[float, float], base=base) -> tuple[float, float]:
            return (ox + p[0] * scale, oy + (base - p[1]) * scale)

        for s in d.segments:
            _draw_segment(draw, s, px, scale)
        _partition_labels(draw, d, px, small, normal, caption=False)
        sl = d.meta.get("slots_in") or []
        cap = (f"{label} x {qty} ({d.meta['board']}): {_fmt(d.meta['piece_in']['length'], 'in')} x "  # type: ignore[index]
               f"{_fmt(H, 'in')} in, " + (f"{len(sl)} slots from the {sl[0]['from']}" if sl else "no slots"))
        draw.text((px((0, H))[0], px((0, H))[1] - 24), cap, fill=(0, 0, 0), font=normal)
        top = base + gap
    line1 = "partition set (not calibrated)   |   black = cut   |   plan view on top, then each piece"
    line2 = f"{scale:.1f} px/in   |   slot labels: width x depth; along each piece: end or slot centre to slot centre"
    draw.text((10, h - LEGEND + 6), _fit(draw, line1, small, w - 20), fill=(60, 60, 60), font=small)
    draw.text((10, h - LEGEND + 26), _fit(draw, line2, small, w - 20), fill=(60, 60, 60), font=small)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), {"units": "in", "px_per_unit": round(scale, 3), "image_px": [w, h],
                            "shows": "the set's plan view and each piece type"}

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

    show_labels = region is None and not _annotation_only(d)
    if show_labels:
        _labels(draw, d, px, small, normal)

    others = sorted({seg.type.value for seg in d.segments} - {"cut", "crease"})
    key = "black = cut, blue dashed = crease" + (
        ", orange = perforation" if "perforation" in others else "") + (
        ", green = score" if "score" in others else "") + (
        ", grey = annotation" if "annotation" in others else "")
    line1 = f"{title or d.name}   |   {key}"
    line2 = (f"window x {_fmt(win.x0, units)}..{_fmt(win.x1, units)}, y {_fmt(win.y0, units)}..{_fmt(win.y1, units)} "
             f"{units}   |   {scale:.1f} px/{units}")
    if show_labels:
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


def _score_positions(d: Dieline, vertical: bool, tol: float, region: Region | None = None) -> list[float]:
    """Score positions, tolerant of the tiny jitter in real CAD exports, with
    positions closer than the tolerance merged."""
    i = 0 if vertical else 1
    vals = sorted(
        (seg.start[i] + seg.end[i]) / 2
        for seg in d.of_type(LineType.CREASE)
        if isinstance(seg, Line) and abs(seg.start[i] - seg.end[i]) <= tol
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
    xs = _score_positions(d, vertical=True, tol=tol)
    ys = _score_positions(d, vertical=False, tol=tol)
    scale = px((1, 0))[0] - px((0, 0))[0]

    def pill(cx: float, cy: float, text: str, room_px: float) -> None:
        tw = draw.textlength(text, font=normal)
        if tw + 8 > room_px:
            return  # doesn't fit: say nothing rather than cover the drawing
        draw.rectangle([cx - tw / 2 - 3, cy - 11, cx + tw / 2 + 3, cy + 11], fill=(255, 255, 230))
        draw.text((cx - tw / 2, cy - 9), text, fill=(150, 60, 0), font=normal)

    # The body is where the vertical (panel) scores run; flaps lie beyond it.
    vertical = [c for c in d.of_type(LineType.CREASE)
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

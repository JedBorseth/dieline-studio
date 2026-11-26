"""Phase 4: artwork (docs/DESIGN-SYSTEM.md, "Artwork"): the sources, biarc fitting,
islands and bridges, the aggregated checks and the suspicious-artwork flags."""

import math

import pytest

from dieline_engine import artwork_import as ai
from dieline_engine import artwork_sources as asrc
from dieline_engine import curves, designs, glyphs
from dieline_engine.designs import BoxParams
from dieline_engine.geometry import Arc, LineType
from dieline_engine.topology import validate

BOX = {"style": "reg-glio-nx", "length": 14, "width": 10, "depth": 10, "units": "in", "board": "32 B KR"}
MID = {"on": "P2", "x": "centred", "y": "centred"}


def params(*arts, **kw):
    return BoxParams.model_validate({**BOX, **kw, "artwork": list(arts)})


def made(*arts, **kw):
    p = params(*arts, **kw)
    return designs.create_box(p), designs.build(p)


def checks(out, prefix="artwork"):
    return {c["id"]: c for c in out["checks"] if c["id"].startswith(prefix)}


def ring(levels, fix=None, width=5):
    shapes = [{"type": "circle", "cx": 50, "cy": 50, "r": r} for r in (48, 36, 24, 12)[:levels]]
    return {"kind": "sketch", "sketch": {"shapes": shapes}, "width": width, "placement": MID,
            **({"islands": fix} if fix else {})}


# ------------------------------------------------------------------ biarc fitting


def _dev(curve, prims, n=2000):
    return max(min(curves.prim_point_dist(p, curve.point(i / n)) for p in prims) for i in range(n + 1))


@pytest.mark.parametrize("curve", [
    curves.Bezier(((0, 0), (1, 2), (3, -2), (4, 0))),
    curves.Bezier(((0, 0), (2, 3), (4, 0))),
    curves.EllipseArc((0, 0), 3, 1.2, 0.4, 0.0, math.tau),
    curves.AxisEllipse((1, 1), (2, 0.5), (-0.3, 1.0), 0.3, 4.0),
    curves.Nurbs(3, (0, 0, 0, 0, 0.3, 0.7, 1, 1, 1, 1), ((0, 0), (1, 3), (2, -1), (3, 2), (4, 0), (5, 1))),
])
def test_biarcs_fit_within_the_tolerance_with_arcs(curve):
    f = curves.fit(curve, 0.002)
    assert f.fallback == 0 and any(p.center is not None for p in f.prims)
    assert _dev(curve, f.prims) <= 0.002 + 1e-9
    # Chained end to end, from the curve's start to its end.
    assert f.prims[0].start == curve.point(0.0)
    for a, b in zip(f.prims, f.prims[1:]):
        assert a.end == b.start
    assert math.dist(f.prims[-1].end, curve.point(1.0)) < 1e-9


def test_a_tighter_tolerance_takes_more_arcs_and_a_cusp_falls_back_to_reported_chords():
    c = curves.Bezier(((0, 0), (1, 2), (3, -2), (4, 0)))
    assert len(curves.fit(c, 0.0002).prims) > len(curves.fit(c, 0.002).prims)
    cusp = curves.Bezier(((0, 0), (2, 2), (0, 2), (2, 0)))  # a cusp: no tangent at its tip
    f = curves.fit(cusp, 0.002)
    assert _dev(cusp, f.prims) <= 0.002 + 1e-6


def test_source_curvature_is_kept_for_the_bend_check():
    e = curves.EllipseArc((0, 0), 2, 0.5, 0, 0, math.tau)
    rmin = min(r for r, _ in e.radius_samples())
    assert rmin == pytest.approx(0.5 ** 2 / 2, rel=1e-6)  # b^2/a at the ends of the long axis


def test_svg_arc_radius_correction_is_reported_and_refused_over_one_percent():
    a = curves.svg_arc((0, 0), 1, 1, 0, False, True, (2.01, 0))
    assert a.correction == pytest.approx(0.005, rel=1e-3)
    ok = {"kind": "sketch", "sketch": {"paths": ["M 10 50 A 20 20 0 0 1 50.2 50 Z"]}, "width": 3, "placement": MID}
    out, _ = made(ok)
    notes = [c for c in out["checks"] if c["id"].startswith("artwork_a1_note")]
    assert out["features"][0]["status"] == "ok" and any("enlarged" in c["message"] for c in notes)
    bad = {"kind": "sketch", "sketch": {"paths": ["M 10 50 A 20 20 0 0 1 60 50 Z"]}, "width": 3, "placement": MID}
    out, _ = made(bad)
    assert out["features"][0]["status"] == "stale" and "more than 1%" in out["features"][0]["reason"]


# ------------------------------------------------------------------ sources


@pytest.mark.parametrize("shape", [
    {"shape": "circle", "diameter": 2}, {"shape": "ellipse", "width": 3, "height": 1.5},
    {"shape": "rounded_rect", "width": 3, "height": 2, "corner_radius": 0.25},
    {"shape": "polygon", "sides": 6, "diameter": 3, "corner_radius": 0.2},
    {"shape": "star", "points": 5, "diameter": 3}, {"shape": "heart", "width": 3, "height": 3},
    {"shape": "smiley", "diameter": 2, "outline": "crease"}, {"shape": "arrow", "length": 4, "width": 2},
])
def test_every_library_shape_draws_valid_geometry(shape):
    out, d = made({"kind": "shape", **shape, "placement": MID})
    f = out["features"][0]
    assert f["status"] == "ok", f.get("reason")
    assert f["provenance"] == "library artwork" and validate(d).ok
    assert not any(c["status"] == "fail" for c in out["checks"])


def test_smiley_eyes_and_mouth_drop_out_and_its_outline_is_a_crease():
    out, d = made({"kind": "shape", "shape": "smiley", "diameter": 4, "outline": "crease", "placement": MID})
    assert out["features"][0]["rules"] == {"cut": 3, "crease": 1}
    assert "artwork_a1_islands" not in checks(out)  # the eyes are plain holes


def test_the_stencil_font_has_no_islands_and_keeps_its_breaks():
    for gap in (0.6, 0.75):
        for ch in glyphs.GLYPHS:
            polys = glyphs.stencil_glyph(ch, gap)
            assert polys, ch
    out, d = made({"kind": "stencil_text", "text": "ABDOPQR", "height": 1.5, "placement": MID},
                  {"kind": "stencil_text", "text": "04689", "height": 1.5, "placement": {**MID, "on": "P4"}})
    assert out["features"][0]["status"] == "ok", out["features"][0].get("reason")
    assert "artwork_a1_islands" not in checks(out) and validate(d).islands == []


def test_stencil_text_too_small_for_the_boards_bridge_says_how_tall_it_must_be():
    out, _ = made({"kind": "stencil_text", "text": "B8", "height": 0.6, "placement": MID})
    assert out["features"][0]["status"] == "stale" and "at least" in out["features"][0]["reason"]


def test_stroke_text_is_crease_or_annotation_never_cut():
    with pytest.raises(ValueError, match="hairline slits"):
        params({"kind": "stroke_text", "text": "HI", "height": 1, "rule": "cut", "placement": MID})
    out, d = made({"kind": "stroke_text", "text": "Hi 4X", "height": 1.5, "placement": MID})
    assert out["features"][0]["status"] == "ok", out["features"][0].get("reason")
    assert set(out["features"][0]["rules"]) == {"crease"} and validate(d).ok
    with pytest.raises(ValueError, match="no '#'"):
        params({"kind": "stencil_text", "text": "#1", "height": 1, "placement": MID})


def test_the_sketch_canvas_is_svg_coordinates_scaled_uniformly():
    # A bar along the canvas's top (small y) lands at the top of the face.
    top = {"kind": "sketch", "sketch": {"shapes": [{"type": "rect", "x": 0, "y": 0, "width": 100, "height": 10},
                                                    {"type": "circle", "cx": 50, "cy": 80, "r": 10}]},
           "width": 4, "placement": MID}
    _, d = made(top)
    art = d.extras["artwork"][0]
    f = d.faces.faces["P2"]
    bar, dot = art.drawn
    ybar = f.from_flat(bar.segs[0].start)[1]
    ydot = f.from_flat(dot.segs[0].center)[1]
    assert ybar > ydot
    w = max(f.from_flat(s.start)[0] for s in bar.segs) - min(f.from_flat(s.start)[0] for s in bar.segs)
    assert w == pytest.approx(4.0, abs=1e-5)
    assert "agent-drawn sketch, verify before cutting" in art.words


def test_open_sketch_paths_are_never_closed_silently():
    open_path = {"kind": "sketch", "sketch": {"paths": ["M 10 10 L 90 10 L 50 90"]}, "width": 3, "placement": MID}
    out, _ = made(open_path)
    assert out["features"][0]["status"] == "stale" and "open path" in out["features"][0]["reason"]
    out, _ = made({**open_path, "rule": "crease"})
    assert out["features"][0]["status"] == "ok"
    near = {"kind": "sketch", "sketch": {"paths": ["M 10 10 L 90 10 L 50 90 L 10.1 10"]}, "width": 3,
            "closed": True, "placement": MID}
    out, _ = made(near)  # 0.1 canvas units at 3.75 in per 100: 0.004 in, within 0.005
    assert out["features"][0]["status"] == "ok" and "snapped" in out["features"][0]["summary"]
    far = {**near, "sketch": {"paths": ["M 10 10 L 90 10 L 50 90 L 11 10"]}}
    out, _ = made(far)
    assert out["features"][0]["status"] == "stale" and "declared closed" in out["features"][0]["reason"]
    with pytest.raises(asrc.SourceError, match="sketch's commands"):
        asrc.parse_path("M 0 0 H 10", what="p", canvas=True, corrections=[], strict_arcs=True)
    with pytest.raises(asrc.SourceError, match="outside the 0-100"):
        asrc.parse_path("M 0 0 L 120 10", what="p", canvas=True, corrections=[], strict_arcs=True)


def test_sketch_curves_are_fitted_at_final_size():
    sk = {"kind": "sketch", "sketch": {"paths": ["M 10 50 C 10 0 90 0 90 50 C 90 100 10 100 10 50 Z"]},
          "width": 6, "placement": MID}
    _, d = made(sk)
    dr = d.extras["artwork"][0].drawn[0]
    assert {dr.kinds[s] for s in dr.segs} == {"fit"} and all(isinstance(s, Arc) for s in dr.segs)
    assert dr.radii  # the source curvature, for the bend check


# ------------------------------------------------------------------ imports

SVG = b"""<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="4in" height="2in"
 viewBox="0 0 400 200"><defs><circle id="dot" cx="0" cy="0" r="10"/></defs>
<g transform="translate(10,10) scale(1)"><rect x="0" y="0" width="100" height="50" rx="5"/>
<path d="M 200 20 h 50 v 50 h -50 z M 210 30 h 30 v 30 h -30 z" fill="black"/>
<use xlink:href="#dot" x="300" y="100"/><ellipse cx="150" cy="150" rx="30" ry="15" transform="rotate(20)"/>
<text>Hi</text><path d="M0 0 L 5 5" style="display:none"/><path d="M 0 150 L 50 150" fill="none" stroke="black"/>
</g></svg>"""


def test_svg_import_reads_units_transforms_and_use_and_lists_what_it_cant():
    p = ai.parse_svg(SVG)
    assert p.units_in == pytest.approx(0.01)
    assert len(p.subs) == 6  # rect, two subpaths, the used dot, the ellipse, the stroke-only line
    assert any("<text>" in u for u in p.unsupported) and any("hidden" in u for u in p.unsupported)
    assert any("fill-rule" in n for n in p.notes) and any("stroke-only" in n for n in p.notes)
    dot = next(s for s in p.subs if len(s.pieces) == 1 and isinstance(s.pieces[0], asrc.Prim)
               and s.pieces[0].full)
    c = dot.pieces[0].center
    assert c == pytest.approx((310, -110))  # used at (300, 100) inside translate(10, 10), y flipped up


def test_svg_with_evenodd_shows_no_fill_rule_note_and_unitless_needs_a_size():
    svg = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><path fill-rule="evenodd"
      d="M0 0 H10 V10 H0 Z M2 2 H8 V8 H2 Z"/></svg>"""
    p = ai.parse_svg(svg)
    assert p.units_in is None and not any("fill-rule" in n for n in p.notes)
    with pytest.raises(asrc.SourceError, match="width, height or fit_box"):
        asrc.scale_for(asrc.subs_bbox(p.subs), {}, None, "it")


def test_svg_parser_refuses_entities_and_dtds():
    bomb = b"""<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "aaaaaaaa"><!ENTITY b "&a;&a;&a;">]>
    <svg xmlns="http://www.w3.org/2000/svg"><text>&b;</text></svg>"""
    with pytest.raises(ai.ImportRefused, match="safely"):
        ai.parse_svg(bomb)


def _dxf(entities, header="", blocks=""):
    return ("0\nSECTION\n2\nHEADER\n" + header + "0\nENDSEC\n0\nSECTION\n2\nBLOCKS\n" + blocks +
            "0\nENDSEC\n0\nSECTION\n2\nENTITIES\n" + entities + "0\nENDSEC\n0\nEOF\n").encode()


def test_dxf_bulges_blocks_mirrored_and_scaled_inserts_and_units():
    poly = "0\nLWPOLYLINE\n70\n1\n10\n0\n20\n0\n42\n0\n10\n4\n20\n0\n42\n1\n10\n4\n20\n2\n10\n0\n20\n2\n"
    block = "0\nBLOCK\n2\nB1\n10\n0\n20\n0\n0\nCIRCLE\n10\n5\n20\n1\n40\n1\n0\nENDBLK\n"
    insert = "0\nINSERT\n2\nB1\n10\n100\n20\n0\n41\n-2\n42\n2\n0\nINSERT\n2\nB1\n10\n0\n20\n50\n50\n90\n"
    p = ai.parse_dxf(_dxf(poly + insert, "9\n$INSUNITS\n70\n4\n", block))
    assert p.units_in == pytest.approx(1 / 25.4)
    polyl = next(s for s in p.subs if len(s.pieces) == 4)
    bulge = polyl.pieces[1]  # bulge 1 from (4, 0) to (4, 2): a half circle, counter-clockwise, centre (4, 1)
    assert bulge.center == pytest.approx((4, 1)) and not bulge.clockwise
    circles = [s.pieces[0] for s in p.subs if len(s.pieces) == 1]
    centres = sorted(tuple(round(v, 6) for v in c.center) for c in circles)
    # Mirrored and doubled: (5, 1) -> (100 - 10, 2); rotated 90 deg: (5, 1) -> (-1, 55).
    assert centres == [(-1.0, 55.0), (90.0, 2.0)]
    assert sorted(round(math.dist(c.center, c.start), 6) for c in circles) == [1.0, 2.0]


def test_dxf_ellipse_spline_chaining_colours_and_refusals():
    ents = ("0\nELLIPSE\n10\n0\n20\n0\n11\n2\n21\n0\n40\n0.5\n41\n0\n42\n6.283185307179586\n"
            "0\nLINE\n62\n2\n10\n10\n20\n0\n11\n12\n21\n0\n0\nLINE\n62\n2\n10\n12\n20\n0\n11\n12\n21\n2\n"
            "0\nSPLINE\n71\n3\n40\n0\n40\n0\n40\n0\n40\n0\n40\n1\n40\n1\n40\n1\n40\n1\n"
            "10\n20\n20\n0\n10\n21\n20\n2\n10\n23\n20\n2\n10\n24\n20\n0\n0\nTEXT\n1\nhi\n")
    p = ai.parse_dxf(_dxf(ents))
    assert p.units_in is None and p.unsupported == ["TEXT"]
    rules = [s.rule for s in p.subs]
    assert rules.count("crease") == 1  # the two crease lines chained into one path
    crease = next(s for s in p.subs if s.rule == "crease")
    assert len(crease.pieces) == 2 and not crease.closed
    with pytest.raises(ai.ImportRefused, match="out of the drawing plane"):
        ai.parse_dxf(_dxf("0\nLINE\n10\n0\n20\n0\n30\n1\n11\n1\n21\n0\n31\n1\n"))
    with pytest.raises(ai.ImportRefused, match="binary"):
        ai.parse_dxf(b"AutoCAD Binary DXF\r\n\x1a\x00")


def test_stored_import_geometry_round_trips_and_is_checked():
    p = ai.parse_svg(SVG)
    back = ai.from_json(ai.to_json(p.subs))
    assert len(back) == len(p.subs)
    with pytest.raises(asrc.SourceError):
        ai.from_json([{"pieces": [{"l": [0, 0, "x", 1]}]}])


def test_an_import_item_can_only_carry_geometry_when_trusted():
    from dieline_engine import artwork

    item = {"kind": "import", "path": "x.svg", "width": 2, "placement": MID,
            "source": {"geometry": ai.to_json(ai.parse_svg(SVG).subs), "units_in": 0.01, "name": "x.svg"}}
    with pytest.raises(artwork.ArtworkError, match="designer's approval"):
        artwork.normalize(item)
    with pytest.raises(artwork.ArtworkError, match="approve"):
        artwork.upsert([], [{k: v for k, v in item.items() if k != "source"}], None)
    out, d = made({**item, "id": "a1", "loop_rules": {"6": "crease"}})  # a record (trusted)
    f = out["features"][0]
    assert f["status"] == "ok" and f["provenance"] == "imported artwork", f.get("reason")


# ------------------------------------------------------------------ islands and bridges


def test_the_middle_of_an_o_is_an_island_that_fails_and_blocks_export(tmp_path):
    out, d = made(ring(2))
    isl = checks(out)["artwork_a1_islands"]
    assert isl["status"] == "fail" and "bridge" in isl["message"] and "drop_out" in isl["message"]
    assert out["overall"] == "fail" and out["features"][0]["islands"][0]["status"] == "open"
    assert len(validate(d).islands) == 1  # drawn, with the island
    with pytest.raises(designs.DesignCheckFailed):
        designs.export(params(ring(2)), tmp_path, "o")


@pytest.mark.parametrize("levels,islands", [(2, 1), (3, 1), (4, 2)])
def test_bridges_hold_two_and_three_levels_of_nesting(levels, islands):
    out, d = made(ring(levels, "bridge"))
    f = out["features"][0]
    assert [i["status"] for i in f["islands"]] == ["bridged"] * islands
    rep = validate(d)
    assert rep.ok and rep.islands == []  # every island now joined to the blank
    assert out["overall"] != "fail"
    art = d.extras["artwork"][0]
    sides = [s for dr in art.live() for s, k in dr.kinds.items() if k == "bridge" and s in dr.segs]
    assert len(sides) == 4 * islands  # two corridors per island, two sides each
    w = art.bridge_width
    assert w == pytest.approx(max(3 / 16, 1.5 * d.meta["cal_in"]))
    # Each corridor's two sides are parallel, w apart.
    pairs = 0
    for a in sides:
        for b in sides:
            if a is b:
                continue
            ua = (a.end[0] - a.start[0], a.end[1] - a.start[1])
            ub = (b.end[0] - b.start[0], b.end[1] - b.start[1])
            if abs(ua[0] * ub[1] - ua[1] * ub[0]) < 1e-6 * a.length * b.length:
                n = (-ua[1] / a.length, ua[0] / a.length)
                dist = abs((b.start[0] - a.start[0]) * n[0] + (b.start[1] - a.start[1]) * n[1])
                if abs(dist - w) < 1e-4:
                    pairs += 1
    assert pairs >= 2 * islands


def test_bridges_go_across_the_flutes_first():
    _, d = made(ring(2, "bridge"))
    art = d.extras["artwork"][0]
    sides = [s for dr in art.live() for s, k in dr.kinds.items() if k == "bridge"]
    # Vertical flutes (along flat y): the first corridor runs along flat x, across them.
    assert any(abs(s.end[1] - s.start[1]) < 1e-9 for s in sides)


def test_partial_cut_and_drop_out_are_explicit_choices():
    out, d = made(ring(2, "partial_cut"))
    assert out["features"][0]["islands"][0]["status"] == "partial_cut"
    assert out["features"][0]["rules"] == {"partial_cut": 1, "cut": 1}
    assert not out["targets"]["cf2"]["ready"]  # a partial cut isn't verified for either target
    handled = checks(out)["artwork_a1_islands_handled"]
    assert "explicit choice" in handled["message"]
    out, d = made(ring(2, {"i1": "drop_out"}))
    assert out["features"][0]["islands"][0]["status"] == "drop_out"
    assert "falls out" in checks(out)["artwork_a1_islands_handled"]["message"]
    assert next(c for c in out["checks"] if c["id"] == "material")["status"] == "pass"
    assert out["overall"] != "fail"


def test_an_island_inside_a_features_hole_counts_features_and_artwork_together():
    window = {"kind": "window", "shape": "rect", "width": 5, "height": 5, "placement": MID}
    dot = {"kind": "shape", "shape": "circle", "diameter": 1, "placement": MID}
    p = BoxParams.model_validate({**BOX, "features": [window], "artwork": [dot]})
    out = designs.create_box(p)
    isl = checks(out)["artwork_a1_islands"]
    assert isl["status"] == "fail"
    p2 = BoxParams.model_validate({**BOX, "features": [window], "artwork": [{**dot, "islands": "bridge"}]})
    out2 = designs.create_box(p2)
    assert "feature's" in out2["features"][1]["islands"][0]["reason"]


# ------------------------------------------------------------------ tooling checks


def test_bend_radius_acute_short_and_thin_web_checks_aggregate():
    small = {"kind": "shape", "shape": "circle", "diameter": 0.2, "placement": {
        "on": "P2", "x": "centred", "y": {"from": "top_score", "to": "near_edge", "distance": 2}}}
    star = {"kind": "shape", "shape": "star", "points": 5, "diameter": 3, "inner_ratio": 0.2, "placement": MID}
    tiny_edge = {"kind": "sketch", "sketch": {"shapes": [{"type": "polygon", "points": [[0, 0], [100, 0], [100, 100],
                                                                                        [1, 100], [0, 99]]}]},
                 "width": 2, "placement": {"on": "P3", "x": "centred", "y": "centred"}}
    pair = {"kind": "sketch", "sketch": {"shapes": [{"type": "circle", "cx": 30, "cy": 50, "r": 19},
                                                    {"type": "circle", "cx": 70, "cy": 50, "r": 19}]},
            "width": 3, "placement": {"on": "P4", "x": "centred", "y": "centred"}}
    out, _ = made(small, star, tiny_edge, pair)
    c = checks(out)
    assert c["artwork_bend_radius"]["status"] == "warn" and "a1" in c["artwork_bend_radius"]["message"]
    assert c["artwork_acute"]["status"] == "warn" and c["artwork_acute"]["message"].startswith("5 corners")
    assert c["artwork_short"]["status"] == "warn" and "a3" in c["artwork_short"]["message"]
    assert c["artwork_web"]["status"] == "warn" and "a4" in c["artwork_web"]["message"]
    # One result each, however many places: never more than five listed.
    many = {"kind": "shape", "shape": "star", "points": 12, "diameter": 3, "inner_ratio": 0.15, "placement": MID}
    out, _ = made(many)
    msg = checks(out)["artwork_acute"]["message"]
    assert msg.startswith("12 corners") and msg.count(";") <= 5 and msg.endswith("...")


def test_bend_check_uses_the_source_curve_not_the_fit():
    thin = {"kind": "shape", "shape": "ellipse", "width": 3, "height": 0.4, "placement": MID}
    out, _ = made(thin)
    assert "artwork_bend_radius" in checks(out)  # b^2/a = 0.2^2 / 1.5 at the tips


def test_placement_edge_clearance_keepouts_and_outside_face():
    near = {"kind": "shape", "shape": "circle", "diameter": 1, "placement": {
        "on": "P2", "x": {"from": "left", "to": "near_edge", "distance": 0.125}, "y": "centred"}}
    glue = {"kind": "shape", "shape": "circle", "diameter": 1, "placement": {
        "on": "P1", "x": {"from": "free_edge", "to": "near_edge", "distance": 0.5}, "y": "centred"}}
    out, _ = made(near, glue)
    c = checks(out)
    assert c["artwork_edge"]["status"] == "warn" and "a1" in c["artwork_edge"]["message"]
    assert c["artwork_keepout"]["status"] == "warn" and "glue" in c["artwork_keepout"]["message"]
    off = {"kind": "shape", "shape": "circle", "diameter": 3, "placement": {
        "on": "P2", "x": {"from": "left", "to": "near_edge", "distance": 13}, "y": "centred"}}
    out, _ = made(off)
    assert out["features"][0]["status"] == "stale" and "outside" in out["features"][0]["reason"]


# ------------------------------------------------------------------ suspicious artwork

SLOT = {"kind": "sketch", "sketch": {"shapes": [{"type": "rect", "x": 0, "y": 0, "width": 100, "height": 10}]},
        "width": 3, "placement": {"on": "P2", "x": "centred",
                                  "y": {"from": "top_score", "to": "near_edge", "distance": 0.5}}}


def test_each_suspicious_flag_blocks_export_until_acknowledged():
    long_crease = {"kind": "sketch", "sketch": {"paths": ["M 0 50 L 100 50"]}, "width": 8, "rule": "crease",
                   "placement": {"on": "P1", "x": "centred", "y": "centred"}}
    big = {"kind": "shape", "shape": "rounded_rect", "width": 12, "height": 3, "corner_radius": 0.5,
           "placement": {"on": "P4", "x": "centred", "y": "centred"}}
    out, _ = made(SLOT, long_crease, big)
    c = checks(out)
    assert c["artwork_a1_flag_slot"]["status"] == "fail" and c["artwork_a1_flag_parallel"]["status"] == "fail"
    assert c["artwork_a2_flag_rule"]["status"] == "fail"
    assert c["artwork_a3_flag_span"]["status"] == "fail"
    assert all(x.get("ack") for x in c.values() if "_flag_" in x["id"])
    assert out["overall"] == "fail"
    keys = [x["ack"] for x in c.values() if "_flag_" in x["id"]]
    out, _ = made(SLOT, long_crease, big, acknowledged=keys)
    c = checks(out)
    assert all(x["status"] == "info" and x["acknowledged"] for x in c.values() if "_flag_" in x["id"])
    assert out["overall"] != "fail"


def test_an_acknowledgement_is_for_exactly_that_geometry():
    out, _ = made(SLOT)
    keys = [x["ack"] for x in checks(out).values() if x.get("ack")]
    moved = {**SLOT, "placement": {**SLOT["placement"], "y": {"from": "top_score", "to": "near_edge",
                                                              "distance": 0.625}}}
    out, _ = made(moved, acknowledged=keys)
    assert checks(out)["artwork_a1_flag_slot"]["status"] == "fail"


# ------------------------------------------------------------------ the tool


# ------------------------------------------------------------------ outputs


def test_cut_and_crease_artwork_read_back_from_cf2_and_dxf(tmp_path):
    arts = [ring(2, "bridge", 3),
            {"kind": "stencil_text", "text": "OK", "height": 2, "placement": {"on": "P1", "x": "centred",
                                                                              "y": "centred"}},
            {"kind": "shape", "shape": "smiley", "diameter": 1.5, "outline": "crease",
             "placement": {"on": "P3", "x": "centred", "y": "centred"}}]
    p = params(*arts)
    d = designs.build(p)
    assert designs.create_box(p)["targets"]["cf2"]["ready"]
    designs.check_outputs(d, {"cf2": designs.write_cf2(d), "dxf": designs.write_dxf(d)})
    out = designs.export(p, tmp_path, "art", ["cf2", "dxf"])
    assert out


def test_perforation_and_partial_cut_artwork_block_the_targets_but_crease_does_not():
    perf = {"kind": "shape", "shape": "circle", "diameter": 2, "rule": "perforation", "placement": MID}
    out, _ = made(perf)
    assert not out["targets"]["cf2"]["ready"] and not out["targets"]["dxf"]["ready"]
    crease = {"kind": "shape", "shape": "circle", "diameter": 2, "rule": "crease", "placement": MID}
    out, _ = made(crease)
    assert out["targets"]["cf2"]["ready"] and out["targets"]["dxf"]["ready"]


def test_scene_tags_provenance_and_the_features_list_has_the_artwork():
    from dieline_engine.designstore import scene

    out, d = made(ring(2, "bridge", 3), {"kind": "shape", "shape": "star", "diameter": 2,
                                         "placement": {"on": "P1", "x": "centred", "y": "centred"}})
    sc = scene(d)
    provs = {s["provenance"] for s in sc if s.get("feature_id")}
    assert provs == {"agent sketch", "library artwork"}
    assert [f["kind"] for f in out["features"]] == ["artwork", "artwork"]
    assert out["features"][0].get("agent_drawn") and not out["features"][1].get("agent_drawn")
    info = checks(out)["artwork_a1_agent_drawn"]
    assert info["status"] == "info"


def test_a_resize_re_resolves_artwork_and_can_leave_it_stale():
    big = {"kind": "shape", "shape": "circle", "diameter": 8, "placement": MID}
    out, _ = made(big)
    assert out["features"][0]["status"] == "ok"
    out, _ = made(big, length=7, width=6, depth=6)
    assert out["features"][0]["status"] == "stale" and out["overall"] == "fail"


def test_mm_parts_take_artwork_sizes_in_their_units():
    p = BoxParams.model_validate({**BOX, "units": "mm", "length": 356, "width": 254, "depth": 254, "artwork": [
        {"kind": "shape", "shape": "circle", "diameter": 50.8, "placement": MID}]})
    d = designs.build(p)
    s = d.extras["artwork"][0].segs()[0]
    assert d.units == "in" and s.radius == pytest.approx(1.0, abs=1e-5) and s.type == LineType.CUT

"""Phase 2: features on standards (docs/DESIGN-SYSTEM.md, "Features", "Placement",
"Expressions", "Rule types and export capability", "Feature checks")."""

import math

import pytest

from dieline_engine import designs, exports, rules, shapes
from dieline_engine.cf2 import read_cf2, write_cf2
from dieline_engine.designs import BoxParams, TargetBlocked
from dieline_engine.dxf import read_dxf, write_dxf
from dieline_engine.expressions import ExpressionError, env_for, evaluate, evaluate_all
from dieline_engine.boards import get_board
from dieline_engine.features import FeatureError, normalize, upsert
from dieline_engine.geometry import Arc, Line, LineType as T
from dieline_engine.topology import reconstruct, validate

BOX = {"style": "reg-glio-nx", "length": 14, "width": 10, "depth": 12, "units": "in", "board": "32 B KR"}
HH = {"kind": "handhole", "placement": {"on": "end_panels", "x": "centred",
                                        "y": {"from": "top_score", "to": "near_edge", "distance": 2}}}


def params(feats, **kw):
    return BoxParams.model_validate({**BOX, **kw, "features": feats})


def build(feats, **kw):
    return designs.build(params(feats, **kw))


def payload(feats, **kw):
    return designs.create_box(params(feats, **kw))


def feature(p, fid="f1"):
    return next(f for f in p["features"] if f["id"] == fid)


def checks(p, prefix="feature"):
    return {c["id"]: c for c in p["checks"] if c["id"].startswith(prefix)}


def segs_of(d, fid):
    tags = d.extras["tags"]
    return [s for s in d.segments if tags.get(s, {}).get("feature_id") == fid and s.type != T.ANNOTATION]


def bbox(segs):
    from dieline_engine.geometry import _segment_extent_points

    pts = [p for s in segs for p in _segment_extent_points(s)]
    return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))


def face_box(d, fid, face, index=0):
    """A feature copy's bounding box in its face's frame."""
    from dieline_engine.feature_checks import _face_box

    b = next(x for x in d.extras["features"] if x.id == fid)
    c = [c for c in b.copies if c.face == face][index]
    return _face_box(d.faces.faces[face], bbox(c.outline or c.segs))


# ------------------------------------------------------------------ expressions


def test_expressions_types_fractions_units_and_names():
    env = env_for("in", get_board("32 B KR"), {"L": 14, "W": 10, "D": 12})
    assert evaluate("W/2 - 1/4", env) == 4.75
    assert evaluate("1 1/2", env) == 1.5
    assert evaluate("2:12", env) == 2.75
    assert evaluate("2 * CAL", env) == pytest.approx(2 * get_board("32 B KR").cal)
    assert evaluate("50mm", env) == pytest.approx(50 / 25.4)
    assert evaluate("round(W/3, 1/16in)", env) == pytest.approx(3.3125)
    assert evaluate("max(1, 6*CAL)", env) == 1.0
    assert evaluate("L / W", env, "scalar") == 1.4
    with pytest.raises(ExpressionError, match="scalar"):
        evaluate("L / W", env)  # a ratio where a length is wanted
    with pytest.raises(ExpressionError, match="lengths together"):
        evaluate("L * W", env)
    with pytest.raises(ExpressionError, match="division by zero"):
        evaluate("W / 0", env)
    with pytest.raises(ExpressionError, match="unknown name"):
        evaluate("H + 1", env)
    with pytest.raises(ExpressionError, match="ambiguous"):
        evaluate("W / 3/4", env)
    with pytest.raises(ExpressionError):
        evaluate("__import__('os')", env)
    mm = env_for("mm", get_board("32 B KR"), {"L": 300, "W": 200, "D": 100})
    assert evaluate("D - CAL", mm) == pytest.approx(100 - get_board("32 B KR").cal * 25.4)  # CAL in the part's units
    assert evaluate("1in", mm) == pytest.approx(25.4)
    with pytest.raises(ExpressionError, match="inch parts"):
        evaluate("2:12", mm)
    with pytest.raises(ExpressionError, match="reserved"):
        env_for("in", None, {}, {"CAL": 1})


def test_expressions_order_their_dependencies_and_name_a_loop():
    env = env_for("in", None, {"L": 14, "W": 10, "D": 12})
    assert evaluate_all({"a": "b + 1", "b": "W / 2"}, env) == {"b": 5.0, "a": 6.0}
    with pytest.raises(ExpressionError, match="a -> b -> a"):
        evaluate_all({"a": "b + 1", "b": "a - 1"}, env)


# ------------------------------------------------------------------ shapes


def test_four_centre_oval_is_tangent_continuous_through_the_vertices():
    prims, gap = shapes.oval(4, 2)
    assert len(prims) == 4 and gap < 0.05
    for p in prims:  # each arc's ends sit on its own circle
        r0, r1 = math.dist(p.center, p.start), math.dist(p.center, p.end)
        assert r0 == pytest.approx(r1)
    for a, b in zip(prims, prims[1:] + prims[:1]):  # joined end to start, with the same tangent
        assert math.dist(a.end, b.start) < 1e-9
        ta = (a.end[0] - a.center[0], a.end[1] - a.center[1])
        tb = (b.start[0] - b.center[0], b.start[1] - b.center[1])
        assert abs(ta[0] * tb[1] - ta[1] * tb[0]) < 1e-9  # radii collinear: tangent
    assert shapes.bbox(prims) == pytest.approx((-2, -1, 2, 1))


def test_secant_hinge_on_the_file_box_handhole_meets_the_arcs_at_35_degrees_or_more():
    fp = shapes.flap_profile("obround", 3.625, 1.375, hinge="top")
    assert fp.inset == pytest.approx(1 / 8)  # r (1 - cos 35) = 0.1243, to the next 1/32
    assert fp.join_deg >= 35
    for hinge in ("bottom", "left", "right"):
        fp = shapes.flap_profile("rect", 3, 1.5, hinge=hinge)
        assert fp.join_deg == 90
    with pytest.raises(ValueError, match="long side"):
        shapes.flap_profile("obround", 1.375, 3.625, hinge="top")
    with pytest.raises(ValueError, match="dovetail"):
        shapes.flap_profile("angled", 3, 1.5, hinge="top", taper_deg=-5)
    with pytest.raises(ValueError, match="under 35"):
        shapes.flap_profile("angled", 3, 1.5, hinge="top", taper_deg=60)


# ------------------------------------------------------------------ input


def test_features_need_a_kind_known_fields_and_an_explicit_to():
    with pytest.raises(FeatureError, match="kind must be one of"):
        normalize({"kind": "hole"})
    with pytest.raises(FeatureError, match="no field"):
        normalize({**HH, "colour": "red"})
    bad = {"kind": "handhole", "placement": {"on": "P1", "x": "centred", "y": {"from": "top_score", "distance": 2}}}
    with pytest.raises(FeatureError, match="to is required"):
        normalize(bad)
    with pytest.raises(FeatureError, match="leave x out"):
        normalize({"kind": "vent", "placement": {"on": "P2", "x": "centred", "y": "centred",
                                                 "pattern": {"kind": "spread", "axis": "x", "count": 3, "inset": 1}}})
    with pytest.raises(FeatureError, match="relief nicks"):
        normalize({"kind": "hand_flap", "relief_nicks": True, "placement": HH["placement"]})
    with pytest.raises(FeatureError, match="faces must be"):
        normalize({"kind": "score", "route": {"direction": "horizontal", "at": {"from": "top", "distance": 1}}})
    hh = normalize(HH)
    assert (hh["shape"], hh["width"], hh["height"]) == ("obround", "3 5/8 in", "1 3/8 in")  # File_Box_1's


def test_batch_upsert_by_id_merges_and_removes():
    feats, touched = upsert([], [HH, {"kind": "vent", "placement": {"on": "P2", "x": "centred", "y": "centred"}}], None)
    assert [f["id"] for f in feats] == ["f1", "f2"] and touched == ["f1", "f2"]
    feats, touched = upsert(feats, [{"id": "f1", "placement": {"y": {"from": "top_score", "to": "centre",
                                                                     "distance": 3}}}], ["f2"])
    assert [f["id"] for f in feats] == ["f1"] and touched == ["f1"]
    assert feats[0]["placement"]["on"] == "end_panels"  # kept: only y was given
    assert feats[0]["placement"]["y"]["to"] == "centre"
    feats, _ = upsert(feats, [HH], None)
    assert [f["id"] for f in feats] == ["f1", "f2"]  # a new id after the highest, never a reused one in a list
    with pytest.raises(FeatureError, match="no feature 'f9'"):
        upsert(feats, [{"id": "f9", "kind": "vent"}], None)
    with pytest.raises(FeatureError, match="remove it and add"):
        upsert(feats, [{"id": "f1", "kind": "vent"}], None)
    with pytest.raises(FeatureError, match="to remove"):
        upsert(feats, [], ["f7"])


def test_a_part_without_features_reads_exactly_as_before():
    p = BoxParams.model_validate(BOX)
    assert "features" not in p.model_dump()
    assert "features" in params([HH]).model_dump()


# ------------------------------------------------------------------ placement


def test_edge_and_centre_distances_measure_the_named_side():
    d = build([HH])
    fb = face_box(d, "f1", "P1")
    h = d.faces.faces["P1"].height
    assert h - fb[3] == pytest.approx(2)  # near edge 2 from the top score
    assert (fb[2] - fb[0], fb[3] - fb[1]) == pytest.approx((3.625, 1.375))  # long side horizontal
    far = build([{**HH, "placement": {**HH["placement"], "y": {"from": "top_score", "to": "far_edge",
                                                                  "distance": 3}}}])
    assert h - face_box(far, "f1", "P1")[1] == pytest.approx(3)
    centre = build([{**HH, "placement": {**HH["placement"], "y": {"from": "centre_y", "to": "centre",
                                                                     "distance": -1}}}])
    fb = face_box(centre, "f1", "P1")
    assert (fb[1] + fb[3]) / 2 == pytest.approx(h / 2 - 1)
    with pytest.raises(ValueError, match="use to: centre"):
        _refused([{**HH, "placement": {**HH["placement"], "y": {"from": "centre_y", "to": "near_edge",
                                                                  "distance": 0}}}])


def _refused(feats, **kw):
    """Build, and raise the first stale feature's reason (what a tool call would refuse with)."""
    p = payload(feats, **kw)
    bad = [f for f in p["features"] if f["status"] == "stale"]
    if bad:
        raise ValueError(bad[0]["reason"])
    return p


def test_wrong_axis_references_and_unknown_lines_are_refused():
    with pytest.raises(ValueError, match="horizontal line on P1, so it can only position y"):
        _refused([{**HH, "placement": {"on": "P1", "x": {"from": "top_score", "to": "centre", "distance": 1},
                                       "y": "centred"}}])
    with pytest.raises(ValueError, match="no reference line 'top_edge'"):
        _refused([{**HH, "placement": {"on": "P1", "x": "centred",
                                       "y": {"from": "top_edge", "to": "centre", "distance": 1}}}])
    with pytest.raises(ValueError, match="outside its face"):
        _refused([{**HH, "placement": {**HH["placement"], "y": {"from": "top_score", "to": "near_edge",
                                                                  "distance": 20}}}])


def test_centred_is_the_assembled_centre_so_opposite_panels_match():
    # P1 is 1/8 narrower than P3 (it stops short of the joint), but the assembled centres agree.
    d = build([{**HH, "placement": {**HH["placement"], "mirror": "none"}}])
    p1, p3 = d.faces.faces["P1"], d.faces.faces["P3"]
    b1, b3 = face_box(d, "f1", "P1"), face_box(d, "f1", "P3")
    assert (b1[0] + b1[2]) / 2 == pytest.approx(p1.centre_x)
    assert (b3[0] + b3[2]) / 2 == pytest.approx(p3.centre_x)
    assert p1.width != pytest.approx(p3.width)


def test_position_only_mirroring_keeps_the_corner_and_pairs_opposite_faces():
    left = {"on": "end_panels", "x": {"from": "right", "to": "near_edge", "distance": 1}, "y": "centred"}
    d = build([{"kind": "vent", "placement": left}])
    b1, b3 = face_box(d, "f1", "P1"), face_box(d, "f1", "P3")
    # On P1 it's 1 in from the right (the P1/P2 score); mirrored, on P3 1 in from the left (the P2/P3 score).
    assert d.faces.faces["P1"].width - b1[2] == pytest.approx(1)
    assert b3[0] == pytest.approx(1)
    # Without mirroring, both are 1 in from their own right.
    d2 = build([{"kind": "vent", "placement": {**left, "mirror": "none"}}])
    assert d2.faces.faces["P3"].width - face_box(d2, "f1", "P3")[2] == pytest.approx(1)
    # All four body panels pair by opposite faces; P2 and P4 follow the same rule.
    d4 = build([{"kind": "vent", "placement": {**left, "on": "body_panels"}}])
    assert face_box(d4, "f1", "P4")[0] == pytest.approx(1)
    with pytest.raises(ValueError, match="aren't all opposite faces"):
        _refused([{"kind": "vent", "placement": {**left, "on": ["P1", "P2"]}}])
    ok = _refused([{"kind": "vent", "placement": {**left, "on": ["P1", "P2"], "pairs": [["P1", "P2"]]}}])
    assert feature(ok)["status"] == "ok"
    with pytest.raises(ValueError, match="glue_lug"):
        _refused([{"kind": "vent", "placement": {**left, "on": ["P1", "P3", "glue_lug"]}}])


def test_position_and_shape_flips_an_asymmetric_shape_on_the_second_face():
    d_hole = {"kind": "handhole", "shape": "d_shape", "width": 3, "height": 1.5, "flat": "left",
              "placement": {"on": "end_panels", "x": "centred", "y": "centred", "mirror": "position_and_shape"}}
    d = build([d_hole])
    b = next(x for x in d.extras["features"] if x.id == "f1")
    p1 = [c for c in b.copies if c.face == "P1"][0]
    p3 = [c for c in b.copies if c.face == "P3"][0]
    f1, f3 = d.faces.faces["P1"], d.faces.faces["P3"]
    # The flat side's x in each face frame: left of centre on P1, right of centre on P3.

    def flat_side_x(c, f):
        lines = [s for s in c.segs if isinstance(s, Line)]
        vertical = [s for s in lines if abs(s.start[0] - s.end[0]) < 1e-9 and s.length > 1.4]
        return f.from_flat(vertical[0].start)[0]

    fb1, fb3 = face_box(d, "f1", "P1"), face_box(d, "f1", "P3")
    assert flat_side_x(p1, f1) == pytest.approx(fb1[0])
    assert flat_side_x(p3, f3) == pytest.approx(fb3[2])


def test_spread_puts_equal_gaps_between_insets_and_skips_the_glue_zone():
    vent = {"kind": "vent", "placement": {"on": "P2", "y": "centred",
                                          "pattern": {"kind": "spread", "axis": "x", "count": 4, "inset": 1}}}
    d = build([vent])
    f = d.faces.faces["P2"]
    boxes = sorted(face_box(d, "f1", "P2", i) for i in range(4))
    assert boxes[0][0] == pytest.approx(1) and f.width - boxes[-1][2] == pytest.approx(1)
    gaps = [b[0] - a[2] for a, b in zip(boxes, boxes[1:])]
    assert max(gaps) - min(gaps) < 1e-9
    one = build([{**vent, "placement": {**vent["placement"], "pattern": {"kind": "spread", "axis": "x",
                                                                          "count": 1, "inset": 1}}}])
    b = face_box(one, "f1", "P2")
    assert (b[0] + b[2]) / 2 == pytest.approx(f.width / 2)
    on_p1 = build([{**vent, "placement": {**vent["placement"], "on": "P1"}}])
    glue = next(k for k in on_p1.faces.faces["P1"].keepouts if k.kind == "glue_landing")
    boxes = sorted(face_box(on_p1, "f1", "P1", i) for i in range(4))
    assert boxes[0][0] >= glue.rect[2] + 1 - 1e-9  # the inset runs from the usable edge, past the glue zone
    with pytest.raises(ValueError, match="don't fit"):
        _refused([{**vent, "placement": {**vent["placement"], "pattern": {"kind": "spread", "axis": "x",
                                                                           "count": 40, "inset": 1}}}])


def test_row_and_grid_patterns_place_the_whole_pattern_box():
    row = {"kind": "vent", "placement": {"on": "P2", "x": {"from": "left", "to": "near_edge", "distance": 2},
                                         "y": "centred", "pattern": {"kind": "row", "count": 3, "pitch": 1}}}
    d = build([row])
    boxes = sorted(face_box(d, "f1", "P2", i) for i in range(3))
    assert boxes[0][0] == pytest.approx(2) and boxes[1][0] - boxes[0][0] == pytest.approx(1)
    p = payload([{**row, "placement": {**row["placement"], "pattern": {"kind": "grid", "cols": 2, "rows": 2,
                                                                       "pitch_x": 1, "pitch_y": 1}}}])
    assert feature(p)["copies"] == 4


@pytest.mark.parametrize("style,opts", [
    ("reg-glio-nx", {"order": "eses"}), ("reg-glio-nx", {"order": "sese"}),
    ("reg-glio-nx", {"closure": "outside"}), ("reg-gli-ext", {}), ("hsc-gl", {"use": "tray"}),
    ("hsc-gl", {"use": "lid"}), ("tube-glued", {}),
])
@pytest.mark.parametrize("dims", [(14, 10, 12), (12, 12, 12)])  # L = W too
def test_selector_matrix_places_on_roles_not_sizes(style, opts, dims):
    L, W, D = dims
    vent = {"kind": "vent", "placement": {"on": "end_panels", "x": "centred", "y": "centred"}}
    p = payload([vent], style=style, length=L, width=W, depth=D, options=opts)
    ends = sorted(feature(p)["faces"])
    assert ends == (["P2", "P4"] if opts.get("order") == "sese" else ["P1", "P3"])
    top = payload([{"kind": "vent", "placement": {"on": "top_flaps", "x": "centred", "y": "centred"}}],
                  style=style, length=L, width=W, depth=D, options=opts)
    if style == "tube-glued":
        assert feature(top)["status"] == "stale" and "no top flaps" in feature(top)["reason"]
    elif style == "hsc-gl" and opts["use"] == "tray":
        assert feature(top)["status"] == "stale"  # a tray's flap row is its bottom
        bottom = payload([{"kind": "vent", "placement": {"on": "bottom_flaps", "x": "centred", "y": "centred"}}],
                         style=style, length=L, width=W, depth=D, options=opts)
        assert feature(bottom)["faces"] == ["bottom_flap_P1", "bottom_flap_P2", "bottom_flap_P3", "bottom_flap_P4"]
    else:
        assert len(feature(top)["faces"]) == 4 and feature(top)["status"] == "ok"
    if style == "reg-gli-ext":
        ext = payload([{"kind": "vent", "diameter": "1/4",
                        "placement": {"on": "glue_lug_ext_top", "x": "centred", "y": "centred"}}], style=style,
                      length=L, width=W, depth=D)
        assert feature(ext)["status"] == "ok"
        assert "glue_lug_ext_top" not in payload([{"kind": "vent", "placement": {
            "on": "top_flaps", "x": "centred", "y": "centred"}}], style=style)["features"][0]["faces"]


def test_a_face_named_by_id_goes_stale_when_its_role_changes():
    from dieline_engine.designstore import with_features

    p, touched = with_features(BoxParams.model_validate(BOX), [{"kind": "vent", "placement": {
        "on": "P1", "x": "centred", "y": "centred"}}])
    assert p.features[0]["bound"] == {"P1": "end"}
    swapped = BoxParams.model_validate({**p.model_dump(), "options": {"order": "sese"}})
    f = feature(designs.create_box(swapped))
    assert f["status"] == "stale" and "was an end panel and is now a side panel" in f["reason"]
    by_role, _ = with_features(BoxParams.model_validate(BOX), [{"kind": "vent", "placement": {
        "on": "end_panels", "x": "centred", "y": "centred"}}])
    swapped = BoxParams.model_validate({**by_role.model_dump(), "options": {"order": "sese"}})
    assert feature(designs.create_box(swapped))["faces"] == ["P2", "P4"]  # follows its role


# ------------------------------------------------------------------ topology contacts


def test_hand_flap_cut_ends_on_its_hinge_and_the_board_stays_one_piece():
    for prof, extra in (("obround", {}), ("rect", {"width": 3, "height": 1.5}), ("d_shape", {"width": 3,
                                                                                               "height": 1.5}),
                        ("circle", {"diameter": 2}), ("angled", {"width": 3, "height": 1.5, "taper_deg": 20})):
        flap = {"kind": "hand_flap", "profile": prof, **extra, "double_crease": True, "relief_nicks": True,
                "placement": {"on": "side_panels", "x": "centred", "y": {"from": "top_score", "to": "near_edge",
                                                                          "distance": 2}}}
        d = build([flap])
        f = next(x for x in d.extras["features"] if x.id == "f1")
        assert f.status == "ok", (prof, f.reason)
        r = validate(d)
        assert r.ok and not r.islands, (prof, r.invalid, r.islands)
        cut = [s for s in d.segments if s.op == "f1.1.cut"]
        hinge = [s for s in d.segments if s.op == "f1.1.hinge"][0]
        ends = {cut[0].start, cut[-1].end}
        assert ends == {hinge.start, hinge.end}  # the cut ends exactly on its hinge
        outer = [s for s in d.segments if s.op == "f1.1.outer"][0]
        assert math.dist(hinge.start, outer.start) == pytest.approx(2 * get_board("32 B KR").cal)


@pytest.mark.parametrize("hinge", ["top", "bottom", "left", "right"])
def test_hand_flap_hinge_sides_and_the_bottom_hinge_note(hinge):
    flap = {"kind": "hand_flap", "profile": "rect", "width": 3, "height": 3, "hinge": hinge,
            "placement": {"on": "P2", "x": "centred", "y": "centred"}}
    p = payload([flap])
    assert feature(p)["status"] == "ok"
    notes = [c for c in p["checks"] if c["id"] == "feature_f1_note"]
    assert bool(notes) == (hinge == "bottom") and (not notes or notes[0]["status"] == "info")


def test_unsupported_hand_flap_combinations_list_what_works():
    flap = {"kind": "hand_flap", "hinge": "left", "placement": {"on": "P2", "x": "centred", "y": "centred"}}
    with pytest.raises(ValueError, match="Works: obround and circle on a long side"):
        _refused([flap])
    with pytest.raises(ValueError, match="steps of 90"):
        _refused([{**flap, "hinge": "top", "placement": {**flap["placement"], "rotation_deg": 30}}])
    turned = _refused([{**flap, "placement": {**flap["placement"], "rotation_deg": 90}}])  # long side vertical
    assert feature(turned)["status"] == "ok"


def test_a_notch_joins_the_outline_and_belongs_to_it():
    d = build([{"kind": "notch", "on": "P1", "edge": "free_edge", "width": 1}])
    r = validate(d)
    assert r.ok and not r.islands
    notch = segs_of(d, "f1")
    assert notch and all(s.op == "outline" for s in notch)
    assert any(isinstance(s, Arc) for s in notch)
    v = build([{"kind": "notch", "on": "top_flaps", "edge": "free_edge", "shape": "rounded_rect", "width": 1,
                "depth": "1/2", "corner_radius": "1/8", "pattern": {"kind": "row", "count": 2, "pitch": 2},
                "along": "centred"}])
    assert validate(v).ok and len(next(x for x in v.extras["features"]).copies) == 8


def test_notches_on_creases_corners_and_each_other_are_refused():
    with pytest.raises(ValueError, match="crease"):
        _refused([{"kind": "notch", "on": "P2", "edge": "top", "width": 1}])
    with pytest.raises(ValueError, match="corner|straight"):
        _refused([{"kind": "notch", "on": "P1", "edge": "free_edge", "width": 1,
                   "along": {"from": "bottom", "to": "near_edge", "distance": "1/16"}}])
    with pytest.raises(ValueError, match="closer than"):
        _refused([{"kind": "notch", "on": "P1", "edge": "free_edge", "width": 1,
                   "pattern": {"kind": "row", "count": 2, "pitch": "1 1/16"}, "along": "centred"}])


def test_an_extra_score_ends_on_the_outline_and_crosses_the_panel_scores():
    score = {"kind": "score", "route": {"faces": "body_panels", "direction": "horizontal",
                                        "at": {"from": "bottom_score", "distance": 3}}}
    d = build([score])
    assert validate(d).ok
    (s,) = segs_of(d, "f1")
    assert s.type == T.CREASE and len(d.extras["features"][0].copies[0].crossings) == 3
    one = build([{**score, "route": {**score["route"], "faces": ["P2"]}}])  # ends on two panel scores
    assert validate(one).ok
    with pytest.raises(ValueError, match="aren't joined by a hinge"):
        _refused([{**score, "route": {**score["route"], "faces": ["P1", "P3"]}}])
    with pytest.raises(ValueError, match="slot"):
        _refused([{"kind": "score", "route": {"faces": ["top_flap_P1", "top_flap_P2"], "direction": "horizontal",
                                              "at": {"from": "hinge", "distance": 1}}}])
    jumped = _refused([{"kind": "score", "route": {"faces": ["top_flap_P1", "top_flap_P2"],
                                                   "direction": "horizontal", "slots": "jump",
                                                   "at": {"from": "hinge", "distance": 1}}}])
    assert feature(jumped)["status"] == "ok"
    vertical = _refused([{"kind": "score", "route": {"faces": ["bottom_flap_P2", "P2", "top_flap_P2"],
                                                     "direction": "vertical",
                                                     "at": {"from": "left", "distance": 2}}}])
    assert feature(vertical)["status"] == "ok"


def test_a_perforation_crosses_a_crease_at_a_tie_when_expanded():
    perf = {"kind": "perforation", "route": {"faces": "body_panels", "direction": "horizontal",
                                             "at": {"from": "bottom_score", "distance": 3}}}
    d = build([perf])
    assert validate(d).ok
    expanded = rules.expand_perforations(d)
    assert not any(s.type == T.PERFORATION for s in expanded.segments)
    r = validate(expanded)
    assert r.ok, r.invalid  # no cut touches a panel score: a tie is centred on each
    creases = [s for s in d.segments if s.op in ("hinge_P1_P2", "hinge_P2_P3", "hinge_P3_P4")]
    cuts = [s for s in expanded.segments if s.op == "f1.cuts"]
    for c in creases:
        x = c.start[0]
        near = min(min(abs(s.start[0] - x), abs(s.end[0] - x)) for s in cuts)
        # A tie is centred on the crease (3/32 each side); a cut trimmed shorter than half a tie is dropped.
        assert 3 / 32 - 1e-6 <= near <= 3 / 8 + 1e-6
    # Continuous: the pattern runs on, a cut lands on a crease, and it's flagged.
    cont = payload([{**perf, "crossing": "continuous", "phase": "1/16"}])
    flagged = [c for c in cont["checks"] if c["id"] == "feature_f1_cut_on_crease"]
    assert flagged and flagged[0]["status"] == "warn"
    dcont = build([{**perf, "crossing": "continuous", "phase": "1/16"}])
    assert not validate(rules.expand_perforations(dcont)).ok  # a cut ending on the crease is invalid there


def test_a_perforation_pattern_continues_over_a_jumped_slot():
    rule = rules.RuleSpec("perforation", cut=0.25, tie=0.25)
    pieces = rules.perf_cuts(2.0, rule, [], start=0.0)
    assert pieces[0] == (0.0, 0.25) and pieces[1] == (0.5, 0.75)
    later = rules.perf_cuts(1.0, rule, [], start=0.6)  # a second run, stationed on from the first
    assert later[0] == pytest.approx((0.6, 0.75))


def test_tear_strip_rails_tabs_and_joint():
    ts = {"kind": "tear_strip", "route": {"faces": "body_panels", "direction": "horizontal",
                                          "at": {"from": "bottom_score", "distance": 3}}}
    for tab in ("edge_tab", "angled_tab", "thumb_hole"):
        d = build([{**ts, "tab": tab}])
        f = d.extras["features"][0]
        assert f.status == "ok", (tab, f.reason)
        assert validate(d).ok and not validate(d).islands
        assert "glue_lug" in f.copies[0].faces  # glue_joint cross carries it over the lug
    stop = payload([{**ts, "glue_joint": "stop"}])
    assert "feature_f1_joint" in checks(stop)
    with pytest.raises(ValueError, match="edge tab needs the strip to start at a cut edge"):
        _refused([{**ts, "route": {**ts["route"], "faces": ["P2", "P3"]}}])
    narrow = payload([{**ts, "width": "3/8"}])
    assert "feature_f1_width" in checks(narrow)
    bc = payload([ts], board="48 BC KR")
    assert "feature_f1_doublewall" in checks(bc)
    vertical = payload([{"kind": "tear_strip", "tab": "thumb_hole", "route": {
        "faces": ["bottom_flap_P2", "P2", "top_flap_P2"], "direction": "vertical",
        "at": {"from": "left", "distance": 4}}}])
    assert feature(vertical)["status"] == "ok" and "feature_f1_flute" in checks(vertical)
    score_rails = payload([{**ts, "rails": "cut_score"}])
    assert not score_rails["targets"]["cf2"]["ready"] and not score_rails["targets"]["dxf"]["ready"]


# ------------------------------------------------------------------ feature checks


def test_carry_and_side_webs_and_the_combined_ligament():
    near_top = payload([{**HH, "placement": {**HH["placement"], "y": {"from": "top_score", "to": "near_edge",
                                                                        "distance": "3/4"}}}])
    c = checks(near_top)
    assert c["feature_f1_P1_1_carry"]["status"] == "warn" and "3/4 in" in c["feature_f1_P1_1_carry"]["message"]
    assert c["feature_f1_P1_1_carry"]["feature_id"] == "f1" and c["feature_f1_P1_1_carry"]["category"] == "feature"
    assert len(c["feature_f1_P1_1_carry"]["region"]) == 4
    ok = payload([HH])
    assert not any(k.endswith("_carry") for k in checks(ok))
    wide = payload([{**HH, "width": 9, "placement": {**HH["placement"], "on": "P1", "mirror": "none"}}])
    assert any("_side_" in k for k in checks(wide))
    vent_above = payload([HH, {"kind": "vent", "diameter": "1/4", "placement": {
        "on": "end_panels", "x": "centred", "y": {"from": "top_score", "to": "near_edge", "distance": "1"}}}])
    lig = [v for k, v in checks(vent_above).items() if k.endswith("_ligament")]
    assert lig and "vent f2" in lig[0]["message"]


def test_flutes_along_the_long_side_and_an_hsc_tray_have_their_own_notes():
    flute = payload([HH], options={"flute": "horizontal"})
    assert "feature_f1_P1_1_flute" in checks(flute)
    assert "feature_f1_P1_1_flute" not in checks(payload([HH]))
    tray = payload([{**HH, "placement": {**HH["placement"], "y": {"from": "top", "to": "near_edge",
                                                                    "distance": 2}}}], style="hsc-gl")
    carry = checks(tray)["feature_f1_P1_1_carry"]
    assert carry["status"] == "info" and "HSC tray" in carry["message"]


def test_glue_zone_overlap_band_and_near_misses():
    glue = payload([{"kind": "vent", "placement": {"on": "P1", "y": "centred",
                                                   "x": {"from": "free_edge", "to": "near_edge", "distance": 1}}}])
    msg = checks(glue)["feature_f1_P1_1_glue"]["message"]
    assert "enters the 1 1/4 in glue zone on P1 by 1/4 in" in msg
    band = payload([{"kind": "vent", "placement": {"on": "top_flap_P1", "x": "centred", "mirror": "none",
                                                   "y": {"from": "free_edge", "to": "near_edge", "distance": "1/2"}}}],
                   options={"top_flaps": "full_overlap"})
    assert any(k.endswith("_overlap") for k in checks(band))
    matched = payload([{"kind": "vent", "placement": {"on": ["top_flap_P1", "top_flap_P3"], "x": "centred",
                                                      "y": {"from": "free_edge", "to": "near_edge",
                                                            "distance": "1/2"}}}],
                      options={"top_flaps": "full_overlap"})
    assert not any(k.endswith("_overlap") for k in checks(matched))
    two = payload([{"kind": "vent", "placement": {"on": "P2", "y": "centred", "pattern": {"kind": "row", "count": 2,
                                                                                         "pitch": "11/16"},
                                                  "x": "centred"}}])
    assert any(k.endswith("_near") and "3/16 in" in v["message"] for k, v in checks(two).items())


def test_touching_features_and_outside_ones_are_refused_not_drawn():
    touching = [{"kind": "vent", "placement": {"on": "P2", "x": "centred", "y": "centred"}},
                {"kind": "vent", "placement": {"on": "P2", "y": "centred",
                                               "x": {"from": "centre_x", "to": "near_edge", "distance": "1/4"}}}]
    p = payload(touching)
    assert feature(p, "f2")["status"] == "stale" and "vent f2" in feature(p, "f2")["reason"]
    assert "overlap" in feature(p, "f2")["reason"] or "touch" in feature(p, "f2")["reason"]
    assert p["overall"] == "fail"
    assert checks(p)["feature_f2"]["status"] == "stale"
    d = build(touching)
    ghosts = [s for s in d.segments if s.type == T.ANNOTATION and d.extras["tags"].get(s, {}).get("stale")]
    assert ghosts  # drawn as a ghost, never manufactured
    assert "f2" not in write_cf2(d)  # CF2 carries no annotation


def test_provisional_limits_are_flagged_and_the_read_back_is_in_the_checks():
    p = payload([HH])
    c = checks(p)
    assert c["feature_limits"]["status"] == "warn" and c["feature_limits"]["category"] == "calibration"
    assert "provisional" in c["feature_limits"]["message"]
    assert "near edge" in c["features"]["message"] and "P1, P3" in c["features"]["message"]


def test_die_cut_features_list_only_die_cutting_machines():
    plain = payload([])
    holes = payload([HH])
    assert "Printing slotter" in next(c for c in plain["checks"] if c["id"] == "machines")["message"]
    m = next(c for c in holes["checks"] if c["id"] == "machines")["message"]
    assert "die-cutting machines only" in m and "Printing slotter die-cutter not listed" in m
    scored = payload([{"kind": "score", "route": {"faces": ["P2"], "direction": "horizontal",
                                                  "at": {"from": "bottom_score", "distance": 3}}}])
    assert "die-cut feature" not in next(c for c in scored["checks"] if c["id"] == "machines")["message"]


# ------------------------------------------------------------------ capability and outputs


def test_capability_blocks_each_target_with_the_reason():
    table = rules.table()
    assert {r["rule"]: r["status"] for r in table["cf2"]}["cut 2 pt"] == "verified"
    assert {r["rule"]: r["status"] for r in table["dxf"]}["perforation as dashed cuts"] == "unverified"
    perf = payload([{"kind": "perforation", "route": {"faces": ["P2"], "direction": "horizontal",
                                                      "at": {"from": "bottom_score", "distance": 3}}}])
    assert perf["overall"] != "fail"  # target blocks aren't in overall()
    assert not perf["targets"]["cf2"]["ready"] and "unverified" in perf["targets"]["cf2"]["blocking"][0]
    assert "unsupported" in perf["targets"]["dxf"]["blocking"][0]
    reverse = payload([{"kind": "score", "side": "reverse", "route": {"faces": ["P2"], "direction": "horizontal",
                                                                      "at": {"from": "bottom_score", "distance": 3}}}])
    assert "reverse crease" in reverse["targets"]["cf2"]["blocking"][0]
    assert "unsupported" in reverse["targets"]["dxf"]["blocking"][0]
    three = payload([{**HH, "pointage": 3}])
    assert not three["targets"]["cf2"]["ready"] and three["targets"]["dxf"]["ready"]
    assert any("mixed pointage" in b for b in three["targets"]["cf2"]["blocking"])
    plain = payload([HH])
    assert plain["targets"]["cf2"]["ready"] and plain["targets"]["dxf"]["ready"]


def test_a_blocked_target_writes_nothing_and_the_plan_shows_each_format(tmp_path):
    p = params([{"kind": "perforation", "route": {"faces": ["P2"], "direction": "horizontal",
                                                  "at": {"from": "bottom_score", "distance": 3}}}])
    with pytest.raises(TargetBlocked, match="perforation isn't verified"):
        designs.export(p, tmp_path, "perf", ["cf2"])
    with pytest.raises(TargetBlocked):
        designs.export(p, tmp_path, "perf", ["dxf"], dxf_expand_perfs=True)
    assert list(tmp_path.iterdir()) == []
    plan = exports.plan([exports.ExportPart("p1", "box", p)], tmp_path, "perf", ["cf2", "dxf"], None, True)
    assert not plan["ready"] and set(plan["formats"]) == {"cf2", "dxf"}
    assert not plan["formats"]["cf2"]["ready"] and "unverified" in plan["formats"]["dxf"]["blocking"][0]
    ok = exports.plan([exports.ExportPart("p1", "box", params([HH]))], tmp_path, "hh", ["cf2"], None)
    assert ok["ready"] and ok["formats"]["cf2"]["ready"] and ok["formats"]["dxf"]["ready"]


def test_the_expanded_perforation_dxf_reads_back_as_dashed_cuts():
    d = build([{"kind": "perforation", "route": {"faces": "body_panels", "direction": "horizontal",
                                                 "at": {"from": "bottom_score", "distance": 3}}}])
    text = write_dxf(d, expand_perfs=True)
    back, _ = read_dxf(text, "in", strict=True)
    cuts = [s for s in back.segments if s.type == T.CUT]
    assert len(cuts) > 100
    designs.check_outputs(d, {"dxf": text}, expand_perfs=True)  # reconstructs onto the expanded design
    with pytest.raises(ValueError, match="no sample-table colour"):
        write_dxf(d)


@pytest.mark.parametrize("feat", [
    HH,
    {"kind": "vent", "placement": {"on": "P2", "x": "centred", "y": "centred",
                                   "pattern": {"kind": "row", "count": 3, "pitch": 1}}},
    {"kind": "window", "shape": "ellipse", "width": 4, "height": 2, "placement": {"on": "P2", "x": "centred",
                                                                                  "y": "centred"}},
    {"kind": "hand_flap", "double_crease": True, "placement": HH["placement"]},
    {"kind": "notch", "on": "top_flaps", "edge": "free_edge", "width": 1},
])
def test_arcs_read_back_from_cf2_and_dxf(feat):
    d = build([feat])
    texts = {"cf2": write_cf2(d), "dxf": write_dxf(d)}
    designs.check_outputs(d, texts)  # each file, read back from its own text, reconstructs and validates
    back = read_cf2(texts["cf2"]).dieline
    recon, why = reconstruct(d, back)
    assert recon is not None, why
    arcs = [s for s in back.segments if isinstance(s, Arc)]
    if feat.get("kind") == "vent":
        # Full circles are written as two half arcs.
        assert len(arcs) == 6 and not any(a.full_circle for a in arcs)
    dxf_back, _ = read_dxf(texts["dxf"], "in", strict=True)
    assert reconstruct(d, dxf_back)[0] is not None


def test_job_record_keeps_features_and_regenerates_them(tmp_path):
    p = params([HH])
    out = designs.export(p, tmp_path, "hh", ["cf2"])
    import json

    record = json.loads((tmp_path / "hh.dieline.json").read_text())
    assert record["parts"][0]["params"]["features"][0]["kind"] == "handhole"
    assert record["parts"][0]["rules"]
    again = designs.open_job(record, "regenerate")
    assert again["changes"] == [] and again["design"]["features"][0]["faces"] == ["P1", "P3"]
    assert out["overall"] == "warn"


def test_the_scene_tags_feature_segments_with_feature_and_face():
    from dieline_engine.designstore import scene

    d = build([HH, {"kind": "notch", "on": "P1", "edge": "free_edge", "width": 1}])
    sc = scene(d)
    hh = [s for s in sc if s.get("feature_id") == "f1"]
    assert hh and {s["face_id"] for s in hh} == {"P1", "P3"} and all(s["provenance"] == "feature" for s in hh)
    assert all(s["op"] == "outline" for s in sc if s.get("feature_id") == "f2")
    assert any(s.get("face_id") == "P2" and s["op"] == "hinge_P1_P2" for s in sc)


def test_inch_defaults_mean_the_same_on_a_millimetre_part():
    mm = build([{**HH, "placement": {**HH["placement"], "y": {"from": "top_score", "to": "near_edge",
                                                                "distance": "50mm"}}}],
               units="mm", length=356, width=254, depth=305)
    fb = face_box(mm, "f1", "P1")
    assert (fb[2] - fb[0], fb[3] - fb[1]) == pytest.approx((3.625, 1.375))
    assert mm.faces.faces["P1"].height - fb[3] == pytest.approx(50 / 25.4)


def test_short_ties_on_thick_board_and_the_tear_strip_tab_checks():
    perf = {"kind": "perforation", "route": {"faces": ["P2"], "direction": "horizontal",
                                             "at": {"from": "bottom_score", "distance": 3}}}
    assert "feature_f1_ties" not in checks(payload([perf]))  # 32 B KR is under 3/16 caliper
    thick = payload([{**perf, "tie": "1/8"}], board="71 BC KR")
    assert thick["checks"] and "feature_f1_ties" in checks(thick)
    ts = {"kind": "tear_strip", "route": {"faces": "body_panels", "direction": "horizontal",
                                          "at": {"from": "bottom_score", "distance": 3}}}
    c = checks(payload([ts]))
    assert "glue lands on it" in c["feature_f1_tab_glue"]["message"]
    outside = checks(payload([ts], options={"closure": "outside"}))
    assert "can't be reached" in outside["feature_f1_tab_glue"]["message"]
    near = checks(payload([{"kind": "tear_strip", "tab": "angled_tab", "start": "end", "route": {
        "faces": ["P3", "P2"], "direction": "horizontal", "at": {"from": "bottom_score", "distance": 3}}}]))
    assert "feature_f1_tab_score" in near  # the tab sits at the P1/P2 score end of P2

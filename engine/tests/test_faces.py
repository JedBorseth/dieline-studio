"""Face maps for the slotted standards (docs/DESIGN-SYSTEM.md, "Faces and orientation", "Geometry contract")."""

import itertools
import math

import pytest

from dieline_engine.boards import get_board
from dieline_engine.faces import polygon_area
from dieline_engine.geometry import Arc, LineType as T
from dieline_engine.styles import STYLES, Dims, get_style
from dieline_engine.styles.slotted import raw_panels


def make(style="reg-glio-nx", dims=(14, 8, 6), board="32 B KR", **opts):
    return get_style(style).make(Dims(*dims), get_board(board), opts)


def combos():
    for sid, st in STYLES.items():
        choices = [o for o in st.options if o.choices]
        for combo in itertools.product(*[list(o.choices) for o in choices]):
            opts = dict(zip([o.id for o in choices], combo))
            for board in ("32 B KR", "48 BC KR"):
                for dims in ((14, 8, 6), (10, 10, 10), (24, 12, 13)):
                    yield sid, opts, board, dims


def _on_drawn_line(d, p, tol=1e-6):
    for s in d.segments:
        if isinstance(s, Arc):
            continue
        (ax, ay), (bx, by) = s.start, s.end
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2))
        if math.dist(p, (ax + t * dx, ay + t * dy)) <= tol:
            return True
    return False


def _outline_area(d):
    pts = [s.start for s in d.segments if s.op == "outline"]
    return polygon_area(pts)


@pytest.mark.parametrize("sid,opts,board,dims", list(combos()))
def test_faces_tile_the_blank_and_sit_on_its_lines(sid, opts, board, dims):
    d = make(sid, dims, board, **opts)
    fm = d.faces
    # Every face boundary lies on drawn cuts and creases.
    for f in fm.faces.values():
        poly = f.polygon
        for a, b in zip(poly, poly[1:] + poly[:1]):
            for k in range(5):
                p = (a[0] + (b[0] - a[0]) * k / 4, a[1] + (b[1] - a[1]) * k / 4)
                assert _on_drawn_line(d, p), (f.id, p)
    # Together they are exactly the board.
    assert math.isclose(sum(polygon_area(f.polygon) for f in fm.faces.values()), _outline_area(d), abs_tol=1e-6)
    # Frames: rigid, and an outside view of the inside-up drawing.
    for f in fm.faces.values():
        assert f.mirrored, f.id
        for q in ((0.3, 0.7), (f.width, f.height)):
            back = f.from_flat(f.to_flat(q))
            assert math.isclose(back[0], q[0], abs_tol=1e-9) and math.isclose(back[1], q[1], abs_tol=1e-9)
    # Hinges: on their crease, child on the left of the axis (inside face).
    creases = {s.op: s for s in d.of_type(T.CREASE)}
    for hg in fm.hinges.values():
        assert hg.id in creases, hg.id
        child = fm.faces[hg.child]
        cx = sum(p[0] for p in child.polygon) / len(child.polygon)
        cy = sum(p[1] for p in child.polygon) / len(child.polygon)
        ax, ay = hg.end[0] - hg.start[0], hg.end[1] - hg.start[1]
        assert ax * (cy - hg.start[1]) - ay * (cx - hg.start[0]) > 0, hg.id
        assert _on_drawn_line(d, hg.start) and _on_drawn_line(d, hg.end)
        assert fm.faces[hg.child].parent == hg.parent


@pytest.mark.parametrize("order,first", [("eses", "end"), ("sese", "side")])
def test_roles_follow_the_panel_order_even_on_a_square_box(order, first):
    fm = make(dims=(10, 10, 8), order=order).faces
    roles = [fm.faces[f"P{i}"].role for i in range(1, 5)]
    other = "side" if first == "end" else "end"
    assert roles == [first, other, first, other]
    assert fm.resolve("end_panels") == [f"P{i}" for i in range(1, 5) if fm.faces[f"P{i}"].role == "end"]


@pytest.mark.parametrize("closure", ["inside", "outside"])
@pytest.mark.parametrize("order", ["eses", "sese"])
def test_assembled_centres_follow_the_contract(closure, order):
    b = get_board("32 B KR")
    d = make(order=order, closure=closure)
    fm = d.faces
    raw = raw_panels(14, 8, order)
    p1, p4 = fm.faces["P1"], fm.faces["P4"]
    # P1: half of (P1A + CAL) from the P1/P2 score.
    score12 = fm.hinges["hinge_P1_P2"].start[0]
    assert math.isclose(abs(p1.to_flat((p1.centre_x, 0))[0] - score12), (raw[0] + b.cal) / 2, abs_tol=1e-9)
    score34 = fm.hinges["hinge_P3_P4"].start[0]
    assert math.isclose(abs(p4.to_flat((p4.centre_x, 0))[0] - score34), (raw[3] + b.cal) / 2, abs_tol=1e-9)
    for pid in ("P2", "P3"):
        assert fm.faces[pid].centre_x == fm.faces[pid].width / 2
    # A flap's assembled centre lines up with its panel's.
    for i in range(1, 5):
        body, flap = fm.faces[f"P{i}"], fm.faces[f"top_flap_P{i}"]
        assert math.isclose(body.to_flat((body.centre_x, 0))[0], flap.to_flat((flap.centre_x, 0))[0], abs_tol=1e-9)
    # Material centres: on P1 the flap's is 3/32" off the body's (flush with the free edge, half a slot short).
    if closure == "inside":
        bx = p1.to_flat((p1.material_centre_x, 0))[0]
        f1 = fm.faces["top_flap_P1"]
        fx = f1.to_flat((f1.material_centre_x, 0))[0]
        assert math.isclose(abs(bx - fx), 3 / 32, abs_tol=1e-9)


def test_reference_lines_name_the_physical_edges():
    fm = make().faces
    p1 = fm.faces["P1"]
    assert {p1.lines["left"].kind, p1.lines["right"].kind} == {"free_edge", "score"}
    assert "free_edge" in p1.lines and "top_score" in p1.lines and "bottom_score" in p1.lines
    p4 = fm.faces["P4"]
    assert "glue_score" in p4.lines
    # Seen from outside the lug's glue score is on its left (the side toward P4) and its free end on the right.
    lug = fm.faces["glue_lug"]
    assert lug.lines["left"].kind == "glue_score" and lug.lines["right"].kind == "free_edge"
    assert math.isclose(lug.to_flat((0.0, 0.0))[0], fm.hinges["hinge_glue"].start[0])
    # Seen from outside, P1's free edge is on its left (the drawing is inside up, glue lug on the left).
    assert p1.lines["left"].kind == "free_edge"
    for f in fm.faces.values():
        for ln in f.lines.values():
            assert ln.id == f"{f.id}.{ln.name}"
            if ln.inward is not None:
                assert math.isclose(math.hypot(*ln.inward), 1.0)


def test_glue_landing_keep_out_runs_along_p1s_free_edge():
    for closure in ("inside", "outside"):
        fm = make(closure=closure).faces
        p1 = fm.faces["P1"]
        (k,) = p1.keepouts
        assert k.kind == "glue_landing" and k.side == closure
        assert k.rect == (0.0, 0.0, 1.25, p1.height)  # free edge on the left, from outside


def test_extended_lug_has_its_own_faces_and_lands_on_p1s_flaps():
    fm = make("reg-gli-ext", (14, 9, 6)).faces
    assert {"glue_lug_ext_top", "glue_lug_ext_bottom"} <= set(fm.faces)
    assert not any(f.startswith("glue_lug") for f in fm.resolve("top_flaps") + fm.resolve("bottom_flaps"))
    for end in ("top", "bottom"):
        landing = [k for k in fm.faces[f"{end}_flap_P1"].keepouts if k.kind == "glue_landing"]
        assert len(landing) == 1 and math.isclose(landing[0].rect[3], 1.25)
        x0, _, x1, _ = landing[0].rect
        assert math.isclose(x1 - x0, 1.25)  # the extension is as wide as the lug: flush with its outer edge
    lug = fm.faces["glue_lug"]
    assert lug.lines["top_score"].op == "hinge_lug_ext_top" and lug.lines["bottom_score"].op == "hinge_lug_ext_bottom"


def test_hsc_use_changes_frames_and_ids_not_the_drawing():
    tray = make("hsc-gl", use="tray")
    lid = make("hsc-gl", use="lid")
    assert tray.segments == lid.segments
    assert "bottom_flaps" in tray.faces.selectors and "top_flaps" in tray.faces.unavailable
    assert "top_flaps" in lid.faces.selectors and "bottom_flaps" in lid.faces.unavailable
    p = tray.faces.faces["P2"]
    assert p.y_axis == (0.0, -1.0) and p.x_axis == (1.0, 0.0)  # flips y, not x
    assert p.lines["bottom"].kind == "score" and p.lines["top"].kind == "open_edge"
    q = lid.faces.faces["P2"]
    assert q.y_axis == (0.0, 1.0) and q.x_axis == (-1.0, 0.0)
    assert q.lines["top"].kind == "score" and q.lines["bottom"].kind == "open_edge"


def test_selectors_and_their_errors():
    fm = make(top_flaps="regular", bottom_flaps="cssc").faces
    assert fm.resolve("top_outer_flaps") == [f for f in fm.resolve("top_flaps") if fm.faces[f].outer]
    assert all(fm.faces[f].role == "flap" and fm.faces[fm.faces[f].panel].role == "side"
               for f in fm.resolve("top_outer_flaps"))
    with pytest.raises(ValueError, match="aren't defined for full-overlap or CSSC"):
        fm.resolve("bottom_outer_flaps")
    with pytest.raises(ValueError, match="no face or selector"):
        fm.resolve("front_panel")
    assert fm.resolve(["P1", "end_panels", "P1"]) == ["P1"] + [f for f in fm.resolve("end_panels") if f != "P1"]
    assert "glue_lug" not in fm.resolve("body_panels")
    tube = make("tube-glued").faces
    with pytest.raises(ValueError, match="no top flaps"):
        tube.resolve("top_flaps")


def test_overlap_bands_follow_the_flaps_that_actually_meet():
    b = get_board("32 B KR")
    fm = make(top_flaps="gap_lap", top_lap=0.5).faces
    outer = fm.resolve("top_outer_flaps")
    for f in fm.resolve("top_flaps"):
        face = fm.faces[f]
        bands = [k for k in face.keepouts if k.kind == "overlap_band"]
        if f in outer:  # the long-side pair overlaps by about the lap, either one may be on top
            (k,) = bands
            assert k.side == "conditional" and 0 <= k.rect[1] < k.rect[3] == face.height
            # Each flap reaches its depth less IL; the walls are the inside width (8") apart.
            assert math.isclose(k.rect[3] - k.rect[1], 2 * (face.height - b.il) - 8, abs_tol=1e-9)
        else:  # the end flaps don't meet; the outer flaps lie over them
            assert not bands
            assert {k.kind for k in face.keepouts} == {"covered"} and face.keepouts[0].side == "outside"


@pytest.mark.parametrize("board", ["32 B KR", "32 C KR", "48 BC KR"])
def test_overlaps_are_the_same_whatever_the_panel_order_and_joint(board):
    seen = set()
    for order in ("eses", "sese"):
        for closure in ("inside", "outside"):
            fm = make(board=board, order=order, closure=closure, top_flaps="gap_lap", top_lap=0.5).faces
            (band,) = {round(k.rect[3] - k.rect[1], 9) for f in fm.resolve("top_outer_flaps")
                       for k in fm.faces[f].keepouts if k.kind == "overlap_band"}
            covered = {len([k for k in fm.faces[f].keepouts if k.kind == "covered"]) for f in fm.resolve("top_inner_flaps")}
            seen.add((band, tuple(sorted(covered))))
    assert len(seen) == 1, seen


def test_a_regular_rsc_has_no_overlap_bands():
    for board in ("32 B KR", "32 C KR", "48 BC KR"):
        for flaps in ("regular", "cssc"):
            fm = make(board=board, top_flaps=flaps).faces
            assert not any(k.kind == "overlap_band" for f in fm.resolve("top_flaps") for k in fm.faces[f].keepouts), (board, flaps)


def test_full_overlap_end_flaps_across_a_long_box_dont_meet():
    fm = make(dims=(24, 8, 6), bottom_flaps="full_overlap").faces
    for f in fm.resolve("bottom_flaps"):
        bands = [k for k in fm.faces[f].keepouts if k.kind == "overlap_band"]
        if fm.faces[fm.faces[f].panel].role == "side":
            assert bands and bands[0].side == "conditional"
        else:
            assert not bands  # 8" flaps can't meet across 24"
        assert not any(k.kind == "covered" for k in fm.faces[f].keepouts)  # closing order isn't defined


def test_an_overlong_lap_is_clipped_and_said():
    fm = make(top_flaps="gap_lap", top_lap=24).faces
    for f in fm.resolve("top_outer_flaps"):
        (k,) = [k for k in fm.faces[f].keepouts if k.kind == "overlap_band"]
        assert k.clipped and k.rect[1] == 0.0 and "past this flap's hinge" in k.note


def test_negative_lap_leaves_the_middle_of_the_inner_flaps_exposed():
    fm = make(top_flaps="gap_lap", top_lap=-1.0).faces
    for f in fm.resolve("top_inner_flaps"):
        face = fm.faces[f]
        covered = [k for k in face.keepouts if k.kind == "covered"]
        assert len(covered) == 2
        assert not any(k.rect[0] <= face.centre_x <= k.rect[2] for k in covered)  # the gap is exposed


def test_edges_say_exactly_what_each_boundary_is():
    fm = make().faces
    p2 = fm.faces["P2"]
    top = [e for e in p2.edges if e.side == "top"]
    assert [e.kind for e in top] == ["cut", "crease", "cut"]  # half slot, flap hinge, half slot
    assert top[1].across == "top_flap_P2" and top[1].op == "hinge_top_flap_P2"
    ts = p2.lines["top_score"]
    assert ts.op == "hinge_top_flap_P2" and math.dist(*ts.span) < math.dist(ts.start, ts.end)
    lug = fm.faces["glue_lug"]
    assert {e.side for e in lug.edges} >= {"taper"}
    fe = lug.lines["free_edge"]
    assert math.isclose(min(fe.start[1], fe.end[1]), 0.25) and math.isclose(max(fe.start[1], fe.end[1]), lug.height - 0.25)
    assert lug.lines["top"].kind == lug.lines["bottom"].kind == "datum"
    # Every face's spans cover its whole boundary.
    for f in fm.faces.values():
        per = sum(math.dist(a, b) for a, b in zip(f.polygon, f.polygon[1:] + f.polygon[:1]))
        assert math.isclose(sum(math.dist(e.start, e.end) for e in f.edges), per, abs_tol=1e-6), f.id


def test_a_tube_names_both_open_edges():
    p2 = make("tube-glued").faces.faces["P2"]
    assert "top_open_edge" in p2.lines and "bottom_open_edge" in p2.lines and "open_edge" not in p2.lines
    assert "open_edge" in make("hsc-gl").faces.faces["P2"].lines


def test_selector_errors_list_what_exists():
    tube = make("tube-glued").faces
    with pytest.raises(ValueError, match="Faces: P1"):
        tube.resolve("top_flaps")
    with pytest.raises(ValueError, match="empty selection"):
        tube.resolve([])
    cssc = make(bottom_flaps="cssc").faces
    with pytest.raises(ValueError, match="bottom_flaps"):
        cssc.resolve("bottom_outer_flaps")


def test_a_narrow_panel_clips_its_glue_landing():
    fm = make("tube-glued", dims=(14, 0.75, 6)).faces
    (k,) = fm.faces["P1"].keepouts
    assert k.clipped and k.rect[2] == fm.faces["P1"].width


def test_list_faces_says_which_way_each_face_faces():
    data = make("hsc-gl", use="tray").faces.to_dict()
    p2 = next(f for f in data["faces"] if f["id"] == "P2")
    assert "tray" in p2["orientation"] and p2["frame"]["y_axis_flat"] == (0.0, -1.0)


def test_face_map_serialises():
    data = make().faces.to_dict()
    assert data["viewing"] == "outside" and len(data["faces"]) == 13 and data["hinges"]


def test_covered_zones_start_where_the_outer_flaps_really_do():
    fm = make().faces  # 14 x 8 x 6, E-S-E-S: P1 and P3 are the ends, their flaps inner
    p3 = fm.faces["top_flap_P3"]
    covered = [k for k in p3.keepouts if k.kind == "covered"]
    assert len(covered) == 2
    # The side flaps stop half a slot short of P3's corners, so the strip by P3's hinge is exposed.
    assert all(math.isclose(k.rect[1], 3 / 32 * 2) for k in covered)
    assert not any(k.rect[0] <= 2 <= k.rect[2] and k.rect[1] <= 0.1 <= k.rect[3] for k in covered)
    # P1's flap: P2's flap stops half a slot short at the P1/P2 corner, and P4's at the joint corner.
    p1 = fm.faces["top_flap_P1"]
    assert all(math.isclose(k.rect[1], 3 / 16) for k in p1.keepouts if k.kind == "covered")


@pytest.mark.parametrize("order", ["eses", "sese"])
@pytest.mark.parametrize("closure", ["inside", "outside"])
def test_covered_zones_at_the_glue_joint_follow_the_joint(order, closure):
    b = get_board("32 B KR")
    delta = b.il if closure == "outside" else 0.0
    fm = make(order=order, closure=closure).faces
    inner = fm.resolve("top_inner_flaps")
    # The joint corner is between P4 and P1. If P1's flap is inner, P4's flap covers it from the
    # joint corner: half a slot short of the glue score. If P4's flap is inner, P1's flap covers it:
    # flush with P1's free edge, 1/8 (+ IL outside) short of the corner.
    target, coverer, expect = (("top_flap_P1", "P4", 3 / 16 - delta) if "top_flap_P1" in inner
                               else ("top_flap_P4", "P1", 1 / 8 + delta))
    zones = [k for k in fm.faces[target].keepouts if k.kind == "covered" and coverer in k.note]
    assert len(zones) == 1 and math.isclose(zones[0].rect[1], expect), (zones, expect)


def test_overlap_bands_only_cover_where_the_opposite_flap_is():
    fm = make(closure="outside", top_flaps="gap_lap", top_lap=0.5).faces
    for a, b in (("top_flap_P2", "top_flap_P4"), ("top_flap_P4", "top_flap_P2")):
        A, B = fm.faces[a], fm.faces[b]
        (band,) = [k for k in A.keepouts if k.kind == "overlap_band"]
        lo, hi = A.centre_x + B.centre_x - B.width, A.centre_x + B.centre_x
        assert band.rect[0] >= lo - 1e-9 and band.rect[2] <= hi + 1e-9


def test_a_narrow_rsc_names_each_flap_side_correctly():
    fm = make(dims=(14, 0.30, 6)).faces
    orient = fm.faces["top_flap_P1"].up
    assert "free edge side" in orient and "panel score side" in orient


def test_negative_sizes_print_as_signed_fractions():
    from dieline_engine.checks import fmt_in

    assert fmt_in(-1 / 32) == "-1/32" and fmt_in(-1.5) == "-1 1/2" and fmt_in(-0.3) == "-0.3"
    view = make("tube-glued", dims=(14, 1 / 16, 6)).faces.agent_view()
    p1 = next(f for f in view["faces"] if f["id"] == "P1")
    assert p1["centre_x_in"] == "-1/32"

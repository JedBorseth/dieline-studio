"""The contact and disposition rules (docs/DESIGN-SYSTEM.md, "Contact and disposition rules")."""

import itertools
import math

import pytest

from dieline_engine import designs
from dieline_engine.boards import get_board
from dieline_engine.cf2 import read_cf2, write_cf2
from dieline_engine.checks import CheckResult, overall, run_checks
from dieline_engine.designs import BoxParams, GeometryRefused, TargetBlocked, check_outputs
from dieline_engine.dxf import cutting_order, read_dxf, write_dxf
from dieline_engine.geometry import Arc, Dieline, Line, LineType as T, Operation
from dieline_engine.styles import STYLES, Dims, get_style
from dieline_engine.topology import reconstruct, validate


def rsc(**opts):
    return get_style("reg-glio-nx").make(Dims(14, 8, 6), get_board("32 B KR"), opts)


def panel_score_x(d: Dieline, i: int) -> float:
    return sorted({s.start[0] for s in d.of_type(T.CREASE) if s.start[0] == s.end[0]})[i]


def hole(d: Dieline, op: str, center, r, **kw):
    d.add_op(Operation(op, "hole", closed=True, **kw))
    start = (center[0] + r, center[1])
    d.add_arc(start, start, center, False, T.CUT, full_circle=True, op=op)


def statuses(d: Dieline) -> dict[str, str]:
    return {r.id: r.status for r in run_checks(d)}


@pytest.mark.parametrize("style", sorted(STYLES))
def test_every_standard_option_is_one_valid_piece(style):
    st = STYLES[style]
    choices = [o for o in st.options if o.choices]
    for combo in itertools.product(*[list(o.choices) for o in choices]):
        opts = dict(zip([o.id for o in choices], combo))
        for board in ("32 E KR", "32 B KR", "48 BC KR"):
            for dims in ((14, 8, 6), (10, 10, 10), (24, 12, 0.75), (14.0625, 8.03125, 6.09375)):
                try:
                    d = st.make(Dims(*dims), get_board(board), opts)
                except ValueError:
                    continue  # the style refuses these sizes itself
                r = validate(d)
                assert r.ok and not r.islands and r.material_faces == 1, (style, opts, board, dims, r.invalid)


def test_standard_geometry_is_tagged_with_operations():
    d = rsc()
    assert all(s.op in d.ops for s in d.segments)
    assert d.ops["outline"].closed and d.ops["outline"].kind == "outline"
    assert {o for o in d.ops if o.startswith("hinge_P")} == {"hinge_P1_P2", "hinge_P2_P3", "hinge_P3_P4"}
    assert "hinge_glue" in d.ops and "hinge_top_flap_P1" in d.ops


def test_a_hole_inside_a_panel_is_valid():
    d = rsc()
    hole(d, "vent", (panel_score_x(d, 2) + 3, 7), 0.5)
    r = validate(d)
    assert r.ok and r.material_faces == 1 and not r.islands


def test_board_inside_a_cut_out_is_an_island():
    d = rsc()
    x = panel_score_x(d, 2) + 3
    hole(d, "ring", (x, 7), 1.0)
    hole(d, "middle", (x, 7), 0.5)
    r = validate(d)
    assert r.ok and [i.kind for i in r.islands] == ["island"]
    s = statuses(d)
    assert s["material"] == "fail" and overall(run_checks(d)) == "fail"  # drawn, export blocked


def test_a_cut_across_the_blank_splits_it():
    d = get_style("tube-glued").make(Dims(14, 8, 6), get_board("32 B KR"), {})  # no flaps: body edges are outline
    _, h = d.size()
    x = panel_score_x(d, 2) + 3
    d.add_op(Operation("slit", "slit", open_ends="on", ends_on=("outline",)))
    d.add_line((x, 0), (x, h), T.CUT, op="slit")
    r = validate(d)
    assert r.ok and [i.kind for i in r.islands] == ["split"], r.invalid


def test_hole_crossing_a_score_is_refused():
    d = rsc()
    hole(d, "vent", (panel_score_x(d, 2), 7), 0.3)
    r = validate(d)
    assert not r.ok and {i.kind for i in r.invalid} == {"crossing"}


def test_hand_flap_cut_ending_on_its_hinge_is_a_permitted_junction():
    d = rsc()
    x = panel_score_x(d, 2) + 2
    d.add_op(Operation("hf_hinge", "hinge", open_ends="free"))
    d.add_line((x, 8), (x + 2, 8), T.CREASE, op="hf_hinge")
    d.add_op(Operation("hf_cut", "hole", open_ends="on", ends_on=("hf_hinge",)))
    d.add_polyline([(x, 8), (x, 7), (x + 2, 7), (x + 2, 8)], T.CUT, op="hf_cut")
    r = validate(d)
    assert r.ok and r.material_faces == 1, r.invalid
    assert statuses(d)["closed_outline"] == "pass"


def test_undeclared_open_cut_is_refused():
    d = rsc()
    x = panel_score_x(d, 2) + 2
    d.add_op(Operation("hf_hinge", "hinge", open_ends="free"))
    d.add_line((x, 8), (x + 2, 8), T.CREASE, op="hf_hinge")
    d.add_op(Operation("hf_cut", "hole"))
    d.add_polyline([(x, 8), (x, 7), (x + 2, 7), (x + 2, 8)], T.CUT, op="hf_cut")
    kinds = {i.kind for i in validate(d).invalid}
    assert kinds == {"contact", "open_end"}


def test_touching_vents_are_refused():
    d = rsc()
    x = panel_score_x(d, 2) + 3
    hole(d, "v1", (x, 7), 0.5)
    hole(d, "v2", (x + 1, 7), 0.5)
    # Tangent circles are also within the tolerance of each other for 0.0045": coincident too.
    assert "tangent" in {i.kind for i in validate(d).invalid}


def test_near_miss_warns():
    d = rsc()
    hole(d, "v1", (panel_score_x(d, 2) + 0.3, 7), 0.1, clearance=0.25)
    r = validate(d)
    assert r.ok and len(r.near_misses) == 1 and "0.2" in r.near_misses[0].message
    assert statuses(d)["clearance"] == "warn"


def test_duplicate_rule_is_an_overlap():
    d = rsc()
    d.segments.append(d.of_type(T.CREASE)[0])
    assert {i.kind for i in validate(d).invalid} == {"overlap"}


def test_cut_on_a_crease_is_refused():
    d = rsc()
    crease = next(s for s in d.segments if s.op == "hinge_P2_P3")
    d.add_op(Operation("bad", "slit", open_ends="free"))
    d.add_line(crease.start, ((crease.start[0] + crease.end[0]) / 2, (crease.start[1] + crease.end[1]) / 2),
               T.CUT, op="bad")
    assert "overlap" in {i.kind for i in validate(d).invalid}


@pytest.mark.parametrize("arc,problem", [
    (Arc((1, 0), (1, 0), (0, 0), False, T.CUT), "coincide"),  # short arc collapsed to a point, not flagged
    (Arc((1, 0), (0, 1), (0, 0), False, T.CUT, full_circle=True), "full circle"),
    (Arc((1, 0), (0, 1.01), (0, 0), False, T.CUT), "radii differ"),
    (Arc((0, 0), (0, 0), (0, 0), False, T.CUT, full_circle=True), "zero-radius"),
])
def test_arc_invariants(arc, problem):
    d = Dieline("arcs", units="in")
    d.segments.append(arc)
    issues = validate(d).invalid
    assert issues and issues[0].kind == "arc" and problem in issues[0].message


def test_full_circle_sweep_is_one_turn():
    a = Arc((1, 0), (1, 0), (0, 0), True, T.CUT, full_circle=True)
    assert a.sweep == -math.tau and math.isclose(a.length, math.tau)


def test_cf2_writes_full_circles_as_two_half_arcs():
    d = rsc()
    hole(d, "vent", (panel_score_x(d, 2) + 3, 7), 0.5)
    text = write_cf2(d)
    arcs = [row for row in text.split("\r\n") if row.startswith("A,")]
    assert len(arcs) == 2
    back = read_cf2(text).dieline
    recon, reason = reconstruct(d, back)
    assert reason is None and validate(recon).ok


def test_dxf_cuts_the_outer_perimeter_last():
    d = rsc()
    hole(d, "vent", (panel_score_x(d, 2) + 3, 7), 0.5)
    order = cutting_order(d)
    ranks = [0 if s.type != T.CUT else 1 if s.op == "vent" else 2 for s in order]
    assert ranks == sorted(ranks) and ranks[-1] == 2
    back, _ = read_dxf(write_dxf(d))
    assert [s.full_circle for s in back.segments if isinstance(s, Arc)] == [True]


def test_reconstruct_reports_a_missing_segment():
    d = rsc()
    back = read_cf2(write_cf2(d)).dieline
    back.segments.pop()
    recon, reason = reconstruct(d, back)
    assert recon is None and "missing" in reason


def test_reconstruct_reports_an_extra_segment():
    d = rsc()
    back = read_cf2(write_cf2(d)).dieline
    back.add_line((0, 0), (5, 5), T.CREASE)
    recon, reason = reconstruct(d, back)
    assert recon is None and "no counterpart" in reason


def test_export_is_blocked_when_an_output_loses_geometry(tmp_path, monkeypatch):
    real = designs.write_cf2

    def lossy(d, *a, **kw):
        return "\r\n".join(row for i, row in enumerate(real(d, *a, **kw).split("\r\n")) if i != 12)

    monkeypatch.setattr(designs, "write_cf2", lossy)
    with pytest.raises(TargetBlocked, match="CF2 not exported"):
        designs.export(BoxParams(style="reg-glio-nx", length=14, width=8, depth=6), tmp_path, "x", ["cf2"])
    assert list(tmp_path.iterdir()) == []


def test_check_outputs_accepts_real_exports():
    d = designs.build(BoxParams(style="reg-gli-ext", length=14, width=9, depth=6))
    check_outputs(d, {"cf2": write_cf2(d), "dxf": write_dxf(d)})


def test_invalid_geometry_is_refused_before_it_becomes_a_design(monkeypatch):
    real = designs.build

    def broken(params):
        d = real(params)
        d.segments.append(d.of_type(T.CREASE)[0])
        return d

    monkeypatch.setattr(designs, "build", broken)
    with pytest.raises(GeometryRefused, match="can't be made"):
        designs.create_box(BoxParams(style="reg-glio-nx", length=14, width=8, depth=6))


@pytest.mark.parametrize("found,expected", [
    (["pass", "info", "not_applicable"], "pass"),
    (["pass", "unavailable"], "warn"),
    (["warn", "info"], "warn"),
    (["warn", "stale"], "fail"),
    (["fail", "pass"], "fail"),
])
def test_overall_precedence(found, expected):
    assert overall([CheckResult(str(i), s, "") for i, s in enumerate(found)]) == expected


def test_untagged_dielines_use_inferred_operations():
    d = Dieline("hand", units="in")
    d.add_polyline([(0, 0), (10, 0), (10, 5), (0, 5)], T.CUT, closed=True)
    d.add_line((5, 0), (5, 5), T.CREASE)
    r = validate(d)
    assert r.ok and r.material_faces == 1
    d.segments.append(Line((5, 5), (5, 6), T.CUT))  # a stray open cut
    assert "open_end" in {i.kind for i in validate(d).invalid}


# --- regressions from the Phase 1a review --------------------------------------


def pad(units="in", size=10.0) -> Dieline:
    d = Dieline("pad", units=units)
    d.add_op(Operation("outline", "outline", closed=True))
    d.add_polyline([(0, 0), (size, 0), (size, size), (0, size)], T.CUT, closed=True, op="outline")
    return d


def test_a_vertex_on_a_hinge_still_crosses_it():
    d = pad()
    d.add_op(Operation("hinge", "hinge", open_ends="on", ends_on=("outline",)))
    d.add_line((0, 5), (10, 5), T.CREASE, op="hinge")
    d.add_op(Operation("slit", "slit", open_ends="free", ends_on=("hinge",)))
    d.add_polyline([(5, 4), (5, 5), (5, 6)], T.CUT, op="slit")
    assert "crossing" in {i.kind for i in validate(d).invalid}


def test_a_vertex_touching_a_hinge_from_one_side_is_a_touch():
    d = pad()
    d.add_op(Operation("hinge", "hinge", open_ends="on", ends_on=("outline",)))
    d.add_line((0, 5), (10, 5), T.CREASE, op="hinge")
    d.add_op(Operation("slit", "slit", open_ends="free", ends_on=("hinge",)))
    d.add_polyline([(4, 4), (5, 5), (6, 4)], T.CUT, op="slit")
    assert "tangent" in {i.kind for i in validate(d).invalid}


def test_a_slit_ending_just_short_of_the_outline_still_splits():
    d = pad()
    d.add_op(Operation("slit", "slit", open_ends="on", ends_on=("outline",)))
    d.add_line((5, 0.000004), (5, 9.999996), T.CUT, op="slit")
    r = validate(d)
    assert r.ok and [i.kind for i in r.islands] == ["split"]


def test_a_hole_with_a_gap_is_an_open_end_not_dropped():
    d = pad()
    d.add_op(Operation("hole", "hole", closed=True))
    d.add_polyline([(2, 2), (4, 2), (4, 4), (2, 4), (2, 2.00004)], T.CUT, op="hole")
    assert "open_end" in {i.kind for i in validate(d).invalid}


def test_concentric_circles_are_one_annulus_whatever_their_seams():
    d = Dieline("ring", units="in")
    d.add_op(Operation("outline", "outline", closed=True))
    d.add_arc((6, 5), (6, 5), (5, 5), False, T.CUT, True, "outline")
    d.add_op(Operation("hole", "hole", closed=True))
    a = math.pi / 71
    start = (5 + 0.9995 * math.cos(a), 5 + 0.9995 * math.sin(a))
    d.segments.append(Arc(start, start, (5, 5), False, T.CUT, True, "hole"))
    r = validate(d)
    assert r.ok and r.material_faces == 1 and not r.islands


def test_a_slit_cutting_a_cap_off_a_circle_splits_it():
    d = Dieline("disc", units="in")
    d.add_op(Operation("outline", "outline", closed=True))
    d.add_arc((6, 5), (6, 5), (5, 5), False, T.CUT, True, "outline")
    d.add_op(Operation("slit", "slit", open_ends="on", ends_on=("outline",)))
    p0 = (5 + math.cos(0.3), 5 + math.sin(0.3))
    p1 = (5 + math.cos(0.9), 5 + math.sin(0.9))
    d.segments.append(Line(p0, p1, T.CUT, "slit"))
    r = validate(d)
    assert r.ok and [i.kind for i in r.islands] == ["split"]
    # A chord meeting the circle at 0.7 degrees runs within the tolerance of it for 0.0008":
    # coincident rules, refused (any crossing or meeting under about 3-6 degrees is).
    d.segments[-1] = Line((5 + math.cos(0.03), 5 + math.sin(0.03)), (5 + math.cos(0.055), 5 + math.sin(0.055)),
                          T.CUT, "slit")
    assert "overlap" in {i.kind for i in validate(d).invalid}


def test_near_tangent_line_through_a_circle_is_two_crossings():
    d = pad()
    hole(d, "c", (5, 5), 1.0)
    d.add_op(Operation("crease", "hinge", open_ends="free"))
    d.segments.append(Line((4.996, 5.999995), (4.998, 5.999995), T.CREASE, "crease"))
    assert "crossing" in {i.kind for i in validate(d).invalid}


def test_read_back_needs_every_segment_covered():
    d = pad()
    d.add_op(Operation("slit", "slit", open_ends="free"))
    d.add_line((3, 3), (3.001, 3), T.CUT, op="slit")
    without = pad()
    with pytest.raises(TargetBlocked, match="missing"):
        check_outputs(d, {"cf2": write_cf2(without)})


def test_strict_dxf_read_back_refuses_a_degenerate_arc():
    d = pad()
    hole(d, "c", (5, 5), 1.0)
    text = write_dxf(d).replace("CIRCLE", "ARC")
    with pytest.raises(TargetBlocked):
        check_outputs(d, {"dxf": text})


def test_wide_inch_blank_exports_dxf(tmp_path):
    out = designs.export(BoxParams(style="reg-glio-nx", length=100, width=60, depth=6), tmp_path, "wide",
                         ["cf2", "dxf"])
    assert len(out["paths"]) == 2


def test_mm_design_dxf_reads_back():
    d = pad(units="mm")
    check_outputs(d, {"cf2": write_cf2(d), "dxf": write_dxf(d)})


def test_reversed_arc_in_dxf_is_the_same_path():
    d = pad()
    d.add_op(Operation("semi", "hole", closed=True))
    d.add_arc((4, 4), (6, 4), (5, 4), True, T.CUT, op="semi")
    d.add_line((6, 4), (4, 4), T.CUT, op="semi")
    assert validate(d).ok
    check_outputs(d, {"cf2": write_cf2(d), "dxf": write_dxf(d)})


def test_close_parallel_creases_each_find_their_own_counterpart():
    d = pad()
    d.add_op(Operation("c1", "hinge", open_ends="free"))
    d.add_op(Operation("c2", "hinge", open_ends="free"))
    d.add_line((2, 2), (2, 4), T.CREASE, op="c1")
    d.add_line((2.00015, 2), (2.00015, 4), T.CREASE, op="c2")
    assert validate(d).ok
    check_outputs(d, {"cf2": write_cf2(d)})


def test_board_needs_an_outline_and_rules_must_lie_on_it():
    d = Dieline("hole only", units="in")
    hole(d, "c", (5, 5), 1.0)
    assert "outline" in {i.kind for i in validate(d).invalid}
    d = pad()
    d.add_op(Operation("h", "hinge", open_ends="on", ends_on=("outline",)))
    d.add_polyline([(0, 2), (-2, 2), (-2, 8), (0, 8)], T.CREASE, op="h")
    assert "off_board" in {i.kind for i in validate(d).invalid}


def test_partial_arc_with_a_full_turn_sweep_is_refused():
    d = pad()
    d.add_op(Operation("a", "hinge", open_ends="free"))
    d.segments.append(Arc((6, 5), (6.00005, 5), (5, 5), False, T.CREASE, False, "a"))
    assert "arc" in {i.kind for i in validate(d).invalid}


def test_near_miss_is_measured_on_the_true_arc():
    d = pad()
    hole(d, "c", (2, 4), 1.0, clearance=0.1002)
    d.add_op(Operation("s", "hinge", open_ends="free"))
    d.segments.append(Line((3.099366, 4.038666), (3.098481, 4.058647), T.CREASE, "s"))
    assert validate(d).near_misses


def test_a_permitted_junction_doesnt_hide_a_later_near_miss():
    d = pad()
    d.add_op(Operation("h", "hinge", open_ends="on", ends_on=("outline",)))
    d.add_line((5, 0), (5, 10), T.CREASE, op="h")
    d.add_op(Operation("slit", "slit", open_ends="free", ends_on=("h",), clearance=0.25))
    d.add_polyline([(5, 2), (6, 3), (6, 7), (5.1, 8)], T.CUT, op="slit")
    r = validate(d)
    assert r.ok and r.near_misses, r.invalid


def test_an_open_path_cannot_branch():
    d = pad()
    d.add_op(Operation("h", "hinge", open_ends="free"))
    for end in ((3, 3), (5, 3), (4, 4)):
        d.add_line((4, 3), end, T.CREASE, op="h")
    assert "branch" in {i.kind for i in validate(d).invalid}


def test_unverified_rules_are_blocked_per_target(tmp_path, monkeypatch):
    real = designs.build

    def with_perf(params):
        d = real(params)
        x = panel_score_x(d, 2) + 2
        d.add_op(Operation("perf", "perf", open_ends="free"))
        d.add_line((x, 7), (x + 1, 7), T.PERFORATION, op="perf")
        return d

    monkeypatch.setattr(designs, "build", with_perf)
    for fmt in ("cf2", "dxf"):
        with pytest.raises(TargetBlocked, match="perforation isn't verified"):
            designs.export(BoxParams(style="reg-glio-nx", length=14, width=8, depth=6), tmp_path, "p", [fmt])
    assert list(tmp_path.iterdir()) == []


def test_two_thousand_segment_loop_is_fast():
    import time

    d = pad(size=20)
    d.add_op(Operation("art", "hole", closed=True))
    n = 2000
    pts = [(10 + 2 * math.cos(math.tau * i / n), 10 + 2 * math.sin(math.tau * i / n)) for i in range(n)]
    d.add_polyline(pts, T.CUT, closed=True, op="art")
    t = time.perf_counter()
    assert validate(d).ok
    check_outputs(d, {"cf2": write_cf2(d), "dxf": write_dxf(d)})
    assert time.perf_counter() - t < 3.0


def test_vent_grid_with_clearance_is_fast():
    import time

    d = pad(size=21)
    for i in range(20):
        for j in range(20):
            hole(d, f"v{i}_{j}", (1 + i, 1 + j), 0.2, clearance=0.03)
    t = time.perf_counter()
    r = validate(d)
    assert r.ok and r.material_faces == 1 and not r.islands
    assert time.perf_counter() - t < 3.0


# --- regressions from the second Phase 1a review (Gemini) ---------------------


def rounded_pad(w=10.0, h=6.0, r=0.5) -> Dieline:
    d = Dieline("rounded", units="in")
    d.add_op(Operation("outline", "outline", closed=True))
    o = "outline"
    d.add_line((r, 0), (w - r, 0), T.CUT, o)
    d.add_arc((w - r, 0), (w, r), (w - r, r), False, T.CUT, op=o)
    d.add_line((w, r), (w, h - r), T.CUT, o)
    d.add_arc((w, h - r), (w - r, h), (w - r, h - r), False, T.CUT, op=o)
    d.add_line((w - r, h), (r, h), T.CUT, o)
    d.add_arc((r, h), (0, h - r), (r, h - r), False, T.CUT, op=o)
    d.add_line((0, h - r), (0, r), T.CUT, o)
    d.add_arc((0, r), (r, 0), (r, r), False, T.CUT, op=o)
    return d


def obround(d: Dieline, op: str, c, length, width, angle=0.0, cw=False):
    """A closed obround cut-out (two arcs, two lines), rotated about its centre;
    traversed clockwise when cw."""
    r = width / 2
    s = length / 2 - r
    ca, sa = math.cos(angle), math.sin(angle)
    rot = lambda x, y: (c[0] + x * ca - y * sa, c[1] + x * sa + y * ca)  # noqa: E731
    p0, p1, p2, p3 = rot(-s, -r), rot(s, -r), rot(s, r), rot(-s, r)
    right, left = rot(s, 0), rot(-s, 0)
    d.add_op(Operation(op, "hole", closed=True))
    if not cw:
        d.add_line(p0, p1, T.CUT, op)
        d.add_arc(p1, p2, right, False, T.CUT, op=op)
        d.add_line(p2, p3, T.CUT, op)
        d.add_arc(p3, p0, left, False, T.CUT, op=op)
    else:
        d.add_line(p1, p0, T.CUT, op)
        d.add_arc(p0, p3, left, True, T.CUT, op=op)
        d.add_line(p3, p2, T.CUT, op)
        d.add_arc(p2, p1, right, True, T.CUT, op=op)


def test_rounded_corner_pad_exports_both_formats():
    d = rounded_pad()
    assert validate(d).ok
    check_outputs(d, {"cf2": write_cf2(d), "dxf": write_dxf(d)})


def test_clockwise_obround_handhole_exports_dxf():
    d = rsc()
    obround(d, "hh", (panel_score_x(d, 2) + 4, 7), 3.625, 1.375, cw=True)
    assert validate(d).ok, validate(d).invalid
    check_outputs(d, {"cf2": write_cf2(d), "dxf": write_dxf(d)})


def test_rotated_obround_in_obround_is_an_island_in_every_output():
    d = rsc()
    c = (panel_score_x(d, 2) + 4, 7)
    obround(d, "outer", c, 3.625, 1.375, angle=math.radians(15))
    obround(d, "inner", c, 2.5, 0.75, angle=math.radians(15))
    assert [i.kind for i in validate(d).islands] == ["island"]
    for text, reader in ((write_cf2(d), lambda t: read_cf2(t).dieline), (write_dxf(d), lambda t: read_dxf(t, "in", strict=True)[0])):
        recon, reason = reconstruct(d, reader(text))
        assert reason is None
        assert [i.kind for i in validate(recon).islands] == ["island"]


def test_odd_thirty_seconds_and_rotated_shapes_read_back():
    d = rsc()
    x = 20.03125
    d.add_op(Operation("D", "hole", closed=True))
    d.add_arc((x, 6), (x, 8), (x, 7), False, T.CUT, op="D")
    d.add_line((x, 8), (x, 6), T.CUT, "D")
    obround(d, "rr", (panel_score_x(d, 1) + 3, 7), 1.5, 0.5, angle=math.radians(56))
    assert validate(d).ok
    check_outputs(d, {"cf2": write_cf2(d), "dxf": write_dxf(d)})


def test_endpoints_a_hair_apart_still_close():
    d = pad()
    d.add_op(Operation("sq", "hole", closed=True))
    x = 2.03125
    d.add_polyline([(x - 1e-8, 2), (4, 2), (4, 4), (2, 4)], T.CUT, op="sq")
    d.segments.append(Line((2, 4), (x + 1e-8, 2), T.CUT, "sq"))
    assert validate(d).ok


def test_short_line_tangent_to_a_large_arc_joins():
    for deg in range(1, 90, 7):
        a = math.radians(deg)
        d = Dieline("t", units="in")
        d.add_op(Operation("p", "hinge", open_ends="free"))
        end = (8 * math.cos(a), 8 * math.sin(a))
        d.add_arc((8, 0), end, (0, 0), False, T.CREASE, op="p")
        tangent = (-math.sin(a), math.cos(a))
        d.add_line(end, (end[0] + 0.125 * tangent[0], end[1] + 0.125 * tangent[1]), T.CREASE, "p")
        from dieline_engine.topology import _Arrangement, _Piece, ensure_ops, op_of  # noqa: PLC0415
        ops = ensure_ops(d)
        arr = _Arrangement([_Piece(s, op_of(s, ops)) for s in d.segments], 1e-5)
        assert len(arr.nodes) == 3, deg  # start, shared joint, end


def test_shallow_touch_within_tolerance_is_a_contact():
    d = Dieline("t", units="in")
    d.add_op(Operation("a", "hinge", open_ends="free"))
    d.add_op(Operation("b", "hinge", open_ends="free"))
    d.add_arc((1, 0), (-1, 0), (0, 0), False, T.CREASE, op="a")
    s10, c10 = math.sin(math.radians(80)), math.cos(math.radians(80))
    d.segments.append(Line((0, 1 + 8e-6), (c10, 1 + 8e-6 + s10), T.CREASE, "b"))
    assert "contact" in {i.kind for i in validate(d).invalid}


def test_nearly_parallel_duplicates_overlap():
    d = pad(size=30)
    d.add_op(Operation("h", "hinge", open_ends="free"))
    d.add_line((18, 7), (22, 7), T.CREASE, op="h")
    d.segments.append(Line((18, 7), (21, 7.000001), T.CREASE, "h"))
    assert "overlap" in {i.kind for i in validate(d).invalid}
    d = pad(size=30)
    d.add_op(Operation("a", "hinge", open_ends="free"))
    d.add_op(Operation("b", "hinge", open_ends="free"))
    d.add_line((18, 7), (22, 7), T.CREASE, op="a")
    d.segments.append(Line((18.5, 7.000004), (21.5, 7.000007), T.CREASE, "b"))
    assert validate(d).invalid


def test_flipped_semicircle_is_not_a_match():
    d = Dieline("semi", units="in")
    d.add_op(Operation("outline", "outline", closed=True))
    d.add_arc((1, 0), (-1, 0), (0, 0), False, T.CUT, op="outline")
    d.add_line((-1, 0), (1, 0), T.CUT, "outline")
    wrong = Dieline("w", units="in")
    wrong.segments += [Arc((-1, 0), (1, 0), (0, 0), False, T.CUT), Line((-1, 0), (1, 0), T.CUT)]
    recon, reason = reconstruct(d, wrong)
    assert recon is None


def test_pinched_hand_flap_is_refused():
    d = rsc()
    x = panel_score_x(d, 2) + 2
    d.add_op(Operation("hf_hinge", "hinge", open_ends="free"))
    d.add_line((x, 8), (x + 2, 8), T.CREASE, op="hf_hinge")
    d.add_op(Operation("hf_cut", "hole", open_ends="on", ends_on=("hf_hinge",)))
    d.add_polyline([(x, 8), (x, 7), (x + 1, 8), (x + 2, 7), (x + 2, 8)], T.CUT, op="hf_cut")
    assert not validate(d).ok


def test_tangent_hand_flap_join_is_refused():
    d = rsc()
    x, y, r = panel_score_x(d, 2) + 2, 8.0, 0.6875
    d.add_op(Operation("hf_hinge", "hinge", open_ends="free"))
    d.add_line((x, y), (x + 2.25, y), T.CREASE, op="hf_hinge")
    d.add_op(Operation("hf_cut", "hole", open_ends="on", ends_on=("hf_hinge",)))
    # End arcs leave the hinge tangentially (the full-radius obround the spec forbids hinging).
    d.add_arc((x, y), (x, y - 2 * r), (x, y - r), False, T.CUT, op="hf_cut")
    d.add_line((x, y - 2 * r), (x + 2.25, y - 2 * r), T.CUT, "hf_cut")
    d.add_arc((x + 2.25, y - 2 * r), (x + 2.25, y), (x + 2.25, y - r), False, T.CUT, op="hf_cut")
    assert "tangent" in {i.kind for i in validate(d).invalid}


def test_cut_and_nick_may_both_end_where_they_end_on_the_hinge():
    d = rsc()
    x = panel_score_x(d, 2) + 2
    d.add_op(Operation("hinge", "hinge", open_ends="free"))
    d.add_line((x, 8), (x + 2, 8), T.CREASE, op="hinge")
    d.add_op(Operation("cut", "hole", open_ends="on", ends_on=("hinge",)))
    d.add_polyline([(x, 8), (x, 7), (x + 2, 7), (x + 2, 8)], T.CUT, op="cut")
    d.add_op(Operation("nick", "slit", open_ends="free", ends_on=("hinge",)))
    d.add_line((x, 8), (x - 0.2, 8.2), T.CUT, op="nick")
    assert validate(d).ok, validate(d).invalid


def test_broken_arcs_make_other_topology_checks_unavailable():
    d = Dieline("bad", units="in")
    d.segments.append(Arc((1, 0), (1, 0), (0, 0), False, T.CUT))
    d.add_line((0, 0), (10, 10), T.CUT)
    s = statuses(d)
    assert s["zero_length"] == "fail"
    assert s["closed_outline"] == s["overlaps"] == s["crossings"] == s["material"] == "unavailable"


def test_vent_pair_warns_once():
    d = pad(size=30)
    hole(d, "v0", (18, 7), 0.25, clearance=0.25)
    hole(d, "v1", (18.6, 7), 0.25, clearance=0.25)
    assert len(validate(d).near_misses) == 1


def test_non_finite_coordinates_are_refused():
    for seg in (Line((float("nan"), 0), (1, 0), T.CUT), Arc((float("inf"), 0), (1, 0), (0, 0), False, T.CUT)):
        d = Dieline("bad", units="in")
        d.segments.append(seg)
        assert validate(d).invalid[0].kind == "finite"


def test_writers_refuse_an_unflagged_collapsed_arc():
    d = Dieline("bad", units="in")
    d.segments.append(Arc((1, 0), (1, 0), (0, 0), False, T.CUT))
    for write in (write_cf2, write_dxf):
        with pytest.raises(ValueError, match="full circle"):
            write(d)


# --- regressions from the Phase 1a re-check (Sol) ------------------------------


def test_a_rule_in_scrap_is_off_board_even_through_a_mixed_vertex():
    d = pad(size=20)
    d.add_op(Operation("hole", "hole", closed=True))
    d.add_polyline([(12, 8), (12, 12), (2, 12), (2, 2), (12, 2), (12, 6)], T.CUT, op="hole")
    d.add_arc((12, 6), (12, 8), (12, 7), True, T.CUT, op="hole")
    d.add_op(Operation("c", "hinge", open_ends="free"))
    d.add_line((4, 6), (5, 6), T.CREASE, op="c")
    assert "off_board" in {i.kind for i in validate(d).invalid}
    d = pad()
    d.segments = [s for s in d.segments if not (s.start == (10, 0) and s.end == (10, 10))]
    d.add_line((10, 0), (10, 4), T.CUT, op="outline")
    d.add_arc((10, 4), (10, 6), (10, 5), True, T.CUT, op="outline")
    d.add_line((10, 6), (10, 10), T.CUT, op="outline")
    d.add_op(Operation("c", "hinge", open_ends="free"))
    d.add_line((2, 4), (3, 4), T.CREASE, op="c")
    assert validate(d).ok, validate(d).invalid


@pytest.mark.parametrize("length", [0.00002, 0.00005, 0.00015, 0.0002])
def test_short_rules_read_back_present_and_are_missed_when_omitted(length):
    d = pad()
    d.add_op(Operation("s", "slit", open_ends="free"))
    d.add_line((3, 3), (3 + length, 3), T.CUT, op="s")
    assert validate(d).ok
    check_outputs(d, {"cf2": write_cf2(d), "dxf": write_dxf(d)})
    with pytest.raises(TargetBlocked, match="missing"):
        check_outputs(d, {"cf2": write_cf2(pad())})


def test_a_pinwheel_of_slits_makes_five_pieces():
    d = pad()
    names = ["s1", "s2", "s3", "s4"]
    paths = [((3, 0), (3, 7)), ((0, 7), (7, 7)), ((7, 10), (7, 3)), ((10, 3), (3, 3))]
    for k, (name, (a, b)) in enumerate(zip(names, paths)):
        d.add_op(Operation(name, "slit", open_ends="on", ends_on=("outline", names[(k + 1) % 4], names[k - 1])))
        d.add_line(a, b, T.CUT, op=name)
    r = validate(d)
    assert r.ok, r.invalid
    assert r.material_faces == 5 and [i.kind for i in r.islands] == ["split"] * 4
    d.add_op(Operation("c", "hinge", open_ends="free"))
    d.add_line((4, 5), (6, 5), T.CREASE, op="c")
    assert validate(d).ok


def test_a_sub_tolerance_overlap_is_still_an_overlap():
    d = pad()
    d.add_op(Operation("a", "slit", open_ends="free", ends_on=("b",)))
    d.add_op(Operation("b", "slit", open_ends="free"))
    d.add_line((3, 3), (4, 3), T.CUT, op="a")
    d.add_line((3.999995, 3), (5, 3), T.CUT, op="b")
    assert "overlap" in {i.kind for i in validate(d).invalid}


def test_strict_dxf_refuses_malformed_arcs_and_radii():
    d = pad()
    hole(d, "c", (5, 5), 1.0)
    good = write_dxf(d)
    bad_arc = good.replace("CIRCLE", "ARC").replace(" 40\r\n1\r\n", " 40\r\n1\r\n 50\r\n-1383\r\n 51\r\n-1023.0000000000001\r\n")
    bad_radius = good.replace(" 40\r\n1\r\n", " 40\r\n-1\r\n")
    nan_line = good.replace("LINE\r\n  8\r\nDesign\r\n  6\r\nCONTINUOUS\r\n 62\r\n1\r\n 10\r\n0\r\n",
                            "LINE\r\n  8\r\nDesign\r\n  6\r\nCONTINUOUS\r\n 62\r\n1\r\n 10\r\nnan\r\n", 1)
    for text in (bad_arc, bad_radius, nan_line):
        assert text != good
        with pytest.raises(TargetBlocked):
            check_outputs(d, {"dxf": text})


def test_full_circle_coverage_counts_the_gap_across_its_seam():
    d = pad()
    d.add_op(Operation("c", "hinge", open_ends="free"))
    d.add_arc((6, 5), (6, 5), (5, 5), False, T.CREASE, True, "c")
    out = pad()
    g = 0.00015
    out.segments += [Arc((5 + math.cos(g), 5 + math.sin(g)), (4, 5), (5, 5), False, T.CREASE),
                     Arc((4, 5), (5 + math.cos(-g), 5 + math.sin(-g)), (5, 5), False, T.CREASE)]
    recon, reason = reconstruct(d, out)
    assert recon is None and "missing" in reason


def test_chained_welds_dont_invent_a_junction():
    d = pad()
    d.add_op(Operation("a", "slit", open_ends="free", ends_on=("b",)))
    d.add_op(Operation("b", "slit", open_ends="free", ends_on=("c",)))
    d.add_op(Operation("c", "slit", open_ends="free"))
    d.add_line((5, 5), (3, 3), T.CUT, op="a")
    d.add_line((5.000008, 5), (5, 3), T.CUT, op="b")
    d.add_line((5.000016, 5), (7, 3), T.CUT, op="c")
    assert "weld" in {i.kind for i in validate(d).invalid}


def test_long_line_through_a_circle_keeps_both_roots():
    d = pad(size=220)
    d.add_op(Operation("c", "hinge", open_ends="free"))
    d.add_arc((111, 100), (111, 100), (110, 100), False, T.CREASE, True, "c")
    d.add_op(Operation("l", "hinge", open_ends="free", crosses=("c",)))
    d.segments.append(Line((10, 100.99), (210, 100.99), T.CREASE, "l"))
    assert validate(d).ok, validate(d).invalid  # two crossings at about 8 degrees on a 200" line
    # A billionth of an inch below the top: both roots are kept, and the stretch between them
    # never leaves the circle by more than the tolerance, so it's a coincident touch.
    d.segments[-1] = Line((10, 100.999999999), (210, 100.999999999), T.CREASE, "l")
    assert "overlap" in {i.kind for i in validate(d).invalid}


def test_a_junction_doesnt_hide_a_near_miss_on_the_same_pair():
    d = pad()
    d.add_op(Operation("c", "hinge", open_ends="free"))
    d.add_arc((6, 5), (6, 5), (5, 5), False, T.CREASE, True, "c")
    d.add_op(Operation("s", "slit", open_ends="free", ends_on=("c",), clearance=0.25))
    d.add_line((4, 5), (5.9, 5), T.CUT, op="s")
    r = validate(d)
    assert r.ok and r.near_misses, r.invalid


def test_welded_contour_area_doesnt_depend_on_position():
    for x in (0.0, -50.02, 1234.5):
        d = Dieline("tiny", units="in")
        d.add_op(Operation("outline", "outline", closed=True))
        d.segments += [Line(a, b, T.CUT, "outline") for a, b in zip(
            [(x, -0.000004), (x + 0.01, -0.000004), (x + 0.01, 0.01), (x, 0.01)],
            [(x + 0.01, -0.000004), (x + 0.01, 0.01), (x, 0.01), (x, 0)])]
        r = validate(d)
        assert r.ok and r.material_faces == 1, (x, r.invalid)


def test_a_column_of_vents_is_fast():
    import time

    d = pad(size=81)
    for k in range(2000):
        hole(d, f"v{k}", (5, 0.5 + 0.04 * k), 0.01)
    t = time.perf_counter()
    r = validate(d)
    assert r.ok and r.material_faces == 1
    assert time.perf_counter() - t < 2.0


def test_refusals_show_in_the_outline_and_material_checks():
    d = Dieline("hole only", units="in")
    hole(d, "c", (5, 5), 1.0)
    assert statuses(d)["closed_outline"] == "fail"
    d = pad()
    d.add_op(Operation("c", "hinge", open_ends="free"))
    d.add_line((-3, 4), (-2, 4), T.CREASE, op="c")
    assert statuses(d)["material"] == "fail"


# --- regressions from the second Phase 1a re-check (Sol) -----------------------


@pytest.mark.parametrize("rotate", [False, True])
def test_a_hole_near_the_outline_doesnt_hide_the_blank(rotate):
    d = pad()
    if rotate:  # same geometry, outline segments in another order
        d.segments.append(d.segments.pop(0))
    hole(d, "h", (5, 0.50003), 0.5)
    r = validate(d)
    assert r.ok and r.material_faces == 1 and not r.islands, r.invalid
    check_outputs(d, {"cf2": write_cf2(d), "dxf": write_dxf(d)})


def test_close_concentric_cuts_are_one_ring_and_one_island():
    d = pad()
    hole(d, "a", (5, 5), 2.0)
    hole(d, "b", (5, 5), 1.99997)
    r = validate(d)
    assert r.ok and r.material_faces == 2 and [i.kind for i in r.islands] == ["island"]


def test_open_cuts_outside_the_board_are_refused_even_when_they_close_a_cycle():
    d = pad()
    d.add_op(Operation("u", "slit", open_ends="on", ends_on=("outline",)))
    d.add_polyline([(2, 0), (2, -1), (4, -1), (4, 0)], T.CUT, op="u")
    assert "off_board" in {i.kind for i in validate(d).invalid}
    d = pad()
    hole(d, "h", (5, 5), 2.0)
    d.add_op(Operation("v", "slit", open_ends="on", ends_on=("h",)))
    d.add_polyline([(3, 5), (5, 6), (7, 5)], T.CUT, op="v")
    assert "off_board" in {i.kind for i in validate(d).invalid}


def test_a_substitute_that_covers_nothing_is_missing():
    d = pad()
    d.add_op(Operation("s", "slit", open_ends="free"))
    d.add_line((3, 3), (3.000012, 3), T.CUT, op="s")
    out = pad()
    out.segments.append(Line((3.000012, 3.000012), (3.000024, 3.000012), T.CUT))
    recon, reason = reconstruct(d, out)
    assert recon is None


def test_consecutive_arcs_on_slightly_different_centres_arent_an_overlap():
    d = Dieline("lens", units="mm")
    d.add_op(Operation("outline", "outline", closed=True))
    k = 25.4
    d.add_polyline([(0, -5 * k), (10 * k, -5 * k), (10 * k, 5 * k), (0, 5 * k)], T.CUT, closed=True, op="outline")
    d.add_op(Operation("h", "hole", closed=True))
    d.add_arc((6 * k, 0.000005 * k), (4 * k, 0.000005 * k), (5 * k, 0.0000102 * k), False, T.CUT, op="h")
    d.add_arc((4 * k, 0.000005 * k), (6 * k, 0.000005 * k), (5 * k, -0.0000002 * k), False, T.CUT, op="h")
    assert validate(d).ok, validate(d).invalid
    check_outputs(d, {"cf2": write_cf2(d), "dxf": write_dxf(d)})


def test_a_weld_needs_contact_at_that_node():
    d = pad()
    for name in "abc":
        d.add_op(Operation(name, "slit", open_ends="free", ends_on=tuple(o for o in "abc" if o != name)))
    d.add_line((5, 5), (5, 8), T.CUT, op="a")
    d.add_line((5.000008, 5), (5.000008, 3), T.CUT, op="b")
    d.add_arc((5.000016, 5), (5, 8), (5.000008, 6.5), False, T.CUT, op="c")
    assert "weld" in {i.kind for i in validate(d).invalid}


def test_a_crease_ending_at_an_outline_corner_is_a_legitimate_three_way_node():
    for reverse in (False, True):
        d = Dieline("corner", units="in")
        d.add_op(Operation("outline", "outline", closed=True))
        pts = [(0, 0), (10, 0), (10, 5), (0, 5)]
        d.add_polyline(pts[::-1] if reverse else pts, T.CUT, closed=True, op="outline")
        d.add_op(Operation("c", "hinge", open_ends="on", ends_on=("outline",)))
        d.add_line((10, 0), (5, 5), T.CREASE, op="c")
        assert validate(d).ok, validate(d).invalid
        check_outputs(d, {"cf2": write_cf2(d), "dxf": write_dxf(d)})


def test_untagged_dielines_read_back_as_untagged():
    d = Dieline("plain", units="in")
    d.add_polyline([(0, 0), (10, 0), (10, 5), (0, 5)], T.CUT, closed=True)
    d.add_line((5, 0), (5, 5), T.CREASE)
    assert validate(d).ok
    check_outputs(d, {"cf2": write_cf2(d), "dxf": write_dxf(d)})


# --- regressions from the second Phase 1a re-check (Gemini) --------------------


def test_a_hole_outside_the_outline_is_refused():
    d = pad()
    hole(d, "vent", (20, 20), 1.0)
    assert "off_board" in {i.kind for i in validate(d).invalid}
    d.add_op(Operation("c", "hinge", open_ends="free"))
    d.add_line((19.5, 20), (20.5, 20), T.CREASE, op="c")
    assert "off_board" in {i.kind for i in validate(d).invalid}


def test_nearby_distinct_nodes_dont_tear_a_path_open():
    d = pad()
    d.add_op(Operation("c1", "hinge", open_ends="free", ends_on=("outline",)))
    d.add_op(Operation("c2", "hinge", open_ends="free", ends_on=("outline",)))
    d.add_line((5.0, 0.0), (4.0, 1.0), T.CREASE, op="c1")
    d.segments.append(Line((5.000009, 0.000009), (6.0, 0.5), T.CREASE, "c2"))
    assert "open_end" not in {i.kind for i in validate(d).invalid}
    d = pad()
    d.add_op(Operation("h", "hinge", open_ends="on", ends_on=("outline",)))
    d.add_line((0, 5), (10, 5), T.CREASE, op="h")
    d.add_op(Operation("s1", "slit", open_ends="free", ends_on=("h",)))
    d.add_op(Operation("s2", "slit", open_ends="free", ends_on=("h",)))
    d.segments += [Line((5.0, 4.999993), (6.0, 4.5), T.CUT, "s1"), Line((5.000009, 5.000007), (4.0, 5.5), T.CUT, "s2")]
    assert "open_end" not in {i.kind for i in validate(d).invalid}


@pytest.mark.parametrize("a,b,ends", [
    (((2, 5), (12, 5)), ((2, 5), (12, 5.00002)), False),  # same op, tilted: within tol for 5"
    (((2, 5), (12, 5)), ((4, 5.000004), (10, 5.000015)), True),
    (((3, 3), (4, 3)), ((3.999995, 3.000002), (5, 3.000002)), True),  # 5e-6" overlap, 2e-6" apart
])
def test_tilted_and_shifted_overlaps_are_caught(a, b, ends):
    d = pad(size=20)
    if ends:
        d.add_op(Operation("a", "hinge", open_ends="free", ends_on=("b",)))
        d.add_op(Operation("b", "hinge", open_ends="free"))
        d.segments += [Line(*a, T.CREASE, "a"), Line(*b, T.CREASE, "b")]
    else:
        d.add_op(Operation("a", "hinge", open_ends="free"))
        d.segments += [Line(*a, T.CREASE, "a"), Line(*b, T.CREASE, "a")]
    assert "overlap" in {i.kind for i in validate(d).invalid}


@pytest.mark.parametrize("deg", [15, 27, 45, 63])
@pytest.mark.parametrize("r", [0.6875, 0.5, 0.125])
def test_rotated_tangent_hand_flaps_are_refused(deg, r):
    d = pad(size=20)
    s_ = 1.125
    a = math.radians(deg)
    rot = lambda x, y: (10 + x * math.cos(a) - y * math.sin(a), 10 + x * math.sin(a) + y * math.cos(a))  # noqa: E731
    d.add_op(Operation("hf_hinge", "hinge", open_ends="free"))
    d.add_line(rot(-s_, r), rot(s_, r), T.CREASE, op="hf_hinge")
    d.add_op(Operation("hf_cut", "hole", open_ends="on", ends_on=("hf_hinge",)))
    d.add_arc(rot(s_, r), rot(s_, -r), rot(s_, 0), True, T.CUT, op="hf_cut")
    d.add_line(rot(s_, -r), rot(-s_, -r), T.CUT, "hf_cut")
    d.add_arc(rot(-s_, -r), rot(-s_, r), rot(-s_, 0), True, T.CUT, op="hf_cut")
    assert "tangent" in {i.kind for i in validate(d).invalid}


def test_no_false_near_miss_at_a_permitted_junction():
    d = pad(size=20)
    d.add_op(Operation("hf_hinge", "hinge", open_ends="free"))
    d.add_line((5, 10), (15, 10), T.CREASE, op="hf_hinge")
    d.add_op(Operation("hf_cut", "hole", open_ends="on", ends_on=("hf_hinge",), clearance=0.25))
    d.add_polyline([(6, 10), (6, 9.85), (6, 8), (14, 8), (14, 10)], T.CUT, op="hf_cut")
    r = validate(d)
    assert r.ok and not r.near_misses, (r.invalid, r.near_misses)


@pytest.mark.parametrize("line_first", [True, False])
def test_overlap_check_doesnt_depend_on_segment_order(line_first):
    d = pad(size=20)
    d.add_op(Operation("c", "hole", closed=True))
    da = 9e-6
    p0, p1 = (11, 10), (10 + math.cos(-da), 10 + math.sin(-da))
    segs = [Line(p1, p0, T.CUT, "c"), Arc(p0, p1, (10, 10), False, T.CUT, False, "c")]
    d.segments += segs if line_first else segs[::-1]
    assert "overlap" not in {i.kind for i in validate(d).invalid}


def test_large_circles_keep_two_close_intersections():
    from dieline_engine.topology import _roots_circle_circle  # noqa: PLC0415

    dx = math.sqrt(50 ** 2 - 4e-5 ** 2)
    roots = _roots_circle_circle((110 - dx, 110), 50.0, (110 + dx, 110), 50.0)
    assert len(roots) == 2 and math.isclose(math.dist(*roots), 8e-5, rel_tol=1e-3)
    # Between those roots the circles stay within the tolerance of each other: coincident.
    # Crossing at a real angle is fine.
    d = pad(size=220)
    d.add_op(Operation("a", "hinge", open_ends="free"))
    d.add_op(Operation("b", "hinge", open_ends="free", crosses=("a",)))
    dx = math.sqrt(50 ** 2 - 5.0 ** 2)  # crossing at about 11 degrees
    for name, cx in (("a", 110 - dx), ("b", 110 + dx)):
        d.segments.append(Arc((cx + 50, 110), (cx + 50, 110), (cx, 110), False, T.CREASE, True, name))
    assert validate(d).ok, validate(d).invalid


def test_near_full_turn_partial_arc_reads_back():
    d = pad(size=20)
    d.add_op(Operation("a", "hinge", open_ends="free"))
    r, da = 0.04, 0.0004  # chord 0.000016", above the tolerance
    p0 = (10 + r * math.cos(da / 2), 10 + r * math.sin(da / 2))
    p1 = (10 + r * math.cos(-da / 2), 10 + r * math.sin(-da / 2))
    d.segments.append(Arc(p0, p1, (10, 10), False, T.CREASE, False, "a"))
    assert validate(d).ok, validate(d).invalid
    check_outputs(d, {"cf2": write_cf2(d), "dxf": write_dxf(d)})


def test_closed_operations_cant_declare_open_ends():
    d = pad()
    d.ops["outline"] = Operation("outline", "outline", closed=True, open_ends="on", ends_on=("notch",))
    assert "outline" in {i.kind for i in validate(d).invalid}


# --- regressions from the Phase 1a confirmation (Sol) --------------------------


def test_a_disjoint_partial_arc_doesnt_cover_the_source():
    d = pad()
    d.add_op(Operation("s", "slit", open_ends="free"))
    d.segments.append(Arc((6, 5), (5 + math.cos(0.000012), 5 + math.sin(0.000012)), (5, 5), False, T.CUT, False, "s"))
    out = pad()
    out.segments.append(Arc((5 + math.cos(0.000013), 5 + math.sin(0.000013)),
                            (5 + math.cos(0.000025), 5 + math.sin(0.000025)), (5, 5), False, T.CUT))
    recon, _ = reconstruct(d, out)
    assert recon is None


@pytest.mark.parametrize("kind", ["line", "circle"])
def test_declared_shallow_crossings_are_still_coincident(kind):
    d = pad()
    d.add_op(Operation("c", "hinge", open_ends="free"))
    d.add_arc((6, 5), (6, 5), (5, 5), False, T.CREASE, True, "c")
    d.add_op(Operation("x", "hinge", open_ends="free", crosses=("c",)))
    if kind == "line":
        d.segments.append(Line((2, 5.9998), (8, 5.9998), T.CREASE, "x"))
    else:
        dx = math.sqrt(1 - 0.02 ** 2)
        d.segments[-1] = Arc((5 - dx + 1, 5), (5 - dx + 1, 5), (5 - dx, 5), False, T.CREASE, True, "c")
        d.segments.append(Arc((5 + dx + 1, 5), (5 + dx + 1, 5), (5 + dx, 5), False, T.CREASE, True, "x"))
    assert "overlap" in {i.kind for i in validate(d).invalid}


def test_an_untagged_rule_doesnt_switch_off_the_outline():
    d = pad()
    hole(d, "h", (20, 20), 1.0)
    d.segments.append(Line((19.5, 20), (20.5, 20), T.CREASE))  # untagged
    assert "off_board" in {i.kind for i in validate(d).invalid}


def test_absurd_coordinates_block_the_file_instead_of_crashing():
    d = pad()
    text = write_dxf(d).replace("  0\r\nENDSEC\r\n  0\r\nEOF",
                                "  0\r\nLINE\r\n  8\r\nDesign\r\n 62\r\n2\r\n 10\r\n1e308\r\n 20\r\n5\r\n"
                                " 11\r\n1e308\r\n 21\r\n6\r\n  0\r\nENDSEC\r\n  0\r\nEOF")
    with pytest.raises(TargetBlocked):
        check_outputs(d, {"dxf": text})
    big = pad()
    big.segments.append(Line((1e308, 5), (1e308, 6), T.CREASE))
    assert validate(big).invalid[0].kind == "finite"


# --- regression from the Phase 1a confirmation (Gemini) ------------------------


def _creases_into_corners(d, corners, inset, target):
    for k, (x, y) in enumerate(corners):
        sx, sy = (1 if x < 10 else -1), (1 if y < 10 else -1)
        op = f"c{k}"
        d.ops[op] = Operation(op, "hinge", open_ends="free", ends_on=(target,))
        d.segments.insert(0, Line((x + sx * inset, y + sy * inset), (x + sx * 1, y + sy * 2), T.CREASE, op))


def test_probes_use_welded_edges_so_islands_and_blanks_are_found():
    d = pad(size=20)
    d.add_op(Operation("outer", "hole", closed=True))
    d.add_polyline([(2, 2), (18, 2), (18, 18), (2, 18)], T.CUT, closed=True, op="outer")
    d.add_op(Operation("island", "hole", closed=True))
    d.add_polyline([(5, 5), (15, 5), (15, 15), (5, 15)], T.CUT, closed=True, op="island")
    _creases_into_corners(d, [(5, 5), (15, 5), (15, 15), (5, 15)], 0.000006, "island")
    r = validate(d)
    assert [i.kind for i in r.islands] == ["island"], (r.invalid, r.material_faces)
    d = pad()
    _creases_into_corners(d, [(0, 0), (10, 0), (10, 10), (0, 10)], 0.000006, "outline")
    r = validate(d)
    assert r.ok and r.material_faces == 1, r.invalid


# --- regressions from the Phase 1a final check (Sol) ---------------------------


def test_a_mirrored_disjoint_partial_arc_doesnt_cover_the_source():
    d = pad()
    d.add_op(Operation("s", "slit", open_ends="free"))
    d.segments.append(Arc((6, 5), (5 + math.cos(0.000012), 5 + math.sin(0.000012)), (5, 5), False, T.CUT, False, "s"))
    out = pad()
    out.segments.append(Arc((5 + math.cos(-0.000013), 5 + math.sin(-0.000013)),
                            (5 + math.cos(-0.000001), 5 + math.sin(-0.000001)), (5, 5), False, T.CUT))
    recon, _ = reconstruct(d, out)
    assert recon is None


def test_a_coincident_band_just_over_the_limit_is_caught():
    from dieline_engine.topology import TOL_IN, _curve_stretch, _Piece  # noqa: PLC0415

    def band(h):  # length of each band where a line at distance h stays within tol of a unit circle
        return math.sqrt((1 + TOL_IN) ** 2 - h * h) - math.sqrt(max((1 - TOL_IN) ** 2 - h * h, 0.0))

    lo, hi = 0.5, 1 - TOL_IN  # band grows as h approaches 1 (a shallower crossing)
    for _ in range(100):
        h = (lo + hi) / 2
        lo, hi = (lo, h) if band(h) > 20.04 * TOL_IN else (h, hi)
    h = (lo + hi) / 2
    assert 20.0 * TOL_IN < band(h) < 20.1 * TOL_IN
    op = Operation("x", "hinge")
    circle = _Piece(Arc((6, 5), (6, 5), (5, 5), False, T.CREASE, True, "c"), op)
    line = _Piece(Line((2, 5 + h), (8, 5 + h), T.CREASE, "x"), op)
    assert _curve_stretch(line, circle, TOL_IN) is not None
    line = _Piece(Line((2, 5 + h - 0.002), (8, 5 + h - 0.002), T.CREASE, "x"), op)  # steeper: under the limit
    assert _curve_stretch(line, circle, TOL_IN) is None

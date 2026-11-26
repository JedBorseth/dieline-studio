"""Slotted partitions (docs/DESIGN-SYSTEM.md, "Partitions" and "Fit between
parts"): the crossing model and its input modes, slot geometry and topology,
the partition checks, product-mode candidates and choose_layout, replace_set,
fit and stack checks, and multi-part exports of a set."""

import json
import math
from dataclasses import replace
from pathlib import Path

import pytest

from dieline_engine import agent as agent_mod, designs, partitions as P, partsets, render
from dieline_engine.cf2 import read_cf2
from dieline_engine.designs import BoxParams
from dieline_engine.designstore import DesignStore, new_design, remove_part, update_part
from dieline_engine.dxf import read_dxf
from dieline_engine.expressions import ExpressionError, evaluate, evaluate_all
from dieline_engine.geometry import Arc, Dieline, LineType, Operation
from dieline_engine.layouts import names_candidate
from dieline_engine.settings import SettingsStore
from dieline_engine.topology import validate

BOX = {"style": "reg-glio-nx", "length": 12, "width": 9, "depth": 6, "units": "in", "board": "32 B KR"}
B_CAL, C_CAL, BC_CAL = 1 / 8, 3 / 16, 5 / 16


def spec(mode="box_cells", **kw):
    args = {"length": 12, "width": 9, "depth": 6, "cells_long": 4, "cells_short": 3} if mode == "box_cells" else {}
    return P.spec_from_inputs(mode, {**args, **kw})


def plan(mode="box_cells", **kw):
    return P.plan_set(spec(mode, **kw))


def pieces_by_role(p):
    return {x.role: x for x in p.pieces}


def checks(params):
    return {c["id"]: c for c in designs.create_box(params)["checks"]}


def piece_params(sp, role):
    pl = P.plan_set(sp)
    return P.PartitionParams(board=pl.piece(role).board, role=role, set=sp)


# ------------------------------------------------------------------ the crossing model


def test_box_cells_equal_cells_and_the_grid_arithmetic():
    p = plan()
    lc, sc = p.spec.long_cells, p.spec.short_cells
    assert lc == pytest.approx([(12 - 3 * B_CAL) / 4] * 4) and sc == pytest.approx([(9 - 2 * B_CAL) / 3] * 3)
    # installed clear cells + walls = the box inside span
    assert sum(lc) + 3 * B_CAL == pytest.approx(12) and p.box_long == pytest.approx(12)
    assert p.box_short == pytest.approx(9)
    # slot centre, box frame: cell_0 + t/2, then + t/2 + cell + t/2
    c = lc[0]
    assert p.positions_long == pytest.approx([c + B_CAL / 2, 2 * c + 1.5 * B_CAL, 3 * c + 2.5 * B_CAL])
    pcs = pieces_by_role(p)
    long, short = pcs["long"], pcs["short"]
    assert (long.quantity, short.quantity) == (2, 3)  # cells_long = 4 takes three short pieces
    assert long.length == pytest.approx(12 - 1 / 8) and short.length == pytest.approx(9 - 1 / 8)
    # on the piece: box-frame position - C/2
    assert [s.centre for s in long.slots] == pytest.approx([x - 1 / 16 for x in p.positions_long])
    assert long.end_legs == pytest.approx((c - 1 / 16, c - 1 / 16))
    # widths from the mating caliper: B flute (1/8) -> CAL + 1/32, square bottoms (narrower than 2 x 1/8)
    assert all(s.width == pytest.approx(5 / 32) and s.bottom == "square" for s in long.slots + short.slots)
    # slot depth H/2 + 1/32 on each piece, long from the top, short from the bottom
    assert all(s.depth == pytest.approx(3 + 1 / 32) and s.edge == "top" for s in long.slots)
    assert all(s.edge == "bottom" for s in short.slots)
    assert p.play == pytest.approx(1 / 16)
    assert p.outside == pytest.approx((12 - 1 / 8, 9 - 1 / 8, 6))


def test_cell_sizes_unequal_cells_solve_the_box():
    p = plan("cell_sizes", cells_long=[2, 3, 4], cells_short=[2.5, "2 1/2"], height=5)
    assert p.box_long == pytest.approx(9 + 2 * B_CAL) and p.box_short == pytest.approx(5 + B_CAL)
    assert p.positions_long == pytest.approx([2 + B_CAL / 2, 5 + 1.5 * B_CAL])
    long = pieces_by_role(p)["long"]
    assert long.end_legs == pytest.approx((2 - 1 / 16, 4 - 1 / 16))
    assert long.height == 5 and p.spec.depth is None
    # an asymmetric plan carries the orientation note; a symmetric one doesn't
    c = checks(piece_params(p.spec, "long"))
    assert c["partition_orientation"]["status"] == "info" and "backwards" in c["partition_orientation"]["message"]
    assert c["partition_height"]["status"] == "unavailable"
    assert "partition_orientation" not in checks(piece_params(spec(), "long"))


def test_mixed_boards_use_the_mating_caliper_and_split_parts():
    p = plan(board_long="32 C KR", board_short="32 B KR")
    pcs = pieces_by_role(p)
    # long pieces are crossed by B-flute short pieces; short pieces by C-flute long pieces
    assert all(s.width == pytest.approx(B_CAL + 1 / 32) for s in pcs["long"].slots)
    assert all(s.width == pytest.approx(C_CAL + 1 / 16) for s in pcs["short"].slots)  # 1/4: round bottom
    assert all(s.bottom == "round" for s in pcs["short"].slots)
    assert p.box_short == pytest.approx(9) and sum(p.spec.short_cells) + 2 * C_CAL == pytest.approx(9)
    # one long piece in heavier board: a separate part, and the short pieces' slots differ per crossing
    q = plan(boards_long=["32 B KR", "48 BC KR"])
    roles = {x.role: x for x in q.pieces}
    assert set(roles) == {"long", "long_2", "short"} and roles["long"].quantity == roles["long_2"].quantity == 1
    assert [round(s.width, 6) for s in roles["short"].slots] == [round(5 / 32, 6), round(BC_CAL + 1 / 16, 6)]


def test_both_piece_orientations():
    swapped = pieces_by_role(plan(slot_dir="short_top"))
    assert all(s.edge == "bottom" for s in swapped["long"].slots)
    assert all(s.edge == "top" for s in swapped["short"].slots)
    # pieces mode: positions measured from the other end give the same grid
    a = plan("pieces", pieces=[{"axis": "long", "length": 10, "height": 3, "slots": [2, 5]},
                               {"axis": "short", "length": 6, "height": 3, "slots": [1.5, 3, 4.5]}])
    b = plan("pieces", pieces=[{"axis": "long", "length": 10, "height": 3, "slots": [8, 5], "from": "end"},
                               {"axis": "short", "length": 6, "height": 3, "slots": [4.5, 3, 1.5], "from": "end"}])
    assert a.spec.long_cells == pytest.approx(b.spec.long_cells)
    assert a.spec.short_cells == pytest.approx(b.spec.short_cells)


def test_pieces_mode_becomes_one_grid_or_names_the_mismatch():
    sp = spec("pieces", pieces=[{"axis": "long", "length": 10, "height": 2.75, "slots": [2.5, 5, 7.5]},
                                {"axis": "short", "length": 7, "height": 2.75, "slots": ["2 1/3", "4 2/3"]}])
    p = P.plan_set(sp)
    pcs = pieces_by_role(p)
    assert (pcs["long"].quantity, pcs["short"].quantity) == (2, 3)
    assert pcs["long"].length == pytest.approx(10) and pcs["short"].length == pytest.approx(7)
    assert [s.centre for s in pcs["long"].slots] == pytest.approx([2.5, 5, 7.5])
    assert sum(sp.long_cells) + 3 * B_CAL == pytest.approx(10 + 1 / 8)
    with pytest.raises(P.Refused, match="needs 3 short pieces"):
        spec("pieces", pieces=[{"axis": "long", "length": 10, "height": 3, "slots": [2.5, 5, 7.5]},
                               {"axis": "short", "length": 7, "height": 3, "slots": [3], "quantity": 2}])
    with pytest.raises(P.Refused, match="long piece types 1 and 2 have different slot positions"):
        spec("pieces", pieces=[{"axis": "long", "length": 10, "height": 3, "slots": [2.5, 5], "quantity": 1},
                               {"axis": "long", "length": 10, "height": 3, "slots": [2.5, 6], "quantity": 1},
                               {"axis": "short", "length": 7, "height": 3, "slots": [2, 5]}])
    with pytest.raises(P.Refused, match="don't leave a cell between slots 1 and 2"):
        spec("pieces", pieces=[{"axis": "long", "length": 10, "height": 3, "slots": [3, 3.05]},
                               {"axis": "short", "length": 7, "height": 3, "slots": [3]}])
    with pytest.raises(P.Refused, match="end cells"):
        spec("pieces", pieces=[{"axis": "long", "length": 10, "height": 3, "slots": [0.01]},
                               {"axis": "short", "length": 7, "height": 3, "slots": [3]}])


def test_comparable_to_a_shop_sample_table_set():
    """A 12-cell (4 x 3) sample-table set measured in general terms: pieces about 10 and 7 in long and
    2 3/4 in tall, slots at an equal pitch, 3/16 in wide and square, no lead-ins, and slots on both
    piece types that run well past the middle (a locking variant, not a plain half slot). The engine
    keeps its defaults: the board and box sizes of that set are unknown, so nothing is calibrated."""
    sp = spec("pieces", board="32 B KR", mouth="square",
              pieces=[{"axis": "long", "length": 10, "height": 2.75, "slots": [2.5, 5, 7.5]},
                      {"axis": "short", "length": 7, "height": 2.75, "slots": ["7/3", "14/3"]}])
    pcs = pieces_by_role(P.plan_set(sp))
    for piece, pitch in ((pcs["long"], 2.5), (pcs["short"], 7 / 3)):
        centres = [s.centre for s in piece.slots]
        assert [b - a for a, b in zip(centres, centres[1:])] == pytest.approx([pitch] * (len(centres) - 1))
        for s in piece.slots:
            assert abs(s.width - 3 / 16) <= 1 / 32  # 5/32 on B flute: within 1/32 of the sample's 3/16
            assert s.bottom == "square" and s.lead_in == 0
            assert s.depth == pytest.approx(2.75 / 2 + 1 / 32) and s.depth < 1.75  # a plain half slot


# ------------------------------------------------------------------ slot geometry and topology


def test_round_bottoms_lose_depth_and_are_compensated():
    p = plan(length=16, width=12, depth=10, cells_long=3, cells_short=2, board="48 BC KR")
    s = pieces_by_role(p)["short"].slots[0]
    assert s.width == pytest.approx(BC_CAL + 1 / 16) and s.bottom == "round"  # 3/8 >= twice the 1/8 bend radius
    loss = P.round_loss(s.width, BC_CAL)
    assert loss > 0 and s.usable == pytest.approx(5 + 1 / 32) and s.depth == pytest.approx(s.usable + loss)
    # without the compensation, the usable depths wouldn't overlap: refused
    with pytest.raises(P.Refused, match="don't overlap"):
        P.interlock(10 - 5 + 1 / 32 - loss, 5 + 1 / 32 - loss, 10)


def test_slots_are_part_of_the_closed_outline():
    sp = spec(length=16, width=12, depth=10, cells_long=3, cells_short=2, board="48 BC KR")
    d = designs.build(piece_params(sp, "short"))
    rep = validate(d)
    assert rep.ok and not rep.islands and rep.material_faces == 1
    assert {s.op for s in d.segments} == {"outline"} and d.ops["outline"].closed
    assert all(s.type == LineType.CUT for s in d.segments)
    arcs = [s for s in d.segments if isinstance(s, Arc)]
    assert len(arcs) == 1 and all(a.radius == pytest.approx(3 / 16) for a in arcs)
    # each slot meets the piece's edge at its mouth: the lead-in starts on the bottom edge
    s = pieces_by_role(P.plan_set(sp))["short"].slots[0]
    mouth = (round(s.centre - s.width / 2 - s.lead_in, 6), 0.0)
    assert any(seg.end == mouth for seg in d.segments) and any(seg.start == mouth for seg in d.segments)


def test_a_slot_as_its_own_cut_on_an_unbroken_edge_would_leave_a_slug():
    """Why a slot belongs to the outline: drawn as a separate cut ending on an
    unbroken outline (a declared contact), its slug is a separate piece of board."""
    d = Dieline("t", units="in")
    d.add_op(Operation("outline", "outline", closed=True))
    d.add_op(Operation("slot", "slot", open_ends="on", ends_on=("outline",)))
    d.add_polyline([(0, 0), (6, 0), (6, 3), (0, 3)], LineType.CUT, closed=True, op="outline")
    d.add_polyline([(2.9, 3), (2.9, 1.5), (3.1, 1.5), (3.1, 3)], LineType.CUT, op="slot")
    rep = validate(d)
    assert rep.ok  # the contact is declared
    assert rep.islands and rep.islands[0].kind == "split"  # but the slug stays: export would be blocked


def test_lead_ins_reduce_to_fit_and_warn_on_thin_rims():
    p = plan("cell_sizes", cells_long=[2, 0.3, 2], cells_short=[2, 2], height=4)
    long = pieces_by_role(p)["long"]
    assert all(s.lead_in_reduced and s.lead_in < 1 / 8 for s in long.slots)
    c = checks(piece_params(p.spec, "long"))
    assert c["partition_mouths"]["status"] == "pass" and "reduced" in c["partition_mouths"]["message"]
    q = plan("cell_sizes", cells_long=[2, 0.2, 2], cells_short=[2, 2], height=4)
    c = checks(piece_params(q.spec, "long"))
    assert c["partition_mouths"]["status"] == "warn" and "rim" in c["partition_mouths"]["message"]
    with pytest.raises(P.Refused, match="overlap"):
        plan("cell_sizes", cells_long=[2, 0.01, 2], cells_short=[2, 2], height=4)
    sq = pieces_by_role(plan(mouth="square"))["long"]
    assert all(s.lead_in == 0 for s in sq.slots)


# ------------------------------------------------------------------ partition checks


def test_no_overlap_and_cut_through_are_refused():
    with pytest.raises(P.Refused, match="don't overlap"):
        spec(overcut="-1/16")
    with pytest.raises(P.Refused, match="cuts through"):
        spec("pieces", pieces=[{"axis": "long", "length": 10, "height": 6, "slots": [5]},
                               {"axis": "short", "length": 7, "height": 0.05, "slots": [3.5]}])


def test_vertical_play_warns():
    store = DesignStore()
    out = store.mutate("s", lambda cur: partsets.add_set(None, spec(overcut="3/32"))[0])
    inter = next(c for c in out["design_checks"] if c["id"].endswith("_interlock"))
    assert inter["status"] == "warn" and "3/16" in inter["message"]


def test_residual_web():
    c = checks(piece_params(spec(depth=1.5), "long"))
    assert c["partition_web"]["status"] == "warn" and "snap along a flute" in c["partition_web"]["message"]
    c = checks(piece_params(spec(depth=1.5, flute="horizontal"), "long"))
    assert c["partition_web"]["status"] == "pass"
    c = checks(piece_params(spec(depth=0.9), "long"))
    assert c["partition_web"]["status"] == "warn" and "under 1/2" in c["partition_web"]["message"]


def test_slot_fit():
    with pytest.raises(P.Refused, match="narrower than the mating board"):
        spec(slot_width=0.1)
    c = checks(piece_params(spec(slot_width=0.25), "long"))
    assert c["partition_fit"]["status"] == "warn" and "loose" in c["partition_fit"]["message"]
    assert checks(piece_params(spec(), "long"))["partition_fit"]["status"] == "pass"


def test_small_cells_warn():
    store = DesignStore()
    sp = spec("cell_sizes", cells_long=[2, 0.3, 2], cells_short=[2, 2], height=4)
    out = store.mutate("s", lambda cur: partsets.add_set(None, sp)[0])
    cells = next(c for c in out["design_checks"] if c["id"].endswith("_cells"))
    assert cells["status"] == "warn" and "3 x CAL" in cells["message"]


def test_height_against_the_box_depth():
    c = checks(piece_params(spec(height=7), "long"))
    assert c["partition_height"]["status"] == "warn" and "lid won't close" in c["partition_height"]["message"]
    assert checks(piece_params(spec(height="D - 1/2"), "long"))["partition_height"]["status"] == "pass"
    assert P.plan_set(spec(height="D - 1/2")).spec.height_long == pytest.approx(5.5)


def test_one_by_n_needs_confirmation_and_one_by_one_is_refused():
    with pytest.raises(P.Refused, match="confirm_degenerate"):
        spec(cells_long=1, cells_short=4)
    p = plan(cells_long=1, cells_short=4, confirm_degenerate=True)
    assert [(x.role, x.quantity, len(x.slots)) for x in p.pieces] == [("long", 3, 0)]
    store = DesignStore()
    out = store.mutate("s", lambda cur: partsets.add_set(None, p.spec)[0])
    by = {c["id"].rsplit("_", 1)[-1]: c for c in out["design_checks"]}
    assert by["reciprocity"]["status"] == "not_applicable" and by["layout"]["status"] == "info"
    with pytest.raises(P.Refused, match="1 x 1"):
        spec(cells_long=1, cells_short=1, confirm_degenerate=True)


def test_reciprocity_fails_when_a_piece_goes_or_changes_alone():
    store = DesignStore()
    store.mutate("s", lambda cur: partsets.add_set(new_design(BoxParams.model_validate(BOX), "carton"), spec())[0])
    st = store.current("s")
    out = store.mutate("s", lambda cur: remove_part(cur, "p3"))
    rec = next(c for c in out["design_checks"] if c["id"].endswith("_reciprocity"))
    assert rec["status"] == "fail" and "missing" in rec["message"] and out["overall"] == "fail"
    other = piece_params(spec(cells_long=5), "long")
    out = store.mutate("s", lambda cur: update_part(st, "p2", other))
    rec = next(c for c in out["design_checks"] if c["id"].endswith("_reciprocity"))
    assert rec["status"] == "fail"


def test_unknown_inputs_and_bad_values_are_refused():
    with pytest.raises(P.Refused, match="takes no"):
        spec(cells=3)
    with pytest.raises(P.Refused, match="cells_long"):
        spec(cells_long=0)
    with pytest.raises(P.Refused, match="unknown name"):
        spec(height="H - 1")


# ------------------------------------------------------------------ faces, pictures


def test_piece_face_map_follows_the_geometry_contract():
    d = designs.build(piece_params(spec(), "long"))
    fm = d.faces
    assert list(fm.faces) == ["piece"] and not fm.faces["piece"].mirrored
    f = fm.faces["piece"]
    assert (f.width, f.height) == pytest.approx((12 - 1 / 8, 6))
    assert {"top_edge", "bottom_edge", "start_end", "end_end", "centre_x", "centre_y"} <= set(f.lines)
    assert len([k for k in f.keepouts if k.kind == "slot"]) == 3
    assert fm.resolve("pieces") == ["piece"]
    with pytest.raises(ValueError, match="no panels or flaps"):
        fm.resolve("body_panels")
    assert designs.face_map(piece_params(spec(), "long"))["faces"][0]["id"] == "piece"
    assert f.to_flat((1.0, 2.0)) == (1.0, 2.0)


def test_pictures_render():
    sp = spec()
    png, info = render.render_png(designs.build(piece_params(sp, "long")))
    assert png[:4] == b"\x89PNG"
    pl = P.plan_set(sp)
    dl = [designs.build(piece_params(sp, r)) for r in ("long", "short")]
    png, info = render.render_set_png(pl, dl, [("divider long", 2), ("divider short", 3)])
    assert png[:4] == b"\x89PNG" and info["units"] == "in"


# ------------------------------------------------------------------ the agent's tools


def _agent(tmp_path):
    ag = agent_mod.Agent(SettingsStore(tmp_path / "settings.json"), lambda e: None)
    s = agent_mod.Session("s1", turns=1)
    t = agent_mod.Turn("s1", 1, [], None)
    s.active = t
    ag.sessions["s1"] = s
    return ag, t


def _next_turn(ag, t, text):
    """The designer sends another message (as start_turn records it)."""
    s = ag.sessions["s1"]
    s.turns += 1
    s.user_messages[f"m{s.turns}"] = text
    t2 = agent_mod.Turn("s1", s.turns, [], t.binding, t.binding_seq)
    s.active = t2
    return t2


def test_create_partition_in_a_carton_reads_back_and_links(tmp_path):
    ag, t = _agent(tmp_path)
    ag._dispatch(t, "create_box", {**BOX, "label": "carton"}, "a1")
    out = ag._dispatch(t, "create_partition", {"input_mode": "box_cells", "cells_long": 4, "cells_short": 3,
                                               "contained_in": "carton", "label": "divider"}, "a2")
    assert out["_image"][:4] == b"\x89PNG"
    assert "4 x 3 cells" in out["read_back"] and "not calibrated" in out["read_back"]
    assert "flute vertical" in out["read_back"]
    assert [p["label"] for p in out["pieces"]] == ["divider long", "divider short"]
    assert [p["quantity"] for p in out["pieces"]] == [2, 3]
    (g,) = out["groups"]
    assert g["kind"] == "partition_set" and out["links"] == [
        {"id": out["links"][0]["id"], "container": "p1", "contained": g["id"]}]
    fit = next(c for c in out["design_checks"] if c["id"] == out["links"][0]["id"])
    assert fit["status"] == "pass" and "1/8 in clearance" in fit["message"]
    # a retry with the same call id is the same result, applied once
    again = ag._dispatch(t, "create_partition", {"input_mode": "box_cells", "cells_long": 4, "cells_short": 3,
                                                 "contained_in": "carton", "label": "divider"}, "a2")
    assert again["revision"] == out["revision"] and len(ag.store.current("s1").parts) == 3
    # a piece can only be relabelled with update_part; the set changes through create_partition
    with pytest.raises(ValueError, match="replace_set"):
        ag._dispatch(t, "update_part", {"part": "p2", "quantity": 4}, "a3")
    assert ag._dispatch(t, "update_part", {"part": "p2", "label": "main divider"}, "a4")["revision"] == 3
    # dry_run changes nothing
    dry = ag._dispatch(t, "create_partition", {"input_mode": "box_cells", "cells_long": 3, "cells_short": 3,
                                               "action": "replace_set", "set_id": g["id"], "dry_run": True}, "a5")
    assert dry["dry_run"] and ag.store.current("s1").revision == 3


def test_replace_set_keeps_the_group_and_matched_part_ids(tmp_path):
    ag, t = _agent(tmp_path)
    ag._dispatch(t, "create_box", {**BOX, "label": "carton"}, "a1")
    out = ag._dispatch(t, "create_partition", {"input_mode": "box_cells", "cells_long": 4, "cells_short": 3,
                                               "contained_in": "carton"}, "a2")
    gid, link = out["groups"][0]["id"], out["links"][0]["id"]
    # a direct link to the short piece, which won't survive a mixed-board replacement's renaming... it does: roles
    ag._dispatch(t, "link_fit", {"container": "carton", "contained": "p3"}, "a3")
    new = ag._dispatch(t, "create_partition", {"input_mode": "box_cells", "cells_long": 5, "cells_short": 3,
                                               "boards_long": ["32 B KR", "32 C KR"], "action": "replace_set",
                                               "set_id": gid}, "a4")
    assert [g["id"] for g in new["groups"]] == [gid]
    roles = {m["role"]: m for m in new["groups"][0]["members"]}
    assert roles["long"]["ref"] == "p2" and roles["short"]["ref"] == "p3" and roles["short"]["quantity"] == 4
    assert roles["long_2"]["ref"] == "p4"  # a new role gets a new id
    assert any(lk["id"] == link and lk["contained"] == gid for lk in new["links"])
    assert not any(c["status"] == "stale" for c in new["design_checks"])
    # back to one board: long_2 has no match and goes; a direct link to it would go stale
    ag._dispatch(t, "link_fit", {"container": "carton", "contained": "p4"}, "a5")
    back = ag._dispatch(t, "create_partition", {"input_mode": "box_cells", "cells_long": 5, "cells_short": 3,
                                                "action": "replace_set", "set_id": gid}, "a6")
    assert [p["part"] for p in back["parts"]] == ["p1", "p2", "p3"]
    assert any(c["status"] == "stale" for c in back["design_checks"])
    # remove the whole set by its id
    gone = ag._dispatch(t, "remove_part", {"part": gid}, "a7")
    assert [p["part"] for p in gone["parts"]] == ["p1"] and gone["groups"] == []


def test_product_mode_offers_candidates_with_spare_cells(tmp_path):
    ag, t = _agent(tmp_path)
    out = ag._dispatch(t, "create_partition", {"input_mode": "product", "count": 7, "item": {"diameter": 3},
                                               "item_height": 5}, "a1")
    assert ag.store.current("s1") is None and out["design_unchanged"]  # nothing drawn
    cands = out["candidates"]
    assert all(c["id"].startswith("c") for c in cands) and "spare cells" in out["note"]
    exact = [c for c in cands if c["spare_cells"] == 0]
    assert all(c["degenerate"] for c in exact)  # 7 = 1 x 7 only
    assert {c["cells"] for c in cands if not c["degenerate"]} >= {"4 x 2", "3 x 3"}
    first = cands[0]
    assert first["spare_cells"] == 1 and first["cell_in"]["long"] == pytest.approx(3 + 1 / 8)
    assert "estimate" in json.dumps(first) and "spec" not in first
    # rectangular items are tried both ways round
    rect = ag._dispatch(t, "create_partition", {"input_mode": "product", "count": 6,
                                                "item": {"length": 4, "width": 2}, "item_height": 3}, "a2")
    assert len({c["item_along_long_in"] for c in rect["candidates"]}) == 2
    with pytest.raises(ValueError, match="no layout fits"):
        ag._dispatch(t, "create_partition", {"input_mode": "product", "count": 7, "item": {"diameter": 3},
                                             "item_height": 5, "max_box": {"length": 4, "width": 4}}, "a3")


def test_choose_layout_needs_the_designer_to_name_it(tmp_path):
    ag, t = _agent(tmp_path)
    ag.sessions["s1"].user_messages["m1"] = "7 jars, 3 in across, 5 in tall; pick c1 for me"
    out = ag._dispatch(t, "create_partition", {"input_mode": "product", "count": 7, "item": {"diameter": 3},
                                               "item_height": 5}, "a1")
    cid = out["candidates"][0]["id"]
    with pytest.raises(ValueError, match="no designer message"):
        ag._dispatch(t, "choose_layout", {"candidate_id": cid, "message_id": "m9"}, "a2")
    with pytest.raises(ValueError, match="hasn't named"):  # said in the same turn the cards were offered
        ag._dispatch(t, "choose_layout", {"candidate_id": cid, "message_id": "m1"}, "a3")
    t2 = _next_turn(ag, t, "the second one looks fine")
    with pytest.raises(ValueError, match="hasn't named"):
        ag._dispatch(t2, "choose_layout", {"candidate_id": cid, "message_id": "m2"}, "b1")
    t3 = _next_turn(ag, t2, f"Go with {cid.upper()}, please.")
    done = ag._dispatch(t3, "choose_layout", {"candidate_id": cid, "message_id": "m3"}, "c1")
    gid = done["set"]
    choice = next(c for c in done["design_checks"] if c["id"].endswith("layout_choice"))
    assert "message m3" in choice["message"]
    params = ag.store.current("s1").parts[0].params
    assert params.set.chosen == {"candidate": cid, "via": "chat", "message_id": "m3"}
    assert params.set.input_mode == "product" and gid.startswith("set_")
    # the design changed: the other candidates have expired
    other = out["candidates"][1]["id"]
    t4 = _next_turn(ag, t3, f"and also {other}")
    with pytest.raises(ValueError, match="expired"):
        ag._dispatch(t4, "choose_layout", {"candidate_id": other, "message_id": "m4", "action": "add"}, "d1")


def test_degenerate_candidates_need_confirmation(tmp_path):
    ag, t = _agent(tmp_path)
    out = ag._dispatch(t, "create_partition", {"input_mode": "product", "count": 3, "item": {"diameter": 2},
                                               "item_height": 4}, "a1")
    strip = next(c for c in out["candidates"] if c["degenerate"])
    t2 = _next_turn(ag, t, f"use {strip['id']}")
    with pytest.raises(ValueError, match="confirm_degenerate"):
        ag._dispatch(t2, "choose_layout", {"candidate_id": strip["id"], "message_id": "m2"}, "b1")
    ok = ag._dispatch(t2, "choose_layout", {"candidate_id": strip["id"], "message_id": "m2",
                                            "confirm_degenerate": True}, "b2")
    assert len(ok["parts"]) == 1 and ok["pieces"][0]["slots_in"] == []


def test_a_card_click_commits_and_tells_the_model(tmp_path):
    from dieline_engine.rpc import Server

    server = Server(open(tmp_path / "out.jsonl", "w"), SettingsStore(tmp_path / "settings.json"))
    ag = server.agent
    ag.sessions["s1"] = agent_mod.Session("s1")
    t = agent_mod.Turn("s1", 1, [], None)
    ag.sessions["s1"].active = t
    out = ag._dispatch(t, "create_partition", {"input_mode": "product", "count": 4, "item": {"diameter": 2},
                                               "item_height": 4}, "a1")
    ag.sessions["s1"].active = None
    cid = out["candidates"][0]["id"]
    res = server._layout_choose({"session_id": "s1", "candidate_id": cid})
    assert res["groups"][0]["kind"] == "partition_set" and res["revision"] == 1
    assert ag.store.current("s1").parts[0].params.set.chosen == {"candidate": cid, "via": "card"}
    assert cid in ag.sessions["s1"].note
    with pytest.raises(ValueError, match="expired"):  # the design changed since it was offered
        server._layout_choose({"session_id": "s1", "candidate_id": out["candidates"][1]["id"]})


def test_candidates_expire_when_rules_or_boards_change(tmp_path, monkeypatch):
    ag, t = _agent(tmp_path)
    out = ag._dispatch(t, "create_partition", {"input_mode": "product", "count": 4, "item": {"diameter": 2},
                                               "item_height": 4}, "a1")
    cid = out["candidates"][0]["id"]
    from dieline_engine import layouts

    monkeypatch.setattr(layouts, "RULES_VERSION", "partition-rules-2")
    with pytest.raises(ValueError, match="rules changed"):
        ag.layouts.get("s1", cid, None)
    monkeypatch.setattr(layouts, "RULES_VERSION", P.RULES_VERSION)
    from dieline_engine import boards

    b = boards.BOARDS["32 B KR"]
    monkeypatch.setitem(boards.BOARDS, "32 B KR", replace(b, cal=0.13))
    with pytest.raises(ValueError, match="board values changed"):
        ag.layouts.get("s1", cid, None)
    assert names_candidate("Use c1.", "c1") and not names_candidate("use c12", "c1")


def test_user_messages_carry_ids(tmp_path, monkeypatch):
    ag = agent_mod.Agent(SettingsStore(tmp_path / "settings.json"), lambda e: None)
    monkeypatch.setattr(ag.settings, "api_key", lambda: "k")
    monkeypatch.setattr(agent_mod.threading, "Thread", lambda **kw: type("T", (), {"start": lambda self: None})())
    ag.start_turn("s1", "four cells please")
    s = ag.sessions["s1"]
    assert s.user_messages == {"m1": "four cells please"}
    assert s.active.messages[-1]["content"].startswith("[message m1]\n")


# ------------------------------------------------------------------ fit and stack checks


def _with_set(**kw):
    d = new_design(BoxParams.model_validate({**BOX, **kw.pop("box", {})}), "carton")
    d, gid = partsets.add_set(d, spec(**kw), contained_in="carton")
    return d, gid


def _design_checks(d):
    store = DesignStore()
    return {c["id"]: c for c in store.mutate("s", lambda cur: d)["design_checks"]}


def test_fit_slip_tight_loose_and_too_big():
    d, gid = _with_set()
    lk = d.links[0].id
    assert _design_checks(d)[lk]["status"] == "pass"
    tight, _ = _with_set(clearance=0.03)
    assert "tight" in _design_checks(tight)[tight.links[0].id]["message"]
    loose, _ = _with_set(box={"length": 13})
    c = _design_checks(loose)[loose.links[0].id]
    assert c["status"] == "warn" and "loose" in c["message"]
    big, _ = _with_set(length=13)
    c = _design_checks(big)[big.links[0].id]
    assert c["status"] == "warn" and "doesn't go in" in c["message"]
    tall, _ = _with_set(height=7)
    assert "lid won't close" in _design_checks(tall)[f"{tall.links[0].id}_depth"]["message"]
    assert _design_checks(d)["fit_unchecked"]["status"] == "info"


def test_two_sets_in_one_box_need_a_stack_and_the_stack_checks_depth():
    d = new_design(BoxParams.model_validate({**BOX, "depth": 12}), "carton")
    d, g1 = partsets.add_set(d, spec(depth=5.5), "tier1", contained_in="carton")
    d, g2 = partsets.add_set(d, spec(depth=5.5), "tier2", contained_in="carton")
    c = _design_checks(d)
    assert c["fit_p1_stack"]["status"] == "warn" and "without a stack" in c["fit_p1_stack"]["message"]
    d2 = replace(d, links=())
    d2, sid = partsets.add_stack(d2, [g1, g2], pads=3, pad_board="32 C KR", contained_in="carton")
    c = _design_checks(d2)
    assert c[f"stack_{sid}"]["status"] == "pass" and "11 9/16" in c[f"stack_{sid}"]["message"]  # 11 + 3 x 3/16
    d3, sid3 = partsets.add_stack(replace(d, links=()), [g1, g2], pads=8, pad_board="48 BC KR",
                                  contained_in="carton")
    assert _design_checks(d3)[f"stack_{sid3}"]["status"] == "warn"


def test_a_link_to_one_piece_or_a_missing_part():
    d, gid = _with_set()
    d, lk = partsets.add_link(d, "carton", "p2")
    c = _design_checks(d)
    assert c[lk]["status"] == "unavailable" and "link the whole set" in c[lk]["message"]
    with pytest.raises(ValueError, match="no part"):
        partsets.add_link(d, "carton", "p9")


def test_link_tools(tmp_path):
    ag, t = _agent(tmp_path)
    ag._dispatch(t, "create_box", {**BOX, "label": "base"}, "a1")
    ag._dispatch(t, "create_box", {**BOX, "style": "hsc-gl", "length": 12.5, "width": 9.5, "depth": 3,
                                   "action": "add", "label": "lid"}, "a2")
    out = ag._dispatch(t, "link_fit", {"container": "lid", "contained": "base"}, "a3")
    lk = out["links"][0]["id"]
    c = next(x for x in out["design_checks"] if x["id"] == lk)
    assert c["status"] == "pass" and "base outside" in c["message"]
    out = ag._dispatch(t, "unlink_fit", {"link": lk}, "a4")
    assert out["links"] == []


# ------------------------------------------------------------------ exports


def test_a_set_exports_as_parts_and_reads_back(tmp_path):
    from dieline_engine import exports

    store = DesignStore()
    state = partsets.add_set(new_design(BoxParams.model_validate(BOX), "carton"), spec(board="48 BC KR"),
                             contained_in="carton")[0]
    store.mutate("s", lambda cur: state)
    snap = store.current("s")
    checks_ = store.check_exportable(snap)
    payload = store.payload(snap)
    out = exports.export_design(store.export_parts(snap), tmp_path, "RL-SET", ["cf2", "dxf"],
                                design_id=snap.id, revision=snap.revision, design_checks=checks_,
                                structure={"groups": payload["groups"], "links": payload["links"]})
    names = sorted(Path(p).name for p in out["paths"])
    assert names == ["RL-SET-p1.cf2", "RL-SET-p1.dxf", "RL-SET-p2.cf2", "RL-SET-p2.dxf", "RL-SET-p3.cf2",
                     "RL-SET-p3.dxf"]
    for f in ("RL-SET-p2.cf2", "RL-SET-p3.cf2"):
        back = read_cf2((tmp_path / f).read_text()).dieline
        assert validate(back).ok and any(isinstance(s, Arc) for s in back.segments)  # round bottoms survive
    back, _ = read_dxf((tmp_path / "RL-SET-p3.dxf").read_text(), "in", strict=True)
    assert validate(back).ok
    record = json.loads(Path(out["job_path"]).read_text())
    q = {p["part_id"]: p["quantity"] for p in record["parts"]}
    assert q == {"p1": 1, "p2": 2, "p3": 3}
    assert record["groups"][0]["kind"] == "partition_set"
    assert record["parts"][1]["params"]["set"]["inputs"]["cells_long"] == 4
    # regenerating with today's rules reproduces the set with no changes
    job = exports.open_job(record, "regenerate")
    assert job["changes"] == [] and job["parts"][1]["params"]["role"] == "long"
    rebuilt = DesignStore()
    from dieline_engine.designstore import design_from_parts

    parts = [{**p, "params": designs.parse_params(p["params"])} for p in job["parts"]]
    st = design_from_parts(parts, job["groups"], job["links"])
    out2 = rebuilt.mutate("s", lambda cur: st)
    assert not any(c["status"] in ("fail", "stale") for c in out2["design_checks"])


# ------------------------------------------------------------------ expressions


def test_expressions():
    assert evaluate("D - 1/4", {"D": 6}) == 5.75
    assert evaluate("1 1/2") == 1.5 and evaluate("2:12") == 2.75
    assert evaluate("2 * CAL", {"CAL": 0.125}) == 0.25
    assert evaluate("round(W / 3, 1/16in)", {"W": 10}) == 3.3125
    assert evaluate("50mm") == pytest.approx(50 / 25.4) and evaluate("1in", units="mm") == pytest.approx(25.4)
    assert evaluate("max(W/2 - 1 1/2, 1)", {"W": 8}) == 2.5
    assert evaluate("L / W", {"L": 2, "W": 1}, kind="scalar") == 2
    for bad, msg in (("L * W", "length times a length"), ("L / W", "scalar"), ("CAL / 0", "division by zero"),
                     ("1 +", "ends too early"), ("foo + 1", "unknown name"), ("pow(2, 3)", "unknown function"),
                     ("1" * 300, "longer than")):
        with pytest.raises(ExpressionError, match=msg):
            evaluate(bad, {"L": 1, "W": 2, "CAL": 0.1})
    with pytest.raises(ExpressionError, match="inch parts"):
        evaluate("2:12", units="mm")
    assert evaluate_all({"a": "b + 1", "b": "D / 2"}, {"D": 6}) == {"b": 3, "a": 4}
    with pytest.raises(ExpressionError, match="a -> b -> a"):
        evaluate_all({"a": "b + 1", "b": "a"}, {})
    with pytest.raises(ExpressionError, match="reserved"):
        evaluate_all({"CAL": "1"}, {})
    assert math.isclose(P.spec_from_inputs("cell_sizes", {"cells_long": ["2 * CAL + 1", 2],
                                                         "cells_short": [2, 2], "height": 3}).long_cells[0], 1.25)

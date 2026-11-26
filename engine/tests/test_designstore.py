"""The design store: revisions, atomic changes, retries, snapshot exports
(docs/DESIGN-SYSTEM.md, "Design and parts" and "Agent tools / Revisions")."""

import threading
from dataclasses import replace

import json

import pytest

from dieline_engine import agent as agent_mod, designs
from dieline_engine.designs import BoxParams, GeometryRefused
from dieline_engine.designstore import (DesignChanged, DesignStore, FitLink, Group, Member, add_part, new_design,
                                        remove_part, update_part)
from dieline_engine.settings import SettingsStore

BOX = {"style": "reg-glio-nx", "length": 14, "width": 8, "depth": 6, "units": "in", "board": "32 B KR"}
LID = {**BOX, "style": "hsc-gl", "length": 14.25, "width": 8.25, "depth": 2}


def params(**kw):
    return BoxParams.model_validate({**BOX, **kw})


def test_a_new_design_has_an_id_and_revision_one():
    store, events = DesignStore(), []
    store._emit = events.append
    out = store.mutate("s", lambda cur: new_design(params()))
    assert out["revision"] == 1 and out["design_id"] and [p["part_id"] for p in out["parts"]] == ["p1"]
    assert out["cf2"] == out["parts"][0]["cf2"]  # the first part's fields sit at the top, as before
    assert events and events[-1]["type"] == "design" and events[-1]["design"]["revision"] == 1


def test_changes_move_the_revision_on_and_keep_part_ids():
    store = DesignStore()
    a = store.mutate("s", lambda cur: new_design(params()))
    b = store.mutate("s", lambda cur: add_part(cur, BoxParams.model_validate(LID), "lid"),
                     expected=(a["design_id"], 1))
    assert b["design_id"] == a["design_id"] and b["revision"] == 2
    assert [p["part_id"] for p in b["parts"]] == ["p1", "p2"] and b["parts"][1]["label"] == "lid"
    c = store.mutate("s", lambda cur: update_part(cur, "lid", quantity=2), expected=(a["design_id"], 2))
    assert c["revision"] == 3 and c["parts"][1]["quantity"] == 2 and c["parts"][1]["part_id"] == "p2"
    d = store.mutate("s", lambda cur: remove_part(cur, "p1"), expected=(a["design_id"], 3))
    assert [p["part_id"] for p in d["parts"]] == ["p2"]
    # Removed ids aren't reused.
    e = store.mutate("s", lambda cur: add_part(cur, params(), "base"))
    assert [p["part_id"] for p in e["parts"]] == ["p2", "p3"]


def test_a_stale_revision_is_refused_and_nothing_changes():
    store = DesignStore()
    a = store.mutate("s", lambda cur: new_design(params()))
    store.mutate("s", lambda cur: new_design(params(length=16)))  # the form replaced it
    with pytest.raises(DesignChanged, match="changed outside the chat"):
        store.mutate("s", lambda cur: update_part(cur, None, params(length=20)), expected=(a["design_id"], 1))
    assert store.current("s").parts[0].params.length == 16


def test_an_edit_made_while_a_change_builds_is_never_overwritten():
    store = DesignStore()
    store.mutate("s", lambda cur: new_design(params()))
    started, release = threading.Event(), threading.Event()

    def slow(cur):
        started.set()
        release.wait(5)
        return update_part(cur, None, params(length=20))

    errors = []
    th = threading.Thread(target=lambda: errors.append(_catch(lambda: store.mutate("s", slow))))
    th.start()
    started.wait(5)
    store.mutate("s", lambda cur: new_design(params(length=16)))  # lands while slow() is building
    release.set()
    th.join(5)
    assert isinstance(errors[0], DesignChanged)
    assert store.current("s").parts[0].params.length == 16


def _catch(fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        return exc
    return None


def test_a_retried_request_isnt_applied_twice():
    store = DesignStore()
    store.mutate("s", lambda cur: new_design(params()))
    one = store.mutate("s", lambda cur: add_part(cur, params(), "x"), request_id="call-1")
    two = store.mutate("s", lambda cur: add_part(cur, params(), "x"), request_id="call-1")
    assert one == two and len(store.current("s").parts) == 2


def test_dry_run_and_refused_geometry_change_nothing(monkeypatch):
    store = DesignStore()
    a = store.mutate("s", lambda cur: new_design(params()))
    dry = store.mutate("s", lambda cur: update_part(cur, None, params(length=20)), dry_run=True)
    assert dry["dry_run"] and dry["revision"] == 2 and store.current("s").revision == 1

    def refuse(p):
        raise GeometryRefused("can't be made")

    monkeypatch.setattr(designs, "create_box", refuse)
    with pytest.raises(GeometryRefused):
        store.mutate("s", lambda cur: update_part(cur, None, params(length=21)))
    assert store.current("s").revision == 1 and store.current("s").id == a["design_id"]


def test_the_guard_stops_a_commit():
    store = DesignStore()

    def stop():
        raise RuntimeError("stopped")

    with pytest.raises(RuntimeError):
        store.mutate("s", lambda cur: new_design(params()), guard=stop)
    assert store.current("s") is None


def test_snapshots_are_checked_against_the_revision():
    store = DesignStore()
    a = store.mutate("s", lambda cur: new_design(params()))
    assert store.snapshot("s", a["design_id"], 1).revision == 1
    store.mutate("s", lambda cur: update_part(cur, None, params(length=15)))
    with pytest.raises(DesignChanged):
        store.snapshot("s", a["design_id"], 1)
    with pytest.raises(DesignChanged):  # another design at the same revision number
        store.mutate("s", lambda cur: new_design(params()))
        store.snapshot("s", a["design_id"], 1)


def test_groups_and_stale_links():
    store = DesignStore()
    store.mutate("s", lambda cur: add_part(new_design(params(), "base"), BoxParams.model_validate(LID), "lid"))

    def link(cur):
        return replace(cur, groups=(Group("g1", "telescoping", (Member("p1", "base"), Member("p2", "lid"))),),
                       links=(FitLink("f1", "p2", "p1"),))

    out = store.mutate("s", link)
    assert out["groups"][0]["members"][1]["ref"] == "p2" and not out["design_checks"]
    out = store.mutate("s", lambda cur: remove_part(cur, "lid"))
    assert out["groups"][0]["members"] == [{"ref": "p1", "role": "base", "quantity": 1}]
    assert out["design_checks"][0]["status"] == "stale" and out["overall"] == "fail"


def test_a_design_keeps_at_least_one_part():
    store = DesignStore()
    store.mutate("s", lambda cur: new_design(params()))
    with pytest.raises(ValueError, match="at least one part"):
        store.mutate("s", lambda cur: remove_part(cur, "p1"))


def test_parts_carry_a_tagged_preview_scene():
    store = DesignStore()
    out = store.mutate("s", lambda cur: new_design(params()))
    scene = out["parts"][0]["scene"]
    assert scene and {s["op"] for s in scene} >= {"outline", "hinge_P1_P2"}
    assert all(s["provenance"] == "structure" for s in scene)


# ------------------------------------------------------------------ through the agent


def _agent(tmp_path):
    events = []
    ag = agent_mod.Agent(SettingsStore(tmp_path / "settings.json"), events.append)
    s = agent_mod.Session("s1", turns=1)
    t = agent_mod.Turn("s1", 1, [], None)
    s.active = t
    ag.sessions["s1"] = s
    return ag, t, events


def test_agent_adds_updates_and_removes_parts(tmp_path):
    ag, t, _ = _agent(tmp_path)
    one = ag._dispatch(t, "create_box", BOX, "c1")
    assert one["revision"] == 1 and t.binding == (one["design_id"], 1)
    two = ag._dispatch(t, "create_box", {**LID, "action": "add", "label": "lid"}, "c2")
    assert [p["part"] for p in two["parts"]] == ["p1", "p2"] and two["part"] == "p2"
    up = ag._dispatch(t, "update_part", {"part": "lid", "depth": 3}, "c3")
    assert up["revision"] == 3 and ag.store.current("s1").part("lid").params.depth == 3
    with pytest.raises(ValueError, match="say which"):
        ag._dispatch(t, "view_design", {}, "c4")
    assert ag._dispatch(t, "list_faces", {"part": "lid"}, "c5")["faces"]
    gone = ag._dispatch(t, "remove_part", {"part": "p1"}, "c6")
    assert [p["part"] for p in gone["parts"]] == ["p2"]


def test_agent_is_refused_after_an_outside_edit_until_it_looks_again(tmp_path):
    ag, t, _ = _agent(tmp_path)
    ag._dispatch(t, "create_box", BOX, "c1")
    ag.set_design("s1", params(length=18))  # the designer pressed Generate in the form
    with pytest.raises(DesignChanged):
        ag._dispatch(t, "update_part", {"depth": 7}, "c2")
    with pytest.raises(DesignChanged):
        ag._dispatch(t, "export_files", {"filename": "x"}, "c3")
    ag._dispatch(t, "view_design", {}, "c4")  # rebinds to what's there now
    out = ag._dispatch(t, "update_part", {"depth": 7}, "c5")
    assert out["parts"][0]["name"].startswith("REG-GLIO-NX 18x8x7")


def test_agent_retry_with_the_same_call_id_adds_one_part(tmp_path):
    ag, t, _ = _agent(tmp_path)
    ag._dispatch(t, "create_box", BOX, "c1")
    ag._dispatch(t, "create_box", {**LID, "action": "add"}, "c2")
    t.binding = (t.binding[0], t.binding[1] - 1)  # as if the reply was lost and the call is retried
    ag._dispatch(t, "create_box", {**LID, "action": "add"}, "c2")
    assert len(ag.store.current("s1").parts) == 2


def test_agent_dry_run_changes_nothing(tmp_path):
    ag, t, _ = _agent(tmp_path)
    ag._dispatch(t, "create_box", BOX, "c1")
    out = ag._dispatch(t, "update_part", {"length": 20, "dry_run": True}, "c2")
    assert out["dry_run"] and ag.store.current("s1").parts[0].params.length == 14 and t.binding[1] == 1


def test_agent_exports_its_bound_snapshot_with_every_part(tmp_path):
    ag, t, _ = _agent(tmp_path)
    ag.settings.update({"output_dir": str(tmp_path / "out")})
    ag._dispatch(t, "create_box", BOX, "c1")
    res = ag._dispatch(t, "export_files", {"filename": "job"}, "c2")
    assert res["paths"][0].endswith("job.cf2")
    ag._dispatch(t, "create_box", {**LID, "action": "add"}, "c3")
    res = ag._dispatch(t, "export_files", {"filename": "job2"}, "c4")
    out = tmp_path / "out"
    assert sorted(p.name for p in out.iterdir()) == ["job.cf2", "job.dieline.json", "job2-p1.cf2", "job2-p2.cf2",
                                                       "job2.dieline.json"]
    record = json.loads((out / "job2.dieline.json").read_text())
    assert record["version"] == 2 and [p["part_id"] for p in record["parts"]] == ["p1", "p2"]
    assert record["design_id"] == t.binding[0] and record["revision"] == t.binding[1]
    assert [f["part_id"] for f in res["files"]] == ["p1", "p2"]


def test_turn_note_tells_the_model_about_outside_changes(tmp_path):
    ag, t, _ = _agent(tmp_path)
    s = ag.sessions["s1"]
    ag._dispatch(t, "create_box", BOX, "c1")
    s.seen = t.binding
    assert ag._design_note(s) is None
    ag.set_design("s1", params(length=18))
    note = ag._design_note(s)
    assert note and "changed since you last saw it" in note and '"length": 18.0' in note


# ------------------------------------------------------------------ over RPC


def test_rpc_replace_get_and_export_by_snapshot(tmp_path):
    import io

    from dieline_engine.rpc import Server

    store = SettingsStore(tmp_path / "settings.json")
    store.update({"output_dir": str(tmp_path / "out")})
    server = Server(io.StringIO(), store)
    shown = server.methods["design.replace"]({"session_id": "s1", "params": BOX})
    assert shown["revision"] == 1 and server.methods["design.get"]({"session_id": "s1"})["design_id"] == shown["design_id"]
    res = server.methods["box.export"]({"session_id": "s1", "design_id": shown["design_id"], "revision": 1,
                                        "filename": "job"})
    assert res["paths"][0].endswith("job.cf2")
    # The chat changed the design after the export bar showed it: that bar's export is refused.
    server.store.mutate("s1", lambda cur: update_part(cur, None, params(length=16)))
    with pytest.raises(DesignChanged):
        server.methods["box.export"]({"session_id": "s1", "design_id": shown["design_id"], "revision": 1,
                                      "filename": "job2"})
    assert server.methods["design.get"]({"session_id": "nobody"}) is None
    server._pool.shutdown()


# --- regressions from the Phase 1c reviews (Sol, Gemini) -----------------------


def test_a_failed_read_doesnt_rebind_the_turn(tmp_path):
    ag, t, _ = _agent(tmp_path)
    ag._dispatch(t, "create_box", BOX, "c1")
    ag.set_design("s1", params(length=18))
    ag.store.mutate("s1", lambda cur: add_part(cur, BoxParams.model_validate(LID), "lid"))
    before = t.binding
    with pytest.raises(ValueError, match="say which"):
        ag._dispatch(t, "view_design", {}, "c2")
    assert t.binding == before
    with pytest.raises(DesignChanged):
        ag._dispatch(t, "create_box", BOX, "c3")  # replacing an unseen two-part design is refused
    assert len(ag.store.current("s1").parts) == 2


def test_an_outside_edit_is_reported_before_part_names_are_checked(tmp_path):
    ag, t, _ = _agent(tmp_path)
    ag._dispatch(t, "create_box", BOX, "c1")
    ag._dispatch(t, "create_box", {**LID, "action": "add", "label": "lid"}, "c2")
    ag.set_design("s1", params(length=18))  # one part now, no lid
    with pytest.raises(DesignChanged):
        ag._dispatch(t, "update_part", {"part": "lid", "depth": 3}, "c3")
    with pytest.raises(DesignChanged):
        ag._dispatch(t, "remove_part", {"part": "lid"}, "c4")


def test_request_ids_are_scoped_to_the_turn(tmp_path):
    ag, t, _ = _agent(tmp_path)
    ag._dispatch(t, "create_box", BOX, "call_2")
    ag.set_design("s1", params(length=20))
    t2 = agent_mod.Turn("s1", 2, [], None)
    ag.sessions["s1"].active = t2
    ag._dispatch(t2, "view_design", {}, "call_1")
    out = ag._dispatch(t2, "create_box", {**BOX, "length": 18, "action": "add"}, "call_2")
    assert [p["name"].split()[1] for p in out["parts"]] == ["20x8x6", "18x8x6"]


def test_a_replayed_result_never_moves_the_binding_back(tmp_path):
    ag, t, _ = _agent(tmp_path)
    ag._dispatch(t, "create_box", BOX, "c1")
    ag._dispatch(t, "create_box", {**LID, "action": "add"}, "c2")
    after = t.binding
    ag._dispatch(t, "create_box", BOX, "c1")  # a late retry of the first call
    assert t.binding == after


def test_an_overlapping_retry_returns_the_first_result():
    store = DesignStore()
    store.mutate("s", lambda cur: new_design(params()))

    def change(cur):  # while this builds, the same request commits
        store.mutate("s", lambda c: add_part(c, params(), "x"), request_id="r")
        return add_part(cur, params(), "x")

    out = store.mutate("s", change, request_id="r")
    assert len(out["parts"]) == 2 and len(store.current("s").parts) == 2


def test_stop_waits_for_a_commit_in_progress(tmp_path):
    ag, t, _ = _agent(tmp_path)
    done = threading.Event()
    with ag.store.commit_boundary():
        th = threading.Thread(target=lambda: (ag.cancel("s1"), done.set()))
        th.start()
        assert not done.wait(0.2)  # Stop can't finish while a commit holds the boundary
    th.join(5)
    assert done.is_set() and ag.sessions["s1"].active is None


def test_a_stop_that_changed_nothing_leaves_no_note(tmp_path):
    ag, t, _ = _agent(tmp_path)
    ag.cancel("s1")
    assert ag.sessions["s1"].note is None


def test_labels_cant_take_an_id_or_repeat():
    with pytest.raises(ValueError, match="looks like a part id"):
        new_design(params(), "p2")
    d = add_part(new_design(params()), params())
    assert [p.label for p in d.parts] == ["reg-glio-nx", "reg-glio-nx 2"]
    with pytest.raises(ValueError, match="already labelled"):
        add_part(d, params(), "reg-glio-nx")
    assert d.part("p2").id == "p2" and d.part("reg-glio-nx 2").id == "p2"


def test_a_stale_link_blocks_export_everywhere(tmp_path):
    import io

    from dieline_engine.rpc import Server

    store = SettingsStore(tmp_path / "settings.json")
    store.update({"output_dir": str(tmp_path / "out")})
    server = Server(io.StringIO(), store)
    server.store.mutate("s1", lambda cur: add_part(new_design(params(), "base"), BoxParams.model_validate(LID), "lid"))
    server.store.mutate("s1", lambda cur: replace(cur, links=(FitLink("f1", "p1", "p2"),)))
    out = server.store.mutate("s1", lambda cur: remove_part(cur, "lid"))
    with pytest.raises(designs.DesignCheckFailed, match="fit link"):
        server.methods["box.export"]({"session_id": "s1", "design_id": out["design_id"],
                                      "revision": out["revision"], "filename": "x"})
    t = agent_mod.Turn("s1", 1, [], (out["design_id"], out["revision"]), out["seq"])
    server.agent.sessions["s1"] = agent_mod.Session("s1", active=t)
    with pytest.raises(designs.DesignCheckFailed):
        server.agent._dispatch(t, "export_files", {"filename": "x"}, "c1")
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").iterdir())
    server._pool.shutdown()


def test_dry_run_add_and_retries_report_the_part_they_are_about(tmp_path):
    ag, t, _ = _agent(tmp_path)
    ag._dispatch(t, "create_box", BOX, "c1")
    dry = ag._dispatch(t, "create_box", {**LID, "action": "add", "dry_run": True}, "c2")
    assert dry["part"] == "p2" and dry["dry_run"]
    ag._dispatch(t, "create_box", {**LID, "action": "add", "label": "lid"}, "c3")
    again = ag._dispatch(t, "create_box", {**LID, "action": "add", "label": "lid"}, "c3")
    assert again["part"] == "p2"


@pytest.mark.parametrize("args,match", [
    ({"options": [1, 2]}, "options must be an object"),
    ({"depht": 7}, "no argument"),
    ({"quantity": 0}, "whole number"),
    ({"quantity": 1.5}, "whole number"),
    ({"quantity": True}, "whole number"),
    ({"depth": 7, "dry_run": "false"}, "true or false"),
    ({}, "at least one field"),
])
def test_update_part_arguments_are_checked(tmp_path, args, match):
    ag, t, _ = _agent(tmp_path)
    ag._dispatch(t, "create_box", BOX, "c1")
    with pytest.raises(ValueError, match=match):
        ag._dispatch(t, "update_part", args, "c2")
    assert ag.store.current("s1").revision == 1


def test_changing_style_drops_the_old_styles_options(tmp_path):
    ag, t, _ = _agent(tmp_path)
    ag._dispatch(t, "create_box", {**BOX, "options": {"top_flaps": "regular"}}, "c1")
    out = ag._dispatch(t, "update_part", {"style": "hsc-gl", "options": {"use": "lid"}}, "c2")
    assert out["params"]["style"] == "hsc-gl" and out["params"]["options"] == {"use": "lid"}


def test_view_design_says_which_design_and_part_it_shows(tmp_path):
    ag, t, _ = _agent(tmp_path)
    ag._dispatch(t, "create_box", {**BOX, "options": {"closure": "outside"}}, "c1")
    out = ag._dispatch(t, "view_design", {}, "c2")
    d = out["design"]
    assert d["revision"] == 1 and d["part"] == "p1" and d["params"]["options"] == {"closure": "outside"}
    assert "checks" in d and d["parts"] == [{"part": "p1", "label": "reg-glio-nx", "quantity": 1}]


def test_remove_part_with_no_design_is_a_clean_error(tmp_path):
    ag, t, _ = _agent(tmp_path)
    with pytest.raises(ValueError, match="no design yet"):
        ag._dispatch(t, "remove_part", {"part": "p1"}, "c1")


def test_nested_groups_are_pruned_all_the_way_up():
    d = add_part(new_design(params(), "base"), BoxParams.model_validate(LID), "lid")
    d = replace(d, groups=(Group("g1", "assembly", (Member("p2", "lid"),)), Group("g2", "stack", (Member("g1", "set"),))),
                links=(FitLink("f1", "p1", "g2"),))
    after = remove_part(d, "p2")
    assert after.groups == ()
    store = DesignStore()
    store.mutate("s", lambda cur: after)
    assert store.payload(store.current("s"))["design_checks"][0]["status"] == "stale"


def test_payloads_dont_share_the_cache():
    store = DesignStore()
    out = store.mutate("s", lambda cur: new_design(params()))
    out["parts"][0]["checks"].clear()
    assert store.payload(store.current("s"))["parts"][0]["checks"]


# --- regressions from the Phase 1c re-check (Sol) ------------------------------


def test_the_same_call_id_in_two_steps_is_two_calls(tmp_path):
    ag, t, _ = _agent(tmp_path)
    t.step = 1
    ag._dispatch(t, "create_box", BOX, "call_2")
    t.step = 2
    out = ag._dispatch(t, "create_box", {**BOX, "length": 18, "action": "add"}, "call_2")
    assert len(out["parts"]) == 2


def test_a_reset_clears_retry_keys(tmp_path):
    ag, t, _ = _agent(tmp_path)
    ag._dispatch(t, "create_box", BOX, "call_2")
    ag.sessions["s1"].active = None
    ag.reset("s1")
    ag.sessions["s1"] = agent_mod.Session("s1", turns=1)
    t2 = agent_mod.Turn("s1", 1, [], None)
    ag.sessions["s1"].active = t2
    out = ag._dispatch(t2, "create_box", {**BOX, "length": 18}, "call_2")
    assert out["parts"][0]["name"].split()[1] == "18x8x6" and ag.store.current("s1") is not None


def test_tool_row_ids_stay_unique_across_engine_restarts(tmp_path):
    a, ta, _ = _agent(tmp_path)
    b, tb, _ = _agent(tmp_path)  # a restarted engine: turns count from 1 again
    ta.step = tb.step = 1
    assert a._row_id(ta, "call_2") != b._row_id(tb, "call_2")

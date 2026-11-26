"""Phase 2 through the agent's tools and the picture it sees (docs/DESIGN-SYSTEM.md, "Agent tools", "UI")."""

import pytest

from dieline_engine import designs
from dieline_engine.designs import BoxParams

BOX = {"style": "reg-glio-nx", "length": 14, "width": 10, "depth": 12, "units": "in", "board": "32 B KR"}
HH = {"kind": "handhole", "placement": {"on": "end_panels", "x": "centred",
                                        "y": {"from": "top_score", "to": "near_edge", "distance": 2}}}


def build(feats, **kw):
    return designs.build(BoxParams.model_validate({**BOX, **kw, "features": feats}))


def test_the_picture_labels_faces_from_the_face_map_and_shows_offsets():
    from dieline_engine import render

    flap = {"kind": "hand_flap", "profile": "rect", "width": 3, "height": 3, "hinge": "left",
            "placement": {"on": "P2", "x": "centred", "y": {"from": "top_score", "to": "near_edge", "distance": 2}}}
    d = build([flap])
    png, info = render.render_png(d)
    assert png[:4] == b"\x89PNG"
    # A vertical hand-flap hinge isn't a panel score: the panel positions come from the face map only.
    xs = render._score_positions(d, True, 0.001, structure_only=True)
    assert len(xs) == 4
    assert len(render._score_positions(d, True, 0.001)) == 5


# ------------------------------------------------------------------ the agent's tools


def _agent(tmp_path):
    from dieline_engine import agent as agent_mod
    from dieline_engine.settings import SettingsStore

    ag = agent_mod.Agent(SettingsStore(tmp_path / "settings.json"), lambda e: None)
    s = agent_mod.Session("s1", turns=1)
    t = agent_mod.Turn("s1", 1, [], None)
    s.active = t
    ag.sessions["s1"] = s
    return ag, t


def test_feature_tools_are_in_the_schema_without_coordinates():
    from dieline_engine.agent import TOOLS

    names = {t["name"]: t for t in TOOLS}
    assert "set_features" in names and "features" in names["create_box"]["input_schema"]["properties"]
    assert "features" in names["update_part"]["input_schema"]["properties"]
    assert "dry_run" in names["set_features"]["input_schema"]["properties"]
    item = names["set_features"]["input_schema"]["properties"]["items"]["items"]
    assert item["additionalProperties"] is False
    for banned in ("points", "vertices", "path", "coordinates", "start", "end"):
        if banned == "start":
            assert item["properties"]["start"]["enum"] == ["start", "end"]  # the tear strip's tab end, not a point
            continue
        assert banned not in item["properties"]
    placement = item["properties"]["placement"]
    assert set(placement["properties"]) == {"on", "datum", "x", "y", "pattern", "rotation_deg", "mirror", "pairs"}


def test_create_box_with_features_and_set_features_upsert(tmp_path):
    ag, t = _agent(tmp_path)
    out = ag._dispatch(t, "create_box", {**BOX, "features": [HH]}, "c1")
    assert out["features"][0]["id"] == "f1" and out["features"][0]["faces"] == ["P1", "P3"]
    assert out["_image"][:4] == b"\x89PNG"
    out = ag._dispatch(t, "set_features", {"items": [{"kind": "vent", "placement": {
        "on": "P2", "x": "centred", "y": "centred"}}]}, "c2")
    assert [f["id"] for f in out["features"]] == ["f1", "f2"] and out["revision"] == 2
    out = ag._dispatch(t, "set_features", {"items": [{"id": "f1", "placement": {
        "y": {"from": "top_score", "to": "centre", "distance": 3}}}], "remove": ["f2"]}, "c3")
    assert [f["id"] for f in out["features"]] == ["f1"] and "3 from top_score to its centre" in \
        out["features"][0]["summary"]
    # A retry of the same call returns the first result and changes nothing.
    again = ag._dispatch(t, "set_features", {"items": [{"id": "f1", "placement": {
        "y": {"from": "top_score", "to": "centre", "distance": 3}}}], "remove": ["f2"]}, "c3")
    assert again["revision"] == out["revision"]


def test_set_features_refuses_what_cant_be_placed_and_keeps_the_revision(tmp_path):
    ag, t = _agent(tmp_path)
    ag._dispatch(t, "create_box", BOX, "c1")
    with pytest.raises(ValueError, match="outside its face"):
        ag._dispatch(t, "set_features", {"items": [{"kind": "handhole", "placement": {
            "on": "P1", "x": "centred", "y": {"from": "top_score", "to": "near_edge", "distance": 30}}}]}, "c2")
    with pytest.raises(ValueError, match="to is required"):
        ag._dispatch(t, "set_features", {"items": [{"kind": "handhole", "placement": {
            "on": "P1", "x": "centred", "y": {"from": "top_score", "distance": 2}}}]}, "c3")
    with pytest.raises(ValueError, match="no argument"):
        ag._dispatch(t, "set_features", {"items": [HH], "points": [[0, 0]]}, "c4")
    with pytest.raises(ValueError, match="no field"):
        ag._dispatch(t, "set_features", {"items": [{**HH, "points": [[0, 0]]}]}, "c5")
    with pytest.raises(ValueError, match="needs items"):
        ag._dispatch(t, "set_features", {}, "c6")
    assert ag.store.current("s1").revision == 1


def test_dry_run_resolves_without_changing_and_a_resize_can_leave_a_feature_stale(tmp_path):
    ag, t = _agent(tmp_path)
    ag._dispatch(t, "create_box", BOX, "c1")
    dry = ag._dispatch(t, "set_features", {"items": [HH], "dry_run": True}, "c2")
    assert dry["dry_run"] and dry["features"][0]["faces"] == ["P1", "P3"]
    assert ag.store.current("s1").revision == 1 and not ag.store.current("s1").parts[0].params.features
    ag._dispatch(t, "set_features", {"items": [HH]}, "c3")
    shallow = ag._dispatch(t, "update_part", {"depth": 3}, "c4")  # the handhole no longer fits
    assert shallow["features"][0]["status"] == "stale" and shallow["overall"] == "fail"
    with pytest.raises(ValueError, match="stale"):
        ag._dispatch(t, "export_files", {"filename": "x"}, "c5")
    # A resize can carry the fix in the same call.
    fixed = ag._dispatch(t, "update_part", {"depth": 12, "features": {"items": [
        {"id": "f1", "placement": {"y": {"from": "top_score", "to": "near_edge", "distance": 1}}}]}}, "c6")
    assert fixed["features"][0]["status"] == "ok"


def test_update_part_with_a_new_feature_that_cant_fit_is_refused(tmp_path):
    ag, t = _agent(tmp_path)
    ag._dispatch(t, "create_box", BOX, "c1")
    with pytest.raises(ValueError, match="can't be placed"):
        ag._dispatch(t, "update_part", {"depth": 3, "features": {"items": [HH]}}, "c2")
    assert ag.store.current("s1").revision == 1


def test_the_system_prompt_carries_the_placement_rules():
    from dieline_engine.agent import SYSTEM_PROMPT

    for words in ("OUTSIDE the folded box", "near_edge, far_edge or", "ask which", "Read-back",
                  "never copy", "feature:p1/f1"):
        assert words in SYSTEM_PROMPT

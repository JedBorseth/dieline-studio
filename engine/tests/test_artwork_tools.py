"""Phase 4 through the agent's set_artwork tool, the designer's approvals and acknowledgements
(docs/DESIGN-SYSTEM.md, "Agent tools", "Suspicious artwork")."""

import pytest

from dieline_engine import designs
from test_artwork import BOX, MID, SLOT, SVG, ring


def agent(tmp_path, refs=None):
    from dieline_engine import agent as agent_mod
    from dieline_engine.settings import SettingsStore

    store = SettingsStore(tmp_path / "settings.json")
    if refs is not None:
        store.update({"reference_dirs": [str(r) for r in refs]})
    ag = agent_mod.Agent(store, lambda e: None)
    s = agent_mod.Session("s1", turns=1)
    t = agent_mod.Turn("s1", 1, [], None)
    s.active = t
    ag.sessions["s1"] = s
    return ag, t


def test_artwork_touching_a_score_is_refused_through_the_tool(tmp_path):
    ag, t = agent(tmp_path)
    ag._dispatch(t, "create_box", BOX, "c1")
    across = {"kind": "shape", "shape": "rounded_rect", "width": 3, "height": 3, "placement": {
        "on": "P2", "x": "centred", "y": {"from": "top_score", "to": "centre", "distance": 0}}}
    with pytest.raises(ValueError):
        ag._dispatch(t, "set_artwork", {"items": [across]}, "c2")
    assert ag.store.current("s1").revision == 1


def test_the_agent_cannot_acknowledge_but_the_designer_can(tmp_path):
    from dieline_engine.designstore import AcknowledgeRefused, acknowledge
    ag, t = agent(tmp_path)
    ag._dispatch(t, "create_box", BOX, "c1")
    out = ag._dispatch(t, "set_artwork", {"items": [SLOT]}, "c2")
    keys = [c["ack"] for c in out["checks"] if c.get("ack")]
    assert keys
    # No tool takes an acknowledgement.
    from dieline_engine.agent import tools_for
    from dieline_engine.settings import Settings

    schema = str(tools_for(Settings()))
    assert "acknowledged" not in schema
    with pytest.raises(ValueError, match="no argument"):
        ag._dispatch(t, "update_part", {"acknowledged": keys}, "c3")
    with pytest.raises(ValueError, match="no argument"):
        ag._dispatch(t, "create_box", {**BOX, "acknowledged": keys}, "c4")
    # The store refuses any chat change that adds one, whatever the path.
    with pytest.raises(AcknowledgeRefused):
        ag._mutate(t, lambda d: acknowledge(d, "p1", keys), "c5", False, "p1")
    with pytest.raises(AcknowledgeRefused):
        ag._mutate(t, lambda d: acknowledge(d, "p1", keys), "c6", True, "p1")  # not even a dry run
    cur = ag.store.current("s1")
    assert not cur.parts[0].params.acknowledged
    # The designer, in the checks panel.
    payload = ag.acknowledge("s1", (cur.id, cur.revision), "p1", keys)
    part = payload["parts"][0]
    assert all(c["status"] == "info" for c in part["checks"] if c.get("ack"))
    # The chat may still change other things, and keeps the acknowledgement.
    t.binding = (payload["design_id"], payload["revision"])
    t.binding_seq = payload["seq"]
    out = ag._dispatch(t, "set_artwork", {"items": [{"id": "a1", "label": "bar"}]}, "c7")
    assert all(c["status"] == "info" for c in out["checks"] if c.get("ack"))
    with pytest.raises(ValueError, match="aren't shown"):
        ag.acknowledge("s1", (out["design_id"], out["revision"]), "p1", ["a1:span:0000"])


def test_the_rpc_acknowledges_only_on_the_design_the_designer_sees(tmp_path):
    import io

    from dieline_engine.rpc import Server
    from dieline_engine.settings import SettingsStore

    server = Server(io.StringIO(), SettingsStore(tmp_path / "settings.json"))
    d = server._design_replace({"session_id": "s", "params": {**BOX, "artwork": [SLOT]}})
    keys = [c["ack"] for c in d["parts"][0]["checks"] if c.get("ack")]
    with pytest.raises(Exception):
        server._acknowledge({"session_id": "s", "design_id": d["design_id"], "revision": 99, "part_id": "p1",
                             "keys": keys})
    out = server._acknowledge({"session_id": "s", "design_id": d["design_id"], "revision": d["revision"],
                               "part_id": "p1", "keys": keys})
    assert out["revision"] == d["revision"] + 1 and out["overall"] != "fail"


def test_set_artwork_upserts_by_id_without_resending_the_sketch(tmp_path):
    ag, t = agent(tmp_path)
    ag._dispatch(t, "create_box", BOX, "c1")
    out = ag._dispatch(t, "set_artwork", {"items": [ring(2, "bridge", 3)]}, "c2")
    f = out["features"][0]
    assert f["id"] == "a1" and f["provenance"] == "agent sketch" and "agent-drawn sketch, verify before cutting" \
        in f["summary"]
    assert out["_image"][:4] == b"\x89PNG"
    out = ag._dispatch(t, "set_artwork", {"items": [{"id": "a1", "placement": {
        "x": {"from": "left", "to": "near_edge", "distance": 1}}, "width": 2}]}, "c3")
    stored = ag.store.current("s1").parts[0].params.artwork[0]
    assert stored["sketch"]["shapes"] and stored["width"] == 2
    assert "1 from left to its near edge" in out["features"][0]["summary"]
    dry = ag._dispatch(t, "set_artwork", {"items": [{"kind": "shape", "shape": "heart", "width": 2, "height": 2,
                                                       "placement": {"on": "P1", "x": "centred", "y": "centred"}}],
                                          "dry_run": True}, "c4")
    assert dry["dry_run"] and ag.store.current("s1").revision == out["revision"]
    out = ag._dispatch(t, "set_artwork", {"remove": ["a1"]}, "c5")
    assert not out.get("features")
    with pytest.raises(ValueError, match="one face"):
        ag._dispatch(t, "set_artwork", {"items": [{"kind": "shape", "shape": "circle", "diameter": 1,
                                                   "placement": {"on": "end_panels", "x": "centred",
                                                                 "y": "centred"}}]}, "c6")


def test_partition_pieces_refuse_artwork():
    from dieline_engine.designstore import with_artwork
    from dieline_engine.partitions import PartitionParams  # noqa: F401

    with pytest.raises(ValueError, match="standard box parts"):
        with_artwork(object(), [{"kind": "shape"}])  # type: ignore[arg-type]


def test_import_waits_for_the_designer_and_is_confined_to_the_reference_folders(tmp_path):
    refs = tmp_path / "refs"
    refs.mkdir()
    (refs / "logo.svg").write_bytes(SVG)
    outside = tmp_path / "elsewhere.svg"
    outside.write_bytes(SVG)
    ag, t = agent(tmp_path, refs=[refs])
    ag._dispatch(t, "create_box", BOX, "c1")
    with pytest.raises(ValueError):
        ag._dispatch(t, "set_artwork", {"items": [{"kind": "import", "path": str(outside), "width": 3,
                                                   "placement": MID}]}, "c2")
    out = ag._dispatch(t, "set_artwork", {"items": [{"kind": "import", "path": str(refs / "logo.svg"), "width": 3,
                                                     "loop_rules": {"6": "crease"}, "placement": MID}]}, "c3")
    imp = out["import"]
    assert imp["unsupported"] and "designer" in imp["status"] and out["dry_run"]
    assert ag.store.current("s1").revision == 1  # nothing committed
    with pytest.raises(ValueError, match="confirm importing it without"):
        ag.approve_import("s1", imp["id"])
    payload = ag.approve_import("s1", imp["id"], without_unsupported=True)
    arts = payload["parts"][0]["params"]["artwork"]
    assert arts[0]["source"]["sha256"] and arts[0]["source"]["geometry"]
    assert payload["parts"][0]["features"][0]["provenance"] == "imported artwork"
    # The model sees no stored geometry, and the change was noted for its next turn.
    view = designs.agent_view(payload["parts"][0])
    assert isinstance(view["params"]["artwork"][0]["source"]["geometry"], str)
    assert "approved the import" in ag.sessions["s1"].note
    # The agent can't swap the file or forge geometry.
    t.binding, t.binding_seq = (payload["design_id"], payload["revision"]), payload["seq"]
    with pytest.raises(ValueError):
        ag._dispatch(t, "set_artwork", {"items": [{"id": "a1", "path": str(refs / "other.svg")}]}, "c4")
    with pytest.raises(ValueError):
        ag._dispatch(t, "set_artwork", {"items": [{"id": "a1", "source": {"geometry": []}}]}, "c5")
    out = ag._dispatch(t, "set_artwork", {"items": [{"id": "a1", "width": 2}]}, "c6")
    assert out["features"][0]["status"] == "ok"


def test_the_prompt_carries_the_artwork_rules():
    from dieline_engine.agent import SYSTEM_PROMPT

    for words in ("No structure in artwork", "agent-drawn sketch, verify before", "you can't approve",
                  "DESIGNER acknowledges", "y DOWN", "M L Q C A Z"):
        assert words in SYSTEM_PROMPT


def test_the_job_record_keeps_artwork_provenance_and_its_drawn_outlines(tmp_path):
    import json

    from dieline_engine.designs import BoxParams

    p = BoxParams.model_validate({**BOX, "artwork": [ring(2, "bridge", 3)]})
    out = designs.export(p, tmp_path, "art", ["cf2"])
    record = json.loads((tmp_path / "art.dieline.json").read_text())
    art = record["parts"][0]["artwork"][0]
    assert art["provenance"] == "agent sketch" and "agent-drawn" in art["agent_drawn"]
    assert art["islands"][0]["status"] == "bridged" and len(art["outlines"]) == 2
    assert record["parts"][0]["params"]["artwork"][0]["sketch"]["shapes"]
    assert out


def test_an_import_that_cant_be_placed_is_refused_before_any_card_is_shown(tmp_path):
    refs = tmp_path / "refs"
    refs.mkdir()
    (refs / "big.svg").write_bytes(SVG)
    ag, t = agent(tmp_path, refs=[refs])
    ag._dispatch(t, "create_box", BOX, "c1")
    with pytest.raises(ValueError, match="can't be placed"):
        ag._dispatch(t, "set_artwork", {"items": [{"kind": "import", "path": str(refs / "big.svg"), "width": 30,
                                                   "placement": MID}]}, "c2")
    assert not ag.artwork_imports._items  # no card waiting
    out = ag._dispatch(t, "set_artwork", {"items": [{"kind": "import", "path": str(refs / "big.svg"), "width": 3,
                                                     "loop_rules": {"6": "crease"}, "placement": MID}]}, "c3")
    assert out["import"]["artwork"]["status"] == "ok" and ag.artwork_imports._items

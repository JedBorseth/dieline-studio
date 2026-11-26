import io
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Barrier

import pytest

from dieline_engine import designs
from dieline_engine.boards import get_board
from dieline_engine.cf2 import read_cf2, segments_equal, write_cf2
from dieline_engine.checks import overall, run_checks
from dieline_engine.designs import BoxParams
from dieline_engine.geometry import Dieline, LineType
from dieline_engine.rpc import Server
from dieline_engine.settings import SettingsStore
from dieline_engine.styles import Dims, get_style


def rsc(l=14.0, w=8.0, d=6.0, board="32 B KR", **options) -> Dieline:
    return get_style("reg-glio-nx").make(Dims(l, w, d), get_board(board), options)


def test_rsc_follows_reg_glio_nx_formulas():
    # 14 x 8 x 6 in 32 B KR (CAL 1/8, IL 1/16), E-S-E-S, inside joint.
    d = rsc()
    m = d.meta
    assert m["panels_in"] == [8.0, 14.125, 8.125, 14.125]  # P1 = W + CAL - 1/8
    assert m["body_height_in"] == 6.3125  # D + 5CAL - 4IL - 1/16
    assert m["top_flaps_in"] == [4.0, 4.0]  # W/2 + CAL - 1/8
    assert m["glue_lug_in"] == 1.25
    assert d.size() == (45.625, 14.3125)
    assert d.units == "in"


def test_rsc_passes_geometry_checks():
    results = {r.id: r for r in run_checks(rsc())}
    for check in ("line_types", "zero_length", "closed_outline", "overlaps", "machines", "length_width", "board"):
        assert results[check].status == "pass", results[check]
    # Uncalibrated allowances are always flagged.
    assert results["calibration"].status == "warn"


def test_open_outline_fails():
    d = rsc()
    cut = next(i for i, s in enumerate(d.segments) if s.type == LineType.CUT)
    del d.segments[cut]
    results = {r.id: r for r in run_checks(d)}
    assert results["closed_outline"].status == "fail"
    assert overall(run_checks(d)) == "fail"


def test_duplicate_line_fails():
    d = rsc()
    d.segments.append(d.of_type(LineType.CREASE)[0])
    assert {r.id: r for r in run_checks(d)}["overlaps"].status == "fail"


def test_cf2_round_trip():
    d = rsc()
    text = write_cf2(d)
    assert text.startswith("$BOF\r\n") and text.rstrip().endswith("$EOF")
    assert "\r\nUI\r\n" in text  # inch designs are written in inches
    back = read_cf2(text)
    assert back.warnings == []
    mm = [type(x)(*(tuple(round(v * 25.4, 4) for v in p) for p in (x.start, x.end)), x.type) for x in d.segments]
    assert segments_equal(mm, back.dieline.segments)


def test_cf2_reader_expands_subroutines_and_inches():
    text = "\n".join([
        "$BOF", "V2", "MAIN,test", "UI", "C,box,1,0,90,1,1", "END",
        "SUB,box", "L,2,1,0,0,0,1,0,0,0", "L,2,2,0,0,0,0,1,0,0", "END", "$EOF",
    ])
    res = read_cf2(text)
    lines = res.dieline.segments
    assert len(lines) == 2
    cut = next(s for s in lines if s.type == LineType.CUT)
    # (0,0)-(1,0) in, rotated 90deg and moved by (1,0) in -> (25.4,0)-(25.4,25.4) mm
    assert cut.start == pytest.approx((25.4, 0.0))
    assert cut.end == pytest.approx((25.4, 25.4))


def test_box_params_reject_unknown_fields_and_bad_dims():
    with pytest.raises(ValueError):
        BoxParams.model_validate({"style": "reg-glio-nx", "length": -1, "width": 1, "depth": 1, "board": "32 C KR"})
    with pytest.raises(ValueError):
        BoxParams.model_validate(
            {"style": "reg-glio-nx", "length": 1, "width": 1, "depth": 1, "board": "32 C KR", "x": 0}
        )


def test_agent_view_has_no_geometry():
    design = designs.create_box(
        BoxParams(style="reg-glio-nx", length=304.8, width=203.2, depth=152.4, units="mm", board="32 B KR")
    )
    view = designs.agent_view(design)
    assert "cf2" not in view
    assert view["params"]["units"] == "mm"
    assert design["summary"]["inside_in"]["length"] == pytest.approx(12)


def test_export_writes_into_output_dir_only(tmp_path):
    params = BoxParams(style="reg-glio-nx", length=14, width=8, depth=6, board="32 B KR")
    out = designs.export_cf2(params, tmp_path, "my box")
    assert out["path"].endswith("my box.cf2") and out["job_path"].endswith("my box.dieline.json")
    assert open(out["path"], newline="").read().count("\r\n") > 10
    # Directory parts are stripped, so the file still lands in tmp_path.
    out2 = designs.export_cf2(params, tmp_path, "../../evil")
    assert str(tmp_path) in out2["path"]


def test_rpc_round_trip(tmp_path):
    out = io.StringIO()
    server = Server(out, SettingsStore(tmp_path / "settings.json"))
    server._dispatch({"jsonrpc": "2.0", "id": 1, "method": "box.create",
                      "params": {"style": "reg-glio-nx", "length": 14, "width": 8, "depth": 6,
                                 "board": "32 B KR"}})
    server._dispatch({"jsonrpc": "2.0", "id": 2, "method": "nope"})
    server._dispatch({"jsonrpc": "2.0", "id": 3, "method": "box.create", "params": {"style": "x"}})
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert replies[0]["result"]["overall"] == "warn"  # calibration warning only
    assert replies[1]["error"]["code"] == -32601
    assert replies[2]["error"]["code"] == -32602


# --- regressions from review ------------------------------------------------

from dieline_engine.designs import DesignCheckFailed, safe_filename  # noqa: E402
from dieline_engine.geometry import Arc  # noqa: E402


def test_shallow_box_glue_taper_does_not_self_intersect():
    results = {r.id: r for r in run_checks(rsc(14, 8, 0.5))}
    assert results["crossings"].status == "pass", results["crossings"]
    assert results["closed_outline"].status == "pass"


def test_crossing_lines_fail():
    d = rsc()
    w, h = d.size()
    d.add_line((0, 0), (w, h), LineType.CUT)
    d.add_line((0, h), (w, 0), LineType.CUT)
    assert {r.id: r for r in run_checks(d)}["crossings"].status == "fail"


def test_export_refuses_failing_design(tmp_path):
    params = BoxParams(style="reg-glio-nx", length=8, width=10, depth=6, board="32 B KR")  # W > L
    assert designs.create_box(params)["overall"] == "fail"
    with pytest.raises(DesignCheckFailed):
        designs.export_cf2(params, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_arc_overlap_uses_swept_intervals():
    d = Dieline("arcs")
    top = Arc((10, 0), (-10, 0), (0, 0), False, LineType.CUT)
    bottom = Arc((-10, 0), (10, 0), (0, 0), False, LineType.CUT)
    d.segments += [top, bottom]
    assert {r.id: r for r in run_checks(d)}["overlaps"].status == "pass"
    d.segments.append(Arc((0, 10), (0, -10), (0, 0), False, LineType.CUT))  # left half overlaps both
    assert {r.id: r for r in run_checks(d)}["overlaps"].status == "fail"


def test_cf2_nested_rotation_under_mirror():
    # Outer call mirrors x; inner call rotates 90deg. (1,0) -> rot90 -> (0,1) -> mirror x -> (0,1).
    text = "\n".join([
        "MAIN,m", "C,outer,0,0,0,-1,1", "END",
        "SUB,outer", "C,inner,0,0,90,1,1", "END",
        "SUB,inner", "L,2,1,0,0,0,1,0,0,0", "END",
    ])
    line = read_cf2(text).dieline.segments[0]
    assert line.end == pytest.approx((0.0, 1.0))
    # Mirror applies after rotation inside one call: (1,0) -> rot90 -> (0,1) -> flip y -> (0,-1).
    line = read_cf2("MAIN,m\nC,s,0,0,90,1,-1\nEND\nSUB,s\nL,2,1,0,0,0,1,0,0,0\nEND").dieline.segments[0]
    assert line.end == pytest.approx((0.0, -1.0))


def test_reserved_windows_names_are_rewritten():
    assert safe_filename("CON") == "_CON.cf2"
    assert safe_filename("lpt1.cf2") == "_lpt1.cf2"
    assert safe_filename("box.cf2") == "box.cf2"


def test_api_key_must_be_an_openrouter_api_key(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    for bad in ("sk-ant-oat01-subscription-token", "sk-ant-api03-anthropic-key", "hello"):
        with pytest.raises(ValueError):
            store.update({"api_key": bad})
    with pytest.raises(ValueError):
        store.update({"model": "claude-opus-5-5"})  # OpenRouter ids have a provider prefix


def test_old_anthropic_settings_migrate(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"model": "claude-opus-5-5", "effort": "high", "web_search": False}))
    s = SettingsStore(path).settings
    assert s.model == "anthropic/claude-opus-5.5" and s.effort == "high" and s.search_provider == "off"


# --- agent loop against a fake OpenRouter client --------------------------------

from types import SimpleNamespace as NS  # noqa: E402

from dieline_engine import agent as agent_mod  # noqa: E402


FINISH = {"tool_use": "tool_calls", "end_turn": "stop", "refusal": "content_filter", "max_tokens": "length"}


def _chunks(response):
    """OpenRouter-style stream chunks for a scripted reply NS(stop_reason, content)."""
    out = []
    calls = [b for b in response.content if b.type == "tool_use"]
    for b in response.content:
        if b.type == "text":
            out.append(NS(model_extra={}, choices=[NS(finish_reason=None, delta=NS(
                content=b.text, tool_calls=None, model_extra={}))]))
    for i, b in enumerate(calls):
        args = json.dumps(b.input)
        half = len(args) // 2  # arguments arrive in pieces
        for piece, first in ((args[:half], True), (args[half:], False)):
            fn = NS(name=b.name if first else None, arguments=piece)
            out.append(NS(model_extra={}, choices=[NS(finish_reason=None, delta=NS(
                content=None, tool_calls=[NS(index=i, id=b.id if first else None, function=fn)], model_extra={}))]))
    out.append(NS(model_extra={}, choices=[NS(finish_reason=FINISH[response.stop_reason], delta=None)]))
    return out


class _FakeStream:
    def __init__(self, response):
        self.chunks = _chunks(response)

    def __iter__(self):
        return iter(self.chunks)

    def close(self):
        pass


def _fake_client(responses, calls):
    class Client:
        def __init__(self, api_key, base_url=None, timeout=None, default_headers=None, max_retries=None):
            self.chat = NS(completions=NS(create=self.create))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def create(self, **kw):
            calls.append(kw)
            return _FakeStream(responses.pop(0))

    return Client


def _tool_use(id_, name, input_):
    return NS(type="tool_use", id=id_, name=name, input=input_)


def _text(t):
    return NS(type="text", text=t)


BOX = {"style": "reg-glio-nx", "length": 14, "width": 8, "depth": 6, "units": "in", "board": "32 B KR"}


def _run(monkeypatch, tmp_path, responses):
    calls, events = [], []
    monkeypatch.setattr(agent_mod.openai, "OpenAI", _fake_client(responses, calls))
    store = SettingsStore(tmp_path / "settings.json")
    monkeypatch.setattr(store, "api_key", lambda: "sk-or-v1-test")
    ag = agent_mod.Agent(store, events.append)
    s, t = _start(ag)
    ag._run_turn(s, t, "sk-or-v1-test", ag.settings.settings)
    return ag, s, events, calls


def _start(ag):
    s = agent_mod.Session("s1", turns=1)
    t = agent_mod.Turn("s1", 1, [{"role": "user", "content": "make a box"}], None)
    s.active = t
    ag.sessions["s1"] = s
    return s, t


def test_agent_tool_round_trip(monkeypatch, tmp_path):
    responses = [
        NS(stop_reason="tool_use", content=[_tool_use("t1", "create_box", BOX)]),
        NS(stop_reason="end_turn", content=[_text("Done.")]),
    ]
    _, s, events, calls = _run(monkeypatch, tmp_path, responses)
    types = [e["type"] for e in events]
    assert "design" in types and types[-1] == "chat.turn_end"
    assert events[-1]["stop_reason"] == "end_turn" and not s.busy
    # history: user, assistant(tool call), tool(result), user(picture), assistant(text)
    assert [m["role"] for m in s.messages] == ["user", "assistant", "tool", "user", "assistant"]
    assert s.messages[2]["tool_call_id"] == "t1"
    assert s.messages[1]["tool_calls"][0]["function"]["name"] == "create_box"
    assert s.current is not None


def test_agent_refusal_discards_turn_but_keeps_the_shown_design(monkeypatch, tmp_path):
    responses = [
        NS(stop_reason="tool_use", content=[_tool_use("t1", "create_box", BOX)]),
        NS(stop_reason="refusal", content=[]),
    ]
    _, s, events, _ = _run(monkeypatch, tmp_path, responses)
    assert s.messages == []
    # The preview already shows the design, so it stays current and the model is told next turn.
    assert s.current is not None and s.current.length == 14 and "interrupted" in s.note
    assert any(e["type"] == "chat.error" and "declined" in e["message"] for e in events)
    assert events[-1] == {"type": "chat.turn_end", "session_id": "s1", "stop_reason": "error"}


def test_agent_max_steps_discards_turn(monkeypatch, tmp_path):
    responses = [
        NS(stop_reason="tool_use", content=[_tool_use(f"t{i}", "list_options", {})])
        for i in range(agent_mod.MAX_STEPS)
    ]
    _, s, events, _ = _run(monkeypatch, tmp_path, responses)
    assert s.messages == []
    assert events[-1]["stop_reason"] == "error"


def test_width_over_length_fails_like_the_standard():
    results = {r.id: r for r in run_checks(rsc(8, 14, 6))}
    assert results["length_width"].status == "fail"
    assert {r.id: r for r in run_checks(rsc())}["length_width"].status == "pass"


def test_settings_update_is_all_or_nothing(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    with pytest.raises(ValueError):
        store.update({"model": "anthropic/claude-haiku-5.5", "effort": "bogus"})
    assert store.settings.model == "anthropic/claude-opus-5.5"
    with pytest.raises(ValueError):
        store.update({"output_dir": "relative/dir"})


def test_settings_validate_all_credentials_before_writing(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text("[]")
    store = SettingsStore(path)
    writes = []
    monkeypatch.setattr(store, "_store_secret", lambda user, key: writes.append((user, key)))
    with pytest.raises(ValueError):
        store.update({"api_key": "sk-or-example", "exa_api_key": "invalid key"})
    with pytest.raises(ValueError):
        store.update({"api_key": 1})
    assert not writes
    store.update({"model": "~google/gemini-pro-latest"})
    assert SettingsStore(path).settings.model == "~google/gemini-pro-latest"


def test_cf2_header_units_and_block_scale():
    text = "$BOF\nV2\nORDER\nUI\nEND\nMAIN,t\nSCALE,2,2\nL,2,1,0,0,0,1,0,0,0\nEND\n$EOF"
    line = read_cf2(text).dieline.segments[0]
    assert line.end == pytest.approx((50.8, 0.0))
    bad = read_cf2("MAIN,t\nL,2,,0,0,0,1,0,0,0\nEND")
    assert bad.dieline.segments == [] and bad.raw_codes == {} and len(bad.warnings) == 1


def test_stopped_turn_is_abandoned(monkeypatch, tmp_path):
    calls, events = [], []
    responses = [
        NS(stop_reason="tool_use", content=[_tool_use("t1", "create_box", BOX)]),
        NS(stop_reason="end_turn", content=[_text("Done.")]),
    ]
    monkeypatch.setattr(agent_mod.openai, "OpenAI", _fake_client(responses, calls))
    ag = agent_mod.Agent(SettingsStore(tmp_path / "settings.json"), events.append)
    s, t = _start(ag)
    ag.cancel("s1")
    assert events == [{"type": "chat.turn_end", "session_id": "s1", "stop_reason": "cancelled"}]
    assert not s.busy
    # The worker finishing later (e.g. after a stalled read) changes nothing and says nothing.
    t.cancel.clear()
    ag._run_turn(s, t, "sk-or-v1-test", ag.settings.settings)
    assert len(events) == 1 and s.messages == [] and s.current is None


def test_cancel_during_create_does_not_keep_an_unshown_design(monkeypatch, tmp_path):
    events = []
    ag = agent_mod.Agent(SettingsStore(tmp_path / "settings.json"), events.append)
    s, t = _start(ag)
    create = designs.create_box

    def create_then_cancel(params):
        design = create(params)
        ag.cancel(s.id)
        return design

    monkeypatch.setattr(designs, "create_box", create_then_cancel)
    with pytest.raises(agent_mod.TurnCancelled):
        ag._dispatch(t, "create_box", BOX)
    assert s.current is None and s.note is None
    assert [event["type"] for event in events] == ["chat.turn_end"]


# --- R&L standards ------------------------------------------------------------

from dieline_engine.dxf import write_dxf  # noqa: E402
from dieline_engine.checks import fmt_in  # noqa: E402


def _creases(d):
    v = sorted({s.start[0] for s in d.of_type(LineType.CREASE) if s.start[0] == s.end[0]})
    h = sorted({s.start[1] for s in d.of_type(LineType.CREASE) if s.start[1] == s.end[1]})
    return v, h


@pytest.mark.parametrize(
    "dims, board, creases_x, creases_y, size",
    [
        # Real R&L blanks, measured from their sample-table DXF exports.
        # Reference: REG-GLIO-NX 24 x 12 x 24 5/8, 32 C KR, S-E-S-E.
        ((24, 12, 24.625), "32 C KR", [1.5, 13.6875, 37.875, 50.0625], [6.0625, 31.0625], (74.125, 37.125)),
        # Reference: RSC 17 1/2 x 13 1/4 x 16 1/4, 32 B KR, S-E-S-E.
        ((17.5, 13.25, 16.25), "32 B KR", [1.5, 14.875, 32.5, 45.875], [6.625, 23.1875], (63.375, 29.8125)),
    ],
)
def test_rsc_reproduces_real_rl_blanks(dims, board, creases_x, creases_y, size):
    d = rsc(*dims, board=board, order="sese")
    assert _creases(d) == (creases_x, creases_y)
    assert d.size() == size
    assert overall(run_checks(d)) != "fail"


def test_glue_lug_is_on_the_left_like_rl_exports():
    d = rsc()
    xs, _ = _creases(d)
    assert xs[0] == 1.25  # glue score first, then panels 4..1
    assert d.meta["panels_in"][0] == 8.0 and d.size()[0] - xs[-1] == 8.0  # panel 1 at the free edge
    width, height = d.size()
    free_edge = [s for s in d.of_type(LineType.CUT) if s.start[0] == s.end[0] == width]
    assert len(free_edge) == 1
    assert {free_edge[0].start[1], free_edge[0].end[1]} == {0.0, height}


@pytest.mark.parametrize("top,bottom", [("regular", "regular"), ("full_overlap", "cssc"), ("gap_lap", "regular"),
                                        ("cssc", "full_overlap")])
@pytest.mark.parametrize("closure", ["inside", "outside"])
def test_flap_styles_pass_geometry_checks(top, bottom, closure):
    d = rsc(24, 12, 10, "48 BC KR", top_flaps=top, bottom_flaps=bottom, closure=closure)
    results = {r.id: r.status for r in run_checks(d)}
    for c in ("closed_outline", "overlaps", "crossings", "zero_length"):
        assert results[c] == "pass", (c, top, bottom)


def test_flap_style_formulas():
    full = rsc(14, 8, 6, top_flaps="full_overlap", bottom_flaps="full_overlap").meta
    assert full["top_flaps_in"] == [8.0, 8.0]
    assert full["body_height_in"] == 6 + 4 * 0.125 - 2 * 0.0625 + 0.125  # TD + BD for TF=2
    cssc = rsc(14, 8, 6, top_flaps="cssc").meta
    assert cssc["top_flaps_in"] == [7.0, 4.0]  # end panels get L/2 + CAL - 1/8
    outside = rsc(14, 8, 6, closure="outside").meta
    assert outside["panels_in"] == [8.0 - 0.0625, 14.125, 8.125, 14.125 + 0.0625]


def test_hsc_and_tube_follow_their_standards():
    b = get_board("32 B KR")
    hsc = get_style("hsc-gl").make(Dims(14, 8, 6), b, {})
    assert hsc.meta["body_height_in"] == 6 + (5 * 0.125 - 4 * 0.0625 - 0.0625) / 2
    assert hsc.meta["bottom_flaps_in"] is None and hsc.meta["top_flaps_in"] == [4.0, 4.0]
    tube = get_style("tube-glued").make(Dims(14, 8, 12), b, {})
    assert tube.meta["body_height_in"] == 12 and tube.meta["glue_lug_in"] == 1.5  # D >= 12
    for d in (hsc, tube):
        assert overall(run_checks(d)) != "fail"


def test_options_are_validated():
    with pytest.raises(ValueError):
        designs.create_box(BoxParams(style="reg-glio-nx", length=14, width=8, depth=6, options={"flaps": "x"}))
    with pytest.raises(ValueError):
        designs.create_box(BoxParams(style="tube-glued", length=14, width=8, depth=6, options={"closure": "inside"}))


def test_fraction_inputs():
    p = BoxParams(style="reg-glio-nx", length="34 7/16", width="8-1/2", depth='6"')
    assert (p.length, p.width, p.depth) == (34.4375, 8.5, 6.0)
    with pytest.raises(ValueError):
        BoxParams(style="reg-glio-nx", length="12 1/0", width=1, depth=1)
    assert fmt_in(34.4375) == "34 7/16" and fmt_in(0.03125) == "1/32" and fmt_in(6) == "6"


def test_nonfinite_dimensions_and_options_are_rejected():
    with pytest.raises(ValueError, match="must be finite"):
        BoxParams(style="reg-glio-nx", length="inf", width=8, depth=6)
    for top in ("regular", "gap_lap"):
        with pytest.raises(ValueError, match="lap must be finite"):
            rsc(top_flaps=top, top_lap=float("nan"))


def test_export_preflights_the_job_destination(tmp_path):
    params = BoxParams(style="reg-glio-nx", length=14, width=8, depth=6)
    out = tmp_path / "exports"
    out.mkdir()
    protected = tmp_path / "approved.json"
    protected.write_text("approved")
    job = out / "RL26-0999.dieline.json"
    try:
        job.symlink_to(protected)
    except OSError:
        pytest.skip("creating symlinks is unavailable")
    with pytest.raises(ValueError, match="export destination"):
        designs.export(params, out, "RL26-0999", ["cf2", "dxf"])
    assert protected.read_text() == "approved"
    assert list(out.iterdir()) == [job]


def test_export_rejects_malformed_formats_before_writing(tmp_path):
    params = BoxParams(style="reg-glio-nx", length=14, width=8, depth=6)
    with pytest.raises(ValueError, match="unknown export format"):
        designs.export(params, tmp_path, formats=["cf2", {}])
    assert not list(tmp_path.iterdir())


def test_dxf_matches_sample_table_format(tmp_path):
    d = rsc()
    text = write_dxf(d)
    assert text.startswith("  0\r\nSECTION\r\n  2\r\nHEADER\r\n  9\r\n$ACADVER\r\n  1\r\nAC1009\r\n")
    assert "  2\r\nDesign\r\n" in text and text.endswith("  0\r\nEOF\r\n")
    assert text.count("LINE\r\n  8\r\nDesign\r\n  6\r\nCONTINUOUS\r\n 62\r\n1\r\n") == 34  # cuts
    assert write_dxf(read_cf2(write_cf2(d)).dieline) == text  # mm reader to inch DXF
    assert text.count(" 62\r\n2\r\n") == 12  # creases
    out = designs.export(BoxParams(style="reg-glio-nx", length=14, width=8, depth=6), tmp_path, "RL26-0999",
                         ["cf2", "dxf"])
    assert [p.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] for p in out["paths"]] == ["RL26-0999.cf2", "RL26-0999.dxf"]
    job = json.loads(open(out["job_path"]).read())
    assert out["job_path"].endswith("RL26-0999.dieline.json")
    assert job["format"] == "dieline-studio-job" and [f.replace("\\", "/").rsplit("/", 1)[-1] for f in job["files"]] == ["RL26-0999.cf2", "RL26-0999.dxf"]
    # The record rebuilds the same design.
    again = designs.create_box(BoxParams.model_validate(job["params"]))
    assert again["cf2"] == designs.create_box(BoxParams(style="reg-glio-nx", length=14, width=8, depth=6))["cf2"]


def test_dxf_arc_angles_are_counter_clockwise():
    d = Dieline("arc", units="in")
    d.segments.append(Arc((1, 0), (0, 1), (0, 0), False, LineType.CUT))
    d.segments.append(Arc((0, 1), (1, 0), (0, 0), True, LineType.CUT))
    text = write_dxf(d)
    assert text.count(" 50\r\n0\r\n 51\r\n90\r\n") == 2
    arc = d.segments[0]
    d.segments = [Arc(arc.start, arc.start, arc.center, arc.clockwise, arc.type, full_circle=True)]
    text = write_dxf(d)
    assert "\r\nCIRCLE\r\n" in text and "\r\nARC\r\n" not in text


def test_machines_check():
    from dieline_engine.checks import check_machines

    assert check_machines(rsc()).status == "pass"
    huge = rsc(150, 60, 60)
    r = check_machines(huge)
    assert r.status == "warn" and "larger than every" in r.message  # screens, never blocks export
    # 60 x 10 x 60: 141 7/8 x 70 5/16 blank is too tall for the slotter's regular feed
    # (64") but within skip-feed (86.6"), across 181".
    r = check_machines(rsc(60, 10, 60))
    assert r.status == "warn" and "skip-feed" in r.message
    # The slotter can't turn a blank: a 30" high x 150" long blank fits, the reverse wouldn't.
    from dieline_engine.checks import MACHINES, _fit

    slotter = MACHINES[0]
    assert _fit(150, 30, slotter) == "fits" and _fit(30, 150, slotter) == "large"
    assert _fit(20, 70, slotter) == "small"  # skip-feed still needs the minimum across
    across, regular, skip = (round(mm / 25.4, 6) for mm in (4600, 1625, 2200))
    assert _fit(across, regular, slotter) == "fits"
    assert _fit(across, skip, slotter) == "skip"
    assert _fit(across, skip + 0.001, slotter) == "large"
    pioneer = next(m for m in MACHINES if m.name.startswith("Pioneer"))
    assert _fit(115, 21, pioneer) == "small"
    assert _fit(99, 21, pioneer) == "fits"  # turning this narrower blank satisfies both axes


def test_shop_fraction_notations():
    p = BoxParams(style="reg-glio-nx", length="14+3/16", width="10:03", depth="6:00")
    assert (p.length, p.width, p.depth) == (14.1875, 10.1875, 6.0)
    with pytest.raises(ValueError):
        BoxParams(style="reg-glio-nx", length="10:16", width=1, depth=1)


@pytest.mark.parametrize("style", ["reg-glio-nx", "reg-gli-ext"])
def test_gap_lap_applies_independently_to_both_ends(style):
    p = BoxParams(style=style, length=14, width=9, depth=6, options={
        "top_flaps": "gap_lap", "bottom_flaps": "gap_lap", "top_lap": 3.0625, "bottom_lap": -0.5,
    })
    summary = designs.build(p).meta
    assert summary["top_flaps_in"] == [6.03125, 6.03125]
    assert summary["bottom_flaps_in"] == [4.25, 4.25]
    assert summary["top_depth_in"] == 3.25
    assert summary["bottom_depth_in"] == 3.15625


def test_export_never_mixes_file_sets(tmp_path):
    a = BoxParams(style="reg-glio-nx", length=14, width=8, depth=6)
    b = BoxParams(style="reg-glio-nx", length=16, width=8, depth=6)
    designs.export(a, tmp_path, "RL26-0001", ["cf2", "dxf"])
    with pytest.raises(designs.ExportExists) as exc:
        designs.export(b, tmp_path, "RL26-0001", ["cf2"])
    assert set(exc.value.names) == {"RL26-0001.cf2", "RL26-0001.dxf", "RL26-0001.dieline.json"}
    out = designs.export(b, tmp_path, "RL26-0001", ["cf2"], overwrite=True)
    # The old DXF didn't survive next to the new CF2: the whole old set moved aside.
    assert not (tmp_path / "RL26-0001.dxf").exists()
    old = list((tmp_path / "superseded").iterdir())
    assert len(old) == 1 and {p.name for p in old[0].iterdir()} == set(exc.value.names)
    assert len(out["superseded"]) == 3
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def test_dxf_can_go_to_a_separate_sample_folder(tmp_path):
    p = BoxParams(style="reg-glio-nx", length=14, width=8, depth=6)
    out = designs.export(p, tmp_path / "cad", "RL26-0002", ["cf2", "dxf"], dxf_dir=tmp_path / "samples")
    assert (tmp_path / "samples" / "RL26-0002.dxf").exists() and (tmp_path / "cad" / "RL26-0002.cf2").exists()
    with pytest.raises(designs.ExportExists):  # the sample-folder DXF is part of the set too
        designs.export(p, tmp_path / "cad2", "RL26-0002", ["dxf"], dxf_dir=tmp_path / "samples")


def test_same_second_replacements_keep_each_revision(tmp_path, monkeypatch):
    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 25, tzinfo=tz)

    monkeypatch.setattr(designs, "datetime", FixedClock)
    cad, samples = tmp_path / "cad", tmp_path / "samples"
    params = BoxParams.model_validate(BOX)
    for length in (14, 16, 18):
        designs.export(params.model_copy(update={"length": length}), cad, "job", ["cf2", "dxf"],
                       overwrite=True, dxf_dir=samples)
    jobs = {p.parent.name: json.loads(p.read_text())["params"]["length"]
            for p in (cad / "superseded").glob("*/job.dieline.json")}
    assert sorted(jobs.values()) == [14, 16]
    assert {p.parent.name for p in (samples / "superseded").glob("*/job.dxf")} == jobs.keys()


@pytest.mark.parametrize("failure", ["archive", "publish"])
def test_export_rolls_back_when_a_rename_fails(tmp_path, monkeypatch, failure):
    cad, samples = tmp_path / "cad", tmp_path / "samples"
    params = BoxParams.model_validate(BOX)
    designs.export(params, cad, "job", ["cf2", "dxf"], dxf_dir=samples)
    original = {p: p.read_bytes() for p in (*cad.iterdir(), *samples.iterdir())}
    rename = designs.os.replace

    def fail_rename(source, target):
        if failure == "archive" and source == samples / "job.dxf":
            raise PermissionError("injected archive failure")
        if failure == "publish" and source.parent.name.startswith(".dieline-") and target.name.endswith(".json"):
            raise PermissionError("injected publish failure")
        rename(source, target)

    monkeypatch.setattr(designs.os, "replace", fail_rename)
    with pytest.raises(PermissionError, match="injected"):
        designs.export(params.model_copy(update={"length": 16}), cad, "job", ["cf2", "dxf"],
                       overwrite=True, dxf_dir=samples)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == original


def test_export_refuses_a_redirected_superseded_folder(tmp_path):
    cad, other = tmp_path / "cad", tmp_path / "other"
    other.mkdir()
    params = BoxParams.model_validate(BOX)
    designs.export(params, cad, "job")
    try:
        (cad / "superseded").symlink_to(other, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    with pytest.raises(ValueError, match="archive folder"):
        designs.export(params, cad, "job", overwrite=True)
    assert (cad / "job.dieline.json").is_file() and not list(other.iterdir())


def test_ui_and_agent_exports_compete_for_one_name(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    cad = tmp_path / "cad"
    samples = tmp_path / "samples"
    store.update({"output_dir": str(cad), "dxf_dir": str(samples)})
    server = Server(io.StringIO(), store)
    _, turn = _start(server.agent)
    turn.current = BoxParams.model_validate(BOX)
    gate = Barrier(2)

    def attempt(export):
        gate.wait(timeout=5)
        try:
            return export()
        except designs.ExportExists:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        ui = pool.submit(attempt, lambda: server._export(
            {"params": {**BOX, "length": 16}, "filename": "job", "formats": ["dxf"]}))
        chat = pool.submit(attempt, lambda: server.agent._dispatch(
            turn, "export_files", {"filename": "job", "formats": ["cf2", "dxf"]}))
        ui_won, chat_won = ui.result() is not None, chat.result() is not None
    server._pool.shutdown()
    assert ui_won != chat_won
    job = json.loads((cad / "job.dieline.json").read_text())
    expected = BoxParams.model_validate({**BOX, "length": 16 if ui_won else 14})
    assert job["params"]["length"] == expected.length
    assert (samples / "job.dxf").read_bytes() == write_dxf(designs.build(expected)).encode("ascii")
    assert (cad / "job.cf2").exists() == chat_won


def test_stopped_agent_export_cannot_publish_after_staging(tmp_path, monkeypatch):
    store = SettingsStore(tmp_path / "settings.json")
    cad = tmp_path / "cad"
    store.update({"output_dir": str(cad)})
    ag = agent_mod.Agent(store, lambda e: None)
    s, t = _start(ag)
    t.current = BoxParams.model_validate(BOX)
    write = Path.write_bytes

    def stop_after_stage(path, data):
        count = write(path, data)
        ag.cancel(s.id)
        return count

    monkeypatch.setattr(Path, "write_bytes", stop_after_stage)
    with pytest.raises(agent_mod.TurnCancelled):
        ag._dispatch(t, "export_files", {"filename": "job"})
    assert not list(cad.iterdir())


def test_open_job_reports_changes(tmp_path, monkeypatch):
    p = BoxParams(style="reg-glio-nx", length=14, width=8, depth=6, board="32 C KR")
    out = designs.export(p, tmp_path, "RL26-0003", ["cf2"])
    record = json.loads(open(out["job_path"]).read())
    assert record["stem"] == "RL26-0003" and record["params"]["options"]["top_flaps"] == "regular"
    same = designs.open_job(record)
    assert same["changes"] == [] and same["stem"] == "RL26-0003"
    legacy = dict(record)
    legacy.pop("geometry_sha256")
    assert designs.open_job(legacy)["changes"] == []
    changed_segments = designs.open_job({**record, "geometry_sha256": "0" * 64})
    assert changed_segments["changes"] == ["The regenerated cut or rule data differs from the exported geometry."]
    # Board values corrected after the export: reopening says what moved.
    from dieline_engine import boards

    corrected = boards.Board("32 C KR", "C", 0.1875, 0.0625, 0.0625, 0.0625, "artioscad")
    monkeypatch.setitem(boards.BOARDS, "32 C KR", corrected)
    changed = designs.open_job(record)
    assert any("IL was 1/8 in, now 1/16 in" in c for c in changed["changes"])
    assert any(c.startswith("body height") for c in changed["changes"])
    for overrides in ({"version": 99}, {"version": True}, {"version": 0}, {"version": -1},
                      {"board": [1]}, {"board": {"cal_in": float("inf")}},
                      {"summary": "blank"}, {"stem": 7}, {"geometry_sha256": "bad"}):
        with pytest.raises(ValueError):
            designs.open_job({**record, **overrides})
    with pytest.raises(ValueError):
        designs.open_job({"format": "other"})


def test_set_design_syncs_chat_with_the_preview(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    ag = agent_mod.Agent(store, lambda e: None)
    p = BoxParams(style="hsc-gl", length=20, width=10, depth=8)
    ag.set_design("s9", p)
    s = ag.sessions["s9"]
    assert s.current == p and "hsc-gl" in s.note


def test_preflight_checks():
    results = {r.id: r for r in run_checks(rsc(14, 8, 6, closure="outside"))}
    # P1/P3 differ by IL + 1/8 = 3/16 > CAL + 1/32 = 5/32 on 32 B KR.
    assert results["preflight_panels"].status == "warn"
    assert {r.id: r for r in run_checks(rsc())}["preflight_panels"].status == "pass"
    bc = {r.id: r for r in run_checks(rsc(14, 8, 6, "48 BC KR"))}
    assert bc["preflight_pointage"].status == "warn"
    assert "preflight_pointage" not in {r.id for r in run_checks(rsc())}


def test_summary_has_area_and_rule_length():
    m = rsc().meta
    assert m["blank_area_sqft"] == round(45.625 * 14.3125 / 144, 3)
    assert m["rule_length_in"]["cut"] > 0 and m["rule_length_in"]["crease"] > 0


@pytest.mark.parametrize(
    "dims, board, creases_x, creases_y, size",
    [
        # Reference: REG_GLI_EXT 24 7/8 x 11 1/2 x 19 1/2, 48 BC KR, S-E-S-E (sample-table DXF).
        ((24.875, 11.5, 19.5), "48 BC KR", [1.5, 13.3125, 38.5, 50.3125], [5.9375, 26.1875], (75.375, 32.125)),
        # Reference: REG_GLI_EXT 17 5/8 x 12 3/4 x 6, C-flute, S-E-S-E.
        ((17.625, 12.75, 6), "32 C KR", [1.5, 14.4375, 32.25, 45.1875], [6.4375, 12.8125], (62.875, 19.25)),
    ],
)
def test_reg_gli_ext_reproduces_real_rl_blanks(dims, board, creases_x, creases_y, size):
    d = get_style("reg-gli-ext").make(Dims(*dims), get_board(board), {"order": "sese"})
    assert _creases(d) == (creases_x, creases_y)
    assert d.size() == size
    # Square-ended lug 1 1/4" past each score, creased across at the scores.
    lug_cuts = [s for s in d.of_type(LineType.CUT) if max(s.start[0], s.end[0]) <= 1.3125 + 1e-9]
    ys = sorted({p[1] for s in lug_cuts for p in (s.start, s.end)})
    assert (ys[0], ys[-1]) == (creases_y[0] - 1.25, creases_y[1] + 1.25)
    lug_creases = [s for s in d.of_type(LineType.CREASE) if s.start[1] == s.end[1] and min(s.start[0], s.end[0]) == 0]
    assert sorted(s.start[1] for s in lug_creases) == creases_y
    assert overall(run_checks(d)) != "fail"


def test_c_flute_glue_lug_is_one_and_a_half_like_artioscad():
    # Every 32 C KR REG-GLIO-NX design with D <= 12 this year has the 1 1/2" lug.
    assert rsc(15, 15, 4, "32 C KR").meta["glue_lug_in"] == 1.5
    assert rsc(15, 15, 4, "32 B KR").meta["glue_lug_in"] == 1.25


def test_rounding_warning_only_for_variables_the_standard_rounds():
    # REG-GLIO-NX rounds only CD; for 32 B KR regular flaps CD = D + 5/16 is on 1/16
    # even though TD = BD = 3 5/32 (their rounding is off in the standard).
    results = {r.id: r for r in run_checks(rsc())}
    assert results["rounding"].status == "pass"
    # HSC_GL rounds FL = W/2 + CAL - 1/8: an odd-sixteenth width lands between sixteenths.
    hsc = get_style("hsc-gl").make(Dims(14, 8.0625, 6), get_board("32 B KR"), {})
    results = {r.id: r for r in run_checks(hsc)}
    assert results["rounding"].status == "warn" and "FL" in results["rounding"].message


# Blank sizes ArtiosCAD recorded for this year's REG-GLIO-NX designs whose
# descriptions give inside L x W x D (board, dims, width x height).
REAL_REG_GLIO_NX = [
    ("32 C KR", (14, 14, 40), (58.125, 54.5)),
    ("32 C KR", (15, 15, 4), (62.125, 19.5)),
    ("32 C KR", (24, 16, 14), (82.125, 30.5)),
    ("32 C KR", (16, 16, 40), (66.125, 56.5)),
    ("48 BC KR", (13, 13, 15), (54.625, 29.125)),
    ("32 C KR", (24, 12, 24.3125), (74.125, 36.8125)),
    ("32 C KR", (24, 12, 24.625), (74.125, 37.125)),
    ("32 C KR", (16, 10, 10), (54.125, 20.5)),
    ("32 C KR", (22, 16, 6), (78.125, 22.5)),
    ("32 C KR", (12, 10, 10), (46.125, 20.5)),
    ("71 BC KR", (18, 18, 32), (74.625, 51.125)),
]


@pytest.mark.parametrize("board, dims, size", REAL_REG_GLIO_NX)
def test_reg_glio_nx_blank_sizes_match_artioscad(board, dims, size):
    assert rsc(*dims, board=board).size() == size


def test_replacing_a_set_finds_its_dxf_in_an_earlier_sample_folder(tmp_path):
    a = BoxParams(style="reg-glio-nx", length=14, width=8, depth=6)
    b = BoxParams(style="reg-glio-nx", length=16, width=8, depth=6)
    designs.export(a, tmp_path / "cad", "RL26-0010", ["cf2", "dxf"], dxf_dir=tmp_path / "samples-a")
    # The sample folder changed in Settings since; the old DXF is still part of the set.
    with pytest.raises(designs.ExportExists) as exc:
        designs.export(b, tmp_path / "cad", "RL26-0010", ["cf2", "dxf"], dxf_dir=tmp_path / "samples-b")
    assert "RL26-0010.dxf" in exc.value.names
    designs.export(b, tmp_path / "cad", "RL26-0010", ["cf2", "dxf"], overwrite=True, dxf_dir=tmp_path / "samples-b")
    assert not (tmp_path / "samples-a" / "RL26-0010.dxf").exists()
    assert (tmp_path / "samples-b" / "RL26-0010.dxf").exists()


def test_open_job_change_notes_use_fractions(tmp_path):
    p = BoxParams(style="reg-glio-nx", length=14, width=8, depth=6)
    record = json.loads(open(designs.export(p, tmp_path, "RL26-0011")["job_path"]).read())
    record["summary"]["panels_in"] = [8.0, 14.0, 8.0, 14.0]
    record["summary"]["blank_in"] = {"width": 45.5, "height": 14.3125}
    notes = designs.open_job(record)["changes"]
    assert "panels was 8 / 14 / 8 / 14, now 8 / 14 1/8 / 8 1/8 / 14 1/8" in notes
    assert "blank was 45 1/2 x 14 5/16, now 45 5/8 x 14 5/16" in notes


# --- agent tools: seeing, measuring, reference files, web ---------------------

from dieline_engine import files as files_mod  # noqa: E402
from dieline_engine import render  # noqa: E402


def test_create_box_result_includes_a_picture(monkeypatch, tmp_path):
    responses = [
        NS(stop_reason="tool_use", content=[_tool_use("t1", "create_box", BOX)]),
        NS(stop_reason="end_turn", content=[_text("Done.")]),
    ]
    _, s, events, calls = _run(monkeypatch, tmp_path, responses)
    result, picture = s.messages[2], s.messages[3]
    assert result["role"] == "tool" and '"blank_in"' in result["content"]
    assert picture["role"] == "user" and picture["content"][1]["type"] == "image_url"
    assert picture["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    shown = next(e for e in events if e["type"] == "chat.tool_result")
    assert shown["image"].startswith("data:image/png;base64,") and "_image" not in shown["result"]


def test_view_and_geometry_tools(monkeypatch, tmp_path):
    responses = [
        NS(stop_reason="tool_use", content=[_tool_use("t1", "create_box", BOX)]),
        NS(stop_reason="tool_use", content=[
            _tool_use("t2", "view_design", {"region": [0, 0, 4, 6]}),
            _tool_use("t3", "get_geometry", {"line_type": "crease"}),
            _tool_use("t4", "view_design", {"region": [4, 0, 1, 6]}),
        ]),
        NS(stop_reason="end_turn", content=[_text("Checked.")]),
    ]
    _, s, events, _ = _run(monkeypatch, tmp_path, responses)
    view, geo, bad, picture = s.messages[5:9]
    assert view["role"] == "tool" and picture["role"] == "user"
    assert [p["type"] for p in picture["content"]] == ["text", "image_url"]  # one picture: the zoom
    g = json.loads(geo["content"])
    assert g["vertical_score_x"] == [1.25, 15.375, 23.5, 37.625] and g["units"] == "in"
    assert all(seg["type"] == "crease" for seg in g["segments"])
    assert "x1 > x0" in bad["content"] and '"error"' in bad["content"]


def test_render_png_is_a_png_and_regions_validate():
    png, info = render.render_png(rsc())
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and info["units"] == "in"
    assert max(info["image_px"]) <= render.MAX_SIDE + 40
    with pytest.raises(ValueError):
        render.Region.parse([0, 0, 1])
    with pytest.raises(ValueError):
        render.Region.parse([0, 0, float("nan"), 1])


def test_reference_files_are_read_only_and_confined(tmp_path):
    root = tmp_path / "OneDrive - R&L" / "Designs"
    root.mkdir(parents=True)
    (root / "RL26-0500 Acme shipper.txt").write_text("spec: 32 C KR")
    designs.export(BoxParams(style="reg-glio-nx", length=14, width=8, depth=6), root, "RL26-0501", ["cf2", "dxf"])
    (tmp_path / "secret.txt").write_text("not for the agent")
    refs = files_mod.ReferenceFiles([root])
    found = refs.search("acme shipper")
    assert [m["path"].endswith("RL26-0500 Acme shipper.txt") for m in found["matches"]] == [True]
    assert refs.search("rl26-0501", ["dxf"])["total_matches"] == 1
    with pytest.raises(ValueError):
        refs.resolve(str(tmp_path / "secret.txt"))
    with pytest.raises(ValueError):
        refs.resolve(str(root / ".." / ".." / "secret.txt"))
    cf2 = files_mod.read_path(refs.resolve(str(root / "RL26-0501.cf2")))
    assert cf2["kind"] == "cf2" and cf2["_dieline"].size() == pytest.approx((45.625 * 25.4, 14.3125 * 25.4))
    dxf = files_mod.read_path(refs.resolve(str(root / "RL26-0501.dxf")))
    assert dxf["_dieline"].size() == (45.625, 14.3125)


def test_read_file_tool_returns_picture_and_scores(monkeypatch, tmp_path):
    root = tmp_path / "refs"
    designs.export(BoxParams(style="reg-glio-nx", length=14, width=8, depth=6), root, "RL26-0502", ["dxf"])
    store = SettingsStore(tmp_path / "settings.json")
    store.update({"reference_dirs": [str(root)]})
    monkeypatch.setattr(agent_mod.openai, "OpenAI", _fake_client([
        NS(stop_reason="tool_use", content=[
            _tool_use("t1", "read_file", {"path": str(root / "RL26-0502.dxf")}),
            _tool_use("t2", "get_geometry", {"path": str(root / "RL26-0502.dxf"), "line_type": "crease"}),
            _tool_use("t3", "read_file", {"path": str(tmp_path / "settings.json")}),
        ]),
        NS(stop_reason="end_turn", content=[_text("ok")]),
    ], []))
    monkeypatch.setattr(store, "api_key", lambda: "sk-or-v1-test")
    ag = agent_mod.Agent(store, lambda e: None)
    s, t = _start(ag)
    ag._run_turn(s, t, "sk-or-v1-test", ag.settings.settings)
    read, geo, outside, picture = s.messages[2:6]
    assert picture["content"][1]["type"] == "image_url"
    assert json.loads(read["content"])["geometry"]["vertical_score_x"] == [1.25, 15.375, 23.5, 37.625]
    assert json.loads(geo["content"])["horizontal_score_y"] == [4.0, 10.3125]
    assert "outside the reference folders" in outside["content"]


def test_tool_list_follows_settings():
    from dieline_engine.settings import Settings

    names = lambda cfg, private=False: [t["name"] for t in agent_mod.tools_for(cfg, private)]  # noqa: E731
    on = Settings(search_provider="exa")
    assert {"web_search", "web_fetch", "search_files", "view_design"} <= set(names(on))
    assert "web_fetch" not in names(on, private=True) and "web_search" not in names(on, private=True)
    off = Settings(search_provider="off")
    assert "web_search" not in names(off) and "web_fetch" not in names(off)
    assert agent_mod.tools_for(on) == agent_mod.tools_for(Settings(search_provider="exa"))  # stable for caching
    fns = agent_mod.as_functions(agent_mod.tools_for(on))
    assert all(f["type"] == "function" and "parameters" in f["function"] for f in fns)


def test_reference_dir_settings_validate(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    with pytest.raises(ValueError):
        store.update({"reference_dirs": ["relative/folder"]})
    with pytest.raises(ValueError):
        store.update({"search_provider": "google"})
    store.update({"reference_dirs": [str(tmp_path)], "search_provider": "off"})
    assert store.public()["reference_dirs_resolved"] == [str(tmp_path)] and store.public()["search_provider"] == "off"


def test_confinement_checks_happen_before_file_access(tmp_path, monkeypatch):
    root = tmp_path / "refs"
    root.mkdir()
    (root / "a.txt").write_text("hello")
    (tmp_path / "outside.txt").write_text("secret")
    refs = files_mod.ReferenceFiles([root])
    touched = []
    real_resolve = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda self, *a, **k: touched.append(str(self)) or real_resolve(self, *a, **k))
    for bad in ("//otherserver/share/x.txt", str(tmp_path / "outside.txt"), "\\\\?\\C:\\x.txt", "relative.txt"):
        with pytest.raises(ValueError):
            refs.read_bytes(bad)
    assert touched == []  # rejected without resolving (no network or disk access)
    monkeypatch.undo()
    # A symlink inside the root that points outside is refused after resolution.
    try:
        (root / "link.txt").symlink_to(tmp_path / "outside.txt")
    except OSError:
        pytest.skip("creating symlinks is unavailable")
    with pytest.raises(ValueError):
        refs.read_bytes(str(root / "link.txt"))
    assert refs.read_bytes(str(root / "a.txt"))[1] == b"hello"


def test_search_never_follows_links_out_of_the_root(tmp_path):
    root = tmp_path / "refs"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "RL26-9999 secret.ard").write_bytes(b"x")
    try:
        (root / "jump").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks is unavailable")
    assert files_mod.ReferenceFiles([root]).search("rl26-9999")["total_matches"] == 0


def test_reading_a_reference_marks_the_chat_private(monkeypatch, tmp_path):
    root = tmp_path / "refs"
    root.mkdir()
    (root / "spec.txt").write_text("32 C KR")
    store = SettingsStore(tmp_path / "settings.json")
    store.update({"reference_dirs": [str(root)]})
    calls = []
    monkeypatch.setattr(agent_mod.openai, "OpenAI", _fake_client([
        NS(stop_reason="tool_use", content=[_tool_use("t1", "read_file", {"path": str(root / "spec.txt")})]),
        NS(stop_reason="end_turn", content=[_text("ok")]),
    ], calls))
    seen_tools = []
    orig = agent_mod.tools_for
    monkeypatch.setattr(agent_mod, "tools_for",
                        lambda cfg, private=False: seen_tools.append(private) or orig(cfg, private))
    ag = agent_mod.Agent(store, lambda e: None)
    s, t = _start(ag)
    ag._run_turn(s, t, "sk-or-v1-test", ag.settings.settings)
    assert seen_tools == [False, True] and s.private


def test_region_geometry_includes_segments_crossing_the_window():
    d = rsc()
    g = render.geometry(d, "crease", render.Region(5, 6, 6, 8))  # mid-body: a vertical crease crosses it
    assert g["segments"] == [] or all(seg["type"] == "crease" for seg in g["segments"])
    g = render.geometry(d, "crease", render.Region(14, 6, 16, 8))
    assert g["vertical_score_x"] == [15.375] and len(g["segments"]) == 1
    png, _ = render.render_png(d, render.Region(15, 6, 15.5, 6.5))  # tight zoom stays cheap and valid
    assert png[:4] == b"\x89PNG"


def test_dxf_reader_reports_what_it_skips():
    from dieline_engine.dxf import read_dxf

    text = "\n".join(["0", "SECTION", "2", "ENTITIES", "0", "LWPOLYLINE", "8", "0", "0", "TEXT", "8", "0",
                      "0", "LINE", "8", "0", "62", "1", "10", "0", "20", "0", "11", "1", "21", "0",
                      "0", "ENDSEC", "0", "EOF"])
    d, warnings = read_dxf(text)
    assert len(d.segments) == 1
    assert any("1 LWPOLYLINE" in w and "1 TEXT" in w for w in warnings)


def test_list_faces_tool(monkeypatch, tmp_path):
    responses = [
        NS(stop_reason="tool_use", content=[_tool_use("t1", "create_box", BOX)]),
        NS(stop_reason="tool_use", content=[_tool_use("t2", "list_faces", {})]),
        NS(stop_reason="end_turn", content=[_text("Listed.")]),
    ]
    _, s, _, _ = _run(monkeypatch, tmp_path, responses)
    tool = next(m for m in s.messages if m.get("role") == "tool" and "viewing" in m.get("content", ""))
    data = json.loads(tool["content"])
    assert "OUTSIDE" in data["viewing"] and data["units"] == "in" and "folding" in data
    assert len(tool["content"]) < 12000  # compact: no flat coordinates, no operation ids
    ids = {f["id"] for f in data["faces"]}
    assert {"P1", "P2", "P3", "P4", "glue_lug", "top_flap_P1", "bottom_flap_P4"} <= ids
    assert data["selectors"]["end_panels"] and "body_panels" in data["selectors"]

import io
import json

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


def rsc(l=300.0, w=200.0, d=150.0, board="C-32ECT") -> Dieline:
    return get_style("fefco-0201").build(Dims(l, w, d), get_board(board))


def test_rsc_blank_size_follows_allowances():
    d = rsc()
    a = d.meta["allowances_mm"]
    t = d.meta["caliper_mm"]
    expected_w = a["glue_flap"] + 2 * (300 + a["length_panel"]) + 2 * (200 + a["width_panel"])
    expected_h = (200 + a["flap"]) + (150 + a["depth"])
    w, h = d.size()
    assert w == pytest.approx(expected_w)
    assert h == pytest.approx(expected_h)
    assert t == 4.0


def test_rsc_passes_geometry_checks():
    results = {r.id: r for r in run_checks(rsc())}
    for check in ("line_types", "zero_length", "closed_outline", "overlaps", "sheet_fit"):
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
    back = read_cf2(text)
    assert back.warnings == []
    assert segments_equal(d.segments, back.dieline.segments)


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
        BoxParams.model_validate({"style": "fefco-0201", "length": -1, "width": 1, "depth": 1, "board": "C-32ECT"})
    with pytest.raises(ValueError):
        BoxParams.model_validate(
            {"style": "fefco-0201", "length": 1, "width": 1, "depth": 1, "board": "C-32ECT", "x": 0}
        )


def test_agent_view_has_no_geometry():
    design = designs.create_box(
        BoxParams(style="fefco-0201", length=12, width=8, depth=6, units="in", board="B-32ECT")
    )
    view = designs.agent_view(design)
    assert "cf2" not in view
    assert view["params"]["units"] == "in"
    assert design["summary"]["inside_dims_mm"]["length"] == pytest.approx(304.8)


def test_export_writes_into_output_dir_only(tmp_path):
    params = BoxParams(style="fefco-0201", length=300, width=200, depth=150, board="C-32ECT")
    out = designs.export_cf2(params, tmp_path, "my box")
    assert out["path"].endswith("my box.cf2")
    assert open(out["path"], newline="").read().count("\r\n") > 10
    # Directory parts are stripped, so the file still lands in tmp_path.
    out2 = designs.export_cf2(params, tmp_path, "../../evil")
    assert str(tmp_path) in out2["path"]


def test_rpc_round_trip(tmp_path):
    out = io.StringIO()
    server = Server(out, SettingsStore(tmp_path / "settings.json"))
    server._dispatch({"jsonrpc": "2.0", "id": 1, "method": "box.create",
                      "params": {"style": "fefco-0201", "length": 300, "width": 200, "depth": 150,
                                 "board": "C-32ECT"}})
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
    results = {r.id: r for r in run_checks(rsc(300, 200, 10))}
    assert results["crossings"].status == "pass", results["crossings"]
    assert results["closed_outline"].status == "pass"


def test_crossing_lines_fail():
    d = rsc()
    w, h = d.size()
    d.add_line((0, 0), (w, h), LineType.CUT)
    d.add_line((0, h), (w, 0), LineType.CUT)
    assert {r.id: r for r in run_checks(d)}["crossings"].status == "fail"


def test_export_refuses_failing_design(tmp_path):
    params = BoxParams(style="fefco-0201", length=2, width=2, depth=10, board="E-32ECT")
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


def test_api_key_must_be_an_api_key(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    with pytest.raises(ValueError):
        store.update({"api_key": "sk-ant-oat01-subscription-token"})

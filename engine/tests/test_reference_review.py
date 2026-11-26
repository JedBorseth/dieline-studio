import json
import zipfile

import openai
import httpx
import pytest
from PIL import ImageDraw

from dieline_engine import files, render
from dieline_engine.agent import Agent, Session, Turn
from dieline_engine.cf2 import read_cf2, write_cf2
from dieline_engine.dxf import read_dxf, write_dxf
from dieline_engine.geometry import Dieline, LineType
from dieline_engine.settings import Settings, SettingsStore


def test_render_uses_native_reference_units_and_origin(monkeypatch):
    design = Dieline("offset", units="in")
    design.add_line((-1.25, 0), (2.5, 1.25), LineType.CUT)
    labels = []
    original_text = ImageDraw.ImageDraw.text

    def record_text(self, xy, text, *args, **kwargs):
        labels.append(text)
        return original_text(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record_text)
    _, inch_view = render.render_png(design)
    assert any("window x -1 1/4..2 1/2" in label for label in labels)
    assert inch_view["origin"] == "native design coordinates, x right and y up"

    labels.clear()
    reference = read_cf2(write_cf2(design)).dieline
    _, mm_view = render.render_png(reference)
    assert any("window x -31.75..63.5" in label for label in labels)
    assert "blank 95.25 x 31.75 mm" in labels
    assert mm_view["units"] == render.geometry(reference)["units"] == "mm"


def test_docx_extraction_bounds_uncompressed_xml(tmp_path, monkeypatch):
    path = tmp_path / "reference.docx"
    xml = b"<w:p><w:t>reference</w:t></w:p>"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)

    monkeypatch.setattr(files, "MAX_DOCX_XML_BYTES", len(xml))
    assert files.read_path(path)["text"] == "reference\n"

    monkeypatch.setattr(files, "MAX_DOCX_XML_BYTES", len(xml) - 1)
    with pytest.raises(ValueError, match="Word document XML is over"):
        files.read_path(path)


def test_dxf_reads_explicit_bylayer_colour_case_insensitively():
    design = Dieline("bylayer", units="in")
    design.add_line((0, 0), (1, 1), LineType.CREASE)
    text = write_dxf(design).replace(" 62\r\n7", " 62\r\n2")
    text = text.replace(" 62\r\n2\r\n 10", " 62\r\n256\r\n 10")
    text = text.replace("  8\r\nDesign", "  8\r\ndesign")

    read, warnings = read_dxf(text)
    assert warnings == ["no units in the file; read as in"]
    assert read.segments == design.segments


def test_openai_sdk_stream_is_assembled_and_replayed(tmp_path, monkeypatch):
    """The real openai SDK, through a mock transport, with OpenRouter-style chunks:
    text, reasoning_details fragments, and a tool call whose arguments arrive in pieces."""
    requests = []
    events = []

    def sse(chunks):
        return "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"

    def chunk(delta, finish=None):
        return {"id": "gen-1", "object": "chat.completion.chunk", "created": 1, "model": "anthropic/claude-opus-5.5",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    first = [
        chunk({"role": "assistant", "reasoning_details": [{"type": "reasoning.text", "text": "Need ", "index": 0}]}),
        chunk({"reasoning_details": [
            {"type": "reasoning.encrypted", "data": "opaque", "index": 1, "id": "reason-1"},
            {"type": "reasoning.text", "text": "options.", "index": 0, "signature": "sig"},
            {"type": "reasoning.encrypted", "data": "second", "id": "reason-2"},
        ]}),
        chunk({"content": "Checking."}),
        chunk({"tool_calls": [{"index": 1, "id": "call_2", "type": "function",
                               "function": {"name": "list_options", "arguments": "{}"}},
                              {"index": 0, "type": "function",
                               "function": {"name": "list_options", "arguments": "{"}}]}),
        chunk({"tool_calls": [{"index": 0, "id": "call_1", "function": {"arguments": "}"}}]}),
        chunk({}, "tool_calls"),
    ]
    second = [chunk({"content": "Done."}), chunk({}, "stop")]

    def respond(request):
        assert request.headers.get_list("authorization") == ["Bearer sk-or-v1-offline"]
        assert "openai-project" not in request.headers
        requests.append(json.loads(request.content))
        body = sse(first if len(requests) == 1 else second)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    agent = Agent(SettingsStore(tmp_path / "settings.json"), events.append)
    turn = Turn("test", 1, [{"role": "user", "content": "What styles?"}], None)
    agent.sessions[turn.id] = Session(turn.id, active=turn)
    sdk = openai.OpenAI
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "authorization: Bearer ambient")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "example-project")
    monkeypatch.setattr(openai, "OpenAI", lambda **kwargs: sdk(
        **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(respond))))
    cfg = Settings()
    assert agent._loop(turn, "sk-or-v1-offline", cfg) == "end_turn"

    assistant = turn.messages[1]
    assert assistant["tool_calls"][0] == {"id": "call_1", "type": "function",
                                          "function": {"name": "list_options", "arguments": "{}"}}
    assert assistant["reasoning_details"] == [
        {"type": "reasoning.text", "text": "Need options.", "index": 0, "signature": "sig"},
        {"type": "reasoning.encrypted", "data": "opaque", "index": 1, "id": "reason-1"},
        {"type": "reasoning.encrypted", "data": "second", "id": "reason-2"},
    ]
    assert [(msg["role"], msg["tool_call_id"]) for msg in turn.messages[2:4]] == [
        ("tool", "call_1"), ("tool", "call_2"),
    ]
    assert [(e["type"], e["tool_use_id"].split(":", 1)[1]) for e in events if e["type"].startswith("chat.tool_")] == [
        ("chat.tool_start", "1:1:call_1"), ("chat.tool_result", "1:1:call_1"),
        ("chat.tool_start", "1:1:call_2"), ("chat.tool_result", "1:1:call_2"),
    ]
    assert len({e["tool_use_id"].split(":", 1)[0] for e in events if e["type"].startswith("chat.tool_")}) == 1  # the UI's row ids carry the model step; the replayed history keeps the provider's ids
    # The second request replays the assistant step unchanged, reasoning included.
    assert requests[1]["messages"][2] == assistant
    assert requests[0]["reasoning"] == {"effort": cfg.effort} and requests[0]["cache_control"]["type"] == "ephemeral"
    assert requests[0]["tools"] == requests[1]["tools"]
    assert [e["text"] for e in events if e["type"] == "chat.thinking"] == ["Need ", "options."]


@pytest.mark.parametrize(("final", "error"), [
    (None, "stream ended before the model finished"),
    ({"error": {"code": 429, "message": "provider rate limit"}}, "Rate limited by OpenRouter"),
])
def test_openai_sdk_failed_stream_does_not_dispatch_tools(tmp_path, monkeypatch, final, error):
    partial = {"id": "gen-1", "object": "chat.completion.chunk", "created": 1, "model": "test",
               "choices": [{"index": 0, "finish_reason": None, "delta": {"tool_calls": [
                   {"index": 0, "id": "call_1", "type": "function",
                    "function": {"name": "list_options", "arguments": "{}"}}
               ]}}]}
    body = "data: " + json.dumps(partial) + "\n\n"
    if final:
        body += "data: " + json.dumps(final) + "\n\n"
    sdk = openai.OpenAI
    monkeypatch.setattr(openai, "OpenAI", lambda **kwargs: sdk(
        **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)))))
    events = []
    agent = Agent(SettingsStore(tmp_path / "settings.json"), events.append)
    turn = Turn("test", 1, [{"role": "user", "content": "List styles."}], None)
    session = agent.sessions[turn.id] = Session(turn.id, active=turn)
    agent._run_turn(session, turn, "sk-or-v1-offline", agent.settings.settings)

    assert session.messages == []
    assert [event["type"] for event in events] == ["chat.error", "chat.turn_end"]
    assert error in events[0]["message"]

def test_dxf_uses_insunits_for_scale():
    design = Dieline("metric", units="in")
    design.add_line((0, 0), (10, 5), LineType.CUT)
    text = write_dxf(design).replace("  9\r\n$EXTMIN",
                                     "  9\r\n$MEASUREMENT\r\n 70\r\n1\r\n  9\r\n$EXTMIN")
    unitless, warnings = read_dxf(text)
    assert unitless.units == "in" and unitless.size() == (10, 5)
    assert "no units" in warnings[0]

    text = text.replace("  9\r\n$EXTMIN", "  9\r\n$INSUNITS\r\n 70\r\n5\r\n  9\r\n$EXTMIN")
    metric, warnings = read_dxf(text)
    assert metric.units == "mm" and metric.size() == (100, 50)
    assert not warnings


@pytest.mark.parametrize("tool", ["web_search", "web_fetch"])
def test_private_turn_blocks_web_dispatch(tool, tmp_path):
    config = SettingsStore(tmp_path / "settings.json")
    config.settings.search_provider = "exa"
    agent = Agent(config, lambda event: None)
    turn = Turn("test", 1, [], None, private=True)
    with pytest.raises(ValueError, match="Web tools are off"):
        agent._dispatch(turn, tool, {})


def test_chat_requires_new_session_after_model_switch(tmp_path):
    config = SettingsStore(tmp_path / "settings.json")
    agent = Agent(config, lambda event: None)
    agent.sessions["test"] = Session("test", model="other/model")
    with pytest.raises(ValueError, match="Start a new chat"):
        agent.start_turn("test", "Continue")

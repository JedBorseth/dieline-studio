"""Chat agent: Claude picks parameters, the engine draws.

Runs a manual streaming tool loop on the Messages API so text and reasoning
summaries stream straight into the UI. The model's only way to affect geometry
is `create_box`, whose inputs are parameters; it never sees or writes
coordinates.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

import anthropic
from pydantic import ValidationError

from . import designs
from .designs import BoxParams
from .settings import SettingsStore

log = logging.getLogger(__name__)

Emit = Callable[[dict[str, Any]], None]

MAX_TOKENS = 64000
MAX_STEPS = 12  # model calls per user turn
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM_PROMPT = """\
You are the design assistant in Dieline Studio, a tool for packaging designers \
who make corrugated boxes and folding cartons for import into ArtiosCAD.

You choose parameters; a deterministic engine draws the dieline. You never \
write, estimate or edit coordinates. To make or change a design, call \
create_box with the full parameter set; the engine generates it, runs the \
geometry and manufacturing checks, and shows it in the preview pane next to \
this chat. Call list_options when you need the available styles and boards.

Dimensions are inside dimensions (length x width x depth) unless the user says \
otherwise; ask if it is ambiguous, and ask for any missing dimension or board \
rather than guessing. Convert units only by setting `units`; the engine does \
the arithmetic.

After creating a design, report the blank size and any failed or warning \
checks in a sentence or two. Allowances are not yet calibrated against \
ArtiosCAD; mention this when the user is about to use a design for \
production. Export a CF2 file with export_cf2 only when the user asks for it.\
"""

TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_options",
        "description": "List the box styles and board grades the engine supports, with ids to use in create_box.",
        "eager_input_streaming": True,
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "create_box",
        "description": (
            "Generate a dieline from a style, inside dimensions and a board grade. "
            "Replaces the design shown in the preview. Returns the blank size, panel "
            "sizes and check results (no coordinates)."
        ),
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "style": {"type": "string", "description": "Style id from list_options, e.g. 'fefco-0201'"},
                "length": {"type": "number", "description": "Inside length"},
                "width": {"type": "number", "description": "Inside width"},
                "depth": {"type": "number", "description": "Inside depth"},
                "units": {"type": "string", "enum": ["mm", "in"], "description": "Units of the three dimensions"},
                "board": {"type": "string", "description": "Board id from list_options, e.g. 'C-32ECT'"},
            },
            "required": ["style", "length", "width", "depth", "units", "board"],
            "additionalProperties": False,
        },
    },
    {
        "name": "export_cf2",
        "description": "Write the current design to a CF2 file in the user's export folder. Returns the file path.",
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "Optional file name without folders"},
            },
            "additionalProperties": False,
        },
    },
]


class TurnCancelled(Exception):
    pass


@dataclass
class Session:
    id: str
    messages: list[Any] = field(default_factory=list)
    current: BoxParams | None = None
    busy: bool = False
    cancel: threading.Event = field(default_factory=threading.Event)


class Agent:
    def __init__(self, settings: SettingsStore, emit: Emit) -> None:
        self.settings = settings
        self.emit = emit
        self.sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def session(self, session_id: str) -> Session:
        with self._lock:
            return self.sessions.setdefault(session_id, Session(session_id))

    def reset(self, session_id: str) -> None:
        with self._lock:
            s = self.sessions.get(session_id)
            if s and s.busy:
                raise RuntimeError("session is busy; cancel the turn first")
            self.sessions.pop(session_id, None)

    def cancel(self, session_id: str) -> None:
        s = self.sessions.get(session_id)
        if s:
            s.cancel.set()

    def start_turn(self, session_id: str, text: str) -> None:
        """Validate and start a turn on a worker thread; progress arrives as events."""
        if not text.strip():
            raise ValueError("empty message")
        key = self.settings.api_key()
        if not key:
            raise RuntimeError("No Anthropic API key set. Add one in Settings.")
        s = self.session(session_id)
        with self._lock:
            if s.busy:
                raise RuntimeError("a turn is already running in this session")
            s.busy = True
            s.cancel.clear()
        threading.Thread(target=self._run_turn, args=(s, text, key), daemon=True).start()

    # ------------------------------------------------------------------ loop

    def _run_turn(self, s: Session, text: str, api_key: str) -> None:
        checkpoint = len(s.messages)
        s.messages.append({"role": "user", "content": text})
        sid = s.id
        try:
            stop_reason = self._loop(s, api_key)
            self.emit({"type": "chat.turn_end", "session_id": sid, "stop_reason": stop_reason})
        except TurnCancelled:
            del s.messages[checkpoint:]  # drop the whole turn; history stays append-only
            self.emit({"type": "chat.turn_end", "session_id": sid, "stop_reason": "cancelled"})
        except Exception as exc:  # surface every failure to the UI, keep the engine alive
            del s.messages[checkpoint:]
            self.emit({"type": "chat.error", "session_id": sid, "message": _describe_error(exc)})
            self.emit({"type": "chat.turn_end", "session_id": sid, "stop_reason": "error"})
        finally:
            s.busy = False

    def _loop(self, s: Session, api_key: str) -> str:
        cfg = self.settings.settings
        client = anthropic.Anthropic(api_key=api_key)
        sid = s.id
        json_retries = 0
        for _ in range(MAX_STEPS):
            try:
                with client.beta.messages.stream(
                    model=cfg.model,
                    max_tokens=MAX_TOKENS,
                    system=SYSTEM_PROMPT,
                    tools=TOOLS,
                    messages=s.messages,
                    thinking={"type": "adaptive", "display": "summarized"},
                    output_config={"effort": cfg.effort},
                    cache_control={"type": "ephemeral"},
                    betas=[FALLBACK_BETA],
                    fallbacks="default",
                ) as stream:
                    for event in stream:
                        if s.cancel.is_set():
                            raise TurnCancelled()
                        self._forward(sid, event)
                    response = stream.get_final_message()
                json_retries = 0
            except ValueError:
                # Tool input JSON the SDK could not parse at all; re-issue the step.
                json_retries += 1
                if json_retries > 2:
                    raise
                continue

            if response.stop_reason == "pause_turn":
                s.messages.append({"role": "assistant", "content": response.content})
                continue

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason == "refusal":
                s.messages.append({"role": "assistant", "content": response.content})
                details = getattr(response, "stop_details", None)
                category = getattr(details, "category", None) if details else None
                self.emit({"type": "chat.error", "session_id": sid,
                           "message": f"Claude declined this request{f' ({category})' if category else ''}."})
                return "refusal"
            if response.stop_reason == "max_tokens" and tool_uses:
                raise RuntimeError("tool input was cut off at max_tokens")

            s.messages.append({"role": "assistant", "content": response.content})
            if not tool_uses:
                return response.stop_reason or "end_turn"

            results = [self._run_tool(s, b) for b in tool_uses]
            s.messages.append({"role": "user", "content": results})
        return "max_steps"

    def _forward(self, sid: str, event: Any) -> None:
        if event.type == "content_block_start":
            block = event.content_block
            if block.type == "tool_use":
                self.emit({"type": "chat.tool_start", "session_id": sid,
                           "tool_use_id": block.id, "name": block.name})
        elif event.type == "content_block_delta":
            delta = event.delta
            if delta.type == "text_delta":
                self.emit({"type": "chat.text", "session_id": sid, "text": delta.text})
            elif delta.type == "thinking_delta" and delta.thinking:
                self.emit({"type": "chat.thinking", "session_id": sid, "text": delta.thinking})

    # ----------------------------------------------------------------- tools

    def _run_tool(self, s: Session, block: Any) -> dict[str, Any]:
        args = block.input if isinstance(block.input, dict) else None
        try:
            if args is None:
                raise ValueError(json.dumps({"INVALID_JSON": json.dumps(block.input)}))
            result = self._dispatch(s, block.name, args)
            is_error = False
        except (ValidationError, ValueError, OSError) as exc:
            result = {"error": str(exc)}
            is_error = True
        self.emit({"type": "chat.tool_result", "session_id": s.id, "tool_use_id": block.id,
                   "name": block.name, "input": args, "result": result, "is_error": is_error})
        return {"type": "tool_result", "tool_use_id": block.id,
                "content": json.dumps(result), "is_error": is_error}

    def _dispatch(self, s: Session, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "list_options":
            return designs.catalog()
        if name == "create_box":
            params = BoxParams.model_validate(args)
            design = designs.create_box(params)
            s.current = params
            self.emit({"type": "design", "session_id": s.id, "design": design})
            return designs.agent_view(design)
        if name == "export_cf2":
            if s.current is None:
                raise ValueError("no design yet; call create_box first")
            filename = args.get("filename")
            if filename is not None and not isinstance(filename, str):
                raise ValueError("filename must be a string")
            out = self.settings.settings.resolved_output_dir()
            return designs.export_cf2(s.current, out, filename)
        raise ValueError(f"unknown tool {name!r}")


def _describe_error(exc: Exception) -> str:
    if isinstance(exc, anthropic.AuthenticationError):
        return "The Anthropic API key was rejected. Check it in Settings."
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "This API key lacks permission for the selected model."
    if isinstance(exc, anthropic.NotFoundError):
        return "Model not found. Pick another model in Settings."
    if isinstance(exc, anthropic.RateLimitError):
        return "Rate limited by the Anthropic API. Wait a moment and retry."
    if isinstance(exc, anthropic.APIStatusError):
        return f"Anthropic API error {exc.status_code}: {exc.message}"
    if isinstance(exc, anthropic.APIConnectionError):
        return "Could not reach the Anthropic API. Check the network connection."
    return f"{type(exc).__name__}: {exc}"

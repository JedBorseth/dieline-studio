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
from dataclasses import dataclass, field, replace
from typing import Any, Callable

import anthropic
from pydantic import ValidationError

from . import designs
from .designs import BoxParams
from .settings import Settings, SettingsStore

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


class TurnDiscarded(Exception):
    """The turn can't be kept in the history; the message is shown to the user."""


@dataclass
class Session:
    id: str
    messages: list[Any] = field(default_factory=list)
    current: BoxParams | None = None
    busy: bool = False
    cancel: threading.Event = field(default_factory=threading.Event)
    stream: Any = None  # the in-flight SDK stream, closed by cancel()


class Agent:
    def __init__(self, settings: SettingsStore, emit: Emit) -> None:
        self.settings = settings
        self.emit = emit
        self.sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def reset(self, session_id: str) -> None:
        with self._lock:
            s = self.sessions.get(session_id)
            if s and s.busy:
                raise RuntimeError("session is busy; stop the turn first")
            self.sessions.pop(session_id, None)

    def cancel(self, session_id: str) -> None:
        with self._lock:
            s = self.sessions.get(session_id)
            if not s or not s.busy:
                return
            s.cancel.set()
            stream = s.stream
        if stream is not None:
            try:
                stream.close()  # unblocks a stalled read in the worker
            except Exception:  # closing a finished stream is harmless
                pass

    def start_turn(self, session_id: str, text: str) -> None:
        """Validate and start a turn on a worker thread; progress arrives as events."""
        if not text.strip():
            raise ValueError("empty message")
        key = self.settings.api_key()
        if not key:
            raise RuntimeError("No Anthropic API key set. Add one in Settings.")
        with self._lock:  # look up and reserve atomically so reset/send can't race
            s = self.sessions.setdefault(session_id, Session(session_id))
            if s.busy:
                raise RuntimeError("a turn is already running in this session")
            s.busy = True
            s.cancel.clear()
        threading.Thread(target=self._run_turn, args=(s, text, key), daemon=True).start()

    # ------------------------------------------------------------------ loop

    def _run_turn(self, s: Session, text: str, api_key: str) -> None:
        checkpoint = len(s.messages)
        current_before = s.current  # restored if the turn is discarded
        s.messages.append({"role": "user", "content": text})
        events: list[dict[str, Any]] = []
        try:
            stop_reason = self._loop(s, api_key)
        except TurnCancelled:
            del s.messages[checkpoint:]  # drop the whole turn; history stays append-only
            s.current = current_before
            stop_reason = "cancelled"
        except Exception as exc:  # surface every failure to the UI, keep the engine alive
            log.warning("turn failed: %s", exc)
            del s.messages[checkpoint:]
            s.current = current_before
            events.append({"type": "chat.error", "session_id": s.id, "message": _describe_error(exc)})
            stop_reason = "error"
        with self._lock:  # release before announcing, so an immediate follow-up is accepted
            s.busy = False
            s.stream = None
        for event in events:
            self.emit(event)
        self.emit({"type": "chat.turn_end", "session_id": s.id, "stop_reason": stop_reason})

    def _check_cancel(self, s: Session) -> None:
        if s.cancel.is_set():
            raise TurnCancelled()

    def _loop(self, s: Session, api_key: str) -> str:
        cfg = replace(self.settings.settings)  # snapshot: settings changes apply from the next turn
        sid = s.id
        with anthropic.Anthropic(api_key=api_key) as client:
            for _ in range(MAX_STEPS):
                self._check_cancel(s)
                response = self._step(client, cfg, s)
                done = self._after_step(s, response)
                if done is not None:
                    return done
        # Every step ended in tool calls. The last tool results have no reply, so
        # discard the turn rather than leave an unanswered exchange in the history.
        raise TurnDiscarded(f"Stopped after {MAX_STEPS} tool steps without an answer.")

    def _step(self, client: anthropic.Anthropic, cfg: Settings, s: Session) -> Any:
        """One streamed model call. A ValueError from the SDK (tool input it
        couldn't parse) propagates and discards the turn: retrying would leave
        partly streamed text and tool cards in the UI."""
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
                s.stream = stream
                for event in stream:
                    self._check_cancel(s)
                    self._forward(s.id, event)
                self._check_cancel(s)
                return stream.get_final_message()
        except TurnCancelled:
            raise
        except Exception:
            if s.cancel.is_set():  # the stream was closed under us by cancel()
                raise TurnCancelled() from None
            raise
        finally:
            s.stream = None

    def _after_step(self, s: Session, response: Any) -> str | None:
        """Record a model step and run its tools. Returns a stop reason when the turn is over."""
        sid = s.id
        stop = response.stop_reason
        if stop == "pause_turn":
            s.messages.append({"role": "assistant", "content": response.content})
            return None

        if stop == "refusal":
            # A refusal may be empty or cut a tool call off; neither can stay in
            # the history, so the whole turn is discarded.
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise TurnDiscarded(f"Claude declined this request{f' ({category})' if category else ''}.")
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if stop != "tool_use" and tool_uses:
            # Truncation cut a tool call off. A tool_use without a tool_result
            # would break the next request, so drop the whole turn.
            raise TurnDiscarded(f"Claude's reply was cut off mid tool call ({stop}).")
        s.messages.append({"role": "assistant", "content": response.content})
        if stop in ("max_tokens", "model_context_window_exceeded"):
            # Never run tools from a truncated turn: their input may be cut off.
            what = "the reply length limit" if stop == "max_tokens" else "the context window"
            self.emit({"type": "chat.error", "session_id": sid,
                       "message": f"The reply was cut off at {what}. Start a new design to continue."})
            return stop
        if stop != "tool_use":
            return stop or "end_turn"

        results = []
        for block in tool_uses:
            self._check_cancel(s)
            results.append(self._run_tool(s, block))
        s.messages.append({"role": "user", "content": results})
        return None

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
    if isinstance(exc, (TurnDiscarded, ValueError, RuntimeError)):
        return str(exc)
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

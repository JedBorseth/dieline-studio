"""Line-delimited JSON-RPC 2.0 over stdio.

The desktop shell (Tauri) and the Vite dev bridge spawn this process and talk
to it through stdin/stdout. stdout carries protocol messages only; logs go to
stderr. Server -> client notifications use method "event" with a typed payload
(see app/src/lib/protocol.ts for the matching TypeScript types).
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, TextIO

from pydantic import ValidationError

from . import __version__, designs, exports, models
from .agent import Agent
from .designs import BoxParams
from .designstore import DesignChanged, DesignStore, new_design
from .settings import SettingsStore

log = logging.getLogger(__name__)

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
APP_ERROR = -32000
EXPORT_EXISTS = -32001  # the UI offers "Replace" for this one
DESIGN_CHANGED = -32002  # the UI refreshes the design it shows
EXPORT_LOCKED = -32003  # another export holds a folder: try again
EXPORT_LOCK_STALE = -32004  # the lock looks abandoned: the UI offers to break exactly data.lock_id


class RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class Server:
    def __init__(self, out: TextIO, settings: SettingsStore | None = None) -> None:
        self._out = out
        self._write_lock = threading.Lock()
        self.settings = settings or SettingsStore()
        self.store = DesignStore(self.emit)
        self.agent = Agent(self.settings, self.emit, self.store)
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="rpc")
        self.methods: dict[str, Callable[[dict[str, Any]], Any]] = {
            "engine.info": self._info,
            "box.create": lambda p: designs.create_box(BoxParams.model_validate(p)),
            "design.replace": self._design_replace,
            "design.get": self._design_get,
            "box.export": self._export,
            "export.plan": self._export_plan,
            "export.pickup": self._export_pickup,
            "export.recover": self._export_recover,
            "job.open": self._job_open,
            "settings.get": lambda p: self.settings.public(),
            "models.list": self._models,
            "settings.update": self._settings_update,
            "chat.send": self._chat_send,
            "chat.set_design": self._chat_set_design,
            "layout.choose": self._layout_choose,
            "artwork.approve_import": self._approve_import,
            "artwork.acknowledge": self._acknowledge,
            "chat.cancel": lambda p: self.agent.cancel(_str(p, "session_id")) or {"ok": True},
            "chat.reset": lambda p: self.agent.reset(_str(p, "session_id")) or {"ok": True},
        }

    # -------------------------------------------------------------- transport

    def _send(self, msg: dict[str, Any]) -> None:
        line = json.dumps(msg, separators=(",", ":"), ensure_ascii=False)
        with self._write_lock:
            self._out.write(line + "\n")
            self._out.flush()

    def emit(self, payload: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": "event", "params": payload})

    def handle_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            self._send({"jsonrpc": "2.0", "id": None, "error": {"code": PARSE_ERROR, "message": str(exc)}})
            return
        if not isinstance(msg, dict) or not isinstance(msg.get("method"), str):
            self._send({"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                        "error": {"code": INVALID_REQUEST, "message": "invalid request"}})
            return
        self._pool.submit(self._dispatch, msg)

    def _dispatch(self, msg: dict[str, Any]) -> None:
        req_id = msg.get("id")
        params = {} if msg.get("params") is None else msg["params"]
        try:
            fn = self.methods.get(msg["method"])
            if fn is None:
                raise RpcError(METHOD_NOT_FOUND, f"unknown method {msg['method']!r}")
            if not isinstance(params, dict):
                raise RpcError(INVALID_PARAMS, "params must be an object")
            result = fn(params)
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "result": result}
        except RpcError as exc:
            reply = {"jsonrpc": "2.0", "id": req_id, "error": {"code": exc.code, "message": str(exc)}}
        except ValidationError as exc:
            reply = {"jsonrpc": "2.0", "id": req_id,
                     "error": {"code": INVALID_PARAMS, "message": _validation_message(exc)}}
        except DesignChanged:
            reply = {"jsonrpc": "2.0", "id": req_id, "error": {
                "code": DESIGN_CHANGED,
                "message": "The design changed since this was shown (the chat updated it). Showing the current "
                           "design; check it, then try again."}}
        except designs.ExportExists as exc:
            reply = {"jsonrpc": "2.0", "id": req_id,
                     "error": {"code": EXPORT_EXISTS, "message": str(exc), "data": {"files": exc.names}}}
        except exports.LockHeld as exc:
            reply = {"jsonrpc": "2.0", "id": req_id,
                     "error": {"code": EXPORT_LOCK_STALE if exc.stale else EXPORT_LOCKED, "message": str(exc),
                               "data": {"stale": exc.stale, "folder": str(exc.folder), "lock_id": exc.lock_id}}}
        except (ValueError, RuntimeError) as exc:  # expected, user-facing failures
            reply = {"jsonrpc": "2.0", "id": req_id, "error": {"code": APP_ERROR, "message": str(exc)}}
        except Exception as exc:
            log.exception("method %s failed", msg.get("method"))
            reply = {"jsonrpc": "2.0", "id": req_id, "error": {"code": APP_ERROR, "message": str(exc)}}
        if req_id is not None:  # notifications get no reply
            self._send(reply)

    def serve(self, inp: TextIO) -> None:
        self.emit({"type": "engine.ready", "version": __version__})
        for line in inp:
            self.handle_line(line)
        # stdin closed (the app quit): finish requests already received, then exit.
        self._pool.shutdown(wait=True)

    # ---------------------------------------------------------------- methods

    def _info(self, _: dict[str, Any]) -> dict[str, Any]:
        return {"version": __version__, **designs.catalog(), "settings": self.settings.public()}

    def _design_replace(self, p: dict[str, Any]) -> dict[str, Any]:
        """The form's Generate and a reopened job: a new design for the session, made from one
        standard part, or from a job record's parts. The designer's own action, so it doesn't
        wait on a revision."""
        expected: Any = "any"
        if "expected" in p:  # what the form showed when the designer pressed Generate (null: nothing)
            e = p["expected"]
            if e is not None and not (isinstance(e, dict) and isinstance(e.get("design_id"), str)
                                      and _is_int(e.get("revision"))):
                raise RpcError(INVALID_PARAMS, "expected must be {design_id, revision} or null")
            expected = None if e is None else (e["design_id"], e["revision"])
        if p.get("parts") is not None:
            raw = p["parts"]
            if not isinstance(raw, list) or not raw or not all(isinstance(x, dict) for x in raw):
                raise RpcError(INVALID_PARAMS, "parts must be a non-empty list of parts")
            parts = [{**x, "params": designs.parse_params(x.get("params") or {})} for x in raw]
            return self.agent.set_parts(_str(p, "session_id"), parts, p.get("groups") or [], p.get("links") or [],
                                        expected)
        params = BoxParams.model_validate(p.get("params") or {})
        return self.agent.set_design(_str(p, "session_id"), params, expected)

    def _design_get(self, p: dict[str, Any]) -> dict[str, Any] | None:
        cur = self.store.current(_str(p, "session_id"))
        return self.store.payload(cur) if cur else None

    def _export_target(self, p: dict[str, Any]) -> tuple[list[exports.ExportPart], dict[str, Any]]:
        """The parts an export (or its plan) is for, and what the manifest records about the design."""
        if "design_id" in p:  # the export bar: exactly the snapshot it showed
            if not _is_int(p.get("revision")):
                raise RpcError(INVALID_PARAMS, "revision must be the revision shown (a whole number)")
            snap = self.store.snapshot(_str(p, "session_id"), _str(p, "design_id"), p.get("revision"))
            checks = self.store.check_exportable(snap)  # the design's own checks block it too
            payload = self.store.payload(snap)
            return self.store.export_parts(snap), {
                "design_id": snap.id, "revision": snap.revision, "design_checks": checks,
                "structure": {"groups": payload["groups"], "links": payload["links"]}}
        raw = p.get("params") or {}
        if isinstance(raw, dict) and raw.get("style") == "partition-slotted":
            # One piece's parameters alone would skip its set's crossing, interlock and fit checks.
            raise RpcError(INVALID_PARAMS, "a partition piece is exported with its set: export the design the "
                                           "engine holds (session_id, design_id and revision), not one piece's "
                                           "parameters")
        params = designs.parse_params(raw)
        return [exports.ExportPart("p1", params.style, params)], {}

    def _export_args(self, p: dict[str, Any]) -> tuple[str | None, list[str]]:
        filename = p.get("filename")
        if filename is not None and not isinstance(filename, str):
            raise RpcError(INVALID_PARAMS, "filename must be a string")
        formats = p.get("formats") or ["cf2"]
        if not isinstance(formats, list) or not all(isinstance(f, str) for f in formats):
            raise RpcError(INVALID_PARAMS, "formats must be a list of 'cf2' and/or 'dxf'")
        return filename, formats

    def _export(self, p: dict[str, Any]) -> dict[str, Any]:
        parts, about = self._export_target(p)
        filename, formats = self._export_args(p)
        flags = {}
        for k in ("overwrite", "confirm_pickup"):
            flags[k] = p.get(k, False)
            if not isinstance(flags[k], bool):
                raise RpcError(INVALID_PARAMS, f"{k} must be true or false")
        flags["break_lock"] = _lock_id(p)
        cfg = self.settings.settings
        return exports.export_design(parts, cfg.resolved_output_dir(), filename, formats, flags["overwrite"],
                                     cfg.resolved_dxf_dir(), **about, break_lock=flags["break_lock"],
                                     confirm_pickup=flags["confirm_pickup"],  # its reply offers the pickup
                                     dxf_expand_perfs=_flag(p, "dxf_expand_perfs"))

    def _export_plan(self, p: dict[str, Any]) -> dict[str, Any]:
        filename, formats = self._export_args(p)
        try:
            parts, _ = self._export_target(p)
        except designs.DesignCheckFailed as exc:
            return {"ready": False, "blocking": [str(exc)], "files": [], "job_path": None, "replaces": [],
                    "pickup": False, "formats": {f: {"ready": False, "blocking": [str(exc)]} for f in ("cf2", "dxf")}}
        cfg = self.settings.settings
        return exports.plan(parts, cfg.resolved_output_dir(), filename, formats, cfg.resolved_dxf_dir(),
                            _flag(p, "dxf_expand_perfs"))

    def _export_pickup(self, p: dict[str, Any]) -> dict[str, Any]:
        return exports.pickup(Path(_str(p, "job_path")), _str(p, "txn"), break_lock=_lock_id(p))

    def _export_recover(self, _: dict[str, Any]) -> dict[str, Any]:
        cfg = self.settings.settings
        return {"notes": exports.recover_all(cfg.resolved_output_dir(), cfg.resolved_dxf_dir())}

    def _job_open(self, p: dict[str, Any]) -> dict[str, Any]:
        mode = p.get("mode", "regenerate")
        if not isinstance(mode, str):
            raise RpcError(INVALID_PARAMS, "mode must be 'exported' or 'regenerate'")
        return exports.open_job(p.get("record"), mode)

    def _models(self, p: dict[str, Any]) -> dict[str, Any]:
        try:
            return {"models": models.list_models(bool(p.get("refresh")))}
        except Exception as exc:  # offline, or OpenRouter down: Settings shows its built-in list
            raise RuntimeError(f"couldn't load OpenRouter's model list ({type(exc).__name__})") from None

    def _settings_update(self, p: dict[str, Any]) -> dict[str, Any]:
        self.settings.update(p)
        return self.settings.public()

    def _chat_set_design(self, p: dict[str, Any]) -> dict[str, Any]:
        """Older name for design.replace."""
        self.agent.set_design(_str(p, "session_id"), BoxParams.model_validate(p.get("params") or {}))
        return {"ok": True}

    def _layout_choose(self, p: dict[str, Any]) -> dict[str, Any]:
        """The designer clicked a product-mode layout card: commit that candidate (refused if it expired)."""
        action, set_id = p.get("action"), p.get("set_id")
        for k, v in (("action", action), ("set_id", set_id)):
            if v is not None and not isinstance(v, str):
                raise RpcError(INVALID_PARAMS, f"{k} must be a string")
        return self.agent.choose_layout(_str(p, "session_id"), _str(p, "candidate_id"), action, set_id)

    def _approve_import(self, p: dict[str, Any]) -> dict[str, Any]:
        """The designer approved an artwork import on its chat card (the agent can't)."""
        return self.agent.approve_import(_str(p, "session_id"), _str(p, "import_id"),
                                         _flag(p, "without_unsupported"))

    def _acknowledge(self, p: dict[str, Any]) -> dict[str, Any]:
        """The designer acknowledged suspicious artwork in the checks panel, on the design they see."""
        keys = p.get("keys")
        if not isinstance(keys, list) or not keys or not all(isinstance(k, str) for k in keys):
            raise RpcError(INVALID_PARAMS, "keys must be a list of acknowledgement keys")
        rev = p.get("revision")
        if not _is_int(rev):
            raise RpcError(INVALID_PARAMS, "revision must be the revision shown")
        return self.agent.acknowledge(_str(p, "session_id"), (_str(p, "design_id"), rev), _str(p, "part_id"), keys)

    def _chat_send(self, p: dict[str, Any]) -> dict[str, Any]:
        self.agent.start_turn(_str(p, "session_id"), _str(p, "text"))
        return {"accepted": True}


def _is_int(v: Any) -> bool:
    """A whole number, and not a bool (True == 1 in Python)."""
    return type(v) is int


def _flag(p: dict[str, Any], key: str) -> bool:
    v = p.get(key, False)
    if not isinstance(v, bool):
        raise RpcError(INVALID_PARAMS, f"{key} must be true or false")
    return v


def _lock_id(p: dict[str, Any]) -> str | None:
    """The one abandoned lock the designer agreed to break (its id from the error), or None."""
    v = p.get("break_lock")
    if v is None:
        return None
    if not isinstance(v, str) or not v:
        raise RpcError(INVALID_PARAMS, "break_lock must be the id of the lock to break")
    return v


def _str(p: dict[str, Any], key: str) -> str:
    v = p.get(key)
    if not isinstance(v, str) or not v:
        raise RpcError(INVALID_PARAMS, f"{key} must be a non-empty string")
    return v


def _validation_message(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err.get("loc", ()))
        parts.append(f"{loc}: {err.get('msg')}" if loc else str(err.get("msg")))
    return "; ".join(parts)


def main() -> None:
    # Logs may contain non-ASCII paths; the Windows console code page can't encode them.
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    # Binary-safe UTF-8 on Windows pipes regardless of the console code page.
    stdin = open(sys.stdin.fileno(), "r", encoding="utf-8", newline="\n", closefd=False)
    stdout = open(sys.stdout.fileno(), "w", encoding="utf-8", newline="\n", closefd=False)
    sys.stdout = sys.stderr  # stray print() calls must never corrupt the protocol stream
    Server(stdout).serve(stdin)

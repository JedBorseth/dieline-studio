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
from typing import Any, Callable, TextIO

from pydantic import ValidationError

from . import __version__, designs, models
from .agent import Agent
from .designs import BoxParams
from .settings import SettingsStore

log = logging.getLogger(__name__)

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
APP_ERROR = -32000
EXPORT_EXISTS = -32001  # the UI offers "Replace" for this one


class RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class Server:
    def __init__(self, out: TextIO, settings: SettingsStore | None = None) -> None:
        self._out = out
        self._write_lock = threading.Lock()
        self.settings = settings or SettingsStore()
        self.agent = Agent(self.settings, self.emit)
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="rpc")
        self.methods: dict[str, Callable[[dict[str, Any]], Any]] = {
            "engine.info": self._info,
            "box.create": lambda p: designs.create_box(BoxParams.model_validate(p)),
            "box.export": self._export,
            "job.open": lambda p: designs.open_job(p.get("record")),
            "settings.get": lambda p: self.settings.public(),
            "models.list": self._models,
            "settings.update": self._settings_update,
            "chat.send": self._chat_send,
            "chat.set_design": self._chat_set_design,
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
        except designs.ExportExists as exc:
            reply = {"jsonrpc": "2.0", "id": req_id,
                     "error": {"code": EXPORT_EXISTS, "message": str(exc), "data": {"files": exc.names}}}
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

    def _export(self, p: dict[str, Any]) -> dict[str, Any]:
        params = BoxParams.model_validate(p.get("params") or {})
        filename = p.get("filename")
        if filename is not None and not isinstance(filename, str):
            raise RpcError(INVALID_PARAMS, "filename must be a string")
        overwrite = p.get("overwrite", False)
        if not isinstance(overwrite, bool):
            raise RpcError(INVALID_PARAMS, "overwrite must be true or false")
        formats = p.get("formats") or ["cf2"]
        if not isinstance(formats, list) or not all(isinstance(f, str) for f in formats):
            raise RpcError(INVALID_PARAMS, "formats must be a list of 'cf2' and/or 'dxf'")
        cfg = self.settings.settings
        return designs.export(params, cfg.resolved_output_dir(), filename, formats, overwrite, cfg.resolved_dxf_dir())

    def _models(self, p: dict[str, Any]) -> dict[str, Any]:
        try:
            return {"models": models.list_models(bool(p.get("refresh")))}
        except Exception as exc:  # offline, or OpenRouter down: Settings shows its built-in list
            raise RuntimeError(f"couldn't load OpenRouter's model list ({type(exc).__name__})") from None

    def _settings_update(self, p: dict[str, Any]) -> dict[str, Any]:
        self.settings.update(p)
        return self.settings.public()

    def _chat_set_design(self, p: dict[str, Any]) -> dict[str, Any]:
        self.agent.set_design(_str(p, "session_id"), BoxParams.model_validate(p.get("params") or {}))
        return {"ok": True}

    def _chat_send(self, p: dict[str, Any]) -> dict[str, Any]:
        self.agent.start_turn(_str(p, "session_id"), _str(p, "text"))
        return {"accepted": True}


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

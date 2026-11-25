"""User settings. The API key lives in the OS credential store (Windows
Credential Manager via `keyring`), never in the settings file."""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

import platformdirs

log = logging.getLogger(__name__)

APP_NAME = "Dieline Studio"
KEYRING_SERVICE = "dieline-studio"
KEYRING_USER = "anthropic-api-key"
API_KEY_PREFIX = "sk-ant-api"

MODELS = [
    {"id": "claude-opus-5-5", "label": "Claude Opus 5.5"},
    {"id": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5"},
    {"id": "claude-haiku-5-5", "label": "Claude Haiku 5.5"},
]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]


def default_output_dir() -> Path:
    env = os.environ.get("DIELINE_OUTPUT_DIR")
    if env:
        return Path(env)
    return Path(platformdirs.user_documents_dir()) / APP_NAME / "exports"


@dataclass
class Settings:
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    output_dir: str = ""

    def resolved_output_dir(self) -> Path:
        return Path(self.output_dir) if self.output_dir else default_output_dir()


class SettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path(platformdirs.user_config_dir(APP_NAME, appauthor=False)) / "settings.json"
        self._lock = threading.Lock()
        self.settings = self._load()

    def _load(self) -> Settings:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            known = {k: v for k, v in data.items() if k in Settings.__dataclass_fields__}
            return Settings(**known)
        except FileNotFoundError:
            return Settings()
        except (ValueError, TypeError) as exc:
            log.warning("ignoring unreadable settings file %s: %s", self.path, exc)
            return Settings()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self.settings), indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # API key -----------------------------------------------------------------

    def api_key(self) -> str | None:
        """The stored key, else ANTHROPIC_API_KEY from the environment (dev use)."""
        try:
            import keyring

            stored = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
            if stored:
                return stored
        except Exception as exc:  # no backend (headless Linux) or a locked store
            log.debug("keyring unavailable: %s", type(exc).__name__)
        return os.environ.get("ANTHROPIC_API_KEY") or None

    def set_api_key(self, key: str | None) -> None:
        """Store the key in the OS credential store, or clear it with None/''."""
        key = (key or "").strip() or None
        if key is not None and not key.startswith(API_KEY_PREFIX):
            # The shipped app may only use API keys: no OAuth, subscription or admin tokens.
            raise ValueError("That isn't an Anthropic API key. API keys start with sk-ant-api.")
        try:
            import keyring

            if key:
                keyring.set_password(KEYRING_SERVICE, KEYRING_USER, key)
            else:
                try:
                    keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
                except keyring.errors.PasswordDeleteError:
                    pass
        except ValueError:
            raise
        except Exception as exc:
            # Never include the key in the message; the exception text could echo it.
            raise RuntimeError(
                f"Couldn't save to the system credential store ({type(exc).__name__}). The key was not stored."
            ) from None

    # Settings ----------------------------------------------------------------

    def update(self, changes: dict) -> None:
        if "api_key" in changes:
            self.set_api_key(changes["api_key"])
        with self._lock:
            if "model" in changes:
                model = str(changes["model"])
                if model not in {m["id"] for m in MODELS}:
                    raise ValueError(f"unknown model {model!r}")
                self.settings.model = model
            if "effort" in changes:
                effort = str(changes["effort"])
                if effort not in EFFORTS:
                    raise ValueError(f"unknown effort {effort!r}")
                self.settings.effort = effort
            if "output_dir" in changes:
                self.settings.output_dir = str(changes["output_dir"] or "")
            self._save()

    def public(self) -> dict:
        return {
            **asdict(self.settings),
            "output_dir_resolved": str(self.settings.resolved_output_dir()),
            "has_api_key": bool(self.api_key()),
            "models": MODELS,
            "efforts": EFFORTS,
        }

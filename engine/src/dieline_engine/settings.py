"""User settings. API keys (OpenRouter, optional search providers) live in the
OS credential store (Windows Credential Manager via `keyring`), never in the
settings file."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

import platformdirs

log = logging.getLogger(__name__)

APP_NAME = "Dieline Studio"
KEYRING_SERVICE = "dieline-studio"
KEYRING_USER = "openrouter-api-key"
API_KEY = re.compile(r"sk-or-[A-Za-z0-9_-]+")
SEARCH_KEY_USERS = {"exa": "exa-api-key", "tavily": "tavily-api-key"}

# Suggestions shown in Settings; any OpenRouter model id that supports tools and
# image input works (the live list comes from OpenRouter, see models.py).
MODELS = [
    {"id": "anthropic/claude-opus-5.5", "label": "Claude Opus 5.5"},
    {"id": "anthropic/claude-sonnet-5.5", "label": "Claude Sonnet 5.5"},
    {"id": "openai/gpt-6.2-sol", "label": "GPT-6.2 Sol"},
    {"id": "google/gemini-4-neon", "label": "Gemini 4 Neon"},
]
MODEL_ID = re.compile(r"~?[A-Za-z0-9._-]+/[A-Za-z0-9._:-]+")
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
SEARCH_PROVIDERS = ["exa", "tavily", "off"]


def default_output_dir() -> Path:
    env = os.environ.get("DIELINE_OUTPUT_DIR")
    if env:
        return Path(env)
    return Path(platformdirs.user_documents_dir()) / APP_NAME / "exports"


@dataclass
class Settings:
    model: str = "anthropic/claude-opus-5.5"
    effort: str = "medium"
    output_dir: str = ""
    # Where DXF exports go, e.g. the designer's sample-table folder
    # (\\server\Sample_Files\<name>). Empty: next to the CF2.
    dxf_dir: str = ""
    # Web search provider for the chat's web_search tool (client-side, as in
    # OpenCode): "exa", "tavily" or "off". Both work keyless (rate-limited) or
    # with the user's own key. "off" also removes web_fetch.
    search_provider: str = "exa"
    # Folders the chat may search and read (read-only). Empty: the OneDrive
    # folders synced on this PC.
    reference_dirs: list[str] = field(default_factory=list)

    def resolved_output_dir(self) -> Path:
        return Path(self.output_dir) if self.output_dir else default_output_dir()

    def resolved_dxf_dir(self) -> Path | None:
        return Path(self.dxf_dir) if self.dxf_dir else None

    def resolved_reference_dirs(self) -> list[Path]:
        from .files import default_roots

        return [Path(p) for p in self.reference_dirs] if self.reference_dirs else default_roots()


class SettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path(platformdirs.user_config_dir(APP_NAME, appauthor=False)) / "settings.json"
        self._lock = threading.Lock()
        self.settings = self._load()

    def _load(self) -> Settings:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("expected a settings object")
            known = {k: v for k, v in data.items() if k in Settings.__dataclass_fields__}
            # Settings from the Anthropic-direct versions: their model ids have no
            # provider prefix, and web search was a plain on/off switch.
            if "model" in known and not MODEL_ID.fullmatch(str(known["model"])):
                known.pop("model")
            if data.get("web_search") is False and "search_provider" not in data:
                known["search_provider"] = "off"
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

    def _secret(self, user: str) -> str | None:
        try:
            import keyring

            return keyring.get_password(KEYRING_SERVICE, user) or None
        except Exception as exc:  # no backend (headless Linux) or a locked store
            log.debug("keyring unavailable: %s", type(exc).__name__)
            return None

    def api_key(self) -> str | None:
        """The stored OpenRouter key, else OPENROUTER_API_KEY from the environment (dev use)."""
        key = (self._secret(KEYRING_USER) or os.environ.get("OPENROUTER_API_KEY") or "").strip()
        return key if API_KEY.fullmatch(key) else None

    def search_key(self, provider: str) -> str | None:
        """The user's own key for a search provider, if any (else keyless)."""
        user = SEARCH_KEY_USERS.get(provider)
        if user is None:
            return None
        return self._secret(user) or os.environ.get(f"{provider.upper()}_API_KEY") or None

    def _store_secret(self, user: str, value: str | None) -> None:
        try:
            import keyring

            if value:
                keyring.set_password(KEYRING_SERVICE, user, value)
            else:
                try:
                    keyring.delete_password(KEYRING_SERVICE, user)
                except keyring.errors.PasswordDeleteError:
                    pass
        except Exception as exc:
            # Never include the key in the message; the exception text could echo it.
            raise RuntimeError(
                f"Couldn't save to the system credential store ({type(exc).__name__}). The key was not stored."
            ) from None

    @staticmethod
    def _validate_key(field: str, key: object) -> str | None:
        if key is not None and not isinstance(key, str):
            raise ValueError(f"{field} must be a string")
        key = (key or "").strip() or None
        if key is None:
            return None
        if field == "api_key":
            if not API_KEY.fullmatch(key):
                raise ValueError("That isn't an OpenRouter API key. OpenRouter keys start with sk-or-.")
        elif len(key) > 200 or not re.fullmatch(r"[A-Za-z0-9_.-]+", key):
            raise ValueError("That doesn't look like an API key.")
        return key

    # Settings ----------------------------------------------------------------

    def update(self, changes: dict) -> None:
        """Validate every field first, then apply all of them, so a bad value changes nothing."""
        new = replace(self.settings)
        if "model" in changes:
            model = str(changes["model"]).strip()
            if not MODEL_ID.fullmatch(model):
                raise ValueError(f"{model!r} isn't an OpenRouter model id (like anthropic/claude-opus-5.5)")
            new.model = model
        if "effort" in changes:
            effort = str(changes["effort"])
            if effort not in EFFORTS:
                raise ValueError(f"unknown effort {effort!r}")
            new.effort = effort
        if "output_dir" in changes:
            out = str(changes["output_dir"] or "").strip()
            if out and not Path(out).is_absolute():
                raise ValueError("the export folder must be a full path, e.g. C:\\Users\\me\\Documents\\Dielines")
            new.output_dir = out
        if "dxf_dir" in changes:
            out = str(changes["dxf_dir"] or "").strip()
            if out and not Path(out).is_absolute():
                raise ValueError("the DXF folder must be a full path, e.g. \\\\server\\Sample_Files\\Josh")
            new.dxf_dir = out
        if "search_provider" in changes:
            if changes["search_provider"] not in SEARCH_PROVIDERS:
                raise ValueError(f"search_provider must be one of {', '.join(SEARCH_PROVIDERS)}")
            new.search_provider = changes["search_provider"]
        credentials = {"api_key": KEYRING_USER, **{f"{p}_api_key": user for p, user in SEARCH_KEY_USERS.items()}}
        secrets = {
            user: self._validate_key(key, changes[key])
            for key, user in credentials.items()
            if key in changes
        }
        if "reference_dirs" in changes:
            dirs = changes["reference_dirs"] or []
            if not isinstance(dirs, list) or not all(isinstance(d, str) for d in dirs):
                raise ValueError("reference_dirs must be a list of folder paths")
            dirs = [d.strip() for d in dirs if d.strip()]
            for d in dirs:
                if not Path(d).is_absolute():
                    raise ValueError(f"reference folders must be full paths: {d}")
            new.reference_dirs = dirs
        with self._lock:
            for user, key in secrets.items():
                self._store_secret(user, key)
            self.settings = new
            self._save()

    def public(self) -> dict:
        return {
            **asdict(self.settings),
            "output_dir_resolved": str(self.settings.resolved_output_dir()),
            "reference_dirs_resolved": [str(p) for p in self.settings.resolved_reference_dirs()],
            "has_api_key": bool(self.api_key()),
            "has_search_key": {p: bool(self.search_key(p)) for p in SEARCH_KEY_USERS},
            "search_providers": SEARCH_PROVIDERS,
            "models": MODELS,
            "efforts": EFFORTS,
        }

"""The live list of OpenRouter models the chat can use (tools and image input),
for the Settings picker. Fetched from OpenRouter's public model list and cached
for the session; Settings falls back to a short built-in list offline."""

from __future__ import annotations

import threading
import time

import httpx

URL = "https://openrouter.ai/api/v1/models"
TTL = 3600.0

_lock = threading.Lock()
_cache: tuple[float, list[dict]] | None = None


def usable(models: list[dict]) -> list[dict]:
    out = []
    for m in models:
        params = m.get("supported_parameters") or []
        inputs = (m.get("architecture") or {}).get("input_modalities") or []
        mid = m.get("id", "")
        if "tools" not in params or "image" not in inputs or mid.endswith(":batch"):
            continue
        pricing = m.get("pricing") or {}
        out.append({
            "id": mid,
            "label": m.get("name") or mid,
            "context": m.get("context_length"),
            "input_per_mtok": _per_mtok(pricing.get("prompt")),
            "output_per_mtok": _per_mtok(pricing.get("completion")),
            "reasoning": "reasoning" in params,
            "efforts": (m.get("reasoning") or {}).get("supported_efforts") or [],
            "max_completion_tokens": (m.get("top_provider") or {}).get("max_completion_tokens"),
        })
    return sorted(out, key=lambda m: m["id"])


def _per_mtok(v: object) -> float | None:
    try:
        return round(float(v) * 1_000_000, 4)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def list_models(refresh: bool = False) -> list[dict]:
    global _cache
    with _lock:
        if _cache and not refresh and time.monotonic() - _cache[0] < TTL:
            return _cache[1]
    r = httpx.get(URL, timeout=20.0, headers={"User-Agent": "DielineStudio/0.1"})
    r.raise_for_status()
    models = usable(r.json().get("data") or [])
    with _lock:
        _cache = (time.monotonic(), models)
    return models

EFFORT_ORDER = ("minimal", "low", "medium", "high", "xhigh", "max")


def cached(model_id: str) -> dict | None:
    """Return cached capabilities without delaying a chat with a network call."""
    with _lock:
        entries = _cache[1] if _cache else []
    return next((entry for entry in entries if entry["id"] == model_id), None)


def effort_for(info: dict | None, wanted: str) -> str:
    supported = (info or {}).get("efforts") or []
    known = [effort for effort in EFFORT_ORDER if effort in supported]
    if not known or wanted in supported:
        return wanted
    lower = [effort for effort in known if EFFORT_ORDER.index(effort) <= EFFORT_ORDER.index(wanted)]
    return lower[-1] if lower else known[0]

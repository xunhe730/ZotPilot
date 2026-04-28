"""Runtime configuration resolution for ZotPilot."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path

from .config import Config, _default_config_dir, _old_config_path
from .secret_store import describe_backend, get_secret

SECRET_FIELDS: tuple[str, ...] = (
    "gemini_api_key",
    "dashscope_api_key",
    "anthropic_api_key",
    "zotero_api_key",
    "semantic_scholar_api_key",
)

ENV_TO_FIELD: dict[str, str] = {
    "GEMINI_API_KEY": "gemini_api_key",
    "DASHSCOPE_API_KEY": "dashscope_api_key",
    "ANTHROPIC_API_KEY": "anthropic_api_key",
    "ZOTERO_API_KEY": "zotero_api_key",
    "ZOTERO_USER_ID": "zotero_user_id",
    "OPENALEX_EMAIL": "openalex_email",
    "S2_API_KEY": "semantic_scholar_api_key",
}


@dataclass(frozen=True)
class ResolvedRuntimeSettings:
    config: Config
    sources: dict[str, str]
    secret_backend: str
    legacy_sources: dict[str, str]
    runtime_config_path: Path


def _resolved_config_path(path: Path | str | None = None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    config_path = _default_config_dir() / "config.json"
    if config_path.exists():
        return config_path
    old_path = _old_config_path()
    if old_path.exists():
        return old_path
    return config_path


def _read_json_if_exists(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _collect_legacy_config_secrets(path: Path | str | None = None) -> dict[str, str]:
    config_path = _resolved_config_path(path)
    candidates: list[Path] = [config_path]
    old_path = _old_config_path()
    if old_path not in candidates:
        candidates.append(old_path)

    found: dict[str, str] = {}
    for candidate in candidates:
        data = _read_json_if_exists(candidate)
        for field in SECRET_FIELDS:
            value = data.get(field)
            if value and field not in found:
                found[field] = str(value)
    return found


def resolve_runtime_settings(
    path: Path | str | None = None,
    *,
    overrides: dict[str, str | None] | None = None,
) -> ResolvedRuntimeSettings:
    base = Config.load(path)
    updates: dict[str, object] = {}
    sources: dict[str, str] = {}
    backend = describe_backend()
    legacy_sources = _collect_legacy_config_secrets(path)

    for field in SECRET_FIELDS:
        config_value = getattr(base, field, None)
        if config_value:
            sources[field] = "config"
            continue
        secret_value = get_secret(field)
        if secret_value:
            updates[field] = secret_value
            sources[field] = f"legacy-{backend.name}"

    for env_key, field in ENV_TO_FIELD.items():
        value = os.environ.get(env_key)
        if value:
            updates[field] = value
            sources[field] = "env-override"

    for field, value in (overrides or {}).items():
        if value is not None:
            updates[field] = value
            sources[field] = "cli-override"

    resolved = replace(base, **updates)

    # If values still came from the shared config, record that source explicitly.
    for field in ("zotero_user_id", "openalex_email"):
        value = getattr(resolved, field, None)
        if value and field not in sources:
            sources[field] = "config"

    return ResolvedRuntimeSettings(
        config=resolved,
        sources=sources,
        secret_backend=backend.name,
        legacy_sources=legacy_sources,
        runtime_config_path=_resolved_config_path(path),
    )


def resolve_runtime_config(
    path: Path | str | None = None,
    *,
    overrides: dict[str, str | None] | None = None,
) -> Config:
    return resolve_runtime_settings(path, overrides=overrides).config

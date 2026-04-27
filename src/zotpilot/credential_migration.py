"""Legacy secret migration helpers."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import _platforms
from .config import Config, _default_config_dir, _old_config_path
from .runtime_settings import SECRET_FIELDS
from .secret_store import get_secret, has_secret, set_secret

_ENV_TO_SECRET_FIELD = {
    "GEMINI_API_KEY": "gemini_api_key",
    "DASHSCOPE_API_KEY": "dashscope_api_key",
    "ANTHROPIC_API_KEY": "anthropic_api_key",
    "ZOTERO_API_KEY": "zotero_api_key",
    "S2_API_KEY": "semantic_scholar_api_key",
}


@dataclass(frozen=True)
class MigrationResult:
    imported: dict[str, str] = field(default_factory=dict)
    preserved: list[str] = field(default_factory=list)
    config_updated: bool = False
    re_registered_platforms: list[str] = field(default_factory=list)
    backups: list[str] = field(default_factory=list)


def _config_path(path: Path | str | None = None) -> Path:
    return Path(path).expanduser() if path is not None else (_default_config_dir() / "config.json")


def _read_json_if_exists(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _legacy_config_candidates(config_path: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for path in (config_path, _old_config_path()):
        data = _read_json_if_exists(path)
        for config_field in SECRET_FIELDS + ("zotero_user_id",):
            value = data.get(config_field)
            if value and config_field not in found:
                found[config_field] = str(value)
    return found


def _client_candidates() -> tuple[dict[str, str], list[str]]:
    found: dict[str, str] = {}
    touched_platforms: list[str] = []
    for plat in _platforms.SUPPORTED_PLATFORM_NAMES:
        registered, _command, _args, env, config_path = _platforms._inspect_registration(plat)  # noqa: SLF001
        if not registered or not env:
            continue
        touched_platforms.append(config_path or plat)
        for env_key, config_field in _ENV_TO_SECRET_FIELD.items():
            value = env.get(env_key)
            if value and config_field not in found:
                found[config_field] = str(value)
        user_id = env.get("ZOTERO_USER_ID")
        if user_id and "zotero_user_id" not in found:
            found["zotero_user_id"] = str(user_id)
    return found, touched_platforms


def migrate_secrets(
    *,
    config_path: Path | str | None = None,
    force: bool = False,
    to_config: bool = True,
) -> MigrationResult:
    target_path = _config_path(config_path)
    config = Config.load(target_path)
    config_candidates = _legacy_config_candidates(target_path)
    client_candidates, touched_platforms = _client_candidates()

    imported: dict[str, str] = {}
    preserved: list[str] = []

    if to_config:
        data_updated = False
        for env_key, config_field in _ENV_TO_SECRET_FIELD.items():
            candidate = (
                get_secret(config_field)
                or client_candidates.get(config_field)
                or config_candidates.get(config_field)
            )
            if not candidate:
                continue
            if getattr(config, config_field, None) and not force:
                preserved.append(config_field)
                continue
            setattr(config, config_field, candidate)
            if get_secret(config_field):
                source = "legacy-secret-backend"
            elif config_field in client_candidates:
                source = "client-config"
            else:
                source = "config-file"
            imported[config_field] = source
            data_updated = True
        if data_updated:
            config.save(target_path)
    else:
        for env_key, config_field in _ENV_TO_SECRET_FIELD.items():
            candidate = client_candidates.get(config_field) or config_candidates.get(config_field)
            if not candidate:
                continue
            if has_secret(config_field) and not force:
                preserved.append(config_field)
                continue
            set_secret(config_field, candidate)
            imported[config_field] = (
                "client-config" if config_field in client_candidates else "config-file"
            )

    config_updated = False
    user_id_candidate = (
        client_candidates.get("zotero_user_id")
        or config_candidates.get("zotero_user_id")
    )
    if user_id_candidate and (force or not config.zotero_user_id):
        config.zotero_user_id = user_id_candidate
        config.save(target_path)
        config_updated = True

    re_registered_platforms: list[str] = []
    backups: list[str] = list(touched_platforms)
    if (imported and not to_config) or config_updated or touched_platforms:
        result = _platforms.reconcile_runtime(apply=True)
        re_registered_platforms = list(result.applied.registered if result.applied else ())

    return MigrationResult(
        imported=imported,
        preserved=preserved,
        config_updated=config_updated,
        re_registered_platforms=re_registered_platforms,
        backups=backups,
    )

"""Secret storage backends for ZotPilot."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import _default_config_dir

_SECRET_BACKEND_ENV = "ZOTPILOT_SECRET_BACKEND"
_LOCAL_SECRETS_PATH_ENV = "ZOTPILOT_LOCAL_SECRETS_PATH"
_DEFAULT_LOCAL_SECRETS = "secrets.json"
_SERVICE_PREFIX = "zotpilot"


@dataclass(frozen=True)
class SecretBackendInfo:
    name: str
    available: bool
    path: Path | None = None
    detail: str | None = None


class SecretStoreError(RuntimeError):
    """Raised when secure secret storage is unavailable or fails."""


def _backend_marker_path() -> Path:
    return _default_config_dir() / ".secret-backend"


def _local_secrets_path() -> Path:
    override = os.environ.get(_LOCAL_SECRETS_PATH_ENV)
    if override:
        return Path(override).expanduser()
    return _default_config_dir() / _DEFAULT_LOCAL_SECRETS


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp", prefix="zotpilot_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        if sys.platform != "win32":
            os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        tmp_path = None
    except OSError as exc:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise SecretStoreError(f"Failed to write secret store metadata to {path}: {exc}") from exc


def _select_backend_name() -> str:
    explicit = os.environ.get(_SECRET_BACKEND_ENV)
    if explicit in {"keychain", "local-file"}:
        return explicit

    marker = _backend_marker_path()
    if marker.exists():
        try:
            value = marker.read_text(encoding="utf-8").strip()
            if value in {"keychain", "local-file"}:
                return value
        except OSError:
            pass

    if sys.platform == "darwin" and shutil.which("security"):
        return "keychain"

    local_path = _local_secrets_path()
    if local_path.exists():
        return "local-file"

    return "unavailable"


def describe_backend() -> SecretBackendInfo:
    backend = _select_backend_name()
    if backend == "keychain":
        return SecretBackendInfo(
            name="keychain",
            available=bool(shutil.which("security")),
            detail="macOS Keychain",
        )
    if backend == "local-file":
        return SecretBackendInfo(
            name="local-file",
            available=True,
            path=_local_secrets_path(),
            detail="Local secrets file",
        )
    return SecretBackendInfo(
        name="unavailable",
        available=False,
        path=_local_secrets_path(),
        detail=(
            "No secure backend available. Set ZOTPILOT_SECRET_BACKEND=local-file "
            "to enable the local fallback explicitly."
        ),
    )


def enable_local_file_backend() -> Path:
    path = _local_secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        _write_text_atomic(path, "{}\n")
    if sys.platform != "win32":
        os.chmod(path, 0o600)
    _write_text_atomic(_backend_marker_path(), "local-file\n")
    return path


def _secret_service(secret_name: str) -> str:
    return f"{_SERVICE_PREFIX}/{secret_name}"


def _keychain_get(secret_name: str) -> str | None:
    service = _secret_service(secret_name)
    result = subprocess.run(
        ["security", "find-generic-password", "-a", _SERVICE_PREFIX, "-s", service, "-w"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _keychain_set(secret_name: str, value: str) -> None:
    service = _secret_service(secret_name)
    result = subprocess.run(
        ["security", "add-generic-password", "-U", "-a", _SERVICE_PREFIX, "-s", service, "-w", value],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SecretStoreError(result.stderr.strip() or f"Failed to store {secret_name} in Keychain")
    _write_text_atomic(_backend_marker_path(), "keychain\n")


def _keychain_delete(secret_name: str) -> None:
    service = _secret_service(secret_name)
    subprocess.run(
        ["security", "delete-generic-password", "-a", _SERVICE_PREFIX, "-s", service],
        capture_output=True,
        text=True,
    )


def _load_local_secrets() -> dict[str, str]:
    path = _local_secrets_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SecretStoreError(f"Failed to read local secret store {path}: {exc}") from exc


def _save_local_secrets(data: dict[str, str]) -> None:
    path = enable_local_file_backend()
    _write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    if sys.platform != "win32":
        os.chmod(path, 0o600)


def get_secret(secret_name: str) -> str | None:
    backend = describe_backend()
    if not backend.available:
        return None
    if backend.name == "keychain":
        return _keychain_get(secret_name)
    return _load_local_secrets().get(secret_name)


def has_secret(secret_name: str) -> bool:
    return get_secret(secret_name) is not None


def set_secret(secret_name: str, value: str) -> str:
    backend = describe_backend()
    if not backend.available:
        enable_local_file_backend()
        backend = describe_backend()
    if not backend.available:
        raise SecretStoreError(backend.detail or "No secret backend available")
    if backend.name == "keychain":
        _keychain_set(secret_name, value)
        return "keychain"
    data = _load_local_secrets()
    data[secret_name] = value
    _save_local_secrets(data)
    return "local-file"


def delete_secret(secret_name: str) -> None:
    backend = describe_backend()
    if not backend.available:
        return
    if backend.name == "keychain":
        _keychain_delete(secret_name)
        return
    data = _load_local_secrets()
    if secret_name in data:
        del data[secret_name]
        _save_local_secrets(data)


def list_secret_keys() -> list[str]:
    backend = describe_backend()
    if not backend.available:
        return []
    if backend.name == "local-file":
        return sorted(_load_local_secrets().keys())
    # Keychain does not provide a cheap filtered list without exposing unrelated
    # entries, so callers should probe known keys individually.
    return []

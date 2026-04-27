"""Health-check diagnostics for ZotPilot environment."""
from __future__ import annotations

import sqlite3
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import _default_config_dir
from .runtime_settings import SECRET_FIELDS, resolve_runtime_settings
from .secret_store import describe_backend


@dataclass(frozen=True)
class CheckResult:
    """Result of a single health check."""

    name: str
    status: str  # "pass", "warn", "fail"
    message: str


def _check_python_version() -> CheckResult:
    """Check Python >= 3.10."""
    version = sys.version_info
    version_str = f"Python {version.major}.{version.minor}.{version.micro}"
    if (version.major, version.minor) >= (3, 10):
        return CheckResult("python_version", "pass", version_str)
    return CheckResult("python_version", "fail", f"{version_str} (requires >= 3.10)")


def _check_config_exists(config_path: Path) -> CheckResult:
    """Check that the config file exists."""
    if config_path.exists():
        return CheckResult("config_file", "pass", str(config_path))
    return CheckResult("config_file", "fail", f"Not found: {config_path}")


def _check_config_permissions(config_path: Path, config) -> CheckResult:
    has_config_secret = any(getattr(config, field, None) for field in SECRET_FIELDS)
    if not config_path.exists():
        return CheckResult("config_permissions", "warn", "config file missing")
    if sys.platform == "win32":
        if has_config_secret:
            return CheckResult(
                "config_permissions",
                "warn",
                "config.json contains API keys; Windows ACL hardening is not enforced by ZotPilot v1",
            )
        return CheckResult("config_permissions", "pass", "no API keys stored in config.json")
    mode = stat.S_IMODE(config_path.stat().st_mode)
    if mode == 0o600:
        return CheckResult("config_permissions", "pass", "0600")
    status = "fail" if has_config_secret else "warn"
    return CheckResult(
        "config_permissions",
        status,
        f"{oct(mode)}; expected 0o600{' because config.json contains API keys' if has_config_secret else ''}",
    )


def _check_zotero_data(config) -> CheckResult:
    """Check Zotero data directory and sqlite readability."""
    zotero_dir: Path = config.zotero_data_dir
    sqlite_path = zotero_dir / "zotero.sqlite"

    if not zotero_dir.exists():
        return CheckResult("zotero_data", "fail", f"Directory not found: {zotero_dir}")
    if not sqlite_path.exists():
        return CheckResult("zotero_data", "fail", f"zotero.sqlite not found in {zotero_dir}")

    try:
        uri = f"file:{sqlite_path}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True)
        conn.execute("SELECT count(*) FROM items LIMIT 1")
        conn.close()
    except Exception as exc:
        return CheckResult("zotero_data", "fail", f"Cannot read zotero.sqlite: {exc}")

    return CheckResult("zotero_data", "pass", str(zotero_dir))


def _check_embedding_api_key(config) -> CheckResult:
    """Check that the required embedding API key is set."""
    provider = config.embedding_provider

    if provider == "local":
        return CheckResult("embedding_api_key", "pass", "provider=local (no key needed)")

    if provider == "gemini":
        if config.gemini_api_key:
            return CheckResult("embedding_api_key", "pass", "GEMINI_API_KEY is set")
        return CheckResult("embedding_api_key", "fail", "GEMINI_API_KEY not set (required for provider=gemini)")

    if provider == "dashscope":
        if config.dashscope_api_key:
            return CheckResult("embedding_api_key", "pass", "DASHSCOPE_API_KEY is set")
        return CheckResult("embedding_api_key", "fail", "DASHSCOPE_API_KEY not set (required for provider=dashscope)")

    return CheckResult("embedding_api_key", "fail", f"Unknown provider: {provider}")


def _check_secret_backend(config=None, sources: dict[str, str] | None = None) -> CheckResult:
    backend = describe_backend()
    uses_legacy_backend = any(
        source.startswith("legacy-") for source in (sources or {}).values()
    )
    if backend.available:
        detail = backend.name if not backend.path else f"{backend.name} ({backend.path})"
        return CheckResult("legacy_secret_backend", "pass", detail)
    if not uses_legacy_backend:
        detail = backend.detail or "no legacy backend configured"
        return CheckResult("legacy_secret_backend", "pass", f"unused ({detail})")
    return CheckResult("legacy_secret_backend", "warn", backend.detail or "No legacy secret backend available")


def _check_chromadb_index(config) -> CheckResult:
    """Check ChromaDB index health."""
    try:
        from .embeddings import create_embedder
        from .index_authority import authoritative_indexed_doc_ids, current_library_pdf_doc_ids
        from .vector_store import VectorStore
        from .zotero_client import ZoteroClient

        embedder = create_embedder(config)
        store = VectorStore(config.chroma_db_path, embedder)
        zotero = ZoteroClient(config.zotero_data_dir)
        current_doc_ids = current_library_pdf_doc_ids(zotero)
        doc_ids = authoritative_indexed_doc_ids(store, current_doc_ids)
        total = store.count_chunks_for_doc_ids(doc_ids)
        doc_count = len(doc_ids)

        if doc_count > 0:
            avg = total / doc_count
            return CheckResult(
                "chromadb_index",
                "pass",
                f"{doc_count} documents, {total} chunks (avg {avg:.1f} chunks/doc)",
            )
        return CheckResult("chromadb_index", "warn", "Index is empty (run 'zotpilot index' to populate)")
    except Exception as exc:
        return CheckResult("chromadb_index", "fail", f"Cannot open index: {exc}")


def _check_zotero_web_api(config, sources: dict[str, str]) -> CheckResult:
    """Check Zotero Web API credentials presence and source."""
    api_key = config.zotero_api_key
    user_id = config.zotero_user_id

    if api_key and user_id:
        if not user_id.isdigit():
            return CheckResult(
                "zotero_web_api",
                "fail",
                f"ZOTERO_USER_ID must be a numeric ID (e.g. '1234567'), "
                f"not a username (got '{user_id}'). "
                f"Fix: zotpilot config set zotero_user_id <numeric_id>",
            )
        key_src = sources.get("zotero_api_key", "unset")
        id_src = sources.get("zotero_user_id", "config")
        return CheckResult(
            "zotero_web_api",
            "pass",
            f"ZOTERO_API_KEY [source: {key_src}] and ZOTERO_USER_ID [source: {id_src}] are set",
        )

    missing = []
    if not api_key:
        missing.append("ZOTERO_API_KEY")
    if not user_id:
        missing.append("ZOTERO_USER_ID")
    return CheckResult(
        "zotero_web_api",
        "warn",
        f"Missing: {', '.join(missing)}. This is optional until you need write operations "
        "(ingest/tag/collections/notes). Store `zotero_user_id` in shared config with "
        "`zotpilot config set zotero_user_id <numeric_id>`, store `zotero_api_key` with "
        "`zotpilot config set zotero_api_key <key>`, or run `zotpilot setup` to configure both.",
    )


def _check_write_connectivity(config) -> CheckResult:
    """Test Zotero Web API connectivity (slow — only with --full)."""
    api_key = config.zotero_api_key
    user_id = config.zotero_user_id

    if not api_key or not user_id:
        return CheckResult("write_connectivity", "fail", "Cannot test: missing ZOTERO_API_KEY or ZOTERO_USER_ID")

    try:
        from pyzotero import zotero

        zot = zotero.Zotero(user_id, config.zotero_library_type, api_key)
        # A lightweight call to verify credentials
        zot.key_info()
        return CheckResult("write_connectivity", "pass", "Zotero Web API connection successful")
    except Exception as exc:
        return CheckResult("write_connectivity", "fail", f"Zotero Web API error: {exc}")


def run_checks(config_path: str | None = None, full: bool = False) -> list[CheckResult]:
    """Run all health checks and return structured results.

    Args:
        config_path: Path to config file, or None for default.
        full: If True, include slow checks (API connectivity).

    Returns:
        List of CheckResult objects.
    """
    results: list[CheckResult] = []

    # 1. Python version
    results.append(_check_python_version())

    resolved = resolve_runtime_settings(config_path)
    # 2. Config file exists
    if config_path:
        resolved_config_path = Path(config_path).expanduser()
    else:
        candidate = _default_config_dir() / "config.json"
        resolved_config_path = candidate if candidate.exists() else resolved.runtime_config_path
    results.append(_check_config_exists(resolved_config_path))

    # Load config (needed for remaining checks)
    config = resolved.config
    results.append(_check_config_permissions(resolved_config_path, config))

    # 3. Zotero data directory + sqlite
    results.append(_check_zotero_data(config))

    # 4. Legacy secret backend
    results.append(_check_secret_backend(config, resolved.sources))

    # 5. Embedding API key
    results.append(_check_embedding_api_key(config))

    # 6. ChromaDB index
    results.append(_check_chromadb_index(config))

    # 7. Zotero Web API credentials
    results.append(_check_zotero_web_api(config, resolved.sources))

    # 8. Write connectivity (only with --full)
    if full:
        results.append(_check_write_connectivity(config))

    return results

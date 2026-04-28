"""CLI entry point for ZotPilot."""
import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from ._platforms import (
    _deployment_status,
    _detect_cli_installer,
    _get_current_version,
    _get_latest_pypi_version,
    _get_skill_dirs,  # noqa: F401 — re-exported for test patching compatibility
)
from .config import Config, _default_config_dir
from .credential_migration import migrate_secrets
from .runtime_settings import resolve_runtime_config, resolve_runtime_settings
from .secret_store import SecretStoreError, delete_secret

logger = logging.getLogger(__name__)


def _default_config_path() -> Path:
    """Return default config file path."""
    return _default_config_dir() / "config.json"


def _split_validate_errors(errors: list[str]) -> tuple[list[str], list[str]]:
    """Split config.validate() errors into (blocking_errors, api_key_warnings).

    API key errors are non-blocking warnings when keys may be configured in
    config.json or provided via environment overrides.
    """
    warnings = [e for e in errors if "_API_KEY not set" in e]
    blocking = [e for e in errors if e not in warnings]
    return blocking, warnings


def _import_register_secret_overrides(args, config_path: Path) -> bool:
    imported_any = False
    if getattr(args, "gemini_key", None):
        _config_set("gemini_api_key", args.gemini_key, config_path)
        imported_any = True
    if getattr(args, "dashscope_key", None):
        _config_set("dashscope_api_key", args.dashscope_key, config_path)
        imported_any = True
    if getattr(args, "zotero_api_key", None):
        _config_set("zotero_api_key", args.zotero_api_key, config_path)
        imported_any = True
    if getattr(args, "zotero_user_id", None):
        if not args.zotero_user_id.isdigit():
            print("  WARNING: zotero_user_id should be a numeric ID, not a username.", file=sys.stderr)
        _config_set("zotero_user_id", args.zotero_user_id, config_path)
        imported_any = True

    return imported_any


def cmd_setup(args):
    """Interactive or non-interactive setup wizard."""
    from ._platforms import register as register_runtime
    from .config import _default_config_dir, _default_data_dir, _old_config_path
    from .zotero_detector import detect_zotero_data_dir

    # Redirect misused API key flags (agents sometimes guess these exist)
    for flag, opt, config_key in [
        ("gemini_key", "--gemini-key", "gemini_api_key"),
        ("dashscope_key", "--dashscope-key", "dashscope_api_key"),
    ]:
        if getattr(args, flag, None):
            print(
                f"Note: {opt} is not a setup argument — use interactive setup or config set instead:\n"
                f"  zotpilot config set {config_key} <key>"
            )

    non_interactive = getattr(args, "non_interactive", False)

    # Step 1: Detect Zotero data directory
    if non_interactive:
        zotero_dir = getattr(args, "zotero_dir", None)
        if zotero_dir:
            zotero_path = Path(zotero_dir).expanduser()
        else:
            detected = detect_zotero_data_dir()
            if detected:
                zotero_path = detected
            else:
                print("ERROR: Cannot auto-detect Zotero data directory. Use --zotero-dir.", file=sys.stderr)
                return 1

        if not (zotero_path / "zotero.sqlite").exists():
            print(f"ERROR: zotero.sqlite not found at {zotero_path}", file=sys.stderr)
            return 1

        # Provider from flag
        embedding_provider = getattr(args, "provider", None) or "gemini"
        if embedding_provider not in ("gemini", "dashscope", "local"):
            print(f"ERROR: Invalid provider '{embedding_provider}'. Must be 'gemini', 'dashscope', or 'local'.", file=sys.stderr)  # noqa: E501
            return 1

    else:
        # Interactive mode (original behavior)
        print("ZotPilot Setup Wizard")
        print("=" * 40)

        print("\n[1/5] Detecting Zotero data directory...")
        detected = detect_zotero_data_dir()

        if detected:
            print(f"  Found: {detected}")
            response = input("  Use this path? [Y/n] ").strip().lower()
            if response in ("n", "no"):
                zotero_dir = input("  Enter Zotero data directory: ").strip()
            else:
                zotero_dir = str(detected)
        else:
            print("  Could not auto-detect Zotero data directory.")
            zotero_dir = input("  Enter Zotero data directory path: ").strip()

        zotero_path = Path(zotero_dir).expanduser()
        if not (zotero_path / "zotero.sqlite").exists():
            print(f"  WARNING: zotero.sqlite not found at {zotero_path}")
            if input("  Continue anyway? [y/N] ").strip().lower() not in ("y", "yes"):
                return 1

        # Choose embedding provider
        print("\n[2/5] Choose embedding provider:")
        print("  1. Gemini (recommended, requires API key)")
        print("  2. DashScope / Bailian (Alibaba Cloud, requires API key)")
        print("  3. Local (all-MiniLM-L6-v2, no API key needed)")
        choice = input("  Choice [1/2/3]: ").strip()
        if choice == "2":
            embedding_provider = "dashscope"
        elif choice == "3":
            embedding_provider = "local"
        else:
            embedding_provider = "gemini"

    # Step 3: Configure API key (interactive only)
    gemini_api_key = None
    dashscope_api_key = None
    zotero_api_key = None
    zotero_user_id = None
    if non_interactive:
        zotero_user_id = os.environ.get("ZOTERO_USER_ID") or None
        zotero_api_key = os.environ.get("ZOTERO_API_KEY") or None
    if embedding_provider == "gemini":
        import os as _os
        existing_key = _os.environ.get("GEMINI_API_KEY")
        if non_interactive:
            gemini_api_key = existing_key
            if not gemini_api_key:
                print("NOTE: GEMINI_API_KEY not set. Set it before running the MCP server.", file=sys.stderr)
        else:
            print("\n[3/5] Gemini API key:")
            if existing_key:
                print("  Found GEMINI_API_KEY in environment (***hidden)")
                if input("  Use this key? [Y/n] ").strip().lower() not in ("n", "no"):
                    gemini_api_key = existing_key
            if not gemini_api_key:
                gemini_api_key = input("  Enter Gemini API key: ").strip()
                if not gemini_api_key:
                    print("  WARNING: No API key provided. Set GEMINI_API_KEY env var later.")
    elif embedding_provider == "dashscope":
        import os as _os
        existing_key = _os.environ.get("DASHSCOPE_API_KEY")
        if non_interactive:
            dashscope_api_key = existing_key
            if not dashscope_api_key:
                print("NOTE: DASHSCOPE_API_KEY not set. Set it before running the MCP server.", file=sys.stderr)
        else:
            print("\n[3/5] DashScope API key:")
            if existing_key:
                print("  Found DASHSCOPE_API_KEY in environment (***hidden)")
                if input("  Use this key? [Y/n] ").strip().lower() not in ("n", "no"):
                    dashscope_api_key = existing_key
            else:
                print("  Get a key at https://bailian.console.aliyun.com/")
                print("  Set it as: export DASHSCOPE_API_KEY='your-key'")
            if not dashscope_api_key:
                dashscope_api_key = input("  Enter DashScope API key: ").strip()
                if not dashscope_api_key:
                    print("  WARNING: No API key provided. Set DASHSCOPE_API_KEY env var later.")
    elif not non_interactive:
        print("\n[3/5] Skipping API key (local embeddings selected)")

    if not non_interactive:
        print("\n[4/6] Zotero Web API (optional, needed for ingest/tags/collections/notes):")
        enable_write = input("  Configure Zotero write credentials now? [Y/n] ").strip().lower()
        if enable_write not in ("n", "no"):
            print("  Need your Zotero numeric User ID and a private key with library/write access.")
            existing_user_id = os.environ.get("ZOTERO_USER_ID")
            if existing_user_id:
                print(f"  Found ZOTERO_USER_ID in environment: {existing_user_id}")
                if input("  Use this User ID? [Y/n] ").strip().lower() not in ("n", "no"):
                    zotero_user_id = existing_user_id
            if not zotero_user_id:
                zotero_user_id = input("  Enter Zotero numeric User ID: ").strip() or None
                if zotero_user_id and not zotero_user_id.isdigit():
                    print("  WARNING: Zotero User ID should be numeric, not a username.")

            existing_zotero_key = os.environ.get("ZOTERO_API_KEY")
            if existing_zotero_key:
                print("  Found ZOTERO_API_KEY in environment (***hidden)")
                if input("  Use this key? [Y/n] ").strip().lower() not in ("n", "no"):
                    zotero_api_key = existing_zotero_key
            if not zotero_api_key:
                zotero_api_key = input("  Enter Zotero API key: ").strip() or None
                if not zotero_api_key:
                    print("  WARNING: No Zotero API key provided. Write operations will stay disabled.")

    # Step 5: Check for existing deep-zotero config
    chroma_db_path = _default_data_dir() / "chroma"

    if not non_interactive:
        print("\n[5/6] Checking for existing configuration...")
        old_config = _old_config_path()
        old_chroma = _default_data_dir().parent / "deep-zotero" / "chroma"

        if old_config.exists():
            print(f"  Found existing deep-zotero config: {old_config}")
            if input("  Migrate settings from deep-zotero? [Y/n] ").strip().lower() not in ("n", "no"):
                with open(old_config, encoding="utf-8") as f:
                    old_data = json.load(f)
                print(f"  Found {len(old_data)} settings in old config (not auto-migrated)")
                if old_chroma.exists():
                    print(f"  Found existing ChromaDB index: {old_chroma}")
                    if input("  Reuse existing index? [Y/n] ").strip().lower() not in ("n", "no"):
                        chroma_db_path = old_chroma

    # Step 6: Write config
    if not non_interactive:
        print("\n[6/6] Writing configuration...")

    config_path = _default_config_dir() / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    from dataclasses import replace

    # Load defaults, override setup-chosen fields, then save.
    from .config import Config as _Config

    base_config = _Config.load(config_path)
    config = replace(
        base_config,
        zotero_data_dir=zotero_path,
        chroma_db_path=chroma_db_path,
        embedding_provider=embedding_provider,
        gemini_api_key=gemini_api_key or base_config.gemini_api_key,
        dashscope_api_key=dashscope_api_key or base_config.dashscope_api_key,
        zotero_api_key=zotero_api_key or base_config.zotero_api_key,
        zotero_user_id=zotero_user_id or base_config.zotero_user_id,
    )
    config.save(config_path)

    results = register_runtime(platforms=None)
    registration_ok = (
        _report_registration_results(results, context="Client registrations updated")
        if results else False
    )

    if non_interactive:
        print(f"Config written to: {config_path}")
        if registration_ok:
            print("Restart your AI agent.")
        print(f"Shared config saved to {config_path}. API keys, when configured, are stored in this file.")
        # Tell user how to provide API keys
        if embedding_provider == "gemini":
            print("Set your API key via:")
            print("  export GEMINI_API_KEY='<your-key>'")
            print("  or: zotpilot config set gemini_api_key <key>")
        elif embedding_provider == "dashscope":
            print("Set your API key via:")
            print("  export DASHSCOPE_API_KEY='<your-key>'")
            print("  or: zotpilot config set dashscope_api_key <key>")
        print("For Zotero write operations:")
        print("  zotpilot config set zotero_user_id <numeric-id>")
        print("  zotpilot config set zotero_api_key <your-key>")
    else:
        print(f"  Config written to: {config_path}")

        import os as _os
        if gemini_api_key and not _os.environ.get("GEMINI_API_KEY"):
            masked = gemini_api_key[:4] + "..." + gemini_api_key[-4:] if len(gemini_api_key) > 8 else "****"
            print("\n  NOTE: GEMINI_API_KEY was stored in config.json.")
            print(f"    Masked value: {masked}")
        if dashscope_api_key and not _os.environ.get("DASHSCOPE_API_KEY"):
            masked = dashscope_api_key[:4] + "..." + dashscope_api_key[-4:] if len(dashscope_api_key) > 8 else "****"
            print("\n  NOTE: DASHSCOPE_API_KEY was stored in config.json.")
            print(f"    Masked value: {masked}")
        if zotero_api_key and not _os.environ.get("ZOTERO_API_KEY"):
            masked = zotero_api_key[:4] + "..." + zotero_api_key[-4:] if len(zotero_api_key) > 8 else "****"
            print("\n  NOTE: ZOTERO_API_KEY was stored in config.json.")
            print(f"    Masked value: {masked}")

        print("\n" + "=" * 40)
        print("Setup complete!" if registration_ok else "Setup partially completed.")
        print()
        if registration_ok:
            print("Detected client runtimes have been registered with the new ZotPilot MCP command.")
            print("Restart your AI agent to load the new MCP config and skills.")
        else:
            print("Client registration failed. Run `zotpilot doctor` for details.")
        print(f"Shared config lives in {config_path}. API keys, when configured, are stored in this file.")

    return 0 if registration_ok else 1


def cmd_index(args):
    """Index Zotero library."""
    from .indexer import Indexer

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    config = resolve_runtime_config(args.config)
    errors = config.validate()
    blocking_errors, api_warnings = _split_validate_errors(errors)
    if blocking_errors:
        for e in blocking_errors:
            print(f"Config error: {e}", file=sys.stderr)
        return 1
    for w in api_warnings:
        print(f"Warning: {w} (configure with `zotpilot setup` or `zotpilot config set ...`)", file=sys.stderr)

    if args.no_vision:
        from dataclasses import replace
        config = replace(config, vision_enabled=False)

    max_pages = args.max_pages if args.max_pages is not None else config.max_pages

    batch_size = args.batch_size if args.batch_size > 0 else None

    # CLI is human-in-the-loop, so bypass the index_library MCP gate. Surface a
    # one-line warning if there is an unresolved metadata-only decision so the
    # user knows why some items will be skipped.
    try:
        from .tools.ingestion import _batch_store
        found = _batch_store.find_unresolved_metadata_only(None)
        if found is not None:
            decision, _ = found
            print(
                f"Note: indexing {len(decision.item_keys)} metadata-only item(s) from "
                f"batch {decision.batch_id} — re-run ingest on VPN to attach PDFs.",
                file=sys.stderr,
            )
    except Exception:
        pass

    indexer = Indexer(config)
    result = indexer.index_all(
        force_reindex=args.force,
        limit=args.limit,
        item_key=args.item_key,
        title_pattern=args.title,
        max_pages=max_pages,
        batch_size=batch_size,
    )

    print("\nIndexing complete:")
    print(f"  Indexed:         {result['indexed']}")
    print(f"  Already indexed: {result['already_indexed']}")
    print(f"  Skipped (empty): {result['skipped']}")
    print(f"  Failed:          {result['failed']}")
    print(f"  Empty:           {result['empty']}")

    if result.get("quality_distribution"):
        dist = result["quality_distribution"]
        print(f"  Quality: A={dist.get('A',0)} B={dist.get('B',0)} "
              f"C={dist.get('C',0)} D={dist.get('D',0)} F={dist.get('F',0)}")

    if result.get("extraction_stats"):
        stats = result["extraction_stats"]
        print(f"  Pages: {stats.get('total_pages',0)} total, "
              f"{stats.get('text_pages',0)} text, "
              f"{stats.get('ocr_pages',0)} OCR, "
              f"{stats.get('empty_pages',0)} empty")

    failures = [r for r in result["results"] if r.status == "failed"]
    if failures:
        print("\nFailures:")
        for f in failures:
            print(f"  {f.item_key}: {f.reason}")

    if result.get("long_documents"):
        print(f"\nSkipped {result['skipped_long']} long documents (>{max_pages} pages):")
        for doc in result["long_documents"]:
            print(f"  {doc['item_key']}: {doc['title']} ({doc['pages']} pages)")
        print("\nTo index these, re-run with: zotpilot index --max-pages 0")

    return 1 if result["failed"] > 0 and result["indexed"] == 0 else 0


def cmd_status(args):
    """Show configuration and index stats."""
    from . import __version__
    output_json = getattr(args, "json", False)

    resolved = resolve_runtime_settings(args.config)
    config = resolved.config
    errors = config.validate()
    blocking_errors, api_warnings = _split_validate_errors(errors)
    deployment = _deployment_status(config)

    if output_json:
        result = {
            "version": __version__,
            "zotpilot_installed": True,
            "config_exists": resolved.runtime_config_path.exists(),
            "zotero_dir": str(config.zotero_data_dir),
            "zotero_dir_valid": config.zotero_data_dir.exists()
                and (config.zotero_data_dir / "zotero.sqlite").exists(),
            "embedding_provider": config.embedding_provider,
            "gemini_key_set": bool(config.gemini_api_key),
            "dashscope_key_set": bool(config.dashscope_api_key),
            "secret_backend": resolved.secret_backend,
            "legacy_secret_backend": resolved.secret_backend,
            "write_ops_ready": bool(config.zotero_api_key and config.zotero_user_id),
            "index_ready": False,
            "doc_count": 0,
            "chunk_count": 0,
            "errors": blocking_errors,
            "warnings": api_warnings,
            "detected_platforms": deployment["detected_platforms"],
            "registered_platforms": deployment["registered_platforms"],
            "unsupported_platforms": deployment["unsupported_platforms"],
            "registration": deployment["registration"],
            "skill_dirs": deployment["skill_dirs"],
            "drift_state": deployment["drift_state"],
            "restart_required": deployment["restart_required"],
            "legacy_embedded_secrets_detected": deployment.get("legacy_embedded_secrets_detected", False),
            "legacy_embedded_secret_platforms": deployment.get("legacy_embedded_secret_platforms", []),
            "credentials_source": resolved.sources,
            "runtime_config_path": str(resolved.runtime_config_path),
        }
        if deployment.get("deployment_warning"):
            result["warnings"].append(
                f"Deployment status unavailable: {deployment['deployment_warning']}"
            )
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
            result["doc_count"] = len(doc_ids)
            result["chunk_count"] = total
            result["index_ready"] = len(doc_ids) > 0
        except Exception as e:
            result["errors"].append(f"Index error: {e}")

        print(json.dumps(result, indent=2))
        return 1 if blocking_errors else 0

    # Human-readable output
    from . import __version__
    print("ZotPilot Status")
    print("=" * 40)
    print(f"  Version:            {__version__}")
    print(f"  Zotero data dir:    {config.zotero_data_dir}")
    print(f"  ChromaDB path:      {config.chroma_db_path}")
    print(f"  Embedding provider: {config.embedding_provider}")
    print(f"  Legacy secret backend: {resolved.secret_backend}")
    print(f"  Write ops ready:    {'yes' if (config.zotero_api_key and config.zotero_user_id) else 'no'}")
    print(f"  Embedding model:    {config.embedding_model}")
    print(f"  Embedding dims:     {config.embedding_dimensions}")
    print(f"  Reranking enabled:  {config.rerank_enabled}")
    print(f"  Vision enabled:     {config.vision_enabled}")
    print("\n  Client integration:")
    detected = deployment["detected_platforms"]
    print(f"    Detected:   {', '.join(detected) if detected else 'none'}")
    registered = deployment["registered_platforms"]
    print(f"    Registered: {', '.join(registered) if registered else 'none'}")
    unsupported = deployment["unsupported_platforms"]
    if unsupported:
        print(f"    Unsupported in v0.5: {', '.join(unsupported)}")
    print(f"    Drift:      {deployment['drift_state']}")
    print(f"    Restart:    {'yes' if deployment['restart_required'] else 'no'}")
    if deployment["skill_dirs"]:
        print("    Skill dirs:")
        for skill_dir in deployment["skill_dirs"]:
            flags = []
            if skill_dir["is_symlink"]:
                flags.append("symlink")
            if skill_dir["is_broken_symlink"]:
                flags.append("broken")
            if skill_dir["is_duplicate"]:
                flags.append("duplicate")
            suffix = f" ({', '.join(flags)})" if flags else ""
            print(f"      - {skill_dir['path']}{suffix}")
    if deployment.get("deployment_warning"):
        print(f"    Warning: {deployment['deployment_warning']}")

    if blocking_errors:
        print("\n  Config errors:")
        for e in blocking_errors:
            print(f"    ✗ {e}")
        return 1
    if api_warnings:
        print("\n  Warnings:")
        for w in api_warnings:
            print(f"    ⚠ {w} (configure with `zotpilot setup` or `zotpilot config set ...`)")

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
        print("\n  Index stats:")
        print(f"    Documents: {len(doc_ids)}")
        print(f"    Chunks:    {total}")
        if doc_ids:
            print(f"    Avg chunks/doc: {total / len(doc_ids):.1f}")
    except Exception as e:
        print(f"\n  Could not read index: {e}")

    return 0


def cmd_doctor(args):
    from .doctor import run_checks

    output_json = getattr(args, "json", False)
    full = getattr(args, "full", False)

    results = run_checks(config_path=args.config, full=full)

    # Check for runtime drift / embedded secrets
    try:
        config = resolve_runtime_config(args.config)
        deployment = _deployment_status(config)
        embedded = deployment.get("legacy_embedded_secrets_detected", False)
        embedded_platforms = deployment.get("legacy_embedded_secret_platforms", [])
    except Exception:
        embedded = False
        embedded_platforms = []

    if output_json:
        summary = {"pass": 0, "warn": 0, "fail": 0}
        for r in results:
            summary[r.status] += 1
        data = {
            "checks": [{"name": r.name, "status": r.status, "message": r.message} for r in results],
            "summary": summary,
            "legacy_embedded_secrets_detected": embedded,
            "legacy_embedded_secret_platforms": embedded_platforms,
        }
        print(json.dumps(data, indent=2))
    else:
        status_icons = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}
        print("ZotPilot Doctor")
        print("=" * 50)
        for r in results:
            icon = status_icons[r.status]
            print(f"  [{icon}] {r.name}: {r.message}")
        print()
        if embedded:
            print(f"  [FAIL] legacy_embedded_secrets: found embedded client secrets in {', '.join(embedded_platforms)}")
            print("    Run `zotpilot config migrate-secrets` and then restart affected clients.")
        counts = {"pass": 0, "warn": 0, "fail": 0}
        for r in results:
            counts[r.status] += 1
        print(f"  Summary: {counts['pass']} passed, {counts['warn']} warnings, {counts['fail']} failures")

    has_fail = any(r.status == "fail" for r in results)
    return 1 if (has_fail or embedded) else 0

def _mask_secret(v: str) -> str:
    return v[:4] + "****" if len(v) > 4 else "****"


_SENSITIVE_FIELDS = {
    "gemini_api_key", "dashscope_api_key", "anthropic_api_key",
    "zotero_api_key", "semantic_scholar_api_key",
}

_SENSITIVE_REGISTER_FLAGS = {
    "gemini_api_key": "--gemini-key",
    "dashscope_api_key": "--dashscope-key",
    "zotero_api_key": "--zotero-api-key",
}

_SCALAR_TYPES = {
    "chunk_size": int, "chunk_overlap": int, "embedding_timeout": float,
    "embedding_max_retries": int, "rerank_alpha": float, "rerank_enabled": bool,
    "oversample_multiplier": int, "oversample_topic_factor": int,
    "stats_sample_limit": int, "max_pages": int, "vision_enabled": bool,
    "embedding_dimensions": int, "preflight_enabled": bool,
}


def _coerce_value(key: str, value: str):
    """Coerce string value to appropriate type for config field."""
    if key in _SCALAR_TYPES:
        t = _SCALAR_TYPES[key]
        if t is bool:
            if value.lower() in ("true", "1", "yes"):
                return True
            if value.lower() in ("false", "0", "no"):
                return False
            raise ValueError(f"Expected true/false for {key}, got '{value}'")
        return t(value)
    # dict/list fields: try JSON parse
    if value.startswith("{") or value.startswith("["):
        return json.loads(value)
    return value


def _config_set(key: str, value: str, config_path: Path) -> None:
    """Set a config field using atomic write (tempfile + os.replace)."""
    # Load existing config
    data: dict = {}
    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)
    data[key] = _coerce_value(key, value)
    _write_config_data(config_path, data)


def _write_config_data(config_path: Path, data: dict) -> None:
    """Write raw config data using atomic write (tempfile + os.replace)."""
    # Atomic write: temp file + rename
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=config_path.parent, suffix=".tmp", prefix="zotpilot_"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        if sys.platform != "win32":
            os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, config_path)
        tmp_path = None
    except OSError as e:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise RuntimeError(f"Failed to write config to {config_path}: {e}") from e
_ENV_TO_CONFIG = {
    "GEMINI_API_KEY": "gemini_api_key",
    "DASHSCOPE_API_KEY": "dashscope_api_key",
    "ZOTERO_API_KEY": "zotero_api_key",
    "ZOTERO_USER_ID": "zotero_user_id",
    "OPENALEX_EMAIL": "openalex_email",
    "S2_API_KEY": "semantic_scholar_api_key",
}


def _read_raw_config(config_path: Path) -> dict:
    if not config_path.exists():
        return {}
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def _write_raw_config(config_path: Path, data: dict) -> None:
    # Deprecated: no-op after atomic writer migration. Kept for backward compatibility.
    pass


def _import_runtime_env_to_config(
    *,
    config_path: Path,
    platforms: list[str] | None = None,
) -> dict[str, str]:
    # Deprecated: no-op after atomic writer migration. Kept for backward compatibility.
    return {}


def cmd_config(args):
    """Manage ZotPilot configuration."""
    config_path = Path(getattr(args, "config", _default_config_path()) or _default_config_path()).expanduser()

    # Known fields from Config dataclass
    from .config import Config as _Cfg
    known_fields = set(_Cfg.__dataclass_fields__.keys())

    subcmd = args.config_subcmd

    if subcmd == "path":
        print(config_path)
        return 0

    if subcmd == "set":
        key, value = args.key, args.value
        if key not in known_fields:
            print(f"Error: unknown field '{key}'. Run 'zotpilot config list' to see valid fields.",
                  file=sys.stderr)
            return 1
        if key == "zotero_user_id" and not value.isdigit():
            print(f"Warning: zotero_user_id should be a numeric ID, not a username (got '{value}').\n"
                  f"Find your numeric ID at https://www.zotero.org/settings/keys")
        if key in _SENSITIVE_FIELDS:
            try:
                _config_set(key, value, config_path)
            except (ValueError, json.JSONDecodeError, RuntimeError) as e:
                print(f"Error: {e}", file=sys.stderr)
                return 1
            print(f"✓ Saved '{key}' to {config_path}")
            return 0
        try:
            _config_set(key, value, config_path)
            print(f"✓ Saved to {config_path}")
        except (ValueError, json.JSONDecodeError, RuntimeError) as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        return 0

    if subcmd == "get":
        key = args.key
        if key not in known_fields:
            print(f"Error: unknown field '{key}'.", file=sys.stderr)
            return 1
        resolved = resolve_runtime_settings(config_path)
        cfg = resolved.config
        val = getattr(cfg, key, None)
        if val is None:
            print(f"{key}: (not set)")
        elif key in _SENSITIVE_FIELDS:
            src = resolved.sources.get(key, "unset")
            print(f"{key}: {_mask_secret(str(val))} [{src}]")
        else:
            src = resolved.sources.get(key, "config")
            print(f"{key}: {val} [{src}]")
        return 0

    if subcmd == "list":
        base = Config.load(config_path)
        resolved = resolve_runtime_settings(config_path)
        print("Shared config:")
        for field in sorted(known_fields - _SENSITIVE_FIELDS):
            val = getattr(base, field, None)
            if val is None:
                continue
            print(f"  {field}: {val}")
        print("API keys:")
        for field in sorted(_SENSITIVE_FIELDS):
            val = getattr(resolved.config, field, None)
            if val is None:
                continue
            src = resolved.sources.get(field, "unset")
            print(f"  {field}: {_mask_secret(str(val))} [{src}]")
        env_overrides = {field: src for field, src in resolved.sources.items() if src == "env-override"}
        print("Active env overrides:")
        if not env_overrides:
            print("  (none)")
        else:
            for field in sorted(env_overrides):
                print(f"  {field}")
        return 0

    if subcmd == "unset":
        key = args.key
        if key in _SENSITIVE_FIELDS:
            try:
                data = _read_raw_config(config_path)
                data.pop(key, None)
                _write_config_data(config_path, data)
                delete_secret(key)
                print(f"✓ Removed '{key}' from {config_path} and legacy secret backend")
            except (json.JSONDecodeError, RuntimeError) as e:
                print(f"Error: {e}", file=sys.stderr)
                return 1
            return 0
        cfg = Config.load(path=config_path)
        setattr(cfg, key, None)
        cfg.save(path=config_path)
        print(f"✓ Removed '{key}' from {config_path}")
        return 0

    if subcmd == "migrate-secrets":
        try:
            result = migrate_secrets(
                config_path=config_path,
                force=getattr(args, "force", False),
                to_config=True,
            )
        except SecretStoreError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print("Secret migration complete.")
        if result.imported:
            print(f"  Imported: {', '.join(sorted(result.imported))}")
        if result.preserved:
            print(f"  Preserved existing target values: {', '.join(sorted(result.preserved))}")
        if result.config_updated:
            print("  Updated shared config with zotero_user_id")
        if result.re_registered_platforms:
            print(f"  Re-registered platforms: {', '.join(result.re_registered_platforms)}")
        if result.backups:
            print(f"  Legacy config sources inspected: {', '.join(result.backups)}")
        return 0

    return 0

















def _is_windows_lock_error(stderr: str) -> bool:
    """Check if a failed upgrade looks like a Windows file-locking error.

    Known patterns (pip / uv / Windows):
    - PermissionError — Python's OSError subclass name
    - WinError — ctypes/Windows native error prefix
    - Access is denied — Windows shell error message
    - [Errno 13] — POSIX EACCES, also appears on Windows pip
    - permission (case-insensitive) — broad fallback
    """
    if sys.platform != "win32":
        return False
    lower = stderr.lower()
    return any(kw in lower for kw in [
        "permissionerror", "winerror", "access is denied",
        "[errno 13]", "permission",
    ])


def cmd_update(args):
    """Upgrade ZotPilot CLI and skill files."""
    errors: list[str] = []
    warnings: list[str] = []
    installer, uv_cmd = _detect_cli_installer()
    config_path = _default_config_path()

    # Step 1: Version info
    old_ver = _get_current_version()

    if not args.dry_run:
        latest = _get_latest_pypi_version()
        if latest:
            print(f"  Installed: {old_ver}")
            print(f"  Latest:    {latest}")
        else:
            print(f"  Installed: {old_ver}")
            print("  Latest:    (PyPI unreachable)")
            warnings.append("PyPI unreachable")
    else:
        latest = None
        print(f"[dry-run] current version: {old_ver} (PyPI check skipped)")

    # Step 2: --check mode — just report, always exit 0
    if args.check:
        if latest is None:
            print("Warning: Cannot reach PyPI to check for updates")
        elif old_ver == latest:
            print(f"Already up to date ({old_ver})")
        else:
            print(f"Update available: {old_ver} → {latest}")
        return 0

    # Step 3: CLI update (unless --skill-only)
    if not args.skill_only:
        if installer == "editable":
            print("Dev install detected — update by running git pull in the repo")
            warnings.append("editable install: CLI update skipped")
        elif installer == "uv":
            cmd = uv_cmd + ["tool", "upgrade", "zotpilot"]
            if args.dry_run:
                print(f"[dry-run] Would run: {' '.join(cmd)}")
            else:
                try:
                    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
                    print(result.stdout.strip() or "CLI updated.")
                except FileNotFoundError:
                    manual = " ".join(uv_cmd + ["tool", "upgrade", "zotpilot"])
                    print(f"Command not found ({cmd[0]}) — run manually: {manual}")
                    errors.append(f"{cmd[0]} not found")
                    return 1
                except subprocess.CalledProcessError as e:
                    if _is_windows_lock_error(e.stderr or ""):
                        print("Update failed — the zotpilot executable appears to be locked "
                              "by a running process (e.g. MCP server).\n"
                              "Close all MCP clients (Cursor, VS Code, etc.) and try again.")
                        if e.stderr:
                            print(f"\nOriginal error:\n{e.stderr}")
                    else:
                        print(e.stderr or "Upgrade failed", file=sys.stderr)
                    errors.append("CLI update failed")
                    return 1
        elif installer == "pip":
            cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "zotpilot"]
            if args.dry_run:
                print(f"[dry-run] Would run: {' '.join(cmd)}")
            else:
                try:
                    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
                    print(result.stdout.strip() or "CLI updated.")
                except FileNotFoundError:
                    print(f"Command not found ({cmd[0]}) — run manually: pip install --upgrade zotpilot")
                    errors.append(f"{cmd[0]} not found")
                    return 1
                except subprocess.CalledProcessError as e:
                    if _is_windows_lock_error(e.stderr or ""):
                        print("Update failed — the zotpilot executable appears to be locked "
                              "by a running process (e.g. MCP server).\n"
                              "Close all MCP clients (Cursor, VS Code, etc.) and try again.")
                        if e.stderr:
                            print(f"\nOriginal error:\n{e.stderr}")
                    else:
                        print(e.stderr or "Upgrade failed", file=sys.stderr)
                    errors.append("CLI update failed")
                    return 1
        else:  # unknown
            print("Cannot determine installer. Update manually:")
            print("  uv tool upgrade zotpilot")
            print("  # or:")
            print("  pip install --upgrade zotpilot")
            errors.append("installer unknown: cannot auto-update CLI")

    # Step 4: Runtime / registration maintenance (unless --cli-only)
    if not args.cli_only:
        from ._platforms import register as register_runtime

        if installer == "editable":
            print("Dev install detected — code update remains manual, but runtime will be synchronized")
            warnings.append("editable install: code update skipped")

        if getattr(args, "migrate_secrets", False):
            try:
                migrate_secrets(config_path=config_path, force=False, to_config=True)
                print("Legacy secrets migrated to config.json.")
            except SecretStoreError as exc:
                print(f"Secret migration failed: {exc}", file=sys.stderr)
                errors.append("secret migration failed")

        if args.dry_run:
            try:
                deployment = _deployment_status(resolve_runtime_config(config_path))
                print(f"[dry-run] Drift: {deployment['drift_state']}")
                if deployment.get("legacy_embedded_secrets_detected"):
                    print(
                        "[dry-run] legacy embedded secrets: "
                        + ", ".join(deployment.get("legacy_embedded_secret_platforms", []))
                    )
            except Exception as exc:
                print(f"Runtime reconcile failed: {exc}", file=sys.stderr)
                errors.append("runtime reconcile failed")
        else:
            try:
                deployment = _deployment_status(resolve_runtime_config(config_path))
                if getattr(args, "re_register", False) or deployment["drift_state"] != "clean":
                    results = register_runtime(platforms=None)
                    if results:
                        if not _report_registration_results(results, context="Client registrations refreshed"):
                            errors.append("client registration failed")
            except Exception as exc:
                print(f"Runtime reconcile failed: {exc}", file=sys.stderr)
                errors.append("runtime reconcile failed")

    # Step 5: Post-update summary
    if not args.dry_run:
        new_ver = _get_current_version()
        if new_ver != old_ver:
            print(f"✓ {old_ver} → {new_ver}")
        else:
            print("Version unchanged in this process — restart terminal to activate new binary")
        print("Done. Restart your AI agent to load the new version.")
        print("完成。请重启你的 AI agent 以加载新版本。")

    return 1 if errors else 0


def cmd_sync(args):
    """Developer-facing runtime synchronize command."""
    from ._platforms import register as register_runtime
    try:
        if getattr(args, "dry_run", False):
            deployment = _deployment_status(resolve_runtime_config(_default_config_path()))
            print(f"[dry-run] Drift: {deployment['drift_state']}")
            return 0

        results = register_runtime(platforms=None)
        if results:
            if _report_registration_results(results, context="Runtime synchronized"):
                print("Restart your AI agent to load the new config and skills.")
            else:
                print("Runtime sync incomplete. Fix the failed platforms and retry.", file=sys.stderr)
                return 1
        else:
            print("Runtime already in sync.")
        return 0
    except Exception as exc:
        print(f"Runtime synchronize failed: {exc}", file=sys.stderr)
        return 1


def cmd_bridge(args):
    """Run the HTTP bridge for ZotPilot Connector extension."""
    import time

    from .bridge import BridgeServer

    port = getattr(args, "port", 2619)
    server = BridgeServer(port=port)
    server.start()
    print(f"ZotPilot bridge running on http://127.0.0.1:{port}")
    print("The ZotPilot Connector extension will poll this endpoint.")
    print("Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping bridge...")
        server.stop()
    return 0


def cmd_mcp_serve(_args):
    from .server import main as run_server

    run_server()
    return 0


def _report_registration_results(results: dict[str, bool], *, context: str) -> bool:
    """Print a concise multi-platform registration summary."""
    succeeded = [plat for plat, ok in results.items() if ok]
    failed = [plat for plat, ok in results.items() if not ok]

    if succeeded:
        print(f"{context}: {', '.join(succeeded)}")
    if failed:
        print(f"Failed: {', '.join(failed)}", file=sys.stderr)
    return not failed


def cmd_register(args):
    """Install/register ZotPilot MCP server on AI agent platforms.

    The `register()` call handles both skill file deployment and MCP server
    registration as a single flow — the two are deliberately decoupled at the
    platform-loop level so a skill can still be installed even if the MCP
    add command fails on that platform.
    """
    from ._platforms import register

    config_path = _default_config_path()
    try:
        imported_any = _import_register_secret_overrides(args, config_path)
    except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if imported_any:
        print(
            "Warning: register --*-key flags are deprecated for interactive use. "
            "Credentials were written to config.json instead."
        )

    results = register(
        platforms=args.platforms,
    )
    if not results:
        return 1
    return 0 if _report_registration_results(results, context="Configured") else 1


def main(argv: list[str] | None = None) -> int:
    from . import __version__
    parser = argparse.ArgumentParser(
        prog="zotpilot",
        description="ZotPilot — AI-powered Zotero research assistant",
    )
    parser.add_argument("--version", action="version", version=f"zotpilot {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    # setup
    sub_setup = subparsers.add_parser("setup", help="Configure ZotPilot and register supported AI clients")
    sub_setup.add_argument(
        "--non-interactive", action="store_true",
        help="Run without prompts (use flags or auto-detect)",
    )
    sub_setup.add_argument("--zotero-dir", type=str, default=None, help="Zotero data directory path")
    sub_setup.add_argument(
        "--provider", type=str, default=None,
        choices=["gemini", "dashscope", "local"],
        help="Embedding provider (default: gemini)",
    )
    sub_setup.add_argument("--gemini-key", type=str, default=None, help=argparse.SUPPRESS)
    sub_setup.add_argument("--dashscope-key", type=str, default=None, help=argparse.SUPPRESS)
    sub_setup.set_defaults(func=cmd_setup)

    # index
    sub_index = subparsers.add_parser("index", help="Index Zotero library")
    sub_index.add_argument("--force", action="store_true", help="Force re-index all")
    sub_index.add_argument("--limit", type=int, default=None, help="Max items to index")
    sub_index.add_argument("--item-key", type=str, default=None, help="Index specific item")
    sub_index.add_argument("--title", type=str, default=None, help="Filter by title regex")
    sub_index.add_argument("--max-pages", type=int, default=None,
        help="Skip PDFs longer than N pages (default: 40, 0=no limit)")
    sub_index.add_argument("--no-vision", action="store_true", help="Disable vision extraction")
    sub_index.add_argument("--batch-size", type=int, default=2,
        help="Process N items per call (default: 2, 0 = all at once)")
    sub_index.add_argument("--config", type=str, default=None, help="Config file path")
    sub_index.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    sub_index.set_defaults(func=cmd_index)

    # status
    sub_status = subparsers.add_parser("status", help="Show config and index stats")
    sub_status.add_argument("--config", type=str, default=None, help="Config file path")
    sub_status.add_argument("--json", action="store_true", help="Output as JSON")
    sub_status.set_defaults(func=cmd_status)

    # doctor
    sub_doctor = subparsers.add_parser("doctor", help="Check environment health")
    sub_doctor.add_argument("--config", type=str, default=None, help="Config file path")
    sub_doctor.add_argument("--json", action="store_true", help="Output as JSON")
    sub_doctor.add_argument("--full", action="store_true", help="Include slow checks (API connectivity)")
    sub_doctor.set_defaults(func=cmd_doctor)

    # config
    sub_config = subparsers.add_parser("config", help="Manage ZotPilot configuration")
    sub_config.add_argument("--config", default=str(_default_config_path()), help=argparse.SUPPRESS)
    config_sub = sub_config.add_subparsers(dest="config_subcmd")

    cfg_set = config_sub.add_parser("set", help="Set a config value")
    cfg_set.add_argument("key", help="Config field name")
    cfg_set.add_argument("value", help="Value to set")

    cfg_get = config_sub.add_parser("get", help="Get a config value")
    cfg_get.add_argument("key", help="Config field name")

    config_sub.add_parser("list", help="List all config values")

    cfg_unset = config_sub.add_parser("unset", help="Remove a config value")
    cfg_unset.add_argument("key", help="Config field name")

    config_sub.add_parser("path", help="Print config file path")
    cfg_migrate = config_sub.add_parser("migrate-secrets", help="Migrate legacy secrets")
    cfg_migrate.add_argument("--force", action="store_true", help="Overwrite existing target values")
    sub_config.set_defaults(func=cmd_config)

    # update
    sub_update = subparsers.add_parser(
        "upgrade",
        aliases=["update"],
        help="Upgrade ZotPilot and refresh client registration",
    )
    grp = sub_update.add_mutually_exclusive_group()
    grp.add_argument("--cli-only", action="store_true", help="Only update CLI")
    grp.add_argument("--skill-only", action="store_true", help="Only update skill files")
    mode_grp = sub_update.add_mutually_exclusive_group()
    mode_grp.add_argument("--check", action="store_true",
        help="Check for updates without installing (always exits 0)")
    mode_grp.add_argument("--dry-run", action="store_true",
        help="Preview update actions without making changes (skips PyPI)")
    sub_update.add_argument(
        "--migrate-secrets",
        action="store_true",
        help="Migrate legacy secrets into config.json",
    )
    sub_update.add_argument(
        "--re-register",
        action="store_true",
        help="Force re-register ZotPilot on detected clients",
    )
    sub_update.set_defaults(func=cmd_update)

    # sync
    sub_sync = subparsers.add_parser("sync", help=argparse.SUPPRESS)
    sub_sync.add_argument("--dry-run", action="store_true", help="Preview runtime sync changes")
    sub_sync.set_defaults(func=cmd_sync)

    # bridge
    sub_bridge = subparsers.add_parser("bridge", help="Run HTTP bridge for ZotPilot Connector")
    sub_bridge.add_argument("--port", type=int, default=2619, help="HTTP port (default: 2619)")
    sub_bridge.set_defaults(func=cmd_bridge)

    # mcp
    sub_mcp = subparsers.add_parser("mcp", help="Internal MCP server commands")
    mcp_sub = sub_mcp.add_subparsers(dest="mcp_subcmd")
    mcp_serve = mcp_sub.add_parser("serve", help="Run the ZotPilot MCP server")
    mcp_serve.set_defaults(func=cmd_mcp_serve)

    # register / install
    sub_register = subparsers.add_parser(
        "register",
        aliases=["install"],
        help="Advanced: refresh client registration only",
    )
    sub_register.add_argument("--platform", action="append", dest="platforms",
                              help="Target platform (repeatable). Auto-detects if omitted.")
    sub_register.add_argument("--gemini-key", dest="gemini_key")
    sub_register.add_argument("--dashscope-key", dest="dashscope_key")
    sub_register.add_argument("--zotero-api-key", dest="zotero_api_key")
    sub_register.add_argument("--zotero-user-id", dest="zotero_user_id")
    sub_register.set_defaults(func=cmd_register)

    args = parser.parse_args(argv)

    if not args.command:
        # Default: run MCP server — signal state.py to start parent monitor
        os.environ["ZOTPILOT_SERVER"] = "1"
        from .server import main as server_main
        server_main()
        return 0

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

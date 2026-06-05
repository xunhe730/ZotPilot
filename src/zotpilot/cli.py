"""CLI entry point for ZotPilot."""
import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

from . import providers
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


class ProbeResult(NamedTuple):
    """Outcome of a connectivity self-check against an openai-compatible endpoint.

    ``state`` classifies the outcome for the machine-readable ``--verify`` JSON
    taxonomy: ``ok`` | ``dim_mismatch`` | ``auth`` | ``unreachable`` | ``error``
    (``skipped`` is produced by the caller for non-wire-probeable providers, not
    by ``_probe_endpoint`` itself). The wizard self-check only reads ``ok`` /
    ``returned_dim`` / ``message``; ``state`` is additive and defaults to ``ok``.
    """

    ok: bool
    message: str
    returned_dim: int | None
    state: str = "ok"


def _probe_endpoint(
    base_url: str,
    api_key: str | None,
    model: str,
    dims: int,
    timeout: float = 5.0,
) -> ProbeResult:
    """Tiny single-embed connectivity self-check (U2).

    Sends ONE embed request (not ``GET /models``) so it can compare the returned
    vector length against the user-entered ``dims`` and surface a C1 mismatch at
    setup time. Never raises; all failures are reported via ``ProbeResult``.

    Mirrors the runtime embedder's dimensions-drop-on-400 fallback
    (``openai_compat.py``): a fixed-dimension model (e.g. SiliconFlow ``bge-m3``)
    rejects the ``dimensions`` parameter with HTTP 400, so on a 400 we retry ONCE
    without ``dimensions`` and classify on the no-dims response length -- otherwise
    such a model would false-fail the self-check even though it indexes fine.
    HTTP 401/403 -> ``auth``; connect/timeout -> ``unreachable``; anything else
    -> ``error``, each kept DISTINCT so the Agent gets correct remediation.
    """
    import httpx

    url = base_url.rstrip("/") + "/embeddings"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    def _post(send_dimensions: bool) -> dict:
        payload: dict = {
            "model": model,
            "input": "ping",
            "encoding_format": "float",
        }
        if send_dimensions:
            payload["dimensions"] = dims
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            return response.json()

    try:
        try:
            data = _post(send_dimensions=True)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 400:
                # Fixed-dimension endpoint rejecting `dimensions`; retry once
                # without it and classify on the native response length.
                data = _post(send_dimensions=False)
            else:
                raise
        vec = data["data"][0]["embedding"]
        returned_dim = len(vec) if isinstance(vec, list) else None
        if returned_dim is not None and returned_dim != dims:
            return ProbeResult(
                False,
                f"Endpoint returned {returned_dim}-dimensional vectors but you entered "
                f"embedding_dimensions={dims}. Set embedding_dimensions to {returned_dim} "
                f"to match the server's native output.",
                returned_dim,
                "dim_mismatch",
            )
        return ProbeResult(
            True,
            f"Connectivity OK — endpoint returned {returned_dim}-dimensional vectors.",
            returned_dim,
            "ok",
        )
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        if status in (401, 403):
            return ProbeResult(
                False,
                f"Authentication failed (HTTP {status}). Check the API key for "
                f"{base_url}.",
                None,
                "auth",
            )
        return ProbeResult(
            False,
            f"HTTP {status} from {url}: {e.response.text[:200]}",
            None,
            "error",
        )
    except (httpx.ConnectError, httpx.TimeoutException):
        return ProbeResult(
            False,
            f"Cannot reach {base_url}. Is the server running? "
            f"For Ollama, try `ollama serve`, then `ollama pull {model}`.",
            None,
            "unreachable",
        )
    except Exception as e:  # noqa: BLE001 — self-check must never crash setup
        return ProbeResult(False, f"Self-check failed: {e}", None, "error")


def _vendor_catalog_payload() -> dict:
    """Build the versioned ``--list-vendors --json`` envelope from VENDOR_CATALOG.

    The Agent's discovery contract (D6.1). ``schema_version`` decouples the skill
    from ``Vendor``/``VendorModel`` field churn; the skill asserts it before
    parsing. ``.vendors`` mirrors VENDOR_CATALOG exactly (a test pins equality).
    """
    return {
        "schema_version": 1,
        "vendors": [
            {
                "key": v.key,
                "label": v.label,
                "provider": v.provider,
                "base_url": v.base_url,
                "requires_key": v.requires_key,
                "key_url": v.key_url,
                "aliases": list(v.aliases),
                "allow_custom_model": v.allow_custom_model,
                "models": [
                    {
                        "model": m.model,
                        "dimensions": m.dimensions,
                        "note": m.note,
                        "recommended": m.recommended,
                    }
                    for m in v.models
                ],
            }
            for v in providers.VENDOR_CATALOG
        ],
    }


def _print_vendor_catalog(as_json: bool) -> int:
    """Print the vendor catalog for Agent/human discovery (D6.1). Returns 0.

    Short-circuits ``cmd_setup`` BEFORE any Zotero detection so it works on a
    machine with no Zotero install (the MCP server may not be configured yet).
    """
    if as_json:
        print(json.dumps(_vendor_catalog_payload(), ensure_ascii=False, indent=2))
        return 0
    print("Available embedding vendors (vendor → model):")
    for v in providers.VENDOR_CATALOG:
        key_note = "no key" if not v.requires_key else "requires key"
        alias_note = f" (aliases: {', '.join(v.aliases)})" if v.aliases else ""
        print(f"\n  {v.key} — {v.label} [{v.provider}, {key_note}]{alias_note}")
        if v.base_url:
            print(f"    base_url: {v.base_url}")
        if v.key_url:
            print(f"    key: {v.key_url}")
        if v.models:
            for m in v.models:
                star = " ★ recommended" if m.recommended else ""
                hint = f" [{m.note}]" if m.note else ""
                print(f"    - {m.model} ({m.dimensions}d){hint}{star}")
        if v.allow_custom_model:
            print("    - <custom model> (enter model + dimensions)")
    print("\nConfigure with: zotpilot setup --non-interactive --provider <vendor> "
          "[--embedding-model <m>] [--verify]")
    return 0


def _prompt_vendor_model(vendor) -> tuple[str | None, str, int, str | None]:
    """Interactive Layer-2 model selection for a chosen vendor.

    Returns ``(base_url, embedding_model, embedding_dimensions, api_key)`` where
    ``base_url`` is ``None`` for the gemini/dashscope/local providers and the
    ``api_key`` is collected INLINE only for openai-compatible-mapped vendors
    (gemini/dashscope keys are collected by the vendor-aware Step 3, local/ollama
    need none). All paths normalize through ``providers.resolve_setup_choice``.
    """
    is_oai = vendor.provider == "openai-compatible"
    is_custom = is_oai and not vendor.base_url  # free-form Custom vendor

    # base_url: fixed (overridable) for siliconflow/zhipu/ollama; free-form for
    # Custom; not applicable (None) for gemini/dashscope/local.
    base_url: str | None = vendor.base_url or None
    if is_custom:
        print("\n  Custom OpenAI-compatible endpoint. base_url is the API root")
        print("  (usually ends in /v1, e.g. http://localhost:11434/v1; GLM uses /api/paas/v4).")
        base_url = input("  base_url: ").strip() or None
    elif is_oai:
        entered = input(f"  base_url [{vendor.base_url}]: ").strip()
        if entered and entered != vendor.base_url:
            # Mirror the non-interactive override WARNING so both surfaces guide
            # the user identically when diverging from the vetted endpoint.
            print(
                f"  WARNING: overriding the built-in base_url for '{vendor.key}' "
                f"({vendor.base_url}). The self-check still catches a dimension "
                f"mismatch, but a wrong-but-same-dimension endpoint cannot be detected."
            )
        base_url = entered or vendor.base_url

    # Layer-2 model menu.
    chosen_model: str | None = None
    chosen_dims: int | None = None
    custom_idx: int | None = None
    if vendor.models:
        print(f"\n  Choose a {vendor.label} model (press Enter for the recommended one):")
        rec = providers.recommended_model(vendor)
        for i, m in enumerate(vendor.models, 1):
            star = " ★ recommended" if m.recommended else ""
            hint = f"  [{m.note}]" if m.note else ""
            print(f"    {i}. {m.model} ({m.dimensions}d){star}{hint}")
        if vendor.allow_custom_model:
            custom_idx = len(vendor.models) + 1
            print(f"    {custom_idx}. Custom model (enter model + dimensions)")
        # Default to the recommended model; a valid numeric pick overrides it,
        # and the "Custom model" entry clears it to fall through to free-form.
        if rec:
            chosen_model, chosen_dims = rec.model, rec.dimensions
        sel = input(f"  Choice [1-{custom_idx or len(vendor.models)}, Enter=recommended]: ").strip()
        if custom_idx is not None and sel == str(custom_idx):
            chosen_model = chosen_dims = None
        elif sel:
            try:
                m = vendor.models[int(sel) - 1]
                chosen_model, chosen_dims = m.model, m.dimensions
            except (ValueError, IndexError):
                pass  # keep the recommended default

    if chosen_model is None:
        # Custom vendor, or "Custom model" picked, or no curated models.
        chosen_model = input("  embedding_model: ").strip()
        print("  embedding_dimensions must be set explicitly: non-matryoshka servers")
        print("  ignore a requested size and return their native dimension.")
        dims_raw = input("  embedding_dimensions: ").strip()
        try:
            chosen_dims = int(dims_raw) if dims_raw else None
        except ValueError:
            chosen_dims = None

    # Vendor-aware key prompt (only for openai-compatible-mapped vendors here).
    api_key: str | None = None
    if is_oai:
        if vendor.requires_key:
            if vendor.key_url:
                print(f"  Get an API key at: {vendor.key_url}")
            api_key = input("  API key (leave blank if none): ").strip() or None
        else:
            print("  No API key needed for local Ollama.")

    # Normalize through the shared resolver (fills recommended/base defaults,
    # validates required dims) -- the SAME mapping the non-interactive CLI uses.
    try:
        _provider, base_url, chosen_model, chosen_dims = providers.resolve_setup_choice(
            vendor.key, chosen_model, chosen_dims, base_url
        )
    except ValueError as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        return base_url, chosen_model or "", chosen_dims or 0, api_key

    # U2: non-blocking connectivity self-check (openai-compatible-mapped vendors
    # only); print that it makes ONE tiny embedding call, with a skip affordance.
    if is_oai and base_url and chosen_model and chosen_dims > 0:
        if input(
            "  Run a connectivity self-check now? It makes one tiny embedding "
            "call. [Y/n] "
        ).strip().lower() not in ("n", "no"):
            probe = _probe_endpoint(base_url, api_key, chosen_model, chosen_dims)
            print(f"  {'OK' if probe.ok else 'WARNING'}: {probe.message}")
            if (
                not probe.ok
                and probe.returned_dim
                and probe.returned_dim != chosen_dims
            ):
                if input(
                    f"  Update embedding_dimensions to {probe.returned_dim}? [Y/n] "
                ).strip().lower() not in ("n", "no"):
                    chosen_dims = probe.returned_dim

    return base_url, chosen_model, chosen_dims, api_key


def _run_setup_verify(
    provider: str,
    base_url: str | None,
    api_key: str | None,
    model: str | None,
    dims: int | None,
) -> tuple[dict, int]:
    """Opt-in ``--verify`` pre-flight of the candidate (model, dims) (D6.3).

    Returns ``(payload, exit_code)``. Only ``dim_mismatch`` exits non-zero -- it
    is the one deterministic "config is wrong" signal the Agent can self-heal
    from. ``auth``/``unreachable``/``error``/``skipped`` all exit 0 so surface ③
    (CI, no reachable endpoint) stays headless-safe. Non-wire-probeable providers
    (gemini/dashscope/local) are ``skipped`` (the skill always passes ``--verify``).
    """
    if provider != "openai-compatible" or not base_url or not model or not dims:
        return (
            {
                "verify": "skipped",
                "reason": "provider not wire-probeable",
                "expected_dim": dims,
                "returned_dim": None,
                "message": f"--verify skipped for provider {provider!r}.",
            },
            0,
        )
    probe = _probe_endpoint(base_url, api_key, model, dims)
    payload = {
        "verify": probe.state,
        "expected_dim": dims,
        "returned_dim": probe.returned_dim,
        "message": probe.message,
    }
    return payload, (1 if probe.state == "dim_mismatch" else 0)


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

    # D6.1: `--list-vendors` is a pure catalog dump for Agent/human discovery.
    # It MUST short-circuit BEFORE Zotero detection / provider validation so it
    # works on a machine with no Zotero install and before setup is configured.
    if getattr(args, "list_vendors", False):
        return _print_vendor_catalog(as_json=getattr(args, "json", False))

    non_interactive = getattr(args, "non_interactive", False)

    # OpenAI-compatible provider locals (populated from args or the wizard below;
    # left None for every other provider so the write block does not clobber them).
    embedding_base_url = None
    embedding_api_key = None
    embedding_model = None
    embedding_dimensions = None

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

        # Provider/vendor from flag (omitted -> gemini, preserving prior default).
        vendor_arg = getattr(args, "provider", None) or "gemini"
        vendor = providers.resolve_vendor(vendor_arg)
        if vendor is None:
            print(
                f"ERROR: Invalid provider '{vendor_arg}'. Must be one of: "
                f"{', '.join(providers.vendor_cli_choices())}.",
                file=sys.stderr,
            )
            return 1

        base_url_arg = getattr(args, "embedding_base_url", None)
        # WARN (do not block) when overriding a fixed-base vendor's built-in URL.
        if base_url_arg and vendor.base_url and base_url_arg.strip() != vendor.base_url:
            print(
                f"WARNING: --embedding-base-url overrides the built-in base_url for "
                f"vendor '{vendor.key}' ({vendor.base_url}). The setup probe still "
                f"catches a dimension mismatch, but a wrong-but-same-dimension "
                f"endpoint cannot be detected.",
                file=sys.stderr,
            )
        try:
            (
                embedding_provider,
                embedding_base_url,
                embedding_model,
                embedding_dimensions,
            ) = providers.resolve_setup_choice(
                vendor_arg,
                getattr(args, "embedding_model", None),
                getattr(args, "embedding_dimensions", None),
                base_url_arg,
            )
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        embedding_api_key = getattr(args, "embedding_key", None)

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

        # Layer 1 — choose embedding vendor (from the single VENDOR_CATALOG).
        print("\n[2/5] Choose embedding vendor:")
        for i, v in enumerate(providers.VENDOR_CATALOG, 1):
            key_note = "" if v.requires_key else "  · no key"
            print(f"  {i}. {v.label}{key_note}")
        sel = input(f"  Choice [1-{len(providers.VENDOR_CATALOG)}]: ").strip()
        try:
            chosen_vendor = providers.VENDOR_CATALOG[int(sel) - 1]
        except (ValueError, IndexError):
            chosen_vendor = providers.VENDOR_CATALOG[0]  # default: Google (Gemini)
        embedding_provider = chosen_vendor.provider
        # Layer 2 — model selection (+ base_url/key for openai-compatible vendors).
        (
            embedding_base_url,
            embedding_model,
            embedding_dimensions,
            embedding_api_key,
        ) = _prompt_vendor_model(chosen_vendor)

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
    elif not non_interactive and embedding_provider == "local":
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
        embedding_base_url=embedding_base_url or base_config.embedding_base_url,
        embedding_api_key=embedding_api_key or base_config.embedding_api_key,
        embedding_model=embedding_model or base_config.embedding_model,
        embedding_dimensions=(
            embedding_dimensions
            if embedding_dimensions is not None
            else base_config.embedding_dimensions
        ),
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
        elif embedding_provider == "openai-compatible":
            print(f"OpenAI-compatible endpoint: {embedding_base_url}")
            print(f"  model={embedding_model}, dimensions={embedding_dimensions}")
            print("Set the API key (if your endpoint needs one) via:")
            print("  export ZOTPILOT_EMBEDDING_API_KEY='<your-key>'")
            print("  or: zotpilot config set embedding_api_key <key>")
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

    # D6.3: opt-in `--verify` pre-flight. Meaningful only with --non-interactive
    # (the interactive wizard already self-checks); a no-op note otherwise.
    if getattr(args, "verify", False):
        if not non_interactive:
            print(
                "Note: --verify is only meaningful with --non-interactive "
                "(the interactive wizard already runs a self-check)."
            )
        else:
            payload, verify_rc = _run_setup_verify(
                embedding_provider,
                embedding_base_url,
                embedding_api_key,
                embedding_model,
                embedding_dimensions,
            )
            print(json.dumps(payload, ensure_ascii=False))
            return verify_rc

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
            "dashscope_embedding_endpoint": config.dashscope_embedding_endpoint,
            "gemini_key_set": bool(config.gemini_api_key),
            "dashscope_key_set": bool(config.dashscope_api_key),
            "vision_enabled": config.vision_enabled,
            "vision_provider": config.vision_provider,
            "vision_model": config.vision_model,
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
            from .vector_store import EmbeddingDimensionMismatchError, IndexUnavailableError, VectorStore
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
        except EmbeddingDimensionMismatchError as e:
            result["errors"].append(
                f"Embedding dimension mismatch: {e} "
                "Switch back to the original embedding provider, or reindex with `zotpilot index --force`."
            )
        except IndexUnavailableError as e:
            result["errors"].append(
                f"Index unavailable (data left intact): {e} "
                "Often transient — retry; if it persists run `zotpilot doctor --recover-index`."
            )
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
    if config.embedding_provider == "dashscope":
        print(f"  DashScope endpoint:  {config.dashscope_embedding_endpoint}")
    print(f"  Legacy secret backend: {resolved.secret_backend}")
    print(f"  Write ops ready:    {'yes' if (config.zotero_api_key and config.zotero_user_id) else 'no'}")
    print(f"  Embedding model:    {config.embedding_model}")
    print(f"  Embedding dims:     {config.embedding_dimensions}")
    print(f"  Reranking enabled:  {config.rerank_enabled}")
    print(f"  Vision enabled:     {config.vision_enabled}")
    print(f"  Vision provider:    {config.vision_provider}")
    print(f"  Vision model:       {config.vision_model}")
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
        from .vector_store import EmbeddingDimensionMismatchError, IndexUnavailableError, VectorStore
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
    except EmbeddingDimensionMismatchError as e:
        print(f"\n  ✗ Embedding dimension mismatch: {e}")
        print("    Switch back to the original embedding provider, or reindex with `zotpilot index --force`.")
    except IndexUnavailableError as e:
        print(f"\n  ✗ Index unavailable (your data was left intact): {e}")
        print("    Often transient — retry; if it persists run `zotpilot doctor --recover-index`.")
    except Exception as e:
        print(f"\n  Could not read index: {e}")

    return 0


def cmd_doctor(args):
    from .doctor import run_checks

    # `is True` (not truthiness): argparse store_true yields real booleans, while a
    # MagicMock args object would make a bare getattr truthy and misfire the dispatch.
    if getattr(args, "recover_index", False) is True:
        return _cmd_recover_index(args)
    if getattr(args, "reconcile", False) is True:
        return _cmd_reconcile(args)

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


def _cmd_recover_index(args) -> int:
    """Thin CLI glue for `zotpilot doctor --recover-index` (engine in index_recovery)."""
    from .embeddings import create_embedder
    from .index_recovery import (
        HnswlibUnavailableError,
        RecoverySourceError,
        RecoveryVerificationError,
        recover_index,
    )

    config = resolve_runtime_config(args.config)
    source = Path(args.source).expanduser() if getattr(args, "source", None) else None
    dry_run = getattr(args, "dry_run", False)

    # The re-embed fallback (paid) is only enabled after explicit interactive
    # confirmation; non-interactive runs stick to the zero-cost HNSW path.
    embedder = None
    try:
        embedder = create_embedder(config)
    except Exception as exc:  # noqa: BLE001 - embedder only needed for the paid fallback
        logger.debug("embedder unavailable for recovery fallback: %s", exc)

    def _confirm(report) -> bool:
        if report.method != "reembed":
            return True
        print(
            f"\n  ⚠ HNSW vectors unreadable — the only way to recover is to RE-EMBED "
            f"{report.recovered_count} stored chunks via '{config.embedding_provider}'."
        )
        print("    This WILL cost embedding-API calls. Install chroma-hnswlib for free recovery:")
        print(f"      {_recover_install_hint()}")
        return input("    Proceed with paid re-embed? [y/N] ").strip().lower() in ("y", "yes")

    allow_reembed = not dry_run and sys.stdin.isatty()
    try:
        report = recover_index(
            config.chroma_db_path,
            config.embedding_dimensions,
            source=source,
            dry_run=dry_run,
            embedder=embedder,
            allow_reembed=allow_reembed,
            confirm=_confirm if allow_reembed else None,
        )
    except RecoverySourceError as exc:
        print(f"✗ {exc}")
        return 1
    except HnswlibUnavailableError as exc:
        print(f"✗ {exc}")
        print(f"  Install the optional recovery extra: {_recover_install_hint()}")
        return 1
    except RecoveryVerificationError as exc:
        print(f"✗ Recovery verification failed — original index left untouched.\n  {exc}")
        return 1

    print("\nZotPilot Index Recovery")
    print("=" * 50)
    for message in report.messages:
        print(f"  {message}")
    if report.dry_run:
        print(
            f"\n  DRY RUN: would recover {report.recovered_count} chunks across "
            f"{report.doc_count} documents (method={report.method}). No changes made."
        )
        return 0
    if report.swapped:
        print(
            f"\n  ✓ Recovered {report.recovered_count} chunks across {report.doc_count} documents "
            f"(method={report.method}, merged {report.merged_count} live-only)."
        )
        return 0
    print("\n  Recovery did not complete (no swap performed).")
    return 1


def _recover_install_hint() -> str:
    from .index_recovery import INSTALL_HINT

    return INSTALL_HINT


def _cmd_reconcile(args) -> int:
    """Standalone orphan reconciliation: opt-in, dry-run preview, --force override."""
    from .embeddings import create_embedder
    from .index_authority import (
        current_library_pdf_doc_ids,
        orphaned_index_doc_ids,
        reconcile_orphaned_index_docs,
    )
    from .vector_store import EmbeddingDimensionMismatchError, IndexUnavailableError, VectorStore
    from .zotero_client import ZoteroClient

    config = resolve_runtime_config(args.config)
    try:
        store = VectorStore(config.chroma_db_path, create_embedder(config))
    except EmbeddingDimensionMismatchError as exc:
        print(f"✗ Embedding dimension mismatch: {exc}")
        return 1
    except IndexUnavailableError as exc:
        print(f"✗ Index unavailable (data left intact): {exc}")
        return 1

    zotero = ZoteroClient(config.zotero_data_dir)
    current = current_library_pdf_doc_ids(zotero)
    dry_run = getattr(args, "dry_run", False)
    force = getattr(args, "force", False)

    if dry_run:
        orphaned = sorted(orphaned_index_doc_ids(store, current))
        print("ZotPilot Reconcile (dry run)")
        print("=" * 50)
        print(f"  Current library documents: {len(current)}")
        print(f"  Orphaned indexed documents: {len(orphaned)}")
        for doc_id in orphaned[:50]:
            print(f"    - {doc_id}")
        if len(orphaned) > 50:
            print(f"    ... and {len(orphaned) - 50} more")
        print("\n  No changes made. Re-run without --dry-run to reconcile"
              f"{' (add --force for >25% deletions)' if not force else ''}.")
        return 0

    # Mirror Indexer._library_unreachable: an unmounted external drive must still
    # refuse mass deletion even under --force (parity with the auto-callers).
    library_unreachable = not Path(config.zotero_data_dir).exists()
    summary = reconcile_orphaned_index_docs(
        store, current, allow_mass_delete=force, library_unreachable=library_unreachable
    )
    if summary.get("refused_mass_delete"):
        print("✗ Reconciliation refused (no changes made):")
        print(f"  {summary.get('skipped_reason', 'safety floor breached')}")
        return 1
    print(f"✓ Reconciled: deleted {summary['deleted_count']} orphaned document(s) from the index.")
    return 0


def _mask_secret(v: str) -> str:
    return v[:4] + "****" if len(v) > 4 else "****"


_SENSITIVE_FIELDS = {
    "gemini_api_key", "dashscope_api_key", "anthropic_api_key",
    "zotero_api_key", "semantic_scholar_api_key", "embedding_api_key",
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
    # {env:VAR} references must round-trip as literals (H3): bypass JSON parse so
    # `config set embedding_api_key '{env:OPENAI_API_KEY}'` is not eaten by the
    # leading-`{` JSON heuristic below.
    if value.startswith("{env:"):
        return value
    # dict/list fields: try JSON parse
    if value.startswith("{") or value.startswith("["):
        return json.loads(value)
    return value


def _warn_if_index_bound_change(key: str, value: str, config_path: Path) -> None:
    """Print ONE advisory warning when `config set` mutates an index-bound field
    and a non-empty index directory exists (Decision 8).

    Purely additive UX: it never blocks the set. Hash comparison reuses
    ``config._config_hash``; coercion/replace errors are swallowed so the real
    set path surfaces them instead.
    """
    import dataclasses

    from .config import _config_hash

    try:
        current = Config.load(config_path)
        proposed = dataclasses.replace(current, **{key: _coerce_value(key, value)})
    except Exception:
        return
    if _config_hash(current) == _config_hash(proposed):
        return
    chroma_dir = current.chroma_db_path
    try:
        non_empty = chroma_dir.exists() and any(chroma_dir.iterdir())
    except OSError:
        non_empty = False
    if non_empty:
        print(
            "WARNING: This changes an index-bound setting. Run 'zotpilot index --force' "
            "to rebuild. Until then, search results may be degraded."
        )


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
        _warn_if_index_bound_change(key, value, config_path)
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
        choices=providers.vendor_cli_choices(),
        help="Embedding vendor (default: gemini). See `setup --list-vendors`.",
    )
    sub_setup.add_argument(
        "--list-vendors", action="store_true",
        help="List the vendor→model catalog and exit (use --json for a machine-readable envelope)",
    )
    sub_setup.add_argument(
        "--json", action="store_true",
        help="With --list-vendors, emit the catalog as a JSON envelope",
    )
    sub_setup.add_argument(
        "--verify", action="store_true",
        help="After a --non-interactive write, probe the endpoint and print a JSON verify result",
    )
    sub_setup.add_argument(
        "--embedding-base-url", type=str, default=None,
        help="OpenAI-compatible endpoint root (e.g. http://localhost:11434/v1)",
    )
    sub_setup.add_argument(
        "--embedding-model", type=str, default=None,
        help="Embedding model name (required for --provider openai-compatible)",
    )
    sub_setup.add_argument(
        "--embedding-dimensions", type=int, default=None,
        help="Embedding output dimensions (required for --provider openai-compatible)",
    )
    sub_setup.add_argument(
        "--embedding-key", type=str, default=None,
        help="API key for the openai-compatible endpoint (omit for local Ollama)",
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
    sub_doctor.add_argument(
        "--recover-index",
        action="store_true",
        help="Rebuild the vector index from an intact SQLite + HNSW backup (zero-cost)",
    )
    sub_doctor.add_argument(
        "--reconcile",
        action="store_true",
        help="Remove indexed documents that no longer exist in the Zotero library (opt-in)",
    )
    sub_doctor.add_argument(
        "--source",
        type=str,
        default=None,
        help="Recovery source dir (a 'chroma.corrupt-*' backup); autodiscovered if omitted",
    )
    sub_doctor.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview recovery/reconcile actions without writing or deleting",
    )
    sub_doctor.add_argument(
        "--force",
        action="store_true",
        help="With --reconcile: allow deletions exceeding the 25%% mass-delete safety floor",
    )
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

"""Single source of truth for the embedding-provider registry.

This module is intentionally a LEAF: it imports ONLY the standard library
(``os``/``re``/``typing``) so that heavier modules such as :mod:`zotpilot.config`,
:mod:`zotpilot.embeddings`, and :mod:`zotpilot.indexer` can depend on it without
risking an import cycle.

It centralizes the embedding provider allow-list, the per-provider model and
dimension defaults, the (setup-only) ``VENDOR_CATALOG`` two-layer vendor->model
catalog plus its resolvers, and the shared ``{env:VAR}`` secret/URL helper.

Vision provider allow-lists are deliberately NOT centralized here -- they stay
inline at their existing call sites (see the plan, Decision 6).

**Principle-1 boundary (hard constraint).** ``VENDOR_CATALOG`` and
``resolve_setup_choice`` (and the ``Vendor``/``VendorModel`` types and the other
``resolve_*``/``vendor_*`` helpers) are a SETUP-LAYER mapping only. They MUST
NEVER be imported by :mod:`zotpilot.config` or by any embedder in
:mod:`zotpilot.embeddings`. The runtime authority stays ``EMBEDDING_PROVIDERS`` +
``EMBEDDING_MODEL_DEFAULTS`` (read by ``config.load()``); the catalog merely maps
a human/Agent/CLI vendor choice onto those runtime fields at setup time. A test
(``test_provider_registry.py``) asserts ``config`` does not import the catalog
symbols, so a future "DRY the defaults" refactor cannot silently cross this line.
"""
from __future__ import annotations

import os
import re
from typing import NamedTuple

# Canonical embedding-provider allow-list. ``openai-compatible`` is the new
# generic, wire-config provider; ``none`` is the No-RAG sentinel.
EMBEDDING_PROVIDERS: tuple[str, ...] = (
    "gemini",
    "dashscope",
    "local",
    "openai-compatible",
    "none",
)

# Per-provider (model, dimensions) defaults. Mirrors the historical inline dict
# in ``config.load()`` plus the ``openai-compatible`` sentinel meaning "the user
# MUST specify model + dimensions" (enforced by ``Config.validate()``).
EMBEDDING_MODEL_DEFAULTS: dict[str, tuple[str, int]] = {
    "gemini": ("gemini-embedding-001", 768),
    "dashscope": ("text-embedding-v4", 1024),
    "local": ("all-MiniLM-L6-v2", 384),
    "none": ("none", 0),
    "openai-compatible": ("", 0),
}


class VendorModel(NamedTuple):
    """One curated Layer-2 model offered under a :class:`Vendor`.

    ``(model, dimensions)`` are advisory setup defaults, LIVE-VERIFIED against
    the vendor's ``/embeddings`` endpoint before commit (see
    ``scripts/verify_vendor_catalog.py``). A stale ``dimensions`` degrades to a
    contributor-time drift-script failure first and at worst a user-side setup
    probe / runtime C1 warning -- never silent corruption.
    """

    model: str
    dimensions: int
    note: str = ""  # value/positioning hint shown in the Layer-2 menu
    recommended: bool = False  # pre-selected default (blank input picks it)


class Vendor(NamedTuple):
    """One Layer-1 vendor in the two-layer setup catalog.

    Maps a human/Agent/CLI vendor choice onto the runtime ``embedding_provider``
    (always a member of :data:`EMBEDDING_PROVIDERS`) plus its wire ``base_url``.
    Multiple vendors may share one runtime ``provider`` (siliconflow/zhipu/ollama/
    custom all map to ``openai-compatible``, differing only by ``base_url``).
    """

    key: str  # canonical CLI value, e.g. "siliconflow"
    label: str  # menu label, e.g. "SiliconFlow"
    provider: str  # runtime embedding_provider it maps to (in EMBEDDING_PROVIDERS)
    base_url: str | None  # fixed for siliconflow/zhipu/ollama; None for
    #                       gemini/dashscope/local; "" => user-supplied (Custom)
    requires_key: bool
    key_url: str
    key_env: tuple[str, ...]  # env var(s) the key resolves from (help/display)
    models: tuple[VendorModel, ...]  # curated; empty => free-form (Custom)
    aliases: tuple[str, ...]  # accepted CLI synonyms (e.g. ("gemini",) for google)
    allow_custom_model: bool  # offer a "custom model (enter model + dims)" entry


# Single source of truth for the two-layer vendor->model setup UX. Pure data:
# adding/updating a model is one ``VendorModel`` edit, no code change. Each cloud
# ``(model, dimensions)`` was LIVE-VERIFIED (2026-06); editing a dimension
# REQUIRES re-running ``scripts/verify_vendor_catalog.py`` (CONTRIBUTING gate).
# gemini/dashscope/local reuse the shipping ``EMBEDDING_MODEL_DEFAULTS`` (the
# runtime authority) -- a consistency test pins catalog<->runtime agreement.
# Exactly one model per non-Custom vendor is ``recommended=True``. NO DeepSeek
# (no embeddings API). Qwen3-Embedding is offered ONLY via SiliconFlow's
# OpenAI-compatible endpoint; "Custom" is the free-form escape hatch.
VENDOR_CATALOG: tuple[Vendor, ...] = (
    Vendor(
        key="google",
        label="Google (Gemini)",
        provider="gemini",
        base_url=None,
        requires_key=True,
        key_url="https://aistudio.google.com/apikey",
        key_env=("GEMINI_API_KEY",),
        models=(
            VendorModel("gemini-embedding-001", 768, "default", recommended=True),
        ),
        aliases=("gemini",),
        allow_custom_model=True,
    ),
    Vendor(
        key="dashscope",
        label="Alibaba DashScope (Qwen)",
        provider="dashscope",
        base_url=None,
        requires_key=True,
        key_url="https://bailian.console.aliyun.com/",
        key_env=("DASHSCOPE_API_KEY",),
        models=(
            VendorModel("text-embedding-v4", 1024, "default", recommended=True),
        ),
        aliases=(),
        allow_custom_model=True,
    ),
    Vendor(
        key="local",
        label="Local (offline, no key)",
        provider="local",
        base_url=None,
        requires_key=False,
        key_url="",
        key_env=(),
        models=(
            VendorModel("all-MiniLM-L6-v2", 384, "offline", recommended=True),
        ),
        aliases=(),
        allow_custom_model=False,
    ),
    Vendor(
        key="siliconflow",
        label="SiliconFlow",
        provider="openai-compatible",
        base_url="https://api.siliconflow.cn/v1",
        requires_key=True,
        key_url="https://cloud.siliconflow.cn",
        key_env=("ZOTPILOT_EMBEDDING_API_KEY", "OPENAI_API_KEY"),
        models=(
            VendorModel(
                "BAAI/bge-m3", 1024, "multilingual · cheapest", recommended=True
            ),
            VendorModel("Qwen/Qwen3-Embedding-0.6B", 1024, "fast · low cost · MRL"),
            VendorModel("Qwen/Qwen3-Embedding-8B", 2048, "best quality · MRL"),
        ),
        aliases=(),
        allow_custom_model=True,
    ),
    Vendor(
        key="zhipu",
        label="Zhipu (GLM)",
        provider="openai-compatible",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        requires_key=True,
        key_url="https://open.bigmodel.cn",
        key_env=("ZOTPILOT_EMBEDDING_API_KEY", "OPENAI_API_KEY"),
        models=(
            VendorModel("embedding-3", 2048, "MRL · non-/v1 base", recommended=True),
        ),
        aliases=(),
        allow_custom_model=True,
    ),
    Vendor(
        key="ollama",
        label="Ollama (local, no key)",
        provider="openai-compatible",
        base_url="http://localhost:11434/v1",
        requires_key=False,
        key_url="",
        key_env=(),
        models=(
            VendorModel("nomic-embed-text", 768, "local · free", recommended=True),
        ),
        aliases=(),
        allow_custom_model=True,
    ),
    Vendor(
        key="custom",
        label="Custom (OpenAI-compatible)",
        provider="openai-compatible",
        base_url="",  # user-supplied; the only intentionally-empty fixed base_url
        requires_key=True,
        key_url="",
        key_env=("ZOTPILOT_EMBEDDING_API_KEY", "OPENAI_API_KEY"),
        models=(),  # free-form: model + dimensions entered by the user
        aliases=("openai-compatible",),
        allow_custom_model=True,
    ),
)


def recommended_model(vendor: Vendor) -> VendorModel | None:
    """Return the vendor's pre-selected default model (or None for free-form)."""
    for vm in vendor.models:
        if vm.recommended:
            return vm
    return vendor.models[0] if vendor.models else None


def resolve_vendor(key_or_alias: str | None) -> Vendor | None:
    """Resolve a vendor by canonical key OR alias, case-insensitively.

    Returns ``None`` when nothing matches (the caller decides how to error).
    """
    if not key_or_alias:
        return None
    needle = key_or_alias.strip().lower()
    for vendor in VENDOR_CATALOG:
        if needle == vendor.key.lower():
            return vendor
        if any(needle == alias.lower() for alias in vendor.aliases):
            return vendor
    return None


def vendor_cli_choices() -> list[str]:
    """All accepted ``--provider`` values: canonical keys + aliases.

    Deliberately EXCLUDES ``none`` (the No-RAG sentinel), matching the historical
    argparse choices. Order: each vendor's key followed by its aliases.
    """
    choices: list[str] = []
    for vendor in VENDOR_CATALOG:
        choices.append(vendor.key)
        choices.extend(vendor.aliases)
    return choices


def resolve_setup_choice(
    key_or_alias: str,
    model: str | None = None,
    dims: int | None = None,
    base_url: str | None = None,
) -> tuple[str, str | None, str, int]:
    """Map a (vendor, model?, dims?, base_url?) setup choice to runtime fields.

    The SINGLE vendor->runtime resolver shared by BOTH the interactive wizard
    and the non-interactive CLI (no duplicated mapping logic). Returns
    ``(provider, base_url, model, dims)`` where ``provider`` is a member of
    :data:`EMBEDDING_PROVIDERS` and ``base_url`` is ``None`` for the
    gemini/dashscope/local providers.

    Resolution:

    - ``model`` omitted -> the vendor's ``recommended`` model (and its dims).
    - ``dims`` omitted -> the catalog dims for ``(vendor, model)`` when known.
    - ``base_url`` omitted -> the vendor's fixed ``base_url``.

    Raises ``ValueError`` when a required value cannot be determined: an unknown
    vendor; an openai-compatible vendor with no resolvable ``base_url`` (Custom
    without ``--embedding-base-url``); no model for a free-form vendor; or
    ``dims`` required but absent (Custom, or a model not in the curated list --
    we cannot guess a non-matryoshka model's native dimension).
    """
    vendor = resolve_vendor(key_or_alias)
    if vendor is None:
        raise ValueError(
            f"Unknown vendor/provider {key_or_alias!r}. "
            f"Valid choices: {', '.join(vendor_cli_choices())}."
        )

    resolved_model = (model or "").strip() or None
    catalog_dims: int | None = None
    if resolved_model:
        for vm in vendor.models:
            if vm.model == resolved_model:
                catalog_dims = vm.dimensions
                break
    else:
        rec = recommended_model(vendor)
        if rec is not None:
            resolved_model = rec.model
            catalog_dims = rec.dimensions

    resolved_dims = dims if dims is not None else catalog_dims
    # base_url: an explicit override wins; else the vendor's fixed base_url.
    # vendor.base_url == "" (Custom) is treated as "unset" so the user must give one.
    resolved_base = (base_url or "").strip() or (vendor.base_url or None)

    if vendor.provider == "openai-compatible" and not resolved_base:
        raise ValueError(
            f"--embedding-base-url is required for vendor {vendor.key!r} "
            f"(OpenAI-compatible endpoint root, e.g. http://localhost:11434/v1)."
        )
    if not resolved_model:
        raise ValueError(
            f"--embedding-model is required for vendor {vendor.key!r} "
            f"(free-form vendor has no recommended default)."
        )
    if resolved_dims is None:
        raise ValueError(
            f"--embedding-dimensions is required for vendor {vendor.key!r} with "
            f"model {resolved_model!r}: it is not a curated catalog model, so its "
            f"output dimension cannot be guessed (non-matryoshka servers ignore a "
            f"requested size and return their native dimension). Set it explicitly."
        )
    return vendor.provider, resolved_base, resolved_model, resolved_dims

# Anchored full-match: ``{env:NAME}`` where NAME is a valid env-var identifier.
# Anything that does not match exactly (``{env:}``, ``{env:FOO``, ``{ENV:FOO}``)
# is treated as a plain literal.
_ENV_REF_RE = re.compile(r"^\{env:([A-Za-z_][A-Za-z0-9_]*)\}$")


def _resolve_secret(config_value: str | None, *env_names: str) -> str | None:
    """Resolve a secret/URL through a precedence ladder.

    Ladder: config literal -> ``{env:VAR}`` ref -> ``env_names`` in order -> None.

    - If ``config_value`` is a non-empty string that is NOT a ``{env:NAME}``
      reference, it is returned directly (a configured literal wins).
    - If ``config_value`` is an exact ``{env:NAME}`` reference, ``NAME`` is
      resolved via ``os.environ`` (missing -> never raises ``KeyError``; an
      empty value is treated as unset and the ladder continues).
    - Malformed refs (``{env:}``, ``{env:FOO``, ``{ENV:FOO}``) are treated as
      literal strings, never partially matched.
    - Otherwise each name in ``env_names`` is tried in order; the first
      non-empty value wins.
    - Returns ``None`` when nothing is set.
    """
    if config_value:
        match = _ENV_REF_RE.match(config_value)
        if match:
            resolved = os.environ.get(match.group(1))
            if resolved:
                return resolved
            # missing or empty -> fall through to the env_names ladder
        else:
            # Plain literal (covers malformed ``{env:...}`` strings too).
            return config_value

    for env_name in env_names:
        value = os.environ.get(env_name)
        if value:
            return value

    return None

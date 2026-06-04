"""Single source of truth for the embedding-provider registry.

This module is intentionally a LEAF: it imports ONLY the standard library
(``os``/``re``/``typing``) so that heavier modules such as :mod:`zotpilot.config`,
:mod:`zotpilot.embeddings`, and :mod:`zotpilot.indexer` can depend on it without
risking an import cycle.

It centralizes the embedding provider allow-list, the per-provider model and
dimension defaults, the (wizard-only) vendor preset catalog, and the shared
``{env:VAR}`` secret/URL resolution helper.

Vision provider allow-lists are deliberately NOT centralized here -- they stay
inline at their existing call sites (see the plan, Decision 6).
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


class VendorPreset(NamedTuple):
    """A wizard-only pre-fill for a known OpenAI-compatible embedding vendor.

    Presets are a thin, OPTIONAL data layer that ONLY pre-fills the setup
    wizard. The generic ``openai-compatible`` provider remains the only runtime
    code path; presets never appear at runtime. Values are best-effort and
    drift-tolerant (a stale preset degrades to "user overrides the wrong
    default", not a crash); ``Custom`` is always a fallback.
    """

    name: str
    base_url: str
    embedding_model: str
    embedding_dimensions: int
    key_url: str
    requires_key: bool


# Seed presets, verified against vendor docs at planning time (2026-06).
# NO DeepSeek (no embeddings API). NO Qwen preset (stays on dedicated
# ``dashscope`` provider; reachable via SiliconFlow by overriding the model).
EMBEDDING_PRESETS: list[VendorPreset] = [
    VendorPreset(
        name="SiliconFlow",
        base_url="https://api.siliconflow.cn/v1",
        embedding_model="BAAI/bge-m3",
        embedding_dimensions=1024,
        key_url="https://cloud.siliconflow.cn",
        requires_key=True,
    ),
    VendorPreset(
        name="Zhipu/GLM",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        embedding_model="embedding-3",
        embedding_dimensions=2048,
        key_url="https://open.bigmodel.cn",
        requires_key=True,
    ),
    VendorPreset(
        name="Ollama (local)",
        base_url="http://localhost:11434/v1",
        embedding_model="nomic-embed-text",
        embedding_dimensions=768,
        key_url="",
        requires_key=False,
    ),
    VendorPreset(
        name="Custom",
        base_url="",
        embedding_model="",
        embedding_dimensions=0,
        key_url="",
        requires_key=True,
    ),
]

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

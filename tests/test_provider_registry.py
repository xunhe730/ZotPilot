"""Characterization tests: lock current embedding behavior before/after refactor.

These tests encode invariants that MUST hold both on the pre-change ``dev`` HEAD
and after the openai-compatible-provider feature lands. They guard against
behavior drift while the embedding allow-list is centralized into
``providers.py`` (Principle 5) and ``_config_hash`` is relocated to ``config.py``.

The ``_config_hash`` golden hex baselines were captured from ``dev`` HEAD BEFORE
any code change (M5); they prove existing users get zero forced reindex.
"""
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from zotpilot import providers
from zotpilot.config import Config
from zotpilot.embeddings import create_embedder
from zotpilot.embeddings.dashscope import DashScopeEmbedder
from zotpilot.embeddings.gemini import GeminiEmbedder
from zotpilot.embeddings.local import LocalEmbedder
from zotpilot.indexer import _config_hash

# Existing providers and their (model, dimensions) defaults -- frozen snapshot
# of config.py's historical inline dict. These four MUST never drift.
EXISTING_DEFAULTS = {
    "gemini": ("gemini-embedding-001", 768),
    "dashscope": ("text-embedding-v4", 1024),
    "local": ("all-MiniLM-L6-v2", 384),
    "none": ("none", 0),
}


def _load_config(**overrides) -> Config:
    """Load a Config from a temp json, exercising the real load() defaults."""
    data = {"zotero_data_dir": "/tmp/zot", "chroma_db_path": "/tmp/chroma"}
    data.update(overrides)
    fd, path = tempfile.mkstemp(suffix=".json")
    Path(path).write_text(json.dumps(data), encoding="utf-8")
    try:
        return Config.load(path)
    finally:
        Path(path).unlink()


class TestEmbeddingDefaults:
    @pytest.mark.parametrize("provider,expected", EXISTING_DEFAULTS.items())
    def test_existing_provider_defaults_unchanged(self, provider, expected):
        cfg = _load_config(embedding_provider=provider)
        assert (cfg.embedding_model, cfg.embedding_dimensions) == expected

    def test_registry_reproduces_existing_defaults(self):
        for provider, expected in EXISTING_DEFAULTS.items():
            assert providers.EMBEDDING_MODEL_DEFAULTS[provider] == expected

    def test_openai_compatible_default_is_sentinel(self):
        # Sentinel meaning "user must specify model + dimensions".
        assert providers.EMBEDDING_MODEL_DEFAULTS["openai-compatible"] == ("", 0)


class TestEmbeddingAllowList:
    def test_existing_providers_all_present(self):
        for provider in EXISTING_DEFAULTS:
            assert provider in providers.EMBEDDING_PROVIDERS

    def test_allow_list_is_existing_four_plus_openai_compatible(self):
        # The ONLY delta vs the historical {gemini,dashscope,local,none} allow-list
        # is the documented addition of openai-compatible.
        assert set(providers.EMBEDDING_PROVIDERS) == set(EXISTING_DEFAULTS) | {
            "openai-compatible"
        }


class TestCreateEmbedderBehavior:
    def test_local_returns_local_embedder(self):
        config = MagicMock()
        config.embedding_provider = "local"
        assert isinstance(create_embedder(config), LocalEmbedder)

    def test_gemini_returns_gemini_embedder(self):
        config = MagicMock()
        config.embedding_provider = "gemini"
        config.embedding_model = "gemini-embedding-001"
        config.embedding_dimensions = 768
        config.gemini_api_key = "test-key"
        config.embedding_timeout = 120.0
        config.embedding_max_retries = 3
        with patch("google.genai.Client"):
            assert isinstance(create_embedder(config), GeminiEmbedder)

    def test_dashscope_returns_dashscope_embedder(self):
        config = MagicMock()
        config.embedding_provider = "dashscope"
        config.embedding_model = "text-embedding-v4"
        config.embedding_dimensions = 1024
        config.dashscope_api_key = "test-key"
        config.dashscope_embedding_endpoint = "compatible"
        config.embedding_timeout = 120.0
        config.embedding_max_retries = 3
        assert isinstance(create_embedder(config), DashScopeEmbedder)

    def test_none_returns_none(self):
        config = MagicMock()
        config.embedding_provider = "none"
        assert create_embedder(config) is None

    def test_unknown_provider_raises_value_error(self):
        config = MagicMock()
        config.embedding_provider = "bogus-provider"
        with pytest.raises(ValueError, match="Invalid embedding_provider"):
            create_embedder(config)


class TestVisionModelDefaultSwap:
    """Proves the vision_model default-swap (config.py:144-154) is untouched."""

    def test_dashscope_no_explicit_model_defaults_to_qwen(self):
        cfg = _load_config(vision_provider="dashscope")
        assert cfg.vision_model == "qwen3-vl-flash"

    def test_dashscope_with_anthropic_model_rewritten_to_qwen(self):
        cfg = _load_config(
            vision_provider="dashscope", vision_model="claude-haiku-4-5-20251001"
        )
        assert cfg.vision_model == "qwen3-vl-flash"

    def test_anthropic_with_qwen_model_rewritten_to_claude(self):
        cfg = _load_config(vision_provider="anthropic", vision_model="qwen3-vl-flash")
        assert cfg.vision_model == "claude-haiku-4-5-20251001"

    def test_anthropic_no_explicit_model_defaults_to_claude(self):
        cfg = _load_config(vision_provider="anthropic")
        assert cfg.vision_model == "claude-haiku-4-5-20251001"


class TestResolveSecret:
    """Precedence ladder + malformed/empty handling for providers._resolve_secret."""

    def test_config_literal_wins(self, monkeypatch):
        monkeypatch.setenv("ZP_KEY", "from-env")
        assert providers._resolve_secret("literal-value", "ZP_KEY") == "literal-value"

    def test_env_ref_resolves(self, monkeypatch):
        monkeypatch.setenv("MY_VAR", "resolved")
        assert providers._resolve_secret("{env:MY_VAR}", "FALLBACK") == "resolved"

    def test_env_ref_missing_falls_through_ladder(self, monkeypatch):
        monkeypatch.delenv("MISSING_VAR", raising=False)
        monkeypatch.setenv("FALLBACK", "fallback-value")
        assert providers._resolve_secret("{env:MISSING_VAR}", "FALLBACK") == "fallback-value"

    def test_env_ref_missing_returns_none_when_no_fallback(self, monkeypatch):
        monkeypatch.delenv("MISSING_VAR", raising=False)
        assert providers._resolve_secret("{env:MISSING_VAR}") is None  # never KeyError

    def test_env_names_first_match_wins(self, monkeypatch):
        monkeypatch.delenv("FIRST", raising=False)
        monkeypatch.setenv("SECOND", "second-value")
        assert providers._resolve_secret(None, "FIRST", "SECOND") == "second-value"

    def test_returns_none_when_nothing_set(self, monkeypatch):
        monkeypatch.delenv("A", raising=False)
        monkeypatch.delenv("B", raising=False)
        assert providers._resolve_secret(None, "A", "B") is None

    @pytest.mark.parametrize("malformed", ["{env:}", "{env:FOO", "{ENV:FOO}", "{env: FOO}"])
    def test_malformed_refs_treated_as_literal(self, malformed):
        assert providers._resolve_secret(malformed, "ANY") == malformed

    def test_empty_env_value_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("EMPTY_VAR", "")
        monkeypatch.setenv("FALLBACK", "fallback-value")
        assert providers._resolve_secret("{env:EMPTY_VAR}", "FALLBACK") == "fallback-value"


class TestVendorPresets:
    def test_contains_expected_vendors(self):
        names = {p.name for p in providers.EMBEDDING_PRESETS}
        assert "SiliconFlow" in names
        assert "Zhipu/GLM" in names
        assert "Custom" in names
        assert any("Ollama" in n for n in names)

    def test_excludes_chat_only_vendors(self):
        names = " ".join(p.name for p in providers.EMBEDDING_PRESETS).lower()
        assert "deepseek" not in names
        assert "qwen" not in names

    def test_ollama_requires_no_key(self):
        ollama = next(p for p in providers.EMBEDDING_PRESETS if "Ollama" in p.name)
        assert ollama.requires_key is False

    def test_non_custom_presets_have_base_url(self):
        for preset in providers.EMBEDDING_PRESETS:
            if preset.name != "Custom":
                assert preset.base_url


class TestConfigHashGoldenBaselines:
    """M5: hard-coded _config_hash baselines captured from dev HEAD before changes.

    Any drift here means existing users would be forced to reindex on upgrade --
    a backward-compat break. These MUST stay green.
    """

    GOLDEN = {
        ("gemini", "anthropic"): "7f4f892bc3358f00",
        ("gemini", "dashscope"): "c7b58e5abac6e62f",
        ("dashscope", "anthropic"): "e60a8304db40ee45",
        ("dashscope", "dashscope"): "ea9dd3839160f395",
        ("local", "anthropic"): "d581597dacca8926",
        ("local", "dashscope"): "28f870ba5e7d073b",
        ("none", "anthropic"): "f4dd55e6a07e5819",
        ("none", "dashscope"): "8ada215c37baba66",
    }

    @pytest.mark.parametrize("combo,expected", GOLDEN.items())
    def test_config_hash_matches_golden(self, combo, expected):
        embedding_provider, vision_provider = combo
        cfg = _load_config(
            embedding_provider=embedding_provider, vision_provider=vision_provider
        )
        assert _config_hash(cfg) == expected

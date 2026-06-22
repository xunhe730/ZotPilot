"""Tests for Config.chunker_backend field and conditional config-hash behavior."""

import json
import types

import pytest

from zotpilot.config import Config, _config_hash


def _load_config(tmp_path, **json_fields):
    """Write a minimal config JSON and load it, passing extra json_fields."""
    data = {}
    data.update(json_fields)
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(data))
    return Config.load(path=config_file)


# ---------------------------------------------------------------------------
# Test 1: chunker_backend defaults to "char"
# ---------------------------------------------------------------------------

def test_chunker_backend_defaults_to_char(tmp_path, monkeypatch):
    for key in ("GEMINI_API_KEY", "DASHSCOPE_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ZOTPILOT_SECRET_BACKEND", "local-file")
    monkeypatch.setenv("ZOTPILOT_LOCAL_SECRETS_PATH", str(tmp_path / "secrets.json"))
    cfg = Config.load(path=tmp_path / "nonexistent.json")
    assert cfg.chunker_backend == "char"


# ---------------------------------------------------------------------------
# Test 2 (strengthened): char backend must NOT extend the hash string.
#
# Strategy: build a types.SimpleNamespace with exactly the same index-affecting
# attributes that _config_hash reads, but WITHOUT a chunker_backend attribute.
# Because _config_hash uses getattr(config, "chunker_backend", "char"), the
# missing-attr path must produce the identical hash as an explicit "char" config.
#
# We enumerate the attributes from _config_hash's body:
#   chunk_size, chunk_overlap, embedding_provider, dashscope_embedding_endpoint,
#   embedding_dimensions, embedding_model, ocr_language,
#   vision_enabled, vision_provider, vision_model
# (embedding_base_url is only appended for openai-compatible; we use "gemini")
# ---------------------------------------------------------------------------

def test_char_backend_hash_unchanged_vs_no_field(tmp_path, monkeypatch):
    monkeypatch.setenv("ZOTPILOT_SECRET_BACKEND", "local-file")
    monkeypatch.setenv("ZOTPILOT_LOCAL_SECRETS_PATH", str(tmp_path / "secrets.json"))
    char_cfg = Config.load(path=tmp_path / "nonexistent.json")
    assert char_cfg.chunker_backend == "char"  # sanity-check

    # Build a SimpleNamespace with no chunker_backend attribute so getattr falls
    # back to the default "char". Must carry all fields _config_hash reads.
    ns = types.SimpleNamespace(
        chunk_size=char_cfg.chunk_size,
        chunk_overlap=char_cfg.chunk_overlap,
        embedding_provider=char_cfg.embedding_provider,
        dashscope_embedding_endpoint=char_cfg.dashscope_embedding_endpoint,
        embedding_dimensions=char_cfg.embedding_dimensions,
        embedding_model=char_cfg.embedding_model,
        ocr_language=char_cfg.ocr_language,
        vision_enabled=char_cfg.vision_enabled,
        vision_provider=char_cfg.vision_provider,
        vision_model=char_cfg.vision_model,
        # NOTE: no chunker_backend attribute — tests the getattr default path
    )
    assert not hasattr(ns, "chunker_backend")

    # The missing-attr path must hash identically to explicit "char"
    assert _config_hash(ns) == _config_hash(char_cfg)


# ---------------------------------------------------------------------------
# Test 3: non-char backend changes the hash
# ---------------------------------------------------------------------------

def test_llamaindex_backend_changes_hash(tmp_path, monkeypatch):
    monkeypatch.setenv("ZOTPILOT_SECRET_BACKEND", "local-file")
    monkeypatch.setenv("ZOTPILOT_LOCAL_SECRETS_PATH", str(tmp_path / "secrets.json"))
    char_cfg = Config.load(path=tmp_path / "char_cfg.json")
    # Write a config with chunker_backend="llamaindex"
    li_file = tmp_path / "li_cfg.json"
    li_file.write_text(json.dumps({"chunker_backend": "llamaindex"}))
    li_cfg = Config.load(path=li_file)

    assert li_cfg.chunker_backend == "llamaindex"
    assert _config_hash(char_cfg) != _config_hash(li_cfg)

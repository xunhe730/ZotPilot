"""Tests for shared config loading/saving and runtime resolution."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from zotpilot.config import Config
from zotpilot.runtime_settings import resolve_runtime_settings
from zotpilot.secret_store import set_secret


def _use_local_secrets(monkeypatch, tmp_path: Path) -> None:
    for key in (
        "GEMINI_API_KEY",
        "DASHSCOPE_API_KEY",
        "ANTHROPIC_API_KEY",
        "ZOTERO_API_KEY",
        "ZOTERO_USER_ID",
        "OPENALEX_EMAIL",
        "S2_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ZOTPILOT_SECRET_BACKEND", "local-file")
    monkeypatch.setenv("ZOTPILOT_LOCAL_SECRETS_PATH", str(tmp_path / "secrets.json"))


class TestConfigLoadDefaults:
    def test_load_defaults(self, tmp_path, monkeypatch):
        _use_local_secrets(monkeypatch, tmp_path)
        cfg = Config.load(path=tmp_path / "nonexistent" / "config.json")
        assert cfg.zotero_data_dir == Path("~/Zotero").expanduser()
        assert cfg.embedding_provider == "gemini"
        assert cfg.dashscope_embedding_endpoint == "compatible"
        assert cfg.vision_provider == "anthropic"
        assert cfg.vision_model == "claude-haiku-4-5-20251001"
        assert cfg.gemini_api_key is None
        assert cfg.zotero_api_key is None
        assert cfg.zotero_user_id is None
        assert cfg.formula_ocr_enabled is False
        assert cfg.formula_ocr_provider == "local"
        assert cfg.formula_ocr_max_formulas_per_doc == 40
        assert cfg.formula_ocr_max_formulas_per_page == 6
        assert cfg.formula_ocr_min_confidence == 0.6


class TestConfigLoadFromFile:
    def test_load_shared_fields_from_file(self, tmp_path, monkeypatch):
        _use_local_secrets(monkeypatch, tmp_path)
        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps(
                {
                    "zotero_data_dir": str(tmp_path / "MyZotero"),
                    "embedding_model": "custom-model",
                    "embedding_provider": "local",
                    "zotero_user_id": "12345",
                    "openalex_email": "user@example.com",
                }
            )
        )
        cfg = Config.load(path=config_file)
        assert cfg.zotero_data_dir == tmp_path / "MyZotero"
        assert cfg.embedding_model == "custom-model"
        assert cfg.embedding_provider == "local"
        assert cfg.dashscope_embedding_endpoint == "compatible"
        assert cfg.zotero_user_id == "12345"
        assert cfg.openalex_email == "user@example.com"
        assert cfg.gemini_api_key is None

    def test_dashscope_vision_provider_gets_qwen_default_model(self, tmp_path, monkeypatch):
        _use_local_secrets(monkeypatch, tmp_path)
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"embedding_provider": "local", "vision_provider": "dashscope"}))

        cfg = Config.load(path=config_file)

        assert cfg.vision_provider == "dashscope"
        assert cfg.vision_model == "qwen3-vl-flash"

    def test_dashscope_vision_provider_replaces_stale_anthropic_default_model(self, tmp_path, monkeypatch):
        _use_local_secrets(monkeypatch, tmp_path)
        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps({
                "embedding_provider": "local",
                "vision_provider": "dashscope",
                "vision_model": "claude-haiku-4-5-20251001",
            })
        )

        cfg = Config.load(path=config_file)

        assert cfg.vision_provider == "dashscope"
        assert cfg.vision_model == "qwen3-vl-flash"

    def test_loads_dashscope_native_embedding_endpoint(self, tmp_path, monkeypatch):
        _use_local_secrets(monkeypatch, tmp_path)
        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps({
                "embedding_provider": "dashscope",
                "dashscope_embedding_endpoint": "native",
            })
        )

        cfg = Config.load(path=config_file)

        assert cfg.dashscope_embedding_endpoint == "native"


class TestRuntimeResolution:
    def test_runtime_uses_legacy_secret_backend_fallback(self, tmp_path, monkeypatch):
        _use_local_secrets(monkeypatch, tmp_path)
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"zotero_user_id": "11111111"}))
        set_secret("gemini_api_key", "stored-gemini")
        set_secret("zotero_api_key", "stored-zotero")

        resolved = resolve_runtime_settings(config_file)

        assert resolved.config.gemini_api_key == "stored-gemini"
        assert resolved.config.zotero_api_key == "stored-zotero"
        assert resolved.config.zotero_user_id == "11111111"
        assert resolved.sources["gemini_api_key"] == "legacy-local-file"

    def test_config_secret_overrides_legacy_secret_backend(self, tmp_path, monkeypatch):
        _use_local_secrets(monkeypatch, tmp_path)
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"gemini_api_key": "config-gemini"}))
        set_secret("gemini_api_key", "stored-gemini")

        resolved = resolve_runtime_settings(config_file)

        assert resolved.config.gemini_api_key == "config-gemini"
        assert resolved.sources["gemini_api_key"] == "config"

    def test_env_overrides_secure_store(self, tmp_path, monkeypatch):
        _use_local_secrets(monkeypatch, tmp_path)
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"zotero_user_id": "11111111"}))
        set_secret("gemini_api_key", "stored-gemini")
        monkeypatch.setenv("GEMINI_API_KEY", "env-gemini")
        monkeypatch.setenv("ZOTERO_USER_ID", "99999999")

        resolved = resolve_runtime_settings(config_file)

        assert resolved.config.gemini_api_key == "env-gemini"
        assert resolved.config.zotero_user_id == "99999999"
        assert resolved.sources["gemini_api_key"] == "env-override"
        assert resolved.sources["zotero_user_id"] == "env-override"

    def test_gemini_base_url_env_override(self, tmp_path, monkeypatch):
        _use_local_secrets(monkeypatch, tmp_path)
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"gemini_base_url": "https://config.example.com"}))
        monkeypatch.setenv("GEMINI_BASE_URL", "https://env.example.com")

        resolved = resolve_runtime_settings(config_file)

        assert resolved.config.gemini_base_url == "https://env.example.com"
        assert resolved.sources["gemini_base_url"] == "env-override"

    def test_runtime_loads_config_secrets(self, tmp_path, monkeypatch):
        _use_local_secrets(monkeypatch, tmp_path)
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"gemini_api_key": "legacy-gemini"}))

        cfg = Config.load(path=config_file)
        resolved = resolve_runtime_settings(config_file)

        assert cfg.gemini_api_key == "legacy-gemini"
        assert resolved.config.gemini_api_key == "legacy-gemini"
        assert resolved.sources["gemini_api_key"] == "config"
        assert resolved.legacy_sources["gemini_api_key"] == "legacy-gemini"


class TestConfigSave:
    def test_save_persists_api_keys(self, tmp_path, monkeypatch):
        _use_local_secrets(monkeypatch, tmp_path)
        cfg = Config.load(path=tmp_path / "nonexistent.json")
        cfg.gemini_api_key = "secret-gemini"
        cfg.zotero_api_key = "secret-zotero"
        cfg.zotero_user_id = "12345"
        save_path = tmp_path / "saved_config.json"
        cfg.save(path=save_path)

        saved_data = json.loads(save_path.read_text())
        assert saved_data["gemini_api_key"] == "secret-gemini"
        assert saved_data["zotero_api_key"] == "secret-zotero"
        assert saved_data["zotero_user_id"] == "12345"

    def test_save_load_round_trips_gemini_base_url(self, tmp_path, monkeypatch):
        _use_local_secrets(monkeypatch, tmp_path)
        cfg = Config.load(path=tmp_path / "nonexistent.json")
        cfg.gemini_base_url = "https://proxy.example.com"
        save_path = tmp_path / "saved_config.json"
        cfg.save(path=save_path)

        assert json.loads(save_path.read_text())["gemini_base_url"] == "https://proxy.example.com"
        assert Config.load(path=save_path).gemini_base_url == "https://proxy.example.com"

    def test_save_file_permissions(self, tmp_path, monkeypatch):
        _use_local_secrets(monkeypatch, tmp_path)
        cfg = Config.load(path=tmp_path / "nonexistent.json")
        save_path = tmp_path / "saved_config.json"
        cfg.save(path=save_path)
        file_mode = stat.S_IMODE(save_path.stat().st_mode)
        assert file_mode == 0o600


class TestConfigValidation:
    def test_validate_missing_zotero_dir(self, tmp_path, monkeypatch):
        _use_local_secrets(monkeypatch, tmp_path)
        cfg = Config.load(path=tmp_path / "nonexistent.json")
        cfg.zotero_data_dir = tmp_path / "missing"
        cfg.gemini_api_key = "set"
        errors = cfg.validate()
        assert any("Zotero data dir not found" in e for e in errors)

    def test_validate_missing_api_key(self, tmp_path, monkeypatch):
        _use_local_secrets(monkeypatch, tmp_path)
        zotero_dir = tmp_path / "Zotero"
        zotero_dir.mkdir()
        (zotero_dir / "zotero.sqlite").touch()
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"zotero_data_dir": str(zotero_dir)}))

        cfg = Config.load(path=config_file)
        errors = cfg.validate()
        assert any("GEMINI_API_KEY not set" in e for e in errors)

    def test_validate_dashscope_vision_requires_dashscope_key(self, tmp_path, monkeypatch):
        _use_local_secrets(monkeypatch, tmp_path)
        zotero_dir = tmp_path / "Zotero"
        zotero_dir.mkdir()
        (zotero_dir / "zotero.sqlite").touch()
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "zotero_data_dir": str(zotero_dir),
            "embedding_provider": "local",
            "vision_enabled": True,
            "vision_provider": "dashscope",
        }))

        cfg = Config.load(path=config_file)
        errors = cfg.validate()

        assert any("DASHSCOPE_API_KEY not set" in e for e in errors)

    def test_validate_invalid_vision_provider(self, tmp_path, monkeypatch):
        _use_local_secrets(monkeypatch, tmp_path)
        cfg = Config.load(path=tmp_path / "nonexistent.json")
        cfg.zotero_data_dir = tmp_path
        (tmp_path / "zotero.sqlite").touch()
        cfg.gemini_api_key = "set"
        cfg.vision_provider = "openai"

        errors = cfg.validate()

        assert any("Invalid vision_provider" in e for e in errors)

    def test_validate_vision_model_provider_mismatch(self, tmp_path, monkeypatch):
        _use_local_secrets(monkeypatch, tmp_path)
        cfg = Config.load(path=tmp_path / "nonexistent.json")
        cfg.zotero_data_dir = tmp_path
        (tmp_path / "zotero.sqlite").touch()
        cfg.embedding_provider = "local"
        cfg.vision_provider = "dashscope"
        cfg.vision_model = "claude-haiku-4-5-20251001"
        cfg.dashscope_api_key = "dashscope"

        errors = cfg.validate()

        assert any("Invalid vision_model" in e for e in errors)

    def test_validate_invalid_dashscope_embedding_endpoint(self, tmp_path, monkeypatch):
        _use_local_secrets(monkeypatch, tmp_path)
        cfg = Config.load(path=tmp_path / "nonexistent.json")
        cfg.zotero_data_dir = tmp_path
        (tmp_path / "zotero.sqlite").touch()
        cfg.gemini_api_key = "set"
        cfg.dashscope_embedding_endpoint = "invalid"

        errors = cfg.validate()

        assert any("Invalid dashscope_embedding_endpoint" in e for e in errors)


class TestOpenAICompatConfigSchema:
    """Phase 2: openai-compatible Config fields, back-compat, validation."""

    def test_old_config_loads_without_new_fields(self, tmp_path, monkeypatch):
        _use_local_secrets(monkeypatch, tmp_path)
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"embedding_provider": "local"}))
        cfg = Config.load(path=config_file)
        assert cfg.embedding_base_url is None
        assert cfg.embedding_api_key is None

    def test_new_fields_round_trip(self, tmp_path, monkeypatch):
        _use_local_secrets(monkeypatch, tmp_path)
        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps({
                "embedding_provider": "openai-compatible",
                "embedding_model": "BAAI/bge-m3",
                "embedding_dimensions": 1024,
                "embedding_base_url": "https://api.siliconflow.cn/v1",
                "embedding_api_key": "sk-secret",
            })
        )
        cfg = Config.load(path=config_file)
        assert cfg.embedding_base_url == "https://api.siliconflow.cn/v1"
        assert cfg.embedding_api_key == "sk-secret"

        out = tmp_path / "saved.json"
        cfg.save(path=out)
        saved = json.loads(out.read_text())
        assert saved["embedding_base_url"] == "https://api.siliconflow.cn/v1"
        assert saved["embedding_api_key"] == "sk-secret"
        reloaded = Config.load(path=out)
        assert reloaded.embedding_base_url == cfg.embedding_base_url
        assert reloaded.embedding_api_key == cfg.embedding_api_key

    def test_save_omits_none_new_fields(self, tmp_path, monkeypatch):
        _use_local_secrets(monkeypatch, tmp_path)
        cfg = Config.load(path=tmp_path / "nonexistent.json")
        out = tmp_path / "saved.json"
        cfg.save(path=out)
        saved = json.loads(out.read_text())
        assert "embedding_base_url" not in saved
        assert "embedding_api_key" not in saved

    def _oai_cfg(self, tmp_path, monkeypatch, **overrides):
        _use_local_secrets(monkeypatch, tmp_path)
        for env in ("ZOTPILOT_EMBEDDING_BASE_URL", "OPENAI_BASE_URL",
                    "ZOTPILOT_EMBEDDING_API_KEY", "OPENAI_API_KEY"):
            monkeypatch.delenv(env, raising=False)
        cfg = Config.load(path=tmp_path / "nonexistent.json")
        cfg.zotero_data_dir = tmp_path
        (tmp_path / "zotero.sqlite").touch()
        cfg.embedding_provider = "openai-compatible"
        cfg.embedding_model = "m"
        cfg.embedding_dimensions = 1024
        cfg.embedding_base_url = "https://api.example.com/v1"
        for key, value in overrides.items():
            setattr(cfg, key, value)
        return cfg

    def test_validate_accepts_complete_openai_compatible(self, tmp_path, monkeypatch):
        cfg = self._oai_cfg(tmp_path, monkeypatch)
        assert cfg.validate() == []

    def test_validate_rejects_dims_zero(self, tmp_path, monkeypatch):
        cfg = self._oai_cfg(tmp_path, monkeypatch, embedding_dimensions=0)
        assert any("embedding_dimensions must be > 0" in e for e in cfg.validate())

    def test_validate_rejects_empty_model(self, tmp_path, monkeypatch):
        cfg = self._oai_cfg(tmp_path, monkeypatch, embedding_model="")
        assert any("embedding_model not set" in e for e in cfg.validate())

    def test_validate_rejects_missing_base_url(self, tmp_path, monkeypatch):
        cfg = self._oai_cfg(tmp_path, monkeypatch, embedding_base_url=None)
        assert any("embedding_base_url not set" in e for e in cfg.validate())

    def test_validate_rejects_userinfo_in_base_url(self, tmp_path, monkeypatch):
        cfg = self._oai_cfg(tmp_path, monkeypatch, embedding_base_url="http://user:pass@host/v1")
        assert any("embedded credentials" in e for e in cfg.validate())

    def test_validate_rejects_non_http_scheme(self, tmp_path, monkeypatch):
        cfg = self._oai_cfg(tmp_path, monkeypatch, embedding_base_url="ftp://host/v1")
        assert any("scheme" in e for e in cfg.validate())

    def test_validate_accepts_glm_non_v1_base_url(self, tmp_path, monkeypatch):
        cfg = self._oai_cfg(
            tmp_path, monkeypatch,
            embedding_model="embedding-3", embedding_dimensions=2048,
            embedding_base_url="https://open.bigmodel.cn/api/paas/v4",
        )
        assert cfg.validate() == []

    def test_validate_accepts_base_url_from_env(self, tmp_path, monkeypatch):
        cfg = self._oai_cfg(tmp_path, monkeypatch, embedding_base_url=None)
        monkeypatch.setenv("ZOTPILOT_EMBEDDING_BASE_URL", "https://env.example/v1")
        assert cfg.validate() == []

    def test_validate_invalid_provider_message_lists_providers(self, tmp_path, monkeypatch):
        cfg = self._oai_cfg(tmp_path, monkeypatch, embedding_provider="bogus")
        errors = cfg.validate()
        assert any("'openai-compatible'" in e for e in errors)

    def test_vision_allow_list_still_inline(self, tmp_path, monkeypatch):
        # Vision allow-list is NOT centralized -- it stays ("anthropic", "dashscope").
        cfg = self._oai_cfg(tmp_path, monkeypatch)
        cfg.vision_provider = "openai-compatible"  # not a valid vision provider
        assert any("Invalid vision_provider" in e for e in cfg.validate())

    def test_validate_formula_provider_uses_registry(self, tmp_path, monkeypatch):
        cfg = self._oai_cfg(tmp_path, monkeypatch, formula_ocr_provider="bogus")

        errors = cfg.validate()

        assert any("Invalid formula_ocr_provider" in e and "'local'" in e for e in errors)

    def test_validate_formula_limits(self, tmp_path, monkeypatch):
        cfg = self._oai_cfg(
            tmp_path,
            monkeypatch,
            formula_ocr_max_formulas_per_doc=-1,
            formula_ocr_max_formulas_per_page=-1,
            formula_ocr_min_confidence=1.5,
        )

        errors = cfg.validate()

        assert any("formula_ocr_max_formulas_per_doc" in e for e in errors)
        assert any("formula_ocr_max_formulas_per_page" in e for e in errors)
        assert any("formula_ocr_min_confidence" in e for e in errors)


class TestConfigHashOpenAICompat:
    """Step 5.8: golden baselines + conditional base_url folding."""

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

    def test_existing_combos_match_golden(self, tmp_path, monkeypatch):
        from zotpilot.config import _config_hash
        _use_local_secrets(monkeypatch, tmp_path)
        for (ep, vp), expected in self.GOLDEN.items():
            config_file = tmp_path / f"cfg_{ep}_{vp}.json"
            config_file.write_text(json.dumps({"embedding_provider": ep, "vision_provider": vp}))
            cfg = Config.load(path=config_file)
            assert _config_hash(cfg) == expected, f"{ep}+{vp} drifted"

    def test_base_url_folded_for_openai_compatible(self, tmp_path, monkeypatch):
        import dataclasses

        from zotpilot.config import _config_hash
        _use_local_secrets(monkeypatch, tmp_path)
        config_file = tmp_path / "cfg.json"
        config_file.write_text(
            json.dumps({
                "embedding_provider": "openai-compatible",
                "embedding_model": "m",
                "embedding_dimensions": 1024,
                "embedding_base_url": "https://a.example/v1",
            })
        )
        cfg = Config.load(path=config_file)
        other = dataclasses.replace(cfg, embedding_base_url="https://b.example/v1")
        assert _config_hash(cfg) != _config_hash(other)

    def test_api_key_not_folded(self, tmp_path, monkeypatch):
        import dataclasses

        from zotpilot.config import _config_hash
        _use_local_secrets(monkeypatch, tmp_path)
        config_file = tmp_path / "cfg.json"
        config_file.write_text(
            json.dumps({
                "embedding_provider": "openai-compatible",
                "embedding_model": "m",
                "embedding_dimensions": 1024,
                "embedding_base_url": "https://a.example/v1",
            })
        )
        cfg = Config.load(path=config_file)
        rotated = dataclasses.replace(cfg, embedding_api_key="new-key")
        assert _config_hash(cfg) == _config_hash(rotated)

    def test_formula_ocr_settings_not_folded(self, tmp_path, monkeypatch):
        import dataclasses

        from zotpilot.config import _config_hash
        _use_local_secrets(monkeypatch, tmp_path)
        config_file = tmp_path / "cfg.json"
        config_file.write_text(json.dumps({"embedding_provider": "local"}))
        cfg = Config.load(path=config_file)

        changed = dataclasses.replace(
            cfg,
            formula_ocr_enabled=True,
            formula_ocr_max_formulas_per_doc=12,
            formula_ocr_max_formulas_per_page=3,
            formula_ocr_min_confidence=0.8,
        )

        assert _config_hash(cfg) == _config_hash(changed)


class TestResolveSecretSaveRoundTrip:
    """H4: a resolved secret is NEVER written back into config.json by save()."""

    def test_env_placeholder_round_trips_as_literal(self, tmp_path, monkeypatch):
        _use_local_secrets(monkeypatch, tmp_path)
        monkeypatch.setenv("OPENAI_API_KEY", "resolved-secret-value")
        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps({
                "embedding_provider": "openai-compatible",
                "embedding_model": "m",
                "embedding_dimensions": 1024,
                "embedding_base_url": "https://x/v1",
                "embedding_api_key": "{env:OPENAI_API_KEY}",
            })
        )
        cfg = Config.load(path=config_file)
        out = tmp_path / "saved.json"
        cfg.save(path=out)
        saved = json.loads(out.read_text())
        # The placeholder is persisted literally, NOT the resolved secret.
        assert saved["embedding_api_key"] == "{env:OPENAI_API_KEY}"
        assert "resolved-secret-value" not in out.read_text()


class TestGeminiBaseUrlValidation:
    """gemini_base_url https-only validation (#17)."""

    def test_validate_rejects_http_gemini_base_url(self, tmp_path, monkeypatch):
        _use_local_secrets(monkeypatch, tmp_path)
        cfg = Config.load(path=tmp_path / "nonexistent.json")
        cfg.zotero_data_dir = tmp_path
        (tmp_path / "zotero.sqlite").touch()
        cfg.gemini_api_key = "set"
        cfg.gemini_base_url = "http://insecure-proxy.example.com"

        errors = cfg.validate()

        assert any("gemini_base_url must use https" in e for e in errors)

    def test_validate_accepts_https_gemini_base_url(self, tmp_path, monkeypatch):
        _use_local_secrets(monkeypatch, tmp_path)
        cfg = Config.load(path=tmp_path / "nonexistent.json")
        cfg.zotero_data_dir = tmp_path
        (tmp_path / "zotero.sqlite").touch()
        cfg.gemini_api_key = "set"
        cfg.gemini_base_url = "https://proxy.example.com"

        errors = cfg.validate()

        assert not any("gemini_base_url" in e for e in errors)

    def test_validate_allows_unset_gemini_base_url(self, tmp_path, monkeypatch):
        _use_local_secrets(monkeypatch, tmp_path)
        cfg = Config.load(path=tmp_path / "nonexistent.json")
        cfg.zotero_data_dir = tmp_path
        (tmp_path / "zotero.sqlite").touch()
        cfg.gemini_api_key = "set"

        errors = cfg.validate()

        assert not any("gemini_base_url" in e for e in errors)

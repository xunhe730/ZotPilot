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
        assert cfg.gemini_api_key is None
        assert cfg.zotero_api_key is None
        assert cfg.zotero_user_id is None


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
        assert cfg.zotero_user_id == "12345"
        assert cfg.openalex_email == "user@example.com"
        assert cfg.gemini_api_key is None


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

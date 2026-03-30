"""Tests for configuration loading, saving, migration, and validation."""
import json
import stat
from pathlib import Path

from zotpilot.config import Config


class TestConfigLoadDefaults:
    def test_load_defaults(self, tmp_path, monkeypatch):
        """Config.load() with no file and no env vars gives correct defaults."""
        # Point to a non-existent config file
        config_path = tmp_path / "nonexistent" / "config.json"

        # Clear relevant env vars
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
        monkeypatch.delenv("ZOTERO_USER_ID", raising=False)
        monkeypatch.delenv("OPENALEX_EMAIL", raising=False)

        cfg = Config.load(path=config_path)

        assert cfg.zotero_data_dir == Path("~/Zotero").expanduser()
        assert cfg.chroma_db_path == Path("~/.local/share/zotpilot/chroma").expanduser()
        assert cfg.embedding_model == "gemini-embedding-001"
        assert cfg.embedding_dimensions == 768
        assert cfg.chunk_size == 400
        assert cfg.chunk_overlap == 100
        assert cfg.gemini_api_key is None
        assert cfg.embedding_provider == "gemini"
        assert cfg.embedding_timeout == 120.0
        assert cfg.embedding_max_retries == 3
        assert cfg.rerank_alpha == 0.7
        assert cfg.rerank_section_weights is None
        assert cfg.rerank_journal_weights is None
        assert cfg.rerank_enabled is True
        assert cfg.oversample_multiplier == 3
        assert cfg.oversample_topic_factor == 5
        assert cfg.stats_sample_limit == 10000
        assert cfg.ocr_language == "eng"
        assert cfg.openalex_email is None
        assert cfg.vision_enabled is True
        assert cfg.vision_model == "claude-haiku-4-5-20251001"
        assert cfg.anthropic_api_key is None
        assert cfg.vision_max_tables_per_run is None
        assert cfg.vision_max_cost_usd is None
        assert cfg.preflight_enabled is True
        assert cfg.zotero_api_key is None
        assert cfg.zotero_user_id is None
        assert cfg.zotero_library_type == "user"


class TestConfigLoadFromFile:
    def test_load_from_file(self, tmp_path):
        """Create a temp JSON config, verify Config.load(path) reads it."""
        config_data = {
            "zotero_data_dir": str(tmp_path / "MyZotero"),
            "embedding_model": "custom-model",
            "chunk_size": 800,
            "embedding_provider": "local",
            "ocr_language": "deu",
        }
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(config_data))

        cfg = Config.load(path=config_file)

        assert cfg.zotero_data_dir == tmp_path / "MyZotero"
        assert cfg.embedding_model == "custom-model"
        assert cfg.chunk_size == 800
        assert cfg.embedding_provider == "local"
        assert cfg.ocr_language == "deu"
        # Non-specified fields keep provider-aware defaults
        assert cfg.chunk_overlap == 100
        assert cfg.embedding_dimensions == 384  # local provider default


class TestConfigLoadEnvVars:
    def test_load_env_vars(self, tmp_path, monkeypatch):
        """Mock env vars (GEMINI_API_KEY, etc.), verify they're picked up."""
        config_file = tmp_path / "config.json"
        # No API keys in file
        config_file.write_text(json.dumps({}))

        monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
        monkeypatch.setenv("ZOTERO_API_KEY", "test-zotero-key")
        monkeypatch.setenv("ZOTERO_USER_ID", "12345")
        monkeypatch.setenv("OPENALEX_EMAIL", "test@example.com")

        cfg = Config.load(path=config_file)

        assert cfg.gemini_api_key == "test-gemini-key"
        assert cfg.anthropic_api_key == "test-anthropic-key"
        assert cfg.zotero_api_key == "test-zotero-key"
        assert cfg.zotero_user_id == "12345"
        assert cfg.openalex_email == "test@example.com"


class TestConfigSave:
    def test_save_excludes_api_keys(self, tmp_path, monkeypatch):
        """Config.save() does NOT write gemini_api_key, anthropic_api_key, zotero_api_key."""
        monkeypatch.setenv("GEMINI_API_KEY", "secret-gemini")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-anthropic")
        monkeypatch.setenv("ZOTERO_API_KEY", "secret-zotero")

        cfg = Config.load(path=tmp_path / "nonexistent.json")
        save_path = tmp_path / "saved_config.json"
        cfg.save(path=save_path)

        saved_data = json.loads(save_path.read_text())
        assert "gemini_api_key" not in saved_data
        assert "anthropic_api_key" not in saved_data
        assert "zotero_api_key" not in saved_data

    def test_save_file_permissions(self, tmp_path, monkeypatch):
        """Config.save() creates file with 0o600 permissions."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ZOTERO_API_KEY", raising=False)

        cfg = Config.load(path=tmp_path / "nonexistent.json")
        save_path = tmp_path / "saved_config.json"
        cfg.save(path=save_path)

        file_mode = stat.S_IMODE(save_path.stat().st_mode)
        assert file_mode == 0o600


class TestConfigMigration:
    def test_migration_from_deep_zotero(self, tmp_path, monkeypatch):
        """If old config exists but new path doesn't, it loads from old path."""
        old_dir = tmp_path / ".config" / "deep-zotero"
        old_dir.mkdir(parents=True)
        old_config = old_dir / "config.json"
        old_config.write_text(json.dumps({
            "embedding_model": "old-model",
            "chunk_size": 512,
        }))

        # Patch expanduser to use tmp_path as home
        original_expanduser = Path.expanduser

        def mock_expanduser(self):
            s = str(self)
            if s.startswith("~"):
                return Path(str(tmp_path) + s[1:])
            return original_expanduser(self)

        monkeypatch.setattr(Path, "expanduser", mock_expanduser)

        cfg = Config.load()  # No explicit path -> triggers migration logic

        assert cfg.embedding_model == "old-model"
        assert cfg.chunk_size == 512


class TestConfigValidation:
    def test_validate_missing_zotero_dir(self, tmp_path, monkeypatch):
        """validate() returns error when zotero_data_dir doesn't exist."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        cfg = Config.load(path=tmp_path / "nonexistent.json")
        # Point to a directory that doesn't exist
        cfg = Config(
            zotero_data_dir=tmp_path / "nonexistent_zotero",
            chroma_db_path=cfg.chroma_db_path,
            embedding_model=cfg.embedding_model,
            embedding_dimensions=cfg.embedding_dimensions,
            chunk_size=cfg.chunk_size,
            chunk_overlap=cfg.chunk_overlap,
            gemini_api_key="some-key",
            dashscope_api_key=None,
            embedding_provider="gemini",
            embedding_timeout=cfg.embedding_timeout,
            embedding_max_retries=cfg.embedding_max_retries,
            rerank_alpha=cfg.rerank_alpha,
            rerank_section_weights=cfg.rerank_section_weights,
            rerank_journal_weights=cfg.rerank_journal_weights,
            rerank_enabled=cfg.rerank_enabled,
            oversample_multiplier=cfg.oversample_multiplier,
            oversample_topic_factor=cfg.oversample_topic_factor,
            stats_sample_limit=cfg.stats_sample_limit,
            ocr_language=cfg.ocr_language,
            openalex_email=cfg.openalex_email,
            vision_enabled=cfg.vision_enabled,
            vision_model=cfg.vision_model,
            anthropic_api_key=cfg.anthropic_api_key,
            vision_max_tables_per_run=cfg.vision_max_tables_per_run,
            vision_max_cost_usd=cfg.vision_max_cost_usd,
            max_pages=cfg.max_pages,
            preflight_enabled=cfg.preflight_enabled,
            zotero_api_key=cfg.zotero_api_key,
            zotero_user_id=cfg.zotero_user_id,
            zotero_library_type=cfg.zotero_library_type,
            semantic_scholar_api_key=None,
        )

        errors = cfg.validate()
        assert any("Zotero data dir not found" in e for e in errors)

    def test_validate_missing_api_key(self, tmp_path, monkeypatch):
        """validate() returns error when gemini provider but no key."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)

        # Create a valid zotero dir with database
        zotero_dir = tmp_path / "Zotero"
        zotero_dir.mkdir()
        (zotero_dir / "zotero.sqlite").touch()

        config_data = {"zotero_data_dir": str(zotero_dir)}
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(config_data))

        cfg = Config.load(path=config_file)
        errors = cfg.validate()

        assert any("GEMINI_API_KEY not set" in e for e in errors)

    def test_validate_invalid_provider(self, tmp_path, monkeypatch):
        """validate() returns error for unknown provider."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)

        zotero_dir = tmp_path / "Zotero"
        zotero_dir.mkdir()
        (zotero_dir / "zotero.sqlite").touch()

        config_data = {
            "zotero_data_dir": str(zotero_dir),
            "embedding_provider": "openai",
        }
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(config_data))

        cfg = Config.load(path=config_file)
        errors = cfg.validate()

        assert any("Invalid embedding_provider" in e for e in errors)
        assert any("openai" in e for e in errors)


class TestConfigPriorityInversion:
    """Regression tests: env var must take precedence over config file."""

    def _clear_creds(self, monkeypatch):
        for var in ("GEMINI_API_KEY", "DASHSCOPE_API_KEY", "ANTHROPIC_API_KEY",
                    "ZOTERO_API_KEY", "ZOTERO_USER_ID", "S2_API_KEY", "OPENALEX_EMAIL"):
            monkeypatch.delenv(var, raising=False)

    def test_env_wins_over_config_file(self, tmp_path, monkeypatch):
        """When both env var and config file have a value, env wins."""
        self._clear_creds(monkeypatch)
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"gemini_api_key": "from-file"}))
        monkeypatch.setenv("GEMINI_API_KEY", "from-env")
        cfg = Config.load(path=config_file)
        assert cfg.gemini_api_key == "from-env"

    def test_config_file_fallback_when_no_env(self, tmp_path, monkeypatch):
        """When env var is absent, config file value is used."""
        self._clear_creds(monkeypatch)
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"gemini_api_key": "from-file"}))
        cfg = Config.load(path=config_file)
        assert cfg.gemini_api_key == "from-file"

    def test_config_set_persists_but_env_still_wins(self, tmp_path, monkeypatch):
        """config set writes to file; env var still overrides on load."""
        self._clear_creds(monkeypatch)
        from zotpilot.cli import _config_set
        config_file = tmp_path / "config.json"
        _config_set("gemini_api_key", "file-key", config_file)
        monkeypatch.setenv("GEMINI_API_KEY", "env-key")
        cfg = Config.load(path=config_file)
        assert cfg.gemini_api_key == "env-key"

    def test_zotero_creds_env_priority(self, tmp_path, monkeypatch):
        """Zotero creds: env wins over config file."""
        self._clear_creds(monkeypatch)
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "zotero_api_key": "file-zotkey",
            "zotero_user_id": "11111111",
        }))
        monkeypatch.setenv("ZOTERO_API_KEY", "env-zotkey")
        monkeypatch.setenv("ZOTERO_USER_ID", "99999999")
        cfg = Config.load(path=config_file)
        assert cfg.zotero_api_key == "env-zotkey"
        assert cfg.zotero_user_id == "99999999"

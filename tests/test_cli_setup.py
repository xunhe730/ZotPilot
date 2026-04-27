"""Contract tests for setup/register CLI behavior under the new runtime model."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from zotpilot.cli import cmd_index, cmd_register, cmd_setup, cmd_sync, cmd_update
from zotpilot.runtime_settings import resolve_runtime_settings


def _make_fake_zotero(tmp_path: Path) -> Path:
    zotero_dir = tmp_path / "zotero"
    zotero_dir.mkdir()
    (zotero_dir / "zotero.sqlite").write_text("fake sqlite")
    return zotero_dir


def _use_local_secrets(monkeypatch, tmp_path: Path) -> Path:
    secrets_path = tmp_path / "secrets.json"
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
    monkeypatch.setenv("ZOTPILOT_LOCAL_SECRETS_PATH", str(secrets_path))
    return secrets_path


class TestSetup:
    def test_non_interactive_setup_writes_shared_config_and_api_keys(self, tmp_path, monkeypatch):
        _use_local_secrets(monkeypatch, tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("GEMINI_API_KEY", "env-gemini")
        monkeypatch.setenv("ZOTERO_API_KEY", "env-zotero")
        monkeypatch.setenv("ZOTERO_USER_ID", "12345678")
        zotero_dir = _make_fake_zotero(tmp_path)
        config_dir = tmp_path / ".config" / "zotpilot"

        with (
            patch("zotpilot.config._default_config_dir", return_value=config_dir),
            patch("zotpilot.config._default_data_dir", return_value=tmp_path / "data"),
            patch("zotpilot.config._old_config_path", return_value=tmp_path / "old" / "config.json"),
            patch("zotpilot._platforms.register", return_value={"codex": True}) as mock_register,
        ):
            args = type(
                "Args",
                (),
                {
                    "non_interactive": True,
                    "zotero_dir": str(zotero_dir),
                    "provider": "gemini",
                    "gemini_key": None,
                    "dashscope_key": None,
                },
            )()
            rc = cmd_setup(args)

        assert rc == 0
        mock_register.assert_called_once()
        data = json.loads((config_dir / "config.json").read_text())
        assert data["zotero_data_dir"] == str(zotero_dir)
        assert data["embedding_provider"] == "gemini"
        assert data["zotero_user_id"] == "12345678"
        assert data["gemini_api_key"] == "env-gemini"
        assert data["zotero_api_key"] == "env-zotero"

        resolved = resolve_runtime_settings(config_dir / "config.json")
        assert resolved.config.gemini_api_key == "env-gemini"
        assert resolved.config.zotero_api_key == "env-zotero"
        assert resolved.config.zotero_user_id == "12345678"
        assert resolved.secret_backend == "local-file"

    def test_setup_fails_when_client_registration_fails(self, tmp_path, monkeypatch):
        _use_local_secrets(monkeypatch, tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        zotero_dir = _make_fake_zotero(tmp_path)
        config_dir = tmp_path / ".config" / "zotpilot"

        with (
            patch("zotpilot.config._default_config_dir", return_value=config_dir),
            patch("zotpilot.config._default_data_dir", return_value=tmp_path / "data"),
            patch("zotpilot.config._old_config_path", return_value=tmp_path / "old" / "config.json"),
            patch("zotpilot._platforms.register", return_value={"codex": False}),
        ):
            args = type(
                "Args",
                (),
                {
                    "non_interactive": True,
                    "zotero_dir": str(zotero_dir),
                    "provider": "local",
                    "gemini_key": None,
                    "dashscope_key": None,
                },
            )()
            rc = cmd_setup(args)

        assert rc == 1
        assert (config_dir / "config.json").exists()

    def test_interactive_setup_collects_zotero_write_credentials(self, tmp_path, monkeypatch, capsys):
        _use_local_secrets(monkeypatch, tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        zotero_dir = _make_fake_zotero(tmp_path)
        config_dir = tmp_path / ".config" / "zotpilot"

        with (
            patch("zotpilot.config._default_config_dir", return_value=config_dir),
            patch("zotpilot.config._default_data_dir", return_value=tmp_path / "data"),
            patch("zotpilot.config._old_config_path", return_value=tmp_path / "old" / "config.json"),
            patch("zotpilot.zotero_detector.detect_zotero_data_dir", return_value=zotero_dir),
            patch("zotpilot._platforms.register", return_value={"codex": True}),
            patch(
                "builtins.input",
                side_effect=["", "1", "gemini-key", "y", "12345678", "zotero-key"],
            ),
        ):
            args = type(
                "Args",
                (),
                {
                    "non_interactive": False,
                    "zotero_dir": None,
                    "provider": None,
                    "gemini_key": None,
                    "dashscope_key": None,
                },
            )()
            rc = cmd_setup(args)

        assert rc == 0
        out = capsys.readouterr().out
        assert "Zotero Web API" in out
        resolved = resolve_runtime_settings(config_dir / "config.json")
        assert resolved.config.gemini_api_key == "gemini-key"
        assert resolved.config.zotero_api_key == "zotero-key"
        assert resolved.config.zotero_user_id == "12345678"

    def test_setup_does_not_implicitly_import_legacy_config(self, tmp_path, monkeypatch):
        _use_local_secrets(monkeypatch, tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        zotero_dir = _make_fake_zotero(tmp_path)
        config_dir = tmp_path / ".config" / "zotpilot"
        old_dir = tmp_path / ".config" / "deep-zotero"
        old_dir.mkdir(parents=True)
        (old_dir / "config.json").write_text(
            json.dumps({"openalex_email": "legacy@example.com", "zotero_user_id": "999"})
        )

        with (
            patch("zotpilot.config._default_config_dir", return_value=config_dir),
            patch("zotpilot.config._default_data_dir", return_value=tmp_path / "data"),
            patch("zotpilot.config._old_config_path", return_value=old_dir / "config.json"),
            patch("zotpilot._platforms.register", return_value={"codex": True}),
        ):
            args = type(
                "Args",
                (),
                {
                    "non_interactive": True,
                    "zotero_dir": str(zotero_dir),
                    "provider": "local",
                    "gemini_key": None,
                    "dashscope_key": None,
                },
            )()
            rc = cmd_setup(args)

        assert rc == 0
        data = json.loads((config_dir / "config.json").read_text())
        assert data["zotero_data_dir"] == str(zotero_dir)
        assert data["embedding_provider"] == "local"
        assert "openalex_email" not in data
        assert "zotero_user_id" not in data

    def test_non_interactive_setup_requires_zotero_sqlite(self, tmp_path, monkeypatch, capsys):
        """Non-interactive setup fails when zotero.sqlite is missing."""
        _use_local_secrets(monkeypatch, tmp_path)
        fake_dir = tmp_path / "not_zotero"
        fake_dir.mkdir()

        args = type(
            "Args",
            (),
            {
                "non_interactive": True,
                "zotero_dir": str(fake_dir),
                "provider": "local",
                "gemini_key": None,
                "dashscope_key": None,
            },
        )()
        rc = cmd_setup(args)
        assert rc == 1
        captured = capsys.readouterr()
        assert "zotero.sqlite not found" in captured.err

    def test_setup_prints_config_path(self, tmp_path, monkeypatch, capsys):
        """Setup prints the config file path after writing."""
        _use_local_secrets(monkeypatch, tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        zotero_dir = _make_fake_zotero(tmp_path)

        config_dir = tmp_path / ".config" / "zotpilot"
        with (
            patch("zotpilot.config._default_config_dir", return_value=config_dir),
            patch("zotpilot.config._default_data_dir", return_value=tmp_path / "data"),
            patch("zotpilot.config._old_config_path", return_value=tmp_path / "old" / "config.json"),
            patch("zotpilot._platforms.register", return_value={"codex": True}),
        ):
            args = type(
                "Args",
                (),
                {
                    "non_interactive": True,
                    "zotero_dir": str(zotero_dir),
                    "provider": "local",
                    "gemini_key": None,
                    "dashscope_key": None,
                },
            )()
            cmd_setup(args)

        captured = capsys.readouterr()
        assert "Config written to:" in captured.out


class TestIndexCli:
    def test_index_cli_defaults_to_batch_size_two(self, tmp_path, monkeypatch):
        _use_local_secrets(monkeypatch, tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        config = MagicMock()
        config.validate.return_value = []
        config.max_pages = 40
        config.vision_enabled = False

        indexer = MagicMock()
        indexer.index_all.return_value = {
            "results": [],
            "indexed": 0,
            "already_indexed": 0,
            "skipped": 0,
            "failed": 0,
            "empty": 0,
        }

        args = type(
            "Args",
            (),
            {
                "config": None,
                "verbose": False,
                "no_vision": False,
                "max_pages": None,
                "batch_size": 2,
                "force": False,
                "limit": None,
                "item_key": None,
                "title": None,
            },
        )()

        with (
            patch("zotpilot.cli.resolve_runtime_config", return_value=config),
            patch("zotpilot.indexer.Indexer", return_value=indexer),
        ):
            rc = cmd_index(args)

        assert rc == 0
        assert indexer.index_all.call_args.kwargs["batch_size"] == 2


class TestRegister:
    def test_register_legacy_secret_flags_import_into_config(self, tmp_path, monkeypatch):
        _use_local_secrets(monkeypatch, tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))

        with (
            patch("zotpilot.cli._default_config_path", return_value=tmp_path / "config.json"),
            patch("zotpilot._platforms.register", return_value={"codex": True}) as mock_register,
        ):
            args = type(
                "Args",
                (),
                {
                    "platforms": None,
                    "gemini_key": "legacy-gemini",
                    "dashscope_key": None,
                    "zotero_api_key": "legacy-zotero",
                    "zotero_user_id": "7654321",
                },
            )()
            rc = cmd_register(args)

        assert rc == 0
        mock_register.assert_called_once()
        resolved = resolve_runtime_settings(tmp_path / "config.json")
        assert resolved.config.gemini_api_key == "legacy-gemini"
        assert resolved.config.zotero_api_key == "legacy-zotero"
        assert resolved.config.zotero_user_id == "7654321"
        data = json.loads((tmp_path / "config.json").read_text())
        assert data["gemini_api_key"] == "legacy-gemini"
        assert data["zotero_api_key"] == "legacy-zotero"


class TestUpdateSync:
    def _setup_minimal_config(self, tmp_path: Path) -> Path:
        config_dir = tmp_path / ".config" / "zotpilot"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "zotero_data_dir": str(tmp_path / "zotero"),
                    "chroma_db_path": str(tmp_path / "chroma"),
                    "embedding_provider": "local",
                }
            )
        )
        return config_path

    def test_update_does_not_back_import_runtime_env_to_config(self, tmp_path, monkeypatch):
        _use_local_secrets(monkeypatch, tmp_path)
        config_path = self._setup_minimal_config(tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("GEMINI_API_KEY", "should-not-be-imported")

        with (
            patch("zotpilot.cli._default_config_path", return_value=config_path),
            patch("zotpilot.cli._get_current_version", return_value="0.5.0"),
            patch("zotpilot.cli._get_latest_pypi_version", return_value="0.5.0"),
            patch(
                "zotpilot.cli._deployment_status",
                return_value={"drift_state": "clean", "legacy_embedded_secrets_detected": False},
            ),
        ):
            args = type(
                "Args",
                (),
                {
                    "cli_only": False,
                    "skill_only": False,
                    "check": False,
                    "dry_run": True,
                    "migrate_secrets": False,
                    "re_register": False,
                },
            )()
            cmd_update(args)

        data = json.loads(config_path.read_text())
        assert "gemini_api_key" not in data

    def test_sync_uses_register_without_mutating_config(self, tmp_path, monkeypatch):
        _use_local_secrets(monkeypatch, tmp_path)
        config_path = self._setup_minimal_config(tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))

        with (
            patch("zotpilot.cli._default_config_path", return_value=config_path),
            patch("zotpilot._platforms.register", return_value={"codex": True}) as mock_register,
        ):
            args = type("Args", (), {"dry_run": False})()
            rc = cmd_sync(args)

        assert rc == 0
        mock_register.assert_called_once()
        data = json.loads(config_path.read_text())
        assert "gemini_api_key" not in data

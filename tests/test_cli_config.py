"""Tests for `zotpilot config` CLI subcommands."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from zotpilot.cli import _coerce_value, _config_set, _mask_secret
from zotpilot.config import Config

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_config(args: list[str], config_path: Path, monkeypatch, capsys):
    """Invoke cmd_config() directly with the given sub-args."""
    import argparse

    from zotpilot.cli import cmd_config

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    p = sub.add_parser("config")
    p.add_argument("subcommand", nargs="?")
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")
    p.add_argument("--config", default=str(config_path))
    p.set_defaults(func=cmd_config)

    parsed = parser.parse_args(["config"] + args)
    parsed.config = str(config_path)
    cmd_config(parsed)
    return capsys.readouterr()


# ---------------------------------------------------------------------------
# _mask_secret
# ---------------------------------------------------------------------------

class TestMaskSecret:
    def test_short_value_is_fully_masked(self):
        assert _mask_secret("abc") == "****"

    def test_long_value_shows_prefix(self):
        result = _mask_secret("ABCD1234")
        assert result.startswith("ABCD")
        assert result.endswith("****")

    def test_exactly_four_chars(self):
        assert _mask_secret("ABCD") == "****"


# ---------------------------------------------------------------------------
# _coerce_value
# ---------------------------------------------------------------------------

class TestCoerceValue:
    def test_int_field(self):
        assert _coerce_value("chunk_size", "512") == 512

    def test_float_field(self):
        assert _coerce_value("rerank_alpha", "0.5") == 0.5

    def test_bool_true(self):
        assert _coerce_value("rerank_enabled", "true") is True

    def test_bool_false(self):
        assert _coerce_value("rerank_enabled", "false") is False

    def test_string_field(self):
        assert _coerce_value("zotero_api_key", "my-key") == "my-key"

    def test_json_dict_field(self):
        val = _coerce_value("rerank_section_weights", '{"abstract": 1.5}')
        assert val == {"abstract": 1.5}

    def test_path_field_is_string(self):
        result = _coerce_value("chroma_db_path", "/tmp/chroma")
        assert result == "/tmp/chroma"


# ---------------------------------------------------------------------------
# _config_set
# ---------------------------------------------------------------------------

class TestConfigSet:
    def test_creates_file_with_value(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        _config_set("chunk_size", "256", cfg_path)
        data = json.loads(cfg_path.read_text())
        assert data["chunk_size"] == 256

    def test_updates_existing_value(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"chunk_size": 400}))
        _config_set("chunk_size", "512", cfg_path)
        data = json.loads(cfg_path.read_text())
        assert data["chunk_size"] == 512

    def test_preserves_other_fields(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"chunk_size": 400, "chunk_overlap": 100}))
        _config_set("chunk_size", "256", cfg_path)
        data = json.loads(cfg_path.read_text())
        assert data["chunk_overlap"] == 100

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix-only permission check")
    def test_file_permissions_are_restrictive(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        _config_set("zotero_api_key", "test-key", cfg_path)
        mode = oct(cfg_path.stat().st_mode & 0o777)
        assert mode == oct(0o600)

    def test_does_not_persist_env_var_value(self, tmp_path, monkeypatch):
        """config set writes only the given value, not whatever is in env."""
        monkeypatch.setenv("GEMINI_API_KEY", "env-value")
        cfg_path = tmp_path / "config.json"
        _config_set("gemini_api_key", "file-value", cfg_path)
        data = json.loads(cfg_path.read_text())
        # Only the explicitly set value should be on disk
        assert data["gemini_api_key"] == "file-value"
        assert data.get("gemini_api_key") != "env-value"


# ---------------------------------------------------------------------------
# config set + Config.load() round-trip
# ---------------------------------------------------------------------------

class TestConfigSetLoadRoundTrip:
    def test_set_and_load(self, tmp_path, monkeypatch):
        """config set persists; Config.load() reads it back when no env var."""
        monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
        cfg_path = tmp_path / "config.json"
        _config_set("zotero_api_key", "stored-key", cfg_path)
        cfg = Config.load(path=cfg_path)
        assert cfg.zotero_api_key == "stored-key"

    def test_env_overrides_set_value(self, tmp_path, monkeypatch):
        """Even after config set, env var wins on load."""
        cfg_path = tmp_path / "config.json"
        _config_set("zotero_api_key", "stored-key", cfg_path)
        monkeypatch.setenv("ZOTERO_API_KEY", "override-key")
        cfg = Config.load(path=cfg_path)
        assert cfg.zotero_api_key == "override-key"

    def test_non_numeric_user_id_stored_as_is(self, tmp_path):
        """config set accepts non-numeric user_id (CLI warns but still writes)."""
        cfg_path = tmp_path / "config.json"
        _config_set("zotero_user_id", "xunhe730", cfg_path)
        data = json.loads(cfg_path.read_text())
        assert data["zotero_user_id"] == "xunhe730"


# ---------------------------------------------------------------------------
# cmd_status --json version field
# ---------------------------------------------------------------------------

class TestStatusJsonVersion:
    def test_status_json_includes_version(self, tmp_path, capsys, monkeypatch):
        """status --json output includes 'version' matching __version__."""
        import argparse
        from unittest.mock import patch

        from zotpilot import __version__
        from zotpilot.cli import cmd_status

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({
            "zotero_data_dir": str(tmp_path),
            "embedding_provider": "none",
        }))

        args = argparse.Namespace(json=True, config=str(cfg_path))
        with patch("zotpilot.cli.Config.load") as mock_load:
            mock_cfg = mock_load.return_value
            mock_cfg.zotero_data_dir = tmp_path
            mock_cfg.chroma_db_path = tmp_path / "chroma"
            mock_cfg.embedding_provider = "none"
            mock_cfg.gemini_api_key = None
            mock_cfg.dashscope_api_key = None
            mock_cfg.validate.return_value = []
            cmd_status(args)

        out = capsys.readouterr().out
        data = json.loads(out)
        assert "version" in data
        assert data["version"] == __version__

    def test_status_json_includes_deployment_visibility(self, tmp_path, capsys):
        """status --json also reports detected/registered clients and skill dirs."""
        import argparse
        from unittest.mock import patch

        from zotpilot.cli import cmd_status

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({
            "zotero_data_dir": str(tmp_path),
            "embedding_provider": "none",
        }))

        args = argparse.Namespace(json=True, config=str(cfg_path))
        with (
            patch("zotpilot.cli.Config.load") as mock_load,
            patch(
                "zotpilot.cli._deployment_status",
                return_value={
                    "detected_platforms": ["codex"],
                    "registered_platforms": ["codex"],
                    "unsupported_platforms": [],
                    "registration": {
                        "codex": {"registered": True, "config_path": None},
                    },
                    "skill_dirs": [
                        {
                            "path": "/tmp/skills/ztp-research",
                            "is_symlink": False,
                            "is_broken_symlink": False,
                            "is_duplicate": False,
                        }
                    ],
                    "drift_state": "clean",
                    "restart_required": False,
                },
            ),
        ):
            mock_cfg = mock_load.return_value
            mock_cfg.zotero_data_dir = tmp_path
            mock_cfg.chroma_db_path = tmp_path / "chroma"
            mock_cfg.embedding_provider = "none"
            mock_cfg.gemini_api_key = None
            mock_cfg.dashscope_api_key = None
            mock_cfg.validate.return_value = []
            cmd_status(args)

        data = json.loads(capsys.readouterr().out)
        assert data["detected_platforms"] == ["codex"]
        assert data["registered_platforms"] == ["codex"]
        assert data["skill_dirs"][0]["path"] == "/tmp/skills/ztp-research"

    def test_status_json_sets_restart_required_when_drift_present(self, tmp_path, capsys):
        import argparse
        from unittest.mock import patch

        from zotpilot.cli import cmd_status

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({
            "zotero_data_dir": str(tmp_path),
            "embedding_provider": "none",
        }))

        args = argparse.Namespace(json=True, config=str(cfg_path))
        with (
            patch("zotpilot.cli.Config.load") as mock_load,
            patch(
                "zotpilot.cli._deployment_status",
                return_value={
                    "detected_platforms": ["codex"],
                    "registered_platforms": ["codex"],
                    "unsupported_platforms": [],
                    "registration": {"codex": {"registered": True, "config_path": None}},
                    "skill_dirs": [],
                    "drift_state": "needs-sync",
                    "restart_required": True,
                },
            ),
        ):
            mock_cfg = mock_load.return_value
            mock_cfg.zotero_data_dir = tmp_path
            mock_cfg.chroma_db_path = tmp_path / "chroma"
            mock_cfg.embedding_provider = "none"
            mock_cfg.gemini_api_key = None
            mock_cfg.dashscope_api_key = None
            mock_cfg.validate.return_value = []
            cmd_status(args)

        data = json.loads(capsys.readouterr().out)
        assert data["drift_state"] == "needs-sync"
        assert data["restart_required"] is True

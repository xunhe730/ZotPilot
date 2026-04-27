"""Tests for `zotpilot config` subcommands under the config-backed key model."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from zotpilot.cli import _coerce_value, _config_set, _mask_secret
from zotpilot.runtime_settings import resolve_runtime_settings


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


def _run_config(args: list[str], config_path: Path, monkeypatch, capsys):
    import argparse

    from zotpilot.cli import cmd_config

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    p = sub.add_parser("config")
    p.add_argument("subcommand", nargs="?")
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")
    p.add_argument("--config", default=str(config_path))
    p.add_argument("--force", action="store_true")
    p.add_argument("--to-config", action="store_true", default=True, dest="to_config")
    p.set_defaults(func=cmd_config)

    parsed = parser.parse_args(["config"] + args)
    parsed.config = str(config_path)
    parsed.config_subcmd = parsed.subcommand
    if not hasattr(parsed, "to_config"):
        parsed.to_config = True
    if not hasattr(parsed, "force"):
        parsed.force = False
    returncode = cmd_config(parsed)
    captured = capsys.readouterr()
    return SimpleNamespace(out=captured.out, err=captured.err, returncode=returncode)


class TestMaskSecret:
    def test_short_value_is_fully_masked(self):
        assert _mask_secret("abc") == "****"

    def test_long_value_shows_prefix(self):
        result = _mask_secret("ABCD1234")
        assert result.startswith("ABCD")
        assert result.endswith("****")


class TestConfigSet:
    def test_creates_file_with_value(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        _config_set("chunk_size", "256", cfg_path)
        assert json.loads(cfg_path.read_text())["chunk_size"] == 256

    def test_preserves_other_fields(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"chunk_size": 400, "chunk_overlap": 100}))
        _config_set("chunk_size", "256", cfg_path)
        data = json.loads(cfg_path.read_text())
        assert data["chunk_overlap"] == 100

    def test_string_field(self):
        assert _coerce_value("zotero_api_key", "my-key") == "my-key"


class TestConfigCommand:
    def test_secret_fields_store_in_config_json(self, tmp_path, monkeypatch, capsys):
        _use_local_secrets(monkeypatch, tmp_path)
        cfg_path = tmp_path / "config.json"
        out = _run_config(["set", "zotero_api_key", "secret-zot"], cfg_path, monkeypatch, capsys)
        assert out.returncode == 0
        assert "config.json" in out.out.lower()

        resolved = resolve_runtime_settings(cfg_path)
        assert resolved.config.zotero_api_key == "secret-zot"
        assert json.loads(cfg_path.read_text())["zotero_api_key"] == "secret-zot"

    def test_non_secret_fields_persist_to_config_json(self, tmp_path, monkeypatch, capsys):
        _use_local_secrets(monkeypatch, tmp_path)
        cfg_path = tmp_path / "config.json"
        out = _run_config(["set", "zotero_user_id", "12345678"], cfg_path, monkeypatch, capsys)
        assert out.returncode == 0
        data = json.loads(cfg_path.read_text())
        assert data["zotero_user_id"] == "12345678"

    def test_config_get_masks_secret_values(self, tmp_path, monkeypatch, capsys):
        _use_local_secrets(monkeypatch, tmp_path)
        cfg_path = tmp_path / "config.json"
        _run_config(["set", "gemini_api_key", "top-secret"], cfg_path, monkeypatch, capsys)
        out = _run_config(["get", "gemini_api_key"], cfg_path, monkeypatch, capsys)
        assert "top-secret" not in out.out
        assert "****" in out.out

    def test_config_unset_removes_secret(self, tmp_path, monkeypatch, capsys):
        _use_local_secrets(monkeypatch, tmp_path)
        cfg_path = tmp_path / "config.json"
        _run_config(["set", "zotero_api_key", "secret-zot"], cfg_path, monkeypatch, capsys)
        out = _run_config(["unset", "zotero_api_key"], cfg_path, monkeypatch, capsys)
        assert out.returncode == 0
        assert "zotero_api_key" not in json.loads(cfg_path.read_text())
        resolved = resolve_runtime_settings(cfg_path)
        assert resolved.config.zotero_api_key is None

    def test_config_unset_removes_legacy_secret_fallback(self, tmp_path, monkeypatch, capsys):
        from zotpilot.secret_store import set_secret

        _use_local_secrets(monkeypatch, tmp_path)
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"gemini_api_key": "config-gemini"}))
        set_secret("gemini_api_key", "legacy-gemini")

        out = _run_config(["unset", "gemini_api_key"], cfg_path, monkeypatch, capsys)

        assert out.returncode == 0
        resolved = resolve_runtime_settings(cfg_path)
        assert resolved.config.gemini_api_key is None

    def test_migrate_secrets_defaults_to_config_json(self, tmp_path, monkeypatch, capsys):
        from zotpilot.secret_store import set_secret

        _use_local_secrets(monkeypatch, tmp_path)
        cfg_path = tmp_path / "config.json"
        set_secret("gemini_api_key", "legacy-gemini")

        out = _run_config(["migrate-secrets"], cfg_path, monkeypatch, capsys)

        assert out.returncode == 0
        data = json.loads(cfg_path.read_text())
        assert data["gemini_api_key"] == "legacy-gemini"

    def test_migrate_secrets_does_not_capture_runtime_env(self, tmp_path, monkeypatch, capsys):
        _use_local_secrets(monkeypatch, tmp_path)
        cfg_path = tmp_path / "config.json"
        monkeypatch.setenv("GEMINI_API_KEY", "env-should-stay-runtime-only")

        out = _run_config(["migrate-secrets"], cfg_path, monkeypatch, capsys)

        assert out.returncode == 0
        assert not cfg_path.exists()

    def test_status_json_includes_new_runtime_fields(self, tmp_path, monkeypatch, capsys):
        _use_local_secrets(monkeypatch, tmp_path)
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "zotero_data_dir": str(tmp_path),
                    "embedding_provider": "none",
                }
            )
        )

        from zotpilot.cli import cmd_status

        args = SimpleNamespace(json=True, config=str(cfg_path))
        cmd_status(args)
        data = json.loads(capsys.readouterr().out)
        assert "version" in data
        assert "secret_backend" in data
        assert "write_ops_ready" in data

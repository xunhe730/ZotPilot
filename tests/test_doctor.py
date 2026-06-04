"""Tests for ZotPilot doctor checks under the config-backed key model."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from zotpilot.doctor import (
    CheckResult,
    _check_chromadb_index,
    _check_config_exists,
    _check_config_permissions,
    _check_embedding_api_key,
    _check_python_version,
    _check_secret_backend,
    _check_write_connectivity,
    _check_zotero_data,
    _check_zotero_web_api,
    run_checks,
)


def _use_local_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("ZOTPILOT_SECRET_BACKEND", "local-file")
    monkeypatch.setenv("ZOTPILOT_LOCAL_SECRETS_PATH", str(tmp_path / "secrets.json"))


class TestCheckPythonVersion:
    def test_pass_on_current_python(self):
        result = _check_python_version()
        assert result.status == "pass"


class TestCheckConfigExists:
    def test_pass_when_exists(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text("{}")
        assert _check_config_exists(config_file).status == "pass"


class TestCheckZoteroData:
    def test_pass_with_valid_sqlite(self, tmp_path):
        sqlite_path = tmp_path / "zotero.sqlite"
        conn = sqlite3.connect(str(sqlite_path))
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO items VALUES (1)")
        conn.commit()
        conn.close()

        config = MagicMock()
        config.zotero_data_dir = tmp_path
        assert _check_zotero_data(config).status == "pass"


class TestCheckEmbeddingApiKey:
    def test_local_no_key_needed(self):
        config = MagicMock()
        config.embedding_provider = "local"
        assert _check_embedding_api_key(config).status == "pass"

    def test_missing_gemini_key_fails(self):
        config = MagicMock()
        config.embedding_provider = "gemini"
        config.gemini_api_key = None
        assert _check_embedding_api_key(config).status == "fail"

    def _openai_compat_config(self, base_url, api_key):
        config = MagicMock()
        config.embedding_provider = "openai-compatible"
        config.embedding_base_url = base_url
        config.embedding_api_key = api_key
        return config

    def test_openai_compatible_pass_when_key_resolves(self, monkeypatch):
        monkeypatch.delenv("ZOTPILOT_EMBEDDING_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        config = self._openai_compat_config("https://api.siliconflow.cn/v1", "sk-secret")
        result = _check_embedding_api_key(config)
        assert result.status == "pass"
        assert "openai-compatible" in result.message

    def test_openai_compatible_pass_when_key_from_env(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("ZOTPILOT_EMBEDDING_API_KEY", "sk-from-env")
        config = self._openai_compat_config("https://api.siliconflow.cn/v1", None)
        result = _check_embedding_api_key(config)
        assert result.status == "pass"

    def test_openai_compatible_warn_when_no_key_remote(self, monkeypatch):
        monkeypatch.delenv("ZOTPILOT_EMBEDDING_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        config = self._openai_compat_config("https://api.siliconflow.cn/v1", None)
        result = _check_embedding_api_key(config)
        # Missing key must WARN, never FAIL (a missing key may still be valid).
        assert result.status == "warn"
        assert "Unknown provider" not in result.message

    def test_openai_compatible_warn_when_no_key_local(self, monkeypatch):
        monkeypatch.delenv("ZOTPILOT_EMBEDDING_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        config = self._openai_compat_config("http://localhost:11434/v1", None)
        result = _check_embedding_api_key(config)
        assert result.status == "warn"
        assert "local" in result.message.lower()

    def test_openai_compatible_never_fails(self, monkeypatch):
        # Even with no base_url and no key, the worst outcome is WARN.
        monkeypatch.delenv("ZOTPILOT_EMBEDDING_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        config = self._openai_compat_config(None, None)
        result = _check_embedding_api_key(config)
        assert result.status in ("pass", "warn")
        assert result.status != "fail"


class TestIsLocalHost:
    def test_localhost_variants_are_local(self):
        from zotpilot.doctor import _is_local_host

        assert _is_local_host("http://localhost:11434/v1")
        assert _is_local_host("http://127.0.0.1:11434/v1")
        assert _is_local_host("http://[::1]:11434/v1")
        assert _is_local_host("http://my-box.local/v1")

    def test_remote_and_empty_are_not_local(self):
        from zotpilot.doctor import _is_local_host

        assert not _is_local_host("https://api.siliconflow.cn/v1")
        assert not _is_local_host(None)
        assert not _is_local_host("")


class TestCheckSecretBackend:
    def test_local_file_backend_passes(self, tmp_path, monkeypatch):
        _use_local_secrets(monkeypatch, tmp_path)
        result = _check_secret_backend()
        assert result.status == "pass"
        assert "local-file" in result.message

    def test_unavailable_backend_is_unused_when_config_has_key(self, monkeypatch):
        config = MagicMock()
        config.embedding_provider = "gemini"
        config.gemini_api_key = "config-gemini"

        with patch("zotpilot.doctor.describe_backend") as mock_backend:
            mock_backend.return_value.available = False
            mock_backend.return_value.detail = "No legacy backend configured"
            result = _check_secret_backend(config, {"gemini_api_key": "config"})

        assert result.status == "pass"
        assert "unused" in result.message


class TestCheckChromaDbIndex:
    @patch("zotpilot.zotero_client.ZoteroClient")
    @patch("zotpilot.vector_store.VectorStore")
    @patch("zotpilot.embeddings.create_embedder")
    def test_warn_when_empty(self, _mock_create_embedder, mock_vector_store_cls, mock_zotero_cls):
        mock_store = MagicMock()
        mock_store.get_indexed_doc_ids.return_value = []
        mock_store.count_chunks_for_doc_ids.return_value = 0
        mock_vector_store_cls.return_value = mock_store
        zotero = MagicMock()
        zotero.get_all_items_with_pdfs.return_value = []
        mock_zotero_cls.return_value = zotero

        config = MagicMock()
        config.zotero_data_dir = "/fake"
        assert _check_chromadb_index(config).status == "warn"


class TestCheckZoteroWebApi:
    def _make_config(self, api_key=None, user_id=None):
        cfg = MagicMock()
        cfg.zotero_api_key = api_key
        cfg.zotero_user_id = user_id
        return cfg

    def test_pass_when_both_set(self):
        result = _check_zotero_web_api(
            self._make_config("key123", "456"),
            {"zotero_api_key": "config", "zotero_user_id": "config"},
        )
        assert result.status == "pass"
        assert "config" in result.message

    def test_warn_when_missing(self):
        result = _check_zotero_web_api(self._make_config(None, None), {})
        assert result.status == "warn"
        assert "config set zotero_api_key" in result.message


class TestCheckWriteConnectivity:
    @patch("pyzotero.zotero.Zotero")
    def test_pass_with_valid_credentials(self, mock_zotero_cls):
        mock_zotero_cls.return_value = MagicMock()
        config = MagicMock()
        config.zotero_api_key = "key123"
        config.zotero_user_id = "456"
        config.zotero_library_type = "user"
        assert _check_write_connectivity(config).status == "pass"


class TestRunChecks:
    @patch("zotpilot.doctor._check_write_connectivity", return_value=CheckResult("write_connectivity", "pass", "ok"))
    @patch("zotpilot.doctor._check_zotero_web_api", return_value=CheckResult("zotero_web_api", "pass", "ok"))
    @patch("zotpilot.doctor._check_chromadb_index", return_value=CheckResult("chromadb_index", "pass", "ok"))
    @patch("zotpilot.doctor._check_embedding_api_key", return_value=CheckResult("embedding_api_key", "pass", "ok"))
    @patch("zotpilot.doctor._check_zotero_data", return_value=CheckResult("zotero_data", "pass", "ok"))
    @patch(
        "zotpilot.doctor._check_secret_backend",
        return_value=CheckResult("legacy_secret_backend", "pass", "local-file"),
    )
    @patch("zotpilot.doctor.resolve_runtime_settings")
    def test_returns_all_checks(self, mock_resolve, *_mocks):
        config_file = Path(tempfile.mkdtemp(prefix="zotpilot-doctor-")) / "config.json"
        config_file.write_text('{"embedding_provider": "local"}')
        mock_resolve.return_value = MagicMock(config=MagicMock(), sources={})

        results = run_checks(config_path=str(config_file), full=True)
        names = [r.name for r in results]
        assert "legacy_secret_backend" in names
        assert "write_connectivity" in names


class TestCmdDoctorJsonOutput:
    def test_json_output_contains_embedded_secret_fields(self, tmp_path, monkeypatch, capsys):
        _use_local_secrets(monkeypatch, tmp_path)
        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps(
                {
                    "zotero_data_dir": str(tmp_path),
                    "embedding_provider": "local",
                }
            )
        )

        from zotpilot.cli import cmd_doctor

        args = MagicMock()
        args.config = str(config_file)
        args.json = True
        args.full = False

        with patch(
            "zotpilot.cli._deployment_status",
            return_value={
                "legacy_embedded_secrets_detected": True,
                "legacy_embedded_secret_platforms": ["codex"],
            },
        ):
            cmd_doctor(args)

        data = json.loads(capsys.readouterr().out)
        assert data["legacy_embedded_secrets_detected"] is True
        assert data["legacy_embedded_secret_platforms"] == ["codex"]


class TestRuntimeSettingsEmbeddingKey:
    """Step 4.7: embedding_api_key must be known to runtime_settings."""

    def test_field_registered(self):
        from zotpilot.runtime_settings import ENV_TO_FIELD, SECRET_FIELDS

        assert "embedding_api_key" in SECRET_FIELDS
        assert ENV_TO_FIELD["ZOTPILOT_EMBEDDING_API_KEY"] == "embedding_api_key"
        assert ENV_TO_FIELD["OPENAI_API_KEY"] == "embedding_api_key"

    def test_zotpilot_env_resolves_with_env_override_source(self, tmp_path, monkeypatch):
        from zotpilot.runtime_settings import resolve_runtime_settings

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("ZOTPILOT_EMBEDDING_API_KEY", "sk-runtime")
        config_file = tmp_path / "config.json"
        config_file.write_text('{"embedding_provider": "openai-compatible"}')

        resolved = resolve_runtime_settings(config_file)
        assert resolved.config.embedding_api_key == "sk-runtime"
        assert resolved.sources["embedding_api_key"] == "env-override"

    def test_zotpilot_env_wins_over_openai_env(self, tmp_path, monkeypatch):
        # Locks the precedence that ENV_TO_FIELD currently encodes only via dict
        # insertion order (ZOTPILOT_EMBEDDING_API_KEY listed last => wins the
        # resolve loop). This guards against a future silent reorder of the dict
        # silently flipping precedence to the generic OPENAI_API_KEY.
        from zotpilot.runtime_settings import resolve_runtime_settings

        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
        monkeypatch.setenv("ZOTPILOT_EMBEDDING_API_KEY", "sk-zotpilot")
        config_file = tmp_path / "config.json"
        config_file.write_text('{"embedding_provider": "openai-compatible"}')

        resolved = resolve_runtime_settings(config_file)
        assert resolved.config.embedding_api_key == "sk-zotpilot"


class TestConfigGetEmbeddingKey:
    """Step 5.16: config get must mask the key and report the env source."""

    def test_masked_value_and_env_override_source(self, tmp_path, monkeypatch, capsys):
        _use_local_secrets(monkeypatch, tmp_path)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("ZOTPILOT_EMBEDDING_API_KEY", "sk-supersecret")
        config_file = tmp_path / "config.json"
        config_file.write_text('{"embedding_provider": "openai-compatible"}')

        from zotpilot.cli import cmd_config

        args = MagicMock()
        args.config = str(config_file)
        args.config_subcmd = "get"
        args.key = "embedding_api_key"

        cmd_config(args)
        out = capsys.readouterr().out
        assert "env-override" in out
        # Secret value must be masked, never printed in full.
        assert "sk-supersecret" not in out
        assert "sk-s****" in out


class TestCheckConfigPermissions:
    def test_check_config_permissions_reads_raw_file_not_env(self, tmp_path):
        """Regression: _check_config_permissions should check raw file data, not env overrides."""
        config_path = tmp_path / "config.json"
        config_path.write_text('{"embedding_provider": "gemini"}', encoding="utf-8")
        if sys.platform != "win32":
            config_path.chmod(0o644)

        result = _check_config_permissions(config_path)
        # Should pass (no secrets in file) even if env vars were set
        assert result.status in ("pass", "warn")  # warn only for permissions, not for "contains API keys"
        assert "contains API keys" not in result.message


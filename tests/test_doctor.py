"""Tests for ZotPilot doctor checks under the config-backed key model."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from zotpilot.doctor import (
    CheckResult,
    _check_chromadb_index,
    _check_config_exists,
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

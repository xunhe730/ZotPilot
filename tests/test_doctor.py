"""Tests for ZotPilot doctor health checks."""
import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from zotpilot.doctor import (
    CheckResult,
    _check_chromadb_index,
    _check_config_exists,
    _check_embedding_api_key,
    _check_python_version,
    _check_write_connectivity,
    _check_zotero_data,
    _check_zotero_web_api,
    run_checks,
)


class TestCheckPythonVersion:
    def test_pass_on_current_python(self):
        result = _check_python_version()
        assert result.status == "pass"
        assert result.name == "python_version"
        assert "Python" in result.message

    @patch("zotpilot.doctor.sys")
    def test_fail_on_old_python(self, mock_sys):
        mock_sys.version_info = type("vi", (), {"major": 3, "minor": 9, "micro": 1})()
        result = _check_python_version()
        assert result.status == "fail"
        assert "requires >= 3.10" in result.message


class TestCheckConfigExists:
    def test_pass_when_exists(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text("{}")
        result = _check_config_exists(config_file)
        assert result.status == "pass"

    def test_fail_when_missing(self, tmp_path):
        result = _check_config_exists(tmp_path / "nonexistent.json")
        assert result.status == "fail"
        assert "Not found" in result.message


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
        result = _check_zotero_data(config)
        assert result.status == "pass"

    def test_fail_when_dir_missing(self, tmp_path):
        config = MagicMock()
        config.zotero_data_dir = tmp_path / "nonexistent"
        result = _check_zotero_data(config)
        assert result.status == "fail"
        assert "Directory not found" in result.message

    def test_fail_when_sqlite_missing(self, tmp_path):
        config = MagicMock()
        config.zotero_data_dir = tmp_path
        result = _check_zotero_data(config)
        assert result.status == "fail"
        assert "zotero.sqlite not found" in result.message


class TestCheckEmbeddingApiKey:
    def test_local_no_key_needed(self):
        config = MagicMock()
        config.embedding_provider = "local"
        result = _check_embedding_api_key(config)
        assert result.status == "pass"

    def test_gemini_key_set(self):
        config = MagicMock()
        config.embedding_provider = "gemini"
        config.gemini_api_key = "test-key"
        result = _check_embedding_api_key(config)
        assert result.status == "pass"

    def test_gemini_key_missing(self):
        config = MagicMock()
        config.embedding_provider = "gemini"
        config.gemini_api_key = None
        result = _check_embedding_api_key(config)
        assert result.status == "fail"
        assert "GEMINI_API_KEY" in result.message

    def test_dashscope_key_set(self):
        config = MagicMock()
        config.embedding_provider = "dashscope"
        config.dashscope_api_key = "test-key"
        result = _check_embedding_api_key(config)
        assert result.status == "pass"

    def test_dashscope_key_missing(self):
        config = MagicMock()
        config.embedding_provider = "dashscope"
        config.dashscope_api_key = None
        result = _check_embedding_api_key(config)
        assert result.status == "fail"
        assert "DASHSCOPE_API_KEY" in result.message

    def test_unknown_provider(self):
        config = MagicMock()
        config.embedding_provider = "unknown"
        result = _check_embedding_api_key(config)
        assert result.status == "fail"
        assert "Unknown provider" in result.message


class TestCheckChromaDbIndex:
    @patch("zotpilot.vector_store.VectorStore")
    @patch("zotpilot.embeddings.create_embedder")
    def test_pass_with_documents(self, mock_create_embedder, mock_vector_store_cls):
        mock_store = MagicMock()
        mock_store.get_indexed_doc_ids.return_value = ["doc1", "doc2"]
        mock_store.count.return_value = 100
        mock_vector_store_cls.return_value = mock_store

        config = MagicMock()
        result = _check_chromadb_index(config)
        assert result.status == "pass"
        assert "2 documents" in result.message
        assert "100 chunks" in result.message

    @patch("zotpilot.vector_store.VectorStore")
    @patch("zotpilot.embeddings.create_embedder")
    def test_warn_when_empty(self, mock_create_embedder, mock_vector_store_cls):
        mock_store = MagicMock()
        mock_store.get_indexed_doc_ids.return_value = []
        mock_store.count.return_value = 0
        mock_vector_store_cls.return_value = mock_store

        config = MagicMock()
        result = _check_chromadb_index(config)
        assert result.status == "warn"
        assert "empty" in result.message.lower()

    @patch("zotpilot.embeddings.create_embedder", side_effect=RuntimeError("bad config"))
    def test_fail_on_error(self, mock_create_embedder):
        config = MagicMock()
        result = _check_chromadb_index(config)
        assert result.status == "fail"
        assert "Cannot open index" in result.message


class TestCheckZoteroWebApi:
    def test_pass_when_both_set(self, monkeypatch):
        monkeypatch.setenv("ZOTERO_API_KEY", "key123")
        monkeypatch.setenv("ZOTERO_USER_ID", "456")
        result = _check_zotero_web_api()
        assert result.status == "pass"

    def test_warn_when_key_missing(self, monkeypatch):
        monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
        monkeypatch.delenv("ZOTERO_USER_ID", raising=False)
        result = _check_zotero_web_api()
        assert result.status == "warn"
        assert "ZOTERO_API_KEY" in result.message

    def test_warn_when_partial(self, monkeypatch):
        monkeypatch.setenv("ZOTERO_API_KEY", "key123")
        monkeypatch.delenv("ZOTERO_USER_ID", raising=False)
        result = _check_zotero_web_api()
        assert result.status == "warn"
        assert "ZOTERO_USER_ID" in result.message


class TestCheckWriteConnectivity:
    @patch("pyzotero.zotero.Zotero")
    def test_pass_with_valid_credentials(self, mock_zotero_cls, monkeypatch):
        mock_zot = MagicMock()
        mock_zotero_cls.return_value = mock_zot

        config = MagicMock()
        config.zotero_api_key = "key123"
        config.zotero_user_id = "456"
        config.zotero_library_type = "user"

        monkeypatch.setenv("ZOTERO_API_KEY", "key123")
        monkeypatch.setenv("ZOTERO_USER_ID", "456")

        result = _check_write_connectivity(config)
        assert result.status == "pass"

    def test_fail_without_credentials(self, monkeypatch):
        config = MagicMock()
        config.zotero_api_key = None
        config.zotero_user_id = None

        monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
        monkeypatch.delenv("ZOTERO_USER_ID", raising=False)

        result = _check_write_connectivity(config)
        assert result.status == "fail"
        assert "missing" in result.message.lower()


class TestRunChecks:
    @patch("zotpilot.doctor._check_zotero_web_api")
    @patch("zotpilot.doctor._check_chromadb_index")
    @patch("zotpilot.doctor._check_embedding_api_key")
    @patch("zotpilot.doctor._check_zotero_data")
    @patch("zotpilot.doctor.Config")
    def test_returns_all_checks(
        self, mock_config_cls, mock_zotero_data, mock_api_key, mock_chroma, mock_web_api, tmp_path
    ):
        config_file = tmp_path / "config.json"
        config_file.write_text('{"embedding_provider": "local"}')

        mock_config_cls.load.return_value = MagicMock()
        mock_zotero_data.return_value = CheckResult("zotero_data", "pass", "ok")
        mock_api_key.return_value = CheckResult("embedding_api_key", "pass", "ok")
        mock_chroma.return_value = CheckResult("chromadb_index", "pass", "ok")
        mock_web_api.return_value = CheckResult("zotero_web_api", "pass", "ok")

        results = run_checks(config_path=str(config_file))
        names = [r.name for r in results]
        assert "python_version" in names
        assert "config_file" in names
        assert "zotero_data" in names
        assert "embedding_api_key" in names
        assert "chromadb_index" in names
        assert "zotero_web_api" in names
        # No write_connectivity without --full
        assert "write_connectivity" not in names

    @patch("zotpilot.doctor._check_write_connectivity")
    @patch("zotpilot.doctor._check_zotero_web_api")
    @patch("zotpilot.doctor._check_chromadb_index")
    @patch("zotpilot.doctor._check_embedding_api_key")
    @patch("zotpilot.doctor._check_zotero_data")
    @patch("zotpilot.doctor.Config")
    def test_full_includes_connectivity(
        self, mock_config_cls, mock_zotero_data, mock_api_key, mock_chroma, mock_web_api, mock_write, tmp_path
    ):
        config_file = tmp_path / "config.json"
        config_file.write_text('{"embedding_provider": "local"}')

        mock_config_cls.load.return_value = MagicMock()
        mock_zotero_data.return_value = CheckResult("zotero_data", "pass", "ok")
        mock_api_key.return_value = CheckResult("embedding_api_key", "pass", "ok")
        mock_chroma.return_value = CheckResult("chromadb_index", "pass", "ok")
        mock_web_api.return_value = CheckResult("zotero_web_api", "pass", "ok")
        mock_write.return_value = CheckResult("write_connectivity", "pass", "ok")

        results = run_checks(config_path=str(config_file), full=True)
        names = [r.name for r in results]
        assert "write_connectivity" in names


class TestCmdDoctorJsonOutput:
    """Test the JSON output format from cmd_doctor."""

    @patch("zotpilot.doctor._check_zotero_web_api")
    @patch("zotpilot.doctor._check_chromadb_index")
    @patch("zotpilot.doctor._check_embedding_api_key")
    @patch("zotpilot.doctor._check_zotero_data")
    @patch("zotpilot.doctor.Config")
    def test_json_output_structure(
        self, mock_config_cls, mock_zotero_data, mock_api_key, mock_chroma, mock_web_api, tmp_path, capsys
    ):
        config_file = tmp_path / "config.json"
        config_file.write_text('{"embedding_provider": "local"}')

        mock_config_cls.load.return_value = MagicMock()
        mock_zotero_data.return_value = CheckResult("zotero_data", "pass", "ok")
        mock_api_key.return_value = CheckResult("embedding_api_key", "pass", "ok")
        mock_chroma.return_value = CheckResult("chromadb_index", "warn", "empty")
        mock_web_api.return_value = CheckResult("zotero_web_api", "warn", "missing")

        from zotpilot.cli import cmd_doctor

        args = MagicMock()
        args.config = str(config_file)
        args.json = True
        args.full = False

        exit_code = cmd_doctor(args)
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert "checks" in data
        assert "summary" in data
        assert isinstance(data["checks"], list)
        assert data["summary"]["fail"] == 0
        # No fail -> exit code 0
        assert exit_code == 0

    @patch("zotpilot.doctor._check_zotero_web_api")
    @patch("zotpilot.doctor._check_chromadb_index")
    @patch("zotpilot.doctor._check_embedding_api_key")
    @patch("zotpilot.doctor._check_zotero_data")
    @patch("zotpilot.doctor.Config")
    def test_exit_code_1_on_fail(
        self, mock_config_cls, mock_zotero_data, mock_api_key, mock_chroma, mock_web_api, tmp_path
    ):
        config_file = tmp_path / "config.json"
        config_file.write_text('{"embedding_provider": "local"}')

        mock_config_cls.load.return_value = MagicMock()
        mock_zotero_data.return_value = CheckResult("zotero_data", "fail", "missing")
        mock_api_key.return_value = CheckResult("embedding_api_key", "pass", "ok")
        mock_chroma.return_value = CheckResult("chromadb_index", "pass", "ok")
        mock_web_api.return_value = CheckResult("zotero_web_api", "pass", "ok")

        from zotpilot.cli import cmd_doctor

        args = MagicMock()
        args.config = str(config_file)
        args.json = False
        args.full = False

        exit_code = cmd_doctor(args)
        assert exit_code == 1

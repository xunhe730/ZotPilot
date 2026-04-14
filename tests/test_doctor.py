"""Tests for ZotPilot doctor health checks."""
import json
import sqlite3
from unittest.mock import MagicMock, patch

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


def _pdf_item(key: str):
    item = MagicMock()
    item.item_key = key
    item.pdf_path = MagicMock()
    item.pdf_path.exists.return_value = True
    return item


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
    @patch("zotpilot.zotero_client.ZoteroClient")
    @patch("zotpilot.vector_store.VectorStore")
    @patch("zotpilot.embeddings.create_embedder")
    def test_pass_with_documents(self, mock_create_embedder, mock_vector_store_cls, mock_zotero_cls):
        mock_store = MagicMock()
        mock_store.get_indexed_doc_ids.return_value = ["doc1", "doc2"]
        mock_store.count_chunks_for_doc_ids.return_value = 100
        mock_vector_store_cls.return_value = mock_store
        zotero = MagicMock()
        zotero.get_all_items_with_pdfs.return_value = [_pdf_item("doc1"), _pdf_item("doc2")]
        mock_zotero_cls.return_value = zotero

        config = MagicMock()
        config.zotero_data_dir = "/fake"
        result = _check_chromadb_index(config)
        assert result.status == "pass"
        assert "2 documents" in result.message
        assert "100 chunks" in result.message

    @patch("zotpilot.zotero_client.ZoteroClient")
    @patch("zotpilot.vector_store.VectorStore")
    @patch("zotpilot.embeddings.create_embedder")
    def test_warn_when_empty(self, mock_create_embedder, mock_vector_store_cls, mock_zotero_cls):
        mock_store = MagicMock()
        mock_store.get_indexed_doc_ids.return_value = []
        mock_store.count_chunks_for_doc_ids.return_value = 0
        mock_vector_store_cls.return_value = mock_store
        zotero = MagicMock()
        zotero.get_all_items_with_pdfs.return_value = []
        mock_zotero_cls.return_value = zotero

        config = MagicMock()
        config.zotero_data_dir = "/fake"
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
    def _make_config(self, api_key=None, user_id=None):
        from unittest.mock import MagicMock
        cfg = MagicMock()
        cfg.zotero_api_key = api_key
        cfg.zotero_user_id = user_id
        return cfg

    def test_pass_when_both_set(self, monkeypatch):
        monkeypatch.setenv("ZOTERO_API_KEY", "key123")
        monkeypatch.setenv("ZOTERO_USER_ID", "456")
        result = _check_zotero_web_api(self._make_config("key123", "456"))
        assert result.status == "pass"

    def test_warn_when_key_missing(self, monkeypatch):
        monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
        monkeypatch.delenv("ZOTERO_USER_ID", raising=False)
        result = _check_zotero_web_api(self._make_config(None, None))
        assert result.status == "warn"
        assert "ZOTERO_API_KEY" in result.message

    def test_warn_when_partial(self, monkeypatch):
        monkeypatch.setenv("ZOTERO_API_KEY", "key123")
        monkeypatch.delenv("ZOTERO_USER_ID", raising=False)
        result = _check_zotero_web_api(self._make_config("key123", None))
        assert result.status == "warn"
        assert "ZOTERO_USER_ID" in result.message
        assert "--zotero-api-key" in result.message
        assert "config set zotero_api_key" not in result.message


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



class TestDivergentRegistration:
    """Tests for divergent registration detection in doctor output.

    Divergent registration occurs when multiple platforms have ZotPilot MCP
    registered but with different credentials (env vars). The approved algorithm
    produces:
    - divergent_registration: boolean
    - divergent_registration_platforms: list of platform names
    - divergent_registration_fields: sorted list of differing credential env keys
    - canonical baseline is the lexicographically first registered supported platform
    """

    def _make_platform_state(self, platform, registered=False, command=None, args=(), env=None):
        """Helper to create a mock PlatformRuntimeState."""
        from unittest.mock import MagicMock
        state = MagicMock()
        state.platform = platform
        state.label = platform
        state.supported = True
        state.detected = True
        state.registered = registered
        state.command = command
        state.args = args
        state.env = env or {}
        state.config_path = f"/fake/{platform}/config"
        state.skill_dirs = ()
        state.skill_hash_ok = True
        state.registration_hash_ok = True
        return state

    def _make_runtime_state(self, platforms_dict):
        """Helper to create a mock RuntimeState from platform configs."""
        from unittest.mock import MagicMock
        runtime = MagicMock()
        runtime.package_version = "0.5.0"
        runtime.supported_targets = tuple(platforms_dict.keys())
        runtime.platforms = {}
        for plat, config in platforms_dict.items():
            runtime.platforms[plat] = self._make_platform_state(plat, **config)
        return runtime

    def test_divergent_registration_boolean_true(self, monkeypatch):
        """Test that divergent_registration is True when credentials differ."""
        platforms = {
            "claude-code": {
                "registered": True,
                "command": "/usr/bin/zotpilot",
                "env": {"GEMINI_API_KEY": "key-a"},
            },
            "opencode": {
                "registered": True,
                "command": "/usr/bin/zotpilot",
                "env": {"GEMINI_API_KEY": "key-b"},
            },
        }
        mock_state = self._make_runtime_state(platforms)

        with patch("zotpilot._platforms.inspect_current_state", return_value=mock_state):
            # Test that _deployment_status includes divergent_registration
            from unittest.mock import MagicMock

            from zotpilot._platforms import _deployment_status

            mock_config = MagicMock()
            mock_config.gemini_api_key = None
            mock_config.dashscope_api_key = None
            mock_config.zotero_api_key = None
            mock_config.zotero_user_id = None

            status = _deployment_status(mock_config)

            # This will FAIL until divergent_registration is added to _deployment_status
            assert "divergent_registration" in status
            assert status["divergent_registration"] is True

    def test_divergent_registration_platforms_list(self, monkeypatch):
        """Test that divergent_registration_platforms contains all registered platforms."""
        platforms = {
            "claude-code": {
                "registered": True,
                "command": "/usr/bin/zotpilot",
                "env": {"GEMINI_API_KEY": "key-a"},
            },
            "opencode": {
                "registered": True,
                "command": "/usr/bin/zotpilot",
                "env": {"GEMINI_API_KEY": "key-b"},
            },
            "codex": {
                "registered": False,
                "command": None,
                "env": {},
            },
        }
        mock_state = self._make_runtime_state(platforms)

        with patch("zotpilot._platforms.inspect_current_state", return_value=mock_state):
            from unittest.mock import MagicMock

            from zotpilot._platforms import _deployment_status

            mock_config = MagicMock()
            mock_config.gemini_api_key = None
            mock_config.dashscope_api_key = None
            mock_config.zotero_api_key = None
            mock_config.zotero_user_id = None

            status = _deployment_status(mock_config)

            # This will FAIL until divergent_registration_platforms is added
            assert "divergent_registration_platforms" in status
            assert set(status["divergent_registration_platforms"]) == {"claude-code", "opencode"}

    def test_divergent_registration_fields_sorted(self, monkeypatch):
        """Test that divergent_registration_fields is a sorted list of differing env keys."""
        platforms = {
            "claude-code": {
                "registered": True,
                "command": "/usr/bin/zotpilot",
                "env": {
                    "GEMINI_API_KEY": "key-a",
                    "ZOTERO_API_KEY": "zot-a",
                    "ZOTERO_USER_ID": "123",
                },
            },
            "opencode": {
                "registered": True,
                "command": "/usr/bin/zotpilot",
                "env": {
                    "GEMINI_API_KEY": "key-b",
                    "ZOTERO_API_KEY": "zot-b",
                    "ZOTERO_USER_ID": "123",
                },
            },
        }
        mock_state = self._make_runtime_state(platforms)

        with patch("zotpilot._platforms.inspect_current_state", return_value=mock_state):
            from unittest.mock import MagicMock

            from zotpilot._platforms import _deployment_status

            mock_config = MagicMock()
            mock_config.gemini_api_key = None
            mock_config.dashscope_api_key = None
            mock_config.zotero_api_key = None
            mock_config.zotero_user_id = None

            status = _deployment_status(mock_config)

            # This will FAIL until divergent_registration_fields is added
            assert "divergent_registration_fields" in status
            assert status["divergent_registration_fields"] == ["GEMINI_API_KEY", "ZOTERO_API_KEY"]

    def test_no_divergence_when_credentials_identical(self, monkeypatch):
        """Test that divergent_registration is False when all platforms have same creds."""
        common_env = {
            "GEMINI_API_KEY": "same-key",
            "ZOTERO_API_KEY": "zotero-key",
        }
        platforms = {
            "claude-code": {"registered": True, "command": "/usr/bin/zotpilot", "env": dict(common_env)},
            "opencode": {"registered": True, "command": "/usr/bin/zotpilot", "env": dict(common_env)},
        }
        mock_state = self._make_runtime_state(platforms)

        with patch("zotpilot._platforms.inspect_current_state", return_value=mock_state):
            from unittest.mock import MagicMock

            from zotpilot._platforms import _deployment_status

            mock_config = MagicMock()
            mock_config.gemini_api_key = None
            mock_config.dashscope_api_key = None
            mock_config.zotero_api_key = None
            mock_config.zotero_user_id = None

            status = _deployment_status(mock_config)

            assert "divergent_registration" in status
            assert status["divergent_registration"] is False


class TestDoctorDivergentRegistrationFail:
    """Test doctor fails when divergent credentials are detected."""

    @patch("zotpilot.doctor._check_zotero_web_api")
    @patch("zotpilot.doctor._check_chromadb_index")
    @patch("zotpilot.doctor._check_embedding_api_key")
    @patch("zotpilot.doctor._check_zotero_data")
    @patch("zotpilot.doctor.Config")
    def test_doctor_fail_on_divergent_credentials(
        self, mock_config_cls, mock_zotero_data, mock_api_key, mock_chroma, mock_web_api,
        tmp_path, capsys, monkeypatch
    ):
        """Doctor should return exit code 1 when divergent registration detected."""
        from unittest.mock import MagicMock

        from zotpilot.doctor import CheckResult

        mock_config_cls.load.return_value = MagicMock()
        mock_zotero_data.return_value = CheckResult("zotero_data", "pass", "ok")
        mock_api_key.return_value = CheckResult("embedding_api_key", "pass", "ok")
        mock_chroma.return_value = CheckResult("chromadb_index", "pass", "ok")
        mock_web_api.return_value = CheckResult("zotero_web_api", "pass", "ok")

        def mock_inspect_current_state(config_env=None, targets=None):
            runtime = MagicMock()
            runtime.package_version = "0.5.0"
            runtime.supported_targets = ("claude-code", "opencode")
            runtime.platforms = {
                "claude-code": MagicMock(
                    platform="claude-code", label="Claude Code", supported=True,
                    detected=True, registered=True, command="/usr/bin/zotpilot",
                    args=(), env={"GEMINI_API_KEY": "key-a"}, config_path="/fake/claude",
                    skill_dirs=(), skill_hash_ok=True, registration_hash_ok=True,
                ),
                "opencode": MagicMock(
                    platform="opencode", label="OpenCode", supported=True,
                    detected=True, registered=True, command="/usr/bin/zotpilot",
                    args=(), env={"GEMINI_API_KEY": "key-b"}, config_path="/fake/opencode",
                    skill_dirs=(), skill_hash_ok=True, registration_hash_ok=True,
                ),
            }
            return runtime

        with patch("zotpilot._platforms.inspect_current_state", side_effect=mock_inspect_current_state):
            from zotpilot.cli import cmd_doctor

            config_file = tmp_path / "config.json"
            config_file.write_text('{"embedding_provider": "gemini", "gemini_api_key": "key-a"}')

            args = MagicMock()
            args.config = str(config_file)
            args.json = True
            args.full = False

            exit_code = cmd_doctor(args)
            captured = capsys.readouterr()

            # Expected: exit_code = 1 due to divergent registration
            assert exit_code == 1, "Doctor should fail when divergent credentials detected"

            data = json.loads(captured.out)
            assert "divergent_registration" in data


class TestDoctorDivergentRegistrationWarn:
    """Test doctor warns when command/args drift without credential divergence."""

    @patch("zotpilot.doctor._check_zotero_web_api")
    @patch("zotpilot.doctor._check_chromadb_index")
    @patch("zotpilot.doctor._check_embedding_api_key")
    @patch("zotpilot.doctor._check_zotero_data")
    @patch("zotpilot.doctor.Config")
    def test_doctor_warn_on_command_drift_no_credential_divergence(
        self, mock_config_cls, mock_zotero_data, mock_api_key, mock_chroma, mock_web_api,
        tmp_path, capsys, monkeypatch
    ):
        """Doctor should warn (not fail) when commands drift but credentials are same."""
        from unittest.mock import MagicMock

        from zotpilot.doctor import CheckResult

        mock_config_cls.load.return_value = MagicMock()
        mock_zotero_data.return_value = CheckResult("zotero_data", "pass", "ok")
        mock_api_key.return_value = CheckResult("embedding_api_key", "pass", "ok")
        mock_chroma.return_value = CheckResult("chromadb_index", "pass", "ok")
        mock_web_api.return_value = CheckResult("zotero_web_api", "pass", "ok")

        def mock_inspect_current_state(config_env=None, targets=None):
            runtime = MagicMock()
            runtime.package_version = "0.5.0"
            runtime.supported_targets = ("claude-code", "opencode")
            runtime.platforms = {
                "claude-code": MagicMock(
                    platform="claude-code", label="Claude Code", supported=True,
                    detected=True, registered=True, command="/usr/bin/zotpilot",
                    args=(), env={"GEMINI_API_KEY": "same-key"}, config_path="/fake/claude",
                    skill_dirs=(), skill_hash_ok=True, registration_hash_ok=False,
                ),
                "opencode": MagicMock(
                    platform="opencode", label="OpenCode", supported=True,
                    detected=True, registered=True, command="/usr/local/bin/zotpilot",
                    args=(), env={"GEMINI_API_KEY": "same-key"}, config_path="/fake/opencode",
                    skill_dirs=(), skill_hash_ok=True, registration_hash_ok=False,
                ),
            }
            return runtime

        with patch("zotpilot._platforms.inspect_current_state", side_effect=mock_inspect_current_state):
            from zotpilot.cli import cmd_doctor

            config_file = tmp_path / "config.json"
            config_file.write_text('{"embedding_provider": "gemini", "gemini_api_key": "same-key"}')

            args = MagicMock()
            args.config = str(config_file)
            args.json = True
            args.full = False

            exit_code = cmd_doctor(args)
            captured = capsys.readouterr()

            # Expected: exit_code = 0 (warn, not fail) since credentials are identical
            assert exit_code == 0, "Doctor should only warn (exit 0) when commands drift but credentials match"

            data = json.loads(captured.out)
            assert "checks" in data

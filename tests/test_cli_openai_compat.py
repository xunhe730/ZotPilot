"""Tests for the openai-compatible CLI surface.

Covers the setup wizard preset sub-menu (U1), the `_probe_endpoint` connectivity
self-check (U2 / Step 5.15), non-interactive flag handling (U4 / Step 5.12), the
`{env:}` coercion bypass (H3 / Step 5.10), and the lightweight `config set`
index-bound warning (Decision 8 / Step 5.11).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx

from zotpilot.cli import (
    ProbeResult,
    _coerce_value,
    _probe_endpoint,
    cmd_config,
    cmd_setup,
)


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
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "ZOTPILOT_EMBEDDING_API_KEY",
        "ZOTPILOT_EMBEDDING_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ZOTPILOT_SECRET_BACKEND", "local-file")
    monkeypatch.setenv("ZOTPILOT_LOCAL_SECRETS_PATH", str(secrets_path))
    return secrets_path


def _mock_httpx_client(*, json_data=None, side_effect=None):
    """Build a context-manager httpx.Client mock for `_probe_endpoint`."""
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    if side_effect is not None:
        client.post.side_effect = side_effect
    else:
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = json_data
        client.post.return_value = response
    return client


class TestProbeEndpoint:
    def test_probe_ok_when_dimension_matches(self):
        client = _mock_httpx_client(
            json_data={"data": [{"index": 0, "embedding": [0.1] * 768}]}
        )
        with patch("httpx.Client", return_value=client):
            result = _probe_endpoint("http://localhost:11434/v1", None, "nomic-embed-text", 768)
        assert result.ok is True
        assert result.returned_dim == 768

    def test_probe_reports_dimension_mismatch(self):
        client = _mock_httpx_client(
            json_data={"data": [{"index": 0, "embedding": [0.1] * 512}]}
        )
        with patch("httpx.Client", return_value=client):
            result = _probe_endpoint("https://api.example.com/v1", "key", "model", 1024)
        assert result.ok is False
        assert result.returned_dim == 512
        assert "512" in result.message and "1024" in result.message

    def test_probe_connect_error_gives_ollama_hint(self):
        client = _mock_httpx_client(side_effect=httpx.ConnectError("refused"))
        with patch("httpx.Client", return_value=client):
            result = _probe_endpoint("http://localhost:11434/v1", None, "nomic-embed-text", 768)
        assert result.ok is False
        assert result.returned_dim is None
        assert "is the server running" in result.message.lower()
        assert "nomic-embed-text" in result.message

    def test_probe_omits_auth_header_when_no_key(self):
        client = _mock_httpx_client(
            json_data={"data": [{"index": 0, "embedding": [0.1] * 768}]}
        )
        with patch("httpx.Client", return_value=client):
            _probe_endpoint("http://localhost:11434/v1", None, "nomic-embed-text", 768)
        _, kwargs = client.post.call_args
        assert "Authorization" not in kwargs["headers"]


class TestInteractiveWizard:
    def test_ollama_preset_skips_key_prompt(self, tmp_path, monkeypatch, capsys):
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
                "zotpilot.cli._probe_endpoint",
                return_value=ProbeResult(True, "Connectivity OK", 768),
            ) as mock_probe,
            patch(
                "builtins.input",
                # use-detected, provider=4, preset=3 (Ollama), base_url keep,
                # model keep, dims keep, skip zotero write creds
                side_effect=["", "4", "3", "", "", "", "n"],
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
        assert "No API key needed for local Ollama" in out
        mock_probe.assert_called_once()
        data = json.loads((config_dir / "config.json").read_text())
        assert data["embedding_provider"] == "openai-compatible"
        assert data["embedding_base_url"] == "http://localhost:11434/v1"
        assert data["embedding_model"] == "nomic-embed-text"
        assert data["embedding_dimensions"] == 768
        assert "embedding_api_key" not in data


class TestNonInteractiveOpenAICompat:
    def test_missing_dimensions_exits_nonzero(self, tmp_path, monkeypatch, capsys):
        _use_local_secrets(monkeypatch, tmp_path)
        zotero_dir = _make_fake_zotero(tmp_path)

        args = type(
            "Args",
            (),
            {
                "non_interactive": True,
                "zotero_dir": str(zotero_dir),
                "provider": "openai-compatible",
                "embedding_base_url": "https://api.siliconflow.cn/v1",
                "embedding_model": "BAAI/bge-m3",
                "embedding_dimensions": None,
                "embedding_key": "sk-x",
                "gemini_key": None,
                "dashscope_key": None,
            },
        )()
        rc = cmd_setup(args)
        assert rc == 1
        assert "--embedding-dimensions" in capsys.readouterr().err

    def test_complete_flags_write_config(self, tmp_path, monkeypatch):
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
                    "provider": "openai-compatible",
                    "embedding_base_url": "https://api.siliconflow.cn/v1",
                    "embedding_model": "BAAI/bge-m3",
                    "embedding_dimensions": 1024,
                    "embedding_key": "sk-x",
                    "gemini_key": None,
                    "dashscope_key": None,
                },
            )()
            rc = cmd_setup(args)

        assert rc == 0
        data = json.loads((config_dir / "config.json").read_text())
        assert data["embedding_provider"] == "openai-compatible"
        assert data["embedding_base_url"] == "https://api.siliconflow.cn/v1"
        assert data["embedding_model"] == "BAAI/bge-m3"
        assert data["embedding_dimensions"] == 1024
        assert data["embedding_api_key"] == "sk-x"


class TestCoerceEnvBypass:
    def test_env_ref_round_trips_as_literal(self):
        assert _coerce_value("embedding_api_key", "{env:OPENAI_API_KEY}") == "{env:OPENAI_API_KEY}"

    def test_env_ref_base_url_round_trips(self):
        assert _coerce_value("embedding_base_url", "{env:OPENAI_BASE_URL}") == "{env:OPENAI_BASE_URL}"

    def test_real_json_still_parsed(self):
        assert _coerce_value("rerank_section_weights", '{"intro": 1.0}') == {"intro": 1.0}


class TestConfigSetWarning:
    def _make_config(self, tmp_path: Path) -> tuple[Path, Path]:
        chroma_dir = tmp_path / "chroma"
        chroma_dir.mkdir()
        (chroma_dir / "chroma.sqlite3").write_text("data")  # make it non-empty
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "zotero_data_dir": str(tmp_path / "zotero"),
                    "chroma_db_path": str(chroma_dir),
                    "embedding_provider": "gemini",
                }
            )
        )
        return cfg_path, chroma_dir

    def _set(self, cfg_path: Path, key: str, value: str, capsys):
        args = SimpleNamespace(config=str(cfg_path), config_subcmd="set", key=key, value=value)
        rc = cmd_config(args)
        return rc, capsys.readouterr().out

    def test_index_bound_field_warns(self, tmp_path, monkeypatch, capsys):
        _use_local_secrets(monkeypatch, tmp_path)
        cfg_path, _ = self._make_config(tmp_path)
        rc, out = self._set(cfg_path, "embedding_model", "BAAI/bge-m3", capsys)
        assert rc == 0
        assert "index --force" in out

    def test_free_field_does_not_warn(self, tmp_path, monkeypatch, capsys):
        _use_local_secrets(monkeypatch, tmp_path)
        cfg_path, _ = self._make_config(tmp_path)
        rc, out = self._set(cfg_path, "rerank_alpha", "0.5", capsys)
        assert rc == 0
        assert "index --force" not in out

    def test_embedding_api_key_does_not_warn(self, tmp_path, monkeypatch, capsys):
        _use_local_secrets(monkeypatch, tmp_path)
        cfg_path, _ = self._make_config(tmp_path)
        rc, out = self._set(cfg_path, "embedding_api_key", "sk-secret", capsys)
        assert rc == 0
        assert "index --force" not in out

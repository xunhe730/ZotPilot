"""Tests for the openai-compatible CLI surface.

Covers the setup wizard preset sub-menu (U1), the `_probe_endpoint` connectivity
self-check (U2 / Step 5.15), non-interactive flag handling (U4 / Step 5.12), the
`{env:}` coercion bypass (H3 / Step 5.10), and the lightweight `config set`
index-bound warning (Decision 8 / Step 5.11).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx

from zotpilot import cli, providers
from zotpilot.cli import (
    ProbeResult,
    _coerce_value,
    _probe_endpoint,
    cmd_config,
    cmd_setup,
)

_SKILL_PATH = Path(__file__).resolve().parents[1] / "src" / "zotpilot" / "skills" / "ztp-setup.md"


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


def _setup_args(**overrides):
    """Build a cmd_setup args object with ALL attributes it reads via getattr."""
    base = {
        "non_interactive": False,
        "zotero_dir": None,
        "provider": None,
        "embedding_base_url": None,
        "embedding_model": None,
        "embedding_dimensions": None,
        "embedding_key": None,
        "gemini_key": None,
        "dashscope_key": None,
        "list_vendors": False,
        "json": False,
        "verify": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestInteractiveWizard:
    def test_ollama_two_step_skips_key_prompt(self, tmp_path, monkeypatch, capsys):
        _use_local_secrets(monkeypatch, tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        zotero_dir = _make_fake_zotero(tmp_path)
        config_dir = tmp_path / ".config" / "zotpilot"
        # Dynamically computed vendor index — adding/reordering vendors can't break it.
        ollama_idx = next(
            i for i, v in enumerate(providers.VENDOR_CATALOG, 1) if v.key == "ollama"
        )

        with (
            patch("zotpilot.config._default_config_dir", return_value=config_dir),
            patch("zotpilot.config._default_data_dir", return_value=tmp_path / "data"),
            patch("zotpilot.config._old_config_path", return_value=tmp_path / "old" / "config.json"),
            patch("zotpilot.zotero_detector.detect_zotero_data_dir", return_value=zotero_dir),
            patch("zotpilot._platforms.register", return_value={"codex": True}),
            patch(
                "builtins.input",
                # use-detected zotero, vendor=ollama, base_url keep, model
                # Enter=recommended, self-check=n (skip), zotero-write=n.
                side_effect=["", str(ollama_idx), "", "", "n", "n"],
            ),
        ):
            rc = cmd_setup(_setup_args())

        assert rc == 0
        out = capsys.readouterr().out
        assert "No API key needed for local Ollama" in out
        data = json.loads((config_dir / "config.json").read_text())
        assert data["embedding_provider"] == "openai-compatible"
        assert data["embedding_base_url"] == "http://localhost:11434/v1"
        assert data["embedding_model"] == "nomic-embed-text"
        assert data["embedding_dimensions"] == 768
        assert not data.get("embedding_api_key")


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


def _run_noninteractive(tmp_path, monkeypatch, **provider_overrides):
    """Drive cmd_setup non-interactively against a sandboxed config dir.

    Returns ``(rc, config_dict_or_{}, capsys_unavailable)``; read config via the
    returned dict. The caller passes provider/model/dims/verify overrides.
    """
    _use_local_secrets(monkeypatch, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    zotero_dir = _make_fake_zotero(tmp_path)
    config_dir = tmp_path / ".config" / "zotpilot"
    args = _setup_args(non_interactive=True, zotero_dir=str(zotero_dir), **provider_overrides)
    with (
        patch("zotpilot.config._default_config_dir", return_value=config_dir),
        patch("zotpilot.config._default_data_dir", return_value=tmp_path / "data"),
        patch("zotpilot.config._old_config_path", return_value=tmp_path / "old" / "config.json"),
        patch("zotpilot._platforms.register", return_value={"codex": True}),
    ):
        rc = cmd_setup(args)
    cfg = config_dir / "config.json"
    return rc, (json.loads(cfg.read_text()) if cfg.exists() else {})


class TestNonInteractiveVendor:
    def test_siliconflow_no_model_uses_recommended(self, tmp_path, monkeypatch):
        rc, data = _run_noninteractive(tmp_path, monkeypatch, provider="siliconflow")
        assert rc == 0
        assert data["embedding_provider"] == "openai-compatible"
        assert data["embedding_base_url"] == "https://api.siliconflow.cn/v1"
        assert data["embedding_model"] == "BAAI/bge-m3"
        assert data["embedding_dimensions"] == 1024

    def test_zhipu_recommended_defaults(self, tmp_path, monkeypatch):
        rc, data = _run_noninteractive(tmp_path, monkeypatch, provider="zhipu")
        assert rc == 0
        assert data["embedding_model"] == "embedding-3"
        assert data["embedding_dimensions"] == 2048
        assert data["embedding_base_url"] == "https://open.bigmodel.cn/api/paas/v4"

    def test_custom_without_dimensions_exits_nonzero(self, tmp_path, monkeypatch, capsys):
        rc, _ = _run_noninteractive(
            tmp_path, monkeypatch, provider="custom",
            embedding_base_url="http://x/v1", embedding_model="m",
        )
        assert rc == 1
        assert "--embedding-dimensions" in capsys.readouterr().err

    def test_openai_compatible_alias_equals_custom(self, tmp_path, monkeypatch):
        common = dict(embedding_base_url="http://x/v1", embedding_model="m",
                      embedding_dimensions=10)
        dir_a, dir_b = tmp_path / "a", tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        _, d1 = _run_noninteractive(dir_a, monkeypatch, provider="openai-compatible", **common)
        _, d2 = _run_noninteractive(dir_b, monkeypatch, provider="custom", **common)
        keys = ("embedding_provider", "embedding_base_url", "embedding_model", "embedding_dimensions")
        assert {k: d1.get(k) for k in keys} == {k: d2.get(k) for k in keys}

    def test_legacy_gemini_unchanged(self, tmp_path, monkeypatch):
        rc, data = _run_noninteractive(tmp_path, monkeypatch, provider="gemini")
        assert rc == 0
        assert data["embedding_provider"] == "gemini"
        assert data["embedding_model"] == "gemini-embedding-001"
        assert data["embedding_dimensions"] == 768

    def test_omitted_provider_defaults_to_gemini(self, tmp_path, monkeypatch):
        rc, data = _run_noninteractive(tmp_path, monkeypatch)  # no provider
        assert rc == 0
        assert data["embedding_provider"] == "gemini"
        assert data["embedding_model"] == "gemini-embedding-001"


class TestListVendors:
    def test_json_envelope_matches_catalog(self, capsys):
        rc = cli._print_vendor_catalog(as_json=True)
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["schema_version"] == 1
        assert [v["key"] for v in payload["vendors"]] == [
            v.key for v in providers.VENDOR_CATALOG
        ]
        sf = next(v for v in payload["vendors"] if v["key"] == "siliconflow")
        assert {"model": "BAAI/bge-m3", "dimensions": 1024, "note": "multilingual · cheapest",
                "recommended": True} in sf["models"]

    def test_payload_equals_catalog_programmatically(self):
        payload = cli._vendor_catalog_payload()
        assert payload["schema_version"] == 1
        assert len(payload["vendors"]) == len(providers.VENDOR_CATALOG)
        for vd, v in zip(payload["vendors"], providers.VENDOR_CATALOG):
            assert vd["key"] == v.key
            assert vd["provider"] == v.provider
            assert vd["base_url"] == v.base_url
            assert [m["model"] for m in vd["models"]] == [m.model for m in v.models]

    def test_human_table_non_empty(self, capsys):
        cli._print_vendor_catalog(as_json=False)
        out = capsys.readouterr().out
        assert "google" in out and "siliconflow" in out and out.strip()

    def test_list_vendors_short_circuits_before_zotero(self):
        # No zotero_dir, no detection patch: --list-vendors must still succeed.
        args = _setup_args(list_vendors=True, json=True)
        with patch("builtins.print"):
            rc = cmd_setup(args)
        assert rc == 0


class TestVerifyTaxonomy:
    @staticmethod
    def _run_verify(tmp_path, monkeypatch, capsys, probe=None, **over):
        _use_local_secrets(monkeypatch, tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        zotero_dir = _make_fake_zotero(tmp_path)
        config_dir = tmp_path / ".config" / "zotpilot"
        args = _setup_args(non_interactive=True, zotero_dir=str(zotero_dir), verify=True, **over)
        cms = [
            patch("zotpilot.config._default_config_dir", return_value=config_dir),
            patch("zotpilot.config._default_data_dir", return_value=tmp_path / "data"),
            patch("zotpilot.config._old_config_path", return_value=tmp_path / "old" / "c.json"),
            patch("zotpilot._platforms.register", return_value={"codex": True}),
        ]
        mock_probe = None
        if probe is not None:
            mock_probe = patch("zotpilot.cli._probe_endpoint", return_value=probe)
            cms.append(mock_probe)
        from contextlib import ExitStack
        with ExitStack() as st:
            patches = [st.enter_context(c) for c in cms]
            rc = cmd_setup(args)
        out = capsys.readouterr().out
        lines = [ln for ln in out.splitlines() if ln.strip().startswith("{")]
        payload = json.loads(lines[-1]) if lines else None
        return rc, payload, (patches[-1] if probe is not None else None)

    def test_ok_exits_zero(self, tmp_path, monkeypatch, capsys):
        rc, p, _ = self._run_verify(
            tmp_path, monkeypatch, capsys, ProbeResult(True, "ok", 1024, "ok"),
            provider="siliconflow",
        )
        assert rc == 0 and p["verify"] == "ok"

    def test_dim_mismatch_exits_nonzero_and_names_dim(self, tmp_path, monkeypatch, capsys):
        rc, p, _ = self._run_verify(
            tmp_path, monkeypatch, capsys,
            ProbeResult(False, "mismatch 512", 512, "dim_mismatch"),
            provider="siliconflow",
        )
        assert rc == 1 and p["verify"] == "dim_mismatch" and p["returned_dim"] == 512

    def test_auth_exits_zero(self, tmp_path, monkeypatch, capsys):
        rc, p, _ = self._run_verify(
            tmp_path, monkeypatch, capsys, ProbeResult(False, "auth", None, "auth"),
            provider="siliconflow",
        )
        assert rc == 0 and p["verify"] == "auth"

    def test_unreachable_exits_zero(self, tmp_path, monkeypatch, capsys):
        rc, p, _ = self._run_verify(
            tmp_path, monkeypatch, capsys,
            ProbeResult(False, "down", None, "unreachable"), provider="siliconflow",
        )
        assert rc == 0 and p["verify"] == "unreachable"

    def test_gemini_is_skipped_without_probe(self, tmp_path, monkeypatch, capsys):
        rc, p, mock_probe = self._run_verify(
            tmp_path, monkeypatch, capsys, probe=None, provider="gemini",
        )
        # No probe patched; skipped path never calls _probe_endpoint.
        assert rc == 0 and p["verify"] == "skipped"

    def test_without_verify_probe_never_called(self, tmp_path, monkeypatch):
        _use_local_secrets(monkeypatch, tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        zotero_dir = _make_fake_zotero(tmp_path)
        config_dir = tmp_path / ".config" / "zotpilot"
        args = _setup_args(non_interactive=True, zotero_dir=str(zotero_dir), provider="siliconflow")
        with (
            patch("zotpilot.config._default_config_dir", return_value=config_dir),
            patch("zotpilot.config._default_data_dir", return_value=tmp_path / "data"),
            patch("zotpilot.config._old_config_path", return_value=tmp_path / "old" / "c.json"),
            patch("zotpilot._platforms.register", return_value={"codex": True}),
            patch("zotpilot.cli._probe_endpoint") as mock_probe,
        ):
            rc = cmd_setup(args)
        assert rc == 0
        mock_probe.assert_not_called()


def _mk_status_response(status: int, text: str = "bad"):
    resp = MagicMock()
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        f"{status}", request=MagicMock(), response=MagicMock(status_code=status, text=text)
    )
    return resp


class TestProbeDimensionsDropFallback:
    def test_400_with_dims_then_success_without(self):
        # First POST (with dimensions) 400s; retry without dimensions returns 1024.
        ok = MagicMock()
        ok.raise_for_status.return_value = None
        ok.json.return_value = {"data": [{"index": 0, "embedding": [0.1] * 1024}]}
        client = MagicMock()
        client.__enter__.return_value = client
        client.__exit__.return_value = False
        client.post.side_effect = [_mk_status_response(400), ok]
        with patch("httpx.Client", return_value=client):
            res = _probe_endpoint("https://api.siliconflow.cn/v1", "k", "BAAI/bge-m3", 1024)
        assert res.ok is True and res.state == "ok" and res.returned_dim == 1024

    def test_401_classified_as_auth(self):
        client = MagicMock()
        client.__enter__.return_value = client
        client.__exit__.return_value = False
        client.post.return_value = _mk_status_response(401)
        with patch("httpx.Client", return_value=client):
            res = _probe_endpoint("https://x/v1", "k", "m", 10)
        assert res.state == "auth" and res.ok is False

    def test_connect_timeout_classified_as_unreachable(self):
        # ConnectTimeout is a TimeoutException, NOT a ConnectError (Critic M1).
        client = MagicMock()
        client.__enter__.return_value = client
        client.__exit__.return_value = False
        client.post.side_effect = httpx.ConnectTimeout("timed out")
        with patch("httpx.Client", return_value=client):
            res = _probe_endpoint("https://x/v1", "k", "m", 10)
        assert res.state == "unreachable" and res.ok is False


class TestSkillHasNoHardcodedProviderList:
    """AC16: future-proof negative grep on the ztp-setup skill."""

    def test_no_hardcoded_provider_examples(self):
        text = _SKILL_PATH.read_text(encoding="utf-8")
        pattern = re.compile(r"--provider\s+\[?(gemini|dashscope|local|openai-compatible)")
        offenders = [ln for ln in text.splitlines() if pattern.search(ln)]
        assert not offenders, f"hardcoded provider list reintroduced: {offenders}"

    def test_references_list_vendors(self):
        assert "list-vendors" in _SKILL_PATH.read_text(encoding="utf-8")


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

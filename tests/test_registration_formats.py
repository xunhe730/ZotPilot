"""MCP registration format tests: write → inspect → drift detection roundtrip."""
from __future__ import annotations
import json, os, sys
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

class TestClaudeCodeRegistration:
    def test_register_command_format(self):
        from zotpilot._platforms import _register_claude_code
        with patch("zotpilot._platforms._runtime_invocation", return_value=("/usr/bin/zotpilot",("mcp","serve"))), \
             patch("zotpilot._platforms._backup_config_file"), \
             patch("zotpilot._platforms.subprocess.run") as mr:
            mr.return_value = MagicMock(returncode=0, stderr="")
            assert _register_claude_code({}) is True
        assert mr.call_count == 2
        assert mr.call_args_list[1].args[0] == ["claude","mcp","add","--scope","user","zotpilot","--","/usr/bin/zotpilot","mcp","serve"]
    def test_register_failure(self, capsys):
        from zotpilot._platforms import _register_claude_code
        with patch("zotpilot._platforms._runtime_invocation", return_value=("/usr/bin/zotpilot",("mcp","serve"))), \
             patch("zotpilot._platforms._backup_config_file"), \
             patch("zotpilot._platforms.subprocess.run", return_value=MagicMock(returncode=1, stderr="err")):
            assert _register_claude_code({}) is False

class TestCodexRegistration:
    def test_register_command_format(self):
        from zotpilot._platforms import _register_codex
        with patch("zotpilot._platforms._runtime_invocation", return_value=("/usr/bin/zotpilot",("mcp","serve"))), \
             patch("zotpilot._platforms._backup_config_file"), \
             patch("zotpilot._platforms.subprocess.run") as mr:
            mr.return_value = MagicMock(returncode=0, stderr="")
            assert _register_codex({}) is True
        assert mr.call_args_list[1].args[0] == ["codex","mcp","add","zotpilot","--","/usr/bin/zotpilot","mcp","serve"]

class TestOpenCodeConfigWrite:
    def test_write_new_config(self, tmp_path):
        from zotpilot._platforms import _write_mcp_config
        cp = tmp_path/"opencode.json"
        with patch("zotpilot._platforms._runtime_invocation", return_value=("/usr/bin/zotpilot",("mcp","serve"))):
            assert _write_mcp_config(cp, {}) is True
        d = json.loads(cp.read_text())
        assert d["mcp"]["zotpilot"]["type"] == "local"
        assert d["mcp"]["zotpilot"]["command"] == ["/usr/bin/zotpilot","mcp","serve"]
    def test_write_preserves_other_servers(self, tmp_path):
        from zotpilot._platforms import _write_mcp_config
        cp = tmp_path/"opencode.json"
        cp.write_text(json.dumps({"mcp":{"other":{"type":"local","command":["/bin/other"]}}}))
        with patch("zotpilot._platforms._runtime_invocation", return_value=("/usr/bin/zotpilot",("mcp","serve"))):
            assert _write_mcp_config(cp, {}) is True
        d = json.loads(cp.read_text())
        assert "other" in d["mcp"] and "zotpilot" in d["mcp"]
    def test_write_sets_timeout(self, tmp_path):
        from zotpilot._platforms import _write_mcp_config
        cp = tmp_path/"opencode.json"
        with patch("zotpilot._platforms._runtime_invocation", return_value=("/usr/bin/zotpilot",("mcp","serve"))):
            _write_mcp_config(cp, {})
        assert json.loads(cp.read_text())["experimental"]["mcp_timeout"] == 600000
    def test_write_preserves_existing_timeout(self, tmp_path):
        from zotpilot._platforms import _write_mcp_config
        cp = tmp_path/"opencode.json"
        cp.write_text(json.dumps({"experimental":{"mcp_timeout":120000}}))
        with patch("zotpilot._platforms._runtime_invocation", return_value=("/usr/bin/zotpilot",("mcp","serve"))):
            _write_mcp_config(cp, {})
        assert json.loads(cp.read_text())["experimental"]["mcp_timeout"] == 120000
    def test_write_empty_config(self, tmp_path):
        from zotpilot._platforms import _write_mcp_config
        cp = tmp_path/"opencode.json"; cp.write_text("")
        with patch("zotpilot._platforms._runtime_invocation", return_value=("/usr/bin/zotpilot",("mcp","serve"))):
            assert _write_mcp_config(cp, {}) is True
    def test_write_malformed_returns_false(self, tmp_path):
        from zotpilot._platforms import _write_mcp_config
        cp = tmp_path/"opencode.json"; cp.write_text("{bad")
        with patch("zotpilot._platforms._runtime_invocation", return_value=("/usr/bin/zotpilot",("mcp","serve"))):
            assert _write_mcp_config(cp, {}) is False
        assert cp.with_suffix(".json.bak").exists()
    def test_unix_permissions(self, tmp_path):
        from zotpilot._platforms import _write_mcp_config
        cp = tmp_path/"opencode.json"
        with patch("zotpilot._platforms._runtime_invocation", return_value=("/usr/bin/zotpilot",("mcp","serve"))), \
             patch.object(sys, "platform", "darwin"):
            _write_mcp_config(cp, {})
        import stat
        assert stat.S_IMODE(cp.stat().st_mode) == 0o600

class TestOpenCodeInspectRoundtrip:
    def test_write_then_inspect_string(self, tmp_path, monkeypatch):
        from zotpilot._platforms import _write_mcp_config, _inspect_registration
        cp = tmp_path/"opencode.json"
        with patch("zotpilot._platforms._runtime_invocation", return_value=("/usr/bin/zotpilot",("mcp","serve"))):
            _write_mcp_config(cp, {})
        import zotpilot._platforms as _p
        monkeypatch.setattr(_p, "_home", lambda: tmp_path)
        monkeypatch.setattr(_p, "_mcp_config_path", lambda plat: tmp_path/"opencode.json" if plat=="opencode" else None)
        reg, cmd, args, env, path = _inspect_registration("opencode")
        assert reg is True and isinstance(cmd, str) and not cmd.startswith("[")
    def test_write_then_inspect_args(self, tmp_path, monkeypatch):
        from zotpilot._platforms import _write_mcp_config, _inspect_registration
        cp = tmp_path/"opencode.json"
        with patch("zotpilot._platforms._runtime_invocation", return_value=("/usr/bin/zotpilot",("mcp","serve"))):
            _write_mcp_config(cp, {})
        import zotpilot._platforms as _p
        monkeypatch.setattr(_p, "_home", lambda: tmp_path)
        monkeypatch.setattr(_p, "_mcp_config_path", lambda plat: tmp_path/"opencode.json" if plat=="opencode" else None)
        _, cmd, args, _, _ = _inspect_registration("opencode")
        assert args == ("mcp", "serve")
    def test_write_then_inspect_no_drift(self, tmp_path, monkeypatch):
        from zotpilot._platforms import _write_mcp_config, _inspect_registration, _commands_equivalent, _runtime_invocation
        cp = tmp_path/"opencode.json"
        with patch("zotpilot._platforms._runtime_invocation", return_value=("/usr/bin/zotpilot",("mcp","serve"))):
            _write_mcp_config(cp, {})
        import zotpilot._platforms as _p
        monkeypatch.setattr(_p, "_home", lambda: tmp_path)
        monkeypatch.setattr(_p, "_mcp_config_path", lambda plat: tmp_path/"opencode.json" if plat=="opencode" else None)
        # Also patch _zotpilot_command so inspection matches desired
        monkeypatch.setattr(_p, "_zotpilot_command", lambda allow_fallback=True: "/usr/bin/zotpilot")
        _, cmd, args, _, _ = _inspect_registration("opencode")
        dc, da = _runtime_invocation()
        assert _commands_equivalent(cmd, dc) and tuple(args) == da

class TestConfigFileBackups:
    def test_backup_creates_bak(self, tmp_path):
        from zotpilot._platforms import _backup_config_file
        cp = tmp_path/"config.json"; cp.write_text('{"original": true}')
        bak = _backup_config_file(cp)
        assert bak is not None and bak.exists()
        # _backup_config_file uses .with_suffix(".bak") which turns config.json → config.bak on some systems
        # or config.json.bak on others; just check it contains the right content
        assert json.loads(bak.read_text()) == {"original": True}
    def test_backup_none_missing(self, tmp_path):
        from zotpilot._platforms import _backup_config_file
        assert _backup_config_file(tmp_path/"nonexistent.json") is None

class TestEmbeddedSecretsDetection:
    def test_claude_no_secrets(self, tmp_path, monkeypatch):
        from zotpilot._platforms import _inspect_claude_registration, CREDENTIAL_ENV_KEYS
        cp = tmp_path/".claude.json"; cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps({"mcpServers":{"zotpilot":{"type":"stdio","command":"/usr/bin/zotpilot","args":["mcp","serve"],"env":{}}}}))
        import zotpilot._platforms as _p
        monkeypatch.setattr(_p, "_home", lambda: tmp_path)
        _, _, _, env, _ = _inspect_claude_registration()
        assert not any(k in env for k in CREDENTIAL_ENV_KEYS)
    def test_claude_has_secrets(self, tmp_path, monkeypatch):
        from zotpilot._platforms import _inspect_claude_registration, CREDENTIAL_ENV_KEYS
        cp = tmp_path/".claude.json"; cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps({"mcpServers":{"zotpilot":{"type":"stdio","command":"/usr/bin/zotpilot","args":["mcp","serve"],"env":{"GEMINI_API_KEY":"sk"}}}}))
        import zotpilot._platforms as _p
        monkeypatch.setattr(_p, "_home", lambda: tmp_path)
        _, _, _, env, _ = _inspect_claude_registration()
        assert any(k in env for k in CREDENTIAL_ENV_KEYS)
    def test_opencode_has_secrets(self, tmp_path, monkeypatch):
        from zotpilot._platforms import _inspect_registration, CREDENTIAL_ENV_KEYS
        cp = tmp_path/"opencode.json"
        cp.write_text(json.dumps({"mcp":{"zotpilot":{"type":"local","command":["/usr/bin/zotpilot","mcp","serve"],"environment":{"GEMINI_API_KEY":"sk"}}}}))
        import zotpilot._platforms as _p
        monkeypatch.setattr(_p, "_home", lambda: tmp_path)
        monkeypatch.setattr(_p, "_mcp_config_path", lambda plat: tmp_path/"opencode.json" if plat=="opencode" else None)
        _, _, _, env, _ = _inspect_registration("opencode")
        assert any(k in env for k in CREDENTIAL_ENV_KEYS)

class TestCommandEquivalence:
    def test_exact(self):
        from zotpilot._platforms import _commands_equivalent
        assert _commands_equivalent("/usr/bin/zotpilot", "/usr/bin/zotpilot") is True
    def test_none(self):
        from zotpilot._platforms import _commands_equivalent
        assert _commands_equivalent(None, "/usr/bin/zotpilot") is False
    def test_different(self):
        from zotpilot._platforms import _commands_equivalent
        assert _commands_equivalent("/usr/bin/zotpilot", "/usr/bin/other") is False

class TestRegistrationInspectionAllPlatforms:
    def test_claude_not_registered(self, tmp_path, monkeypatch):
        from zotpilot._platforms import _inspect_claude_registration
        import zotpilot._platforms as _p; monkeypatch.setattr(_p, "_home", lambda: tmp_path)
        r, c, a, e, p = _inspect_claude_registration()
        assert r is False and c is None
    def test_codex_not_registered(self, tmp_path, monkeypatch):
        from zotpilot._platforms import _inspect_codex_registration
        import zotpilot._platforms as _p; monkeypatch.setattr(_p, "_home", lambda: tmp_path)
        r, c, a, e, p = _inspect_codex_registration()
        assert r is False
    def test_claude_registered(self, tmp_path, monkeypatch):
        from zotpilot._platforms import _inspect_claude_registration
        cp = tmp_path/".claude.json"
        cp.write_text(json.dumps({"mcpServers":{"zotpilot":{"type":"stdio","command":"/usr/bin/zotpilot","args":["mcp","serve"]}}}))
        import zotpilot._platforms as _p; monkeypatch.setattr(_p, "_home", lambda: tmp_path)
        r, c, a, e, p = _inspect_claude_registration()
        assert r is True and c == "/usr/bin/zotpilot" and a == ("mcp","serve")
    def test_inspect_malformed_json(self, tmp_path, monkeypatch):
        from zotpilot._platforms import _inspect_claude_registration
        cp = tmp_path/".claude.json"; cp.write_text("{bad")
        import zotpilot._platforms as _p; monkeypatch.setattr(_p, "_home", lambda: tmp_path)
        r, c, a, e, p = _inspect_claude_registration()
        assert r is False

"""Cross-platform install/register/update correctness tests."""
from __future__ import annotations
import json, sys
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

def _mock_which(binaries: dict[str, str | None]):
    def fake_which(name):
        return binaries.get(name)
    return fake_which

class TestPlatformDetectionMacOS:
    def test_detect_all_three_clients_mac(self, tmp_path, monkeypatch):
        from zotpilot._platforms import detect_platforms
        bin_dir = tmp_path / "bin"; bin_dir.mkdir()
        for b in ("claude", "codex", "opencode"): (bin_dir / b).touch()
        monkeypatch.setenv("PATH", str(bin_dir))
        with patch("shutil.which", side_effect=_mock_which({
            "claude": str(bin_dir/"claude"), "codex": str(bin_dir/"codex"), "opencode": str(bin_dir/"opencode")})):
            assert set(detect_platforms()) == {"claude-code", "codex", "opencode"}

    def test_detect_partial_clients_mac(self):
        from zotpilot._platforms import detect_platforms
        with patch("shutil.which", side_effect=_mock_which({"claude":"/usr/bin/claude","codex":"/usr/bin/codex","opencode":None})), \
             patch("zotpilot._platforms._detect_app_install", return_value=False):
            r = detect_platforms()
        assert set(r) == {"claude-code", "codex"}

    def test_detect_no_clients_mac(self):
        from zotpilot._platforms import detect_platforms
        with patch("shutil.which", return_value=None), patch("zotpilot._platforms._detect_app_install", return_value=False):
            assert detect_platforms() == []

    def test_detect_opencode_via_package_json_mac(self, tmp_path, monkeypatch):
        from zotpilot._platforms import detect_platforms
        d = tmp_path / ".config" / "opencode"; d.mkdir(parents=True)
        (d / "package.json").write_text(json.dumps({"dependencies": {"@opencode-ai/opencode": "^1.0"}}))
        monkeypatch.setenv("HOME", str(tmp_path))
        with patch("shutil.which", side_effect=_mock_which({"claude":None,"codex":None,"opencode":None})), \
             patch("zotpilot._platforms._detect_app_install", return_value=True):
            assert "opencode" in detect_platforms()

    def test_opencode_not_detected_without_binary_or_package(self):
        from zotpilot._platforms import detect_platforms
        with patch("shutil.which", side_effect=_mock_which({"claude":None,"codex":None,"opencode":None})), \
             patch("zotpilot._platforms._detect_app_install", return_value=False):
            assert "opencode" not in detect_platforms()

class TestPlatformDetectionWindows:
    def test_windows_detection_all_three(self):
        from zotpilot._platforms import detect_platforms
        with patch("shutil.which", side_effect=_mock_which({"claude":"C:\\claude.cmd","codex":"C:\\codex.cmd","opencode":"C:\\opencode.cmd"})):
            assert set(detect_platforms()) == {"claude-code","codex","opencode"}
    def test_windows_exe_binary_resolution(self):
        from zotpilot._platforms import detect_platforms
        with patch("shutil.which", side_effect=_mock_which({"claude":"C:\\claude.cmd","codex":"C:\\codex.cmd","opencode":None})), \
             patch("zotpilot._platforms._detect_app_install", return_value=False):
            assert set(detect_platforms()) == {"claude-code","codex"}

class TestConfigDataDirs:
    def test_mac_config_dir(self, monkeypatch):
        monkeypatch.setenv("HOME", "/Users/testuser")
        from zotpilot.config import _default_config_dir, _default_data_dir
        with patch.object(sys, "platform", "darwin"):
            assert str(_default_config_dir()) == "/Users/testuser/.config/zotpilot"
            assert str(_default_data_dir()) == "/Users/testuser/.local/share/zotpilot"
    def test_windows_config_dir(self, monkeypatch):
        monkeypatch.setenv("APPDATA", "C:\\Users\\test\\AppData\\Roaming")
        monkeypatch.setenv("LOCALAPPDATA", "C:\\Users\\test\\AppData\\Local")
        from zotpilot.config import _default_config_dir, _default_data_dir
        with patch.object(sys, "platform", "win32"):
            # Path str on macOS uses / separator; normalize for comparison
            assert _default_config_dir().as_posix().endswith("zotpilot")
            # On macOS, Path str() preserves \ from env var; check both forms
            cfg = str(_default_config_dir()); assert "AppData" in cfg and "Roaming" in cfg and "zotpilot" in cfg
            data = str(_default_data_dir()); assert "AppData" in data and "Local" in data and "zotpilot" in data
    def test_linux_config_dir(self, monkeypatch):
        monkeypatch.setenv("HOME", "/home/testuser")
        from zotpilot.config import _default_config_dir, _default_data_dir
        with patch.object(sys, "platform", "linux"):
            assert str(_default_config_dir()) == "/home/testuser/.config/zotpilot"
    def test_old_config_path_mac(self, monkeypatch):
        monkeypatch.setenv("HOME", "/Users/testuser")
        from zotpilot.config import _old_config_path
        with patch.object(sys, "platform", "darwin"):
            assert str(_old_config_path()) == "/Users/testuser/.config/deep-zotero/config.json"
    def test_old_config_path_windows(self, monkeypatch):
        monkeypatch.setenv("APPDATA", "C:\\Users\\test\\AppData\\Roaming")
        from zotpilot.config import _old_config_path
        with patch.object(sys, "platform", "win32"):
            p = _old_config_path().as_posix()
            assert "AppData" in p and "Roaming" in p and "deep-zotero" in p and "config.json" in p

class TestZoteroDetector:
    def test_mac_profile_dir(self, monkeypatch, tmp_path):
        pd = tmp_path/"Library"/"Application Support"/"Zotero"; pd.mkdir(parents=True)
        (pd/"profiles.ini").write_text("[Profile0]\nName=default\nIsRelative=1\nPath=abc.default\nDefault=1\n")
        (pd/"abc.default"/"prefs.js").parent.mkdir(); (pd/"abc.default"/"prefs.js").write_text(
            'user_pref("extensions.zotero.useDataDir", true);\nuser_pref("extensions.zotero.dataDir", "'+str(tmp_path/"Zotero")+'");\n')
        (tmp_path/"Zotero").mkdir(); (tmp_path/"Zotero"/"zotero.sqlite").touch()
        monkeypatch.setenv("HOME", str(tmp_path))
        with patch("platform.system", return_value="Darwin"):
            from zotpilot.zotero_detector import detect_zotero_data_dir
            assert detect_zotero_data_dir() == tmp_path/"Zotero"
    def test_windows_profile_dir(self, monkeypatch, tmp_path):
        pd = tmp_path/"AppData"/"Roaming"/"Zotero"/"Zotero"; pd.mkdir(parents=True)
        (pd/"profiles.ini").write_text("[Profile0]\nName=default\nIsRelative=1\nPath=abc.default\nDefault=1\n")
        (pd/"abc.default").mkdir(); (pd/"abc.default"/"prefs.js").write_text(
            'user_pref("extensions.zotero.useDataDir", true);\nuser_pref("extensions.zotero.dataDir", "'+str(tmp_path/"Zotero")+'");\n')
        (tmp_path/"Zotero").mkdir(); (tmp_path/"Zotero"/"zotero.sqlite").touch()
        monkeypatch.setenv("HOME", str(tmp_path))
        with patch("platform.system", return_value="Windows"):
            from zotpilot.zotero_detector import detect_zotero_data_dir
            assert detect_zotero_data_dir() == tmp_path/"Zotero"
    def test_fallback_to_home_zotero(self, monkeypatch, tmp_path):
        (tmp_path/"Zotero").mkdir(); (tmp_path/"Zotero"/"zotero.sqlite").touch()
        monkeypatch.setenv("HOME", str(tmp_path))
        with patch("platform.system", return_value="Darwin"), patch("zotpilot.zotero_detector._detect_from_profiles", return_value=None):
            from zotpilot.zotero_detector import detect_zotero_data_dir
            assert detect_zotero_data_dir() == tmp_path/"Zotero"

class TestBinaryResolution:
    def test_zotpilot_command_path_first(self, tmp_path, monkeypatch):
        from zotpilot._platforms import _zotpilot_command
        monkeypatch.setenv("HOME", str(tmp_path))
        bd = tmp_path/"bin"; bd.mkdir(); (bd/"zotpilot").touch()
        with patch("shutil.which", return_value=str(bd/"zotpilot")):
            assert _zotpilot_command() == str(bd/"zotpilot")
    def test_zotpilot_command_windows_pip_user_path(self, tmp_path, monkeypatch):
        from zotpilot._platforms import _zotpilot_command
        ad = tmp_path/"AppData"/"Roaming"; sc = ad/"Python"/"Python313"/"Scripts"; sc.mkdir(parents=True)
        (sc/"zotpilot.exe").touch(); monkeypatch.setenv("APPDATA", str(ad))
        with patch("shutil.which", return_value=None), patch("platform.system", return_value="Windows"), \
             patch.object(sys, "version_info", type("",(),{"major":3,"minor":13})):
            assert _zotpilot_command() == str(sc/"zotpilot.exe")
    def test_zotpilot_command_unix_user_path(self, tmp_path, monkeypatch):
        from zotpilot._platforms import _zotpilot_command
        ub = tmp_path/".local"/"bin"; ub.mkdir(parents=True); (ub/"zotpilot").touch()
        monkeypatch.setenv("HOME", str(tmp_path))
        with patch("shutil.which", return_value=None), patch("platform.system", return_value="Darwin"):
            assert _zotpilot_command() == str(ub/"zotpilot")
    def test_zotpilot_command_fallback(self, tmp_path, monkeypatch):
        from zotpilot._platforms import _zotpilot_command
        monkeypatch.setenv("HOME", str(tmp_path))
        with patch("shutil.which", return_value=None), patch("platform.system", return_value="Darwin"), \
             patch.object(sys, "version_info", type("",(),{"major":3,"minor":11})):
            assert _zotpilot_command() == "zotpilot"

class TestMCPConfigPaths:
    def test_claude_config_path(self, monkeypatch):
        monkeypatch.setenv("HOME", "/Users/testuser")
        from zotpilot._platforms import _claude_config_path
        assert str(_claude_config_path()) == "/Users/testuser/.claude.json"
    def test_codex_config_path(self, monkeypatch):
        monkeypatch.setenv("HOME", "/Users/testuser")
        from zotpilot._platforms import _codex_config_path
        assert str(_codex_config_path()) == "/Users/testuser/.codex/config.toml"
    def test_opencode_config_path(self, monkeypatch):
        monkeypatch.setenv("HOME", "/Users/testuser")
        from zotpilot._platforms import _mcp_config_path
        assert str(_mcp_config_path("opencode")) == "/Users/testuser/.config/opencode/opencode.json"
    def test_mcp_config_path_unknown_platform(self):
        from zotpilot._platforms import _mcp_config_path
        assert _mcp_config_path("unknown") is None

class TestMultiClientRegistration:
    def test_plan_changes_all_clean(self):
        from zotpilot._platforms import DesiredRuntime, RuntimeState, plan_runtime_changes, PlatformRuntimeState
        current = RuntimeState(package_version="0.5.0", supported_targets=("claude-code","codex","opencode"), platforms={
            "claude-code": PlatformRuntimeState(platform="claude-code",label="Claude Code",supported=True,detected=True,registered=True,command="/usr/bin/zotpilot",args=("mcp","serve"),env={},has_embedded_secrets=False,skill_dirs=(),skill_hash_ok=True,registration_hash_ok=True),
            "codex": PlatformRuntimeState(platform="codex",label="Codex CLI",supported=True,detected=True,registered=True,command="/usr/bin/zotpilot",args=("mcp","serve"),env={},has_embedded_secrets=False,skill_dirs=(),skill_hash_ok=True,registration_hash_ok=True),
            "opencode": PlatformRuntimeState(platform="opencode",label="OpenCode",supported=True,detected=True,registered=True,command="/usr/bin/zotpilot",args=("mcp","serve"),env={},has_embedded_secrets=False,skill_dirs=(),skill_hash_ok=True,registration_hash_ok=True),
        })
        desired = DesiredRuntime(command="/usr/bin/zotpilot",args=("mcp","serve"),env={},targets=("claude-code","codex","opencode"))
        c = plan_runtime_changes(desired, current)
        assert c.deploy_skill_platforms == () and c.register_platforms == () and c.drift_state == "clean"
    def test_plan_changes_partial_registration(self):
        from zotpilot._platforms import DesiredRuntime, RuntimeState, plan_runtime_changes, PlatformRuntimeState
        current = RuntimeState(package_version="0.5.0", supported_targets=("claude-code","codex"), platforms={
            "claude-code": PlatformRuntimeState(platform="claude-code",label="CC",supported=True,detected=True,registered=True,command="/usr/bin/zotpilot",args=("mcp","serve"),env={},has_embedded_secrets=False,skill_dirs=(),skill_hash_ok=True,registration_hash_ok=True),
            "codex": PlatformRuntimeState(platform="codex",label="Codex",supported=True,detected=True,registered=False,command=None,args=(),env={},has_embedded_secrets=False,skill_dirs=(),skill_hash_ok=False,registration_hash_ok=False),
        })
        desired = DesiredRuntime(command="/usr/bin/zotpilot",args=("mcp","serve"),env={},targets=("claude-code","codex"))
        c = plan_runtime_changes(desired, current)
        assert "codex" in c.register_platforms and "claude-code" not in c.register_platforms
    def test_plan_changes_embedded_secrets(self):
        from zotpilot._platforms import DesiredRuntime, RuntimeState, plan_runtime_changes, PlatformRuntimeState
        current = RuntimeState(package_version="0.5.0", supported_targets=("claude-code","codex"), platforms={
            "claude-code": PlatformRuntimeState(platform="claude-code",label="CC",supported=True,detected=True,registered=True,command="/usr/bin/zotpilot",args=("mcp","serve"),env={"GEMINI_API_KEY":"sk"},has_embedded_secrets=True,skill_dirs=(),skill_hash_ok=True,registration_hash_ok=True),
            "codex": PlatformRuntimeState(platform="codex",label="Codex",supported=True,detected=True,registered=True,command="/usr/bin/zotpilot",args=("mcp","serve"),env={},has_embedded_secrets=False,skill_dirs=(),skill_hash_ok=True,registration_hash_ok=True),
        })
        desired = DesiredRuntime(command="/usr/bin/zotpilot",args=("mcp","serve"),env={},targets=("claude-code","codex"))
        c = plan_runtime_changes(desired, current)
        assert "claude-code" in c.register_platforms and "codex" not in c.register_platforms

class TestDoctorCrossPlatform:
    def test_config_permissions_mac_clean(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        cp = tmp_path/".config"/"zotpilot"/"config.json"; cp.parent.mkdir(parents=True)
        cp.write_text(json.dumps({"zotero_data_dir": str(tmp_path/"zotero")}))
        cp.chmod(0o600)
        from zotpilot.doctor import _check_config_permissions
        assert _check_config_permissions(cp).status == "pass"
    def test_config_permissions_windows_warning(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        cp = tmp_path/".config"/"zotpilot"/"config.json"; cp.parent.mkdir(parents=True)
        cp.write_text(json.dumps({"zotero_data_dir": str(tmp_path/"zotero"), "gemini_api_key": "sk-1234"}))
        from zotpilot.doctor import _check_config_permissions
        with patch.object(sys, "platform", "win32"):
            assert _check_config_permissions(cp).status == "warn"

class TestUpgradeFlowCrossPlatform:
    def test_upgrade_uv_windows_lock_error(self, capsys):
        import subprocess
        from zotpilot.cli import cmd_update
        args = type("Args",(),{"cli_only":True,"skill_only":False,"check":False,"dry_run":False,"migrate_secrets":False,"re_register":False})()
        with patch("zotpilot.cli._get_current_version",return_value="0.5.0"),patch("zotpilot.cli._get_latest_pypi_version",return_value="0.5.1"), \
             patch("zotpilot.cli._detect_cli_installer",return_value=("uv",["uv"])), \
             patch("zotpilot.cli.subprocess.run",side_effect=subprocess.CalledProcessError(1,"uv",stderr="PermissionError: [WinError 32]")), \
             patch("zotpilot.cli.sys") as ms:
            ms.platform="win32"; ms.executable=sys.executable; ms.argv=sys.argv
            assert cmd_update(args) == 1
        assert "locked" in capsys.readouterr().out
    def test_upgrade_pip_unix(self, capsys):
        from zotpilot.cli import cmd_update
        args = type("Args",(),{"cli_only":True,"skill_only":False,"check":False,"dry_run":False,"migrate_secrets":False,"re_register":False})()
        mr = MagicMock(); mr.returncode=0; mr.stdout="ok"
        with patch("zotpilot.cli._get_current_version",return_value="0.5.0"),patch("zotpilot.cli._get_latest_pypi_version",return_value="0.5.1"), \
             patch("zotpilot.cli._detect_cli_installer",return_value=("pip",None)), \
             patch("zotpilot.cli.subprocess.run",return_value=mr) as mr2:
            assert cmd_update(args) == 0
        assert any("pip" in c.args[0] and "--upgrade" in c.args[0] for c in mr2.call_args_list)

class TestSkillDeploymentCrossPlatform:
    def _pp(self, fp):
        import zotpilot._platforms as m; o=m.PLATFORMS; m.PLATFORMS=fp; return o
    def _rp(self, o):
        import zotpilot._platforms as m; m.PLATFORMS=o
    def test_deploy_mac_claude(self, tmp_path, monkeypatch):
        sr = tmp_path/".claude"/"skills"; sr.mkdir(parents=True)
        src = tmp_path/"source"; src.mkdir()
        (src/"SKILL.md").write_text("---\nname: zotpilot\n---\n")
        monkeypatch.setenv("HOME", str(tmp_path))
        o = self._pp({"claude-code":{"tier":1,"binary":"claude","label":"CC","skills_dir":str(sr)}})
        try:
            with patch("shutil.which",return_value="/usr/bin/claude"),patch("zotpilot._platforms._skill_source_files",return_value=list(src.glob("*.md"))), \
                 patch("zotpilot._platforms._skill_source_dir",return_value=src):
                from zotpilot._platforms import deploy_skills
                assert deploy_skills(platforms=["claude-code"]) == {"claude-code":True}
        finally: self._rp(o)
        assert (sr/"zotpilot"/"SKILL.md").exists()

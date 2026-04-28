"""Bootstrap install script (scripts/run.py) correctness tests."""
from __future__ import annotations
import importlib.util, json, subprocess, sys
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

RUN_PY = Path(__file__).resolve().parents[1] / "scripts" / "run.py"
_spec = importlib.util.spec_from_file_location("run_mod", RUN_PY)
_rm = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_rm)

class TestUvDetection:
    def test_uv_in_path(self, tmp_path):
        bd = tmp_path/"bin"; bd.mkdir(); (bd/"uv").touch()
        with patch("shutil.which", return_value=str(bd/"uv")):
            assert _rm._ensure_uv() == str(bd/"uv")
    def test_uv_via_python_m_uv(self):
        with patch("shutil.which", return_value=None), patch("subprocess.run", return_value=MagicMock(returncode=0)):
            assert _rm._ensure_uv() == f"{sys.executable} -m uv"
    def test_uv_not_found_exits(self):
        # Verify _ensure_uv exits with code 1 when uv is unavailable
        # subprocess.run with check=True raises CalledProcessError on failure
        # Must raise, not just return returncode=1
        err = subprocess.CalledProcessError(1, [sys.executable, "-m", "uv", "--version"])
        with patch('shutil.which', return_value=None), patch.object(_rm.subprocess, 'run', side_effect=err):
            try:
                _rm._ensure_uv()
                assert False, "Should have exited"
            except SystemExit as e:
                assert e.code == 1
        src = (Path(__file__).parents[1] / "scripts" / "run.py").read_text()
        assert "uv is not installed" in src
        assert "curl" in src
        assert "powershell" in src

    def test_uv_args_python_m(self):
        assert _rm._uv_args(f"{sys.executable} -m uv") == [sys.executable, "-m", "uv"]
    def test_uv_args_direct(self, tmp_path):
        p = str(tmp_path/"bin"/"uv")
        assert _rm._uv_args(p) == [p]

class TestZotpilotBinaryResolution:
    def test_on_path(self):
        with patch("shutil.which", return_value="/usr/bin/zotpilot"):
            assert _rm._find_zotpilot_after_pip() == ["/usr/bin/zotpilot"]
    def test_pip_mac(self, tmp_path, monkeypatch):
        ub = tmp_path/".local"/"bin"; ub.mkdir(parents=True); (ub/"zotpilot").touch()
        monkeypatch.setenv("HOME", str(tmp_path))
        with patch("shutil.which", return_value=None):
            assert _rm._find_zotpilot_after_pip() == [str(ub/"zotpilot")]
    def test_pip_win(self, tmp_path, monkeypatch):
        ad = tmp_path/"AppData"/"Roaming"; sc = ad/"Python"/"Python313"/"Scripts"; sc.mkdir(parents=True)
        (sc/"zotpilot.exe").touch(); monkeypatch.setenv("APPDATA", str(ad))
        monkeypatch.setenv("HOME", str(tmp_path))  # Ensure ~/.local/bin doesn't exist
        import platform as _p
        with patch("shutil.which", return_value=None), patch.object(_p, "system", return_value="Windows"), \
             patch.object(sys, "version_info", type("",(),{"major":3,"minor":13})):
            assert _rm._find_zotpilot_after_pip() == [str(sc/"zotpilot.exe")]
    def test_pip_win_no_appdata(self, tmp_path, monkeypatch):
        monkeypatch.delenv("APPDATA", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        import platform as _p
        with patch("shutil.which", return_value=None), patch.object(_p, "system", return_value="Windows"):
            assert _rm._find_zotpilot_after_pip() is None
    def test_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        import platform as _p
        with patch("shutil.which", return_value=None), patch.object(_p, "system", return_value="Darwin"):
            assert _rm._find_zotpilot_after_pip() is None

class TestUvToolInstallCheck:
    def test_installed(self, tmp_path):
        bd = tmp_path/"uv-bin"; bd.mkdir(); (bd/"zotpilot").touch()
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=str(bd)+"\n")):
            assert _rm._is_uv_tool_installed("uv") is True
    def test_not_installed(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=1)):
            assert _rm._is_uv_tool_installed("uv") is False

class TestVersionComparison:
    def test_needs_upgrade(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_rm, "SKILL_DIR", tmp_path)
        (tmp_path/"pyproject.toml").write_text('version = "0.5.0"\n')
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="zotpilot v0.4.0\n")):
            assert _rm._needs_upgrade("uv") is True
    def test_same_version(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_rm, "SKILL_DIR", tmp_path)
        (tmp_path/"pyproject.toml").write_text('version = "0.5.0"\n')
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="zotpilot v0.5.0\n")):
            assert _rm._needs_upgrade("uv") is False
    def test_no_pyproject(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_rm, "SKILL_DIR", tmp_path)
        with patch("subprocess.run"):
            assert _rm._needs_upgrade("uv") is False
    def test_get_source_version(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_rm, "SKILL_DIR", tmp_path)
        (tmp_path/"pyproject.toml").write_text('version = "0.5.0"\n')
        assert _rm._get_source_version() == "0.5.0"

class TestEnsureZotpilot:
    def test_already_on_path(self):
        with patch("shutil.which", return_value="/usr/bin/zotpilot"):
            assert _rm._ensure_zotpilot("uv") is None
    def test_uv_tool_install_succeeds(self, tmp_path):
        with patch("shutil.which", return_value=None), patch.object(_rm, "SKILL_DIR", tmp_path), \
             patch("platform.system", return_value="Darwin"), patch.object(_rm, "_find_zotpilot_after_pip", return_value=None), \
             patch.object(_rm, "_is_uv_tool_installed", return_value=False), \
             patch("subprocess.run", return_value=MagicMock(returncode=0)) as mr:
            assert _rm._ensure_zotpilot("uv") is None
        assert any("install" in c.args[0] for c in mr.call_args_list)

class TestHandleRegister:
    def test_register_uv(self):
        def fr(cmd, **kw):
            r = MagicMock(); r.returncode=0; return r
        with patch.object(_rm, "_ensure_uv", return_value="uv"), patch.object(_rm, "_ensure_zotpilot", return_value=None), \
             patch.object(_rm, "_uv_args", return_value=["uv"]), patch("subprocess.run", side_effect=fr) as mr:
            assert _rm._handle_register(["--platform","codex"]) == 0
        assert len(mr.call_args_list) >= 2
    def test_register_pip(self):
        with patch.object(_rm, "_ensure_uv", return_value="uv"), patch.object(_rm, "_ensure_zotpilot", return_value=["/p/zotpilot"]), \
             patch("subprocess.run", return_value=MagicMock(returncode=0)) as mr:
            assert _rm._handle_register(["--platform","claude-code"]) == 0
        cmd = mr.call_args_list[0].args[0]
        assert cmd[0] == "/p/zotpilot" and "register" in cmd

class TestMainDelegation:
    def test_register_intercepted(self):
        captured = {'called': False, 'args': None}
        def fake_hr(args):
            captured['called'] = True
            captured['args'] = args
            return 0
        def fake_exit(code):
            pass
        old_argv = _rm.sys.argv
        old_exit = _rm.sys.exit
        old_hr = _rm._handle_register
        try:
            _rm.sys.argv = ['run.py', 'register', '--platform', 'codex']
            _rm.sys.exit = fake_exit
            _rm._handle_register = fake_hr
            _rm.main()
        finally:
            _rm.sys.argv = old_argv
            _rm.sys.exit = old_exit
            _rm._handle_register = old_hr
        assert captured['called'] is True
        assert captured['args'] == ['--platform', 'codex']

    def test_other_delegated(self):
        with patch.object(_rm, "_ensure_uv", return_value="uv"), patch.object(_rm, "_ensure_zotpilot", return_value=None), \
             patch.object(_rm, "_uv_args", return_value=["uv"]), \
             patch("subprocess.run", return_value=MagicMock(returncode=0)) as mr, \
             patch("sys.argv", ["run.py","status","--json"]), patch("sys.exit"):
            _rm.main()
        cmd = mr.call_args.args[0]
        assert "tool" in cmd and "run" in cmd and "zotpilot" in cmd and "status" in cmd

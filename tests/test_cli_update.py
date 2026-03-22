"""Tests for `zotpilot update` CLI subcommand."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from zotpilot.cli import (
    SkillDir,
    _detect_cli_installer,
    _get_current_version,
    _get_latest_pypi_version,
    _get_skill_dirs,
    _is_zotpilot_skill_repo,
    _uv_bin_dir,
    cmd_update,
)


def _make_args(**kwargs):
    """Create argparse.Namespace with default update args."""
    defaults = dict(cli_only=False, skill_only=False, check=False, dry_run=False)
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _make_skill_dir(tmp_path: Path, name: str = "zotpilot") -> Path:
    """Create a minimal valid skill directory structure."""
    skill_dir = tmp_path / name
    skill_dir.mkdir(exist_ok=True)
    (skill_dir / ".git").mkdir(exist_ok=True)
    (skill_dir / "scripts").mkdir(exist_ok=True)
    (skill_dir / "scripts" / "run.py").touch()
    (skill_dir / "SKILL.md").write_text("---\nname: zotpilot\n---\n")
    return skill_dir


# ---------------------------------------------------------------------------
# TestUvBinDir
# ---------------------------------------------------------------------------


class TestUvBinDir:
    def test_uv_bin_dir_failure_returns_none(self):
        """subprocess raises → None."""
        with patch("zotpilot.cli.subprocess.run", side_effect=Exception("no uv")):
            assert _uv_bin_dir(["uv"]) is None

    def test_uv_bin_dir_nonzero_returns_none(self):
        """subprocess returns non-zero exit code → None."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        with patch("zotpilot.cli.subprocess.run", return_value=mock_result):
            assert _uv_bin_dir(["uv"]) is None

    def test_uv_bin_dir_empty_stdout_returns_none(self):
        """subprocess returns 0 but empty stdout → None."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "   "
        with patch("zotpilot.cli.subprocess.run", return_value=mock_result):
            assert _uv_bin_dir(["uv"]) is None


# ---------------------------------------------------------------------------
# TestDetectCliInstaller
# ---------------------------------------------------------------------------


class TestDetectCliInstaller:
    def test_detect_editable(self):
        """direct_url.json with editable=true → ('editable', None)."""
        mock_dist = MagicMock()
        mock_dist.read_text.return_value = json.dumps({"dir_info": {"editable": True}})
        with patch("importlib.metadata.distribution", return_value=mock_dist):
            installer, uv_cmd = _detect_cli_installer()
        assert installer == "editable"
        assert uv_cmd is None

    def test_detect_pip_with_evidence(self):
        """Metadata found, no editable flag, uv not detected → ('pip', None)."""
        mock_dist = MagicMock()
        mock_dist.read_text.return_value = None  # no direct_url.json
        with patch("importlib.metadata.distribution", return_value=mock_dist), \
             patch("shutil.which", return_value=None), \
             patch("zotpilot.cli._uv_bin_dir", return_value=None):
            installer, uv_cmd = _detect_cli_installer()
        assert installer == "pip"
        assert uv_cmd is None

    def test_detect_unknown_no_evidence(self):
        """PackageNotFoundError → ('unknown', None)."""
        with patch("importlib.metadata.distribution",
                   side_effect=importlib.metadata.PackageNotFoundError("zotpilot")):
            installer, uv_cmd = _detect_cli_installer()
        assert installer == "unknown"
        assert uv_cmd is None

    def test_detect_uv_by_exe_path(self, tmp_path):
        """shutil.which('uv') found, argv0 under uv bin dir → ('uv', ['uv'])."""
        # Use tmp_path so Path.resolve() returns a consistent real path
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_exe = bin_dir / "zotpilot"
        fake_exe.touch()

        mock_dist = MagicMock()
        mock_dist.read_text.return_value = None
        with patch("importlib.metadata.distribution", return_value=mock_dist), \
             patch("shutil.which", return_value=str(bin_dir / "uv")), \
             patch("zotpilot.cli._uv_bin_dir", return_value=bin_dir.resolve()), \
             patch.object(sys, "argv", [str(fake_exe)]):
            installer, uv_cmd = _detect_cli_installer()
        assert installer == "uv"
        assert uv_cmd == ["uv"]

    def test_detect_uv_via_python_m_uv(self, tmp_path):
        """shutil.which returns None, [sys.executable, '-m', 'uv'] works → ('uv', [...])."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_exe = bin_dir / "zotpilot"
        fake_exe.touch()
        resolved_bin = bin_dir.resolve()

        mock_dist = MagicMock()
        mock_dist.read_text.return_value = None

        def fake_uv_bin_dir(cmd):
            if cmd == [sys.executable, "-m", "uv"]:
                return resolved_bin
            return None

        with patch("importlib.metadata.distribution", return_value=mock_dist), \
             patch("shutil.which", return_value=None), \
             patch("zotpilot.cli._uv_bin_dir", side_effect=fake_uv_bin_dir), \
             patch.object(sys, "argv", [str(fake_exe)]):
            installer, uv_cmd = _detect_cli_installer()
        assert installer == "uv"
        assert uv_cmd == [sys.executable, "-m", "uv"]

    def test_detect_unknown_on_malformed_direct_url_json(self):
        """Malformed direct_url.json → doesn't crash, falls back conservatively."""
        mock_dist = MagicMock()
        mock_dist.read_text.return_value = "not valid json {"
        with patch("importlib.metadata.distribution", return_value=mock_dist), \
             patch("shutil.which", return_value=None), \
             patch("zotpilot.cli._uv_bin_dir", return_value=None):
            installer, uv_cmd = _detect_cli_installer()
        assert installer == "unknown"
        assert uv_cmd is None

    def test_detect_uv_bin_dir_fails_falls_back_to_python_m_uv(self, tmp_path):
        """shutil.which finds uv but _uv_bin_dir(['uv']) returns None → try python -m uv."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_exe = bin_dir / "zotpilot"
        fake_exe.touch()
        resolved_bin = bin_dir.resolve()

        mock_dist = MagicMock()
        mock_dist.read_text.return_value = None

        def fake_uv_bin_dir(cmd):
            if cmd == ["uv"]:
                return None  # uv binary found but bin_dir lookup failed
            if cmd == [sys.executable, "-m", "uv"]:
                return resolved_bin
            return None

        with patch("importlib.metadata.distribution", return_value=mock_dist), \
             patch("shutil.which", return_value="/usr/local/bin/uv"), \
             patch("zotpilot.cli._uv_bin_dir", side_effect=fake_uv_bin_dir), \
             patch.object(sys, "argv", [str(fake_exe)]):
            installer, uv_cmd = _detect_cli_installer()
        assert installer == "uv"
        assert uv_cmd == [sys.executable, "-m", "uv"]


# ---------------------------------------------------------------------------
# TestGetVersion
# ---------------------------------------------------------------------------


class TestGetVersion:
    def test_get_current_version_fallback(self):
        """importlib.metadata.version raises → falls back to __version__."""
        with patch("importlib.metadata.version", side_effect=Exception("not found")):
            version = _get_current_version()
        # Falls back to zotpilot.__version__ = "0.3.0"
        assert version == "0.3.0"


# ---------------------------------------------------------------------------
# TestGetLatestPypi
# ---------------------------------------------------------------------------


class TestGetLatestPypi:
    def test_get_latest_pypi_version_network_error(self):
        """urllib raises → returns None."""
        import urllib.error
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("timeout")):
            result = _get_latest_pypi_version()
        assert result is None


# ---------------------------------------------------------------------------
# TestIsZotpilotSkillRepo
# ---------------------------------------------------------------------------


class TestIsZotpilotSkillRepo:
    def test_skill_identity_check_no_skill_md(self, tmp_path):
        """No SKILL.md → False."""
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "run.py").touch()
        assert _is_zotpilot_skill_repo(tmp_path) is False

    def test_skill_identity_check_wrong_skill_name(self, tmp_path):
        """SKILL.md with name: other → False."""
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "run.py").touch()
        (tmp_path / "SKILL.md").write_text("---\nname: other-tool\n---\n")
        assert _is_zotpilot_skill_repo(tmp_path) is False

    def test_skill_identity_check_missing_signature_file(self, tmp_path):
        """SKILL.md with name: zotpilot but no scripts/run.py → False."""
        (tmp_path / "SKILL.md").write_text("---\nname: zotpilot\n---\n")
        assert _is_zotpilot_skill_repo(tmp_path) is False

    def test_valid_skill_repo_returns_true(self, tmp_path):
        """Valid SKILL.md + scripts/run.py → True."""
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "run.py").touch()
        (tmp_path / "SKILL.md").write_text("---\nname: zotpilot\n---\n")
        assert _is_zotpilot_skill_repo(tmp_path) is True


# ---------------------------------------------------------------------------
# TestGetSkillDirs
# ---------------------------------------------------------------------------


class TestGetSkillDirs:
    def _patch_platforms(self, fake_platforms: dict):
        """Context manager to temporarily replace PLATFORMS."""
        import zotpilot._platforms as _plat_mod
        orig = _plat_mod.PLATFORMS
        _plat_mod.PLATFORMS = fake_platforms
        return orig

    def _restore_platforms(self, orig):
        import zotpilot._platforms as _plat_mod
        _plat_mod.PLATFORMS = orig

    def test_get_skill_dirs_uses_platforms(self, tmp_path):
        """Derives dirs from PLATFORMS, not a hardcoded list."""
        fake_skills = tmp_path / "skills"
        fake_skills.mkdir()
        skill_path = fake_skills / "zotpilot"
        skill_path.mkdir()
        fake_platforms = {
            "test-platform": {"tier": 1, "skills_dir": str(fake_skills)},
        }
        orig = self._patch_platforms(fake_platforms)
        try:
            result = _get_skill_dirs()
        finally:
            self._restore_platforms(orig)
        paths = [sd.path for sd in result]
        assert skill_path in paths

    def test_duplicate_canonical_prefers_non_symlink(self, tmp_path):
        """Same realpath via symlink and real dir: real dir is canonical, symlink is duplicate."""
        a_skills = tmp_path / "a_skills"
        b_skills = tmp_path / "b_skills"
        a_skills.mkdir()
        b_skills.mkdir()

        real_skill = a_skills / "zotpilot"
        real_skill.mkdir()
        sym_skill = b_skills / "zotpilot"
        os.symlink(real_skill, sym_skill)

        fake_platforms = {
            "plat-a": {"tier": 1, "skills_dir": str(a_skills)},
            "plat-b": {"tier": 1, "skills_dir": str(b_skills)},
        }
        orig = self._patch_platforms(fake_platforms)
        try:
            result = _get_skill_dirs()
        finally:
            self._restore_platforms(orig)

        # Both entries share the same realpath → one canonical, one duplicate
        assert len(result) == 2
        non_sym = [sd for sd in result if not sd.is_symlink]
        sym = [sd for sd in result if sd.is_symlink]
        assert len(non_sym) == 1
        assert non_sym[0].is_duplicate is False
        assert len(sym) == 1
        assert sym[0].is_duplicate is True

    def test_broken_symlink_warned_not_silently_dropped(self, tmp_path):
        """Broken symlink included in results with is_broken_symlink=True."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        zotpilot_sym = skills_dir / "zotpilot"
        os.symlink(tmp_path / "nonexistent_target", zotpilot_sym)

        fake_platforms = {
            "test": {"tier": 1, "skills_dir": str(skills_dir)},
        }
        orig = self._patch_platforms(fake_platforms)
        try:
            result = _get_skill_dirs()
        finally:
            self._restore_platforms(orig)

        assert len(result) == 1
        assert result[0].is_broken_symlink is True
        assert result[0].is_symlink is True
        assert result[0].is_duplicate is False


# ---------------------------------------------------------------------------
# TestCmdUpdate
# ---------------------------------------------------------------------------


class TestCmdUpdate:
    def test_dry_run_no_mutating_subprocess(self, capsys):
        """--dry-run: subprocess.run never called with upgrade/pull commands."""
        args = _make_args(dry_run=True)
        with patch("zotpilot.cli._get_current_version", return_value="0.2.0"), \
             patch("zotpilot.cli._detect_cli_installer", return_value=("pip", None)), \
             patch("zotpilot.cli._get_skill_dirs", return_value=[]), \
             patch("zotpilot.cli.subprocess.run") as mock_run:
            cmd_update(args)
        for c in mock_run.call_args_list:
            cmd_args = c[0][0] if c[0] else []
            assert "upgrade" not in cmd_args
            assert "pull" not in cmd_args

    def test_dry_run_skips_pypi_query(self, capsys):
        """--dry-run: _get_latest_pypi_version not called."""
        args = _make_args(dry_run=True)
        with patch("zotpilot.cli._get_current_version", return_value="0.2.0"), \
             patch("zotpilot.cli._get_latest_pypi_version") as mock_pypi, \
             patch("zotpilot.cli._detect_cli_installer", return_value=("pip", None)), \
             patch("zotpilot.cli._get_skill_dirs", return_value=[]):
            cmd_update(args)
        mock_pypi.assert_not_called()

    def test_check_no_subprocess(self, capsys):
        """--check: subprocess.run not called; returns 0 immediately after version display."""
        args = _make_args(check=True)
        with patch("zotpilot.cli._get_current_version", return_value="0.2.0"), \
             patch("zotpilot.cli._get_latest_pypi_version", return_value="0.2.1"), \
             patch("zotpilot.cli.subprocess.run") as mock_run:
            result = cmd_update(args)
        assert result == 0
        mock_run.assert_not_called()

    def test_uv_cmd_used_in_upgrade(self, capsys):
        """installer='uv' with custom uv_cmd: upgrade uses uv_cmd prefix, not hardcoded ['uv']."""
        uv_cmd = [sys.executable, "-m", "uv"]
        args = _make_args(cli_only=True)
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Updated."
        with patch("zotpilot.cli._get_current_version", return_value="0.2.0"), \
             patch("zotpilot.cli._get_latest_pypi_version", return_value="0.2.1"), \
             patch("zotpilot.cli._detect_cli_installer", return_value=("uv", uv_cmd)), \
             patch("zotpilot.cli.subprocess.run", return_value=mock_result) as mock_run:
            cmd_update(args)
        upgrade_calls = [c for c in mock_run.call_args_list
                         if "upgrade" in (c[0][0] if c[0] else [])]
        assert len(upgrade_calls) == 1
        cmd_used = upgrade_calls[0][0][0]
        assert cmd_used[:len(uv_cmd)] == uv_cmd

    def test_subprocess_error_exits_1(self, capsys):
        """CalledProcessError on CLI upgrade → return 1, skill update NOT attempted."""
        args = _make_args()
        mock_get_skill_dirs = MagicMock()
        with patch("zotpilot.cli._get_current_version", return_value="0.2.0"), \
             patch("zotpilot.cli._get_latest_pypi_version", return_value="0.2.1"), \
             patch("zotpilot.cli._detect_cli_installer", return_value=("uv", ["uv"])), \
             patch("zotpilot.cli._get_skill_dirs", mock_get_skill_dirs), \
             patch("zotpilot.cli.subprocess.run",
                   side_effect=subprocess.CalledProcessError(1, "uv", stderr="update failed")):
            result = cmd_update(args)
        assert result == 1
        mock_get_skill_dirs.assert_not_called()

    def test_installer_unknown_exits_1(self, capsys):
        """installer='unknown' → return 1, manual instructions printed."""
        args = _make_args()
        with patch("zotpilot.cli._get_current_version", return_value="0.2.0"), \
             patch("zotpilot.cli._get_latest_pypi_version", return_value="0.2.1"), \
             patch("zotpilot.cli._detect_cli_installer", return_value=("unknown", None)), \
             patch("zotpilot.cli._get_skill_dirs", return_value=[]):
            result = cmd_update(args)
        assert result == 1
        out = capsys.readouterr().out
        assert "uv tool upgrade zotpilot" in out

    def test_uv_not_in_path_exits_1(self, capsys):
        """FileNotFoundError on uv subprocess → caught, manual cmd printed, return 1."""
        args = _make_args(cli_only=True)
        with patch("zotpilot.cli._get_current_version", return_value="0.2.0"), \
             patch("zotpilot.cli._get_latest_pypi_version", return_value="0.2.1"), \
             patch("zotpilot.cli._detect_cli_installer", return_value=("uv", ["uv"])), \
             patch("zotpilot.cli.subprocess.run", side_effect=FileNotFoundError()):
            result = cmd_update(args)
        assert result == 1
        out = capsys.readouterr().out
        assert "not found" in out.lower() or "manually" in out.lower()

    def test_git_not_in_path_exits_1(self, tmp_path, capsys):
        """git status raises FileNotFoundError → caught, manual cmd printed, return 1."""
        args = _make_args(skill_only=True)
        skill_dir = _make_skill_dir(tmp_path)
        sd = SkillDir(path=skill_dir, is_symlink=False, is_broken_symlink=False, is_duplicate=False)

        def fake_run(cmd, **kwargs):
            raise FileNotFoundError("git not found")

        with patch("zotpilot.cli._get_current_version", return_value="0.2.0"), \
             patch("zotpilot.cli._get_latest_pypi_version", return_value="0.2.1"), \
             patch("zotpilot.cli._get_skill_dirs", return_value=[sd]), \
             patch("zotpilot.cli.subprocess.run", side_effect=fake_run):
            result = cmd_update(args)
        assert result == 1
        out = capsys.readouterr().out
        assert "git" in out.lower()

    def test_git_pull_nonzero_exits_1(self, tmp_path, capsys):
        """git pull returns non-zero → stderr printed, return 1."""
        args = _make_args(skill_only=True)
        skill_dir = _make_skill_dir(tmp_path)
        sd = SkillDir(path=skill_dir, is_symlink=False, is_broken_symlink=False, is_duplicate=False)

        def fake_run(cmd, **kwargs):
            r = MagicMock()
            if "remote" in cmd:
                r.returncode = 1
                r.stdout = ""
                return r
            if "status" in cmd:
                r.returncode = 0
                r.stdout = ""
                return r
            if "pull" in cmd:
                r.returncode = 1
                r.stderr = "merge conflict detected"
                return r
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        with patch("zotpilot.cli._get_current_version", return_value="0.2.0"), \
             patch("zotpilot.cli._get_latest_pypi_version", return_value="0.2.1"), \
             patch("zotpilot.cli._get_skill_dirs", return_value=[sd]), \
             patch("zotpilot.cli.subprocess.run", side_effect=fake_run):
            result = cmd_update(args)
        assert result == 1
        out = capsys.readouterr().out
        assert "merge conflict detected" in out

    def test_symlink_skill_dir_skipped(self, tmp_path, capsys):
        """skill dir is_symlink=True → git pull not called."""
        args = _make_args(skill_only=True)
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        sym_dir = tmp_path / "sym_zotpilot"
        os.symlink(real_dir, sym_dir)

        sd = SkillDir(path=sym_dir, is_symlink=True, is_broken_symlink=False, is_duplicate=False)
        with patch("zotpilot.cli._get_current_version", return_value="0.2.0"), \
             patch("zotpilot.cli._get_latest_pypi_version", return_value="0.2.1"), \
             patch("zotpilot.cli._get_skill_dirs", return_value=[sd]), \
             patch("zotpilot.cli.subprocess.run") as mock_run:
            cmd_update(args)
        pull_calls = [c for c in mock_run.call_args_list if "pull" in (c[0][0] if c[0] else [])]
        assert len(pull_calls) == 0

    def test_dirty_skill_dir_skipped(self, tmp_path, capsys):
        """git status has output → git pull not called."""
        args = _make_args(skill_only=True)
        skill_dir = _make_skill_dir(tmp_path)
        sd = SkillDir(path=skill_dir, is_symlink=False, is_broken_symlink=False, is_duplicate=False)

        def fake_run(cmd, **kwargs):
            r = MagicMock()
            if "remote" in cmd:
                r.returncode = 1
                r.stdout = ""
                return r
            if "status" in cmd:
                r.returncode = 0
                r.stdout = " M modified_file.py"  # dirty
                return r
            r.returncode = 0
            r.stdout = ""
            return r

        with patch("zotpilot.cli._get_current_version", return_value="0.2.0"), \
             patch("zotpilot.cli._get_latest_pypi_version", return_value="0.2.1"), \
             patch("zotpilot.cli._get_skill_dirs", return_value=[sd]), \
             patch("zotpilot.cli.subprocess.run", side_effect=fake_run) as mock_run:
            cmd_update(args)
        pull_calls = [c for c in mock_run.call_args_list if "pull" in (c[0][0] if c[0] else [])]
        assert len(pull_calls) == 0

    def test_git_status_nonzero_skipped(self, tmp_path, capsys):
        """git status returncode != 0 → git pull not called."""
        args = _make_args(skill_only=True)
        skill_dir = _make_skill_dir(tmp_path)
        sd = SkillDir(path=skill_dir, is_symlink=False, is_broken_symlink=False, is_duplicate=False)

        def fake_run(cmd, **kwargs):
            r = MagicMock()
            if "remote" in cmd:
                r.returncode = 1
                r.stdout = ""
                return r
            if "status" in cmd:
                r.returncode = 128
                r.stdout = ""
                return r
            r.returncode = 0
            r.stdout = ""
            return r

        with patch("zotpilot.cli._get_current_version", return_value="0.2.0"), \
             patch("zotpilot.cli._get_latest_pypi_version", return_value="0.2.1"), \
             patch("zotpilot.cli._get_skill_dirs", return_value=[sd]), \
             patch("zotpilot.cli.subprocess.run", side_effect=fake_run) as mock_run:
            cmd_update(args)
        pull_calls = [c for c in mock_run.call_args_list if "pull" in (c[0][0] if c[0] else [])]
        assert len(pull_calls) == 0

    def test_skill_remote_mismatch_warns_but_does_not_skip(self, tmp_path, capsys):
        """Non-canonical remote URL → warning logged but git pull still called."""
        args = _make_args(skill_only=True)
        skill_dir = _make_skill_dir(tmp_path)
        sd = SkillDir(path=skill_dir, is_symlink=False, is_broken_symlink=False, is_duplicate=False)

        def fake_run(cmd, **kwargs):
            r = MagicMock()
            if "remote" in cmd and "get-url" in cmd:
                r.returncode = 0
                r.stdout = "https://example.com/someone-else/fork.git"
                return r
            if "status" in cmd:
                r.returncode = 0
                r.stdout = ""
                return r
            if "pull" in cmd:
                r.returncode = 0
                r.stdout = "Already up to date."
                r.stderr = ""
                return r
            r.returncode = 0
            r.stdout = ""
            return r

        with patch("zotpilot.cli._get_current_version", return_value="0.2.0"), \
             patch("zotpilot.cli._get_latest_pypi_version", return_value="0.2.1"), \
             patch("zotpilot.cli._get_skill_dirs", return_value=[sd]), \
             patch("zotpilot.cli.subprocess.run", side_effect=fake_run) as mock_run:
            result = cmd_update(args)

        pull_calls = [c for c in mock_run.call_args_list if "pull" in (c[0][0] if c[0] else [])]
        assert len(pull_calls) == 1  # pull WAS called despite URL mismatch
        out = capsys.readouterr().out
        assert "Note:" in out or "does not look" in out

    def test_broken_symlink_distinct_from_valid_symlink(self, tmp_path, capsys):
        """Broken symlink prints 'target missing'; valid symlink prints 'update the source repo'."""
        args = _make_args(skill_only=True)
        real_target = tmp_path / "real_target"
        real_target.mkdir()

        valid_sym = tmp_path / "valid_sym"
        os.symlink(real_target, valid_sym)

        broken_sym = tmp_path / "broken_sym"
        os.symlink(tmp_path / "nonexistent", broken_sym)

        sd_valid = SkillDir(path=valid_sym, is_symlink=True, is_broken_symlink=False, is_duplicate=False)
        sd_broken = SkillDir(path=broken_sym, is_symlink=True, is_broken_symlink=True, is_duplicate=False)

        with patch("zotpilot.cli._get_current_version", return_value="0.2.0"), \
             patch("zotpilot.cli._get_latest_pypi_version", return_value="0.2.1"), \
             patch("zotpilot.cli._get_skill_dirs", return_value=[sd_valid, sd_broken]), \
             patch("zotpilot.cli.subprocess.run"):
            cmd_update(args)

        out = capsys.readouterr().out
        assert "target missing" in out
        assert "update the source repo" in out

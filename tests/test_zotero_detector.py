"""Tests for Zotero auto-detection."""
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from zotpilot.zotero_detector import (
    detect_zotero_data_dir,
    _validate_data_dir,
    _parse_profiles_ini,
    _parse_prefs_js,
)


class TestValidateDataDir:
    def test_valid_dir(self, tmp_path):
        (tmp_path / "zotero.sqlite").touch()
        assert _validate_data_dir(tmp_path) is True

    def test_missing_sqlite(self, tmp_path):
        assert _validate_data_dir(tmp_path) is False

    def test_nonexistent_dir(self):
        assert _validate_data_dir(Path("/nonexistent/path")) is False


class TestDetection:
    def test_configured_path_takes_priority(self, tmp_path):
        (tmp_path / "zotero.sqlite").touch()
        result = detect_zotero_data_dir(configured_path=str(tmp_path))
        assert result == tmp_path

    def test_invalid_configured_path_tries_fallbacks(self, tmp_path):
        with patch("zotpilot.zotero_detector._detect_from_profiles", return_value=None):
            result = detect_zotero_data_dir(configured_path="/nonexistent")
            assert result is None or isinstance(result, Path)

    def test_fallback_to_home_zotero(self, tmp_path, monkeypatch):
        home_zotero = tmp_path / "Zotero"
        home_zotero.mkdir()
        (home_zotero / "zotero.sqlite").touch()

        with patch("zotpilot.zotero_detector._detect_from_profiles", return_value=None):
            with patch("pathlib.Path.home", return_value=tmp_path):
                result = detect_zotero_data_dir()
                assert result == home_zotero


class TestParseProfilesIni:
    def test_single_default_profile(self, tmp_path):
        ini_content = """[General]
StartWithLastProfile=1

[Profile0]
Name=default
IsRelative=1
Path=Profiles/abc123.default
Default=1
"""
        ini_path = tmp_path / "profiles.ini"
        ini_path.write_text(ini_content)

        result = _parse_profiles_ini(ini_path, tmp_path)
        assert result == tmp_path / "Profiles" / "abc123.default"

    def test_no_default_uses_first(self, tmp_path):
        ini_content = """[Profile0]
Name=first
IsRelative=1
Path=Profiles/first.default
"""
        ini_path = tmp_path / "profiles.ini"
        ini_path.write_text(ini_content)

        result = _parse_profiles_ini(ini_path, tmp_path)
        assert result == tmp_path / "Profiles" / "first.default"

    def test_absolute_path(self, tmp_path):
        ini_content = """[Profile0]
Name=custom
IsRelative=0
Path=/custom/path/profile
Default=1
"""
        ini_path = tmp_path / "profiles.ini"
        ini_path.write_text(ini_content)

        result = _parse_profiles_ini(ini_path, tmp_path)
        assert result == Path("/custom/path/profile")


class TestParsePrefsJs:
    def test_custom_datadir(self, tmp_path):
        prefs = '''user_pref("extensions.zotero.useDataDir", true);
user_pref("extensions.zotero.dataDir", "/custom/zotero/data");
'''
        prefs_path = tmp_path / "prefs.js"
        prefs_path.write_text(prefs)

        result = _parse_prefs_js(prefs_path)
        assert result == Path("/custom/zotero/data")

    def test_no_custom_datadir(self, tmp_path):
        prefs = 'user_pref("extensions.zotero.lastVersion", "7.0.0");\n'
        prefs_path = tmp_path / "prefs.js"
        prefs_path.write_text(prefs)

        result = _parse_prefs_js(prefs_path)
        assert result is None

    def test_datadir_without_useDataDir(self, tmp_path):
        prefs = 'user_pref("extensions.zotero.dataDir", "/some/path");\n'
        prefs_path = tmp_path / "prefs.js"
        prefs_path.write_text(prefs)

        result = _parse_prefs_js(prefs_path)
        assert result is None  # useDataDir not set to true

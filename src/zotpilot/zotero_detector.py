"""Auto-detect Zotero data directory from profiles.ini and prefs.js.

Detection strategy:
1. User-configured path (highest priority)
2. Parse profiles.ini → find active profile → read prefs.js for dataDir
3. Fallback to ~/Zotero (common default)
4. Platform-specific defaults
"""
from __future__ import annotations

import configparser
import logging
import platform
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Regex to extract dataDir from prefs.js
_DATADIR_RE = re.compile(
    r'user_pref\("extensions\.zotero\.dataDir",\s*"([^"]+)"\);'
)
_USE_DATADIR_RE = re.compile(
    r'user_pref\("extensions\.zotero\.useDataDir",\s*true\);'
)


def _profile_dirs(system: str) -> list[Path]:
    home = Path.home()
    return {
        "Darwin": [home / "Library" / "Application Support" / "Zotero"],
        "Linux": [home / ".zotero" / "zotero"],
        "Windows": [home / "AppData" / "Roaming" / "Zotero" / "Zotero"],
    }.get(system, [])


def _data_dirs(system: str) -> list[Path]:
    home = Path.home()
    return {
        "Darwin": [home / "Zotero"],
        "Linux": [home / "Zotero"],
        "Windows": [home / "Zotero"],
    }.get(system, [])


def detect_zotero_data_dir(configured_path: str | None = None) -> Path | None:
    """Detect Zotero data directory.

    Args:
        configured_path: User-configured path (highest priority).

    Returns:
        Path to Zotero data directory, or None if not found.
    """
    # Priority 1: User-configured path
    if configured_path:
        path = Path(configured_path).expanduser()
        if _validate_data_dir(path):
            return path
        logger.warning(f"Configured path is not a valid Zotero data dir: {path}")

    # Priority 2: Parse profiles.ini
    detected = _detect_from_profiles()
    if detected and _validate_data_dir(detected):
        logger.info(f"Detected Zotero data dir from profiles: {detected}")
        return detected

    # Priority 3: Default ~/Zotero
    default = Path.home() / "Zotero"
    if _validate_data_dir(default):
        logger.info(f"Using default Zotero data dir: {default}")
        return default

    # Priority 4: Platform-specific defaults
    system = platform.system()
    for data_dir in _data_dirs(system):
        if _validate_data_dir(data_dir):
            logger.info(f"Using platform default Zotero data dir: {data_dir}")
            return data_dir

    return None


def _validate_data_dir(path: Path) -> bool:
    """Check if path is a valid Zotero data directory."""
    return path.is_dir() and (path / "zotero.sqlite").exists()


def _detect_from_profiles() -> Path | None:
    """Parse profiles.ini to find the active profile's data directory."""
    system = platform.system()

    for profile_dir in _profile_dirs(system):
        profiles_ini = profile_dir / "profiles.ini"
        if not profiles_ini.exists():
            continue

        profile_path = _parse_profiles_ini(profiles_ini, profile_dir)
        if profile_path is None:
            continue

        prefs_path = profile_path / "prefs.js"
        if not prefs_path.exists():
            continue

        data_dir = _parse_prefs_js(prefs_path)
        if data_dir:
            return data_dir

    return None


def _parse_profiles_ini(ini_path: Path, base_dir: Path) -> Path | None:
    """Parse profiles.ini and return the active profile directory."""
    config = configparser.ConfigParser()
    config.read(str(ini_path))

    # Find the default profile
    for section in config.sections():
        if not section.startswith("Profile"):
            continue

        is_default = config.get(section, "Default", fallback="0") == "1"
        if not is_default:
            continue

        is_relative = config.get(section, "IsRelative", fallback="1") == "1"
        path_str = config.get(section, "Path", fallback=None)

        if not path_str:
            continue

        if is_relative:
            return base_dir / path_str
        return Path(path_str)

    # If no default found, use the first profile
    for section in config.sections():
        if not section.startswith("Profile"):
            continue

        is_relative = config.get(section, "IsRelative", fallback="1") == "1"
        path_str = config.get(section, "Path", fallback=None)

        if not path_str:
            continue

        if is_relative:
            return base_dir / path_str
        return Path(path_str)

    return None


def _parse_prefs_js(prefs_path: Path) -> Path | None:
    """Parse prefs.js to extract the Zotero data directory."""
    try:
        content = prefs_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    # Check if custom dataDir is enabled
    if not _USE_DATADIR_RE.search(content):
        return None

    match = _DATADIR_RE.search(content)
    if not match:
        return None

    data_dir = Path(match.group(1)).expanduser()
    return data_dir

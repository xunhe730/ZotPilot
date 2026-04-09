# TODO: This is a copy of scripts/platforms.py for wheel distribution.
# Deduplicate once install bootstrap is reworked. Keep in sync manually.
"""Cross-platform MCP server registration for ZotPilot.

Supports:
  Tier 1 (Skill + MCP): Claude Code, Codex CLI, OpenCode, Gemini CLI, Cursor, Windsurf
  Tier 2 (MCP only):    Cline, Roo Code

Security note: CLI-based platforms (claude, codex, gemini) require passing
env vars via -e flags on the command line. This briefly exposes keys in the
process table. This is the only mechanism these CLIs provide. For higher
security, register manually and use environment variables or secret managers.
"""
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

import tomllib

# ---------------------------------------------------------------------------
# Platform definitions
# ---------------------------------------------------------------------------

PLATFORMS = {
    "claude-code": {
        "tier": 1,
        "binary": "claude",
        "label": "Claude Code",
        "skills_dir": "~/.claude/skills",
    },
    "codex": {
        "tier": 1,
        "binary": "codex",
        "label": "Codex CLI",
        "skills_dir": "~/.agents/skills",
    },
    "opencode": {
        "tier": 1,
        "binary": "opencode",
        "label": "OpenCode",
        "skills_dir": "~/.config/opencode/skills",
    },
    "gemini": {
        "tier": 1,
        "binary": "gemini",
        "label": "Gemini CLI",
        "skills_dir": "~/.gemini/skills",
    },
    "cursor": {
        "tier": 1,
        "binary": None,
        "label": "Cursor",
        "skills_dir": "~/.cursor/skills",
    },
    "windsurf": {
        "tier": 1,
        "binary": None,
        "label": "Windsurf",
        "skills_dir": "~/.codeium/windsurf/skills",
    },
}

SUPPORTED_PLATFORM_NAMES: tuple[str, ...] = ("claude-code", "codex")


@dataclass(frozen=True)
class PlatformRuntimeState:
    platform: str
    label: str
    supported: bool
    detected: bool
    registered: bool
    config_path: str | None = None
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    skill_dirs: tuple[str, ...] = ()
    skill_hash_ok: bool = False
    registration_hash_ok: bool = False


@dataclass(frozen=True)
class RuntimeState:
    package_version: str
    supported_targets: tuple[str, ...]
    platforms: dict[str, PlatformRuntimeState]


@dataclass(frozen=True)
class DesiredRuntime:
    command: str
    args: tuple[str, ...]
    env: dict[str, str]
    targets: tuple[str, ...]


@dataclass(frozen=True)
class ChangeSet:
    deploy_skill_platforms: tuple[str, ...]
    register_platforms: tuple[str, ...]
    drift_state: str
    reasons: dict[str, list[str]]


@dataclass(frozen=True)
class ApplyResult:
    deployed: tuple[str, ...]
    registered: tuple[str, ...]
    restart_required: bool


@dataclass(frozen=True)
class ReconcileResult:
    current: RuntimeState
    desired: DesiredRuntime
    changes: ChangeSet
    applied: ApplyResult | None = None


def _is_windows() -> bool:
    return platform.system() == "Windows"


def _home() -> Path:
    return Path.home()


def _supported_targets(platforms: list[str] | None = None) -> list[str]:
    requested = platforms or detect_platforms()
    return [plat for plat in requested if plat in SUPPORTED_PLATFORM_NAMES]


def _zotpilot_command(allow_fallback: bool = True) -> str:
    """Return the reliable absolute path to the zotpilot binary.

    Resolution order:
    1. shutil.which() — works when binary is already on PATH
    2. uv tool dir --bin — finds uv's bin directory even right after
       a fresh `uv tool install` in the same process (PATH not yet updated)
    3. Falls back to bare 'zotpilot' as last resort
    """
    # 1. Try PATH lookup
    path = shutil.which("zotpilot")
    if path:
        return path

    # 2. Windows: pip --user installs to %APPDATA%\Python\PythonXYY\Scripts\
    if _is_windows():
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            py_ver = f"Python{sys.version_info.major}{sys.version_info.minor}"
            win_pip_dir = Path(appdata) / "Python" / py_ver / "Scripts"
            for name in ("zotpilot.exe", "zotpilot"):
                candidate = win_pip_dir / name
                if candidate.exists():
                    return str(candidate)

    # 3. Ask uv where it installs tool binaries
    uv = shutil.which("uv")
    if not uv:
        # uv may have been installed via pip and not be in PATH on Windows
        try:
            r = subprocess.run(
                [sys.executable, "-m", "uv", "tool", "dir", "--bin"],
                capture_output=True, text=True,
            )
            if r.returncode == 0:
                bin_dir = r.stdout.strip()
                for name in ("zotpilot", "zotpilot.exe"):
                    candidate = Path(bin_dir) / name
                    if candidate.exists():
                        return str(candidate)
        except FileNotFoundError:
            pass
    if uv:
        r = subprocess.run(
            [uv, "tool", "dir", "--bin"],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            bin_dir = r.stdout.strip()
            for name in ("zotpilot", "zotpilot.exe"):
                candidate = Path(bin_dir) / name
                if candidate.exists():
                    return str(candidate)

    # 4. Last resort
    if not allow_fallback:
        raise RuntimeError(
            "zotpilot binary not found. Install first: "
            "python3 scripts/run.py (Tier 1) or pip install zotpilot (Tier 2)."
        )
    return "zotpilot"


# ---------------------------------------------------------------------------
# Config file paths (per-platform)
# ---------------------------------------------------------------------------

def _mcp_config_path(plat: str) -> Path | None:
    """Return the MCP config file path for platforms that use config files."""
    home = _home()
    if _is_windows():
        appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        code_support = appdata / "Code"
        paths = {
            "opencode": home / ".config" / "opencode" / "opencode.json",
            "gemini": home / ".gemini" / "settings.json",
            "cursor": home / ".cursor" / "mcp.json",
            "windsurf": appdata / "windsurf" / "mcp_config.json",
            "cline": code_support / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json",  # noqa: E501
            "roo": code_support / "User" / "globalStorage" / "rooveterinaryinc.roo-cline" / "settings" / "mcp_settings.json",  # noqa: E501
        }
    else:  # macOS / Linux
        is_mac = platform.system() == "Darwin"
        code_support = (home / "Library" / "Application Support" / "Code") if is_mac else (home / ".config" / "Code")
        paths = {
            "opencode": home / ".config" / "opencode" / "opencode.json",
            "gemini": home / ".gemini" / "settings.json",
            "cursor": home / ".cursor" / "mcp.json",
            "windsurf": home / ".codeium" / "windsurf" / "mcp_config.json",
            "cline": code_support / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json",  # noqa: E501
            "roo": code_support / "User" / "globalStorage" / "rooveterinaryinc.roo-cline" / "settings" / "mcp_settings.json",  # noqa: E501
        }
    return paths.get(plat)


def _codex_config_path() -> Path:
    return _home() / ".codex" / "config.toml"


def _claude_config_path() -> Path:
    return _home() / ".claude.json"


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _detect_app_install(plat: str) -> bool:
    """Check whether an editor-based client is actually installed.

    Cannot rely on MCP config files alone — ``zotpilot register`` itself may
    have created them as a side effect on a previous run.  Look for evidence
    that the actual application exists.
    """
    home = _home()
    system = platform.system()

    if plat == "cursor":
        if system == "Darwin":
            return Path("/Applications/Cursor.app").exists()
        if system == "Windows":
            local_appdata = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
            return (local_appdata / "Programs" / "cursor" / "Cursor.exe").exists()
        # Linux
        return any(shutil.which(name) for name in ("cursor",))

    if plat == "windsurf":
        if system == "Darwin":
            return Path("/Applications/Windsurf.app").exists()
        if system == "Windows":
            local_appdata = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
            return (local_appdata / "Programs" / "Windsurf" / "Windsurf.exe").exists()
        return any(shutil.which(name) for name in ("windsurf",))

    if plat == "opencode":
        # OpenCode without a binary on PATH may still be a real install
        # via local node_modules.  Look for a package.json that mentions
        # opencode under its config dir.
        cfg_dir = home / ".config" / "opencode"
        pkg = cfg_dir / "package.json"
        if pkg.exists():
            try:
                data = json.loads(pkg.read_text(encoding="utf-8"))
                deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
                if any("opencode" in name for name in deps):
                    return True
            except (json.JSONDecodeError, OSError):
                pass
        return False

    return False


def detect_platforms() -> list[str]:
    """Return list of platform names actually installed on this machine.

    Detection rules:
    - CLI-based clients (Claude Code, Codex, Gemini, OpenCode): require the
      binary to be present on PATH.
    - Editor-based clients (Cursor, Windsurf): require the actual application
      bundle/executable to be installed.
    - OpenCode (no global binary): also accept a real install discovered via
      ``~/.config/opencode/package.json``.

    Critically, ``zotpilot register`` may create the per-platform config dirs
    as a side effect when deploying skill files.  Detection therefore must NOT
    treat the existence of the config dir or file as proof of installation.
    """
    found: set[str] = set()

    # Step 1: CLI binaries
    for name, info in PLATFORMS.items():
        binary = info.get("binary")
        if binary and shutil.which(binary):
            found.add(name)

    # Step 2: Editor-based clients (and OpenCode local install fallback)
    for name in ("cursor", "windsurf", "opencode"):
        if name in found:
            continue
        if _detect_app_install(name):
            found.add(name)

    return [p for p in PLATFORMS if p in found]


def _inspect_codex_registration() -> tuple[bool, str | None, tuple[str, ...], dict[str, str], str | None]:
    config_path = _codex_config_path()
    if not config_path.exists():
        return False, None, (), {}, str(config_path)
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False, None, (), {}, str(config_path)
    entry = data.get("mcp_servers", {}).get("zotpilot")
    if not isinstance(entry, dict):
        return False, None, (), {}, str(config_path)
    command = entry.get("command")
    args = tuple(str(arg) for arg in entry.get("args", []) or ())
    env = {str(k): str(v) for k, v in (entry.get("env", {}) or {}).items()}
    return True, command, args, env, str(config_path)


def _inspect_claude_registration() -> tuple[bool, str | None, tuple[str, ...], dict[str, str], str | None]:
    config_path = _claude_config_path()
    if not config_path.exists():
        return False, None, (), {}, str(config_path)
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, None, (), {}, str(config_path)
    entry = data.get("mcpServers", {}).get("zotpilot")
    if not isinstance(entry, dict):
        return False, None, (), {}, str(config_path)
    command = entry.get("command")
    args = tuple(str(arg) for arg in entry.get("args", []) or ())
    env = {str(k): str(v) for k, v in (entry.get("env", {}) or {}).items()}
    return True, command, args, env, str(config_path)


def _inspect_registration(plat: str) -> tuple[bool, str | None, tuple[str, ...], dict[str, str], str | None]:
    if plat == "codex":
        return _inspect_codex_registration()
    if plat == "claude-code":
        return _inspect_claude_registration()

    config_path = _mcp_config_path(plat)
    if not config_path:
        return False, None, (), {}, None
    expanded = config_path.expanduser()
    if not expanded.exists():
        return False, None, (), {}, str(expanded)
    try:
        text = expanded.read_text(encoding="utf-8")
        if expanded.suffix == ".jsonc":
            text = _strip_jsonc_comments(text)
        data = json.loads(text) if text.strip() else {}
    except (json.JSONDecodeError, OSError):
        return False, None, (), {}, str(expanded)

    mcp_key = "mcp" if "opencode" in str(expanded) else "mcpServers"
    entry = data.get(mcp_key, {}).get("zotpilot")
    if not isinstance(entry, dict):
        return False, None, (), {}, str(expanded)
    command = entry.get("command")
    args = tuple(str(arg) for arg in entry.get("args", []) or ())
    env_key = "environment" if "opencode" in str(expanded) else "env"
    env = {str(k): str(v) for k, v in (entry.get(env_key, {}) or {}).items()}
    return True, str(command) if command is not None else None, args, env, str(expanded)


# ---------------------------------------------------------------------------
# Build env dict from user-provided credentials
# ---------------------------------------------------------------------------

def _build_env(
    gemini_key: str | None = None,
    dashscope_key: str | None = None,
    zotero_api_key: str | None = None,
    zotero_user_id: str | None = None,
) -> dict[str, str]:
    env = {}
    if gemini_key:
        env["GEMINI_API_KEY"] = gemini_key
    if dashscope_key:
        env["DASHSCOPE_API_KEY"] = dashscope_key
    if zotero_api_key:
        env["ZOTERO_API_KEY"] = zotero_api_key
    if zotero_user_id:
        env["ZOTERO_USER_ID"] = zotero_user_id
    return env


def _skill_state_for_platform(plat: str) -> tuple[tuple[str, ...], bool]:
    info = PLATFORMS.get(plat, {})
    skills_dir = info.get("skills_dir")
    if not skills_dir:
        return (), False
    base = Path(skills_dir).expanduser()
    skill_files = _skill_source_files()
    if not skill_files:
        return (), False
    deployed: list[str] = []
    all_ok = True
    for source in skill_files:
        target = base / _skill_name_for_file(source)
        deployed.append(str(target))
        marker = _read_version_marker(target)
        expected_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        if not target.exists() or not (target / "SKILL.md").exists():
            all_ok = False
            continue
        deployed_hashes = (marker or {}).get("skill_hashes", {})
        if deployed_hashes.get(source.name) != expected_hash:
            all_ok = False
    return tuple(deployed), all_ok


def inspect_current_state(
    config_env: dict[str, str] | None = None,
    targets: list[str] | None = None,
) -> RuntimeState:
    from . import __version__

    target_names = tuple(_supported_targets(targets))
    detected = set(detect_platforms())
    desired_command = _zotpilot_command()
    desired_args: tuple[str, ...] = ()
    desired_env = config_env or {}

    states: dict[str, PlatformRuntimeState] = {}
    for plat, info in PLATFORMS.items():
        registered, command, args, env, config_path = _inspect_registration(plat)
        skill_dirs, skill_hash_ok = _skill_state_for_platform(plat)
        supported = plat in SUPPORTED_PLATFORM_NAMES
        registration_hash_ok = (
            registered
            and command == desired_command
            and tuple(args) == desired_args
            and all(env.get(k) == v for k, v in desired_env.items())
        )
        states[plat] = PlatformRuntimeState(
            platform=plat,
            label=str(info.get("label", plat)),
            supported=supported,
            detected=plat in detected,
            registered=registered,
            config_path=config_path,
            command=command,
            args=args,
            env=env,
            skill_dirs=skill_dirs,
            skill_hash_ok=skill_hash_ok,
            registration_hash_ok=registration_hash_ok,
        )
    return RuntimeState(
        package_version=__version__,
        supported_targets=target_names,
        platforms=states,
    )


def plan_runtime_changes(desired: DesiredRuntime, current: RuntimeState) -> ChangeSet:
    deploy: list[str] = []
    register: list[str] = []
    reasons: dict[str, list[str]] = {}

    for plat in desired.targets:
        state = current.platforms[plat]
        platform_reasons: list[str] = []
        if not state.skill_hash_ok:
            deploy.append(plat)
            platform_reasons.append("skills-out-of-sync")
        if not state.registered:
            register.append(plat)
            platform_reasons.append("not-registered")
        elif state.command != desired.command or tuple(state.args) != desired.args:
            register.append(plat)
            platform_reasons.append("command-drift")
        elif any(state.env.get(k) != v for k, v in desired.env.items()):
            register.append(plat)
            platform_reasons.append("env-drift")
        if platform_reasons:
            reasons[plat] = platform_reasons

    if not reasons:
        drift_state = "clean"
    elif len(register) < len(desired.targets) and register:
        drift_state = "partially-registered"
    else:
        drift_state = "needs-sync"

    return ChangeSet(
        deploy_skill_platforms=tuple(dict.fromkeys(deploy)),
        register_platforms=tuple(dict.fromkeys(register)),
        drift_state=drift_state,
        reasons=reasons,
    )


def apply_runtime_changes(
    desired: DesiredRuntime,
    changes: ChangeSet,
) -> ApplyResult:
    deployed: list[str] = []
    registered: list[str] = []

    if changes.deploy_skill_platforms:
        deploy_results = deploy_skills(platforms=list(changes.deploy_skill_platforms))
        deployed = [plat for plat, ok in deploy_results.items() if ok]

    for plat in changes.register_platforms:
        fn = _REGISTER_FNS.get(plat)
        if fn is None:
            continue
        if fn(dict(desired.env)):
            registered.append(plat)

    return ApplyResult(
        deployed=tuple(deployed),
        registered=tuple(registered),
        restart_required=bool(deployed or registered),
    )


def reconcile_runtime(
    *,
    platforms: list[str] | None = None,
    gemini_key: str | None = None,
    dashscope_key: str | None = None,
    zotero_api_key: str | None = None,
    zotero_user_id: str | None = None,
    apply: bool = False,
) -> ReconcileResult:
    desired_env = _build_env(gemini_key, dashscope_key, zotero_api_key, zotero_user_id)
    current = inspect_current_state(desired_env, platforms)
    desired = DesiredRuntime(
        command=_zotpilot_command(),
        args=(),
        env=desired_env,
        targets=current.supported_targets,
    )
    changes = plan_runtime_changes(desired, current)
    applied = apply_runtime_changes(desired, changes) if apply else None
    return ReconcileResult(current=current, desired=desired, changes=changes, applied=applied)


# ---------------------------------------------------------------------------
# JSONC parsing helper
# ---------------------------------------------------------------------------

def _strip_jsonc_comments(text: str) -> str:
    """Strip // comments from JSONC, preserving URLs inside strings."""
    result = []
    in_string = False
    escape = False
    i = 0
    while i < len(text):
        ch = text[i]
        if escape:
            result.append(ch)
            escape = False
            i += 1
            continue
        if ch == '\\' and in_string:
            result.append(ch)
            escape = True
            i += 1
            continue
        if ch == '"':
            in_string = not in_string
            result.append(ch)
            i += 1
            continue
        if not in_string and ch == '/' and i + 1 < len(text) and text[i + 1] == '/':
            # Skip to end of line
            while i < len(text) and text[i] != '\n':
                i += 1
            continue
        result.append(ch)
        i += 1
    return ''.join(result)


# ---------------------------------------------------------------------------
# Skill directory hints (Tier 1 only)
# ---------------------------------------------------------------------------

def print_skill_hints(platforms: list[str]) -> None:
    """Print the skill directory path for each Tier 1 platform.

    Registration now deploys packaged skill files as individual directories
    (one per skill).  This function reports the current state.
    """
    tier1 = [p for p in platforms if PLATFORMS.get(p, {}).get("tier") == 1]
    if not tier1:
        return
    for plat in tier1:
        info = PLATFORMS[plat]
        base = Path(info.get("skills_dir", "")).expanduser()
        router_dir = base / "zotpilot"
        if (router_dir / "SKILL.md").is_file():
            # Count deployed skill dirs
            skill_dirs = [d for d in base.iterdir() if d.is_dir() and (d / "SKILL.md").is_file()
                          and d.name.startswith(("zotpilot", "ztp-"))]
            print(f"  {info['label']}: {len(skill_dirs)} skills deployed under {base}")
        else:
            print(f"  {info['label']}: skills NOT found — register will deploy to {base}")


_VERSION_MARKER = ".zotpilot-version.json"


def _skill_source_dir() -> Path:
    source = resources.files("zotpilot").joinpath("skills")
    return Path(str(source))


def _skill_source_files() -> list[Path]:
    source = _skill_source_dir()
    if not source.exists():
        return []
    return sorted(path for path in source.glob("*.md") if path.is_file())


def _skill_name_for_file(skill_file: Path) -> str:
    """Map source filename to deployed directory name.

    SKILL.md → zotpilot (routing shell)
    ztp-research.md → ztp-research
    """
    return "zotpilot" if skill_file.name == "SKILL.md" else skill_file.stem


def _skill_targets_for_platform(
    _plat: str,
    skills_dir: Path,
    skill_files: list[Path],
) -> list[tuple[Path, list[Path]]]:
    """Each skill file gets its own directory with SKILL.md inside.

    Deployed structure:
        skills_dir/zotpilot/SKILL.md      (routing shell)
        skills_dir/ztp-research/SKILL.md
        skills_dir/ztp-setup/SKILL.md
        skills_dir/ztp-review/SKILL.md
        skills_dir/ztp-profile/SKILL.md
    """
    return [
        (skills_dir / _skill_name_for_file(skill_file), [skill_file])
        for skill_file in skill_files
    ]


def _version_marker_path(target: Path) -> Path:
    return target / _VERSION_MARKER


def _read_version_marker(target: Path) -> dict | None:
    marker = _version_marker_path(target)
    if not marker.exists():
        return None
    try:
        return json.loads(marker.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write_version_marker(target: Path, version: str, skill_files: list[Path]) -> None:
    marker = _version_marker_path(target)
    payload = {
        "version": version,
        "deployed_at": datetime.now(timezone.utc).isoformat(),
        "skills": [path.name for path in skill_files],
        "skill_hashes": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in skill_files
        },
    }
    marker.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _migrate_legacy_bundle(skills_dir: Path) -> None:
    """Remove a legacy bundle directory or symlink at skills_dir/zotpilot.

    v0.4.x deployed all skill files into a single ``zotpilot/`` directory
    (bundle layout) or used a symlink to the repo root.  v0.5.0 uses flat
    layout (one directory per skill).  If the old ``zotpilot/`` target is a
    symlink or a bundle with multiple ``.md`` files, replace it so the new
    per-skill directories can be created cleanly.
    """
    legacy = skills_dir / "zotpilot"
    if not legacy.exists() and not legacy.is_symlink():
        return

    # Symlink (editable install or manual) → remove unconditionally
    if legacy.is_symlink():
        legacy.unlink()
        print(f"    migrated: removed legacy symlink {legacy}")
        return

    # Bundle directory with multiple .md files → remove
    md_files = list(legacy.glob("*.md"))
    if len(md_files) > 1:
        shutil.rmtree(legacy)
        print(f"    migrated: removed legacy bundle directory {legacy}")
        return

    # Single SKILL.md inside zotpilot/ → already flat layout, keep it


def _should_skip_deploy(target: Path, version: str, skill_files: list[Path]) -> tuple[bool, str]:
    if target.is_symlink():
        # Stale symlink from editable install — remove and redeploy
        target.unlink()
        return False, "deploy"
    marker = _read_version_marker(target)
    if not marker:
        return False, "deploy"
    target_version = str(marker.get("version", "")).strip()
    if not target_version:
        return False, "deploy"
    expected_hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in skill_files
    }
    if marker.get("skill_hashes") != expected_hashes:
        return False, "refresh-content"
    if target_version == version:
        return True, "up-to-date"
    if target_version > version:
        return True, "target-newer-than-package"
    return False, "upgrade"


def deploy_skills(platforms: list[str] | None = None) -> dict[str, bool]:
    """Deploy packaged skill files as individual directories per platform.

    Each skill file becomes its own directory with a ``SKILL.md`` inside:
        ``~/.claude/skills/ztp-research/SKILL.md``

    On upgrade from v0.4.x bundle layout, the legacy ``zotpilot/`` bundle
    directory (or symlink) is automatically migrated first.
    """
    from . import __version__

    skill_files = _skill_source_files()
    if not skill_files:
        raise FileNotFoundError(f"No packaged skill files found in {_skill_source_dir()}")

    if not platforms:
        # Only deploy to platforms that are actually installed.
        # Filter further by which ones have a skills_dir defined.
        detected = set(detect_platforms())
        platforms = [name for name, info in PLATFORMS.items()
                     if info.get("skills_dir") and name in detected]
        if not platforms:
            print("  No skill-supporting platforms detected — skipping skill deployment")
            return {}

    results: dict[str, bool] = {}
    for plat in platforms:
        info = PLATFORMS.get(plat)
        skills_dir = info.get("skills_dir") if info else None
        if not skills_dir:
            results[plat] = False
            continue

        base = Path(skills_dir).expanduser()

        # Migrate legacy bundle/symlink before deploying flat dirs
        _migrate_legacy_bundle(base)

        targets = _skill_targets_for_platform(plat, base, skill_files)
        platform_ok = True

        for target, source_files in targets:
            skip, reason = _should_skip_deploy(target, __version__, source_files)
            if skip:
                print(f"  {info['label']}: {target.name} {reason}")
                platform_ok = platform_ok and (reason == "up-to-date")
                continue

            target.mkdir(parents=True, exist_ok=True)
            # Clean existing .md files before writing new ones
            for existing_md in target.glob("*.md"):
                existing_md.unlink()
            for skill_file in source_files:
                shutil.copy2(skill_file, target / "SKILL.md")
            _write_version_marker(target, __version__, source_files)
            print(f"  {info['label']}: deployed {target.name} to {target}")

        results[plat] = platform_ok

    return results


# ---------------------------------------------------------------------------
# CLI-based registration (Tier 1)
# ---------------------------------------------------------------------------

def _register_claude_code(env: dict[str, str]) -> bool:
    """Register via ``claude mcp add``.

    The ``-e/--env`` flag is declared as variadic in claude-code (``<env...>``),
    so any positional args placed *after* ``-e`` are greedily consumed as env
    values.  To avoid that, put ``<name>`` and ``<commandOrUrl>`` FIRST and the
    variadic ``-e`` flags LAST.
    """
    try:
        zp = _zotpilot_command(allow_fallback=False)
    except RuntimeError as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        return False
    subprocess.run(["claude", "mcp", "remove", "zotpilot"],
                   capture_output=True, text=True)
    cmd = ["claude", "mcp", "add", "--scope", "user", "zotpilot", zp]
    for k, v in env.items():
        cmd.extend(["-e", f"{k}={v}"])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ERROR: {result.stderr.strip()}", file=sys.stderr)
        return False
    return True


def _register_codex(env: dict[str, str]) -> bool:
    """Register via `codex mcp add`."""
    try:
        zp = _zotpilot_command(allow_fallback=False)
    except RuntimeError as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        return False
    subprocess.run(["codex", "mcp", "remove", "zotpilot"],
                   capture_output=True, text=True)
    cmd = ["codex", "mcp", "add", "zotpilot"]
    for k, v in env.items():
        cmd.extend(["--env", f"{k}={v}"])
    cmd.extend(["--", zp])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ERROR: {result.stderr.strip()}", file=sys.stderr)
        return False
    return True


def _register_gemini(env: dict[str, str]) -> bool:
    """Register via `gemini mcp add`.

    Syntax: gemini mcp add [options] <name> <command> [args...]
    No -- separator; command is a positional argument.
    """
    try:
        zp = _zotpilot_command(allow_fallback=False)
    except RuntimeError as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        return False
    subprocess.run(["gemini", "mcp", "remove", "zotpilot"],
                   capture_output=True, text=True)
    cmd = ["gemini", "mcp", "add", "-s", "user"]
    for k, v in env.items():
        cmd.extend(["-e", f"{k}={v}"])
    cmd.extend(["zotpilot", zp])  # <name> <command>, no --
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ERROR: {result.stderr.strip()}", file=sys.stderr)
        return False
    return True


# ---------------------------------------------------------------------------
# Config-file-based registration (OpenCode + Tier 2)
# ---------------------------------------------------------------------------

def _write_mcp_config(config_path: Path, env: dict[str, str]) -> bool:
    """Read-modify-write an MCP entry into a JSON config file.

    Handles: file missing, existing entries, other MCP servers.
    Uses atomic write (temp + rename) with backup.
    """
    config_path = config_path.expanduser()

    # Determine JSON structure: most use {"mcpServers": {...}}, opencode uses {"mcp": {...}}
    is_opencode = "opencode" in str(config_path)
    mcp_key = "mcp" if is_opencode else "mcpServers"

    # Read existing config
    existing = {}
    already_backed_up = False
    if config_path.exists():
        try:
            text = config_path.read_text(encoding="utf-8")
            if config_path.suffix == ".jsonc":
                text = _strip_jsonc_comments(text)
            existing = json.loads(text) if text.strip() else {}
        except (json.JSONDecodeError, OSError) as e:
            print(f"  WARNING: Could not parse {config_path}: {e}", file=sys.stderr)
            bak = config_path.with_suffix(config_path.suffix + ".bak")
            print(f"  Creating backup at {bak}", file=sys.stderr)
            try:
                shutil.copy2(config_path, bak)
                already_backed_up = True
            except OSError:
                bak = None
            print(
                f"  ERROR: Cannot safely update {config_path}. "
                f"Fix the file manually{f' or restore from {bak}' if bak else ''}.",
                file=sys.stderr,
            )
            return False

    # Build zotpilot MCP entry (use absolute path for reliability)
    try:
        zp = _zotpilot_command(allow_fallback=False)
    except RuntimeError as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        return False
    if is_opencode:
        entry: dict = {"type": "local", "command": [zp]}
        if env:
            entry["environment"] = env
        # OpenCode: set experimental.mcp_timeout for long-running tool calls.
        # The per-server "timeout" only controls tool discovery, not execution.
        existing.setdefault("experimental", {})
        existing["experimental"].setdefault("mcp_timeout", 600000)
    else:
        entry = {"type": "stdio", "command": zp, "args": []}
        if env:
            entry["env"] = env

    # Merge
    if mcp_key not in existing:
        existing[mcp_key] = {}
    existing[mcp_key]["zotpilot"] = entry

    # Atomic write: temp file + rename
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        # Backup existing file (skip if already backed up from parse failure)
        if config_path.exists() and not already_backed_up:
            shutil.copy2(config_path, config_path.with_suffix(config_path.suffix + ".bak"))

        fd, tmp_path = tempfile.mkstemp(
            dir=config_path.parent, suffix=".tmp", prefix="zotpilot_"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)
            f.write("\n")

        # Validate written JSON
        with open(tmp_path, encoding="utf-8") as f:
            json.load(f)

        os.replace(tmp_path, config_path)
        return True
    except (OSError, json.JSONDecodeError) as e:
        print(f"  ERROR writing {config_path}: {e}", file=sys.stderr)
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return False


def _register_opencode(env: dict[str, str]) -> bool:
    """OpenCode: config file write (interactive CLI not scriptable)."""
    config_path = _mcp_config_path("opencode")
    if not config_path:
        return False
    return _write_mcp_config(config_path, env)


def _register_ide(plat: str, env: dict[str, str]) -> bool:
    """Register for IDE platforms (Cursor, Windsurf, Cline, Roo)."""
    config_path = _mcp_config_path(plat)
    if not config_path:
        print(f"  ERROR: Unknown config path for {plat}", file=sys.stderr)
        return False
    return _write_mcp_config(config_path, env)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_REGISTER_FNS = {
    "claude-code": _register_claude_code,
    "codex": _register_codex,
    "gemini": _register_gemini,
    "opencode": _register_opencode,
    "cursor": lambda env: _register_ide("cursor", env),
    "windsurf": lambda env: _register_ide("windsurf", env),
    "cline": lambda env: _register_ide("cline", env),
    "roo": lambda env: _register_ide("roo", env),
}


def register(
    platforms: list[str] | None = None,
    gemini_key: str | None = None,
    dashscope_key: str | None = None,
    zotero_api_key: str | None = None,
    zotero_user_id: str | None = None,
) -> dict[str, bool]:
    """Compatibility wrapper over runtime reconciliation."""
    # Auto-fill from config file if not passed as CLI args
    if not all([gemini_key, dashscope_key, zotero_api_key, zotero_user_id]):
        try:
            from .config import Config
            cfg = Config.load()
            gemini_key = gemini_key or cfg.gemini_api_key
            dashscope_key = dashscope_key or cfg.dashscope_api_key
            zotero_api_key = zotero_api_key or cfg.zotero_api_key
            zotero_user_id = zotero_user_id or cfg.zotero_user_id
            if any([cfg.gemini_api_key, cfg.zotero_api_key]):
                print("Credentials loaded from config file.")
        except Exception:
            pass

    supported_targets = _supported_targets(platforms)
    if not supported_targets:
        if platforms:
            unsupported = [plat for plat in platforms if plat not in SUPPORTED_PLATFORM_NAMES]
            if unsupported:
                print(
                    "Requested platforms are unsupported in v0.5.0: "
                    + ", ".join(unsupported),
                    file=sys.stderr,
                )
        print("No supported AI agent platforms detected for v0.5.0.", file=sys.stderr)
        print(f"Supported: {', '.join(SUPPORTED_PLATFORM_NAMES)}", file=sys.stderr)
        return {}

    print(f"Reconciling runtime for: {', '.join(PLATFORMS[p]['label'] for p in supported_targets)}")
    result = reconcile_runtime(
        platforms=supported_targets,
        gemini_key=gemini_key,
        dashscope_key=dashscope_key,
        zotero_api_key=zotero_api_key,
        zotero_user_id=zotero_user_id,
        apply=True,
    )
    results = {
        plat: (
            plat not in result.changes.deploy_skill_platforms
            or plat in result.applied.deployed
        ) and (
            plat not in result.changes.register_platforms
            or plat in result.applied.registered
        )
        for plat in supported_targets
    }

    # Summary
    succeeded = [p for p, ok in results.items() if ok]
    if succeeded:
        print(f"\nRegistered on: {', '.join(PLATFORMS[p]['label'] for p in succeeded)}")
        print("Restart your AI agent(s) to activate ZotPilot MCP tools.")
    return results


def _print_manual_fallback(plat: str, env: dict[str, str]) -> None:
    """Print manual registration instructions when auto-registration fails."""
    zp = _zotpilot_command()
    if plat == "claude-code":
        env_flags = " ".join(f"-e {k}={v}" for k, v in env.items())
        print(f"    Manual: claude mcp add --scope user zotpilot {zp} {env_flags}")
    elif plat == "codex":
        env_flags = " ".join(f"--env {k}={v}" for k, v in env.items())
        print(f"    Manual: codex mcp add zotpilot {env_flags} -- {zp}")
    elif plat == "gemini":
        env_flags = " ".join(f"-e {k}={v}" for k, v in env.items())
        print(f"    Manual: gemini mcp add -s user {env_flags} zotpilot {zp}")
    else:
        config_path = _mcp_config_path(plat)
        is_opencode = "opencode" in str(config_path) if config_path else False
        if is_opencode:
            mcp_key = "mcp"
            entry: dict = {"type": "local", "command": [zp]}
            if env:
                entry["environment"] = env
            print(f"    Manual: Add to {config_path}:")
            print(f'    {{"{mcp_key}": {{"zotpilot": {json.dumps(entry)}}},')
            print('     "experimental": {"mcp_timeout": 600000}}')
        else:
            mcp_key = "mcpServers"
            entry = {"type": "stdio", "command": zp, "args": []}
            if env:
                entry["env"] = env
            print(f"    Manual: Add to {config_path}:")
            print(f'    {{"{mcp_key}": {{"zotpilot": {json.dumps(entry)}}}}}')


# ---------------------------------------------------------------------------
# Check registration status
# ---------------------------------------------------------------------------

def check_registered() -> dict:
    """Check which platforms have ZotPilot MCP registered.

    Returns {platform_name: {"registered": bool, "config_path": str|None}}.
    Best-effort: config paths may change across platform versions.
    """
    state = inspect_current_state()
    return {
        plat: {
            "registered": platform_state.registered,
            "config_path": platform_state.config_path,
            "supported": platform_state.supported,
            "command": platform_state.command,
        }
        for plat, platform_state in state.platforms.items()
    }

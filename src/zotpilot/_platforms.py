# TODO: This is a copy of scripts/platforms.py for wheel distribution.
# Deduplicate once install bootstrap is reworked. Keep in sync manually.
"""Cross-platform MCP registration for ZotPilot.

Currently supported clients:
  - Claude Code
  - Codex CLI
  - OpenCode

Registration writes only the ZotPilot MCP command (`zotpilot mcp serve`) into
client config. Credentials are resolved at runtime by ZotPilot from shared
config, secure storage, and explicit environment overrides.
"""
import dataclasses
import hashlib
import importlib.metadata
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

from .config import Config

logger = logging.getLogger(__name__)


def _mask_secret(secret: str) -> str:
    """Mask a secret key for safe display in logs and output."""
    if len(secret) <= 8:
        return "****"
    return secret[:2] + "****" + secret[-2:]

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - older runtime envs
    import tomli as tomllib

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



}

SUPPORTED_PLATFORM_NAMES: tuple[str, ...] = ("claude-code", "codex", "opencode")


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
    has_embedded_secrets: bool = False
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
    source_dir: Path | None = None


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
    1. Stable user-level bin path (`~/.local/bin/zotpilot` on Unix) when present
    2. shutil.which() — works when binary is already on PATH
    3. uv tool dir --bin — finds uv's bin directory even right after
       a fresh `uv tool install` in the same process (PATH not yet updated)
    4. Falls back to bare 'zotpilot' as last resort
    """
    # 1. Prefer stable user-level install path over transient uv archive paths.
    if not _is_windows():
        stable_user_bin = Path.home() / ".local" / "bin" / "zotpilot"
        if stable_user_bin.exists():
            return str(stable_user_bin)

    # 2. Try PATH lookup
    path = shutil.which("zotpilot")
    if path:
        return path

    # 3. Windows: pip --user installs to %APPDATA%\Python\PythonXYY\Scripts\
    if _is_windows():
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            py_ver = f"Python{sys.version_info.major}{sys.version_info.minor}"
            win_pip_dir = Path(appdata) / "Python" / py_ver / "Scripts"
            for name in ("zotpilot.exe", "zotpilot"):
                candidate = win_pip_dir / name
                if candidate.exists():
                    return str(candidate)

    # 4. Ask uv where it installs tool binaries
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

    # 5. Last resort
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
    paths = {
        "opencode": home / ".config" / "opencode" / "opencode.json",
    }
    return paths.get(plat)


def _codex_config_path() -> Path:
    return _home() / ".codex" / "config.toml"


def _claude_config_path() -> Path:
    return _home() / ".claude.json"


def _backup_config_file(path: Path | None) -> Path | None:
    if path is None:
        return None
    expanded = path.expanduser()
    if not expanded.exists():
        return None
    backup = expanded.with_suffix(expanded.suffix + ".bak")
    shutil.copy2(expanded, backup)
    return backup


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _detect_app_install(plat: str) -> bool:
    """Check whether an editor-based client is actually installed."""
    home = _home()
    if plat == "opencode":
        cfg_dir = home / ".config" / "opencode"
        pkg = cfg_dir / "package.json"
        if pkg.exists():
            try:
                import json
                data = json.loads(pkg.read_text(encoding="utf-8"))
                deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
                if any("opencode" in name for name in deps):
                    return True
            except (Exception):
                pass
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
    for name in ("opencode",):
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
    # OpenCode stores command as a list: [binary, arg1, arg2, ...]
    # Split it so inspection matches the (command, args) shape used everywhere else.
    if isinstance(command, list):
        args = tuple(str(a) for a in command[1:])
        command = command[0] if command else None
    else:
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


def _runtime_invocation(source_dir: Path | None = None) -> tuple[str, tuple[str, ...]]:
    if source_dir:
        return "uv", ("run", "--directory", str(source_dir), "zotpilot", "mcp", "serve")
    return _zotpilot_command(), ("mcp", "serve")


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


def _commands_equivalent(actual: str | None, desired: str) -> bool:
    if actual is None:
        return False
    if actual == desired:
        return True
    try:
        actual_path = Path(actual)
        desired_path = Path(desired)
        if actual_path.name == desired_path.name == "zotpilot" and actual_path.exists():
            return True
    except OSError:
        return False
    return False


def inspect_current_state(
    config_env: dict[str, str] | None = None,
    targets: list[str] | None = None,
) -> RuntimeState:
    from . import __version__

    target_names = tuple(_supported_targets(targets))
    detected = set(detect_platforms())
    desired_command, desired_args = _runtime_invocation()

    states: dict[str, PlatformRuntimeState] = {}
    for plat, info in PLATFORMS.items():
        registered, command, args, env, config_path = _inspect_registration(plat)
        skill_dirs, skill_hash_ok = _skill_state_for_platform(plat)
        supported = plat in SUPPORTED_PLATFORM_NAMES
        has_embedded_secrets = any(key in env for key in CREDENTIAL_ENV_KEYS)
        registration_hash_ok = (
            registered
            and command == desired_command
            and tuple(args) == desired_args
            and not has_embedded_secrets
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
            has_embedded_secrets=has_embedded_secrets,
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
        elif not _commands_equivalent(state.command, desired.command) or tuple(state.args) != desired.args:
            register.append(plat)
            platform_reasons.append("command-drift")
        elif state.has_embedded_secrets:
            register.append(plat)
            platform_reasons.append("embedded-secrets")
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
        if fn(dict(desired.env), desired.source_dir):
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
    dev_source_dir: Path | None = None,
) -> ReconcileResult:
    current = inspect_current_state({}, platforms)
    command, args = _runtime_invocation(dev_source_dir)
    desired = DesiredRuntime(
        command=command,
        args=args,
        env={},
        targets=current.supported_targets,
        source_dir=dev_source_dir,
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
        deployed_skill_dirs = (
            [
                d for d in base.iterdir()
                if d.is_dir() and (d / "SKILL.md").is_file() and d.name.startswith("ztp-")
            ]
            if base.exists()
            else []
        )
        if deployed_skill_dirs:
            print(f"  {info['label']}: {len(deployed_skill_dirs)} skills deployed under {base}")
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
    """Map source filename to deployed directory name (e.g. ztp-research.md → ztp-research)."""
    return skill_file.stem


def _skill_targets_for_platform(
    _plat: str,
    skills_dir: Path,
    skill_files: list[Path],
) -> list[tuple[Path, list[Path]]]:
    """Each skill file gets its own directory with SKILL.md inside.

    Deployed structure:
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

    # Single SKILL.md inside zotpilot/ → routing shell from v0.5.0-beta, now removed
    shutil.rmtree(legacy)
    print(f"    migrated: removed legacy routing shell directory {legacy}")


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
                platform_ok = platform_ok and reason in ("up-to-date", "target-newer-than-package")
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

def _register_claude_code(env: dict[str, str], source_dir: Path | None = None) -> bool:
    """Register via ``claude mcp add``.

    The ``-e/--env`` flag is declared as variadic in claude-code (``<env...>``),
    so any positional args placed *after* ``-e`` are greedily consumed as env
    values.  To avoid that, put ``<name>`` and ``<commandOrUrl>`` FIRST and the
    variadic ``-e`` flags LAST.
    """
    command, args = _runtime_invocation(source_dir)
    _backup_config_file(_claude_config_path())
    subprocess.run(["claude", "mcp", "remove", "zotpilot"],
                   capture_output=True, text=True)
    cmd = ["claude", "mcp", "add", "--scope", "user", "zotpilot", "--", command] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ERROR: {result.stderr.strip()}", file=sys.stderr)
        return False
    return True


def _register_codex(env: dict[str, str], source_dir: Path | None = None) -> bool:
    """Register via `codex mcp add`."""
    command, args = _runtime_invocation(source_dir)
    _backup_config_file(_codex_config_path())
    subprocess.run(["codex", "mcp", "remove", "zotpilot"],
                   capture_output=True, text=True)
    cmd = ["codex", "mcp", "add", "zotpilot"]
    cmd.extend(["--", command] + list(args))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ERROR: {result.stderr.strip()}", file=sys.stderr)
        return False
    return True

# ---------------------------------------------------------------------------
# Config-file-based registration (OpenCode)
# ---------------------------------------------------------------------------

def _write_mcp_config(config_path: Path, env: dict[str, str], source_dir: Path | None = None) -> bool:
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

    # Build zotpilot MCP entry
    command, args = _runtime_invocation(source_dir)
    if is_opencode:
        entry: dict = {"type": "local", "command": [command, *args]}
    else:
        entry = {"type": "stdio", "command": command, "args": list(args)}
    if is_opencode:
        # OpenCode: set experimental.mcp_timeout for long-running tool calls.
        # The per-server "timeout" only controls tool discovery, not execution.
        existing.setdefault("experimental", {})
        existing["experimental"].setdefault("mcp_timeout", 600000)

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
            _backup_config_file(config_path)

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

        # Restrict permissions on Unix (owner read/write only)
        if sys.platform != "win32":
            os.chmod(config_path, 0o600)
        return True
    except (OSError, json.JSONDecodeError) as e:
        print(f"  ERROR writing {config_path}: {e}", file=sys.stderr)
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return False


def _register_opencode(env: dict[str, str], source_dir: Path | None = None) -> bool:
    """OpenCode: config file write (interactive CLI not scriptable)."""
    config_path = _mcp_config_path("opencode")
    if not config_path:
        return False
    return _write_mcp_config(config_path, env, source_dir=source_dir)

_REGISTER_FNS = {
    "claude-code": _register_claude_code,
    "codex": _register_codex,
    "opencode": _register_opencode,
}


def register(
    platforms: list[str] | None = None,
    gemini_key: str | None = None,
    dashscope_key: str | None = None,
    zotero_api_key: str | None = None,
    zotero_user_id: str | None = None,
    dev_source_dir: Path | None = None,
) -> dict[str, bool]:
    """Compatibility wrapper over runtime reconciliation."""
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
        print(f"Supported: {', '.join(SUPPORTED_PLATFORM_NAMES)}")
        return {}

    print(f"Reconciling runtime for: {', '.join(PLATFORMS[p]['label'] for p in supported_targets)}")
    result = reconcile_runtime(
        platforms=supported_targets,
        apply=True,
        dev_source_dir=dev_source_dir,
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
    command, args = _runtime_invocation()
    if plat == "claude-code":
        print(f"    Manual: claude mcp add --scope user zotpilot -- {command} {' '.join(args)}")
    elif plat == "codex":
        print(f"    Manual: codex mcp add zotpilot -- {command} {' '.join(args)}")
    else:
        config_path = _mcp_config_path(plat)
        is_opencode = "opencode" in str(config_path) if config_path else False
        if is_opencode:
            mcp_key = "mcp"
            entry: dict = {"type": "local", "command": [command, *args]}
            print(f"    Manual: Add to {config_path}:")
            print(f'    {{"{mcp_key}": {{"zotpilot": {json.dumps(entry)}}},')
            print('     "experimental": {"mcp_timeout": 600000}}')
        else:
            mcp_key = "mcpServers"
            entry = {"type": "stdio", "command": command, "args": list(args)}
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



@dataclasses.dataclass
class SkillDir:
    path: Path
    is_symlink: bool
    is_broken_symlink: bool
    is_duplicate: bool

def _uv_bin_dir(uv_cmd: list[str]) -> "Path | None":
    """Run `uv tool dir --bin` and return the path, or None on failure."""
    try:
        result = subprocess.run(
            uv_cmd + ["tool", "dir", "--bin"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip())
        return None
    except Exception:
        return None

def _detect_cli_installer() -> "tuple[str, list[str] | None]":
    """Detect how zotpilot CLI was installed.

    Returns (installer, uv_cmd) where installer is one of:
    'editable', 'uv', 'pip', 'unknown'
    """
    # Step 1: Check for editable install via direct_url.json
    try:
        dist = importlib.metadata.distribution("zotpilot")
        direct_url_text = dist.read_text("direct_url.json")
        if direct_url_text:
            data = json.loads(direct_url_text)
            dir_info = data.get("dir_info", {})
            if dir_info.get("editable"):
                return ("editable", None)
    except importlib.metadata.PackageNotFoundError:
        return ("unknown", None)
    except json.JSONDecodeError:
        return ("unknown", None)  # malformed direct_url.json — conservative fallback
    except (KeyError, TypeError):
        pass  # malformed direct_url.json structure

    # Step 2: uv detection via shutil.which("uv")
    argv0 = Path(sys.argv[0]).resolve()
    uv_path = shutil.which("uv")
    if uv_path:
        bin_dir = _uv_bin_dir(["uv"])
        if bin_dir and argv0.is_relative_to(bin_dir):
            return ("uv", ["uv"])
        # uv binary found but bin_dir lookup failed — try python -m uv as fallback
        bin_dir = _uv_bin_dir([sys.executable, "-m", "uv"])
        if bin_dir and argv0.is_relative_to(bin_dir):
            return ("uv", [sys.executable, "-m", "uv"])
    else:
        # Try via sys.executable -m uv
        bin_dir = _uv_bin_dir([sys.executable, "-m", "uv"])
        if bin_dir and argv0.is_relative_to(bin_dir):
            return ("uv", [sys.executable, "-m", "uv"])

    # Step 4 / default: metadata found but not editable and not uv → pip
    return ("pip", None)

def _get_current_version() -> str:
    """Return the currently installed zotpilot version."""
    try:
        return importlib.metadata.version("zotpilot")
    except Exception:
        try:
            from . import __version__
            return __version__
        except Exception:
            return "unknown"

def _get_latest_pypi_version() -> "str | None":
    """Fetch the latest zotpilot version from PyPI. Returns None on any error."""
    try:
        with urllib.request.urlopen("https://pypi.org/pypi/zotpilot/json", timeout=5) as resp:
            data = json.loads(resp.read())
            return data["info"]["version"]
    except Exception:
        return None

def _get_skill_dirs() -> list[SkillDir]:
    """Collect and deduplicate deployed ztp-* skill directories across all platforms."""

    candidates: list[Path] = []
    for info in PLATFORMS.values():
        skills_dir = info.get("skills_dir")
        if not skills_dir:
            continue
        base = Path(skills_dir).expanduser()
        if not base.exists():
            continue
        for path in base.iterdir():
            if path.name.startswith("ztp-") and (path.is_dir() or path.is_symlink()):
                candidates.append(path)

    # Dedup by realpath: group entries by resolved path
    # Broken symlinks use path itself as key
    seen: dict[Path, list[Path]] = {}
    for p in candidates:
        if p.is_symlink() and not p.exists():
            key = p  # broken symlink — unique key
        else:
            key = p.resolve()
        seen.setdefault(key, []).append(p)

    result: list[SkillDir] = []
    for key, paths in seen.items():
        # Sort: non-symlinks first, then symlinks
        paths_sorted = sorted(paths, key=lambda x: (x.is_symlink(), str(x)))
        for i, p in enumerate(paths_sorted):
            is_sym = p.is_symlink()
            is_broken = is_sym and not p.exists()
            if is_broken:
                # broken symlinks are never canonical but not marked as duplicate
                result.append(SkillDir(path=p, is_symlink=True, is_broken_symlink=True, is_duplicate=False))
            else:
                result.append(SkillDir(
                    path=p,
                    is_symlink=is_sym,
                    is_broken_symlink=False,
                    is_duplicate=(i > 0),
                ))

    return result

def _deployment_status(config: Config | None = None) -> dict:
    """Return platform detection, registration, skill dirs, and drift."""
    try:
        reconcile = reconcile_runtime(
            apply=False,
        )
        detected = [plat for plat, state in reconcile.current.platforms.items() if state.detected]
        registered = [plat for plat, state in reconcile.current.platforms.items() if state.registered]
        unsupported = [
            plat for plat, state in reconcile.current.platforms.items()
            if state.detected and not state.supported
        ]
        skill_dirs = [
            {
                "path": str(skill_dir.path),
                "is_symlink": skill_dir.is_symlink,
                "is_broken_symlink": skill_dir.is_broken_symlink,
                "is_duplicate": skill_dir.is_duplicate,
            }
            for skill_dir in _get_skill_dirs()
        ]
        embedded_secret_platforms = [
            plat for plat, state in reconcile.current.platforms.items() if state.has_embedded_secrets
        ]
        return {
            "detected_platforms": detected,
            "registered_platforms": registered,
            "unsupported_platforms": unsupported,
            "registration": {
                plat: {
                    "registered": state.registered,
                    "config_path": state.config_path,
                    "supported": state.supported,
                    "command": state.command,
                }
                for plat, state in reconcile.current.platforms.items()
            },
            "skill_dirs": skill_dirs,
            "drift_state": reconcile.changes.drift_state,
            "restart_required": reconcile.changes.drift_state != "clean",
            "legacy_embedded_secrets_detected": bool(embedded_secret_platforms),
            "legacy_embedded_secret_platforms": embedded_secret_platforms,
        }
    except Exception as exc:
        logger.debug("deployment status inspection failed", exc_info=True)
        return {
            "detected_platforms": [],
            "registered_platforms": [],
            "unsupported_platforms": [],
            "registration": {},
            "skill_dirs": [],
            "drift_state": "unknown",
            "restart_required": False,
            "deployment_warning": str(exc),
            "legacy_embedded_secrets_detected": False,
            "legacy_embedded_secret_platforms": [],
        }


CREDENTIAL_ENV_KEYS: tuple[str, ...] = (
    "GEMINI_API_KEY",
    "DASHSCOPE_API_KEY",
    "ZOTERO_API_KEY",
    "ZOTERO_USER_ID",
)


def compute_divergent_registration(state: RuntimeState) -> dict:
    """Compute divergent registration across registered supported platforms.

    Pure read-only comparison — does NOT mutate config or registrations.

    Algorithm:
    1. Sort registered supported platforms lexicographically
    2. First entry = canonical baseline
    3. Compare each platform's credential_env to baseline
    4. If any key differs by presence or value → divergent_registration = true

    Returns:
        {
            "divergent_registration": bool,
            "divergent_registration_platforms": list[str],
            "divergent_registration_fields": list[str],
        }
    """
    embedded = [plat for plat, ps in state.platforms.items() if ps.registered and ps.has_embedded_secrets]
    return {
        "divergent_registration": bool(embedded),
        "divergent_registration_platforms": embedded,
        "divergent_registration_fields": list(CREDENTIAL_ENV_KEYS) if embedded else [],
    }

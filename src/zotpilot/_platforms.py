# TODO: This is a copy of scripts/platforms.py for wheel distribution.
# Deduplicate once install bootstrap is reworked. Keep in sync manually.
"""Cross-platform MCP server registration for ZotPilot.

Supports:
  Tier 1 (Skill + MCP): Claude Code, Codex CLI, OpenCode, Gemini CLI
  Tier 2 (MCP only):    Cursor, Windsurf, Cline, Roo Code

Security note: CLI-based platforms (claude, codex, gemini) require passing
env vars via -e flags on the command line. This briefly exposes keys in the
process table. This is the only mechanism these CLIs provide. For higher
security, register manually and use environment variables or secret managers.
"""
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

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
        "tier": 2,
        "binary": None,
        "label": "Cursor",
    },
    "windsurf": {
        "tier": 2,
        "binary": None,
        "label": "Windsurf",
    },
    "cline": {
        "tier": 2,
        "binary": None,
        "label": "Cline",
    },
    "roo": {
        "tier": 2,
        "binary": None,
        "label": "Roo Code",
    },
}


def _is_windows() -> bool:
    return platform.system() == "Windows"


def _home() -> Path:
    return Path.home()


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


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def detect_platforms() -> list[str]:
    """Return list of platform names detected on this machine."""
    found: set[str] = set()
    for name, info in PLATFORMS.items():
        binary = info.get("binary")
        if binary and shutil.which(binary):
            found.add(name)
    # Tier 2 + opencode (may be installed as IDE without CLI binary)
    for name in ("cursor", "windsurf", "cline", "roo", "opencode"):
        if name in found:
            continue
        path = _mcp_config_path(name)
        if path and (path.exists() or path.parent.exists()):
            found.add(name)
    # Return in stable order matching PLATFORMS dict
    return [p for p in PLATFORMS if p in found]


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

    Skill installation (clone/copy/symlink) is the agent's responsibility,
    not this script's. This function just tells the agent where to put it.
    """
    tier1 = [p for p in platforms if PLATFORMS.get(p, {}).get("tier") == 1]
    if not tier1:
        return
    for plat in tier1:
        info = PLATFORMS[plat]
        skills_dir = info.get("skills_dir", "")
        target = Path(skills_dir).expanduser() / "zotpilot"
        skill_md = target / "SKILL.md"
        if skill_md.is_file():
            print(f"  {info['label']}: skill found at {target}")
        elif target.is_symlink():
            print(f"  {info['label']}: BROKEN symlink at {target} — re-clone repo")
        elif target.exists():
            print(f"  {info['label']}: {target} exists but missing SKILL.md — re-clone repo")
        else:
            print(f"  {info['label']}: skill NOT found — clone repo to {target}")


# ---------------------------------------------------------------------------
# CLI-based registration (Tier 1)
# ---------------------------------------------------------------------------

def _register_claude_code(env: dict[str, str]) -> bool:
    """Register via `claude mcp add`."""
    try:
        zp = _zotpilot_command(allow_fallback=False)
    except RuntimeError as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        return False
    subprocess.run(["claude", "mcp", "remove", "zotpilot"],
                   capture_output=True, text=True)
    cmd = ["claude", "mcp", "add", "-s", "user"]
    for k, v in env.items():
        cmd.extend(["-e", f"{k}={v}"])
    cmd.extend(["zotpilot", "--", zp])
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
    """Register ZotPilot MCP server on specified (or auto-detected) platforms.

    Returns dict of {platform: success}.
    """
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

    env = _build_env(gemini_key, dashscope_key, zotero_api_key, zotero_user_id)

    if not platforms:
        platforms = detect_platforms()
        if not platforms:
            print("No supported AI agent platforms detected.", file=sys.stderr)
            print("Specify one with: --platform <name>", file=sys.stderr)
            print(f"Supported: {', '.join(PLATFORMS.keys())}", file=sys.stderr)
            return {}
        print(f"Detected platforms: {', '.join(PLATFORMS[p]['label'] for p in platforms)}")

    # Check skill installation status for Tier 1 platforms
    print_skill_hints(platforms)

    # Register MCP server
    results = {}
    for plat in platforms:
        if plat not in _REGISTER_FNS:
            print(f"  Unknown platform: {plat}", file=sys.stderr)
            results[plat] = False
            continue

        label = PLATFORMS[plat]["label"]
        print(f"  Registering on {label}...", end=" ")
        ok = _REGISTER_FNS[plat](env)
        results[plat] = ok
        if ok:
            print("OK")
            config_path = _mcp_config_path(plat)
            if config_path:
                print(f"    Config: {config_path}")
        else:
            print("FAILED")
            _print_manual_fallback(plat, env)

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
        print(f"    Manual: claude mcp add -s user {env_flags} zotpilot -- {zp}")
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
    result = {}
    for plat in PLATFORMS:
        info = {"registered": False, "config_path": None}
        binary = PLATFORMS[plat].get("binary")

        # CLI-based check (some CLIs output to stderr)
        if plat in ("claude-code", "codex", "gemini") and binary and shutil.which(binary):
            r = subprocess.run(
                [binary, "mcp", "list"], capture_output=True, text=True
            )
            info["registered"] = "zotpilot" in (r.stdout + r.stderr)
        else:
            # Config-file check
            config_path = _mcp_config_path(plat)
            if config_path:
                expanded = config_path.expanduser()
                info["config_path"] = str(expanded)
                if expanded.exists():
                    try:
                        text = expanded.read_text(encoding="utf-8")
                        if expanded.suffix == ".jsonc":
                            text = _strip_jsonc_comments(text)
                        data = json.loads(text) if text.strip() else {}
                        mcp_key = "mcp" if "opencode" in str(expanded) else "mcpServers"
                        info["registered"] = "zotpilot" in data.get(mcp_key, {})
                    except (json.JSONDecodeError, OSError):
                        pass
        result[plat] = info
    return result

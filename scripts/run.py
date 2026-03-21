#!/usr/bin/env python3
"""ZotPilot skill bootstrap — ensures zotpilot CLI is installed, then delegates.

Usage by AI agent (via SKILL.md):
    python scripts/run.py status --json
    python scripts/run.py setup --non-interactive --provider local
    python scripts/run.py index --limit 10
    python scripts/run.py register [--platform <name>] [--gemini-key <k>] ...

Windows note: use 'python' instead of 'python3' if python3 is not in PATH.
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent  # repo root


def _uv_args(uv: str) -> list[str]:
    """Return uv invocation as a list (handles 'python -m uv' form)."""
    if uv.startswith(sys.executable):
        return [sys.executable, "-m", "uv"]
    return [uv]


def _ensure_uv() -> str:
    """Return path/invocation for uv, or exit with helpful message."""
    uv = shutil.which("uv")
    if uv:
        return uv
    # Fallback: uv installed via pip but not in PATH (common on Windows)
    try:
        subprocess.run(
            [sys.executable, "-m", "uv", "--version"],
            capture_output=True, check=True,
        )
        return f"{sys.executable} -m uv"  # sentinel for _uv_args()
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    print(
        "ERROR: uv is not installed.\n"
        "Install it:\n"
        "  Linux/macOS: curl -LsSf https://astral.sh/uv/install.sh | sh\n"
        "  Windows:     powershell -ExecutionPolicy ByPass -c "
        '"irm https://astral.sh/uv/install.ps1 | iex"\n'
        "  Any platform: pip install uv",
        file=sys.stderr,
    )
    sys.exit(1)


def _find_zotpilot_after_pip() -> list[str] | None:
    """Try to locate the zotpilot binary after a pip install.

    Returns a ready-to-use command list, or None if not found.
    """
    # PATH may have been updated by the install
    zp = shutil.which("zotpilot")
    if zp:
        return [zp]
    # Linux/macOS pip --user installs to ~/.local/bin/
    user_bin = Path.home() / ".local" / "bin" / "zotpilot"
    if user_bin.exists():
        return [str(user_bin)]
    # Windows pip --user installs to %APPDATA%\Python\PythonXYY\Scripts\
    import os as _os, platform as _plt
    if _plt.system() == "Windows":
        appdata = _os.environ.get("APPDATA", "")
        if appdata:
            py_ver = f"Python{sys.version_info.major}{sys.version_info.minor}"
            win_scripts = Path(appdata) / "Python" / py_ver / "Scripts"
            for name in ("zotpilot.exe", "zotpilot"):
                candidate = win_scripts / name
                if candidate.exists():
                    return [str(candidate)]
    return None


def _is_uv_tool_installed(uv: str) -> bool:
    """Check if zotpilot binary exists in uv's tool bin directory."""
    try:
        r = subprocess.run(
            _uv_args(uv) + ["tool", "dir", "--bin"],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and r.stdout.strip():
            bin_dir = Path(r.stdout.strip())
            return (bin_dir / "zotpilot").exists() or (bin_dir / "zotpilot.exe").exists()
    except (FileNotFoundError, OSError):
        pass
    return False


def _get_source_version() -> str | None:
    """Read version from pyproject.toml in SKILL_DIR."""
    toml = SKILL_DIR / "pyproject.toml"
    if not toml.exists():
        return None
    try:
        for line in toml.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("version"):
                # version = "0.1.2"
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def _get_installed_version(uv: str) -> str | None:
    """Get version of zotpilot installed as a uv tool.

    Parses `uv tool list` output which looks like:
        zotpilot v0.1.2
        - zotpilot
    """
    try:
        r = subprocess.run(
            _uv_args(uv) + ["tool", "list"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if line.startswith("zotpilot "):
                    # "zotpilot v0.1.2" → "0.1.2"
                    ver_part = line.split()[-1]
                    return ver_part.lstrip("v")
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        pass
    return None


def _needs_upgrade(uv: str) -> bool:
    """Check if installed zotpilot is outdated vs source pyproject.toml."""
    source_ver = _get_source_version()
    if not source_ver:
        return False
    installed_ver = _get_installed_version(uv)
    if not installed_ver:
        return False  # can't determine (pip install or no uv) → skip upgrade
    return installed_ver != source_ver


def _ensure_zotpilot(uv: str) -> list[str] | None:
    """Install zotpilot CLI if needed.

    Returns an override command list when pip-installed (binary outside uv),
    or None when uv tool run should be used (the normal case).

    Resolution order:
    1. shutil.which() — binary on PATH
    2. _find_zotpilot_after_pip() — pip-installed binary (Windows %APPDATA%, ~/.local/bin)
    3. _is_uv_tool_installed() — uv's tool bin dir (not on PATH but uv tool run works)
    4. Fresh install via uv tool install
    5. pip fallback if uv fails
    After finding an existing install, checks version and upgrades if needed.
    """
    # 1. Check PATH
    if shutil.which("zotpilot"):
        return None

    # 2. Check pip-installed locations (binary outside uv)
    pip_cmd = _find_zotpilot_after_pip()
    if pip_cmd:
        return pip_cmd

    # 3. Check uv tool bin dir (installed but not on PATH — common on Windows)
    if _is_uv_tool_installed(uv):
        return None  # uv tool run still works

    # 4. Not installed anywhere — install now
    print("ZotPilot CLI not found. Installing...", file=sys.stderr)
    uv_cmd = _uv_args(uv)
    result = subprocess.run(
        uv_cmd + ["tool", "install", str(SKILL_DIR)],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print("ZotPilot CLI installed successfully.", file=sys.stderr)
        return None  # uv tool run works

    # 5. uv tool install failed — pip fallback
    print(
        f"uv tool install failed:\n{result.stderr}\n"
        "Falling back to pip install...",
        file=sys.stderr,
    )
    pip_result = subprocess.run(
        [sys.executable, "-m", "pip", "install", str(SKILL_DIR)],
        capture_output=True,
        text=True,
    )
    if pip_result.returncode != 0:
        print(f"pip install also failed:\n{pip_result.stderr}", file=sys.stderr)
        sys.exit(1)
    print("ZotPilot CLI installed via pip.", file=sys.stderr)
    cmd = _find_zotpilot_after_pip()
    if cmd is None:
        print(
            "WARNING: zotpilot binary not found after pip install.\n"
            "You may need to add the scripts directory to your PATH.",
            file=sys.stderr,
        )
        cmd = ["zotpilot"]  # last resort
    return cmd


def _handle_register(argv: list[str]) -> int:
    """Handle the 'register' subcommand for cross-platform MCP registration."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "platforms", Path(__file__).resolve().parent / "platforms.py"
    )
    if spec is None or spec.loader is None:
        print("ERROR: platforms.py not found in scripts/", file=sys.stderr)
        return 1
    platforms_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(platforms_mod)
    register = platforms_mod.register
    PLATFORMS = platforms_mod.PLATFORMS

    parser = argparse.ArgumentParser(
        prog="run.py register",
        description="Register ZotPilot MCP server on AI agent platforms.",
    )
    parser.add_argument(
        "--platform", action="append", dest="platforms",
        choices=list(PLATFORMS.keys()),
        help="Platform to register on (repeatable). Auto-detects if omitted.",
    )
    parser.add_argument("--gemini-key", help="Gemini API key for embeddings")
    parser.add_argument("--dashscope-key", help="DashScope API key for embeddings")
    parser.add_argument("--zotero-api-key", help="Zotero Web API key (for write ops)")
    parser.add_argument("--zotero-user-id", help="Zotero numeric user ID (for write ops)")
    args = parser.parse_args(argv)

    results = register(
        platforms=args.platforms,
        gemini_key=args.gemini_key,
        dashscope_key=args.dashscope_key,
        zotero_api_key=args.zotero_api_key,
        zotero_user_id=args.zotero_user_id,
    )
    return 0 if results and all(results.values()) else 1


def main():
    args = sys.argv[1:]

    # Intercept 'register' before uv check — register only edits JSON config files,
    # it does not need uv or the zotpilot CLI to be installed.
    if args and args[0] == "register":
        sys.exit(_handle_register(args[1:]))

    uv = _ensure_uv()
    pip_cmd = _ensure_zotpilot(uv)

    # Check if installed version is outdated vs source pyproject.toml
    if _needs_upgrade(uv):
        print("ZotPilot CLI outdated. Upgrading...", file=sys.stderr)
        uv_cmd = _uv_args(uv)
        r = subprocess.run(
            uv_cmd + ["tool", "install", "--reinstall", str(SKILL_DIR)],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            print("ZotPilot CLI upgraded successfully.", file=sys.stderr)
            pip_cmd = None  # use uv tool run after upgrade
        else:
            print(f"Upgrade failed (non-fatal): {r.stderr}", file=sys.stderr)

    # All other subcommands delegate to zotpilot CLI.
    # pip_cmd is set only when installed via pip fallback (binary outside uv).
    if pip_cmd is not None:
        sys.exit(subprocess.run(pip_cmd + args).returncode)
    sys.exit(subprocess.run(_uv_args(uv) + ["tool", "run", "zotpilot"] + args).returncode)


if __name__ == "__main__":
    main()

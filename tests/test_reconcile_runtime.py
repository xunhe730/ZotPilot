from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

from zotpilot._platforms import (
    ApplyResult,
    ChangeSet,
    DesiredRuntime,
    PlatformRuntimeState,
    RuntimeState,
    plan_runtime_changes,
    register,
)
from zotpilot.cli import cmd_sync, cmd_update


def test_plan_runtime_changes_is_noop_when_runtime_matches_desired():
    current = RuntimeState(
        package_version="0.5.0",
        supported_targets=("codex",),
        platforms={
            "codex": PlatformRuntimeState(
                platform="codex",
                label="Codex CLI",
                supported=True,
                detected=True,
                registered=True,
                command="/usr/bin/zotpilot",
                args=(),
                env={"GEMINI_API_KEY": "x"},
                skill_dirs=("/tmp/skills/zotpilot",),
                skill_hash_ok=True,
                registration_hash_ok=True,
            ),
        },
    )
    desired = DesiredRuntime(
        command="/usr/bin/zotpilot",
        args=(),
        env={"GEMINI_API_KEY": "x"},
        targets=("codex",),
    )

    changes = plan_runtime_changes(desired, current)
    assert changes.deploy_skill_platforms == ()
    assert changes.register_platforms == ()
    assert changes.drift_state == "clean"


def test_plan_runtime_changes_detects_skill_and_registration_drift():
    current = RuntimeState(
        package_version="0.5.0",
        supported_targets=("codex", "claude-code"),
        platforms={
            "codex": PlatformRuntimeState(
                platform="codex",
                label="Codex CLI",
                supported=True,
                detected=True,
                registered=False,
                skill_hash_ok=False,
            ),
            "claude-code": PlatformRuntimeState(
                platform="claude-code",
                label="Claude Code",
                supported=True,
                detected=True,
                registered=True,
                command="/old/zotpilot",
                args=(),
                env={},
                skill_hash_ok=True,
            ),
        },
    )
    desired = DesiredRuntime(
        command="/new/zotpilot",
        args=(),
        env={"GEMINI_API_KEY": "x"},
        targets=("codex", "claude-code"),
    )

    changes = plan_runtime_changes(desired, current)
    assert changes.deploy_skill_platforms == ("codex",)
    assert changes.register_platforms == ("codex", "claude-code")
    assert changes.drift_state == "needs-sync"


def test_register_delegates_to_reconcile_runtime():
    fake_result = MagicMock()
    fake_result.changes = ChangeSet(("codex",), ("codex",), "needs-sync", {"codex": ["env-drift"]})
    fake_result.applied = ApplyResult(("codex",), ("codex",), True)
    fake_result.current = RuntimeState(
        package_version="0.5.0",
        supported_targets=("codex",),
        platforms={},
    )
    with patch("zotpilot._platforms.reconcile_runtime", return_value=fake_result) as mock_reconcile:
        result = register(platforms=["codex"])
    assert result == {"codex": True}
    assert mock_reconcile.call_args.kwargs["apply"] is True


def test_cmd_update_uses_reconcile_runtime_for_runtime_sync(capsys):
    args = argparse.Namespace(cli_only=False, skill_only=False, check=False, dry_run=False)
    fake_result = MagicMock()
    fake_result.applied = ApplyResult(("codex",), ("codex",), True)
    fake_result.current.supported_targets = ("codex",)
    with (
        patch("zotpilot.cli._get_current_version", return_value="0.5.0"),
        patch("zotpilot.cli._get_latest_pypi_version", return_value="0.5.0"),
        patch("zotpilot.cli._detect_cli_installer", return_value=("editable", None)),
        patch("zotpilot.cli._import_runtime_env_to_config", return_value={}),
        patch("zotpilot._platforms.reconcile_runtime", return_value=fake_result) as mock_reconcile,
    ):
        assert cmd_update(args) == 0
    assert mock_reconcile.call_args.kwargs["apply"] is True


def test_cmd_sync_runs_reconcile_runtime(capsys):
    args = argparse.Namespace(dry_run=False)
    fake_result = MagicMock()
    fake_result.applied = ApplyResult(("codex",), ("codex",), True)
    with (
        patch("zotpilot.cli._import_runtime_env_to_config", return_value={}),
        patch("zotpilot._platforms.reconcile_runtime", return_value=fake_result) as mock_reconcile,
    ):
        assert cmd_sync(args) == 0
    assert mock_reconcile.call_args.kwargs["apply"] is True


def test_run_py_register_bootstraps_then_delegates(tmp_path):
    run_path = Path(__file__).resolve().parents[1] / "scripts" / "run.py"
    spec = importlib.util.spec_from_file_location("zotpilot_run_script", run_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with (
        patch.object(module, "_ensure_uv", return_value="uv"),
        patch.object(module, "_ensure_zotpilot", return_value=None),
        patch.object(module, "_uv_args", return_value=["uv"]),
        patch.object(module.subprocess, "run", return_value=MagicMock(returncode=0)) as mock_run,
    ):
        assert module._handle_register(["--platform", "codex"]) == 0

    assert mock_run.call_args.args[0] == [
        "uv",
        "tool",
        "run",
        "zotpilot",
        "register",
        "--platform",
        "codex",
    ]

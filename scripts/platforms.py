#!/usr/bin/env python3
"""Thin wrapper over src/zotpilot/_platforms.py for source checkouts.

This file exists only so bootstrap-time code can access the same platform
implementation without maintaining a second copy.

It assumes the standard source checkout layout where `scripts/` is a sibling
of `src/`. Moving this file elsewhere without the repo structure will fail.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SOURCE = Path(__file__).resolve().parents[1] / "src" / "zotpilot" / "_platforms.py"
_SPEC = importlib.util.spec_from_file_location("zotpilot_source_platforms", _SOURCE)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Cannot load platform implementation from {_SOURCE}")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


PLATFORMS = _MODULE.PLATFORMS
SUPPORTED_PLATFORM_NAMES = _MODULE.SUPPORTED_PLATFORM_NAMES
detect_platforms = _MODULE.detect_platforms
deploy_skills = _MODULE.deploy_skills
inspect_current_state = _MODULE.inspect_current_state
plan_runtime_changes = _MODULE.plan_runtime_changes
apply_runtime_changes = _MODULE.apply_runtime_changes
reconcile_runtime = _MODULE.reconcile_runtime
register = _MODULE.register
check_registered = _MODULE.check_registered

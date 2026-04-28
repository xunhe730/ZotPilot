"""Tests for MCP tool profile exposure."""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LIST_TOOLS_SCRIPT = """
import asyncio
import json
from zotpilot.state import mcp
from zotpilot import tools  # noqa: F401

async def main():
    tools_list = await mcp.list_tools(run_middleware=False)
    print(json.dumps(sorted(tool.name for tool in tools_list)))

asyncio.run(main())
"""


def _list_tools(profile: str, disabled: str | None = None) -> list[str]:
    env = os.environ.copy()
    env["ZOTPILOT_TOOL_PROFILE"] = profile
    if disabled is not None:
        env["ZOTPILOT_DISABLE_TOOLS"] = disabled
    else:
        env.pop("ZOTPILOT_DISABLE_TOOLS", None)

    proc = subprocess.run(
        [sys.executable, "-c", LIST_TOOLS_SCRIPT],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(proc.stdout)


def test_core_profile_exposes_baseline_tools():
    tools = _list_tools("core")
    assert len(tools) == 17
    assert "profile_library" not in tools
    assert "advanced_search" in tools
    assert "search_papers" in tools
    assert "manage_collections" in tools

def test_full_profile_includes_profile_library():
    tools = _list_tools("full")
    assert len(tools) == 18
    assert "profile_library" in tools

def test_disabled_tools_are_removed_after_profile_filtering():
    tools = _list_tools("full", disabled="search_topic;manage_tags")
    assert "search_topic" not in tools
    assert "manage_tags" not in tools
    assert "search_papers" in tools


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


def test_core_profile_exposes_only_workflow_tools():
    assert _list_tools("core") == [
        "advanced_search",
        "get_index_stats",
        "get_ingest_status",
        "get_paper_details",
        "get_passage_context",
        "ingest_papers",
        "research_session",
        "search_academic_databases",
        "search_papers",
        "search_topic",
    ]


def test_extended_profile_includes_admin_and_extended_tools():
    tools = _list_tools("extended")
    assert "browse_library" in tools
    assert "manage_tags" in tools
    assert "index_library" in tools
    assert "get_index_stats" in tools
    # Deprecated aliases removed in v0.5.0
    assert "save_from_url" not in tools
    assert "list_tags" not in tools
    assert "add_item_tags" not in tools
    assert "find_citing_papers" not in tools
    assert "get_reranking_config" not in tools
    assert "get_unindexed_papers" not in tools
    assert "get_vision_costs" not in tools


def test_all_profile_no_longer_includes_removed_deprecated_aliases():
    tools = _list_tools("all")
    # Deprecated aliases removed in v0.5.0
    assert "list_tags" not in tools
    assert "add_item_tags" not in tools
    assert "find_citing_papers" not in tools
    assert "save_from_url" not in tools


def test_disabled_tools_are_removed_after_profile_filtering():
    tools = _list_tools("extended", disabled="search_topic;manage_tags")
    assert "search_topic" not in tools
    assert "manage_tags" not in tools
    assert "search_papers" in tools


def test_research_profile_exposes_research_tool_surface():
    tools = _list_tools("research")
    assert "search_papers" in tools
    assert "browse_library" in tools
    assert "index_library" in tools
    assert "switch_library" in tools
    assert "get_index_stats" in tools

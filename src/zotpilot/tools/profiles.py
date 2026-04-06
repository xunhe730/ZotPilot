"""Tool profile configuration for ZotPilot MCP exposure."""

from __future__ import annotations

import os

from ..state import mcp

DEFAULT_TOOL_PROFILE = "extended"
VALID_TOOL_PROFILES = {"core", "extended", "all", "research"}

PROFILE_VISIBLE_TAGS: dict[str, set[str] | None] = {
    "core": {"core"},
    "extended": {"core", "extended", "admin"},
    "research": {"core", "extended", "admin", "research"},
    "all": None,
}


def tool_tags(*tags: str) -> set[str]:
    """Return a normalized tag set for FastMCP tool registration."""
    return {tag for tag in tags if tag}


def get_tool_profile_name(raw: str | None = None) -> str:
    """Resolve the configured tool profile and validate it."""
    profile = (raw if raw is not None else os.getenv("ZOTPILOT_TOOL_PROFILE", DEFAULT_TOOL_PROFILE)).strip().lower()
    if not profile:
        profile = DEFAULT_TOOL_PROFILE
    if profile not in VALID_TOOL_PROFILES:
        valid = ", ".join(sorted(VALID_TOOL_PROFILES))
        raise ValueError(f"Invalid ZOTPILOT_TOOL_PROFILE '{profile}'. Expected one of: {valid}")
    return profile


def parse_disabled_tools(raw: str | None = None) -> set[str]:
    """Parse optional comma/semicolon-delimited disabled tool list."""
    if raw is None:
        raw = os.getenv("ZOTPILOT_DISABLE_TOOLS")
    if not raw:
        return set()
    disabled: set[str] = set()
    for token in raw.replace(";", ",").split(","):
        name = token.strip()
        if name:
            disabled.add(name)
    return disabled


def apply_tool_profile() -> str:
    """Apply runtime tool visibility based on the configured profile."""
    profile = get_tool_profile_name()
    visible_tags = PROFILE_VISIBLE_TAGS[profile]
    if visible_tags is not None:
        mcp.enable(tags=visible_tags, components={"tool"}, only=True)

    disabled_tools = parse_disabled_tools()
    if disabled_tools:
        mcp.disable(names=disabled_tools, components={"tool"})

    return profile

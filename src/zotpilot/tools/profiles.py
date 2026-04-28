"""Tool profile configuration for ZotPilot MCP exposure."""

from __future__ import annotations

import os

from ..state import mcp

DEFAULT_TOOL_PROFILE = "core"
VALID_TOOL_PROFILES = {"core", "full"}

PROFILE_VISIBLE_TAGS: dict[str, set[str] | None] = {
    "core": None,
    "full": None,
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
    visible_tags = PROFILE_VISIBLE_TAGS.get(profile)
    if visible_tags is not None:
        enable = getattr(mcp, "enable", None)
        if enable is not None:
            enable(tags=visible_tags, components={"tool"}, only=True)

    disabled_tools = parse_disabled_tools()
    if profile == "core":
        disabled_tools.add("profile_library")

    if disabled_tools:
        disable = getattr(mcp, "disable", None)
        if disable is not None:
            disable(names=disabled_tools, components={"tool"})
        else:
            for name in disabled_tools:
                try:
                    mcp.remove_tool(name)
                except Exception:
                    pass

    return profile

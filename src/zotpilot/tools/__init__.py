"""MCP tool registration with group/profile-based filtering."""

import importlib
import os

from ..state import mcp

# Canonical tools only — these are the tools that agents should use.
TOOL_GROUPS: dict[str, set[str]] = {
    "search": {
        "advanced_search",
        "search_boolean",
        "search_figures",
        "search_papers",
        "search_tables",
        "search_topic",
    },
    "context": {
        "get_paper_details",
        "get_passage_context",
    },
    "browse": {
        "browse_library",
        "get_annotations",
        "get_notes",
        "profile_library",
    },
    "cite": {
        "get_citations",
    },
    "index": {
        "get_index_stats",
        "get_unindexed_papers",
        "index_library",
    },
    "write": {
        "create_collection",
        "create_note",
        "manage_collections",
        "manage_tags",
    },
    "ingest": {
        "get_ingest_status",
        "ingest_papers",
        "save_urls",
        "search_academic_databases",
    },
    "admin": {
        "switch_library",
    },
}

# Deprecated aliases — still registered as @mcp.tool() but NOT in TOOL_GROUPS.
# Kept for backward compat; will be removed in v0.6.0.
# They route internally to the canonical tools above.
_DEPRECATED_TOOLS: set[str] = {
    "find_citing_papers", "find_references", "get_citation_count",
    "get_library_overview", "list_tags", "list_collections", "get_collection_papers",
    "add_item_tags", "set_item_tags", "remove_item_tags", "batch_tags",
    "add_to_collection", "remove_from_collection", "batch_collections",
}

TOOL_PROFILES: dict[str, set[str]] = {
    "full": set(TOOL_GROUPS),
    "read_only": {"search", "context", "browse", "cite", "index", "admin"},
    "search_only": {"search", "context", "cite"},
}

# Module → all tool names it registers (canonical + deprecated).
# Every module that has at least one enabled canonical tool gets imported.
# After import, deprecated tools are removed via mcp.remove_tool().
_MODULE_TOOLS: dict[str, set[str]] = {
    "search": TOOL_GROUPS["search"],
    "context": {"get_passage_context"},
    "library": {
        "browse_library", "get_paper_details", "get_notes", "get_annotations",
        "profile_library",
        # deprecated (registered by module, removed after import):
        "get_library_overview", "list_tags", "list_collections", "get_collection_papers",
    },
    "citations": {
        "get_citations",
        "find_citing_papers", "find_references", "get_citation_count",
    },
    "indexing": TOOL_GROUPS["index"],
    "write_ops": {
        "create_collection", "create_note", "manage_collections", "manage_tags",
        "add_item_tags", "set_item_tags", "remove_item_tags", "batch_tags",
        "add_to_collection", "remove_from_collection", "batch_collections",
    },
    "ingestion": TOOL_GROUPS["ingest"],
    "admin": TOOL_GROUPS["admin"],
}


def _parse_disabled_tools(raw: str | None) -> set[str]:
    if not raw:
        return set()
    disabled: set[str] = set()
    for token in raw.replace(";", ",").split(","):
        name = token.strip()
        if name:
            disabled.add(name)
    return disabled


def _get_enabled_tools() -> set[str]:
    profile = os.getenv("ZOTPILOT_TOOL_PROFILE", "full").strip().lower() or "full"
    if profile not in TOOL_PROFILES:
        valid = ", ".join(sorted(TOOL_PROFILES))
        raise ValueError(f"Invalid ZOTPILOT_TOOL_PROFILE '{profile}'. Expected one of: {valid}")

    enabled_groups = TOOL_PROFILES[profile]
    enabled_tools = {
        tool_name
        for group_name in enabled_groups
        for tool_name in TOOL_GROUPS[group_name]
    }
    disabled_tools = _parse_disabled_tools(os.getenv("ZOTPILOT_DISABLE_TOOLS"))
    return enabled_tools - disabled_tools


ENABLED_TOOLS = _get_enabled_tools()


def _register_enabled_modules() -> None:
    for module_name, module_tools in _MODULE_TOOLS.items():
        # Import module if it has at least one enabled canonical tool
        canonical_tools = module_tools - _DEPRECATED_TOOLS
        if not (canonical_tools & ENABLED_TOOLS):
            continue
        importlib.import_module(f"{__name__}.{module_name}")
        # Remove tools not in ENABLED_TOOLS (includes all deprecated tools)
        for tool_name in module_tools - ENABLED_TOOLS:
            try:
                mcp.local_provider.remove_tool(tool_name)
            except Exception:
                continue


_register_enabled_modules()

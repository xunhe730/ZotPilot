"""Admin tools."""
from typing import Annotated, Literal

from pydantic import Field

from ..state import (
    _clear_library_override,
    _get_zotero,
    _set_library_override,
    mcp,
)
from ..workflow import SessionStore
from .profiles import tool_tags

_session_store = SessionStore()


@mcp.tool(tags=tool_tags("admin", "admin"))
def switch_library(
    library_id: Annotated[str | None, Field(description="Library/group ID. None to list available.")] = None,
    library_type: Annotated[Literal["user", "group", "default"], Field(description="'default' resets to user library")] = "group",  # noqa: E501
) -> dict:
    """List libraries or switch active library context.

    NOTE: Switching applies to metadata tools (tags, collections, notes, annotations,
    write operations) and the Zotero Web API reader. It does NOT apply to RAG search
    tools (search_papers, search_topic, search_tables, search_figures), passage context,
    or index stats — these always operate on the default user library because the vector
    store has no per-library isolation yet.
    """
    if library_id is None:
        # List available libraries
        zotero = _get_zotero()
        return {"libraries": zotero.get_libraries()}

    if library_type == "default":
        _clear_library_override()
        return {"switched": True, "library_type": "user", "message": "Reset to default user library"}

    active_session = _session_store.get_active(library_id=str(_get_zotero().library_id))
    _set_library_override(library_id, library_type)
    result = {
        "switched": True,
        "library_id": library_id,
        "library_type": library_type,
        "message": (
            f"Switched to {library_type} library {library_id}. "
            f"Metadata/write tools now operate on this library. "
            f"Note: RAG search and indexing still use the default user library."
        ),
    }
    if active_session is not None:
        result["warning"] = (
            f"Active research session {active_session.session_id} was left in library "
            f"{active_session.library_id}. Approve, cancel, or resume it before switching back."
        )
    return result

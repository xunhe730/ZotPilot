"""Workflow-specific MCP tools and guardrails."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from ..state import ToolError, mcp
from ..workflow import SessionStore
from ..workflow.session_store import current_library_id
from .profiles import tool_tags

_STORE = SessionStore()
_CHECKPOINTS = {"candidate-review", "post-ingest-review"}


def get_active_research_session(*, statuses: set[str] | None = None):
    return _STORE.get_active(library_id=current_library_id(), statuses=statuses)


def load_research_session(session_id: str | None = None):
    if session_id:
        return _STORE.load(session_id)
    return get_active_research_session()


@mcp.tool(tags=tool_tags("core", "workflow", "research"))
def research_session(
    action: Annotated[
        Literal["create", "get", "approve", "validate"],
        Field(description="create/get/approve/validate a research workflow session"),
    ],
    session_id: Annotated[str | None, Field(description="Existing session ID for get/approve/validate")] = None,
    query: Annotated[str | None, Field(description="Research intent, required when action='create'")] = None,
    checkpoint: Annotated[
        Literal["candidate-review", "post-ingest-review"] | None,
        Field(description="Checkpoint to approve when action='approve'"),
    ] = None,
) -> dict:
    """Manage persisted research sessions for the ztp-research workflow."""
    if action == "create":
        active = get_active_research_session()
        if active is not None:
            return active.to_dict()
        if not query:
            raise ToolError("research_session(action='create') requires query")
        return _STORE.create(query=query).to_dict()

    session = load_research_session(session_id)
    if session is None:
        return {"session_id": session_id, "status": "not_found", "item_count": 0}

    if action == "approve":
        if checkpoint not in _CHECKPOINTS:
            raise ToolError("checkpoint must be one of: candidate-review, post-ingest-review")
        session.approve(checkpoint)
        _STORE.save(session)
        return session.to_dict()

    if action == "validate":
        _STORE.save(session)
    return session.to_dict()

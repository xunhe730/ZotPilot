"""MCP tool registration and profile-based visibility filtering."""

from . import admin, citations, context, indexing, ingestion, library, search, workflow, write_ops  # noqa: F401
from .profiles import apply_tool_profile

ACTIVE_TOOL_PROFILE = apply_tool_profile()

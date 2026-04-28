"""MCP tool registration and profile-based visibility filtering."""

from . import (  # noqa: F401
    admin,
    citations,
    context,
    indexing,
    ingestion,
    library,
    search,
    write_ops,
)
from .profiles import apply_tool_profile

ACTIVE_TOOL_PROFILE = apply_tool_profile()

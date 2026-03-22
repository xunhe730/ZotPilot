"""ZotPilot MCP server entry point.

This is a thin orchestrator that imports the mcp instance from state
and all tool modules to trigger decorator registration.
"""
from . import tools  # noqa: F401 — triggers tool registration
from .state import mcp


def main():
    """Run the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()

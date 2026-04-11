"""ZotPilot MCP server entry point.

This is a thin orchestrator that imports the mcp instance from state
and all tool modules to trigger decorator registration.
"""
from . import tools  # noqa: F401 — triggers tool registration
from .state import mcp


def _check_skill_drift():
    """Check if the deployment environment is drifted from the codebase."""
    try:
        from ._platforms import _deployment_status
        from .config import Config
        config = Config.load()
        status = _deployment_status(config)
        if status.get("restart_required"):
            from .state import mcp
            mcp.instructions += (
                "\n\n⚠️ ZotPilot skills or configuration paths are outdated "
                "or not in sync with the current system.\n"
                "Please run `zotpilot register --sync` or `zotpilot upgrade` "
                "to fix this issue."
            )
    except Exception:
        pass  # Do not crash the server on drift detection failure


def main():
    """Run the MCP server."""
    _check_skill_drift()
    mcp.run()


if __name__ == "__main__":
    main()

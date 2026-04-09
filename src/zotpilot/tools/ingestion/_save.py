"""Save helpers and MCP tools for the ingestion package (connector routing, API saves, background worker)."""

from __future__ import annotations

import logging
import threading
import time
from typing import Annotated

from fastmcp.exceptions import ToolError
from pydantic import Field

from ...bridge import DEFAULT_PORT, BridgeServer
from ...state import mcp
from .. import ingestion_bridge
from ..profiles import tool_tags

logger = logging.getLogger(__name__)


def _save_via_api(
    candidate: dict,
    resolved_collection_key: str | None,
    tags: list[str] | None,
    batch,
    writer,
    writer_lock: threading.Lock | None = None,
    logger=None,
    **kwargs,
) -> dict:
    """Thin wrapper around ingestion_bridge.save_via_api."""
    import logging as _logging

    from ._shared import _writer_lock as _default_lock
    _log = logger if logger is not None else _logging.getLogger(__name__)
    # Support legacy _writer_lock kwarg (original ingestion.py API)
    lock = kwargs.get("_writer_lock") or writer_lock or _default_lock
    return ingestion_bridge.save_via_api(
        candidate, resolved_collection_key, tags, batch, writer, lock, logger=_log
    )


def _run_save_worker(
    batch,
    connector_candidates: list[dict],
    api_candidates: list[dict],
    resolved_collection_key: str | None,
) -> None:
    """Thin wrapper — delegates to ingestion_bridge.run_save_worker.

    All callables are loaded lazily from the package __init__ so that
    test patches on 'zotpilot.tools.ingestion.X' are honoured at call time.
    """
    # Lazy imports via package so test patches on zotpilot.tools.ingestion.X are honoured
    from . import (  # type: ignore[attr-defined]
        _discover_via_local_api,
        _discover_via_web_api,
        _get_writer,
        _route_via_local_api,
        _writer_lock,
        save_urls,
    )
    from . import _save_via_api as _pkg_save_via_api
    ingestion_bridge.run_save_worker(
        batch,
        connector_candidates,
        api_candidates,
        resolved_collection_key,
        save_urls_fn=save_urls,
        get_writer_fn=_get_writer,
        writer_lock=_writer_lock,
        logger=logger,
        route_fn=_route_via_local_api,
        discover_fn=_discover_via_local_api,
        save_via_api_fn=_pkg_save_via_api,
        discover_web_fn=_discover_via_web_api,
    )


@mcp.tool(tags=tool_tags("extended", "ingestion"))
def save_urls(
    urls: Annotated[list[str] | str, Field(description="URLs to save. Max 10 per call.")],
    collection_key: Annotated[str | None, Field(description="Zotero collection key for all saved items")] = None,
    tags: Annotated[list[str] | str | None, Field(description="Tags to apply to all saved items")] = None,
) -> dict:
    """Batch save multiple URLs to Zotero via ZotPilot Connector."""
    # Lazy imports via package so test patches on zotpilot.tools.ingestion.X are honoured
    from . import (  # type: ignore[attr-defined]
        _apply_bridge_result_routing,
        _coerce_json_list,
        _ensure_inbox_collection,
    )
    urls = _coerce_json_list(urls, "urls")
    if not isinstance(urls, list):
        raise ToolError("urls must be a JSON array of strings")
    if isinstance(tags, str):
        tags = _coerce_json_list(tags, "tags")

    if not urls:
        raise ToolError("urls list cannot be empty.")
    if len(urls) > 10:
        raise ToolError(f"Too many URLs ({len(urls)}). Max 10 per call — split into batches.")

    if collection_key is None:
        collection_key = _ensure_inbox_collection()
    resolved_collection_key = collection_key

    bridge_url = f"http://127.0.0.1:{DEFAULT_PORT}"
    if not BridgeServer.is_running(DEFAULT_PORT):
        try:
            BridgeServer.auto_start(DEFAULT_PORT)
        except RuntimeError as exc:
            return {"success": False, "error": str(exc), "results": [], "collection_used": resolved_collection_key}

    ext_status = ingestion_bridge.wait_for_extension(bridge_url)
    if not ext_status.get("extension_connected"):
        last_seen = ext_status.get("extension_last_seen_s")
        if last_seen is not None:
            detail = (
                f"ZotPilot Connector last seen {last_seen:.0f}s ago. "
                "Ensure Chrome is open and the extension is enabled."
            )
        else:
            detail = (
                "ZotPilot Connector has not connected. "
                "Ensure Chrome is open and the extension is installed and enabled."
            )
        return {
            "success": False,
            "error": detail,
            "total": len(urls),
            "succeeded": 0,
            "failed": len(urls),
            "results": [{"url": u, "success": False, "error": detail} for u in urls],
            "collection_used": resolved_collection_key,
        }

    id_to_url: dict[str, str] = {}
    enqueue_errors: list[dict] = []
    for url in urls:
        request_id, enqueue_error = ingestion_bridge.enqueue_save_request(
            bridge_url, url, resolved_collection_key,
            tags if isinstance(tags, list) or tags is None else None,
        )
        if enqueue_error is not None:
            enqueue_errors.append({"url": url, **enqueue_error})
        elif request_id is not None:
            id_to_url[request_id] = url

    coerced_tags: list[str] | None = tags if isinstance(tags, list) or tags is None else None
    polled_results = ingestion_bridge.poll_batch_save_results(
        bridge_url,
        id_to_url,
        ingestion_bridge.compute_save_result_poll_timeout_s(len(id_to_url)),
        ingestion_bridge.compute_save_result_poll_overall_timeout_s(len(id_to_url)),
        _apply_bridge_result_routing,
        resolved_collection_key,
        coerced_tags,
        logger,
        sleep_fn=time.sleep,
        monotonic_fn=time.monotonic,
    )
    all_results = enqueue_errors + polled_results
    succeeded = sum(1 for result in all_results if result.get("success") is True)
    failed_count = len(all_results) - succeeded

    return {
        "total": len(urls),
        "succeeded": succeeded,
        "failed": failed_count,
        "results": all_results,
        "collection_used": resolved_collection_key,
    }

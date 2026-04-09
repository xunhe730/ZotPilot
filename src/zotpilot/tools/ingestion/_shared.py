"""Shared helpers and thin wrappers used by both _save and _ingest sub-modules."""

from __future__ import annotations

import json
import logging
import threading
import time  # noqa: F401  re-exported for research_workflow compatibility
from typing import Literal

from fastmcp.exceptions import ToolError

from ...bridge import DEFAULT_PORT, BridgeServer  # noqa: F401  re-exported
from ...state import _get_config, _get_writer, _get_zotero, register_reset_callback
from .. import ingestion_bridge, ingestion_search

logger = logging.getLogger(__name__)

_writer_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Inbox collection cache — lives here so both _ingest and _save can import it
# and tests can patch via `zotpilot.tools.ingestion._inbox_collection_key`.
# ---------------------------------------------------------------------------
_inbox_collection_key: str | None = None
_inbox_lock = threading.Lock()
_INBOX_COLLECTION_NAME = "INBOX"


def _clear_inbox_cache() -> None:
    global _inbox_collection_key
    _inbox_collection_key = None
    # Also clear the package-level re-exported alias so callers/tests that
    # assign via `zotpilot.tools.ingestion._inbox_collection_key = X` see the
    # reset. The package module may not have been imported yet in some early
    # reset paths, so we look it up defensively.
    import sys as _sys
    _pkg = _sys.modules.get("zotpilot.tools.ingestion")
    if _pkg is not None and hasattr(_pkg, "_inbox_collection_key"):
        _pkg._inbox_collection_key = None  # type: ignore[attr-defined]


register_reset_callback(_clear_inbox_cache)


def _ensure_inbox_collection() -> str | None:
    """Return the INBOX collection key, creating it if absent when possible."""
    global _inbox_collection_key
    if _inbox_collection_key is not None:
        return _inbox_collection_key
    with _inbox_lock:
        if _inbox_collection_key is not None:
            return _inbox_collection_key
        try:
            writer = _get_writer()
        except Exception:
            return None
        if not _get_config().zotero_api_key:
            return None
        try:
            with _writer_lock:
                collections = writer._zot.collections()
            for coll in collections:
                data = coll.get("data", {})
                if data.get("name") == _INBOX_COLLECTION_NAME:
                    _inbox_collection_key = data.get("key") or coll.get("key")
                    return _inbox_collection_key
            with _writer_lock:
                response = writer._zot.create_collections([{"name": _INBOX_COLLECTION_NAME}])
            if response and "successful" in response:
                for value in response["successful"].values():
                    _inbox_collection_key = value.get("key") or value.get("data", {}).get("key")
                    if _inbox_collection_key:
                        return _inbox_collection_key
            with _writer_lock:
                collections = writer._zot.collections()
            for coll in collections:
                data = coll.get("data", {})
                if data.get("name") == _INBOX_COLLECTION_NAME:
                    _inbox_collection_key = data.get("key") or coll.get("key")
                    return _inbox_collection_key
        except Exception as exc:
            logger.warning("_ensure_inbox_collection failed: %s", exc)
    return None


def _is_pdf_or_doi_url(url: str | None) -> bool:
    """Return True if url is a direct PDF link or a doi.org redirect."""
    return ingestion_search.is_pdf_or_doi_url(url)


def resolve_doi_to_landing_url(doi: str) -> str | None:
    """Resolve DOI to publisher landing page via doi.org redirect."""
    return ingestion_bridge.resolve_doi_to_landing_url(doi)


def _route_via_local_api(item_key: str, collection_key: str) -> bool:
    """Route item into collection via Zotero Desktop local API (patchable)."""
    return ingestion_bridge.route_via_local_api(item_key, collection_key)


def _discover_via_local_api(url: str, title: str | None) -> str | None:
    """Discover item key via Zotero Desktop local API (patchable)."""
    return ingestion_bridge.discover_item_via_local_api(url, title)


def _discover_via_web_api(url: str, title: str | None) -> str | None:
    """Discover item key via Zotero Web API (patchable)."""
    try:
        writer = _get_writer()
        return ingestion_bridge.discover_item_via_web_api(url, title, writer, _writer_lock)
    except Exception:
        return None


def classify_ingest_candidate(
    paper: dict,
    normalized_doi: str | None,
    arxiv_id: str | None,
    landing_page_url: str | None,
) -> Literal["connector", "api", "reject"]:
    """Classify a paper candidate for routing."""
    return ingestion_search.classify_ingest_candidate(paper, normalized_doi, arxiv_id, landing_page_url)


def _lookup_local_item_key_by_doi(normalized_doi: str | None) -> str | None:
    """Return a unique local Zotero item key for a DOI, if one exists."""
    if not normalized_doi:
        return None
    try:
        hits = _get_zotero().advanced_search(
            [{"field": "doi", "op": "is", "value": normalized_doi}],
            limit=10,
        )
    except Exception:
        return None
    unique_keys = [hit["item_key"] for hit in hits if isinstance(hit, dict) and hit.get("item_key")]
    return unique_keys[0] if unique_keys else None


def _coerce_json_list(value, field_name: str):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception as exc:
            raise ToolError(f"{field_name} must be a JSON array") from exc
    return value

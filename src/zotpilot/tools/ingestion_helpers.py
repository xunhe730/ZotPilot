"""Shared helper utilities for ingestion tools."""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from fastmcp.exceptions import ToolError

from ..state import _get_config, _get_writer, _get_zotero

logger = logging.getLogger(__name__)

_INBOX_COLLECTION_NAME = "INBOX"
_inbox_collection_key: str | None = None
_inbox_lock = threading.Lock()
_writer_lock = threading.Lock()


def clear_inbox_cache() -> None:
    """Reset the inbox collection key cache (called on library switch)."""
    global _inbox_collection_key
    _inbox_collection_key = None


def ensure_inbox_collection(writer_lock: threading.Lock) -> str | None:
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
            with writer_lock:
                collections = writer._zot.collections()
            for coll in collections:
                data = coll.get("data", {})
                if data.get("name") == _INBOX_COLLECTION_NAME:
                    _inbox_collection_key = data.get("key") or coll.get("key")
                    return _inbox_collection_key

            with writer_lock:
                response = writer._zot.create_collections([{"name": _INBOX_COLLECTION_NAME}])
            if response and "successful" in response:
                for value in response["successful"].values():
                    _inbox_collection_key = value.get("key") or value.get("data", {}).get("key")
                    if _inbox_collection_key:
                        return _inbox_collection_key

            with writer_lock:
                collections = writer._zot.collections()
            for coll in collections:
                data = coll.get("data", {})
                if data.get("name") == _INBOX_COLLECTION_NAME:
                    _inbox_collection_key = data.get("key") or coll.get("key")
                    return _inbox_collection_key
        except Exception as exc:
            logger.warning("ensure_inbox_collection failed: %s", exc)

    return None


def coerce_json_list(value, field_name: str):
    """Coerce a JSON string to a list, or pass through if already a list."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception as exc:
            raise ToolError(f"{field_name} must be a JSON array") from exc
    return value


def lookup_local_item_key_by_doi(normalized_doi: str | None) -> str | None:
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


def resolve_dois_concurrent(
    dois: list[str],
    resolve_fn,
) -> dict[str, str | None]:
    """Resolve multiple DOIs concurrently using the provided resolver function."""
    if not dois:
        return {}
    results: dict[str, str | None] = {}
    with ThreadPoolExecutor(max_workers=min(len(dois), 10)) as pool:
        futures = {pool.submit(resolve_fn, doi): doi for doi in dois}
        for future in futures:
            doi = futures[future]
            try:
                results[doi] = future.result()
            except Exception:
                results[doi] = None
    return results

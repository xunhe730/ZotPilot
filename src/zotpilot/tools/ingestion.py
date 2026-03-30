"""MCP tools for academic paper ingestion into Zotero."""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, Literal

import httpx
from fastmcp.exceptions import ToolError
from pydantic import Field

from ..bridge import DEFAULT_PORT, BridgeServer
from ..state import _get_config, _get_writer, _get_zotero, mcp, register_reset_callback
from . import ingestion_bridge, ingestion_search
from .ingest_state import BatchState, BatchStore, IngestItemState

logger = logging.getLogger(__name__)

_writer_lock = threading.Lock()
_inbox_collection_key: str | None = None
_inbox_lock = threading.Lock()
_INBOX_COLLECTION_NAME = "INBOX"


def _clear_inbox_cache() -> None:
    global _inbox_collection_key
    _inbox_collection_key = None


register_reset_callback(_clear_inbox_cache)

_batch_store = BatchStore(max_batches=50, completed_ttl_s=1800.0)
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ingest")


def _clear_batch_store() -> None:
    _batch_store.clear()


register_reset_callback(_clear_batch_store)


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
            collections = writer._zot.collections()
            for coll in collections:
                data = coll.get("data", {})
                if data.get("name") == _INBOX_COLLECTION_NAME:
                    _inbox_collection_key = data.get("key") or coll.get("key")
                    return _inbox_collection_key

            response = writer._zot.create_collections([{"name": _INBOX_COLLECTION_NAME}])
            if response and "successful" in response:
                for value in response["successful"].values():
                    _inbox_collection_key = value.get("key") or value.get("data", {}).get("key")
                    if _inbox_collection_key:
                        return _inbox_collection_key

            collections = writer._zot.collections()
            for coll in collections:
                data = coll.get("data", {})
                if data.get("name") == _INBOX_COLLECTION_NAME:
                    _inbox_collection_key = data.get("key") or coll.get("key")
                    return _inbox_collection_key
        except Exception as exc:
            logger.warning("_ensure_inbox_collection failed: %s", exc)

    return None


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

    unique_keys = [
        hit["item_key"]
        for hit in hits
        if isinstance(hit, dict) and hit.get("item_key")
    ]
    if unique_keys:
        return unique_keys[0]
    return None


def _coerce_json_list(value, field_name: str):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception as exc:
            raise ToolError(f"{field_name} must be a JSON array") from exc
    return value


@mcp.tool()
def search_academic_databases(
    query: Annotated[str, Field(description="Search query for academic papers")],
    limit: Annotated[int, Field(ge=1, le=100, description="Number of results (1-100)")] = 20,
    year_min: Annotated[int | None, Field(description="Earliest publication year filter")] = None,
    year_max: Annotated[int | None, Field(description="Latest publication year filter")] = None,
    sort_by: Annotated[
        Literal["relevance", "citationCount", "publicationDate"],
        Field(description="Sort order: relevance (default), citationCount, or publicationDate")
    ] = "relevance",
) -> list[dict]:
    """Search external academic databases for papers on a topic. Use this as the first step
    for any literature survey, research discovery, or "帮我调研 X" request — it finds papers
    NOT yet in the local library. Does NOT add to Zotero automatically; call ingest_papers
    with selected results to add them.

    Uses OpenAlex only.
    Supports "author:Name" prefix for author-scoped search (use "author:Name | topic"
    for combined queries) and DOI strings for exact lookup."""
    return ingestion_search.search_academic_databases_impl(
        _get_config(),
        query,
        limit,
        year_min,
        year_max,
        sort_by,
        httpx_module=httpx,
        tool_error_cls=ToolError,
        logger=logger,
    )


def _run_save_worker(
    batch: BatchState,
    save_candidates: list[dict],
    resolved_collection_key: str | None,
    tags: list[str] | None,
) -> None:
    """Background worker: runs save_urls chunks and updates batch state.

    This function calls save_urls exactly as the old synchronous code did —
    the only difference is it runs in a background thread and writes results
    into BatchState instead of returning them directly.
    """
    try:
        batch.state = "running"
        urls_to_save = [c["url"] for c in save_candidates]
        candidate_by_url = {c["url"]: c for c in save_candidates}

        for start in range(0, len(urls_to_save), 10):
            chunk = urls_to_save[start:start + 10]
            batch_result = save_urls(chunk, collection_key=resolved_collection_key, tags=tags)
            batch_results = list(batch_result.get("results") or [])
            returned_urls = {r.get("url") for r in batch_results if r.get("url")}

            top_level_failed = batch_result.get("success") is False
            for result in batch_results:
                url = result.get("url")
                candidate = candidate_by_url.get(url)
                if candidate is None:
                    continue
                idx = candidate["_index"]
                if result.get("success") is True:
                    batch.update_item(
                        idx, status="saved",
                        item_key=result.get("item_key"),
                        title=result.get("title") or candidate.get("paper", {}).get("title"),
                    )
                else:
                    batch.update_item(
                        idx, status="failed",
                        error=result.get("error") or result.get("error_message") or "bridge save failed",
                    )

            for url in chunk:
                candidate = candidate_by_url.get(url)
                if candidate is None:
                    continue
                idx = candidate["_index"]
                # Mark failed if top-level failure or URL missing from results,
                # but only if the item is still pending (not already updated above).
                if top_level_failed or url not in returned_urls:
                    for item in batch.pending_items:
                        if item.index == idx and item.status == "pending":
                            batch.update_item(idx, status="failed", error="bridge save failed")
    except Exception as exc:
        logger.error("Ingest worker failed: %s", exc, exc_info=True)
        for item in batch.pending_items:
            if item.status == "pending":
                batch.update_item(item.index, status="failed", error=f"worker error: {exc}")
    finally:
        batch.finalize()


@mcp.tool()
def ingest_papers(
    papers: Annotated[list[dict] | str, Field(description=(
        "JSON array of paper dicts, each with at least one of: doi, arxiv_id, landing_page_url. "
        "Typically from search_academic_databases results. Max 50 per call."
    ))],
    collection_key: Annotated[
        str | None,
        Field(description="Zotero collection key for all ingested papers. Defaults to INBOX."),
    ] = None,
    tags: Annotated[
        list[str] | str | None,
        Field(description='JSON array of tags to apply to all ingested papers, e.g. ["tag1","tag2"]'),
    ] = None,
) -> dict:
    """Start async batch ingestion of papers to Zotero via ZotPilot Connector.

    Returns immediately after validation and duplicate checking. Papers that need
    saving are processed in the background. Use get_ingest_status(batch_id) to
    track progress. When is_final is true, all papers have been processed."""
    papers = _coerce_json_list(papers, "papers")
    if not isinstance(papers, list):
        raise ToolError("papers must be a JSON array of paper dicts")
    if len(papers) > 50:
        raise ToolError(f"Batch size {len(papers)} exceeds maximum of 50. Split into smaller batches.")

    tags = _coerce_json_list(tags, "tags") if isinstance(tags, str) else tags

    if collection_key is None:
        collection_key = _ensure_inbox_collection()
    resolved_collection_key = collection_key

    results: list[dict] = []
    save_candidates: list[dict] = []
    saved = 0
    duplicates = 0
    failed = 0

    for idx, paper in enumerate(papers):
        arxiv_id = paper.get("arxiv_id")
        landing_page_url = paper.get("landing_page_url")
        doi = paper.get("doi")

        normalized_doi = ingestion_search.normalize_doi(doi)
        arxiv_doi = ingestion_search.normalize_doi(f"10.48550/arxiv.{arxiv_id}") if arxiv_id else None
        if not normalized_doi:
            normalized_doi = arxiv_doi

        existing_item_key = _lookup_local_item_key_by_doi(normalized_doi) or (
            _lookup_local_item_key_by_doi(arxiv_doi) if arxiv_doi and arxiv_doi != normalized_doi else None
        )
        if existing_item_key:
            duplicates += 1
            results.append({
                "url": landing_page_url or (f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else None),
                "status": "duplicate",
                "item_key": existing_item_key,
                "title": paper.get("title"),
            })
            continue

        if arxiv_id:
            url = f"https://arxiv.org/abs/{arxiv_id}"
        elif landing_page_url:
            url = landing_page_url
        elif doi:
            failed += 1
            results.append({
                "url": None,
                "status": "failed",
                "error": (
                    "no arxiv_id or landing_page_url; DOI-only papers cannot be ingested. "
                    "doi.org redirects produce unpredictable publisher formats that cause "
                    "Zotero translators to save incorrect entries."
                ),
            })
            continue
        else:
            failed += 1
            results.append({
                "url": None,
                "status": "failed",
                "error": "no usable identifier",
            })
            continue

        save_candidates.append({
            "paper": paper,
            "url": url,
            "_index": idx,
        })

    # --- Preflight (still synchronous — fast enough) ---
    urls_to_save = [candidate["url"] for candidate in save_candidates]
    if urls_to_save and _get_config().preflight_enabled:
        preflight_report = ingestion_bridge.preflight_urls(
            urls_to_save,
            sample_size=5,
            default_port=DEFAULT_PORT,
            bridge_server_cls=BridgeServer,
            logger=logger,
            sleep_fn=time.sleep,
            monotonic_fn=time.monotonic,
        )
        if not preflight_report.get("all_clear", False):
            for blocked in preflight_report.get("blocked", []):
                failed += 1
                results.append({
                    "url": blocked.get("url"),
                    "status": "failed",
                    "error": blocked.get("error") or "preflight blocked",
                })
            for error in preflight_report.get("errors", []):
                failed += 1
                results.append({
                    "url": error.get("url"),
                    "status": "failed",
                    "error": error.get("error") or "preflight failed",
                })
            # All candidates blocked — clear so no background work
            save_candidates = []

    # --- Build batch state ---
    pending_items = [
        IngestItemState(index=c["_index"], url=c["url"], title=c["paper"].get("title"))
        for c in save_candidates
    ]
    batch = BatchState(
        total=len(papers),
        collection_used=resolved_collection_key,
        tags=tags,
        pending_items=pending_items,
    )

    if not save_candidates:
        # Everything resolved synchronously — mark final immediately
        batch.state = "completed" if failed == 0 else "completed_with_errors"
        batch.is_final = True
        batch.finalized_at = time.monotonic()
    else:
        # Submit background work
        _batch_store.put(batch)
        _executor.submit(_run_save_worker, batch, save_candidates, resolved_collection_key, tags)

    return {
        "batch_id": batch.batch_id,
        "is_final": batch.is_final,
        "total": len(papers),
        "saved": saved,
        "duplicates": duplicates,
        "failed": failed,
        "pending_count": len(save_candidates),
        "collection_used": resolved_collection_key,
        "results": results,
        "pending_items": [it.to_dict() for it in pending_items],
        "_instruction": f"Use get_ingest_status(batch_id='{batch.batch_id}') to track progress"
        if not batch.is_final else None,
    }


@mcp.tool()
def get_ingest_status(
    batch_id: Annotated[str, Field(description="Batch ID returned by ingest_papers")],
) -> dict:
    """Check progress of an async paper ingestion batch.

    Returns current status of all papers in the batch. When state is 'completed'
    or 'completed_with_errors', is_final will be true and results contain the
    final item_keys for further operations (tagging, indexing)."""
    batch = _batch_store.get(batch_id)
    if batch is None:
        return {
            "batch_id": batch_id,
            "state": "not_found",
            "is_final": True,
            "error": (
                "Batch not found. It may have expired (TTL 30min after completion) "
                "or the server was restarted. Check Zotero directly."
            ),
        }
    return batch.full_status()


@mcp.tool()
def save_from_url(
    url: str,
    collection_key: str | None = None,
    tags: Annotated[list[str] | str | None, Field(description="Tags to apply, as a list or JSON array string")] = None,
) -> dict:
    """Save a paper from URL to Zotero. Alias for save_urls([url])."""
    batch = save_urls([url], collection_key=collection_key, tags=tags)
    item = batch["results"][0] if batch["results"] else {"success": False, "error": "no result"}
    item["collection_used"] = batch.get("collection_used")
    return item


@mcp.tool()
def save_urls(
    urls: Annotated[list[str] | str, Field(description="URLs to save. Max 10 per call.")],
    collection_key: Annotated[str | None, Field(description="Zotero collection key for all saved items")] = None,
    tags: Annotated[list[str] | str | None, Field(description="Tags to apply to all saved items")] = None,
) -> dict:
    """Batch save multiple URLs to Zotero via ZotPilot Connector."""
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
            return {
                "success": False,
                "error": str(exc),
                "results": [],
                "collection_used": resolved_collection_key,
            }

    # Fast-fail if extension is not connected (Chrome closed or extension disabled).
    ext_status = ingestion_bridge.get_extension_status(bridge_url)
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
            bridge_url,
            url,
            resolved_collection_key,
            tags,
        )
        if enqueue_error is not None:
            enqueue_errors.append({"url": url, **enqueue_error})
        elif request_id is not None:
            id_to_url[request_id] = url

    polled_results = ingestion_bridge.poll_batch_save_results(
        bridge_url,
        id_to_url,
        ingestion_bridge.compute_save_result_poll_timeout_s(len(id_to_url)),
        ingestion_bridge.compute_save_result_poll_overall_timeout_s(len(id_to_url)),
        _apply_bridge_result_routing,
        resolved_collection_key,
        tags,
        logger,
        sleep_fn=time.sleep,
        monotonic_fn=time.monotonic,
    )
    all_results = enqueue_errors + polled_results
    succeeded = sum(1 for result in all_results if result.get("success") is True)
    failed = len(all_results) - succeeded

    return {
        "total": len(urls),
        "succeeded": succeeded,
        "failed": failed,
        "results": all_results,
        "collection_used": resolved_collection_key,
    }


def _apply_bridge_result_routing(
    result: dict,
    collection_key: str | None,
    tags: list[str] | None,
) -> dict:
    """Apply collection/tag routing after a bridge save result."""
    return ingestion_bridge.apply_bridge_result_routing(
        result,
        collection_key,
        tags,
        get_config=_get_config,
        get_writer=_get_writer,
        discover_saved_item_key_fn=lambda title,
        url,
        known_key,
        writer,
        window_s=ingestion_bridge.ITEM_DISCOVERY_WINDOW_S: ingestion_bridge.discover_saved_item_key(
            title, url, known_key, writer, window_s=window_s, logger=logger
        ),
        apply_collection_tag_routing_fn=lambda item_key,
        routed_collection_key,
        routed_tags,
        writer: ingestion_bridge.apply_collection_tag_routing(
            item_key, routed_collection_key, routed_tags, writer, get_config=_get_config
        ),
        writer_lock=_writer_lock,
        sleep_fn=time.sleep,
        logger=logger,
    )

"""MCP tools for academic paper ingestion into Zotero."""

from __future__ import annotations

import json
import logging
import re as _re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Annotated, Literal

import httpx
from fastmcp.exceptions import ToolError
from pydantic import Field

from ..bridge import DEFAULT_PORT, BridgeServer
from ..state import _get_config, _get_writer, _get_zotero, mcp, register_reset_callback
from . import ingestion_bridge, ingestion_search
from .ingest_state import _POST_INGEST_INSTRUCTION, BatchState, BatchStore, IngestItemState

logger = logging.getLogger(__name__)

_writer_lock = threading.Lock()
_inbox_collection_key: str | None = None
_inbox_lock = threading.Lock()
_INBOX_COLLECTION_NAME = "INBOX"
_ZOTERO_LOCAL_API_ITEMS_URL = "http://127.0.0.1:23119/api/users/0/items"


def _clear_inbox_cache() -> None:
    global _inbox_collection_key
    _inbox_collection_key = None


register_reset_callback(_clear_inbox_cache)

_batch_store = BatchStore(max_batches=50, completed_ttl_s=1800.0)
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ingest")


def _clear_batch_store() -> None:
    _batch_store.clear()


register_reset_callback(_clear_batch_store)

# ---------------------------------------------------------------------------
# URL classification for routing decisions
# ---------------------------------------------------------------------------

_PDF_URL_RE = _re.compile(
    r"(?:"
    r"\.pdf(?:[?#]|$)"          # ends with .pdf or .pdf?query
    r"|/pdf(?:[?/]|$)"          # /pdf path segment
    r"|/content/pdf/"           # Springer PDF pattern
    r"|pdf\.sciencedirect\.com" # Elsevier PDF domain
    r")",
    _re.IGNORECASE,
)
_DOI_REDIRECT_RE = _re.compile(r"^https?://(?:dx\.)?doi\.org/10\.", _re.IGNORECASE)


def _is_pdf_or_doi_url(url: str | None) -> bool:
    """Return True if url is a direct PDF link or a doi.org redirect."""
    if not url:
        return False
    return bool(_PDF_URL_RE.search(url)) or bool(_DOI_REDIRECT_RE.match(url))


_LINKINGHUB_PII_RE = _re.compile(
    r"^https?://linkinghub\.elsevier\.com/retrieve/pii/(S[0-9X]+)",
    _re.IGNORECASE,
)


def _normalize_landing_url(url: str) -> str:
    """Convert known intermediate redirectors to final landing pages."""
    # Elsevier linkinghub → ScienceDirect
    m = _LINKINGHUB_PII_RE.match(url)
    if m:
        return f"https://www.sciencedirect.com/science/article/pii/{m.group(1)}"
    return url


def resolve_doi_to_landing_url(doi: str) -> str | None:
    """Resolve DOI to publisher landing page via doi.org redirect."""
    try:
        response = httpx.head(
            f"https://doi.org/{doi}",
            follow_redirects=False,
            timeout=10.0,
        )
        if response.status_code in (301, 302, 303, 307, 308):
            url = response.headers.get("location")
            if url:
                return _normalize_landing_url(url)
    except Exception as exc:
        logger.debug("DOI resolution failed for %s: %s", doi, exc)
    return None


def _resolve_dois_concurrent(dois: list[str]) -> dict[str, str | None]:
    """Resolve multiple DOIs concurrently."""
    if not dois:
        return {}

    results: dict[str, str | None] = {}
    with ThreadPoolExecutor(max_workers=min(len(dois), 10)) as pool:
        futures = {pool.submit(resolve_doi_to_landing_url, doi): doi for doi in dois}
        for future in as_completed(futures):
            doi = futures[future]
            try:
                results[doi] = future.result()
            except Exception:
                results[doi] = None
    return results


def classify_ingest_candidate(
    paper: dict,
    normalized_doi: str | None,
    arxiv_id: str | None,
    landing_page_url: str | None,
) -> Literal["connector", "api", "reject"]:
    """Classify a paper candidate for routing.

    Returns:
        "connector" - regular landing page, use Zotero Connector
        "api"       - PDF direct link or doi.org URL, use API fallback
        "reject"    - no usable identifier
    """
    if arxiv_id:
        return "connector"
    if landing_page_url and not _is_pdf_or_doi_url(landing_page_url):
        return "connector"
    resolved_url = paper.get("_resolved_landing_url")
    if resolved_url and not _is_pdf_or_doi_url(resolved_url):
        return "connector"
    if normalized_doi or (paper.get("doi") and not landing_page_url):
        return "api"
    return "reject"


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


def _route_via_local_api(item_key: str, collection_key: str) -> bool:
    """Route an item into a collection via Zotero Desktop local API."""
    try:
        req = urllib.request.Request(
            f"{_ZOTERO_LOCAL_API_ITEMS_URL}/{item_key}",
            headers={"Accept": "application/json", "Zotero-Allowed-Request": "1"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            item = json.loads(resp.read())
        version = item.get("version", 0)
        data = item.get("data", item)
        collections = set(data.get("collections", []))
        collections.add(collection_key)

        patch_req = urllib.request.Request(
            f"{_ZOTERO_LOCAL_API_ITEMS_URL}/{item_key}",
            data=json.dumps({"collections": sorted(collections)}).encode(),
            headers={
                "Content-Type": "application/json",
                "Zotero-Allowed-Request": "1",
                "If-Unmodified-Since-Version": str(version),
            },
            method="PATCH",
        )
        with urllib.request.urlopen(patch_req, timeout=5):
            return True
    except (urllib.error.URLError, ConnectionRefusedError, OSError):
        return False
    except Exception as exc:
        logger.warning("Local API routing failed for %s: %s", item_key, exc)
        return False


def _discover_via_local_api(url: str, title: str | None) -> str | None:
    """Try to discover a newly saved item via Zotero Desktop local API."""
    if not title:
        return None
    try:
        search_url = (
            f"{_ZOTERO_LOCAL_API_ITEMS_URL}/top?format=json&limit=5"
            f"&q={urllib.parse.quote(title[:50])}"
            f"&qmode=titleCreatorYear&sort=dateAdded&direction=desc"
        )
        req = urllib.request.Request(
            search_url,
            headers={"Accept": "application/json", "Zotero-Allowed-Request": "1"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            items = json.loads(resp.read())
        if len(items) == 1:
            return items[0].get("key")
        if len(items) > 1:
            logger.debug("Local API discovery: ambiguous match (%d items) for '%s'", len(items), title[:50])
    except Exception:
        return None
    return None


def _discover_via_web_api(url: str, title: str | None) -> str | None:
    """Fallback item-key discovery via Zotero Web API."""
    if not title:
        return None
    try:
        with _writer_lock:
            keys = _get_writer().find_items_by_url_and_title(url, title, window_s=120)
        if len(keys) == 1:
            return keys[0]
    except Exception:
        return None
    return None


@mcp.tool()
def search_academic_databases(
    query: Annotated[str, Field(description="Search query for academic papers")],
    limit: Annotated[int, Field(ge=1, le=100, description="Number of results (1-100)")] = 20,
    year_min: Annotated[int | None, Field(description="Earliest publication year filter")] = None,
    year_max: Annotated[int | None, Field(description="Latest publication year filter")] = None,
    sort_by: Annotated[
        Literal["relevance", "citationCount", "publicationDate"],
        Field(description="Sort order: relevance (default), citationCount, or publicationDate"),
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


def _save_via_api(
    candidate: dict,
    resolved_collection_key: str | None,
    tags: list[str] | None,
    batch: BatchState,
    writer,  # ZoteroWriter instance
    _writer_lock: threading.Lock,
) -> dict:
    """Save a single paper via API (CrossRef/arXiv + pyzotero), bypassing Connector.

    All pyzotero API calls are wrapped in _writer_lock for thread safety.
    On success, sets routing_status="routed_by_api" so reconciliation skips this item.
    """
    from ..state import _get_resolver

    paper = candidate["paper"]
    idx = candidate["_index"]

    # --- Determine identifier to resolve ---
    doi = paper.get("doi")
    arxiv_id = paper.get("arxiv_id")
    landing_page_url = paper.get("landing_page_url")

    normalized_doi = ingestion_search.normalize_doi(doi)
    arxiv_doi = ingestion_search.normalize_doi(f"10.48550/arxiv.{arxiv_id}") if arxiv_id else None
    if not normalized_doi:
        normalized_doi = arxiv_doi

    if arxiv_id:
        identifier = f"arxiv:{arxiv_id}"
    elif normalized_doi:
        identifier = normalized_doi
    elif landing_page_url:
        identifier = landing_page_url
    else:
        batch.update_item(idx, status="failed", error="no usable identifier for API resolution")
        return {"success": False, "error": "no usable identifier"}

    try:
        resolver = _get_resolver()
        metadata = resolver.resolve(identifier)

        # --- Merge OpenAlex abstract when CrossRef abstract is empty ---
        if not metadata.abstract:
            abstract_snippet = paper.get("abstract_snippet") or paper.get("abstract")
            if abstract_snippet:
                metadata.abstract = abstract_snippet

        with _writer_lock:
            collection_keys = [resolved_collection_key] if resolved_collection_key else None
            result = writer.create_item_from_metadata(
                metadata,
                collection_keys=collection_keys,
                tags=tags,
            )

        # Extract created item key from pyzotero result
        item_key = None
        if result and "successful" in result:
            for value in result["successful"].values():
                item_key = value.get("key") or value.get("data", {}).get("key")
                if item_key:
                    break

        if not item_key:
            raise ToolError("create_item_from_metadata returned no item key")

        # --- Attach OA PDF (best-effort) ---
        try:
            with _writer_lock:
                writer.try_attach_oa_pdf(
                    item_key,
                    doi=metadata.doi,
                    oa_url=paper.get("oa_url") or metadata.oa_url,
                    arxiv_id=metadata.arxiv_id,
                )
        except Exception as attach_exc:
            logger.debug("PDF attach best-effort failed for %s: %s", item_key, attach_exc)
            pass  # best-effort, proceed without PDF

        connector_error = candidate.get("_connector_error")
        warning = None
        if connector_error:
            warning = (
                f"Connector failed ({connector_error}); saved via API (metadata only, no PDF). "
                "Check Chrome/Connector if you need PDF."
            )

        batch.update_item(
            idx,
            status="saved",
            item_key=item_key,
            title=metadata.title,
            routing_status="routed_by_api",
            ingest_method="api",
            warning=warning,
        )
        if item_key:
            ingestion_bridge._cleanup_publisher_tags(item_key, landing_page_url or "", writer, logger)
        return {
            "success": True,
            "item_key": item_key,
            "title": metadata.title,
            "error": None,
        }

    except Exception as exc:
        logger.warning("API save failed for index %d: %s", idx, exc)
        batch.update_item(idx, status="failed", error=str(exc))
        return {"success": False, "error": str(exc)}


def _run_save_worker(
    batch: BatchState,
    connector_candidates: list[dict],
    api_candidates: list[dict],
    resolved_collection_key: str | None,
) -> None:
    """Background worker: runs save_urls chunks and updates batch state.

    This function calls save_urls exactly as the old synchronous code did —
    the only difference is it runs in a background thread and writes results
    into BatchState instead of returning them directly.

    Phase 1: Connector saves (via browser bridge)
    Phase 2: API saves (via CrossRef/arXiv + pyzotero, skipped by reconciliation)
    """
    try:
        batch.state = "running"
        saved = 0
        failed = 0

        # --- Phase 1: Connector saves ---
        candidate_by_index: dict[int, dict] = {c["_index"]: c for c in connector_candidates}
        candidate_by_url: dict[str, dict] = {c["url"]: c for c in connector_candidates}
        urls_to_save = [c["url"] for c in connector_candidates]

        for start in range(0, len(urls_to_save), 10):
            chunk = urls_to_save[start : start + 10]
            batch_result = save_urls(chunk, collection_key=resolved_collection_key, tags=None)
            batch_results = list(batch_result.get("results") or [])
            returned_urls = {r.get("url") for r in batch_results if r.get("url")}

            top_level_failed = batch_result.get("success") is False
            for result in batch_results:
                idx = result.get("index")
                url = result.get("url")
                if idx is None:
                    candidate = candidate_by_url.get(url)
                    if candidate is None:
                        continue
                    idx = candidate["_index"]
                else:
                    candidate = candidate_by_index.get(idx)
                if result.get("success") is True:
                    batch.update_item(
                        idx,
                        status="saved",
                        item_key=result.get("item_key"),
                        title=result.get("title") or (candidate.get("paper", {}).get("title") if candidate else None),
                        warning=result.get("warning"),
                        routing_status=result.get("routing_status"),
                    )
                else:
                    batch.update_item(
                        idx,
                        status="failed",
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

        # --- Phase 2: API fallback for failed connector items ---
        # Preserve the connector failure reason as a warning so the user knows
        # what went wrong (anti-bot, timeout, translator error, etc.), while
        # still attempting API save to ensure metadata is not lost.
        connector_api_retries: list[dict] = []
        for candidate in connector_candidates:
            paper = candidate["paper"]
            if not paper.get("doi"):
                continue
            for item in batch.pending_items:
                if item.index == candidate["_index"] and item.status == "failed":
                    connector_error = item.error or "connector save failed"
                    connector_api_retries.append(
                        {
                            **candidate,
                            "url": None,
                            "ingest_method": "api",
                            "_connector_error": connector_error,
                        }
                    )
                    batch.update_item(
                        candidate["_index"],
                        status="pending",
                        ingest_method="api",
                        warning=(
                            f"Connector failed ({connector_error}); retrying via API (metadata only, no PDF). "
                            "Check Chrome/Connector if you need PDF."
                        ),
                        error=None,
                    )
                    break

        phase2_api_candidates = api_candidates + connector_api_retries
        if phase2_api_candidates:
            writer = _get_writer()
            for candidate in phase2_api_candidates:
                idx = candidate["_index"]
                api_result = _save_via_api(
                    candidate,
                    resolved_collection_key=resolved_collection_key,
                    tags=None,
                    batch=batch,
                    writer=writer,
                    _writer_lock=_writer_lock,
                )
                if api_result.get("success"):
                    saved += 1
                else:
                    failed += 1

        # Post-batch reconciliation for items that were saved but not fully routed.
        # API-saved items are skipped (already routed via API).
        unrouted_items = [
            item
            for item in batch.pending_items
            if item.status == "saved" and item.item_key and not item.routing_status and item.ingest_method != "api"
        ]
        deferred_items = [item for item in batch.pending_items if item.status == "saved" and not item.item_key]
        if (unrouted_items or deferred_items) and resolved_collection_key:
            logger.info(
                "Starting reconciliation for %d unrouted and %d deferred items",
                len(unrouted_items),
                len(deferred_items),
            )
            time.sleep(15)
            writer = None
            for item in deferred_items:
                discovered_key = _discover_via_local_api(item.url or "", item.title)
                if not discovered_key:
                    discovered_key = _discover_via_web_api(item.url or "", item.title)
                if discovered_key:
                    item.item_key = discovered_key
                    unrouted_items.append(item)
                else:
                    batch.update_item(
                        item.index,
                        status="failed",
                        error="Item not found in Zotero after save — connector may have reported false success.",
                        routing_status="routing_failed",
                    )
                    logger.error("Reconciliation: item_key not found for %s — demoted to failed", item.url)

            for item in unrouted_items:
                if not item.item_key:
                    continue
                try:
                    if _route_via_local_api(item.item_key, resolved_collection_key):
                        batch.update_item(
                            item.index,
                            status="saved",
                            item_key=item.item_key,
                            warning=None,
                            routing_status="routed_by_reconciliation_local",
                        )
                    else:
                        if writer is None:
                            writer = _get_writer()
                        with _writer_lock:
                            writer.add_to_collection(item.item_key, resolved_collection_key)
                        batch.update_item(
                            item.index,
                            status="saved",
                            item_key=item.item_key,
                            warning=None,
                            routing_status="routed_by_reconciliation_web",
                        )
                    # Clean publisher auto-tags after successful routing
                    if writer is None:
                        writer = _get_writer()
                    ingestion_bridge._cleanup_publisher_tags(item.item_key, item.url or "", writer, logger)
                except Exception as exc:
                    batch.update_item(
                        item.index,
                        status="saved",
                        item_key=item.item_key,
                        warning=f"Reconciliation routing failed: {exc}",
                        routing_status="routing_failed",
                    )
                    logger.error("Reconciliation routing failed for %s: %s", item.item_key, exc)

    except Exception as exc:
        logger.error("Ingest worker failed: %s", exc, exc_info=True)
        for item in batch.pending_items:
            if item.status == "pending":
                batch.update_item(item.index, status="failed", error=f"worker error: {exc}")
    finally:
        batch.finalize()


@mcp.tool()
def ingest_papers(
    papers: Annotated[
        list[dict] | str,
        Field(
            description=(
                "JSON array of paper dicts, each with at least one of: doi, arxiv_id, landing_page_url. "
                "Typically from search_academic_databases results. Max 50 per call."
            )
        ),
    ],
    collection_key: Annotated[
        str | None,
        Field(description="Zotero collection key for all ingested papers. Defaults to INBOX."),
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

    if collection_key is None:
        collection_key = _ensure_inbox_collection()
    resolved_collection_key = collection_key

    results: list[dict] = []
    connector_candidates: list[dict] = []
    api_candidates: list[dict] = []
    saved = 0
    duplicates = 0
    failed = 0
    skipped_indices: set[int] = set()
    batch_seen_dois: dict[str, int] = {}

    for idx, paper in enumerate(papers):
        arxiv_id = paper.get("arxiv_id")
        landing_page_url = paper.get("landing_page_url")
        doi = paper.get("doi")

        normalized_doi = ingestion_search.normalize_doi(doi)
        arxiv_doi = ingestion_search.normalize_doi(f"10.48550/arxiv.{arxiv_id}") if arxiv_id else None
        if not normalized_doi:
            normalized_doi = arxiv_doi

        if normalized_doi and normalized_doi in batch_seen_dois:
            first_idx = batch_seen_dois[normalized_doi]
            logger.warning("Skipping batch item %d due to duplicate DOI %s already seen at index %d", idx, normalized_doi, first_idx)
            skipped_indices.add(idx)
            duplicates += 1
            results.append(
                {
                    "url": landing_page_url or (f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else None),
                    "status": "duplicate_in_batch",
                    "title": paper.get("title"),
                    "error": f"Duplicate of item {first_idx} in this batch",
                }
            )
            continue
        if normalized_doi:
            batch_seen_dois[normalized_doi] = idx

        existing_item_key = _lookup_local_item_key_by_doi(normalized_doi) or (
            _lookup_local_item_key_by_doi(arxiv_doi) if arxiv_doi and arxiv_doi != normalized_doi else None
        )
        if existing_item_key:
            skipped_indices.add(idx)
            duplicates += 1
            if resolved_collection_key:
                try:
                    writer = _get_writer()
                    with _writer_lock:
                        writer.add_to_collection(existing_item_key, resolved_collection_key)
                except Exception as exc:
                    results.append(
                        {
                            "url": landing_page_url or (f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else None),
                            "status": "duplicate",
                            "item_key": existing_item_key,
                            "title": paper.get("title"),
                            "warning": f"collection routing failed: {exc}",
                        }
                    )
                    continue
            results.append(
                {
                    "url": landing_page_url or (f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else None),
                    "status": "duplicate",
                    "item_key": existing_item_key,
                    "title": paper.get("title"),
                }
            )
            continue

    dois_to_resolve: list[str] = []
    seen_dois: set[str] = set()
    for idx, paper in enumerate(papers):
        if idx in skipped_indices:
            continue
        normalized_doi = ingestion_search.normalize_doi(paper.get("doi"))
        if (
            normalized_doi
            and not paper.get("arxiv_id")
            and (not paper.get("landing_page_url") or _is_pdf_or_doi_url(paper.get("landing_page_url")))
            and normalized_doi not in seen_dois
        ):
            seen_dois.add(normalized_doi)
            dois_to_resolve.append(normalized_doi)

    resolved_dois = _resolve_dois_concurrent(dois_to_resolve)
    for idx, paper in enumerate(papers):
        if idx in skipped_indices:
            continue
        normalized_doi = ingestion_search.normalize_doi(paper.get("doi"))
        if normalized_doi in resolved_dois:
            paper["_resolved_landing_url"] = resolved_dois[normalized_doi]

    for idx, paper in enumerate(papers):
        if idx in skipped_indices:
            continue
        arxiv_id = paper.get("arxiv_id")
        landing_page_url = paper.get("landing_page_url")
        doi = paper.get("doi")

        normalized_doi = ingestion_search.normalize_doi(doi)
        arxiv_doi = ingestion_search.normalize_doi(f"10.48550/arxiv.{arxiv_id}") if arxiv_id else None
        if not normalized_doi:
            normalized_doi = arxiv_doi

        routing = classify_ingest_candidate(paper, normalized_doi, arxiv_id, landing_page_url)

        if routing == "reject":
            failed += 1
            results.append(
                {
                    "url": landing_page_url or None,
                    "status": "failed",
                    "error": "no usable identifier",
                }
            )
            continue

        if routing == "api":
            api_candidates.append(
                {
                    "paper": paper,
                    "url": None,
                    "_index": idx,
                    "ingest_method": "api",
                }
            )
            continue

        # connector
        if arxiv_id:
            url = f"https://arxiv.org/abs/{arxiv_id}"
        else:
            url = paper.get("_resolved_landing_url") or landing_page_url
        connector_candidates.append(
            {
                "paper": paper,
                "url": url,
                "_index": idx,
                "ingest_method": "connector",
            }
        )

    # --- Bridge availability check: no Chrome → re-route DOI candidates to API ---
    if connector_candidates:
        bridge_running = BridgeServer.is_running(DEFAULT_PORT)
        if not bridge_running:
            try:
                BridgeServer.auto_start(DEFAULT_PORT)
                bridge_running = True
            except Exception:
                bridge_running = False
        if bridge_running:
            ext_status = ingestion_bridge.get_extension_status(f"http://127.0.0.1:{DEFAULT_PORT}")
            bridge_running = ext_status.get("extension_connected", False)

        if not bridge_running:
            _no_bridge_warning = (
                "Chrome/Connector not available — falling back to API (metadata only, no PDF). "
                "For full PDF download, open Chrome with ZotPilot Connector extension enabled."
            )
            doi_count = sum(1 for c in connector_candidates if c["paper"].get("doi"))
            no_doi_count = len(connector_candidates) - doi_count
            for candidate in connector_candidates:
                paper = candidate["paper"]
                if paper.get("doi"):
                    api_candidates.append(
                        {
                            "paper": paper,
                            "url": None,
                            "_index": candidate["_index"],
                            "ingest_method": "api",
                        }
                    )
                else:
                    failed += 1
                    results.append(
                        {
                            "url": candidate["url"],
                            "status": "failed",
                            "error": "Chrome/Connector not available, no DOI for API fallback",
                        }
                    )
            connector_candidates = []  # all re-routed or failed
            results.append(
                {
                    "status": "warning",
                    "warning": _no_bridge_warning,
                    "api_fallback_count": doi_count,
                    "no_fallback_count": no_doi_count,
                }
            )

    # --- Preflight (still synchronous — fast enough) ---
    urls_to_save = [candidate["url"] for candidate in connector_candidates]
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
                results.append(
                    {
                        "url": blocked.get("url"),
                        "status": "failed",
                        "error": blocked.get("error") or "preflight blocked",
                    }
                )
            for error in preflight_report.get("errors", []):
                failed += 1
                results.append(
                    {
                        "url": error.get("url"),
                        "status": "failed",
                        "error": error.get("error") or "preflight failed",
                    }
                )
            # All connector candidates blocked — user must handle anti-bot/timeout then retry
            connector_candidates = []

            # If no API candidates remain either, return immediately so the agent
            # surfaces the blocked/error list to the user instead of polling.
            if not api_candidates:
                return {
                    "batch_id": None,
                    "is_final": True,
                    "total": len(papers),
                    "saved": saved,
                    "duplicates": duplicates,
                    "failed": failed,
                    "pending_count": 0,
                    "collection_used": resolved_collection_key,
                    "results": results,
                    "blocked": preflight_report.get("blocked", []),
                    "errors": preflight_report.get("errors", []),
                    "pending_items": [],
                    "_instruction": (
                        "Preflight detected blocked or errored URLs — batch halted. "
                        "Show the blocked/errors list to the user and wait for them to "
                        "resolve anti-bot verification in Chrome before retrying."
                    ),
                }

    # --- Build batch state (includes ALL candidates for index-based updates) ---
    pending_items = [
        IngestItemState(
            index=c["_index"],
            url=c["url"],
            title=c["paper"].get("title"),
            ingest_method=c.get("ingest_method"),
        )
        for c in connector_candidates + api_candidates
    ]
    batch = BatchState(
        total=len(papers),
        collection_used=resolved_collection_key,
        pending_items=pending_items,
    )

    if not connector_candidates and not api_candidates:
        # Everything resolved synchronously — mark final immediately
        batch.state = "completed" if failed == 0 else "completed_with_errors"
        batch.is_final = True
        batch.finalized_at = time.monotonic()
    else:
        # Submit background work
        _batch_store.put(batch)
        _executor.submit(_run_save_worker, batch, connector_candidates, api_candidates, resolved_collection_key)

    _instruction: str | None = None
    if batch.is_final and (saved + duplicates) > 0:
        _instruction = _POST_INGEST_INSTRUCTION
    elif not batch.is_final:
        _instruction = f"Use get_ingest_status(batch_id='{batch.batch_id}') to track progress"

    return {
        "batch_id": batch.batch_id,
        "is_final": batch.is_final,
        "total": len(papers),
        "saved": saved,
        "duplicates": duplicates,
        "failed": failed,
        "pending_count": len(connector_candidates) + len(api_candidates),
        "collection_used": resolved_collection_key,
        "results": results,
        "pending_items": [it.to_dict() for it in pending_items],
        "_instruction": _instruction,
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
        discover_saved_item_key_fn=lambda title, url, known_key, writer, window_s=(
            ingestion_bridge.ITEM_DISCOVERY_WINDOW_S
        ): ingestion_bridge.discover_saved_item_key(
            title, url, known_key, writer, window_s=window_s, logger=logger
        ),
        apply_collection_tag_routing_fn=lambda item_key, routed_collection_key, routed_tags, writer: (
            ingestion_bridge.apply_collection_tag_routing(
                item_key, routed_collection_key, routed_tags, writer, get_config=_get_config
            )
        ),
        writer_lock=_writer_lock,
        sleep_fn=time.sleep,
        logger=logger,
    )

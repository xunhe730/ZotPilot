"""MCP tools for academic paper ingestion into Zotero."""
from __future__ import annotations

import logging
import threading
import time
from typing import Annotated, Literal

import httpx
from fastmcp.exceptions import ToolError
from pydantic import Field

from ..bridge import DEFAULT_PORT, BridgeServer
from ..state import _get_config, _get_resolver, _get_writer, mcp

logger = logging.getLogger(__name__)

# How long to wait after the extension reports save completion before querying Zotero.
# Gives Zotero desktop time to sync the new item to the web API.
_ITEM_DISCOVERY_DELAY_S = 3.0

# Window for item discovery: only consider items modified within this many seconds
# before the save completion timestamp.
_ITEM_DISCOVERY_WINDOW_S = 60


def _apply_collection_tag_routing(
    item_key: str,
    collection_key: str | None,
    tags: list[str] | None,
    writer,
) -> str | None:
    """Apply collection and/or tag routing to an item. Returns None on success, else warning."""
    if not collection_key and not tags:
        return None  # No routing requested — nothing to do

    needs_api_key = (collection_key is not None) or (tags is not None)
    config = _get_config()
    if needs_api_key and not config.zotero_api_key:
        return "collection_key and tags ignored — ZOTERO_API_KEY not configured"

    try:
        if collection_key:
            writer.add_to_collection(item_key, collection_key)
        if tags:
            writer.add_item_tags(item_key, tags)
        return None
    except Exception as e:
        logger.warning(f"Collection/tag routing failed for {item_key}: {e}")
        # Bridge-side routing is best-effort; the save itself succeeded.
        # Return a warning rather than failing the whole operation.
        return f"collection_key/tags partially applied — {e}"


def _discover_saved_item_key(
    title: str,
    url: str,
    known_key: str | None,
    writer,
) -> str | None:
    """Best-effort item key discovery.

    Strategy:
    - If known_key is available (saveAsWebpage path, ~5% of saves), use it directly.
    - Otherwise: query Zotero for items added in the last 60s matching BOTH title
      AND URL. Require both to minimize false positives.
    - If exactly one match: use it.
    - If zero or multiple matches: return None (caller returns warning).

    This is inherently unreliable under concurrent saves or duplicate titles.
    Phase 2 will replace this with a correlation ID flowing end-to-end.
    """
    if known_key:
        return known_key

    if not title and not url:
        return None

    try:
        items = writer.find_items_by_url_and_title(url, title)
    except Exception as e:
        logger.warning(f"Item discovery query failed: {e}")
        return None

    if len(items) == 1:
        return items[0]
    # 0 or 2+ matches: ambiguous, do not apply routing
    return None


@mcp.tool()
def add_paper_by_identifier(
    identifier: Annotated[str, Field(description=(
        "Paper identifier: DOI (e.g. 10.1038/s41586-024...), "
        "arXiv ID (arxiv:2301.00001), arXiv URL (arxiv.org/abs/...), "
        "or doi.org URL."
    ))],
    collection_key: Annotated[str | None, Field(description="Zotero collection key to add the paper to")] = None,
    tags: Annotated[list[str] | None, Field(description="Tags to apply to the paper")] = None,
    attach_pdf: Annotated[bool, Field(description="Attempt to find and attach an open-access PDF")] = True,
) -> dict:
    """Add a single paper to Zotero by DOI or arXiv identifier.
    Fetches metadata automatically. Checks for duplicates before creating."""
    resolver = _get_resolver()
    writer = _get_writer()

    metadata = resolver.resolve(identifier)  # raises ToolError on unknown format

    if metadata.doi:
        existing = writer.check_duplicate_by_doi(metadata.doi)
        if existing:
            return {
                "success": True,
                "duplicate": True,
                "existing_key": existing,
                "title": metadata.title,
            }

    result = writer.create_item_from_metadata(
        metadata,
        collection_keys=[collection_key] if collection_key else None,
        tags=tags,
    )

    if not isinstance(result, dict) or not result.get("success"):
        raise ToolError(f"Failed to create Zotero item: {result}")

    item_key = next(iter(result["success"].values()))

    pdf_status = "skipped"
    if attach_pdf:
        pdf_status = writer.try_attach_oa_pdf(
            item_key=item_key,
            doi=metadata.doi,
            oa_url=metadata.oa_url,
            crossref_raw=getattr(resolver, "last_crossref_metadata", None),
            arxiv_id=metadata.arxiv_id,
        )

    return {
        "success": True,
        "duplicate": False,
        "item_key": item_key,
        "title": metadata.title,
        "item_type": metadata.item_type,
        "pdf": pdf_status,
    }


def _reconstruct_abstract(inverted_index: dict | None) -> str:
    """Reconstruct plain-text abstract from OpenAlex inverted index format."""
    if not inverted_index:
        return ""
    words: dict[int, str] = {}
    for word, positions in inverted_index.items():
        for pos in positions:
            words[pos] = word
    return " ".join(words[i] for i in sorted(words))


def _search_openalex(
    query: str,
    limit: int,
    year_min: int | None,
    year_max: int | None,
    sort_by: str,
) -> list[dict]:
    """Search OpenAlex API. Returns papers in the same format as S2 results."""
    sort_map = {
        "relevance": "relevance_score:desc",
        "citationCount": "cited_by_count:desc",
        "publicationDate": "publication_date:desc",
    }
    params: dict = {
        "search": query,
        "per-page": min(limit, 200),
        "sort": sort_map.get(sort_by, "relevance_score:desc"),
        "select": "id,doi,display_name,authorships,publication_year,cited_by_count,open_access,abstract_inverted_index",
        "mailto": "zotpilot@example.com",
    }
    filters: list[str] = []
    if year_min:
        filters.append(f"publication_year:>{year_min - 1}")
    if year_max:
        filters.append(f"publication_year:<{year_max + 1}")
    if filters:
        params["filter"] = ",".join(filters)

    resp = httpx.get(
        "https://api.openalex.org/works",
        params=params,
        timeout=15.0,
    )
    resp.raise_for_status()

    results = []
    for p in resp.json().get("results", []):
        doi_raw = p.get("doi") or ""
        doi = doi_raw.replace("https://doi.org/", "").replace("http://doi.org/", "") or None
        oa_id = p.get("id", "").replace("https://openalex.org/", "")
        authors = [
            a.get("author", {}).get("display_name")
            for a in (p.get("authorships") or [])[:5]
            if a.get("author", {}).get("display_name")
        ]
        abstract = _reconstruct_abstract(p.get("abstract_inverted_index"))
        results.append({
            "title": p.get("display_name"),
            "authors": authors,
            "year": p.get("publication_year"),
            "doi": doi,
            "arxiv_id": None,
            "openalex_id": oa_id,
            "cited_by_count": p.get("cited_by_count"),
            "abstract_snippet": abstract[:300],
            "_source": "openalex",
        })
    return results


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
    """Search academic databases for papers. Does NOT add to Zotero.
    Use ingest_papers to add selected results to your library.

    Tries Semantic Scholar first; falls back to OpenAlex automatically on rate-limit (429)
    or timeout. Both return the same result shape."""
    config = _get_config()

    # --- Semantic Scholar ---
    params: dict = {
        "query": query,
        "limit": limit,
        "fields": "title,authors,year,externalIds,citationCount,abstract",
        "sort": sort_by,
    }
    if year_min or year_max:
        lo = str(year_min) if year_min else ""
        hi = str(year_max) if year_max else ""
        params["publicationDateOrYear"] = f"{lo}-{hi}"

    headers = {}
    if config.semantic_scholar_api_key:
        headers["x-api-key"] = config.semantic_scholar_api_key

    s2_error: str | None = None
    try:
        resp = httpx.get(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            params=params,
            headers=headers,
            timeout=15.0,
        )
        if resp.status_code == 429:
            s2_error = "rate_limited"
        else:
            resp.raise_for_status()
            papers = resp.json().get("data", [])
            return [
                {
                    "title": p.get("title"),
                    "authors": [a.get("name") for a in (p.get("authors") or [])[:5]],
                    "year": p.get("year"),
                    "doi": (p.get("externalIds") or {}).get("DOI"),
                    "arxiv_id": (p.get("externalIds") or {}).get("ArXiv"),
                    "s2_id": p.get("paperId"),
                    "cited_by_count": p.get("citationCount"),
                    "abstract_snippet": (p.get("abstract") or "")[:300],
                    "_source": "semantic_scholar",
                }
                for p in papers
            ]
    except httpx.TimeoutException:
        s2_error = "timeout"
    except httpx.HTTPStatusError as e:
        s2_error = f"http_{e.response.status_code}"

    # --- OpenAlex fallback ---
    logger.info(f"S2 unavailable ({s2_error}), falling back to OpenAlex")
    try:
        return _search_openalex(query, limit, year_min, year_max, sort_by)
    except httpx.TimeoutException:
        raise ToolError(
            f"Academic search failed: Semantic Scholar ({s2_error}), OpenAlex (timeout). "
            "Try again later."
        )
    except httpx.HTTPStatusError as e:
        raise ToolError(
            f"Academic search failed: Semantic Scholar ({s2_error}), "
            f"OpenAlex (HTTP {e.response.status_code})."
        )
    except Exception as e:
        raise ToolError(
            f"Academic search failed: Semantic Scholar ({s2_error}), OpenAlex ({e})."
        )


@mcp.tool()
def ingest_papers(
    papers: Annotated[list[dict], Field(description=(
        "List of paper dicts, each with at least one of: doi, arxiv_id, s2_id. "
        "Typically from search_academic_databases results. Max 50 per call."
    ))],
    collection_key: Annotated[str | None, Field(description="Zotero collection key for all ingested papers")] = None,
    tags: Annotated[list[str] | None, Field(description="Tags to apply to all ingested papers")] = None,
    skip_duplicates: Annotated[bool, Field(description="Skip papers already in the library")] = True,
) -> dict:
    """Batch add papers to Zotero from search results.
    Each paper is processed independently — failures don't abort the batch."""
    if len(papers) > 50:
        raise ToolError(
            f"Batch size {len(papers)} exceeds maximum of 50. Split into smaller batches."
        )

    config = _get_config()
    warning = None
    if not config.semantic_scholar_api_key and len(papers) > 5:
        warning = (
            f"No S2_API_KEY configured. Estimated latency for {len(papers)} papers: "
            f"~{len(papers)}s (1 req/sec rate limit). "
            "Set S2_API_KEY environment variable for higher throughput."
        )

    results = []
    ingested = skipped = failed = 0

    for paper in papers:
        doi = paper.get("doi")
        arxiv_id = paper.get("arxiv_id")
        s2_id = paper.get("s2_id")

        if doi:
            identifier = doi
        elif arxiv_id:
            identifier = f"arxiv:{arxiv_id}"
        elif s2_id:
            identifier = s2_id
        else:
            results.append({"status": "failed", "error": "no usable identifier in paper dict"})
            failed += 1
            continue

        try:
            r = add_paper_by_identifier(identifier, collection_key, tags, attach_pdf=True)
            if r.get("duplicate") and skip_duplicates:
                skipped += 1
                results.append({
                    "identifier": identifier,
                    "status": "duplicate",
                    "existing_key": r.get("existing_key"),
                    "title": r.get("title"),
                })
            else:
                ingested += 1
                results.append({
                    "identifier": identifier,
                    "status": "ingested",
                    "item_key": r.get("item_key"),
                    "title": r.get("title"),
                    "pdf": r.get("pdf"),
                })
        except ToolError as e:
            failed += 1
            results.append({"identifier": identifier, "status": "failed", "error": str(e)})
        except Exception as e:
            failed += 1
            results.append({"identifier": identifier, "status": "failed", "error": str(e)})

    return {
        "total": len(papers),
        "ingested": ingested,
        "skipped_duplicates": skipped,
        "failed": failed,
        "warning": warning,
        "results": results,
    }


@mcp.tool()
def save_from_url(
    url: str,
    collection_key: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    """Save a paper from any publisher URL to Zotero via ZotPilot Connector.

    Opens the URL in the user's real browser (with institutional cookies),
    runs Zotero translators to extract metadata, downloads PDF, and saves to Zotero.

    Requires: ZotPilot Connector extension installed in Chrome.

    When collection_key and/or tags are provided, the tool attempts to place
    the saved item in the specified collection and/or apply the given tags.
    Routing is best-effort: if the item cannot be uniquely identified within
    30s of the save completing, a warning is returned instead.

    The bridge is auto-started if not already running.
    """
    import json
    import urllib.request

    bridge_url = f"http://127.0.0.1:{DEFAULT_PORT}"

    # Auto-start bridge if not running
    if not BridgeServer.is_running(DEFAULT_PORT):
        try:
            BridgeServer.auto_start(DEFAULT_PORT)
        except RuntimeError as e:
            return {"success": False, "error": str(e)}

    # POST command to bridge's /enqueue endpoint
    command = {
        "action": "save",
        "url": url,
        "collection_key": collection_key,
        "tags": tags or [],
    }
    try:
        req = urllib.request.Request(
            f"{bridge_url}/enqueue",
            data=json.dumps(command).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=5)
        body = json.loads(resp.read())
        # Bridge may return 503 if extension is not connected — surface immediately
        if "error_code" in body:
            return {"success": False, **body}
        request_id = body["request_id"]
    except urllib.error.HTTPError as e:
        # 503 from bridge means extension not connected
        if e.code == 503:
            try:
                err_body = json.loads(e.read())
                return {"success": False, **err_body}
            except Exception:
                return {
                    "success": False,
                    "error_code": "extension_not_connected",
                    "error_message": "ZotPilot Connector has not sent a heartbeat. Ensure it is installed and Chrome is open.",
                }
        return {"success": False, "error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        return {"success": False, "error": f"Failed to enqueue: {e}"}

    # Poll GET /result/<request_id> until result arrives or timeout
    deadline = time.monotonic() + 90.0
    while time.monotonic() < deadline:
        time.sleep(2)
        try:
            resp = urllib.request.urlopen(
                f"{bridge_url}/result/{request_id}", timeout=5
            )
            if resp.status == 200:
                result = json.loads(resp.read())
                return _apply_bridge_result_routing(result, collection_key, tags)
        except Exception:
            pass  # 204 or connection error — keep polling

    return {
        "success": False,
        "error": "Timeout (90s) — extension did not respond. "
                 "Ensure ZotPilot Connector is installed and Chrome is open.",
    }


@mcp.tool()
def save_urls(
    urls: Annotated[list[str], Field(description="URLs to save. Max 10 per call.")],
    collection_key: Annotated[str | None, Field(description="Zotero collection key for all saved items")] = None,
    tags: Annotated[list[str] | None, Field(description="Tags to apply to all saved items")] = None,
) -> dict:
    """Batch save multiple URLs to Zotero via ZotPilot Connector.

    Enqueues all URLs immediately (milliseconds each), then waits concurrently
    for all results. The extension processes them sequentially, so total time
    is roughly N × per-URL load time (~30s each).

    Requires: ZotPilot Connector installed in Chrome.
    Max 10 URLs per call.
    """
    import json
    import urllib.request

    if not urls:
        raise ToolError("urls list cannot be empty.")
    if len(urls) > 10:
        raise ToolError(f"Too many URLs ({len(urls)}). Max 10 per call — split into batches.")

    bridge_url = f"http://127.0.0.1:{DEFAULT_PORT}"

    if not BridgeServer.is_running(DEFAULT_PORT):
        try:
            BridgeServer.auto_start(DEFAULT_PORT)
        except RuntimeError as e:
            return {"success": False, "error": str(e), "results": []}

    # Enqueue all URLs sequentially (fast — each is a local HTTP POST)
    # id_to_url built at enqueue time to avoid ordering assumptions
    id_to_url: dict[str, str] = {}
    enqueue_errors: list[dict] = []
    for url in urls:
        command = {"action": "save", "url": url, "collection_key": collection_key, "tags": tags or []}
        try:
            req = urllib.request.Request(
                f"{bridge_url}/enqueue",
                data=json.dumps(command).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=5)
            body = json.loads(resp.read())
            if "error_code" in body:
                enqueue_errors.append({"url": url, "success": False, **body})
            else:
                id_to_url[body["request_id"]] = url
        except urllib.error.HTTPError as e:
            try:
                err_body = json.loads(e.read())
                enqueue_errors.append({"url": url, "success": False, **err_body})
            except Exception:
                enqueue_errors.append({"url": url, "success": False, "error": f"HTTP {e.code}"})
        except Exception as e:
            enqueue_errors.append({"url": url, "success": False, "error": str(e)})

    # Poll all request_ids concurrently using threads
    # Per-URL: 90s timeout. Overall hard cap: 300s.
    _PER_URL_TIMEOUT = 90.0
    _OVERALL_TIMEOUT = 300.0

    polled: dict[str, dict] = {}
    polled_lock = threading.Lock()

    def _poll_one(request_id: str, url: str) -> None:
        deadline = time.monotonic() + _PER_URL_TIMEOUT
        while time.monotonic() < deadline:
            time.sleep(2)
            try:
                resp = urllib.request.urlopen(
                    f"{bridge_url}/result/{request_id}", timeout=5
                )
                if resp.status == 200:
                    result = json.loads(resp.read())
                    final = _apply_bridge_result_routing(result, collection_key, tags)
                    with polled_lock:
                        polled[request_id] = {**final, "url": url}
                    return
            except Exception:
                pass
        with polled_lock:
            polled[request_id] = {
                "url": url,
                "success": False,
                "error": "Timeout (90s) — extension did not respond.",
            }

    threads = [
        threading.Thread(target=_poll_one, args=(rid, url), daemon=True)
        for rid, url in id_to_url.items()
    ]
    for t in threads:
        t.start()

    overall_deadline = time.monotonic() + _OVERALL_TIMEOUT
    for t in threads:
        remaining = overall_deadline - time.monotonic()
        if remaining > 0:
            t.join(timeout=remaining)

    all_results = enqueue_errors + [
        polled.get(rid, {"url": id_to_url[rid], "success": False, "error": "cancelled"})
        for rid in id_to_url
    ]

    succeeded = sum(1 for r in all_results if r.get("success") is True)
    failed = len(all_results) - succeeded

    return {
        "total": len(urls),
        "succeeded": succeeded,
        "failed": failed,
        "results": all_results,
    }


def _apply_bridge_result_routing(
    result: dict,
    collection_key: str | None,
    tags: list[str] | None,
) -> dict:
    """Apply collection/tag routing after a bridge save result, and always attempt
    to surface item_key for subsequent pipeline steps (index, note, classify).

    Extension result shape:
      {
        request_id, success, url,
        title?,        # always present (tab.title)
        item_key?,     # only in saveAsWebpage path (~5%)
        collection_key?, tags?, _detected_via?
        error_code?,   error_message?
      }
    """
    if not result.get("success"):
        # Save failed — propagate error, no routing possible
        return result

    config = _get_config()
    if not config.zotero_api_key:
        # No Web API key — cannot discover item_key or apply routing
        if collection_key or tags:
            return {
                **result,
                "warning": "collection_key and tags ignored — ZOTERO_API_KEY not configured",
            }
        return result

    writer = _get_writer()

    # Give Zotero desktop a moment to sync the new item to the web API.
    # Skip the wait when the connector already provided item_key (saveAsWebpage path, ~5%):
    # the known_key fast-path in _discover_saved_item_key returns immediately without an API call.
    if not result.get("item_key"):
        time.sleep(_ITEM_DISCOVERY_DELAY_S)

    # Always attempt item_key discovery — needed for subsequent pipeline steps
    # (index_library, create_note, add_to_collection) even when no routing requested.
    item_key = _discover_saved_item_key(
        title=result.get("title", ""),
        url=result.get("url", ""),
        known_key=result.get("item_key"),
        writer=writer,
    )

    out = {**result}
    if item_key:
        out["item_key"] = item_key  # surface to caller regardless of routing

    needs_routing = bool(collection_key) or bool(tags)
    if not needs_routing:
        return out

    if item_key is None:
        # Could not uniquely identify the saved item — count matches for better error message
        discovered = 0
        try:
            discovered = len(writer.find_items_by_url_and_title(
                result.get("url", ""), result.get("title", "")
            ))
        except Exception:
            pass
        if discovered == 0:
            warning = "collection_key/tags not applied — item not found in Zotero within discovery window"
        else:
            warning = f"collection_key/tags not applied — ambiguous match ({discovered} items found)"
        return {**out, "warning": warning}

    # Exactly one match — apply routing
    routing_warning = _apply_collection_tag_routing(
        item_key=item_key,
        collection_key=collection_key,
        tags=tags,
        writer=writer,
    )

    if routing_warning:
        out["warning"] = routing_warning
    return out

"""MCP tools for academic paper ingestion into Zotero."""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Annotated, Literal
from urllib.parse import urlparse

import httpx
from fastmcp.exceptions import ToolError
from pydantic import Field

from ..bridge import DEFAULT_PORT, BridgeServer
from ..state import _get_config, _get_resolver, _get_writer, _get_zotero, mcp, register_reset_callback

logger = logging.getLogger(__name__)

# Exponential backoff delays (seconds) for item discovery after bridge save.
_DISCOVERY_BACKOFF_DELAYS = [2.0, 4.0, 8.0]

# Window for item discovery: only consider items modified within this many seconds
# before the save completion timestamp.
_ITEM_DISCOVERY_WINDOW_S = 60

# Save polling windows: connector save + bridge-side item discovery can exceed 90s.
_SAVE_RESULT_POLL_TIMEOUT_S = 150.0
_SAVE_RESULT_POLL_OVERALL_TIMEOUT_S = 300.0
_SAVE_RESULT_POLL_PER_URL_BUDGET_S = 45.0
_SAVE_RESULT_POLL_OVERALL_GRACE_S = 120.0

# Routing retries smooth over local-SQLite vs Web API visibility lag after connector saves.
_ROUTING_RETRY_DELAYS_S = [0.0, 1.0, 2.0]

# Post-save PDF verification: Zotero may attach PDFs asynchronously after item creation.
# Delays are computed dynamically based on batch size — see _finalize_pdf_status.

# DOI-based dedup: track recently saved DOIs to prevent double-saves in slow batches.
# Process-scoped; lost on server restart (singletons reset via _reset_singletons).
_recently_saved_dois: dict[str, float] = {}  # doi (normalised, no prefix) -> save timestamp
_RECENT_SAVE_DEDUP_WINDOW_S = 300  # extended to cover slow batches
_writer_lock = threading.Lock()
_inbox_collection_key: str | None = None
_inbox_lock = threading.Lock()

register_reset_callback(lambda: _recently_saved_dois.clear())


def _clear_inbox_cache() -> None:
    global _inbox_collection_key
    _inbox_collection_key = None


register_reset_callback(_clear_inbox_cache)

# Anti-bot page title patterns (case-insensitive substring match).
_ANTI_BOT_TITLE_PATTERNS = [
    "just a moment",
    "请稍候",
    "请稍等",
    "verify you are human",
    "access denied",
    "please verify",
    "robot check",
    "cloudflare",
    "security check",
    "captcha",
    "checking your browser",
    "one more step",
    "page not found",
    "404",
    "not found",
]

# Error-page title patterns for post-save detection (startswith match, case-insensitive).
# More permissive than _ANTI_BOT_TITLE_PATTERNS — only used after a save completes,
# not during preflight (to avoid blocking legitimate paper titles like "Error Analysis...").
_ERROR_PAGE_TITLE_PATTERNS = [
    "page not found",
    "404 -",
    "404 ",
    "access denied",
    "subscription required",
    "unavailable -",
]

# Publisher suffixes that indicate Zotero translator fallback to webpage save.
_TRANSLATOR_FALLBACK_SUFFIXES = [
    " | cambridge core",
    " | springerlink",
    " | sciencedirect",
    " | wiley online library",
    " | taylor & francis",
    " | oxford academic",
    " | jstor",
    " | aip publishing",
    " | acs publications",
    " | ieee xplore",
    " | sage journals",
    " | mdpi",
    " | frontiers",
    " | pnas",
    " | nature",
    " | annual reviews",
]

_GENERIC_SITE_ONLY_TITLE_PATTERNS = [
    "| arxiv",
    "| arxiv.org",
    "| biorxiv",
    "| medrxiv",
]


def _compute_save_result_poll_timeout_s(batch_size: int) -> float:
    """Scale poll timeout with batch size to cover connector's sequential saves."""
    if batch_size <= 0:
        return _SAVE_RESULT_POLL_TIMEOUT_S
    return max(_SAVE_RESULT_POLL_TIMEOUT_S, batch_size * _SAVE_RESULT_POLL_PER_URL_BUDGET_S)


def _compute_save_result_poll_overall_timeout_s(batch_size: int) -> float:
    per_url_timeout = _compute_save_result_poll_timeout_s(batch_size)
    return max(_SAVE_RESULT_POLL_OVERALL_TIMEOUT_S, per_url_timeout + _SAVE_RESULT_POLL_OVERALL_GRACE_S)


def _looks_like_error_page_title(raw_title: str, item_key: str | None) -> bool:
    title = raw_title.strip().lower()
    if not title:
        return False
    if any(title.startswith(pattern) for pattern in _ERROR_PAGE_TITLE_PATTERNS):
        return True
    if item_key:
        return False
    return any(title.startswith(pattern) for pattern in _GENERIC_SITE_ONLY_TITLE_PATTERNS)


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

    last_error: Exception | None = None
    for attempt, delay in enumerate(_ROUTING_RETRY_DELAYS_S, start=1):
        if delay:
            time.sleep(delay)
        try:
            if collection_key:
                writer.add_to_collection(item_key, collection_key)
            if tags:
                writer.add_item_tags(item_key, tags)
            return None
        except Exception as e:
            last_error = e
            logger.warning(
                "Collection/tag routing failed for %s on attempt %s/%s: %s",
                item_key,
                attempt,
                len(_ROUTING_RETRY_DELAYS_S),
                e,
            )

    # Bridge-side routing is best-effort; the save itself succeeded.
    # Return a warning rather than failing the whole operation.
    return f"collection_key/tags partially applied — {last_error}"


def _extract_publisher_domain(url: str) -> str:
    """Normalize a URL to a publisher-ish domain for preflight sampling."""
    hostname = (urlparse(url).hostname or "").lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return hostname or url


def _sample_preflight_urls(urls: list[str], sample_size: int) -> tuple[list[str], list[str]]:
    """Pick up to sample_size URLs, favoring publisher diversity first."""
    if len(urls) <= sample_size:
        return list(urls), []

    grouped: dict[str, list[str]] = {}
    for url in urls:
        grouped.setdefault(_extract_publisher_domain(url), []).append(url)

    sample: list[str] = []
    selected: set[str] = set()
    groups = sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0]))

    for _, group_urls in groups:
        if len(sample) >= sample_size:
            break
        url = group_urls[0]
        sample.append(url)
        selected.add(url)

    if len(sample) < sample_size:
        max_group_size = max(len(group_urls) for _, group_urls in groups)
        for index in range(1, max_group_size):
            for _, group_urls in groups:
                if len(sample) >= sample_size:
                    break
                if index < len(group_urls):
                    url = group_urls[index]
                    if url not in selected:
                        sample.append(url)
                        selected.add(url)

    skipped = [url for url in urls if url not in selected]
    return sample, skipped


def _preflight_urls(urls: list[str], sample_size: int = 5) -> dict:
    """Probe URL accessibility via connector tabs before attempting saves."""
    import json
    import urllib.request

    if not urls:
        return {
            "checked": 0,
            "accessible": [],
            "blocked": [],
            "skipped": [],
            "errors": [],
            "all_clear": True,
        }

    sample, skipped_urls = _sample_preflight_urls(urls, sample_size)
    if skipped_urls:
        logger.info(
            "Preflight sampling: checking %s of %s URLs (%s unique publishers)",
            len(sample),
            len(urls),
            len({_extract_publisher_domain(url) for url in urls}),
        )

    report = {
        "checked": len(sample),
        "accessible": [],
        "blocked": [],
        "skipped": [{"url": url, "reason": "sampling"} for url in skipped_urls],
        "errors": [],
        "all_clear": True,
    }

    bridge_url = f"http://127.0.0.1:{DEFAULT_PORT}"
    if not BridgeServer.is_running(DEFAULT_PORT):
        try:
            BridgeServer.auto_start(DEFAULT_PORT)
        except RuntimeError as e:
            report["errors"] = [{"url": url, "error": str(e)} for url in sample]
            report["all_clear"] = False
            return report

    id_to_url: dict[str, str] = {}
    for url in sample:
        command = {"action": "preflight", "url": url}
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
                report["errors"].append({
                    "url": url,
                    "error": body.get("error_message") or body["error_code"],
                    "error_code": body["error_code"],
                })
            else:
                id_to_url[body["request_id"]] = url
        except urllib.error.HTTPError as e:
            try:
                err_body = json.loads(e.read())
                report["errors"].append({
                    "url": url,
                    "error": err_body.get("error_message") or f"HTTP {e.code}",
                    "error_code": err_body.get("error_code"),
                })
            except Exception:
                report["errors"].append({"url": url, "error": f"HTTP {e.code}"})
        except Exception as e:
            report["errors"].append({"url": url, "error": str(e)})

    _PER_URL_TIMEOUT = 60.0
    _OVERALL_TIMEOUT = 180.0

    polled: dict[str, dict] = {}
    polled_lock = threading.Lock()

    def _poll_one(request_id: str, url: str) -> None:
        deadline = time.monotonic() + _PER_URL_TIMEOUT
        while time.monotonic() < deadline:
            time.sleep(2)
            try:
                resp = urllib.request.urlopen(f"{bridge_url}/result/{request_id}", timeout=5)
                if resp.status != 200:
                    continue
                result = json.loads(resp.read())
                if result.get("status") in {"pending", "queued", "processing"}:
                    continue
                with polled_lock:
                    polled[request_id] = result
                return
            except Exception as e:
                logger.debug("Preflight poll %s: %s", request_id, e)
        with polled_lock:
            polled[request_id] = {
                "status": "error",
                "url": url,
                "error": "Timeout (60s) — page did not finish loading in time.",
            }

    threads = [
        threading.Thread(target=_poll_one, args=(request_id, url), daemon=True)
        for request_id, url in id_to_url.items()
    ]
    for thread in threads:
        thread.start()

    overall_deadline = time.monotonic() + _OVERALL_TIMEOUT
    for thread in threads:
        remaining = overall_deadline - time.monotonic()
        if remaining > 0:
            thread.join(timeout=remaining)

    for request_id, url in id_to_url.items():
        result = polled.get(request_id, {
            "status": "error",
            "url": url,
            "error": "Timeout (120s) — preflight did not complete.",
        })
        status = result.get("status")
        if status == "accessible":
            report["accessible"].append({
                "url": url,
                "title": result.get("title", ""),
                "final_url": result.get("final_url", url),
            })
        elif status == "anti_bot_detected":
            report["blocked"].append({
                "url": url,
                "title": result.get("title", ""),
                "final_url": result.get("final_url", url),
            })
        else:
            error_entry = {
                "url": url,
                "error": (
                    result.get("error")
                    or result.get("error_message")
                    or "unknown preflight error"
                ),
            }
            if result.get("title"):
                error_entry["title"] = result["title"]
            report["errors"].append(error_entry)

    report["all_clear"] = not report["blocked"] and not report["errors"]
    return report


def _summarize_preflight_report(report: dict, verbose_preflight: bool) -> dict:
    """Return a compact preflight envelope unless full arrays are requested."""
    summarized = {
        "checked": report.get("checked", 0),
        "all_clear": report.get("all_clear", False),
        "blocked": report.get("blocked", []),
        "errors": report.get("errors", []),
        "accessible_count": len(report.get("accessible", [])),
        "skipped_count": len(report.get("skipped", [])),
    }
    if verbose_preflight:
        summarized["accessible"] = report.get("accessible", [])
        summarized["skipped"] = report.get("skipped", [])
    return summarized


def _discover_saved_item_key(
    title: str,
    url: str,
    known_key: str | None,
    writer,
    window_s: int = _ITEM_DISCOVERY_WINDOW_S,
) -> str | None:
    """Best-effort item key discovery.

    Strategy:
    - If known_key is available (saveAsWebpage path, ~5% of saves), use it directly.
    - Otherwise: query Zotero for items added within window_s seconds matching
      title. Items with a URL field are additionally filtered by URL match;
      items with no URL field (most journal articles) are accepted as-is.
    - If exactly one match: use it.
    - If zero or multiple matches: return None (caller returns warning).

    This is inherently unreliable under concurrent saves or duplicate titles,
    and may miss real matches when titles differ slightly, multiple candidates
    exist, or the item falls outside the search window. Phase 2 will replace
    this with a correlation ID flowing end-to-end.
    """
    if known_key:
        return known_key

    if not title and not url:
        return None

    try:
        items = writer.find_items_by_url_and_title(url, title, window_s=window_s)
    except Exception as e:
        logger.warning(f"Item discovery query failed: {e}")
        return None

    if len(items) == 1:
        return items[0]
    # Still no results or no title: give up
    return None


# RETIRED as MCP tool (v0.4.1): Connector path (save_urls) provides better PDF acquisition
# via the browser + Zotero translator. Kept as internal helper for potential future use.
# To restore as MCP tool, re-add @mcp.tool() decorator.
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

    # Enrich oa_url from OpenAlex when CrossRef didn't provide one
    if attach_pdf and metadata.doi and not metadata.oa_url and not metadata.arxiv_id:
        metadata.oa_url = _enrich_oa_url(metadata.doi)

    if metadata.doi:
        with _writer_lock:
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


def _enrich_oa_url(doi: str) -> str | None:
    """Query OpenAlex by DOI to get an open-access PDF URL.

    Returns the oa_url string if available, or None on any error or missing data.
    Non-fatal: callers should proceed normally when this returns None.
    """
    try:
        resp = httpx.get(
            f"https://api.openalex.org/works/doi:{doi}",
            params={"select": "open_access"},
            timeout=8.0,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        oa = resp.json().get("open_access") or {}
        return oa.get("oa_url")
    except Exception as e:
        logger.warning("OpenAlex OA enrichment failed for doi:%s — %s", doi, e)
        return None


def _reconstruct_abstract(inverted_index: dict | None) -> str:
    """Reconstruct plain-text abstract from OpenAlex inverted index format."""
    if not inverted_index:
        return ""
    words: dict[int, str] = {}
    for word, positions in inverted_index.items():
        for pos in positions:
            words[pos] = word
    return " ".join(words[i] for i in sorted(words))


def _is_doi_query(query: str) -> str | None:
    """Return cleaned DOI for DOI-like queries, else None."""
    cleaned = query.strip()
    lowered = cleaned.lower()
    if lowered.startswith("doi:"):
        cleaned = cleaned[4:].strip()
    elif lowered.startswith("https://doi.org/"):
        cleaned = cleaned[len("https://doi.org/"):].strip()
    elif lowered.startswith("http://doi.org/"):
        cleaned = cleaned[len("http://doi.org/"):].strip()

    return cleaned if re.match(r"^10\.\d{4,}/\S+$", cleaned) else None


def _normalize_doi(doi: str | None) -> str | None:
    """Return a normalized DOI without scheme/prefix, or None if invalid."""
    if not doi:
        return None
    return _is_doi_query(doi if doi.lower().startswith(("doi:", "http://", "https://")) else f"doi:{doi}")


_OA_ARXIV_PREFIX = "https://doi.org/10.48550/arxiv."
_INBOX_COLLECTION_NAME = "INBOX"


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

        config = _get_config()
        if not config.zotero_api_key:
            return None

        try:
            collections = writer._zot.collections()
            for coll in collections:
                data = coll.get("data", {})
                if data.get("name") == _INBOX_COLLECTION_NAME:
                    _inbox_collection_key = data.get("key") or coll.get("key")
                    return _inbox_collection_key

            resp = writer._zot.create_collections([{"name": _INBOX_COLLECTION_NAME}])
            if resp and "successful" in resp:
                for val in resp["successful"].values():
                    _inbox_collection_key = val.get("key") or val.get("data", {}).get("key")
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


def _format_openalex_paper(p: dict) -> dict:
    """Format a single OpenAlex work dict into ZotPilot's result format."""
    doi_raw = p.get("doi") or ""
    formatted_doi = doi_raw.replace("https://doi.org/", "").replace("http://doi.org/", "") or None
    oa_id = p.get("id", "").replace("https://openalex.org/", "")
    authors = [
        a.get("author", {}).get("display_name")
        for a in (p.get("authorships") or [])[:5]
        if a.get("author", {}).get("display_name")
    ]
    abstract = _reconstruct_abstract(p.get("abstract_inverted_index"))

    ids = p.get("ids") or {}
    ids_doi = ids.get("doi") or ""
    arxiv_id = (
        ids_doi.lower()[len(_OA_ARXIV_PREFIX):]
        if ids_doi.lower().startswith(_OA_ARXIV_PREFIX.lower())
        else None
    )

    oa = p.get("open_access") or {}
    primary = p.get("primary_location") or {}
    source = primary.get("source") or {}

    return {
        "title": p.get("display_name"),
        "authors": authors,
        "year": p.get("publication_year"),
        "doi": formatted_doi,
        "arxiv_id": arxiv_id,
        "openalex_id": oa_id,
        "cited_by_count": p.get("cited_by_count"),
        "abstract_snippet": abstract[:300],
        "is_oa": oa.get("is_oa", False),
        "oa_url": oa.get("oa_url"),
        "landing_page_url": primary.get("landing_page_url"),
        "journal": source.get("display_name"),
        "publisher": source.get("host_organization_name"),
        "relevance_score": p.get("relevance_score"),
        "_source": "openalex",
    }


def _fetch_openalex_by_doi(doi: str, mailto: str) -> list[dict]:
    """Fetch a single OpenAlex work by DOI and format like search results."""
    resp = httpx.get(
        f"https://api.openalex.org/works/doi:{doi}",
        params={
            "select": (
                "id,doi,display_name,authorships,publication_year,"
                "cited_by_count,open_access,abstract_inverted_index,ids,primary_location"
            ),
            "mailto": mailto,
        },
        timeout=15.0,
    )
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    return [_format_openalex_paper(resp.json())]


def _search_openalex(
    query: str,
    limit: int,
    year_min: int | None,
    year_max: int | None,
    sort_by: str,
    mailto: str = "zotpilot@example.com",
) -> list[dict]:
    """Search OpenAlex API. Returns papers in the same format as S2 results."""
    sort_map = {
        "relevance": "relevance_score:desc",
        "publicationDate": "publication_date:desc",
    }
    author_filter: str | None = None
    search_query = query
    if query.lower().startswith("author:"):
        remainder = query[len("author:"):].strip()
        if not remainder:
            return []
        elif "|" in remainder:
            parts = remainder.split("|", 1)
            author_filter = parts[0].strip()
            search_query = parts[1].strip()
        else:
            author_filter = remainder
            search_query = ""

    params: dict = {
        "per-page": min(limit * 2, 200),
        "sort": sort_map.get(sort_by, "relevance_score:desc"),
        "select": (
            "id,doi,display_name,authorships,publication_year,"
            "cited_by_count,open_access,abstract_inverted_index,ids,primary_location,"
            "relevance_score"
        ),
        "mailto": mailto,
    }
    if search_query:
        params["search"] = search_query
    filters: list[str] = []
    if author_filter is not None:
        filters.append(f"raw_author_name.search:{author_filter}")
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

    results = [_format_openalex_paper(p) for p in resp.json().get("results", [])]
    # OpenAlex already returns results sorted by relevance_score desc; truncate to limit.
    # No client-side score threshold: the score scale varies by query (e.g. 17000 for
    # "machine learning" vs 300 for narrow terms), so a fixed ratio filter would silently
    # drop relevant papers for niche queries. per_page over-fetch is used only to give
    # citationCount re-sort a larger pool.
    results = results[:limit]
    if sort_by == "citationCount":
        # Approximation: re-sorts most-cited within the relevance-ranked pool,
        # not globally most-cited. Over-fetching (e.g., 3x per_page) was not chosen
        # because it adds ~3x API cost for marginal gain; the pool is already
        # relevance-filtered by OpenAlex, so top-cited within this set is a
        # reasonable proxy.
        results.sort(key=lambda r: r.get("cited_by_count") or 0, reverse=True)
    return results


def _search_s2(
    query: str,
    limit: int,
    year_min: int | None,
    year_max: int | None,
    sort_by: str,
    api_key: str | None,
) -> list[dict]:
    """Search Semantic Scholar API. Raises on failure."""
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
    if api_key:
        headers["x-api-key"] = api_key

    resp = httpx.get(
        "https://api.semanticscholar.org/graph/v1/paper/search",
        params=params,
        headers=headers,
        timeout=15.0,
    )
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


def _merge_oa_s2(oa_results: list[dict], s2_results: list[dict]) -> list[dict]:
    """Merge OpenAlex and S2 results, deduplicating by doi.lower()."""
    oa_by_doi = {r["doi"].lower(): dict(r) for r in oa_results if r.get("doi")}
    no_doi_results = [r for r in oa_results if not r.get("doi")]

    for s2_paper in s2_results:
        s2_doi = (s2_paper.get("doi") or "").lower()
        if s2_doi and s2_doi in oa_by_doi:
            # Enrich OpenAlex result with S2-specific fields
            oa_by_doi[s2_doi]["s2_id"] = s2_paper.get("s2_id")
            if not oa_by_doi[s2_doi].get("cited_by_count"):
                oa_by_doi[s2_doi]["cited_by_count"] = s2_paper.get("cited_by_count")
        else:
            # S2-only paper: add with OA defaults
            no_doi_results.append({
                **s2_paper,
                "is_oa": s2_paper.get("is_oa", False),
                "oa_url": s2_paper.get("oa_url"),
                "landing_page_url": s2_paper.get("landing_page_url"),
            })

    return list(oa_by_doi.values()) + no_doi_results


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

    Uses OpenAlex as primary source; Semantic Scholar as supplement when S2_API_KEY is set.
    Supports "author:Name" prefix for author-scoped search (use "author:Name | topic"
    for combined queries) and DOI strings for exact lookup."""
    config = _get_config()
    mailto = config.openalex_email or "zotpilot@example.com"

    detected_doi = _is_doi_query(query)
    if detected_doi:
        return _fetch_openalex_by_doi(detected_doi, mailto=mailto)

    # --- OpenAlex (primary) ---
    oa_error: str | None = None
    oa_results: list[dict] = []
    try:
        oa_results = _search_openalex(query, limit, year_min, year_max, sort_by, mailto=mailto)
    except httpx.TimeoutException:
        oa_error = "timeout"
    except httpx.HTTPStatusError as e:
        oa_error = f"http_{e.response.status_code}"
    except Exception as e:
        oa_error = str(e)

    if oa_error is None:
        # OpenAlex succeeded — optionally supplement with S2
        if config.semantic_scholar_api_key:
            try:
                s2_results = _search_s2(
                    query, limit, year_min, year_max, sort_by,
                    api_key=config.semantic_scholar_api_key,
                )
                return _merge_oa_s2(oa_results, s2_results)
            except Exception as e:
                logger.warning(f"S2 supplement failed ({e}), returning OpenAlex-only results")
        return oa_results

    # --- OpenAlex failed — try S2 as fallback ---
    logger.info(f"OpenAlex unavailable ({oa_error}), falling back to Semantic Scholar")
    if not config.semantic_scholar_api_key:
        raise ToolError(
            f"Academic search failed: OpenAlex ({oa_error}). "
            "No S2_API_KEY configured for fallback."
        )

    s2_error: str | None = None
    try:
        s2_results = _search_s2(
            query, limit, year_min, year_max, sort_by,
            api_key=config.semantic_scholar_api_key,
        )
        # Add OA defaults to S2-only results
        return [
            {
                **r,
                "is_oa": r.get("is_oa", False),
                "oa_url": r.get("oa_url"),
                "landing_page_url": r.get("landing_page_url"),
            }
            for r in s2_results
        ]
    except httpx.TimeoutException:
        s2_error = "timeout"
    except httpx.HTTPStatusError as e:
        s2_error = f"http_{e.response.status_code}"
    except Exception as e:
        s2_error = str(e)

    raise ToolError(
        f"Academic search failed: OpenAlex ({oa_error}), Semantic Scholar ({s2_error})."
    )


@mcp.tool()
def ingest_papers(
    papers: Annotated[list[dict] | str, Field(description=(
        "JSON array of paper dicts, each with at least one of: doi, arxiv_id, landing_page_url. "
        "Typically from search_academic_databases results. Max 50 per call. "
        "Example: [{\"doi\": \"10.1038/s41586-024-00001-0\", \"landing_page_url\": \"https://doi.org/10.1038/s41586-024-00001-0\"}]"
    ))],
    collection_key: Annotated[str | None, Field(description=(
        "Zotero collection key for all ingested papers. Defaults to INBOX."
    ))] = None,
    tags: Annotated[list[str] | str | None, Field(description=(
        'JSON array of tags to apply to all ingested papers, e.g. ["tag1","tag2"]'
    ))] = None,
    preflight: Annotated[bool, Field(description=(
        "Run accessibility preflight before saving. When blocked URLs are found, "
        "return a preflight report instead of saving. Default: True."
    ))] = True,
    verbose_preflight: Annotated[bool, Field(description=(
        "Include full accessible/skipped arrays in the preflight report."
    ))] = False,
    skip_duplicates: Annotated[bool, Field(description=(
        "Ignored when using Connector path — Zotero handles deduplication locally"
    ))] = True,
) -> dict:
    """Batch add papers to Zotero via ZotPilot Connector (browser-based save).
    Each paper is routed to save_urls by priority: arxiv_id > landing_page_url > doi.
    Papers without any usable identifier are skipped.
    Without collection_key, papers go to the INBOX collection (auto-created if absent)."""
    import json as _json
    if isinstance(papers, str):
        try:
            papers = _json.loads(papers)
        except Exception:
            raise ToolError("papers must be a JSON array of paper dicts")
    if isinstance(tags, str):
        try:
            tags = _json.loads(tags) if tags else None
        except Exception:
            tags = None
    if len(papers) > 50:
        raise ToolError(
            f"Batch size {len(papers)} exceeds maximum of 50. Split into smaller batches."
        )

    if collection_key is None:
        collection_key = _ensure_inbox_collection()
    resolved_collection_key = collection_key

    # Build URL list and identifier map in one pass; collect no-identifier failures eagerly
    results = []
    ingested = failed = skipped_duplicates = 0
    url_to_identifier: dict[str, str] = {}  # url → human-readable identifier for result mapping
    url_to_doi: dict[str, str] = {}  # url → normalized doi for recently-saved cache
    urls_to_save: list[str] = []
    save_candidates: list[dict[str, str | None]] = []

    now = time.time()
    stale_dois = [
        doi for doi, saved_at in _recently_saved_dois.items()
        if now - saved_at > _RECENT_SAVE_DEDUP_WINDOW_S
    ]
    for doi in stale_dois:
        _recently_saved_dois.pop(doi, None)

    for paper in papers:
        arxiv_id = paper.get("arxiv_id")
        doi = paper.get("doi")
        landing_url = paper.get("landing_page_url")

        # Priority routing: arxiv_id > landing_page_url > doi
        # NOTE: Always pass `landing_page_url` from search_academic_databases results.
        # For ScienceDirect/Elsevier and other publisher pages, doi.org redirects frequently
        # trigger anti-bot checks. Using the direct landing_page_url avoids this.
        if arxiv_id:
            url = f"https://arxiv.org/abs/{arxiv_id}"
        elif landing_url:
            url = landing_url
        elif doi:
            url = f"https://doi.org/{doi}"
        else:
            results.append({"status": "failed", "error": "no usable identifier"})
            failed += 1
            continue

        identifier = arxiv_id or doi or url
        url_to_identifier[url] = identifier
        normalized_doi = _normalize_doi(doi)
        if not normalized_doi and arxiv_id:
            normalized_doi = _normalize_doi(f"10.48550/arxiv.{arxiv_id}")
        if normalized_doi:
            url_to_doi[url] = normalized_doi
        urls_to_save.append(url)
        save_candidates.append({
            "url": url,
            "identifier": identifier,
            "title": paper.get("title"),
            "doi": paper.get("doi"),
            "arxiv_id": arxiv_id,
        })

    preflight_report = None
    if preflight and urls_to_save:
        full_preflight_report = _preflight_urls(urls_to_save)
        preflight_report = _summarize_preflight_report(full_preflight_report, verbose_preflight)
        if not full_preflight_report["all_clear"]:
            blocked = len(full_preflight_report["blocked"])
            errors = len(full_preflight_report["errors"])
            checked = full_preflight_report["checked"]
            issue_count = blocked + errors
            issue_label = (
                "blocked by anti-bot/access restrictions"
                if blocked and not errors
                else "blocked or errored during preflight"
            )
            return {
                "total": len(papers),
                "ingested": 0,
                "skipped_duplicates": 0,
                "failed": failed,
                "results": results,
                "preflight_report": preflight_report,
                "pdf_summary": {"attached": 0, "none": 0, "unknown": 0},
                "collection_used": resolved_collection_key,
                "ingest_complete": False,
                "message": (
                    f"{checked - issue_count} of {checked} URLs checked are accessible. "
                    f"{issue_count} URLs were {issue_label}. "
                    "Preflight found issues. Show the blocked and error URLs to the user and "
                    "wait for their decision before retrying."
                ),
            }

    dedup_results: list[dict] = []
    urls_needing_save: list[str] = []
    pdf_entries_by_key: dict[str, list[dict]] = {}

    def _finalize_pdf_status(entries_by_key: dict[str, list[dict]]) -> None:
        if not entries_by_key:
            return
        try:
            writer = _get_writer()
        except Exception:
            writer = None

        # Dynamic poll schedule: each paper needs ~10s for Zotero to download its PDF.
        # Poll every 5s; total budget = max(30s, n_papers * 10s), capped at 120s.
        n = len(entries_by_key)
        total_budget_s = min(max(30.0, n * 10.0), 120.0)
        poll_interval_s = 5.0
        n_polls = int(total_budget_s / poll_interval_s)

        pdf_status_by_key = {item_key: False for item_key in entries_by_key}
        if writer is not None:
            for item_key in list(pdf_status_by_key):
                try:
                    pdf_status_by_key[item_key] = writer.check_has_pdf(item_key)
                except Exception:
                    pass
            for _ in range(n_polls):
                if all(pdf_status_by_key.values()):
                    break
                time.sleep(poll_interval_s)
                for item_key, has_pdf in list(pdf_status_by_key.items()):
                    if has_pdf:
                        continue
                    try:
                        pdf_status_by_key[item_key] = writer.check_has_pdf(item_key)
                    except Exception:
                        pass

        for item_key, entries in entries_by_key.items():
            actual_has_pdf = pdf_status_by_key.get(item_key, False)
            for entry in entries:
                entry["pdf"] = "attached" if actual_has_pdf else "none"
                if not actual_has_pdf:
                    entry["warning"] = (
                        "PDF not attached. If Zotero showed a robot verification, "
                        "please complete it in Zotero and the PDF will download automatically. "
                        "Otherwise download the PDF manually and attach it in Zotero."
                    )

    try:
        dedup_writer = _get_writer()
    except Exception:
        dedup_writer = None

    if dedup_writer is not None:
        for candidate in save_candidates:
            url = candidate["url"] or ""
            identifier = candidate["identifier"] or url
            normalized_doi = _normalize_doi(candidate.get("doi"))
            # arxiv_id can also serve as a DOI via 10.48550/arxiv.{id}
            if not normalized_doi and candidate.get("arxiv_id"):
                normalized_doi = _normalize_doi(f"10.48550/arxiv.{candidate['arxiv_id']}")
            existing_item_key = None
            dedup_hit = False
            if normalized_doi:
                dedup_hit = normalized_doi in _recently_saved_dois
                if dedup_hit:
                    with _writer_lock:
                        existing_item_key = _discover_saved_item_key(
                            title=candidate["title"] or "",
                            url=url,
                            known_key=None,
                            writer=dedup_writer,
                            window_s=_RECENT_SAVE_DEDUP_WINDOW_S,
                        )
                else:
                    # Try local SQLite first — Zotero Web API q= search does not index DOI field
                    existing_item_key = None
                    try:
                        local_hits = _get_zotero().advanced_search(
                            [{"field": "doi", "op": "is", "value": normalized_doi}], limit=1
                        )
                        if local_hits:
                            existing_item_key = local_hits[0]["item_key"]
                    except Exception:
                        pass
                    if existing_item_key is None:
                        with _writer_lock:
                            existing_item_key = dedup_writer.check_duplicate_by_doi(normalized_doi)
                    dedup_hit = existing_item_key is not None
            else:
                with _writer_lock:
                    existing_item_key = _discover_saved_item_key(
                        title=candidate["title"] or "",
                        url=url,
                        known_key=None,
                        writer=dedup_writer,
                        window_s=_RECENT_SAVE_DEDUP_WINDOW_S,
                    )
                dedup_hit = existing_item_key is not None
            if dedup_hit:
                logger.warning(
                    "Dedup hit: skipping %s (item_key=%s, matched within %ds)",
                    url,
                    existing_item_key,
                    _RECENT_SAVE_DEDUP_WINDOW_S,
                )
                skipped_duplicates += 1
                entry = {
                    "identifier": identifier,
                    "status": "already_in_library",
                    "item_key": existing_item_key,
                    "title": candidate["title"],
                    "pdf": "unknown" if not existing_item_key else "none",
                    "url": url,
                }
                dedup_results.append(entry)
                if existing_item_key:
                    pdf_entries_by_key.setdefault(existing_item_key, []).append(entry)
            else:
                urls_needing_save.append(url)
    else:
        urls_needing_save = list(urls_to_save)

    # Batch call: save_urls enqueues all URLs concurrently, avoiding the heartbeat
    # timeout that occurs when serial single-URL calls block the extension for >30s.
    # save_urls caps at 10 URLs per call — chunk and merge when needed.
    _CHUNK_SIZE = 10
    if urls_needing_save:
        merged_results: list[dict] = []
        for i in range(0, len(urls_needing_save), _CHUNK_SIZE):
            chunk = urls_needing_save[i:i + _CHUNK_SIZE]
            chunk_result = save_urls(chunk, collection_key=collection_key, tags=tags)
            merged_results.extend(chunk_result.get("results") or [])
        batch_result = {"results": merged_results}
        for sub in batch_result.get("results") or []:
            url = sub.get("url", "")
            identifier = url_to_identifier.get(url, url)
            if sub.get("success"):
                ingested += 1
                item_key = sub.get("item_key")
                entry: dict = {
                    "identifier": identifier,
                    "status": "ingested",
                    "item_key": item_key,
                    "title": sub.get("title"),
                    "pdf": "none",
                    "url": url,
                }
                if item_key:
                    pdf_entries_by_key.setdefault(item_key, []).append(entry)
                normalized_doi = url_to_doi.get(url)
                if normalized_doi:
                    _recently_saved_dois[normalized_doi] = time.time()
                results.append(entry)
            elif sub.get("anti_bot_detected"):
                failed += 1
                results.append({
                    "identifier": identifier,
                    "status": "failed",
                    "anti_bot_detected": True,
                    "error": sub.get("error"),
                    "url": url,
                })
            elif sub.get("status") == "pending":
                # Batch was short-circuited by anti-bot on another URL; these
                # should be retried once the user completes Chrome verification.
                failed += 1
                results.append({
                    "identifier": identifier,
                    "status": "pending",
                    "error": sub.get("error"),
                    "url": url,
                })
            elif sub.get("status") == "timeout_likely_saved":
                failed += 1
                item_key = sub.get("item_key")
                if not item_key and dedup_writer is not None:
                    discovery_window_s = int(sub.get("poll_timeout_s") or _SAVE_RESULT_POLL_TIMEOUT_S) + 60
                    with _writer_lock:
                        item_key = _discover_saved_item_key(
                            title=sub.get("title", ""),
                            url=url,
                            known_key=None,
                            writer=dedup_writer,
                            window_s=discovery_window_s,
                        )
                entry = {
                    "identifier": identifier,
                    "status": "timeout_likely_saved",
                    "error": sub.get("error") or sub.get("error_message"),
                    "url": url,
                    "item_key": item_key,
                    "pdf": "unknown" if not item_key else "none",
                }
                if item_key:
                    pdf_entries_by_key.setdefault(item_key, []).append(entry)
                results.append(entry)
            elif sub.get("translator_fallback_detected") or sub.get("error_code") == "no_translator":
                failed += 1
                results.append({
                    "identifier": identifier,
                    "status": "failed",
                    "translator_fallback_detected": True,
                    "error": (
                        sub.get("error")
                        or sub.get("error_message")
                        or "No Zotero translator found. Retry after the page fully loads, "
                        "or open manually in Chrome."
                    ),
                    "url": url,
                })
            else:
                failed += 1
                error = sub.get("error") or sub.get("error_message") or "connector save failed"
                entry: dict = {"identifier": identifier, "status": "failed", "error": error, "url": url}
                if sub.get("error_code"):
                    entry["error_code"] = sub["error_code"]
                results.append(entry)

        results.extend(dedup_results)
        _finalize_pdf_status(pdf_entries_by_key)
    elif dedup_results:
        results.extend(dedup_results)
        _finalize_pdf_status(pdf_entries_by_key)

    pdf_summary = {"attached": 0, "none": 0, "unknown": 0}
    for entry in results:
        pdf_status = entry.get("pdf")
        if pdf_status == "attached":
            pdf_summary["attached"] += 1
        elif pdf_status == "none":
            pdf_summary["none"] += 1
        else:
            pdf_summary["unknown"] += 1

    missing_item_keys = [r for r in results if r.get("status") == "ingested" and not r.get("item_key")]
    has_unknown_pdf = pdf_summary["unknown"] > 0
    has_missing_pdf = pdf_summary["none"] > 0
    ingest_complete = ingested > 0 and not missing_item_keys and not has_unknown_pdf and not has_missing_pdf

    result = {
        "total": len(papers),
        "ingested": ingested,
        "skipped_duplicates": skipped_duplicates,
        "failed": failed,
        "results": results,
        "preflight_report": preflight_report,
        "pdf_summary": pdf_summary,
        "collection_used": resolved_collection_key,
        "ingest_complete": ingest_complete,
    }

    if not ingest_complete and (missing_item_keys or has_unknown_pdf or has_missing_pdf):
        blockers = []
        if missing_item_keys:
            blockers.append(
                f"{len(missing_item_keys)} items missing item_key — use advanced_search to locate"
            )
        if has_unknown_pdf:
            blockers.append(
                f"{pdf_summary['unknown']} items with unknown PDF status — use get_paper_details to verify"
            )
        if has_missing_pdf:
            blockers.append(
                f"{pdf_summary['none']} items saved without PDF — check robot verification in Zotero or attach PDF manually"
            )
        result["ingest_blockers"] = blockers

    if tags:
        result["tags_advisory"] = (
            "Tags applied at ingest time. For vocabulary-consistent tagging, "
            "prefer list_tags -> add_item_tags after indexing."
        )

    return result


@mcp.tool()
def save_from_url(
    url: str,
    collection_key: str | None = None,
    tags: Annotated[list[str] | str | None, Field(description="Tags to apply, as a list or JSON array string")] = None,
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

    # Coerce tags from JSON string if needed (Claude Code MCP client quirk)
    if isinstance(tags, str):
        try:
            tags = json.loads(tags) if tags else None
        except Exception:
            tags = None

    if collection_key is None:
        collection_key = _ensure_inbox_collection()
    resolved_collection_key = collection_key

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
                    "error_message": (
                        "ZotPilot Connector has not sent a heartbeat. "
                        "Ensure it is installed and Chrome is open."
                    ),
                }
        return {"success": False, "error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        return {"success": False, "error": f"Failed to enqueue: {e}"}

    # Poll GET /result/<request_id> until result arrives or timeout
    deadline = time.monotonic() + _SAVE_RESULT_POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        time.sleep(2)
        try:
            resp = urllib.request.urlopen(
                f"{bridge_url}/result/{request_id}", timeout=5
            )
            if resp.status == 200:
                result = json.loads(resp.read())
                routed = _apply_bridge_result_routing(result, collection_key, tags)
                routed["collection_used"] = resolved_collection_key
                return routed
        except Exception as e:
            logger.debug("Poll %s: %s", request_id, e)

    return {
        "success": False,
        "status": "timeout_likely_saved",
        "collection_used": resolved_collection_key,
        "error": (
            f"Timeout ({int(_SAVE_RESULT_POLL_TIMEOUT_S)}s) — the paper was likely saved but "
            "confirmation was not received in time. Check Zotero before retrying."
        ),
    }


@mcp.tool()
def save_urls(
    urls: Annotated[list[str] | str, Field(description="URLs to save. Max 10 per call.")],
    collection_key: Annotated[str | None, Field(description="Zotero collection key for all saved items")] = None,
    tags: Annotated[list[str] | str | None, Field(description="Tags to apply to all saved items")] = None,
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

    if isinstance(urls, str):
        try:
            urls = json.loads(urls)
        except Exception:
            raise ToolError("urls must be a JSON array of strings")
    if isinstance(tags, str):
        try:
            tags = json.loads(tags) if tags else None
        except Exception:
            tags = None

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

    # Poll all request_ids concurrently using threads.
    # Per-URL timeout scales with batch size because the connector processes
    # queued saves sequentially, while Python polls them concurrently.
    # Anti-bot short-circuit: the moment any URL hits an anti-bot page, a
    # cancel_event is set so all other threads stop immediately. The caller
    # receives successes so far + the blocking URL flagged as anti_bot_detected,
    # and remaining URLs as "pending" — ready for retry once the user clears the
    # verification in Chrome.
    per_url_timeout_s = _compute_save_result_poll_timeout_s(len(id_to_url))
    overall_timeout_s = _compute_save_result_poll_overall_timeout_s(len(id_to_url))
    polled: dict[str, dict] = {}
    polled_lock = threading.Lock()
    cancel_event = threading.Event()

    def _poll_one(request_id: str, url: str) -> None:
        deadline = time.monotonic() + per_url_timeout_s
        while time.monotonic() < deadline:
            if cancel_event.is_set():
                # Another URL hit anti-bot; mark this one as pending for retry.
                with polled_lock:
                    polled[request_id] = {
                        "url": url,
                        "success": False,
                        "status": "pending",
                        "error": (
                            "Skipped — another URL triggered anti-bot verification. "
                            "Wait for the user to complete Chrome verification before continuing."
                        ),
                    }
                return
            time.sleep(2)
            try:
                resp = urllib.request.urlopen(
                    f"{bridge_url}/result/{request_id}", timeout=5
                )
                if resp.status == 200:
                    result = json.loads(resp.read())
                    # Anti-bot detected by extension before saving (error_code set,
                    # no junk item created). Signal other threads to stop immediately.
                    if result.get("error_code") == "anti_bot_detected":
                        cancel_event.set()
                        logger.warning(
                            "Anti-bot page detected for %s (title: '%s'). "
                            "Please complete the verification in Chrome, then retry.",
                            url, result.get("title"),
                        )
                        with polled_lock:
                            polled[request_id] = {
                                "url": url,
                                "success": False,
                                "anti_bot_detected": True,
                                "error": result.get("error_message") or (
                                    f"Anti-bot page detected (title: '{result.get('title')}'). "
                                    "Wait for the user to complete the Chrome verification "
                                    "before continuing."
                                ),
                            }
                        return
                    final = _apply_bridge_result_routing(result, collection_key, tags)
                    with polled_lock:
                        polled[request_id] = {**final, "url": url}
                    return
            except Exception as e:
                logger.debug("Poll %s: %s", request_id, e)
        with polled_lock:
            polled[request_id] = {
                "url": url,
                "success": False,
                "status": "timeout_likely_saved",
                "poll_timeout_s": int(per_url_timeout_s),
                "error": (
                    f"Timeout ({int(per_url_timeout_s)}s) — the paper was likely saved but "
                    "confirmation was not received in time. Check Zotero before retrying."
                ),
            }

    threads = [
        threading.Thread(target=_poll_one, args=(rid, url), daemon=True)
        for rid, url in id_to_url.items()
    ]
    for t in threads:
        t.start()

    overall_deadline = time.monotonic() + overall_timeout_s
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

    # Error-page detection: title starts with known error-page prefix → save was useless.
    raw_title = result.get("title", "")
    title = raw_title.lower()
    item_key = result.get("item_key")
    if _looks_like_error_page_title(raw_title, item_key):
        try:
            writer = _get_writer()
        except Exception:
            writer = None
        if item_key:
            if writer is not None and hasattr(writer, "delete_item"):
                try:
                    writer.delete_item(item_key)
                except Exception:
                    import logging
                    logging.getLogger(__name__).warning(
                        "Failed to delete error-page item %s", item_key
                    )
        elif writer is not None:
            with _writer_lock:
                discovered_key = _discover_saved_item_key(
                    title=result.get("title", ""),
                    url=result.get("url", ""),
                    known_key=None,
                    writer=writer,
                    window_s=60,
                )
            if discovered_key:
                try:
                    writer.delete_item(discovered_key)
                except Exception:
                    logger.warning("Failed to delete discovered error-page item %s", discovered_key)
            else:
                logger.warning(
                    "Error page detected but item deletion could not be confirmed (title=%r, url=%r)",
                    raw_title,
                    result.get("url", ""),
                )
        else:
            logger.warning(
                "Error page detected but writer unavailable for cleanup (title=%r, url=%r)",
                raw_title,
                result.get("url", ""),
            )
        return {
            **result,
            "success": False,
            "error_code": "error_page_detected",
            "error": (
                f"Error page saved instead of paper (title: '{raw_title}'). "
                "The URL returned an error page. Try a different URL or add the paper manually."
            ),
        }

    # Translator fallback detection: publisher suffix in title means webpage saved instead of paper.
    if any(title.endswith(suffix) for suffix in _TRANSLATOR_FALLBACK_SUFFIXES):
        return {
            **result,
            "success": False,
            "translator_fallback_detected": True,
            "error": (
                f"Translator fallback detected (title: '{result.get('title')}'). "
                "Zotero captured the webpage instead of the paper. "
                "Retry with a different URL, or manually add the paper in Zotero."
            ),
        }

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

    # Discover item_key with exponential backoff (fast path: connector already provided key).
    if not result.get("item_key"):
        item_key = None
        for delay in _DISCOVERY_BACKOFF_DELAYS:
            time.sleep(delay)
            with _writer_lock:
                item_key = _discover_saved_item_key(
                    title=result.get("title", ""),
                    url=result.get("url", ""),
                    known_key=None,
                    writer=writer,
                    window_s=_ITEM_DISCOVERY_WINDOW_S,
                )
            if item_key:
                break
    else:
        with _writer_lock:
            item_key = _discover_saved_item_key(
                title=result.get("title", ""),
                url=result.get("url", ""),
                known_key=result.get("item_key"),
                writer=writer,
                window_s=_ITEM_DISCOVERY_WINDOW_S,
            )

    out = {**result}
    if item_key:
        out["item_key"] = item_key  # surface to caller regardless of routing

    # Closed-loop verification: check actual itemType saved in Zotero.
    # If Zotero saved a "webpage" instead of a journal article, the translator
    # failed silently — delete the junk item and report the failure.
    _ACADEMIC_ITEM_TYPES = {
        "journalArticle", "conferencePaper", "preprint", "thesis",
        "book", "bookSection", "report", "magazineArticle", "newspaperArticle",
    }
    if item_key:
        saved_type = writer.get_item_type(item_key)
        if saved_type and saved_type not in _ACADEMIC_ITEM_TYPES:
            logger.warning(
                "Translator fallback confirmed via Zotero: item %s saved as '%s' (expected academic type). "
                "Deleting junk item.",
                item_key, saved_type,
            )
            writer.delete_item(item_key)
            return {
                **result,
                "success": False,
                "translator_fallback_detected": True,
                "saved_item_type": saved_type,
                "error": (
                    f"Zotero saved this as '{saved_type}' instead of a journal article — "
                    "translator did not recognise the page. The item has been deleted. "
                    "Retry with a different URL, or manually add the paper in Zotero."
                ),
            }

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

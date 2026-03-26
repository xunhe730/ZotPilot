"""MCP tools for academic paper ingestion into Zotero."""
from __future__ import annotations

import logging
import threading
import time
from typing import Annotated, Literal
from urllib.parse import urlparse

import httpx
from fastmcp.exceptions import ToolError
from pydantic import Field

from ..bridge import DEFAULT_PORT, BridgeServer
from ..state import _get_config, _get_resolver, _get_writer, mcp

logger = logging.getLogger(__name__)

# Exponential backoff delays (seconds) for item discovery after bridge save.
_DISCOVERY_BACKOFF_DELAYS = [2.0, 4.0, 8.0]

# Window for item discovery: only consider items modified within this many seconds
# before the save completion timestamp.
_ITEM_DISCOVERY_WINDOW_S = 60

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

    _PER_URL_TIMEOUT = 30.0
    _OVERALL_TIMEOUT = 120.0

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
                with polled_lock:
                    polled[request_id] = result
                return
            except Exception as e:
                logger.debug("Preflight poll %s: %s", request_id, e)
        with polled_lock:
            polled[request_id] = {"status": "error", "url": url, "error": "Timeout (30s) — extension did not respond."}

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
            error_entry = {"url": url, "error": result.get("error") or result.get("error_message") or "unknown preflight error"}
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

    This is inherently unreliable under concurrent saves or duplicate titles.
    Phase 2 will replace this with a correlation ID flowing end-to-end.
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
        "citationCount": "cited_by_count:desc",
        "publicationDate": "publication_date:desc",
    }
    params: dict = {
        "search": query,
        "per-page": min(limit, 200),
        "sort": sort_map.get(sort_by, "relevance_score:desc"),
        "select": (
            "id,doi,display_name,authorships,publication_year,"
            "cited_by_count,open_access,abstract_inverted_index,ids,primary_location"
        ),
        "mailto": mailto,
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

    arxiv_prefix = "https://doi.org/10.48550/arxiv."
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

        # Extract arxiv_id from ids.doi when it's an arXiv DOI
        ids = p.get("ids") or {}
        ids_doi = ids.get("doi") or ""
        arxiv_id = ids_doi.lower()[len(arxiv_prefix):] if ids_doi.lower().startswith(arxiv_prefix.lower()) else None

        # OA fields
        oa = p.get("open_access") or {}
        is_oa = oa.get("is_oa", False)
        oa_url = oa.get("oa_url")

        # Landing page
        primary = p.get("primary_location") or {}
        landing_page_url = primary.get("landing_page_url")
        source = primary.get("source") or {}

        results.append({
            "title": p.get("display_name"),
            "authors": authors,
            "year": p.get("publication_year"),
            "doi": doi,
            "arxiv_id": arxiv_id,
            "openalex_id": oa_id,
            "cited_by_count": p.get("cited_by_count"),
            "abstract_snippet": abstract[:300],
            "is_oa": is_oa,
            "oa_url": oa_url,
            "landing_page_url": landing_page_url,
            "journal": source.get("display_name"),
            "publisher": source.get("host_organization_name"),
            "_source": "openalex",
        })
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

    Uses OpenAlex as primary source; Semantic Scholar as supplement when S2_API_KEY is set."""
    config = _get_config()
    mailto = config.openalex_email or "zotpilot@example.com"

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

    # Build URL list and identifier map in one pass; collect no-identifier failures eagerly
    results = []
    ingested = failed = 0
    url_to_identifier: dict[str, str] = {}  # url → human-readable identifier for result mapping
    urls_to_save: list[str] = []

    for paper in papers:
        arxiv_id = paper.get("arxiv_id")
        doi = paper.get("doi")
        landing_url = paper.get("landing_page_url")

        # Priority routing: arxiv_id > landing_page_url > doi
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
        urls_to_save.append(url)

    preflight_report = None
    if preflight and urls_to_save:
        full_preflight_report = _preflight_urls(urls_to_save)
        preflight_report = _summarize_preflight_report(full_preflight_report, verbose_preflight)
        if not full_preflight_report["all_clear"]:
            blocked = len(full_preflight_report["blocked"])
            errors = len(full_preflight_report["errors"])
            checked = full_preflight_report["checked"]
            issue_count = blocked + errors
            issue_label = "blocked by anti-bot/access restrictions" if blocked and not errors else "blocked or errored during preflight"
            return {
                "total": len(papers),
                "ingested": 0,
                "skipped_duplicates": 0,
                "failed": failed,
                "results": results,
                "preflight_report": preflight_report,
                "message": (
                    f"{checked - issue_count} of {checked} URLs checked are accessible. "
                    f"{issue_count} URLs were {issue_label}. "
                    "Call ingest_papers again with preflight=False to proceed with accessible URLs only, "
                    "or resolve access issues first."
                ),
            }

    # Batch call: save_urls enqueues all URLs concurrently, avoiding the heartbeat
    # timeout that occurs when serial single-URL calls block the extension for >30s.
    # save_urls caps at 10 URLs per call — chunk and merge when needed.
    _CHUNK_SIZE = 10
    if urls_to_save:
        merged_results: list[dict] = []
        for i in range(0, len(urls_to_save), _CHUNK_SIZE):
            chunk = urls_to_save[i:i + _CHUNK_SIZE]
            chunk_result = save_urls(chunk, collection_key=collection_key, tags=tags)
            merged_results.extend(chunk_result.get("results") or [])
        batch_result = {"results": merged_results}
        for sub in batch_result.get("results") or []:
            url = sub.get("url", "")
            identifier = url_to_identifier.get(url, url)
            if sub.get("success"):
                ingested += 1
                item_key = sub.get("item_key")
                # Verify actual PDF status via Zotero Web API — do not trust extension's
                # pdf_failed flag alone. Zotero-side robot verification (e.g. Elsevier)
                # completes after extension already reported success, so pdf_failed may be
                # False even though no PDF was attached.
                actual_has_pdf = False
                if item_key:
                    try:
                        writer = _get_writer()
                        actual_has_pdf = writer.check_has_pdf(item_key)
                    except Exception:
                        pass
                pdf_status = "attached" if actual_has_pdf else "none"
                entry: dict = {
                    "identifier": identifier,
                    "status": "ingested",
                    "item_key": item_key,
                    "title": sub.get("title"),
                    "pdf": pdf_status,
                    "url": url,
                }
                if not actual_has_pdf:
                    entry["warning"] = (
                        "PDF not attached. If Zotero showed a robot verification, "
                        "please complete it in Zotero and the PDF will download automatically. "
                        "Otherwise download the PDF manually and attach it in Zotero."
                    )
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
            elif sub.get("translator_fallback_detected") or sub.get("error_code") == "no_translator":
                failed += 1
                results.append({
                    "identifier": identifier,
                    "status": "failed",
                    "translator_fallback_detected": True,
                    "error": sub.get("error") or sub.get("error_message") or "No Zotero translator found. Retry after the page fully loads, or open manually in Chrome.",
                    "url": url,
                })
            else:
                failed += 1
                error = sub.get("error") or sub.get("error_message") or "connector save failed"
                entry: dict = {"identifier": identifier, "status": "failed", "error": error, "url": url}
                if sub.get("error_code"):
                    entry["error_code"] = sub["error_code"]
                results.append(entry)

    return {
        "total": len(papers),
        "ingested": ingested,
        "skipped_duplicates": 0,
        "failed": failed,
        "results": results,
        "preflight_report": preflight_report,
    }


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
        except Exception as e:
            logger.debug("Poll %s: %s", request_id, e)

    return {
        "success": False,
        "error": "Timeout (90s) — extension did not respond. "
                 "Ensure ZotPilot Connector is installed and Chrome is open.",
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
    # Per-URL: 90s timeout. Overall hard cap: 300s.
    # Anti-bot short-circuit: the moment any URL hits an anti-bot page, a
    # cancel_event is set so all other threads stop immediately. The caller
    # receives successes so far + the blocking URL flagged as anti_bot_detected,
    # and remaining URLs as "pending" — ready for retry once the user clears the
    # verification in Chrome.
    _PER_URL_TIMEOUT = 90.0
    _OVERALL_TIMEOUT = 300.0

    polled: dict[str, dict] = {}
    polled_lock = threading.Lock()
    cancel_event = threading.Event()

    def _poll_one(request_id: str, url: str) -> None:
        deadline = time.monotonic() + _PER_URL_TIMEOUT
        while time.monotonic() < deadline:
            if cancel_event.is_set():
                # Another URL hit anti-bot; mark this one as pending for retry.
                with polled_lock:
                    polled[request_id] = {
                        "url": url,
                        "success": False,
                        "status": "pending",
                        "error": "Skipped — another URL triggered anti-bot verification. Retry after completing the Chrome verification.",
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
                                    "Please complete the verification in Chrome, then retry."
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

    # Translator fallback detection: publisher suffix in title means webpage saved instead of paper.
    title = (result.get("title") or "").lower()
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

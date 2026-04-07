"""Search backends for ingestion-related academic discovery."""
from __future__ import annotations

import re

from ..openalex_client import OpenAlexClient

_OA_ARXIV_PREFIX = "https://doi.org/10.48550/arxiv."


def reconstruct_abstract(inverted_index: dict | None) -> str:
    """Reconstruct plain-text abstract from OpenAlex inverted index format."""
    if not inverted_index:
        return ""
    words: dict[int, str] = {}
    for word, positions in inverted_index.items():
        for pos in positions:
            words[pos] = word
    return " ".join(words[i] for i in sorted(words))


def is_doi_query(query: str) -> str | None:
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


def normalize_doi(doi: str | None) -> str | None:
    """Return a normalized lowercase DOI without scheme/prefix, or None if invalid."""
    if not doi:
        return None
    prefixed = doi if doi.lower().startswith(("doi:", "http://", "https://")) else f"doi:{doi}"
    result = is_doi_query(prefixed)
    return result.lower() if result else None


def format_openalex_paper(paper: dict) -> dict:
    """Format a single OpenAlex work dict into ZotPilot's result format."""
    doi_raw = paper.get("doi") or ""
    formatted_doi = doi_raw.replace("https://doi.org/", "").replace("http://doi.org/", "") or None
    oa_id = paper.get("id", "").replace("https://openalex.org/", "")
    authors = [
        author.get("author", {}).get("display_name")
        for author in (paper.get("authorships") or [])[:5]
        if author.get("author", {}).get("display_name")
    ]
    abstract = reconstruct_abstract(paper.get("abstract_inverted_index"))

    ids = paper.get("ids") or {}
    ids_doi = ids.get("doi") or ""
    arxiv_id = (
        ids_doi.lower()[len(_OA_ARXIV_PREFIX):]
        if ids_doi.lower().startswith(_OA_ARXIV_PREFIX.lower())
        else None
    )

    oa = paper.get("open_access") or {}
    primary = paper.get("primary_location") or {}
    source = primary.get("source") or {}
    summary_stats = source.get("summary_stats") or {}
    cited_by_count = int(paper.get("cited_by_count") or 0)
    venue_h_index = summary_stats.get("h_index")
    venue_two_yr_mean_citedness = summary_stats.get("2yr_mean_citedness")
    venue = {
        "display_name": source.get("display_name"),
        "h_index": venue_h_index,
        "two_yr_mean_citedness": venue_two_yr_mean_citedness,
    }

    return {
        "title": paper.get("display_name"),
        "authors": authors,
        "year": paper.get("publication_year"),
        "doi": formatted_doi,
        "arxiv_id": arxiv_id,
        "openalex_id": oa_id,
        "cited_by_count": cited_by_count,
        "is_retracted": bool(paper.get("is_retracted", False)),
        "type": paper.get("type"),
        "venue": venue,
        "top_venue": bool((venue_h_index is not None and venue_h_index >= 100) or cited_by_count >= 500),
        "abstract_snippet": abstract[:300],
        "is_oa": oa.get("is_oa", False),
        "oa_url": oa.get("oa_url"),
        "landing_page_url": primary.get("landing_page_url"),
        "journal": source.get("display_name"),
        "publisher": source.get("host_organization_name"),
        "relevance_score": paper.get("relevance_score"),
        "_source": "openalex",
    }


def fetch_openalex_by_doi(doi: str, client: OpenAlexClient) -> list[dict]:
    """Fetch a single OpenAlex work by DOI, with search fallback.

    Primary path: /works/doi:<doi> (canonical mapping).
    Fallback: when the canonical lookup 404s, search by the bare DOI string.
    OpenAlex sometimes stores a paper's alternative DOIs (arxiv preprint, ECCC,
    etc.) only in the ``ids`` field of a different canonical record. The
    search fallback can surface those records when direct lookup fails.
    """
    paper = client.get_work_details_by_doi(doi)
    if paper is not None:
        return [format_openalex_paper(paper)]

    # Fallback: search by the bare DOI as a quoted phrase. OpenAlex's search
    # index occasionally indexes alternative DOIs or DOI-like identifiers in
    # work metadata; this recovers ~5% of fuzzy DOI mismatches.
    try:
        papers = client.search_works(f'"{doi}"', per_page=3, high_quality=False)
    except Exception:
        return []
    if not papers:
        return []
    return [format_openalex_paper(papers[0])]


def search_openalex(
    query: str,
    limit: int,
    year_min: int | None,
    year_max: int | None,
    sort_by: str,
    *,
    client: OpenAlexClient,
    high_quality: bool = True,
) -> list[dict]:
    """Single-path keyword search via OpenAlex /works?search=<query>.

    Multi-angle recall and vocabulary normalization are the agent's
    responsibility per ztp-research SKILL §4 (WebFetch priming + multi-precise
    queries). The server only provides fast, dumb keyword retrieval and
    structured signals (venue, citation count, top_venue flag) for the agent
    to reason over.
    """
    sort_map = {
        "relevance": "relevance_score:desc",
        "publicationDate": "publication_date:desc",
        "citationCount": "cited_by_count:desc",
    }
    sort_value = sort_map.get(sort_by, "relevance_score:desc")

    papers = client.search_works(
        query,
        per_page=min(limit * 2, 200),
        high_quality=high_quality,
        year_min=year_min,
        year_max=year_max,
        sort=sort_value,
    )
    results = [format_openalex_paper(p) for p in papers]
    _mark_top_venue_relative(results)
    return results[:limit]


_AUTHOR_PREFIX_RE = re.compile(r"^\s*author\s*:", re.IGNORECASE)
_DOI_LIKE_RE = re.compile(r"\b10\.\d{4,9}/\S+", re.IGNORECASE)


def _is_fuzzy_nl_query(query: str) -> bool:
    if not query or not query.strip():
        return False
    if _AUTHOR_PREFIX_RE.match(query):
        return False
    if _DOI_LIKE_RE.search(query):
        return False
    if query.strip().lower().startswith(("doi:", "https://doi.org/", "http://doi.org/")):
        return False
    if '"' in query or " AND " in query or " OR " in query:
        return False
    return True


def _mark_top_venue_relative(results: list[dict], *, percentile: float = 0.75) -> None:
    """Re-stamp ``top_venue`` based on batch-relative citation percentile.

    Domain-aware: a fluid-mechanics paper with 100 cites is top-tier, while a
    CRISPR paper with 100 cites is mid-tier. Computing the threshold from the
    current result set adapts to the field. Falls back to the absolute
    threshold from ``format_openalex_paper`` when the batch is too small.
    """
    if len(results) < 5:
        return
    cites = sorted(r.get("cited_by_count") or 0 for r in results)
    idx = int(len(cites) * percentile)
    threshold = max(cites[min(idx, len(cites) - 1)], 10)
    for r in results:
        if (r.get("cited_by_count") or 0) >= threshold:
            r["top_venue"] = True


def search_academic_databases_impl(
    config,
    query: str,
    limit: int,
    year_min: int | None,
    year_max: int | None,
    sort_by: str,
    high_quality: bool,
    httpx_module,
    tool_error_cls,
    logger,
) -> list[dict]:
    """Shared implementation for academic search tool."""
    client = OpenAlexClient(email=config.openalex_email)

    detected_doi = is_doi_query(query)
    try:
        if detected_doi:
            results = fetch_openalex_by_doi(detected_doi, client=client)
        else:
            results = search_openalex(
                query,
                limit,
                year_min,
                year_max,
                sort_by,
                client=client,
                high_quality=high_quality,
            )
    except httpx_module.TimeoutException:
        error = "timeout"
    except httpx_module.HTTPStatusError as exc:
        error = f"http_{exc.response.status_code}"
    except Exception as exc:
        error = str(exc)
    else:
        if _is_fuzzy_nl_query(query) and sort_by == "relevance" and results:
            results[0] = {
                **results[0],
                "_warnings": [
                    {
                        "code": "missing_priming",
                        "message": (
                            "Fuzzy NL query detected without WebFetch priming. "
                            "Per ztp-research SKILL §4, you MUST WebFetch a "
                            "Wikipedia/survey URL first to extract anchor authors "
                            "and DOIs, then issue precise follow-up queries "
                            "(DOI direct, author:Name, phrase boolean). "
                            "Single-shot keyword search will miss seminal papers "
                            "with vocabulary-divergent titles."
                        ),
                    }
                ],
            }
        return results

    logger.info("OpenAlex search failed (%s)", error)
    raise tool_error_cls(f"Academic search failed: OpenAlex ({error}).")

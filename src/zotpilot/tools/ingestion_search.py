"""Search backends for ingestion-related academic discovery."""
from __future__ import annotations

import re

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
    """Return a normalized DOI without scheme/prefix, or None if invalid."""
    if not doi:
        return None
    prefixed = doi if doi.lower().startswith(("doi:", "http://", "https://")) else f"doi:{doi}"
    return is_doi_query(prefixed)


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

    return {
        "title": paper.get("display_name"),
        "authors": authors,
        "year": paper.get("publication_year"),
        "doi": formatted_doi,
        "arxiv_id": arxiv_id,
        "openalex_id": oa_id,
        "cited_by_count": paper.get("cited_by_count"),
        "abstract_snippet": abstract[:300],
        "is_oa": oa.get("is_oa", False),
        "oa_url": oa.get("oa_url"),
        "landing_page_url": primary.get("landing_page_url"),
        "journal": source.get("display_name"),
        "publisher": source.get("host_organization_name"),
        "relevance_score": paper.get("relevance_score"),
        "_source": "openalex",
    }


def fetch_openalex_by_doi(doi: str, mailto: str, http_get) -> list[dict]:
    """Fetch a single OpenAlex work by DOI and format like search results."""
    response = http_get(
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
    if response.status_code == 404:
        return []
    response.raise_for_status()
    return [format_openalex_paper(response.json())]


def search_openalex(
    query: str,
    limit: int,
    year_min: int | None,
    year_max: int | None,
    sort_by: str,
    http_get,
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
        if "|" in remainder:
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

    response = http_get(
        "https://api.openalex.org/works",
        params=params,
        timeout=15.0,
    )
    response.raise_for_status()

    results = [format_openalex_paper(p) for p in response.json().get("results", [])]
    results = results[:limit]
    if sort_by == "citationCount":
        results.sort(key=lambda result: result.get("cited_by_count") or 0, reverse=True)
    return results

def search_academic_databases_impl(
    config,
    query: str,
    limit: int,
    year_min: int | None,
    year_max: int | None,
    sort_by: str,
    httpx_module,
    tool_error_cls,
    logger,
) -> list[dict]:
    """Shared implementation for academic search tool."""
    mailto = config.openalex_email or "zotpilot@example.com"

    detected_doi = is_doi_query(query)
    if detected_doi:
        return fetch_openalex_by_doi(detected_doi, mailto=mailto, http_get=httpx_module.get)

    try:
        return search_openalex(query, limit, year_min, year_max, sort_by, httpx_module.get, mailto=mailto)
    except httpx_module.TimeoutException:
        error = "timeout"
    except httpx_module.HTTPStatusError as exc:
        error = f"http_{exc.response.status_code}"
    except Exception as exc:
        error = str(exc)

    logger.info("OpenAlex search failed (%s)", error)
    raise tool_error_cls(f"Academic search failed: OpenAlex ({error}).")

"""Search backends for ingestion-related academic discovery (v0.5.0).

Merged from:
- tools/ingestion_search.py — OpenAlex search, DOI normalization, result formatting
- docs/_migrating_functions.py — query building, dedup, local duplicate annotation
"""
from __future__ import annotations

import re
from typing import Any, Literal

from ...openalex_client import OpenAlexClient

_OA_ARXIV_PREFIX = "https://doi.org/10.48550/arxiv."


# ---------------------------------------------------------------------------
# DOI / abstract utilities
# ---------------------------------------------------------------------------

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


def normalize_arxiv_id(arxiv_id: str | None) -> str | None:
    """Return an arXiv ID without prefix or version suffix, else None."""
    if not arxiv_id:
        return None
    cleaned = arxiv_id.strip()
    if cleaned.lower().startswith("arxiv:"):
        cleaned = cleaned.split(":", 1)[1].strip()
    cleaned = re.sub(r"v\d+$", "", cleaned, flags=re.IGNORECASE)
    return cleaned or None


# ---------------------------------------------------------------------------
# OpenAlex formatting
# ---------------------------------------------------------------------------

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

    # OA enrichment — merge best_oa_location (covers preprint servers / repos)
    # so papers with arXiv preprints are not mis-reported as closed-access just
    # because the published version is paywalled. OpenAlex's own
    # `best_oa_location` is the authoritative cross-location OA pointer.
    best_oa = paper.get("best_oa_location") or {}
    best_oa_url = best_oa.get("pdf_url") or best_oa.get("landing_page_url")
    best_oa_source = best_oa.get("source") or {}
    best_oa_host = (
        best_oa_source.get("display_name")
        or best_oa_source.get("host_organization_name")
    )
    is_oa_published = bool(oa.get("is_oa", False))
    is_oa = is_oa_published or bool(best_oa_url)
    oa_url = oa.get("oa_url") or best_oa_url

    # Back-fill arxiv_id from best_oa_location when the primary DOI isn't
    # an arXiv DOI (e.g. CoCoOp published as CVPR with an arXiv preprint).
    if not arxiv_id and best_oa_url and "arxiv.org" in best_oa_url.lower():
        match = re.search(r"arxiv\.org/(?:abs|pdf)/([\w.\-]+?)(?:v\d+)?(?:\.pdf)?(?:[?#]|$)", best_oa_url)
        if match:
            arxiv_id = match.group(1)

    # Back-fill from any OpenAlex location (e.g. Location[1] = arXiv).
    # Many Springer/Elsevier papers have arXiv preprints listed only as
    # secondary locations, not in the primary DOI or best_oa_location.
    if not arxiv_id:
        for loc in paper.get("locations", []):
            for url_field in ("landing_page_url", "pdf_url"):
                loc_url = (loc.get(url_field) or "").lower()
                if "arxiv.org" in loc_url:
                    match = re.search(
                        r"arxiv\.org/(?:abs|pdf)/([\w.\-]+?)(?:v\d+)?(?:\.pdf)?(?:[?#]|$)",
                        loc_url,
                    )
                    if match:
                        arxiv_id = match.group(1)
                        break
            if arxiv_id:
                break

    publisher_name = source.get("host_organization_name") or ""
    landing_url = (primary.get("landing_page_url") or "").lower()
    # Publishers whose Zotero translator opens a user-interactive dialog
    # during save (e.g. Elsevier's "Continue" verification on ScienceDirect).
    # When such a candidate is ingested the user MUST click the dialog in
    # Zotero Desktop; re-triggering the ingest while the dialog is open
    # creates duplicate items.
    needs_manual_verification = bool(
        (formatted_doi and formatted_doi.startswith("10.1016/"))
        or "sciencedirect.com" in landing_url
        or "linkinghub.elsevier.com" in landing_url
        or "elsevier" in publisher_name.lower()
    )

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
        "top_venue": bool(
            (venue_h_index is not None and venue_h_index >= 100) or cited_by_count >= 500
        ),
        "abstract_snippet": abstract[:300],
        "is_oa": is_oa,
        "is_oa_published": is_oa_published,
        "oa_url": oa_url,
        "oa_host": best_oa_host if (not is_oa_published and best_oa_url) else None,
        "landing_page_url": primary.get("landing_page_url"),
        "journal": source.get("display_name"),
        "publisher": publisher_name or None,
        "needs_manual_verification": needs_manual_verification,
        "relevance_score": paper.get("relevance_score"),
        "_source": "openalex",
    }


def fetch_openalex_by_doi(doi: str, client: OpenAlexClient) -> list[dict]:
    """Fetch a single OpenAlex work by DOI, with search fallback."""
    paper = client.get_work_details_by_doi(doi)
    if paper is not None:
        return [format_openalex_paper(paper)]

    try:
        data = client.search_works(f'"{doi}"', per_page=3)
        papers = data.get("results", [])
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
    min_citations: int | None = None,
    concept_ids: list[str] | None = None,
    institution_ids: list[str] | None = None,
    source_id: str | None = None,
    oa_only: bool = False,
    cursor: str | None = None,
) -> dict:
    """OpenAlex /works?search= with full filter suite.

    Returns ``{"results": list[dict], "next_cursor": str | None, "total_count": int}``.
    """
    sort_map = {
        "relevance": "relevance_score:desc",
        "publicationDate": "publication_date:desc",
        "citationCount": "cited_by_count:desc",
    }
    sort_value = sort_map.get(sort_by, "relevance_score:desc")

    data = client.search_works(
        query,
        per_page=min(limit * 2, 200),
        min_citations=min_citations,
        concepts=concept_ids,
        institutions=institution_ids,
        source=source_id,
        oa_only=oa_only,
        year_min=year_min,
        year_max=year_max,
        sort=sort_value,
        cursor=cursor,
    )
    papers = data.get("results", [])
    formatted = [format_openalex_paper(p) for p in papers]
    _mark_top_venue_relative(formatted)
    return {
        "results": formatted[:limit],
        "next_cursor": data.get("next_cursor"),
        "total_count": data.get("total_count", 0),
    }


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
    """Re-stamp ``top_venue`` based on batch-relative citation percentile."""
    if len(results) < 5:
        return
    cites = sorted(r.get("cited_by_count") or 0 for r in results)
    idx = int(len(cites) * percentile)
    threshold = max(cites[min(idx, len(cites) - 1)], 10)
    for r in results:
        if (r.get("cited_by_count") or 0) >= threshold:
            r["top_venue"] = True


_FUZZY_REJECTION_MSG = (
    "Fuzzy bag-of-words query rejected. OpenAlex keyword search returns noisy "
    "results for unstructured queries (e.g. \"Chatting and cheating\" coming "
    "back for a VLM search). Two paths forward — pick based on how familiar "
    "you are with this topic:\n"
    "\n"
    "(A) You already know the canonical English term, seminal papers, venues, "
    "and OpenAlex concepts for this topic:\n"
    "  1. DOI direct      →  \"10.48550/arxiv.2103.00020\"\n"
    "  2. Author-anchored →  \"author:Radford CLIP\"\n"
    "  3. Quoted phrase   →  '\"visual instruction tuning\"'\n"
    "  4. Boolean combo   →  '\"LLaVA\" OR \"Flamingo\" OR \"GPT-4V\"'\n"
    "  Combine with filters: concepts=['Computer vision'], venue='CVPR', "
    "year_min=2023, min_citations=50.\n"
    "\n"
    "(B) Topic is unfamiliar (non-English input like '调研XX', niche/new "
    "term, ambiguous acronym, uncertain canonical spelling) — run "
    "reconnaissance FIRST, then retry this tool:\n"
    "  1. WebFetch one reference page to LEARN search vocabulary (not to "
    "read papers). Preferred URLs:\n"
    "     - https://en.wikipedia.org/wiki/<Topic>\n"
    "     - https://paperswithcode.com/search?q=<term>\n"
    "     - https://arxiv.org/list/<category>/<yyyy-mm>\n"
    "  2. Extract from the page: (a) canonical English term, "
    "(b) 2-3 seminal paper DOIs, (c) common venues, (d) related concept "
    "names.\n"
    "  3. Retry search_academic_databases with the learned vocabulary via "
    "the path (A) forms above.\n"
    "\n"
    "Do NOT ask the user to provide the search plan — if they knew the "
    "canonical terms they would not need your help. Do NOT retry with a "
    "slightly different bag-of-words query — the rejection is structural.\n"
    "\n"
    "Rejected query: {query!r}"
)


def _resolve_names_to_ids(
    client: OpenAlexClient,
    *,
    concepts: list[str] | None,
    institutions: list[str] | None,
    venue: str | None,
    logger,
) -> tuple[list[str], list[str], str | None, list[str]]:
    """Resolve human-readable names → OpenAlex IDs, reporting unresolved names."""
    concept_ids: list[str] = []
    institution_ids: list[str] = []
    source_id: str | None = None
    unresolved: list[str] = []

    if concepts:
        for name in concepts:
            cid = client.resolve_concept(name)
            if cid:
                concept_ids.append(cid.replace("https://openalex.org/", ""))
            else:
                unresolved.append(f"concept:{name}")
    if institutions:
        for name in institutions:
            iid = client.resolve_institution(name)
            if iid:
                institution_ids.append(iid.replace("https://openalex.org/", ""))
            else:
                unresolved.append(f"institution:{name}")
    if venue:
        sid = client.resolve_source(venue)
        if sid:
            source_id = sid.replace("https://openalex.org/", "")
        else:
            unresolved.append(f"venue:{venue}")

    if unresolved:
        logger.info("OpenAlex name resolution missed: %s", ", ".join(unresolved))
    return concept_ids, institution_ids, source_id, unresolved


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
    *,
    min_citations: int | None = None,
    oa_only: bool = False,
    concepts: list[str] | None = None,
    institutions: list[str] | None = None,
    venue: str | None = None,
    cursor: str | None = None,
    lookup_by_doi=None,
    lookup_by_arxiv_extra=None,
) -> dict:
    """Shared implementation for the academic search tool.

    Returns ``{"results": list[dict], "next_cursor": str | None,
    "total_count": int, "unresolved_filters": list[str]}``.

    Fuzzy bag-of-words queries are rejected ONLY when no structured filter
    (concepts/institutions/venue) is supplied — those filters narrow the search
    space enough that keyword-only queries become tolerable.
    """
    client = OpenAlexClient(email=config.openalex_email)

    detected_doi = is_doi_query(query) if query else None

    # DOI short-circuit: no filter resolution, no fuzzy check.
    if detected_doi:
        try:
            results = fetch_openalex_by_doi(detected_doi, client=client)
        except httpx_module.TimeoutException:
            raise tool_error_cls("Academic search failed: OpenAlex (timeout).")
        except httpx_module.HTTPStatusError as exc:
            raise tool_error_cls(
                f"Academic search failed: OpenAlex (http_{exc.response.status_code})."
            )
        return {
            "results": annotate_local_duplicates(
                results,
                lookup_by_doi=lookup_by_doi,
                lookup_by_arxiv_extra=lookup_by_arxiv_extra,
            ),
            "next_cursor": None,
            "total_count": len(results),
            "unresolved_filters": [],
        }

    # Resolve name-based filters first so we know whether structured context exists.
    concept_ids, institution_ids, source_id, unresolved = _resolve_names_to_ids(
        client,
        concepts=concepts,
        institutions=institutions,
        venue=venue,
        logger=logger,
    )
    has_structured_filter = bool(
        concept_ids or institution_ids or source_id
    )

    # Hard rejection of fuzzy bag-of-words queries WITHOUT structured filters.
    # Rationale: OpenAlex keyword search returns garbage for fuzzy queries, but
    # when the agent has narrowed by concept/institution/venue the keyword layer
    # is allowed to be loose. A soft warning in the result payload is routinely
    # ignored by agents, so the guardrail lives in code.
    if query and _is_fuzzy_nl_query(query) and not has_structured_filter:
        raise tool_error_cls(_FUZZY_REJECTION_MSG.format(query=query))

    try:
        payload = search_openalex(
            query or "",
            limit,
            year_min,
            year_max,
            sort_by,
            client=client,
            min_citations=min_citations,
            concept_ids=concept_ids or None,
            institution_ids=institution_ids or None,
            source_id=source_id,
            oa_only=oa_only,
            cursor=cursor,
        )
    except httpx_module.TimeoutException:
        raise tool_error_cls("Academic search failed: OpenAlex (timeout).")
    except httpx_module.HTTPStatusError as exc:
        raise tool_error_cls(
            f"Academic search failed: OpenAlex (http_{exc.response.status_code})."
        )
    except Exception as exc:
        logger.info("OpenAlex search failed (%s)", exc)
        raise tool_error_cls(f"Academic search failed: OpenAlex ({exc}).")

    payload["results"] = annotate_local_duplicates(
        payload.get("results", []),
        lookup_by_doi=lookup_by_doi,
        lookup_by_arxiv_extra=lookup_by_arxiv_extra,
    )
    payload["unresolved_filters"] = unresolved
    return payload


# ---------------------------------------------------------------------------
# URL classification helpers
# ---------------------------------------------------------------------------

_PDF_URL_RE = re.compile(
    r"(?:"
    r"\.pdf(?:[?#]|$)"
    r"|/pdf(?:[?/]|$)"
    r"|/content/pdf/"
    r"|pdf\.sciencedirect\.com"
    r")",
    re.IGNORECASE,
)
_DOI_REDIRECT_RE = re.compile(r"^https?://(?:dx\.)?doi\.org/10\.", re.IGNORECASE)


def is_pdf_or_doi_url(url: str | None) -> bool:
    """Return True if url is a direct PDF link or a doi.org redirect."""
    if not url:
        return False
    return bool(_PDF_URL_RE.search(url)) or bool(_DOI_REDIRECT_RE.match(url))


_LINKINGHUB_PII_RE = re.compile(
    r"^https?://linkinghub\.elsevier\.com/retrieve/pii/(S[0-9X]+)",
    re.IGNORECASE,
)


def normalize_landing_url(url: str) -> str:
    """Convert known intermediate redirectors to final landing pages."""
    m = _LINKINGHUB_PII_RE.match(url)
    if m:
        return f"https://www.sciencedirect.com/science/article/pii/{m.group(1)}"
    return url


def classify_ingest_candidate(
    paper: dict,
    normalized_doi: str | None,
    arxiv_id: str | None,
    landing_page_url: str | None,
) -> Literal["connector", "api", "reject"]:
    """Classify a paper candidate for routing."""
    if arxiv_id:
        return "connector"
    if landing_page_url and not is_pdf_or_doi_url(landing_page_url):
        return "connector"
    resolved_url = paper.get("_resolved_landing_url")
    if resolved_url and not is_pdf_or_doi_url(resolved_url):
        return "connector"
    if normalized_doi or (paper.get("doi") and not landing_page_url):
        return "api"
    return "reject"


# ---------------------------------------------------------------------------
# Migrated from research_workflow.py — query building and dedup helpers
# ---------------------------------------------------------------------------

def _normalize_title_key(title: str | None) -> str:
    if not title:
        return ""
    lowered = title.casefold()
    lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
    return " ".join(lowered.split())


def _infer_anchor_kind(query: str) -> str:
    if is_doi_query(query):
        return "doi"
    if query.strip().lower().startswith("author:"):
        return "author"
    return "phrase"


def build_structured_queries(
    *,
    query: str,
    request_class: Literal["known_item", "seminal_seed_set", "topic_survey"],
    anchors: list,
    strict_policy: bool,
    audit: dict[str, Any],
) -> tuple[list[dict[str, str]], Any]:
    """将搜索请求分解为精确子查询。"""
    structured_queries: list[dict[str, str]] = []

    if anchors:
        for anchor in anchors:
            normalized = anchor.normalized_query(topic=query)
            structured_queries.append({
                "label": anchor.source_label(topic=query),
                "query": normalized,
                "kind": anchor.kind,
            })
            if normalized != anchor.query.strip():
                audit["repaired_queries"].append({
                    "kind": anchor.kind,
                    "input": anchor.query,
                    "normalized": normalized,
                })
    elif not _is_fuzzy_nl_query(query):
        inferred_kind = _infer_anchor_kind(query)
        structured_queries.append({
            "label": inferred_kind,
            "query": query.strip(),
            "kind": inferred_kind,
        })

    if not strict_policy:
        return structured_queries or [{
            "label": "query",
            "query": query.strip(),
            "kind": _infer_anchor_kind(query),
        }], None

    if request_class == "known_item":
        if structured_queries:
            return structured_queries, None
        return [], None

    min_precise_queries = 1 if request_class == "seminal_seed_set" else 2
    if len(structured_queries) < min_precise_queries:
        return [], None
    return structured_queries, None


def paper_rank_tuple(paper: dict[str, Any]) -> tuple[int, int, float]:
    """论文排名元组（venue × citations）。"""
    return (
        int(paper.get("cited_by_count") or 0),
        int(bool(paper.get("top_venue"))),
        float(paper.get("relevance_score") or 0.0),
    )


def paper_dedup_key(paper: dict[str, Any]) -> tuple[str, str]:
    """论文去重 key（DOI 或 normalized title）。"""
    normalized_doi_val = normalize_doi(paper.get("doi"))
    if normalized_doi_val:
        return ("doi", normalized_doi_val)
    for field_name in ("openalex_id", "arxiv_id", "landing_page_url", "oa_url"):
        value = str(paper.get(field_name) or "").strip()
        if value:
            return (field_name, value)
    normalized_title = _normalize_title_key(paper.get("title"))
    if normalized_title:
        return ("title", normalized_title)
    fallback = paper.get("title") or paper.get("landing_page_url") or "candidate"
    return ("fallback", str(fallback))


def merge_search_hits(
    query_results: list[tuple[dict[str, str], list[dict[str, Any]]]],
    *,
    limit: int,
    audit: dict[str, Any],
) -> list[dict[str, Any]]:
    """合并多次搜索结果并去重。"""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    raw_hits = 0
    for query_info, papers in query_results:
        raw_hits += len(papers)
        for paper in papers:
            key = paper_dedup_key(paper)
            existing = merged.get(key)
            candidate = dict(paper)
            candidate_sources = list(
                dict.fromkeys([*paper.get("_sources", []), query_info["label"]])
            )
            candidate["_sources"] = candidate_sources
            candidate["_source"] = (
                candidate_sources[0] if len(candidate_sources) == 1 else "multi_query"
            )
            if existing is None:
                merged[key] = candidate
                continue
            combined_sources = list(
                dict.fromkeys([*existing.get("_sources", []), *candidate_sources])
            )
            better = (
                candidate
                if paper_rank_tuple(candidate) > paper_rank_tuple(existing)
                else existing
            )
            better = dict(better)
            better["_sources"] = combined_sources
            better["_source"] = (
                combined_sources[0] if len(combined_sources) == 1 else "multi_query"
            )
            merged[key] = better

    merged_results = sorted(merged.values(), key=paper_rank_tuple, reverse=True)
    audit["dedup_stats"] = {
        "raw_hits": raw_hits,
        "unique_candidates": len(merged_results),
        "duplicates_removed": max(raw_hits - len(merged_results), 0),
    }
    return merged_results[:limit]


def annotate_local_duplicate(
    result: dict[str, Any],
    *,
    lookup_by_doi,
    lookup_by_arxiv_extra,
) -> dict[str, Any]:
    """Annotate one search result with authoritative local duplicate state."""
    doi = normalize_doi(result.get("doi"))
    arxiv_id = normalize_arxiv_id(result.get("arxiv_id"))

    existing_item_key: str | None = None
    try:
        if doi and lookup_by_doi is not None:
            existing_item_key = lookup_by_doi(doi)
        if not existing_item_key and arxiv_id and lookup_by_doi is not None:
            existing_item_key = lookup_by_doi(f"10.48550/arxiv.{arxiv_id}")
        if not existing_item_key and arxiv_id and lookup_by_arxiv_extra is not None:
            existing_item_key = lookup_by_arxiv_extra(arxiv_id)
    except Exception:
        existing_item_key = None

    annotated = dict(result)
    annotated["local_duplicate"] = existing_item_key is not None
    annotated["existing_item_key"] = existing_item_key
    return annotated


def annotate_local_duplicates(
    papers: list[dict[str, Any]],
    *,
    lookup_by_doi,
    lookup_by_arxiv_extra,
) -> list[dict[str, Any]]:
    """Annotate search results with local duplicate state."""
    annotated: list[dict[str, Any]] = []
    for paper in papers:
        annotated.append(
            annotate_local_duplicate(
                paper,
                lookup_by_doi=lookup_by_doi,
                lookup_by_arxiv_extra=lookup_by_arxiv_extra,
            )
        )
    return annotated

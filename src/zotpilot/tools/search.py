"""Search tools: semantic, topic, boolean, tables, figures."""
import logging
import time
from collections import defaultdict
from dataclasses import replace
from typing import Annotated, Literal

from pydantic import Field

from ..filters import (
    VALID_CHUNK_TYPES,
    _apply_required_terms,
    _apply_text_filters,
    _build_chromadb_filters,
    _has_text_filters,
)
from ..reranker import validate_journal_weights, validate_section_weights
from ..result_utils import (
    _merge_results_by_chunk,
    _result_to_dict,
    _stored_chunk_to_retrieval_result,
)
from ..state import (
    ToolError,
    _get_config,
    _get_reranker,
    _get_retriever,
    _get_store,
    _get_zotero,
    mcp,
)

logger = logging.getLogger(__name__)


@mcp.tool()
def search_papers(
    query: Annotated[str, Field(description="Natural language search query")],
    top_k: Annotated[int, Field(description="Number of results", ge=1, le=50)] = 10,
    context_chunks: Annotated[int, Field(description="Adjacent chunks to include for context", ge=0, le=3)] = 0,
    year_min: Annotated[int | None, Field(description="Minimum publication year")] = None,
    year_max: Annotated[int | None, Field(description="Maximum publication year")] = None,
    author: Annotated[str | None, Field(description="Filter by author name (case-insensitive substring)")] = None,
    tag: Annotated[str | None, Field(description="Filter by Zotero tag (case-insensitive substring)")] = None,
    collection: Annotated[str | None, Field(description="Filter by collection name (substring)")] = None,
    chunk_types: Annotated[list[str] | None, Field(description="Content types to include: text, figure, table. Omit for all.")] = None,  # noqa: E501
    section_weights: Annotated[dict[str, float] | None, Field(description="Section relevance 0.0-1.0. Keys: abstract, introduction, background, methods, results, discussion, conclusion, references, appendix, preamble, table, unknown")] = None,  # noqa: E501
    journal_weights: Annotated[dict[str, float] | None, Field(description="Journal quartile weights 0.0-1.0. Keys: Q1, Q2, Q3, Q4, unknown")] = None,  # noqa: E501
    required_terms: Annotated[list[str] | None, Field(description="Words that must appear in passage (case-insensitive whole-word match)")] = None,  # noqa: E501
    verbosity: Annotated[Literal["minimal", "standard", "full"], Field(description="Response detail level")] = "minimal",
) -> list[dict]:
    """Semantic search over paper chunks. Returns passages ranked by composite score (similarity × section × journal). Use chunk_types for content type, section_weights for paper location, required_terms for exact keyword filtering."""  # noqa: E501
    start = time.perf_counter()

    # Validate chunk_types if provided
    if chunk_types is not None:
        invalid = set(chunk_types) - VALID_CHUNK_TYPES
        if invalid:
            raise ToolError(
                f"Invalid chunk_types: {invalid}. "
                f"Valid values: {', '.join(sorted(VALID_CHUNK_TYPES))}"
            )

    # Validate section_weights if provided
    if section_weights is not None:
        errors = validate_section_weights(section_weights)
        if errors:
            raise ToolError(f"Invalid section_weights: {'; '.join(errors)}")

    # Validate journal_weights if provided
    if journal_weights is not None:
        errors = validate_journal_weights(journal_weights)
        if errors:
            raise ToolError(f"Invalid journal_weights: {'; '.join(errors)}")

    retriever = _get_retriever()
    reranker = _get_reranker()
    _config = _get_config()

    queries = [query]

    # Oversample for reranking; increase if post-retrieval filters will reduce results
    base_fetch = min(top_k * _config.oversample_multiplier, 150)
    has_post_filters = _has_text_filters(author, tag, collection) or required_terms
    fetch_k = base_fetch * 3 if has_post_filters else base_fetch

    all_results = []
    for q in queries:
        r = retriever.search(
            query=q,
            top_k=fetch_k,
            context_window=min(context_chunks, 3),
            filters=_build_chromadb_filters(year_min, year_max, chunk_types)
        )
        r = _apply_text_filters(r, author, tag, collection)
        if required_terms:
            r = _apply_required_terms(r, required_terms)
        if _config.rerank_enabled:
            r = reranker.rerank(r, section_weights, journal_weights)
        else:
            r = [replace(x, composite_score=x.score) for x in r]
        all_results.append(r)

    if len(all_results) == 1:
        top_results = all_results[0][:min(top_k, 50)]
    else:
        top_results = _merge_results_by_chunk(all_results[0], all_results[1], min(top_k, 50))

    logger.debug(f"search_papers: {time.perf_counter() - start:.3f}s")
    return [_result_to_dict(r, verbosity=verbosity) for r in top_results]


@mcp.tool()
def search_topic(
    query: Annotated[str, Field(description="Natural language topic description")],
    num_papers: Annotated[int, Field(description="Number of distinct papers to return", ge=1, le=50)] = 10,
    year_min: Annotated[int | None, Field(description="Minimum publication year")] = None,
    year_max: Annotated[int | None, Field(description="Maximum publication year")] = None,
    author: Annotated[str | None, Field(description="Filter by author name (case-insensitive substring)")] = None,
    tag: Annotated[str | None, Field(description="Filter by Zotero tag (case-insensitive substring)")] = None,
    collection: Annotated[str | None, Field(description="Filter by collection name (substring)")] = None,
    chunk_types: Annotated[list[str] | None, Field(description="Content types to include: text, figure, table. Omit for all.")] = None,  # noqa: E501
    section_weights: Annotated[dict[str, float] | None, Field(description="Section relevance 0.0-1.0. Keys: abstract, introduction, background, methods, results, discussion, conclusion, references, appendix, preamble, table, unknown")] = None,  # noqa: E501
    journal_weights: Annotated[dict[str, float] | None, Field(description="Journal quartile weights 0.0-1.0. Keys: Q1, Q2, Q3, Q4, unknown")] = None,  # noqa: E501
    verbosity: Annotated[Literal["minimal", "standard", "full"], Field(description="Response detail level")] = "minimal",
) -> list[dict]:
    """Paper-level topic discovery. Returns one entry per paper sorted by avg composite score. Use for 'what do I have on X' surveys."""  # noqa: E501
    start = time.perf_counter()

    # Validate chunk_types if provided
    if chunk_types is not None:
        invalid = set(chunk_types) - VALID_CHUNK_TYPES
        if invalid:
            raise ToolError(
                f"Invalid chunk_types: {invalid}. "
                f"Valid values: {', '.join(sorted(VALID_CHUNK_TYPES))}"
            )

    # Validate section_weights if provided
    if section_weights is not None:
        errors = validate_section_weights(section_weights)
        if errors:
            raise ToolError(f"Invalid section_weights: {'; '.join(errors)}")

    # Validate journal_weights if provided
    if journal_weights is not None:
        errors = validate_journal_weights(journal_weights)
        if errors:
            raise ToolError(f"Invalid journal_weights: {'; '.join(errors)}")

    retriever = _get_retriever()
    reranker = _get_reranker()
    _config = _get_config()

    queries = [query]

    # Fetch more chunks than papers requested; double if text filters active
    base_fetch = min(
        num_papers * _config.oversample_topic_factor * _config.oversample_multiplier,
        600
    )
    fetch_k = base_fetch * 2 if _has_text_filters(author, tag, collection) else base_fetch

    all_chunks: list = []
    for q in queries:
        r = retriever.search(
            query=q,
            top_k=fetch_k,
            context_window=0,
            filters=_build_chromadb_filters(year_min, year_max, chunk_types)
        )
        r = _apply_text_filters(r, author, tag, collection)
        if _config.rerank_enabled:
            r = reranker.rerank(r, section_weights, journal_weights)
        else:
            r = [replace(x, composite_score=x.score) for x in r]
        all_chunks.extend(r)

    # Deduplicate by (doc_id, chunk_index), keep best composite score
    if len(queries) > 1:
        seen: dict[tuple, object] = {}
        for r in all_chunks:
            key = (r.doc_id, r.chunk_index)
            existing = seen.get(key)
            if existing is None:
                seen[key] = r
            else:
                r_score = r.composite_score if r.composite_score is not None else r.score
                e_score = existing.composite_score if existing.composite_score is not None else existing.score
                if r_score > e_score:
                    seen[key] = r
        reranked = list(seen.values())
    else:
        reranked = all_chunks

    # Group by document
    by_doc: dict[str, list] = defaultdict(list)
    for r in reranked:
        by_doc[r.doc_id].append(r)

    # Score and rank papers using pre-computed composite scores
    paper_results = []
    for doc_id, hits in by_doc.items():
        # composite_score is already populated by reranker
        composite_scores = [h.composite_score for h in hits]
        avg_composite = sum(composite_scores) / len(composite_scores)

        # Best hit by composite score
        best_idx = composite_scores.index(max(composite_scores))
        best_hit = hits[best_idx]
        best_composite = composite_scores[best_idx]

        paper_results.append({
            "doc_id": doc_id,
            "doc_title": best_hit.doc_title,
            "year": best_hit.year,
            "avg_composite_score": round(avg_composite, 3),
            "best_composite_score": round(best_composite, 3),
            "num_relevant_chunks": len(hits),
            "best_passage": best_hit.text,
            "best_passage_page": best_hit.page_num,
            "best_passage_section": best_hit.section,
        })

        if verbosity in {"standard", "full"}:
            paper_results[-1].update({
                "authors": best_hit.authors,
                "citation_key": best_hit.citation_key,
                "publication": best_hit.publication,
                "journal_quartile": best_hit.journal_quartile,
                # Raw similarity scores
                "avg_score": round(sum(h.score for h in hits) / len(hits), 3),
                "best_chunk_score": round(best_hit.score, 3),
                "best_passage_section_confidence": round(best_hit.section_confidence, 2),
            })

        if verbosity == "full":
            paper_results[-1].update({
                "tags": best_hit.tags,
                "collections": best_hit.collections,
            })

    paper_results.sort(key=lambda p: p["avg_composite_score"], reverse=True)
    logger.debug(f"search_topic: {time.perf_counter() - start:.3f}s")
    return paper_results[:num_papers]


@mcp.tool()
def search_boolean(
    query: Annotated[str, Field(description="Space-separated search terms (case-insensitive)")],
    operator: Annotated[str, Field(description="AND (all terms required) or OR (any term)")] = "AND",
    year_min: Annotated[int | None, Field(description="Minimum publication year")] = None,
    year_max: Annotated[int | None, Field(description="Maximum publication year")] = None,
    verbosity: Annotated[Literal["minimal", "standard", "full"], Field(description="Response detail level")] = "minimal",
) -> list[dict]:
    """Full-text keyword search via Zotero's word index (not semantic). No stemming, no phrase matching. Best for author names, acronyms, exact terms."""  # noqa: E501
    zotero = _get_zotero()
    matching_keys = zotero.search_fulltext(query, operator)

    if not matching_keys:
        return []

    # Get metadata for matching items
    all_items = zotero.get_all_items_with_pdfs()
    items_by_key = {i.item_key: i for i in all_items}

    results = []
    for key in matching_keys:
        item = items_by_key.get(key)
        if not item:
            continue

        # Apply year filters
        if year_min and (item.year is None or item.year < year_min):
            continue
        if year_max and (item.year is None or item.year > year_max):
            continue

        results.append({
            "item_key": item.item_key,
            "doc_id": item.item_key,
            "title": item.title,
            "year": item.year,
            "authors": item.authors,
        })

        if verbosity in {"standard", "full"}:
            results[-1].update({
                "citation_key": item.citation_key,
                "publication": item.publication,
                "doi": item.doi,
            })

        if verbosity == "full":
            results[-1].update({
                "tags": item.tags,
                "collections": item.collections,
            })

    # Sort by year descending
    results.sort(key=lambda x: x.get("year") or 0, reverse=True)
    return results


@mcp.tool()
def search_tables(
    query: Annotated[str, Field(description="Search query for table content")],
    top_k: Annotated[int, Field(description="Number of tables to return", ge=1, le=30)] = 10,
    year_min: Annotated[int | None, Field(description="Minimum publication year")] = None,
    year_max: Annotated[int | None, Field(description="Maximum publication year")] = None,
    author: Annotated[str | None, Field(description="Filter by author name (case-insensitive substring)")] = None,
    tag: Annotated[str | None, Field(description="Filter by Zotero tag (case-insensitive substring)")] = None,
    collection: Annotated[str | None, Field(description="Filter by collection name (substring)")] = None,
    journal_weights: Annotated[dict[str, float] | None, Field(description="Journal quartile weights 0.0-1.0. Keys: Q1, Q2, Q3, Q4, unknown")] = None,  # noqa: E501
    verbosity: Annotated[Literal["minimal", "standard", "full"], Field(description="Response detail level")] = "minimal",
) -> list[dict]:
    """Search table content (headers, cells, captions) semantically. For mixed content, use search_papers with chunk_types=["table"]."""  # noqa: E501
    start = time.perf_counter()

    # Validate journal_weights if provided
    if journal_weights is not None:
        errors = validate_journal_weights(journal_weights)
        if errors:
            raise ToolError(f"Invalid journal_weights: {'; '.join(errors)}")

    top_k = max(1, min(top_k, 30))
    store = _get_store()
    reranker = _get_reranker()
    _config = _get_config()

    # Build filters: chunk_type=table + year range (ChromaDB-native operators only)
    type_filter = {"chunk_type": {"$eq": "table"}}
    year_filter = _build_chromadb_filters(year_min, year_max)
    filters = {"$and": [type_filter, year_filter]} if year_filter else type_filter

    # Oversample for reranking; double if text filters active
    base_fetch = min(top_k * _config.oversample_multiplier, 90)
    fetch_k = base_fetch * 2 if _has_text_filters(author, tag, collection) else base_fetch

    results = store.search(query=query, top_k=fetch_k, filters=filters)
    results = _apply_text_filters(results, author, tag, collection)

    # Apply reranking (or bypass if disabled)
    if _config.rerank_enabled:
        # Convert StoredChunk to RetrievalResult for reranking
        retrieval_results = [_stored_chunk_to_retrieval_result(r) for r in results]
        # Note: section_weights not needed - all tables have section="table"
        reranked = reranker.rerank(retrieval_results, journal_weights=journal_weights)
        top_results = reranked[:min(top_k, 30)]
    else:
        # No reranking - set composite_score = relevance_score
        retrieval_results = [_stored_chunk_to_retrieval_result(r) for r in results]
        top_results = [replace(r, composite_score=r.score) for r in retrieval_results]
        top_results = top_results[:min(top_k, 30)]

    # Build output from reranked RetrievalResult objects
    # Need to look up original StoredChunk for table-specific metadata
    result_by_id = {r.id: r for r in results}

    output = []
    for r in top_results:
        original = result_by_id.get(r.chunk_id)
        meta = original.metadata if original else {}

        output.append({
            "doc_id": r.doc_id,
            "doc_title": r.doc_title,
            "year": r.year,
            "page": r.page_num,
            "table_index": meta.get("table_index", 0),
            "caption": meta.get("table_caption", ""),
            "table_markdown": r.text,
            "num_rows": meta.get("table_num_rows", 0),
            "num_cols": meta.get("table_num_cols", 0),
            "relevance_score": round(r.score, 3),
            "composite_score": round(r.composite_score, 3) if r.composite_score is not None else None,
        })

        if verbosity in {"standard", "full"}:
            output[-1].update({
                "authors": r.authors,
                "citation_key": r.citation_key,
                "publication": r.publication,
                "journal_quartile": r.journal_quartile,
            })

    logger.debug(f"search_tables: {time.perf_counter() - start:.3f}s")
    return output


@mcp.tool()
def search_figures(
    query: Annotated[str, Field(description="Search query for figure captions")],
    top_k: Annotated[int, Field(description="Number of figures to return", ge=1, le=30)] = 10,
    year_min: Annotated[int | None, Field(description="Minimum publication year")] = None,
    year_max: Annotated[int | None, Field(description="Maximum publication year")] = None,
    author: Annotated[str | None, Field(description="Filter by author name (case-insensitive substring)")] = None,
    tag: Annotated[str | None, Field(description="Filter by Zotero tag (case-insensitive substring)")] = None,
    collection: Annotated[str | None, Field(description="Filter by collection name (substring)")] = None,
    verbosity: Annotated[Literal["minimal", "standard", "full"], Field(description="Response detail level")] = "minimal",
) -> list[dict]:
    """Search figure captions semantically. Returns image paths. Orphan figures (no caption) included with generic descriptions."""  # noqa: E501
    start = time.perf_counter()
    top_k = max(1, min(top_k, 30))
    store = _get_store()

    # Build filters: chunk_type=figure + year range (ChromaDB-native operators only)
    type_filter = {"chunk_type": {"$eq": "figure"}}
    year_filter = _build_chromadb_filters(year_min, year_max)
    filters = {"$and": [type_filter, year_filter]} if year_filter else type_filter

    # Oversample if text filters active
    base_fetch = min(top_k * 3, 90)
    fetch_k = base_fetch * 2 if _has_text_filters(author, tag, collection) else base_fetch

    results = store.search(query=query, top_k=fetch_k, filters=filters)
    results = _apply_text_filters(results, author, tag, collection)

    output = []
    for r in results[:top_k]:
        meta = r.metadata
        output.append({
            "doc_id": meta.get("doc_id", ""),
            "doc_title": meta.get("doc_title", ""),
            "year": meta.get("year"),
            "page_num": meta.get("page_num", 0),
            "figure_index": meta.get("figure_index", 0),
            "caption": meta.get("caption", ""),
            "image_path": meta.get("image_path", ""),
            "relevance_score": round(r.score, 3),
        })

        if verbosity in {"standard", "full"}:
            output[-1].update({
                "authors": meta.get("authors", ""),
                "citation_key": meta.get("citation_key", ""),
                "publication": meta.get("publication", ""),
            })

    logger.debug(f"search_figures: {time.perf_counter() - start:.3f}s")
    return output


@mcp.tool()
def advanced_search(
    conditions: Annotated[list[dict], Field(description='[{field, op, value}]. Fields: title, author, year, tag, collection, publication, doi. Ops: contains, is, isNot, beginsWith, gt, lt.')],  # noqa: E501
    match: Annotated[Literal["all", "any"], Field(description="all=AND, any=OR")] = "all",
    sort_by: Annotated[str | None, Field(description="Sort: year, title, dateAdded")] = None,
    sort_dir: Annotated[Literal["asc", "desc"], Field(description="Sort direction")] = "desc",
    limit: Annotated[int, Field(description="Max results", ge=1, le=500)] = 50,
) -> list[dict]:
    """Multi-condition metadata search. Works without indexing. Use for precise filters by year/author/tag/etc."""
    try:
        return _get_zotero().advanced_search(
            conditions=conditions,
            match=match,
            sort_by=sort_by,
            sort_dir=sort_dir,
            limit=limit,
        )
    except ValueError as e:
        raise ToolError(str(e))

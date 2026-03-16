"""Search tools: semantic, topic, boolean, tables, figures."""
import time
import logging
from collections import defaultdict
from dataclasses import replace

from ..state import (
    mcp, _get_retriever, _get_reranker, _get_store, _get_config,
    ToolError, VALID_CHUNK_TYPES,
    _build_chromadb_filters, _apply_text_filters, _has_text_filters,
    _apply_required_terms, _contains_chinese, _translate_to_english,
    _merge_results_by_chunk, _result_to_dict, _stored_chunk_to_retrieval_result,
)
from ..reranker import validate_section_weights, validate_journal_weights, VALID_SECTIONS, VALID_QUARTILES

logger = logging.getLogger(__name__)


@mcp.tool()
def search_papers(
    query: str,
    top_k: int = 10,
    context_chunks: int = 1,
    year_min: int | None = None,
    year_max: int | None = None,
    author: str | None = None,
    tag: str | None = None,
    collection: str | None = None,
    chunk_types: list[str] | None = None,
    section_weights: dict[str, float] | None = None,
    journal_weights: dict[str, float] | None = None,
    required_terms: list[str] | None = None,
) -> list[dict]:
    """
    Semantic search over research paper chunks.

    Returns relevant passages with surrounding context.

    Results are reranked by a composite score combining semantic similarity,
    document section, and journal quartile. chunk_types and section_weights
    are orthogonal dimensions -- use chunk_types to select what kind of
    content (text, figures, tables) and section_weights to prefer where
    in the paper it appears.

    Combine semantic search with exact word matching by passing
    required_terms. Each term must appear as a whole word (case-insensitive)
    in the passage text for the result to be included. This is useful for
    ensuring results contain specific acronyms, identifiers, or keywords
    that semantic search alone might miss.

    Args:
        query: Natural language search query
        top_k: Number of results (1-50)
        context_chunks: Adjacent chunks to include (0-3)
        year_min: Minimum publication year filter
        year_max: Maximum publication year filter
        author: Filter by author name (case-insensitive substring match)
        tag: Filter by Zotero tag (case-insensitive substring match)
        collection: Filter by Zotero collection name (substring match)
        chunk_types: Filter by content type. Valid values: text, figure,
            table. Pass a list to include multiple (e.g. ["text", "table"]).
            Omit or pass null to search all types.
        section_weights: Override section relevance weights. Keys are section
            labels: abstract, introduction, background, methods, results,
            discussion, conclusion, references, appendix, preamble, table,
            unknown. Values are 0.0-1.0. Set a section to 0 to exclude it.
        journal_weights: Override journal quartile weights. Keys: Q1, Q2,
            Q3, Q4, unknown. Values are 0.0-1.0.
        required_terms: List of words that must appear in the passage text
            (case-insensitive whole-word match). All terms must be present.
            Use this to combine semantic search with exact keyword filtering.

    Returns:
        List of results with passage text, context, and metadata
    """
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

    # Auto-translate Chinese queries and run bilingual search
    queries = [query]
    if _contains_chinese(query):
        en_query = _translate_to_english(query)
        if en_query:
            queries.append(en_query)
            logger.debug(f"Bilingual search: zh='{query}' en='{en_query}'")

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
    return [_result_to_dict(r) for r in top_results]


@mcp.tool()
def search_topic(
    query: str,
    num_papers: int = 10,
    year_min: int | None = None,
    year_max: int | None = None,
    author: str | None = None,
    tag: str | None = None,
    collection: str | None = None,
    chunk_types: list[str] | None = None,
    section_weights: dict[str, float] | None = None,
    journal_weights: dict[str, float] | None = None,
) -> list[dict]:
    """
    Find the most relevant papers for a topic, deduplicated by document.

    Searches across all chunks, then groups by paper. Each paper is scored
    by both its average composite relevance and its best single chunk.
    Results are sorted by average composite score.

    Papers are scored using composite relevance combining similarity, section,
    and journal quality. chunk_types and section_weights are orthogonal --
    use chunk_types to select content kind and section_weights to prefer
    where in the paper it appears.

    Args:
        query: Natural language topic description
        num_papers: Number of distinct papers to return (1-50)
        year_min: Minimum publication year filter
        year_max: Maximum publication year filter
        author: Filter by author name (case-insensitive substring match)
        tag: Filter by Zotero tag (case-insensitive substring match)
        collection: Filter by Zotero collection name (substring match)
        chunk_types: Filter by content type. Valid values: text, figure,
            table. Pass a list to include multiple (e.g. ["text", "figure"]).
            Omit or pass null to search all types.
        section_weights: Override section relevance weights. Keys are section
            labels: abstract, introduction, background, methods, results,
            discussion, conclusion, references, appendix, preamble, table,
            unknown. Values are 0.0-1.0. Set a section to 0 to exclude it.
        journal_weights: Override journal quartile weights. Keys: Q1, Q2,
            Q3, Q4, unknown. Values are 0.0-1.0.

    Returns:
        List of per-paper results with scores and best passage
    """
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

    # Auto-translate Chinese queries and run bilingual search
    queries = [query]
    if _contains_chinese(query):
        en_query = _translate_to_english(query)
        if en_query:
            queries.append(en_query)
            logger.debug(f"Bilingual topic search: zh='{query}' en='{en_query}'")

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
            context_window=1,
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
            "authors": best_hit.authors,
            "year": best_hit.year,
            "citation_key": best_hit.citation_key,
            "publication": best_hit.publication,
            "journal_quartile": best_hit.journal_quartile,
            # Raw similarity scores
            "avg_score": round(sum(h.score for h in hits) / len(hits), 3),
            "best_chunk_score": round(best_hit.score, 3),
            # Composite scores
            "avg_composite_score": round(avg_composite, 3),
            "best_composite_score": round(best_composite, 3),
            "best_passage_section": best_hit.section,
            "best_passage_section_confidence": round(best_hit.section_confidence, 2),
            "num_relevant_chunks": len(hits),
            "best_passage": best_hit.text,
            "best_passage_page": best_hit.page_num,
            "best_passage_context": best_hit.full_context(),
        })

    paper_results.sort(key=lambda p: p["avg_composite_score"], reverse=True)
    logger.debug(f"search_topic: {time.perf_counter() - start:.3f}s")
    return paper_results[:num_papers]


@mcp.tool()
def search_boolean(
    query: str,
    operator: str = "AND",
    year_min: int | None = None,
    year_max: int | None = None,
) -> list[dict]:
    """
    Boolean full-text search using Zotero's native word index.

    Use for exact word matching with AND/OR logic. Unlike semantic search,
    this finds exact word matches only (no synonyms or similar meaning).

    This searches the full text of PDFs that Zotero has indexed. Words are
    tokenized by Zotero's indexer, so punctuation and hyphenation affect
    matching (e.g., "heart-rate" is two words: "heart" and "rate").

    Limitations:
    - No phrase search ("heart rate" searches for both words, not the phrase)
    - No stemming ("running" won't match "run")
    - Requires Zotero to have indexed the PDFs

    Args:
        query: Space-separated search terms (case-insensitive)
        operator: "AND" (all terms required) or "OR" (any term matches)
        year_min: Minimum publication year filter
        year_max: Maximum publication year filter

    Returns:
        List of matching papers with metadata (no passages - use search_papers
        for passage retrieval on specific papers)
    """
    from ..zotero_client import ZoteroClient
    from ..state import _config as _state_config
    from ..config import Config

    # Get config lazily
    _config = _state_config
    if _config is None:
        _config = Config.load()

    zotero = ZoteroClient(_config.zotero_data_dir)
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
            "title": item.title,
            "authors": item.authors,
            "year": item.year,
            "publication": item.publication,
            "citation_key": item.citation_key,
            "tags": item.tags,
            "collections": item.collections,
            "doi": item.doi,
        })

    # Sort by year descending
    results.sort(key=lambda x: x.get("year") or 0, reverse=True)
    return results


@mcp.tool()
def search_tables(
    query: str,
    top_k: int = 10,
    year_min: int | None = None,
    year_max: int | None = None,
    author: str | None = None,
    tag: str | None = None,
    collection: str | None = None,
    journal_weights: dict[str, float] | None = None,
) -> list[dict]:
    """
    Search for tables in indexed papers.

    Searches table content (headers, cells, captions) semantically.
    Returns tables as markdown with metadata. Use this for table-specific
    searches; for mixed content searches (e.g. tables in results sections),
    use search_papers with chunk_types=["table"] and section_weights.

    Results are reranked by composite score combining semantic similarity
    and journal quartile. Tables are assigned section="table" with
    default weight 0.9.

    Args:
        query: Search query describing desired table content
        top_k: Number of tables to return (1-30)
        year_min: Minimum publication year filter
        year_max: Maximum publication year filter
        author: Filter by author name (case-insensitive substring match)
        tag: Filter by Zotero tag (case-insensitive substring match)
        collection: Filter by Zotero collection name (substring match)
        journal_weights: Override journal quartile weights. Keys: Q1, Q2,
            Q3, Q4, unknown. Values are 0.0-1.0.

    Returns:
        List of matching tables with:
        - doc_title, authors, year, citation_key: Bibliographic info
        - page: Page number where table appears
        - table_index: Index of table on page
        - caption: Table caption if detected
        - table_markdown: Full table as markdown
        - num_rows, num_cols: Table dimensions
        - relevance_score: Semantic similarity (0-1)
        - composite_score: Reranked score (similarity * section * journal)
        - doc_id: Document ID for use with get_passage_context
    """
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
            "doc_title": r.doc_title,
            "authors": r.authors,
            "year": r.year,
            "citation_key": r.citation_key,
            "publication": r.publication,
            "journal_quartile": r.journal_quartile,
            "page": r.page_num,
            "table_index": meta.get("table_index", 0),
            "caption": meta.get("table_caption", ""),
            "table_markdown": r.text,
            "num_rows": meta.get("table_num_rows", 0),
            "num_cols": meta.get("table_num_cols", 0),
            "relevance_score": round(r.score, 3),
            "composite_score": round(r.composite_score, 3) if r.composite_score is not None else None,
            "doc_id": r.doc_id,
        })

    logger.debug(f"search_tables: {time.perf_counter() - start:.3f}s")
    return output


@mcp.tool()
def search_figures(
    query: str,
    top_k: int = 10,
    year_min: int | None = None,
    year_max: int | None = None,
    author: str | None = None,
    tag: str | None = None,
    collection: str | None = None,
) -> list[dict]:
    """
    Search for figures by caption content.

    Searches figure captions semantically. Returns figures with
    their captions, page numbers, and paths to extracted images.
    Use this for figure-specific searches; for mixed content searches
    (e.g. figures in results sections), use search_papers with
    chunk_types=["figure"] and section_weights.

    Figures without detected captions are included as "orphans"
    with a generic description like "Figure on page X".

    Args:
        query: Search query for figure captions
        top_k: Number of figures to return (1-30)
        year_min: Minimum publication year filter
        year_max: Maximum publication year filter
        author: Filter by author name (case-insensitive substring match)
        tag: Filter by Zotero tag (case-insensitive substring match)
        collection: Filter by Zotero collection name (substring match)

    Returns:
        List of matching figures with:
        - doc_title, authors, year, citation_key: Bibliographic info
        - page_num: Page number where figure appears
        - figure_index: Index of figure on page
        - caption: Figure caption (empty string for orphans)
        - image_path: Path to extracted PNG image
        - relevance_score: Semantic similarity (0-1)
        - doc_id: Document ID for use with other tools
    """
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
            "authors": meta.get("authors", ""),
            "year": meta.get("year"),
            "citation_key": meta.get("citation_key", ""),
            "publication": meta.get("publication", ""),
            "page_num": meta.get("page_num", 0),
            "figure_index": meta.get("figure_index", 0),
            "caption": meta.get("caption", ""),
            "image_path": meta.get("image_path", ""),
            "relevance_score": round(r.score, 3),
        })

    logger.debug(f"search_figures: {time.perf_counter() - start:.3f}s")
    return output

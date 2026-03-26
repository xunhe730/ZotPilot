"""Indexing tools: index_library, get_index_stats."""
import logging
from collections import defaultdict
from typing import Annotated

from pydantic import Field

from ..state import ToolError, _get_config, _get_retriever, _get_store, _get_zotero, mcp

logger = logging.getLogger(__name__)


def _collect_unindexed_papers(limit: int | None = None, offset: int = 0) -> tuple[list[dict], int]:
    """Return unindexed Zotero papers and their total count."""
    zotero = _get_zotero()
    indexed_set = set(_get_store().get_indexed_doc_ids())
    papers: list[dict] = []
    total = 0

    for item in zotero.get_all_items_with_pdfs():
        if item.item_key in indexed_set:
            continue
        total += 1
        if total <= offset:
            continue
        if limit is not None and len(papers) >= limit:
            continue
        papers.append({
            "doc_id": item.item_key,
            "title": item.title or "(no title)",
            "year": item.year,
            "authors": item.authors,
        })

    return papers, total


@mcp.tool()
def index_library(
    force_reindex: Annotated[bool, Field(description="Delete and rebuild index for matching items")] = False,
    limit: Annotated[int | None, Field(description="Max items to index, None for all")] = None,
    item_key: Annotated[str | None, Field(description="Index only this specific item key")] = None,
    title_pattern: Annotated[str | None, Field(description="Regex to filter items by title (case-insensitive)")] = None,
    no_vision: Annotated[bool, Field(description="Disable vision-based table extraction")] = False,
    batch_size: Annotated[int, Field(description="Items per batch (default 20). Set 0 for all at once. Call repeatedly until has_more=false. Vision extraction is auto-disabled in batch mode; use batch_size=0 for vision.")] = 20,  # noqa: E501
    max_pages: Annotated[int | None, Field(description="Skip PDFs over N pages. None uses config default (40). 0=no limit.")] = None,  # noqa: E501
    include_summary: Annotated[bool, Field(description="Include extended indexing summary fields")] = False,
) -> dict:
    """Index Zotero PDFs into the vector store. Incremental by default; processes batch_size items per call. Repeat until has_more=false to index all."""  # noqa: E501
    from dataclasses import replace as dc_replace

    from ..indexer import Indexer

    _config = _get_config()

    errors = _config.validate()
    if errors:
        raise ToolError(f"Config errors: {'; '.join(errors)}")

    config = _config

    # Batch mode defaults to no_vision to avoid many small vision API calls
    if batch_size > 0 and not no_vision:
        config = dc_replace(config, vision_enabled=False)
    elif no_vision:
        config = dc_replace(config, vision_enabled=False)

    effective_max_pages = max_pages if max_pages is not None else config.max_pages

    indexer = Indexer(config)
    result = indexer.index_all(
        force_reindex=force_reindex,
        limit=limit,
        item_key=item_key,
        title_pattern=title_pattern,
        max_pages=effective_max_pages,
        batch_size=batch_size if batch_size > 0 else None,
    )

    # Clear query embedding cache so new documents are findable
    _get_store().clear_query_cache()

    # Serialize IndexResult objects
    serialized_results = []
    for r in result["results"]:
        serialized_results.append({
            "item_key": r.item_key,
            "title": r.title,
            "status": r.status,
            "reason": r.reason,
            "n_chunks": r.n_chunks,
            "n_tables": r.n_tables,
            "quality_grade": r.quality_grade,
        })

    response = {
        "results": serialized_results,
        "indexed": result["indexed"],
        "failed": result["failed"],
        "empty": result["empty"],
        "skipped": result["skipped"],
        "already_indexed": result["already_indexed"],
        "has_more": result.get("has_more", False),
        "vision_enabled": config.vision_enabled,
    }

    if include_summary:
        response.update({
            "quality_distribution": result.get("quality_distribution"),
            "extraction_stats": result.get("extraction_stats"),
            "long_documents": result.get("long_documents", []),
            "skipped_long": result.get("skipped_long", 0),
            "total_to_index": result.get("total_to_index", 0),
        })

    return response


@mcp.tool()
def get_index_stats() -> dict:
    """Get index statistics and list of unindexed papers. Call first to check readiness."""
    _config = _get_config()
    if _config.embedding_provider == "none":
        return {
            "total_documents": 0,
            "mode": "no-rag",
            "message": "Indexing disabled in No-RAG mode. Use advanced_search, get_notes, list_tags, etc. for basic features. Configure an embedding provider to enable semantic search.",  # noqa: E501
        }
    _get_retriever()  # Ensure initialized
    store = _get_store()
    _config = _get_config()
    doc_ids = store.get_indexed_doc_ids()
    total_chunks = store.count()

    # Get section, journal, and chunk type coverage from a sample of chunks
    # (Getting all chunks would be expensive for large collections)
    sample = store.collection.get(limit=_config.stats_sample_limit, include=["metadatas"])

    section_counts: dict[str, int] = defaultdict(int)
    journal_doc_quartiles: dict[str, str] = {}  # doc_id -> quartile
    chunk_type_counts: dict[str, int] = defaultdict(int)

    if sample["metadatas"]:
        for meta in sample["metadatas"]:
            section = meta.get("section", "unknown")
            section_counts[section] += 1

            chunk_type = meta.get("chunk_type", "text")
            chunk_type_counts[chunk_type] += 1

            doc_id = meta.get("doc_id", "")
            quartile = meta.get("journal_quartile", "")
            if doc_id and doc_id not in journal_doc_quartiles:
                journal_doc_quartiles[doc_id] = quartile

    # Count documents per quartile
    journal_counts: dict[str, int] = defaultdict(int)
    for quartile in journal_doc_quartiles.values():
        key = quartile if quartile else "unknown"
        journal_counts[key] += 1

    # Check for unindexed papers: items in Zotero with PDFs but not in ChromaDB
    sample_unindexed = []
    unindexed_count = 0
    try:
        sample_unindexed, unindexed_count = _collect_unindexed_papers(limit=5, offset=0)
    except Exception as e:
        logger.warning(f"Could not check for unindexed papers: {e}")

    result = {
        "total_documents": len(doc_ids),
        "total_chunks": total_chunks,
        "avg_chunks_per_doc": round(total_chunks / len(doc_ids), 1) if doc_ids else 0,
        "section_coverage": dict(section_counts),
        "journal_coverage": dict(journal_counts),
        "chunk_types": dict(chunk_type_counts),
        "unindexed_count": unindexed_count,
        "sample_unindexed": sample_unindexed,
    }

    if unindexed_count:
        sample_note = f" Showing {len(sample_unindexed)} sample(s)." if sample_unindexed else ""
        result["_notice"] = (
            f"\u26a0\ufe0f {unindexed_count} paper(s) in Zotero are not yet indexed."
            f"{sample_note} Call index_library() to update the RAG library."
        )

    return result


@mcp.tool()
def get_unindexed_papers(
    limit: Annotated[int, Field(description="Papers per page", ge=1, le=200)] = 50,
    offset: Annotated[int, Field(description="Starting index for pagination", ge=0)] = 0,
) -> dict:
    """List unindexed Zotero papers with pagination."""
    _config = _get_config()
    if _config.embedding_provider == "none":
        return {
            "total": 0,
            "offset": offset,
            "limit": limit,
            "papers": [],
            "mode": "no-rag",
        }

    _get_retriever()

    try:
        papers, total = _collect_unindexed_papers(limit=limit, offset=offset)
    except Exception as e:
        raise ToolError(f"Could not collect unindexed papers: {e}")

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "papers": papers,
    }

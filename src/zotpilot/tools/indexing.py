"""Indexing tools: index_library, get_index_stats."""
import logging
from collections import defaultdict

from ..state import mcp, _get_retriever, _get_store, _get_zotero, _get_config, ToolError
from ..config import Config

logger = logging.getLogger(__name__)


@mcp.tool()
def index_library(
    force_reindex: bool = False,
    limit: int | None = None,
    item_key: str | None = None,
    title_pattern: str | None = None,
    no_vision: bool = False,
) -> dict:
    """
    Index Zotero PDFs into the vector store.

    Extracts text, tables, and figures from PDFs, chunks them, and stores
    embeddings in ChromaDB. Incrementally indexes only new/changed documents
    unless force_reindex is True.

    Args:
        force_reindex: Delete and rebuild index for all matching items
        limit: Maximum number of items to index (None = all)
        item_key: Index only this specific Zotero item key
        title_pattern: Regex pattern to filter items by title (case-insensitive)
        no_vision: Disable vision-based table extraction for this run

    Returns:
        Summary with counts of indexed/failed/skipped items and quality stats
    """
    from ..indexer import Indexer

    _config = _get_config()

    errors = _config.validate()
    if errors:
        raise ToolError(f"Config errors: {'; '.join(errors)}")

    config = _config
    if no_vision:
        from dataclasses import replace as dc_replace
        config = dc_replace(_config, vision_enabled=False)

    indexer = Indexer(config)
    result = indexer.index_all(
        force_reindex=force_reindex,
        limit=limit,
        item_key=item_key,
        title_pattern=title_pattern,
    )

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

    return {
        "results": serialized_results,
        "indexed": result["indexed"],
        "failed": result["failed"],
        "empty": result["empty"],
        "skipped": result["skipped"],
        "already_indexed": result["already_indexed"],
        "quality_distribution": result.get("quality_distribution"),
        "extraction_stats": result.get("extraction_stats"),
    }


@mcp.tool()
def get_index_stats() -> dict:
    """Get statistics about the indexed collection.

    Also checks for papers in Zotero that have not yet been indexed into the
    RAG store. If unindexed_count > 0, consider calling index_library() to
    update the index.
    """
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
    unindexed_papers = []
    try:
        zotero = _get_zotero()
        all_items = zotero.get_all_items_with_pdfs()
        indexed_set = set(doc_ids)
        for item in all_items:
            if item.item_key not in indexed_set:
                unindexed_papers.append({
                    "item_key": item.item_key,
                    "title": item.title or "(no title)",
                    "year": item.year,
                    "authors": item.authors,
                })
    except Exception as e:
        logger.warning(f"Could not check for unindexed papers: {e}")

    result = {
        "total_documents": len(doc_ids),
        "total_chunks": total_chunks,
        "avg_chunks_per_doc": round(total_chunks / len(doc_ids), 1) if doc_ids else 0,
        "section_coverage": dict(section_counts),
        "journal_coverage": dict(journal_counts),
        "chunk_types": dict(chunk_type_counts),
        "unindexed_count": len(unindexed_papers),
        "unindexed_papers": unindexed_papers,
    }

    if unindexed_papers:
        result["_notice"] = (
            f"\u26a0\ufe0f {len(unindexed_papers)} paper(s) in Zotero are not yet indexed. "
            "Call index_library() to update the RAG library."
        )

    return result

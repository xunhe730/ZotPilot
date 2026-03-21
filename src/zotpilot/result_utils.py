"""Result conversion and merging utilities."""
from .models import RetrievalResult


def _stored_chunk_to_retrieval_result(chunk) -> RetrievalResult:
    """Convert a StoredChunk to RetrievalResult for reranking."""
    meta = chunk.metadata
    return RetrievalResult(
        chunk_id=chunk.id,
        text=chunk.text,
        score=chunk.score,
        doc_id=meta.get("doc_id", ""),
        doc_title=meta.get("doc_title", ""),
        authors=meta.get("authors", ""),
        year=meta.get("year"),
        page_num=meta.get("page_num", 0),
        chunk_index=meta.get("chunk_index", 0),
        citation_key=meta.get("citation_key", ""),
        publication=meta.get("publication", ""),
        section=meta.get("section", "table"),  # Tables default to "table" section
        section_confidence=meta.get("section_confidence", 1.0),
        tags=meta.get("tags", ""),
        collections=meta.get("collections", ""),
        journal_quartile=meta.get("journal_quartile"),
    )


def _merge_results_by_chunk(primary: list, secondary: list, top_k: int) -> list:
    """Merge two result lists, keeping the best composite_score per unique (doc_id, chunk_index)."""
    seen: dict[tuple, object] = {}
    for r in primary + secondary:
        key = (r.doc_id, r.chunk_index)
        existing = seen.get(key)
        if existing is None:
            seen[key] = r
        else:
            # Keep whichever has the higher composite_score (or score as fallback)
            r_score = r.composite_score if r.composite_score is not None else r.score
            e_score = existing.composite_score if existing.composite_score is not None else existing.score
            if r_score > e_score:
                seen[key] = r
    merged = sorted(seen.values(),
                    key=lambda x: x.composite_score if x.composite_score is not None else x.score,
                    reverse=True)
    return merged[:top_k]


def _result_to_dict(r) -> dict:
    """Convert RetrievalResult to API response dict.

    Expects r.composite_score to be populated by reranker.
    """
    return {
        "doc_title": r.doc_title,
        "authors": r.authors,
        "year": r.year,
        "citation_key": r.citation_key,
        "publication": r.publication,
        "page": r.page_num,
        "relevance_score": round(r.score, 3),
        "composite_score": round(r.composite_score, 3) if r.composite_score is not None else None,
        "section": r.section,
        "section_confidence": round(r.section_confidence, 2),
        "journal_quartile": r.journal_quartile,
        "passage": r.text,
        "context_before": r.context_before,
        "context_after": r.context_after,
        "full_context": r.full_context(),
        "doc_id": r.doc_id,
        "item_key": r.doc_id,
        "chunk_index": r.chunk_index,
        "tags": r.tags,
        "collections": r.collections,
    }

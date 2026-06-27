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
        chunk_type=meta.get("chunk_type", "text"),
        formula_latex=meta.get("formula_latex", ""),
        formula_equation_number=meta.get("formula_equation_number", ""),
        formula_equation_number_status=meta.get("formula_equation_number_status", ""),
        formula_locator=meta.get("formula_locator", ""),
        formula_variable_gloss=meta.get("formula_variable_gloss", ""),
        formula_provider=meta.get("formula_provider", ""),
        formula_source=meta.get("formula_source", ""),
        formula_confidence=meta.get("formula_confidence"),
        reference_context=meta.get("reference_context", ""),
    )


def _merge_results_by_chunk(primary: list, secondary: list, top_k: int) -> list:
    """Merge result lists, keeping the best score per unique stored chunk."""
    seen: dict[tuple, object] = {}
    for r in primary + secondary:
        key = (r.doc_id, r.chunk_type, r.chunk_index)
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


def _result_to_dict(r, verbosity: str = "full") -> dict:
    """Convert RetrievalResult to API response dict.

    Expects r.composite_score to be populated by reranker.
    """
    result = {
        "doc_title": r.doc_title,
        "doc_id": r.doc_id,
        "year": r.year,
        "page": r.page_num,
        "chunk_index": r.chunk_index,
        "relevance_score": round(r.score, 3),
        "composite_score": round(r.composite_score, 3) if r.composite_score is not None else None,
        "section": r.section,
        "chunk_type": r.chunk_type,
        "passage": r.text,
    }

    if r.chunk_type == "formula":
        formula_payload = {
            "formula_latex": r.formula_latex,
            "equation_number": r.formula_equation_number,
            "equation_number_status": r.formula_equation_number_status,
            "formula_locator": r.formula_locator,
            "variable_gloss": r.formula_variable_gloss,
            "reference_context": r.reference_context,
            "formula_provider": r.formula_provider,
            "formula_source": r.formula_source,
        }
        if r.formula_confidence is not None:
            formula_payload["formula_confidence"] = round(float(r.formula_confidence), 3)
        result.update(formula_payload)

    if verbosity != "minimal" and (r.context_before or r.context_after):
        result.update({
            "context_before": r.context_before,
            "context_after": r.context_after,
            "full_context": r.full_context(),
        })

    if verbosity in {"standard", "full"}:
        result.update({
            "authors": r.authors,
            "citation_key": r.citation_key,
            "publication": r.publication,
            "section_confidence": round(r.section_confidence, 2),
            "journal_quartile": r.journal_quartile,
        })

    if verbosity == "full":
        result.update({
            "tags": r.tags,
            "collections": r.collections,
        })

    return result

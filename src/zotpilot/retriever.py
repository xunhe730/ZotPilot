"""Search with automatic context expansion."""
from .interfaces import VectorStoreProtocol
from .models import RetrievalResult


class Retriever:
    """
    Semantic search with context expansion.

    Wraps VectorStore to provide:
    - Automatic context expansion around hits
    - Result formatting with full metadata
    """

    def __init__(self, vector_store: VectorStoreProtocol):
        self.store = vector_store

    def search(
        self,
        query: str,
        top_k: int = 10,
        context_window: int = 1,
        filters: dict | None = None
    ) -> list[RetrievalResult]:
        """
        Search for relevant chunks and expand context.

        Args:
            query: Search query
            top_k: Number of results
            context_window: Chunks before/after to include (0-5)
            filters: Optional metadata filters

        Returns:
            List of RetrievalResult with expanded context
        """
        hits = self.store.search(query, top_k=top_k, filters=filters)

        results = []
        for hit in hits:
            chunk_type = hit.metadata.get("chunk_type", "text")
            # Get adjacent chunks
            if context_window > 0 and chunk_type == "text":
                adjacent = self.store.get_adjacent_chunks(
                    hit.metadata.get("doc_id", "unknown"),
                    hit.metadata.get("chunk_index", -1),
                    window=context_window
                )
            else:
                adjacent = []

            # Separate into before/after
            context_before = []
            context_after = []
            center_idx = hit.metadata.get("chunk_index", -1)

            for adj in adjacent:
                adj_idx = adj.metadata.get("chunk_index", -1)
                if adj_idx < center_idx:
                    context_before.append(adj.text)
                elif adj_idx > center_idx:
                    context_after.append(adj.text)

            # Handle journal_quartile - empty string from DB means None
            jq = hit.metadata.get("journal_quartile", "")
            journal_quartile = jq if jq else None

            results.append(RetrievalResult(
                chunk_id=hit.id,
                text=hit.text,
                score=hit.score,
                doc_id=hit.metadata.get("doc_id", "unknown"),
                doc_title=hit.metadata.get("doc_title", "Unknown"),
                authors=hit.metadata.get("authors", ""),
                year=hit.metadata.get("year") or None,
                page_num=hit.metadata.get("page_num", 0),
                chunk_index=hit.metadata.get("chunk_index", -1),
                citation_key=hit.metadata.get("citation_key", ""),
                publication=hit.metadata.get("publication", ""),
                tags=hit.metadata.get("tags", ""),
                collections=hit.metadata.get("collections", ""),
                section=hit.metadata.get("section", "unknown"),
                section_confidence=hit.metadata.get("section_confidence", 1.0),
                journal_quartile=journal_quartile,
                chunk_type=chunk_type,
                formula_latex=hit.metadata.get("formula_latex", ""),
                formula_equation_number=hit.metadata.get("formula_equation_number", ""),
                formula_variable_gloss=hit.metadata.get("formula_variable_gloss", ""),
                formula_provider=hit.metadata.get("formula_provider", ""),
                formula_source=hit.metadata.get("formula_source", ""),
                formula_confidence=hit.metadata.get("formula_confidence"),
                reference_context=hit.metadata.get("reference_context", ""),
                context_before=context_before,
                context_after=context_after,
            ))

        return results

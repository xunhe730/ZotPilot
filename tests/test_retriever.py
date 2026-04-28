"""Tests for Retriever search and context expansion."""
from unittest.mock import MagicMock

from zotpilot.models import RetrievalResult, StoredChunk
from zotpilot.retriever import Retriever


def _make_stored_chunk(
    doc_id="DOC1",
    chunk_index=0,
    text="Some chunk text",
    score=0.9,
    **meta_overrides,
):
    """Helper to create a StoredChunk with sensible defaults."""
    metadata = {
        "doc_id": doc_id,
        "doc_title": "Test Paper",
        "authors": "Smith, J.",
        "year": 2023,
        "page_num": 1,
        "chunk_index": chunk_index,
        "citation_key": "smith2023",
        "publication": "Nature",
        "tags": "ml; ai",
        "collections": "Research",
        "section": "results",
        "section_confidence": 1.0,
        "journal_quartile": "Q1",
    }
    metadata.update(meta_overrides)
    return StoredChunk(
        id=f"{doc_id}_chunk_{chunk_index:04d}",
        text=text,
        metadata=metadata,
        score=score,
    )


class TestRetrieverSearch:
    def test_search_returns_results(self):
        """Mock VectorStore.search() and get_adjacent_chunks(), verify RetrievalResult fields."""
        mock_store = MagicMock()
        hit = _make_stored_chunk(chunk_index=1, text="Main result text", score=0.92)
        adj_before = _make_stored_chunk(chunk_index=0, text="Before context")
        adj_after = _make_stored_chunk(chunk_index=2, text="After context")

        mock_store.search.return_value = [hit]
        mock_store.get_adjacent_chunks.return_value = [adj_before, adj_after]

        retriever = Retriever(mock_store)
        results = retriever.search("test query", top_k=5, context_window=1)

        assert len(results) == 1
        result = results[0]
        assert isinstance(result, RetrievalResult)
        assert result.text == "Main result text"
        assert result.score == 0.92
        assert result.doc_id == "DOC1"
        assert result.doc_title == "Test Paper"
        assert result.authors == "Smith, J."
        assert result.year == 2023
        assert result.page_num == 1
        assert result.chunk_index == 1
        assert result.citation_key == "smith2023"
        assert result.publication == "Nature"
        assert result.section == "results"
        assert result.section_confidence == 1.0
        assert result.journal_quartile == "Q1"

    def test_context_expansion(self):
        """Verify context_before/context_after populated from adjacent chunks."""
        mock_store = MagicMock()
        hit = _make_stored_chunk(chunk_index=2, text="Center chunk")
        adj_0 = _make_stored_chunk(chunk_index=0, text="Two before")
        adj_1 = _make_stored_chunk(chunk_index=1, text="One before")
        adj_3 = _make_stored_chunk(chunk_index=3, text="One after")
        adj_4 = _make_stored_chunk(chunk_index=4, text="Two after")

        mock_store.search.return_value = [hit]
        mock_store.get_adjacent_chunks.return_value = [adj_0, adj_1, adj_3, adj_4]

        retriever = Retriever(mock_store)
        results = retriever.search("query", context_window=2)

        result = results[0]
        assert result.context_before == ["Two before", "One before"]
        assert result.context_after == ["One after", "Two after"]

    def test_no_context_window(self):
        """context_window=0 means no adjacent chunks fetched."""
        mock_store = MagicMock()
        hit = _make_stored_chunk(chunk_index=0, text="Solo chunk")

        mock_store.search.return_value = [hit]

        retriever = Retriever(mock_store)
        results = retriever.search("query", context_window=0)

        mock_store.get_adjacent_chunks.assert_not_called()
        result = results[0]
        assert result.context_before == []
        assert result.context_after == []

    def test_empty_results(self):
        """Empty search returns empty list."""
        mock_store = MagicMock()
        mock_store.search.return_value = []

        retriever = Retriever(mock_store)
        results = retriever.search("query with no hits")

        assert results == []

    def test_journal_quartile_empty_string_to_none(self):
        """Empty string from DB converted to None."""
        mock_store = MagicMock()
        hit = _make_stored_chunk(chunk_index=0, journal_quartile="")

        mock_store.search.return_value = [hit]
        mock_store.get_adjacent_chunks.return_value = []

        retriever = Retriever(mock_store)
        results = retriever.search("query", context_window=1)

        assert results[0].journal_quartile is None

"""Tests for VectorStore query embedding cache (P2-11)."""
from unittest.mock import MagicMock

import pytest

from zotpilot.vector_store import VectorStore


@pytest.fixture
def mock_store(tmp_path):
    """Create a VectorStore with a mock embedder (no real ChromaDB)."""
    embedder = MagicMock()
    embedder.dimensions = 8
    embedder.embed_query = MagicMock(side_effect=lambda q: [0.1] * 8)
    embedder.embed = MagicMock(return_value=[[0.1] * 8])

    store = VectorStore(tmp_path / "chroma", embedder)
    return store


class TestQueryCache:
    def test_cache_hit(self, mock_store):
        """Same query twice: embed_query called only once."""
        mock_store._cached_embed_query("test query")
        mock_store._cached_embed_query("test query")
        assert mock_store.embedder.embed_query.call_count == 1

    def test_cache_miss_different_query(self, mock_store):
        """Different queries each call embed_query."""
        mock_store._cached_embed_query("query A")
        mock_store._cached_embed_query("query B")
        assert mock_store.embedder.embed_query.call_count == 2

    def test_clear_cache(self, mock_store):
        """After clear, same query triggers embed_query again."""
        mock_store._cached_embed_query("test query")
        mock_store.clear_query_cache()
        mock_store._cached_embed_query("test query")
        assert mock_store.embedder.embed_query.call_count == 2

    def test_cache_maxsize(self, mock_store):
        """Cache doesn't exceed maxsize."""
        mock_store._query_cache_maxsize = 10
        for i in range(20):
            mock_store._cached_embed_query(f"query_{i}")
        assert len(mock_store._query_cache) == 10

    def test_cache_fifo_eviction(self, mock_store):
        """FIFO: oldest entry evicted when full."""
        mock_store._query_cache_maxsize = 3
        mock_store._cached_embed_query("a")
        mock_store._cached_embed_query("b")
        mock_store._cached_embed_query("c")
        mock_store._cached_embed_query("d")  # should evict "a"
        assert "a" not in mock_store._query_cache
        assert "d" in mock_store._query_cache

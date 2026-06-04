"""End-to-end dimension-contract test (Step 5.14 / AC#17).

Verifies the FULL corruption-prevention chain, not just the embedder layer:
the embedder's ``dimensions`` flows into the Chroma collection metadata, and
reopening the store with a differently-dimensioned embedder raises
``EmbeddingDimensionMismatchError``. Deterministic, no network.
"""
import pytest

from zotpilot.models import Chunk
from zotpilot.vector_store import EmbeddingDimensionMismatchError, VectorStore


class StubEmbedder:
    """Deterministic embedder returning fixed-length zero vectors."""

    def __init__(self, dimensions: int):
        self.dimensions = dimensions

    def embed(self, texts, task_type="RETRIEVAL_DOCUMENT"):
        return [[0.0] * self.dimensions for _ in texts]

    def embed_query(self, query):
        return [0.0] * self.dimensions


def _chunks():
    return [
        Chunk(text="alpha chunk", chunk_index=0, page_num=1, char_start=0, char_end=11),
        Chunk(text="beta chunk", chunk_index=1, page_num=1, char_start=11, char_end=21),
    ]


def test_dimension_written_to_collection_metadata(tmp_path):
    store = VectorStore(tmp_path / "chroma", StubEmbedder(1024))
    store.add_chunks("doc1", {"title": "Doc 1"}, _chunks())
    assert store.collection.metadata.get("embedding_dimensions") == 1024


def test_reopen_with_different_dimension_raises(tmp_path):
    db = tmp_path / "chroma"
    store = VectorStore(db, StubEmbedder(1024))
    store.add_chunks("doc1", {"title": "Doc 1"}, _chunks())
    del store  # release the client before reopening

    with pytest.raises(EmbeddingDimensionMismatchError):
        VectorStore(db, StubEmbedder(768))


def test_reopen_with_same_dimension_succeeds(tmp_path):
    db = tmp_path / "chroma"
    store = VectorStore(db, StubEmbedder(1024))
    store.add_chunks("doc1", {"title": "Doc 1"}, _chunks())
    del store

    reopened = VectorStore(db, StubEmbedder(1024))
    assert reopened.collection.metadata.get("embedding_dimensions") == 1024

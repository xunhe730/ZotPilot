"""Tests for ChromaDB vector store."""

from unittest.mock import patch

import pytest

from zotpilot.models import ExtractedFormula
from zotpilot.vector_store import (
    EmbeddingProviderUnavailableError,
    IndexUnavailableError,
    VectorStore,
    _probe_chroma_db_access,
)


@pytest.fixture
def store(tmp_path, mock_embedder):
    """Create a VectorStore with in-memory ChromaDB."""
    return VectorStore(tmp_path / "chroma", mock_embedder)


@pytest.fixture
def populated_store(store, sample_chunks):
    """Store with some chunks added."""
    doc_meta = {
        "title": "Test Paper",
        "authors": "Test Author",
        "year": 2020,
        "citation_key": "test2020",
        "publication": "Test Journal",
        "doi": "10.1234/test",
        "tags": "ml; ai",
        "collections": "Test Collection",
        "journal_quartile": "Q1",
        "pdf_hash": "abc123",
        "quality_grade": "A",
    }
    store.add_chunks("TEST001", doc_meta, sample_chunks)
    return store


class TestVectorStore:
    def test_add_and_count(self, populated_store):
        assert populated_store.count() == 3

    def test_get_indexed_doc_ids(self, populated_store):
        ids = populated_store.get_indexed_doc_ids()
        assert "TEST001" in ids

    def test_delete_document(self, populated_store):
        populated_store.delete_document("TEST001")
        assert populated_store.count() == 0

    def test_get_document_meta(self, populated_store):
        meta = populated_store.get_document_meta("TEST001")
        assert meta is not None
        assert meta["doc_title"] == "Test Paper"
        assert meta["year"] == 2020

    def test_get_document_meta_not_found(self, store):
        meta = store.get_document_meta("NONEXISTENT")
        assert meta is None

    def test_get_adjacent_chunks(self, populated_store):
        chunks = populated_store.get_adjacent_chunks("TEST001", 1, window=1)
        assert len(chunks) >= 1
        # Should include the center and at least one neighbor
        indices = {c.metadata["chunk_index"] for c in chunks}
        assert 1 in indices

    def test_search(self, populated_store):
        results = populated_store.search("neural networks", top_k=3)
        assert len(results) <= 3
        for r in results:
            assert r.score >= 0

    def test_empty_store_search(self, store):
        results = store.search("anything", top_k=5)
        assert results == []

    def test_add_formulas(self, store):
        doc_meta = {
            "title": "Plasticity Paper",
            "authors": "Test Author",
            "year": 2024,
            "citation_key": "test2024",
            "publication": "Mechanics Journal",
            "doi": "10.1234/formula",
            "tags": "constitutive-model",
            "collections": "Models",
            "journal_quartile": "Q1",
            "pdf_hash": "hash",
            "quality_grade": "A",
        }
        formula = ExtractedFormula(
            page_num=7,
            formula_index=0,
            bbox=(10.0, 20.0, 110.0, 45.0),
            latex=r"\sigma = E\varepsilon",
            confidence=0.98,
        )

        store.add_formulas("FORM001", doc_meta, [formula])

        results = store.search("stress strain constitutive", filters={"chunk_type": {"$eq": "formula"}})
        assert len(results) == 1
        assert results[0].metadata["chunk_type"] == "formula"
        assert results[0].metadata["latex"] == r"\sigma = E\varepsilon"
        assert results[0].metadata["confidence"] == 0.98

    def test_add_formulas_requires_embedder(self, tmp_path):
        store = VectorStore(tmp_path / "chroma", None)
        formula = ExtractedFormula(
            page_num=1,
            formula_index=0,
            bbox=(10.0, 20.0, 110.0, 45.0),
            latex=r"E = mc^2",
        )

        with pytest.raises(EmbeddingProviderUnavailableError, match="requires an embedding provider"):
            store.add_formulas("FORM002", {"title": "No Embedder"}, [formula])

    def test_delete_chunks_by_type_preserves_other_chunks(self, store, sample_chunks):
        doc_meta = {
            "title": "Mixed Index Paper",
            "authors": "Test Author",
            "year": 2024,
            "citation_key": "mixed2024",
            "publication": "Mechanics Journal",
            "doi": "10.1234/mixed",
            "tags": "",
            "collections": "",
            "journal_quartile": "Q1",
            "pdf_hash": "hash",
            "quality_grade": "A",
        }
        formula = ExtractedFormula(
            page_num=2,
            formula_index=0,
            bbox=(10.0, 20.0, 110.0, 45.0),
            latex=r"\varepsilon_f = f(\eta)",
        )
        store.add_chunks("MIXED001", doc_meta, sample_chunks)
        store.add_formulas("MIXED001", doc_meta, [formula])

        store.delete_chunks_by_type("MIXED001", "formula")

        assert store.search("fracture locus", filters={"chunk_type": {"$eq": "formula"}}) == []
        text_results = store.search("transformer architecture", filters={"chunk_type": {"$eq": "text"}})
        assert len(text_results) >= 1

    def test_probe_fail_on_read_path_raises_and_keeps_bytes(self, tmp_path, mock_embedder):
        """AC1: a probe/open failure on the READ path never moves/recreates the DB.

        It raises IndexUnavailableError and leaves the bytes 100% intact (nothing
        moved aside, no `chroma.corrupt-*` backup created).
        """
        db_path = tmp_path / "chroma"
        db_path.mkdir()
        (db_path / "chroma.sqlite3").write_text("broken")
        original_bytes = (db_path / "chroma.sqlite3").read_text()

        with patch("zotpilot.vector_store._probe_chroma_db_access", return_value=False):
            with pytest.raises(IndexUnavailableError):
                VectorStore(db_path, mock_embedder)

        # Bytes intact, nothing moved, no quarantine backup created.
        assert db_path.exists()
        assert (db_path / "chroma.sqlite3").read_text() == original_bytes
        assert list(tmp_path.glob("chroma.corrupt-*")) == []


class TestProbeChromaDbAccess:
    def test_missing_dir_is_openable(self, tmp_path):
        assert _probe_chroma_db_access(tmp_path / "does-not-exist") is True

    def test_empty_dir_is_openable(self, tmp_path):
        empty = tmp_path / "chroma"
        empty.mkdir()
        assert _probe_chroma_db_access(empty) is True

    def test_healthy_populated_dir_is_openable(self, populated_store):
        assert _probe_chroma_db_access(populated_store.db_path) is True

    def test_unreadable_dir_is_unavailable_without_raising(self, tmp_path):
        db_path = tmp_path / "chroma"
        db_path.mkdir()
        # Non-empty but not a valid Chroma store -> subprocess open fails (non-zero
        # exit). Must return False without raising on the READ path.
        (db_path / "chroma.sqlite3").write_text("not a real sqlite database")
        assert _probe_chroma_db_access(db_path) is False


class TestGuardedAdd:
    """A provider returning the wrong vector count must fail loudly, not silently
    misalign text↔embedding (chunk N served with chunk M's vector)."""

    def test_misaligned_lengths_raise(self, store):
        with pytest.raises(ValueError, match="misaligned"):
            store._guarded_add(["a", "b"], ["t1", "t2"], [[0.1]], [{}, {}])  # 1 embedding, 2 ids

    def test_aligned_calls_collection_add(self, store):
        from unittest.mock import MagicMock
        store.collection = MagicMock()
        store._guarded_add(["a"], ["t1"], [[0.1, 0.2]], [{"doc_id": "X"}])
        store.collection.add.assert_called_once()

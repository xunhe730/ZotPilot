"""Tests for ChromaDB vector store."""

from unittest.mock import patch

import pytest

from zotpilot.vector_store import (
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
        assert {c.metadata["chunk_type"] for c in chunks} == {"text"}

    def test_get_adjacent_chunks_excludes_formula_chunks(self, populated_store):
        from zotpilot.models import ExtractedFormula

        doc_meta = populated_store.get_document_meta("TEST001")
        populated_store.add_formulas(
            "TEST001",
            doc_meta,
            [
                ExtractedFormula(
                    page_num=2,
                    formula_index=0,
                    bbox=(1, 2, 3, 4),
                    latex=r"E = mc^2",
                ),
                ExtractedFormula(
                    page_num=2,
                    formula_index=1,
                    bbox=(4, 5, 6, 7),
                    latex=r"F = ma",
                ),
            ],
        )

        chunks = populated_store.get_adjacent_chunks("TEST001", 1, window=1)

        assert chunks
        assert all(chunk.metadata["chunk_type"] == "text" for chunk in chunks)
        assert all(not chunk.text.startswith("Formula on page") for chunk in chunks)

    def test_search(self, populated_store):
        results = populated_store.search("neural networks", top_k=3)
        assert len(results) <= 3
        for r in results:
            assert r.score >= 0

    def test_empty_store_search(self, store):
        results = store.search("anything", top_k=5)
        assert results == []

    def test_add_formulas_uses_real_chunk_indices_and_counts_type(self, populated_store):
        from zotpilot.models import ExtractedFormula

        doc_meta = populated_store.get_document_meta("TEST001")
        formulas = [
            ExtractedFormula(
                page_num=2,
                formula_index=0,
                bbox=(1, 2, 3, 4),
                latex=r"E = mc^2",
                confidence=0.91,
                reference_context="Energy is defined by the following equation.",
                equation_number="(1)",
            ),
            ExtractedFormula(
                page_num=3,
                formula_index=1,
                bbox=(5, 6, 7, 8),
                latex=r"L = \sum_i x_i",
                confidence=0.82,
            ),
        ]

        populated_store.add_formulas("TEST001", doc_meta, formulas)

        results = populated_store.collection.get(
            where={
                "$and": [
                    {"doc_id": {"$eq": "TEST001"}},
                    {"chunk_type": {"$eq": "formula"}},
                ]
            },
            include=["documents", "metadatas"],
        )
        indices = sorted(meta["chunk_index"] for meta in results["metadatas"])
        counts = populated_store.count_chunk_types({"TEST001"})

        assert indices == [0, 1]
        assert counts == {"text": 3, "table": 0, "figure": 0, "formula": 2}
        assert all(doc.startswith("Formula on page") for doc in results["documents"])
        assert populated_store._doc_id_from_chunk_id("TEST001_formula_0001") == "TEST001"

    def test_delete_chunks_by_type_removes_only_formulas(self, populated_store):
        from zotpilot.models import ExtractedFormula

        doc_meta = populated_store.get_document_meta("TEST001")
        populated_store.add_formulas(
            "TEST001",
            doc_meta,
            [ExtractedFormula(page_num=2, formula_index=0, bbox=(1, 2, 3, 4), latex=r"E = mc^2")],
        )

        populated_store.delete_chunks_by_type("TEST001", "formula")

        counts = populated_store.count_chunk_types({"TEST001"})
        assert counts == {"text": 3, "table": 0, "figure": 0, "formula": 0}

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

    def test_add_formulas_raises_on_embedding_count_mismatch(self, store, mock_embedder):
        from zotpilot.models import ExtractedFormula

        mock_embedder.embed.side_effect = None
        mock_embedder.embed.return_value = [[0.1] * 768]
        formulas = [
            ExtractedFormula(page_num=1, formula_index=0, bbox=(0, 0, 1, 1), latex=r"a=b"),
            ExtractedFormula(page_num=1, formula_index=1, bbox=(1, 1, 2, 2), latex=r"c=d"),
        ]

        with pytest.raises(ValueError, match="misaligned"):
            store.add_formulas("DOC", {"title": "Paper"}, formulas)

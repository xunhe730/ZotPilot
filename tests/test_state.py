"""Tests for shared state helpers in zotpilot.state."""
import threading
import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from zotpilot.models import RetrievalResult
from zotpilot.state import (
    _build_chromadb_filters,
    _apply_text_filters,
    _has_text_filters,
    _apply_required_terms,
    _contains_chinese,
    _result_to_dict,
    _merge_results_by_chunk,
)


# ---------------------------------------------------------------------------
# _build_chromadb_filters
# ---------------------------------------------------------------------------

class TestBuildChromadbFilters:
    def test_build_chromadb_filters_none(self):
        assert _build_chromadb_filters() is None

    def test_build_chromadb_filters_year_min(self):
        result = _build_chromadb_filters(year_min=2020)
        assert result == {"year": {"$gte": 2020}}

    def test_build_chromadb_filters_year_range(self):
        result = _build_chromadb_filters(year_min=2020, year_max=2023)
        assert result == {"$and": [{"year": {"$gte": 2020}}, {"year": {"$lte": 2023}}]}

    def test_build_chromadb_filters_chunk_types_single(self):
        result = _build_chromadb_filters(chunk_types=["table"])
        assert result == {"chunk_type": {"$eq": "table"}}

    def test_build_chromadb_filters_chunk_types_multiple(self):
        result = _build_chromadb_filters(chunk_types=["text", "figure"])
        assert result == {"chunk_type": {"$in": ["text", "figure"]}}


# ---------------------------------------------------------------------------
# _apply_text_filters
# ---------------------------------------------------------------------------

def _make_result_with_metadata(authors="", tags="", collections=""):
    """Create a SimpleNamespace with a .metadata dict for _apply_text_filters."""
    return SimpleNamespace(metadata={"authors": authors, "tags": tags, "collections": collections})


class TestApplyTextFilters:
    def test_apply_text_filters_author(self):
        r1 = _make_result_with_metadata(authors="Smith, J.; Doe, A.")
        r2 = _make_result_with_metadata(authors="Zhang, W.")
        filtered = _apply_text_filters([r1, r2], author="Smith")
        assert filtered == [r1]

    def test_apply_text_filters_tag(self):
        r1 = _make_result_with_metadata(tags="deep-learning; transformers")
        r2 = _make_result_with_metadata(tags="biology")
        filtered = _apply_text_filters([r1, r2], tag="transformer")
        assert filtered == [r1]

    def test_apply_text_filters_collection(self):
        r1 = _make_result_with_metadata(collections="Machine Learning")
        r2 = _make_result_with_metadata(collections="Chemistry")
        filtered = _apply_text_filters([r1, r2], collection="machine")
        assert filtered == [r1]

    def test_apply_text_filters_no_match(self):
        r1 = _make_result_with_metadata(authors="Smith")
        r2 = _make_result_with_metadata(authors="Doe")
        filtered = _apply_text_filters([r1, r2], author="nonexistent")
        assert filtered == []

    def test_apply_text_filters_none(self):
        r1 = _make_result_with_metadata(authors="Smith")
        r2 = _make_result_with_metadata(authors="Doe")
        filtered = _apply_text_filters([r1, r2])
        assert filtered == [r1, r2]


# ---------------------------------------------------------------------------
# _has_text_filters
# ---------------------------------------------------------------------------

class TestHasTextFilters:
    def test_has_text_filters_true(self):
        assert _has_text_filters(author="Smith", tag=None, collection=None) is True
        assert _has_text_filters(author=None, tag="ml", collection=None) is True
        assert _has_text_filters(author=None, tag=None, collection="CS") is True

    def test_has_text_filters_false(self):
        assert _has_text_filters(author=None, tag=None, collection=None) is False


# ---------------------------------------------------------------------------
# _apply_required_terms
# ---------------------------------------------------------------------------

class TestApplyRequiredTerms:
    def test_apply_required_terms(self):
        r1 = SimpleNamespace(text="The transformer model is effective")
        r2 = SimpleNamespace(text="A CNN approach")
        filtered = _apply_required_terms([r1, r2], ["transformer"])
        assert filtered == [r1]

    def test_apply_required_terms_case_insensitive(self):
        r1 = SimpleNamespace(text="The Transformer model is effective")
        filtered = _apply_required_terms([r1], ["transformer"])
        assert filtered == [r1]


# ---------------------------------------------------------------------------
# _contains_chinese
# ---------------------------------------------------------------------------

class TestContainsChinese:
    def test_contains_chinese(self):
        assert _contains_chinese("深度学习研究") is True
        assert _contains_chinese("mixed 中文 text") is True

    def test_contains_chinese_false(self):
        assert _contains_chinese("deep learning research") is False
        assert _contains_chinese("") is False


# ---------------------------------------------------------------------------
# _result_to_dict
# ---------------------------------------------------------------------------

class TestResultToDict:
    def test_result_to_dict(self):
        r = RetrievalResult(
            chunk_id="DOC1_chunk_0001",
            text="Some passage text",
            score=0.85,
            doc_id="DOC1",
            doc_title="Test Paper",
            authors="Smith, J.",
            year=2021,
            page_num=3,
            chunk_index=1,
            citation_key="smith2021",
            publication="Nature",
            section="results",
            section_confidence=0.9,
            journal_quartile="Q1",
            composite_score=0.78,
            context_before=["Before text"],
            context_after=["After text"],
        )
        d = _result_to_dict(r)
        assert d["doc_title"] == "Test Paper"
        assert d["authors"] == "Smith, J."
        assert d["year"] == 2021
        assert d["citation_key"] == "smith2021"
        assert d["publication"] == "Nature"
        assert d["page"] == 3
        assert d["relevance_score"] == 0.85
        assert d["composite_score"] == 0.78
        assert d["section"] == "results"
        assert d["section_confidence"] == 0.9
        assert d["journal_quartile"] == "Q1"
        assert d["passage"] == "Some passage text"
        assert d["context_before"] == ["Before text"]
        assert d["context_after"] == ["After text"]
        assert d["doc_id"] == "DOC1"
        assert d["chunk_index"] == 1
        assert "full_context" in d


# ---------------------------------------------------------------------------
# _merge_results_by_chunk
# ---------------------------------------------------------------------------

class TestMergeResultsByChunk:
    def _make_rr(self, doc_id, chunk_index, score, composite_score=None):
        return RetrievalResult(
            chunk_id=f"{doc_id}_chunk_{chunk_index:04d}",
            text="text",
            score=score,
            doc_id=doc_id,
            doc_title="Title",
            authors="Author",
            year=2021,
            page_num=1,
            chunk_index=chunk_index,
            composite_score=composite_score,
        )

    def test_merge_results_by_chunk(self):
        r1 = self._make_rr("DOC1", 0, 0.9, 0.8)
        r2 = self._make_rr("DOC1", 0, 0.7, 0.6)  # duplicate, lower score
        r3 = self._make_rr("DOC2", 1, 0.5, 0.5)
        merged = _merge_results_by_chunk([r1], [r2, r3], top_k=10)
        # Should deduplicate DOC1/chunk_index=0, keeping r1 (higher composite)
        assert len(merged) == 2
        ids = [(r.doc_id, r.chunk_index) for r in merged]
        assert ("DOC1", 0) in ids
        assert ("DOC2", 1) in ids
        # The kept DOC1 result should have the higher composite_score
        doc1_result = [r for r in merged if r.doc_id == "DOC1"][0]
        assert doc1_result.composite_score == 0.8


# ---------------------------------------------------------------------------
# Thread-safety: _get_config() returns same instance across threads
# ---------------------------------------------------------------------------

class TestThreadSafety:
    def test_get_config_concurrent(self):
        """10 threads calling _get_config() should all get the same instance."""
        import zotpilot.state as state_mod

        mock_config = MagicMock()
        original_config = state_mod._config
        original_lock = state_mod._init_lock

        try:
            # Reset to force initialization
            state_mod._config = None

            with patch("zotpilot.state.Config.load", return_value=mock_config):
                results = [None] * 10
                def worker(idx):
                    results[idx] = state_mod._get_config()

                threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()

                # All threads should get the exact same object
                assert all(r is results[0] for r in results)
        finally:
            state_mod._config = original_config

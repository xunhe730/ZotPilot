"""Tests for the reranker."""
from zotpilot.models import RetrievalResult
from zotpilot.reranker import (
    Reranker,
    validate_journal_weights,
    validate_section_weights,
)


def _make_result(score=0.8, section="results", journal_quartile="Q1", **kwargs):
    defaults = dict(
        chunk_id="test_chunk_0001",
        text="test text",
        doc_id="TEST",
        doc_title="Test Paper",
        authors="Author",
        year=2020,
        page_num=1,
        chunk_index=0,
    )
    defaults.update(kwargs)
    return RetrievalResult(
        score=score,
        section=section,
        journal_quartile=journal_quartile,
        **defaults,
    )


class TestReranker:
    def test_empty_results(self):
        reranker = Reranker()
        assert reranker.rerank([]) == []

    def test_composite_score_populated(self):
        reranker = Reranker()
        results = [_make_result(score=0.9)]
        reranked = reranker.rerank(results)
        assert len(reranked) == 1
        assert reranked[0].composite_score is not None
        assert reranked[0].composite_score > 0

    def test_sorting_by_composite_score(self):
        reranker = Reranker()
        results = [
            _make_result(score=0.5, section="references", journal_quartile="Q4"),
            _make_result(score=0.9, section="results", journal_quartile="Q1"),
        ]
        reranked = reranker.rerank(results)
        assert reranked[0].score == 0.9  # Higher composite should come first

    def test_section_weight_override(self):
        reranker = Reranker()
        results = [
            _make_result(score=0.8, section="introduction"),
            _make_result(score=0.8, section="results"),
        ]
        # Boost introduction, suppress results
        reranked = reranker.rerank(results, section_weights={"introduction": 1.0, "results": 0.1})
        assert reranked[0].section == "introduction"

    def test_zero_weight_excludes(self):
        reranker = Reranker()
        results = [
            _make_result(score=0.8, section="references"),
        ]
        reranked = reranker.rerank(results, section_weights={"references": 0.0})
        assert len(reranked) == 0  # Zero composite excluded

    def test_journal_weight_effect(self):
        reranker = Reranker()
        r1 = _make_result(score=0.8, journal_quartile="Q1")
        r2 = _make_result(score=0.8, journal_quartile="Q4")
        reranked = reranker.rerank([r1, r2])
        assert reranked[0].journal_quartile == "Q1"

    def test_score_result_single(self):
        reranker = Reranker()
        result = _make_result(score=0.8, section="results", journal_quartile="Q1")
        score = reranker.score_result(result)
        assert score > 0
        assert score <= 1.0

    def test_alpha_effect(self):
        r = _make_result(score=0.5)
        low_alpha = Reranker(alpha=0.3)
        high_alpha = Reranker(alpha=1.0)
        # Lower alpha compresses similarity range
        low_score = low_alpha.score_result(r)
        high_score = high_alpha.score_result(r)
        assert low_score > high_score  # 0.5^0.3 > 0.5^1.0


class TestValidation:
    def test_valid_section_weights(self):
        errors = validate_section_weights({"results": 1.0, "methods": 0.5})
        assert errors == []

    def test_invalid_section_key(self):
        errors = validate_section_weights({"not_a_section": 1.0})
        assert len(errors) == 1
        assert "Unknown section" in errors[0]

    def test_invalid_section_value(self):
        errors = validate_section_weights({"results": "high"})
        assert len(errors) == 1
        assert "numeric" in errors[0]

    def test_valid_journal_weights(self):
        errors = validate_journal_weights({"Q1": 1.0, "unknown": 0.5})
        assert errors == []

    def test_invalid_quartile(self):
        errors = validate_journal_weights({"Q5": 1.0})
        assert len(errors) == 1
        assert "Unknown quartile" in errors[0]

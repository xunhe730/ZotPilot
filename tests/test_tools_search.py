"""Tests for search tool functions."""
from unittest.mock import MagicMock, patch

import pytest

from zotpilot.models import RetrievalResult, ZoteroItem


def _make_rr(doc_id="DOC1", chunk_index=0, score=0.9, composite_score=0.8, text="some text"):
    return RetrievalResult(
        chunk_id=f"{doc_id}_chunk_{chunk_index:04d}",
        text=text,
        score=score,
        doc_id=doc_id,
        doc_title="Test Paper",
        authors="Smith, J.",
        year=2021,
        page_num=1,
        chunk_index=chunk_index,
        citation_key="smith2021",
        publication="Nature",
        section="results",
        section_confidence=0.9,
        journal_quartile="Q1",
        composite_score=composite_score,
    )


def _make_config():
    cfg = MagicMock()
    cfg.oversample_multiplier = 3
    cfg.oversample_topic_factor = 5
    cfg.rerank_enabled = True
    cfg.rerank_alpha = 0.5
    return cfg


def _pdf_item(key: str):
    item = MagicMock()
    item.item_key = key
    item.pdf_path = MagicMock()
    item.pdf_path.exists.return_value = True
    return item


@pytest.fixture
def mock_singletons():
    """Patch all state singletons for search tool tests."""
    retriever = MagicMock()
    reranker = MagicMock()
    store = MagicMock()
    config = _make_config()

    with patch("zotpilot.tools.search._get_retriever", return_value=retriever), \
         patch("zotpilot.tools.search._get_reranker", return_value=reranker), \
         patch("zotpilot.tools.search._get_store", return_value=store), \
         patch("zotpilot.tools.search._get_store_optional", return_value=store), \
         patch("zotpilot.tools.search._get_config", return_value=config), \
         patch("zotpilot.tools.search._get_zotero") as mock_zotero:
        mock_zotero.return_value.get_all_items_with_pdfs.return_value = [
            _pdf_item("DOC1"),
            _pdf_item("A"),
            _pdf_item("B"),
        ]
        yield {
            "retriever": retriever,
            "reranker": reranker,
            "store": store,
            "config": config,
            "zotero": mock_zotero,
        }


class TestSearchPapers:
    def test_happy_path(self, mock_singletons):
        from zotpilot.tools.search import search_papers
        results = [_make_rr()]
        mock_singletons["retriever"].search.return_value = results
        mock_singletons["reranker"].rerank.return_value = results

        output = search_papers(query="test query", top_k=5)
        assert len(output) == 1
        assert output[0]["doc_id"] == "DOC1"
        assert output[0]["passage"] == "some text"
        assert "authors" not in output[0]
        mock_singletons["retriever"].search.assert_called_once_with(
            query="test query",
            top_k=15,
            context_window=0,
            filters=None,
        )

    def test_invalid_chunk_types_raises(self, mock_singletons):
        from zotpilot.tools.search import search_papers
        with pytest.raises(Exception, match="Invalid chunk_types"):
            search_papers(query="test", chunk_types=["invalid_type"])

    def test_empty_results(self, mock_singletons):
        from zotpilot.tools.search import search_papers
        mock_singletons["retriever"].search.return_value = []
        mock_singletons["reranker"].rerank.return_value = []
        output = search_papers(query="test")
        assert output == []

    def test_filters_orphaned_doc_ids(self, mock_singletons):
        from zotpilot.tools.search import search_papers

        current = MagicMock()
        current.item_key = "DOC1"
        current.pdf_path = MagicMock()
        current.pdf_path.exists.return_value = True
        mock_singletons["zotero"].return_value.get_all_items_with_pdfs.return_value = [current]

        results = [_make_rr(doc_id="ORPHAN", score=0.95), _make_rr(doc_id="DOC1", score=0.90)]
        mock_singletons["retriever"].search.return_value = results
        mock_singletons["reranker"].rerank.side_effect = lambda rows, *_args, **_kwargs: rows

        output = search_papers(query="test query", top_k=5)

        assert [row["doc_id"] for row in output] == ["DOC1"]

    def test_section_type_formulas_dispatches(self, mock_singletons):
        from zotpilot.tools.search import search_papers

        with patch("zotpilot.tools.search.search_formulas", return_value=[{"doc_id": "DOC1"}]) as mock_search:
            output = search_papers(query="formula meaning", section_type="formulas")

        assert output == [{"doc_id": "DOC1"}]
        mock_search.assert_called_once()

    def test_formula_chunk_type_is_valid(self, mock_singletons):
        from zotpilot.tools.search import search_papers

        results = [_make_rr()]
        mock_singletons["retriever"].search.return_value = results
        mock_singletons["reranker"].rerank.return_value = results

        search_papers(query="test", chunk_types=["formula"])

        _args, kwargs = mock_singletons["retriever"].search.call_args
        assert kwargs["filters"] == {"chunk_type": {"$eq": "formula"}}


class TestSearchTopic:
    def test_happy_path(self, mock_singletons):
        from zotpilot.tools.search import search_topic
        results = [_make_rr(doc_id="A", composite_score=0.9), _make_rr(doc_id="B", composite_score=0.7)]
        mock_singletons["retriever"].search.return_value = results
        mock_singletons["reranker"].rerank.return_value = results

        output = search_topic(query="neural networks", num_papers=5)
        assert len(output) == 2
        assert "item_key" not in output[0]
        assert "best_passage" not in output[0]
        assert "best_passage_chunk_index" in output[0]
        assert "best_passage_context" not in output[0]
        mock_singletons["retriever"].search.assert_called_once_with(
            query="neural networks",
            top_k=50,
            context_window=0,
            filters=None,
        )

    def test_invalid_chunk_types_raises(self, mock_singletons):
        from zotpilot.tools.search import search_topic
        with pytest.raises(Exception, match="Invalid chunk_types"):
            search_topic(query="test", chunk_types=["bad"])


class TestSearchBoolean:
    def test_uses_get_zotero_singleton(self, mock_singletons):
        from zotpilot.tools.search import search_boolean
        zotero = MagicMock()
        zotero.search_fulltext.return_value = {"KEY1"}
        item = ZoteroItem(
            item_key="KEY1", title="Paper", authors="Auth",
            year=2021, pdf_path=None, citation_key="auth2021",
            publication="J", doi="10/x", tags="ml", collections="CS",
        )
        zotero.get_all_items_with_pdfs.return_value = [item]
        mock_singletons["zotero"].return_value = zotero

        output = search_boolean(query="test words")
        assert len(output) == 1
        assert output[0]["doc_id"] == "KEY1"
        assert "item_key" not in output[0]

    def test_empty_query(self, mock_singletons):
        from zotpilot.tools.search import search_boolean
        zotero = MagicMock()
        zotero.search_fulltext.return_value = set()
        mock_singletons["zotero"].return_value = zotero
        output = search_boolean(query="nonexistent")
        assert output == []


class TestSearchFormulas:
    def test_returns_formula_metadata_and_context(self, mock_singletons):
        from dataclasses import replace

        from zotpilot.tools.search import search_formulas

        chunk = MagicMock()
        chunk.id = "DOC1_formula_0000"
        chunk.text = "Formula on page 4 (2)\nContext: loss is minimized.\nLaTeX: L = \\sum_i x_i"
        chunk.score = 0.91
        chunk.metadata = {
            "doc_id": "DOC1",
            "doc_title": "Formula Paper",
            "authors": "Smith",
            "year": 2024,
            "page_num": 4,
            "chunk_index": 0,
            "formula_index": 0,
            "citation_key": "smith2024",
            "publication": "NeurIPS",
            "section": "formula",
            "section_confidence": 1.0,
            "journal_quartile": "Q1",
            "chunk_type": "formula",
            "formula_latex": r"L = \sum_i x_i",
            "formula_equation_number": "(2)",
            "formula_variable_gloss": "where x_i is the token score",
            "formula_confidence": 0.86,
            "formula_provider": "local",
            "formula_source": "text_block",
            "reference_context": "The objective minimizes the following loss.",
            "formula_raw_text": "L = sum_i x_i (2)",
            "bbox": "1,2,3,4",
        }
        mock_singletons["store"].search.return_value = [chunk]
        mock_singletons["reranker"].rerank.side_effect = (
            lambda rows, *_args, **_kwargs: [replace(rows[0], composite_score=0.77)]
        )

        output = search_formulas("loss objective", verbosity="full")

        assert output[0]["doc_id"] == "DOC1"
        assert output[0]["latex"] == r"L = \sum_i x_i"
        assert output[0]["equation_number"] == "(2)"
        assert output[0]["variable_gloss"] == "where x_i is the token score"
        assert output[0]["reference_context"] == "The objective minimizes the following loss."
        assert output[0]["formula_provider"] == "local"
        assert output[0]["raw_text"] == "L = sum_i x_i (2)"

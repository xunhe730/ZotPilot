"""Tests for search tool functions."""
import pytest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock
from dataclasses import replace

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
         patch("zotpilot.tools.search._get_config", return_value=config), \
         patch("zotpilot.tools.search._get_zotero") as mock_zotero:
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


class TestSearchTopic:
    def test_happy_path(self, mock_singletons):
        from zotpilot.tools.search import search_topic
        results = [_make_rr(doc_id="A", composite_score=0.9), _make_rr(doc_id="B", composite_score=0.7)]
        mock_singletons["retriever"].search.return_value = results
        mock_singletons["reranker"].rerank.return_value = results

        output = search_topic(query="neural networks", num_papers=5)
        assert len(output) == 2
        assert "item_key" not in output[0]
        assert "best_passage_context" not in output[0]
        mock_singletons["retriever"].search.assert_called_once_with(
            query="neural networks",
            top_k=75,
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
        assert output[0]["item_key"] == "KEY1"
        assert output[0]["doc_id"] == "KEY1"

    def test_empty_query(self, mock_singletons):
        from zotpilot.tools.search import search_boolean
        zotero = MagicMock()
        zotero.search_fulltext.return_value = set()
        mock_singletons["zotero"].return_value = zotero
        output = search_boolean(query="nonexistent")
        assert output == []

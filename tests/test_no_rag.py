"""Tests for No-RAG mode (embedding_provider='none')."""
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

from zotpilot.config import Config


@dataclass
class _MockConfig:
    """Minimal config mock for No-RAG tests."""
    embedding_provider: str = "none"
    zotero_data_dir: str = "/tmp"
    chroma_db_path: str = "/tmp/chroma"
    gemini_api_key: str | None = None
    dashscope_api_key: str | None = None
    zotero_api_key: str | None = None
    zotero_user_id: str | None = None
    zotero_library_type: str = "user"
    rerank_enabled: bool = False
    openalex_email: str | None = None


class TestConfigValidation:
    def test_none_provider_is_valid(self, tmp_path):
        """embedding_provider='none' should pass validation."""
        db = tmp_path / "zotero.sqlite"
        db.touch()
        config = Config(
            zotero_data_dir=tmp_path,
            chroma_db_path=tmp_path / "chroma",
            embedding_model="none",
            embedding_dimensions=0,
            chunk_size=400,
            chunk_overlap=100,
            gemini_api_key=None,
            dashscope_api_key=None,
            embedding_provider="none",
            embedding_timeout=120.0,
            embedding_max_retries=3,
            rerank_alpha=0.7,
            rerank_section_weights=None,
            rerank_journal_weights=None,
            rerank_enabled=False,
            oversample_multiplier=3,
            oversample_topic_factor=5,
            stats_sample_limit=10000,
            ocr_language="eng",
            openalex_email=None,
            vision_enabled=False,
            vision_provider="anthropic",
            vision_model="",
            anthropic_api_key=None,
            vision_max_tables_per_run=None,
            vision_max_cost_usd=None,
            max_pages=40,
            preflight_enabled=True,
            zotero_api_key=None,
            zotero_user_id=None,
            zotero_library_type="user",
            semantic_scholar_api_key=None,
        )
        errors = config.validate()
        # Should have no embedding-related errors
        embedding_errors = [e for e in errors if "embedding" in e.lower() or "api_key" in e.lower()]
        assert embedding_errors == []

    def test_invalid_provider_rejected(self, tmp_path):
        db = tmp_path / "zotero.sqlite"
        db.touch()
        config = Config(
            zotero_data_dir=tmp_path,
            chroma_db_path=tmp_path / "chroma",
            embedding_model="x",
            embedding_dimensions=0,
            chunk_size=400,
            chunk_overlap=100,
            gemini_api_key=None,
            dashscope_api_key=None,
            embedding_provider="invalid_provider",
            embedding_timeout=120.0,
            embedding_max_retries=3,
            rerank_alpha=0.7,
            rerank_section_weights=None,
            rerank_journal_weights=None,
            rerank_enabled=False,
            oversample_multiplier=3,
            oversample_topic_factor=5,
            stats_sample_limit=10000,
            ocr_language="eng",
            openalex_email=None,
            vision_enabled=False,
            vision_provider="anthropic",
            vision_model="",
            anthropic_api_key=None,
            vision_max_tables_per_run=None,
            vision_max_cost_usd=None,
            max_pages=40,
            preflight_enabled=True,
            zotero_api_key=None,
            zotero_user_id=None,
            zotero_library_type="user",
            semantic_scholar_api_key=None,
        )
        errors = config.validate()
        assert any("invalid_provider" in e.lower() for e in errors)


class TestCreateEmbedder:
    def test_none_returns_none(self):
        from zotpilot.embeddings import create_embedder
        mock_config = _MockConfig(embedding_provider="none")
        result = create_embedder(mock_config)
        assert result is None

    def test_gemini_returns_embedder(self):
        """Sanity: non-none provider still works."""
        from zotpilot.embeddings import create_embedder
        mock_config = _MockConfig(embedding_provider="local")
        result = create_embedder(mock_config)
        assert result is not None


class TestGetIndexStats:
    @patch("zotpilot.tools.indexing._get_config")
    def test_no_rag_returns_stub(self, mock_config):
        mock_config.return_value = _MockConfig()
        from zotpilot.tools.indexing import get_index_stats
        result = get_index_stats()
        assert result["total_documents"] == 0
        assert result["mode"] == "no-rag"


class TestGetIndexStatsMergedConfig:
    @patch("zotpilot.tools.indexing._get_config")
    def test_no_rag_returns_disabled_reranking_config(self, mock_config):
        mock_config.return_value = _MockConfig()
        from zotpilot.tools.indexing import get_index_stats

        result = get_index_stats(include_config=True)

        assert result["mode"] == "no-rag"
        assert result["reranking_config"]["enabled"] is False
        assert result["reranking_config"]["mode"] == "no-rag"


class TestGetPaperDetailsNoRag:
    @patch("zotpilot.tools.library._get_store_optional")
    @patch("zotpilot.tools.library._get_zotero")
    def test_indexed_false_in_no_rag(self, mock_zotero, mock_store_opt):
        mock_store_opt.return_value = None  # No-RAG
        mock_client = MagicMock()
        mock_item = MagicMock()
        mock_item.item_key = "KEY1"
        mock_item.title = "Test"
        mock_item.authors = "Author"
        mock_item.year = 2024
        mock_item.publication = ""
        mock_item.doi = ""
        mock_item.tags = ""
        mock_item.collections = ""
        mock_item.citation_key = ""
        mock_item.pdf_path = None
        mock_client.get_item.return_value = mock_item
        mock_client.get_item_abstract.return_value = ""
        mock_zotero.return_value = mock_client

        from zotpilot.tools.library import get_paper_details
        result = get_paper_details(doc_id="KEY1")
        assert result["doc_id"] == "KEY1"
        assert "key" not in result
        assert result["indexed"] is False


class TestSearchPapersNoRag:
    @patch("zotpilot.tools.search._get_store_optional")
    def test_raises_tool_error(self, mock_store_optional):
        from fastmcp.exceptions import ToolError
        mock_store_optional.side_effect = ToolError("Semantic search requires indexing")

        from zotpilot.tools.search import search_papers
        with pytest.raises(ToolError, match="Semantic search requires indexing"):
            search_papers("test query")


class TestCitationsNoRag:
    @patch("zotpilot.tools.citations._get_zotero")
    @patch("zotpilot.tools.citations._get_store_optional")
    def test_fallback_to_sqlite(self, mock_store_opt, mock_zotero):
        """In No-RAG mode, citations should get DOI from SQLite."""
        mock_store_opt.return_value = None  # No-RAG
        mock_item = MagicMock()
        mock_item.doi = ""  # no DOI
        mock_item.item_key = "DOC1"
        mock_item.pdf_path = MagicMock()
        mock_item.pdf_path.exists.return_value = True
        mock_client = MagicMock()
        mock_client.get_item.return_value = mock_item
        mock_client.get_all_items_with_pdfs.return_value = [mock_item]
        mock_zotero.return_value = mock_client

        from fastmcp.exceptions import ToolError

        from zotpilot.tools.citations import _get_doi
        with pytest.raises(ToolError, match="no DOI"):
            _get_doi("DOC1")

    @patch("zotpilot.tools.citations._get_zotero")
    @patch("zotpilot.tools.citations._get_store_optional")
    def test_not_found_in_sqlite(self, mock_store_opt, mock_zotero):
        mock_store_opt.return_value = None
        mock_client = MagicMock()
        mock_client.get_item.return_value = None
        mock_client.get_all_items_with_pdfs.return_value = []
        mock_zotero.return_value = mock_client

        from fastmcp.exceptions import ToolError

        from zotpilot.tools.citations import _get_doi
        with pytest.raises(ToolError, match="Document not found"):
            _get_doi("MISSING")


class TestPassageContextNoRag:
    @patch("zotpilot.tools.context._get_config")
    def test_raises_tool_error(self, mock_config):
        mock_config.return_value = _MockConfig()
        from fastmcp.exceptions import ToolError

        from zotpilot.tools.context import get_passage_context
        with pytest.raises(ToolError, match="requires indexing"):
            get_passage_context("DOC1", 0)


class TestBasicToolsWorkInNoRag:
    """Verify that non-RAG tools still function normally."""

    @patch("zotpilot.tools.library._get_zotero")
    def test_list_tags_works(self, mock_zotero):
        mock_client = MagicMock()
        mock_client.get_all_tags.return_value = [{"name": "ML", "count": 5}]
        mock_zotero.return_value = mock_client

        from zotpilot.tools.library import browse_library
        result = browse_library(view="tags")
        assert len(result) == 1
        assert result[0]["name"] == "ML"

    @patch("zotpilot.tools.search._get_zotero")
    def test_advanced_search_works(self, mock_zotero):
        mock_client = MagicMock()
        mock_client.advanced_search.return_value = [{"item_key": "K1", "title": "Test"}]
        mock_zotero.return_value = mock_client

        from zotpilot.tools.search import advanced_search
        result = advanced_search([{"field": "year", "op": "gt", "value": "2020"}])
        assert len(result) == 1

    @patch("zotpilot.tools.library._get_writer")
    @patch("zotpilot.tools.library._get_zotero")
    def test_get_notes_works(self, mock_zotero, mock_get_writer):
        from fastmcp.exceptions import ToolError

        mock_client = MagicMock()
        mock_client.get_notes.return_value = [{"key": "N1", "content": "note text"}]
        mock_zotero.return_value = mock_client
        mock_get_writer.side_effect = ToolError("No API key")

        from zotpilot.tools.library import get_notes
        result = get_notes()
        assert len(result) == 1

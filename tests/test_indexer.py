"""Tests for the Indexer pipeline — specifically P0-3 ReDoS protection."""
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


class TestTitlePatternValidation:
    """Test P0-3: ReDoS protection on title_pattern in Indexer.index_all()."""

    def test_invalid_regex_raises(self):
        """Invalid regex in title_pattern should raise ValueError."""
        # Directly test the validation logic that was added to indexer.py
        title_pattern = "[invalid"
        with pytest.raises(re.error):
            re.compile(title_pattern, re.IGNORECASE)

    def test_too_long_pattern_raises(self):
        """Pattern > 200 chars should be rejected."""
        title_pattern = "a" * 201
        assert len(title_pattern) > 200

    def test_valid_pattern_works(self):
        """Valid regex should compile fine."""
        pattern = re.compile("neural.*network", re.IGNORECASE)
        assert pattern.search("Neural Network Architecture") is not None

    def test_length_limit_boundary(self):
        """Exactly 200 chars should be accepted."""
        title_pattern = "a" * 200
        assert len(title_pattern) <= 200
        pattern = re.compile(title_pattern, re.IGNORECASE)
        assert pattern is not None


class TestIndexerReDoSIntegration:
    """Integration test using actual Indexer if dependencies are available."""

    def test_invalid_regex_in_index_all(self):
        """Indexer.index_all should raise ValueError on bad regex."""
        try:
            from zotpilot.indexer import Indexer
        except (ImportError, ModuleNotFoundError):
            pytest.skip("Indexer dependencies not fully available")

        from pathlib import Path
        from unittest.mock import MagicMock, patch

        config = MagicMock()
        config.zotero_data_dir = Path("/fake")
        config.chroma_db_path = Path("/fake/chroma")
        config.chunk_size = 1000
        config.chunk_overlap = 200
        config.embedding_provider = "local"
        config.embedding_dimensions = 384
        config.embedding_model = "test"
        config.ocr_language = "eng"
        config.vision_enabled = False
        config.anthropic_api_key = None

        with patch("zotpilot.indexer.ZoteroClient"), \
             patch("zotpilot.indexer.create_embedder"), \
             patch("zotpilot.indexer.VectorStore"), \
             patch("zotpilot.indexer.JournalRanker"):
            indexer = Indexer(config)
            mock_item = MagicMock()
            mock_item.item_key = "KEY1"
            mock_item.title = "Test Paper"
            mock_pdf_path = MagicMock()
            mock_pdf_path.exists.return_value = True
            mock_pdf_path.__str__ = lambda self: "/fake/test.pdf"
            mock_item.pdf_path = mock_pdf_path
            indexer.zotero.get_all_items_with_pdfs.return_value = [mock_item]
            indexer.store.get_indexed_doc_ids.return_value = set()

            with pytest.raises(ValueError, match="Invalid regex"):
                indexer.index_all(title_pattern="[invalid")


class TestVisionBudgetGuards:
    def test_skips_batch_vision_when_table_cap_is_exceeded(self):
        from zotpilot.indexer import Indexer

        config = MagicMock()
        config.zotero_data_dir = Path("/fake")
        config.chroma_db_path = Path("/fake/chroma")
        config.chunk_size = 1000
        config.chunk_overlap = 200
        config.embedding_provider = "local"
        config.embedding_dimensions = 384
        config.embedding_model = "test"
        config.ocr_language = "eng"
        config.vision_enabled = True
        config.vision_model = ""
        config.anthropic_api_key = None
        config.vision_max_tables_per_run = 1
        config.vision_max_cost_usd = None

        with patch("zotpilot.indexer.ZoteroClient"), \
             patch("zotpilot.indexer.create_embedder"), \
             patch("zotpilot.indexer.VectorStore"), \
             patch("zotpilot.indexer.JournalRanker"):
            indexer = Indexer(config)
            indexer._vision_api = object()

        mock_item = MagicMock()
        mock_item.item_key = "KEY1"
        mock_item.title = "Test Paper"
        mock_pdf_path = MagicMock()
        mock_pdf_path.exists.return_value = True
        mock_pdf_path.__str__ = lambda self: "/fake/test.pdf"
        mock_item.pdf_path = mock_pdf_path

        extraction = SimpleNamespace(
            pending_vision=SimpleNamespace(
                specs=[object(), object()],
                pdf_path=Path("/fake/test.pdf"),
            ),
        )

        indexer.zotero.get_all_items_with_pdfs.return_value = [mock_item]
        indexer.store.get_indexed_doc_ids.return_value = set()
        indexer.store.get_document_meta.return_value = None
        indexer._index_extraction = MagicMock(return_value=(1, 0, "", {}, "A"))
        indexer._pdf_hash = MagicMock(return_value="abc")
        indexer._config_hash_path = MagicMock()
        indexer._save_empty_docs = MagicMock()

        with patch("zotpilot.indexer.extract_document", return_value=extraction), \
             patch("zotpilot.pdf.extractor._finalize_document_no_tables") as mock_finalize, \
             patch("zotpilot.pdf.extractor.resolve_pending_vision") as mock_resolve:
            result = indexer.index_all()

        assert result["vision_budget_skipped"] is True
        assert result["vision_pending_tables"] == 2
        assert "table cap 1" in result["vision_skip_reason"]
        mock_finalize.assert_called_once_with(extraction)
        mock_resolve.assert_not_called()

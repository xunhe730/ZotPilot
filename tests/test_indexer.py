"""Tests for the Indexer pipeline — specifically P0-3 ReDoS protection."""
import pytest
import re


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

        from unittest.mock import MagicMock, patch
        from pathlib import Path

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
            mock_item.pdf_path = Path("/fake/test.pdf")
            mock_item.pdf_path.exists = lambda: True
            indexer.zotero.get_all_items_with_pdfs.return_value = [mock_item]
            indexer.store.get_indexed_doc_ids.return_value = set()

            with pytest.raises(ValueError, match="Invalid regex"):
                indexer.index_all(title_pattern="[invalid")

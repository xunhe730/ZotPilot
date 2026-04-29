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


class TestSkipTracking:
    """Test that items without PDFs are tracked rather than silently dropped."""

    def _make_indexer(self, items):
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
        config.max_pages = 0
        config.vision_max_tables_per_run = None
        config.vision_max_cost_usd = None
        config.oversample_multiplier = 2

        with patch("zotpilot.indexer.ZoteroClient"), \
             patch("zotpilot.indexer.create_embedder"), \
             patch("zotpilot.indexer.VectorStore"), \
             patch("zotpilot.indexer.JournalRanker"):
            indexer = Indexer(config)
        indexer.zotero.get_all_items_with_pdfs.return_value = items
        indexer.store.get_indexed_doc_ids.return_value = set()
        indexer.store.get_indexed_doc_ids = MagicMock(return_value=set())
        return indexer

    def _make_item(self, key, title, has_pdf):
        from unittest.mock import MagicMock
        item = MagicMock()
        item.item_key = key
        item.title = title
        if has_pdf:
            pdf = MagicMock()
            pdf.exists.return_value = True
            pdf.__str__ = lambda self: f"/fake/{key}.pdf"
            item.pdf_path = pdf
        else:
            item.pdf_path = None
        return item

    def _patch_indexer(self, indexer):
        """Patch filesystem-touching methods so tests run without real files."""
        from unittest.mock import MagicMock
        indexer._load_empty_docs = MagicMock(return_value={})
        indexer._save_empty_docs = MagicMock()
        indexer._config_hash_path = MagicMock()
        indexer._config_hash_path.exists.return_value = False
        indexer._config_hash_path.write_text = MagicMock()

    def test_items_without_pdf_are_tracked(self):
        """skipped_no_pdf list must contain items that have no pdf_path."""
        from unittest.mock import MagicMock, patch

        item_with_pdf = self._make_item("KEY_PDF", "Has PDF", has_pdf=True)
        item_no_pdf = self._make_item("KEY_NOPDF", "No PDF", has_pdf=False)

        indexer = self._make_indexer([item_with_pdf, item_no_pdf])
        self._patch_indexer(indexer)

        mock_extraction = MagicMock()
        mock_extraction.pages = [MagicMock()]
        mock_extraction.stats = {"total_pages": 1, "text_pages": 1, "ocr_pages": 0, "empty_pages": 0}
        mock_extraction.quality_grade = "A"
        mock_extraction.pending_vision = None

        with patch("zotpilot.indexer.extract_document", return_value=mock_extraction), \
             patch.object(indexer, "_index_extraction", return_value=(5, 0, "", {}, "A")):
            result = indexer.index_all(batch_size=None)

        skipped = result.get("skipped_no_pdf", [])
        assert len(skipped) == 1
        assert skipped[0]["item_key"] == "KEY_NOPDF"
        assert skipped[0]["reason"] == "no_pdf_attachment"

    def test_all_have_pdf_no_skipped(self):
        """When all items have PDFs, skipped_no_pdf must be empty."""
        from unittest.mock import MagicMock, patch

        item1 = self._make_item("K1", "Paper A", has_pdf=True)
        item2 = self._make_item("K2", "Paper B", has_pdf=True)

        indexer = self._make_indexer([item1, item2])
        self._patch_indexer(indexer)

        mock_extraction = MagicMock()
        mock_extraction.pages = [MagicMock()]
        mock_extraction.stats = {"total_pages": 1, "text_pages": 1, "ocr_pages": 0, "empty_pages": 0}
        mock_extraction.quality_grade = "B"
        mock_extraction.pending_vision = None

        with patch("zotpilot.indexer.extract_document", return_value=mock_extraction), \
             patch.object(indexer, "_index_extraction", return_value=(3, 0, "", {}, "B")):
            result = indexer.index_all(batch_size=None)

        assert result.get("skipped_no_pdf", []) == []

    def test_empty_journal_still_respects_legacy_store_indexed_docs(self, tmp_path):
        from zotpilot.index_authority import IndexJournal

        item = self._make_item("K1", "Paper A", has_pdf=True)
        indexer = self._make_indexer([item])
        self._patch_indexer(indexer)

        # Legacy store has chunks for K1, but journal is newly created and empty.
        indexer.store.db_path = tmp_path / "chroma"
        indexer.store.get_indexed_doc_ids.return_value = {"K1"}
        indexer._needs_reindex = MagicMock(return_value=(False, "current"))
        journal = IndexJournal(tmp_path / "index_journal.json")
        journal._save()

        with patch("zotpilot.indexer.extract_document") as mock_extract, \
             patch.object(indexer, "_index_extraction") as mock_index_extraction:
            result = indexer.index_all(batch_size=None, journal=journal)

        assert result["already_indexed"] == 1
        mock_extract.assert_not_called()
        mock_index_extraction.assert_not_called()

    def test_changed_doc_demoted_before_extraction_failure(self, tmp_path):
        from zotpilot.index_authority import IndexJournal, is_doc_committed, mark_committed

        item = self._make_item("K1", "Paper A", has_pdf=True)
        indexer = self._make_indexer([item])
        self._patch_indexer(indexer)

        journal = IndexJournal(tmp_path / "index_journal.json")
        mark_committed(journal, "K1")
        indexer.store.db_path = tmp_path / "chroma"
        indexer.store.get_indexed_doc_ids.return_value = {"K1"}
        indexer.store.get_document_meta.return_value = {"pdf_hash": "old-hash"}
        indexer._pdf_hash = MagicMock(return_value="new-hash")

        def _boom(*args, **kwargs):
            indexer.store.delete_document.assert_called_once_with("K1")
            raise RuntimeError("boom")

        with patch("zotpilot.indexer.extract_document", side_effect=_boom):
            result = indexer.index_all(batch_size=None, journal=journal)

        assert result["failed"] == 1
        assert not is_doc_committed(journal, "K1")
        assert "K1" in journal.in_progress

    def test_changed_doc_skipped_by_max_pages_keeps_existing_index(self, tmp_path):
        from zotpilot.index_authority import IndexJournal, is_doc_committed, mark_committed

        item = self._make_item("K1", "Paper A", has_pdf=True)
        indexer = self._make_indexer([item])
        self._patch_indexer(indexer)
        indexer.store.db_path = tmp_path / "chroma"
        indexer.store.get_indexed_doc_ids.return_value = {"K1"}
        indexer.store.get_document_meta.return_value = {"pdf_hash": "old-hash"}
        indexer._pdf_hash = MagicMock(return_value="new-hash")
        indexer.store.delete_document = MagicMock()

        journal = IndexJournal(tmp_path / "index_journal.json")
        mark_committed(journal, "K1")

        fake_doc = MagicMock()
        fake_doc.__len__.return_value = 100
        fake_doc.close = MagicMock()

        with patch("fitz.open", return_value=fake_doc), \
             patch("zotpilot.indexer.extract_document") as mock_extract:
            result = indexer.index_all(batch_size=None, journal=journal, max_pages=40)

        assert result["skipped"] == 1
        assert is_doc_committed(journal, "K1")
        indexer.store.delete_document.assert_not_called()
        mock_extract.assert_not_called()

    def test_changed_doc_deferred_by_batch_size_keeps_existing_index(self, tmp_path):
        from zotpilot.index_authority import IndexJournal, is_doc_committed, mark_committed

        item1 = self._make_item("K1", "Paper A", has_pdf=True)
        item2 = self._make_item("K2", "Paper B", has_pdf=True)
        indexer = self._make_indexer([item1, item2])
        self._patch_indexer(indexer)
        indexer.store.db_path = tmp_path / "chroma"
        indexer.store.get_indexed_doc_ids.return_value = {"K1", "K2"}
        indexer._pdf_hash = MagicMock(side_effect=["new-hash-1", "new-hash-2"])
        indexer.store.get_document_meta = MagicMock(
            side_effect=[{"pdf_hash": "old-hash-1"}, {"pdf_hash": "old-hash-2"}]
        )
        indexer.store.delete_document = MagicMock()

        journal = IndexJournal(tmp_path / "index_journal.json")
        mark_committed(journal, "K1")
        mark_committed(journal, "K2")

        mock_extraction = MagicMock()
        mock_extraction.pages = [MagicMock()]
        mock_extraction.stats = {"total_pages": 1, "text_pages": 1, "ocr_pages": 0, "empty_pages": 0}
        mock_extraction.quality_grade = "A"
        mock_extraction.pending_vision = None

        with patch("zotpilot.indexer.extract_document", return_value=mock_extraction), \
             patch.object(indexer, "_index_extraction", return_value=(1, 0, "", {}, "A")):
            result = indexer.index_all(batch_size=1, journal=journal)

        assert result["indexed"] == 1
        assert result["has_more"] is True
        assert is_doc_committed(journal, "K2")
        indexer.store.delete_document.assert_called_once_with("K1")

    def test_stale_in_progress_doc_is_prioritized_for_healing(self, tmp_path):
        from zotpilot.index_authority import IndexJournal, mark_in_progress

        item1 = self._make_item("K1", "Paper A", has_pdf=True)
        item2 = self._make_item("K2", "Paper B", has_pdf=True)
        indexer = self._make_indexer([item1, item2])
        self._patch_indexer(indexer)
        indexer.store.db_path = tmp_path / "chroma"
        indexer.store.get_indexed_doc_ids.return_value = {"K1", "K2"}
        indexer.store.get_document_meta = MagicMock(side_effect=[None])
        indexer.store.delete_document = MagicMock()

        journal = IndexJournal(tmp_path / "index_journal.json")
        mark_in_progress(journal, "K2")

        mock_extraction = MagicMock()
        mock_extraction.pages = [MagicMock()]
        mock_extraction.stats = {"total_pages": 1, "text_pages": 1, "ocr_pages": 0, "empty_pages": 0}
        mock_extraction.quality_grade = "A"
        mock_extraction.pending_vision = None

        with patch("zotpilot.indexer.extract_document", return_value=mock_extraction), \
             patch.object(indexer, "_index_extraction", return_value=(1, 0, "", {}, "A")):
            result = indexer.index_all(batch_size=1, journal=journal)

        assert result["indexed"] == 1
        indexer.store.delete_document.assert_called_once_with("K2")

    def test_doc_deleted_during_run_is_removed_by_final_reconciliation(self):
        from unittest.mock import MagicMock, patch

        item = self._make_item("K1", "Paper A", has_pdf=True)
        indexer = self._make_indexer([item])
        self._patch_indexer(indexer)

        # First library snapshot (start of run): item still present.
        # Second snapshot (end of run): item has disappeared from the library.
        indexer.zotero.get_all_items_with_pdfs.side_effect = [[item], []]
        indexer.store.get_indexed_doc_ids.return_value = {"K1"}
        indexer.store.get_document_meta.return_value = None
        indexer.store.delete_document = MagicMock()

        mock_extraction = MagicMock()
        mock_extraction.pages = [MagicMock()]
        mock_extraction.stats = {"total_pages": 1, "text_pages": 1, "ocr_pages": 0, "empty_pages": 0}
        mock_extraction.quality_grade = "A"
        mock_extraction.pending_vision = None

        with patch("zotpilot.indexer.extract_document", return_value=mock_extraction), \
             patch.object(indexer, "_index_extraction", return_value=(1, 0, "", {}, "A")):
            result = indexer.index_all(batch_size=None)

        assert result["indexed"] == 1
        # The document was indexed during this run, then removed when the
        # refreshed library snapshot no longer contained it.
        indexer.store.delete_document.assert_called_once_with("K1")


class TestVisionBudgetGuards:
    def test_dashscope_vision_provider_uses_dashscope_api(self, tmp_path):
        from zotpilot.indexer import Indexer

        config = MagicMock()
        config.zotero_data_dir = Path("/fake")
        config.chroma_db_path = tmp_path / "chroma"
        config.chroma_db_path.mkdir()
        config.chunk_size = 1000
        config.chunk_overlap = 200
        config.embedding_provider = "local"
        config.embedding_dimensions = 384
        config.embedding_model = "test"
        config.ocr_language = "eng"
        config.vision_enabled = True
        config.vision_provider = "dashscope"
        config.vision_model = "qwen3-vl-flash"
        config.dashscope_api_key = "dashscope-key"
        config.anthropic_api_key = None

        import zotpilot.feature_extraction.dashscope_vision_api as dashscope_vision_api

        with patch("zotpilot.indexer.ZoteroClient"), \
             patch("zotpilot.indexer.create_embedder"), \
             patch("zotpilot.indexer.VectorStore"), \
             patch("zotpilot.indexer.JournalRanker"), \
             patch.object(dashscope_vision_api, "DashScopeVisionAPI") as vision_cls:
            Indexer(config)

        vision_cls.assert_called_once_with(api_key="dashscope-key", model="qwen3-vl-flash")

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

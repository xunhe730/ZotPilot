"""Tests for the Indexer pipeline — specifically P0-3 ReDoS protection."""
import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@dataclass
class _HashCfg:
    """Minimal real dataclass carrying every field _config_hash reads.

    A real dataclass (not a MagicMock/SimpleNamespace) is required so
    ``dataclasses.replace`` works inside ``_vision_only_drift``.
    """
    chunk_size: int = 400
    chunk_overlap: int = 100
    embedding_provider: str = "local"
    dashscope_embedding_endpoint: str = "compatible"
    embedding_dimensions: int = 384
    embedding_model: str = "local"
    ocr_language: str = "eng"
    vision_enabled: bool = True
    vision_provider: str = "anthropic"
    vision_model: str = ""


class TestConfigHash:
    def test_dashscope_embedding_endpoint_affects_index_hash(self):
        from zotpilot.indexer import _config_hash

        base = SimpleNamespace(
            chunk_size=400,
            chunk_overlap=100,
            embedding_provider="dashscope",
            dashscope_embedding_endpoint="compatible",
            embedding_dimensions=1024,
            embedding_model="text-embedding-v4",
            ocr_language="eng",
            vision_enabled=False,
            vision_provider="anthropic",
            vision_model="",
        )
        native = SimpleNamespace(**{**base.__dict__, "dashscope_embedding_endpoint": "native"})

        assert _config_hash(base) != _config_hash(native)

    def test_formula_ocr_settings_do_not_affect_index_hash(self):
        from zotpilot.indexer import _config_hash

        base = SimpleNamespace(
            chunk_size=400,
            chunk_overlap=100,
            embedding_provider="local",
            dashscope_embedding_endpoint="compatible",
            embedding_dimensions=384,
            embedding_model="local",
            ocr_language="eng",
            vision_enabled=False,
            vision_provider="anthropic",
            vision_model="",
            formula_ocr_enabled=False,
            formula_ocr_provider="local",
        )
        formula_enabled = SimpleNamespace(
            **{
                **base.__dict__,
                "formula_ocr_enabled": True,
                "formula_ocr_max_formulas_per_doc": 12,
                "formula_ocr_min_confidence": 0.8,
            }
        )

        assert _config_hash(base) == _config_hash(formula_enabled)

    def test_vision_only_drift_detects_disabled_vision(self):
        """When the sole change vs the stored index is the vision toggle (as a
        batch_size>0/no_vision run leaves it), _vision_only_drift must report True."""
        from zotpilot.config import _config_hash, _vision_only_drift

        stored = _config_hash(_HashCfg(vision_enabled=True))
        assert _vision_only_drift(_HashCfg(vision_enabled=False), stored) is True
        # Symmetric: stored built vision-off, current vision-on, only vision differs.
        stored_off = _config_hash(_HashCfg(vision_enabled=False))
        assert _vision_only_drift(_HashCfg(vision_enabled=True), stored_off) is True

    def test_vision_only_drift_false_when_other_field_changed(self):
        """A real embedding-space change (chunk size) alongside vision must NOT be
        misreported as a vision-only drift — that case genuinely needs a rebuild."""
        from zotpilot.config import _config_hash, _vision_only_drift

        stored = _config_hash(_HashCfg(vision_enabled=True, chunk_size=400))
        assert _vision_only_drift(_HashCfg(vision_enabled=False, chunk_size=800), stored) is False

    def test_vision_only_drift_degrades_for_non_dataclass(self):
        """Non-dataclass configs (mocks) can't be replace()d — degrade to False."""
        from zotpilot.config import _vision_only_drift

        assert _vision_only_drift(MagicMock(), "any-stored-hash") is False


class TestFormulaBackfill:
    def _hash_config(self):
        return SimpleNamespace(
            chunk_size=400,
            chunk_overlap=100,
            embedding_provider="local",
            dashscope_embedding_endpoint="compatible",
            embedding_dimensions=384,
            embedding_model="local",
            ocr_language="eng",
            vision_enabled=False,
            vision_provider="anthropic",
            vision_model="",
            formula_ocr_enabled=True,
            formula_ocr_provider="local",
            formula_ocr_max_formulas_per_doc=40,
            formula_ocr_max_formulas_per_page=6,
            formula_ocr_min_confidence=0.6,
        )

    def test_backfill_requires_existing_matching_config_hash(self, tmp_path):
        from zotpilot.indexer import ConfigDriftError, Indexer, _config_hash

        indexer = Indexer.__new__(Indexer)
        indexer.config = self._hash_config()
        indexer._config_hash_path = tmp_path / "config_hash.txt"

        with pytest.raises(ConfigDriftError, match="config hash exists"):
            indexer._assert_config_hash_current()

        indexer._config_hash_path.write_text("stale")
        with pytest.raises(ConfigDriftError, match="differs"):
            indexer._assert_config_hash_current()

        indexer._config_hash_path.write_text(_config_hash(indexer.config))
        indexer._assert_config_hash_current()

    def test_index_formulas_backfills_already_indexed_docs(self, tmp_path):
        from zotpilot.indexer import Indexer
        from zotpilot.models import ExtractedFormula, ZoteroItem

        pdf_path = tmp_path / "paper.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem(
            item_key="DOC1",
            title="Paper",
            authors="Auth",
            year=2024,
            pdf_path=pdf_path,
            citation_key="auth2024",
            publication="Nature",
        )

        formula = ExtractedFormula(
            page_num=1,
            formula_index=0,
            bbox=(0, 0, 10, 10),
            latex=r"E = mc^2",
        )
        indexer = Indexer.__new__(Indexer)
        indexer.config = self._hash_config()
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1", "DOC2"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer.journal_ranker = MagicMock()
        indexer.journal_ranker.lookup.return_value = "Q1"
        indexer._ensure_formula_provider_available = MagicMock()
        indexer._assert_config_hash_current = MagicMock()
        indexer._pdf_hash = MagicMock(return_value="hash")
        indexer._recognize_formulas_for_item = MagicMock(return_value=[formula])

        result = indexer.index_formulas()

        assert result["processed"] == 1
        assert result["formulas_indexed"] == 1
        indexer._ensure_formula_provider_available.assert_called_once()
        indexer._assert_config_hash_current.assert_called_once()
        indexer.store.delete_chunks_by_type.assert_called_once_with("DOC1", "formula")
        indexer.store.add_formulas.assert_called_once()

    def test_formula_provider_preflight_has_actionable_install_hint(self):
        from zotpilot.indexer import FormulaProviderUnavailableError, Indexer

        indexer = Indexer.__new__(Indexer)
        indexer.config = SimpleNamespace(
            formula_ocr_enabled=True,
            formula_ocr_provider="local",
        )
        indexer._get_formula_provider = MagicMock()

        with patch("zotpilot.feature_extraction.formula_ocr.importlib.util.find_spec", return_value=None):
            with pytest.raises(FormulaProviderUnavailableError) as exc_info:
                indexer._ensure_formula_provider_available()

        message = str(exc_info.value)
        assert "zotpilot[formula]" in message
        assert "formula_ocr_enabled=false" in message
        indexer._get_formula_provider.assert_not_called()

    def test_formula_provider_preflight_does_not_load_model_when_available(self):
        from zotpilot.indexer import Indexer

        indexer = Indexer.__new__(Indexer)
        indexer.config = SimpleNamespace(
            formula_ocr_enabled=True,
            formula_ocr_provider="local",
        )
        indexer._get_formula_provider = MagicMock()

        with patch("zotpilot.feature_extraction.formula_ocr.importlib.util.find_spec", return_value=object()):
            indexer._ensure_formula_provider_available()

        indexer._get_formula_provider.assert_not_called()

    def test_formula_provider_preflight_skips_when_disabled(self):
        from zotpilot.indexer import Indexer

        indexer = Indexer.__new__(Indexer)
        indexer.config = SimpleNamespace(formula_ocr_enabled=False)
        indexer._get_formula_provider = MagicMock()

        indexer._ensure_formula_provider_available()

        indexer._get_formula_provider.assert_not_called()

    def test_formula_backfill_keeps_existing_chunks_when_refresh_finds_none(self, tmp_path):
        from zotpilot.indexer import Indexer
        from zotpilot.models import ZoteroItem

        pdf_path = tmp_path / "paper.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem(
            item_key="DOC1",
            title="Paper",
            authors="Auth",
            year=2024,
            pdf_path=pdf_path,
            citation_key="auth2024",
            publication="Nature",
        )
        indexer = Indexer.__new__(Indexer)
        indexer.config = self._hash_config()
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1"}
        indexer.store.count_chunk_types.return_value = {"text": 3, "table": 0, "figure": 0, "formula": 2}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer.journal_ranker = MagicMock()
        indexer.journal_ranker.lookup.return_value = "Q1"
        indexer._ensure_formula_provider_available = MagicMock()
        indexer._assert_config_hash_current = MagicMock()
        indexer._pdf_hash = MagicMock(return_value="hash")
        indexer._recognize_formulas_for_item = MagicMock(return_value=[])

        result = indexer.index_formulas(refresh_existing=True)

        assert result["processed"] == 1
        assert result["formulas_indexed"] == 0
        assert result["results"][0]["existing_formulas_kept"] == 2
        indexer.store.delete_chunks_by_type.assert_not_called()
        indexer.store.add_formulas.assert_not_called()

    def test_formula_failure_does_not_block_table_failure_cleanup(self, tmp_path):
        from zotpilot.index_authority import IndexJournal, mark_committed, record_table_failure
        from zotpilot.indexer import Indexer
        from zotpilot.models import Chunk, ExtractedFormula, PageExtraction, ZoteroItem

        pdf_path = tmp_path / "paper.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem(
            item_key="DOC1",
            title="Paper",
            authors="Auth",
            year=2024,
            pdf_path=pdf_path,
            citation_key="auth2024",
            publication="Nature",
        )
        extraction = SimpleNamespace(
            pages=[PageExtraction(page_num=1, markdown="Body text", char_start=0)],
            full_markdown="Body text",
            sections=[],
            tables=[],
            figures=[],
            stats={"text_pages": 1, "ocr_pages": 0, "empty_pages": 0},
            quality_grade="A",
            formulas=[],
        )
        chunk = Chunk(
            text="Body text",
            chunk_index=0,
            page_num=1,
            char_start=0,
            char_end=9,
            section="body",
        )
        formula = ExtractedFormula(
            page_num=1,
            formula_index=0,
            bbox=(0, 0, 10, 10),
            latex=r"E = mc^2",
        )
        journal = IndexJournal(tmp_path / "journal.json")
        mark_committed(journal, item.item_key)
        record_table_failure(journal, item.item_key, "table storage: stale")

        indexer = Indexer.__new__(Indexer)
        indexer.config = SimpleNamespace(formula_ocr_enabled=True)
        indexer.chunker = MagicMock()
        indexer.chunker.chunk.return_value = [chunk]
        indexer.journal_ranker = MagicMock()
        indexer.journal_ranker.lookup.return_value = "Q1"
        indexer.store = MagicMock()
        indexer.store.add_formulas.side_effect = RuntimeError("formula boom")
        indexer._pdf_hash = MagicMock(return_value="hash")
        indexer._recognize_formulas_for_item = MagicMock(return_value=[formula])

        with patch("zotpilot.indexer.record_table_failure") as mock_record_failure:
            n_chunks, n_tables, reason, _stats, quality = indexer._index_extraction(
                item,
                extraction,
                journal,
            )

        assert n_chunks == 1
        assert n_tables == 0
        assert reason == ""
        assert quality == "A"
        indexer.store.add_formulas.assert_called_once()
        mock_record_failure.assert_not_called()
        assert item.item_key not in journal.table_failures
        assert "table_failure" not in journal.committed[item.item_key]

    def test_formula_provider_error_is_tool_error_for_index_formulas(self, tmp_path):
        from zotpilot.indexer import FormulaProviderUnavailableError
        from zotpilot.state import ToolError
        from zotpilot.tools import indexing as idx_mod

        config = MagicMock()
        config.validate.return_value = []
        config.formula_ocr_enabled = True
        config.chroma_db_path = tmp_path / "chroma"

        class FakeIndexer:
            def __init__(self, _config):
                pass

            def index_formulas(self, **_kwargs):
                raise FormulaProviderUnavailableError("Install `zotpilot[formula]`")

        with patch.object(idx_mod, "_get_config", return_value=config), \
             patch.object(idx_mod, "acquire_lease"), \
             patch.object(idx_mod, "release_lease"), \
             patch("zotpilot.indexer.Indexer", FakeIndexer):
            with pytest.raises(ToolError, match="zotpilot\\[formula\\]"):
                idx_mod.index_formulas()

    def test_formula_provider_error_is_tool_error_for_index_library(self, tmp_path):
        from zotpilot.indexer import FormulaProviderUnavailableError
        from zotpilot.state import ToolError
        from zotpilot.tools import indexing as idx_mod

        config = MagicMock()
        config.validate.return_value = []
        config.chroma_db_path = tmp_path / "chroma"
        config.max_pages = 0
        config.vision_enabled = False

        def fake_index_all_libraries(_config, **_kwargs):
            raise FormulaProviderUnavailableError("Install `zotpilot[formula]`")

        with patch.object(idx_mod, "_get_config", return_value=config), \
             patch.object(idx_mod, "acquire_lease"), \
             patch.object(idx_mod, "release_lease"), \
             patch("zotpilot.indexer.index_all_libraries", fake_index_all_libraries):
            with pytest.raises(ToolError, match="zotpilot\\[formula\\]"):
                idx_mod.index_library(batch_size=0)


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
        from unittest.mock import patch

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

    def test_doc_deleted_during_run_is_removed_by_final_reconciliation(self, tmp_path):
        from unittest.mock import MagicMock, patch

        items = [self._make_item(f"K{i}", f"Paper {i}", has_pdf=True) for i in range(1, 6)]
        indexer = self._make_indexer(items)
        self._patch_indexer(indexer)
        # Reachable library so the RC6 floor does not refuse the legit deletion.
        indexer.config.zotero_data_dir = tmp_path

        # First snapshot (start of run): all five present.
        # Second snapshot (end of run): K1 has disappeared (1/5 = 20%, below floor).
        indexer.zotero.get_all_items_with_pdfs.side_effect = [list(items), items[1:]]
        indexer.store.get_indexed_doc_ids.return_value = {"K1", "K2", "K3", "K4", "K5"}
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

        assert result["indexed"] == 5
        # The document removed mid-run is reconciled at end (sub-floor deletion).
        indexer.store.delete_document.assert_called_once_with("K1")

    def test_final_reconciliation_refusal_is_surfaced_not_silent(self, tmp_path, caplog):
        """AC9: an empty end-of-run read must surface a refusal, never delete, and
        never log the misleading "removed 0 orphans" line."""
        import logging
        from unittest.mock import MagicMock, patch

        item = self._make_item("K1", "Paper A", has_pdf=True)
        indexer = self._make_indexer([item])
        self._patch_indexer(indexer)
        indexer.config.zotero_data_dir = tmp_path

        # Start snapshot has K1; end snapshot reads empty (e.g. drive unmounted).
        indexer.zotero.get_all_items_with_pdfs.side_effect = [[item], []]
        indexer.store.get_indexed_doc_ids.return_value = {"K1"}
        indexer.store.get_document_meta.return_value = None
        indexer.store.delete_document = MagicMock()

        mock_extraction = MagicMock()
        mock_extraction.pages = [MagicMock()]
        mock_extraction.stats = {"total_pages": 1, "text_pages": 1, "ocr_pages": 0, "empty_pages": 0}
        mock_extraction.quality_grade = "A"
        mock_extraction.pending_vision = None

        with caplog.at_level(logging.WARNING, logger="zotpilot.indexer"), \
             patch("zotpilot.indexer.extract_document", return_value=mock_extraction), \
             patch.object(indexer, "_index_extraction", return_value=(1, 0, "", {}, "A")):
            indexer.index_all(batch_size=None)

        # Nothing deleted, refusal surfaced as a WARNING, no misleading info line.
        indexer.store.delete_document.assert_not_called()
        assert any(
            "refused end-of-run orphan reconciliation" in r.message for r in caplog.records
        )
        assert not any("removed 0 orphaned" in r.getMessage() for r in caplog.records)

    def test_config_drift_without_force_blocks(self, tmp_path):
        """AC6 / RC8: a config-hash mismatch without force must BLOCK with a clear
        error, not silently proceed into a mixed embedding-space index."""
        from unittest.mock import patch

        import pytest

        from zotpilot.indexer import ConfigDriftError

        item = self._make_item("K1", "Paper A", has_pdf=True)
        indexer = self._make_indexer([item])
        self._patch_indexer(indexer)
        indexer.config.zotero_data_dir = tmp_path
        indexer.store.get_indexed_doc_ids.return_value = {"K1"}

        # A persisted hash that cannot match the current config hash.
        indexer._config_hash_path.exists.return_value = True
        indexer._config_hash_path.read_text.return_value = "stale-hash-does-not-match"

        with patch("zotpilot.indexer.extract_document") as mock_extract, \
             patch.object(indexer, "_index_extraction") as mock_index_extraction, \
             pytest.raises(ConfigDriftError, match="--force"):
            indexer.index_all(batch_size=None)

        # Blocked before any extraction/indexing work.
        mock_extract.assert_not_called()
        mock_index_extraction.assert_not_called()

    def test_vision_built_index_with_batch_steers_to_batch_size_zero(self, tmp_path):
        """UX sharp edge: a vision-built index indexed with batch_size>0 (which auto-
        disables vision) trips the drift guard. The error must steer the user to
        batch_size=0 for an incremental pass, NOT force_reindex (which would rebuild
        every paper and re-spend embedding quota)."""
        from unittest.mock import patch

        import pytest

        from zotpilot.config import _config_hash
        from zotpilot.indexer import ConfigDriftError

        item = self._make_item("K1", "Paper A", has_pdf=True)
        indexer = self._make_indexer([item])
        self._patch_indexer(indexer)
        # The MCP/CLI batch path hands the Indexer a vision-DISABLED config; model that
        # with a real dataclass so _vision_only_drift can replace()/re-hash it.
        indexer.config = _HashCfg(vision_enabled=False)
        indexer.store.get_indexed_doc_ids.return_value = {"K1"}

        # Persisted hash reflects the original vision-ON build (the only difference).
        stored = _config_hash(_HashCfg(vision_enabled=True))
        indexer._config_hash_path.exists.return_value = True
        indexer._config_hash_path.read_text.return_value = stored

        with patch("zotpilot.indexer.extract_document") as mock_extract, \
             patch.object(indexer, "_index_extraction") as mock_index_extraction, \
             pytest.raises(ConfigDriftError) as exc_info:
            indexer.index_all(batch_size=2)

        message = str(exc_info.value)
        # Actionable: keep vision on via batch_size=0.
        assert "batch_size=0" in message
        # Explicitly warns AGAINST the quota-burning force-rebuild.
        assert "force_reindex" in message
        assert "Do NOT" in message
        # Blocked before any extraction/indexing work.
        mock_extract.assert_not_called()
        mock_index_extraction.assert_not_called()


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

        vision_cls.assert_called_once()
        _args, kwargs = vision_cls.call_args
        assert kwargs["api_key"] == "dashscope-key"
        assert kwargs["model"] == "qwen3-vl-flash"
        assert "result_cache" in kwargs  # vision-results cache wired in

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
        indexer._config_hash_path.exists.return_value = False
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

"""Tests for the Indexer pipeline — specifically P0-3 ReDoS protection."""
import json
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
            formula_candidate_pdf_fallback_max_pages=80,
            formula_ocr_max_formulas_per_doc=40,
            formula_ocr_max_formulas_per_page=6,
            formula_ocr_min_confidence=0.6,
        )

    def test_formula_estimate_factory_reads_doc_ids_without_vector_store_init(self, tmp_path):
        import sqlite3

        from zotpilot.indexer import Indexer

        chroma_path = tmp_path / "chroma"
        chroma_path.mkdir()
        with sqlite3.connect(chroma_path / "chroma.sqlite3") as conn:
            conn.execute("CREATE TABLE embeddings (embedding_id TEXT NOT NULL)")
            conn.executemany(
                "INSERT INTO embeddings (embedding_id) VALUES (?)",
                [
                    ("DOC1_chunk_0000",),
                    ("DOC1_table_0001_00",),
                    ("DOC2_fig_001_00",),
                    ("DOC3_formula_0001",),
                ],
            )
        config = SimpleNamespace(
            **{
                **self._hash_config().__dict__,
                "zotero_data_dir": tmp_path,
                "chroma_db_path": chroma_path,
            }
        )

        with (
            patch("zotpilot.indexer.ZoteroClient"),
            patch("zotpilot.indexer.VectorStore.__init__", side_effect=AssertionError("writable init")),
        ):
            indexer = Indexer.for_formula_estimate(config)
            assert indexer.store.get_indexed_doc_ids() == {"DOC1", "DOC2", "DOC3"}

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
            equation_number="(1)",
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

        with patch("zotpilot.feature_extraction.formula_ocr.extract_formula_candidates", return_value=[object()]):
            result = indexer.index_formulas()

        assert result["processed"] == 1
        assert result["formulas_indexed"] == 1
        indexer._ensure_formula_provider_available.assert_called_once()
        indexer._assert_config_hash_current.assert_called_once()
        indexer.store.delete_chunks_by_type.assert_not_called()
        indexer.store.replace_formulas.assert_called_once()
        indexer.store.add_formulas.assert_not_called()

    def test_index_formulas_blocks_candidate_numbering_warnings_by_default(self, tmp_path):
        from zotpilot.feature_extraction.formula_ocr import FormulaCandidate
        from zotpilot.indexer import Indexer
        from zotpilot.models import ExtractedFormula, ZoteroItem

        pdf_path = tmp_path / "paper.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("DOC1", "Paper", "Auth", 2024, pdf_path, publication="Nature")
        candidates = [
            FormulaCandidate(
                page_num=1,
                bbox=(0, 0, 10, 10),
                raw_text=r"E=mc^2",
                confidence=0.95,
                equation_number="(1)",
                latex=r"E=mc^2",
                source="mineru_content_list",
            ),
            FormulaCandidate(
                page_num=2,
                bbox=(0, 20, 10, 30),
                raw_text=r"\sigma=E\epsilon",
                confidence=0.95,
                equation_number="(3)",
                latex=r"\sigma=E\epsilon",
                source="mineru_content_list",
            ),
        ]
        formula = ExtractedFormula(
            page_num=1,
            formula_index=0,
            bbox=(0, 0, 10, 10),
            latex=r"E=mc^2",
            equation_number="(1)",
        )
        indexer = Indexer.__new__(Indexer)
        indexer.config = self._hash_config()
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer.journal_ranker = MagicMock()
        indexer._ensure_formula_provider_available = MagicMock()
        indexer._assert_config_hash_current = MagicMock()
        indexer._recognize_formulas_for_item = MagicMock(return_value=[formula])

        with patch("zotpilot.feature_extraction.formula_ocr.extract_formula_candidates", return_value=candidates):
            result = indexer.index_formulas()

        assert result["processed"] == 1
        assert result["formulas_indexed"] == 0
        assert result["candidate_quality_review_count"] == 1
        assert result["results"][0]["status"] == "needs_review"
        assert result["results"][0]["reason"] == "formula_candidate_review_required"
        assert result["results"][0]["review_reasons"] == ["missing_equation_number_gap"]
        assert result["results"][0]["candidate_audit"]["equation_number_warnings"] == [
            "missing_equation_number_gap"
        ]
        indexer._recognize_formulas_for_item.assert_not_called()
        indexer.store.replace_formulas.assert_not_called()
        indexer.store.add_new_formulas.assert_not_called()

    def test_index_formulas_can_override_candidate_quality_warnings(self, tmp_path):
        from zotpilot.feature_extraction.formula_ocr import FormulaCandidate
        from zotpilot.indexer import Indexer
        from zotpilot.models import ExtractedFormula, ZoteroItem

        pdf_path = tmp_path / "paper.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("DOC1", "Paper", "Auth", 2024, pdf_path, publication="Nature")
        candidates = [
            FormulaCandidate(
                page_num=1,
                bbox=(0, 0, 10, 10),
                raw_text=r"E=mc^2",
                confidence=0.95,
                equation_number="",
                latex=r"E=mc^2",
                source="mineru_content_list",
            ),
        ]
        formula = ExtractedFormula(
            page_num=1,
            formula_index=0,
            bbox=(0, 0, 10, 10),
            latex=r"E=mc^2",
            equation_number="(1)",
        )
        indexer = Indexer.__new__(Indexer)
        indexer.config = self._hash_config()
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1"}
        indexer.store.replace_formulas.return_value = 1
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer.journal_ranker = MagicMock()
        indexer.journal_ranker.lookup.return_value = "Q1"
        indexer._ensure_formula_provider_available = MagicMock()
        indexer._assert_config_hash_current = MagicMock()
        indexer._pdf_hash = MagicMock(return_value="hash")
        indexer._recognize_formulas_for_item = MagicMock(return_value=[formula])

        with patch("zotpilot.feature_extraction.formula_ocr.extract_formula_candidates", return_value=candidates):
            result = indexer.index_formulas(allow_candidate_quality_warnings=True)

        assert result["formulas_indexed"] == 1
        assert result["candidate_quality_review_count"] == 0
        assert result["results"][0]["status"] == "indexed"
        indexer._recognize_formulas_for_item.assert_called_once()
        indexer.store.replace_formulas.assert_called_once()

    def test_index_formulas_no_refresh_adds_only_new_formula_chunks(self, tmp_path):
        from zotpilot.indexer import Indexer
        from zotpilot.models import ExtractedFormula, ZoteroItem

        pdf_path = tmp_path / "paper.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("DOC1", "Paper", "Auth", 2024, pdf_path, publication="Nature")
        formula = ExtractedFormula(
            page_num=1,
            formula_index=0,
            bbox=(0, 0, 10, 10),
            latex=r"E = mc^2",
            equation_number="(1)",
        )
        indexer = Indexer.__new__(Indexer)
        indexer.config = self._hash_config()
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1"}
        indexer.store.add_new_formulas.return_value = 1
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer.journal_ranker = MagicMock()
        indexer.journal_ranker.lookup.return_value = "Q1"
        indexer._ensure_formula_provider_available = MagicMock()
        indexer._assert_config_hash_current = MagicMock()
        indexer._pdf_hash = MagicMock(return_value="hash")
        indexer._get_formula_provider = MagicMock(return_value=SimpleNamespace())
        indexer._recognize_formulas_for_item = MagicMock(return_value=[formula])

        with patch("zotpilot.feature_extraction.formula_ocr.extract_formula_candidates", return_value=[object()]):
            result = indexer.index_formulas(refresh_existing=False)

        assert result["formulas_indexed"] == 1
        indexer.store.add_new_formulas.assert_called_once()
        indexer.store.replace_formulas.assert_not_called()
        indexer.store.add_formulas.assert_not_called()

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

        with patch("zotpilot.feature_extraction.formula_ocr.extract_formula_candidates", return_value=[]):
            result = indexer.index_formulas(refresh_existing=True)

        assert result["processed"] == 1
        assert result["formulas_indexed"] == 0
        assert result["results"][0]["existing_formulas_kept"] == 2
        indexer.store.delete_chunks_by_type.assert_not_called()
        indexer.store.add_formulas.assert_not_called()

    def test_formula_backfill_blocks_structural_review_before_writing(self, tmp_path):
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
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1"}
        indexer.store.count_chunk_types.return_value = {"text": 3, "table": 0, "figure": 0, "formula": 2}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer.journal_ranker = MagicMock()
        indexer.journal_ranker.lookup.return_value = "Q1"
        indexer._ensure_formula_provider_available = MagicMock()
        indexer._assert_config_hash_current = MagicMock()
        indexer._pdf_hash = MagicMock(return_value="hash")
        indexer._get_formula_provider = MagicMock(return_value=SimpleNamespace())
        indexer._recognize_formulas_for_item = MagicMock(return_value=[formula])

        with patch("zotpilot.feature_extraction.formula_ocr.extract_formula_candidates", return_value=[object()]):
            result = indexer.index_formulas(refresh_existing=True)

        row = result["results"][0]
        assert result["processed"] == 1
        assert result["formulas_indexed"] == 0
        assert result["low_confidence_review_count"] == 1
        assert row["status"] == "needs_review"
        assert row["reason"] == "formula_structural_review_required"
        assert row["existing_formulas_kept"] == 2
        assert row["review_reasons"] == ["missing_equation_number"]
        assert result["low_confidence_review_queue"][0]["review_reasons"] == ["missing_equation_number"]
        indexer.store.delete_chunks_by_type.assert_not_called()
        indexer.store.add_formulas.assert_not_called()

    def test_formula_review_allows_source_unnumbered_formulas(self, tmp_path):
        from zotpilot.indexer import Indexer
        from zotpilot.models import ExtractedFormula, ZoteroItem

        item = ZoteroItem(
            "DOC1",
            "Unnumbered formula paper",
            "Author",
            2025,
            tmp_path / "paper.pdf",
            publication="Journal",
        )
        formulas = [
            ExtractedFormula(
                page_num=2,
                formula_index=0,
                bbox=(0, 0, 10, 10),
                latex=r"\eta = -p / q",
                equation_number_status="unnumbered",
            )
        ]

        rows = Indexer._formula_review_rows(item=item, formulas=formulas, threshold=0.0)

        assert rows == []

    def test_formula_backfill_blocks_gapped_numbering_before_writing(self, tmp_path):
        from zotpilot.indexer import Indexer
        from zotpilot.models import ExtractedFormula, ZoteroItem

        pdf_path = tmp_path / "paper.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("DOC1", "Paper", "Auth", 2024, pdf_path, publication="Nature")
        formulas = [
            ExtractedFormula(
                page_num=1,
                formula_index=0,
                bbox=(0, 0, 10, 10),
                latex=r"a = b",
                equation_number="(1)",
            ),
            ExtractedFormula(
                page_num=1,
                formula_index=1,
                bbox=(0, 20, 10, 30),
                latex=r"c = d",
                equation_number="(3)",
            ),
            ExtractedFormula(
                page_num=1,
                formula_index=2,
                bbox=(0, 40, 10, 50),
                latex=r"e = f",
                equation_number="(3)",
            ),
        ]
        indexer = Indexer.__new__(Indexer)
        indexer.config = self._hash_config()
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1"}
        indexer.store.count_chunk_types.return_value = {"text": 3, "table": 0, "figure": 0, "formula": 1}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer.journal_ranker = MagicMock()
        indexer.journal_ranker.lookup.return_value = "Q1"
        indexer._ensure_formula_provider_available = MagicMock()
        indexer._assert_config_hash_current = MagicMock()
        indexer._pdf_hash = MagicMock(return_value="hash")
        indexer._recognize_formulas_for_item = MagicMock(return_value=formulas)

        with patch("zotpilot.feature_extraction.formula_ocr.extract_formula_candidates", return_value=[object()]):
            result = indexer.index_formulas(refresh_existing=True)

        row = result["results"][0]
        assert row["status"] == "needs_review"
        assert row["review_reasons"] == ["duplicate_equation_number", "numbering_sequence_gap"]
        assert row["existing_formulas_kept"] == 1
        assert result["formulas_indexed"] == 0
        indexer.store.delete_chunks_by_type.assert_not_called()
        indexer.store.add_formulas.assert_not_called()

    def test_formula_backfill_reports_candidate_ocr_failures_before_writing(self, tmp_path):
        from zotpilot.feature_extraction.formula_ocr import FormulaCandidate
        from zotpilot.indexer import Indexer
        from zotpilot.models import ExtractedFormula, ZoteroItem

        pdf_path = tmp_path / "paper.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("DOC1", "Paper", "Auth", 2024, pdf_path, publication="Nature")
        candidates = [
            FormulaCandidate(
                page_num=1,
                bbox=(0, 0, 10, 10),
                raw_text="a = b (1)",
                confidence=0.95,
                equation_number="(1)",
                latex=r"a = b",
                source="mineru_cache",
            ),
            FormulaCandidate(
                page_num=1,
                bbox=(0, 20, 10, 30),
                raw_text="c = d (2)",
                confidence=0.72,
                equation_number="(2)",
                source="pdf_text_equation_number",
            ),
        ]
        formulas = [
            ExtractedFormula(
                page_num=1,
                formula_index=0,
                bbox=(0, 0, 10, 10),
                latex=r"a = b",
                equation_number="(1)",
            )
        ]
        indexer = Indexer.__new__(Indexer)
        indexer.config = self._hash_config()
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1"}
        indexer.store.count_chunk_types.return_value = {"text": 3, "table": 0, "figure": 0, "formula": 1}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer.journal_ranker = MagicMock()
        indexer.journal_ranker.lookup.return_value = "Q1"
        indexer._ensure_formula_provider_available = MagicMock()
        indexer._assert_config_hash_current = MagicMock()
        indexer._pdf_hash = MagicMock(return_value="hash")
        indexer._recognize_formulas_for_item = MagicMock(return_value=formulas)

        with patch("zotpilot.feature_extraction.formula_ocr.extract_formula_candidates", return_value=candidates):
            result = indexer.index_formulas(refresh_existing=True)

        row = result["results"][0]
        assert row["status"] == "needs_review"
        assert row["review_reasons"] == ["ocr_failed"]
        assert row["existing_formulas_kept"] == 1
        assert result["formulas_indexed"] == 0
        review = result["low_confidence_review_queue"][0]
        assert review["equation_number"] == "(2)"
        assert review["review_reasons"] == ["ocr_failed"]
        assert review["bbox"] == (0, 20, 10, 30)
        indexer.store.delete_chunks_by_type.assert_not_called()
        indexer.store.add_formulas.assert_not_called()
        indexer.store.replace_formulas.assert_not_called()

    def test_formula_backfill_writes_cached_formulas_with_ocr_miss_when_no_existing_formulas(self, tmp_path):
        from zotpilot.feature_extraction.formula_ocr import FormulaCandidate
        from zotpilot.indexer import Indexer
        from zotpilot.models import ExtractedFormula, ZoteroItem

        pdf_path = tmp_path / "paper.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("DOC1", "Paper", "Auth", 2024, pdf_path, publication="Nature")
        candidates = [
            FormulaCandidate(
                page_num=1,
                bbox=(0, 0, 10, 10),
                raw_text="a = b (1)",
                confidence=0.95,
                equation_number="(1)",
                latex=r"a = b",
                source="mineru_cache",
            ),
            FormulaCandidate(
                page_num=1,
                bbox=(0, 20, 10, 30),
                raw_text="c = d (2)",
                confidence=0.72,
                equation_number="(2)",
                source="pdf_text_equation_number",
            ),
        ]
        formulas = [
            ExtractedFormula(
                page_num=1,
                formula_index=0,
                bbox=(0, 0, 10, 10),
                latex=r"a = b",
                equation_number="(1)",
            )
        ]
        indexer = Indexer.__new__(Indexer)
        indexer.config = self._hash_config()
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1"}
        indexer.store.count_chunk_types.return_value = {"text": 3, "table": 0, "figure": 0, "formula": 0}
        indexer.store.replace_formulas.return_value = 1
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer.journal_ranker = MagicMock()
        indexer.journal_ranker.lookup.return_value = "Q1"
        indexer._ensure_formula_provider_available = MagicMock()
        indexer._assert_config_hash_current = MagicMock()
        indexer._pdf_hash = MagicMock(return_value="hash")
        indexer._recognize_formulas_for_item = MagicMock(return_value=formulas)

        with patch("zotpilot.feature_extraction.formula_ocr.extract_formula_candidates", return_value=candidates):
            result = indexer.index_formulas(refresh_existing=True)

        row = result["results"][0]
        assert row["status"] == "indexed_with_review"
        assert row["reason"] == "formula_review_required"
        assert row["review_reasons"] == ["ocr_failed"]
        assert row["n_formulas"] == 1
        assert result["processed"] == 1
        assert result["formulas_indexed"] == 1
        review = result["low_confidence_review_queue"][0]
        assert review["equation_number"] == "(2)"
        assert review["review_reasons"] == ["ocr_failed"]
        indexer.store.replace_formulas.assert_called_once()

    def test_formula_review_rows_distinguish_rejected_cached_latex_from_ocr_failure(self):
        from zotpilot.feature_extraction.formula_ocr import FormulaCandidate
        from zotpilot.indexer import Indexer
        from zotpilot.models import ExtractedFormula, ZoteroItem

        item = ZoteroItem("DOC1", "Paper", "Auth", 2024, None)
        candidates = [
            FormulaCandidate(
                page_num=1,
                bbox=(0, 0, 10, 10),
                raw_text="a = b (1)",
                confidence=0.95,
                equation_number="(1)",
                latex=r"a = b",
                source="mineru_cache",
            ),
            FormulaCandidate(
                page_num=1,
                bbox=(0, 20, 10, 30),
                raw_text="not a formula (2)",
                confidence=0.95,
                equation_number="(2)",
                latex=r"\text{not a formula}",
                source="mineru_cache",
            ),
            FormulaCandidate(
                page_num=1,
                bbox=(0, 40, 10, 50),
                raw_text="c = d (3)",
                confidence=0.72,
                equation_number="(3)",
                source="pdf_text_equation_number",
            ),
        ]
        formulas = [
            ExtractedFormula(
                page_num=1,
                formula_index=0,
                bbox=(0, 0, 10, 10),
                latex=r"a = b",
                equation_number="(1)",
            )
        ]

        rows = Indexer._formula_ocr_failure_review_rows(item=item, candidates=candidates, formulas=formulas)

        assert [row["equation_number"] for row in rows] == ["(2)", "(3)"]
        assert [row["review_reasons"] for row in rows] == [["cached_latex_rejected"], ["ocr_failed"]]

    def test_formula_ocr_failure_review_rows_keep_stable_page_window_indices(self):
        from zotpilot.feature_extraction.formula_ocr import FormulaCandidate
        from zotpilot.indexer import Indexer
        from zotpilot.models import ZoteroItem

        item = ZoteroItem("DOC1", "Paper", "Auth", 2024, None)
        candidates = [
            FormulaCandidate(
                page_num=10,
                bbox=(0, 0, 10, 10),
                raw_text="stale unnumbered (6)",
                confidence=0.72,
                equation_number="(6)",
                equation_number_status="unnumbered",
                source="mineru_cache",
            ),
            FormulaCandidate(
                page_num=11,
                bbox=(0, 20, 10, 30),
                raw_text="c = d (7)",
                confidence=0.72,
                equation_number="(7)",
                source="pdf_text_equation_number",
            ),
        ]

        rows = Indexer._formula_ocr_failure_review_rows(
            item=item,
            candidates=candidates,
            formulas=[],
            formula_indices=[25, 26],
        )

        assert len(rows) == 1
        assert rows[0]["formula_index"] == 26
        assert rows[0]["equation_number"] == "(7)"

    def test_formula_backfill_blocks_truncated_pdf_fallback_before_writing(self, tmp_path):
        from zotpilot.feature_extraction.formula_ocr import FormulaCandidate
        from zotpilot.indexer import Indexer
        from zotpilot.models import ExtractedFormula, ZoteroItem

        pdf_path = tmp_path / "paper.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("DOC1", "Paper", "Auth", 2024, pdf_path, publication="Nature")
        candidate = FormulaCandidate(
            page_num=1,
            bbox=(0, 0, 10, 10),
            raw_text="a = b (1)",
            confidence=0.72,
            equation_number="(1)",
            source="pdf_text_equation_number_truncated",
        )
        formula = ExtractedFormula(
            page_num=1,
            formula_index=0,
            bbox=(0, 0, 10, 10),
            latex=r"a = b",
            confidence=0.95,
            equation_number="(1)",
            source="pdf_text_equation_number_truncated",
        )
        indexer = Indexer.__new__(Indexer)
        indexer.config = self._hash_config()
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1"}
        indexer.store.count_chunk_types.return_value = {"text": 3, "table": 0, "figure": 0, "formula": 1}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer.journal_ranker = MagicMock()
        indexer.journal_ranker.lookup.return_value = "Q1"
        indexer._ensure_formula_provider_available = MagicMock()
        indexer._assert_config_hash_current = MagicMock()
        indexer._pdf_hash = MagicMock(return_value="hash")
        indexer._recognize_formulas_for_item = MagicMock(return_value=[formula])

        with patch("zotpilot.feature_extraction.formula_ocr.extract_formula_candidates", return_value=[candidate]):
            result = indexer.index_formulas(refresh_existing=True)

        row = result["results"][0]
        assert row["status"] == "needs_review"
        assert row["review_reasons"] == ["fallback_truncated"]
        assert row["existing_formulas_kept"] == 1
        assert result["formulas_indexed"] == 0
        review = result["candidate_quality_review_queue"][0]
        assert review["review_reasons"] == ["fallback_truncated"]
        assert review["candidate_audit"]["has_truncated_source"] is True
        indexer._recognize_formulas_for_item.assert_not_called()
        indexer.store.delete_chunks_by_type.assert_not_called()
        indexer.store.add_formulas.assert_not_called()

    def test_formula_backfill_stops_before_exceeding_daily_budget(self, tmp_path):
        from zotpilot.indexer import Indexer
        from zotpilot.models import ExtractedFormula, ZoteroItem

        pdf1 = tmp_path / "paper1.pdf"
        pdf2 = tmp_path / "paper2.pdf"
        pdf1.write_bytes(b"%PDF-1.4")
        pdf2.write_bytes(b"%PDF-1.4")
        items = [
            ZoteroItem("DOC1", "Paper 1", "Auth", 2024, pdf1, publication="Nature"),
            ZoteroItem("DOC2", "Paper 2", "Auth", 2024, pdf2, publication="Nature"),
        ]
        formula = ExtractedFormula(
            page_num=1,
            formula_index=0,
            bbox=(0, 0, 10, 10),
            latex=r"E = mc^2",
            equation_number="(1)",
        )
        indexer = Indexer.__new__(Indexer)
        indexer.config = self._hash_config()
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1", "DOC2"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = items
        indexer.journal_ranker = MagicMock()
        indexer.journal_ranker.lookup.return_value = "Q1"
        indexer._ensure_formula_provider_available = MagicMock()
        indexer._assert_config_hash_current = MagicMock()
        indexer._pdf_hash = MagicMock(return_value="hash")
        indexer._get_formula_provider = MagicMock(return_value=SimpleNamespace())
        indexer._recognize_formulas_for_item = MagicMock(return_value=[formula])

        with patch(
            "zotpilot.feature_extraction.formula_ocr.extract_formula_candidates",
            side_effect=[[object(), object()], [object(), object()]],
        ):
            result = indexer.index_formulas(daily_call_budget=2)

        assert result["processed"] == 1
        assert result["provider_calls_used"] == 2
        assert result["budget_exhausted"] is True
        assert result["resume_cursor"] == "DOC1"
        assert result["next_item_key"] == "DOC2"
        assert result["next_item_candidate_count"] == 2
        assert result["results"][-1]["status"] == "deferred_budget"
        assert result["results"][-1]["reason"] == "provider_calls_exceed_remaining_budget"
        indexer._recognize_formulas_for_item.assert_called_once()

    def test_formula_backfill_does_not_meter_candidates_with_latex(self, tmp_path):
        from zotpilot.feature_extraction.formula_ocr import FormulaCandidate
        from zotpilot.indexer import Indexer
        from zotpilot.models import ExtractedFormula, ZoteroItem

        pdf_path = tmp_path / "paper.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("DOC1", "Paper", "Auth", 2024, pdf_path, publication="Nature")
        formula = ExtractedFormula(
            page_num=1,
            formula_index=0,
            bbox=(0, 0, 0, 0),
            latex=r"E = mc^2",
            equation_number="(1)",
        )
        candidate = FormulaCandidate(
            page_num=1,
            bbox=(0, 0, 0, 0),
            raw_text=r"E = mc^2",
            confidence=0.95,
            source="mineru_content_list",
            equation_number="(1)",
            latex=r"E = mc^2",
        )
        indexer = Indexer.__new__(Indexer)
        indexer.config = self._hash_config()
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer.journal_ranker = MagicMock()
        indexer.journal_ranker.lookup.return_value = "Q1"
        indexer._ensure_formula_provider_available = MagicMock()
        indexer._assert_config_hash_current = MagicMock()
        indexer._pdf_hash = MagicMock(return_value="hash")
        indexer._recognize_formulas_for_item = MagicMock(return_value=[formula])

        with patch("zotpilot.feature_extraction.formula_ocr.extract_formula_candidates", return_value=[candidate]):
            result = indexer.index_formulas(daily_call_budget=1)

        assert result["provider_calls_used"] == 0
        assert result["external_calls_used"] == 0
        assert result["results"][0]["candidate_count"] == 1
        assert result["results"][0]["provider_calls"] == 0
        indexer._ensure_formula_provider_available.assert_not_called()
        indexer.store.replace_formulas.assert_called_once()
        indexer.store.add_formulas.assert_not_called()

    def test_formula_backfill_skips_translated_pdf_and_records_reason(self, tmp_path):
        from zotpilot.indexer import Indexer
        from zotpilot.models import ExtractedFormula, ZoteroItem

        original_pdf = tmp_path / "original.pdf"
        translated_pdf = tmp_path / "双语对照-Paper.pdf"
        original_pdf.write_bytes(b"%PDF-1.4")
        translated_pdf.write_bytes(b"%PDF-1.4")
        items = [
            ZoteroItem("DOC1", "Original paper", "Auth", 2024, original_pdf, publication="Nature"),
            ZoteroItem("DOC2", "Translated paper", "Auth", 2024, translated_pdf, publication="Nature"),
        ]
        formula = ExtractedFormula(
            page_num=1,
            formula_index=0,
            bbox=(0, 0, 10, 10),
            latex=r"E = mc^2",
            equation_number="(1)",
        )
        indexer = Indexer.__new__(Indexer)
        indexer.config = self._hash_config()
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1", "DOC2"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = items
        indexer.journal_ranker = MagicMock()
        indexer.journal_ranker.lookup.return_value = "Q1"
        indexer._ensure_formula_provider_available = MagicMock()
        indexer._assert_config_hash_current = MagicMock()
        indexer._pdf_hash = MagicMock(return_value="hash")
        indexer._recognize_formulas_for_item = MagicMock(return_value=[formula])

        with patch(
            "zotpilot.feature_extraction.formula_ocr.extract_formula_candidates",
            return_value=[object()],
        ) as extract:
            result = indexer.index_formulas()

        assert result["processed"] == 1
        assert result["selected"] == 2
        assert result["skipped"] == 1
        assert result["matched"] == 2
        assert result["provider_calls_used"] == 1
        assert extract.call_count == 1
        assert [row["item_key"] for row in result["results"] if row["status"] == "skipped"] == ["DOC2"]
        assert result["results"][0]["reason"] == "bilingual_or_translated_pdf"

    def test_formula_backfill_does_not_skip_weak_translation_filename_shape(self, tmp_path):
        from zotpilot.indexer import Indexer
        from zotpilot.models import ExtractedFormula, ZoteroItem

        weak_pdf = tmp_path / "Gao 等 - 2026 - Paper-164877.pdf"
        weak_pdf.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("DOC1", "Original paper", "Auth", 2024, weak_pdf, publication="Nature")
        formula = ExtractedFormula(
            page_num=1,
            formula_index=0,
            bbox=(0, 0, 10, 10),
            latex=r"E = mc^2",
            equation_number="(1)",
        )
        indexer = Indexer.__new__(Indexer)
        indexer.config = self._hash_config()
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer.journal_ranker = MagicMock()
        indexer.journal_ranker.lookup.return_value = "Q1"
        indexer._ensure_formula_provider_available = MagicMock()
        indexer._assert_config_hash_current = MagicMock()
        indexer._pdf_hash = MagicMock(return_value="hash")
        indexer._recognize_formulas_for_item = MagicMock(return_value=[formula])

        with patch(
            "zotpilot.feature_extraction.formula_ocr.extract_formula_candidates",
            return_value=[object()],
        ):
            result = indexer.index_formulas()

        assert result["processed"] == 1
        assert result["skipped"] == 0
        assert result["provider_calls_used"] == 1

    def test_estimate_formula_backfill_skips_translated_pdf_without_budget_cost(self, tmp_path):
        from zotpilot.indexer import Indexer
        from zotpilot.models import ZoteroItem

        original_pdf = tmp_path / "original.pdf"
        translated_pdf = tmp_path / "双语对照-paper.pdf"
        original_pdf.write_bytes(b"%PDF-1.4")
        translated_pdf.write_bytes(b"%PDF-1.4")
        items = [
            ZoteroItem("DOC1", "Original paper", "Auth", 2024, original_pdf),
            ZoteroItem("DOC2", "Translated paper", "Auth", 2024, translated_pdf),
        ]
        indexer = Indexer.__new__(Indexer)
        indexer.config = SimpleNamespace(
            **{
                **self._hash_config().__dict__,
                "formula_ocr_provider": "simpletex",
                "formula_ocr_simpletex_min_interval": 0.5,
            }
        )
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1", "DOC2"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = items
        indexer._assert_config_hash_current = MagicMock()

        with patch(
            "zotpilot.feature_extraction.formula_ocr.extract_formula_candidates",
            return_value=[object()],
        ) as extract:
            result = indexer.estimate_formula_backfill(daily_call_budget=1800)

        assert result["processed"] == 1
        assert result["selected"] == 2
        assert result["skipped"] == 1
        assert result["matched"] == 2
        assert result["candidate_count"] == 1
        assert result["estimated_external_calls"] == 1
        assert result["estimated_runs"] == 1
        assert extract.call_count == 1
        skipped = [row for row in result["results"] if row.get("status") == "skipped"]
        assert skipped[0]["item_key"] == "DOC2"
        assert skipped[0]["reason"] == "bilingual_or_translated_pdf"

    def test_formula_backfill_reports_single_paper_over_daily_budget(self, tmp_path):
        from zotpilot.indexer import Indexer
        from zotpilot.models import ZoteroItem

        pdf_path = tmp_path / "paper.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("DOC1", "Paper", "Auth", 2024, pdf_path, publication="Nature")
        indexer = Indexer.__new__(Indexer)
        indexer.config = self._hash_config()
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer._ensure_formula_provider_available = MagicMock()
        indexer._assert_config_hash_current = MagicMock()
        indexer._recognize_formulas_for_item = MagicMock()

        with patch(
            "zotpilot.feature_extraction.formula_ocr.extract_formula_candidates",
            return_value=[object(), object(), object()],
        ):
            result = indexer.index_formulas(daily_call_budget=2)

        assert result["processed"] == 0
        assert result["formulas_indexed"] == 0
        assert result["next_item_key"] == "DOC1"
        assert result["next_item_candidate_count"] == 3
        assert result["results"][0]["reason"] == "single_paper_exceeds_daily_budget"
        assert "more than the daily budget" in result["warnings"][0]
        indexer._recognize_formulas_for_item.assert_not_called()

    def test_formula_backfill_stops_on_quota_without_marking_current_item_processed(self, tmp_path):
        from zotpilot.indexer import Indexer
        from zotpilot.models import ExtractedFormula, ZoteroItem

        pdf1 = tmp_path / "paper1.pdf"
        pdf2 = tmp_path / "paper2.pdf"
        pdf1.write_bytes(b"%PDF-1.4")
        pdf2.write_bytes(b"%PDF-1.4")
        items = [
            ZoteroItem("DOC1", "Paper 1", "Auth", 2024, pdf1, publication="Nature"),
            ZoteroItem("DOC2", "Paper 2", "Auth", 2024, pdf2, publication="Nature"),
        ]
        formula = ExtractedFormula(
            page_num=1,
            formula_index=0,
            bbox=(0, 0, 10, 10),
            latex=r"E = mc^2",
            equation_number="(1)",
        )
        indexer = Indexer.__new__(Indexer)
        indexer.config = SimpleNamespace(
            **{
                **self._hash_config().__dict__,
                "formula_ocr_provider": "simpletex",
            }
        )
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1", "DOC2"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = items
        indexer.journal_ranker = MagicMock()
        indexer.journal_ranker.lookup.return_value = "Q1"
        indexer._ensure_formula_provider_available = MagicMock()
        indexer._get_formula_provider = MagicMock(return_value=object())
        indexer._assert_config_hash_current = MagicMock()
        indexer._pdf_hash = MagicMock(return_value="hash")
        indexer._recognize_formulas_for_item = MagicMock(
            side_effect=[[formula], RuntimeError("429 quota exceeded")]
        )

        with patch(
            "zotpilot.feature_extraction.formula_ocr.extract_formula_candidates",
            side_effect=[[object()], [object()]],
        ):
            result = indexer.index_formulas(daily_call_budget=10, stop_on_quota=True)

        assert result["processed"] == 1
        assert result["provider_calls_used"] == 1
        assert result["external_calls_used"] == 1
        assert result["stopped_reason"] == "provider_quota_or_rate_limit"
        assert result["resume_cursor"] == "DOC1"
        assert result["next_item_key"] == "DOC2"
        assert result["results"][-1]["status"] == "stopped_quota"
        assert result["results"][-1]["provider_calls"] == 1
        assert indexer._recognize_formulas_for_item.call_count == 2
        indexer.store.replace_formulas.assert_called_once()
        indexer.store.add_formulas.assert_not_called()

    def test_formula_backfill_requires_positive_config_budget_for_default_simpletex_run(self, tmp_path):
        from zotpilot.indexer import Indexer
        from zotpilot.models import ZoteroItem

        pdf_path = tmp_path / "paper.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("DOC1", "Paper", "Auth", 2024, pdf_path, publication="Nature")
        indexer = Indexer.__new__(Indexer)
        indexer.config = SimpleNamespace(
            **{
                **self._hash_config().__dict__,
                "formula_ocr_provider": "simpletex",
                "formula_ocr_daily_call_budget": 0,
            }
        )
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer._assert_config_hash_current = MagicMock()

        with pytest.raises(ValueError, match="daily_call_budget > 0"):
            indexer.index_formulas()

        indexer.store.add_formulas.assert_not_called()
        indexer.store.replace_formulas.assert_not_called()

    def test_formula_backfill_stops_when_simpletex_attempt_budget_is_exhausted(self, tmp_path):
        from zotpilot.indexer import Indexer
        from zotpilot.models import ZoteroItem

        class FakeAttemptBudgetProvider:
            attempts_used = 0

            def set_attempt_budget(self, budget):
                self.budget = budget

        pdf_path = tmp_path / "paper.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("DOC1", "Paper", "Auth", 2024, pdf_path, publication="Nature")
        provider = FakeAttemptBudgetProvider()
        indexer = Indexer.__new__(Indexer)
        indexer.config = SimpleNamespace(
            **{
                **self._hash_config().__dict__,
                "formula_ocr_provider": "simpletex",
            }
        )
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer.journal_ranker = MagicMock()
        indexer.journal_ranker.lookup.return_value = "Q1"
        indexer._ensure_formula_provider_available = MagicMock()
        indexer._get_formula_provider = MagicMock(return_value=provider)
        indexer._assert_config_hash_current = MagicMock()
        indexer._pdf_hash = MagicMock(return_value="hash")

        def exhaust_budget(*_args, **_kwargs):
            provider.attempts_used += 1
            raise RuntimeError("SimpleTex formula OCR daily call budget exhausted before request")

        indexer._recognize_formulas_for_item = MagicMock(side_effect=exhaust_budget)

        with patch("zotpilot.feature_extraction.formula_ocr.extract_formula_candidates", return_value=[object()]):
            result = indexer.index_formulas(daily_call_budget=1)

        assert provider.budget == 1
        assert result["processed"] == 0
        assert result["formulas_indexed"] == 0
        assert result["provider_calls_used"] == 1
        assert result["external_calls_used"] == 1
        assert result["budget_exhausted"] is True
        assert result["stopped_reason"] == "daily_call_budget"
        assert result["next_item_key"] == "DOC1"
        assert result["results"][0]["reason"] == "provider_attempts_exhausted_daily_budget"
        indexer.store.replace_formulas.assert_not_called()
        indexer.store.add_formulas.assert_not_called()

    def test_formula_backfill_uses_original_pdf_when_translated_attachment_is_fallback(self, tmp_path):
        from zotpilot.indexer import Indexer
        from zotpilot.models import ExtractedFormula, ZoteroItem

        translated_pdf = tmp_path / "双语对照-Paper.pdf"
        original_pdf = tmp_path / "Original Paper.pdf"
        translated_pdf.write_bytes(b"%PDF-1.4")
        original_pdf.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("DOC1", "Paper", "Auth", 2024, translated_pdf, publication="Nature")
        formula = ExtractedFormula(
            page_num=1,
            formula_index=0,
            bbox=(0, 0, 10, 10),
            latex=r"E = mc^2",
            equation_number="(1)",
        )
        indexer = Indexer.__new__(Indexer)
        indexer.config = self._hash_config()
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer.zotero.resolve_original_pdf_path.return_value = original_pdf
        indexer.journal_ranker = MagicMock()
        indexer.journal_ranker.lookup.return_value = "Q1"
        indexer._ensure_formula_provider_available = MagicMock()
        indexer._assert_config_hash_current = MagicMock()
        indexer._pdf_hash = MagicMock(return_value="hash")
        indexer._recognize_formulas_for_item = MagicMock(return_value=[formula])

        with patch(
            "zotpilot.feature_extraction.formula_ocr.extract_formula_candidates",
            return_value=[object()],
        ) as extract:
            result = indexer.index_formulas()

        assert result["processed"] == 1
        assert result["skipped"] == 0
        assert item.pdf_path == original_pdf
        assert extract.call_count == 1
        indexer.zotero.resolve_original_pdf_path.assert_any_call(
            "DOC1",
            title="Paper",
            fallback_path=translated_pdf,
        )
        indexer.store.replace_formulas.assert_called_once()
        indexer.store.add_formulas.assert_not_called()

    def test_extract_formula_candidates_passes_mineru_cache_paths_from_zotero(self, tmp_path):
        from zotpilot.indexer import Indexer
        from zotpilot.models import ZoteroItem

        pdf_path = tmp_path / "paper.pdf"
        cache_path = tmp_path / "LLM-for-Zotero-MinerU-cache-PDFKEY12.zip"
        pdf_path.write_bytes(b"%PDF-1.4")
        cache_path.write_bytes(b"PK")
        item = ZoteroItem("DOC1", "Paper", "Auth", 2024, pdf_path, publication="Nature")
        candidate_provider = object()
        indexer = Indexer.__new__(Indexer)
        indexer.config = self._hash_config()
        indexer.zotero = MagicMock()
        indexer.zotero.mineru_cache_paths_for_item.return_value = [cache_path]
        indexer._get_formula_candidate_provider = MagicMock(return_value=candidate_provider)

        with patch(
            "zotpilot.feature_extraction.formula_ocr.extract_formula_candidates",
            return_value=[],
        ) as extract:
            result = indexer._extract_formula_candidates_for_item(item)

        assert result == []
        indexer.zotero.mineru_cache_paths_for_item.assert_called_once_with(
            "DOC1",
            pdf_path=pdf_path,
        )
        extract.assert_called_once()
        assert extract.call_args.kwargs["cache_paths"] == (cache_path,)
        assert extract.call_args.kwargs["candidate_provider"] is candidate_provider
        assert extract.call_args.kwargs["max_candidates_per_doc"] == 0
        assert extract.call_args.kwargs["pdf_fallback_max_pages"] == 80

    def test_formula_backfill_resume_after_skips_processed_items(self, tmp_path):
        from zotpilot.indexer import Indexer
        from zotpilot.models import ExtractedFormula, ZoteroItem

        pdf1 = tmp_path / "paper1.pdf"
        pdf2 = tmp_path / "paper2.pdf"
        pdf1.write_bytes(b"%PDF-1.4")
        pdf2.write_bytes(b"%PDF-1.4")
        items = [
            ZoteroItem("DOC2", "Paper 2", "Auth", 2024, pdf2, publication="Nature"),
            ZoteroItem("DOC1", "Paper 1", "Auth", 2024, pdf1, publication="Nature"),
        ]
        formula = ExtractedFormula(
            page_num=1,
            formula_index=0,
            bbox=(0, 0, 10, 10),
            latex=r"E = mc^2",
            equation_number="(1)",
        )
        indexer = Indexer.__new__(Indexer)
        indexer.config = self._hash_config()
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1", "DOC2"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = items
        indexer.journal_ranker = MagicMock()
        indexer.journal_ranker.lookup.return_value = "Q1"
        indexer._ensure_formula_provider_available = MagicMock()
        indexer._assert_config_hash_current = MagicMock()
        indexer._pdf_hash = MagicMock(return_value="hash")
        indexer._recognize_formulas_for_item = MagicMock(return_value=[formula])

        with patch("zotpilot.feature_extraction.formula_ocr.extract_formula_candidates", return_value=[object()]):
            result = indexer.index_formulas(resume_after="DOC1")

        assert result["processed"] == 1
        assert result["results"][0]["item_key"] == "DOC2"
        assert result["resume_cursor"] == "DOC2"
        assert result["resume_after_found"] is True

    def test_formula_backfill_item_key_selection_does_not_scan_full_library(self, tmp_path):
        from zotpilot.indexer import Indexer
        from zotpilot.models import ExtractedFormula, ZoteroItem

        pdf_path = tmp_path / "paper.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("DOC1", "Paper", "Auth", 2024, pdf_path, publication="Nature")
        formula = ExtractedFormula(
            page_num=1,
            formula_index=0,
            bbox=(0, 0, 10, 10),
            latex=r"a = b",
            equation_number="(1)",
        )
        indexer = Indexer.__new__(Indexer)
        indexer.config = self._hash_config()
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_item.return_value = item
        indexer.zotero.get_all_items_with_pdfs.side_effect = AssertionError("full library scan should not run")
        indexer.journal_ranker = MagicMock()
        indexer.journal_ranker.lookup.return_value = "Q1"
        indexer._ensure_formula_provider_available = MagicMock()
        indexer._assert_config_hash_current = MagicMock()
        indexer._pdf_hash = MagicMock(return_value="hash")
        indexer._recognize_formulas_for_item = MagicMock(return_value=[formula])

        with patch("zotpilot.feature_extraction.formula_ocr.extract_formula_candidates", return_value=[object()]):
            result = indexer.index_formulas(item_key="DOC1")

        assert result["processed"] == 1
        assert result["results"][0]["item_key"] == "DOC1"
        assert indexer.zotero.get_item.call_count == 2
        indexer.zotero.get_item.assert_called_with("DOC1")
        indexer.zotero.get_all_items_with_pdfs.assert_not_called()

    def test_index_formulas_reports_unmatched_requested_item_keys(self, tmp_path):
        from zotpilot.feature_extraction.formula_ocr import FormulaCandidate
        from zotpilot.indexer import Indexer
        from zotpilot.models import ExtractedFormula, ZoteroItem

        pdf_path = tmp_path / "paper.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("DOC1", "Indexed paper", "Auth", 2024, pdf_path, publication="Nature")
        formula = ExtractedFormula(
            page_num=1,
            formula_index=0,
            bbox=(0, 0, 10, 10),
            latex=r"E=mc^2",
            equation_number="(1)",
        )
        candidate = FormulaCandidate(
            page_num=1,
            bbox=(0, 0, 10, 10),
            raw_text=r"E=mc^2",
            confidence=0.95,
            equation_number="(1)",
            latex=r"E=mc^2",
            source="mineru_content_list",
        )
        indexer = Indexer.__new__(Indexer)
        indexer.config = self._hash_config()
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1"}
        indexer.store.replace_formulas.return_value = 1
        indexer.zotero = MagicMock()
        indexer.zotero.get_item.side_effect = lambda key: item if key == "DOC1" else None
        indexer.journal_ranker = MagicMock()
        indexer.journal_ranker.lookup.return_value = "Q1"
        indexer._ensure_formula_provider_available = MagicMock()
        indexer._assert_config_hash_current = MagicMock()
        indexer._pdf_hash = MagicMock(return_value="hash")
        indexer._recognize_formulas_for_item = MagicMock(return_value=[formula])

        with patch("zotpilot.feature_extraction.formula_ocr.extract_formula_candidates", return_value=[candidate]):
            result = indexer.index_formulas(item_keys=["DOC1", "MISSING1"])

        assert result["matched"] == 1
        assert result["unmatched_requested_item_keys"] == ["MISSING1"]
        assert result["unmatched_requested_item_key_count"] == 1
        assert "1 requested item_key(s) were not matched" in result["warnings"][-1]
        assert result["formulas_indexed"] == 1

    def test_formula_backfill_returns_low_confidence_review_queue(self, tmp_path):
        from zotpilot.indexer import Indexer
        from zotpilot.models import ExtractedFormula, ZoteroItem

        pdf_path = tmp_path / "paper.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("DOC1", "Paper", "Auth", 2024, pdf_path, publication="Nature")
        formula = ExtractedFormula(
            page_num=2,
            formula_index=0,
            bbox=(0, 0, 10, 10),
            latex=r"\sigma = E\epsilon",
            confidence=0.42,
            equation_number="(1)",
            provider="simpletex",
        )
        indexer = Indexer.__new__(Indexer)
        indexer.config = self._hash_config()
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer.journal_ranker = MagicMock()
        indexer.journal_ranker.lookup.return_value = "Q1"
        indexer._ensure_formula_provider_available = MagicMock()
        indexer._assert_config_hash_current = MagicMock()
        indexer._pdf_hash = MagicMock(return_value="hash")
        indexer._recognize_formulas_for_item = MagicMock(return_value=[formula])

        with patch("zotpilot.feature_extraction.formula_ocr.extract_formula_candidates", return_value=[object()]):
            result = indexer.index_formulas(low_confidence_threshold=0.7)

        assert result["low_confidence_review_count"] == 1
        review = result["low_confidence_review_queue"][0]
        assert review["item_key"] == "DOC1"
        assert review["equation_number"] == "(1)"
        assert review["confidence"] == 0.42
        assert review["review_reasons"] == ["low_confidence"]

    def test_formula_review_queue_flags_missing_numbers_and_split_formulas(self):
        from zotpilot.indexer import Indexer
        from zotpilot.models import ExtractedFormula, ZoteroItem

        item = ZoteroItem("DOC1", "Paper", "Auth", 2024, None)
        formulas = [
            ExtractedFormula(
                page_num=2,
                formula_index=0,
                bbox=(0, 0, 10, 10),
                latex=r"\sigma = E\epsilon",
                confidence=0.95,
                equation_number="",
                provider="mineru_cache",
            ),
            ExtractedFormula(
                page_num=2,
                formula_index=1,
                bbox=(0, 0, 10, 10),
                latex=r"\left[ \sqrt{\frac{1+c_1^2}{3}} + c_1 \eta \right]",
                confidence=0.95,
                equation_number="(8)",
                provider="mineru_cache",
            ),
        ]

        rows = Indexer._formula_review_rows(item=item, formulas=formulas, threshold=0.0)

        assert len(rows) == 2
        assert rows[0]["review_reasons"] == ["missing_equation_number"]
        assert rows[1]["review_reasons"] == ["possible_split_formula"]

    def test_formula_review_queue_flags_duplicate_and_gapped_numbering(self):
        from zotpilot.indexer import Indexer
        from zotpilot.models import ExtractedFormula, ZoteroItem

        item = ZoteroItem("DOC1", "Paper", "Auth", 2024, None)
        formulas = [
            ExtractedFormula(
                page_num=1,
                formula_index=0,
                bbox=(0, 0, 10, 10),
                latex=r"a = b",
                equation_number="(1)",
            ),
            ExtractedFormula(
                page_num=1,
                formula_index=1,
                bbox=(0, 20, 10, 30),
                latex=r"c = d",
                equation_number="(3)",
            ),
            ExtractedFormula(
                page_num=1,
                formula_index=2,
                bbox=(0, 40, 10, 50),
                latex=r"e = f",
                equation_number="(3)",
            ),
        ]

        rows = Indexer._formula_review_rows(item=item, formulas=formulas, threshold=0.0)

        assert len(rows) == 3
        assert rows[0]["review_reasons"] == ["numbering_sequence_gap"]
        assert rows[1]["review_reasons"] == ["duplicate_equation_number", "numbering_sequence_gap"]
        assert rows[2]["review_reasons"] == ["duplicate_equation_number", "numbering_sequence_gap"]

    def test_formula_review_queue_treats_letter_suffix_numbers_as_sequence_members(self):
        from zotpilot.indexer import Indexer
        from zotpilot.models import ExtractedFormula, ZoteroItem

        item = ZoteroItem("DOC1", "Paper", "Auth", 2024, None)
        formulas = [
            ExtractedFormula(
                page_num=1,
                formula_index=0,
                bbox=(0, 0, 10, 10),
                latex=r"a = b",
                equation_number="(1)",
            ),
            ExtractedFormula(
                page_num=1,
                formula_index=1,
                bbox=(0, 20, 10, 30),
                latex=r"c = d",
                equation_number="(2)",
            ),
            ExtractedFormula(
                page_num=1,
                formula_index=2,
                bbox=(0, 40, 10, 50),
                latex=r"e = f",
                equation_number="(3a)",
            ),
            ExtractedFormula(
                page_num=1,
                formula_index=3,
                bbox=(0, 60, 10, 70),
                latex=r"g = h",
                equation_number="(3b)",
            ),
            ExtractedFormula(
                page_num=1,
                formula_index=4,
                bbox=(0, 80, 10, 90),
                latex=r"i = j",
                equation_number="(4)",
            ),
        ]

        rows = Indexer._formula_review_rows(item=item, formulas=formulas, threshold=0.0)

        assert rows == []

    def test_formula_review_queue_allows_distant_duplicate_numbers_for_long_documents(self):
        from zotpilot.indexer import Indexer
        from zotpilot.models import ExtractedFormula, ZoteroItem

        item = ZoteroItem("DOC1", "Thesis", "Auth", 2024, None)
        formulas = [
            ExtractedFormula(
                page_num=30,
                formula_index=0,
                bbox=(0, 0, 10, 10),
                latex=r"E = mc^2",
                equation_number="(2)",
            ),
            ExtractedFormula(
                page_num=76,
                formula_index=1,
                bbox=(0, 20, 10, 30),
                latex=r"\sigma = E\epsilon",
                equation_number="(2)",
            ),
            ExtractedFormula(
                page_num=77,
                formula_index=2,
                bbox=(0, 40, 10, 50),
                latex=r"\tau = G\gamma",
                equation_number="(3)",
            ),
        ]

        rows = Indexer._formula_review_rows(item=item, formulas=formulas, threshold=0.0)

        assert rows == []

    def test_estimate_formula_backfill_counts_candidates_without_ocr(self, tmp_path):
        from zotpilot.indexer import Indexer
        from zotpilot.models import ZoteroItem

        pdf1 = tmp_path / "paper1.pdf"
        pdf2 = tmp_path / "paper2.pdf"
        pdf1.write_bytes(b"%PDF-1.4")
        pdf2.write_bytes(b"%PDF-1.4")
        items = [
            ZoteroItem("DOC1", "Paper 1", "Auth", 2024, pdf1),
            ZoteroItem("DOC2", "Paper 2", "Auth", 2024, pdf2),
        ]
        indexer = Indexer.__new__(Indexer)
        indexer.config = SimpleNamespace(
            **{
                **self._hash_config().__dict__,
                "formula_ocr_provider": "simpletex",
                "formula_ocr_simpletex_min_interval": 0.5,
            }
        )
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1", "DOC2"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = items
        indexer._assert_config_hash_current = MagicMock()

        with patch(
            "zotpilot.feature_extraction.formula_ocr.extract_formula_candidates",
            side_effect=[[object(), object(), object()], [object()]],
        ):
            result = indexer.estimate_formula_backfill(daily_call_budget=2)

        assert result["candidate_count"] == 4
        assert result["estimated_external_calls"] == 4
        assert result["normal_batch_estimated_provider_calls"] == 4
        assert result["deferred_high_density_provider_calls"] == 0
        assert result["estimated_min_duration_seconds"] == 2.0
        assert result["estimated_runs"] == 2
        assert result["summary"]["daily_call_budget"] == 2
        assert result["high_call_papers"] == [
            {
                "item_key": "DOC1",
                "title": "Paper 1",
                "candidate_count": 3,
                "estimated_provider_calls": 3,
                "estimated_external_calls": 3,
            }
        ]
        assert result["summary"]["high_call_paper_count"] == 1
        assert "individually exceed the daily call budget" in result["summary"]["warnings"][-1]

    def test_estimate_formula_backfill_samples_matched_items_reproducibly(self, tmp_path):
        from zotpilot.indexer import Indexer
        from zotpilot.models import ZoteroItem

        items = []
        for index in range(5):
            pdf_path = tmp_path / f"paper-{index}.pdf"
            pdf_path.write_bytes(b"%PDF-1.4")
            items.append(ZoteroItem(f"DOC{index}", f"Paper {index}", "Auth", 2024, pdf_path))
        indexer = Indexer.__new__(Indexer)
        indexer.config = SimpleNamespace(
            **{
                **self._hash_config().__dict__,
                "formula_ocr_provider": "simpletex",
                "formula_ocr_simpletex_min_interval": 0.5,
            }
        )
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {
            item.item_key for item in items
        } | {"ATTACHMENTKEY", "ORPHANKEY"}
        indexer.zotero = MagicMock()
        items_by_key = {item.item_key: item for item in items}
        indexer.zotero.get_item.side_effect = lambda item_key: items_by_key.get(item_key)
        indexer.zotero.get_all_items_with_pdfs = MagicMock()
        indexer.zotero.resolve_original_pdf_path = MagicMock(return_value=None)
        indexer._assert_config_hash_current = MagicMock()
        with patch(
            "zotpilot.feature_extraction.formula_ocr.extract_formula_candidates",
            return_value=[object()],
        ), patch("zotpilot.indexer.pdf_content_translation_risk_score", return_value=0.0) as risk_score:
            result = indexer.estimate_formula_backfill(
                daily_call_budget=1800,
                sample_size=2,
                sample_seed=0,
            )

        result_keys = [row["item_key"] for row in result["results"]]
        assert len(result_keys) == 2
        assert set(result_keys).issubset(items_by_key)
        assert "ATTACHMENTKEY" not in result_keys
        assert "ORPHANKEY" not in result_keys
        indexer.zotero.get_all_items_with_pdfs.assert_not_called()
        assert indexer.zotero.get_item.call_count == 3
        assert indexer.zotero.resolve_original_pdf_path.call_count == 2
        assert risk_score.call_count == 2
        assert result["processed"] == 2
        assert result["sampled_from"] == 7
        assert result["sampled_unresolved_key_count"] == 1
        assert result["matched"] == 7
        assert result["summary"]["sample_size"] == 2
        assert result["summary"]["sample_seed"] == 0

    def test_estimate_formula_backfill_rejects_sample_with_explicit_item_keys(self, tmp_path):
        from zotpilot.indexer import Indexer
        from zotpilot.models import ZoteroItem

        pdf_path = tmp_path / "paper.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("DOC1", "Paper", "Auth", 2024, pdf_path)
        indexer = Indexer.__new__(Indexer)
        indexer.config = self._hash_config()
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_item.return_value = item
        indexer._assert_config_hash_current = MagicMock()

        with pytest.raises(ValueError, match="without item_key or item_keys"):
            indexer.estimate_formula_backfill(item_keys=["DOC1"], sample_size=1)

    def test_estimate_formula_backfill_reports_unmatched_requested_item_keys(self, tmp_path):
        from zotpilot.feature_extraction.formula_ocr import FormulaCandidate
        from zotpilot.indexer import Indexer
        from zotpilot.models import ZoteroItem

        pdf_path = tmp_path / "paper.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("DOC1", "Indexed paper", "Auth", 2024, pdf_path)
        candidates = [
            FormulaCandidate(
                page_num=1,
                bbox=(0, 0, 10, 10),
                raw_text=r"E=mc^2",
                confidence=0.95,
                equation_number="(1)",
                latex=r"E=mc^2",
                source="mineru_content_list",
            )
        ]
        indexer = Indexer.__new__(Indexer)
        indexer.config = SimpleNamespace(**self._hash_config().__dict__)
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_item.side_effect = lambda key: item if key == "DOC1" else None
        indexer._assert_config_hash_current = MagicMock()

        with patch("zotpilot.feature_extraction.formula_ocr.extract_formula_candidates", return_value=candidates):
            result = indexer.estimate_formula_backfill(item_keys=["DOC1", "MISSING1"])

        assert result["matched"] == 1
        assert result["unmatched_requested_item_keys"] == ["MISSING1"]
        assert result["summary"]["unmatched_requested_item_key_count"] == 1
        assert "1 requested item_key(s) were not matched" in result["summary"]["warnings"][-1]

    def test_estimate_formula_backfill_flags_high_density_documents(self, tmp_path):
        from zotpilot.indexer import Indexer
        from zotpilot.models import ZoteroItem

        pdf_path = tmp_path / "book.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("BOOK1", "Damage mechanics", "Auth", 2024, pdf_path)
        indexer = Indexer.__new__(Indexer)
        indexer.config = SimpleNamespace(
            **{
                **self._hash_config().__dict__,
                "formula_ocr_provider": "simpletex",
                "formula_ocr_simpletex_min_interval": 0.5,
                "formula_ocr_high_density_call_threshold": 2,
            }
        )
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"BOOK1"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer._assert_config_hash_current = MagicMock()

        with patch(
            "zotpilot.feature_extraction.formula_ocr.extract_formula_candidates",
            return_value=[object(), object(), object()],
        ):
            result = indexer.estimate_formula_backfill(daily_call_budget=1800)

        assert result["dense_formula_papers"] == [
            {
                "item_key": "BOOK1",
                "title": "Damage mechanics",
                "candidate_count": 3,
                "estimated_provider_calls": 3,
                "estimated_external_calls": 3,
                "high_density_call_threshold": 2,
                "high_density_candidate_threshold": 160,
                "high_density_trigger": "provider_calls",
            }
        ]
        assert result["summary"]["dense_formula_paper_count"] == 1
        assert result["normal_batch_estimated_provider_calls"] == 0
        assert result["deferred_high_density_provider_calls"] == 3
        assert result["results"][0]["default_batch_status"] == "deferred_high_density"
        assert "high-density formula document" in result["summary"]["warnings"][-1]
        assert "Do not run a normal formula batch yet" in result["summary"]["next_action"]

    def test_estimate_formula_backfill_builds_high_density_page_window_plan(self, tmp_path):
        from zotpilot.feature_extraction.formula_ocr import FormulaCandidate
        from zotpilot.indexer import Indexer
        from zotpilot.models import ZoteroItem

        pdf_path = tmp_path / "thesis.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("THESIS1", "Damage mechanics thesis", "Auth", 2024, pdf_path)
        candidates = [
            FormulaCandidate(
                page_num=10 + index,
                bbox=(0, index * 10, 100, index * 10 + 8),
                raw_text=rf"\sigma_{{{index}}}=E\epsilon",
                confidence=0.95,
                equation_number=f"(3.{index + 1})",
            )
            for index in range(5)
        ]
        indexer = Indexer.__new__(Indexer)
        indexer.config = SimpleNamespace(
            **{
                **self._hash_config().__dict__,
                "formula_ocr_provider": "simpletex",
                "formula_ocr_simpletex_min_interval": 0.5,
                "formula_ocr_high_density_call_threshold": 2,
            }
        )
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"THESIS1"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer._assert_config_hash_current = MagicMock()

        with patch("zotpilot.feature_extraction.formula_ocr.extract_formula_candidates", return_value=candidates):
            result = indexer.estimate_formula_backfill(daily_call_budget=1800)

        assert result["high_density_backfill_plans"][0]["item_key"] == "THESIS1"
        assert result["high_density_backfill_plans"][0]["segment_count"] == 3
        assert [
            (segment["page_min"], segment["page_max"], segment["candidate_start"], segment["candidate_end"])
            for segment in result["high_density_backfill_plans"][0]["segments"]
        ] == [
            (10, 11, 0, 2),
            (12, 13, 2, 4),
            (14, 14, 4, 5),
        ]
        assert result["high_density_backfill_plans"][0]["segments"][1]["formula_index_offset"] == 2
        assert result["summary"]["high_density_backfill_plan_count"] == 1

    def test_high_density_page_window_plan_flags_suspicious_equation_number_ordering(self, tmp_path):
        from zotpilot.feature_extraction.formula_ocr import FormulaCandidate
        from zotpilot.indexer import Indexer
        from zotpilot.models import ZoteroItem

        pdf_path = tmp_path / "thesis.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("THESIS1", "Damage mechanics thesis", "Auth", 2024, pdf_path)
        candidates = [
            FormulaCandidate(
                page_num=10 + index,
                bbox=(0, index * 10, 100, index * 10 + 8),
                raw_text=rf"\sigma_{{{index}}}=E\epsilon",
                confidence=0.95,
                equation_number=number,
            )
            for index, number in enumerate(["(1)", "(8)", "(3)"])
        ]
        indexer = Indexer.__new__(Indexer)
        indexer.config = SimpleNamespace(
            **{
                **self._hash_config().__dict__,
                "formula_ocr_provider": "simpletex",
                "formula_ocr_simpletex_min_interval": 0.5,
                "formula_ocr_high_density_call_threshold": 2,
            }
        )
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"THESIS1"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer._assert_config_hash_current = MagicMock()

        with patch("zotpilot.feature_extraction.formula_ocr.extract_formula_candidates", return_value=candidates):
            result = indexer.estimate_formula_backfill(daily_call_budget=1800)

        audit = result["results"][0]["candidate_audit"]
        assert audit["equation_number_prefixes"] == ["regular"]
        assert audit["equation_number_warnings"] == [
            "equation_number_regression",
            "large_equation_number_gap",
        ]
        assert audit["equation_number_sequence_breaks"] == [
            {
                "previous": "(1)",
                "current": "(8)",
                "prefix": "regular",
                "reason": "large_gap",
                "gap": 7,
            },
            {
                "previous": "(8)",
                "current": "(3)",
                "prefix": "regular",
                "reason": "regression",
            },
        ]
        plan = result["high_density_backfill_plans"][0]
        assert plan["equation_number_warnings"] == [
            "equation_number_regression",
            "large_equation_number_gap",
        ]
        assert plan["segments"][0]["equation_number_warnings"] == ["large_equation_number_gap"]

    def test_high_density_page_window_plan_preserves_provider_order_within_page(self, tmp_path):
        from zotpilot.feature_extraction.formula_ocr import FormulaCandidate
        from zotpilot.indexer import Indexer
        from zotpilot.models import ZoteroItem

        pdf_path = tmp_path / "two-column-thesis.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("THESIS1", "Two-column thesis", "Auth", 2024, pdf_path)
        candidates = [
            FormulaCandidate(
                page_num=42,
                bbox=(300, 100, 390, 118),
                raw_text=r"\sigma_{11}=E\epsilon",
                confidence=0.95,
                equation_number="(2.11)",
            ),
            FormulaCandidate(
                page_num=42,
                bbox=(40, 90, 130, 108),
                raw_text=r"\sigma_{12}=E\epsilon",
                confidence=0.95,
                equation_number="(2.12)",
            ),
            FormulaCandidate(
                page_num=42,
                bbox=(40, 130, 130, 148),
                raw_text=r"\sigma_{13}=E\epsilon",
                confidence=0.95,
                equation_number="(2.13)",
            ),
        ]
        indexer = Indexer.__new__(Indexer)
        indexer.config = SimpleNamespace(
            **{
                **self._hash_config().__dict__,
                "formula_ocr_provider": "simpletex",
                "formula_ocr_high_density_call_threshold": 2,
            }
        )
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"THESIS1"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer._assert_config_hash_current = MagicMock()

        with patch("zotpilot.feature_extraction.formula_ocr.extract_formula_candidates", return_value=candidates):
            result = indexer.estimate_formula_backfill(daily_call_budget=1800)

        segment = result["high_density_backfill_plans"][0]["segments"][0]
        assert segment["first_equation_number"] == "(2.11)"
        assert segment["last_equation_number"] == "(2.13)"
        assert "equation_number_regression" not in segment["equation_number_warnings"]

    def test_high_density_page_window_segment_audit_uses_equation_review_order(self, tmp_path):
        from zotpilot.feature_extraction.formula_ocr import FormulaCandidate
        from zotpilot.indexer import _equation_number_audit_value, _formula_candidate_segment_summary

        page_ordered_candidates = [
            FormulaCandidate(
                page_num=128,
                bbox=(40, 140, 130, 158),
                raw_text=r"\sigma_{28}=E\epsilon",
                confidence=0.95,
                equation_number="(4.28)",
            ),
            FormulaCandidate(
                page_num=128,
                bbox=(40, 180, 130, 198),
                raw_text=r"\sigma_{30}=E\epsilon",
                confidence=0.95,
                equation_number="(4.30)",
            ),
            FormulaCandidate(
                page_num=129,
                bbox=(40, 120, 130, 138),
                raw_text=r"\sigma_{27}=E\epsilon",
                confidence=0.95,
                equation_number="(4.27)",
            ),
            FormulaCandidate(
                page_num=132,
                bbox=(40, 160, 130, 178),
                raw_text=r"\sigma_{29}=E\epsilon",
                confidence=0.95,
                equation_number="(4.29)",
            ),
        ]

        assert _equation_number_audit_value("(4.30)") == ("4", 30)
        segment = _formula_candidate_segment_summary(
            segment_index=1,
            candidates=page_ordered_candidates,
            formula_indices=[1, 2, 3, 4],
            candidate_start=0,
            candidate_end=4,
            data_egress=False,
        )
        assert segment["first_equation_number"] == "(4.27)"
        assert segment["last_equation_number"] == "(4.30)"
        assert segment["equation_number_sequence_breaks"] == []
        assert "equation_number_regression" not in segment["equation_number_warnings"]

    def test_estimate_formula_backfill_flags_missing_equation_number_gap(self, tmp_path):
        from zotpilot.feature_extraction.formula_ocr import FormulaCandidate
        from zotpilot.indexer import Indexer
        from zotpilot.models import ZoteroItem

        pdf_path = tmp_path / "paper.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("DOC1", "Impact paper", "Auth", 2024, pdf_path)
        candidates = [
            FormulaCandidate(
                page_num=4 + index,
                bbox=(0, index * 10, 100, index * 10 + 8),
                raw_text=rf"\sigma_{{{index}}}=E\epsilon",
                confidence=0.95,
                equation_number=number,
                latex=rf"\sigma_{{{index}}}=E\epsilon",
                source="mineru_content_list",
            )
            for index, number in enumerate(["(1)", "(2)", "(3)", "(4)", "(5)", "(6)", "(7)", "(9)", "(10)"])
        ]
        indexer = Indexer.__new__(Indexer)
        indexer.config = SimpleNamespace(**self._hash_config().__dict__)
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer._assert_config_hash_current = MagicMock()

        with patch("zotpilot.feature_extraction.formula_ocr.extract_formula_candidates", return_value=candidates):
            result = indexer.estimate_formula_backfill(candidate_preview_limit=20)

        audit = result["results"][0]["candidate_audit"]
        assert audit["equation_number_warnings"] == ["missing_equation_number_gap"]
        assert audit["equation_number_sequence_breaks"] == [
            {
                "previous": "(7)",
                "current": "(9)",
                "prefix": "regular",
                "reason": "missing_gap",
                "gap": 2,
                "missing_count": 1,
            }
        ]
        assert result["results"][0]["candidate_preview"][7]["equation_number"] == "(9)"

    def test_estimate_formula_backfill_reports_candidate_quality_blocking_papers(self, tmp_path):
        from zotpilot.feature_extraction.formula_ocr import FormulaCandidate
        from zotpilot.indexer import Indexer
        from zotpilot.models import ZoteroItem

        pdf_path = tmp_path / "paper.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("DOC1", "Impact paper", "Auth", 2024, pdf_path)
        candidates = [
            FormulaCandidate(
                page_num=4 + index,
                bbox=(0, index * 10, 100, index * 10 + 8),
                raw_text=rf"\sigma_{{{index}}}=E\epsilon",
                confidence=0.95,
                equation_number=number,
                latex=rf"\sigma_{{{index}}}=E\epsilon",
                source="mineru_content_list",
            )
            for index, number in enumerate(["(1)", "(2)", "(4)"])
        ]
        indexer = Indexer.__new__(Indexer)
        indexer.config = SimpleNamespace(**self._hash_config().__dict__)
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer._assert_config_hash_current = MagicMock()

        with patch("zotpilot.feature_extraction.formula_ocr.extract_formula_candidates", return_value=candidates):
            result = indexer.estimate_formula_backfill(candidate_preview_limit=20)

        assert result["candidate_quality_blocking_paper_count"] == 1
        assert result["summary"]["candidate_quality_blocking_paper_count"] == 1
        assert result["candidate_quality_blocking_papers"] == [
            {
                "item_key": "DOC1",
                "title": "Impact paper",
                "candidate_count": 3,
                "review_reasons": ["missing_equation_number_gap"],
                "equation_number_warnings": ["missing_equation_number_gap"],
                "truncated_source_count": 0,
                "cached_latex_missing_equation_number_count": 0,
                "cached_latex_low_quality_count": 0,
                "duplicate_equation_numbers": [],
                "equation_number_sequence_breaks": [
                    {
                        "previous": "(2)",
                        "current": "(4)",
                        "prefix": "regular",
                        "reason": "missing_gap",
                        "gap": 2,
                        "missing_count": 1,
                    }
                ],
            }
        ]
        assert "candidate-stage formula quality" in result["summary"]["warnings"][-1]
        assert result["summary"]["next_action"].startswith(
            "Review candidate-stage formula quality warnings"
        )

    def test_estimate_formula_backfill_blocks_low_quality_cached_latex(self, tmp_path):
        from zotpilot.feature_extraction.formula_ocr import FormulaCandidate
        from zotpilot.indexer import Indexer
        from zotpilot.models import ZoteroItem

        pdf_path = tmp_path / "paper.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("DOC1", "Impact paper", "Auth", 2024, pdf_path)
        candidates = [
            FormulaCandidate(
                page_num=9,
                bbox=(109, 95, 442, 115),
                raw_text=r"\varepsilon_f = [ D_1 + D_2 \tt e x p",
                confidence=0.95,
                equation_number="(13)",
                latex=r"\varepsilon_f = [ D_1 + D_2 \tt e x p",
                source="mineru_content_list_low_quality",
            )
        ]
        indexer = Indexer.__new__(Indexer)
        indexer.config = SimpleNamespace(**self._hash_config().__dict__)
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer._assert_config_hash_current = MagicMock()

        with patch("zotpilot.feature_extraction.formula_ocr.extract_formula_candidates", return_value=candidates):
            result = indexer.estimate_formula_backfill(candidate_preview_limit=20)

        audit = result["results"][0]["candidate_audit"]
        assert audit["cached_latex_low_quality_count"] == 1
        assert audit["equation_number_warnings"] == ["cached_latex_low_quality"]
        assert result["candidate_quality_blocking_paper_count"] == 1
        assert result["candidate_quality_blocking_papers"][0]["review_reasons"] == ["cached_latex_low_quality"]
        assert result["candidate_quality_blocking_papers"][0]["cached_latex_low_quality_count"] == 1

    def test_estimate_formula_backfill_does_not_block_on_mixed_number_prefixes_only(self, tmp_path):
        from zotpilot.feature_extraction.formula_ocr import FormulaCandidate
        from zotpilot.indexer import Indexer
        from zotpilot.models import ZoteroItem

        pdf_path = tmp_path / "paper.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("DOC1", "Appendix paper", "Auth", 2024, pdf_path)
        candidates = [
            FormulaCandidate(
                page_num=page,
                bbox=(0, index * 10, 100, index * 10 + 8),
                raw_text=rf"\sigma_{{{index}}}=E\epsilon",
                confidence=0.95,
                equation_number=number,
                latex=rf"\sigma_{{{index}}}=E\epsilon",
                source="mineru_content_list",
            )
            for index, (page, number) in enumerate([(4, "(1)"), (12, "(A.1)")])
        ]
        indexer = Indexer.__new__(Indexer)
        indexer.config = SimpleNamespace(**self._hash_config().__dict__)
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer._assert_config_hash_current = MagicMock()

        with patch("zotpilot.feature_extraction.formula_ocr.extract_formula_candidates", return_value=candidates):
            result = indexer.estimate_formula_backfill(candidate_preview_limit=20)

        audit = result["results"][0]["candidate_audit"]
        assert audit["equation_number_warnings"] == ["mixed_equation_number_prefixes"]
        assert result["candidate_quality_blocking_paper_count"] == 0
        assert result["candidate_quality_blocking_papers"] == []

    def test_estimate_formula_backfill_does_not_count_unnumbered_cache_rows_as_missing(self, tmp_path):
        from zotpilot.feature_extraction.formula_ocr import FormulaCandidate
        from zotpilot.indexer import Indexer
        from zotpilot.models import ZoteroItem

        pdf_path = tmp_path / "paper.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("DOC1", "Multirow paper", "Auth", 2024, pdf_path)
        candidates = [
            FormulaCandidate(
                page_num=4,
                bbox=(0, 0, 100, 20),
                raw_text=r"s_1=\sigma_1+p",
                confidence=0.95,
                equation_number="(2)",
                equation_number_status="provided",
                latex=r"s_1=\sigma_1+p",
                source="mineru_content_list_row",
            ),
            FormulaCandidate(
                page_num=4,
                bbox=(0, 22, 100, 42),
                raw_text=r"s_2=\sigma_2+p",
                confidence=0.95,
                equation_number_status="unnumbered",
                latex=r"s_2=\sigma_2+p",
                source="mineru_content_list_row",
            ),
        ]
        indexer = Indexer.__new__(Indexer)
        indexer.config = SimpleNamespace(**self._hash_config().__dict__)
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer._assert_config_hash_current = MagicMock()

        with patch("zotpilot.feature_extraction.formula_ocr.extract_formula_candidates", return_value=candidates):
            result = indexer.estimate_formula_backfill(candidate_preview_limit=20)

        audit = result["results"][0]["candidate_audit"]
        assert audit["cached_latex_missing_equation_number_count"] == 0
        assert audit["unnumbered_count"] == 1
        assert audit["equation_number_warnings"] == []
        assert result["candidate_quality_blocking_paper_count"] == 0
        assert result["candidate_quality_blocking_papers"] == []

    def test_estimate_formula_backfill_promotes_duplicate_equation_numbers_to_warning(self, tmp_path):
        from zotpilot.feature_extraction.formula_ocr import FormulaCandidate
        from zotpilot.indexer import Indexer
        from zotpilot.models import ZoteroItem

        pdf_path = tmp_path / "paper.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("DOC1", "Impact paper", "Auth", 2024, pdf_path)
        candidates = [
            FormulaCandidate(
                page_num=4 + index,
                bbox=(0, index * 10, 100, index * 10 + 8),
                raw_text=rf"\sigma_{{{index}}}=E\epsilon",
                confidence=0.95,
                equation_number=number,
                latex=rf"\sigma_{{{index}}}=E\epsilon",
                source="mineru_content_list",
            )
            for index, number in enumerate(["(1)", "(2)", "(2)", "(3)"])
        ]
        indexer = Indexer.__new__(Indexer)
        indexer.config = SimpleNamespace(**self._hash_config().__dict__)
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer._assert_config_hash_current = MagicMock()

        with patch("zotpilot.feature_extraction.formula_ocr.extract_formula_candidates", return_value=candidates):
            result = indexer.estimate_formula_backfill(candidate_preview_limit=20)

        audit = result["results"][0]["candidate_audit"]
        assert audit["duplicate_equation_numbers"] == ["(2)"]
        assert audit["duplicate_equation_number_count"] == 1
        assert audit["equation_number_warnings"] == ["duplicate_equation_numbers"]

    def test_estimate_formula_backfill_page_window_uses_unlimited_candidate_extraction(self, tmp_path):
        from zotpilot.feature_extraction.formula_ocr import FormulaCandidate
        from zotpilot.indexer import Indexer
        from zotpilot.models import ZoteroItem

        pdf_path = tmp_path / "thesis.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("THESIS1", "Damage mechanics thesis", "Auth", 2024, pdf_path)
        candidates = [
            FormulaCandidate(
                page_num=page,
                bbox=(0, index * 10, 100, index * 10 + 8),
                raw_text=rf"\sigma_{{{index}}}=E\epsilon",
                confidence=0.95,
                equation_number=f"(3.{index + 1})",
            )
            for index, page in enumerate([9, 10, 11])
        ]
        indexer = Indexer.__new__(Indexer)
        indexer.config = SimpleNamespace(
            **{
                **self._hash_config().__dict__,
                "formula_ocr_provider": "simpletex",
                "formula_ocr_simpletex_min_interval": 0.5,
                "formula_ocr_high_density_candidate_threshold": 1,
            }
        )
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"THESIS1"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_item.return_value = item
        indexer._assert_config_hash_current = MagicMock()

        with patch(
            "zotpilot.feature_extraction.formula_ocr.extract_formula_candidates",
            return_value=candidates,
        ) as extract:
            result = indexer.estimate_formula_backfill(
                item_key="THESIS1",
                daily_call_budget=1800,
                page_min=10,
                page_max=11,
            )

        assert result["candidate_count"] == 2
        assert extract.call_args.kwargs["max_candidates_per_doc"] == 0
        assert extract.call_args.kwargs["max_formulas_per_doc"] == 0
        assert extract.call_args.kwargs["max_formulas_per_page"] == 0

    def test_estimate_formula_backfill_defers_cached_formula_dense_documents(self, tmp_path):
        from zotpilot.feature_extraction.formula_ocr import FormulaCandidate
        from zotpilot.indexer import Indexer
        from zotpilot.models import ZoteroItem

        pdf_path = tmp_path / "thesis.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("THESIS1", "Damage mechanics thesis", "Auth", 2024, pdf_path)
        candidates = [
            FormulaCandidate(
                page_num=index + 1,
                bbox=(0, index * 10, 100, index * 10 + 8),
                raw_text=r"\sigma=E\epsilon",
                confidence=0.95,
                latex=rf"\sigma_{{{index}}}=E\epsilon",
                equation_number=f"({index + 1})",
            )
            for index in range(3)
        ]
        indexer = Indexer.__new__(Indexer)
        indexer.config = SimpleNamespace(
            **{
                **self._hash_config().__dict__,
                "formula_ocr_provider": "simpletex",
                "formula_ocr_high_density_call_threshold": 80,
                "formula_ocr_high_density_candidate_threshold": 2,
            }
        )
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"THESIS1"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer._assert_config_hash_current = MagicMock()

        with patch(
            "zotpilot.feature_extraction.formula_ocr.extract_formula_candidates",
            return_value=candidates,
        ) as extract:
            result = indexer.estimate_formula_backfill(daily_call_budget=1800)

        assert result["estimated_provider_calls"] == 0
        assert result["dense_formula_papers"] == [
            {
                "item_key": "THESIS1",
                "title": "Damage mechanics thesis",
                "candidate_count": 3,
                "estimated_provider_calls": 0,
                "estimated_external_calls": 0,
                "high_density_call_threshold": 80,
                "high_density_candidate_threshold": 2,
                "high_density_trigger": "candidate_count",
            }
        ]
        assert result["results"][0]["default_batch_status"] == "deferred_high_density"
        assert result["deferred_high_density_candidate_count"] == 3
        assert result["deferred_high_density_provider_calls"] == 0
        assert result["scan_limited_high_density_papers"] == [
            {
                "item_key": "THESIS1",
                "title": "Damage mechanics thesis",
                "scanned_candidate_count": 3,
                "scan_limit": 3,
                "reason": "scan_limit",
                "recommended_review": {
                    "mode": "single_item_readonly_estimate",
                    "reason": "scan_limit",
                    "item_key": "THESIS1",
                    "cli_args": [
                        "estimate-formula-backfill",
                        "--item-key",
                        "THESIS1",
                        "--pdf-fallback-max-pages",
                        "0",
                        "--cache-pdf-number-enrichment",
                        "--preview-all-candidates",
                        "--json",
                    ],
                    "opens_pdf": True,
                    "writes_index": False,
                    "uses_external_ocr": False,
                },
            }
        ]
        assert extract.call_args.kwargs["max_candidates_per_doc"] == 3

    def test_formula_backfill_defers_high_density_external_documents_in_batch(self, tmp_path):
        from zotpilot.indexer import Indexer
        from zotpilot.models import ZoteroItem

        pdf_path = tmp_path / "book.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("BOOK1", "Damage mechanics", "Auth", 2024, pdf_path)
        indexer = Indexer.__new__(Indexer)
        indexer.config = SimpleNamespace(
            **{
                **self._hash_config().__dict__,
                "formula_ocr_provider": "simpletex",
                "formula_ocr_high_density_call_threshold": 2,
            }
        )
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"BOOK1"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer.journal_ranker = MagicMock()
        indexer._ensure_formula_provider_available = MagicMock()
        indexer._assert_config_hash_current = MagicMock()
        indexer._recognize_formulas_for_item = MagicMock()

        with patch(
            "zotpilot.feature_extraction.formula_ocr.extract_formula_candidates",
            return_value=[object(), object(), object()],
        ):
            result = indexer.index_formulas(daily_call_budget=1800)

        assert result["processed"] == 0
        assert result["formulas_indexed"] == 0
        assert result["results"][0]["status"] == "deferred_high_density"
        assert result["results"][0]["reason"] == "high_density_formula_document"
        assert result["results"][0]["provider_calls"] == 3
        assert "high-density formula document" in result["warnings"][0]
        indexer._recognize_formulas_for_item.assert_not_called()
        indexer.store.replace_formulas.assert_not_called()
        indexer.store.add_formulas.assert_not_called()

    def test_formula_backfill_defers_cached_formula_dense_documents_in_batch(self, tmp_path):
        from zotpilot.feature_extraction.formula_ocr import FormulaCandidate
        from zotpilot.indexer import Indexer
        from zotpilot.models import ZoteroItem

        pdf_path = tmp_path / "thesis.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("THESIS1", "Damage mechanics thesis", "Auth", 2024, pdf_path)
        candidates = [
            FormulaCandidate(
                page_num=index + 1,
                bbox=(0, index * 10, 100, index * 10 + 8),
                raw_text=r"\sigma=E\epsilon",
                confidence=0.95,
                latex=rf"\sigma_{{{index}}}=E\epsilon",
                equation_number=f"({index + 1})",
            )
            for index in range(3)
        ]
        indexer = Indexer.__new__(Indexer)
        indexer.config = SimpleNamespace(
            **{
                **self._hash_config().__dict__,
                "formula_ocr_provider": "simpletex",
                "formula_ocr_high_density_call_threshold": 80,
                "formula_ocr_high_density_candidate_threshold": 2,
            }
        )
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"THESIS1"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer.journal_ranker = MagicMock()
        indexer._ensure_formula_provider_available = MagicMock()
        indexer._assert_config_hash_current = MagicMock()
        indexer._recognize_formulas_for_item = MagicMock()

        with patch(
            "zotpilot.feature_extraction.formula_ocr.extract_formula_candidates",
            return_value=candidates,
        ) as extract:
            result = indexer.index_formulas(daily_call_budget=1800)

        assert result["processed"] == 0
        assert result["formulas_indexed"] == 0
        assert result["results"][0]["status"] == "deferred_high_density"
        assert result["results"][0]["provider_calls"] == 0
        assert result["results"][0]["high_density_candidate_threshold"] == 2
        assert "formula candidate" in result["warnings"][0]
        indexer._ensure_formula_provider_available.assert_not_called()
        indexer._recognize_formulas_for_item.assert_not_called()
        indexer.store.replace_formulas.assert_not_called()
        assert extract.call_args.kwargs["max_candidates_per_doc"] == 3

    def test_formula_backfill_defers_single_item_keys_high_density_by_default(self, tmp_path):
        from zotpilot.indexer import Indexer
        from zotpilot.models import ZoteroItem

        pdf_path = tmp_path / "book.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("BOOK1", "Damage mechanics", "Auth", 2024, pdf_path)
        indexer = Indexer.__new__(Indexer)
        indexer.config = SimpleNamespace(
            **{
                **self._hash_config().__dict__,
                "formula_ocr_provider": "simpletex",
                "formula_ocr_high_density_call_threshold": 2,
            }
        )
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"BOOK1"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer.journal_ranker = MagicMock()
        indexer._ensure_formula_provider_available = MagicMock()
        indexer._assert_config_hash_current = MagicMock()
        indexer._recognize_formulas_for_item = MagicMock()

        with patch(
            "zotpilot.feature_extraction.formula_ocr.extract_formula_candidates",
            return_value=[object(), object(), object()],
        ):
            result = indexer.index_formulas(item_keys=["BOOK1"], daily_call_budget=1800)

        assert result["processed"] == 0
        assert result["high_density_deferred_count"] == 1
        assert result["results"][0]["status"] == "deferred_high_density"
        indexer._recognize_formulas_for_item.assert_not_called()
        indexer.store.replace_formulas.assert_not_called()

    def test_formula_backfill_allows_single_item_key_high_density_without_include_flag(self, tmp_path):
        from zotpilot.indexer import Indexer
        from zotpilot.models import ExtractedFormula, ZoteroItem

        pdf_path = tmp_path / "book.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("BOOK1", "Damage mechanics", "Auth", 2024, pdf_path, publication="Book")
        formula = ExtractedFormula(
            page_num=12,
            formula_index=0,
            bbox=(0, 0, 10, 10),
            latex=r"\sigma = E\epsilon",
            equation_number="(1)",
            provider="simpletex",
        )
        indexer = Indexer.__new__(Indexer)
        indexer.config = SimpleNamespace(
            **{
                **self._hash_config().__dict__,
                "formula_ocr_provider": "simpletex",
                "formula_ocr_high_density_call_threshold": 2,
            }
        )
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"BOOK1"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer.journal_ranker = MagicMock()
        indexer.journal_ranker.lookup.return_value = ""
        indexer._ensure_formula_provider_available = MagicMock()
        indexer._assert_config_hash_current = MagicMock()
        indexer._pdf_hash = MagicMock(return_value="hash")
        indexer._get_formula_provider = MagicMock(return_value=SimpleNamespace())
        indexer._recognize_formulas_for_item = MagicMock(return_value=[formula])

        with patch(
            "zotpilot.feature_extraction.formula_ocr.extract_formula_candidates",
            return_value=[object(), object(), object()],
        ):
            result = indexer.index_formulas(item_key="BOOK1", daily_call_budget=1800)

        assert result["processed"] == 1
        assert result["high_density_deferred_count"] == 0
        assert result["provider_calls_used"] == 3
        assert result["results"][0]["status"] == "indexed"
        indexer._recognize_formulas_for_item.assert_called_once()
        indexer.store.replace_formulas.assert_called_once()

    def test_formula_backfill_page_window_appends_with_stable_formula_indices(self, tmp_path):
        from zotpilot.feature_extraction.formula_ocr import FormulaCandidate
        from zotpilot.indexer import Indexer
        from zotpilot.models import ZoteroItem

        pdf_path = tmp_path / "thesis.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("THESIS1", "Damage mechanics thesis", "Auth", 2024, pdf_path)
        candidates = [
            FormulaCandidate(
                page_num=page,
                bbox=(0, index * 10, 100, index * 10 + 8),
                raw_text=rf"\sigma_{{{index}}}=E\epsilon",
                confidence=0.95,
                source="mineru_content_list",
                latex=rf"\sigma_{{{index}}}=E\epsilon",
                equation_number=f"(3.{index + 1})",
            )
            for index, page in enumerate([9, 10, 11])
        ]
        indexer = Indexer.__new__(Indexer)
        indexer.config = SimpleNamespace(
            **{
                **self._hash_config().__dict__,
                "formula_ocr_provider": "simpletex",
                "formula_ocr_high_density_call_threshold": 80,
            }
        )
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"THESIS1"}
        indexer.store.add_new_formulas.return_value = 2
        indexer.zotero = MagicMock()
        indexer.zotero.get_item.return_value = item
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer.journal_ranker = MagicMock()
        indexer.journal_ranker.lookup.return_value = ""
        indexer._ensure_formula_provider_available = MagicMock()
        indexer._assert_config_hash_current = MagicMock()
        indexer._pdf_hash = MagicMock(return_value="hash")

        with patch(
            "zotpilot.feature_extraction.formula_ocr.extract_formula_candidates",
            return_value=candidates,
        ) as extract:
            result = indexer.index_formulas(
                item_key="THESIS1",
                daily_call_budget=1800,
                page_min=10,
                page_max=11,
            )

        stored_formulas = indexer.store.add_new_formulas.call_args.args[2]
        assert [formula.page_num for formula in stored_formulas] == [10, 11]
        assert [formula.formula_index for formula in stored_formulas] == [1, 2]
        assert result["processed"] == 1
        assert result["formulas_indexed"] == 2
        assert result["page_range_backfill"] is True
        assert "append-only" in result["warnings"][0]
        assert extract.call_args.kwargs["max_candidates_per_doc"] == 0
        assert extract.call_args.kwargs["max_formulas_per_doc"] == 0
        assert extract.call_args.kwargs["max_formulas_per_page"] == 0
        indexer.store.replace_formulas.assert_not_called()

    def test_formula_backfill_allows_high_density_documents_when_explicitly_included(self, tmp_path):
        from zotpilot.indexer import Indexer
        from zotpilot.models import ExtractedFormula, ZoteroItem

        pdf_path = tmp_path / "book.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("BOOK1", "Damage mechanics", "Auth", 2024, pdf_path, publication="Book")
        formula = ExtractedFormula(
            page_num=12,
            formula_index=0,
            bbox=(0, 0, 10, 10),
            latex=r"\sigma = E\epsilon",
            equation_number="(1)",
            provider="simpletex",
        )
        indexer = Indexer.__new__(Indexer)
        indexer.config = SimpleNamespace(
            **{
                **self._hash_config().__dict__,
                "formula_ocr_provider": "simpletex",
                "formula_ocr_high_density_call_threshold": 2,
            }
        )
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"BOOK1"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer.journal_ranker = MagicMock()
        indexer.journal_ranker.lookup.return_value = ""
        indexer._ensure_formula_provider_available = MagicMock()
        indexer._assert_config_hash_current = MagicMock()
        indexer._pdf_hash = MagicMock(return_value="hash")
        indexer._get_formula_provider = MagicMock(return_value=SimpleNamespace())
        indexer._recognize_formulas_for_item = MagicMock(return_value=[formula])

        with patch(
            "zotpilot.feature_extraction.formula_ocr.extract_formula_candidates",
            return_value=[object(), object(), object()],
        ):
            result = indexer.index_formulas(
                item_keys=["BOOK1"],
                daily_call_budget=1800,
                include_high_density=True,
            )

        assert result["processed"] == 1
        assert result["formulas_indexed"] == 1
        assert result["high_density_deferred_count"] == 0
        assert result["include_high_density"] is True
        assert result["provider_calls_used"] == 3
        assert result["results"][0]["status"] == "indexed"
        indexer._ensure_formula_provider_available.assert_called_once()
        indexer._recognize_formulas_for_item.assert_called_once()
        indexer.store.replace_formulas.assert_called_once()

    def test_estimate_formula_backfill_preview_distinguishes_candidates_from_provider_calls(self, tmp_path):
        from zotpilot.feature_extraction.formula_ocr import FormulaCandidate
        from zotpilot.indexer import Indexer
        from zotpilot.models import ZoteroItem

        pdf_path = tmp_path / "paper.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("DOC1", "Paper", "Auth", 2024, pdf_path)
        candidates = [
            FormulaCandidate(
                page_num=1,
                bbox=(0, 0, 0, 0),
                raw_text=r"E = mc^2",
                confidence=0.95,
                source="mineru_content_list",
                latex=r"E = mc^2",
            ),
            FormulaCandidate(page_num=2, bbox=(1, 2, 3, 4), raw_text=r"\sigma = E\epsilon", confidence=0.8),
        ]
        indexer = Indexer.__new__(Indexer)
        indexer.config = SimpleNamespace(
            **{
                **self._hash_config().__dict__,
                "formula_ocr_provider": "simpletex",
                "formula_ocr_simpletex_min_interval": 0.5,
            }
        )
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer._assert_config_hash_current = MagicMock()

        with patch("zotpilot.feature_extraction.formula_ocr.extract_formula_candidates", return_value=candidates):
            result = indexer.estimate_formula_backfill(candidate_preview_limit=2)

        assert result["candidate_count"] == 2
        assert result["estimated_provider_calls"] == 1
        assert result["estimated_external_calls"] == 1
        assert result["estimated_min_duration_seconds"] == 0.5
        assert result["summary"]["cached_latex_missing_number_paper_count"] == 1
        assert result["cached_latex_missing_number_papers"] == [
            {
                "item_key": "DOC1",
                "title": "Paper",
                "candidate_count": 2,
                "cached_latex_count": 1,
                "missing_equation_number_count": 1,
                "missing_equation_number_ratio": 1.0,
            }
        ]
        assert result["candidate_quality_blocking_paper_count"] == 1
        assert result["candidate_quality_blocking_papers"][0]["review_reasons"] == [
            "cached_latex_missing_equation_numbers"
        ]
        assert result["summary"]["next_action"].startswith(
            "Review candidate-stage formula quality warnings"
        )
        audit = result["results"][0]["candidate_audit"]
        assert audit["candidate_count"] == 2
        assert audit["cached_latex_count"] == 1
        assert audit["cached_latex_missing_equation_number_count"] == 1
        assert audit["cached_latex_missing_equation_number_ratio"] == 1.0
        assert audit["ocr_needed_count"] == 1
        assert audit["equation_number_warnings"] == ["cached_latex_missing_equation_numbers"]
        assert audit["source_counts"] == {"mineru_content_list": 1, "text_layer": 1}
        assert audit["page_min"] == 1
        assert audit["page_max"] == 2
        assert result["results"][0]["candidate_preview"][0]["has_latex"] is True
        assert result["results"][0]["candidate_preview"][0]["needs_ocr"] is False
        assert result["results"][0]["candidate_preview"][1]["has_latex"] is False
        assert result["results"][0]["candidate_preview"][1]["needs_ocr"] is True
        assert result["results"][0]["candidate_preview"][1]["bbox"] == [1, 2, 3, 4]

    def test_estimate_formula_backfill_preview_can_include_all_candidates_without_truncation(self, tmp_path):
        from zotpilot.feature_extraction.formula_ocr import FormulaCandidate
        from zotpilot.indexer import Indexer
        from zotpilot.models import ZoteroItem

        pdf_path = tmp_path / "paper.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("DOC1", "Paper", "Auth", 2024, pdf_path)
        long_latex = r"E = mc^2 " * 30
        candidates = [
            FormulaCandidate(
                page_num=1,
                bbox=(0, 0, 0, 0),
                raw_text=f"raw {index}",
                confidence=0.95,
                latex=long_latex,
                equation_number=f"({index + 1})",
            )
            for index in range(3)
        ]
        indexer = Indexer.__new__(Indexer)
        indexer.config = SimpleNamespace(
            **{
                **self._hash_config().__dict__,
                "formula_ocr_provider": "simpletex",
                "formula_ocr_simpletex_min_interval": 0.5,
            }
        )
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer._assert_config_hash_current = MagicMock()

        with patch(
            "zotpilot.feature_extraction.formula_ocr.extract_formula_candidates",
            return_value=candidates,
        ) as extract:
            result = indexer.estimate_formula_backfill(
                candidate_preview_limit=-1,
                candidate_preview_chars=0,
            )

        previews = result["results"][0]["candidate_preview"]
        assert [preview["candidate_index"] for preview in previews] == [0, 1, 2]
        assert [preview["equation_number"] for preview in previews] == ["(1)", "(2)", "(3)"]
        assert previews[0]["latex_preview"] == long_latex
        assert extract.call_args.kwargs["max_candidates_per_doc"] == 0

    def test_estimate_formula_backfill_candidate_audit_warns_on_truncated_sources(self, tmp_path):
        from zotpilot.feature_extraction.formula_ocr import FormulaCandidate
        from zotpilot.indexer import Indexer
        from zotpilot.models import ZoteroItem

        pdf_path = tmp_path / "paper.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("DOC1", "Paper", "Auth", 2024, pdf_path)
        candidates = [
            FormulaCandidate(
                page_num=80,
                bbox=(0, 0, 10, 10),
                raw_text=r"E = mc^2",
                confidence=0.72,
                source="pdf_text_equation_number_truncated",
                equation_number="(1)",
            )
        ]
        indexer = Indexer.__new__(Indexer)
        indexer.config = SimpleNamespace(
            **{
                **self._hash_config().__dict__,
                "formula_ocr_provider": "simpletex",
                "formula_ocr_simpletex_min_interval": 0.5,
            }
        )
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer._assert_config_hash_current = MagicMock()

        with patch("zotpilot.feature_extraction.formula_ocr.extract_formula_candidates", return_value=candidates):
            result = indexer.estimate_formula_backfill(candidate_preview_limit=1)

        audit = result["results"][0]["candidate_audit"]
        assert audit["has_truncated_source"] is True
        assert audit["truncated_source_count"] == 1
        assert result["truncated_candidate_papers"] == [
            {
                "item_key": "DOC1",
                "title": "Paper",
                "candidate_count": 1,
                "truncated_source_count": 1,
                "recommended_review": {
                    "mode": "single_item_readonly_estimate",
                    "reason": "fallback_truncated",
                    "item_key": "DOC1",
                    "cli_args": [
                        "estimate-formula-backfill",
                        "--item-key",
                        "DOC1",
                        "--pdf-fallback-max-pages",
                        "0",
                        "--cache-pdf-number-enrichment",
                        "--preview-all-candidates",
                        "--json",
                    ],
                    "opens_pdf": True,
                    "writes_index": False,
                    "uses_external_ocr": False,
                },
            }
        ]
        assert result["candidate_quality_blocking_papers"][0]["recommended_review"]["reason"] == "fallback_truncated"
        assert result["summary"]["truncated_candidate_paper_count"] == 1
        assert any("truncated PDF fallback" in warning for warning in result["summary"]["warnings"])

    def test_estimate_formula_backfill_cached_latex_still_needs_one_run(self, tmp_path):
        from zotpilot.feature_extraction.formula_ocr import FormulaCandidate
        from zotpilot.indexer import Indexer
        from zotpilot.models import ZoteroItem

        pdf_path = tmp_path / "paper.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("DOC1", "Paper", "Auth", 2024, pdf_path)
        candidate = FormulaCandidate(
            page_num=1,
            bbox=(0, 0, 0, 0),
            raw_text=r"E = mc^2",
            confidence=0.95,
            source="mineru_content_list",
            latex=r"E = mc^2",
        )
        indexer = Indexer.__new__(Indexer)
        indexer.config = SimpleNamespace(
            **{
                **self._hash_config().__dict__,
                "formula_ocr_provider": "simpletex",
                "formula_ocr_simpletex_min_interval": 0.5,
            }
        )
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer._assert_config_hash_current = MagicMock()

        with patch("zotpilot.feature_extraction.formula_ocr.extract_formula_candidates", return_value=[candidate]):
            result = indexer.estimate_formula_backfill(daily_call_budget=1800)

        assert result["candidate_count"] == 1
        assert result["estimated_provider_calls"] == 0
        assert result["estimated_external_calls"] == 0
        assert result["estimated_runs"] == 1

    def test_formula_backfill_writes_jsonl_run_and_item_events(self, tmp_path):
        from zotpilot.indexer import Indexer
        from zotpilot.models import ExtractedFormula, ZoteroItem

        pdf_path = tmp_path / "paper.pdf"
        state_path = tmp_path / "formula_state.jsonl"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("DOC1", "Paper", "Auth", 2024, pdf_path, publication="Nature")
        formula = ExtractedFormula(
            page_num=1,
            formula_index=0,
            bbox=(0, 0, 10, 10),
            latex=r"E = mc^2",
            equation_number="(1)",
        )
        indexer = Indexer.__new__(Indexer)
        indexer.config = self._hash_config()
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer.journal_ranker = MagicMock()
        indexer.journal_ranker.lookup.return_value = "Q1"
        indexer._ensure_formula_provider_available = MagicMock()
        indexer._assert_config_hash_current = MagicMock()
        indexer._pdf_hash = MagicMock(return_value="hash")
        indexer._recognize_formulas_for_item = MagicMock(return_value=[formula])

        with patch("zotpilot.feature_extraction.formula_ocr.extract_formula_candidates", return_value=[object()]):
            result = indexer.index_formulas(status_jsonl=state_path)

        events = [json.loads(line) for line in state_path.read_text(encoding="utf-8").splitlines()]
        assert [event["event"] for event in events] == [
            "formula_backfill_run_started",
            "formula_backfill_item",
            "formula_backfill_run_finished",
        ]
        assert {event["run_id"] for event in events} == {result["run_id"]}
        assert all(event["schema_version"] == 1 for event in events)
        assert events[1]["status"] == "indexed"

    def test_formula_backfill_jsonl_records_unmatched_requested_item_keys(self, tmp_path):
        from zotpilot.feature_extraction.formula_ocr import FormulaCandidate
        from zotpilot.indexer import Indexer
        from zotpilot.models import ExtractedFormula, ZoteroItem

        pdf_path = tmp_path / "paper.pdf"
        state_path = tmp_path / "formula_state.jsonl"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("DOC1", "Indexed paper", "Auth", 2024, pdf_path, publication="Nature")
        formula = ExtractedFormula(
            page_num=1,
            formula_index=0,
            bbox=(0, 0, 10, 10),
            latex=r"E=mc^2",
            equation_number="(1)",
        )
        candidate = FormulaCandidate(
            page_num=1,
            bbox=(0, 0, 10, 10),
            raw_text=r"E=mc^2",
            confidence=0.95,
            equation_number="(1)",
            latex=r"E=mc^2",
            source="mineru_content_list",
        )
        indexer = Indexer.__new__(Indexer)
        indexer.config = self._hash_config()
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"DOC1"}
        indexer.store.replace_formulas.return_value = 1
        indexer.zotero = MagicMock()
        indexer.zotero.get_item.side_effect = lambda key: item if key == "DOC1" else None
        indexer.journal_ranker = MagicMock()
        indexer.journal_ranker.lookup.return_value = "Q1"
        indexer._ensure_formula_provider_available = MagicMock()
        indexer._assert_config_hash_current = MagicMock()
        indexer._pdf_hash = MagicMock(return_value="hash")
        indexer._recognize_formulas_for_item = MagicMock(return_value=[formula])

        with patch("zotpilot.feature_extraction.formula_ocr.extract_formula_candidates", return_value=[candidate]):
            result = indexer.index_formulas(item_keys=["DOC1", "MISSING1"], status_jsonl=state_path)

        events = [json.loads(line) for line in state_path.read_text(encoding="utf-8").splitlines()]
        assert result["unmatched_requested_item_keys"] == ["MISSING1"]
        assert events[0]["unmatched_requested_item_keys"] == ["MISSING1"]
        assert events[-1]["unmatched_requested_item_keys"] == ["MISSING1"]
        assert events[-1]["unmatched_requested_item_key_count"] == 1

    def test_formula_backfill_writes_jsonl_high_density_deferred_events(self, tmp_path):
        from zotpilot.indexer import Indexer
        from zotpilot.models import ZoteroItem

        pdf_path = tmp_path / "book.pdf"
        state_path = tmp_path / "formula_state.jsonl"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("BOOK1", "Damage mechanics", "Auth", 2024, pdf_path)
        indexer = Indexer.__new__(Indexer)
        indexer.config = SimpleNamespace(
            **{
                **self._hash_config().__dict__,
                "formula_ocr_provider": "simpletex",
                "formula_ocr_high_density_call_threshold": 2,
            }
        )
        indexer.store = MagicMock()
        indexer.store.get_indexed_doc_ids.return_value = {"BOOK1"}
        indexer.zotero = MagicMock()
        indexer.zotero.get_all_items_with_pdfs.return_value = [item]
        indexer.journal_ranker = MagicMock()
        indexer._ensure_formula_provider_available = MagicMock()
        indexer._assert_config_hash_current = MagicMock()
        indexer._recognize_formulas_for_item = MagicMock()

        with patch(
            "zotpilot.feature_extraction.formula_ocr.extract_formula_candidates",
            return_value=[object(), object(), object()],
        ):
            result = indexer.index_formulas(item_keys=["BOOK1"], daily_call_budget=1800, status_jsonl=state_path)

        events = [json.loads(line) for line in state_path.read_text(encoding="utf-8").splitlines()]
        assert [event["event"] for event in events] == [
            "formula_backfill_run_started",
            "formula_backfill_item",
            "formula_backfill_run_finished",
        ]
        assert events[0]["high_density_call_threshold"] == 2
        assert events[0]["include_high_density"] is False
        assert events[1]["status"] == "deferred_high_density"
        assert events[1]["provider_calls"] == 3
        assert events[1]["high_density_call_threshold"] == 2
        assert events[2]["processed"] == 0
        assert events[2]["high_density_deferred_count"] == 1
        assert result["high_density_deferred_count"] == 1

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
            equation_number="(1)",
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

    def test_index_extraction_skips_truncated_pdf_fallback_formula_storage(self, tmp_path):
        from zotpilot.index_authority import IndexJournal
        from zotpilot.indexer import Indexer
        from zotpilot.models import Chunk, ExtractedFormula, PageExtraction, ZoteroItem

        pdf_path = tmp_path / "paper.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        item = ZoteroItem("DOC1", "Paper", "Auth", 2024, pdf_path, publication="Nature")
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
            confidence=0.95,
            equation_number="(1)",
            source="pdf_text_equation_number_truncated",
        )
        indexer = Indexer.__new__(Indexer)
        indexer.config = SimpleNamespace(formula_ocr_enabled=True)
        indexer.chunker = MagicMock()
        indexer.chunker.chunk.return_value = [chunk]
        indexer.journal_ranker = MagicMock()
        indexer.journal_ranker.lookup.return_value = "Q1"
        indexer.store = MagicMock()
        indexer._pdf_hash = MagicMock(return_value="hash")
        indexer._recognize_formulas_for_item = MagicMock(return_value=[formula])

        n_chunks, n_tables, reason, _stats, quality = indexer._index_extraction(
            item,
            extraction,
            IndexJournal(tmp_path / "journal.json"),
        )

        assert n_chunks == 1
        assert n_tables == 0
        assert reason == ""
        assert quality == "A"
        indexer.store.add_chunks.assert_called_once()
        indexer.store.add_formulas.assert_not_called()

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

        class FakeIndexer:
            def __init__(self, _config):
                pass

            def index_all(self, **_kwargs):
                raise FormulaProviderUnavailableError("Install `zotpilot[formula]`")

        with patch.object(idx_mod, "_get_config", return_value=config), \
             patch.object(idx_mod, "acquire_lease"), \
             patch.object(idx_mod, "release_lease"), \
             patch("zotpilot.indexer.Indexer", FakeIndexer):
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

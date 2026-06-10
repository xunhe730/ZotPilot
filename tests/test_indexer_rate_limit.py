"""Integration tests for issue #15 — fail-fast 429 abort in the Phase-3 loop.

Drives Indexer.index_all with mocked Zotero/store/embedder and a controllable
_index_extraction to pin: tail append + counts, the mixed-failure counts formula,
exit-code behavior, the provider-agnostic backstop, the mark_committed ordering
invariant, the table/figure-path 429 propagation (0.5b), the journal invariant,
D1 reconciliation no-delete, D2 ToolError non-mapping, and M1 survival.
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _make_config():
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
    return config


def _make_item(key, title="Paper", has_pdf=True):
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


def _make_indexer(items, indexed_ids=None):
    from zotpilot.indexer import Indexer
    config = _make_config()
    with patch("zotpilot.indexer.ZoteroClient"), \
         patch("zotpilot.indexer.create_embedder"), \
         patch("zotpilot.indexer.VectorStore"), \
         patch("zotpilot.indexer.JournalRanker"):
        indexer = Indexer(config)
    indexer.zotero.get_all_items_with_pdfs.return_value = items
    indexer.store.get_indexed_doc_ids = MagicMock(return_value=set(indexed_ids or []))
    indexer._load_empty_docs = MagicMock(return_value={})
    indexer._save_empty_docs = MagicMock()
    indexer._pdf_hash = MagicMock(return_value="hash")
    indexer._config_hash_path = MagicMock()
    indexer._config_hash_path.exists.return_value = False
    indexer._config_hash_path.write_text = MagicMock()
    indexer._library_unreachable = MagicMock(return_value=False)
    indexer._sleep = MagicMock()  # neutralize rate-limit retry waits in tests
    return indexer


def _run(indexer, index_extraction_side_effect, journal=None):
    """Run index_all with extract_document stubbed and _index_extraction driven."""
    extraction = MagicMock()
    extraction.pages = [MagicMock()]
    extraction.stats = {"total_pages": 1, "text_pages": 1, "ocr_pages": 0, "empty_pages": 0}
    extraction.quality_grade = "A"
    extraction.pending_vision = None
    with patch("zotpilot.indexer.extract_document", return_value=extraction), \
         patch.object(indexer, "_index_extraction", side_effect=index_extraction_side_effect):
        return indexer.index_all(batch_size=None, journal=journal)


def _success(item, extraction, journal):
    return (5, 0, "", {}, "A")


class TestTailAppendAndCounts:
    def test_rate_limit_on_doc_n_appends_untried_tail(self):
        """关键1/AC2/AC3: 429 on doc 3 of 5 → docs 3..5 all 'failed', tail carries
        the AbortNotAttempted reason, counts agree."""
        from zotpilot.embeddings.base import RateLimitError
        items = [_make_item(f"K{i}") for i in range(1, 6)]
        indexer = _make_indexer(items)

        def se(item, extraction, journal):
            if item.item_key == "K3":
                raise RateLimitError("quota", provider="gemini", retry_after=30.0)
            return _success(item, extraction, journal)

        result = _run(indexer, se)
        statuses = {r.item_key: r.status for r in result["results"]}
        assert statuses == {"K1": "indexed", "K2": "indexed", "K3": "failed",
                            "K4": "failed", "K5": "failed"}
        tail = {r.item_key: r.reason for r in result["results"]
                if r.reason and "AbortNotAttempted" in r.reason}
        assert set(tail) == {"K4", "K5"}  # only the never-attempted tail
        assert result["indexed"] == 2
        assert result["failed"] == 3
        assert result["rate_limited_abort"] is True
        assert result["systemic_abort"] is False
        assert result["not_indexed_due_to_abort"] == 3  # docs 3,4,5

    def test_counts_formula_excludes_unrelated_prior_failures(self):
        """0.6/AC3: doc 2 fails for an unrelated reason, doc 4 hits a 429 →
        not_indexed_due_to_abort counts only docs 4-5, not doc 2."""
        from zotpilot.embeddings.base import EmbeddingError, RateLimitError
        items = [_make_item(f"K{i}") for i in range(1, 6)]
        indexer = _make_indexer(items)

        def se(item, extraction, journal):
            if item.item_key == "K2":
                raise EmbeddingError("corrupt PDF")  # unrelated, NOT quota
            if item.item_key == "K4":
                raise RateLimitError("quota", provider="gemini")
            return _success(item, extraction, journal)

        result = _run(indexer, se)
        assert result["indexed"] == 2          # K1, K3
        assert result["failed"] == 3           # K2 (unrelated) + K4 (trigger) + K5 (tail)
        assert result["not_indexed_due_to_abort"] == 2  # K4, K5 ONLY
        assert result["rate_limited_abort"] is True


class TestExitCode:
    def test_fully_exhausted_run_exits_nonzero(self):
        """AC4: indexed == 0 and failed > 0 → cli.py:396 expression yields 1."""
        from zotpilot.embeddings.base import RateLimitError
        items = [_make_item(f"K{i}") for i in range(1, 4)]
        indexer = _make_indexer(items)

        def se(item, extraction, journal):
            raise RateLimitError("quota", provider="gemini")

        result = _run(indexer, se)
        assert result["indexed"] == 0
        assert result["failed"] == 3
        exit_code = 1 if result["failed"] > 0 and result["indexed"] == 0 else 0
        assert exit_code == 1


class TestBackstop:
    def test_three_consecutive_same_signature_triggers_systemic(self):
        """AC9/关键3: 3 consecutive same-signature untyped failures abort with
        systemic_abort, NOT rate_limited_abort."""
        from zotpilot.embeddings.base import EmbeddingError
        items = [_make_item(f"K{i}") for i in range(1, 4)]
        indexer = _make_indexer(items)

        def se(item, extraction, journal):
            raise EmbeddingError("Batch 1/9 failed after 3 attempts (32 texts, 5000 chars)")

        result = _run(indexer, se)
        assert result["systemic_abort"] is True
        assert result["rate_limited_abort"] is False
        # 3 docs, abort at doc 3 → trigger doc only, no untried tail beyond it.
        assert result["not_indexed_due_to_abort"] == 1
        assert result["failed"] == 3

    def test_non_consecutive_failures_do_not_abort(self):
        """Control: 2 same + 1 different + 2 same never reaches 3-in-a-row."""
        from zotpilot.embeddings.base import EmbeddingError
        items = [_make_item(f"K{i}") for i in range(1, 6)]
        indexer = _make_indexer(items)
        same = "Batch 1/9 failed after 3 attempts (32 texts, 5000 chars)"
        diff = "totally different corrupt-pdf error"

        def se(item, extraction, journal):
            raise EmbeddingError(same if item.item_key in {"K1", "K2", "K4", "K5"} else diff)

        result = _run(indexer, se)
        assert result["systemic_abort"] is False
        assert result["rate_limited_abort"] is False
        assert result["failed"] == 5  # all failed but run completed (no abort)

    def test_backstop_is_not_narrowed_to_embedding_error(self):
        """Second control: 3 consecutive non-embedding (Chroma write) errors also
        trip the broad backstop."""
        items = [_make_item(f"K{i}") for i in range(1, 4)]
        indexer = _make_indexer(items)

        def se(item, extraction, journal):
            raise RuntimeError("Chroma collection.add failed: disk error")

        result = _run(indexer, se)
        assert result["systemic_abort"] is True
        assert result["rate_limited_abort"] is False


class TestOrderingInvariantMarkCommitted:
    def test_mark_committed_not_called_when_add_chunks_429s(self):
        """R1: a 429 during text-chunk embedding (inside add_chunks) raises BEFORE
        mark_committed, so the doc is not committed and is re-attempted next run."""
        from zotpilot.embeddings.base import RateLimitError
        from zotpilot.index_authority import IndexJournal, is_doc_committed

        item = _make_item("K1")
        indexer = _make_indexer([item])
        indexer.store.add_chunks = MagicMock(
            side_effect=RateLimitError("quota", provider="gemini"))
        indexer.chunker = MagicMock()
        indexer.chunker.chunk.return_value = [MagicMock()]
        indexer.journal_ranker = MagicMock()
        indexer.journal_ranker.lookup.return_value = "Q1"

        page = MagicMock()
        page.markdown = "hello world body text"
        extraction = MagicMock()
        extraction.pages = [page]
        extraction.full_markdown = "hello world body text"
        extraction.sections = []
        extraction.stats = {"text_pages": 1, "ocr_pages": 0, "empty_pages": 0}
        extraction.quality_grade = "A"

        journal = IndexJournal(Path("/tmp/zp-test-journal-ordering.json"))
        with pytest.raises(RateLimitError):
            indexer._index_extraction(item, extraction, journal)
        assert "K1" in journal.in_progress          # mark_in_progress ran
        assert not is_doc_committed(journal, "K1")   # mark_committed NOT reached


class TestTableFigure429:
    """0.5b/AC14: a 429 from add_tables/add_figures must PROPAGATE (not be
    swallowed into a record_table_failure warning)."""

    def _extraction_with_tables(self):
        page = MagicMock()
        page.markdown = "hello world body text"
        extraction = MagicMock()
        extraction.pages = [page]
        extraction.full_markdown = "hello world body text"
        extraction.sections = []
        extraction.stats = {"text_pages": 1, "ocr_pages": 0, "empty_pages": 0}
        extraction.quality_grade = "A"
        table = MagicMock()
        table.artifact_type = None
        table.caption = "Results overview"  # no "Table N" → skips get_reference_context
        extraction.tables = [table]
        extraction.figures = []
        return extraction

    def _indexer_with_committed_text(self, item):
        indexer = _make_indexer([item])
        indexer.store.add_chunks = MagicMock()  # text commit succeeds
        indexer.chunker = MagicMock()
        indexer.chunker.chunk.return_value = [MagicMock()]
        indexer.journal_ranker = MagicMock()
        indexer.journal_ranker.lookup.return_value = "Q1"
        return indexer

    def test_table_429_propagates(self):
        from zotpilot.embeddings.base import RateLimitError
        from zotpilot.index_authority import IndexJournal, is_doc_committed

        item = _make_item("K1")
        indexer = self._indexer_with_committed_text(item)
        indexer.store.add_tables = MagicMock(
            side_effect=RateLimitError("quota", provider="gemini"))

        journal = IndexJournal(Path("/tmp/zp-test-journal-table.json"))
        with patch("zotpilot.pdf.reference_matcher.match_references", return_value={}), \
             patch("zotpilot.pdf.reference_matcher.get_reference_context", return_value=""):
            with pytest.raises(RateLimitError):
                indexer._index_extraction(item, self._extraction_with_tables(), journal)
        # text chunks were committed before the table 429 (关键2)
        assert is_doc_committed(journal, "K1")

    def test_generic_table_failure_still_swallowed(self):
        """Control: a non-RateLimitError table failure is still swallowed (run
        continues), proving 0.5b's guard is type-scoped."""
        from zotpilot.index_authority import IndexJournal

        item = _make_item("K1")
        indexer = self._indexer_with_committed_text(item)
        indexer.store.add_tables = MagicMock(side_effect=RuntimeError("transient table error"))

        journal = IndexJournal(Path("/tmp/zp-test-journal-table2.json"))
        with patch("zotpilot.pdf.reference_matcher.match_references", return_value={}), \
             patch("zotpilot.pdf.reference_matcher.get_reference_context", return_value=""):
            # Should NOT raise — swallowed into record_table_failure.
            n_chunks, n_tables, reason, stats, quality = indexer._index_extraction(
                item, self._extraction_with_tables(), journal)
        assert n_chunks == 1
        assert n_tables == 0  # table failed but was swallowed


class TestJournalInvariant:
    def test_abort_does_not_journal_untried_tail(self):
        """AC7: after a mid-run abort, only the currently-failing doc is in
        journal.in_progress; untried tail papers are never journaled."""
        from zotpilot.embeddings.base import RateLimitError
        from zotpilot.index_authority import IndexJournal, mark_committed, mark_in_progress

        items = [_make_item(f"K{i}") for i in range(1, 6)]
        indexer = _make_indexer(items)
        journal = IndexJournal(Path("/tmp/zp-test-journal-invariant.json"))

        def se(item, extraction, jnl):
            # Mimic _index_extraction's journal writes.
            mark_in_progress(jnl, item.item_key)
            if item.item_key == "K3":
                raise RateLimitError("quota", provider="gemini")
            mark_committed(jnl, item.item_key)
            return _success(item, extraction, jnl)

        result = _run(indexer, se, journal=journal)
        assert result["rate_limited_abort"] is True
        # Only K3 (the failing doc) remains in_progress; K4/K5 never journaled.
        assert set(journal.in_progress) == {"K3"}
        assert "K4" not in journal.in_progress and "K4" not in journal.committed
        assert "K5" not in journal.in_progress and "K5" not in journal.committed


class TestD1Reconciliation:
    def test_abort_issues_zero_deletes_and_full_library_read(self):
        """AC12: on a 429 abort, end-of-run reconcile deletes nothing and the full
        library is re-enumerated (final_current_doc_ids integrity)."""
        from zotpilot.embeddings.base import RateLimitError
        items = [_make_item(f"K{i}") for i in range(1, 4)]
        indexer = _make_indexer(items)
        indexer.store.delete_document = MagicMock()

        def se(item, extraction, journal):
            if item.item_key == "K2":
                raise RateLimitError("quota", provider="gemini")
            return _success(item, extraction, journal)

        result = _run(indexer, se)
        assert result["rate_limited_abort"] is True
        # (a) zero deletes across the whole run (phase-agnostic, robust assertion)
        indexer.store.delete_document.assert_not_called()
        # (b) library was re-enumerated at end-of-run (startup + end = 2 calls)
        assert indexer.zotero.get_all_items_with_pdfs.call_count >= 2


class TestD2ToolErrorBoundary:
    def test_quota_abort_returns_dict_not_toolerror_with_partial_indexed(self):
        """AC13/AC6: a quota-aborted run returns a normal dict (NOT ToolError);
        indexed equals the exact pre-abort success count; the lease is released."""
        from zotpilot.tools import indexing as idx_mod

        # index_all returns a quota-abort dict with one success preserved.
        fake_result = {
            "results": [],
            "indexed": 1,
            "failed": 2,
            "empty": 0,
            "skipped": 0,
            "already_indexed": 0,
            "rate_limited_abort": True,
            "systemic_abort": False,
            "not_indexed_due_to_abort": 2,
            "has_more": False,
        }
        fake_indexer = MagicMock()
        fake_indexer.index_all.return_value = fake_result

        config = MagicMock()
        config.validate.return_value = []
        config.chroma_db_path = Path("/fake/chroma/db")
        config.vision_enabled = False

        release_spy = MagicMock()
        with patch("zotpilot.indexer.Indexer", return_value=fake_indexer), \
             patch.object(idx_mod, "_get_config", return_value=config), \
             patch.object(idx_mod, "_get_store") as get_store, \
             patch.object(idx_mod, "IndexJournal"), \
             patch.object(idx_mod, "IndexLease"), \
             patch.object(idx_mod, "acquire_lease"), \
             patch.object(idx_mod, "release_lease", release_spy):
            get_store.return_value.clear_query_cache = MagicMock()
            response = idx_mod.index_library(batch_size=0)

        assert isinstance(response, dict)
        assert response["rate_limited_abort"] is True
        assert response["indexed"] == 1   # exact pre-abort success count survived
        assert response["not_indexed_due_to_abort"] == 2
        release_spy.assert_called_once()


class TestM1StaleInProgress:
    def test_in_progress_doc_survives_quota_dead_abort(self):
        """M1: a doc already in_progress before a quota-dead run is not orphaned —
        it stays in_progress (retriable) and commits on a later healthy run."""
        from zotpilot.embeddings.base import RateLimitError
        from zotpilot.index_authority import IndexJournal, is_doc_committed, mark_in_progress

        item = _make_item("Y")
        indexer = _make_indexer([item])
        indexer.store.delete_document = MagicMock()
        journal = IndexJournal(Path("/tmp/zp-test-journal-m1.json"))
        mark_in_progress(journal, "Y")  # pre-existing in_progress

        # Quota-dead run: 429 immediately.
        def boom(item, extraction, jnl):
            raise RateLimitError("quota", provider="gemini")

        result = _run(indexer, boom, journal=journal)
        assert result["rate_limited_abort"] is True
        assert "Y" in journal.in_progress       # not orphaned/removed
        assert not is_doc_committed(journal, "Y")

        # Subsequent healthy run commits Y.
        from zotpilot.index_authority import mark_committed

        def heal(item, extraction, jnl):
            mark_committed(jnl, item.item_key)
            return _success(item, extraction, jnl)

        result2 = _run(indexer, heal, journal=journal)
        assert result2["indexed"] == 1
        assert is_doc_committed(journal, "Y")


class TestRateLimitRetry:
    """issue #15 follow-up: the embedding layer parses retry_after but the
    indexer used to discard it. These pin that a typed RateLimitError now drives
    a bounded wait+retry of the same paper before the fail-fast abort fires."""

    def test_transient_429_recovers_within_retry_budget(self):
        """A paper that 429s twice then succeeds is indexed, not aborted."""
        from zotpilot.embeddings.base import RateLimitError
        items = [_make_item("K1"), _make_item("K2"), _make_item("K3")]
        indexer = _make_indexer(items)

        calls = {"K2": 0}

        def se(item, extraction, journal):
            if item.item_key == "K2":
                calls["K2"] += 1
                if calls["K2"] <= 2:  # fail first two attempts, succeed on third
                    raise RateLimitError("quota", provider="gemini", retry_after=12.0)
            return _success(item, extraction, journal)

        result = _run(indexer, se)
        statuses = {r.item_key: r.status for r in result["results"]}
        assert statuses == {"K1": "indexed", "K2": "indexed", "K3": "indexed"}
        assert result["indexed"] == 3
        assert result["rate_limited_abort"] is False
        assert calls["K2"] == 3  # 2 failed attempts + 1 success
        assert indexer._sleep.call_count == 2  # waited once per failed attempt
        indexer._sleep.assert_called_with(12.0)  # honored the provider retry_after

    def test_retry_exhausted_then_aborts(self):
        """A persistently 429ing paper retries up to the budget, then aborts —
        preserving the original fail-fast semantics after recovery fails."""
        from zotpilot.embeddings.base import RateLimitError
        items = [_make_item(f"K{i}") for i in range(1, 4)]
        indexer = _make_indexer(items)
        indexer._rate_limit_max_retries = 3

        def se(item, extraction, journal):
            if item.item_key == "K2":
                raise RateLimitError("quota", provider="gemini", retry_after=5.0)
            return _success(item, extraction, journal)

        result = _run(indexer, se)
        assert result["rate_limited_abort"] is True
        assert result["indexed"] == 1  # K1 only
        assert indexer._sleep.call_count == 3  # exactly max_retries waits, then abort
        statuses = {r.item_key: r.status for r in result["results"]}
        assert statuses["K2"] == "failed" and statuses["K3"] == "failed"

    def test_missing_retry_after_uses_default_wait(self):
        """A 429 with no retry_after falls back to the default wait, capped."""
        from zotpilot.embeddings.base import RateLimitError
        from zotpilot.indexer import RATE_LIMIT_DEFAULT_WAIT_SECONDS, RATE_LIMIT_MAX_WAIT_SECONDS
        items = [_make_item("K1")]
        indexer = _make_indexer(items)
        indexer._rate_limit_max_retries = 1

        seen = {"n": 0}

        def se(item, extraction, journal):
            seen["n"] += 1
            if seen["n"] == 1:
                raise RateLimitError("quota", provider="gemini")  # no retry_after
            return _success(item, extraction, journal)

        result = _run(indexer, se)
        assert result["indexed"] == 1
        indexer._sleep.assert_called_once_with(RATE_LIMIT_DEFAULT_WAIT_SECONDS)
        assert RATE_LIMIT_DEFAULT_WAIT_SECONDS <= RATE_LIMIT_MAX_WAIT_SECONDS

    def test_bogus_retry_after_is_capped(self):
        """A wildly large retry_after is clamped to the per-attempt cap."""
        from zotpilot.embeddings.base import RateLimitError
        from zotpilot.indexer import RATE_LIMIT_MAX_WAIT_SECONDS
        items = [_make_item("K1")]
        indexer = _make_indexer(items)
        indexer._rate_limit_max_retries = 1

        seen = {"n": 0}

        def se(item, extraction, journal):
            seen["n"] += 1
            if seen["n"] == 1:
                raise RateLimitError("quota", provider="gemini", retry_after=999999.0)
            return _success(item, extraction, journal)

        _run(indexer, se)
        indexer._sleep.assert_called_once_with(RATE_LIMIT_MAX_WAIT_SECONDS)


class TestEndOfRunRescanGate:
    """#7: the end-of-run library re-scan only runs when something was indexed.
    A no-op/all-current call must not pay a second full library enumeration."""

    def test_noop_run_skips_end_of_run_rescan(self):
        items = [_make_item("K1")]
        indexer = _make_indexer(items, indexed_ids=["K1"])
        indexer._needs_reindex = MagicMock(return_value=(False, "current"))
        result = _run(indexer, _success)
        assert result["indexed"] == 0
        # startup reconcile enumerated once; end-of-run skipped → exactly 1
        assert indexer.zotero.get_all_items_with_pdfs.call_count == 1

    def test_run_that_indexed_still_rescans(self):
        items = [_make_item("K1")]
        indexer = _make_indexer(items)
        result = _run(indexer, _success)
        assert result["indexed"] == 1
        assert indexer.zotero.get_all_items_with_pdfs.call_count >= 2


def _real_config(tmp_path, *, vision_enabled):
    from zotpilot.config import Config
    (tmp_path / "zotero.sqlite").touch()
    return Config(
        zotero_data_dir=tmp_path,
        chroma_db_path=tmp_path / "chroma",
        embedding_model="none",
        embedding_dimensions=0,
        chunk_size=400,
        chunk_overlap=100,
        gemini_api_key=None,
        dashscope_api_key=None,
        gemini_base_url=None,
        embedding_provider="none",
        dashscope_embedding_endpoint="compatible",
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
        vision_enabled=vision_enabled,
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


class TestVisionDisabledByBatchNotice:
    """#5: batch_size>0 silently disables vision; the override must be surfaced."""

    def _run_tool(self, config, **kwargs):
        from zotpilot.tools import indexing as idx_mod
        fake_indexer = MagicMock()
        fake_indexer.index_all.return_value = {
            "results": [], "indexed": 0, "failed": 0, "empty": 0, "skipped": 0,
            "already_indexed": 0, "rate_limited_abort": False, "systemic_abort": False,
            "not_indexed_due_to_abort": 0, "has_more": False, "skipped_no_pdf": [],
        }
        with patch("zotpilot.indexer.Indexer", return_value=fake_indexer), \
             patch.object(idx_mod, "_get_config", return_value=config), \
             patch.object(idx_mod, "_get_store") as get_store, \
             patch.object(idx_mod, "IndexJournal"), \
             patch.object(idx_mod, "IndexLease"), \
             patch.object(idx_mod, "acquire_lease"), \
             patch.object(idx_mod, "release_lease"):
            get_store.return_value.clear_query_cache = MagicMock()
            return idx_mod.index_library(**kwargs)

    def test_batch_disables_vision_is_surfaced(self, tmp_path):
        config = _real_config(tmp_path, vision_enabled=True)
        resp = self._run_tool(config, batch_size=2)
        assert resp["vision_disabled_by_batch"] is True
        assert "_notice_vision" in resp
        assert resp["vision_enabled"] is False

    def test_all_at_once_keeps_vision_no_notice(self, tmp_path):
        config = _real_config(tmp_path, vision_enabled=True)
        resp = self._run_tool(config, batch_size=0)
        assert "vision_disabled_by_batch" not in resp
        assert resp["vision_enabled"] is True

    def test_explicit_no_vision_is_not_flagged_as_batch_override(self, tmp_path):
        config = _real_config(tmp_path, vision_enabled=True)
        resp = self._run_tool(config, batch_size=0, no_vision=True)
        assert "vision_disabled_by_batch" not in resp
        assert resp["vision_enabled"] is False


class TestTableFailureMarkerCleared:
    """#2: stale table/figure-failure markers must be cleared when a doc is
    reprocessed cleanly, so they don't linger and read as still-broken forever."""

    def _extraction_with_tables(self):
        extraction = MagicMock()
        page = MagicMock()
        page.markdown = "body"
        extraction.pages = [page]
        extraction.full_markdown = "body"
        extraction.sections = []
        extraction.stats = {"text_pages": 1, "ocr_pages": 0, "empty_pages": 0}
        extraction.quality_grade = "A"
        table = MagicMock()
        table.artifact_type = None
        table.caption = "Results overview"
        extraction.tables = [table]
        extraction.figures = []
        return extraction

    def _indexer(self, item):
        indexer = _make_indexer([item])
        indexer.store.add_chunks = MagicMock()
        indexer.chunker = MagicMock()
        indexer.chunker.chunk.return_value = [MagicMock()]
        indexer.journal_ranker = MagicMock()
        indexer.journal_ranker.lookup.return_value = "Q1"
        return indexer

    def test_marker_recorded_then_cleared_on_clean_reprocess(self, tmp_path):
        from zotpilot.index_authority import IndexJournal
        item = _make_item("K1")
        indexer = self._indexer(item)
        journal = IndexJournal(tmp_path / "index_journal.json")

        with patch("zotpilot.pdf.reference_matcher.match_references", return_value={}), \
             patch("zotpilot.pdf.reference_matcher.get_reference_context", return_value=""):
            # Run 1: table storage fails (non-429) -> marker recorded.
            indexer.store.add_tables = MagicMock(side_effect=RuntimeError("boom"))
            indexer._index_extraction(item, self._extraction_with_tables(), journal)
            assert "K1" in journal.table_failures

            # Run 2: table storage succeeds -> stale marker cleared.
            indexer.store.add_tables = MagicMock()
            indexer._index_extraction(item, self._extraction_with_tables(), journal)
            assert "K1" not in journal.table_failures

        # Persisted clear survives a reload.
        assert "K1" not in IndexJournal(tmp_path / "index_journal.json").table_failures


class TestGetIndexStatsSurfacesIncomplete:
    """#2: get_index_stats surfaces half-indexed docs (table_failures) that are
    still in the indexed set, so the user can selectively force_reindex them."""

    def test_incomplete_table_docs_surfaced(self, tmp_path):
        from zotpilot.index_authority import IndexJournal, mark_committed, record_table_failure
        from zotpilot.tools import indexing as idx_mod

        chroma = tmp_path / "chroma"
        chroma.mkdir()
        # K1 is indexed but lost its tables; K9 is stale (not in current index) and must be filtered out.
        journal = IndexJournal(tmp_path / "index_journal.json")
        for k in ("K1", "K9"):
            mark_committed(journal, k)
            record_table_failure(journal, k, "table storage: boom")

        config = MagicMock()
        config.embedding_provider = "local"
        config.chroma_db_path = chroma
        config.stats_sample_limit = 100

        store = MagicMock()
        store.count_chunks_for_doc_ids.return_value = 3
        store.collection.get.return_value = {"metadatas": []}

        with patch.object(idx_mod, "_get_config", return_value=config), \
             patch.object(idx_mod, "_get_retriever"), \
             patch.object(idx_mod, "_get_store", return_value=store), \
             patch.object(idx_mod, "_get_zotero"), \
             patch.object(idx_mod, "current_library_pdf_doc_ids", return_value={"K1"}), \
             patch.object(idx_mod, "authoritative_indexed_doc_ids", return_value={"K1"}), \
             patch.object(idx_mod, "_collect_unindexed_papers", return_value=([], 0)):
            result = idx_mod.get_index_stats()

        assert result["incomplete_table_docs_count"] == 1  # K9 filtered (not current)
        assert result["incomplete_table_docs"] == [{"doc_id": "K1", "reason": "table storage: boom"}]
        assert "_notice_incomplete" in result
        assert "table/figure chunks" in result["_notice_incomplete"]
        assert "formula chunks" not in result["_notice_incomplete"]


class TestTableMarkerOrdering:
    """#2 regression: clear_table_failure must run only AFTER tables+figures store
    cleanly, so a failure (esp. a re-raised RateLimitError) keeps the marker."""

    def _ext(self):
        extraction = MagicMock()
        page = MagicMock()
        page.markdown = "body"
        extraction.pages = [page]
        extraction.full_markdown = "body"
        extraction.sections = []
        extraction.stats = {"text_pages": 1, "ocr_pages": 0, "empty_pages": 0}
        extraction.quality_grade = "A"
        table = MagicMock()
        table.artifact_type = None
        table.caption = "Results"
        extraction.tables = [table]
        extraction.figures = []
        return extraction

    def _idx(self, item):
        indexer = _make_indexer([item])
        indexer.store.add_chunks = MagicMock()
        indexer.chunker = MagicMock()
        indexer.chunker.chunk.return_value = [MagicMock()]
        indexer.journal_ranker = MagicMock()
        indexer.journal_ranker.lookup.return_value = "Q1"
        return indexer

    def test_marker_kept_when_table_fails_this_run(self, tmp_path):
        from zotpilot.index_authority import IndexJournal
        item = _make_item("K1")
        indexer = self._idx(item)
        indexer.store.add_tables = MagicMock(side_effect=RuntimeError("boom"))
        journal = IndexJournal(tmp_path / "j.json")
        with patch("zotpilot.pdf.reference_matcher.match_references", return_value={}), \
             patch("zotpilot.pdf.reference_matcher.get_reference_context", return_value=""):
            indexer._index_extraction(item, self._ext(), journal)
        # swallowed failure this run → marker present, NOT cleared at end
        assert "K1" in journal.table_failures

    def test_prior_marker_survives_table_ratelimit(self, tmp_path):
        from zotpilot.embeddings.base import RateLimitError
        from zotpilot.index_authority import IndexJournal, record_table_failure
        item = _make_item("K1")
        indexer = self._idx(item)
        indexer.store.add_tables = MagicMock(side_effect=RateLimitError("quota", provider="gemini"))
        journal = IndexJournal(tmp_path / "j.json")
        record_table_failure(journal, "K1", "prior failure")  # stale marker from a prior run
        with patch("zotpilot.pdf.reference_matcher.match_references", return_value={}), \
             patch("zotpilot.pdf.reference_matcher.get_reference_context", return_value=""):
            with pytest.raises(RateLimitError):
                indexer._index_extraction(item, self._ext(), journal)
        # 429 re-raises before the end-of-function clear → prior marker intact
        assert "K1" in journal.table_failures

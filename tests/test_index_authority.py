import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from zotpilot.index_authority import (
    authoritative_indexed_doc_ids,
    current_library_pdf_doc_ids,
    orphaned_index_doc_ids,
    reconcile_orphaned_index_docs,
)


def _item(key: str, has_pdf: bool = True):
    pdf_path = Path(f"/tmp/{key}.pdf") if has_pdf else None
    if pdf_path is not None:
        exists = MagicMock(return_value=True)
        pdf_path = SimpleNamespace(exists=exists)
    return SimpleNamespace(item_key=key, pdf_path=pdf_path)


def _requires_journal():
    """Skip test if IndexJournal and related classes are not yet implemented."""
    try:
        from zotpilot.index_authority import IndexJournal

        return IndexJournal
    except ImportError:
        pytest.skip("IndexJournal/lease classes not yet implemented")


def test_current_library_pdf_doc_ids_only_keeps_resolved_pdfs():
    zotero = MagicMock()
    zotero.get_all_items_with_pdfs.return_value = [
        _item("DOC1", has_pdf=True),
        _item("DOC2", has_pdf=False),
    ]

    assert current_library_pdf_doc_ids(zotero) == {"DOC1"}


def test_authoritative_indexed_doc_ids_excludes_orphans():
    store = MagicMock()
    store.get_indexed_doc_ids.return_value = {"DOC1", "DOC2", "DOC3"}

    assert authoritative_indexed_doc_ids(store, {"DOC1", "DOC3"}) == {"DOC1", "DOC3"}
    assert orphaned_index_doc_ids(store, {"DOC1", "DOC3"}) == {"DOC2"}


def test_authoritative_indexed_doc_ids_prefers_journal_for_touched_docs_and_keeps_legacy_raw(tmp_path):
    from zotpilot.index_authority import IndexJournal, mark_committed

    store = MagicMock()
    store.db_path = tmp_path / "chroma"
    store.get_indexed_doc_ids.return_value = {"DOC1", "DOC2", "DOC3"}

    journal = IndexJournal(tmp_path / "index_journal.json")
    mark_committed(journal, "DOC1")

    assert authoritative_indexed_doc_ids(store, {"DOC1", "DOC2"}) == {"DOC1", "DOC2"}


def test_authoritative_indexed_doc_ids_filters_out_non_current_committed_docs(tmp_path):
    from zotpilot.index_authority import IndexJournal, mark_committed

    store = MagicMock()
    store.db_path = tmp_path / "chroma"
    store.get_indexed_doc_ids.return_value = {"DOC1", "DOC9"}

    journal = IndexJournal(tmp_path / "index_journal.json")
    mark_committed(journal, "DOC1")
    mark_committed(journal, "DOC9")

    assert authoritative_indexed_doc_ids(store, {"DOC1", "DOC2"}) == {"DOC1"}


def test_authoritative_indexed_doc_ids_with_empty_store_does_not_return_all_current_docs():
    store = MagicMock()
    store.get_indexed_doc_ids.return_value = set()

    assert authoritative_indexed_doc_ids(store, {"DOC1", "DOC2"}) == set()


def test_authoritative_indexed_doc_ids_merges_legacy_raw_docs_with_committed_journal(tmp_path):
    from zotpilot.index_authority import IndexJournal, mark_committed, mark_in_progress

    store = MagicMock()
    store.db_path = tmp_path / "chroma"
    store.get_indexed_doc_ids.return_value = {"DOC1", "DOC2", "DOC3"}

    journal = IndexJournal(tmp_path / "index_journal.json")
    mark_committed(journal, "DOC1")
    mark_in_progress(journal, "DOC3")

    assert authoritative_indexed_doc_ids(store, {"DOC1", "DOC2", "DOC3"}) == {"DOC1", "DOC2"}


def test_authoritative_indexed_doc_ids_drops_committed_docs_missing_from_store(tmp_path):
    from zotpilot.index_authority import IndexJournal, mark_committed

    store = MagicMock()
    store.db_path = tmp_path / "chroma"
    store.get_indexed_doc_ids.return_value = {"DOC2"}

    journal = IndexJournal(tmp_path / "index_journal.json")
    mark_committed(journal, "DOC1")

    assert authoritative_indexed_doc_ids(store, {"DOC1", "DOC2"}) == {"DOC2"}


def test_reconcile_orphaned_index_docs_deletes_missing_docs():
    store = MagicMock()
    store.get_indexed_doc_ids.return_value = {"DOC1", "DOC2", "DOC3"}

    result = reconcile_orphaned_index_docs(store, {"DOC1"})

    assert result == {
        "orphaned_doc_ids": ["DOC2", "DOC3"],
        "deleted_count": 2,
    }
    store.delete_document.assert_any_call("DOC2")
    store.delete_document.assert_any_call("DOC3")


# ---------------------------------------------------------------------------
# Journal state tests
# ---------------------------------------------------------------------------


class TestJournalStates:
    """Tests for IndexJournal lifecycle: empty -> in_progress -> committed."""

    def test_new_journal_is_empty(self):
        _requires_journal()
        from zotpilot.index_authority import IndexJournal

        journal = IndexJournal()
        assert journal.get_committed_doc_ids() == set()

    def test_mark_in_progress_records_doc(self):
        _requires_journal()
        from zotpilot.index_authority import IndexJournal, mark_in_progress

        journal = IndexJournal()
        mark_in_progress(journal, "DOC1")
        assert "DOC1" in journal.in_progress

    def test_mark_committed_updates_state(self):
        _requires_journal()
        from zotpilot.index_authority import (
            IndexJournal,
            mark_committed,
            mark_in_progress,
        )

        journal = IndexJournal()
        mark_in_progress(journal, "DOC1")
        mark_committed(journal, "DOC1")
        assert "DOC1" in journal.committed
        assert "DOC1" not in journal.in_progress

    def test_committed_docs_appear_in_journal_index(self):
        _requires_journal()
        from zotpilot.index_authority import (
            IndexJournal,
            get_committed_doc_ids,
            mark_committed,
            mark_in_progress,
        )

        journal = IndexJournal()
        mark_in_progress(journal, "DOC1")
        mark_committed(journal, "DOC1")
        assert get_committed_doc_ids(journal) == {"DOC1"}

    def test_in_progress_not_in_committed_set(self):
        _requires_journal()
        from zotpilot.index_authority import (
            IndexJournal,
            get_committed_doc_ids,
            mark_committed,
            mark_in_progress,
        )

        journal = IndexJournal()
        mark_in_progress(journal, "DOC1")
        mark_in_progress(journal, "DOC2")
        mark_committed(journal, "DOC1")
        committed = get_committed_doc_ids(journal)
        assert "DOC1" in committed
        assert "DOC2" not in committed


# ---------------------------------------------------------------------------
# Crash-before-commit test
# ---------------------------------------------------------------------------


class TestCrashBeforeCommit:
    """A doc that never reached committed must not appear in authoritative set."""

    def test_crash_before_commit_excludes_doc(self):
        _requires_journal()
        from zotpilot.index_authority import (
            IndexJournal,
            authoritative_indexed_doc_ids_with_journal,
            mark_in_progress,
        )

        journal = IndexJournal()
        mark_in_progress(journal, "DOC1")
        # Never call mark_committed -- simulates crash
        authoritative = authoritative_indexed_doc_ids_with_journal(MagicMock(), {"DOC1", "DOC2"}, journal)
        assert "DOC1" not in authoritative


# ---------------------------------------------------------------------------
# Crash-after-commit tests
# ---------------------------------------------------------------------------


class TestCrashAfterCommit:
    """A committed doc survives process crashes."""

    def test_committed_doc_included_despite_table_failure(self):
        _requires_journal()
        from zotpilot.index_authority import (
            IndexJournal,
            authoritative_indexed_doc_ids_with_journal,
            mark_committed,
            mark_in_progress,
            record_table_failure,
        )

        journal = IndexJournal()
        mark_in_progress(journal, "DOC1")
        mark_committed(journal, "DOC1")
        record_table_failure(journal, "DOC1", "vision error")
        authoritative = authoritative_indexed_doc_ids_with_journal(MagicMock(), {"DOC1"}, journal)
        assert "DOC1" in authoritative

    def test_committed_doc_retains_indexed_status(self):
        _requires_journal()
        from zotpilot.index_authority import (
            IndexJournal,
            is_doc_committed,
            mark_committed,
            mark_in_progress,
        )

        journal = IndexJournal()
        mark_in_progress(journal, "DOC1")
        mark_committed(journal, "DOC1")
        assert is_doc_committed(journal, "DOC1")


# ---------------------------------------------------------------------------
# Lease contention tests
# ---------------------------------------------------------------------------


class TestLeaseContention:
    """Tests for mutual-exclusion via IndexLease."""

    def test_acquire_lease_succeeds_first_time(self):
        _requires_journal()
        from zotpilot.index_authority import IndexLease, acquire_lease

        lease = IndexLease()
        result = acquire_lease(lease)
        assert result is not None

    def test_second_acquire_fails_fast(self):
        _requires_journal()
        from zotpilot.index_authority import (
            IndexLease,
            LeaseContentionError,
            acquire_lease,
        )

        lease = IndexLease()
        acquire_lease(lease)
        with pytest.raises(LeaseContentionError):
            acquire_lease(lease)

    def test_lease_records_holder_pid(self):
        _requires_journal()
        from zotpilot.index_authority import IndexLease, acquire_lease

        lease = IndexLease()
        acquire_lease(lease)
        assert lease.holder_pid == os.getpid()
        assert lease.acquired_at is not None


# ---------------------------------------------------------------------------
# Stale lease recovery tests
# ---------------------------------------------------------------------------


class TestStaleLeaseRecovery:
    """Dead/old leases should be cleared so indexing can proceed."""

    def test_stale_pid_cleared(self):
        _requires_journal()
        from zotpilot.index_authority import IndexLease, acquire_lease

        lease = IndexLease()
        # Simulate a lease held by a non-existent PID
        lease.holder_pid = 9999999
        lease.acquired_at = time.time()
        acquire_lease(lease)  # should clear stale and succeed
        assert lease.holder_pid == os.getpid()

    def test_stale_timestamp_cleared(self):
        _requires_journal()
        from zotpilot.index_authority import IndexLease, acquire_lease

        lease = IndexLease()
        lease.holder_pid = os.getpid()
        lease.acquired_at = time.time() - 120  # 2 minutes ago
        acquire_lease(lease)  # should clear stale and succeed
        assert lease.holder_pid == os.getpid()
        assert time.time() - lease.acquired_at < 60

    def test_fresh_lease_not_cleared(self):
        _requires_journal()
        from zotpilot.index_authority import (
            IndexLease,
            LeaseContentionError,
            acquire_lease,
        )

        lease = IndexLease()
        acquire_lease(lease)
        # Second acquire should still fail -- lease is fresh
        with pytest.raises(LeaseContentionError):
            acquire_lease(lease)


# ---------------------------------------------------------------------------
# Table-failure-after-commit tests
# ---------------------------------------------------------------------------


class TestTableFailureAfterCommit:
    """Vision/table errors after commit must not revoke committed status."""

    def test_table_failure_is_warning_only(self):
        _requires_journal()
        from zotpilot.index_authority import (
            IndexJournal,
            is_doc_committed,
            mark_committed,
            mark_in_progress,
            record_table_failure,
        )

        journal = IndexJournal()
        mark_in_progress(journal, "DOC1")
        mark_committed(journal, "DOC1")
        record_table_failure(journal, "DOC1", "timeout")
        assert is_doc_committed(journal, "DOC1")

    def test_table_failure_recorded_in_journal(self):
        _requires_journal()
        from zotpilot.index_authority import (
            IndexJournal,
            mark_committed,
            mark_in_progress,
            record_table_failure,
        )

        journal = IndexJournal()
        mark_in_progress(journal, "DOC1")
        mark_committed(journal, "DOC1")
        record_table_failure(journal, "DOC1", "vision error")
        assert "DOC1" in journal.table_failures

"""Helpers for reconciling Chroma index state with the current Zotero PDF library."""

import json
import os
import tempfile
import time
from pathlib import Path


def current_library_pdf_doc_ids(zotero) -> set[str]:
    """Return current Zotero item keys that still have resolved PDF files."""
    doc_ids: set[str] = set()
    for item in zotero.get_all_items_with_pdfs():
        if item.pdf_path and item.pdf_path.exists():
            doc_ids.add(item.item_key)
    return doc_ids


def _stored_doc_ids_or_current(store, current_doc_ids: set[str]) -> set[str]:
    """Best-effort read of stored doc IDs.

    In production stores this should be a concrete set-like value. In tests or
    partial mocks, missing/non-iterable values fall back to the current library
    set so journal authority still works.
    """
    getter = getattr(store, "get_indexed_doc_ids", None)
    if getter is None:
        return set(current_doc_ids)
    try:
        raw = getter()
    except Exception:
        return set(current_doc_ids)
    if isinstance(raw, (set, list, tuple)):
        return set(raw)
    return set(current_doc_ids)


# ---------------------------------------------------------------------------
# Journal state management
# ---------------------------------------------------------------------------


class IndexJournal:
    """In-memory journal tracking doc indexing state with atomic disk persistence."""

    def __init__(self, journal_path: str | Path | None = None) -> None:
        self._path: Path | None = None
        if journal_path is not None:
            self._path = Path(journal_path)
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self.committed: dict[str, dict] = {}
        self.in_progress: dict[str, dict] = {}
        self.table_failures: dict[str, str] = {}
        self._load()

    @property
    def path(self) -> Path | None:
        return self._path

    def _load(self) -> None:
        """Load journal from disk if a path is set and the file exists."""
        if self._path is None or not self._path.exists():
            return
        with open(self._path, encoding="utf-8") as f:
            data = json.load(f)
        for doc_id, entry in data.items():
            if entry.get("state") == "committed":
                self.committed[doc_id] = entry
            elif entry.get("state") == "in_progress":
                self.in_progress[doc_id] = entry
            if "table_failure" in entry:
                self.table_failures[doc_id] = entry["table_failure"]

    def _save(self) -> None:
        """Persist journal to disk using atomic write (tempfile + os.replace)."""
        if self._path is None:
            return
        data: dict[str, dict] = {}
        for doc_id, entry in self.in_progress.items():
            data[doc_id] = entry
        for doc_id, entry in self.committed.items():
            data[doc_id] = entry
            if doc_id in self.table_failures:
                data[doc_id]["table_failure"] = self.table_failures[doc_id]

        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp", prefix="zotpilot_journal_")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, self._path)
            tmp_path = None
        except OSError as e:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise RuntimeError(f"Failed to write journal to {self._path}: {e}") from e

    def get_committed_doc_ids(self) -> set[str]:
        """Return the set of committed doc IDs from the journal."""
        return set(self.committed.keys())


class IndexLease:
    """Mutual-exclusion lease for indexing operations."""

    def __init__(self, lease_path: str | Path | None = None) -> None:
        self._path: Path | None = None
        if lease_path is not None:
            self._path = Path(lease_path)
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self.holder_pid: int | None = None
        self.acquired_at: float | None = None
        self._load()

    @property
    def path(self) -> Path | None:
        return self._path

    def _load(self) -> None:
        """Load lease from disk if a path is set and the file exists."""
        if self._path is None or not self._path.exists():
            return
        with open(self._path, encoding="utf-8") as f:
            data = json.load(f)
        self.holder_pid = data.get("holder_pid")
        self.acquired_at = data.get("acquired_at")

    def _save(self) -> None:
        """Persist lease to disk using atomic write."""
        if self._path is None:
            return
        data = {
            "holder_pid": self.holder_pid,
            "acquired_at": self.acquired_at,
        }
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp", prefix="zotpilot_lease_")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, self._path)
            tmp_path = None
        except OSError as e:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise RuntimeError(f"Failed to write lease to {self._path}: {e}") from e


class LeaseContentionError(Exception):
    """Raised when a lease cannot be acquired due to an active holder."""

    pass


# ---------------------------------------------------------------------------
# Journal helper functions
# ---------------------------------------------------------------------------


def mark_in_progress(journal: IndexJournal, doc_id: str) -> None:
    """Mark a document as currently being indexed."""
    entry = {"state": "in_progress", "timestamp": time.time()}
    journal.in_progress[doc_id] = entry
    journal.committed.pop(doc_id, None)
    journal._save()


def mark_committed(journal: IndexJournal, doc_id: str) -> None:
    """Mark a document as successfully indexed."""
    entry = {"state": "committed", "timestamp": time.time()}
    journal.committed[doc_id] = entry
    journal.in_progress.pop(doc_id, None)
    journal._save()


def get_committed_doc_ids(journal: IndexJournal) -> set[str]:
    """Return the set of committed doc IDs from the journal."""
    return set(journal.committed.keys())


def get_touched_doc_ids(journal: IndexJournal) -> set[str]:
    """Return all journal-tracked doc IDs (committed + in_progress)."""
    return set(journal.committed.keys()) | set(journal.in_progress.keys())


def is_doc_committed(journal: IndexJournal, doc_id: str) -> bool:
    """Check if a specific document is committed."""
    return doc_id in journal.committed


def record_table_failure(journal: IndexJournal, doc_id: str, reason: str) -> None:
    """Record a table/vision extraction failure for a committed doc (warning only)."""
    journal.table_failures[doc_id] = reason
    if doc_id in journal.committed:
        journal.committed[doc_id]["table_failure"] = reason
        journal._save()


def acquire_lease(lease: IndexLease) -> str | None:
    """Attempt to acquire an indexing lease. Returns lease ID on success.

    Stale leases (dead PID or older than 60 seconds) are cleared automatically.
    """
    now = time.time()
    if lease.holder_pid is not None and lease.acquired_at is not None:
        # Check if lease is stale
        pid_alive = _is_pid_alive(lease.holder_pid)
        age = now - lease.acquired_at
        if not pid_alive or age > 60:
            # Stale lease — clear it
            lease.holder_pid = None
            lease.acquired_at = None
            lease._save()
        else:
            raise LeaseContentionError(f"Indexing lease held by PID {lease.holder_pid} (acquired {age:.0f}s ago)")

    lease.holder_pid = os.getpid()
    lease.acquired_at = now
    lease._save()
    return "active"


def release_lease(lease: IndexLease) -> None:
    """Release the current indexing lease."""
    lease.holder_pid = None
    lease.acquired_at = None
    lease._save()


def _is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is alive."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Authority functions (existing, updated)
# ---------------------------------------------------------------------------


def authoritative_indexed_doc_ids(store, current_doc_ids: set[str]) -> set[str]:
    """Return authoritative indexed doc IDs for the current library.

    Rules:
    - Start from docs that are both in the current library and in the store
    - If no journal exists, return that raw intersection
    - If a journal exists, committed journal docs are authoritative for touched docs
    - Legacy raw docs not represented in the journal are preserved
    - In-progress journal docs are excluded
    """
    current = set(current_doc_ids)
    stored = _stored_doc_ids_or_current(store, current)
    raw_indexed = current & stored

    db_path = getattr(store, "db_path", None)
    if db_path is None:
        return raw_indexed

    journal_path = Path(db_path).parent / "index_journal.json"
    if not journal_path.exists():
        return raw_indexed

    journal = IndexJournal(journal_path)
    touched = get_touched_doc_ids(journal)
    committed = get_committed_doc_ids(journal) & raw_indexed
    legacy_raw = raw_indexed - touched
    return committed | legacy_raw


def authoritative_indexed_doc_ids_with_journal(store, current_doc_ids: set[str], journal: IndexJournal) -> set[str]:
    """Return indexed doc IDs based on journal authority that still exist in the current library."""
    current = set(current_doc_ids)
    stored = _stored_doc_ids_or_current(store, current)
    raw_indexed = current & stored
    touched = get_touched_doc_ids(journal)
    committed = get_committed_doc_ids(journal) & raw_indexed
    legacy_raw = raw_indexed - touched
    return committed | legacy_raw


def orphaned_index_doc_ids(store, current_doc_ids: set[str]) -> set[str]:
    """Return indexed doc IDs that are no longer present in the current Zotero PDF library."""
    current = set(current_doc_ids)
    return set(store.get_indexed_doc_ids()) - current


def reconcile_orphaned_index_docs(store, current_doc_ids: set[str]) -> dict:
    """Delete orphaned indexed docs from Chroma and return a summary."""
    orphaned = sorted(orphaned_index_doc_ids(store, current_doc_ids))
    for doc_id in orphaned:
        store.delete_document(doc_id)
    return {
        "orphaned_doc_ids": orphaned,
        "deleted_count": len(orphaned),
    }

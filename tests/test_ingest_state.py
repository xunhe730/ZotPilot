"""Tests for BatchState/BatchStore data structures (TDD - written before implementation)."""

from __future__ import annotations

import threading
import time

from zotpilot.tools.ingest_state import BatchState, BatchStore, IngestItemState


class TestIngestItemState:
    def test_create_pending_item_defaults(self):
        item = IngestItemState(index=0, url="https://example.com/paper.pdf")
        assert item.index == 0
        assert item.url == "https://example.com/paper.pdf"
        assert item.status == "pending"
        assert item.title is None
        assert item.item_key is None
        assert item.error is None

    def test_to_dict_omits_none_optional_fields(self):
        item = IngestItemState(index=0, url="https://example.com/paper.pdf")
        d = item.to_dict()
        assert d["index"] == 0
        assert d["url"] == "https://example.com/paper.pdf"
        assert d["status"] == "pending"
        # None optional fields must be omitted
        assert "title" not in d
        assert "item_key" not in d
        assert "error" not in d

    def test_to_dict_includes_set_optional_fields(self):
        item = IngestItemState(
            index=1,
            url="https://example.com/paper2.pdf",
            title="A Great Paper",
            status="saved",
            item_key="ABC123",
            routing_status="routed_by_connector",
        )
        d = item.to_dict()
        assert d["title"] == "A Great Paper"
        assert d["item_key"] == "ABC123"
        assert d["status"] == "saved"
        assert d["routing_status"] == "routed_by_connector"
        assert "error" not in d

    def test_to_dict_includes_error_when_set(self):
        item = IngestItemState(index=2, url=None, status="failed", error="timeout")
        d = item.to_dict()
        assert d["error"] == "timeout"
        assert d["status"] == "failed"
        # url is always included even if None
        assert "url" in d

    def test_to_dict_always_includes_index_url_status(self):
        item = IngestItemState(index=5, url=None)
        d = item.to_dict()
        assert "index" in d
        assert "url" in d
        assert "status" in d

    def test_status_literals(self):
        for status in ("pending", "saved", "duplicate", "failed"):
            item = IngestItemState(index=0, url=None, status=status)
            assert item.status == status


class TestBatchState:
    def _make_batch(self, n=3, **kwargs):
        items = [IngestItemState(index=i, url=f"https://ex.com/{i}") for i in range(n)]
        return BatchState(
            total=n,
            collection_used=kwargs.get("collection_used"),
            pending_items=items,
        )

    def test_batch_default_state_is_queued(self):
        batch = self._make_batch()
        assert batch.state == "queued"
        assert batch.is_final is False

    def test_batch_id_auto_generated(self):
        b1 = self._make_batch()
        b2 = self._make_batch()
        assert b1.batch_id.startswith("ing_")
        assert b1.batch_id != b2.batch_id

    def test_summary_counts(self):
        batch = self._make_batch(n=3)
        s = batch.summary()
        assert s["total"] == 3
        assert s["saved"] == 0
        assert s["failed"] == 0
        assert s["pending_count"] == 3
        assert s["state"] == "queued"
        assert s["is_final"] is False
        assert "batch_id" in s

    def test_update_item_mark_saved(self):
        batch = self._make_batch(n=3)
        batch.update_item(0, status="saved", item_key="KEY1", title="Paper One", routing_status="routed_by_backend")
        assert batch.pending_items[0].status == "saved"
        assert batch.pending_items[0].item_key == "KEY1"
        assert batch.pending_items[0].title == "Paper One"
        assert batch.pending_items[0].routing_status == "routed_by_backend"

    def test_update_item_mark_failed(self):
        batch = self._make_batch(n=2)
        batch.update_item(1, status="failed", error="403 Forbidden")
        assert batch.pending_items[1].status == "failed"
        assert batch.pending_items[1].error == "403 Forbidden"

    def test_update_item_can_clear_warning(self):
        batch = self._make_batch(n=1)
        batch.update_item(0, status="saved", warning="needs reconciliation")
        batch.update_item(0, status="saved", warning=None, routing_status="routed_by_reconciliation_local")
        assert batch.pending_items[0].warning is None
        assert batch.pending_items[0].routing_status == "routed_by_reconciliation_local"

    def test_summary_counts_after_updates(self):
        batch = self._make_batch(n=4)
        batch.update_item(0, status="saved", item_key="K1")
        batch.update_item(1, status="saved", item_key="K2")
        batch.update_item(2, status="failed", error="err")
        s = batch.summary()
        assert s["saved"] == 2
        assert s["failed"] == 1
        assert s["pending_count"] == 1

    def test_finalize_all_saved(self):
        batch = self._make_batch(n=2)
        batch.update_item(0, status="saved", item_key="K1")
        batch.update_item(1, status="saved", item_key="K2")
        batch.finalize()
        assert batch.is_final is True
        assert batch.state == "completed"
        assert batch.finalized_at is not None

    def test_finalize_with_some_errors(self):
        batch = self._make_batch(n=3)
        batch.update_item(0, status="saved", item_key="K1")
        batch.update_item(1, status="failed", error="err")
        batch.update_item(2, status="saved", item_key="K3")
        batch.finalize()
        assert batch.is_final is True
        assert batch.state == "completed_with_errors"

    def test_finalize_all_failed(self):
        batch = self._make_batch(n=2)
        batch.update_item(0, status="failed", error="e1")
        batch.update_item(1, status="failed", error="e2")
        batch.finalize()
        assert batch.state == "failed"

    def test_finalize_with_duplicates_treated_as_resolved(self):
        batch = self._make_batch(n=2)
        batch.update_item(0, status="duplicate")
        batch.update_item(1, status="saved", item_key="K1")
        batch.finalize()
        assert batch.is_final is True
        # duplicates + saved = completed (no errors)
        assert batch.state == "completed"

    def test_full_status_includes_results(self):
        batch = self._make_batch(n=2)
        batch.update_item(0, status="saved", item_key="K1")
        fs = batch.full_status()
        assert "results" in fs
        assert len(fs["results"]) == 2
        assert fs["results"][0]["status"] == "saved"

    def test_update_item_with_gap_indices(self):
        """update_item must find items even when index sequence has gaps (duplicates skipped)."""
        # Simulate: index 0 saved, index 1 duplicate (skipped), index 2 saved
        items = [
            IngestItemState(index=0, url="https://ex.com/0"),
            IngestItemState(index=2, url="https://ex.com/2"),  # gap: index 1 missing
        ]
        batch = BatchState(total=3, collection_used=None, pending_items=items)
        # Can update index 2 even though pending_items[1] has index 2
        batch.update_item(2, status="saved", item_key="K2")
        assert batch.pending_items[1].status == "saved"
        assert batch.pending_items[1].item_key == "K2"
        # Index 0 is still pending (not updated)
        assert batch.pending_items[0].status == "pending"

    def test_full_status_includes_instruction_when_final_with_saved(self):
        """_instruction is included when batch is final with saved items."""
        batch = self._make_batch(n=1)
        batch.update_item(0, status="saved", item_key="K1")
        batch.finalize()
        fs = batch.full_status()
        assert "_instruction" in fs
        assert "post-ingest" in fs["_instruction"]

    def test_full_status_includes_poll_instruction_when_running(self):
        """_instruction is a poll hint when batch is still running with pending items."""
        batch = self._make_batch(n=2)
        batch.update_item(0, status="saved", item_key="K1")
        fs = batch.full_status()
        assert "_instruction" in fs
        assert "get_ingest_status" in fs["_instruction"]

    def test_full_status_no_instruction_when_final_no_saved(self):
        """No _instruction when batch is final but nothing was saved (all-failed)."""
        batch = self._make_batch(n=1)
        batch.update_item(0, status="failed", error="timeout")
        batch.finalize()
        fs = batch.full_status()
        assert "_instruction" not in fs

    def test_summary_includes_collection_used(self):
        batch = self._make_batch(n=1, collection_used="MyCollection")
        s = batch.summary()
        assert s["collection_used"] == "MyCollection"

    def test_update_item_thread_safety(self):
        """Concurrent updates must not corrupt state."""
        n = 100
        items = [IngestItemState(index=i, url=f"https://ex.com/{i}") for i in range(n)]
        batch = BatchState(total=n, collection_used=None, pending_items=items)

        errors = []

        def mark_saved(idx):
            try:
                batch.update_item(idx, status="saved", item_key=f"K{idx}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=mark_saved, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        saved = sum(1 for it in batch.pending_items if it.status == "saved")
        assert saved == n


class TestBatchStore:
    def test_put_and_get(self):
        store = BatchStore()
        batch = BatchState(
            total=1,
            collection_used=None,
            pending_items=[IngestItemState(index=0, url="https://ex.com/0")],
        )
        store.put(batch)
        retrieved = store.get(batch.batch_id)
        assert retrieved is batch

    def test_get_unknown_returns_none(self):
        store = BatchStore()
        assert store.get("nonexistent_id") is None

    def test_count(self):
        store = BatchStore()
        assert store.count() == 0
        b = BatchState(
            total=1,
            collection_used=None,
            pending_items=[IngestItemState(index=0, url=None)],
        )
        store.put(b)
        assert store.count() == 1

    def test_clear(self):
        store = BatchStore()
        b = BatchState(
            total=1,
            collection_used=None,
            pending_items=[IngestItemState(index=0, url=None)],
        )
        store.put(b)
        store.clear()
        assert store.count() == 0

    def test_ttl_evicts_completed_batches(self):
        store = BatchStore(completed_ttl_s=0.01)  # 10ms TTL
        b = BatchState(
            total=1,
            collection_used=None,
            pending_items=[IngestItemState(index=0, url=None)],
        )
        b.update_item(0, status="saved", item_key="K1")
        b.finalize()
        store.put(b)
        assert store.count() == 1

        # Simulate TTL expiry by backdating finalized_at
        b.finalized_at = time.monotonic() - 1.0  # 1 second ago > 10ms TTL
        store.evict_expired()
        assert store.count() == 0

    def test_running_batch_not_evicted_by_ttl(self):
        store = BatchStore(completed_ttl_s=0.01)
        b = BatchState(
            total=1,
            collection_used=None,
            pending_items=[IngestItemState(index=0, url=None)],
            state="running",
        )
        store.put(b)
        # Backdate created_at to simulate old running batch
        b.created_at = time.monotonic() - 9999
        store.evict_expired()
        # Still present because it's not finalized
        assert store.count() == 1

    def test_max_batches_evicts_oldest_completed(self):
        store = BatchStore(max_batches=3)

        completed_batches = []
        for _ in range(3):
            b = BatchState(
                total=1,
                collection_used=None,
                pending_items=[IngestItemState(index=0, url=None)],
            )
            b.update_item(0, status="saved", item_key="K")
            b.finalize()
            store.put(b)
            completed_batches.append(b)
            time.sleep(0.001)  # ensure monotonic ordering

        assert store.count() == 3

        # Add a 4th batch — should evict oldest completed
        new_batch = BatchState(
            total=1,
            collection_used=None,
            pending_items=[IngestItemState(index=0, url=None)],
        )
        store.put(new_batch)
        assert store.count() == 3
        # Oldest completed batch should be gone
        assert store.get(completed_batches[0].batch_id) is None
        # Newest should still be there
        assert store.get(new_batch.batch_id) is not None

    def test_max_batches_never_evicts_running(self):
        store = BatchStore(max_batches=2)

        # Fill with running batches
        running = []
        for _ in range(2):
            b = BatchState(
                total=1,
                collection_used=None,
                pending_items=[IngestItemState(index=0, url=None)],
                state="running",
            )
            store.put(b)
            running.append(b)

        # Try to add a third — cannot evict running batches
        new_b = BatchState(
            total=1,
            collection_used=None,
            pending_items=[IngestItemState(index=0, url=None)],
        )
        store.put(new_b)  # should not raise, just store (or overflow gracefully)
        # All running batches must still be present
        for r in running:
            assert store.get(r.batch_id) is not None

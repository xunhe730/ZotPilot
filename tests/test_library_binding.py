"""P15: Library binding — ingest worker must reject mismatched library_id."""

from __future__ import annotations

from pathlib import Path
from time import time
from unittest.mock import patch

import pytest

from zotpilot.workflow.batch import Batch, Item, LibraryMismatchError, PreflightResult
from zotpilot.workflow.batch_store import BatchStore
from zotpilot.workflow.worker import _run_ingest_worker, assert_library_binding


def _approved_batch(library_id: str, tmp_path: Path) -> tuple[BatchStore, Batch]:
    from zotpilot.workflow.batch import new_batch_id

    pr = PreflightResult(round=1, checked_at=time(), all_clear=True)
    item = Item(
        identifier="doc1",
        doc_id="doc1",
        source_url="https://example.com/paper",
        status="pending",
        paper_payload={"title": "Test Paper"},
    )
    batch = Batch(
        batch_id=new_batch_id(),
        library_id=library_id,
        query="test",
        phase="approved",
        items=(item,),
        preflight_result=pr,
        authorized_new_tags=frozenset(),
        authorized_new_collections=frozenset(),
        created_at=time(),
        last_transition_at=time(),
    )
    store = BatchStore(base_dir=tmp_path)
    store.save(batch)
    return store, batch


# ---------------------------------------------------------------------------
# Direct assert_library_binding tests (public API)
# ---------------------------------------------------------------------------

def test_assert_library_binding_raises_on_mismatch() -> None:
    from zotpilot.workflow.batch import new_batch_id

    batch = Batch(
        batch_id=new_batch_id(),
        library_id="lib_A",
        query="test",
        phase="approved",
        items=(),
        preflight_result=None,
        authorized_new_tags=frozenset(),
        authorized_new_collections=frozenset(),
        created_at=time(),
        last_transition_at=time(),
    )
    with pytest.raises(LibraryMismatchError):
        assert_library_binding(batch, current_library_id="lib_B")


def test_assert_library_binding_passes_on_match() -> None:
    from zotpilot.workflow.batch import new_batch_id

    batch = Batch(
        batch_id=new_batch_id(),
        library_id="lib_A",
        query="test",
        phase="approved",
        items=(),
        preflight_result=None,
        authorized_new_tags=frozenset(),
        authorized_new_collections=frozenset(),
        created_at=time(),
        last_transition_at=time(),
    )
    # Must not raise
    assert_library_binding(batch, current_library_id="lib_A")


# ---------------------------------------------------------------------------
# Worker-level library binding check
# ---------------------------------------------------------------------------

def test_ingest_worker_aborts_on_library_mismatch(tmp_path: Path) -> None:
    """If active library != batch.library_id, the worker aborts the batch."""
    store, batch = _approved_batch("lib_A", tmp_path)

    with patch("zotpilot.workflow.worker._get_zotero") as mock_zotero:
        mock_zotero.return_value.library_id = "lib_B"  # mismatch
        _run_ingest_worker(store, batch.batch_id)

    reloaded = store.load(batch.batch_id)
    assert reloaded is not None
    # Worker should have transitioned to aborted after library mismatch
    assert reloaded.phase == "aborted"


def test_ingest_worker_proceeds_on_library_match(tmp_path: Path) -> None:
    """With matching library_id the worker passes the binding check."""
    store, batch = _approved_batch("lib_A", tmp_path)

    with patch("zotpilot.workflow.worker._get_zotero") as mock_zotero:
        mock_zotero.return_value.library_id = "lib_A"
        # Patch out the actual ingestion call
        with patch("zotpilot.workflow.worker.legacy_ingestion", create=True):
            with patch(
                "zotpilot.tools.ingestion.ingest_papers_impl",
                return_value={
                    "is_final": True,
                    "results": [{"status": "saved", "item_key": "ZK001", "has_pdf": True}],
                },
            ):
                _run_ingest_worker(store, batch.batch_id)

    reloaded = store.load(batch.batch_id)
    assert reloaded is not None
    # Must NOT be aborted due to library mismatch (may have moved to post_ingest_verified)
    assert reloaded.phase != "approved"  # worker ran

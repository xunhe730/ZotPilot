"""P3: Post-process worker entry-phase gate.

The worker must refuse to proceed if batch.phase != "post_ingest_approved"
(or "taxonomy_authorized", which is the re-entry path).  Every other phase
must result in the batch transitioning to "aborted" (because
_run_post_process_worker catches the exception and aborts).
"""

from __future__ import annotations

from pathlib import Path
from time import time
from unittest.mock import patch

import pytest

from zotpilot.workflow.batch import Batch, new_batch_id
from zotpilot.workflow.batch_store import BatchStore
from zotpilot.workflow.worker import _run_post_process_worker


def _make_batch(phase: str, tmp_path: Path) -> tuple[Batch, BatchStore]:
    batch = Batch(
        batch_id=new_batch_id(),
        library_id="lib_test",
        query="test query",
        phase=phase,  # type: ignore[arg-type]
        items=(),
        preflight_result=None,
        authorized_new_tags=frozenset(),
        authorized_new_collections=frozenset(),
        created_at=time(),
        last_transition_at=time(),
    )
    store = BatchStore(base_dir=tmp_path)
    store.save(batch)
    return batch, store


# Every phase that is NOT a valid post-process entry point
REJECTED_PHASES = [
    "candidate",
    "candidates_confirmed",
    "preflighting",
    "preflight_blocked",
    "approved",
    "ingesting",
    "post_ingest_verified",
    "post_processing",
    "post_ingest_skipped",
    "post_process_verified",
    "done",
    "aborted",
]


@pytest.mark.parametrize("phase", REJECTED_PHASES)
def test_post_process_worker_rejects_wrong_phase(phase: str, tmp_path: Path) -> None:
    """_run_post_process_worker must not advance batches not in an entry-eligible phase.

    Phases that allow -> aborted transition will land in 'aborted'.
    Phases with no 'aborted' edge (candidates_confirmed, preflight_blocked,
    post_ingest_skipped, done) stay in their original phase because the
    abort-on-error handler also fails silently — the batch is still rejected.
    In both cases the batch must NOT be in any post-processing phase.
    """
    batch, store = _make_batch(phase, tmp_path)

    with patch("zotpilot.workflow.worker._get_zotero") as mock_zotero:
        mock_zotero.return_value.library_id = "lib_test"
        # Run synchronously (not via executor)
        _run_post_process_worker(store, batch.batch_id)

    reloaded = store.load(batch.batch_id)
    assert reloaded is not None
    # The batch must NOT have advanced into or through post_processing.
    # Phases that have an 'aborted' edge will land there; others stay put.
    assert reloaded.phase not in {"post_processing", "post_process_verified"}, (
        f"Worker must not advance phase={phase!r} into post-processing, "
        f"but got phase={reloaded.phase!r}"
    )
    # The phase must either be 'aborted' (clean error path) or unchanged
    # (double-abort failed silently). Either outcome is correct: the worker
    # was rejected and did not make progress.
    assert reloaded.phase in {"aborted", phase}, (
        f"After rejecting phase={phase!r}, expected 'aborted' or unchanged, "
        f"got {reloaded.phase!r}"
    )


def test_post_process_worker_accepts_post_ingest_approved(tmp_path: Path) -> None:
    """Worker must accept post_ingest_approved and advance to post_processing."""
    batch, store = _make_batch("post_ingest_approved", tmp_path)

    with patch("zotpilot.workflow.worker._get_zotero") as mock_zotero:
        mock_zotero.return_value.library_id = "lib_test"
        # Patch _index_item so we don't need real infrastructure
        with patch("zotpilot.workflow.worker._index_item", side_effect=lambda it: it):
            _run_post_process_worker(store, batch.batch_id)

    reloaded = store.load(batch.batch_id)
    assert reloaded is not None
    # Should have progressed past post_processing
    assert reloaded.phase in {"post_process_verified", "aborted"}, (
        f"Unexpected terminal phase: {reloaded.phase!r}"
    )
    # With no items and _index_item patched, it should succeed fully
    assert reloaded.phase == "post_process_verified"


def test_post_process_worker_accepts_taxonomy_authorized(tmp_path: Path) -> None:
    """Worker must also accept taxonomy_authorized as a re-entry point."""
    batch, store = _make_batch("taxonomy_authorized", tmp_path)

    with patch("zotpilot.workflow.worker._get_zotero") as mock_zotero:
        mock_zotero.return_value.library_id = "lib_test"
        with patch("zotpilot.workflow.worker._index_item", side_effect=lambda it: it):
            _run_post_process_worker(store, batch.batch_id)

    reloaded = store.load(batch.batch_id)
    assert reloaded is not None
    assert reloaded.phase in {"post_process_verified", "aborted"}
    assert reloaded.phase == "post_process_verified"


def test_post_process_worker_missing_batch_is_noop(tmp_path: Path) -> None:
    """If the batch_id does not exist, the worker exits cleanly without side-effects."""
    store = BatchStore(base_dir=tmp_path)
    with patch("zotpilot.workflow.worker._get_zotero") as mock_zotero:
        mock_zotero.return_value.library_id = "lib_test"
        # Should not raise
        _run_post_process_worker(store, "nonexistent_batch_id")

"""P2, P13, P14: Replay 2026-04-08 incident events.

Key assertion: no path advances past approve_ingest without going through
the 'approved' phase; invoking the ingest worker on a non-approved batch
raises/rejects.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zotpilot.workflow.batch import (
    Batch,
    IllegalPhaseTransition,
    InvalidPhaseError,
    PreflightResult,
)

INCIDENT_FILE = Path(__file__).parent / "incidents" / "2026_04_08_post_ingest_index_gate.jsonl"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_done_batch() -> Batch:
    from time import time

    from zotpilot.workflow.batch import Item, new_batch_id

    item = Item(
        identifier="10.1145/3544548.3581111",
        doc_id="10.1145/3544548.3581111",
        source_url="https://doi.org/10.1145/3544548.3581111",
        status="saved",
        pdf_present=True,
        metadata_complete=True,
        indexed=True,
        tagged=True,
        classified=True,
        zotero_item_key="ABC123",
    )
    return Batch(
        batch_id=new_batch_id(),
        library_id="lib_test",
        query="test",
        phase="done",
        items=(item,),
        preflight_result=None,
        authorized_new_tags=frozenset(),
        authorized_new_collections=frozenset(),
        created_at=time(),
        last_transition_at=time(),
    )


def _batch_at(phase: str) -> Batch:
    from time import time

    from zotpilot.workflow.batch import new_batch_id

    return Batch(
        batch_id=new_batch_id(),
        library_id="lib_test",
        query="test",
        phase=phase,  # type: ignore[arg-type]
        items=(),
        preflight_result=None,
        authorized_new_tags=frozenset(),
        authorized_new_collections=frozenset(),
        created_at=time(),
        last_transition_at=time(),
    )


# ---------------------------------------------------------------------------
# Incident file loads
# ---------------------------------------------------------------------------

def test_incident_file_exists() -> None:
    assert INCIDENT_FILE.exists(), f"Incident file missing: {INCIDENT_FILE}"


def test_incident_file_parseable() -> None:
    events = [json.loads(line) for line in INCIDENT_FILE.read_text().splitlines() if line.strip()]
    assert len(events) > 0


# ---------------------------------------------------------------------------
# P2: Cannot advance past approve_ingest without 'approved' phase
# ---------------------------------------------------------------------------

def test_transition_to_ingesting_requires_approved_phase() -> None:
    """Only a batch in 'approved' phase may transition to 'ingesting'."""
    # Any phase that is NOT 'approved' must reject -> 'ingesting'
    non_approved_phases = [
        "candidate",
        "candidates_confirmed",
        "preflighting",
        "preflight_blocked",
        "post_ingest_verified",
        "post_ingest_approved",
        "post_processing",
        "post_process_verified",
        "done",
        "aborted",
    ]
    for phase in non_approved_phases:
        batch = _batch_at(phase)
        with pytest.raises((IllegalPhaseTransition, InvalidPhaseError)):
            batch.transition_to("ingesting")


def test_approved_batch_can_transition_to_ingesting() -> None:
    """A batch in 'approved' phase MUST be able to advance to 'ingesting'."""
    pr = PreflightResult(round=1, checked_at=0.0, all_clear=True)
    batch = _batch_at("preflighting").set_preflight_result(pr).mark_approved()
    assert batch.phase == "approved"
    ingesting = batch.transition_to("ingesting")
    assert ingesting.phase == "ingesting"


# ---------------------------------------------------------------------------
# P13 / P14: Ingest worker rejects non-approved batch
# ---------------------------------------------------------------------------

def test_ingest_worker_requires_approved_phase(tmp_path: Path) -> None:
    """_run_ingest_worker must call batch.assert_phase('approved'); if batch
    is in wrong phase the assertion fires and the batch is aborted (not silently
    advanced)."""
    from zotpilot.workflow.batch_store import BatchStore
    from zotpilot.workflow.worker import _run_ingest_worker

    store = BatchStore(base_dir=tmp_path)
    # Put a batch in post_ingest_verified — past ingest, should be rejected
    batch = _batch_at("post_ingest_verified")
    store.save(batch)

    # The worker will raise InvalidPhaseError internally; it catches and aborts
    _run_ingest_worker(store, batch.batch_id)

    reloaded = store.load(batch.batch_id)
    assert reloaded is not None
    # Worker must not have advanced to 'ingesting' from wrong phase
    assert reloaded.phase != "ingesting"


def test_cannot_re_approve_already_ingested_batch() -> None:
    """Incident event: approve_ingest called on post_ingest_verified batch
    must raise IllegalPhaseTransition (the 2026-04-08 pattern)."""
    batch = _batch_at("post_ingest_verified")
    with pytest.raises((IllegalPhaseTransition, InvalidPhaseError)):
        # mark_approved calls transition_to('approved') which is illegal
        batch.transition_to("approved")


# ---------------------------------------------------------------------------
# Replay: each incident event asserts its documented outcome
# ---------------------------------------------------------------------------

def test_incident_event_index_library_blocked_before_approve() -> None:
    """Event 2: index_library (post-process) cannot run on post_ingest_verified
    without approve_post_ingest first.  The new tool is approve_post_ingest;
    attempting to run post-process worker on post_ingest_verified is illegal."""
    batch = _batch_at("post_ingest_verified")
    with pytest.raises((IllegalPhaseTransition, InvalidPhaseError)):
        # post_processing is only reachable from post_ingest_approved
        batch.transition_to("post_processing")


def test_incident_event_approve_ingest_from_post_ingest_verified_blocked() -> None:
    """Event 3: approve_ingest on post_ingest_verified must be rejected."""
    batch = _batch_at("post_ingest_verified")
    with pytest.raises((IllegalPhaseTransition, InvalidPhaseError)):
        batch.transition_to("ingesting")


def test_incident_event_post_ingest_approved_required_before_post_process() -> None:
    """Event 5 (index with approved=True): index cannot run without going
    through post_ingest_approved -> post_processing chain."""
    batch = _batch_at("post_ingest_verified")
    # Correct path: transition to post_ingest_approved first
    approved = batch.transition_to("post_ingest_approved")
    assert approved.phase == "post_ingest_approved"
    processing = approved.transition_to("post_processing")
    assert processing.phase == "post_processing"

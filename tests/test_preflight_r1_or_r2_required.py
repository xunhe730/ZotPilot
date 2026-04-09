"""P4: approve_ingest / mark_approved requires preflight all_clear (R1 or R2)."""

from __future__ import annotations

from time import time

import pytest

from zotpilot.workflow.batch import (
    Batch,
    InvalidPhaseError,
    PreflightResult,
)


def _preflighting_batch(**extra) -> Batch:
    from zotpilot.workflow.batch import new_batch_id

    defaults: dict = dict(
        batch_id=new_batch_id(),
        library_id="lib_test",
        query="test",
        phase="preflighting",
        items=(),
        preflight_result=None,
        authorized_new_tags=frozenset(),
        authorized_new_collections=frozenset(),
        created_at=time(),
        last_transition_at=time(),
    )
    defaults.update(extra)
    return Batch(**defaults)


def test_no_preflight_result_raises() -> None:
    """transition to approved with preflight_result=None must raise."""
    batch = _preflighting_batch()
    assert batch.preflight_result is None
    with pytest.raises(InvalidPhaseError):
        batch.mark_approved()


def test_round1_all_clear_false_raises() -> None:
    """R1 result with all_clear=False must be rejected."""
    pr = PreflightResult(round=1, checked_at=time(), all_clear=False)
    batch = _preflighting_batch(preflight_result=pr)
    with pytest.raises(InvalidPhaseError):
        batch.mark_approved()


def test_round1_all_clear_true_succeeds() -> None:
    """R1 result with all_clear=True must allow approval."""
    pr = PreflightResult(round=1, checked_at=time(), all_clear=True)
    batch = _preflighting_batch(preflight_result=pr)
    approved = batch.mark_approved()
    assert approved.phase == "approved"


def test_round2_all_clear_true_succeeds() -> None:
    """R2 result with all_clear=True must allow approval."""
    pr = PreflightResult(round=2, checked_at=time(), all_clear=True)
    batch = _preflighting_batch(preflight_result=pr)
    approved = batch.mark_approved()
    assert approved.phase == "approved"


def test_round2_all_clear_false_raises() -> None:
    """R2 result with all_clear=False must be rejected."""
    pr = PreflightResult(round=2, checked_at=time(), all_clear=False)
    batch = _preflighting_batch(preflight_result=pr)
    with pytest.raises(InvalidPhaseError):
        batch.mark_approved()


def test_mark_approved_returns_new_batch_object() -> None:
    """Immutability: mark_approved must return a new Batch, not mutate in place."""
    pr = PreflightResult(round=1, checked_at=time(), all_clear=True)
    batch = _preflighting_batch(preflight_result=pr)
    approved = batch.mark_approved()
    assert approved is not batch
    assert batch.phase == "preflighting"  # original unchanged
    assert approved.phase == "approved"

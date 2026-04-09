"""P1: Full cartesian-product test of _ALLOWED_TRANSITIONS."""

from __future__ import annotations

import pytest

from zotpilot.workflow.batch import (
    _ALLOWED_TRANSITIONS,
    Batch,
    IllegalPhaseTransition,
    Phase,
    PreflightResult,
)

ALL_PHASES: list[Phase] = list(_ALLOWED_TRANSITIONS.keys())


def _make_batch(phase: Phase) -> Batch:
    """Create a minimal frozen Batch in the requested phase."""
    from time import time

    from zotpilot.workflow.batch import new_batch_id

    kwargs: dict = dict(
        batch_id=new_batch_id(),
        library_id="lib_test",
        query="test",
        phase=phase,
        items=(),
        preflight_result=None,
        authorized_new_tags=frozenset(),
        authorized_new_collections=frozenset(),
        created_at=time(),
        last_transition_at=time(),
    )
    return Batch(**kwargs)


@pytest.mark.parametrize(
    "from_phase,to_phase",
    [
        (f, t)
        for f in ALL_PHASES
        for t in ALL_PHASES
        if t not in _ALLOWED_TRANSITIONS[f]
    ],
)
def test_illegal_transitions_raise(from_phase: Phase, to_phase: Phase) -> None:
    batch = _make_batch(from_phase)
    with pytest.raises(IllegalPhaseTransition):
        batch.transition_to(to_phase)


@pytest.mark.parametrize(
    "from_phase,to_phase",
    [
        (f, t)
        for f in ALL_PHASES
        for t in _ALLOWED_TRANSITIONS[f]
    ],
)
def test_allowed_transitions_succeed(from_phase: Phase, to_phase: Phase) -> None:
    # For 'preflighting' -> 'approved' we need a valid preflight result
    batch = _make_batch(from_phase)
    if from_phase == "preflighting" and to_phase == "approved":
        pr = PreflightResult(round=1, checked_at=0.0, all_clear=True)
        batch = batch.set_preflight_result(pr)
        result = batch.mark_approved()
    else:
        result = batch.transition_to(to_phase)
    assert result.phase == to_phase
    assert result is not batch  # immutable: must be a new object

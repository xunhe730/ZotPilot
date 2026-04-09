"""P7: next_action contract — every non-terminal batch phase returns a valid
next_action dict with all 4 required keys, or None for terminal phases."""

from __future__ import annotations

from time import time

import pytest

from zotpilot.workflow.batch import (
    _ALLOWED_TRANSITIONS,
    TERMINAL_PHASES,
    Batch,
    BlockingDecision,
    Phase,
    PreflightResult,
)

_REQUIRED_KEYS = {"tool", "args_hint", "why", "blocks_on"}


def _batch_at(phase: Phase, **extra) -> Batch:
    from zotpilot.workflow.batch import new_batch_id

    defaults: dict = dict(
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
    defaults.update(extra)
    return Batch(**defaults)


ALL_PHASES: list[Phase] = list(_ALLOWED_TRANSITIONS.keys())


@pytest.mark.parametrize("phase", ALL_PHASES)
def test_next_action_contract(phase: Phase) -> None:
    """For terminal phases next_action must be None.
    For all other phases it must be a dict with all 4 required keys."""
    if phase == "preflight_blocked":
        # Needs a blocking decision with a URL payload to generate urls list
        bd = BlockingDecision(
            decision_id="preflight_blocked",
            description="anti-bot",
            item_keys=("doc1",),
            payload={"url": "https://example.com"},
        )
        pr = PreflightResult(round=1, checked_at=time(), all_clear=False, blocking_decisions=(bd,))
        batch = _batch_at(phase, preflight_result=pr)
    elif phase == "AwaitingTaxonomyAuthorization":
        batch = _batch_at(
            phase,
            pending_taxonomy_tags=("new-tag",),
            pending_taxonomy_collections=(),
        )
    else:
        batch = _batch_at(phase)

    result = batch.next_action_payload()

    if phase in TERMINAL_PHASES:
        assert result is None, f"Phase {phase!r}: expected None, got {result!r}"
    else:
        assert result is not None, f"Phase {phase!r}: expected dict, got None"
        assert isinstance(result, dict), f"Phase {phase!r}: next_action must be a dict"
        missing = _REQUIRED_KEYS - result.keys()
        assert not missing, (
            f"Phase {phase!r}: next_action missing keys {missing}. Got: {sorted(result.keys())}"
        )
        # blocks_on must be one of the two valid values
        assert result["blocks_on"] in {"user", "worker"}, (
            f"Phase {phase!r}: blocks_on={result['blocks_on']!r} is not 'user' or 'worker'"
        )
        # tool must be a non-empty string
        assert isinstance(result["tool"], str) and result["tool"], (
            f"Phase {phase!r}: tool must be a non-empty string"
        )
        # why must be a non-empty string
        assert isinstance(result["why"], str) and result["why"], (
            f"Phase {phase!r}: why must be a non-empty string"
        )

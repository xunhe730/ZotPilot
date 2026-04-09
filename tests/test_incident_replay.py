"""P14: Incident replay corpus structural verification.

Loads every tests/incidents/*.jsonl file, validates the schema of each event,
and asserts that at least one event in each trace represents a *rejected*
operation (phase_after == phase_before), which is the signature of the new
phase machine blocking an illegal tool call.

The actual replay execution (feeding events through the live state machine)
is marked TODO — it requires a complex integration harness that is out of
scope for this structural guardian test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

INCIDENTS_DIR = Path(__file__).parent / "incidents"

REQUIRED_FIELDS = {"tool", "args", "phase_before", "phase_after"}


def _load_traces() -> list[tuple[Path, list[dict[str, Any]]]]:
    """Return (path, events) for every .jsonl file in the incidents directory."""
    if not INCIDENTS_DIR.exists():
        return []
    traces = []
    for jsonl_path in sorted(INCIDENTS_DIR.glob("*.jsonl")):
        events: list[dict[str, Any]] = []
        for lineno, raw_line in enumerate(jsonl_path.read_text().splitlines(), 1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as exc:
                pytest.fail(
                    f"{jsonl_path.name}:{lineno} — invalid JSON: {exc}"
                )
        if events:
            traces.append((jsonl_path, events))
    return traces


def test_incidents_directory_exists() -> None:
    assert INCIDENTS_DIR.exists(), (
        f"Incidents directory missing: {INCIDENTS_DIR}. "
        "At least one .jsonl replay trace is required for P14."
    )


def test_at_least_one_incident_file() -> None:
    traces = _load_traces()
    assert traces, (
        f"No .jsonl files found under {INCIDENTS_DIR}. "
        "Add at least one incident replay trace."
    )


@pytest.mark.parametrize(
    "jsonl_path,events",
    _load_traces(),
    ids=[p.name for p, _ in _load_traces()],
)
def test_incident_event_schema(jsonl_path: Path, events: list[dict[str, Any]]) -> None:
    """Every event in a trace must have the required schema fields."""
    for idx, event in enumerate(events):
        missing = REQUIRED_FIELDS - set(event.keys())
        assert not missing, (
            f"{jsonl_path.name} event[{idx}] missing fields: {sorted(missing)}. "
            f"Event: {event!r}"
        )
        # phase_before and phase_after must be non-empty strings
        for field in ("phase_before", "phase_after"):
            value = event[field]
            assert isinstance(value, str) and value, (
                f"{jsonl_path.name} event[{idx}] has empty/non-string {field!r}: {value!r}"
            )
        # tool must be a non-empty string
        assert isinstance(event["tool"], str) and event["tool"], (
            f"{jsonl_path.name} event[{idx}] has empty/non-string 'tool': {event['tool']!r}"
        )
        # args must be a dict (may be empty)
        assert isinstance(event["args"], dict), (
            f"{jsonl_path.name} event[{idx}] 'args' must be a dict, got {type(event['args']).__name__}"
        )


@pytest.mark.parametrize(
    "jsonl_path,events",
    _load_traces(),
    ids=[p.name for p, _ in _load_traces()],
)
def test_incident_trace_contains_rejected_event(
    jsonl_path: Path, events: list[dict[str, Any]]
) -> None:
    """Each trace must contain at least one event where the phase machine rejected
    the tool call (phase_after == phase_before).  This is the defining signature
    of the 2026-04-08 class of incidents: the agent tried to advance but the
    state machine held the phase stable.
    """
    rejected = [
        e for e in events
        if e.get("phase_before") == e.get("phase_after")
    ]
    assert rejected, (
        f"{jsonl_path.name} contains no rejected events (phase_after == phase_before). "
        "An incident replay trace must include at least one event where the phase "
        "machine blocked a tool call, otherwise it does not exercise the guard."
    )


# ---------------------------------------------------------------------------
# TODO: Full replay harness (out of scope for structural guardian)
# ---------------------------------------------------------------------------
# When the batch state machine integration harness is available, add a test
# that feeds each trace through the live machine and verifies the final phase.
# Mark that test with @pytest.mark.integration and run it separately.
#
# Pseudocode:
#   for event in trace:
#       result = state_machine.dispatch(event["tool"], event["args"])
#       assert state_machine.current_phase == event["phase_after"]

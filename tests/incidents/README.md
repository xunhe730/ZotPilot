# Incident Replay Corpus

Each file in this directory is a JSONL fixture recording the tool-call trace of a real
or synthetic incident. These fixtures feed `tests/test_incident_replay.py` (P14) and
`tests/test_incident_2026_04_08_replay.py` (P2/P13), which replay the trace against the
real MCP adapter and assert that the incident cannot recur.

## File naming convention

```
YYYY_MM_DD_<short_slug>.jsonl
```

One file per incident. Never delete old files — they are a permanent regression corpus.

## Line schema

Each line is a self-contained JSON object:

```json
{
  "timestamp":    "<ISO-8601 UTC>",
  "tool":         "<mcp_tool_name>",
  "args":         { "<param>": "<value>", "..." : "..." },
  "result":       { "<field>": "<value>", "..." : "..." },
  "phase_before": "<Phase literal>",
  "phase_after":  "<Phase literal>"
}
```

| Field | Type | Description |
|---|---|---|
| `timestamp` | string | Wall-clock time the tool was called (UTC ISO-8601). Used for ordering only — replay is sequential by line. |
| `tool` | string | Name of the MCP tool that was called (matches `@mcp.tool` function name). |
| `args` | object | Exact arguments passed to the tool. |
| `result` | object | Exact response returned by the tool, including any `error_code`, `status`, and `next_action`. |
| `phase_before` | string | `Batch.phase` value immediately before the call. Must be a valid `Phase` literal defined in `_ALLOWED_TRANSITIONS`. |
| `phase_after` | string | `Batch.phase` value immediately after the call. Must equal `phase_before` when the call was rejected or a no-op. |

Valid `Phase` literals (from `_ALLOWED_TRANSITIONS` in `workflow/batch.py`):

```
candidate, candidates_confirmed, preflighting, preflight_blocked,
approved, ingesting, post_ingest_verified, post_ingest_approved,
post_processing, AwaitingTaxonomyAuthorization, taxonomy_authorized,
post_ingest_skipped, post_process_verified, done, aborted
```

## How `tests/test_incident_2026_04_08_replay.py` consumes these files

The test:

1. Loads `tests/incidents/2026_04_08_post_ingest_index_gate.jsonl` line by line.
2. For each line, replays the tool call against the real adapter (with a fresh in-memory
   `BatchStore`) using the recorded `args`.
3. Asserts that:
   - If the recorded `result` contains `error_code`, the replay also raises/returns that
     error code (incident-class calls must still be rejected).
   - `phase_after` matches the batch's actual phase after replay.
   - No call that should be blocked results in a successful side-effect (e.g. no embedding
     writes, no Zotero API calls).
4. The final `phase_after` across all lines must match the expected terminal state
   recorded in the fixture (typically `post_ingest_verified` for the 2026-04-08 incident,
   meaning the batch stayed stuck rather than silently proceeding).

## How `tests/test_incident_replay.py` consumes these files

`test_incident_replay.py` is the generic replay harness:

1. Discovers all `*.jsonl` files under `tests/incidents/`.
2. For each file, runs the same replay + assertion loop described above.
3. Parameterises pytest so each incident gets its own test node ID, e.g.
   `test_incident_replay[2026_04_08_post_ingest_index_gate]`.

## Adding a new incident

1. Reproduce or reconstruct the tool-call trace (from logs, from a failing test, or
   synthetically).
2. Create a new file: `tests/incidents/YYYY_MM_DD_<slug>.jsonl`.
3. Ensure every line's `phase_before`/`phase_after` uses a valid `Phase` literal.
4. Add a corresponding named test in `tests/test_incident_<YYYY_MM_DD>_<slug>.py` if the
   incident has property-specific assertions beyond what the generic harness covers.
5. Open a PR — the CI gate (`test_incident_replay`) will run all fixtures automatically.

**Rule: a fix PR for a new incident class must include the fixture before it includes
the fix. The fixture is the spec; the fix is the implementation.**

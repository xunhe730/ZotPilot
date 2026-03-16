"""Debug database interface for recording and inspecting table extraction results."""
from __future__ import annotations

import json
import sqlite3


EXTENDED_SCHEMA = """\
CREATE TABLE IF NOT EXISTS vision_agent_results (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    table_id          TEXT NOT NULL,
    agent_idx         INTEGER NOT NULL,
    agent_role        TEXT,
    model             TEXT NOT NULL,
    raw_response      TEXT,
    headers_json      TEXT,
    rows_json         TEXT,
    table_label       TEXT,
    is_incomplete     INTEGER,
    incomplete_reason TEXT,
    parse_success     INTEGER,
    execution_time_ms INTEGER,
    corrections_json  TEXT,
    num_corrections   INTEGER,
    cell_accuracy_pct REAL,
    footnotes         TEXT
);

CREATE TABLE IF NOT EXISTS vision_run_details (
    table_id            TEXT PRIMARY KEY,
    text_layer_caption  TEXT,
    vision_caption      TEXT,
    page_num            INTEGER,
    crop_bbox_json      TEXT,
    recropped           BOOLEAN DEFAULT 0,
    recrop_bbox_pct_json TEXT,
    parse_success       BOOLEAN,
    is_incomplete       BOOLEAN,
    incomplete_reason   TEXT,
    recrop_needed       BOOLEAN,
    raw_response        TEXT,
    headers_json        TEXT,
    rows_json           TEXT,
    footnotes           TEXT,
    table_label         TEXT,
    fullpage_attempted  BOOLEAN DEFAULT 0,
    fullpage_parse_success BOOLEAN
);
"""


def create_extended_tables(con: sqlite3.Connection) -> None:
    """Execute the extended schema on an existing connection.

    Safe to call multiple times — all statements use CREATE TABLE IF NOT EXISTS.
    """
    con.executescript(EXTENDED_SCHEMA)


def write_vision_agent_result(
    con: sqlite3.Connection,
    table_id: str,
    agent_idx: int,
    model: str,
    raw_response: str | None,
    headers_json: str | None,
    rows_json: str | None,
    table_label: str | None,
    is_incomplete: bool,
    incomplete_reason: str,
    parse_success: bool,
    execution_time_ms: int | None,
    agent_role: str | None = None,
    corrections_json: str | None = None,
    num_corrections: int | None = None,
    cell_accuracy_pct: float | None = None,
    footnotes: str | None = None,
) -> None:
    """Insert a single vision agent result row."""
    con.execute(
        "INSERT INTO vision_agent_results "
        "(table_id, agent_idx, agent_role, model, raw_response, headers_json, rows_json, "
        "table_label, is_incomplete, incomplete_reason, parse_success, execution_time_ms, "
        "corrections_json, num_corrections, cell_accuracy_pct, footnotes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (table_id, agent_idx, agent_role, model, raw_response, headers_json, rows_json,
         table_label, int(is_incomplete), incomplete_reason, int(parse_success),
         execution_time_ms, corrections_json, num_corrections, cell_accuracy_pct,
         footnotes),
    )


def write_vision_run_detail(
    con: sqlite3.Connection,
    *,
    table_id: str,
    details_dict: dict,
) -> None:
    """Insert or replace a vision run detail row from a details dict.

    Uses INSERT OR REPLACE for idempotency — writing the same table_id twice
    overwrites the previous entry.

    The details_dict must contain the keys defined by the vision_details schema
    in Task 4.3.1.
    """
    crop_bbox = details_dict.get("crop_bbox")
    recrop_bbox_pct = details_dict.get("recrop_bbox_pct")
    fullpage_ps = details_dict.get("fullpage_parse_success")
    con.execute(
        "INSERT OR REPLACE INTO vision_run_details "
        "(table_id, text_layer_caption, vision_caption, page_num, crop_bbox_json, "
        "recropped, recrop_bbox_pct_json, parse_success, is_incomplete, "
        "incomplete_reason, recrop_needed, raw_response, headers_json, rows_json, "
        "footnotes, table_label, fullpage_attempted, fullpage_parse_success) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            table_id,
            details_dict.get("text_layer_caption"),
            details_dict.get("vision_caption"),
            details_dict.get("page_num"),
            json.dumps(crop_bbox) if crop_bbox is not None else None,
            int(bool(details_dict.get("recropped", False))),
            json.dumps(recrop_bbox_pct) if recrop_bbox_pct is not None else None,
            int(bool(details_dict.get("parse_success", False))),
            int(bool(details_dict.get("is_incomplete", False))),
            details_dict.get("incomplete_reason"),
            int(bool(details_dict.get("recrop_needed", False))),
            details_dict.get("raw_response"),
            json.dumps(details_dict.get("headers", [])),
            json.dumps(details_dict.get("rows", [])),
            details_dict.get("footnotes"),
            details_dict.get("table_label"),
            int(bool(details_dict.get("fullpage_attempted", False))),
            int(fullpage_ps) if fullpage_ps is not None else None,
        ),
    )


def clear_vision_results(
    db_path: str,
    item_key: str | None = None,
) -> int:
    """Delete vision rows matching the given scope.

    Returns the total number of deleted rows across both tables.
    """
    with sqlite3.connect(db_path) as con:
        deleted = 0
        for table in ("vision_agent_results", "vision_run_details"):
            try:
                if item_key:
                    cur = con.execute(
                        f"DELETE FROM {table} WHERE table_id LIKE ?",
                        (f"{item_key}%",),
                    )
                else:
                    cur = con.execute(f"DELETE FROM {table}")
                deleted += cur.rowcount
            except sqlite3.OperationalError:
                pass  # table doesn't exist yet
    return deleted



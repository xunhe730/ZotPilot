"""Tests for research session workflow guardrails."""

from unittest.mock import MagicMock, patch

import pytest

from zotpilot.state import ToolError
from zotpilot.tools.ingest_state import BatchState, IngestItemState
from zotpilot.workflow import SessionStore


def _make_store(tmp_path):
    return SessionStore(tmp_path / "sessions")


def test_research_session_create_get_and_approve(tmp_path):
    store = _make_store(tmp_path)

    with (
        patch("zotpilot.tools.workflow._STORE", store),
        patch("zotpilot.tools.workflow.current_library_id", return_value="1"),
        patch("zotpilot.workflow.session_store.current_library_id", return_value="1"),
    ):
        from zotpilot.tools.workflow import research_session

        created = research_session(action="create", query="attention mechanisms")
        fetched = research_session(action="get")
        approved = research_session(
            action="approve",
            session_id=created["session_id"],
            checkpoint="candidate-review",
        )

    assert created["query"] == "attention mechanisms"
    assert fetched["session_id"] == created["session_id"]
    assert approved["approved_checkpoints"] == ["candidate-review"]
    assert approved["phase"] == "ingest"


def test_ingest_papers_blocks_without_candidate_review_approval(tmp_path):
    store = _make_store(tmp_path)
    store.create(query="test topic", library_id="1")

    with (
        patch("zotpilot.tools.ingestion._session_store", store),
        patch("zotpilot.tools.ingestion.current_library_id", return_value="1"),
    ):
        from zotpilot.tools.ingestion import ingest_papers

        with pytest.raises(ToolError, match="requires user approval before ingest"):
            ingest_papers(papers=[{"doi": "10.1000/test"}])


def test_get_ingest_status_moves_session_to_post_ingest_review(tmp_path):
    store = _make_store(tmp_path)
    session = store.create(query="test topic", library_id="1")
    session.approve("candidate-review")
    store.save(session)

    item = MagicMock()
    item.title = "Test Paper"
    item.date_added = "2026-04-07T00:00:00Z"

    batch = BatchState(
        total=1,
        collection_used="INBOX",
        pending_items=[IngestItemState(index=0, url="https://example.com", status="saved", item_key="ITEM1")],
        session_id=session.session_id,
    )
    batch.finalize()

    with (
        patch("zotpilot.tools.ingestion._session_store", store),
        patch("zotpilot.tools.ingestion._batch_store.get", return_value=batch),
        patch("zotpilot.workflow.session_store._get_zotero") as mock_get_zotero,
    ):
        mock_get_zotero.return_value.get_item.return_value = item
        from zotpilot.tools.ingestion import get_ingest_status

        result = get_ingest_status(batch.batch_id)
        updated = store.load(session.session_id)

    assert result["session_id"] == session.session_id
    assert updated is not None
    assert updated.status == "awaiting_user"
    assert updated.phase == "post-ingest-review"
    assert updated.items[0].item_key == "ITEM1"


def test_create_note_idempotent_skips_existing_zotpilot_note(tmp_path):
    store = _make_store(tmp_path)
    writer = MagicMock()
    writer.get_notes.return_value = [
        {"key": "NOTE1", "title": "[ZotPilot] Existing note", "content": "existing"},
    ]

    with (
        patch("zotpilot.tools.write_ops._session_store", store),
        patch("zotpilot.tools.write_ops._get_writer", return_value=writer),
    ):
        from zotpilot.tools.write_ops import create_note

        result = create_note(item_key="ITEM1", content="new content", idempotent=True)

    assert result["skipped"] is True
    assert result["existing_note_key"] == "NOTE1"
    writer.create_note.assert_not_called()


def test_create_note_blocks_when_post_ingest_review_not_approved(tmp_path):
    store = _make_store(tmp_path)
    store.create(query="test topic", library_id="1")

    with (
        patch("zotpilot.tools.write_ops._session_store", store),
        patch("zotpilot.tools.write_ops.current_library_id", return_value="1"),
    ):
        from zotpilot.tools.write_ops import create_note

        with pytest.raises(ToolError, match="Post-ingest steps require user approval"):
            create_note(item_key="ITEM1", content="note body")

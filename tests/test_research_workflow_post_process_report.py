"""Report truthfulness checks for post-process summaries."""

from __future__ import annotations

from zotpilot.tools import research_workflow as rw
from zotpilot.workflow import Item, new_batch


def test_post_process_report_does_not_count_pending_pdf_as_full_success():
    batch = new_batch(
        library_id="1",
        query="q",
        phase="post_process_verified",
        items=(
            Item(
                identifier="10.1000/a",
                doc_id="W1",
                source_url="https://example.com/a",
                title="Paper A",
                status="saved",
                pdf_present=None,
                pdf_verification_status="pending",
                indexed=True,
                noted=True,
                tagged=True,
                classified=True,
            ),
        ),
    )

    report = rw._build_post_process_report(batch)
    assert report["full_success_count"] == 0
    assert report["partial_count"] == 1
    assert report["items"][0]["outcome"] == "partial"


def test_get_batch_status_after_done_preserves_verified_final_report(monkeypatch, tmp_path):
    from pathlib import Path

    from zotpilot.tools import research_workflow as rw_local
    from zotpilot.workflow import BatchStore

    rw_local._batch_store = BatchStore(Path(tmp_path) / "batches")
    batch = new_batch(
        library_id="1",
        query="q",
        phase="post_process_verified",
        items=(
            Item(
                identifier="10.1000/a",
                doc_id="W1",
                source_url="https://example.com/a",
                title="Paper A",
                status="saved",
                pdf_present=True,
                pdf_verification_status="present",
                indexed=True,
                noted=True,
                tagged=True,
                classified=True,
                zotero_item_key="I1",
            ),
        ),
    )
    final_report = rw_local._build_post_process_report(batch)
    done = rw_local.Batch.from_dict(
        batch.transition_to("done").to_dict() | {"final_report": final_report, "legacy_ingest_batch_id": "eng_1"}
    )
    rw_local._batch_store.save(done)

    monkeypatch.setattr(
        rw_local.ingestion,
        "get_ingest_status_impl",
        lambda batch_id: {
            "batch_id": batch_id,
            "is_final": True,
            "results": [
                {
                    "index": 0,
                    "status": "saved",
                    "item_key": "I1",
                    "has_pdf": True,
                    "pdf_verification_status": "present",
                }
            ],
        },
    )

    status = rw_local.get_batch_status(done.batch_id)
    assert status["phase"] == "done"
    assert status["final_report"]["full_success_count"] == 1
    assert status["final_report"]["items"][0]["outcome"] == "full_success"

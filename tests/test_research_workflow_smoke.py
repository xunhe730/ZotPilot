"""Smoke tests for the batch-centric research workflow tools."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_research_workflow_happy_path_smoke(monkeypatch, tmp_path: Path):
    from zotpilot.tools import research_workflow as rw
    from zotpilot.workflow import BatchStore, PreflightResult

    rw._batch_store = BatchStore(tmp_path / "batches")

    monkeypatch.setattr(
        rw.ingestion,
        "search_academic_databases_impl",
        lambda **kwargs: [
            {
                "doi": "10.1000/a",
                "title": "Paper A",
                "landing_page_url": "https://example.com/a",
            }
        ],
    )

    def fake_preflight(batch, *, round_number):
        result = PreflightResult(round=round_number, checked_at=0.0, blocking_decisions=(), all_clear=True)
        if batch.phase == "candidates_confirmed":
            batch = batch.transition_to("preflighting")
        return batch.with_preflight_result(result), {"all_clear": True}

    monkeypatch.setattr(rw, "_build_preflight_result", fake_preflight)
    def fake_ingest_worker(store, batch_id: str):
        batch = rw._batch_store.load(batch_id)
        assert batch is not None
        items = [
            item.with_updates(
                status="saved",
                zotero_item_key="I1",
                pdf_present=True,
                metadata_complete=True,
                routing_method="api",
            )
            for item in batch.items
        ]
        batch = batch.with_items(items).transition_to("ingesting").transition_to("post_ingest_verified")
        rw._batch_store.save(batch)

    def fake_post_process_worker(store, batch_id: str):
        batch = rw._batch_store.load(batch_id)
        assert batch is not None
        items = [item.with_updates(indexed=True, tagged=True, classified=True) for item in batch.items]
        batch = batch.with_items(items).transition_to("post_processing").transition_to("post_process_verified")
        batch = rw.Batch.from_dict(batch.to_dict() | {"final_report": rw._build_post_process_report(batch)})
        rw._batch_store.save(batch)

    monkeypatch.setattr(rw, "start_ingest_worker", fake_ingest_worker)
    monkeypatch.setattr(rw, "start_post_process_worker", fake_post_process_worker)

    search = rw.search_academic_databases("attention")
    batch_id = search["batch_id"]
    confirm = rw.confirm_candidates(batch_id, [search["candidates"][0]["doc_id"]])
    assert confirm["next_action"]["tool"] == "approve_ingest"

    approved = rw.approve_ingest(batch_id)
    assert approved["next_action"]["tool"] == "get_batch_status"

    after_ingest = rw.get_batch_status(batch_id)
    assert after_ingest["phase"] == "post_ingest_verified"

    post_ingest = rw.approve_post_ingest(batch_id)
    assert post_ingest["next_action"]["tool"] == "get_batch_status"

    status = rw.get_batch_status(batch_id)
    assert status["phase"] == "post_process_verified"

    final = rw.approve_post_process(batch_id)
    assert final["phase"] == "done"
    assert final["next_action"] is None


def test_reindex_degraded_rejects_ineligible_items(monkeypatch, tmp_path: Path):
    from zotpilot.tools import research_workflow as rw
    from zotpilot.workflow import BatchStore, Item, new_batch

    rw._batch_store = BatchStore(tmp_path / "batches")
    batch = new_batch(
        library_id="1",
        query="direct",
        phase="done",
        items=(
            Item(
                identifier="10.1000/a",
                doc_id="10.1000/a",
                source_url="https://example.com/a",
                status="degraded",
                zotero_item_key="I1",
                degradation_reasons=("no_pdf",),
            ),
        ),
    )
    rw._batch_store.save(batch)

    with pytest.raises(Exception):
        rw.reindex_degraded(batch.batch_id, ["I1"], "manual_retry")


def test_get_batch_status_refreshes_pending_pdf_from_legacy_engine(monkeypatch, tmp_path: Path):
    from zotpilot.tools import research_workflow as rw
    from zotpilot.workflow import BatchStore, Item, new_batch

    rw._batch_store = BatchStore(tmp_path / "batches")
    batch = new_batch(
        library_id="1",
        query="direct",
        phase="post_ingest_verified",
        items=(
            Item(
                identifier="10.1000/a",
                doc_id="10.1000/a",
                source_url="https://example.com/a",
                status="saved",
                zotero_item_key="I1",
                pdf_verification_status="pending",
                reason_code="connector_save_pending_pdf",
            ),
        ),
    )
    batch = batch.with_engine_batch_id("eng_1")
    rw._batch_store.save(batch)

    monkeypatch.setattr(
        rw.ingestion,
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
                    "save_method_used": "connector_primary",
                    "route_selected": "connector_primary",
                }
            ],
        },
    )

    status = rw.get_batch_status(batch.batch_id)
    assert status["phase"] == "post_ingest_verified"
    assert status["items"][0]["pdf_verification_status"] == "present"
    assert status["items"][0]["pdf_present"] is True

    persisted = rw._batch_store.load(batch.batch_id)
    assert persisted is not None
    assert persisted.phase == "post_ingest_verified"
    assert persisted.items[0].pdf_verification_status == "present"
    assert persisted.items[0].reason_code is None


def test_get_batch_status_keeps_ingesting_until_legacy_batch_is_final(monkeypatch, tmp_path: Path):
    from zotpilot.tools import research_workflow as rw
    from zotpilot.workflow import BatchStore, Item, new_batch

    rw._batch_store = BatchStore(tmp_path / "batches")
    batch = new_batch(
        library_id="1",
        query="direct",
        phase="ingesting",
        items=(
            Item(
                identifier="10.1000/a",
                doc_id="10.1000/a",
                source_url="https://example.com/a",
                status="pending",
            ),
        ),
    )
    batch = batch.with_engine_batch_id("eng_1")
    rw._batch_store.save(batch)

    monkeypatch.setattr(
        rw.ingestion,
        "get_ingest_status_impl",
        lambda batch_id: {
            "batch_id": batch_id,
            "is_final": False,
            "results": [
                {
                    "index": 0,
                    "status": "saved",
                    "item_key": "I1",
                    "save_method_used": "connector_primary",
                    "route_selected": "connector_primary",
                }
            ],
        },
    )

    status = rw.get_batch_status(batch.batch_id)
    assert status["phase"] == "ingesting"

    persisted = rw._batch_store.load(batch.batch_id)
    assert persisted is not None
    assert persisted.phase == "ingesting"

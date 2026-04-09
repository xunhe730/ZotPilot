"""Mock E2E gates for the batch-centric research workflow."""

from __future__ import annotations

from pathlib import Path

import pytest


def _paper(doc_id: str, *, doi: str, title: str, url: str) -> dict:
    return {
        "openalex_id": doc_id,
        "doi": doi,
        "title": title,
        "landing_page_url": url,
    }


def test_mock_e2e_canonical_flow_reaches_done_with_truthful_item_fields(monkeypatch, tmp_path: Path):
    from zotpilot.tools import research_workflow as rw
    from zotpilot.workflow import BatchStore, PreflightResult

    rw._batch_store = BatchStore(tmp_path / "batches")

    monkeypatch.setattr(
        rw.ingestion,
        "search_academic_databases_impl",
        lambda **kwargs: [
            _paper("W1", doi="10.1000/a", title="Paper A", url="https://example.com/a"),
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
                routing_method="connector",
                route_selected="connector_primary",
                save_method_used="connector_primary",
                item_discovery_status="known_item_key",
                pdf_verification_status="present",
                reason_code=None,
            )
            for item in batch.items
        ]
        batch = batch.with_items(items).transition_to("ingesting").transition_to("post_ingest_verified")
        rw._batch_store.save(batch)

    def fake_post_process_worker(store, batch_id: str):
        batch = rw._batch_store.load(batch_id)
        assert batch is not None
        items = [
            item.with_updates(indexed=True, noted=False, tagged=False, classified=False)
            for item in batch.items
        ]
        batch = batch.with_items(items).transition_to("post_processing").transition_to("post_process_verified")
        rw._batch_store.save(batch)

    monkeypatch.setattr(rw, "start_ingest_worker", fake_ingest_worker)
    monkeypatch.setattr(rw, "start_post_process_worker", fake_post_process_worker)

    search = rw.search_academic_databases("attention")
    batch_id = search["batch_id"]
    assert search["next_action"]["tool"] == "confirm_candidates"

    confirm = rw.confirm_candidates(batch_id, [search["candidates"][0]["doc_id"]])
    assert confirm["next_action"]["tool"] == "approve_ingest"

    approved = rw.approve_ingest(batch_id)
    assert approved["next_action"]["tool"] == "get_batch_status"

    after_ingest = rw.get_batch_status(batch_id)
    assert after_ingest["phase"] == "post_ingest_verified"
    item = after_ingest["items"][0]
    assert item["route_selected"] == "connector_primary"
    assert item["save_method_used"] == "connector_primary"
    assert item["item_discovery_status"] == "known_item_key"
    assert item["pdf_verification_status"] == "present"
    assert item["reason_code"] is None

    post_ingest = rw.approve_post_ingest(batch_id)
    assert post_ingest["next_action"]["tool"] == "get_batch_status"

    post_status = rw.get_batch_status(batch_id)
    assert post_status["phase"] == "post_process_verified"
    assert post_status["next_action"]["tool"] == "approve_post_process"

    final = rw.approve_post_process(batch_id)
    assert final["phase"] == "done"
    assert final["final_report"]["full_success_count"] == 0
    assert final["final_report"]["partial_count"] == 1
    assert final["final_report"]["items"][0]["missing_steps"] == ["note", "classify", "tag"]


def test_mock_e2e_post_process_is_not_reported_complete_until_worker_advances_phase(monkeypatch, tmp_path: Path):
    from zotpilot.tools import research_workflow as rw
    from zotpilot.workflow import BatchStore, PreflightResult

    rw._batch_store = BatchStore(tmp_path / "batches")

    monkeypatch.setattr(
        rw.ingestion,
        "search_academic_databases_impl",
        lambda **kwargs: [
            _paper("W1", doi="10.1000/a", title="Paper A", url="https://example.com/a"),
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
                routing_method="connector",
            )
            for item in batch.items
        ]
        batch = batch.with_items(items).transition_to("ingesting").transition_to("post_ingest_verified")
        rw._batch_store.save(batch)

    def fake_post_process_worker(store, batch_id: str):
        batch = rw._batch_store.load(batch_id)
        assert batch is not None
        batch = batch.transition_to("post_processing")
        rw._batch_store.save(batch)

    monkeypatch.setattr(rw, "start_ingest_worker", fake_ingest_worker)
    monkeypatch.setattr(rw, "start_post_process_worker", fake_post_process_worker)

    batch_id = rw.search_academic_databases("attention")["batch_id"]
    rw.confirm_candidates(batch_id, ["W1"])
    rw.approve_ingest(batch_id)
    rw.approve_post_ingest(batch_id)

    status = rw.get_batch_status(batch_id)
    assert status["phase"] == "post_processing"
    with pytest.raises(Exception):
        rw.approve_post_process(batch_id)

    batch = rw._batch_store.load(batch_id)
    assert batch is not None
    completed = batch.with_items(
        [item.with_updates(indexed=True, noted=True) for item in batch.items]
    ).transition_to("post_process_verified")
    rw._batch_store.save(completed)

    final = rw.approve_post_process(batch_id)
    assert final["phase"] == "done"


def test_mock_e2e_partial_success_keeps_batch_truthful_and_finalizable(monkeypatch, tmp_path: Path):
    from zotpilot.tools import research_workflow as rw
    from zotpilot.workflow import BatchStore, PreflightResult

    rw._batch_store = BatchStore(tmp_path / "batches")

    monkeypatch.setattr(
        rw.ingestion,
        "search_academic_databases_impl",
        lambda **kwargs: [
            _paper("W1", doi="10.1000/a", title="Paper A", url="https://example.com/a"),
            _paper("W2", doi="10.1000/b", title="Paper B", url="https://example.com/b"),
            _paper("W3", doi="10.1000/c", title="Paper C", url="https://example.com/c"),
            _paper("W4", doi="10.1000/d", title="Paper D", url="https://example.com/d"),
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
            batch.items[0].with_updates(
                status="saved",
                zotero_item_key="I1",
                pdf_present=True,
                metadata_complete=True,
                pdf_verification_status="present",
            ),
            batch.items[1].with_updates(
                status="degraded",
                zotero_item_key="I2",
                pdf_present=False,
                metadata_complete=True,
                pdf_verification_status="missing",
                degradation_reasons=("no_pdf",),
            ),
            batch.items[2].with_updates(status="failed"),
            batch.items[3].with_updates(status="duplicate", zotero_item_key="I4"),
        ]
        batch = batch.with_items(items).transition_to("ingesting").transition_to("post_ingest_verified")
        rw._batch_store.save(batch)

    def fake_post_process_worker(store, batch_id: str):
        batch = rw._batch_store.load(batch_id)
        assert batch is not None
        items = [
            batch.items[0].with_updates(indexed=True, noted=True),
            batch.items[1].with_updates(indexed=False, noted=False, status="degraded"),
            batch.items[2],
            batch.items[3],
        ]
        batch = batch.with_items(items).transition_to("post_processing").transition_to("post_process_verified")
        rw._batch_store.save(batch)

    monkeypatch.setattr(rw, "start_ingest_worker", fake_ingest_worker)
    monkeypatch.setattr(rw, "start_post_process_worker", fake_post_process_worker)

    search = rw.search_academic_databases("attention")
    batch_id = search["batch_id"]
    rw.confirm_candidates(batch_id, [candidate["doc_id"] for candidate in search["candidates"]])
    rw.approve_ingest(batch_id)

    after_ingest = rw.get_batch_status(batch_id)
    assert after_ingest["summary"]["saved"] == 1
    assert after_ingest["summary"]["degraded"] == 1
    assert after_ingest["summary"]["failed"] == 1
    assert after_ingest["summary"]["duplicate"] == 1
    assert after_ingest["summary"]["pdf_present_count"] == 1

    rw.approve_post_ingest(batch_id)
    final = rw.approve_post_process(batch_id)
    report = final["final_report"]
    assert report["full_success_count"] == 0
    assert report["partial_count"] == 2
    assert report["reindex_eligible"] == []
    assert {item["status"] for item in report["items"]} == {"saved", "degraded", "failed", "duplicate"}

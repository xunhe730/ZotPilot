"""Truthfulness checks for the actual post-process worker implementation."""

from __future__ import annotations

from pathlib import Path
from time import time
from unittest.mock import MagicMock, patch

from zotpilot.workflow.batch import Batch, Item, new_batch_id
from zotpilot.workflow.batch_store import BatchStore
from zotpilot.workflow.worker import _run_post_process_worker


def test_post_process_worker_does_not_infer_tagging_or_classification(tmp_path: Path) -> None:
    batch = Batch(
        batch_id=new_batch_id(),
        library_id="lib_test",
        query="test",
        phase="post_ingest_approved",
        items=(
            Item(
                identifier="10.1000/a",
                doc_id="W1",
                source_url="https://example.com/a",
                title="Paper A",
                status="saved",
                pdf_present=True,
                metadata_complete=True,
                zotero_item_key="I1",
            ),
        ),
        preflight_result=None,
        authorized_new_tags=frozenset(),
        authorized_new_collections=frozenset(),
        created_at=time(),
        last_transition_at=time(),
    )
    store = BatchStore(base_dir=tmp_path)
    store.save(batch)

    zotero = MagicMock()
    zotero.library_id = "lib_test"
    zotero.get_notes.return_value = []
    zotero.get_item.return_value = MagicMock(tags="existing-tag")
    zotero.get_item_collections.return_value = [{"key": "C1", "name": "Existing Collection"}]

    with (
        patch("zotpilot.workflow.worker._get_zotero", return_value=zotero),
        patch(
            "zotpilot.workflow.worker._index_item",
            side_effect=lambda item: item.with_updates(indexed=True),
        ),
    ):
        _run_post_process_worker(store, batch.batch_id)

    reloaded = store.load(batch.batch_id)
    assert reloaded is not None
    assert reloaded.phase == "post_process_verified"
    item = reloaded.items[0]
    assert item.indexed is True
    assert item.noted is False
    assert item.tagged is False
    assert item.classified is False

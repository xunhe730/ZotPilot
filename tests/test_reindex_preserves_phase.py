"""P16: reindex must not change batch.phase or authorizations."""

from __future__ import annotations

from pathlib import Path
from time import time
from unittest.mock import patch

from zotpilot.workflow.batch import Batch, Item
from zotpilot.workflow.batch_store import BatchStore
from zotpilot.workflow.worker import reindex_items


def _done_batch_with_eligible_item(tmp_path: Path) -> tuple[BatchStore, Batch]:
    from zotpilot.workflow.batch import new_batch_id

    item = Item(
        identifier="doc1",
        doc_id="doc1",
        source_url="",
        status="degraded",
        pdf_present=False,
        metadata_complete=True,
        zotero_item_key="ZK001",
        degradation_reasons=("embedding_api_unavailable",),
    )
    batch = Batch(
        batch_id=new_batch_id(),
        library_id="lib_test",
        query="test",
        phase="done",
        items=(item,),
        preflight_result=None,
        authorized_new_tags=frozenset(),
        authorized_new_collections=frozenset(),
        created_at=time(),
        last_transition_at=time(),
    )
    store = BatchStore(base_dir=tmp_path)
    store.save(batch)
    return store, batch


def test_reindex_preserves_done_phase(tmp_path: Path) -> None:
    store, batch = _done_batch_with_eligible_item(tmp_path)
    assert batch.phase == "done"

    with patch("zotpilot.workflow.worker._get_zotero") as mock_zotero:
        mock_zotero.return_value.library_id = "lib_test"
        with patch(
            "zotpilot.workflow.worker._index_item",
            side_effect=lambda it: it.with_updates(status="saved", indexed=True),
        ):
            updated_batch, _, _ = reindex_items(store, batch.batch_id, ["ZK001"])

    assert updated_batch.phase == "done"


def test_reindex_does_not_add_authorized_tags(tmp_path: Path) -> None:
    store, batch = _done_batch_with_eligible_item(tmp_path)
    assert batch.authorized_new_tags == frozenset()

    with patch("zotpilot.workflow.worker._get_zotero") as mock_zotero:
        mock_zotero.return_value.library_id = "lib_test"
        with patch(
            "zotpilot.workflow.worker._index_item",
            side_effect=lambda it: it.with_updates(status="saved", indexed=True),
        ):
            updated_batch, _, _ = reindex_items(store, batch.batch_id, ["ZK001"])

    assert updated_batch.authorized_new_tags == frozenset()


def test_reindex_does_not_add_authorized_collections(tmp_path: Path) -> None:
    store, batch = _done_batch_with_eligible_item(tmp_path)
    assert batch.authorized_new_collections == frozenset()

    with patch("zotpilot.workflow.worker._get_zotero") as mock_zotero:
        mock_zotero.return_value.library_id = "lib_test"
        with patch(
            "zotpilot.workflow.worker._index_item",
            side_effect=lambda it: it.with_updates(status="saved", indexed=True),
        ):
            updated_batch, _, _ = reindex_items(store, batch.batch_id, ["ZK001"])

    assert updated_batch.authorized_new_collections == frozenset()

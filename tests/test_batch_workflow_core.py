from __future__ import annotations

import json

import pytest

from zotpilot.workflow import BatchStore, IllegalPhaseTransition, Item, new_batch


def _item(doc_id: str) -> Item:
    return Item(identifier=doc_id, doc_id=doc_id, source_url=f"https://example.com/{doc_id}")


def test_batch_transition_matrix_rejects_illegal_jump():
    batch = new_batch(library_id="1", query="q", phase="candidate", items=[_item("a")])

    with pytest.raises(IllegalPhaseTransition):
        batch.transition_to("approved")


def test_batch_store_filters_active_batches(tmp_path):
    store = BatchStore(tmp_path / "batches")
    active = new_batch(library_id="1", query="q", phase="candidate", items=[_item("a")])
    done = new_batch(
        library_id="1", query="q2", phase="post_process_verified", items=[_item("b")]
    ).transition_to("done")

    store.save(active)
    store.save(done)

    listed = store.list_active(library_id="1")
    assert [batch.batch_id for batch in listed] == [active.batch_id]


def test_batch_roundtrip_preserves_engine_fields(tmp_path):
    store = BatchStore(tmp_path / "batches")
    batch = new_batch(library_id="1", query="q", phase="approved", items=[_item("a")])
    batch = batch.with_engine_batch_id("legacy123")

    store.save(batch)
    loaded = store.load(batch.batch_id)

    assert loaded is not None
    assert loaded.engine_batch_id == "legacy123"
    assert json.dumps(loaded.to_dict())

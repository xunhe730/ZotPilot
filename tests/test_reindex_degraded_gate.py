"""P16: reindex_degraded gate — phase, item key, and reason validation."""

from __future__ import annotations

from pathlib import Path
from time import time

import pytest

from zotpilot.workflow.batch import (
    REINDEX_ELIGIBLE_REASONS,
    Batch,
    InvalidPhaseError,
    Item,
)
from zotpilot.workflow.batch_store import BatchStore


def _item(
    doc_id: str = "doc1",
    *,
    zotero_key: str = "ZK001",
    status: str = "saved",
    degradation_reasons: tuple[str, ...] = (),
    pdf_present: bool = True,
) -> Item:
    return Item(
        identifier=doc_id,
        doc_id=doc_id,
        source_url="",
        status=status,  # type: ignore[arg-type]
        pdf_present=pdf_present,
        metadata_complete=True,
        zotero_item_key=zotero_key,
        degradation_reasons=degradation_reasons,
    )


def _batch(phase: str, items: tuple[Item, ...] = ()) -> Batch:
    from zotpilot.workflow.batch import new_batch_id

    return Batch(
        batch_id=new_batch_id(),
        library_id="lib_test",
        query="test",
        phase=phase,  # type: ignore[arg-type]
        items=items,
        preflight_result=None,
        authorized_new_tags=frozenset(),
        authorized_new_collections=frozenset(),
        created_at=time(),
        last_transition_at=time(),
    )


NON_DONE_PHASES = [
    "candidate",
    "candidates_confirmed",
    "preflighting",
    "preflight_blocked",
    "approved",
    "ingesting",
    "post_ingest_verified",
    "post_ingest_approved",
    "post_processing",
    "AwaitingTaxonomyAuthorization",
    "taxonomy_authorized",
    "post_ingest_skipped",
    "post_process_verified",
]


@pytest.mark.parametrize("phase", NON_DONE_PHASES)
def test_reindex_on_non_done_phase_raises(phase: str, tmp_path: Path) -> None:
    """reindex_items must reject batches not in 'done' phase."""
    from unittest.mock import patch

    from zotpilot.workflow.worker import reindex_items

    store = BatchStore(base_dir=tmp_path)
    batch = _batch(phase, items=(_item(),))
    store.save(batch)

    with patch("zotpilot.workflow.worker._get_zotero") as mock_zotero:
        mock_zotero.return_value.library_id = "lib_test"
        with pytest.raises((InvalidPhaseError, ValueError, KeyError)):
            reindex_items(store, batch.batch_id, ["ZK001"])


def test_reindex_on_done_phase_succeeds(tmp_path: Path) -> None:
    """reindex_items on a done batch with eligible item succeeds (index call
    will fail internally but the phase guard passes)."""
    from unittest.mock import patch

    from zotpilot.workflow.worker import reindex_items

    eligible_item = _item(
        doc_id="doc1",
        zotero_key="ZK001",
        status="degraded",
        degradation_reasons=("embedding_api_unavailable",),
        pdf_present=False,
    )
    store = BatchStore(base_dir=tmp_path)
    batch = _batch("done", items=(eligible_item,))
    store.save(batch)

    # Patch _get_zotero so library binding check passes
    with patch("zotpilot.workflow.worker._get_zotero") as mock_zotero:
        mock_zotero.return_value.library_id = "lib_test"
        # Patch _index_item to avoid real indexing
        with patch(
            "zotpilot.workflow.worker._index_item",
            side_effect=lambda it: it.with_updates(status="saved", indexed=True),
        ):
            updated_batch, reindexed, still_degraded = reindex_items(store, batch.batch_id, ["ZK001"])

    assert updated_batch is not None


def test_reindex_unknown_item_key_is_silently_skipped(tmp_path: Path) -> None:
    """item_keys not present in the batch are silently skipped (not an error).

    The reindex_items function only raises if a found item is not eligible;
    unknown keys are simply not matched and skipped. This test documents
    that behaviour so that a future change to raise on unknown keys is
    caught as a deliberate regression.
    """
    from unittest.mock import patch

    from zotpilot.workflow.worker import reindex_items

    store = BatchStore(base_dir=tmp_path)
    batch = _batch("done", items=(
        _item(doc_id="doc1", zotero_key="ZK001", status="degraded",
              degradation_reasons=("embedding_api_unavailable",)),
    ))
    store.save(batch)

    with patch("zotpilot.workflow.worker._get_zotero") as mock_zotero:
        mock_zotero.return_value.library_id = "lib_test"
        updated_batch, reindexed, still_degraded = reindex_items(store, batch.batch_id, ["UNKNOWN_KEY"])

    # Unknown key → nothing reindexed, nothing still_degraded
    assert reindexed == []
    assert still_degraded == []


@pytest.mark.parametrize("bad_reason", ["no_pdf", "incomplete_metadata", "anti_bot_blocked"])
def test_reindex_non_eligible_reason_raises(bad_reason: str, tmp_path: Path) -> None:
    """Items with non-reindex-eligible degradation reasons must be rejected."""
    from unittest.mock import patch

    from zotpilot.workflow.worker import reindex_items

    bad_item = _item(
        doc_id="doc1",
        zotero_key="ZK001",
        status="degraded",
        degradation_reasons=(bad_reason,),
    )
    store = BatchStore(base_dir=tmp_path)
    batch = _batch("done", items=(bad_item,))
    store.save(batch)

    with patch("zotpilot.workflow.worker._get_zotero") as mock_zotero:
        mock_zotero.return_value.library_id = "lib_test"
        with pytest.raises((ValueError, KeyError)):
            reindex_items(store, batch.batch_id, ["ZK001"])


def test_reindex_eligible_reasons_constant_contains_required_set() -> None:
    """Spec §8.3 mandates these four reasons are in REINDEX_ELIGIBLE_REASONS."""
    required = {
        "embedding_api_unavailable",
        "embedding_api_rate_limit",
        "index_write_failed",
        "chromadb_transient_error",
    }
    assert required <= REINDEX_ELIGIBLE_REASONS

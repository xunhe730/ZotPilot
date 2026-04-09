"""P5: Taxonomy gate — unauthorized tag/collection creation must raise."""

from __future__ import annotations

from time import time

import pytest

from zotpilot.workflow.batch import (
    Batch,
    UnauthorizedTaxonomyChange,
)


def _batch_with_auth(
    authorized_tags: frozenset[str] = frozenset(),
    authorized_collections: frozenset[str] = frozenset(),
) -> Batch:
    from zotpilot.workflow.batch import new_batch_id

    return Batch(
        batch_id=new_batch_id(),
        library_id="lib_test",
        query="test",
        phase="post_processing",
        items=(),
        preflight_result=None,
        authorized_new_tags=authorized_tags,
        authorized_new_collections=authorized_collections,
        created_at=time(),
        last_transition_at=time(),
    )


def _taxonomy_gate_create_tag(batch: Batch, tag_name: str) -> None:
    """Simulate the taxonomy gate check for tag creation.

    The gate logic: if a tag name is not in batch.authorized_new_tags, raise
    UnauthorizedTaxonomyChange.  This mirrors how the worker/tool layer should
    guard ZoteroWriter.create_tag calls.
    """
    if tag_name not in batch.authorized_new_tags:
        raise UnauthorizedTaxonomyChange(
            f"Tag {tag_name!r} is not in the authorized list for batch {batch.batch_id}"
        )


def _taxonomy_gate_create_collection(batch: Batch, collection_name: str) -> None:
    """Same gate for collections."""
    if collection_name not in batch.authorized_new_collections:
        raise UnauthorizedTaxonomyChange(
            f"Collection {collection_name!r} is not in the authorized list for batch {batch.batch_id}"
        )


# ---------------------------------------------------------------------------
# Tag gate tests
# ---------------------------------------------------------------------------

def test_unauthorized_tag_raises() -> None:
    batch = _batch_with_auth(authorized_tags=frozenset({"machine-learning"}))
    with pytest.raises(UnauthorizedTaxonomyChange):
        _taxonomy_gate_create_tag(batch, "new-unauthorized-tag")


def test_authorized_tag_succeeds() -> None:
    batch = _batch_with_auth(authorized_tags=frozenset({"machine-learning", "nlp"}))
    # Should not raise
    _taxonomy_gate_create_tag(batch, "machine-learning")
    _taxonomy_gate_create_tag(batch, "nlp")


def test_empty_authorization_rejects_any_tag() -> None:
    batch = _batch_with_auth()
    with pytest.raises(UnauthorizedTaxonomyChange):
        _taxonomy_gate_create_tag(batch, "any-tag")


# ---------------------------------------------------------------------------
# Collection gate tests
# ---------------------------------------------------------------------------

def test_unauthorized_collection_raises() -> None:
    batch = _batch_with_auth(authorized_collections=frozenset({"AI Papers"}))
    with pytest.raises(UnauthorizedTaxonomyChange):
        _taxonomy_gate_create_collection(batch, "New Unauthorized Collection")


def test_authorized_collection_succeeds() -> None:
    batch = _batch_with_auth(authorized_collections=frozenset({"AI Papers", "ML Surveys"}))
    _taxonomy_gate_create_collection(batch, "AI Papers")
    _taxonomy_gate_create_collection(batch, "ML Surveys")


def test_empty_authorization_rejects_any_collection() -> None:
    batch = _batch_with_auth()
    with pytest.raises(UnauthorizedTaxonomyChange):
        _taxonomy_gate_create_collection(batch, "any-collection")


# ---------------------------------------------------------------------------
# has_authorized_tags / has_authorized_collections helpers on Batch
# ---------------------------------------------------------------------------

def test_batch_has_authorized_tags_helper() -> None:
    batch = _batch_with_auth(authorized_tags=frozenset({"tag-a", "tag-b"}))
    assert batch.has_authorized_tags(["tag-a"]) is True
    assert batch.has_authorized_tags(["tag-a", "tag-b"]) is True
    assert batch.has_authorized_tags(["tag-a", "tag-c"]) is False
    assert batch.has_authorized_tags([]) is True


def test_batch_has_authorized_collections_helper() -> None:
    batch = _batch_with_auth(authorized_collections=frozenset({"col-a"}))
    assert batch.has_authorized_collections(["col-a"]) is True
    assert batch.has_authorized_collections(["col-b"]) is False

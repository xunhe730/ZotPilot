"""Tests for batch write operations (merged batch_tags + batch_collections)."""
from unittest.mock import MagicMock, patch

import pytest

from zotpilot.tools.write_ops import (
    _BATCH_MAX,
    batch_collections,
    batch_tags,
)


@pytest.fixture
def mock_writer():
    writer = MagicMock()
    with patch("zotpilot.tools.write_ops._get_writer", return_value=writer):
        yield writer


@pytest.fixture
def mock_zotero():
    zotero = MagicMock()
    zotero.get_all_tags.return_value = [
        {"name": "ml", "count": 3},
        {"name": "dl", "count": 2},
        {"name": "nlp", "count": 1},
        {"name": "old", "count": 1},
        {"name": "new", "count": 1},
        {"name": "t", "count": 1},
    ]
    with patch("zotpilot.tools.write_ops._get_zotero", return_value=zotero):
        yield zotero


class TestBatchTags:
    def test_add_happy(self, mock_writer, mock_zotero):
        items = [
            {"item_key": "A", "tags": ["ml"]},
            {"item_key": "B", "tags": ["dl"]},
            {"item_key": "C", "tags": ["nlp"]},
        ]
        result = batch_tags(action="add", items=items)
        assert result["total"] == 3
        assert result["succeeded"] == 3
        assert result["failed"] == 0
        assert mock_writer.add_item_tags.call_count == 3

    def test_set_happy(self, mock_writer):
        items = [{"item_key": "A", "tags": ["new"]}]
        result = batch_tags(action="set", items=items)
        assert result["succeeded"] == 1
        mock_writer.set_item_tags.assert_called_once_with("A", ["new"])

    def test_remove_happy(self, mock_writer):
        items = [{"item_key": "A", "tags": ["old"]}]
        result = batch_tags(action="remove", items=items)
        assert result["succeeded"] == 1
        mock_writer.remove_item_tags.assert_called_once_with("A", ["old"])

    def test_partial_fail(self, mock_writer, mock_zotero):
        mock_writer.add_item_tags.side_effect = [None, Exception("API error")]
        items = [
            {"item_key": "A", "tags": ["ml"]},
            {"item_key": "B", "tags": ["dl"]},
        ]
        result = batch_tags(action="add", items=items)
        assert result["succeeded"] == 1
        assert result["failed"] == 1
        assert result["results"][1]["error"] == "API error"

    def test_empty(self, mock_writer, mock_zotero):
        result = batch_tags(action="add", items=[])
        assert result["total"] == 0
        assert result["succeeded"] == 0

    def test_over_limit(self, mock_writer, mock_zotero):
        from zotpilot.state import ToolError
        items = [{"item_key": f"K{i}", "tags": ["t"]} for i in range(_BATCH_MAX + 1)]
        with pytest.raises(ToolError, match="exceeds limit"):
            batch_tags(action="add", items=items)

    def test_missing_field(self, mock_writer, mock_zotero):
        items = [{"item_key": "A"}]  # missing tags
        result = batch_tags(action="add", items=items)
        assert result["results"][0]["success"] is False
        assert "Missing" in result["results"][0]["error"]


class TestBatchCollections:
    def test_add_happy(self, mock_writer):
        result = batch_collections(action="add", item_keys=["A", "B"], collection_key="COL1")
        assert result["total"] == 2
        assert result["succeeded"] == 2
        assert mock_writer.add_to_collection.call_count == 2

    def test_remove_happy(self, mock_writer):
        result = batch_collections(action="remove", item_keys=["A", "B"], collection_key="COL1")
        assert result["total"] == 2
        assert result["succeeded"] == 2
        assert mock_writer.remove_from_collection.call_count == 2

    def test_over_limit(self, mock_writer):
        from zotpilot.state import ToolError
        keys = [f"K{i}" for i in range(_BATCH_MAX + 1)]
        with pytest.raises(ToolError, match="exceeds limit"):
            batch_collections(action="add", item_keys=keys, collection_key="COL1")

    def test_add_invalidates_cache(self, mock_writer):
        with patch("zotpilot.tools.write_ops._invalidate_collection_cache") as mock_inv:
            batch_collections(action="add", item_keys=["A"], collection_key="COL1")
            assert mock_inv.call_count >= 1  # called per-item via _add_to_collection_impl (add + inbox cleanup)

    def test_remove_invalidates_cache(self, mock_writer):
        with patch("zotpilot.tools.write_ops._invalidate_collection_cache") as mock_inv:
            batch_collections(action="remove", item_keys=["A"], collection_key="COL1")
            mock_inv.assert_called_once()

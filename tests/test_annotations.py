"""Tests for get_annotations tool."""
from unittest.mock import MagicMock, patch

import pytest

from zotpilot.zotero_api_reader import ZoteroApiReader


class TestZoteroApiReader:
    @patch("zotpilot.zotero_api_reader.zotero.Zotero")
    def test_get_annotations_by_item(self, mock_zot_cls):
        mock_zot = MagicMock()
        mock_zot.children.return_value = [
            {
                "data": {
                    "key": "ANN1",
                    "itemType": "annotation",
                    "parentItem": "ITEM1",
                    "annotationType": "highlight",
                    "annotationText": "important finding",
                    "annotationComment": "check this",
                    "annotationColor": "#ffff00",
                    "annotationPageLabel": "5",
                    "tags": [{"tag": "key-finding"}],
                }
            },
            {
                "data": {
                    "key": "ATT1",
                    "itemType": "attachment",  # not an annotation
                }
            },
        ]
        mock_zot_cls.return_value = mock_zot

        reader = ZoteroApiReader("key", "123")
        results = reader.get_annotations(item_key="ITEM1")

        assert len(results) == 1
        assert results[0]["key"] == "ANN1"
        assert results[0]["type"] == "highlight"
        assert results[0]["text"] == "important finding"
        assert results[0]["comment"] == "check this"
        assert results[0]["color"] == "#ffff00"
        assert results[0]["page"] == "5"
        assert results[0]["tags"] == ["key-finding"]

    @patch("zotpilot.zotero_api_reader.zotero.Zotero")
    def test_get_annotations_all(self, mock_zot_cls):
        mock_zot = MagicMock()
        mock_zot.items.return_value = [
            {"data": {"key": "A1", "itemType": "annotation", "parentItem": "P1",
                      "annotationType": "note", "annotationText": "", "annotationComment": "my note",
                      "annotationColor": "", "annotationPageLabel": "", "tags": []}},
        ]
        mock_zot_cls.return_value = mock_zot

        reader = ZoteroApiReader("key", "123")
        results = reader.get_annotations()
        assert len(results) == 1
        assert results[0]["type"] == "note"

    @patch("zotpilot.zotero_api_reader.zotero.Zotero")
    def test_get_annotations_empty(self, mock_zot_cls):
        mock_zot = MagicMock()
        mock_zot.children.return_value = []
        mock_zot_cls.return_value = mock_zot

        reader = ZoteroApiReader("key", "123")
        results = reader.get_annotations(item_key="ITEM1")
        assert results == []


class TestGetAnnotationsTool:
    @patch("zotpilot.tools.library._get_api_reader")
    def test_get_annotations_tool(self, mock_reader):
        mock_api = MagicMock()
        mock_api.get_annotations.return_value = [
            {"key": "A1", "type": "highlight", "text": "test", "comment": "", "color": "", "page": "1", "tags": []},
        ]
        mock_reader.return_value = mock_api

        from zotpilot.tools.library import get_annotations
        result = get_annotations(item_key="ITEM1", limit=10)
        assert len(result) == 1
        mock_api.get_annotations.assert_called_once_with(item_key="ITEM1", limit=10)

    @patch("zotpilot.tools.library._get_api_reader")
    def test_no_api_key(self, mock_reader):
        from fastmcp.exceptions import ToolError
        mock_reader.side_effect = ToolError("ZOTERO_API_KEY not set")

        from zotpilot.tools.library import get_annotations
        with pytest.raises(ToolError, match="ZOTERO_API_KEY"):
            get_annotations()

"""Tests for MCP tool functions (mock dependencies)."""
import pytest
from unittest.mock import patch, MagicMock

from zotpilot.state import ToolError


# ---------------------------------------------------------------------------
# library.py tools
# ---------------------------------------------------------------------------

class TestListCollections:
    @patch("zotpilot.tools.library._get_zotero")
    def test_list_collections(self, mock_get_zotero):
        from zotpilot.tools.library import list_collections

        mock_zotero = MagicMock()
        mock_zotero.get_all_collections.return_value = [
            {"key": "COL1", "name": "Machine Learning", "parent_key": None},
            {"key": "COL2", "name": "NLP", "parent_key": "COL1"},
        ]
        mock_get_zotero.return_value = mock_zotero

        result = list_collections()
        assert len(result) == 2
        assert result[0]["key"] == "COL1"
        assert result[1]["name"] == "NLP"
        mock_zotero.get_all_collections.assert_called_once()


class TestListTags:
    @patch("zotpilot.tools.library._get_zotero")
    def test_list_tags(self, mock_get_zotero):
        from zotpilot.tools.library import list_tags

        mock_zotero = MagicMock()
        mock_zotero.get_all_tags.return_value = [
            {"name": "deep-learning", "count": 15},
            {"name": "nlp", "count": 10},
        ]
        mock_get_zotero.return_value = mock_zotero

        result = list_tags(limit=10)
        assert len(result) == 2
        assert result[0]["name"] == "deep-learning"
        mock_zotero.get_all_tags.assert_called_once()


class TestGetPaperDetails:
    @patch("zotpilot.tools.library._get_store_optional")
    @patch("zotpilot.tools.library._get_zotero")
    def test_get_paper_details_not_found(self, mock_get_zotero, mock_get_store_opt):
        from zotpilot.tools.library import get_paper_details

        mock_zotero = MagicMock()
        mock_zotero.get_item.return_value = None
        mock_get_zotero.return_value = mock_zotero

        with pytest.raises(ToolError, match="Item not found"):
            get_paper_details("NONEXISTENT")


# ---------------------------------------------------------------------------
# write_ops.py tools
# ---------------------------------------------------------------------------

class TestSetItemTags:
    @patch("zotpilot.tools.write_ops._get_writer")
    def test_set_item_tags(self, mock_get_writer):
        from zotpilot.tools.write_ops import set_item_tags

        mock_writer = MagicMock()
        mock_get_writer.return_value = mock_writer

        result = set_item_tags("ITEM1", ["tag1", "tag2"])
        assert result == {"success": True, "item_key": "ITEM1", "tags": ["tag1", "tag2"]}
        mock_writer.set_item_tags.assert_called_once_with("ITEM1", ["tag1", "tag2"])


class TestAddItemTags:
    @patch("zotpilot.tools.write_ops._get_writer")
    def test_add_item_tags(self, mock_get_writer):
        from zotpilot.tools.write_ops import add_item_tags

        mock_writer = MagicMock()
        mock_get_writer.return_value = mock_writer

        result = add_item_tags("ITEM1", ["new-tag"])
        assert result == {"success": True, "item_key": "ITEM1", "added": ["new-tag"]}
        mock_writer.add_item_tags.assert_called_once_with("ITEM1", ["new-tag"])


class TestCreateCollection:
    @patch("zotpilot.tools.write_ops._get_writer")
    def test_create_collection(self, mock_get_writer):
        from zotpilot.tools.write_ops import create_collection

        mock_writer = MagicMock()
        mock_writer.create_collection.return_value = {
            "key": "NEWCOL",
            "name": "New Collection",
            "parent_key": None,
        }
        mock_get_writer.return_value = mock_writer

        result = create_collection("New Collection")
        assert result["key"] == "NEWCOL"
        assert result["name"] == "New Collection"
        mock_writer.create_collection.assert_called_once_with("New Collection", None)


# ---------------------------------------------------------------------------
# citations.py tools
# ---------------------------------------------------------------------------

class TestFindCitingPapers:
    @patch("zotpilot.tools.citations._get_config")
    @patch("zotpilot.tools.citations._get_store_optional")
    def test_find_citing_papers_no_doi(self, mock_get_store_opt, mock_get_config):
        from zotpilot.tools.citations import find_citing_papers

        mock_store = MagicMock()
        mock_store.get_document_meta.return_value = {"doc_id": "DOC1"}  # no "doi" key
        mock_get_store_opt.return_value = mock_store

        with pytest.raises(ToolError, match="no DOI"):
            find_citing_papers("DOC1")

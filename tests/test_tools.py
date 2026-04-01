"""Tests for MCP tool functions (mock dependencies)."""
from unittest.mock import MagicMock, patch

import pytest

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


class TestBrowseLibrary:
    @patch("zotpilot.tools.library._get_zotero")
    @patch("zotpilot.tools.library._get_store_optional")
    def test_browse_library_overview(self, mock_get_store_opt, mock_get_zotero):
        from zotpilot.tools.library import browse_library

        item = MagicMock()
        item.item_key = "DOC1"
        item.title = "Paper 1"
        item.year = "2024"
        item.authors = ["A"]
        item.publication = "Journal"
        item.tags = ["ml"]
        item.collections = ["COL1"]
        item.citation_key = "citekey"
        mock_get_zotero.return_value.get_all_items_with_pdfs.return_value = [item]
        mock_get_store_opt.return_value.get_indexed_doc_ids.return_value = {"DOC1"}

        result = browse_library(view="overview", limit=1, verbosity="full")

        assert result["total"] == 1
        assert result["papers"][0]["doc_id"] == "DOC1"
        assert result["papers"][0]["indexed"] is True

    def test_browse_library_collection_papers_requires_collection_key(self):
        from zotpilot.tools.library import browse_library

        with pytest.raises(ToolError, match="browse_library\\(view='collection_papers'\\) requires collection_key"):
            browse_library(view="collection_papers")


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


class TestNotesAndAnnotations:
    @patch("zotpilot.tools.library._get_zotero")
    def test_get_notes_truncates_content_in_minimal_mode(self, mock_get_zotero):
        from zotpilot.tools.library import get_notes

        mock_zotero = MagicMock()
        mock_zotero.get_notes.return_value = [
            {"key": "N1", "content": "x" * 250, "title": "Note 1", "tags": ["t1"], "date_added": "today"},
        ]
        mock_get_zotero.return_value = mock_zotero

        with patch("zotpilot.tools.library._get_writer", side_effect=ToolError("no api")):
            result = get_notes()
        assert len(result) == 1
        assert len(result[0]["content"]) <= 200
        assert result[0]["content"].startswith("x" * 10)
        assert result[0]["title"] == "Note 1"
        assert set(result[0]) == {"key", "parent_key", "parent_title", "title", "content"}

    @patch("zotpilot.tools.library._get_zotero")
    def test_get_notes_full_content_when_requested(self, mock_get_zotero):
        from zotpilot.tools.library import get_notes

        mock_zotero = MagicMock()
        mock_zotero.get_notes.return_value = [
            {"key": "N1", "content": "x" * 250},
        ]
        mock_get_zotero.return_value = mock_zotero

        with patch("zotpilot.tools.library._get_writer", side_effect=ToolError("no api")):
            result = get_notes(verbosity="full")
        assert result[0]["content"] == "x" * 250

    @patch("zotpilot.tools.library._get_zotero")
    def test_get_notes_merges_sqlite_and_web_api_results(self, mock_get_zotero):
        from zotpilot.tools.library import get_notes

        mock_zotero = MagicMock()
        mock_zotero.get_notes.return_value = [
            {"key": "N1", "title": "SQLite note", "content": "local"},
        ]
        mock_writer = MagicMock()
        mock_writer.get_notes.return_value = [
            {"key": "N2", "title": "API note", "content": "remote"},
            {"key": "N1", "title": "API wins", "content": "newer"},
        ]
        mock_get_zotero.return_value = mock_zotero

        with patch("zotpilot.tools.library._get_writer", return_value=mock_writer):
            result = get_notes(verbosity="full")

        assert [note["key"] for note in result] == ["N1", "N2"]
        assert result[0]["title"] == "API wins"

    @patch("zotpilot.tools.library._get_api_reader")
    def test_get_annotations_truncates_text_and_comment_in_minimal_mode(self, mock_get_api_reader):
        from zotpilot.tools.library import get_annotations

        mock_reader = MagicMock()
        mock_reader.get_annotations.return_value = [
            {"key": "A1", "text": "t" * 240, "comment": "c" * 220, "page": 1},
        ]
        mock_get_api_reader.return_value = mock_reader

        result = get_annotations()
        assert len(result[0]["text"]) <= 200
        assert len(result[0]["comment"]) <= 200
        assert result[0]["text"].startswith("t" * 10)
        assert result[0]["comment"].startswith("c" * 10)
        assert result[0]["page"] == 1

    @patch("zotpilot.tools.library._get_api_reader")
    def test_get_annotations_full_content_when_requested(self, mock_get_api_reader):
        from zotpilot.tools.library import get_annotations

        mock_reader = MagicMock()
        mock_reader.get_annotations.return_value = [
            {"key": "A1", "text": "t" * 240, "comment": "c" * 220},
        ]
        mock_get_api_reader.return_value = mock_reader

        result = get_annotations(verbosity="full")
        assert result[0]["text"] == "t" * 240
        assert result[0]["comment"] == "c" * 220


class TestGetPaperDetails:
    @patch("zotpilot.tools.library._get_store_optional")
    @patch("zotpilot.tools.library._get_zotero")
    def test_get_paper_details_not_found(self, mock_get_zotero, mock_get_store_opt):
        from zotpilot.tools.library import get_paper_details

        mock_zotero = MagicMock()
        mock_zotero.get_item.return_value = None
        mock_get_zotero.return_value = mock_zotero

        with pytest.raises(ToolError, match="Item not found"):
            get_paper_details(doc_id="NONEXISTENT")


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
    @patch("zotpilot.tools.write_ops._get_zotero")
    @patch("zotpilot.tools.write_ops._get_writer")
    def test_add_item_tags(self, mock_get_writer, mock_get_zotero):
        from zotpilot.tools.write_ops import add_item_tags

        mock_writer = MagicMock()
        mock_get_writer.return_value = mock_writer
        mock_get_zotero.return_value.get_all_tags.return_value = [{"name": "new-tag", "count": 1}]

        result = add_item_tags("ITEM1", ["new-tag"])
        assert result == {"success": True, "item_key": "ITEM1", "added": ["new-tag"]}
        mock_writer.add_item_tags.assert_called_once_with("ITEM1", ["new-tag"])


class TestManageTags:
    def test_manage_tags_requires_tags(self):
        from zotpilot.tools.write_ops import manage_tags

        with pytest.raises(ToolError, match="manage_tags requires tags"):
            manage_tags(action="add", item_keys="ITEM1")


class TestManageCollections:
    def test_manage_collections_requires_collection_key(self):
        from zotpilot.tools.write_ops import manage_collections

        with pytest.raises(ToolError, match="manage_collections requires collection_key"):
            manage_collections(action="add", item_keys="ITEM1")


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

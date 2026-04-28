"""Tests for profile_library MCP tool."""
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch


def _make_sqlite_row(data: dict):
    """Create a sqlite3.Row-like object from a dict."""
    row = MagicMock()
    row.__getitem__ = lambda self, key: data[key]
    return row


def _make_mock_connection(total_items=0, year_rows=None, col_rows=None, journal_rows=None):
    """Build a mock sqlite3 connection that returns canned query results."""
    if year_rows is None:
        year_rows = []
    if col_rows is None:
        col_rows = []
    if journal_rows is None:
        journal_rows = []

    total_row = _make_sqlite_row({"cnt": total_items})

    def make_rows(rows_data):
        return [_make_sqlite_row(d) for d in rows_data]

    mock_conn = MagicMock()
    mock_conn.__enter__ = lambda s: s
    mock_conn.__exit__ = MagicMock(return_value=False)

    # Track call count to return different results per query
    # Order: 1) COUNT total, 2) year distribution, 3) collections, 4) journals
    call_results = [
        MagicMock(**{"fetchone.return_value": total_row}),
        MagicMock(**{"fetchall.return_value": make_rows(year_rows)}),
        MagicMock(**{"fetchall.return_value": make_rows(col_rows)}),
        MagicMock(**{"fetchall.return_value": make_rows(journal_rows)}),
    ]
    call_count = [0]

    def execute_side_effect(sql, params=None):
        result = call_results[call_count[0]]
        call_count[0] += 1
        return result

    mock_conn.execute.side_effect = execute_side_effect
    return mock_conn


class TestProfileLibraryWithItems:
    @patch("zotpilot.tools.library._get_store_optional")
    @patch("zotpilot.tools.library._get_zotero")
    @patch("zotpilot.tools.library.sqlite3")
    def test_profile_library_with_items(self, mock_sqlite3, mock_get_zotero, mock_get_store_opt):
        from zotpilot.tools.library import profile_library

        mock_zotero = MagicMock()
        mock_zotero.db_path = Path("/fake/zotero.sqlite")
        mock_zotero.library_id = 1
        mock_zotero.get_all_tags.return_value = [
            {"name": "machine learning", "count": 2},
            {"name": "NLP", "count": 2},
        ]

        # Mock items with PDFs so current_library_pdf_doc_ids returns KEY1 and KEY2
        mock_item1 = MagicMock()
        mock_item1.item_key = "KEY1"
        mock_item1.pdf_path = MagicMock()
        mock_item1.pdf_path.exists.return_value = True
        mock_item2 = MagicMock()
        mock_item2.item_key = "KEY2"
        mock_item2.pdf_path = MagicMock()
        mock_item2.pdf_path.exists.return_value = True
        mock_zotero.get_all_items_with_pdfs.return_value = [mock_item1, mock_item2]
        mock_get_zotero.return_value = mock_zotero

        mock_conn = _make_mock_connection(
            total_items=3,
            year_rows=[
                {"year": "2024", "cnt": 1},
                {"year": "2023", "cnt": 1},
                {"year": "2022", "cnt": 1},
            ],
            col_rows=[
                {"key": "COL1", "collectionName": "AI", "cnt": 2},
                {"key": "COL2", "collectionName": "Methods", "cnt": 1},
            ],
            journal_rows=[
                {"journal": "Journal of Fluid Mechanics", "cnt": 2},
                {"journal": "Physics of Fluids", "cnt": 1},
            ],
        )
        mock_sqlite3.connect.return_value = mock_conn
        mock_sqlite3.Row = sqlite3.Row

        mock_store = MagicMock()
        mock_store.get_indexed_doc_ids.return_value = {"KEY1", "KEY2"}
        mock_get_store_opt.return_value = mock_store

        result = profile_library()

        # total_items comes from SQLite COUNT, not len(get_all_items_with_pdfs)
        assert result["total_items"] == 3
        assert result["year_distribution"] == {"2024": 1, "2023": 1, "2022": 1}
        assert "machine learning" in result["top_tags"]
        assert "NLP" in result["top_tags"]
        assert len(result["top_collections"]) >= 1
        ai_col = next(c for c in result["top_collections"] if c["name"] == "AI")
        assert ai_col["count"] == 2
        assert ai_col["key"] == "COL1"
        assert result["topic_density"]["indexed"] is True
        assert result["topic_density"]["doc_count"] == 2
        assert len(result["top_journals"]) == 2
        assert result["top_journals"][0]["name"] == "Journal of Fluid Mechanics"
        assert result["top_journals"][0]["count"] == 2


class TestProfileLibraryNoRagMode:
    @patch("zotpilot.tools.library._get_store_optional")
    @patch("zotpilot.tools.library._get_zotero")
    @patch("zotpilot.tools.library.sqlite3")
    def test_profile_library_no_rag_mode(self, mock_sqlite3, mock_get_zotero, mock_get_store_opt):
        from zotpilot.tools.library import profile_library

        mock_zotero = MagicMock()
        mock_zotero.db_path = Path("/fake/zotero.sqlite")
        mock_zotero.library_id = 1
        mock_zotero.get_all_tags.return_value = [
            {"name": "deep learning", "count": 1},
        ]
        mock_get_zotero.return_value = mock_zotero

        mock_conn = _make_mock_connection(
            total_items=1,
            year_rows=[{"year": "2023", "cnt": 1}],
            col_rows=[{"key": "COL1", "collectionName": "AI", "cnt": 1}],
        )
        mock_sqlite3.connect.return_value = mock_conn
        mock_sqlite3.Row = sqlite3.Row

        # No-RAG mode: store is None
        mock_get_store_opt.return_value = None

        result = profile_library()

        assert result["topic_density"] == {"indexed": False}
        assert result["total_items"] == 1
        assert len(result["top_tags"]) >= 1


class TestProfileLibraryEmptyLibrary:
    @patch("zotpilot.tools.library._get_store_optional")
    @patch("zotpilot.tools.library._get_zotero")
    @patch("zotpilot.tools.library.sqlite3")
    def test_profile_library_empty_library(self, mock_sqlite3, mock_get_zotero, mock_get_store_opt):
        from zotpilot.tools.library import profile_library

        mock_zotero = MagicMock()
        mock_zotero.db_path = Path("/fake/zotero.sqlite")
        mock_zotero.library_id = 1
        mock_zotero.get_all_tags.return_value = []
        mock_get_zotero.return_value = mock_zotero

        mock_conn = _make_mock_connection(total_items=0, year_rows=[], col_rows=[])
        mock_sqlite3.connect.return_value = mock_conn
        mock_sqlite3.Row = sqlite3.Row

        mock_get_store_opt.return_value = None

        result = profile_library()

        assert result["total_items"] == 0
        assert result["year_distribution"] == {}
        assert result["top_tags"] == []
        assert result["top_collections"] == []
        assert result["top_journals"] == []


class TestProfileLibraryExistingProfile:
    @patch("zotpilot.tools.library._get_store_optional")
    @patch("zotpilot.tools.library._get_zotero")
    @patch("zotpilot.tools.library.sqlite3")
    def test_profile_library_existing_profile(self, mock_sqlite3, mock_get_zotero, mock_get_store_opt, tmp_path):
        from zotpilot.tools.library import profile_library

        mock_zotero = MagicMock()
        mock_zotero.db_path = Path("/fake/zotero.sqlite")
        mock_zotero.library_id = 1
        mock_zotero.get_all_tags.return_value = []
        mock_get_zotero.return_value = mock_zotero

        mock_conn = _make_mock_connection(total_items=0, year_rows=[], col_rows=[])
        mock_sqlite3.connect.return_value = mock_conn
        mock_sqlite3.Row = sqlite3.Row

        mock_get_store_opt.return_value = None

        profile_content = "# My Research Profile\n\n" + ("Focused on NLP and ML. " * 20)

        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.read_text", return_value=profile_content):
            result = profile_library()

        assert result["existing_profile_present"] is True
        assert result["existing_profile_length"] == len(profile_content)
        assert result["existing_profile_snippet"].endswith("...")
        assert len(result["existing_profile_snippet"]) == 200
        assert "existing_profile" not in result

    @patch("zotpilot.tools.library._get_store_optional")
    @patch("zotpilot.tools.library._get_zotero")
    @patch("zotpilot.tools.library.sqlite3")
    def test_profile_library_include_profile(self, mock_sqlite3, mock_get_zotero, mock_get_store_opt):
        from zotpilot.tools.library import profile_library

        mock_zotero = MagicMock()
        mock_zotero.db_path = Path("/fake/zotero.sqlite")
        mock_zotero.library_id = 1
        mock_zotero.get_all_tags.return_value = []
        mock_get_zotero.return_value = mock_zotero

        mock_conn = _make_mock_connection(total_items=0, year_rows=[], col_rows=[])
        mock_sqlite3.connect.return_value = mock_conn
        mock_sqlite3.Row = sqlite3.Row

        mock_get_store_opt.return_value = None
        profile_content = "# My Research Profile"

        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.read_text", return_value=profile_content):
            result = profile_library(include_profile=True)

        assert result["existing_profile"] == profile_content

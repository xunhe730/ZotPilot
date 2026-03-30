"""Tests for get_notes (ZoteroClient method) and get_notes / create_note (MCP tools)."""
import sqlite3
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from zotpilot.zotero_client import ZoteroClient


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _create_notes_db(tmp_path: Path) -> Path:
    """Create a minimal SQLite DB with the notes schema."""
    db_path = tmp_path / "zotero.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE items (
            itemID INTEGER PRIMARY KEY,
            itemTypeID INTEGER,
            dateAdded TEXT DEFAULT '2024-01-01 00:00:00',
            key TEXT UNIQUE,
            libraryID INTEGER DEFAULT 1
        );
        CREATE TABLE deletedItems (itemID INTEGER PRIMARY KEY);
        CREATE TABLE itemNotes (
            itemID INTEGER PRIMARY KEY,
            parentItemID INTEGER,
            note TEXT
        );
        CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
        INSERT INTO fields VALUES (1, 'title');
        CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
        CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
        CREATE TABLE itemTags (itemID INTEGER, tagID INTEGER);
        CREATE TABLE tags (tagID INTEGER PRIMARY KEY, name TEXT);
    """)
    conn.close()
    return db_path


def _insert_note(
    db_path: Path,
    note_id: int,
    note_key: str,
    parent_id: int,
    parent_key: str,
    parent_title: str,
    content: str,
    tags: list[str] | None = None,
    date_added: str = "2024-01-15 00:00:00",
) -> None:
    """Insert a note and its parent item into the test DB."""
    conn = sqlite3.connect(str(db_path))
    # Parent item (itemTypeID=2 = journalArticle)
    conn.execute(
        "INSERT OR IGNORE INTO items (itemID, itemTypeID, dateAdded, key) VALUES (?, 2, '2024-01-01 00:00:00', ?)",
        (parent_id, parent_key),
    )
    # Title for parent
    conn.execute(
        "INSERT OR IGNORE INTO itemDataValues VALUES (?, ?)",
        (parent_id, parent_title),
    )
    conn.execute(
        "INSERT OR IGNORE INTO itemData VALUES (?, 1, ?)",
        (parent_id, parent_id),
    )
    # Note item (itemTypeID=1)
    conn.execute(
        "INSERT INTO items (itemID, itemTypeID, dateAdded, key) VALUES (?, 1, ?, ?)",
        (note_id, date_added, note_key),
    )
    conn.execute(
        "INSERT INTO itemNotes VALUES (?, ?, ?)",
        (note_id, parent_id, content),
    )
    if tags:
        for i, tag_name in enumerate(tags):
            tag_id = note_id * 100 + i
            conn.execute(
                "INSERT OR IGNORE INTO tags VALUES (?, ?)",
                (tag_id, tag_name),
            )
            conn.execute(
                "INSERT INTO itemTags VALUES (?, ?)",
                (note_id, tag_id),
            )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# ZoteroClient.get_notes tests
# ---------------------------------------------------------------------------

class TestGetNotes:
    def test_get_notes_all(self, tmp_path):
        """Insert 2 notes, call get_notes() with no filter, verify both returned."""
        db_path = _create_notes_db(tmp_path)
        _insert_note(db_path, 10, "NOTE01", 1, "ITEM01", "Paper One", "First note content")
        _insert_note(db_path, 11, "NOTE02", 2, "ITEM02", "Paper Two", "Second note content")

        client = ZoteroClient(tmp_path)
        results = client.get_notes()

        keys = {r["key"] for r in results}
        assert "NOTE01" in keys
        assert "NOTE02" in keys
        assert len(results) == 2

    def test_get_notes_by_parent(self, tmp_path):
        """Insert notes for 2 parents, filter by item_key, verify only matching notes returned."""
        db_path = _create_notes_db(tmp_path)
        _insert_note(db_path, 10, "NOTE01", 1, "ITEM01", "Paper One", "Note for paper one")
        _insert_note(db_path, 11, "NOTE02", 2, "ITEM02", "Paper Two", "Note for paper two")

        client = ZoteroClient(tmp_path)
        results = client.get_notes(item_key="ITEM01")

        assert len(results) == 1
        assert results[0]["key"] == "NOTE01"
        assert results[0]["parent_key"] == "ITEM01"

    def test_get_notes_search(self, tmp_path):
        """Insert notes with different content, search by query, verify filtering."""
        db_path = _create_notes_db(tmp_path)
        _insert_note(db_path, 10, "NOTE01", 1, "ITEM01", "Paper One", "Contains neural network methods")
        _insert_note(db_path, 11, "NOTE02", 2, "ITEM02", "Paper Two", "About fluid dynamics experiments")

        client = ZoteroClient(tmp_path)
        results = client.get_notes(query="neural")

        assert len(results) == 1
        assert results[0]["key"] == "NOTE01"

    def test_get_notes_empty(self, tmp_path):
        """No notes in DB returns empty list."""
        _create_notes_db(tmp_path)

        client = ZoteroClient(tmp_path)
        results = client.get_notes()

        assert results == []

    def test_get_notes_html_strip(self, tmp_path):
        """HTML content is stripped to plain text."""
        db_path = _create_notes_db(tmp_path)
        html_content = "<p>Hello <b>world</b></p><ul><li>item</li></ul>"
        _insert_note(db_path, 10, "NOTE01", 1, "ITEM01", "Paper One", html_content)

        client = ZoteroClient(tmp_path)
        results = client.get_notes()

        assert len(results) == 1
        content = results[0]["content"]
        assert "<p>" not in content
        assert "<b>" not in content
        assert "<ul>" not in content
        assert "<li>" not in content
        assert "Hello" in content
        assert "world" in content
        assert "item" in content

    def test_get_notes_includes_tags(self, tmp_path):
        """Notes with tags return them in the tags field."""
        db_path = _create_notes_db(tmp_path)
        _insert_note(
            db_path, 10, "NOTE01", 1, "ITEM01", "Paper One",
            "Note with tags", tags=["important", "review"],
        )

        client = ZoteroClient(tmp_path)
        results = client.get_notes()

        assert len(results) == 1
        tags = results[0]["tags"]
        assert "important" in tags
        assert "review" in tags

    def test_get_notes_result_shape(self, tmp_path):
        """Each result dict has the expected keys."""
        db_path = _create_notes_db(tmp_path)
        _insert_note(db_path, 10, "NOTE01", 1, "ITEM01", "Paper One", "Some content")

        client = ZoteroClient(tmp_path)
        results = client.get_notes()

        assert len(results) == 1
        note = results[0]
        for field in ("key", "parent_key", "parent_title", "tags", "content", "date_added"):
            assert field in note, f"Missing field: {field}"

    def test_get_notes_limit(self, tmp_path):
        """limit parameter caps the number of returned notes."""
        db_path = _create_notes_db(tmp_path)
        for i in range(5):
            _insert_note(
                db_path, 10 + i, f"NOTE{i:02d}", i + 1, f"ITEM{i:02d}",
                f"Paper {i}", f"Content {i}",
                date_added=f"2024-01-{i + 1:02d} 00:00:00",
            )

        client = ZoteroClient(tmp_path)
        results = client.get_notes(limit=3)

        assert len(results) == 3


# ---------------------------------------------------------------------------
# MCP tool: create_note
# ---------------------------------------------------------------------------

class TestCreateNoteTool:
    def test_create_note_success(self):
        """create_note tool calls writer.create_note() with correct arguments."""
        from zotpilot.tools.write_ops import create_note

        mock_writer = MagicMock()
        mock_writer.create_note.return_value = {
            "success": True,
            "note_key": "NEWKEY1",
        }

        with patch("zotpilot.tools.write_ops._get_writer", return_value=mock_writer):
            result = create_note(
                item_key="ITEM01",
                content="This is a test note",
                title="Test Title",
                tags=["tag1", "tag2"],
            )

        mock_writer.create_note.assert_called_once_with(
            "ITEM01",
            "This is a test note",
            title="Test Title",
            tags=["tag1", "tag2"],
        )
        assert result["success"] is True
        assert result["note_key"] == "NEWKEY1"

    def test_create_note_no_title_no_tags(self):
        """create_note works with only required arguments."""
        from zotpilot.tools.write_ops import create_note

        mock_writer = MagicMock()
        mock_writer.create_note.return_value = {"success": True, "note_key": "NK2"}

        with patch("zotpilot.tools.write_ops._get_writer", return_value=mock_writer):
            result = create_note(item_key="ITEM02", content="Minimal note")

        mock_writer.create_note.assert_called_once_with(
            "ITEM02", "Minimal note", title=None, tags=None
        )
        assert result["success"] is True

    def test_create_note_no_api_key(self):
        """create_note propagates ToolError when writer is unavailable."""
        from zotpilot.tools.write_ops import create_note
        from zotpilot.state import ToolError

        with patch(
            "zotpilot.tools.write_ops._get_writer",
            side_effect=ToolError("ZOTERO_API_KEY not set -- write operations unavailable"),
        ):
            with pytest.raises(ToolError, match="ZOTERO_API_KEY"):
                create_note(item_key="ITEM01", content="Some content")


# ---------------------------------------------------------------------------
# MCP tool: get_notes (tool layer)
# ---------------------------------------------------------------------------

class TestGetNotesTool:
    def test_get_notes_tool_delegates_to_client(self, tmp_path):
        """get_notes MCP tool returns results from the ZoteroClient."""
        from fastmcp.exceptions import ToolError

        from zotpilot.tools.library import get_notes

        db_path = _create_notes_db(tmp_path)
        _insert_note(db_path, 10, "NOTE01", 1, "ITEM01", "Paper One", "Tool layer note")

        mock_client = ZoteroClient(tmp_path)
        with patch("zotpilot.tools.library._get_zotero", return_value=mock_client), \
             patch("zotpilot.tools.library._get_writer", side_effect=ToolError("no api key")):
            results = get_notes(item_key=None, limit=20, query=None)

        assert len(results) == 1
        assert results[0]["key"] == "NOTE01"

    def test_get_notes_tool_filter_by_parent(self, tmp_path):
        """get_notes MCP tool passes item_key filter through to client."""
        from fastmcp.exceptions import ToolError

        from zotpilot.tools.library import get_notes

        db_path = _create_notes_db(tmp_path)
        _insert_note(db_path, 10, "NOTE01", 1, "ITEM01", "Paper One", "Note A")
        _insert_note(db_path, 11, "NOTE02", 2, "ITEM02", "Paper Two", "Note B")

        mock_client = ZoteroClient(tmp_path)
        with patch("zotpilot.tools.library._get_zotero", return_value=mock_client), \
             patch("zotpilot.tools.library._get_writer", side_effect=ToolError("no api key")):
            results = get_notes(item_key="ITEM02", limit=20, query=None)

        assert len(results) == 1
        assert results[0]["key"] == "NOTE02"

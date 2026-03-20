"""Tests for switch_library tool and get_libraries."""
import sqlite3
import pytest
from unittest.mock import patch, MagicMock

from zotpilot.zotero_client import ZoteroClient


def _create_lib_db(tmp_path, with_groups=False):
    db_path = tmp_path / "zotero.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE items (
            itemID INTEGER PRIMARY KEY, itemTypeID INTEGER,
            dateAdded TEXT DEFAULT '2024-01-01', key TEXT UNIQUE,
            libraryID INTEGER DEFAULT 1
        );
        CREATE TABLE deletedItems (itemID INTEGER PRIMARY KEY);
    """)
    # Insert some items (libraryID=1 = user library)
    conn.execute("INSERT INTO items VALUES (1, 2, '2024-01-01', 'ITEM1', 1)")
    conn.execute("INSERT INTO items VALUES (2, 2, '2024-01-01', 'ITEM2', 1)")
    conn.execute("INSERT INTO items VALUES (3, 1, '2024-01-01', 'NOTE1', 1)")  # note, excluded

    if with_groups:
        conn.executescript("""
            CREATE TABLE libraries (libraryID INTEGER PRIMARY KEY, type TEXT NOT NULL, editable INT NOT NULL DEFAULT 1, filesEditable INT NOT NULL DEFAULT 1, version INT NOT NULL DEFAULT 0, storageVersion INT NOT NULL DEFAULT 0, lastSync INT NOT NULL DEFAULT 0, archived INT NOT NULL DEFAULT 0);
            INSERT INTO libraries VALUES (1, 'user', 1, 1, 0, 0, 0, 0);
            INSERT INTO libraries VALUES (2, 'group', 1, 1, 0, 0, 0, 0);
            CREATE TABLE groups (groupID INTEGER PRIMARY KEY, libraryID INT NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', version INT NOT NULL DEFAULT 0);
            INSERT INTO groups VALUES (100, 2, 'Lab Group', '', 0);
        """)
        # Add an item in the group library
        conn.execute("INSERT INTO items VALUES (10, 2, '2024-01-01', 'GITEM1', 2)")
    conn.commit()
    conn.close()
    return db_path


class TestGetLibraries:
    def test_user_only(self, tmp_path):
        _create_lib_db(tmp_path)
        client = ZoteroClient(tmp_path)
        libs = client.get_libraries()
        assert len(libs) == 1
        assert libs[0]["library_type"] == "user"
        assert libs[0]["item_count"] == 2  # excludes note

    def test_with_groups(self, tmp_path):
        _create_lib_db(tmp_path, with_groups=True)
        client = ZoteroClient(tmp_path)
        libs = client.get_libraries()
        assert len(libs) == 2
        assert libs[1]["library_type"] == "group"
        assert libs[1]["name"] == "Lab Group"
        assert libs[1]["item_count"] == 1


class TestSwitchLibraryTool:
    @patch("zotpilot.tools.admin._get_zotero")
    def test_list_libraries(self, mock_zotero):
        mock_client = MagicMock()
        mock_client.get_libraries.return_value = [
            {"library_id": "1", "library_type": "user", "name": "My Library", "item_count": 10},
        ]
        mock_zotero.return_value = mock_client

        from zotpilot.tools.admin import switch_library
        result = switch_library()
        assert "libraries" in result
        assert len(result["libraries"]) == 1

    @patch("zotpilot.tools.admin._set_library_override")
    def test_switch_to_group(self, mock_set):
        from zotpilot.tools.admin import switch_library
        result = switch_library(library_id="100", library_type="group")
        assert result["switched"] is True
        assert result["library_id"] == "100"
        mock_set.assert_called_once_with("100", "group")

    @patch("zotpilot.tools.admin._clear_library_override")
    def test_reset_to_default(self, mock_clear):
        from zotpilot.tools.admin import switch_library
        result = switch_library(library_id="1", library_type="default")
        assert result["switched"] is True
        mock_clear.assert_called_once()


class TestResetSingletons:
    def test_reset_clears_all(self):
        import zotpilot.state as state
        # Set some dummy values
        state._config = "dummy"
        state._zotero = "dummy"
        state._writer = "dummy"

        state._reset_singletons()

        assert state._config is None
        assert state._zotero is None
        assert state._writer is None
        assert state._retriever is None
        assert state._store is None
        assert state._reranker is None

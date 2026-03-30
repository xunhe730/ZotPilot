"""Tests for switch_library tool and get_libraries."""
import sqlite3
from unittest.mock import MagicMock, patch

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
            CREATE TABLE libraries (
                libraryID INTEGER PRIMARY KEY,
                type TEXT NOT NULL,
                editable INT NOT NULL DEFAULT 1,
                filesEditable INT NOT NULL DEFAULT 1,
                version INT NOT NULL DEFAULT 0,
                storageVersion INT NOT NULL DEFAULT 0,
                lastSync INT NOT NULL DEFAULT 0,
                archived INT NOT NULL DEFAULT 0
            );
            INSERT INTO libraries VALUES (1, 'user', 1, 1, 0, 0, 0, 0);
            INSERT INTO libraries VALUES (2, 'group', 1, 1, 0, 0, 0, 0);
            CREATE TABLE groups (
                groupID INTEGER PRIMARY KEY,
                libraryID INT NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                version INT NOT NULL DEFAULT 0
            );
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
        assert libs[0]["item_count"] == 2  # user library only (not group items)
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


class TestLibraryOverrideIntegration:
    """Tests that _library_override is actually used by factory functions."""

    def test_writer_receives_override_ids(self):
        """_get_writer uses override lib_id/lib_type when _library_override is set."""
        import zotpilot.state as state
        state._reset_singletons()
        state._library_override = {"library_id": "200", "library_type": "group"}
        state._config = MagicMock()
        state._config.zotero_api_key = "test-key"
        state._config.zotero_user_id = "12345"
        state._config.zotero_library_type = "user"

        with patch("zotpilot.zotero_writer.ZoteroWriter") as MockWriter:
            state._get_writer()
            MockWriter.assert_called_once_with("test-key", "200", "group")

        state._library_override = None
        state._reset_singletons()

    def test_api_reader_receives_override_ids(self):
        """_get_api_reader uses override lib_id/lib_type when _library_override is set."""
        import zotpilot.state as state
        state._reset_singletons()
        state._library_override = {"library_id": "300", "library_type": "group"}
        state._config = MagicMock()
        state._config.zotero_api_key = "test-key"
        state._config.zotero_user_id = "12345"
        state._config.zotero_library_type = "user"

        with patch("zotpilot.zotero_api_reader.ZoteroApiReader") as MockReader:
            state._get_api_reader()
            MockReader.assert_called_once_with("test-key", "300", "group")

        state._library_override = None
        state._reset_singletons()

    def test_writer_uses_config_defaults_without_override(self):
        """Without override, _get_writer uses config defaults."""
        import zotpilot.state as state
        state._reset_singletons()
        state._library_override = None
        state._config = MagicMock()
        state._config.zotero_api_key = "test-key"
        state._config.zotero_user_id = "12345"
        state._config.zotero_library_type = "user"

        with patch("zotpilot.zotero_writer.ZoteroWriter") as MockWriter:
            state._get_writer()
            MockWriter.assert_called_once_with("test-key", "12345", "user")

        state._reset_singletons()

    def test_clear_override_restores_defaults(self):
        """After clearing override, factories use config defaults again."""
        import zotpilot.state as state
        state._reset_singletons()
        # Set override and create writer
        state._library_override = {"library_id": "200", "library_type": "group"}
        state._config = MagicMock()
        state._config.zotero_api_key = "test-key"
        state._config.zotero_user_id = "12345"
        state._config.zotero_library_type = "user"

        with patch("zotpilot.zotero_writer.ZoteroWriter"):
            state._get_writer()

        # Clear override and re-init
        state._clear_library_override()
        state._config = MagicMock()
        state._config.zotero_api_key = "test-key"
        state._config.zotero_user_id = "12345"
        state._config.zotero_library_type = "user"

        with patch("zotpilot.zotero_writer.ZoteroWriter") as MockWriter:
            state._get_writer()
            MockWriter.assert_called_once_with("test-key", "12345", "user")

        state._reset_singletons()


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

    def test_reset_runs_registered_callbacks(self):
        import zotpilot.state as state

        called = []

        def callback():
            called.append(True)

        state.register_reset_callback(callback)
        try:
            state._reset_singletons()
        finally:
            state._reset_callbacks.remove(callback)

        assert called == [True]

    def test_reset_clears_ingestion_process_caches(self):
        import zotpilot.state as state
        import zotpilot.tools.ingestion as ingestion

        ingestion._inbox_collection_key = "INBOX123"

        state._reset_singletons()

        assert ingestion._inbox_collection_key is None

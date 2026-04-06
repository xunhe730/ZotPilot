"""Tests for feed browsing (ZoteroClient + browse_library tool)."""
import sqlite3
from unittest.mock import MagicMock, patch

from zotpilot.zotero_client import ZoteroClient


def _create_feeds_db(tmp_path):
    """Create SQLite DB with feeds schema."""
    db_path = tmp_path / "zotero.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE items (
            itemID INTEGER PRIMARY KEY,
            itemTypeID INTEGER,
            dateAdded TEXT DEFAULT '2024-01-01',
            key TEXT UNIQUE,
            libraryID INTEGER DEFAULT 1
        );
        CREATE TABLE deletedItems (itemID INTEGER PRIMARY KEY);
        CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
        INSERT INTO fields VALUES (1, 'title'), (5, 'abstractNote'), (6, 'url');
        CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
        CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
        CREATE TABLE itemCreators (itemID INTEGER, creatorID INTEGER, orderIndex INTEGER);
        CREATE TABLE creators (creatorID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT);
        CREATE TABLE feeds (
            libraryID INTEGER PRIMARY KEY,
            name TEXT,
            url TEXT,
            lastCheck TEXT
        );
        CREATE TABLE feedItems (
            itemID INTEGER PRIMARY KEY,
            guid TEXT,
            readTime TEXT
        );
    """)
    conn.close()
    return db_path


def _insert_feed(db_path, library_id, title, url, last_check=None):
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO feeds VALUES (?, ?, ?, ?)",
        (library_id, title, url, last_check),
    )
    conn.commit()
    conn.close()


def _insert_feed_item(db_path, item_id, key, library_id, title, read=False):
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO items VALUES (?, 2, '2024-02-01', ?, ?)", (item_id, key, library_id))
    conn.execute(
        "INSERT INTO feedItems VALUES (?, ?, ?)",
        (item_id, f"guid-{key}", "2024-02-01" if read else None),
    )
    vid = item_id * 10 + 1
    conn.execute("INSERT INTO itemDataValues VALUES (?, ?)", (vid, title))
    conn.execute("INSERT INTO itemData VALUES (?, 1, ?)", (item_id, vid))
    conn.commit()
    conn.close()


class TestGetFeeds:
    def test_list_feeds(self, tmp_path):
        db_path = _create_feeds_db(tmp_path)
        _insert_feed(db_path, 1, "ArXiv CS", "https://arxiv.org/rss/cs", "2024-03-01")
        _insert_feed(db_path, 2, "Nature", "https://nature.com/rss", None)

        client = ZoteroClient(tmp_path)
        feeds = client.get_feeds()
        assert len(feeds) == 2
        assert feeds[0]["name"] == "ArXiv CS"
        assert feeds[0]["url"] == "https://arxiv.org/rss/cs"
        assert feeds[0]["library_id"] == 1
        assert feeds[1]["name"] == "Nature"

    def test_list_feeds_empty(self, tmp_path):
        _create_feeds_db(tmp_path)
        client = ZoteroClient(tmp_path)
        assert client.get_feeds() == []

    def test_list_feeds_no_table(self, tmp_path):
        """Old Zotero without feeds table."""
        db_path = tmp_path / "zotero.sqlite"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE items ("
            "itemID INTEGER PRIMARY KEY, "
            "itemTypeID INTEGER, "
            "dateAdded TEXT, "
            "key TEXT, "
            "libraryID INTEGER DEFAULT 1)"
        )
        conn.close()
        client = ZoteroClient(tmp_path)
        assert client.get_feeds() == []

    def test_get_feed_items(self, tmp_path):
        db_path = _create_feeds_db(tmp_path)
        _insert_feed(db_path, 1, "ArXiv", "https://arxiv.org/rss/cs")
        _insert_feed_item(db_path, 100, "FEED1", 1, "Paper A", read=True)
        _insert_feed_item(db_path, 101, "FEED2", 1, "Paper B", read=False)

        client = ZoteroClient(tmp_path)
        items = client.get_feed_items(1)
        assert len(items) == 2
        assert items[0]["title"] in ("Paper A", "Paper B")
        # Check read status
        read_items = [i for i in items if i["read"]]
        unread_items = [i for i in items if not i["read"]]
        assert len(read_items) == 1
        assert len(unread_items) == 1

    def test_get_feed_items_empty(self, tmp_path):
        db_path = _create_feeds_db(tmp_path)
        _insert_feed(db_path, 1, "Empty Feed", "https://example.com/rss")
        client = ZoteroClient(tmp_path)
        assert client.get_feed_items(1) == []

    def test_get_feed_items_no_table(self, tmp_path):
        db_path = tmp_path / "zotero.sqlite"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE items ("
            "itemID INTEGER PRIMARY KEY, "
            "itemTypeID INTEGER, "
            "dateAdded TEXT, "
            "key TEXT, "
            "libraryID INTEGER DEFAULT 1)"
        )
        conn.close()
        client = ZoteroClient(tmp_path)
        assert client.get_feed_items(1) == []

    def test_feed_item_count(self, tmp_path):
        db_path = _create_feeds_db(tmp_path)
        _insert_feed(db_path, 1, "Feed", "https://example.com/rss")
        _insert_feed_item(db_path, 100, "F1", 1, "A")
        _insert_feed_item(db_path, 101, "F2", 1, "B")

        client = ZoteroClient(tmp_path)
        feeds = client.get_feeds()
        assert feeds[0]["item_count"] == 2


class TestGetFeedsTool:
    @patch("zotpilot.tools.library._get_zotero")
    def test_list_feeds_tool(self, mock_zotero):
        mock_client = MagicMock()
        mock_client.get_feeds.return_value = [
            {"library_id": 1, "name": "ArXiv", "url": "https://arxiv.org", "item_count": 5, "last_check": ""},
        ]
        mock_zotero.return_value = mock_client

        from zotpilot.tools.library import browse_library
        result = browse_library(view="feeds")
        assert result["total"] == 1
        assert result["feeds"][0]["name"] == "ArXiv"
        mock_client.get_feeds.assert_called_once()

    @patch("zotpilot.tools.library._get_zotero")
    def test_get_feed_items_tool(self, mock_zotero):
        mock_client = MagicMock()
        mock_client.get_feed_items.return_value = [
            {
                "key": "F1",
                "title": "Paper",
                "authors": "",
                "abstract": "",
                "url": "",
                "date_added": "",
                "read": False,
            },
        ]
        mock_zotero.return_value = mock_client

        from zotpilot.tools.library import browse_library
        result = browse_library(view="feeds", library_id=1, limit=10)
        assert result["total"] == 1
        assert result["library_id"] == 1
        assert set(result["items"][0]) == {"key", "title", "date_added", "read"}
        mock_client.get_feed_items.assert_called_once_with(1, limit=10)

    @patch("zotpilot.tools.library._get_zotero")
    def test_get_feed_items_tool_full_preserves_legacy_fields(self, mock_zotero):
        mock_client = MagicMock()
        mock_client.get_feed_items.return_value = [
            {
                "key": "F1",
                "title": "Paper",
                "authors": "Auth",
                "abstract": "Abstract",
                "url": "https://example.com",
                "date_added": "",
                "read": False,
            },
        ]
        mock_zotero.return_value = mock_client

        from zotpilot.tools.library import browse_library
        result = browse_library(view="feeds", library_id=1, limit=10, verbosity="full")
        assert result["items"][0]["authors"] == "Auth"
        assert result["items"][0]["abstract"] == "Abstract"
        assert result["items"][0]["url"] == "https://example.com"

    @patch("zotpilot.tools.library._get_zotero")
    def test_get_feed_items_minimal_tolerates_missing_optional_fields(self, mock_zotero):
        mock_client = MagicMock()
        mock_client.get_feed_items.return_value = [
            {"key": "F1", "title": "Paper"},
        ]
        mock_zotero.return_value = mock_client

        from zotpilot.tools.library import browse_library
        result = browse_library(view="feeds", library_id=1, limit=10)
        assert result["items"][0] == {
            "key": "F1",
            "title": "Paper",
            "date_added": None,
            "read": None,
        }

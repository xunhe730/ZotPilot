"""Tests for get_collection_items (key-based SQL query)."""
import sqlite3

from zotpilot.zotero_client import ZoteroClient


def _create_collection_db(tmp_path):
    """Create SQLite DB with collections schema."""
    db_path = tmp_path / "zotero.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE items (
            itemID INTEGER PRIMARY KEY, itemTypeID INTEGER,
            dateAdded TEXT DEFAULT '2024-01-01', key TEXT UNIQUE,
            libraryID INTEGER DEFAULT 1
        );
        CREATE TABLE deletedItems (itemID INTEGER PRIMARY KEY);
        CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
        INSERT INTO fields VALUES (1, 'title'), (5, 'abstractNote'), (6, 'url'),
                                  (7, 'date'), (8, 'publicationTitle'), (9, 'DOI'),
                                  (10, 'citationKey');
        CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
        CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
        CREATE TABLE itemCreators (itemID INTEGER, creatorID INTEGER, orderIndex INTEGER);
        CREATE TABLE creators (creatorID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT);
        CREATE TABLE itemTags (itemID INTEGER, tagID INTEGER);
        CREATE TABLE tags (tagID INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE collections (
            collectionID INTEGER PRIMARY KEY,
            collectionName TEXT,
            parentCollectionID INTEGER,
            key TEXT UNIQUE,
            libraryID INTEGER DEFAULT 1
        );
        CREATE TABLE collectionItems (
            collectionID INTEGER,
            itemID INTEGER,
            orderIndex INTEGER DEFAULT 0
        );
    """)
    # Create two collections with the SAME name
    conn.execute("INSERT INTO collections VALUES (1, 'Machine Learning', NULL, 'COL_A', 1)")
    conn.execute("INSERT INTO collections VALUES (2, 'Machine Learning', NULL, 'COL_B', 1)")

    # Item 1 in COL_A only
    conn.execute("INSERT INTO items VALUES (1, 2, '2024-01-01', 'ITEM1', 1)")
    conn.execute("INSERT INTO itemDataValues VALUES (101, 'Paper Alpha')")
    conn.execute("INSERT INTO itemData VALUES (1, 1, 101)")
    conn.execute("INSERT INTO collectionItems VALUES (1, 1, 0)")

    # Item 2 in COL_B only
    conn.execute("INSERT INTO items VALUES (2, 2, '2024-01-01', 'ITEM2', 1)")
    conn.execute("INSERT INTO itemDataValues VALUES (102, 'Paper Beta')")
    conn.execute("INSERT INTO itemData VALUES (2, 1, 102)")
    conn.execute("INSERT INTO collectionItems VALUES (2, 2, 0)")

    # Item 3 in both collections
    conn.execute("INSERT INTO items VALUES (3, 2, '2024-01-01', 'ITEM3', 1)")
    conn.execute("INSERT INTO itemDataValues VALUES (103, 'Paper Gamma')")
    conn.execute("INSERT INTO itemData VALUES (3, 1, 103)")
    conn.execute("INSERT INTO collectionItems VALUES (1, 3, 1)")
    conn.execute("INSERT INTO collectionItems VALUES (2, 3, 1)")

    conn.commit()
    conn.close()
    return db_path


class TestGetCollectionItems:
    def test_items_by_key_not_name(self, tmp_path):
        """Two collections with same name return different items when queried by key."""
        _create_collection_db(tmp_path)
        client = ZoteroClient(tmp_path)

        items_a = client.get_collection_items("COL_A")
        items_b = client.get_collection_items("COL_B")

        keys_a = {i["key"] for i in items_a}
        keys_b = {i["key"] for i in items_b}

        assert "ITEM1" in keys_a
        assert "ITEM2" not in keys_a  # ITEM2 is only in COL_B
        assert "ITEM2" in keys_b
        assert "ITEM1" not in keys_b  # ITEM1 is only in COL_A
        assert "ITEM3" in keys_a  # shared
        assert "ITEM3" in keys_b  # shared

    def test_limit(self, tmp_path):
        _create_collection_db(tmp_path)
        client = ZoteroClient(tmp_path)
        items = client.get_collection_items("COL_A", limit=1)
        assert len(items) == 1

    def test_empty_collection(self, tmp_path):
        db_path = tmp_path / "zotero.sqlite"
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE items (itemID INTEGER PRIMARY KEY, itemTypeID INTEGER,
                               dateAdded TEXT, key TEXT UNIQUE, libraryID INTEGER DEFAULT 1);
            CREATE TABLE deletedItems (itemID INTEGER PRIMARY KEY);
            CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
            INSERT INTO fields VALUES (1, 'title');
            CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
            CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
            CREATE TABLE itemCreators (itemID INTEGER, creatorID INTEGER, orderIndex INTEGER);
            CREATE TABLE creators (creatorID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT);
            CREATE TABLE itemTags (itemID INTEGER, tagID INTEGER);
            CREATE TABLE tags (tagID INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE collections (collectionID INTEGER PRIMARY KEY, collectionName TEXT,
                                     parentCollectionID INTEGER, key TEXT UNIQUE, libraryID INTEGER DEFAULT 1);
            CREATE TABLE collectionItems (collectionID INTEGER, itemID INTEGER, orderIndex INTEGER DEFAULT 0);
            INSERT INTO collections VALUES (1, 'Empty', NULL, 'EMPTY_COL', 1);
        """)
        conn.commit()
        conn.close()
        client = ZoteroClient(tmp_path)
        assert client.get_collection_items("EMPTY_COL") == []

    def test_nonexistent_collection_key(self, tmp_path):
        _create_collection_db(tmp_path)
        client = ZoteroClient(tmp_path)
        assert client.get_collection_items("NONEXISTENT") == []

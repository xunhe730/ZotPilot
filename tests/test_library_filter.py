"""Tests for ZoteroClient library_id filtering."""
import sqlite3
import pytest
from zotpilot.zotero_client import ZoteroClient


def _create_multi_library_db(tmp_path):
    """Create DB with items in user library (1) and group library (2)."""
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
        INSERT INTO fields VALUES (1, 'title'), (5, 'abstractNote'),
                                  (7, 'date'), (8, 'publicationTitle'), (9, 'DOI');
        CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
        CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
        CREATE TABLE itemCreators (itemID INTEGER, creatorID INTEGER, orderIndex INTEGER);
        CREATE TABLE creators (creatorID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT);
        CREATE TABLE itemTags (itemID INTEGER, tagID INTEGER);
        CREATE TABLE tags (tagID INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE collections (
            collectionID INTEGER PRIMARY KEY, collectionName TEXT,
            parentCollectionID INTEGER, key TEXT UNIQUE,
            libraryID INTEGER DEFAULT 1
        );
        CREATE TABLE collectionItems (collectionID INTEGER, itemID INTEGER, orderIndex INTEGER DEFAULT 0);
        CREATE TABLE itemAttachments (
            itemID INTEGER PRIMARY KEY, parentItemID INTEGER,
            contentType TEXT, linkMode INTEGER, path TEXT
        );
        CREATE TABLE itemNotes (itemID INTEGER PRIMARY KEY, parentItemID INTEGER, note TEXT);
        CREATE TABLE groups (groupID INTEGER PRIMARY KEY, libraryID INT NOT NULL,
                            name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                            version INT NOT NULL DEFAULT 0);
        CREATE TABLE libraries (libraryID INTEGER PRIMARY KEY, type TEXT NOT NULL,
                               editable INT NOT NULL DEFAULT 1, filesEditable INT NOT NULL DEFAULT 1,
                               version INT NOT NULL DEFAULT 0, storageVersion INT NOT NULL DEFAULT 0,
                               lastSync INT NOT NULL DEFAULT 0, archived INT NOT NULL DEFAULT 0);
        INSERT INTO libraries VALUES (1, 'user', 1, 1, 0, 0, 0, 0);
        INSERT INTO libraries VALUES (2, 'group', 1, 1, 0, 0, 0, 0);
        INSERT INTO groups VALUES (100, 2, 'Lab Group', '', 0);
    """)

    # User library items (libraryID=1)
    conn.execute("INSERT INTO items VALUES (1, 2, '2024-01-01', 'USER1', 1)")
    conn.execute("INSERT INTO items VALUES (2, 2, '2024-01-01', 'USER2', 1)")
    conn.execute("INSERT INTO itemDataValues VALUES (101, 'User Paper A')")
    conn.execute("INSERT INTO itemDataValues VALUES (102, 'User Paper B')")
    conn.execute("INSERT INTO itemData VALUES (1, 1, 101)")
    conn.execute("INSERT INTO itemData VALUES (2, 1, 102)")
    conn.execute("INSERT INTO creators VALUES (1, 'Alice', 'Smith')")
    conn.execute("INSERT INTO itemCreators VALUES (1, 1, 0)")
    conn.execute("INSERT INTO itemCreators VALUES (2, 1, 0)")
    # PDF attachment for user item 1
    conn.execute("INSERT INTO items VALUES (10, 14, '2024-01-01', 'ATT_U1', 1)")
    conn.execute("INSERT INTO itemAttachments VALUES (10, 1, 'application/pdf', 0, 'storage:test.pdf')")

    # Group library items (libraryID=2)
    conn.execute("INSERT INTO items VALUES (3, 2, '2024-01-01', 'GRP1', 2)")
    conn.execute("INSERT INTO items VALUES (4, 2, '2024-01-01', 'GRP2', 2)")
    conn.execute("INSERT INTO itemDataValues VALUES (103, 'Group Paper X')")
    conn.execute("INSERT INTO itemDataValues VALUES (104, 'Group Paper Y')")
    conn.execute("INSERT INTO itemData VALUES (3, 1, 103)")
    conn.execute("INSERT INTO itemData VALUES (4, 1, 104)")
    conn.execute("INSERT INTO creators VALUES (2, 'Bob', 'Jones')")
    conn.execute("INSERT INTO itemCreators VALUES (3, 2, 0)")
    conn.execute("INSERT INTO itemCreators VALUES (4, 2, 0)")
    # PDF attachment for group item 3
    conn.execute("INSERT INTO items VALUES (11, 14, '2024-01-01', 'ATT_G1', 2)")
    conn.execute("INSERT INTO itemAttachments VALUES (11, 3, 'application/pdf', 0, 'storage:test2.pdf')")

    # Tags: 'ML' on user item, 'Bio' on group item
    conn.execute("INSERT INTO tags VALUES (1, 'ML')")
    conn.execute("INSERT INTO tags VALUES (2, 'Bio')")
    conn.execute("INSERT INTO itemTags VALUES (1, 1)")
    conn.execute("INSERT INTO itemTags VALUES (3, 2)")

    # Collections: one per library
    conn.execute("INSERT INTO collections VALUES (1, 'User Collection', NULL, 'UCOL', 1)")
    conn.execute("INSERT INTO collections VALUES (2, 'Group Collection', NULL, 'GCOL', 2)")
    conn.execute("INSERT INTO collectionItems VALUES (1, 1, 0)")
    conn.execute("INSERT INTO collectionItems VALUES (2, 3, 0)")

    # Notes: one user note, one group note
    conn.execute("INSERT INTO items VALUES (20, 1, '2024-01-01', 'UNOTE', 1)")
    conn.execute("INSERT INTO itemNotes VALUES (20, 1, '<p>User note</p>')")
    conn.execute("INSERT INTO items VALUES (21, 1, '2024-01-01', 'GNOTE', 2)")
    conn.execute("INSERT INTO itemNotes VALUES (21, 3, '<p>Group note</p>')")

    conn.commit()
    conn.close()
    return db_path


class TestLibraryFilter:
    def test_default_returns_user_items_only(self, tmp_path):
        """Default ZoteroClient (library_id=1) returns only user library items."""
        _create_multi_library_db(tmp_path)
        client = ZoteroClient(tmp_path)
        items = client.get_all_items_with_pdfs()
        keys = {i.item_key for i in items}
        assert "USER1" in keys
        assert "GRP1" not in keys

    def test_group_library_returns_group_items_only(self, tmp_path):
        """ZoteroClient(library_id=2) returns only group items."""
        _create_multi_library_db(tmp_path)
        client = ZoteroClient(tmp_path, library_id=2)
        items = client.get_all_items_with_pdfs()
        keys = {i.item_key for i in items}
        assert "GRP1" in keys
        assert "USER1" not in keys

    def test_tags_filtered_by_library(self, tmp_path):
        _create_multi_library_db(tmp_path)
        user_client = ZoteroClient(tmp_path, library_id=1)
        group_client = ZoteroClient(tmp_path, library_id=2)
        user_tags = {t["name"] for t in user_client.get_all_tags()}
        group_tags = {t["name"] for t in group_client.get_all_tags()}
        assert "ML" in user_tags
        assert "Bio" not in user_tags
        assert "Bio" in group_tags
        assert "ML" not in group_tags

    def test_collections_filtered_by_library(self, tmp_path):
        _create_multi_library_db(tmp_path)
        user_client = ZoteroClient(tmp_path, library_id=1)
        group_client = ZoteroClient(tmp_path, library_id=2)
        user_cols = {c["name"] for c in user_client.get_all_collections()}
        group_cols = {c["name"] for c in group_client.get_all_collections()}
        assert "User Collection" in user_cols
        assert "Group Collection" not in user_cols
        assert "Group Collection" in group_cols
        assert "User Collection" not in group_cols

    def test_notes_filtered_by_library(self, tmp_path):
        _create_multi_library_db(tmp_path)
        user_client = ZoteroClient(tmp_path, library_id=1)
        group_client = ZoteroClient(tmp_path, library_id=2)
        user_notes = user_client.get_notes()
        group_notes = group_client.get_notes()
        user_keys = {n["key"] for n in user_notes}
        group_keys = {n["key"] for n in group_notes}
        assert "UNOTE" in user_keys
        assert "GNOTE" not in user_keys
        assert "GNOTE" in group_keys
        assert "UNOTE" not in group_keys

    def test_advanced_search_filtered_by_library(self, tmp_path):
        _create_multi_library_db(tmp_path)
        user_client = ZoteroClient(tmp_path, library_id=1)
        group_client = ZoteroClient(tmp_path, library_id=2)
        user_results = user_client.advanced_search(
            [{"field": "title", "op": "contains", "value": "Paper"}]
        )
        group_results = group_client.advanced_search(
            [{"field": "title", "op": "contains", "value": "Paper"}]
        )
        user_keys = {r["item_key"] for r in user_results}
        group_keys = {r["item_key"] for r in group_results}
        assert "USER1" in user_keys
        assert "GRP1" not in user_keys
        assert "GRP1" in group_keys
        assert "USER1" not in group_keys

    def test_collection_items_filtered_by_library(self, tmp_path):
        _create_multi_library_db(tmp_path)
        user_client = ZoteroClient(tmp_path, library_id=1)
        group_client = ZoteroClient(tmp_path, library_id=2)
        user_items = user_client.get_collection_items("UCOL")
        group_items = group_client.get_collection_items("GCOL")
        assert len(user_items) == 1
        assert user_items[0]["key"] == "USER1"
        assert len(group_items) == 1
        assert group_items[0]["key"] == "GRP1"
        # Cross-library: user client can't see group collection items
        assert user_client.get_collection_items("GCOL") == []

    def test_resolve_group_library_id(self, tmp_path):
        _create_multi_library_db(tmp_path)
        lib_id = ZoteroClient.resolve_group_library_id(tmp_path, 100)
        assert lib_id == 2

    def test_resolve_group_library_id_not_found(self, tmp_path):
        _create_multi_library_db(tmp_path)
        with pytest.raises(ValueError, match="Group 999 not found"):
            ZoteroClient.resolve_group_library_id(tmp_path, 999)

    def test_get_item_filtered_by_library(self, tmp_path):
        """get_item() should not return items from another library."""
        _create_multi_library_db(tmp_path)
        user_client = ZoteroClient(tmp_path, library_id=1)
        group_client = ZoteroClient(tmp_path, library_id=2)
        # User client can see USER1, not GRP1
        assert user_client.get_item("USER1") is not None
        assert user_client.get_item("GRP1") is None
        # Group client can see GRP1, not USER1
        assert group_client.get_item("GRP1") is not None
        assert group_client.get_item("USER1") is None

    def test_get_item_abstract_filtered_by_library(self, tmp_path):
        """get_item_abstract() should not return abstracts from another library."""
        db_path = _create_multi_library_db(tmp_path)
        # Add abstracts to both items
        conn = sqlite3.connect(str(db_path))
        conn.execute("INSERT INTO itemDataValues VALUES (201, 'User abstract')")
        conn.execute("INSERT INTO itemData VALUES (1, 5, 201)")  # fieldID 5 = abstractNote
        conn.execute("INSERT INTO itemDataValues VALUES (202, 'Group abstract')")
        conn.execute("INSERT INTO itemData VALUES (3, 5, 202)")
        conn.commit()
        conn.close()

        user_client = ZoteroClient(tmp_path, library_id=1)
        group_client = ZoteroClient(tmp_path, library_id=2)
        assert user_client.get_item_abstract("USER1") == "User abstract"
        assert user_client.get_item_abstract("GRP1") == ""
        assert group_client.get_item_abstract("GRP1") == "Group abstract"
        assert group_client.get_item_abstract("USER1") == ""


class TestCollectionItemsYear:
    def test_year_is_integer_not_date_string(self, tmp_path):
        """get_collection_items() should return year as integer, not raw date string."""
        db_path = tmp_path / "zotero.sqlite"
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE items (itemID INTEGER PRIMARY KEY, itemTypeID INTEGER,
                               dateAdded TEXT, key TEXT UNIQUE, libraryID INTEGER DEFAULT 1);
            CREATE TABLE deletedItems (itemID INTEGER PRIMARY KEY);
            CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
            INSERT INTO fields VALUES (1, 'title'), (7, 'date');
            CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
            CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
            CREATE TABLE itemCreators (itemID INTEGER, creatorID INTEGER, orderIndex INTEGER);
            CREATE TABLE creators (creatorID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT);
            CREATE TABLE itemTags (itemID INTEGER, tagID INTEGER);
            CREATE TABLE tags (tagID INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE collections (collectionID INTEGER PRIMARY KEY, collectionName TEXT,
                                     parentCollectionID INTEGER, key TEXT UNIQUE,
                                     libraryID INTEGER DEFAULT 1);
            CREATE TABLE collectionItems (collectionID INTEGER, itemID INTEGER, orderIndex INTEGER DEFAULT 0);
            INSERT INTO collections VALUES (1, 'Test', NULL, 'COL1', 1);
            INSERT INTO items (itemID, itemTypeID, dateAdded, key) VALUES (1, 2, '2024-01-01', 'ITEM1');
            INSERT INTO itemDataValues VALUES (1, 'Paper Title');
            INSERT INTO itemData VALUES (1, 1, 1);
            INSERT INTO itemDataValues VALUES (2, '2023-06-15');
            INSERT INTO itemData VALUES (1, 7, 2);
            INSERT INTO collectionItems VALUES (1, 1, 0);
        """)
        conn.commit()
        conn.close()

        client = ZoteroClient(tmp_path)
        items = client.get_collection_items("COL1")
        assert len(items) == 1
        assert items[0]["year"] == 2023  # integer, not "2023-06-15"

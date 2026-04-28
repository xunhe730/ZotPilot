"""Tests for Zotero SQLite client."""
import sqlite3

import pytest

from zotpilot.zotero_client import ZoteroClient


@pytest.fixture
def zotero_db(tmp_path):
    """Create a minimal Zotero SQLite database for testing."""
    db_path = tmp_path / "zotero.sqlite"
    conn = sqlite3.connect(str(db_path))

    # Create minimal schema
    conn.executescript("""
        CREATE TABLE items (
            itemID INTEGER PRIMARY KEY,
            itemTypeID INTEGER,
            key TEXT UNIQUE,
            libraryID INTEGER DEFAULT 1
        );
        CREATE TABLE deletedItems (itemID INTEGER PRIMARY KEY);
        CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
        CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
        CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
        CREATE TABLE itemCreators (
            itemID INTEGER, creatorID INTEGER, creatorTypeID INTEGER, orderIndex INTEGER
        );
        CREATE TABLE creators (
            creatorID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT
        );
        CREATE TABLE itemAttachments (
            itemID INTEGER PRIMARY KEY, parentItemID INTEGER,
            contentType TEXT, linkMode INTEGER, path TEXT
        );
        CREATE TABLE tags (tagID INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE itemTags (itemID INTEGER, tagID INTEGER);
        CREATE TABLE collections (
            collectionID INTEGER PRIMARY KEY, collectionName TEXT,
            parentCollectionID INTEGER, key TEXT,
            libraryID INTEGER DEFAULT 1
        );
        CREATE TABLE collectionItems (collectionID INTEGER, itemID INTEGER);
        CREATE TABLE fulltextWords (wordID INTEGER PRIMARY KEY, word TEXT);
        CREATE TABLE fulltextItemWords (wordID INTEGER, itemID INTEGER);

        -- Insert field definitions
        INSERT INTO fields VALUES (1, 'title');
        INSERT INTO fields VALUES (2, 'date');
        INSERT INTO fields VALUES (3, 'publicationTitle');
        INSERT INTO fields VALUES (4, 'DOI');
        INSERT INTO fields VALUES (5, 'abstractNote');
        INSERT INTO fields VALUES (6, 'extra');

        -- Insert a test item (itemTypeID 2 = journalArticle)
        INSERT INTO items (itemID, itemTypeID, key) VALUES (1, 2, 'ITEM001');

        -- Title
        INSERT INTO itemDataValues VALUES (1, 'Test Paper Title');
        INSERT INTO itemData VALUES (1, 1, 1);

        -- Date
        INSERT INTO itemDataValues VALUES (2, '2020-01-15');
        INSERT INTO itemData VALUES (1, 2, 2);

        -- Publication
        INSERT INTO itemDataValues VALUES (3, 'Nature');
        INSERT INTO itemData VALUES (1, 3, 3);

        -- DOI
        INSERT INTO itemDataValues VALUES (4, '10.1234/test');
        INSERT INTO itemData VALUES (1, 4, 4);

        -- Abstract
        INSERT INTO itemDataValues VALUES (5, 'This is the abstract.');
        INSERT INTO itemData VALUES (1, 5, 5);

        -- Author
        INSERT INTO creators VALUES (1, 'John', 'Smith');
        INSERT INTO itemCreators VALUES (1, 1, 1, 0);

        -- PDF attachment
        INSERT INTO items (itemID, itemTypeID, key) VALUES (2, 14, 'ATT001');
        INSERT INTO itemAttachments VALUES (2, 1, 'application/pdf', 0, 'storage:test.pdf');

        -- Tag
        INSERT INTO tags VALUES (1, 'machine-learning');
        INSERT INTO itemTags VALUES (1, 1);

        -- Collection
        INSERT INTO collections (collectionID, collectionName, parentCollectionID, key)
            VALUES (1, 'AI Papers', NULL, 'COL001');
        INSERT INTO collectionItems VALUES (1, 1);

        -- Fulltext
        INSERT INTO fulltextWords VALUES (1, 'neural');
        INSERT INTO fulltextWords VALUES (2, 'network');
        INSERT INTO fulltextItemWords VALUES (1, 2);
        INSERT INTO fulltextItemWords VALUES (2, 2);
    """)
    conn.close()

    # Create storage directory with a test PDF
    storage = tmp_path / "storage" / "ATT001"
    storage.mkdir(parents=True)
    (storage / "test.pdf").write_bytes(b"%PDF-1.4 test")

    return tmp_path


def _insert_item_with_extra(db_dir, *, item_id: int, item_type_id: int, key: str, value_id: int, extra: str):
    conn = sqlite3.connect(str(db_dir / "zotero.sqlite"))
    conn.executescript(f"""
        INSERT INTO items (itemID, itemTypeID, key) VALUES ({item_id}, {item_type_id}, '{key}');
        INSERT INTO itemDataValues VALUES ({value_id}, '{extra}');
        INSERT INTO itemData VALUES ({item_id}, 6, {value_id});
    """)
    conn.close()


class TestZoteroClient:
    def test_get_all_items_with_pdfs(self, zotero_db):
        client = ZoteroClient(zotero_db)
        items = client.get_all_items_with_pdfs()
        assert len(items) == 1
        assert items[0].item_key == "ITEM001"
        assert items[0].title == "Test Paper Title"
        assert items[0].year == 2020
        assert items[0].pdf_path is not None
        assert items[0].publication == "Nature"

    def test_get_all_collections(self, zotero_db):
        client = ZoteroClient(zotero_db)
        collections = client.get_all_collections()
        assert len(collections) == 1
        assert collections[0]["name"] == "AI Papers"
        assert collections[0]["key"] == "COL001"

    def test_get_all_tags(self, zotero_db):
        client = ZoteroClient(zotero_db)
        tags = client.get_all_tags()
        assert len(tags) == 1
        assert tags[0]["name"] == "machine-learning"

    def test_get_item_abstract(self, zotero_db):
        client = ZoteroClient(zotero_db)
        abstract = client.get_item_abstract("ITEM001")
        assert abstract == "This is the abstract."

    def test_search_fulltext_and(self, zotero_db):
        client = ZoteroClient(zotero_db)
        results = client.search_fulltext("neural network", "AND")
        assert "ITEM001" in results

    def test_search_fulltext_or(self, zotero_db):
        client = ZoteroClient(zotero_db)
        results = client.search_fulltext("neural nonexistent", "OR")
        assert "ITEM001" in results

    def test_search_fulltext_no_match(self, zotero_db):
        client = ZoteroClient(zotero_db)
        results = client.search_fulltext("nonexistent", "AND")
        assert len(results) == 0

    def test_db_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            ZoteroClient(tmp_path / "nonexistent")

    def test_get_item_key_by_doi(self, zotero_db):
        client = ZoteroClient(zotero_db)
        assert client.get_item_key_by_doi("https://doi.org/10.1234/test") == "ITEM001"

    def test_get_item_key_by_arxiv_id_direct_match(self, zotero_db):
        _insert_item_with_extra(
            zotero_db,
            item_id=3,
            item_type_id=2,
            key="ITEMARXIV",
            value_id=6,
            extra="arXiv:2301.00001 [cs.CL]",
        )

        client = ZoteroClient(zotero_db)
        assert client.get_item_key_by_arxiv_id("2301.00001") == "ITEMARXIV"

    def test_get_item_key_by_arxiv_id_version_suffix_stripped(self, zotero_db):
        _insert_item_with_extra(
            zotero_db,
            item_id=3,
            item_type_id=2,
            key="ITEMARXIV",
            value_id=6,
            extra="arXiv:2301.00001 [cs.CL]",
        )

        client = ZoteroClient(zotero_db)
        assert client.get_item_key_by_arxiv_id("2301.00001v2") == "ITEMARXIV"

    def test_get_item_key_by_arxiv_id_no_match(self, zotero_db):
        client = ZoteroClient(zotero_db)
        assert client.get_item_key_by_arxiv_id("2301.00001") is None

    def test_get_item_key_by_arxiv_id_excludes_attachments(self, zotero_db):
        _insert_item_with_extra(
            zotero_db,
            item_id=3,
            item_type_id=14,
            key="ATTARXIV",
            value_id=6,
            extra="arXiv:2301.00001 [cs.CL]",
        )

        client = ZoteroClient(zotero_db)
        assert client.get_item_key_by_arxiv_id("2301.00001") is None

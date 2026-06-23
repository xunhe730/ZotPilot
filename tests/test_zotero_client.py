"""Tests for Zotero SQLite client."""
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from zotpilot.zotero_client import (
    ZoteroClient,
    has_translation_plugin_filename_shape,
    is_likely_bilingual_or_translated_pdf,
    pdf_content_translation_risk_score,
)


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
            libraryID INTEGER DEFAULT 1,
            dateAdded TEXT DEFAULT ''
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

    def test_get_all_items_prefers_original_pdf_over_translated_attachment(self, zotero_db):
        conn = sqlite3.connect(str(zotero_db / "zotero.sqlite"))
        conn.executescript("""
            INSERT INTO items (itemID, itemTypeID, key) VALUES (3, 14, 'ATT002');
            INSERT INTO itemAttachments VALUES (
                3,
                1,
                'application/pdf',
                0,
                'storage:双语对照-Test Paper Title.pdf'
            );
        """)
        conn.close()
        storage = zotero_db / "storage" / "ATT002"
        storage.mkdir(parents=True)
        (storage / "双语对照-Test Paper Title.pdf").write_bytes(b"%PDF-1.4")

        client = ZoteroClient(zotero_db)
        items = client.get_all_items_with_pdfs()

        assert len(items) == 1
        assert items[0].pdf_path is not None
        assert items[0].pdf_path.name == "test.pdf"

        item = client.get_item("ITEM001")
        assert item is not None
        assert item.pdf_path is not None
        assert item.pdf_path.name == "test.pdf"

    def test_get_all_items_prefers_pdf_with_matching_mineru_cache(self, zotero_db):
        conn = sqlite3.connect(str(zotero_db / "zotero.sqlite"))
        conn.executescript("""
            INSERT INTO items (itemID, itemTypeID, key) VALUES (10, 2, 'ITEMCACHE');
            INSERT INTO itemDataValues VALUES (20, 'Cache-backed paper');
            INSERT INTO itemData VALUES (10, 1, 20);

            INSERT INTO items (itemID, itemTypeID, key) VALUES (11, 14, 'NOCACHE');
            INSERT INTO itemAttachments VALUES (
                11,
                10,
                'application/pdf',
                0,
                'storage:No cache original.pdf'
            );

            INSERT INTO items (itemID, itemTypeID, key) VALUES (12, 14, 'CACHEDPDF');
            INSERT INTO itemAttachments VALUES (
                12,
                10,
                'application/pdf',
                0,
                'storage:Wang 等 - 2024 - Cached Original-872228.pdf'
            );

            INSERT INTO items (itemID, itemTypeID, key) VALUES (13, 14, 'CACHEZIP');
            INSERT INTO itemAttachments VALUES (
                13,
                10,
                'application/zip',
                0,
                'storage:LLM-for-Zotero-MinerU-cache-CACHEDPDF.zip'
            );
        """)
        conn.close()
        no_cache_storage = zotero_db / "storage" / "NOCACHE"
        no_cache_storage.mkdir(parents=True)
        (no_cache_storage / "No cache original.pdf").write_bytes(b"%PDF-1.4")
        cached_storage = zotero_db / "storage" / "CACHEDPDF"
        cached_storage.mkdir(parents=True)
        cached_name = "Wang 等 - 2024 - Cached Original-872228.pdf"
        (cached_storage / cached_name).write_bytes(b"%PDF-1.4")
        cache_zip_storage = zotero_db / "storage" / "CACHEZIP"
        cache_zip_storage.mkdir(parents=True)
        (cache_zip_storage / "LLM-for-Zotero-MinerU-cache-CACHEDPDF.zip").write_bytes(b"PK")

        client = ZoteroClient(zotero_db)
        item = client.get_item("ITEMCACHE")

        assert item is not None
        assert item.pdf_path is not None
        assert item.pdf_path.name == cached_name
        assert not is_likely_bilingual_or_translated_pdf(item.pdf_path)
        assert has_translation_plugin_filename_shape(item.pdf_path)

    def test_mineru_cache_paths_for_item_returns_cache_matching_selected_pdf(self, zotero_db):
        conn = sqlite3.connect(str(zotero_db / "zotero.sqlite"))
        conn.executescript("""
            INSERT INTO items (itemID, itemTypeID, key) VALUES (10, 2, 'ITEMCACHE2');
            INSERT INTO itemDataValues VALUES (20, 'Cache-backed paper');
            INSERT INTO itemData VALUES (10, 1, 20);

            INSERT INTO items (itemID, itemTypeID, key) VALUES (11, 14, 'PDFNOZIP');
            INSERT INTO itemAttachments VALUES (
                11,
                10,
                'application/pdf',
                0,
                'storage:No cache original.pdf'
            );

            INSERT INTO items (itemID, itemTypeID, key) VALUES (12, 14, 'PDFMATCH');
            INSERT INTO itemAttachments VALUES (
                12,
                10,
                'application/pdf',
                0,
                'storage:Matched original.pdf'
            );

            INSERT INTO items (itemID, itemTypeID, key) VALUES (13, 14, 'CACHEOK1');
            INSERT INTO itemAttachments VALUES (
                13,
                10,
                'application/zip',
                0,
                'storage:LLM-for-Zotero-MinerU-cache-PDFMATCH.zip'
            );

            INSERT INTO items (itemID, itemTypeID, key) VALUES (14, 14, 'CACHEBAD');
            INSERT INTO itemAttachments VALUES (
                14,
                10,
                'application/zip',
                0,
                'storage:LLM-for-Zotero-MinerU-cache-PDFNOZIP.zip'
            );
        """)
        conn.close()
        pdf_storage = zotero_db / "storage" / "PDFMATCH"
        pdf_storage.mkdir(parents=True)
        pdf_path = pdf_storage / "Matched original.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        cache_storage = zotero_db / "storage" / "CACHEOK1"
        cache_storage.mkdir(parents=True)
        cache_path = cache_storage / "LLM-for-Zotero-MinerU-cache-PDFMATCH.zip"
        cache_path.write_bytes(b"PK")
        other_cache_storage = zotero_db / "storage" / "CACHEBAD"
        other_cache_storage.mkdir(parents=True)
        (other_cache_storage / "LLM-for-Zotero-MinerU-cache-PDFNOZIP.zip").write_bytes(b"PK")

        client = ZoteroClient(zotero_db)
        cache_paths = client.mineru_cache_paths_for_item("ITEMCACHE2", pdf_path=pdf_path)

        assert cache_paths == [cache_path]

    def test_resolve_original_pdf_path_uses_content_risk_for_multi_pdf_item(self, zotero_db, monkeypatch):
        conn = sqlite3.connect(str(zotero_db / "zotero.sqlite"))
        conn.executescript("""
            INSERT INTO items (itemID, itemTypeID, key) VALUES (20, 2, 'ITEMMULTIPDF');
            INSERT INTO itemDataValues VALUES (30, 'A computational model of viscoplasticity');
            INSERT INTO itemData VALUES (20, 1, 30);

            INSERT INTO items (itemID, itemTypeID, key) VALUES (21, 14, 'BADPDF01');
            INSERT INTO itemAttachments VALUES (
                21,
                20,
                'application/pdf',
                0,
                'storage:Borvik 等 - 2001 - A computational model-730029.pdf'
            );

            INSERT INTO items (itemID, itemTypeID, key) VALUES (22, 14, 'GOODPDF1');
            INSERT INTO itemAttachments VALUES (
                22,
                20,
                'application/pdf',
                0,
                'storage:Borvik 等 - 2001 - A computational model-895630.pdf'
            );
        """)
        conn.close()
        bad_storage = zotero_db / "storage" / "BADPDF01"
        bad_storage.mkdir(parents=True)
        bad_path = bad_storage / "Borvik 等 - 2001 - A computational model-730029.pdf"
        bad_path.write_bytes(b"%PDF-1.4")
        good_storage = zotero_db / "storage" / "GOODPDF1"
        good_storage.mkdir(parents=True)
        good_name = "Borvik 等 - 2001 - A computational model-895630.pdf"
        good_path = good_storage / good_name
        good_path.write_bytes(b"%PDF-1.4")

        def fake_content_risk(path, *, title="", max_pages=6):
            return 120.0 if Path(path).name.endswith("730029.pdf") else 0.0

        monkeypatch.setattr(
            "zotpilot.zotero_client.pdf_content_translation_risk_score",
            fake_content_risk,
        )
        client = ZoteroClient(zotero_db)

        selected = client.resolve_original_pdf_path(
            "ITEMMULTIPDF",
            title="A computational model of viscoplasticity",
            fallback_path=bad_path,
        )

        assert selected is not None
        assert selected.name == good_name

    def test_resolve_original_pdf_path_trusts_unique_mineru_cache_match(self, zotero_db, monkeypatch):
        conn = sqlite3.connect(str(zotero_db / "zotero.sqlite"))
        conn.executescript("""
            INSERT INTO items (itemID, itemTypeID, key) VALUES (25, 2, 'ITEMCACHEPDF');
            INSERT INTO itemDataValues VALUES (35, 'A computational model of viscoplasticity');
            INSERT INTO itemData VALUES (25, 1, 35);

            INSERT INTO items (itemID, itemTypeID, key) VALUES (26, 14, 'TRANSPDF');
            INSERT INTO itemAttachments VALUES (
                26,
                25,
                'application/pdf',
                0,
                'storage:translated.pdf'
            );

            INSERT INTO items (itemID, itemTypeID, key) VALUES (27, 14, 'ORIGPDF1');
            INSERT INTO itemAttachments VALUES (
                27,
                25,
                'application/pdf',
                0,
                'storage:original.pdf'
            );

            INSERT INTO items (itemID, itemTypeID, key) VALUES (28, 14, 'CACHEPDF');
            INSERT INTO itemAttachments VALUES (
                28,
                25,
                'application/zip',
                0,
                'storage:LLM-for-Zotero-MinerU-cache-ORIGPDF1.zip'
            );
        """)
        conn.close()
        for key, name in (
            ("TRANSPDF", "translated.pdf"),
            ("ORIGPDF1", "original.pdf"),
            ("CACHEPDF", "LLM-for-Zotero-MinerU-cache-ORIGPDF1.zip"),
        ):
            storage = zotero_db / "storage" / key
            storage.mkdir(parents=True)
            (storage / name).write_bytes(b"PK" if name.endswith(".zip") else b"%PDF-1.4")
        content_risk = MagicMock(return_value=100.0)
        monkeypatch.setattr(
            "zotpilot.zotero_client.pdf_content_translation_risk_score",
            content_risk,
        )

        selected = ZoteroClient(zotero_db).resolve_original_pdf_path("ITEMCACHEPDF")

        assert selected is not None
        assert selected.name == "original.pdf"
        content_risk.assert_not_called()

    def test_get_item_uses_original_pdf_resolver_for_multi_pdf_item(self, zotero_db, monkeypatch):
        conn = sqlite3.connect(str(zotero_db / "zotero.sqlite"))
        conn.executescript("""
            INSERT INTO items (itemID, itemTypeID, key) VALUES (30, 2, 'ITEMPDFCHOICE');
            INSERT INTO itemDataValues VALUES (40, 'A computational model of viscoplasticity');
            INSERT INTO itemData VALUES (30, 1, 40);

            INSERT INTO items (itemID, itemTypeID, key) VALUES (31, 14, 'TRANSPDF');
            INSERT INTO itemAttachments VALUES (
                31,
                30,
                'application/pdf',
                0,
                'storage:Borvik 等 - 2001 - A computational model-730029.pdf'
            );

            INSERT INTO items (itemID, itemTypeID, key) VALUES (32, 14, 'ORIGPDF1');
            INSERT INTO itemAttachments VALUES (
                32,
                30,
                'application/pdf',
                0,
                'storage:Borvik 等 - 2001 - A computational model-895630.pdf'
            );
        """)
        conn.close()
        translated_storage = zotero_db / "storage" / "TRANSPDF"
        translated_storage.mkdir(parents=True)
        translated_path = translated_storage / "Borvik 等 - 2001 - A computational model-730029.pdf"
        translated_path.write_bytes(b"%PDF-1.4")
        original_storage = zotero_db / "storage" / "ORIGPDF1"
        original_storage.mkdir(parents=True)
        original_name = "Borvik 等 - 2001 - A computational model-895630.pdf"
        original_path = original_storage / original_name
        original_path.write_bytes(b"%PDF-1.4")

        def fake_content_risk(path, *, title="", max_pages=6):
            return 150.0 if Path(path) == translated_path else 0.0

        monkeypatch.setattr(
            "zotpilot.zotero_client.pdf_content_translation_risk_score",
            fake_content_risk,
        )

        item = ZoteroClient(zotero_db).get_item("ITEMPDFCHOICE")

        assert item is not None
        assert item.pdf_path is not None
        assert item.pdf_path.name == original_name

    def test_low_cjk_density_content_risk_does_not_skip_original_thesis(self, tmp_path, monkeypatch):
        pdf_path = tmp_path / "english-thesis-with-chinese-frontmatter.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")

        class FakePage:
            def get_text(self, mode="text"):
                if mode == "blocks":
                    return []
                return ("English front matter and abstract " * 80) + ("中文摘要" * 18)

        class FakeDoc:
            def __len__(self):
                return 1

            def __getitem__(self, index):
                return FakePage()

            def close(self):
                pass

        monkeypatch.setitem(sys.modules, "pymupdf", SimpleNamespace(open=lambda _path: FakeDoc()))

        risk = pdf_content_translation_risk_score(pdf_path, title="English thesis title")

        assert 0 < risk < 20.0

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

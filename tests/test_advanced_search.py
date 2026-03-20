"""Tests for advanced_search (ZoteroClient method) and advanced_search (MCP tool)."""
import sqlite3
import pytest
from pathlib import Path
from unittest.mock import patch

from zotpilot.zotero_client import ZoteroClient


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _create_search_db(tmp_path: Path) -> Path:
    """Create SQLite DB with full metadata schema for advanced_search testing."""
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
        INSERT INTO fields VALUES (1, 'title');
        INSERT INTO fields VALUES (2, 'date');
        INSERT INTO fields VALUES (3, 'publicationTitle');
        INSERT INTO fields VALUES (4, 'DOI');
        CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
        CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
        CREATE TABLE itemCreators (itemID INTEGER, creatorID INTEGER, orderIndex INTEGER);
        CREATE TABLE creators (creatorID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT);
        CREATE TABLE itemTags (itemID INTEGER, tagID INTEGER);
        CREATE TABLE tags (tagID INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE collectionItems (itemID INTEGER, collectionID INTEGER);
        CREATE TABLE collections (
            collectionID INTEGER PRIMARY KEY,
            collectionName TEXT,
            key TEXT,
            parentCollectionID INTEGER
        );
        CREATE TABLE itemAttachments (
            itemID INTEGER,
            parentItemID INTEGER,
            contentType TEXT,
            linkMode INTEGER,
            path TEXT
        );
    """)
    conn.close()
    return db_path


def _insert_item(
    db_path: Path,
    item_id: int,
    key: str,
    title: str,
    year: int,
    authors_list: list[tuple[str, str]],
    publication: str = "",
    doi: str = "",
    tags: list[str] | None = None,
    collections: list[str] | None = None,
) -> None:
    """Insert an item with full metadata into the test DB."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO items (itemID, itemTypeID, dateAdded, key) VALUES (?, 2, '2024-01-01', ?)",
        (item_id, key),
    )

    # Title
    vid = item_id * 10 + 1
    conn.execute("INSERT INTO itemDataValues VALUES (?, ?)", (vid, title))
    conn.execute("INSERT INTO itemData VALUES (?, 1, ?)", (item_id, vid))

    # Date/year
    vid2 = item_id * 10 + 2
    conn.execute("INSERT INTO itemDataValues VALUES (?, ?)", (vid2, f"{year}-01-01"))
    conn.execute("INSERT INTO itemData VALUES (?, 2, ?)", (item_id, vid2))

    # Publication
    if publication:
        vid3 = item_id * 10 + 3
        conn.execute("INSERT INTO itemDataValues VALUES (?, ?)", (vid3, publication))
        conn.execute("INSERT INTO itemData VALUES (?, 3, ?)", (item_id, vid3))

    # DOI
    if doi:
        vid4 = item_id * 10 + 4
        conn.execute("INSERT INTO itemDataValues VALUES (?, ?)", (vid4, doi))
        conn.execute("INSERT INTO itemData VALUES (?, 4, ?)", (item_id, vid4))

    # Authors
    for i, (first, last) in enumerate(authors_list):
        cid = item_id * 10 + i
        conn.execute(
            "INSERT OR IGNORE INTO creators VALUES (?, ?, ?)",
            (cid, first, last),
        )
        conn.execute(
            "INSERT INTO itemCreators VALUES (?, ?, ?)",
            (item_id, cid, i),
        )

    # Tags
    if tags:
        for i, tag_name in enumerate(tags):
            tid = item_id * 100 + i
            conn.execute(
                "INSERT OR IGNORE INTO tags VALUES (?, ?)",
                (tid, tag_name),
            )
            conn.execute("INSERT INTO itemTags VALUES (?, ?)", (item_id, tid))

    # Collections
    if collections:
        for i, col_name in enumerate(collections):
            cid = item_id * 100 + i + 50
            conn.execute(
                "INSERT OR IGNORE INTO collections VALUES (?, ?, ?, NULL)",
                (cid, col_name, f"COL{cid}"),
            )
            conn.execute("INSERT INTO collectionItems VALUES (?, ?)", (item_id, cid))

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Helper: build a client from tmp_path (no BBT file → _load_citation_keys={})
# ---------------------------------------------------------------------------

def _make_client(tmp_path: Path) -> ZoteroClient:
    return ZoteroClient(tmp_path)


# ---------------------------------------------------------------------------
# ZoteroClient.advanced_search tests
# ---------------------------------------------------------------------------

class TestAdvancedSearch:
    def test_single_condition_title(self, tmp_path):
        """Search by title contains 'neural', verify only matching item returned."""
        db = _create_search_db(tmp_path)
        _insert_item(db, 1, "ITEM01", "Deep Neural Networks", 2021, [("John", "Smith")])
        _insert_item(db, 2, "ITEM02", "Fluid Dynamics Review", 2020, [("Jane", "Doe")])

        client = _make_client(tmp_path)
        results = client.advanced_search(
            [{"field": "title", "op": "contains", "value": "neural"}]
        )

        keys = {r["item_key"] for r in results}
        assert "ITEM01" in keys
        assert "ITEM02" not in keys

    def test_single_condition_year_gt(self, tmp_path):
        """Search year > 2020 returns only items from 2021+."""
        db = _create_search_db(tmp_path)
        _insert_item(db, 1, "ITEM01", "Old Paper", 2019, [("A", "B")])
        _insert_item(db, 2, "ITEM02", "Recent Paper", 2022, [("C", "D")])
        _insert_item(db, 3, "ITEM03", "Boundary Paper", 2020, [("E", "F")])

        client = _make_client(tmp_path)
        results = client.advanced_search(
            [{"field": "year", "op": "gt", "value": "2020"}]
        )

        keys = {r["item_key"] for r in results}
        assert "ITEM02" in keys
        assert "ITEM01" not in keys
        assert "ITEM03" not in keys

    def test_multi_condition_and(self, tmp_path):
        """Two conditions with match='all' (AND) — both must be satisfied."""
        db = _create_search_db(tmp_path)
        _insert_item(db, 1, "ITEM01", "Neural Network Survey", 2022, [("A", "B")],
                     publication="Nature")
        _insert_item(db, 2, "ITEM02", "Neural Network Methods", 2020, [("C", "D")],
                     publication="Science")
        _insert_item(db, 3, "ITEM03", "Fluid Dynamics", 2022, [("E", "F")],
                     publication="Nature")

        client = _make_client(tmp_path)
        results = client.advanced_search(
            [
                {"field": "title", "op": "contains", "value": "neural"},
                {"field": "publication", "op": "is", "value": "Nature"},
            ],
            match="all",
        )

        keys = {r["item_key"] for r in results}
        # Only ITEM01 satisfies both conditions
        assert "ITEM01" in keys
        assert "ITEM02" not in keys
        assert "ITEM03" not in keys

    def test_multi_condition_or(self, tmp_path):
        """Two conditions with match='any' (OR) — either can be satisfied."""
        db = _create_search_db(tmp_path)
        _insert_item(db, 1, "ITEM01", "Neural Network Paper", 2022, [("A", "B")])
        _insert_item(db, 2, "ITEM02", "Fluid Dynamics", 2019, [("C", "D")])
        _insert_item(db, 3, "ITEM03", "Unrelated Topic", 2018, [("E", "F")])

        client = _make_client(tmp_path)
        results = client.advanced_search(
            [
                {"field": "title", "op": "contains", "value": "neural"},
                {"field": "year", "op": "gt", "value": "2020"},
            ],
            match="any",
        )

        keys = {r["item_key"] for r in results}
        # ITEM01 matches both; ITEM02 matches neither; ITEM03 matches neither
        assert "ITEM01" in keys
        assert "ITEM02" not in keys
        assert "ITEM03" not in keys

    def test_tag_exact_match(self, tmp_path):
        """tag op='is' is exact — 'ML' should NOT match item tagged 'HTML'."""
        db = _create_search_db(tmp_path)
        _insert_item(db, 1, "ITEM01", "Machine Learning Paper", 2021, [("A", "B")],
                     tags=["ML"])
        _insert_item(db, 2, "ITEM02", "Web Technologies", 2021, [("C", "D")],
                     tags=["HTML"])

        client = _make_client(tmp_path)
        results = client.advanced_search(
            [{"field": "tag", "op": "is", "value": "ML"}]
        )

        keys = {r["item_key"] for r in results}
        assert "ITEM01" in keys
        assert "ITEM02" not in keys

    def test_collection_join(self, tmp_path):
        """Filter by collection name returns only items in that collection."""
        db = _create_search_db(tmp_path)
        _insert_item(db, 1, "ITEM01", "Paper A", 2021, [("A", "B")],
                     collections=["Machine Learning"])
        _insert_item(db, 2, "ITEM02", "Paper B", 2021, [("C", "D")],
                     collections=["Physics"])

        client = _make_client(tmp_path)
        results = client.advanced_search(
            [{"field": "collection", "op": "is", "value": "Machine Learning"}]
        )

        keys = {r["item_key"] for r in results}
        assert "ITEM01" in keys
        assert "ITEM02" not in keys

    def test_empty_result(self, tmp_path):
        """Search for non-existent value returns empty list."""
        db = _create_search_db(tmp_path)
        _insert_item(db, 1, "ITEM01", "Some Paper", 2021, [("A", "B")])

        client = _make_client(tmp_path)
        results = client.advanced_search(
            [{"field": "title", "op": "contains", "value": "xyznonexistent123"}]
        )

        assert results == []

    def test_invalid_field(self, tmp_path):
        """Invalid field name raises ValueError."""
        _create_search_db(tmp_path)

        client = _make_client(tmp_path)
        with pytest.raises(ValueError, match="Invalid field"):
            client.advanced_search(
                [{"field": "invalid_field", "op": "contains", "value": "test"}]
            )

    def test_invalid_op(self, tmp_path):
        """Invalid operator raises ValueError."""
        _create_search_db(tmp_path)

        client = _make_client(tmp_path)
        with pytest.raises(ValueError, match="Invalid op"):
            client.advanced_search(
                [{"field": "title", "op": "invalid_op", "value": "test"}]
            )

    def test_no_pdf_items_visible(self, tmp_path):
        """Items without PDF attachments still appear in advanced_search results."""
        db = _create_search_db(tmp_path)
        # Insert item with no attachment at all
        _insert_item(db, 1, "ITEM01", "No PDF Paper", 2021, [("A", "B")])

        client = _make_client(tmp_path)
        results = client.advanced_search(
            [{"field": "title", "op": "contains", "value": "No PDF"}]
        )

        # advanced_search uses ADVANCED_SEARCH_BASE_SQL which does NOT require PDFs
        keys = {r["item_key"] for r in results}
        assert "ITEM01" in keys

    def test_sql_injection_prevention(self, tmp_path):
        """Passing SQL injection payload in value does not corrupt the database."""
        db = _create_search_db(tmp_path)
        _insert_item(db, 1, "ITEM01", "Legitimate Paper", 2021, [("A", "B")])

        client = _make_client(tmp_path)
        # Should not raise and should not destroy data
        results = client.advanced_search(
            [{"field": "title", "op": "contains", "value": "'; DROP TABLE items; --"}]
        )

        # No match expected, but DB must still be intact
        assert isinstance(results, list)

        # Verify items table is intact by doing a clean search
        clean_results = client.advanced_search(
            [{"field": "title", "op": "contains", "value": "Legitimate"}]
        )
        assert len(clean_results) == 1
        assert clean_results[0]["item_key"] == "ITEM01"

    def test_result_shape(self, tmp_path):
        """Each result dict includes expected metadata keys."""
        db = _create_search_db(tmp_path)
        _insert_item(
            db, 1, "ITEM01", "Shape Test Paper", 2021,
            [("Alice", "Smith")],
            publication="Nature",
            doi="10.1234/test",
            tags=["ml"],
            collections=["AI"],
        )

        client = _make_client(tmp_path)
        results = client.advanced_search(
            [{"field": "title", "op": "contains", "value": "Shape"}]
        )

        assert len(results) == 1
        r = results[0]
        for key in ("item_key", "doc_id", "title", "authors", "year",
                    "publication", "doi", "tags", "collections", "citation_key"):
            assert key in r, f"Missing key: {key}"

    def test_sort_by_year_asc(self, tmp_path):
        """sort_by='year', sort_dir='asc' returns oldest item first."""
        db = _create_search_db(tmp_path)
        _insert_item(db, 1, "ITEM01", "Alpha Paper", 2019, [("A", "B")])
        _insert_item(db, 2, "ITEM02", "Beta Paper", 2023, [("C", "D")])

        client = _make_client(tmp_path)
        results = client.advanced_search(
            [{"field": "title", "op": "contains", "value": "paper"}],
            sort_by="year",
            sort_dir="asc",
        )

        assert len(results) == 2
        assert results[0]["item_key"] == "ITEM01"
        assert results[1]["item_key"] == "ITEM02"

    def test_limit(self, tmp_path):
        """limit parameter caps the number of results."""
        db = _create_search_db(tmp_path)
        for i in range(5):
            _insert_item(db, i + 1, f"ITEM{i:02d}", f"Paper {i}", 2020 + i, [("A", "B")])

        client = _make_client(tmp_path)
        results = client.advanced_search(
            [{"field": "title", "op": "contains", "value": "paper"}],
            limit=3,
        )

        assert len(results) == 3

    def test_empty_conditions(self, tmp_path):
        """Empty conditions list returns empty list immediately."""
        db = _create_search_db(tmp_path)
        _insert_item(db, 1, "ITEM01", "Some Paper", 2021, [("A", "B")])

        client = _make_client(tmp_path)
        results = client.advanced_search([])

        assert results == []

    def test_tag_contains(self, tmp_path):
        """tag op='contains' matches partial tag name."""
        db = _create_search_db(tmp_path)
        _insert_item(db, 1, "ITEM01", "Paper A", 2021, [("A", "B")],
                     tags=["machine-learning"])
        _insert_item(db, 2, "ITEM02", "Paper B", 2021, [("C", "D")],
                     tags=["deep-learning"])

        client = _make_client(tmp_path)
        results = client.advanced_search(
            [{"field": "tag", "op": "contains", "value": "machine"}]
        )

        keys = {r["item_key"] for r in results}
        assert "ITEM01" in keys
        assert "ITEM02" not in keys

    def test_doi_filter(self, tmp_path):
        """DOI field filter works correctly."""
        db = _create_search_db(tmp_path)
        _insert_item(db, 1, "ITEM01", "Paper With DOI", 2021, [("A", "B")],
                     doi="10.1234/abcdef")
        _insert_item(db, 2, "ITEM02", "Paper No DOI", 2021, [("C", "D")])

        client = _make_client(tmp_path)
        results = client.advanced_search(
            [{"field": "doi", "op": "contains", "value": "1234"}]
        )

        keys = {r["item_key"] for r in results}
        assert "ITEM01" in keys
        assert "ITEM02" not in keys


# ---------------------------------------------------------------------------
# MCP tool: advanced_search (tool layer)
# ---------------------------------------------------------------------------

class TestAdvancedSearchTool:
    def test_tool_invalid_field_raises_toolerror(self, tmp_path):
        """MCP tool converts ValueError (invalid field) to ToolError."""
        from zotpilot.tools.search import advanced_search
        from zotpilot.state import ToolError

        db = _create_search_db(tmp_path)
        mock_client = _make_client(tmp_path)

        with patch("zotpilot.tools.search._get_zotero", return_value=mock_client):
            with pytest.raises(ToolError):
                advanced_search(
                    conditions=[{"field": "bad_field", "op": "contains", "value": "x"}]
                )

    def test_tool_invalid_op_raises_toolerror(self, tmp_path):
        """MCP tool converts ValueError (invalid op) to ToolError."""
        from zotpilot.tools.search import advanced_search
        from zotpilot.state import ToolError

        db = _create_search_db(tmp_path)
        mock_client = _make_client(tmp_path)

        with patch("zotpilot.tools.search._get_zotero", return_value=mock_client):
            with pytest.raises(ToolError):
                advanced_search(
                    conditions=[{"field": "title", "op": "bad_op", "value": "x"}]
                )

    def test_tool_returns_results(self, tmp_path):
        """MCP tool returns results list from underlying client."""
        from zotpilot.tools.search import advanced_search

        db = _create_search_db(tmp_path)
        _insert_item(db, 1, "ITEM01", "Neural Network Paper", 2022, [("A", "Smith")])

        mock_client = _make_client(tmp_path)

        with patch("zotpilot.tools.search._get_zotero", return_value=mock_client):
            results = advanced_search(
                conditions=[{"field": "title", "op": "contains", "value": "neural"}]
            )

        assert len(results) == 1
        assert results[0]["item_key"] == "ITEM01"

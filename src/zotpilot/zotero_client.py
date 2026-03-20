"""Zotero SQLite database client."""
import sqlite3
from html.parser import HTMLParser
from pathlib import Path
from .models import ZoteroItem


class _HTMLStripper(HTMLParser):
    """Strip HTML tags, keeping text content."""
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data):
        self._parts.append(data)

    def get_text(self):
        return "".join(self._parts).strip()


def _strip_html(html: str) -> str:
    """Convert HTML to plain text."""
    stripper = _HTMLStripper()
    stripper.feed(html)
    return stripper.get_text()


def _sqlite_uri(path: Path) -> str:
    """Build a SQLite URI for read-only access, cross-platform.

    On Windows, Path.as_uri() produces 'file:///C:/...' which SQLite accepts.
    On Unix, it produces 'file:///home/...' which also works.
    """
    return path.as_uri() + "?mode=ro&immutable=1"


class ZoteroClient:
    """
    Read-only access to Zotero's SQLite database.

    Key schema notes:
    - itemTypeID 1 = note, 14 = attachment (filter these for "real" items)
    - EAV pattern: itemData + itemDataValues + fields tables
    - Attachments: linkMode 0,1,4 = storage/{key}/, linkMode 2 = linked file
    """

    # Combined query: items with PDFs and all metadata
    ITEMS_WITH_PDFS_SQL = """
    WITH
        base_items AS (
            SELECT items.itemID, items."key" AS itemKey, items.itemTypeID
            FROM items
            WHERE items.itemTypeID NOT IN (1, 14)
              AND items.itemID NOT IN (SELECT itemID FROM deletedItems)
              AND items.libraryID = ?
        ),
        titles AS (
            SELECT itemData.itemID, itemDataValues.value AS title
            FROM itemData
            JOIN itemDataValues ON itemData.valueID = itemDataValues.valueID
            JOIN fields ON itemData.fieldID = fields.fieldID
            WHERE fields.fieldName = 'title'
        ),
        years AS (
            SELECT itemData.itemID, CAST(substr(itemDataValues.value, 1, 4) AS INTEGER) AS year
            FROM itemData
            JOIN itemDataValues ON itemData.valueID = itemDataValues.valueID
            JOIN fields ON itemData.fieldID = fields.fieldID
            WHERE fields.fieldName = 'date'
        ),
        authors AS (
            SELECT
                items.itemID,
                CASE
                    WHEN COUNT(*) = 1 THEN
                        MAX(creators.lastName) ||
                        CASE WHEN MAX(creators.firstName) IS NOT NULL AND MAX(creators.firstName) != ''
                             THEN ', ' || substr(MAX(creators.firstName), 1, 1) || '.'
                             ELSE '' END
                    ELSE
                        MAX(CASE WHEN itemCreators.orderIndex = 0 THEN creators.lastName END) || ' et al.'
                END AS authors
            FROM items
            JOIN itemCreators ON items.itemID = itemCreators.itemID
            JOIN creators ON itemCreators.creatorID = creators.creatorID
            GROUP BY items.itemID
        ),
        publications AS (
            SELECT itemData.itemID, itemDataValues.value AS publication
            FROM itemData
            JOIN itemDataValues ON itemData.valueID = itemDataValues.valueID
            JOIN fields ON itemData.fieldID = fields.fieldID
            WHERE fields.fieldName = 'publicationTitle'
        ),
        dois AS (
            SELECT itemData.itemID, itemDataValues.value AS doi
            FROM itemData
            JOIN itemDataValues ON itemData.valueID = itemDataValues.valueID
            JOIN fields ON itemData.fieldID = fields.fieldID
            WHERE fields.fieldName = 'DOI'
        ),
        item_tags AS (
            SELECT items.itemID, GROUP_CONCAT(tags.name, '; ') AS tags
            FROM items
            JOIN itemTags ON items.itemID = itemTags.itemID
            JOIN tags ON itemTags.tagID = tags.tagID
            GROUP BY items.itemID
        ),
        item_collections AS (
            SELECT items.itemID, GROUP_CONCAT(c.collectionName, '; ') AS collection_names
            FROM items
            JOIN collectionItems ci ON items.itemID = ci.itemID
            JOIN collections c ON ci.collectionID = c.collectionID
            GROUP BY items.itemID
        ),
        pdfs AS (
            SELECT
                COALESCE(ia.parentItemID, ia.itemID) AS parentItemID,
                items."key" AS attachmentKey,
                ia.linkMode,
                ia.path
            FROM itemAttachments ia
            JOIN items ON ia.itemID = items.itemID
            WHERE ia.contentType = 'application/pdf'
              AND ia.linkMode IN (0, 1, 2)
        )
    SELECT
        base_items.itemKey,
        COALESCE(titles.title, '[No Title]') AS title,
        COALESCE(authors.authors, '[No Author]') AS authors,
        years.year,
        COALESCE(publications.publication, '') AS publication,
        COALESCE(dois.doi, '') AS doi,
        COALESCE(item_tags.tags, '') AS tags,
        COALESCE(item_collections.collection_names, '') AS collections,
        pdfs.attachmentKey,
        pdfs.linkMode,
        pdfs.path
    FROM base_items
    LEFT JOIN titles ON base_items.itemID = titles.itemID
    LEFT JOIN years ON base_items.itemID = years.itemID
    LEFT JOIN authors ON base_items.itemID = authors.itemID
    LEFT JOIN publications ON base_items.itemID = publications.itemID
    LEFT JOIN dois ON base_items.itemID = dois.itemID
    LEFT JOIN item_tags ON base_items.itemID = item_tags.itemID
    LEFT JOIN item_collections ON base_items.itemID = item_collections.itemID
    JOIN pdfs ON base_items.itemID = pdfs.parentItemID
    ORDER BY base_items.itemID;
    """

    @classmethod
    def resolve_group_library_id(cls, data_dir: Path, group_id: int) -> int:
        """Look up the SQLite libraryID for a Zotero group."""
        db_path = Path(data_dir) / "zotero.sqlite"
        conn = sqlite3.connect(_sqlite_uri(db_path), uri=True)
        try:
            row = conn.execute(
                "SELECT libraryID FROM groups WHERE groupID = ?",
                (group_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Group {group_id} not found in Zotero database")
            return row[0]
        finally:
            conn.close()

    def __init__(self, data_dir: Path, library_id: int = 1):
        self.data_dir = Path(data_dir)
        self.db_path = self.data_dir / "zotero.sqlite"
        self.bbt_db_path = self.data_dir / "better-bibtex.sqlite"
        self.library_id = library_id
        if not self.db_path.exists():
            raise FileNotFoundError(f"Zotero database not found: {self.db_path}")

    def _load_citation_keys(self) -> dict[str, str]:
        """Load BetterBibTeX citation keys. Returns itemKey -> citationKey mapping."""
        if not self.bbt_db_path.exists():
            return {}
        conn = sqlite3.connect(_sqlite_uri(self.bbt_db_path), uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT itemKey, citationKey FROM citationkey").fetchall()
            return {row["itemKey"]: row["citationKey"] for row in rows}
        finally:
            conn.close()

    def _resolve_pdf_path(self, path_field: str | None, link_mode: int, attachment_key: str) -> Path | None:
        """
        Resolve attachment path based on linkMode.

        Link modes (from Zotero source):
        - 0: IMPORTED_FILE - storage/{attachmentKey}/{filename}
        - 1: IMPORTED_URL  - storage/{attachmentKey}/{filename}
        - 2: LINKED_FILE   - relative to linked attachment base dir (skip for now)
        - 3: LINKED_URL    - no local file
        - 4: EMBEDDED_IMAGE - storage/{attachmentKey}/{filename}
        """
        if path_field is None:
            return None

        if link_mode == 2:
            # Linked file - would need base dir from Zotero prefs
            # Skip for now, or make configurable
            return None

        if path_field.startswith("storage:"):
            filename = path_field[len("storage:"):]
            full_path = self.data_dir / "storage" / attachment_key / filename
            return full_path if full_path.exists() else None

        return None

    def get_all_items_with_pdfs(self) -> list[ZoteroItem]:
        """Get all Zotero items that have PDF attachments."""
        conn = sqlite3.connect(_sqlite_uri(self.db_path), uri=True)
        conn.row_factory = sqlite3.Row

        try:
            cursor = conn.execute(self.ITEMS_WITH_PDFS_SQL, (self.library_id,))
            rows = cursor.fetchall()
        finally:
            conn.close()

        citation_keys = self._load_citation_keys()
        return [self._row_to_item(row, citation_keys) for row in rows]

    def get_library_diagnostics(self) -> dict:
        """
        Return a breakdown of why items are/aren't indexable.

        Returns dict with:
          total_items: all non-note, non-attachment, non-deleted items
          no_attachment: items with no attachment at all
          non_pdf_attachment_types: {content_type: count} for items whose
              only attachments are non-PDF
          pdf_resolved: items with a PDF that resolves to a file on disk
          pdf_unresolved: list of (itemKey, title, reason) for PDF
              attachments that couldn't be resolved
        """
        conn = sqlite3.connect(_sqlite_uri(self.db_path), uri=True)
        conn.row_factory = sqlite3.Row

        try:
            # Total library items (non-note, non-attachment, non-deleted)
            total = conn.execute("""
                SELECT COUNT(*) FROM items
                WHERE itemTypeID NOT IN (1, 14)
                  AND itemID NOT IN (SELECT itemID FROM deletedItems)
                  AND libraryID = ?
            """, (self.library_id,)).fetchone()[0]

            # IDs of items that have at least one PDF attachment
            pdf_item_ids = set(r[0] for r in conn.execute("""
                SELECT DISTINCT COALESCE(ia.parentItemID, ia.itemID)
                FROM itemAttachments ia
                JOIN items ON COALESCE(ia.parentItemID, ia.itemID) = items.itemID
                WHERE ia.contentType = 'application/pdf'
                  AND ia.linkMode IN (0, 1, 2)
                  AND items.libraryID = ?
            """, (self.library_id,)).fetchall())

            # Items with only non-PDF attachments (excluding those that also have PDFs)
            if pdf_item_ids:
                placeholders = ",".join("?" * len(pdf_item_ids))
                non_pdf_rows = conn.execute(f"""
                    SELECT ia.contentType,
                           COUNT(DISTINCT COALESCE(ia.parentItemID, ia.itemID))
                    FROM itemAttachments ia
                    JOIN items base ON COALESCE(ia.parentItemID, ia.itemID) = base.itemID
                    WHERE base.itemTypeID NOT IN (1, 14)
                      AND base.itemID NOT IN (SELECT itemID FROM deletedItems)
                      AND base.libraryID = ?
                      AND COALESCE(ia.parentItemID, ia.itemID) NOT IN ({placeholders})
                    GROUP BY ia.contentType
                """, [self.library_id] + list(pdf_item_ids)).fetchall()
            else:
                non_pdf_rows = conn.execute("""
                    SELECT ia.contentType,
                           COUNT(DISTINCT COALESCE(ia.parentItemID, ia.itemID))
                    FROM itemAttachments ia
                    JOIN items base ON COALESCE(ia.parentItemID, ia.itemID) = base.itemID
                    WHERE base.itemTypeID NOT IN (1, 14)
                      AND base.itemID NOT IN (SELECT itemID FROM deletedItems)
                      AND base.libraryID = ?
                    GROUP BY ia.contentType
                """, (self.library_id,)).fetchall()

            non_pdf_types = {r[0] or "(null)": r[1] for r in non_pdf_rows}
            items_with_non_pdf = sum(non_pdf_types.values())

            # Get the full PDF attachment details to check resolution
            pdf_rows = conn.execute("""
                SELECT
                    base."key" AS itemKey,
                    COALESCE(t.value, '[No Title]') AS title,
                    ai."key" AS attachmentKey,
                    ia.linkMode,
                    ia.path
                FROM items base
                JOIN itemAttachments ia ON COALESCE(ia.parentItemID, ia.itemID) = base.itemID
                JOIN items ai ON ia.itemID = ai.itemID
                LEFT JOIN (
                    SELECT id2.itemID, idv.value
                    FROM itemData id2
                    JOIN itemDataValues idv ON id2.valueID = idv.valueID
                    JOIN fields f ON id2.fieldID = f.fieldID
                    WHERE f.fieldName = 'title'
                ) t ON base.itemID = t.itemID
                WHERE base.itemTypeID NOT IN (1, 14)
                  AND base.itemID NOT IN (SELECT itemID FROM deletedItems)
                  AND base.libraryID = ?
                  AND ia.contentType = 'application/pdf'
                  AND ia.linkMode IN (0, 1, 2)
            """, (self.library_id,)).fetchall()
        finally:
            conn.close()

        # Check which PDF paths resolve
        resolved = 0
        unresolved: list[tuple[str, str, str]] = []
        seen_keys = set()
        for r in pdf_rows:
            key = r["itemKey"]
            if key in seen_keys:
                continue  # multiple PDF attachments on same item
            seen_keys.add(key)
            path = self._resolve_pdf_path(r["path"], r["linkMode"], r["attachmentKey"])
            if path and path.exists():
                resolved += 1
            else:
                if r["linkMode"] == 2:
                    reason = "linked file (unsupported)"
                elif r["path"] is None:
                    reason = "no path in database"
                elif not r["path"].startswith("storage:"):
                    reason = f"unexpected path format: {r['path'][:50]}"
                else:
                    reason = "file missing from storage"
                unresolved.append((key, r["title"], reason))

        no_attachment = total - len(seen_keys) - items_with_non_pdf

        return {
            "total_items": total,
            "no_attachment": no_attachment,
            "non_pdf_attachment_types": non_pdf_types,
            "pdf_resolved": resolved,
            "pdf_unresolved": unresolved,
        }

    SINGLE_ITEM_SQL = """
    WITH
        base_items AS (
            SELECT items.itemID, items."key" AS itemKey, items.itemTypeID
            FROM items
            WHERE items.itemTypeID NOT IN (1, 14)
              AND items.itemID NOT IN (SELECT itemID FROM deletedItems)
              AND items."key" = ?
              AND items.libraryID = ?
        ),
        titles AS (
            SELECT itemData.itemID, itemDataValues.value AS title
            FROM itemData
            JOIN itemDataValues ON itemData.valueID = itemDataValues.valueID
            JOIN fields ON itemData.fieldID = fields.fieldID
            WHERE fields.fieldName = 'title'
        ),
        years AS (
            SELECT itemData.itemID, CAST(substr(itemDataValues.value, 1, 4) AS INTEGER) AS year
            FROM itemData
            JOIN itemDataValues ON itemData.valueID = itemDataValues.valueID
            JOIN fields ON itemData.fieldID = fields.fieldID
            WHERE fields.fieldName = 'date'
        ),
        authors AS (
            SELECT
                items.itemID,
                CASE
                    WHEN COUNT(*) = 1 THEN
                        MAX(creators.lastName) ||
                        CASE WHEN MAX(creators.firstName) IS NOT NULL AND MAX(creators.firstName) != ''
                             THEN ', ' || substr(MAX(creators.firstName), 1, 1) || '.'
                             ELSE '' END
                    ELSE
                        MAX(CASE WHEN itemCreators.orderIndex = 0 THEN creators.lastName END) || ' et al.'
                END AS authors
            FROM items
            JOIN itemCreators ON items.itemID = itemCreators.itemID
            JOIN creators ON itemCreators.creatorID = creators.creatorID
            GROUP BY items.itemID
        ),
        publications AS (
            SELECT itemData.itemID, itemDataValues.value AS publication
            FROM itemData
            JOIN itemDataValues ON itemData.valueID = itemDataValues.valueID
            JOIN fields ON itemData.fieldID = fields.fieldID
            WHERE fields.fieldName = 'publicationTitle'
        ),
        dois AS (
            SELECT itemData.itemID, itemDataValues.value AS doi
            FROM itemData
            JOIN itemDataValues ON itemData.valueID = itemDataValues.valueID
            JOIN fields ON itemData.fieldID = fields.fieldID
            WHERE fields.fieldName = 'DOI'
        ),
        item_tags AS (
            SELECT items.itemID, GROUP_CONCAT(tags.name, '; ') AS tags
            FROM items
            JOIN itemTags ON items.itemID = itemTags.itemID
            JOIN tags ON itemTags.tagID = tags.tagID
            GROUP BY items.itemID
        ),
        item_collections AS (
            SELECT items.itemID, GROUP_CONCAT(c.collectionName, '; ') AS collection_names
            FROM items
            JOIN collectionItems ci ON items.itemID = ci.itemID
            JOIN collections c ON ci.collectionID = c.collectionID
            GROUP BY items.itemID
        ),
        pdfs AS (
            SELECT
                COALESCE(ia.parentItemID, ia.itemID) AS parentItemID,
                items."key" AS attachmentKey,
                ia.linkMode,
                ia.path
            FROM itemAttachments ia
            JOIN items ON ia.itemID = items.itemID
            WHERE ia.contentType = 'application/pdf'
              AND ia.linkMode IN (0, 1, 2)
        )
    SELECT
        base_items.itemKey,
        COALESCE(titles.title, '[No Title]') AS title,
        COALESCE(authors.authors, '[No Author]') AS authors,
        years.year,
        COALESCE(publications.publication, '') AS publication,
        COALESCE(dois.doi, '') AS doi,
        COALESCE(item_tags.tags, '') AS tags,
        COALESCE(item_collections.collection_names, '') AS collections,
        pdfs.attachmentKey,
        pdfs.linkMode,
        pdfs.path
    FROM base_items
    LEFT JOIN titles ON base_items.itemID = titles.itemID
    LEFT JOIN years ON base_items.itemID = years.itemID
    LEFT JOIN authors ON base_items.itemID = authors.itemID
    LEFT JOIN publications ON base_items.itemID = publications.itemID
    LEFT JOIN dois ON base_items.itemID = dois.itemID
    LEFT JOIN item_tags ON base_items.itemID = item_tags.itemID
    LEFT JOIN item_collections ON base_items.itemID = item_collections.itemID
    LEFT JOIN pdfs ON base_items.itemID = pdfs.parentItemID;
    """

    NOTES_SQL = """
    SELECT
        notes.key AS noteKey,
        notes.itemID AS noteItemID,
        parent.key AS parentKey,
        COALESCE(titles.title, '[No Title]') AS parentTitle,
        itemNotes.note AS noteContent,
        notes.dateAdded,
        COALESCE(item_tags.tags, '') AS tags
    FROM items notes
    JOIN itemNotes ON notes.itemID = itemNotes.itemID
    LEFT JOIN items parent ON itemNotes.parentItemID = parent.itemID
    LEFT JOIN (
        SELECT itemData.itemID, itemDataValues.value AS title
        FROM itemData
        JOIN itemDataValues ON itemData.valueID = itemDataValues.valueID
        JOIN fields ON itemData.fieldID = fields.fieldID
        WHERE fields.fieldName = 'title'
    ) titles ON parent.itemID = titles.itemID
    LEFT JOIN (
        SELECT items.itemID, GROUP_CONCAT(tags.name, '; ') AS tags
        FROM items
        JOIN itemTags ON items.itemID = itemTags.itemID
        JOIN tags ON itemTags.tagID = tags.tagID
        GROUP BY items.itemID
    ) item_tags ON notes.itemID = item_tags.itemID
    WHERE notes.itemTypeID = 1
      AND notes.itemID NOT IN (SELECT itemID FROM deletedItems)
      AND notes.libraryID = ?
"""

    def _row_to_item(self, row, citation_keys: dict[str, str]) -> ZoteroItem:
        """Convert a database row to a ZoteroItem."""
        pdf_path = self._resolve_pdf_path(
            row["path"],
            row["linkMode"],
            row["attachmentKey"]
        ) if row["attachmentKey"] else None
        item_key = row["itemKey"]
        return ZoteroItem(
            item_key=item_key,
            title=row["title"],
            authors=row["authors"],
            year=row["year"],
            pdf_path=pdf_path,
            citation_key=citation_keys.get(item_key, ""),
            publication=row["publication"],
            doi=row["doi"],
            tags=row["tags"],
            collections=row["collections"],
        )

    def get_item(self, item_key: str) -> ZoteroItem | None:
        """Get a specific item by key (single-item SQL, not full table scan)."""
        conn = sqlite3.connect(_sqlite_uri(self.db_path), uri=True)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.execute(self.SINGLE_ITEM_SQL, (item_key, self.library_id))
            row = cursor.fetchone()
        finally:
            conn.close()

        if not row:
            return None

        citation_keys = self._load_citation_keys()
        return self._row_to_item(row, citation_keys)

    def get_notes(self, item_key: str | None = None, query: str | None = None, limit: int = 20) -> list[dict]:
        """Get notes, optionally filtered by parent item or content search."""
        conn = sqlite3.connect(_sqlite_uri(self.db_path), uri=True)
        conn.row_factory = sqlite3.Row
        try:
            sql = self.NOTES_SQL
            params = [self.library_id]
            conditions = []

            if item_key:
                conditions.append("parent.key = ?")
                params.append(item_key)

            if query:
                conditions.append("itemNotes.note LIKE ?")
                params.append(f"%{query}%")

            if conditions:
                sql += " AND " + " AND ".join(conditions)

            sql += " ORDER BY notes.dateAdded DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(sql, params).fetchall()
            return [
                {
                    "key": row["noteKey"],
                    "parent_key": row["parentKey"] or "",
                    "parent_title": row["parentTitle"] or "",
                    "tags": row["tags"],
                    "content": _strip_html(row["noteContent"] or ""),
                    "date_added": row["dateAdded"],
                }
                for row in rows
            ]
        finally:
            conn.close()

    # =========================================================================
    # Boolean Full-Text Search (Feature 3)
    # =========================================================================

    def search_fulltext(
        self,
        query: str,
        operator: str = "AND",
    ) -> set[str]:
        """Search Zotero's full-text index with boolean logic.

        Uses Zotero's fulltextWords/fulltextItemWords tables for exact word matching.

        Args:
            query: Space-separated search terms (case-insensitive)
            operator: "AND" (all terms required) or "OR" (any term matches)

        Returns:
            Set of item keys matching the query
        """
        words = [w.lower().strip() for w in query.split() if w.strip()]
        if not words:
            return set()

        conn = sqlite3.connect(_sqlite_uri(self.db_path), uri=True)
        try:
            if operator.upper() == "AND":
                return self._search_fulltext_and(conn, words)
            else:
                return self._search_fulltext_or(conn, words)
        finally:
            conn.close()

    def _search_fulltext_and(self, conn: sqlite3.Connection, words: list[str]) -> set[str]:
        """Find items containing ALL words."""
        placeholders = ",".join("?" * len(words))

        # First, get word IDs for all search terms
        word_rows = conn.execute(
            f"SELECT wordID, word FROM fulltextWords WHERE word IN ({placeholders})",
            words
        ).fetchall()

        if len(word_rows) != len(words):
            # Some words not in index - no results possible for AND
            return set()

        word_ids = [w[0] for w in word_rows]

        # Find items that have ALL of these word IDs
        sql = f"""
            SELECT items."key"
            FROM fulltextItemWords fiw
            JOIN itemAttachments ia ON fiw.itemID = ia.itemID
            JOIN items ON ia.parentItemID = items.itemID
            WHERE fiw.wordID IN ({placeholders})
              AND items.libraryID = ?
            GROUP BY items."key"
            HAVING COUNT(DISTINCT fiw.wordID) = ?
        """
        results = conn.execute(sql, word_ids + [self.library_id, len(words)]).fetchall()
        return {r[0] for r in results}

    def _search_fulltext_or(self, conn: sqlite3.Connection, words: list[str]) -> set[str]:
        """Find items containing ANY word."""
        placeholders = ",".join("?" * len(words))
        sql = f"""
            SELECT DISTINCT items."key"
            FROM fulltextWords fw
            JOIN fulltextItemWords fiw ON fw.wordID = fiw.wordID
            JOIN itemAttachments ia ON fiw.itemID = ia.itemID
            JOIN items ON ia.parentItemID = items.itemID
            WHERE fw.word IN ({placeholders})
              AND items.libraryID = ?
        """
        results = conn.execute(sql, words + [self.library_id]).fetchall()
        return {r[0] for r in results}

    # =========================================================================
    # Library Metadata Queries
    # =========================================================================

    def get_all_collections(self) -> list[dict]:
        """Get all Zotero collections with hierarchy."""
        conn = sqlite3.connect(_sqlite_uri(self.db_path), uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("""
                SELECT c.key, c.collectionName,
                       p.key AS parentKey
                FROM collections c
                LEFT JOIN collections p ON c.parentCollectionID = p.collectionID
                WHERE c.libraryID = ?
                ORDER BY c.collectionName
            """, (self.library_id,)).fetchall()
            return [
                {"key": r["key"], "name": r["collectionName"], "parent_key": r["parentKey"]}
                for r in rows
            ]
        finally:
            conn.close()

    def get_collection_items(self, collection_key: str, limit: int = 100) -> list[dict]:
        """Get items in a specific collection by collection key."""
        conn = sqlite3.connect(_sqlite_uri(self.db_path), uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("""
                SELECT i.key AS itemKey,
                       COALESCE(
                           (SELECT idv.value FROM itemData id
                            JOIN itemDataValues idv ON id.valueID = idv.valueID
                            JOIN fields f ON id.fieldID = f.fieldID
                            WHERE id.itemID = i.itemID AND f.fieldName = 'title'),
                           '[No Title]'
                       ) AS title,
                       COALESCE(
                           (SELECT GROUP_CONCAT(
                               CASE WHEN cr.firstName != '' THEN cr.lastName || ', ' || cr.firstName
                                    ELSE cr.lastName END, '; ')
                            FROM itemCreators ic
                            JOIN creators cr ON ic.creatorID = cr.creatorID
                            WHERE ic.itemID = i.itemID
                            ORDER BY ic.orderIndex),
                           '[No Author]'
                       ) AS authors,
                       (SELECT CAST(substr(idv.value, 1, 4) AS INTEGER) FROM itemData id
                        JOIN itemDataValues idv ON id.valueID = idv.valueID
                        JOIN fields f ON id.fieldID = f.fieldID
                        WHERE id.itemID = i.itemID AND f.fieldName = 'date') AS year,
                       COALESCE(
                           (SELECT idv.value FROM itemData id
                            JOIN itemDataValues idv ON id.valueID = idv.valueID
                            JOIN fields f ON id.fieldID = f.fieldID
                            WHERE id.itemID = i.itemID AND f.fieldName = 'publicationTitle'),
                           ''
                       ) AS publication,
                       (SELECT idv.value FROM itemData id
                        JOIN itemDataValues idv ON id.valueID = idv.valueID
                        JOIN fields f ON id.fieldID = f.fieldID
                        WHERE id.itemID = i.itemID AND f.fieldName = 'DOI') AS doi,
                       COALESCE(
                           (SELECT GROUP_CONCAT(t.name, '; ')
                            FROM itemTags it JOIN tags t ON it.tagID = t.tagID
                            WHERE it.itemID = i.itemID),
                           ''
                       ) AS tags,
                       (SELECT idv.value FROM itemData id
                        JOIN itemDataValues idv ON id.valueID = idv.valueID
                        JOIN fields f ON id.fieldID = f.fieldID
                        WHERE id.itemID = i.itemID AND f.fieldName = 'citationKey') AS citationKey
                FROM items i
                JOIN collectionItems ci ON i.itemID = ci.itemID
                JOIN collections c ON ci.collectionID = c.collectionID
                WHERE c.key = ?
                  AND i.itemTypeID NOT IN (1, 14)
                  AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
                  AND i.libraryID = ?
                LIMIT ?
            """, (collection_key, self.library_id, limit)).fetchall()
            return [
                {
                    "key": r["itemKey"],
                    "title": r["title"],
                    "authors": r["authors"],
                    "year": r["year"],
                    "publication": r["publication"],
                    "doi": r["doi"] or "",
                    "tags": r["tags"],
                    "citation_key": r["citationKey"] or "",
                }
                for r in rows
            ]
        finally:
            conn.close()

    def get_all_tags(self) -> list[dict]:
        """Get all tags with usage counts, sorted by frequency."""
        conn = sqlite3.connect(_sqlite_uri(self.db_path), uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("""
                SELECT tags.name, COUNT(itemTags.itemID) AS count
                FROM tags
                JOIN itemTags ON tags.tagID = itemTags.tagID
                JOIN items ON itemTags.itemID = items.itemID
                WHERE items.itemID NOT IN (SELECT itemID FROM deletedItems)
                  AND items.libraryID = ?
                GROUP BY tags.tagID, tags.name
                ORDER BY count DESC, tags.name
            """, (self.library_id,)).fetchall()
            return [{"name": r["name"], "count": r["count"]} for r in rows]
        finally:
            conn.close()

    def get_item_abstract(self, item_key: str) -> str:
        """Get abstract text for a specific item."""
        conn = sqlite3.connect(_sqlite_uri(self.db_path), uri=True)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("""
                SELECT idv.value
                FROM items i
                JOIN itemData id ON i.itemID = id.itemID
                JOIN itemDataValues idv ON id.valueID = idv.valueID
                JOIN fields f ON id.fieldID = f.fieldID
                WHERE i.key = ? AND i.libraryID = ? AND f.fieldName = 'abstractNote'
            """, (item_key, self.library_id)).fetchone()
            return row[0] if row else ""
        finally:
            conn.close()

    def get_libraries(self) -> list[dict]:
        """List available Zotero libraries (user library + groups)."""
        conn = sqlite3.connect(_sqlite_uri(self.db_path), uri=True)
        conn.row_factory = sqlite3.Row
        try:
            results = []
            # User library is always present
            user_count = conn.execute("""
                SELECT COUNT(*) FROM items
                WHERE libraryID = 1
                  AND itemTypeID NOT IN (1, 14)
                  AND itemID NOT IN (SELECT itemID FROM deletedItems)
            """).fetchone()[0]
            results.append({
                "library_id": "1",
                "library_type": "user",
                "name": "My Library",
                "item_count": user_count,
            })

            # Check for groups (groups link to libraries via libraryID; items link via items.libraryID)
            if self._table_exists(conn, "groups"):
                groups = conn.execute("""
                    SELECT g.groupID, g.name,
                           (SELECT COUNT(*) FROM items i
                            WHERE i.libraryID = g.libraryID
                              AND i.itemTypeID NOT IN (1, 14)
                              AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
                           ) AS itemCount
                    FROM groups g
                    ORDER BY g.name
                """).fetchall()
                for g in groups:
                    results.append({
                        "library_id": str(g["groupID"]),
                        "library_type": "group",
                        "name": g["name"],
                        "item_count": g["itemCount"],
                    })
            return results
        finally:
            conn.close()

    ADVANCED_SEARCH_BASE_SQL = """
    SELECT
        base.itemID,
        base.key AS itemKey,
        COALESCE(titles.title, '[No Title]') AS title,
        COALESCE(authors.authors, '[No Author]') AS authors,
        years.year,
        COALESCE(publications.publication, '') AS publication,
        COALESCE(dois.doi, '') AS doi,
        COALESCE(item_tags.tags, '') AS tags,
        COALESCE(item_collections.collection_names, '') AS collections
    FROM items base
    LEFT JOIN (
        SELECT itemData.itemID, itemDataValues.value AS title
        FROM itemData
        JOIN itemDataValues ON itemData.valueID = itemDataValues.valueID
        JOIN fields ON itemData.fieldID = fields.fieldID
        WHERE fields.fieldName = 'title'
    ) titles ON base.itemID = titles.itemID
    LEFT JOIN (
        SELECT itemData.itemID, CAST(substr(itemDataValues.value, 1, 4) AS INTEGER) AS year
        FROM itemData
        JOIN itemDataValues ON itemData.valueID = itemDataValues.valueID
        JOIN fields ON itemData.fieldID = fields.fieldID
        WHERE fields.fieldName = 'date'
    ) years ON base.itemID = years.itemID
    LEFT JOIN (
        SELECT
            items.itemID,
            CASE
                WHEN COUNT(*) = 1 THEN
                    MAX(creators.lastName) ||
                    CASE WHEN MAX(creators.firstName) IS NOT NULL AND MAX(creators.firstName) != ''
                         THEN ', ' || substr(MAX(creators.firstName), 1, 1) || '.'
                         ELSE '' END
                ELSE
                    MAX(CASE WHEN itemCreators.orderIndex = 0 THEN creators.lastName END) || ' et al.'
            END AS authors
        FROM items
        JOIN itemCreators ON items.itemID = itemCreators.itemID
        JOIN creators ON itemCreators.creatorID = creators.creatorID
        GROUP BY items.itemID
    ) authors ON base.itemID = authors.itemID
    LEFT JOIN (
        SELECT itemData.itemID, itemDataValues.value AS publication
        FROM itemData
        JOIN itemDataValues ON itemData.valueID = itemDataValues.valueID
        JOIN fields ON itemData.fieldID = fields.fieldID
        WHERE fields.fieldName = 'publicationTitle'
    ) publications ON base.itemID = publications.itemID
    LEFT JOIN (
        SELECT itemData.itemID, itemDataValues.value AS doi
        FROM itemData
        JOIN itemDataValues ON itemData.valueID = itemDataValues.valueID
        JOIN fields ON itemData.fieldID = fields.fieldID
        WHERE fields.fieldName = 'DOI'
    ) dois ON base.itemID = dois.itemID
    LEFT JOIN (
        SELECT items.itemID, GROUP_CONCAT(tags.name, '; ') AS tags
        FROM items
        JOIN itemTags ON items.itemID = itemTags.itemID
        JOIN tags ON itemTags.tagID = tags.tagID
        GROUP BY items.itemID
    ) item_tags ON base.itemID = item_tags.itemID
    LEFT JOIN (
        SELECT items.itemID, GROUP_CONCAT(c.collectionName, '; ') AS collection_names
        FROM items
        JOIN collectionItems ci ON items.itemID = ci.itemID
        JOIN collections c ON ci.collectionID = c.collectionID
        GROUP BY items.itemID
    ) item_collections ON base.itemID = item_collections.itemID
    WHERE base.itemTypeID NOT IN (1, 14)
      AND base.itemID NOT IN (SELECT itemID FROM deletedItems)
      AND base.libraryID = ?
"""

    _ADVANCED_FIELDS = {"title", "author", "year", "tag", "collection", "publication", "doi"}
    _ADVANCED_OPS = {"contains", "is", "isNot", "beginsWith", "gt", "lt"}

    def advanced_search(
        self,
        conditions: list[dict],
        match: str = "all",
        sort_by: str | None = None,
        sort_dir: str = "desc",
        limit: int = 50,
    ) -> list[dict]:
        """Multi-condition metadata search. Works without indexing.

        Args:
            conditions: List of {field, op, value} dicts.
                Fields: title, author, year, tag, collection, publication, doi
                Ops: contains, is, isNot, beginsWith, gt, lt
            match: "all" (AND) or "any" (OR)
            sort_by: Optional sort field: year, title, dateAdded
            sort_dir: "asc" or "desc"
            limit: Max results

        Returns:
            List of item dicts with metadata
        """
        if not conditions:
            return []

        where_clauses = []
        params = [self.library_id]  # First param is for libraryID = ? in base SQL

        for cond in conditions:
            field = cond.get("field", "")
            op = cond.get("op", "")
            value = cond.get("value", "")

            if field not in self._ADVANCED_FIELDS:
                raise ValueError(f"Invalid field: '{field}'. Valid: {', '.join(sorted(self._ADVANCED_FIELDS))}")
            if op not in self._ADVANCED_OPS:
                raise ValueError(f"Invalid op: '{op}'. Valid: {', '.join(sorted(self._ADVANCED_OPS))}")

            clause, clause_params = self._build_condition(field, op, str(value))
            where_clauses.append(clause)
            params.extend(clause_params)

        joiner = " AND " if match == "all" else " OR "
        combined = joiner.join(where_clauses)

        sql = self.ADVANCED_SEARCH_BASE_SQL + f" AND ({combined})"

        # Sorting
        sort_map = {"year": "years.year", "title": "titles.title", "dateAdded": "base.dateAdded"}
        if sort_by and sort_by in sort_map:
            direction = "ASC" if sort_dir == "asc" else "DESC"
            sql += f" ORDER BY {sort_map[sort_by]} {direction}"
        else:
            sql += " ORDER BY base.itemID DESC"

        sql += " LIMIT ?"
        params.append(limit)

        conn = sqlite3.connect(_sqlite_uri(self.db_path), uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(sql, params).fetchall()
            citation_keys = self._load_citation_keys()
            return [
                {
                    "item_key": row["itemKey"],
                    "doc_id": row["itemKey"],
                    "title": row["title"],
                    "authors": row["authors"],
                    "year": row["year"],
                    "publication": row["publication"],
                    "doi": row["doi"],
                    "tags": row["tags"],
                    "collections": row["collections"],
                    "citation_key": citation_keys.get(row["itemKey"], ""),
                }
                for row in rows
            ]
        finally:
            conn.close()

    def _build_condition(self, field: str, op: str, value: str) -> tuple[str, list]:
        """Build a single WHERE clause + params for advanced_search."""
        # Tag: use EXISTS with direct JOIN for exact match
        if field == "tag":
            if op == "is":
                return (
                    "EXISTS (SELECT 1 FROM itemTags JOIN tags ON itemTags.tagID = tags.tagID "
                    "WHERE itemTags.itemID = base.itemID AND tags.name = ?)",
                    [value],
                )
            elif op == "isNot":
                return (
                    "NOT EXISTS (SELECT 1 FROM itemTags JOIN tags ON itemTags.tagID = tags.tagID "
                    "WHERE itemTags.itemID = base.itemID AND tags.name = ?)",
                    [value],
                )
            elif op == "contains":
                return (
                    "EXISTS (SELECT 1 FROM itemTags JOIN tags ON itemTags.tagID = tags.tagID "
                    "WHERE itemTags.itemID = base.itemID AND tags.name LIKE ?)",
                    [f"%{value}%"],
                )
            elif op == "beginsWith":
                return (
                    "EXISTS (SELECT 1 FROM itemTags JOIN tags ON itemTags.tagID = tags.tagID "
                    "WHERE itemTags.itemID = base.itemID AND tags.name LIKE ?)",
                    [f"{value}%"],
                )
            else:
                raise ValueError(f"Op '{op}' not supported for field 'tag'")

        # Collection: use EXISTS with direct JOIN
        if field == "collection":
            if op == "is":
                return (
                    "EXISTS (SELECT 1 FROM collectionItems ci JOIN collections c ON ci.collectionID = c.collectionID "
                    "WHERE ci.itemID = base.itemID AND c.collectionName = ?)",
                    [value],
                )
            elif op == "isNot":
                return (
                    "NOT EXISTS (SELECT 1 FROM collectionItems ci JOIN collections c ON ci.collectionID = c.collectionID "
                    "WHERE ci.itemID = base.itemID AND c.collectionName = ?)",
                    [value],
                )
            elif op == "contains":
                return (
                    "EXISTS (SELECT 1 FROM collectionItems ci JOIN collections c ON ci.collectionID = c.collectionID "
                    "WHERE ci.itemID = base.itemID AND c.collectionName LIKE ?)",
                    [f"%{value}%"],
                )
            elif op == "beginsWith":
                return (
                    "EXISTS (SELECT 1 FROM collectionItems ci JOIN collections c ON ci.collectionID = c.collectionID "
                    "WHERE ci.itemID = base.itemID AND c.collectionName LIKE ?)",
                    [f"{value}%"],
                )
            else:
                raise ValueError(f"Op '{op}' not supported for field 'collection'")

        # Year: numeric comparison
        if field == "year":
            col = "years.year"
            if op == "is":
                return f"{col} = ?", [int(value)]
            elif op == "isNot":
                return f"{col} != ?", [int(value)]
            elif op == "gt":
                return f"{col} > ?", [int(value)]
            elif op == "lt":
                return f"{col} < ?", [int(value)]
            elif op == "contains":
                return f"CAST({col} AS TEXT) LIKE ?", [f"%{value}%"]
            elif op == "beginsWith":
                return f"CAST({col} AS TEXT) LIKE ?", [f"{value}%"]
            else:
                raise ValueError(f"Op '{op}' not supported for field 'year'")

        # Simple text fields: title, author, publication, doi
        col_map = {
            "title": "titles.title",
            "author": "authors.authors",
            "publication": "publications.publication",
            "doi": "dois.doi",
        }
        col = col_map[field]

        if op == "contains":
            return f"{col} LIKE ?", [f"%{value}%"]
        elif op == "is":
            return f"{col} = ?", [value]
        elif op == "isNot":
            return f"({col} IS NULL OR {col} != ?)", [value]
        elif op == "beginsWith":
            return f"{col} LIKE ?", [f"{value}%"]
        elif op == "gt":
            return f"{col} > ?", [value]
        elif op == "lt":
            return f"{col} < ?", [value]
        else:
            raise ValueError(f"Unsupported op: {op}")

    # =========================================================================
    # RSS Feeds
    # =========================================================================

    def _table_exists(self, conn: sqlite3.Connection, table_name: str) -> bool:
        """Check if a table exists in the database."""
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        return row is not None

    def get_feeds(self) -> list[dict]:
        """List all RSS feeds configured in Zotero."""
        conn = sqlite3.connect(_sqlite_uri(self.db_path), uri=True)
        conn.row_factory = sqlite3.Row
        try:
            if not self._table_exists(conn, "feeds"):
                return []
            rows = conn.execute("""
                SELECT f.libraryID, f.url, f.name,
                       f.lastCheck,
                       (SELECT COUNT(*) FROM feedItems fi
                        JOIN items i ON fi.itemID = i.itemID
                        WHERE i.libraryID = f.libraryID) AS itemCount
                FROM feeds f
                ORDER BY f.name
            """).fetchall()
            return [
                {
                    "library_id": row["libraryID"],
                    "name": row["name"] or "",
                    "url": row["url"] or "",
                    "item_count": row["itemCount"],
                    "last_check": row["lastCheck"] or "",
                }
                for row in rows
            ]
        finally:
            conn.close()

    def get_feed_items(self, library_id: int, limit: int = 20) -> list[dict]:
        """Get items from a specific RSS feed."""
        conn = sqlite3.connect(_sqlite_uri(self.db_path), uri=True)
        conn.row_factory = sqlite3.Row
        try:
            if not self._table_exists(conn, "feedItems"):
                return []
            rows = conn.execute("""
                SELECT fi.itemID, i.key, fi.guid, fi.readTime,
                       COALESCE(titles.title, '[No Title]') AS title,
                       COALESCE(authors.authors, '') AS authors,
                       COALESCE(abstracts.abstract, '') AS abstract,
                       COALESCE(urls.url, '') AS url,
                       i.dateAdded
                FROM feedItems fi
                JOIN items i ON fi.itemID = i.itemID
                LEFT JOIN (
                    SELECT id.itemID, idv.value AS title
                    FROM itemData id
                    JOIN itemDataValues idv ON id.valueID = idv.valueID
                    JOIN fields f ON id.fieldID = f.fieldID
                    WHERE f.fieldName = 'title'
                ) titles ON fi.itemID = titles.itemID
                LEFT JOIN (
                    SELECT ic.itemID,
                        CASE WHEN COUNT(*) = 1
                            THEN MAX(c.lastName)
                            ELSE MAX(CASE WHEN ic.orderIndex = 0 THEN c.lastName END) || ' et al.'
                        END AS authors
                    FROM itemCreators ic
                    JOIN creators c ON ic.creatorID = c.creatorID
                    GROUP BY ic.itemID
                ) authors ON fi.itemID = authors.itemID
                LEFT JOIN (
                    SELECT id.itemID, idv.value AS abstract
                    FROM itemData id
                    JOIN itemDataValues idv ON id.valueID = idv.valueID
                    JOIN fields f ON id.fieldID = f.fieldID
                    WHERE f.fieldName = 'abstractNote'
                ) abstracts ON fi.itemID = abstracts.itemID
                LEFT JOIN (
                    SELECT id.itemID, idv.value AS url
                    FROM itemData id
                    JOIN itemDataValues idv ON id.valueID = idv.valueID
                    JOIN fields f ON id.fieldID = f.fieldID
                    WHERE f.fieldName = 'url'
                ) urls ON fi.itemID = urls.itemID
                WHERE i.libraryID = ?
                ORDER BY i.dateAdded DESC
                LIMIT ?
            """, (library_id, limit)).fetchall()
            return [
                {
                    "key": row["key"],
                    "title": row["title"],
                    "authors": row["authors"],
                    "abstract": row["abstract"],
                    "url": row["url"],
                    "date_added": row["dateAdded"] or "",
                    "read": row["readTime"] is not None,
                }
                for row in rows
            ]
        finally:
            conn.close()

"""Zotero SQLite database client."""
import sqlite3
from pathlib import Path
from .models import ZoteroItem


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

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.db_path = self.data_dir / "zotero.sqlite"
        self.bbt_db_path = self.data_dir / "better-bibtex.sqlite"
        if not self.db_path.exists():
            raise FileNotFoundError(f"Zotero database not found: {self.db_path}")

    def _load_citation_keys(self) -> dict[str, str]:
        """Load BetterBibTeX citation keys. Returns itemKey -> citationKey mapping."""
        if not self.bbt_db_path.exists():
            return {}
        conn = sqlite3.connect(f"file:{self.bbt_db_path}?mode=ro&immutable=1", uri=True)
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
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row

        try:
            cursor = conn.execute(self.ITEMS_WITH_PDFS_SQL)
            rows = cursor.fetchall()
        finally:
            conn.close()

        citation_keys = self._load_citation_keys()

        items = []
        for row in rows:
            pdf_path = self._resolve_pdf_path(
                row["path"],
                row["linkMode"],
                row["attachmentKey"]
            )
            item_key = row["itemKey"]
            items.append(ZoteroItem(
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
            ))

        return items

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
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row

        try:
            # Total library items (non-note, non-attachment, non-deleted)
            total = conn.execute("""
                SELECT COUNT(*) FROM items
                WHERE itemTypeID NOT IN (1, 14)
                  AND itemID NOT IN (SELECT itemID FROM deletedItems)
            """).fetchone()[0]

            # IDs of items that have at least one PDF attachment
            pdf_item_ids = set(r[0] for r in conn.execute("""
                SELECT DISTINCT COALESCE(ia.parentItemID, ia.itemID)
                FROM itemAttachments ia
                WHERE ia.contentType = 'application/pdf'
                  AND ia.linkMode IN (0, 1, 2)
            """).fetchall())

            # Items with only non-PDF attachments (excluding those that also have PDFs)
            non_pdf_rows = conn.execute("""
                SELECT ia.contentType,
                       COUNT(DISTINCT COALESCE(ia.parentItemID, ia.itemID))
                FROM itemAttachments ia
                JOIN items base ON COALESCE(ia.parentItemID, ia.itemID) = base.itemID
                WHERE base.itemTypeID NOT IN (1, 14)
                  AND base.itemID NOT IN (SELECT itemID FROM deletedItems)
                  AND COALESCE(ia.parentItemID, ia.itemID) NOT IN ({})
                GROUP BY ia.contentType
            """.format(",".join(str(i) for i in pdf_item_ids) if pdf_item_ids else "NULL")
            ).fetchall()

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
                  AND ia.contentType = 'application/pdf'
                  AND ia.linkMode IN (0, 1, 2)
            """).fetchall()
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

    def get_item(self, item_key: str) -> ZoteroItem | None:
        """Get a specific item by key."""
        # For now, just filter from all items
        # Could optimize with a WHERE clause if needed
        all_items = self.get_all_items_with_pdfs()
        for item in all_items:
            if item.item_key == item_key:
                return item
        return None

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

        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro&immutable=1", uri=True)
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
            GROUP BY items."key"
            HAVING COUNT(DISTINCT fiw.wordID) = ?
        """
        results = conn.execute(sql, word_ids + [len(words)]).fetchall()
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
        """
        results = conn.execute(sql, words).fetchall()
        return {r[0] for r in results}

    # =========================================================================
    # Library Metadata Queries
    # =========================================================================

    def get_all_collections(self) -> list[dict]:
        """Get all Zotero collections with hierarchy."""
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("""
                SELECT c.key, c.collectionName,
                       p.key AS parentKey
                FROM collections c
                LEFT JOIN collections p ON c.parentCollectionID = p.collectionID
                ORDER BY c.collectionName
            """).fetchall()
            return [
                {"key": r["key"], "name": r["collectionName"], "parent_key": r["parentKey"]}
                for r in rows
            ]
        finally:
            conn.close()

    def get_all_tags(self) -> list[dict]:
        """Get all tags with usage counts, sorted by frequency."""
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("""
                SELECT tags.name, COUNT(itemTags.itemID) AS count
                FROM tags
                JOIN itemTags ON tags.tagID = itemTags.tagID
                JOIN items ON itemTags.itemID = items.itemID
                WHERE items.itemID NOT IN (SELECT itemID FROM deletedItems)
                GROUP BY tags.tagID, tags.name
                ORDER BY count DESC, tags.name
            """).fetchall()
            return [{"name": r["name"], "count": r["count"]} for r in rows]
        finally:
            conn.close()

    def get_item_abstract(self, item_key: str) -> str:
        """Get abstract text for a specific item."""
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("""
                SELECT idv.value
                FROM items i
                JOIN itemData id ON i.itemID = id.itemID
                JOIN itemDataValues idv ON id.valueID = idv.valueID
                JOIN fields f ON id.fieldID = f.fieldID
                WHERE i.key = ? AND f.fieldName = 'abstractNote'
            """, (item_key,)).fetchone()
            return row[0] if row else ""
        finally:
            conn.close()

"""Indexing pipeline orchestration."""
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from tqdm import tqdm

from .config import Config
from .embeddings import create_embedder
from .index_authority import (
    IndexJournal,
    mark_committed,
    mark_in_progress,
    reconcile_orphaned_index_docs,
    record_table_failure,
)
from .journal_ranker import JournalRanker
from .models import ZoteroItem
from .pdf import extract_document
from .pdf.chunker import Chunker
from .vector_store import VectorStore
from .zotero_client import ZoteroClient

logger = logging.getLogger(__name__)

_VISION_ESTIMATED_COST_PER_TABLE_USD = 0.01


def _config_hash(config: Config) -> str:
    """Hash of config values that affect indexed content.

    Changes to these values require re-indexing.
    """
    data = (
        f"{config.chunk_size}:"
        f"{config.chunk_overlap}:"
        f"{config.embedding_provider}:"
        f"{getattr(config, 'dashscope_embedding_endpoint', 'compatible')}:"
        f"{config.embedding_dimensions}:"
        f"{config.embedding_model}:"
        f"{config.ocr_language}:"
        f"{getattr(config, 'vision_enabled', True)}:"
        f"{getattr(config, 'vision_provider', 'anthropic')}:"
        f"{getattr(config, 'vision_model', '')}"
    )
    return hashlib.sha256(data.encode()).hexdigest()[:16]


@dataclass
class IndexResult:
    """Outcome of indexing a single document."""
    item_key: str
    title: str
    status: str          # "indexed", "failed", "empty", "skipped"
    reason: str = ""
    n_chunks: int = 0
    n_tables: int = 0
    quality_grade: str = ""  # A/B/C/D/F quality grade per document


class Indexer:
    """
    Orchestrates the full indexing pipeline.

    Pipeline: Zotero -> PDF -> Chunks -> Embeddings -> VectorStore
    """

    def __init__(self, config: Config):
        self.config = config
        self.zotero = ZoteroClient(config.zotero_data_dir)

        self.chunker = Chunker(
            chunk_size=config.chunk_size,
            overlap=config.chunk_overlap,
        )
        # Use factory to create appropriate embedder based on config
        self.embedder = create_embedder(config)
        self.store = VectorStore(config.chroma_db_path, self.embedder)
        self.journal_ranker = JournalRanker()
        self._empty_docs_path = config.chroma_db_path / "empty_docs.json"
        self._config_hash_path = config.chroma_db_path / "config_hash.txt"
        self.journal: IndexJournal | None = None
        vision_provider = getattr(config, "vision_provider", "anthropic")
        if vision_provider not in ("anthropic", "dashscope"):
            vision_provider = "anthropic"

        if config.vision_enabled and vision_provider == "dashscope" and config.dashscope_api_key:
            from .feature_extraction.dashscope_vision_api import DashScopeVisionAPI
            self._vision_api = DashScopeVisionAPI(
                api_key=config.dashscope_api_key,
                model=config.vision_model,
            )
        elif config.vision_enabled and config.anthropic_api_key:
            from .feature_extraction.vision_api import VisionAPI
            cost_log_path = config.chroma_db_path.parent / "vision_costs.json"
            self._vision_api = VisionAPI(
                api_key=config.anthropic_api_key,
                model=config.vision_model,
                cost_log_path=cost_log_path,
            )
        else:
            self._vision_api = None

    # ------------------------------------------------------------------
    # Empty-doc tracking (keyed by item_key -> pdf file hash)
    # ------------------------------------------------------------------

    def _load_empty_docs(self) -> dict[str, str]:
        """Load {item_key: pdf_hash} for docs that yielded no chunks."""
        if self._empty_docs_path.exists():
            return json.loads(self._empty_docs_path.read_text())
        return {}

    def _save_empty_docs(self, mapping: dict[str, str]) -> None:
        self._empty_docs_path.write_text(json.dumps(mapping, indent=2))

    def _estimate_vision_cost_usd(self, pending_tables: int) -> float:
        """Return a rough upper-bound estimate for batch vision cost."""
        return round(pending_tables * _VISION_ESTIMATED_COST_PER_TABLE_USD, 6)

    @staticmethod
    def _pdf_hash(path: Path) -> str:
        """Fast hash of first 64 KiB of a PDF (enough to detect replacement)."""
        h = hashlib.sha256()
        with open(path, "rb") as f:
            h.update(f.read(65536))
        return h.hexdigest()

    def _needs_reindex(self, item: ZoteroItem) -> tuple[bool, str]:
        """Check if a document needs (re)indexing based on PDF hash.

        Returns:
            (needs_reindex, reason) where reason is:
            - "new": Document not in index
            - "changed": PDF hash differs from stored hash
            - "no_hash": Document indexed without hash, needs reindex
            - "current": Document is up-to-date, no reindex needed
        """
        existing_meta = self.store.get_document_meta(item.item_key)
        if not existing_meta:
            return True, "new"

        stored_hash = existing_meta.get("pdf_hash")
        if not stored_hash:
            return True, "no_hash"

        current_hash = self._pdf_hash(item.pdf_path)
        if stored_hash != current_hash:
            return True, "changed"

        return False, "current"

    # ------------------------------------------------------------------
    # Main pipeline
    # ------------------------------------------------------------------

    def index_all(
        self,
        force_reindex: bool = False,
        limit: int | None = None,
        item_key: str | None = None,
        item_keys: list[str] | None = None,
        title_pattern: str | None = None,
        max_pages: int = 0,
        batch_size: int | None = None,
        journal: IndexJournal | None = None,
    ) -> dict:
        """
        Index all PDFs in Zotero library.

        Args:
            force_reindex: Delete and re-index matching items
            limit: Maximum number of items to index
            item_key: If provided, only index this specific Zotero item key
            title_pattern: If provided, only index items matching this regex pattern
            max_pages: Skip PDFs longer than N pages (0 = no limit)
            batch_size: Process at most N items per call (None/0 = all at once)
            journal: Optional IndexJournal for commit tracking

        Returns:
            Dict with 'results' (list[IndexResult]) and summary counts.
        """
        items = self.zotero.get_all_items_with_pdfs()
        skipped_no_pdf: list[dict] = []
        kept_items: list = []
        for i in items:
            if i.pdf_path and i.pdf_path.exists():
                kept_items.append(i)
            else:
                skipped_no_pdf.append({
                    "item_key": i.item_key,
                    "title": getattr(i, "title", None) or "",
                    "reason": "no_pdf_attachment",
                })
        items = kept_items
        if skipped_no_pdf:
            logger.info(
                "Indexer: skipped %d item(s) without PDF attachments", len(skipped_no_pdf)
            )
        # Deduplicate by item_key (defensive: SQL should already deduplicate)
        seen_keys: set[str] = set()
        unique_items: list[ZoteroItem] = []
        for item in items:
            if item.item_key not in seen_keys:
                seen_keys.add(item.item_key)
                unique_items.append(item)
        if len(unique_items) < len(items):
            logger.info(f"Deduplicated {len(items) - len(unique_items)} duplicate item(s)")
        items = unique_items
        current_doc_ids = {item.item_key for item in items}
        reconciliation = reconcile_orphaned_index_docs(self.store, current_doc_ids)
        if reconciliation["deleted_count"] > 0:
            logger.info(
                "Indexer: removed %d orphaned indexed document(s) not present in the current Zotero PDF library",
                reconciliation["deleted_count"],
            )
        logger.info(f"Discovered {len(items)} papers with PDFs in Zotero library")

        # Apply filters
        if item_key:
            items = [i for i in items if i.item_key == item_key]
            if not items:
                logger.error(f"No item found with key: {item_key}")
                return {
                    "results": [], "indexed": 0, "failed": 0, "empty": 0,
                    "skipped": 0, "already_indexed": 0, "skipped_no_pdf": [],
                }

        if item_keys:
            items = [i for i in items if i.item_key in item_keys]
            if not items:
                logger.error("No items found matching item_keys")
                return {
                    "results": [], "indexed": 0, "failed": 0, "empty": 0,
                    "skipped": 0, "already_indexed": 0, "skipped_no_pdf": [],
                }

        if title_pattern:
            import re
            if len(title_pattern) > 200:
                raise ValueError(f"title_pattern too long ({len(title_pattern)} chars, max 200)")
            try:
                pattern = re.compile(title_pattern, re.IGNORECASE)
            except re.error as e:
                raise ValueError(f"Invalid regex in title_pattern: {e}")
            items = [i for i in items if pattern.search(i.title)]
            logger.info(f"Title filter: {len(items)} papers match '{title_pattern}'")

        if journal is not None and journal.in_progress:
            in_progress_first = set(journal.in_progress.keys())
            items = sorted(items, key=lambda item: (item.item_key not in in_progress_first, item.item_key))

        if limit:
            items = items[:limit]
            logger.info(f"Limit applied: processing at most {limit} papers")

        deferred_by_batch = False
        if batch_size is not None and batch_size > 0 and len(items) > batch_size:
            deferred_by_batch = True
            items = items[:batch_size]

        if force_reindex:
            indexed_ids = set()
            empty_docs: dict[str, str] = {}
        else:
            indexed_ids = self.store.get_indexed_doc_ids()
            empty_docs = self._load_empty_docs()

        # Check for config mismatch
        config_hash = _config_hash(self.config)
        stored_hash = None
        if self._config_hash_path.exists():
            stored_hash = self._config_hash_path.read_text().strip()

        if stored_hash and stored_hash != config_hash and not force_reindex:
            logger.warning(
                "Config has changed since last index (chunk_size, overlap, embedding, or section settings). "
                "Run with --force to re-index, otherwise results may be inconsistent."
            )

        # Store journal reference for use in indexing pipeline
        self.journal = journal

        results: list[IndexResult] = []
        to_index: list[ZoteroItem] = []
        reindex_reasons: dict[str, str] = {}
        for item in items:
            if journal is not None and item.item_key in journal.in_progress and item.item_key in indexed_ids:
                reindex_reasons[item.item_key] = "stale_in_progress"
                logger.info(f"Reindexing {item.item_key}: stale in-progress journal entry")
            elif item.item_key in indexed_ids:
                needs_reindex, reason = self._needs_reindex(item)
                if needs_reindex:
                    reindex_reasons[item.item_key] = reason
                    logger.info(f"Reindexing {item.item_key}: {reason}")
                else:
                    continue

            if item.item_key in empty_docs:
                current_hash = self._pdf_hash(item.pdf_path)
                if current_hash == empty_docs[item.item_key]:
                    results.append(IndexResult(
                        item.item_key, item.title, "skipped",
                        reason="no extractable text (unchanged PDF)"))
                    continue
                else:
                    del empty_docs[item.item_key]
                    reindex_reasons[item.item_key] = "changed"

            to_index.append(item)

        # Filter long documents
        long_items: list[tuple[ZoteroItem, int]] = []
        if max_pages and max_pages > 0:
            import fitz
            short_items = []
            for item in to_index:
                try:
                    doc = fitz.open(str(item.pdf_path))
                    pages = len(doc)
                    doc.close()
                    if pages > max_pages:
                        long_items.append((item, pages))
                        results.append(IndexResult(
                            item.item_key, item.title, "skipped",
                            reason=f"too long ({pages} pages, max {max_pages})"))
                    else:
                        short_items.append(item)
                except Exception:
                    short_items.append(item)
            to_index = short_items

        # Batch slicing: record total before cutting
        total_to_index = len(to_index)

        keys_requiring_delete = {
            item.item_key
            for item in to_index
            if reindex_reasons.get(item.item_key) in {"changed", "no_hash"}
        }
        if journal is not None and journal.in_progress:
            keys_requiring_delete |= {
                item.item_key for item in to_index
                if item.item_key in journal.in_progress and item.item_key in indexed_ids
            }

        # Deferred force_reindex deletion: only delete docs in current batch
        if force_reindex:
            existing = self.store.get_indexed_doc_ids()
            keys_to_delete = {item.item_key for item in to_index}
            for doc_id in keys_to_delete & existing:
                self.store.delete_document(doc_id)
        else:
            for doc_id in keys_requiring_delete:
                self.store.delete_document(doc_id)
                indexed_ids.discard(doc_id)

        reindex_count = len(reindex_reasons)
        n_skipped = sum(1 for r in results if r.status == "skipped")
        logger.info(
            f"Index plan: {len(to_index)} to index, "
            f"{reindex_count} to reindex (PDF changed), "
            f"{len(indexed_ids)} already indexed, "
            f"{n_skipped} skipped (empty/unchanged)"
        )
        if not to_index:
            logger.info("Nothing to index \u2014 all papers are up to date")

        quality_distribution: dict[str, int] = {"A": 0, "B": 0, "C": 0, "D": 0, "F": 0}
        aggregated_extraction_stats = {
            "total_pages": 0,
            "text_pages": 0,
            "ocr_pages": 0,
            "empty_pages": 0,
        }

        # ---- Phase 1: Extract all documents (vision specs collected but deferred) ----
        figures_dir = self.config.chroma_db_path.parent / "figures"
        doc_extractions: dict[str, tuple[ZoteroItem, object]] = {}  # item_key -> (item, extraction)

        total_to_extract = len(to_index)
        extraction_times: list[float] = []
        phase1_start = time.perf_counter()
        log_interval = 5  # log every N papers

        for i, item in enumerate(tqdm(to_index, desc="Extracting"), 1):
            t0 = time.perf_counter()
            try:
                if self.journal is not None and item.item_key in reindex_reasons:
                    mark_in_progress(self.journal, item.item_key)
                logger.debug(
                    f"Starting extraction {item.item_key}: "
                    f"title={item.title!r}, pdf={item.pdf_path}"
                )
                extraction = extract_document(
                    item.pdf_path,
                    write_images=True,
                    images_dir=figures_dir,
                    ocr_language=self.config.ocr_language,
                    vision_api=self._vision_api,
                )
                doc_extractions[item.item_key] = (item, extraction)
            except Exception as e:
                logger.error(f"Failed to extract {item.item_key}: {type(e).__name__}: {e}")
                results.append(IndexResult(
                    item.item_key, item.title, "failed",
                    reason=f"{type(e).__name__}: {e}"))

            elapsed = time.perf_counter() - t0
            extraction_times.append(elapsed)
            logger.info("Extraction timing [%s]: total=%.1fs", item.item_key, elapsed)

            if i % log_interval == 0 or i == total_to_extract:
                avg_time = sum(extraction_times) / len(extraction_times)
                remaining = total_to_extract - i
                eta_secs = avg_time * remaining
                if eta_secs >= 60:
                    eta_str = f"{eta_secs / 60:.1f}m"
                else:
                    eta_str = f"{eta_secs:.0f}s"
                logger.info(
                    f"Extraction: {i}/{total_to_extract} papers "
                    f"({avg_time:.1f}s avg, ETA {eta_str})"
                )

        phase1_elapsed = time.perf_counter() - phase1_start
        if total_to_extract > 0:
            logger.info(
                f"Extraction complete: {total_to_extract} papers in "
                f"{phase1_elapsed:.1f}s ({phase1_elapsed / total_to_extract:.1f}s avg)"
            )

        # ---- Phase 2: Resolve vision batch (one API call for all papers) ----
        vision_pending_tables = 0
        vision_estimated_cost_usd = 0.0
        vision_budget_skipped = False
        vision_skip_reason = ""
        if self._vision_api and doc_extractions:
            from .pdf.extractor import _finalize_document_no_tables, resolve_pending_vision
            pending_count = sum(
                len(v[1].pending_vision.specs)
                for v in doc_extractions.values()
                if v[1].pending_vision is not None and v[1].pending_vision.specs
            )
            vision_pending_tables = pending_count
            vision_estimated_cost_usd = self._estimate_vision_cost_usd(pending_count)
            pending_docs = sum(
                1 for v in doc_extractions.values()
                if v[1].pending_vision is not None and v[1].pending_vision.specs
            )
            over_table_cap = (
                self.config.vision_max_tables_per_run is not None
                and pending_count > self.config.vision_max_tables_per_run
            )
            over_cost_cap = (
                self.config.vision_max_cost_usd is not None
                and vision_estimated_cost_usd > self.config.vision_max_cost_usd
            )
            if pending_count > 0:
                logger.info(
                    f"Vision: {pending_count} tables across {pending_docs} papers "
                    f"queued for Batch API (up to 3 waves, est. 10-30min per wave)"
                )
            if pending_count > 0 and (over_table_cap or over_cost_cap):
                reasons = []
                if over_table_cap:
                    reasons.append(f"table cap {self.config.vision_max_tables_per_run}")
                if over_cost_cap:
                    reasons.append(
                        "estimated cost "
                        f"${vision_estimated_cost_usd:.2f} exceeds cap "
                        f"${self.config.vision_max_cost_usd:.2f}"
                    )
                vision_budget_skipped = True
                vision_skip_reason = "; ".join(reasons)
                logger.warning("Skipping vision batch: %s", vision_skip_reason)
                for _item, extraction in doc_extractions.values():
                    if extraction.pending_vision is not None:
                        _finalize_document_no_tables(extraction)
            else:
                phase2_start = time.perf_counter()
                resolve_pending_vision(
                    {k: v[1] for k, v in doc_extractions.items()},
                    self._vision_api,
                )
                phase2_elapsed = time.perf_counter() - phase2_start
                if pending_count > 0:
                    logger.info(
                        f"Vision complete: {pending_count} tables in "
                        f"{phase2_elapsed / 60:.1f}min ({phase2_elapsed / max(pending_count, 1):.1f}s avg/table)"
                    )

        # ---- Phase 3: Index each document (chunk, store, etc.) ----
        total_to_store = len(doc_extractions)
        index_times: list[float] = []
        phase3_start = time.perf_counter()
        if total_to_store > 0:
            logger.info(f"Indexing: chunking and storing {total_to_store} papers")

        for idx, (item_key, (item, extraction)) in enumerate(doc_extractions.items(), 1):
            t0 = time.perf_counter()
            try:
                n_chunks, n_tables, reason, extraction_stats, quality_grade = self._index_extraction(
                    item, extraction, self.journal
                )

                # Aggregate extraction stats
                for key in ["total_pages", "text_pages", "ocr_pages", "empty_pages"]:
                    aggregated_extraction_stats[key] += extraction_stats.get(key, 0)

                # Track quality distribution
                if quality_grade in quality_distribution:
                    quality_distribution[quality_grade] += 1

                if n_chunks > 0:
                    results.append(IndexResult(
                        item.item_key, item.title, "indexed",
                        n_chunks=n_chunks, n_tables=n_tables,
                        quality_grade=quality_grade))
                else:
                    empty_docs[item.item_key] = self._pdf_hash(item.pdf_path)
                    results.append(IndexResult(
                        item.item_key, item.title, "empty", reason=reason,
                        quality_grade=quality_grade))
                logger.debug(f"Completed {item.item_key}: {n_chunks} chunks, {n_tables} tables, quality {quality_grade}")  # noqa: E501
            except Exception as e:
                logger.error(f"Failed to index {item.item_key}: {type(e).__name__}: {e}")
                results.append(IndexResult(
                    item.item_key, item.title, "failed",
                    reason=f"{type(e).__name__}: {e}"))

            index_times.append(time.perf_counter() - t0)
            if idx % log_interval == 0 or idx == total_to_store:
                avg_t = sum(index_times) / len(index_times)
                remaining = total_to_store - idx
                eta_secs = avg_t * remaining
                eta_str = f"{eta_secs / 60:.1f}m" if eta_secs >= 60 else f"{eta_secs:.0f}s"
                logger.info(
                    f"Indexing: {idx}/{total_to_store} papers "
                    f"({avg_t:.1f}s avg, ETA {eta_str})"
                )

        phase3_elapsed = time.perf_counter() - phase3_start
        if total_to_store > 0:
            logger.info(
                f"Indexing complete: {total_to_store} papers in "
                f"{phase3_elapsed:.1f}s ({phase3_elapsed / total_to_store:.1f}s avg)"
            )

        self._save_empty_docs(empty_docs)

        counts = {
            "indexed": sum(1 for r in results if r.status == "indexed"),
            "failed": sum(1 for r in results if r.status == "failed"),
            "empty": sum(1 for r in results if r.status == "empty"),
            "skipped": sum(1 for r in results if r.status == "skipped"),
            "already_indexed": len(indexed_ids),
            "quality_distribution": quality_distribution,
            "extraction_stats": aggregated_extraction_stats,
        }

        counts["skipped_no_pdf"] = skipped_no_pdf
        counts["skipped_long"] = len(long_items)
        counts["long_documents"] = [
            {"item_key": item.item_key, "title": item.title, "pages": pages}
            for item, pages in long_items
        ]

        # Batch metadata
        counts["total_to_index"] = total_to_index
        counts["batch_size"] = batch_size
        counts["has_more"] = deferred_by_batch or (total_to_index > len(to_index) if batch_size else False)
        counts["vision_pending_tables"] = vision_pending_tables
        counts["vision_estimated_cost_usd"] = vision_estimated_cost_usd
        counts["vision_budget_skipped"] = vision_budget_skipped
        if vision_skip_reason:
            counts["vision_skip_reason"] = vision_skip_reason

        # Save config hash after successful indexing
        if counts["indexed"] > 0 or counts["already_indexed"] > 0:
            self._config_hash_path.write_text(config_hash)

        # Deletions can land in Zotero while a long indexing run is already in
        # progress. Reconcile once more at the end so a document that was still
        # visible at startup but moved to trash during this run is removed from
        # Chroma immediately, without requiring a second index_library call.
        final_current_doc_ids = {
            item.item_key
            for item in self.zotero.get_all_items_with_pdfs()
            if item.pdf_path and item.pdf_path.exists()
        }
        final_reconciliation = reconcile_orphaned_index_docs(self.store, final_current_doc_ids)
        if final_reconciliation["deleted_count"] > 0:
            logger.info(
                "Indexer: removed %d orphaned indexed document(s) after refresh of Zotero library state",
                final_reconciliation["deleted_count"],
            )

        return {"results": results, **counts}

    def _index_document_detailed(self, item: ZoteroItem) -> tuple[int, int, str, dict, str]:
        """
        Extract and index a single document (includes vision resolution).

        For batch indexing use index_all() which batches vision across all docs.
        """
        if item.pdf_path is None or not item.pdf_path.exists():
            raise FileNotFoundError(f"PDF not found for {item.item_key}")

        figures_dir = self.config.chroma_db_path.parent / "figures"
        extraction = extract_document(
            item.pdf_path,
            write_images=True,
            images_dir=figures_dir,
            ocr_language=self.config.ocr_language,
            vision_api=self._vision_api,
        )

        # Resolve vision for this single document
        if extraction.pending_vision is not None and self._vision_api:
            from .pdf.extractor import _finalize_document_no_tables, resolve_pending_vision

            pending_count = len(extraction.pending_vision.specs)
            estimated_cost = self._estimate_vision_cost_usd(pending_count)
            over_table_cap = (
                self.config.vision_max_tables_per_run is not None
                and pending_count > self.config.vision_max_tables_per_run
            )
            over_cost_cap = (
                self.config.vision_max_cost_usd is not None
                and estimated_cost > self.config.vision_max_cost_usd
            )
            if pending_count > 0 and (over_table_cap or over_cost_cap):
                _finalize_document_no_tables(extraction)
            else:
                resolve_pending_vision({item.item_key: extraction}, self._vision_api)

        return self._index_extraction(item, extraction, self.journal)

    def _index_extraction(
        self,
        item: ZoteroItem,
        extraction,
        journal: IndexJournal | None = None,
    ) -> tuple[int, int, str, dict, str]:
        """
        Index a pre-extracted document (vision already resolved).

        Returns:
            (n_chunks, n_tables, reason, extraction_stats, quality_grade)
        """
        item_key = item.item_key

        # Mark in_progress before any persistence
        if journal is not None:
            mark_in_progress(journal, item_key)

        if not extraction.pages:
            return 0, 0, "PDF has 0 pages (corrupt or unreadable)", extraction.stats, "F"

        total_chars = sum(len(p.markdown) for p in extraction.pages)
        quality_grade = extraction.quality_grade

        logger.debug(
            f"  Extracted {len(extraction.pages)} pages, {total_chars} chars "
            f"(text: {extraction.stats['text_pages']}, "
            f"ocr: {extraction.stats['ocr_pages']}, "
            f"empty: {extraction.stats['empty_pages']}, "
            f"quality: {quality_grade})"
        )

        if total_chars == 0:
            return 0, 0, f"{len(extraction.pages)} pages but no text", extraction.stats, quality_grade

        # Chunk using the new interface
        chunk_started = time.perf_counter()
        chunks = self.chunker.chunk(
            extraction.full_markdown,
            extraction.pages,
            extraction.sections,
        )
        chunk_elapsed = time.perf_counter() - chunk_started
        if not chunks:
            return 0, 0, f"{len(extraction.pages)} pages, {total_chars} chars but no chunks created", extraction.stats, quality_grade  # noqa: E501
        logger.debug(f"  Created {len(chunks)} chunks")

        # Look up journal quartile
        journal_quartile = self.journal_ranker.lookup(item.publication)

        # Store text chunks
        doc_meta = {
            "title": item.title,
            "authors": item.authors,
            "year": item.year,
            "citation_key": item.citation_key,
            "publication": item.publication,
            "journal_quartile": journal_quartile or "",
            "doi": item.doi,
            "tags": item.tags,
            "collections": item.collections,
            "pdf_hash": self._pdf_hash(item.pdf_path),
            "quality_grade": quality_grade,
        }
        store_started = time.perf_counter()
        self.store.add_chunks(item.item_key, doc_meta, chunks)
        store_elapsed = time.perf_counter() - store_started

        # Mark committed after text-chunk persistence
        if journal is not None:
            mark_committed(journal, item_key)
        logger.info(
            "Index timings [%s]: chunk=%.1fs store=%.1fs chunks=%d",
            item_key,
            chunk_elapsed,
            store_elapsed,
            len(chunks),
        )

        # Build reference map for table/figure placement
        from .pdf.reference_matcher import match_references
        ref_map = match_references(extraction.full_markdown, chunks, extraction.tables, extraction.figures)

        # Enrich tables/figures with reference context.
        # Only for real captions (Table N / Figure N), not synthetic ones.
        import re

        from .pdf.extractor import SYNTHETIC_CAPTION_PREFIX
        from .pdf.reference_matcher import get_reference_context
        _TAB_NUM_RE = re.compile(r"(?:Table|Tab\.?)\s+(\d+)", re.IGNORECASE)
        _FIG_NUM_RE = re.compile(r"(?:Figure|Fig\.?)\s+(\d+)", re.IGNORECASE)
        for table in extraction.tables:
            if table.artifact_type:
                continue  # skip layout artifacts
            if table.caption and not table.caption.startswith(SYNTHETIC_CAPTION_PREFIX):
                m = _TAB_NUM_RE.search(table.caption)
                if m:
                    ctx = get_reference_context(extraction.full_markdown, chunks, ref_map, "table", int(m.group(1)))
                    table.reference_context = ctx
        for fig in extraction.figures:
            if fig.caption and not fig.caption.startswith(SYNTHETIC_CAPTION_PREFIX):
                m = _FIG_NUM_RE.search(fig.caption)
                if m:
                    ctx = get_reference_context(extraction.full_markdown, chunks, ref_map, "figure", int(m.group(1)))
                    fig.reference_context = ctx

        # Store tables if enabled (skip layout artifacts)
        n_tables = 0
        real_tables = [t for t in extraction.tables if not t.artifact_type]
        n_artifacts = len(extraction.tables) - len(real_tables)
        if real_tables:
            try:
                self.store.add_tables(item_key, doc_meta, real_tables, ref_map=ref_map)
                n_tables = len(real_tables)
            except Exception as e:
                logger.warning(f"Table storage failed for {item_key}: {e}")
                if journal is not None:
                    record_table_failure(journal, item_key, f"table storage: {e}")
        if n_artifacts:
            logger.debug(f"  Skipped {n_artifacts} artifact table(s)")
        logger.debug(f"  Extracted {n_tables} tables")

        # Store figures if enabled
        n_figures = 0
        if extraction.figures:
            try:
                self.store.add_figures(item_key, doc_meta, extraction.figures, ref_map=ref_map)
                n_figures = len(extraction.figures)
                logger.debug(f"  Extracted {n_figures} figures")
            except Exception as e:
                logger.warning(f"Figure storage failed for {item_key}: {e}")
                if journal is not None:
                    record_table_failure(journal, item_key, f"figure storage: {e}")

        logger.debug(f"Indexed {item.item_key}: {len(chunks)} chunks, {n_tables} tables, {n_figures} figures, quality {quality_grade}")  # noqa: E501
        return len(chunks), n_tables, "", extraction.stats, quality_grade

    def index_document(self, item: ZoteroItem) -> int:
        """Index a single document. Returns number of chunks created."""
        n_chunks, _n_tables, _reason, _stats, _quality = self._index_document_detailed(item)
        return n_chunks

    def reindex_document(self, item_key: str) -> int:
        """Re-index a specific document."""
        self.store.delete_document(item_key)
        item = self.zotero.get_item(item_key)
        if item:
            return self.index_document(item)
        return 0

    def get_stats(self) -> dict:
        """Get index statistics."""
        current_doc_ids = {item.item_key for item in self.zotero.get_all_items_with_pdfs() if item.pdf_path and item.pdf_path.exists()}  # noqa: E501
        doc_ids = self.store.get_indexed_doc_ids() & current_doc_ids
        total_chunks = self.store.count_chunks_for_doc_ids(doc_ids)
        return {
            "total_documents": len(doc_ids),
            "total_chunks": total_chunks,
            "avg_chunks_per_doc": round(total_chunks / len(doc_ids), 1) if doc_ids else 0,
        }

    def get_library_diagnostics(self) -> dict:
        """Delegate to ZoteroClient for library-wide diagnostics."""
        return self.zotero.get_library_diagnostics()

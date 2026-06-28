"""Indexing pipeline orchestration."""
import hashlib
import json
import logging
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from tqdm import tqdm

from .config import Config, _config_hash, _vision_only_drift
from .embeddings import create_embedder
from .embeddings.base import RateLimitError
from .index_authority import (
    IndexJournal,
    clear_table_failure,
    mark_committed,
    mark_in_progress,
    reconcile_orphaned_index_docs,
    record_table_failure,
)
from .index_progress import ProgressSink, emit_progress
from .journal_ranker import JournalRanker
from .models import ZoteroItem
from .pdf import extract_document
from .pdf.chunker import Chunker
from .vector_store import VectorStore
from .zotero_client import ZoteroClient

logger = logging.getLogger(__name__)

_VISION_ESTIMATED_COST_PER_TABLE_USD = 0.01

# Generic provider-agnostic backstop: abort after this many consecutive
# same-signature doc failures even when no RateLimitError was classified.
CONSECUTIVE_FAILURE_ABORT_THRESHOLD = 3

# Rate-limit retry: on a typed RateLimitError, wait the provider-supplied
# retry_after (capped) and retry the SAME paper up to N times before letting the
# error propagate to the Phase-3 fail-fast abort. This consumes the retry_after
# that the embedding layer already parses (previously parsed but discarded).
RATE_LIMIT_MAX_RETRIES = 5
RATE_LIMIT_DEFAULT_WAIT_SECONDS = 30.0  # used when a 429 carries no retry_after
RATE_LIMIT_MAX_WAIT_SECONDS = 120.0  # per-attempt cap so a bogus retry_after can't hang the run


def _failure_signature(e: Exception) -> str:
    """Normalize volatile tokens so two same-cause failures compare equal.

    Strips ``Batch N/M`` and char/text counts so e.g. a quota failure on
    "Batch 3/9 ... (32 texts, 5000 chars)" matches one on "Batch 7/9 ...".
    """
    msg = re.sub(r"[Bb]atch \d+/\d+", "Batch N/M", str(e))
    msg = re.sub(r"\d+\s*(texts?|chars?)", r"N \1", msg)
    return f"{type(e).__name__}:{msg}"


_PROGRESS_COUNT_KEYS = (
    "indexed",
    "failed",
    "empty",
    "skipped",
    "already_indexed",
    "total_to_index",
    "batch_size",
    "has_more",
    "rate_limited_abort",
    "systemic_abort",
    "not_indexed_due_to_abort",
    "skipped_long",
    "vision_pending_tables",
    "vision_estimated_cost_usd",
    "vision_budget_skipped",
)


def _progress_counts(counts: dict) -> dict[str, object]:
    """Keep run-finished progress payload compact and JSON-friendly."""
    payload: dict[str, object] = {}
    for key in _PROGRESS_COUNT_KEYS:
        if key in counts:
            payload[key] = counts[key]
    if "quality_distribution" in counts:
        payload["quality_distribution"] = counts["quality_distribution"]
    if "extraction_stats" in counts:
        payload["extraction_stats"] = counts["extraction_stats"]
    if "skipped_no_pdf" in counts:
        payload["skipped_no_pdf_count"] = len(counts["skipped_no_pdf"])
    return payload


class ConfigDriftError(RuntimeError):
    """Raised when the persisted index config hash differs from the current config.

    Continuing would mix incompatible embedding spaces in a single index and corrupt
    search results, so indexing blocks until the caller opts into a rebuild with
    ``force_reindex=True`` (CLI ``--force``).
    """


class FormulaProviderUnavailableError(RuntimeError):
    """Raised when formula OCR is enabled but its provider cannot be used."""


# NOTE: _config_hash is defined in config.py (Decision 4 relocation) so the
# lightweight CLI can import it without the indexer's heavy deps. It is imported
# above and remains accessible as `indexer._config_hash` for existing callers.


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
        # Injectable so tests can neutralize the wait; production uses time.sleep.
        self._sleep = time.sleep
        self._rate_limit_max_retries = RATE_LIMIT_MAX_RETRIES
        self._empty_docs_path = config.chroma_db_path / "empty_docs.json"
        self._config_hash_path = config.chroma_db_path / "config_hash.txt"
        self.journal: IndexJournal | None = None
        self._formula_provider = None
        self._formula_candidate_provider = None
        vision_provider = getattr(config, "vision_provider", "anthropic")
        if vision_provider not in ("anthropic", "dashscope"):
            vision_provider = "anthropic"

        if config.vision_enabled and vision_provider == "dashscope" and config.dashscope_api_key:
            from .feature_extraction.dashscope_vision_api import DashScopeVisionAPI
            from .feature_extraction.vision_cache import VisionResultCache
            self._vision_api = DashScopeVisionAPI(
                api_key=config.dashscope_api_key,
                model=config.vision_model,
                result_cache=VisionResultCache(config.chroma_db_path.parent / "vision_cache"),
            )
        elif config.vision_enabled and config.anthropic_api_key:
            from .feature_extraction.vision_api import VisionAPI
            from .feature_extraction.vision_cache import VisionResultCache
            cost_log_path = config.chroma_db_path.parent / "vision_costs.json"
            self._vision_api = VisionAPI(
                api_key=config.anthropic_api_key,
                model=config.vision_model,
                cost_log_path=cost_log_path,
                # Cache parsed results so a re-run (e.g. resuming after a
                # rate-limit abort) does not re-pay the vision API for tables
                # already transcribed from unchanged PDFs.
                result_cache=VisionResultCache(config.chroma_db_path.parent / "vision_cache"),
            )
        else:
            self._vision_api = None

    def _assert_config_hash_current(self) -> None:
        """Block incremental backfills when the embedding-space hash drifted."""
        config_hash = _config_hash(self.config)
        if not self._config_hash_path.exists():
            raise ConfigDriftError(
                "Cannot backfill formulas before the text index config hash exists. "
                "Run index_library() first so formulas share the same embedding space."
            )
        stored_hash = self._config_hash_path.read_text().strip()
        if stored_hash != config_hash:
            raise ConfigDriftError(
                "Cannot backfill formulas because the current config hash differs from the "
                "stored index hash. Rebuild the index with index_library(force_reindex=True) "
                "before adding formula chunks."
            )

    def _get_formula_provider(self):
        """Create the configured formula OCR provider lazily."""
        if self._formula_provider is None:
            from .feature_extraction.formula_ocr import create_formula_ocr_provider

            self._formula_provider = create_formula_ocr_provider(
                self.config.formula_ocr_provider,
                config=self.config,
            )
        return self._formula_provider

    def _get_formula_candidate_provider(self):
        """Create the configured formula candidate detector lazily."""
        if self._formula_candidate_provider is None:
            from .feature_extraction.formula_ocr import create_formula_candidate_provider

            self._formula_candidate_provider = create_formula_candidate_provider(
                getattr(self.config, "formula_candidate_provider", "text_layer"),
                config=self.config,
            )
        return self._formula_candidate_provider

    def _ensure_formula_provider_available(self) -> None:
        """Fail fast when formula OCR is enabled but its optional extra is missing."""
        if getattr(self.config, "formula_ocr_enabled", False) is not True:
            return
        if getattr(self.config, "formula_candidate_provider", "text_layer") == "mineru_cache":
            return
        provider_name = getattr(self.config, "formula_ocr_provider", "unknown")
        try:
            from .feature_extraction.formula_ocr import ensure_formula_ocr_provider_dependency

            ensure_formula_ocr_provider_dependency(provider_name)
        except RuntimeError as e:
            raise FormulaProviderUnavailableError(
                f"Formula OCR provider {provider_name!r} is unavailable. "
                "Install the optional dependency with `pip install zotpilot[formula]` "
                "(or `uv pip install -e .[formula]` for an editable checkout), "
                "then rerun indexing; or set formula_ocr_enabled=false."
            ) from e

    def _recognize_formulas_for_item(self, item: ZoteroItem):
        """Run text-layer formula OCR for one item if possible."""
        if item.pdf_path is None or not item.pdf_path.exists():
            return []
        from .feature_extraction.formula_ocr import recognize_formulas

        cache_paths: tuple[Path | str, ...] = ()
        cache_path_resolver = getattr(self.zotero, "mineru_cache_paths_for_item", None)
        if callable(cache_path_resolver):
            try:
                cache_paths = tuple(cache_path_resolver(item.item_key, pdf_path=item.pdf_path))
            except Exception as exc:
                logger.warning(
                    "Failed to resolve MinerU formula cache paths for %s: %s",
                    item.item_key,
                    exc,
                )
        candidate_provider_name = getattr(self.config, "formula_candidate_provider", "text_layer")
        provider = None if candidate_provider_name == "mineru_cache" else self._get_formula_provider()
        return recognize_formulas(
            item.pdf_path,
            provider,
            candidate_provider=self._get_formula_candidate_provider(),
            item_key=item.item_key,
            cache_paths=cache_paths,
            max_formulas_per_doc=self.config.formula_ocr_max_formulas_per_doc,
            max_formulas_per_page=self.config.formula_ocr_max_formulas_per_page,
            min_confidence=self.config.formula_ocr_min_confidence,
        )

    def index_formulas(
        self,
        *,
        item_key: str | None = None,
        item_keys: list[str] | None = None,
        limit: int | None = None,
        refresh_existing: bool = True,
    ) -> dict:
        """Backfill formula chunks for already-indexed documents."""
        if not self.config.formula_ocr_enabled:
            raise ValueError("formula_ocr_enabled must be true before running formula backfill")
        self._ensure_formula_provider_available()
        self._assert_config_hash_current()

        indexed_ids = self.store.get_indexed_doc_ids()
        items = [
            item for item in self.zotero.get_all_items_with_pdfs()
            if item.item_key in indexed_ids and item.pdf_path and item.pdf_path.exists()
        ]
        if item_key:
            items = [item for item in items if item.item_key == item_key]
        if item_keys:
            wanted = set(item_keys)
            items = [item for item in items if item.item_key in wanted]
        if limit:
            items = items[:limit]

        results = []
        for item in items:
            journal_quartile = self.journal_ranker.lookup(item.publication)
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
                "quality_grade": "",
            }
            formulas = self._recognize_formulas_for_item(item)
            existing_formula_count = self._count_existing_formulas(item.item_key) if refresh_existing else 0
            kept_existing = 0
            if refresh_existing and formulas:
                self.store.delete_chunks_by_type(item.item_key, "formula")
            elif refresh_existing and existing_formula_count > 0:
                kept_existing = existing_formula_count
                logger.warning(
                    "Formula backfill found 0 formulas for %s; keeping %d existing formula chunk(s)",
                    item.item_key,
                    existing_formula_count,
                )
            if formulas:
                self.store.add_formulas(item.item_key, doc_meta, formulas)
            results.append({
                "item_key": item.item_key,
                "title": item.title,
                "n_formulas": len(formulas),
                "existing_formulas_kept": kept_existing,
            })

        return {
            "processed": len(results),
            "formulas_indexed": sum(row["n_formulas"] for row in results),
            "results": results,
        }

    def _count_existing_formulas(self, item_key: str) -> int:
        """Best-effort count of existing formula chunks for one document."""
        counter = getattr(self.store, "count_chunk_types", None)
        if counter is None:
            return 0
        try:
            counts = counter({item_key})
        except Exception:
            return 0
        if not isinstance(counts, dict):
            return 0
        value = counts.get("formula", 0)
        return int(value) if isinstance(value, int) else 0

    # ------------------------------------------------------------------
    # Empty-doc tracking (keyed by item_key -> pdf file hash)
    # ------------------------------------------------------------------

    def _load_empty_docs(self) -> dict[str, str]:
        """Load {item_key: pdf_hash} for docs that yielded no chunks.

        A corrupt file (e.g. truncated by a crash mid-write) must not brick the
        whole indexing run — treat it as empty and let the run rewrite it.
        """
        if not self._empty_docs_path.exists():
            return {}
        try:
            return json.loads(self._empty_docs_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Ignoring corrupt empty_docs file %s: %s", self._empty_docs_path, e)
            return {}

    def _save_empty_docs(self, mapping: dict[str, str]) -> None:
        """Persist atomically (tempfile + os.replace) so a crash mid-write
        cannot leave a half-written file that fails to parse next run."""
        fd, tmp_path = tempfile.mkstemp(
            dir=self._empty_docs_path.parent, suffix=".tmp", prefix="zotpilot_empty_docs_"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(mapping, f, indent=2)
            os.replace(tmp_path, self._empty_docs_path)
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

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

    def _library_unreachable(self) -> bool:
        """Best-effort cheap check that the Zotero data directory is reachable.

        Used to refuse orphan reconciliation when the library lives on a drive that
        is unmounted/unreachable — never wipe the index on a transient signal. When
        unknown, returns False (the empty-read guard still covers the unmounted->0
        items case).
        """
        data_dir = getattr(self.config, "zotero_data_dir", None)
        if data_dir is None:
            return False
        try:
            return not Path(data_dir).exists()
        except OSError:
            return True

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
        progress_sink: ProgressSink | None = None,
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
            progress_sink: Optional sink for structured progress events

        Returns:
            Dict with 'results' (list[IndexResult]) and summary counts.
        """
        self._ensure_formula_provider_available()

        run_id = uuid.uuid4().hex

        def progress(event_type: str, **payload: object) -> None:
            emit_progress(progress_sink, event_type, run_id=run_id, **payload)

        progress(
            "run_started",
            force_reindex=force_reindex,
            limit=limit,
            item_key=item_key,
            item_keys=item_keys,
            title_filter=bool(title_pattern),
            max_pages=max_pages,
            batch_size=batch_size,
        )
        items = self.zotero.get_all_items_with_pdfs()
        skipped_no_pdf: list[dict] = []
        kept_items: list = []
        for i in items:
            if i.pdf_path and i.pdf_path.exists():
                kept_items.append(i)
            else:
                item_title = getattr(i, "title", None) or ""
                skipped_no_pdf.append({
                    "item_key": i.item_key,
                    "title": item_title,
                    "reason": "no_pdf_attachment",
                })
                progress(
                    "paper_finished",
                    phase="planning",
                    item_key=i.item_key,
                    title=item_title,
                    status="skipped",
                    reason="no_pdf_attachment",
                )
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
        reconciliation = reconcile_orphaned_index_docs(
            self.store,
            current_doc_ids,
            library_unreachable=self._library_unreachable(),
        )
        if reconciliation.get("refused_mass_delete"):
            logger.warning(
                "Indexer: refused to delete orphaned indexed document(s) — %s",
                reconciliation.get("skipped_reason", "mass-deletion safety floor triggered"),
            )
        elif reconciliation["deleted_count"] > 0:
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
                empty_result = {
                    "results": [], "indexed": 0, "failed": 0, "empty": 0,
                    "skipped": 0, "already_indexed": 0, "skipped_no_pdf": [],
                }
                progress("run_finished", **_progress_counts(empty_result))
                return empty_result

        if item_keys:
            items = [i for i in items if i.item_key in item_keys]
            if not items:
                logger.error("No items found matching item_keys")
                empty_result = {
                    "results": [], "indexed": 0, "failed": 0, "empty": 0,
                    "skipped": 0, "already_indexed": 0, "skipped_no_pdf": [],
                }
                progress("run_finished", **_progress_counts(empty_result))
                return empty_result

        if title_pattern:
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
            logger.error(
                "Index configuration drift detected (stored hash %s != current %s); blocking to avoid "
                "a mixed embedding-space index.",
                stored_hash,
                config_hash,
            )
            progress("run_failed", reason="config_drift")
            # Common false alarm: the stored index was built WITH vision but this
            # run disabled it (batch_size>0 auto-disables vision, or no_vision was
            # set), and that single toggle -- not any embedding-space change --
            # tripped the guard. Steer to the cheap fix (keep vision on, index
            # incrementally) instead of a force-rebuild that re-spends embedding
            # quota on every already-indexed paper.
            if _vision_only_drift(self.config, stored_hash):
                if not self.config.vision_enabled:
                    raise ConfigDriftError(
                        "This index was built WITH vision, but this run disabled it "
                        "(batch_size>0 auto-disables vision, or no_vision/--no-vision was set), "
                        "and that single change -- not the embedding space -- tripped the drift "
                        "guard. To index the remaining papers incrementally, keep vision ON: "
                        "re-run with batch_size=0 (API: index_library(batch_size=0); CLI: drop "
                        "--no-vision). Do NOT use force_reindex/--force here -- it would rebuild "
                        "every already-indexed paper and re-spend embedding quota, when only an "
                        "incremental pass is needed."
                    )
                raise ConfigDriftError(
                    "This index was built WITHOUT vision, but this run enabled it, and that "
                    "single change tripped the drift guard. To index incrementally, match the "
                    "stored setting by keeping vision OFF: re-run with no_vision=True (CLI: "
                    "--no-vision) or batch_size>0. Use force_reindex/--force only if you intend "
                    "to rebuild the whole index with vision on."
                )
            raise ConfigDriftError(
                "Index configuration has changed since the last run (chunk size/overlap, embedding "
                "provider/model/dimensions, OCR, or vision settings). Continuing would mix incompatible "
                "embedding spaces in one index and corrupt search results. Re-run with --force (CLI) or "
                "force_reindex=True (API) to rebuild the index under the new configuration."
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
                    progress(
                        "paper_finished",
                        phase="planning",
                        item_key=item.item_key,
                        title=item.title,
                        status="already_indexed",
                    )
                    continue

            if item.item_key in empty_docs:
                current_hash = self._pdf_hash(item.pdf_path)
                if current_hash == empty_docs[item.item_key]:
                    results.append(IndexResult(
                        item.item_key, item.title, "skipped",
                        reason="no extractable text (unchanged PDF)"))
                    progress(
                        "paper_finished",
                        phase="planning",
                        item_key=item.item_key,
                        title=item.title,
                        status="skipped",
                        reason="no extractable text (unchanged PDF)",
                    )
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
                        progress(
                            "paper_finished",
                            phase="planning",
                            item_key=item.item_key,
                            title=item.title,
                            status="skipped",
                            reason=f"too long ({pages} pages, max {max_pages})",
                            pages=pages,
                        )
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
        progress(
            "plan_ready",
            to_index=len(to_index),
            reindex_count=reindex_count,
            already_indexed=len(indexed_ids),
            skipped=n_skipped,
            skipped_no_pdf_count=len(skipped_no_pdf),
            skipped_long=len(long_items),
            batch_size=batch_size,
            has_more=deferred_by_batch,
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

        progress("phase_started", phase="extraction", total=total_to_extract)
        for i, item in enumerate(tqdm(to_index, desc="Extracting"), 1):
            t0 = time.perf_counter()
            extraction_status = "extracted"
            extraction_reason = ""
            progress(
                "paper_started",
                phase="extraction",
                item_key=item.item_key,
                title=item.title,
                position=i,
                total=total_to_extract,
            )
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
                extraction_status = "failed"
                extraction_reason = f"{type(e).__name__}: {e}"
                results.append(IndexResult(
                    item.item_key, item.title, "failed",
                    reason=extraction_reason))

            elapsed = time.perf_counter() - t0
            progress(
                "paper_finished",
                phase="extraction",
                item_key=item.item_key,
                title=item.title,
                position=i,
                total=total_to_extract,
                status=extraction_status,
                reason=extraction_reason,
                elapsed_seconds=elapsed,
            )
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
        progress(
            "phase_finished",
            phase="extraction",
            total=total_to_extract,
            elapsed_seconds=phase1_elapsed,
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
            progress(
                "phase_started",
                phase="vision",
                total=pending_count,
                pending_docs=pending_docs,
                estimated_cost_usd=vision_estimated_cost_usd,
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
                progress(
                    "phase_finished",
                    phase="vision",
                    total=pending_count,
                    status="skipped",
                    reason=vision_skip_reason,
                    estimated_cost_usd=vision_estimated_cost_usd,
                )
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
                progress(
                    "phase_finished",
                    phase="vision",
                    total=pending_count,
                    status="completed",
                    elapsed_seconds=phase2_elapsed,
                    estimated_cost_usd=vision_estimated_cost_usd,
                )

        # ---- Phase 3: Index each document (chunk, store, etc.) ----
        total_to_store = len(doc_extractions)
        index_times: list[float] = []
        phase3_start = time.perf_counter()
        if total_to_store > 0:
            logger.info(f"Indexing: chunking and storing {total_to_store} papers")

        # Snapshot so the never-attempted tail can be enumerated after an abort break (关键1).
        extraction_items = list(doc_extractions.items())
        rate_limited_abort = False   # set ONLY by a typed RateLimitError
        systemic_abort = False       # set ONLY by the generic consecutive-failure backstop
        abort_index: int | None = None
        consecutive_same = 0
        last_failure_sig: str | None = None

        progress("phase_started", phase="indexing", total=total_to_store)
        for idx, (item_key, (item, extraction)) in enumerate(extraction_items, 1):
            t0 = time.perf_counter()
            progress(
                "paper_started",
                phase="indexing",
                item_key=item.item_key,
                title=item.title,
                position=idx,
                total=total_to_store,
            )
            try:
                n_chunks, n_tables, reason, extraction_stats, quality_grade = self._index_extraction_with_retry(
                    item, extraction
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
                    progress(
                        "paper_finished",
                        phase="indexing",
                        item_key=item.item_key,
                        title=item.title,
                        position=idx,
                        total=total_to_store,
                        status="indexed",
                        n_chunks=n_chunks,
                        n_tables=n_tables,
                        quality_grade=quality_grade,
                    )
                else:
                    empty_docs[item.item_key] = self._pdf_hash(item.pdf_path)
                    results.append(IndexResult(
                        item.item_key, item.title, "empty", reason=reason,
                        quality_grade=quality_grade))
                    progress(
                        "paper_finished",
                        phase="indexing",
                        item_key=item.item_key,
                        title=item.title,
                        position=idx,
                        total=total_to_store,
                        status="empty",
                        reason=reason,
                        n_chunks=n_chunks,
                        n_tables=n_tables,
                        quality_grade=quality_grade,
                    )
                logger.debug(f"Completed {item.item_key}: {n_chunks} chunks, {n_tables} tables, quality {quality_grade}")  # noqa: E501
            except RateLimitError as e:
                logger.error(f"Rate limit hit on {item.item_key}: {e}")
                failure_reason = f"{type(e).__name__}: {e}"
                results.append(IndexResult(
                    item.item_key, item.title, "failed",
                    reason=failure_reason))
                progress(
                    "paper_finished",
                    phase="indexing",
                    item_key=item.item_key,
                    title=item.title,
                    position=idx,
                    total=total_to_store,
                    status="failed",
                    reason=failure_reason,
                )
                rate_limited_abort = True
                abort_index = idx
                break  # stop the run; remaining papers are untried. MUST break, not raise — see D1/D2.
            except Exception as e:
                logger.error(f"Failed to index {item.item_key}: {type(e).__name__}: {e}")
                failure_reason = f"{type(e).__name__}: {e}"
                results.append(IndexResult(
                    item.item_key, item.title, "failed",
                    reason=failure_reason))
                progress(
                    "paper_finished",
                    phase="indexing",
                    item_key=item.item_key,
                    title=item.title,
                    position=idx,
                    total=total_to_store,
                    status="failed",
                    reason=failure_reason,
                )
                sig = _failure_signature(e)
                if sig == last_failure_sig:
                    consecutive_same += 1
                else:
                    consecutive_same, last_failure_sig = 1, sig
                if consecutive_same >= CONSECUTIVE_FAILURE_ABORT_THRESHOLD:
                    systemic_abort = True          # NOT rate_limited — cause is unknown (关键3)
                    abort_index = idx
                    break  # MUST break, not raise — see D1/D2.
            else:
                consecutive_same, last_failure_sig = 0, None  # reset on any success

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

        # Append the never-attempted tail after the break so results/failed/counts agree (关键1).
        if abort_index is not None:
            for _k, (_it, _ex) in extraction_items[abort_index:]:
                abort_tail_reason = "AbortNotAttempted: skipped after early abort (quota/systemic)"
                results.append(IndexResult(
                    _it.item_key, _it.title, "failed",
                    reason=abort_tail_reason))
                progress(
                    "paper_finished",
                    phase="indexing",
                    item_key=_it.item_key,
                    title=_it.title,
                    status="failed",
                    reason=abort_tail_reason,
                )

        # Single source of truth for the abort count — reused by the log and the counts block.
        aborted = rate_limited_abort or systemic_abort
        not_indexed_due_to_abort = (
            len(extraction_items) - (abort_index - 1)
            if aborted and abort_index is not None
            else 0
        )

        abort_cause = ""
        if rate_limited_abort:
            abort_cause = "rate_limit"
        elif systemic_abort:
            abort_cause = "consecutive_failures"

        phase3_elapsed = time.perf_counter() - phase3_start
        if aborted:
            log_cause = "rate limit" if rate_limited_abort else "consecutive failures"
            logger.warning(
                f"Indexing aborted while processing {abort_index}/{total_to_store} papers "
                f"({log_cause}); {not_indexed_due_to_abort} not attempted"
            )
            progress(
                "run_aborted",
                phase="indexing",
                cause=abort_cause,
                abort_index=abort_index,
                total=total_to_store,
                not_indexed_due_to_abort=not_indexed_due_to_abort,
            )
        elif total_to_store > 0:
            logger.info(
                f"Indexing complete: {total_to_store} papers in "
                f"{phase3_elapsed:.1f}s ({phase3_elapsed / total_to_store:.1f}s avg)"
            )
        progress(
            "phase_finished",
            phase="indexing",
            total=total_to_store,
            status="aborted" if aborted else "completed",
            elapsed_seconds=phase3_elapsed,
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

        # Abort surfacing (additive; 关键3 naming). `aborted`/`not_indexed_due_to_abort`
        # were computed once right after the loop — reuse, do NOT recompute the formula.
        counts["rate_limited_abort"] = rate_limited_abort   # typed RateLimitError only
        counts["systemic_abort"] = systemic_abort           # generic backstop only (cause unknown)
        counts["not_indexed_due_to_abort"] = not_indexed_due_to_abort

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
        # Skip when nothing was indexed this call: the startup reconciliation
        # (above) already reflects current state, and a second full library scan
        # per no-op/small batch call is pure overhead (the default batch_size
        # makes many such calls). A run that committed nothing spanned no
        # meaningful window for new deletions.
        if counts["indexed"] > 0:
            final_current_doc_ids = {
                item.item_key
                for item in self.zotero.get_all_items_with_pdfs()
                if item.pdf_path and item.pdf_path.exists()
            }
            final_reconciliation = reconcile_orphaned_index_docs(
                self.store,
                final_current_doc_ids,
                library_unreachable=self._library_unreachable(),
            )
            if final_reconciliation.get("refused_mass_delete"):
                logger.warning(
                    "Indexer: refused end-of-run orphan reconciliation — %s",
                    final_reconciliation.get("skipped_reason", "mass-deletion safety floor triggered"),
                )
            elif final_reconciliation["deleted_count"] > 0:
                logger.info(
                    "Indexer: removed %d orphaned indexed document(s) after refresh of Zotero library state",
                    final_reconciliation["deleted_count"],
                )

        progress("run_finished", **_progress_counts(counts))
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

    def _index_extraction_with_retry(self, item: ZoteroItem, extraction) -> tuple[int, int, str, dict, str]:
        """Wrap ``_index_extraction`` with bounded rate-limit retries.

        On a typed ``RateLimitError`` we wait the provider-supplied ``retry_after``
        (falling back to ``RATE_LIMIT_DEFAULT_WAIT_SECONDS``, capped at
        ``RATE_LIMIT_MAX_WAIT_SECONDS``) and retry the same paper up to
        ``self._rate_limit_max_retries`` times. Once retries are exhausted the
        last ``RateLimitError`` propagates unchanged, so the Phase-3 loop still
        fails fast exactly as before — retry is a recovery layer in front of that
        abort, not a replacement for it.
        """
        attempt = 0
        while True:
            try:
                return self._index_extraction(item, extraction, self.journal)
            except RateLimitError as e:
                if attempt >= self._rate_limit_max_retries:
                    raise
                attempt += 1
                wait = e.retry_after if e.retry_after is not None else RATE_LIMIT_DEFAULT_WAIT_SECONDS
                wait = min(max(wait, 0.0), RATE_LIMIT_MAX_WAIT_SECONDS)
                logger.warning(
                    f"Rate limit on {item.item_key} (attempt {attempt}/{self._rate_limit_max_retries}); "
                    f"waiting {wait:.0f}s before retry"
                )
                self._sleep(wait)

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

        # Mark committed after text-chunk persistence. NOTE: a stale
        # table/figure-failure marker is intentionally NOT cleared here — it is
        # cleared only after tables+figures actually store below, so a failure
        # (incl. a re-raised RateLimitError) leaves the prior marker intact.
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

        # Tracks a swallowed (non-quota) table/figure failure recorded THIS run,
        # so we don't clear the marker we just wrote at the end. Formula OCR is
        # tracked separately and must not poison table/figure completeness.
        table_figure_failure_this_run = False
        formula_failure_this_run = False

        # Store formulas if explicitly enabled. Phase A only covers text-layer
        # candidates; image/vector formulas are intentionally left for later.
        n_formulas = 0
        if getattr(self.config, "formula_ocr_enabled", False) is True:
            try:
                formulas = list(getattr(extraction, "formulas", []) or [])
                if not formulas:
                    formulas = self._recognize_formulas_for_item(item)
                self.store.add_formulas(item_key, doc_meta, formulas)
                n_formulas = len(formulas)
                logger.debug(f"  Extracted {n_formulas} formulas")
            except RateLimitError:
                raise
            except Exception as e:
                logger.warning(f"Formula OCR/storage failed for {item_key}: {e}")
                formula_failure_this_run = True

        # Store tables if enabled (skip layout artifacts)
        n_tables = 0
        real_tables = [t for t in extraction.tables if not t.artifact_type]
        n_artifacts = len(extraction.tables) - len(real_tables)
        if real_tables:
            try:
                self.store.add_tables(item_key, doc_meta, real_tables, ref_map=ref_map)
                n_tables = len(real_tables)
            except RateLimitError:
                raise  # quota exhaustion must propagate to the Phase-3 abort, not degrade to a warning
            except Exception as e:
                logger.warning(f"Table storage failed for {item_key}: {e}")
                if journal is not None:
                    record_table_failure(journal, item_key, f"table storage: {e}")
                    table_figure_failure_this_run = True
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
            except RateLimitError:
                raise  # quota exhaustion must propagate to the Phase-3 abort, not degrade to a warning
            except Exception as e:
                logger.warning(f"Figure storage failed for {item_key}: {e}")
                if journal is not None:
                    record_table_failure(journal, item_key, f"figure storage: {e}")
                    table_figure_failure_this_run = True

        if formula_failure_this_run:
            logger.debug(
                "Formula OCR/storage failed for %s independently of table/figure state",
                item_key,
            )

        # Tables and figures stored cleanly this run: clear any stale marker
        # from a prior run. Skipped when this run recorded its own table/figure
        # failure (keep that), and a re-raised RateLimitError above never
        # reaches here, so a quota-aborted run keeps the doc's prior marker
        # intact. Formula OCR failures are intentionally independent.
        if journal is not None and not table_figure_failure_this_run:
            clear_table_failure(journal, item_key)

        logger.debug(f"Indexed {item.item_key}: {len(chunks)} chunks, {n_tables} tables, {n_figures} figures, {n_formulas} formulas, quality {quality_grade}")  # noqa: E501
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

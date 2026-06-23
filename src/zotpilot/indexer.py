"""Indexing pipeline orchestration."""
import hashlib
import json
import logging
import os
import random
import re
import sqlite3
import tempfile
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from tqdm import tqdm

from .config import Config, _config_hash, _vision_only_drift, index_data_dir
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
from .vector_store import IndexUnavailableError, VectorStore
from .zotero_client import (
    ZoteroClient,
    is_likely_bilingual_or_translated_pdf,
    pdf_content_translation_risk_score,
)

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


class _ReadOnlyIndexedDocStore:
    """Read indexed doc ids without constructing a writable Chroma collection."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)

    def get_indexed_doc_ids(self) -> set[str]:
        sqlite_path = self.db_path / "chroma.sqlite3"
        if not sqlite_path.exists():
            raise IndexUnavailableError(f"Chroma SQLite index not found at {sqlite_path}")
        uri = f"file:{sqlite_path.as_posix()}?mode=ro&immutable=1"
        doc_ids: set[str] = set()
        try:
            with sqlite3.connect(uri, uri=True) as conn:
                cursor = conn.execute("SELECT embedding_id FROM embeddings")
                for (chunk_id,) in cursor:
                    doc_id = VectorStore._doc_id_from_chunk_id(str(chunk_id))
                    if doc_id:
                        doc_ids.add(doc_id)
        except sqlite3.Error as exc:
            raise IndexUnavailableError(
                f"Could not read indexed document ids from {sqlite_path}: {exc}"
            ) from exc
        return doc_ids


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


def _formula_backfill_state_path(config: Config) -> Path:
    """Default append-only state stream for formula-only backfill runs."""
    return index_data_dir(config) / "formula_backfill_state.jsonl"


def _append_formula_backfill_state(path: Path | None, payload: dict[str, object]) -> None:
    """Append one formula backfill status event without affecting indexing."""
    if path is None:
        return
    event = {
        "schema_version": 1,
        "event": "formula_backfill_item",
        "timestamp": time.time(),
        **payload,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, sort_keys=True, default=str))
            f.write("\n")
    except OSError as e:
        logger.warning("Failed to append formula backfill state to %s: %s", path, e)


def _formula_backfill_row(
    *,
    item_key: str,
    title: str,
    status: str,
    reason: str = "",
    candidate_count: int = 0,
    provider_calls: int = 0,
    external_calls: int = 0,
    n_formulas: int = 0,
    existing_formulas_kept: int = 0,
    low_confidence_count: int = 0,
    review_reasons: list[str] | None = None,
    high_density_call_threshold: int = 0,
    high_density_candidate_threshold: int = 0,
    error: str = "",
    error_message: str = "",
) -> dict[str, object]:
    """Return a stable per-paper formula backfill status row."""
    return {
        "item_key": item_key,
        "title": title,
        "status": status,
        "reason": reason,
        "candidate_count": candidate_count,
        "provider_calls": provider_calls,
        "external_calls": external_calls,
        "n_formulas": n_formulas,
        "existing_formulas_kept": existing_formulas_kept,
        "low_confidence_count": low_confidence_count,
        "review_reasons": review_reasons or [],
        "high_density_call_threshold": high_density_call_threshold,
        "high_density_candidate_threshold": high_density_candidate_threshold,
        "error": error,
        "error_message": error_message,
    }


def _formula_backfill_estimate_skip_row(item: ZoteroItem, reason: str) -> dict[str, object]:
    """Return an estimate row for an item deliberately skipped before OCR."""
    return {
        "item_key": item.item_key,
        "title": item.title,
        "status": "skipped",
        "reason": reason,
        "candidate_count": 0,
        "estimated_provider_calls": 0,
        "estimated_external_calls": 0,
        "error": "",
    }


def _formula_candidate_preview(
    candidates: list,
    *,
    limit: int,
    text_limit: int = 160,
) -> list[dict[str, object]]:
    """Return a compact, non-image preview of candidate formula regions."""
    preview: list[dict[str, object]] = []
    selected_candidates = candidates if limit < 0 else candidates[:max(limit, 0)]
    clipped_text_limit = max(int(text_limit), 0)

    def clip(value: str) -> str:
        return value if clipped_text_limit == 0 else value[:clipped_text_limit]

    for index, candidate in enumerate(selected_candidates):
        raw_text = str(getattr(candidate, "raw_text", "") or "")
        latex = str(getattr(candidate, "latex", "") or "")
        equation_number = _formula_candidate_effective_equation_number(candidate)
        preview.append({
            "candidate_index": index,
            "page_num": getattr(candidate, "page_num", 0),
            "source": getattr(candidate, "source", ""),
            "confidence": getattr(candidate, "confidence", None),
            "equation_number": equation_number,
            "equation_number_status": getattr(candidate, "equation_number_status", ""),
            "bbox": list(getattr(candidate, "bbox", ()) or ()),
            "bbox_coordinate_space": getattr(candidate, "bbox_coordinate_space", ""),
            "has_latex": bool(latex.strip()),
            "needs_ocr": not bool(latex.strip()),
            "raw_text_preview": clip(raw_text),
            "latex_preview": clip(latex),
        })
    return preview


def _formula_candidate_effective_equation_number(candidate: object) -> str:
    if getattr(candidate, "equation_number_status", "") == "unnumbered":
        return ""
    return str(getattr(candidate, "equation_number", "") or "")


def _equation_number_audit_value(equation_number: str) -> tuple[str, int] | None:
    """Return a prefix/value pair for lightweight numbering sequence audits."""
    token = str(equation_number or "").strip()
    token = re.sub(r"^(?:Eq\.?\s*)?[\(（]\s*|\s*[\)）]$", "", token, flags=re.IGNORECASE)
    token = (
        token.replace("－", "-")
        .replace("–", "-")
        .replace("—", "-")
        .replace("−", "-")
        .replace(":", ".")
        .strip()
    )
    simple_match = re.fullmatch(r"(?P<value>\d+)(?:[A-Za-z])?", token)
    if simple_match:
        return ("", int(simple_match.group("value")))
    compound_match = re.fullmatch(
        r"(?P<prefix>[A-Za-z]|\d+(?:[.-]\d+)*)[.-](?P<value>\d+)(?:[A-Za-z])?",
        token,
    )
    if compound_match:
        return (compound_match.group("prefix"), int(compound_match.group("value")))
    return None


def _formula_equation_number_audit(equation_numbers: list[str]) -> dict[str, object]:
    """Collect review-only warnings for suspicious equation-number ordering."""
    values: list[tuple[str, str, int]] = []
    for equation_number in equation_numbers:
        parsed = _equation_number_audit_value(equation_number)
        if parsed is None:
            continue
        prefix, value = parsed
        values.append((equation_number, prefix, value))

    prefixes = sorted({prefix for _number, prefix, _value in values})
    warnings: set[str] = set()
    sequence_breaks: list[dict[str, object]] = []
    if len(prefixes) > 1:
        warnings.add("mixed_equation_number_prefixes")

    previous_by_prefix: dict[str, tuple[str, int]] = {}
    for equation_number, prefix, value in values:
        previous = previous_by_prefix.get(prefix)
        if previous is not None:
            previous_number, previous_value = previous
            if value < previous_value:
                warnings.add("equation_number_regression")
                sequence_breaks.append({
                    "previous": previous_number,
                    "current": equation_number,
                    "prefix": prefix or "regular",
                    "reason": "regression",
                })
            elif value - previous_value > 5:
                warnings.add("large_equation_number_gap")
                sequence_breaks.append({
                    "previous": previous_number,
                    "current": equation_number,
                    "prefix": prefix or "regular",
                    "reason": "large_gap",
                    "gap": value - previous_value,
                })
            elif value - previous_value > 1:
                warnings.add("missing_equation_number_gap")
                sequence_breaks.append({
                    "previous": previous_number,
                    "current": equation_number,
                    "prefix": prefix or "regular",
                    "reason": "missing_gap",
                    "gap": value - previous_value,
                    "missing_count": value - previous_value - 1,
                })
        previous_by_prefix[prefix] = (equation_number, value)

    return {
        "equation_number_prefixes": [prefix or "regular" for prefix in prefixes],
        "equation_number_warnings": sorted(warnings),
        "equation_number_warning_count": len(warnings),
        "equation_number_sequence_breaks": sequence_breaks[:20],
    }


def _validate_formula_page_range(page_min: int | None, page_max: int | None) -> tuple[int | None, int | None]:
    """Validate optional 1-based PDF page bounds for formula-only runs."""
    normalized_min = int(page_min) if page_min is not None else None
    normalized_max = int(page_max) if page_max is not None else None
    if normalized_min is not None and normalized_min < 1:
        raise ValueError("page_min must be >= 1")
    if normalized_max is not None and normalized_max < 1:
        raise ValueError("page_max must be >= 1")
    if normalized_min is not None and normalized_max is not None and normalized_min > normalized_max:
        raise ValueError("page_min must be <= page_max")
    return normalized_min, normalized_max


def _formula_page_range_requested(page_min: int | None, page_max: int | None) -> bool:
    return page_min is not None or page_max is not None


def _keys_after_resume(keys: list[str], resume_after: str) -> list[str]:
    skipped = True
    resumed_keys = []
    for key in keys:
        if skipped:
            skipped = key != resume_after
            continue
        resumed_keys.append(key)
    return resumed_keys


def _assert_single_item_for_formula_page_range(
    *,
    item_key: str | None,
    item_keys: list[str] | None,
    page_min: int | None,
    page_max: int | None,
) -> None:
    """Keep page-window formula backfills scoped to one reviewed document."""
    if not _formula_page_range_requested(page_min, page_max):
        return
    if item_key:
        return
    if item_keys is not None and len(item_keys) == 1:
        return
    raise ValueError("page_min/page_max formula backfill options require one item_key or exactly one item_keys entry")


def _filter_formula_candidates_by_page_range(
    candidates: list,
    *,
    page_min: int | None,
    page_max: int | None,
) -> tuple[list, list[int]]:
    """Return candidates in the requested page range plus their stable original indices."""
    if not _formula_page_range_requested(page_min, page_max):
        return candidates, list(range(len(candidates)))
    filtered: list = []
    formula_indices: list[int] = []
    for index, candidate in enumerate(candidates):
        page_num = getattr(candidate, "page_num", 0)
        if not isinstance(page_num, int):
            continue
        if page_min is not None and page_num < page_min:
            continue
        if page_max is not None and page_num > page_max:
            continue
        filtered.append(candidate)
        formula_indices.append(index)
    return filtered, formula_indices


def _formula_candidate_needs_ocr(candidate: object) -> bool:
    return not str(getattr(candidate, "latex", "") or "").strip()


def _formula_candidate_has_cached_latex(candidate: object) -> bool:
    return bool(str(getattr(candidate, "latex", "") or "").strip())


def _formula_candidate_source(candidate: object) -> str:
    return str(getattr(candidate, "source", "") or "")


def _formula_candidate_is_structured_cache(candidate: object) -> bool:
    return _formula_candidate_source(candidate).startswith(("mineru_", "pdf_extract_kit_"))


def _formula_candidate_has_low_quality_cached_latex(candidate: object) -> bool:
    return (
        _formula_candidate_has_cached_latex(candidate)
        and _formula_candidate_source(candidate).endswith("_low_quality")
    )


def _format_index_ranges(indices: list[int]) -> str:
    """Compress stable formula indices for human review output."""
    unique = sorted({int(index) for index in indices if int(index) >= 0})
    if not unique:
        return ""
    ranges: list[str] = []
    start = previous = unique[0]
    for index in unique[1:]:
        if index == previous + 1:
            previous = index
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = index
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


def _formula_candidate_segment_summary(
    *,
    segment_index: int,
    candidates: list,
    formula_indices: list[int],
    candidate_start: int,
    candidate_end: int,
    data_egress: bool,
) -> dict[str, object]:
    audit = _formula_candidate_audit(candidates)
    provider_calls = sum(1 for candidate in candidates if _formula_candidate_needs_ocr(candidate))
    formula_index_min = min(formula_indices) if formula_indices else 0
    formula_index_max = max(formula_indices) if formula_indices else 0
    return {
        "segment_index": segment_index,
        "candidate_start": candidate_start,
        "candidate_end": candidate_end,
        "formula_index_offset": formula_index_min,
        "formula_index_min": formula_index_min,
        "formula_index_max": formula_index_max,
        "formula_index_count": len(set(formula_indices)),
        "formula_index_ranges": _format_index_ranges(formula_indices),
        "page_min": audit["page_min"],
        "page_max": audit["page_max"],
        "candidate_count": audit["candidate_count"],
        "estimated_provider_calls": provider_calls,
        "estimated_external_calls": provider_calls if data_egress else 0,
        "cached_latex_count": audit["cached_latex_count"],
        "ocr_needed_count": audit["ocr_needed_count"],
        "numbered_count": audit["numbered_count"],
        "unnumbered_count": audit["unnumbered_count"],
        "first_equation_number": audit["first_equation_number"],
        "last_equation_number": audit["last_equation_number"],
        "equation_number_prefixes": audit["equation_number_prefixes"],
        "equation_number_warnings": audit["equation_number_warnings"],
        "equation_number_warning_count": audit["equation_number_warning_count"],
        "equation_number_sequence_breaks": audit["equation_number_sequence_breaks"],
        "source_counts": audit["source_counts"],
    }


def _formula_candidate_page_sort_key(indexed_candidate: tuple[int, object]) -> tuple[int, float, float, int]:
    index, candidate = indexed_candidate
    page_num = getattr(candidate, "page_num", 0)
    bbox = tuple(getattr(candidate, "bbox", ()) or ())
    y0 = float(bbox[1]) if len(bbox) >= 2 else 0.0
    x0 = float(bbox[0]) if bbox else 0.0
    return (
        page_num if isinstance(page_num, int) and page_num > 0 else 0,
        y0,
        x0,
        index,
    )


def _formula_high_density_backfill_plan(
    *,
    item: ZoteroItem,
    candidates: list,
    data_egress: bool,
    daily_call_budget: int,
    high_density_call_threshold: int,
    high_density_candidate_threshold: int,
) -> dict[str, object]:
    """Split a dense formula document into reviewed page-window backfill batches."""
    if not candidates:
        return {}
    candidate_limit = high_density_candidate_threshold if high_density_candidate_threshold > 0 else 160
    call_limit = high_density_call_threshold if high_density_call_threshold > 0 else 80
    if daily_call_budget > 0:
        call_limit = min(call_limit, daily_call_budget) if call_limit > 0 else daily_call_budget
    candidate_limit = max(candidate_limit, 1)
    call_limit = max(call_limit, 1)

    ordered = sorted(enumerate(candidates), key=_formula_candidate_page_sort_key)
    page_groups: list[list[tuple[int, object]]] = []
    for indexed_candidate in ordered:
        page_num = getattr(indexed_candidate[1], "page_num", 0)
        if page_groups and getattr(page_groups[-1][-1][1], "page_num", 0) == page_num:
            page_groups[-1].append(indexed_candidate)
        else:
            page_groups.append([indexed_candidate])

    segments: list[dict[str, object]] = []
    current_group: list[tuple[int, object]] = []
    candidate_order_start = 0
    candidate_order_index = 0
    current_calls = 0
    for page_group in page_groups:
        group_calls = sum(1 for _index, candidate in page_group if _formula_candidate_needs_ocr(candidate))
        current_count = len(current_group)
        should_flush = current_count > 0 and (
            current_count + len(page_group) > candidate_limit
            or (data_egress and current_calls + group_calls > call_limit)
        )
        if should_flush:
            segment_candidates = [candidate for _index, candidate in current_group]
            segment_indices = [index for index, _candidate in current_group]
            segments.append(
                _formula_candidate_segment_summary(
                    segment_index=len(segments) + 1,
                    candidates=segment_candidates,
                    formula_indices=segment_indices,
                    candidate_start=candidate_order_start,
                    candidate_end=candidate_order_index,
                    data_egress=data_egress,
                )
            )
            candidate_order_start = candidate_order_index
            current_group = []
            current_calls = 0
        current_group.extend(page_group)
        current_calls += group_calls
        candidate_order_index += len(page_group)
    if current_group:
        segment_candidates = [candidate for _index, candidate in current_group]
        segment_indices = [index for index, _candidate in current_group]
        segments.append(
            _formula_candidate_segment_summary(
                segment_index=len(segments) + 1,
                candidates=segment_candidates,
                formula_indices=segment_indices,
                candidate_start=candidate_order_start,
                candidate_end=candidate_order_index,
                data_egress=data_egress,
            )
        )

    audit = _formula_candidate_audit(candidates)
    provider_calls = sum(1 for candidate in candidates if _formula_candidate_needs_ocr(candidate))
    return {
        "item_key": item.item_key,
        "title": item.title,
        "candidate_count": len(candidates),
        "estimated_provider_calls": provider_calls,
        "estimated_external_calls": provider_calls if data_egress else 0,
        "page_min": audit["page_min"],
        "page_max": audit["page_max"],
        "first_equation_number": audit["first_equation_number"],
        "last_equation_number": audit["last_equation_number"],
        "equation_number_prefixes": audit["equation_number_prefixes"],
        "equation_number_warnings": audit["equation_number_warnings"],
        "equation_number_warning_count": audit["equation_number_warning_count"],
        "equation_number_sequence_breaks": audit["equation_number_sequence_breaks"],
        "segment_candidate_limit": candidate_limit,
        "segment_provider_call_limit": call_limit if data_egress else 0,
        "segment_count": len(segments),
        "segments": segments,
        "recommended_write_mode": "single-item page-window backfill; append new formula chunks only",
    }


def _formula_candidate_audit(candidates: list) -> dict[str, object]:
    """Return per-paper candidate quality signals for read-only review."""
    source_counts = Counter(_formula_candidate_source(candidate) for candidate in candidates)
    page_nums = [
        int(page_num)
        for candidate in candidates
        if isinstance((page_num := getattr(candidate, "page_num", None)), int) and page_num > 0
    ]
    equation_numbers = [
        number
        for candidate in candidates
        if (number := _formula_candidate_effective_equation_number(candidate))
    ]
    number_counts = Counter(equation_numbers)
    duplicate_numbers = [
        number
        for number, count in number_counts.items()
        if count > 1
    ]
    cached_latex_count = sum(1 for candidate in candidates if _formula_candidate_has_cached_latex(candidate))
    cached_latex_low_quality_count = sum(
        1 for candidate in candidates
        if _formula_candidate_has_low_quality_cached_latex(candidate)
    )
    cached_latex_missing_number_count = sum(
        1 for candidate in candidates
        if _formula_candidate_has_cached_latex(candidate)
        and _formula_candidate_is_structured_cache(candidate)
        and getattr(candidate, "equation_number_status", "") != "unnumbered"
        and not _formula_candidate_effective_equation_number(candidate)
    )
    unnumbered_count = sum(
        1 for candidate in candidates
        if not _formula_candidate_effective_equation_number(candidate)
    )
    truncated_source_count = sum(
        1 for candidate in candidates
        if str(getattr(candidate, "source", "") or "").endswith("truncated")
    )
    equation_number_audit = _formula_equation_number_audit(equation_numbers)
    equation_number_warnings = set(equation_number_audit["equation_number_warnings"])
    if duplicate_numbers:
        equation_number_warnings.add("duplicate_equation_numbers")
    if cached_latex_missing_number_count:
        equation_number_warnings.add("cached_latex_missing_equation_numbers")
    if cached_latex_low_quality_count:
        equation_number_warnings.add("cached_latex_low_quality")
    return {
        "candidate_count": len(candidates),
        "cached_latex_count": cached_latex_count,
        "cached_latex_low_quality_count": cached_latex_low_quality_count,
        "cached_latex_missing_equation_number_count": cached_latex_missing_number_count,
        "cached_latex_missing_equation_number_ratio": (
            round(cached_latex_missing_number_count / cached_latex_count, 4)
            if cached_latex_count
            else 0.0
        ),
        "ocr_needed_count": len(candidates) - cached_latex_count,
        "numbered_count": len(equation_numbers),
        "unnumbered_count": unnumbered_count,
        "duplicate_equation_numbers": duplicate_numbers[:20],
        "duplicate_equation_number_count": len(duplicate_numbers),
        "truncated_source_count": truncated_source_count,
        "has_truncated_source": truncated_source_count > 0,
        "source_counts": dict(sorted(source_counts.items())),
        "page_min": min(page_nums) if page_nums else 0,
        "page_max": max(page_nums) if page_nums else 0,
        "page_count_with_candidates": len(set(page_nums)),
        "first_equation_number": equation_numbers[0] if equation_numbers else "",
        "last_equation_number": equation_numbers[-1] if equation_numbers else "",
        **equation_number_audit,
        "equation_number_warnings": sorted(equation_number_warnings),
        "equation_number_warning_count": len(equation_number_warnings),
    }


def _formula_high_density_call_threshold(config: object) -> int:
    return max(int(getattr(config, "formula_ocr_high_density_call_threshold", 80) or 0), 0)


def _formula_high_density_candidate_threshold(config: object) -> int:
    return max(int(getattr(config, "formula_ocr_high_density_candidate_threshold", 160) or 0), 0)


def _formula_high_density_trigger(
    *,
    candidate_count: int,
    provider_call_count: int,
    call_threshold: int,
    candidate_threshold: int,
    data_egress: bool,
) -> str:
    if candidate_threshold > 0 and candidate_count > candidate_threshold:
        return "candidate_count"
    if data_egress and call_threshold > 0 and provider_call_count > call_threshold:
        return "provider_calls"
    return ""


def _format_estimated_duration(seconds: float) -> str:
    """Return a compact human-readable duration for planning output."""
    seconds = max(float(seconds), 0.0)
    if seconds < 60:
        return f"{round(seconds, 1):g}s"
    total_seconds = int(round(seconds))
    minutes, remaining_seconds = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m {remaining_seconds}s"
    hours, remaining_minutes = divmod(minutes, 60)
    return f"{hours}h {remaining_minutes}m"


def _formula_backfill_next_action(
    *,
    processed: int,
    failed_papers: int,
    candidate_count: int,
    data_egress: bool,
    daily_call_budget: int,
) -> str:
    """Summarize the likely next step after a formula backfill estimate."""
    if processed == 0:
        return "No already-indexed PDFs matched this request; check the item filters or index papers first."
    if failed_papers == processed:
        return "Candidate detection failed for every matched PDF; inspect the per-paper errors before backfilling."
    if candidate_count == 0:
        return "No formula candidates were found; running index_formulas is unlikely to add formula chunks."
    if daily_call_budget > 0 and data_egress:
        return "Run index_formulas with the same daily_call_budget; rerun tomorrow with resume_after=resume_cursor."
    if failed_papers:
        return "Some PDFs failed candidate detection; review the per-paper errors, then backfill the remaining papers."
    if data_egress:
        return "SimpleTex is configured; review the external-call estimate and endpoint before running index_formulas."
    return "Local formula OCR is configured; run index_formulas when ready."


def _formula_backfill_warnings(
    *,
    processed: int,
    failed_papers: int,
    candidate_count: int,
    provider_calls: int | None = None,
    data_egress: bool,
    daily_call_budget: int,
) -> list[str]:
    """Collect concise caveats for formula backfill planning."""
    warnings: list[str] = []
    metered_calls = candidate_count if provider_calls is None else provider_calls
    if processed == 0:
        warnings.append("No already-indexed PDFs matched this request.")
    if failed_papers:
        warnings.append(f"{failed_papers} paper(s) failed local candidate detection.")
    if candidate_count == 0:
        warnings.append("No formula candidates were detected.")
    if data_egress and metered_calls > 0:
        warnings.append("SimpleTex will send formula crops to the configured HTTPS endpoint.")
    if daily_call_budget > 0 and data_egress and metered_calls > daily_call_budget:
        warnings.append("The estimate exceeds the daily call budget; the backfill will need multiple runs.")
    return warnings


def _looks_like_formula_quota_error(exc: Exception) -> bool:
    """Classify provider errors that should stop formula backfill immediately."""
    message = str(exc).lower()
    hints = (
        "401", "402", "429", "balance", "insufficient", "limit", "quota", "rate",
        "budget", "余额", "次数", "额度", "限流",
    )
    return any(hint in message for hint in hints)


def _looks_like_formula_daily_budget_error(exc: Exception) -> bool:
    """Return true for local daily-budget stops raised before a provider request."""
    return "daily call budget" in str(exc).lower()


def _provider_attempts_used(provider: object | None) -> int | None:
    """Read provider-reported actual request attempts when available."""
    if provider is None:
        return None
    value = getattr(provider, "attempts_used", None)
    return value if isinstance(value, int) else None


def _provider_attempt_delta(provider: object | None, before: int | None, fallback: int) -> int:
    """Return actual provider attempts since ``before`` or a conservative fallback."""
    after = _provider_attempts_used(provider)
    if before is None or after is None:
        return max(int(fallback), 0)
    return max(after - before, 0)


def _looks_like_split_formula_latex(latex: str) -> bool:
    cleaned = (latex or "").strip()
    if not cleaned:
        return False
    if _formula_latex_has_relation(cleaned):
        return False
    head = cleaned[:120]
    if re.match(r"^(?:\\left|\\right|\\big|\\Big|\\bigg|\\Bigg|\[|\]|\)|,|;)", head):
        return "=" not in head
    brace_balance = cleaned.count("{") - cleaned.count("}")
    return abs(brace_balance) > 5


_STRUCTURAL_FORMULA_REVIEW_REASONS = frozenset({
    "duplicate_equation_number",
    "fallback_truncated",
    "missing_equation_number",
    "numbering_sequence_gap",
    "possible_split_formula",
})


_BLOCKING_CANDIDATE_REVIEW_WARNINGS = frozenset({
    "cached_latex_low_quality",
    "cached_latex_missing_equation_numbers",
    "duplicate_equation_numbers",
    "equation_number_regression",
    "large_equation_number_gap",
    "missing_equation_number_gap",
})


def _formula_candidate_blocking_review_reasons(candidate_audit: dict[str, object]) -> list[str]:
    """Return candidate-stage warnings that should block OCR/index writes by default."""
    warnings = candidate_audit.get("equation_number_warnings", [])
    reasons = {
        warning for warning in warnings
        if isinstance(warning, str) and warning in _BLOCKING_CANDIDATE_REVIEW_WARNINGS
    }
    if candidate_audit.get("has_truncated_source"):
        reasons.add("fallback_truncated")
    return sorted(reasons)


def _formula_candidate_quality_blocking_row(
    *,
    item_key: str,
    title: str,
    candidate_count: int,
    candidate_audit: dict[str, object],
    review_reasons: list[str],
) -> dict[str, object]:
    """Build the read-only estimate row for papers that should not be written yet."""
    return {
        "item_key": item_key,
        "title": title,
        "candidate_count": candidate_count,
        "review_reasons": review_reasons,
        "equation_number_warnings": candidate_audit.get("equation_number_warnings", []),
        "truncated_source_count": candidate_audit.get("truncated_source_count", 0),
        "cached_latex_missing_equation_number_count": candidate_audit.get(
            "cached_latex_missing_equation_number_count",
            0,
        ),
        "cached_latex_low_quality_count": candidate_audit.get(
            "cached_latex_low_quality_count",
            0,
        ),
        "duplicate_equation_numbers": candidate_audit.get("duplicate_equation_numbers", []),
        "equation_number_sequence_breaks": candidate_audit.get(
            "equation_number_sequence_breaks",
            [],
        ),
    }


def _structural_formula_review_reasons(review_rows: list[dict[str, object]]) -> list[str]:
    """Return structural review reasons that should block formula index writes."""
    reasons: set[str] = set()
    for row in review_rows:
        row_reasons = row.get("review_reasons", [])
        if not isinstance(row_reasons, list):
            continue
        reasons.update(
            reason for reason in row_reasons
            if isinstance(reason, str) and reason in _STRUCTURAL_FORMULA_REVIEW_REASONS
        )
    return sorted(reasons)


def _formula_review_reason_names(review_rows: list[dict[str, object]]) -> list[str]:
    """Return all review reason names in stable order."""
    reasons: set[str] = set()
    for row in review_rows:
        row_reasons = row.get("review_reasons", [])
        if not isinstance(row_reasons, list):
            continue
        reasons.update(reason for reason in row_reasons if isinstance(reason, str))
    return sorted(reasons)


def _append_formula_numbering_review_reasons(formulas: list, reasons_by_index: list[list[str]]) -> None:
    regular_numbers: list[tuple[int, int]] = []
    seen: dict[str, list[int]] = {}
    duplicates: set[str] = set()
    for index, formula in enumerate(formulas):
        equation_number = getattr(formula, "equation_number", "")
        if not equation_number:
            continue
        seen.setdefault(equation_number, []).append(index)
        if _duplicate_equation_number_needs_review(equation_number, seen[equation_number], formulas):
            duplicates.add(equation_number)
        value = _regular_equation_number_value(equation_number)
        if value is not None:
            regular_numbers.append((index, value))

    for index, formula in enumerate(formulas):
        if getattr(formula, "equation_number", "") in duplicates:
            reasons_by_index[index].append("duplicate_equation_number")

    if len(regular_numbers) < 3:
        return
    values = sorted({value for _index, value in regular_numbers})
    if values[0] != 1:
        return
    expected = set(range(1, values[-1] + 1))
    if expected == set(values):
        return
    for index, _value in regular_numbers:
        reasons_by_index[index].append("numbering_sequence_gap")


def _duplicate_equation_number_needs_review(equation_number: str, indices: list[int], formulas: list) -> bool:
    if len(indices) < 2:
        return False
    pages = [
        int(getattr(formulas[index], "page_num", 0) or 0)
        for index in indices
    ]
    pages = [page for page in pages if page > 0]
    if len(pages) >= 2 and min(
        abs(right - left)
        for left_index, left in enumerate(pages)
        for right in pages[left_index + 1:]
    ) > 20:
        return False
    return bool(equation_number)


def _regular_equation_number_value(equation_number: str) -> int | None:
    match = re.fullmatch(r"\((\d+)(?:[A-Za-z])?\)", equation_number or "")
    if match is None:
        return None
    value = int(match.group(1))
    return value if 1 <= value <= 80 else None


def _formula_latex_has_relation(latex: str) -> bool:
    relation_pattern = (
        r"(?:=|≈|≤|≥|≠|<|>|:=|"
        r"\\(?:leqslant|geqslant|leq|geq|le|ge|approx|sim)\b)"
    )
    return bool(re.search(relation_pattern, latex))


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

    @classmethod
    def for_formula_estimate(cls, config: Config) -> "Indexer":
        """Create a minimal read-only indexer for formula backfill estimates."""
        self = cls.__new__(cls)
        self.config = config
        self.zotero = ZoteroClient(config.zotero_data_dir)
        self.store = _ReadOnlyIndexedDocStore(config.chroma_db_path)
        self._empty_docs_path = config.chroma_db_path / "empty_docs.json"
        self._config_hash_path = config.chroma_db_path / "config_hash.txt"
        self._formula_provider = None
        self._formula_candidate_provider = None
        return self

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
        if getattr(self, "_formula_provider", None) is None:
            from .feature_extraction.formula_ocr import create_formula_ocr_provider

            self._formula_provider = create_formula_ocr_provider(
                self.config.formula_ocr_provider,
                config=self.config,
            )
        return self._formula_provider

    def _get_formula_candidate_provider(self):
        """Create the configured formula candidate detector lazily."""
        if getattr(self, "_formula_candidate_provider", None) is None:
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

    def _recognize_formulas_for_item(
        self,
        item: ZoteroItem,
        *,
        candidates: list | None = None,
        formula_index_offset: int = 0,
        formula_indices: list[int] | None = None,
    ) -> list:
        """Run formula OCR for one item if possible."""
        if item.pdf_path is None or not item.pdf_path.exists():
            return []
        from .feature_extraction.formula_ocr import count_formula_provider_calls, recognize_formulas

        if candidates is None:
            candidates = self._extract_formula_candidates_for_item(item)
        provider = self._get_formula_provider() if count_formula_provider_calls(candidates) > 0 else None

        return recognize_formulas(
            item.pdf_path,
            provider,
            max_formulas_per_doc=self.config.formula_ocr_max_formulas_per_doc,
            max_formulas_per_page=self.config.formula_ocr_max_formulas_per_page,
            min_confidence=self.config.formula_ocr_min_confidence,
            candidates=candidates,
            formula_index_offset=formula_index_offset,
            formula_indices=formula_indices,
        )

    def _extract_formula_candidates_for_item(
        self,
        item: ZoteroItem,
        *,
        max_candidates_per_doc: int = 0,
        max_formulas_per_doc: int | None = None,
        max_formulas_per_page: int | None = None,
        pdf_fallback_max_pages: int | None = None,
    ) -> list:
        """Extract formula candidates for one item using the configured detector."""
        if item.pdf_path is None or not item.pdf_path.exists():
            return []
        from .feature_extraction.formula_ocr import extract_formula_candidates

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
        return extract_formula_candidates(
            item.pdf_path,
            item_key=item.item_key,
            cache_paths=cache_paths,
            max_formulas_per_doc=(
                self.config.formula_ocr_max_formulas_per_doc
                if max_formulas_per_doc is None
                else max_formulas_per_doc
            ),
            max_formulas_per_page=(
                self.config.formula_ocr_max_formulas_per_page
                if max_formulas_per_page is None
                else max_formulas_per_page
            ),
            max_candidates_per_doc=max_candidates_per_doc,
            min_confidence=self.config.formula_ocr_min_confidence,
            candidate_provider=self._get_formula_candidate_provider(),
            pdf_fallback_max_pages=(
                getattr(self.config, "formula_candidate_pdf_fallback_max_pages", 80)
                if pdf_fallback_max_pages is None
                else pdf_fallback_max_pages
            ),
        )

    def index_formulas(
        self,
        *,
        item_key: str | None = None,
        item_keys: list[str] | None = None,
        limit: int | None = None,
        refresh_existing: bool = True,
        daily_call_budget: int | None = None,
        resume_after: str | None = None,
        stop_on_quota: bool = True,
        status_jsonl: Path | str | None = None,
        low_confidence_threshold: float | None = None,
        include_high_density: bool = False,
        allow_candidate_quality_warnings: bool = False,
        pdf_fallback_max_pages: int | None = None,
        page_min: int | None = None,
        page_max: int | None = None,
    ) -> dict:
        """Backfill formula chunks for already-indexed documents."""
        if not self.config.formula_ocr_enabled:
            raise ValueError("formula_ocr_enabled must be true before running formula backfill")
        self._assert_config_hash_current()
        page_min, page_max = _validate_formula_page_range(page_min, page_max)
        _assert_single_item_for_formula_page_range(
            item_key=item_key,
            item_keys=item_keys,
            page_min=page_min,
            page_max=page_max,
        )
        partial_page_backfill = _formula_page_range_requested(page_min, page_max)

        budget = (
            int(getattr(self.config, "formula_ocr_daily_call_budget", 0) or 0)
            if daily_call_budget is None
            else int(daily_call_budget)
        )
        if budget < 0:
            raise ValueError("daily_call_budget must be >= 0")
        review_threshold = (
            float(getattr(self.config, "formula_ocr_low_confidence_threshold", 0.0) or 0.0)
            if low_confidence_threshold is None
            else float(low_confidence_threshold)
        )
        if not 0.0 <= review_threshold <= 1.0:
            raise ValueError("low_confidence_threshold must be between 0.0 and 1.0")
        effective_pdf_fallback_max_pages = (
            int(getattr(self.config, "formula_candidate_pdf_fallback_max_pages", 80) or 0)
            if pdf_fallback_max_pages is None
            else int(pdf_fallback_max_pages)
        )
        if effective_pdf_fallback_max_pages < 0:
            raise ValueError("pdf_fallback_max_pages must be >= 0")

        provider_name = getattr(self.config, "formula_ocr_provider", "local")
        candidate_provider_name = getattr(self.config, "formula_candidate_provider", "text_layer")
        has_external_egress = provider_name == "simpletex"
        high_density_threshold = _formula_high_density_call_threshold(self.config)
        high_density_candidate_threshold = _formula_high_density_candidate_threshold(self.config)
        if has_external_egress and daily_call_budget is None and budget <= 0:
            raise ValueError(
                "SimpleTex formula backfill requires formula_ocr_daily_call_budget > 0. "
                "Pass daily_call_budget=0 explicitly only when you intentionally want an uncapped run."
            )
        state_path = (
            _formula_backfill_state_path(self.config)
            if status_jsonl == ""
            else Path(status_jsonl).expanduser() if status_jsonl is not None else None
        )
        matched_items, matched_skipped = self._formula_backfill_selection(
            item_key=item_key,
            item_keys=item_keys,
        )
        matched_keys = {item.item_key for item in matched_items}
        matched_keys.update(item.item_key for item, _reason in matched_skipped)
        requested_keys = (
            [item_key]
            if item_key
            else list(dict.fromkeys(item_keys or []))
        )
        unmatched_requested_item_keys = [
            key for key in requested_keys
            if key and key not in matched_keys
        ]
        resume_after_found = (
            resume_after is None
            or resume_after in matched_keys
        )
        items, skipped_items = self._formula_backfill_selection(
            item_key=item_key,
            item_keys=item_keys,
            limit=limit,
            resume_after=resume_after,
        )

        from .feature_extraction.formula_ocr import count_formula_provider_calls

        results: list[dict[str, object]] = [
            _formula_backfill_row(
                item_key=item.item_key,
                title=item.title,
                status="skipped",
                reason=reason,
            )
            for item, reason in skipped_items
        ]
        low_confidence_review_queue: list[dict[str, object]] = []
        candidate_quality_review_queue: list[dict[str, object]] = []
        provider_calls_used = 0
        external_calls_used = 0
        stopped_reason = ""
        resume_cursor = ""
        next_item_key = ""
        next_item_candidate_count = 0
        run_warnings: list[str] = []
        run_id = uuid.uuid4().hex
        if not resume_after_found:
            run_warnings.append(
                f"resume_after item_key {resume_after!r} was not found in the matched backfill set."
            )
        if skipped_items:
            run_warnings.append(
                f"{len(skipped_items)} bilingual/translated PDF(s) skipped; "
                "formula backfill only processes original PDFs."
            )
        if unmatched_requested_item_keys:
            run_warnings.append(
                f"{len(unmatched_requested_item_keys)} requested item_key(s) were not matched "
                "to already-indexed ZotPilot papers with available original PDFs."
            )
        if partial_page_backfill:
            run_warnings.append(
                "Page-window formula backfill is append-only; existing formula chunks outside the page range "
                "will not be replaced or deleted."
            )
        _append_formula_backfill_state(
            state_path,
            {
                "event": "formula_backfill_run_started",
                "run_id": run_id,
                "provider": provider_name,
                "candidate_provider": candidate_provider_name,
                "selected": len(items) + len(skipped_items),
                "matched": len(matched_items) + len(matched_skipped),
                "skipped": len(skipped_items),
                "unmatched_requested_item_key_count": len(unmatched_requested_item_keys),
                "unmatched_requested_item_keys": unmatched_requested_item_keys,
                "daily_call_budget": budget,
                "high_density_call_threshold": high_density_threshold,
                "high_density_candidate_threshold": high_density_candidate_threshold,
                "include_high_density": include_high_density,
                "allow_candidate_quality_warnings": allow_candidate_quality_warnings,
                "pdf_fallback_max_pages": effective_pdf_fallback_max_pages,
                "page_min": page_min or 0,
                "page_max": page_max or 0,
                "page_range_backfill": partial_page_backfill,
                "resume_after": resume_after or "",
                "resume_after_found": resume_after_found,
                "data_egress": has_external_egress,
            },
        )
        for row in results:
            _append_formula_backfill_state(state_path, {**row, "run_id": run_id})
        batch_candidate_scan_limit = (
            high_density_candidate_threshold + 1
            if (
                high_density_candidate_threshold > 0
                and item_key is None
                and not include_high_density
                and not partial_page_backfill
            )
            else 0
        )
        for item in items:
            try:
                candidates = self._extract_formula_candidates_for_item(
                    item,
                    max_candidates_per_doc=batch_candidate_scan_limit,
                    max_formulas_per_doc=0 if partial_page_backfill else None,
                    max_formulas_per_page=0 if partial_page_backfill else None,
                    pdf_fallback_max_pages=effective_pdf_fallback_max_pages,
                )
                candidates, formula_indices = _filter_formula_candidates_by_page_range(
                    candidates,
                    page_min=page_min,
                    page_max=page_max,
                )
            except Exception as exc:
                formula_indices = []
                row = _formula_backfill_row(
                    item_key=item.item_key,
                    title=item.title,
                    status="failed",
                    reason="candidate_detection_failed",
                    error=type(exc).__name__,
                    error_message=str(exc),
                )
                results.append(row)
                resume_cursor = item.item_key
                _append_formula_backfill_state(state_path, {**row, "run_id": run_id})
                continue

            candidate_count = len(candidates)
            provider_call_count = count_formula_provider_calls(candidates)
            high_density_trigger = _formula_high_density_trigger(
                candidate_count=candidate_count,
                provider_call_count=provider_call_count,
                call_threshold=high_density_threshold,
                candidate_threshold=high_density_candidate_threshold,
                data_egress=has_external_egress,
            )
            if high_density_trigger and not include_high_density and item_key is None:
                row = _formula_backfill_row(
                    item_key=item.item_key,
                    title=item.title,
                    status="deferred_high_density",
                    reason="high_density_formula_document",
                    candidate_count=candidate_count,
                    provider_calls=provider_call_count,
                    external_calls=provider_call_count if has_external_egress else 0,
                    high_density_call_threshold=high_density_threshold,
                    high_density_candidate_threshold=high_density_candidate_threshold,
                )
                results.append(row)
                resume_cursor = item.item_key
                trigger_text = (
                    f"{candidate_count} formula candidate(s) > threshold {high_density_candidate_threshold}"
                    if high_density_trigger == "candidate_count"
                    else f"{provider_call_count} OCR call(s) > threshold {high_density_threshold}"
                )
                run_warnings.append(
                    f"{item.item_key} is a high-density formula document "
                    f"({trigger_text}); "
                    "backfill it separately after reviewing the estimate."
                )
                _append_formula_backfill_state(state_path, {**row, "run_id": run_id})
                continue
            candidate_audit = _formula_candidate_audit(candidates) if candidates else {}
            candidate_review_reasons = (
                []
                if allow_candidate_quality_warnings
                else _formula_candidate_blocking_review_reasons(candidate_audit)
            )
            if candidate_review_reasons:
                existing_formula_count = (
                    self._count_existing_formulas(item.item_key)
                    if refresh_existing and not partial_page_backfill
                    else 0
                )
                row = _formula_backfill_row(
                    item_key=item.item_key,
                    title=item.title,
                    status="needs_review",
                    reason="formula_candidate_review_required",
                    candidate_count=candidate_count,
                    provider_calls=provider_call_count,
                    external_calls=provider_call_count if has_external_egress else 0,
                    n_formulas=0,
                    existing_formulas_kept=existing_formula_count,
                    review_reasons=candidate_review_reasons,
                )
                row["candidate_audit"] = candidate_audit
                candidate_quality_review_queue.append(row)
                results.append(row)
                resume_cursor = item.item_key
                run_warnings.append(
                    f"{item.item_key} candidate quality warning(s) require review before formula backfill: "
                    f"{', '.join(candidate_review_reasons)}."
                )
                _append_formula_backfill_state(state_path, {**row, "run_id": run_id})
                continue
            remaining_budget = budget - provider_calls_used if budget > 0 else None
            if remaining_budget is not None and provider_call_count > remaining_budget:
                stopped_reason = "daily_call_budget"
                next_item_key = item.item_key
                next_item_candidate_count = candidate_count
                reason = (
                    "single_paper_exceeds_daily_budget"
                    if provider_call_count > budget
                    else "provider_calls_exceed_remaining_budget"
                )
                if reason == "single_paper_exceeds_daily_budget":
                    run_warnings.append(
                        f"{item.item_key} needs {provider_call_count} OCR call(s), "
                        f"more than the daily budget {budget}; "
                        "raise the budget or backfill this paper separately."
                    )
                row = _formula_backfill_row(
                    item_key=item.item_key,
                    title=item.title,
                    status="deferred_budget",
                    reason=reason,
                    candidate_count=candidate_count,
                    provider_calls=provider_call_count,
                )
                results.append(row)
                _append_formula_backfill_state(state_path, {**row, "run_id": run_id})
                break

            if provider_call_count > 0:
                self._ensure_formula_provider_available()
            provider_for_item = None
            attempts_before = None
            if provider_call_count > 0 and has_external_egress:
                provider_for_item = self._get_formula_provider()
                attempts_before = _provider_attempts_used(provider_for_item)
                budget_setter = getattr(provider_for_item, "set_attempt_budget", None)
                if callable(budget_setter):
                    budget_setter(remaining_budget)

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
            try:
                formulas = self._recognize_formulas_for_item(
                    item,
                    candidates=candidates,
                    formula_indices=formula_indices,
                )
            except Exception as exc:
                actual_provider_calls = _provider_attempt_delta(provider_for_item, attempts_before, 0)
                provider_calls_used += actual_provider_calls
                if has_external_egress:
                    external_calls_used += actual_provider_calls
                if _looks_like_formula_daily_budget_error(exc):
                    stopped_reason = "daily_call_budget"
                    next_item_key = item.item_key
                    next_item_candidate_count = candidate_count
                    row = _formula_backfill_row(
                        item_key=item.item_key,
                        title=item.title,
                        status="deferred_budget",
                        reason="provider_attempts_exhausted_daily_budget",
                        candidate_count=candidate_count,
                        provider_calls=actual_provider_calls,
                        external_calls=actual_provider_calls if has_external_egress else 0,
                        error=type(exc).__name__,
                        error_message=str(exc),
                    )
                    results.append(row)
                    _append_formula_backfill_state(state_path, {**row, "run_id": run_id})
                    break
                if stop_on_quota and _looks_like_formula_quota_error(exc):
                    stopped_reason = "provider_quota_or_rate_limit"
                    next_item_key = item.item_key
                    next_item_candidate_count = candidate_count
                    row = _formula_backfill_row(
                        item_key=item.item_key,
                        title=item.title,
                        status="stopped_quota",
                        reason="provider_quota_or_rate_limit",
                        candidate_count=candidate_count,
                        provider_calls=actual_provider_calls or provider_call_count,
                        external_calls=(actual_provider_calls or provider_call_count) if has_external_egress else 0,
                        error=type(exc).__name__,
                        error_message=str(exc),
                    )
                    results.append(row)
                    _append_formula_backfill_state(state_path, {**row, "run_id": run_id})
                    break
                row = _formula_backfill_row(
                    item_key=item.item_key,
                    title=item.title,
                    status="failed",
                    reason="formula_ocr_failed",
                    candidate_count=candidate_count,
                    error=type(exc).__name__,
                    error_message=str(exc),
                )
                results.append(row)
                resume_cursor = item.item_key
                _append_formula_backfill_state(state_path, {**row, "run_id": run_id})
                continue

            actual_provider_calls = _provider_attempt_delta(
                provider_for_item,
                attempts_before,
                provider_call_count,
            )
            provider_calls_used += actual_provider_calls
            if has_external_egress:
                external_calls_used += actual_provider_calls
            review_rows = self._formula_review_rows(
                item=item,
                formulas=formulas,
                threshold=review_threshold,
            )
            review_rows.extend(self._formula_ocr_failure_review_rows(
                item=item,
                candidates=candidates,
                formulas=formulas,
                formula_indices=formula_indices,
            ))
            blocking_review_reasons = _structural_formula_review_reasons(review_rows)
            review_reason_names = _formula_review_reason_names(review_rows)
            existing_formula_count = (
                self._count_existing_formulas(item.item_key)
                if refresh_existing and not partial_page_backfill
                else 0
            )
            kept_existing = 0
            stored_formula_count = 0
            partial_replace_blocked = (
                refresh_existing
                and existing_formula_count > 0
                and "ocr_failed" in review_reason_names
            )
            if blocking_review_reasons or partial_replace_blocked or (review_rows and not formulas):
                low_confidence_review_queue.extend(review_rows)
                kept_existing = existing_formula_count
                logger.warning(
                    "Formula backfill for %s needs structural review (%s); keeping %d existing formula chunk(s)",
                    item.item_key,
                    ", ".join(blocking_review_reasons or review_reason_names),
                    kept_existing,
                )
                row = _formula_backfill_row(
                    item_key=item.item_key,
                    title=item.title,
                    status="needs_review",
                    reason="formula_structural_review_required",
                    candidate_count=candidate_count,
                    provider_calls=provider_call_count,
                    external_calls=provider_call_count if has_external_egress else 0,
                    n_formulas=0,
                    existing_formulas_kept=kept_existing,
                    low_confidence_count=len(review_rows),
                    review_reasons=blocking_review_reasons or review_reason_names,
                )
                results.append(row)
                resume_cursor = item.item_key
                _append_formula_backfill_state(state_path, {**row, "run_id": run_id})
                continue
            if refresh_existing and formulas and not partial_page_backfill:
                stored_formula_count = self.store.replace_formulas(item.item_key, doc_meta, formulas)
            elif refresh_existing and existing_formula_count > 0:
                kept_existing = existing_formula_count
                logger.warning(
                    "Formula backfill found 0 formulas for %s; keeping %d existing formula chunk(s)",
                    item.item_key,
                    existing_formula_count,
                )
            if formulas and (not refresh_existing or partial_page_backfill):
                stored_formula_count = self.store.add_new_formulas(item.item_key, doc_meta, formulas)
            if formulas and not isinstance(stored_formula_count, int):
                stored_formula_count = len(formulas)
            low_confidence_review_queue.extend(review_rows)
            row = _formula_backfill_row(
                item_key=item.item_key,
                title=item.title,
                status="indexed_with_review" if formulas and review_rows else "indexed" if formulas else "no_formula",
                reason=(
                    "formula_review_required"
                    if formulas and review_rows
                    else "" if formulas else "no_formula_recognized"
                ),
                candidate_count=candidate_count,
                provider_calls=provider_call_count,
                external_calls=provider_call_count if has_external_egress else 0,
                n_formulas=stored_formula_count,
                existing_formulas_kept=kept_existing,
                low_confidence_count=len(review_rows),
                review_reasons=review_reason_names,
            )
            results.append(row)
            resume_cursor = item.item_key
            _append_formula_backfill_state(state_path, {**row, "run_id": run_id})

        processed_count = sum(
            1 for row in results
            if row.get("status") not in {
                "deferred_budget",
                "deferred_high_density",
                "skipped",
                "stopped_quota",
            }
        )
        formulas_indexed = sum(
            row["n_formulas"] if isinstance(row.get("n_formulas"), int) else 0
            for row in results
        )
        high_density_deferred_count = sum(
            1 for row in results
            if row.get("status") == "deferred_high_density"
        )
        result = {
            "run_id": run_id,
            "provider": provider_name,
            "candidate_provider": candidate_provider_name,
            "processed": processed_count,
            "selected": len(items) + len(skipped_items),
            "matched": len(matched_items) + len(matched_skipped),
            "skipped": len(skipped_items),
            "unmatched_requested_item_key_count": len(unmatched_requested_item_keys),
            "unmatched_requested_item_keys": unmatched_requested_item_keys,
            "formulas_indexed": formulas_indexed,
            "provider_calls_used": provider_calls_used,
            "external_calls_used": external_calls_used,
            "daily_call_budget": budget,
            "high_density_call_threshold": high_density_threshold,
            "high_density_candidate_threshold": high_density_candidate_threshold,
            "high_density_deferred_count": high_density_deferred_count,
            "include_high_density": include_high_density,
            "allow_candidate_quality_warnings": allow_candidate_quality_warnings,
            "pdf_fallback_max_pages": effective_pdf_fallback_max_pages,
            "page_min": page_min or 0,
            "page_max": page_max or 0,
            "page_range_backfill": partial_page_backfill,
            "daily_call_budget_remaining": max(budget - provider_calls_used, 0) if budget > 0 else None,
            "budget_exhausted": stopped_reason == "daily_call_budget",
            "stopped_reason": stopped_reason,
            "resume_cursor": resume_cursor,
            "next_item_key": next_item_key,
            "next_item_candidate_count": next_item_candidate_count,
            "resume_after_found": resume_after_found,
            "state_path": str(state_path) if state_path is not None else "",
            "candidate_quality_review_count": len(candidate_quality_review_queue),
            "candidate_quality_review_queue": candidate_quality_review_queue,
            "low_confidence_review_count": len(low_confidence_review_queue),
            "low_confidence_review_queue": low_confidence_review_queue,
            "warnings": run_warnings,
            "results": results,
        }
        _append_formula_backfill_state(
            state_path,
            {
                "event": "formula_backfill_run_finished",
                "run_id": run_id,
                "processed": result["processed"],
                "selected": result["selected"],
                "skipped": result["skipped"],
                "unmatched_requested_item_key_count": result["unmatched_requested_item_key_count"],
                "unmatched_requested_item_keys": result["unmatched_requested_item_keys"],
                "formulas_indexed": result["formulas_indexed"],
                "provider_calls_used": result["provider_calls_used"],
                "external_calls_used": result["external_calls_used"],
                "candidate_quality_review_count": result["candidate_quality_review_count"],
                "high_density_deferred_count": high_density_deferred_count,
                "high_density_call_threshold": high_density_threshold,
                "high_density_candidate_threshold": high_density_candidate_threshold,
                "pdf_fallback_max_pages": effective_pdf_fallback_max_pages,
                "page_min": page_min or 0,
                "page_max": page_max or 0,
                "page_range_backfill": partial_page_backfill,
                "stopped_reason": stopped_reason,
                "resume_cursor": resume_cursor,
                "next_item_key": next_item_key,
                "next_item_candidate_count": next_item_candidate_count,
                "warnings": run_warnings,
            },
        )
        return result

    def estimate_formula_backfill(
        self,
        *,
        item_key: str | None = None,
        item_keys: list[str] | None = None,
        limit: int | None = None,
        resume_after: str | None = None,
        daily_call_budget: int | None = None,
        candidate_preview_limit: int = 0,
        candidate_preview_chars: int = 160,
        pdf_fallback_max_pages: int | None = None,
        page_min: int | None = None,
        page_max: int | None = None,
        sample_size: int | None = None,
        sample_seed: int = 0,
    ) -> dict:
        """Estimate formula OCR candidate volume without OCR calls or index writes."""
        self._assert_config_hash_current()
        from .feature_extraction.formula_ocr import count_formula_provider_calls

        page_min, page_max = _validate_formula_page_range(page_min, page_max)
        _assert_single_item_for_formula_page_range(
            item_key=item_key,
            item_keys=item_keys,
            page_min=page_min,
            page_max=page_max,
        )
        partial_page_estimate = _formula_page_range_requested(page_min, page_max)
        provider_name = getattr(self.config, "formula_ocr_provider", "local")
        candidate_provider_name = getattr(self.config, "formula_candidate_provider", "text_layer")
        min_interval = float(getattr(self.config, "formula_ocr_simpletex_min_interval", 0.55))
        has_external_egress = provider_name == "simpletex"
        high_density_threshold = _formula_high_density_call_threshold(self.config)
        high_density_candidate_threshold = _formula_high_density_candidate_threshold(self.config)
        budget = (
            int(getattr(self.config, "formula_ocr_daily_call_budget", 0) or 0)
            if daily_call_budget is None
            else int(daily_call_budget)
        )
        if budget < 0:
            raise ValueError("daily_call_budget must be >= 0")
        requested_keys = (
            [item_key]
            if item_key
            else list(dict.fromkeys(item_keys or []))
        )
        effective_sample_size = int(sample_size or 0)
        if effective_sample_size < 0:
            raise ValueError("sample_size must be >= 0")
        if effective_sample_size > 0 and (item_key or item_keys):
            raise ValueError("sample_size can only be used without item_key or item_keys")
        effective_sample_seed = int(sample_seed)
        effective_pdf_fallback_max_pages = (
            int(getattr(self.config, "formula_candidate_pdf_fallback_max_pages", 80) or 0)
            if pdf_fallback_max_pages is None
            else int(pdf_fallback_max_pages)
        )
        if effective_pdf_fallback_max_pages < 0:
            raise ValueError("pdf_fallback_max_pages must be >= 0")
        if effective_sample_size > 0:
            indexed_keys = sorted(self.store.get_indexed_doc_ids())
            matched_keys = set(indexed_keys)
            resume_after_found = resume_after is None or resume_after in matched_keys
            candidate_keys = indexed_keys
            if resume_after:
                candidate_keys = _keys_after_resume(candidate_keys, resume_after)
            if limit:
                candidate_keys = candidate_keys[:limit]
            sampled_from = len(candidate_keys)
            sampled_key_pool = list(candidate_keys)
            rng = random.Random(effective_sample_seed)
            raw_items = []
            sampled_unresolved_key_count = 0
            unmatched_requested_item_keys: list[str] = []
            while sampled_key_pool and len(raw_items) < effective_sample_size:
                remaining_needed = effective_sample_size - len(raw_items)
                draw_count = min(len(sampled_key_pool), remaining_needed)
                sampled_keys = rng.sample(sampled_key_pool, draw_count)
                sampled_key_set = set(sampled_keys)
                sampled_key_pool = [
                    key for key in sampled_key_pool
                    if key not in sampled_key_set
                ]
                for sampled_key in sampled_keys:
                    item = self.zotero.get_item(sampled_key)
                    if item is not None and item.pdf_path and item.pdf_path.exists():
                        raw_items.append(item)
                    else:
                        sampled_unresolved_key_count += 1
                    if len(raw_items) >= effective_sample_size:
                        break
            matched_count = len(indexed_keys)
        else:
            sampled_unresolved_key_count = 0
            matched_items = self._formula_backfill_candidate_items(
                item_key=item_key,
                item_keys=item_keys,
            )
            matched_keys = {item.item_key for item in matched_items}
            unmatched_requested_item_keys = [
                key for key in requested_keys
                if key and key not in matched_keys
            ]
            resume_after_found = (
                resume_after is None
                or resume_after in matched_keys
            )
            raw_items = self._formula_backfill_candidate_items(
                item_key=item_key,
                item_keys=item_keys,
                limit=limit,
                resume_after=resume_after,
            )
            sampled_from = len(raw_items)
            matched_count = len(matched_items)
        items, skipped_items = self._filter_formula_backfill_original_items(raw_items)

        results: list[dict[str, object]] = [
            _formula_backfill_estimate_skip_row(item, reason)
            for item, reason in skipped_items
        ]
        total_candidates = 0
        total_provider_calls = 0
        normal_batch_candidates = 0
        normal_batch_provider_calls = 0
        deferred_high_density_candidates = 0
        deferred_high_density_provider_calls = 0
        failed_papers = 0
        high_call_papers: list[dict[str, object]] = []
        dense_formula_papers: list[dict[str, object]] = []
        high_density_backfill_plans: list[dict[str, object]] = []
        scan_limited_high_density_papers: list[dict[str, object]] = []
        truncated_candidate_papers: list[dict[str, object]] = []
        cached_latex_missing_number_papers: list[dict[str, object]] = []
        candidate_quality_blocking_papers: list[dict[str, object]] = []
        batch_candidate_scan_limit = (
            high_density_candidate_threshold + 1
            if (
                high_density_candidate_threshold > 0
                and item_key is None
                and candidate_preview_limit >= 0
                and not partial_page_estimate
            )
            else 0
        )
        for item in items:
            try:
                candidates = self._extract_formula_candidates_for_item(
                    item,
                    max_candidates_per_doc=batch_candidate_scan_limit,
                    max_formulas_per_doc=0 if partial_page_estimate else None,
                    max_formulas_per_page=0 if partial_page_estimate else None,
                    pdf_fallback_max_pages=effective_pdf_fallback_max_pages,
                )
                candidates, _ = _filter_formula_candidates_by_page_range(
                    candidates,
                    page_min=page_min,
                    page_max=page_max,
                )
                candidate_count = len(candidates)
                provider_call_count = count_formula_provider_calls(candidates)
                error = ""
            except Exception as exc:
                candidates = []
                candidate_count = 0
                provider_call_count = 0
                failed_papers += 1
                error = type(exc).__name__
            total_candidates += candidate_count
            total_provider_calls += provider_call_count
            high_density_trigger = _formula_high_density_trigger(
                candidate_count=candidate_count,
                provider_call_count=provider_call_count,
                call_threshold=high_density_threshold,
                candidate_threshold=high_density_candidate_threshold,
                data_egress=has_external_egress,
            )
            is_deferred_high_density = bool(high_density_trigger and item_key is None)
            high_density_scan_limited = (
                bool(high_density_trigger)
                and batch_candidate_scan_limit > 0
                and candidate_count >= batch_candidate_scan_limit
            )
            high_density_truncated = bool(high_density_trigger) and any(
                str(getattr(candidate, "source", "") or "").endswith("truncated")
                for candidate in candidates
            )
            if is_deferred_high_density:
                deferred_high_density_candidates += candidate_count
                deferred_high_density_provider_calls += provider_call_count
            else:
                normal_batch_candidates += candidate_count
                normal_batch_provider_calls += provider_call_count
            row = {
                "item_key": item.item_key,
                "title": item.title,
                "candidate_count": candidate_count,
                "estimated_provider_calls": provider_call_count,
                "estimated_external_calls": provider_call_count if has_external_egress else 0,
                "error": error,
            }
            if candidates:
                candidate_audit = _formula_candidate_audit(candidates)
                row["candidate_audit"] = candidate_audit
                candidate_quality_review_reasons = _formula_candidate_blocking_review_reasons(
                    candidate_audit
                )
                if candidate_quality_review_reasons:
                    candidate_quality_blocking_papers.append(
                        _formula_candidate_quality_blocking_row(
                            item_key=item.item_key,
                            title=item.title,
                            candidate_count=candidate_count,
                            candidate_audit=candidate_audit,
                            review_reasons=candidate_quality_review_reasons,
                        )
                    )
                if candidate_audit["has_truncated_source"]:
                    truncated_candidate_papers.append({
                        "item_key": item.item_key,
                        "title": item.title,
                        "candidate_count": candidate_count,
                        "truncated_source_count": candidate_audit["truncated_source_count"],
                    })
                missing_cached_numbers = int(
                    candidate_audit.get("cached_latex_missing_equation_number_count", 0)
                    or 0
                )
                if missing_cached_numbers:
                    cached_latex_missing_number_papers.append({
                        "item_key": item.item_key,
                        "title": item.title,
                        "candidate_count": candidate_count,
                        "cached_latex_count": candidate_audit["cached_latex_count"],
                        "missing_equation_number_count": missing_cached_numbers,
                        "missing_equation_number_ratio": candidate_audit[
                            "cached_latex_missing_equation_number_ratio"
                        ],
                    })
            if is_deferred_high_density:
                row["default_batch_status"] = "deferred_high_density"
                row["high_density_call_threshold"] = high_density_threshold
                row["high_density_candidate_threshold"] = high_density_candidate_threshold
                row["high_density_trigger"] = high_density_trigger
            if has_external_egress and budget > 0 and provider_call_count > budget:
                high_call_papers.append({
                    "item_key": item.item_key,
                    "title": item.title,
                    "candidate_count": candidate_count,
                    "estimated_provider_calls": provider_call_count,
                    "estimated_external_calls": provider_call_count,
                })
            if high_density_trigger:
                dense_formula_row = {
                    "item_key": item.item_key,
                    "title": item.title,
                    "candidate_count": candidate_count,
                    "estimated_provider_calls": provider_call_count,
                    "estimated_external_calls": provider_call_count if has_external_egress else 0,
                    "high_density_call_threshold": high_density_threshold,
                    "high_density_candidate_threshold": high_density_candidate_threshold,
                    "high_density_trigger": high_density_trigger,
                }
                dense_formula_papers.append(dense_formula_row)
                if high_density_scan_limited or high_density_truncated:
                    scan_limited_high_density_papers.append({
                        "item_key": item.item_key,
                        "title": item.title,
                        "scanned_candidate_count": candidate_count,
                        "scan_limit": batch_candidate_scan_limit,
                        "reason": "truncated_candidate_source" if high_density_truncated else "scan_limit",
                    })
                else:
                    high_density_plan = _formula_high_density_backfill_plan(
                        item=item,
                        candidates=candidates,
                        data_egress=has_external_egress,
                        daily_call_budget=budget,
                        high_density_call_threshold=high_density_threshold,
                        high_density_candidate_threshold=high_density_candidate_threshold,
                    )
                    if high_density_plan:
                        high_density_plan["high_density_trigger"] = high_density_trigger
                        high_density_backfill_plans.append(high_density_plan)
            if candidate_preview_limit != 0 and candidates:
                row["candidate_preview"] = _formula_candidate_preview(
                    candidates,
                    limit=candidate_preview_limit,
                    text_limit=candidate_preview_chars,
                )
            results.append(row)

        estimated_min_duration_seconds = total_provider_calls * min_interval if has_external_egress else 0.0
        normal_estimated_min_duration_seconds = (
            normal_batch_provider_calls * min_interval if has_external_egress else 0.0
        )
        processed = len(items)
        selected = len(items) + len(skipped_items)
        average_candidates_per_paper = round(total_candidates / processed, 2) if processed else 0.0
        estimated_runs = max(1, (total_provider_calls + budget - 1) // budget) if budget > 0 else 1
        normal_estimated_runs = (
            max(1, (normal_batch_provider_calls + budget - 1) // budget)
            if budget > 0 and normal_batch_provider_calls > 0
            else 0 if deferred_high_density_provider_calls and normal_batch_provider_calls == 0
            else 1
        )
        warnings = _formula_backfill_warnings(
            processed=processed,
            failed_papers=failed_papers,
            candidate_count=total_candidates,
            provider_calls=total_provider_calls,
            data_egress=has_external_egress,
            daily_call_budget=budget,
        )
        if not resume_after_found:
            warnings.append(
                f"resume_after item_key {resume_after!r} was not found in the matched backfill set."
            )
        if skipped_items:
            warnings.append(
                f"{len(skipped_items)} bilingual/translated PDF(s) skipped; "
                "formula backfill only processes original PDFs."
            )
        if unmatched_requested_item_keys:
            warnings.append(
                f"{len(unmatched_requested_item_keys)} requested item_key(s) were not matched "
                "to already-indexed ZotPilot papers with available original PDFs."
            )
        if high_call_papers:
            warnings.append(
                f"{len(high_call_papers)} paper(s) individually exceed the daily call budget; "
                "run them separately with a larger budget or rely on cached LaTeX first."
            )
        if dense_formula_papers:
            warnings.append(
                f"{len(dense_formula_papers)} high-density formula document(s) will be deferred "
                "by a normal formula batch unless they are run as a single item or with include_high_density."
            )
        if scan_limited_high_density_papers:
            warnings.append(
                f"{len(scan_limited_high_density_papers)} high-density document estimate(s) are incomplete "
                "because they stopped at a batch scan limit or yielded truncated candidates; run a single-item "
                "estimate with --pdf-fallback-max-pages 0 to generate a complete page-window plan."
            )
        if truncated_candidate_papers:
            warnings.append(
                f"{len(truncated_candidate_papers)} paper(s) have truncated PDF fallback candidates; "
                "rerun those papers with --pdf-fallback-max-pages 0 before writing formulas."
            )
        if cached_latex_missing_number_papers:
            warnings.append(
                f"{len(cached_latex_missing_number_papers)} paper(s) have cached LaTeX formulas with missing "
                "equation numbers; review numbering before writing or enable PDF number enrichment explicitly."
            )
        if candidate_quality_blocking_papers:
            warnings.append(
                f"{len(candidate_quality_blocking_papers)} paper(s) have candidate-stage formula quality "
                "warnings that should be reviewed before writing formulas."
            )
        next_action = _formula_backfill_next_action(
            processed=processed,
            failed_papers=failed_papers,
            candidate_count=total_candidates,
            data_egress=has_external_egress,
            daily_call_budget=budget,
        )
        if deferred_high_density_candidates:
            if normal_batch_provider_calls == 0:
                next_action = (
                    "Do not run a normal formula batch yet; every matched candidate set is high-density. "
                    "Review structured MinerU/PDF-Extract-Kit cache coverage, then backfill single items "
                    "or rerun with include_high_density intentionally."
                )
            else:
                next_action = (
                    "Run index_formulas for normal papers first; high-density documents will be deferred "
                    "and should be reviewed or backfilled separately."
                )
        if cached_latex_missing_number_papers:
            next_action = (
                "Review cached LaTeX equation numbering before writing formulas, or rerun the affected "
                "paper(s) with formula_candidate_cache_pdf_number_enrichment=true intentionally."
            )
        if candidate_quality_blocking_papers:
            next_action = (
                "Review candidate-stage formula quality warnings before writing formulas; do not run "
                "index_formulas until these papers are fixed or explicitly allowed."
            )
        summary = {
            "papers": processed,
            "selected": selected,
            "matched": matched_count,
            "skipped": len(skipped_items),
            "candidates": total_candidates,
            "candidate_provider": candidate_provider_name,
            "provider_calls": total_provider_calls,
            "external_calls": total_provider_calls if has_external_egress else 0,
            "normal_batch_candidates": normal_batch_candidates,
            "normal_batch_provider_calls": normal_batch_provider_calls,
            "normal_batch_external_calls": normal_batch_provider_calls if has_external_egress else 0,
            "normal_batch_estimated_min_duration": _format_estimated_duration(normal_estimated_min_duration_seconds),
            "normal_batch_estimated_runs": normal_estimated_runs,
            "deferred_high_density_candidates": deferred_high_density_candidates,
            "deferred_high_density_provider_calls": deferred_high_density_provider_calls,
            "average_candidates_per_paper": average_candidates_per_paper,
            "estimated_min_duration": _format_estimated_duration(estimated_min_duration_seconds),
            "data_egress": has_external_egress,
            "daily_call_budget": budget,
            "estimated_runs": estimated_runs,
            "high_call_paper_count": len(high_call_papers),
            "high_density_call_threshold": high_density_threshold,
            "high_density_candidate_threshold": high_density_candidate_threshold,
            "pdf_fallback_max_pages": effective_pdf_fallback_max_pages,
            "page_min": page_min or 0,
            "page_max": page_max or 0,
            "page_range_estimate": partial_page_estimate,
            "dense_formula_paper_count": len(dense_formula_papers),
            "high_density_backfill_plan_count": len(high_density_backfill_plans),
            "scan_limited_high_density_paper_count": len(scan_limited_high_density_papers),
            "truncated_candidate_paper_count": len(truncated_candidate_papers),
            "cached_latex_missing_number_paper_count": len(cached_latex_missing_number_papers),
            "candidate_quality_blocking_paper_count": len(candidate_quality_blocking_papers),
            "unmatched_requested_item_key_count": len(unmatched_requested_item_keys),
            "resume_after_found": resume_after_found,
            "sample_size": effective_sample_size,
            "sample_seed": effective_sample_seed,
            "sampled_from": sampled_from,
            "warnings": warnings,
            "next_action": next_action,
        }
        return {
            "provider": provider_name,
            "candidate_provider": candidate_provider_name,
            "processed": processed,
            "selected": selected,
            "matched": matched_count,
            "skipped": len(skipped_items),
            "failed_papers": failed_papers,
            "candidate_count": total_candidates,
            "average_candidates_per_paper": average_candidates_per_paper,
            "estimated_provider_calls": total_provider_calls,
            "estimated_external_calls": total_provider_calls if has_external_egress else 0,
            "estimated_min_duration_seconds": round(estimated_min_duration_seconds, 3),
            "estimated_min_duration": summary["estimated_min_duration"],
            "normal_batch_candidate_count": normal_batch_candidates,
            "normal_batch_estimated_provider_calls": normal_batch_provider_calls,
            "normal_batch_estimated_external_calls": normal_batch_provider_calls if has_external_egress else 0,
            "normal_batch_estimated_min_duration_seconds": round(normal_estimated_min_duration_seconds, 3),
            "normal_batch_estimated_min_duration": summary["normal_batch_estimated_min_duration"],
            "normal_batch_estimated_runs": normal_estimated_runs,
            "deferred_high_density_candidate_count": deferred_high_density_candidates,
            "deferred_high_density_provider_calls": deferred_high_density_provider_calls,
            "daily_call_budget": budget,
            "estimated_runs": estimated_runs,
            "high_density_call_threshold": high_density_threshold,
            "high_density_candidate_threshold": high_density_candidate_threshold,
            "pdf_fallback_max_pages": effective_pdf_fallback_max_pages,
            "page_min": page_min or 0,
            "page_max": page_max or 0,
            "page_range_estimate": partial_page_estimate,
            "high_call_papers": high_call_papers,
            "dense_formula_papers": dense_formula_papers,
            "high_density_backfill_plans": high_density_backfill_plans,
            "scan_limited_high_density_papers": scan_limited_high_density_papers,
            "truncated_candidate_papers": truncated_candidate_papers,
            "cached_latex_missing_number_papers": cached_latex_missing_number_papers,
            "candidate_quality_blocking_papers": candidate_quality_blocking_papers,
            "candidate_quality_blocking_paper_count": len(candidate_quality_blocking_papers),
            "unmatched_requested_item_keys": unmatched_requested_item_keys,
            "resume_after_found": resume_after_found,
            "sample_size": effective_sample_size,
            "sample_seed": effective_sample_seed,
            "sampled_from": sampled_from,
            "sampled_unresolved_key_count": sampled_unresolved_key_count,
            "data_egress": has_external_egress,
            "summary": summary,
            "results": results,
        }

    def _formula_backfill_selection(
        self,
        *,
        item_key: str | None = None,
        item_keys: list[str] | None = None,
        limit: int | None = None,
        resume_after: str | None = None,
    ) -> tuple[list[ZoteroItem], list[tuple[ZoteroItem, str]]]:
        items = self._formula_backfill_candidate_items(
            item_key=item_key,
            item_keys=item_keys,
            limit=limit,
            resume_after=resume_after,
        )
        return self._filter_formula_backfill_original_items(items)

    def _formula_backfill_candidate_items(
        self,
        *,
        item_key: str | None = None,
        item_keys: list[str] | None = None,
        limit: int | None = None,
        resume_after: str | None = None,
    ) -> list[ZoteroItem]:
        indexed_ids = self.store.get_indexed_doc_ids()
        if item_key or item_keys:
            requested_keys = [item_key] if item_key else list(dict.fromkeys(item_keys or []))
            items = []
            for requested_key in requested_keys:
                if not requested_key or requested_key not in indexed_ids:
                    continue
                item = self.zotero.get_item(requested_key)
                if item is not None and item.pdf_path and item.pdf_path.exists():
                    items.append(item)
        else:
            items = [
                item for item in self.zotero.get_all_items_with_pdfs()
                if item.item_key in indexed_ids and item.pdf_path and item.pdf_path.exists()
            ]
        items.sort(key=lambda item: item.item_key)
        if resume_after:
            skipped = True
            resumed_items = []
            for item in items:
                if skipped:
                    skipped = item.item_key != resume_after
                    continue
                resumed_items.append(item)
            items = resumed_items
        if limit:
            items = items[:limit]
        return items

    def _formula_backfill_items_for_indexed_keys(self, item_keys: list[str]) -> list[ZoteroItem]:
        items: list[ZoteroItem] = []
        for requested_key in dict.fromkeys(item_keys):
            if not requested_key:
                continue
            item = self.zotero.get_item(requested_key)
            if item is not None and item.pdf_path and item.pdf_path.exists():
                items.append(item)
        return items

    def _filter_formula_backfill_original_items(
        self,
        items: list[ZoteroItem],
    ) -> tuple[list[ZoteroItem], list[tuple[ZoteroItem, str]]]:
        selected: list[ZoteroItem] = []
        skipped_items: list[tuple[ZoteroItem, str]] = []
        for item in items:
            original_pdf_path = None
            resolver = getattr(self.zotero, "resolve_original_pdf_path", None)
            if callable(resolver):
                original_pdf_path = resolver(
                    item.item_key,
                    title=item.title,
                    fallback_path=item.pdf_path,
                )
            if isinstance(original_pdf_path, Path) and original_pdf_path != item.pdf_path:
                item.pdf_path = original_pdf_path
            if is_likely_bilingual_or_translated_pdf(item.pdf_path):
                skipped_items.append((item, "bilingual_or_translated_pdf"))
            elif pdf_content_translation_risk_score(item.pdf_path, title=item.title) >= 20.0:
                skipped_items.append((item, "bilingual_or_translated_pdf_content"))
            else:
                selected.append(item)
        return selected, skipped_items

    def _formula_backfill_items(
        self,
        *,
        item_key: str | None = None,
        item_keys: list[str] | None = None,
        limit: int | None = None,
        resume_after: str | None = None,
    ) -> list[ZoteroItem]:
        items, _skipped = self._formula_backfill_selection(
            item_key=item_key,
            item_keys=item_keys,
            limit=limit,
            resume_after=resume_after,
        )
        return items

    @staticmethod
    def _formula_review_rows(
        *,
        item: ZoteroItem,
        formulas: list,
        threshold: float,
    ) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        reasons_by_index: list[list[str]] = [[] for _formula in formulas]
        for index, formula in enumerate(formulas):
            confidence = getattr(formula, "confidence", None)
            if threshold > 0 and confidence is not None and confidence < threshold:
                reasons_by_index[index].append("low_confidence")
            equation_number_status = getattr(formula, "equation_number_status", "")
            if not getattr(formula, "equation_number", "") and equation_number_status != "unnumbered":
                reasons_by_index[index].append("missing_equation_number")
            if getattr(formula, "source", "") == "pdf_text_equation_number_truncated":
                reasons_by_index[index].append("fallback_truncated")
            if _looks_like_split_formula_latex(getattr(formula, "latex", "")):
                reasons_by_index[index].append("possible_split_formula")
        _append_formula_numbering_review_reasons(formulas, reasons_by_index)
        for formula, reasons in zip(formulas, reasons_by_index):
            if not reasons:
                continue
            rows.append({
                "item_key": item.item_key,
                "title": item.title,
                "page_num": formula.page_num,
                "formula_index": formula.formula_index,
                "equation_number": formula.equation_number,
                "confidence": getattr(formula, "confidence", None),
                "provider": formula.provider,
                "source": getattr(formula, "source", ""),
                "review_reasons": reasons,
                "latex": formula.latex,
            })
        return rows

    @staticmethod
    def _formula_ocr_failure_review_rows(
        *,
        item: ZoteroItem,
        candidates: list,
        formulas: list,
        formula_indices: list[int] | None = None,
    ) -> list[dict[str, object]]:
        recognized_numbers = {
            getattr(formula, "equation_number", "")
            for formula in formulas
            if getattr(formula, "equation_number", "")
        }
        rows: list[dict[str, object]] = []
        stable_formula_indices = (
            formula_indices
            if formula_indices is not None and len(formula_indices) == len(candidates)
            else None
        )
        for index, candidate in enumerate(candidates):
            equation_number = _formula_candidate_effective_equation_number(candidate)
            if not equation_number or equation_number in recognized_numbers:
                continue
            formula_index = (
                max(int(stable_formula_indices[index]), 0)
                if stable_formula_indices is not None
                else index
            )
            review_reason = "cached_latex_rejected" if getattr(candidate, "latex", "") else "ocr_failed"
            rows.append({
                "item_key": item.item_key,
                "title": item.title,
                "page_num": getattr(candidate, "page_num", None),
                "formula_index": formula_index,
                "equation_number": equation_number,
                "confidence": getattr(candidate, "confidence", None),
                "provider": getattr(candidate, "source", ""),
                "review_reasons": [review_reason],
                "latex": getattr(candidate, "latex", ""),
                "bbox": getattr(candidate, "bbox", None),
                "bbox_coordinate_space": getattr(candidate, "bbox_coordinate_space", ""),
            })
        return rows

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
                review_threshold = float(getattr(self.config, "formula_ocr_low_confidence_threshold", 0.0) or 0.0)
                review_rows = self._formula_review_rows(
                    item=item,
                    formulas=formulas,
                    threshold=review_threshold,
                )
                blocking_review_reasons = _structural_formula_review_reasons(review_rows)
                if blocking_review_reasons:
                    logger.warning(
                        "Formula indexing for %s needs structural review (%s); skipping formula storage",
                        item_key,
                        ", ".join(blocking_review_reasons),
                    )
                else:
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

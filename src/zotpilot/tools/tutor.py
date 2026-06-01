"""MCP tools for the /ztp-tutor feature (Phase 2).

Two tools:
  - get_paper_for_tutor: resolves a paper by title or item_key and returns
    page-delimited extraction, figures/tables, persona, and existing annotations.
  - annotate_pdf: writes the 5-dim color highlight + per-sentence comment +
    page-1 overview into the Zotero storage PDF via annotator.annotate_pdf_file.

Deterministic logic stays in `..pdf.annotator`; this module is a thin MCP wrapper.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

import pymupdf
from pydantic import Field

from ..pdf.annotator import (
    AnnotationSpec,
    ScannedPdfError,
    annotate_pdf_file,
    has_text_layer,
    read_existing_annotations,
)
from ..state import ToolError, _get_zotero, mcp
from .profiles import tool_tags

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ITEM_KEY_RE = re.compile(r"^[A-Z0-9]{8,}$")

_PERSONA_HEADINGS = ("## 阅读画像", "## Reading Persona")
# Heading written by save_reading_persona; starts with _PERSONA_HEADINGS[0] so
# _read_persona() detects it on the next run.
_PERSONA_CANONICAL_HEADING = "## 阅读画像 (Reading Persona)"

_TRIMMED_SECTION_LABELS = frozenset({"references", "appendix"})


def _coerce_list(value: Any) -> list:
    """Coerce a value to list, parsing JSON string if needed."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
    return []


def _coerce_dict(value: Any) -> dict:
    """Coerce a value to dict, parsing JSON string if needed."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {}


def _looks_like_item_key(s: str) -> bool:
    return bool(_ITEM_KEY_RE.match(s.strip()))


def _read_persona() -> str | None:
    """Return the raw `## 阅读画像` / `## Reading Persona` section from
    ~/.config/zotpilot/ZOTPILOT.md (heading through next `## ` or EOF), or None."""
    try:
        path = Path("~/.config/zotpilot/ZOTPILOT.md").expanduser()
        if not path.exists():
            return None
        content = path.read_text(encoding="utf-8")
    except Exception:
        return None
    lines = content.splitlines()
    start_idx: int | None = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        for heading in _PERSONA_HEADINGS:
            if stripped.startswith(heading):
                start_idx = i
                break
        if start_idx is not None:
            break
    if start_idx is None:
        return None
    end_idx = len(lines)
    for j in range(start_idx + 1, len(lines)):
        if lines[j].lstrip().startswith("## "):
            end_idx = j
            break
    section = "\n".join(lines[start_idx:end_idx]).strip()
    return section or None


def _upsert_persona_section(existing: str, section: str) -> str:
    """Return `existing` with the persona section replaced (if a heading from
    _PERSONA_HEADINGS is present) or appended. Other content is preserved."""
    lines = existing.splitlines()
    start_idx: int | None = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if any(stripped.startswith(h) for h in _PERSONA_HEADINGS):
            start_idx = i
            break
    section_block = section.rstrip("\n")
    if start_idx is None:
        prefix = existing.rstrip("\n")
        return (f"{prefix}\n\n{section_block}\n" if prefix else f"{section_block}\n")
    end_idx = len(lines)
    for j in range(start_idx + 1, len(lines)):
        if lines[j].lstrip().startswith("## "):
            end_idx = j
            break
    new_lines = lines[:start_idx] + section_block.split("\n") + lines[end_idx:]
    return "\n".join(new_lines).rstrip("\n") + "\n"


def _trim_sections(full_md: str, sections, labels: frozenset) -> str:
    """Concatenate full_markdown EXCLUDING SectionSpan ranges in `labels`."""
    if not full_md:
        return ""
    spans = sorted(
        [(int(s.char_start), int(s.char_end)) for s in (sections or [])
         if getattr(s, "label", None) in labels],
        key=lambda t: t[0],
    )
    if not spans:
        return full_md
    parts: list[str] = []
    cursor = 0
    for start, end in spans:
        if start > cursor:
            parts.append(full_md[cursor:start])
        cursor = max(cursor, end)
    if cursor < len(full_md):
        parts.append(full_md[cursor:])
    return "".join(parts)


def _existing_annotations_for_path(pdf_path: Path) -> list[dict]:
    """Open PDF read-only and return existing (foreign) annotations as dicts."""
    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception:
        return []
    try:
        existing = read_existing_annotations(doc)
    finally:
        doc.close()
    out: list[dict] = []
    for e in existing:
        d = asdict(e)
        # ensure JSON-serializable rect/color
        if d.get("rect") is not None:
            d["rect"] = list(d["rect"])
        if d.get("color") is not None:
            d["color"] = list(d["color"])
        out.append(d)
    return out


# Short function words dropped before token-matching a title query. These carry
# no discriminating power, so including them would inflate overlap scores and
# pull in unrelated papers.
_RESOLVE_STOPWORDS = frozenset({
    "the", "a", "an", "of", "for", "and", "or", "to", "in", "on", "with",
    "based", "using", "via", "model", "models", "method", "methods",
    "approach", "approaches", "study", "novel", "toward", "towards", "from",
    "into", "by", "is", "are", "we", "our", "new", "this", "that",
})


def _tokenize_title(s: str) -> list[str]:
    """Split a title query into distinct discriminating tokens.

    Lowercases, splits on any non-alphanumeric run, drops tokens shorter than
    3 chars and common stopwords. Order-preserving, de-duplicated.
    """
    seen: set[str] = set()
    tokens: list[str] = []
    for raw in re.split(r"[^0-9a-z]+", s.lower()):
        if len(raw) >= 3 and raw not in _RESOLVE_STOPWORDS and raw not in seen:
            seen.add(raw)
            tokens.append(raw)
    return tokens


def _title_overlap(candidate_title: str, query_tokens: list[str]) -> int:
    ct = (candidate_title or "").lower()
    return sum(1 for t in query_tokens if t in ct)


def _title_search(zc, conditions: list[dict], match: str, limit: int) -> list:
    try:
        results = zc.advanced_search(
            conditions=conditions,
            match=match,
            sort_by=None,
            sort_dir="desc",
            limit=limit,
        )
    except Exception as e:
        raise ToolError(f"advanced_search failed: {e}") from e
    return list(results or [])


def _resolve_to_item(zc, candidate: Any):
    """Map a search-result candidate to a resolved ZoteroItem, or None."""
    item_key = _maybe_get(candidate, "item_key") or _maybe_get(candidate, "doc_id")
    if not item_key:
        return None
    return zc.get_item(item_key)


def _resolve_item(title_or_doc_id: str):
    """Resolve a Zotero item by item_key (exact) or title (multi-strategy fuzzy).

    Strategy 0: if the input looks like an item key, try an exact get_item.
    Strategy 1: strict full-title `contains` (high precision).
    Strategy 2 (only when Strategy 1 misses): tokenize the title and run a
      token-OR advanced_search, then rank candidates by distinct-token overlap.
      A clearly dominant winner (overlap >=3 and >= 2x the runner-up) is
      auto-selected; otherwise the strong candidates are returned for
      disambiguation. This bridges trivial title differences (punctuation,
      abbreviations like "PIV" vs "particle image velocimetry") that a strict
      substring match would miss.

    Returns (item, candidates):
      (item, [item])      -> single resolved match
      (None, candidates)  -> ambiguous; caller emits needs_disambiguation
      (None, [])          -> no match; caller raises a no-match ToolError
    """
    zc = _get_zotero()
    key = title_or_doc_id.strip()
    if _looks_like_item_key(key):
        item = zc.get_item(key)
        if item is not None:
            return item, [item]

    # Strategy 1: strict full-title contains.
    candidates: list = _title_search(
        zc, [{"field": "title", "op": "contains", "value": key}], "all", 10
    )

    # Strategy 2: tokenized OR search, ranked by token overlap.
    if not candidates:
        tokens = _tokenize_title(key)
        if tokens:
            raw = _title_search(
                zc,
                [{"field": "title", "op": "contains", "value": t} for t in tokens],
                "any",
                25,
            )
            scored = sorted(
                ((_title_overlap(_maybe_get(c, "title") or "", tokens), c) for c in raw),
                key=lambda pair: pair[0],
                reverse=True,
            )
            if scored:
                best_overlap = scored[0][0]
                runner_up = scored[1][0] if len(scored) > 1 else 0
                # The 2x ratio is conservative by design: prefer disambiguation
                # over a silent mis-selection when two plausible titles compete.
                if best_overlap >= 3 and best_overlap >= 2 * max(runner_up, 1):
                    candidates = [scored[0][1]]
                else:
                    strong = [c for overlap, c in scored if overlap >= 2]
                    candidates = strong or [c for _, c in scored]

    if not candidates:
        return None, []
    if len(candidates) == 1:
        item = _resolve_to_item(zc, candidates[0])
        if item is None:
            return None, candidates
        return item, candidates
    return None, candidates


def _maybe_get(obj: Any, attr: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(attr)
    return getattr(obj, attr, None)


def _candidate_summary(c: Any) -> dict:
    return {
        "doc_id": _maybe_get(c, "item_key") or _maybe_get(c, "doc_id") or "",
        "title": _maybe_get(c, "title") or "",
        "authors": _maybe_get(c, "authors") or "",
        "year": _maybe_get(c, "year"),
    }


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


@mcp.tool(tags=tool_tags("extended", "write"))
def save_reading_persona(
    persona_text: Annotated[
        str,
        Field(description=(
            "Reading-persona body to persist under the '## 阅读画像 (Reading Persona)' "
            "section of ~/.config/zotpilot/ZOTPILOT.md (the heading is added "
            "automatically). Typically the four hints: 英文水平 / 领域熟悉度 / 导读深度 / "
            "风格偏好. Call this once after the user states preferences so future "
            "/ztp-tutor runs don't re-ask. Replaces an existing section if present."
        )),
    ],
) -> dict:
    """Persist the reading persona to ZOTPILOT.md so it is auto-detected next run."""
    body = (persona_text or "").strip()
    if not body:
        raise ToolError("persona_text is required")
    path = Path("~/.config/zotpilot/ZOTPILOT.md").expanduser()
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
    except Exception as e:
        raise ToolError(f"cannot read {path}: {e}") from e
    replaced = any(h in existing for h in _PERSONA_HEADINGS)
    section = f"{_PERSONA_CANONICAL_HEADING}\n\n{body}"
    new_content = _upsert_persona_section(existing, section)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_content, encoding="utf-8")
    except Exception as e:
        raise ToolError(f"cannot write {path}: {e}") from e
    return {
        "saved": True,
        "path": str(path),
        "action": "replaced" if replaced else "created",
    }


@mcp.tool(tags=tool_tags("extended", "read"))
def get_paper_for_tutor(
    title_or_doc_id: Annotated[
        str,
        Field(description="Fuzzy paper title or exact doc_id/item_key"),
    ],
) -> dict:
    """Resolve a paper for /ztp-tutor and return its extraction payload.

    On a single match returns the full extraction payload (page_texts,
    sectioned_text, figures, tables, tables_on_page, persona,
    existing_annotations). On multiple matches returns
    {needs_disambiguation: True, candidates:[...]}. Raises ToolError on zero
    matches or when the PDF has no text layer.
    """
    if not title_or_doc_id or not title_or_doc_id.strip():
        raise ToolError("title_or_doc_id is required")

    item, candidates = _resolve_item(title_or_doc_id)
    if not item and not candidates:
        raise ToolError(f"no Zotero item matches {title_or_doc_id!r}")
    if item is None and len(candidates) > 1:
        return {
            "needs_disambiguation": True,
            "candidates": [_candidate_summary(c) for c in candidates[:10]],
        }
    if item is None:
        raise ToolError(f"could not resolve item from {title_or_doc_id!r}")

    pdf_path = getattr(item, "pdf_path", None)
    if pdf_path is None:
        raise ToolError(
            f"item {getattr(item, 'item_key', '?')} has no attached PDF"
        )
    pdf_path = Path(pdf_path)
    if not has_text_layer(pdf_path):
        raise ToolError(
            f"PDF has no text layer (scanned?); OCR required: {pdf_path}"
        )

    # Imported lazily to keep tutor's own module load decoupled from the heavy
    # extraction stack (pymupdf4llm + layout + OCR) and narrow its import-failure
    # surface. (extract_document is a core dep and already re-exported by
    # zotpilot.pdf.__init__, so this is hygiene, not a startup-cost guarantee.)
    from ..pdf.extractor import extract_document

    try:
        extraction = extract_document(pdf_path)
    except Exception as e:
        raise ToolError(f"extract_document failed for {pdf_path}: {e}") from e

    page_texts = [
        {"page_num": p.page_num, "text": p.markdown} for p in extraction.pages
    ]
    sectioned_text = _trim_sections(
        extraction.full_markdown, extraction.sections, _TRIMMED_SECTION_LABELS
    )
    figures = [
        {
            "page_num": f.page_num,
            "figure_index": f.figure_index,
            "bbox": list(f.bbox),
            "caption": f.caption,
        }
        for f in extraction.figures
    ]
    tables = [
        {
            "page_num": t.page_num,
            "table_index": t.table_index,
            "bbox": list(t.bbox),
            "caption": t.caption,
        }
        for t in extraction.tables
    ]
    tables_on_page = {
        p.page_num: p.tables_on_page for p in extraction.pages if p.tables_on_page > 0
    }
    persona = _read_persona()
    existing_annotations = _existing_annotations_for_path(pdf_path)

    return {
        "doc_id": getattr(item, "item_key", ""),
        "pdf_path": str(pdf_path),
        "page_texts": page_texts,
        "sectioned_text": sectioned_text,
        "figures": figures,
        "tables": tables,
        "tables_on_page": tables_on_page,
        "persona": persona,
        "existing_annotations": existing_annotations,
    }


def _spec_from_dict(i: int, raw: dict) -> AnnotationSpec:
    if not isinstance(raw, dict):
        raise ToolError(f"annotations[{i}] must be a dict")
    try:
        quote = str(raw["quote"])
        dimension = str(raw["dimension"])
        comment = str(raw["comment"])
    except KeyError as e:
        raise ToolError(
            f"annotations[{i}] missing required field {e!s}"
        ) from None
    kind = str(raw.get("kind", "highlight"))
    page_hint = raw.get("page_hint")
    page = raw.get("page")
    bbox_raw = raw.get("bbox")
    bbox: tuple[float, float, float, float] | None = None
    if bbox_raw is not None:
        try:
            bb = list(bbox_raw)
            if len(bb) != 4:
                raise ValueError("bbox must have 4 elements")
            bbox = (float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3]))
        except Exception as e:
            raise ToolError(f"annotations[{i}].bbox invalid: {e}") from e
    subtype = raw.get("subtype")
    return AnnotationSpec(
        quote=quote,
        dimension=dimension,
        comment=comment,
        page_hint=int(page_hint) if page_hint is not None else None,
        kind=kind,  # type: ignore[arg-type]
        page=int(page) if page is not None else None,
        bbox=bbox,
        subtype=str(subtype) if subtype is not None else None,
    )


def _resolve_specs_input(
    specs_path: str | None,
    annotations: list[dict] | None,
    overview: dict | None,
) -> tuple[Any, Any]:
    """Return (annotations, overview), loading them from a JSON file when
    specs_path is given. The file form keeps the bulky payload out of the
    tool-call arguments so the approval prompt stays compact."""
    if not specs_path or not str(specs_path).strip():
        return annotations, overview
    path = Path(str(specs_path)).expanduser()
    if not path.exists():
        raise ToolError(f"specs_path does not exist: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise ToolError(f"specs_path is not valid JSON: {e}") from e
    if not isinstance(payload, dict):
        raise ToolError("specs_path JSON must be an object {annotations, overview}")
    return payload.get("annotations"), payload.get("overview")


def _report_to_dict(report) -> dict:
    return {
        "placed": [
            {
                "quote": s.quote,
                "dimension": s.dimension,
                "comment": s.comment,
                "kind": s.kind,
                "subtype": s.subtype,
                "page": s.page,
                "page_hint": s.page_hint,
            }
            for s in report.placed
        ],
        "unplaced": [
            {"label": label, "reason": reason}
            for label, reason in report.unplaced
        ],
        "overview_placed": report.overview_placed,
        "backup_path": report.backup_path,
        "page_count": report.page_count,
        "file_size_before": report.file_size_before,
        "file_size_after": report.file_size_after,
        "verified": report.verified,
        "verification_details": report.verification_details,
        "coverage": report.coverage,
    }


@mcp.tool(tags=tool_tags("extended", "write"))
def annotate_pdf(
    doc_id: Annotated[
        str,
        Field(description="Zotero item_key of the paper to annotate"),
    ],
    specs_path: Annotated[
        str | None,
        Field(description=(
            "PREFERRED: path to a JSON file {annotations:[...], overview:{...}}. "
            "Lets the caller keep the bulky annotation payload out of the tool-call "
            "arguments (cleaner approval prompt). When set, annotations/overview "
            "args are ignored."
        )),
    ] = None,
    annotations: Annotated[
        list[dict] | None,
        Field(description=(
            "Inline alternative to specs_path. "
            "[{quote, dimension, comment, page_hint?, kind?, page?, bbox?, subtype?}]. "
            "Caps: comment<=500B, quote<=1000B, max 200 annotations. "
            "page_hint is 1-based; kind='region' requires page and bbox."
        )),
    ] = None,
    overview: Annotated[
        dict | None,
        Field(description=(
            "Inline alternative to specs_path. "
            "{thesis, skeleton:{question,claim,evidence,rebuttal,conclusion}, "
            "strongest, weakest}. Max 2000 bytes total."
        )),
    ] = None,
) -> dict:
    """Write the 5-dim reading guide into the Zotero-stored PDF (in place).

    Backup -> work-copy -> distinct full save -> verify -> atomic swap.
    Clears prior ZotPilot annotations on re-run. Foreign annotations are never
    touched.

    Provide the annotation payload EITHER via specs_path (a JSON file, preferred
    — keeps the approval prompt small) OR inline via annotations + overview.
    """
    if not doc_id or not str(doc_id).strip():
        raise ToolError("doc_id is required")

    item = _get_zotero().get_item(doc_id)
    if item is None:
        raise ToolError(f"no Zotero item with key {doc_id!r}")
    pdf_path = getattr(item, "pdf_path", None)
    if pdf_path is None:
        raise ToolError(f"item {doc_id!r} has no attached PDF")
    pdf_path = Path(pdf_path)

    annotations, overview = _resolve_specs_input(specs_path, annotations, overview)

    raw_list = _coerce_list(annotations)
    if not raw_list:
        raise ToolError(
            "annotations must be a non-empty list (provide specs_path or inline annotations)"
        )
    overview_dict = _coerce_dict(overview)

    specs: list[AnnotationSpec] = [
        _spec_from_dict(i, raw) for i, raw in enumerate(raw_list)
    ]

    try:
        report = annotate_pdf_file(pdf_path, specs, overview_dict)
    except ScannedPdfError as e:
        raise ToolError(str(e)) from e

    result = _report_to_dict(report)
    placed_n = len(report.placed)
    unplaced_n = len(report.unplaced)
    total = placed_n + unplaced_n
    result["summary"] = (
        f"导读完成：{placed_n}/{total} 已放置，{unplaced_n} 处未定位；"
        f"备份 {report.backup_path}"
    )
    return result

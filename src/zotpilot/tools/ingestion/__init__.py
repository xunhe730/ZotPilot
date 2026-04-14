"""Ingestion MCP tools: search_academic_databases + ingest_by_identifiers."""
from __future__ import annotations

import json
import logging
import re
import threading
from typing import Annotated, Any, Literal

import httpx
from pydantic import BeforeValidator, Field

from ...bridge import DEFAULT_PORT, BridgeServer
from ...state import (
    ToolError,
    _get_config,
    _get_writer,
    _get_zotero,
    mcp,
    register_reset_callback,
)
from ..profiles import tool_tags
from . import connector, search
from .models import IngestCandidate

logger = logging.getLogger(__name__)
_writer_lock = threading.Lock()


def _parse_json_string_list(value: Any) -> Any:
    """Accept list params even when an MCP client wraps them as JSON strings.

    Some MCP client wrappers (e.g. Qwen-based 'Sisyphus' runtimes, older
    Claude Code builds) serialize list[T] params as JSON strings instead
    of real arrays before they reach the server. Decode transparently so
    Pydantic can validate the inner structure.

    Passes through lists, None, and other non-string values unchanged.
    On malformed JSON, returns the original value so Pydantic surfaces
    a type error the caller can act on instead of silently swallowing.
    """
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return value
        if isinstance(parsed, list):
            return parsed
    return value

# ---------------------------------------------------------------------------
# INBOX collection cache
# ---------------------------------------------------------------------------
_inbox_collection_key: str | None = None
_inbox_lock = threading.Lock()
_INBOX_COLLECTION_NAME = "INBOX"


def _clear_inbox_cache() -> None:
    global _inbox_collection_key
    _inbox_collection_key = None
    import sys as _sys
    _pkg = _sys.modules.get("zotpilot.tools.ingestion")
    if _pkg is not None and hasattr(_pkg, "_inbox_collection_key"):
        _pkg._inbox_collection_key = None  # type: ignore[attr-defined]


register_reset_callback(_clear_inbox_cache)


def _ensure_inbox_collection() -> str | None:
    """Return the INBOX collection key, creating it if absent when possible."""
    global _inbox_collection_key
    if _inbox_collection_key is not None:
        return _inbox_collection_key
    with _inbox_lock:
        if _inbox_collection_key is not None:
            return _inbox_collection_key
        try:
            writer = _get_writer()
        except Exception:
            return None
        if not _get_config().zotero_api_key:
            return None
        try:
            with _writer_lock:
                collections = writer._zot.collections()
            for coll in collections:
                data = coll.get("data", {})
                if data.get("name") == _INBOX_COLLECTION_NAME:
                    _inbox_collection_key = data.get("key") or coll.get("key")
                    return _inbox_collection_key
            with _writer_lock:
                response = writer._zot.create_collections([{"name": _INBOX_COLLECTION_NAME}])
            if response and "successful" in response:
                for value in response["successful"].values():
                    _inbox_collection_key = value.get("key") or value.get("data", {}).get("key")
                    if _inbox_collection_key:
                        return _inbox_collection_key
            with _writer_lock:
                collections = writer._zot.collections()
            for coll in collections:
                data = coll.get("data", {})
                if data.get("name") == _INBOX_COLLECTION_NAME:
                    _inbox_collection_key = data.get("key") or coll.get("key")
                    return _inbox_collection_key
        except Exception as exc:
            logger.warning("_ensure_inbox_collection failed: %s", exc)
    return None


def _lookup_local_item_key_by_doi(doi: str | None) -> str | None:
    """Check if a DOI already exists in the local Zotero library."""
    if not doi:
        return None
    try:
        zotero = _get_zotero()
        return zotero.get_item_key_by_doi(doi)
    except Exception:
        return None


def _lookup_local_item_key_by_arxiv_extra(arxiv_id: str | None) -> str | None:
    """Check whether an arXiv ID appears in a local Zotero item's extra field."""
    if not arxiv_id:
        return None
    try:
        zotero = _get_zotero()
        return zotero.get_item_key_by_arxiv_id(arxiv_id)
    except Exception:
        return None


def _normalize_arxiv_id(arxiv_id: str | None) -> str | None:
    """Normalize an arXiv ID by trimming prefixes and version suffixes."""
    if not arxiv_id:
        return None
    cleaned = arxiv_id.strip()
    if cleaned.lower().startswith("arxiv:"):
        cleaned = cleaned.split(":", 1)[1].strip()
    cleaned = re.sub(r"v\d+$", "", cleaned, flags=re.IGNORECASE)
    return cleaned or None


def _arxiv_doi(arxiv_id: str) -> str:
    return f"10.48550/arxiv.{arxiv_id}"


def _doi_to_landing_url(doi: str) -> str:
    return f"https://doi.org/{doi}"


def _candidates_to_internal(input_candidates: list[IngestCandidate]) -> list[dict]:
    """Convert structured candidates into the internal ingestion representation."""
    internal: list[dict] = []
    for index, candidate in enumerate(input_candidates):
        source_doi = search.normalize_doi(candidate.doi)
        arxiv_id = _normalize_arxiv_id(candidate.arxiv_id)
        entry: dict = {
            "identifier": source_doi or arxiv_id or candidate.landing_page_url or "",
            "title": candidate.title,
            "arxiv_id": arxiv_id,
            "source_doi": source_doi,
            "_index": index,
            "url": None,
            "doi": None,
        }

        doi_is_arxiv = bool(source_doi and source_doi.startswith("10.48550/arxiv."))
        if candidate.is_oa_published and source_doi and not doi_is_arxiv:
            entry["doi"] = source_doi
            entry["url"] = _doi_to_landing_url(source_doi)
        elif arxiv_id:
            entry["doi"] = _arxiv_doi(arxiv_id)
            entry["url"] = f"https://arxiv.org/abs/{arxiv_id}"
        elif source_doi:
            entry["doi"] = source_doi
            entry["url"] = _doi_to_landing_url(source_doi)
        elif candidate.landing_page_url:
            entry["url"] = candidate.landing_page_url
        else:
            entry["status"] = "failed"
            entry["error"] = "no_usable_identifier"

        internal.append(entry)
    return internal


def _identifiers_to_internal(identifiers: list[str]) -> list[dict]:
    """Convert deprecated identifier strings into the internal ingestion representation."""
    internal: list[dict] = []
    for index, raw_identifier in enumerate(identifiers):
        identifier = raw_identifier.strip()
        arxiv_id: str | None = None
        source_doi: str | None = None
        candidate: dict = {
            "identifier": identifier,
            "title": None,
            "url": None,
            "doi": None,
            "arxiv_id": None,
            "source_doi": None,
            "_index": index,
        }

        source_doi = search.normalize_doi(identifier)
        if source_doi:
            candidate["source_doi"] = source_doi
            candidate["doi"] = source_doi
            candidate["url"] = connector.resolve_doi_to_landing_url(source_doi)
            if source_doi.startswith("10.48550/arxiv."):
                arxiv_id = _normalize_arxiv_id(source_doi[len("10.48550/arxiv."):])
                candidate["arxiv_id"] = arxiv_id
        elif identifier.lower().startswith("arxiv:") or _looks_like_arxiv_id(identifier):
            arxiv_id = _normalize_arxiv_id(identifier)
            candidate["arxiv_id"] = arxiv_id
            if arxiv_id:
                candidate["source_doi"] = _arxiv_doi(arxiv_id)
                candidate["doi"] = _arxiv_doi(arxiv_id)
                candidate["url"] = f"https://arxiv.org/abs/{arxiv_id}"
        elif identifier.startswith(("http://", "https://")):
            candidate["url"] = identifier
            doi_from_url = search.is_doi_query(identifier)
            if doi_from_url:
                source_doi = search.normalize_doi(doi_from_url)
                candidate["source_doi"] = source_doi
                candidate["doi"] = source_doi
                if source_doi and source_doi.startswith("10.48550/arxiv."):
                    candidate["arxiv_id"] = _normalize_arxiv_id(
                        source_doi[len("10.48550/arxiv."):]
                    )
        else:
            candidate["status"] = "failed"
            candidate["error"] = "unrecognized_identifier"

        internal.append(candidate)
    return internal


def _result_from_candidate(
    candidate: dict,
    *,
    status: str | None = None,
    error: str | None = None,
    item_key: str | None = None,
    has_pdf: bool = False,
    title: str | None = None,
    action_required=None,
    warning: str | None = None,
    **extra,
) -> dict:
    """Format a public result row from an internal candidate + save result fields."""
    return {
        "identifier": candidate.get("identifier", ""),
        "candidate_index": candidate.get("_index"),
        "status": status or candidate.get("status", "failed"),
        "item_key": item_key if item_key is not None else candidate.get("item_key"),
        "has_pdf": has_pdf,
        "title": title if title is not None else candidate.get("title", "") or "",
        "error": error if error is not None else candidate.get("error"),
        "action_required": action_required,
        "warning": warning,
        **extra,
    }


# ---------------------------------------------------------------------------
# MCP Tool: search_academic_databases
# ---------------------------------------------------------------------------

@mcp.tool(tags=tool_tags("core", "ingestion"))
def search_academic_databases(
    query: Annotated[
        str,
        Field(description=(
            "Search query. MUST be structured (not fuzzy bag-of-words) UNLESS "
            "you also pass concepts/venue/institutions. Allowed forms: DOI "
            "('10.xxx/yyy'), author-anchored ('author:Radford CLIP'), "
            "quoted phrase ('\"visual instruction tuning\"'), boolean "
            "('\"LLaVA\" OR \"Flamingo\"'). Pass '' when filtering purely by "
            "concept/venue."
        )),
    ],
    limit: Annotated[int, Field(ge=1, le=100, description="Max results per page")] = 20,
    year_min: Annotated[int | None, Field(description="Minimum publication year")] = None,
    year_max: Annotated[int | None, Field(description="Maximum publication year")] = None,
    min_citations: Annotated[
        int | None,
        Field(description="Minimum citation count (use to cut long tail)"),
    ] = None,
    sort_by: Annotated[
        Literal["relevance", "citations", "date"],
        Field(description="Sort order"),
    ] = "relevance",
    oa_only: Annotated[
        bool,
        Field(description="Restrict to open access papers"),
    ] = False,
    concepts: Annotated[
        list[str] | None,
        BeforeValidator(_parse_json_string_list),
        Field(description=(
            "Concept/topic names (human-readable, resolved server-side). "
            "Examples: ['Computer vision', 'Natural language processing']. "
            "Use to anchor topic searches and escape bag-of-words rejection."
        )),
    ] = None,
    venue: Annotated[
        str | None,
        Field(description=(
            "Venue/journal/conference name (resolved server-side). "
            "Examples: 'CVPR', 'NeurIPS', 'IEEE TPAMI', 'ICLR'."
        )),
    ] = None,
    institutions: Annotated[
        list[str] | None,
        BeforeValidator(_parse_json_string_list),
        Field(description=(
            "Institution names (resolved server-side). "
            "Examples: ['Stanford University', 'MIT', 'Google DeepMind']."
        )),
    ] = None,
    cursor: Annotated[
        str | None,
        Field(description="Pagination cursor from previous call's next_cursor"),
    ] = None,
) -> dict:
    """Search OpenAlex with full filter suite (concepts/venue/institutions).

    Returns: {"results": [...], "next_cursor": str|None, "total_count": int,
    "unresolved_filters": [...]}. Fuzzy queries are rejected unless a
    structured filter narrows the space. Use concepts+venue+year_min for
    precise topic discovery; use DOI or quoted phrase for known papers.
    """
    # MCP client compatibility: some clients wrap list[str] params as JSON
    # strings. Coerce defensively — mirrors the same pattern used by
    # ingest_by_identifiers. See _parse_json_string_list for rationale.
    concepts = _parse_json_string_list(concepts)
    institutions = _parse_json_string_list(institutions)
    if isinstance(concepts, str):
        raise ToolError(
            "`concepts` must be a list of concept names, e.g. "
            '["Computer vision", "Natural language processing"].'
        )
    if isinstance(institutions, str):
        raise ToolError(
            "`institutions` must be a list of institution names, e.g. "
            '["Stanford University", "Google DeepMind"].'
        )

    config = _get_config()
    sort_map = {"relevance": "relevance", "citations": "citationCount", "date": "publicationDate"}
    return search.search_academic_databases_impl(
        config, query, limit=limit,
        year_min=year_min, year_max=year_max,
        sort_by=sort_map.get(sort_by, "relevance"),
        httpx_module=httpx, tool_error_cls=ToolError, logger=logger,
        min_citations=min_citations,
        oa_only=oa_only,
        concepts=concepts,
        institutions=institutions,
        venue=venue,
        cursor=cursor,
        lookup_by_doi=_lookup_local_item_key_by_doi,
        lookup_by_arxiv_extra=_lookup_local_item_key_by_arxiv_extra,
    )


# ---------------------------------------------------------------------------
# MCP Tool: ingest_by_identifiers
# ---------------------------------------------------------------------------

@mcp.tool(tags=tool_tags("core", "ingestion"))
def ingest_by_identifiers(
    candidates: Annotated[
        list[IngestCandidate] | None,
        BeforeValidator(_parse_json_string_list),
        Field(description=(
            "**Preferred path.** Structured candidates from "
            "search_academic_databases. Pass each selected search result dict "
            "directly. Extra fields are ignored. Do not reconstruct identifier "
            "strings from memory."
        )),
    ] = None,
    identifiers: Annotated[
        list[str] | None,
        BeforeValidator(_parse_json_string_list),
        Field(description=(
            "_Deprecated._ Direct DOI/arXiv/URL string list kept only for "
            "backward compatibility. Use candidates= instead."
        )),
    ] = None,
) -> dict:
    """Ingest papers into Zotero's INBOX collection. Per-paper status, synchronous.

    Destination and tagging are **not** caller-controlled:
      - All new items land in the INBOX collection (auto-created on first use).
      - Tags are NEVER applied at save time. Topic tagging and reclassification
        happen in Phase 3 via `manage_tags` / `manage_collections` through the
        plan-then-execute workflow in ztp-research — this prevents drive-by
        tagging that bypasses user vocabulary review.

    Internal flow: normalize → dedup → connector check → preflight →
    sequential save+verify → API fallback on failure → PDF check.

    Statuses: saved_with_pdf, saved_metadata_only, blocked, duplicate, failed.
    When action_required is non-empty, surface to user and wait.
    """
    # MCP client compatibility: some clients (Qwen-based 'Sisyphus' runtimes,
    # older Claude Code builds) wrap list[T] params as JSON strings on the
    # wire. The signature has a BeforeValidator that catches this during
    # FastMCP dispatch, but we also coerce defensively here so direct Python
    # calls (tests, in-process callers) get the same behavior and so the
    # string → list[IngestCandidate] conversion is explicit at the body level.
    candidates = _parse_json_string_list(candidates)
    identifiers = _parse_json_string_list(identifiers)
    if isinstance(candidates, str):
        raise ToolError(
            "`candidates` could not be parsed as a JSON list of candidate "
            "dicts. Pass the search result object directly without "
            "modification."
        )
    if isinstance(identifiers, str):
        raise ToolError(
            "`identifiers` could not be parsed as a JSON list of strings. "
            'Expected form: ["10.1234/abc", "2301.00001"].'
        )
    if isinstance(candidates, list):
        candidates = [
            c if isinstance(c, IngestCandidate) else IngestCandidate.model_validate(c)
            for c in candidates
        ]

    # Empty lists count as "absent": calling ingest with zero candidates is
    # almost always an upstream filter bug (e.g. `local_duplicate` wiped the
    # selection). Fail loudly instead of silently returning total=0.
    has_candidates = bool(candidates)
    has_identifiers = bool(identifiers)
    if not has_candidates and not has_identifiers:
        raise ToolError(
            "ingest_by_identifiers requires at least one candidate. Pass "
            "`candidates` (preferred — structured dicts from "
            "search_academic_databases results) or `identifiers` "
            "(deprecated — raw DOI/URL strings). Empty or missing inputs "
            "are not a valid call — this usually means an upstream filter "
            "removed every selection. Pass the search result object "
            "directly without modification."
        )
    if has_candidates and has_identifiers:
        raise ToolError(
            "ingest_by_identifiers accepts only one of `candidates` or "
            "`identifiers`, not both. Prefer `candidates`."
        )

    bridge_url = f"http://127.0.0.1:{DEFAULT_PORT}"
    get_writer = _get_writer
    _get_zotero()
    total_inputs = len(candidates) if has_candidates else len(identifiers or [])

    # Destination is hardcoded to INBOX. _ensure_inbox_collection auto-creates
    # it on first use; returns None only when ZOTERO_API_KEY is missing or the
    # writer init fails — in that case the tool cannot function at all.
    collection_key = _ensure_inbox_collection()
    if not collection_key:
        raise ToolError(
            "INBOX collection unavailable. ingest_by_identifiers requires "
            "ZOTERO_API_KEY and ZOTERO_USER_ID so it can create and route "
            "items into the INBOX collection. Configure credentials and retry."
        )

    # Step 1: Normalize inputs → internal candidates
    if candidates is not None:
        candidates_internal = _candidates_to_internal(candidates)
    else:
        logger.warning(
            "ingest_by_identifiers called with deprecated identifiers=<list[str]>. "
            "Prefer candidates=<list[dict]> forwarded from "
            "search_academic_databases results. The str branch will be removed "
            "in 0.6.0."
        )
        candidates_internal = _identifiers_to_internal(identifiers or [])

    # Step 2: Local dedup — check journal DOI, arXiv DOI variant, and extra field
    for candidate in candidates_internal:
        if candidate.get("status"):
            continue

        doi_candidates: list[str] = []
        for value in (candidate.get("source_doi"), candidate.get("doi")):
            normalized = search.normalize_doi(value)
            if normalized and normalized not in doi_candidates:
                doi_candidates.append(normalized)

        arxiv_id = _normalize_arxiv_id(candidate.get("arxiv_id"))
        existing_key: str | None = None
        for doi_value in doi_candidates:
            existing_key = _lookup_local_item_key_by_doi(doi_value)
            if existing_key:
                break

        if not existing_key and arxiv_id:
            arxiv_doi_variant = _arxiv_doi(arxiv_id)
            if arxiv_doi_variant not in doi_candidates:
                existing_key = _lookup_local_item_key_by_doi(arxiv_doi_variant)

        if not existing_key and arxiv_id:
            existing_key = _lookup_local_item_key_by_arxiv_extra(arxiv_id)

        if existing_key:
            candidate["status"] = "duplicate"
            candidate["item_key"] = existing_key

    # Step 3: Check Connector availability
    active_candidates = [c for c in candidates_internal if not c.get("status") and c.get("url")]
    ext_ok = False
    if active_candidates:
        ext_ok, _ext_error, _ = connector.check_connector_availability(
            active_candidates, DEFAULT_PORT, BridgeServer,
        )

    # action_required declared early — needed by both Step 4 blocking and Step 5 anti-bot
    action_required: list[dict] = []

    # Step 4: Preflight (if Connector online)
    if ext_ok and active_candidates:
        urls = [c["url"] for c in active_candidates if c.get("url")]
        if urls:
            remaining, preflight_failures, blocking, blocked_publishers = connector.run_preflight_check(
                [{"url": u} for u in urls], DEFAULT_PORT, BridgeServer, logger,
            )

            # NEW: Blocking behavior — 全批预检完成后统一判断
            if blocking:
                # Extract blocked publisher domains from preflight failures
                blocked_domains = {
                    connector.extract_publisher_domain(f["url"]) for f in preflight_failures
                    if f.get("error_code") == "anti_bot_detected" and f.get("url")
                }
                blocked_count = 0
                pending_count = 0

                # 区分两种状态：被拦截 vs 通过预检但整批等待
                for candidate in candidates_internal:
                    if not candidate.get("status"):
                        url = candidate.get("url", "")
                        domain = connector.extract_publisher_domain(url) if url else ""
                        if domain in blocked_domains:
                            # 真正被 anti-bot 拦截
                            candidate["status"] = "preflight_blocked"
                            candidate["error"] = "anti_bot_detected"
                            blocked_count += 1
                        else:
                            # 预检通过，因批次阻塞等待
                            candidate["status"] = "preflight_pending"
                            candidate["note"] = "passed preflight, waiting for blocked publishers to clear"
                            pending_count += 1

                # Extract publisher domain strings from the 4-tuple blocked_publishers
                publisher_names = (
                    [p["publisher"] for p in blocked_publishers]
                    if blocked_publishers
                    else sorted(blocked_domains)
                )
                action_required.append({
                    "type": "preflight_blocked",
                    "publishers": publisher_names,
                    "blocked_count": blocked_count,
                    "pending_count": pending_count,
                    "message": (
                        f"{blocked_count} 篇被 anti-bot 拦截"
                        f"（{', '.join(publisher_names)}），"
                        f"{pending_count} 篇就绪等待。"
                        "请在浏览器完成验证后重新调用 ingest_by_identifiers。"
                    ),
                })

                # Return early — 全批不 save，等用户决策后重新调用
                return {
                    "total": total_inputs,
                    "results": [_result_from_candidate(c) for c in candidates_internal],
                    "action_required": action_required,
                }

            # When no blocking: use remaining as the filtered candidate list for save
            remaining_ids = {id(c) for c in remaining}
            candidates_internal = [c for c in candidates_internal if id(c) in remaining_ids]


    # Step 5: Sequential save + verify
    results: list[dict] = []
    for index, candidate in enumerate(candidates_internal):
        # Already resolved (failed, duplicate)
        if candidate.get("status"):
            results.append(_result_from_candidate(candidate))
            continue

        url = candidate.get("url")
        doi = candidate.get("doi")
        title = candidate.get("title")

        if ext_ok and url:
            # Connector route. tags=None is invariant — see tool docstring.
            result = connector.save_single_and_verify(
                url, doi, title,
                arxiv_id=candidate.get("arxiv_id"),
                collection_key=collection_key, tags=None,
                bridge_url=bridge_url, get_writer=get_writer,
                writer_lock=_writer_lock, _logger=logger,
            )
        elif doi:
            # API-only route. tags=None is invariant — see tool docstring.
            result = connector._doi_api_fallback(
                doi, title,
                arxiv_id=candidate.get("arxiv_id"),
                oa_url=None,
                collection_key=collection_key, tags=None,
                get_writer=get_writer, writer_lock=_writer_lock, _logger=logger,
            )
        else:
            result = {
                "status": "failed", "error": "no_usable_identifier",
                "item_key": None, "has_pdf": False, "title": title or "",
                "action_required": None, "warning": None,
            }

        results.append({
            **result,
            "identifier": candidate.get("identifier", ""),
            "candidate_index": candidate.get("_index", index),
        })

        # Anti-bot: halt remaining
        if result.get("status") == "blocked":
            action_required.append({
                "type": "anti_bot_detected",
                "message": result.get("action_required", ""),
                "identifier": candidate.get("identifier", ""),
            })
            # Mark remaining as blocked
            for rem in candidates_internal[index + 1:]:
                if not rem.get("status"):
                    results.append(
                        _result_from_candidate(
                            rem,
                            status="blocked",
                            error="batch_halted_by_anti_bot",
                        )
                    )
            break

    return {
        "total": total_inputs,
        "results": results,
        "action_required": action_required,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ARXIV_ID_RE = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$", re.IGNORECASE)


def _looks_like_arxiv_id(s: str) -> bool:
    return bool(_ARXIV_ID_RE.match(s.strip()))

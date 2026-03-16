"""Shared state and lazy singletons for ZotPilot MCP server."""
import os
import sys
import time
import logging
import threading
from collections import defaultdict
from dataclasses import replace
from fastmcp import FastMCP
from .config import Config
from .models import RetrievalResult

logger = logging.getLogger(__name__)

# Try to import FastMCP's error type; define fallback if not available
try:
    from fastmcp.exceptions import ToolError
except ImportError:
    class ToolError(Exception):
        """Error raised by MCP tools to signal failure to client."""
        pass


def _get_ancestor_pid():
    """
    Get the PID to monitor for parent death.

    On Windows with subprocess.Popen, there may be an intermediate process
    between the actual parent (Claude Code) and this process. We need to
    find the real parent by walking up the process tree.
    """
    if sys.platform != 'win32':
        return os.getppid()

    import ctypes
    from ctypes import wintypes

    ntdll = ctypes.WinDLL('ntdll')

    class PROCESS_BASIC_INFORMATION(ctypes.Structure):
        _fields_ = [
            ('Reserved1', ctypes.c_void_p),
            ('PebBaseAddress', ctypes.c_void_p),
            ('Reserved2', ctypes.c_void_p * 2),
            ('UniqueProcessId', wintypes.HANDLE),
            ('InheritedFromUniqueProcessId', wintypes.HANDLE),
        ]

    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    def get_parent_pid(pid):
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        pbi = PROCESS_BASIC_INFORMATION()
        ret_len = ctypes.c_ulong()
        status = ntdll.NtQueryInformationProcess(
            handle, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), ctypes.byref(ret_len)
        )
        kernel32.CloseHandle(handle)
        if status == 0:
            return int(pbi.InheritedFromUniqueProcessId)
        return None

    # Get parent and grandparent
    parent_pid = os.getppid()
    grandparent_pid = get_parent_pid(parent_pid)

    # Return grandparent if available (skips intermediate process), else parent
    return grandparent_pid if grandparent_pid else parent_pid


def _start_parent_monitor():
    """
    Monitor parent process and exit when it dies.

    When the parent process (Claude Code) terminates, this process should
    also exit. Without this monitor, the asyncio event loop may hang
    indefinitely, leaving orphaned processes that consume CPU.
    """
    target_pid = _get_ancestor_pid()

    def monitor():
        if sys.platform == 'win32':
            import ctypes
            kernel32 = ctypes.windll.kernel32

            SYNCHRONIZE = 0x00100000
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, target_pid)

            if handle:
                # Wait for process to exit (blocks until process dies)
                INFINITE = 0xFFFFFFFF
                kernel32.WaitForSingleObject(handle, INFINITE)
                kernel32.CloseHandle(handle)
        else:
            # Unix: poll parent PID
            while True:
                time.sleep(1.0)
                try:
                    os.kill(target_pid, 0)
                except (OSError, PermissionError):
                    break

        os._exit(0)

    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()


# Start parent monitor before anything else
_start_parent_monitor()

_MCP_INSTRUCTIONS = """\
ZotPilot — AI-powered Zotero research assistant. Tool selection guide:

| User intent                        | Tool             |
|------------------------------------|------------------|
| Find specific passages or claims   | search_papers    |
| Survey a topic / find papers       | search_topic     |
| Find paper by exact terms          | search_boolean   |
| Find data tables                   | search_tables    |
| Find figures or diagrams           | search_figures   |

Start with get_index_stats to check readiness. If doc count is 0, \
tell the user to run `zotpilot index` first.

For thorough research, chain: search_topic → get_paper_details → \
search_papers with section_weights. Use search_boolean for exact \
terms (author names, acronyms). Use get_passage_context to expand \
any result with surrounding text.

Write operations (tags, collections) require ZOTERO_API_KEY and \
ZOTERO_USER_ID environment variables.
"""

mcp = FastMCP("zotpilot", instructions=_MCP_INSTRUCTIONS)

# Lazy initialization
_retriever = None
_store = None
_reranker = None
_config = None


def _get_retriever():
    global _retriever, _store, _reranker, _config
    if _retriever is None:
        from .vector_store import VectorStore
        from .retriever import Retriever
        from .reranker import Reranker
        from .embeddings import create_embedder

        _config = Config.load()
        embedder = create_embedder(_config)
        _store = VectorStore(_config.chroma_db_path, embedder)
        _retriever = Retriever(_store)
        _reranker = Reranker(alpha=_config.rerank_alpha)
    return _retriever


def _get_store():
    _get_retriever()  # Ensure initialized
    return _store


def _get_reranker():
    _get_retriever()  # Ensure initialized
    return _reranker


_zotero = None


def _get_zotero():
    global _zotero, _config
    if _zotero is None:
        if _config is None:
            _config = Config.load()
        from .zotero_client import ZoteroClient
        _zotero = ZoteroClient(_config.zotero_data_dir)
    return _zotero


_writer = None


def _get_writer():
    """Lazy-initialize ZoteroWriter (Pyzotero Web API)."""
    global _writer, _config
    if _writer is None:
        if _config is None:
            _config = Config.load()
        if not _config.zotero_api_key:
            raise ToolError("ZOTERO_API_KEY not set -- write operations unavailable")
        if not _config.zotero_user_id:
            raise ToolError("ZOTERO_USER_ID not set -- write operations unavailable")
        from .zotero_writer import ZoteroWriter
        _writer = ZoteroWriter(
            _config.zotero_api_key,
            _config.zotero_user_id,
            _config.zotero_library_type,
        )
    return _writer


def _get_config():
    """Ensure _config is loaded and return it."""
    global _config
    if _config is None:
        _config = Config.load()
    return _config


def _stored_chunk_to_retrieval_result(chunk) -> RetrievalResult:
    """Convert a StoredChunk to RetrievalResult for reranking."""
    meta = chunk.metadata
    return RetrievalResult(
        chunk_id=chunk.id,
        text=chunk.text,
        score=chunk.score,
        doc_id=meta.get("doc_id", ""),
        doc_title=meta.get("doc_title", ""),
        authors=meta.get("authors", ""),
        year=meta.get("year"),
        page_num=meta.get("page_num", 0),
        chunk_index=meta.get("chunk_index", 0),
        citation_key=meta.get("citation_key", ""),
        publication=meta.get("publication", ""),
        section=meta.get("section", "table"),  # Tables default to "table" section
        section_confidence=meta.get("section_confidence", 1.0),
        journal_quartile=meta.get("journal_quartile"),
    )


VALID_CHUNK_TYPES = {"text", "figure", "table"}


def _build_chromadb_filters(
    year_min: int | None = None,
    year_max: int | None = None,
    chunk_types: list[str] | None = None,
) -> dict | None:
    """Build ChromaDB where clause for year range and chunk_type filters.

    IMPORTANT: ChromaDB only supports: $eq, $ne, $gt, $gte, $lt, $lte, $in, $nin
    It does NOT support substring/contains operations on metadata.
    Text-based filters (author, tag, collection) must use _apply_text_filters().

    Args:
        year_min: Minimum publication year
        year_max: Maximum publication year
        chunk_types: Filter to specific chunk types (text, figure, table)

    Returns:
        ChromaDB where clause dict, or None if no filters
    """
    conditions = []
    if year_min:
        conditions.append({"year": {"$gte": year_min}})
    if year_max:
        conditions.append({"year": {"$lte": year_max}})
    if chunk_types:
        if len(chunk_types) == 1:
            conditions.append({"chunk_type": {"$eq": chunk_types[0]}})
        else:
            conditions.append({"chunk_type": {"$in": chunk_types}})

    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}


def _meta_get(r, key: str, default: str = "") -> str:
    """Get a metadata field from StoredChunk (.metadata dict) or RetrievalResult (attrs)."""
    if hasattr(r, "metadata") and isinstance(r.metadata, dict):
        return r.metadata.get(key, default)
    return getattr(r, key, default)


def _apply_text_filters(
    results: list,
    author: str | None = None,
    tag: str | None = None,
    collection: str | None = None,
) -> list:
    """Apply substring-based filters in Python (post-retrieval).

    ChromaDB doesn't support substring matching, so we filter after retrieval.
    All matches are case-insensitive substrings.

    Works with both StoredChunk (metadata dict) and RetrievalResult (dataclass attrs).

    Args:
        results: List of StoredChunk or RetrievalResult objects
        author: Author name substring (case-insensitive)
        tag: Tag substring (case-insensitive)
        collection: Collection name substring (case-insensitive)

    Returns:
        Filtered list
    """
    if not author and not tag and not collection:
        return results

    author_lower = author.lower() if author else None
    tag_lower = tag.lower() if tag else None
    collection_lower = collection.lower() if collection else None

    filtered = []
    for r in results:
        if author_lower:
            authors = _meta_get(r, "authors", "").lower()
            if author_lower not in authors:
                continue

        if tag_lower:
            tags = _meta_get(r, "tags", "").lower()
            if tag_lower not in tags:
                continue

        if collection_lower:
            colls = _meta_get(r, "collections", "").lower()
            if collection_lower not in colls:
                continue

        filtered.append(r)

    return filtered


def _has_text_filters(author: str | None, tag: str | None, collection: str | None) -> bool:
    """Check if any text-based filters are active."""
    return bool(author or tag or collection)


def _apply_required_terms(results: list, terms: list[str]) -> list:
    """Filter results to only those containing all required terms as whole words.

    Case-insensitive. Checks the passage text (and full_context if available).
    """
    import re
    patterns = [re.compile(r'\b' + re.escape(t) + r'\b', re.IGNORECASE) for t in terms]

    filtered = []
    for r in results:
        text = getattr(r, 'text', '') or ''
        full_ctx = r.full_context() if hasattr(r, 'full_context') and callable(getattr(r, 'full_context', None)) else ''
        combined = text + ' ' + full_ctx
        if all(p.search(combined) for p in patterns):
            filtered.append(r)
    return filtered


def _contains_chinese(text: str) -> bool:
    """Return True if text contains at least one Chinese character."""
    import re
    return bool(re.search(r'[\u4e00-\u9fff\u3400-\u4dbf\U00020000-\U0002a6df]', text))


def _translate_to_english(text: str) -> str | None:
    """Translate Chinese text to English using Gemini. Returns None on failure."""
    api_key = _config.gemini_api_key if _config else None
    if not api_key:
        api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    try:
        import google.genai as genai
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-2.0-flash-lite",
            contents=(
                "Translate the following Chinese academic query to English. "
                "Output only the translated text, nothing else.\n\n" + text
            ),
        )
        translated = response.text.strip()
        logger.debug(f"Translated query: '{text}' -> '{translated}'")
        return translated if translated else None
    except Exception as e:
        logger.warning(f"Query translation failed: {e}")
        return None


def _merge_results_by_chunk(primary: list, secondary: list, top_k: int) -> list:
    """Merge two result lists, keeping the best composite_score per unique (doc_id, chunk_index)."""
    seen: dict[tuple, object] = {}
    for r in primary + secondary:
        key = (r.doc_id, r.chunk_index)
        existing = seen.get(key)
        if existing is None:
            seen[key] = r
        else:
            # Keep whichever has the higher composite_score (or score as fallback)
            r_score = r.composite_score if r.composite_score is not None else r.score
            e_score = existing.composite_score if existing.composite_score is not None else existing.score
            if r_score > e_score:
                seen[key] = r
    merged = sorted(seen.values(),
                    key=lambda x: x.composite_score if x.composite_score is not None else x.score,
                    reverse=True)
    return merged[:top_k]


def _result_to_dict(r) -> dict:
    """Convert RetrievalResult to API response dict.

    Expects r.composite_score to be populated by reranker.
    """
    return {
        "doc_title": r.doc_title,
        "authors": r.authors,
        "year": r.year,
        "citation_key": r.citation_key,
        "publication": r.publication,
        "page": r.page_num,
        "relevance_score": round(r.score, 3),
        "composite_score": round(r.composite_score, 3) if r.composite_score is not None else None,
        "section": r.section,
        "section_confidence": round(r.section_confidence, 2),
        "journal_quartile": r.journal_quartile,
        "passage": r.text,
        "context_before": r.context_before,
        "context_after": r.context_after,
        "full_context": r.full_context(),
        "doc_id": r.doc_id,
        "chunk_index": r.chunk_index,
    }

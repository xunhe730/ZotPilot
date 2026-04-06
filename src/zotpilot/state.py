"""Shared state and lazy singletons for ZotPilot MCP server."""

import logging
import os
import sys
import threading
import time
from collections.abc import Callable

from fastmcp import FastMCP

from .config import Config

# Re-exports for backward compatibility (tools other than search.py import from here)
from .filters import (  # noqa: F401
    VALID_CHUNK_TYPES,
    _apply_required_terms,
    _apply_text_filters,
    _build_chromadb_filters,
    _has_text_filters,
    _meta_get,
)
from .result_utils import (  # noqa: F401
    _merge_results_by_chunk,
    _result_to_dict,
    _stored_chunk_to_retrieval_result,
)

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

| User intent                                             | Tool                        |
|---------------------------------------------------------|-----------------------------|
| Find specific passages or claims                        | search_papers               |
| Survey a topic / find papers                            | search_topic                |
| Find paper by exact terms                               | search_boolean              |
| Filter by year/author/tag/etc.                          | advanced_search             |
| Find data tables                                        | search_tables               |
| Find figures or diagrams                                | search_figures              |
| Search external academic databases (OpenAlex)            | search_academic_databases   |
| Batch add papers from search results to Zotero          | ingest_papers               |
| Save browser pages into Zotero via Connector            | save_urls                   |

**Note**: `search_topic` searches your LOCAL indexed Zotero library \
(requires prior `index_library`). `search_academic_databases` queries \
EXTERNAL databases (OpenAlex) and finds papers not yet in your library.

**Default `core` profile tools:**
`search_topic`, `search_papers`, `get_passage_context`, `advanced_search`,
`get_paper_details`, `search_academic_databases`, `ingest_papers`,
`get_ingest_status`, `get_index_stats`, `research_session`

Set `ZOTPILOT_TOOL_PROFILE=extended` to expose advanced browse/write/admin tools,
or `ZOTPILOT_TOOL_PROFILE=all` for the complete tool surface.

**Default research flow:**
1. `search_topic` to discover what is already in the local library
2. Optionally `search_papers` for supporting passages
3. Optionally `get_passage_context` for surrounding text

All search tools default to `verbosity="minimal"`. Escalate to `standard` \
or `full` only when needed. `search_papers` defaults to `context_chunks=0`; \
set `context_chunks=1` only when adjacent context is useful. \
`search_topic` no longer returns `best_passage_context` — use \
`search_papers` or `get_passage_context` instead.

`doc_id` is the canonical identifier in search and library results. \
`browse_library` and `get_paper_details` return `doc_id` instead of `key`.

**Typical literature collection workflow:**
1. `search_academic_databases` → review candidates
2. `ingest_papers` with selected papers → added to Zotero with metadata and OA PDF

advanced_search works without indexing — use for precise metadata \
filters. Extended profile adds browse/write/admin tools such as \
`browse_library`, `manage_tags`, `index_library`, and `switch_library`. \
Use `browse_library(view="feeds")` for RSS feeds. `get_index_stats` also \
handles unindexed-paper pagination plus optional reranking and vision-cost details. \
Use `research_session` to create, resume, approve, or validate guarded research runs.

Write operations (tags, collections, notes) require ZOTERO_API_KEY \
and ZOTERO_USER_ID environment variables.
"""

mcp = FastMCP("zotpilot", instructions=_MCP_INSTRUCTIONS)

# Lazy initialization with thread safety
_init_lock = threading.Lock()
_retriever = None
_store = None
_reranker = None
_config = None
_reset_callbacks: list[Callable[[], None]] = []


def _get_retriever():
    global _retriever, _store, _reranker, _config
    if _retriever is None:
        with _init_lock:
            if _retriever is None:
                _config = Config.load()
                if _config.embedding_provider == "none":
                    raise ToolError(
                        "Semantic search requires indexing. "
                        "Configure an embedding provider (gemini/dashscope/local) "
                        "and run index_library() first."
                    )
                from .embeddings import create_embedder
                from .reranker import Reranker
                from .retriever import Retriever
                from .vector_store import VectorStore

                embedder = create_embedder(_config)
                _store = VectorStore(_config.chroma_db_path, embedder)
                _retriever = Retriever(_store)
                _reranker = Reranker(alpha=_config.rerank_alpha)
    return _retriever


def _get_store():
    _get_retriever()  # Ensure initialized
    return _store


def _get_store_optional():
    """Returns VectorStore or None if No-RAG mode (embedding_provider='none')."""
    config = _get_config()
    if config.embedding_provider == "none":
        return None
    return _get_store()


def _get_reranker():
    _get_retriever()  # Ensure initialized
    return _reranker


_zotero = None


def _get_zotero():
    global _zotero, _config
    if _zotero is None:
        with _init_lock:
            if _zotero is None:
                if _config is None:
                    _config = Config.load()
                from .zotero_client import ZoteroClient
                if _library_override and _library_override["library_type"] == "group":
                    lib_id = ZoteroClient.resolve_group_library_id(
                        _config.zotero_data_dir, int(_library_override["library_id"])
                    )
                    _zotero = ZoteroClient(_config.zotero_data_dir, library_id=lib_id)
                else:
                    _zotero = ZoteroClient(_config.zotero_data_dir)
    return _zotero


_writer = None
_resolver = None


def _get_resolver():
    """Lazy-initialize IdentifierResolver."""
    global _resolver
    if _resolver is None:
        with _init_lock:
            if _resolver is None:
                from .identifier_resolver import IdentifierResolver
                _resolver = IdentifierResolver()
    return _resolver


def _get_writer():
    """Lazy-initialize ZoteroWriter (Pyzotero Web API)."""
    global _writer, _config
    if _writer is None:
        with _init_lock:
            if _writer is None:
                if _config is None:
                    _config = Config.load()
                if not _config.zotero_api_key:
                    raise ToolError("ZOTERO_API_KEY not set -- write operations unavailable")
                if not _config.zotero_user_id:
                    raise ToolError("ZOTERO_USER_ID not set -- write operations unavailable")
                from .zotero_writer import ZoteroWriter
                # Apply library override if set
                if _library_override:
                    lib_id = _library_override["library_id"]
                    lib_type = _library_override["library_type"]
                else:
                    lib_id = _config.zotero_user_id
                    lib_type = _config.zotero_library_type
                _writer = ZoteroWriter(
                    _config.zotero_api_key,
                    lib_id,
                    lib_type,
                )
    return _writer


_api_reader = None


def _get_api_reader():
    """Lazy-initialize ZoteroApiReader (read-only Pyzotero Web API)."""
    global _api_reader, _config
    if _api_reader is None:
        with _init_lock:
            if _api_reader is None:
                if _config is None:
                    _config = Config.load()
                if not _config.zotero_api_key:
                    raise ToolError("ZOTERO_API_KEY not set -- annotation reading unavailable")
                if not _config.zotero_user_id:
                    raise ToolError("ZOTERO_USER_ID not set -- annotation reading unavailable")
                from .zotero_api_reader import ZoteroApiReader
                # Apply library override if set
                if _library_override:
                    lib_id = _library_override["library_id"]
                    lib_type = _library_override["library_type"]
                else:
                    lib_id = _config.zotero_user_id
                    lib_type = _config.zotero_library_type
                _api_reader = ZoteroApiReader(
                    _config.zotero_api_key,
                    lib_id,
                    lib_type,
                )
    return _api_reader


def _get_config():
    """Ensure _config is loaded and return it."""
    global _config
    if _config is None:
        with _init_lock:
            if _config is None:
                _config = Config.load()
    return _config


# Library override for switch_library
_library_override: dict | None = None


def _reset_singletons():
    """Tear down all cached singletons. Called by switch_library."""
    global _retriever, _store, _reranker, _config, _zotero, _writer, _api_reader, _resolver
    with _init_lock:
        _retriever = None
        _store = None
        _reranker = None
        _config = None
        _zotero = None
        _writer = None
        _api_reader = None
        _resolver = None
    for callback in _reset_callbacks:
        try:
            callback()
        except Exception:
            pass


def register_reset_callback(fn: Callable[[], None]) -> None:
    """Register a function to be called when cached singletons are reset."""
    _reset_callbacks.append(fn)


def _set_library_override(library_id: str, library_type: str):
    """Set library override and reset singletons."""
    global _library_override
    _library_override = {"library_id": library_id, "library_type": library_type}
    _reset_singletons()


def _clear_library_override():
    """Clear library override and reset singletons."""
    global _library_override
    _library_override = None
    _reset_singletons()

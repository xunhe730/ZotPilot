"""Shared state and lazy singletons for ZotPilot MCP server."""
import os
import sys
import time
import logging
import threading
from fastmcp import FastMCP
from .config import Config

# Re-exports for backward compatibility (tools other than search.py import from here)
from .filters import (  # noqa: F401
    VALID_CHUNK_TYPES, _build_chromadb_filters, _meta_get,
    _apply_text_filters, _has_text_filters, _apply_required_terms,
)
from .result_utils import (  # noqa: F401
    _stored_chunk_to_retrieval_result, _merge_results_by_chunk, _result_to_dict,
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

| User intent                        | Tool             |
|------------------------------------|------------------|
| Find specific passages or claims   | search_papers    |
| Survey a topic / find papers       | search_topic     |
| Find paper by exact terms          | search_boolean   |
| Filter by year/author/tag/etc.     | advanced_search  |
| Find data tables                   | search_tables    |
| Find figures or diagrams           | search_figures   |

Start with get_index_stats to check readiness. If doc count is 0, \
tell the user to run `zotpilot index` first.

For thorough research, chain: search_topic → get_paper_details → \
search_papers with section_weights. Use search_boolean for exact \
terms (author names, acronyms). Use get_passage_context to expand \
any result with surrounding text.

advanced_search works without indexing — use for precise metadata \
filters. get_notes/create_note for reading and writing notes.

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
                from .vector_store import VectorStore
                from .retriever import Retriever
                from .reranker import Reranker
                from .embeddings import create_embedder

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
                _zotero = ZoteroClient(_config.zotero_data_dir)
    return _zotero


_writer = None


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
        with _init_lock:
            if _config is None:
                _config = Config.load()
    return _config

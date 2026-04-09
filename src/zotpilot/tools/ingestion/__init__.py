"""Ingestion package — thin re-export shim.

Sub-modules hold all implementation and @mcp.tool registrations.
Importing this package triggers registration of all MCP tools as a side-effect.
All public names are re-exported here so that test patches on
'zotpilot.tools.ingestion.X' work correctly at call time.
"""

from __future__ import annotations

import time as time  # noqa: PLC0414  re-exported for test patching

import httpx as httpx  # noqa: PLC0414  re-exported for test patching

from ...bridge import DEFAULT_PORT as DEFAULT_PORT  # noqa: F401  re-exported for test patching
from ...bridge import BridgeServer as BridgeServer  # noqa: F401  re-exported for test patching
from ...state import _get_config, _get_writer, _get_zotero  # noqa: F401  re-exported for test patching
from .. import ingestion_bridge, ingestion_search  # noqa: F401  re-exported
from ..ingest_state import BatchStore as BatchStore  # noqa: F401
from ..ingest_state import IngestItemState as IngestItemState  # noqa: F401

# Sub-module imports trigger @mcp.tool registration as side-effects.
from . import _ingest, _save, _shared  # noqa: F401
from ._ingest import (
    _apply_bridge_result_routing,
    _batch_store,
    _clear_batch_store,
    _clear_inbox_cache,
    _ensure_inbox_collection,
    _executor,
    _inbox_collection_key,  # noqa: F401  re-exported for test patching
    _resolve_dois_concurrent,
    _update_session_after_ingest,
    get_ingest_status,
    ingest_papers,
    search_academic_databases,
)
from ._ingest import (
    get_ingest_status as get_ingest_status_impl,  # noqa: PLC0414
)
from ._ingest import (
    ingest_papers as ingest_papers_impl,  # noqa: PLC0414
)
from ._ingest import (
    search_academic_databases as search_academic_databases_impl,  # noqa: PLC0414
)
from ._save import _run_save_worker, _save_via_api, save_urls
from ._shared import (
    _coerce_json_list,
    _discover_via_local_api,
    _discover_via_web_api,
    _is_pdf_or_doi_url,
    _lookup_local_item_key_by_doi,
    _lookup_suspected_local_duplicates_by_title,
    _route_via_local_api,
    _writer_lock,
    classify_ingest_candidate,
    logger,
    resolve_doi_to_landing_url,
)

__all__ = [
    # state accessors (for test patching via this namespace)
    "_get_config",
    "_get_writer",
    "_get_zotero",
    "ingestion_bridge",
    "ingestion_search",
    "time",
    "httpx",
    "BridgeServer",
    "DEFAULT_PORT",
    # shared
    "classify_ingest_candidate",
    "logger",
    "resolve_doi_to_landing_url",
    "_coerce_json_list",
    "_discover_via_local_api",
    "_discover_via_web_api",
    "_is_pdf_or_doi_url",
    "_lookup_local_item_key_by_doi",
    "_lookup_suspected_local_duplicates_by_title",
    "_resolve_dois_concurrent",
    "_route_via_local_api",
    "_writer_lock",
    # ingest
    "_apply_bridge_result_routing",
    "_batch_store",
    "_clear_batch_store",
    "_clear_inbox_cache",
    "_ensure_inbox_collection",
    "_executor",
    "_update_session_after_ingest",
    "get_ingest_status",
    "ingest_papers",
    "search_academic_databases",
    # save
    "_run_save_worker",
    "_save_via_api",
    "save_urls",
    # store types
    "BatchStore",
    "IngestItemState",
    # aliases
    "get_ingest_status_impl",
    "ingest_papers_impl",
    "search_academic_databases_impl",
]

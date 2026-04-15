"""Connector communication layer for Zotero ingestion (v0.5.0).

Extracted from ingestion_bridge.py — contains:
- Constants (anti-bot patterns, error page patterns, publisher tags)
- Bridge/Connector communication (save, poll, preflight)
- Local Zotero API helpers (route, discover)
- DOI API fallback (CrossRef/arXiv → pyzotero)
- v0.5.0 new: validate_saved_item, delete_item_safe, check_pdf_status,
  save_single_and_verify
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, cast
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DISCOVERY_BACKOFF_DELAYS = [2.0, 4.0, 8.0, 16.0, 32.0]
ITEM_DISCOVERY_WINDOW_S = 120
PDF_VERIFICATION_WINDOW_S = 15.0
ROUTING_RETRY_DELAYS_S = [0.0, 5.0, 10.0, 15.0]

_PUBLISHER_TAG_SOURCES = {
    "arxiv.org", "biorxiv.org", "medrxiv.org",
    "sciencedirect.com", "elsevier.com",
    "springer.com", "springerlink.com", "nature.com",
    "wiley.com", "onlinelibrary.wiley.com",
    "acs.org", "pubs.acs.org",
    "iop.org", "iopscience.iop.org",
    "mdpi.com", "cambridge.org", "tandfonline.com",
    "aip.org", "aip.scitation.org",
    "rsc.org", "pnas.org",
    "ieee.org", "ieeexplore.ieee.org",
    "acm.org", "dl.acm.org",
}

ANTI_BOT_TITLE_PATTERNS = [
    "just a moment", "请稍候", "请稍等",
    "verify you are human", "access denied", "please verify",
    "robot check", "cloudflare", "security check", "captcha",
    "checking your browser", "one more step",
    "page not found", "404", "not found",
]

ERROR_PAGE_TITLE_PATTERNS = [
    "page not found", "404 -", "404 ",
    "access denied", "subscription required", "unavailable -",
]

TRANSLATOR_FALLBACK_SUFFIXES = [
    " | cambridge core", " | springerlink", " | sciencedirect",
    " | wiley online library", " | taylor & francis", " | oxford academic",
    " | jstor", " | aip publishing", " | acs publications",
    " | ieee xplore", " | sage journals", " | mdpi",
    " | frontiers", " | pnas", " | nature", " | annual reviews",
]

GENERIC_SITE_ONLY_TITLE_PATTERNS = [
    "| arxiv", "| arxiv.org", "| biorxiv", "| medrxiv",
]

VALID_ACADEMIC_ITEM_TYPES = frozenset({
    "journalArticle", "conferencePaper", "preprint", "thesis",
    "book", "bookSection", "report",
})


class _FakeBatch:
    """Minimal batch shim for single-item API fallback saves."""
    def update_item(self, idx, **kw):
        pass


_ZOTERO_LOCAL_API_ITEMS_URL = "http://127.0.0.1:23119/api/users/0/items"


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def looks_like_error_page_title(raw_title: str, item_key: str | None) -> bool:
    """Detect post-save error pages by title."""
    title = raw_title.strip().lower()
    if not title:
        return False
    if any(title.startswith(pattern) for pattern in ERROR_PAGE_TITLE_PATTERNS):
        return True
    if any(title.startswith(pattern) for pattern in ANTI_BOT_TITLE_PATTERNS):
        return True
    if item_key:
        return False
    return any(title.endswith(pattern) for pattern in GENERIC_SITE_ONLY_TITLE_PATTERNS)


def extract_publisher_domain(url: str) -> str:
    """Normalize a URL to a publisher-ish domain for preflight sampling."""
    hostname = (urlparse(url).hostname or "").lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return hostname or url




def _cleanup_publisher_tags(item_key: str | None, url: str, writer, _logger) -> None:
    """Clear publisher-injected tags after a successful save.

    Note: called immediately after Connector save, so pyzotero Web API may
    404 due to sync lag. This is a cosmetic cleanup — failures are logged
    at debug level since the save itself succeeded.
    """
    if not item_key:
        return
    try:
        current_tags = []
        if hasattr(writer, "_zot"):
            item = writer._zot.item(item_key)
            current_tags = list((item.get("data") or {}).get("tags") or [])
        writer.set_item_tags(item_key, [])
        _logger.info("Cleared %d publisher auto-tags for %s", len(current_tags), item_key)
    except Exception as exc:
        _logger.debug("Skipped publisher tag cleanup for %s (likely sync lag): %s",
                      item_key, exc)


def apply_collection_tag_routing(
    item_key: str,
    collection_key: str | None,
    tags: list[str] | None,
    writer,
    get_config,
) -> str | None:
    """Apply collection and/or tag routing to an item."""
    if not collection_key and not tags:
        return None

    config = get_config()
    if ((collection_key is not None) or (tags is not None)) and not config.zotero_api_key:
        return "collection_key and tags ignored — ZOTERO_API_KEY not configured"

    last_error: Exception | None = None
    for attempt, delay in enumerate(ROUTING_RETRY_DELAYS_S, start=1):
        if delay:
            time.sleep(delay)
        try:
            if collection_key:
                writer.add_to_collection(item_key, collection_key)
            if tags:
                writer.add_item_tags(item_key, tags)
            return None
        except Exception as exc:
            last_error = exc
    return f"collection_key/tags partially applied — {last_error}"


# ---------------------------------------------------------------------------
# Bridge/Connector communication
# ---------------------------------------------------------------------------

def get_extension_status(bridge_url: str) -> dict[str, Any]:
    """Query /status and return the parsed JSON, or an error dict."""
    try:
        response = urllib.request.urlopen(f"{bridge_url}/status", timeout=3)
        return dict(json.loads(response.read()))
    except Exception as exc:
        return {"extension_connected": False, "error": str(exc)}


def wait_for_extension(
    bridge_url: str,
    timeout_s: float = 12.0,
    poll_interval_s: float = 1.0,
    sleep_fn=time.sleep,
    monotonic_fn=time.monotonic,
) -> dict:
    """Poll /status until the extension reports connected, or timeout_s."""
    deadline = monotonic_fn() + timeout_s
    last_status: dict = {}
    while True:
        last_status = get_extension_status(bridge_url)
        if last_status.get("extension_connected"):
            return last_status
        if monotonic_fn() >= deadline:
            return last_status
        sleep_fn(poll_interval_s)


def _classify_preflight_error_code(result: dict) -> str:
    """Return a structured error code for a failed preflight result."""
    status = result.get("status", "")
    title = (result.get("title") or "").lower()
    error_msg = (result.get("error") or result.get("error_message") or "").lower()
    combined = f"{title} {error_msg}"

    if status == "anti_bot_detected":
        return "anti_bot_detected"

    _anti_bot_markers = (
        "cloudflare", "checking your browser", "captcha",
        "verify you are human", "just a moment", "please verify",
        "robot check", "security check", "access denied",
    )
    if any(marker in combined for marker in _anti_bot_markers):
        return "anti_bot_detected"

    _subscription_markers = (
        "subscribe", "purchase access", "full access", "get access",
        "sign in to access", "login to access", "log in to access",
        "paywall", "subscription required",
    )
    if any(marker in combined for marker in _subscription_markers):
        return "subscription_required"

    if "timeout" in error_msg:
        return "preflight_timeout"

    return "preflight_failed"


def sample_preflight_urls(urls: list[str], sample_size: int) -> tuple[list[str], list[str]]:
    """Pick up to sample_size URLs, favoring publisher diversity first."""
    if len(urls) <= sample_size:
        return list(urls), []

    grouped: dict[str, list[str]] = {}
    for url in urls:
        grouped.setdefault(extract_publisher_domain(url), []).append(url)

    sample: list[str] = []
    selected: set[str] = set()
    groups = sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0]))

    for _, group_urls in groups:
        if len(sample) >= sample_size:
            break
        url = group_urls[0]
        sample.append(url)
        selected.add(url)

    if len(sample) < sample_size:
        max_group_size = max(len(group_urls) for _, group_urls in groups)
        for index in range(1, max_group_size):
            for _, group_urls in groups:
                if len(sample) >= sample_size:
                    break
                if index < len(group_urls):
                    url = group_urls[index]
                    if url not in selected:
                        sample.append(url)
                        selected.add(url)

    skipped = [url for url in urls if url not in selected]
    return sample, skipped


def preflight_urls(
    urls: list[str],
    sample_size: int,
    default_port: int,
    bridge_server_cls,
    _logger,
    *,
    sleep_fn=time.sleep,
    monotonic_fn=time.monotonic,
) -> dict:
    """Probe URL accessibility via connector tabs before attempting saves."""
    if not urls:
        return {
            "checked": 0, "accessible": [], "blocked": [],
            "skipped": [], "errors": [], "all_clear": True,
        }

    sample, skipped_urls = sample_preflight_urls(urls, sample_size)
    report: dict[str, Any] = {
        "checked": len(sample),
        "accessible": [], "blocked": [],
        "skipped": [{"url": url, "reason": "sampling"} for url in skipped_urls],
        "errors": [], "all_clear": True,
    }

    bridge_url = f"http://127.0.0.1:{default_port}"
    bridge_was_running = bridge_server_cls.is_running(default_port)
    if not bridge_was_running:
        try:
            bridge_server_cls.auto_start(default_port)
        except RuntimeError as exc:
            report["errors"] = [{"url": url, "error": str(exc)} for url in sample]
            report["all_clear"] = False
            return report

    if not bridge_was_running:
        if not wait_for_extension(bridge_url, sleep_fn=sleep_fn, monotonic_fn=monotonic_fn):
            err_msg = (
                "Connector extension did not connect within 12 seconds. "
                "Please ensure Chrome is running and the ZotPilot Connector extension is enabled."
            )
            report["errors"] = [{"url": url, "error": err_msg} for url in sample]
            report["all_clear"] = False
            return report

    id_to_url: dict[str, str] = {}
    for url in sample:
        command = {"action": "preflight", "url": url}
        try:
            req = urllib.request.Request(
                f"{bridge_url}/enqueue",
                data=json.dumps(command).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            response = urllib.request.urlopen(req, timeout=5)
            body = json.loads(response.read())
            if "error_code" in body:
                report["errors"].append({
                    "url": url,
                    "error": body.get("error_message") or body["error_code"],
                    "error_code": body["error_code"],
                })
            else:
                id_to_url[body["request_id"]] = url
        except urllib.error.HTTPError as exc:
            try:
                err_body = json.loads(exc.read())
                report["errors"].append({
                    "url": url,
                    "error": err_body.get("error_message") or f"HTTP {exc.code}",
                    "error_code": err_body.get("error_code"),
                })
            except Exception:
                report["errors"].append({"url": url, "error": f"HTTP {exc.code}"})
        except Exception as exc:
            report["errors"].append({"url": url, "error": str(exc)})

    per_url_timeout = 60.0
    overall_timeout = 180.0
    polled: dict[str, dict] = {}
    pending_ids = set(id_to_url)
    per_request_deadlines = {
        request_id: monotonic_fn() + per_url_timeout for request_id in id_to_url
    }
    overall_deadline = monotonic_fn() + overall_timeout

    while pending_ids and monotonic_fn() < overall_deadline:
        sleep_fn(2)
        for request_id in list(pending_ids):
            url = id_to_url[request_id]
            if monotonic_fn() >= per_request_deadlines[request_id]:
                polled[request_id] = {
                    "status": "error", "url": url,
                    "error": "Timeout (60s) — page did not finish loading in time.",
                    "error_code": "preflight_timeout",
                }
                pending_ids.remove(request_id)
                continue
            try:
                response = urllib.request.urlopen(f"{bridge_url}/result/{request_id}", timeout=5)
                if response.status != 200:
                    continue
                result = json.loads(response.read())
                if result.get("status") in {"pending", "queued", "processing"}:
                    continue
                polled[request_id] = result
                pending_ids.remove(request_id)
            except Exception as exc:
                _logger.debug("Preflight poll %s: %s", request_id, exc)

    for request_id, url in id_to_url.items():
        result = polled.get(request_id, {
            "status": "error", "url": url,
            "error": "Timeout (overall) — preflight did not complete.",
            "error_code": "preflight_timeout",
        })
        status = result.get("status")
        if status == "accessible":
            report["accessible"].append({
                "url": url, "title": result.get("title", ""),
                "final_url": result.get("final_url", url),
            })
        elif status == "anti_bot_detected":
            error_code = result.get("error_code") or _classify_preflight_error_code(result)
            report["blocked"].append({
                "url": url, "title": result.get("title", ""),
                "final_url": result.get("final_url", url),
                "error_code": error_code,
            })
        else:
            error_code = result.get("error_code") or _classify_preflight_error_code(result)
            error_entry = {
                "url": url,
                "error": result.get("error") or result.get("error_message") or "unknown preflight error",
                "error_code": error_code,
            }
            if result.get("title"):
                error_entry["title"] = result["title"]
            report["errors"].append(error_entry)

    report["all_clear"] = not report["blocked"] and not report["errors"]
    return report


def enqueue_save_request(
    bridge_url: str,
    url: str,
    collection_key: str | None = None,
    tags: list[str] | None = None,
) -> tuple[str | None, dict | None]:
    """Enqueue one bridge save request."""
    command = {
        "action": "save", "url": url,
        "collection_key": collection_key, "tags": tags or [],
    }
    try:
        request = urllib.request.Request(
            f"{bridge_url}/enqueue",
            data=json.dumps(command).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        response = urllib.request.urlopen(request, timeout=5)
        body = json.loads(response.read())
        if "error_code" in body:
            return None, {"success": False, **body}
        return body["request_id"], None
    except urllib.error.HTTPError as exc:
        if exc.code == 503:
            try:
                err_body = json.loads(exc.read())
                return None, {"success": False, **err_body}
            except Exception:
                return None, {
                    "success": False,
                    "error_code": "extension_not_connected",
                    "error_message": (
                        "ZotPilot Connector has not sent a heartbeat. "
                        "Ensure it is installed and Chrome is open."
                    ),
                }
        return None, {"success": False, "error": f"HTTP {exc.code}: {exc.reason}"}
    except Exception as exc:
        return None, {"success": False, "error": f"Failed to enqueue: {exc}"}


def poll_single_save_result(
    bridge_url: str,
    request_id: str,
    timeout_s: float = 60.0,
    *,
    sleep_fn=time.sleep,
    monotonic_fn=time.monotonic,
) -> dict[str, Any]:
    """Poll one bridge save request until it completes or times out."""
    deadline = monotonic_fn() + timeout_s
    while monotonic_fn() < deadline:
        sleep_fn(2)
        try:
            response = urllib.request.urlopen(f"{bridge_url}/result/{request_id}", timeout=5)
            if response.status == 200:
                return dict(json.loads(response.read()))
        except Exception:
            pass
    return {
        "success": False, "status": "timeout_likely_saved",
        "error": (
            f"Timeout ({int(timeout_s)}s) — the paper was likely saved but "
            "confirmation was not received in time. Check Zotero before retrying."
        ),
    }


# ---------------------------------------------------------------------------
# Connector availability + preflight helpers
# ---------------------------------------------------------------------------

def check_connector_availability(
    connector_candidates: list[dict],
    default_port: int,
    bridge_server_cls,
) -> tuple[bool, str | None, float | None]:
    """Check bridge + extension availability for connector candidates.

    Returns (extension_connected, detail_error, last_seen_s).
    """
    bridge_running = bridge_server_cls.is_running(default_port)
    if not bridge_running:
        try:
            bridge_server_cls.auto_start(default_port)
            bridge_running = True
        except Exception:
            bridge_running = False

    extension_connected = False
    last_seen_s: float | None = None
    if bridge_running:
        ext_status = wait_for_extension(f"http://127.0.0.1:{default_port}")
        extension_connected = bool(ext_status.get("extension_connected"))
        last_seen_s = ext_status.get("extension_last_seen_s")

    if extension_connected:
        return True, None, None

    if not bridge_running:
        detail = (
            "ZotPilot bridge could not be started. "
            "Run 'zotpilot bridge' manually or check the logs."
        )
    elif last_seen_s is not None:
        detail = (
            f"ZotPilot Connector last sent a heartbeat {last_seen_s:.0f}s ago "
            "(stale). Make sure Chrome is open and the ZotPilot Connector "
            "extension is enabled, then retry ingest_papers."
        )
    else:
        detail = (
            "ZotPilot Connector has not connected to the bridge. "
            "Make sure Chrome is open and the ZotPilot Connector extension "
            "is installed and enabled, then retry ingest_papers."
        )
    return False, detail, last_seen_s


def run_preflight_check(
    connector_candidates: list[dict],
    default_port: int,
    bridge_server_cls,
    _logger,
    *,
    sleep_fn=time.sleep,
    monotonic_fn=time.monotonic,
) -> tuple[list[dict], list[dict], dict | None, list[dict]]:
    """Run preflight on connector candidate URLs.

    Returns (updated_connector_candidates, preflight_failures, blocking_decision_dict, blocked_publishers_details).
    """
    urls_to_save = [c["url"] for c in connector_candidates]
    preflight_report = preflight_urls(
        urls_to_save, sample_size=len(urls_to_save),
        default_port=default_port, bridge_server_cls=bridge_server_cls,
        _logger=_logger, sleep_fn=sleep_fn, monotonic_fn=monotonic_fn,
    )

    if preflight_report.get("all_clear", False):
        return connector_candidates, [], None, []

    failures: list[dict] = []
    blocked_domains: set[str] = set()

    for blocked in preflight_report.get("blocked", []):
        blocked_url = blocked.get("url") or ""
        blocked_domains.add(extract_publisher_domain(blocked_url))
        failures.append({
            "url": blocked_url, "status": "failed",
            "error_code": blocked.get("error_code") or "anti_bot_detected",
            "error": (
                blocked.get("error")
                or "Anti-bot protection detected. "
                "Please complete browser verification in Chrome, then retry."
            ),
        })

    for error in preflight_report.get("errors", []):
        error_url = error.get("url") or ""
        # Block on anti-bot, timeout, and subscription errors.
        # Generic failures (preflight_failed) are logged but not blocking.
        error_code = error.get("error_code") or "preflight_failed"
        if error_code in ("anti_bot_detected", "preflight_timeout", "subscription_required"):
            blocked_domains.add(extract_publisher_domain(error_url))
        else:
            _logger.debug(
                "Preflight non-blocking error for %s: %s (%s)",
                error_url, error.get("error"), error_code,
            )
        failures.append({
            "url": error_url, "status": "failed",
            "error_code": error_code,
            "error": error.get("error") or "preflight failed",
        })

    remaining = [
        c for c in connector_candidates
        if extract_publisher_domain(c["url"]) not in blocked_domains
    ]
    dropped = len(connector_candidates) - len(remaining)
    if dropped:
        _logger.info(
            "Preflight: dropped %d connector candidate(s) from %d blocked domain(s); %d remain.",
            dropped, len(blocked_domains), len(remaining),
        )

    if blocked_domains:
        blocking_dict = {
            "decision_id": "preflight_blocked",
            "description": (
                "Preflight detected anti-bot protection (CAPTCHA / Cloudflare / login). "
                "User must complete browser verification in Chrome, then retry."
            ),
            "item_keys": [],
        }
        # Build blocked publishers details
        blocked_publishers_details = []
        for domain in blocked_domains:
            domain_urls = [
                f["url"] for f in failures
                if extract_publisher_domain(f.get("url", "")) == domain
            ]
            blocked_publishers_details.append({
                "publisher": domain,
                "sample_urls": domain_urls[:3],
                "error_code": "anti_bot_detected",
                "total_affected": len(domain_urls),
            })
        return remaining, failures, blocking_dict, blocked_publishers_details

    return remaining, failures, None, []


# ---------------------------------------------------------------------------
# Local Zotero API helpers
# ---------------------------------------------------------------------------

def resolve_doi_to_landing_url(doi: str) -> str | None:
    """Resolve DOI to publisher landing page via doi.org redirect."""
    from .search import normalize_landing_url

    try:
        response = httpx.head(
            f"https://doi.org/{doi}",
            follow_redirects=False,
            timeout=10.0,
        )
        if response.status_code in (301, 302, 303, 307, 308):
            url = response.headers.get("location")
            if url:
                return normalize_landing_url(url)
    except Exception as exc:
        logger.debug("DOI resolution failed for %s: %s", doi, exc)
    return None


def resolve_dois_concurrent(dois: list[str]) -> dict[str, str | None]:
    """Resolve multiple DOIs concurrently."""
    if not dois:
        return {}
    results: dict[str, str | None] = {}
    with ThreadPoolExecutor(max_workers=min(len(dois), 10)) as pool:
        futures = {pool.submit(resolve_doi_to_landing_url, doi): doi for doi in dois}
        for future in as_completed(futures):
            doi = futures[future]
            try:
                results[doi] = future.result()
            except Exception:
                results[doi] = None
    return results


# NOTE: route_via_local_api() was removed 2026-04-11.
# It attempted to PATCH /api/users/0/items/<key> via the Zotero Desktop local
# HTTP API to add items to collections post-save. That endpoint returns
# 501 Not Implemented (verified against Zotero 7.x) — the local API is
# effectively read-only for items. All collection / tag routing now goes
# through apply_collection_tag_routing() which uses pyzotero Web API
# (subject to Desktop sync lag, but actually works).


def discover_item_via_local_api(url: str, title: str | None) -> str | None:
    """Try to discover a newly saved item via Zotero Desktop local API."""
    if not title:
        return None
    try:
        search_url = (
            f"{_ZOTERO_LOCAL_API_ITEMS_URL}/top?format=json&limit=5"
            f"&q={urllib.parse.quote(title[:50])}"
            f"&qmode=titleCreatorYear&sort=dateAdded&direction=desc"
        )
        req = urllib.request.Request(
            search_url,
            headers={"Accept": "application/json", "Zotero-Allowed-Request": "1"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            items = json.loads(resp.read())
        if len(items) == 1:
            key = items[0].get("key")
            return str(key) if key is not None else None
        if len(items) > 1:
            logger.debug(
                "Local API discovery: ambiguous match (%d items) for '%s'",
                len(items), title[:50],
            )
    except Exception:
        return None
    return None


def discover_item_via_web_api(
    url: str, title: str | None, writer, writer_lock
) -> str | None:
    """Fallback item-key discovery via Zotero Web API."""
    if not title:
        return None
    try:
        with writer_lock:
            keys = writer.find_items_by_url_and_title(url, title, window_s=120)
        if len(keys) == 1:
            return str(keys[0])
    except Exception:
        return None
    return None


def save_via_api(
    candidate: dict,
    resolved_collection_key: str | None,
    tags: list[str] | None,
    batch,
    writer,
    writer_lock: threading.Lock,
    logger=None,
) -> dict:
    """Save a single paper via API (CrossRef/arXiv + pyzotero), bypassing Connector."""
    from fastmcp.exceptions import ToolError

    from ...state import _get_resolver
    from .search import normalize_doi

    if logger is None:
        logger = logging.getLogger(__name__)

    paper = candidate["paper"]
    idx = candidate["_index"]

    doi = paper.get("doi")
    arxiv_id = paper.get("arxiv_id")
    landing_page_url = paper.get("landing_page_url")

    normalized_doi = normalize_doi(doi)
    arxiv_doi = normalize_doi(f"10.48550/arxiv.{arxiv_id}") if arxiv_id else None
    if not normalized_doi:
        normalized_doi = arxiv_doi

    if arxiv_id:
        identifier = f"arxiv:{arxiv_id}"
    elif normalized_doi:
        identifier = normalized_doi
    elif landing_page_url:
        identifier = landing_page_url
    else:
        batch.update_item(idx, status="failed", error="no usable identifier for API resolution")
        return {"success": False, "error": "no usable identifier"}

    try:
        resolver = _get_resolver()
        metadata = resolver.resolve(identifier)

        if not metadata.abstract:
            abstract_snippet = paper.get("abstract_snippet") or paper.get("abstract")
            if abstract_snippet:
                metadata.abstract = abstract_snippet

        with writer_lock:
            collection_keys = [resolved_collection_key] if resolved_collection_key else None
            result = writer.create_item_from_metadata(
                metadata, collection_keys=collection_keys, tags=tags,
            )

        item_key = None
        if result and "successful" in result:
            for value in result["successful"].values():
                item_key = value.get("key") or value.get("data", {}).get("key")
                if item_key:
                    break

        if not item_key:
            raise ToolError("create_item_from_metadata returned no item key")

        try:
            with writer_lock:
                attach_status = writer.try_attach_oa_pdf(
                    item_key,
                    doi=metadata.doi,
                    oa_url=paper.get("oa_url") or metadata.oa_url,
                    arxiv_id=metadata.arxiv_id,
                )
        except Exception as attach_exc:
            logger.debug("PDF attach best-effort failed for %s: %s", item_key, attach_exc)
            attach_status = "attach_failed"

        has_pdf = attach_status == "attached"

        batch.update_item(
            idx, status="saved", item_key=item_key, title=metadata.title,
        )
        if item_key:
            _cleanup_publisher_tags(item_key, landing_page_url or "", writer, logger)
        return {"success": True, "item_key": item_key, "title": metadata.title,
                "pdf": has_pdf, "error": None}

    except Exception as exc:
        logger.warning("API save failed for index %d: %s", idx, exc)
        batch.update_item(idx, status="failed", error=str(exc))
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# v0.5.0 new: validation and synchronous save+verify
# ---------------------------------------------------------------------------

def _fetch_item_via_local_api(
    item_key: str,
    *,
    timeout_s: float = 5.0,
    max_retries: int = 3,
    _logger=None,
) -> dict | None:
    """Fetch item data from local Zotero Desktop HTTP API (port 23119).

    Local API is instant (no Web API sync lag). Retries briefly for cases
    where Zotero is still flushing the new item to its local index.

    Returns the 'data' dict from the item payload, or None on failure.
    """
    if _logger is None:
        _logger = logger
    url = f"{_ZOTERO_LOCAL_API_ITEMS_URL}/{item_key}"
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(
                url,
                headers={"Accept": "application/json", "Zotero-Allowed-Request": "1"},
            )
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                payload = json.loads(resp.read())
            # Zotero local API returns either {"data": {...}} or the data directly
            if isinstance(payload, dict):
                return cast(dict[Any, Any], payload.get("data", payload))
            return None
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and attempt < max_retries - 1:
                time.sleep(1.0 * (attempt + 1))  # small backoff for flush lag
                continue
            _logger.debug("local API fetch %s: HTTP %s", item_key, exc.code)
            return None
        except Exception as exc:
            _logger.debug("local API fetch %s failed: %s", item_key, exc)
            return None
    return None


def validate_saved_item(
    item_key: str,
    *,
    get_writer,
    _logger=None,
) -> dict:
    """验证 Connector save 后的 item 质量。

    优先使用本地 Zotero Desktop API (port 23119) — 无同步延迟。
    仅当本地 API 完全不可用时，降级到 pyzotero Web API（有 ~10s 同步延迟）。

    返回 {"valid": bool, "item_type": str, "title": str, "reason": str | None}。
    """
    if _logger is None:
        _logger = logger

    # Primary path: local Zotero API (instant after Connector save)
    data = _fetch_item_via_local_api(item_key, _logger=_logger)
    source = "local_api"

    # Fallback: Web API (only if local API unavailable, e.g. Zotero closed)
    if data is None:
        try:
            writer = get_writer()
            item = writer._zot.item(item_key)
            data = item.get("data", {})
            source = "web_api"
        except Exception as exc:
            _logger.warning(
                "validate_saved_item: both local and web API failed for %s: %s",
                item_key, exc,
            )
            return {"valid": False, "item_type": "unknown", "title": "",
                    "reason": f"validation_error:{exc}"}

    item_type = data.get("itemType", "unknown")
    title = data.get("title", "")
    _logger.debug("validate_saved_item %s via %s: type=%s title=%r",
                  item_key, source, item_type, title[:50])

    if item_type not in VALID_ACADEMIC_ITEM_TYPES:
        return {"valid": False, "item_type": item_type, "title": title,
                "reason": f"invalid_item_type:{item_type}"}

    if title.startswith(("http://", "https://")):
        return {"valid": False, "item_type": item_type, "title": title,
                "reason": "title_is_url"}

    if title.lower().strip() == "snapshot":
        return {"valid": False, "item_type": item_type, "title": title,
                "reason": "title_is_snapshot"}

    if looks_like_error_page_title(title, item_key):
        return {"valid": False, "item_type": item_type, "title": title,
                "reason": "error_page_title"}

    return {"valid": True, "item_type": item_type, "title": title, "reason": None}


def delete_item_safe(
    item_key: str,
    *,
    get_writer,
    _logger=None,
) -> bool:
    """Best-effort 删除 Zotero item via Web API with sync-lag retry.

    Zotero Desktop local API (port 23119) does NOT support DELETE (501).
    The only reliable path is pyzotero Web API, which requires the item
    to have synced from Desktop → api.zotero.org. Sync typically takes
    ~10s. Retries with exponential backoff up to ~30s total.
    """
    if _logger is None:
        _logger = logger

    delays = (0, 5, 10, 15)  # 0s, 5s, 15s, 30s cumulative
    last_exc = None
    for attempt, delay in enumerate(delays):
        if delay:
            _logger.debug("Delete retry %d/%d for %s (waiting %ds for sync)",
                          attempt + 1, len(delays), item_key, delay)
            time.sleep(delay)
        try:
            writer = get_writer()
            item = writer._zot.item(item_key)
            writer._zot.delete_item(item)
            _logger.info("Deleted item %s (attempt %d)", item_key, attempt + 1)
            return True
        except Exception as exc:
            last_exc = exc
            continue

    _logger.warning(
        "Failed to delete item %s after %d attempts: %s. "
        "Item may remain in library and require manual cleanup.",
        item_key, len(delays), last_exc,
    )
    return False


def _check_has_pdf_via_local_api(
    item_key: str,
    *,
    timeout_s: float = 5.0,
    _logger=None,
) -> bool | None:
    """Check PDF attachment via local Zotero Desktop API (port 23119).

    Returns True/False when local API answers, None when local API is
    unreachable (caller should fall back to Web API).
    """
    if _logger is None:
        _logger = logger
    url = f"{_ZOTERO_LOCAL_API_ITEMS_URL}/{item_key}/children"
    try:
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "Zotero-Allowed-Request": "1"},
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # Item exists locally but has no children yet — treat as False,
            # retry handled by caller.
            return False
        _logger.debug("local API children fetch %s: HTTP %s", item_key, exc.code)
        return None
    except (urllib.error.URLError, ConnectionRefusedError, OSError) as exc:
        _logger.debug("local API unavailable for %s: %s", item_key, exc)
        return None
    except Exception as exc:
        _logger.debug("local API children fetch %s failed: %s", item_key, exc)
        return None

    # Zotero local API returns a list of child item payloads
    children = payload if isinstance(payload, list) else payload.get("results", [])
    for child in children:
        data = child.get("data", child) if isinstance(child, dict) else {}
        if data.get("contentType") == "application/pdf":
            return True
        # Some attachment types report contentType on the top-level dict
        if child.get("contentType") == "application/pdf":
            return True
    return False


def check_pdf_status(
    item_key: str,
    *,
    get_writer,
    timeout_s: float = 30.0,
    _logger=None,
) -> str:
    """检查 item 是否有 PDF 附件。

    优先使用本地 Zotero Desktop API（无同步延迟），仅在本地 API 完全不可达
    时降级到 pyzotero Web API（有 ~10-60s 同步延迟）。

    返回: "attached" | "none" | "pending" | "check_failed"
    """
    if _logger is None:
        _logger = logger

    deadline = time.monotonic() + timeout_s
    local_api_unavailable = False
    while time.monotonic() < deadline:
        # Primary path: local API (instant for Connector-saved PDFs)
        if not local_api_unavailable:
            local_result = _check_has_pdf_via_local_api(item_key, _logger=_logger)
            if local_result is True:
                return "attached"
            if local_result is None:
                local_api_unavailable = True  # stop trying local on this call

        # Fallback: Web API (only when local unreachable, e.g. Zotero closed)
        if local_api_unavailable:
            try:
                writer = get_writer()
                if writer.check_has_pdf(item_key):
                    return "attached"
            except Exception:
                return "check_failed"

        remaining = deadline - time.monotonic()
        if remaining < 2:
            break
        time.sleep(min(2.0, remaining))

    return "none"


def save_single_and_verify(
    url: str,
    doi: str | None,
    title: str | None,
    *,
    arxiv_id: str | None = None,
    collection_key: str | None,
    tags: list[str] | None,
    bridge_url: str,
    get_writer,
    writer_lock,
    _logger=None,
    _retry_count: int = 0,
) -> dict:
    """逐条 save + 即时验证。v0.5.0 入库的核心函数。

    流程：
    1. enqueue_save_request (Connector)
    2. poll_single_save_result (最多 60s)
    3. 如果 success：validate_saved_item
       a. valid → apply collection/tag routing → check PDF → return saved
       b. invalid → delete_item → save_via_api (DOI fallback) → return
    4. 如果 anti-bot → return blocked
    5. 其他失败 → 尝试 DOI API fallback → return

    返回 dict:
      status: "saved_with_pdf" | "saved_metadata_only" | "blocked" | "failed"
      item_key: str | None
      has_pdf: bool
      title: str
      method: "connector" | "api_fallback" | "api_direct"
      action_required: str | None
      warning: str | None
    """
    if _logger is None:
        _logger = logger

    # Step 1: Connector save
    request_id, enqueue_error = enqueue_save_request(
        bridge_url, url, collection_key=collection_key, tags=tags,
    )
    if enqueue_error:
        error_code = enqueue_error.get("error_code", "")
        if error_code == "extension_not_connected":
            # Connector offline — try DOI API fallback if we have a DOI
            if doi:
                _logger.info("Connector offline for %s, trying DOI API fallback", url)
                return _doi_api_fallback(
                    doi, title, arxiv_id=arxiv_id,
                    collection_key=collection_key, tags=tags,
                    get_writer=get_writer, writer_lock=writer_lock, _logger=_logger,
                )
            return {"status": "failed", "method": "connector",
                    "error": "connector_offline", "item_key": None, "has_pdf": False,
                    "title": title or "", "action_required": None, "warning": None}
        return {"status": "failed", "method": "connector",
                "error": str(enqueue_error), "item_key": None, "has_pdf": False,
                "title": title or "", "action_required": None, "warning": None}
    assert request_id is not None

    # Step 2: Poll result
    save_result = poll_single_save_result(bridge_url, request_id, timeout_s=60.0)

    # Step 2.5: Timeout recovery.
    # Slow publishers (AIP, Cloudflare-protected sites, JS-heavy pages) can
    # push the Connector save past our 60s poll window. The browser translator
    # often completes the save anyway — we just miss the bridge confirmation.
    # Falling straight to _doi_api_fallback here would lose the Connector's
    # browser-session PDF and degrade to a metadata-only record. Instead,
    # try to discover the newly-saved item by URL+title and upgrade the
    # save_result so the validation path can take it from here.
    if (not save_result.get("success")
            and save_result.get("status") == "timeout_likely_saved"):
        _logger.info("Connector poll timed out for %s — attempting discovery", url)
        discovered_key = discover_item_via_local_api(url, title)
        if not discovered_key:
            time.sleep(5.0)  # give Zotero a few more seconds to finalize
            discovered_key = discover_item_via_local_api(url, title)
        if discovered_key:
            _logger.info(
                "Recovered timed-out Connector save: item_key=%s for %s",
                discovered_key, url,
            )
            save_result = {"success": True, "item_key": discovered_key}

    if not save_result.get("success"):
        error_text = save_result.get("error", "unknown")
        error_code = save_result.get("error_code", "")
        result_title = save_result.get("title", "")
        if looks_like_error_page_title(result_title, save_result.get("item_key")):
            ik = save_result.get("item_key")
            if ik:
                deleted = delete_item_safe(ik, get_writer=get_writer, _logger=_logger)
                if not deleted:
                    _logger.warning("Failed to delete anti-bot item %s — manual cleanup may be needed", ik)
            return {"status": "blocked", "method": "connector",
                    "error": "anti_bot_detected", "item_key": None, "has_pdf": False,
                    "title": title or "",
                    "action_required": "用户需在浏览器中完成验证，然后重试",
                    "warning": None}
        # Cold-start retry: Connector-side already retried _waitForReady once
        # (5s delay + 15s wait). If still no_translator, one more attempt with a
        # fresh tab — DNS/TLS cache is now warm and JS hydration should be faster.
        if error_code == "no_translator" and _retry_count == 0:
            _logger.info(
                "no_translator after Connector retry for %s — retrying with fresh tab",
                url,
            )
            return save_single_and_verify(
                url, doi, title, arxiv_id=arxiv_id,
                collection_key=collection_key, tags=tags,
                bridge_url=bridge_url, get_writer=get_writer,
                writer_lock=writer_lock, _logger=_logger,
                _retry_count=1,
            )
        if doi:
            _logger.info("Connector failed for %s, trying DOI API fallback", url)
            return _doi_api_fallback(
                doi, title, arxiv_id=arxiv_id,
                collection_key=collection_key, tags=tags,
                get_writer=get_writer, writer_lock=writer_lock, _logger=_logger,
            )
        return {"status": "failed", "method": "connector",
                "error": error_text, "item_key": None, "has_pdf": False,
                "title": title or "", "action_required": None, "warning": None}

    # Step 3: Connector reported success — VERIFY
    item_key = save_result.get("item_key")
    if not item_key:
        item_key = discover_item_via_local_api(url, title)

    if not item_key:
        _logger.warning("Connector success but no item_key for %s", url)
        if doi:
            return _doi_api_fallback(
                doi, title, collection_key=collection_key, tags=tags,
                get_writer=get_writer, writer_lock=writer_lock, _logger=_logger,
            )
        return {"status": "failed", "method": "connector",
                "error": "item_not_found_after_save", "item_key": None, "has_pdf": False,
                "title": title or "", "action_required": None,
                "warning": "Connector reported success but item not found."}

    # Step 4: Validate the saved item
    validation = validate_saved_item(item_key, get_writer=get_writer, _logger=_logger)

    if not validation["valid"]:
        reason = validation.get("reason", "")
        # Cold-start retry for webpage saves: background tab JS throttling
        # caused the translator to miss citation_* meta tags and fall back to
        # a generic webpage save.  Retry with a fresh tab (warm cache) before
        # falling back to the API-only path which loses Connector PDF access.
        if "webpage" in reason and _retry_count == 0:
            _logger.info(
                "Connector saved webpage for %s (item %s) — retrying with fresh tab",
                url, item_key,
            )
            # Best-effort delete; don't block long — Zotero may auto-cleanup
            deleted = delete_item_safe(item_key, get_writer=get_writer, _logger=_logger)
            if not deleted:
                _logger.warning("Failed to delete invalid webpage item %s — manual cleanup may be needed", item_key)
            return save_single_and_verify(
                url, doi, title, arxiv_id=arxiv_id,
                collection_key=collection_key, tags=tags,
                bridge_url=bridge_url, get_writer=get_writer,
                writer_lock=writer_lock, _logger=_logger,
                _retry_count=1,
            )
        _logger.warning(
            "Connector item %s invalid: %s — deleting and falling back to API",
            item_key, reason,
        )
        deleted = delete_item_safe(item_key, get_writer=get_writer, _logger=_logger)
        delete_warn = None
        if not deleted:
            delete_warn = f"Failed to delete invalid item {item_key}. Manual cleanup may be needed."
            _logger.warning(delete_warn)
        if doi:
            return _doi_api_fallback(
                doi, title, arxiv_id=arxiv_id,
                collection_key=collection_key, tags=tags,
                get_writer=get_writer, writer_lock=writer_lock, _logger=_logger,
            )
        warning_text = f"Connector created invalid item ({validation['reason']}), deleted."
        if delete_warn:
            warning_text += " " + delete_warn
        return {"status": "failed", "method": "connector",
                "error": f"invalid_item:{validation['reason']}", "item_key": None,
                "has_pdf": False, "title": title or "", "action_required": None,
                "warning": warning_text}

    # Step 5: Valid item — apply routing and check PDF
    real_title = validation["title"]
    _cleanup_publisher_tags(item_key, url, get_writer(), _logger)

    # Collection/tag routing via pyzotero Web API.
    # NOTE: Zotero Desktop local API (port 23119) rejects PATCH with
    # 501 Not Implemented, so we cannot route locally. pyzotero Web API
    # works reliably (subject to ~10-30s Desktop sync lag — routing is
    # visible in Zotero UI after the sync completes).
    if (collection_key or tags) and not save_result.get("routing_applied"):
        from ...state import _get_config
        try:
            routing_error = apply_collection_tag_routing(
                item_key, collection_key, tags,
                get_writer(), _get_config,
            )
            if routing_error:
                _logger.warning(
                    "Collection/tag routing for %s: %s", item_key, routing_error,
                )
        except Exception as exc:
            _logger.warning("Routing failed for %s: %s", item_key, exc)

    connector_pdf_confirmed = bool(save_result.get("pdf_connector_confirmed"))
    connector_pdf_failed = bool(save_result.get("pdf_failed"))
    if connector_pdf_confirmed:
        _logger.debug(
            "Connector reports PDF attached for %s — verifying via local API",
            item_key,
        )
    elif connector_pdf_failed:
        _logger.debug(
            "Connector reports PDF failed for %s — will try OA fallback",
            item_key,
        )

    # Always verify via local API — connector signals are hints, not ground
    # truth.  Springer embedded-PDF pages can trigger pdf_connector_confirmed
    # without actually attaching a file.  Use a shorter poll window when the
    # connector claims success (PDF should appear quickly if it really saved).
    pdf_status = check_pdf_status(
        item_key, get_writer=get_writer,
        timeout_s=10.0 if connector_pdf_confirmed else 30.0,
        _logger=_logger,
    )

    # OA PDF fallback: when the Connector's translator didn't attach a PDF
    # (paywalled with no session cookies, translator bug, JS-rendered page,
    # preflight-accessible but PDF URL hidden, etc.), try the Unpaywall /
    # arXiv fallback used by the API path. We resolve the DOI through
    # IdentifierResolver first so we get arxiv_id + oa_url hints — passing
    # only the DOI to try_attach_oa_pdf misses papers whose published
    # version has no Unpaywall record but whose arXiv preprint is free
    # (e.g. IJCV-published CLIP-Adapter resolves only via its arXiv id).
    if pdf_status != "attached" and (doi or arxiv_id):
        try:
            from ...state import _get_resolver
            resolver = _get_resolver()
            # Resolve via arXiv when arxiv_id is known: CrossRef._resolve_doi
            # hard-codes arxiv_id=None, so resolving a journal DOI never yields
            # an arXiv PDF URL even when a preprint exists.
            resolve_id = f"arxiv:{arxiv_id}" if arxiv_id else doi
            metadata = resolver.resolve(resolve_id)
            effective_arxiv = metadata.arxiv_id or arxiv_id
            with writer_lock:
                attach_status = get_writer().try_attach_oa_pdf(
                    item_key,
                    doi=metadata.doi or doi,
                    oa_url=metadata.oa_url,
                    arxiv_id=effective_arxiv,
                )
            if attach_status == "attached":
                _logger.info(
                    "OA PDF fallback attached to %s (arxiv=%s oa_url=%s)",
                    item_key, effective_arxiv, metadata.oa_url,
                )
                pdf_status = "attached"
            else:
                _logger.debug(
                    "OA PDF fallback for %s: %s (arxiv=%s oa_url=%s)",
                    item_key, attach_status, effective_arxiv, metadata.oa_url,
                )
        except Exception as exc:
            _logger.debug("OA PDF fallback failed for %s: %s", item_key, exc)

    status = "saved_with_pdf" if pdf_status == "attached" else "saved_metadata_only"
    warning = None if pdf_status == "attached" else "PDF not attached (paywall or download pending)"

    return {"status": status, "method": "connector", "item_key": item_key,
            "has_pdf": pdf_status == "attached", "title": real_title,
            "action_required": None, "warning": warning}


def _doi_api_fallback(
    doi: str,
    title: str | None,
    *,
    arxiv_id: str | None = None,
    oa_url: str | None = None,
    collection_key: str | None,
    tags: list[str] | None,
    get_writer,
    writer_lock,
    _logger=None,
) -> dict:
    """DOI API fallback：通过 CrossRef/arXiv 元数据 + pyzotero 创建条目。

    arxiv_id — when provided, save_via_api resolves via the arXiv API instead
    of CrossRef (avoids CrossRef returning arxiv_id=None for journal DOIs) and
    try_attach_oa_pdf can download the arXiv PDF directly.
    """
    if _logger is None:
        _logger = logger


    candidate = {
        "paper": {
            "doi": doi,
            "title": title or "",
            "arxiv_id": arxiv_id,
            "oa_url": oa_url,
        },
        "_index": 0,
    }
    result = save_via_api(
        candidate, collection_key, tags,
        _FakeBatch(), get_writer(), writer_lock or threading.Lock(),
        logger=_logger,
    )
    if result.get("success"):
        return {"status": "saved_metadata_only", "method": "api_fallback",
                "item_key": result.get("item_key"), "has_pdf": bool(result.get("pdf")),
                "title": result.get("title", title or ""),
                "action_required": None,
                "warning": "Created via DOI API (Connector failed). PDF may be missing for paywalled papers."}
    return {"status": "failed", "method": "api_fallback",
            "error": result.get("error", "api_save_failed"), "item_key": None,
            "has_pdf": False, "title": title or "",
            "action_required": None, "warning": None}

"""Bridge-facing helpers for ingestion connector workflows."""
from __future__ import annotations

import json
import logging as _logging
import threading as _threading
import time
import urllib.error
import urllib.parse as _urllib_parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor
from concurrent.futures import as_completed as _as_completed
from typing import Any, Callable
from urllib.parse import urlparse

import httpx as _httpx

DISCOVERY_BACKOFF_DELAYS = [2.0, 4.0, 8.0, 16.0, 32.0]
ITEM_DISCOVERY_WINDOW_S = 120
PDF_VERIFICATION_WINDOW_S = 15.0
SAVE_RESULT_POLL_TIMEOUT_S = 150.0
SAVE_RESULT_POLL_OVERALL_TIMEOUT_S = 600.0
SAVE_RESULT_POLL_PER_URL_BUDGET_S = 75.0
SAVE_RESULT_POLL_OVERALL_GRACE_S = 120.0
ROUTING_RETRY_DELAYS_S = [0.0, 2.0, 5.0]
_PUBLISHER_TAG_SOURCES = {
    "arxiv.org",
    "biorxiv.org",
    "medrxiv.org",
    "sciencedirect.com",
    "elsevier.com",
    "springer.com",
    "springerlink.com",
    "nature.com",
    "wiley.com",
    "onlinelibrary.wiley.com",
    "acs.org",
    "pubs.acs.org",
    "iop.org",
    "iopscience.iop.org",
    "mdpi.com",
    "cambridge.org",
    "tandfonline.com",
    "aip.org",
    "aip.scitation.org",
    "rsc.org",
    "pnas.org",
    "ieee.org",
    "ieeexplore.ieee.org",
    "acm.org",
    "dl.acm.org",
}

ANTI_BOT_TITLE_PATTERNS = [
    "just a moment",
    "请稍候",
    "请稍等",
    "verify you are human",
    "access denied",
    "please verify",
    "robot check",
    "cloudflare",
    "security check",
    "captcha",
    "checking your browser",
    "one more step",
    "page not found",
    "404",
    "not found",
]

ERROR_PAGE_TITLE_PATTERNS = [
    "page not found",
    "404 -",
    "404 ",
    "access denied",
    "subscription required",
    "unavailable -",
]

TRANSLATOR_FALLBACK_SUFFIXES = [
    " | cambridge core",
    " | springerlink",
    " | sciencedirect",
    " | wiley online library",
    " | taylor & francis",
    " | oxford academic",
    " | jstor",
    " | aip publishing",
    " | acs publications",
    " | ieee xplore",
    " | sage journals",
    " | mdpi",
    " | frontiers",
    " | pnas",
    " | nature",
    " | annual reviews",
]

GENERIC_SITE_ONLY_TITLE_PATTERNS = [
    "| arxiv",
    "| arxiv.org",
    "| biorxiv",
    "| medrxiv",
]


def compute_save_result_poll_timeout_s(batch_size: int) -> float:
    """Scale poll timeout with batch size to cover connector's sequential saves."""
    if batch_size <= 0:
        return SAVE_RESULT_POLL_TIMEOUT_S
    return max(SAVE_RESULT_POLL_TIMEOUT_S, batch_size * SAVE_RESULT_POLL_PER_URL_BUDGET_S)


def compute_save_result_poll_overall_timeout_s(batch_size: int) -> float:
    """Compute the overall poll timeout for a batch of ``batch_size`` URLs.

    Because the connector processes URLs sequentially (one at a time), the
    overall wall-clock budget must cover the sum of all per-URL deadlines, not
    just a single deadline plus a fixed grace window.  The formula is:

        overall = max(600, batch_size * per_url_timeout + grace)

    This ensures the last URL in the queue still has a full per-URL window
    even if earlier URLs consumed their full budget.
    """
    n = max(batch_size, 1)
    per_url_timeout = compute_save_result_poll_timeout_s(batch_size)
    return max(SAVE_RESULT_POLL_OVERALL_TIMEOUT_S, n * per_url_timeout + SAVE_RESULT_POLL_OVERALL_GRACE_S)


def looks_like_error_page_title(raw_title: str, item_key: str | None) -> bool:
    """Detect post-save error pages by title."""
    title = raw_title.strip().lower()
    if not title:
        return False
    if any(title.startswith(pattern) for pattern in ERROR_PAGE_TITLE_PATTERNS):
        return True
    if item_key:
        return False
    return any(title.endswith(pattern) for pattern in GENERIC_SITE_ONLY_TITLE_PATTERNS)


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


def extract_publisher_domain(url: str) -> str:
    """Normalize a URL to a publisher-ish domain for preflight sampling."""
    hostname = (urlparse(url).hostname or "").lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return hostname or url


def _should_clean_publisher_tags(url: str) -> bool:
    """Check if publisher tags should be cleaned for this URL.

    Default to True for all URLs; publisher auto-tags are treated as noise.
    """
    if not url:
        return True
    hostname = (urlparse(url).hostname or "").lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return True if hostname else True


def _cleanup_publisher_tags(item_key: str | None, url: str, writer, logger) -> None:
    """Clear publisher-injected tags after a successful save."""
    if not item_key or not _should_clean_publisher_tags(url):
        return
    try:
        current_tags = []
        if hasattr(writer, "_zot"):
            item = writer._zot.item(item_key)
            current_tags = list((item.get("data") or {}).get("tags") or [])
        writer.set_item_tags(item_key, [])
        logger.info("Cleared %d publisher auto-tags for %s", len(current_tags), item_key)
    except Exception as exc:
        logger.warning("Failed to clean publisher tags for %s: %s", item_key, exc)


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
    """Poll ``/status`` until the extension reports connected, or ``timeout_s``.

    When the bridge server is freshly started (auto_start), its heartbeat
    table is empty and ``extension_connected`` will initially be False.
    The browser extension normally sends a heartbeat every 10s, so waiting
    up to ~12s lets a real running extension reconnect before we declare
    it offline.

    Returns the last ``/status`` payload seen.  Caller inspects
    ``extension_connected``.
    """
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
    """Return a structured error code for a failed preflight result.

    Classifies based on the connector-reported status, title, and any error
    message fields.  This allows callers to distinguish between anti-bot pages
    (user must complete CAPTCHA), subscription walls (user needs access), and
    transient failures.

    Priority order:
      1. ``anti_bot_detected`` — connector explicitly flagged it, OR title/error
         contains Cloudflare / CAPTCHA markers.
      2. ``subscription_required`` — title/error contains paywall markers.
      3. ``preflight_timeout`` — connector reported a timeout.
      4. ``preflight_failed`` — everything else.
    """
    status = result.get("status", "")
    title = (result.get("title") or "").lower()
    error_msg = (result.get("error") or result.get("error_message") or "").lower()
    combined = f"{title} {error_msg}"

    if status == "anti_bot_detected":
        return "anti_bot_detected"

    _anti_bot_markers = (
        "cloudflare",
        "checking your browser",
        "captcha",
        "verify you are human",
        "just a moment",
        "please verify",
        "robot check",
        "security check",
        "access denied",
    )
    if any(marker in combined for marker in _anti_bot_markers):
        return "anti_bot_detected"

    _subscription_markers = (
        "subscribe",
        "purchase access",
        "full access",
        "get access",
        "sign in to access",
        "login to access",
        "log in to access",
        "paywall",
        "subscription required",
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
    logger,
    *,
    sleep_fn=time.sleep,
    monotonic_fn=time.monotonic,
) -> dict:
    """Probe URL accessibility via connector tabs before attempting saves."""
    if not urls:
        return {
            "checked": 0,
            "accessible": [],
            "blocked": [],
            "skipped": [],
            "errors": [],
            "all_clear": True,
        }

    sample, skipped_urls = sample_preflight_urls(urls, sample_size)
    report: dict[str, Any] = {
        "checked": len(sample),
        "accessible": [],
        "blocked": [],
        "skipped": [{"url": url, "reason": "sampling"} for url in skipped_urls],
        "errors": [],
        "all_clear": True,
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
        if not wait_for_extension(
            bridge_url,
            sleep_fn=sleep_fn,
            monotonic_fn=monotonic_fn,
        ):
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
                    "status": "error",
                    "url": url,
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
                logger.debug("Preflight poll %s: %s", request_id, exc)

    for request_id, url in id_to_url.items():
        result = polled.get(request_id, {
            "status": "error",
            "url": url,
            "error": "Timeout (overall) — preflight did not complete.",
            "error_code": "preflight_timeout",
        })
        status = result.get("status")
        if status == "accessible":
            report["accessible"].append({
                "url": url,
                "title": result.get("title", ""),
                "final_url": result.get("final_url", url),
            })
        elif status == "anti_bot_detected":
            # Connector confirmed anti-bot; classify for downstream routing.
            error_code = result.get("error_code") or _classify_preflight_error_code(result)
            report["blocked"].append({
                "url": url,
                "title": result.get("title", ""),
                "final_url": result.get("final_url", url),
                "error_code": error_code,
            })
        else:
            # Connector returned an error or timed out locally.
            error_code = result.get("error_code") or _classify_preflight_error_code(result)
            error_entry = {
                "url": url,
                "error": (
                    result.get("error")
                    or result.get("error_message")
                    or "unknown preflight error"
                ),
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
    collection_key: str | None,
    tags: list[str] | None,
) -> tuple[str | None, dict | None]:
    """Enqueue one bridge save request."""
    command = {
        "action": "save",
        "url": url,
        "collection_key": collection_key,
        "tags": tags or [],
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
    timeout_s: float,
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
        "success": False,
        "status": "timeout_likely_saved",
        "error": (
            f"Timeout ({int(timeout_s)}s) — the paper was likely saved but "
            "confirmation was not received in time. Check Zotero before retrying."
        ),
    }


def poll_batch_save_results(
    bridge_url: str,
    id_to_url: dict[str, str],
    per_url_timeout_s: float,
    overall_timeout_s: float,
    apply_bridge_result_routing_fn,
    collection_key: str | None,
    tags: list[str] | None,
    logger,
    *,
    sleep_fn=time.sleep,
    monotonic_fn=time.monotonic,
) -> list[dict]:
    """Poll batch save requests with anti-bot short-circuiting.

    The connector processes URLs sequentially (``_busy = true`` while one save
    is in flight).  Assigning every request the same ``t0 + per_url_timeout_s``
    deadline would expire queue-tail URLs before the connector even starts them.

    Instead we assign FIFO-position-based deadlines:
        deadline_k = t0 + k * per_url_timeout_s   (k = 1-based position)

    This gives each URL a full per-URL window *starting from when the connector
    is likely to begin processing it*, matching the sequential save model.
    """
    polled: dict[str, dict] = {}
    pending_ids = set(id_to_url)
    # id_to_url preserves insertion order (Python 3.7+ dict).
    per_request_deadlines = {
        request_id: monotonic_fn() + k * per_url_timeout_s
        for k, request_id in enumerate(id_to_url, start=1)
    }
    overall_deadline = monotonic_fn() + overall_timeout_s
    cancel_remaining = False

    while pending_ids and monotonic_fn() < overall_deadline:
        if cancel_remaining:
            for request_id in list(pending_ids):
                polled[request_id] = {
                    "url": id_to_url[request_id],
                    "success": False,
                    "status": "pending",
                    "error": (
                        "Skipped — another URL triggered anti-bot verification. "
                        "Wait for the user to complete Chrome verification before continuing."
                    ),
                }
                pending_ids.remove(request_id)
            break

        sleep_fn(2)
        for request_id in list(pending_ids):
            url = id_to_url[request_id]
            if monotonic_fn() >= per_request_deadlines[request_id]:
                polled[request_id] = {
                    "url": url,
                    "success": False,
                    "status": "timeout_likely_saved",
                    "poll_timeout_s": int(per_url_timeout_s),
                    "error": (
                        f"Timeout ({int(per_url_timeout_s)}s) — the paper was likely saved but "
                        "confirmation was not received in time. Check Zotero before retrying."
                    ),
                }
                pending_ids.remove(request_id)
                continue

            try:
                response = urllib.request.urlopen(f"{bridge_url}/result/{request_id}", timeout=5)
                if response.status != 200:
                    continue
                result = json.loads(response.read())
                if result.get("error_code") == "anti_bot_detected":
                    logger.warning(
                        "Anti-bot page detected for %s (title: '%s'). "
                        "Please complete the verification in Chrome, then retry.",
                        url,
                        result.get("title"),
                    )
                    polled[request_id] = {
                        "url": url,
                        "success": False,
                        "anti_bot_detected": True,
                        "error": result.get("error_message") or (
                            f"Anti-bot page detected (title: '{result.get('title')}'). "
                            "Wait for the user to complete the Chrome verification "
                            "before continuing."
                        ),
                    }
                    pending_ids.remove(request_id)
                    cancel_remaining = True
                    break
                polled[request_id] = {
                    **apply_bridge_result_routing_fn(result, collection_key, tags),
                    "url": url,
                }
                pending_ids.remove(request_id)
            except Exception as exc:
                logger.debug("Poll %s: %s", request_id, exc)

    for request_id in list(pending_ids):
        polled.setdefault(
            request_id,
            {
                "url": id_to_url[request_id],
                "success": False,
                "status": "timeout_likely_saved",
                "poll_timeout_s": int(per_url_timeout_s),
                "error": (
                    f"Timeout ({int(per_url_timeout_s)}s) — the paper was likely saved but "
                    "confirmation was not received in time. Check Zotero before retrying."
                ),
            },
        )

    return [polled[request_id] for request_id in id_to_url]


def discover_saved_item_key(
    title: str,
    url: str,
    known_key: str | None,
    writer,
    window_s: int = ITEM_DISCOVERY_WINDOW_S,
    logger=None,
) -> str | None:
    """Best-effort item key discovery after connector save."""
    if known_key:
        return known_key
    if not title and not url:
        return None
    try:
        items = writer.find_items_by_url_and_title(url, title, window_s=window_s)
    except Exception as exc:
        if logger is not None:
            logger.warning("Item discovery query failed: %s", exc)
        return None
    if len(items) == 1:
        return str(items[0])
    return None


def apply_bridge_result_routing(
    result: dict,
    collection_key: str | None,
    tags: list[str] | None,
    *,
    get_config,
    get_writer,
    discover_saved_item_key_fn,
    apply_collection_tag_routing_fn,
    writer_lock,
    sleep_fn,
    logger,
) -> dict:
    """Apply collection/tag routing after a bridge save result."""
    academic_item_types = {
        "journalArticle",
        "conferencePaper",
        "preprint",
        "thesis",
        "book",
        "bookSection",
        "report",
        "magazineArticle",
        "newspaperArticle",
    }

    if not result.get("success"):
        return result

    raw_title = result.get("title", "")
    title = raw_title.lower()
    item_key = result.get("item_key")
    if looks_like_error_page_title(raw_title, item_key):
        try:
            writer = get_writer()
        except Exception:
            writer = None
        if item_key:
            if writer is not None and hasattr(writer, "delete_item"):
                try:
                    writer.delete_item(item_key)
                except Exception:
                    logger.warning("Failed to delete error-page item %s", item_key)
        elif writer is not None:
            with writer_lock:
                discovered_key = discover_saved_item_key_fn(
                    title=result.get("title", ""),
                    url=result.get("url", ""),
                    known_key=None,
                    writer=writer,
                    window_s=60,
                )
            if discovered_key:
                try:
                    writer.delete_item(discovered_key)
                except Exception:
                    logger.warning("Failed to delete discovered error-page item %s", discovered_key)
            else:
                logger.warning(
                    "Error page detected but item deletion could not be confirmed (title=%r, url=%r)",
                    raw_title,
                    result.get("url", ""),
                )
        else:
            logger.warning(
                "Error page detected but writer unavailable for cleanup (title=%r, url=%r)",
                raw_title,
                result.get("url", ""),
            )
        return {
            **result,
            "success": False,
            "error_code": "error_page_detected",
            "error": (
                f"Error page saved instead of paper (title: '{raw_title}'). "
                "The URL returned an error page. Try a different URL or add the paper manually."
            ),
        }

    if any(title.endswith(suffix) for suffix in TRANSLATOR_FALLBACK_SUFFIXES):
        return {
            **result,
            "success": False,
            "translator_fallback_detected": True,
            "error": (
                f"Translator fallback detected (title: '{result.get('title')}'). "
                "Zotero captured the webpage instead of the paper. "
                "Retry with a different URL, or manually add the paper in Zotero."
            ),
        }

    needs_routing = bool(collection_key) or bool(tags)

    def finalize_success(out: dict, routing_status: str | None = None) -> dict:
        finalized = {**out}
        if routing_status:
            finalized["routing_status"] = routing_status
        if finalized.get("item_key") and _should_clean_publisher_tags(finalized.get("url", "")):
            try:
                cleanup_writer = get_writer()
            except Exception:
                cleanup_writer = None
            if cleanup_writer is not None:
                _cleanup_publisher_tags(finalized.get("item_key"), finalized.get("url", ""), cleanup_writer, logger)
        return finalized

    connector_routing_applied = result.get("routing_applied")
    if needs_routing and connector_routing_applied is True and result.get("item_key"):
        return finalize_success({**result}, "routed_by_connector")

    if needs_routing and connector_routing_applied is False:
        if result.get("item_key"):
            logger.warning(
                "Connector routing failed (routing_warning=%s), falling back to Web API routing for %s",
                result.get("routing_warning"),
                result.get("item_key"),
            )
        else:
            logger.error("item_key discovery failed for %s — deferring to reconciliation", result.get("url"))
            return {
                **result,
                "routing_status": "routing_deferred",
                "warning": (
                    "item_key not discovered — collection/tag routing deferred to "
                    "post-batch reconciliation. Use get_ingest_status to track."
                ),
            }
    elif needs_routing and connector_routing_applied is None:
        logger.debug("routing_applied absent — old connector version, falling through to Web API routing")

    config = get_config()
    if not config.zotero_api_key:
        if needs_routing:
            return {
                **result,
                "warning": "collection_key and tags ignored — ZOTERO_API_KEY not configured",
            }
        return finalize_success(result)

    writer = get_writer()

    if not result.get("item_key"):
        item_key = None
        for delay in DISCOVERY_BACKOFF_DELAYS:
            sleep_fn(delay)
            with writer_lock:
                item_key = discover_saved_item_key_fn(
                    title=result.get("title", ""),
                    url=result.get("url", ""),
                    known_key=None,
                    writer=writer,
                    window_s=ITEM_DISCOVERY_WINDOW_S,
                )
            if item_key:
                break
    else:
        with writer_lock:
            item_key = discover_saved_item_key_fn(
                title=result.get("title", ""),
                url=result.get("url", ""),
                known_key=result.get("item_key"),
                writer=writer,
                window_s=ITEM_DISCOVERY_WINDOW_S,
            )

    out = {**result}
    if item_key:
        out["item_key"] = item_key

    if item_key:
        saved_type = writer.get_item_type(item_key)
        if saved_type and saved_type not in academic_item_types:
            logger.warning(
                "Translator fallback confirmed via Zotero: item %s saved as '%s' "
                "(expected academic type). Deleting junk item.",
                item_key,
                saved_type,
            )
            writer.delete_item(item_key)
            return {
                **result,
                "success": False,
                "translator_fallback_detected": True,
                "saved_item_type": saved_type,
                "error": (
                    f"Zotero saved this as '{saved_type}' instead of a journal article — "
                    "translator did not recognise the page. The item has been deleted. "
                    "Retry with a different URL, or manually add the paper in Zotero."
                ),
            }

    if not needs_routing:
        return finalize_success(out)

    if item_key is None:
        discovered = 0
        try:
            discovered = len(
                writer.find_items_by_url_and_title(result.get("url", ""), result.get("title", ""))
            )
        except Exception:
            pass
        if discovered == 0:
            warning = (
                "collection_key/tags not applied — item not found within discovery window. "
                "Use add_to_collection(item_key, collection_key) once the item appears in Zotero."
            )
            logger.error(warning)
        else:
            warning = f"collection_key/tags not applied — ambiguous match ({discovered} items found)"
        return {**out, "warning": warning}

    routing_warning = apply_collection_tag_routing_fn(item_key, collection_key, tags, writer)
    if routing_warning:
        out["warning"] = routing_warning
        return out
    return finalize_success(out, "routed_by_backend")


# ---------------------------------------------------------------------------
# Local Zotero API helpers and DOI resolution (moved from ingestion.py)
# ---------------------------------------------------------------------------

_bridge_logger = _logging.getLogger(__name__)

_ZOTERO_LOCAL_API_ITEMS_URL = "http://127.0.0.1:23119/api/users/0/items"


def resolve_doi_to_landing_url(doi: str) -> str | None:
    """Resolve DOI to publisher landing page via doi.org redirect."""
    from .ingestion_search import normalize_landing_url
    try:
        response = _httpx.head(
            f"https://doi.org/{doi}",
            follow_redirects=False,
            timeout=10.0,
        )
        if response.status_code in (301, 302, 303, 307, 308):
            url = response.headers.get("location")
            if url:
                return normalize_landing_url(url)
    except Exception as exc:
        _bridge_logger.debug("DOI resolution failed for %s: %s", doi, exc)
    return None


def resolve_dois_concurrent(dois: list[str]) -> dict[str, str | None]:
    """Resolve multiple DOIs concurrently."""
    if not dois:
        return {}
    results: dict[str, str | None] = {}
    with _ThreadPoolExecutor(max_workers=min(len(dois), 10)) as pool:
        futures = {pool.submit(resolve_doi_to_landing_url, doi): doi for doi in dois}
        for future in _as_completed(futures):
            doi = futures[future]
            try:
                results[doi] = future.result()
            except Exception:
                results[doi] = None
    return results


def route_via_local_api(item_key: str, collection_key: str) -> bool:
    """Route an item into a collection via Zotero Desktop local API."""
    try:
        req = urllib.request.Request(
            f"{_ZOTERO_LOCAL_API_ITEMS_URL}/{item_key}",
            headers={"Accept": "application/json", "Zotero-Allowed-Request": "1"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            item = json.loads(resp.read())
        version = item.get("version", 0)
        data = item.get("data", item)
        collections = set(data.get("collections", []))
        collections.add(collection_key)
        patch_req = urllib.request.Request(
            f"{_ZOTERO_LOCAL_API_ITEMS_URL}/{item_key}",
            data=json.dumps({"collections": sorted(collections)}).encode(),
            headers={
                "Content-Type": "application/json",
                "Zotero-Allowed-Request": "1",
                "If-Unmodified-Since-Version": str(version),
            },
            method="PATCH",
        )
        with urllib.request.urlopen(patch_req, timeout=5):
            return True
    except (urllib.error.URLError, ConnectionRefusedError, OSError):
        return False
    except Exception as exc:
        _bridge_logger.warning("Local API routing failed for %s: %s", item_key, exc)
        return False


def discover_item_via_local_api(url: str, title: str | None) -> str | None:
    """Try to discover a newly saved item via Zotero Desktop local API."""
    if not title:
        return None
    try:
        search_url = (
            f"{_ZOTERO_LOCAL_API_ITEMS_URL}/top?format=json&limit=5"
            f"&q={_urllib_parse.quote(title[:50])}"
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
            _bridge_logger.debug(
                "Local API discovery: ambiguous match (%d items) for '%s'", len(items), title[:50]
            )
    except Exception:
        return None
    return None


def discover_item_via_web_api(url: str, title: str | None, writer, writer_lock) -> str | None:
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
    writer_lock: _threading.Lock,
    logger=None,
) -> dict:
    """Save a single paper via API (CrossRef/arXiv + pyzotero), bypassing Connector."""
    from fastmcp.exceptions import ToolError

    from ..state import _get_resolver
    from .ingestion_search import normalize_doi

    if logger is None:
        logger = _bridge_logger

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
                metadata,
                collection_keys=collection_keys,
                tags=tags,
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

        connector_error = candidate.get("_connector_error")
        warning = None

        save_method_used = "connector_to_api_fallback" if connector_error else "api_primary"
        item_discovery_status = "known_item_key"
        reason_code = None
        pdf_verification_status = None
        has_pdf: bool | None = None
        if attach_status == "attached":
            pdf_verification_status = "present"
            has_pdf = True
        elif attach_status == "attach_failed":
            pdf_verification_status = "pending"
            has_pdf = None
            reason_code = "pdf_attach_failed"
        else:
            pdf_verification_status = "missing"
            has_pdf = False
            if connector_error:
                reason_code = "api_metadata_only"
            else:
                reason_code = "oa_pdf_not_found"

        if connector_error:
            if pdf_verification_status == "present":
                warning = (
                    f"Connector failed ({connector_error}); saved via API and attached an OA PDF."
                )
            elif pdf_verification_status == "pending":
                warning = (
                    f"Connector failed ({connector_error}); saved via API, but PDF verification is still pending. "
                    "Check Chrome/Connector if you need browser-backed capture."
                )
            else:
                warning = (
                    f"Connector failed ({connector_error}); saved via API (metadata only, no PDF). "
                    "Check Chrome/Connector if you need PDF."
                )

        batch.update_item(
            idx,
            status="saved",
            item_key=item_key,
            title=metadata.title,
            routing_status="routed_by_api",
            ingest_method="api",
            warning=warning,
            save_method_used=save_method_used,
            item_discovery_status=item_discovery_status,
            pdf_verification_status=pdf_verification_status,
            has_pdf=has_pdf,
            reason_code=reason_code,
        )
        if item_key:
            _cleanup_publisher_tags(item_key, landing_page_url or "", writer, logger)
        return {"success": True, "item_key": item_key, "title": metadata.title, "error": None}

    except Exception as exc:
        logger.warning("API save failed for index %d: %s", idx, exc)
        batch.update_item(idx, status="failed", error=str(exc))
        return {"success": False, "error": str(exc)}


def run_save_worker(
    batch,
    connector_candidates: list[dict],
    api_candidates: list[dict],
    resolved_collection_key: str | None,
    save_urls_fn: Callable,
    get_writer_fn: Callable,
    writer_lock: _threading.Lock,
    logger=None,
    route_fn: Callable | None = None,
    discover_fn: Callable | None = None,
    save_via_api_fn: Callable | None = None,
    discover_web_fn: Callable | None = None,
) -> None:
    """Background worker: connector save → API fallback → reconciliation → PDF verify."""
    if logger is None:
        logger = _bridge_logger
    _route_fn = route_fn if route_fn is not None else route_via_local_api
    _discover_fn = discover_fn if discover_fn is not None else discover_item_via_local_api
    _save_via_api_fn = save_via_api_fn if save_via_api_fn is not None else save_via_api
    _discover_web_fn = discover_web_fn

    try:
        batch.state = "running"
        writer: Any = None

        # --- Phase 1: Connector saves ---
        candidate_by_index: dict[int, dict] = {c["_index"]: c for c in connector_candidates}
        candidate_by_url: dict[str, dict] = {c["url"]: c for c in connector_candidates}
        urls_to_save = [c["url"] for c in connector_candidates]

        for start in range(0, len(urls_to_save), 10):
            chunk = urls_to_save[start : start + 10]
            batch_result = save_urls_fn(chunk, collection_key=resolved_collection_key, tags=None)
            batch_results = list(batch_result.get("results") or [])
            returned_urls = {r.get("url") for r in batch_results if r.get("url")}

            top_level_failed = batch_result.get("success") is False
            for result in batch_results:
                idx = result.get("index")
                url = result.get("url")
                if idx is None:
                    candidate = candidate_by_url.get(url)
                    if candidate is None:
                        continue
                    idx = candidate["_index"]
                else:
                    candidate = candidate_by_index.get(idx)
                if result.get("success") is True:
                    item_key = result.get("item_key")
                    result_title = result.get("title") or (
                        candidate.get("paper", {}).get("title") if candidate else None
                    )
                    item_discovery_status = "known_item_key" if item_key else None
                    if not item_key:
                        discovered = _discover_fn(url or "", result_title)
                        if discovered:
                            item_discovery_status = "discovered_local"
                        else:
                            if _discover_web_fn is not None:
                                discovered = _discover_web_fn(url or "", result_title)
                                item_discovery_status = "discovered_web" if discovered else None
                            else:
                                discovered = discover_item_via_web_api(
                                    url or "", result_title, get_writer_fn(), writer_lock
                                )
                                item_discovery_status = "discovered_web" if discovered else None
                        if discovered:
                            item_key = discovered
                        else:
                            batch.update_item(
                                idx,
                                status="failed",
                                error=(
                                    "Connector reported success but item not found in Zotero. "
                                    "Save may have silently failed."
                                ),
                                save_method_used="connector_primary",
                                item_discovery_status="not_found",
                                reason_code="connector_item_not_found",
                            )
                            continue
                    batch.update_item(
                        idx,
                        status="saved",
                        item_key=item_key,
                        title=result_title,
                        warning=result.get("warning"),
                        routing_status=result.get("routing_status"),
                        save_method_used="connector_primary",
                        item_discovery_status=item_discovery_status or "known_item_key",
                    )
                else:
                    batch.update_item(
                        idx,
                        status="failed",
                        error=result.get("error") or result.get("error_message") or "bridge save failed",
                        save_method_used="connector_primary",
                        reason_code="connector_save_failed",
                    )

            for url in chunk:
                candidate = candidate_by_url.get(url)
                if candidate is None:
                    continue
                idx = candidate["_index"]
                if top_level_failed or url not in returned_urls:
                    for item in batch.pending_items:
                        if item.index == idx and item.status == "pending":
                            batch.update_item(
                                idx,
                                status="failed",
                                error="bridge save failed",
                                save_method_used="connector_primary",
                                reason_code="connector_save_failed",
                            )

        # --- Phase 2: API fallback for failed connector items ---
        connector_api_retries: list[dict] = []
        for candidate in connector_candidates:
            paper = candidate["paper"]
            if not paper.get("doi"):
                continue
            for item in batch.pending_items:
                if item.index == candidate["_index"] and item.status == "failed":
                    connector_error = item.error or "connector save failed"
                    connector_api_retries.append(
                        {
                            **candidate,
                            "url": None,
                            "ingest_method": "api",
                            "_connector_error": connector_error,
                        }
                    )
                    batch.update_item(
                        candidate["_index"],
                        status="pending",
                        ingest_method="api",
                        warning=(
                            f"Connector failed ({connector_error}); retrying via API (metadata only, no PDF). "
                            "Check Chrome/Connector if you need PDF."
                        ),
                        error=None,
                        save_method_used="connector_to_api_fallback",
                    )
                    break

        phase2_api_candidates = api_candidates + connector_api_retries
        if phase2_api_candidates:
            writer = get_writer_fn()
            for candidate in phase2_api_candidates:
                _save_via_api_fn(
                    candidate,
                    resolved_collection_key=resolved_collection_key,
                    tags=None,
                    batch=batch,
                    writer=writer,
                    writer_lock=writer_lock,
                    logger=logger,
                )

        # Post-batch reconciliation
        unrouted_items = [
            item for item in batch.pending_items
            if item.status == "saved" and item.item_key and not item.routing_status and item.ingest_method != "api"
        ]
        deferred_items = [item for item in batch.pending_items if item.status == "saved" and not item.item_key]
        if (unrouted_items or deferred_items) and resolved_collection_key:
            logger.info(
                "Starting reconciliation for %d unrouted and %d deferred items",
                len(unrouted_items), len(deferred_items),
            )
            time.sleep(15)
            writer = None
            for item in deferred_items:
                discovered_key = _discover_fn(item.url or "", item.title)
                if not discovered_key:
                    try:
                        if _discover_web_fn is not None:
                            discovered_key = _discover_web_fn(item.url or "", item.title)
                        else:
                            w = get_writer_fn()
                            discovered_key = discover_item_via_web_api(item.url or "", item.title, w, writer_lock)
                    except Exception:
                        pass
                if discovered_key:
                    item.item_key = discovered_key
                    unrouted_items.append(item)
                else:
                    batch.update_item(
                        item.index,
                        status="failed",
                        error="Item not found in Zotero after save — connector may have reported false success.",
                        routing_status="routing_failed",
                    )
                    logger.error("Reconciliation: item_key not found for %s — demoted to failed", item.url)

            for item in unrouted_items:
                if not item.item_key:
                    continue
                try:
                    if _route_fn(item.item_key, resolved_collection_key):
                        batch.update_item(
                            item.index, status="saved", item_key=item.item_key,
                            warning=None, routing_status="routed_by_reconciliation_local",
                            item_discovery_status="discovered_local",
                        )
                    else:
                        if writer is None:
                            writer = get_writer_fn()
                        with writer_lock:
                            writer.add_to_collection(item.item_key, resolved_collection_key)
                        batch.update_item(
                            item.index, status="saved", item_key=item.item_key,
                            warning=None, routing_status="routed_by_reconciliation_web",
                            item_discovery_status="discovered_web",
                        )
                    if writer is None:
                        writer = get_writer_fn()
                    _cleanup_publisher_tags(item.item_key, item.url or "", writer, logger)
                except Exception as exc:
                    batch.update_item(
                        item.index, status="saved", item_key=item.item_key,
                        warning=f"Reconciliation routing failed: {exc}",
                        routing_status="routing_failed",
                    )
                    logger.error("Reconciliation routing failed for %s: %s", item.item_key, exc)

        # Post-reconciliation: verify PDF attachments
        saved_items_with_keys = [
            item for item in batch.pending_items if item.status == "saved" and item.item_key
        ]
        if saved_items_with_keys:
            if writer is None:
                try:
                    writer = get_writer_fn()
                except Exception as exc:
                    logger.warning("PDF verification: writer unavailable: %s", exc)
                    writer = None
            if writer is not None and hasattr(writer, "check_has_pdf"):
                with writer_lock:
                    for item in saved_items_with_keys:
                        try:
                            has_pdf = writer.check_has_pdf(item.item_key)
                            existing_warning = item.warning or ""
                            if has_pdf:
                                batch.update_item(
                                    item.index,
                                    status=item.status,
                                    has_pdf=True,
                                    pdf_verification_status="present",
                                    reason_code=None,
                                    verification_attempts=1,
                                    warning=existing_warning or None,
                                )
                            else:
                                if item.reason_code == "pdf_attach_failed":
                                    deadline = item.verification_deadline_at or (
                                        time.monotonic() + PDF_VERIFICATION_WINDOW_S
                                    )
                                    batch.update_item(
                                        item.index,
                                        status=item.status,
                                        has_pdf=None,
                                        pdf_verification_status="pending",
                                        reason_code="pdf_attach_failed",
                                        verification_attempts=1,
                                        verification_deadline_at=deadline,
                                        warning=existing_warning or None,
                                    )
                                    continue
                                if item.save_method_used in {"api_primary", "connector_to_api_fallback"}:
                                    batch.update_item(
                                        item.index,
                                        status=item.status,
                                        has_pdf=False,
                                        pdf_verification_status=item.pdf_verification_status or "missing",
                                        reason_code=item.reason_code or "api_metadata_only",
                                        verification_attempts=1,
                                        warning=existing_warning or None,
                                    )
                                    continue
                                deadline = time.monotonic() + PDF_VERIFICATION_WINDOW_S
                                batch.update_item(
                                    item.index,
                                    status=item.status,
                                    has_pdf=None,
                                    pdf_verification_status="pending",
                                    reason_code=(
                                        item.reason_code
                                        or (
                                            "api_metadata_only"
                                            if item.save_method_used in {"api_primary", "connector_to_api_fallback"}
                                            else "connector_save_pending_pdf"
                                        )
                                    ),
                                    verification_attempts=1,
                                    verification_deadline_at=deadline,
                                    warning=existing_warning or None,
                                )
                        except Exception as exc:
                            logger.warning("check_has_pdf failed for %s: %s", item.item_key, exc)

    except Exception as exc:
        logger.error("Ingest worker failed: %s", exc, exc_info=True)
        for item in batch.pending_items:
            if item.status == "pending":
                batch.update_item(item.index, status="failed", error=f"worker error: {exc}")
    finally:
        batch.finalize()


# ---------------------------------------------------------------------------
# Connector availability + preflight helpers (called by ingest_papers)
# ---------------------------------------------------------------------------

def check_connector_availability(
    connector_candidates: list[dict],
    default_port: int,
    bridge_server_cls,
) -> tuple[bool, str | None, float | None]:
    """Check bridge + extension availability for connector candidates.

    Returns (extension_connected, detail_error, last_seen_s).
    detail_error is None when extension is connected.
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
    logger,
    *,
    sleep_fn=time.sleep,
    monotonic_fn=time.monotonic,
) -> tuple[list[dict], list[dict], dict | None]:
    """Run preflight on connector candidate URLs.

    Returns (updated_connector_candidates, preflight_failures, blocking_decision_dict).
    preflight_failures is a list of result dicts with status=failed.
    blocking_decision_dict is non-None when ALL candidates were blocked (batch must halt).
    """
    from .ingest_state import BlockingDecision

    urls_to_save = [c["url"] for c in connector_candidates]
    preflight_report = preflight_urls(
        urls_to_save,
        sample_size=5,
        default_port=default_port,
        bridge_server_cls=bridge_server_cls,
        logger=logger,
        sleep_fn=sleep_fn,
        monotonic_fn=monotonic_fn,
    )

    if preflight_report.get("all_clear", False):
        return connector_candidates, [], None

    failures: list[dict] = []
    blocked_domains: set[str] = set()

    for blocked in preflight_report.get("blocked", []):
        blocked_url = blocked.get("url") or ""
        blocked_domains.add(extract_publisher_domain(blocked_url))
        failures.append({
            "url": blocked_url,
            "status": "failed",
            "error_code": blocked.get("error_code") or "anti_bot_detected",
            "error": (
                blocked.get("error")
                or "Anti-bot protection detected. "
                "Please complete browser verification in Chrome, then retry. "
                "DO NOT retry with save_urls or DOI links — "
                "you'll hit the same wall and produce a partial-success batch."
            ),
        })

    for error in preflight_report.get("errors", []):
        error_url = error.get("url") or ""
        blocked_domains.add(extract_publisher_domain(error_url))
        failures.append({
            "url": error_url,
            "status": "failed",
            "error_code": error.get("error_code") or "preflight_failed",
            "error": error.get("error") or "preflight failed",
        })

    remaining = [
        c for c in connector_candidates
        if extract_publisher_domain(c["url"]) not in blocked_domains
    ]
    dropped = len(connector_candidates) - len(remaining)
    if dropped:
        logger.info(
            "Preflight: dropped %d connector candidate(s) from %d blocked domain(s); %d remain.",
            dropped, len(blocked_domains), len(remaining),
        )

    if not remaining:
        decision = BlockingDecision(
            decision_id="preflight_blocked",
            batch_id=None,
            item_keys=tuple(),
            description=(
                "Preflight detected anti-bot protection (CAPTCHA / Cloudflare / login). "
                "User must complete browser verification in Chrome, then retry the SAME "
                "ingest_papers call with identical inputs. "
                "DO NOT retry with save_urls or DOI links — same wall, worse state."
            ),
        )
        blocking_dict = {
            "decision_id": decision.decision_id,
            "batch_id": decision.batch_id,
            "item_keys": list(decision.item_keys),
            "description": decision.description,
            "resolved": decision.resolved,
        }
        return remaining, failures, blocking_dict

    return remaining, failures, None

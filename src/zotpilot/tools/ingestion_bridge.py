"""Bridge-facing helpers for ingestion connector workflows."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

DISCOVERY_BACKOFF_DELAYS = [2.0, 4.0, 8.0, 16.0, 32.0]
ITEM_DISCOVERY_WINDOW_S = 120
SAVE_RESULT_POLL_TIMEOUT_S = 150.0
SAVE_RESULT_POLL_OVERALL_TIMEOUT_S = 600.0
SAVE_RESULT_POLL_PER_URL_BUDGET_S = 75.0
SAVE_RESULT_POLL_OVERALL_GRACE_S = 120.0
ROUTING_RETRY_DELAYS_S = [0.0, 2.0, 5.0]
_PUBLISHER_TAG_SOURCES = {"arxiv.org", "biorxiv.org", "medrxiv.org"}

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
    per_url_timeout = compute_save_result_poll_timeout_s(batch_size)
    return max(SAVE_RESULT_POLL_OVERALL_TIMEOUT_S, per_url_timeout + SAVE_RESULT_POLL_OVERALL_GRACE_S)


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
    """Return True for sources known to inject unwanted auto-tags."""
    hostname = (urlparse(url).hostname or "").lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return any(hostname.endswith(source) for source in _PUBLISHER_TAG_SOURCES)


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


def wait_for_extension(
    bridge_url: str,
    timeout: int = 35,
    *,
    sleep_fn=time.sleep,
    monotonic_fn=time.monotonic,
) -> bool:
    """Poll GET /status until extension_connected is True or timeout expires."""
    deadline = monotonic_fn() + timeout
    while monotonic_fn() < deadline:
        try:
            response = urllib.request.urlopen(f"{bridge_url}/status", timeout=3)
            data = json.loads(response.read())
            if data.get("extension_connected"):
                return True
        except Exception:
            pass
        sleep_fn(1)
    return False


def get_extension_status(bridge_url: str) -> dict:
    """Query /status and return the parsed JSON, or an error dict."""
    try:
        response = urllib.request.urlopen(f"{bridge_url}/status", timeout=3)
        return json.loads(response.read())
    except Exception as exc:
        return {"extension_connected": False, "error": str(exc)}


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
    report = {
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
                "Connector extension did not connect within 35 seconds. "
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
            "error": "Timeout (120s) — preflight did not complete.",
        })
        status = result.get("status")
        if status == "accessible":
            report["accessible"].append({
                "url": url,
                "title": result.get("title", ""),
                "final_url": result.get("final_url", url),
            })
        elif status == "anti_bot_detected":
            report["blocked"].append({
                "url": url,
                "title": result.get("title", ""),
                "final_url": result.get("final_url", url),
            })
        else:
            error_entry = {
                "url": url,
                "error": (
                    result.get("error")
                    or result.get("error_message")
                    or "unknown preflight error"
                ),
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
) -> dict:
    """Poll one bridge save request until it completes or times out."""
    deadline = monotonic_fn() + timeout_s
    while monotonic_fn() < deadline:
        sleep_fn(2)
        try:
            response = urllib.request.urlopen(f"{bridge_url}/result/{request_id}", timeout=5)
            if response.status == 200:
                return json.loads(response.read())
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
    """Poll batch save requests with anti-bot short-circuiting."""
    polled: dict[str, dict] = {}
    pending_ids = set(id_to_url)
    per_request_deadlines = {
        request_id: monotonic_fn() + per_url_timeout_s for request_id in id_to_url
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
        return items[0]
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

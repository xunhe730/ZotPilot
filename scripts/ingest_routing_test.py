#!/usr/bin/env python3
"""Ingest routing benchmark — measures real Connector behavior.

Tests on the user's actual Zotero library with papers they want.
Records: save timing, item_key discovery, preflight, PDF status.

Usage:
    python scripts/ingest_routing_test.py
"""
import json
import sys
import time
from pathlib import Path

# Ensure src is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zotpilot.bridge import BridgeServer, DEFAULT_PORT

# Import ingestion_bridge directly to avoid pulling in the full tools package
# (which requires FastMCP and the MCP server infrastructure)
import importlib.util as _ilu
_ib_path = str(Path(__file__).resolve().parents[1] / "src" / "zotpilot" / "tools" / "ingestion_bridge.py")
_ib_spec = _ilu.spec_from_file_location("ingestion_bridge", _ib_path)
_ib = _ilu.module_from_spec(_ib_spec)

# Patch sys.modules so internal relative imports within ingestion_bridge resolve
import zotpilot.tools  # noqa: this may fail, ignore
sys.modules.setdefault("zotpilot.tools.ingestion_bridge", _ib)
_ib_spec.loader.exec_module(_ib)

enqueue_save_request = _ib.enqueue_save_request
get_extension_status = _ib.get_extension_status
looks_like_error_page_title = _ib.looks_like_error_page_title
poll_single_save_result = _ib.poll_single_save_result
preflight_urls = _ib.preflight_urls
discover_item_via_local_api = _ib.discover_item_via_local_api

# ---------------------------------------------------------------------------
# Test candidates: 5 VLM papers, mixed publishers + OA status
# ---------------------------------------------------------------------------
CANDIDATES = [
    {
        "doi": "10.1007/s11263-023-01891-x",
        "title": "CLIP-Adapter: Better Vision-Language Models with Feature Adapters",
        "url": "https://link.springer.com/article/10.1007/s11263-023-01891-x",
        "publisher": "Springer",
        "oa": False,
    },
    {
        "doi": "10.1109/tpami.2024.3369699",
        "title": "Vision-Language Models for Vision Tasks: A Survey",
        "url": "https://ieeexplore.ieee.org/document/10445007",
        "publisher": "IEEE",
        "oa": False,
    },
    {
        "doi": "10.1109/tpami.2023.3275156",
        "title": "Multimodal Learning With Transformers: A Survey",
        "url": "https://ieeexplore.ieee.org/document/10123038",
        "publisher": "IEEE",
        "oa": True,
    },
    {
        "doi": "10.18653/v1/2023.ijcnlp-main.45",
        "title": "A Multitask, Multilingual, Multimodal Evaluation of ChatGPT",
        "url": "https://aclanthology.org/2023.ijcnlp-main.45/",
        "publisher": "ACL Anthology",
        "oa": True,
    },
    {
        "doi": "10.1038/s41586-023-05881-4",
        "title": "Foundation models for generalist medical artificial intelligence",
        "url": "https://www.nature.com/articles/s41586-023-05881-4",
        "publisher": "Nature",
        "oa": True,
    },
]

BRIDGE_URL = f"http://127.0.0.1:{DEFAULT_PORT}"
RESULTS = []


def log(msg: str):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def check_prerequisites() -> bool:
    """Check Bridge + Connector are online."""
    log("=== Prerequisites ===")

    bridge_running = BridgeServer.is_running(DEFAULT_PORT)
    log(f"Bridge running: {bridge_running}")
    if not bridge_running:
        try:
            BridgeServer.auto_start(DEFAULT_PORT)
            bridge_running = True
            log("Bridge auto-started")
        except Exception as e:
            log(f"Bridge start failed: {e}")
            return False

    ext = get_extension_status(BRIDGE_URL)
    connected = ext.get("extension_connected", False)
    last_seen = ext.get("extension_last_seen_s")
    log(f"Connector connected: {connected}, last_seen: {last_seen}s ago")

    if not connected:
        log("ERROR: Connector not connected. Open Chrome + enable ZotPilot Connector.")
        return False

    return True


def experiment_1_preflight(candidates: list[dict]) -> dict:
    """Experiment: preflight URL sampling — no items created."""
    log("\n=== Experiment 1: Preflight (no saves) ===")
    urls = [c["url"] for c in candidates]

    t0 = time.monotonic()
    report = preflight_urls(
        urls,
        sample_size=5,
        default_port=DEFAULT_PORT,
        bridge_server_cls=BridgeServer,
        logger=__import__("logging").getLogger("preflight_test"),
    )
    elapsed = time.monotonic() - t0

    all_clear = report.get("all_clear", False)
    blocked = report.get("blocked", [])
    passed = report.get("passed", [])
    errors = report.get("errors", [])

    log(f"  Time: {elapsed:.1f}s")
    log(f"  All clear: {all_clear}")
    log(f"  Passed: {len(passed)}, Blocked: {len(blocked)}, Errors: {len(errors)}")

    for b in blocked:
        log(f"  BLOCKED: {b.get('url', '')[:60]} — {b.get('error_code', '?')}")

    result = {
        "experiment": "preflight",
        "elapsed_s": round(elapsed, 2),
        "all_clear": all_clear,
        "passed_count": len(passed),
        "blocked_count": len(blocked),
        "error_count": len(errors),
        "blocked_urls": [b.get("url") for b in blocked],
    }
    RESULTS.append(result)
    return result


def experiment_2_single_save(candidate: dict) -> dict:
    """Experiment: save ONE paper via Connector, measure everything."""
    doi = candidate["doi"]
    url = candidate["url"]
    title = candidate["title"]
    publisher = candidate["publisher"]

    log(f"\n--- Save: {title[:60]} ({publisher}) ---")

    # Step 1: Enqueue
    t_enqueue = time.monotonic()
    request_id, error = enqueue_save_request(BRIDGE_URL, url, collection_key=None, tags=None)
    t_enqueue_done = time.monotonic()

    if error:
        log(f"  Enqueue FAILED: {error}")
        result = {
            "experiment": "single_save",
            "doi": doi,
            "publisher": publisher,
            "status": "enqueue_failed",
            "error": str(error),
            "enqueue_time_s": round(t_enqueue_done - t_enqueue, 2),
        }
        RESULTS.append(result)
        return result

    log(f"  Enqueued: request_id={request_id}, took {t_enqueue_done - t_enqueue:.2f}s")

    # Step 2: Poll for result
    t_poll = time.monotonic()
    save_result = poll_single_save_result(BRIDGE_URL, request_id, timeout_s=60.0)
    t_poll_done = time.monotonic()

    success = save_result.get("success", False)
    item_key = save_result.get("item_key")
    result_title = save_result.get("title", "")
    has_pdf = save_result.get("pdf")
    routing_applied = save_result.get("routing_applied")

    log(f"  Poll done: {t_poll_done - t_poll:.2f}s")
    log(f"  Success: {success}")
    log(f"  Item key in result: {item_key}")
    log(f"  Title returned: {result_title[:60] if result_title else 'N/A'}")
    log(f"  PDF: {has_pdf}")
    log(f"  Routing applied: {routing_applied}")

    # Step 3: Error page check
    is_error = looks_like_error_page_title(result_title or "", item_key) if result_title else False
    log(f"  Error page detected: {is_error}")

    # Step 4: If no item_key, try local discovery
    discovery_method = "direct" if item_key else None
    discovery_time = 0.0
    if success and not item_key:
        log("  Item key missing — attempting local discovery...")
        t_disc = time.monotonic()
        try:
            from zotpilot.tools.ingestion_bridge import discover_item_via_local_api
            discovered = discover_item_via_local_api(url, title)
            discovery_time = time.monotonic() - t_disc
            if discovered:
                item_key = discovered
                discovery_method = "local_api"
                log(f"  Discovered via local API: {item_key} ({discovery_time:.2f}s)")
            else:
                discovery_method = "failed"
                log(f"  Local discovery FAILED ({discovery_time:.2f}s)")
        except Exception as e:
            discovery_time = time.monotonic() - t_disc
            discovery_method = "error"
            log(f"  Discovery error: {e}")

    total_time = t_poll_done - t_enqueue

    result = {
        "experiment": "single_save",
        "doi": doi,
        "publisher": publisher,
        "oa": candidate["oa"],
        "status": "saved" if success else "failed",
        "item_key": item_key,
        "item_key_source": discovery_method,
        "is_error_page": is_error,
        "has_pdf": has_pdf,
        "enqueue_time_s": round(t_enqueue_done - t_enqueue, 2),
        "poll_time_s": round(t_poll_done - t_poll, 2),
        "discovery_time_s": round(discovery_time, 2),
        "total_time_s": round(total_time, 2),
        "result_title": (result_title or "")[:100],
        "error": save_result.get("error") if not success else None,
    }
    RESULTS.append(result)
    return result


def main():
    log("ZotPilot Ingest Routing Benchmark")
    log(f"Candidates: {len(CANDIDATES)} papers")
    log("")

    if not check_prerequisites():
        sys.exit(1)

    # Experiment 1: Preflight (safe, no saves)
    preflight = experiment_1_preflight(CANDIDATES)

    if preflight["blocked_count"] > 0:
        blocked_urls = set(preflight["blocked_urls"])
        log(f"\n⚠️  {preflight['blocked_count']} URLs blocked at preflight.")
        log("Skipping blocked candidates for save experiments.")
        log("Please verify in Chrome, then re-run.")
        save_candidates = [c for c in CANDIDATES if c["url"] not in blocked_urls]
    else:
        save_candidates = list(CANDIDATES)

    if not save_candidates:
        log("\nAll candidates blocked. Resolve in browser first.")
    else:
        # Experiment 2: Sequential single saves
        log(f"\n=== Experiment 2: Sequential Single Saves ({len(save_candidates)} papers) ===")
        log("Each paper saved one at a time with full measurement.\n")

        input(f"Press Enter to start saving {len(save_candidates)} papers to your Zotero library...")

        for i, candidate in enumerate(save_candidates):
            log(f"\n[{i+1}/{len(save_candidates)}]")
            experiment_2_single_save(candidate)
            if i < len(save_candidates) - 1:
                log("  Waiting 3s before next save...")
                time.sleep(3)  # Politeness delay

    # Summary
    log("\n" + "=" * 60)
    log("RESULTS SUMMARY")
    log("=" * 60)

    save_results = [r for r in RESULTS if r["experiment"] == "single_save"]
    if save_results:
        saved = [r for r in save_results if r["status"] == "saved"]
        failed = [r for r in save_results if r["status"] != "saved"]
        direct_key = [r for r in saved if r["item_key_source"] == "direct"]
        disc_key = [r for r in saved if r["item_key_source"] in ("local_api", "web_api")]
        no_key = [r for r in saved if r["item_key_source"] in ("failed", "error", None)]
        with_pdf = [r for r in saved if r["has_pdf"]]

        log(f"\nSaves: {len(saved)}/{len(save_results)} succeeded, {len(failed)} failed")
        log(f"Item key: {len(direct_key)} direct, {len(disc_key)} discovered, {len(no_key)} not found")
        log(f"PDF attached: {len(with_pdf)}/{len(saved)}")

        if saved:
            times = [r["total_time_s"] for r in saved]
            log(f"Save time: min={min(times):.1f}s, max={max(times):.1f}s, avg={sum(times)/len(times):.1f}s")

    # Write full results
    out_path = Path(__file__).parent / "ingest_routing_results.json"
    with open(out_path, "w") as f:
        json.dump(RESULTS, f, indent=2, ensure_ascii=False)
    log(f"\nFull results written to {out_path}")


if __name__ == "__main__":
    main()

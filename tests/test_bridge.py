"""Tests for ZotPilot HTTP bridge server."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from zotpilot.bridge import BridgeServer


class TestBridgeServer:
    def test_no_pending_returns_204(self):
        """GET /pending with no commands returns 204."""
        bridge = BridgeServer(port=0)
        bridge.start()
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{bridge.port}/pending")
            resp = urllib.request.urlopen(req)
            assert resp.status == 204
        finally:
            bridge.stop()

    def test_enqueue_and_fetch(self):
        """Enqueue a command, GET /pending returns it with request_id."""
        bridge = BridgeServer(port=0)
        bridge.start()
        try:
            bridge.enqueue({
                "action": "save",
                "url": "https://example.com/paper",
            })
            req = urllib.request.Request(f"http://127.0.0.1:{bridge.port}/pending")
            resp = urllib.request.urlopen(req)
            data = json.loads(resp.read())
            assert data["action"] == "save"
            assert data["url"] == "https://example.com/paper"
            assert "request_id" in data
        finally:
            bridge.stop()

    def test_enqueue_via_http(self):
        """POST /enqueue accepts commands and returns request_id when extension is connected."""
        bridge = BridgeServer(port=0)
        bridge.start()
        try:
            # Send a heartbeat so the extension is considered connected
            heartbeat = json.dumps({"extension_version": "0.1.0", "zotero_connected": True}).encode()
            hb_req = urllib.request.Request(
                f"http://127.0.0.1:{bridge.port}/heartbeat",
                data=heartbeat,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(hb_req)

            command = json.dumps({"action": "save", "url": "https://example.com"}).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{bridge.port}/enqueue",
                data=command,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = urllib.request.urlopen(req)
            data = json.loads(resp.read())
            assert "request_id" in data

            # Verify it's in the queue
            req2 = urllib.request.Request(f"http://127.0.0.1:{bridge.port}/pending")
            resp2 = urllib.request.urlopen(req2)
            queued = json.loads(resp2.read())
            assert queued["request_id"] == data["request_id"]
        finally:
            bridge.stop()

    def test_enqueue_returns_503_when_extension_disconnected(self):
        """POST /enqueue returns 503 when no extension heartbeat has been received."""
        bridge = BridgeServer(port=0)
        bridge.start()
        try:
            command = json.dumps({"action": "save", "url": "https://example.com"}).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{bridge.port}/enqueue",
                data=command,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                urllib.request.urlopen(req)
                assert False, "Expected HTTPError 503"
            except urllib.error.HTTPError as e:
                assert e.code == 503
                body = json.loads(e.read())
                assert body["error_code"] == "extension_not_connected"
        finally:
            bridge.stop()

    def test_post_result_and_retrieve(self):
        """POST /result stores result, GET /result/<id> returns it."""
        bridge = BridgeServer(port=0)
        bridge.start()
        try:
            rid = bridge.enqueue({"action": "save", "url": "https://example.com"})
            result = {"request_id": rid, "success": True, "title": "Test Paper"}
            data = json.dumps(result).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{bridge.port}/result",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req)

            # Retrieve via GET /result/<id>
            req2 = urllib.request.Request(f"http://127.0.0.1:{bridge.port}/result/{rid}")
            resp2 = urllib.request.urlopen(req2)
            stored = json.loads(resp2.read())
            assert stored["success"] is True
            assert stored["title"] == "Test Paper"
        finally:
            bridge.stop()

    def test_result_not_found_returns_204(self):
        """GET /result/<nonexistent_id> returns 204."""
        bridge = BridgeServer(port=0)
        bridge.start()
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{bridge.port}/result/nonexistent")
            resp = urllib.request.urlopen(req)
            assert resp.status == 204
        finally:
            bridge.stop()

    def test_status_endpoint(self):
        """GET /status returns running status."""
        bridge = BridgeServer(port=0)
        bridge.start()
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{bridge.port}/status")
            resp = urllib.request.urlopen(req)
            data = json.loads(resp.read())
            assert data["bridge"] == "running"
        finally:
            bridge.stop()

    def test_queue_is_fifo(self):
        """Multiple commands dequeue in order."""
        bridge = BridgeServer(port=0)
        bridge.start()
        try:
            bridge.enqueue({"action": "save", "url": "https://first.com"})
            bridge.enqueue({"action": "save", "url": "https://second.com"})

            resp1 = urllib.request.urlopen(f"http://127.0.0.1:{bridge.port}/pending")
            data1 = json.loads(resp1.read())
            assert data1["url"] == "https://first.com"

            resp2 = urllib.request.urlopen(f"http://127.0.0.1:{bridge.port}/pending")
            data2 = json.loads(resp2.read())
            assert data2["url"] == "https://second.com"
        finally:
            bridge.stop()

    def test_enqueue_does_not_mutate_input(self):
        """enqueue() makes a defensive copy of the command dict."""
        bridge = BridgeServer(port=0)
        original = {"action": "save", "url": "https://example.com"}
        bridge.enqueue(original)
        assert "request_id" not in original

    def test_is_running_false_when_not_started(self):
        """is_running returns False for a port with no server."""
        assert BridgeServer.is_running(port=19999) is False


# ------------------------------------------------------------------
# Origin ACL tests
# ------------------------------------------------------------------


class TestOriginWhitelist:
    """Verify _check_origin enforces the extension-origin whitelist."""

    @pytest.mark.parametrize(
        "origin,expected_status",
        [
            (None, 204),                         # CLI/MCP (no Origin header) — queue empty
            ("", 204),                           # empty Origin — queue empty
            ("chrome-extension://abcdef", 204),  # Chrome extension — queue empty
            ("moz-extension://xxx", 204),        # Firefox extension — queue empty
            ("safari-web-extension://yyy", 204), # Safari extension — queue empty
            ("https://evil.com", 403),           # malicious website
            ("http://localhost:3000", 403),      # local dev server (not extension)
            ("null", 403),                       # file:// or sandboxed
            ("chrome-extension-evil://xxx", 403),  # prefix spoof
        ],
    )
    def test_origin_acl_on_pending(self, origin, expected_status):
        """GET /pending enforces Origin whitelist."""
        bridge = BridgeServer(port=0)
        bridge.start()
        try:
            headers = {"Accept": "application/json"}
            if origin is not None:
                headers["Origin"] = origin
            req = urllib.request.Request(
                f"http://127.0.0.1:{bridge.port}/pending",
                headers=headers,
            )
            try:
                resp = urllib.request.urlopen(req)
                assert resp.status == expected_status
            except urllib.error.HTTPError as e:
                assert e.code == expected_status
        finally:
            bridge.stop()

    @pytest.mark.parametrize(
        "origin,expected_status",
        [
            (None, 200),
            ("", 200),
            ("chrome-extension://abcdef", 200),
            ("https://evil.com", 403),
            ("null", 403),
        ],
    )
    def test_origin_acl_on_enqueue(self, origin, expected_status):
        """POST /enqueue enforces Origin whitelist."""
        bridge = BridgeServer(port=0)
        bridge.start()
        try:
            # Send heartbeat first so we don't get 503 masking the 403
            heartbeat = json.dumps({"extension_version": "0.1.0"}).encode()
            hb_req = urllib.request.Request(
                f"http://127.0.0.1:{bridge.port}/heartbeat",
                data=heartbeat,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(hb_req)

            headers = {"Content-Type": "application/json"}
            if origin is not None:
                headers["Origin"] = origin
            req = urllib.request.Request(
                f"http://127.0.0.1:{bridge.port}/enqueue",
                data=json.dumps({"action": "save", "url": "https://example.com"}).encode(),
                headers=headers,
                method="POST",
            )
            try:
                resp = urllib.request.urlopen(req)
                assert resp.status == expected_status
            except urllib.error.HTTPError as e:
                assert e.code == expected_status
        finally:
            bridge.stop()

    def test_status_always_200_regardless_of_origin(self):
        """GET /status is public — no origin check."""
        bridge = BridgeServer(port=0)
        bridge.start()
        try:
            for origin in [None, "https://evil.com", "chrome-extension://abc"]:
                headers = {"Accept": "application/json"}
                if origin is not None:
                    headers["Origin"] = origin
                req = urllib.request.Request(
                    f"http://127.0.0.1:{bridge.port}/status",
                    headers=headers,
                )
                resp = urllib.request.urlopen(req)
                assert resp.status == 200
        finally:
            bridge.stop()


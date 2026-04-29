"""HTTP bridge between ZotPilot MCP tools and the ZotPilot Connector extension.

The bridge serves endpoints on localhost:
  GET  /pending       → returns next queued save command (or 204 No Content)
  POST /enqueue       → accepts a save command from MCP tools; returns 503 if
                        extension has not been seen in >30s
  POST /result        → receives save results from the extension
  GET  /result/<id>   → returns result for a specific request_id (or 204)
  POST /heartbeat     → extension liveness signal (every ~10s)
  GET  /status        → health check with extension + Zotero connectivity info

The Chrome extension polls GET /pending every 2 seconds and POSTs /heartbeat
every 10s. MCP tools POST to /enqueue and poll GET /result/<id> for the outcome.

Uses ThreadingHTTPServer to avoid deadlock when the MCP tool is polling
/result while the extension tries to POST /result concurrently.
"""

import json
import logging
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_PORT = 2619

# Extension is considered disconnected if no heartbeat in this many seconds.
# 30s = 3× the 10s heartbeat interval, tolerating transient scheduling jitter.
_HEARTBEAT_TIMEOUT_S = 30

# Result TTL and max entries (Task 1.4: prevent unbounded growth)
_RESULT_TTL_S = 300  # 5 minutes
_RESULT_MAX_ENTRIES = 100


_ALLOWED_ORIGIN_PREFIXES = (
    "chrome-extension://",
    "moz-extension://",
    "safari-web-extension://",
)


class _BridgeHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the bridge."""

    def log_message(self, format, *args):
        logger.debug(format, *args)

    def _set_cors(self):
        origin = self.headers.get("Origin", "")
        if origin and origin.startswith(_ALLOWED_ORIGIN_PREFIXES):
            self.send_header("Access-Control-Allow-Origin", origin)
        else:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._set_cors()
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def _check_origin(self) -> bool:
        """Allow empty Origin (CLI/MCP) or browser-extension Origins; deny others."""
        origin = self.headers.get("Origin", "")
        if not origin:
            return True  # non-browser caller (CLI / MCP / curl)
        if origin.startswith(_ALLOWED_ORIGIN_PREFIXES):
            return True
        # Reject any http(s)://... and "null"
        self._send_403("origin_not_allowed", origin)
        return False

    def _send_403(self, error_code: str, origin: str) -> None:
        """Send a 403 response for origin-not-allowed."""
        body = json.dumps({"error": error_code, "origin": origin}).encode()
        self.send_response(403)
        self._set_cors()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/status":
            body = json.dumps(self.server.bridge.get_status()).encode()
            self.send_response(200)
            self._set_cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return

        # All other GET endpoints require origin check
        if not self._check_origin():
            return

        if self.path == "/pending":
            cmd = self.server.bridge._dequeue()
            if cmd:
                body = json.dumps(cmd).encode()
                self.send_response(200)
                self._set_cors()
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(204)
                self._set_cors()
                self.end_headers()
        elif self.path.startswith("/result/"):
            request_id = self.path[len("/result/") :]
            result = self.server.bridge.get_result(request_id)
            if result:
                body = json.dumps(result).encode()
                self.send_response(200)
                self._set_cors()
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(204)
                self._set_cors()
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        # POST endpoints require origin check
        if not self._check_origin():
            return

        if self.path == "/enqueue":
            # Task 1.4: reject immediately if extension is not connected
            if not self.server.bridge.extension_connected:
                body = json.dumps(
                    {
                        "error_code": "extension_not_connected",
                        "error_message": (
                            "ZotPilot Connector has not sent a heartbeat in the last "
                            f"{_HEARTBEAT_TIMEOUT_S}s. Ensure the extension is installed "
                            "and Chrome is open."
                        ),
                    }
                ).encode()
                self.send_response(503)
                self._set_cors()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                command = json.loads(body)
            except json.JSONDecodeError:
                self.send_response(400)
                self._set_cors()
                self.end_headers()
                return

            # Command schema validation
            validation_error = self.server.bridge._validate_command(command)
            if validation_error:
                resp = json.dumps({"error": "invalid_command", "reason": validation_error}).encode()
                self.send_response(400)
                self._set_cors()
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(resp)
                return

            request_id = self.server.bridge.enqueue(command)
            resp = json.dumps({"request_id": request_id}).encode()
            self.send_response(200)
            self._set_cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(resp)
        elif self.path == "/result":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                result = json.loads(body)
                self.server.bridge._store_result(result)
                self.send_response(200)
                self._set_cors()
                self.end_headers()
            except json.JSONDecodeError:
                self.send_response(400)
                self._set_cors()
                self.end_headers()
        elif self.path == "/heartbeat":
            # Task 1.4: extension liveness signal
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                info = json.loads(body) if length else {}
            except json.JSONDecodeError:
                info = {}
            self.server.bridge._record_heartbeat(info)
            self.send_response(204)
            self._set_cors()
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()


class BridgeServer:
    """HTTP bridge server for Chrome extension communication."""

    def __init__(self, port: int = DEFAULT_PORT):
        self._requested_port = port
        self._queue: list[dict] = []
        self._results: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = port


        # Task 1.4: heartbeat tracking
        self._last_heartbeat_time: float = 0.0
        self._extension_info: dict[str, Any] = {}



    def _validate_command(self, command: dict) -> str | None:
        """Validate command schema. Returns error reason string or None if valid."""
        action = command.get("action")
        if action not in ("save", "preflight"):
            return f"invalid action '{action}'; must be 'save' or 'preflight'"
        url = command.get("url", "")
        if not isinstance(url, str) or not (url.startswith("http://") or url.startswith("https://")):
            return f"invalid url '{url}'; must start with http:// or https://"
        return None

    # ------------------------------------------------------------------
    # Extension connectivity (Task 1.4)
    # ------------------------------------------------------------------

    @property
    def extension_connected(self) -> bool:
        """True if extension sent a heartbeat within the last 30s."""
        if self._last_heartbeat_time == 0.0:
            return False
        return (time.monotonic() - self._last_heartbeat_time) < _HEARTBEAT_TIMEOUT_S

    def _record_heartbeat(self, info: dict) -> None:
        with self._lock:
            self._last_heartbeat_time = time.monotonic()
            self._extension_info = info

    def get_status(self) -> dict[str, Any]:
        """Return enriched status dict for GET /status."""
        connected = self.extension_connected
        last_seen_s = None
        if self._last_heartbeat_time > 0:
            last_seen_s = round(time.monotonic() - self._last_heartbeat_time, 1)

        status: dict[str, Any] = {
            "bridge": "running",
            "port": self.port,
            "extension_connected": connected,
        }
        if last_seen_s is not None:
            status["extension_last_seen_s"] = last_seen_s
        if self._extension_info:
            status["extension_version"] = self._extension_info.get("extension_version")
            status["zotero_running"] = self._extension_info.get("zotero_connected", False)
        return status

    # ------------------------------------------------------------------
    # Queue and result management
    # ------------------------------------------------------------------

    def enqueue(self, command: dict) -> str:
        """Add a save command to the queue. Returns request_id."""
        command = {**command}  # defensive copy — never mutate caller's dict
        if "request_id" not in command:
            command["request_id"] = uuid.uuid4().hex[:12]
        request_id: str = str(command["request_id"])
        with self._lock:
            self._queue.append(command)
        return request_id

    def get_result(self, request_id: str) -> dict[str, Any] | None:
        """Get a stored result without blocking."""
        with self._lock:
            entry = self._results.get(request_id)
            return entry["data"] if entry else None

    def _dequeue(self) -> dict | None:
        with self._lock:
            return self._queue.pop(0) if self._queue else None

    def _store_result(self, result: dict) -> None:
        """Store result with TTL metadata. Evicts stale entries on each store."""
        request_id = result.get("request_id")
        if not request_id:
            return
        now = time.monotonic()
        with self._lock:
            # Evict entries older than TTL
            stale = [rid for rid, e in self._results.items() if now - e["ts"] > _RESULT_TTL_S]
            for rid in stale:
                del self._results[rid]
            # Cap at max entries (evict oldest first)
            if len(self._results) >= _RESULT_MAX_ENTRIES:
                oldest = sorted(self._results.items(), key=lambda kv: kv[1]["ts"])
                for rid, _ in oldest[: len(self._results) - _RESULT_MAX_ENTRIES + 1]:
                    del self._results[rid]
            self._results[request_id] = {"data": result, "ts": now}

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Start the HTTP server in a background thread."""
        self._server = ThreadingHTTPServer(("127.0.0.1", self._requested_port), _BridgeHandler)
        self._server.bridge = self  # type: ignore[attr-defined]
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        logger.info(f"Bridge server listening on http://127.0.0.1:{self.port}")

    def stop(self):
        """Stop the HTTP server."""
        if self._server:
            self._server.shutdown()
            self._server = None

    @staticmethod
    def is_running(port: int = DEFAULT_PORT) -> bool:
        """Check if a bridge is already running on the given port."""
        import urllib.request

        try:
            resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=2)
            return bool(resp.status == 200)
        except Exception:
            return False

    @staticmethod
    def auto_start(port: int = DEFAULT_PORT) -> None:
        """Start bridge as a background subprocess if not already running."""
        if BridgeServer.is_running(port):
            return
        subprocess.Popen(
            [sys.executable, "-m", "zotpilot.cli", "bridge", "--port", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(10):
            time.sleep(0.5)
            if BridgeServer.is_running(port):
                return
        raise RuntimeError(
            f"Failed to auto-start bridge on port {port}. "
            "Ensure zotpilot is installed in the active Python environment."
        )

# ZotPilot Connector Bridge Protocol

> **Version:** 2.1.0
> **Scope:** HTTP bridge between ZotPilot MCP server and ZotPilot Connector extension

The bridge exposes HTTP endpoints on `http://127.0.0.1:2619`. Any HTTP-capable client (MCP tool, curl, Python script) can integrate without Chrome-specific APIs.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Authentication](#2-authentication)
3. [Command Validation](#3-command-validation)
4. [Endpoints](#4-endpoints)
5. [Data Types](#5-data-types)
6. [Error Taxonomy](#6-error-taxonomy)
7. [Polling Contract](#7-polling-contract)
8. [Sequence Diagrams](#8-sequence-diagrams)
9. [Manual Testing with curl](#9-manual-testing-with-curl)
10. [Versioning](#10-versioning)

---

## 1. Architecture Overview

```
┌─────────────┐    HTTP/JSON     ┌──────────────┐   2s poll   ┌──────────────────┐
│ ZotPilot    │◄──────────────►  │ BridgeServer │◄──────────► │ ZotPilot         │
│ MCP server  │                 │ (localhost   │             │ Connector        │
│ (Python)    │                 │  :2619)      │             │ (Chrome ext.)    │
└─────────────┘                 └──────────────┘             └────────┬─────────┘
                                                                      │
                                                                     opens tab
                                                                      ▼
                                                               ┌──────────────────┐
                                                               │ Zotero Desktop   │
                                                               │ (localhost       │
                                                               │  :23119)         │
                                                               └──────────────────┘
```

### Components

| Component | Language | Role |
|-----------|----------|------|
| `bridge.py` | Python | HTTP server, command queue, result storage, heartbeat tracking |
| `agentAPI.js` | JavaScript | Extension-side poller, save orchestration, completion detection |
| MCP tool `ingest_by_identifiers` | Python | Client-facing API (via ZotPilot MCP server) |

---

## 2. Authentication

### Origin-based ACL (v2.1+)

The bridge uses **Origin-based ACL** — no shared secret, no configuration.

#### How it works

Every browser cross-origin `fetch()` carries an immutable `Origin` header that
the page's JavaScript cannot forge. The bridge inspects it on every request:

| Origin value                        | Verdict |
|-------------------------------------|---------|
| (absent) — CLI / MCP / curl         | Allow   |
| `chrome-extension://<id>`           | Allow   |
| `moz-extension://<id>`              | Allow   |
| `safari-web-extension://<id>`       | Allow   |
| `https://…`, `http://…`, `null`, … | **403** |

`/status` is exempt from the check so health probes work without a browser.

#### Threat model covered
- ✅ Malicious website you visit cannot POST `/enqueue` to save items to
  your Zotero (Origin would be `https://attacker.com`, rejected).
- ❌ Same-machine processes can impersonate the bridge on a different port —
  out of scope (localhost is the trust boundary).
- ❌ User deliberately installing a malicious browser extension — out of scope.

## 3. Command Validation

All commands submitted via `POST /enqueue` are validated before being queued:

| Field | Validation | Error on failure |
|-------|-----------|------------------|
| `action` | Must be `"save"` or `"preflight"` | `400 {"error": "invalid_command", "reason": "..."}` |
| `url` | Must start with `http://` or `https://` | `400 {"error": "invalid_command", "reason": "..."}` |

The extension also validates commands received from `/pending` before executing them (defense-in-depth).

---

## 4. Endpoints

### `GET /pending`

Extension polls this endpoint every 2 seconds. Returns the next pending save command, or `204 No Content` if the queue is empty.

**Request**
```
GET /pending
Accept: application/json
```

**Response (200 — command available)**
```json
{
  "request_id": "abc123def456",
  "action": "save",
  "url": "https://arxiv.org/abs/2401.00001",
  "collection_key": "XYZ789",
  "tags": ["ml", "survey"]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `request_id` | string | always | Opaque identifier for this save; must be echoed in the result |
| `action` | string | always | `"save"` or `"preflight"` |
| `url` | string | always | URL to open and save |
| `collection_key` | string \| null | no | Zotero collection key to place the item in |
| `tags` | string[] | no | Tags to apply to the item |

**Response (204 — no command)**
```
No body
```

---

### `POST /enqueue`

Enqueue a save command. Used by MCP tools and other clients.

**Request**
```
POST /enqueue
Content-Type: application/json
```

```json
{
  "action": "save",
  "url": "https://www.science.org/doi/10.1126/science.adk0001",
  "collection_key": "MYCOLL1",
  "tags": ["climate", "2024"]
}
```

**Response (200)**
```json
{ "request_id": "abc123def456" }
```

**Response (400 — invalid command)**
```json
{
  "error": "invalid_command",
  "reason": "invalid action 'delete'; must be 'save' or 'preflight'"
}
```

**Response (400 — malformed JSON)**
```
No body
```

**Response (403 — origin not allowed)**
```json
{ "error": "forbidden_origin", "origin": "https://evil.example" }
```

**Response (503 — extension not connected)**
```json
{
  "error_code": "extension_not_connected",
  "error_message": "ZotPilot Connector has not sent a heartbeat in the last 30s. Ensure the extension is installed and Chrome is open."
}
```

---

### `POST /result`

Extension posts save results here after completion.

**Request**
```
POST /result
Content-Type: application/json
```

```json
{
  "request_id": "abc123def456",
  "success": true,
  "url": "https://arxiv.org/abs/2401.00001",
  "title": "A Neural Network for Everything",
  "item_key": "ABCD1234",
  "collection_key": "XYZ789",
  "tags": ["ml"],
  "_detected_via": "sendMessage"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `request_id` | string | always | Echoed from the command |
| `success` | boolean \| `"unconfirmed"` | always | `true` = confirmed saved; `"unconfirmed"` = timeout, outcome unknown |
| `error_code` | string | if `success` is falsy | Canonical error code (see §6) |
| `error_message` | string | if `success` is falsy | Human-readable error |
| `url` | string | always | Echoed from the command |
| `title` | string | always | Page/tab title at time of result (the saved item's title) |
| `item_key` | string \| null | no | Zotero item key (best-effort) |
| `collection_key` | string \| null | no | Echoed from command |
| `tags` | string[] | no | Echoed from command |
| `_detected_via` | string | always | Telemetry: `"sendMessage"` \| `"receiveMessage"` \| `"timeout"` |

---

### `GET /result/<request_id>`

Clients poll for results after enqueueing.

**Response (200)**
```json
{
  "request_id": "abc123def456",
  "success": true,
  "url": "https://arxiv.org/abs/2401.00001",
  "title": "A Neural Network for Everything",
  "item_key": null,
  "warning": "collection_key/tags not applied — item not found in Zotero within discovery window"
}
```

**Response (204 — result not yet available)**
```
No body
```

---

### `POST /heartbeat`

Extension sends this every 10 seconds. Bridge tracks connectivity state.

**Request**
```
POST /heartbeat
Content-Type: application/json
```

```json
{
  "extension_version": "0.0.2",
  "zotero_connected": true
}
```

| Field | Type | Description |
|-------|------|-------------|
| `extension_version` | string | Version from manifest |
| `zotero_connected` | boolean | Whether Zotero desktop is reachable at localhost:23119 |

**Response (204)** — No body.

---

### `GET /status`

Health check and connectivity status. **Exempt from the Origin ACL** so non-browser health probes can check bridge state without attaching an Origin header.

**Request**
```
GET /status
```

**Response (200)**
```json
{
  "bridge": "running",
  "port": 2619,
  "extension_connected": true,
  "extension_last_seen_s": 3.2,
  "extension_version": "0.0.2",
  "zotero_running": true
}
```

| Field | Type | Description |
|-------|------|-------------|
| `bridge` | string | Always `"running"` if server is up |
| `port` | integer | Actual port the server is listening on |
| `extension_connected` | boolean | `true` if a heartbeat was received in the last 30s |
| `extension_last_seen_s` | float | Seconds since last heartbeat (omitted if never connected) |
| `extension_version` | string | From last heartbeat (omitted if never connected) |
| `zotero_running` | boolean | From last heartbeat (omitted if never connected) |

---

## 5. Data Types

### SaveCommand

```typescript
interface SaveCommand {
  request_id:    string;
  action:        "save" | "preflight";
  url:           string;      // must start with http:// or https://
  collection_key: string | null;
  tags:          string[];
}
```

### SaveResult

```typescript
interface SaveResult {
  request_id:   string;
  success:      boolean | "unconfirmed";
  error_code?:  string;       // present when success === false
  error_message?: string;     // present when success === false
  url:          string;
  title:        string;
  item_key?:    string | null;
  collection_key?: string | null;
  tags?:        string[];
  warning?:     string;
  _detected_via?: "sendMessage" | "receiveMessage" | "timeout";
}
```

---

## 6. Error Taxonomy

All error responses use `{ error_code, error_message }` split schema:

- **`error_code`** — stable, machine-readable, retryable
- **`error_message`** — human-readable, may change between versions

| `error_code` | Meaning | `success` value | Retryable? | Likely cause |
|---|---|---|---|---|
| `forbidden_origin` | Request `Origin` not allowed (not a browser extension and not empty) | — | No | Fix the caller; non-browser callers must omit `Origin` |
| `invalid_command` | Command failed schema validation | — | No | Invalid `action` or `url` |
| `extension_not_connected` | Bridge has not received a heartbeat in >30s | `false` | Yes | Chrome closed, extension disabled |
| `zotero_not_running` | Extension cannot reach Zotero desktop | `false` | Yes | Zotero desktop not running |
| `save_trigger_failed` | `onZoteroButtonElementClick` threw synchronously | `false` | Maybe | Page incompatible with Connector |
| `page_load_failed` | Tab failed to load the URL | `false` | Yes | Invalid URL, network error |
| `completion_unconfirmed` | Save triggered but no `progressWindow.done` received within 60s | `"unconfirmed"` | Maybe | Check Zotero directly |
| `collection_not_found` | Collection key does not exist | warning only | No | Fix the collection key |
| `api_key_missing` | `ZOTERO_API_KEY` needed for routing | warning only | No | Configure key |
| `bridge_enqueue_failed` | Failed to POST to bridge | `false` | Yes | Bridge not running |

### Notes

- `no_translator` is **not** a pre-flight error. The Connector's `saveAsWebpage` fallback handles pages without translators automatically.
- If a translatorless page ultimately fails, it surfaces as `save_trigger_failed` or `completion_unconfirmed`.
- `completion_unconfirmed` means the save **may** have succeeded. Always check Zotero before retrying to avoid duplicates.

---

## 7. Polling Contract

### Extension → Bridge

The extension polls `GET /pending` every **2 seconds** (MV3 service worker keep-alive).

On every **5th poll** (every 10 seconds), it also sends `POST /heartbeat` with `{ extension_version, zotero_connected }`.

If the extension misses 3 consecutive heartbeat cycles (>30s), the bridge marks it disconnected.

### Bridge → MCP tool

After `POST /enqueue`, the client polls `GET /result/<request_id>` every **2 seconds**, up to a **90-second overall timeout**.

---

## 8. Sequence Diagrams

### Happy path

```
Client          Bridge            Extension         Zotero
  │               │                   │                │
  │─POST /enqueue>                    │                │
  │               │                   │                │
  │               │<────GET /pending───                │
  │               │──200 {url,id}────>                  │
  │               │                   │                │
  │               │              opens tab───>          │
  │               │                   │──translator──> │
  │               │                   │  detection     │
  │               │                   │──save────────> │
  │               │                   │                │
  │               │               POST /result {ok}     │
  │               │<─────────────────────────────────────│
  │<──GET result──│                   │                │
  │──200 {ok}────>                   │                │
  │               │                   │                │
```

### Origin ACL flow

```
Browser ext.     Bridge                    Malicious page   Bridge
  │               │                             │             │
  │──GET /pending  (Origin: chrome-extension://)│             │
  │<──200 {command}                             │             │
  │                                             │             │
  │──POST /result  (Origin: chrome-extension://)│             │
  │<──200                                       │             │
  │                                             │             │
  │                                             │──POST /enqueue  (Origin: https://evil.com)
  │                                             │<──403 {error: "forbidden_origin"}
  │                                             │             │
  │              (no shared secret — browser    │             │
  │               force-attaches Origin, JS     │             │
  │               cannot forge it)              │             │
```

### Completion detection (dual monkey-patch)

```
Translator      pageSaving.js       Messaging          background.js
   │                 │                  │                   │
   │──save complete─>│                  │                   │
   │                 │──sendMessage───>│                   │
   │                 │ ("progressWindow.│                   │
   │                 │  done", args)    │                   │
   │                 │                  │──dispatch via────>│
   │                 │                  │  MESSAGES config  │
   │                 │                  │                   │
   │                 │                  │  agentAPI.js      │
   │                 │                  │  monkey-patches:  │
   │                 │                  │  sendMessage ◄── PRIMARY
   │                 │                  │  receiveMessage  ◄── DEFENSE
   │                 │                  │                   │
   │                 │                  │──resolve(promise)│
   │                 │                  │                   │
```

### Failure: extension disconnected

```
Client          Bridge
  │               │
  │─POST /enqueue>│
  │──503 {error:  │──enqueue succeeds, returns request_id
  │   extension_  │
  │   not_connected}
  │               │
```

---

## 9. Manual Testing with curl

Assumes the bridge is running on port 2619. `curl` does not attach an `Origin` header, so it hits the bridge as a non-browser caller and always passes the ACL.

### 1. Check bridge health

```bash
curl http://127.0.0.1:2619/status
```

Expected: `{"bridge": "running", "port": 2619, ...}` — no `auth_token` field.

### 2. Enqueue a save

```bash
curl -X POST http://127.0.0.1:2619/enqueue \
  -H "Content-Type: application/json" \
  -d '{
    "action": "save",
    "url": "https://arxiv.org/abs/2401.00001",
    "collection_key": null,
    "tags": ["test"]
  }'
```

Expected: `{"request_id": "..."}`

### 3. Test forbidden origin (simulate a malicious webpage)

```bash
curl -X POST http://127.0.0.1:2619/enqueue \
  -H "Content-Type: application/json" \
  -H "Origin: https://evil.example" \
  -d '{"action": "save", "url": "https://example.com"}'
```

Expected: `HTTP 403 {"error": "forbidden_origin", "origin": "https://evil.example"}`

### 4. Test invalid command

```bash
curl -X POST http://127.0.0.1:2619/enqueue \
  -H "Content-Type: application/json" \
  -d '{"action": "delete", "url": "https://example.com"}'
```

Expected: `HTTP 400 {"error": "invalid_command", "reason": "..."}`

### 5. Extension processes — poll for result

```bash
# Replace REQUEST_ID with the id from step 2
curl http://127.0.0.1:2619/result/REQUEST_ID
```

Expected (after ~5-15s): `{"request_id": "REQUEST_ID", "success": true, ...}`

### 6. Simulate disconnect (extension not running)

Close Chrome, wait 35s, then:

```bash
curl -X POST http://127.0.0.1:2619/enqueue \
  -H "Content-Type: application/json" \
  -d '{"action": "save", "url": "https://example.com"}'
```

Expected: `HTTP 503 {"error_code": "extension_not_connected", ...}`

### 7. Simulate an allowed browser-extension caller

```bash
curl -X POST http://127.0.0.1:2619/enqueue \
  -H "Content-Type: application/json" \
  -H "Origin: chrome-extension://abc123" \
  -d '{"action": "save", "url": "https://example.com"}'
```

Expected: `{"request_id": "..."}` (same as step 2; the extension Origin prefix passes the ACL)

---

## 10. Versioning

The protocol is backward-compatible. New optional fields may be added to requests and responses at any time.

Breaking changes (removing or renaming fields) will increment the major version and be documented in CHANGELOG.

| Version | Changes |
|---------|---------|
| **2.1.0** | Replaced token-based auth with Origin-based ACL (no `X-ZotPilot-Token`, no `auth_token` in `/status`) |
| **2.0.0** | Added token-based authentication (`X-ZotPilot-Token` header), command schema validation (action/url), `auth_token` in `/status` response |
| **1.0.0** | Initial protocol version |

Current protocol version: **2.1.0**

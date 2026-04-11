---
name: zotpilot
description: >-
  Use when user mentions Zotero, academic papers, citations, literature reviews,
  research libraries, or wants to search/organize their paper collection.
  The actual workflow processing is guided by ZotPilot's native skills
  (ztp-research, ztp-review, ztp-profile, ztp-setup). This document serves as a
  reference for the 18 MCP tools provided by the ZotPilot server.
license: MIT
compatibility:
  - Python 3.10+
  - Zotero desktop (installed and run at least once)
---

# ZotPilot MCP Server Reference

This reference is for the 18 available MCP tools in ZotPilot v0.5.0. Workflows should be implemented by matching user intent to one of the specific sub-skills: `ztp-research`, `ztp-review`, `ztp-profile`, or `ztp-setup`.

## Tool Index (18 Core Tools)

Tools are organized into two semantic profiles (`core` and `full`).

### 🔍 Search (4 Tools)
| Tool | Purpose | Profile |
|------|---------|---------|
| `search_papers` | Passage-level semantic search (`section_type` supports tables/figures) | `core` |
| `search_topic` | Paper-level topic discovery | `core` |
| `search_boolean` | Exact keyword full-text search | `core` |
| `advanced_search` | Metadata filter (year/author/tag/DOI/collection, works without index) | `core` |

### 📖 Read (6 Tools)
| Tool | Purpose | Profile |
|------|---------|---------|
| `get_passage_context` | Expand a search result with surrounding text | `core` |
| `get_paper_details` | Full metadata + abstract for one paper | `core` |
| `get_notes` | Read notes attached to papers | `core` |
| `get_annotations` | Read highlights and comments (requires API key) | `core` |
| `browse_library` | Browse library overview / tags / collections / papers | `core` |
| `profile_library` | **[SLOW: 30-60s]** Full-library theme analysis | `full` |

### 🌐 Discover (1 Tool)
| Tool | Purpose | Profile |
|------|---------|---------|
| `search_academic_databases` | OpenAlex external search (support full features) | `core` |

### 📥 Ingest (1 Tool)
| Tool | Purpose | Profile |
|------|---------|---------|
| `ingest_by_identifiers` | Atomic atomic ingest using DOI/arXiv/URL. Returns sync final status | `core` |

### 📁 Organize (3 Tools)
| Tool | Purpose | Profile |
|------|---------|---------|
| `manage_tags` | Add / remove / set tags (**CRITICAL: prefer existing tags**) | `core` |
| `manage_collections` | Add / remove / create collections (**CRITICAL: prefer existing**) | `core` |
| `create_note` | Create a note on a paper | `core` |

### 🔗 Cite & Index (3 Tools)
| Tool | Purpose | Profile |
|------|---------|---------|
| `get_citations` | Citation graph: citing papers / references / counts | `core` |
| `index_library` | Build / update / local re-index of vector index | `core` |
| `get_index_stats` | Verify index readiness and health | `core` |

---

## Critical Behaviors and Hard Rules

1. **Self-Validation**: Do not attempt to bypass or ignore error messages from tools. If `ingest_by_identifiers` returns `action_required`, **STOP IMMEDIATELY** and surface the remediation steps to the user.
2. **Taxonomy Discipline**: Never blindly create new tags or collections. Use `browse_library` to check the existing taxonomy, and only propose existing tags where possible. Wait for user confirmation for creating >5 papers or new vocabulary.
3. **Atomic Pacing**: After the V0.5.0 refactor, ingest acts synchronously. No need to loop/poll statuses.
4. **Tool Replacements**:
   - `save_urls` is now handled natively within `ingest_by_identifiers`.
   - `search_tables` and `search_figures` are now accessed via `search_papers(section_type=...)`.
   - `reindex_degraded` is now handled by `index_library(item_keys=[...])`.

---

## Error Recovery

### Critical: Ingestion Blocks

When `ingest_by_identifiers` returns `action_required` containing `anti_bot` or `connector_offline`, this is a **HARD HALT**.
1. Surface the contents of `action_required` verbatim.
2. If it's a captcha block, instruct the user to complete verification in their browser.
3. Once completed, invoke `ingest_by_identifiers` again with the identical identifier list.

| Symptom / Reason | Fix |
|---|---|
| `extension_not_connected` | Ask user to open Chrome and ensure ZotPilot Connector is active. |
| `anti_bot` / Cloudflare check | **STOP**. Surface to user. Wait for manual verification. Retry exactly. |
| Missing PDF / Paywalled | Acceptable. The metadata is intact. You can notify the user. |

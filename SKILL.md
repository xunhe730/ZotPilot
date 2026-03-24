---
name: zotpilot
description: >-
  Use when user mentions Zotero, academic papers, citations,
  literature reviews, research libraries, or wants to search/organize their
  paper collection. Also triggers on "find papers about...", "what's in my
  library", "organize my papers", "who cites...", "tag these papers".
  Always use this skill for Zotero-related tasks.
license: MIT
compatibility:
  - Python 3.10+
  - Zotero desktop (installed and run at least once)
---

# ZotPilot

> All script paths are relative to this skill's directory.

## Step 1: Check readiness

**Python command:** Use `python3` on Linux/macOS. On Windows, use `python`.

Run: `python3 scripts/run.py status --json`  (Windows: `python scripts/run.py status --json`)

Parse the JSON output and follow the FIRST matching branch:

1. Command fails entirely → **Prerequisites** (see `references/setup-guide.md`)
2. `config_exists` is false → **First-Time Setup** (see `references/setup-guide.md`)
3. `errors` is non-empty → **First-Time Setup** (note: `warnings` like API key not in env are OK if key was passed to `register`)
4. `index_ready` is false or `doc_count` is 0 → go to **Index** below
5. All green → go to **Research**

**Inline fallback** (if agent cannot access references/):
```bash
python3 scripts/run.py setup --non-interactive --provider gemini
python3 scripts/run.py register
# Restart your AI agent, then ask again.
```

If any errors: run `python3 scripts/run.py doctor` for diagnostics.

## Index (if doc_count = 0)

MCP tools are now available. Index the user's papers:

```bash
python3 scripts/run.py index
```

Indexing takes ~2-5 seconds per paper. Documents over 40 pages are skipped by default.
After indexing, check for "Skipped N long documents" — offer to index them with `--max-pages 0`.

## Research (daily use)

### Tool selection — pick the RIGHT tool first

| User intent | Tool | Key params |
|---|---|---|
| Find specific passages or evidence | `search_papers` | `query`, `top_k=10`, `section_weights`, `required_terms` |
| Survey a topic / "what do I have on X" | `search_topic` | `query`, `num_papers=10` |
| Find a known paper by name/author | `search_boolean` | `query`, `operator="AND"` |
| Find data tables | `search_tables` | `query` |
| Find figures | `search_figures` | `query` |
| Read more context around a result | `get_passage_context` | `doc_id`, `chunk_index`, `window=3` |
| See all papers | `get_library_overview` | `limit=100`, `offset=0` |
| Paper details | `get_paper_details` | `item_key` |
| Who cites this? | `find_citing_papers` | `doc_id` |
| Tag/organize one paper | `add_item_tags`, `add_to_collection` | `item_key` |
| Batch tag/organize many papers | `batch_tags`, `batch_collections` | `items` or `item_keys`, `action` |
| Search external databases for new papers | `search_academic_databases` | `query`, `limit=20` |
| Add a paper by DOI/arXiv/URL | `add_paper_by_identifier` | `identifier` |
| Batch add papers from search results | `ingest_papers` | `papers` (from search_academic_databases) |
| Capture a URL to Zotero via browser | `save_from_url` | `url`, `collection_key`, `tags` — **requires ZotPilot Connector** |
| Batch capture URLs to Zotero via browser | `save_urls` | `urls` (list, max 10), `collection_key`, `tags` — **requires ZotPilot Connector** |

> **`save_from_url` prerequisites:** ZotPilot Connector must be installed in Chrome and Chrome must be open. The bridge (`:2619`) auto-starts. Returns `item_key` when found in Zotero within 3s discovery window. Requires `ZOTERO_API_KEY` for `item_key` discovery and routing — without it, save still works but `item_key` is not returned.

### Workflow chains

**Literature review:**
search_topic → get_paper_details (top 5) → find_references → search_papers with section_weights

**"What do I have on X?":**
search_topic(num_papers=20) → report count, year range, key authors, top passages

**Organize by theme (batch):**
search_topic → create_collection → batch_collections(action="add", item_keys=[...]) → batch_tags(action="add", items=[...])

**Find specific paper:**
search_boolean first (exact terms) → fallback to search_papers (semantic) → get_paper_details

**Find and add new papers:**
search_academic_databases → review candidates with user → ingest_papers → index_library

**Agent research discovery** ("帮我调研 X"):

Agent judges routing from context — no mechanical Q&A. Capability reference:

| Situation | Save path | PDF? | Speed |
|---|---|---|---|
| User says "要读" / "要索引" / "需要全文" | `save_from_url("doi.org/{doi}")` or `save_urls([...])` | ✅ institutional via browser | ~30s/URL |
| arXiv paper (any intent) | `add_paper_by_identifier("arxiv:ID")` | ✅ OA free | fast |
| Only need metadata / references | `ingest_papers([{doi:...}])` | ❌ OA only | fast, batch 50 |
| URL-only, no DOI/arXiv | `save_from_url(url)` or `save_urls(urls)` | ✅ institutional | ~30s/URL |

Discovery channels (agent picks based on topic):
- **OpenAlex / Semantic Scholar**: `search_academic_databases(query, year_min=...) `— structured results with abstracts, citation counts, DOIs; S2 tried first, OpenAlex fallback on 429
- **PubMed**: `mcp__claude_ai_PubMed__search_articles` — biomedical focus
- **arXiv / DOI direct**: `add_paper_by_identifier` — when identifier is known
- **Real browser search**: Playwright MCP `browser_navigate` + `browser_snapshot` — Google Scholar, any publisher page, no API limits

Step 1 — Discover: `search_academic_databases(X, limit=20)` (add PubMed for biomedical)
Step 2 — Evaluate: agent ranks by relevance, citations, recency, journal quality; shows user a curated list (5–10 papers) with one-line rationale each; user only confirms or adjusts
Step 3 — Ingest:
  - Papers with arxiv_id → `add_paper_by_identifier("arxiv:ID")`
  - Papers with DOI, PDF needed → `save_urls(["https://doi.org/{doi}", ...])` (batch, max 10)
  - Papers with DOI, metadata only → `ingest_papers([{doi:...}])`
Step 4 — Post-ingest (per item_key, when available): `index_library` → `get_paper_details` → `create_note` → `batch_tags` / `batch_collections`

Alert user when using `save_from_url` / `save_urls`: ~30s per URL, Chrome + Connector must be running.

**Agent research ingest** (via ZotPilot Connector):
Prerequisites: Chrome open, ZotPilot Connector installed, `ZOTERO_API_KEY` configured
1. `save_from_url(url)` → get `item_key` from result
2. `index_library(item_key=...)` → incremental index (only this paper)
3. `get_paper_details(item_key)` → read abstract, methods, conclusions
4. `create_note(item_key, content)` → write structured reading note
5. `add_to_collection(item_key, collection_key)` + `add_item_tags(item_key, tags)` → classify

If `item_key` missing from `save_from_url` result: use `advanced_search(title=result["title"])` to locate the item first.

**Organize library (classification advisor):**
get_library_overview + list_collections + list_tags → analyze themes via
search_topic → diagnose issues (uncategorized papers, inconsistent tags,
oversized collections) → propose collection hierarchy + tag normalization
→ interview user for confirmation → batch_collections + batch_tags(add/remove) to execute

### Output formatting

- Lead with paper title, authors, year, citation key
- Quote the relevant passage directly
- Include page number and section name
- Group results by paper, not by chunk
- Render table content as markdown tables
- NEVER dump raw JSON to the user

### Error recovery

| Error | Fix |
|---|---|
| Empty results | Try broader query, or `search_boolean` for exact terms. Check `get_index_stats` |
| "GEMINI_API_KEY not set" | Expected if key was passed to `register`. Only re-run setup if provider is wrong |
| "ZOTERO_API_KEY not set" | Write ops need Zotero Web API credentials — see `references/setup-guide.md` |
| "Document has no DOI" | Cannot use citation tools for this paper |
| "No chunks found" | Paper not indexed — run `index_library(item_key="...")` |
| `save_from_url`: "extension_not_connected" | Chrome not open or ZotPilot Connector not installed/enabled. Open Chrome and check `chrome://extensions/` |
| `save_from_url`: no `item_key` in result | `ZOTERO_API_KEY` not set, or title match was ambiguous. Use `advanced_search(title=...)` to find the item |

### Write operations (tags, collections)

Write tools require Zotero Web API credentials (`ZOTERO_API_KEY` + `ZOTERO_USER_ID`).
If missing, see **Configure Zotero Web API** in `references/setup-guide.md`.

**Single-item:** `add_item_tags`, `set_item_tags`, `remove_item_tags`, `add_to_collection`, `remove_from_collection`, `create_collection`

**Batch (max 100 items):** `batch_tags(action="add|set|remove")`, `batch_collections(action="add|remove")`
Partial failures are reported per-item without rollback.

For detailed parameter reference, see `references/tool-guide.md`.
For common issues and fixes, see `references/troubleshooting.md`.

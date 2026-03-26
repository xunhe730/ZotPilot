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

## When to Apply

### Must Use
- User mentions Zotero, papers, citations, literature review, research library
- "帮我调研 X", "find papers about X", "search my library"
- "who cites X", "tag these papers", "add to collection"
- Any ingest / save / annotate operation on academic papers

### Recommended
- User pastes a DOI, arXiv ID, or paper URL and wants it saved
- User asks to analyze, profile, or summarize their library
- Post-ingest: tagging, classification, note creation

### Skip
- Pure coding tasks unrelated to literature
- General web search not involving academic papers
- File operations on non-Zotero data

---

## MCP Tool Reference

### Search (read-only, no API key required)

| Tool | Use when |
|------|----------|
| `search_papers` | Find specific passages or claims in indexed papers |
| `search_topic` | Survey a topic across your local library |
| `search_boolean` | Exact term match (author names, acronyms) |
| `search_tables` | Find data tables in papers |
| `search_figures` | Find figures or diagrams |
| `get_passage_context` | Expand a search result with surrounding text |
| `get_paper_details` | Full metadata + abstract for one paper |
| `find_citing_papers` | Papers that cite a given work |
| `find_references` | References listed in a paper |
| `get_citation_count` | Citation count from external sources |
| `advanced_search` | Filter by year/author/tag/DOI/collection (works without index) |
| `get_library_overview` | Summary stats of entire library |
| `get_notes` | Read notes attached to a paper |
| `list_tags` | All tags in library |
| `list_collections` | All collections |

### External discovery

| Tool | Use when |
|------|----------|
| `search_academic_databases` | Search OpenAlex + Semantic Scholar (papers NOT yet in library) |
| `profile_library` | Analyze library themes, gaps, top venues |

### Ingest (requires Chrome + ZotPilot Connector for non-OA papers)

| Tool | Use when |
|------|----------|
| `ingest_papers` | Batch add papers by DOI / arXiv ID / OA URL |
| `save_urls` | Save non-OA papers via browser connector |
| `save_from_url` | Save a single URL via browser connector |

### Write (requires `ZOTERO_API_KEY` + `ZOTERO_USER_ID`)

| Tool | Use when |
|------|----------|
| `create_note` | Add a note to a paper |
| `add_item_tags` | Add tags without replacing existing ones |
| `set_item_tags` | Replace all tags on a paper |
| `create_collection` | Create a new collection |
| `add_to_collection` | Move paper into a collection |
| `batch_tags` | Tag multiple papers at once |
| `batch_collections` | Batch collection assignment |

---

## Research (daily use)

### Agent research discovery ("帮我调研 X")

**Step 0 — Subscription check** (mandatory):
Check whether user has a Zotero Web API key configured. Write ops (`save_from_url`, `save_urls`, tag/collection tools) require `ZOTERO_API_KEY`. If not set, warn before proceeding.

**Default research flow:**
1. `search_topic(X)` to discover what is already in the local library
2. Optionally `search_papers(X)` when you need supporting passages
3. Optionally `get_passage_context(doc_id, chunk_index)` when you need surrounding text

All search tools default to `verbosity="minimal"`. Escalate to `standard` or `full` only when you need more metadata.
`search_papers` defaults to `context_chunks=0`; set `context_chunks=1` only when surrounding chunks are needed.
`search_topic` no longer returns `best_passage_context`. Use `search_papers` or `get_passage_context` for expanded text.
`doc_id` is the canonical identifier across tools. In `search_boolean`, `item_key` remains as a backward-compatible alias with the same value.
`get_library_overview` and `get_paper_details` now return `doc_id` instead of `key`.

**External discovery** remains available when you need new papers:
`search_academic_databases(X, limit=20)` (add PubMed MCP for biomedical topics)

**Score candidates** per `references/scoring-guide.md`

> **MUST STOP** — Show scored list to user and wait for explicit confirmation before any ingest. Do not proceed automatically.

**Ingest** (after user confirms list):

De-duplicate first: batch-check all candidate DOIs in one call:
`advanced_search(conditions=[{field:"doi", op:"is", value:"doi1"}, ...], match:"any")`
- Already in library → skip ingest, inform user ("已在库中: Title"), use existing `item_key` for Step 4
- Not found → route each new paper by priority:

Preflight check:
- `ingest_papers(...)` now runs an accessibility preflight by default before it saves anything through the Connector.
- Read `preflight_report` on every `ingest_papers` response. It contains `checked`, `accessible`, `blocked`, `skipped`, `errors`, and `all_clear`.
- If `all_clear=false`, stop and show the blocked/error URLs to the user before retrying. No save should have happened yet on that code path.
- Use `preflight=False` only when the user explicitly wants to bypass the check after resolving access issues or accepting the risk.

| Priority | Condition | Tool |
|----------|-----------|------|
| 1 | `arxiv_id` present | `ingest_papers([{arxiv_id:...}])` |
| 2 | `doi` + `is_oa=True` + `oa_url` | `ingest_papers([{doi:..., oa_url:...}])` |
| 3 | `doi` + `is_oa=False` + `landing_page_url` | `save_urls([landing_page_url])` — requires Chrome+Connector |
| 4 | `doi` only | `ingest_papers([{doi:...}])` — metadata only, no PDF |
| 5 | No `doi` | Skip, inform user |

Anti-bot / translator recovery:
- `save_urls` entry has `error_code: "anti_bot_detected"` → retry with canonical DOI URL (`doi.org/{doi}`) or ask user to open the page manually in Chrome
- `save_urls` entry has `translator_fallback_detected=True` → saved as web snapshot; user should verify/replace in Zotero
- `save_urls` entry has `pdf_failed=True` (Elsevier-style robot check on PDF) → metadata saved successfully; inform user to download PDF manually

**Step 4 — Post-ingest:** for each `item_key`, follow `references/post-ingest-guide.md`

**Library profiling:**
Trigger: user says "分析我的文献库", "profile my library", or ZOTPILOT.md missing before research starts → follow `references/profiling-guide.md`

---

## Error Recovery

| Error code / symptom | Cause | Fix |
|---|---|---|
| `extension_not_connected` | Chrome not open or Connector not installed/enabled | Open Chrome, check `chrome://extensions/`, enable ZotPilot Connector |
| `error_code: "anti_bot_detected"` | Cloudflare / publisher blocked the save before it happened | Retry with `doi.org/{doi}`, or ask user to manually open page in Chrome first |
| `translator_fallback_detected` | No Zotero translator matched the page | Saved as web snapshot — user should verify/replace in Zotero |
| `pdf_failed: true` | Elsevier-style secondary robot check blocked PDF download | Metadata saved OK; user downloads PDF manually and attaches in Zotero |
| `status: "pending"` in batch result | Anti-bot triggered mid-batch; remaining URLs were short-circuited | Re-run ingest for the pending items after user resolves the blocked page |
